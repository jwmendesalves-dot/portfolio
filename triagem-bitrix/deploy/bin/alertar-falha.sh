#!/bin/bash
# alertar-falha.sh — handler de OnFailure=triagem-alerta@%n.service (OP-O4/S08).
#
# Recebe o nome da unit que falhou, registra o contexto no journal e, se o ambiente
# definir ALERTA_CMD, executa o comando de notificação (um único ponto de saída para
# onde a alerta for — Telegram legado está desativado; destino real = TODO orquestrador,
# "conferir no servidor").
#
# Nunca imprime conteúdo de log de cliente nem segredo: só nomes de unit e contagens.
set -euo pipefail

UNIT="${1:?uso: alertar-falha.sh <unit-falha>}"
TAG="triagem-alerta"

echo "[$TAG] unit com falha: $UNIT" >&2

# F2-06: a alerta sai PRIMEIRO — nada da coleta de contexto abaixo (journalctl sem
# permissão, systemctl ausente) pode impedir a notificação.
if [ -n "${ALERTA_CMD:-}" ]; then
  echo "[$TAG] executando ALERTA_CMD" >&2
  # ALERTA_CMD vem do EnvironmentFile 0600 (/etc/triagem-bitrix/env) — nunca do argv
  eval "$ALERTA_CMD" || echo "[$TAG] ALERTA_CMD falhou (exit $?)" >&2
else
  echo "[$TAG] ALERTA_CMD não definido — só journal (TODO orquestrador: destino da alerta)" >&2
fi

if command -v systemctl >/dev/null 2>&1; then
  # resumo curto da falha, sem conteúdo sensível (Resultado/ExecMainStatus)
  systemctl show "$UNIT" \
    -p Id -p Result -p ExecMainStatus -p ActiveState -p UnitFileState 2>/dev/null >&2 || true
  # journalctl sai ≠ 0 sem permissão (User=triagem fora de systemd-journal/adm): com
  # pipefail + set -e isso mataria o script — o `|| N=?` segura.
  N=$(journalctl -u "$UNIT" -n 3 --no-pager -o cat 2>/dev/null | wc -l | tr -d ' ') || N="?"
  echo "[$TAG] últimas linhas do journal disponíveis via: journalctl -u $UNIT -n 50 ($N lidas)" >&2
else
  echo "[$TAG] systemctl indisponível neste host" >&2
fi

exit 0
