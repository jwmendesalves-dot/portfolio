"""decisoes_jev — roteamento, regra de ouro, sombra, falha e PII. Sem rede: o JEV é
sempre um `_perguntar` falso. CPFs de cpf_util.gerar_para_teste; nomes fictícios."""
import json
import os

import pytest

import cpf_util
import decisoes_jev as D
import typesafe_cliente as T

CPF_CLI = cpf_util.gerar_para_teste(11)
CPF_OUTRO = cpf_util.gerar_para_teste(12)
CLIENTE = {"nome": "Maria Fictícia da Silva", "cpf": CPF_CLI}
OCR_IGUAL = f"CARTEIRA DE IDENTIDADE  Nome: MARIA FICTICIA DA SILVA  CPF {cpf_util.formatar(CPF_CLI)}"
OCR_OUTRO = f"RG  Nome: JOAO INVENTADO  CPF {CPF_OUTRO}"
OCR_SEM = "COMPROVANTE DE RESIDENCIA  Nome: MARIA F SILVA  Rua Inventada 10"


def cfg(tmp_path, modo):
    return {"ativo": True, "modo": modo, "sombra_jsonl": str(tmp_path / "jev.jsonl")}


def jev(answers, chamadas=None):
    def _p(state, questions, *, config=None):
        if chamadas is not None:
            chamadas.append({"state": state, "questions": questions})
        T.validar_questoes(questions)          # as perguntas montadas têm shape válido
        return {"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 2},
                "latencia_ms": 42}
    return _p


def jev_falha(exc):
    def _p(state, questions, *, config=None):
        raise exc
    return _p


def dono(choice, conf, score=0.0):
    return {"dono_documento": {"choice": choice, "confidence": conf, "probabilities": {}},
            "risco_documento_terceiro": {"score": score, "confidence": 0.9}}


def linhas(tmp_path):
    p = tmp_path / "jev.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# --- regra determinística ---------------------------------------------------
def test_fato_cpf():
    assert D.fato_cpf(CLIENTE, OCR_IGUAL) == "sim"
    assert D.fato_cpf(CLIENTE, OCR_OUTRO) == "nao"
    assert D.fato_cpf(CLIENTE, OCR_SEM) == "ausente"
    assert D.fato_cpf(CLIENTE, OCR_IGUAL + " " + OCR_OUTRO) == "ambiguo"
    invalido = CPF_CLI[:9] + str((int(CPF_CLI[9]) + 1) % 10) + CPF_CLI[10]
    assert D.fato_cpf(CLIENTE, f"CPF {invalido}") == "ausente"   # DV inválido não é identidade


@pytest.mark.parametrize("modo", [None, "desligado"])
def test_desligado_devolve_regra_sem_chamar(tmp_path, modo):
    chamadas = []
    c = {"ativo": False, "modo": "decide"} if modo is None else cfg(tmp_path, modo)
    r = D.documento_e_do_cliente(CLIENTE, OCR_SEM, config=c, _perguntar=jev(dono("mesmo_cliente", 1), chamadas))
    assert chamadas == [] and r["fonte"] == "regra" and r["acao"] == D.ACAO_QUARENTENA
    assert linhas(tmp_path) == []


def test_modo_invalido_vira_desligado(tmp_path):
    chamadas = []
    r = D.lote_misturado_acao({}, config=cfg(tmp_path, "turbo"), _perguntar=jev({}, chamadas))
    assert chamadas == [] and r["fonte"] == "regra"


# --- (a) documento ----------------------------------------------------------
def test_decide_cpf_igual_regra_decide_jev_confirma(tmp_path):
    r = D.documento_e_do_cliente(CLIENTE, OCR_IGUAL, config=cfg(tmp_path, "decide"),
                                 _perguntar=jev(dono("mesmo_cliente", 0.95)))
    assert r["acao"] == D.ACAO_KIT and r["fonte"] == "regra" and r["divergencia_jev"] is False


def test_decide_cpf_diferente_jev_nao_promove(tmp_path):
    r = D.documento_e_do_cliente(CLIENTE, OCR_OUTRO, config=cfg(tmp_path, "decide"),
                                 _perguntar=jev(dono("mesmo_cliente", 0.99)))
    assert r["acao"] == D.ACAO_QUARENTENA and r["fonte"] == "regra" and r["divergencia_jev"] is True


@pytest.mark.parametrize("choice, conf, score", [
    ("mesmo_cliente", 0.99, 0.0), ("mesmo_cliente", 1.0, 3.0), ("indefinido", 0.99, 0.0),
    ("mesmo_cliente", 0.5, 0.0), ("outro_cliente", 0.99, 3.0),
])
def test_jev_nunca_promove_dono_desconhecido(tmp_path, choice, conf, score):
    r = D.documento_e_do_cliente(CLIENTE, OCR_SEM, config=cfg(tmp_path, "decide"),
                                 _perguntar=jev(dono(choice, conf, score)))
    assert r["acao"] in (D.ACAO_QUARENTENA, D.ACAO_REVISAO)
    assert r["acao"] != D.ACAO_KIT


def test_decide_confianca_baixa_vai_a_revisao(tmp_path):
    r = D.documento_e_do_cliente(CLIENTE, OCR_SEM, config=cfg(tmp_path, "decide"),
                                 _perguntar=jev(dono("outro_cliente", 0.6)))
    assert r["acao"] == D.ACAO_REVISAO and r["fonte"] == "jev" and r["confianca"] == 0.6


def test_decide_outro_cliente_confiante_quarentena(tmp_path):
    r = D.documento_e_do_cliente(CLIENTE, OCR_SEM, config=cfg(tmp_path, "decide"),
                                 _perguntar=jev(dono("outro_cliente", 0.9)))
    assert r["acao"] == D.ACAO_QUARENTENA and r["fonte"] == "jev"


def test_state_nao_leva_cpf_cru(tmp_path):
    chamadas = []
    D.documento_e_do_cliente(CLIENTE, OCR_IGUAL + " " + OCR_OUTRO, config=cfg(tmp_path, "decide"),
                             _perguntar=jev(dono("mesmo_cliente", 0.9), chamadas))
    bruto = json.dumps(chamadas[0]["state"], ensure_ascii=False)
    for cpf in (CPF_CLI, CPF_OUTRO, cpf_util.formatar(CPF_CLI)):
        assert cpf not in bruto
    assert chamadas[0]["state"]["fatos"]["cpf_do_documento_igual_ao_do_cliente"] == "ambiguo"
    assert "cpf" not in chamadas[0]["state"]["cliente"]


def test_trecho_ocr_truncado():
    state, _ = D.montar_documento(CLIENTE, "x" * 10000)
    assert len(state["documento"]["texto_ocr"]) <= D.LIMITE_TRECHO_OCR


# --- falha nunca muda comportamento ------------------------------------------
@pytest.mark.parametrize("exc", [
    T.ErroCredencial("401"), T.ErroTransitorio("529"), T.ErroRequisicao("422"),
    TimeoutError(), RuntimeError("qualquer"), KeyError("x"),
])
@pytest.mark.parametrize("modo", ["sombra", "decide"])
def test_falha_jev_devolve_regra(tmp_path, exc, modo):
    for fn, args in ((D.documento_e_do_cliente, (CLIENTE, OCR_SEM)),
                     (D.foto_sem_cpf_vale, (CLIENTE, OCR_SEM, "jpg")),
                     (D.lote_misturado_acao, ({"n_documentos_sem_dono": 0},))):
        regra = fn(*args, config={"ativo": False})
        r = fn(*args, config=cfg(tmp_path, modo), _perguntar=jev_falha(exc))
        assert r["acao"] == regra["acao"] and r["fonte"] == "falha_jev"
    assert all(x.get("erro") for x in linhas(tmp_path))


def test_resposta_malformada_do_jev_vira_falha(tmp_path):
    r = D.documento_e_do_cliente(CLIENTE, OCR_SEM, config=cfg(tmp_path, "decide"),
                                 _perguntar=lambda s, q, config=None: None)
    assert r["fonte"] == "falha_jev" and r["acao"] == D.ACAO_QUARENTENA


def test_chave_ausente_real_vira_falha(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TRIAGEM_") and k not in ("TRIAGEM_BASE", "TRIAGEM_SEM_REDE"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "")
    monkeypatch.setattr("caminhos.SEGREDOS_DIR", str(tmp_path / "nada"))
    r = D.documento_e_do_cliente(CLIENTE, OCR_SEM, config=cfg(tmp_path, "decide"))
    assert r["fonte"] == "falha_jev"


# --- sombra -------------------------------------------------------------------
def test_sombra_nao_altera_decisao_e_grava_jsonl_sem_cpf(tmp_path):
    c = cfg(tmp_path, "sombra")
    ocr = OCR_SEM + f" CPF de terceiro {CPF_OUTRO[:9]}-{CPF_OUTRO[9:]}"
    regra = D.documento_e_do_cliente(CLIENTE, ocr, config={"ativo": False})
    r = D.documento_e_do_cliente(CLIENTE, ocr, config=c, _perguntar=jev(dono("outro_cliente", 0.97)))
    assert r == regra
    reg = linhas(tmp_path)
    assert len(reg) == 1 and reg[0]["modo"] == "sombra" and reg[0]["devolvido"] == "regra"
    # CPF válido de terceiro no OCR => fato "nao": a regra decide até no roteamento do JEV
    assert reg[0]["jev"]["acao"] == D.ACAO_QUARENTENA and reg[0]["jev"]["fonte"] == "regra"
    assert reg[0]["regra"]["fato"] == "nao"
    texto = (tmp_path / "jev.jsonl").read_text()
    for cpf in (CPF_CLI, CPF_OUTRO, cpf_util.formatar(CPF_CLI), cpf_util.formatar(CPF_OUTRO)):
        assert cpf not in texto
    assert "texto_ocr" not in texto and "JOAO" not in texto
    assert oct(os.stat(tmp_path / "jev.jsonl").st_mode & 0o777) == "0o600"


def test_sombra_registra_concordancia(tmp_path):
    D.documento_e_do_cliente(CLIENTE, OCR_OUTRO, config=cfg(tmp_path, "sombra"),
                             _perguntar=jev(dono("outro_cliente", 0.97)))
    reg = linhas(tmp_path)[0]
    assert reg["regra"]["acao"] == D.ACAO_QUARENTENA and reg["concorda"] is True
    assert reg["latencia_ms"] == 42 and reg["questoes"] == ["dono_documento", "risco_documento_terceiro"]


def test_decide_tambem_registra(tmp_path):
    D.lote_misturado_acao({"n_documentos_sem_dono": 0}, config=cfg(tmp_path, "decide"),
                          _perguntar=jev({"acao_lote_misto": {"choice": "separar", "confidence": 0.9}}))
    assert linhas(tmp_path)[0]["devolvido"] == "jev"


# --- (b) foto sem CPF ---------------------------------------------------------
@pytest.mark.parametrize("choice, conf, esperado", [
    ("provavelmente_do_cliente", 0.9, D.ACAO_REVISAO),
    ("provavelmente_do_cliente", 0.5, D.ACAO_QUARENTENA),
    ("provavelmente_de_outra_pessoa", 0.99, D.ACAO_QUARENTENA),
    ("sem_elementos", 0.99, D.ACAO_QUARENTENA),
])
def test_foto_sem_cpf_nunca_aceita(tmp_path, choice, conf, esperado):
    r = D.foto_sem_cpf_vale(CLIENTE, OCR_SEM, "jpg", config=cfg(tmp_path, "decide"),
                            _perguntar=jev({"foto_relacionada": {"choice": choice, "confidence": conf}}))
    assert r["acao"] == esperado and r["acao"] in (D.ACAO_QUARENTENA, D.ACAO_REVISAO)


def test_foto_regra_padrao_quarentena():
    assert D.foto_sem_cpf_vale(CLIENTE, OCR_SEM, "jpg", config={"ativo": False})["acao"] == D.ACAO_QUARENTENA


# --- (c) lote misturado -------------------------------------------------------
@pytest.mark.parametrize("choice, conf, sem_dono, esperado", [
    ("separar", 0.9, 0, D.ACAO_SEPARAR),
    ("separar", 0.9, 2, D.ACAO_REVISAO),        # separar com doc sem dono = promover desconhecido
    ("separar", 0.9, None, D.ACAO_REVISAO),
    ("pedir_reenvio", 0.95, 1, D.ACAO_REENVIO),
    ("separar", 0.7, 0, D.ACAO_REVISAO),        # confiança baixa
    ("revisao_humana", 0.99, 0, D.ACAO_REVISAO),
])
def test_lote_misturado(tmp_path, choice, conf, sem_dono, esperado):
    resumo = {"n_arquivos": 8, "n_cpfs_validos_distintos": 2}
    if sem_dono is not None:
        resumo["n_documentos_sem_dono"] = sem_dono
    r = D.lote_misturado_acao(resumo, config=cfg(tmp_path, "decide"),
                              _perguntar=jev({"acao_lote_misto": {"choice": choice, "confidence": conf}}))
    assert r["acao"] == esperado


def test_lote_state_descarta_campos_fora_da_lista():
    state, _ = D.montar_lote({"n_arquivos": 3, "cpfs": [CPF_CLI], "nome_cliente": "Maria"})
    assert state == {"lote": {"n_arquivos": 3}}


def test_perguntas_montadas_tem_shape_valido():
    for _, q in (D.montar_documento(CLIENTE, OCR_SEM), D.montar_foto(CLIENTE, OCR_SEM, "pdf"),
                 D.montar_lote({})):
        T.validar_questoes(q)
