"""Regression tests for card bug:crispal-codex-oauth-token-not-persisted
(2026-09-04): the shared $CODEX_HOME/auth.json used to be populated once
(2026-08-23) and never re-synced ("copy only if missing"), with no lock
guarding concurrent workers and no visible signal when codex itself reports
the stored refresh_token was revoked — a dead token had no self-heal path
and cascaded silently, run after run, until a human happened to notice.

Three independent fixes, three groups of tests here:

1. `_resync_shared_cli_home_auth` (execute.py) re-syncs the shared
   auth.json from this run's staged login snapshot whenever content
   diverges, but never clobbers a shared copy that is already as fresh or
   fresher than the source.
2. That resync takes an flock on a sibling lockfile so two workers
   building a codex spawn at the same moment don't race a
   read-compare-write on the same file (AW_WORKSPACE_WORKERS > 1).
3. Both the cold path (execute.py::_stream_cold_attempt) and the warm
   relay (aw-warm-relay-codex.py::_run_codex_turn) detect codex's own
   refresh_token_invalidated/token_revoked signatures and surface them
   loudly instead of letting them cascade unnoticed.

Run: .venv/aw/bin/python -m pytest tests/test_codex_shared_home_auth.py
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

AUTH_REVOKED_ERROR = (
    'codex_login::auth::manager: Failed to refresh token: 401 Unauthorized '
    '... "message": "Your session has ended. Please log in again.", '
    '"code": "refresh_token_invalidated"'
)


# ---------------------------------------------------------------------------
# _resync_shared_cli_home_auth
# ---------------------------------------------------------------------------

def test_resync_populates_when_shared_copy_is_missing(tmp_path):
    shared = tmp_path / "codex-home"
    staged = tmp_path / "aw-creds"
    staged.mkdir()
    (staged / "auth.json").write_text('{"last_refresh": "2026-09-01T00:00:00Z"}')

    execute_mod._resync_shared_cli_home_auth(shared, staged)

    assert (shared / "auth.json").read_text() == '{"last_refresh": "2026-09-01T00:00:00Z"}'


def test_resync_overwrites_when_source_is_newer(tmp_path):
    shared = tmp_path / "codex-home"
    shared.mkdir()
    (shared / "auth.json").write_text('{"last_refresh": "2026-08-30T18:31:11Z"}')
    staged = tmp_path / "aw-creds"
    staged.mkdir()
    (staged / "auth.json").write_text('{"last_refresh": "2026-09-04T17:00:00Z"}')

    execute_mod._resync_shared_cli_home_auth(shared, staged)

    assert json.loads((shared / "auth.json").read_text())["last_refresh"] == "2026-09-04T17:00:00Z"


def test_resync_never_clobbers_a_fresher_shared_copy(tmp_path):
    """The exact failure this card diagnosed in reverse: a concurrent process
    (another codex container, an in-flight refresh) already wrote a NEWER
    token into the shared home than what this run staged from login — the
    stale staged copy must not overwrite it."""
    shared = tmp_path / "codex-home"
    shared.mkdir()
    (shared / "auth.json").write_text('{"last_refresh": "2026-09-04T18:00:00Z"}')
    staged = tmp_path / "aw-creds"
    staged.mkdir()
    (staged / "auth.json").write_text('{"last_refresh": "2026-08-30T18:31:11Z"}')

    execute_mod._resync_shared_cli_home_auth(shared, staged)

    assert json.loads((shared / "auth.json").read_text())["last_refresh"] == "2026-09-04T18:00:00Z"


def test_resync_falls_back_to_content_and_mtime_without_last_refresh_field(tmp_path):
    shared = tmp_path / "codex-home"
    shared.mkdir()
    (shared / "auth.json").write_text('{"access_token": "old"}')
    staged = tmp_path / "aw-creds"
    staged.mkdir()
    (staged / "auth.json").write_text('{"access_token": "new"}')
    # Ensure the staged copy is unambiguously the newer file on disk.
    now = time.time()
    import os
    os.utime(shared / "auth.json", (now - 10, now - 10))
    os.utime(staged / "auth.json", (now, now))

    execute_mod._resync_shared_cli_home_auth(shared, staged)

    assert (shared / "auth.json").read_text() == '{"access_token": "new"}'


def test_resync_is_a_noop_when_content_is_identical(tmp_path):
    shared = tmp_path / "codex-home"
    shared.mkdir()
    (shared / "auth.json").write_text('{"access_token": "same"}')
    staged = tmp_path / "aw-creds"
    staged.mkdir()
    (staged / "auth.json").write_text('{"access_token": "same"}')
    before = (shared / "auth.json").stat().st_mtime

    execute_mod._resync_shared_cli_home_auth(shared, staged)

    assert (shared / "auth.json").stat().st_mtime == before


def test_resync_never_raises_when_staged_source_is_missing(tmp_path):
    shared = tmp_path / "codex-home"
    staged = tmp_path / "aw-creds"
    staged.mkdir()  # no auth.json inside — nothing to sync from

    execute_mod._resync_shared_cli_home_auth(shared, staged)  # must not raise

    assert not (shared / "auth.json").exists()


def test_resync_serializes_concurrent_callers_via_flock(tmp_path):
    """Two 'workers' (threads standing in for two concurrent
    AW_WORKSPACE_WORKERS processes) racing the same resync must not
    interleave their read-compare-write — the lock must make each call
    atomic, so the shared file ends up holding exactly one caller's full,
    well-formed content rather than a torn mix of both."""
    shared = tmp_path / "codex-home"
    shared.mkdir()
    (shared / "auth.json").write_text('{"last_refresh": "2026-09-01T00:00:00Z", "pad": "%s"}'
                                       % ("x" * 2000))

    staged_a = tmp_path / "aw-creds-a"
    staged_a.mkdir()
    (staged_a / "auth.json").write_text('{"last_refresh": "2026-09-04T10:00:00Z", "pad": "%s"}'
                                         % ("a" * 2000))
    staged_b = tmp_path / "aw-creds-b"
    staged_b.mkdir()
    (staged_b / "auth.json").write_text('{"last_refresh": "2026-09-04T11:00:00Z", "pad": "%s"}'
                                         % ("b" * 2000))

    errors: list[Exception] = []

    def _call(staged):
        try:
            execute_mod._resync_shared_cli_home_auth(shared, staged)
        except Exception as e:  # pragma: no cover - failure path only
            errors.append(e)

    threads = [threading.Thread(target=_call, args=(staged_a,)),
               threading.Thread(target=_call, args=(staged_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # Whichever ran last, the result must be one caller's untouched content —
    # never a mix — and must always parse as valid JSON.
    final = json.loads((shared / "auth.json").read_text())
    assert final["last_refresh"] in ("2026-09-04T10:00:00Z", "2026-09-04T11:00:00Z")


# ---------------------------------------------------------------------------
# Cold path: _stream_cold_attempt logs the revocation loudly, once
# ---------------------------------------------------------------------------

class _FakeRedis:
    pass


class _FakeContainer:
    def __init__(self, lines: list[str], rc: int):
        self._chunks = [(l + "\n").encode() for l in lines]
        self._rc = rc

    def logs(self, stream=True, follow=True):
        return iter(self._chunks)

    def wait(self):
        return {"StatusCode": self._rc}


def test_cold_path_logs_auth_revoked_once(monkeypatch, caplog):
    monkeypatch.setattr(execute_mod, "_publish_line", lambda *_a, **_kw: None)
    lines = [AUTH_REVOKED_ERROR] * 5 + ["another unrelated cascade line"]
    container = _FakeContainer(lines, 1)
    with caplog.at_level(logging.WARNING, logger="aw_apps.agents_platform_runners.execute"):
        rc, contention_hit = execute_mod._stream_cold_attempt(
            container, _FakeRedis(), "run-auth-x", False)
    assert rc == 1
    assert contention_hit is False
    warnings = [r for r in caplog.records if "revoked/invalidated" in r.getMessage()]
    assert len(warnings) == 1, "must log exactly once even though codex repeats the line"
    assert "run-auth-x" in warnings[0].getMessage()


def test_cold_path_does_not_log_for_an_ordinary_run(monkeypatch, caplog):
    monkeypatch.setattr(execute_mod, "_publish_line", lambda *_a, **_kw: None)
    container = _FakeContainer(['{"type":"turn.completed"}'], 0)
    with caplog.at_level(logging.WARNING, logger="aw_apps.agents_platform_runners.execute"):
        execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-ok", False)
    assert not any("revoked/invalidated" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Warm relay: _run_codex_turn surfaces auth_revoked to its caller
# ---------------------------------------------------------------------------

RELAY_PATH = ROOT / "agent-images" / "shared" / "aw-warm-relay-codex.py"


def _load_relay():
    if "redis" not in sys.modules:
        stub = type(sys)("redis")
        stub.from_url = lambda *_a, **_kw: None
        sys.modules["redis"] = stub
    spec = importlib.util.spec_from_file_location("aw_warm_relay_codex_authtest", str(RELAY_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRelayRedis:
    def __init__(self):
        self.lines: list[str] = []

    def xadd(self, _key, fields, **_kw):
        if "line" in fields:
            self.lines.append(fields["line"])


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = [l + "\n" for l in lines]

    def __iter__(self):
        return iter(self._lines)


class _FakeProc:
    def __init__(self, lines: list[str], rc: int):
        self.stdout = _FakeStdout(lines)
        self._rc = rc

    def wait(self):
        return self._rc


def test_warm_relay_flags_auth_revoked(monkeypatch):
    relay = _load_relay()
    monkeypatch.setattr(relay, "aw_attach", None)
    monkeypatch.setattr(relay.subprocess, "Popen",
                        lambda *_a, **_kw: _FakeProc([AUTH_REVOKED_ERROR], 1))
    r = _FakeRelayRedis()
    rc, attempts, tail, auth_revoked = relay._run_codex_turn(
        ["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 1
    assert attempts == 1
    assert auth_revoked is True
    # Not the rollout-contention signature, so it streams live immediately —
    # still visible on the run's own event stream, not swallowed.
    assert r.lines == [AUTH_REVOKED_ERROR]


def test_warm_relay_does_not_flag_an_ordinary_turn(monkeypatch):
    relay = _load_relay()
    monkeypatch.setattr(relay, "aw_attach", None)
    monkeypatch.setattr(relay.subprocess, "Popen",
                        lambda *_a, **_kw: _FakeProc(['{"type":"turn.completed"}'], 0))
    r = _FakeRelayRedis()
    rc, attempts, tail, auth_revoked = relay._run_codex_turn(
        ["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert auth_revoked is False


# ---------------------------------------------------------------------------
# End to end: a real codex spawn actually resyncs the shared home
# ---------------------------------------------------------------------------

def test_build_container_kwargs_resyncs_shared_codex_home(tmp_path, monkeypatch):
    """A codex spawn whose login snapshot carries a NEWER auth.json than
    the shared, already-populated $CODEX_HOME must land that fresher copy
    into the shared home as part of building this run's container kwargs —
    the actual end-to-end path the card's bug lived in (the old guard only
    ever fired once, back on 2026-08-23, and never again)."""
    ws = tmp_path / "ws"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text('{"last_refresh": "2026-09-04T20:00:00Z"}')
    (codex_dir / "config.toml").write_text("")

    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))

    shared_home = ws / ".aw-workspace" / "data" / "agents-platform-runners" / "codex-home"
    shared_home.mkdir(parents=True)
    (shared_home / "auth.json").write_text('{"last_refresh": "2026-08-30T18:31:11Z"}')

    execute_mod._build_container_kwargs({"run_id": "r-resync", "cli": "codex", "prompt": "hi"})

    synced = json.loads((shared_home / "auth.json").read_text())
    assert synced["last_refresh"] == "2026-09-04T20:00:00Z"
