"""TDD: ficha encaminhada do WhatsApp com cabeçalho "[data, hora] Nome:".

Caso real 2026-10-27 (nome e lote omitidos):
o operador encaminhou a ficha e o WhatsApp grudou o cabeçalho na linha do nome:
    "[26/10, 10:57] Fulana: Nome: Fulana Teste costa"
O parse_ficha não achava "Nome:" (estava depois do cabeçalho) e o lote ficava
SEM nome + SEM CPF -> preso em revisão, sem ir pra ponte.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lote_para_job as L  # noqa: E402
import cpf_util  # noqa: E402
CPF_A_FMT = cpf_util.formatar(cpf_util.gerar_para_teste(1301))  # CPF sintético (DV válido)



def test_cabecalho_encaminhado_nome():
    ficha = ("[26/10, 10:57] Fulana: Nome: Fulana Teste costa\n"
             "Idade:20\nRua: Raimundo Cavalcante\nCEP:69240-000\nCidade: Cidade Teste")
    c = L.parse_ficha(ficha)
    assert c.get("nome") == "Fulana Teste costa"


def test_cabecalho_encaminhado_com_cpf():
    ficha = ("[26/10, 10:57] Fulana: Nome: Fulana Teste costa\n"
             f"CPF: {CPF_A_FMT}\nCidade: Cidade Teste")
    c = L.parse_ficha(ficha)
    assert c.get("nome") == "Fulana Teste costa"
    assert c.get("cpf") == f"{CPF_A_FMT}"


def test_cabecalho_com_segundos_e_ano():
    ficha = ("[26/10/2026, 10:57:30] Fulano de Tal: Nome: Maria da Silva\nCPF: 123.456.789-00")
    c = L.parse_ficha(ficha)
    assert c.get("nome") == "Maria da Silva"
    assert c.get("cpf") == "123.456.789-00"


def test_sem_cabecalho_nao_quebra():
    """Ficha normal (sem cabeçalho) continua funcionando."""
    c = L.parse_ficha("Nome: Maria da Silva\nCPF: 123.456.789-00")
    assert c.get("nome") == "Maria da Silva"
    assert c.get("cpf") == "123.456.789-00"
