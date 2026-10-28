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
import queue
import random
import shlex
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

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


# codex's own $CODEX_HOME is ONE directory shared by every concurrent codex
# session this runner spawns (execute.py::_cli_home_rel — "one shared codex
# home", deliberate since commit 106d993). It holds state_5.sqlite in WAL
# mode; codex-rs itself explicitly sets a flat 5s busy_timeout on it
# (.busy_timeout(Duration::from_secs(5)) in codex-rs/state/src/sqlite.rs —
# NOT the SQLite library default, which is 0/no-wait) and does not retry
# past that timeout, so a writer collision under concurrent processes either
# waits up to 5s or surfaces as a hard failure — this is an open, upstream,
# reproduced bug (openai/codex#20213: "Multi-terminal codex CLI freezes due
# to SQLite lock contention with no BUSY retry"; openai/codex#35555: "CLI
# hard-fails at startup when any process holds a write lock on logs_2.sqlite
# ... flat 5s busy_timeout, no retry" — both verified live against the real
# repo, 2026-09-04). Card 3d15bf3b-9510-8164-95c8-d1c26da0df00's debugger run
# found a thread whose rollout .jsonl AND state_5.sqlite `threads` row were
# both intact, correct and unarchived, yet `codex exec resume` still failed
# with this exact error — the leading (evidenced here, not proven inside the
# codex-rs binary itself) explanation is this same SQLITE_BUSY-class
# contention, surfaced through codex's generic "no rollout found" message
# rather than a lock-specific one. Retrying the whole subprocess is safe
# here specifically because this failure happens before codex ever
# processes the prompt — nothing has been double-applied.
_ROLLOUT_ERROR_SIGNATURE = "no rollout found for thread id"

# Card 4055bf3b-9510-8179 (2026-10-26): under concurrent codex spawns a warm
# turn sometimes dies having emitted nothing but its own CLI startup banner —
# no rollout-contention message at all — which the signature-only check
# above never retried. Mirrors execute.py's own copy of this list (same
# "duplicated rather than imported" reason as _ROLLOUT_ERROR_SIGNATURE).
_PREAMBLE_SIGNATURES = ("Reading additional input from stdin",)

# Card 4055bf3b-9510-8179 follow-up (2026-10-26): a resume can also die
# having published nothing but the CLI banner AND one or both of codex's own
# session-handshake frames (`{"type":"thread.started",...}` and
# `{"type":"turn.started"}`) — no real turn content ever follows — and still
# exit 0, a CLEAN exit. turn.started in particular showed up on EVERY single
# codex invocation observed (success or failure alike, confirmed via live
# DIAG instrumentation) — it is normal boilerplate that just happens to
# always precede the real content on a successful turn. The previous
# generalization treated ANY non-preamble line as proof of real output, so
# either line alone flipped `live` True regardless of exit code. Mirrors
# execute.py's own _CODEX_SESSION_HANDSHAKE_SIGNATURES (same "duplicated
# rather than imported" reason). Unlike _ROLLOUT_ERROR_SIGNATURE (which
# stays gated on a nonzero exit — see saw_rollout_signature below, because
# that text could plausibly appear as genuine quoted model output), a
# stream consisting only of these and/or the preamble is contention
# regardless of exit code — codex cannot legitimately "succeed" having
# produced nothing beyond its own session handshake.
_SESSION_HANDSHAKE_SIGNATURES = ("thread.started", "turn.started")

_MAX_RESUME_ATTEMPTS = 3  # 1 initial + 2 retries
_RETRY_BACKOFF_S = (0.3, 0.6)  # indexed by retry number, last value repeats
_HOSTNAME = socket.gethostname()

# Card 4055bf3b-9510-8179 follow-up (2026-10-26, see
# docs/design/codex-runner-429-rate-limit-handling.md in
# aw-app-agents-platform-runners for the full decision record): codex's own
# internal HTTP client can exhaust its retry budget against a genuine OpenAI
# 429, surfaced as a real, non-boilerplate `{"type":"error","message":
# "exceeded retry limit, last status: 429 Too Many Requests"}` event. A
# SECOND retry class, distinct from the contention one above: a long,
# jittered schedule (never the 0.3s/0.6s contention one — codex has already
# burned its own internal retries against the same quota by the time this
# surfaces, so a sub-second reattempt is a guaranteed-wasted spawn),
# coordinated through the SAME shared Redis cooldown key execute.py's cold
# path uses — any process that sees this signature (any cold worker, any
# warm relay) arms it, and every codex attempt waits for it to clear first.
_RATE_LIMIT_SIGNATURE_PARTS = ("exceeded retry limit", "429")
_RATE_LIMIT_MAX_ATTEMPTS = 3  # 1 initial + 2 retries
_RATE_LIMIT_RETRY_BACKOFF_S = (20, 60)
_RATE_LIMIT_JITTER = 0.25  # ±25%

# Duplicated from execute.py's own _CODEX_429_COOLDOWN_KEY (this file ships
# into a different container image, loaded by path — same "duplicated
# rather than imported" rule as every signature above). Hardcoded "codex"
# rather than derived: this file only ever relays codex turns, so there is
# only ever one identity to key on from here.
_COOLDOWN_KEY = "apr:codex:429-cooldown:codex"
_COOLDOWN_BASE_S = 30
_COOLDOWN_MAX_S = 120
_COOLDOWN_WAIT_POLL_S = 1
_COOLDOWN_WAIT_BOUND_S = 150  # fail-open past this — a stuck key must never freeze the fleet
_COOLDOWN_RELEASE_JITTER_S = (0, 5)

# codex_login::auth::manager's own error codes when the OAuth refresh_token
# in the shared $CODEX_HOME/auth.json has been revoked/invalidated
# server-side — duplicated from execute.py's own copy of this list for the
# same reason _ROLLOUT_ERROR_SIGNATURE is duplicated (this file ships into a
# different container image, loaded by path, not importable as a package).
# Card bug:crispal-codex-oauth-token-not-persisted (2026-09-04): this shared
# home had no self-heal path for a revoked token, so it cascaded silently
# turn after turn until a human happened to look. Detecting and logging it
# loudly here (plus a diagnostics record) means a future occurrence surfaces
# without needing a debugger run to rediscover.
_AUTH_REVOKED_SIGNATURES = ("refresh_token_invalidated", "token_revoked")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _publish_line(r, stream_key: str, run_id: str, line: str) -> None:
    if aw_attach is not None:
        try:
            line = aw_attach.rewrite_stream_line(line, run_id)
        except Exception as e:
            print(f"aw-warm-relay-codex: attach rewrite failed ({e})", file=sys.stderr)
    try:
        r.xadd(stream_key, {"type": "stdout", "line": line}, maxlen=50_000, approximate=True)
    except Exception as e:
        print(f"aw-warm-relay-codex: XADD failed ({e})", file=sys.stderr)


def _publish_stderr_line(r, stream_key: str, run_id: str, line: str) -> None:
    """Publish codex diagnostics as metadata, never as assistant text.

    The cold connector has always wrapped subprocess stderr in this exact
    ``cli.stderr`` event shape.  The warm relay used ``stderr=STDOUT``
    instead, so warnings from codex (including models-cache schema errors)
    entered the JSON stdout parser as plain assistant deltas and were sent
    verbatim to Telegram.  Keep stderr observable without letting it become
    an ``llm_token``.
    """
    _publish_line(r, stream_key, run_id, json.dumps({
        "type": "cli.stderr",
        "text": line[:20000],
    }))


def _iter_process_output(proc):
    """Yield ``(source, line)`` from stdout and stderr without deadlocking.

    Both pipes must be drained concurrently: codex can be verbose on stderr
    while it is still producing JSON events on stdout.  A thread per pipe is
    deliberately small and local to one subprocess attempt.
    """
    out: queue.Queue = queue.Queue()
    streams = [
        ("stdout", getattr(proc, "stdout", None)),
        ("stderr", getattr(proc, "stderr", None)),
    ]
    streams = [(source, stream) for source, stream in streams if stream is not None]

    def _pump(source, stream):
        try:
            for line in stream:
                out.put((source, line))
        finally:
            out.put((source, None))

    for source, stream in streams:
        threading.Thread(target=_pump, args=(source, stream), daemon=True).start()

    open_streams = len(streams)
    while open_streams:
        source, line = out.get()
        if line is None:
            open_streams -= 1
            continue
        yield source, line


def _invalidate_incompatible_models_cache(env: dict) -> bool:
    """Delete the reconstructible cache written by an older codex schema.

    codex 0.147 requires ``supports_parallel_tool_calls`` on every cached
    model.  An older cache lacks it, and codex otherwise logs the same parse
    failure on startup and every TTL renewal.  Only delete a cache proven to
    have that incompatible shape; malformed/in-flight files and unrelated
    future schemas are left alone for codex to handle.
    """
    home = env.get("CODEX_HOME")
    if not home:
        process_home = env.get("HOME")
        if not process_home:
            return False
        home = os.path.join(process_home, ".codex")
    path = os.path.join(home, "models_cache.json")
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not any(
                isinstance(model, dict)
                and "supports_parallel_tool_calls" not in model
                for model in models):
            return False
        os.unlink(path)
        return True
    except (FileNotFoundError, PermissionError, OSError, TypeError, ValueError):
        return False


def _is_rate_limit_exhaustion(line: str) -> bool:
    """Structural match on codex's own terminal 429-exhaustion event, NOT a
    substring match — mirrors execute.py's own
    _is_codex_rate_limit_exhaustion (same "duplicated rather than imported"
    reason as every signature in this file). Requires the line to parse as
    JSON with a top-level ``"type": "error"`` and a ``message`` containing
    BOTH signature fragments; never gated on returncode, unlike
    _ROLLOUT_ERROR_SIGNATURE — codex can die cleanly either way."""
    try:
        evt = json.loads(line)
    except (TypeError, ValueError):
        return False
    if not isinstance(evt, dict) or evt.get("type") != "error":
        return False
    message = evt.get("message")
    return isinstance(message, str) and all(
        part in message for part in _RATE_LIMIT_SIGNATURE_PARTS)


def _cooldown_arm(r) -> None:
    """Arm (or re-arm) the shared codex 429 cooldown — mirrors execute.py's
    own _codex_429_cooldown_arm against the SAME Redis key, so a storm seen
    by a warm relay gates every cold spawn too, and vice versa. TTL starts
    at _COOLDOWN_BASE_S and doubles on each re-arm seen while the previous
    cooldown is still active, capped at _COOLDOWN_MAX_S."""
    if r is None:
        return
    try:
        prev = r.get(_COOLDOWN_KEY)
        ttl = min(int(prev) * 2, _COOLDOWN_MAX_S) if prev else _COOLDOWN_BASE_S
        r.set(_COOLDOWN_KEY, str(ttl), ex=ttl)
    except Exception as e:
        print(f"aw-warm-relay-codex: could not arm the shared 429 cooldown ({e})",
              file=sys.stderr)


def _cooldown_wait(r, stream_key: str, run_id: str) -> None:
    """Block until the shared codex 429 cooldown clears, before this warm
    turn's codex subprocess is spawned — mirrors execute.py's own
    _codex_429_cooldown_wait against the SAME key. Costs nothing when no
    cooldown is armed. Bounded at _COOLDOWN_WAIT_BOUND_S total (fail-open
    past that — a stuck key must never freeze a warm container) and
    publishes a run-stream notice the moment it finds the cooldown actually
    armed, both so the watchdog sees activity and so the wait is visible.
    No abort check: this relay has no abort channel for an in-flight turn
    today (nothing here does), so there is nothing to mirror — same as the
    unguarded contention backoff sleep below."""
    if r is None:
        return
    try:
        waited = 0.0
        notified = False
        cleared = False
        while True:
            try:
                armed = bool(r.get(_COOLDOWN_KEY))
            except Exception as e:
                print(f"aw-warm-relay-codex: 429 cooldown check failed ({e}) — "
                      "proceeding without waiting", file=sys.stderr)
                return
            if not armed:
                cleared = notified  # only a real wait counts as "cleared"
                break
            if not notified:
                notified = True
                print(f"aw-warm-relay-codex[{_HOSTNAME}] {_utc_now()}: run={run_id} "
                      "waiting on the shared codex 429 cooldown before spawning",
                      file=sys.stderr)
                _publish_line(r, stream_key, run_id, json.dumps({
                    "type": "runner.notice",
                    "text": "codex rate-limited by OpenAI — waiting for the shared "
                            "cooldown to clear before spawning",
                }))
            if waited >= _COOLDOWN_WAIT_BOUND_S:
                print(f"aw-warm-relay-codex[{_HOSTNAME}] {_utc_now()}: run={run_id} "
                      f"proceeding past the codex 429 cooldown after waiting "
                      f"{waited:.0f}s (fail-open)", file=sys.stderr)
                break
            time.sleep(_COOLDOWN_WAIT_POLL_S)
            waited += _COOLDOWN_WAIT_POLL_S
        if cleared:
            # Only a waiter whose cooldown actually cleared gets the extra
            # release jitter, not one that gave up via fail-open — a cooldown
            # that was never armed costs nothing.
            time.sleep(random.uniform(*_COOLDOWN_RELEASE_JITTER_S))
    except Exception as e:
        print(f"aw-warm-relay-codex: 429 cooldown wait failed ({e}) — proceeding",
              file=sys.stderr)


def _run_codex_turn(argv: list, cwd: str, env: dict, r, stream_key: str, run_id: str
                     ) -> tuple[int, int, str, bool]:
    """Run one `codex exec resume` subprocess, retrying an attempt that never
    produced a line of real turn content — whether it hit the known "no
    rollout found for thread id ... (code -32600)" error (only retryable
    alongside a nonzero exit — see saw_rollout_signature), printed only its
    own startup banner and/or codex's own thread/turn-start handshake frames
    (_PREAMBLE_SIGNATURES / _SESSION_HANDSHAKE_SIGNATURES — retryable
    regardless of exit code), or emitted nothing at all (see the
    module-level note on _ROLLOUT_ERROR_SIGNATURE for why retrying the whole
    subprocess is safe).

    Buffers only the first attempt's lines until either the error signature
    or real output appears, so a normal turn's live streaming is unaffected
    — the added latency is confined to attempts that actually hit the
    transient error.

    A second, independent retry class (card 4055bf3b-9510-8179 follow-up)
    handles codex's own 429-exhaustion event the same way, on its own long,
    jittered schedule (_RATE_LIMIT_RETRY_BACKOFF_S), coordinated through the
    shared Redis cooldown key _cooldown_wait/_cooldown_arm set and read —
    every attempt, including the very first, waits for it to clear before
    spawning.

    Returns (returncode, attempts_made, tail_text, auth_revoked) — the first
    three feed main()'s resume_end diagnostics line (attempt count and exact
    trailing output survive a single-attempt view of the run's Redis stream);
    auth_revoked is True the moment any attempt's output ever matched
    _AUTH_REVOKED_SIGNATURES, so main() can log/diagnose it once per turn
    instead of it cascading silently (card
    bug:crispal-codex-oauth-token-not-persisted).
    """
    last_tail: deque = deque(maxlen=5)
    auth_revoked = False
    max_attempts = max(_MAX_RESUME_ATTEMPTS, _RATE_LIMIT_MAX_ATTEMPTS)
    reason = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            if reason == "rate_limit":
                _cooldown_arm(r)
                backoff = _RATE_LIMIT_RETRY_BACKOFF_S[
                    min(attempt - 2, len(_RATE_LIMIT_RETRY_BACKOFF_S) - 1)]
                backoff *= 1 + random.uniform(-_RATE_LIMIT_JITTER, _RATE_LIMIT_JITTER)
                print(f"aw-warm-relay-codex[{_HOSTNAME}] {_utc_now()}: run={run_id} "
                      f"hit codex 429 rate-limit exhaustion on attempt {attempt - 1}/"
                      f"{max_attempts} — retrying in {backoff:.1f}s", file=sys.stderr)
                _publish_line(r, stream_key, run_id, json.dumps({
                    "type": "runner.notice",
                    "text": f"codex rate-limited by OpenAI — waiting {backoff:.0f}s "
                             f"before retry (attempt {attempt}/{_RATE_LIMIT_MAX_ATTEMPTS})",
                }))
                time.sleep(backoff)
            else:
                backoff = _RETRY_BACKOFF_S[min(attempt - 2, len(_RETRY_BACKOFF_S) - 1)]
                print(f"aw-warm-relay-codex[{_HOSTNAME}] {_utc_now()}: resume produced no "
                      f"real output (attempt {attempt - 1}/{max_attempts}, rc={rc}, "
                      f"tail={list(last_tail)}) — likely shared-$CODEX_HOME contention, "
                      f"retrying after {backoff}s", file=sys.stderr)
                time.sleep(backoff)
        _cooldown_wait(r, stream_key, run_id)
        if _invalidate_incompatible_models_cache(env):
            print(f"aw-warm-relay-codex[{_HOSTNAME}] {_utc_now()}: removed an "
                  "incompatible codex models cache; codex will rebuild it",
                  file=sys.stderr)
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1, env=env)
        except Exception as e:
            print(f"aw-warm-relay-codex: could not start codex ({e})", file=sys.stderr)
            _publish_line(r, stream_key, run_id, json.dumps({
                "type": "cli.stderr", "text": f"warm codex failed to start: {e}"}))
            return 1, attempt, f"spawn failed: {e}", auth_revoked

        buffered: list[tuple[str, str]] = []
        live = False
        saw_rollout_signature = False
        saw_rate_limit_signature = False
        for source, out_line in _iter_process_output(proc):
            out_line = out_line.rstrip("\n")
            if not out_line:
                continue
            last_tail.append(out_line)
            if any(sig in out_line for sig in _AUTH_REVOKED_SIGNATURES):
                auth_revoked = True
            if live:
                if source == "stderr":
                    _publish_stderr_line(r, stream_key, run_id, out_line)
                else:
                    _publish_line(r, stream_key, run_id, out_line)
                continue
            if _is_rate_limit_exhaustion(out_line):
                saw_rate_limit_signature = True
                buffered.append((source, out_line))
                continue
            if _ROLLOUT_ERROR_SIGNATURE in out_line:
                saw_rollout_signature = True
                buffered.append((source, out_line))
                continue
            if source == "stderr":
                buffered.append((source, out_line))
                continue
            if (any(sig in out_line for sig in _SESSION_HANDSHAKE_SIGNATURES)
                    or any(sig in out_line for sig in _PREAMBLE_SIGNATURES)):
                buffered.append((source, out_line))
                continue
            # Real content arrived — this attempt is not hitting the
            # transient error. Flush what we held and stream live from here.
            live = True
            for held_source, held in buffered:
                if held_source == "stderr":
                    _publish_stderr_line(r, stream_key, run_id, held)
                else:
                    _publish_line(r, stream_key, run_id, held)
            buffered = []
            _publish_line(r, stream_key, run_id, out_line)
        rc = proc.wait()

        if not live and saw_rate_limit_signature:
            reason = "rate_limit"
        elif not live and (rc != 0 or (bool(buffered) and not saw_rollout_signature)):
            reason = "contention"
        else:
            reason = None
        if reason and attempt < max_attempts:
            continue

        # Final attempt (succeeded, hit a different error, or retries
        # exhausted) — publish whatever never got flushed so a real failure
        # is still visible, never silently swallowed.
        for held_source, held in buffered:
            if held_source == "stderr":
                _publish_stderr_line(r, stream_key, run_id, held)
            else:
                _publish_line(r, stream_key, run_id, held)
        if reason:
            print(f"aw-warm-relay-codex[{_HOSTNAME}] {_utc_now()}: resume still failing "
                  f"after {attempt} attempts, giving up — tail: "
                  f"{list(last_tail)}", file=sys.stderr)
        return rc, attempt, "\n".join(last_tail), auth_revoked

    return 1, max_attempts, "", auth_revoked  # unreachable — loop always returns


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


def _diagnostics_path(codex_home: str) -> str:
    return os.path.join(codex_home, "diagnostics", "resume-activity.jsonl")


def _diag(codex_home: str, event: str, **fields) -> None:
    """Append one line to a diagnostics log INSIDE the shared, persistent
    $CODEX_HOME — not the ephemeral container filesystem, not the Redis
    stream (which expires in 86400s and needs a redis-cli to read). Added
    2026-09-04 after a resume failure (bug:codex-warm-container-rollout-
    lost-on-resume) turned out to have an intact rollout AND an intact
    state_5.sqlite row, leaving version drift and concurrent-writer
    contention on $CODEX_HOME as the two untested explanations — neither
    could be checked after the fact because nothing recorded the codex
    binary version in use or the wall-clock window of each resume attempt.

    $CODEX_HOME is ONE directory shared by every concurrently-warm codex
    session on this runner (by design, see execute.py's staged_home
    comment), so EVERY session appends to this SAME file — a future failed
    resume can be grepped here for another session's resume_start/
    resume_end window overlapping it, which is direct evidence for or
    against the SQLite-contention hypothesis without needing podman/docker
    access (the exact gap the 2026-09-04 investigation hit).

    Best-effort: a diagnostics write that fails must never fail the turn
    itself, same reasoning as execute.py's own _log_recycle."""
    try:
        path = _diagnostics_path(codex_home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
               "pid": os.getpid(), "host": socket.gethostname(), **fields}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"aw-warm-relay-codex: diagnostics write failed ({e})", file=sys.stderr)


def _codex_version() -> str:
    """Resolved ONCE per container lifetime (not per resume — the binary
    cannot change mid-container-life, and shelling out on every single turn
    would add latency for a value that is already constant). Logged into
    every resume_start/resume_end diagnostics line below so it is still
    visible per-resume without re-invoking the binary."""
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True,
                              text=True, timeout=10)
        return (out.stdout or out.stderr or "").strip() or f"exit={out.returncode}"
    except Exception as e:
        return f"unresolved ({e})"


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

    # codex_home: same container-side mount path _build_warm_kwargs_codex set
    # CODEX_HOME to (staged_home in execute.py) — the persistent, host-visible,
    # cross-session-shared directory. agent_tag: the (often unpinned, "latest"
    # by default) image tag execute.py resolved this container from — compared
    # against codex_version (what's actually running) to catch version drift.
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    codex_version = _codex_version()
    agent_tag = os.environ.get("AW_AGENT_TAG") or "unknown"
    _diag(codex_home, "container_start", session_id=session_id,
          codex_version=codex_version, agent_tag=agent_tag)

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
        turn_started = time.monotonic()
        _diag(codex_home, "resume_start", session_id=session_id, run_id=run_id,
              codex_version=codex_version, agent_tag=agent_tag)
        rc, attempts, tail_text, auth_revoked = _run_codex_turn(
            argv, cwd, env, r, stream_key, run_id)
        _diag(codex_home, "resume_end", session_id=session_id, run_id=run_id,
              rc=rc, duration_s=round(time.monotonic() - turn_started, 3),
              attempts=attempts, is_rollout_error=_ROLLOUT_ERROR_SIGNATURE in tail_text,
              error_excerpt=tail_text[-1500:] if rc != 0 else None)
        if auth_revoked:
            print(f"aw-warm-relay-codex[{_HOSTNAME}] {_utc_now()}: WARNING codex reports "
                  f"its OAuth refresh_token was revoked/invalidated (session={session_id} "
                  f"run={run_id}) — $CODEX_HOME/auth.json needs a fresh login; every "
                  f"further turn against this shared home will fail identically until "
                  f"it's resynced. See card bug:crispal-codex-oauth-token-not-persisted.",
                  file=sys.stderr)
            _diag(codex_home, "auth_revoked", session_id=session_id, run_id=run_id,
                  codex_version=codex_version, agent_tag=agent_tag,
                  error_excerpt=tail_text[-1500:])

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
