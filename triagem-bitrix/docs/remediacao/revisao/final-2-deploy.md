# Revisão final independente — foco 2: deploy/infra (branch `remediacao`, HEAD `ce6cc54`)

Revisor: subagente (só leitura; este relatório é o único arquivo criado no repo).
Escopo: `deploy/bin/*.sh`, `ponte-rodar.sh`, `pipeline.sh`, `certidoes-rodar.sh`, `deploy/systemd/*`
(+ README), `PLANO-IMPLANTACAO.md`, `RUNBOOK-OPERACIONAL.md`, comparados com as units reais em
`SERVIDOR-COLETA-20261109.md` e `coleta-triagem.txt` (09/11/2026 17:38).

Prova: os wrappers rodaram em `~/.hermes/cache/scratch/revfinal2/` (`t1.sh`, `t2.sh`) com scripts
Python falsos (código de saída/sleep controlados por variável). O macOS não tem `flock` nem `timeout`:
os dois foram simulados — `flock` com **flock(2) real** (`fcntl.flock`, mesma semântica de descrição de
arquivo compartilhada) e `timeout` com novo grupo de processos + SIGTERM no grupo + saída 124 (como o
GNU). `_fd_trava_herdada` foi copiada literalmente de `lote_para_fila.py:220-234`.

## Veredito

**NÃO IMPLANTAR como está.** Os wrappers estão corretos: trava única `fila.lock` sem aninhamento,
todos os passos rodam, o pior código propaga, o timeout não deixa processo órfão nem trava presa.
Quatro problemas impedem o deploy: (1) o PLANO ignora o bloqueio do OCR/TypeSafe (§5 da coleta);
(2) o hardening da Fase 1 (`ProtectHome=yes`) esconde `/root/.hermes/node`, e a ponte e as certidões
param de enfileirar; (3) o README instala as units sem os drop-ins `User=root` e liga todos os timers
de uma vez; (4) o rollback não cobre o código. Correções pequenas e localizadas; nenhuma mexe na lógica.

## Provas executadas (resumo)

| # | Cenário | Resultado |
|---|---|---|
| T1 | `lote-fila.sh`, passo1 rc=1, passo3 rc=2 | os 5 passos rodam; `exit=2`; passo 5 com trava `herdada` ✔ |
| T2 | ponte segurando `fila.lock` + lote-fila | passo 5 "pulando", `exit=0`, ponte termina normal ✔ |
| T3 | lote-fila dentro de `flock -n fila.lock` (anti-padrão PT-02) | ponte **pula sempre**, `lote_para_fila` 0 execuções — confirma o risco. **Nenhuma unit/runbook faz isso** ✔ |
| T4 | `T_FILA=2`, python dorme 5 s | `exit=124`, sem python órfão, `fila.lock` livre ✔ |
| T5 | `lote_para_fila` rc=1 | ponte `exit=1`, lote-fila `exit=1` ✔ |
| T6 | `pipeline.sh`, rc=1 no passo2, rc=3 no final | todos rodam, `exit=3` (PIPESTATUS ok); ponte com `--limite 4` |
| T7 | sem `timeout` no PATH | os 5 passos `saída 127`, nada roda, `exit=127` (o C1 cobre) |
| T8 | `ponte-rodar.sh` antigo instalado | lote-fila recusa, `exit=2` com mensagem ✔ |
| T9 | `certidoes-rodar.sh` rc=3 / trava ocupada | `exit=3` / `exit=0` ✔ (`date -Is` só falha no macOS; GNU ok) |
| T10 | `flock -n` ocupado (pintar-linha) | `exit=1` → unit `failed` + alerta |
| A2 | `alertar-falha.sh` com `journalctl` saindo 1 | morre com `exit=1` **antes** do `ALERTA_CMD` — alerta perdida |

## Achados

### F2-01 — CRÍTICO — PLANO não trata o OCR/TypeSafe do servidor como bloqueio
- **Onde:** `PLANO-IMPLANTACAO.md:9-12` ("Falta **um** item: o ExecStart do bot") × `SERVIDOR-COLETA-20261109.md:39`.
- **Prova:** `grep -i "ocr\|typesafe" PLANO-IMPLANTACAO.md` → nada. `ls ocr_documento.py typesafe.py` → não existem no repo.
  O servidor tem `ocr_documento.py`, `typesafe.py`, `typesafe_smoke.py`, o bloco "OCR (Indra)" em
  `lote_para_fila.py` e o bloco `typesafe` no `config.json`. Copiar esta branch por cima apaga a
  barreira por OCR (o CPF impresso decide o dono; sem CPF vai para `_QUARENTENA-docs-de-outros/`): passam a
  entrar na fila documentos que hoje ficam na quarentena. "Nenhuma mudança em config.json" (`:20-22`)
  apaga também o bloco `typesafe` do servidor se o arquivo for copiado.
- **Correção:** novo pré-requisito/checkpoint C0 **bloqueante**: coletar os 4 arquivos + `config.json`
  do servidor, integrar na branch (com teste) e conferir com `diff` arquivo a arquivo entre o servidor e a
  branch antes de copiar; deploy por lista explícita de arquivos, nunca `rsync --delete`/cópia do diretório inteiro.
  Com o OCR, o passo 5 fica mais lento: revisar o F2-07.

### F2-02 — CRÍTICO — `ProtectHome=yes` na Fase 1 quebra node e segredos legados em `/root`
- **Onde:** todas as units (`triagem-bitrix-ponte.service:39`, `triagem-lote-fila.service:46`,
  `triagem-bitrix-certidoes.service:36`, …) × `lote_para_fila.py:85`, `certidoes.py:64`
  (`NODE = TRIAGEM_NODE or "/root/.hermes/node/bin/node"`), `seguranca.py:294-297` (legados em `/root/...`).
- **Problema:** o próprio PLANO (`:51-54`) mantém root na Fase 1 *porque* os jobs dependem de
  `/root/.hermes/node` e `/root/.vibecode`. Só que `ProtectHome=yes` torna `/root` inacessível **inclusive
  para root**. `gravar_na_fila` (node) falha → a ponte e o passo 5 do lote-fila não enfileiram nada; as
  certidões (node) também quebram. `TRIAGEM_NODE` não aparece em nenhum doc/env de `deploy/`.
- **Reprodução (servidor):** `systemd-run -p ProtectHome=yes --wait -P ls /root/.hermes/node/bin/node`
  → `Permission denied`/vazio.
- **Correção:** na Fase 1, `ProtectHome=read-only` (ou `ProtectHome=no` no drop-in `migracao.conf`) **ou**
  copiar o node para `/opt/triagem-bitrix/node/` e pôr `TRIAGEM_NODE=` no `/etc/triagem-bitrix/env`
  antes de ativar. Acrescentar ao C7: a rodada manual da ponte precisa mostrar job **criado** na fila, não só `exit 0`.

### F2-03 — ALTO — o bloco de instalação do README deixa a janela quebrada
- **Onde:** `deploy/systemd/README.md:78-92`; o PLANO (`:63`) manda seguir o README.
- **Problema:** (a) o bloco não cria os drop-ins `User=root`, então as units sobem com `User=triagem`. Como a
  Fase 0.1 deixa `/opt/triagem-bitrix` com 0700 root, o resultado é `200/CHDIR` ou `EACCES` em tudo,
  e o `OnFailure` também roda como `triagem`. (b) `enable --now` dos 6 timers de uma vez contradiz o
  PLANO `:69-70` ("um a um com rodada manual"). (c) Não há passo "parar timers e esperar os
  services ficarem inativos" antes de copiar o código. Uma rodada antiga do lote-fila (5 ExecStart) ou da ponte
  (trava antiga `/var/lock/triagem-bitrix-ponte.lock`) pode estar no meio quando o código ou a trava mudam.
  (d) O drop-in do template `triagem-alerta@.service` precisa ir em `triagem-alerta@.service.d/`, e isso não está escrito.
- **Correção:** sequência única no PLANO e no README: `systemctl stop <6 timers>` → esperar
  `systemctl is-active` = inactive em cada service → backup do código e das units → copiar o código → criar os
  drop-ins (incluindo `triagem-alerta@.service.d/`) → instalar as units → `daemon-reload` →
  `systemd-analyze verify` → `start` manual de cada service → `enable --now` de um timer por vez.

### F2-04 — ALTO — rollback não executável para o código
- **Onde:** `PLANO-IMPLANTACAO.md:56, 75-77`.
- **Problema:** o backup cobre só `/etc/systemd/system`. O código de `/opt/triagem-bitrix` (que hoje tem
  o OCR, ver F2-01) não tem backup. O próprio plano diz que a unit antiga não pode voltar com código novo,
  mas não diz como voltar o código. O rollback também não desliga `triagem-limpeza.timer` (novo, roda
  `--aplicar`) e não diz se o código antigo lê o estado gravado pelo novo (`backups-estado/`, `f1_regiao`).
- **Correção:** passo 1 da Fase 1 = `tar czf <ata>/opt-triagem-$(date +%F).tgz /opt/triagem-bitrix
  --exclude=backups* --exclude=*.log` (0600) + `cp -a estado.json` com a trava; rollback = parar os
  timers, restaurar o tar, restaurar as units, `disable --now triagem-limpeza.timer`, `daemon-reload`,
  religar os timers. Registrar se o estado novo é legível pelo código antigo (ou restaurar o `estado.json` do C4).

### F2-05 — ALTO — unit hipotética do bot não grava `pergunta.log` → vigia em laço de restart
- **Onde:** `deploy/systemd/triagem-bitrix-bot.service:34` (sem `StandardOutput=`) × `vigia_bot.sh:19-38`.
- **Prova:** `pergunta_traco.py` não abre `pergunta.log` (grep: o log só existe pela
  redireção `>> pergunta.log` em `pergunta-rodar.sh:14`). Com `ExecStart=python3 … --loop 8` e saída no
  journal, o mtime do `pergunta.log` congela → o vigia reinicia o bot a cada 3–5 min, sem fim.
- **Correção:** `StandardOutput=append:/opt/triagem-bitrix/pergunta.log` +
  `StandardError=append:…` na proposta. No C2, comparar também `StandardOutput`/redireção da unit real,
  não só ExecStart/WorkingDirectory/Restart. (Hoje o PLANO segura este arquivo até o C2; o risco é instalar depois sem ver isto.)

### F2-06 — MÉDIO — `alertar-falha.sh` sem bit de execução e alerta perdida por `set -e`
- **Onde:** `git ls-files -s`: `deploy/bin/alertar-falha.sh` = **100644** (e
  `verificar-ambiente.sh` = 100644, chamado direto em `RUNBOOK:13`, `PLANO:43`). `alertar-falha.sh:10,21`.
- **Problema:** (a) `ExecStart=/opt/…/alertar-falha.sh %i` sem `+x` → `203/EXEC`: nenhuma
  alerta funciona. (b) Prova A2: se `journalctl` sair ≠ 0, o `N=$(journalctl … | wc -l)` com
  `pipefail`+`set -e` encerra o script antes do `ALERTA_CMD`.
- **Correção:** `git update-index --chmod=+x deploy/bin/alertar-falha.sh deploy/bin/verificar-ambiente.sh`
  (ou `ExecStart=/bin/bash …`); trocar a linha 21 por
  `N=$(journalctl … 2>/dev/null | wc -l | tr -d ' ' || true)` e chamar o `ALERTA_CMD` antes da coleta de contexto.

### F2-07 — MÉDIO — timeouts do passo 5 incoerentes com a ponte
- **Onde:** `lote-fila.sh:63` (`T_FILA=300`, `--limite 15`) × `triagem-bitrix-ponte.service:27`
  (900 s, `--limite 4`) × `lote_para_fila.py:87,206,652` (cada node até 180 s: planilha + enfileirar).
- **Problema:** um caminho normal com os dois node lentos (360 s) já passa de 300 s, e com 15 lotes
  e o OCR do servidor (F2-01) piora. O `timeout` mata o processo no meio do `gravar_na_fila`, quando a fila
  do painel já foi gravada mas `fila-enviados.json` ainda não (T4 prova o kill; a idempotência depende do
  uuid determinístico, que esta revisão não verificou). A soma dos passos (1080 s) cabe em `TimeoutStartSec=20min` ✔.
- **Correção:** `T_FILA=600` (soma 1380 s → `TimeoutStartSec=25min`) ou `FILA_LIMITE` menor; confirmar
  que reenviar após um kill não duplica job (teste com kill entre o node e o CONTROLE).

### F2-08 — MÉDIO — sudoers da Fase 2 é incompatível com `NoNewPrivileges=yes`
- **Onde:** `triagem-bitrix-bot-vigia.service:7-8,25` + `deploy/sudoers/triagem-bitrix` + `PLANO:89-90`.
- **Problema:** com `NoNewPrivileges=yes` o `sudo` (setuid) não eleva. Com `User=triagem`, o vigia não
  consegue reiniciar o bot nem com o sudoers. Além disso, `vigia_bot.sh:38` chama `systemctl` sem `sudo`.
- **Correção:** para o vigia, regra polkit (`org.freedesktop.systemd1.manage-units` só para
  `triagem-bitrix-bot.service` e o usuário `triagem`) em vez de sudoers, ou `WatchdogSec`. Se ficar o sudoers,
  tirar `NoNewPrivileges` só dessa unit e documentar.

### F2-09 — MÉDIO — `triagem-bitrix.service` dado como inexistente sem prova
- **Onde:** `README.md:23-25`; units da ponte e das certidões trocam `After=triagem-bitrix.service` por `-bot`.
- **Problema:** a coleta só despejou as units que têm timer. A ausência de `triagem-bitrix.service` não está
  provada, e ela pode ser o coletor contínuo (quem grava o `estado.json`). Se existir, fica fora do hardening,
  do C9 e do restore (`RUNBOOK §8` para só os timers do lote-fila e da ponte).
- **Correção:** acrescentar ao C2 `systemctl list-units --all 'triagem*' 'pintar*'` e
  `systemctl list-unit-files 'triagem*'`. No RUNBOOK §8, parar todo escritor do estado (bot/coletor) antes do restore.

### F2-10 — BAIXO
- `pintar-linha-triagem.service:23`, `triagem-limpeza.service:23`: `flock -n`/`-w 300` ocupado sai 1
  (T10) → unit `failed` + alerta. Isso contradiz o comentário "desiste e tenta amanhã". Usar `flock -E 0 -n` (pintar)
  e `-E 75` + `SuccessExitStatus=75` (limpeza).
- `README.md:31` e `triagem-lote-fila.service:13`: `pipeline.sh` **não** é "alternativa equivalente":
  ponte com `--limite 4` (T6) × 15, e sem timeout por passo.
- `RUNBOOK §9:132-134`: obsoleto ("editar o ExecStart --limite 4"). O ExecStart é o `ponte-rodar.sh`; o
  limite vem de `PONTE_LIMITE` (`Environment=` na unit) e do `FILA_LIMITE` do lote-fila.
- `lote-fila.sh:38`: checa `flock` mas não `timeout`. Sem ele, 5×127 (T7). O C1 cobre; uma linha
  `command -v timeout || exit 2` deixa a falha explícita.
- `alertar-falha.sh` com `User=triagem`: o `journalctl -u` não mostra nada sem o grupo `systemd-journal`/`adm`.

## Pontos conferidos e OK
- Sem trava aninhada: nenhuma unit envolve `ponte-rodar.sh`/`lote-fila.sh`/`certidoes-rodar.sh` em
  `flock`. A trava do shell (`$BASE/fila.lock`) é o mesmo inode que `lote_para_fila` confere (`TRAVA_RODADA+".lock"`).
  O fd 9 atravessa `timeout` e `env` (T1/T2: `herdada`).
- `pintar-linha` mantém `/opt/qc-planilha/venv/bin/python` (igual à unit real); o `flock` inline não aninha.
- `podar-backups.py` não pega a trava do estado por dentro, então o `flock -w 300 estado.json.lock` da limpeza não aninha.
- Caminhos, WorkingDirectory e logs `append:` das units batem com as reais. As cadências se mantêm com `OnUnitInactiveSec`.
- `certidoes-rodar.sh` e `ponte-rodar.sh` propagam o código real. O passo que falha não interrompe os seguintes.

## Ordem mínima de correção antes do deploy
F2-01 (integrar OCR/TypeSafe) → F2-02 (ProtectHome/node) → F2-03 + F2-04 (sequência e rollback no PLANO e no README)
→ F2-06 (chmod/alerta) → F2-05/F2-09 no C2 → F2-07. F2-08 e F2-10 podem ficar para a Fase 2.
