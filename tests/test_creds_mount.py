"""Regression test for the ~8h forced-relogin bug
(docs/knowledge_base/memory/aw-workspace-runner-claude-oauth-token-not-refreshed-relogin-20260807.md).

The Claude OAuth access token has an ~8h TTL and is only ever written by the
workspace's own login. The old code copied $HOME/.claude into the workspace
tree before every spawn and mounted .credentials.json READ-ONLY, so a token
refresh the spawned CLI performed could never persist back — auth silently
died every ~8h until a human re-logged in.

Fix (Frederico 2026-08-07, replicating agentic-workspace docker_agent.py):
when AW_WORKSPACE_HOME_HOST_DIR is known, mount the LIVE $HOME/.claude rw
DIRECTLY as the sibling container's ~/.claude — no copy, no read-only shadow —
so refreshes land on the source of truth. This test pins that contract and
that the legacy copy+RO fallback still applies when the host $HOME path is
unknown.

Run: .venv/aw/bin/python -m pytest tests/test_creds_mount.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


def _make_home(tmp_path: Path) -> Path:
    """A fake $HOME with a populated .claude creds dir + .claude.json."""
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
    (home / ".claude.json").write_text("{}")
    return home


def _volumes_for(job: dict) -> dict:
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    return kwargs["volumes"]


def test_direct_home_mount_is_rw_and_uncopied(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    home_host = "/host/aw-workspace-home"  # what the podman daemon sees for $HOME
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", home_host)
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    # A blanking resync must NOT happen in direct mode — fail loudly if called.
    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resync must not run in direct mode")))

    vols = _volumes_for({"run_id": "r1", "cli": "claude", "prompt": "hi"})

    # .claude mounted rw straight from the LIVE $HOME host path — no copy.
    claude_src = f"{home_host}/.claude"
    assert claude_src in vols, vols
    assert vols[claude_src] == {"bind": "/home/ubuntu/.claude", "mode": "rw"}

    # .claude.json also rw from the live host path.
    assert vols[f"{home_host}/.claude.json"] == {"bind": "/home/ubuntu/.claude.json", "mode": "rw"}

    # No read-only .credentials.json shadow anywhere — that's what blocked
    # refresh persistence.
    assert not any(m["mode"] == "ro" and m["bind"].endswith(".credentials.json")
                   for m in vols.values()), vols

    # No separate isolated mount: it lives inside the whole-.claude mount.
    assert not any(b == "/home/ubuntu/.claude/isolated/r1" for b in
                   (v["bind"] for v in vols.values()))


def test_oauth_token_injected_as_env(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    tok_file = tmp_path / "secrets" / "claude_code_oauth_token"
    tok_file.parent.mkdir(parents=True)
    tok_file.write_text("sk-ant-oat01-DURABLE\n")
    monkeypatch.setattr(execute_mod, "CLAUDE_OAUTH_TOKEN_FILE", str(tok_file))

    _img, _argv, kw, _mcp = execute_mod._build_container_kwargs({"run_id": "r3", "cli": "claude", "prompt": "hi"})
    # Trimmed, injected as env — durable auth that never blanks the creds file.
    assert kw["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-DURABLE"

    # Not injected for a non-claude CLI (they don't read this var).
    _img, _argv, kw2, _mcp2 = execute_mod._build_container_kwargs({"run_id": "r4", "cli": "codex", "prompt": "hi"})
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in kw2["environment"]


def test_fallback_copies_and_shadows_credentials_ro(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    ws = tmp_path / "ws"
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")  # unknown -> fallback
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))

    called = {"resync": False}

    def fake_resync(real_home, spec):
        called["resync"] = True
        # emulate the copy so the mount guards see a populated tree
        dst = ws / ".claude"
        dst.mkdir(parents=True, exist_ok=True)
        (dst / ".credentials.json").write_text("{}")
        (ws / ".claude.json").write_text("{}")

    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace", fake_resync)

    vols = _volumes_for({"run_id": "r2", "cli": "claude", "prompt": "hi"})

    assert called["resync"], "fallback must resync creds into the workspace tree"
    # .credentials.json mounted read-only in the fallback (blank-write guard).
    cred_src = f"/host/aw-workspace/.claude/.credentials.json"
    assert vols[cred_src] == {"bind": "/home/ubuntu/.claude/.credentials.json", "mode": "ro"}
