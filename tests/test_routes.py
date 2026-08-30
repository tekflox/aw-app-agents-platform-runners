"""TestClient coverage for agents_platform_runners_app/routes.py's
build_routes() (ADR Decision 6 item 6, docs/knowledge_base/docs/
architecture/adr-app-front-back-routes-dual-mode.md).

Run: .venv/aw/bin/python -m pytest tests/test_routes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes, RUNNERS  # noqa: E402


def test_status_reports_every_runner():
    client = TestClient(build_routes())
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["runners"].keys()) == set(RUNNERS)
    for info in body["runners"].values():
        assert "installed" in info and "path" in info and "version" in info


def test_status_reflects_config():
    client = TestClient(build_routes({"agents_platform_base": "http://example.test:9999"}))
    resp = client.get("/status")
    assert resp.json()["agents_platform_base"] == "http://example.test:9999"


def test_warm_containers_without_a_container_socket_is_a_clear_error(monkeypatch):
    """No AW_CONTAINER_SOCKET must read as 'no engine available', never as an
    empty containers list — the same distinction /execute already makes."""
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", None)
    client = TestClient(build_routes())
    resp = client.get("/warm-containers")
    assert resp.status_code == 503
    assert "AW_CONTAINER_SOCKET is not set" in resp.json()["detail"]
