"""``aw-workspace-cli agents-platform`` — this app's own CLI command.

Auto-discovered by aw-workspace-cli from this app's installed directory
(``<apps_root>/agents-platform-runners/commands/``, since this file lives at
``commands/`` in this repo's root — see aw-workspace's
``src/cli/discovery.py``, which loads every ``<apps_root>/<slug>/
commands/*.py`` exposing ``COMMAND``/``DESCRIPTION``/``run``). Same shape as
``aw-app-remote-host-cli``'s ``commands/remote_hosts.py``.

Every flag/parser/behavior stays defined in
``agents_platform_runners_app.cli`` (single source of truth); this file only
puts the app's package dir on ``sys.path`` and calls ``main()``. Tier-1 apps
load under a synthetic ``aw_apps.<id>`` namespace inside the *workspace*
process (see aw-workspace's ``src/apps/runtime.py:_import_plugin``), so
``agents_platform_runners_app`` is not importable as a top-level package
from the separate ``aw-workspace-cli`` process without this.

Usage:
    aw-workspace-cli agents-platform --help
    aw-workspace-cli agents-platform telegram-bot list
    aw-workspace-cli agents-platform agents list
    aw-workspace-cli agents-platform agents run <slug> --input "..." --wait
"""
from __future__ import annotations

import os
import sys

COMMAND = "agents-platform"
DESCRIPTION = "Control Agents Platform: telegram bots, agents, runs (and more)"

# <this file>/../ — the app package dir. Resolved from __file__ rather than
# apps_root() so this works identically from the installed copy and from a
# checkout under repos/.
APP_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

PROG = "aw-workspace-cli agents-platform"


def run(args: list[str]) -> int:
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)

    try:
        from agents_platform_runners_app.cli.main import main
    except ImportError as exc:  # missing dep (httpx) or a broken install
        print(f"{PROG}: cannot load the agents-platform client from {APP_DIR}: {exc}",
              file=sys.stderr)
        return 1

    return main(list(args or []), prog=PROG)
