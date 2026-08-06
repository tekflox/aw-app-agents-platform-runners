"""Regression test for the MCP-servers-dropped-at-the-route bug
(docs/knowledge_base/memory/agents-platform-multitenant-runner-mcp-servers-dropped-at-execute-route-20260806.md).

execute_job() in routes.py used to rebuild the job dict through an explicit
field allowlist that silently omitted `mcp_servers` and
`dangerous_skip_permissions` before handing it to execute.start_job() —
so an AP-MT AgentConfig's MCP config had zero observable effect on the
spawned CLI. This test pins both fields to the job dict AND statically
checks every job.get("<key>") execute.py actually consumes is present in
routes.py's job dict literal, so a future refactor can't silently drop a
field again.

Run: .venv/aw/bin/python -m pytest tests/test_execute_job_forwarding.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


def test_execute_job_forwards_mcp_servers_and_dangerous_skip_permissions(monkeypatch):
    captured = {}

    def fake_start_job(job, redis_url):
        captured.update(job)

    monkeypatch.setattr(execute_mod, "start_job", fake_start_job)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "unix:///fake.sock")

    client = TestClient(build_routes({
        "execute_secret": "s3cr3t",
        "shared_redis_url": "redis://example.test:6379/0",
    }))
    resp = client.post(
        "/execute",
        headers={"x-runner-secret": "s3cr3t"},
        json={
            "prompt": "hi",
            "mcp_servers": {"workspace-gateway": {"type": "http", "url": "http://x:9200/mcp"}},
            "dangerous_skip_permissions": False,
        },
    )
    assert resp.status_code == 200
    assert captured.get("mcp_servers") == {
        "workspace-gateway": {"type": "http", "url": "http://x:9200/mcp"}
    }
    assert captured.get("dangerous_skip_permissions") is False


def test_execute_job_defaults_dangerous_skip_permissions_true_when_absent(monkeypatch):
    captured = {}

    def fake_start_job(job, redis_url):
        captured.update(job)

    monkeypatch.setattr(execute_mod, "start_job", fake_start_job)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "unix:///fake.sock")

    client = TestClient(build_routes({
        "execute_secret": "s3cr3t",
        "shared_redis_url": "redis://example.test:6379/0",
    }))
    resp = client.post(
        "/execute",
        headers={"x-runner-secret": "s3cr3t"},
        json={"prompt": "hi"},
    )
    assert resp.status_code == 200
    assert captured.get("mcp_servers") is None
    assert captured.get("dangerous_skip_permissions") is True


def _job_get_keys(execute_py_source: str) -> set[str]:
    """Every `job.get("<key>"` (or `job["<key>"]`) literal referenced in execute.py."""
    keys = set(re.findall(r'job(?:\.get)?\(?\[?"([a-zA-Z_]+)"', execute_py_source))
    return keys


def _job_dict_literal_keys(routes_py_source: str) -> set[str]:
    """Every key of the `job = {...}` dict literal built in routes.py's execute_job()."""
    tree = ast.parse(routes_py_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if targets == ["job"]:
                return {
                    k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    pytest.fail("could not find `job = {...}` dict literal in routes.py")


def test_no_field_execute_py_reads_is_dropped_by_the_route():
    """Contract test: if execute.py starts reading a new job["..."] field,
    routes.py's job dict literal must forward it too, or this fails —
    the exact bug class that caused the MCP-config-dropped incident."""
    execute_src = (ROOT / "agents_platform_runners_app" / "execute.py").read_text()
    routes_src = (ROOT / "agents_platform_runners_app" / "routes.py").read_text()

    consumed = _job_get_keys(execute_src)
    forwarded = _job_dict_literal_keys(routes_src)

    # run_id is synthesized by the route itself (uuid4 fallback), not a
    # passthrough field from the request body — legitimately absent from
    # `body.get(...)` forwarding, so exempt it from the contract.
    consumed -= {"run_id"}

    missing = consumed - forwarded
    assert not missing, (
        f"execute.py reads job[{missing!r}] but routes.py's job dict literal "
        "never forwards it from the request body — this is exactly the bug "
        "that silently dropped mcp_servers/dangerous_skip_permissions."
    )
