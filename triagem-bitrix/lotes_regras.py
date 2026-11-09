#!/usr/bin/env python3
"""
lotes_regras.py — REGRAS PURAS sobre o ESTADO dos lotes (sem rede, testável).

Motivo de existir (caso real 24/09/2026): `LOTE-SIG-20260922-1845-fcd74515` ficou ABERTO
por 50h no `estado.json`. O timeout de fechamento é avaliado só sobre as mensagens que o
ciclo acabou de ler; quando o cursor passa delas, nenhum ciclo reavalia o lote e ele nunca
sai de ABERTO. `lotes_vencidos()` faz essa varredura sobre o estado inteiro.

Fecha por timeout apenas lote que ainda está ABERTO (não mexe em pronto/fechado/CONFLITO).
"""
from __future__ import annotations

import datetime as dt

STATUS_FINAIS = ("FECHADO_PRONTO", "FECHADO_POR_JANELA", "FECHADO_POR_TIMEOUT",
                 "FECHADO_POR_INATIVIDADE", "FECHADO_POR_TROCA_DE_PESSOA", "CONFLITO")


def _quando(valor):
    if isinstance(valor, dt.datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=dt.timezone.utc)
    if not valor:
        return None
    try:
        t = dt.datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def lotes_vencidos(estado, agora: dt.datetime, timeout_min: int = 120) -> list[dict]:
    """Lotes marcados ABERTO cujo último movimento passou do timeout.

    Devolve do mais antigo para o mais novo, com o status novo e o motivo — quem chama
    decide persistir. Estado vazio/estranho devolve [].
    """
    lotes = ((estado or {}).get("lotes") or {}) if isinstance(estado, dict) else {}
    agora = _quando(agora) or dt.datetime.now(dt.timezone.utc)
    saida = []
    for lid, reg in (lotes or {}).items():
        if not isinstance(reg, dict):
            continue
        if str(reg.get("status") or "").upper() != "ABERTO":
            continue
        referencia = _quando(reg.get("fim")) or _quando(reg.get("inicio"))
        if referencia is None:
            continue
        minutos = int((agora - referencia).total_seconds() // 60)
        if minutos <= int(timeout_min):
            continue
        saida.append({
            "lote_id": reg.get("lote_id") or lid,
            "minutos": minutos,
            "status_novo": "FECHADO_POR_TIMEOUT",
            "motivo": f"fechado por timeout ({timeout_min} min sem o traço de fechamento) "
                      f"— varredura do estado ({minutos} min parado)",
            "quando": referencia.isoformat(),
        })
    return sorted(saida, key=lambda x: -x["minutos"])


def kit_completo(reg: dict) -> bool:
    """Mesma condição de `pronto` do agrupar para lote fechado: identificação OK
    (CPF único de cliente + arquivo), sem barreira/revisão pendente."""
    if not isinstance(reg, dict):
        return False
    try:
        qtd = int(reg.get("qtd_arquivos") or len(reg.get("file_ids") or []))
    except (TypeError, ValueError):
        qtd = 0
    return (str(reg.get("identificacao") or "") == "OK" and qtd > 0
            and not reg.get("requer_revisao"))


def fechamento_por_timeout(reg: dict, motivo: str, agora: dt.datetime) -> dict:
    """F1-10 — patch ÚNICO de fechamento por timeout (varredura do estado).

    Regra igual à do `agrupar` (triagem_grupo, pós-processamento: lote fechado com
    identificação OK é pronto; não-OK vai para revisão): kit completo -> PRONTO;
    qualquer outra coisa -> REVISÃO com motivo (estado_lote.marcar_revisao). Antes a
    varredura gravava pronto=False sem requer_revisao: o kit sumia de todos os avisos.
    """
    agora = _quando(agora) or dt.datetime.now(dt.timezone.utc)
    patch = {"status": "FECHADO_POR_TIMEOUT", "aberto": False,
             "fechado_por": motivo, "fechado": agora.isoformat(timespec="seconds")}
    if kit_completo(reg):
        patch.update({"pronto": True, "pronto_por": motivo})
        return patch
    import estado_lote                                   # import tardio: módulo puro
    ident = (reg or {}).get("identificacao") or "sem identificação"
    revisado = estado_lote.marcar_revisao(
        reg or {}, f"fechado por timeout sem o traço de fechamento e kit incompleto "
                   f"({ident}) — conferir")
    patch.update({"pronto": False, "pronto_por": None, "requer_revisao": True,
                  "motivos_revisao": revisado.get("motivos_revisao") or []})
    return patch
