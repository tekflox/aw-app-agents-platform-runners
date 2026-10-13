"""mcp_otel.py + otel_env.py — the OTel wiring for the MCP child
(Kanban observability:runners-app-no-otel-instrumentation).

The point of this suite is the NO-OP paths. The child is spawned by
aw-mcp-gateway inside the gateway's own container, whose image is released
from a different repo on its own cadence — so "the opentelemetry SDK isn't
installed" and "there is no OTLP endpoint configured" are both normal states,
and in both of them the server has to serve tools exactly as it did before
any of this existed. A hard import or an unguarded setup there is not a lost
trace, it is a dead upstream and zero Agents Platform tools for every agent on
the workspace, silently.
"""
from __future__ import annotations

import builtins
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_otel, otel_env  # noqa: E402


def _fresh(monkeypatch):
    """A re-imported mcp_otel with its once-per-process latch cleared."""
    monkeypatch.setattr(mcp_otel, "_initialized", False, raising=False)
    monkeypatch.setattr(mcp_otel, "_tracer", None, raising=False)
    return mcp_otel


# --- mcp_otel.setup: the guarded paths ---------------------------------------


def test_setup_is_a_noop_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    otel = _fresh(monkeypatch)

    assert otel.setup() is False
    assert otel.enabled() is False


def test_setup_survives_the_sdk_not_being_installed(monkeypatch):
    """The gateway image may not carry opentelemetry yet — the whole reason
    every import in mcp_otel.py is inside the function. Simulated by making
    `import opentelemetry...` raise, which is exactly what the real gateway
    container did before its requirements.txt gained the SDK."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://aw-app-signoz:8080")
    real_import = builtins.__import__

    def _no_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_otel)
    otel = _fresh(monkeypatch)

    assert otel.setup() is False
    assert otel.enabled() is False


def test_tool_span_is_a_pass_through_when_disabled(monkeypatch):
    """The call site in mcp_server._call_tool has no branch of its own, so
    this context manager must work uninstrumented."""
    _fresh(monkeypatch)
    with mcp_otel.tool_span("list_runs", "run-123") as span:
        assert span is None


def test_the_server_still_dispatches_tools_with_the_sdk_missing(monkeypatch):
    """The end-to-end form of the two tests above: import the real server,
    run a tool call through the real `_call_tool` wrapper with no SDK and no
    endpoint, and get the tool's answer back."""
    import asyncio

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    _fresh(monkeypatch)
    from agents_platform_runners_app import mcp_server

    assert mcp_otel.setup() is False
    out = asyncio.run(mcp_server._call_tool("definitely-not-a-tool", {}))

    # The dispatcher's own unknown-tool answer, i.e. the span wrapper neither
    # swallowed nor rewrote it.
    assert "unknown tool" in out[0].text


def test_setup_keeps_stderr_logging_alive(monkeypatch):
    """Installing an OTLP handler on the root logger turns `logging.lastResort`
    off, which would silently take away the container logs that are today the
    only place this child's errors appear. Both handlers go on together.

    Runs the real `setup()` with the two OTLP exporters swapped for in-memory
    ones: the handler wiring is the point, and a real exporter here would
    both spam name-resolution retries and leave this test process pointed at
    a SigNoz that isn't there."""
    try:
        from opentelemetry.exporter.otlp.proto.http import _log_exporter, trace_exporter
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
    except ImportError:  # pragma: no cover — SDK absent, the no-op tests cover it
        pytest.skip("opentelemetry SDK not installed in this environment")

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter",
                        lambda **_kw: InMemorySpanExporter())
    monkeypatch.setattr(_log_exporter, "OTLPLogExporter",
                        lambda **_kw: InMemoryLogRecordExporter())
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://aw-app-signoz:8080")
    root = logging.getLogger()
    before = list(root.handlers)
    otel = _fresh(monkeypatch)
    try:
        assert otel.setup() is True
        streams = [h for h in root.handlers
                   if h not in before and isinstance(h, logging.StreamHandler)]
        assert any(getattr(h, "stream", None) is sys.stderr for h in streams), \
            "no stderr handler installed alongside the OTLP one"
        # stdout is the JSON-RPC channel — a handler there bricks the upstream.
        assert not any(getattr(h, "stream", None) is sys.stdout
                       for h in root.handlers)
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)


# --- otel_env: what gets baked into the child's mcp.json ---------------------


def test_child_env_is_empty_without_aw_workspace_core():
    """Standalone/dev mode and this very test run: aw-workspace core is not
    importable, so the env keys are omitted rather than written blank."""
    assert otel_env.child_env() == {}
    assert otel_env.status() == {"env_injected": False,
                                 "reason": "core-helper-unavailable"}


def test_child_env_translates_cores_answer(monkeypatch):
    fake_core = type(sys)("src.api.otel")
    fake_core.export_target_for_sibling_container = lambda: {
        "endpoint": "http://aw-app-signoz:8080",
        "headers": {"X-Api-Key": "deadbeef"},
    }
    monkeypatch.setitem(sys.modules, "src", type(sys)("src"))
    monkeypatch.setitem(sys.modules, "src.api", type(sys)("src.api"))
    monkeypatch.setitem(sys.modules, "src.api.otel", fake_core)

    env = otel_env.child_env()

    assert env == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://aw-app-signoz:8080",
        "OTEL_SERVICE_NAME": "aw-app-agents-platform-runners",
        "OTEL_EXPORTER_OTLP_HEADERS": "X-Api-Key=deadbeef",
    }


def test_status_never_reports_the_credential(monkeypatch):
    fake_core = type(sys)("src.api.otel")
    fake_core.export_target_for_sibling_container = lambda: {
        "endpoint": "http://aw-app-signoz:8080",
        "headers": {"X-Api-Key": "deadbeef"},
    }
    monkeypatch.setitem(sys.modules, "src", type(sys)("src"))
    monkeypatch.setitem(sys.modules, "src.api", type(sys)("src.api"))
    monkeypatch.setitem(sys.modules, "src.api.otel", fake_core)

    status = otel_env.status()

    assert status["env_injected"] is True
    assert status["auth_header"] is True
    assert "deadbeef" not in repr(status)


def test_no_target_omits_the_env_entirely(monkeypatch):
    """Observability off, or core hasn't resolved yet (it does so after every
    app's activate() on a cold boot) — not an error either way."""
    fake_core = type(sys)("src.api.otel")
    fake_core.export_target_for_sibling_container = lambda: None
    monkeypatch.setitem(sys.modules, "src", type(sys)("src"))
    monkeypatch.setitem(sys.modules, "src.api", type(sys)("src.api"))
    monkeypatch.setitem(sys.modules, "src.api.otel", fake_core)

    assert otel_env.child_env() == {}
    assert otel_env.status()["reason"] == "no-observability-target"


def test_build_mcp_servers_merges_the_otel_env(monkeypatch):
    """The actual seam: plugin.build_mcp_servers is what writes the child's
    env block, and the OTEL keys must land there without disturbing the four
    that were already baked in."""
    from agents_platform_runners_app import otel_env as otel_env_mod
    from agents_platform_runners_app import plugin as plugin_mod

    monkeypatch.setattr(otel_env_mod, "child_env", lambda: {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://aw-app-signoz-connector:4318",
        "OTEL_SERVICE_NAME": "aw-app-agents-platform-runners",
    })

    env = plugin_mod.build_mcp_servers({"agents_platform_token": "t"})[
        "agents-platform-runners"]["env"]

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://aw-app-signoz-connector:4318"
    assert env["OTEL_SERVICE_NAME"] == "aw-app-agents-platform-runners"
    assert env["AGENTS_PLATFORM_TOKEN"] == "t"


def test_write_mcp_json_skips_an_unchanged_write(tmp_path, monkeypatch):
    """aw-mcp-gateway reloads on mtime and each reload briefly drops every
    tool it proxies, so the repeat callers this file now has (the refresh
    watchdog, /register-observability, one activate() per worker) must not
    rewrite identical content."""
    from agents_platform_runners_app import plugin as plugin_mod

    plugin_mod.write_mcp_json(str(tmp_path), {})
    path = tmp_path / "mcp.json"
    stamped = path.stat().st_mtime_ns
    path_text = path.read_text(encoding="utf-8")

    plugin_mod.write_mcp_json(str(tmp_path), {})

    assert path.stat().st_mtime_ns == stamped
    assert path.read_text(encoding="utf-8") == path_text


def test_write_mcp_json_writes_when_the_otel_env_appears(tmp_path, monkeypatch):
    from agents_platform_runners_app import otel_env as otel_env_mod
    from agents_platform_runners_app import plugin as plugin_mod

    plugin_mod.write_mcp_json(str(tmp_path), {})
    monkeypatch.setattr(otel_env_mod, "child_env", lambda: {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://aw-app-signoz:8080"})

    doc = plugin_mod.write_mcp_json(str(tmp_path), {})

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in (tmp_path / "mcp.json").read_text()
    assert doc["mcpServers"]["agents-platform-runners"]["env"][
        "OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://aw-app-signoz:8080"
