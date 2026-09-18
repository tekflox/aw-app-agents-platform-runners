"""Auto-discovery of ``agents-platform`` command groups — the growth seam
this whole command exists for. Adding a future group (``targets``,
``runs``, ``workflows``, ``sessions``, ``wakeups``, ``lessons``) is one new
file in this package and zero edits anywhere else; nothing maintains a
registry that could go stale.

Mirrors the mechanism aw-workspace's own ``src/cli/discovery.py`` uses for
top-level commands: ``pkgutil.iter_modules`` over a package directory, one
module per unit, no manual list.

**Group module contract** — every ``*.py`` in this package (module names
starting with ``_`` are skipped) must define:

    GROUP = "telegram-bot"              # the subcommand name, e.g.
                                         # `agents-platform telegram-bot ...`
    DESCRIPTION = "Manage the Telegram bots wired to Agents Platform agents"

    def register(sub) -> None:
        \"\"\"``sub`` is this group's own ``add_subparsers()`` return value.
        Add one ``sub.add_parser(...)`` per verb, and
        ``p.set_defaults(func=_some_handler)`` on each.\"\"\"
        p = sub.add_parser("list", help="...")
        p.set_defaults(func=_list)

    def _list(client: PlatformClient, ns: argparse.Namespace) -> int:
        ...   # return a process exit code

A module missing any of ``GROUP``/``DESCRIPTION``/``register`` is silently
skipped rather than failing ``discover_groups()`` outright — consistent
with how ``src/cli/discovery.py`` treats a broken app command, so one bad
group file degrades to "that group is missing" instead of taking down
``agents-platform --help`` entirely.
"""
from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType


def discover_groups() -> list[ModuleType]:
    groups: list[ModuleType] = []
    for _, name, _ in pkgutil.iter_modules(__path__):
        if name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        if hasattr(module, "GROUP") and hasattr(module, "DESCRIPTION") and hasattr(module, "register"):
            groups.append(module)
    groups.sort(key=lambda m: m.GROUP)
    return groups
