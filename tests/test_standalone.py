"""Boot smoke test for template_app/__main__.py's standalone FastAPI app
(ADR Decision 4/6). Doesn't require `npm run build` to have run — asserts the
mounted API sub-app works either way; only exercises the static-file mount
when ui/dist/ actually exists (so this passes in a fresh checkout, and gets
stricter automatically once someone builds the UI).

Run: .venv/aw/bin/python -m pytest tests/test_standalone.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from template_app.__main__ import build_standalone_app, SLUG, UI_DIST  # noqa: E402


def test_standalone_app_boots_and_mounts_api():
    client = TestClient(build_standalone_app())
    resp = client.get(f"/api/apps/{SLUG}/hello")
    assert resp.status_code == 200
    assert resp.json()["message"]


def test_standalone_serves_ui_dist_when_built():
    if not UI_DIST.is_dir():
        return  # ui/ not built in this checkout — see ui/README or run `npm run build`
    client = TestClient(build_standalone_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
