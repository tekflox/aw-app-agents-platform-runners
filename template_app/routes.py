"""
template_app's mode-agnostic FastAPI sub-app (ADR Decision 2/6:
docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-mode.md).

``build_routes()`` returns the SAME sub-app object used in both modes:

* **integrated** — ``plugin.py`` hands it to ``ctx.routes.register(...)``,
  which mounts it at ``/api/apps/<slug>`` behind the runtime's
  ``IdentityGuard`` (aw-workspace ``src/apps/runtime.py``). Apps never
  implement their own auth in this mode.
* **standalone** — ``__main__.py`` mounts it at the SAME prefix itself, with
  no ``IdentityGuard`` (see that file's docstring for the auth posture).

Keep every path here RELATIVE (no ``/api/apps/<slug>`` prefix) so client
code and docs use one path shape in both modes:

    integrated: /api/apps/hello/hello        /api/apps/hello/ws/echo
    standalone: /api/apps/hello/hello        /api/apps/hello/ws/echo

Integrated in-process WS path shape (Decision 2):
``/api/apps/<slug>/ws/<name>`` — this sub-app declares
``@app.websocket("/ws/<name>")``. Browser-facing app-owned WebSockets that
need a top-level edge namespace belong under ``/ws/apps/<slug>/...``. Root
``/ws/*`` stays reserved for core/control-plane sockets
(``/ws/terminal``, ``/ws/notifications``, ...) — never add a new root-level
WS route for an app feature.

``local_paths`` escape (Decision 2): an app that needs an endpoint callable
without a JWT from inside the workspace's own network namespace (e.g. an
agent-driven eval endpoint, like aw-app-devctl's planned ``/eval``/``/tabs``)
declares it in ``aw-app.json``:

    "contributes": { "routes": [ { "prefix": "/api/apps/<slug>",
                                    "local_paths": ["/eval", "/tabs"] } ] }

gated by a ``routes:local`` capability. TODO(framework, 2026-07-28): neither
``routes:local`` nor the IdentityGuard bypass it needs exist yet in
aw-workspace (``src/apps/capabilities.py``, ``src/apps/runtime.py``) — this
template ships no ``local_paths`` route and its manifest does not request
the capability; see ``skills/aw-create-app/SKILL.md`` for the documented
(not-yet-live) shape.
"""
from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


def build_routes() -> FastAPI:
    """Mode-agnostic factory — call this fresh for each mode (plugin.py /
    __main__.py both call it exactly once)."""
    app = FastAPI(title="template")

    @app.get("/hello")
    async def hello() -> dict:
        return {"message": "Hello from the aw-app-template sub-app"}

    @app.websocket("/ws/echo")
    async def ws_echo(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                text = await ws.receive_text()
                await ws.send_text(text)
        except WebSocketDisconnect:
            pass

    return app
