# -*- coding: utf-8 -*-
"""Frente C2b (onda 3) — F1-06/F1-07: config inválida é erro visível; webhook
respeita historico_paginas na 1ª leitura. Sem rede."""
import json

import pytest

import triagem_grupo as tg
import test_fase1_cursor as _cursor

# fixture compartilhado `ambiente`: conftest.py
args = _cursor.args


@pytest.fixture
def arq_config(tmp_path, monkeypatch):
    caminho = tmp_path / "config.json"
    monkeypatch.setattr(tg, "ARQ_CONFIG", str(caminho))
    return caminho


def _gravar(caminho, conteudo):
    caminho.write_text(conteudo if isinstance(conteudo, str) else json.dumps(conteudo),
                       encoding="utf-8")


def test_config_valida_de_producao_carrega(arq_config):
    _gravar(arq_config, {"modo_traco": "delimitador", "fonte_leitura": "webhook",
                         "historico_paginas": 12})
    cfg = tg.carregar_config()
    assert cfg["modo_traco"] == "delimitador" and cfg["fonte_leitura"] == "webhook"
    assert tg.modo_traco(cfg) == "delimitador"
    assert "delimitador" in tg.MODOS_TRACO and "delimitador_autor" in tg.MODOS_TRACO


@pytest.mark.parametrize("conteudo", [
    '{"modo_traco": "delimitador", ',            # JSON truncado
    "[1, 2]",                                    # raiz não-objeto
    {"fonte_leitura": "webhok"},                 # fonte desconhecida (typo)
    {"modo_traco": "delimitadorr"},              # modo desconhecido (typo)
    {"historico_paginas": "doze"},
])
def test_config_invalida_levanta_em_vez_de_cair_nos_padroes(arq_config, conteudo, caplog):
    _gravar(arq_config, conteudo)
    with pytest.raises(tg.ConfigInvalida):
        tg.carregar_config()
    assert any("NÃO roda" in r.getMessage() for r in caplog.records)


def test_config_ilegivel_levanta(arq_config):
    arq_config.write_bytes(b"\xff\xfe\x00lixo")
    with pytest.raises(tg.ConfigInvalida):
        tg.carregar_config()


def test_modo_traco_desconhecido_nao_vira_toggle_legado():
    with pytest.raises(tg.ConfigInvalida):
        tg.modo_traco({"modo_traco": "xpto"})
    assert tg.modo_traco({"toggle_por_autor": False}) == "grupo"     # legado continua


def test_main_sai_com_codigo_diferente_de_zero_e_alerta(arq_config, monkeypatch, capsys):
    _gravar(arq_config, "{ isto não é json")
    monkeypatch.setattr(tg, "configurar_log", lambda *a, **k: None)
    rodou = []
    monkeypatch.setattr(tg, "ciclo", lambda *a, **k: rodou.append(1) or {})
    assert tg.main(["--dry-run"]) == 3
    assert not rodou, "ciclo não pode rodar com config inválida"
    assert "NÃO rodou" in capsys.readouterr().out


def test_webhook_primeira_leitura_respeita_historico_paginas(ambiente, monkeypatch):
    ambiente["conf"]["historico_paginas"] = 2
    vistos = []

    def fake(w, dialog, max_paginas=40, cfg=None, first_id=None, ate_id=None, info=None):
        vistos.append({"max_paginas": max_paginas, "first_id": first_id})
        return []

    monkeypatch.setattr(tg, "buscar_mensagens", fake)
    tg.ciclo(args())
    assert vistos and vistos[0] == {"max_paginas": 2, "first_id": None}


def test_disjuntor_que_nao_abre_fica_registrado(monkeypatch, caplog):
    """F1-13: `_registrar_bloqueio_download` engolia a falha (`except: pass`)."""
    class Quebrado:
        def abrir(self, *a, **k):
            raise OSError("disco cheio")

    monkeypatch.setattr(tg.rb, "circuito", lambda nome: Quebrado())
    tg._registrar_bloqueio_download("RATE_LIMIT")
    assert any("disjuntor bitrix-download" in r.getMessage() and r.levelname == "ERROR"
               for r in caplog.records)
