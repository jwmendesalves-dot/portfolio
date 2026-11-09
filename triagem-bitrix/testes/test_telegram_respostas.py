# -*- coding: utf-8 -*-
"""
TDD — resposta do bot do TELEGRAM na conversa (24/09/2026).

Pedido do dono: *"recebido - (nome do cliente) proximos"* e *"garante que o do tele
vai fazer download"*. O /start é o traço (mesma lógica do grupo do Bitrix): o 1º
ABRE; cada /start seguinte FECHA o de cima e deixa o de baixo aberto.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

import telegram_grupo as tgg  # noqa: E402

# formato REAL do config.json: allowlist aninhada em `telegram.chats_permitidos`
CFG = {"sinal_traco": ["-", "–", "—", "=", "_"], "traco_telegram": "/start",
       "timeout_lote_min": 120,
       "telegram": {"habilitado": True, "chats_permitidos": ["555", "777"]}}


def U(msg_id, texto="", arquivos=0, chat=555, autor="Ana", ts=1790251200):  # 2026-09-24 12:00Z
    """Update cru do Telegram, no formato que a API devolve."""
    u = {"update_id": msg_id,
         "message": {"message_id": msg_id, "date": ts,
                     "chat": {"id": chat, "type": "private"},
                     "from": {"id": 42, "first_name": autor}}}
    if texto:
        u["message"]["text"] = texto
    if arquivos:
        u["message"]["photo"] = [{"file_id": f"f{msg_id}-{i}", "file_size": 100 + i}
                                 for i in range(arquivos)]
    return u


def test_primeiro_start_abre():
    est = {}
    respostas = tgg.decidir_respostas([U(1, "/start")], est, CFG)
    assert respostas == [("555", tgg.RL.texto_abre("/start"))]
    assert est["aberto"]["555"]["msg"] == 1


def test_start_depois_dos_documentos_reporta_recebido_com_contagem():
    est = {}
    tgg.decidir_respostas([U(1, "/start")], est, CFG)
    tgg.decidir_respostas([U(2, arquivos=1), U(3, arquivos=1), U(4, arquivos=1)], est, CFG)
    respostas = tgg.decidir_respostas([U(5, "/start")], est, CFG)
    chat, texto = respostas[0]
    assert chat == "555"
    assert "registrado" in texto.lower() and "3 arquivos" in texto
    assert est["aberto"]["555"]["msg"] == 5, "o mesmo /start abre o bloco de baixo"
    assert est["arquivos_desde_aberto"]["555"] == 0


def test_start_seguido_sem_documento_reporta_zero():
    est = {}
    tgg.decidir_respostas([U(1, "/start")], est, CFG)
    _, texto = tgg.decidir_respostas([U(2, "/start")], est, CFG)[0]
    assert "0 arquivos" in texto


def test_nome_do_cliente_no_texto_entra_no_relatorio():
    est = {}
    tgg.decidir_respostas([U(1, "/start")], est, CFG)
    tgg.decidir_respostas([U(2, "MARIA JOSE DA SILVA", arquivos=1)], est, CFG)
    _, texto = tgg.decidir_respostas([U(3, "/start")], est, CFG)[0]
    assert "MARIA JOSE DA SILVA" in texto


def test_um_arquivo_no_singular():
    est = {}
    tgg.decidir_respostas([U(1, "/start")], est, CFG)
    tgg.decidir_respostas([U(2, arquivos=1)], est, CFG)
    _, texto = tgg.decidir_respostas([U(3, "/start")], est, CFG)[0]
    assert "1 arquivo." in texto


def test_chats_independentes():
    est = {}
    tgg.decidir_respostas([U(1, "/start", chat=555)], est, CFG)
    r = tgg.decidir_respostas([U(2, "/start", chat=777)], est, CFG)
    assert r == [("777", tgg.RL.texto_abre("/start"))], "o /start de outra pessoa abre o dela"
    assert set(est["aberto"]) == {"555", "777"}


def test_bloco_vencido_fecha_no_grupo():
    est = {"aberto": {"555": {"msg": 1, "ts": "2026-09-24T10:00:00+00:00", "autor": "Ana"}}}
    _, texto = tgg.decidir_respostas([U(9, "/start", ts=1790258400)], est, CFG)[0]  # 14:00Z
    assert "Lote aberto" not in texto
    assert "registrado" in texto.lower()  # fechou o bloco anterior (vencido) — bug 15/10


def test_mensagem_sem_sinal_nao_responde():
    assert tgg.decidir_respostas([U(1, arquivos=2)], {}, CFG) == []


def test_startatbot_tambem_vale():
    est = {}
    r = tgg.decidir_respostas([U(1, "/start@documentosexemploBot")], est, CFG)
    assert r and "Lote aberto" in r[0][1]
