# Revisão final 3 — segredos, PII e JEV (branch `remediacao`, HEAD `ce6cc54`)

Revisor independente, somente leitura (nenhum código editado, sem git de escrita, sem rede).
Testes adversariais temporários: `~/.hermes/cache/scratch/revfinal3/test_adversarial.py`
(chaves e CPFs sintéticos — `cpf_util.gerar_para_teste(7)`; nenhum valor real impresso aqui).

## Veredito

**APROVADO COM RESSALVAS.** As invariantes centrais se mantêm: a chave não vaza no formato
real (`apikey_…`) em URL, cabeçalho, JSON aninhado, repr ou traceback encadeado; o JEV nunca
devolve `manter_no_kit` para dono desconhecido (7 respostas maliciosas testadas); o modo sombra
devolve sempre a regra; falha do JEV devolve a regra com `fonte="falha_jev"`. Duas ressalvas
de peso médio precisam de correção **antes de ligar `typesafe.modo` = sombra/decide** (hoje
`ativo=false` por padrão, então nada sai em produção):

1. CPF nos formatos `000.000.000/00` e `000.000.000 00` vai **cru** para a API externa (state) e para o JSONL da sombra.
2. `confidence` NaN / >1 / booleano atravessa o limiar: no lote, o JEV dispara `separar` automático.

## Achados

| # | Sev. | Arquivo:linha | Resumo |
|---|------|---------------|--------|
| A1 | **Média** | `decisoes_jev.py:58` × `seguranca.py:80`; uso em `decisoes_jev.py:73-74,133,209,312` | regex de CPF do JEV aceita `/` e espaço antes do DV; a máscara não → CPF cru no state enviado ao TypeSafe e no JSONL |
| A2 | **Média** | `decisoes_jev.py:158,227,280-290` (e `160` para score) | `confidence` NaN/42/`true` aceito; NaN faz `conf < 0.85` dar False → `acao="separar"` com `confianca=nan` |
| A3 | Baixa | `typesafe_cliente.py:249` + `decisoes_jev.py:344` | chave com quebra de linha interna: `ValueError('Invalid header value b"Bearer <chave>…"')` do http.client escapa do `except` e vai ao log; mascarar só cobre se a chave tiver prefixo `apikey_` |
| A4 | Baixa | `seguranca.py:63` (`_PARAMS_LIVRES`) | `{"api_key": "<v>"}` / `{"typesafe_key": "<v>"}` em JSON/texto livre não são mascarados (só importa para chave sem prefixo `apikey_`) |
| A5 | Baixa | `typesafe_cliente.py:226-228` | `_resumo_erro` copia até 60 chars de `error`/`code`/`type` livres do corpo de erro; um 5xx de proxy que ecoe a entrada põe nome do cliente (ou chave sem prefixo) na exceção/log. CPF nos formatos comuns sai mascarado |
| A6 | Baixa | `telegram_grupo.py:40,88-90` | `ler_token` lê o arquivo direto (`TELEGRAM_TOKEN_FILE`), sem `seguranca.ler_segredo`: sem checagem 0600/dono, sem recusa em /tmp, ignora `TRIAGEM_SEGREDOS_ESTRITO`. Token fica fora do argv e os erros saem mascarados (ok) |
| A7 | Info | `decisoes_jev.py:312` | NaN vindo do JEV é gravado como literal `NaN` no JSONL (JSON inválido para leitores estritos: `jq`, JS) |
| A8 | Info | `certidoes.py:371` | stdout/stderr do node (80 chars cada) voltam na mensagem de erro sem `seguranca.mascarar` |

### A1 — CPF cru no state e no JSONL (Média)
Reprodução (CPF sintético; `montar_documento` + sombra com JEV que ecoa a entrada):
```
formato                 fato_cpf  mascarar_ok  state_cru  jsonl_cru(eco)
00000000000             sim       True         False      False
000.000.000-00          sim       True         False      False
000000000-00            sim       True         False      False
000.000.000/00          sim       False        True       True
000.000.000 00          sim       False        True       True
000 000 000 00          ausente   False        True       True
```
O `fato_cpf` reconhece 3 e 4 como identidade (o código usa o CPF) mas `_trecho_seguro` não mascara,
e o docstring (`decisoes_jev.py:22-25`) promete "o state não leva CPF cru". O JSONL só pega o caso
se o JEV/`usage` ecoar o texto, mas o state vai sempre.
**Correção:** em `_trecho_seguro`, depois de `seguranca.mascarar`, trocar cada casamento de
`_RX_CPF_TEXTO` por `cpf_util.mascarar(...)`; idealmente estender `seguranca._RX_CPF` para
`\d{3}\.?\d{3}\.?\d{3}[-/ ]?\d{2}` (fonte única), com teste para `/` e espaço. Formato todo com
espaços (linha 6) fica como pendência (risco de falso positivo em telefones).

### A2 — `confidence` fora de [0,1] (Média, só em `modo=decide`)
```
answers {"acao_lote_misto": {"choice":"separar","confidence": NaN}}, n_documentos_sem_dono=0
-> {'acao': 'separar', 'confianca': nan, 'fonte': 'jev', ...}
answers {... "confidence": 42} -> {'acao': 'separar', 'confianca': 42.0, ...}
```
`json.loads` aceita `NaN`; `True` passa em `isinstance(conf,(int,float))` e vira 1.0. Em
`rotear_documento`/`rotear_foto` o efeito é inofensivo (testado: nunca `manter_no_kit`; NaN → revisão/quarentena),
mas `separar` é ação automática.
**Correção:** helper único `_conf(v)` → `float(v)` só se `type(v) in (int, float)`, `math.isfinite`
e `0 <= v <= 1`; senão `0.0`. Mesmo tratamento para `score` (`isfinite` e dentro de 0..len(criteria)-1).
Teste com NaN, inf, 1.5, -0.1, `True`.

### A3 — chave no `ValueError` do http.client (Baixa)
`TRIAGEM_TYPESAFE_KEY="<chave sem prefixo>\nlinha2"` → `lote_misturado_acao(..., modo=decide)`:
devolve a regra (`fonte=falha_jev`, ok), mas o `log.warning` em `decisoes_jev.py:344` contém
`b'Bearer <chave>\nlinha2'`. Com chave `apikey_…` o `_RX_APIKEY_TYPESAFE` mascara (testado).
Plausível só com arquivo de chave de 2 linhas **e** chave fora do formato documentado.
**Correção:** `ler_chave` recusar valor com espaço/controle (`re.fullmatch(r"[\x21-\x7e]+", chave)` → `SegredoInvalido`
sem o valor); em `_chamar`, `except ValueError` → `ErroRequisicao("cabeçalho inválido (valor não exibido)")`;
em `seguranca`, regra `(?i)\bbearer\s+\S+` independente do nome do cabeçalho.

### A4/A5/A6 — correções sugeridas
- A4: incluir `api_key`, `apikey`, `typesafe_key`, `x-api-key` em `_RX_PARAM_CHAVE` (forma JSON `"nome": "v"`, sem risco de prosa).
- A5: em `_resumo_erro` aceitar só valores que casem `^[a-z_]{1,40}$` (códigos de máquina); o resto vira `"<detalhe omitido>"`.
- A6: `ler_token` → `seguranca.ler_segredo("telegram_token")` (o legado `/root/.telegram/documentosexemplo.token` já está em `_legados_padrao`), mantendo `TELEGRAM_TOKEN_FILE` via `TRIAGEM_SEGREDOS_LEGADOS`.
- A7: `json.dumps(..., allow_nan=False)` dentro de try, ou normalizar via A2.
- A8: envolver em `seguranca.mascarar(...)`.

## Verificações que passaram
- `git log -p fbc440c..HEAD | grep -cE 'apikey_[a-f0-9]{20}|rest/[0-9]+/[a-z0-9]{10,}'` → **4**. Conferi as 4 (sem imprimir valor): todas são webhooks de fixture em host fictício — `testes/test_cenario_integral.py`, `testes/test_triagem_download.py`, `testes/test_triagem_seguranca.py` (`exemplo.bitrix24.com.br`) e `testes/fakes_download.py` (`teste.bitrix24.com.br`). Nenhuma chave `apikey_` nem webhook real no histórico novo (58 commits).
- Máscara da chave `apikey_…`: em querystring (`?apikey=`, `?api_key=`), `Authorization: Bearer`, JSON aninhado com `Authorization`, `repr(dict)`, texto livre — 8/8 ok.
- `FiltroMascara` com traceback encadeado (`raise … from e`, chave + CPF nos dois níveis, CPF nos args) — nada cru no log.
- Erros 401/422 do cliente: sem chave; corpo 422 com `input` contendo CPF+chave → só `type@loc` aproveitado.
- Regra de ouro: 7 respostas maliciosas (choice inexistente `manter_no_kit`, conf 7.5, NaN, `true`, score NaN, answer não-dict, vazio) → nunca `manter_no_kit` para documento sem CPF válido.
- Sombra: 6 formatos de CPF × JEV malicioso → decisão devolvida sempre == regra.
- Falha do JEV (ValueError, HTTP 401/500, desligado) → regra + `fonte=falha_jev`; config quebrado → regra.
- `seguranca.ler_segredo`: `SegredoRecusado` não herda de `SegredoAusente` (`seguranca.py:263`); modo estrito converte toda recusa por arquivo inseguro; /tmp recusado; mensagens sem valor.
- `rede_bitrix`/`bitrix_cliente`: detalhe passa por `mascarar` (`rede_bitrix.py:458`); escrita ambígua não repete (`rede_bitrix.py:452-454`, `bitrix_cliente.py:64`).
- `certidoes.py`: Bitrix via `bitrix_cliente` (webhook fora do argv), `X-Api-Key` só para o host VibeCode, chave via `ler_segredo`; node recebe só caminhos no argv (`certidoes.py:365`).
- `telegram_grupo.py`: token só na URL HTTPS (não argv); todos os `print` de erro passam por `seguranca.mascarar`.
- `manutencao/sanear_senhas.py`: saída só com contagens e nomes de exceção (`type(e).__name__`); gravação tmp `O_EXCL` 0600 + fsync + `os.replace`.
- Suíte focal: `test_seguranca, test_typesafe_cliente, test_decisoes_jev, test_rede_bitrix, test_sanear_senhas, test_telegram_config_invalida, test_triagem_seguranca` → **351 passed**.

## Evidência dos testes adversariais
`pytest revfinal3/test_adversarial.py`: 16 passaram (máscara da chave no formato real, traceback, regra de ouro);
9 falharam, todos explicados acima: A4 ×2, A3 ×2 (texto + log real), A5 ×1 (eco de 500 com chave sem prefixo), A2 ×2, A1 ×1, A7 ×1.

## O que NÃO testei
- API TypeSafe real (sem rede/chave): formato real de corpos de erro 5xx e se ela ecoa entrada.
- Servidor: permissões reais de `/etc/triagem-bitrix/*`, `/root/.telegram/*.token`, JSONL da sombra existente.
- Integração do decisor nos fluxos de triagem (o J2 só expõe as funções; não conferi chamadores).
- `rede_bitrix` só por leitura e suíte existente; não fiz fuzz do classificador.
- Formatos de CPF além dos 7 listados (ex.: OCR com `O` no lugar de `0`).

## O que corrigir antes de ligar sombra/decide
1. A1 — `decisoes_jev.py:73-74` (`_trecho_seguro`) e/ou `seguranca.py:80`: mascarar CPF com `/` e espaço antes do DV.
2. A2 — `decisoes_jev.py:158,160,227,280`: validar `confidence`/`score` finitos e no intervalo.
3. (recomendado) A3 em `typesafe_cliente.py:129-142,249`; A6 em `telegram_grupo.py:88-90`.
