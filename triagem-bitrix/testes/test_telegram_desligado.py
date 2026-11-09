# -*- coding: utf-8 -*-
"""
TDD — canal Telegram DESLIGADO (exigência da remediação).

O escritório NÃO usa mais Telegram (o robô em uso é o bot nativo do Bitrix, ID 100003).
Com `telegram.habilitado = false` (ou a chave ausente) o processo TEM DE SAIR com
mensagem clara e código 0, SEM ler token e SEM chamar a API — antes ele seguia direto
para o getUpdates.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

import telegram_grupo as tgg  # noqa: E402


def _proibido(*a, **k):
    raise AssertionError("não podia ser chamado com o canal desabilitado")


def test_habilitado_false_sai_zero_sem_ler_token_nem_chamar_api(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tgg.tg, "carregar_config",
                        lambda: {"telegram": {"habilitado": False, "chats_permitidos": []}})
    monkeypatch.setattr(tgg, "ler_token", _proibido)
    monkeypatch.setattr(tgg, "chamar", _proibido)
    monkeypatch.setattr(tgg, "ARQ_ESTADO", str(tmp_path / "telegram-estado.json"))

    assert tgg.main(["--uma-vez"]) == 0
    assert "desabilitado" in capsys.readouterr().out.lower()


def test_habilitado_ausente_tambem_sai_zero(monkeypatch, capsys, tmp_path):
    # config.json sem o bloco `telegram` (ou sem a chave) = canal desligado
    monkeypatch.setattr(tgg.tg, "carregar_config", lambda: {})
    monkeypatch.setattr(tgg, "ler_token", _proibido)
    monkeypatch.setattr(tgg, "chamar", _proibido)
    monkeypatch.setattr(tgg, "ARQ_ESTADO", str(tmp_path / "telegram-estado.json"))

    assert tgg.main(["--uma-vez"]) == 0
    assert "desabilitado" in capsys.readouterr().out.lower()


def test_loop_desligado_tambem_sai_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(tgg.tg, "carregar_config",
                        lambda: {"telegram": {"habilitado": False}})
    monkeypatch.setattr(tgg, "ler_token", _proibido)
    monkeypatch.setattr(tgg, "chamar", _proibido)
    monkeypatch.setattr(tgg, "ARQ_ESTADO", str(tmp_path / "telegram-estado.json"))

    assert tgg.main(["--loop", "30"]) == 0


def test_habilitado_true_continua_processando(monkeypatch, tmp_path):
    """Caminho normal: com habilitado=true o processo segue e termina limpo."""
    monkeypatch.setattr(tgg.tg, "carregar_config",
                        lambda: {"telegram": {"habilitado": True, "chats_permitidos": ["111"]}})
    monkeypatch.setattr(tgg, "ler_token", lambda: "TOK")
    monkeypatch.setattr(tgg, "chamar", lambda token, metodo, **params: {"result": []})
    monkeypatch.setattr(tgg, "ARQ_ESTADO", str(tmp_path / "telegram-estado.json"))

    assert tgg.main(["--uma-vez"]) == 0
