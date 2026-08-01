// Integrated-mode entrypoint (ADR Decision 3/3b/4) — dynamic-imported by
// aw-frontend's loadComponentPlugin() (src/apps/loadPlugin.js) when this app
// is installed SIGNED with the "ui:code" capability granted (effectiveMode()
// downgrades any other install to iframe mode, and this file never runs).
// Built by `npm run build:plugin` -> ui/dist/template.js, referenced from
// aw-app.json's contributes.frontend.bundle.
//
// register(host) is the ONE required export. `host` is the APP-SCOPED handle
// from aw-frontend's hostForApp() (src/apps/pluginHost.js) — host.React /
// host.h / host.sdk are the shared instances (never import your own React;
// see vite.config.js), host.registerSlot / host.registerWindow /
// host.onDispose are how you contribute UI and clean it up on uninstall.
//
// TODO(framework, ADR Decision 3): `host.app.{apiUrl,fetch,wsUrl}` scoped
// helpers are proposed but not yet landed in aw-frontend's pluginHost.js
// (hostForApp only exposes host.sdk.api.fetch/wsUrl today) — this file
// hand-builds the `/api/apps/<slug>/...` prefix below and should switch to
// host.app.apiUrl/wsUrl the moment that lands (grep this repo's README when
// bumping the template).

import { createClient } from './client.js';

const SLUG = 'hello'; // TEMPLATE: must match aw-app.json's "id"

export function register(host) {
  const client = createClient({
    apiUrl: (sub) => `/api/apps/${SLUG}${sub}`,
    wsUrl: (sub) => host.sdk.api.wsUrl(`/api/apps/${SLUG}${sub}`),
    fetchImpl: (path, init) => host.sdk.api.fetch(path, init),
  });

  // --- Pattern A: a visible slot component --------------------------------
  // Renders a tiny status pill into core.nav (needs "ui:slots:core.nav" in
  // aw-app.json's permissions). Delete this block if your app is headless
  // (Pattern B only) — a plugin bundle is not required to fill any slot.
  function TemplateNavPill() {
    const [status, setStatus] = host.React.useState('…');
    host.React.useEffect(() => {
      client.hello()
        .then((r) => setStatus(r.message))
        .catch((e) => setStatus(`error: ${e.message}`));
    }, []);
    return host.h('span', { title: status }, 'template');
  }
  host.registerSlot('core.nav', TemplateNavPill);

  // --- Pattern B: headless background work --------------------------------
  // register(host) may start background work (open a WS, install listeners)
  // WITHOUT registering any slot, as long as every teardown is registered
  // via host.onDispose(fn) — this is the sanctioned headless pattern
  // (pluginHost.js onDispose), exactly what a client like aw-app-devctl's
  // needs. Shown here for reference; safe to delete if you have no headless
  // work to do.
  const echo = client.connectEcho({
    onMessage: (msg) => console.debug(`[${SLUG}] echo:`, msg),
  });
  host.onDispose(() => echo.close());
}

export default register;
