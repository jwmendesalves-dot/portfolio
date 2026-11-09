# Frente Z — limpeza de integração (onda 3)

Arquivos alterados: `pergunta_traco.py`, `vigia-bot-triagem.py`, `esperar_bot_liberar.py`,
`circuit_breaker.py`, `estado_lote.py`, `download_svc.py`, `monitor_lotes.py`,
`testes/test_estado_lote.py`, `testes/test_monitor_lotes.py`, `testes/test_senha_cabecalho_curto.py`.
`triagem_grupo.py` NÃO foi tocado.

## Z1 · fallbacks provisórios / import tardio — corrigido

| arquivo | antes | agora |
|---|---|---|
| `pergunta_traco.py` | `import seguranca`/`rede_bitrix` tardios dentro de `try`, `_RX_MASCARA` + `_mascarar_local` | import direto no topo; `_mascarar` = `seguranca.mascarar`; `_classe_rede_bitrix` chama `rede_bitrix.classificar` direto (mantido só um `except` em volta da CHAMADA: roda no caminho de erro do envio e não pode levantar). Mapa final por status HTTP em `_tipo_falha` mantido de propósito: não é fallback de módulo, cobre classes sem mapa próprio (400 INVALIDO / 404 NAO_ENCONTRADO → RECUSADO; 5xx → INCERTO). |
| `vigia-bot-triagem.py`, `esperar_bot_liberar.py` | `_mascarar` com regex local; `ler_segredo` em import tardio com `except Exception` | `import seguranca` direto; `_mascarar` = `seguranca.mascarar`; leitura da chave captura só `seguranca.SegredoAusente`/`OSError` (antes engolia qualquer exceção, inclusive bug) e mantém a leitura do arquivo `<BASE>/vibecode.key` como antes. `import re` órfão removido. |
| `circuit_breaker.py` | `caminhos` em try/except; `mascarar_texto` tentava `seguranca` e caía num mínimo local | `import caminhos`, `import seguranca` diretos (sem ciclo: `seguranca` importa só `caminhos`/`cpf_util`; `rede_bitrix → circuit_breaker` continua unidirecional). `mascarar_texto` = `seguranca.mascarar` + `_RE_CHAVE` por cima (ver mudança de comportamento). Removidos `_RE_WEBHOOK/_RE_TG/_RE_CPF` (cobertos por `seguranca`). |
| `estado_lote.py` | `_cpf_valido_local` + import tardio de `cpf_util` | `import cpf_util` direto; `_cpf_valido_local` removido; `import re` órfão removido. |

**Mudança de comportamento (circuit_breaker):** com `seguranca` presente, o disjuntor antes usava SÓ
`seguranca.mascarar`, que **não mascara** pares genéricos `token=…`/`access_token=…`/`auth=…`
(conferido: `seguranca.mascarar("token=abc123def&x=1")` devolve o texto inteiro). O mínimo local
(que mascarava) só rodava com `seguranca` ausente. Agora `_RE_CHAVE` é sempre aplicado por cima →
máscara mais forte em `Erro.detalhe`/logs do `rede_bitrix`. Idempotente (não remascara `***`).

Testes ajustados: `test_estado_lote.py::test_cpf_de_teste_e_valido_via_cpf_util` (era sobre o
fallback removido) e `test_usa_cpf_util_quando_existe` (agora `monkeypatch.setattr(estado_lote, "cpf_util", …)`).
`testes/test_rede_bitrix.py::test_mascara_minima_sem_seguranca` (não é meu) continua verde — mas o
nome agora engana: `sys.modules["seguranca"]=None` não tem mais efeito; ele passa a provar a máscara
de `token=` pelo `_RE_CHAVE`. → pendência R: renomear.

Evidência: `grep -n "TODO(orquestrador)|import seguranca  # noqa: PLC|import cpf_util  # noqa: WPS"` nos 5 arquivos → vazio.

## Z2 · download_svc `re` sem uso + monitor de falhas permanentes de download — corrigido

- `download_svc.py`: `import re` removido (pyflakes limpo).
- `monitor_lotes.py`: nova `itens_da_fila_download(caminho=None)` lê `download_fila.json`
  (`$DOWNLOAD_FILA_JSON` ou `caminhos.em_base("download_fila.json")`, mesma regra do download_svc;
  aceita formato canônico `{"itens": [...]}` e o legado lista pura; ausente/corrompido → `[]`).
  Itens `falha_permanente` → categoria `download_falha_permanente`; `telegram_legado` →
  `download_telegram_legado` (rótulos próprios, exibidos logo após `falha`). Chave na memória de
  avisos (`monitor-avisos.json`, mesma de F2-07): `download:<file_id>` com `lote_origem` = lote_id;
  dedupe por file_id; motivo passa por `seguranca.mascarar`. Só avisa na MUDANÇA: arquivo novo em
  falha → aviso; mesmo conjunto → silêncio; arquivo que sai da fila é esquecido.
- Testes novos (`testes/test_monitor_lotes.py`): `test_fila_download_vira_categorias_proprias_e_mascara_cpf`,
  `test_fila_download_ausente_corrompida_ou_legada`, `test_cli_avisa_falha_de_download_so_na_mudanca`
  (subprocess: 1ª rodada avisa, 2ª silêncio, 3ª só o arquivo novo). No código antigo falham (função/categoria inexistente).
- Evidência: `.venv/bin/python -m pytest -q testes/test_monitor_lotes.py` → 24 passed.

## Z3 · senha real em `testes/test_senha_cabecalho_curto.py` — corrigido

Substituídos por valores fictícios de mesmo formato (sem o valor real em lugar nenhum; troca feita
por regex de formato, sem imprimir): senha `Maiúscula+14 minúsculas+dígito` (16 chars, com/sem ponto
final) → valor fictício de mesmo formato; senha gov.br `3 letras+3 dígitos+@` → fictícia; e também
dados pessoais reais no mesmo teste: nome completo e nome no docstring/nome da função, ID de lote,
CPF (DV válido real → `cpf_util.gerar_para_teste(4242)` formatado), nome da indicação e data.
O que o teste verifica não mudou (parse da senha nos formatos com/sem `:`, gov.br, ficha completa,
fichinha do painel). Evidência: `pytest -q testes/test_senha_cabecalho_curto.py` → 5 passed.
**Risco:** o valor real continua no histórico git (`/opt/triagem-bitrix` e branch) — orquestrador:
considerar troca da senha pelo cliente/rotina do escritório; reescrita de histórico só se o repo sair do servidor.

## Z4 · varredura de CPF (DV válido) e e-mail não-example — listado

Script: `~/.hermes/cache/scratch/varre_pii.py` (todo o repo exceto `.venv`, `.git`, `docs/auditoria`;
regex `\d{11}` e `000.000.000-00` + `cpf_util.valido`; "sintético" = igual a `gerar_para_teste(n)`,
n<20000, ou linha com `gerar_para_teste`; "exemplo público" = 698.308.525-63, 924.576.268-89, 708.393.983-79,
492.907.337-56; "artificial" = ≥6 zeros, sequência ou ≤3 dígitos distintos). Valores NÃO impressos;
`Vn` identifica o mesmo valor entre arquivos. 11 ocorrências sintéticas (ok).

**Nos meus arquivos: nenhum suspeito** (o único era o do Z3, já corrigido).

Suspeitos em arquivos de outras frentes (para o orquestrador):

| arquivo:linha | tipo | classificação |
|---|---|---|
| README.md:115 | CPF formatado | suspeito real V1 |
| README.md:167 | CPF formatado | suspeito real V2 |
| certidoes.py:24 | CPF formatado | suspeito real V2 (mesmo do README) |
| testes/test_cabecalho_encaminhado.py:26, 29 | CPF formatado | suspeito real V16 |
| testes/test_pintar_linha.py:40, 48 | CPF 11 díg. e formatado | suspeito real V13 |
| testes/test_pintar_linha.py:41 | CPF 11 díg. | suspeito real V14 |
| testes/test_pintar_linha.py:115, 116 | CPF formatado / 11 díg. | suspeito real V15 |
| testes/test_telegram_batidas.py:46 | CPF formatado | suspeito real V17 |
| testes/test_traco_delimitador.py:133, 143 | CPF formatado / 11 díg. | suspeito real V18 |
| triagem_grupo.py:618…4085 (101 linhas, autoteste embutido) | CPF / 11 díg. | 9 valores distintos (V3–V11; V6 em 51 linhas, V5 18, V9 15). Parte pode ser telefone com DV válido usado como caso negativo — conferir. Linhas mudam: arquivo sendo editado por outra frente; rodar o script de novo. |
| lote_para_job.py:288 | e-mail (comentário) | domínio gmail.com — suspeito real |
| cpf_util.py:25; testes/test_cpf_util.py:22, 23, 87 | CPF | artificial (padrão óbvio) V12 — ok |
| testes/test_cpf_util.py:32; testes/test_rede_bitrix.py:234, 236 | CPF formatado | exemplo público conhecido — ok |
| testes/test_seguranca.py:81 | e-mail | domínio `host.exemplo` — fictício, ok |
| triagem_grupo.py:3311 | e-mail | `x@y.com` — fictício, ok |

Para reproduzir: `.venv/bin/python ~/.hermes/cache/scratch/varre_pii.py .` (imprime só arquivo:linha/classe).

## Verificação final

- `pyflakes` nos meus arquivos + testes tocados → rc=0, sem saída.
- Suíte inteira `.venv/bin/python -m pytest -q` → **951 passed, 1 failed, 2 skipped**.
  A falha é `testes/test_circuit_breaker_integracao.py::test_redireciona_para_webhook_apos_n_falhas_da_api`:
  `TypeError: fake_webhook() got an unexpected keyword argument 'first_id'` em `triagem_grupo.py:2904` —
  vem da edição em andamento de `triagem_grupo.py` pela outra frente (assinatura nova `first_id`; o
  fake do teste não aceita; `git diff triagem_grupo.py` tem 13 linhas com `first_id`). Depois do Z1
  (antes dos 3 testes novos do Z2 e da edição alheia) a suíte deu 949 passed, 0 failed.

## Pendências para outras frentes
- **triagem_grupo / dona do test_circuit_breaker_integracao**: fake `fake_webhook` precisa aceitar `first_id` (ou `**kw`).
- **R**: renomear `test_rede_bitrix.py::test_mascara_minima_sem_seguranca` (não testa mais "sem seguranca").
- **S**: `seguranca.mascarar` não cobre `token=`/`access_token=`/`auth=` genéricos; o disjuntor compensa, mas logs que usam `seguranca` direto não.
- **S**: `ler_segredo` com arquivo principal inseguro levanta `SegredoInseguro` (filha de `SegredoAusente`); vigia/esperar_bot então leem `<BASE>/vibecode.key` direto (comportamento antigo preservado). Se a política for "recusar e parar", decidir em S e eu/dono ajusto o `except`.
- **Orquestrador**: CPFs/e-mail suspeitos da tabela Z4 (README, certidoes.py, testes de outras frentes, autoteste do triagem_grupo, comentário do lote_para_job); senha real no histórico git.
