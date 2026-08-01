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

2. **Documents the runner-CLI source of truth** `agents-platform_multitenant`'s
   `agent-images/*/Dockerfile` (claude/codex/copilot/cursor) pull their
   install scripts from — `runner_source_repo`/`runner_source_ref` (config,
   default `tekflox/aw-app-code-agent-clis@v0.2.1`) — the SAME scripts this
   workspace's own `code-agent-clis` app installs, so there's exactly one
   place that knows how to install each CLI. `GET /api/apps/
   agents-platform-runners/status` reports the current config back.

## Config

| Key | Default | What |
|---|---|---|
| `agents_platform_base` | `http://127.0.0.1:10014` | Base URL of the agents-platform_multitenant instance the MCP tools control |
| `runner_source_repo` | `tekflox/aw-app-code-agent-clis` | Repo the runner install scripts live in |
| `runner_source_ref` | `v0.2.1` | Git ref (tag/branch) of that repo to use |

## Dependencies

Depends on `mcp-gateway` (required) — it contributes `mcp.json` definitions
the gateway discovers and merges; without it installed first, this app's
MCP tools never surface anywhere.

## Local dev

```bash
.venv/aw/bin/python -m pytest tests/
.venv/aw/bin/python tests/validate_manifest.py
python -m agents_platform_runners_app   # standalone, binds 127.0.0.1:9407
```
