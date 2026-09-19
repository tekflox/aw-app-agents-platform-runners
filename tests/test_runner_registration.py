"""Unit tests for runner_registration.py (Kanban
feature:ap-runners-auto-register-on-activation) — the logic shared by the
manual POST /register route and the automatic call now made at activate()
and its periodic watchdog. Same stub-the-transport style as
test_identity_token.py. No real network, no agents-platform-multitenant.

Run: .venv/aw/bin/python -m pytest tests/test_runner_registration.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import runner_registration as rr  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self)


def _stub_client(monkeypatch, responder, recorder=None):
    """Replace httpx.Client inside runner_registration with one whose
    .post(url, **kwargs) is answered by responder(url, kwargs) — a
    FakeResponse, or an exception instance to raise."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            if recorder is not None:
                recorder.append((url, kwargs))
            result = responder(url, kwargs)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(rr.httpx, "Client", _Client)


@pytest.fixture(autouse=True)
def _stub_runner_status(monkeypatch):
    # Real CLIs are neither installed nor relevant to this logic — freeze
    # the per-runner shape so payload assertions don't depend on this host.
    monkeypatch.setattr(rr, "runner_status", lambda name: {
        "installed": True, "path": f"/usr/local/bin/{name}", "version": "1.0",
    })


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # register_with_platform's retry loop (REGISTER_MAX_ATTEMPTS) sleeps
    # between attempts — every test below exercises at most a handful of
    # attempts, so keep the suite fast without weakening what's asserted.
    monkeypatch.setattr(rr.time, "sleep", lambda seconds: None)


def test_no_token_configured_is_a_non_fatal_error_no_network(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    result = rr.register_with_platform({})

    assert "error" in result
    assert "agents_platform_token" in result["error"]
    assert seen == []


def test_successful_register_posts_every_runner_with_bearer_token(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {"ok": True}), seen)

    result = rr.register_with_platform({
        "agents_platform_token": "tok123",
        "agents_platform_base": "http://ap-mt.example",
    })

    assert result == {"registered": {"ok": True}}
    assert len(seen) == 1
    url, kwargs = seen[0]
    assert url == "http://ap-mt.example/api/runners/register"
    assert kwargs["headers"] == {"Authorization": "Bearer tok123"}
    payload = kwargs["json"]
    assert payload["workspace"] == "aw"
    assert {r["cli"] for r in payload["runners"]} == set(rr.RUNNERS)


def test_register_reads_workspace_from_the_env_file_not_just_process_env(monkeypatch, tmp_path):
    """runner-dynamic-workspace-slug (Perna C): a raw ``os.environ.get`` here
    silently registers this workspace as "aw" — the shared default — the
    moment ``AW_WORKSPACE`` lives only in ``.aw-workspace/.env`` and not in
    the process's own env, overwriting whatever this workspace already
    registered under its real name (dedup key is (tenant_id, workspace, cli)).
    """
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("AW_WORKSPACE=crispal\n")

    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {"ok": True}), seen)

    rr.register_with_platform({
        "agents_platform_token": "tok123",
        "agents_platform_base": "http://ap-mt.example",
    })

    assert seen[0][1]["json"]["workspace"] == "crispal"


def test_register_payload_carries_dispatch_credentials_for_ap_mt(monkeypatch):
    """AP-MT's RunnerLLM needs a caller_token + execute_secret to call this
    workspace's own /execute back (self-heal for the 2026-09-17 stale-
    RUNNER_CALLER_TOKEN incident — see runner_registration.py). Both should
    ride this same, already-authenticated registration call rather than
    living as separately-managed static secrets."""
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    rr.register_with_platform({
        "agents_platform_token": "tok123",
        "execute_secret": "shhh",
    })

    payload = seen[0][1]["json"]
    # Same value used for the call's own Bearer auth — no second mint.
    assert payload["caller_token"] == "tok123"
    assert payload["execute_secret"] == "shhh"


def test_register_payload_omits_execute_secret_when_not_configured(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    rr.register_with_platform({"agents_platform_token": "tok123"})

    assert seen[0][1]["json"]["execute_secret"] is None


def test_register_uses_own_base_url_override_when_configured(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    rr.register_with_platform({
        "agents_platform_token": "tok",
        "own_base_url": "https://custom.example",
    })

    assert seen[0][1]["json"]["base_url"] == "https://custom.example"


def test_register_returns_error_on_http_error_status_no_raise(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(500, None, "boom"))

    result = rr.register_with_platform({"agents_platform_token": "tok"})

    assert "error" in result
    assert "500" in result["error"]


def test_register_returns_error_on_connection_failure_no_raise(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: httpx.ConnectError("connection refused"), seen)

    result = rr.register_with_platform({"agents_platform_token": "tok"})

    assert "error" in result
    assert "could not reach" in result["error"]
    # Transient (connection-level) failures are retried, not given up on
    # after a single attempt — see REGISTER_MAX_ATTEMPTS's docstring for the
    # 2026-09-18 incident this closes.
    assert len(seen) == rr.REGISTER_MAX_ATTEMPTS


def test_register_retries_transient_5xx_and_succeeds(monkeypatch):
    """A 502/503/504 is the tunnel edge or agents-platform-multitenant
    briefly hiccuping, not a real misconfiguration — retry it, same as
    RunnerLLM._dispatch's RETRYABLE_STATUS on the other side of this call."""
    seen = []
    responses = iter([
        FakeResponse(503, None, "starting up"),
        FakeResponse(200, {"ok": True}),
    ])
    _stub_client(monkeypatch, lambda url, kwargs: next(responses), seen)

    result = rr.register_with_platform({"agents_platform_token": "tok"})

    assert result == {"registered": {"ok": True}}
    assert len(seen) == 2


def test_register_does_not_retry_deterministic_4xx(monkeypatch):
    """A 401 (bad/expired agents_platform_token) is not going to succeed on
    a second attempt — retrying just delays the error an operator needs to
    see, same reasoning RunnerLLM._dispatch already applies to /execute."""
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(401, None, "expired"), seen)

    result = rr.register_with_platform({"agents_platform_token": "tok"})

    assert "error" in result
    assert len(seen) == 1


def test_register_gives_up_after_max_attempts_on_persistent_5xx(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(503, None, "down"), seen)

    result = rr.register_with_platform({"agents_platform_token": "tok"})

    assert "error" in result
    assert "503" in result["error"]
    assert len(seen) == rr.REGISTER_MAX_ATTEMPTS
