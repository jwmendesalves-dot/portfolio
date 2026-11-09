# Frente Z2 (onda 3) — ajustes pequenos de contrato

Arquivos alterados: `seguranca.py`, `estado_store.py`, `rede_bitrix.py` (só docstring),
`lote_para_job.py` (só o comentário da linha 288), `testes/test_seguranca.py`,
`testes/test_estado_store.py`, `testes/test_rede_bitrix.py`.

## 1 · `seguranca.mascarar` cobre `token=`/`access_token=`/`auth=`/`refresh_token=`/`client_secret=` — corrigido

- Antes: só mascarava esses nomes em querystring (`?`/`&` antes). `mascarar("token=abc123def&x=1")`
  devolvia o texto inteiro (pendência S do Z).
- Agora (`_RX_PARAM_LIVRE` + `_RX_PARAM_CHAVE`, aplicados em `_mascarar_urls`, logo valem para
  `mascarar` e `mascarar_url`):
  - texto livre/kwargs/form: `token=x`, `auth = x`, `access_token='x'`, `(client_secret=x)`;
  - chave JSON/dict com valor string: `"token": "x"`, `{'refresh_token': 'x'}`.
- Lista curta de propósito (só os 5 nomes OAuth/Bitrix); `key`/`hash`/`sig` em texto livre dariam
  falso positivo em prosa. Nome exato: não casa colado à esquerda (`oauth=`, `mytoken=`, `x.token=`,
  `x-auth=`) nem com continuação (`tokens=`, `auth_type=`, `token_count=`, `client_secrets=`).
  Prosa (`token expirado`, `auth falhou`) e valor não-string (`"auth": null`, `"token": 123`) ficam.
- Mudança visível: `token=<token Telegram>` agora vira `token=***` (antes mantinha o id do bot:
  `token=1234567890:***`). Caso da tabela ajustado. Mais forte, não mais fraco.
- Testes: `test_mascara_param_secreto_em_texto_livre` (15 casos, idempotência e `mascarar_url`),
  `test_param_secreto_falso_positivo_nao_mascara` (15 casos), `test_param_secreto_preserva_resto_da_linha`.
  No código original os 15 positivos falham (conferido manualmente antes da mudança:
  `token=abc123def`, `erro auth=…`, `access_token=…&a=1`, `client_secret=…`, `"token": "…"` voltavam crus).

## 2 · `estado_store.PROPRIEDADE_CAMPOS`: donos para o plano de recuperação — corrigido

Campos que o plano (`triagem-bitrix-openbot/docs/remediacao/PLANO-RECUPERACAO-DADOS.md`, R1 e R2)
cita como "sem dono declarado" e que caíam em `triagem` via `dono_do_campo` (log + `CAMPOS_DESCONHECIDOS`):

| campo | dono | por quê |
|---|---|---|
| `ignorado`, `ignorado_em`, `ignorado_motivo`, `ignorado_por` | `revisao` | é a decisão de não encaminhar (`estado_lote._ignorado` tira o lote da fila/painel), mesma natureza de `pronto`/`requer_revisao`. Gravado por `revisao`, o campo fica reivindicado e o ciclo da triagem (que regrava o lote inteiro a cada 2 min) não o desfaz. |
| `cpf_confiavel` | `identificacao` | é juízo sobre o `cpf`, que é da identificação. **Não** entrou em `_GRUPO_IDENTIFICACAO`: a heurística legada (`cpf_origem` preenchido) reivindicaria o campo antes de alguém gravá-lo e o script do plano (com `dono="triagem"`) passaria a dar conflito. |

- Compatibilidade com o plano como está escrito (`dono="triagem"`): continua aplicando sem conflito,
  porque a triagem, produtora-base, preenche campo ainda não reivindicado (teste
  `test_ignorado_ainda_gravavel_pela_triagem_enquanto_ninguem_reivindica`). Mas aí ninguém reivindica.
  **Recomendação ao plano:** R1 aplicar e reverter com `dono="revisao"`; R2 passo 2 com
  `dono="identificacao"`. Depois de reivindicado por `revisao`, um rollback com `dono="triagem"` dá
  conflito (o critério de parada do plano já pega isso).
- C2a / `triagem_grupo.py`: todas as chaves de `serializar_lote` (o registro persistido) já têm dono
  (varredura automática: 0 faltando). `f1_regiao` fica no topo do estado (`est["f1_regiao"][dialog]`),
  não no lote — não é campo de lote. Chaves `_alerta_barreira`, `_kit_pela_barreira`,
  `_revisao_barreira`, `_chave_abertura`, `_realocadas` são internas do lote em memória (prefixo `_`
  é ignorado por `mesclar_lote`); `marcador_fim`/`fechado_pelo_traco` também são de memória
  (`marcador_fim` persiste como `marcador_fim_textual`, já declarado). Nada a declarar.
- Testes: `test_donos_campos_do_plano_de_recuperacao_declarados`,
  `test_ignorado_pela_revisao_sobrevive_ao_ciclo_da_triagem`,
  `test_ignorado_ainda_gravavel_pela_triagem_enquanto_ninguem_reivindica`,
  `test_cpf_confiavel_e_da_identificacao_e_nao_e_reivindicado_pela_heuristica_legada`.
  Os dois primeiros falham no código original (campo sem dono → triagem; ciclo da triagem desfazia).
- Bônus: `import os` duplicado em `testes/test_estado_store.py` removido (pyflakes).

## 3 · `test_mascara_minima_sem_seguranca` renomeado — corrigido

Agora `test_mascarar_cobre_webhook_token_senha_e_cpf`; tirado o `monkeypatch.setitem(sys.modules,
"seguranca", None)` (sem efeito: `rb.mascarar` → `circuit_breaker.mascarar_texto` →
`seguranca.mascarar`, import direto). Docstring de `rede_bitrix.mascarar` corrigida (falava em
"import tardio; fallback mínimo local", que não existe mais).

## 4 · E-mail real em comentário (`lote_para_job.py:288`) — corrigido

Trocado por `fulano@example.com`. `grep` por e-mail fora de `example.com` no arquivo → vazio.

## 5 · Decisão S: modo estrito de segredos — corrigido (padrão inalterado)

- Padrão (sem `TRIAGEM_SEGREDOS_ESTRITO`): **igual a antes**. `SegredoInseguro` continua filha de
  `SegredoAusente`; legado com permissão aberta só gera aviso e é lido; vigia/esperar_bot que capturam
  `SegredoAusente` e leem `<BASE>/vibecode.key` direto seguem funcionando. Não derruba produção no deploy.
- Defeito encontrado: com `ESTRITO=1`, a recusa era `SegredoInseguro` (⊂ `SegredoAusente`), então o
  `except SegredoAusente: open(legado)` dos consumidores engolia a recusa e lia o arquivo aberto do
  mesmo jeito. O estrito não recusava "em todos os caminhos".
- Agora, com `ESTRITO=1`, **toda** recusa por arquivo inseguro (principal com 0o077/dono estranho,
  legado aberto, caminho em `/tmp`/`/var/tmp`, diretório, não-regular) levanta
  `seguranca.SegredoRecusado(RuntimeError)` — que **não** herda de `SegredoAusente` — e a mensagem
  traz "[modo estrito]" (nunca o valor). Novo helper `seguranca.modo_estrito()`. Segredo ausente
  continua `SegredoAusente`; arquivo 0600 continua lido.
- Testes: `test_legado_inseguro_avisa_e_estrito_recusa` (ajustado), `test_estrito_recusa_principal_inseguro_com_segredorecusado`,
  `test_estrito_recusa_legado_nao_regular_e_diretorio`, `test_estrito_recusa_legado_em_tmp`,
  `test_estrito_fura_fallback_de_consumidor_que_captura_ausente` (reproduz o padrão vigia/esperar_bot:
  padrão lê; estrito para; depois do `chmod 600` lê normal), `test_estrito_aceita_arquivo_seguro_e_ausente_continua_ausente`.
- **Recomendação de operação:** no servidor, `chmod 600` em `<BASE>/vibecode.key`,
  `/root/.vibecode/api_key`, `/root/.telegram/documentosexemplo.token` (e o que estiver em
  `/etc/triagem-bitrix/`, dir 0700), conferir com `stat -c '%a %U' …`, **depois** ligar
  `TRIAGEM_SEGREDOS_ESTRITO=1` nas units/cron (EnvironmentFile). Ligar antes do chmod para o processo
  com `SegredoRecusado` (de propósito). Efeito com ESTRITO em quem captura só `SegredoAusente`
  (`triagem_grupo.candidatos_webhook`, `completar_cpf_por_nome.main`, vigia, esperar_bot): a exceção
  sobe e o processo para com traceback (mascarado pelo filtro de log) em vez de ler o arquivo aberto.

## Evidência

- `.venv/bin/python -m pyflakes seguranca.py estado_store.py rede_bitrix.py testes/test_seguranca.py testes/test_estado_store.py testes/test_rede_bitrix.py` → limpo (rc=0).
  `lote_para_job.py` tem 3 avisos antigos (variáveis `marcar`/`tel`/`alvo` sem uso, linhas 500/520/568) — não mexi (só me cabia o comentário).
- `.venv/bin/python -m pytest -q testes/test_seguranca.py testes/test_estado_store.py testes/test_rede_bitrix.py` → todos passam.
- Suíte inteira `.venv/bin/python -m pytest -q` → **1039 passed, 2 skipped, 0 failed** (com as edições paralelas de outras frentes em `triagem_grupo.py` e testes delas já no disco).

## Pendências para outras frentes

- **Orquestrador / CONTRATOS.md**: registrar `SegredoRecusado` (RuntimeError, não `SegredoAusente`, só com ESTRITO), `modo_estrito()` e a cobertura nova de `mascarar`.
- **Dono de `circuit_breaker.py`**: o comentário de `_RE_CHAVE` ("seguranca.mascarar não cobre par genérico token=/access_token=/auth=") ficou desatualizado; `_RE_CHAVE` pode ficar (cobre ainda `api_key`/`senha`/`password` em texto livre), só corrigir o comentário.
- **Dono de vigia-bot-triagem.py / esperar_bot_liberar.py**: com ESTRITO o fallback direto já não é alcançado pela recusa; se quiserem coerência total, pular o `open(<BASE>/vibecode.key)` quando `seguranca.modo_estrito()` (cobre o caso de `TRIAGEM_SEGREDOS_LEGADOS` customizado que exclua esse arquivo).
- **Plano de recuperação (outra equipe)**: trocar `dono="triagem"` por `dono="revisao"` em R1 (aplicação e rollback) e por `dono="identificacao"` no passo 2 de R2; remover as notas "sem dono declarado".
- **Frente da ponte/pintura**: consultar `cpf_confiavel is False` (pendência já citada no plano).

## Riscos

- Máscara nova pode esconder valor legítimo de um parâmetro chamado exatamente `token`/`auth` em log (ex.: `auth=webhook`). Aceito: é log; a lista é curta e o nome é exato.
- ESTRITO ligado antes do `chmod 600` para os jobs — é o comportamento pedido; operar na ordem acima.
