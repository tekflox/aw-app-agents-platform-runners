"""Firing agents-platform runs from Notion Kanban cards.

This is the half of the monolith's ``src/api/routes/notion_kanban.py`` that
aw-app-notion deliberately refused. That app owns "a Notion database used as a
Kanban board" and does not talk to an orchestrator — its own module docstring
says reimplementing dispatch there would hardcode it to one. It was right, and
this is where the other half belongs: this app already *is* the orchestrator
client, so a card-to-run bridge here costs one new dependency (the board) in a
process that already has the harder one (the platform).

Split, concretely:

* the **board** — reading Ready cards, their body and comments, stamping the
  run id back onto the card — goes over the workspace API to aw-app-notion.
* the **dispatch** — finding an in-flight run, firing an agent or workflow,
  resuming a session — goes to agents-platform, the same ``BASE`` every other
  tool in this app uses.

**Where this actually runs matters.** Both apps are ``tier: inprocess``, which
makes it tempting to call the board over loopback — and wrong. This module is
imported by ``mcp_server.py``, which aw-mcp-gateway spawns as a *stdio child of
its own container*: ``127.0.0.1`` there is the gateway, not the workspace, and
the workspace's environment (including its API key) is not inherited. So the
address comes from ``AW_WORKSPACE_API_URL`` and the key is baked into the
upstream's env by ``plugin.build_mcp_servers``, the same way
``AGENTS_PLATFORM_TOKEN`` already is. Loopback stays as a last-resort fallback
for running this module inside the server (tests, a REPL), not as the norm.

Importing aw-app-notion's ``KanbanBoard`` directly would sidestep all of that
and is worse: it reaches into another app's object graph, and from the gateway
container it isn't even importable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

log = logging.getLogger("aw_apps.agents_platform_runners.kanban")

NOTION_PREFIX = "/api/apps/notion"
API_URL_VAR = "AW_WORKSPACE_API_URL"
API_KEY_VAR = "AW_WORKSPACE_API_KEY"
CONTAINER_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")

# Reading a card with body + comments is three Notion round trips behind one
# call, and Notion is paced at ~3 req/s — generous, not hot-path.
CARD_TIMEOUT_S = 60.0
DEFAULT_TIMEOUT_S = 30.0

# Where a card's run id is stamped so a human can jump from the card to the
# run. The monolith also wrote QARunId/QAAgent for qa-* agents; that stayed
# there with the QA flow and is not reproduced here.
RUN_ID_PROPERTY = "AgentRunId"

# The monolith's default when a card names no target. Kept identical: cards
# created before this port still rely on it.
DEFAULT_TARGET_SLUG = "system-investigations"

# Which side of the fence a claim token came from — see claim_card(). The
# monolith writes "mono:<uuid>"; only the prefix differs, so a human reading a
# stuck card can tell who was holding the lease when it stopped.
CLAIM_SIDE = "mt"

_SKILL_HINT = ("Load skill aw-kanban before acting on this card — it's the tool "
               "reference for the aw-kanban MCP (how to call the tools, the "
               "page_id from NOTION_TASK_ID).")


class BoardUnavailable(RuntimeError):
    """aw-app-notion could not be reached, or refused the call.

    Distinct from a platform error on purpose: "the board is unreachable" and
    "the run failed to start" are different outages with different owners, and
    a caller that can't tell them apart debugs the wrong one.
    """


def _env_file() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(CONTAINER_DIR, ".aw-workspace")
    return os.path.join(home, ".env")


def _from_env_file(name: str) -> str | None:
    try:
        with open(_env_file(), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def board_base_url(*, prefer_loopback: bool = False) -> str:
    """Where to reach the workspace API — see the module docstring.

    Published URL first, loopback last. This process is normally a stdio child
    of the gateway's container, where loopback is the gateway itself; a
    loopback-first order there doesn't fail loudly, it just talks to the wrong
    server.

    ``prefer_loopback`` inverts that for the one caller where the published URL
    is the wrong answer: the in-process watchdog (plugin.py) runs *inside* the
    workspace server, so the published URL would send its traffic out to the
    tunnel edge and back — and that edge cuts a request at ~30s, which is
    *shorter* than this module's own ``CARD_TIMEOUT_S``. A slow card read there
    dies at the edge rather than at the timeout that was sized for it.
    """
    loopback = f"http://127.0.0.1:{os.environ.get('AW_PORT', '9030')}"
    if prefer_loopback:
        return (os.environ.get("AW_LOCAL_API_URL") or loopback).rstrip("/")
    return (os.environ.get("AW_LOCAL_API_URL")
            or os.environ.get(API_URL_VAR)
            or _from_env_file(API_URL_VAR)
            or loopback).rstrip("/")


class BoardClient:
    """The narrow slice of aw-app-notion this module needs."""

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base = (base_url or "").rstrip("/") or None

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S) -> dict:
        base = self._base or board_base_url()
        key = os.environ.get(API_KEY_VAR) or _from_env_file(API_KEY_VAR)
        if not key:
            raise BoardUnavailable(
                f"{API_KEY_VAR} is not set for this MCP upstream. It is baked into the "
                "stdio env by the app's plugin.build_mcp_servers() — an upstream missing "
                "it was registered by an older version of this app, so restart the "
                "workspace (which rewrites mcp.json) and then the mcp-gateway.")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Api-Key": key}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 404:
                raise BoardUnavailable(
                    "aw-app-notion is not installed or its kanban routes are missing "
                    f"({detail})") from None
            raise BoardUnavailable(f"aw-app-notion returned {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise BoardUnavailable(
                f"could not reach aw-app-notion at {base}: {exc.reason}") from None
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            raise BoardUnavailable(
                f"aw-app-notion returned a non-JSON body: {raw[:200]!r}") from None

    def ready_cards(self, status: str = "ready", limit: int = 50) -> list[dict]:
        query = urllib.parse.urlencode({"status": status, "limit": limit})
        body = self._request("GET", f"{NOTION_PREFIX}/kanban/cards?{query}")
        if isinstance(body, dict) and body.get("ok") is False:
            raise BoardUnavailable(body.get("error") or "board refused the card query")
        cards = body.get("cards") if isinstance(body, dict) else body
        return cards if isinstance(cards, list) else []

    def card(self, page_id: str, *, with_content: bool = True) -> dict:
        query = urllib.parse.urlencode({
            "include_body": str(with_content).lower(),
            "include_comments": str(with_content).lower()})
        return self._request("GET", f"{NOTION_PREFIX}/kanban/cards/{page_id}?{query}",
                             timeout=CARD_TIMEOUT_S)

    def move(self, page_id: str, status: str) -> dict:
        return self._request("POST", f"{NOTION_PREFIX}/kanban/move",
                             {"page_id": page_id, "status": status})

    def set_property(self, page_id: str, name: str, value) -> dict:
        return self._request("POST", f"{NOTION_PREFIX}/kanban/set-property",
                             {"page_id": page_id, "property": name, "value": value})

    def properties(self, page_id: str, names: list[str] | None = None) -> dict:
        """``{property_name: value}`` — the read half of ``set_property``.

        ``card()``'s summary deliberately carries a fixed set of fields, and
        ``AgentRunId`` is not one of them; the claim read-back needs the raw
        property, so it comes from here.
        """
        query = {"page_id": page_id}
        if names:
            query["properties"] = ",".join(names)
        body = self._request("GET",
                             f"{NOTION_PREFIX}/kanban/properties?{urllib.parse.urlencode(query)}")
        if isinstance(body, dict) and body.get("ok") is False:
            raise BoardUnavailable(body.get("error") or "board refused the property read")
        return body if isinstance(body, dict) else {}


def build_run_input(card: dict) -> str:
    """The dispatch prompt, assembled the way the monolith assembled it.

    Body first because it is the task; comments after because they are the
    history that makes the task make sense; the skill hint last so the agent
    loads the tool reference instead of guessing at REST endpoints — which is
    the specific failure that put that line in the monolith.
    """
    page_id = card.get("page_id", "")
    title = card.get("title") or "(untitled card)"
    body = (card.get("body_md") or "").strip()
    comments = (card.get("comments_md") or "").strip()
    parts = [
        f'Kanban card: "{title}"',
        f"page_id={page_id}",
        "(same value as your $NOTION_TASK_ID env var — use either)",
        f"Task content:\n\n{body or f'Run task: {title}'}",
    ]
    if comments:
        parts.append(f"Comment history (oldest → newest):\n\n{comments}")
    parts.append(_SKILL_HINT)
    return "\n\n".join(parts)


def dispatch_payload(card: dict, input_text: str) -> tuple[str, dict] | None:
    """``(path, body)`` for the platform call, or None when the card names
    neither an agent nor a workflow — which is not an error, just a card that
    isn't dispatchable yet."""
    agent_slug = (card.get("agent_slug") or "").strip()
    workflow_slug = (card.get("workflow_slug") or "").strip()
    if not agent_slug and not workflow_slug:
        return None
    payload = {
        "input": {"input": input_text},
        "target_slug": (card.get("target_slug") or "").strip() or DEFAULT_TARGET_SLUG,
        "notion_task_id": card.get("page_id", ""),
    }
    path = (f"/api/agents/{agent_slug}/run" if agent_slug
            else f"/api/workflows/{workflow_slug}/run")
    return path, payload


def resume_payload(run: dict, page_id: str, message: str) -> tuple[str, dict] | str:
    """``(path, body)`` to resume the card's session, or a string saying why
    it can't be resumed. A card whose last run has no session is a real,
    explainable state — not an exception."""
    agent_slug = (run.get("source_slug") or "").strip()
    session_id = (run.get("session_id") or "").strip()
    if not agent_slug:
        return "the latest run for this card has no source_slug (agent) to resume"
    if not session_id:
        return ("the latest run for this card has no session_id — it never opened a "
                "resumable CLI session, so there is nothing to send a message to")
    payload = {"input": {"input": message}, "session_id": session_id,
               "notion_task_id": page_id}
    if run.get("target_id"):
        payload["target_id"] = run["target_id"]
    return f"/api/agents/{agent_slug}/run", payload


def claim_card(board: BoardClient, page_id: str, *, side: str = CLAIM_SIDE,
               token: str | None = None) -> str | None:
    """Lease this card for one dispatcher. Returns the winning token, or None.

    Two dispatchers watch this board — the monolith's Notion webhook and this
    app's sweep — and they read two *different* run databases, so neither can
    see the other's in-flight runs. "Is a run already going?" is therefore blind
    across the fence; the only signal both sides share is the card itself.

    Status alone isn't enough. Both sides write "In Progress" before firing,
    which handles a late duplicate delivery, but it is still check-then-act: if
    both GETs land before either PATCH, both fire. So instead of asking, each
    side *claims* — write a unique token into ``AgentRunId``, read it straight
    back, and only the one that reads its own token proceeds. Notion's page
    patch is last-write-wins and the following GET returns exactly one value, so
    exactly one side can win. It's a lease, not a timing heuristic.

    It fails **closed**: a stale or failed read makes *both* sides stand down,
    which leaves the card in Ready — visible on the board, and reclaimed by the
    next tick 60s later. The alternative failure (two runs on one card) is the
    one that costs money and confuses the human reading the card.

    ``AgentRunId`` is reused rather than a new property because both sides
    already write it today, and the value it settles on is still the real run
    id — the lease is only what lives there for the second or two in between.
    """
    token = token or f"{side}:{uuid.uuid4()}"
    board.set_property(page_id, RUN_ID_PROPERTY, token)
    current = board.properties(page_id, [RUN_ID_PROPERTY]).get(RUN_ID_PROPERTY)
    if str(current or "").strip() != token:
        log.info("kanban sweep: lost the claim on %s (holder=%r)", page_id, current)
        return None
    return token


class PlatformClient:
    """The narrow slice of agents-platform the sweep needs.

    Wraps a caller-supplied ``httpx.AsyncClient`` instead of owning one, so the
    ``run_ready_cards`` tool hands over the very client — and identity-JWT auth
    headers — every other tool in ``mcp_server`` already uses, and the watchdog
    builds its own.
    """

    def __init__(self, client, base: str) -> None:
        self._c = client
        self._base = base.rstrip("/")

    async def latest_run(self, page_id: str) -> dict | None:
        try:
            resp = await self._c.get(f"{self._base}/api/runs",
                                     params={"notion_task_id": page_id, "limit": 1})
        except Exception:
            log.warning("kanban sweep: run lookup failed for %s", page_id, exc_info=True)
            return None
        if resp.status_code != 200:
            return None
        runs = resp.json() or []
        return runs[0] if runs else None

    async def dispatch(self, path: str, payload: dict) -> tuple[str, str]:
        """``(run_id, error)`` — exactly one of the two is non-empty."""
        try:
            resp = await self._c.post(f"{self._base}{path}", json=payload)
        except Exception as exc:
            return "", f"dispatch failed: {exc}"
        if resp.status_code != 200:
            return "", f"dispatch failed: {resp.status_code} {resp.text[:200]}"
        run = resp.json() or {}
        return str(run.get("run_id") or run.get("id") or ""), ""


async def sweep_ready(board: BoardClient, platform: PlatformClient, *,
                      status: str = "ready", limit: int = 50,
                      dry_run: bool = False, claim_side: str = CLAIM_SIDE) -> dict:
    """Dispatch every Ready card that isn't already claimed or in flight.

    The single implementation behind both triggers: the ``run_ready_cards`` MCP
    tool (a human asking for a catch-all pass) and the in-process watchdog
    (plugin.py, every ``kanban_sweep_interval_s``). Deliberately one function —
    a second copy of this without the claim would put the double-fire back
    through the door the claim exists to close.

    Per card, and try/except per card so one bad card never ends the pass:
      1. skip if this platform already has a pending/running run for it — cheap,
         and the common case for a board the other side just dispatched from;
      2. re-read the card. Its body and comments are the prompt, and its status
         is the confirmation that it is still Ready and not something the other
         side moved out from under us between the list and now;
      3. skip cards naming neither an agent nor a workflow — that's a card
         someone hasn't finished filling in, not an error;
      4. claim it (see claim_card) — nothing is written before this point;
      5. move to running, *then* dispatch. That order is the monolith's and it
         is deliberate: a card left in Ready after a good dispatch gets swept
         again and shows up as a duplicate; a card moved to running after a
         failed dispatch just sits there silently. Sweeping again is the louder
         and cheaper of the two wrongs;
      6. stamp the real run id over the claim token.
    """
    cards = await asyncio.to_thread(board.ready_cards, status, limit)
    results: list[dict] = []

    for summary in cards:
        page_id = summary.get("page_id") or ""
        row: dict = {"page_id": page_id, "title": summary.get("title")}
        # The Notion display name of the Ready status, straight from a card the
        # board itself filtered as Ready — so the re-read below compares like
        # with like without this module having to know the status mapping.
        ready_name = summary.get("status")
        try:
            latest = await platform.latest_run(page_id)
            if latest and latest.get("status") in ("pending", "running"):
                results.append({**row, "skipped": "run-already-in-flight",
                                "run_id": latest.get("id")})
                continue

            card = await asyncio.to_thread(board.card, page_id)
            # list_cards' summary lacks body/comments; card() has them but is
            # one page's worth. Merge so the prompt sees everything.
            card = {**summary, **card}
            if ready_name and card.get("status") != ready_name:
                results.append({**row, "skipped": "no-longer-ready",
                                "status": card.get("status")})
                continue

            planned = dispatch_payload(card, build_run_input(card))
            if planned is None:
                results.append({**row, "skipped": "card names no agent_slug or workflow_slug"})
                continue
            path, payload = planned
            if dry_run:
                results.append({**row, "would_dispatch": path,
                                "target_slug": payload["target_slug"]})
                continue

            if await asyncio.to_thread(claim_card, board, page_id, side=claim_side) is None:
                results.append({**row, "skipped": "claim-lost"})
                continue

            await asyncio.to_thread(board.move, page_id, "running")
            run_id, error = await platform.dispatch(path, payload)
            if error:
                results.append({**row, "error": error})
                continue
            if run_id:
                try:
                    await asyncio.to_thread(board.set_property, page_id,
                                            RUN_ID_PROPERTY, run_id)
                except BoardUnavailable as exc:
                    # The run is already flying; a missing back-reference is a
                    # navigation annoyance, not a reason to report failure.
                    row["run_id_stamp_failed"] = str(exc)
            results.append({**row, "run_id": run_id,
                            "target_slug": payload["target_slug"]})
        except BoardUnavailable as exc:
            results.append({**row, "error": f"board unavailable — {exc}"})
        except Exception as exc:  # pragma: no cover - defensive, per card
            log.warning("kanban sweep: card %s failed", page_id, exc_info=True)
            results.append({**row, "error": str(exc)})

    dispatched = sum(1 for r in results if r.get("run_id") and not r.get("skipped"))
    return {"considered": len(results), "dispatched": dispatched,
            "dry_run": dry_run, "results": results}


__all__ = ["BoardClient", "BoardUnavailable", "PlatformClient", "build_run_input",
           "dispatch_payload", "resume_payload", "board_base_url", "claim_card",
           "sweep_ready", "RUN_ID_PROPERTY", "DEFAULT_TARGET_SLUG", "CLAIM_SIDE"]
