# -*- coding: utf-8 -*-
"""ID-09 / OP-T3 / F2-09 / F2-10 — montagem do job sem efeito colateral de rede.

- importar `nomear_lotes_por_cpf` não resolve webhook nem dá SystemExit;
- `codigo_pasta_do_cliente` usa `bitrix_cliente` (lazy), nunca derruba a montagem
  (nem com SystemExit legado), CPF inválido não consulta, falha não é cacheada;
- `montar_job` (job REAL da ponte): histórico sem "SIMULAÇÃO", `file_id` preservado;
- `lote_para_job.main` (simulação): offline, marca de simulação só nela.
"""
import importlib
import json

import pytest

import lote_para_job as L
from fixtures import sinteticos as F


@pytest.fixture(autouse=True)
def _limpo(monkeypatch):
    F.instalar_texto_pdf(monkeypatch, L)
    L._CACHE_PASTA.clear()
    yield
    L._CACHE_PASTA.clear()


def _com_rede(monkeypatch):
    monkeypatch.setattr(L, "SEM_REDE", False)
    monkeypatch.delenv("TRIAGEM_SEM_REDE", raising=False)


# ------------------------------------------------------------------ ID-09/OP-T3
def test_importar_nomear_lotes_nao_resolve_webhook_nem_encerra(monkeypatch):
    import seguranca
    import bitrix_cliente
    monkeypatch.setattr(seguranca, "ler_segredo",
                        lambda *a, **k: pytest.fail("webhook resolvido na importação"))
    monkeypatch.setattr(bitrix_cliente, "_webhook_cache", None)
    import nomear_lotes_por_cpf as N
    importlib.reload(N)                      # antes: SystemExit("webhook do Bitrix nao encontrado")
    assert not hasattr(N, "WEBHOOK")
    assert callable(N.buscar_cpf)


def test_pasta_systemexit_legado_nao_derruba_montagem(monkeypatch):
    """Antes: `except Exception` não pegava o SystemExit do import → processo morria."""
    import bitrix_cliente
    _com_rede(monkeypatch)

    def sai(*a, **k):
        raise SystemExit("webhook do Bitrix nao encontrado")

    monkeypatch.setattr(bitrix_cliente, "listar", sai)
    assert L.codigo_pasta_do_cliente(F.CPF_CLIENTE) == ""
    env = L.montar_job(F.lote_molde(nome=F.NOME_CLIENTE, cpf=F.CPF_CLIENTE))
    assert env["job"]["cliente"]["pasta"] == ""


def test_pasta_sem_webhook_devolve_vazio(monkeypatch):
    import bitrix_cliente
    import seguranca
    _com_rede(monkeypatch)
    monkeypatch.setattr(bitrix_cliente, "_webhook_cache", None)

    def ausente(*a, **k):
        raise seguranca.SegredoAusente("webhook_bitrix")

    monkeypatch.setattr(seguranca, "ler_segredo", ausente)
    assert L.codigo_pasta_do_cliente(F.CPF_CLIENTE) == ""


def test_pasta_cpf_invalido_nao_consulta(monkeypatch):
    import bitrix_cliente
    _com_rede(monkeypatch)
    monkeypatch.setattr(bitrix_cliente, "listar", lambda *a, **k: pytest.fail("consultou CPF inválido"))
    for cpf in ("", "123", "000.000.000-00", "111.111.111-11", F.CPF_CLIENTE[:-1] + "x"):
        assert L.codigo_pasta_do_cliente(cpf) == ""


def test_pasta_unica_vem_do_bitrix_cliente_e_e_cacheada(monkeypatch):
    import bitrix_cliente
    _com_rede(monkeypatch)
    chamadas = []

    def falso(metodo, params=None, **kw):
        chamadas.append((metodo, params))
        return [{"ID": "1", "UF_CRM_1726587995894": "2188 P"},
                {"ID": "2", "UF_CRM_1726587995894": ["2188 P"]}]

    monkeypatch.setattr(bitrix_cliente, "listar", falso)
    assert L.codigo_pasta_do_cliente(F.CPF_CLIENTE) == "2188 P"
    assert L.codigo_pasta_do_cliente(F.CPF_CLIENTE_FMT) == "2188 P"
    assert len(chamadas) == 1
    metodo, params = chamadas[0]
    assert metodo == "crm.contact.list"
    assert params["filter"] == {"UF_CRM_1524762221558": F.CPF_CLIENTE_FMT}


def test_pasta_divergente_nao_arrisca(monkeypatch):
    import bitrix_cliente
    _com_rede(monkeypatch)
    monkeypatch.setattr(bitrix_cliente, "listar", lambda *a, **k: [
        {"ID": "1", "UF_CRM_1726587995894": "2188 P"}, {"ID": "2", "UF_CRM_1726587995894": "8502 P"}])
    assert L.codigo_pasta_do_cliente(F.CPF_CLIENTE) == ""


def test_pasta_falha_nao_fica_no_cache(monkeypatch):
    import bitrix_cliente
    _com_rede(monkeypatch)
    estado = {"n": 0}

    def instavel(*a, **k):
        estado["n"] += 1
        if estado["n"] == 1:
            raise RuntimeError("timeout")
        return [{"ID": "1", "UF_CRM_1726587995894": "77 P"}]

    monkeypatch.setattr(bitrix_cliente, "listar", instavel)
    assert L.codigo_pasta_do_cliente(F.CPF_CLIENTE) == ""
    assert L.codigo_pasta_do_cliente(F.CPF_CLIENTE) == "77 P"


# ------------------------------------------------------------------ F2-10
def test_job_real_sem_marca_de_simulacao_e_com_file_id(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "SEM_REDE", True)
    pdf = F.contrato_sintetico(tmp_path)
    env = L.montar_job(F.lote_molde(arquivos=[F.arquivo_pdf(pdf, file_id="990123")]))
    eventos = " ".join(h["evento"] for h in env["job"]["historico"])
    assert "SIMULA" not in eventos.upper()
    assert env["arquivos"][0]["file_id"] == "990123"


def test_ponte_staging_usa_file_id(tmp_path, monkeypatch):
    """Consumidor (lote_para_fila.planejar): nome staged e sidecar pelo file_id."""
    lpf = pytest.importorskip("lote_para_fila")
    monkeypatch.setattr(L, "SEM_REDE", True)
    pdf = F.contrato_sintetico(tmp_path)
    sim = lpf.planejar(F.lote_molde(arquivos=[F.arquivo_pdf(pdf, file_id="990124")]))
    assert sim["vai_enviar"][0]["staged_nome"] == "990124.pdf"


# ------------------------------------------------------------------ F2-09
def test_main_simulacao_offline_e_marcada(tmp_path, monkeypatch, capsys):
    import bitrix_cliente
    import estado_store
    monkeypatch.delenv("TRIAGEM_SEM_REDE", raising=False)
    monkeypatch.setattr(L, "SEM_REDE", False)
    monkeypatch.setattr(bitrix_cliente, "listar", lambda *a, **k: pytest.fail("simulação usou rede"))
    monkeypatch.setattr(bitrix_cliente, "chamar", lambda *a, **k: pytest.fail("simulação usou rede"))
    lote = F.lote_molde(nome=F.NOME_CLIENTE, cpf=F.CPF_CLIENTE,
                        mensagens=[{"id": 1, "autor": "A", "data": "", "texto": "Senha: [senha removida]"}])
    est = tmp_path / "estado.json"
    estado_store.gravar_atomico({"lotes": {lote["lote_id"]: lote}}, str(est))
    assert L.main(["--estado", str(est), "--lote", lote["lote_id"], "--json"]) == 0
    sims = json.loads(capsys.readouterr().out)
    assert sims[0]["job"]["historico"][0]["evento"] == "enfileirado (SIMULAÇÃO — não gravado)"
    assert L.SEM_REDE is False, "main deve restaurar a flag"


def test_help_aponta_a_simulacao_fiel(capsys):
    with pytest.raises(SystemExit):
        L.main(["--help"])
    saida = capsys.readouterr().out
    assert "lote_para_fila.py --simular" in saida
    assert "OFFLINE" in saida
