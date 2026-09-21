"""The Agents Platform image gallery, read and tagged over HTTP.

Ported from ``aw-app-crispal``'s ``crispal_app/mcp/gallery_http.py``
(2026-09-21, Architect plan ``.tmp/gallery-migration/PLAN.md``). The tools
themselves used to live in the Crispal MCP, which needed AP-MT credentials of
its own to reach the gallery — three config fields that were referenced in
``runtime.env`` but never declared in ``config_schema``, so ``enabled()``
there was always False and the whole path was dead. This app already mints
and rotates an ``agents_platform_token`` (see ``identity_token.py``), which is
exactly the credential the gallery's admin endpoints accept
(``require_tenant_or_service``), so the tools move here and the credential
stops being duplicated.

**What changed in the contract:** the old tools downloaded each image and
returned ``file_paths`` into the Crispal container's own disk. This module
runs inside the aw-mcp-gateway container, which shares no writable directory
with either the Crispal container or an agent container — a path from here
would name a file nobody else can open. So there are no paths any more, only
URLs: ``images[].url`` / ``image_urls`` (per-image capability URLs that need
no Authorization header of their own — only the metadata call is gated), and
``images[].id``, which is what identifies an image for :func:`set_tags`.

**stdlib + httpx only.** ``mcp_server.py`` is spawned as a stdio child inside
the gateway's container, which has no third-party packages beyond httpx — the
same restriction its own docstring states, and the reason
``kanban_dispatch.py`` (the module this one mirrors structurally) is
stdlib-only too.
"""
from __future__ import annotations

import httpx

TIMEOUT_S = 30.0

# Gallery share-tokens minted for this process, keyed by bot_slug. Minting is
# idempotent and cheap on the server side — POST /api/admin/gallery/token
# returns the EXISTING trusted-workspace token while it still has >7 days to
# live (gallery.py:418-430 in agents-platform-multitenant) — so this cache is
# only here to save a round-trip, never to make the call safe to repeat.
_TOKENS: dict[str, str] = {}


class GalleryError(RuntimeError):
    """An AP-MT gallery call that failed, carrying the status the tool layer
    reports back. ``status`` is 502 for "could not reach AP-MT at all", so a
    transport failure and an AP-MT rejection stay distinguishable — the
    distinction the run-initiator lookup lost for 13 silent cycles."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    try:
        resp = await client.get(url, params=params or {}, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise GalleryError(502, f"cannot reach agents-platform at {url}: {exc}") from exc
    if resp.status_code != 200:
        raise GalleryError(resp.status_code,
                           f"agents-platform answered {resp.status_code} for {url} — "
                           f"{resp.text[:300]}")
    return resp.json()


async def _post(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    try:
        return await client.post(url, json=payload, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise GalleryError(502, f"cannot reach agents-platform at {url}: {exc}") from exc


async def _fetch_blocks(client: httpx.AsyncClient, base: str, bot_slug: str,
                        source: str = "") -> list[dict]:
    """Blocks for one bot, or — with ``bot_slug`` empty — every block the
    caller's identity can see.

    An empty ``bot_slug`` is not "unfiltered across the world": the endpoint
    is behind ``require_tenant_or_service``, which BINDS a tenant, so the
    listing is already scoped to this workspace's own gallery. That is why it
    is a safe default (see ``DEFAULT_BOT_SLUG`` in mcp_server.py) — a
    hardcoded slug is only correct on the deployment it was written for.

    Caps at AP-MT's own 500 newest blocks either way (``admin_list_blocks``);
    with several bots in one tenant that ceiling arrives sooner, and what
    drops off is the oldest. ``last_block`` is unaffected.
    """
    params = {}
    if bot_slug:
        params["bot_slug"] = bot_slug
    if source:
        params["source"] = source
    payload = await _get(client, f"{base.rstrip('/')}/api/admin/gallery/blocks", params)
    # Oldest-first: every caller below (last_block, since_block, the order of
    # the returned images) is written against ascending created_at, while the
    # endpoint sorts descending. Not a cosmetic reversal — do not "simplify".
    return list(reversed(payload.get("blocks") or []))


def _tag_name(tag) -> str:
    """The admin endpoint returns tags as ``{"id": ..., "name": ...}``, not as
    bare strings — see ``_load_tags_by_image()`` in AP's api/gallery.py. A
    plain string is accepted too so this does not become a second bug the day
    that shape changes. Caught only after the legacy migration landed real
    tags; before that every image had an empty list and the code never ran."""
    if isinstance(tag, dict):
        return tag.get("name") or ""
    return tag or ""


def normalize_tag(raw) -> str:
    """Same business rule as ``_normalize_tag`` in AP-MT's models.py."""
    return " ".join(_tag_name(raw).strip().casefold().split())


async def list_tags(client: httpx.AsyncClient, base: str, bot_slug: str) -> dict:
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for block in await _fetch_blocks(client, base, bot_slug):
        for image in block.get("images") or []:
            for tag in image.get("tags") or []:
                key = normalize_tag(tag)
                counts[key] = counts.get(key, 0) + 1
                display.setdefault(key, _tag_name(tag))  # the display is text, not the object
    tags = [{"name": display[k], "image_count": v} for k, v in counts.items()]
    tags.sort(key=lambda t: (-t["image_count"], t["name"]))
    return {"tags": tags}


async def list_images(client: httpx.AsyncClient, base: str, bot_slug: str, scope: str,
                      block_id: str | None, tags: list[str], match: str,
                      source: str = "") -> dict:
    blocks = await _fetch_blocks(client, base, bot_slug, source)
    if scope == "block":
        blocks = [b for b in blocks if b["id"] == block_id]
    elif scope == "since_block":
        ids = [b["id"] for b in blocks]
        if block_id not in ids:
            raise GalleryError(404, f"block_id {block_id!r} not found for bot {bot_slug!r}")
        blocks = blocks[ids.index(block_id):]
    elif scope == "last_block" and blocks:
        blocks = [blocks[-1]]

    wanted = {normalize_tag(t) for t in tags}
    picked: list[dict] = []
    for block in blocks:
        for image in block.get("images") or []:
            if wanted:
                have = {normalize_tag(t) for t in (image.get("tags") or [])}
                if match == "all" and not wanted <= have:
                    continue
                if match != "all" and not (wanted & have):
                    continue
            picked.append({
                "id": image.get("id"),
                "block_id": block["id"],
                # The per-image capability URL. There is deliberately no local
                # path counterpart any more — see the module docstring.
                "url": image.get("direct_url"),
                "original_name": image.get("original_name"),
                "mime": image.get("mime"),
                "bytes": image.get("bytes"),
                "created_at": image.get("created_at"),
                "tags": [_tag_name(t) for t in (image.get("tags") or [])],
            })

    return {
        "blocks": [
            {"block_id": b["id"], "created_at": b["created_at"],
             "image_count": b["image_count"], "source": b["source"],
             # Which bot's gallery this block came from. Only interesting when
             # the caller named no bot_slug and the tenant runs more than one
             # — but then it is the only way to tell them apart.
             "bot_slug": b.get("bot_slug")}
            for b in blocks
        ],
        "images": picked,
        # Same order as `images`. Kept as its own flat array because that is
        # what the [[ATTACH: ...]] flow and the skills already consume.
        "image_urls": [i["url"] for i in picked],
    }


async def _mint_token(client: httpx.AsyncClient, base: str, bot_slug: str) -> str:
    """A bot-scoped GalleryToken, exchanged for this app's identity token.

    There is no tag-WRITE endpoint behind ``require_tenant_or_service`` — the
    write path is the share-link one, keyed on a GalleryToken. AP-MT already
    anticipated this exact caller: ``POST /api/admin/gallery/token``'s own
    comment says it exists to "let background apps exchange that identity for
    a bot-scoped gallery credential". So: mint here, tag with it below, and
    the migration stays inside two repos.
    """
    resp = await _post(client, f"{base.rstrip('/')}/api/admin/gallery/token",
                       {"bot_slug": bot_slug})
    if resp.status_code != 200:
        raise GalleryError(resp.status_code,
                           f"could not mint a gallery token for bot {bot_slug!r}: "
                           f"{resp.status_code} — {resp.text[:300]}")
    token = (resp.json() or {}).get("token")
    if not token:
        raise GalleryError(502, f"agents-platform minted no token for bot {bot_slug!r}")
    _TOKENS[bot_slug] = token
    return token


async def _gallery_token(client: httpx.AsyncClient, base: str, bot_slug: str) -> str:
    cached = _TOKENS.get(bot_slug)
    if cached:
        return cached
    return await _mint_token(client, base, bot_slug)


async def _owning_bots(client: httpx.AsyncClient, base: str,
                       image_ids: list[str]) -> dict[str, str]:
    """``image_id -> bot_slug``, for a caller that named no bot.

    A GalleryToken is bot-scoped, so tagging needs to know which bot owns each
    image before it can mint the right credential. When the caller named a
    bot, that answer is free; when it did not, it comes from the same listing
    ``list_images`` reads, so the ids the caller just received are exactly the
    ids resolvable here.
    """
    wanted = set(image_ids)
    owners: dict[str, str] = {}
    for block in await _fetch_blocks(client, base, ""):
        for image in block.get("images") or []:
            if image.get("id") in wanted:
                owners[image["id"]] = block.get("bot_slug") or ""
    return owners


async def set_tags(client: httpx.AsyncClient, base: str, bot_slug: str,
                   image_ids: list[str], tags: list[str]) -> dict:
    """Apply every tag in ``tags`` to every image in ``image_ids``.

    Idempotent, and additive only — an image keeps the tags it already had.
    ``tags_applied`` counts the links that were actually NEW, mirroring what
    the old ``on conflict do nothing`` INSERT reported, so re-running over the
    same images reports 0 rather than inflating the count.

    An image id the owning bot's token cannot tag answers 404 (AP-MT's
    ``_get_owned_image`` refuses to confirm a foreign id exists) and lands in
    ``missing_image_ids`` instead of failing the call — one bad id in a list
    of twenty must not lose the other nineteen.

    ``bot_slug`` is optional: with none, each image's owner is resolved from
    the gallery listing first, and one token is minted per distinct bot.
    """
    names = [t.strip() for t in tags if normalize_tag(t)]
    if not image_ids:
        raise GalleryError(400, "image_ids must be a non-empty list")
    if not names:
        raise GalleryError(400, "tags must be a non-empty list")

    owners = ({i: bot_slug for i in image_ids} if bot_slug
              else await _owning_bots(client, base, image_ids))

    tagged, applied, missing = 0, 0, []
    for image_id in image_ids:
        owner = owners.get(image_id)
        if not owner:
            missing.append(image_id)
            continue
        token = await _gallery_token(client, base, owner)
        found = True
        for name in names:
            url = f"{base.rstrip('/')}/api/gallery/{token}/image/{image_id}/tag"
            resp = await _post(client, url, {"name": name})
            if resp.status_code == 401:
                # The cached token expired or was revoked between calls. Mint
                # a fresh one and retry this tag once — an expired credential
                # is not the caller's error and must not read as a missing
                # image (which is what a plain `continue` would report).
                token = await _mint_token(client, base, owner)
                resp = await _post(client, f"{base.rstrip('/')}/api/gallery/{token}"
                                           f"/image/{image_id}/tag", {"name": name})
            if resp.status_code == 404:
                found = False
                break
            if resp.status_code != 200:
                raise GalleryError(resp.status_code,
                                   f"tagging image {image_id} with {name!r} failed: "
                                   f"{resp.status_code} — {resp.text[:300]}")
            if not (resp.json() or {}).get("already_applied"):
                applied += 1
        if found:
            tagged += 1
        else:
            missing.append(image_id)

    return {"tagged_images": tagged, "tags_applied": applied, "missing_image_ids": missing}
