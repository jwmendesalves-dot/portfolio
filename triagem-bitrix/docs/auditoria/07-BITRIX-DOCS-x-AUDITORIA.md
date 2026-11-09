# Documentação oficial Bitrix24 × auditoria da triagem

Base: `BITRIX24-DOCS-OFICIAL.zip` (3.062 .md, b24restdocs sincronizado em 06/11/2026), extraído em
`~/Documents/bitrix24-docs/`, + `BITRIX-ESCRITORIO.md` (portal real). Caminhos abaixo são relativos à pasta da doc.

O que a doc oficial **muda ou confirma** nas correções propostas em 00–06.

---

## 1. Cursor de leitura: a API já tem o que falta (F1-01/F1-02)

- `im.dialog.messages.get` aceita **`FIRST_ID`** = "mensagens com id **maior** que este" (`api-reference/chats/messages/im-dialog-messages-get.md`). É o cursor incremental.
- O código (`triagem_grupo.py:1245-1279`) só usa `LAST_ID=9999999999` e desce até 40 páginas × 50 = **2.000 mensagens por chat por ciclo**, sempre. É daí que vem a janela deslizante que gera lotes vazios/BAIXA/duplicatas.
- Correção baseada na doc: guardar `max(id)` por chat → próximo ciclo pede `FIRST_ID=<cursor>`, `LIMIT=50`, repetindo até vir vazio. Releitura curta (ex.: últimas 2 h) só para fechar lote aberto, nunca o histórico.
- Pegadinha documentada: "o método pode devolver **mais** que `LIMIT` se houver mensagens não lidas" — não usar `len == LIMIT` como critério de parada; usar "veio vazio" ou "nenhum id novo".
- `LIMIT` máximo é **50**.

## 2. Os dois grupos são Linha Aberta — e isso explica a perda do webhook (hipótese forte)

- `BITRIX-ESCRITORIO.md` §6: `chat100001` e `chat100002` são **Open Channel** (`type: "lines"`).
- A doc de `im.dialog.messages.get`: *"Quem pode executar: **participante do chat**"* e *"mensagens só podem ser obtidas **sem participar do chat**, para Linhas Abertas, via `imopenlines.session.history.get`"*.
- Linha Aberta é organizada em **sessões**; o webhook age como o usuário que o criou (user 1). Se o user 1 não participa de todas as sessões/trechos, o `im.dialog.messages.get` devolve um **subconjunto** — exatamente o sintoma medido (234 × 386, "perdia justamente kits/anexos").
- Caminho documentado para Linha Aberta:
  1. `imopenlines.dialog.get` (`DIALOG_ID=chat100001`) → `entity_data_1`, 6º campo = `SESSION_ID`;
  2. `imopenlines.session.history.get` com `SESSION_ID` (recomendado) ou `CHAT_ID` numérico (pega a última sessão);
  3. escopo **`imopenlines`** no webhook (conferir com `scope.json`).
- **Antes de trocar `fonte_leitura`**: medir no servidor, só leitura, no mesmo intervalo: VibeCode × `im.dialog.messages.get` (com `FIRST_ID`) × `imopenlines.session.history.get`. A medição de 21/09 é anterior à correção de paginação de 21/10.

## 2b. O robô é bot nativo do Bitrix (100003) — pode virar a fonte de leitura

O dono confirmou que o robô da triagem é um bot criado dentro do Bitrix (não Telegram). A doc de bots v2 (`api-reference/chat-bots/chat-bots-v2/index.md:89-109`) abre um caminho melhor que webhook e VibeCode:

- Bot do tipo **`supervisor`** ou **`personal`** *"vê todas as mensagens dos chats de que é membro"*, sem precisar de menção. Tipo `bot`/`openline` só recebe quando mencionado.
- Com `eventMode: "fetch"`, **`imbot.v2.Event.get`** entrega a fila de eventos com `offset`/`nextOffset` — um cursor confirmado pelo próprio Bitrix (`imbot.v2/events/event-get.md:16-32`). Funciona por webhook usando `botToken` (sem OAuth).
- **`imbot.v2.File.download`** baixa os anexos pelo bot; **`Chat.Message.getContext`** devolve até 50 mensagens em volta de uma (só supervisor/personal) — serve para reconciliar.
- O código já lê eventos do bot pela VibeCode (`GET /v1/bots/100003/events`, `triagem_grupo.py:1282`), que provavelmente embrulha esse mesmo `Event.get`.

Pergunta que decide: **qual o `type` e o `eventMode` do bot 100003** e se ele é membro dos dois grupos. A coleta (`coleta-triagem.sh` §9) consulta `imbot.v2.Bot.get`. Se for `supervisor`/`personal` + `fetch`, a recomendação passa a ser: **bot como fonte primária** (cursor por `offset`), `imopenlines.session.history.get` como reconciliação, webhook `im.dialog.messages.get` só diagnóstico.

## 3. Tempo real por evento — viável, mas não com o webhook atual

- Evento certo para Linha Aberta: **`ONOPENLINEMESSAGEADD`** (cliente, operador ou sistema; traz `message{id,date,text,files}`) — `api-reference/imopenlines/openlines/events/on-open-line-message-add.md`. Não existe `ONIMOPENLINE*`.
- Assinatura **só por aplicação** (app local OAuth): `event.bind` com webhook de entrada dá `WRONG_AUTH_TYPE` (`api-reference/events/event-bind.md:18`).
- A doc diz que o Bitrix **não reenvia** evento que falhou (`api-reference/events/index.md:167`). Logo: evento serve para reduzir latência, **o polling com `FIRST_ID` continua obrigatório** como reconciliação. Recomendação: não é prioridade; o cursor resolve o problema principal.

## 4. Rate limit é por IP — e o servidor tem 7+ integrações no mesmo IP

- Leaky bucket: plano não-Enterprise = **2 req/s, rajada 50**; Enterprise = 5 req/s, rajada 250 (`settings/performance/limits.md`). Estouro = **503 `QUERY_LIMIT_EXCEEDED`** (não 429).
- *"Se várias aplicações no mesmo servidor trabalham com o mesmo Bitrix24, **compartilham** o limite"* e *"não rode séries paralelas de requisições do mesmo IP"*.
- No servidor rodam `/opt/triagem-bitrix`, `qc-planilha`, `entrada-planilha`, `leads-doenca`, `hub-planilhas`, `api-documentos`, `agente-separador` + os 4 timers da triagem + o MCP. O throttle de 1 req/s do `triagem_grupo.py:239` é **por processo**; `baixar_pendentes`/`download_svc`/`completar_cpf_por_nome`/`certidoes` não respeitam nem esse.
- Isso reforça B1/B3/ID-04: o disjuntor abrindo "sozinho" provavelmente é a soma dos consumidores. Correção: **um limitador único por host** (token bucket em arquivo com `flock`, ex. `/run/bitrix-ratelimit`) usado por todo cliente Bitrix do servidor, com teto ~1,5 req/s total.
- Limite de recurso: **429 `OPERATION_TIME_LIMIT`** por método/webhook, 10 cestos de 1 min; esperar `operating_reset_at`. O `espera_429` do `triagem_grupo.py:281` já segue isso — os outros scripts não.

## 5. Política de retry segundo a doc (D1/C2/PT-01)

| Erro | Repetir? |
|---|---|
| `QUERY_LIMIT_EXCEEDED` (503) | sim, atraso crescente; recusa antes de executar, não duplica |
| `OPERATION_TIME_LIMIT` (429) | só depois de `operating_reset_at` |
| `ACCESS_DENIED`, `insufficient_scope`, 400/403/404 | **nunca** — repetir arma o `ERROR_LOOP_DETECTED` (que já calou o bot) |
| `OVERLOAD_LIMIT` | nunca (bloqueio manual) |
| timeout/erro de rede em **escrita** | não repetir às cegas — pode ter executado (tarefa de certidão duplicada, CE-03) |
| `batch` | nunca repetir inteiro; olhar `result_error` por item |

Fontes: `settings/performance/limits.md`, `error-codes.md`, `NOTAS-B-limites-erros.md` §4–5.

## 6. CPF por nome: a doc dá a 2ª confirmação (ID-02)

- `crm.duplicate.findbycomm` (`TYPE=PHONE`, até 20 valores, `ENTITY_TYPE=CONTACT`) acha o contato pelo **telefone** da ficha/autor — `api-reference/crm/duplicates/crm-duplicate-find-by-comm.md`.
- Regra proposta: só gravar CPF se **nome (busca paginada) e telefone** apontam para o **mesmo** contato; nunca limpar `requer_revisao`.
- Paginação estável para não ler só os 50 primeiros: filtro `>ID` + `order ID ASC` + `start=-1` (`settings/performance/huge-data.md:12-40`).

## 7. Credenciais (S06/S07) — a doc é explícita

- Webhook de entrada **age como o funcionário que o criou**; o webhook em uso é do **user 1 (administrador) com 63 escopos**, em texto dentro de `/opt/data/mcp/bitrix24_mcp_server.py` (`BITRIX-ESCRITORIO.md` §1).
- A doc proíbe segredo em código/repositório (`local-integrations/local-webhooks.md:77`, `settings/cloud-and-on-premise/security-recommendations.md:148-150`).
- Correção: **webhook dedicado para a triagem**, criado por um usuário de integração (não o admin), só com `im`, `imopenlines`, `disk`, `crm`, `task`; guardado em arquivo `0600` fora do código (`/etc/triagem-bitrix/webhook`, lido por `EnvironmentFile=` do systemd). Depois, revogar o admin do código do MCP e rotacionar.
- Os 2 tokens de `/opt/qc-planilha/bitrix-webhook.token` dão 401: apagar o arquivo para ninguém "achar" credencial morta.

## 8. Download

- `disk.file.get` → `DOWNLOAD_URL` assinada que **expira em minutos**: pedir URL nova a cada tentativa, nunca guardar na fila (D3).
- IDs do Telegram (`CQAC…`) não são de disco: nunca mandar ao `disk.file.get` (`BITRIX-ESCRITORIO.md` §5.3) — confirma o achado de `baixar_pendentes`.
- Anexar no card só via `crm.timeline.comment.add` (útil se a triagem passar a anexar o kit no negócio).

---

## Impacto no plano (00-RESUMO §6)

- **Fase 1** ganha: limitador único por host (item 4) e política de retry por código (item 5).
- **Fase 2, item 11** passa a ser concreto: cursor com `FIRST_ID` (item 1).
- **Fase 2, item 12** muda: a medição deve incluir `imopenlines.session.history.get` — é a fonte documentada para Linha Aberta (item 2). Possível que dispense a VibeCode.
- **Fase 0, item 5** fica mais preciso: webhook dedicado de escopo mínimo (item 7).
