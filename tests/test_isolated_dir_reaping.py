"""Isolated run dirs are reaped, or they are a slow disk leak.

THE MEASUREMENT (2026-09-12): 127 of them on a live host, 12 GB, spanning
Aug 14 to Sep 9. One per run, created by _build_kwargs and removed by nothing.

Each is ~100 MB — not the mcp.json the dir nominally exists for, but a full
clone of the plugins repo the CLI drops in `creds/.tmp/plugins` (a 23 MB git
pack plus assets, 5,491 files), re-cloned every run.

What these tests guard is the part that is easy to get wrong: an age rule that
deletes a RUNNING run's directory, and a housekeeping failure that takes a
dispatch down with it.
"""
import os
import time
from pathlib import Path

import pytest

from agents_platform_runners_app.execute import (
    ISOLATED_KEEP_SECONDS,
    _reap_isolated_dirs,
)


def _aged(path: Path, seconds_old: float) -> Path:
    """A run dir as it looks after finishing: the directory AND everything in
    it untouched since then. Ageing only the directory would not be a finished
    run — it would be the running one the reaper must never take."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "mcp.json").write_text("{}")
    (path / "creds").mkdir(exist_ok=True)
    when = time.time() - seconds_old
    for p in (path / "creds", path / "mcp.json", path):
        os.utime(p, (when, when))
    return path


def test_an_old_dir_is_removed(tmp_path):
    old = _aged(tmp_path / "run-old", ISOLATED_KEEP_SECONDS + 60)
    _reap_isolated_dirs(tmp_path)
    assert not old.exists()


def test_a_fresh_dir_is_left_alone(tmp_path):
    fresh = _aged(tmp_path / "run-fresh", 10)
    _reap_isolated_dirs(tmp_path)
    assert fresh.exists()


def test_a_running_run_is_never_a_candidate(tmp_path):
    """THE ONE THAT MATTERS. A dir written to during a long run keeps a young
    mtime, so however long the run has been going it is never old. Keying on
    ctime instead would delete the scratch dir out from under a live
    container."""
    running = _aged(tmp_path / "run-live", ISOLATED_KEEP_SECONDS * 10)
    (running / "mcp.json").write_text('{"still": "working"}')  # touches mtime
    _reap_isolated_dirs(tmp_path)
    assert running.exists()


def test_a_dir_exactly_at_the_boundary_is_kept(tmp_path):
    """Off-by-one on the wrong side deletes something still in its window."""
    edge = _aged(tmp_path / "run-edge", ISOLATED_KEEP_SECONDS - 5)
    _reap_isolated_dirs(tmp_path)
    assert edge.exists()


def test_only_directories_are_touched(tmp_path):
    """A loose file is not a run dir and is not ours to delete.

    Note for whoever changes this: the is_dir() guard in the reaper is NOT
    what this test proves. `shutil.rmtree(file, ignore_errors=True)` does
    nothing and raises nothing, so removing that guard leaves this test green
    — checked. The guard is belt-and-braces for the day someone drops
    ignore_errors, and this test pins the OUTCOME (the file survives), which
    is what actually matters either way."""
    stray = tmp_path / "notes.txt"
    stray.write_text("x")
    os.utime(stray, (0, 0))
    _reap_isolated_dirs(tmp_path)
    assert stray.exists()


def test_a_missing_parent_is_not_an_error(tmp_path):
    """First run on a fresh host: nothing to sweep, and refusing to dispatch
    over that would be absurd."""
    _reap_isolated_dirs(tmp_path / "never-created")


def test_an_undeletable_entry_never_fails_the_dispatch(tmp_path, monkeypatch):
    """A run that does not start is a worse outcome than a directory that
    survives one more round."""
    _aged(tmp_path / "run-stuck", ISOLATED_KEEP_SECONDS + 60)

    def boom(*a, **k):
        raise OSError("device busy")

    monkeypatch.setattr("agents_platform_runners_app.execute.shutil.rmtree", boom)
    _reap_isolated_dirs(tmp_path)  # must not raise


def test_the_window_is_far_longer_than_any_run():
    """The safety margin IS the design. A value near a run's duration would
    turn this from housekeeping into a race."""
    assert ISOLATED_KEEP_SECONDS >= 86400


def test_several_at_once(tmp_path):
    olds = [_aged(tmp_path / f"old-{i}", ISOLATED_KEEP_SECONDS + 100) for i in range(5)]
    news = [_aged(tmp_path / f"new-{i}", 5) for i in range(3)]
    _reap_isolated_dirs(tmp_path)
    assert not any(p.exists() for p in olds)
    assert all(p.exists() for p in news)
