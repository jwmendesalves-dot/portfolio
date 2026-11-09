# 06 — Qualidade, testes e operação

Código auditado: `~/Downloads/triagem-codigo-20261109-1552` (cópia de `/opt/triagem-bitrix`). Tudo somente leitura.
Snapshot de mtime/tamanho da pasta antes e depois dos testes: **idêntico** (nada gravado na cópia).
Ambiente: Python 3.9.6 do sistema (pytest 8.4.2, sem `requests`, sem `pytest-timeout`, sem `pyflakes`) e venv
em `triagem-audit/venv` (pytest, pytest-timeout, requests, pyflakes, vulture). Rede bloqueada nos testes por
plugin (`triagem-audit/plug/noredes.py`, troca `socket.connect`); nenhum teste tentou rede.
Saídas brutas: `triagem-audit/pytest-por-arquivo.txt`, `pyflakes.txt`, `vulture.txt`.

---

## (a) Suíte completa

### Comando pedido
`python3 -m pytest testes -q -p no:cacheprovider -x [--timeout=60]` → **para na coleta**:
`ERROR testes/test_vigia_bot_transitorio.py — FileNotFoundError: '/opt/triagem-bitrix/vigia-bot-triagem.py'`.
Sem `-x` também: `Interrupted: 1 error during collection` → **0 testes rodam**. Ou seja: fora do servidor a suíte
inteira não roda, e no servidor um único arquivo que quebre na coleta derruba tudo.

### Por arquivo (Python 3.9.6 do sistema e venv deram o MESMO resultado — `requests` não é dependência de nenhum teste)

| arquivo | resultado | causa da falha |
|---|---|---|
| test_429_operating | 3 passed | |
| test_cabecalho_encaminhado | 4 passed | |
| test_circuit_breaker | 9 passed | |
| test_circuit_breaker_integracao | 3 passed | |
| test_download_svc | 8 passed | |
| test_envio_bot_bloqueio | **6 failed** | caminho fixo `/opt/triagem-bitrix/pergunta_traco.py` (l.29) |
| test_estado_store | 5 passed | |
| test_guarda_dns | 2 passed | |
| test_limpeza_servico | 1 passed | |
| test_lotes_regras | 8 passed | |
| test_monitor_lotes | 15 passed, **1 failed** | subprocess em `/opt/triagem-bitrix/monitor_lotes.py` (l.152) |
| test_nome_do_cliente_prefixo | 12 passed | |
| test_nome_regras | 61 passed, **1 failed** | `SystemExit: webhook do Bitrix nao encontrado` — import de `nomear_lotes_por_cpf` (l.66/69) |
| test_pasta_fichinha | 2 passed, **1 failed** | idem (webhook no import) |
| test_pintar_linha | 19 passed | |
| test_ponte_criterio | 19 passed | |
| test_ponte_fila | 8 passed, **1 failed** | lê `/opt/agente-separador/src/fila-jobs.js` (outro projeto) |
| test_relatorio | 26 passed | |
| test_senha_cabecalho_curto | 5 passed | |
| test_servico_lote | **3 failed** | `/opt/triagem-bitrix/estado.json` de PRODUÇÃO + fixture por caminho absoluto |
| test_telefones_fichinha | **1 failed** | `estado.json` de produção (l.20) |
| test_telegram_batidas / download / grupo / regras / respostas | 6/6/6/13/9 passed | |
| test_titulo_ficha | 5 passed | |
| test_traco_delimitador | 7 passed | |
| test_variaveis_contrato | **7 failed** | `estado.json` de produção |
| test_vigia_bot_transitorio | **ERRO de coleta** (14 testes) | caminho fixo `/opt/.../vigia-bot-triagem.py` (l.17) |
| test_vigia_bot_triagem | **4 failed** | idem |

**Total (por arquivo): 262 passed · 25 failed · 1 erro de coleta (14 testes).**
Com um plugin de remapeamento `/opt/triagem-bitrix/* → cópia local` (só leitura, `triagem-audit/plug/remap.py`):
envio_bot_bloqueio 6/6, vigia_bot_triagem 4/4, vigia_bot_transitorio 14/14, monitor_lotes 16/16 → **287 passed, 14 failed**.
As 14 restantes dependem do `estado.json` de produção (11), de `config.json` com webhook (2) ou do código do
`agente-separador` (1). **Nenhuma falha é de lógica; todas são de ambiente.** Nenhuma por dependência faltando
(nem `requests`, nem `pypdf`/`pdfplumber` foram exigidos na execução).

### [T1] Suíte presa ao servidor: 31 arquivos de teste com `/opt/triagem-bitrix` fixo
· Severidade **ALTA** · `testes/*.py` (ex.: test_circuit_breaker.py:27, test_vigia_bot_transitorio.py:17,
test_envio_bot_bloqueio.py:29, test_monitor_lotes.py:152, test_servico_lote.py:19/24)
· Evidência: `sys.path.insert(0, "/opt/triagem-bitrix")`; `_CAMINHO = "/opt/triagem-bitrix/pergunta_traco.py"`.
· Impacto: não há como rodar a suíte antes de copiar para produção — o teste só acontece *depois* do deploy, no
servidor. Um arquivo que quebra na coleta zera a suíte toda (confirmado: 0 testes rodam).
· Correção: criar `testes/conftest.py` com `RAIZ = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(RAIZ))`
e trocar os caminhos absolutos por `RAIZ / "pergunta_traco.py"`, `RAIZ / "testes/fixtures/..."`. Adicionar
`pytest.ini` com `testpaths = testes`, `addopts = -p no:cacheprovider`, `timeout = 60`.
· Confiança: confirmado (execução).

### [T2] Testes leem o `estado.json` de PRODUÇÃO (não herméticos, com dados pessoais)
· Severidade **ALTA** · test_servico_lote.py:24, test_telefones_fichinha.py:20, test_variaveis_contrato.py (7 testes)
· Evidência: `with open("/opt/triagem-bitrix/estado.json") ...` e busca de um lote real dentro dele.
· Impacto: o resultado do teste muda conforme o estado (2.722 lotes, 15 MB, regravado a cada ciclo); se a
retenção/limpeza dos 900 lotes vazios (CONTEXTO §10.3) remover o lote usado, o teste quebra sem regressão real.
Também amarra CPF/nome de cliente real à suíte.
· Correção: extrair os 1–2 lotes usados para `testes/fixtures/lote_servico.json` anonimizados e carregar daí.
· Confiança: confirmado.

### [T3] `nomear_lotes_por_cpf` resolve o webhook NO IMPORT e mata o processo
· Severidade **MÉDIA** · nomear_lotes_por_cpf.py:66-69 (`WEBHOOK = _webhook()` → `raise SystemExit`)
· Importado por `lote_para_job.py` e `completar_cpf_por_nome.py:23`.
· Impacto: qualquer importador (inclusive `lote_para_job`, que é usado pela ponte) morre com SystemExit se o
config não estiver legível; torna 2 testes inexecutáveis fora do servidor e impede testar `completar_cpf_por_nome`.
· Correção: resolver preguiçosamente (`@functools.lru_cache def webhook(): ...`) e chamar na hora do uso.
· Confiança: confirmado (2 testes falham com essa mensagem).

### [T4] Teste que executa o `main()` real em produção e grava arquivo
· Severidade **BAIXA** · test_monitor_lotes.py:152 roda `monitor_lotes.py --minutos 30`, que grava
`monitor-lotes.json` (monitor_lotes.py:44/209).
· Impacto: rodar a suíte no servidor sobrescreve o arquivo que o vigia (cron Hermes 30 min) lê.
· Correção: passar `MONITOR_SAIDA` via env para `tmp_path` no teste.
· Confiança: confirmado no código.

### [T5] Testes "embutidos" fora do pytest (~970 linhas em produção)
· Severidade **BAIXA** · triagem_grupo.py:2962-3931 (`testar_cpf`, `testar_sinais`, `testar_fonte`); lote_para_fila.py:462 (`testar`).
· Impacto: casos de CPF/traço (núcleo da fase 1) não rodam no `pytest`; só manualmente com `--testar-*`.
· Correção: mover para `testes/test_cpf_extrator.py` e `testes/test_sinais_traco.py` (parametrize); `testar_fonte` (usa rede) vira `tools/provar_fonte.py`.
· Confiança: confirmado.

---

## (b) Cobertura: módulos SEM nenhum teste

Busca por import/nome do módulo em `testes/*.py`:

| sem teste | papel | crítico? |
|---|---|---|
| **baixar_pendentes.py** | 1º passo do timer de 10 min (download) | **sim** |
| **completar_cpf_por_nome.py** | grava `pronto=True`/CPF no estado (timer 10 min) | **sim** |
| **reconciliar_estado.py** | grava estado (timer 10 min) | **sim** |
| **reconciliar_por_file_ids.py** | grava estado (timer 10 min) | **sim** |
| **certidoes.py** | timer 5 min, cria tarefa no CRM | **sim** |
| reconciliar_arquivos_disco.py, nomear_lotes_por_cpf.py, backfill_nome_lotes.py, dividir_lote_misto.py, reparo_josilene.py, checar_midias.py, backup_pintura.py, conferir_pintura.py, esperar_bot_liberar.py, avisar-primeiro.py | one-off / manutenção | não (ver (e)) |

Com teste mas **caminho crítico descoberto**:
- `lote_para_fila.py` — `test_ponte_fila.py` cobre só `gravar_na_fila` (AST) e `perdidos_relevantes`. **Sem teste**:
  `planejar` (l.195), `montar_staging` (l.253, cópia + sidecar — o que o worker consome), `marcar_duplicatas_no_estado`
  (l.100, os 503 descartes), e o laço de seleção de `main` (`--dias/--limite/--so-traco/--pular-cpf-repetido`, l.501-).
- `download_svc.py` testado; mas `baixar_pendentes.py` (quem efetivamente roda no timer) não usa o `download_svc` nem o breaker — ver O3.

### [C1] Escritores do `estado.json` sem trava e sem teste competem com o ciclo
· Severidade **ALTA** · completar_cpf_por_nome.py:88 (lê) → 141-146 (grava com `os.replace`), reconciliar_estado.py:36/86-90,
reconciliar_arquivos_disco.py:109-114, nomear_lotes_por_cpf.py:206, backfill_nome_lotes.py:86 — nenhum usa `estado_store.atualizar_estado`.
· Evidência: `estado = json.load(open(ESTADO))` … `time.sleep(0.55)` por lote … `os.replace(tmp, ESTADO)`.
O próprio `estado_store.py:5-14` documenta o bug de *lost update* (6 de 7 lotes perdidos em 24/09).
· Impacto: `completar_cpf_por_nome` segura uma cópia do estado por **0,55 s × N lotes SEM_CPF** (centenas → minutos) e
depois sobrescreve o arquivo inteiro: tudo que `triagem_grupo` gravou nesse intervalo some. O inverso também: se o ciclo
gravar por cima, o CPF preenchido some e o lote volta a ser "achado" no próximo ciclo → novo backup (ver O1, hipótese
para 1 backup/10 min).
· Correção: trocar o bloco de gravação por `atualizar_estado(mutador)` reaplicando só os campos dos `achados`
(mesmo padrão de `reconciliar_por_file_ids.py:15/69`). Teste: copiar o `test_estado_store` de concorrência para esses 3 scripts.
· Confiança: confirmado no código (efeito em produção = hipótese).

### [C2] `estado_store.ler_estado` devolve `{}` em erro e `atualizar_estado` grava por cima
· Severidade **MÉDIA** · estado_store.py:39-44 e 122-127
· Evidência: `except (OSError, ValueError): return {}` → `mutador(estado)` → `gravar_atomico(estado)`.
· Impacto: um erro transitório de leitura (EMFILE, EIO, JSON truncado por escritor sem trava de C1) zera os 2.722
lotes; a única defesa hoje são os 27 GB de `.bak`.
· Correção: em `atualizar_estado`, ler com função que **levanta** em erro se o arquivo existe; `{}` só se `FileNotFoundError`.
Teste: arquivo com JSON inválido → `atualizar_estado` deve levantar e não gravar.
· Confiança: confirmado no código.

---

## (c) Operação: logs e backups

### [O1] `estado.json.bak-*` (1.833 arquivos, 27 GB) — quem gera
· Severidade **ALTA** · Geradores (todos mesmo padrão `f"{ESTADO}.bak-{%Y%m%d-%H%M%S}"`, sem retenção):
completar_cpf_por_nome.py:141, reconciliar_arquivos_disco.py:109, nomear_lotes_por_cpf.py:206, backfill_nome_lotes.py:86, reparo_josilene.py:123.
`triagem_grupo.py`, `estado_store.py` e `reconciliar_estado.py` **não** fazem backup.
· Dos que estão no timer de 10 min (CONTEXTO §5), só **`completar_cpf_por_nome.py --aplicar`** gera `.bak`, e só quando
`achados` não é vazio (l.125). 1.833 ÷ 144 ciclos/dia ≈ 12,7 dias de 1 backup por ciclo → **hipótese**: o mesmo lote é
"achado" todo ciclo porque o ciclo regrava o estado sem o CPF (C1). Verificar no servidor:
`ls estado.json.bak-* | tail` vs `journalctl -u triagem-lote-fila | grep APLICADO`.
· Correção (no código, `estado_store.py` — função única usada por todos):
```python
def backup_com_retencao(caminho=ARQ_ESTADO, manter_ultimos=24, dias_diarios=14):
    """Copia para <caminho>.bak-AAAAmmdd-HHMMSS e poda: mantém os N mais novos
    + o mais novo de cada dia nos últimos `dias_diarios` dias; apaga o resto."""
    import glob, shutil, datetime as dt
    destino = f"{caminho}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(caminho, destino)
    baks = sorted(glob.glob(f"{caminho}.bak-*"), reverse=True)   # nome ordena por data
    manter = set(baks[:manter_ultimos])
    limite = (dt.date.today() - dt.timedelta(days=dias_diarios)).strftime("%Y%m%d")
    vistos = set()
    for b in baks:
        dia = b.rsplit(".bak-", 1)[1][:8]
        if dia >= limite and dia not in vistos:
            manter.add(b); vistos.add(dia)
    for b in baks:
        if b not in manter:
            os.remove(b)
    return destino
```
Resultado: 24 + 14 = ~38 arquivos × 15 MB ≈ **570 MB** (de 27 GB). Comprimir (`gzip`) os diários reduz para ~60 MB.
Substituir os 5 `shutil.copy2(ESTADO, backup)` por `backup_com_retencao()`. Além disso: só fazer backup se o conteúdo
vai mudar de fato.
Limpeza imediata (antes do deploy), equivalente em shell, via timer diário:
```bash
cd /opt/triagem-bitrix && ls -1 estado.json.bak-* | sort -r | awk -F'bak-' '
  NR<=24 {print "KEEP",$0; next}
  { d=substr($2,1,8); if (d>=lim && !(d in v)) {v[d]=1; print "KEEP",$0} else print "DEL",$0 }' \
  lim=$(date -d '14 days ago' +%Y%m%d) | awk '$1=="DEL"{print $2}' | xargs -r rm -f --
```
· Confiança: geradores confirmados; causa da frequência = hipótese.

### [O2] `backups/` (737 MB) — `fila-jobs-<data>.json` a cada envio
· Severidade **MÉDIA** · lote_para_fila.py:52 e 341-344 (`shutil.copy2(FILA, BACKUPS/fila-jobs-{marca}.json)`), sem poda.
· Impacto: a ponte roda a cada 5 min e o lote-fila a cada 10; cada rodada com envio copia a fila inteira.
· Correção: mesma política (`manter_ultimos=50`, `dias_diarios=14`) aplicada em `gravar_na_fila`, ou
`find /opt/triagem-bitrix/backups -name 'fila-jobs-*.json' -mtime +14 -delete` no timer de limpeza. Outros conteúdos
de `backups/` ("dumps e cópias") não são gerados pelo código desta cópia → manuais; revisar à mão.
· Confiança: gerador confirmado; composição dos 737 MB não verificável sem o servidor.

### [O3] Logs sem rotação: `ponte.log` (208 MB), `pergunta.log`, `certidoes.log`
· Severidade **MÉDIA**
· `ponte.log`: `ponte-rodar.sh` só escreve em stdout (l.10/15/20); o arquivo é criado pela unit — **hipótese**:
`StandardOutput=append:/opt/triagem-bitrix/ponte.log` em `triagem-bitrix-ponte.service`. Idem `certidoes.log` (README:163).
· `pergunta.log`: `pergunta-rodar.sh:3` faz `>> pergunta.log 2>&1`; o loop escreve a cada 8 s (vigia_bot.sh:11) e
`vigia_bot.sh` depende do **mtime** do arquivo — a rotação não pode recriar arquivo vazio antigo; `copytruncate` serve.
· Só `triagem.log` tem rotação (triagem_grupo.py:220, `RotatingFileHandler` 2 MB × 5).
· Correção — `/etc/logrotate.d/triagem-bitrix`:
```
/opt/triagem-bitrix/ponte.log
/opt/triagem-bitrix/certidoes.log
/opt/triagem-bitrix/pergunta.log
/opt/triagem-bitrix/pintar-linha.log {
    daily
    rotate 14
    maxsize 20M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root root
}
```
Melhor ainda: tirar o `append:` das units e usar o journal (`journalctl -u triagem-bitrix-ponte`), com
`SystemMaxUse=500M` em `journald.conf`. Após a 1ª rotação o `ponte.log` de 208 MB vira ~10 MB gz.
· Confiança: rotação ausente confirmada no código; origem do arquivo = hipótese.

### [O4] Scripts escondem falha do systemd
· Severidade **MÉDIA**
· `certidoes-rodar.sh:13`: `echo "... (exit $?)"` é a última linha → o script sai **sempre 0** (o status do `echo`).
Falha do `certidoes.py` nunca vira `failed` no systemd. Correção: `code=$?; echo ...; exit $code` (como ponte-rodar.sh:21-23).
· `baixar_pendentes.py:146-147`: `return 0` mesmo com `erros>0`; idem `reconciliar_estado.py:92`. Correção: `return 1 if erros else 0`
(ou código 2 = "parcial") para o timer poder alertar.
· `pipeline.sh:9-15`: `python3 ... | tail -4` sem `set -o pipefail` → o status é do `tail`. Correção: `set -euo pipefail`.
· Confiança: confirmado.

### [O5] `baixar_pendentes.py` ignora o circuit-breaker e o tipo de mídia
· Severidade **ALTA** · baixar_pendentes.py:75-147 (0 ocorrências de `circuit`); laço l.119-141
· Evidência: chama `disk.file.get` com `RATE=0.55` e `TENTATIVAS=3` para todo `file_id` faltante, sem consultar
`circuit-download.json` e sem filtro de extensão.
· Impacto: o breaker está **aberto desde 09/11 13:06** (CONTEXTO §4), mas o 1º passo do timer de 10 min continua
batendo no Bitrix — exatamente o que mantém o bloqueio. Os 3 áudios >20 MB e 453 pendentes são re-tentados a cada
ciclo ("erro a cada 10 min", CONTEXTO §7.4).
· Correção: no início de `main`, `from circuit_breaker import ...; if not breaker.permitido(): print("breaker aberto"); return 0`;
registrar sucesso/falha no breaker; pular `.flac/.wav/.mp3/.mp4`; ou simplesmente substituir por `download_svc.py`
(que já tem fila, backoff e breaker, e é testado).
· Confiança: confirmado no código.

---

## (d) README.md × código × CONTEXTO

| # | README | Código / CONTEXTO | Sev. |
|---|---|---|---|
| D1 | l.5 "não fala com a fila — fase 2 (ainda não ligada)" | ponte ligada desde 21/09 (o próprio README l.82, 94) | BAIXA |
| D2 | l.54 `fonte_leitura = api`; l.38 webhook "só diagnóstico" | `config.json`: **`"webhook"`**; triagem_grupo.py:2665 aceita `webhook` do config como fonte primária | **ALTA** — doc diz que a fonte ruim é só diagnóstico, produção roda nela |
| D3 | l.48 `modo_traco = grupo_autor`; l.217 `"grupo"` | `config.json`: `"delimitador"` | MÉDIA |
| D4 | l.51 `sinal_traco - – — =` | config inclui `_` (CONTEXTO também) | BAIXA |
| D5 | l.18 `--loop 300`; l.208 `--loop 120` | contraditório no próprio README; unit não disponível | BAIXA |
| D6 | l.97 ponte `--limite 4 --dias 2 --so-traco` | ponte-rodar.sh:20 `--dias 7`, sem `--so-traco` (é padrão); help em lote_para_fila.py:512 ainda diz "O timer usa 2" | BAIXA |
| D7 | l.129-132 e l.143 "a ponte **para o worker**", `esperar_ocioso` | revertido em 24/09 (README l.236; lote_para_fila.py:333-340). `esperar_ocioso`/`WorkerOcupado`/`WORKER_SVC` (l.54, 274-300) ficaram mortos | MÉDIA (instrução operacional errada) |
| D8 | l.100-107 regra "só traço" com ⚠️ | regra mudou em 24/09 (l.212, `ponte_criterio.py`); flag ainda se chama `--so-traco` mas aplica a regra nova (lote_para_fila.py:519) | MÉDIA |
| D9 | l.138 staging `<sha12>.<ext>` | lote_para_fila.py:14/215 usa `<file_id>.<ext>` (sha12 só como fallback); CONTEXTO certo | BAIXA |
| D10 | l.127 `lotes-vs-planilha.js`; l.79 `simulacao-jobs/` | não existem nesta cópia (hipótese: no servidor fora do pacote) | BAIXA |
| D11 | l.232/246 suíte `/opt/qc-planilha/venv` "85 testes" | 301 testes nesta cópia; venv de outro projeto | BAIXA |
| D12 | — | **Não documentados**: timer `triagem-lote-fila` (10 min), `pintar-linha-triagem` (2 min), `baixar_pendentes`, `download_svc`, `circuit_breaker`, `completar_cpf_por_nome`, `reconciliar_*`, `pipeline.sh`, `vigia_bot.sh`, `pergunta_traco` (bot), retenção/limpeza | **ALTA** — metade do que roda sozinho não está no README |
| D13 | CONTEXTO §2 "triagem_grupo lê com cursor"; README l.18 serviço `triagem-bitrix` | CONTEXTO §5 não lista o serviço `triagem-bitrix` (fase 1) nem `triagem-bitrix-bot` (vigia_bot.sh:17) entre os timers | BAIXA |

Correção: README novo curto com tabela "unidade → script → cadência → log → lock" (fonte única), remover seções
históricas para `docs/HISTORICO.md`.

---

## (e) Dívida técnica

### [E1] `triagem_grupo.py` (4.116 linhas, 89 funções) — divisão proposta
As seções já existem como comentários; corte direto, sem mudar lógica (manter `triagem_grupo.py` como fachada
reexportando os nomes que os 6 testes importam):

| novo módulo | linhas atuais | conteúdo |
|---|---|---|
| `triagem/config.py` | 82-209, 394-464 | constantes, `carregar_config`, descoberta de credenciais |
| `triagem/http.py` | 233-393 | urllib + retry/backoff/rate-limit |
| `triagem/sinais.py` | 465-625 | traço, modos, toggle |
| `triagem/cpf.py` | 626-750 | extração/validação de CPF, pistas |
| `triagem/texto_nome.py` | 751-1035 | normalização de nome (junto de `nome_regras.py`) |
| `triagem/mensagens.py` | 1036-1161 | modelo de mensagem, reação |
| `triagem/fonte_api.py` / `fonte_webhook.py` | 1162-1237 / 1238-1303 | leitores |
| `triagem/agrupador.py` | 1304-2156 (~850 l.) | montagem de lotes — maior bloco; quebrar em `abertura.py`/`fechamento.py`/`identidade.py` numa 2ª etapa |
| `triagem/download.py` | 2157-2271 | (avaliar troca por `download_svc`) |
| `triagem/estado.py` | 2272-2555 (2 seções "Estado" duplicadas) | serialização; gravação só via `estado_store` |
| `triagem/breaker_fontes.py` | 2556-2608 | |
| `triagem/ciclo.py` | 2609-2961 | `ciclo()` |
| `testes/test_cpf_extrator.py`, `test_sinais_traco.py` | 2962-3788 | testes embutidos → pytest |
| `tools/provar_fonte.py` | 3789-3931 | `--testar-fonte` |
| `triagem_grupo.py` (CLI) | 3932-4116 | parser + main |

### [E2] Código morto / problemas do pyflakes+vulture (confirmados por grep em todo o repo)
· Severidade **BAIXA** (exceto onde indicado)
- `triagem_grupo.py:2500 salvar_estado` — **sem chamadas**; grava sem trava (perigoso se alguém reusar). Remover.
- `triagem_grupo.py:2209 baixar_file_id`, `:96 DIALOG_IDS_PADRAO`, `:133 BOT_ID_AULA`, `:1627-1628 cpf_msg/nomes_msg` (atribuídas e não usadas — checar se era para entrar na decisão), `:205 PALAVRAS_NAO_NOME` importado sem uso.
- `lote_para_fila.py:54 WORKER_SVC`, `:274 WorkerOcupado`, `:278 esperar_ocioso` — resto do modo "para o worker" (só o teste referencia). `:228 lote` não usado.
- `lote_para_job.py:323 semanas_para_meses`, `:722 servico_contratado_dos_pdfs`, `:556 RE_SERVICO_CONTRATADO`, `:85 CAMPOS_FICHA`, `:147 RX_LINHA_SEM_SEP`, `:463 marcar`, `:483 tel`, `:531 alvo`.
- `pergunta_traco.py:351 falar_desfecho`, `:100/111/112 TEXTO_*`; `telegram_grupo.py:400 decifrar_batidas_para_log`; `ponte_criterio.py:15 FECHAMENTOS_FALLBACK`; `lotes_regras.py:16 STATUS_FINAIS`; `relatorio.py:32 SINAL_TELEGRAM`.
- `reconciliar_estado.py:91` `{lotas_ok if False else lotes_ok}` — ternário impossível (resto de correção de typo).
- `completar_cpf_por_nome.py:23` importa `WEBHOOK` só pelo efeito colateral (e herda o SystemExit de T3).
- `dividir_lote_misto.py:20 json` e `:114 filenames` não usados.

### [E3] Scripts one-off na raiz (mesmo nível dos serviços)
Mover para `tools/` (ou apagar após confirmar que não estão em unit/cron no servidor):
`reparo_josilene.py` (nome de cliente no arquivo, reparo pontual), `backfill_nome_lotes.py`, `dividir_lote_misto.py`,
`nomear_lotes_por_cpf.py` (manter só o helper `chamar/formatar_cpf` num `bitrix_api.py`), `reconciliar_arquivos_disco.py`,
`checar_midias.py`, `backup_pintura.py`, `conferir_pintura.py`, `esperar_bot_liberar.py`, `avisar-primeiro.py`, `pipeline.sh`
(substituído pelo timer `triagem-lote-fila`). Todos os que escrevem `estado.json` devem ganhar `--aplicar` explícito +
`atualizar_estado` antes de sobreviverem (C1).

---

## (f) Proposta de units systemd/timers (não temos os .service — proposta)

Princípios: **todo** job tem `flock` (o lote-fila e a ponte usam o MESMO lock, porque ambos rodam
`lote_para_fila --enviar` e gravam `fila-enviados.json` sem trava — lote_para_fila.py:93-97); `TimeoutStartSec`
menor que a cadência×3; `Nice`/`IOSchedulingClass` para não competir com o worker; saída no journal.

`/etc/systemd/system/triagem-lote-fila.service`
```ini
[Unit]
Description=Triagem: baixar -> reconciliar -> CPF por nome -> enfileirar
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/triagem-bitrix
Environment=PYTHONUNBUFFERED=1
# mesmo lock da ponte (ambos enviam p/ a fila); -w 0 = pula se ocupado
ExecStart=/usr/bin/flock -n /var/lock/triagem-bitrix-ponte.lock /opt/triagem-bitrix/tools/lote_fila.sh
TimeoutStartSec=25min
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
MemoryMax=1G
SuccessExitStatus=1
```
(`tools/lote_fila.sh` = `pipeline.sh` com `set -euo pipefail` e cada passo com `timeout 10m`.)

`triagem-lote-fila.timer`
```ini
[Timer]
OnBootSec=3min
OnUnitInactiveSec=10min      # conta do FIM da última rodada: nunca empilha
RandomizedDelaySec=30
Persistent=false
[Install]
WantedBy=timers.target
```

Ponte / certidões / pintar-linha (padrão igual):
```ini
[Service]
Type=oneshot
WorkingDirectory=/opt/triagem-bitrix
ExecStart=/usr/bin/flock -n /var/lock/triagem-bitrix-ponte.lock /usr/bin/python3 lote_para_fila.py --enviar --todos --limite 4 --dias 7
TimeoutStartSec=4min          # ponte 5 min | certidoes 4min | pintar-linha 90s
Nice=10
IOSchedulingClass=idle        # pintar-linha/certidoes
```
- certidões: `flock -n /var/lock/triagem-bitrix-certidoes.lock python3 certidoes.py --enviar` (sem o wrapper que engole o exit, O4).
- pintar-linha: `flock -n /var/lock/pintar-linha-triagem.lock ...` (hoje não há lock visível no código).
- Timers com `OnUnitInactiveSec=` em vez de `OnCalendar=*:0/5`.

Limpeza (novo) — `triagem-limpeza.service` + `.timer` diário 04:15:
```ini
[Service]
Type=oneshot
ExecStart=/usr/bin/flock -w 300 /opt/triagem-bitrix/estado.json.lock /usr/bin/python3 /opt/triagem-bitrix/tools/limpar_backups.py --estado-ultimos 24 --estado-diarios 14 --fila-ultimos 50 --fila-diarios 14
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=15min
[Timer]
OnCalendar=*-*-* 04:15
Persistent=true
```
(o lock `estado.json.lock` é o mesmo de `estado_store._TravaDeArquivo`, estado_store.py:83.)

Alertas: `OnFailure=triagem-alerta@%n.service` em todas (envia ao Telegram) — só funciona depois de O4.
Bot: `triagem-bitrix-bot.service` com `WatchdogSec=180` + `sd_notify` no loop substituiria `vigia_bot.sh` (que hoje
depende do mtime do log).

---

## Top 5 da frente (prioridade)

1. **O1 + C1** — retenção dos `.bak` (função única `backup_com_retencao` em `estado_store`, 24 últimos + 1/dia × 14 → ~570 MB de 27 GB) e passar `completar_cpf_por_nome`/`reconciliar_*` para `atualizar_estado` (lost update + provável causa de 1 backup/ciclo).
2. **O5** — `baixar_pendentes.py` ignora o circuit-breaker aberto e re-tenta áudios a cada 10 min; respeitar o breaker ou trocar por `download_svc`.
3. **T1/T2** — suíte não roda fora do servidor (0 testes por erro de coleta); `conftest.py` com raiz relativa + fixtures anonimizadas no lugar do `estado.json` de produção (aqui: 287/301 passam só remapeando caminhos).
4. **O3/O4** — logrotate para `ponte.log`/`pergunta.log`/`certidoes.log` e corrigir exit codes engolidos (`certidoes-rodar.sh:13`, `pipeline.sh` sem pipefail, `baixar_pendentes` sempre 0).
5. **D2/D12** — README diz que webhook é "só diagnóstico" mas produção roda em `webhook`, e não documenta metade dos timers; reescrever com tabela unidade→script→lock→log.

## O que NÃO deu para verificar sem o servidor
- Conteúdo real das units/timers (ExecStart do `triagem-lote-fila`, se usa flock, de onde vem `ponte.log`/`certidoes.log`).
- Qual script gera de fato 1 `.bak` por ciclo (datas dos arquivos × journal); composição dos 737 MB de `backups/`.
- Os 14 testes que exigem `estado.json` de produção, `config.json` com webhook e `/opt/agente-separador/src/fila-jobs.js`.
- Se os one-off de (e) são chamados por cron/unit; existência de `lotes-vs-planilha.js`/`simulacao-jobs/` no servidor.
- Cobertura por linha (`pytest-cov`) — não instalado para não alongar; o mapeamento de (b) é por módulo/função, via grep.
