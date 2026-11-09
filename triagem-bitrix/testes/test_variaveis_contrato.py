#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TESTES do serviço + dos DEMAIS campos da página de variáveis do contrato.

A tabela assinada (ZapSign) traz, por exemplo (valores FICTÍCIOS):
    Nome               {Name}                  MARIA FICTICIA DOS TESTES
    Cidade             {UfCrm...}              Manaus
    Senha do MEU INSS  {UfCrm1725391896584}    <senha>
    Serviço Contratado {UfCrm1725033408826}    Auxílio Maternidade Facultativo
A triagem lia nome/CPF das mensagens do grupo (texto solto) e deixava o resto
para trás — inclusive a senha do INSS e o serviço. Agora a tabela assinada manda.

Hermético (OP-T2): lote e contrato vêm de `testes/fixtures/sinteticos.py`
(antes: estado.json de PRODUÇÃO + PDF de contrato de cliente real).
"""
import pytest

import lote_para_job as L  # noqa: E402
from fixtures import sinteticos as F  # noqa: E402


@pytest.fixture(autouse=True)
def _pdf_sintetico(monkeypatch, tmp_path):
    F.instalar_texto_pdf(monkeypatch, L)
    monkeypatch.setattr(L, "SEM_REDE", True)
    global COM_SERVICO, SEM_SERVICO
    COM_SERVICO = F.contrato_sintetico(tmp_path, com_tabela=True)
    SEM_SERVICO = F.contrato_sintetico(tmp_path, com_tabela=False)


COM_SERVICO = SEM_SERVICO = None


def _lote(caminho_pdf: str, mensagens=None) -> dict:
    """Lote sintético com a estrutura do estado.json, 1 PDF e mensagens controladas."""
    return F.lote_molde(mensagens=mensagens if mensagens is not None else [],
                        arquivos=[F.arquivo_pdf(caminho_pdf)])


def _campos(pdf=None, mensagens=None) -> dict:
    env = L.montar_job(_lote(pdf or COM_SERVICO, mensagens))
    return env["job"], env.get("campos_lidos_da_ficha") or {}, env.get("avisos") or [], env


# ---------------------------------------------------------------- serviço
def test_servico_extraido_da_pagina_de_variaveis():
    job, _c, _a, _e = _campos()
    benef = (job.get("cliente") or {}).get("beneficio") or ""
    assert benef, "cliente.beneficio vazio — serviço não foi levado ao job"
    assert "maternidade" in benef.lower(), f"serviço errado: {benef!r}"


def test_servico_aparece_na_fichinha():
    job, _c, _a, _e = _campos()
    ficha = job.get("fichinha") or ""
    assert "Serviço contratado" in ficha, "a fichinha não mostra o serviço contratado"
    assert "aternidade" in ficha, "o valor do serviço não está na fichinha"


def test_sem_servico_gera_aviso_e_nao_inventa():
    job, _c, avisos, env = _campos(pdf=SEM_SERVICO)
    assert not (job.get("cliente") or {}).get("beneficio"), "inventou serviço onde não havia"
    todos = list(avisos) + list(job.get("avisos") or [])
    assert any("servi" in a.lower() for a in todos), f"faltou aviso de serviço ausente: {todos}"


# ---------------------------------------------------------------- senha + campos
def test_senha_do_inss_vem_da_tabela():
    """A senha do MEU INSS tem que sair da tabela assinada, não do texto do grupo."""
    job, campos, _a, _e = _campos()
    senha = campos.get("senha") or ""
    assert senha == F.SENHA_FICTICIA, "senha não veio da tabela"
    assert F.SENHA_FICTICIA in (job.get("fichinha") or ""), "a senha não chegou na fichinha"


def test_campos_da_tabela_preenchem_o_job():
    """Nome, CPF, endereço, contato e benefício vêm todos da tabela."""
    job, campos, _a, _e = _campos()
    ficha = job.get("fichinha") or ""
    esperado_na_ficha = [F.NOME_CLIENTE, F.CPF_CLIENTE_FMT, F.BAIRRO, F.CIDADE,
                         F.UF, F.CEP, F.SENHA_FICTICIA, F.EMAIL_FICTICIO]
    faltando = [v for v in esperado_na_ficha if v not in ficha]
    assert not faltando, f"faltou na fichinha: {faltando}\n---\n{ficha[:900]}"
    assert F.TEL_CELULAR in ficha, "telefone da tabela não entrou"
    assert (job.get("cliente") or {}).get("cpf", "").replace(".", "").replace("-", "") == F.CPF_CLIENTE
    assert campos.get("estado civil"), "estado civil não preenchido"


def test_valor_lixo_da_tabela_nao_entra():
    """NIS '0' e Sobrenome vazio não podem virar dado do cliente."""
    _job, campos, _a, _e = _campos()
    assert campos.get("nis") in ("", None), f"NIS lixo entrou: {campos.get('nis')!r}"


def test_divergencia_da_ficha_perde_para_o_contrato_e_gera_aviso():
    """Se o texto do grupo discordar do contrato assinado, vale o contrato."""
    ficha_grupo = ("*DADOS NECESSÁRIOS PARA DAR A ENTRADA*\n"
                   f"Nome completo: {F.NOME_CLIENTE}\n"
                   f"CPF: {F.CPF_CLIENTE_FMT}\n"
                   f"Senha: {F.SENHA_FICHA_DIVERGENTE}\n"
                   "Cidade: Manaus\n")
    mensagens = [{"id": "m1", "texto": ficha_grupo, "autor": "grupo"}]
    job, campos, avisos, env = _campos(mensagens=mensagens)
    assert campos.get("senha") == F.SENHA_FICTICIA, "contrato deveria vencer"
    todos = list(avisos) + list(job.get("avisos") or [])
    assert any("senha" in a.lower() and "contrato" in a.lower() for a in todos), \
        f"faltou aviso de divergência de senha: {todos}"
    # S01: o aviso vai para log/estado — nunca com o valor de nenhuma das senhas
    assert not any(F.SENHA_FICTICIA in a or F.SENHA_FICHA_DIVERGENTE in a for a in todos), todos
