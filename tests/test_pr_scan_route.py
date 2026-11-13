"""``POST /pr-scan`` — the core-side half of the work_loop `prs` loop
(card 4175bf3b gateway-boundary follow-up). ``mcp_server.py`` runs as a
stdio child of the mcp-gateway's OWN container and cannot reach
``/opt/aw-workspace/repos``/``gh``/``git`` directly, so the actual
``pr_scan.scan_prs`` call has to happen here, core-side, reached over the
published workspace API — same topology as ``/execution-index/search`` and
``/warm-containers``.

This file only exercises the ROUTE: that it forwards params to
``pr_scan.scan_prs`` and returns its result verbatim, unreshaped. The
scanner's own logic is covered by test_pr_scan.py; the MCP-side HTTP
closure that calls this route is covered by test_mcp_server_work_loop.py.

Run: python -m pytest tests/test_pr_scan_route.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import pr_scan as pr_scan_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


def test_pr_scan_route_returns_scan_prs_result_verbatim(monkeypatch):
    """Thin-on-purpose (Architect ruling): the route must not pop or
    reshape ``work_orders``/``tagged`` — that contract stays owned by
    work_loop.py's ``_prs_loop`` on the MCP side."""
    fake_result = {"owners": ["tekflox"], "candidates": [{"pr": "tekflox/aw-app-kb#7"}],
                  "work_orders": [{"pr": "tekflox/aw-app-kb#7", "reason": "stalled"}],
                  "tagged": [{"pr": "tekflox/aw-app-kb#7", "detail": "x"}],
                  "repos_unmapped": [], "counts": {}, "truncated": False, "errors": []}
    captured = {}

    async def fake_scan_prs(**kwargs):
        captured.update(kwargs)
        return dict(fake_result)

    monkeypatch.setattr(pr_scan_mod, "scan_prs", fake_scan_prs)
    client = TestClient(build_routes())

    resp = client.post("/pr-scan", json={"now": 1800000000.0, "dry_run": True,
                                         "limit": 7, "stall_hours": 6})
    assert resp.status_code == 200
    assert resp.json() == fake_result
    assert captured == {"now": 1800000000.0, "dry_run": True, "limit": 7, "stall_hours": 6.0}


def test_pr_scan_route_forwards_dry_run_false_and_defaults(monkeypatch):
    captured = {}

    async def fake_scan_prs(**kwargs):
        captured.update(kwargs)
        return {"owners": [], "candidates": [], "work_orders": [], "tagged": [],
               "repos_unmapped": [], "counts": {}, "truncated": False, "errors": []}

    monkeypatch.setattr(pr_scan_mod, "scan_prs", fake_scan_prs)
    client = TestClient(build_routes())

    resp = client.post("/pr-scan", json={"now": 1800000000.0})
    assert resp.status_code == 200
    assert captured["dry_run"] is False
    assert captured["limit"] == 10
    assert captured["stall_hours"] == pr_scan_mod.DEFAULT_STALL_HOURS


def test_pr_scan_route_defaults_now_to_its_own_clock_when_omitted(monkeypatch):
    captured = {}

    async def fake_scan_prs(**kwargs):
        captured.update(kwargs)
        return {"owners": [], "candidates": [], "work_orders": [], "tagged": [],
               "repos_unmapped": [], "counts": {}, "truncated": False, "errors": []}

    monkeypatch.setattr(pr_scan_mod, "scan_prs", fake_scan_prs)
    client = TestClient(build_routes())

    resp = client.post("/pr-scan", json={"dry_run": True, "limit": 5})
    assert resp.status_code == 200
    assert isinstance(captured["now"], float) and captured["now"] > 0
