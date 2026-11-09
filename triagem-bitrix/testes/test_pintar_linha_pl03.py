# -*- coding: utf-8 -*-
"""PL-03 — pintar_linha: CPF inválido ou identidade inconclusiva NÃO cria linha.

Reproduz o defeito antigo: o `decidir` antigo criava linha para qualquer 11 dígitos,
inclusive CPF com DV errado e CPF achado por homônimo (`bitrix-nome`), gerando linha na
PASTAS PREVI GERAL com nome/CPF de outra pessoa.
Sem rede; só regras puras.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpf_util
import pintar_linha as pl

CABECALHO = [''] * 17


def planilha(linhas_de_dados):
    return [list(CABECALHO)] + [list(x) for x in linhas_de_dados]


def linha_vazia(numero=''):
    v = [''] * 17
    v[0] = numero
    return v


CPF_VALIDO = cpf_util.gerar_para_teste(1)
CPF_VALIDO_B = cpf_util.gerar_para_teste(2)
# 11 dígitos com DV ERRADO (não é sequência repetida; falha em cpf_util.valido)
CPF_DV_ERRADO = CPF_VALIDO[:9] + str((int(CPF_VALIDO[9:]) + 1) % 10) + CPF_VALIDO[10]
assert not cpf_util.valido(CPF_DV_ERRADO)


def test_pl03_cpf_com_dv_invalido_NAO_cria_linha():
    vals = planilha([linha_vazia('1 P')])
    plano = pl.decidir(vals, CPF_DV_ERRADO, 'CLIENTA QUALQUER', pintadas={})
    assert plano['acao'] == 'nada'
    assert plano['escritas'] == {}
    assert 'DV' in plano['motivo']


def test_pl03_cpf_de_homonimo_bitrix_nome_NAO_cria_linha():
    vals = planilha([linha_vazia('1 P')])
    plano = pl.decidir(vals, CPF_VALIDO, 'CLIENTA QUALQUER', pintadas={},
                       cpf_origem='bitrix-nome')
    assert plano['acao'] == 'nada'
    assert 'inconclusiva' in plano['motivo']


def test_pl03_lote_com_varios_cpfs_NAO_cria_linha():
    vals = planilha([linha_vazia('1 P')])
    plano = pl.decidir(vals, CPF_VALIDO, 'CLIENTA QUALQUER', pintadas={},
                       cpfs_distintos=[CPF_VALIDO, CPF_VALIDO_B])
    assert plano['acao'] == 'nada'
    assert 'inconclusiva' in plano['motivo']


def test_pl03_identidade_conclusa_cria_linha():
    vals = planilha([linha_vazia('1 P')])
    plano = pl.decidir(vals, CPF_VALIDO, 'CLIENTA QUALQUER', pintadas={},
                       cpf_origem='documento', cpfs_distintos=[CPF_VALIDO])
    assert plano['acao'] == 'criar'
    assert plano['escritas'][pl.COL_CPF] == cpf_util.formatar(CPF_VALIDO)


def test_pl03_cliente_que_JA_tem_linha_continua_sendo_pintado():
    """A trava é só para CRIAR linha nova; quem já tem linha é pintado normalmente."""
    vals = planilha([linha_vazia('1 P')])
    vals[1][pl.COL_CPF - 1] = cpf_util.formatar(CPF_DV_ERRADO)
    plano = pl.decidir(vals, CPF_DV_ERRADO, 'X', pintadas={})
    assert plano['acao'] == 'pintar'
    assert plano['linha'] == 2


def test_pl03_planejar_repassa_a_origem_do_lote():
    vals = planilha([linha_vazia('1 P')])
    lotes = [{'lote_id': 'L1', 'cpf': CPF_VALIDO, 'nome': 'ANA',
              'cpf_origem': 'bitrix-nome'}]
    planos, ignorados = pl.planejar(vals, lotes, pintadas={})
    assert planos == []
    assert len(ignorados) == 1
    assert 'inconclusiva' in ignorados[0]['motivo']
