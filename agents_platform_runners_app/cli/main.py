"""``agents-platform`` root parser — builds one subparser per auto-discovered
group (see ``cli/groups/__init__.py``) and dispatches to it. Contains no
knowledge of any specific group; a group's verbs, flags and REST calls all
live in that group's own module.
"""
from __future__ import annotations

import argparse

from .client import NotConfigured, PlatformClient, PlatformError
from .groups import discover_groups
from .output import fail


def _build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Control Agents Platform: "
                                      "telegram bots, agents, runs (and more)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                         help="print raw JSON instead of a table/confirmation")
    top = parser.add_subparsers(dest="group", required=True)
    for module in discover_groups():
        group_parser = top.add_parser(module.GROUP, help=module.DESCRIPTION)
        sub = group_parser.add_subparsers(dest="verb", required=True)
        module.register(sub)
    return parser


def main(argv: list[str], prog: str = "agents-platform") -> int:
    parser = _build_parser(prog)
    ns = parser.parse_args(argv)

    if not hasattr(ns, "func"):
        parser.print_help()
        return 2

    client = PlatformClient()
    try:
        return ns.func(client, ns)
    except NotConfigured as e:
        fail(f"{prog}: {e}")
        return 2
    except PlatformError as e:
        if e.status == 401:
            fail(
                f"{prog}: agents-platform rejected the token (401).\n"
                "The app refreshes it automatically; if this persists, restart it:\n"
                "  aw-workspace-cli restart agents-platform-runners"
            )
        else:
            fail(f"{prog}: {e}")
        return 1
