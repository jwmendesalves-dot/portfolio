# Frente C2b (onda 3) — Fase 1: identidade, CPF com DV, config visível, exceções, PII

Status: **CONCLUÍDO COM RESSALVAS**. F1-05 e F1-09 foram corrigidos. F1-06, F1-07 e F1-13 ficaram parciais: o que falta depende de arquivos de outros donos ou de medição no servidor (ver Pendências). A PII do autoteste foi trocada e o teste de integração frágil foi refeito.

Código: commit `a927112` (comitado pelo orquestrador). Base anterior: `4440431`. O `triagem_grupo.py` é idêntico em `4440431` e em `a927112~1` (`git diff --quiet` = sem diferença), então a prova de "falha antes" vale para os dois.

O subagente que implementou a frente foi interrompido antes de escrever este relatório. Ele foi escrito depois, a partir do diff de `a927112` e do transcript da implementação. Os testes, a prova contra o código antigo, a suíte e o pyflakes foram **rodados de novo** para este relatório (saídas abaixo).

## Arquivos (commit a927112)
- `triagem_grupo.py` (alterado)
- `testes/test_fase1_identidade.py` (novo, 9 testes)
- `testes/test_fase1_config.py` (novo, 11 testes contando os parametrizados)
- `testes/test_circuit_breaker_integracao.py` (reescrito com `monkeypatch`)
- `testes/test_telegram_grupo.py`, `testes/test_traco_delimitador.py`, `testes/test_cabecalho_encaminhado.py` (CPFs de teste trocados por CPFs com DV válido gerados por `cpf_util.gerar_para_teste`; nomes e locais reais trocados por fictícios)

---

## F1-05 — Barreira de identidade disparava com "nome" tirado de conversa · **CORRIGIDO**
O que mudou (`triagem_grupo.py`):
- `_CONVERSA_INICIO` / `_CONVERSA_QUALQUER` + `_e_frase_conversa()` (novas): saudações e frases de serviço ("bom dia", "Segue documentos", "Boa tarde, segue"…) nunca são nome.
- `_e_nome_valido()`: rejeita o candidato quando `_e_frase_conversa()` dá True, antes do `validar_nome`.
- `extrair_nomes_com_papel()`, regra 4 (1ª linha solta): só vale quando a mensagem é cabeçalho, ou seja, tem CPF (`RX_CPF`) ou uma linha começando com `=`.
- `_muda_identidade()` e o corte de `agrupar_por_janela()`: `nomes_lote` vem de `lote["nomes_ident"]` (evidência: ficha, cabeçalho ou mensagem com CPF), nunca de `lote["nomes"]` cru.
- `agrupar()`, bloco `traco_no_fim_fecha` (montagem de `nomes_bloco` para `m_pend`/`m_ant`): só soma nomes de mensagem com `e_identificador`.
- `_mesmo_nome()` (nova), usada em `_e_outra_pessoa()` e `_intruso_no_lote()`: um nome é parcial do outro por substring ou por conjunto de palavras, ignorando partículas (DA/DE/DO/DAS/DOS/E). "JOANA LIMA" e "JOANA TESTE LIMA" contam como a mesma pessoa, então o cabeçalho completo do próprio cliente não corta mais o kit.

Testes (`testes/test_fase1_identidade.py`): `test_saudacao_e_frase_de_servico_nao_sao_nome`, `test_primeira_linha_solta_so_vira_nome_em_cabecalho`, `test_cabecalho_do_proprio_cliente_nao_corta_o_kit`, `test_nome_da_ficha_e_cabecalho_completo_sao_a_mesma_pessoa`, e a guarda `test_barreira_ainda_corta_outro_cliente_identificado` (outro cliente identificado com CPF próprio continua sendo cortado).

Evidência no código antigo (seção "Prova" abaixo): os 4 primeiros falham, com as mensagens:
- `assert ['bom dia'] == []`
- `assert ['Maria Ficticia Souza'] == []` (1ª linha solta sem cabeçalho virava nome)
- `bloco interrompido por outra pessoa (nome 'JOANA TESTE LIMA' != 'BOM DIA' do bloco)`: é exatamente o caso da auditoria
- `(nome 'JOANA TESTE LIMA' != 'JOANA LIMA' do bloco)` → `assert 2 == 1`

A guarda passa no código antigo e no novo: a barreira legítima não foi afrouxada.

Diferença em relação à auditoria: a correção (3) sugerida, de pôr as saudações em `nome_regras.PALAVRAS_NAO_NOME`, **não** foi feita, porque `nome_regras.py` não é desta frente. O filtro ficou na camada de sinal de `triagem_grupo.py` (ver Pendências).

## F1-06 — `fonte_leitura="webhook"` sem failover, sem breaker, ignorando `historico_paginas` · **PARCIAL**
O que mudou (`triagem_grupo.py`):
- `ciclo()`: no modo webhook, a 1ª leitura e a releitura (sem cursor) usam `max_paginas=paginas`, com `paginas = --paginas` ou `historico_paginas` do config. Antes eram sempre 40 páginas (2.000 mensagens por chat). Com cursor, segue `first_id=cursor`, como no C2a.
- `ciclo()`: o rótulo `resumo["fonte"]` deixou de dizer `[DIAGNÓSTICO]`. Agora diz `[config fonte_leitura=webhook]` ou `[--fonte-webhook]`.
- `_ler_mensagens_com_breaker()`: o failover API→webhook sem cursor também respeita `historico_paginas`.
- `validar_config()` (nova): `fonte_leitura` fora de `api`/`webhook` ou `historico_paginas` não inteiro positivo levantam `ConfigInvalida`.

Testes: `test_fase1_config.py::test_webhook_primeira_leitura_respeita_historico_paginas`, `test_circuit_breaker_integracao.py::test_failover_webhook_respeita_historico_paginas_e_first_id`.

Evidência no código antigo: os dois falham com `{'max_paginas': 40} != {'max_paginas': 2}`.

Não feito (fora do escopo ou dono):
- Failover webhook→API (rotear o modo webhook por `_ler_mensagens_com_breaker`).
- Separar `_registrar_bloqueio_leitura()` do disjuntor de download.
- Fazer o bot (`pergunta_traco.py:419`) ler pela mesma fonte da esteira.
- Rodar `--testar-fonte` no servidor.

## F1-07 — `modo_traco` divergente e config inválido caindo em silêncio nos padrões · **PARCIAL** (o risco 1 foi corrigido)
O que mudou (`triagem_grupo.py`):
- `ConfigInvalida` (nova exceção) e `carregar_config()`: JSON inválido, arquivo ilegível (`OSError`/`UnicodeDecodeError`), raiz que não é objeto ou valor desconhecido de `fonte_leitura`/`modo_traco`/`historico_paginas` → `log.error` + `raise ConfigInvalida`. Antes: `except Exception` → `CONFIG_PADRAO` (`grupo_autor` + `api`).
- `main()`: com `ConfigInvalida`, imprime `🚨 triagem NÃO rodou: …` no stdout (canal de alerta do Hermes) e devolve **3**, sem rodar o ciclo.
- `MODOS_TRACO`: incluídos `delimitador` e `delimitador_autor`.
- `_modo_traco_explicito()` (nova) e `modo_traco()`: um valor explícito e desconhecido levanta `ConfigInvalida`. Antes caía no toggle legado `autor`/`grupo` em silêncio.

Testes (`testes/test_fase1_config.py`): `test_config_valida_de_producao_carrega`, `test_config_invalida_levanta_em_vez_de_cair_nos_padroes` (5 casos: JSON quebrado, lista, fonte desconhecida, modo desconhecido, páginas inválidas), `test_config_ilegivel_levanta`, `test_modo_traco_desconhecido_nao_vira_toggle_legado`, `test_main_sai_com_codigo_diferente_de_zero_e_alerta`.

Evidência no código antigo: todos falham. Os motivos:
- `AttributeError: module 'triagem_grupo' has no attribute 'ConfigInvalida'`
- `'delimitador' in MODOS_TRACO` falso
- `main(['--dry-run'])` devolveu 2 em vez de 3, porque o log mostra `config.json inválido (...); usando padrões` e o ciclo rodou.

Não feito:
- `CONFIG_PADRAO["modo_traco"]` continua `grupo_autor` e `fonte_leitura` continua `api` (linhas 646/660). Com o config inválido abortando, o padrão só vale quando `config.json` não existe.
- O `_comentario` do `config.json` não foi tocado (o arquivo não é desta frente).
- Risco 2 (paridade do `traco_no_fim_fecha`): não mexido aqui. O C2a persiste o bloco aberto entre ciclos, o que ataca a causa (ver `C2a-fase1-cursor.md`).

## F1-09 — CPF com pontuação aceito sem DV · **CORRIGIDO**
O que mudou (`triagem_grupo.py`):
- `cpf_normalizar()` / `cpf_plausivel()`: viraram cascas sobre `cpf_util.normalizar` / `cpf_util.valido`, a fonte única de CONTRATOS. O DV local duplicado foi removido.
- `extrair_cpfs_com_papel(texto, invalidos_out=None)`: o DV agora é **obrigatório com ou sem pontuação**. Antes, `rotulado_cpf or tem_sep` já bastava. Número rotulado ou pontuado com DV errado vai para `invalidos_out`.
- `extrair_cpfs_invalidos()` (nova).
- `classificar_mensagem()`: grava `msg["cpfs_invalidos"]`. `_novo_lote()`, `_acumular()` e `_reacumular()` carregam `lote["cpfs_invalidos"]`.
- `agrupar()`: CPF inválido não entra em `cpfs`, `lote.cpf`, barreira nem CONFLITO. Se houver algum, o lote ganha o motivo `CPF_INVALIDO: N número(s) … conferir` (só a contagem, sem o número), fica com `pronto=False` e `requer_revisao=True`.
- `serializar_lote()`: novo campo `cpfs_invalidos` no estado.
- Autoteste `testar_cpf()`: o caso `"CPF 000.000.000-00"` agora espera `[]`.

Testes (`testes/test_fase1_identidade.py`): `test_cpf_com_pontuacao_e_dv_errado_nao_e_cpf`, `test_cpf_errado_mais_cpf_certo_nao_vira_conflito_mas_vai_para_revisao`, `test_lote_so_com_cpf_invalido_fica_sem_cpf_em_revisao`, e a guarda `test_cpf_normalizar_e_plausivel_usam_dv`.

Evidência no código antigo: os 3 primeiros falham:
- `assert ['<11 dígitos sintéticos>'] == []`
- `KeyError: 'cpfs_invalidos'`
- `assert ('<cpf sintético>' is None)`

A guarda passa nas duas versões.

Diferença em relação à auditoria: a auditoria sugeria `identificacao="CPF_INVALIDO"`. A implementação usa o **motivo** `CPF_INVALIDO` + `requer_revisao` e mantém os valores de `identificacao` já gravados no estado, sem criar status novo (REGRAS: "mapeie, não renomeie").

## F1-13 — Exceções engolidas · **PARCIAL** (as exceções de `triagem_grupo.py` foram tratadas; `agrupar` não foi dividido)
O que mudou (`triagem_grupo.py`):
- `_registrar_bloqueio_download()`: `except: pass` → `log.error("não consegui abrir o disjuntor bitrix-download …")`, com a mensagem passada por `seguranca.mascarar`.
- `http_json()` e `_baixar_atomico()`: corpo de erro HTTP ilegível → `log.warning(... classificando só pelo status)`. Antes, `pass` perdia o marcador LOOP_DETECTED/QUERY_LIMIT sem rastro.
- `_meta_do_disco()`: `OSError` → `log.warning(... vai baixar de novo)`.
- `ciclo()`: alerta de LACUNA que não sai → `log.error`. Antes era `pass`.
- `carregar_config()`: ver F1-07.
- O `from nome_regras import PALAVRAS_NAO_NOME` (reexport) virou `import nome_regras as _nome_regras; PALAVRAS_NAO_NOME = ...`, para o pyflakes ficar limpo sem `noqa`.

Teste: `test_fase1_config.py::test_disjuntor_que_nao_abre_fica_registrado`. No código antigo falha (`assert False`: nenhum registro de log).

Não feito:
- `carregar_estado` (F1-08, de outra frente/C1).
- `completar_cpf_por_nome.nome_do_lote` (arquivo de outro dono).
- Dividir `agrupar` (cerca de 480 linhas) e mover `testar_sinais` para `testes/`: refatoração grande, não pedida nesta onda.

## PII do autoteste (tabela Z-integracao) · **CORRIGIDO**
- `triagem_grupo.py`: 12 CPFs reais do `testar_cpf`/`testar_sinais` e de comentários/docstrings (`e_sinal_traco`, `_sem_cpf_na_linha`, `_acumular`, cabeçalho de `CONFIG_PADRAO`, `RX_CTX_NAO_CPF`) foram trocados por CPFs gerados com `cpf_util.gerar_para_teste` (DV válido). Telefones reais foram trocados por números fictícios (o DV dos telefones continua inválido, preservando o caso de regressão de 21/09). Nenhum valor real foi copiado para este relatório.
- `testes/test_traco_delimitador.py`, `testes/test_cabecalho_encaminhado.py`: nomes de clientes, operador, cidade e `lote_id` reais foram trocados por fictícios ou omitidos.
- `testes/test_telegram_grupo.py`, `testes/test_traco_delimitador.py`, `testes/test_cabecalho_encaminhado.py`: os CPFs "inventados" sem DV (ex.: `111.222.333-44`) pararam de ser CPF com o F1-09. Foram trocados por CPFs sintéticos com DV válido. As 4 falhas iniciais eram `test_dois_chats_nao_interferem_um_no_outro`, `test_janela_por_chat_fecha_quando_a_pessoa_manda_start_de_novo`, `test_modo_delimitador_nao_mistura_os_dois_clientes` e `test_delimitador_sem_material_nao_cria_lote_vazio`. A asserção dos testes não mudou, só os dados.

Evidência: os testes ajustados rodados **contra o código antigo** (`4440431:triagem_grupo.py`) dão `1 failed, 22 passed`. A única falha é `test_failover_webhook_respeita_historico_paginas_e_first_id` (F1-06, esperada). A troca de dados não depende do código novo.

Autoteste embutido, rodado no código novo:
```
$ python triagem_grupo.py --testar-cpf      ->  casos=14 falhas=0
$ python triagem_grupo.py --testar-sinais   ->  casos=89 falhas=2
[FALHA] barreira: documento de outra pessoa NÃO entra no bloco do 1º (nada de lote com 2 CPFs)
[FALHA] barreira: o lote do 2º fecha pelo traço dele
```
As mesmas 2 falhas de `--testar-sinais` acontecem no código antigo (`4440431`): `casos=89 falhas=2`, idênticas. São **anteriores ao C2b** e não foram introduzidas por ele. Ver Pendências.

## Teste frágil de integração (`test_circuit_breaker_integracao.py`) · **CORRIGIDO**
- Antes: atribuía `tg.ARQ_BREAKER = ...` e `tg.buscar_mensagens = fake` direto no módulo, sem restaurar, e vazava para os testes seguintes (dependia da ordem). Os fakes tinham a assinatura antiga (`fake_webhook(w, dialog, cfg=None)`) e quebraram com o `first_id`/`max_paginas`/`info` do C2a (`test_redireciona_para_webhook_apos_n_falhas_da_api` falhava).
- Agora: fixture `arq` com `tmp_path` + `monkeypatch.setattr`. Os fakes aceitam a assinatura atual. Há o novo `test_nada_vaza_para_o_modulo_depois_dos_testes` e o `test_failover_webhook_respeita_historico_paginas_e_first_id` (F1-06). São 6 testes.

---

## Prova contra o código antigo (refeita para este relatório)
```
R=~/.hermes/cache/scratch/c2b/red_rel
git archive a927112 | tar -x -C $R           # testes novos
git show 4440431:triagem_grupo.py > $R/triagem_grupo.py   # código antes do C2b
cd $R && .venv/bin/python -m pytest -q testes/test_fase1_identidade.py testes/test_fase1_config.py
...
FAILED testes/test_fase1_identidade.py::test_saudacao_e_frase_de_servico_nao_sao_nome
FAILED testes/test_fase1_identidade.py::test_primeira_linha_solta_so_vira_nome_em_cabecalho
FAILED testes/test_fase1_identidade.py::test_cabecalho_do_proprio_cliente_nao_corta_o_kit
FAILED testes/test_fase1_identidade.py::test_nome_da_ficha_e_cabecalho_completo_sao_a_mesma_pessoa
FAILED testes/test_fase1_identidade.py::test_cpf_com_pontuacao_e_dv_errado_nao_e_cpf
FAILED testes/test_fase1_identidade.py::test_cpf_errado_mais_cpf_certo_nao_vira_conflito_mas_vai_para_revisao
FAILED testes/test_fase1_identidade.py::test_lote_so_com_cpf_invalido_fica_sem_cpf_em_revisao
FAILED testes/test_fase1_config.py::test_config_valida_de_producao_carrega - ...
FAILED testes/test_fase1_config.py::test_config_invalida_levanta_em_vez_de_cair_nos_padroes[{"modo_traco": "delimitador", ]
FAILED testes/test_fase1_config.py::test_config_invalida_levanta_em_vez_de_cair_nos_padroes[[1, 2]]
FAILED testes/test_fase1_config.py::test_config_invalida_levanta_em_vez_de_cair_nos_padroes[conteudo2]
FAILED testes/test_fase1_config.py::test_config_invalida_levanta_em_vez_de_cair_nos_padroes[conteudo3]
FAILED testes/test_fase1_config.py::test_config_invalida_levanta_em_vez_de_cair_nos_padroes[conteudo4]
FAILED testes/test_fase1_config.py::test_config_ilegivel_levanta - AttributeE...
FAILED testes/test_fase1_config.py::test_modo_traco_desconhecido_nao_vira_toggle_legado
FAILED testes/test_fase1_config.py::test_main_sai_com_codigo_diferente_de_zero_e_alerta
FAILED testes/test_fase1_config.py::test_webhook_primeira_leitura_respeita_historico_paginas
FAILED testes/test_fase1_config.py::test_disjuntor_que_nao_abre_fica_registrado
18 failed, 2 passed in 0.22s
```
Resumo: **18 de 20 falham no código antigo**. Os motivos estão por achado acima. Os 2 que passam são guardas de não-regressão, de propósito: `test_barreira_ainda_corta_outro_cliente_identificado` e `test_cpf_normalizar_e_plausivel_usam_dv`. No código novo: 20/20 passam.

## Suíte e pyflakes (código novo, árvore do repo)
```
$ .venv/bin/python -m pytest -q
........................................................................ [100%]
1078 passed, 2 skipped in 14.73s

$ .venv/bin/python -m pyflakes triagem_grupo.py
(sem saída)  pyflakes exit=0
```
Nota: em duas execuções anteriores, sob carga (a suíte levou 75 s), `testes/test_rede_bitrix.py` falhou de forma intermitente, cada vez num teste diferente (`test_trava_ocupada_respeita_timeout`, `test_limitador_compartilhado_entre_processos_respeita_taxa_e_rajada`). São testes de tempo, não tocados pelo C2b. Numa árvore limpa de `a927112`, `test_rede_bitrix.py` dá `72 passed`. Flakiness de outra frente: listada, não consertada.

---

## Mudanças de comportamento para o deploy
1. **`config.json` inválido agora para tudo.** JSON quebrado, ilegível, raiz não-objeto, `fonte_leitura` desconhecida, `modo_traco` desconhecido ou `historico_paginas` não positivo fazem `triagem_grupo.py` sair com **código 3** e imprimir `🚨 triagem NÃO rodou: …`, sem ciclo. Antes rodava com `grupo_autor` + `api`. No modo `--loop`, o config é lido no início (aborta) e de novo em cada `ciclo()`. Se ficar inválido no meio do loop, cada ciclo falha, gera `alertar_falha` e o loop segue tentando.
2. **Outros scripts que chamam `tg.carregar_config()` também passam a levantar `ConfigInvalida`** com config inválido: `pergunta_traco.py:1180`, `telegram_grupo.py:415`, `checar_midias.py:98`. Antes recebiam os padrões em silêncio. É desejável, mas o alerta/saída desses scripts não foi ajustado nesta frente.
3. **CPF sem DV válido não é mais CPF**, mesmo pontuado ou rotulado "CPF". Ele não vira âncora, `lote.cpf`, barreira, CONFLITO nem chave de CRM. Fica em `lote["cpfs_invalidos"]` (campo novo no `estado.json`), com motivo `CPF_INVALIDO` e `requer_revisao=True`, `pronto=False`. Efeito esperado: menos CONFLITO e barreira falsos, e alguns lotes que antes iam com o CPF errado agora param em revisão.
4. **Saudação ou frase de serviço não é mais nome**, e a 1ª linha solta só vira nome em cabeçalho (CPF ou linha `=`). Esperado: menos `FECHADO_POR_TROCA_DE_PESSOA`/REVISAO por "BOM DIA". Lotes que tinham `nome="bom dia"` passam a ficar sem nome.
5. **Nome parcial por palavras** ("JOANA LIMA" e "JOANA TESTE LIMA") é tratado como a mesma pessoa na barreira e em `_intruso_no_lote`. Risco residual: dois clientes distintos em que um nome é subconjunto do outro sem CPF diferente não serão separados por nome. Com CPFs distintos, o corte continua.
6. **Webhook como fonte:** a 1ª leitura/releitura agora lê `historico_paginas` páginas, não 40 fixas. Se o config de produção tiver `historico_paginas` baixo, a releitura sem cursor cobre menos histórico. Conferir o valor antes do deploy.
7. Logs novos (`error`/`warning`) onde antes havia `pass`: disjuntor que não abriu, corpo HTTP ilegível, arquivo em disco ilegível, alerta de lacuna que não saiu.

## Pendências para outras frentes
- **`nome_regras.py`** (dono de nome_regras): incorporar saudações e frases de serviço em `PALAVRAS_NAO_NOME` e depois remover `_CONVERSA_*` de `triagem_grupo.py`.
- **`pergunta_traco.py`**: ler `fonte_leitura` do config (mesma fonte da esteira, F1-06) e tratar `ConfigInvalida` com alerta.
- **`telegram_grupo.py` / `checar_midias.py`**: tratar `ConfigInvalida` (hoje vira traceback).
- **`config.json`** (orquestrador): atualizar o `_comentario` para `delimitador` e conferir `historico_paginas`.
- **`triagem_grupo.py` (próxima onda)**: failover webhook→API via `_ler_mensagens_com_breaker`; `_registrar_bloqueio_leitura()` separado do disjuntor de download; alinhar `CONFIG_PADRAO` com produção; dividir `agrupar`; mover `testar_sinais` para `testes/`.
- **Autoteste `--testar-sinais`**: há 2 falhas anteriores ao C2b ("barreira: documento de outra pessoa NÃO entra no bloco do 1º" e "o lote do 2º fecha pelo traço dele"), idênticas em `4440431`. É preciso decidir se o autoteste está desatualizado em relação ao C1/C2a ou se é regressão deles.
- **`testes/test_rede_bitrix.py`**: testes de tempo intermitentes sob carga.

## O que precisa do servidor
- `triagem_grupo.py --testar-fonte` para comparar API × webhook antes de decidir a fonte (F1-06).
- Validar o `config.json` real com `python3 -c "import triagem_grupo as t; t.carregar_config()"` **antes** do deploy. Um config que hoje "funciona" com chave desconhecida passaria a parar o serviço.
- Acompanhar, depois do deploy, a contagem de motivo `CPF_INVALIDO` e a queda de CONFLITO/`FECHADO_POR_TROCA_DE_PESSOA`.
