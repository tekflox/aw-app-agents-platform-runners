---
name: aw-agents-platform-rest-api
description: Calling the Agents Platform backend's raw REST API for anything this app's MCP tools don't cover — agent-groups, agent-flows, supervisions, runs, me, or a route still under development — plus the two fields that exist ONLY on raw REST (mcp_config, disable_harness_tools) and the end-to-end recipe for scoping an agent to exactly one MCP tool. Read this before re-deriving the auth dance or concluding a field doesn't exist.
---

# aw-agents-platform-rest-api — the surface the MCP tools don't expose

This app's MCP tools cover individual agents (`list_agents`, `get_agent`,
…), workflows and runs. They do **not** cover agent-groups or agent-flows,
and `create_agent`/`update_agent` omit several fields the backend accepts.
For those, call the backend's REST API directly.

One session reconstructed this exact curl-and-auth incantation **34 times**
because each shell invocation is a fresh process with no persisted
variables; a later one repeated it 15 more times while testing
`/api/supervisions`, `/api/runs` and `/api/me` under local development.
That is what this skill is for.

## Auth

The REST bearer lives in this app's own config, not a secret store:

```bash
CFG="$AW_WORKSPACE_HOME/app-config/agents-platform-runners.json"
T=$(jq -r .agents_platform_token "$CFG")
B=$(jq -r .agents_platform_base  "$CFG")
```

Read the base URL from the config rather than hardcoding one. It is **not**
`127.0.0.1` — that resolves to the agent's own container, not the backend —
and a port you remember from an older deployment may have been retired.

If a call unexpectedly 401s, rule out a stale token before assuming the
endpoint is wrong. A seeded config can carry a dead token:

```bash
python3 - "$T" <<'PY'
import base64, json, sys, time
p = sys.argv[1].split('.')[1]; p += '=' * (-len(p) % 4)
d = json.loads(base64.urlsafe_b64decode(p))
print(d, '\nexp in', d.get('exp', 0) - time.time(), 'seconds')
PY
```

## The endpoints the MCP surface doesn't cover

Group and flow-graph data — `kanban_target_status`, `hidden_from_flow`,
flow `graph.nodes`/`graph.edges`, group `slug`/`name`/`description` — is
only reachable here:

```bash
for p in agents agent-groups agent-flows; do
  curl -s -m 20 -H "Authorization: Bearer $T" "$B/api/$p" -o ".tmp/ap-rest/$p.json"
done

jq -r '.[] | "\(.slug) | \(.name) | kanban=\(.kanban_target_status//"-")"' .tmp/ap-rest/agent-groups.json
jq -r '.[] | "\(.slug) | enabled=\(.enabled) | max_hops=\(.max_hops//"-")"'  .tmp/ap-rest/agent-flows.json
jq -r '.[0].graph.nodes[]? | "\(.id) | type=\(.type//"-") | agent=\(.agent_slug//.agent//"-")"' .tmp/ap-rest/agent-flows.json
jq -r '.[0].graph.edges[]? | "\(.source) -> \(.target)"' .tmp/ap-rest/agent-flows.json
```

Same recipe for `/api/supervisions`, `/api/runs`, `/api/me`, or a route you
are actively adding and want to curl-test before it has an MCP wrapper.

## There is no `PATCH /api/agents/{slug}` — mutate with GET + PUT

Changing one field on an agent is not a `PATCH`. That verb returns **405
Method Not Allowed**. A 405 means *wrong verb*, not *wrong field* — don't
probe with `PATCH` and conclude the field or endpoint doesn't exist.

The only way to change one field is to read-modify-write the whole object:

```bash
for slug in agent-one agent-two; do
  curl -s "$B/api/agents/$slug" -H "Authorization: Bearer $T" \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
d['workspace'] = '<workspace-slug>'
print(json.dumps(d))
" > "/tmp/agent_$slug.json"
  curl -s -X PUT "$B/api/agents/$slug" -H "Authorization: Bearer $T" \
    -H "Content-Type: application/json" --data-binary "@/tmp/agent_$slug.json" \
    | jq '{slug, workspace, model_slug}'
done
```

`update_agent`'s MCP wrapper doesn't expose `workspace` either, so this has
to go through raw REST regardless. (A NULL `workspace` is the root cause of
a silent scheduled-dispatch failure — see `aw-agents-dispatch-reliability`.)

## `/v1` is a different surface and needs a different credential

The same base URL also serves an OpenAI-compatible surface
(`/v1/chat/completions`, `/v1/responses`). It does **not** accept the
`agents_platform_token` above. It needs a separate per-agent `ApiKey`,
minted for that specific agent. Don't reuse the REST token against `/v1`
and conclude the endpoint is broken when it 401s — mint the agent-specific
key instead.

If you're building something that talks to a CLI-backed runner agent
through `/v1` and needs clean JSON back: expect the response to be wrapped
in a markdown code fence no matter how firmly the prompt forbids it. Parse
the fence properly — a fence can open with more than three backticks and
the closing run must match the opening run's length. A naive
`text[3:-3]` slice is wrong, and it took three coder→QA round-trips to fix
a production bug caused by exactly that shortcut.

## Scoping an agent to exactly one MCP tool

Relevant when a spec calls for an agent that reaches a service's API as a
narrowed **service identity**, rather than a general Coder/QA agent with the
full tool surface. One session spent ~130 tool calls discovering this
end-to-end by reading the gateway's and the backend's own source. This is
the shortcut.

### 1. Create a gateway named config with `tools_allow`

The gateway's admin token lives somewhere different from the REST token
above:

```bash
GW_TOKEN=$(jq -r .token "$AW_WORKSPACE_HOME/data/mcp-gateway/gateway.json")
GW=http://aw-app-mcp-gateway:9200    # the gateway's in-network address

curl -s -m 15 "$GW/admin/configs" -H "Authorization: Bearer $GW_TOKEN" -o /tmp/named_configs.json
python3 -c "
import json
configs = json.load(open('/tmp/named_configs.json'))['configs']
configs['<profile-name>'] = {
    'upstreams':   ['<upstream-name>'],  # the app's own gateway upstream name
    'tools_allow': ['<tool_name>'],      # BARE tool name, not the aw__<app>__<tool> prefixed form
}
json.dump({'configs': configs}, open('/tmp/named_configs_new.json', 'w'), indent=2)
"
curl -s -m 15 -X PUT "$GW/admin/configs" -H "Authorization: Bearer $GW_TOKEN" \
  -H "Content-Type: application/json" --data-binary @/tmp/named_configs_new.json
```

No restart needed — `/mcp/{name}` is one dynamic route resolved per
request. Verify immediately:

```bash
curl -s -m 15 -X POST "$GW/mcp/<profile-name>" -H "Authorization: Bearer $GW_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

That must show **exactly** the one tool, under its plain name — the scoped
endpoint speaks the upstream's own naming convention, not the gateway's
prefixed one. Then prove a disallowed sibling tool on the same upstream is
actually **rejected** by `tools/call`, not merely absent from the list,
before wiring an agent to it.

If `tools/list` doesn't show a tool you just shipped in the upstream app,
check that first — a tool missing after an app update is its own problem,
not a `tools_allow` mistake.

### 2. Create the agent via raw REST, not the MCP wrapper

`create_agent`/`update_agent` expose neither `mcp_config` nor
`disable_harness_tools`. **There is no way to scope an agent through the MCP
surface at all.**

```bash
python3 -c "
import json
json.dump({
    'slug': '<agent-slug>',
    'name': '<Display Name>',
    'description': '...',
    'system_prompt': open('<path-to-prompt>').read(),
    'model_slug': '<cheapest model that can hold a single-tool loop>',
    'tool_specs': [],
    'disable_harness_tools': True,
    'mcp_config': {'servers': {'<label>': {
        'type': 'http',
        'url': '<gateway-url>/mcp/<profile-name>',
        'headers': {'Authorization': 'Bearer <gateway-token>'},
    }}},
}, open('/tmp/agent_body.json', 'w'))
"
curl -s -m 20 -X POST "$B/api/agents" -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" --data-binary @/tmp/agent_body.json
```

`disable_harness_tools: true` is the part that is easy to skip and easy to
regret — **without it the agent keeps every default harness tool on top of
the one you just scoped it to**, defeating the exercise. Note the literal
token has to be substituted into the body; Python single-quoted strings
don't expand shell variables.

### 3. Mint a scoped API key and store it properly

```bash
curl -s -m 20 -X POST "$B/api/api-keys" -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" \
  -d '{"name":"<agent-slug>-key","agent_slugs":["<agent-slug>"]}'
```

The returned `token` is a real bearer credential for
`POST /v1/chat/completions` with `model: "agent/<agent-slug>"`. Write it to
the secret store immediately, naming which agent it is scoped to and what
it is for, then securely delete every plaintext copy — of the key *and* of
the agent-creation body, which embeds the gateway token. Don't leave either
in a temp file after the run.

### 4. Mutation-check the restriction, not just the happy path

Confirming the tool is reachable proves only that it works — not that the
scoping holds. Temporarily break each guarding clause in the system prompt
in turn, confirm the failure mode actually changes, then restore and
re-verify. A few completions is cheap, and it is the only way to know the
restriction is doing something rather than being decorative. (Same
technique as the regression-test mutation check in `aw-agent-qa`.)
