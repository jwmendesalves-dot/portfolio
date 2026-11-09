# -*- coding: utf-8 -*-
"""estado_lote.py — modelo de estados do lote (funções puras). Fixtures sintéticas."""
import sys

import pytest

from conftest import RAIZ

sys.path.insert(0, RAIZ)

import estado_lote  # noqa: E402
from estado_lote import (ESTADOS_LOGICOS, estado_logico, marcar_revisao,  # noqa: E402
                         motivos_impeditivos, pode_enfileirar, resolver_revisao,
                         transicao_valida)


def _cpf_teste(semente: int) -> str:
    base = f"{(semente * 7919 + 123456789) % 10 ** 9:09d}"
    if base == base[0] * 9:
        base = "123456789"
    d = [int(c) for c in base]
    for n in (9, 10):
        soma = sum(d[i] * (n + 1 - i) for i in range(n))
        d.append((soma * 10) % 11 % 10)
    return "".join(map(str, d))


CPF = _cpf_teste(1)


def _arquivo(i=1, **extra):
    a = {"file_id": str(10 + i), "indice": i, "baixado": True, "sha256": "ab" * 32,
         "tamanho_bits": 1000, "caminho": f"/base/lotes/L1/{i:02d}_doc.pdf", "erro": None}
    a.update(extra)
    return a


def _lote(**extra):
    lote = {"lote_id": "L1", "status": "FECHADO_PRONTO", "fechado": True, "aberto": False,
            "identificacao": "OK", "cpf": CPF, "requer_revisao": False, "motivos_revisao": [],
            "pronto": True, "file_ids": ["11", "12"], "arquivos": [_arquivo(1), _arquivo(2)]}
    lote.update(extra)
    return lote


def test_cpf_de_teste_e_valido_via_cpf_util():
    assert estado_lote._cpf_valido(CPF)
    assert not estado_lote._cpf_valido(CPF[:-1] + str((int(CPF[-1]) + 1) % 10))
    assert not estado_lote._cpf_valido("111.111.111-11")
    assert not estado_lote._cpf_valido("123")


def test_lote_completo_e_elegivel():
    ok, motivos = pode_enfileirar(_lote())
    assert ok and motivos == []
    assert estado_logico(_lote()) == "PRONTO"


@pytest.mark.parametrize("alteracao, trecho", [
    ({"identificacao": "SEM_CPF"}, "identificação"),
    ({"cpf": None}, "sem CPF"),
    ({"cpf": "123.456.789-00"}, "CPF inválido"),
    ({"requer_revisao": True, "motivos_revisao": ["x"]}, "requer revisão"),
    ({"status": "ABERTO", "aberto": True, "fechado": False}, "não está fechado"),
    ({"status": "CONFLITO", "identificacao": "CONFLITO"}, "conflito"),
    ({"status": "descartado_duplicata", "status_anterior": "FECHADO_PRONTO"}, "duplicata"),
    ({"ignorado": True}, "ignorado"),
    ({"dividido": True}, "dividido"),
    ({"erro_permanente": True}, "erro permanente"),
    ({"arquivos": []}, "sem arquivos"),
    ({"arquivos": [_arquivo(1), _arquivo(2, baixado=False)]}, "não baixado"),
    ({"arquivos": [_arquivo(1), _arquivo(2, sha256=None)]}, "sem sha256"),
    ({"arquivos": [_arquivo(1), _arquivo(2, tamanho_bits=0)]}, "tamanho 0"),
    ({"arquivos": [_arquivo(1), _arquivo(2, caminho="/x/02_doc.pdf.part")]}, "parcial"),
    ({"arquivos": [_arquivo(1), _arquivo(2, caminho="/x/02_doc.parcial")]}, "parcial"),
    ({"arquivos": [_arquivo(1)]}, "sem registro de arquivo"),
])
def test_cada_impedimento_bloqueia(alteracao, trecho):
    ok, motivos = pode_enfileirar(_lote(**alteracao))
    assert not ok
    assert any(trecho.lower() in m.lower() for m in motivos), motivos


def test_motivos_sao_todos_listados():
    motivos = motivos_impeditivos(_lote(cpf=None, identificacao="SEM_CPF", arquivos=[]))
    assert len(motivos) >= 3
    assert motivos_impeditivos(None) and motivos_impeditivos({})


def test_usa_cpf_util_quando_existe(monkeypatch):
    import types
    falso = types.ModuleType("cpf_util")
    falso.valido = lambda v: False                   # prova que delega ao cpf_util
    monkeypatch.setattr(estado_lote, "cpf_util", falso)
    ok, motivos = pode_enfileirar(_lote())
    assert not ok and any("CPF inválido" in m for m in motivos)


@pytest.mark.parametrize("lote, controle, esperado", [
    (_lote(status="ABERTO", aberto=True, fechado=False), None, "ABERTO"),
    (_lote(arquivos=[_arquivo(1), _arquivo(2, baixado=False)]), None, "AGUARDANDO_DOWNLOAD"),
    (_lote(identificacao="SEM_CPF", cpf=None), None, "AGUARDANDO_IDENTIFICACAO"),
    (_lote(requer_revisao=True), None, "REVISAO"),
    (_lote(status="revisao_duplicata", status_anterior="FECHADO_PRONTO"), None, "REVISAO"),
    (_lote(status="CONFLITO"), None, "REVISAO"),
    (_lote(identificacao="SEM_ARQUIVOS", arquivos=[]), None, "REVISAO"),
    (_lote(), None, "PRONTO"),
    (_lote(), {"job_id": "j1", "upload_id": "u1", "em": "x", "enviados": 2}, "ENFILEIRADO"),
    (_lote(), {"em": "x"}, "ENFILEIRANDO"),
    (_lote(status="descartado_duplicata"), None, "DESCARTADO_DUPLICATA"),
    (_lote(dividido=True), None, "DIVIDIDO"),
    (_lote(ignorado=True), None, "IGNORADO"),
    (_lote(erro_permanente=True), None, "ERRO_PERMANENTE"),
    ({}, None, "ERRO_PERMANENTE"),
])
def test_estado_logico(lote, controle, esperado):
    assert estado_logico(lote, controle) == esperado
    assert esperado in ESTADOS_LOGICOS


def test_status_externos_nao_sao_renomeados():
    lote = _lote(status="FECHADO_POR_TIMEOUT")
    estado_logico(lote)
    pode_enfileirar(lote)
    assert lote["status"] == "FECHADO_POR_TIMEOUT"


def test_transicoes():
    assert transicao_valida("ABERTO", "FECHADO")
    assert transicao_valida("PRONTO", "ENFILEIRANDO")
    assert transicao_valida("ENFILEIRANDO", "ENFILEIRADO")
    assert transicao_valida("ENFILEIRANDO", "PRONTO")          # falha no envio volta
    assert transicao_valida("REVISAO", "REVISAO")
    assert not transicao_valida("ABERTO", "ENFILEIRADO")
    assert not transicao_valida("AGUARDANDO_DOWNLOAD", "ENFILEIRANDO")
    assert not transicao_valida("ENFILEIRADO", "PRONTO")
    assert not transicao_valida("DIVIDIDO", "PRONTO")
    assert not transicao_valida("X", "PRONTO")
    for e in ESTADOS_LOGICOS:
        assert transicao_valida(e, e)


def test_marcar_revisao_acumula_e_nao_altera_original():
    original = _lote(motivos_revisao=["a"])
    m = marcar_revisao(original, "b")
    m = marcar_revisao(m, "b")
    assert m["motivos_revisao"] == ["a", "b"]
    assert m["requer_revisao"] is True and m["pronto"] is False
    assert original["motivos_revisao"] == ["a"] and original["pronto"] is True


def test_resolver_revisao_remove_so_o_motivo():
    m = marcar_revisao(marcar_revisao(_lote(), "SEM_CPF: x"), "duplicata")
    r = resolver_revisao(m, "SEM_CPF*", por="identificacao")
    assert r["motivos_revisao"] == ["duplicata"] and r["requer_revisao"] is True
    r = resolver_revisao(r, "duplicata")
    assert r["motivos_revisao"] == [] and r["requer_revisao"] is False
    assert [x["removidos"] for x in r["motivos_resolvidos"]] == [["SEM_CPF: x"], ["duplicata"]]
