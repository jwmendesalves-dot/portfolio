# Revisão independente da onda 1 — Revisor OpenBot

- **Escopo:** commits `3e95fca..3a85672` (`git log --oneline 3e95fca..3a85672` → 7 commits: `c3989a3` B, `9e79ba7` S, `8373bd5` R, `fdcd55e` PT, `2826fab`/`cf3895e` docs, `803a047`/`3a85672` onda1.5).
- **Leitura prévia:** `docs/remediacao/REGRAS.md`, `docs/remediacao/CONTRATOS.md` (inclusive as notas pós-onda 1), relatórios `docs/remediacao/frentes/{B,S,R,PT}-*.md`, `docs/auditoria/00-RESUMO-EXECUTIVO.md`.
- **Método:** revisão estática dos 11 módulos + testes, varreduras de padrão (segredos hardcoded, `except` engolidor, `from __future__ import annotations`, `match`, caminhos `/opt`/`/tmp` literais), conferência das assinaturas prometidas no CONTRATOS × código, e execução da suíte (Python 3.9.6 da `.venv`).
- **Restrições respeitadas:** nenhum arquivo de código editado; sem rede; sem commit; sem tocar em `triagem-bitrix-remediacao`/`triagem-bitrix-mimo`.
- **Arquivo criado:** apenas este relatório.

## Baseline da suíte (real, antes de qualquer edição nesta revisão)

```
$ .venv/bin/python -m pytest -q
14 failed, 728 passed in 7.54s
```

As 14 falhas são as listadas no brief (dependem de dados de produção) e nenhuma pertence aos módulos da onda 1: `test_variaveis_contrato` (7), `test_servico_lote` (3), `test_nome_regras`, `test_pasta_fichinha`, `test_ponte_fila`, `test_telefones_fichinha`.

## pyflakes (real)

```
$ .venv/bin/python -m pyflakes estado_store.py estado_lote.py cpf_util.py seguranca.py rede_bitrix.py circuit_breaker.py bitrix_cliente.py tipos_documento.py pergunta_traco.py vigia-bot-triagem.py esperar_bot_liberar.py
(silha vazia — exit 0)
$ .venv/bin/python -m pyflakes --version
3.4.0 Python 3.9.6 on Darwin
```

## Varreduras globais (real)

- `from __future__ import annotations`: presente nos 11 módulos.
- `match`/`case`: nenhuma ocorrência (compatível com 3.9).
- Caminhos literais `/opt/` ou `/tmp/` nos 11 módulos: nenhuma ocorrência.
- URL/token em texto dos módulos: só em docstrings, constantes de validação (`seguranca.py:248`, regex) e chamadas legítimas com variável (`X-Api-Key: chave` em `pergunta_traco.py:265`, `esperar_bot_liberar.py:105`; header em `vigia-bot-triagem.py:124`) — nenhum segredo literal.

## Achados

Formato: achado · severidade · arquivo:linha · evidência (comando + saída real) · correção sugerida.

### A1 · média · `bitrix_cliente.py:128` — paginação para cedo e perde itens quando a página do servidor é menor que 50
- **Achado:** `listar()` encerra assim que `len(pagina) < 50`, assumindo página de 50. Num método que devolva menos de 50 itens por página, só a primeira página é lida — perda silenciosa de dados, sem erro. O teste existente (`test_listar_pagina_tudo_por_id`) cobre só página cheia (50) + última (10), por isso passa.
- **Evidência (comando + saída real):**
```
$ TRIAGEM_WEBHOOK_BITRIX='https://exemplo.bitrix24.com.br/rest/1/fake/' .venv/bin/python - <<'PY'
  # transporte devolvendo 2 páginas de 20 itens; chamada: bitrix_cliente.listar("crm.contact.list", {}, _transporte=transporte)
itens devolvidos: 20 | esperados com 2 paginas de 20: 40
ultima pagina tinha 20 < 50 -> parou antes de pedir a 2a pagina
```
- **Correção sugerida:** parar só por página vazia ou `novo == ultimo` (já tratados) e remover o corte em `< 50`; ou aceitar `tamanho_pagina` como parâmetro. Acrescentar teste com página de 20 no meio.

### A2 · média · `docs/remediacao/CONTRATOS.md:8` — nota pós-onda 1 promete `ValueError` em `cpf_util.mascarar`, mas o código devolve `"***"`
- **Achado:** a nota diz "`cpf_util.mascarar`, `cpf_util.formatar` levanta `ValueError` sem 11 dígitos". Só `formatar` levanta; `mascarar` devolve `"***"` para valor não normalizável (comportamento bom, mas o contrato está errado). Consumidor que espere a exceção para detectar dado inválido não a recebe.
- **Evidência (comando + saída real):**
```
$ grep -n 'cpf_util.mascarar' docs/remediacao/CONTRATOS.md
8:> - `cpf_util.mascarar`, `cpf_util.formatar` levanta `ValueError` sem 11 dígitos. ...

$ sed -n '73,82p' cpf_util.py
def mascarar(valor) -> str:
    ...
    n = normalizar(valor)
    if n is None:
        return "***"
```
- **Correção sugerida:** separar as frases: "`formatar` levanta `ValueError` sem 11 dígitos; `mascarar` devolve `\"***\"`".

### A3 · baixa · `bitrix_cliente.py:15` — docstring promete retry interno de `RATE_LIMIT`, que o código não faz
- **Achado:** a docstring do módulo diz "Leitura repete RATE_LIMIT/REDE/SERVIDOR dentro da chamada". `RATE_LIMIT` e `OPERATING` nascem com `pausa_rodada=True` e a linha 96 levanta antes de dormir — na prática só `REDE`/`SERVIDOR` repetem. O `CONTRATOS.md` está certo ("RATE_LIMIT com pausa_rodada levanta"); quem ler só o docstring do módulo erra o tratamento.
- **Evidência (comando + saída real):**
```
$ grep -n 'pausa_rodada' rede_bitrix.py | head
380: ... espera=ra if ra is not None else POLITICA[Classe.RATE_LIMIT][0], pausa_rodada=True)
406: ... espera=ra if ra is not None else POLITICA[Classe.RATE_LIMIT][0], pausa_rodada=True)

$ sed -n '95,97p' bitrix_cliente.py
        espera = None if n >= maximo else rb.atraso(n, erro)
        if espera is None or erro.pausa_rodada:
            raise FalhaBitrix(erro)

$ # classificar(503, '{"error":"QUERY_LIMIT_EXCEEDED"}') em execução real:
503 QUERY_LIMIT      -> Classe.RATE_LIMIT pausa_rodada: True
```
- **Correção sugerida:** ajustar a docstring para "leitura repete REDE/SERVIDOR; RATE_LIMIT/OPERATING levantam com `pausa_rodada` para o chamador parar a rodada".

### A4 · baixa · `rede_bitrix.py:598` — `flock` bloqueante sem prazo; `aguardar(timeout=...)` não cobre a espera pela trava
- **Achado:** o `timeout` de `aguardar()` limita só a espera por tokens. Se outro processo segurar o flock do arquivo de rate limit além do prazo, a chamada bloqueia além do `timeout` (o `TempoEsgotado` nunca dispara para isso). As transações sob a trava são curtas (leitura/escrita de JSON pequeno) e o SO libera o flock na morte do processo, então o risco é baixo.
- **Evidência (comando + saída real):**
```
$ grep -n 'LOCK_EX)' rede_bitrix.py
598:            fcntl.flock(fd, fcntl.LOCK_EX)
$ sed -n '617,621p' rede_bitrix.py
            novos = tokens - custo
            espera = max(0.0, -novos / self.taxa)
            if espera > timeout:
                raise TempoEsgotado(...)
```
- **Correção sugerida:** usar `LOCK_EX | LOCK_NB` em loop com prazo (mesmo padrão de `estado_store._TravaDeArquivo`) e cair no bucket em memória com aviso quando esgotar.

## Verificações que passaram (com evidência)

1. **Estado nunca vazio na corrupção** — `EstadoCorrompido` propaga em JSON inválido e raiz não-dict, cópia `.corrompido-<ts>` uma vez por conteúdo, mutador nem roda (`estado_store.py:170-181, 313-321`).
2. **Gravação atômica** — tmp exclusivo `pid+uuid` com `O_EXCL` e `0600`, `fsync` do arquivo, `os.replace`, `fsync` do diretório, tmp removido em qualquer exceção (`estado_store.py:197-221`).
3. **Flock/merge** — trava reentrante por thread com `TimeoutError` (`estado_store.py:234-301`); `mesclar_lote` só escreve campos do dono, chaves `_*` do patch ignoradas, conflito registrado (`estado_store.py:546-655`).
4. **Máscara cobre os padrões pedidos** (execução real):
```
webhook        -> https://escritorio.bitrix24.com.br/rest/123/***/ 
xapikey        -> GET /v1/chats com header X-Api-Key: ***
telegram       -> (token real de 35 chars) 123456789:***  e  /bot123456789:***/sendMessage
cpf            -> cliente CPF ***.982.247-** mandou o kit
senha          -> Senha: ***   |  {"senha": "***"}  |  NIS ... / [senha removida]
download_url   -> .../rest/123/***/download.json?auth=***&token=***&fileId=9
```
5. **Nenhum caminho de log escapa** (execução real com `exc_info=True`):
```
LOG -> envio https://esc.bitrix24.com.br/rest/9/***/x com CPF ***.982.247-** | ... RuntimeError: falha em https://esc.bitrix24.com.br/rest/9/***/crm.get | X-Api-Key: *** valor
```
6. **`classificar` conforme contrato** (execução real): `503 QUERY_LIMIT → RATE_LIMIT pausa_rodada`; `429 OPERATION_TIME_LIMIT → OPERATING`; `403/401/200/503 com ACCESS_DENIED → ACESSO, repetir=False, permanente=True` (código vence status); `timeout → REDE repetir`; `404 → permanente`; `502 → SERVIDOR`; `detalhe` do 403 mascarado (`'ACESSO status=403 codigo=-'`, sem token).
7. **Escrita nunca repete após timeout; leitura repete** (execução real):
```
chamadas com escrita=True + timeout: 1 (deve ser 1: nunca repete)
chamadas leitura + timeout com tentativas=3: 3 (repate ate esgotar)
```
8. **Meia-abertura com sonda única entre processos** — `test_meia_abertura_libera_uma_unica_sonda_entre_instancias` e `test_uma_sonda_entre_processos_reais`: `2 passed in 0.13s`.
9. **`tipos_documento`** conforme C1/S10: extensão manda, MIME desempata, mídia não vira documento (arquivo lido por inteiro; sem achado).
10. **`pergunta_traco` regra ENVIO_INCERTO** coerente no cabeçalho e na implementação (`pergunta_traco.py:36-39, 512, 779`: timeout → `ErroEnvioIncerto`, estado gravado, sem reenvio às cegas).

## Evidências de execução (coladas)

```
$ .venv/bin/python -m pytest -q            # baseline (início da revisão)
14 failed, 728 passed in 7.54s             # as 14 são as do brief (dados de produção), nenhuma da onda 1

$ .venv/bin/python -m pytest -q            # rodada final (após commits 98173c7 e 39678de do orquestrador: +17 e +14 testes das tarefas 1–2 = +31)
14 failed, 759 passed in 7.07s             # mesma lista de 14 FAILED; a subida é dos testes novos de inventario_estado/medir_fontes, não da onda 1

$ git log --oneline -3                    # commits novos desde a baseline (fora do escopo desta revisão)
20a762d openbot/Analista: plano de recuperação de dados históricos (R0–R6)
39678de openbot/Programador: manutencao/medir_fontes.py — ... (14 testes)
98173c7 openbot/Programador: manutencao/inventario_estado.py — ... (17 testes)

$ .venv/bin/python -m pytest -q testes/test_estado_store.py testes/test_estado_concorrencia.py testes/test_rede_bitrix.py testes/test_circuit_breaker.py testes/test_circuit_breaker_integracao.py testes/test_seguranca.py testes/test_bitrix_cliente.py
278 passed in 5.02s

$ .venv/bin/python -m pytest -q testes/test_estado_concorrencia.py -v
collected 7 items ... 7 passed in 1.53s   # processos reais com spawn

$ .venv/bin/python -m pyflakes estado_store.py estado_lote.py cpf_util.py seguranca.py rede_bitrix.py circuit_breaker.py bitrix_cliente.py tipos_documento.py pergunta_traco.py vigia-bot-triagem.py esperar_bot_liberar.py
(exit 0, sem saída)   # pyflakes 3.4.0 Python 3.9.6

$ git status --short -uall                 # arquivos alterados por ESTA revisão
?? docs/remediacao/revisao/onda1-revisor-openbot.md
```

Nenhum arquivo de código foi editado nesta revisão (só este relatório). Sem rede, sem commit.

## Veredito

**APROVADO COM RESSALVAS**

A onda 1 está sólida nos pontos de maior risco: estado não se perde na corrupção, gravação é atômica e travada, a máscara e o filtro de log não deixam segredo/CPF/senha escapar nos testes executados, a classificação de erro segue o contrato, a escrita não repete após timeout e a meia-abertura tem sonda única com teste entre processos reais. Os 4 achados são de documentação/paginação — nenhum bloqueia a onda 2.

**Ressalvas (a corrigir pelas frentes donas):** A1 (média, `bitrix_cliente.py:128`), A2 (média, `CONTRATOS.md:8`), A3 (baixa, `bitrix_cliente.py:15`), A4 (baixa, `rede_bitrix.py:598`).

**O que NÃO testei nesta revisão:**
- Execução real em servidor Linux: flock em NFS, permissões `0600` do `estado.json` × usuário do painel, `/etc/triagem-bitrix`, systemd — só comportamento local.
- Nenhuma chamada de Bitrix/VibeCode/Telegram real (proibido; tudo com transporte falso).
- Se existem métodos Bitrix com página real menor que 50 (o achado A1 prova a lógica, não ocorrência em produção).
- Vazamento de log nos entrypoints da onda 2 que ainda não chamam `instalar_filtro_log()` (o filtro em si foi testado).
- Auditoria linha a linha de `pergunta_traco.py` (~830 linhas), `esperar_bot_liberar.py` e `vigia-bot-triagem.py`: cobri a regra ENVIO_INCERTO, varreduras de padrão (`except`, segredo literal, py3.9) e os testes da frente; a revisão de fundo desses três arquivos não foi completa.
- As 14 falhas da baseline são de dados de produção e não são da onda 1 (lista de FAILED conferida).
