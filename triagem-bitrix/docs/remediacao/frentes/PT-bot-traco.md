# Frente PT — bot do traço (100003) e vigias — onda 1

Status: **CONCLUÍDO** (sem commit; orquestrador comita).

## Arquivos
Alterados: `pergunta_traco.py`, `pergunta-rodar.sh`, `vigia-bot-triagem.py`, `vigia_bot.sh`,
`esperar_bot_liberar.py`, `testes/test_vigia_bot_triagem.py`.
Criado: `testes/test_pergunta_traco_envio.py` (31 testes).
Não tocados: `testes/test_envio_bot_bloqueio.py` (passa sem mudança), `testes/test_vigia_bot_transitorio.py`
(passa sem mudança), `testes/test_telegram_respostas.py` / `test_telegram_batidas.py` (importam `telegram_grupo`, não `pergunta_traco`).

## Evidência
- Testes da frente: `.venv/bin/python -m pytest -q testes/test_pergunta_traco_envio.py testes/test_envio_bot_bloqueio.py testes/test_vigia_bot_triagem.py testes/test_vigia_bot_transitorio.py` → **59 passed**.
- Python 3.9.6 (`/usr/bin/python3 -m unittest test_pergunta_traco_envio test_vigia_bot_triagem test_envio_bot_bloqueio`, em `testes/`) → **45 tests OK**.
- Reprodução no código ORIGINAL (cópia em scratch com `git show HEAD:<arquivo>` dos 5 arquivos da frente): **32 dos 39 testes novos/alterados falham**, entre eles:
  - timeout depois do POST → `AssertionError: 4 != 1` (4 envios em 4 rodadas);
  - disco cheio → `AssertionError: 5 != 0` (5 envios em 5 rodadas);
  - segunda instância → retorno `0` e rodada executada.
- Suíte inteira: `.venv/bin/python -m pytest -q` → **14 failed, 717 passed**. As 14 falhas não são desta frente (test_variaveis_contrato ×7, test_servico_lote ×3, test_nome_regras, test_pasta_fichinha, test_ponte_fila, test_telefones_fichinha), as mesmas 14 da linha de base.

## Achados

### PT-01 · corrigido · `pergunta_traco.py`
- `_req`: erro HTTP → `ErroHTTP` (subclasse de RuntimeError, mesmo texto `HTTP <n> em <url>: …`); falha de transporte (URLError, timeout, reset, IncompleteRead) → `ErroRede`. Antes ela escapava crua.
- `bot_fala` classifica (`_tipo_falha`: código VibeCode → `rede_bitrix.classificar` → status): LOOP → grava bloqueio e para; ACESSO → `ErroEnvioSemAcesso`; LIMITE (429/RATE_LIMITED, recusa sem execução) → repete com texto variado e, esgotado, `ErroEnvioAdiado`; rede/5xx → **`ErroEnvioIncerto`** (nunca repete); outro 4xx → `ErroEnvioRecusado`.
- Máquina de envio persistida em `pergunta-traco-estado.json → envios["<chat>:<traço>"]`: `PENDENTE → CONFIRMADO | ENVIO_INCERTO | ADIADO | FALHA`. **PENDENTE e a transição do lote (aberto/hist/ultimo_traco) são gravados ANTES do POST.** PENDENTE encontrado numa rodada seguinte = ENVIO_INCERTO (o processo caiu ou a gravação falhou depois do envio).
- Reconciliação (`_reconciliar_envios`, toda rodada ativa): incerto vira CONFIRMADO se aparece mensagem do bot (`autor_id == bot`) entre o traço e o próximo traço de pessoa, ou com o `id_msg` devolvido. Só há **reenvio** (1 vez, `MAX_REENVIOS_INCERTO`, texto variado) se: a leitura alcança o traço, a mensagem NÃO está lá, já passaram `JANELA_REENVIO_INCERTO_S` (600 s) e não há bloqueio. Depois disso, ou quando o traço passa da janela de 45 min → FALHA.
- ADIADO (bloqueio ativo / 429 esgotado) é tentado de novo fora do bloqueio, no máximo 3 vezes, espaçado ≥ 60 s, e só dentro da janela de 45 min.
- `salvar` atômico (tmp único 0600 + fsync). Falha → `ErroGravacaoEstado` + pausa crescente em memória (60 s → 15 min). Durante a pausa a rodada não lê nem envia. Sem PENDENTE gravado, nada é enviado.
- ERROR_LOOP_DETECTED: `bot-bloqueio.json` mantém o formato, e o bloqueio vale **também em memória** se o arquivo não gravar.
- ACCESS_DENIED: FALHA definitiva + `est["sem_acesso"][chat]`. Nenhum envio a esse chat por `PAUSA_SEM_ACESSO_S` (1 h), nem para traço novo.
- `emitir_resumo_desfechos`: envio incerto não fica na fila (sem reenvio cego). Gravação atômica + trava `desfechos-pendentes.json.lock`; registros que chegam durante o envio são preservados.
- Testes: `TestEnvioIncerto` (5), `TestFalhaGravacao` (2), `TestBloqueio` (3), `TestReconciliacaoLimites` (2).

### PT-02 · corrigido · `pergunta_traco.py`, `pergunta-rodar.sh`
- `trava_de_instancia()` usa `fcntl.flock` em `caminhos.em_base("pergunta-traco.lock")` (antes `<estado>.run.lock`, só no `--loop`). Vale para rodada única, `--loop`, `--testar/--para` e `--limpar`, e é tomada antes de qualquer leitura ou envio. Ocupada → **sai 75** (antes `0` no loop; a rodada única nem verificava).
- Exceção consciente: `--registrar-desfecho` e `--resumo` usam a trava própria do arquivo de desfechos. São chamados com o bot no ar e a trava do bot os barraria para sempre.
- `pergunta-rodar.sh`: `set -euo pipefail`, `BASE=${TRIAGEM_BASE:-/opt/triagem-bitrix}`, `exec python … >> $LOG 2>&1` (código real ao systemd). **Sem `flock` no shell de propósito**: um flock do shell no mesmo arquivo bloquearia o do Python.
- Testes: `TestInstanciaUnica` (5), `TestShell.test_pergunta_rodar_propaga_codigo_do_python`.

### PT-03 · corrigido · `pergunta_traco.py`
- O autor é identificado por `autor_id` (`_e_do_bot`), contra o próprio bot, os usuários-bot do chat (`_BOT_IDS`, preenchido por `membros_do_chat`, mesma assinatura) e o novo `pergunta_traco.ignorar_ids` do config. O nome exibido só serve de fallback quando a mensagem não traz `autor_id`.
- A comparação "mesma pessoa" das respostas legadas (`perguntas`) também usa ID quando há.
- Logs sem dado pessoal: autor aparece como `autor_id=<n>`; o log do FECHA não leva mais o nome do cliente; exceções passam por `_mascarar`. O `hist` do estado mantém `autor` (compatibilidade) + `autor_id`.
- Testes: `TestAutorPorId` (3), `TestLogEDisparo.test_log_da_rodada_nao_traz_nome_da_pessoa`.

### PT-04 · corrigido · `pergunta_traco.py`
- O ciclo disparado grava stdout e stderr em `caminhos.em_base("pergunta-disparo.log")` (antes DEVNULL), com uma linha de cabeçalho por disparo, e recebe `TRIAGEM_DIR`/`TRIAGEM_BASE` no ambiente.
- A "mesma trava do ciclo periódico" **já existe**: `triagem_grupo.main` envolve o ciclo em `trava_de_ciclo()` (`ciclo.lock`). Não é preciso trava nova.
- Teste: `TestLogEDisparo.test_disparo_da_esteira_vai_para_log`.

### H1 (auditoria 03) · corrigido · `esperar_bot_liberar.py`
Reescrito:
- não lê a chave na importação; usa `seguranca.ler_segredo("vibecode_key")` com fallback para `<BASE>/vibecode.key` (antes `/root/.vibecode/api_key`);
- `--dialog` é obrigatório e um chat de clientes (`config.pergunta_traco.chats` + os 2 conhecidos) é recusado;
- pausa crescente de 60/300/900/3600 s, respeitando `bot-bloqueio.json`;
- ACCESS_DENIED para na hora;
- códigos de saída: 0 liberou, 1 bloqueado, 2 uso, 3 sem acesso, 4 sem chave.

A sonda continua sendo um ENVIO, porque o bloqueio só cai com um envio bem-sucedido; mas vai para o privado de quem opera. Testes: `TestEsperarBotLiberar` (6).

### H2 (auditoria 03) · corrigido · `vigia-bot-triagem.py`
- Só transfere se `targetApiKeyId` for igual ao id da chave local. Fontes do id, nesta ordem: `$VIGIA_API_KEY_ID`, `$TRIAGEM_VIBECODE_KEY_ID`, `<BASE>/vibecode.key.id`, `GET /v1/me` (campos `apiKeyId`/`keyId`/`apiKey.id`).
- Sem id confirmado, ou id diferente → alerta e exit 1, sem transferir.
- `CHAVE_FILE` = `caminhos.em_base("vibecode.key")`; a chave vem de `seguranca.ler_segredo` quando existir; mensagens mascaradas.
- Testes: 4 novos em `test_vigia_bot_triagem.py`. O teste antigo de recuperação agora define `VIGIA_API_KEY_ID`. O `setUp` passou a apontar `ARQ_ESTADO_VIGIA` para um tmp: antes a suíte gravava `.vigia-estado.json` real ao lado do script (06-testes).

### vigia_bot.sh · corrigido
- `set -euo pipefail`, `BASE` via `TRIAGEM_BASE`, `stat` GNU com fallback BSD.
- Diagnóstico em `$BASE/vigia-bot-ultimo.txt` em vez de `/tmp` (nome fixo em /tmp aceita symlink).
- Falha do `systemctl restart` → exit 1.
- Testes: `TestShell` (2, com `systemctl` falso no PATH).

## Mudanças de comportamento (atenção no deploy)
1. **Vigia: a auto-recuperação BOT_ACCESS_DENIED PARA até se configurar o id da chave.** Criar `/opt/triagem-bitrix/vibecode.key.id` (0600) ou `VIGIA_API_KEY_ID`. O formato de `GET /v1/me` não está na doc local: se ele trouxer `apiKeyId`, funciona sem configuração.
2. Rodada única ocupada sai **75**, e o `--loop` duplicado também (antes 0). Unidade com `Restart=always` vai reiniciar em laço se houver dois serviços: escolher UM modo (timer `pergunta-rodar.sh` **ou** serviço `--loop 8`).
3. A transição do lote (ABRE/FECHA) avança uma vez por traço mesmo se o aviso não sair (bloqueio/incerto/falha). O disparo da esteira acontece sempre que o traço é tratado (antes só com envio OK).
4. Aviso incerto pode **não** chegar ao grupo (preferimos perder um aviso a mandar 300). Fica `FALHA`/`ENVIO_INCERTO` em `envios`, e `saude-bot.json` mostra "envio incerto".
5. `--testar` e `--limpar` agora exigem que o bot não esteja rodando (saem 75).
6. `esperar_bot_liberar.py` exige `--dialog`.

## Fallbacks a remover (orquestrador)
- `pergunta_traco.py`: `_mascarar_local`/`_RX_MASCARA` (preferir `seguranca.mascarar`) e o ramo por status HTTP em `_tipo_falha`, depois que `rede_bitrix.classificar` estiver estável. Os imports são tardios e já preferem os módulos, então cada frente testa sozinha.
- `vigia-bot-triagem.py` e `esperar_bot_liberar.py`: fallback de `_mascarar` e de leitura direta de `vibecode.key`.
- Conferir que `rede_bitrix.classificar(status, corpo, exc)` devolve `Erro.classe` com `.name` em {LOOP, ACESSO, RATE_LIMIT, OPERATING, REDE, …} (contrato). Códigos próprios do VibeCode (`RATE_LIMITED`, `BITRIX_ACCESS_DENIED`, `ERROR_LOOP_DETECTED`) são tratados antes, localmente.

## Pendências para outras frentes
- `triagem_grupo.py` (dono único): `BASE_DIR` lê `TRIAGEM_DIR`, não `TRIAGEM_BASE`/`caminhos.BASE`. O disparo já passa os dois no ambiente; alinhar ao contrato.
- Frente S: `seguranca.ler_segredo("vibecode_key")` deve cobrir o legado `/root/.vibecode/api_key` (era usado por `esperar_bot_liberar.py`; agora o fallback é `<BASE>/vibecode.key`).
- Logrotate de `pergunta-disparo.log` (novo), junto de `pergunta.log`, com `copytruncate` (O3).

## Riscos / o que só o servidor confirma
- Quais unidades existem (timer do `.sh` e/ou serviço `--loop`) e o `Restart=` delas (item 2 acima).
- `vigia_bot.sh` depende do log crescer: durante a pausa de gravação (disco cheio) o bot não escreve. O vigia reinicia o serviço a cada 3 min, o que zera a pausa e o bloqueio em memória. Pior caso com disco cheio e `bot-bloqueio.json` sem gravar: **1** chamada por reinício (~3 min), não por rodada de 8 s. Sugestão já da auditoria: `WatchdogSec` + `sd_notify`.
- A reconciliação usa a leitura de 2 páginas (`buscar_mensagens_api`). Traço que sai da janela lida nunca é reenviado (vira FALHA), por desenho.
- Formato da resposta de `POST /v1/bots/{id}/messages`: `id_msg` só é guardado se vier como `data.id`/`result`/`id` numérico. Sem ele, a confirmação de incerto é por `autor_id`.
