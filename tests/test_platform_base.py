"""platform_base.resolve() — where this app resolves agents_platform_base
(Kanban bug:agents-platform-base-default-unreachable-on-byod, Architect
decision 2026-09-14). No real network — just the resolution order and the
legacy-literal-as-unset rule.
"""
from __future__ import annotations

import pytest

from agents_platform_runners_app import platform_base


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AW_AGENTS_PLATFORM_BASE", "AW_BACKEND_URL", "AW_WORKSPACE_HOME",
                "AW_WORKSPACE_CONTAINER_DIR"):
        monkeypatch.delenv(var, raising=False)
    # workspace_env() falls back to reading <home>/.env when the process env
    # is empty — point it at a directory with no such file so tests don't
    # pick up whatever happens to be on this host.
    monkeypatch.setenv("AW_WORKSPACE_HOME", "/nonexistent-for-tests")


def test_explicit_non_legacy_config_wins_over_everything(monkeypatch):
    monkeypatch.setenv("AW_AGENTS_PLATFORM_BASE", "http://from-env:10014")
    monkeypatch.setenv("AW_BACKEND_URL", "https://api.aw.tekflox.com")
    assert platform_base.resolve(
        {"agents_platform_base": "https://explicit.example:9999"}
    ) == "https://explicit.example:9999"


def test_env_var_escape_hatch_used_when_config_blank_or_absent(monkeypatch):
    monkeypatch.setenv("AW_AGENTS_PLATFORM_BASE", "http://172.18.0.1:10014")
    monkeypatch.setenv("AW_BACKEND_URL", "https://api.aw.tekflox.com")
    assert platform_base.resolve({}) == "http://172.18.0.1:10014"
    assert platform_base.resolve({"agents_platform_base": ""}) == "http://172.18.0.1:10014"


def test_derives_from_aw_backend_url_apex_when_nothing_else_set(monkeypatch):
    monkeypatch.setenv("AW_BACKEND_URL", "https://api.aw.tekflox.com")
    assert platform_base.resolve({}) == "https://agents-platform.aw.tekflox.com"


def test_derives_from_aw_backend_url_apex_for_a_different_domain(monkeypatch):
    monkeypatch.setenv("AW_BACKEND_URL", "https://api.example.org")
    assert platform_base.resolve({}) == "https://agents-platform.example.org"


def test_static_public_fallback_when_nothing_is_set():
    assert platform_base.resolve({}) == "https://agents-platform.aw.tekflox.com"
    assert platform_base.resolve(None) == "https://agents-platform.aw.tekflox.com"


@pytest.mark.parametrize("legacy", [
    "http://172.18.0.1:10014",
    "http://127.0.0.1:10014",
    "http://localhost:10014",
    "http://172.18.0.1:10014/",  # trailing slash must not defeat the match
])
def test_persisted_legacy_literal_is_treated_as_unset_and_reresolved(monkeypatch, legacy):
    """The real production case for every existing BYOD install: the config
    row already has the old bridge/loopback default persisted (this app's
    identity_token watchdog re-saves the whole config every ~6h), not an
    empty dict. Resolution must ignore it and fall through exactly as if the
    field had never been configured."""
    monkeypatch.setenv("AW_BACKEND_URL", "https://api.aw.tekflox.com")
    config = {"agents_platform_token": "tok123", "agents_platform_base": legacy}
    assert platform_base.resolve(config) == "https://agents-platform.aw.tekflox.com"


def test_legacy_literal_config_still_yields_env_escape_hatch(monkeypatch):
    monkeypatch.setenv("AW_AGENTS_PLATFORM_BASE", "http://co-located-bridge:10014")
    config = {"agents_platform_base": "http://172.18.0.1:10014"}
    assert platform_base.resolve(config) == "http://co-located-bridge:10014"


def test_malformed_backend_url_does_not_raise_and_falls_through(monkeypatch):
    monkeypatch.setenv("AW_BACKEND_URL", "::::not a url::::")
    assert platform_base.resolve({}) == "https://agents-platform.aw.tekflox.com"
