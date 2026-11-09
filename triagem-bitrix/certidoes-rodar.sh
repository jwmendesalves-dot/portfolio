#!/bin/bash
# Rodada automática das CERTIDÕES DE NASCIMENTO (projeto + grupo -> tarefa no negócio).
# Chamada pelo timer triagem-bitrix-certidoes.timer. Trava p/ não sobrepor rodadas.
#
# OP-O4/CE-05: set -euo pipefail + propagação do CÓDIGO DE SAÍDA REAL do certidoes.py
# para o systemd (antes o último `echo` engolia a falha e o serviço saía sempre 0).
# Sobrescrevíveis por ambiente (usado pelos testes; em produção os padrões valem):
#   TRIAGEM_BASE, TRIAGEM_LOCK, PYTHON
set -euo pipefail
BASE="${TRIAGEM_BASE:-/opt/triagem-bitrix}"
PYTHON="${PYTHON:-/usr/bin/python3}"
LOCK="${TRIAGEM_LOCK:-/var/lock/triagem-bitrix-certidoes.lock}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "=== [$(date -Is)] rodada anterior ainda em andamento — pulando esta"
  exit 0
fi
echo "=== [$(date -Is)] CERTIDOES --enviar"
code=0
"$PYTHON" "$BASE/certidoes.py" --enviar || code=$?
echo "=== [$(date -Is)] fim da rodada (exit $code)"
exit "$code"
