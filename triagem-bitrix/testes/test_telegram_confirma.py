# -*- coding: utf-8 -*-
"""
TDD — "Recebido" só DEPOIS de gravar (TG-03).

TG-03: o bot respondia "Recebido — N arquivos" ANTES de `mesclar_no_estado`; se a
gravação falhasse (trava estourada, disco cheio), a cliente já tinha a confirmação
de um kit que não estava no estado.json — confirmação falsa — e o processo morria
sem `try` no loop.

Regras novas:
  · persistir o lote ANTES de responder;
  · falha ao gravar = SEM confirmação falsa (nenhum "Recebido" sai);
  · o loop não morre: registra o erro e segue (guardando o lote na memória para o
    `reforcar_lotes_salvos` tentar gravar de novo).
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

import telegram_grupo as tgg  # noqa: E402

CFG = {"sinal_traco": ["-", "–", "—", "=", "_"], "traco_telegram": "/start",
       "modo_traco": "delimitador", "timeout_lote_min": 120, "janela_min_fallback": 30,
       "telegram": {"habilitado": True, "chats_permitidos": ["111"]}}


def U(msg_id, texto="/start", chat=111):
    return {"update_id": msg_id, "message": {
        "message_id": msg_id, "date": 1790251200 + msg_id,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": 42, "first_name": "Ana"}, "text": texto}}


RESUMO = {"chats": {"111": {"mensagens": 1, "lotes": 1, "autores": ["Ana"]}},
          "lotes": [{"lote_id": "LOTE-CONF", "status": "FECHADO_PRONTO", "file_ids": [],
                     "arquivos": [], "dialog_id": "tg:111", "fonte_entrada": "telegram"}]}


def _preparar(monkeypatch, tmp_path, eventos, falhar_gravacao=False):
    """Munda o mundo externo e registra a ORDEM dos eventos 'mesclar'/'falar'."""
    monkeypatch.setattr(tgg.tg, "carregar_config", lambda: dict(CFG))
    monkeypatch.setattr(tgg, "ler_token", lambda: "TOK")
    monkeypatch.setattr(tgg, "chamar",
                        lambda token, metodo, **params: {"result": [U(1)]})
    monkeypatch.setattr(tgg, "ARQ_ESTADO", str(tmp_path / "telegram-estado.json"))
    monkeypatch.setattr(tgg, "processar", lambda *a, **k: RESUMO)

    mesclar_real = tgg.mesclar_no_estado

    def mesclar(resumo, simular=False, cfg=None):
        if falhar_gravacao:
            eventos.append("mesclar-falhou")
            raise RuntimeError("disco cheio (simulado)")
        eventos.append("mesclar")
        return mesclar_real(resumo, simular=simular, cfg=cfg)

    def falar(token, chat, texto, cfg=None):
        eventos.append("falar")
        return True

    monkeypatch.setattr(tgg, "mesclar_no_estado", mesclar)
    monkeypatch.setattr(tgg, "falar_no_chat", falar)


def test_grava_o_lote_antes_de_confirmar(monkeypatch, tmp_path):
    eventos = []
    _preparar(monkeypatch, tmp_path, eventos)

    assert tgg.main(["--uma-vez"]) == 0

    assert "mesclar" in eventos and "falar" in eventos
    assert eventos.index("mesclar") < eventos.index("falar"), \
        "o lote tem de ser gravado ANTES do 'Recebido' (TG-03)"


def test_falha_ao_gravar_nao_gera_confirmacao_falsa(monkeypatch, tmp_path, capsys):
    eventos = []
    _preparar(monkeypatch, tmp_path, eventos, falhar_gravacao=True)

    assert tgg.main(["--uma-vez"]) == 1, "falha ao gravar é falha da rodada"

    assert "falar" not in eventos, "sem lote gravado não sai 'Recebido' nenhum"
    assert "sem confirma" in capsys.readouterr().out.lower()


def test_falha_ao_gravar_nao_derruba_o_processo(monkeypatch, tmp_path):
    """O loop não morre com exceção de gravação (antes o processar/mesclar era sem try)."""
    eventos = []
    _preparar(monkeypatch, tmp_path, eventos, falhar_gravacao=True)

    tgg.main(["--uma-vez"])          # não pode levantar exceção

    assert eventos == ["mesclar-falhou"]


def test_confirmacao_sai_quando_a_gravacao_dar_certo(monkeypatch, tmp_path):
    """Caminho normal: gravou -> confirma (o 'Recebido' continua sendo enviado)."""
    eventos = []
    _preparar(monkeypatch, tmp_path, eventos)

    tgg.main(["--uma-vez"])

    assert eventos.count("falar") == 1
