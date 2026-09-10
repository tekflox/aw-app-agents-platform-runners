"""Warm containers must invalidate when a DIFFERENT app moves this
workspace's MCP surface.

Until aw-workspace core grew `Plugin.on_workspace_mcp_changed`, the only
things that ever called `warm_pool.bump_generation()` were this app's own
`activate()` and `on_config_saved()`. Installing, updating or uninstalling
ANOTHER app changes the tool list every warm container's CLI process built
its MCP clients against — once, at process start, with nothing
re-initialising them for the container's whole 6h life — and nothing told
this app that happened. The 2026-08-30 incident is the live case: a shipped
tool stayed invisible to every running session until a human recycled things
by hand.

Core fires the hook from `Reconciler._trigger_gateway_reload` (aw-workspace
src/apps/reconciler.py); this side is the ~6 lines that answer it.

Run: .venv/aw/bin/python -m pytest tests/test_on_workspace_mcp_changed.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agents_platform_runners_app.plugin as plugin_mod  # noqa: E402
from agents_platform_runners_app.plugin import AgentsPlatformRunnersAppPlugin  # noqa: E402


class _StubCtx:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}


def _plugin(monkeypatch, *, warm_on: bool, redis_url: str | None = "redis://stub:6379/0"):
    bumped: list[str] = []
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "enabled", lambda: warm_on)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "bump_generation", bumped.append)
    monkeypatch.setattr(plugin_mod.shared_redis_mod, "resolve", lambda config=None: redis_url)
    return AgentsPlatformRunnersAppPlugin(), bumped


def test_bumps_generation_when_warm_mode_is_on(monkeypatch):
    """The whole point: another app moved the surface, so every warm
    container is condemned and drains+respawns on its next dispatch."""
    plugin, bumped = _plugin(monkeypatch, warm_on=True)

    asyncio.run(plugin.on_workspace_mcp_changed(_StubCtx()))

    assert bumped == ["redis://stub:6379/0"]


def test_does_nothing_when_warm_mode_is_off(monkeypatch):
    """Warm mode off means there are no warm containers to condemn — and
    `enabled()` is the same gate activate()/on_config_saved() use."""
    plugin, bumped = _plugin(monkeypatch, warm_on=False)

    asyncio.run(plugin.on_workspace_mcp_changed(_StubCtx()))

    assert bumped == []


def test_does_nothing_when_redis_is_undiscoverable(monkeypatch):
    """`resolve()` returning None is a real misconfiguration, not an empty
    string to pass along — bump_generation would have nothing to write to."""
    plugin, bumped = _plugin(monkeypatch, warm_on=True, redis_url=None)

    asyncio.run(plugin.on_workspace_mcp_changed(_StubCtx()))

    assert bumped == []


def test_does_not_write_mcp_json_or_push_platform_settings(monkeypatch):
    """Core awaits this on the install critical path and calls it on ONE
    worker only. Regenerating mcp.json or pushing platform settings here
    would be both slow and (being per-worker work done once) wrong — those
    belong in on_config_saved, which core calls for THIS app's own save."""
    plugin, bumped = _plugin(monkeypatch, warm_on=True)

    def _boom(*args, **kwargs):
        raise AssertionError("on_workspace_mcp_changed must not do provision work")

    monkeypatch.setattr(plugin_mod, "write_mcp_json", _boom)
    monkeypatch.setattr(plugin_mod.platform_settings_mod, "push_settings", _boom)

    asyncio.run(plugin.on_workspace_mcp_changed(_StubCtx()))

    assert bumped == ["redis://stub:6379/0"]


def test_reads_live_config_not_ctx_config(monkeypatch):
    """`shared_redis.resolve` is handed this app's own `_live_config`, which
    on_config_reloaded keeps fresh on every worker. `ctx` here belongs to the
    hook's signature, not to a config change — the app that changed is almost
    never this one, so `ctx.config` carries nothing new to read."""
    seen: list[dict] = []
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "enabled", lambda: True)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "bump_generation", lambda url: None)

    def _resolve(config=None):
        seen.append(config)
        return "redis://stub:6379/0"

    monkeypatch.setattr(plugin_mod.shared_redis_mod, "resolve", _resolve)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["shared_redis_url"] = "redis://from-live-config:6379/0"

    asyncio.run(plugin.on_workspace_mcp_changed(_StubCtx({"shared_redis_url": "unused"})))

    assert seen == [plugin._live_config]
