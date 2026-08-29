"""Unit tests for observability_push.py (Kanban ap-mt-tenant-log-routing) —
covers the two-hop push (local read, AP-MT write) against stubbed HTTP legs,
same monkeypatch-the-private-helper style as test_skills_sync.py. No real
network, no aw-workspace server, no agents-platform-multitenant instance
needed.
"""
from __future__ import annotations

import pytest

from agents_platform_runners_app import observability_push as op


def test_no_token_skips_without_touching_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("should never read local settings with no token configured")
    monkeypatch.setattr(op, "_read_local_observability", _boom)

    result = op.push_once({})
    assert result == {"pushed": False, "reason": "agents_platform_token not configured"}


def test_local_read_failure_short_circuits_before_the_remote_leg(monkeypatch):
    def _fail(*, timeout):
        raise op.ObservabilityPushError("boom: local unreachable")
    monkeypatch.setattr(op, "_read_local_observability", _fail)

    def _boom(*a, **k):
        raise AssertionError("should never reach AP-MT when the local read failed")
    monkeypatch.setattr(op, "_push_to_platform", _boom)

    result = op.push_once({"agents_platform_token": "tok"})
    assert result == {"pushed": False, "reason": "boom: local unreachable"}


def test_resolved_target_is_forwarded_verbatim(monkeypatch):
    monkeypatch.setattr(op, "_read_local_observability", lambda *, timeout: {
        "mode": "custom",
        "resolved": {"endpoint": "https://tenant-a.example", "api_key": "k-a", "source": "custom"},
    })

    seen = {}

    def _capture(base, token, payload, *, timeout):
        seen["base"] = base
        seen["token"] = token
        seen["payload"] = payload
        return {"workspace": payload["workspace"], "configured": True}
    monkeypatch.setattr(op, "_push_to_platform", _capture)

    result = op.push_once({"agents_platform_token": "tok-a",
                           "agents_platform_base": "http://ap-mt.example"})

    assert seen["base"] == "http://ap-mt.example"
    assert seen["token"] == "tok-a"
    assert seen["payload"]["endpoint"] == "https://tenant-a.example"
    assert seen["payload"]["api_key"] == "k-a"
    assert result == {"pushed": True, "mode": "custom", "workspace": seen["payload"]["workspace"],
                      "configured": True}


def test_unresolved_local_settings_pushes_empty_endpoint_to_clear(monkeypatch):
    """mode 'off' (or 'local' with the app since uninstalled) resolves to
    `resolved: null` — this must still push, with an empty endpoint, so
    AP-MT's own row gets deleted (default-drop) instead of staying stale."""
    monkeypatch.setattr(op, "_read_local_observability", lambda *, timeout: {
        "mode": "off", "resolved": None,
    })

    seen = {}

    def _capture(base, token, payload, *, timeout):
        seen["payload"] = payload
        return {"configured": False}
    monkeypatch.setattr(op, "_push_to_platform", _capture)

    result = op.push_once({"agents_platform_token": "tok"})

    assert seen["payload"]["endpoint"] == ""
    assert seen["payload"]["api_key"] == ""
    assert result == {"pushed": True, "mode": "off", "configured": False}


def test_remote_leg_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(op, "_read_local_observability", lambda *, timeout: {
        "mode": "custom", "resolved": {"endpoint": "https://x.example", "api_key": "k"},
    })

    def _fail(*a, **k):
        raise op.ObservabilityPushError("agents-platform-multitenant rejected the push: 401")
    monkeypatch.setattr(op, "_push_to_platform", _fail)

    result = op.push_once({"agents_platform_token": "tok"})
    assert result == {"pushed": False,
                      "reason": "agents-platform-multitenant rejected the push: 401"}


def test_local_api_key_missing_raises_push_error(monkeypatch):
    monkeypatch.delenv(op.API_KEY_VAR, raising=False)
    monkeypatch.setattr(op, "_from_env_file", lambda name: None)

    with pytest.raises(op.ObservabilityPushError, match=op.API_KEY_VAR):
        op._read_local_observability(timeout=5.0)
