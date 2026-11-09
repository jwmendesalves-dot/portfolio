# Frente H2 — units reais em deploy/ + PII (branch mimo/auxiliares)

Data: 09/11/2026. Escopo: `deploy/` e `docs/remediacao/PLANO-IMPLANTACAO.md`, a partir
da coleta real do servidor (`coleta-triagem.txt`, 09/11/2026 17:38;
`SERVIDOR-COLETA-20261109.md` do repo de remediação). Sem commit (o orquestrador comita).

## 1. PII — dispensada pelo dono

A troca de CPFs por sintéticos foi **dispensada pelo dono** no meio da frente. Nenhum
arquivo foi alterado por esse motivo (`README.md`, `certidoes.py`,
`testes/test_pintar_linha.py` e `testes/test_telegram_batidas.py` estão como no HEAD).

## 2. Units reais → deploy/

### lote-fila (prioridade)
- **Antes (real):** 5 `ExecStart=` em cadeia num `Type=oneshot`, sem trava, como root.
  Com o código remediado (os scripts passam a devolver 1/2), o primeiro passo ≠ 0 corta os
  seguintes → a fila para (regressão no deploy).
- **Depois:** `ExecStart=/opt/triagem-bitrix/deploy/bin/lote-fila.sh` (único). O script
  roda os 5 passos sempre, `timeout` por passo, devolve o **pior** código. Mesmos argumentos
  da unit real, com 2 diferenças: passo 4 `--limite 30` (`CPF_LIMITE=0` volta ao real);
  passo 5 via `ponte-rodar.sh` (`PONTE_LIMITE=15 PONTE_DIAS=7`) → **mesma trava `fila.lock`
  da ponte**. Alternativa documentada: `ExecStart=/opt/triagem-bitrix/pipeline.sh`.
- Guardas: sai 2 se não houver `flock` ou se o `ponte-rodar.sh` instalado for o antigo (sem
  `fila.lock`).

### Tabela antes × depois
Está em `deploy/systemd/README.md` (uma linha por unit). Resumo:

| Unit | Mudança |
|---|---|
| triagem-lote-fila | 5 ExecStart → 1 (`lote-fila.sh`); trava `fila.lock` via ponte-rodar.sh; 900 s → 20 min |
| triagem-bitrix-ponte | mantém `ponte-rodar.sh`, `WorkingDirectory=/opt/agente-separador`, `append:ponte.log`, 900 s; sem flock na unit (o wrapper remediado trava `fila.lock`) |
| triagem-bitrix-certidoes | mantém `certidoes-rodar.sh`, `WorkingDirectory=/opt/agente-separador`, `append:certidoes.log`, 900 s; sem flock na unit |
| pintar-linha-triagem | mantém `/opt/qc-planilha/venv/bin/python … --aplicar`, `pintura.log`, 180 s; + `flock -n` |
| triagem-bitrix-bot-vigia | **renomeada** (era `triagem-bot-vigia`): `/bin/bash vigia_bot.sh`, `After=triagem-bitrix-bot.service` |
| triagem-bitrix-bot | ExecStart **não coletado** → cabeçalho "COLETAR ANTES DO DEPLOY"; fora da instalação em lote |
| triagem-bitrix-pergunta | inativo/TESTE → **manter desativado** (documentado no README, no bot e no checkpoint C6) |
| todas | `User=triagem` + drop-in `User=root` na fase 1, hardening S08, `OnFailure`; `After=triagem-bitrix.service` (não existe na coleta) trocado por `triagem-bitrix-bot.service` |

Os timers mantêm as cadências reais (10/5/5/2/2 min), registradas no cabeçalho de cada
`.timer`; a proposta usa `OnUnitInactiveSec`.

### PLANO-IMPLANTACAO.md
- Checkpoints C1–C10: `command -v flock`; `systemctl cat triagem-bitrix-bot.service`;
  `/etc/triagem-bitrix/{webhook_bitrix,vibecode_key}` 0600 antes do código novo;
  `manutencao/inventario_estado.py` antes e depois; `python3 -c "import pergunta_traco as p;
  p.carregar_config_do_bot()"`; desativar `triagem-bitrix-pergunta.timer`; duração do ciclo
  (tem de voltar a baixar após G-01); `chmod 600` em todos os segredos antes de
  `TRIAGEM_SEGREDOS_ESTRITO=1`.
- `fonte_leitura` continua `webhook` (48 h: 247/247 e 269/269).
- Fase 1: backup das units reais, substituição no lugar (mesmos nomes), ordem obrigatória
  código remediado + unit nova do lote-fila na mesma janela, rollback novo.
- Matriz de risco com 3 riscos novos.

### Outros ajustes
- `deploy/bin/verificar-ambiente.sh`: sonda `$BASE/fila.lock` (e a trava antiga da ponte).

## 3. Verificação (saída real)
- `bash -n`: OK em `deploy/bin/lote-fila.sh`, `alertar-falha.sh`, `verificar-ambiente.sh`.
- Teste funcional do `lote-fila.sh` com scripts falsos: passo 1 sai 1 e os passos 2–5
  rodam mesmo assim; os argumentos conferem; `pior saída: 1` → exit 1. Com wrapper antigo
  → exit 2.
- Units: revisão manual da sintaxe (`systemd-analyze` não existe no macOS — rodar
  `systemd-analyze verify` no servidor, Fase 1 passo 5).
- `.venv/bin/python -m pytest -q`: **14 failed, 816 passed in 7.63s**. Nenhuma falha vem
  desta frente (nenhum teste lê `deploy/`). São todas de ambiente:
  - `test_variaveis_contrato.py` (7) e `test_servico_lote.py` (3): leem `/opt/triagem-bitrix/estado.json`;
  - `test_telefones_fichinha.py` (1): `/opt/triagem-bitrix/estado.json`;
  - `test_ponte_fila.py` (1): `/opt/agente-separador/src/fila-jobs.js`;
  - `test_nome_regras.py` (1) e `test_pasta_fichinha.py` (1): `SystemExit: webhook do Bitrix nao encontrado`.

## 4. Pendências / riscos
- **O `ponte-rodar.sh` desta branch ainda é o antigo** (`/var/lock/triagem-bitrix-ponte.lock`).
  O `lote-fila.sh` e a unit da ponte esperam o remediado (`$BASE/fila.lock` +
  `TRIAGEM_FILA_LOCK_FD`, branch `remediacao`). Na integração, a versão remediada tem de
  prevalecer, senão o lote-fila recusa rodar (exit 2, de propósito).
- `README.md` (raiz), `RUNBOOK-OPERACIONAL.md`, `ARQUITETURA-CORRIGIDA.md` e `H-infra.md`
  ainda citam `/var/lock/triagem-bitrix-ponte.lock` e `triagem-bot-vigia`. Ficaram fora do
  escopo (o README estava congelado pela ordem do dono).
- Unit do bot: hipótese até o C2.
- Durante a inspeção, alguns valores de teste com formato de CPF apareceram na saída do
  terminal desta sessão (uma linha de `sed` mal mascarada e o traceback do pytest). Não
  foram gravados em nenhum arquivo nem neste relatório.
