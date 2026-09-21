---
repo: architecture
path: docs/architecture/aw-app-agents-platform-runners.md
source: generated
edited: true
checksum: sha256:5e0f72a6563d70d33424b1e2a4b74eeff730b16d432cf691c8ac6dd0122eb03d
---
# Agents Platform Runners

- **repo**: aw-app-agents-platform-runners
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Depends on the code-agent-clis app (claude/codex/copilot/cursor-agent already installed at /usr/local/bin, single source of truth) so this workspace's agent-CLI runners are what agents-platform-multitenant's agent sessions use, and contributes the ported "aw-agents" MCP (agent_mcp.py) so agents-platform is controllable as MCP tools from this workspace.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/agents-platform-runners
- `other` → **aw-app-code-agent-clis** — This app doesn't install the CLIs itself — it depends on code-agent-clis having already put claude/codex/copilot/cursor-agent on /usr/local/bin, same path aw-workspace installs already reuse: one app owns installing each runner, this app just depends on that instead of re-implementing it
- `other` → **aw-app-kb** — Optionally stores a bounded semantic index of completed execution dumps; runner delivery remains fail-open when KB is absent
- `http` → **agents-platform-multitenant** — every MCP tool and the three proxy routes below call AP-MT with this app's own `agents_platform_token` (minted and rotated by `identity_token.py`); it is the only AP-MT credential in this workspace

## MCP tools

`mcp_server.py` registers ~94 static tools plus one `agent_<slug>` /
`workflow_<slug>` per active platform resource. The control-plane surface
(agents, workflows, runs, targets, lessons, artefacts) is documented by the
tool descriptions themselves; what follows is the one group that is **not**
orchestration, and so is easy to lose track of.

### Gallery (ported from `aw-app-crispal`, 2026-09-21)

Reads the Agents Platform image gallery — the Telegram `/images` mini-app's
uploads, plus images agents filed back — over AP-MT's identity-gated admin
endpoints. Logic in `agents_platform_runners_app/gallery.py`, registration in
`mcp_server.py`'s `static` list under `# ----- gallery -----`.

| Tool | Does | AP-MT endpoint |
|---|---|---|
| `list_gallery_images` | Resolve upload blocks to image ids + fetchable URLs, filtered by `scope`/`block_id`/`tags`/`match`/`source` | `GET /api/admin/gallery/blocks` |
| `list_gallery_tags` | The bot's tag vocabulary with per-tag image counts, derived from the same listing | `GET /api/admin/gallery/blocks` |
| `set_gallery_tags` | Add tags to images by `image_ids` (additive, idempotent) | `POST /api/admin/gallery/token` once, then `POST /api/gallery/{token}/image/{id}/tag` |

Two things about this group that are load-bearing:

* **No file paths, only URLs.** The tools these replaced downloaded each image
  and returned `file_paths` into the Crispal container's disk. This MCP runs
  as a stdio child inside the **aw-mcp-gateway** container, which shares no
  writable directory with the Crispal container or with an agent container —
  a path from here would name a file nobody else can open. `images[].url` /
  `image_urls` are per-image capability URLs, fetchable with no Authorization
  header of their own.
* **`set_gallery_tags` mints, it does not write directly.** AP-MT has no
  tag-write endpoint behind `require_tenant_or_service`; the write path is
  keyed on a `GalleryToken`. `POST /api/admin/gallery/token` exists precisely
  to let a trusted workspace exchange its identity for one, and returns the
  existing token while it has >7 days left, so minting is cheap and
  idempotent. The token is cached per `bot_slug` for the life of the process
  and re-minted on a 401.

`bot_slug` is optional on all three tools, and omitting it means **every
gallery this workspace owns**, not a hardcoded bot. The tools this ports from
defaulted to the literal `aw-cris`; checked live on 2026-09-21, that bot does
not exist on this deployment — all 345 blocks are under `cp-2` — so a
no-argument call would have answered 200 with an empty list, which reads
exactly like "you have no photos". An empty slug is not unscoped: the listing
endpoint binds the caller's tenant, so "no bot filter" already means "this
workspace's own galleries", and each returned block carries its own
`bot_slug`. `set_gallery_tags` resolves each image's owning bot from the same
listing when none is named, because a `GalleryToken` is bot-scoped.

## Tier-1 routes

Mounted at `/api/apps/agents-platform-runners` (see `routes.py`). Beyond
`/status`, `/warm-containers`, `/register`, `/register-observability`,
`/notion-token*`, `/execute` and `/abort`, three of them exist purely so
another app can reach AP-MT **without holding an AP-MT credential of its
own** — aw-app-crispal is the caller, over the workspace API key it already
has. A missing `agents_platform_token` answers 503 (this side has nothing to
present onward), an unreachable AP-MT 502, and AP-MT's own status code is
passed through otherwise — never swallowed, because the caller of
`/gallery/upload` is a queue worker that has to tell a refused upload from a
successful one.

| Route | Proxies to | Replaces, in aw-app-crispal |
|---|---|---|
| `POST /gallery/upload` (multipart: `bot_slug`, `source`, `files`) | `POST /api/admin/gallery/upload` | `gallery_http.file_images()` — the Arvin archive |
| `GET /runs/{run_id}/initiator` | `GET /api/telegram/run-initiator/{run_id}` | `gallery_http.lookup_run_initiator()` — which chat to wake |
| `POST /telegram/inject` (body passes through) | `POST /api/telegram/inject` | `_ap_inject_secret()` + its call site — the Arvin wake-up |

The third also retires the recurring `AGENTS_TELEGRAM_INJECT_SECRET not found
at /app/repos/agents-platform/.env` failure: `/inject` accepts the same
`require_tenant_or_service` identity this app already holds, so no shared
inject secret has to exist anywhere.

## Requirements
### O primeiro turno de uma conversa também roda quente, com sessão criada e não retomada
- Given warm está ligado, o CLI é claude, o job traz agent_id e o chamador ainda não tem session_id (turno 1)
- When o dispatch cunha um uuid próprio antes de escolher o caminho quente/frio (agents_platform_runners_app/execute.py::mint_warm_session_id:1184) e o argv é montado a partir do marcador _warm_minted_session (agents_platform_runners_app/execute.py::_build_warm_kwargs_claude:1000, escolha em :1053)
- Then o argv leva --session-id (criar) e nunca --resume, e um job que já traz session_id do chamador jamais é re-cunhado — se os dois se invertessem o container subiria e o claude morreria com "no conversation found" numa sessão que não existe, e sem a cunhagem 39% dos dispatches medidos em 14/08 pagariam spawn completo de container por serem sempre turno 1
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-agents-platform-runners/tests/test_warm_first_turn.py` (passing)

### O argv do modo quente carrega as mesmas flags de permissão que o frio
- Given um job com dangerous_skip_permissions, allowed_tools, disallowed_tools ou append_system_prompt roda num container quente
- When o claude_argv do container quente é montado do zero em vez de herdar o do caminho frio (agents_platform_runners_app/execute.py::_build_warm_kwargs_claude:1000)
- Then todas essas flags aparecem no argv quente com o mesmo valor do frio, inclusive a ausência do bypass quando o job pede modo seguro — o turno 1 é sempre frio, então uma flag que some aqui só se manifesta a partir do turno 2, e foi assim que --dangerously-skip-permissions sumiu e o gate interativo do Claude Code ("This command requires approval") travou um runner supostamente desassistido em 11/08
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-agents-platform-runners/tests/test_warm_kwargs_argv_parity.py` (passing)

### O relay só finaliza a run do turno que foi despachado, e só uma vez
- Given um container quente de vida longa serve vários turnos e o claude também emite results por conta própria (task-notification, wakeup), que chegam com o campo origin preenchido
- When o relay lê um evento type=result no stdout e decide publicar o sentinela {done:1} (agent-images/shared/aw-warm-relay.py, teste de origin em :135 e memória de finalizadas em :102)
- Then um result com qualquer origin é relayado como stdout mas não finaliza nada, e um run_id já finalizado não recebe segundo sentinela — sem isso o relay finaliza a run que estiver corrente no momento, que pode ser a PRÓXIMA run e não a dele: foi o que aconteceu na run 15032895, dois sentinelas com 6 minutos de diferença, o segundo matando trabalho vivo de outro despacho
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-agents-platform-runners/tests/test_warm_relay_done_sentinel.py` (passing)

### Um [[ATTACH]] com caminho local vira artefact:// antes de cruzar a fronteira de host
- Given o agente escreve [[ATTACH: /caminho/local]] dentro do container do runner, e o conector do Telegram roda noutro host que não enxerga esse filesystem
- When a linha do stream passa pelo reescritor antes do XADD (agent-images/shared/aw_attach.py::rewrite_text:153, chamado por ::rewrite_stream_line:277 a partir do relay)
- Then o marcador sai como artefact://run_id/nome, com caption e extensão preservadas e um upload por arquivo por run; e todo caso que não dá para resolver — caminho relativo, arquivo inexistente, tamanho zero, acima do limite, já reescrito, ou upload que falhou — deixa o marcador exatamente como o agente escreveu, que é o comportamento antigo de descarte silencioso e não um modo de falha novo
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-agents-platform-runners/tests/test_attach_rewrite.py` (passing)

### Conteúdo de agente semeado é semeado uma vez, credencial de gateway é re-afirmada toda ativação
- Given um agent_config semeado pelo app declarou mcp_servers por nome (por referência, sem credencial no manifesto) e já existe na plataforma
- When o app ativa de novo e o provisioner encontra o slug já criado (repos/aw-app-agents-platform-runners/agents_platform_runners_app/agent_provisioner.py::AgentProvisioner._refresh_credentials:174)
- Then só o campo mcp_config é reescrito com o token resolvido agora — prompt, modelo e demais edições do usuário sobrevivem, e um mcp_config escrito à mão no manifesto nunca é tocado; sem essa exceção ao seed-once o token congela no primeiro install e o agente fica com config perfeita na UI e zero tools, porque o gateway 401a e o cliente registra nada sem ninguém reportar
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-agents-platform-runners/tests/test_agent_provisioner.py` (passing)
