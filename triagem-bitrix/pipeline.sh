#!/bin/bash
# Pipeline manual da triagem (22/10/2026).
# Ordem importa: baixar -> reconciliar estado com o disco -> completar CPF por
# nome (Bitrix) -> enfileirar.
# Sem o reconciliar o lote_para_fila ve "arquivo ausente" e o lote fica fora.
#
# F2-01: o passo 5 é a MESMA rodada da ponte (ponte-rodar.sh): regra do escritório
# obrigatória (nada de --todos-os-fechamentos), --dias/--limite padrão da ponte e a
# MESMA trava ($BASE/fila.lock) — nunca roda em paralelo com o timer.
#
# Códigos de saída (os scripts agora devolvem 1 = atenção/permanente, 2 = estado
# ilegível/interrompido de verdade): cada passo roda mesmo que o anterior falhe
# (são independentes — a ponte decide sozinha pelo estado_lote.pode_enfileirar),
# o código REAL de cada um é registrado (sem `| tail` engolindo: PIPESTATUS) e o
# pipeline termina com o PIOR código visto.
set -uo pipefail
BASE="${TRIAGEM_BASE:-/opt/triagem-bitrix}"
PY="${PYTHON:-python3}"
cd "$BASE" || exit 2
pior=0
passo() {  # passo <linhas-de-saida> <cmd...>
  local n="$1"; shift
  "$@" 2>&1 | tail -n "$n"
  local rc="${PIPESTATUS[0]}"
  [ "$rc" -ne 0 ] && echo "    -> saída $rc: $*"
  [ "$rc" -gt "$pior" ] && pior="$rc"
  return 0
}
echo "=== 1/5 baixando pendentes ==="
passo 4 "$PY" baixar_pendentes.py
echo "=== 2/5 reconciliando estado x disco (por índice) ==="
passo 2 "$PY" reconciliar_estado.py
echo "=== 3/5 reconciliando metadados por file_ids (só com prova, 7 dias) ==="
passo 4 "$PY" reconciliar_por_file_ids.py --dias 7 --aplicar
echo "=== 4/5 completando CPF por nome (Bitrix, até 30 lotes) ==="
passo 4 "$PY" completar_cpf_por_nome.py --aplicar --limite 30
echo "=== 5/5 enfileirando (mesma rodada/trava da ponte) ==="
passo 20 env PYTHON="$PY" bash "$BASE/ponte-rodar.sh"
echo "=== fim do pipeline (pior saída: $pior) ==="
exit "$pior"
