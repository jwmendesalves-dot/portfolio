# Frente X — Certidões e planilha (CE-01..05, S09/S12/S13, PL-01..03, OP-O4)

Branch `mimo/auxiliares` · worktree `triagem-bitrix-mimo` · Python 3.9 · sem rede nos testes.

> **Pós-onda 1:** `seguranca`, `cpf_util`, `rede_bitrix`, `estado_store`, `bitrix_cliente`,
> `tipos_documento` já estavam na branch (merge do orquestrador) → todo o trabalho usa
> **import direto**; nenhum fallback `# COMPAT-ONDA1` foi necessário.

## Evidências gerais (comandos reais)

```
$ .venv/bin/python -m pytest -q testes/test_pintar_linha.py testes/test_certidoes*.py testes/test_pintar_linha_*.py
..................................................................       [100%]
66 passed in 1.04s

$ .venv/bin/python -m pytest -q            # suíte inteira (baseline do repositório)
14 failed, 816 passed in 7.51s
# as 14 falhas são as do baseline (dependem de dados de produção, ex. /opt/triagem-bitrix/estado.json):
#   test_nome_regras, test_pasta_fichinha, test_ponte_fila, test_servico_lote (3),
#   test_telefones_fichinha, test_variaveis_contrato (7) — nenhuma é da Frente X.
```

Arquivos alterados/criados pela frente:
`certidoes.py`, `certidoes-rodar.sh`, `pintar_linha.py`, `testes/test_pintar_linha.py`,
`testes/test_certidoes_estado.py` (novo), `testes/test_certidoes_identificacao.py` (novo),
`testes/test_certidoes_rodar.py` (novo), `testes/test_pintar_linha_planilha.py` (novo),
`testes/test_pintar_linha_pl03.py` (novo), este relatório.
`backup_pintura.py` e `conferir_pintura.py` **não** foram tocados (ver Pendências).

---

## CE-01 · máquina de estados por mensagem, idempotência e retry · **corrigido**

- **Arquivos:** `certidoes.py`, `testes/test_certidoes_estado.py`.
- **O que mudou:**
  - Máquina de estados POR MENSAGEM em `estado-certidoes.json`, gravada com
    `estado_store.gravar_atomico` **depois de cada mensagem** (e `processando` antes do
    trabalho): `pendente → processando → tarefa_criada | falha_transitoria(n, proxima_em) |
    falha_permanente` (+ `sem_negocio`/`ignorada` como estados finais de aviso/fora de
    escopo). Listas legadas (`msgs_processadas`, `tarefas`, `sem_negocio`) preservadas com
    os mesmos nomes; elas só recebem a mensagem **depois** do resultado confirmado.
  - **Só marca `tarefa_criada` depois de `tasks.task.get` confirmar** a tarefa recém-criada.
  - **Idempotência:** marcador `[certidao:<chat>:<id>]` na descrição da tarefa +
    `tasks.task.list` (filtro `UF_CRM_TASK=D_<deal>` ou `GROUP_ID`, paginado por
    `bitrix_cliente.listar`) antes de criar. Queda no meio da rodada não duplica tarefa.
  - **Retry só transitório:** falha classificada com `rede_bitrix.classificar` (via
    `FalhaBitrix` do `bitrix_cliente`); `erro.permanente` → `falha_permanente` com motivo e
    aviso ao projeto; transitório → `falha_transitoria` com `proxima_em` =
    `rede_bitrix.proxima_tentativa(n, erro, fila=True)` (instante epoch) e teto
    `MAX_TENTATIVAS_MSG=8` (alinhado a `POLITICA_FILA`). Escrita ambígua (timeout em
    `tasks.task.add`): a MESMA requisição não se repete (contrato); a mensagem é
    reprocessada com consulta de idempotência antes de recriar (nunca "às cegas").
  - `--simular` continua não gravando nada; flags CLI (`--status/--simular/--enviar/
    --chat/--chats/--teste/--legenda`) preservadas.
- **Testes** (`testes/test_certidoes_estado.py`): `test_ce01_falha_transitoria_NAO_marca_como_processada`,
  `test_ce01_erro_permanente_vira_falha_permanente_com_aviso`,
  `test_ce01_idempotencia_nao_duplica_tarefa`, `test_ce01_somente_marca_concluido_depois_de_confirmar`,
  `test_ce01_queda_no_meio_da_rodada_nao_duplica_tarefa`, `test_ce01_retry_agendado_so_depois_do_prazo`,
  `test_ce01_retry_esgotado_vira_permanente`, `test_ce01_sem_negocio_so_fica_marcado_depois_do_aviso`,
  `test_ce01_simular_nao_grava_estado`. Todos falham no código antigo (que marcava
  `msgs_processadas` para toda entrada e gravava só no fim).
- **Evidência:** `66 passed` (comando acima); os 9 testes CE-01 estão incluídos.
- **Divergências da auditoria:** nenhuma de substância. Detalhe extra registrado: o
  aviso de "sem negócio" agora também só é marcado depois de ENTREGUE (o CE-01 pedia
  "ok ou sem negócio avisado").

## CE-02 · foto de certidão ≠ PDF · **corrigido**

- **Arquivos:** `certidoes.py` (`formato_do_arquivo`, `analisar_certidao`, nome do upload),
  `testes/test_certidoes_identificacao.py`.
- **O que mudou:** tipo detectado pelos **BYTES** (magics: `%PDF`, JPEG, PNG, GIF, TIFF,
  WebP/RIFF); o temporário, o `mimeType` do node e o nome do upload usam a extensão certa
  (`certidao_<id>.jpg`, não `.pdf` fixo). Formato desconhecido não vai para a IA (aviso
  claro) e o upload usa `.bin` (nunca `.pdf` enganoso). Node recebe `mime` como argv.
- **Testes:** `test_ce02_foto_de_certidao_nao_e_pdf`, `test_ce02_foto_vai_para_a_ia_com_extensao_e_mime_certos`,
  `test_ce02_formato_desconhecido_nao_vai_para_a_ia`.
- **Evidência:** incluídos nos `66 passed`.
- **Divergências:** a auditoria sugeria "magic number" para decidir; o HEIC/HEIF também é
  aceito via `ftyp`? — **não**: HEIC não está na tabela de magics (o `tipos_documento`
  aceita `.heic` por extensão, mas sem magic o download do chat não traz nome); fica como
  melhoria registrada, foto comum (JPEG/PNG — o caso do grupo) resolvido.

## CE-03 · identificação sem "primeiro contato" pela criança · **corrigido**

- **Arquivos:** `certidoes.py` (`identificar`, `identificar_negocio`, `contatos_por_cpf`,
  `contatos_por_nome`, `achar_deal_por_contato`, `_nome_mae_da_ia`), testes.
- **O que mudou:**
  - Busca **paginada** (`bitrix_cliente.listar`, nunca só 50) e **unicidade exigida**:
    exatamente 1 contato; 0 ou >1 → NÃO cria tarefa, registra motivo e avisa o projeto
    (motivo específico: "N contatos com o nome…", "CPF em N contatos…", "telefone não
    confere…").
  - Nome vem da **mãe/responsável**: legenda (`Mãe:/Genitora:/Responsável:/Cliente:/Nome:`)
    ou IA (`nome_mae`/`genitora`/`responsavel`…). O `nome`/`nome_completo` da certidão é o
    do REGISTRADO (criança) e **nunca** vira âncora. Telefone da legenda funciona como 2ª
    âncora quando o contato tem telefone.
  - CPF só âncora com **DV válido** (`cpf_util.valido`); DV inválido cai para o caminho de
    nome (alinhado a ID-05).
  - Negócio: `filter[CLOSED]=N` + mais recente por `DATE_CREATE` (nunca "o primeiro").
  - Nome buscado com casamento **exato** `NAME+LAST_NAME` (o `%NAME` do Bitrix é LIKE no
  primeiro nome — homônimo parcial não casa).
- **Testes:** `test_ce03_ia_nunca_usa_nome_da_crianca`, `test_ce03_contato_ambiguo_nao_escolhe_o_primeiro`,
  `test_ce03_busca_paginada_casa_nome_completo`, `test_ce03_contato_unico_mas_telefone_nao_confere`,
  `test_ce03_negocio_aberto_mais_recente`, `test_identificar_cpf_com_dv_invalido_nao_vira_ancora`,
  `test_identificar_prefere_mae_responsavel`.
- **Divergências:** o audit diz "docstring promete nome da mãe" — confirmado e agora
  verdadeiro. O nome exato dos campos da IA (`nome` vs `nome_mae`) é hipótese do audit:
  aceitamos várias grafias; se a IA só devolver o nome do registrado, a mensagem cai em
  "sem negócio" com aviso (nunca busca pela criança). Confirmar no servidor.

## CE-04 · nascimento × casamento e paginação além de 50 · **corrigido**

- **Arquivos:** `certidoes.py` (`e_certidao_de_nascimento`, `ler_mensagens_chat`,
  `ler_certidoes_novas`), testes.
- **O que mudou:** no grupo, "nascimento" aceita; "certidão/registro civil" sem
  "nascimento" só aceita se **não** falar de casamento/óbito/casado. Histórico paginado
  com `?lastId=` (teto 50/página × 12 páginas, mesma curva de `triagem_grupo`), e mensagens
  já concluídas (máquina de estados + legado) são puladas por chave `chat:id`.
- **Testes:** `test_ce04_casamento_e_obito_nao_entram`, `test_ce04_historico_pagina_alem_de_50`,
  `test_ce04_grupo_descarta_certidao_de_casamento`.
- **Divergências:** nenhuma.

## CE-05 · temporários com PII, webhook no argv, exit code 0 · **corrigido**

- **Arquivos:** `certidoes.py`, `certidoes-rodar.sh`, testes.
- **O que mudou:** a certidão baixada e o JS do node são gravados com
  `seguranca.arquivo_temp_privado` e apagados com `seguranca.remover_seguro` no `finally`
  (mesmo com exceção). O `bx()` de `curl` sumiu (ver S12). `main()` devolve **1** quando a
  rodada tem `falha_permanente` (e 2 para estado corrompido/arquivo de teste ausente),
  para o systemd/monitor enxergarem falha; `certidoes-rodar.sh` propaga o exit real (OP-O4).
- **Testes:** `test_ce05_temporarios_sao_apagados_no_finally`,
  `test_ce05_temporarios_apagados_mesmo_quando_o_node_falha`, `test_ce01_*` (exit path do
  script está em OP-O4).
- **Divergências:** o audit cita `certidoes.py:101-104` (curl) — corrigido sob S12; o
  `grep remove|unlink` vazio do audit virou `remover_seguro` no `finally`.

## S09 (parte de `certidoes.py`) · JS do node em `/tmp` fixo · **corrigido**

- **Arquivos:** `certidoes.py` (`analisar_certidao`), testes.
- **O que mudou:** o JS é gravado em `seguranca.arquivo_temp_privado(sufixo=".js",
  prefixo="ler-certidao-")` — diretório privado `caminhos.BASE/tmp` (0700/0600), nome
  **único** por chamada (O_EXCL), nunca `/tmp/_ler_certidao.js` — e apagado no `finally`.
- **Testes:** `test_s09_js_em_arquivo_privado_unico_nao_tmp_fixo`,
  + `test_s12_fonte_sem_curl_nem_webhook_no_argv` (garante que o path fixo não voltou).
- **Evidência:** incluído nos `66 passed`.
- **Divergências:** o audit sugeria "JS versionado em arquivo fixo"; a exigência da frente
  pedia "arquivo privado de nome único" — foi essa a implementada (menor janela de corrida
  que arquivo versionado + temp).

## S12 · webhook no argv do `curl` · **corrigido**

- **Arquivos:** `certidoes.py`, `testes/test_certidoes_estado.py`,
  `testes/test_certidoes_identificacao.py`.
- **O que mudou:** `bx()` (curl com URL+chave no argv e `-d k=v` sem encode) substituído por
  `bitrix_cliente.chamar/listar` (POST JSON com timeout; a URL do webhook nunca aparece em
  argv nem em log — `seguranca.mascarar` em mensagens de erro). `im.message.add`,
  `crm.*`, `tasks.task.add/list/get` todos passam pelo cliente único. Os parâmetros vindo
  de cliente/IA (`filter[%NAME]`, `MESSAGE`) vão em corpo JSON — `&`, `=` e `+` não dividem
  mais o texto.
- **Testes:** `test_s12_aviso_com_e_comercial_chega_inteiro`, `test_s12_fonte_sem_curl_nem_webhook_no_argv`.
- **Divergências:** o audit comentava "se ficar com o curl, usar -K -" — não ficou: curl
  removido por completo.

## S13 · X-Api-Key para qualquer URL devolvida pela API · **corrigido**

- **Arquivos:** `certidoes.py` (`baixar_url_externa`), testes.
- **O que mudou:** o header `X-Api-Key` só é enviado quando
  `urlparse(url).hostname == "vibecode.bitrix24.com"`; URL de CDN/storage de terceiro é
  baixada **sem** chave. O restante das chamadas VibeCode (`vibe`) sempre mira o host
  autorizado.
- **Testes:** `test_s13_chave_soh_para_o_host_vibecode`, `test_s13_baixar_do_chat_nao_manda_chave_para_url_estranha`.
- **Divergências:** nenhuma; o host real da `downloadUrl` segue como hipótese do audit
  (precisa do servidor).

## PL-01 · linha livre sem conferir na hora da escrita · **corrigido**

- **Arquivos:** `pintar_linha.py` (`gravar`, `reler_linha_alvo`, `linha_ocupada_agora`,
  `conferir_escrita`), `testes/test_pintar_linha_planilha.py`.
- **O que mudou:** para cada linha nova, `B{n}:G{n}` é **relida na hora** e a escrita só
  acontece se a linha continuar livre; ocupada → nada é escrito, motivo registrado e o
  lote fica para a próxima rodada (nunca sobrescreve nome/CPF). Depois de gravar, relê e
  **confere** CPF (e nome); não confere → não entra em `pintadas.json` e sai alerta.
  Cor pintada só sobre linha confirmada. `salvar_json_atomico` passou a usar
  `estado_store.gravar_atomico` (contrato).
- **Testes:** `test_pl01_grava_na_linha_livre_e_confere`,
  `test_pl01_linha_ocupada_na_hora_NAO_sobrescreve`,
  `test_pl01_gravado_mas_nao_confere_nao_vai_para_pintadas`,
  `test_pl01_plano_de_linha_existente_so_pinta_sem_escrever_valores`,
  `test_pl02_linha_ocupada_na_corrida_fica_para_a_proxima_rodada` (corrida fim-a-fim).
- **Evidência:** incluídos nos `66 passed` (fake de Google Sheets em memória; sem rede).
- **Divergências:** o audit sugeria `values.append` com `INSERT_ROWS` como alternativa;
  implementada a outra alternativa permitida (reler + conferir + nunca sobrescrever), que
  preserva a regra da "linha livre DEPOIS da última usada" do escritório.

## PL-02 · leitura da planilha inteira a cada 2 min · **corrigido**

- **Arquivos:** `pintar_linha.py` (`main`, `meta_aba`), testes.
- **O que mudou:** lotes já em `pintadas.json` são filtrados **antes** de `abrir()` — sem
  lote novo, nenhuma chamada ao Google. E `meta_aba` passa a medir a última linha em
  `B:G` (a coluna A é pré-numerada até o fim da grade e fazia `ultima` ≈ 10 mil).
- **Testes:** `test_pl02_sem_lote_novo_NAO_abre_a_planilha` (o `abrir()` antigo era chamado
  e o teste falha), `test_pl02_meta_aba_medem_b_g_nao_a_coluna_a_pre_numerada`,
  `test_pl02_fluxo_com_lote_novo_cria_linha_e_registra`.
- **Evidência:** incluídos nos `66 passed`.

## PL-03 · CPF errado cria linha para sempre · **corrigido**

- **Arquivos:** `pintar_linha.py` (`decidir`, `planejar`), `testes/test_pintar_linha.py`
  (fixtures com CPF sintético válido), `testes/test_pintar_linha_pl03.py`.
- **O que mudou:** linha NOVA só é criada com identidade **conclusa**: `cpf_util.valido(cpf)`
  (DV) e sem indício de inconclusividade — `cpf_origem == "bitrix-nome"` (CPF de homônimo)
  ou mais de 1 CPF em `cpfs_distintos` no lote → **não cria linha** e registra o motivo.
  Quem já tem linha continua sendo pintado (a trava é só para criação).
- **Testes:** `test_pl03_cpf_com_dv_invalido_NAO_cria_linha`,
  `test_pl03_cpf_de_homonimo_bitrix_nome_NAO_cria_linha`,
  `test_pl03_lote_com_varios_cpfs_NAO_cria_linha`,
  `test_pl03_identidade_conclusa_cria_linha`,
  `test_pl03_cliente_que_JA_tem_linha_continua_sendo_pintado`,
  `test_pl03_planejar_repassa_a_origem_do_lote`.
- **Divergências:** a auditoria menciona ainda "a marcação por CPF é para sempre"
  (`pintadas` sem expiração/repintura por lote novo) — **fora** da exigência da frente;
  mantido como está e registrado nas Pendências. Comportamento mudado: CPF de DV inválido
  que antes gerava linha agora é ignorado com motivo (exigência).

## OP-O4 (parte: `certidoes-rodar.sh`) · sempre saía 0 · **corrigido**

- **Arquivos:** `certidoes-rodar.sh`, `testes/test_certidoes_rodar.py`.
- **O que mudou:** `set -euo pipefail`; `PYTHON` roda com `|| code=$?` e o script termina
  com `exit "$code"` (código REAL do `certidoes.py` chega ao systemd); trava ocupada
  continua saindo 0 com aviso. `TRIAGEM_BASE`/`TRIAGEM_LOCK`/`PYTHON` sobrescrevíveis por
  ambiente (para teste; em produção os padrões `/opt/...` valem).
- **Testes:** `test_op_o4_exit_code_real_propaga_ao_systemd` (stub python exit 7 → script
  exit 7; no código antigo o `echo` final engolia e saía 0),
  `test_op_o4_sucesso_continua_saindo_0`, `test_op_o4_trava_ocupada_sai_0_com_aviso`,
  `test_op_o4_script_tem_pipefail_e_exit_real`.
- **Evidência:** `10 passed in 0.93s` (`pytest -q testes/test_certidoes_rodar.py
  testes/test_pintar_linha_pl03.py -v`).

---

## Pendências para o orquestrador

1. **`backup_pintura.py` e `conferir_pintura.py`** (meus arquivos, mas fora dos meus
   achados): scripts avulsos com caminhos/datas fixos, sem `main()`, que imprimem conteúdo
   da planilha (nome/CPF) — o audit AUX-01 propõe movê-los para `backups/scripts-usados/`.
   Não mexi; decisão do orquestrador.
2. **`pintadas.json` sem expiração** (parte do PL-03 que a auditoria cita: cliente que
   volta meses depois não é repintado): fora da exigência desta frente; se quiserem,
   `pintadas[cpf]` já guarda `lote` e `em` para implementar repintura por janela.
3. **Comportamento novo do exit code** de `certidoes.py --enviar`: 1 quando a rodada tem
   `falha_permanente` (faz a unit ir a `failed` e disparar `OnFailure=`): aprovar/ajustar
   com a frente H antes do deploy.
4. **Criação de tarefa migrada** de `POST /v1/tasks` (VibeCode) para `tasks.task.add`
   (REST via `bitrix_cliente`, conforme instrução da retomada): conferir no servidor os
   campos `UF_CRM_TASK`/`UF_TASK_WEBDAV_FILES`/`DEADLINE` e o formato da resposta
   (`result.task`).
5. `config.json` não tocado (dono do orquestrador); nada a mudar nele por esta frente.

## Pontos `COMPAT-ONDA1`

Nenhum. A onda 1 já estava integrada quando o trabalho foi retomado; todos os imports
(`seguranca`, `cpf_util`, `rede_bitrix`, `estado_store`, `bitrix_cliente`, `tipos_documento`)
são diretos.

## Precisa do servidor

- Campos reais devolvidos pela IA do painel (`ia.classificarDocumento`): `nome` vs
  `nome_mae` (CE-03). Se a IA só devolver o nome do registrado, hoje a mensagem cai em
  "sem negócio" com aviso — correto por segurança, mas talvez precise estender a IA.
- Comportamento de `GET /v1/chats/{id}/messages` com `?lastId=` (paginação CE-04) — alinhado
  com `triagem_grupo.buscar_mensagens_api`, medido 21/09/26.
- Formato de `tasks.task.list` do portal (`result.tasks` vs `result.items`) —
  `bitrix_cliente.listar` já trata ambos.
- Host real da `downloadUrl` da VibeCode (S13 — hipótese do audit).
- HEIC: confirmar se chega foto de iPhone no grupo (hoje HEIC sem magic conhecido não é
  lido pela IA; extensão `.heic` do `tipos_documento` é aceita quando há nome de arquivo).

## Riscos

- `tasks.task.add` pode exigir campos customizados obrigatórios no portal (a doc avisa);
  em caso de `INVALIDO` a mensagem vira `falha_permanente` com aviso — não some em silêncio.
- `tarefa_existente` busca o marcador só em tarefas dos últimos 90 dias (`>=CREATED_DATE`);
  cobre o histórico lido (600 mensagens/ciclo), mas uma tarefa muito antiga não seria
  reaproveitada.
- `linha_livre` agora nunca devolve a linha 1 (cabeçalho) — borda encontrada pelos testes
  (com a planilha só de cabeçalho, o código antigo escreveria por cima do cabeçalho).
- PL-02: se `pintadas.json` for perdido, os lotes são reprocessados (idempotente: a linha é
  achada pelo CPF e só pintada).
- O `certidoes-rodar.sh` agora falha (exit != 0) com o `python` ausente/errado — é o
  desejado (systemd vê), mas as units devem ter `OnFailure=` configurado (frente H).

## Sugestão de agrupamento de commits

1. `certidoes: CE-01 máquina de estados por mensagem, idempotência e retry só transitório`
   (`certidoes.py` + `testes/test_certidoes_estado.py`)
2. `certidoes: CE-02/CE-04 foto×PDF, filtro nascimento×casamento e paginação além de 50`
3. `certidoes: CE-03 unicidade de contato/negócio e nome da mãe como âncora`
   (inclui os testes de `testes/test_certidoes_identificacao.py`)
4. `certidoes: S09/S12/S13/CE-05 JS privado, webhook fora do argv, X-Api-Key restrito e temporários apagados`
5. `certidoes-rodar.sh: OP-O4 set -euo pipefail e propagação do exit code real`
   (`certidoes-rodar.sh` + `testes/test_certidoes_rodar.py`)
6. `pintar_linha: PL-01 reler a linha alvo na hora e conferir a gravação`
7. `pintar_linha: PL-02 não reler a planilha sem lote novo`
8. `pintar_linha: PL-03 CPF com DV válido e identidade conclusa para criar linha`
   (inclui `testes/test_pintar_linha_planilha.py`, `testes/test_pintar_linha_pl03.py` e o
   ajuste de fixtures de `testes/test_pintar_linha.py`)
9. `docs: relatório da Frente X (certidões e planilha)`
