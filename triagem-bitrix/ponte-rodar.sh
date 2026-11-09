#!/bin/bash
# Rodada automática da PONTE da fase 2 (lote pronto do grupo -> fila do painel).
# Chamada pelo timer triagem-bitrix-ponte.timer e pelo passo 4 do pipeline.sh.
#
# Trava ÚNICA da ponte (F2-01/F2-05/A4): "$BASE/fila.lock" — a mesma que o
# lote_para_fila.py usa por dentro (estado_store.trava_arquivo(em_base("fila"))).
# O fd 9 segurado aqui é passado ao Python em TRIAGEM_FILA_LOCK_FD: ele confere que
# é o mesmo arquivo e não tenta travar de novo (reabrir brigaria com esta trava).
#
# Padrões da ponte (só mude de propósito):
#   PONTE_LIMITE (4)  — lotes ENVIADOS por rodada (contado depois dos filtros)
#   PONTE_DIAS   (7)  — janela pelo início do lote; catch-up é manual (--dias 0)
set -u
BASE="${TRIAGEM_BASE:-/opt/triagem-bitrix}"
PY="${PYTHON:-/usr/bin/python3}"
LIMITE="${PONTE_LIMITE:-4}"
DIAS="${PONTE_DIAS:-7}"
LOCK="$BASE/fila.lock"

# trava: se uma rodada anterior (timer, pipeline ou manual) ainda está rodando, sai
command -v flock >/dev/null 2>&1 || { echo "=== flock ausente — sem trava não roda (instale util-linux)" >&2; exit 2; }
exec 9>>"$LOCK"
if ! flock -n 9; then
  echo "=== [$(date +%Y-%m-%dT%H:%M:%S%z)] rodada anterior da ponte ainda em andamento ($LOCK) — pulando esta"
  exit 0
fi
export TRIAGEM_FILA_LOCK_FD=9

echo "=== [$(date +%Y-%m-%dT%H:%M:%S%z)] PONTE --enviar --todos --limite $LIMITE --dias $DIAS"
cd "$BASE" || exit 1
"$PY" "$BASE/lote_para_fila.py" --enviar --todos --limite "$LIMITE" --dias "$DIAS"
code=$?
echo "=== [$(date +%Y-%m-%dT%H:%M:%S%z)] fim da rodada (exit $code)"
exit $code
