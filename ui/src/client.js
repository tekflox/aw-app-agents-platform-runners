// Framework-free client core (ADR Decision 4) — the actual app logic,
// completely unaware of whether it's running integrated (inside the AW SPA,
// behind IdentityGuard) or standalone (its own page, no auth). Both
// plugin.js and standalone.js build the same {apiUrl, wsUrl} shape and hand
// it here; nothing else differs between the two modes.
//
//   apiUrl:    (sub) => string        e.g. sub="/hello"   -> ".../api/apps/hello/hello"
//   wsUrl:     (sub) => string        e.g. sub="/ws/echo" -> "ws(s)://.../api/apps/hello/ws/echo"
//
// App-owned top-level WebSocket namespaces are reserved at /ws/apps/<slug>/...
// so apps never claim root /ws/*; root /ws/* stays AW core/control-plane only.
//   fetchImpl: (path, init) => Promise<Response>   defaults to plain fetch

export function createClient({ apiUrl, wsUrl, fetchImpl = fetch }) {
  async function hello() {
    const res = await fetchImpl(apiUrl('/hello'));
    if (!res.ok) throw new Error(`GET /hello -> ${res.status}`);
    return res.json();
  }

  function connectEcho({ onOpen, onMessage, onClose } = {}) {
    const ws = new WebSocket(wsUrl('/ws/echo'));
    if (onOpen) ws.addEventListener('open', onOpen);
    if (onMessage) ws.addEventListener('message', (ev) => onMessage(ev.data));
    if (onClose) ws.addEventListener('close', onClose);
    return {
      send: (text) => ws.send(text),
      close: () => ws.close(),
      raw: ws,
    };
  }

  return { hello, connectEcho };
}
