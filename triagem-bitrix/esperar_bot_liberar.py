#!/usr/bin/env python3
"""
Espera o bloqueio do VibeCode (ERROR_LOOP_DETECTED) cair no envio do bot 100003.

Contexto (22/10/2026): o bot foi REMOVIDO do grupo chat100001 sem ninguém
notar. Sem estar no chat o Bitrix recusou 43x (ACCESS_DENIED) em
imbot.v2.Chat.Message.send e o proxy do VibeCode travou o envio com
ERROR_LOOP_DETECTED ("the block clears after the first successful call").

O bloqueio só cai com um ENVIO bem-sucedido (uma leitura não serve de prova), então
a sonda é uma mensagem — mas (H1, auditoria 03):
  - NUNCA no grupo de clientes: `--dialog` é obrigatório e um chat listado em
    `config.json -> pergunta_traco.chats` é recusado (use o privado de quem opera);
  - pausa CRESCENTE (1, 5, 15, 60 min — o que o próprio Bitrix pede), respeitando
    o `bot-bloqueio.json` do bot do traço; sonda a cada 2 min mantinha o bloqueio vivo;
  - ACCESS_DENIED para na hora (repetir é o que arma o bloqueio);
  - timeout/erro de rede: a sonda pode ter saído — conta como tentativa, sem repetir já;
  - a chave é lida dentro de main() (antes: na importação, com /root literal).

Uso:
    esperar_bot_liberar.py --dialog 1                # privado do usuário 1
    esperar_bot_liberar.py --dialog 1 --sem-reiniciar

Saída: 0 liberou · 1 continua bloqueado após as tentativas · 2 uso inválido ·
       3 sem acesso ao dialog (ACCESS_DENIED) · 4 chave ausente.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

_AQUI = os.path.dirname(os.path.abspath(__file__))
if _AQUI not in sys.path:
    sys.path.insert(0, _AQUI)
import caminhos  # noqa: E402
import seguranca  # noqa: E402

BOT = 100003
URL = f"https://vibecode.bitrix24.com/v1/bots/{BOT}/messages"
PAUSAS_S = (60, 300, 900, 3600)          # depois repete a última
ARQ_BLOQUEIO = caminhos.em_base("bot-bloqueio.json")
ARQ_CONFIG = caminhos.em_base("config.json")
SERVICO = "triagem-bitrix-bot"


def _mascarar(texto) -> str:
    """Texto seguro para log: seguranca.mascarar (frente S)."""
    return seguranca.mascarar(str(texto))


def ler_chave() -> str | None:
    try:
        k = seguranca.ler_segredo("vibecode_key", obrigatorio=False)
        if k:
            return str(k).strip()
    except (seguranca.SegredoAusente, OSError):
        pass
    try:
        with open(caminhos.em_base("vibecode.key"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def chats_de_clientes() -> set:
    try:
        with open(ARQ_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    chats = set((cfg.get("pergunta_traco") or {}).get("chats") or [])
    return chats | {"chat100001", "chat100002"}


def espera_bloqueio_s(agora: float | None = None) -> float:
    """Quanto falta do bloqueio gravado pelo bot do traço (0 se não há)."""
    try:
        with open(ARQ_BLOQUEIO, encoding="utf-8") as f:
            ate = float((json.load(f) or {}).get("ate") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0
    return max(0.0, ate - (time.time() if agora is None else agora))


def sondar(chave: str, dialog: str, n: int) -> tuple[str, str]:
    """Uma sonda. Devolve (resultado, detalhe): 'ok' | 'loop' | 'acesso' | 'outro' | 'incerto'."""
    corpo = {"dialogId": dialog,
             "fields": {"message": f"Teste do bot da triagem (sonda {n}, "
                                   f"{time.strftime('%H:%M')})"}}
    req = urllib.request.Request(
        URL, data=json.dumps(corpo).encode(),
        headers={"X-Api-Key": chave, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return "ok", ""
    except urllib.error.HTTPError as e:
        try:
            cod = str((json.loads(e.read().decode() or "{}").get("error") or {}).get("code") or "")
        except Exception:  # noqa: BLE001
            cod = ""
        cod = cod.upper()
        if cod == "ERROR_LOOP_DETECTED":
            return "loop", f"HTTP {e.code} {cod}"
        if "ACCESS_DENIED" in cod or e.code in (401, 403):
            return "acesso", f"HTTP {e.code} {cod}"
        return "outro", f"HTTP {e.code} {cod}"
    except Exception as e:  # noqa: BLE001
        return "incerto", _mascarar(f"{type(e).__name__}: {e}")[:120]


def main(argv=None, dormir=time.sleep) -> int:
    ap = argparse.ArgumentParser(description="Espera o bloqueio do envio do bot 100003 cair")
    ap.add_argument("--dialog", required=False, default=None,
                    help="dialog da sonda (privado de quem opera; NUNCA o grupo de clientes)")
    ap.add_argument("--tentativas", type=int, default=6)
    ap.add_argument("--sem-reiniciar", action="store_true",
                    help=f"não dá `systemctl start {SERVICO}` quando liberar")
    a = ap.parse_args(argv)

    if not a.dialog:
        print("✗ --dialog é obrigatório (ex.: --dialog 1 = privado do usuário 1)", flush=True)
        return 2
    if a.dialog in chats_de_clientes():
        print(f"✗ {a.dialog} é grupo de clientes — a sonda não vai para lá (H1)", flush=True)
        return 2
    chave = ler_chave()
    if not chave:
        print("✗ chave do VibeCode ausente (vibecode_key)", flush=True)
        return 4

    for i in range(1, max(1, a.tentativas) + 1):
        pausa = max(PAUSAS_S[min(i - 1, len(PAUSAS_S) - 1)], espera_bloqueio_s())
        print(f"aguardo {int(pausa)}s antes da sonda {i}/{a.tentativas}", flush=True)
        dormir(pausa)
        res, det = sondar(chave, a.dialog, i)
        if res == "ok":
            print(f"liberou na sonda {i}", flush=True)
            if a.sem_reiniciar:
                return 0
            r = subprocess.run(["systemctl", "start", SERVICO], check=False)
            print(f"systemctl start {SERVICO}: código {r.returncode}", flush=True)
            return 0 if r.returncode == 0 else 1
        if res == "acesso":
            print(f"✗ sem acesso ao dialog {a.dialog} ({det}) — não repito "
                  f"(repetir é o que arma o bloqueio)", flush=True)
            return 3
        print(f"sonda {i}: {res} {det}", flush=True)
    print("o bloqueio continua — falar com o suporte do VibeCode", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
