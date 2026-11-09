# 05 — Identificação (nome/CPF), scripts auxiliares e integrações laterais

Escopo: `nome_regras.py`, `completar_cpf_por_nome.py`, `nomear_lotes_por_cpf.py`, `backfill_nome_lotes.py`,
`reparo_josilene.py`, `certidoes.py`, `certidoes-rodar.sh`, `pintar_linha.py`, `backup_pintura.py`,
`conferir_pintura.py`, `pergunta_traco.py`, `pergunta-rodar.sh`, `telegram_grupo.py` (mensagens/estado).
Cópia local: `~/Downloads/triagem-codigo-20261109-1552` (somente leitura). Nenhum script de rede foi executado.

## Testes executados

`python3 -m pytest testes/{test_nome_regras,test_pintar_linha,test_telefones_fichinha,test_pasta_fichinha,test_telegram_respostas,test_telegram_batidas}.py -q -p no:cacheprovider`
→ **97 passaram, 3 falharam**. As 3 falhas são de ambiente, mas uma delas mostra um bug latente:

- `test_telefones_fichinha::test_telefones_sem_repeticao_e_no_maximo_dois`: abre `/opt/triagem-bitrix/estado.json` direto (teste:20). O teste depende do servidor.
- `test_nome_regras::test_job_prefere_o_nome_do_lote_quando_a_ficha_vem_suja` e `test_pasta_fichinha::test_cpf_invalido_nao_consulta`: `lote_para_job.codigo_pasta_do_cliente` (lote_para_job.py:400) importa `nomear_lotes_por_cpf`, que chama `_webhook()` **na importação** (nomear_lotes_por_cpf.py:69) e levanta `SystemExit`. O `except Exception` da linha 401 **não pega SystemExit**. Ver [ID-09].

---

## Achados

### [ID-01] `completar_cpf_por_nome` libera o lote para a ponte e apaga TODOS os motivos de revisão
· **CRÍTICA** · completar_cpf_por_nome.py:136-140
· Evidência:
```python
lotes[lid]["identificacao"] = "OK"
lotes[lid]["requer_revisao"] = False
lotes[lid]["motivos_revisao"] = []
lotes[lid]["pronto"] = True
```
O filtro de alvos (linhas 96-103) só olha `cpf`, `cpfs_distintos` e `arquivos`. Não olha `pronto`, `status`, `requer_revisao` nem o motivo da revisão.
· Impacto: um lote SEM_CPF que estava em revisão por **outro** motivo perde esse motivo. Exemplos: `revisao_duplicata` gravado por lote_para_fila.py:140-145 ("decisão humana, não descarte"), lote misto, fechamento BAIXA por janela de tempo (são 1.102 lotes) ou lote ainda aberto (`pronto=False`, ainda juntando arquivos). Sem o motivo, o lote passa a ser "pronto" e "sem revisão". O `ponte_criterio.motivo_recusa` (ponte_criterio.py:27-35) só bloqueia por `pronto`/`requer_revisao`/CONFLITO, então a ponte envia o lote no ciclo seguinte. O `status="revisao_duplicata"` continua no registro, mas a ponte não o confere. Resultado possível: um kit incompleto ou misturado chega ao painel e vira contrato com o CPF achado por nome.
· Correção: (1) só considerar lotes com `pronto=True` cujos `motivos_revisao` sejam **apenas** "sem CPF"; (2) remover **só** esse motivo e manter os outros; (3) nunca escrever `pronto=True`; (4) pular `status` que comece com `descartado_`/`revisao_` e lotes CONFLITO.
· Confiança: confirmado no código. Quantos lotes já passaram por isso: depende do `estado.json` do servidor.

### [ID-02] "Exatamente 1 contato" é falso: a busca lê só a 1ª página (50) e filtra pelo 1º nome
· **ALTA** · completar_cpf_por_nome.py:56-71
· Evidência: `chamar("crm.contact.list", {"filter[%NAME]": primeiro, ...})`. Não há `start` nem paginação. O `%NAME` faz LIKE no **primeiro nome** (`MARIA`, `ANA`...) e o Bitrix devolve no máximo 50 registros por página.
· Impacto: com primeiro nome comum, o CRM tem centenas de contatos e o script vê só os 50 primeiros. Dois erros possíveis:
  - **Falso positivo (grave):** o homônimo exato está na página 2+. O script vê só 1, preenche o CPF dele e libera o lote ([ID-01]). O lote vai para o painel com o **CPF de outra pessoa**, e a pintura ([PL-03]) e a pasta seguem esse CPF.
  - **Falso negativo:** o contato certo está fora da 1ª página. Isso alimenta os 639 SEM_CPF.

  Além disso, mesmo com a busca completa, "1 homônimo no CRM" não prova identidade. Um cliente novo (ainda sem contato) com o mesmo nome de um cliente antigo recebe o CPF do antigo.
· Correção: paginar até `next` acabar (ou filtrar por `NAME`+`LAST_NAME` exatos com `=`). Exigir uma **segunda âncora** antes de gravar: telefone do contato (`PHONE`) igual a um telefone da ficha (`parse_ficha` já extrai `telefones`, lote_para_job.py:257-314) ou igual ao autor da mensagem. Validar o DV do CPF do CRM (`triagem_grupo.cpf_plausivel`, triagem_grupo.py:677). Ver a proposta no fim.
· Confiança: código confirmado. O limite de 50 por página é comportamento documentado do REST do Bitrix.

### [ID-03] `completar_cpf_por_nome` grava o `estado.json` inteiro sem a trava do `estado_store` (perde atualizações)
· **ALTA** · completar_cpf_por_nome.py:88 (lê) … 141-147 (grava)
· Evidência: `json.load(open(ESTADO))` no início e `json.dump` + `os.replace` no fim. Não usa `estado_store.atualizar_estado`, que é a trava criada justamente porque "6 dos 7 lotes do Telegram sumiram" (estado_store.py:5-17). Entre ler e gravar, o script faz 1 chamada ao Bitrix + `sleep(0.55)` **por lote**.
· Impacto: o script roda a cada 10 min (pipeline.sh:13, timer `triagem-lote-fila`). A janela entre ler e gravar dura minutos. Tudo o que outros processos gravarem nesse intervalo é sobrescrito: ponte (5 min, `marcar_duplicatas_no_estado`), `telegram_grupo.mesclar_no_estado`, `triagem_grupo`, `reconciliar_por_file_ids`, `dividir_lote_misto`. Pode sumir uma marcação de duplicata (o lote volta a ser candidato) ou um lote recém-criado do Bitrix ou do Telegram. Os do Telegram voltam pelo `reforcar_lotes_salvos`; os outros só voltam se o ciclo reler o histórico.
· Correção: buscar CPFs fora da trava, sobre um snapshot. Aplicar dentro de `atualizar_estado(mutador)`, relendo cada lote e conferindo que continua SEM_CPF. O mesmo vale para [AUX-01].
· Confiança: confirmado no código. Que a sobreposição já aconteceu é hipótese (depende do horário dos timers).

### [ID-04] O ciclo de 10 min consulta o Bitrix de novo para cada lote SEM_CPF, sem cache de resultado negativo
· **ALTA** · completar_cpf_por_nome.py:91-117 e pipeline.sh:13
· Evidência: `--limite` vale 0 (tudo). `buscar_cpf_por_nome` é chamado para todo lote sem CPF que tenha nome e arquivos, em **toda** rodada. `nomear_lotes_por_cpf` tem `.cache_cpf_nome.json`; este script não tem cache nenhum.
· Impacto: centenas de `crm.contact.list` a cada 10 min, sempre com os mesmos nomes e a mesma resposta (dezenas de milhares por dia). Isso pesa no limite do webhook que a triagem usa também para `disk.file.get`. O CONTEXTO registra o **circuit-breaker de download aberto desde 09/11 13:06** porque "o Bitrix bloqueia". A duração da rodada também aumenta a janela de [ID-03].
· Correção: cache por nome normalizado com TTL (ex.: 24 h para "0 ou >1 resultado"), teto por rodada (`--limite 30`) e reconsulta só quando o lote mudar (`atualizado_em`). Não rodar enquanto o circuit-breaker estiver aberto.
· Confiança: código confirmado. A relação com o bloqueio é hipótese, mas plausível.

### [ID-05] Nenhum dos caminhos laterais valida o dígito verificador do CPF
· **MÉDIA** · completar_cpf_por_nome.py:73-76 · certidoes.py:163-172 · pintar_linha.py:55-60 · lote_para_job.py:377-381 · triagem_grupo.py:723-724
· Evidência: todos aceitam qualquer sequência de 11 dígitos. Na extração principal, `aceitar = True` quando há rótulo "CPF" **ou** separador, **sem** DV (triagem_grupo.py:723). Já existe `cpf_plausivel` (triagem_grupo.py:677), mas só é usado para 11 dígitos sem rótulo.
· Impacto: um CPF digitado errado ("CPF 123.456.789-01") vira âncora do lote. Na pintura, gera uma **linha nova** com CPF errado na planilha ([PL-03]). Em certidões, um telefone sem formatação na legenda vira "CPF", a busca por nome nunca roda e a mensagem cai em "sem negócio". No CRM, um CPF lixo cadastrado é copiado para o lote.
· Correção: um `cpf_valido()` único em `nome_regras.py` (ou num módulo `cpf.py`), usado em todos os pontos acima. CPF com DV inválido deve ir para revisão com o motivo "CPF com DV inválido", em vez de ser descartado em silêncio.
· Confiança: confirmado no código.

### [AUX-01] Scripts de reparo pontual continuam na pasta de produção, gravam o estado sem trava e um deles não é idempotente
· **MÉDIA** · reparo_josilene.py:49-133 · backfill_nome_lotes.py:55-92 · nomear_lotes_por_cpf.py:142-213
· Evidência: os três fazem `json.load` → mutação → `os.replace` sem `estado_store` e criam o próprio `estado.json.bak-*`. Em `reparo_josilene` (lotes e fileIds fixos no código, de 04/11), a guarda de "já corrigido" é só `if not atrasados` (linha 70), que procura os índices `01..04` na pasta da Kliciane. **Depois do reparo**, a Kliciane fica com `01..06` (os arquivos dela, renumerados). Rodar `--aplicar` de novo move os 4 primeiros arquivos **dela** para a Josilene. O `shutil.move` (linha 95) pode sobrescrever destinos com o mesmo nome, e as linhas 113-114 duplicam `FIDS_ATRASADOS` em `file_ids`.
· Impacto: se alguém rodar de novo, o estado fica corrompido e um lote real perde arquivos. Os três somam backups de 15 MB aos 27 GB já existentes. `backfill_nome_lotes` importa `triagem_grupo` inteiro, que tem efeitos colaterais na importação (hipótese).
· Correção: mover `reparo_josilene.py`, `backfill_nome_lotes.py`, `backup_pintura.py` e `conferir_pintura.py` para `backups/scripts-usados/` (ou removê-los do deploy). Se `nomear_lotes_por_cpf` continuar existindo, usar `atualizar_estado`. Em `reparo_josilene`, a guarda deveria conferir `FIDS_ATRASADOS in j["file_ids"]`.
· Confiança: confirmado no código.

### [AUX-02] `nomear_lotes_por_cpf`: pega o 1º contato sem conferir se é único e guarda "não achei" para sempre
· **BAIXA** · nomear_lotes_por_cpf.py:113-116, 182-186
· Evidência: `for reg in ...: nome = nome_do_registro(reg); if nome: return nome`. Com CPF duplicado no CRM e nomes diferentes, vale o primeiro. `cache[cpf] = nome` grava também `""`, sem prazo de validade.
· Impacto: o nome pode vir errado quando o CPF está duplicado no CRM. Um contato criado depois nunca é encontrado.
· Correção: exigir que todos os contatos tenham o mesmo nome canônico, com TTL no cache negativo.
· Confiança: confirmado no código.

### [CE-01] Certidões: falha marca a mensagem como processada e uma queda no meio da rodada duplica tarefas
· **ALTA** · certidoes.py:362-376, 429-441
· Evidência: `msgs_processadas` recebe **todas** as `entradas` (linhas 430-432), inclusive as que deram "não consegui baixar — pulando" (363-365) ou "falha ao criar tarefa" (375-376). O estado só é gravado **no fim** (441). Uma exceção no meio interrompe a rodada sem gravar: `subprocess.TimeoutExpired` do node (timeout 300, linha 207), `.get` em resposta que é lista (`t.get`/`d.get`), arquivo `vibecode.key` ausente etc. As tarefas já criadas nessa rodada **são criadas de novo** na próxima, 5 min depois.
· Impacto: (a) certidão que falhou num pico do Bitrix/VibeCode some sem tarefa e sem alerta, porque não há nova tentativa; (b) tarefa duplicada para a Anna Luisa quando a rodada cai.
· Correção: gravar o estado **após cada mensagem** (antes e depois de `criar_tarefa`, com `status: criando/ok/falhou`). Só marcar como processada o que deu `ok` ou "sem negócio avisado". Falha entra numa lista `retentar` com contador e, depois de N tentativas, vira aviso. Para evitar duplicar, antes de criar a tarefa procurar uma existente (`tasks.task.list` com `filter[UF_CRM_TASK]=D_x` e o título/ID da mensagem na descrição).
· Confiança: confirmado no código.

### [CE-02] Certidões: foto da certidão é tratada como PDF e a IA nunca lê
· **ALTA** · certidoes.py:323-325, 191-194
· Evidência: `caminho_local = f"/tmp/certidao_{e['id']}.pdf"` sempre, e o script node decide pela extensão: `if (ext === 'pdf') pdf.extrairTexto(...)`. Um JPG (o caso comum no grupo) entra no extrator de PDF. O upload também é sempre `certidao_x.pdf` (linha 366).
· Impacto: sem CPF ou nome na legenda, o passo 3 da cascata ("leitura da certidão") sempre falha com foto. A mensagem cai em "Não achei o cliente desta certidão" e a equipe resolve na mão. O anexo da tarefa vai com extensão errada.
· Correção: descobrir o tipo pelos bytes (`%PDF` / `\xFF\xD8` / `\x89PNG`), usar a extensão certa no caminho temporário e no upload, e passar o `mimeType` correto.
· Confiança: confirmado no código.

### [CE-03] Certidões: busca por nome sem garantia de unicidade, com o nome da criança e o "1º negócio" qualquer do contato
· **ALTA** · certidoes.py:154-160, 147-151, 213-214
· Evidência: `achar_contato_por_nome` → `filter[%NAME]=<nome completo>` → `return achados[0]`, sem conferir unicidade. `achar_deal_por_contato` → `achados[0]`, sem ordem, sem filtro de etapa ou funil. A docstring (linha 14) diz "nome da **mãe**", mas o código usa `dados.nome || nome_completo` (linha 214), que numa certidão de nascimento é o nome do **registrado** (a criança). `achar_contato_por_cpf` também aceita até 5 contatos e pega o primeiro (linha 142).
· Impacto: tarefa (com a certidão de um menor anexada, que é dado sensível) vinculada ao **negócio errado**: um homônimo, um negócio antigo ou perdido do mesmo contato, ou nenhum, porque a busca vai pelo nome da criança enquanto o contato é a mãe.
· Correção: com nome, exigir exatamente 1 contato e casar `NAME+LAST_NAME` completos. Ler `nome_mae` da IA. Escolher o negócio com `order[DATE_CREATE]=DESC` e `filter[CLOSED]=N` (ou a categoria PREVI). Com 0 ou mais de 1, mandar o aviso no projeto, como já acontece no caso sem contato.
· Confiança: confirmado no código. O nome dos campos que a IA devolve é hipótese (ia.js está no painel).

### [CE-04] Certidões: o filtro do grupo pega certidão de casamento e lê só 50 mensagens
· **MÉDIA** · certidoes.py:220, 239-241
· Evidência: `re.search(r"certid[ãa]o|nascimento|registro civil", texto)` dispara com "certidão de **casamento**" (caso real no corpus: `nome_regras.py:10` cita 'Certidão de casamento Bianca'). A leitura é `params={"limit": 50}`, sem paginação.
· Impacto: tarefa de "Certidão de nascimento" criada com uma certidão de casamento do kit. Se o timer parar e o grupo (386 mensagens/dia) passar de 50 mensagens nesse intervalo, certidões são perdidas em silêncio.
· Correção: no grupo, exigir `nascimento` (ou "certidão" sem "casamento|óbito"). Paginar até chegar ao último id processado (guardar `ultimo_id` por chat em vez da lista crescente `msgs_processadas`).
· Confiança: confirmado no código.

### [CE-05] Certidões: arquivos em /tmp com PII que nunca são apagados, webhook visível no `ps` e código de saída sempre 0
· **BAIXA** · certidoes.py:101-104, 205, 323-324 · certidoes-rodar.sh:4,13
· Evidência: `/tmp/certidao_<id>.pdf` e `/tmp/_ler_certidao.js` (caminho fixo e previsível, rodando como root) nunca são removidos. `bx()` chama `curl` com a **URL do webhook (que contém a chave)** nos argumentos do processo, visível a qualquer usuário local em `ps`. O `-d k=v` não faz URL-encode (`&`/`+` no texto quebram o aviso). No `.sh`: `set -u` sem `-e`/`pipefail`; a última linha é um `echo`, então o serviço **sempre termina com 0**, e o systemd e o monitor nunca veem falha.
· Correção: `tempfile.NamedTemporaryFile(delete=True)` e `--data-urlencode`, ou usar `urllib` como o resto do código. No `.sh`: `code=$?; echo ...; exit $code` (como já faz o `ponte-rodar.sh`).
· Confiança: confirmado no código.

### [PL-01] Pintura: o cliente novo é escrito numa "linha livre" sem conferir na hora de gravar (pode sobrescrever a linha de outro)
· **ALTA** · pintar_linha.py:143-152, 271-281
· Evidência: `linha_livre()` é decidida na leitura (linha 324). `gravar()` escreve `B` e `G` com `values().batchUpdate` RAW nessa linha, **sem reler**. A docstring promete "nunca sobrescreve célula preenchida" (linha 11), mas nada garante isso no momento da escrita. O painel (`app2.planilha`) também cria linha por CPF (docstring, linha 15) e a equipe digita na mão.
· Impacto: se o painel ou uma pessoa ocupar a mesma "próxima linha" entre a leitura e a escrita (alguns segundos, a cada 2 min), o NOME e o CPF dessa linha são **trocados pelos de outro cliente**. A PASTAS PREVI GERAL é a referência de pastas do escritório.
· Correção: antes de cada escrita, reler `B{n}:G{n}` e só gravar se estiver vazia; se não estiver, recalcular. Melhor ainda: usar `values.append` com `insertDataOption=INSERT_ROWS` numa faixa ancorada em B:G, que é atômico do lado do Google. Depois de gravar, reler e conferir o CPF.
· Confiança: código confirmado. A colisão com o painel é hipótese (o código do painel não está no escopo).

### [PL-02] Pintura: lê a planilha inteira a cada 2 min, mesmo quando tudo já foi pintado
· **MÉDIA** · pintar_linha.py:311-326, 135-137
· Evidência: o filtro `pintadas` só é aplicado **dentro** de `planejar` (`decidir`, linha 135), **depois** de `abrir()`, `meta_aba()` (que lê a coluna A inteira; ela é pré-numerada até o fim da grade, então `ultima` ≈ total da grade) e `ler_bloco(A1:Q<total+n+5>)`. Qualquer lote pronto do dia já pintado dispara isso.
· Impacto: cerca de 720 leituras por dia de ~10 mil linhas × 17 colunas, mais duas chamadas de metadados. Está longe de estourar a cota de 300 leituras/min, mas é tráfego e latência inúteis, e a credencial é a mesma conta de serviço do painel (`/opt/agente-separador/service-account.json`, escopo `spreadsheets` completo). Se o Google devolver 429 ou 5xx, o painel divide a mesma cota.
· Correção: filtrar `lotes = [l for l in lotes if cpf_norm(l["cpf"]) not in pintadas]` **antes** de `abrir()`. Usar a última linha preenchida em `B`/`G` em vez de `A:A`.
· Confiança: confirmado no código.

### [PL-03] Pintura: CPF errado gera linha nova e a marcação por CPF é para sempre
· **MÉDIA** · pintar_linha.py:135-152, 350-353
· Evidência: `pintadas[cpf]` não expira e não guarda o lote. Cliente que manda documentos de novo meses depois não é repintado. Lote com CPF errado (DV inválido [ID-05] ou CPF de homônimo [ID-02]) cai em "cliente novo" e **cria uma linha** com nome e CPF errados. O painel, com o CPF correto, cria outra.
· Correção: validar o DV antes de criar; criar linha só se `cpf_origem` não for `bitrix-nome` (ou exigir a 2ª âncora); guardar `pintadas[cpf]` com o `lote_id` e repintar quando chegar lote novo depois de X dias.
· Confiança: confirmado no código.

### [PT-01] Bot do traço: timeout de rede depois do envio leva a reenvio a cada 8 s (spam); disco cheio também
· **ALTA** · pergunta_traco.py:134-146, 263-291, 515-521, 569-570
· Evidência: `_req` converte em `RuntimeError` só o `HTTPError`. Timeout de leitura e `URLError` (`timeout=40`) passam direto por `bot_fala` (que só trata `RuntimeError`) e caem no `except Exception` da rodada: `sucesso=False`, "tento de novo na próxima". Se o POST **foi entregue** e só a resposta demorou, a mensagem sai de novo a cada rodada (loop de 8 s) até a janela de 45 min, até ~300 vezes. Do mesmo modo, `salvar(est)` (305-310) não tem tratamento de erro: com o disco cheio (os backups crescem ~2,1 GB/dia, ver CONTEXTO §7), a rodada envia, não grava `ultimo_traco` e reenvia na rodada seguinte.
· Impacto: spam no grupo das clientes e disparo do `ERROR_LOOP_DETECTED`, que bloqueia a API key inteira (incidente de 20/10 descrito nas linhas 52-55). Durante o bloqueio, nenhum bot fala em nenhum chat.
· Correção: avançar `ultimo_traco` **antes** de enviar (no máximo uma tentativa por traço) ou gravar um estado `enviando` com o id da mensagem. Tratar `URLError`/`TimeoutError` como "talvez entregue" e não repetir. Proteger `salvar()` e, se falhar, parar de enviar (por exemplo, criar `bot-bloqueio.json` em memória).
· Confiança: confirmado no código. A entrega com timeout é hipótese de rede.

### [PT-02] Bot do traço: trava de instância só no `--loop`; o `pergunta-rodar.sh` roda sem trava
· **MÉDIA** · pergunta_traco.py:697-702, 722-723 · pergunta-rodar.sh:3
· Evidência: `trava_de_instancia()` só é chamada no ramo `--loop`. `pergunta-rodar.sh` roda `--ativo` (uma vez) sem `flock`. Se existir ao mesmo tempo um serviço `--loop 8` e um timer chamando o `.sh`, os dois leem o mesmo `pergunta-traco-estado.json` e cada um responde ao mesmo traço. É a "resposta dobrada" que a própria docstring cita (linha 600).
· Correção: chamar `trava_de_instancia()` também no modo de rodada única (e sair se estiver ocupada). Adicionar `flock -n` no `.sh` e escolher **um** modo em produção.
· Confiança: código confirmado. Quais unidades existem no servidor não deu para verificar.

### [PT-03] Bot do traço: `ignorar_autores` compara pelo nome exibido
· **BAIXA** · pergunta_traco.py:417, 441-445 · config.json `pergunta_traco.ignorar_autores`
· Evidência: `eh_bot = autor in ignorar`, em que `autor` é o nome e `ignorar` = nomes de bots (`membros_do_chat`) + `["Triagem de Documentos Bot"]`. Se `/chats/{chat}/users` falhar, retorna `set()` (linha 163) e só sobra o nome do config.
· Impacto: se o bot for renomeado, ou um conector ou outro bot mandar um traço, ele recebe resposta. Os textos do bot não são traço puro, então o risco de loop é baixo. Uma pessoa com o mesmo nome do bot seria ignorada.
· Correção: comparar `autor_id` (já existe nas mensagens da fase 1, triagem_grupo.py:1934) com `bot_id` e com uma lista `ignorar_ids`.
· Confiança: confirmado no código.

### [PT-04] Bot do traço: o disparo da esteira descarta stdout e stderr
· **BAIXA** · pergunta_traco.py:590-593
· Evidência: `subprocess.Popen([... "triagem_grupo.py", "--dialog-id", chat], stdout=DEVNULL, stderr=DEVNULL)` a cada traço, ao mesmo tempo que o ciclo periódico.
· Impacto: erros desse ciclo não aparecem em log nenhum. Gera mais chamadas à API (429) e disputa pela trava do estado.
· Correção: mandar a saída para `pergunta-disparo.log` e usar a mesma trava de execução do ciclo periódico.
· Confiança: confirmado no código.

### [TG-01] Telegram: a lista de chats permitidos lida pelo código não é a que está no config
· **MÉDIA** · telegram_regras.py:45-48 · telegram_grupo.py:414-416 e 166 · config.json `telegram.chats_permitidos`
· Evidência: o config grava a lista em `telegram.chats_permitidos`. `chat_permitido` lê `cfg.get("chats_permitidos")` no **nível de cima** (o `cfg` é o `carregar_config()` mesclado). Os testes passam a chave no nível de cima (test_telegram_regras.py:25), o que esconde o problema.
· Impacto: hoje qualquer pessoa que abrir o `@documentosexemploBot` injeta arquivos em `estado.json`/`lotes/`, e eles seguem para a ponte e o painel. Pode ser intencional para receber de clientes, mas o dono **não consegue fechar** o bot pelo config: preencher a lista não tem efeito.
· Correção: `cfg.get("chats_permitidos") or (cfg.get("telegram") or {}).get("chats_permitidos")` e um teste com o formato real do config.
· Confiança: confirmado no código.

### [TG-02] Telegram: a memória de lotes recria lotes apagados de propósito e cresce sem limite
· **MÉDIA** · telegram_grupo.py:321-356, 463
· Evidência: `lotes_memoria` guarda o registro **completo** de todo lote já visto, para sempre. `reforcar_lotes_salvos` roda em toda volta do loop e **recria** qualquer lote que não esteja no estado.
· Impacto: a limpeza recomendada no CONTEXTO (§10.3, marcar ou remover os 900 lotes vazios) vai contra isso: um lote do Telegram removido do estado volta no segundo seguinte, com o `status` antigo. O `telegram-estado.json` também cresce sem limite.
· Correção: recriar só lotes com menos de N horas que nunca chegaram ao estado (guardar `confirmado_no_estado=True` depois do primeiro `mesclar`). Limpeza deve ser feita por marcação (`IGNORADO`), nunca por remoção.
· Confiança: confirmado no código.

### [TG-03] Telegram: o "Recebido" é enviado antes de o lote ser gravado
· **BAIXA** · telegram_grupo.py:447-453
· Evidência: o bot responde (447-450) **antes** de `mesclar_no_estado` (451) e de `gravar_estado` (453). Se `atualizar_estado` estourar o tempo (120 s, estado_store.py:32) ou o disco encher, o processo cai depois de já ter respondido. O `processar` (436) também não tem `try`, então no `--loop` o processo morre.
· Impacto: a cliente recebe "Recebido — N arquivos" de um kit que não está no estado. Depois de reiniciar, o reprocessamento recupera o kit (o offset do Telegram não foi confirmado), mas a mensagem sai em dobro.
· Correção: gravar primeiro e responder depois. Pôr `try` em volta de `processar`/`mesclar` dentro do loop.
· Confiança: confirmado no código.

### [ID-09] Código de produção depende de um script manual que dá `SystemExit` na importação
· **BAIXA** · nomear_lotes_por_cpf.py:28, 69 · completar_cpf_por_nome.py:23 · lote_para_job.py:400-403
· Evidência: `WEBHOOK = _webhook()` no nível do módulo, com `raise SystemExit` e `sys.path.insert(0, "/opt/triagem-bitrix")`. `lote_para_job` (ponte) protege com `except Exception`, que não pega `SystemExit`. Se o config não tiver a URL, a montagem do job derruba o processo inteiro em vez de devolver `pasta=""`. Foi o que derrubou 2 dos 3 testes.
· Correção: levar `chamar`/`webhook()`/`formatar_cpf` para um módulo `bitrix_rest.py` com resolução preguiçosa (sem efeito na importação) que levante `RuntimeError`.
· Confiança: confirmado (reproduzido no pytest).

### [NR-01] `nome_regras`: casamento por nome é conservador por desenho e não trata partículas nem abreviações
· **BAIXA (informativo)** · nome_regras.py:142-217 · completar_cpf_por_nome.py:48-51, 68
· Evidência: a comparação é igualdade exata depois de tirar acentos e colapsar espaços. "MARIA DE SOUZA" ≠ "MARIA SOUZA", "M. APARECIDA" ≠ "MARIA APARECIDA", e um sobrenome a menos no CRM já impede o casamento. O `RE_PREPOSICAO_INICIO` (linha 34) recusa nomes que começam com "Da/De/Do" isolados, de propósito.
· Impacto: parte dos 639 SEM_CPF é falso negativo por grafia. Isso não deve ser afrouxado sem uma 2ª âncora (ver proposta), porque afrouxar o casamento por nome **aumenta** o risco de [ID-02].
· Confiança: confirmado no código.

---

## Proposta segura para reduzir os 639 SEM_CPF (sem perder o conservadorismo)

Nessa ordem; cada passo só grava com **duas âncoras independentes**:

1. **Paginação completa + 2ª âncora obrigatória** (corrige [ID-02]): o candidato do CRM (nome exato, único, DV válido) só é aceito se **um** destes casar: (a) um telefone da ficha (`parse_ficha().telefones`) igual a `PHONE` do contato, comparando os últimos 8-9 dígitos; (b) o autor da mensagem do lote (o mesmo `autor_id` aparece num lote anterior que já tem esse CPF no `estado.json`); (c) o contato tem negócio aberto criado há menos de 90 dias.
2. **Busca por telefone sozinha** (nova e barata): `crm.contact.list?filter[PHONE]=<fone>` → 1 contato com CPF válido **e** nome com ≥2 tokens em comum com o lote → preenche. Cobre ficha sem CPF, mas com telefone (o formato da ficha pede telefone).
3. **Mesmo autor, mesmo dia**: lote SEM_CPF do mesmo `autor_id` até X horas depois de um lote OK cujo nome casa → **sugere** (vai para revisão com o CPF pré-preenchido, não grava sozinho).
4. **OCR do RG/CNH do lote** (o classificador do painel já lê documento): CPF extraído com DV válido **e** nome do documento casando com o nome da ficha → preenche, com `cpf_origem="ocr-documento"`. É a fonte mais confiável, porque o documento é da própria pessoa, mas custa uma chamada de IA. Usar só nos lotes SEM_CPF com arquivos.
5. Sem 2 âncoras → continua em revisão, mas com o **candidato sugerido** gravado (`cpf_sugerido`, `motivo`), para a revisão manual virar um clique em vez de uma busca.

Em todos os casos: aplicar com `atualizar_estado`, remover só o motivo "sem CPF" ([ID-01]), usar cache negativo com TTL ([ID-04]) e registrar `cpf_origem` para a pintura e o painel tratarem com cautela.

---

## Top 5 da frente (prioridade)

1. **[ID-01]** `completar_cpf_por_nome` força `pronto=True` e apaga todos os motivos de revisão: libera lote incompleto, misto ou duplicado para a ponte.
2. **[ID-02]** "Exatamente 1 contato" vê só a 1ª página de 50 (LIKE no 1º nome): um homônimo pode passar o **CPF de outra pessoa**. Exigir paginação e uma 2ª âncora (telefone/autor).
3. **[CE-01]/[CE-03]** Certidões: falha marca a mensagem como processada (perda silenciosa), uma queda no meio duplica tarefas, e a busca por nome pega o 1º contato e o 1º negócio, com o nome da criança.
4. **[PT-01]** Bot do traço: timeout depois do envio ou disco cheio fazem reenvio a cada 8 s, levando ao `ERROR_LOOP_DETECTED` que cala o bot em todos os chats.
5. **[ID-03]/[ID-04]** `completar_cpf` grava o estado sem trava, por minutos, e martela o Bitrix a cada 10 min sem cache (perde gravações da ponte e do Telegram e piora o bloqueio).

(Logo depois: [PL-01], a linha livre da planilha sem conferir na gravação, e [CE-02], foto de certidão que nunca é lida pela IA.)

## O que NÃO deu para verificar sem o servidor

- Quantos dos 639 SEM_CPF têm nome válido e arquivos (o volume real de chamadas de [ID-04]) e quantos lotes já foram liberados por `cpf_origem="bitrix-nome"` com outro motivo de revisão apagado ([ID-01]).
- As unidades systemd reais: se `pergunta_traco` roda em `--loop` **e** pelo timer ao mesmo tempo ([PT-02]), a cadência e o `TimeoutStartSec` do `pintar-linha-triagem` e do `triagem-lote-fila`, e se `triagem-lote-fila` chama exatamente o `pipeline.sh` (a cópia tem `set -u` sem `-e`/`pipefail`, e o `| tail` esconde o código de saída de cada passo).
- Se o `app2.planilha` do painel cria linha na mesma "próxima linha livre" ([PL-01]) e o volume de `pintadas.json`.
- A forma exata da resposta de `ia.classificarDocumento` para certidão (`nome` vs `nome_mae`) ([CE-03]).
- Conteúdo de `estado-certidoes.json` (tarefas duplicadas já criadas?) e de `telegram-estado.json` (tamanho de `lotes_memoria`).
- Permissões de `/tmp` e se outros usuários locais conseguem ver o `ps` (exposição do webhook em [CE-05]).
