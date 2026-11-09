# deploy/systemd/ — units e timers (OP-O4, S08) reescritos a partir das units reais

> **Status: PROPOSTA conferida com o servidor.** Base: coleta real de 09/11/2026 17:38
> (`servidor-triagem`, `/opt/triagem-bitrix`, `systemctl cat` de `/etc/systemd/system`) —
> resumo em `SERVIDOR-COLETA-20261109.md` (repo de remediação). **Exceção:**
> `triagem-bitrix-bot.service` — o ExecStart dele não foi coletado (**coletar antes do
> deploy**). `systemd-analyze verify` não roda no macOS — validar no servidor após a
> instalação (ver PLANO-IMPLANTACAO.md).

## Units reais encontradas (09/11/2026)

| Unit real | Estado | Proposta neste diretório |
|---|---|---|
| `triagem-lote-fila.{service,timer}` | ativo, 10 min | `triagem-lote-fila.*` (ExecStart único) |
| `triagem-bitrix-ponte.{service,timer}` | ativo, 5 min | `triagem-bitrix-ponte.*` |
| `triagem-bitrix-certidoes.{service,timer}` | ativo, 5 min | `triagem-bitrix-certidoes.*` |
| `pintar-linha-triagem.{service,timer}` | ativo, 2 min | `pintar-linha-triagem.*` |
| `triagem-bitrix-bot-vigia.{service,timer}` | ativo, 2 min | `triagem-bitrix-bot-vigia.*` (antes proposto como `triagem-bot-vigia` — renomeado para o nome real) |
| `triagem-bitrix-bot.service` | contínuo; **ExecStart não coletado** | `triagem-bitrix-bot.service` (hipótese — **coletar antes do deploy**) |
| `triagem-bitrix-pergunta.{service,timer}` | **inativo** ("TESTE", 1 min, `pergunta-rodar.sh`) | nenhuma — **manter desativado** (brigaria com o bot `--loop` pela trava; 2ª instância sai 75) |
| `triagem-bitrix.service` | **não coletada — pode existir** (as units reais da ponte e das certidões têm `After=triagem-bitrix.service`; candidata a coletor contínuo que grava o `estado.json`) | nenhuma — **coletar antes do deploy** (C2) junto com o bot; se existir, entra no C9, no restore (RUNBOOK §8) e no backup das units |
| — | não existe | `triagem-limpeza.*`, `triagem-alerta@.service` (novos) |

Todas as reais rodam **sem `User=`** (root) — S08 confirmado. Certidões e ponte têm
`After=triagem-bitrix.service`. A coleta só despejou units com timer: a ausência dela
**não está provada** (F2-09). As propostas usam `After=triagem-bitrix-bot.service`; se o C2
mostrar `triagem-bitrix.service`, acrescentá-la de volta ao `After=` (ordenação só — não
cria dependência) e tratá-la como escritora do estado.

## Antes × depois por unit

| Unit | ANTES (real) | DEPOIS (proposta) |
|---|---|---|
| `triagem-lote-fila` | **5 `ExecStart=` em cadeia** (`baixar_pendentes` → `reconciliar_estado` → `reconciliar_por_file_ids --aplicar --dias 7` → `completar_cpf_por_nome --aplicar` → `lote_para_fila --enviar --todos --limite 15`); um passo ≠ 0 corta os seguintes; sem trava; `TimeoutStartSec=900`; journal | **`ExecStart=/opt/triagem-bitrix/deploy/bin/lote-fila.sh`** — roda os 5 passos sempre, `timeout` por passo, devolve o **pior** código; passo 4 com `--limite 30` (`CPF_LIMITE=0` volta ao real); passo 5 via `ponte-rodar.sh` → trava **`fila.lock`** compartilhada com a ponte; `TimeoutStartSec=30min` (soma dos limites por passo 1680 s; passo 5 = 900 s); journal. `pipeline.sh` **não** é equivalente (manual: ponte com limite 4, sem timeout por passo) |
| `triagem-bitrix-ponte` | `ponte-rodar.sh`, `WorkingDirectory=/opt/agente-separador`, `append:ponte.log`, 900 s; trava antiga `/var/lock/triagem-bitrix-ponte.lock` (só a ponte) | mesmo ExecStart/WorkingDirectory/log/timeout; `ponte-rodar.sh` remediado trava **`/opt/triagem-bitrix/fila.lock`** (mesma do lote-fila e do `lote_para_fila.py`); sem flock na unit; `Environment=TRIAGEM_BASE=` |
| `triagem-bitrix-certidoes` | `certidoes-rodar.sh`, `WorkingDirectory=/opt/agente-separador`, `append:certidoes.log`, 900 s | mesmo; wrapper já trava `/var/lock/triagem-bitrix-certidoes.lock` e propaga exit (CE-05); log ganha rotação |
| `pintar-linha-triagem` | `/opt/qc-planilha/venv/bin/python pintar_linha.py --aplicar`, `append:pintura.log`, 180 s, sem trava | **mesmo Python da venv** e mesmo log/timeout; + `flock -n -E 0 /var/lock/pintar-linha-triagem.lock` (ocupado = pula com 0, sem alerta) |
| `triagem-bitrix-bot-vigia` | `/bin/bash vigia_bot.sh`, `After=triagem-bitrix-bot.service`; timer 3min/2min, `Requires=` | mesmo ExecStart/After; timer `OnUnitInactiveSec=2min`, sem `Requires=` |
| `triagem-bitrix-bot` | contínuo, **não coletado** | hipótese `pergunta_traco.py --ativo --loop 8`, `SuccessExitStatus=75`/`RestartPreventExitStatus=75`, **`StandardOutput=append:pergunta.log`** (o vigia mede o mtime desse arquivo) |
| `triagem-bitrix-pergunta` | inativo | **manter desativado** (`systemctl disable --now triagem-bitrix-pergunta.timer`) |
| todas | root, sem hardening, sem `OnFailure` | `User=triagem` (drop-in `migracao-fase1.conf` com `User=root` + `ProtectHome=read-only` na fase 1), hardening S08, `OnFailure=triagem-alerta@%n` |

Timers: os reais usam `OnUnitActiveSec` (conta do INÍCIO da rodada); a proposta usa
`OnUnitInactiveSec` (conta do FIM — rodada lenta atrasa a seguinte em vez de sobrepor)
com as mesmas cadências (10/5/5/2/2 min). Cada `.timer` registra os valores reais no
cabeçalho.

## Trava (flock) — regras

1. **`triagem-lote-fila` e `triagem-bitrix-ponte` compartilham `/opt/triagem-bitrix/fila.lock`**
   (`caminhos.em_base("fila.lock")`): o `ponte-rodar.sh` remediado adquire com
   `flock -n` e exporta `TRIAGEM_FILA_LOCK_FD` para o `lote_para_fila.py` não travar de
   novo. O lote-fila só entra na fila pelo `ponte-rodar.sh` (passo 5). Quem não consegue
   a trava **pula a rodada** (exit 0) — nunca espera. A trava antiga
   `/var/lock/triagem-bitrix-ponte.lock` deixa de ser usada.
2. **Pré-requisito:** o `lote-fila.sh` recusa rodar (exit 2) se `flock` não existir ou se o
   `ponte-rodar.sh` instalado não for o remediado (sem `fila.lock`) — evita a combinação
   "unit nova + wrapper antigo" com travas diferentes.
3. **Ordem de aquisição:** FILA (shell, `ponte-rodar.sh`) → ESTADO (curta, dentro do
   Python, `estado_store`). Nunca estado → fila. Nenhum processo segura duas travas de shell.
4. **Sem lock aninhado sobre o MESMO arquivo** (PT-02): as units chamam **ou** o wrapper
   self-locking (`ponte-rodar.sh`, `certidoes-rodar.sh`, `pergunta_traco.py`) **ou**
   `flock` inline + Python direto (`pintar-linha`) — nunca wrapper dentro de flock.
   `pergunta-rodar.sh` (rodada única) e `triagem-bitrix-bot` (`--loop`) nunca juntos.
5. **Locks órfãos:** arquivo de lock que sobrou não trava nada. `deploy/bin/verificar-ambiente.sh`
   sonda `fila.lock`, a trava antiga, certidões, pintar-linha, estado e bot.

## Hardening comum (S08)

- `User=triagem` / `Group=triagem` (fase 1 com o drop-in `migracao-fase1.conf` — ver PLANO-IMPLANTACAO.md).
- `UMask=0077`, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict`,
  `ProtectHome=yes` **só na fase 2** — na fase 1 o drop-in põe `read-only`, senão o node
  em `/root/.hermes/node` (`TRIAGEM_NODE`) e os segredos legados em `/root` somem até para
  root (F2-02), `ReadWritePaths=/opt/triagem-bitrix /opt/agente-separador/data /var/lock`
  (`/opt/qc-planilha` e `/opt/agente-separador` fora de `data/` ficam só leitura).
- `EnvironmentFile=/etc/triagem-bitrix/env` (0600 root:root).
- `OnFailure=triagem-alerta@%n.service` em todas.
- Vigia na fase 2: regra polkit `deploy/polkit/50-triagem-bitrix-bot.rules` (o sudoers
  antigo foi removido: `NoNewPrivileges=yes` impede o sudo de elevar e o `vigia_bot.sh`
  não chama sudo — F2-08).
- Logs: ponte, certidões e pintar-linha mantêm o `append:` real (rotação `copytruncate`
  em `deploy/logrotate/`); lote-fila, vigia e bot no journal.

## Instalação

A sequência completa (com C0 — coleta e integração do OCR/TypeSafe do servidor, bloqueante —
backup do código e rollback) está em `docs/remediacao/PLANO-IMPLANTACAO.md`, Fase 1.
**Não há bloco curto aqui de propósito:** instalar units sem parar os timers, sem os
drop-ins e ligando todos os timers de uma vez deixou a janela quebrada (F2-03). Resumo da
ordem: parar os 6 timers → esperar os services inativos → backup (código + units +
`estado.json`) → copiar o código (lista explícita, depois do diff do C0) → drop-ins
`migracao.conf` (inclusive `triagem-alerta@.service.d/`) → instalar units (menos o bot) →
`daemon-reload` → `systemd-analyze verify` → `start` manual de cada service → `enable --now`
de **um** timer por vez.
