"""Regression test: warm mode must be a PERSISTED-CONFIG switch, ON by
default — not the host env var it used to be.

Until 0.32.0 `warm_pool.enabled()` was `os.environ["RUNNER_WARM_CONTAINER"]
== "1"`. aw-remote-host's bootstrap/workspace/install.sh only forwards that
var into the workspace container when the HOST's own aw-remote-host process
has it set, so every workspace recreate (i.e. every update/deploy) dropped a
hand-set flag and warm mode silently turned itself back off — observed
repeatedly, last on 2026-08-12, when the workspace container was recreated
at 15:33 UTC and came back with no RUNNER_WARM_CONTAINER at all while an
orphaned aw-warm-* container from before the recreate kept burning.

Contract now:
  * default (nothing configured, no env)          -> warm ON
  * warm_container: false in app config           -> warm OFF
  * RUNNER_WARM_CONTAINER explicitly set          -> wins over config
  * RUNNER_WARM_CONTAINER unset/empty             -> config decides

Run: .venv/aw/bin/python -m pytest tests/test_warm_enabled_config.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import warm_pool  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Both halves of the state this module reads are process-global — the
    env var and the module-level config resolution — so reset both around
    every test rather than leaking one case's answer into the next."""
    monkeypatch.delenv(warm_pool.ENV_VAR, raising=False)
    warm_pool.configure({})
    yield
    warm_pool.configure({})


def test_default_is_on_with_no_config_and_no_env():
    assert warm_pool.configure({}) is True
    assert warm_pool.enabled() is True


def test_missing_field_in_a_populated_config_still_defaults_on():
    # The realistic case: an install that predates this field. Its persisted
    # config has the other keys and no warm_container — that must read as ON,
    # not as "unset -> off".
    assert warm_pool.configure({"agents_platform_base": "http://x:1"}) is True


def test_config_false_disables():
    assert warm_pool.configure({"warm_container": False}) is False
    assert warm_pool.enabled() is False


def test_config_true_enables():
    warm_pool.configure({"warm_container": False})
    assert warm_pool.configure({"warm_container": True}) is True


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "off", ""])
def test_stringy_falsey_config_values_disable(raw):
    # A hand-edited config (or any UI that round-trips the boolean as text)
    # must not turn the string "false" into True.
    assert warm_pool.configure({"warm_container": raw}) is False


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on"])
def test_stringy_truthy_config_values_enable(raw):
    assert warm_pool.configure({"warm_container": raw}) is True


def test_env_var_overrides_config_off(monkeypatch):
    warm_pool.configure({"warm_container": False})
    monkeypatch.setenv(warm_pool.ENV_VAR, "1")
    assert warm_pool.enabled() is True


def test_env_var_overrides_config_on(monkeypatch):
    warm_pool.configure({"warm_container": True})
    monkeypatch.setenv(warm_pool.ENV_VAR, "0")
    assert warm_pool.enabled() is False


def test_empty_env_var_does_not_count_as_an_override(monkeypatch):
    # install.sh's `${RUNNER_WARM_CONTAINER:+-e ...}` never emits an empty
    # value, but a hand-written `-e RUNNER_WARM_CONTAINER=` would — and an
    # empty string must fall through to config, not silently force OFF.
    warm_pool.configure({"warm_container": True})
    monkeypatch.setenv(warm_pool.ENV_VAR, "")
    assert warm_pool.enabled() is True
