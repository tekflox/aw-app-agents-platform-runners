"""codex_scope_canary.py — the required deliverable on card
4085bf3b-9510-81ba-adc3-fee01f6cee37: a doctor/CI check that spawns a
no-MCP codex job and asserts the gateway saw zero `/mcp` transports while
it ran, automating that card's own acceptance criterion.

Covers the detection logic (``run_canary``, the access-line regex) with a
fake docker-SDK client — this repo's CI has no live podman socket or real
``aw-mcp-gateway`` container to spawn against, so this is what actually
runs in CI; the live spawn+log-check itself can only be exercised by hand
against a real deployment (see this module's own docstring for why that is
a deliberately NOT-cheap, on-demand check) — and the ``/codex-scope-canary/
doctor`` route's own translation of the result into a doctor verdict.

Run: /usr/bin/python3.12 -m pytest tests/test_codex_scope_canary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import codex_scope_canary  # noqa: E402
from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


class _FakeCanaryContainer:
    def __init__(self, rc: int = 0):
        self._rc = rc
        self.remove_calls = 0

    def wait(self, timeout=None):
        return {"StatusCode": self._rc}

    def remove(self, force=False):
        self.remove_calls += 1


class _FakeGatewayContainer:
    def __init__(self, log_lines: list[str]):
        self._log_lines = log_lines
        self.logs_calls: list[dict] = []

    def logs(self, since=None, stdout=True, stderr=True):
        self.logs_calls.append({"since": since, "stdout": stdout, "stderr": stderr})
        return ("\n".join(self._log_lines)).encode()


class _FakeContainers:
    def __init__(self, canary_container, gateway_container):
        self._canary_container = canary_container
        self._gateway_container = gateway_container
        self.run_calls: list[dict] = []

    def run(self, image, **kwargs):
        self.run_calls.append({"image": image, "kwargs": kwargs})
        return self._canary_container

    def get(self, name):
        if name != codex_scope_canary.GATEWAY_CONTAINER_NAME:
            raise LookupError(f"no such container: {name}")
        return self._gateway_container


class _FakeClient:
    def __init__(self, canary_rc=0, gateway_log_lines=None):
        self.containers = _FakeContainers(
            _FakeCanaryContainer(canary_rc),
            _FakeGatewayContainer(gateway_log_lines or []))


def _stub_build_container_kwargs(monkeypatch):
    """The canary's job construction is already covered end-to-end by
    test_codex_agent_scoped_mcp_config.py — stub it here so these tests
    exercise only the canary's OWN logic (spawn/wait/remove, gateway-log
    scan), not the full home/creds-staging machinery."""
    monkeypatch.setattr(
        execute_mod, "_build_container_kwargs",
        lambda job, client: ("fake-image", [], {"name": f"canary-{job['run_id']}"}, None))


def test_canary_reports_ok_when_gateway_log_is_clean(monkeypatch):
    _stub_build_container_kwargs(monkeypatch)
    client = _FakeClient(canary_rc=0, gateway_log_lines=[
        '172.18.0.5:51234 - "GET /healthz HTTP/1.1" 200',
    ])

    result = codex_scope_canary.run_canary(client, run_id="r-clean")

    assert result["ok"] is True
    assert result["gateway_mcp_access_lines"] == []
    assert result["container_exit_code"] == 0
    assert client.containers._canary_container.remove_calls == 1


def test_canary_reports_not_ok_when_gateway_logged_mcp_traffic(monkeypatch):
    """THE failure this canary exists to catch: a no-MCP codex job ran and
    the gateway's own access log shows a /mcp handshake anyway."""
    _stub_build_container_kwargs(monkeypatch)
    client = _FakeClient(canary_rc=0, gateway_log_lines=[
        '172.18.0.9:9876 - "POST /mcp HTTP/1.1" 200',
        '172.18.0.9:9876 - "POST /mcp HTTP/1.1" 202',
    ])

    result = codex_scope_canary.run_canary(client, run_id="r-leak")

    assert result["ok"] is False
    assert len(result["gateway_mcp_access_lines"]) == 2


def test_canary_matches_scoped_profile_paths_too(monkeypatch):
    _stub_build_container_kwargs(monkeypatch)
    client = _FakeClient(gateway_log_lines=[
        '172.18.0.9:1 - "POST /mcp/crispal-full HTTP/1.1" 200',
    ])

    result = codex_scope_canary.run_canary(client, run_id="r-scoped")

    assert result["ok"] is False
    assert result["gateway_mcp_access_lines"]


def test_canary_reports_error_without_raising_when_spawn_fails(monkeypatch):
    """An unrelated infra hiccup (container engine refuses the spawn) is a
    different problem from what this canary exists to catch — it must be
    reported, not raised, so a bad canary run doesn't look like a crash."""
    def _boom(job, client):
        raise RuntimeError("podman is down")
    monkeypatch.setattr(execute_mod, "_build_container_kwargs", _boom)
    client = _FakeClient()

    result = codex_scope_canary.run_canary(client, run_id="r-boom")

    assert result["ok"] is False
    assert "podman is down" in result["error"]


def test_canary_removes_the_container_even_when_already_leaking(monkeypatch):
    _stub_build_container_kwargs(monkeypatch)
    client = _FakeClient(gateway_log_lines=['x "POST /mcp HTTP/1.1" 200'])

    codex_scope_canary.run_canary(client, run_id="r-cleanup")

    assert client.containers._canary_container.remove_calls == 1


# ---------------------------------------------------------------------------
# /codex-scope-canary/doctor route
# ---------------------------------------------------------------------------

def test_doctor_route_is_red_without_a_container_socket(monkeypatch):
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "")
    app = TestClient(build_routes({}))

    body = app.get("/codex-scope-canary/doctor").json()

    assert body["ok"] is False
    assert "AW_CONTAINER_SOCKET" in body["detail"]["problems"][0]


def test_doctor_route_is_green_when_the_canary_is_clean(monkeypatch):
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/var/run/fake.sock")
    monkeypatch.setattr("docker.DockerClient", lambda base_url=None: object())
    monkeypatch.setattr(codex_scope_canary, "run_canary",
                         lambda client, **kw: {"run_id": "r-x", "ok": True,
                                                "gateway_mcp_access_lines": [],
                                                "container_exit_code": 0})
    app = TestClient(build_routes({}))

    body = app.get("/codex-scope-canary/doctor").json()

    assert body["ok"] is True
    assert body["detail"]["problems"] == []


def test_doctor_route_is_red_and_names_the_leak_when_the_canary_catches_one(monkeypatch):
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/var/run/fake.sock")
    monkeypatch.setattr("docker.DockerClient", lambda base_url=None: object())
    monkeypatch.setattr(codex_scope_canary, "run_canary",
                         lambda client, **kw: {
                             "run_id": "r-y", "ok": False,
                             "gateway_mcp_access_lines": ['x "POST /mcp HTTP/1.1" 200'],
                             "container_exit_code": 0})
    app = TestClient(build_routes({}))

    body = app.get("/codex-scope-canary/doctor").json()

    assert body["ok"] is False
    assert "NO MCP servers" in body["detail"]["problems"][0]
    assert body["detail"]["gateway_mcp_access_lines"]
