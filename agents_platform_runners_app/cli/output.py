"""The one output convention every ``agents-platform`` group follows — no
group invents its own table/JSON rendering.

- List verbs: table by default, raw JSON with ``--json``.
- Mutating verbs: one-line confirmation by default, raw response envelope
  with ``--json``.
- ``--json`` must never un-redact something the table path redacts — that
  is a per-caller responsibility (pass already-redacted rows to
  ``emit_json`` too), not something this module can enforce structurally.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Sequence


def emit_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def emit_table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> None:
    widths = [len(h) for h in headers]
    str_rows = [[str(c) for c in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    if not str_rows:
        print("(none)")
        return
    for row in str_rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


def ok(msg: str) -> None:
    print(msg)


def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
