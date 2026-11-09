#!/bin/bash
# verificar-ambiente.sh — checagens SOMENTE DE LEITURA do ambiente da triagem (S08).
#
# Nada é criado, modificado ou apagado. A única interação com o disco além de stat/df
# é uma sonda de trava (`flock -n`) em arquivos de lock JÁ EXISTENTES — adquire e solta
# na hora; se a trava estiver livre, a sonda prova que o lock não está órfão; se estiver
# ocupada, o dono está vivo (também não é órfão).
#
# Saída: uma linha por checagem com OK | AVISO | ERRO | INFO; exit 1 se houver ERRO,
# 2 se só AVISO, 0 se tudo OK. `set -euo pipefail` NÃO é usado de propósito: o script
# precisa continuar após cada checagem falha para reunir o quadro inteiro.
set -uo pipefail

BASE="${TRIAGEM_BASE:-/opt/triagem-bitrix}"
SEPARADOR="${AGENTE_SEPARADOR_BASE:-/opt/agente-separador}"
SEGREDOS="${TRIAGEM_SEGREDOS_DIR:-/etc/triagem-bitrix}"
MIN_PCT_LIVRE="${MIN_PCT_LIVRE:-10}"      # aviso abaixo disso
MIN_PCT_CRITICO="${MIN_PCT_CRITICO:-5}"   # erro abaixo disso

ERROS=0
AVISOS=0

ok()   { echo "OK    $*"; }
aviso(){ echo "AVISO $*"; AVISOS=$((AVISOS+1)); }
erro() { echo "ERRO  $*"; ERROS=$((ERROS+1)); }
info() { echo "INFO  $*"; }

# modo octal de um arquivo (Linux: stat -c %a; BSD/macOS: stat -f %Lp)
modo_de() {
  stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null || echo "???"
}

dono_de() {
  stat -c %U:%G "$1" 2>/dev/null || stat -f %Su:%Sg "$1" 2>/dev/null || echo "?:?"
}

# --- 1) segredos: 0600 (arquivo) / 0700 (diretório), dono root ou triagem -------------
echo "== 1. segredos e permissões =="
check_segredo() {
  local arq="$1" rot="$2"
  if [ ! -e "$arq" ]; then
    info "$rot: $arq ausente (ok se o segredo veio por EnvironmentFile)"
    return
  fi
  if [ ! -f "$arq" ]; then
    erro "$rot: $arq não é arquivo regular"
    return
  fi
  local m d
  m=$(modo_de "$arq"); d=$(dono_de "$arq")
  case "$m" in
    600|400) ok "$rot: $arq modo $m" ;;
    *)       erro "$rot: $arq modo $m (esperado 600) — chmod 600" ;;
  esac
  case "$d" in
    root:*|triagem:*) ok "$rot: dono $d" ;;
    *) aviso "$rot: dono $d (esperado root:root ou triagem:triagem)" ;;
  esac
}

if [ -d "$SEGREDOS" ]; then
  m=$(modo_de "$SEGREDOS")
  case "$m" in
    700) ok "diretório de segredos $SEGREDOS modo 700" ;;
    *)   erro "diretório de segredos $SEGREDOS modo $m (esperado 700)" ;;
  esac
  for f in "$SEGREDOS"/*; do
    [ -e "$f" ] && check_segredo "$f" "segredo"
  done
else
  aviso "diretório de segredos $SEGREDOS não existe (S06: fonte única de credencial)"
fi
check_segredo "$SEGREDOS/env" "EnvironmentFile"
check_segredo "$BASE/vibecode.key" "chave VibeCode (legado)"
check_segredo "/root/.vibecode/api_key" "chave VibeCode (legado raiz)"
check_segredo "/root/.telegram/documentosexemplo.token" "token Telegram (legado — canal desativado)"
check_segredo "/tmp/b24_webhook.txt" "webhook em /tmp (S06 — deve ser eliminado)"
if [ -e "/tmp/b24_webhook.txt" ]; then
  erro "S06: /tmp/b24_webhook.txt existe — webhook legado em diretório world-writable"
fi

# --- 2) dono/permissão de diretórios e arquivos de estado ------------------------------
echo
echo "== 2. diretórios e dados =="
check_dir() {
  local d="$1" rot="$2"
  if [ ! -d "$d" ]; then
    info "$rot: $d ausente"
    return
  fi
  local m
  m=$(modo_de "$d")
  case "$m" in
    700) ok "$rot: $d modo 700" ;;
    *)   aviso "$rot: $d modo $m (S08: restringir a 0700 depois da migração p/ usuário triagem)" ;;
  esac
}
check_dir "$BASE" "base da triagem"
check_dir "$BASE/lotes" "documentos de cliente"
check_dir "$BASE/backups" "backups da fila"
check_dir "$BASE/backups-estado" "backups do estado"
check_dir "$BASE/tmp" "temporários privados"
check_dir "$SEPARADOR/data" "dados do separador (staging/fila)"
check_dir "$SEPARADOR/data/staging" "staging do worker"

if [ -f "$BASE/estado.json" ]; then
  m=$(modo_de "$BASE/estado.json")
  case "$m" in
    600|400) ok "estado.json modo $m" ;;
    *)       erro "estado.json modo $m (S08: esperado 600 — CPF/senha de cliente)" ;;
  esac
else
  info "estado.json ausente em $BASE"
fi

# --- 3) espaço em disco ----------------------------------------------------------------
echo
echo "== 3. espaço em disco =="
for alvo in "$BASE" "$SEPARADOR"; do
  [ -d "$alvo" ] || continue
  pct=$(df -P "$alvo" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
  livre=$(df -Ph "$alvo" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -z "$pct" ]; then
    info "df indisponível para $alvo"
    continue
  fi
  if [ "$pct" -ge $((100 - MIN_PCT_CRITICO)) ]; then
    erro "$alvo: ${pct}% usado (livre: $livre) — risco de perder escrita do estado"
  elif [ "$pct" -ge $((100 - MIN_PCT_LIVRE)) ]; then
    aviso "$alvo: ${pct}% usado (livre: $livre)"
  else
    ok "$alvo: ${pct}% usado (livre: $livre)"
  fi
done

# --- 4) locks órfãos (sonda de leitura: flock -n em arquivo existente) ------------------
echo
echo "== 4. travas (locks) =="
check_lock() {
  local lk="$1" rot="$2"
  if [ ! -e "$lk" ]; then
    info "$rot: $lk não existe (nenhuma rodada registrada)"
    return
  fi
  if ! command -v flock >/dev/null 2>&1; then
    info "$rot: $lk existe (sem o comando flock para sondar — Linux only)"
    return
  fi
  if flock -n "$lk" true 2>/dev/null; then
    ok "$rot: $lk livre (sem rodada ativa; arquivo residual é normal)"
  else
    ok "$rot: $lk ocupada — há uma rodada em andamento"
  fi
}
check_lock "$BASE/fila.lock" "trava compartilhada lote-fila/ponte (fila.lock)"
check_lock "/var/lock/triagem-bitrix-ponte.lock" "trava ANTIGA da ponte (pré-remediação; deve sumir de uso)"
check_lock "/var/lock/triagem-bitrix-certidoes.lock" "trava das certidões"
check_lock "/var/lock/pintar-linha-triagem.lock" "trava do pintar-linha"
check_lock "$BASE/estado.json.lock" "trava do estado (estado_store)"
check_lock "$BASE/pergunta-traco.lock" "trava de instância do bot do traço"

# --- 5) retenção: quanto pesa o passivo de backups (S05) --------------------------------
echo
echo "== 5. retenção (S05) =="
conta_padrao() {
  local dir="$1" padrao="$2" rot="$3"
  local n tam
  n=$(find "$dir" -maxdepth 1 -name "$padrao" -type f 2>/dev/null | wc -l | tr -d ' ')
  tam=$(find "$dir" -maxdepth 1 -name "$padrao" -type f -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
  if [ "${n:-0}" -gt 100 ]; then
    aviso "$rot: $n arquivo(s), ${tam:-?} — aplicar podar-backups.py (24 + 1/dia x 14)"
  else
    info "$rot: $n arquivo(s), ${tam:-?}"
  fi
}
conta_padrao "$BASE" "estado.json.bak-*" "backups legados do estado (contêm senha — S01)"
conta_padrao "$BASE/backups-estado" "estado.json.bak-*" "backups novos do estado"
conta_padrao "$BASE/backups" "fila-jobs-*.json" "backups da fila"
conta_padrao "$BASE" "estado.json.corrompido-*" "cópias de estado corrompido (recuperação)"
conta_padrao "/tmp" "_entradas.json" "resíduo S09 (/tmp/_entradas.json)"
conta_padrao "/tmp" "certidao_*.pdf" "resíduo S09 (certidões em /tmp)"

# --- 6) unidades e logrotate (só presença) ---------------------------------------------
echo
echo "== 6. componentes instalados =="
for u in triagem-lote-fila.timer triagem-bitrix-ponte.timer triagem-bitrix-certidoes.timer \
         pintar-linha-triagem.timer triagem-bitrix-bot.service triagem-limpeza.timer; do
  if [ -f "/etc/systemd/system/$u" ]; then
    ok "unit $u instalada"
  else
    info "unit $u não encontrada em /etc/systemd/system (conferir no servidor)"
  fi
done
if [ -f /etc/logrotate.d/triagem-bitrix ]; then
  ok "logrotate /etc/logrotate.d/triagem-bitrix instalado"
else
  aviso "logrotate /etc/logrotate.d/triagem-bitrix ausente (OP-O3: logs sem rotação)"
fi

echo
echo "== resumo: $ERROS erro(s), $AVISOS aviso(s) =="
if [ "$ERROS" -gt 0 ]; then exit 1; fi
if [ "$AVISOS" -gt 0 ]; then exit 2; fi
exit 0
