# -*- coding: utf-8 -*-
"""TDD — o nome do cliente na triagem (corpus real de produção, 24/09/2026).

Cada valor abaixo veio de um lote que PASSOU pelo sistema e foi aceito como nome de
cliente. O objetivo é recusar esses e continuar aceitando nomes de verdade.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

import pytest  # noqa: E402

from nome_regras import parece_ruido  # noqa: E402
from triagem_grupo import _e_nome_valido  # noqa: E402

RUIDOS = [
    "Solteira", "Casada", "Domestica", "Certo", "Fatura.pdf",
    "É isso aq ?", "Dpp está nessa página", "aperto em continuar e não vai",
    "Não está conseguindo assinar", "Não avança nesse botão já assistir todo esse vídeo",
    "VAI RESPONDER DIGITADO : solteira ou casada no Papel ? Solteira",
    "MÃE RESPONSAVEL: PATRICIA", "Certidão de casamento Bianca",
    # caso REAL 25/09/2026: a cliente escreveu "Meu namorado" no Nome completo
    "Meu namorado", "Minha mãe", "Meu marido", "Minha filha", "Namorado",
    "Da minha mae", "Nome do meu filho", "Da minha mãe",
]

VALIDOS = [
    "Beatriz de teste dos santos", "ANA CAROLINA TESTE JORGE", "Joana Ficção Campos",
    "Maria Vitória Teste Gomes", "Elzira Teste Marques", "REBECA TESTÁ DE ALMEIDA",
    "Karina de Teste Moya", "Jhuliana Lara De Teste Costa",
]


@pytest.mark.parametrize("valor", RUIDOS)
def test_parece_ruido_recusa(valor):
    assert parece_ruido(valor) is True, valor


@pytest.mark.parametrize("valor", VALIDOS)
def test_parece_ruido_aceita_nome_de_verdade(valor):
    assert parece_ruido(valor) is False, valor


@pytest.mark.parametrize("valor", RUIDOS)
def test_triagem_recusa_no_lote(valor):
    # `_e_nome_valido` é quem decide o nome do lote lá na triagem
    assert _e_nome_valido(valor) is False, valor


@pytest.mark.parametrize("valor", VALIDOS)
def test_triagem_continua_aceitando_nome(valor):
    assert _e_nome_valido(valor) is True, valor


def test_nome_do_menor_nao_vira_a_mae():
    # cabeçalho real: a linha da MÃE não pode ganhar do nome do MENOR
    texto = ("= MENOR MATHEUS FICTICIO MACHADO DA SILVA\n"
             "= CPF  297.786.399-06\n"
             "= MÃE RESPONSAVEL:  PATRICIA FICTICIA DA SILVA")
    from triagem_grupo import extrair_nomes
    nomes = [n.upper() for n in extrair_nomes(texto)]
    assert not any("RESPONSAVEL" in n or n.startswith("MÃE") for n in nomes), nomes


def test_contato_compartilhado_nao_vira_nome():
    # bloco real do WhatsApp: 'Vidabbzao' é o apelido salvo no celular do operador
    texto = ("Contatos compartilhados\n\n\nNome: Vidabbzao\n"
             "Telefones: +551****4210_7667\n"
             "[I]Lembre-se de remover o _ ao usar os números[/I]")
    from triagem_grupo import extrair_nomes
    nomes = [n.upper() for n in extrair_nomes(texto)]
    assert not any("VIDABBZAO" in n for n in nomes), nomes


def test_uma_palavra_no_cabecalho_ainda_vale():
    # '= MARIA = CPF ...' — nome de uma palavra só no cabeçalho do grupo continua ok
    assert _e_nome_valido("MARIA") is True


def test_job_prefere_o_nome_do_lote_quando_a_ficha_vem_suja():
    """Caso real: lote com nome CERTO, ficha com conversa ('Fez a senha do gov')."""
    import lote_para_job as LPJ
    lote = {
        "lote_id": "LOTE-TESTE-1", "modo": "janela", "status": "FECHADO_PRONTO",
        "pronto": True, "requer_revisao": False, "identificacao": "OK",
        "nome": "DANIELA FICTICIA DA SILVA", "cpf_formatado": "068.749.851-16",
        "cpfs_distintos": ["06874985116"], "qtd_arquivos": 1, "arquivos": [],
        "mensagens": [{"id": 1, "autor": "Operador", "texto": "Fez a senha do gov"}],
        "pasta": "/tmp/nao-existe", "dialog_id": "chat100002",
        "autores": ["Operador"], "arquivos_meta": [], "inicio": 1, "fim": 2, "pronto_por": "traco",
    }
    sim = LPJ.montar_job(lote)
    nome = (sim["job"].get("cliente") or {}).get("nome") or ""
    assert nome.upper().startswith("DANIELA"), nome
    assert "senha" not in nome.lower(), nome


# --------------------------------------------------------------------------
# NR-01 — partículas e abreviações, sem aumentar falso positivo (homônimos)
# --------------------------------------------------------------------------
from nome_regras import chave_comparacao, nomes_equivalentes  # noqa: E402

EQUIVALENTES = [
    ("MARIA DE SOUZA FICTICIA", "MARIA SOUZA FICTICIA"),           # partícula
    ("Maria das Dores Ficticia", "MARIA DORES FICTÍCIA"),           # partícula + acento/caixa
    ("M. APARECIDA SANTOS", "MARIA APARECIDA SANTOS"),              # prenome abreviado
    ("MARIA A. SANTOS", "MARIA APARECIDA SANTOS"),                  # nome do meio abreviado
    ("JOSE D'AVILA FICTICIO", "JOSÉ DAVILA FICTICIO"),              # apóstrofo
]

HOMONIMOS_E_RISCOS = [
    ("MARIA SOUZA", "MARIA SILVA SOUZA"),          # sobrenome a menos: NÃO (precisa 2ª âncora)
    ("J. SILVA", "JOSE SILVA"),                    # só 1 nome por extenso: fraco demais
    ("MARIA APARECIDA S.", "MARIA APARECIDA SANTOS"),  # sobrenome abreviado: NÃO
    ("M. SANTOS", "J. SANTOS"),                    # inicial dos dois lados
    ("MARIA ANTONIA SANTOS", "MARIA APARECIDA SANTOS"),  # homônimos parciais
    ("ANA SOUZA FICTICIA", "ANA FICTICIA SOUZA"),  # ordem trocada
    ("M. APARECIDA SANTOS", "JOANA APARECIDA SANTOS"),   # inicial não bate
    ("MARIA", "MARIA"),                            # 1 palavra nunca basta
]


@pytest.mark.parametrize("a,b", EQUIVALENTES)
def test_nr01_equivalentes(a, b):
    assert nomes_equivalentes(a, b) and nomes_equivalentes(b, a)


@pytest.mark.parametrize("a,b", HOMONIMOS_E_RISCOS)
def test_nr01_homonimos_continuam_diferentes(a, b):
    assert not nomes_equivalentes(a, b) and not nomes_equivalentes(b, a)


def test_nr01_nao_transitivo_de_proposito():
    """A abreviação casa com dois homônimos, que entre si NÃO casam: o chamador
    (CPF por nome / nome por CPF) tem de exigir candidato único."""
    abrev = "MARIA A. SANTOS"
    assert nomes_equivalentes(abrev, "MARIA ANTONIA SANTOS")
    assert nomes_equivalentes(abrev, "MARIA APARECIDA SANTOS")
    assert not nomes_equivalentes("MARIA ANTONIA SANTOS", "MARIA APARECIDA SANTOS")


def test_nr01_chave_sem_particula_e_acento():
    assert chave_comparacao("Maria das Dores Fictícia") == "MARIA DORES FICTICIA"


def test_nr01_validacao_continua_conservadora():
    """NR-01 não mexe em validar_nome/parece_ruido: 'Da marya' segue recusado."""
    assert parece_ruido("Da marya") is True
