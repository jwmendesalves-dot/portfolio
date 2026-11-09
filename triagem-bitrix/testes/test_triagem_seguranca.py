# -*- coding: utf-8 -*-
"""Frente C1 (onda 2) — segurança em triagem_grupo.py (S01, S06, S07, alias TRIAGEM_DIR).

Sem rede, sem segredo real: webhook/chave fictícios por variável de ambiente.
"""
import datetime as dt
import inspect
import io
import logging
import os
import subprocess
import sys
import urllib.error

import pytest

import bitrix_cliente
import cpf_util
import triagem_grupo as tg
from conftest import RAIZ

WH = "https://exemplo.bitrix24.com.br/rest/1/tokenficticio9876/"
TOKEN = "tokenficticio9876"


@pytest.fixture(autouse=True)
def _sem_cache(monkeypatch):
    monkeypatch.setattr(bitrix_cliente, "_webhook_cache", None)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "")           # nenhum legado do sistema real
    monkeypatch.setenv("TRIAGEM_SEGREDOS_DIR", "/nao/existe/segredos")
    yield
    tg.log.handlers.clear()


# ---------------------------------------------------------------- S06
def test_webhook_vem_do_segredo_sem_varrer_disco(monkeypatch):
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", WH)

    def proibido(*a, **k):
        raise AssertionError("varreu o disco atrás de webhook (S06)")

    monkeypatch.setattr(os, "walk", proibido)
    assert tg.descobrir_webhook() == WH
    assert tg.candidatos_webhook() == [WH]


def test_sem_webhook_configurado_erro_claro_sem_citar_outros_projetos(monkeypatch):
    monkeypatch.delenv("TRIAGEM_WEBHOOK_BITRIX", raising=False)
    with pytest.raises(RuntimeError) as exc:
        tg.descobrir_webhook()
    msg = str(exc.value)
    assert "PrevSync" not in msg and "mcp" not in msg
    assert tg.candidatos_webhook() == []


def test_codigo_nao_procura_webhook_em_outros_projetos_nem_em_tmp():
    fonte = inspect.getsource(tg)
    for proibido in ("/root/trampo", "bitrix24_mcp_server", "/tmp/b24", "os.walk("):
        assert proibido not in fonte, f"triagem_grupo ainda referencia {proibido!r} (S06)"


def test_ler_chave_bot_usa_ler_segredo(monkeypatch):
    monkeypatch.setenv("TRIAGEM_VIBECODE_KEY", "chave-ficticia-123")
    assert tg.ler_chave_bot() == "chave-ficticia-123"


def test_ler_chave_bot_ausente_levanta_sem_vazar(monkeypatch):
    monkeypatch.delenv("TRIAGEM_VIBECODE_KEY", raising=False)
    with pytest.raises(RuntimeError):
        tg.ler_chave_bot()


# ---------------------------------------------------------------- S07 / S04
def test_log_e_excecao_nunca_mostram_o_token_do_webhook(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(tg, "ARQ_LOG", str(tmp_path / "triagem.log"))
    monkeypatch.setattr(tg, "BASE_DIR", str(tmp_path))
    tg.configurar_log()
    monkeypatch.setattr(tg.time, "sleep", lambda *_a, **_k: None)

    def falha(url, dados, headers, timeout):
        raise urllib.error.HTTPError(url, 500, "erro", {}, io.BytesIO(f'{{"error":"x","u":"{url}"}}'.encode()))

    monkeypatch.setattr(tg, "_requisicao", falha)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as exc:
            tg.http_json(WH + "profile.json", {}, tentativas=2)        # sem rotulo: o pior caso
    for h in tg.log.handlers:
        h.flush()
    conteudo = open(tmp_path / "triagem.log", encoding="utf-8").read()
    assert TOKEN not in conteudo, "webhook com token gravado no arquivo de log (S07)"
    assert TOKEN not in caplog.text
    assert TOKEN not in str(exc.value)


def test_configurar_log_instala_mascara_de_cpf_e_senha(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "ARQ_LOG", str(tmp_path / "triagem.log"))
    monkeypatch.setattr(tg, "BASE_DIR", str(tmp_path))
    tg.configurar_log()
    cpf = cpf_util.formatar(cpf_util.gerar_para_teste(3))
    tg.log.info("cliente cpf %s senha: Segr3doFict webhook %s", cpf, WH)
    for h in tg.log.handlers:
        h.flush()
    conteudo = open(tmp_path / "triagem.log", encoding="utf-8").read()
    assert cpf not in conteudo and "Segr3doFict" not in conteudo and TOKEN not in conteudo


# ---------------------------------------------------------------- S01
def test_serializar_lote_remove_senha_e_mantem_o_resto():
    cpf = cpf_util.formatar(cpf_util.gerar_para_teste(5))
    agora = dt.datetime.now(dt.timezone.utc)
    lote = {"lote_id": "LOTE-X", "status": "FECHADO_PRONTO", "motivos_revisao": [], "cpf": None,
            "cpfs_distintos": [], "nome": None, "nomes": [], "autores": ["Ana"], "inicio": agora,
            "fim": agora, "qtd_arquivos": 0, "file_ids": [],
            "mensagens": [{"id": 1, "autor": "Ana", "data": None,
                           "texto": f"= JOANA FICTICIA\n= CPF: {cpf}\n=SENHA: Abc12345x\n"
                                    "Acesso ao gov.br (CPF e senha): Outra#Senha9"}]}
    texto = tg.serializar_lote(lote)["mensagens"][0]["texto"]
    assert "Abc12345x" not in texto and "Outra#Senha9" not in texto
    assert texto.count("[senha removida]") == 2
    assert "JOANA FICTICIA" in texto and cpf in texto, "a triagem precisa do nome/CPF no estado"


# ---------------------------------------------------------------- caminhos (contrato)
def _base_dir_com(env_extra):
    env = {k: v for k, v in os.environ.items() if k not in ("TRIAGEM_DIR",)}
    env.update(env_extra)
    r = subprocess.run([sys.executable, "-c", "import triagem_grupo as t; print(t.BASE_DIR)"],
                       cwd=RAIZ, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_base_dir_segue_caminhos_base_e_aceita_triagem_dir_como_alias(tmp_path):
    base = str(tmp_path / "base")
    assert _base_dir_com({"TRIAGEM_BASE": base}) == base
    alias = str(tmp_path / "alias")
    assert _base_dir_com({"TRIAGEM_BASE": base, "TRIAGEM_DIR": alias}) == alias
