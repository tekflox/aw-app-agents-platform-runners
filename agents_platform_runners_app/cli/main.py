"""``agents-platform`` root parser — builds one subparser per auto-discovered
group (see ``cli/groups/__init__.py``) and dispatches to it. Contains no
knowledge of any specific group; a group's verbs, flags and REST calls all
live in that group's own module.

``--json`` has to work both before the group (``agents-platform --json
telegram-bot list``, the only position argparse's root parser can see on its
own) and trailing after the verb (``agents-platform telegram-bot list
--json``, what every existing command in this workspace does — see
``docs/architecture/cli.md``). Trailing only works if the flag is *also*
declared on the leaf verb parser, since a subparsers action hands the entire
remainder of argv to the child parser. ``_json_parent()`` is the single
place ``--json`` is declared; every parser that needs it takes a *fresh*
instance via ``parents=[json_parent()]``. ``_VerbSubparsers`` calls it for
every verb a group registers, so a future group module never has to know
``--json`` exists.

Each parser gets its own ``Action`` object rather than sharing one, because
``ArgumentParser.set_defaults()`` mutates ``Action.default`` in place on
every parser holding that action — a single shared instance meant the root
parser's ``set_defaults(as_json=False)`` (needed for the "neither position
used it" fallback) silently rewrote the leaf verb parsers' default too,
which put ``as_json`` back in every subnamespace and reintroduced the exact
clobbering this design exists to avoid (root ``--json`` overridden back to
False by the leaf's now-``False``-not-``SUPPRESS`` default as the dispatch
chain copied it back up). Each parser's default must stay ``SUPPRESS`` so a
level that never saw ``--json`` doesn't add the key to its subnamespace at
all — only then does the copy-upward in the subparsers dispatch chain leave
whichever position actually set it alone.
"""
from __future__ import annotations

import argparse

from .client import NotConfigured, PlatformClient, PlatformError
from .groups import discover_groups
from .output import fail


def _json_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", dest="as_json", action="store_true",
                         default=argparse.SUPPRESS,
                         help="print raw JSON instead of a table/confirmation")
    return parent


class _VerbSubparsers:
    """Wraps a group's ``add_subparsers()`` action so every verb parser it
    creates also accepts trailing ``--json``, without the group module
    itself referencing ``_json_parent``."""

    def __init__(self, action):
        self._action = action

    def add_parser(self, name, **kwargs):
        parents = [_json_parent(), *kwargs.pop("parents", [])]
        return self._action.add_parser(name, parents=parents, **kwargs)

    def __getattr__(self, name):
        return getattr(self._action, name)


def _build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Control Agents Platform: "
                                      "telegram bots, agents, runs (and more)",
                                      parents=[_json_parent()])
    parser.set_defaults(as_json=False)
    top = parser.add_subparsers(dest="group", required=True)
    for module in discover_groups():
        group_parser = top.add_parser(module.GROUP, help=module.DESCRIPTION)
        sub = group_parser.add_subparsers(dest="verb", required=True)
        module.register(_VerbSubparsers(sub))
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
