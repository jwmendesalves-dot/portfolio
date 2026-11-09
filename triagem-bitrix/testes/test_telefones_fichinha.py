#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telefone repetido não pode virar dois campos na fichinha.

O ZapSign repete o mesmo número em "Telefone celular" e "Telefone trabalho";
a fichinha saía com Telefone 1 e Telefone 2 idênticos. Decisão do dono (04/11/2026):
no máximo DOIS números, sem repetição.

Hermético (OP-T2): lote e contrato sintéticos — o contrato fictício repete o
mesmo celular em "trabalho" e traz um terceiro número em "outro".
"""
import re

import pytest

import lote_para_job as L  # noqa: E402
from fixtures import sinteticos as F  # noqa: E402

PDFS = {}


@pytest.fixture(autouse=True)
def _pdf_sintetico(monkeypatch, tmp_path):
    F.instalar_texto_pdf(monkeypatch, L)
    monkeypatch.setattr(L, "SEM_REDE", True)
    PDFS["com"] = F.contrato_sintetico(tmp_path, com_tabela=True)


def _lote() -> dict:
    return F.lote_molde(mensagens=[], arquivos=[F.arquivo_pdf(PDFS["com"], nome="CONTRATO.pdf")])


def _fones(ficha: str) -> list:
    saida = []
    for linha in ficha.splitlines():
        if "Telefone" in linha:
            saida.append(linha.split(":**")[-1].strip())
    return saida


def test_telefones_sem_repeticao_e_no_maximo_dois():
    env = L.montar_job(_lote())
    ficha = env["job"]["fichinha"]
    fones = _fones(ficha)
    assert fones, f"nenhum telefone na fichinha:\n{ficha[-400:]}"
    assert len(fones) <= 2, f"mais de dois telefones: {fones}"
    digitos = [re.sub(r"\D", "", f) for f in fones]
    assert len(set(digitos)) == len(digitos), f"telefone repetido: {fones}"
    # o contrato sintético tem 3 entradas (celular = trabalho, outro distinto):
    # sobram exatamente os dois números distintos, na ordem
    assert digitos == [re.sub(r"\D", "", F.TEL_CELULAR), re.sub(r"\D", "", F.TEL_OUTRO)], fones
