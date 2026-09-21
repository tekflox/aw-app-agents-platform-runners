"""The gallery client ported out of aw-app-crispal (2026-09-21).

Every test here pins a behaviour whose absence was a real, shipped bug in the
module this one replaces — block ordering, tag objects vs strings, an image id
another bot owns. See `.tmp/gallery-migration/PLAN.md` §8.

`asyncio.run` rather than a pytest-asyncio marker: this suite has no
pytest-asyncio dependency and every other async test here (test_kanban_
dispatch.py, test_list_tools_apmt_outage.py) drives its coroutine the same way.

Run: .venv/aw/bin/python -m pytest tests/test_gallery.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import gallery as gallery_mod  # noqa: E402
from agents_platform_runners_app import mcp_server  # noqa: E402

BASE = "http://ap-mt.test"


def _image(image_id: str, *, tags: list | None = None, name: str = "a.jpg") -> dict:
    return {"id": image_id, "original_name": name, "mime": "image/jpeg", "bytes": 10,
            "created_at": "2026-09-01T00:00:00", "tags": tags or [],
            "direct_url": f"{BASE}/api/gallery/direct/tok-{image_id}"}


def _block(block_id: str, images: list[dict], *, source: str = "upload",
           created_at: str = "2026-09-01T00:00:00") -> dict:
    return {"id": block_id, "bot_slug": "aw-cris", "origin_chat_id": None,
            "source": source, "image_count": len(images),
            "created_at": created_at, "images": images}


def _run(handler, coro_factory):
    """Drive ``coro_factory(client)`` against a MockTransport-backed client."""
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await coro_factory(c)
    return asyncio.run(go())


def _blocks_handler(blocks: list[dict], *, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.path == "/api/admin/gallery/blocks"
        source = request.url.params.get("source")
        kept = [b for b in blocks if not source or b["source"] == source]
        # AP-MT answers newest-first; the client is what flips it.
        return httpx.Response(200, json={"blocks": list(reversed(kept))})
    return handler


def _list_images(handler, *args, **kwargs):
    return _run(handler, lambda c: gallery_mod.list_images(c, BASE, *args, **kwargs))


# --------------------------------------------------------------------------
# list_images — scope / tags / source
# --------------------------------------------------------------------------

def test_blocks_come_back_oldest_first():
    """AP-MT sorts descending; last_block/since_block and the order of the
    returned images are all written against ascending created_at. Getting this
    backwards makes `last_block` return the OLDEST upload — the one block a
    user would never mean by "the photos I just sent"."""
    blocks = [_block("b1", [_image("i1")], created_at="2026-09-01T00:00:00"),
              _block("b2", [_image("i2")], created_at="2026-09-02T00:00:00")]
    out = _list_images(_blocks_handler(blocks), "aw-cris", "all", None, [], "any")
    assert [b["block_id"] for b in out["blocks"]] == ["b1", "b2"]


def test_scope_last_block_keeps_only_the_newest():
    blocks = [_block("b1", [_image("i1")]), _block("b2", [_image("i2")])]
    out = _list_images(_blocks_handler(blocks), "aw-cris", "last_block", None, [], "any")
    assert [b["block_id"] for b in out["blocks"]] == ["b2"]
    assert [i["id"] for i in out["images"]] == ["i2"]


def test_scope_block_keeps_only_the_named_one():
    blocks = [_block("b1", [_image("i1")]), _block("b2", [_image("i2")])]
    out = _list_images(_blocks_handler(blocks), "aw-cris", "block", "b1", [], "any")
    assert [i["id"] for i in out["images"]] == ["i1"]


def test_scope_since_block_is_inclusive_and_ordered():
    blocks = [_block("b1", [_image("i1")]), _block("b2", [_image("i2")]),
              _block("b3", [_image("i3")])]
    out = _list_images(_blocks_handler(blocks), "aw-cris", "since_block", "b2", [], "any")
    assert [i["id"] for i in out["images"]] == ["i2", "i3"]


def test_scope_since_block_on_an_unknown_block_is_a_404_not_an_empty_list():
    with pytest.raises(gallery_mod.GalleryError) as exc:
        _list_images(_blocks_handler([_block("b1", [_image("i1")])]),
                     "aw-cris", "since_block", "nope", [], "any")
    assert exc.value.status == 404


def test_tags_filter_any_vs_all():
    """Tags arrive as {"id","name"} objects, not strings — the shape that only
    showed up once the legacy migration created real tags, by which point the
    string-only code had been "working" for months on empty lists."""
    blocks = [_block("b1", [
        _image("i1", tags=[{"id": "t1", "name": "Inverno"}]),
        _image("i2", tags=[{"id": "t1", "name": "Inverno"}, {"id": "t2", "name": "salto alto"}]),
        _image("i3", tags=[]),
    ])]
    any_hit = _list_images(_blocks_handler(blocks), "aw-cris", "all", None,
                           ["inverno", "salto alto"], "any")
    all_hit = _list_images(_blocks_handler(blocks), "aw-cris", "all", None,
                           ["inverno", "salto alto"], "all")
    assert [i["id"] for i in any_hit["images"]] == ["i1", "i2"]
    assert [i["id"] for i in all_hit["images"]] == ["i2"]
    # And the tag names survive as text, not as the raw {"id","name"} object.
    assert all_hit["images"][0]["tags"] == ["Inverno", "salto alto"]


def test_source_is_pushed_to_the_endpoint_as_a_query_param():
    seen: list[httpx.Request] = []
    blocks = [_block("b1", [_image("i1")], source="upload"),
              _block("b2", [_image("i2")], source="arvin")]
    out = _list_images(_blocks_handler(blocks, seen=seen), "aw-cris", "all", None, [],
                       "any", source="arvin")
    assert seen[0].url.params.get("source") == "arvin"
    assert [b["block_id"] for b in out["blocks"]] == ["b2"]


def test_the_response_carries_urls_and_ids_and_no_file_paths():
    """The contract PR-B is written against (PLAN.md §1.2). A `file_paths` key
    reappearing here would name files in a container nobody else can read."""
    out = _list_images(_blocks_handler([_block("b1", [_image("i1")])]),
                       "aw-cris", "all", None, [], "any")
    assert set(out) == {"blocks", "images", "image_urls"}
    assert out["images"][0]["id"] == "i1"
    assert out["images"][0]["block_id"] == "b1"
    assert out["image_urls"] == [out["images"][0]["url"]]
    assert set(out["blocks"][0]) == {"block_id", "created_at", "image_count",
                                     "source", "bot_slug"}


def test_a_bot_with_no_blocks_answers_empty_rather_than_raising():
    """A bot_slug nobody ever uploaded for is a legitimate question with an
    empty answer, on every scope including last_block."""
    for scope in ("all", "last_block"):
        out = _list_images(_blocks_handler([]), "who-dis", scope, None, [], "any")
        assert out == {"blocks": [], "images": [], "image_urls": []}
    assert _run(_blocks_handler([]),
                lambda c: gallery_mod.list_tags(c, BASE, "who-dis")) == {"tags": []}


# --------------------------------------------------------------------------
# list_tags
# --------------------------------------------------------------------------

def test_list_tags_counts_images_and_sorts_by_count():
    blocks = [_block("b1", [
        _image("i1", tags=[{"id": "t1", "name": "Inverno"}]),
        _image("i2", tags=[{"id": "t1", "name": "Inverno"}, {"id": "t2", "name": "Salto"}]),
    ])]
    out = _run(_blocks_handler(blocks), lambda c: gallery_mod.list_tags(c, BASE, "aw-cris"))
    assert out == {"tags": [{"name": "Inverno", "image_count": 2},
                            {"name": "Salto", "image_count": 1}]}


def test_an_ap_mt_error_keeps_its_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="not authenticated")

    with pytest.raises(gallery_mod.GalleryError) as exc:
        _run(handler, lambda c: gallery_mod.list_tags(c, BASE, "aw-cris"))
    assert exc.value.status == 401


def test_an_unreachable_ap_mt_is_502_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    with pytest.raises(gallery_mod.GalleryError) as exc:
        _run(handler, lambda c: gallery_mod.list_tags(c, BASE, "aw-cris"))
    assert exc.value.status == 502


# --------------------------------------------------------------------------
# set_tags — mint-then-tag
# --------------------------------------------------------------------------

def _tagging_handler(*, owned: set[str], mints: list, applied: list,
                     already: set = frozenset(), token: str = "gt-1",
                     blocks: list[dict] | None = None):
    """`blocks` is only consulted when the caller named no bot_slug — that is
    the path where set_tags has to discover who owns each image before it can
    mint the right (bot-scoped) token."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/gallery/blocks":
            return httpx.Response(200, json={"blocks": list(reversed(blocks or []))})
        if request.url.path == "/api/admin/gallery/token":
            mints.append(json.loads(request.content))
            return httpx.Response(200, json={"token": token, "created": False})
        parts = request.url.path.strip("/").split("/")
        assert parts[:2] == ["api", "gallery"] and parts[-1] == "tag"
        presented, image_id = parts[2], parts[4]
        if presented != token:
            return httpx.Response(401, text="invalid, expired, or revoked token")
        if image_id not in owned:
            return httpx.Response(404, text="image not found")
        name = json.loads(request.content)["name"]
        applied.append((image_id, name))
        return httpx.Response(200, json={"tag": {"id": "t", "name": name}, "created": True,
                                         "already_applied": (image_id, name) in already})
    return handler


def test_set_tags_mints_one_token_and_reuses_it(monkeypatch):
    monkeypatch.setattr(gallery_mod, "_TOKENS", {})
    mints, applied = [], []

    async def go(c):
        first = await gallery_mod.set_tags(c, BASE, "aw-cris", ["i1", "i2"],
                                           ["inverno", "salto"])
        await gallery_mod.set_tags(c, BASE, "aw-cris", ["i1"], ["inverno"])
        return first

    out = _run(_tagging_handler(owned={"i1", "i2"}, mints=mints, applied=applied), go)
    assert mints == [{"bot_slug": "aw-cris"}], "minting is per-process, not per-call"
    assert out == {"tagged_images": 2, "tags_applied": 4, "missing_image_ids": []}
    assert len(applied) == 5


def test_set_tags_reports_unowned_ids_without_losing_the_rest(monkeypatch):
    monkeypatch.setattr(gallery_mod, "_TOKENS", {})
    mints, applied = [], []
    out = _run(_tagging_handler(owned={"i1"}, mints=mints, applied=applied),
               lambda c: gallery_mod.set_tags(c, BASE, "aw-cris", ["i1", "ghost"], ["inverno"]))
    assert out == {"tagged_images": 1, "tags_applied": 1, "missing_image_ids": ["ghost"]}
    assert applied == [("i1", "inverno")]


def test_a_reapplied_tag_is_not_counted_again(monkeypatch):
    """`tags_applied` mirrors what the old `on conflict do nothing` INSERT
    reported: links actually created, not calls made."""
    monkeypatch.setattr(gallery_mod, "_TOKENS", {})
    mints, applied = [], []
    handler = _tagging_handler(owned={"i1"}, mints=mints, applied=applied,
                               already={("i1", "inverno")})
    out = _run(handler, lambda c: gallery_mod.set_tags(c, BASE, "aw-cris", ["i1"], ["inverno"]))
    assert out == {"tagged_images": 1, "tags_applied": 0, "missing_image_ids": []}


def test_a_stale_cached_token_is_reminted_and_the_tag_still_lands(monkeypatch):
    """An expired share-token answers 401, not 404 (AP-MT's
    `_get_valid_token`). Treating it as a missing image would report every
    image as unowned the day the cached token ages out."""
    monkeypatch.setattr(gallery_mod, "_TOKENS", {"aw-cris": "stale"})
    mints, applied = [], []
    out = _run(_tagging_handler(owned={"i1"}, mints=mints, applied=applied),
               lambda c: gallery_mod.set_tags(c, BASE, "aw-cris", ["i1"], ["inverno"]))
    assert mints == [{"bot_slug": "aw-cris"}]
    assert out == {"tagged_images": 1, "tags_applied": 1, "missing_image_ids": []}


def test_set_tags_without_a_bot_slug_resolves_each_images_owner(monkeypatch):
    """A GalleryToken is bot-scoped, so "no bot_slug" cannot mean "no token".
    The owner comes from the same listing the ids came from, and one token is
    minted per distinct bot."""
    monkeypatch.setattr(gallery_mod, "_TOKENS", {})
    mints, applied = [], []
    blocks = [_block("b1", [_image("i1")]), _block("b2", [_image("i2")])]
    blocks[1]["bot_slug"] = "cp-2"
    out = _run(_tagging_handler(owned={"i1", "i2"}, mints=mints, applied=applied,
                                blocks=blocks),
               lambda c: gallery_mod.set_tags(c, BASE, "", ["i1", "i2"], ["inverno"]))
    assert mints == [{"bot_slug": "aw-cris"}, {"bot_slug": "cp-2"}]
    assert out == {"tagged_images": 2, "tags_applied": 2, "missing_image_ids": []}


def test_an_unresolvable_id_is_missing_without_minting_anything(monkeypatch):
    monkeypatch.setattr(gallery_mod, "_TOKENS", {})
    mints, applied = [], []
    out = _run(_tagging_handler(owned=set(), mints=mints, applied=applied,
                                blocks=[_block("b1", [_image("i1")])]),
               lambda c: gallery_mod.set_tags(c, BASE, "", ["ghost"], ["inverno"]))
    assert out == {"tagged_images": 0, "tags_applied": 0, "missing_image_ids": ["ghost"]}
    assert mints == [] and applied == []


def test_set_tags_rejects_empty_inputs(monkeypatch):
    monkeypatch.setattr(gallery_mod, "_TOKENS", {})

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call AP-MT for an empty request")

    for ids, tags in (([], ["a"]), (["i1"], []), (["i1"], ["   "])):
        with pytest.raises(gallery_mod.GalleryError) as exc:
            _run(handler, lambda c, i=ids, t=tags: gallery_mod.set_tags(c, BASE, "aw-cris", i, t))
        assert exc.value.status == 400


# --------------------------------------------------------------------------
# The MCP layer — registration and dispatch, not the client
# --------------------------------------------------------------------------

def _call(monkeypatch, handler, name: str, args: dict) -> dict:
    """`_call_tool(name, args)`'s JSON payload, with AP-MT mocked out."""
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(mcp_server, "BASE", BASE)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = asyncio.run(mcp_server._call_tool(name, args))
    return json.loads(out[0].text)


def test_the_three_tools_are_registered_in_the_namespace(monkeypatch):
    """Registration lives in mcp_server.py's `static` list on purpose — the
    logic is in gallery.py, but a tool that isn't greppable here is a tool
    nobody can find."""
    monkeypatch.setattr(mcp_server, "_LAST_GOOD_AGENTS", [])
    monkeypatch.setattr(mcp_server, "_LAST_GOOD_WORKFLOWS", [])
    names = {t.name for t in asyncio.run(mcp_server._list_tools())}
    assert {"list_gallery_images", "list_gallery_tags", "set_gallery_tags"} <= names


def test_no_bot_slug_lists_every_gallery_this_workspace_owns(monkeypatch):
    """Every documented flow calls `list_gallery_images()` with no arguments,
    so the default decides whether the tool works at all. It must not be a
    hardcoded slug: the one this ports from ("aw-cris") matches nothing on
    this deployment, where all 345 blocks live under `cp-2` — verified live
    2026-09-21 — and would have answered 200 with an empty list. Omitting the
    filter is already tenant-scoped by the identity the call carries."""
    seen: list[httpx.Request] = []
    out = _call(monkeypatch, _blocks_handler([_block("b1", [_image("i1")])], seen=seen),
                "list_gallery_images", {})
    assert "bot_slug" not in seen[0].url.params
    assert out["images"][0]["id"] == "i1"
    assert out["blocks"][0]["bot_slug"] == "aw-cris", "which bot answered is part of the answer"


def test_an_explicit_bot_slug_still_narrows_to_that_bot(monkeypatch):
    seen: list[httpx.Request] = []
    _call(monkeypatch, _blocks_handler([_block("b1", [_image("i1")])], seen=seen),
          "list_gallery_images", {"bot_slug": "cp-2"})
    assert seen[0].url.params.get("bot_slug") == "cp-2"


def test_a_tag_query_defaults_to_scope_all_not_last_block(monkeypatch):
    """Tag filtering is inherently cross-block; defaulting to last_block would
    silently answer "no images with that tag" for every older upload."""
    blocks = [_block("b1", [_image("i1", tags=[{"id": "t1", "name": "inverno"}])]),
              _block("b2", [_image("i2")])]
    out = _call(monkeypatch, _blocks_handler(blocks),
                "list_gallery_images", {"tags": ["inverno"]})
    assert [i["id"] for i in out["images"]] == ["i1"]


def test_bad_arguments_are_a_400_tool_error_not_an_exception(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach AP-MT with invalid arguments")

    assert _call(monkeypatch, handler, "list_gallery_images",
                 {"match": "some"})["status"] == 400
    assert _call(monkeypatch, handler, "list_gallery_images",
                 {"scope": "since_block"})["status"] == 400


def test_an_ap_mt_failure_reaches_the_caller_as_a_tool_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="not authenticated")

    out = _call(monkeypatch, handler, "list_gallery_tags", {})
    assert out["error"] is True and out["status"] == 401


def test_set_gallery_tags_dispatches_to_the_mint_then_tag_path(monkeypatch):
    monkeypatch.setattr(gallery_mod, "_TOKENS", {})
    mints, applied = [], []
    out = _call(monkeypatch,
                _tagging_handler(owned={"i1"}, mints=mints, applied=applied,
                                 blocks=[_block("b1", [_image("i1")])]),
                "set_gallery_tags", {"image_ids": ["i1"], "tags": ["inverno"]})
    assert out == {"tagged_images": 1, "tags_applied": 1, "missing_image_ids": []}
