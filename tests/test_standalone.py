"""Boot smoke test for agents_platform_runners_app/__main__.py's standalone
FastAPI app (ADR Decision 4/6). No UI mount — this app is infra-only.

Run: .venv/aw/bin/python -m pytest tests/test_standalone.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app.__main__ import build_standalone_app, SLUG  # noqa: E402


def test_standalone_app_boots_and_mounts_api():
    client = TestClient(build_standalone_app())
    resp = client.get(f"/api/apps/{SLUG}/status")
    assert resp.status_code == 200
    assert resp.json()["runners"]
