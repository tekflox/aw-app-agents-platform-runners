"""Warm container lifetime, reduced from 6h to 30 min (2026-09-30 product
request): "reduce warm container lifetime to 30 min, they need to be
refreshed after 30min", with the explicit constraint that expiry must
condemn/drain and safely replace the container WITHOUT interrupting a turn
already in progress.

warm_pool.WARM_TTL_S is a reference constant only (see its own docstring —
never polled or enforced from this side); the actual enforcement lives
INSIDE the container, in aw-warm-wrapper's (claude) and
aw-warm-wrapper-codex's own TTL watcher subshell. This is a static,
grep-based check on those two shell scripts — no docker daemon required —
mirroring the drain-vs-kill separation convention already used elsewhere in
this codebase's ported siblings (agents-platform-multitenant/legacy's
test_warm_drain_separation.py) for the SAME graceful-drain principle: the
TTL mark must touch the drain flag (letting the existing poll loop close
stdin only once the current turn, if any, is done) rather than signal the
CLI process directly, so a turn in flight is never interrupted. A bounded
fallback kill still exists for a genuinely wedged process, but only AFTER
the graceful drain flag was given a full DRAIN_GRACE_S (3600s) chance to
work — see warm_pool.DRAIN_GRACE_S and the wrapper scripts' own comments.

Run: python3 -m pytest tests/test_warm_ttl_drain.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import warm_pool  # noqa: E402

WRAPPER_CLAUDE = ROOT / "agent-images" / "shared" / "aw-warm-wrapper"
WRAPPER_CODEX = ROOT / "agent-images" / "shared" / "aw-warm-wrapper-codex"


def test_warm_ttl_s_is_thirty_minutes():
    assert warm_pool.WARM_TTL_S == 1800


def test_drain_grace_s_unchanged_and_used_as_the_ttl_fallback_window():
    """DRAIN_GRACE_S is reap()'s own wedged-drainer grace window — the TTL
    watchers reuse the same 3600s number (hardcoded, since the shell scripts
    can't import from warm_pool.py) for their own backstop-kill delay, so the
    two must keep matching or the wrapper comments/tests go stale silently."""
    assert warm_pool.DRAIN_GRACE_S == 3600


def _ttl_watcher_subshell(text: str) -> str:
    """Extract the `( ... ) &` subshell body that owns TTL_WATCHER_PID —
    the block both scripts open right after touching `ready`."""
    match = re.search(r"\(\s*exec[^\n]*\n(.*?)\)\s*&\s*\nTTL_WATCHER_PID=\$!",
                       text, re.DOTALL)
    assert match, "could not locate the TTL watcher subshell — did the wrapper's structure change?"
    return match.group(1)


def _assert_graceful_ttl_watcher(path: Path, pid_var: str) -> None:
    text = path.read_text()
    body = _ttl_watcher_subshell(text)

    assert "sleep 1800" in body, (
        f"{path.name}: TTL watcher must fire at 1800s (30 min), not the old 6h (21600s)"
    )
    assert "21600" not in text, f"{path.name}: old 6h TTL literal must not remain anywhere in the file"

    # The FIRST action after the 30-min sleep must be the graceful drain flag,
    # not a direct kill — this is what keeps an in-flight turn from being cut
    # off. A kill is only acceptable as a LATER, bounded fallback.
    drain_touch = f'touch "$RUNDIR/drain"'
    assert drain_touch in body, (
        f"{path.name}: TTL mark must touch the drain flag (graceful path), "
        f"reusing the same poll loop a host-triggered drain uses"
    )
    sleep_idx = body.index("sleep 1800")
    drain_idx = body.index(drain_touch)
    kill_idx = body.index(f'kill "${pid_var}"')
    assert sleep_idx < drain_idx < kill_idx, (
        f"{path.name}: order must be sleep(1800) -> touch drain -> (bounded wait) -> kill fallback, "
        f"never a direct kill at the 30-min mark"
    )

    # The fallback kill must be gated behind ANOTHER sleep (the grace window),
    # not fire back-to-back with the drain touch.
    between = body[drain_idx:kill_idx]
    assert "sleep 3600" in between, (
        f"{path.name}: the fallback kill must wait DRAIN_GRACE_S (3600s) after "
        f"the drain flag before force-killing a wedged process"
    )


def test_claude_wrapper_ttl_drains_gracefully_before_any_fallback_kill():
    _assert_graceful_ttl_watcher(WRAPPER_CLAUDE, "CLAUDE_PID")


def test_codex_wrapper_ttl_drains_gracefully_before_any_fallback_kill():
    _assert_graceful_ttl_watcher(WRAPPER_CODEX, "RELAY_PID")


def test_claude_wrapper_still_never_shells_out_to_docker_kill_or_stop():
    """Mirrors the ported sibling repos' test_warm_drain_separation.py intent
    for THIS file: neither wrapper's TTL/drain machinery may ever escalate to
    `docker kill`/`docker stop` — that verb belongs solely to the host-side
    hard-abort path (kill_run), never to anything running inside the
    container. Scoped to executable lines only: both scripts' comments
    reference `docker kill` in prose (explaining what NOT to do), which is
    the documentation this test exists to keep honest, not a violation."""
    for path in (WRAPPER_CLAUDE, WRAPPER_CODEX):
        code_lines = [line for line in path.read_text().splitlines()
                      if line.strip() and not line.strip().startswith("#")]
        code_text = "\n".join(code_lines)
        assert "docker kill" not in code_text
        assert "docker stop" not in code_text
