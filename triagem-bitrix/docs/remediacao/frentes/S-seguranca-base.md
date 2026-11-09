# Frente S — base de segurança (onda 1, produtor)

Status: **concluído**. Só a BASE foi criada; os consumidores (onda 2) é que fecham S01..S14 usando estes módulos.

## Arquivos (todos novos; nenhum arquivo alheio tocado)
- `cpf_util.py`
- `seguranca.py`
- `testes/test_cpf_util.py`
- `testes/test_seguranca.py`
- `docs/remediacao/frentes/S-seguranca-base.md` (este relatório)

## Contrato entregue (CONTRATOS.md) — assinaturas idênticas, mais acréscimos
`cpf_util`: `normalizar`, `valido`, `formatar`, `gerar_para_teste` + **acréscimo** `mascarar(valor) -> "***.456.789-**"`.
- `normalizar` aceita `int` (restaura zero à esquerda) e `str` só com dígitos/`.`/`-`/`/`/espaços; texto com letras (`"CPF 123..."`) → `None` (quem extrai de texto livre isola o número antes). `bool`, negativos, float, bytes → `None`.
- `valido` rejeita as 10 sequências repetidas; DV conferido contra um oráculo independente (fórmula "11 − resto").
- `formatar` levanta `ValueError` se não houver 11 dígitos (não devolve lixo formatado como CPF).
- `gerar_para_teste(semente)` é determinístico (`random.Random(semente)`), nunca sequência repetida.

`seguranca`: `mascarar`, `mascarar_url`, `FiltroMascara`, `instalar_filtro_log`, `remover_senhas`, `ler_segredo`, `SegredoAusente`, `arquivo_temp_privado`, `remover_seguro`, mais estes **acréscimos**: `SegredoInseguro(SegredoAusente)`, `SegredoInvalido(SegredoAusente)`, `NOMES_CONHECIDOS`, `PREFIXOS_PROIBIDOS`, `MASCARA="***"` e `SENHA_REMOVIDA="[senha removida]"`.

### mascarar (idempotente)
| padrão | saída |
|---|---|
| webhook `https://<portal>/rest/<id>/<token>/...` | `https://<portal>/rest/<id>/***/...` (mantém o portal, o id e o método) |
| `X-Api-Key: v`, `"X-Api-Key": "v"`, `'x-api-key': 'v'`, `Authorization: Bearer v` | valor → `***` |
| token de bot do Telegram `<dígitos>:<35 chars>` (solto ou em `/bot<token>/`) | `<dígitos>:***` |
| querystring com credencial (`auth`, `token`, `access_token`, `_esd`, `sessid`, `signature`, `X-Amz-*`, …): a DOWNLOAD_URL assinada do Bitrix é `rest/download.json?auth=..&token=disk|..` | `auth=***&token=***`; os outros parâmetros (`fileId`, `name`) ficam |
| `https://user:pass@host` | `https://***:***@host` |
| CPF `000.000.000-00`, `00000000000`, `000000000-00` | `***.000.000-**` |
| senha: `Senha: x`, `SENHA: x`, `=SENHA: x`, `=SENHA x` (cabeçalho curto), `=ACESSO AO GOV.BR: x`, `Acesso ao gov.br (CPF e senha): x`, `Senha gov br: x`, `🔐Senha de acesso ao GOV.BR,* x`, tabela `Senha do MEU INSS {UfCrm…} x`, fichinha `NIS/Senha do INSS:** <nis> / x` (preserva o NIS), JSON/repr `"senha": "x"`, texto JSON com `\n` escapado | valor → `***` |

Falsos positivos que NÃO são mascarados, todos com teste: ids de 7 a 10 dígitos, timestamps de 10 e 13 dígitos, carimbos de 12 e 14 dígitos, chat id `-100…`, IP, sha256 hex, uuid, decimais, `LOTE-…`, `IMG-…-WA0001`, datas, valores em R$, URLs sem credencial, prosa ("esqueceu a senha do gov.br", "Senha incorreta"), placeholders (`S/SENHA`, `[senha removida]`, `***`), `10:30:45`.
O valor da senha termina no fim da linha, num `\n` escapado, numa aspa ou no começo de outro campo da mesma linha (`Senha: x Email: y` → só `x` sai). A forma sem separador (`=SENHA x`) só vale no começo da linha e com valor de um token só que tenha dígito ou símbolo.

### remover_senhas (persistência)
Troca só o VALOR por `[senha removida]` e deixa o resto do texto como estava, CPF incluído, porque a triagem precisa dele. Teste de integração: `lote_para_job.parse_ficha(remover_senhas(f))` devolve os mesmos campos que `parse_ficha(f)`, com exceção de `senha`, que passa a ser `[senha removida]`. Testado nos formatos A, C e "🔐…,*".

### FiltroMascara / instalar_filtro_log
Mascara `record.getMessage()` (já formatada; `%d`, `%(k)s` e `%%` funcionam), grava em `record.msg` e zera `record.args`. Também mascara `exc_text` (traceback) e `stack_info`. Se a string de formato estiver quebrada, não deixa os args crus vazarem. `instalar_filtro_log(logger=None)` instala o filtro no logger (padrão: a raiz) e em todos os handlers dele. O filtro do handler pega os loggers filhos que propagam. É idempotente. **Tem de ser chamado depois de configurar os handlers.**

### ler_segredo(nome, obrigatorio=True)
1. env `TRIAGEM_<NOME>` (ex.: `TRIAGEM_WEBHOOK_BITRIX`);
2. `caminhos.SEGREDOS_DIR/<nome>`: recusa com `SegredoInseguro` se `st_mode & 0o077`, se o dono não for root nem o euid, ou se não for arquivo regular. A recusa vale **também com obrigatorio=False**;
3. legados explícitos: a env `TRIAGEM_SEGREDOS_LEGADOS` (entradas separadas por `:`, no formato `nome=/arquivo` ou `/diretorio` → `/diretorio/<nome>`) substitui os padrões quando definida, e vazia desliga os legados. Padrões: `vibecode_key` → `em_base("vibecode.key")`, `/root/.vibecode/api_key`; `telegram_token` → `/root/.telegram/documentosexemplo.token`; `webhook_bitrix` e `bot_token_100003` → nenhum.
- Recusa sempre qualquer caminho sob `/tmp`, `/var/tmp`, `/dev/shm` (e `/private/...` do macOS), inclusive por symlink (`realpath`) e inclusive `SEGREDOS_DIR`.
- Legado com permissão aberta: dá **aviso** e é lido (para não derrubar a produção no deploy). Com `TRIAGEM_SEGREDOS_ESTRITO=1`, é recusado.
- `webhook_bitrix`: valida `https://<sub>.bitrix24.<tld>/rest/<n>/<token>/`, normaliza a barra final e recusa http, outro host, `bitrix24.com.br.evil.com`, URL com método ou id não numérico (`SegredoInvalido`).
- Nome fora de `[a-z0-9_]` → `ValueError` (evita traversal). Nunca loga o valor nem o põe em mensagem de erro (testado com caplog em DEBUG).

### arquivo_temp_privado / remover_seguro
- Diretório `caminhos.em_base("tmp")`: cria com 0700, corrige se estiver aberto, recusa se for symlink ou de outro dono. Arquivo: `O_CREAT|O_EXCL|O_NOFOLLOW`, 0600 mesmo com umask 0, nome `prefixo + token_hex(8) + sufixo`.
- `remover_seguro`: zera e faz fsync antes do unlink. Não falha se o arquivo não existir. Symlink: apaga só o link. **Arquivo com mais de um hard link: só desvincula** (zerar destruiria a outra cópia). Diretório → `IsADirectoryError`.

## Achados (base para a onda 2)
| ID | status | o que esta frente entrega |
|---|---|---|
| S01 | base pronta | `remover_senhas` para estado, fila e backups; `mascarar` para log. A aplicação fica com os donos de `triagem_grupo.py`, `lote_para_job.py` e `lote_para_fila.py`, e o saneamento dos `.bak` também é deles |
| S02 | base pronta | `gerar_para_teste` para trocar os CPFs reais dos testes e do código |
| S04 | base pronta | `cpf_util.mascarar`, `mascarar` e `instalar_filtro_log` |
| S06 | base pronta | `ler_segredo` sem varredura, sem /tmp, só com legados explícitos |
| S07 | base pronta | `mascarar_url` e `mascarar` para `triagem_grupo.py:450,452` e `rotulo or url` |
| S09 | base pronta | `arquivo_temp_privado` e `remover_seguro` em vez de `/tmp/_entradas.json`, `/tmp/_enfileirar.js` e `/tmp/certidao_*.pdf` |
| S12/S13 | parcial | `mascarar` cobre a URL e o X-Api-Key em log. Urlencode, timeout e o header só para o host VibeCode ficam com o dono de `certidoes.py` |
| S03, S05, S08, S10, S11, S14 | não aplicável aqui | são das frentes donas, ou do servidor no caso do S08 (units/UMask) |

## Evidência (comandos reais)
- `.venv/bin/python -m pytest -q testes/test_seguranca.py testes/test_cpf_util.py` → **232 passed**.
- Os mesmos testes com `TMPDIR=/tmp` (simula o Linux, onde o `tmp_path` fica em /tmp) → **232 passed**.
- Suíte inteira `.venv/bin/python -m pytest -q` → **14 failed, 717 passed**. As 14 falhas **não são minhas**: são as mesmas com e sem os meus testes (comparação por diff da lista de FAILED), e nenhum desses testes importa `cpf_util`/`seguranca`. Lista: `test_variaveis_contrato.py` (7), `test_servico_lote.py` (3), `test_nome_regras.py::test_job_prefere_o_nome_do_lote_quando_a_ficha_vem_suja`, `test_pasta_fichinha.py::test_cpf_invalido_nao_consulta`, `test_ponte_fila.py::test_persistencia_atomica_vem_do_filajobs_do_painel`, `test_telefones_fichinha.py::test_telefones_sem_repeticao_e_no_maximo_dois`. Outras frentes estão editando em paralelo, então o número pode mudar.
- Desempenho: um JSON de 1,5 MB com 3.000 fichas sai de `remover_senhas` em 0,16 s e de `mascarar` em 0,31 s. A primeira versão do regex de rótulo levava **~150 s** em prosa patológica (`"senha senha de do ao "*5000`). Corrigi com repetição limitada e sem `[ \t]*` encadeados, e o teste `test_mascarar_sem_backtracking_catastrofico` impede a regressão.

## Mudanças de comportamento / decisões a revisar
- Número de **11 dígitos colado** (ex.: celular com DDD sem pontuação, NIS de 11 dígitos) é mascarado como CPF em log. Telefone formatado `(92) 99123-4567` não é. Decidi assim porque os dois são dado pessoal.
- A máscara de CPF mantém os 6 dígitos do meio (`***.456.789-**`), como sugere o S04. Se quiserem a máscara total, a troca é em `cpf_util.mascarar`.
- Legado com permissão aberta só gera aviso, sem `TRIAGEM_SEGREDOS_ESTRITO=1`. Depois do `chmod 600` no servidor, ligar o estrito.
- O formato do webhook exige `*.bitrix24.<tld>`. Um portal com domínio próprio seria recusado; hoje é `*.bitrix24.com.br`.
- CPF com espaços entre os blocos (`123 456 789 01`) não é mascarado por `mascarar`, mas `cpf_util.normalizar` aceita.

## Pendências para outras frentes (onda 2)
- **Consumidores**: chamar `seguranca.instalar_filtro_log()` logo após configurar o logging em cada entrypoint. Trocar `print` com CPF ou nome por `mascarar`. Usar `ler_segredo("webhook_bitrix")` e remover `candidatos_webhook`, `B24_WEBHOOK_FILE=/tmp/...` e as varreduras de `/opt/leads-doenca`, PrevSync e mcp. Aplicar `remover_senhas` antes de `serializar_lote` e antes de gravar `fila-jobs`/backups. Usar `arquivo_temp_privado` + `remover_seguro` em `finally`.
- `circuit_breaker.py`, `rede_bitrix.py`, `pergunta_traco.py` e `estado_lote.py` já fazem import tardio de `seguranca.mascarar`/`cpf_util.valido`. Agora podem trocar o fallback por um import direto.
- **Servidor**: criar `/etc/triagem-bitrix/` (0700) com `webhook_bitrix`, `vibecode_key` e `telegram_token` em 0600 (ou `EnvironmentFile=` com `TRIAGEM_*`), dar `chmod 600` em `vibecode.key` e `/root/.vibecode/api_key`, depois `TRIAGEM_SEGREDOS_ESTRITO=1`. Rotacionar o webhook do usuário 1 (S06/S07).

## Riscos
- A heurística de senha é por regex. Um formato novo de ficha que não esteja na tabela de testes pode passar sem máscara. Ao achar um formato real novo, acrescentar o caso (com valor fictício) em `CASOS_MASCARA`.
- `mascarar` pode cortar texto de prosa que tenha `senha:` seguido de outra coisa (ex.: "pedir a senha: amanhã"). O efeito é só perder texto no log, nunca vazar.
