# -*- coding: utf-8 -*-
"""Testes do medidor das fontes de leitura (TAREFA 2 — frente de apoio).

Cobrem: paginação do im.dialog (parada por "nenhum id novo"/página vazia, não
por len==LIMIT), extração do SESSION_ID da Linha Aberta, reporte do bot,
diferenças entre fontes, orçamento de requisições e ausência de PII.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

from conftest import RAIZ

_CAMINHO = os.path.join(RAIZ, "manutencao", "medir_fontes.py")
_spec = importlib.util.spec_from_file_location("medir_fontes", _CAMINHO)
medir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(medir)

NOME = "Maria da Silva Santos"
CPF = "111.222.333-44"
TELEFONE = "18 99999-0000"


# ---------------------------------------------------------------------------
# transportes falsos
# ---------------------------------------------------------------------------
def _fazer_chamar_im_dialog(paginas):
    """Paginação im.dialog.messages.get: páginas em sequência, depois vazio."""
    estado = {"n": 0}

    def chamar(metodo, params):
        assert metodo == "im.dialog.messages.get"
        n = estado["n"]
        estado["n"] += 1
        pagina = paginas[n] if n < len(paginas) else []
        return {"result": {"messages": [
            {"id": mid, "date": "2026-01-01T00:00:00+00:00"} for mid in pagina]}}
    return chamar


def _fazer_chamar_completo():
    """im.dialog (2 páginas) + imopenlines + bot, com sobreposição parcial."""
    estado = {"n": 0}

    def chamar(metodo, params):
        if metodo == "im.dialog.messages.get":
            n = estado["n"]
            estado["n"] += 1
            paginas = [
                [{"id": 300, "date": "2026-01-03T00:00:00+00:00",
                  "params": {"FILE_ID": [1]}},
                 {"id": 290, "date": "2026-01-02T00:00:00+00:00"}],
                [{"id": 280, "date": "2026-01-01T00:00:00+00:00"}],
            ]
            pagina = paginas[n] if n < len(paginas) else []
            return {"result": {"messages": pagina}}
        if metodo == "imopenlines.dialog.get":
            return {"result": {"entity_data_1": "Y|LEAD|1205|N|N|321|1773223732|0|0|0"}}
        if metodo == "imopenlines.session.history.get":
            return {"result": {"message": {
                "300": {"id": "300", "date": "2026-01-03T00:00:00+00:00",
                        "params": {"fileId": [1]}},
                "290": {"id": "290", "date": "2026-01-02T00:00:00+00:00"},
                "999": {"id": "999", "date": "2026-01-04T00:00:00+00:00"},
            }}}
        if metodo == "imbot.v2.Bot.get":
            return {"result": {"bot": {"id": 100003, "type": "supervisor",
                                       "eventMode": "fetch"}}}
        raise AssertionError(f"método inesperado: {metodo}")
    return chamar


# ---------------------------------------------------------------------------
# paginação e parada
# ---------------------------------------------------------------------------
def test_paginacao_coleta_todas_as_paginas_e_para_na_vazia():
    chamar = _fazer_chamar_im_dialog([[100, 99], [98, 97], [96]])
    ids, arq, reqs = medir.coletar_im_dialog(chamar, "chat1", max_req=60)
    assert set(ids) == {"100", "99", "98", "97", "96"}
    assert reqs == 4                       # 3 páginas + 1 vazia que confirma o fim


def test_parada_por_nenhum_id_novo():
    """Transporte que devolve SEMPRE os mesmos ids: para por 'nenhum id novo'."""
    def chamar(metodo, params):
        return {"result": {"messages": [{"id": 100, "date": "2026-01-01T00:00:00+00:00"}]}}

    ids, arq, reqs = medir.coletar_im_dialog(chamar, "chat1", max_req=60)
    assert set(ids) == {"100"}
    assert reqs == 2                       # 2ª chamada não traz id novo


def test_nao_para_por_len_igual_limite():
    """Uma página cheia (len == LIMIT) não encerra; segue até a página vazia."""
    paginas = [list(range(50)), list(range(50, 100))]
    chamar = _fazer_chamar_im_dialog(paginas)
    ids, arq, reqs = medir.coletar_im_dialog(chamar, "chat1", max_req=60)
    assert len(ids) == 100
    assert reqs == 3


def test_max_req_limita_o_orcamento():
    chamar = _fazer_chamar_im_dialog([[100, 99], [98], [97], [96]])
    ids, arq, reqs = medir.coletar_im_dialog(chamar, "chat1", max_req=2)
    assert reqs == 2
    assert set(ids) == {"100", "99", "98"}


def test_detecta_arquivo_im_dialog():
    def chamar(metodo, params):
        return {"result": {"messages": [
            {"id": 1, "date": "2026-01-01T00:00:00+00:00", "params": {"FILE_ID": [99]}},
            {"id": 2, "date": "2026-01-01T00:00:00+00:00"},
        ]}}

    ids, arq, reqs = medir.coletar_im_dialog(chamar, "chat1", max_req=60)
    assert arq == {"1"}


# ---------------------------------------------------------------------------
# imopenlines e bot
# ---------------------------------------------------------------------------
def test_imopenlines_extrai_session_id_e_coleta():
    chamar = _fazer_chamar_completo()
    ids, arq, reqs, sid, erro = medir.coletar_imopenlines(chamar, "chat1", max_req=60)
    assert sid == 321
    assert erro is None
    assert set(ids) == {"300", "290", "999"}
    assert arq == {"300"}
    assert reqs == 2


def test_imopenlines_sem_session_id_reporta_erro():
    def chamar(metodo, params):
        if metodo == "imopenlines.dialog.get":
            return {"result": {"entity_data_1": "Y|LEAD"}}   # sem 6º campo
        raise AssertionError(metodo)

    ids, arq, reqs, sid, erro = medir.coletar_imopenlines(chamar, "chat1", max_req=60)
    assert sid is None
    assert erro is not None
    assert ids == {}


def test_consultar_bot_reporta_type_e_eventmode():
    chamar = _fazer_chamar_completo()
    bot = medir.consultar_bot(chamar, 100003)
    assert bot["type"] == "supervisor"
    assert bot["eventMode"] == "fetch"


# ---------------------------------------------------------------------------
# diferenças e janela
# ---------------------------------------------------------------------------
def test_diferencas_entre_fontes():
    fontes = {"A": {"1", "2", "3"}, "B": {"3", "4"}}
    d = medir._diferencas(fontes)
    assert d["A × B"] == {"so_A": 2, "so_B": 1, "em_ambos": 1}


def test_janela_ultimas_pega_as_mais_recentes():
    ids = {"100": "2026-01-03", "99": "2026-01-02", "98": "2026-01-01"}
    filtrados = medir._aplicar_janela(ids, ultimas=2)
    assert filtrados == {"100": "2026-01-03", "99": "2026-01-02"}


def test_medir_chat_compara_fontes():
    r = medir.medir_chat("chat1", chamar_fn=_fazer_chamar_completo(),
                         com_bot=True, com_vibecode=False)
    assert r["fontes"]["im_dialog"]["quantidade"] == 3
    assert r["fontes"]["imopenlines"]["quantidade"] == 3
    # im_dialog tem {300,290,280}; imopenlines tem {300,290,999}
    d = r["diferencas"]["im_dialog × imopenlines"]
    assert d["so_im_dialog"] == 1
    assert d["so_imopenlines"] == 1
    assert d["em_ambos"] == 2
    assert r["bot"]["type"] == "supervisor"


# ---------------------------------------------------------------------------
# ausência de PII
# ---------------------------------------------------------------------------
def test_saida_nao_contem_pii():
    """Mensagens com nome/CPF/telefone no texto NUNCA vazam para a saída."""
    def chamar(metodo, params):
        if metodo == "im.dialog.messages.get":
            return {"result": {"messages": [
                {"id": 100, "date": "2026-01-01T00:00:00+00:00",
                 "text": f"Olá, {NOME}, CPF {CPF}, tel {TELEFONE}"},
            ]}}
        if metodo == "imopenlines.dialog.get":
            return {"result": {"entity_data_1": "Y|LEAD|1205|N|N|321|0|0|0|0"}}
        if metodo == "imopenlines.session.history.get":
            return {"result": {"message": {"100": {
                "id": "100", "date": "2026-01-01T00:00:00+00:00",
                "text": f"{NOME} {CPF} {TELEFONE}"}}}}
        if metodo == "imbot.v2.Bot.get":
            return {"result": {"bot": {"id": 100003, "type": "bot", "eventMode": "webhook"}}}
        raise AssertionError(metodo)

    r = medir.medir_chat("chat1", chamar_fn=chamar, com_bot=True)
    serializado = json.dumps(r, ensure_ascii=False)
    for proibido in (NOME, CPF, TELEFONE):
        assert proibido not in serializado, f"vazou PII: {proibido!r}"


def test_imprimir_texto_nao_contem_pii(capsys):
    """A saída em TEXTO também não vaza nome/CPF/telefone."""
    r = medir.medir_chat("chat1", chamar_fn=_fazer_chamar_completo(),
                         com_bot=True, com_vibecode=False)
    medir._imprimir_texto([r])
    saida = capsys.readouterr().out
    for proibido in (NOME, CPF, TELEFONE):
        assert proibido not in saida, f"vazou PII no texto: {proibido!r}"


def test_cli_help_nao_derruba():
    r = subprocess.run([sys.executable, _CAMINHO, "--help"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert "chat" in r.stdout
