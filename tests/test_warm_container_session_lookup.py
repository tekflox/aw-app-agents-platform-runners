"""The MCP-layer join: a warm container's session_id -> what agents-platform
knows about that session (mcp_server._enrich_warm_containers).

Separate from test_list_warm_containers.py on purpose — that file fakes a
DOCKER client and covers warm_pool.list_containers; this one fakes the
PLATFORM client. Both used to be called _FakeClient, and defining the second
in the same module silently shadowed the first for every test above it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents_platform_runners_app.mcp_server import _enrich_warm_containers  # noqa: E402

# ---------------------------------------------------------------------------
# session_id -> platform translation (_enrich_warm_containers).
#
# The labels answer "which session", not "whose work". The model's own
# params.cli is what the platform dispatches from, so it outranks the
# entrypoint guess — but never the label, which needs no network at all.
# ---------------------------------------------------------------------------





class _FakePlatformResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakePlatformClient:
    """Answers /api/runs and /api/models; raises whatever `boom` is set to."""

    def __init__(self, runs_by_session=None, models=None, boom=None):
        self.runs_by_session = runs_by_session or {}
        self.models = models or []
        self.boom = boom
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        if self.boom:
            raise self.boom
        if url.endswith("/api/models"):
            return _FakePlatformResponse(self.models)
        sid = (params or {}).get("session_id")
        row = self.runs_by_session.get(sid)
        return _FakePlatformResponse([row] if row else [])


_MODELS = [{"slug": "claude-runner-opus", "params": {"cli": "claude"}},
           {"slug": "codex-runner-gpt-5", "params": {"cli": "codex"}}]


def test_session_lookup_fills_cli_and_outranks_the_entrypoint_guess():
    payload = {"containers": [{"session_id": "s1", "cli": "claude", "cli_source": "inferred"}]}
    client = _FakePlatformClient(
        runs_by_session={"s1": {"id": "r1", "source_slug": "coder-sonnet", "status": "success",
                                "model_slug": "codex-runner-gpt-5", "target_slug": "t",
                                "started_at": "2026-08-30T00:00:00"}},
        models=_MODELS)
    asyncio.run(_enrich_warm_containers(client, payload))

    ct = payload["containers"][0]
    assert ct["cli"] == "codex"                 # the guess said claude; the platform disagrees
    assert ct["cli_source"] == "session_lookup"
    assert ct["session"]["agent_slug"] == "coder-sonnet"
    assert ct["session"]["last_run_status"] == "success"
    assert payload["session_lookup"] == {"ok": True, "resolved": 1, "of": 1}


def test_label_is_never_overridden_by_the_lookup():
    """The label needs no network and was written by the spawn itself."""
    payload = {"containers": [{"session_id": "s1", "cli": "claude", "cli_source": "label"}]}
    client = _FakePlatformClient(
        runs_by_session={"s1": {"id": "r1", "model_slug": "codex-runner-gpt-5"}},
        models=_MODELS)
    asyncio.run(_enrich_warm_containers(client, payload))

    assert payload["containers"][0]["cli"] == "claude"
    assert payload["containers"][0]["cli_source"] == "label"
    assert "session" in payload["containers"][0]   # still enriched with the rest


def test_platform_unreachable_degrades_instead_of_erroring():
    """An inventory that dies because the platform is down is useless exactly
    when it is most needed."""
    payload = {"containers": [{"session_id": "s1", "cli": None, "cli_source": "inferred"}]}
    client = _FakePlatformClient(boom=RuntimeError("connection refused"))
    asyncio.run(_enrich_warm_containers(client, payload))

    assert payload["session_lookup"]["ok"] is False
    assert "connection refused" in payload["session_lookup"]["reason"]
    assert payload["containers"][0]["cli_source"] == "inferred"   # untouched
    assert "session" not in payload["containers"][0]              # absent, not empty


def test_unknown_session_leaves_that_container_alone():
    payload = {"containers": [{"session_id": "ghost", "cli": None, "cli_source": "inferred"}]}
    client = _FakePlatformClient(runs_by_session={}, models=_MODELS)
    asyncio.run(_enrich_warm_containers(client, payload))

    assert "session" not in payload["containers"][0]
    assert payload["session_lookup"] == {"ok": True, "resolved": 0, "of": 1}


def test_no_session_ids_skips_the_lookup_entirely():
    payload = {"containers": [{"session_id": None}]}
    client = _FakePlatformClient()
    asyncio.run(_enrich_warm_containers(client, payload))

    assert client.calls == []
    assert payload["session_lookup"]["ok"] is True
