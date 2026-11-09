# Plano de implantação — infraestrutura da triagem-bitrix (rascunho da Frente H)

> **Rascunho — TODO orquestrador completa** (datas, responsáveis, janela de manutenção).
> Princípio: nada de mudança de comportamento "silenciosa"; cada fase tem verificação e
> rollback. Nada apaga dado sem autorização (§Fase 3).

## Pré-requisitos (uma vez)

1. **Coleta feita em 09/11/2026** (`coleta-triagem.txt`; resumo em
   `SERVIDOR-COLETA-20261109.md`): units/timers reais conferidos — `deploy/systemd/` foi
   reescrito a partir deles (tabela antes × depois em `deploy/systemd/README.md`). Faltam
   **dois** itens: (a) **C0 — BLOQUEANTE** — o servidor tem código que esta branch NÃO tem
   (OCR/TypeSafe, `SERVIDOR-COLETA-20261109.md` §5); (b) o `ExecStart`/redireção do
   `triagem-bitrix-bot.service` e a existência de `triagem-bitrix.service` (C2).
2. Usuário de sistema: `useradd --system --home-dir /opt/triagem-bitrix --shell /usr/sbin/nologin triagem`.
3. Segredos em `/etc/triagem-bitrix/` (0700): `webhook_bitrix`, `vibecode_key`,
   `bot_token_100003` (0600) e `env` (0600) — conteúdo `TRIAGEM_*` + `ALERTA_CMD`.
   Copiar SEMPRE com `install -m 0600` (não `cp`), nunca por `/tmp`.
   **`webhook_bitrix` e `vibecode_key` precisam existir ANTES do código novo** (checkpoint C3).
4. Acesso de leitura a este repositório no servidor (`/opt/triagem-bitrix`) com o
   diretório `deploy/` incluído.
5. `fonte_leitura` **continua `webhook`**: a medição de 48 h deu webhook = API
   (chat100001 247/247; chat100002 269/269; 0 mensagens só de um lado). Nenhuma mudança
   em `config.json` **feita por esta branch** — mas o `config.json` do servidor tem o bloco
   `typesafe`, que a branch não tem: **nunca copiar o `config.json` da branch por cima**
   (C0 integra; no servidor só se edita chave a chave).

## Checkpoints obrigatórios (na ordem; cada um com a saída anexada à ata)

| # | Quando | Comando / ação | Passa se |
|---|---|---|---|
| **C0** | **antes de tudo — BLOQUEANTE** | No servidor: `tar czf <ata>/coleta-ocr.tgz -C /opt/triagem-bitrix ocr_documento.py typesafe.py typesafe_smoke.py lote_para_fila.py config.json` (0600). Fora da janela: integrar na branch o OCR (bloco "OCR (Indra)" do `lote_para_fila.py` + quarentena `_QUARENTENA-docs-de-outros/`) e o bloco `typesafe` do `config.json`, com teste. Na janela, ANTES de copiar: `rsync -anic --exclude='backups*' --exclude='*.log' --exclude='estado.json*' <branch>/ /opt/triagem-bitrix/ \| tee <ata>/rsync-dryrun.txt` e `diff -u /opt/triagem-bitrix/<arq> <branch>/<arq>` de **cada** arquivo listado | `ocr_documento.py`, `typesafe.py`, `typesafe_smoke.py` existem na branch; `diff` do `lote_para_fila.py` não remove o bloco OCR; `config.json` do servidor preserva `typesafe`; o dry-run **não** lista `deleting` e só lista arquivos esperados. Cópia por lista explícita (`rsync --files-from=`), **nunca `--delete`** nem o diretório inteiro. Falhou → **não implantar** (sem o OCR, documentos que hoje vão para a quarentena entram na fila) |
| C1 | antes de tudo | `command -v flock && command -v timeout` | os dois existem (util-linux/coreutils). Sem `flock` o `ponte-rodar.sh` remediado e o `lote-fila.sh` saem 2 — não implantar |
| C2 | antes de instalar units | `systemctl cat triagem-bitrix-bot.service`; `systemctl list-units --all 'triagem*' 'pintar*'`; `systemctl list-unit-files 'triagem*' 'pintar*'`; `systemctl cat triagem-bitrix.service` | ExecStart/WorkingDirectory/Restart **e `StandardOutput`/redireção para `pergunta.log`** (o vigia mede o mtime dele) conferidos com `deploy/systemd/triagem-bitrix-bot.service`; divergência → ajustar a proposta (a real manda). **Não instalar o arquivo do bot sem isso.** Se `triagem-bitrix.service` existir: registrar o ExecStart, incluí-la no backup das units, no passo "parar" da Fase 1, no C9 e no restore (RUNBOOK §8) — é candidata a escritora do `estado.json` |
| C3 | antes do código novo | `install -d -m 0700 /etc/triagem-bitrix` e `install -m 0600` de `webhook_bitrix` e `vibecode_key` | `stat -c '%a %U' /etc/triagem-bitrix/*` = `600 root` |
| C4 | antes do código novo | `python3 manutencao/inventario_estado.py` (linha de base) | saída anexada (contagens por status, prontos, requer_revisao, cursores) |
| C5 | depois do código novo | `cd /opt/triagem-bitrix && python3 -c "import pergunta_traco as p; p.carregar_config_do_bot()"` | sem exceção (config do bot 100003 lida com o modo exigido) |
| C6 | depois do código novo | `systemctl disable --now triagem-bitrix-pergunta.timer` (já inativo — garantir) | `systemctl is-enabled triagem-bitrix-pergunta.timer` ≠ `enabled` |
| C7 | depois de instalar as units | 1 rodada manual de cada (`systemctl start <svc>; systemctl status <svc>`); `journalctl -u triagem-lote-fila -n 50` mostra os 5 passos e "pior saída"; `tail ponte.log` + fila do painel | lote-fila roda os 5 passos mesmo com passo ≠ 0; ponte e lote-fila nunca simultâneos (`fila.lock`); a rodada da ponte mostra **job criado** na fila (não só `exit 0` — prova que o node em `/root/.hermes/node` é alcançável com o hardening, F2-02); certidões idem; passo 5 terminou bem abaixo de `T_FILA` (900 s) |
| C8 | depois do código novo | `python3 manutencao/inventario_estado.py` de novo | contagens coerentes com C4 (sem perda de lotes; só crescimento esperado) |
| C9 | primeiras 24-48 h | duração do ciclo do bot/coleta (`journalctl -u triagem-bitrix-bot`, mtime do `pergunta.log`) e `systemctl list-timers` | a duração do ciclo **volta a baixar** depois de G-01 (o 1º ciclo monta `f1_regiao` com leitura completa e é lento; os seguintes usam cursor). Se não baixar, investigar antes de seguir |
| C10 | antes de `TRIAGEM_SEGREDOS_ESTRITO=1` | `chmod 600` em todos os segredos (legados inclusive) + `verificar-ambiente.sh` | nenhum ERRO de permissão; só então ligar o modo estrito |

## Fase 0 — hoje, sem mexer em lógica (baixo risco)

| # | Ação | Evidência | Rollback |
|---|---|---|---|
| 0.1 | `chmod 0600` nos segredos legados (`vibecode.key`, `/root/.vibecode/api_key`, `/root/.telegram/...token`) + `chmod 0700` em `/opt/triagem-bitrix` e `/opt/agente-separador/data` | `verificar-ambiente.sh` sem ERRO | devolver permissões anotadas antes |
| 0.2 | Instalar `deploy/logrotate/triagem-bitrix`; `logrotate -d` valida; rodar `logrotate -f` uma vez | `ponte.log` cai de 208 MB p/ ~10 MB gz na 1ª rotação | remover o arquivo de config |
| 0.3 | Rodar `podar-backups.py` (SIMULAR) e anexar a saída à ata; decisão do responsável | plano 24+14 listado | simulação não altera nada |
| 0.4 | `podar-backups.py --aplicar` (só operacionais novos + fila; legado intacto) | `du -sh backups/ backups-estado/` antes/depois | backups apagados são excedentes por política; sem rollback possível (mas nada essencial sai: a política mantém 24+14) |
| 0.5 | Mascarar CPF/nome nos prints é de código alheio (S04) — **pendência** registrada; enquanto isso, `journalctl` com retenção curta | — | — |

## Fase 1 — units propostas, ainda como root (com hardening)

Por que ainda root: os caminhos `/root/.hermes/node`, `/root/.vibecode/api_key`,
`/root/.telegram/...` e os `/tmp` fixos (S09) são de arquivos de outros donos; com
`User=triagem` hoje os jobs falhariam. Mesmo como root, já vale: `UMask=0077`,
`PrivateTmp`, `ProtectSystem=strict`, `ReadWritePaths`, `NoNewPrivileges`, `OnFailure`.

Hardening da Fase 1 com UMA exceção: `ProtectHome=read-only` (não `yes`). Com `yes` o
`/root` some **até para root** e o node (`/root/.hermes/node/bin/node`, `lote_para_fila.py:85`,
`certidoes.py`; `TRIAGEM_NODE` não está definido) e os segredos legados ficam inacessíveis —
ponte, passo 5 do lote-fila e certidões param de enfileirar (F2-02). O drop-in
`deploy/systemd/migracao-fase1.conf` faz as duas coisas (`User=root` + `ProtectHome=read-only`).

**Sequência única da janela (não há atalho no README):**

1. Checkpoints **C0**-C4 ok.
2. **Parar tudo que escreve:** `systemctl stop triagem-lote-fila.timer triagem-bitrix-ponte.timer
   triagem-bitrix-certidoes.timer pintar-linha-triagem.timer triagem-bitrix-bot-vigia.timer`
   (+ `triagem-limpeza.timer` se reinstalando). Esperar cada service ficar inativo — uma rodada
   antiga do lote-fila (5 ExecStart) ou da ponte (trava antiga `/var/lock/triagem-bitrix-ponte.lock`)
   não pode estar no meio da troca:
   `for s in triagem-lote-fila triagem-bitrix-ponte triagem-bitrix-certidoes pintar-linha-triagem; do
   while systemctl is-active -q $s.service; do sleep 10; done; done`. O bot (`--loop`) e, se o C2
   mostrar, `triagem-bitrix.service`: `systemctl stop` só durante a cópia do código.
3. **Backup (rollback)** em `<dir-da-ata>` (0700):
   `tar czf <ata>/opt-triagem-$(date +%F).tgz --exclude='backups*' --exclude='*.log' -C / opt/triagem-bitrix`;
   `cp -a /etc/systemd/system/{triagem-*,pintar-linha-triagem*} <ata>/units/`;
   `flock -w 60 /opt/triagem-bitrix/estado.json.lock cp -p /opt/triagem-bitrix/estado.json <ata>/estado.json.pre-deploy`;
   `chmod -R go= <ata>`.
4. **Código:** cópia por lista explícita conferida no C0 (`rsync -ac --files-from=<lista>`; nunca
   `--delete`; `config.json` só por edição de chave). `ponte-rodar.sh` com `fila.lock` entra JUNTO
   com as units novas (ver passo 7).
5. **Drop-ins:** `install -D -m 0644 deploy/systemd/migracao-fase1.conf
   /etc/systemd/system/<svc>.service.d/migracao.conf` para cada service proposto — `triagem-lote-fila`,
   `triagem-bitrix-ponte`, `triagem-bitrix-certidoes`, `pintar-linha-triagem`,
   `triagem-bitrix-bot-vigia`, `triagem-limpeza` **e o template**
   `/etc/systemd/system/triagem-alerta@.service.d/migracao.conf` (com o `@`). Sem eles as units sobem
   `User=triagem` → `200/CHDIR`/`EACCES` (o `/opt/triagem-bitrix` é 0700 root desde a Fase 0.1).
6. **Units:** `install -m 0644` das units + timers (mesmos nomes das reais; substituem no lugar)
   + novas (`triagem-limpeza`, `triagem-alerta@`). **Não** instalar `triagem-bitrix-bot.service`
   antes do C2. `systemctl daemon-reload`; `systemd-analyze verify /etc/systemd/system/{triagem-*,pintar-linha-triagem}.service`;
   `systemctl show -p User,ProtectHome <svc>` = `root` / `read-only` em cada uma.
7. **Ordem obrigatória lote-fila × ponte:** unit nova do lote-fila com wrapper antigo → o
   `lote-fila.sh` recusa (exit 2, alerta). Unit antiga (5 ExecStart) com código novo →
   **regressão**: o 1º passo ≠ 0 corta os seguintes e a fila para. Os passos 4-6 acontecem na
   mesma janela, com os timers parados.
8. Religar bot (e `triagem-bitrix.service`, se existir). `systemctl start` **manual** de cada service
   (C7), conferir, e só então `systemctl enable --now <timer>` — **um timer por vez**, na ordem:
   pintar-linha → certidões → ponte → lote-fila → vigia → limpeza (esta por último; roda `--aplicar`).
9. Bot do traço: modo único `--loop` (serviço contínuo existente); `triagem-bitrix-pergunta`
   fica desativado (C6); confirmar que o vigia reinicia e que exit 75 não gera laço.
10. C5, C8, C9; `verificar-ambiente.sh` + checklist do RUNBOOK §10.

**Rollback (executável; código + units + estado):**

1. `systemctl stop` dos 6 timers (`triagem-lote-fila`, `triagem-bitrix-ponte`,
   `triagem-bitrix-certidoes`, `pintar-linha-triagem`, `triagem-bitrix-bot-vigia`,
   `triagem-limpeza`) e esperar os services inativos (como no passo 2).
2. `systemctl disable --now triagem-limpeza.timer` (unit nova, roda `podar-backups --aplicar`; não
   existe no código antigo) e remover `triagem-limpeza.*` e `triagem-alerta@.service` (+ `.d/`).
3. Código: `tar xzf <ata>/opt-triagem-<data>.tgz -C /` (restaura `/opt/triagem-bitrix` inclusive o OCR;
   arquivos novos da branch que sobrarem não são chamados pelo código antigo — listar com o dry-run do C0).
4. Units: `install -m 0644 <ata>/units/* /etc/systemd/system/`; remover `/etc/systemd/system/*.service.d/migracao.conf`;
   `systemctl daemon-reload`.
5. Estado: o código antigo **não** é garantido a ler o que o novo gravou (`f1_regiao`, cursores,
   `backups-estado/`). Até prova em contrário, restaurar `<ata>/estado.json.pre-deploy` sob
   `flock estado.json.lock` (RUNBOOK §8) com bot/coletor parados, aceitando refazer o que entrou
   depois (a fila é idempotente por uuid5 determinístico, `lote_para_fila.py:329`). Registrar na ata.
6. Religar bot e timers (`systemctl start <timers>`), conferir `systemctl list-timers` e 1 rodada.

## Fase 2 — usuário `triagem` (migração gradual)

Bloqueadores a remover primeiro (todos de **outros donos** — ver H-infra.md "Pendências"):

- `lote_para_fila.py:85` / `certidoes.py`: node em `/root/.hermes/node/bin/node` →
  copiar o node para `/opt/triagem-bitrix/node/` e definir `TRIAGEM_NODE=` no
  `/etc/triagem-bitrix/env`. Só então trocar `ProtectHome=read-only` (drop-in) por `yes`.
- `download_svc.py:34`, `esperar_bot_liberar.py`, `vigia-bot-triagem.py`: segredos via
  `seguranca.ler_segredo` (já pronto na onda 1).
- `certidoes.py`/`lote_para_fila.py`: temporários em `/tmp` fixo (S09) →
  `seguranca.arquivo_temp_privado` (contrato pronto).
- `vigia_bot.sh`: `systemctl restart` (sem `sudo`) como `triagem` → regra polkit
  `deploy/polkit/50-triagem-bitrix-bot.rules` (polkit ≥ 0.106; `pkaction --version`). **Sudoers não
  serve:** `NoNewPrivileges=yes` impede o `sudo` (setuid) de elevar (F2-08). Polkit 0.105 → o vigia
  fica com o drop-in `User=root`. Futuro: `WatchdogSec` + `sd_notify`.

Depois, por unit (ordem sugerida: pintar-linha → certidões → ponte → lote-fila → bot):

1. `chown -R triagem:triagem /opt/triagem-bitrix /opt/agente-separador/data`.
2. Remover o drop-in `migracao.conf` (`User=root` + `ProtectHome=read-only`); `daemon-reload`; rodada manual; conferir saída e
   permissão dos arquivos criados (0600 via `UMask`).
3. `TRIAGEM_SEGREDOS_ESTRITO=1` no `/etc/triagem-bitrix/env` **só depois do C10**
   (`chmod 600` em todos os segredos e nenhum dependendo de legado com permissão aberta).
4. `pintar-linha-triagem` continua com `/opt/qc-planilha/venv/bin/python`: o usuário
   `triagem` precisa de leitura em `/opt/qc-planilha/venv` e em
   `/opt/agente-separador/service-account.json`.

## Fase 3 — descarte seguro dos legados (S01/S05) — SÓ com autorização escrita

1. Ata/RIPD com a decisão (quem autorizou, quando, por quê — art. 16 LGPD).
2. `podar-backups.py --aplicar --descarte-seguro --autorizado-por "<nome>"`.
3. Conferir `verificar-ambiente.sh` (contagens) e anexar a saída à ata.
4. Verificar também `/tmp/_entradas.json`, `/tmp/certidao_*.pdf`, `/tmp/_ler_certidao.js`
   (S09) e `backups/fila-jobs-*` antigos — mesma autorização.

## Fase 4 — sustentação (depende de outras frentes)

- Migração dos gravadores de estado para `estado_store.atualizar_estado` (C1) e do ciclo
  para `mesclar_lote` (A3): fim do loop de 1 backup/ciclo (O1) — onda 2.
- Exit codes reais em `certidoes-rodar.sh`, `baixar_pendentes.py`, `reconciliar_estado.py`,
  `pipeline.sh` (OP-O4) — donos dos arquivos; com isso o `OnFailure` passa a valer.
- `WatchdogSec` no bot (substitui o vigia por mtime).
- Testes de operação: o `deploy/` hoje tem validação de sintaxe + teste funcional do
  podar-backups; testes pytest dos wrappers (subprocess) ficam para quando os donos
  liberarem os arquivos (**TODO orquestrador**).

## Matriz de risco resumida

| Risco | Mitigação |
|---|---|
| Unit nova sobe duplicada com timer antigo | units novas têm os MESMOS nomes das reais (substituem no lugar); backup das reais (Fase 1, passo 1) |
| lote-fila real (5 ExecStart) + código que devolve 1/2 → fila para | ExecStart único `lote-fila.sh` (pior código, todos os passos); troca na mesma janela do código (Fase 1, passo 4) |
| lote-fila e ponte com travas diferentes | as duas pelo `ponte-rodar.sh` → `fila.lock`; `lote-fila.sh` recusa wrapper antigo |
| Bot sobrescrito por hipótese | C2 (`systemctl cat`) antes; arquivo do bot fora da instalação em lote |
| `User=triagem` quebra job por caminho `/root` | drop-in temporário + ordem de migração por unit |
| Deploy apaga o OCR/TypeSafe do servidor | C0 bloqueante: coleta, integração com teste, `rsync -n` + `diff`, cópia por lista, nunca `--delete` |
| `ProtectHome=yes` esconde o node em `/root` | `read-only` no drop-in da Fase 1; C7 exige job criado |
| Rollback sem código | tar de `/opt/triagem-bitrix` + `estado.json` no passo 3 da Fase 1 |
| Poda apaga algo necessário | política 24+14 com 1/dia × 14 dias; simulação antes; `.corrompido-*`/dumps intocáveis |
| Descarte de senha sem autorização | modo legado SÓ com `--descarte-seguro --autorizado-por`; timer nunca roda esse modo |
| Alerta em laço (exit code errado) | `SuccessExitStatus`/`RestartPreventExitStatus` no bot; OnFailure dispara 1× por falha |
| Log rotate engana o vigia | `copytruncate` obrigatório (documentado no logrotate) |
