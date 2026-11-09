"""Testes do critério de encaminhamento de lote (TDD — antes do código).

Decisão do dono (24/09/2026): a ponte passa a aceitar, além do lote com traço de
abertura E fechamento, o lote fechado por JANELA/TIMEOUT quando ele é de UM cliente só
e está limpo. Lote com cara de misturado ("cabeçalho de outro cliente", 2 CPFs, revisão,
conflito) continua fora.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

from ponte_criterio import hashes_do_lote, kit_novo, lote_aceitavel, motivo_recusa  # noqa: E402


def lote(modo="janela", status="FECHADO_POR_JANELA", fechado_por="janela de tempo",
         cpfs=("11122233344",), identificacao="OK", revisao=False, arquivos=5,
         pronto=True, abre=None, fecha=None, nome="Maria Aparecida da Silva"):
    # `nome` completo (2+ palavras) é obrigatório desde o CONTRATO DE NORMALIZAÇÃO v1
    # (14/10/2026): nome de 1 palavra/ruído NÃO vai para o painel — vira motivo de
    # revisão em ponte_criterio.motivo_recusa. Fixture sem nome = recusado por regra,
    # não por lote sujo (ver testes do nome no fim do arquivo).
    return {"lote_id": "LOTE-X", "modo": modo, "status": status, "fechado_por": fechado_por,
            "nome": nome,
            "cpfs_distintos": list(cpfs), "cpf": cpfs[0] if cpfs else None,
            "identificacao": identificacao, "requer_revisao": revisao,
            "arquivos": [f"a{i}.jpg" for i in range(arquivos)], "pronto": pronto,
            "sinal_abertura": abre, "sinal_fechamento": fecha}


def test_lote_com_traco_de_abertura_e_fechamento_e_aceito():
    lote_ok = lote(modo="marcadores", status="FECHADO_PRONTO", fechado_por="traço (fechamento)",
                   abre={"message_id": 1}, fecha={"message_id": 2})

    assert lote_aceitavel(lote_ok) is True


def test_lote_de_janela_limpo_de_um_cliente_e_aceito():
    assert lote_aceitavel(lote()) is True


def test_lote_de_timeout_limpo_e_aceito():
    assert lote_aceitavel(lote(status="FECHADO_POR_TIMEOUT",
                               fechado_por="fechado por timeout (120 min sem o traço)")) is True


def test_lote_com_cabecalho_de_outro_cliente_e_recusado():
    ruim = lote(fechado_por="cabeçalho de outro cliente (CPF diferente)")

    assert lote_aceitavel(ruim) is False
    assert "outro cliente" in motivo_recusa(ruim)


def test_lote_com_mais_de_um_cpf_e_recusado():
    ruim = lote(cpfs=("11122233344", "55566677788"))

    assert lote_aceitavel(ruim) is False
    assert "CPF" in motivo_recusa(ruim)


def test_lote_que_pede_revisao_e_recusado():
    assert lote_aceitavel(lote(revisao=True)) is False
    assert "revis" in motivo_recusa(lote(revisao=True)).lower()


def test_lote_com_conflito_e_recusado():
    assert lote_aceitavel(lote(identificacao="CONFLITO")) is False
    assert lote_aceitavel(lote(status="CONFLITO")) is False


def test_lote_sem_traco_de_abertura_mas_com_traco_de_fechamento_e_aceito_se_limpo():
    lote_ok = lote(modo="marcadores", status="FECHADO_PRONTO",
                   fechado_por="sinal de traço (fechamento)", abre=None, fecha={"message_id": 2})

    assert lote_aceitavel(lote_ok) is True


# ---------------------------------------------------------------- kit novo (sha256)

def lote_com_hashes(*hashes):
    return {"lote_id": "L", "pronto": True, "requer_revisao": False, "identificacao": "OK",
            "status": "FECHADO_POR_JANELA", "modo": "janela", "fechado_por": "janela de tempo",
            "cpfs_distintos": ["11122233344"], "cpf": "11122233344",
            "arquivos": [{"sha256": h, "nome_salvo": f"0{i}.jpg"} for i, h in enumerate(hashes)]}


def test_hashes_do_lote_usa_prefixo_de_12_como_o_registro_da_ponte():
    lote_x = lote_com_hashes("af08e94d2a8f8ad5dc62d93d9c1234", "bbbbccccdddd9999")

    assert hashes_do_lote(lote_x) == {"af08e94d2a8f", "bbbbccccdddd"}


def test_kit_com_arquivo_novo_e_considerado_novo():
    lote_x = lote_com_hashes("af08e94d2a8f8ad5", "NOVO1234567890abcd")

    assert kit_novo(lote_x, {"af08e94d2a8f"}) is True


def test_kit_todo_repetido_nao_e_novo():
    lote_x = lote_com_hashes("af08e94d2a8f8ad5", "bbbbccccdddd0000")

    assert kit_novo(lote_x, {"af08e94d2a8f", "bbbbccccdddd"}) is False


def test_kit_sem_hash_no_estado_nao_e_provado_novo():
    lote_x = lote_com_hashes()

    assert kit_novo(lote_x, {"af08e94d2a8f"}) is False


def test_lote_ja_enviado_antes_nao_tem_hash_anterior_e_entra():
    lote_x = lote_com_hashes("af08e94d2a8f8ad5")

    assert kit_novo(lote_x, set()) is True


def test_lote_sem_documento_e_recusado():
    assert lote_aceitavel(lote(arquivos=0)) is False
    assert "arquivo" in motivo_recusa(lote(arquivos=0))


def test_lote_nao_pronto_e_recusado():
    assert lote_aceitavel(lote(pronto=False)) is False
    assert lote_aceitavel(None) is False
    assert lote_aceitavel({}) is False


# ------------------------------------------- nome (CONTRATO DE NORMALIZAÇÃO v1, 14/10)

def test_lote_com_nome_de_uma_palavra_vai_para_revisao():
    """Regra do dono (14/10/2026): 1 palavra não identifica ninguém — CPF é a âncora."""
    ruim = lote(nome="Fulano")

    assert lote_aceitavel(ruim) is False
    assert "nome" in motivo_recusa(ruim)


def test_lote_sem_nome_vai_para_revisao():
    ruim = lote(nome=None)

    assert lote_aceitavel(ruim) is False
    assert "nome" in motivo_recusa(ruim)


def test_lote_com_ruido_no_lugar_do_nome_vai_para_revisao():
    for ruido in ("Solteira", "Link da procuração", "Fez a senha do gov"):
        assert lote_aceitavel(lote(nome=ruido)) is False, ruido


def test_lote_com_nome_completo_continua_aceito():
    assert lote_aceitavel(lote(nome="Maria Aparecida da Silva")) is True

