# Plano de ondas e donos (orquestrador)

Inventário: os relatórios 01–05 têm **86 achados** (01:14, 02:13, 03:23, 04:12, 05:24) e o 06 tem **15 itens operacionais** (prefixo `OP-`, porque `C1`, `C2`, `E1`–`E3` colidem com IDs do 03). O "82" do resumo era aproximação; a matriz usa os 101 IDs reais.

Ordem: Segurança base → Persistência → Identificação → Formação de lotes → Download → Ponte → Auxiliares → Testes integrados → Implantação.

## Onda 0 — orquestrador ✅
Repositório git local, branch `remediacao`, suíte portátil (conftest/pytest.ini, caminhos relativos), `caminhos.py`, REGRAS, CONTRATOS. Baseline: 287/301 (14 dependiam de produção).

## Onda 1 — produtores de contrato (paralelo, arquivos disjuntos)
| Frente | Arquivos | Achados |
|---|---|---|
| B persistência | estado_store.py, estado_lote.py, test_estado_* | A2, OP-C2, F1-08 (base), A1/A3/F1-03 (API de merge), F1-04/OP-O1 (poda) |
| S segurança-base | cpf_util.py, seguranca.py, test_cpf_util, test_seguranca | base p/ S01, S04, S06, S07, F1-09, ID-05 |
| R rede | rede_bitrix.py, circuit_breaker.py, test_rede_bitrix, test_circuit_breaker* | base p/ B1–B4, D1, D2, D3, G1 |
| PT bot do traço | pergunta_traco.py, pergunta-rodar.sh, vigia-bot-triagem.py, vigia_bot.sh, esperar_bot_liberar.py, testes do bot/vigia | PT-01..04, H1, H2 |

## Onda 2 — consumidores (paralelo, arquivos disjuntos)
| Frente | Arquivos | Achados |
|---|---|---|
| J job/identidade da ficha | lote_para_job.py, nomear_lotes_por_cpf.py, nome_regras.py, test_variaveis_contrato, test_servico_lote, test_telefones_fichinha, test_pasta_fichinha, test_nome_regras, test_titulo_ficha, test_nome_do_cliente_prefixo | S01 (extração/persistência no job), ID-09, AUX-02, F2-09, NR-01, OP-T2, OP-T3 |
| E ponte | lote_para_fila.py, ponte_criterio.py, monitor_lotes.py, relatorio.py, avisar-primeiro.py, pipeline.sh, ponte-rodar.sh, testes ponte/monitor/relatorio/limpeza | F2-01..08, F2-10, F2-12, A4, S02 (lote_para_fila), S04 (ponte), S09 (lote_para_fila /tmp js), S10, OP-O2, OP-T4 |
| F identificação/reconciliação | completar_cpf_por_nome.py, reconciliar_estado.py, reconciliar_arquivos_disco.py, reconciliar_por_file_ids.py, backfill_nome_lotes.py, dividir_lote_misto.py, reparo_josilene.py → `manutencao/` | ID-01..04, S11, F1-04, F1-12, A1, E4 (reconciliadores), E5, F2-11, AUX-01, OP-C1, OP-O1, OP-E3 (parte) |
| D download | baixar_pendentes.py, download_svc.py, checar_midias.py, test_download_svc, test_guarda_dns, test_429_operating | A5, D1, D2, E1, E4 (download_svc), G1, S14, OP-O5, B3 (consumidores) |
| C1 triagem (segurança/persistência/download) | triagem_grupo.py | S01 (mensagens), S06, S07, A3/F1-03, F1-08, B1, B2, B4, C1, D3, E3, G2 |
| X certidões/planilha | certidoes.py, certidoes-rodar.sh, pintar_linha.py, backup_pintura.py, conferir_pintura.py, test_pintar_linha | CE-01..05, S09 (certidoes), S12, S13, PL-01..03 |
| T telegram legado | telegram_grupo.py, telegram_regras.py, config.json, testes telegram_* | S03, TG-01..03, C2, E2 |

## Onda 3
| Frente | Arquivos | Achados |
|---|---|---|
| C2 fase 1 | triagem_grupo.py, lotes_regras.py | F1-01, F1-02, F1-05, F1-06, F1-07, F1-09, F1-10, F1-11, F1-13 |
| G cenário integral | testes/test_cenario_integral.py (novo) | 12.3 do prompt |
| H infra | deploy/ (systemd, logrotate, poda), README.md | S05, S08, OP-O3, OP-O4, OP-E1 (doc), OP-E2, OP-T5 |

## Harness 2 (Mimo 2.6 Pro) — worktree `../triagem-bitrix-mimo`, branch `mimo/auxiliares`
Prompt: `~/Downloads/PROMPT-MIMO-FRENTES.md`. Frentes **X** (certidões/planilha), **T** (Telegram legado) e **H** (deploy/, README, rascunhos de runbook/implantação) saem do plano deste worktree: nenhuma onda daqui toca `certidoes.py`, `certidoes-rodar.sh`, `pintar_linha.py`, `backup_pintura.py`, `conferir_pintura.py`, `telegram_grupo.py`, `telegram_regras.py`, `testes/test_telegram_*.py`, `testes/test_pintar_linha*.py`, `deploy/`, `README.md`. `config.json` continua do orquestrador. Integração: merge `mimo/auxiliares` → `remediacao` na barreira, depois de remover os `COMPAT-ONDA1`.

## Onda 4
Revisor independente (I) + matriz + documentos finais (orquestrador).
