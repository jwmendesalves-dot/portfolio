#!/bin/bash
# Vigia do bot da triagem — auto-recuperação (22/10/2026).
#
# MOTIVO: em 22/10 o `pergunta_traco.py` ficou 28 min em `do_sys_poll` com uma
# conexão estabelecida ao Bitrix e 1582 bytes no recv-Q que ele nunca leu. O
# `urlopen(timeout=10)` do urllib NÃO cobre o `getaddrinfo`/TLS em todos os casos,
# então o log simplesmente parou de crescer e ninguém percebeu — o bot ficou
# "active" no systemd, mas mudo.
#
# Regra: se o `pergunta.log` não é escrito há LIMITE_MIN minutos, o bot está
# travado → reinicia. O loop normal escreve a cada 8s, então 3 min é folgado.
#
# Códigos de saída (vão para o systemd/agendador): 0 ok ou reiniciado e ativo;
# != 0 se o restart falhou ou o serviço não voltou.

set -euo pipefail

BASE="${TRIAGEM_BASE:-/opt/triagem-bitrix}"
LOG="${LOG:-$BASE/pergunta.log}"
LIMITE_MIN="${LIMITE_MIN:-3}"
SERVICO="${SERVICO:-triagem-bitrix-bot}"

if [ ! -f "$LOG" ]; then
    echo "[VIGIA] $LOG não existe — nada a vigiar"
    exit 0
fi

AGORA=$(date +%s)
# GNU (servidor) e BSD (macOS) — sem mtime legível conta como parado
MTIME=$(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG" 2>/dev/null || echo 0)
IDADE_MIN=$(( (AGORA - MTIME) / 60 ))

if [ "$IDADE_MIN" -ge "$LIMITE_MIN" ]; then
    echo "[VIGIA] bot sem escrever há ${IDADE_MIN}min (limite ${LIMITE_MIN}) — reiniciando $SERVICO"
    # guarda as últimas linhas para diagnóstico pós-fato
    # (em $BASE, não em /tmp: nome fixo em /tmp aceita symlink de terceiros)
    tail -5 "$LOG" > "$BASE/vigia-bot-ultimo.txt" 2>/dev/null || true
    if ! systemctl restart "$SERVICO"; then
        echo "[VIGIA] ERRO: systemctl restart $SERVICO falhou"
        exit 1
    fi
    sleep 8
    if systemctl is-active --quiet "$SERVICO"; then
        echo "[VIGIA] $SERVICO reiniciado e ativo"
    else
        echo "[VIGIA] ERRO: $SERVICO não voltou"
        exit 1
    fi
else
    echo "[VIGIA] ok — última escrita há ${IDADE_MIN}min"
fi
