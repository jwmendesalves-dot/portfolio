# Revisão final independente — foco 4: qualidade dos testes

- Branch `remediacao`, HEAD `ce6cc54` (sem mudança durante a revisão).
- Revisor somente leitura: nenhum arquivo de código ou teste editado, sem git de escrita, sem rede.
- Cópias de trabalho e scripts: `~/.hermes/cache/scratch/revfinal4/` (`prova.sh`, `mutar.py`, `varrer.py`).

## Veredito do foco

**APROVADO COM RESSALVAS.**

Pontos fortes:
- A suíte é determinística. Dá o mesmo resultado em 3 execuções seguidas, em ordem reversa e com cada arquivo rodado isolado.
- Nenhum teste escreve no repositório nem no `$HOME`.
- Todos os testes amostrados ficam vermelhos no código antigo.
- Os 6 mutantes da J2 foram mortos.

Ressalvas, todas de severidade baixa ou informativa:
- vazamento de diretórios temporários (R1);
- um skip que nunca roda em lugar nenhum (R2);
- os testes da C2a, da E e parte da C2b/Z3 só ficam vermelhos no `fbc440c` por mudança de API (R3);
- 82 avisos do pyflakes nos testes (R4).

Nenhum achado bloqueia a branch.

## Baseline e estabilidade (ordem/tempo)

`pytest-randomly` **não está instalado** no `.venv` (`pip list`: só pytest 8.4.2, pytest-timeout 2.4.0, pyflakes 3.4.0). Por isso não houve teste com seeds aleatórias. A ordem foi exercitada de três outros jeitos: ordem normal, ordem reversa e cada arquivo isolado.

```
$ .venv/bin/python -m pytest -q -p no:randomly -rsx        (início)
1282 passed, 2 skipped, 1 xfailed in 18.32s
$ for i in 1 2 3; do pytest -q -p no:randomly | tail -1; done
1282 passed, 2 skipped, 1 xfailed in 18.06s
1282 passed, 2 skipped, 1 xfailed in 17.84s
1282 passed, 2 skipped, 1 xfailed in 17.84s
$ pytest -q $(ls testes/test_*.py | sort -r)               (ordem reversa de arquivos)
1282 passed, 2 skipped, 1 xfailed in 18.10s
$ for f in testes/test_*.py; do pytest -q $f; done         (cada arquivo isolado)
nenhum arquivo com failed/error (o único casamento foi "1 xfailed" de test_config_consumidores.py)
$ pytest -q                                                 (fim)
1282 passed, 2 skipped, 1 xfailed in 18.04s
```

Conclusão: não foi encontrada dependência de ordem nem de tempo. Fica a ressalva de que a granularidade de ordem testada foi por arquivo, não por teste dentro do arquivo.

## Skips e xfail

| Item | Legítimo? | Observação |
|---|---|---|
| SKIP `test_job_senha_s01.py:185` `test_pdftotext_real_le_a_tabela_do_contrato_sintetico` (sem `pdftotext`) | Sim | Depende do ambiente: `which pdftotext` → não encontrado nesta máquina. O caminho real de PDF da S01 nunca roda aqui. O resto da S01 é coberto com `_texto_pdf` falso. Sugestão: `brew install poppler` na máquina de CI/dev, ou rodar no servidor. |
| SKIP `test_ponte_fila.py:70` `test_persistencia_atomica_vem_do_filajobs_do_painel` | **Parcial** — ver R2 | O `conftest.py:21` força `AGENTE_SEPARADOR_BASE` para um tmp vazio. Por isso o teste **nunca** roda, nem no servidor. Além disso, ele só procura texto (`"renameSync" in fonte`). |
| XFAIL(strict) `test_config_consumidores.py:226` `test_cabecalho_com_cpf_valido_e_nome_curto_fecha_pelo_traco` | Sim | É uma lacuna de regra documentada: nome com menos de 5 letras é recusado por `parece_ruido`. O dono está nomeado (agrupamento/`nome_regras`) e está registrado em `frentes/C3-config-consumidores.md`. Como é `strict=True`, o teste avisa quando a lacuna for corrigida. Não está esquecido. |

## Prova vermelha (reverter produção para `fbc440c` e rodar o teste)

Método: copiar a árvore para `revfinal4/arv-<rótulo>`, trocar os arquivos de produção da frente por `git show <ref>:<arquivo>` e rodar os testes selecionados com o `.venv` do repo. Quando o vermelho no `fbc440c` vinha só de `AttributeError` (API nova), também reverti para o **commit pai da frente**, para separar defeito real de API nova.

| Frente | Ref | Teste | Resultado | Tipo do vermelho |
|---|---|---|---|---|
| C2a | fbc440c | `test_fase1_lotes_estaveis::test_simulacao_3_kits_janela_andando_vira_3_lotes` | FAILED | API: `triagem_grupo has no attribute 'bitrix_cliente'` (fixture) |
| C2a | fbc440c | `test_fase1_lotes_estaveis::test_rodar_o_ciclo_3x_com_a_mesma_fonte_nao_cria_lote_novo` | FAILED | API (idem) |
| C2a | fbc440c | `test_fase1_cursor::test_cursor_nao_avanca_quando_a_gravacao_falha` | FAILED | API (idem) |
| C2a | fbc440c | `test_fase1_cursor::test_lacuna_na_api_nao_avanca_cursor_e_registra` | FAILED | API (idem) |
| C2b | fbc440c e a927112~1 | `test_fase1_identidade::test_saudacao_e_frase_de_servico_nao_sao_nome` | FAILED | **Comportamento**: `assert ['bom dia'] == []` |
| C2b | fbc440c e a927112~1 | `test_fase1_identidade::test_cpf_com_pontuacao_e_dv_errado_nao_e_cpf` | FAILED | **Comportamento**: `assert ['85527451601'] == []` |
| C2b | fbc440c e a927112~1 | `test_fase1_config::test_config_invalida_levanta_em_vez_de_cair_nos_padroes` (5 casos) | FAILED | API: sem `ConfigInvalida`. É o próprio contrato novo, então é aceitável |
| C4 | 3b958cf~1 | `test_g_correcoes::test_g01_parse_data_aceita_datetime_aware` | FAILED | **Comportamento**: `assert None is datetime(...)` |
| C4 | 3b958cf~1 | `test_g_correcoes::test_g02_ponte_trata_erro_tipo_midia_como_midia` | FAILED | **Comportamento**: `'desconhecido' == 'midia'` |
| C4 | 3b958cf~1 | `test_g_correcoes::test_g03_access_denied_no_ciclo_e_permanente_e_nao_vai_ao_retry` | FAILED | **Comportamento** (`baixado False`, vai ao retry) |
| C4 | 3b958cf~1 | `test_g_correcoes::test_cpfs_invalidos_tem_dono_declarado` | FAILED | **Comportamento**: `None == 'triagem'` |
| C4 | 3b958cf~1 | `test_g_correcoes::test_g03_erro_de_sistema_nao_marca_arquivo_permanente` (401/503) | PASSED | Esperado: é guarda de não-regressão ("não punir erro de sistema"), não prova de defeito. Forma par com o teste de ACCESS_DENIED, que fica vermelho. |
| C4 | fbc440c | os 5 acima | 3 failed + 3 errors | API (`PROPRIEDADE_CAMPOS`, `tipo_arquivo`, `_MIGRACAO_CIRCUITO`) |
| E | fbc440c | `test_ponte_rodada::test_kit_com_documento_nao_baixado_nao_vai_parcial` | ERROR | API: `lote_para_fila has no attribute 'ARQ_ESTADO'` (setup) |
| E | fbc440c | `test_ponte_rodada::test_duplicata_com_download_pendente_e_indeterminada` | ERROR | API (idem) |
| E | fbc440c | `test_ponte_rodada::test_limite_conta_so_enviaveis_e_traco_vai_primeiro` | ERROR | API (idem) |
| D | fbc440c | `test_download_integridade::test_truncado_nao_e_aceito` | FAILED | **Comportamento**: "arquivo truncado não pode virar o destino" |
| D | fbc440c | `test_download_integridade::test_access_denied_permanente_sem_retry` | FAILED | **Comportamento**: `'pendente' == 'falha_permanente'` |
| D | fbc440c | `test_baixar_pendentes::test_parcial_orfao_nao_bloqueia_o_indice` | FAILED | **Comportamento** (`FileNotFoundError` no caminho final) |
| Z3 | fbc440c | `test_telegram_config_invalida::test_uma_vez_config_invalido_sai_3_mascarado_sem_api` | FAILED | **Comportamento**: "não podia ser chamado com config inválido" (a API foi chamada) |
| Z3 | fbc440c | `test_telegram_config_invalida::test_loop_config_invalido_pausa_crescente_e_segue_quando_valido` | FAILED | **Comportamento** (idem) |
| Z3 | fbc440c e 2916753~1 | `test_telegram_config_invalida::test_backoff_tem_teto` | FAILED | API: sem `carregar_config_valida` |
| J2 | — | `decisoes_jev.py`/`typesafe_cliente.py` não existem no `fbc440c`. Por isso a prova foi por **mutação** (tabela abaixo) | — | — |

### Mutantes da J2 (`mutar.py`, uma troca por vez)

```
M1 promove dono desconhecido (REVISAO->KIT): 1 failed  test_jev_nunca_promove_dono_desconhecido[mesmo_cliente-0.99-0.0]
M2 limiar 0.85->0.5:                         3 failed  test_decide_confianca_baixa_vai_a_revisao, test_foto_sem_cpf_nunca_aceita[...0.5...], test_lote_misturado[separar-0.7-0-...]
M3 separar com doc sem dono (guarda off):    2 failed  test_lote_misturado[separar-0.9-2-...], [separar-0.9-None-...]
M4 foto aceita automático:                   1 failed  test_foto_sem_cpf_nunca_aceita[provavelmente_do_cliente-0.9-...]
M5 401 vira transitório:                     1 failed  test_401_credencial_sem_retry_e_sem_chave
M6 sem teto no Retry-After:                  1 failed  test_retry_after_respeitado_com_teto
```

Os 6 de 6 mutantes foram mortos. Os testes da J2 cobrem a regra de ouro e os limites.

## Asserções fracas e mocks que reimplementam a regra

A varredura AST (`varrer.py`) procurou testes sem nenhuma asserção e testes cujas asserções são todas do tipo `is not None`, `isinstance`, verdade simples ou `len > 0`:

```
SEM ASSERÇÃO DIRETA (3):
testes/test_decisoes_jev.py:232 test_perguntas_montadas_tem_shape_valido
testes/test_seguranca.py:554 test_remover_seguro_inexistente_nao_falha
testes/test_typesafe_cliente.py:104 test_noul_com_criteria_true_false_e_valido
SÓ ASSERÇÃO FRACA (0)
```

Os 3 são testes de "não levanta" sobre funções que levantam quando há defeito (`validar_questoes`, `remover_seguro`). São legítimos. Fica a sugestão cosmética de comentar a intenção.

Mocks: procurei `monkeypatch.setattr` sobre funções de regra (`pode_enfileirar`, `agrupar`, `classificar*`, `identific*`, `tipo_arquivo`, `parse_data`…). Só aparece `tg.agrupar` em `test_triagem_persistencia.py:53,166`, que testa **persistência**, não agrupamento. Não encontrei mock que reimplemente a regra testada.

## Achados

### R1 — Diretórios temporários vazam a cada execução · baixa
- **Onde:**
  - `testes/conftest.py:19` (`tempfile.mkdtemp(prefix="triagem-teste-")`, nunca removido);
  - `testes/test_circuit_breaker.py:51` (`cb_`);
  - `testes/test_rede_bitrix.py:293` (`rl_`);
  - `testes/test_envio_bot_bloqueio.py:56` (`traco-teste-`);
  - `testes/test_pergunta_traco_envio.py:103` (o `tearDown` na linha 114 só faz `chmod`, sem `rmtree`), `:423`, `:471`;
  - `testes/test_vigia_bot_triagem.py:33`;
  - `testes/test_inventario_estado.py:234`.
- **Reprodução:** contei os diretórios por prefixo em `$TMPDIR` antes e depois de uma execução:
  ```
  antes : triagem-teste-=2746 cb_=2812 traco-teste-=864 traco-envio-=3208 traco-sh-=396 esperar-=774 rl_=889 vigia-=1056
  depois: triagem-teste-=2763 cb_=2831 traco-teste-=870 traco-envio-=3230 traco-sh-=399 esperar-=780 rl_=895 vigia-=1064
  ```
  São cerca de 70 a 90 diretórios por execução. A variação de `triagem-teste-` inclui as execuções paralelas de outros revisores. O total hoje é de 2,3 MB só em `triagem-teste-*`.
- **Impacto:** tudo fica em `$TMPDIR`, nunca no repo nem em `$HOME`. Não há risco a dado real; o problema é acúmulo. No servidor, se a suíte rodar como root em `/tmp`, pode deixar PII sintética espalhada.
- **Correção:**
  - no conftest: `atexit.register(shutil.rmtree, _BASE_TESTE, ignore_errors=True)`;
  - nos demais: trocar `mkdtemp` por `tmp_path` (pytest) ou `self.addCleanup(shutil.rmtree, self.tmp, True)` (unittest).

### R2 — Teste de atomicidade da fila nunca roda · baixa
- **Onde:** `testes/test_ponte_fila.py:63-75` combinado com `testes/conftest.py:21`.
- **Reprodução:** o motivo do skip aponta para `$TMPDIR/triagem-teste-*/agente-separador/src/fila-jobs.js`. O conftest força `AGENTE_SEPARADOR_BASE` para o tmp da sessão (com `setdefault`), então o arquivo nunca existe, nem no servidor, a menos que a variável já venha exportada. A checagem também é por texto (`"renameSync" in fonte`).
- **Correção:** tirar o teste da suíte e levá-lo ao checklist do servidor (`grep -n renameSync /opt/agente-separador/src/fila-jobs.js`), ou ler um caminho explícito (`TRIAGEM_PAINEL_REAL`) com skip declarado "só no servidor". Registrar no PLANO-IMPLANTACAO.

### R3 — Prova vermelha de C2a, E e parte de C2b/Z3 é só por API · informativa
- **Onde:**
  - `testes/test_fase1_cursor.py:127` (`usar_fonte` faz `setattr(tg.bitrix_cliente, ...)`);
  - `testes/test_ponte_rodada.py` (fixture faz `setattr(lote_para_fila, "ARQ_ESTADO", ...)`);
  - `testes/test_fase1_config.py:47`;
  - `testes/test_telegram_config_invalida.py:78`.
- **Reprodução:** tabela acima. No `fbc440c` esses testes falham no setup, por `AttributeError`, antes de exercitar a regra.
- **Impacto:** não prova que o teste passaria no código quebrado; ele é forte por leitura (ex.: `test_simulacao_3_kits...` compara com `agrupar` sobre a conversa inteira e verifica que não houve fragmentos). Mas o vermelho observado não demonstra o defeito F1-01/F1-02 nem o da E. A C2b, a C4, a D e a Z3 têm vermelho comportamental em outros testes da mesma frente.
- **Correção (opcional):** para a C2a e a E, um teste-ponte que exercite o mesmo cenário pela API pública antiga (ou um adaptador mínimo no tmp) para reproduzir "3 kits → N>3 lotes" no `fbc440c`. Alternativa: registrar no relatório da frente a contagem real do `sim_fase1.py` antigo como evidência.

### R4 — pyflakes: 82 avisos, todos em `testes/` · informativa
- Produção e `manutencao/`: **zero** avisos.
- Testes: imports sem uso (`os`, `sys`, `io`, `pytest`), `'b'`/`'url'` atribuídos e nunca usados (`test_circuit_breaker.py:156`, `test_download_integridade.py:39`), e "redefinition of unused 'rede'/'ambiente'/'amb'". Este último é falso positivo do padrão de fixture importada (`from fakes_download import rede` + parâmetro `rede`). O `noqa: F401` não é lido pelo pyflakes.
- **Correção:**
  - mover as fixtures compartilhadas (`rede`, `ambiente`, `amb`) para o `conftest.py`, o que elimina cerca de 50 avisos;
  - apagar os imports sem uso.

Saída integral:

```
$ .venv/bin/python -m pyflakes *.py manutencao/*.py testes/*.py
testes/test_baixar_pendentes.py:13:1: 'fakes_download.rede' imported but unused
testes/test_baixar_pendentes.py:49:46: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:60:54: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:70:49: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:81:44: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:97:36: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:108:49: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:120:63: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:133:52: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:142:62: redefinition of unused 'rede' from line 13
testes/test_baixar_pendentes.py:152:40: redefinition of unused 'rede' from line 13
testes/test_bitrix_cliente.py:3:1: 'os' imported but unused
testes/test_certidoes_rodar.py:9:1: 'sys' imported but unused
testes/test_circuit_breaker.py:26:1: redefinition of unused 'os' from line 23
testes/test_circuit_breaker.py:156:5: local variable 'b' is assigned to but never used
testes/test_download_integridade.py:13:1: 'fakes_download.rede' imported but unused
testes/test_download_integridade.py:26:32: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:38:30: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:39:5: local variable 'url' is assigned to but never used
testes/test_download_integridade.py:48:46: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:60:36: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:70:49: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:85:49: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:101:45: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:113:53: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:131:37: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:146:47: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:157:47: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:165:63: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:178:29: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:202:57: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:216:43: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:232:52: redefinition of unused 'rede' from line 13
testes/test_download_integridade.py:245:58: redefinition of unused 'rede' from line 13
testes/test_download_svc.py:16:1: 'fakes_download.rede' imported but unused
testes/test_download_svc.py:73:72: redefinition of unused 'rede' from line 16
testes/test_download_svc.py:90:53: redefinition of unused 'rede' from line 16
testes/test_download_svc.py:109:53: redefinition of unused 'rede' from line 16
testes/test_download_svc.py:122:47: redefinition of unused 'rede' from line 16
testes/test_download_svc.py:137:32: redefinition of unused 'rede' from line 16
testes/test_envio_bot_bloqueio.py:25:1: redefinition of unused 'os' from line 21
testes/test_fase1_lotes_estaveis.py:12:1: 'test_fase1_cursor.ambiente' imported but unused
testes/test_fase1_lotes_estaveis.py:29:55: redefinition of unused 'ambiente' from line 12
testes/test_fase1_lotes_estaveis.py:50:64: redefinition of unused 'ambiente' from line 12
testes/test_fase1_lotes_estaveis.py:74:57: redefinition of unused 'ambiente' from line 12
testes/test_fase1_lotes_estaveis.py:93:66: redefinition of unused 'ambiente' from line 12
testes/test_fase1_lotes_estaveis.py:125:39: redefinition of unused 'ambiente' from line 12
testes/test_fase1_lotes_estaveis.py:137:57: redefinition of unused 'ambiente' from line 12
testes/test_fase1_lotes_estaveis.py:153:51: redefinition of unused 'ambiente' from line 12
testes/test_guarda_dns.py:8:1: 'os' imported but unused
testes/test_limpeza_servico.py:4:1: 'os' imported but unused
testes/test_lotes_regras.py:11:1: 'os' imported but unused
testes/test_pintar_linha_planilha.py:14:1: 'pytest' imported but unused
testes/test_ponte_criterio.py:8:1: 'os' imported but unused
testes/test_ponte_pendencias.py:24:1: 'test_ponte_rodada.amb' imported but unused
testes/test_ponte_pendencias.py:28:58: redefinition of unused 'amb' from line 24
testes/test_ponte_pendencias.py:37:50: redefinition of unused 'amb' from line 24
testes/test_ponte_pendencias.py:48:56: redefinition of unused 'amb' from line 24
testes/test_relatorio.py:12:1: 'os' imported but unused
testes/test_telegram_autorizacao.py:17:1: 'os' imported but unused
testes/test_telegram_batidas.py:13:1: 'os' imported but unused
testes/test_telegram_confirma.py:16:1: 'os' imported but unused
testes/test_telegram_desligado.py:10:1: 'os' imported but unused
testes/test_telegram_download.py:13:1: 'io' imported but unused
testes/test_telegram_download.py:14:1: redefinition of unused 'os' from line 10
testes/test_telegram_grupo.py:7:1: 'os' imported but unused
testes/test_telegram_memoria.py:15:1: 'os' imported but unused
testes/test_telegram_regras.py:10:1: 'os' imported but unused
testes/test_telegram_respostas.py:9:1: 'os' imported but unused
testes/test_triagem_persistencia.py:10:1: 'os' imported but unused
testes/test_vigia_bot_transitorio.py:13:1: redefinition of unused 'os' from line 9
testes/test_vigia_bot_triagem.py:13:1: redefinition of unused 'os' from line 10
(exit 1)
```

## Verificações que passaram
- Suíte estável: 3 execuções seguidas iguais, ordem reversa igual, todos os arquivos verdes isolados.
- Escritas fora de `tmp_path`: depois da suíte, `git status --short --ignored` não mostra nada novo no repo. O `find ~ -maxdepth 2 -newer marcador` não encontrou nada atribuível à suíte. `/tmp` e `/var/tmp` não foram tocados; tudo vai para `$TMPDIR` (ver R1).
- Rede: `conftest.py` bloqueia `socket.connect` para fora de localhost.
- Prova vermelha comportamental confirmada em C2b, C4, D e Z3. Os 6 mutantes da J2 foram mortos.
- Nenhum teste só com asserção fraca. Nenhum mock que reimplemente a regra testada.
- O xfail é estrito, documentado e tem dono.

## O que NÃO testei
- Seeds aleatórias com `pytest-randomly` (não instalado) e ordem aleatória **dentro** de cada arquivo.
- Suíte rodando no servidor (Python, poppler e paths reais), e o teste do `pdftotext` real.
- Cobertura de linhas (`coverage` não instalado). Não medi o que fica sem teste.
- Prova vermelha comportamental da C2a e da E no `fbc440c` (R3).
- As frentes fora da amostra (B, C1, C3, F, G, H, J, PT, R, S, T, X, Z, Z2, DSH): não amostradas aqui.

## O que corrigir (todos opcionais, nenhum bloqueante)
1. `testes/conftest.py:19` e os `mkdtemp` listados em R1 → limpeza com `atexit`/`addCleanup`/`tmp_path`.
2. `testes/test_ponte_fila.py:63-75` → passar para o checklist do servidor ou para um caminho explícito só do servidor (R2).
3. Fixtures `rede`/`ambiente`/`amb` → `conftest.py`, e remover os imports sem uso (R4).
4. (Opcional) Teste-ponte com vermelho comportamental para C2a/E no código antigo (R3).
