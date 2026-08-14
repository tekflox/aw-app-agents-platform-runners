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

A claude stream-json turn ends with a ``{"type":"result",...}`` event. On
the result that ends the DISPATCHED turn, this also publishes that stream's
"done" sentinel (``{"done": "1", "returncode": "0"}``) so the consumer —
which is waiting for exactly that — finalises the run normally. The relay
process itself never exits between turns; only the wrapper's drain/TTL
logic ends it.

**Not every result ends a dispatch.** claude re-invokes itself when work it
backgrounded finishes (a `run_in_background` Agent/Bash task, a scheduled
wakeup), and each of those continuations emits its own `result` — tagged
``"origin": {"kind": "task-notification"}`` (or another harness kind). The
turn fed in over the FIFO is the only one with no ``origin`` at all.

Treating every result as terminal, which this did until 2026-08-14, breaks
the NEXT run rather than the one that continued. The container is warm and
outlives the dispatch, so a continuation's lines are tagged with whatever
``current_run_id`` holds when they arrive — and if a new turn has been
dispatched by then, that stale continuation's `result` publishes a "done"
onto the LIVE run's stream. agents-platform's consumer stops at the first
done it sees (`core/redis_streams.py`, `if fields.get("done"): return`), so
the innocent run is finalised early with a truncated answer and everything
it goes on to produce is written to a stream nobody is reading. Observed
live: run 15032895… published two done sentinels 6 minutes apart, the second
after a background agent came back.

So: publish the sentinel only for a result with no ``origin``, and only once
per run_id. A dispatched turn always emits exactly one such result, so this
can never leave a run unfinalised — it only withholds the sentinels that
were never ours to send.

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

# aw_attach lives outside the image (it must be editable without a rebuild),
# so its directory goes on sys.path explicitly. Two candidates, in order:
#   1. next to this script — execute.py bind-mounts it there;
#   2. the workspace tree, which every agent container already mounts.
# (2) is not redundant: it makes the rewrite work on the NEXT warm container
# after a deploy, without waiting for a workspace restart to pick up the new
# mount in _build_warm_kwargs.
for _cand in (
    os.path.dirname(os.path.abspath(__file__)),
    os.path.join(os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace"),
                 "apps", "agents-platform-runners", "agent-images", "shared"),
):
    if os.path.isfile(os.path.join(_cand, "aw_attach.py")):
        sys.path.insert(0, _cand)
        break
try:
    import aw_attach
except Exception as _e:  # pragma: no cover - a missing helper must not kill the relay
    aw_attach = None
    print(f"aw-warm-relay: attachment rewriting disabled ({_e})", file=sys.stderr)


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

    # run_ids already finalised. The relay outlives every run it serves, so
    # this is what makes the sentinel idempotent across turns.
    finalised: set[str] = set()

    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line:
            continue
        run_id = current_run_id()
        stream_key = f"run:{run_id}:events"
        # An [[ATTACH: /local/path]] the agent wrote is readable HERE and
        # nowhere near the Telegram connector's container — swap it for a
        # reference that side can resolve before the line leaves this host.
        # See aw_attach.py's module docstring.
        if aw_attach is not None:
            try:
                line = aw_attach.rewrite_stream_line(line, run_id)
            except Exception as e:
                print(f"aw-warm-relay: attach rewrite failed ({e})", file=sys.stderr)
        try:
            r.xadd(stream_key, {"type": "stdout", "line": line}, maxlen=50_000, approximate=True)
        except Exception as e:
            print(f"aw-warm-relay: XADD failed ({e})", file=sys.stderr)
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "result":
            continue
        # A continuation claude started on its own (background task finished,
        # wakeup fired) — the dispatch it belongs to is already over, and
        # run_id here may well name somebody else's live run. Never ours to
        # finalise. See the module docstring.
        if evt.get("origin") is not None:
            print(f"aw-warm-relay: continuation result ({evt['origin']}) — "
                  f"not finalising run={run_id}", file=sys.stderr)
            continue
        if run_id in finalised:
            print(f"aw-warm-relay: run={run_id} already finalised — "
                  f"skipping duplicate done", file=sys.stderr)
            continue
        try:
            r.xadd(stream_key, {"done": "1", "returncode": "0"}, maxlen=50_000, approximate=True)
            r.expire(stream_key, 86400)
            finalised.add(run_id)
        except Exception as e:
            print(f"aw-warm-relay: done-sentinel XADD failed ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
