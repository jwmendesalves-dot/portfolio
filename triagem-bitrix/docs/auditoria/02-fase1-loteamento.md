# 02 — FASE 1: leitura + montagem de lotes (`triagem_grupo.py` e satélites)

Escopo: `triagem_grupo.py` (4.116 l.), `lotes_regras.py`, `telegram_regras.py`, `pergunta_traco.py` (parte de lote), `estado_store.py`, mais os scripts do pipeline que gravam `estado.json` (necessários para o item f).
Código lido: cópia local `Downloads/triagem-codigo-20261109-1552`. Nada foi alterado na pasta do código (pytest com `-p no:cacheprovider` e `PYTHONDONTWRITEBYTECODE=1`; nenhum `__pycache__` criado).

**Verificação executada**
- Testes: `test_traco_delimitador, test_lotes_regras, test_titulo_ficha, test_cabecalho_encaminhado, test_senha_cabecalho_curto, test_nome_do_cliente_prefixo, test_telegram_{batidas,download,grupo,regras,respostas}` → **81 passed**. Nenhum deles cobre janela deslizante, sobrescrita do estado ou CPF com DV inválido.
- Simulação offline (sem rede) com o `config.json` real: `scratch/triagem-audit/sim_fase1.py`. São 3 kits sintéticos e 6 "ciclos" com a janela de leitura andando 1 mensagem por vez. Resultado: **9 lotes distintos para 3 kits**. Saída usada como evidência em F1-01, F1-02 e F1-05.

---

## Achados

### [F1-01] O "cursor por chat" é gravado mas nunca lido: cada ciclo relê e reagrupa a janela inteira (2.000 msgs no webhook)
· **Severidade: CRÍTICA** · `triagem_grupo.py:2817-2822, 2873-2879` (só escrita), `:2669` (leitura), `:1245` (`max_paginas=40`)
- Evidência: `grep cursores|cursor_msg_id` em todo o código só acha escrita (`fresco["cursores"][dialog] = max(...)`). O único filtro de leitura é `--desde-msg-id`, que vem da CLI (`:2685`) e não é usado em produção. No modo webhook, `buscar_mensagens(w, dialog, cfg=conf)` lê 40×50 = **2.000 mensagens por ciclo e por chat**, e `historico_paginas=12` é ignorado. O próprio código comenta "2000 mensagens → ~170-230 lotes" (`:2740`).
- Impacto: o CONTEXTO diz "cursor por chat (não relê o histórico)", e isso é falso. Todo ciclo (loop de 120/300 s, mais um ciclo extra a cada traço disparado por `pergunta_traco.py:590`) reagrupa até 2.000 mensagens e regrava cada lote. É a raiz de F1-02, F1-03 e F1-07. Também gasta cota do webhook, o mesmo usado no download (ver F1-06).
- Correção: usar o cursor de verdade. Ler só `id > cursores[dialog] − margem` (margem = mensagens do bloco aberto, ou seja, a partir de `sinal_abertura.message_id` do último lote ABERTO do chat) e manter `aberto/pendentes` persistidos no estado entre ciclos. Paliativo imediato: ler com `max_paginas` = `historico_paginas` também no webhook.
- Confiança: confirmado no código.

### [F1-02] Janela deslizante gera lotes-fragmento novos a cada ciclo (lote_id muda) → origem principal dos lotes vazios/BAIXA e dos duplicados
· **Severidade: CRÍTICA** · `triagem_grupo.py:1322-1328` (`_lote_id` = hash da 1ª mensagem), `:1732-1735`, `:1601-1670` (corte por "troca de autor"), `:2870` (`update`, sem remover nada)
- Evidência: `lote_id = sha1(f"{autor_id}-{id da 1ª msg}")`. Quando a janela anda, o pedaço de cima (mensagens antes do 1º traço da janela) vira um lote com outra 1ª mensagem, ou seja, outro ID, e é gravado como lote novo. O antigo continua no estado. Fora de bloco, `agrupar_por_janela` ainda corta a cada **troca de autor**: cada fala solta de outra pessoa vira um lote de 1–2 mensagens. Simulação (`sim_fase1.py`), mesmos 3 kits:
  ```
  ciclo=1 msgs=5 arqs=2 ALTA OK      <- mesmo kit 1, ID novo (duplicata pronta)
  ciclo=2 msgs=4 arqs=2 ALTA OK      <- de novo
  ciclo=3 msgs=3 arqs=1 ALTA OK      <- kit parcial "OK" com 1 de 2 arquivos
  ciclo=4 msgs=2 arqs=0 SEM_ARQUIVOS <- lote vazio
  ciclo=5 msgs=2 arqs=0 REVISAO SEM_CPF
  ```
- Impacto: casa com os números do CONTEXTO: **900 lotes com zero arquivo (850 com 1–2 msgs)**, **1.102 BAIXA (janela)** e **503 descartes por sha256 idêntico** na fase 2. Os descartes são os fragmentos com arquivo do mesmo kit, que hoje só a dedupe da ponte segura. Fragmento com parte dos arquivos e CPF sai `pronto` (ciclo=3) e pode virar job com kit incompleto quando o sha256 não bate 100%. Isso é hipótese: depende da regra exata de dedupe da fase 2.
- Correção na origem, sem perder kit legítimo:
  1. Quando a leitura foi truncada (bateu `max_paginas` sem página curta), **descartar todo lote que contém a 1ª mensagem da janela**. Ele é parcial por definição, e o lote inteiro já foi gravado em ciclo anterior.
  2. Em `agrupar()`, no pós-processamento (`:2002`), **não persistir lote com `file_ids == []`, `cpfs == []` e nenhuma mensagem `e_identificador`** (marcar `status="IGNORADO"`, `pronto=False`, `requer_revisao=False`). Não perde kit, porque kit = arquivo ou identidade. Lote só com ficha/CPF continua (SEM_ARQUIVOS em revisão).
  3. Em `agrupar_por_janela`, só cortar por "troca de autor" quando a mensagem nova tem arquivo ou identidade. Conversa solta não abre lote.
- Confiança: mecanismo confirmado no código e na simulação. A proporção exata nos 900/1.102 é hipótese (precisa do `estado.json`).

### [F1-03] Cada ciclo SOBRESCREVE o registro inteiro do lote e apaga o que outros scripts gravaram (CPF por nome, descarte de duplicata)
· **Severidade: ALTA** · `triagem_grupo.py:2812-2814` (`serializar_lote` monta um dict novo), `:2870` (`fresco["lotes"].update(lotes_criados)`)
- Evidência: o delta é "por lote inteiro", não "por campo". Campos que outros processos gravam no mesmo registro se perdem no próximo ciclo enquanto o lote estiver na janela: `completar_cpf_por_nome.py:126-139` (`cpf`, `nome`, `identificacao="OK"`, `pronto=True`, `cpf_origem`) e `lote_para_fila.py:125-147` (`status="descartado_duplicata"/"revisao_duplicata"`, `lote_origem`, `descartado_em`).
- Impacto: (a) CPF completado volta a `None`/SEM_CPF; (b) o `completar` acha o mesmo lote de novo a cada 10 min, regrava e **cria backup** (ver F1-04); (c) o descarte de duplicata é desfeito. A checagem "já alertado antes — silêncio" (`lote_para_fila.py:131`) compara `status` e falha, o que gera realerta (hipótese de efeito; o mecanismo está confirmado).
- Correção: em `_aplicar` (`:2869`), fazer merge. Preservar do registro existente os campos `cpf_origem/nome_origem == "bitrix-nome"` (com `cpf`, `nome`, `identificacao`, `pronto`), `status` em `descartado_*`/`revisao_duplicata` e `descarte_*`/`lote_origem`. Só sobrescrever `mensagens`, `file_ids`, `arquivos`, `fim`, `status` de ciclo de vida.
- Confiança: confirmado no código.

### [F1-04] Backup `estado.json.bak-*` sem retenção: a linha que cria está em `completar_cpf_por_nome.py:140-141`, e o gatilho é F1-03
· **Severidade: ALTA** (27 GB, ~2,1 GB/dia) · `completar_cpf_por_nome.py:140-141`
- Evidência:
  ```python
  backup = f"{ESTADO}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
  shutil.copy2(ESTADO, backup)
  ```
  Roda sempre que `--aplicar` acha ≥1 lote (`if a.aplicar and achados`). É chamado a cada 10 min pelo pipeline (`pipeline.sh:13`, timer `triagem-lote-fila`). Por F1-03, sempre há "achados": são os mesmos lotes que o ciclo da fase 1 zerou. 6 rodadas/h × 24 × 15 MB ≈ **2,1 GB/dia**, igual ao medido. O mesmo padrão sem retenção existe em `reconciliar_arquivos_disco.py:108-110`, `nomear_lotes_por_cpf.py:206`, `backfill_nome_lotes.py:86` e `reparo_josilene.py:123` (manuais, menos frequentes). `triagem_grupo.py` **não** cria `.bak`.
- Correção:
  1. Imediata (o dono roda no servidor): `cd /opt/triagem-bitrix && ls -1t estado.json.bak-* | tail -n +25 | xargs -r rm --`. Mantém os 24 mais novos e libera cerca de 26,6 GB.
  2. No código: criar em `estado_store.py` um helper `backup_rotativo(caminho, manter=24, dir="backups/estado")` que só copia se o sha256 for diferente do último backup e apaga os mais antigos. Trocar as 5 chamadas `shutil.copy2(ESTADO, backup)` por ele.
  3. Atacar a causa (F1-03). Com o merge, o `completar` deixa de reachar os mesmos lotes e o backup fica raro.
  4. O `completar` grava sem a trava do `estado_store`, com `.tmp` de nome fixo: migrar para `atualizar_estado()` (ver F1-08).
- Confiança: linha e frequência confirmadas no código. O ciclo "zera → completa → backup" está confirmado no código. O número de backups por dia vem do CONTEXTO.

### [F1-05] Barreira de identidade dispara com "nome" tirado de conversa ("bom dia", "Segue documentos") e corta o kit do PRÓPRIO cliente
· **Severidade: ALTA** · `triagem_grupo.py:922-925` (regra 4: 1ª linha solta vira nome), `:1501-1505` (barreira usa `lote["nomes"]`, não `nomes_ident`), `:1647`, `:1880`
- Evidência (executado): `extrair_nomes('bom dia') → ['bom dia']`, `_e_nome_valido('bom dia') → True`, `extrair_nomes('Segue documentos') → ['Segue documentos']`. Na simulação (ciclo 5), o cabeçalho real da cliente cortou o bloco: `INTERROMPIDO pela barreira (nome 'JOANA TESTE LIMA' != 'BOM DIA' do bloco)` → lote `REVISAO`, mais 3 mensagens realocadas.
- Impacto: lote vai para REVISAO ou fica com `nome="bom dia"` sem motivo real. Isso alimenta os **1.349 requer_revisao** e o `CONFLITO`/`FECHADO_POR_TROCA_DE_PESSOA`. Também faz `_realocar_sem_identidade` (`:1531-1533`) parar antes da hora, porque a mensagem com "nome" é tratada como do cliente A.
- Correção: (1) em `_muda_identidade`, no corte de `agrupar_por_janela` e no bloco do `traco_no_fim_fecha`, montar `nomes_lote` a partir de `lote["nomes_ident"]` (nomes vindos de ficha/cabeçalho/CPF), nunca de `lote["nomes"]`; (2) a regra 4 (`:923-925`) só deve valer quando a mensagem tem CPF ou linha `=`; (3) incluir saudações e frases de serviço (BOM DIA, BOA NOITE, SEGUE, DOCUMENTOS…) em `nome_regras.PALAVRAS_NAO_NOME`.
- Confiança: confirmado (execução real das funções).

### [F1-06] `fonte_leitura="webhook"`: sem failover para a API, sem breaker, ignora `historico_paginas`, e o bot lê por OUTRA fonte
· **Severidade: ALTA** · `config.json` (`"fonte_leitura": "webhook"`), `triagem_grupo.py:2665-2670`, `:505` (padrão `api`), `pergunta_traco.py:419`, `triagem_grupo.py:332-336`
- Evidência:
  - Lido só em `ciclo()` (`:2665`). Com `webhook`, cai em `buscar_mensagens(w, dialog, cfg=conf)` direto. Não passa por `_ler_mensagens_com_breaker` (`:2573`), então **não existe fallback para a API** se o webhook falhar: o ciclo grava erro e não forma lote (`:2679-2682`). O failover só existe no sentido API → webhook.
  - O resumo rotula `"[DIAGNÓSTICO]"` (`:2670`), mas em produção essa é a fonte primária.
  - O bot do traço (`pergunta_traco.py:419`) lê **pela API** (`buscar_mensagens_api`, 2 páginas) e anuncia "lote registrado, N arq." contando os arquivos que a API vê (`:477`). A esteira monta o lote com o que o **webhook** vê. Se o webhook não trouxer um anexo ou traço, o grupo ouve "N arquivos" e o lote sai com menos. É o caso "kit sem anexo" sem alarme.
  - Rate limit na **leitura** (`QUERY_LIMIT_EXCEEDED` em `im.dialog.messages.get`) chama `_registrar_bloqueio_download()` (`:332-336`) e **abre o disjuntor de DOWNLOAD**. Leitura de 40 páginas × 2 chats por ciclo, mais um ciclo por traço, mais `completar_cpf_por_nome` consultando `crm.contact.list` a cada 0,55 s sobre todos os SEM_CPF a cada 10 min: tudo no mesmo webhook. Plausível ligação com o "circuit-breaker aberto desde 09/11 13:06" e os 78 arquivos pausados (hipótese).
  - Observação importante: a medição "webhook é subconjunto (386 × 234)" é de 21/09. Em 21/10 o código registra que, **sem LAST_ID, o webhook devolvia as mensagens mais antigas** (`:1251-1255`), e a paginação foi corrigida. É possível que a medição de 21/09 tenha sido distorcida por esse bug e que a troca para webhook tenha sido proposital (instabilidade da API). Hipótese: precisa medir de novo.
- Correção: (1) rodar `triagem_grupo.py --testar-fonte` no servidor e comparar IDs API × webhook do dia antes de decidir; (2) qualquer que seja a fonte, rotear as duas por `_ler_mensagens_com_breaker` com failover nos dois sentidos; (3) no modo webhook, respeitar `historico_paginas`; (4) a leitura não deve abrir o disjuntor de download: separar `_registrar_bloqueio_leitura()`; (5) o bot e a esteira devem usar a mesma fonte (ler `fonte_leitura` em `pergunta_traco`).
- Confiança: confirmado no código. A causa do disjuntor e a cobertura atual do webhook são hipóteses.

### [F1-07] `modo_traco`: o config diz que a decisão do dono é `grupo_autor`, mas roda `delimitador`, e o resultado depende de onde a janela começa
· **Severidade: MÉDIA** · `config.json` (`modo_traco: "delimitador"`), `triagem_grupo.py:487-491, 559-567, 587-604, 1767-1792, 1826-1926`, `relatorio.py:332-362`
- O que cada modo faz no código:
  - `grupo_autor`: o traço de qualquer um abre. Só o traço de quem abriu fecha. Traço de outra pessoa é ignorado, salvo bloco parado > `traco_vigencia_min` (FECHADO_POR_INATIVIDADE).
  - `grupo`: qualquer traço alterna abre/fecha.
  - `autor`: cada pessoa tem seu toggle.
  - `delimitador` (24/09, pedido do dono, só no comentário de `:1768`): **todo traço fecha o bloco de cima e abre o de baixo**. Não espera timeout. `traco_vigencia_min` não tem efeito.
  - `delimitador_autor`: idem, mas o traço de outra pessoa é ignorado.
- Divergência: o `_comentario` do config, `CONFIG_PADRAO` (`:491`), `MODOS_TRACO` (`:563`) e a docstring de `agrupar` (`:1683-1692`) não citam `delimitador`. O bot (`relatorio.abre_ou_fecha`/`decidir_resposta`) implementa delimitador, então bot e esteira estão coerentes **hoje**. Riscos:
  1. Se o `config.json` ficar inválido, `carregar_config` (`:543-544`) engole o erro e volta a `CONFIG_PADRAO`, que é **`grupo_autor` + `fonte_leitura=api`**. A esteira muda de regra em silêncio e o bot continua em delimitador.
  2. Com `traco_no_fim_fecha=True`, o traço que fecha "pelo fim" (`:1833-1926`) dá `continue` e **não abre** o bloco de baixo. Já o traço com bloco aberto fecha e abre. Assim, o mesmo trecho de conversa vira lotes diferentes conforme a janela comece antes ou depois de um traço. Na simulação, o kit 2 virou 1 lote OK no ciclo 0 e 2 lotes (REVISAO + OK) no ciclo 5.
- Correção: atualizar o `_comentario`, `CONFIG_PADRAO["modo_traco"]` e `MODOS_TRACO` para `delimitador`. Em `carregar_config`, se o JSON for inválido, **abortar o ciclo com alerta** em vez de cair nos padrões. Com F1-01 corrigido (estado do bloco persistido), a ambiguidade de paridade some.
- Confiança: confirmado no código e na simulação.

### [F1-08] Gravação do `estado.json`: 4 escritores fora da trava, `.tmp` de nome fixo, e estado corrompido vira "estado vazio"
· **Severidade: MÉDIA** · `estado_store.py:47-55, 37-44`, `triagem_grupo.py:2482-2497`, `reconciliar_estado.py:86-90`, `completar_cpf_por_nome.py:142-146`, `reconciliar_arquivos_disco.py:111-115`
- Evidência: `triagem_grupo.ciclo`, `lote_para_fila`, `reconciliar_por_file_ids` e `telegram_grupo.mesclar_no_estado` usam `atualizar_estado` (flock + releitura + tmp com pid + fsync). Já `reconciliar_estado.py`, `completar_cpf_por_nome.py`, `reconciliar_arquivos_disco.py`, `nomear_lotes_por_cpf.py`, `backfill_nome_lotes.py` fazem ler → modificar → `ESTADO + ".tmp"` → `os.replace` **sem trava**. Quem grava por último apaga o delta do outro: o bug de 24/09 continua aberto para esses scripts. O pipeline de 10 min roda em paralelo com o loop da fase 1. Além disso, `carregar_estado` (`:2491-2497`) e `ler_estado` (`estado_store.py:43`), com JSON ilegível, devolvem estado vazio. O ciclo então grava só o delta e **reduz 2.722 lotes aos da janela** (o `.corrompido` é sobrescrito na próxima vez).
- Correção: todos os escritores passam por `estado_store.atualizar_estado`. Em `ler_estado`/`carregar_estado`, JSON inválido deve **abortar** (exceção + alerta), nunca virar `{}`. Fazer fsync do diretório após `os.replace`.
- Confiança: confirmado no código. A ocorrência de perda real é hipótese.

### [F1-09] CPF com pontuação é aceito sem checar dígito verificador, e CPF digitado errado vira âncora, CONFLITO ou barreira
· **Severidade: MÉDIA** · `triagem_grupo.py:719-727`
- Evidência: `elif rotulado_cpf or tem_sep: aceitar = True`. O DV (`cpf_plausivel`) só é exigido para 11 dígitos crus. Executado: `extrair_cpfs("= CPF 123.456.789-00") → ['12345678900']` com `cpf_plausivel → False`; `extrair_cpfs("protocolo 123.456.789-10") → ['12345678910']`.
- Impacto: um dígito errado no cabeçalho, seguido do CPF correto em outra mensagem, dá 2 "CPFs de cliente". Resultado: **CONFLITO** (23 hoje), corte pela barreira no meio do kit, ou o CPF inválido vira `lote.cpf` e procura/cria o contato errado no CRM.
- Correção: guardar o CPF com DV inválido em `cpfs_invalidos` (para revisão), fora de `cpfs`. Ele não conta para barreira, CONFLITO nem `lote.cpf`. Se o lote só tiver CPF inválido → `identificacao="CPF_INVALIDO"`, em revisão.
- Confiança: confirmado (execução).

### [F1-10] Varredura de vencidos marca `pronto=False`, enquanto o mesmo status no agrupador é `pronto=True`: kit some sem aviso
· **Severidade: MÉDIA** · `triagem_grupo.py:2848-2852` vs `:2131-2136`, `lotes_regras.py:33-60`
- Evidência: `agrupar` fecha por timeout com `status_lote="FECHADO_POR_TIMEOUT"` e cai em `elif lote.get("fechado"): pronto=True`. A varredura grava `reg["pronto"]=False` para o mesmo status, sem mexer em `requer_revisao` nem `motivos_revisao`.
- Impacto: um lote ABERTO que saiu da janela (o caso que a varredura existe para pegar) e tinha kit completo fica `pronto=False` e `requer_revisao=False`. Não vai para a ponte nem para a fila de revisão.
- Correção: na varredura, recalcular `pronto` com a mesma regra (`identificacao == "OK"` → pronto) ou, no mínimo, `requer_revisao=True` com motivo "fechado por varredura de timeout".
- Confiança: confirmado no código.

### [F1-11] Perda e releitura de mensagens: limites concretos
· **Severidade: MÉDIA** · `triagem_grupo.py:1213-1232, 1256-1277, 1322-1328`
- Perda: sem cursor, só se lê a cauda (600 pela API, 2.000 pelo webhook). Se o serviço ficar parado ou o disjuntor aberto enquanto chegam mais mensagens que isso num chat, as mais antigas **nunca** são lidas, sem nenhum log (não há comparação com `cursores[dialog]`). Na API, o laço para em `len(msgs) < 50` (`:1227`): se a API devolver página incompleta no meio do histórico, para cedo (hipótese).
- Releitura/duplicação: editar mensagem recalcula o lote (ok). **Apagar a 1ª mensagem** de um lote já gravado, ou o 1º traço sair da janela, muda o `lote_id`. O lote velho fica no estado, o novo nasce e os dois podem estar `pronto` (ver F1-02).
- Dois chats: IDs de mensagem do Bitrix são globais, então não há colisão de `lote_id` entre chats. `cursor_msg_id` é um máximo global sem uso. `trava_de_ciclo` (`:4023-4047`) desiste após 300 s e roda **sem trava**: com download lento (até 120 downloads × 5 tentativas × 10 s), dois ciclos podem rodar juntos (o estado está protegido pelo delta sob flock, mas os downloads duplicam).
- Correção: F1-01 (cursor real + alerta quando `min(id lido) > cursor` = buraco) e `lote_id` estável (semente = `sinal_abertura.message_id` ou o 1º `file_id`, não a 1ª mensagem qualquer).
- Confiança: confirmado no código (perda/duplicação por mecanismo). Ocorrência real é hipótese.

### [F1-12] `completar_cpf_por_nome` grava CPF formatado, enquanto a fase 1 grava 11 dígitos
· **Severidade: BAIXA** · `completar_cpf_por_nome.py:132-134` vs `triagem_grupo.py:2020, 2526`
- Evidência: `lotes[lid]["cpf"] = cpf` com `cpf = formatar_cpf(...)` ("xxx.xxx.xxx-xx"). Na fase 1, `cpf` tem só dígitos e o formatado vai em `cpf_formatado`.
- Impacto: comparação de CPF entre lotes ou com a fila (dedupe, `ponte_criterio`) pode tratar como CPF diferente (hipótese: depende do consumidor normalizar).
- Correção: `lotes[lid]["cpf"] = re.sub(r"\D", "", cpf)`.
- Confiança: confirmado no código (o efeito a jusante é hipótese).

### [F1-13] Funções gigantes e exceções engolidas que mudam comportamento
· **Severidade: BAIXA** · `triagem_grupo.py`
- `agrupar` tem **482 linhas** (`:1673-2154`), com 5 modos de traço, barreira, traço no fim e pós-processamento juntos. `ciclo` tem **302** (`:2612-2913`). `testar_sinais` tem cerca de **790 linhas de teste dentro do arquivo de produção** (`:3001-3791`). `extrair_nomes_com_papel` tem 125.
- Exceções relevantes: `carregar_config` `except Exception` → usa padrões (`:543`, ver F1-07); `carregar_estado` `except Exception` → estado vazio (`:2491`, ver F1-08); `_registrar_bloqueio_download` `except OSError: pass` (`:130`, o disjuntor pode não abrir); `http_json` `except Exception: pass` ao ler o corpo do erro (`:317`, perde o marcador LOOP_DETECTED e martela o portal); `completar_cpf_por_nome.nome_do_lote` `except Exception: pass` (`:42`).
- Correção: separar `agrupar` em `_agrupar_delimitador`, `_agrupar_toggle` e `_posprocessar_lote` (o pós-processamento de `:2000-2154` vira função pura testável). Mover `testar_sinais` para `testes/`. Trocar os dois `except` de config/estado por abortar + alerta.
- Confiança: confirmado no código.

---

## Top 5 da frente (prioridade)
1. **F1-04 + F1-03**: apagar os backups antigos (manter 24), trocar o `copy2` de `completar_cpf_por_nome.py:140-141` por backup rotativo e fazer merge por campo em `triagem_grupo.py:2869-2870`. Isso para o ciclo "zera CPF → recompleta → backup" (27 GB).
2. **F1-01 + F1-02**: cursor por chat de verdade (hoje só é escrito). Até lá, descartar o lote que contém a 1ª mensagem da janela truncada e não persistir lote sem arquivo, sem CPF e sem identificador. Corta os ~900 vazios/1.102 BAIXA e as duplicatas por sha256 na origem.
3. **F1-06**: medir de novo API × webhook (`--testar-fonte`), pôr as duas fontes atrás do breaker com failover nos dois sentidos, separar o disjuntor de leitura do de download e fazer bot e esteira lerem pela mesma fonte.
4. **F1-05 + F1-09**: barreira e CONFLITO só com evidência de identificador (`nomes_ident`) e CPF com DV válido. "bom dia" hoje vira nome e corta kit, e CPF com dígito errado vira CONFLITO.
5. **F1-08 + F1-10**: todos os escritores sob `estado_store.atualizar_estado`, JSON inválido aborta em vez de zerar, e a varredura de timeout não pode deixar kit `pronto=False` sem revisão.

## O que NÃO deu para verificar sem o servidor
- A proporção real dos 900 vazios/1.102 BAIXA que vem de fragmento de janela, conversa solta ou bloco entre traços (precisa do `estado.json`: agrupar por `confianca`, `len(mensagens)`, e checar se a 1ª mensagem coincide com o início da janela da época).
- Se `completar_cpf_por_nome` acha de fato os mesmos lote_ids a cada rodada (journal do `triagem-lote-fila`: linhas `APLICADO: N lote(s)` e a lista de IDs).
- A cobertura atual do webhook × API (a medição de 21/09 pode ter vindo do bug de paginação sem LAST_ID corrigido em 21/10) e o motivo da troca para `webhook`.
- Se o disjuntor de download aberto às 13:06 foi provocado pela leitura (`im.dialog.messages.get`) ou pelo `crm.contact.list` do `completar` (logs `triagem.log` com "rate limit do Bitrix … em im.dialog.messages.get").
- Qual unit roda a fase 1 hoje (`triagem-bitrix.service` com `--loop 120` ou `300`: o README cita os dois) e se `pergunta_traco` dispara ciclos extras com frequência.
- Ocorrência real de corrida entre os escritores sem trava (precisa de log com timestamps de gravação).
