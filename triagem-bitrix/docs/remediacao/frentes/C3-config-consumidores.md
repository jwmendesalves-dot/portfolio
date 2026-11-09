# Frente C3 (onda 4) — config dos consumidores (F1-07 + ConfigInvalida + autoteste)

Status: **concluída**. Arquivos alterados: `triagem_grupo.py` (só `CONFIG_PADRAO`, `validar_config` e o autoteste `testar_sinais`), `pergunta_traco.py`, `checar_midias.py`, `testes/test_pergunta_traco_envio.py`, `testes/test_fase1_config.py`. Arquivo novo: `testes/test_config_consumidores.py`.

## F1-07 · CONFIG_PADRAO × uso real · **corrigido (modo); fonte com decisão registrada**
- **`modo_traco` padrão: `grupo_autor` → `delimitador`.** É o que o `config.json` usa e o único modo que o bot do traço implementa (`relatorio.decidir_resposta`: todo traço fecha o bloco de cima e abre o de baixo). O padrão só vale quando a chave está **ausente**. Valor desconhecido continua `ConfigInvalida` (erro visível, código 3).
- **`fonte_leitura` padrão: continua `api`, de propósito.** Comentário no código e no `_comentario` do padrão.
  - Motivo: o caminho `api` passa por `_ler_mensagens_com_breaker`, com failover para o webhook. O caminho `webhook` (`ciclo`, `triagem_grupo.py` ~3267) **não tem failover para a API**, que é pendência da C2b.
  - Um padrão `webhook` deixaria instalação nova ou config sem a chave sem failover.
  - Teste feito: com o padrão `webhook`, 3 testes de `test_triagem_persistencia.py` (outro dono) quebraram, porque o ciclo tentou descobrir o webhook.
  - A produção define `webhook` explicitamente, então não muda nada lá.
  - Trocar o padrão para `webhook` só depois que o failover webhook→API existir.
- `validar_config`: `fonte_leitura: null` explícito agora usa `CONFIG_PADRAO["fonte_leitura"]` em vez do literal `"api"`. Mesmo valor hoje, fonte única.
- **pergunta_traco usa o mesmo modo da esteira:** a função nova `carregar_config_do_bot()` lê `tg.carregar_config()` e confere `tg.modo_traco(conf)` contra `MODOS_DO_BOT = ("delimitador",)`.
  - Modo diferente gera `ConfigInvalida`: o bot não roda com avisos que contradiriam o agrupamento real.
  - `--registrar-desfecho`/`--resumo` não exigem o modo, porque não leem traço.

## ConfigInvalida nos consumidores · **corrigido** (pergunta_traco, checar_midias)
- **`pergunta_traco.py`:**
  - Rodada única, `--testar`, `--para`, `--resumo` e `--registrar-desfecho` imprimem `🚨 pergunta_traco NÃO rodou: config inválido — … (código 3)` (com máscara) e retornam `SAIDA_CONFIG = 3`. Sem traceback e sem nada lido ou enviado.
  - **`--loop`** não sai, porque o systemd reiniciaria em laço rápido. `esperar_config_valida()` avisa e relê o config com pausa crescente de 60 → 120 → 240 → 480 → 900 s (teto) até ficar válido, e só então entra nas rodadas.
  - A trava de instância já foi tomada antes, então não aparece segunda instância.
  - Docstring de códigos de saída atualizada.
- **`checar_midias.py`:** `ConfigInvalida` imprime `🚨 config.json inválido — nada lido do grupo: …` e retorna `SAIDA_CONFIG = 3`, sem ler a chave e sem traceback. Códigos de saída documentados.
- **`telegram_grupo.py` não é meu:** pendência abaixo.

## `--testar-sinais` · 2 falhas anteriores · **autoteste desatualizado (fixture); expectativa ajustada**
- Casos: "barreira: documento de outra pessoa NÃO entra no bloco do 1º" e "barreira: o lote do 2º fecha pelo traço dele".
- **Causa:** o cabeçalho do 2º cliente na fixture era `= JOAO` (4 letras). A regra vigente de nome (`nome_regras.parece_ruido`: 5..80 caracteres, mesmo na camada de sinal) recusa esse nome. Sem nome não há cabeçalho (`tem_nome_cabecalho`), e o traço final não fecha o kit do 2º: abre um lote vazio.
- **A barreira em si funcionava.** Rodei as duas variantes nos dois modos:
  - os CPFs ficaram em lotes separados, sem CONFLITO e com `file_ids` corretos;
  - com `= JOAO` saem 3 lotes: MARIA, JOAO fechado por janela e um lote vazio ABERTO;
  - com `= JOAO PEDRO` saem 2 lotes, e o 2º fica `FECHADO_PRONTO` pelo traço.
- A expectativa "2 lotes / fecha pelo traço" contradizia a regra atual de nome. Troquei a fixture para `= JOAO PEDRO`, que é o formato do cabeçalho real do grupo. O comentário antigo ("NÃO devem ser consertadas") foi substituído pela justificativa.
- **Lacuna registrada (não corrigida, porque é agrupamento):** cabeçalho com `= CPF` de DV válido e nome curto ou ausente não conta como marcador do `traco_no_fim_fecha`. Fica como `xfail(strict=True)` em `test_cabecalho_com_cpf_valido_e_nome_curto_fecha_pelo_traco`. Não há mistura de documentos, só o lote vazio e o fechamento por janela. A decisão é do dono do agrupamento/`nome_regras`.
- **Efeito colateral da troca do padrão:** o `testar_sinais` usava `dict(CONFIG_PADRAO)` e seus casos-base foram escritos para `grupo_autor`. Com o padrão `delimitador` deram 22 falhas. Fixei `cfg["modo_traco"] = "grupo_autor"` no autoteste, com comentário. Observação: **o autoteste embutido não exercita o modo de produção.** `delimitador` é coberto em `testes/test_traco_delimitador.py`.

## Testes
`testes/test_config_consumidores.py` (16 testes + 1 xfail estrito). Mudanças em arquivos meus já existentes:
- `test_pergunta_traco_envio.py`: o conf mockado traz `modo_traco: delimitador`, como o `carregar_config` real entrega;
- `test_fase1_config.py`: o import do fixture compartilhado virou atribuição, para o pyflakes ficar limpo.

Prova no código anterior: cópia temporária com `git show HEAD:{triagem_grupo,pergunta_traco,checar_midias}.py` mais os testes novos.
```
FAILED test_padrao_do_modo_traco_e_o_do_uso_real
FAILED test_config_sem_a_chave_modo_traco_usa_delimitador
FAILED test_config_ausente_e_criado_com_delimitador
FAILED test_pergunta_traco_config_invalido_sai_3_sem_traceback
FAILED test_pergunta_traco_modo_diferente_da_esteira_sai_3
FAILED test_pergunta_traco_loop_com_config_invalido_pausa_crescente_sem_sair
FAILED test_backoff_do_config_tem_teto
FAILED test_checar_midias_config_invalido_sai_3_sem_traceback
FAILED test_autoteste_testar_sinais_sem_falhas
9 failed, 47 passed, 1 xfailed
HEAD --testar-sinais: casos=89 falhas=2
```
Os que passam no HEAD são guardas de não-regressão: modo explícito preservado, modo inválido continua erro, config real roda, desfecho sem exigir modo, barreira com cabeçalho completo.

Código novo:
```
$ .venv/bin/python -m pyflakes triagem_grupo.py pergunta_traco.py checar_midias.py testes/test_config_consumidores.py testes/test_pergunta_traco_envio.py testes/test_fase1_config.py
pyflakes=0
$ .venv/bin/python triagem_grupo.py --testar-sinais   -> casos=89 falhas=0
$ .venv/bin/python -m pytest -q
1093 passed, 2 skipped, 3 xfailed in 15.10s
```
Numa execução intermediária, `testes/test_cenario_integral.py` (frente G, em paralelo) falhou. Também falhava com o `triagem_grupo.py` do HEAD, então não foi causado por esta frente. Na execução final passou.

## Mudanças de comportamento para o deploy
1. Config **sem** `modo_traco` passa a agrupar em `delimitador`, não em `grupo_autor`. A produção já define `delimitador`: sem efeito lá.
2. Se `config.json` não existir, ele é criado com `modo_traco: delimitador`.
3. `pergunta_traco` **recusa rodar** (código 3; no `--loop`, pausa e tenta de novo) quando a esteira está num modo diferente de `delimitador`. Se alguém mudar o config para `grupo_autor`, o bot para de falar no grupo e avisa no log.
4. `pergunta_traco`/`checar_midias` com config inválido retornam código 3 com mensagem, em vez de traceback (código 1). No `--loop` do bot, aguardam de 60 s a 15 min entre tentativas.

## Pendências para outras frentes
- **`telegram_grupo.py`** (outra equipe), linha ~415: `cfg.update(tg.carregar_config())` sem tratar `ConfigInvalida`, o que vira traceback e, sob systemd, reinício em laço. Tratar como em `pergunta_traco`: código 3 fora do loop, e backoff com releitura no `--loop`.
- **`triagem_grupo.py` (dono do agrupamento):** failover webhook→API no caminho `fonte_leitura=webhook`. Depois disso, trocar `CONFIG_PADRAO["fonte_leitura"]` para `webhook` e ajustar o fixture de `testes/test_triagem_persistencia.py`: `dict(tg.CONFIG_PADRAO, fonte_leitura="api")` ou mockar o webhook.
- **`triagem_grupo.py`/`nome_regras.py`:** decidir se cabeçalho com CPF de DV válido e nome < 5 letras (ou sem nome) deve contar como marcador do `traco_no_fim_fecha`. Quando decidido, o xfail estrito vai acusar XPASS.
- **`triagem_grupo.py`:** `ciclo` (~3267) usa `conf.get("fonte_leitura") or "api"`. Com `null` explícito dá o mesmo que o padrão hoje, mas o literal deveria vir de `CONFIG_PADRAO`. Fora do meu escopo.
- **`pergunta_traco.py`:** ainda lê sempre pela API (`buscar_mensagens_api`), não por `fonte_leitura`. A medição de 09/11 deu as mesmas mensagens nas duas fontes; não mexi.
- Mover `testar_sinais` para `testes/` e cobrir também o modo `delimitador` lá.

## O que precisa do servidor
- Antes do deploy: `python3 -c "import pergunta_traco as p; p.carregar_config_do_bot()"` para confirmar que o config real passa na conferência de modo. Não serve `--resumo`, porque ele não confere o modo.
