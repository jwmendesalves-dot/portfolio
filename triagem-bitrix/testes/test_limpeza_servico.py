#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trava a limpeza do serviço com os valores REAIS que apareceram na base."""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)
from lote_para_job import _limpar_servico  # noqa: E402

CASOS = [
    ("Auxílio Maternidade Facultativo, e para isso", "Auxílio Maternidade Facultativo"),
    ("ADM Auxilio Maternidade Facultativo", "ADM Auxilio Maternidade Facultativo"),
    ("Auxílio-Doença Facultativo — com", "Auxílio-Doença Facultativo"),
    ("Auxílio-Doença e Auxílio-Acidente: O(a)", "Auxílio-Doença e Auxílio-Acidente"),
    ("Auxílio-Doença e Auxílio-Acidente: O(a", "Auxílio-Doença e Auxílio-Acidente"),
    ("*Auxílio doença e auxílio acidente*", "Auxílio doença e auxílio acidente"),
    ("LOAS Benefício de Prestação Continuada, e", "LOAS Benefício de Prestação Continuada"),
    ("Auxílio Maternidade Facultativo, e para isso o(a) contratante pagará honorários de 40%",
     "Auxílio Maternidade Facultativo"),
    ("0", ""),
    ("{UfCrm1725033408826}", ""),
]


def test_limpeza():
    erros = []
    for entrada, esperado in CASOS:
        obtido = _limpar_servico(entrada)
        if obtido != esperado:
            erros.append(f"{entrada!r} -> {obtido!r} (esperado {esperado!r})")
    assert not erros, "\n".join(erros)


if __name__ == "__main__":
    try:
        test_limpeza()
        print("PASS test_limpeza_servico")
    except AssertionError as e:
        print("FAIL test_limpeza_servico:\n" + str(e))
        sys.exit(1)
