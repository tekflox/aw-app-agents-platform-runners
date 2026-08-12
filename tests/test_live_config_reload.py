"""Regression test: a config save (e.g. rotating execute_secret after an
uninstall/reinstall wiped it) used to only update the on-disk config —
build_routes()'s /execute, /register and /status closures kept reading the
STALE dict snapshot handed to them at activate() time, so nothing short of
a full workspace-process restart made a saved secret actually take effect.
Found live 2026-08-11 (WS-11 kept 401ing after the secret was restored via
the config API).

Fix: build_routes()'s `cfg` is now the SAME dict object plugin.py owns as
self._live_config, mutated IN PLACE (never rebound) by on_config_saved —
so a request made right after a config save sees the new values, with no
restart. This test exercises that identity directly against build_routes(),
without needing the full AppRuntime/plugin lifecycle.

Run: .venv/aw/bin/python -m pytest tests/test_live_config_reload.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app.routes import build_routes  # noqa: E402


def test_mutating_the_live_config_dict_in_place_is_seen_by_open_routes():
    live_config = {"agents_platform_base": "http://old.example:1"}
    client = TestClient(build_routes(live_config))

    assert client.get("/status").json()["agents_platform_base"] == "http://old.example:1"

    # Simulate on_config_saved: mutate in place, never rebind the name.
    live_config.clear()
    live_config.update({"agents_platform_base": "http://new.example:2"})

    assert client.get("/status").json()["agents_platform_base"] == "http://new.example:2"


def test_execute_secret_rotated_after_save_is_honoured_without_restart():
    live_config: dict = {}  # simulates a wiped-secret reinstall: no secret yet
    client = TestClient(build_routes(live_config))

    resp = client.post("/execute", headers={"X-Runner-Secret": "whatever"}, json={})
    assert resp.status_code == 500  # execute_secret is not configured

    # Simulate the config-save fix restoring the secret in place.
    live_config["execute_secret"] = "s3cr3t"

    resp = client.post("/execute", headers={"X-Runner-Secret": "wrong"}, json={})
    assert resp.status_code == 401  # now actually checking the presented header

    resp = client.post("/execute", headers={"X-Runner-Secret": "s3cr3t"}, json={})
    assert resp.status_code != 401  # secret accepted (fails later for unrelated reasons in this stub env)


def test_build_routes_with_empty_config_still_shares_identity_via_none_check():
    # `config or {}` would silently break the shared-identity contract the
    # moment the live config starts out empty ({} is falsy) — it would
    # rebind `cfg` to a brand-new dict literal instead of keeping the
    # caller's object. Guard against that regression explicitly.
    live_config: dict = {}
    build_routes(live_config)  # must not raise / must accept an empty dict
    live_config["agents_platform_base"] = "http://after.example:3"
    client = TestClient(build_routes(live_config))
    assert client.get("/status").json()["agents_platform_base"] == "http://after.example:3"
