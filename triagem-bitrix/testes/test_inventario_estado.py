# -*- coding: utf-8 -*-
"""Testes do inventário agregado do estado (TAREFA 1 — frente de apoio).

Garantem o essencial do brief:
  · só CONTAGENS — nome, CPF, telefone, senha e texto de mensagem NUNCA saem;
  · estado corrompido -> código de saída 2 e mensagem clara (não vira `{}`);
  · estado válido -> código de saída 0.
"""
from __future__ import annotations

import importlib.util
import glob
import json
import os
import subprocess
import sys
import tempfile

from conftest import RAIZ

import cpf_util

# carrega o módulo (manutencao/ não é pacote; carrega pelo caminho)
_CAMINHO = os.path.join(RAIZ, "manutencao", "inventario_estado.py")
_spec = importlib.util.spec_from_file_location("inventario_estado", _CAMINHO)
inventario = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inventario)

# PII sintética (nunca pode aparecer na saída)
NOME = "Maria da Silva Santos"
TELEFONE = "18 99999-0000"
SENHA = "Abc123@x"
CPF = cpf_util.gerar_para_teste(7)          # 11 dígitos, DV válido, sintético
CPF_FMT = cpf_util.formatar(CPF)


def _lote_valido(lid="LOTE-BOM"):
    """Lote que `pode_enfileirar` aprova (fechado, CPF válido, arquivo baixado)."""
    return {
        "lote_id": lid,
        "status": "FECHADO_PRONTO",
        "fechado": True,
        "pronto": True,
        "requer_revisao": False,
        "identificacao": "OK",
        "cpf": CPF,
        "cpf_formatado": CPF_FMT,
        "arquivos": [{
            "file_id": "12345", "indice": 0, "nome_original": "doc.pdf",
            "baixado": True, "sha256": "a" * 64, "tamanho_bits": 1000,
        }],
        "file_ids": ["12345"],
        "mensagens": [{"id": 1, "texto": "kit completo"}],
    }


def _lote_sensivel(lid="LOTE-SENSIVEL"):
    """Lote com PII real e senha no texto — o alvo da asserção de não-vazamento."""
    lote = _lote_valido(lid)
    lote["nome"] = NOME
    lote["mensagens"] = [
        {"id": 1, "texto": f"Olá, meu nome é {NOME}, telefone {TELEFONE}."},
        {"id": 2, "texto": f"Senha de acesso ao GOV.BR: {SENHA}"},
    ]
    lote["motivos_revisao"] = [f"contato {TELEFONE} para confirmar"]
    lote["cpf_origem"] = "bitrix-nome"
    return lote


def _escrever_estado(estado: dict) -> str:
    fd, caminho = tempfile.mkstemp(prefix="estado-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(estado, fh, ensure_ascii=False)
    return caminho


def _rodar(*args):
    return subprocess.run([sys.executable, _CAMINHO, *args],
                          capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# ausência de PII
# ---------------------------------------------------------------------------
def test_dict_nao_contem_pii():
    estado = {"lotes": {"LOTE-A": _lote_sensivel("LOTE-A")}}
    inv = inventario.inventariar(estado)
    serializado = json.dumps(inv, ensure_ascii=False)
    for proibido in (NOME, CPF, CPF_FMT, TELEFONE, SENHA):
        assert proibido not in serializado, f"vazou PII: {proibido!r}"


def test_saida_texto_nao_contem_pii():
    estado = {"lotes": {"LOTE-A": _lote_sensivel("LOTE-A")}}
    caminho = _escrever_estado(estado)
    try:
        r = _rodar("--estado", caminho)
        assert r.returncode == 0, r.stderr
        saida = r.stdout + r.stderr
        for proibido in (NOME, CPF, CPF_FMT, TELEFONE, SENHA):
            assert proibido not in saida, f"vazou PII no texto: {proibido!r}"
    finally:
        os.unlink(caminho)


def test_saida_json_nao_contem_pii():
    estado = {"lotes": {"LOTE-A": _lote_sensivel("LOTE-A")}}
    caminho = _escrever_estado(estado)
    try:
        r = _rodar("--estado", caminho, "--json")
        assert r.returncode == 0, r.stderr
        saida = r.stdout + r.stderr
        for proibido in (NOME, CPF, CPF_FMT, TELEFONE, SENHA):
            assert proibido not in saida, f"vazou PII no JSON: {proibido!r}"
    finally:
        os.unlink(caminho)


# ---------------------------------------------------------------------------
# contagens
# ---------------------------------------------------------------------------
def test_total_de_lotes_e_estados():
    estado = {"lotes": {
        "LOTE-A": _lote_valido("LOTE-A"),
        "LOTE-B": _lote_valido("LOTE-B"),
    }}
    estado["lotes"]["LOTE-B"]["status"] = "ABERTO"
    estado["lotes"]["LOTE-B"]["fechado"] = False
    inv = inventario.inventariar(estado)
    assert inv["total_lotes"] == 2
    assert inv["por_status"]["FECHADO_PRONTO"] == 1
    assert inv["por_status"]["ABERTO"] == 1


def test_detecta_senha_no_texto():
    estado = {"lotes": {"LOTE-A": _lote_sensivel("LOTE-A")}}
    inv = inventario.inventariar(estado)
    assert inv["lotes_com_senha_no_texto"] == 1


def test_lote_sem_senha_nao_conta():
    estado = {"lotes": {"LOTE-A": _lote_valido("LOTE-A")}}
    inv = inventario.inventariar(estado)
    assert inv["lotes_com_senha_no_texto"] == 0


def test_conta_cpf_invalido():
    estado = {"lotes": {"LOTE-A": _lote_valido("LOTE-A")}}
    estado["lotes"]["LOTE-A"]["cpf"] = "11111111111"   # sequência repetida -> inválido
    inv = inventario.inventariar(estado)
    assert inv["cpfs_invalidos"] == 1


def test_cpf_valido_nao_conta_como_invalido():
    estado = {"lotes": {"LOTE-A": _lote_valido("LOTE-A")}}
    inv = inventario.inventariar(estado)
    assert inv["cpfs_invalidos"] == 0


def test_conta_ids_telegram_legados():
    estado = {"lotes": {"LOTE-A": _lote_valido("LOTE-A")}}
    estado["lotes"]["LOTE-A"]["file_ids"] = ["CQA123", "999"]
    inv = inventario.inventariar(estado)
    assert inv["ids_telegram_legados"] == 1


def test_conta_lote_sem_arquivo():
    estado = {"lotes": {"LOTE-A": _lote_valido("LOTE-A")}}
    estado["lotes"]["LOTE-A"]["arquivos"] = []
    estado["lotes"]["LOTE-A"]["file_ids"] = []
    inv = inventario.inventariar(estado)
    assert inv["lotes_sem_arquivo"] == 1


def test_arquivos_por_tipo():
    estado = {"lotes": {"LOTE-A": _lote_valido("LOTE-A")}}
    estado["lotes"]["LOTE-A"]["arquivos"] = [
        {"file_id": "1", "indice": 0, "nome_original": "a.pdf", "baixado": True},
        {"file_id": "2", "indice": 1, "nome_original": "b.mp3", "baixado": True},
        {"file_id": "3", "indice": 2, "nome_original": "c", "baixado": True},
    ]
    inv = inventario.inventariar(estado)
    assert inv["arquivos_por_tipo"]["documento"] == 1
    assert inv["arquivos_por_tipo"]["midia"] == 1
    assert inv["arquivos_por_tipo"]["desconhecido"] == 1


def test_nao_baixados_agrupados_por_fonte_e_erro():
    estado = {"lotes": {"LOTE-A": _lote_valido("LOTE-A")}}
    estado["lotes"]["LOTE-A"]["arquivos"] = [
        {"file_id": "1", "indice": 0, "nome_original": "a.pdf", "baixado": False,
         "fonte": "webhook:disk.file.get", "erro": "TimeoutError: timeout"},
        {"file_id": "2", "indice": 1, "nome_original": "b.pdf", "baixado": False,
         "fonte": None, "erro": None},
    ]
    inv = inventario.inventariar(estado)
    assert inv["nao_baixados"]["total"] == 2
    assert inv["nao_baixados"]["por_fonte"]["webhook:disk.file.get"] == 1
    assert inv["nao_baixados"]["por_fonte"]["(sem fonte)"] == 1


def test_pode_enfileirar_aprovados_e_bloqueios():
    estado = {"lotes": {
        "LOTE-A": _lote_valido("LOTE-A"),
        "LOTE-B": _lote_valido("LOTE-B"),
    }}
    estado["lotes"]["LOTE-B"]["cpf"] = ""          # sem CPF -> bloqueado
    inv = inventario.inventariar(estado)
    assert inv["pode_enfileirar"]["aprovados"] == 1
    assert any("CPF" in k for k in inv["pode_enfileirar"]["motivos_bloqueio"])


def test_cpf_origem_contado_sem_vazar_valor_real():
    estado = {"lotes": {"LOTE-A": _lote_sensivel("LOTE-A")}}
    inv = inventario.inventariar(estado)
    assert inv["cpf_origem"]["total"] == 1


# ---------------------------------------------------------------------------
# códigos de saída
# ---------------------------------------------------------------------------
def test_estado_corrompido_exit_2():
    fd, caminho = tempfile.mkstemp(prefix="estado-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{isto não é json válido")
    try:
        r = _rodar("--estado", caminho)
        assert r.returncode == 2, f"esperado 2, veio {r.returncode}"
        assert "corrompido" in (r.stdout + r.stderr).lower()
    finally:
        os.unlink(caminho)
        # a cópia preservada do corrompido (A2/F1-08) fica ao lado do arquivo
        for copia in glob.glob(caminho + ".corrompido-*"):
            os.unlink(copia)


def test_estado_ausente_exit_0():
    with tempfile.TemporaryDirectory() as d:
        caminho = os.path.join(d, "nao-existe.json")
        r = _rodar("--estado", caminho, "--json")
    assert r.returncode == 0, r.stderr
    inv = json.loads(r.stdout)
    assert inv["total_lotes"] == 0


def test_estado_valido_exit_0():
    caminho = _escrever_estado({"lotes": {"LOTE-A": _lote_valido("LOTE-A")}})
    try:
        r = _rodar("--estado", caminho)
        assert r.returncode == 0, r.stderr
        assert "total de lotes: 1" in r.stdout
    finally:
        os.unlink(caminho)
