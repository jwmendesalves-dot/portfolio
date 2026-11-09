# -*- coding: utf-8 -*-
"""S01 (lado do job): a senha gov.br/INSS só pode existir no campo que o painel
consome (linha "NIS/Senha do INSS" da fichinha). Nunca em avisos, variaveis_contrato,
prints, --json, histórico ou nomes de arquivo. Com a persistência sem senha (C1:
"[senha removida]"), o job não quebra e sai com S/SENHA + aviso, ou com a senha
relida da mensagem original (bitrix_cliente, leitura) quando houver rede.

Tudo sintético (testes/fixtures/sinteticos.py).
"""
import json
import shutil

import pytest

import lote_para_job as L
from fixtures import sinteticos as F

SENHA = F.SENHA_FICTICIA


@pytest.fixture(autouse=True)
def _isolado(monkeypatch, tmp_path):
    F.instalar_texto_pdf(monkeypatch, L)
    monkeypatch.setattr(L, "SEM_REDE", True)
    L._CACHE_PASTA.clear()
    yield
    L._CACHE_PASTA.clear()


def _ficha(senha_txt: str) -> str:
    return ("*DADOS NECESSÁRIOS PARA DAR A ENTRADA*\n"
            f"Nome completo: {F.NOME_CLIENTE}\n"
            f"CPF: {F.CPF_CLIENTE_FMT}\n"
            f"Senha: {senha_txt}\n"
            "Cidade: Manaus\n")


def _lote_texto(senha_txt: str, mensagem_id: int = 5001, **kw) -> dict:
    return F.lote_molde(nome=F.NOME_CLIENTE, cpf=F.CPF_CLIENTE,
                        mensagens=[{"id": mensagem_id, "autor": "Atendente", "data": "2099-12-31",
                                    "texto": _ficha(senha_txt)}], **kw)


def _linha_senha(ficha: str) -> str:
    """Valor da linha "NIS/Senha do INSS" ("" = linha vazia, que a ponte remove)."""
    for ln in ficha.splitlines():
        if "NIS/Senha do INSS" in ln:
            return ln.split(":**", 1)[-1].strip()
    raise AssertionError("fichinha sem a linha NIS/Senha do INSS")


def _sem_senha_utilizavel(ficha: str) -> bool:
    v = _linha_senha(ficha)
    return v == "" or v.endswith("/ S/SENHA")


def _tudo_menos_fichinha(env: dict) -> str:
    """Serializa tudo o que NÃO é a fichinha (o que pode ir a log/estado/backup)."""
    job = dict(env["job"])
    job.pop("fichinha")
    resto = dict(env)
    resto["job"] = job
    resto.pop("campos_lidos_da_ficha")    # só em memória (testes/painel); para exibir: para_exibir
    return json.dumps(resto, ensure_ascii=False, default=str)


# --------------------------------------------------------------- caminho normal
def test_senha_da_ficha_vai_so_para_a_fichinha():
    env = L.montar_job(_lote_texto(SENHA))
    assert f"/ {SENHA}" in env["job"]["fichinha"]
    assert SENHA not in _tudo_menos_fichinha(env)


def test_senha_da_tabela_nao_vai_para_variaveis_contrato(tmp_path):
    """Antes: job.variaveis_contrato['senha'] duplicava a senha na fila e nos backups."""
    pdf = F.contrato_sintetico(tmp_path, com_tabela=True)
    env = L.montar_job(F.lote_molde(arquivos=[F.arquivo_pdf(pdf)]))
    assert SENHA in env["job"]["fichinha"]
    assert "senha" not in env["job"]["variaveis_contrato"]
    assert env["job"]["variaveis_contrato"].get("servico_contratado")   # o resto continua
    assert SENHA not in _tudo_menos_fichinha(env)


def test_aviso_de_divergencia_nao_carrega_senha(tmp_path):
    """Antes: 'senha: a ficha do grupo dizia <s1>, o contrato assinado diz <s2>'."""
    pdf = F.contrato_sintetico(tmp_path, com_tabela=True)
    lote = _lote_texto(F.SENHA_FICHA_DIVERGENTE, arquivos=[F.arquivo_pdf(pdf)])
    env = L.montar_job(lote)
    assert any("senha" in a and "contrato" in a for a in env["avisos"])
    junto = " ".join(env["avisos"])
    assert SENHA not in junto and F.SENHA_FICHA_DIVERGENTE not in junto


def test_imprimir_e_json_nao_mostram_senha_nem_cpf(capsys, tmp_path):
    env = L.montar_job(_lote_texto(SENHA))
    L.imprimir(env, completo=True)
    saida = capsys.readouterr().out
    assert SENHA not in saida
    assert F.CPF_CLIENTE_FMT not in saida and F.CPF_CLIENTE not in saida
    exib = json.dumps(L.para_exibir(env), ensure_ascii=False)
    assert SENHA not in exib and F.CPF_CLIENTE_FMT not in exib
    # o job real não foi alterado por para_exibir
    assert f"/ {SENHA}" in env["job"]["fichinha"]


def test_main_json_offline_sem_senha(tmp_path, capsys):
    import estado_store
    est = tmp_path / "estado.json"
    lote = _lote_texto(SENHA)
    estado_store.gravar_atomico({"lotes": {lote["lote_id"]: lote}}, str(est))
    assert L.main(["--estado", str(est), "--todos-prontos", "--json"]) == 0
    saida = capsys.readouterr().out
    assert SENHA not in saida
    sims = json.loads(saida)
    assert sims[0]["job"]["historico"][0]["evento"].startswith("enfileirado (SIMULAÇÃO")


# --------------------------------------------------- persistência sem senha (C1)
def test_senha_removida_vira_s_senha_e_aviso_sem_quebrar():
    env = L.montar_job(_lote_texto(L.SENHA_REMOVIDA))
    ficha = env["job"]["fichinha"]
    assert L.SENHA_REMOVIDA not in ficha, "o painel gravaria '[senha removida]' como senha"
    # sem NIS: a linha fica vazia (fichinha_sem_vazios a remove; o painel grava S/SENHA)
    assert _linha_senha(ficha) == ""
    assert any("removida" in a and "mensagem original" in a for a in env["avisos"])
    assert env["job"]["cliente"]["cpf"] == F.CPF_CLIENTE_FMT       # resto do job intacto


def test_senha_removida_com_nis_vira_nis_barra_s_senha():
    ficha = L.montar_fichinha_painel({}, {"nis": "12345678901", "senha": L.SENHA_REMOVIDA})
    assert _linha_senha(ficha) == "12345678901 / S/SENHA"


@pytest.mark.parametrize("marcador", ["***", "S/SENHA", "[informada]"])
def test_placeholder_nunca_vira_senha_na_fichinha(marcador):
    ficha = L.montar_fichinha_painel({}, {"nis": "12345678901", "senha": marcador})
    assert "12345678901 / S/SENHA" in ficha


def test_senha_relida_da_mensagem_original_quando_ha_rede(monkeypatch):
    """Caminho mínimo: relê a mensagem (leitura) e põe a senha SÓ na fichinha."""
    import bitrix_cliente
    chamadas = []

    def falso_chamar(metodo, params=None, **kw):
        chamadas.append((metodo, dict(params or {})))
        return {"result": {"messages": [{"id": 5001, "text": _ficha(SENHA)}]}}

    monkeypatch.setattr(bitrix_cliente, "chamar", falso_chamar)
    monkeypatch.setattr(bitrix_cliente, "listar", lambda *a, **k: [])
    monkeypatch.setattr(L, "SEM_REDE", False)
    monkeypatch.delenv("TRIAGEM_SEM_REDE", raising=False)
    env = L.montar_job(_lote_texto(L.SENHA_REMOVIDA, dialog_id="chat000777"))
    assert f"/ {SENHA}" in env["job"]["fichinha"]
    assert SENHA not in _tudo_menos_fichinha(env)
    assert not any("removida" in a for a in env["avisos"])
    assert chamadas == [("im.dialog.messages.get",
                         {"DIALOG_ID": "chat000777", "FIRST_ID": 5000, "LIMIT": 1})]


def test_falha_ao_reler_mensagem_nao_derruba_job(monkeypatch):
    import bitrix_cliente

    def explode(*a, **k):
        raise RuntimeError("rede caiu")

    monkeypatch.setattr(bitrix_cliente, "chamar", explode)
    monkeypatch.setattr(bitrix_cliente, "listar", explode)
    monkeypatch.setattr(L, "SEM_REDE", False)
    monkeypatch.delenv("TRIAGEM_SEM_REDE", raising=False)
    env = L.montar_job(_lote_texto(L.SENHA_REMOVIDA))
    assert _sem_senha_utilizavel(env["job"]["fichinha"])
    assert env["job"]["cliente"]["pasta"] == ""


def test_sem_rede_nao_rele_mensagem(monkeypatch):
    import bitrix_cliente
    monkeypatch.setattr(bitrix_cliente, "chamar",
                        lambda *a, **k: pytest.fail("simulação offline chamou o Bitrix"))
    env = L.montar_job(_lote_texto(L.SENHA_REMOVIDA))
    assert _sem_senha_utilizavel(env["job"]["fichinha"])


# ------------------------------------------------- pdftotext real (se existir)
@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext (poppler) não instalado")
def test_pdftotext_real_le_a_tabela_do_contrato_sintetico(tmp_path, monkeypatch):
    monkeypatch.undo()                       # volta o _texto_pdf verdadeiro
    monkeypatch.setattr(L, "SEM_REDE", True)
    pdf = F.contrato_sintetico(tmp_path, com_tabela=True)
    tabela = L.ler_tabela_variaveis({"arquivos": [F.arquivo_pdf(pdf)]})
    assert tabela.get("senha") == SENHA
    assert "maternidade" in (tabela.get("servico_contratado") or "").lower()
    assert tabela.get("cidade") == F.CIDADE
