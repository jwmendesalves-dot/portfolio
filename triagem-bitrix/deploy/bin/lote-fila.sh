#!/bin/bash
# lote-fila.sh — ExecStart ÚNICO do triagem-lote-fila.service (10 min).
#
# Por que existe (coleta real do servidor, 09/11/2026 — SERVIDOR-COLETA-20261109.md §2):
# a unit real tem 5 `ExecStart=` em cadeia. Num `Type=oneshot`, o primeiro que sai ≠ 0
# INTERROMPE os seguintes. Com o código remediado os scripts devolvem 1/2 de verdade
# (antes era sempre 0) → ex.: 1 falha permanente nova no baixar_pendentes e a fila não
# anda. Aqui os 5 passos rodam SEMPRE, o código real de cada um é registrado e o script
# termina com o PIOR código visto (0 ok · 1 atenção/permanente · 2 estado ilegível ou
# interrompido · 124 timeout do passo). O OnFailure da unit dispara 1× com o pior caso.
#
# Passos (mesmos argumentos da unit real; diferenças marcadas):
#   1 baixar_pendentes.py
#   2 reconciliar_estado.py
#   3 reconciliar_por_file_ids.py --aplicar --dias 7
#   4 completar_cpf_por_nome.py --aplicar --limite 30  (real: sem --limite; 30 = pipeline.sh
#                                                        remediado; CPF_LIMITE=0 volta ao real)
#   5 lote_para_fila.py --enviar --todos --limite 15 --dias 7 VIA ponte-rodar.sh
#     (real: Python direto, sem trava) — o passo 5 passa a usar a MESMA trava da ponte
#     ($BASE/fila.lock) e a regra do escritório (nunca --todos-os-fechamentos).
#
# TRAVA: os passos 1-4 não gravam fila-enviados.json (o estado é serializado por dentro,
# estado_store) e rodam sem trava de shell. O passo 5 entra pelo ponte-rodar.sh, que
# adquire "$BASE/fila.lock" (`flock -n`; ocupado = pula com exit 0) e exporta
# TRIAGEM_FILA_LOCK_FD para o lote_para_fila.py não travar de novo. NUNCA envolva este
# script num `flock` de fila.lock (na unit ou aqui): o ponte-rodar.sh pularia sempre
# (armadilha PT-02 — lock aninhado no mesmo arquivo). Ordem: FILA → ESTADO, nunca o inverso.
set -uo pipefail

BASE="${TRIAGEM_BASE:-/opt/triagem-bitrix}"
PY="${PYTHON:-/usr/bin/python3}"
DIAS="${FILA_DIAS:-7}"
LIMITE_FILA="${FILA_LIMITE:-15}"
CPF_LIMITE="${CPF_LIMITE:-30}"

cd "$BASE" || exit 2

command -v flock >/dev/null 2>&1 || { echo "=== flock ausente (util-linux) — a fila não roda sem trava" >&2; exit 2; }
command -v timeout >/dev/null 2>&1 || { echo "=== timeout ausente (coreutils) — nenhum passo rodaria (127)" >&2; exit 2; }
# exige o ponte-rodar.sh remediado (trava $BASE/fila.lock, a mesma do lote_para_fila.py)
if ! grep -q 'fila.lock' "$BASE/ponte-rodar.sh" 2>/dev/null; then
  echo "=== ponte-rodar.sh sem fila.lock (versão antiga) — implante o código remediado antes desta unit" >&2
  exit 2
fi

pior=0
passo() {  # passo <rótulo> <segundos> <cmd...>
  local rot="$1" lim="$2"; shift 2
  echo "=== [$(date +%Y-%m-%dT%H:%M:%S%z)] $rot ==="
  local rc=0
  timeout "$lim" "$@" || rc=$?
  [ "$rc" -ne 0 ] && echo "    -> saída $rc: $*"
  [ "$rc" -gt "$pior" ] && pior="$rc"
  return 0
}

cpf_args=(--aplicar)
[ "$CPF_LIMITE" != "0" ] && cpf_args+=(--limite "$CPF_LIMITE")

passo "1/5 baixando pendentes"                     "${T_BAIXAR:-420}"      "$PY" baixar_pendentes.py
passo "2/5 reconciliando estado x disco"           "${T_RECONCILIAR:-120}" "$PY" reconciliar_estado.py
passo "3/5 reconciliando por file_ids (${DIAS} d)" "${T_FILEIDS:-120}"     "$PY" reconciliar_por_file_ids.py --aplicar --dias "$DIAS"
passo "4/5 completando CPF por nome"               "${T_CPF:-120}"         "$PY" completar_cpf_por_nome.py "${cpf_args[@]}"
# T_FILA=900 (F2-07): igual ao TimeoutStartSec da ponte. Cada chamada node do
# lote_para_fila pode levar até 180 s (planilha + enfileirar) e o servidor tem o OCR em
# lote (ocr_documento.py) no mesmo passo; 300 s matava rodada normal no meio do
# gravar_na_fila. Soma dos limites: 420+120+120+120+900 = 1680 s → TimeoutStartSec=30min.
passo "5/5 enfileirando (ponte-rodar.sh, fila.lock, limite ${LIMITE_FILA})" "${T_FILA:-900}" \
  env PYTHON="$PY" PONTE_LIMITE="$LIMITE_FILA" PONTE_DIAS="$DIAS" bash "$BASE/ponte-rodar.sh"

echo "=== [$(date +%Y-%m-%dT%H:%M:%S%z)] fim do lote-fila (pior saída: $pior) ==="
exit "$pior"
