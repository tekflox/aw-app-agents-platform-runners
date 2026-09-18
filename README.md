# Agents Platform Runners

Agents Platform Runners connects an AW Workspace to hosted agent sessions. It lets the workspace start, supervise, and reuse coding-agent runners from the same place where the work is happening.

## What It Does

- Registers this workspace as a runner target for Agents Platform.
- Starts agent sessions that can work inside the workspace.
- Streams run output back to the platform so progress is visible.
- Adds agent-focused tools and skills for Telegram replies, supervision, coding work, documentation work, QA, and coordinated multi-agent runs.

## Why Use It

Use this app when an AW Workspace should be able to receive agent jobs from Agents Platform and run them against the workspace environment. It is useful for delegated coding tasks, documentation work, QA passes, and long-running workflows that need a reusable runner.

## How To Use It

Install the app in the workspace, open its settings, and connect it to the Agents Platform instance that should dispatch work here. Once configured, the platform can launch runs through this workspace and agents can use the contributed tools from their normal sessions.

### CLI

The app contributes `aw-workspace-cli agents-platform` — a terminal front-end
onto the same REST API the MCP tools use, for scripting and one-off calls
without going through an agent session.

```
aw-workspace-cli agents-platform telegram-bot list [--show-secrets]
aw-workspace-cli agents-platform telegram-bot add <id> --token T [--agent SLUG] [--sysadmin] ...
aw-workspace-cli agents-platform telegram-bot delete <id> [--yes]

aw-workspace-cli agents-platform agents list [--deleted]
aw-workspace-cli agents-platform agents add --name N [--model SLUG] [--from-json FILE] ...
aw-workspace-cli agents-platform agents delete <slug> [--hard] [--yes]
aw-workspace-cli agents-platform agents run <slug> --input "..." [--target SLUG] [--wait]
```

`--json` (a global flag, before or after the group) prints the raw response
instead of a table/confirmation. `telegram-bot list` redacts `token` and
`webhook_secret` by default in both the table and `--json` paths —
`--show-secrets` reveals them.

The command tree is two levels, `agents-platform <group> <verb>`, and groups
are auto-discovered from `agents_platform_runners_app/cli/groups/*.py` — a
new group is one file, no registry to edit. See
`docs/architecture/cli.md` for the full design, including the group module
contract a future contributor needs.

Credentials and address come from this app's own config (the same
`agents_platform_token`/`agents_platform_base` the MCP tools already use),
resolved the same way the app resolves them internally — nothing new to
configure.

### Identity token (`agents_platform_token`)

The credential this app sends as `Authorization: Bearer` on every call to `agents_platform_base` is obtained automatically — no manual minting. On activation, and again on a ~6h half-life schedule, the app calls aw-backend's `POST /api/workspaces/{slug}/identity-token` (using this workspace's own host credential) and persists the result through its own config-save path, so the refreshed token also lands in the `mcp.json` a stdio MCP child reads. A short-lived token plus that refresh loop is deliberate: the field stays writable in Settings as a manual override, but leaving it alone is the supported path. See `agents_platform_runners_app/identity_token.py` for the mint+persist design and refresh policy.

### OpenAI models

The app contributes the current OpenAI catalogue as `openai-*` models, and its settings panel holds the **OpenAI API key** they run on. Saving the panel pushes that key onto Agents Platform's own `Settings.openai_api_key` row, so the models and the credential they need are configured in one place instead of two. A blank field never clears the platform's value — clearing is done in the platform UI, deliberately.

These used to be four slugs hardcoded in Agents Platform's `seed.py`, which is why they were stale: a hardcoded seed can't track a catalogue that ships a new frontier model every few months, and since the seed re-runs on every boot, deleting an obsolete one only made it come back. Refreshing the list is now an app release, not a platform deploy.

Two rules matter when refreshing it, because breaking either produces a model that seeds fine and then fails on its first dispatch:

- **Chat-completions only.** Agents Platform's `openai` provider is LangChain `ChatOpenAI`. Every `-pro` variant and the `-codex` line are `v1/responses`-only or deprecated — listed by `/v1/models`, rejected on use.
- **No `temperature` on reasoning models.** GPT-5.x and the o-series accept `temperature=1` and nothing else; any other value is a 400 on every call. Only the gpt-4.x family takes one.

`tests/test_openai_catalog.py` enforces both.

### Kanban dispatch

Two tools bridge a Notion Kanban card to a run: `run_ready_cards` fires an agent for every card in `Ready`, and `invoke_kanban_agent` sends a message into the session of the agent already working a card.

They live here rather than in aw-app-notion on purpose. That app owns "a Notion database used as a Kanban board" and deliberately does not talk to an orchestrator — dispatch there would hardcode it to one. This app already *is* the orchestrator client, so the bridge costs it one new dependency (the board, over the workspace API) instead of the harder one. See `agents_platform_runners_app/kanban_dispatch.py`.

aw-app-notion is **not** a required dependency: without it these two tools return a clear "not installed" error and everything else works unchanged.

## What It Delivers

The app turns the workspace into an active execution target for agent work. Instead of treating the workspace as only a place to store code and data, it makes the workspace available as a controlled runner that can accept tasks, expose progress, and keep agent workflows close to the files and services they need.
