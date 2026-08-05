---
name: aw-create-app
description: >-
  Author a decoupled aw-workspace app from this template — manifest, tiers
  (Tier-1 in-process vs Tier-2 container), the capability/permission catalog,
  what an app can contribute (windows, nav, routes, system CLIs, DB tables,
  frontend bundles), the declarative window widget vocabulary, install +
  marketplace release, and how it shows up in the Apps grid. Use whenever you
  are asked to build a new aw-workspace app, add an app to the marketplace, or
  extend/port a monolith feature into a decoupled app.
---

# Creating a decoupled aw-workspace app

This skill ships **inside `aw-app-template`** and teaches how to turn this
repo into a real app. The template is itself a marketplace app (a minimal,
fully-wired "hello" example). Copy it, rename everything marked `TEMPLATE`/
`hello`, and follow the contract below.

> **Migration mindset:** most new apps are *ports* of an existing
> `agentic-workspace` monolith feature — base the app on the working monolith
> code (cite `file:line`) and port faithfully. Greenfield only when there is
> no monolith equivalent.

## 1. What an app is

A decoupled app is a self-contained repo (`aw-app-<name>`) with a manifest
(`aw-app.json`) at its root. The aw-workspace runtime fetches installed apps
into `/opt/aw-workspace/apps/<slug>/` (`AW_APPS_ROOT`, see `src/apps/fetch.py`)
and serves them under `/api/apps/<id>`.
Two tiers:

- **Tier-1 (`"tier": "inprocess"`)** — a Python plugin loaded into the
  workspace process. Cheapest; use for routes, DB tables, CLIs, windows,
  background tasks. Entrypoint: `runtime.entrypoint = "pkg.plugin:ClassName"`.
- **Tier-2 (`"tier": "container"`)** — a sidecar container from a prebuilt
  image, spawned over the host podman socket and reverse-proxied. Use when the
  app needs its own runtime/binaries (e.g. a browser). Needs
  `permissions: ["containers:manage"]` and `runtime.image/port/run_flags_needed`.

## 2. The manifest (`aw-app.json`)

```jsonc
{
  "manifest_version": 1,
  "id": "myapp",                       // unique slug; becomes /api/apps/myapp
  "name": "My App",
  "version": "0.1.0",
  "description": "...",
  "tier": "inprocess",                 // or "container"
  "publisher": "TekFlox",
  "requires_ui_refresh": true,          // SPA re-fetches contributions after install/config-save
  "resource_estimate": { "cpu": "low", "memory": "low", "disk": "low" },

  "runtime": {                          // Tier-1:
    "python": ">=3.11",
    "entrypoint": "myapp_app.plugin:MyAppPlugin",
    "pip_requires": []
  },
  // Tier-2 instead: "runtime": { "image": "ghcr.io/tekflox/aw-myapp:latest",
  //                              "port": 7900, "resources": {"cpus":0.5,"mem_mb":1024},
  //                              "run_flags_needed": ["--shm-size=1g"] }

  "permissions": [ "routes:register" ],  // capability grants — see §3
  "contributes": { /* see §4 */ },
  "config_schema": { "type": "object", "properties": {}, "required": [] },
  "dependencies": {},
  "migrations": {}
}
```

Validate it: `python tests/validate_manifest.py` (schema in `schemas/aw-app.schema.json`).

### Reacting to a settings save (`on_config_saved`)

`POST /api/apps/<id>/config` updates `ctx.config` and, if your plugin
defines it, awaits `plugin.on_config_saved(ctx)` right after — optional,
duck-typed (no base class required), a no-op if you don't define it. Use it
when a config change needs to do something beyond being read lazily next
time — e.g. `aw-app-mcp-tools` regenerates its own root `mcp.json` on disk
here from a per-tool enable/disable toggle, which is also why it sets
`contributes.mcp.reload_on_save: true` (§4): that flag makes aw-workspace
call the MCP Gateway's `POST /reload` right after your hook returns, so
whatever file you just wrote takes effect immediately.

```python
class MyAppPlugin:
    async def activate(self, ctx):
        ...
    async def on_config_saved(self, ctx):
        # ctx.config already reflects the just-saved values here.
        ...
    async def deactivate(self):
        ...
```

## 3. Capability catalog (`permissions`)

Only request what you use — each is enforced by the runtime's `AppContext`.
Authoritative source: aw-workspace `src/apps/capabilities.py` (mirrored in
aw-backend `src/api/app_capabilities.py`) — keep `schemas/aw-app.schema.json`
in this repo in sync if that catalog ever grows.

| Capability | Risk | Grants |
|---|---|---|
| `routes:register` | low | mount `/api/apps/<id>/*` routes |
| `db:own-tables` | low | create/use app-owned workspace tables |
| `commands:install` | low | install CLIs/commands that survive restart |
| `service:manage` | low | register a start/stop background service |
| `watchdog:tasks` | low | register in-process periodic tasks |
| `net:outbound` | low | outbound HTTP from Tier-1 code |
| `fs:workspace-data` | low | read/write the app's own data dir |
| `secrets:own` | low | request the app's own secrets |
| `notifications:send` | low | fire a workspace notification |
| `containers:manage` | **high** | run/manage sidecar containers (Tier-2) |
| `ui:code` | **high** | load the app's JS bundle into the SPA |
| `ui:slots:<slot>` | low | render into a named SPA slot (e.g. `core.nav.workspace`) |
| `config:extend:<app>` | high | write config into another app's extension point |

> **`routes:local` (proposed, NOT yet live)** — ADR Decision 2
> (`docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-mode.md`)
> proposes a `routes:local` capability that lets specific sub-paths of your
> app skip the JWT check when called from `127.0.0.1`/`::1` (agent-driven
> endpoints, e.g. a future `aw-app-devctl` `/eval`). As of 2026-07-28 this
> capability does not exist in `capabilities.py` and the `local_paths`
> manifest field has no runtime effect — do not request it yet. See §4 for
> the documented (forward-looking) shape.

## 4. What an app contributes (`contributes`)

- **`windows`** — declarative windows opened from the app's card. Each:
  `{ "id": "myapp.main", "title": "...", "body": { "type": "declarative", "spec": "windows/main.json" } }`.
- **`nav`** — OPTIONAL top-bar buttons. `section: "workspace"` → Workspace menu.
  **Omit nav for normal apps** — every installed app already appears as a card
  in the **Apps grid** (the launcher reads `GET /api/apps`); nav is only for
  apps that also want a persistent menu entry.
- **`routes`** — `[{ "prefix": "/api/apps/myapp" }]` (needs `routes:register`).
  See §5 for the full backend contract, and the not-yet-live `local_paths`
  escape:
  ```jsonc
  "contributes": {
    "routes": [
      {
        "prefix": "/api/apps/myapp",
        // Forward-looking (ADR Decision 2) — needs "routes:local", not live yet.
        "local_paths": ["/eval", "/tabs"]
      }
    ]
  }
  ```
- **`system_clis`** — `[{ "name": "hello", "installer": "scripts/install_hello.sh" }]`
  (needs `commands:install`).
- **`db`** — app-owned tables (needs `db:own-tables`).
- **`frontend`** — a JS bundle mounted into granted slots (needs `ui:code`;
  unsigned apps are downgraded to iframe mode). See §6 for the source layout:
  ```jsonc
  "contributes": {
    "frontend": {
      "mode": "component",       // or "iframe" / "declarative" — see loadPlugin.js
      "bundle": "ui/dist/myapp.js"
    }
  }
  ```
- **`mcp`** — this app's MCP-related declarations. Two independent, optional
  sibling keys on the same object:
  ```jsonc
  "contributes": {
    "mcp": {
      // Marketplace "What you get" detail-view list (Manifest.what_you_get) —
      // purely informational, doesn't affect what actually gets exposed.
      "provides": [{ "name": "my_tool" }, "another_tool"],
      // True if this app ALSO ships its own root mcp.json — aw-mcp-gateway's
      // app-scan reads that file directly (see aw-app-mcp-tools for the
      // reference implementation: a Playwright + echo MCP tool bundle with
      // per-tool enable/disable toggles). Setting this tells aw-workspace's
      // POST /api/apps/<id>/config to call the installed mcp-gateway app's
      // POST /reload right after your on_config_saved hook returns (see
      // below) — so a settings change that affects your mcp.json takes
      // effect immediately, no gateway restart.
      "reload_on_save": true
    }
  }
  ```

### Window widget vocabulary (`windows/*.json`)

`layout: "stack"`, `regions: [{ id, widgets: [...] }]`. Widget `type`:
`markdown`, `list`, `button`, `collapsible`, `form`, `auth_status`,
`iframe` (`{ src }`, an `/api/*` path — rewritten to the workspace API host),
`app_iframe` (`{ app_id, path }` — resolves to the app's **external
subdomain** `https://<app_id>.app.<slug>.workspace.<apex>`, honoring the LAN
fast-path; use this to surface a Tier-2 container's own web UI).

## 5. Backend routes (HTTP + WS)

Every app that has a server side ships exactly ONE mode-agnostic factory,
`build_routes() -> FastAPI`, with **relative** paths (no `/api/apps/<id>`
prefix inside the app — see `template_app/routes.py`):

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

def build_routes() -> FastAPI:
    app = FastAPI(title="myapp")

    @app.get("/hello")
    async def hello():
        return {"message": "hi"}

    @app.websocket("/ws/echo")           # app-local WS; externally stays app-namespaced
    async def ws_echo(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            pass

    return app
```

Register it in `plugin.py`'s `activate(ctx)` via the gated facade:
`ctx.routes.register(build_routes())` — the runtime mounts it at
`/api/apps/<id>` behind `IdentityGuard` (bearer/cookie for HTTP, `?token=`
then cookie for WS). **Apps never implement their own auth in integrated
mode.** Full canonical path shapes:

```
/api/apps/<id>/...           HTTP routes
/api/apps/<id>/ws/<name>     in-process app WebSocket routes
/ws/apps/<id>/<name>         reserved edge namespace for app-owned WebSockets
/ws/*                        core/control-plane only
```

`local_paths` (agent-callable, no-JWT-from-localhost endpoints) is a
**proposed, not-yet-live** manifest extension — see §3/§4, don't request the
`routes:local` capability until it lands in `capabilities.py`.

## 6. Frontend code (`register(host)`, headless services, host helpers)

A frontend contribution ships `ui/src/plugin.js` with ONE required export:

```js
export function register(host) {
  // host.React / host.h / host.sdk are the SHARED instances — never import
  // your own React (breaks the "exactly one React instance" invariant).
  function MyPill() {
    const [msg, setMsg] = host.React.useState('…');
    host.React.useEffect(() => {
      host.sdk.api.fetch('/api/apps/myapp/hello')
        .then((r) => r.json()).then((d) => setMsg(d.message));
    }, []);
    return host.h('span', {}, msg);
  }
  host.registerSlot('core.nav', MyPill);   // needs "ui:slots:core.nav" grant

  // Headless pattern — background work with NO slot, teardown via onDispose:
  const ws = new WebSocket(host.sdk.api.wsUrl('/api/apps/myapp/ws/echo'));
  host.onDispose(() => ws.close());
}
export default register;
```

`register(host)` **may be headless** — start background work (open a WS,
install listeners) without registering any slot, as long as every teardown
goes through `host.onDispose(fn)`. This is the sanctioned pattern for a
client that has no UI of its own to show (e.g. a future devctl client).

`host.app.{apiUrl,fetch,wsUrl}` scoped helpers (`app.apiUrl('/hello')` →
`/api/apps/<id>/hello`, no hand-built prefix) are **proposed in ADR Decision
3 but not yet landed** in aw-frontend's `pluginHost.js` — until then, build
the `/api/apps/<id>/...` prefix yourself (see `ui/src/plugin.js` in this
repo) and use `host.sdk.api.fetch`/`host.sdk.api.wsUrl` for the actual
network call (those two already exist and are BYOD-correct — never
hand-build a raw `fetch()`/`new WebSocket()` URL yourself).

Only `component` mode (needs `ui:code`, **signed/marketplace-only**) runs
`register(host)` at all — an unsigned/side-loaded install is silently
downgraded to `iframe` mode (`loadPlugin.js`'s `effectiveMode()`).

## 7. Standalone mode

Ship `<pkg>/__main__.py` so your app runs with `python -m <pkg>` completely
outside the workspace runtime — useful for developing/demoing the UI, or as
a piloted service on its own (ADR Decision 4):

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .routes import build_routes

def build_standalone_app() -> FastAPI:
    app = FastAPI()
    app.mount("/api/apps/myapp", build_routes())     # SAME prefix as integrated
    app.mount("/", StaticFiles(directory="ui/dist", html=True), name="ui")
    return app
```

Declare it (optional, tolerated forward-compatibly by the v1 validator):

```jsonc
"runtime": {
  "entrypoint": "myapp_app.plugin:MyAppPlugin",
  "standalone": { "module": "myapp_app", "default_port": 9400 }
}
```

**No `IdentityGuard` in standalone** — that's runtime machinery, not app
code. Default posture: bind `127.0.0.1`. A shared-token/`AW_APP_TOKEN` gate
is the app's own responsibility if you deploy it more openly. See
`template_app/__main__.py` for the full pattern, and `ui/vite.config.js` /
`ui/src/standalone.js` for the matching frontend half (same `client.js`
core as the plugin bundle, just same-origin relative URLs instead of
`host.sdk.api.*`).

## 8. What stays in core (nothing of yours)

The workspace runtime — not your app — owns: the mount point
(`/api/apps/<id>`), the `IdentityGuard` auth check on every integrated HTTP
and WS request, serving your built bundle from `<pkg>/ui/dist/` at
`/api/apps/<id>/ui/<file>`, the contributions/slot-registry feed the SPA
reads, and the single shared React/SDK instance every `component`-mode
bundle resolves through `window.__AW_PLUGIN_HOST__`. **Never**: add a new
root-level route or WS path for an app feature (`/ws/devctl` was the
mistake this ADR undoes; use `/ws/apps/<id>/...` for top-level app-owned
WS namespaces), implement your own auth on an integrated route,
or bundle your own copy of React into a plugin bundle.

## 9. Depending on another app (`dependencies.apps`)

If your app needs another app's routes/tools to already be loaded — e.g. an
app that contributes an `mcp.json` needs the **MCP Gateway** app running so
its tools get picked up — declare it in the manifest's `dependencies.apps`
list:

```jsonc
"dependencies": {
  "apps": [
    {
      "id": "mcp-gateway",              // the dependency's app id/slug
      "version": ">=0.1.0",             // optional semver constraint (informational)
      "required": true,                 // default true; set false/omit for an optional dep
      "reason": "Contributes mcp.json definitions the gateway discovers and merges."
    }
  ]
}
```

Enforced by `AppReconciler._install_dependencies` (aw-workspace
`src/apps/reconciler.py`) — **not** a `depends_on` key; the field is
`dependencies.apps`, kept as a loose/forward-compatible object so unrelated
metadata can live alongside it. Behavior:

- `POST /api/apps/install` (and `aw-workspace marketplace install <id>` in
  the CLI, `bin/aw-workspace`) resolves every `required` entry in
  `dependencies.apps` **before** installing your app — pulling each
  dependency from the local mirror, the marketplace catalog, or an explicit
  `repo`/`package_dir` on the dependency entry, in that order. Already-loaded
  dependencies are skipped.
- A short string entry (`"apps": ["mcp-gateway"]`) is equivalent to
  `{"id": "mcp-gateway"}` — required by default.
- Set `"required": false` or `"optional": true` on an entry to make it a soft
  dependency (documented, but not auto-installed or blocking).
- Cyclic dependency chains raise instead of hanging (`cyclic app dependency
  chain: a -> b -> a`).
- The reconciler's removal pass also protects a loaded required dependency
  from being uninstalled just because only the *dependent* app was named in
  the desired set (`_loaded_dependency_closure`) — so installing `mcp-tools`
  keeps `mcp-gateway` around even if only `mcp-tools` is in the cloud
  registry's desired-apps row.

Real example: `aw-app-mcp-tools/aw-app.json` declares `mcp-gateway` as a
required dependency for exactly this reason — see that repo's manifest.

## 10. How it shows up + install

- Installed apps live in `/opt/aw-workspace/apps/<slug>/` (`AW_APPS_ROOT`);
  the runtime loads their manifests and serves `GET /api/apps` (list) +
  `GET /api/apps/-/contributions` (live-refetched nav/windows) +
  `GET /api/apps/-/catalog` (marketplace).
- The SPA "Apps" launcher lists them as cards; clicking opens the default window.
- Install paths: `POST /api/apps/install` (fetch repo + reconcile), the
  reconciler's "Install My Apps", or a hand-sync of the app dir + workspace reload.
- **Marketplace:** the app must be listed in the marketplace catalog source
  (public, tokenless raw-GET). Ship `.github/workflows/release.yml` (see this
  repo) to cut versioned releases; the catalog references the repo.

## 11. Reference apps (read these before building)

- `aw-app-template` (this repo) — Tier-1 + `commands:install` (the `hello`
  CLI) + `routes:register`/`ui:code` (a `/hello` + `/ws/echo` sub-app, a
  `core.nav` slot component, and standalone mode — §5–§7 above).
- `aw-app-whiteboard` — Tier-1 + `routes:register` + `db:own-tables` + a window.
- `aw-app-devctl` — Tier-1 + `routes:register` (talks CDP to another app's container).
- `aw-app-browser` — Tier-2 container + `app_iframe` window → external subdomain (noVNC).

## 12. Checklist

1. Copy this repo → `aw-app-<name>`; rename `template_app/`, `id`, `name`, entrypoint.
2. Pick the tier; declare only the capabilities you use.
3. Add your `contributes` (managed window for a full UI, declarative window
   for a small settings/control panel, routes for a backend, frontend bundle,
   etc. — §4–§7 and `docs/window-contract.md`).
4. `python tests/validate_manifest.py` green; `pytest tests/` green (routes
   TestClient + standalone boot smoke); `npm run build` in `ui/` if you have
   a frontend; `tests/standalone_test.sh` green.
5. Push to `github.com/tekflox/aw-app-<name>`; wire `release.yml`; add to the
   marketplace catalog.
6. Install and confirm the card shows in the Apps grid and opens.
