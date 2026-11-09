# Frente C2a (onda 3) — Fase 1 incremental (cursor, bloco aberto, lote_id estável, timeout)

Status: **CONCLUÍDO** (F1-01, F1-02, F1-10 corrigidos; F1-11 documentado abaixo).

## Arquivos
- `triagem_grupo.py` (alterado: leitores, `_ler_mensagens_com_breaker`, helpers F1 antes de `ciclo`, `ciclo`, varredura, flag `--reler-janela`)
- `lotes_regras.py` (alterado: `kit_completo`, `fechamento_por_timeout`)
- `testes/test_fase1_cursor.py` (novo, 9 testes; contém a fonte falsa `FonteFalsa` e o fixture `ambiente`)
- `testes/test_fase1_lotes_estaveis.py` (novo, 7 testes; a simulação `sim_fase1.py` virou teste)
- Nenhum teste existente foi alterado.
- Não mexi em: barreira, extração de nome/CPF, validação de CPF, config/modo_traco/fonte_leitura, `agrupar()` (só o ciclo decide o que gravar).

## Desenho
Estado por chat (dois campos novos no `estado.json`, gravados na 2ª transação do ciclo, `atualizar_estado`):
- `cursores[dialog]`: maior id de mensagem já incorporada. O campo já existia, mas antes só era gravado; agora também é lido.
- `f1_regiao[dialog] = {"inicio", "mensagens", "em"}`: a **região provisória**, com as mensagens (já classificadas, senha removida por `seguranca.remover_senhas`) do bloco ABERTO e da janela que ainda está se formando. Se a chave existe, o chat já está no modo incremental.

Cada ciclo agrupa a região provisória mais as mensagens com `id > cursor`. Depois do agrupamento, `_filtrar_lotes_incrementais` decide o que é gravado e onde começa a próxima região:
- **Provisório:** status ABERTO (o `agrupar` já fechou por timeout o que venceu) ou lote de janela com `agora - fim <= janela_min_fallback`. Ele fica na região. Bloco ABERTO com material é gravado (status ABERTO, nunca enfileirável). Lote de janela ainda em formação **não** é gravado.
- **Fechado:** sai da região e não é mais reagrupado. Se já existe fechado no estado, não é regravado (o teste verifica que `atualizar_lotes` recebe zero lotes nos ciclos seguintes).

## Achados

| ID | Status | O que mudou | Testes |
|---|---|---|---|
| **F1-01** | corrigido | **Webhook:** `buscar_mensagens(..., first_id=cursor)` chama `_buscar_mensagens_desde`, que faz `FIRST_ID=<cursor>, LIMIT=50` e repete com o MAIOR id recebido até vir vazio ou sem id novo. Nunca para por `len==LIMIT` (a doc diz que pode vir mais que LIMIT). FIRST_ID inexistente devolve `[]`, por isso uma sonda da página mais nova confere se há id maior que o cursor; se houver (mensagem do cursor apagada), lê descendo até o cursor. **API VibeCode:** o `firstId` existe em `ler_pagina_mensagens`, mas a ordem/semântica dele nunca foi medida (se devolver as 50 MAIS NOVAS acima do cursor, as do meio somem sem aviso). Por isso usei o `lastId` descendente, que já é verificado, com `ate_id=cursor`: para ao ver id <= cursor e devolve só os ids maiores que o cursor. Página curta no meio não encerra mais a leitura incremental. Teto de páginas: `F1_MAX_PAGINAS_INCREMENTAL=40`. **Lacuna:** se as páginas acabam antes de alcançar o cursor, `info["lacuna"]` faz o ciclo não avançar o cursor nem a região, registrar `LACUNA` em `resumo["erros"]` e no log, e emitir uma linha de alerta. Erro no meio da paginação faz o ciclo abortar sem gravar e o cursor fica onde estava. **Ordem:** os lotes são gravados (`atualizar_lotes`) e só depois vêm cursor e região (`atualizar_estado`). Se cair entre as duas, o próximo ciclo relê e chega aos mesmos ids. **1ª execução** (sem `f1_regiao[dialog]`): leitura antiga limitada, e daí em diante incremental. `--reler-janela` força a leitura antiga (recuperação). | cursor::`test_first_id_usado_na_segunda_leitura`, `test_first_id_nao_para_por_limit_quando_vem_mais_que_50`, `test_cursor_nao_avanca_quando_a_gravacao_falha`, `test_erro_no_meio_da_paginacao_nao_avanca_cursor`, `test_cursor_apagado_sonda_e_le_descendo_ate_o_cursor`, `test_lacuna_na_api_nao_avanca_cursor_e_registra`, `test_api_incremental_para_ao_alcancar_o_cursor` |
| **F1-02** | corrigido | **lote_id estável:** mantive a fórmula `sha1(autor_id-id da 1ª mensagem do bloco)`, que já é determinística. A instabilidade vinha do começo da janela, que mudava a 1ª mensagem do bloco. Com a região começando sempre na mensagem que abriu o bloco (o traço, ou a 1ª mensagem da janela/ficha), o id não muda mais. A vantagem de não mudar a fórmula: **os ids já gravados continuam válidos** e nada é renomeado. Um lote já no estado com a mesma 1ª mensagem recebe o mesmo id e é atualizado por merge. Não incluí `dialog_id` na semente porque os ids de mensagem do Bitrix são globais (auditoria F1-11), e incluí-lo renomearia todo o histórico. **Lotes artificiais:** escolhi **não criar** em vez de criar como IGNORADO. Lote sem arquivo, sem CPF e sem ficha/cabeçalho/identificador não é gravado (kit = arquivo OU identidade, então nenhum kit legítimo se perde; a conversa continua no Bitrix). Não criar também evita encher o estado com milhares de registros IGNORADO. Lote NOVO que reaproveita mensagens de lote já FECHADO: se o que sobra não tem material, não é criado; se tem, nasce com `marcar_revisao` ("F1-02: lote reaproveita mensagens já incorporadas…"), nunca enfileirável sozinho. Os ~900 lotes históricos **não** foram tocados. Contadores por ciclo em `resumo["f1_descartes"]`. | estaveis::`test_simulacao_3_kits_janela_andando_vira_3_lotes`, `test_rodar_o_ciclo_3x_com_a_mesma_fonte_nao_cria_lote_novo`, `test_kit_que_atravessa_dois_ciclos_fica_num_lote_so`, `test_bloco_aberto_pelo_traco_atravessa_ciclos_com_o_mesmo_id`, `test_conversa_solta_nao_vira_lote`, `test_mensagem_ja_em_lote_fechado_nao_gera_lote_novo`, `test_lote_existente_aberto_mantem_o_id_antigo` |
| **F1-10** | corrigido | Regra única em `lotes_regras.fechamento_por_timeout(reg, motivo, agora)`. Kit completo (identificação OK + arquivo + sem revisão) fecha PRONTO. Qualquer outro caso vai para revisão via `estado_lote.marcar_revisao`, com motivo "fechado por timeout sem o traço de fechamento e kit incompleto (…)", sem apagar os motivos existentes. A varredura de vencidos do ciclo usa essa regra, que é a mesma do pós-processamento do `agrupar`: timeout com OK dá pronto, sem OK dá requer_revisao. Acabou o caso `pronto=False` + `requer_revisao=False`. | cursor::`test_timeout_da_varredura_vai_para_revisao_visivel`, `test_regra_de_timeout_unica_kit_completo_fica_pronto` |
| **F1-11** | documentado | ver "Limites reais" abaixo | — |

## Evidência

### Testes novos contra o código de HEAD
Montagem: cópia da árvore atual em `~/.hermes/cache/scratch/c2a/red` (sem .git/.venv/docs), com `triagem_grupo.py` e `lotes_regras.py` trocados por `git show HEAD:…`.
```
env -u TRIAGEM_DIR .venv/bin/python -m pytest -q -p no:cacheprovider testes/test_fase1_cursor.py testes/test_fase1_lotes_estaveis.py
→ 11 failed, 5 passed
   simulação no HEAD: "3 kits viraram 19 lotes" (fragmentos LOTE-… e LOTE-SIG-… a cada ciclo)
   3× mesma fonte: HEAD regrava os mesmos lotes fechados a cada ciclo
   kit atravessando ciclos / conversa solta: HEAD grava lotes SEM_CPF+SEM_ARQUIVOS
   lote legado fechado: HEAD cria lote novo para o mesmo kit
   FIRST_ID: HEAD nunca usa (relê descendo); API: 9 páginas em vez de 1
   timeout: HEAD grava pronto=False e requer_revisao=False
```
Os 5 que passam no HEAD são de regressão/compatibilidade, porque o HEAD relê tudo e não tem como errar nesses cenários: `test_first_id_nao_para_por_limit…`, `test_erro_no_meio_da_paginacao…`, `test_cursor_apagado_sonda…`, `test_bloco_aberto_pelo_traco…`, `test_lote_existente_aberto_mantem_o_id_antigo`.
Os obrigatórios do briefing falham todos no HEAD: 3×, janela andando (3 kits → 3 lotes), kit em dois ciclos, cursor sem gravação (a releitura tem de partir do cursor com FIRST_ID), FIRST_ID na 2ª leitura e timeout visível.

### Código atual
```
.venv/bin/python -m pytest -q testes/test_fase1_cursor.py testes/test_fase1_lotes_estaveis.py → 16 passed
.venv/bin/python -m pytest -q                → 968 passed, 2 skipped in 14.91s
env -u TRIAGEM_DIR .venv/bin/python -m pytest -q → 968 passed, 2 skipped
.venv/bin/python -m pyflakes triagem_grupo.py lotes_regras.py → só os 3 avisos que já existiam (PALAVRAS_NAO_NOME, cpf_msg, nomes_msg)
```
Isolamento: `test_circuit_breaker_integracao.py` troca `tg.buscar_mensagens`, `tg.buscar_mensagens_api` e `tg.ARQ_BREAKER` direto no módulo, sem monkeypatch, e isso vaza para os testes seguintes. O fixture `ambiente` guarda as funções reais na coleta e as restaura. O arquivo não é meu (fica como pendência).

## F1-11 — limites reais de perda e releitura depois da mudança
- **Releitura por ciclo:** só as mensagens novas (`id > cursor`) mais a região provisória, que é o bloco aberto (até `timeout_lote_min`=120 min de atividade) e a janela em formação (até `janela_min_fallback`=30 min). No ciclo ocioso são 1 chamada FIRST_ID mais 1 sonda no webhook, ou 1 página na API. Antes eram até 40 páginas no webhook e 12 na API, em todo ciclo. Teto da região: `F1_MAX_REGIAO=2000` mensagens (acima disso guarda as mais novas, com aviso).
- **Perda:** webhook: a leitura é ascendente a partir do cursor, então não há buraco. Se bater o teto de 40 páginas, o resto entra no próximo ciclo. API: se mais de 40×50 = 2.000 mensagens chegarem num chat entre dois ciclos (serviço parado ou disjuntor aberto), vira **LACUNA**: o cursor não anda, há alerta e o caminho de recuperação é `--paginas N` ou `--reler-janela`. A perda deixou de ser silenciosa. Mensagem do cursor apagada: tratada pela sonda (webhook); na API não afeta, porque a leitura é descendente.
- **Duplicação:** um lote fechado não é regravado nem reagrupado. Uma mensagem já em lote fechado não gera lote novo (ou gera, mas em revisão, se trouxer material novo). Apagar a 1ª mensagem de um bloco **aberto** pode mudar o id dele no próximo ciclo, porque a região é reconstruída a partir do estado, onde a mensagem continua: na prática o id se mantém até o bloco fechar. Editar mensagem: a versão nova só é lida se o id for maior que o cursor. **Edição de mensagem antiga (já incorporada) não é mais relida.** É uma mudança de comportamento: antes a edição entrava enquanto estivesse na janela.
- **Fronteira janela/traço:** um traço que chega mais de 30 min depois do último documento sem traço não "puxa" mais esses documentos, que já viraram lote de janela final. Antes isso dependia de onde a janela começava e gerava as duas versões. O prefixo do id muda com o modo (`LOTE-` para janela, `LOTE-SIG-` para traço), por isso o lote de janela em formação não é gravado.
- **Dois ciclos simultâneos** (`trava_de_ciclo` desiste após 300 s): os dois leem o mesmo cursor e gravam os mesmos ids (idempotente). O cursor usa `max()` e a região do último que gravou vence. Não muda o que já estava descrito para os downloads.

## Mudanças de comportamento para o deploy
1. Na 1ª execução depois do deploy, cada chat faz uma leitura antiga (janela) e grava `f1_regiao`. Daí em diante a leitura é incremental. Lotes já gravados mantêm o id.
2. Kit sem traço (fallback por janela) só é gravado quando a janela de 30 min termina. Antes era gravado como pronto no ciclo seguinte, mesmo ainda incompleto.
3. Conversa solta (sem arquivo, CPF, ficha ou cabeçalho) não vira mais lote.
4. Lote fechado por timeout na varredura: kit completo fica PRONTO, o resto vai para REVISÃO com motivo.
5. `resumo` ganha `cursor_chat_antes/depois`, `incremental`, `mensagens_novas`, `mensagens_regiao`, `regiao_provisoria`, `f1_descartes` e, quando houver, `lacuna`/`leitura_truncada`.

## Pendências para outras frentes / riscos
- **Orquestrador/dono de `test_circuit_breaker_integracao.py`:** trocar as atribuições diretas em `tg.*` por `monkeypatch` (hoje vazam para outros testes).
- **Próxima frente (mesmo arquivo):** barreira, nome/CPF e `fonte_leitura` não foram tocados. Com o estado do bloco persistido, a ambiguidade de paridade do F1-07 deve sumir; vale confirmar.
- **Servidor:** medir a semântica do `firstId` da VibeCode (ordem e limite). Se ele devolver ascendente, dá para trocar o `lastId` descendente por ele. Confirmar que o webhook tem permissão no chat (Linha Aberta, doc 07 §2), porque com FIRST_ID um subconjunto continua sendo subconjunto.
- **Plano de recuperação (fora desta frente):** os ~900 lotes vazios históricos continuam no estado. Para sanear, basta marcá-los IGNORADO por script sob `estado_store`, com o mesmo critério de `_tem_material`.
- **Risco:** a região provisória guarda mensagens classificadas (texto sem senha) no `estado.json`, o que aumenta um pouco o tamanho do arquivo (limitado a 2.000 mensagens por chat; no normal são dezenas).
