"""Rewrite ``[[ATTACH: <local path>]]`` markers into a reference the Telegram
connector can actually resolve.

Why this exists (2026-08-12)
----------------------------
An agent writes ``[[ATTACH: /opt/aw-workspace/.tmp/chart.png]]`` and the
attachment silently never arrives. agents-platform_multitenant's
``api/telegram.py::_deliver_reply`` does::

    if not os.path.exists(path):
        log.warning("ATTACH path not found: %s", path)
        continue

— an ``os.path.exists()`` against **its own** filesystem. AP runs under
docker on the outer bare-metal host; the agent's CLI runs in a podman
container nested inside ``aw-remote-host``. They share no mount, so the path
never resolves and the block is dropped with nothing but a log line. The user
sees neither the file nor an error.

The inbound direction hit exactly this wall first and already solved it:
``api/gallery.py::save_inbound_upload`` (2026-08-08) stopped handing agents a
bare local path precisely because "a RunnerLLM agent's CLI runs inside a
DIFFERENT machine's podman entirely and can't read it at all". This module is
the same fix pointed the other way.

How
---
Rewriting happens on **this** side of the wall, while the bytes are still
readable: the file is uploaded to the run it belongs to via AP's existing
``POST /api/runs/{run_id}/artefacts`` route, and the marker is rewritten to::

    [[ATTACH: artefact://<run_id>/<name> caption="..."]]

The agent's contract is untouched — it keeps writing a plain absolute path,
exactly as ``skills/aw-agent-telegram/SKILL.md`` documents. Nothing about the
marker syntax, the skill, or any agent prompt changes.

Why artefacts and not a public URL: the artefact route is authenticated with
a credential this side already holds (``AGENTS_PLATFORM_TOKEN`` in the
runners app's ``mcp.json``, the same one every agent MCP call uses) and was
verified working from an agent container on 2026-08-12. Serving the file over
HTTP instead would mean either relaxing this app's ``IdentityGuard`` for a
whole new public route, or minting a 24h gallery share-token — both add
externally-reachable surface for what is a one-file handoff.

Called from both output paths, because they publish from different places:

* warm containers (default since 0.32.0) — ``aw-warm-relay.py``, inside the
  agent container itself;
* the ephemeral/cold path — ``execute.py::_publish_line``, in the workspace
  container.

Both see the same ``/opt/aw-workspace`` tree, so either can read the file.

Requires the matching AP-side change: ``_deliver_reply`` has to understand the
``artefact://`` scheme. Until that ships this rewrite is inert but harmless —
a nonexistent ``artefact://…`` path is dropped by the very same
``os.path.exists()`` branch that was already dropping the local one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.request

# Must stay byte-for-byte identical to the regex AP parses with
# (`api/telegram.py::_ATTACH_RE`), so this rewrites exactly the set of markers
# AP would have tried to deliver — no more (mangling prose that only looks
# like a marker), no less (leaving a marker AP will act on and fail).
# Both were fixed together on 2026-08-12: the old shape let the path group
# swallow a single-space-separated ` caption="..."`, dropping the caption.
_ATTACH_RE = re.compile(
    r"\[\[ATTACH:\s*(?P<path>[^\]]+?)"
    r'(?:\s+caption="(?P<caption>[^"]*)")?\s*\]\]',
    re.IGNORECASE,
)

# Telegram's Bot API caps uploads at 20 MB. Artefact content travels as
# base64 (~1.34x), so refuse well before that rather than let a big file
# bloat the runs table and then get rejected by Telegram anyway.
MAX_ATTACH_BYTES = 15 * 1024 * 1024

WORKSPACE_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
_MCP_JSON = os.path.join(WORKSPACE_DIR, "apps", "agents-platform-runners", "mcp.json")

# path -> artefact name, so a file mentioned in both an assistant message and
# the final result event is uploaded once per process, not once per mention.
_uploaded: dict[tuple[str, str], str] = {}


def _credentials() -> tuple[str, str]:
    """(base_url, bearer token) for AP's API. Env first — the runners app's
    own process has these — then the runners app's ``mcp.json``, which is the
    only place a spawned agent container can read them from."""
    base = os.environ.get("AGENTS_BASE") or ""
    token = os.environ.get("AGENTS_PLATFORM_TOKEN") or ""
    if base and token:
        return base.rstrip("/"), token
    try:
        with open(_MCP_JSON, "r", encoding="utf-8") as f:
            env = json.load(f)["mcpServers"]["agents-platform-runners"]["env"]
        base = base or env.get("AGENTS_BASE", "")
        token = token or env.get("AGENTS_PLATFORM_TOKEN", "")
    except Exception:
        return "", ""
    return base.rstrip("/"), token


def _artefact_name(path: str, data: bytes) -> str:
    """Stable, collision-free artefact name. Artefacts are keyed by (run, name)
    and replace on conflict, so two different files sharing a basename inside
    one run would clobber each other — the content hash prevents that while
    keeping the extension, which is what AP's send_photo-vs-send_document
    branch keys off."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path)) or "attachment"
    return f"tgattach-{hashlib.sha256(data).hexdigest()[:8]}-{stem}"


def _upload(run_id: str, path: str, data: bytes) -> str:
    """Store the bytes on the run. Returns the artefact name, or "" on any
    failure — callers leave the marker untouched in that case."""
    base, token = _credentials()
    if not base or not token:
        return ""
    name = _artefact_name(path, data)
    body = json.dumps({
        "name": name,
        "mime": _mime_for(path),
        "content": base64.b64encode(data).decode("ascii"),
        "is_binary": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/runs/{run_id}/artefacts", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            return ""
    return name


def _mime_for(path: str) -> str:
    import mimetypes
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def rewrite_text(text: str, run_id: str) -> str:
    """Return ``text`` with every resolvable local ``[[ATTACH]]`` path swapped
    for its ``artefact://`` reference.

    Never raises: a failed upload leaves the marker exactly as the agent wrote
    it, which is the pre-existing (silent-drop) behaviour rather than a new
    failure mode. Markers whose path is already a URL/scheme, or that point at
    a file this side can't see either, are left alone — the first is somebody
    else's rewrite, the second is an agent bug worth surfacing as-is.
    """
    if not text or "[[ATTACH" not in text.upper() or not run_id:
        return text

    def _sub(m: "re.Match[str]") -> str:
        whole = m.group(0)
        path = re.sub(r"\s+caption=.*$", "", m.group("path").strip()).strip()
        if "://" in path or not os.path.isabs(path):
            return whole
        try:
            if not os.path.isfile(path):
                return whole
            size = os.path.getsize(path)
            if size == 0 or size > MAX_ATTACH_BYTES:
                return whole
            key = (run_id, path)
            name = _uploaded.get(key)
            if name is None:
                with open(path, "rb") as f:
                    data = f.read()
                name = _upload(run_id, path, data)
                _uploaded[key] = name
            if not name:
                return whole
        except Exception:
            return whole
        caption = m.group("caption")
        tail = f' caption="{caption}"' if caption else ""
        return f"[[ATTACH: artefact://{run_id}/{name}{tail}]]"

    return _ATTACH_RE.sub(_sub, text)


def rewrite_stream_line(line: str, run_id: str) -> str:
    """Same rewrite, applied to one ``claude --output-format stream-json``
    line as it goes onto the Redis stream.

    Only the two event shapes whose text can reach the user are touched — the
    terminal ``result`` event (what AP delivers as ``reply``) and assistant
    text blocks (AP's fallback when ``result`` is absent). Everything else,
    including any line that isn't valid JSON, is passed through byte-identical
    so this can never corrupt the stream the consumer is parsing.
    """
    if not line or "[[ATTACH" not in line.upper():
        return line
    try:
        evt = json.loads(line)
    except Exception:
        return line
    if not isinstance(evt, dict):
        return line

    changed = False
    if evt.get("type") == "result" and isinstance(evt.get("result"), str):
        new = rewrite_text(evt["result"], run_id)
        if new != evt["result"]:
            evt["result"] = new
            changed = True
    elif evt.get("type") == "assistant":
        content = (evt.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    new = rewrite_text(block["text"], run_id)
                    if new != block["text"]:
                        block["text"] = new
                        changed = True

    if not changed:
        return line
    try:
        return json.dumps(evt)
    except Exception:
        return line
