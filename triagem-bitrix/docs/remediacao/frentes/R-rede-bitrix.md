# Frente R — rede_bitrix.py + circuit_breaker.py (onda 1, produtor)

Status: **CONCLUÍDO**.

Arquivos da frente (todos os que alterei/criei):
- `rede_bitrix.py` (novo)
- `circuit_breaker.py` (endurecido; API antiga preservada)
- `testes/test_rede_bitrix.py` (novo, 72 testes)
- `testes/test_circuit_breaker.py` (+11 testes; os 9 antigos intactos)
- `testes/test_circuit_breaker_integracao.py` (+1 teste de compatibilidade com arquivo legado)
- este relatório

Nenhuma chamada de rede; nenhum cliente HTTP criado (teste `test_modulo_nao_tem_cliente_http`).

## Evidência

```
.venv/bin/python -m pytest -q testes/test_rede_bitrix.py testes/test_circuit_breaker.py testes/test_circuit_breaker_integracao.py
96 passed in 2.02s            (5 rodadas seguidas: 95/95 antes do teste da fila, sem flakiness)

.venv/bin/python -m pytest -q          (suíte inteira, 09/11/2026)
14 failed, 661 passed in 5.80s
```
As 14 falhas são as mesmas da linha de base da onda 0 (287/301 → 14 falhando) e de outras frentes:
`test_nome_regras`, `test_pasta_fichinha`, `test_ponte_fila`, `test_servico_lote` (3), `test_telefones_fichinha`,
`test_variaveis_contrato` (7). Nenhuma em `rede_bitrix`/`circuit_breaker`.

RED (testes novos contra o `circuit_breaker.py` ORIGINAL, cópia no scratch):
```
9 failed, 11 passed  — falham no original:
  test_persiste_motivo_e_aberto_em, test_meia_abertura_libera_uma_unica_sonda_entre_instancias,
  test_sonda_abandonada_expira, test_falha_de_outra_instancia_nao_e_apagada_por_snapshot_velho (B4),
  test_nunca_aberto_para_sempre_com_prazo_absurdo_no_arquivo, test_abrir_imediato_limitado_e_finito,
  test_conta_falha_rejeitada_nao_conta_e_libera_sonda (B4), test_ultimo_erro_mascarado,
  test_uma_sonda_entre_processos_reais (4 processos spawn; original libera os 4)
```
Controle negativo do limitador: o teste multiprocesso, com cada processo usando o SEU arquivo (= throttle por
processo, como o legado `_RATE_MIN_SEG`), **falha** ("excedeu"); com o arquivo compartilhado passa.

## O que foi entregue (contrato CONTRATOS.md § rede_bitrix.py)

### classificar(status, corpo="", exc=None, *, retry_after, operating_reset_at, download, escrita, agora) -> Erro
Precedência: `FalhaBitrix` (erro já classificado) > falha local do downloader > **código `error` do corpo** >
status HTTP > tipo da exceção. Nunca ramifica por `error_description` (error-codes.md:96); o código vence o status
(batch erra com 200; 503 com `ACCESS_DENIED` é ACESSO). Corpo truncado/não-JSON: regex do campo `error`/`code` e,
em último caso, varredura de **códigos conhecidos** (LOOP antes de ACCESS_DENIED, pois a mensagem do loop cita
ACCESS_DENIED). Aceita `{"error":"X"}` (Bitrix) e `{"error":{"code":"X"}}` (VibeCode/REST 3).

| Entrada | Classe | repetir | permanente | outros |
|---|---|---|---|---|
| `QUERY_LIMIT_EXCEEDED` / 503 sem código | RATE_LIMIT | sim | não | espera 10 s (ou Retry-After), `pausa_rodada` |
| `OPERATION_TIME_LIMIT`, `..._QUERYTIMELIMITEXCEPTION` / 429 sem código | OPERATING | sim | não | espera = Retry-After > `operating_reset_at` (parâmetro da última resposta boa ou do corpo) > 60 s; teto 1 h |
| `ACCESS_DENIED`, `insufficient_scope`, `INVALID_CREDENTIALS`, `WRONG_AUTH_TYPE`, `NO_AUTH_FOUND`, `ERROR_METHOD_NOT_FOUND`, 401/403 | ACESSO | **nunca** | sim | `pausa_rodada` só p/ erro de sistema/auth (401 ou código de credencial); 403 de método = erro de 1 item |
| `ERROR_LOOP_DETECTED` | LOOP | não (sem loop curto) | não | espera 900 s (`BITRIX_PAUSA_LOOP_S`, 60–3600), abre circuito já |
| `OVERLOAD_LIMIT` | OVERLOAD | não | sim | abre circuito 1 h (sonda de hora em hora, nunca "para sempre") |
| 404/410, `*NOT_FOUND` (exceto METHOD) | NAO_ENCONTRADO | não | sim | não conta no circuito |
| timeout, `concurrent.futures.TimeoutError` (guarda DNS), `URLError`, `gaierror`, reset, `RemoteDisconnected`, `IncompleteRead`, SSL, OSError de rede, 408 | REDE | sim | não | `ambiguo` p/ timeout/reset; com `escrita=True` e ambíguo → **não repetir** (pode ter executado) |
| 401/403/404/410 com `download=True` | URL_EXPIRADA | sim (pedir DOWNLOAD_URL nova) | não | não conta no circuito |
| `ErroTamanho`, 413, `FILE_TOO_LARGE`, `*_OVERSIZE` | TAMANHO | não | sim | |
| `ErroTipo`, 415 | TIPO | não | sim | p/ id não numérico (CQAC…) mandado ao disk.file.get |
| `ErroIntegridade` | INTEGRIDADE | não | sim | 0 byte / tamanho ≠ SIZE / hash |
| 5xx, `INTERNAL_SERVER_ERROR` | SERVIDOR (acréscimo) | sim | não | ambíguo p/ escrita |
| 400 / código desconhecido em 200/400 (`ERROR_ARGUMENT`, batch) | INVALIDO (acréscimo) | não | sim | "repetir erro de método dá o mesmo resultado" |
| resto | DESCONHECIDO | sim (máx. 2) | não | |

`detalhe` sempre passa por `seguranca.mascarar` (import tardio) com fallback local mínimo
(`rest/<id>/<token>`, token Telegram, `token=/senha:/api_key`, CPF) — `circuit_breaker.mascarar_texto`.

### proxima_tentativa(tentativas, erro, agora=None, fila=False) -> float | None
Devolve **instante epoch** (`agora + atraso`); `atraso()` devolve os segundos. None se permanente, se
`repetir=False` (exceto LOOP, que devolve depois da pausa) ou se `tentativas >= MAX_TENTATIVAS[classe]`.
Exponencial `base·2^(n-1)` com teto e *equal jitter*; nunca menor que `espera_s`.
`POLITICA` (in-ciclo): RATE 10 s/300 s/5 · OPERATING 60/900/4 · REDE 2/120/5 · SERVIDOR 5/300/4 ·
URL_EXPIRADA 1/10/3 · LOOP 900/3600/3 · DESCONHECIDO 5/60/2 · demais 1 tentativa.
`fila=True` → `POLITICA_FILA` (base 10 min, teto 6 h, 8 tentativas) para fila persistida entre ciclos (D1/D2).

### LimitadorHost(caminho=None, taxa=None, rajada=None).aguardar(custo=1, timeout=60) -> float
Token bucket em arquivo (`$BITRIX_RATELIMIT_FILE` ou `caminhos.em_base(".bitrix-ratelimit")`) com
`fcntl.flock`; cada chamada reserva o seu lugar (dívida) e dorme fora da trava. Padrão 1.5 req/s, rajada 10;
`BITRIX_RATE` (0.01–100) e `BITRIX_RAJADA` (1–1000) — valor inválido cai no padrão com aviso.
`TempoEsgotado(TimeoutError)` se a espera passaria do `timeout` (nada é consumido). Arquivo corrompido = balde
vazio (conservador). Relógio voltando não credita tokens. Sem acesso ao arquivo → bucket em memória + aviso.
`limitador_padrao()` = um por processo (cache por caminho/taxa/rajada).

### circuito(nome, max_falhas=None, timeout_s=None) -> CircuitBreaker
Arquivo `caminhos.em_base(f"circuit-{nome}.json")`; nome validado (`[a-z0-9][a-z0-9_-]*`, sem path traversal).
Padrões: `bitrix-leitura`/`bitrix-download` 3 falhas/180 s (igual ao `CIRCUIT_PAUSA_SEG` legado);
`vibecode-*` 3/300 s. Usa `conta_falha_padrao`: bug nosso (TypeError, KeyError…), `CircuitOpenError` e falha de
item (404, tamanho, tipo, integridade, URL vencida, inválido) **não** contam contra a fonte (B4).
`aplicar_no_circuito(circ, erro)`: LOOP/OVERLOAD/OPERATING abrem já pela espera; demais falhas de saúde contam
para as N seguidas (motivo = classe); falha de item só devolve a sonda.

### circuit_breaker.CircuitBreaker (endurecido)
- API antiga intacta: `CLOSED/OPEN/HALF_OPEN`, `CircuitOpenError`, `__init__(nome, arquivo, max_falhas, timeout_s, agora)`,
  `estado/falhas/permitido/registrar_sucesso/registrar_falha(erro)/chamar`. Arquivo antigo (sem campos novos) lido igual
  (teste de integração com `triagem_grupo._ler_mensagens_com_breaker`).
- Acréscimos: kwargs `sonda_s`, `conta_falha`, `max_abertura_s`; `registrar_falha(erro, motivo=)`, `abrir(motivo, duracao_s, erro)`,
  `fechar()`, `liberar_sonda()`, `situacao()`; `CircuitOpenError.ate/.motivo`; `mascarar_texto()`.
- Persistidos: `aberto_em`, `aberto_ate`, `motivo`, `ultimo_erro` (mascarado), `fechado_em`, `ultimo_aberto_em`,
  `ultimo_motivo`, `sonda_dono`, `sonda_ate`, `atualizado_em`.
- Toda mutação = flock → relê a chave → transição → grava (tmp pid+uuid, fsync, `os.replace`, fsync do dir).
  Leitura (`estado/permitido`) relê o disco: enxerga o que outro processo fez.
- Meia-abertura com **uma** sonda entre processos (lease `sonda_s`, padrão min(T, 120 s); dono morto → libera após o prazo).
- Nunca aberto para sempre: `aberto_ate` sempre finito; prazo > `max_abertura_s` (24 h) no arquivo é limitado a agora+T e
  **gravado** (senão cada leitura empurraria o prazo).

## Achados

| ID | Status | O que mudou / onde |
|---|---|---|
| B4 (half-open sem sonda única; qualquer exceção conta; snapshot sobrescreve outro processo) | **corrigido** | `circuit_breaker.py`; testes `test_meia_abertura_*`, `test_uma_sonda_entre_processos_reais`, `test_falha_de_outra_instancia_*`, `test_conta_falha_*`, `test_circuito_nao_conta_bug_nosso_*` |
| B3 (circuito nunca fecha/limpa; martelo ignora disjuntor) | **parcial (produtor)** | disjuntor novo fecha no sucesso, grava `fechado_em/ultimo_motivo`, motivo, nunca para sempre. Falta consumidor (onda 2) trocar `circuit-download.json` e `baixar_pendentes`/`download_svc` passarem a consultar `circuito("bitrix-download")` + limitador |
| B1 (disjuntor do Bitrix bloqueia fallback VibeCode) | **parcial (produtor)** | circuitos separados por fonte: `bitrix-download` × `vibecode-download`. Consumidor deve checar só o da fonte que vai chamar |
| B2 (leitura abre o disjuntor de download) | **parcial (produtor)** | circuitos separados `bitrix-leitura` × `bitrix-download`; `http_json` (triagem_grupo) precisa registrar no circuito da operação |
| D1 (sem permanente × transitório) | **parcial (produtor)** | `classificar` + `MAX_TENTATIVAS` + `POLITICA_FILA`; registro de tentativas por file_id é do downloader |
| D2 (backoff em segundos para fila por ciclo) | **parcial (produtor)** | `proxima_tentativa(..., fila=True)` 10 min → 6 h |
| D3 (retenta a MESMA URL assinada) | **parcial (produtor)** | `classificar(..., download=True)` → `URL_EXPIRADA` (repetir pedindo URL nova) |
| C2 (>20 MB / id Telegram no disk.file.get) | **parcial (produtor)** | `ErroTamanho`, `ErroTipo` → permanentes, sem retry |
| G1 (sem guarda de DNS) | não aplicável aqui | não há cliente HTTP neste módulo; `concurrent.futures.TimeoutError` da guarda é classificado como REDE. A guarda `_abrir_com_teto` deve ir para o cliente da onda 2 |
| 07 §4 (limitador único por host) | **corrigido (produtor)** | `LimitadorHost` + teste multiprocesso |
| 07 §5 (política de retry da doc) | **corrigido (produtor)** | tabela acima |

Reconfirmado: `circuit_breaker.py` original já tinha `aberto_ate` finito e half-open por prazo (como dizia a auditoria);
o "aberto para sempre" só ocorria com prazo absurdo no arquivo — agora limitado.

## Mudanças de comportamento (não silenciosas)
- `CircuitBreaker` em meia-abertura: um 2º processo/instância que chama `chamar()` enquanto outro sonda recebe
  `CircuitOpenError` ("MEIA-ABERTURA") em vez de também chamar a fonte. `triagem_grupo._ler_mensagens_com_breaker`
  já trata `CircuitOpenError` como "redireciona" — ok.
- O estado `half_open` e a sonda passam a ser gravados no JSON (antes só `closed/open`).
- `ultimo_erro` gravado mascarado.
- `ARQ_BREAKER` padrão = `$CIRCUIT_BREAKER_JSON` ou `caminhos.em_base("circuit_breaker.json")` (antes: pasta do módulo;
  em produção é o mesmo `/opt/triagem-bitrix`). `triagem_grupo` passa `arquivo=` explícito.
- `registrar_sucesso()` em circuito já fechado e sem falhas não regrava o arquivo.

## Diferenças em relação à assinatura do contrato (acréscimos compatíveis)
- `LimitadorHost.__init__(caminho=None, taxa=None, rajada=None)`: `None` = env `BITRIX_RATE/BITRIX_RAJADA` ou 1.5/10
  (contrato listava `taxa=1.5, rajada=10`; mesmo padrão efetivo, mas env passa a valer também em `LimitadorHost()`).
- `classificar` ganhou kwargs opcionais só-nome: `retry_after`, `operating_reset_at`, `download`, `escrita`, `agora`.
- `proxima_tentativa` ganhou `fila=False`; **retorna instante epoch** (não segundos). Segundos: `atraso()`.
- `Erro` ganhou campos com padrão: `codigo`, `status`, `ambiguo`, `pausa_rodada`; propriedades `conta_no_circuito`, `abre_circuito_ja`.
- `Classe` ganhou `SERVIDOR` e `INVALIDO`. `Classe` é `str, Enum` (serializa como texto).
- Extras: `FalhaBitrix`, `ErroTamanho/ErroTipo/ErroIntegridade`, `TempoEsgotado`, `extrair_codigo`, `operating_reset_at_de`,
  `aplicar_no_circuito`, `conta_falha_padrao`, `POLITICA`, `POLITICA_FILA`, `MAX_TENTATIVAS`, `CIRCUITOS_PADRAO`, `mascarar`.

## Migração do legado `circuit-download.json` (onda 2: triagem_grupo.py / baixar_pendentes.py / download_svc.py)
Hoje: `triagem_grupo.py:108-131` grava `{"aberto_em": ts}` e considera aberto por `CIRCUIT_PAUSA_SEG=180`; nunca fecha.
Plano:
1. Trocar `_circuit_download_aberto()` por `not rede_bitrix.circuito("bitrix-download").permitido()` e
   `_registrar_bloqueio_download()` por `aplicar_no_circuito(circ, classificar(status, corpo, e))`
   (LOOP/OPERATING abrem já; QUERY_LIMIT conta 3 seguidas — ou `circ.abrir("RATE_LIMIT", 180)` se quiserem manter o
   comportamento atual de abrir na 1ª). Registrar `registrar_sucesso()` no 1º `disk.file.get` OK (fecha e limpa o painel).
2. Checar o circuito **só no passo webhook/disk.file.get** (B1); VibeCode usa `circuito("vibecode-download")`.
   `http_json` da leitura registra em `bitrix-leitura` (B2), não no de download.
3. `baixar_pendentes.py` e `download_svc.processar_fila`: antes de cada item, `limitador_padrao().aguardar()` e
   `circ.permitido()`; se `erro.pausa_rodada` → parar a rodada (não tentar o próximo arquivo) (B3).
4. Leitura de transição (1 deploy): se `circuit-download.json` existir com `aberto_em` e `time.time()-aberto_em < 180`,
   chamar `circ.abrir("legado circuit-download.json", 180 - idade)`; depois renomear o arquivo para
   `circuit-download.json.migrado-<ts>` (não apagar). Painel/monitor deve ler `circuit-bitrix-download.json`
   (`situacao()`: `estado`, `aberto_em`, `aberto_ate`, `motivo`, `fechado_em`, `ultimo_motivo`).
5. Remover `_RATE_MIN_SEG`/`_throttle_bitrix` por processo em favor de `limitador_padrao().aguardar()` em `_requisicao`
   e `baixar_arquivo` (o host de download do portal conta no mesmo IP).

## Pendências para outras frentes
- **Onda 2 (download/triagem_grupo/baixar_pendentes/download_svc/completar_cpf/certidoes)**: itens 1–5 acima; usar
  `classificar(..., download=True)` no GET da DOWNLOAD_URL e refazer `disk.file.get` em `URL_EXPIRADA` (D3);
  `ErroTipo` para fid não numérico e `ErroTamanho` para >20 MB (C2); `proxima_tentativa(..., fila=True)` e
  `erro.permanente` para `erro_tipo` persistido (D1/D2); guarda DNS `_abrir_com_teto` no cliente (G1);
  `escrita=True` em chamadas que alteram dados (certidões CE-03).
- **Orquestrador**: produção com outros serviços no mesmo IP (qc-planilha, entrada-planilha, MCP…) só divide a cota se
  usarem o mesmo arquivo — sugerir `BITRIX_RATELIMIT_FILE=/run/bitrix-ratelimit/bucket` com grupo comum
  (o arquivo é criado 0664). Hoje o padrão fica em `caminhos.BASE`.
- **Frente S**: `seguranca.mascarar` é usado quando existe (já existe no repo; testes passam com ele e com o fallback).

## Riscos / precisa do servidor
- Taxa 1.5 req/s é soma de TODOS os processos da triagem; se outros serviços do IP não usarem o limitador, ainda pode
  haver `QUERY_LIMIT_EXCEEDED` — conferir journal após a onda 2.
- `PAUSA_LOOP_S=900` é empírico (doc não documenta `ERROR_LOOP_DETECTED`).
- `flock` em NFS não é confiável: o arquivo do limitador/circuitos deve ficar em disco local (`/opt` ou `/run`).
- Teste multiprocesso usa `spawn` e tolerância de 20 ms por janela; em CI muito carregado pode precisar de folga maior.
