#!/usr/bin/env python3
"""
telegram_regras.py — REGRAS PURAS do adaptador TELEGRAM (sem rede, testável).

O grupo do Bitrix usa `——` como traço; no Telegram o sinal é **/start**
(pedido do dono, 24/09/2026). Aqui o `/start` é traduzido para o traço antes de entrar
na esteira, então agrupamento/ponte/painel/worker seguem sem nenhuma mudança.
"""
from __future__ import annotations

import datetime as dt

TRAÇO_PADRAO = "—"


def _msg(u) -> dict:
    if not isinstance(u, dict):
        return {}
    return u.get("message") or u.get("channel_post") or u.get("edited_message") or {}


def texto_do_update(u) -> str:
    """Texto da mensagem (ou a legenda, quando é foto/documento)."""
    m = _msg(u)
    return str(m.get("text") or m.get("caption") or "").strip()


def chat_id_de(u) -> str:
    m = _msg(u)
    return str(((m.get("chat") or {}).get("id")) or "")


def nome_autor_de(from_) -> str:
    f = from_ if isinstance(from_, dict) else {}
    if isinstance(f.get("from"), dict):          # aceita tanto o `from` quanto a mensagem
        f = f["from"]
    nome = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x).strip()
    return nome or (f.get("username") or "Telegram")


def autor_de(u) -> str:
    return nome_autor_de(_msg(u).get("from") or {})


def chats_permitidos(cfg: dict | None) -> list:
    """Lista EFETIVA de chats autorizados, no formato real do config.json.

    O config.json de produção guarda a lista ANINHADA em `telegram.chats_permitidos`
    (TG-01: o código antigo lia `chats_permitidos` no nível de cima, onde ela nunca
    está — preencher o config não tinha efeito). A chave legada no nível de cima
    ainda é aceita quando o bloco `telegram` não tem a lista.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    bloco = cfg.get("telegram")
    bloco = bloco if isinstance(bloco, dict) else {}
    fonte = bloco.get("chats_permitidos")
    if fonte is None:
        fonte = cfg.get("chats_permitidos")          # formato legado (nível de cima)
    return [str(x) for x in (fonte or []) if str(x)]


def chat_permitido(chat_id, cfg: dict | None) -> bool:
    """Fechado por padrão (S03): lista vazia/ausente = NENHUM chat autorizado.

    Antes o oposto era verdadeiro (`chats_permitidos=[]` aceitava qualquer pessoa)
    e o arquivo de estranho entrava direto em `estado.json`/`lotes/` (produção).
    """
    return str(chat_id) in chats_permitidos(cfg)


def telegram_habilitado(cfg: dict | None) -> bool:
    """`telegram.habilitado` ausente ou falso = canal DESLIGADO (nada de API)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    bloco = cfg.get("telegram")
    if not isinstance(bloco, dict):
        return False
    return bool(bloco.get("habilitado"))


def eh_sinal_telegram(texto: str, cfg: dict) -> bool:
    """`/start` ou `/start@bot` = o traço desta fonte."""
    sinal = str(cfg.get("traco_telegram") or "/start").strip().lower()
    limpo = str(texto or "").strip().lower()
    return limpo == sinal or limpo.startswith(sinal + "@")


def texto_para_classificar(texto: str, cfg: dict) -> str:
    """Troca o `/start` pelo traço — é assim que a esteira reconhece o sinal.

    Usa o traço "forte" (—) quando ele está na lista configurada, senão o primeiro que
    houver: o classificador só reconhece sinal se o caractere estiver em `sinal_traco`.
    """
    if eh_sinal_telegram(texto, cfg):
        lista = [str(c) for c in (cfg.get("sinal_traco") or [])]
        if TRAÇO_PADRAO in lista:
            return TRAÇO_PADRAO
        return lista[0] if lista else TRAÇO_PADRAO
    return str(texto or "")


def arquivos_do_update(u) -> list[dict]:
    """Anexos da mensagem. Foto vem em várias resoluções: fica a MAIOR (a última)."""
    m = _msg(u)
    saida = []
    fotos = m.get("photo") or []
    if fotos:
        maior = max(fotos, key=lambda f: f.get("file_size") or 0)
        saida.append({"file_id": maior.get("file_id"), "nome": None,
                      "tamanho": maior.get("file_size"), "tipo": "foto", "mime": None})
    doc = m.get("document") or {}
    if doc.get("file_id"):
        saida.append({"file_id": doc["file_id"], "nome": doc.get("file_name"),
                      "tamanho": doc.get("file_size"), "tipo": "documento",
                      "mime": doc.get("mime_type")})
    for chave, tipo in (("video", "video"), ("audio", "audio"), ("voice", "voz")):
        item = m.get(chave) or {}
        if item.get("file_id"):
            saida.append({"file_id": item["file_id"], "nome": item.get("file_name"),
                          "tamanho": item.get("file_size"), "tipo": tipo,
                          "mime": item.get("mime_type")})
    return saida


def data_iso_do_update(u) -> str:
    m = _msg(u)
    try:
        return dt.datetime.fromtimestamp(int(m.get("date") or 0),
                                         dt.timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def mensagem_api(u, cfg: dict) -> dict:
    """A mensagem do Telegram no formato que o classificador da esteira já entende
    (o mesmo do webhook do Bitrix: author_id + date + text + params.FILE_ID)."""
    m = _msg(u)
    texto = texto_para_classificar(texto_do_update(u), cfg)
    return {
        "id": m.get("message_id"),
        "author_id": (m.get("from") or {}).get("id"),
        "date": data_iso_do_update(u),
        "text": f"[B]{autor_de(u)}:[/B] {texto}".strip(),
        "params": {"FILE_ID": [a["file_id"] for a in arquivos_do_update(u) if a.get("file_id")]},
    }
