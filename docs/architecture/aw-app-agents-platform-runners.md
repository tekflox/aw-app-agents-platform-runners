---
repo: architecture
path: docs/architecture/aw-app-agents-platform-runners.md
source: generated
edited: false
checksum: sha256:1505139dbd74efdcd5a369365857746aca6697459a1e48047b2df1eb43ee395f
---
# Agents Platform Runners

- **repo**: aw-app-agents-platform-runners
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Depends on the code-agent-clis app (claude/codex/copilot/cursor-agent already installed at /usr/local/bin, single source of truth) so this workspace's agent-CLI runners are what agents-platform_multitenant's agent sessions use, and contributes the ported "aw-agents" MCP (agent_mcp.py) so agents-platform is controllable as MCP tools from this workspace.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/agents-platform-runners
- `other` → **aw-app-code-agent-clis** — This app doesn't install the CLIs itself — it depends on code-agent-clis having already put claude/codex/copilot/cursor-agent on /usr/local/bin, same path aw-workspace installs already reuse: one app owns installing each runner, this app just depends on that instead of re-implementing it

## MCP tools
_none exposed_

## Requirements
_none documented_
