"""OpenTelemetry wiring for the **stdio MCP child** (``mcp_server.py``) — the
one process in this app that aw-workspace core's own ``src/api/otel.py`` does
not already cover.

Everything else this app ships runs IN the core process (``aw-app.json``'s
``tier: "inprocess"``), so core's root-logger OTLP handler and its
process-global ``HTTPXClientInstrumentor`` already export it, under
``service.name = "aw-workspace"`` with ``scope_name`` naming the app. This
module exists for the exception: ``python3 -m
agents_platform_runners_app.mcp_server`` is spawned by **aw-mcp-gateway**, as
a stdio child inside the *gateway's* container, with only the env baked into
this app's generated ``mcp.json`` (``plugin.build_mcp_servers``). Nothing of
core's process reaches it — so until this existed, the 13 httpx call sites
every agent's write path goes through (``run_agent_async``,
``mark_flow_done``, …) emitted no spans, propagated no ``traceparent`` to
agents-platform-multitenant, and reached SigNoz only as naked
container-stdout text with no ``service.name`` and no ``trace_id``. Kanban
``observability:runners-app-no-otel-instrumentation``.

Four hard constraints shape the code below, all of them things that would
otherwise break the upstream rather than merely lose telemetry:

* **Guarded, always.** The opentelemetry SDK is NOT a dependency of this app
  — it has to be present in the *gateway's* image
  (``repos/aw-mcp-gateway/back/requirements.txt``), a different repo on a
  different release cadence. A hard top-level import here means an
  ``ImportError`` at spawn, a dead upstream, and **zero** Agents Platform
  tools for every agent on the workspace, silently. So: import inside the
  function, ``ImportError`` is a no-op, and ``setup()`` is a no-op when
  there is no ``OTEL_EXPORTER_OTLP_ENDPOINT`` to export to.
* **stderr only, never stdout.** stdout is the JSON-RPC channel
  (``mcp_server._async_main`` writes responses there); one line of
  diagnostics on it corrupts the protocol and kills the upstream. Every
  handler installed below writes to stderr.
* **Do not instrument ``requests``.** ``opentelemetry-exporter-otlp-proto-
  http`` is built on it, so instrumenting it makes the exporter trace its own
  POST — a feedback loop against a live SigNoz. Core skips it for exactly
  this reason (``src/api/otel.py``'s comment above ``HTTPXClientInstrumentor``).
* **Never log the env.** It carries ``AGENTS_PLATFORM_TOKEN`` and
  ``AW_WORKSPACE_API_KEY``.

Unlike core's module there is no runtime-swappable target here: this process's
whole lifetime is one gateway upstream session, and the gateway respawns it
whenever ``mcp.json`` changes (``contributes.mcp.reload_on_save``) — which is
exactly when the endpoint can change. A fixed exporter per process is
therefore correct, and far less machinery than core's dynamic one.
"""
from __future__ import annotations

import atexit
import logging
import os
import socket
import sys
from contextlib import contextmanager

log = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = "aw-app-agents-platform-runners"

#: Same amplification guard as core's (``src/api/otel.py``'s
#: ``_AMPLIFICATION_PREFIXES``), and found the same way: httpx logs an INFO
#: line per request, including the OTLP log exporter's own successful POST to
#: ``/v1/logs``, which becomes the next batch's payload forever. The method,
#: URL and status are already on the span HTTPXClientInstrumentor emits, so
#: dropping them from LOGS costs nothing.
_AMPLIFICATION_PREFIXES = ("opentelemetry", __name__, "httpx")

_tracer = None
_initialized = False


class _NoAmplification(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_AMPLIFICATION_PREFIXES)


def enabled() -> bool:
    """Whether :func:`setup` actually wired anything up in this process."""
    return _tracer is not None


def setup() -> bool:
    """Build this process's OTel graph, once. Returns whether it is on.

    Idempotent and silent on every failure path — a child that cannot export
    telemetry must still serve tools.
    """
    global _tracer, _initialized
    if _initialized:
        return enabled()
    _initialized = True

    endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip().rstrip("/")
    if not endpoint:
        # Observability is off for this workspace, or this app's mcp.json was
        # written before core could resolve a target. Not an error.
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # The gateway image does not carry the SDK (yet). Run exactly as this
        # process did before this module existed — see the module docstring
        # for why this can never be a hard import.
        return False

    try:
        # service.name comes from the env so the app's own mcp.json owns the
        # name (plugin.build_mcp_servers), rather than this file hardcoding
        # one the way repos/aw-backend/src/api/app.py does — the bug the
        # Architect called out not to copy.
        resource = Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME") or DEFAULT_SERVICE_NAME,
            "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
            "deployment.environment": os.environ.get("AW_ENV", "production"),
            "workspace.slug": os.environ.get("AW_WORKSPACE", ""),
        })

        # Headers (the destination's X-Api-Key, when there is one) ride the
        # standard OTEL_EXPORTER_OTLP_HEADERS env var, which both exporters
        # read themselves — deliberately never read or logged here.
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
        trace.set_tracer_provider(tracer_provider)

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")))

        # Both handlers, together, for the same reason core does it: this
        # process's root logger has none today, so `logging.lastResort` is
        # what puts WARNING+ on stderr — and adding ANY real handler turns
        # that off. Without the explicit stderr one, installing OTLP export
        # would silently take away the container logs that are, today, the
        # only place this child's errors show up at all.
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.WARNING)
        root.addHandler(stderr_handler)
        otlp_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        otlp_handler.addFilter(_NoAmplification())
        root.addHandler(otlp_handler)

        # The whole point: client spans on the 13 AP-MT call sites, each
        # injecting traceparent so AP-MT's server span joins THIS trace.
        # httpx only — never `requests` (module docstring).
        HTTPXClientInstrumentor().instrument()

        # The gateway kills this child on every mcp.json change and on its own
        # reload; a BatchSpanProcessor holding an unflushed batch would lose
        # the tail of whatever ran last, which is usually the interesting part.
        atexit.register(_flush, tracer_provider, logger_provider)

        _tracer = trace.get_tracer(__name__)
        return True
    except Exception:  # noqa: BLE001 — telemetry must never break the upstream
        log.warning("otel: setup failed — serving with no telemetry", exc_info=True)
        return False


def _flush(tracer_provider, logger_provider) -> None:
    for provider in (tracer_provider, logger_provider):
        try:
            provider.force_flush(5000)
        except Exception:  # noqa: BLE001 — best effort at interpreter exit
            pass


@contextmanager
def tool_span(name: str, caller_run_id: str | None = None):
    """One span per MCP tool call — the root of this child's trace, and the
    parent every httpx client span (and therefore AP-MT's own server span)
    hangs off.

    A plain pass-through when :func:`setup` found nothing to export to, so the
    call site needs no branch of its own.
    """
    if _tracer is None:
        yield None
        return
    try:
        cm = _tracer.start_as_current_span(f"mcp.tool/{name}")
    except Exception:  # noqa: BLE001 — never fail a tool call over a span
        yield None
        return
    with cm as span:
        try:
            span.set_attribute("mcp.tool.name", name)
            # Which agent run is on the other end of this call — the join key
            # back to Agents Platform's own run records. See
            # mcp_server._caller_run_id for where it comes from.
            if caller_run_id:
                span.set_attribute("aw.caller.run_id", caller_run_id)
        except Exception:  # noqa: BLE001 — as above
            pass
        yield span
