#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A fichinha tem que falar do serviço do CLIENTE, não de maternidade para todos."""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)
import lote_para_job as L  # noqa: E402

LOTE = {"nome": "MARIA TESTE DA SILVA", "cpf": "37009513295"}


def _ficha(servico: str) -> str:
    campos = {"nome": "MARIA TESTE DA SILVA", "cpf": "37009513295", "servico_contratado": servico,
              "meses": "12 semanas", "parto": "01/02/2027", "telefones": ["(92) 99327-2232"]}
    return L.montar_fichinha_painel(LOTE, campos)


def test_maternidade_mantem_titulo_e_gestacao():
    f = _ficha("Auxílio Maternidade Facultativo")
    assert "SALÁRIO-MATERNIDADE" in f.upper(), "título de maternidade sumiu"
    assert "DADOS DA GESTAÇÃO" in f, "bloco de gestação sumiu para gestante"
    assert "Cartão da Gestante" in f, "não pede cartão da gestante para gestante"


def test_auxilio_doenca_nao_fala_de_maternidade():
    f = _ficha("Auxílio-Doença Facultativo")
    assert "SALÁRIO-MATERNIDADE" not in f.upper(), f"ficha de auxílio-doença falando de maternidade:\n{f[:300]}"
    assert "AUXÍLIO-DOENÇA" in f.upper(), "título não virou auxílio-doença"
    assert "DADOS DA GESTAÇÃO" not in f, "pedia dados de gestação para quem não é gestante"
    assert "Cartão da Gestante" not in f, "pedia cartão da gestante para quem não é gestante"


def test_doenca_e_acidente():
    f = _ficha("Auxílio-Doença e Auxílio-Acidente")
    assert "DOENÇA E AUXÍLIO-ACIDENTE" in f.upper(), f[:200]
    assert "DADOS DA GESTAÇÃO" not in f


def test_acidente_e_loas():
    assert "AUXÍLIO-ACIDENTE" in _ficha("Auxílio Acidente").upper()
    assert "LOAS/BPC" in _ficha("LOAS Benefício de Prestação Continuada").upper()


def test_sem_servico_usa_titulo_neutro():
    f = _ficha("")
    assert "BENEFÍCIO PREVIDENCIÁRIO" in f.upper(), f[:200]
    assert "SALÁRIO-MATERNIDADE" not in f.upper()
    assert "DADOS DA GESTAÇÃO" not in f


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
