# Runbook operacional — triagem-bitrix (rascunho da Frente H)

> **Rascunho.** Comandos do dia a dia, diagnóstico e recuperação. Comandos marcados
> "conferir no servidor" não puderam ser validados localmente (sem systemd/produção).
> Nada aqui executa rede nem apaga dado sem aviso explícito.

## 1. Estado do sistema em 30 segundos

```bash
systemctl list-timers --all | grep -E "triagem|pintar"      # próximas execuções
systemctl status triagem-lote-fila.service --no-pager -l    # última rodada
journalctl -u triagem-lote-fila -n 50 --no-pager            # resumo do ciclo
/opt/triagem-bitrix/deploy/bin/verificar-ambiente.sh        # só leitura; exit 1 = erro
```

Saúde do bot do traço: `pergunta.log` crescendo (mtime recente) e
`journalctl -u triagem-bitrix-bot -n 30`. Saída vazia de `monitor_lotes.py --detalhe`
(cron Hermes, 30 min — conferir no servidor) = lotes prontos tudo em dia.

## 2. Rodar uma etapa na mão

Os scripts já tomam a trava compartilhada `$BASE/fila.lock` (`/opt/triagem-bitrix/fila.lock`)
sozinhos — `ponte-rodar.sh` e `deploy/bin/lote-fila.sh` com `flock -n` (ocupado = pula com
exit 0) e `lote_para_fila.py --enviar` por dentro. **Não** envolva esses comandos em outro
`flock` de `fila.lock`: lock aninhado no mesmo arquivo faz a ponte pular sempre (PT-02).

```bash
# pipeline completo (o que o triagem-lote-fila faz)
sudo /opt/triagem-bitrix/deploy/bin/lote-fila.sh

# ponte isolada (regra do escritório; PONTE_DIAS=0 = sem corte de data)
sudo PONTE_LIMITE=4 PONTE_DIAS=7 /opt/triagem-bitrix/ponte-rodar.sh

# simulação da ponte (não escreve nada)
python3 /opt/triagem-bitrix/lote_para_fila.py --simular --todos

# certidões
sudo flock -n /var/lock/triagem-bitrix-certidoes.lock python3 /opt/triagem-bitrix/certidoes.py --enviar

# CPF por nome em modo simulação (não grava estado)
python3 /opt/triagem-bitrix/completar_cpf_por_nome.py --simular
```

## 3. Sintomas comuns

| Sintoma | Causa provável | Ação |
|---|---|---|
| Timer "pula" sempre (0 rodadas) | trava compartilhada presa por rodada longa da ponte/lote-fila | `flock -n /opt/triagem-bitrix/fila.lock true && echo livre`; ver `journalctl -u triagem-bitrix-ponte`; rodada legítima = esperar |
| Lote pronto não vai para o painel | regra do escritório (precisa traço/limpeza), `requer_revisao`, `--dias` cortou o lote, ou anti-dupe por CPF | `python3 lote_para_fila.py --simular --lote <ID>` (mostra o motivo); `monitor_lotes.py --detalhe` |
| Bot do traço mudo (sem resposta no grupo) | bot travado (evento 22/10) ou bloqueio `ERROR_LOOP_DETECTED`/`ACCESS_DENIED` | vigia deve reiniciar sozinho (mtime do `pergunta.log`); se não: `sudo systemctl restart triagem-bitrix-bot`; bloqueio: `esperar_bot_liberar.py --dialog <privado do operador>` |
| Disjuntor de download aberto (`circuit-download.json`) | Bitrix respondeu 429/5xx em rajada | **não martelar**: `baixar_pendentes`/`download_svc` devem respeitar o breaker (OP-O5 é de outro dono); esperar a meia-abertura; ver `journalctl -u triagem-lote-fila \| grep breaker` |
| Disk enchendo (S05) | massa de `estado.json.bak-*` / `backups/fila-jobs-*` | `podar-backups.py` (simula) → conferir → `--aplicar`; legado com senha: §5 |
| `estado.json` corrompido (EstadoCorrompido) | queda no meio da escrita (raro: escrita atômica) | NÃO apagar; a cópia `.corrompido-<ts>` fica ao lado; restaurar do backup mais recente (`backups-estado/`), com trava segurada; registrar incidente |
| `failed` no systemd sem erro no log | exit code engolido pelo wrapper (OP-O4) | rodar o comando do `ExecStart` à mão para ver o exit real; wrappers corrigidos propagam o código |
| `triagem-alerta@` disparando em laço | falha persistente de uma unit | `systemctl status <unit>`; corrigir a causa — a alerta não resolve, só avisa |

## 4. Poda de backups (S05)

```bash
python3 /opt/triagem-bitrix/deploy/bin/podar-backups.py                 # SIMULA (padrão)
python3 /opt/triagem-bitrix/deploy/bin/podar-backups.py --aplicar       # executa a política
```

- Política: **24 últimos + 1/dia por 14 dias** (estado) · 50 + 1/dia por 14 dias (fila).
- A cota do estado é sobre a **união** dos formatos (`backups-estado/` + legados
  `estado.json.bak-*`): com os dois coexistindo, ela se reparte entre eles (fixture: 12
  novos + 20 legados mantidos). Depois que os legados forem descartados (§5), os novos
  ocupam a cota inteira.
- O timer `triagem-limpeza` roda `--aplicar --tmp-orfaos` diário 04:15.
- O script classifica **operacional** (apaga com política) × **recuperação**
  (`.corrompido-*`, dumps manuais — nunca apaga) × **evidência** (legados `estado.json.bak-*`).

## 5. Descarte SEGURO dos backups legados (só com autorização)

Os 1.833 `estado.json.bak-*` legados contêm **senha gov.br/INSS de cliente** (S01) e
podem ser evidência do incidente. **Nunca automático** (o timer não roda este modo):

1. Autorização **escrita** do responsável (e do encarregado/RIPD — TODO orquestrador).
2. Registrar a decisão no RIPD (LGPD art. 16: eliminação após o tratamento).
3. Executar (descarte seguro = zerar + fsync + unlink):

```bash
python3 /opt/triagem-bitrix/deploy/bin/podar-backups.py --aplicar \
  --descarte-seguro --autorizado-por "Nome Sobrenome (e-mail, data)"
```

A autorização aparece na saída (e no journal, via `triagem-limpeza` manual). Antes disso:
`chmod 0600` nos legados e dono restrito (S08) — ver `verificar-ambiente.sh`.

## 6. Rotação de logs (OP-O3)

`deploy/logrotate/triagem-bitrix` (copytruncate — obrigatório pelo vigia). Instalar em
`/etc/logrotate.d/` e testar: `logrotate -d /etc/logrotate.d/triagem-bitrix` (servidor).
Verificar se as units atuais escrevem `ponte.log`/`certidoes.log` por
`StandardOutput=append:` — se sim, manter; a alternativa é journal puro
(`journalctl -u triagem-bitrix-ponte`) com `SystemMaxUse` no journald (**TODO
orquestrador**).

## 7. Segredos e rotação de credencial (S06/S07)

- Fonte única: `/etc/triagem-bitrix/` (0700) com `webhook_bitrix`, `vibecode_key`,
  `bot_token_100003` (0600) e `env` (0600) — lidos por `seguranca.ler_segredo` /
  `EnvironmentFile`. Depois do `chmod 600` dos legados, ligar
  `TRIAGEM_SEGREDOS_ESTRITO=1`.
- **Rotacionar o webhook do usuário 1** (auditado como admin, 63 escopos) por webhook de
  usuário técnico com escopo mínimo (`im`, `disk`, `task`): a URL antiga está hardcoded
  em outros projetos do servidor (`bitrix24_mcp_server.py`, PrevSync-CRM — fora deste
  repositório) e em logs antigos (S07: `triagem_grupo.py:450,452` sem máscara).
  Depois da rotação, limpar/rotacionar os logs que a receberam.

## 8. Backup/restore do estado

- Backup: `estado_store.fazer_backup()` (0600 em `backups-estado/`); os legados `.bak-*`
  continuam válidos para restore manual.
- Restore (parar **todo escritor do estado** primeiro — timers, bot `--loop` e, se existir,
  `triagem-bitrix.service`; ver C2):

```bash
sudo systemctl stop triagem-lote-fila.timer triagem-bitrix-ponte.timer triagem-limpeza.timer
sudo systemctl stop triagem-bitrix-bot.service triagem-bitrix.service 2>/dev/null || true
while systemctl is-active -q triagem-lote-fila.service triagem-bitrix-ponte.service; do sleep 10; done
sudo flock -w 60 /opt/triagem-bitrix/estado.json.lock bash -c '
  cp -p /opt/triagem-bitrix/estado.json /opt/triagem-bitrix/estado.json.pre-restore &&
  cp -p /opt/triagem-bitrix/backups-estado/estado.json.bak-<TS> /opt/triagem-bitrix/estado.json'
sudo systemctl start triagem-bitrix-bot.service
sudo systemctl start triagem-bitrix.service 2>/dev/null || true   # só se existir (C2)
sudo systemctl start triagem-lote-fila.timer triagem-bitrix-ponte.timer triagem-limpeza.timer
```

Conferir integridade: `python3 -c "import json; json.load(open('/opt/triagem-bitrix/estado.json'))"`
e `monitor_lotes.py --detalhe`.

## 9. Manutenção das units

- Instalar/atualizar: `deploy/systemd/README.md`; instalação por fase em
  `PLANO-IMPLANTACAO.md`.
- Trocar parâmetro (limite, dias): **não** editar `ExecStart` (é `ponte-rodar.sh` /
  `deploy/bin/lote-fila.sh`). Ponte: `PONTE_LIMITE`/`PONTE_DIAS` (`Environment=` na unit ou
  drop-in). Passo 5 do lote-fila: `FILA_LIMITE` (padrão 15), `DIAS`, `CPF_LIMITE` e timeouts
  `T_BAIXAR/T_RECONCILIAR/T_FILEIDS/T_CPF/T_FILA` no `/etc/triagem-bitrix/env` ou drop-in. Mudou
  um `T_*`? `TimeoutStartSec` do lote-fila tem de ser ≥ a soma (hoje 1680 s → 30 min).
- `pipeline.sh` é o pipeline **manual**, não equivalente à unit (ponte com limite 4, sem
  timeout por passo) — não usar como `ExecStart`.
- Trava ocupada não é falha: `pintar-linha` sai 0 (`flock -E 0`), `triagem-limpeza` sai 75
  (`SuccessExitStatus=75`) — nenhum dos dois dispara alerta.
- **Lembrete PT-02:** só UM modo do bot por vez (`triagem-bitrix-bot` --loop OU timer de
  rodada única). Duas instâncias: a segunda sai 75 e não deve reiniciar
  (`SuccessExitStatus=75` já está na unit proposta).

## 10. Validação pós-deploy (checklist)

1. `deploy/bin/verificar-ambiente.sh` → 0 erros.
2. `systemd-analyze verify /etc/systemd/system/triagem-*.service` (servidor).
3. `logrotate -d /etc/logrotate.d/triagem-bitrix` (servidor).
4. `systemctl list-timers` mostra os 6 timers.
5. Uma rodada manual de cada service sem `failed` (`systemctl status`).
6. `podar-backups.py` em modo simulação confere a política antes do primeiro `--aplicar`.
