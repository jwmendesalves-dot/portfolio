"""TDD: senha no cabeçalho curto do grupo (formato C: "= CAMPO").

Caso real 2026-10-27 — cliente (dados abaixo FICTÍCIOS, mesmo formato):
a ficha do grupo tinha "=SENHA: Senhaficticiaxy7." e a senha era DESCARTADA
pelo parse_ficha (o bloco C trata benefício/nis/indicação/nome, mas não senha),
então o contato nascia com "S/SENHA" no Bitrix.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lote_para_job as L  # noqa: E402


def test_senha_com_dois_pontos():
    c = L.parse_ficha("=SENHA: Senhaficticiaxy7.")
    assert c.get("senha") == "Senhaficticiaxy7."


def test_senha_sem_dois_pontos():
    c = L.parse_ficha("=SENHA Senhaficticiaxy7")
    assert c.get("senha") == "Senhaficticiaxy7"


def test_senha_acesso_govbr():
    c = L.parse_ficha("=ACESSO AO GOV.BR: abc123@")
    assert c.get("senha") == "abc123@"


def test_caso_real_ficha_completa_formato_c():
    ficha = (
        "= FULANO FICTÍCIO DA SILVA TESTE \n"
        "= CPF: 620.966.542-03\n"
        "= AUX DOENÇA FACULTATIVO\n"
        "=SENHA: Senhaficticiaxy7.\n"
        "= 90 dias de atestado médico \n"
        "=INDICAÇÃO :   XYZ \n"
        "DATA:15/03/1961"
    )
    c = L.parse_ficha(ficha)
    assert c.get("senha") == "Senhaficticiaxy7."


def test_fichinha_painel_leva_senha():
    """A senha extraída precisa ir para a fichinha do painel (layout)."""
    ficha = "=SENHA: Senhaficticiaxy7."
    campos = L.parse_ficha(ficha)
    fichinha = L.montar_fichinha_painel({}, campos)
    assert "Senhaficticiaxy7." in fichinha
