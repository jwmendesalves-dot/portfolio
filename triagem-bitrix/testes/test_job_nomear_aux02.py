# -*- coding: utf-8 -*-
"""AUX-02 / ID-09 — nomear_lotes_por_cpf: contato ÚNICO (todas as páginas),
"não achei" com TTL, falha de rede não vira "não achei", gravação via
estado_store.atualizar_lotes(dono="identificacao"). Tudo sintético, sem rede.
"""
import json
import os
import time

import pytest

import nomear_lotes_por_cpf as N
from fixtures import sinteticos as F

CPF = F.CPF_CLIENTE


@pytest.fixture(autouse=True)
def _cache_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "CACHE", str(tmp_path / "cache_cpf_nome.json"))


def _contato(i, nome, sobrenome):
    return {"ID": str(i), "NAME": nome, "SECOND_NAME": "", "LAST_NAME": sobrenome}


def _instalar_listar(monkeypatch, contatos=(), deals=(), registro=None):
    def falso(metodo, params):
        if registro is not None:
            registro.append((metodo, params))
        return list(contatos if metodo == "crm.contact.list" else deals)
    monkeypatch.setattr(N, "_listar", falso)


# ---------------------------------------------------------------- unicidade
def test_cpf_duplicado_com_nomes_diferentes_e_ambiguo(monkeypatch):
    """Antes: valia o 1º contato da 1ª página (nome possivelmente errado)."""
    _instalar_listar(monkeypatch, contatos=[_contato(10, "MARIA", "FICTICIA SOUZA"),
                                            _contato(11, "JOANA", "FICTICIA LIMA")],
                     deals=[{"ID": "5", "TITLE": "PREVI ADM - MARIA FICTICIA SOUZA"}])
    nome, origem = N.buscar_cpf(CPF)
    assert nome == ""
    assert origem.startswith("contatos ambiguo")


def test_todas_as_paginas_contam(monkeypatch):
    """O divergente está depois da 1ª página (50): ainda assim é ambíguo."""
    contatos = [_contato(i, "MARIA", "FICTICIA SOUZA") for i in range(1, 56)]
    contatos.append(_contato(99, "PEDRO", "FICTICIO ALVES"))
    reg = []
    _instalar_listar(monkeypatch, contatos=contatos, registro=reg)
    assert N.buscar_cpf(CPF)[0] == ""
    metodo, params = reg[0]
    assert metodo == "crm.contact.list"
    assert params["filter"] == {N.CAMPO_CPF: CPF}
    assert "NAME" in params["select"]


def test_mesmo_nome_com_e_sem_particula_e_unico(monkeypatch):
    _instalar_listar(monkeypatch, contatos=[_contato(10, "MARIA", "SOUZA FICTICIA"),
                                            _contato(11, "MARIA", "DE SOUZA FICTICIA")])
    nome, origem = N.buscar_cpf(CPF)
    assert nome.upper() == "MARIA DE SOUZA FICTICIA"     # a grafia por extenso
    assert origem.startswith("contato ")


def test_contato_sem_nome_valido_cai_no_deal(monkeypatch):
    _instalar_listar(monkeypatch, contatos=[_contato(10, "Vidabbzao", "")],
                     deals=[{"ID": "7", "TITLE": "PREVI ADM - MARIA FICTICIA SOUZA"}])
    assert N.buscar_cpf(CPF) == ("MARIA FICTICIA SOUZA", "deal 7")


def test_falha_do_bitrix_levanta_e_nao_vira_nao_achei(monkeypatch):
    import bitrix_cliente

    def cai(*a, **k):
        raise RuntimeError("disjuntor aberto")

    monkeypatch.setattr(bitrix_cliente, "listar", cai)
    with pytest.raises(N.ConsultaFalhou):
        N.buscar_cpf(CPF)


# ---------------------------------------------------------------- cache / TTL
def test_nao_achei_expira(monkeypatch):
    agora = time.time()
    cache = {CPF: {"nome": "", "origem": "", "em": agora - N.TTL_NEGATIVO_S - 1}}
    assert N.do_cache(cache, CPF, agora) is None
    cache[CPF]["em"] = agora - 60
    assert N.do_cache(cache, CPF, agora) == cache[CPF]


def test_cache_legado_vazio_e_descartado(tmp_path):
    outro = F.CPF_OUTRO
    with open(N.CACHE, "w", encoding="utf-8") as fh:
        json.dump({CPF: "", outro: "MARIA FICTICIA SOUZA"}, fh)
    cache = N.ler_cache()
    assert CPF not in cache                         # antes: "não achei" para sempre
    assert cache[outro]["nome"] == "MARIA FICTICIA SOUZA"


# ---------------------------------------------------------------- main / gravação
def _estado(tmp_path, nome=""):
    import estado_store
    lote = F.lote_molde(nome=nome, cpf=CPF, arquivos=[{"file_id": "1", "nome_salvo": "a.pdf"}])
    lote["nomes_candidatos"] = ["valor da triagem"]
    caminho = str(tmp_path / "estado.json")
    estado_store.gravar_atomico({"lotes": {lote["lote_id"]: lote}}, caminho)
    return caminho, lote["lote_id"]


def test_aplicar_grava_via_estado_store_dono_identificacao(monkeypatch, tmp_path):
    import estado_store
    caminho, lid = _estado(tmp_path)
    _instalar_listar(monkeypatch, contatos=[_contato(10, "MARIA", "FICTICIA SOUZA")])
    chamadas = []
    original = estado_store.atualizar_lotes

    def espiao(mudancas, dono, caminho=None, **kw):
        chamadas.append((mudancas, dono))
        return original(mudancas, dono, caminho=caminho, **kw)

    monkeypatch.setattr(estado_store, "atualizar_lotes", espiao)
    assert N.main(["--aplicar", "--estado", caminho]) == 0
    assert chamadas and chamadas[0][1] == "identificacao"
    lote = estado_store.ler_estado(caminho)["lotes"][lid]
    assert lote["nome"] == "MARIA FICTICIA SOUZA"
    assert lote["nome_origem"] == "bitrix-cpf"
    assert lote["nomes_candidatos"] == ["valor da triagem"]   # campo da triagem preservado
    assert not [n for n in os.listdir(tmp_path) if ".bak-" in n]   # sem copy2 ao lado do estado


def test_simular_nao_grava_estado(monkeypatch, tmp_path):
    caminho, _ = _estado(tmp_path)
    antes = open(caminho, "rb").read()
    _instalar_listar(monkeypatch, contatos=[_contato(10, "MARIA", "FICTICIA SOUZA")])
    assert N.main(["--simular", "--estado", caminho]) == 0
    assert open(caminho, "rb").read() == antes


def test_rodada_interrompida_nao_cacheia_falha(monkeypatch, tmp_path):
    caminho, _ = _estado(tmp_path)

    def falha(metodo, params):
        raise N.ConsultaFalhou("FalhaBitrix")

    monkeypatch.setattr(N, "_listar", falha)
    assert N.main(["--aplicar", "--estado", caminho]) == 1
    assert CPF not in N.ler_cache()
