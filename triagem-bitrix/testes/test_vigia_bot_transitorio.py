# -*- coding: utf-8 -*-
"""Vigia do bot da triagem: 502/503 do VibeCode são TRANSITÓRIOS (não "inesperado").

Incidente 06/11/2026: duas execuções (19:41 e 19:53) alertaram o dono com
"resposta inesperada (HTTP 502, code=BITRIX_UNAVAILABLE)" — o upstream do Bitrix
piscou e voltou sozinho. O vigia já trata 429 assim (contador + dedupe); 5xx tem
de seguir a mesma disciplina, senão todo blip vira mensagem no Telegram.
"""
import os
from conftest import RAIZ
import importlib.util
import json
import sys

MOD = os.path.join(RAIZ, "vigia-bot-triagem.py")
spec = importlib.util.spec_from_file_location("vigia_bot", MOD)
vigia = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vigia)


def classificar(st, corpo):
    return vigia.classificar_resposta(st, corpo)


# ------------------------------------------------------------- classificação
def test_rede_morta_e_rede():
    assert classificar(None, {"_exc": "URLError: timed out"}) == "rede"


def test_200_e_ok():
    assert classificar(200, {"success": True, "data": {}}) == "ok"


def test_502_bitrix_unavailable_e_transitorio():
    assert classificar(502, {"success": False,
                             "error": {"code": "BITRIX_UNAVAILABLE"}}) == "transitorio"


def test_gateway_fora_e_transitorio():
    for st in (408, 502, 503, 504):
        assert classificar(st, {}) == "transitorio", st


def test_500_seco_continua_alertando_na_hora():
    """Bug de servidor (500 sem código conhecido) NÃO é transitório — alerta já."""
    assert classificar(500, {"error": {"code": "ALGO_QUEBRADO"}}) == "inesperado"
    assert classificar(500, {}) == "inesperado"


def test_500_com_bitrix_indisponivel_e_transitorio():
    assert classificar(500, {"error": {"code": "BITRIX_UNAVAILABLE"}}) == "transitorio"


def test_429_continua_transitorio():
    assert classificar(429, {"error": {"code": "RATE_LIMITED"}}) == "transitorio"


def test_409_bot_events_busy_e_ocupado():
    assert classificar(409, {"error": {"code": "BOT_EVENTS_BUSY"}}) == "ocupado"


def test_403_bot_access_denied_e_negado():
    assert classificar(403, {"error": {"code": "BOT_ACCESS_DENIED"}}) == "negado"


def test_erro_desconhecido_e_inesperado():
    assert classificar(418, {"error": {"code": "CHALEIRA"}}) == "inesperado"


# --------------------------------------------------------------- comportamento
def _rodar(monkeypatch, tmp_path, respostas):
    """Roda main() com uma sequência de respostas e captura stdout/retorno."""
    estado = tmp_path / ".vigia-estado.json"
    monkeypatch.setattr(vigia, "ARQ_ESTADO_VIGIA", str(estado))
    fila = list(respostas)
    monkeypatch.setattr(vigia, "_req", lambda *a, **k: fila.pop(0))
    saidas = []
    for _ in range(len(respostas)):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            codigo = vigia.main()
        saidas.append((codigo, buf.getvalue().strip()))
    return saidas, estado


def test_um_502_isolado_e_silencioso(monkeypatch, tmp_path):
    saidas, _ = _rodar(monkeypatch, tmp_path, [(502, {"error": {"code": "BITRIX_UNAVAILABLE"}})])
    assert saidas[0] == (0, ""), saidas


def test_502_em_sequencia_alerta_uma_vez_so(monkeypatch, tmp_path):
    erro = (502, {"error": {"code": "BITRIX_UNAVAILABLE"}})
    saidas, _ = _rodar(monkeypatch, tmp_path, [erro, erro, erro, erro, erro])
    codigos = [c for c, _ in saidas]
    assert codigos[:2] == [0, 0], f"silêncio nas 2 primeiras: {codigos}"
    assert codigos[2] == 1, f"no 3º seguido tem de alertar: {codigos}"
    # exit 1 nas seguintes (o cron marca falha) mas SEM repetir a mensagem
    assert codigos[3] == 1 and codigos[4] == 1, f"exit 1 sem repetir texto: {codigos}"
    textos = [t for _, t in saidas if t]
    assert len(textos) == 1, textos
    assert "502" in textos[0] and "transit" in textos[0].lower(), textos[0]


def test_200_depois_do_blip_zera_o_contador(monkeypatch, tmp_path):
    erro = (502, {"error": {"code": "BITRIX_UNAVAILABLE"}})
    saidas, estado = _rodar(monkeypatch, tmp_path, [erro, erro, (200, {"success": True}),
                                                   erro, erro, erro])
    assert [c for c, _ in saidas] == [0, 0, 0, 0, 0, 1], [c for c, _ in saidas]
    assert json.loads(open(estado).read())["falhas_consecutivas"] == 3


def test_negado_continua_sendo_tratado_como_negado(monkeypatch, tmp_path):
    saidas, _ = _rodar(monkeypatch, tmp_path,
                       [(403, {"error": {"code": "BOT_ACCESS_DENIED", "fix": {}}})])
    assert saidas[0][0] == 1
    assert "BOT_ACCESS_DENIED" in saidas[0][1] or "sem targetApiKeyId" in saidas[0][1]


if __name__ == "__main__":
    sys.exit(0)
