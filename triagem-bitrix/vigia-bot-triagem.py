#!/usr/bin/env python3
"""Vigia do bot de triagem (100003) — auto-recupera o BOT_ACCESS_DENIED.

O bot fica desvinculado da chave quando a chave do VibeCode é rotacionada
(causa do incidente de 12/10/2026 — bot mudo, triagem parecendo parada).

Este vigia testa o acesso do bot a cada ciclo. Se detectar BOT_ACCESS_DENIED,
transfere o bot de volta para a chave atual AUTOMATICAMENTE — o próprio erro
carrega o `targetApiKeyId` (error.fix.body) e a rota de transferência.

Saída vazia      = OK (bot vinculado) — o agendador não envia nada.
Mensagem 🔁      = auto-recuperação executada (evento raro, informa).
Mensagem ⚠️      = falha (transfer/reauth) — o agendador alerta.

Falha de rede: conta as falhas CONSECUTIVAS em .vigia-estado.json (ao lado do
script). Abaixo do limiar é transitório e sai em silêncio; ao cruzar o limiar
emite UMA linha ⚠️ e sai com 1 (dedupe: a sequência seguinte de falhas alerta
de novo, mas não repete todo ciclo). Rede de volta zera o contador.

H2 (auditoria 03): a transferência automática só acontece se o
`targetApiKeyId` do erro for o id da chave LOCAL — configurado em
$VIGIA_API_KEY_ID / $TRIAGEM_VIBECODE_KEY_ID / <BASE>/vibecode.key.id, ou lido
de GET /v1/me. Sem id local confirmado: alerta e NÃO transfere (antes o vigia
transferia o bot para qualquer chave que o corpo da resposta indicasse).

Códigos de saída: 0 ok/silêncio · 1 alerta (o agendador avisa).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

_AQUI = os.path.dirname(os.path.abspath(__file__))
if _AQUI not in sys.path:
    sys.path.insert(0, _AQUI)
import caminhos  # noqa: E402
import seguranca  # noqa: E402

BOT_ID = 100003
BASE = "https://vibecode.bitrix24.com/v1"
CHAVE_FILE = caminhos.em_base("vibecode.key")
ARQ_ID_CHAVE = caminhos.em_base("vibecode.key.id")

# Falhas de rede seguidas antes de alertar: 3 ciclos transitórios são silêncio,
# 3 em sequência já é sinal de que o bot (ou a rede) está fora do ar.
LIMIAR_FALHAS_REDE = 3
# Estado do contador AO LADO DO SCRIPT (nunca em /opt): sobrevive ao cwd do cron.
ARQ_ESTADO_VIGIA = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".vigia-estado.json"
)


def _mascarar(texto) -> str:
    """Texto seguro para log: seguranca.mascarar (frente S)."""
    return seguranca.mascarar(str(texto))


def _chave() -> str:
    """Chave do VibeCode: seguranca.ler_segredo("vibecode_key");
    se recusar/não achar, o arquivo `<BASE>/vibecode.key` (antes: /opt literal)."""
    try:
        k = seguranca.ler_segredo("vibecode_key", obrigatorio=False)
        if k:
            return str(k).strip()
    except (seguranca.SegredoAusente, OSError):
        pass
    with open(CHAVE_FILE, encoding="utf-8") as fh:
        k = fh.read().strip()
    if not k:
        raise RuntimeError(f"{CHAVE_FILE} vazio")
    return k


def _extrair_id_chave(corpo) -> str | None:
    """Id da chave no corpo de GET /v1/me (o formato não está na doc local:
    aceita só campos com nome explícito de id de chave)."""
    d = (corpo or {}).get("data") if isinstance(corpo, dict) else None
    if not isinstance(d, dict):
        return None
    for campo in ("apiKeyId", "keyId", "api_key_id"):
        v = d.get(campo)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    for sub in ("apiKey", "key"):
        v = d.get(sub)
        if isinstance(v, dict) and isinstance(v.get("id"), (str, int)) and str(v["id"]).strip():
            return str(v["id"]).strip()
    return None


def _id_chave_esperado() -> str | None:
    """Id da chave LOCAL (H2): env > arquivo <BASE>/vibecode.key.id > GET /v1/me."""
    for var in ("VIGIA_API_KEY_ID", "TRIAGEM_VIBECODE_KEY_ID"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    try:
        with open(ARQ_ID_CHAVE, encoding="utf-8") as fh:
            v = fh.read().strip()
        if v:
            return v
    except OSError:
        pass
    st, d = _req("GET", "/me")
    return _extrair_id_chave(d) if st == 200 else None


def _req(method: str, path: str, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("X-Api-Key", _chave())
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"_exc": f"{type(e).__name__}: {e}"}


def _estado_ler() -> dict:
    """Lê o contador de falhas de rede. Arquivo ausente/corrompido vira estado
    zerado — a leitura nunca derruba o vigia."""
    try:
        with open(ARQ_ESTADO_VIGIA, encoding="utf-8") as fh:
            est = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(est, dict):
        return {}
    try:
        est["falhas_consecutivas"] = int(est.get("falhas_consecutivas") or 0)
    except (TypeError, ValueError):
        est["falhas_consecutivas"] = 0
    return est


def _estado_gravar(est: dict) -> bool:
    """Grava o estado de forma atômica (tmp + os.replace). Devolve False se o I/O
    falhar; o problema vai para o stderr — nunca para o stdout do Hermes."""
    try:
        tmp = ARQ_ESTADO_VIGIA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(est, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, ARQ_ESTADO_VIGIA)
        return True
    except OSError as e:
        print(f"vigia-bot-triagem: não consegui gravar o estado em "
              f"{ARQ_ESTADO_VIGIA}: {e}", file=sys.stderr)
        return False


def classificar_resposta(st, corpo) -> str:
    """O que a resposta do /events significa.

    'ok' | 'rede' | 'transitorio' | 'ocupado' | 'negado' | 'inesperado'

    🔴 06/11/2026: 502/503 do VibeCode (code=BITRIX_UNAVAILABLE) caíam no ramo
    "resposta inesperada" e alertavam o dono no PRIMEIRO ciclo. Upstream de terceiro
    piscando é transitório — segue a mesma disciplina do 429: silêncio até
    LIMIAR_FALHAS_REDE ciclos seguidos, e um alerta só por sequência.
    """
    corpo = corpo or {}
    if st is None:
        return "rede"
    if st == 200:
        return "ok"
    err = corpo.get("error") or {}
    codigo = err.get("code")
    if codigo == "BOT_ACCESS_DENIED":
        return "negado"
    if codigo == "BOT_EVENTS_BUSY" or st == 409:
        return "ocupado"
    # Transitório = o upstream piscou e volta sozinho: rate limit, Bitrix indisponível,
    # gateway/timeout (408/429/502/503/504). 500 "seco" (bug do servidor) NÃO entra
    # aqui — esse tem de alertar na hora, como sempre foi (teste do próprio repo).
    if codigo in ("RATE_LIMITED", "BITRIX_UNAVAILABLE") or st in (408, 429, 502, 503, 504):
        return "transitorio"
    return "inesperado"


def _zerar_contador() -> None:
    est = _estado_ler()
    if est.get("falhas_consecutivas") or est.get("alertado"):
        est["falhas_consecutivas"] = 0
        est["alertado"] = False
        _estado_gravar(est)


def _conta_falha(st, d, rotulo: str) -> int:
    """Conta falha transitória: silêncio até o limiar, UM alerta por sequência.

    Devolve 0 (ainda silencioso) ou 1 (a partir do limiar — o cron marca falha,
    mas o texto só sai uma vez por sequência).
    """
    est = _estado_ler()
    n = est.get("falhas_consecutivas", 0) + 1
    est["falhas_consecutivas"] = n
    est["ultima_falha_em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    detalhe = _mascarar((d or {}).get("_exc") or (
        f"HTTP {st} {((d or {}).get('error') or {}).get('code') or ''}".strip()))
    if not _estado_gravar(est):
        # Fail-loud: sem estado não há dedupe nem como atingir o limiar.
        print(f"⚠️ Vigia bot triagem: {rotulo} ({detalhe}) e estado ilegível em "
              f"{ARQ_ESTADO_VIGIA} — bot possivelmente fora do ar.")
        return 1
    if n < LIMIAR_FALHAS_REDE:
        return 0
    if not est.get("alertado"):
        est["alertado"] = True
        _estado_gravar(est)
        print(f"⚠️ Vigia bot triagem: {n} ciclos seguidos com {rotulo} ({detalhe}) "
              f"— bot possivelmente fora do ar.")
    return 1


def main() -> int:
    st, d = _req("GET", f"/bots/{BOT_ID}/events")
    tipo = classificar_resposta(st, d)

    # Resposta conclusiva (ok/ocupado/negado/inesperado) zera o contador: uma nova
    # sequência de falhas volta a alertar. 'rede' e 'transitorio' NÃO zeram.
    if tipo in ("ok", "ocupado", "negado", "inesperado"):
        _zerar_contador()

    if tipo in ("ok", "ocupado"):
        # 200 = bot lendo; 409 BOT_EVENTS_BUSY = bot VIVO e ocupado no long-poll.
        return 0

    if tipo == "rede":
        return _conta_falha(None, d, "falha de rede")

    if tipo == "transitorio":
        return _conta_falha(st, d, "instabilidade transitória do upstream")

    if tipo == "inesperado":
        err = (d or {}).get("error") or {}
        print(f"⚠️ Vigia bot triagem: resposta inesperada (HTTP {st}, "
              f"code={err.get('code')}) — sem ação automática.")
        return 1

    # ------------------- daqui pra baixo: BOT_ACCESS_DENIED (auto-recuperação)
    err = (d or {}).get("error") or {}
    fix = err.get("fix") or {}
    target = (fix.get("body") or {}).get("targetApiKeyId")
    if not target:
        print("⚠️ Vigia bot triagem: BOT_ACCESS_DENIED sem targetApiKeyId "
              "(intervenção manual necessária).")
        return 1

    # H2: nunca transferir para uma chave que só o corpo da resposta indica.
    esperado = _id_chave_esperado()
    if not esperado:
        print("⚠️ Vigia bot triagem: BOT_ACCESS_DENIED, mas o id da chave local não está "
              "configurado (VIGIA_API_KEY_ID ou vibecode.key.id) nem veio de /v1/me — "
              "NÃO transfiro automaticamente (intervenção manual necessária).")
        return 1
    if str(target) != str(esperado):
        print("⚠️ Vigia bot triagem: BOT_ACCESS_DENIED indica transferir para OUTRA chave "
              "(targetApiKeyId diferente da chave local) — NÃO transfiro "
              "(intervenção manual necessária).")
        return 1

    st2, d2 = _req("POST", f"/bots/{BOT_ID}/transfer", {"targetApiKeyId": target})
    if not (st2 == 200 and (d2.get("data") or {}).get("transferred")):
        print(f"⚠️ Vigia bot triagem: transferência falhou (HTTP {st2}: "
              f"{_mascarar(json.dumps(d2))[:200]}).")
        return 1

    st3, d3 = _req("POST", f"/bots/{BOT_ID}/reauth")
    if st3 == 200 and (d3.get("data") or {}).get("validated"):
        print("🔁 Vigia bot triagem: bot 100003 foi re-vinculado à chave "
              "automaticamente (BOT_ACCESS_DENIED detectado e corrigido).")
        return 0
    print(f"⚠️ Vigia bot triagem: transferência ok, mas reauth falhou "
          f"(HTTP {st3}: {_mascarar(json.dumps(d3))[:200]}).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
