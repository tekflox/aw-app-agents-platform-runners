"""``agents-platform`` CLI — group auto-discovery, the shim contract, and
the single most likely regression: importing ``cli.main`` must never drag
in ``plugin.py`` (docker/redis/fastapi, ~10k lines) or ``identity_token``
(which imports ``plugin`` at module scope). Nothing else catches this.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cli_main_import_never_touches_plugin():
    """Import in a FRESH subprocess — sys.modules in-process is already
    polluted by the time conftest.py has imported ``execute`` (which imports
    ``plugin``), so this must run isolated to mean anything."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import agents_platform_runners_app.cli.main as m; "
         "import sys; "
         "assert 'agents_platform_runners_app.plugin' not in sys.modules, sys.modules.keys(); "
         "assert 'agents_platform_runners_app.identity_token' not in sys.modules; "
         "print('OK')"],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_discover_groups_finds_telegram_bot_and_agents():
    from agents_platform_runners_app.cli.groups import discover_groups
    names = {m.GROUP for m in discover_groups()}
    assert {"telegram-bot", "agents"} <= names


def test_discover_groups_is_sorted_by_group_name():
    from agents_platform_runners_app.cli.groups import discover_groups
    names = [m.GROUP for m in discover_groups()]
    assert names == sorted(names)


def test_help_lists_both_groups(capsys):
    from agents_platform_runners_app.cli.main import main
    with pytest.raises(SystemExit):
        main(["--help"], prog="agents-platform")
    out = capsys.readouterr().out
    assert "telegram-bot" in out
    assert "agents" in out


def test_shim_module_has_the_required_contract():
    import importlib.util
    import os
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "commands", "agents_platform.py",
    )
    spec = importlib.util.spec_from_file_location("_shim_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.COMMAND == "agents-platform"
    assert isinstance(module.DESCRIPTION, str) and module.DESCRIPTION
    assert callable(module.run)


def test_shim_run_dispatches_to_cli_main(capsys):
    import importlib.util
    import os
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "commands", "agents_platform.py",
    )
    spec = importlib.util.spec_from_file_location("_shim_under_test2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as exc_info:
        module.run(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "telegram-bot" in out
