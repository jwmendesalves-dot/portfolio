# Frente C1 (onda 2) — triagem_grupo.py: segurança, persistência, download

Status: **CONCLUÍDO** (feito por dois subagentes: o primeiro parou por tempo depois da maior parte; o segundo fechou a pendência da frente D, os testes de pendência, a prova contra o original e este relatório).

Escopo: S01, S06, S07, A3/F1-03, F1-08, B1, B2, B4, C1, D3, E3, G2 + pendência da frente D (rodada curta da fila de retry; lote_id/indice no enfileiramento).
**Fora do escopo (onda 3), não mexido:** agrupar(), agrupar_por_janela(), _acumular, _muda_identidade, barreira, extração de nome/CPF, cursor/paginação, lote_id.

## Arquivos
- `triagem_grupo.py` (alterado)
- `testes/test_429_operating.py` (alterado: o teste do 503 passou a olhar o disjuntor novo `vibecode-leitura`/`rede_bitrix.circuito`, e não o `CircuitBreaker` antigo)
- `testes/test_triagem_seguranca.py` (novo, 9 testes)
- `testes/test_triagem_persistencia.py` (novo, 8 testes)
- `testes/test_triagem_download.py` (novo, 18 testes)
- `testes/test_guarda_dns.py`: não precisou mudar (os 2 testes passam antes e depois)
- `docs/remediacao/frentes/C1-triagem-seg-persist.md` (este relatório)

## Achados

| ID | Status | O que mudou | Teste(s) |
|---|---|---|---|
| **S01** | corrigido | `serializar_lote` grava `seguranca.remover_senhas(m["texto"])[:2000]`: a senha gov.br/INSS não entra mais em `estado.json` (nem nos backups ou na fila que derivam dele). O resto do texto continua. | seguranca::`test_serializar_lote_remove_senha_e_mantem_o_resto`; persistencia::`test_ciclo_nao_grava_senha_no_estado` |
| **S06** | corrigido | `ler_chave_bot()` → `seguranca.ler_segredo("vibecode_key")`. O webhook vem só de `seguranca.ler_segredo("webhook_bitrix")`, lido pelo `bitrix_cliente`. A varredura de arquivos de outros projetos (mcp server, PrevSync, /root/trampo, /tmp/b24, `os.walk`) foi **removida**. Sem segredo, a mensagem de erro é clara e não cita outros projetos. `BASE_DIR` = `TRIAGEM_DIR` (alias legado) ou `caminhos.BASE`. | seguranca::`test_webhook_vem_do_segredo_sem_varrer_disco`, `test_sem_webhook_configurado_erro_claro…`, `test_codigo_nao_procura_webhook…`, `test_ler_chave_bot_usa_ler_segredo`, `test_ler_chave_bot_ausente_levanta_sem_vazar`, `test_base_dir_segue_caminhos_base…` |
| **S07** | corrigido | `configurar_log` instala `seguranca.instalar_filtro_log` no logger `triagem` e no root, o que mascara webhook, X-Api-Key, CPF e senha no arquivo e no journal. As mensagens de erro de download passam por `seguranca.mascarar`. A URL do webhook não aparece mais em log nem em exceção. | seguranca::`test_log_e_excecao_nunca_mostram_o_token_do_webhook`, `test_configurar_log_instala_mascara_de_cpf_e_senha` |
| **A3 / F1-03** | corrigido | No fim do ciclo, os lotes vão para `estado_store.atualizar_lotes(lotes_criados, "triagem", caminho=ARQ_ESTADO)` (merge por campo, dono "triagem"). Cursores, índices e metadados vão numa 2ª transação, `atualizar_estado(_aplicar)`. A varredura de vencidos não regrava mais o snapshot do início do ciclo. Agora sobrevivem ao ciclo: o CPF/nome completados pela identificação, a marca de duplicata da ponte e o download com sha256. | persistencia::`test_cpf_da_identificacao_e_marca_da_ponte_sobrevivem_ao_ciclo`, `test_varredura_fecha_lote_fora_da_janela_sem_regravar_snapshot_velho`, `test_download_com_sha256_sobrevive_ao_ciclo` (este já passava no original; é um teste de regressão) |
| **F1-08** | corrigido | `carregar_estado()` → `estado_store.ler_estado`. Arquivo ausente vira estado vazio. Arquivo corrompido levanta `EstadoCorrompido` e o ciclo **aborta**, em vez de "começar do zero". O store guarda uma cópia `.corrompido-<ts>` e não mexe no original. A função morta `salvar_estado` foi removida. | persistencia::`test_carregar_estado_corrompido_aborta_sem_mexer_no_arquivo`, `test_ciclo_com_estado_corrompido_nao_comeca_do_zero`, `test_salvar_estado_morto_foi_removido`, `test_carregar_estado_ausente_devolve_vazio` |
| **B1** | corrigido | Agora há disjuntores por fonte e por finalidade (`rede_bitrix.circuito`): `bitrix-leitura`, `bitrix-download`, `vibecode-leitura`, `vibecode-download`. Só o passo do webhook consulta `bitrix-download`. Com o Bitrix aberto, a VibeCode continua baixando, e o contrário também vale. | download::`test_bitrix_aberto_nao_impede_fallback_vibecode…`, `test_vibecode_aberto_nao_impede_o_bitrix` |
| **B2** | corrigido | O rate limit da leitura (`im.dialog.messages.get`) não abre mais o disjuntor de download. O 503 no `disk.file.get` abre só `bitrix-download` e não recria o arquivo legado. | download::`test_rate_limit_da_leitura_nao_abre_o_disjuntor_de_download`, `test_rate_limit_no_disk_file_get_abre_so_o_disjuntor_novo`; `test_429_operating::test_503_query_limit_continua_sendo_rate_limit` |
| **B4** | corrigido | Falha de item (NAO_ENCONTRADO/TAMANHO/TIPO) vira `erro_tipo="permanente"`: não abre o disjuntor e não vai para a fila de retry. Um bug nosso (TypeError no parser) também não abre o disjuntor da fonte. O `circuit-download.json` legado é migrado uma vez para `circuit-bitrix-download.json`, com o tempo restante, e o original é renomeado para `.migrado-<ts>`. | download::`test_404_de_item_nao_abre_o_disjuntor_e_e_permanente`, `test_bug_nosso_na_leitura_nao_abre_o_disjuntor_da_fonte`, `test_bitrix_aberto_…_e_legado_e_migrado` |
| **C1** | corrigido | A classificação vem de `tipos_documento.classificar` (pelo nome e pelo tipo da API). Mídia é registrada com `tipo="midia"`, sem rede e sem fila de retry. Um id do Telegram (`CQA…`, não numérico) **nunca** vai ao `disk.file.get`: vira `erro_tipo="telegram_legado"`, definitivo, e nos ciclos seguintes não volta à rede. | download::`test_midia_e_registrada_mas_nao_baixada_nem_enfileirada`, `test_midia_pelo_tipo_da_api_sem_extensao`, `test_id_telegram_legado_nunca_vai_ao_disk_file_get` |
| **D3** | corrigido | Uma URL assinada que expira (classe `URL_EXPIRADA`) não é repetida: o código pede um novo `disk.file.get` e usa a URL nova. | download::`test_url_expirada_pede_disk_file_get_novo_e_nao_reusa_a_url_morta` |
| **E3** | corrigido | O nome no disco é único por (índice, file_id) e um caminho já usado no lote não é reaproveitado por outro file_id. A gravação abre um tmp exclusivo `<destino>.parcial.<pid>.<uuid>` (O_EXCL), confere tamanho > 0, faz fsync, calcula o sha256 durante a gravação e só então `os.replace`. O tmp é removido em caso de falha, e `.part`/`.parcial` de outro processo não são apagados nem contados como baixados. | download::`test_dois_anexos_com_mesmo_nome_tem_destinos_unicos_e_estaveis`, `test_parcial_ou_part_nunca_conta_como_baixado`, `test_download_usa_tmp_exclusivo_e_nao_apaga_parcial_alheio`, `test_download_vazio_nao_vira_baixado` (este já passava no original; regressão) |
| **G2** | corrigido | Saiu o `ThreadPoolExecutor(8)` compartilhado da guarda de DNS. Agora cada abertura roda numa thread daemon própria, abandonada se estourar o prazo. Com mais de `MAX_ABERTURAS_PENDURADAS=32` penduradas, a chamada falha na hora com TimeoutError (classe REDE). | download::`test_guarda_de_dns_nao_esgota_com_conexoes_penduradas`; `test_guarda_dns.py` (2) |
| **Pendência D** | corrigido | `_enfileirar_retry(file_id, destino, erro, *, lote_id, indice, nome)` repassa os três dados para `download_svc.Fila.enfileirar` (assinatura conferida em `download_svc.py:680`). Quem chama é `baixar_file_id_api`, com `lote_id=prefixo`. No ciclo, `processar_fila(..., limite_itens=RETRY_LIMITE_ITENS (5), teto_rodada_s=RETRY_TETO_RODADA_S (60))`. Só erro **transitório** de id numérico entra na fila. | download::`test_erro_transitorio_enfileira_retry_com_lote_e_indice`, `test_enfileirar_retry_repassa_lote_id_e_indice_ao_download_svc`, `test_ciclo_processa_fila_de_retry_em_rodada_curta` (este confere constantes e código-fonte, não roda o ciclo) |

## Evidência

### Testes novos contra o código ORIGINAL (`git show HEAD:triagem_grupo.py`)
Montagem: cópia da árvore atual em `~/.hermes/cache/scratch/c1/red2` (sem .git/.venv/docs), com `triagem_grupo.py` trocado pela versão do HEAD. Comando:
```
env -u TRIAGEM_DIR .venv/bin/python -m pytest -q -p no:cacheprovider testes/test_triagem_seguranca.py \
  testes/test_triagem_persistencia.py testes/test_triagem_download.py testes/test_429_operating.py testes/test_guarda_dns.py
→ 32 failed, 8 passed in 2.23s
```
Motivos das falhas, todos defeitos reais do original:
- varredura de disco atrás do webhook (`nenhuma URL de webhook encontrada nas fontes: /opt/data/mcp/…`) e referência a `/root/trampo`;
- leitura direta de `/opt/triagem-bitrix/vibecode.key`;
- token do webhook no arquivo de log, CPF no log e senha no estado;
- `BASE_DIR` fixo em `/opt/triagem-bitrix`;
- estado corrompido "começando do zero" e `salvar_estado` ainda presente;
- CPF da identificação apagado pelo ciclo e pela varredura (snapshot velho);
- `tipo` mídia ausente, id `CQA…` enviado ao `disk.file.get`, URL expirada repetida 5×;
- 2º `CNH.pdf` reaproveitando o arquivo do 1º, `.parcial` alheio sobrescrito;
- disjuntor do Bitrix bloqueando a VibeCode, leitura abrindo o disjuntor de download, bug nosso abrindo o disjuntor, 404 não marcado como permanente;
- pool de DNS esgotado (`concurrent.futures.TimeoutError`);
- `_enfileirar_retry` sem `lote_id`, `RETRY_LIMITE_ITENS` inexistente;
- `http_json` sem `circuito`.

Os 8 que passam no original são de regressão: `test_carregar_estado_ausente_devolve_vazio`, `test_download_com_sha256_sobrevive_ao_ciclo`, `test_download_vazio_nao_vira_baixado`, `test_vibecode_aberto_nao_impede_o_bitrix`, `test_429_propaga_sem_retentar`, `test_espera_429_usa_operating_reset_at_ou_retry_after` e os 2 de `test_guarda_dns.py`.

### Código atual
```
.venv/bin/python -m pytest -q testes/test_triagem_*.py testes/test_429_operating.py testes/test_guarda_dns.py  → todos passam (18 de download após a pendência D)
.venv/bin/python -m pytest -q          → 943 passed, 2 skipped in 15.46s
env -u TRIAGEM_DIR … -m pytest -q      → 943 passed, 2 skipped in 14.33s
.venv/bin/python -m pyflakes triagem_grupo.py
  triagem_grupo.py:241:1: 'nome_regras.PALAVRAS_NAO_NOME' imported but unused
  triagem_grupo.py:1775:9: local variable 'cpf_msg' is assigned to but never used
  triagem_grupo.py:1776:9: local variable 'nomes_msg' is assigned to but never used
```
Os 3 avisos do pyflakes **já existiam no HEAD** (linhas 205/1627/1628 do original). Ficam em código de agrupamento e extração de nome, que é da onda 3, então não foram mexidos.
Observação: a sessão de shell deste ambiente exporta `TRIAGEM_DIR=…/scratch/triagem-audit/fake-opt`. Com essa variável, uma rodada anterior teve 1 falha em `test_monitor_lotes.py`, que não é desta frente. Nas rodadas finais, com e sem `TRIAGEM_DIR`, tudo passou.

## Mudanças de comportamento para o deploy (atenção)
1. **Exige segredos fora do código e fora de outros projetos:** `webhook_bitrix` e `vibecode_key` em `/etc/triagem-bitrix/` (ou `TRIAGEM_SEGREDOS_DIR`), arquivo com permissão 0600/0400 e dono correto, ou então env `TRIAGEM_WEBHOOK_BITRIX` / `TRIAGEM_VIBECODE_KEY`. Caminhos legados só valem pela lista explícita do `seguranca` (`TRIAGEM_SEGREDOS_LEGADOS`). **Sem o segredo `webhook_bitrix`, a leitura e o download pelo Bitrix falham com erro claro**: a busca automática em `/opt/data/mcp/…`, PrevSync e afins acabou. Antes do deploy, crie `/etc/triagem-bitrix/webhook_bitrix`.
2. **Estado corrompido derruba o ciclo** (sai com erro e alerta) em vez de recomeçar do zero. Para recuperar, alguém precisa agir com base na cópia `.corrompido-<ts>`.
3. `estado.json` não guarda mais senha em `lotes[*].mensagens[*].texto`. O que está gravado de ciclos antigos **não é limpo** por esta frente (ver pendências).
4. Disjuntores: o `circuit-download.json` legado é migrado e renomeado para `.migrado-<ts>` na 1ª execução. Os arquivos novos são `circuit-{bitrix,vibecode}-{leitura,download}.json` em `caminhos.BASE`. Painéis ou scripts que liam `circuit-download.json` precisam olhar `circuit-bitrix-download.json`.
5. Mídia (áudio/vídeo/imagem classificada como mídia) aparece em `arquivos` com `tipo="midia"`, `baixado=false` e **sem erro**: não é mais baixada como documento. Id do Telegram legado fica com `erro_tipo="telegram_legado"` (definitivo) e item apagado com `erro_tipo="permanente"`.
6. Nomes de arquivo no lote passam a ser únicos por (índice, file_id). Anexos homônimos não se sobrescrevem mais.
7. A fila de retry é processada no ciclo com no máximo 5 itens ou 60 s por rodada. O resto fica para os ciclos seguintes ou para o `download_svc` standalone.
8. Logs: CPF, senha, token de webhook e X-Api-Key aparecem mascarados (`***`) no `triagem.log` e no journal.

## Pendências para outras frentes / riscos
- **Onda 3 (agrupamento):** os 3 avisos do pyflakes acima e a lógica de agrupamento, cursor e lote_id, que não foram tocados.
- **Limpeza retroativa (S01):** senhas já gravadas em `estado.json`, backups e `fila-jobs.json` de ciclos anteriores. Precisa de um script de saneamento com `seguranca.remover_senhas` sob a trava do `estado_store`, a rodar no servidor. Não é desta frente nem pode ser feito sem os dados reais.
- **download_svc:** `processar_fila` ignora o argumento `webhook` (lê o segredo sozinho). O ciclo ainda só chama a fila quando `webhook_download` existe. É inofensivo, mas dá para simplificar quando o D revisar.
- **Servidor:** criar `/etc/triagem-bitrix/{webhook_bitrix,vibecode_key}` (0600, dono do serviço) e conferir se as units definem `TRIAGEM_DIR`, que continua aceito como alias e tem precedência sobre `caminhos.BASE`.
- O teste `test_ciclo_processa_fila_de_retry_em_rodada_curta` só inspeciona o código-fonte, sem executar o ciclo. A garantia de comportamento vem do `download_svc` (testado pela frente D).
