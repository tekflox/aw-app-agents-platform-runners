"""
Standalone entrypoint (ADR Decision 4) — run this app WITHOUT the
aw-workspace runtime, e.g. to develop/debug the /status route on its own:

    python -m agents_platform_runners_app                # binds 127.0.0.1:9407 (default)
    PORT=9408 python -m agents_platform_runners_app

Mounts the SAME ``build_routes()`` sub-app at the SAME prefix used in
integrated mode (``/api/apps/<slug>``) so client code and docs never need a
mode-specific path — see ``routes.py``. No UI (this app is infra-only, no
frontend bundle in aw-app.json's contributes).

Auth: standalone has **no** ``IdentityGuard`` — that is aw-workspace runtime
machinery, not app code (Decision 4). Default posture here is to bind
``127.0.0.1`` only.
"""
from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from .routes import build_routes

SLUG = "agents-platform-runners"  # must match aw-app.json's "id"
DEFAULT_PORT = 9407  # match aw-app.json's runtime.standalone.default_port


def build_standalone_app() -> FastAPI:
    app = FastAPI(title="agents-platform-runners (standalone)")
    app.mount(f"/api/apps/{SLUG}", build_routes())
    return app


app = build_standalone_app()


def main() -> None:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = os.environ.get("AW_APP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
