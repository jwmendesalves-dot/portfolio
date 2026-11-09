# Frente E — Ponte para a fila do painel e deduplicação (onda 2)

Status: **CONCLUÍDO** (código + testes; o comportamento real do `FilaJobs` precisa ser conferido no servidor).

## Arquivos
Alterados: `lote_para_fila.py` (reescrito sobre os contratos), `ponte_criterio.py`, `monitor_lotes.py`,
`avisar-primeiro.py`, `relatorio.py` (só um comentário com CPF real trocado), `pipeline.sh`, `ponte-rodar.sh`,
`testes/test_ponte_fila.py`, `testes/test_monitor_lotes.py`, `testes/test_relatorio.py`.
Novos: `testes/test_ponte_rodada.py` (29 testes: rodada de ponta a ponta, offline, com `node` falso),
`testes/test_ponte_pendencias.py` (6 testes).
Os outros testes que aparecem no `git diff --stat` (`test_429_operating`, `test_nome_regras`, `test_servico_lote`…) são
de outras frentes e não foram tocados aqui. **Não tocado:** `lote_para_job.py` (frente J).

## Evidência
- `.venv/bin/python -m pytest -q testes/test_ponte_rodada.py testes/test_ponte_pendencias.py` → **42 passed**.
- Suíte inteira `.venv/bin/python -m pytest -q` → **949 passed, 2 skipped** (eram 940 antes desta retomada).
- `bash -n pipeline.sh && bash -n ponte-rodar.sh` → OK.
- pyflakes: limpo nos `.py` de produção da frente. Avisos só em `test_ponte_pendencias.py` (import do fixture `amb`,
  feito de propósito) e em `test_relatorio.py:12 'os' unused` (já existia antes).
- **Contra o original** (cópia em `$TMPDIR/frente-e-orig` com `git show HEAD:` dos 7 arquivos de produção; no fixture,
  `raising=False` + `BASE` apontando para o tmp, para o teste exercitar o comportamento em vez de quebrar na API):
  `test_ponte_rodada.py + test_ponte_pendencias.py` → **32 failed, 10 passed**. Motivos:
  - 13 falhas: o original chega a chamar o `node` com `/tmp/_enfileirar.js` e `/tmp/_entradas.json` fixos, ou seja,
    **tentou enfileirar** kit com documento não baixado, mídia, arquivo desconhecido, duplicata indeterminada etc. (o
    node falso recusa o protocolo legado);
  - asserções de comportamento: `--limite` conta lote que não pode ser enviado (fila `[]` em vez dos 2 lotes de traço); CPF sem
    máscara na saída da duplicata; `--simular` consulta o Bitrix; `enfileirando` órfão não é limpo; `enfileirando` com
    job na fila não é promovido; `pipeline.sh` usa `--todos-os-fechamentos`; `motivo_recusa` exige `pronto`; lote sem
    `pronto` não vai; avisar-primeiro não avisa job sumido; pipeline falha no `cd /opt` fixo e sai pelo `tail`; CPF e
    e-mail reais em `lote_para_fila.py` (3 CPF + 4 e-mails) e `relatorio.py` (2 CPF);
  - 3 por API que não existe no original (`selecionar`, `trava_rodada`, `backup_fila`) e 3 `SystemExit 2` (flags novas
    `--operador`/`--motivo`);
  - os 10 que passam no original são guardas do caminho normal (ex.: lote limpo de traço vai).
  - `test_monitor_lotes.py` contra o `monitor_lotes.py` original: **6 failed** (pronto fora da janela, kit com download
    pendente marcado como repetido, `enfileirando` sem falha, CLI gravando fora do tmp, aviso só quando muda).
  - `test_ponte_fila.py` original: 1 falha (lia `/opt/agente-separador/src/fila-jobs.js`); agora pula quando o arquivo não existe.

## Por achado
| ID | Status | O que mudou | Testes |
|---|---|---|---|
| **F2-01** | corrigido | Elegibilidade única: `ponte_criterio.avaliar` = `estado_lote.pode_enfileirar` sobre a visão só-documentos + regra do escritório. `--todos-os-fechamentos` não desliga mais a regra. `--forcar` exige `--operador` e `--motivo` (gravados em `forcado` no controle) e ignora só revisão/duplicata/regra, que ficam listadas em `ignorados`; nunca pula identidade nem integridade. Pipeline e timer usam a MESMA rodada (`ponte-rodar.sh`). **Pendência da F aplicada:** candidato = `pronto` OU `pode_enfileirar`; `motivo_recusa` só responde "não está pronto" quando `pode_enfileirar` também recusa. | `test_forcar_*` (4), `test_todos_os_fechamentos_nao_desliga_a_regra_do_escritorio`, `test_lote_confirmado_sem_pronto_vai_pela_regra_unica`, `test_sem_pronto_e_sem_download_continua_fora`, `test_motivo_recusa_aceita_lote_elegivel_sem_pronto` |
| **F2-02** | corrigido | Kit com qualquer documento não baixado, sem sha256 ou com 0 byte fica `AGUARDANDO_DOWNLOAD` (nada vai em parte ao staging). Mídia não é pendência nem vai ao staging (`tipos_documento`). | `test_kit_com_documento_nao_baixado_nao_vai_parcial`, `test_midia_nao_e_pendencia_nem_vai_ao_staging` |
| **F2-03** | corrigido | `situacao_kit`: "repetido" só com o kit 100% baixado; com pendência é indeterminado (adia, nunca descarta). O descarte grava via `estado_store` e imprime o CPF mascarado. Descarte antigo cujo kit agora traz documento novo para o CPF volta para revisão. | `test_duplicata_com_download_pendente_e_indeterminada`, `test_duplicata_com_kit_completo_e_descartada_com_cpf_mascarado` |
| **F2-04** | corrigido | `--limite` conta só os lotes que podem ser enviados (depois dos filtros); ordem = traço (ALTA) primeiro, depois o mais antigo pelo `inicio` em datetime; janela padrão `--dias 7`. | `test_limite_conta_so_enviaveis_e_traco_vai_primeiro`, `test_janela_de_dias_padrao_e_7` |
| **F2-05 / A4** | corrigido | Controle `fila-enviados.json` relido e gravado sob trava (`estado_store`), com merge por lote. Trava da rodada `<BASE>/fila.lock` compartilhada com `ponte-rodar.sh` (fd herdado via `TRIAGEM_FILA_LOCK_FD`, conferido por `st_dev/st_ino`); rodada concorrente pula. `ponte-rodar.sh` sai 2 se não houver `flock` (antes pularia sempre, achando que havia "rodada em andamento"). | `test_controle_e_relido_sob_trava_*`, `test_rodada_concorrente_pula_*`, `test_trava_herdada_do_shell_e_reconhecida`, `test_scripts_shell_usam_a_mesma_trava_*` |
| **F2-06** | corrigido | Registro em duas fases (`enfileirando` → confirmado pelo que está na fila). Falha do node limpa staging e registro; a reexecução reaproveita o mesmo `upload_id`. Órfão `enfileirando` sem job é limpo e o lote volta; com job na fila, é promovido com o `upload_id` da fila. | `test_falha_do_node_*`, `test_node_grava_e_falha_depois_*`, `test_orfao_enfileirando_*`, `test_enfileirando_com_job_na_fila_*` |
| **F2-07** | corrigido | Monitor: avisa só quando a categoria muda (e quando o lote chega à fila); `pronto_fora_janela` é categoria própria (catch-up `--dias 0`); kit com download pendente não é "repetido"; lote sem arquivo em revisão sai do aviso. | `test_avisa_so_quando_a_categoria_muda_*`, `test_pronto_fora_da_janela_e_categoria_propria`, `test_kit_com_download_pendente_nao_e_marcado_repetido`, `test_lote_sem_arquivo_em_revisao_nao_entra_no_aviso` |
| **F2-08** | corrigido | Documento que chega depois do envio é listado (no monitor: revisão "doc posterior") sem reenviar o kit; `enfileirando` sem confirmação vira falha no monitor. | `test_documento_que_chega_depois_do_envio_*`, `test_controle_gera_falha_para_enfileirando_e_revisao_para_doc_posterior` |
| `--simular` (ponte; F2-09 é da J) | corrigido na ponte | `lote_para_fila --simular` é offline (sem Bitrix, sem gravar) e decide igual ao real. | `test_simular_e_offline_nao_grava_e_decide_igual_ao_real` |
| **F2-10** | parcial | Do lado da ponte: o job real sai sem o histórico "SIMULAÇÃO" e o sidecar leva `file_id`. O que fica dentro de `lote_para_job.montar_job` é da frente J. | `test_job_real_sem_historico_de_simulacao_e_sidecar_com_file_id` |
| **F2-12** | corrigido | `avisar-primeiro.py`: job do controle que não está nem na fila nem no espelho (≤ 7 dias) → avisa uma vez; `enfileirando` sem job fica para o monitor; `erros_avisados` podado para os jobs vivos (teto de 5000); grava via `estado_store.gravar_atomico`; caminhos via `caminhos`. | `test_job_sumido_da_fila_e_do_espelho_avisa_uma_vez_sem_nome` |
| **S02** | corrigido | Autoteste com e-mail, senha, telefones e CPFs reais removido de `lote_para_fila.py`; CPFs reais de `relatorio.py` (comentário) e de `test_relatorio.py` trocados por `cpf_util.gerar_para_teste`. Os valores antigos não foram copiados para lugar nenhum. **A credencial continua no histórico do git → rotacionar a senha da cliente.** | `test_sem_cpf_real_nem_credencial_de_cliente_no_codigo[*]` (varre os arquivos da frente) |
| **S04** | corrigido | Saídas da ponte, do avisar e do monitor sem nome de cliente e com `cpf_util.mascarar`. | `test_duplicata_*_cpf_mascarado`, `test_job_sumido_*_sem_nome` |
| **S09** | corrigido | JS e `_entradas.json` via `seguranca.arquivo_temp_privado` (0600, nome aleatório), removidos no `finally`. | `test_temporarios_do_node_sao_privados_e_removidos` |
| **S10** | corrigido | Staging só com documento: arquivo desconhecido só vai se o conteúdo provar que é documento (magic number); mídia nunca vai. | `test_desconhecido_so_vai_se_o_conteudo_provar_documento`, `test_midia_*` |
| **OP-O2** | corrigido | Backup da fila em 0600, sem duplicar conteúdo idêntico, com retenção (`manter_ultimos`). | `test_backup_da_fila_0600_sem_duplicar_e_com_retencao` |
| **OP-T4** | corrigido | O teste do CLI do monitor roda com os caminhos no tmp e não grava o `monitor-lotes.json` de produção. | `test_cli_roda_sem_estourar_e_grava_so_no_tmp` |

## pipeline.sh (pendências das frentes F e D)
- `set -uo pipefail`. Cada passo roda pela função `passo`, que guarda o código REAL (`PIPESTATUS[0]`; o `tail` só corta
  a saída). Os passos seguem mesmo que um falhe (são independentes: a ponte decide sozinha pelo `pode_enfileirar`) e o
  pipeline **sai com o pior código** (0/1/2). Sem `set -e`, de propósito: ele abortaria no primeiro 1 (= "atenção").
- Passo novo 3/5: `reconciliar_por_file_ids.py --dias 7 --aplicar` (associação só com prova).
- `completar_cpf_por_nome.py --aplicar --limite 30`.
- `cd "$TRIAGEM_BASE"` (padrão `/opt/triagem-bitrix`), saindo com 2 se a pasta não existir; o passo 5 é o `ponte-rodar.sh`.
- Testes: `test_pipeline_roda_todos_os_passos_e_devolve_o_pior_codigo`, `test_pipeline_tudo_ok_sai_zero` (com `flock` falso
  no PATH; a trava real é testada em `test_ponte_rodada`).

## Mudanças de comportamento
- O pipeline manual não usa mais `--todos-os-fechamentos` e passa pela trava e pelos limites da ponte (limite 4, 7 dias).
- O pipeline devolve código ≠ 0 quando algum passo devolve (antes era sempre o código do `tail`, ou seja, 0): quem
  chama o pipeline vai passar a ver falhas que antes eram mudas.
- Lote sem `pronto=True`, mas elegível pelo `estado_lote`, agora vai para a fila (pedido da frente F).
- `--forcar` agora exige `--operador` e `--motivo`; kit com documento pendente não vai mais em parte.
- `ponte-rodar.sh` sem `flock` sai com 2 (antes pularia em silêncio); a data passou para um formato portátil.
- avisar-primeiro passa a avisar "JOB SUMIU" e não mostra mais o nome nem o CPF inteiro.

## Precisa do servidor
- Conferir em `/opt/agente-separador/src/fila-jobs.js` que `FilaJobs.enfileirar` faz merge por id, grava de forma atômica e
  devolve `criado=false` para id já existente. O node falso dos testes imita isso; `test_ponte_fila.py` pula fora do servidor.
- `flock` (util-linux) instalado; unit do timer com `TRIAGEM_BASE` se a base mudar.
- Rodar `lote_para_fila.py --simular --todos` antes do primeiro `--enviar` e comparar com o monitor (lotes confirmados
  por nome+telefone que vão passar a entrar).
- Rotacionar a senha removida (S02). A retenção de `backups/` só vale para os backups novos; os antigos continuam lá
  (apagar é decisão do operador).

## Pendências para outras frentes
- **J (`lote_para_job.py`)**: partes de F2-10 dentro de `montar_job`; `BASE_DIR`/`STAGING_RAIZ` ainda com `/opt` literal; F2-09.
- **D → E (não feito por tempo)**: listar `falha_permanente`/`telegram_legado` do `download_fila.json` no monitor como
  "precisa de humano".
