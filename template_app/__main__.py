"""
Standalone entrypoint (ADR Decision 4) — run this app WITHOUT the
aw-workspace runtime, e.g. to develop/demo the UI on its own or as a
piloted service:

    python -m template_app                # binds 127.0.0.1:9400 (default)
    PORT=9401 python -m template_app

Mounts the SAME ``build_routes()`` sub-app at the SAME prefix used in
integrated mode (``/api/apps/<slug>``) so client code and docs never need a
mode-specific path — see ``routes.py``. Then serves ``ui/dist/`` (built via
``npm run build`` in ``ui/``) as static files, with ``html=True`` so
``GET /`` (and any unknown path) falls back to ``ui/dist/index.html`` — the
standalone page loaded by ``ui/src/standalone.js`` (relative ``apiUrl``,
same-origin ``wsUrl``).

Auth: standalone has **no** ``IdentityGuard`` — that is aw-workspace runtime
machinery, not app code (Decision 4). Default posture here is to bind
``127.0.0.1`` only. If you need a shared-secret gate for a LAN/public
deployment, check an env var (e.g. ``AW_APP_TOKEN``) inside your own routes
— that's the app's responsibility, not the framework's.
"""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routes import build_routes

SLUG = "hello"  # TEMPLATE: must match aw-app.json's "id"
DEFAULT_PORT = 9400  # TEMPLATE: match aw-app.json's runtime.standalone.default_port

APP_ROOT = Path(__file__).resolve().parent.parent
UI_DIST = APP_ROOT / "ui" / "dist"


def build_standalone_app() -> FastAPI:
    app = FastAPI(title="template (standalone)")
    app.mount(f"/api/apps/{SLUG}", build_routes())

    if UI_DIST.is_dir():
        # html=True: GET / -> index.html, and any non-file path falls back to
        # it too (a plain standalone page, not an SPA router, but this keeps
        # deep-linking harmless instead of a bare 404).
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")

    return app


app = build_standalone_app()


def main() -> None:
    if not UI_DIST.is_dir():
        print(f"NOTE: {UI_DIST} not built yet — run `npm run build` in ui/ first "
              f"(API routes still work without it).")
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = os.environ.get("AW_APP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
