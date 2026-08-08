#!/usr/bin/env python3
"""aw-warm-relay.py — publishes a persistent warm claude container's stdout
to Redis, one turn's worth at a time.

Runs inside the container (spawned by aw-warm-wrapper), reading claude's
stdout on its own stdin. Each line is tagged with whichever run_id is
CURRENT — read from ``<rundir>/current_run_id``, written by
``warm_pool.dispatch_turn()`` (via `docker exec`, from the agents-platform
host process) immediately BEFORE that turn's prompt is fed into the FIFO —
and published exactly like aw-connector-redis does:
``{"type": "stdout", "line": <line>}`` on stream ``run:{run_id}:events``.
cli.py's existing Redis-stream consumer (``consume_stream_into_queue``)
therefore needs zero changes to read a warm turn's output.

A claude stream-json turn ends with a ``{"type":"result",...}`` event. The
moment one is seen, this also publishes that stream's "done" sentinel
(``{"done": "1", "returncode": "0"}``) so the consumer — which is waiting
for exactly that — finalises the run normally. The relay process itself
never exits between turns; only the wrapper's drain/TTL logic ends it.

Under the per-session warm-pool redesign (2026-07-24), the container's own
session_id is known BEFORE spawn (it's the container's key —
`aw-warm-<agent_id>-<session_id>`), so it's baked in as a plain static
`AW_SESSION_ID` env var at `docker run` time (see docker_agent.py's warm_mode
branch / cli.py's `_warm_get_or_create`) — this relay no longer needs to
parse it out of claude's `system`/`init` event turn-by-turn.

Usage: aw-warm-relay.py <rundir>   (reads claude's stdout on its own stdin)
"""
from __future__ import annotations

import json
import os
import sys

import redis


def main() -> int:
    if len(sys.argv) < 2:
        print("aw-warm-relay: missing <rundir> argument", file=sys.stderr)
        return 1
    rundir = sys.argv[1]
    run_id_file = os.path.join(rundir, "current_run_id")
    redis_url = os.environ.get("AW_REDIS_URL", "redis://host.docker.internal:6379/0")

    r = redis.from_url(redis_url, socket_connect_timeout=10, socket_timeout=10)

    def current_run_id() -> str:
        try:
            with open(run_id_file, "r", encoding="utf-8") as f:
                return f.read().strip() or "unknown"
        except OSError:
            return "unknown"

    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line:
            continue
        run_id = current_run_id()
        stream_key = f"run:{run_id}:events"
        try:
            r.xadd(stream_key, {"type": "stdout", "line": line}, maxlen=50_000, approximate=True)
        except Exception as e:
            print(f"aw-warm-relay: XADD failed ({e})", file=sys.stderr)
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "result":
            try:
                r.xadd(stream_key, {"done": "1", "returncode": "0"}, maxlen=50_000, approximate=True)
                r.expire(stream_key, 86400)
            except Exception as e:
                print(f"aw-warm-relay: done-sentinel XADD failed ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
