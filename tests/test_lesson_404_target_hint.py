"""Card 4115bf3b-9510-814c-8f9b-fb9ac70eb663 — create_target_lesson's ``slug``
param is the EXISTING Target's slug, not the lesson's own slug. A caller
that confuses the two (as happened live) got a bare 404 with no way out.

On a 404 from the lessons endpoint, if the caller supplied
``created_in_run_id``, _call_tool now resolves that run via GET
/api/runs/{id} and — if it resolves to a real target — appends a one-step
retry hint naming that target's slug. A failed run lookup (network error,
run not found) must fall back to the plain 404, never mask it.

Run: .venv/aw/bin/python -m pytest tests/test_lesson_404_target_hint.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402


def _patch_client(monkeypatch, handler):
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def test_create_target_lesson_404_with_resolvable_run_id_gets_hint(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/lessons"):
            return httpx.Response(404, json={"detail": "target not found: 'bad-slug'"})
        if request.method == "GET" and "/api/runs/" in request.url.path:
            return httpx.Response(200, json={"id": "run-1", "target_slug": "real-target"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_client(monkeypatch, handler)

    result = asyncio.run(mcp_server._call_tool("create_target_lesson", {
        "slug": "bad-slug", "category": "pitfall", "title": "t",
        "created_in_run_id": "run-1",
    }))

    payload = json.loads(result[0].text)
    assert payload["status"] == 404
    assert "real-target" in payload["message"]
    assert "retry with slug='real-target'" in payload["message"]


def test_create_target_lesson_404_without_run_id_has_no_hint(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/lessons"):
            return httpx.Response(404, json={"detail": "target not found: 'bad-slug'"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_client(monkeypatch, handler)

    result = asyncio.run(mcp_server._call_tool("create_target_lesson", {
        "slug": "bad-slug", "category": "pitfall", "title": "t",
    }))

    payload = json.loads(result[0].text)
    assert payload["status"] == 404
    assert "retry with slug=" not in payload["message"]


def test_create_target_lesson_404_with_unresolvable_run_id_falls_back_plainly(monkeypatch):
    """The run lookup itself failing (run not found / transport error) must
    NOT mask the original 404 — same plain error as if no hint were ever
    attempted."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/lessons"):
            return httpx.Response(404, json={"detail": "target not found: 'bad-slug'"})
        if request.method == "GET" and "/api/runs/" in request.url.path:
            return httpx.Response(404, json={"detail": "run not found"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_client(monkeypatch, handler)

    result = asyncio.run(mcp_server._call_tool("create_target_lesson", {
        "slug": "bad-slug", "category": "pitfall", "title": "t",
        "created_in_run_id": "ghost-run",
    }))

    payload = json.loads(result[0].text)
    assert payload["status"] == 404
    assert "retry with slug=" not in payload["message"]
    assert "target not found" in payload["message"]


def test_update_target_lesson_404_with_resolvable_run_id_gets_hint(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/lessons/" in request.url.path:
            return httpx.Response(404, json={"detail": "target not found: 'bad-slug'"})
        if request.method == "GET" and "/api/runs/" in request.url.path:
            return httpx.Response(200, json={"id": "run-2", "target_slug": "other-target"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_client(monkeypatch, handler)

    result = asyncio.run(mcp_server._call_tool("update_target_lesson", {
        "slug": "bad-slug", "lesson_id": "lesson-1",
        "created_in_run_id": "run-2",
    }))

    payload = json.loads(result[0].text)
    assert payload["status"] == 404
    assert "retry with slug='other-target'" in payload["message"]


def test_create_target_lesson_success_is_unaffected(monkeypatch):
    """No regression on the happy path — a 200 still returns the lesson, no
    hint logic runs at all."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/lessons"):
            return httpx.Response(200, json={"id": "lesson-xyz", "title": "t"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_client(monkeypatch, handler)

    result = asyncio.run(mcp_server._call_tool("create_target_lesson", {
        "slug": "real-target", "category": "pitfall", "title": "t",
    }))

    payload = json.loads(result[0].text)
    assert payload["id"] == "lesson-xyz"
