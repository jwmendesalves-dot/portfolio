# -*- coding: utf-8 -*-
"""
TDD — kit partido entre BATIDAS do long poll vira UM lote só (24/09/2026).

Bug real: o dono mandou 4 fotos no privado do bot entre dois /start. O bot respondeu
"*Recebido · 4 arquivos*" (contagem certa), mas a esteira montou **DOIS lotes de 2** —
o `processar()` agrupava só a batida atual do long poll (a API entrega 2 fotos por
vez), então um lote passou na ponte (tinha o CPF no texto) e o outro ficou fora.

Fix: buffer por chat (`est["buffer"]`) — o bloco é agrupado desde o traço que o abriu
até o traço que o fecha, independentemente de quantas batidas aconteceram.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

import telegram_grupo as TG  # noqa: E402

# formato REAL do config.json: allowlist aninhada em `telegram.chats_permitidos`
CFG = {"sinal_traco": ["-", "–", "—", "=", "_"], "traco_telegram": "/start",
       "modo_traco": "delimitador", "timeout_lote_min": 120,
       "janela_min_fallback": 30, "traco_no_fim_fecha": True,
       "telegram": {"habilitado": True, "chats_permitidos": ["1000000001", "111", "222"]}}

CHAT = 1000000001
BASE_TS = 1790251200          # 2026-09-24 12:00Z


def U(msg_id, texto="", fotos=0, chat=CHAT):
    m = {"message_id": msg_id, "date": BASE_TS + msg_id,
         "chat": {"id": chat, "type": "private"},
         "from": {"id": 1000000001, "first_name": "Operador"}}
    if texto:
        m["text"] = texto
    if fotos:
        m["photo"] = [{"file_id": f"fid-{msg_id}-{i}", "file_size": 1000 + i}
                      for i in range(fotos)]
    return {"update_id": msg_id, "message": m}


def test_batidas_ate_o_traco_de_fechamento_viram_um_lote():
    est = {}
    # batida 1: abre | batida 2: 2 fotos | batida 3: 2 fotos (com o CPF) | batida 4: fecha
    for batida in ([U(1, "/start")],
                   [U(2, fotos=1), U(3, fotos=1)],
                   [U(4, fotos=1), U(5, "= CPF 246.337.393-87", fotos=1)]):
        TG.acumular_buffer(est, batida, CFG)

    bloco = TG.acumular_buffer(est, [U(6, "/start")], CFG)
    assert [u["update_id"] for u in bloco] == [1, 2, 3, 4, 5, 6], "o bloco tem de vir inteiro"

    resumo = TG.processar(bloco, CFG, simular=True)
    fechados = [l for l in resumo["lotes"] if l.get("status") == "FECHADO_PRONTO"]
    assert len(fechados) == 1, f"esperava 1 lote fechado, veio {[l['lote_id'] for l in resumo['lotes']]}"
    assert len(fechados[0]["file_ids"]) == 4, "os 4 arquivos têm de estar no MESMO lote"
    assert fechados[0]["pronto"] is True, "com CPF e fechado pelo traço, o lote vai para a ponte"


def test_o_traco_que_fechou_abre_o_proximo_bloco():
    est = {}
    TG.acumular_buffer(est, [U(1, "/start")], CFG)
    TG.acumular_buffer(est, [U(2, fotos=1)], CFG)
    TG.acumular_buffer(est, [U(3, "/start")], CFG)
    assert [u["update_id"] for u in est["buffer"][str(CHAT)]] == [3]


def test_bloco_que_ainda_esta_aberto_nao_perde_arquivo():
    est = {}
    TG.acumular_buffer(est, [U(1, "/start")], CFG)
    bloco = TG.acumular_buffer(est, [U(2, fotos=1)], CFG)
    assert [u["update_id"] for u in bloco] == [1, 2]
    assert [u["update_id"] for u in est["buffer"][str(CHAT)]] == [1, 2]


def test_sem_sinal_nenhum_o_material_fica_no_buffer():
    est = {}
    TG.acumular_buffer(est, [U(1, fotos=1)], CFG)
    TG.acumular_buffer(est, [U(2, fotos=1)], CFG)
    bloco = TG.acumular_buffer(est, [], CFG)
    assert bloco == [], "nada novo -> nada a processar"


def test_chats_separados_no_buffer():
    est = {}
    TG.acumular_buffer(est, [U(1, "/start", chat=111)], CFG)
    bloco = TG.acumular_buffer(est, [U(2, "/start", chat=222)], CFG)
    assert [u["update_id"] for u in bloco] == [2], "só o chat com novidade"
    assert set(est["buffer"]) == {"111", "222"}


def test_resposta_conta_os_arquivos_das_batidas(monkeypatch):
    """O relatório tem de contar os 4 (era o sintoma: relatório 4 x fila 2)."""
    est = {}
    TG.decidir_respostas([U(1, "/start")], est, CFG)
    TG.decidir_respostas([U(2, fotos=1), U(3, fotos=1)], est, CFG)
    TG.decidir_respostas([U(4, fotos=1), U(5, fotos=1)], est, CFG)
    respostas = TG.decidir_respostas([U(6, "/start")], est, CFG)
    assert "4 arquivos" in respostas[0][1]
