"""``python -m agents_platform_runners_app.cli`` — dev/test entrypoint.

The real entrypoint is ``aw-workspace-cli agents-platform``, via the shim at
``commands/agents_platform.py``; this module exists so the package can be
exercised directly (e.g. from tests, or a checkout not installed as an app).
"""
from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
