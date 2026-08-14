#!/usr/bin/env python3
"""aw-warm-relay-codex.py — runs one `codex exec` per turn inside a warm
container and publishes its output to Redis, exactly like the cold path does.

Replaces the app-server/JSON-RPC version of this relay (2026-08-14, same day
it shipped). That version kept a single `codex app-server --stdio` process
alive and republished its JSON-RPC frames verbatim. The container side of it
worked — wrapper, relay and app-server all came up healthy and turns were
answered — but the frames are the wrong SHAPE for the only consumer that
matters: agents-platform reads a run's session id off its output stream, and
its codex branch (backend/app/core/models/cli.py:1157) understands the
`codex exec --json` event schema — ``thread.started`` / ``item.started`` /
``item.completed`` / ``turn.completed`` — not app-server's JSON-RPC. Nothing
ever matched, so no thread id was captured and EVERY turn looked like a brand
new conversation. Live through the aw-cris bot: turn 1 stored "9091", turn 2
arrived under a different session_id, got its own second warm container, and
answered "não encontrei nenhum número secreto".

So this version stops translating and simply emits the real thing: each turn
is a fresh ``codex exec resume <thread> <prompt> --json`` inside the
already-warm container, whose stdout is byte-for-byte the stream the cold
path produces. agents-platform needs no changes and cannot tell the
difference — which is the point, and is why this needs no knowledge of
app-server's private protocol (which could not be captured from outside
anyway: app-server refuses to start without the sandbox setup the runner's
own spawn provides — "Permission denied (os error 13)", probed 2026-08-14).

What stays warm is the expensive part: container create/start, the image
check, every mount, and the creds copy into $HOME that the cold entrypoint
redoes on every single spawn. What is NOT saved is codex's own process
startup, once per turn. That is the honest trade for a CLI with no
long-lived turn protocol we can speak.

The thread to resume is the container's OWN identity: warm codex containers
are only ever created for a job that already carries a session_id (unlike
claude, mint_warm_session_id() deliberately does not mint for codex), and
that id IS this container's name suffix, handed over as $AW_SESSION_ID.

Usage: aw-warm-relay-codex.py <rundir>   (reads turn lines on stdin)
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

import redis

# aw_attach lives outside the image (it must be editable without a rebuild),
# so its directory goes on sys.path explicitly — same two candidates, in the
# same order, as aw-warm-relay.py uses.
for _cand in (os.path.dirname(os.path.abspath(__file__)), "/usr/local/bin"):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)
try:
    import aw_attach  # type: ignore
except Exception as _e:  # pragma: no cover - depends on the mount
    aw_attach = None
    print(f"aw-warm-relay-codex: aw_attach unavailable ({_e}) — no attach rewriting",
          file=sys.stderr)


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return default


def _turn_env(rundir: str) -> dict:
    """`turn_env` is written by warm_pool.dispatch_turn as shell `export`
    lines (the claude wrapper sources it). Here the turn is a subprocess, so
    parse the same file into an env overlay rather than sourcing it."""
    env = {}
    for line in _read(os.path.join(rundir, "turn_env")).splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        name, _, raw = line[len("export "):].partition("=")
        if not name:
            continue
        try:
            value = shlex.split(raw)[0] if raw.strip() else ""
        except ValueError:
            value = raw.strip().strip("'\"")
        env[name.strip()] = value
    return env


def main() -> int:
    rundir = sys.argv[1] if len(sys.argv) > 1 else "/home/ubuntu/.aw-warm"
    redis_url = os.environ.get("AW_REDIS_URL") or ""
    session_id = os.environ.get("AW_SESSION_ID") or ""
    cwd = os.environ.get("AW_CODEX_CWD") or os.path.expanduser("~")
    if not redis_url:
        print("aw-warm-relay-codex: AW_REDIS_URL is empty — nothing to publish to",
              file=sys.stderr)
        return 1
    if not session_id:
        print("aw-warm-relay-codex: AW_SESSION_ID is empty — a warm codex container "
              "has no thread to resume, refusing to start", file=sys.stderr)
        return 1

    r = redis.from_url(redis_url, decode_responses=True)

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            prompt = (json.loads(raw) or {}).get("prompt") or ""
        except json.JSONDecodeError:
            print(f"aw-warm-relay-codex: dropping malformed turn line: {raw[:200]!r}",
                  file=sys.stderr)
            continue
        if not prompt:
            continue

        # Read AFTER the turn line arrives: dispatch_turn writes current_run_id
        # and turn_env FIRST, then the prompt, precisely so this ordering is safe.
        run_id = _read(os.path.join(rundir, "current_run_id"), "unknown")
        stream_key = f"run:{run_id}:events"

        # Mirrors the cold path's own argv assembly for codex
        # (execute.py::_build_container_kwargs): subcommand, `resume <id>`,
        # the prompt positionally, then skip-approvals, then default_extra.
        # Model is deliberately absent — a ChatGPT-account codex rejects any
        # -c model= override, which is why the cold path drops it too.
        argv = ["codex", "exec", "resume", session_id, prompt,
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check", "--json"]

        env = dict(os.environ)
        env.update(_turn_env(rundir))
        rc = 1
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, env=env)
        except Exception as e:
            print(f"aw-warm-relay-codex: could not start codex ({e})", file=sys.stderr)
            try:
                r.xadd(stream_key, {"type": "stdout", "line": json.dumps({
                    "type": "cli.stderr", "text": f"warm codex failed to start: {e}"})},
                    maxlen=50_000, approximate=True)
            except Exception:
                pass
        else:
            for out_line in proc.stdout:
                out_line = out_line.rstrip("\n")
                if not out_line:
                    continue
                # An [[ATTACH: /local/path]] is readable HERE and nowhere near
                # the Telegram connector — swap it for a reference that side
                # can resolve, same as the claude relay does.
                if aw_attach is not None:
                    try:
                        out_line = aw_attach.rewrite_stream_line(out_line, run_id)
                    except Exception as e:
                        print(f"aw-warm-relay-codex: attach rewrite failed ({e})",
                              file=sys.stderr)
                try:
                    r.xadd(stream_key, {"type": "stdout", "line": out_line},
                           maxlen=50_000, approximate=True)
                except Exception as e:
                    print(f"aw-warm-relay-codex: XADD failed ({e})", file=sys.stderr)
            rc = proc.wait()

        # The cold path's consumer finalises a run on this sentinel and on
        # nothing else — codex's own turn.completed event is usage data, not
        # an end-of-stream marker, so the process exiting is what ends a turn.
        try:
            r.xadd(stream_key, {"done": "1", "returncode": str(rc)},
                   maxlen=50_000, approximate=True)
            r.expire(stream_key, 86400)
        except Exception as e:
            print(f"aw-warm-relay-codex: done-sentinel XADD failed ({e})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
