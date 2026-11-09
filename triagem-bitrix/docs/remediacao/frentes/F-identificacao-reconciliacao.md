# Frente F — Identificação por CPF e reconciliadores (onda 2)

Status: **CONCLUÍDO** (código + 45 testes novos). Pendências para o orquestrador no fim.

## Arquivos
Alterados: `completar_cpf_por_nome.py`, `reconciliar_estado.py`, `reconciliar_arquivos_disco.py`,
`reconciliar_por_file_ids.py`, `dividir_lote_misto.py`.
Movidos com `mv` (não git) e reescritos: `reparo_josilene.py` → `manutencao/reparo_josilene.py`;
`backfill_nome_lotes.py` → `manutencao/backfill_nome_lotes.py`. Novo: `manutencao/__init__.py`.
Para o commit: `git rm --cached reparo_josilene.py backfill_nome_lotes.py` + `git add manutencao/`.
Testes novos: `testes/test_completar_cpf.py` (19), `testes/test_reconciliadores.py` (11),
`testes/test_dividir_lote.py` (9), `testes/test_manutencao.py` (6).

## Resultado dos testes (real)
- `.venv/bin/python -m pytest -q testes/test_completar_cpf.py testes/test_reconciliadores.py testes/test_dividir_lote.py testes/test_manutencao.py testes/test_estado_store.py testes/test_estado_concorrencia.py` → **89 passed in 2.43s**.
- Suíte inteira `.venv/bin/python -m pytest -q` → **8 failed, 832 passed, 1 skipped in 8.55s**. As 8 falhas
  não são desta frente (arquivos de outras frentes em edição paralela): `test_download_svc.py` (6, AttributeError —
  frente D) e `test_ponte_fila.py` (2 — frente E). Não mexi nelas.
- CPFs só de `cpf_util.gerar_para_teste`, nomes fictícios, Bitrix falso via `bitrix_cliente._post`
  (mesma assinatura do `_transporte`). Nenhuma rede.

## Por achado
| ID | Status | O que mudou | Testes |
|---|---|---|---|
| **ID-01 / S11** (libera lote, apaga motivos) | corrigido | `completar_cpf_por_nome` nunca grava `pronto=True` nem `requer_revisao=False`/`motivos_revisao=[]`. Na confirmação manda só `_resolver_motivos` para `SEM_CPF:*` e os próprios `cpf_por_nome:*` (e só se existirem no lote). Pula `descartado_*`, CONFLITO (status ou identificação), DIVIDIDO/IGNORADO/ERRO_PERMANENTE (via `estado_lote.estado_logico`). `revisao_duplicata` recebe o CPF e **continua** em revisão. Elegibilidade = `estado_lote.pode_enfileirar`. | `test_nome_e_telefone_do_mesmo_contato_confirma_sem_liberar_duplicata`, `test_confirmado_limpo_fica_elegivel_pelo_estado_lote_sem_forcar_pronto`, `test_descartado_conflito_dividido_nao_sao_tocados` (5 casos) |
| **ID-02** (1ª página, 1º nome, 1 homônimo = prova) | corrigido | Busca com `bitrix_cliente.listar` (todas as páginas, `>ID`+`start=-1`) em 2 filtros (`%NAME`=1º nome+`%LAST_NAME`=último; `%NAME`=nome inteiro), comparação pelo nome COMPLETO normalizado (sem acento, sem `da/de/do/dos/das/e`). Confirma só com 2ª evidência: telefone do lote/ficha/autor → `crm.duplicate.findbycomm` (`type=PHONE`, `entity_type=CONTACT`, ≤20 valores, doc local) apontando para o MESMO contato, e CPF `cpf_util.valido`. Sem 2ª evidência → `cpf_sugerido` + motivo `cpf_por_nome: …` via `estado_lote.marcar_revisao`. Homônimos com CPFs distintos, busca truncada (≥2000) ou CPF inválido → ambíguo (sem sugestão). Telefone de outro contato → só sugestão. | `test_homonimo_unico_sem_telefone_vira_so_sugestao`, `test_dois_homonimos_mesmo_na_pagina_3_e_ambiguo` (130 contatos, 2º homônimo no ID 130 — o código antigo via só 50), `test_telefone_de_outro_contato_nao_confirma`, `test_cpf_invalido_no_crm_nao_confirma`, `test_chave_nome_…` |
| **ID-03 / A1 / OP-C1** (grava sem trava) | corrigido | Os 7 scripts gravam só por `estado_store` (`atualizar_lotes` com dono `identificacao`/`download`, ou `atualizar_estado` nos estruturais). HTTP e sha256 sempre fora do mutador; o `completar` relê o estado antes de aplicar e tira do patch o lote que ganhou CPF/foi descartado no meio. Nenhum `json.dump`/`os.replace`/`.bak-` nos arquivos (teste estático). | `test_reconciliador_nao_desfaz_marca_da_ponte_gravada_no_meio`, `test_nao_importa_mais_nomear_lotes_nem_grava_json_direto`, `test_scripts_de_manutencao_nao_gravam_json_direto` |
| **ID-04** (sem cache, martela o Bitrix) | corrigido | Cache `caminhos.em_base("cache-cpf-nome.json")` (gravação atômica 0600 por `estado_store.gravar_atomico`): nomes TTL 24 h, negativo 12 h; telefones idem; poda do expirado. `--limite` por rodada. Limite/disjuntor ficam no `bitrix_cliente`; `CircuitOpenError`/`FalhaBitrix` interrompem a rodada (aplica o já apurado, saída 2). | `test_segunda_execucao_nao_cria_backup_nem_consulta_o_bitrix`, `test_cache_negativo_expira_pelo_ttl`, `test_falha_do_bitrix_sai_com_codigo_2_e_nao_grava` |
| **F1-04 / OP-O1** (bak a cada 10 min) | corrigido nos meus arquivos | Backup só por `estado_store.fazer_backup` e só quando há mudança real (o `completar` compara `mesclar_lote(atual, patch) != atual` antes). `reconciliar_estado` (timer) continua sem backup, como antes; `reconciliar_arquivos_disco`, `dividir`… manutenção fazem backup só com mudança. | `test_segunda_execucao_nao_cria_backup…`, `test_arquivos_disco_por_nome_registrado_com_backup_so_quando_muda`, `test_reparo_aplica_e_segunda_execucao_nao_move_nada` |
| **F1-12** (CPF formatado em `cpf`) | corrigido | `cpf` = 11 dígitos; `cpf_formatado` = `cpf_util.formatar`. | `test_nome_e_telefone…` |
| **E4** (reconciliadores) | corrigido | `reconciliar_estado.arquivo_valido`: ignora `.part/.parcial/.tmp/.meta.json`, `*.tmp.*`/`*.part.*`, ocultos e 0 byte; usado pelos 3 reconciliadores. Índice com 2 arquivos e nenhum nome registrado → não escolhe, reporta. | `test_reconciliar_estado_marca_por_indice_e_ignora_parciais`, `test_reconciliar_estado_indice_duplicado_nao_escolhe`, `test_arquivo_valido` |
| **E5** (associação alfabética, meta de erro some) | corrigido | Associação só com prova: `nome_salvo` → sha256 registrado → índice `NN_` (= `indice` do meta ou posição) → `nome_original`. Arquivo sem índice e sem prova fica sem associação e é reportado (`sem_prova`), file_ids sem arquivo em `nao_resolvidos`. Patch dono `download` (merge não remove meta). | `test_file_ids_sem_indice_nao_associa_por_ordem_alfabetica`, `test_file_ids_associa_por_nome_salvo_e_por_sha256`, `test_file_ids_preserva_meta_de_erro_nao_resolvido` |
| **F2-11** (sub-lote sem marcar o pai) | corrigido | Uma transação (`atualizar_estado`): sub-lotes `<pai>-kNN` (sem CPF no id) com `dividido_de`, só os `file_ids`/arquivos do bloco, sem metadados internos/motivos do pai; pai recebe `dividido_em` (triagem) + `status=DIVIDIDO`, `pronto=False` (dono `revisao`, o ciclo não desfaz). Se o pai não ficar DIVIDIDO/não-enfileirável, a transação aborta. Idempotente (pai DIVIDIDO → nada; sub-lote existente nunca sobrescrito). Recusa descartado/`revisao_duplicata`/ignorado. CPF do kit validado por `cpf_util.valido`; nome pelo `bitrix_cliente` só se único (antes: `requests` com webhook lido do `.env` do PrevSync). | `test_divide_e_marca_pai_dividido_na_mesma_transacao`, `test_idempotente_…`, `test_ciclo_da_triagem_nao_desfaz_o_dividido`, `test_nao_divide_…` (3), `test_nome_do_cpf_ambiguo_nao_adivinha` |
| **AUX-01 / OP-E3 (parte)** | corrigido | `manutencao/`: padrão simulação, gravar exige `--aplicar --confirmar` (só `--aplicar` → saída 2). `reparo_josilene`: guarda = os 4 file_ids já no lote da Josilene → não move nada; arquivos achados pelo file_id (não pelo índice); nunca sobrescreve destino (aborta antes de mover); checagem refeita dentro da trava. `backfill_nome_lotes`: idempotente, não toca nome com `nome_origem`, dono `identificacao`, `triagem_grupo` importado só quando precisa. CPF real que estava na docstring do backfill **removido** (não copiado para lugar nenhum). | `test_reparo_aplica_e_segunda_execucao_nao_move_nada`, `test_reparo_simulacao_padrao_e_aplicar_exige_confirmar`, `test_reparo_nao_sobrescreve_destino_existente`, `test_reparo_estado_que_nao_confere_aborta`, `test_backfill_exige_confirmar_e_e_idempotente` |
| Logs mascarados | feito | `seguranca.instalar_filtro_log()` nos mains; CPF só por `cpf_util.mascarar`. | `test_log_nao_traz_cpf_em_claro` |

## Achado novo (reconferência)
- `reconciliar_por_file_ids` antigo: `alvos = [a for a in argv if not a.startswith('--')]` incluía o **valor** de `--dias 7`
  como lote-alvo `"7"`; com `--dias` (o timer de 10 min, CONTEXTO §5) nenhum lote casava e o script **não fazia nada**.
  Com argparse, `--dias 7` passa a reconciliar de verdade (com prova). **Mudança de comportamento no timer.**
- O mesmo script antigo reconciliava também em `--aplicar` sem trava de prova; hoje padrão continua simulação.

## Mudanças de comportamento (deploy)
- `completar_cpf_por_nome`: sem `--simular/--aplicar` agora **simula** (antes dava erro de uso); `pipeline.sh` usa `--aplicar` — inalterado. Saída 2 quando a rodada é interrompida pelo Bitrix (antes sempre 0). Muitos lotes que antes eram "completados" por homônimo agora só ganham `cpf_sugerido` + motivo de revisão (fica para a revisão humana — é o desejado).
- `cpf_origem` novo: `bitrix-nome+telefone` (antes `bitrix-nome`). Consumidores que testam `== "bitrix-nome"` (pintura PL-03?) precisam aceitar o novo valor.
- `reconciliar_estado`: continua gravando por padrão (passo do pipeline); ganhou `--simular`. Índice duplicado não é mais resolvido "pelo último".
- `reconciliar_arquivos_disco`: índice duplicado não escolhe mais "o mais recente" (alfabético); backup em `backups-estado/`.
- `dividir_lote_misto`: id do sub-lote `<pai>-kNN` (antes `<pai>-<CPF>`); saída 2 quando nada é dividido.
- `cache-cpf-nome.json` novo em `TRIAGEM_BASE` (0600).

## Pendências para outras frentes / orquestrador
1. **estado_store.py (orquestrador/B)**: declarar em `PROPRIEDADE_CAMPOS` `"cpf_sugerido_motivo": "identificacao"` (hoje campo desconhecido → dono triagem, a identificação seria negada). O `completar` só manda o campo quando ele estiver declarado (teste `test_motivo_do_sugerido_grava_quando_o_store_declara_o_campo`); até lá o motivo vai só em `motivos_revisao`. Declarar também `"dividido_em": "triagem"` (hoje cai no log "campo sem dono declarado").
2. **pipeline.sh (frente E)**: chamar `reconciliar_por_file_ids.py --dias 7 --aplicar` explicitamente se for para gravar (ver achado novo); `completar_cpf_por_nome.py --aplicar --limite 30` recomendado (ID-04).
3. **Ponte (frente E)**: decidir fila por `estado_lote.pode_enfileirar` — o `completar` não marca mais `pronto=True`; enquanto `ponte_criterio` exigir `pronto`, lote confirmado por nome+telefone não segue sozinho (o sub-lote do `dividir` segue, ele nasce `pronto=True`).
4. **Pintura (frente X)**: tratar `cpf_origem="bitrix-nome+telefone"` e ignorar `cpf_sugerido`.
5. Comentário em `estado_store.py:331` cita `backfill_nome_lotes` na raiz — agora em `manutencao/`.

## Riscos / o que precisa do servidor
- Filtros `%NAME`/`%LAST_NAME` e o formato de `findbycomm` seguem a doc local; volume real de chamadas e o formato real dos telefones nos lotes (ficha × campo `autores`) só no servidor. Rodar primeiro `completar_cpf_por_nome.py --simular` e conferir quantos viram confirmados × sugeridos.
- Contato que tem o nome todo no `NAME` e só parte no filtro 1 é coberto pelo filtro 2 (LIKE do nome inteiro); grafias diferentes continuam falso-negativo (seguro).
- Edge aceito: entre a releitura e o `atualizar_lotes` (milissegundos) o ciclo pode gravar CPF no mesmo lote; o patch da identificação prevaleceria.
- `reparo_josilene` já foi aplicado em produção (04/11): rodar de novo deve dizer "nada a mover" — conferir com `--simular` antes de qualquer coisa.
