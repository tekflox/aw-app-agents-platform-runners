---
repo: architecture
path: docs/architecture/aw-app-agents-platform-runners.md
source: generated
edited: false
checksum: sha256:4173bb992dd560b4915b9a3e72c8e9ab2cda9c0a3840c4c7c48d957d5b908545
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

## MCP tools
_none exposed_

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
