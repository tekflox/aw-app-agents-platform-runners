"""TestClient coverage for template_app/routes.py's build_routes() (ADR
Decision 6 item 6, docs/knowledge_base/docs/architecture/
adr-app-front-back-routes-dual-mode.md).

TEMPLATE: this is the pattern every aw-app-* backend-routes app uses — build
the sub-app fresh per test (build_routes() must be call-idempotent), assert
HTTP + WS against it directly, no framework runtime needed.

Run: .venv/aw/bin/python -m pytest tests/test_routes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from template_app.routes import build_routes  # noqa: E402


def test_hello():
    client = TestClient(build_routes())
    resp = client.get("/hello")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Hello from the aw-app-template sub-app"}


def test_ws_echo():
    client = TestClient(build_routes())
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "ping"
        ws.send_text("pong")
        assert ws.receive_text() == "pong"
