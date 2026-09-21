---
name: aw-image-gallery
description: Como usar a Galeria de Imagens da Agents Platform (mini-app Telegram `/images` + as tools MCP `list_gallery_images`/`list_gallery_tags`/`set_gallery_tags`, servidas pelo app `agents-platform-runners`) para puxar fotos que um usuário já enviou sem pedir reenvio, filtrar por tag, ou marcar tags nelas via chat. Use sempre que o pedido envolver fotos/imagens já enviadas por um link de galeria, filtrar por tag, marcar/adicionar tag numa foto, ou perguntar "quais imagens eu já mandei".
---

# aw-image-gallery — Galeria de Imagens (upload em lote + tags)

## O que é

Um mini-app do Telegram (webapp), aberto pelo comando `/images` (admin-only,
qualquer bot) — o usuário sobe várias fotos de uma vez, sem enviá-las uma a
uma na conversa. Cada foto pode receber **tags** dentro do próprio mini-app
(ex: "inverno", "salto alto"). O backend
(`repos/agents-platform-multitenant/backend/app/api/gallery.py`, tabelas
definidas em `backend/app/models.py`) grava tudo em três-mais-uma tabelas:
`gallery_blocks` (um bloco = um lote de upload), `gallery_images` (arquivo
físico + qual bloco), `gallery_tags` / `gallery_image_tags` (vocabulário de
tags por bot + associação).

**Por que existe**: evita que o usuário tenha que reenviar 20 fotos manualmente
numa conversa de chat só pra você (agente) processar uma por uma — ele sobe
tudo de uma vez no mini-app, e você resolve o lote inteiro com uma única
chamada de tool.

## Quem serve estas tools (mudou em 21/09/2026)

As 3 tools **viviam no MCP `aw-crispal`** e agora vivem no MCP do app
**`agents-platform-runners`** (`agents_platform_runners_app/gallery.py` +
registo em `mcp_server.py`). O motivo é credencial, não arrumação: o container
do Crispal nunca teve credencial válida pra falar com a Agents Platform (as
três env vars que ele usava — `CRISPAL_AP_GALLERY_BASE` / `CRISPAL_AP_TOKEN` /
`CRISPAL_AP_INJECT_SECRET` — eram referenciadas no manifesto mas nunca
declaradas no `config_schema`, então resolviam vazias e **todo** esse caminho
estava morto). O `agents-platform-runners` já minta e renova sozinho um
`agents_platform_token`, que é exatamente o que os endpoints da galeria
aceitam. Uma credencial, num lugar só.

Consequência prática pra você: **o nome da tool pode ter mudado, dependendo de
como a sua sessão está ligada ao gateway.**

## Nome da tool na SUA sessão

**Não existe um prefixo universal — depende de como a sua sessão conecta.**

| Tipo de sessão | Nome da tool |
|---|---|
| Named config de um perfil do gateway (ex. `crispal-sonnet`/`-haiku`/`-codex`, que usam `crispal-full` sob o alias local `crispal`) | **não mudou**: `mcp__crispal__list_gallery_images` etc. O gateway remove o prefixo `{workspace}__{upstream}__` de **todos** os upstreams de um named config, com um ou com vários — então acrescentar o upstream do runners ao perfil não renomeia nada |
| Gateway completo, sem escopo (`coder-*`, `telegram-*`, esta sessão provavelmente) | **mudou**: era `aw__aw_crispal__list_gallery_images`, agora é `aw__agents_platform_runners__list_gallery_images` (nome completo: `mcp__aw-gateway__aw__agents_platform_runners__list_gallery_images`) |

**Agentes que perderam as tools:** `crispal-dev`, `crispal-image` e
`crispal-social` ligam-se **directamente** a `http://aw-app-crispal:9410/mcp`,
sem passar pelo gateway — para eles a galeria simplesmente deixou de existir.
Isso foi uma decisão de escopo consciente (nenhum deles usava a galeria a
sério). Se um pedido a um desses agentes precisar de galeria, **diga isso em
vez de tentar contornar** — a solução é acrescentar-lhes um segundo servidor
MCP, o que é decisão do dono do workspace.

Se não tiver certeza do seu caso: chame `ToolSearch` com **busca por
palavra-chave** primeiro (`query="list_gallery_images"`, sem `select:`) — ela
acha a tool independentemente do nome. Só use `select:<nome exato>` depois de
confirmar o nome real. E o achado de 2026-08-03 continua válido: **um
`ToolSearch` vazio NÃO prova que a tool não existe** em sessões com MCP
escopado por agente — chame o nome direto mesmo assim; só um `tool_result` de
erro real ("tool not found") é sinal confiável.

## `list_gallery_tags`

Lista todas as tags já usadas no vocabulário do bot, com contagem de imagens
por tag — chame **antes** de filtrar por tag, pra saber o que existe (não
adivinhe o nome da tag).

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `bot_slug` | não | Restringe a um bot. Omitido = **todas** as galerias deste workspace (ver abaixo). |

Retorna `{"tags": [{"name": "...", "image_count": N}, ...]}`.

## `list_gallery_images`

Resolve blocos de upload em **ids de imagem + URLs** — zero bytes de imagem
entram na sua janela de contexto.

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `scope` | não | `last_block` (default sem `tags`) = só o bloco de upload mais recente. `block` = um `block_id` específico. `since_block` = daquele `block_id` em diante. `all` (default quando `tags` é passado) = todo bloco já enviado pelo bot. |
| `block_id` | condicional | Obrigatório se `scope=block` ou `scope=since_block`. |
| `bot_slug` | não | Restringe a um bot. Omitido = todas as galerias deste workspace. |
| `tags` | não | Lista de nomes de tag pra filtrar (normalizados internamente). |
| `match` | não | `any` (default) = tem pelo menos uma das `tags`; `all` = tem todas. |
| `source` | não | Filtra por origem do bloco. Valores conhecidos: `upload` (usuário, via mini-app `/images`), `telegram_inbound` (usuário, anexo inline no chat), `arvin` (agente — geração do Arvin arquivada de volta), `agent` (agente, genérico). Omitido = todas as origens. |

Retorna:

```json
{
  "blocks":     [{"block_id": "...", "created_at": "...", "image_count": 3,
                  "source": "upload", "bot_slug": "cp-2"}],
  "images":     [{"id": "...", "block_id": "...", "url": "https://.../api/gallery/direct/<tok>",
                  "original_name": "...", "mime": "image/jpeg", "bytes": 1234,
                  "created_at": "...", "tags": ["inverno"]}],
  "image_urls": ["https://.../api/gallery/direct/<tok>", "..."]
}
```

`image_urls` está na **mesma ordem** de `images`, imagem a imagem — é só um
atalho pro campo `url`.

**Sobre `bot_slug` (mudou em 21/09/2026):** omitir `bot_slug` devolve as
imagens de **todas** as galerias deste workspace, não de um bot fixo. Isso não
é "sem escopo": o endpoint é autenticado com a identidade do workspace, que já
limita a resposta ao tenant dele — e cada bloco diz de que bot veio, no campo
`bot_slug`. A versão antiga destas tools tinha `aw-cris` fixo como default,
bot que **não existe** neste deployment (as 345 blocos vivem sob `cp-2`,
verificado ao vivo em 21/09/2026): uma chamada sem argumentos teria devolvido
lista vazia com 200, indistinguível de "você não tem fotos". Passe `bot_slug`
explicitamente quando quiser mesmo só um bot. Cada bloco em `blocks` carrega `source`, então dá pra
distinguir uma foto que o **usuário** subiu (`upload` / `telegram_inbound`) de
uma que o **próprio agente** gerou (`arvin` / `agent`) sem filtrar — e filtrar
com o parâmetro `source` quando o pedido for só um dos dois lados (ex: "só as
fotos que o Arvin gerou" → `source="arvin"`).

### Só há URLs — não existe mais `file_paths`

A versão antiga destas tools baixava cada imagem e devolvia também
`file_paths`, caminhos absolutos no disco do container do `aw-app-crispal`.
**Esse campo não existe mais**, e não é um esquecimento: este MCP roda dentro
do container do **aw-mcp-gateway**, que não compartilha nenhum directório
escrevível nem com o container do Crispal nem com um container de agente. Um
caminho devolvido daqui nomearia um ficheiro que mais ninguém consegue abrir.

Então, o que fazer com uma imagem:

- **Devolver ao usuário no Telegram** → `[[ATTACH: <url> caption="..."]]` com o
  `url` (ou `image_urls[i]`). Era assim que já tinha de ser feito — quem
  entrega a mensagem é outro container.
- **Passar pra uma tool que processa imagem** (ex. `arvin`,
  `crispal_image_search`) → passe a URL no parâmetro `image_url`; essas tools
  baixam elas próprias. **Não** procure um `image_path` local pra passar: não
  há nenhum.
- **Olhar você mesmo pra foto** → baixe a URL primeiro (ex. `curl -o`
  num ficheiro em `.tmp/`) e leia esse ficheiro. Não há atalho.

## `set_gallery_tags`

Marca (adiciona) tags numa lista de imagens, identificadas pelos **`id`** que
`list_gallery_images` devolve em `images[].id`. Cria qualquer tag que ainda não
exista no vocabulário do bot, depois vincula cada tag a cada imagem.
Idempotente — reaplicar uma tag que a imagem já tem não duplica nada. **Não
remove** tags existentes (só soma).

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `image_ids` | sim | Ids das imagens a marcar (`images[].id` de `list_gallery_images`). **Não são caminhos de ficheiro** — esses não existem mais. |
| `tags` | sim | Nomes de tag a aplicar em todas as imagens de `image_ids` (normalizados internamente). |
| `bot_slug` | não | Restringe a um bot. Omitido = o dono de cada imagem é descoberto automaticamente. |

Retorna `{"tagged_images": N, "tags_applied": N, "missing_image_ids": [...]}`
— `missing_image_ids` lista qualquer id que este bot não possui (id errado, ou
imagem de outro bot); as restantes são marcadas na mesma. `tags_applied` conta
só os vínculos **novos**, então reaplicar as mesmas tags devolve 0, e isso é o
resultado correcto, não uma falha.

## Fluxos comuns

- **"Me manda a última imagem da galeria" / "me manda essa foto de volta"** →
  `list_gallery_images(...)` → pegue o `images[i].url` →
  `[[ATTACH: <url> caption="..."]]`.
- **"Me dá as últimas fotos que eu mandei"** → `list_gallery_images()` sem
  parâmetro nenhum (default = `last_block`, o lote mais recente).
- **"Me dá as imagens de inverno"** → chame `list_gallery_tags()` primeiro se
  não tiver certeza do nome exato da tag, depois
  `list_gallery_images(tags=["inverno"])` (isso já busca em **todos** os
  blocos, não só o último — filtro por tag é cross-block por natureza).
- **"Quais tags eu já usei?"** → só `list_gallery_tags()`.
- **"Só as fotos que o Arvin gerou" / "quais fotos eu mesmo subi"** →
  `list_gallery_images(source="arvin")` (ou `source="upload"` pro lado do
  usuário).
- **"Marca essas fotos como 'inverno'"** → pegue os `images[].id` (via
  `list_gallery_images`, ex. `scope=last_block` se "essas" = as últimas
  enviadas) e chame `set_gallery_tags(image_ids=[...], tags=["inverno"])`.
- **"Gera um post com as fotos de inverno"** → `list_gallery_images(tags=[...])`
  → um `arvin(image_url=<url>)` por imagem (ver a skill `aw-crispal-arvin`).

## Não fazer

- Não peça pro usuário reenviar fotos que ele já subiu pela galeria — resolva
  com `list_gallery_images` primeiro.
- Não invente nome de tag — se não tiver certeza, chame `list_gallery_tags`
  antes de filtrar.
- Não passe `file_paths` pra `set_gallery_tags` nem procure um caminho local
  no retorno de `list_gallery_images`. O parâmetro é `image_ids`, e paths não
  existem mais.
- Não conclua que a tool sumiu porque `ToolSearch` veio vazio — veja a tabela
  de nomes acima e tente o nome direto.
