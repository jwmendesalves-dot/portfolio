# Frente D — serviço de download fora do ciclo (onda 2)

Status: **CONCLUÍDO** (código + testes + prova RED). Nada de rede; nada de dado real.

## Arquivos (todos da frente)
- `download_svc.py` — reescrito (API antiga preservada: `Fila`, `enfileirar`, `pendentes`, `resumo`, `processar_fila(fila, webhook, api_key, ...)`, `ler_chave_vibecode`, CLI `--enfileirar/--processar/--status`).
- `baixar_pendentes.py` — reescrito como planejador da fila do `download_svc` (CLI `[LIMITE]` preservado; novos `--lote --forcar --teto-s --simular --estado --fila`).
- `checar_midias.py` — alinhado a `tipos_documento`, só leitura, `--so-estado` sem rede.
- `testes/test_download_svc.py` — reescrito (os testes antigos codificavam o defeito D2: backoff em segundos e `falha` terminal).
- `testes/test_download_integridade.py` (novo, 19), `testes/test_baixar_pendentes.py` (novo, 11), `testes/fakes_download.py` (novo, helper não coletado: rede falsa via `urllib.request.urlopen`).
- **Não tocados**: `testes/test_guarda_dns.py` e `testes/test_429_operating.py` (testam `triagem_grupo` → C1), `triagem_grupo.py`.

## Evidência
```
.venv/bin/python -m pytest -q testes/test_download_svc.py testes/test_baixar_pendentes.py testes/test_download_integridade.py
39 passed in 0.93s
.venv/bin/python -m pytest -q          (suíte inteira, com as outras frentes da onda 2 na árvore)
935 passed, 2 skipped in 14.67s
```
RED — mesmos testes contra `git show HEAD:{download_svc,baixar_pendentes,checar_midias}.py` numa cópia em scratch:
```
36 failed, 3 passed
```
Os 3 que passam no original: `test_fila_vazia_quando_arquivo_nao_existe` (comportamento mantido),
`test_dois_anexos_com_mesmo_nome_viram_arquivos_distintos` (baixar_pendentes já prefixava `NN_`; regressão — o defeito
de nome igual está no `download_svc`, coberto por `test_dois_itens_mesmo_destino_ficam_em_arquivos_distintos`, que falha
no original) e `test_parcial_recente_nao_e_apagado` (guarda nova: temporário recente não é apagado).
Motivos das falhas no original conferidos um a um (asserção do defeito, não erro de API), p.ex.: truncado/0 byte/SIZE
viram destino; disk.file.get chamado com disjuntor aberto; ACCESS_DENIED fica `pendente`; QUERY_LIMIT chama 9× (3×3);
CQA chamado 3×; `.parcial` órfão pula o índice; fila corrompida vira vazia; 2 instâncias perdem item; dedupe ausente;
abertura pendurada 3 s sem prazo; gotejamento 3,3 s sem prazo total; código de saída sempre 0. Exceção: os testes de
limite/teto de rodada e de `checar_midias` falham no original por API inexistente (`limite_itens`; `checar_midias`
nem importa sob pytest — `int(sys.argv[1])` no import).

## Por achado
| ID | Status | O que mudou | Testes |
|---|---|---|---|
| **A5** | corrigido | Fila só grava via `estado_store.atualizar_estado(caminho=fila)` (flock + relê + tmp exclusivo 0600 + fsync + `os.replace` + fsync dir). Dedupe por `file_id` (inclusive ao carregar fila legada com duplicatas). Poda de `baixado` > 7 dias (`DOWNLOAD_PODA_DIAS`). JSON inválido → `EstadoCorrompido` + cópia (nunca vira fila vazia). Formato legado (lista) migrado. `enfileirar_varios` = 1 gravação. | `test_dedupe_por_file_id`, `test_duas_instancias_nao_perdem_item`, `test_fila_corrompida_nao_vira_vazia`, `test_formato_legado_*`, `test_baixado_antigo_e_podado` |
| **D1** | corrigido | Por item: `tentativas`, `ultimo_erro {classe, detalhe mascarado}`, `erro` (compat), `proxima_em` (epoch, `rede_bitrix.proxima_tentativa(..., fila=True)`), `estado ∈ pendente/baixado/falha_permanente/ignorado_midia/telegram_legado`. Permanente (todas as fontes tentadas deram permanente) nunca reentra. Desfecho também no `estado.json` (`erro_tipo` = permanente/telegram_legado/midia) via `atualizar_lotes(..., dono="download")`; o planejador não reenfileira meta com `erro_tipo` permanente. `--lote L --forcar` = retry dirigido. | `test_access_denied_permanente_*` (2), `test_transitorio_esgotado_vira_falha_permanente` |
| **D2** | corrigido | Backoff da fila 10 min → 6 h com jitter, 8 tentativas (`POLITICA_FILA`); `falha` antiga (5 tentativas em minutos) migra para `pendente` com 0 tentativas. | `test_falha_transitoria_agenda_em_minutos_*` |
| **B3** (consumidores) | corrigido | `disk.file.get` via `bitrix_cliente.chamar(circuito="bitrix-download", tentativas=1)`; GET da DOWNLOAD_URL com `limitador_padrao()` e `aplicar_no_circuito`. QUERY_LIMIT → `circuito("bitrix-download").abrir("RATE_LIMIT", max(espera,180))` (pausa para TODOS, como o legado) + **para a rodada**. Qualquer `pausa_rodada` (QUERY_LIMIT, OPERATING, LOOP, credencial) para a rodada sem tocar no próximo item. Transição: legado `circuit-download.json` recente (<180 s) é respeitado (só leitura; renomear é da C1). | `test_query_limit_para_rodada_e_agenda_no_futuro`, `test_query_limit_para_a_rodada` |
| **B1** (no download_svc) | corrigido | Disjuntor do Bitrix aberto (ou limitador esgotado) → pula só o Bitrix e tenta VibeCode (`circuito("vibecode-download")` + `LimitadorHost` próprio em `$VIBECODE_RATELIMIT_FILE` ou `<BASE>/.vibecode-ratelimit`; chave `seguranca.ler_segredo("vibecode_key")`). Fonte pulada por disjuntor não conta tentativa nem gera permanente. | `test_disjuntor_bitrix_aberto_tenta_fallback`, `test_disjuntor_aberto_usa_fallback_vibecode` |
| **D3** (no download_svc) | corrigido | URL assinada 401/403/404/410 (`classificar(download=True)` → URL_EXPIRADA) → novo `disk.file.get` (até 2×); nunca reusa a URL. | `test_url_expirada_pede_url_nova` |
| **E1** | corrigido | `.parcial/.part/.tmp`/0 byte nunca contam como baixado; órfãos com mais de `DOWNLOAD_IDADE_ORFAO_S` (30 min) são removidos; temporário recente = "em andamento" (só adia a rodada). | `test_parcial_orfao_nao_bloqueia_o_indice`, `test_arquivo_de_zero_byte_*`, `test_parcial_recente_nao_e_apagado` |
| **E4** (download_svc) | corrigido | Streaming para `.<nome>.<pid>.<uuid>.part` exclusivo (O_EXCL, 0600) no mesmo dir; rejeita 0 byte, ≠ Content-Length, ≠ SIZE; sha256 incremental; fsync; `os.replace`; fsync do dir; tmp removido em qualquer falha. Integridade = transitório limitado (3 tentativas, depois permanente). | `test_truncado_nao_e_aceito`, `test_zero_byte_rejeitado`, `test_tamanho_diferente_do_size_rejeitado`, `test_sucesso_grava_sha256_*` |
| **E3** (no download_svc) | corrigido | Mesmo `destino` de outro `file_id` na fila ou arquivo com conteúdo já no destino → `<nome>__fid<id>.<ext>`; nunca sobrescreve/reaproveita. Item já `baixado`+sha256 no estado (o ciclo baixou) é fechado sem rede. | `test_dois_itens_mesmo_destino_*` |
| **G1** | corrigido | Guarda própria `_abrir_com_teto` (thread daemon, prazo duro para DNS+connect+cabeçalho, `DOWNLOAD_TETO_CONEXAO_S`=20; recusa com ≥8 aberturas penduradas — evita G2 aqui), prazo TOTAL por arquivo (`read1` + checagem por bloco, `DOWNLOAD_TETO_ITEM_S`=120), teto por rodada (`DOWNLOAD_TETO_RODADA_S`=300, cabe no timer de 10 min) e limite de itens (`DOWNLOAD_LIMITE_RODADA`=40 / `LIMITE` posicional). | `test_guarda_de_conexao_tem_prazo_duro`, `test_prazo_total_por_arquivo_*`, `test_rodada_respeita_limite_*` |
| **S14** | corrigido | Nada inteiro em memória; teto `DOWNLOAD_TETO_MB` (50) checado por SIZE, Content-Length e no stream → `TAMANHO` permanente sem baixar. | `test_teto_de_tamanho_nao_baixa` |
| **OP-O5** | corrigido | Mídia (`tipos_documento` + `tipo` audio/video/voice + `midias` do lote) → `ignorado_midia`, sem rede quando o nome é conhecido; se o NAME do disk.file.get revela mídia, não faz o GET. Id não numérico (CQA…) → `telegram_legado` permanente, zero rede. Saída 1 quando há permanente nova/telegram legado novo/credencial; 0 em transitório; 2 em estado/fila ilegível ou sem fonte. Logs com `seguranca.instalar_filtro_log`. | `test_midia_*` (2), `test_id_telegram_*` (2), `test_access_denied_permanente_codigo_de_saida_e_sem_retry` |
| checar_midias | corrigido | `e_midia_anexo` (tipos_documento + type), `resumo_estado` (mídias em lote × marca de ignorada), sem `/opt` literal, sem `argv` no import, `--so-estado` sem rede; erro de leitura mascarado. | `test_checar_midias_usa_tipos_documento` |

## Mudanças de comportamento (não silenciosas)
- `baixar_pendentes` não baixa mais direto: enfileira em `download_fila.json` e processa a fila inteira (inclusive itens do ciclo) com teto. Arquivos agora `NN_<NAME>` como antes; VibeCode sem NAME usa `nome_original` do meta ou `file_<id>`.
- Webhook vem de `bitrix_cliente.webhook()` (segredo `webhook_bitrix`), não mais de `/opt/leads-doenca/config.json`; VibeCode de `vibecode_key` (não mais `/tmp/b24_webhook.txt` nem leitura direta de `/root/.vibecode/api_key` — esse caminho segue como legado explícito do `seguranca`). **Servidor precisa ter `TRIAGEM_WEBHOOK_BITRIX` ou `/etc/triagem-bitrix/webhook_bitrix`.**
- `download_fila.json` passa a ficar em `caminhos.em_base(...)` (= `/opt/triagem-bitrix`, igual) ou `$DOWNLOAD_FILA_JSON`; arquivo 0600; ganha campos novos; estado `falha` some (migra).
- `processar_fila` ignora o argumento `webhook` e `base_delay`; `max_tentativas` vira teto extra opcional. Itens processados têm `fonte` = `bitrix`/`vibecode` (antes `primaria`/`fallback`); `fontes` do healthcheck ganham chaves `bitrix`/`vibecode`.
- QUERY_LIMIT no download abre `circuit-bitrix-download.json` por ≥180 s (afeta também a C1 quando ela migrar para esse circuito — é o comportamento legado).
- Desfechos gravados no `estado.json` em lote por rodada (`atualizar_lotes`, dono `download`), só se o arquivo existir; nunca cria estado vazio. Item sem `lote_id` é localizado pelo `file_ids` do estado.
- `pipeline.sh` usa `baixar_pendentes.py | tail -4` sem pipefail: o código de saída novo não interrompe o pipeline (as 4 últimas linhas são o resumo).

## Pendências para outras frentes
- **C1 (triagem_grupo.py)**: o `processar_fila` dentro do ciclo hoje roda com os padrões (até 40 itens / 300 s segurando a trava do ciclo). Passar `limite_itens=5, teto_rodada_s=60` — ou retirar do ciclo e deixar só o timer do `baixar_pendentes`. Ao enfileirar (`_enfileirar_retry`), passar `lote_id=`/`indice=` para o estado ser atualizado sem busca. Migração do legado `circuit-download.json` → `circuit-bitrix-download.json` (renomear) é da C1; aqui só se lê o legado.
- **F (reconciliadores)**: usar `download_svc.arquivo_valido(caminho)` (exclui `.part/.parcial/.tmp/.meta.json`, nomes ocultos e 0 byte) nos filtros por índice (E4 dos reconciliadores). Meus temporários começam com `.` e não casam com `NN_`.
- **Orquestrador/deploy**: segredo `webhook_bitrix` no servidor; opcional `VIBECODE_RATELIMIT_FILE`; `TimeoutStartSec` da unit ≥ `DOWNLOAD_TETO_RODADA_S` + folga.
- **E (monitor/painel)**: listar `falha_permanente`/`telegram_legado` de `download_fila.json` como "precisa de humano" e ler `download_svc.py --status`.

## Riscos / precisa do servidor
- SIZE do `disk.file.get` passa a ser exigido; o ciclo registrou divergências de SIZE em arquivos íntegros (comentário em triagem_grupo sobre 1767 avisos — foram em reaproveitamento). Se ocorrer em download novo, o item vira `pendente` (INTEGRIDADE) e após 3 tentativas `falha_permanente` — conferir `download_svc.py --status` após o deploy.
- Primeira rodada após o deploy: itens antigos `falha` voltam a pendente e os 453 pendentes entram na fila; com 40 itens/rodada drena em ~2 h de timer.
- Contar no servidor `.parcial/.part`/0 byte em `lotes/` antes (serão removidos se > 30 min).
- VibeCode limitado em 1,5 req/s por padrão (mesma env `BITRIX_RATE`); ajustar se a cota da VibeCode for outra.
