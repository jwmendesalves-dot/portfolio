# -*- coding: utf-8 -*-
"""TDD — prefixo 'NOME DO CLIENTE' na ficha da triagem (caso real 23/10/2026).

O lote LOTE-SIG-20261023-2259-xxxxxxxx (Lucas Ficticio Gomes de Oliveira,
CPF 634.706.847-82) tinha TRÊS arquivos baixados e estava PRONTO, mas nunca
entrou na fila:

    lote_aceitavel: False
    motivo_recusa: "nome não identificado (1 palavra/ruído)"

A ficha trazia o nome completo com o rótulo do formulário na frente:

    NOME DO CLIENTE Lucas Ficticio Gomes de Oliveira

RX_PAPEL_NO_INICIO remove 'CLIENTE' no início (^clientes?), mas a ficha começa
com 'NOME'. Sobrando 'NOME DO CLIENTE ...', PALAVRAS_NAO_NOME rejeita porque
'NOME' e 'CLIENTE' estão na lista — e o lote cai em revisão com o cliente
inteiro esperando.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

from nome_regras import normalizar_nome_candidato, validar_nome  # noqa: E402

ESPERADO_LUCAS = "Lucas Ficticio Gomes de Oliveira"


def test_nome_do_cliente_da_ficha_vira_nome():
    """A forma exata que veio da produção (Lucas Ficticio)."""
    assert normalizar_nome_candidato(
        "NOME DO CLIENTE Lucas Ficticio Gomes de Oliveira"
    ) == ESPERADO_LUCAS


def test_nome_do_cliente_com_dois_pontos():
    assert normalizar_nome_candidato("NOME DO CLIENTE: Maria Silva Santos") == "Maria Silva Santos"


def test_nome_do_cliente_com_hifen():
    assert normalizar_nome_candidato("NOME DO CLIENTE - Joao Pereira Lima") == "Joao Pereira Lima"


def test_nome_sozinho_como_rotulo():
    assert normalizar_nome_candidato("NOME: Ana Paula de Souza") == "Ana Paula de Souza"


def test_nome_completo_do_cliente_com_espacos_extras():
    assert normalizar_nome_candidato(
        "  NOME  DO  CLIENTE   Lucas Ficticio Gomes de Oliveira  "
    ) == ESPERADO_LUCAS


def test_cliente_no_inicio_continua_funcionando():
    """Regressão: o prefixo 'CLIENTE' que já era tratado não pode quebrar."""
    assert normalizar_nome_candidato("CLIENTE: Naiara Teste Alves") == "Naiara Teste Alves"


def test_nome_valido_apos_o_prefixo():
    """O canônico precisa PASSAR na camada 2 (é o que destrava a fila)."""
    canonico = normalizar_nome_candidato("NOME DO CLIENTE Lucas Ficticio Gomes de Oliveira")
    assert validar_nome(canonico, exigir_completo=True) is True


def test_prefixo_sem_nome_depois_continua_recusado():
    """'NOME DO CLIENTE' sozinho não pode virar nome de ninguém."""
    assert normalizar_nome_candidato("NOME DO CLIENTE") == ""
    assert normalizar_nome_candidato("NOME DO CLIENTE:") == ""


def test_ruido_conhecido_continua_recusado():
    """Regressão da suíte antiga: ruído não passa só porque agora tiramos NOME."""
    for ruim in ("Solteira", "Casada", "Fatura.pdf", "NOME"):
        assert normalizar_nome_candidato(ruim) == "", ruim


# ---------------------------------------------------------------------------
# O consumidor REAL: extrair_nomes() do triagem_grupo. O normalizador sozinho
# não basta — _e_nome_valido recebia o texto CRU e reprovava 'NOME'/'CLIENTE'
# (PALAVRAS_NAO_NOME) antes de qualquer prefixo ser descascado.
# ---------------------------------------------------------------------------
FICHA_LUCAS = """= NOME DO CLIENTE Lucas Ficticio Gomes de Oliveira 
=CPF: 63470684782
=LOAS / BPC Sim 
=SENHA @Paz2026
= COM LAUDO Sim 
=INDICAÇÃO: TATIANA 
DATA:22/10/2026"""


def test_extrair_nomes_da_ficha_real_do_lucas():
    """Ficha verbatim do lote que ficou preso (23/10/2026)."""
    import triagem_grupo as tg
    assert "Lucas Ficticio Gomes de Oliveira" in tg.extrair_nomes(FICHA_LUCAS)


def test_e_nome_valido_aceita_o_prefixo_da_ficha():
    import triagem_grupo as tg
    assert tg._e_nome_valido("NOME DO CLIENTE Lucas Ficticio Gomes de Oliveira") is True


def test_e_nome_valido_continua_barrando_ruido():
    """A casca não pode virar aceita-tudo: palavras-chave seguem fora."""
    import triagem_grupo as tg
    for ruim in ("CPF", "SENHA", "DATA", "MENOR", "NOME"):
        assert tg._e_nome_valido(ruim) is False, ruim
