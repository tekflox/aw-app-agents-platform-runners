// Standalone-mode entrypoint (ADR Decision 4) — loaded by ui/index.html when
// this app runs as its own page (`python -m template_app`): no aw-frontend
// plugin host, no IdentityGuard. Same client core as plugin.js, but with
// same-origin relative URLs instead of the app-scoped host helpers.

import { createClient } from './client.js';

const SLUG = 'hello'; // TEMPLATE: must match aw-app.json's "id"

const client = createClient({
  apiUrl: (sub) => `/api/apps/${SLUG}${sub}`,
  wsUrl: (sub) => {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${location.host}/api/apps/${SLUG}${sub}`;
  },
});

async function main() {
  const output = document.getElementById('output');
  const status = document.getElementById('status');

  const { message } = await client.hello();
  output.textContent = message;

  const echo = client.connectEcho({
    onOpen: () => { status.textContent = 'ws: connected'; },
    onMessage: (msg) => { status.textContent = `ws: echo -> ${msg}`; },
    onClose: () => { status.textContent = 'ws: closed'; },
  });

  document.getElementById('ping').addEventListener('click', () => {
    echo.send(`ping ${Date.now()}`);
  });
}

main().catch((e) => {
  document.getElementById('status').textContent = `error: ${e.message}`;
});
