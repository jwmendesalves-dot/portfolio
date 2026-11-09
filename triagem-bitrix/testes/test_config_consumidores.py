# -*- coding: utf-8 -*-
"""Frente C3 (onda 4) — F1-07: CONFIG_PADRAO alinhado ao uso real ('delimitador');
pergunta_traco e checar_midias tratam ConfigInvalida (mensagem clara, código != 0,
sem traceback, sem laço rápido no --loop); autoteste --testar-sinais sem falhas.
Sem rede."""
import datetime as _dt
import importlib.util
import json
import os
import sys
from unittest import mock

import pytest

from conftest import RAIZ
import triagem_grupo as tg


def _carregar_pergunta_traco():
    spec = importlib.util.spec_from_file_location(
        "pergunta_traco_c3_t", os.path.join(RAIZ, "pergunta_traco.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def arq_config(tmp_path, monkeypatch):
    caminho = tmp_path / "config.json"
    monkeypatch.setattr(tg, "ARQ_CONFIG", str(caminho))
    monkeypatch.setattr(tg, "BASE_DIR", str(tmp_path))
    return caminho


def _gravar(caminho, conteudo):
    caminho.write_text(conteudo if isinstance(conteudo, str) else json.dumps(conteudo),
                       encoding="utf-8")


# ─────────────────────────── F1-07: padrão = uso real ───────────────────────────
def test_padrao_do_modo_traco_e_o_do_uso_real():
    assert tg.CONFIG_PADRAO["modo_traco"] == "delimitador"
    assert tg.modo_traco(dict(tg.CONFIG_PADRAO)) == "delimitador"


def test_config_sem_a_chave_modo_traco_usa_delimitador(arq_config):
    # config real sem 'modo_traco': antes caía em 'grupo_autor' e a esteira
    # divergia do bot do traço (que só sabe 'delimitador')
    _gravar(arq_config, {"fonte_leitura": "webhook", "historico_paginas": 12})
    cfg = tg.carregar_config()
    assert tg.modo_traco(cfg) == "delimitador"
    assert cfg["fonte_leitura"] == "webhook"       # valor explícito não é tocado


def test_config_ausente_e_criado_com_delimitador(arq_config):
    cfg = tg.carregar_config()
    assert tg.modo_traco(cfg) == "delimitador"
    assert json.loads(arq_config.read_text(encoding="utf-8"))["modo_traco"] == "delimitador"


def test_modo_explicito_valido_nao_e_trocado_pelo_padrao(arq_config):
    _gravar(arq_config, {"modo_traco": "grupo_autor"})
    assert tg.modo_traco(tg.carregar_config()) == "grupo_autor"


def test_modo_invalido_continua_erro_visivel(arq_config):
    _gravar(arq_config, {"modo_traco": "delimitadorr"})
    with pytest.raises(tg.ConfigInvalida):
        tg.carregar_config()


# ───────────────────────────── pergunta_traco ─────────────────────────────────
@pytest.fixture
def pt(tmp_path):
    mod = _carregar_pergunta_traco()
    mod.ARQ_ESTADO = str(tmp_path / "pergunta-traco-estado.json")
    mod.ARQ_LOCK = str(tmp_path / "pergunta-traco.lock")
    mod.ARQ_BLOQUEIO = str(tmp_path / "bot-bloqueio.json")
    mod.ARQ_SAUDE = str(tmp_path / "saude-bot.json")
    mod.ARQ_DISPARO_LOG = str(tmp_path / "pergunta-disparo.log")
    mod.ARQ_DESFECHOS = str(tmp_path / "desfechos-pendentes.json")
    yield mod
    if mod._TRAVA_FD is not None:
        os.close(mod._TRAVA_FD)


class _Parar(BaseException):
    """Sai do `while True` do --loop no teste (não é Exception: não é engolida)."""


def _main_pt(mod, argv, rodada=None, dormir=None):
    with mock.patch.object(sys, "argv", ["pergunta_traco.py"] + argv), \
            mock.patch.object(mod.tg, "ler_chave_bot", return_value="k"), \
            mock.patch.object(mod, "rodada", side_effect=rodada) as rod, \
            mock.patch.object(mod, "bot_fala") as fala, \
            mock.patch.object(mod, "criar_disparador", return_value=None), \
            mock.patch.object(mod.time, "sleep", side_effect=dormir) as sleep, \
            mock.patch.object(mod.urllib.request, "urlopen") as rede:
        try:
            codigo = mod.main()
        except _Parar:
            codigo = "parou"
    return codigo, rod, fala, sleep, rede


def test_pergunta_traco_config_invalido_sai_3_sem_traceback(pt, arq_config, capsys):
    _gravar(arq_config, "{ isto não é json")
    codigo, rodada, fala, _, rede = _main_pt(pt, ["--ativo"])
    assert codigo == pt.SAIDA_CONFIG == 3
    rodada.assert_not_called()
    fala.assert_not_called()
    rede.assert_not_called()
    out = capsys.readouterr().out
    assert "NÃO rodou" in out and "config" in out and "Traceback" not in out


def test_pergunta_traco_modo_diferente_da_esteira_sai_3(pt, arq_config, capsys):
    # esteira em 'grupo_autor' e bot em 'delimitador' = avisos que contradizem
    # o agrupamento real: erro visível em vez de rodar divergente
    _gravar(arq_config, {"modo_traco": "grupo_autor"})
    codigo, rodada, _, _, _ = _main_pt(pt, ["--ativo"])
    assert codigo == 3
    rodada.assert_not_called()
    assert "grupo_autor" in capsys.readouterr().out


def test_pergunta_traco_config_real_roda(pt, arq_config):
    _gravar(arq_config, {"modo_traco": "delimitador", "fonte_leitura": "webhook",
                         "pergunta_traco": {"chats": ["chatX"], "bot_id": 1}})
    codigo, rodada, _, _, _ = _main_pt(pt, ["--ativo"])
    assert codigo == 0 and rodada.call_count == 1


def test_pergunta_traco_loop_com_config_invalido_pausa_crescente_sem_sair(pt, arq_config):
    _gravar(arq_config, "{ quebrado")
    pausas = []

    def dormir(s):
        pausas.append(s)
        if len(pausas) == 3:           # na 3ª pausa alguém corrige o config
            _gravar(arq_config, {"modo_traco": "delimitador",
                                 "pergunta_traco": {"chats": ["chatX"], "bot_id": 1}})
        if len(pausas) > 4:
            raise _Parar()

    def rodada(*a, **k):
        raise _Parar()                 # 1ª rodada depois do config válido: fim do teste

    codigo, rod, fala, _, rede = _main_pt(pt, ["--ativo", "--loop", "8"],
                                          rodada=rodada, dormir=dormir)
    assert codigo == "parou"
    assert pausas == [60, 120, 240], pausas      # pausa crescente, não 8 s
    assert rod.call_count == 1                   # voltou a rodar com o config corrigido
    fala.assert_not_called()
    rede.assert_not_called()


def test_backoff_do_config_tem_teto(pt):
    pausas = []
    n = {"i": 0}

    def carregar(_exigir):
        n["i"] += 1
        if n["i"] > 8:
            return {"ok": True}
        raise tg.ConfigInvalida("config.json ilegível")

    with mock.patch.object(pt, "carregar_config_do_bot", side_effect=carregar):
        assert pt.esperar_config_valida(True, dormir=pausas.append) == {"ok": True}
    assert pausas[:5] == [60, 120, 240, 480, 900] and max(pausas) == pt.PAUSA_CONFIG_MAX_S


def test_registrar_desfecho_nao_exige_modo_do_traco(pt, arq_config):
    _gravar(arq_config, {"modo_traco": "grupo_autor"})
    codigo, _, _, _, _ = _main_pt(pt, ["--registrar-desfecho", "enviado", "--lote-id", "L1"])
    assert codigo == 0


# ───────────────────────────── checar_midias ──────────────────────────────────
def test_checar_midias_config_invalido_sai_3_sem_traceback(arq_config, capsys, monkeypatch):
    import checar_midias
    import estado_store
    _gravar(arq_config, "[1, 2]")
    monkeypatch.setattr(estado_store, "ler_estado", lambda *a, **k: {"lotes": {}})
    monkeypatch.setattr(checar_midias, "_ler_json", lambda caminho: None)
    monkeypatch.setattr(tg, "ler_chave_bot", lambda: pytest.fail("não pode ler a chave"))
    assert checar_midias.main(["3"]) == checar_midias.SAIDA_CONFIG == 3
    out = capsys.readouterr().out
    assert "config.json inválido" in out and "Traceback" not in out


# ───────────────────────────── --testar-sinais ────────────────────────────────
def test_autoteste_testar_sinais_sem_falhas(capsys):
    codigo = tg.testar_sinais()
    out = capsys.readouterr().out
    assert codigo == 0, [l for l in out.splitlines() if l.startswith("[FALHA]")]
    assert "falhas=0" in out


def _agrupar_dois_kits(nome2):
    cfg = dict(tg.CONFIG_PADRAO)
    n = [0]

    def A(ts, texto="", files=None):
        n[0] += 1
        return tg.classificar_mensagem(
            {"id": 1000 + n[0], "author_id": 1, "date": ts,
             "text": f"[B]Ana:[/B] {texto}".strip(), "params": {"FILE_ID": files or []}},
            {}, cfg)

    msgs = [A("2026-09-20T10:00:00+00:00", "-"),
            A("2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
            A("2026-09-20T10:00:10+00:00", f"= {nome2}\n= CPF 513.350.652-02", files=[2]),
            A("2026-09-20T10:00:20+00:00", "-")]
    return tg.agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))


def test_barreira_com_cabecalho_completo_do_segundo_cliente():
    ls = _agrupar_dois_kits("JOAO PEDRO")
    joao = [l for l in ls if l.get("cpf") == "51335065202"]
    assert len(ls) == 2 and len(joao) == 1
    assert joao[0]["fechado_pelo_traco"] and joao[0]["status"] == "FECHADO_PRONTO"
    assert all(l["identificacao"] != "CONFLITO" for l in ls)


@pytest.mark.xfail(strict=True, reason=(
    "lacuna de regra (dono: agrupamento/nome_regras): cabeçalho '= JOAO' (nome < 5 "
    "letras, recusado por parece_ruido) + '= CPF' com DV válido não conta como "
    "marcador do traco_no_fim_fecha; o traço final abre lote vazio e o kit do 2º "
    "fecha por janela, não pelo traço. Sem mistura de documentos (a barreira corta)."))
def test_cabecalho_com_cpf_valido_e_nome_curto_fecha_pelo_traco():
    ls = _agrupar_dois_kits("JOAO")
    joao = [l for l in ls if l.get("cpf") == "51335065202"]
    assert len(ls) == 2 and joao and joao[0]["fechado_pelo_traco"]
