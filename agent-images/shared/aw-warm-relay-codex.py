#!/usr/bin/env python3
"""aw-warm-relay-codex.py — owns a persistent `codex app-server --stdio`
process for a warm codex container, translating between:

  * host->container: one JSON line per turn (``{"prompt": "..."}"``), read
    from this process's own stdin — the wrapper hands it the FIFO's read
    end, same mechanism warm_pool.dispatch_turn() uses for claude, just a
    plain-object payload instead of claude's stream-json envelope (see
    warm_pool.dispatch_turn's cli branch). JSON (not a raw text line) so a
    prompt containing embedded newlines still survives as ONE turn.
  * container->host: every JSON-RPC line codex's app-server writes to ITS
    OWN stdout is republished to Redis verbatim (``{"type":"stdout","line":
    <line>}``), same schema/stream key aw-warm-relay.py uses for claude — so
    agents-platform's existing consumer needs zero changes to read a warm
    codex turn's output. codex's own event shape is used as-is (NOT
    translated into claude's): the cold/ephemeral codex path (execute.py's
    ``_build_container_kwargs``, ``--json`` flag) already publishes codex's
    native event shape to the same Redis stream, and the consumer already
    handles it.

Unlike claude (simple stream-json lines, no request/response correlation),
codex's app-server speaks real JSON-RPC: a turn is a ``turn/start`` request
inside a thread created once by ``thread/start``, and completion is
signalled by an ASYNC ``turn/completed`` notification (matched by
threadId) — never by the turn/start request's own ack, which only ever
means "accepted, now in progress". So this script, unlike the claude
wrapper/relay split, owns BOTH directions of the app-server connection
itself in one process, to keep that id/thread state in one place instead
of splitting it across two.

Known limitation (v1): the thread lives only as long as this container.
Warm containers are per-session already (aw-warm-<agent_id>-<session_id>),
so this only matters across a respawn (epoch bump / TTL expiry / drain) —
a fresh container starts a fresh codex thread rather than resuming the
old one's rollout. claude's warm path does resume (--resume <session_id>
at spawn) because claude lets a caller pick the session id up front;
codex's thread ids are server-generated (UUIDv7), so resuming a SPECIFIC
prior thread across a respawn needs a persisted thread_id lookup this
version doesn't have yet.

Usage: aw-warm-relay-codex.py <rundir>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import redis

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
    print(f"aw-warm-relay-codex: attachment rewriting disabled ({_e})", file=sys.stderr)

THREAD_START_TIMEOUT_S = 30.0


def main() -> int:
    if len(sys.argv) < 2:
        print("aw-warm-relay-codex: missing <rundir> argument", file=sys.stderr)
        return 1
    rundir = sys.argv[1]
    run_id_file = os.path.join(rundir, "current_run_id")
    redis_url = os.environ.get("AW_REDIS_URL", "redis://host.docker.internal:6379/0")
    cwd = os.environ.get("AW_CODEX_CWD", "/opt/aw-workspace")

    r = redis.from_url(redis_url, socket_connect_timeout=10, socket_timeout=10)

    def current_run_id() -> str:
        try:
            with open(run_id_file, "r", encoding="utf-8") as f:
                return f.read().strip() or "unknown"
        except OSError:
            return "unknown"

    proc = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )

    _id_counter = [0]
    _id_lock = threading.Lock()

    def send(method: str, params: dict, notif: bool = False) -> int | None:
        with _id_lock:
            if notif:
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
                proc.stdin.flush()
                return None
            _id_counter[0] += 1
            mid = _id_counter[0]
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            return mid

    # thread_id resolves once (first turn's thread/start) and is reused by
    # every following turn/start for this container's whole lifetime — see
    # the module docstring's "known limitation" note on why a RESPAWNED
    # container can't resume a previous one's thread yet.
    state = {"thread_id": None, "pending_thread_start": None}
    state_lock = threading.Lock()

    def reader_loop() -> None:
        """Pumps codex app-server's stdout: republishes every line to Redis
        under the CURRENT run_id, resolves thread_id off the thread/start
        response, and watches for turn/completed to emit the done sentinel
        (mirrors aw-warm-relay.py's ``{"type":"result"}`` watch for claude,
        just on a different event shape)."""
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            run_id = current_run_id()
            stream_key = f"run:{run_id}:events"
            out_line = line
            if aw_attach is not None:
                try:
                    out_line = aw_attach.rewrite_stream_line(out_line, run_id)
                except Exception as e:
                    print(f"aw-warm-relay-codex: attach rewrite failed ({e})", file=sys.stderr)
            try:
                r.xadd(stream_key, {"type": "stdout", "line": out_line}, maxlen=50_000, approximate=True)
            except Exception as e:
                print(f"aw-warm-relay-codex: XADD failed ({e})", file=sys.stderr)

            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            with state_lock:
                mid = evt.get("id")
                if mid is not None and mid == state["pending_thread_start"]:
                    result = evt.get("result") or {}
                    state["thread_id"] = (result.get("thread") or {}).get("id")
                    state["pending_thread_start"] = None

            if evt.get("method") == "turn/completed":
                params = evt.get("params") or {}
                with state_lock:
                    is_ours = params.get("threadId") == state.get("thread_id")
                if is_ours:
                    try:
                        r.xadd(stream_key, {"done": "1", "returncode": "0"}, maxlen=50_000, approximate=True)
                        r.expire(stream_key, 86400)
                    except Exception as e:
                        print(f"aw-warm-relay-codex: done-sentinel XADD failed ({e})", file=sys.stderr)

    threading.Thread(target=reader_loop, daemon=True, name="codex-stdout-reader").start()

    # Handshake, once, before the first turn. clientName/clientVersion is
    # deliberately absent here — app-server (unlike the separate exec-server
    # stub) wants the nested clientInfo shape, confirmed live 2026-08-14.
    send("initialize", {"clientInfo": {"name": "aw-warm-codex", "title": "AW Warm Codex", "version": "1.0"}})
    time.sleep(1.0)  # best-effort courtesy pause, not a hard requirement
    send("initialized", {}, notif=True)

    for raw in sys.stdin:
        raw = raw.rstrip("\n")
        if not raw:
            continue
        try:
            turn = json.loads(raw)
            prompt = turn.get("prompt") or ""
        except json.JSONDecodeError:
            print(f"aw-warm-relay-codex: dropping malformed turn line: {raw[:200]!r}", file=sys.stderr)
            continue
        if not prompt:
            continue

        with state_lock:
            thread_id = state["thread_id"]
        if thread_id is None:
            with state_lock:
                state["pending_thread_start"] = send("thread/start", {"cwd": cwd})
            deadline = time.monotonic() + THREAD_START_TIMEOUT_S
            while time.monotonic() < deadline:
                with state_lock:
                    if state["thread_id"]:
                        thread_id = state["thread_id"]
                        break
                time.sleep(0.05)
            if thread_id is None:
                print("aw-warm-relay-codex: thread/start did not resolve within "
                      f"{THREAD_START_TIMEOUT_S:.0f}s — dropping this turn", file=sys.stderr)
                continue

        send("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]})

    proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
