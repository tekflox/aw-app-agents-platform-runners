"""recycle_session — the warm-container half.

Written around the finding that motivated the tool, measured live on
2026-08-25: an agent session sat at ZERO MCP tools for hours while
`aw-workspace-cli doctor` reported the gateway serving 686 tools across 33
upstreams, and raw JSON-RPC from that same container returned the full tool
list. Gateway healthy, network fine, token valid, client dead.

The obvious implementation is "restart the container", and the obvious
implementation is not enough: killing the CLI DID recycle the container
(hostname went 899e17d1428b -> f7537fe56a9e) and the client was still dead
in the new one. So the assumption every reader will make — recycling the
container restores tool access — is exactly the one that needs a test
holding it down.

What a container recycle does and does not change is therefore pinned here
explicitly: it replaces the PROCESS, and it re-resolves nothing about the
session it resumes or the config it is handed. Those are the two places the
failure can hide, and no amount of container recycling reaches either.

Run: .venv/aw/bin/python -m pytest tests/test_recycle_session.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app import warm_pool  # noqa: E402


class _FakeContainer:
    def __init__(self, name: str, log: list):
        self.name = name
        self._log = log

    def rename(self, new_name: str) -> None:
        self._log.append(("rename", self.name, new_name))
        self.name = new_name

    def remove(self, force: bool = False) -> None:
        self._log.append(("remove", self.name, force))


class _FakeContainers:
    def __init__(self, existing: dict, log: list):
        self._existing = existing
        self._log = log

    def get(self, name: str):
        if name not in self._existing:
            raise KeyError(name)
        return _FakeContainer(name, self._log)

    def run(self, image, **kwargs):
        self._log.append(("run", kwargs.get("name"), image))
        return _FakeContainer(kwargs["name"], self._log)


class _FakeClient:
    def __init__(self, existing: dict, log: list):
        self.containers = _FakeContainers(existing, log)


@pytest.fixture
def warm(monkeypatch):
    """A live, epoch-matching, running warm container — i.e. the exact state
    get_or_create would normally reuse without a second thought."""
    log: list = []
    name = warm_pool.warm_container_name("agent-1", "sess-1")
    client = _FakeClient({name: True}, log)
    monkeypatch.setattr(warm_pool, "_labels",
                        lambda _c, _n: {warm_pool.EPOCH_LABEL: "epoch1"})
    monkeypatch.setattr(warm_pool, "_is_running", lambda _c, _n: True)
    monkeypatch.setattr(warm_pool, "_wait_ready", lambda _c, _n, timeout_s=10.0: None)
    monkeypatch.setattr(warm_pool, "drain", lambda _c, _n: log.append(("drain", _n)))
    return client, log, name


def _get_or_create(client, recycle=None):
    return warm_pool.get_or_create(
        client=client, agent_id="agent-1", session_id="sess-1", epoch_hash="epoch1",
        build_kwargs=lambda _name, _epoch: ("img", {}), recycle=recycle,
    )


# --------------------------------------------------------------------------
# The levels actually do something different from an ordinary turn
# --------------------------------------------------------------------------

def test_ordinary_turn_reuses_the_container(warm):
    """Baseline. Without this, a test asserting "recycle spawns a fresh one"
    could pass while EVERY turn spawned a fresh one, which would quietly
    delete the entire point of a warm pool."""
    client, log, name = warm
    assert _get_or_create(client) == name
    assert log == []


def test_reconnect_mcp_replaces_a_healthy_container(warm):
    """The cheapest level. An MCP client is constructed once, at CLI start,
    and nothing re-initialises it for that container's whole 6h life — so a
    new PROCESS is the only lever there is, even though this container is
    running and its epoch matches."""
    client, log, name = warm
    assert _get_or_create(client, recycle="drain") == name
    kinds = [e[0] for e in log]
    assert "rename" in kinds and "run" in kinds
    assert "remove" not in kinds, "the graceful level must not force-remove"


def test_recycle_container_removes_it_immediately(warm):
    """The harder level, for a process too wedged to notice a drain flag.
    Distinct from `drain` in mechanism, identical in what it preserves."""
    client, log, name = warm
    assert _get_or_create(client, recycle="force") == name
    assert ("remove", name, True) in log
    assert ("run", name, "img") in log


def test_force_falls_back_to_drain_when_removal_fails(warm, monkeypatch):
    """A recycle that cannot remove the old container must still hand back a
    usable one — the caller is a session with no working tools, and leaving
    it on the broken container is the one outcome with no recovery path."""
    client, log, name = warm

    def _boom(self, force=False):
        raise RuntimeError("engine said no")

    monkeypatch.setattr(_FakeContainer, "remove", _boom)
    assert _get_or_create(client, recycle="force") == name
    assert ("run", name, "img") in log


# --------------------------------------------------------------------------
# The negative result — what a container recycle does NOT fix
# --------------------------------------------------------------------------

def test_a_recycled_container_still_resumes_the_same_session(tmp_path, monkeypatch):
    """THE regression test on this card.

    Recycling the container was done by hand on 2026-08-25 and did not fix
    the outage. This is why: the replacement resumes the very same session,
    so anything carried in that session's own state survives the recycle
    untouched. Only `fresh_session` — which drops --resume, and loses the
    conversation — changes this, and that is precisely why it is marked
    destructive instead of being the silent default.
    """
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    job = {"run_id": "r1", "cli": "claude", "agent_id": "agent-1",
           "session_id": "sess-1", "prompt": "hi", "warm_recycle": "force"}
    _image, kwargs = execute_mod._build_warm_kwargs(job, "epoch2", "redis://x:6379/0")
    argv = kwargs["command"]
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-1"
    assert "--session-id" not in argv


def test_a_recycled_container_gets_whatever_mcp_config_it_is_handed(tmp_path, monkeypatch):
    """The other place the failure hides, and the one that reproduces every
    measured symptom of 2026-08-25.

    A config carrying a bad token (or a bad url) is written out again,
    verbatim, on every single fresh spawn — so all three levels inherit it
    and none of them can fix it. Reproduced by hand with the real CLI: a
    reachable gateway plus a stale token yields `status: failed`, 0 tools,
    while raw JSON-RPC with the workspace's own token returns HTTP 200 —
    indistinguishable, from inside a toolless session, from a dead client.
    That is what recycle_session's `preflight` exists to tell apart.
    """
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    bad = {"aw-gateway": {"type": "http", "url": "http://gw:9200/mcp",
                          "headers": {"Authorization": "Bearer stale"}}}
    job = {"run_id": "r2", "cli": "claude", "agent_id": "agent-1",
           "session_id": "sess-1", "prompt": "hi", "warm_recycle": "force",
           "mcp_servers": bad}
    _image, kwargs = execute_mod._build_warm_kwargs(job, "epoch2", "redis://x:6379/0")
    argv = kwargs["command"]
    cfg_path = next(p for p in (
        Path(execute_mod.REAL_HOME) / ".claude/isolated/r2/mcp.json",
        tmp_path / "ws/.claude/isolated/r2/mcp.json") if p.is_file())
    written = json.loads(cfg_path.read_text())
    assert written["mcpServers"]["aw-gateway"]["headers"]["Authorization"] == "Bearer stale"
    assert "--mcp-config" in argv, "the recycled container is handed the same bad config"


# --------------------------------------------------------------------------
# The durable record
# --------------------------------------------------------------------------

def test_recycle_is_written_to_a_durable_file(tmp_path, monkeypatch):
    """An agent that recycles itself cannot report its own result — it is
    gone by the time the recycle happens. On 2026-08-25 a hand-written log
    was the only reason the outcome of the manual escalation was knowable at
    all, so the tool owns that file rather than leaving it to the caller."""
    monkeypatch.setattr(execute_mod, "RECYCLE_LOG_DIR", tmp_path / "recycle-session")
    job = {"run_id": "r3", "session_id": "sess-1", "agent_id": "agent-1",
           "warm_recycle": "force", "mcp_servers": {"aw-gateway": {"url": "http://gw"}}}
    execute_mod._log_recycle(job, container="aw-warm-agent-1-sess-1")
    rec = json.loads((tmp_path / "recycle-session" / "sess-1.jsonl").read_text().strip())
    assert rec["level"] == "force"
    assert rec["run_id"] == "r3"
    assert rec["container"] == "aw-warm-agent-1-sess-1"
    assert rec["mcp_servers"] == ["aw-gateway"]


def test_logging_failure_never_breaks_the_recycle(monkeypatch):
    """Reporting is not the job. A recycle whose bookkeeping cannot be
    written must still happen — the caller has no working tools."""
    monkeypatch.setattr(execute_mod, "RECYCLE_LOG_DIR", Path("/proc/nonexistent/nope"))
    execute_mod._log_recycle({"run_id": "r4", "session_id": "s"}, container="c")


# --------------------------------------------------------------------------
# The tool surface — what the calling agent is actually told
# --------------------------------------------------------------------------
#
# The backend (agents-platform-multitenant, POST /api/sessions/{sid}/recycle)
# auto-arms a wake-up for the two SAFE levels, because a queued recycle is
# only ever applied on a NEXT turn and an agent recycling itself mid-task has
# nothing to guarantee one happens. That outcome rides back as a `wakeup`
# field, and the tool's `applies` line — which used to promise a next turn
# unconditionally — has to reflect it, or the tool goes on making exactly the
# claim this feature exists to stop being false.

#: The pristine class, captured before anything patches it. The patch below
#: lands on the httpx MODULE (mcp_server just does `import httpx`), so a
#: helper that re-read httpx.AsyncClient would stack its second stub on top
#: of its first and keep serving the first test case's response.
_REAL_ASYNC_CLIENT = None


def _call_recycle(monkeypatch, wakeup, level="reconnect_mcp"):
    """Drive the recycle_session handler against a stubbed AP-MT."""
    import asyncio

    import httpx

    from agents_platform_runners_app import mcp_server

    global _REAL_ASYNC_CLIENT
    if _REAL_ASYNC_CLIENT is None:
        _REAL_ASYNC_CLIENT = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recycle") and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-1", "level": level,
                                             "status": "queued", "attempt": {},
                                             "wakeup": wakeup})
        if request.url.path.endswith("/recycle"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.url}")

    def fake(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*a, **kw)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake)
    out = asyncio.run(mcp_server._call_tool(
        "recycle_session", {"session_id": "sess-1", "level": level}))
    return json.loads(out[0].text)


def test_the_tool_surfaces_the_wakeup_the_backend_reports(monkeypatch):
    """Pass-through, not re-derivation: only the backend knows whether the
    wake-up was armed, deduped or refused, and the agent has to be able to
    read all three."""
    wk = {"armed": True, "wakeup_id": "wk-1", "delay_seconds": 60,
          "fire_at": "2026-09-04T09:00:00", "channel": "telegram"}
    out = _call_recycle(monkeypatch, wk)
    assert out["wakeup"] == wk


def test_applies_promises_a_next_turn_only_when_one_is_armed(monkeypatch):
    """The whole point of the card. With a wake-up armed the agent can end
    its turn and come back recycled; without one, 'applied before your NEXT
    turn' is a promise nothing is going to keep, and saying it anyway is how
    a self-healing agent strands itself waiting."""
    armed = _call_recycle(monkeypatch, {"armed": True, "wakeup_id": "wk-1",
                                        "delay_seconds": 60})["applies"]
    refused = _call_recycle(monkeypatch, {
        "armed": False,
        "reason": "You are not allowed to Wake-up, talk to the user about it"})["applies"]

    assert armed != refused
    assert "wake-up is armed 60s from now" in armed
    assert "NO wake-up is armed" in refused
    # The refusal reason reaches the agent verbatim rather than being
    # flattened into "something went wrong".
    assert "You are not allowed to Wake-up" in refused


def test_fresh_session_is_told_no_wakeup_is_coming(monkeypatch):
    """fresh_session deliberately gets none. It must still read as a stated
    decision with a consequence, not as a silent absence."""
    out = _call_recycle(monkeypatch,
                        {"armed": False, "reason": "fresh_session loses the "
                                                   "conversation, so no wake-up is armed "
                                                   "for it"},
                        level="fresh_session")
    assert "NO wake-up is armed" in out["applies"]
    assert "DESTRUCTIVE" in out["warning"]


def test_a_backend_that_reports_no_wakeup_field_does_not_break_the_tool(monkeypatch):
    """Backend-first is the rollout order, but the tool must not explode if it
    ever meets an older platform that predates the field."""
    out = _call_recycle(monkeypatch, None)
    assert "NO wake-up is armed" in out["applies"]
    assert out["status"] == "queued"


def test_a_deduped_wakeup_is_not_reported_as_no_wakeup(monkeypatch):
    """schedule_wakeup dedups by origin run, so an agent that already armed
    its own wake-up this run gets that one back instead of a second. A turn
    IS coming — telling it 'nothing will trigger a next turn' would be as
    wrong as the unconditional promise this replaced, just in the other
    direction. What it does need to know is that the delay is its own, not
    the 60s this feature picked."""
    out = _call_recycle(monkeypatch, {"armed": False, "duplicate": True,
                                      "existing_wakeup_id": "wk-mine",
                                      "reason": "a wake-up is already armed for run r1"})
    assert "NO wake-up is armed" not in out["applies"]
    assert "already had" in out["applies"]
    assert out["wakeup"]["existing_wakeup_id"] == "wk-mine"
