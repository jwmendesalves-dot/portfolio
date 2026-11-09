# Frente B — Persistência transacional do estado (onda 1, produtor)

Status: **CONCLUÍDO** (API pronta + testes). A migração dos consumidores fica para a onda 2.

Arquivos desta frente (únicos tocados):
- `estado_store.py` (reescrito, mesmas assinaturas antigas + contrato novo)
- `estado_lote.py` (novo)
- `testes/test_estado_store.py` (5 testes antigos intactos + 32 novos)
- `testes/test_estado_lote.py` (novo, 40 testes)
- `testes/test_estado_concorrencia.py` (novo, 7 testes com `multiprocessing` spawn — processos reais)

## Resultado dos testes (real)
- `.venv/bin/python -m pytest -q testes/test_estado_store.py testes/test_estado_lote.py testes/test_estado_concorrencia.py` → **84 passed in 2.21s** (concorrência repetida 4× isolada: 7 passed cada vez).
- Suíte inteira `.venv/bin/python -m pytest -q` → **14 failed, 717 passed in 7.29s** (rodada final; o total cresce porque outras frentes estão adicionando testes).
- CPFs dos testes são sintéticos (`_cpf_sintetico`, DV calculado), nenhum literal de CPF nos arquivos da frente. As 14 falhas são as mesmas da baseline (baseline: 14 failed, 287 passed) e nenhuma é de estado: test_variaveis_contrato (7), test_servico_lote (3), test_nome_regras, test_pasta_fichinha, test_ponte_fila, test_telefones_fichinha. Não mexi nelas.
- Nenhum teste antigo dependia de `{}` em corrupção; nenhum precisou ser ajustado.

Evidência do defeito antigo (código de `HEAD:estado_store.py` num diretório temporário, estado truncado):
```
ler_estado(corrompido) -> {}
depois do atualizar -> {"lotes": {"novo": {}}}      # 100% do estado perdido
```
Com o código novo: `EstadoCorrompido`, mutador nem roda, arquivo byte a byte igual, cópia `.corrompido-<ts>` (testes `test_atualizar_nao_grava_sobre_estado_corrompido` e `test_json_invalido_nao_vira_estado_vazio_nem_gravacao`).

## Por achado

| ID | Status | O que mudou | Testes |
|---|---|---|---|
| **A2** (tmp fixo + corrompido vira `{}`) | **corrigido no store** | `ler_estado`: ausente → `{}`; JSON inválido / vazio / não-UTF-8 / raiz não-dict → `EstadoCorrompido(caminho, copia)` + uma cópia `<arq>.corrompido-<ts>` por conteúdo (sha256), 0600. Outros `OSError` (permissão) propagam — não viram vazio. `atualizar_estado`/`atualizar_lotes` não gravam quando a leitura falha. `gravar_atomico`: tmp `<arq>.tmp.<pid>.<uuid>` com `O_EXCL`, 0600, fsync do arquivo, `os.replace`, fsync do diretório; tmp removido em qualquer exceção; recusa não-dict. | `test_corrompido_levanta_e_preserva_copia` (5 variações), `test_copia_do_corrompido_e_uma_por_conteudo`, `test_atualizar_nao_grava_sobre_estado_corrompido`, `test_gravar_atomico_0600_e_sem_tmp`, `test_queda_antes_do_replace_preserva_original_e_remove_tmp`, `test_tmp_e_exclusivo_por_gravacao`, `test_json_invalido_…` (processo real) |
| **F1-08** | **corrigido no store**; parcial no sistema | Mesma correção de A2 + fsync do diretório. Os 5 gravadores fora da trava continuam lá até a onda 2 migrá-los. `triagem_grupo.carregar_estado:2491` ainda "começa do zero" (arquivo alheio). | idem |
| **A1** (reconciliar_*/completar sem trava) | **parcial — API pronta** | `atualizar_lotes(mudancas, dono)` com merge por campo; consumidores calculam sha256/HTTP fora e aplicam o patch. | `test_marca_de_duplicata_da_ponte_sobrevive_a_reconciliacao` (reconciliador com snapshot velho, processo real) |
| **A3 / F1-03** (ciclo regrava o lote inteiro) | **corrigido no store; falta o ciclo usar** | `PROPRIEDADE_CAMPOS` + `mesclar_lote` + `atualizar_lotes` (regras abaixo). Se `triagem_grupo._aplicar` trocar `fresco["lotes"].update(lotes_criados)` por `atualizar_lotes(lotes_criados, "triagem")`, CPF da identificação, status/marcas da ponte, metas baixadas e motivos de revisão sobrevivem. Inclui heurística para o estado **legado** (`cpf_origem`/`nome_origem` preenchidos, status `descartado_*`/`revisao_duplicata`). | `test_cpf_da_identificacao_sobrevive_a_regravacao_da_triagem` (processo real: ciclo lê no início, completar grava no meio, ciclo regrava), `test_triagem_nao_apaga_cpf_da_identificacao`, `test_estado_legado_cpf_origem_tambem_protege`, `test_ponte_status_duplicata_sobrevive_a_triagem`, `test_padrao_antigo_update_inteiro_perderia_o_cpf` |
| **F1-04** (bak sem retenção) | **corrigido no store; falta trocar as 5 chamadas** | `fazer_backup(caminho, motivo)` → `<dir>/backups-estado/estado.json.bak-<ts>[-motivo]`, 0600 (dir 0700); se o conteúdo é igual ao último backup não copia (devolve o existente). `podar_backups(manter_ultimos=24, diarios=14, simular=True)` reconhece também o legado `<estado>.bak-YYYYMMDD-HHMMSS*` ao lado do estado, nunca apaga com `simular=True`, devolve a lista. Não toca `.corrompido-*` nem backups de outros arquivos. Extra: `limpar_tmp_orfaos(simular=True)`. | `test_fazer_backup_0600_no_subdiretorio_e_sem_duplicar`, `test_podar_backups_mantem_24_mais_diarios_e_reconhece_legado` (180 backups em 30 dias → mantém 24 + 10 diários) |
| **F1-10** (varredura pronto=False sem revisão) | **parcial** | `estado_lote.marcar_revisao`/`motivos_impeditivos` dão a regra; o merge impede que a varredura derrube `pronto` reivindicado pela identificação. A varredura em `triagem_grupo.py:2848` precisa usar `marcar_revisao(reg, "fechado por varredura de timeout")` (arquivo alheio). | `test_motivo_novo_da_triagem_rebaixa_pronto_reivindicado` |
| **A4** (2 `lote_para_fila` concorrentes) | **parcial** | Para `fila-enviados.json`: `atualizar_estado(mutador, caminho=CONTROLE)` serve para qualquer JSON-objeto, e `trava_arquivo(caminho)` expõe o mesmo flock. O flock do `pipeline.sh` e os `/tmp/_entradas.json` fixos são de outras frentes. | trava: `test_trava_ocupada_por_outro_processo_da_timeout` |
| **E5** (reconciliar_por_file_ids apaga meta de erro) | efeito colateral corrigido no merge | dono `download` não remove metas que não mandou nem adiciona file_id fora do lote. | `test_download_nao_apaga_meta_de_erro_dos_outros`, `test_download_recusa_file_id_fora_do_lote` |

## Regras de propriedade (`estado_store.PROPRIEDADE_CAMPOS`) — campos levantados no código

- **triagem** (`serializar_lote` :2512, ciclo :2812, telegram_grupo, dividir_lote_misto): `lote_id, modo, status, status_lote, sinal_abertura, sinal_fechamento, alerta, confianca, nomes_candidatos, nomes*, cpfs, cpfs_papeis, cpf_responsavel*, cpfs_responsavel, autores, inicio, fim, fechado, fechado_por, marcador_fim_textual, ts_marcador, aberto, qtd_arquivos, file_ids, mensagens, arquivos` (estrutura), `midias, pasta, visto_em, dialog_id, chat, fonte_entrada, dividido_de`; dentro de arquivos: `file_id, indice, nome_original, acao, destino_pasta, tipo`.
- **identificacao** (completar_cpf :126-139): `cpf, cpf_formatado, cpfs_distintos, cpf_origem, nome_origem, identificacao, cpf_sugerido, nome`. `nome` entrou aqui (não na triagem) porque o completar o reescreve a partir do CRM com `nome_origem`; os nomes do cabeçalho (`nomes_candidatos`, `nomes_ident`…) continuam da triagem.
- **download** (chaves `arquivos[*].<campo>`): `baixado, sha256, caminho, erro, erro_tipo, tentativas, proximo_retry, tamanho_bits, nome_salvo, obtido_em, fonte, reaproveitado, aviso, nome_derivado`.
- **ponte** (lote_para_fila :125-147): `status_anterior, descartado_em, lote_origem, descarte_motivo`; reservados `enviado, enviado_em, job_id, upload_id, status_envio` (hoje o controle de envio vive em `fila-enviados.json`, não no lote).
- **revisao**: `requer_revisao, motivos_revisao, motivos_resolvidos, pronto, pronto_por`.

Regras de `mesclar_lote(atual, novo, dono)` (puro; devolve `(lote, conflitos)`; campo ausente em `novo` nunca é apagado):
1. Dono escreve os seus campos. `ESCRITORES_EXTRA`: identificacao pode `pronto/pronto_por/requer_revisao/nomes_candidatos`; ponte pode `status` **só** para `descartado_duplicata`/`revisao_duplicata` e `requer_revisao`; revisao pode `status`. Extra não sobrescreve decisão explícita do dono nominal.
2. **Triagem = produtor-base**: preenche campos de qualquer dono enquanto ninguém os reivindicou. Escrita de outro dono grava `lote["_donos_campos"][campo] = dono` (novo metadado no registro) e a triagem passa a ser negada naquele campo (vira conflito, nível DEBUG no log para não poluir o ciclo).
3. **Download monotônico**: metas casadas por `file_id`; triagem não rebaixa `baixado=True`+`sha256` para não-baixado; download só preenche campos de estrutura vazios.
4. **motivos_revisao**: motivo de quem não é a triagem (ex.: `marcar_revisao` da ponte) é fixo e só sai com `_resolver_motivos: ["texto" | "prefixo*"]` (qualquer dono exceto triagem; auditado em `motivos_resolvidos`). Os motivos **derivados** da triagem (`SEM_CPF:/CONFLITO:/SEM_ARQUIVOS:/bloco interrompido`, guardados em `_motivos_triagem`) são recalculados a cada ciclo — senão um lote ABERTO que recebe o CPF depois ficaria preso em revisão. Motivo resolvido não volta pela triagem; `SEM_CPF:` também não volta quando a identificação está reivindicada e OK. Havendo motivo ativo → `requer_revisao=True`; motivo **novo** pode rebaixar `pronto` mesmo reivindicado (sentido seguro).
5. Campo desconhecido → dono `triagem`, registrado em `CAMPOS_DESCONHECIDOS` + log INFO. Chaves `_*` vindas do patch são ignoradas (não se forja `_donos_campos`).

`atualizar_lotes(mudancas, dono, caminho=None, timeout=120)` → `{lote_id: [campos negados]}` **só dos lotes com conflito** (dict vazio = tudo aplicado). Lote inexistente: a triagem cria; outro dono recebe `["<lote inexistente>"]`.

## estado_lote.py
`ESTADOS_LOGICOS`, `estado_logico(lote, controle=None)` (controle = registro em fila-enviados: com `job_id`/`upload_id` → ENFILEIRADO, senão ENFILEIRANDO), `motivos_impeditivos` (lista **todos**), `pode_enfileirar`, `transicao_valida` (grafo explícito), `marcar_revisao`. Acréscimo: `resolver_revisao(lote, motivo, por)`. Só lê os status externos; não renomeia nada. `pode_enfileirar` exige o que o contrato pede e mais: todo `file_id` com meta de arquivo.
- **CPF**: import tardio de `cpf_util.valido`; fallback local mínimo (DV + repetidos) **se o módulo não existir**. No fim da onda `cpf_util.py` (frente S) já existe e a suíte passa com ele; o fallback fica só como proteção. Teste `test_usa_cpf_util_quando_existe` prova que delega.

## Mudanças de comportamento (atenção no deploy)
- Estado corrompido agora **para** o gravador com exceção (antes zerava). Quem chama `atualizar_estado` sem tratar `EstadoCorrompido` vai sair com erro — é o desejado, mas o vigia/alerta deve olhar isso.
- `ARQ_ESTADO` = `$ESTADO_JSON` ou `caminhos.em_base("estado.json")` (antes: diretório do próprio arquivo; em produção dá no mesmo `/opt/triagem-bitrix/estado.json`).
- Estado e `.lock` passam a ser criados com 0600 (antes 0644 conforme umask). Se o painel/node lê `estado.json` com **outro usuário**, vai dar permissão negada → conferir no servidor (`ls -l /opt/triagem-bitrix/estado.json` e o usuário do painel).
- `atualizar_estado` não regrava quando o resultado é idêntico ao disco.
- Formato gravado: JSON compacto (igual ao `estado_store` antigo).

## Pendências para outras frentes (onda 2)
- **triagem_grupo.py**: `_aplicar` (:2869) → `atualizar_lotes(lotes_criados, "triagem")` + resto do `_aplicar` em `atualizar_estado`; `carregar_estado` (:2491) não pode começar do zero (usar `ler_estado` e abortar); varredura (:2848) usar `estado_lote.marcar_revisao`; remover `salvar_estado` sem chamador.
- **completar_cpf_por_nome.py**: patch com dono `identificacao` (`cpf` só dígitos — F1-12 —, `cpf_formatado`, `cpfs_distintos`, `identificacao`, `cpf_origem`, `nome`, `nome_origem`, `pronto: True`, `_resolver_motivos: ["SEM_CPF*"]`); trocar `shutil.copy2(...bak-...)` por `fazer_backup(motivo="completar_cpf")` — e só quando houve mudança.
- **reconciliar_estado / reconciliar_arquivos_disco / reconciliar_por_file_ids / baixar_pendentes**: sha256 fora, `atualizar_lotes({lid: {"arquivos": [metas]}}, "download")`; backups via `fazer_backup`.
- **lote_para_fila.py**: `marcar_duplicatas_no_estado` → `atualizar_lotes(..., "ponte")` (status + campos de descarte; para revisao_duplicata usar `marcar_revisao`); `gravar_controle` → `atualizar_estado(mutador, caminho=CONTROLE)`.
- **telegram_grupo.py**: `mesclar_no_estado` → `atualizar_lotes(..., "triagem")`.
- **nomear_lotes_por_cpf / backfill_nome_lotes / reparo_josilene / avisar-primeiro / certidoes**: mesma migração (dono `identificacao` ou o adequado) + `fazer_backup`.
- **Operação (servidor)**: rodar `podar_backups(simular=True)` primeiro, revisar a lista, depois `simular=False` (libera ~26 GB conforme F1-04). Não feito aqui (sem dados reais).

## Riscos
- Metadados novos no registro (`_donos_campos`, `_motivos_triagem`, `motivos_resolvidos`): consumidores que iteram todas as chaves do lote (painel?) passam a vê-los.
- Heurística legada de `cpf_origem`: lote que já tem `cpf_origem` fica com o grupo de identificação "travado" contra a triagem; correção humana tem de usar dono `identificacao`/`revisao`.
- `_RLOCK` global serializa threads do mesmo processo em todos os arquivos (igual ao antigo).
