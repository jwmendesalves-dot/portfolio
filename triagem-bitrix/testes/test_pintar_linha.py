# -*- coding: utf-8 -*-
"""Testes do pintar_linha.py (triagem -> planilha em roxo). Sem rede: só as regras.

    /opt/qc-planilha/venv/bin/python -m pytest /opt/triagem-bitrix/testes -q
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpf_util
import pintar_linha as pl

# CPFs sintéticos com DV válido (PL-03: criar linha exige cpf_util.valido)
CPF_A = cpf_util.gerar_para_teste(1)
CPF_B = cpf_util.gerar_para_teste(2)
CPF_C = cpf_util.gerar_para_teste(3)
CPF_D = cpf_util.gerar_para_teste(4)


# --------------------------------------------------------------- fixtures
CABECALHO = [' N.º', 'NOME CLIENTE', 'CIDADE', 'FONTE', 'DATA', 'BENECIFÍOS', 'CPF',
             'SENHA INSS', 'ADM AUX DOENCA', 'JUDICIAL AUX DOENÇA', 'ADM AUX ACIDENTE',
             'JUDICIAL AUX ACIDENTE', 'ID CRM', '', '', '', 'JÁ NA PLANILHA']


def planilha(linhas_de_dados):
    """Primeira linha = cabeçalho (índice 0); devolve lista de listas como o Sheets."""
    return [list(CABECALHO)] + [list(x) for x in linhas_de_dados]


def linha(numero='', nome='', cidade='', cpf='', data='', benef=''):
    v = [''] * 17
    v[0] = numero
    v[1] = nome
    v[2] = cidade
    v[4] = data
    v[5] = benef
    v[6] = cpf
    return v


# ------------------------------------------------------------------ CPF
def test_cpf_norm_aceita_formatado_e_incompleto():
    assert pl.cpf_norm('081.885.764-17') == '08188576417'
    assert pl.cpf_norm('11.057.007-39') == '01105700739'      # 10 dígitos ganham zero
    assert pl.cpf_norm('') == ''
    assert pl.cpf_norm('123') == ''
    assert pl.cpf_norm(None) == ''


def test_cpf_formatado_para_log():
    assert pl.cpf_formatado('08188576417') == '081.885.764-17'
    assert pl.cpf_formatado('') == ''


# ---------------------------------------------------------------- índice
def test_indice_cpf_por_linha_da_planilha():
    vals = planilha([
        linha('1 P', 'ANA', 'VG', '111.222.333-44'),
        linha('2 P', 'BRUNO', 'CBÁ', '222.333.444-55'),
        linha('3 P', 'ANA DE NOVO', 'VG', '11122233344'),
        linha('4 P', 'SEM CPF', 'VG', ''),
    ])
    idx = pl.indice_cpf(vals)
    assert idx['11122233344'] == [2, 4]
    assert idx['22233344455'] == [3]
    assert '' not in idx


def test_linha_livre_ignora_a_numeracao_da_coluna_a():
    """A equipe pré-numera a coluna A: '10411 P' NÃO quer dizer linha ocupada."""
    vals = planilha([
        linha('10409 P', 'FULANO', 'VG', '111.222.333-44'),
        linha('10410 P', '', '', ''),          # <- livre (linha 3)
        linha('10411 P', '', '', ''),
    ])
    assert pl.linha_livre(vals) == 3


def test_linha_livre_quando_o_bloco_lido_acabou_usa_a_proxima():
    """A grade tem 10.407 linhas: acabou o bloco de dados, a próxima linha é a livre."""
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44')])
    assert pl.linha_livre(vals) == 3


def test_linha_livre_NUNCA_usa_buraco_no_meio_do_historico():
    """Caso real: a planilha tem 55 linhas vazias no meio (1222, 1679, 3492...).
    Cliente novo tem de entrar DEPOIS da última linha usada, nunca no meio."""
    vals = planilha([
        linha('1 P', 'ANA', 'VG', '111.222.333-44'),      # 2 ocupada
        linha('2 P', '', '', ''),                          # 3 BURACO no meio
        linha('3 P', 'BRUNO', 'CBÁ', '222.333.444-55'),    # 4 ocupada
        linha('4 P', '', '', ''),                           # 5 livre no fim
    ])
    assert pl.linha_livre(vals) == 5


def test_planejar_reserva_a_linha_de_cada_cliente_novo():
    """Dois clientes novos NÃO podem cair na mesma linha livre."""
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44')])
    lotes = [{'lote_id': 'L1', 'cpf': CPF_A, 'nome': 'JULIO'},
             {'lote_id': 'L2', 'cpf': CPF_B, 'nome': 'MARIA'}]
    planos, ignorados = pl.planejar(vals, lotes, pintadas={})
    assert [(p['acao'], p['linha']) for p in planos] == [('criar', 3), ('criar', 4)]
    assert ignorados == []


def test_planejar_mistura_existente_e_novo():
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44')])
    lotes = [{'lote_id': 'L1', 'cpf': '11122233344', 'nome': 'ANA'},
             {'lote_id': 'L2', 'cpf': CPF_A, 'nome': 'JULIO'}]
    planos, _ = pl.planejar(vals, lotes, pintadas={})
    assert [(p['acao'], p['linha']) for p in planos] == [('pintar', 2), ('criar', 3)]


def test_dedupe_por_cpf_mantem_um_lote_por_cliente():
    """O mesmo cliente cai em 2-3 lotes (complemento de documentos). Pinta uma vez."""
    lotes = [
        {'lote_id': 'LOTE-1', 'cpf': '007.652.042-07', 'fim': '2026-11-06T09:00:00+03:00'},
        {'lote_id': 'LOTE-2', 'cpf': '00765204207', 'fim': '2026-11-06T10:00:00+03:00'},
        {'lote_id': 'LOTE-3', 'cpf': '111.222.333-44', 'fim': '2026-11-06T11:00:00+03:00'},
        {'lote_id': 'LOTE-4', 'cpf': '', 'fim': '2026-11-06T12:00:00+03:00'},
    ]
    unicos = pl.dedupe_por_cpf(lotes)
    assert [l['lote_id'] for l in unicos] == ['LOTE-1', 'LOTE-3']


# --------------------------------------------------------------- decisão
def test_decidir_cliente_que_ja_tem_linha_so_pinta():
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44')])
    plano = pl.decidir(vals, '11122233344', 'ANA', pintadas={})
    assert (plano['acao'], plano['linha'], plano['escritas']) == ('pintar', 2, {})


def test_decidir_cliente_novo_cria_e_pinta():
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44'),
                     linha('2 P', '', '', '')])
    plano = pl.decidir(vals, CPF_A, 'JULIO TESTE DA SILVA FILHO', pintadas={})
    assert plano['acao'] == 'criar'
    assert plano['linha'] == 3
    assert plano['escritas'] == {2: 'JULIO TESTE DA SILVA FILHO', 7: cpf_util.formatar(CPF_A)}


def test_decidir_nao_repinta_o_que_ja_foi_pintado():
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44')])
    pintadas = {'11122233344': {'linha': 2, 'em': '2026-11-06T10:00:00'}}
    assert pl.decidir(vals, '11122233344', 'ANA', pintadas)['acao'] == 'nada'


def test_decidir_cpf_invalido_nao_faz_nada():
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44')])
    plano = pl.decidir(vals, '123', 'ANA', pintadas={})
    assert plano['acao'] == 'nada'
    assert 'cpf' in plano['motivo']


def test_decidir_planilha_cheia_cria_depois_da_ultima():
    vals = planilha([linha('1 P', 'ANA', 'VG', '111.222.333-44')])
    plano = pl.decidir(vals, CPF_A, 'NOVO', pintadas={})
    assert (plano['acao'], plano['linha']) == ('criar', 3)


# ------------------------------------------------------------------ cor
def test_cor_exata_mandada_pelo_dono():
    assert pl.COR_ROXO == (180, 166, 217)
    assert pl.cor_hex() == '#B4A6D9'


def test_request_pinta_a_linha_INTEIRA_de_roxo():
    req = pl.requests_pintar(gid=0, linha=4521, colunas=17)
    faixas = [r['repeatCell']['range'] for r in req]
    for f in faixas:
        assert (f['sheetId'], f['startRowIndex'], f['endRowIndex']) == (0, 4520, 4521)
    # A..M (0-12) + Q (16-17); N, O e P estão PROTEGIDAS na planilha e não podem ser pintadas
    assert [(f['startColumnIndex'], f['endColumnIndex']) for f in faixas] == [(0, 13), (16, 17)]
    cor = req[0]['repeatCell']['cell']['userEnteredFormat']['backgroundColor']
    assert cor == pytest.approx({'red': 180 / 255, 'green': 166 / 255, 'blue': 217 / 255})
    assert req[0]['repeatCell']['fields'] == 'userEnteredFormat.backgroundColor'


def test_request_nunca_toca_coluna_protegida():
    req = pl.requests_pintar(gid=7, linha=10, colunas=17)
    for r in req:
        ini = r['repeatCell']['range']['startColumnIndex']
        fim = r['repeatCell']['range']['endColumnIndex']
        for (p_ini, p_fim) in pl.COLUNAS_PROTEGIDAS:
            assert fim <= p_ini or ini >= p_fim, f'faixa {ini}-{fim} invade protegida {p_ini}-{p_fim}'


# ------------------------------------------------------- lotes prontos
def _lote(lid, cpf, nome, fim):
    return {'lote_id': lid, 'status': 'FECHADO_PRONTO', 'pronto': True,
            'cpf': cpf, 'nome': nome, 'fim': fim}


def test_ler_lotes_prontos_respeita_o_corte_de_data():
    estado = {'lotes': {
        'a': _lote('LOTE-A', '11122233344', 'ANA', '2026-11-06T10:00:00+03:00'),
        'b': _lote('LOTE-B', '55566677788', 'BRUNO', '2026-01-10T10:00:00+03:00'),
        'c': {'lote_id': 'LOTE-C', 'pronto': False, 'cpf': '99988877766'},
    }}
    corte = dt.datetime(2026, 11, 1, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    lotes = pl.ler_lotes_prontos(estado, desde=corte)
    assert [l['lote_id'] for l in lotes] == ['LOTE-A']
    cheio = pl.ler_lotes_prontos(estado, desde=None)
    assert {l['lote_id'] for l in cheio} == {'LOTE-A', 'LOTE-B'}   # sem corte = backfill


def test_ler_lotes_prontos_ordena_do_mais_antigo_para_o_mais_novo():
    estado = {'lotes': {
        'x': _lote('LOTE-2', '22233344455', 'B', '2026-11-06T12:00:00+03:00'),
        'y': _lote('LOTE-1', '11122233344', 'A', '2026-11-06T09:00:00+03:00'),
    }}
    assert [l['lote_id'] for l in pl.ler_lotes_prontos(estado, desde=None)] == ['LOTE-1', 'LOTE-2']
