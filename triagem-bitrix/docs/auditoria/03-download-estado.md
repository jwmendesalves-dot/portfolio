# 03 — Download, resiliência, persistência e concorrência

Código auditado: `~/Downloads/triagem-codigo-20261109-1552` (cópia de `/opt/triagem-bitrix`), somente leitura.
Escopo: `download_svc.py`, `baixar_pendentes.py`, `circuit_breaker.py`, `estado_store.py`, `telegram_grupo.py` (download), `checar_midias.py`, `reconciliar_*.py`, `esperar_bot_liberar.py`, `pipeline.sh`, `vigia_bot.sh`, `vigia-bot-triagem.py` + trechos de `triagem_grupo.py` que implementam o disjuntor de download e o ciclo (`circuit-download.json` mora lá, não em `circuit_breaker.py`).

## Testes executados

| Arquivo | Resultado |
|---|---|
| test_download_svc | 8 passed |
| test_circuit_breaker | 9 passed |
| test_circuit_breaker_integracao | 3 passed |
| test_estado_store | 5 passed |
| test_telegram_download | 6 passed |
| test_429_operating | 3 passed |
| test_guarda_dns | 2 passed |
| test_vigia_bot_transitorio / test_vigia_bot_triagem / test_envio_bot_bloqueio | **erro de coleta na cópia** (caminho fixo `/opt/triagem-bitrix/...` em `testes/test_vigia_bot_transitorio.py:14`, `test_vigia_bot_triagem.py:15`, e `pergunta_traco.py` em test_envio_bot_bloqueio). Rodando uma cópia dos testes no scratch com o caminho trocado: **24 passed**. |

Os testes passam, mas nenhum cobre os defeitos abaixo: `.parcial` órfão, colisão de nome, fila sem dedupe, escrita sem trava pelos reconciliadores, fallback bloqueado pelo disjuntor.

---

## A) Concorrência no estado.json e nas filas

Mapa de quem grava `estado.json`:

| Gravador | Trava (`estado_store`)? | fsync? | tmp |
|---|---|---|---|
| `triagem_grupo.ciclo` (`:2887`) | sim | sim | `.tmp.<pid>` |
| `telegram_grupo.mesclar_no_estado` / `reforcar_lotes_salvos` (`:315`, `:352`) | sim | sim | `.tmp.<pid>` |
| `lote_para_fila.marcar_duplicatas_no_estado` (`:151`) | sim | sim | `.tmp.<pid>` |
| `reconciliar_por_file_ids.py` (`:121`) | sim | sim | `.tmp.<pid>` |
| **`reconciliar_estado.py`** (`:36`→`:90`) | **não** | não | `estado.json.tmp` (nome fixo) |
| **`reconciliar_arquivos_disco.py`** (`:42`→`:115`) | **não** | não | nome fixo |
| **`completar_cpf_por_nome.py`** (`:88`→`:147`) | **não** | não | nome fixo |
| `nomear_lotes_por_cpf.py` (`:142`→`:212`), `backfill_nome_lotes.py` (`:55`→`:92`), `reparo_josilene.py` | não | não | nome fixo |
| `triagem_grupo.salvar_estado` (`:2500`) | não (função ainda existe, hoje sem chamador) | não | nome fixo |

Leitores sem trava (seguro, porque a troca é atômica via `os.replace`): `baixar_pendentes`, `lote_para_fila.carregar_estado`, `pintar_linha`, `monitor_lotes`, `checar_midias`, `avisar-primeiro`.

### [A1] Três gravadores do pipeline de 10 min fazem ler-modificar-gravar no estado.json SEM trava (atualização perdida)
- **Severidade:** CRÍTICA
- **Onde:** `reconciliar_estado.py:36-90`, `completar_cpf_por_nome.py:88-147`, `reconciliar_arquivos_disco.py:42-115`
- **Evidência:** `with open(ESTADO) as fh: estado = json.load(fh)` … (loop com `sha256_de` de cada arquivo / consulta ao CRM por nome) … `tmp = ESTADO + ".tmp"; json.dump(...); os.replace(tmp, ESTADO)`. Nenhum desses arquivos importa `estado_store`.
- **Impacto:** é exatamente o bug que `estado_store.py:5-14` descreve e diz ter corrigido ("6 dos 7 lotes do Telegram sumiram"), só que reaberto por esses scripts. Entre a leitura e a gravação, `reconciliar_estado` calcula sha256 de milhares de arquivos (16.138 no total) e `completar_cpf_por_nome` faz uma consulta HTTP ao Bitrix por lote SEM_CPF (639). Essa janela vai de segundos a minutos. Nesse intervalo podem gravar o ciclo do triagem_grupo, o telegram_grupo (loop contínuo) e a ponte de 5 min (`marcar_duplicatas_no_estado`). Quem grava por último apaga o resto: lote novo do Telegram, marcação `descartado_duplicata` (a ponte volta a "ver" o kit e realerta) ou CPF completado. O `reforcar_lotes_salvos` do Telegram mascara parte da perda, mas só para lotes do Telegram.
- **Correção:** trocar o par `json.load`/`os.replace` por `estado_store.atualizar_estado(mutar)` nos três scripts. Calcular o sha256 e consultar o CRM **fora** da trava, montando um dicionário `{lote_id: {indice: meta}}`, e aplicar dentro do mutador só o que ainda está `baixado=False` no estado relido. Assim a trava dura milissegundos.
- **Confiança:** confirmado no código.

### [A2] O tmp com nome fixo `estado.json.tmp`, compartilhado por vários processos, pode gerar JSON corrompido. Com ele corrompido, o leitor zera o estado
- **Severidade:** ALTA
- **Onde:** tmp fixo em `reconciliar_estado.py:87`, `reconciliar_arquivos_disco.py:112`, `completar_cpf_por_nome.py:144`, `nomear_lotes_por_cpf.py:209`; leitores tolerantes em `estado_store.py:39-44` (`except (OSError, ValueError): return {}`) e `triagem_grupo.py:2491-2497` (`estado.json corrompido … começando do zero` + `os.replace(ARQ_ESTADO, ".corrompido")`)
- **Evidência:** dois processos abrem o mesmo `estado.json.tmp` com `"w"`, as escritas se intercalam e o primeiro `os.replace` publica um arquivo misturado. Depois disso, `atualizar_estado` lê `{}`, aplica só o delta do ciclo e **grava por cima**: o estado de 15 MB passa a ter só os lotes do ciclo.
- **Impacto:** perda total do estado (2.722 lotes, marcações de duplicata, cursores). Os backups `.bak-*` existem por acaso, porque o `completar_cpf` e o `reconciliar_arquivos_disco` copiam antes de gravar.
- **Correção:** (1) A1 resolve a colisão. (2) Em `estado_store.ler_estado`, diferenciar *ausente* (devolve `{}`) de *corrompido* (levanta exceção, para `atualizar_estado` **não gravar** e alertar). (3) Em `triagem_grupo.carregar_estado:2491`, não "começar do zero": abortar o ciclo.
- **Confiança:** a corrupção por tmp compartilhado só ocorre se dois desses scripts rodarem ao mesmo tempo (hipótese, depende das units). O "começar do zero" e o `{}`-e-grava estão confirmados no código.

### [A3] O ciclo do triagem_grupo substitui o registro INTEIRO do lote e apaga campos gravados por outros processos
- **Severidade:** ALTA
- **Onde:** `triagem_grupo.py:2812-2814` (`reg = serializar_lote(lote, arquivos)` → `lotes_criados[...] = reg`) e `:2870` (`fresco["lotes"].update(lotes_criados)`); `serializar_lote` `:2512-2553` monta um dicionário novo
- **Evidência:** a trava protege o arquivo, mas o mutador faz `update` por chave de lote e não mescla campos. Campos que não estão em `serializar_lote` (`status_anterior`, `descartado_em`, `lote_origem`, `descarte_motivo` de `lote_para_fila.py:134-139`; `cpf_origem="bitrix-nome"`, `cpf` completado em `completar_cpf_por_nome.py:54-60`) somem em todo lote que ainda está na janela de leitura do ciclo (até 600 mensagens por grupo, `MSG_PAGINAS_HISTORICO=12`).
- **Impacto:** o `completar_cpf` preenche o CPF, o ciclo seguinte apaga e o lote volta a SEM_CPF (o contador de 639 não cai). O status `descartado_duplicata` volta ao original e a ponte reavalia e realerta. Até 4 timers ficam brigando pelo mesmo campo.
- **Correção:** em `_aplicar`, mesclar: `fresco["lotes"][lid] = {**antigo, **reg}`, com uma lista de campos "donos externos" que o ciclo não sobrescreve quando já existem (`cpf` com `cpf_origem`, `status` em `descartado_*`/`revisao_duplicata`/enfileirado, `lote_origem` etc.).
- **Confiança:** confirmado no código. A frequência real precisa ser medida no servidor (comparar `cpf_origem` antes e depois de um ciclo).

### [A4] Dois `lote_para_fila --enviar` concorrentes: o flock existe só na ponte e o arquivo de entradas é fixo em /tmp
- **Severidade:** ALTA
- **Onde:** `ponte-rodar.sh:9-13` (flock em `/var/lock/triagem-bitrix-ponte.lock`); `pipeline.sh:15` chama `lote_para_fila.py --enviar --todos` sem esse lock; `lote_para_fila.py:361-367` grava `/tmp/_enfileirar.js` e `/tmp/_entradas.json` (caminhos fixos); `gravar_controle` `:93-97` com tmp fixo e sem trava
- **Impacto:** o timer lote-fila (10 min) e a ponte (5 min) podem rodar juntos. Um processo sobrescreve `/tmp/_entradas.json` do outro e enfileira os jobs do vizinho (job duplicado, ou lote marcado como enviado sem ter sido). O `controle` também sofre atualização perdida.
- **Correção:** fazer o pipeline de 10 min usar o mesmo `flock /var/lock/triagem-bitrix-ponte.lock` antes do passo 4 (ou mover o flock para dentro de `lote_para_fila.main`). Usar `tempfile.mkstemp()` para os arquivos do node.
- **Confiança:** confirmado no código para `pipeline.sh`. O wrapper real do timer `triagem-lote-fila` não está na cópia (hipótese de que reproduz o pipeline.sh).

### [A5] download_fila.json: sem trava entre processos, sem fsync e sem dedupe de file_id
- **Severidade:** MÉDIA
- **Onde:** `download_svc.py:179-184` (`salvar` com tmp fixo e sem fsync), `:186-197` (`enfileirar` faz `append` sem checar se o `file_id`/destino já está na fila), `triagem_grupo.py:2286-2287` (cria `Fila` nova a cada falha)
- **Impacto:** a cada ciclo em que um arquivo recente falha, um novo item idêntico entra na fila, que cresce sem limite (nunca há poda de `baixado`/`falha`). Rodar `download_svc.py --processar` à mão em paralelo ao ciclo perde itens.
- **Correção:** dedupe em `enfileirar` (se já existe item `pendente` com o mesmo `file_id`, retornar esse item), `fsync` + tmp com pid + flock em `salvar`, e poda de `baixado` com mais de 7 dias.
- **Confiança:** confirmado no código (o crescimento depende da taxa de falha).

---

## B) Disjuntores (circuit breaker)

Há **dois** disjuntores diferentes:
1. `circuit_breaker.py` → `circuit_breaker.json`: só para a **leitura** (`leitura.api`/`leitura.webhook`), com estados closed/open/half_open. A lógica está correta: `aberto_ate` é finito, `_normalizar` passa para half_open depois do prazo, sucesso fecha e falha reabre. Não fica aberto para sempre.
2. `triagem_grupo.py:108-131` → **`circuit-download.json`**: é o do download, citado no CONTEXTO. Não tem estados: guarda só `{"aberto_em": ts}` e considera "aberto" durante `CIRCUIT_PAUSA_SEG=180` s.

### [B1] O disjuntor de download do Bitrix também bloqueia o fallback VibeCode
- **Severidade:** ALTA
- **Onde:** `triagem_grupo.py:2325-2327` (`baixar_file_id_api`: `if _circuit_download_aberto(): meta["erro"] = "...pausado..."; return meta`), antes dos caminhos 1 e 2 da VibeCode (`:2432-2453`)
- **Impacto:** o bloqueio é do **portal Bitrix** (`disk.file.get`), mas o retorno antecipado também impede `GET vibecode/v1/files/{id}/download`, que não passa pelo rate limit do webhook. Os **78 arquivos "pausados pelo circuit-breaker"** nunca tentam a fonte que estaria livre. Também não entram na fila de retry, porque `meta["caminho"]` fica `None` e `_enfileirar_retry` sai em `:2283`.
- **Correção:** mover a checagem para dentro do bloco `if webhook:` (`:2415`), como `if webhook and not _circuit_download_aberto():`, deixando os passos VibeCode sempre disponíveis. O mesmo vale para `info_disk` em `:2336`.
- **Confiança:** confirmado no código.

### [B2] O disjuntor é aberto por QUALQUER chamada ao Bitrix, inclusive pela leitura de mensagens, e pausa os downloads
- **Severidade:** MÉDIA
- **Onde:** `triagem_grupo.py:323-336`: `http_json` (usado por leitura, `profile.json` e `disk.file.get`) chama `_registrar_bloqueio_download()` em `QUERY_LIMIT_EXCEEDED`/`LOOP_DETECTED`
- **Impacto:** com `fonte_leitura: "webhook"` (CONTEXTO §3), o rate limit da leitura `im.dialog.messages.get` pausa os downloads, e vice-versa.
- **Correção:** passar um parâmetro `abre_disjuntor_download=True` só nas chamadas `disk.file.get`.
- **Confiança:** confirmado no código.

### [B3] "Aberto desde 13:06": o arquivo nunca é fechado nem limpo, e quem mais martela o portal ignora o disjuntor
- **Severidade:** ALTA
- **Onde:** `triagem_grupo.py:113-131` (sem `registrar_sucesso`/fechamento; o arquivo guarda o último `aberto_em` para sempre); `baixar_pendentes.py:123-142` (**não consulta** `circuit-download.json`, 3 tentativas por arquivo com 0,55 s + 1,5/3 s); `download_svc.processar_fila` (`:259-270`, chamado em `triagem_grupo.py:2900`) também não consulta.
- **Evidência:** o código diz que "martelar com retry MANTÉM o bloqueio vivo" (`triagem_grupo.py:103-107`). Mesmo assim, a cada 10 min o `baixar_pendentes` dispara `disk.file.get` para **todos** os 453 pendentes ×3 tentativas, a ~1,8 req/s (`RATE=0.55`). O próprio `triagem_grupo` define 1 req/s como teto (`:239`), e os dois processos somados passam disso. Cada ciclo do triagem que encontra o portal bloqueado regrava `aberto_em`.
- **Impacto:** (a) hipótese provável para o "aberto desde 09/11 13:06": o bloqueio do portal fica vivo por causa do martelo do `baixar_pendentes`, e o ciclo reabre a pausa em toda rodada. (b) Outra possibilidade: o arquivo só ficou velho (180 s já passaram, o disjuntor está fechado de fato) e o "78 pausados" é rótulo antigo no estado. Os metas `erro="...pausado..."` de lotes fora da janela de leitura nunca são reescritos.
- **Correção:** (1) `baixar_pendentes` deve importar `_circuit_download_aberto` (ou ler o JSON) e **sair** se estiver aberto. Ao receber `QUERY_LIMIT_EXCEEDED`/`LOOP_DETECTED`, deve registrar o bloqueio e parar a rodada, em vez de tentar o próximo arquivo. (2) Aplicar a mesma checagem em `processar_fila`. (3) `RATE` do `baixar_pendentes` ≥ 1,0 s. (4) Gravar `{"aberto_em", "fechado_em", "motivo"}` e limpar no primeiro sucesso, para o painel não mostrar "aberto" antigo. (5) Reclassificar no estado os metas "pausado" (`erro_tipo: transitorio`).
- **Confiança:** a falta de checagem está confirmada no código. A causa do "aberto desde 13:06" é hipótese: conferir `cat circuit-download.json` contra `date +%s` e o journal do lote-fila.

### [B4] Half-open sem sonda única; `chamar` conta qualquer exceção como falha da fonte
- **Severidade:** BAIXA
- **Onde:** `circuit_breaker.py:73-77`, `:97-117`
- **Impacto:** em half_open, várias chamadas passam ao mesmo tempo. Uma exceção de parsing/bug nosso dentro de `buscar_mensagens_api` também abre o disjuntor. `_gravar` (`:150`) grava o snapshot em memória e pode sobrescrever a mesma chave gravada por outro processo (a trava protege só o arquivo, não o conteúdo).
- **Correção:** em half_open, marcar `sondando=True` persistido e liberar só uma chamada; contar falha apenas para `URLError`/`HTTPError`/`TimeoutError`.
- **Confiança:** confirmado no código. Impacto baixo, porque o ciclo é serializado por `trava_de_ciclo`.

---

## C) Áudio/vídeo como documento e os 3 que nunca baixam

### [C1] Nenhum filtro de tipo na entrada: áudio e vídeo viram anexo de lote (e a ponte aceita)
- **Severidade:** MÉDIA
- **Onde:** Bitrix: `triagem_grupo.py:1059-1069` (`file_ids_da_mensagem` pega todo `fileId`) e `:1150-1158` (`_mapa_arquivos` já tem `tipo=f["type"]`, mas ninguém filtra). Telegram: `telegram_regras.py:86-91` inclui `video`, `audio`, `voice`. Ponte: `lote_para_fila.py:63-73` mapeia mp4/mp3/wav como MIME válido.
- **Impacto:** 28 mp3, 21 mp4, 3 flac e 1 wav entram como documento (9 lotes). Isso infla `qtd_arquivos`, confunde a dedupe por sha256 e manda mídia ao worker do contrato.
- **Correção:** criar uma lista central `EXT_MIDIA = {.mp3,.flac,.wav,.ogg,.opus,.m4a,.mp4,.mov,.3gp,...}` + `tipo in ("audio","video","voz")`. Em `classificar_mensagem` (`:1113-1119`), separar os itens em `lote["midias"]` (fora de `file_ids`). Em `telegram_regras.arquivos_do_update`, marcar `tipo` e o chamador separar. Em `lote_para_fila`, recusar áudio/vídeo como documento. A mídia continua salva para auditoria, mas não vai para a fila.
- **Confiança:** confirmado no código.

### [C2] O Telegram não checa o limite de 20 MB antes do getFile; o erro não é permanente e volta a cada 10 min por outro caminho
- **Severidade:** ALTA (ruído contínuo + martelo no Bitrix)
- **Onde:** `telegram_grupo.py:131` (`getFile` sem olhar `arq["tamanho"]`, que já vem do update); `:145-146` (erro genérico, sem tipo); `baixar_pendentes.py:126-127` manda `{"id": "CQACAg..."}` (id do Telegram, não numérico) ao `disk.file.get` do Bitrix, 3 tentativas a cada rodada; `download_svc.py:114` `int(file_id)` → `ValueError` para id de bot.
- **Impacto:** os 3 arquivos de mais de 20 MB nunca baixam. O `baixar_pendentes` conta a pasta (`existentes < len(fids)`, `:101`), repete o lote em toda rodada e gera 3 erros por arquivo a cada 10 min. Essas 3 chamadas vão ao `disk.file.get` do portal, que já está sob rate limit (B3).
- **Correção:** (1) `telegram_grupo.baixar_arquivo_telegram`: `if int(arq.get("tamanho") or 0) > 20*1024*1024: meta.update(erro="excede 20 MB do Bot API", erro_tipo="permanente"); return meta`. (2) `baixar_pendentes`: pular `fid` não numérico (`if not str(fid).isdigit(): continue`), igual a `triagem_grupo._fid_int`, e pular metas com `erro_tipo == "permanente"`. (3) `download_svc.fonte_primaria/fallback`: id não numérico vira `FalhaDownload` permanente sem chamada de rede. (4) Limpeza dos 3 legados: marcar no estado `erro_tipo=permanente` e tirar de `file_ids` (ou mover para `midias`).
- **Confiança:** confirmado no código. Que os 3 casos sejam exatamente os ids `CQAC…` acima de 20 MB vem do CONTEXTO §3/§7.4, não verifiquei no estado.

---

## D) Retry, backoff, limite e classificação de erro

### [D1] baixar_pendentes não tem memória de tentativas nem distingue erro permanente de transitório
- **Severidade:** ALTA
- **Onde:** `baixar_pendentes.py:123-142` (contador só dentro da rodada; `erros += 1` e esquece); `:82-103` (seleção de pendentes só pelo disco)
- **Impacto:** os **83 arquivos em 38 lotes com erro real** são retentados para sempre, 3× a cada 10 min (~36k chamadas/dia só neles). Não existe estado `falha_definitiva`, então eles nunca saem do relatório como "precisa de humano". `disk.file.get` com `ACCESS_DENIED`/`NOT_FOUND` (arquivo apagado no portal) é permanente e recebe o mesmo tratamento que um 503.
- **Correção:** guardar em `download_fila.json` (ou num `download_tentativas.json`) `{file_id: {tentativas, ultimo_erro, erro_tipo, proximo_retry}}`. Classificar: HTTP 400/403/404, `ERROR_NOT_FOUND`, `ACCESS_DENIED` (sem LOOP), id não numérico, >20 MB = **permanente** (parar e listar no monitor). 429/503/timeout/URLError = **transitório**, com backoff em **minutos/horas** (10 min → 30 → 2 h → 6 h, teto 24 h). Para os 83, fazer um retry dirigido por lote (`baixar_pendentes.py --lote LOTE-…`) e então marcar o desfecho.
- **Confiança:** confirmado no código.

### [D2] O backoff do download_svc está em segundos, mas a fila só roda uma vez por ciclo; depois de 5 ciclos o item vira `falha` terminal
- **Severidade:** MÉDIA
- **Onde:** `download_svc.py:38-39` (`MAX_TENTATIVAS=5`, `BASE_DELAY=1.0` → 1, 2, 4, 8 s); `:250-254`; chamado só no fim do ciclo (`triagem_grupo.py:2892-2901`)
- **Impacto:** o backoff exponencial não tem efeito: as 5 tentativas cabem em 5 ciclos seguidos (minutos). Se o portal ficar bloqueado por ~1 h, o item passa a `falha` e **ninguém o retenta** (`pendentes()` só olha `estado == "pendente"`). Para lotes recentes, o ciclo seguinte reenfileira um item novo (A5), o que é o oposto do desejado.
- **Correção:** `BASE_DELAY=300`, teto de 6 h, `MAX_TENTATIVAS` maior para erro transitório e 1 para permanente (usar a mesma classificação de D1). Respeitar o disjuntor (B3).
- **Confiança:** confirmado no código.

### [D3] `baixar_arquivo` retenta a MESMA URL assinada e não renova o link
- **Severidade:** BAIXA
- **Onde:** `triagem_grupo.py:358-391` (5 tentativas na mesma `url`); chamado em `:2253` e `:2427` com a URL obtida uma vez
- **Impacto:** se o 403 vier de link expirado (ou do token de uso único), as 5 tentativas (~15 s de backoff + até 18 s de timeout cada) são desperdiçadas e o arquivo vai para "erro real". O `baixar_pendentes` e o `download_svc` fazem o certo (renovam o `disk.file.get` a cada tentativa).
- **Correção:** em `baixar_arquivo`, não retentar HTTP 401/403/404 (levantar logo) e, no chamador, refazer o `disk.file.get` uma vez.
- **Confiança:** confirmado no código. Que o portal invalide o link no 1º uso é hipótese.

---

## E) Integridade do arquivo em disco

### [E1] `.parcial` órfão faz o baixar_pendentes pular o arquivo PARA SEMPRE
- **Severidade:** ALTA
- **Onde:** `baixar_pendentes.py:53-67` (`baixar` cria `NN_nome.parcial`; em exceção, inclusive `"download vazio"` em `:64-65`, **não remove** o parcial) + `:119-122` (`if any(n.startswith(pref) for n in os.listdir(pasta)): pulados += 1; continue`; o `.parcial` começa com `NN_`)
- **Evidência:** o filtro de `.parcial` existe só na contagem de `existentes` (`:100`), não na checagem por índice.
- **Impacto:** se o download cair no meio da transferência (timeout de leitura, reset), ou o portal responder 0 byte, o índice fica marcado "já existe" e nunca mais é baixado. O reconciliador ignora `.parcial` (`reconciliar_estado.py:54`), então o estado fica `baixado=False` e o arquivo nunca chega. Candidato forte a parte dos 370 "só adiados" e dos 83 com erro.
- **Correção:** em `baixar`, envolver em `try/except` com `os.remove(parcial)`. Em `:120`, ignorar `n.endswith((".parcial", ".part", ".tmp"))` e arquivos de tamanho 0. Fazer uma varredura única: `find lotes -name '*.parcial' -o -size 0` e apagar.
- **Confiança:** confirmado no código. A quantidade de `.parcial` órfãos precisa ser contada no servidor.

### [E2] O download do Telegram grava direto no destino (sem .part) e reaproveita arquivo truncado como "baixado"
- **Severidade:** ALTA
- **Onde:** `telegram_grupo.py:136-137` (`open(destino,"wb")` + `fh.write(r.read())`); `:126-128` (`if os.path.exists(destino) and os.path.getsize(destino) > 0: baixado = reaproveitado = True`); `:141-142` (tamanho divergente vira só `aviso`, continua `baixado=True`)
- **Impacto:** uma queda no meio deixa um arquivo truncado com tamanho > 0. A rodada seguinte o trata como pronto, calcula o sha256 do pedaço e manda para a ponte e o worker: PDF/JPG corrompido no contrato. Como o sha256 do truncado é diferente, a dedupe também falha. `r.read()` e `hashlib.sha256(open().read())` carregam o arquivo inteiro na RAM.
- **Correção:** gravar em `destino + ".parcial"` em blocos, conferir `tamanho == arq["tamanho"]` (se diferente, `erro`, remover e não marcar baixado) e só então fazer `os.replace`. No reaproveitamento, exigir também `getsize == tamanho esperado`.
- **Confiança:** confirmado no código.

### [E3] Dois anexos com o mesmo nome no lote: o segundo "reaproveita" o primeiro e o documento some
- **Severidade:** ALTA
- **Onde:** `triagem_grupo.py:2241-2251` e `:2361-2372`: nome não ofuscado é salvo como `_nome_seguro(nome)` **sem prefixo de índice**; `if not force and os.path.exists(destino) and getsize>0: baixado=True; reaproveitado=True`
- **Impacto:** dois arquivos `CNH.pdf`, `documento.pdf` ou `image.jpg` no mesmo kit geram um único arquivo em disco. O segundo meta aponta para o primeiro (mesmo `caminho` e sha256) e o lote fica com documento faltando sem nenhum erro. A pasta parece completa e o "0 divergências estado↔disco" não pega o caso, porque o estado aponta para um arquivo que existe.
- **Correção:** sempre prefixar o índice (`f"{indice:02d}_{_nome_seguro(nome)}"`), como já fazem `baixar_pendentes` e o Telegram. Isso também unifica a convenção `NN_`, da qual os 3 reconciliadores dependem. Auditoria única: procurar lotes em que dois metas têm o mesmo `caminho`.
- **Confiança:** confirmado no código. A frequência é hipótese (nomes do WhatsApp costumam vir ofuscados, `nome_ofuscado` `:2160`).

### [E4] `.part` do download_svc não é filtrado pelos reconciliadores; não há checagem de tamanho, de 0 byte nem fsync
- **Severidade:** MÉDIA
- **Onde:** `download_svc.py:223-229` (`destino + ".part"`, sem fsync, sem `os.remove` em falha); `:232-246` (aceita `b""` e não compara com `SIZE`); filtros de sufixo só com `.parcial`: `baixar_pendentes.py:100`, `reconciliar_estado.py:54`, `reconciliar_por_file_ids.py:41`, `triagem_grupo.py:2389`; `_meta_do_disco` `:2196-2197` e `reconciliar_arquivos_disco.py:56,69` não filtram **nenhum** sufixo nem tamanho 0
- **Impacto:** um `NN_x.pdf.part` órfão (crash entre `write` e `replace`) ou um `.parcial` vira, nos reconciliadores, "arquivo do índice NN baixado", com sha256 do pedaço. Em queda de energia, `os.replace` sem fsync pode deixar o arquivo com 0 byte e mesmo assim nomeado.
- **Correção:** uma única função `arquivo_valido(nome, cam)` (sufixos `.part/.parcial/.tmp/.meta.json` excluídos, `getsize>0`) usada em todos os pontos acima. No `download_svc`: rejeitar 0 byte, comparar com `SIZE` do `disk.file.get` e fazer `fsync` antes do `replace`.
- **Confiança:** confirmado no código.

### [E5] reconciliar_por_file_ids associa arquivos sem índice aos file_ids pela ORDEM ALFABÉTICA e descarta metas de erro
- **Severidade:** MÉDIA
- **Onde:** `reconciliar_por_file_ids.py:100-116` (`nome = sem_indice.pop(0)` para posições sem `NN_`; `lote['arquivos'] = novos` substitui a lista inteira)
- **Impacto:** com os nomes reais do E3, o file_id 3 pode receber o arquivo do file_id 1. A lista nova não inclui os fids sem arquivo, então o meta com `erro` some e o lote parece ter `len(novos)` arquivos sem nenhum faltante registrado. Esse script roda no timer de 10 min (`--dias 7`, CONTEXTO §5).
- **Correção:** casar por `nome_original`/`nome_salvo` do meta existente (ou pelo `arquivos_index[fid].path`) antes de cair na ordem. Preservar os metas não resolvidos com `baixado=False`.
- **Confiança:** confirmado no código.

---

## F) URL assinada

- `baixar_file_id`/`baixar_file_id_api`: obtêm `DOWNLOAD_URL` e baixam na sequência, sem persistir a URL (`triagem_grupo.py:2253`, `:2427`). Correto. No `baixar_file_id_api`, o `info_disk` é obtido em `:2341` **antes** da checagem de reuso e da montagem de nome (milissegundos, sem risco).
- `baixar_pendentes` e `download_svc` renovam o `disk.file.get` a cada tentativa. Correto.
- Telegram: `file_path` vale 1 h e é usado na hora. Correto.
- Problema real: D3 (retentar link morto em `baixar_arquivo`).
- `download_svc.fonte_fallback` (`:112-117`) não segue o `downloadUrl` (caminho 2), só o `/download` direto. Sem impacto de expiração.

---

## G) Timeouts

### [G1] baixar_pendentes e download_svc não têm a guarda de DNS nem limite total; podem pendurar o timer de 10 min
- **Severidade:** MÉDIA
- **Onde:** `baixar_pendentes.py:28-32,49,57` (`urlopen(timeout=12)` + `socket.setdefaulttimeout`); `download_svc.py:40,102,108,116` (`TIMEOUT=30`); `telegram_grupo.py:54-61,88,136` (`getaddrinfo` sem prazo; 90/120 s)
- **Evidência:** `triagem_grupo.py:251-271` documenta que `urlopen(timeout=)` **não limita** o `getaddrinfo` (ciclo travado 28 min em 27/10) e criou `_abrir_com_teto`. Os outros três clientes HTTP não usam a guarda. O timeout é por operação de socket: um download que pinga 1 byte a cada 11 s nunca estoura.
- **Impacto:** um travamento do `baixar_pendentes` (passo 1 do pipeline) segura reconciliar, completar CPF e enfileirar: nenhum lote novo chega à fila enquanto durar. O `download_svc.processar_fila` roda dentro do ciclo, depois da gravação do estado, e pendura o processo segurando `trava_de_ciclo`.
- **Correção:** reutilizar `triagem_grupo._abrir_com_teto` (ou copiar) nos três. Adicionar um prazo total por arquivo (checar `time.monotonic()` no loop de `read`) e um orçamento por rodada no `baixar_pendentes` (por exemplo, 6 min, para caber nos 10 min). Na unit, `TimeoutStartSec=9min` (verificar a unit, que não está na cópia).
- **Confiança:** a ausência da guarda está confirmada no código. O travamento real é hipótese.

### [G2] Pool de 8 threads da guarda de DNS esgota com conexões penduradas
- **Severidade:** BAIXA
- **Onde:** `triagem_grupo.py:259-271` (`ThreadPoolExecutor(max_workers=8)`; em timeout, o futuro é abandonado, mas a thread continua ocupando o worker)
- **Impacto:** em processo longo (bot/loop), 8 DNS pendurados ocupam o pool. Daí em diante toda chamada espera na fila e estoura `futuro.result(timeout)`: todas as requisições falham até reiniciar. O `vigia_bot.sh` só reinicia o bot por log parado, não o serviço da triagem.
- **Correção:** contar os timeouts consecutivos da guarda e sair do processo (deixar o systemd reiniciar) quando chegarem a 8, ou criar um executor novo quando `_POOL_HTTP._work_queue.qsize()` crescer.
- **Confiança:** confirmado no código. Ocorrência em produção é hipótese.

---

## Outros (vigias e scripts auxiliares)

### [H1] esperar_bot_liberar.py manda mensagem REAL no grupo de clientes a cada 2 min como teste
- **Severidade:** BAIXA
- **Onde:** `esperar_bot_liberar.py:24-33` (`POST /v1/bots/100003/messages` em `chat100001` com "Bot da triagem reconectado (teste N)", até 20×)
- **Impacto:** é script de incidente pontual, mas rodá-lo de novo polui o grupo da equipe. Cada tentativa negada pode manter vivo o `ERROR_LOOP_DETECTED` que ele espera cair. Lê a chave na importação (`:20`), sem tratar erro.
- **Correção:** testar com uma chamada de leitura (`GET /bots/{id}/events` ou `im.dialog.get`) e só enviar mensagem quando liberar. Ou mover o script para `backups/`, já que é legado do incidente de 22/10.
- **Confiança:** confirmado no código.

### [H2] vigia-bot-triagem: transfere o bot para a chave que a própria resposta de erro indica
- **Severidade:** BAIXA
- **Onde:** `vigia-bot-triagem.py:188-200` (`targetApiKeyId` de `error.fix.body` → `POST /transfer`)
- **Impacto:** a ação automática confia no corpo da resposta. Se a resposta vier de proxy/MITM ou de bug do VibeCode, o bot é transferido para uma chave arbitrária. Comparar com o id da chave local reduziria o risco.
- **Correção:** só transferir se `targetApiKeyId` for o id da chave em `vibecode.key` (obtido via `/v1/me` ou equivalente).
- **Confiança:** confirmado no código. A exploração é hipótese.

- `vigia_bot.sh`: correto para o que propõe (reinicia por log parado). Não vigia o `triagem-lote-fila` nem o `baixar_pendentes`, que são os que podem travar (G1).
- `checar_midias.py:29`: lê o `estado.json` sem trava (seguro). Bate no VibeCode a cada execução (12 páginas × N grupos); sem problema de concorrência.
- Testes de vigia/envio presos a `/opt/triagem-bitrix` (seção Testes): não rodam fora do servidor nem em CI. Trocar por `os.path.join(os.path.dirname(__file__), "..", "vigia-bot-triagem.py")`.

---

## Top 5 da frente (ordem de prioridade)

1. **[A1]+[A2] Reconciliadores e `completar_cpf_por_nome` gravam o estado.json sem trava (tmp fixo e sem fsync), e o leitor zera o estado se o JSON corromper.** Risco de perda total ou parcial do estado a cada 10 min. Correção: `estado_store.atualizar_estado` + não gravar sobre estado ilegível.
2. **[E1]+[E2]+[E3] Integridade:** `.parcial` órfão bloqueia o índice para sempre; o Telegram reaproveita arquivo truncado; nome repetido no lote apaga o 2º documento em silêncio. Correção: limpar o parcial, filtrar sufixos e tamanho 0, gravar o Telegram em `.parcial` com checagem de tamanho, prefixar sempre `NN_`.
3. **[B1]+[B3] Disjuntor de download:** bloqueia o fallback VibeCode (78 "pausados" nunca tentam a fonte livre) e é ignorado pelo `baixar_pendentes`/`download_svc`, que martelam o portal a 1,8 req/s e provavelmente mantêm o bloqueio vivo. Correção: checar o disjuntor só no passo webhook; `baixar_pendentes` respeitar o disjuntor e parar no 1º `QUERY_LIMIT`; RATE ≥ 1 s.
4. **[D1]+[C2] Retry sem memória e sem erro permanente:** os 83 com erro real e os 3 acima de 20 MB (id `CQAC…` mandado ao `disk.file.get`) são retentados 3× a cada 10 min para sempre. Correção: registro de tentativas com `erro_tipo`, backoff em minutos/horas, pular id não numérico e mais de 20 MB, retry dirigido dos 83.
5. **[A3]+[A4] O ciclo sobrescreve o registro inteiro do lote** (apaga o CPF completado e as marcações de duplicata), e dois `lote_para_fila --enviar` podem rodar juntos (flock só na ponte; `/tmp/_entradas.json` fixo). Correção: mesclar campos no `_aplicar`; flock comum + `mkstemp`.

(Depois: [C1] filtro de mídia na entrada, [D2] backoff do `download_svc`, [A5] dedupe da fila, [G1] guarda de DNS e prazo total.)

## O que NÃO deu para verificar sem o servidor

- Conteúdo atual de `circuit-download.json` (se `aberto_em` = 13:06 está vencido, o que confirmaria "rótulo antigo", ou se é regravado a cada ciclo) e o journal do lote-fila mostrando `QUERY_LIMIT`/`LOOP_DETECTED`.
- As units systemd (`triagem-lote-fila`, `-ponte`, `-certidoes`, `pintar-linha`) e o wrapper real do lote-fila (não estão na cópia): se usam flock, `TimeoutStartSec` e qual reconciliador chamam com `--aplicar`. Também se `triagem_grupo` roda como loop ou oneshot.
- Quantidade de `*.parcial`, `*.part` e arquivos de 0 byte em `lotes/` (E1/E4); lotes com dois metas apontando para o mesmo `caminho` (E3).
- Se os 3 que nunca baixam são de fato ids do Telegram acima de 20 MB, e a distribuição de `erro` dos 83 (permanente vs transitório).
- Tamanho e itens repetidos de `download_fila.json` (A5/D2).
- Se já houve corrupção do estado (`estado.json.corrompido` presente?) e se A3 está apagando CPF completado por nome (comparar `cpf_origem` antes e depois de um ciclo).
- Os testes `test_vigia_bot_*` e `test_envio_bot_bloqueio` só passaram com o caminho reescrito numa cópia no scratch.
