# aw-app-template

Starting point for a new AW workspace app (`aw-app.json` manifest schema v1).
Every `aw-app-*` app should be born
from **the latest version of this template**, not copy-pasted from an
existing app — this repo already carries the full, currently-correct
skeleton: manifest, plugin, tests, and a CI/CD pipeline wired to the
`tekflox/aw-marketplace` release + catalog-sync automation, including every
fix that's landed on that pipeline so far (test-gating before release,
correct permissions ceiling, auto-merge).

It's a real, working app — not just files. `hello` installs one trivial CLI
that prints a configurable greeting, contributes a tiny backend sub-app
(`GET /hello` + `WS /ws/echo` inside the app mount) and a `core.nav` frontend slot, and runs
standalone too (`python -m template_app`) — so cloning this template and
pushing to `master` gives you a green CI run, a tagged release, and a
marketplace catalog entry before you've changed a single line. See
`docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-mode.md`
(Decision 6) for the design this scaffold implements.

## Use this template

GitHub's **"Use this template"** button (top of the repo page) creates a
new repo seeded from this one's current `master` — no git history, no fork
relationship, just a fresh copy. From the CLI:

```bash
gh repo create tekflox/aw-app-<yourapp> --template tekflox/aw-app-template --public
git clone https://github.com/tekflox/aw-app-<yourapp>
```

Then rename everything marked **TEMPLATE** in comments and every `hello`/
`template_app` occurrence:

1. **`aw-app.json`** — `id`, `name`, `description`, `runtime.entrypoint`,
   `contributes.system_clis`, `config_schema` (or remove it if your app has
   no config knobs — see `aw-app-brew`'s manifest for a config-free example).
2. **`template_app/`** — rename the directory + the class in `plugin.py`
   (and update `runtime.entrypoint` in `aw-app.json` to match). Keep,
   change, or delete each piece independently — they're not all-or-nothing:
   - `installer.py` / `scripts/` — the `hello` CLI install pattern
     (`commands:install`). Delete if your app has no CLI.
   - `routes.py` / `plugin.py`'s `ctx.routes.register(...)` — the backend
     sub-app pattern (`routes:register`, GET + WS). Delete if your app has
     no backend routes.
   - `__main__.py` — the standalone-mode entry (`python -m <pkg>`). Delete
     if your app only ever runs integrated.
   - See the **`aw-create-app`** skill (`skills/aw-create-app/SKILL.md`,
     §5–§8) for the full backend-routes / frontend-code / standalone
     contract, or a sibling app for a bigger example of one piece:
     - `aw-app-git` — a settings panel + OAuth device-flow login route.
     - `aw-app-whiteboard` — `contributes.nav` + `contributes.frontend`
       (a top-nav entry + window), `db:own-tables`.
     - `aw-app-devctl` — `routes:register` talking CDP to another app's
       container.
     - `aw-app-browser` — `tier: container` (Tier-2, a sidecar container
       instead of in-process Python).
3. **`scripts/`** — replace `install_hello.sh` with your app's real
   installer(s) (one script per CLI is the convention, but a single script
   installing several related tools — like `aw-app-essentials`'s Node.js
   toolkit — is fine too). Keep `uninstall.sh` in sync — it's the one
   script the framework's journal reverse-replay calls on uninstall, so it
   must reverse *everything* every `install_*.sh` here does.
4. **`ui/`** — rename `SLUG` in `src/plugin.js`/`src/standalone.js` (and
   `template_app/__main__.py`'s `SLUG`) to match your app's `id`. Delete the
   whole directory (+ `contributes.frontend`/`ui:code`/`ui:slots:*` in
   `aw-app.json`) if your app has no frontend code — a declarative `windows`
   spec doesn't need any of this.
5. **`tests/`** — `validate_manifest.py` needs no changes (fully generic).
   Update `test_installer.py`'s assertions to match your real
   `installer.py` functions, `test_routes.py`/`test_standalone.py` to match
   your real routes, and `standalone_test.sh` to install/check your real
   CLI(s).
6. **`.github/workflows/release.yml`** — no changes needed. It calls
   `tekflox/aw-marketplace`'s shared `app-release.yml`, which runs
   `tests/validate_manifest.py` + `tests/test_*.py` on every push to
   `master` — a failing test stops the release before any version bump,
   tag, or marketplace catalog write happens.
7. **`README.md`** — replace this file with your app's own (what it
   installs, how it's configured, what's been tested where).

Finally, get your new app **listed** in the marketplace: your first push
past a passing test suite auto-tags a release and opens a
`chore(sync): <id> -> vX.Y.Z` PR against `tekflox/aw-marketplace`'s
`apps.json` (auto-merged for first-party TekFlox sources) — nothing to do
by hand.

## Layout

- `aw-app.json` — the manifest (`id: hello`, `tier: inprocess`).
- `schemas/aw-app.schema.json` — local structural validator; same schema
  every `aw-app-*` repo validates against — keep it in sync with
  `aw-workspace`'s `src/apps/capabilities.py` (the authoritative permission
  catalog) if that ever adds a new capability string.
- `scripts/install_hello.sh` — installs a trivial `hello` command into the
  workspace's persistent bin dir (`~/.aw-workspace/bin`, on PATH, survives
  restarts). Idempotent.
- `scripts/uninstall.sh` — reverses it.
- `template_app/plugin.py` — `HelloAppPlugin` entrypoint; `activate(ctx)`
  installs the CLI via the gated `ctx.commands` facade (capability
  `commands:install`) so it's journaled and the framework reverts it on
  uninstall, and registers `routes.py`'s sub-app via `ctx.routes` (capability
  `routes:register`).
- `template_app/installer.py` — the same install logic as a plain
  subprocess-calling module (no framework `ctx` needed) — used by the tests
  below.
- `template_app/routes.py` — `build_routes() -> FastAPI`, the mode-agnostic
  backend sub-app (`GET /hello`, `WS /ws/echo` inside the app mount) shared by integrated mode
  (`plugin.py`) and standalone mode (`__main__.py`) — ADR Decision 2/4/6.
- `template_app/__main__.py` — standalone entrypoint (`python -m
  template_app`): mounts `routes.py`'s sub-app at the same `/api/apps/hello`
  prefix, serves `ui/dist/` statically, no `IdentityGuard`.
- `ui/` — the frontend half, mode-agnostic (ADR Decision 3/4): `src/client.js`
  is the framework-free core; `src/plugin.js` is the integrated-mode
  `register(host)` entry (a `core.nav` slot component + a headless WS
  client), built by Vite in **lib mode** with `react`/`react-dom` externalized
  (`vite.config.js --mode plugin` → `dist/template.js`, referenced from
  `aw-app.json`'s `contributes.frontend.bundle`); `src/standalone.js` +
  `index.html` is the standalone page (`vite.config.js --mode standalone` →
  `dist/index.html` + assets). `npm run build` in `ui/` runs both, into the
  SAME `ui/dist/`.
- `tests/validate_manifest.py` — validates `aw-app.json` against the schema
  + checks every `system_clis` installer path exists on disk.
- `tests/test_installer.py` — unit tests (subprocess mocked, no real
  installs) — runs in CI on every push, gating the release.
- `tests/test_routes.py` — `TestClient` coverage of `routes.py`'s sub-app
  (GET `/hello`, WS `/ws/echo`) — runs in CI.

### WebSocket namespace rule

Do not claim root `/ws/*` for app features. Root `/ws/*` is reserved for AW
core/control-plane sockets. App-owned browser WebSockets must live in an app
namespace:

- current in-process mount shape: `/api/apps/<slug>/ws/<name>`
- reserved edge namespace for app-owned sockets: `/ws/apps/<slug>/<name>`

When adding a top-level WebSocket route or Caddy/edge mapping for an app, use
`/ws/apps/<slug>/...`, never `/ws/...`.
- `tests/test_standalone.py` — boots `__main__.py`'s standalone app and hits
  the mounted API (plus the static UI mount, once `ui/` is built) — runs in CI.
- `tests/standalone_test.sh` — installs `hello` for real and checks
  resolution + output; run inside the aw-workspace container (not part of
  CI — needs the real target environment).

### App window contract

Window definitions live in `contributes.windows[]`, but shared window chrome
lives in `aw-frontend`, not in each app. Full app surfaces should declare a
managed window:

```jsonc
{
  "id": "myapp.main",
  "title": "My App",
  "icon": "box",
  "body": { "type": "managed_app", "kind": "web", "path": "/" }
}
```

Use `body.type: "declarative"` only for focused settings/control panels that
need the widget vocabulary. Managed windows and `tier: "container"` apps also
receive framework-owned settings automatically: `auto_start`,
`auth_required`, and `public`. `aw-frontend` renders those toggles, and
`aw-workspace` persists and enforces them. App packages should not implement
duplicate lifecycle/auth/public toggles. See `docs/window-contract.md`.

## CI/CD

`tests/validate_manifest.py` and `tests/test_*.py` run in
`tekflox/aw-marketplace`'s shared `app-release.yml` reusable workflow on
every push to `master` — a failure stops the release **before** any version
bump, tag, or marketplace catalog sync happens. See that repo's
`scripts/bump_version.py` for the semver-bump-from-commit-messages logic and
`scripts/sync_catalog_entry.py` for what fields get written into
`apps.json` automatically (name/description/publisher/resource_estimate —
`has_config`/`bootstrap`/`icon`/`tags`/`category` are set once by hand on
first listing and not auto-synced afterward).

## Contributing a skill (`contributes.skills`)

This app ships a **skill** — `skills/aw-create-app/SKILL.md` — that teaches an
agent how to author a new workspace app from this template (manifest, tiers,
the capability catalog, contributes, the window widget vocabulary, install +
marketplace release). It's declared in the manifest:

```jsonc
"contributes": {
  "skills": [
    { "id": "aw-create-app", "path": "skills/aw-create-app/SKILL.md",
      "description": "How to author an aw-workspace app." }
  ]
}
```

Convention for any app that wants to teach an agent how to use it: drop a
`skills/<id>/SKILL.md` (YAML frontmatter with `name` + `description`) and list
it under `contributes.skills`. On install, aw-workspace's runtime symlinks the
skill's own directory into a shared workspace skills index (no content
duplication) and lists it at `GET /api/apps/-/skills`; uninstall removes the
symlink. See `repos/aw-workspace/src/apps/skills.py`.
