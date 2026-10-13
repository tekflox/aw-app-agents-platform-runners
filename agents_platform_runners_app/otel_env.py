"""The OTel env this app bakes into the MCP child's ``mcp.json``.

``mcp_server.py`` runs in **aw-mcp-gateway's** container, not this one, so the
only way to tell it where to export is the env block of the ``mcp.json`` this
app generates (``plugin.build_mcp_servers``). What goes in that block is not
this app's decision to make: aw-workspace core owns the Observability setting
and its precedence rules (aw-app-signoz-connector forwards for everything
when installed, otherwise the local signoz app's INTERNAL container URL,
otherwise a custom endpoint, otherwise nothing). So this module asks core —
``src.api.otel.export_target_for_sibling_container`` — and translates the
answer into the standard OTLP env var names.

Guarded on purpose, twice over:

* the import of core is ``try``/``except ImportError``, because this package
  also runs standalone (``__main__.py``, port 9407) and under its own pytest,
  neither of which has aw-workspace core importable;
* an unresolvable target means the env keys are **omitted**, not written
  empty — ``mcp_otel.setup()`` treats a missing endpoint as "telemetry off"
  and serves tools exactly as before.

The failure mode that guard creates is silence, which is why
:func:`status` exists and why ``routes.py``'s ``/status`` reports it: if core
ever renames that helper, the only visible symptom would be the child quietly
losing its telemetry again.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aw_apps.agents_platform_runners.otel_env")

#: ``service.name`` for the MCP child. Its own app slug — NOT "aw-workspace",
#: which is what this app's in-core half correctly reports (core's resource),
#: and NOT a hardcoded string inside mcp_otel.py (the bug in
#: repos/aw-backend/src/api/app.py the Architect flagged not to copy).
SERVICE_NAME = "aw-app-agents-platform-runners"

#: Reason strings for :func:`status`, so a broken injection is greppable.
_NO_CORE = "core-helper-unavailable"
_NO_TARGET = "no-observability-target"
_OK = "injected"


def _resolve() -> tuple[dict[str, str] | None, str]:
    try:
        from src.api.otel import export_target_for_sibling_container
    except ImportError:
        return None, _NO_CORE
    target = export_target_for_sibling_container()
    if not target or not target.get("endpoint"):
        # Observability is off, or core has not resolved a target yet (it
        # does so at the END of the boot reconcile, i.e. after every app's
        # activate() — see that helper's docstring). Both are normal.
        return None, _NO_TARGET
    return target, _OK


def child_env() -> dict[str, str]:
    """``{OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS,
    OTEL_SERVICE_NAME}`` for the MCP child, or ``{}`` when there is nothing to
    export to. Never raises — it is one of several contributions to an env
    dict that must still be built."""
    try:
        target, _reason = _resolve()
    except Exception:  # noqa: BLE001 — mcp.json generation must never fail on this
        log.warning("otel_env: could not resolve an OTLP target for the MCP child",
                    exc_info=True)
        return {}
    if target is None:
        return {}
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": str(target["endpoint"]),
        "OTEL_SERVICE_NAME": SERVICE_NAME,
    }
    headers = target.get("headers") or {}
    if headers:
        # W3C-baggage form, the OTLP spec's own encoding for this variable —
        # both exporters in the child read it themselves. The value is a
        # credential for the destination, so it is written here and never
        # read back, logged, or reported by status() below.
        env["OTEL_EXPORTER_OTLP_HEADERS"] = ",".join(
            f"{k}={v}" for k, v in headers.items())
    return env


def status() -> dict:
    """What ``/status`` reports: enough to tell "telemetry is off" from "the
    core helper vanished under us", with no credential in it."""
    try:
        target, reason = _resolve()
    except Exception as exc:  # noqa: BLE001 — a status route never 500s on this
        return {"env_injected": False, "reason": f"error: {type(exc).__name__}"}
    if target is None:
        return {"env_injected": False, "reason": reason}
    return {
        "env_injected": True,
        "reason": reason,
        "endpoint": str(target["endpoint"]),
        "service_name": SERVICE_NAME,
        # Whether an auth header rides along, never which one. No header is
        # the NORMAL case with aw-app-signoz-connector installed — it holds
        # the destination's credential itself.
        "auth_header": bool(target.get("headers")),
    }
