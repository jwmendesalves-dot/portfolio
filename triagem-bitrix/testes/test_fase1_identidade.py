# -*- coding: utf-8 -*-
"""Frente C2b (onda 3) — F1-05 (barreira de identidade) e F1-09 (CPF com DV).

Sem rede, CPFs sintéticos (cpf_util.gerar_para_teste), nomes fictícios.
"""
import datetime as dt

import cpf_util
import triagem_grupo as tg

CPF_A = cpf_util.gerar_para_teste(5101)
CPF_B = cpf_util.gerar_para_teste(5102)
FMT_A = cpf_util.formatar(CPF_A)
FMT_B = cpf_util.formatar(CPF_B)


def _invalido(cpf):
    """Mesmo CPF com o último dígito trocado (DV errado)."""
    return cpf[:10] + str((int(cpf[10]) + 1) % 10)


CPF_A_ERRADO = _invalido(CPF_A)
FMT_A_ERRADO = f"{CPF_A_ERRADO[:3]}.{CPF_A_ERRADO[3:6]}.{CPF_A_ERRADO[6:9]}-{CPF_A_ERRADO[9:]}"
BASE = dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.timezone.utc)


def _msgs(itens, autor="Ana"):
    out = []
    for i, it in enumerate(itens):
        texto, files = (it, []) if isinstance(it, str) else it
        out.append(tg.classificar_mensagem({
            "id": 1000 + i, "author_id": 7,
            "date": (BASE + dt.timedelta(minutes=i)).isoformat(),
            "text": f"[B]{autor}:[/B] {texto}",
            "params": {"FILE_ID": files},
        }, {}, tg.CONFIG_PADRAO))
    return out


def _agrupar(itens):
    cfg = dict(tg.CONFIG_PADRAO)
    cfg["modo_traco"] = "grupo_autor"
    return tg.agrupar(_msgs(itens), cfg, agora=BASE + dt.timedelta(minutes=30))


# --------------------------------------------------------------- F1-05
def test_saudacao_e_frase_de_servico_nao_sao_nome():
    assert tg.extrair_nomes("bom dia") == []
    assert tg.extrair_nomes("Segue documentos") == []
    assert tg.extrair_nomes("Boa tarde") == []
    assert not tg._e_nome_valido("bom dia")
    assert not tg._e_nome_valido("Segue documentos")
    # nome de verdade continua valendo
    assert tg.extrair_nomes("= JOANA TESTE LIMA") == ["JOANA TESTE LIMA"]


def test_primeira_linha_solta_so_vira_nome_em_cabecalho():
    assert tg.extrair_nomes("Maria Fictícia Souza") == []          # fala solta
    assert tg.extrair_nomes(f"MARIA FICTICIA SOUZA\n= CPF {FMT_A}") == ["MARIA FICTICIA SOUZA"]


def test_cabecalho_do_proprio_cliente_nao_corta_o_kit():
    """Caso da simulação da auditoria: 'bom dia' + documentos + cabeçalho real."""
    lotes = _agrupar([
        "-",
        "bom dia",
        ("", ["11"]),
        ("", ["12"]),
        f"= JOANA TESTE LIMA\n= CPF {FMT_A}",
        "-",
    ])
    assert not [l for l in lotes if l["status"] == "FECHADO_POR_TROCA_DE_PESSOA"], \
        [l.get("fechado_por") for l in lotes]
    assert len(lotes) == 1, "o bloco do traço inteiro é um kit só"
    com_arq = [l for l in lotes if l["file_ids"]]
    assert len(com_arq) == 1, [l.get("fechado_por") for l in lotes]
    l = com_arq[0]
    assert l.get("fechado_pelo_traco") is True
    assert l["file_ids"] == ["11", "12"]
    assert l["status"] != "FECHADO_POR_TROCA_DE_PESSOA", l.get("fechado_por")
    assert l["cpf"] == CPF_A and l["pronto"] is True


def test_nome_da_ficha_e_cabecalho_completo_sao_a_mesma_pessoa():
    lotes = _agrupar([
        "-",
        "Nome: JOANA LIMA",
        ("", ["21"]),
        "= JOANA TESTE LIMA",
        ("", ["22"]),
        "-",
    ])
    assert len(lotes) == 1, [l.get("fechado_por") for l in lotes]
    assert lotes[0]["file_ids"] == ["21", "22"] and lotes[0].get("fechado_pelo_traco")


def test_barreira_ainda_corta_outro_cliente_identificado():
    lotes = _agrupar([
        "-",
        f"= JOANA TESTE LIMA\n= CPF {FMT_A}",
        ("", ["31"]),
        f"= PEDRO FICTICIO SOUZA\n= CPF {FMT_B}",
        ("", ["32"]),
    ])
    interrompidos = [l for l in lotes if l["status"] == "FECHADO_POR_TROCA_DE_PESSOA"]
    assert len(interrompidos) == 1
    # (a foto sem texto colada no corte é realocada — PONTO 2.3; não é deste teste)
    assert interrompidos[0]["cpf"] == CPF_A and "32" not in interrompidos[0]["file_ids"]


# --------------------------------------------------------------- F1-09
def test_cpf_normalizar_e_plausivel_usam_dv():
    assert tg.cpf_normalizar(FMT_A) == CPF_A
    assert tg.cpf_plausivel(CPF_A)
    assert not tg.cpf_plausivel(CPF_A_ERRADO)
    assert not tg.cpf_plausivel("11111111111")


def test_cpf_com_pontuacao_e_dv_errado_nao_e_cpf():
    assert tg.extrair_cpfs(f"= CPF {FMT_A_ERRADO}") == []
    assert tg.extrair_cpfs(f"protocolo {FMT_A_ERRADO}") == []
    assert tg.extrair_cpfs_invalidos(f"= CPF {FMT_A_ERRADO}") == [CPF_A_ERRADO]
    assert tg.extrair_cpfs(f"= CPF {FMT_A}") == [CPF_A]          # 11 dígitos
    assert tg.extrair_cpfs(f"CPF {CPF_A}") == [CPF_A]
    # telefone rotulado continua fora (nem inválido)
    assert tg.extrair_cpfs_invalidos("Fone 18900000000") == []


def test_cpf_errado_mais_cpf_certo_nao_vira_conflito_mas_vai_para_revisao():
    lotes = _agrupar([
        "-",
        f"= JOANA TESTE LIMA\n= CPF {FMT_A_ERRADO}",
        ("", ["41"]),
        f"CPF correto {FMT_A}",
        "-",
    ])
    com_arq = [l for l in lotes if l["file_ids"]]
    assert len(com_arq) == 1, "CPF digitado errado não pode cortar o kit (barreira)"
    l = com_arq[0]
    assert l["status"] != "CONFLITO" and l["identificacao"] == "OK"
    assert l["cpf"] == CPF_A and l["cpfs_distintos"] == [CPF_A]
    assert l["cpfs_invalidos"] == [CPF_A_ERRADO]
    assert l["requer_revisao"] is True and l["pronto"] is False
    assert any(m.startswith("CPF_INVALIDO") for m in l["motivos_revisao"])
    assert CPF_A_ERRADO not in " ".join(l["motivos_revisao"])     # motivo sem o número
    assert tg.serializar_lote(l)["cpfs_invalidos"] == [CPF_A_ERRADO]


def test_lote_so_com_cpf_invalido_fica_sem_cpf_em_revisao():
    lotes = _agrupar([
        "-",
        f"= JOANA TESTE LIMA\n= CPF {FMT_A_ERRADO}",
        ("", ["51"]),
        "-",
    ])
    l = [l for l in lotes if l["file_ids"]][0]
    assert l["cpf"] is None and l["identificacao"] == "SEM_CPF"
    assert l["cpfs_invalidos"] == [CPF_A_ERRADO]
    assert l["requer_revisao"] is True and l["pronto"] is False
