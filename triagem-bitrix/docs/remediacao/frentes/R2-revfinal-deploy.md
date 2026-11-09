# Frente R2 — correções da revisão final foco 2 (deploy, F2-01..F2-10)

Status: **CONCLUÍDA** (sem commit — fica para o orquestrador). Base: `docs/remediacao/revisao/final-2-deploy.md`.
Retomada de um subagente interrompido: as mudanças dele em `deploy/` foram revisadas e mantidas;
o que faltava (PLANO, RUNBOOK, testes, relatório) foi feito aqui. Nenhum `.py` editado.
Única operação git: `git update-index --chmod=+x` (que também põe no índice o conteúdo atual do arquivo).

## Achado → correção

| # | Correção | Onde |
|---|---|---|
| F2-01 | Checkpoint **C0 BLOQUEANTE**: `tar` dos arquivos `ocr_documento.py`, `typesafe.py`, `typesafe_smoke.py`, `lote_para_fila.py` e `config.json` do servidor; integração com teste na branch; na janela, `rsync -anic` (dry-run, sem `deleting`) + `diff -u` arquivo a arquivo; cópia por `--files-from`, nunca `--delete`; `config.json` só se edita chave a chave (preserva `typesafe`). Pré-requisito 1/5 e matriz de risco atualizados | PLANO |
| F2-02 | Drop-in `deploy/systemd/migracao-fase1.conf` (`User=root`, `Group=root`, `ProtectHome=read-only`). As units mantêm `ProtectHome=yes` como alvo da fase 2, com comentário. Fase 2: copiar o node para `/opt` e definir `TRIAGEM_NODE` antes de voltar para `yes`. O C7 exige **job criado** na fila | units, README, PLANO |
| F2-03 | Sequência única da janela no PLANO: parar os timers → esperar os services inativos → backup → código → drop-ins (inclusive `triagem-alerta@.service.d/`) → units → `daemon-reload`/`verify`/`show -p User,ProtectHome` → start manual → `enable --now` de um timer por vez (limpeza por último). O bloco curto do README foi removido de propósito | PLANO, README |
| F2-04 | Backup = `tar` de `/opt/triagem-bitrix` (sem backups/logs) + units + `estado.json` sob `flock`. Rollback em 6 passos: parar timers, `disable --now triagem-limpeza.timer`, restaurar tar/units, tirar drop-ins, restaurar o estado pré-deploy (compatibilidade do estado novo com o código antigo **não provada**), religar | PLANO |
| F2-05 | `vigia_bot.sh:19,30` mede o mtime de `LOG=${LOG:-$BASE/pergunta.log}`. A proposta do bot ganhou `StandardOutput/StandardError=append:/opt/triagem-bitrix/pergunta.log`. O C2 passa a comparar a redireção real | `triagem-bitrix-bot.service`, PLANO C2 |
| F2-06 | `alertar-falha.sh`: `ALERTA_CMD` roda **antes** da coleta de contexto; `N=$(journalctl…) \|\| N="?"`. `+x` em `alertar-falha.sh` e `verificar-ambiente.sh` | `deploy/bin` |
| F2-07 | `T_FILA` 300→900 s (igual à ponte). Soma 420+120+120+120+900 = 1680 s → `TimeoutStartSec=30min`. RUNBOOK §9 manda recalcular quando mudar algum `T_*`. Idempotência: uuid5 determinístico (`lote_para_fila.py:329`) — **não testado com kill real** | `lote-fila.sh`, unit do lote-fila, RUNBOOK |
| F2-08 | Sudoers removido (`NoNewPrivileges` impede o sudo de elevar, e o `vigia_bot.sh` não usa sudo). Polkit `deploy/polkit/50-triagem-bitrix-bot.rules` **avaliado e mantido**: só `manage-units`, só o usuário `triagem`, só `triagem-bitrix-bot.service`, só `start`/`restart`. Funciona por D-Bus, sem setuid, então é compatível com `NoNewPrivileges`; exige polkit ≥ 0.106 (com 0.105 o vigia fica `User=root`). PLANO Fase 2 atualizado | polkit, unit do vigia, PLANO |
| F2-09 | O README não afirma mais que a unit não existe. O C2 ganhou `list-units --all`/`list-unit-files`/`systemctl cat triagem-bitrix.service`. Se ela existir, entra no backup, na parada, no C9 e no restore. RUNBOOK §8 para todo escritor do estado (timers, bot, `triagem-bitrix.service`) | README, PLANO, RUNBOOK §8 |
| F2-10 | pintar-linha `flock -n -E 0`; limpeza `flock -w 300 -E 75` + `SuccessExitStatus=75`. O texto "alternativa equivalente" do `pipeline.sh` foi corrigido (unit, README, RUNBOOK). RUNBOOK §9 reescrito (`PONTE_LIMITE`/`FILA_LIMITE`/`T_*`, sem editar `ExecStart`). `lote-fila.sh` sai 2 se faltar `timeout` | units, `lote-fila.sh`, RUNBOOK §9 |

## Provas (`~/.hermes/cache/scratch/revfinal2/`, stubs de `flock` com flock(2) real e `timeout` GNU-like)

- `bash -n` ok: `alertar-falha.sh`, `lote-fila.sh`, `verificar-ambiente.sh`.
- `t1.sh` com o `deploy/bin` atual: T1 pior=2 com 5 passos; T2 pula com exit 0; T4 exit=124 sem órfão e trava livre; T5 exit=1; T6 `pipeline.sh` com `limite 4` (confirma que não é equivalente); **T7 agora exit=2** (antes 5×127); T8 recusa o wrapper antigo.
- A2 (`journalctl` sai 1, `bin5/`): versão HEAD → `exit=1` sem alerta; **versão nova → `ALERTA_DISPARADA`, exit=0**.
- `t3.sh`: trava ocupada → pintar `-E 0` exit=0; limpeza `-E 75` exit=75; trava livre → exit=0.
- Não provado aqui (só no servidor): `systemd-analyze verify`, `ProtectHome=read-only` com o node real, regra polkit, OCR integrado (C0 depende de coleta).

## Verificação final

`grep -rn ProtectHome deploy/` → `ProtectHome=yes` só nas 9 units (alvo da fase 2, cada uma com o comentário "na fase 1 o drop-in põe read-only"). `migracao-fase1.conf:19 ProtectHome=read-only`. No README: só texto.

```
100755 ecc15e2e5ff7f3af89c21c038a6604feb0955e5f 0	deploy/bin/alertar-falha.sh
100755 d1c93dc334d2739c08b8b86f76a086d99ecde717 0	deploy/bin/lote-fila.sh
100644 e4c8357f234b7363d30d8accd28dd9ea6c502a66 0	deploy/bin/podar-backups.py
100755 3e08c01e9035a7a07f0bc153e6a98033bd1e9ce7 0	deploy/bin/verificar-ambiente.sh
```

## Pendências

- C0 depende de coleta no servidor e da integração do OCR/TypeSafe nos `.py` (fora desta frente: proibido editar `.py`).
- Teste de "kill entre o node e o CONTROLE não duplica job" (F2-07).
- `alertar-falha.sh` com `User=triagem` (fase 2) precisa do grupo `systemd-journal` para o contexto; a alerta já não depende disso.
