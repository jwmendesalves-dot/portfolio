#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""medir_fontes.py — medidor das fontes de leitura dos grupos (somente leitura).

Responde a pergunta da auditoria §07: o webhook `im.dialog.messages.get` é um
SUBCONJUNTO do grupo? A Linha Aberta (`type: lines`) se lê de verdade por
`imopenlines.session.history.get`? O bot 100003 (v2) tem `type`/`eventMode`
que o tornam a fonte primária?

Para cada chat e janela, coleta IDs de mensagem por fonte e compara, SEM texto
de mensagem, SEM nomes, SEM CPF — só contagens e intervalos de id:

  (a) im.dialog.messages.get      paginando com LAST_ID (LIMIT 50); para por
                                  "nenhum id novo" ou página vazia, NUNCA por
                                  len == LIMIT (a doc avisa que pode vir mais);
  (b) imopenlines.dialog.get      -> SESSION_ID (6º campo de entity_data_1)
      imopenlines.session.history.get -> mensagens da sessão;
  (c) imbot.v2.Bot.get            só reporta type/eventMode do bot;
  (d) VibeCode GET /v1/chats/{id}/messages  (X-Api-Key) — opcional (--vibecode).

Toda chamada Bitrix vai por `bitrix_cliente.chamar` (limitador + disjuntor).
Orçamento de requisições por execução: `--max-req` (padrão 60).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import bitrix_cliente  # noqa: E402
import seguranca  # noqa: E402

LIMITE_PAGINA = 50


# ---------------------------------------------------------------------------
# utilitários
# ---------------------------------------------------------------------------
def _chamar(metodo: str, params: dict) -> dict:
    return bitrix_cliente.chamar(metodo, params)


def _para_epoch(valor):
    """epoch float a partir de epoch numérico ou ISO 8601. None se ilegível."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None


def _tem_arquivo(m: dict) -> bool:
    """Detecta anexo numa mensagem (im.dialog -> params.FILE_ID;
    imopenlines -> params.fileId; VibeCode -> files/fileId)."""
    if not isinstance(m, dict):
        return False
    params = m.get("params") or {}
    if isinstance(params, dict):
        for chave in ("FILE_ID", "fileId", "file_id", "fileIds"):
            if params.get(chave):
                return True
    for chave in ("files", "fileId", "fileIds", "file_id"):
        if m.get(chave):
            return True
    return False


def _extrair_session_id(entity_data_1) -> int | None:
    """SESSION_ID = 6º campo (índice 5) de entity_data_1 separado por '|'."""
    if not entity_data_1:
        return None
    partes = str(entity_data_1).split("|")
    if len(partes) < 6:
        return None
    v = partes[5].strip()
    return int(v) if v.isdigit() else None


def _aplicar_janela(ids: dict, desde=None, ate=None, ultimas=None) -> dict:
    """`ids` = {id: data}. Filtra por [desde, ate] (epoch) ou pelas últimas N."""
    if ultimas is not None:
        escolhidos = sorted(ids, key=lambda k: _num(k), reverse=True)[:max(0, int(ultimas))]
        return {k: ids[k] for k in escolhidos}
    if desde is None and ate is None:
        return dict(ids)
    saida = {}
    for k, data in ids.items():
        ts = _para_epoch(data)
        if desde is not None and (ts is None or ts < desde):
            continue
        if ate is not None and (ts is not None and ts > ate):
            continue
        saida[k] = data
    return saida


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# fontes
# ---------------------------------------------------------------------------
def coletar_im_dialog(chamar_fn, dialog_id: str, max_req: int = 60,
                      limite: int = LIMITE_PAGINA):
    """(ids, tem_arquivo, requisicoes) via im.dialog.messages.get + LAST_ID."""
    ids: dict = {}
    tem_arquivo: set = set()
    reqs = 0
    last_id = None
    while reqs < max_req:
        params = {"DIALOG_ID": dialog_id, "LIMIT": int(limite)}
        if last_id is not None:
            params["LAST_ID"] = last_id
        resp = chamar_fn("im.dialog.messages.get", params)
        reqs += 1
        res = (resp or {}).get("result") or {}
        msgs = res.get("messages") or []
        if not msgs:
            break
        novos = 0
        for m in msgs:
            mid = m.get("id") if isinstance(m, dict) else None
            if mid is None:
                continue
            chave = str(mid)
            if chave in ids:
                continue
            ids[chave] = m.get("date") if isinstance(m, dict) else None
            novos += 1
            if _tem_arquivo(m):
                tem_arquivo.add(chave)
        ids_int = [int(m["id"]) for m in msgs
                   if isinstance(m, dict) and isinstance(m.get("id"), int)]
        if not ids_int or novos == 0:
            break                      # página vazia ou nenhum id novo
        last_id = min(ids_int)
    return ids, tem_arquivo, reqs


def coletar_imopenlines(chamar_fn, dialog_id: str, max_req: int = 60):
    """(ids, tem_arquivo, requisicoes, session_id, erro) via imopenlines."""
    if max_req < 2:
        return {}, set(), 0, None, "orçamento insuficiente (precisa de 2 requisições)"
    resp = chamar_fn("imopenlines.dialog.get", {"DIALOG_ID": dialog_id})
    reqs = 1
    res = (resp or {}).get("result") or {}
    session_id = _extrair_session_id(res.get("entity_data_1"))
    if session_id is None:
        return {}, set(), reqs, None, "não encontrou SESSION_ID em entity_data_1"
    resp = chamar_fn("imopenlines.session.history.get", {"SESSION_ID": session_id})
    reqs += 1
    res = (resp or {}).get("result") or {}
    message = res.get("message") or {}
    ids: dict = {}
    tem_arquivo: set = set()
    if isinstance(message, dict):
        for chave, m in message.items():
            if not isinstance(m, dict):
                continue
            mid = m.get("id") if m.get("id") is not None else chave
            mid = str(mid)
            ids[mid] = m.get("date")
            if _tem_arquivo(m):
                tem_arquivo.add(mid)
    return ids, tem_arquivo, reqs, session_id, None


def consultar_bot(chamar_fn, bot_id: int):
    """(dict com type/eventMode) via imbot.v2.Bot.get. Só reporta."""
    resp = chamar_fn("imbot.v2.Bot.get", {"botId": int(bot_id)})
    res = (resp or {}).get("result") or {}
    bot = res.get("bot") or {}
    return {"id": bot.get("id", bot_id), "type": bot.get("type"),
            "eventMode": bot.get("eventMode")}


def coletar_vibecode(api_get_fn, dialog_id: str, max_req: int = 60):
    """(ids, tem_arquivo, requisicoes) via VibeCode GET /v1/chats/{id}/messages.

    `api_get_fn(dialog_id, params) -> dict` devolve o JSON cru
    ({success, data:{messages:[...]}}). Mais novas primeiro, paginação por lastId.
    """
    ids: dict = {}
    tem_arquivo: set = set()
    reqs = 0
    last_id = None
    while reqs < max_req:
        params = {"limit": LIMITE_PAGINA}
        if last_id is not None:
            params["lastId"] = last_id
        resp = api_get_fn(dialog_id, params)
        reqs += 1
        dados = (resp or {}).get("data") or {}
        msgs = dados.get("messages") or []
        if not msgs:
            break
        novos = 0
        for m in msgs:
            mid = m.get("id") if isinstance(m, dict) else None
            if mid is None:
                continue
            chave = str(mid)
            if chave in ids:
                continue
            ids[chave] = m.get("date") if isinstance(m, dict) else None
            novos += 1
            if _tem_arquivo(m):
                tem_arquivo.add(chave)
        ids_int = [int(m["id"]) for m in msgs
                   if isinstance(m, dict) and isinstance(m.get("id"), int)]
        if not ids_int or novos == 0:
            break
        menor = min(ids_int)
        if last_id is not None and menor >= last_id:
            break
        last_id = menor
    return ids, tem_arquivo, reqs


# ---------------------------------------------------------------------------
# comparação
# ---------------------------------------------------------------------------
def _resumo_fonte(ids: dict, tem_arquivo: set, reqs: int) -> dict:
    numeros = [_num(k) for k in ids if _num(k)]
    return {
        "quantidade": len(ids),
        "min_id": min(numeros) if numeros else None,
        "max_id": max(numeros) if numeros else None,
        "com_arquivo": len(set(ids) & tem_arquivo),
        "requisicoes": reqs,
    }


def _diferencas(fontes: dict) -> dict:
    """Diferenças par a par: ids só em A, só em B, em ambos (contagens)."""
    nomes = sorted(fontes)
    pares = {}
    for i, a in enumerate(nomes):
        for b in nomes[i + 1:]:
            ca = set(fontes[a])
            cb = set(fontes[b])
            pares[f"{a} × {b}"] = {
                f"so_{a}": len(ca - cb),
                f"so_{b}": len(cb - ca),
                "em_ambos": len(ca & cb),
            }
    return pares


# ---------------------------------------------------------------------------
# medição de um chat
# ---------------------------------------------------------------------------
def medir_chat(chat: str, chamar_fn=_chamar, api_get_fn=None, *, desde=None,
               ate=None, ultimas=None, max_req: int = 60, bot_id: int = 100003,
               com_bot: bool = True, com_vibecode: bool = False) -> dict:
    """Coleta e compara as fontes de UM chat. Devolve o dict de contagens."""
    saida: dict = {"chat": chat, "fontes": {}, "diferencas": {}, "bot": None}

    ids_im, arq_im, reqs_im = coletar_im_dialog(chamar_fn, chat, max_req=max_req)
    ids_im = _aplicar_janela(ids_im, desde, ate, ultimas)
    saida["fontes"]["im_dialog"] = _resumo_fonte(ids_im, arq_im, reqs_im)

    ids_ol, arq_ol, reqs_ol, _sid, _erro = coletar_imopenlines(chamar_fn, chat, max_req=max_req)
    ids_ol = _aplicar_janela(ids_ol, desde, ate, ultimas)
    ol_resumo = _resumo_fonte(ids_ol, arq_ol, reqs_ol)
    saida["fontes"]["imopenlines"] = ol_resumo

    conjuntos = {"im_dialog": set(ids_im), "imopenlines": set(ids_ol)}

    if com_vibecode and api_get_fn is not None:
        ids_vc, arq_vc, reqs_vc = coletar_vibecode(api_get_fn, chat, max_req=max_req)
        ids_vc = _aplicar_janela(ids_vc, desde, ate, ultimas)
        saida["fontes"]["vibecode"] = _resumo_fonte(ids_vc, arq_vc, reqs_vc)
        conjuntos["vibecode"] = set(ids_vc)

    saida["diferencas"] = _diferencas(conjuntos)

    if com_bot:
        try:
            saida["bot"] = consultar_bot(chamar_fn, bot_id)
        except Exception as e:  # noqa: BLE001 — o bot é informativo; não derruba a medição
            saida["bot"] = {"erro": seguranca.mascarar(str(e))}

    return saida


# ---------------------------------------------------------------------------
# saída
# ---------------------------------------------------------------------------
def _imprimir_texto(resultados: list[dict]) -> None:
    for r in resultados:
        print(f"== {r['chat']} ==")
        for nome, f in r["fontes"].items():
            intervalo = (f"{f['min_id']}..{f['max_id']}"
                         if f["min_id"] is not None else "(sem ids)")
            print(f"  {nome}: {f['quantidade']} mensagens · ids {intervalo} · "
                  f"{f['com_arquivo']} com arquivo · {f['requisicoes']} req")
        for par, d in sorted(r["diferencas"].items()):
            print(f"  diff {par}: {d}")
        if r.get("bot"):
            b = r["bot"]
            if "erro" in b:
                print(f"  bot: erro ao consultar ({b['erro']})")
            else:
                print(f"  bot {b.get('id')}: type={b.get('type')} "
                      f"eventMode={b.get('eventMode')}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _api_get_vibecode():
    """Fábrica do GET autenticado na API do VibeCode (usada só com --vibecode)."""
    import urllib.parse
    import urllib.request
    import json as _json

    def api_get(dialog_id, params):
        chave = seguranca.ler_segredo("vibecode_key")
        url = (f"https://vibecode.bitrix24.com/v1/chats/"
               f"{urllib.parse.quote(str(dialog_id))}/messages")
        limpos = {k: v for k, v in params.items() if v is not None}
        if limpos:
            url += "?" + urllib.parse.urlencode(limpos)
        req = urllib.request.Request(url, headers={"X-Api-Key": chave})
        with urllib.request.urlopen(req, timeout=25) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))
    return api_get


def _parse_limite(valor):
    if valor is None:
        return None
    return _para_epoch(valor)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Mede as fontes de leitura dos grupos (só contagens, sem PII).")
    p.add_argument("--chat", action="append", default=None,
                   help="chat a medir (ex.: chat100001); pode repetir. Padrão: dialog_ids do config.json")
    p.add_argument("--desde", default=None, help="data mínima (epoch ou ISO 8601)")
    p.add_argument("--ate", default=None, help="data máxima (epoch ou ISO 8601)")
    p.add_argument("--ultimas", type=int, default=None, help="apenas as últimas N mensagens")
    p.add_argument("--max-req", type=int, default=60, help="orçamento de requisições (padrão 60)")
    p.add_argument("--bot-id", type=int, default=100003, help="id do bot (padrão 100003)")
    p.add_argument("--vibecode", action="store_true", help="mede também a API VibeCode")
    p.add_argument("--json", action="store_true", help="saída JSON em vez de texto")
    a = p.parse_args(argv)

    chats = a.chat
    if not chats:
        try:
            with open(os.path.join(RAIZ, "config.json"), encoding="utf-8") as fh:
                cfg = json.load(fh)
            chats = cfg.get("dialog_ids") or []
        except (OSError, ValueError):
            chats = []
    if not chats:
        print("erro: informe --chat ou configure dialog_ids no config.json", file=sys.stderr)
        return 2

    desde = _parse_limite(a.desde)
    ate = _parse_limite(a.ate)

    api_get_fn = _api_get_vibecode() if a.vibecode else None

    resultados = []
    for chat in chats:
        try:
            resultados.append(medir_chat(
                chat, desde=desde, ate=ate, ultimas=a.ultimas,
                max_req=a.max_req, bot_id=a.bot_id,
                com_vibecode=a.vibecode, api_get_fn=api_get_fn))
        except Exception as e:  # noqa: BLE001 — um chat não derruba os outros
            resultados.append({"chat": chat, "erro": seguranca.mascarar(str(e))})

    if a.json:
        json.dump(resultados, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        _imprimir_texto(resultados)
    return 0


if __name__ == "__main__":
    sys.exit(main())
