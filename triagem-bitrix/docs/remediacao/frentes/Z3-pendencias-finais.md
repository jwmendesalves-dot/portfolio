# Frente Z3: pendências finais pós-integração

Status: **concluída**. Sem commit; o orquestrador comita.

## Arquivos
- Alterados: `telegram_grupo.py`, `lote_para_job.py`, `circuit_breaker.py` (só comentário), `docs/remediacao/CONTRATOS.md`, `README.md` (2 linhas), `docs/remediacao/RUNBOOK-OPERACIONAL.md`, `docs/remediacao/ARQUITETURA-CORRIGIDA.md`, `docs/remediacao/frentes/H-infra.md`.
- Novo: `testes/test_telegram_config_invalida.py` (3 testes).
- CPFs existentes: nenhum foi tocado.

## (1) telegram_grupo · ConfigInvalida · **corrigido**
- **Causa:** `main` fazia `cfg.update(tg.carregar_config())` sem `try`. Um `ConfigInvalida` virava traceback (código 1) e, com `--loop` sob systemd, reinício em laço. O traceback do HEAD também mostrava o texto da exceção **sem máscara** (no teste, `token=…` saiu em claro).
- **O que mudou:** criei `carregar_config_valida(loop, dormir=None)`, no mesmo padrão de `pergunta_traco.esperar_config_valida`:
  - fora do loop: imprime `🚨 telegram_grupo NÃO rodou: config inválido — <mascarado> … (código 3)` e `main` devolve `SAIDA_CONFIG = 3`, sem ler o token e sem chamar a API;
  - com `--loop`: não sai. Relê o config com pausa de 60 → 120 → 240 → 480 → 900 s (teto) e segue quando ele fica válido.
- **Testes:** `test_uma_vez_config_invalido_sai_3_mascarado_sem_api`, `test_loop_config_invalido_pausa_crescente_e_segue_quando_valido`, `test_backoff_tem_teto`.
- **Prova no HEAD:** numa cópia temporária com `git show HEAD:telegram_grupo.py`, os 3 testes falham. Dois com `triagem_grupo.ConfigInvalida: config.json ilegível (token=…)` não tratado e um com `AttributeError` (a função não existe).
- **Mudança de comportamento:** config inválido agora sai com código 3 em vez de traceback com código 1. No `--loop`, o processo fica vivo esperando o config ser corrigido.

## (2) CONTRATOS.md · **atualizado**
Conferi cada item no código e nos relatórios Z2, C2b, C3 e C4. Registrei:
- `seguranca.SegredoRecusado`: só com ESTRITO, não herda de `SegredoAusente`.
- `seguranca.modo_estrito()`.
- Cobertura nova de `mascarar`: `token=`, `access_token=`, `auth=`, `refresh_token=`, `client_secret=` em texto livre e em chave JSON, só com nome exato.
- Seção nova `triagem_grupo.ConfigInvalida`: código 3, e backoff no `--loop` para todos os consumidores.
- Seção nova de campo de lote: `cpfs_invalidos`, dono `triagem`.
- `bitrix_cliente.listar` não corta mais quando a página tem menos de 50 itens.
- `LimitadorHost.aguardar`: o `timeout` também cobre o flock (`TempoEsgotado`).

## (3) circuit_breaker `_RE_CHAVE` · **comentário atualizado**
Novo comentário: `seguranca.mascarar` já cobre `token=` e os demais pares. A camada própria do disjuntor fica como defesa em profundidade, porque também pega `x-api-key`, `api_key`, `senha` e `password`. A regex não mudou.

## (4) lote_para_job · pyflakes · **resto morto removido, não é bug**
- `marcar` (lambda de checkbox) estava sem uso. As marcações `trabalha_*`/`bolsa_*` são calculadas em linha e ficam iguais.
- `tel` (lambda) estava sem uso, substituído por `_linhas_de_telefone`. Ao remover `tel`, a variável `fones`, que só existia para ela, também ficou sem uso e foi removida.
- `alvo = _chave(txt).upper()` em `achar_ficha_em_texto` estava sem uso: a condição recalcula `_sem_acento(txt).upper()`. Não mudei para `alvo`. Isso deixaria o casamento mais tolerante (`_chave` troca pontuação e quebra de linha por espaço) e seria mudança de comportamento sem pedido. Se o dono quiser essa tolerância, é melhoria separada.
- Nenhum comportamento mudou: as 3 linhas eram atribuições puras.

## (5) Nomes e trava antigos nos docs · **corrigido**
- `triagem-bot-vigia` → `triagem-bitrix-bot-vigia` em README.md e ARQUITETURA-CORRIGIDA.md (diagrama e §bot).
- `/var/lock/triagem-bitrix-ponte.lock` → `$BASE/fila.lock` em README.md, ARQUITETURA-CORRIGIDA.md, H-infra.md e RUNBOOK-OPERACIONAL.md.
- **RUNBOOK §2 (correção além da troca de nome):** os comandos manuais envolviam `lote-fila.sh` e a ponte num `flock` externo. Com a trava nova, isso é o lock aninhado do PT-02 (o próprio `lote-fila.sh` avisa "NUNCA envolva este script num flock de fila.lock"). Agora o runbook diz para rodar direto `sudo …/deploy/bin/lote-fila.sh` e `sudo PONTE_LIMITE=4 PONTE_DIAS=7 …/ponte-rodar.sh`, que tomam a trava sozinhos. A verificação "livre?" passou a usar `/opt/triagem-bitrix/fila.lock`.
- Resultado do `grep -rn -E "triagem-bot-vigia|triagem-bitrix-ponte\.lock"` (sem .venv/.git): só sobram referências **históricas ou intencionais**:
  - `deploy/systemd/README.md` e o comentário da unit vigia ("antes proposto…", "trava antiga…");
  - `deploy/bin/verificar-ambiente.sh:156`, que confere se a trava ANTIGA saiu de uso;
  - `H2-ajustes-units-pii.md`, relatório histórico;
  - `H-infra.md:181`, como "antes …";
  - `docs/auditoria/*`, a auditoria original.
- **Nenhuma referência ativa.**

## Evidência
```
$ .venv/bin/python -m pyflakes telegram_grupo.py lote_para_job.py circuit_breaker.py testes/test_telegram_config_invalida.py
pyflakes_rc=0
$ .venv/bin/python -m pytest -q
1197 passed, 2 skipped, 1 xfailed in 17.35s
```
Antes: 1194 passed. Os 3 testes a mais são os novos.

## Pendências / riscos
- Os docs de operação agora dependem do `ponte-rodar.sh` remediado (com `fila.lock`), que é o desta branch. No servidor, implante o código antes de seguir o runbook novo.
