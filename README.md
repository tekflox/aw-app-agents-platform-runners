# aw-app-agents-platform-runners

AW workspace app (`aw-app.json` manifest schema v1, `tier: inprocess`) with
two jobs:

1. **Contributes the ported "aw-agents" MCP.** `agents_platform_runners_app/
   mcp_server.py` is a straight copy of `agents-platform`'s
   `mcp_server/agent_mcp.py` — the same stdio MCP server this workspace's
   `.mcp.json` used to run directly as the `agents-platform` upstream.
   Installing this app writes an `mcp.json` (`contributes.mcp.reload_on_save`)
   that aw-mcp-gateway discovers and reloads automatically, pointed at
   `agents_platform_base` (config — default the `agents-platform_multitenant`
   instance, not the legacy single-tenant `agents-platform`).

2. **Reuses this workspace's own runner CLIs.** Depends on the
   `code-agent-clis` app (`dependencies.apps`, required) instead of
   installing claude/codex/copilot/cursor-agent a second way — one app owns
   installing each CLI (`/usr/local/bin`), this one just depends on that
   being done. `GET /api/apps/agents-platform-runners/status` checks each
   binary actually resolves on PATH and reports its version — a live signal
   the dependency did its job, not just that it's declared.
   `agents-platform_multitenant`'s own `agent-images/*/Dockerfile` were
   deliberately left untouched (Frederico decision 2026-08-01) rather than
   forced into the same nvm-based install path — that's a separate,
   independently-built image pipeline.

## Config

| Key | Default | What |
|---|---|---|
| `agents_platform_base` | `http://127.0.0.1:10014` | Base URL of the agents-platform_multitenant instance the MCP tools control |

## Dependencies

- `mcp-gateway` (required) — contributes `mcp.json` definitions the gateway
  discovers and merges; without it installed first, this app's MCP tools
  never surface anywhere.
- `code-agent-clis` (required) — installs the actual claude/codex/copilot/
  cursor-agent binaries this app's `/status` route checks for.

## Local dev

```bash
.venv/aw/bin/python -m pytest tests/
.venv/aw/bin/python tests/validate_manifest.py
python -m agents_platform_runners_app   # standalone, binds 127.0.0.1:9407
```
