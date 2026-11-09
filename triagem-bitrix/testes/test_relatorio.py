# -*- coding: utf-8 -*-
"""
TDD — relatório do bot na conversa (24/09/2026).

Pedido do dono:
  · *"o chatbot do bitrix pode ficar full time na conversa sem delay ... e responda
    um relatorio"*;
  · no Telegram: *"recebido - (nome do cliente) proximos"*.
Regra do traço (delimitador): o 1º traço ABRE; cada traço seguinte FECHA o de cima e
JÁ DEIXA O DE BAIXO ABERTO — então a conversa fica: ABRE uma vez, depois só FECHA.
"""
from conftest import RAIZ
import sys

import pytest

sys.path.insert(0, RAIZ)

from relatorio import (decidir_resposta, nome_do_bloco, nome_do_lote,
                       texto_abre, texto_fecha)


# ------------------------------------------------------------------ textos
def test_abre_no_bitrix_usa_o_traço():
    t = texto_abre("—")
    assert "Lote aberto" in t and "—" in t


def test_abre_no_telegram_usa_o_start():
    t = texto_abre("/start")
    assert "Lote aberto" in t and "/start" in t


def test_fecha_com_nome_e_contagem():
    t = texto_fecha("MARIA JOSÉ DA SILVA", 3, "—")
    assert "MARIA JOSE DA SILVA" in t and "3 arquivo" in t  # nome canônico (sem acento)
    assert "próxim" in t.lower()


def test_fecha_com_1_arquivo_no_singular():
    assert "1 arquivo" in texto_fecha("ANA", 1, "—")


def test_fecha_sem_nome_nao_escreve_None():
    t = texto_fecha(None, 2, "/start")
    assert "None" not in t and "2 arquivo" in t


# ------------------------------------------------------- decisão da resposta
def test_primeiro_sinal_abre():
    texto, novo = decidir_resposta(None, n_arquivos=0, sinal="—", msg="100",
                                   ts="2026-09-24T10:00:00+00:00", autor="Ana")
    assert "Lote aberto" in texto
    assert novo and novo["msg"] == "100"


def test_segundo_sinal_fecha_e_ja_deixa_o_proximo_aberto():
    aberto = {"msg": "100", "ts": "2026-09-24T10:00:00+00:00", "autor": "Ana"}
    texto, novo = decidir_resposta(aberto, nome="MARIA", n_arquivos=4,
                                   sinal="—", msg="200", ts="2026-09-24T10:10:00+00:00",
                                   autor="Ana")
    assert "MARIA" in texto and "4 arquivo" in texto
    assert novo and novo["msg"] == "200", "o mesmo traço abre o bloco de baixo"


def test_terceiro_sinal_tambem_fecha_nao_abre_de_novo():
    aberto = {"msg": "200", "ts": "2026-09-24T10:10:00+00:00", "autor": "Ana"}
    texto, _ = decidir_resposta(aberto, nome="JOÃO", n_arquivos=2, sinal="—",
                                msg="300", ts="2026-09-24T10:20:00+00:00", autor="Ana")
    assert "Lote aberto" not in texto and "JOÃO" in texto


def test_bloco_vencido_registra_fechamento_no_grupo():
    """Bloco aberto há mais que timeout_lote_min TAMBÉM é fechado no grupo.

    Bug 15/10/2026: o bloco vencido virava "Lote aberto" solto, e o grupo via o
    lote anterior sem fechamento. O correto: registra o fechamento do anterior
    (nome + arquivos) e já deixa o próximo aberto.
    """
    aberto = {"msg": "100", "ts": "2026-09-24T10:00:00+00:00", "autor": "Ana"}
    texto, _ = decidir_resposta(aberto, nome="MARIA", n_arquivos=4, sinal="—",
                                msg="500", ts="2026-09-24T14:30:00+00:00", autor="Bia",
                                timeout_min=120)
    assert "Lote aberto" not in texto
    assert "MARIA" in texto and "4 arquivo" in texto


# ------------------------------------------------------------- nome do lote
def test_nome_do_lote_pega_o_mais_completo():
    nomes = ["JULIA", "JULIA DOS SANTOS TESTE", "JULIA DOS SANTOS"]
    assert nome_do_lote(nomes) == "JULIA DOS SANTOS TESTE"


def test_nome_do_lote_ignora_lixo():
    assert nome_do_lote(["", "  ", "CPF DELE", "MARIA DA SILVA"]) == "MARIA DA SILVA"


def test_nome_do_lote_vazio():
    assert nome_do_lote([]) is None
    assert nome_do_lote(None) is None


# ------------------------------------- nome do bloco (dados REAIS do grupo)
# Textos colhidos do grupo em 24/09/2026 — o extrator genérico pegava todos.
def test_nome_do_bloco_prefere_o_cabecalho_da_ficha():
    textos = ["*DADOS NECESSÁRIOS PARA DAR A ENTRADA*👇🏻\n\nFICHA DE ATENDIMENTO*\n"
              "Nome: Raquel da Teste dos Santos \nIdade: 23"]
    assert nome_do_bloco(textos) == "Raquel da Teste dos Santos"


def test_nome_do_bloco_linha_Nome():
    assert nome_do_bloco(["NOME:  Andreza Do Teste De Castro \nCPF: 123"]) == "Andreza Do Teste De Castro"


def test_nome_do_bloco_menor_nao_pega_a_mae():
    from cpf_util import formatar, gerar_para_teste

    textos = [f"= MENOR ANA FICTICIA DE TESTE \n= CPF {formatar(gerar_para_teste(101))}\n"
              "= MAE RESPONSÁVEL BEATRIZ FICTICIA DE TESTE \n= BPC LOAS"]
    assert nome_do_bloco(textos) == "ANA FICTICIA DE TESTE"


def test_nome_do_bloco_nome_solto_em_minusculas():
    from cpf_util import formatar, gerar_para_teste

    texto = f"carla ficticia souza teste\nCPF: {formatar(gerar_para_teste(102))}"
    assert nome_do_bloco([texto]) == "carla ficticia souza teste"


@pytest.mark.parametrize("lixo", [
    "Dpp está nessa página",
    "É isso aq ?",
    "Não avança nesse botão já assistir todo esse vídeo",
    "BENEFICIO BPC LOAS FICHA",
    "Da marya",
    "= MAE RESPONSÁVEL GEANE RODRIGUES DO CARMO",
    "✅ *Lote aberto*",
    "Segue para a triagem",
    "• **E-mail:**Viviane Domingues antonio",
    "👤 **DADOS PESSOAIS",
])
def test_nome_do_bloco_recusa_lixo(lixo):
    assert nome_do_bloco([lixo]) is None
