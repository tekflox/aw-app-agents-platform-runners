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

It recurred on 2026-10-21, an order of magnitude larger, and took the host's
whole disk with it: codex's `packages/app-server-daemon/` ships ~700 MB
releases plus a `current` symlink that `copytree` dereferences into another
full copy, so each run cost 2.1 GB. 37 of them filled a 436 GB disk, which
cost Redis its AOF, put postgres into recovery mode, and left the app whose
update needed the last few bytes rolled back and DOWN.
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
    # codex's app-server package tree, shaped like the real one: two pinned
    # releases plus a `current` SYMLINK into one of them. The symlink matters —
    # copytree dereferences it, so the copy is bigger than the source.
    releases = src / "packages" / "app-server-daemon" / "releases"
    for version in ("0.160.1-x86_64-unknown-linux-musl",
                    "0.161.0-x86_64-unknown-linux-musl"):
        (releases / version / "bin").mkdir(parents=True)
        (releases / version / "bin" / "codex").write_text("B" * 700)
    (src / "packages" / "app-server-daemon" / "current").symlink_to(
        releases / "0.161.0-x86_64-unknown-linux-musl")
    # codex's own scratch dir — plain `tmp`, NOT `.tmp`. It extracts its
    # launcher here per invocation and deletes it again, so a copy taken
    # mid-extraction keeps ~1 GB forever while the source reads as ~100 KB.
    for slot in ("arg0", "arg0-linux"):
        (src / "tmp" / slot / "codex-arg0fV11dV").mkdir(parents=True)
        (src / "tmp" / slot / "codex-arg0fV11dV" / "codex").write_text("A" * 1100)
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


def test_the_codex_package_tree_is_not_copied(tmp_path):
    """THE 2026-10-21 FIX. 2.1 of every 2.1 GB was this.

    Free to drop, not a trade: the container runs with CODEX_HOME pointed at
    the ONE shared home, and this copy is only read to FIRST-populate it,
    behind a `[ -f config.toml ] ||` guard. Wherever codex has run before,
    these bytes were copied, mounted read-only, and never opened.
    """
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    assert not (dst / "packages").exists()


def test_codex_scratch_is_not_copied_even_though_it_lacks_a_dot(tmp_path):
    """`.tmp` is claude's staging dir; codex's is plain `tmp`.

    The 2026-09-13 exclusion only ever matched the dotted one, so codex's
    launcher scratch kept travelling — and it was the bigger half of the
    2026-10-21 incident. Asserted as its own case because the two names are
    one character apart and a future tidy-up could plausibly drop this as a
    duplicate of the `.tmp` test above.
    """
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    assert not (dst / "tmp").exists()
    assert not (dst / ".tmp").exists()


def test_a_dereferenced_symlink_cannot_inflate_the_copy(tmp_path):
    """Why `packages` cost 2.1 GB from a 1.3 GB source.

    `copytree` follows symlinks by default, so `current` -> a 700 MB release
    arrives as a THIRD independent copy. Pinning the outcome: no copy may be
    larger than the directory it was made from.
    """
    src = _creds_tree(tmp_path)
    dst = tmp_path / "copy"
    _copy(src, dst)
    before = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
    after = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    assert after <= before, f"copy is {after} bytes from {before} — a symlink was inflated"


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
