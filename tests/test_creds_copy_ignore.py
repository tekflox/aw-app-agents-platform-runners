"""What the per-run creds copy leaves behind.

Each run gets its OWN copy of the CLI's creds dir, and that is deliberate:
codex has to read auth.json and write session state, and handing it the live
directory made concurrent runs fight over one SQLite. The copy is the right
call. Copying 100 MB to make it was not.

Measured on a live host (2026-09-13): ~/.codex was 12.2 GB, of which 12.1 GB
was accumulated isolated copies — the SOURCE is about 150 MB. Of each ~100 MB
copy, 97 MB was `.tmp/plugins`: a git checkout duplicated on every run.

The reaper (ISOLATED_KEEP_SECONDS) bounds how long copies survive. This bounds
how BIG each one is, which is the half that attacks the cause rather than the
accumulation.
"""
import shutil
from pathlib import Path

import pytest

from agents_platform_runners_app.execute import CREDS_COPY_IGNORE


def _creds_tree(root: Path) -> Path:
    """A creds dir shaped like the real one that caused this."""
    src = root / "src"
    (src / ".tmp" / "plugins" / ".git" / "objects").mkdir(parents=True)
    (src / ".tmp" / "plugins" / ".git" / "objects" / "pack").write_text("x" * 100)
    (src / ".tmp" / "plugins" / "plugins" / "canva").mkdir(parents=True)
    (src / ".tmp" / "git-Cjf0Tk").mkdir(parents=True)
    (src / "plugins" / "real-plugin").mkdir(parents=True)
    (src / "plugins" / "real-plugin" / "manifest.json").write_text("{}")
    (src / "isolated" / "old-run").mkdir(parents=True)
    (src / "sessions").mkdir()
    (src / "cache").mkdir()
    (src / "skills").mkdir()
    (src / "auth.json").write_text("{}")
    (src / "config.toml").write_text("")
    (src / "state_5.sqlite").write_text("")
    return src


def _copy(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*CREDS_COPY_IGNORE))


def test_the_staging_tree_is_not_copied(tmp_path):
    """THE FIX. 97 of every 100 MB was this."""
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    assert not (dst / ".tmp").exists()


def test_the_plugins_the_cli_LOADS_are_still_copied(tmp_path):
    """`.tmp/plugins` is a staging checkout; `plugins/` is what the CLI reads.
    Dropping both would be dropping the feature, not the duplicate."""
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    assert (dst / "plugins" / "real-plugin" / "manifest.json").is_file()


def test_credentials_and_state_still_travel(tmp_path):
    """The whole reason the copy exists: codex must read auth and write state."""
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    for name in ("auth.json", "config.toml", "state_5.sqlite"):
        assert (dst / name).is_file(), f"{name} must reach the container"
    assert (dst / "skills").is_dir()


@pytest.mark.parametrize("name", ["isolated", "sessions", "cache"])
def test_the_pre_existing_exclusions_are_kept(tmp_path, name):
    """`isolated` especially: copying it would copy the copies."""
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    assert not (dst / name).exists()


def test_the_copy_is_dramatically_smaller(tmp_path):
    """Pins the OUTCOME, not the list. A future edit that keeps `.tmp` out of
    the tuple but reintroduces the bulk some other way still fails here."""
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    before = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
    after = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    assert after < before / 2, f"copy is {after} of {before} bytes — the bulk survived"
