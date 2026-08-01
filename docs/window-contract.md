# App Window Contract

`contributes.windows[]` declares what kind of application surface an app
needs. The app does not own the shared window chrome. `aw-frontend` owns the
frame, title bar, z-order, resize/maximize/close behavior, and runtime
controls. The app only declares intent.

## Managed App Windows

Use `body.type: "managed_app"` when the app has a standard app surface such as
a browser, tool, dashboard, or web UI. The frontend turns this into the shared
window component.

```jsonc
{
  "contributes": {
    "windows": [
      {
        "id": "browser.main",
        "title": "Browser",
        "icon": "globe",
        "body": {
          "type": "managed_app",
          "kind": "browser",
          "path": "/vnc.html?autoconnect=true&resize=remote"
        }
      }
    ]
  }
}
```

Rules:

- `id` must be namespaced under the app id: `<app-id>.<name>`.
- `kind` is a frontend hint, not a backend route.
- `path` is optional and is resolved on the app's own subdomain.
- The app should not duplicate title-bar controls or window layout in its own
  spec for managed windows.

### Framework Settings for Managed Apps

Managed apps get workspace-owned lifecycle and access settings automatically.
The app declares `body.type: "managed_app"` or `tier: "container"`; it does not
declare or implement these toggles itself.

`aw-workspace` merges these properties into the effective config schema exposed
by `GET /api/apps` and `GET /api/apps/<app-id>/config`:

```jsonc
{
  "auto_start": {
    "type": "boolean",
    "default": true,
    "title": "Auto-start"
  },
  "auth_required": {
    "type": "boolean",
    "default": true,
    "title": "Authentication required"
  },
  "public": {
    "type": "boolean",
    "default": false,
    "title": "Public"
  }
}
```

- `auto_start`: when true, `aw-workspace` starts the managed process or
  container during workspace startup/reconcile. When false, the app remains
  installed and mounted, but the managed process is left stopped until the user
  starts it.
- `auth_required`: when true, `aw-workspace` requires a valid workspace identity
  token before proxying app routes. When false, the mounted app route may be
  opened without authentication.
- `public`: defaults to false and declares whether the app should be exposed through the public
  workspace routing layer. Apps should treat this as a framework routing
  contract, not app code. The workspace/edge routing layer is responsible for
  enforcing private versus public exposure.

`aw-frontend` renders these as settings toggles in the app config UI and saves
them through `POST /api/apps/<app-id>/config`. App-authored settings panels can
still call their own app routes, but they should not replace these framework
controls.

## Declarative Windows

Use `body.type: "declarative"` when the app needs a small settings/control
panel made from the widget vocabulary (`markdown`, `button`, `form`,
`auth_status`, etc.).

```jsonc
{
  "id": "git.main",
  "title": "Git",
  "body": { "type": "declarative", "spec": "windows/main.json" }
}
```

The runtime inlines `body.spec` as `body.spec_data` in
`GET /api/apps/-/contributions`; the frontend renders that data. New apps
should prefer `managed_app` for full app surfaces and keep declarative specs
for focused panels.
