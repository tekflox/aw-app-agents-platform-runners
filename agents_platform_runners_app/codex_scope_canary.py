"""Live canary automating card 4085bf3b-9510-81ba-adc3-fee01f6cee37's own
acceptance criterion: *"An agent declaring no MCP servers opens **no** MCP
transport — observable in the gateway access log showing zero handshakes
from that agent's container, not merely inferred from the agent behaving
correctly."*

Spawns one real, throwaway codex job through this app's own
``execute._build_container_kwargs`` — the SAME code path every production
run takes, no shortcut — with ``mcp_servers`` deliberately absent (the exact
shape ``knowledgeable-nl-adapter`` dispatches with: ``disable_harness_tools``
plus no declared MCP servers, the shape this whole card exists for). After
it exits, checks whether the ``aw-app-mcp-gateway`` container logged any
``/mcp`` traffic while it ran.

**Honest limitation, not a hidden one.** The gateway has no per-run
attribution in its own access log — confirmed live 2026-10-30: no file,
Redis key, DB table, or endpoint anywhere in ``aw-mcp-gateway`` persists a
per-call record keyed by caller/run_id; ``caller_context.py`` resolves
``X-Aw-Caller-Run-Id``/``X-Aw-Warm-Token`` only into an in-request
contextvar, never to a durable store. So "clean" here means "the gateway
logged zero ``/mcp`` lines during this canary's own narrow time window", not
"this specific container made none" — a concurrent unrelated agent dialing
the gateway in that same window would read as a false alarm. Run this when
the gateway is otherwise quiet (the caller decides when — see the
``/codex-scope-canary/doctor`` route in routes.py, invoked on demand, never
on a health-check cadence); treat a red result as "go read the gateway's
own logs for that window", not as self-sufficient proof.

This is deliberately NOT a cheap check (unlike the autostart probe in
``degradations.py``): it spawns a real container and waits for codex to run
a turn, so it costs real wall-clock time and a real turn against whatever
account this workspace's codex is logged into. That is why it lives behind
its own named doctor entry rather than the fast health-check path, and why
nothing calls it proactively — see ``src/apps/degradations.py``'s own
docstring on keeping ``/api/health`` cheap.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

log = logging.getLogger(__name__)

#: Container name aw-mcp-gateway runs under on this workspace's own engine —
#: the same hostname every rendered codex config.toml dials (see
#: execute.py's live shared config.toml: `http://aw-app-mcp-gateway:9200/mcp`).
GATEWAY_CONTAINER_NAME = "aw-app-mcp-gateway"

#: uvicorn's default access-log line for a call into the MCP route, in
#: either form the gateway serves it (unscoped root `/mcp` or a scoped
#: `/mcp/<profile>`). Matched against the gateway container's raw stdout.
_MCP_ACCESS_LINE_RE = re.compile(r'"(?:POST|GET)\s+/mcp(?:/\S*)?\s+HTTP/[\d.]+"\s+\d{3}')


def _gateway_mcp_traffic_since(client: Any, since_ts: float) -> list[str]:
    """``/mcp`` access-log lines the gateway container emitted at or after
    ``since_ts`` (a Unix timestamp) — see this module's own docstring for
    why this is a window check, not a per-run one."""
    gateway = client.containers.get(GATEWAY_CONTAINER_NAME)
    raw = gateway.logs(since=int(since_ts), stdout=True, stderr=True)
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return [line for line in text.splitlines() if _MCP_ACCESS_LINE_RE.search(line)]


def run_canary(client: Any, *, run_id: str | None = None, timeout_s: float = 90.0) -> dict:
    """Spawn one throwaway, no-MCP codex job and report whether the gateway
    saw any ``/mcp`` traffic while it ran.

    Never raises on the canary job's OWN failure to spawn or exit cleanly
    (an unrelated infra hiccup is a different problem from what this exists
    to catch) — that surfaces as ``result["error"]`` instead, and the
    caller (the ``/codex-scope-canary/doctor`` route) decides how to report
    it. Only a `RuntimeError` resolving the gateway container itself
    propagates, since without it this canary cannot answer its own
    question at all.
    """
    from . import execute as execute_mod  # local import — avoids a routes.py<->this import cycle

    run_id = run_id or f"canary-{uuid.uuid4().hex[:12]}"
    job = {
        "run_id": run_id, "cli": "codex",
        "prompt": "Reply with exactly the single word: ok",
        # Deliberately ABSENT: mcp_servers. This is the exact shape
        # knowledgeable-nl-adapter dispatches with (disable_harness_tools +
        # no declared MCP servers) — the one this card exists for.
    }

    result: dict = {"run_id": run_id}
    started_at = time.time()
    container = None
    try:
        image, _argv, kwargs, _mcp_path = execute_mod._build_container_kwargs(job, client)
        container = client.containers.run(image, **kwargs)
        exit_info = container.wait(timeout=timeout_s)
        result["container_exit_code"] = (
            exit_info.get("StatusCode") if isinstance(exit_info, dict) else exit_info)
    except Exception as exc:  # noqa: BLE001 — reported, not raised; see docstring
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["ok"] = False
        return result
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                log.warning("codex_scope_canary: could not remove canary container "
                            "for run=%s", run_id, exc_info=True)

    gateway_mcp_hits = _gateway_mcp_traffic_since(client, started_at)
    result["gateway_mcp_access_lines"] = gateway_mcp_hits
    result["ok"] = not gateway_mcp_hits
    return result
