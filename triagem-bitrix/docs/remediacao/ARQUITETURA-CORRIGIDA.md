# Arquitetura corrigida — parte de OPERAÇÃO (rascunho da Frente H)

> **Rascunho.** A Frente H documenta aqui só a camada de operação (execução, travas,
> logs, retenção, alerta, usuários). O núcleo (estado, ingestão, download, ponte,
> identificação) é de outras frentes — seções marcadas **TODO orquestrador**.
> Fontes: `docs/auditoria/00-RESUMO-EXECUTIVO.md`, `06-testes-operacao.md`,
> `01-seguranca.md`, `CONTEXTO-TRIAGEM.md` e os relatórios da onda 1
> (`docs/remediacao/frentes/{B,S,R,PT}-*.md`).

## 1. Visão de execução (como o sistema roda sozinho)

```
                        ┌──────────────────────────────────────────────┐
  chats Bitrix          │  FASE 1 — triagem_grupo.py (triagem-bitrix)  │
  (chat100001/100002)──▶│  leitura (fonte: ver §2) → lotes → estado.json│
                        └───────────────┬──────────────────────────────┘
                                        │ estado.json (0600, escrita atômica
                                        │  via estado_store — onda 1)
        ┌───────────────────────────────┼────────────────────────────────┐
        │                               │                                │
        ▼                               ▼                                ▼
 triagem-lote-fila (10 min)   triagem-bitrix-ponte (5 min)    triagem-bitrix-certidoes (5 min)
 baixar pendentes             lote pronto → staging           certidão → tarefa no CRM
 reconciliar estado×disco     /opt/agente-separador/data/     (bot/negócio do cliente)
 reconciliar file_ids (7d)   staging/<upload_id>/
 completar CPF por nome      fila-jobs.json ──▶ agente-separador-worker (painel)
 enfileirar (regra escritório)
        │                               │
        └─────────── trava COMPARTILHADA ┴── $BASE/fila.lock
                    (ambos gravam fila-enviados.json)

 pintar-linha-triagem (2 min)  pintar_linha.py --aplicar → planilha PASTAS PREVI GERAL
 triagem-bitrix-bot (contínuo) pergunta_traco.py --ativo --loop 8 — bot nativo Bitrix 100003
 triagem-bitrix-bot-vigia (2 min) vigia_bot.sh — mtime do pergunta.log → restart do bot
 triagem-limpeza (04:15)       podar-backups.py --aplicar --tmp-orfaos (S05)
 triagem-alerta@ (OnFailure)   alertar-falha.sh → journal + ALERTA_CMD
```

A instalação exata (qual unit roda o quê hoje) **precisa do servidor**
(`coleta-triagem.sh`); a proposta completa está em `deploy/systemd/`.

## 2. Fonte de leitura e modo do traço (comportamento REAL, não o desejado)

- **Fonte de leitura efetiva hoje: webhook do Bitrix.** `config.json` tem
  `"fonte_leitura": "webhook"` e `triagem_grupo.py` (ciclo) honra esse valor: quando é
  `webhook`, lê via `im.dialog.messages.get` — embora o comentário do config e o
  docstring digam que webhook é "só diagnóstico" (divergência D2 da auditoria: a
  documentação promovia a API como fonte, mas a produção roda no webhook, que vê
  MENOS mensagens: medição 21/09 de 386 (API) × 234 (webhook)). A API do VibeCode
  (`bitrix_cliente.py`/fonte nova) tem circuit-breaker e failover entre fontes quando é
  ela a primária. **Decisão a registrar (TODO orquestrador):** refazer a medição
  (`--testar-fonte`) e decidir `fonte_leitura`; enquanto for `webhook`, aceite-se risco
  de lote incompleto.
- **Modo do traço: `delimitador`** (`config.json`), não `grupo_autor` como dizia o
  README antigo. Caracteres aceitos: 5 (inclui `_`), não 4. `traco_vigencia_min=30`,
  `timeout_lote_min=120`, `janela_min_fallback=30`, `barreira_identidade=true`.
- **Regra do escritório (24/09)** vigente: só vai à fila o lote aberto e fechado por
  traço OU fechado por janela/timeout quando é de um cliente só e limpo
  (`ponte_criterio.py`, aplicada por padrão em `lote_para_fila.py`).
  A flag `--todos-os-fechamentos` DESLIGA a regra — o `pipeline.sh` atual a usa
  (pendência; o `deploy/bin/lote-fila.sh` proposto não usa).
- **Bot em uso: nativo do Bitrix, ID 100003** (`pergunta_traco.py`, config
  `pergunta_traco.*`). O canal **Telegram é legado e está desativado em operação**
  (nenhum timer/unit o executa; o dono confirmou 09/11). O código e o
  `config.json` (`telegram.habilitado=true`) ainda existem — mudança proposta pela
  Frente T (não é arquivo da Frente H).

## 3. Estado, travas e ordem de escrita (onda 1 aplicada)

- `estado_store.py` (onda 1, frente B): escrita atômica 0600 + fsync, `EstadoCorrompido`
  (JSON ilegível NUNCA vira `{}`), `atualizar_estado` com flock, merge por dono de campo
  (`mesclar_lote`/`atualizar_lotes`), `fazer_backup`/`podar_backups` com retenção,
  `trava_arquivo`, `limpar_tmp_orfaos`.
- **Consumidores ainda não migrados** (onda 2 — donos próprios): `completar_cpf_por_nome`,
  `reconciliar_estado`, `reconciliar_arquivos_disco`, `nomear_lotes_por_cpf`,
  `backfill_nome_lotes` gravam o estado sem trava (auditoria C1) e fazem backup sem
  retenção (S05). Enquanto não migram, a unit `triagem-limpeza` estanca o disco (S05) e o
  `UMask=0077` reduz a exposição (S08).
- **Travas de processo** (mapa completo em `deploy/systemd/README.md`):
  `fila-enviados.json` é gravado por lote-fila E ponte → trava compartilhada
  `$BASE/fila.lock` (lote_para_fila.py, ponte-rodar.sh, deploy/bin/lote-fila.sh); ordem única fila → estado; sem lock aninhado.

## 4. Logs

| Log | Origem | Rotação |
|---|---|---|
| `triagem.log` | `triagem_grupo.py` | própria (`RotatingFileHandler` 2 MB × 5) |
| `ponte.log` | stdout do timer da ponte (provável `StandardOutput=append:` — **conferir no servidor**) | `deploy/logrotate/triagem-bitrix` (copytruncate) |
| `pergunta.log` | `pergunta-rodar.sh`/bot (`>> pergunta.log`) | idem (copytruncate obrigatório: o vigia usa o mtime) |
| `certidoes.log` | timer das certidões (**conferir no servidor**) | idem |
| journal | stderr de tudo | `MaxRetentionSec`/`SystemMaxUse` (TODO orquestrador) |

`copytruncate` em todos os do logrotate porque o `vigia_bot.sh` decide "bot mudo" pelo
mtime do `pergunta.log` (renomear/recriar enganaria o vigia). Conteúdo com nome/CPF de
cliente em log é achado S04 (dono do código): o logrotate limita retenção, não conteúdo —
a máscara é `seguranca.instalar_filtro_log()` (onda 1, frente S) nos entrypoints.

## 5. Retenção e descarte (S05)

- **Operacionais** — `backups-estado/estado.json.bak-*`, legados `estado.json.bak-*`,
  `backups/fila-jobs-*.json`: política **24 últimos + 1/dia por 14 dias** (estado,
  contrato `estado_store.podar_backups`) e 50 + 1/dia × 14 (fila, OP-O2). Execução:
  `deploy/bin/podar-backups.py` (simulação por padrão; `--aplicar` para executar) via
  `triagem-limpeza.timer` diário.
- **Cópias de recuperação** — `estado.json.corrompido-*`, dumps manuais em `backups/`:
  nunca apagadas pela poda; inventário no relatório do script.
- **Evidências de incidente** — a massa legada de `.bak-*` contém **senha gov.br/INSS de
  cliente** (S01) e pode ser prova do incidente: descarte **seguro** (zerar + fsync +
  unlink, `seguranca.remover_seguro`) **somente após autorização escrita**
  (`--descarte-seguro --autorizado-por "<nome>"`, nunca no timer). Enquanto isso,
  `chmod 0600` + dono restrito (S08) e inventário no RIPD (**TODO orquestrador**).

## 6. Usuário e permissões (S08)

- Alvo: usuário de sistema `triagem` dono de `/opt/triagem-bitrix` e
  `/opt/agente-separador/data`; units com `User=triagem`, `UMask=0077`,
  `ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges`.
- Bloqueadores atuais do `User=triagem` (por isso a migração é gradual):
  `lote_para_fila.py:53` e `certidoes.py:206` executam node em `/root/.hermes/...`;
  `download_svc.py:34` e `esperar_bot_liberar.py` leem `/root/.vibecode/api_key`;
  `telegram_grupo.py:38` lê `/root/.telegram/...`; `vigia_bot.sh` chama `systemctl`
  (sudoers restrito proposto em `deploy/sudoers/`); `certidoes.py`/`lote_para_fila.py`
  escrevem `/tmp` fixo (S09). Todos são de outros donos — ver "Pendências" em
  `docs/remediacao/frentes/H-infra.md`.
- `seguranca.ler_segredo` (onda 1) já lê de `TRIAGEM_*` (env) ou `/etc/triagem-bitrix/*`
  (0600, recusa permissão aberta, sem varredura de projetos, sem `/tmp`): é o destino
  único de credencial (S06) — os legados continuam funcionando com aviso até a migração.

## 7. Alerta e auto-recuperação

- `OnFailure=triagem-alerta@%n.service` em toda service → `deploy/bin/alertar-falha.sh`
  (journal + `ALERTA_CMD` do EnvironmentFile). **Destino final da alerta é TODO
  orquestrador** (Telegram legado desativado; opções: bot Bitrix 100003, e-mail do
  escritório, webhook do painel).
- Bot do traço: `triagem-bitrix-bot` (`Restart=on-failure`, exit 75 = outra instância não
  reinicia) + `triagem-bitrix-bot-vigia` a cada 2 min (mtime do `pergunta.log`).
  Futuro: `WatchdogSec` + `sd_notify` no loop do bot (TODO orquestrador; depende do dono
  do `pergunta_traco.py`).

## 8. O que esta camada NÃO resolve (fronteiras)

- Conteúdo de log com CPF/nome (S04) e senha no estado/fila/backups (S01): são de código
  alheio; a operação só limita retenção e permissão.
- `pipeline.sh` com `--todos-os-fechamentos` e sem `pipefail` (OP-O4): pendência do dono;
  o wrapper proposto (`deploy/bin/lote-fila.sh`) substitui o comportamento sem editar o
  arquivo alheio.
- Migração dos 5 gravadores sem trava (C1) e do ciclo para `mesclar_lote` (A3): onda 2.
