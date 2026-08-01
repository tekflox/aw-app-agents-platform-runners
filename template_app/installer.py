"""
Install/uninstall logic for the `hello` CLI, as a plain subprocess-calling
module (no framework `ctx` needed) — used by tests/test_installer.py
(subprocess mocked) and by tests/standalone_test.sh (real, out-of-framework).
HelloAppPlugin.activate() goes through ctx.commands.install_system_cli()
instead (the gated/journaled framework path); this module exists purely so
the install logic is testable in plain CI without spinning up the runtime.

TEMPLATE: this file is optional — only needed if you want unit-testable
install functions like this one. See aw-app-essentials/essentials_app/
installer.py for a real multi-CLI example (several install_X() functions +
an install_all() aggregate).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = APP_ROOT / "scripts"


class InstallError(RuntimeError):
    pass


def _run_script(script: str, *, env_overrides: dict[str, str] | None = None) -> str:
    import os

    path = SCRIPTS_DIR / script
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        ["bash", str(path)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise InstallError(
            f"{script} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def install_hello(greeting: str = "Hello") -> str:
    return _run_script("install_hello.sh", env_overrides={"AW_APP_HELLO_GREETING": greeting})


def uninstall_hello() -> None:
    _run_script("uninstall.sh")
