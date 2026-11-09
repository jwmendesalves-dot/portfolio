# -*- coding: utf-8 -*-
"""
TDD — MEMÓRIA de lotes do Telegram (TG-02).

TG-02: `lotes_memoria` guardava o registro COMPLETO de todo lote já visto, para
sempre, e `reforcar_lotes_salvos` RECREAVA qualquer lote que sumisse do estado —
inclusive lotes apagados/ignorados de propósito (a limpeza recomendada no CONTEXTO
era impossível: o lote voltava no segundo seguinte).

Regras novas:
  · memória com LIMITE (máximo de entradas + idade máxima) — não cresce sem teto;
  · só ressuscita lote que NUNCA chegou ao estado (`confirmado=False`), recente e
    não marcado como ignorado; lote que já foi gravado e depois apagado NÃO volta.
"""
from conftest import RAIZ
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, RAIZ)

import estado_store  # noqa: E402
import telegram_grupo as TG  # noqa: E402


def lote(lid, **extra):
    return {"lote_id": lid, "status": "FECHADO_PRONTO", "file_ids": [], "arquivos": [],
            "dialog_id": "tg:111", "fonte_entrada": "telegram", **extra}


def _estado():
    return estado_store.ler_estado()


def _limpar_estado():
    estado_store.atualizar_estado(lambda e: e.clear() or e)


# ── limite de tamanho ──────────────────────────────────────────────────────
def test_memoria_de_lotes_tem_limite():
    est = {}
    for i in range(TG.MEMORIA_MAX_LOTES + 50):
        TG.memoria_de_lotes(est, {"lotes": [lote(f"LOTE-{i}")]})

    assert len(est["lotes_memoria"]) <= TG.MEMORIA_MAX_LOTES, \
        "a memória não pode crescer sem limite"


def test_memoria_apaga_entradas_muito_antigas():
    est = {"lotes_memoria": {
        "VELHO": {**lote("VELHO"), "_memoria": {
            "gravado_em": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
                timespec="seconds"), "confirmado": False}}}}

    TG.memoria_de_lotes(est, {"lotes": [lote("NOVO")]})

    assert "VELHO" not in est["lotes_memoria"], "entrada além da idade máxima sai da memória"
    assert "NOVO" in est["lotes_memoria"]


# ── não ressuscitar o que foi apagado/ignorado de propósito ────────────────
def test_nao_ressuscita_lote_confirmado_que_foi_apagado():
    """Lote que JÁ chegou ao estado e depois sumiu = apagado de propósito."""
    _limpar_estado()
    est = {}
    TG.memoria_de_lotes(est, {"lotes": [lote("LOTE-APAGADO")]}, confirmado=True)

    n = TG.reforcar_lotes_salvos(est)

    assert n == 0
    assert "LOTE-APAGADO" not in (_estado().get("lotes") or {})


def test_nao_ressuscita_lote_marcado_ignorado():
    _limpar_estado()
    est = {}
    TG.memoria_de_lotes(est, {"lotes": [lote("LOTE-IGN", status="IGNORADO")]}, confirmado=False)

    assert TG.reforcar_lotes_salvos(est) == 0
    assert "LOTE-IGN" not in (_estado().get("lotes") or {})


def test_nao_ressuscita_lote_muito_antigo_nunca_confirmado():
    _limpar_estado()
    est = {}
    TG.memoria_de_lotes(est, {"lotes": [lote("LOTE-VELHO")]}, confirmado=False)
    est["lotes_memoria"]["LOTE-VELHO"]["_memoria"]["gravado_em"] = (
        datetime.now(timezone.utc) - timedelta(hours=TG.MEMORIA_IDADE_MAX_H + 1)
    ).isoformat(timespec="seconds")

    assert TG.reforcar_lotes_salvos(est) == 0
    assert "LOTE-VELHO" not in (_estado().get("lotes") or {})


def test_nao_ressuscita_entrada_legada_sem_carimbo():
    """Entrada do formato antigo (sem `_memoria`): idade desconhecida, não ressuscita."""
    _limpar_estado()
    est = {"lotes_memoria": {"LOTE-LEGADO": lote("LOTE-LEGADO")}}

    assert TG.reforcar_lotes_salvos(est) == 0
    assert "LOTE-LEGADO" not in (_estado().get("lotes") or {})


# ── anti-perda preservado (o lado bom do reforço) ──────────────────────────
def test_ressuscita_lote_recente_que_nunca_chegou_ao_estado():
    _limpar_estado()
    est = {}
    TG.memoria_de_lotes(est, {"lotes": [lote("LOTE-PERDIDO")]}, confirmado=False)

    assert TG.reforcar_lotes_salvos(est) == 1
    assert "LOTE-PERDIDO" in (_estado().get("lotes") or {}), \
        "lote recente nunca gravado ainda é recuperado (anti-perda)"


def test_marca_confirmado_depois_de_ressuscitar_para_nao_ressuscitar_de_novo():
    _limpar_estado()
    est = {}
    TG.memoria_de_lotes(est, {"lotes": [lote("LOTE-UMA")]}, confirmado=False)
    TG.reforcar_lotes_salvos(est)
    # operador apaga de propósito depois que o lote chegou ao estado
    estado_store.atualizar_estado(lambda e: (e.get("lotes") or {}).pop("LOTE-UMA", None))

    assert TG.reforcar_lotes_salvos(est) == 0, "só ressuscita UMA vez; depois é decisão do operador"
    assert "LOTE-UMA" not in (_estado().get("lotes") or {})


def test_reg_de_memoria_nao_vaza_marcadores_para_o_estado():
    """O marcador `_memoria` é interno: não pode sujar o registro do lote no estado.json."""
    _limpar_estado()
    est = {}
    TG.memoria_de_lotes(est, {"lotes": [lote("LOTE-LIMPO")]}, confirmado=False)
    TG.reforcar_lotes_salvos(est)

    reg = (_estado().get("lotes") or {})["LOTE-LIMPO"]
    assert "_memoria" not in reg
