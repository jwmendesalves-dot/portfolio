#!/bin/bash
# Rodada única do bot do traço (timer). O código de saída REAL vai para o systemd:
#   0 ok · 1 falha · 75 outra instância do bot segura a trava (pergunta-traco.lock).
# A trava de instância única é do próprio pergunta_traco.py (PT-02, todos os modos).
# Não use `flock` aqui no MESMO arquivo: o flock do shell e o do Python se
# bloqueariam e a rodada nunca rodaria.
set -euo pipefail

BASE="${TRIAGEM_BASE:-/opt/triagem-bitrix}"
PY="${PYTHON:-/usr/bin/python3}"
LOG="${PERGUNTA_LOG:-$BASE/pergunta.log}"

cd "$BASE"
exec "$PY" pergunta_traco.py --ativo >> "$LOG" 2>&1
