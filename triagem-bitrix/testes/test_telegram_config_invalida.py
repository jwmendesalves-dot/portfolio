# -*- coding: utf-8 -*-
"""
Z3 — telegram_grupo trata `triagem_grupo.ConfigInvalida` (pendência da C3).

Antes: `cfg.update(tg.carregar_config())` sem tratamento → traceback (código 1) e,
sob systemd com --loop, reinício em laço rápido. Agora, como pergunta_traco /
checar_midias: mensagem mascarada e código 3; no --loop, pausa crescente com
releitura do config, sem sair do processo.
"""
import sys

from conftest import RAIZ

sys.path.insert(0, RAIZ)

import telegram_grupo as tgg  # noqa: E402

SEGREDO = "abcdef1234567890segredo"


def _proibido(*a, **k):
    raise AssertionError("não podia ser chamado com config inválido")


def _invalida(*a, **k):
    raise tgg.tg.ConfigInvalida(f"config.json ilegível (token={SEGREDO})")


def test_uma_vez_config_invalido_sai_3_mascarado_sem_api(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tgg.tg, "carregar_config", _invalida)
    monkeypatch.setattr(tgg, "ler_token", _proibido)
    monkeypatch.setattr(tgg, "chamar", _proibido)
    monkeypatch.setattr(tgg, "ARQ_ESTADO", str(tmp_path / "telegram-estado.json"))

    assert tgg.main(["--uma-vez"]) == 3
    out = capsys.readouterr().out
    assert "config inválido" in out
    assert "código 3" in out
    assert SEGREDO not in out


def test_loop_config_invalido_pausa_crescente_e_segue_quando_valido(monkeypatch, capsys, tmp_path):
    chamadas = {"n": 0}

    def _carregar():
        chamadas["n"] += 1
        if chamadas["n"] <= 3:
            _invalida()
        return {"telegram": {"habilitado": False}}      # válido e desligado → sai 0

    pausas = []
    monkeypatch.setattr(tgg.tg, "carregar_config", _carregar)
    monkeypatch.setattr(tgg.time, "sleep", pausas.append)
    monkeypatch.setattr(tgg, "ler_token", _proibido)
    monkeypatch.setattr(tgg, "chamar", _proibido)
    monkeypatch.setattr(tgg, "ARQ_ESTADO", str(tmp_path / "telegram-estado.json"))

    assert tgg.main(["--loop", "30"]) == 0
    assert pausas == [60, 120, 240]
    out = capsys.readouterr().out
    assert out.count("Nova tentativa") == 3
    assert SEGREDO not in out


def test_backoff_tem_teto():
    pausas = []
    n = {"i": 0}
    orig = tgg.tg.carregar_config

    def _carregar():
        n["i"] += 1
        if n["i"] <= 8:
            _invalida()
        return {}

    tgg.tg.carregar_config = _carregar
    try:
        cfg = tgg.carregar_config_valida(loop=True, dormir=pausas.append)
    finally:
        tgg.tg.carregar_config = orig
    assert isinstance(cfg, dict)
    assert max(pausas) == tgg.PAUSA_CONFIG_MAX_S
    assert pausas[-1] == 900
