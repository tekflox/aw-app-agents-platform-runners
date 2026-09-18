# `aw-workspace-cli agents-platform`

Kanban: `3df5bf3b-9510-81b4-97e1-ed6fdeb9c820` · Target: `agents-platform-cli`

## The approach

`aw-app-agents-platform-runners` contributes one root command,
`agents-platform`, via a **three-line shim** at `commands/agents_platform.py`
— exactly the shape `aw-app-remote-host-cli` already uses
(`commands/remote_hosts.py`). All real code lives in
`agents_platform_runners_app/cli/`.

The command tree is **two levels**: `agents-platform <group> <verb>`.
Groups are **auto-discovered** from `agents_platform_runners_app/cli/
groups/*.py` with `pkgutil.iter_modules` — the same mechanism aw-workspace's
own `src/cli/discovery.py` uses for top-level commands. Adding a future
group (`targets`, `runs`, `workflows`, `sessions`, `wakeups`, `lessons`) is
one new file and zero edits anywhere else.

Every group talks to **agents-platform-multitenant's REST API directly**,
through one shared `PlatformClient`, with the Bearer token and base URL
resolved from this app's own on-disk config — the same credential and
address the MCP server already uses.

## Layout

```
commands/agents_platform.py                     # shim: COMMAND/DESCRIPTION/run
agents_platform_runners_app/cli/__init__.py
agents_platform_runners_app/cli/__main__.py      # python -m …cli  (dev/tests)
agents_platform_runners_app/cli/main.py          # root parser + group dispatch
agents_platform_runners_app/cli/client.py        # PlatformClient + errors
agents_platform_runners_app/cli/output.py        # --json / table / ok / fail
agents_platform_runners_app/cli/groups/__init__.py    # pkgutil auto-discovery
agents_platform_runners_app/cli/groups/telegram_bot.py
agents_platform_runners_app/cli/groups/agents.py
tests/test_cli_discovery.py
tests/test_cli_client.py
tests/test_cli_telegram_bot.py
tests/test_cli_agents.py
```

`plugin.py`, `mcp_server.py`, `routes.py`, `identity_token.py`,
`platform_base.py` and `aw-app.json`'s `contributes` block do **not**
change. There is no manifest entry for a contributed command — discovery is
purely filesystem (`<apps_root>/<slug>/commands/*.py`), and app installs
copy the whole repo tree.

## The shim contract

`src/cli/discovery.py` requires a module-level `COMMAND` (str), `DESCRIPTION`
(str), and `run(args: list[str]) -> int`. `commands/agents_platform.py`
inserts its own package dir onto `sys.path` (Tier-1 apps load under a
synthetic `aw_apps.<id>` namespace inside the *workspace* process, so
`agents_platform_runners_app` isn't importable as a top-level package from
the separate `aw-workspace-cli` process without this), then imports and
calls `agents_platform_runners_app.cli.main:main`. A broken import is caught
and reported with an actionable message rather than propagating — same
pattern as `remote_hosts.py`.

**Discovery swallows a broken command module** (`src/cli/discovery.py`
prints a warning and drops it): a syntax error or a bad import makes the
command silently not exist rather than fail loudly. Verify a change actually
shows up in `aw-workspace-cli help`/`--help`, not just that the file parses.

## Hard constraint: never import `plugin.py`

`agents_platform_runners_app/plugin.py` imports `execute`, `warm_pool`,
`routes`, `shared_redis` — docker, redis, fastapi, ~10k lines. Importing it
from a CLI invocation is a startup-cost and dependency landmine. `identity_
token.py` is off-limits too: its `refresh()` does `from .plugin import
_workspace_env`.

**Only `platform_base` is leaf-safe** and may be imported from `cli/`.
`tests/test_cli_discovery.py::test_cli_main_import_never_touches_plugin`
asserts `"agents_platform_runners_app.plugin" not in sys.modules` after
importing `cli.main`, in a **fresh subprocess** (in-process `sys.modules` is
already polluted by the time `conftest.py` has imported `execute`, which
imports `plugin`) — this is the one thing that catches a regression here;
nothing else does.

## `cli/client.py` — the shared HTTP client

One class, reused by every group — nothing group-specific in it.

**Base URL resolution goes through `platform_base.resolve(config)`, never
`config["agents_platform_base"]` directly.** The persisted value on a host
that has ever saved its config is the legacy literal
`http://172.18.0.1:10014` (a bridge address only reachable when
agents-platform-multitenant happens to run on the same physical host or
container as the workspace) — `resolve()` deliberately treats that literal,
and its `127.0.0.1`/`localhost` siblings, as unset and derives
`https://agents-platform.<apex of AW_BACKEND_URL>` instead. A naive config
read "works" on a host that happens to co-locate both services and is dead
on any real BYOD host. `AW_AGENTS_PLATFORM_BASE` overrides for free, since
`resolve()` already checks it.

**Token**: `config["agents_platform_token"]`, overridable by
`AW_AGENTS_PLATFORM_TOKEN`. Config is read from
`<workspace_home>/app-config/agents-platform-runners.json` (0600 JSON) —
`workspace_home` resolves as `AW_WORKSPACE_HOME`, else
`<AW_WORKSPACE_CONTAINER_DIR or /opt/aw-workspace>/.aw-workspace`, the same
fallback `platform_base.workspace_env()` uses. A missing/unreadable config
file or empty token raises `NotConfigured`, naming the file path.

**Errors**: any non-2xx raises `PlatformError(status, body)`, `body` being
the server's `detail`/`error` field (or raw text) truncated to ~300 chars.
`main.py` gives a 401 a special, actionable message, since it has exactly
one likely cause — a stale on-disk token — and names the restart command
that fixes it.

## `cli/output.py` — the output convention

One place, so no group invents its own:

- `emit_json(obj)` — `json.dumps(obj, indent=2, ensure_ascii=False)`.
- `emit_table(rows, headers)` — left-justified, width-padded columns;
  prints `(none)` when empty.
- `ok(msg)` / `fail(msg)` — one-line confirmations; `fail` writes to stderr.

Rules every group follows:

- `--json` is a **global** flag on the root parser, available to every verb
  — defined once, never re-declared per group.
- List verbs print a table by default, raw JSON with `--json`.
- Mutating verbs print a one-line confirmation by default, the raw response
  envelope with `--json`.
- Exit codes: `0` ok · `1` API/HTTP error (`PlatformError`) · `2`
  `NotConfigured` · argparse's own `2` for bad arguments.

## `cli/groups/__init__.py` — the growth seam

```python
def discover_groups() -> list[ModuleType]:
    """Every module in this package exposing GROUP/DESCRIPTION/register."""
```

`pkgutil.iter_modules(__path__)`, skip `_`-prefixed names, import via
`importlib.import_module`, keep modules exposing all three attributes.
Sorted by `GROUP` so `--help` is stable.

**Group module contract** — the thing a future contributor reads before
adding a group:

```python
GROUP = "telegram-bot"
DESCRIPTION = "Manage the Telegram bots wired to Agents Platform agents"

def register(sub):                       # sub: the group's own add_subparsers()
    p = sub.add_parser("list", help="…")
    p.set_defaults(func=_list)

def _list(client: PlatformClient, ns: argparse.Namespace) -> int: ...
```

`main.py` creates one subparser per group, calls `register()` on its
`add_subparsers(dest="verb", required=True)`, then dispatches
`ns.func(client, ns)` inside a `try/except NotConfigured/PlatformError` that
maps to the exit codes above. **`main.py` contains no knowledge of any
specific group.** The `PlatformClient` is constructed after parsing, so
`--help` works with no config present.

Keep group modules import-cheap — they're all imported on every invocation,
including `--help`.

## The initial surface

### `agents-platform telegram-bot` → `/api/telegram/bots`

| Command | Call |
|---|---|
| `list [--json] [--show-secrets]` | `GET /api/telegram/bots` |
| `add <id> --token T [flags]` | `POST /api/telegram/bots` (201) |
| `delete <id> [--yes]` | `DELETE /api/telegram/bots/{id}` (204) |

`list` **redacts `token`/`webhook_secret` by default** — this is the exact
field pair that was a live credential leak on 2026-08-13 (see the comment at
`backend/app/api/telegram.py` above `_admin_gate`). Table columns:
`ID · NAME · AGENT · ENABLED · SYSADMIN · TOKEN`, TOKEN shown as
`8450…nO0I` (first 4 / last 4 chars). `--show-secrets` reveals them in full.
**`--json` without `--show-secrets` also redacts** — a redaction that only
covers the pretty path is not a redaction.

`add` auto-generates `--webhook-secret` (`secrets.token_hex(32)`) when
omitted, and **registers the bot's webhook by default**
(`POST /bots/{id}/register-webhook`), with `--no-register-webhook` to opt
out — a bot created without a webhook receives nothing from Telegram. If
create succeeds but the webhook call fails, the bot is reported as created,
the webhook failure is reported too, and the command **exits 1** — that
state is real but incomplete and must not read as success. `--sysadmin` is
documented as a radio, not a checkbox: the server demotes every other
sysadmin-flagged bot.

`delete` prompts on a TTY, `--yes` skips it, and a non-TTY stdin with no
`--yes` is a hard error — never auto-confirm in a script.

### `agents-platform agents` → `/api/agents`

| Command | Call |
|---|---|
| `list [--json] [--deleted]` | `GET /api/agents` (`?deleted_only=true`) |
| `add --name N [flags]` | `POST /api/agents` |
| `delete <slug> [--hard] [--yes]` | `DELETE /api/agents/{slug}?hard=` |
| `run <slug> --input TEXT [flags]` | `POST /api/agents/{slug}/run` |

`list` table: `SLUG · NAME · MODEL · GROUP · DESCRIPTION` (description
truncated) — the payload is large, the table projects rather than dumps it.

`add` does **not** expose one flag per `AgentIn` field (19 fields and
growing). Flags cover what a human actually types by hand (`--name`,
`--slug`, `--description`, `--system-prompt`/`--system-prompt-file`,
`--model`, `--group`, `--agent-config`, `--skill` (repeatable), `--icon`,
`--color`); `--from-json <file|->` takes a full/partial `AgentIn` object,
**merged first, with explicit flags overriding it** — the escape hatch that
keeps every remaining/future field reachable with zero CLI changes.

`delete` is soft by default (restorable — the output says so explicitly),
`--hard` for permanent; same confirmation rule as `telegram-bot delete`.

`run`'s request body is **nested**: `{"input": {"input": TEXT}}` — not
`{"input": TEXT}` — because `run_agent_ep` reads
`body.input.get("input")`. `--target` is optional, unlike the MCP tool's
`run_agent_async`: the endpoint falls back to an auto-provisioned `ad-hoc`
Target when none is given. **`call_me_back` is hardcoded `false`** and has
no flag — a CLI invocation has no Agents Platform session to wake, and the
endpoint 400s a `call_me_back=true` with no resolvable `caller_run_id`.

`--wait [--timeout SECONDS]` (default 900s) polls `GET /api/runs/{id}`
every 3s until `status` is terminal. **The real terminal set on `Run.status`
is `success`/`error`/`cancelled`** (see `models.py`: `"pending|queued|
running|success|error|cancelled"`) — not the more guessable
`succeeded`/`failed`. Exit `0` on `success`, `1` on `error`/`cancelled`,
`124` on timeout (the `timeout(1)` convention `remote_host_cli_app/cli.py`
already uses for "never finished"), printing a resume hint. Without
`--wait`, prints `run_id` and exits 0.

## What was rejected, and why

**Routing through the app's own `/api/apps/agents-platform-runners/*`
routes** (the `aw-app-architecture` CLI's pattern) — that app needs the
workspace API because its work needs `ctx.db`, a session that only exists in
the workspace process. Nothing here needs that: the credential is on disk,
the work is a direct HTTP call to agents-platform-multitenant. Routing
through the workspace API would mean a new proxy route per verb, forever.

**Sharing a `dispatch()` with `mcp_server.py`**, the way remote-host-cli
shares one with its own MCP server. Right there, wrong here:
remote-host-cli's client centralizes real work (sha256 streaming, chunked
transfer); AP-MT's verbs here are one `httpx` call each, so the "shared
logic" would be a single line per verb, and `mcp_server.py` (2939 lines of
async hand-rolled JSON-RPC keyed off env vars a different process bakes into
`mcp.json`) is not something worth coupling a synchronous CLI to just to
save four URL strings.

**A hand-maintained `GROUPS = [...]` registry** instead of pkgutil
auto-discovery. The stated requirement is growth to a dozen groups written
by different agents over months — a list someone has to remember to edit is
exactly the friction this card exists to remove, and `src/cli/discovery.py`
already establishes auto-discovery as this codebase's answer.

**One flag per `AgentIn` field.** 19 fields today, more later; every AP-MT
schema addition would otherwise become a CLI card.

**The CLI minting/refreshing its own token** via `identity_token.refresh()`.
Would need `identity_token` made leaf-safe first (it imports `plugin` at
module scope) and adds a second writer to a token the workspace process
currently owns alone. The 401 message points at the one-command fix
(`aw-workspace-cli restart agents-platform-runners`) instead. Revisit if
real 401s from a stale on-disk token show up in practice.

**Three-level nesting** (`agents-platform agents sessions list`). Two levels
covers today's REST surface; a group that genuinely needs depth can add it
inside its own `register()` without the framework changing.

## What this makes harder later

- **Groups share one flat, global namespace.** `--help` gets long as more
  groups land, and two groups can't share a name.
- **The 0600 config file is the credential path.** Any caller whose uid
  can't read `<workspace_home>/app-config/agents-platform-runners.json`
  gets `NotConfigured` — including possibly an agent-runner container with
  a different uid. Same posture as `remote-host-cli`'s `.env`, but "works in
  my terminal, `NotConfigured` in an agent" is a real, confusing outcome
  worth remembering when a group's `NotConfigured` message needs to say
  more than the file path.
- **No pagination.** `list` fetches whatever AP-MT returns — fine today; the
  first group over a few hundred rows needs `--limit`, and the convention
  will need retrofitting into `output.py`.
- **Redaction is per-group, not framework-enforced.** `telegram-bot` redacts
  because its payload carries secrets. A future group returning a
  credential and forgetting to redact will leak by default. If a second
  such group appears, move redaction into `output.py` as a declared field
  list instead of leaving it to each group's own discipline.
