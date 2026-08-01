"""
Entrypoint referenced by aw-app.json's runtime.entrypoint
("template_app.plugin:HelloAppPlugin").

Plugs into the real F4 framework runtime: activate(ctx) (1) installs each
declared system CLI THROUGH the gated ``ctx.commands`` facade (capability
``commands:install``), so every install is journaled and the framework
reverts them on uninstall by replaying the journal (running
scripts/uninstall.sh once), and (2) registers the backend sub-app from
``routes.py`` THROUGH the gated ``ctx.routes`` facade (capability
``routes:register`` — ADR Decision 2/6, docs/knowledge_base/docs/
architecture/adr-app-front-back-routes-dual-mode.md), mounted by the
runtime at ``/api/apps/hello``. The install scripts are idempotent, so the
reconciler safely re-runs activate on every boot / workspace recreation.

TEMPLATE: this is the whole pattern every aw-app-* Tier-1 app uses — copy
it as-is (just rename the class/module) unless your app needs something
`contributes.system_clis`/`contributes.routes` can't express (a settings/
config route needs its own handler in routes.py; a background service or a
frontend nav entry — see aw-app-git, aw-app-presentations, aw-app-
whiteboard for those patterns instead).
"""

from __future__ import annotations

import json
import logging
import os

from . import routes as routes_mod

log = logging.getLogger("aw_apps.hello")


class HelloAppPlugin:
    async def activate(self, ctx) -> None:
        with open(os.path.join(ctx.package_dir, "aw-app.json"), encoding="utf-8") as f:
            manifest = json.load(f)

        greeting = (getattr(ctx, "config", {}) or {}).get("greeting") or "Hello"
        os.environ["AW_APP_HELLO_GREETING"] = str(greeting)

        clis = manifest.get("contributes", {}).get("system_clis", [])
        installed = []
        for cli in clis:
            ctx.commands.install_system_cli(
                cli["name"], cli["installer"], uninstall="scripts/uninstall.sh"
            )
            installed.append(cli["name"])

        ctx.routes.register(routes_mod.build_routes())

        log.info(
            "aw-app-template activated: installed %s (greeting=%s), routes mounted",
            installed, greeting,
        )

    async def deactivate(self) -> None:
        # Revert is driven by the framework's journal reverse-replay (it runs
        # scripts/uninstall.sh once on uninstall) — nothing to undo here.
        log.info("aw-app-template deactivated")
