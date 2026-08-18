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

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

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


def board_base_url() -> str:
    """Where to reach the workspace API — see the module docstring.

    Published URL first, loopback last. This process is normally a stdio child
    of the gateway's container, where loopback is the gateway itself; a
    loopback-first order there doesn't fail loudly, it just talks to the wrong
    server.
    """
    return (os.environ.get("AW_LOCAL_API_URL")
            or os.environ.get(API_URL_VAR)
            or _from_env_file(API_URL_VAR)
            or f"http://127.0.0.1:{os.environ.get('AW_PORT', '9030')}").rstrip("/")


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


__all__ = ["BoardClient", "BoardUnavailable", "build_run_input", "dispatch_payload",
           "resume_payload", "board_base_url", "RUN_ID_PROPERTY", "DEFAULT_TARGET_SLUG"]
