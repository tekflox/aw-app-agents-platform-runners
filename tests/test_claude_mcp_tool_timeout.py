"""A warm or cold claude Runner container's --mcp-config for `aw-gateway` had
no `timeout` field, so Claude Code's own client-side default (MCP_TOOL_TIMEOUT,
60s) killed every approval-gated tool call before a human could plausibly
respond. Confirmed on card 406c4132-9691-81e4-b587-d447bad1f527: `aw-crispal`'s
`backup_database` (two Telegram approval taps + up to a 300s key-vault poll +
a ~650MB mysqldump/rsync) died with "The operation timed out" at exactly
60.006s and 60.005s on two separate attempts, and `backup_status` afterwards
confirmed no dump was produced either time — the call never got far enough to
do any work, let alone anything destructive.

`execute.py::_build_container_kwargs` writes this file once, for `claude`
only (`CLI_SPECS["claude"]["mcp_config_flag"] == "--mcp-config"`; every other
CLI's spec sets it to `None`, so this whole block is skipped for them). The
fix adds a generous per-server `timeout` (ms) to every entry in the generated
`mcpServers` dict — scoped to this one dict literal in execute.py, nothing
else.

Run: python3 -m pytest tests/test_claude_mcp_tool_timeout.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

SERVERS = {
    "aw-gateway": {
        "type": "streamable-http",
        "url": "http://aw-app-mcp-gateway:9200/mcp",
        "headers": {"Authorization": "Bearer live-gateway-token"},
    }
}

BASE_JOB = {
    "run_id": "run-turn-1", "cli": "claude", "agent_id": "agent-1", "prompt": "hi",
    "mcp_servers": SERVERS,
}


def _write_claude_mcp_json(tmp_path, monkeypatch, job=BASE_JOB) -> dict:
    home = tmp_path / "home"  # empty — no .claude dir, so direct_home_mount
    home.mkdir()               # stays False and mcp.json lands under WORKSPACE_CONTAINER_DIR
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))

    execute_mod._build_container_kwargs(job)

    mcp_path = tmp_path / "ws" / ".claude" / "isolated" / job["run_id"] / "mcp.json"
    return json.loads(mcp_path.read_text())


def test_claude_mcp_json_carries_a_generous_per_server_timeout(tmp_path, monkeypatch):
    doc = _write_claude_mcp_json(tmp_path, monkeypatch)
    server = doc["mcpServers"]["aw-gateway"]

    # 30 minutes — covers the documented worst case (two Telegram approval
    # taps, a 300s key-vault poll, then a ~650MB mysqldump+rsync) with room
    # to spare, and is well past Claude Code's own 60s default.
    assert server["timeout"] == 1_800_000
    assert server["url"] == "http://aw-app-mcp-gateway:9200/mcp"
    assert server["headers"]["Authorization"] == "Bearer live-gateway-token"


def test_timeout_applies_to_every_configured_server(tmp_path, monkeypatch):
    job = dict(BASE_JOB, mcp_servers={
        **SERVERS,
        "other-mcp": {"type": "streamable-http", "url": "http://other:9999/mcp"},
    })
    doc = _write_claude_mcp_json(tmp_path, monkeypatch, job=job)

    assert doc["mcpServers"]["aw-gateway"]["timeout"] == 1_800_000
    assert doc["mcpServers"]["other-mcp"]["timeout"] == 1_800_000
