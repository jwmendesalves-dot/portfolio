#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TESTE (falha antes, passa depois): o serviço contratado tem que sair do PDF
assinado e entrar no job da triagem como `cliente.beneficio`.

Contexto: a página de variáveis do contrato assinado tem a linha
    Serviço Contratado   {UfCrm1725033408826}   ADM Auxilio Maternidade Facultativo
O PrevSync lê essa linha, mas a TRIAGEM não levava o serviço para o job — o
separador então adivinhava o benefício e caía no padrão errado.

Hermético (OP-T2): lote e PDF sintéticos (`testes/fixtures/sinteticos.py`);
antes lia o estado.json de PRODUÇÃO e um contrato de cliente real.
"""
import pytest

import lote_para_job as L  # noqa: E402
from fixtures import sinteticos as F  # noqa: E402

PDFS = {}


@pytest.fixture(autouse=True)
def _pdf_sintetico(monkeypatch, tmp_path):
    F.instalar_texto_pdf(monkeypatch, L)
    monkeypatch.setattr(L, "SEM_REDE", True)
    PDFS["com"] = F.contrato_sintetico(tmp_path, com_tabela=True)
    PDFS["sem"] = F.contrato_sintetico(tmp_path, com_tabela=False)


def _lote_com_pdf(caminho_pdf: str) -> dict:
    """Lote sintético com a estrutura do estado.json (todas as chaves de serializar_lote)."""
    return F.lote_molde(nome=F.NOME_CLIENTE, cpf=F.CPF_CLIENTE,
                        arquivos=[F.arquivo_pdf(caminho_pdf)])


def test_servico_extraido_da_pagina_de_variaveis():
    """A linha 'Serviço Contratado' do PDF tem que virar cliente.beneficio."""
    pdfs = [PDFS["com"]]
    job = L.montar_job(_lote_com_pdf(pdfs[0]))["job"]
    benef = (job.get("cliente") or {}).get("beneficio")
    assert benef, f"cliente.beneficio vazio — o serviço não foi levado para o job (job={job.get('cliente')})"
    assert "maternidade" in benef.lower(), f"serviço errado: {benef!r}"


def test_servico_aparece_na_fichinha():
    """Quem lê a fichinha no painel precisa ver o serviço contratado."""
    pdfs = [PDFS["com"]]
    job = L.montar_job(_lote_com_pdf(pdfs[0]))["job"]
    ficha = job.get("fichinha") or ""
    assert "Serviço contratado" in ficha or "Serviço Contratado" in ficha, \
        f"a fichinha não mostra o serviço contratado:\n{ficha[:400]}"
    assert "aternidade" in ficha, "o valor do serviço não está na fichinha"


def test_sem_servico_gera_aviso_e_nao_inventa():
    """PDF sem a tabela: campo vazio + aviso — nunca chutar serviço."""
    vazio = [PDFS["sem"]]
    env = L.montar_job(_lote_com_pdf(vazio[0]))
    job = env["job"]
    assert not (job.get("cliente") or {}).get("beneficio"), "inventou serviço onde não havia"
    avisos = list(env.get("avisos") or []) + list(job.get("avisos") or [])
    assert any("servi" in a.lower() for a in avisos), "faltou aviso de serviço ausente"
