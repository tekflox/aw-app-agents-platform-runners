"""Regression test for the cross-worker config-propagation bug found live
2026-09-06: `kanban_sweep_enabled` was flipped on one uvicorn worker, but the
Redis-lease leader running the watchdog held a `self._live_config` that was
only ever refreshed by `activate()`/`on_config_saved()` — both single-worker
paths — so the leader read the flag as unchanged forever (ticked silently,
zero log). Fixed by wiring `Plugin.on_config_reloaded`, which aw-workspace
core now calls on EVERY worker (inline on the request worker, and via the
`apps:changed` broadcast on the rest) — see src/apps/reconciler.py and
src/apps/routes.py::save_app_config in aw-workspace core.

Run: .venv/aw/bin/python -m pytest tests/test_on_config_reloaded.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app.plugin import AgentsPlatformRunnersAppPlugin  # noqa: E402


class _StubCtx:
    def __init__(self, config: dict) -> None:
        self.config = config


def test_on_config_reloaded_refreshes_live_config_in_place():
    plugin = AgentsPlatformRunnersAppPlugin()
    live_config = plugin._live_config  # identity kept across the call, never rebound
    ctx = _StubCtx({"kanban_sweep_enabled": False})

    asyncio.run(plugin.on_config_reloaded(ctx))
    assert plugin._live_config is live_config
    assert plugin._live_config["kanban_sweep_enabled"] is False

    # Simulate the flag flipping on a DIFFERENT worker: this worker never
    # sees the POST, only the reconciler calling on_config_reloaded off the
    # apps:changed broadcast (or the inline call on the request worker).
    ctx.config["kanban_sweep_enabled"] = True
    asyncio.run(plugin.on_config_reloaded(ctx))

    assert plugin._live_config is live_config
    assert plugin._live_config["kanban_sweep_enabled"] is True


def test_on_config_reloaded_does_not_touch_disk_or_network(tmp_path, monkeypatch):
    # Anything that writes mcp.json, pushes platform settings, or bumps warm
    # generation belongs in on_config_saved (the once-per-save PROVISION
    # half), never here — this hook runs on every worker on every broadcast.
    import agents_platform_runners_app.plugin as plugin_mod

    def _boom(*args, **kwargs):
        raise AssertionError("on_config_reloaded must not touch disk/network")

    monkeypatch.setattr(plugin_mod, "write_mcp_json", _boom)
    monkeypatch.setattr(plugin_mod.platform_settings_mod, "push_settings", _boom)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "bump_generation", _boom)

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({"kanban_sweep_enabled": True})
    asyncio.run(plugin.on_config_reloaded(ctx))  # must not raise
