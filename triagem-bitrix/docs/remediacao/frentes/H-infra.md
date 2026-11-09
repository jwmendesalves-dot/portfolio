# Frente H — Infraestrutura e operação

Branch `mimo/auxiliares` · escopo: `deploy/` (novo), `README.md`,
`docs/remediacao/{ARQUITETURA-CORRIGIDA,RUNBOOK-OPERACIONAL,PLANO-IMPLANTACAO}.md` e este
relatório. **Nenhum arquivo alheio foi tocado.** Nada de rede, nada de servidor, nenhum
dado real de cliente em fixtures/exemplos.

**Contexto:** durante o trabalho o orquestrador integrou a onda 1 (`git merge remediacao`):
`seguranca.py`, `cpf_util.py`, `rede_bitrix.py`, `estado_store.py`, `estado_lote.py`,
`bitrix_cliente.py`, `tipos_documento.py` passaram a existir. Por isso **não há fallback
`# COMPAT-ONDA1`** em nada que escrevi: `deploy/bin/podar-backups.py` importa
`estado_store.podar_backups` e `seguranca.remover_seguro` direto (seção "Pontos
COMPAT-ONDA1" ao final).

## Resumo por achado

| ID | Status | O que esta frente entregou |
|---|---|---|
| **S05** (sem retenção: 1.833 `.bak` = 27 GB + fila) | **parcial** | `deploy/bin/podar-backups.py` (política 24+14 via contrato, `--simular` padrão, categorias operacional/recuperação/evidência, descarte seguro só com autorização), `triagem-limpeza.service/.timer` diário, PLANO/RUNBOOK. Falta: migração dos 5 geradores de backup (donos alheios) e autorização do descarte dos legados |
| **S08** (tudo root, sem permissão restrita) | **parcial** | units propostas com `User=triagem`, `UMask=0077`, `ProtectSystem=strict` + `ReadWritePaths`, `PrivateTmp`, `NoNewPrivileges`, `EnvironmentFile` 0600; `deploy/bin/verificar-ambiente.sh`; `deploy/sudoers/`; migração gradual em 4 fases. Falta: código ainda exige root (caminhos `/root`, `systemctl`) — pendências alheias — e conferir as units reais no servidor |
| **OP-O3** (logs sem rotação) | **parcial** | `deploy/logrotate/triagem-bitrix` (`copytruncate` obrigatório pelo vigia, daily, rotate 14, maxsize 20M, compress+delaycompress). Falta: instalar no servidor e confirmar a origem de `ponte.log`/`certidoes.log` (unit `append:` = hipótese) |
| **OP-O4** (exit codes engolidos; proposta de units) | **parcial** | units/timers completos em `deploy/systemd/` com `OnFailure=triagem-alerta@%n`, `TimeoutStartSec`, `flock` coerente; `deploy/bin/lote-fila.sh` (`set -euo pipefail`, `timeout` por passo, sem `--todos-os-fechamentos`); `deploy/bin/alertar-falha.sh`. Em paralelo, a Frente X corrigiu `certidoes-rodar.sh` (confirmado no diff). Restam `baixar_pendentes.py:146`, `reconciliar_estado.py:92` e `pipeline.sh` (arquivos alheios) |
| **OP-E1** (divisão do `triagem_grupo.py`) | **não aplicável** (só documento) | tabela de divisão com âncoras reais verificadas + divergências (abaixo) |
| **OP-E2** (código morto) | **não aplicável** (só relatório) | lista verificada item a item por grep + divergências de linha pós-onda 1 |
| **OP-T5** (testes embutidos) | **não aplicável** (só relatório) | inventário + proposta de migração para pytest |

---

## S05 · Retenção inexistente de backups (27 GB) — PARCIAL

**Reconfirmação no código (pós-onda 1), todos os geradores ainda ativos:**

```
$ grep -n "shutil.copy2\|\.bak" completar_cpf_por_nome.py reconciliar_arquivos_disco.py \
    nomear_lotes_por_cpf.py reparo_josilene.py backfill_nome_lotes.py lote_para_fila.py
completar_cpf_por_nome.py:141:  backup = f"{ESTADO}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
completar_cpf_por_nome.py:142:  shutil.copy2(ESTADO, backup)
reconciliar_arquivos_disco.py:109/110 ... nomear_lotes_por_cpf.py:206/207 ...
reparo_josilene.py:123/124 ... backfill_nome_lotes.py:86/87 ...
lote_para_fila.py:344:  shutil.copy2(FILA, bkp)          # backups/fila-jobs-<ts>.json
$ grep -rn "remove|unlink|rmtree" --include="*.py" . | grep -i "bak|backup"
(sem resultado — nada apaga backup)
```

Confirmado: nenhum código apaga backups; os 5 geradores de `.bak` e o gerador de
`backups/fila-jobs-*` continuam (a auditoria está correta; o número de 27 GB é do
CONTEXTO e só o servidor confirma). A onda 1 trouxe `estado_store.fazer_backup` (0600 em
`backups-estado/`, não duplica conteúdo idêntico) e `estado_store.podar_backups`
(`estado_store.py:730,762`) — mas **nenhum dos 5 scripts foi migrado** (são de outros
donos), então o acúmulo continua até a onda 2.

**O que mudou (meus arquivos):**

1. `deploy/bin/podar-backups.py` — consome o contrato: a política vem de
   `estado_store.podar_backups(caminho, manter_ultimos=24, diarios=14, simular=True)`;
   a execução usa `seguranca.remover_seguro` (zerar + fsync + unlink). Três categorias:
   - **operacional** (`backups-estado/`, legados `estado.json.bak-*`, `backups/fila-jobs-*`
     com política 50+14 sugerida em OP-O2): podados só com `--aplicar`;
   - **recuperação** (`.corrompido-*`, dumps manuais de `backups/`): **nunca** apagados;
   - **evidência** (legados com senha gov.br de cliente, S01): **nunca automático** —
     exigem `--aplicar --descarte-seguro --autorizado-por "<nome>"`, com a autorização
     registrada na saída/journal.
   Padrão = `--simular` (mostra o plano; nada é removido).
2. `deploy/systemd/triagem-limpeza.service/.timer` — diário 04:15, `--aplicar --tmp-orfaos`
   (`estado_store.limpar_tmp_orfaos`), **sem** `--descarte-seguro`.
3. Política e fluxo de autorização documentados em RUNBOOK §4–5 e PLANO Fase 0/3.

**Testes/evidência (real):** fixture sintética (40 legados + 40 novos + 60 fila +
`.corrompido-*` + dump manual, em `/tmp`, sem dado real):

```
$ python3 deploy/bin/podar-backups.py --estado "$FIX/estado.json"            # simulação
[OPERACIONAL] backups do estado novos (backups-estado/): 28 excedente(s), 2.7 KB
[EVIDENCIA] backups LEGADOS do estado (contem senha de cliente, S01): 20 excedente(s), 2.0 KB
[OPERACIONAL] backups da fila (fila-jobs-*): 10 excedente(s), 1000 B
[RECUPERACAO] .corrompido-* e dumps/cópias manuais em backups/: 2 arquivo(s), 200 B — NUNCA apagados por aqui
SIMULACAO: nada foi apagado. Reaproveitaria 5.7 KB.
$ ... contagens apos simulacao: 44 / 40 / 61  (idênticas — nada apagado)

$ python3 deploy/bin/podar-backups.py --descarte-seguro          # sem autorização
ERRO: --descarte-seguro exige --autorizado-por "<nome de quem autorizou>".
exit: 2                                                          (nada apagado)

$ python3 deploy/bin/podar-backups.py --aplicar                  # só operacionais
total removido: 38 arquivo(s).      # 28 novos + 10 fila
legados restantes: 40 | corrompido: 1 | dump manual: 1           (intocáveis)

$ python3 deploy/bin/podar-backups.py --aplicar --descarte-seguro --autorizado-por "..."
AUTORIZACAO DE DESCARTE SEGURO: ... em 2026-11-09 16:21:42
  [REMOVIDO] (evidencia/legado, autorizado por ...) .../estado.json.bak-20261103-120600
total removido: 20 arquivo(s).
```

**Divergência/nua­nce achada no teste:** a cota 24+14 do contrato é sobre a **união** dos
formatos (novos + legados): com os dois coexistindo ela se reparte (fixture: 12 novos +
20 legados mantidos, não 24+24). Documentado no próprio script e no RUNBOOK §4.

## S08 · Tudo root, sem permissão restrita — PARCIAL

**Reconfirmação (pós-onda 1):**

```
$ grep -n "/root/\|/tmp/" lote_para_fila.py certidoes.py telegram_grupo.py download_svc.py ...
lote_para_fila.py:53: NODE = "/root/.hermes/node/bin/node"
lote_para_fila.py:361/363/367: /tmp/_enfileirar.js + /tmp/_entradas.json (nome fixo)
certidoes.py:205/206: /tmp/_ler_certidao.js + node /root/.hermes/...
certidoes.py:323: /tmp/certidao_<id>.pdf (não apagado)
telegram_grupo.py:38: /root/.telegram/documentosexemplo.token
download_svc.py:33/34: /tmp/b24_webhook.txt (world-writable) + /root/.vibecode/api_key
vigia_bot.sh:32 / esperar_bot_liberar.py:33: systemctl restart/start
$ grep -n "O_CREAT" estado_store.py circuit_breaker.py triagem_grupo.py pergunta_traco.py rede_bitrix.py
estado_store.py:100/271: 0o600        pergunta_traco.py:330/584/1135: 0o600   # corrigidos na onda 1
circuit_breaker.py:351/398: 0o644     triagem_grupo.py:4037 (ciclo.lock): 0o644
rede_bitrix.py:597 (rate-limit): 0o664
```

**Divergência parcial com a auditoria:** o achado dizia "locks criados `0o644`
(estado_store.py:83, ...)"; a onda 1 corrigiu `estado_store` e `pergunta_traco` (0600).
Restam `circuit_breaker.py:351,398`, `triagem_grupo.py:4037` (0644) e
`rede_bitrix.py:597` (0664) — com `UMask=0077` nas units os arquivos novos já nascem 0600,
mas os existentes precisam de `chmod` (PLANO Fase 0.1).

**O que mudou (meus arquivos):** units propostas em `deploy/systemd/` todas com
`User=triagem`/`Group=triagem`, `UMask=0077`, `NoNewPrivileges=yes`, `PrivateTmp=yes`,
`ProtectSystem=strict` + `ReadWritePaths=/opt/triagem-bitrix /opt/agente-separador/data
/var/lock` explícitos, `ProtectHome=yes`, `EnvironmentFile=/etc/triagem-bitrix/env` (0600),
`Nice`/`IOSchedulingClass`/`MemoryMax`; `deploy/bin/verificar-ambiente.sh` (só leitura:
modo/dono de segredos e diretórios, disco, locks órfãos, passivo de backups, presença de
units/logrotate); `deploy/sudoers/triagem-bitrix` (só restart do bot);
`docs/remediacao/PLANO-IMPLANTACAO.md` com **migração gradual em 4 fases** (drop-in
`User=root` transitório enquanto existirem caminhos `/root`/`/tmp` fixos; depois
`TRIAGEM_SEGREDOS_ESTRITO=1`). Assunções marcadas "conferir no servidor" em cada unit.

## OP-O3 · Logs sem rotação — PARCIAL

**Reconfirmação:** só `triagem.log` tem rotação própria
(`triagem_grupo.py:220 RotatingFileHandler 2 MB × 5`); `pergunta-rodar.sh` faz
`exec python ... >> "$LOG" 2>&1` (append em `pergunta.log`); `vigia_bot.sh` decide "bot
mudo" pelo **mtime** do `pergunta.log` ("Regra: se o pergunta.log não é escrito há
LIMITE_MIN minutos..."); `ponte.log`/`certidoes.log` não são criados por código nenhum →
origem por `StandardOutput=append:` da unit é **hipótese** da auditoria (correta a
suspeita: só o servidor confirma).

**O que mudou:** `deploy/logrotate/triagem-bitrix` com `ponte.log`, `pergunta.log`,
`certidoes.log`, `pintar-linha.log`, `pergunta-disparo.log` e `vigia-bot-ultimo.txt`:
`daily`, `rotate 14`, `maxsize 20M`, `compress`, `delaycompress`, `missingok`,
`notifempty`, **`copytruncate`** (comentado no arquivo: rename/create enganaria o vigia e
perderia o inode aberto pelo bot). `triagem.log` ficou de fora por já ter rotação.
Teste: `logrotate -d` **não existe no macOS** → registrar "verificar no servidor"
(junto com `logrotate -f` da primeira execução).

**Divergência:** o bloco sugerido na auditoria (06-testes-operacao.md, O3) usava
`su root root` e não cobria `pergunta-disparo.log`/`vigia-bot-ultimo.txt` (criados depois,
pela onda 1/PT-04); mantive `su root root` com nota para ajustar a `triagem:triagem` na
Fase 2.

## OP-O4 · Exit codes engolidos + proposta de units — PARCIAL

**Reconfirmação (com e sem a edição paralela da Frente X):**

- `certidoes-rodar.sh` no baseline terminava com `echo "... (exit $?)"` → saía sempre 0.
  **Durante este trabalho a Frente X corrigiu** (diff atual: `set -euo pipefail`,
  `code=0; ... || code=$?; exit "$code"`) — fechado por ela, registrado aqui.
- `baixar_pendentes.py:146` — `return 0` mesmo com `erros > 0` (confirmado).
- `reconciliar_estado.py:92` — `return 0` sempre; e `:91` tem o ternário impossível
  `{lotas_ok if False else lotes_ok}` (confirmado).
- `pipeline.sh` — `set -u` sem `-e`/`pipefail`, cada passo vira `| tail -N` (exit do
  `tail`), e o passo 4 usa `--todos-os-fechamentos` (desliga a regra do escritório).
- Em contraste, `ponte-rodar.sh` já fazia `code=$?; exit $code` e os wrappers da PT
  (`pergunta-rodar.sh`, `vigia_bot.sh`) corrigidos na onda 1 propagam exit real.

**O que mudou (meus arquivos):** `deploy/systemd/` completo (11 arquivos) — cada service
com `OnFailure=triagem-alerta@%n.service`, `TimeoutStartSec` coerente com a cadência
(28 min < 10 min × 3; 4 min na ponte/certidões; 90 s no pintar-linha; 15 min na limpeza),
`ExecStart` que **não** engole exit (`flock` + comando direto; `SuccessExitStatus=75` e
`RestartPreventExitStatus=75` no bot, que sinaliza instância duplicada com 75 — PT-02);
`deploy/bin/lote-fila.sh` (substituto proposto do `pipeline.sh`: `set -euo pipefail`,
`timeout` por passo, sem `--todos-os-fechamentos`); `deploy/bin/alertar-falha.sh`
(template de alerta). **Flock coerente:** lote-fila e ponte compartilham
`$BASE/fila.lock` (`flock -n`, pula se ocupado; tomada por `ponte-rodar.sh`/`deploy/bin/lote-fila.sh`
e por dentro do `lote_para_fila.py` — antes `/var/lock/triagem-bitrix-ponte.lock`) porque ambos gravam
`fila-enviados.json`; ordem única **fila → estado** (estado é trava curta do
`estado_store` dentro do Python); nunca duas travas de shell; **sem lock aninhado sobre o
mesmo arquivo** (armadilha em que o wrapper self-locking `ponte-rodar.sh`/`certidoes-rodar.sh`
dentro de um `flock` pularia sempre — documentado em `deploy/systemd/README.md`).
Premissas marcadas "conferir no servidor" em todas as units (não temos as reais).

## OP-E1 · Divisão do `triagem_grupo.py` (4.116 linhas) — SÓ DOCUMENTO

Reconfirmado o mapa de seções/funções no código atual. Tabela da auditoria com âncoras
reais (as seções existem como comentários; corte direto, sem mudar lógica; manter
`triagem_grupo.py` como fachada reexportando o que os testes importam):

| novo módulo | linhas reais | conteúdo |
|---|---|---|
| `triagem/config.py` | 82–209 + 394–464 + **465–547** | constantes; `ler_chave_bot`, `candidatos_webhook`, `descobrir_webhook`, `ler_bot_config`; `carregar_config` |
| `triagem/http.py` | 233–393 | `_throttle_bitrix`, `_abrir_com_teto`, `_requisicao`, `espera_429`, `http_json`, `baixar_arquivo` |
| `triagem/sinais.py` | 548–625 | `chars_de_traco`, `dialogos`, `modo_traco`, `e_sinal_traco` |
| `triagem/cpf.py` | 626–750 | `cpf_normalizar`, `cpf_plausivel`, `cpf_formatado`, `extrair_cpfs*` |
| `triagem/texto_nome.py` | 751–1035 | normalização de nome (junto de `nome_regras.py`) |
| `triagem/mensagens.py` | 1036–1161 | `_parse_data`, `file_ids_da_mensagem`, `classificar_mensagem`, mapas |
| `triagem/fonte_api.py` | 1162–1237 | `api_get`, `ler_pagina_mensagens`, `buscar_mensagens_api` |
| `triagem/fonte_webhook.py` | 1238–1303 | `buscar_mensagens`, `ler_eventos` |
| `triagem/agrupador.py` | 1304–2156 (~850 l.) | montagem de lotes; 2ª etapa: `abertura.py`/`fechamento.py`/`identidade.py` |
| `triagem/download.py` | 2157–2471 | `nome_ofuscado`…`baixar_file_id`, `_enfileirar_retry`, `baixar_file_id_api` (avaliar troca por `download_svc`/`bitrix_cliente`) |
| `triagem/estado.py` | 2472–2555 | `estado_vazio`, `carregar_estado`, `salvar_estado`, `serializar_lote` — gravação só via `estado_store` |
| `triagem/breaker_fontes.py` | 2556–2608 | `_breaker_int/float`, `_ler_mensagens_com_breaker` |
| `triagem/ciclo.py` | 2609–2961 | `ciclo`, `imprimir_resumo` |
| `testes/test_cpf_extrator.py`, `test_sinais_traco.py` | 2962–3788 | testes embutidos → pytest (ver OP-T5) |
| `tools/provar_fonte.py` | 3789–3931 | `--testar-fonte` |
| `triagem_grupo.py` (CLI) | 3932–4116 | `montar_parser`, `trava_de_ciclo`, `main`, `_main_com_guarda` |

**Divergências com a tabela da auditoria (06/E1):** (a) `carregar_config` está em :524 —
a faixa 465–625 dada a `sinais.py` na auditoria inclui o carregamento do config; propus
465–547 para `config.py` e 548–625 para `sinais.py`; (b) a faixa 2272–2555 dada a
`estado.py` começa, na verdade, num segundo bloco intitulado `# Estado` (:2273) que
contém a fila de retry do download (`_enfileirar_retry`, `baixar_file_id_api`) — os
**dois títulos "Estado" duplicados existem** (:2273 e :2474), confirmando o achado, mas o
corte de `download.py` deve ir até 2471. Semântica preservada em ambos.

## OP-E2 · Código morto — SÓ RELATÓRIO

Verificado item a item por grep em todo o repo (pós-onda 1). "só definição" = o nome não
aparece em mais nenhum lugar além da definição/comentário/teste que o cita:

| Item (linha atual) | Verificação |
|---|---|
| `triagem_grupo.py:2500 salvar_estado` | **sem chamadas** (só comentário :2861) — perigoso se reusado: grava sem trava. Remover |
| `triagem_grupo.py:2209 baixar_file_id` | só definição (o usado é `baixar_file_id_api`) |
| `triagem_grupo.py:96 DIALOG_IDS_PADRAO`, `:133 BOT_ID_AULA` | só definição |
| `triagem_grupo.py:1627 cpf_msg`, `:1628 nomes_msg` | atribuídas e não usadas nesse laço (`nomes_msg` é usada em outra função, :1470) |
| `triagem_grupo.py:205 PALAVRAS_NAO_NOME` | importado sem uso funcional — hoje com `# noqa` como "alias do contrato"; decidir se vira reexport ou sai |
| `lote_para_fila.py:54 WORKER_SVC`, `:274 WorkerOcupado`, `:278 esperar_ocioso` | resto do modo "parar o worker" revertido em 24/09; `test_ponte_fila.py:58-59` exige que continuem fora de uso |
| `lote_para_job.py:323 semanas_para_meses`, `:556 RE_SERVICO_CONTRATADO`, `:722 servico_contratado_dos_pdfs`, `:85 CAMPOS_FICHA`, `:147 RX_LINHA_SEM_SEP` | só definição (CAMPOS_FICHA: conferir se a montagem da ficha usa outro caminho antes de apagar) |
| `pergunta_traco.py:622 falar_desfecho` (auditoria dizia :351 — linha andou com a onda 1) | só definição |
| `telegram_grupo.py:400 decifrar_batidas_para_log` | só definição |
| `ponte_criterio.py:15 FECHAMENTOS_FALLBACK` | só definição |
| `lotes_regras.py:16 STATUS_FINAIS` | só definição |
| `relatorio.py:32 SINAL_TELEGRAM` | só definição |
| `reconciliar_estado.py:91` `{lotas_ok if False else lotes_ok}` | ternário impossível (resto de typo) |
| `completar_cpf_por_nome.py:23` importa `WEBHOOK` por efeito colateral | confirmado (herda o SystemExit do import de `nomear_lotes_por_cpf`) |
| `dividir_lote_misto.py` (`json` :20, `filenames` :114) | confirmados não usados |

Nada disso foi removido (arquivos alheios): a lista é para o orquestrador decidir com os
donos. Sugestão: remover `salvar_estado` primeiro (risco de reuso sem trava).

## OP-T5 · Testes embutidos fora do pytest — SÓ RELATÓRIO

Confirmado: `triagem_grupo.py:2965 testar_cpf` (14 casos), `:3001 testar_sinais` (31
casos), `:3792 testar_fonte` (usa rede) e `lote_para_fila.py:462 testar` (3 casos de
ficha) — ~970 linhas que não rodam no `pytest`, só manualmente via CLI. Proposta (igual à
auditoria, com detalhe): mover `testar_cpf`/`testar_sinais` para
`testes/test_cpf_extrator.py` e `testes/test_sinais_traco.py` com `@pytest.mark.parametrize`
(casos idênticos, sem rede), `testar_fonte` para `tools/provar_fonte.py` (ferramenta
manual de rede, fora da suíte) e `lote_para_fila.testar` para
`testes/test_ficha_traducao.py`. Manter os atalhos de CLI apontando para os testes por um
ciclo, e só depois remover. Arquivos são de outros donos → pendência.

---

## Validação do que foi entregue (saída real)

```
$ bash -n deploy/bin/alertar-falha.sh   → OK (exit 0)
$ bash -n deploy/bin/lote-fila.sh       → OK (exit 0)
$ bash -n deploy/bin/verificar-ambiente.sh → OK (exit 0)
$ command -v shellcheck → (vazio) "shellcheck NAO disponivel neste host (macOS) — validar no servidor"
$ python3 -m py_compile deploy/bin/podar-backups.py → OK (exit 0)
```

- `systemd-analyze verify` **não existe no macOS** → "verificar no servidor" (o comando
  está no checklist do PLANO/RUNBOOK).
- Teste funcional do `podar-backups.py` com fixture sintética: simulação não apaga nada;
  `--descarte-seguro` sem autorização sai 2; `--aplicar` remove só operacionais;
  `--descarte-seguro --autorizado-por` remove legados com rastro da autorização;
  `.corrompido-*` e dumps nunca são tocados (saídas completas na seção S05). Durante o
  teste **encontrei e corrigi um defeito meu**: o diretório dos backups da fila não
  acompanhava `--estado` (agora derivado do diretório do estado).
- Suíte completa `.venv/bin/python -m pytest -q` → **56 failed, 727 passed in 6.54s**
  (árvore em movimento: frentes T e X editando em paralelo). Falhas por arquivo:
  `test_telegram_*` 38 (testes novos da Frente T em desenvolvimento), `test_pintar_linha`
  4 (Frente X em desenvolvimento) e as **14 da baseline** que dependem de dados de
  produção/config (`test_variaveis_contrato` 7, `test_servico_lote` 3,
  `test_telefones_fichinha`, `test_ponte_fila`, `test_pasta_fichinha`, `test_nome_regras`).
  **Nenhuma falha toca arquivos da Frente H** (deploy/, docs/, README não entram na suíte).

## Pendências para o orquestrador (arquivos de outros donos)

1. `pipeline.sh` (orquestrador): `set -euo pipefail`, remover `--todos-os-fechamentos` da
   etapa 4, parar de engolir exit com `| tail` — ou apontar o timer para
   `deploy/bin/lote-fila.sh` (já correto).
2. `baixar_pendentes.py:146`: `return 1 if erros else 0` (hoje sempre 0).
3. `reconciliar_estado.py:91-92`: ternário impossível + `return 1 if ... else 0`.
4. S08/S09 (habilitar `User=triagem`): tirar `/root/.hermes/node` de
   `lote_para_fila.py:53` e `certidoes.py:206`; `/tmp` fixos de `lote_para_fila.py:361-367`
   e `certidoes.py:205,323` (usar `seguranca.arquivo_temp_privado`/`remover_seguro`);
   `download_svc.py:33-34` (eliminar `/tmp/b24_webhook.txt`); `telegram_grupo.py:38`.
5. `vigia_bot.sh` (PT): com `User=triagem`, `systemctl restart` precisa de `sudo`
   (sudoers pronto em `deploy/sudoers/`) ou `WatchdogSec`+`sd_notify`.
6. Locks com modo aberto: `circuit_breaker.py:351,398`, `triagem_grupo.py:4037` (0o644) e
   `rede_bitrix.py:597` (0o664) → 0o600 (ou aceitar `UMask=0077` das units e `chmod` nos
   existentes).
7. S05 definitivo: migrar os 5 `shutil.copy2(ESTADO, backup)` para
   `estado_store.fazer_backup` (retido) e `lote_para_fila.py:341-344` para política de
   fila (podar via `triagem-limpeza` já cobre o acúmulo).
8. `triagem_grupo.py:450,452` mascarar webhook em falha (S07 — `seguranca.mascarar_url`).
9. `config.json` (frente T/orquestrador): `telegram.habilitado=false` e allowlist.
10. Jornal: `MaxRetentionSec`/`SystemMaxUse` no `journald.conf`.
11. Definir o destino da alerta (`ALERTA_CMD` em `/etc/triagem-bitrix/env`) — o Telegram
    legado está desativado.
12. OP-E2/OP-T5: decidir remoção/migração com os donos (itens acima).

## Pontos `COMPAT-ONDA1`

**Nenhum.** A onda 1 foi integrada na branch durante o trabalho (aviso do orquestrador);
`deploy/bin/podar-backups.py` usa import direto (`estado_store.podar_backups`,
`seguranca.remover_seguro`) e não há fallback `# COMPAT-ONDA1` em nenhum arquivo desta
frente. (Os fallbacks `# TODO` que restam em `pergunta_traco.py`, `vigia-bot-triagem.py`,
`esperar_bot_liberar.py`, `circuit_breaker.py`, `estado_lote.py` são de outros donos —
contrato pós-onda 1 já autoriza a troca por import direto.)

## Precisa do servidor (o que `coleta-triagem.sh` deve trazer)

1. `systemctl cat` de todas as units/timers: `triagem-bitrix`, `triagem-lote-fila`,
   `triagem-bitrix-ponte`, `triagem-bitrix-certidoes`, `pintar-linha-triagem`,
   `triagem-bitrix-bot` (e o que quer que rode `vigia_bot.sh`/`monitor_lotes.py` — cron
   Hermes?); `systemctl list-timers --all`; `crontab -l` (root e usuários envolvidos).
2. `ls -la` + `du -sh` de `/opt/triagem-bitrix` (estado.json, contagem e amostra de datas
   dos `estado.json.bak-*` — identifica o gerador de 1/ciclo, O1), `backups/`, `lotes/`,
   logs, `/opt/agente-separador/data`, `/etc/systemd/system`, `/etc/logrotate.d`,
   `/etc/triagem-bitrix`.
3. `stat` (modo/dono) de `vibecode.key`, `/root/.vibecode/api_key`,
   `/root/.telegram/…token`, `/tmp/b24_webhook.txt`, `estado.json`, `staging/`; `id`;
   `getent passwd triagem`; `sysctl fs.protected_regular`; existência de outros usuários.
4. Destino real do stdout de cada unit (confirmar `StandardOutput=append:` de
   `ponte.log`/`certidoes.log`); modo real do `pintar-linha` (`--aplicar`?); se o timer de
   10 min usa `pipeline.sh`; `--loop` real do `triagem_grupo.py`.
5. Versões: SO, util-linux (`flock`, `timeout`), `logrotate`, `python3`, node de
   `/root/.hermes` (e alternativa em `/usr/bin/node`).
6. Journal/logs: presença da URL do webhook **sem máscara** (S07 — buscar sem imprimir a
   chave) em `triagem.log`/`ponte.log`/journal; `circuit-download.json`; `monitor-lotes.json`.
7. Contagens para o descarte autorizado (S05): nº de `.bak-*`, `backups/fila-jobs-*`,
   `/tmp/_entradas.json`, `/tmp/certidao_*.pdf`.

## Riscos

1. **Units propostas × reais**: podem divergir do servidor; instalar só após a coleta e
   diff (o risco é o timer novo duplicar o antigo — PLANO Fase 1 tem o passo de
   desabilitar os antigos e o rollback).
2. **Fase 1 com drop-in `User=root`**: se o drop-in não for removido na Fase 2, o
   hardening fica parcial (mitigação: checklist + `verificar-ambiente.sh`).
3. **Retenção sem saneamento (S01)**: os backups mantidos ainda contêm senha de cliente;
   a poda estanca o disco, não o vazamento — o descarte dos legados é único e autorizado.
4. **OnFailure depende de exit real**: enquanto `baixar_pendentes`/`reconciliar_estado`/
   `pipeline.sh` engolem falha, a alerta não dispara para esses passos.
5. **Trava compartilhada única**: rodada lenta do lote-fila faz a ponte pular (aceito e
   documentado; `OnUnitInactiveSec` evita empilhamento).
6. **copytruncate**: quem trocar por `create`/rename quebra o vigia do bot (comentado no
   próprio logrotate).
7. **Cota de retenção dividida** entre formatos enquanto os legados existirem (ver S05) —
   pode manter menos cópias novas do que o esperado; some quando os legados forem
   descartados.
8. `podar-backups.py` apaga com `remover_seguro` (zeragem): mais lento que `unlink` em
   massa de 27 GB — a primeira execução autorizada pode demorar (rodar em `tmux`/nohup;
  registrado no RUNBOOK §5).
