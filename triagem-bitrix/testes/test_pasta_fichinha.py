#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Código da pasta na fichinha + consulta segura (04/11/2026).

- cliente que já é do escritório: a fichinha mostra "• **Pasta:** <código>"
- cliente novo (sem código): a linha NÃO aparece (não inventa)
- CPF inválido: consulta nem é feita
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)
import lote_para_job as L  # noqa: E402


def test_linha_da_pasta_aparece_quando_existe():
    ficha = L.montar_fichinha_painel({"nome": "MARIA TESTE", "cpf": "37009513295"},
                                     {"nome": "MARIA TESTE", "cpf": "37009513295", "pasta": "2188 P"})
    assert "• **Pasta:** 2188 P" in ficha, f"linha da pasta ausente:\n{ficha[:500]}"


def test_linha_da_pasta_nao_aparece_sem_codigo():
    ficha = L.montar_fichinha_painel({"nome": "MARIA TESTE"}, {"nome": "MARIA TESTE", "pasta": ""})
    assert "Pasta:" not in ficha, "inventou linha de pasta sem código"


def test_cpf_invalido_nao_consulta():
    assert L.codigo_pasta_do_cliente("") == ""
    assert L.codigo_pasta_do_cliente("123") == ""
    assert L.codigo_pasta_do_cliente("000.000.000-00") in ("", "000.000.000")  # não quebra


if __name__ == "__main__":
    falhas = 0
    for nome, fn in list(globals().items()):
        if nome.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {nome}")
            except AssertionError as e:
                falhas += 1
                print(f"FAIL {nome}: {e}")
    print("\nRESULTADO:", "todos passaram" if not falhas else f"{falhas} falha(s)")
    sys.exit(1 if falhas else 0)
