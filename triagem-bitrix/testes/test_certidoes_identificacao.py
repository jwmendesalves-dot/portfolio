# -*- coding: utf-8 -*-
"""CE-02/CE-03/CE-04, S09/CE-05, S13 — certidoes.py: mídia, identificação, chat, segurança.

Reproduz os defeitos antigos:
  CE-02: foto da certidão era tratada como PDF (extensão fixa .pdf) e a IA nunca lia;
  CE-03: "primeiro contato" pelo nome da criança, sem unicidade, 1º negócio qualquer;
  CE-04: filtro pegava certidão de casamento e lia só 50 mensagens;
  S09/CE-05: JS do node em /tmp/_ler_certidao.js (nome fixo) e PII em /tmp sem apagar;
  S13: X-Api-Key mandado para qualquer URL devolvida pela API.
Sem rede: node, Bitrix e VibeCode são falsos.
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import certidoes as cx

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def fake_node(resultado):
    """subprocess.run falso que registra o argv do node e devolve `resultado`."""
    capturado = {"chamadas": []}

    def _run(argv, capture_output=True, text=True, timeout=None):
        capturado["chamadas"].append({"argv": list(argv), "timeout": timeout})
        return types.SimpleNamespace(stdout=json.dumps(resultado), stderr="")
    return capturado, _run


# ------------------------------------------------------------------ CE-02
def test_ce02_foto_de_certidao_nao_e_pdf():
    assert cx.formato_do_arquivo(JPEG) == (".jpg", "image/jpeg")
    assert cx.formato_do_arquivo(PNG) == (".png", "image/png")
    assert cx.formato_do_arquivo(PDF) == (".pdf", "application/pdf")
    assert cx.formato_do_arquivo(b"GIF89a" + b"0" * 32) == (".gif", "image/gif")
    assert cx.formato_do_arquivo(b"II*\x00" + b"0" * 32) == (".tif", "image/tiff")
    # sem assinatura conhecida: NÃO é tratado como PDF
    assert cx.formato_do_arquivo(b"ZZZZqualquer") == ("", "application/octet-stream")


def test_ce02_foto_vai_para_a_ia_com_extensao_e_mime_certos(monkeypatch):
    capturado, _run = fake_node({"cls": {"dados": {"nome_mae": "MAE DA SILVA"}}})
    monkeypatch.setattr(cx.subprocess, "run", _run)

    nome, como = cx.analisar_certidao(JPEG)

    assert nome == "MAE DA SILVA"
    argv = capturado["chamadas"][0]["argv"]
    assert argv[2].endswith(".jpg")            # era .pdf fixo (defeito CE-02)
    assert argv[3] == "image/jpeg"             # mimeType correto para classificarImagem
    assert capturado["chamadas"][0]["timeout"] == 300


def test_ce02_formato_desconhecido_nao_vai_para_a_ia(monkeypatch):
    capturado, _run = fake_node({})
    monkeypatch.setattr(cx.subprocess, "run", _run)
    nome, como = cx.analisar_certidao(b"ZZZZqualquer")
    assert nome is None
    assert "não suportado" in como
    assert capturado["chamadas"] == []


# ------------------------------------------------------------------ S09/CE-05
def test_s09_js_em_arquivo_privado_unico_nao_tmp_fixo(monkeypatch):
    capturado, _run = fake_node({"cls": {"dados": {"nome_mae": "MAE"}}})
    monkeypatch.setattr(cx.subprocess, "run", _run)
    cx.analisar_certidao(PDF)
    cx.analisar_certidao(PDF)
    js1 = capturado["chamadas"][0]["argv"][1]
    js2 = capturado["chamadas"][1]["argv"][1]
    assert js1 != js2                                        # nome ÚNICO por chamada
    assert not js1.startswith("/tmp/") or "/tmp/" not in js1.replace(cx.caminhos.em_base("tmp"), "")
    assert js1 != "/tmp/_ler_certidao.js"                    # o fixo do defeito S09
    assert os.path.dirname(js1) == os.path.join(cx.caminhos.BASE, "tmp")


def test_ce05_temporarios_sao_apagados_no_finally(monkeypatch):
    capturado, _run = fake_node({"cls": {"dados": {"nome_mae": "MAE"}}})
    monkeypatch.setattr(cx.subprocess, "run", _run)
    cx.analisar_certidao(JPEG)
    argv = capturado["chamadas"][0]["argv"]
    assert not os.path.exists(argv[1])          # JS apagado
    assert not os.path.exists(argv[2])          # documento (PII) apagado


def test_ce05_temporarios_apagados_mesmo_quando_o_node_falha(monkeypatch):
    def _run(argv, capture_output=True, text=True, timeout=None):
        raise RuntimeError("node explodiu")
    monkeypatch.setattr(cx.subprocess, "run", _run)
    caminhos_vistos = []
    original = cx.seguranca.arquivo_temp_privado

    def _espiar(*a, **kw):
        c = original(*a, **kw)
        caminhos_vistos.append(c)
        return c
    monkeypatch.setattr(cx.seguranca, "arquivo_temp_privado", _espiar)

    with pytest.raises(RuntimeError):
        cx.analisar_certidao(JPEG)
    assert caminhos_vistos and all(not os.path.exists(c) for c in caminhos_vistos)


# ------------------------------------------------------------------ CE-03
def test_ce03_ia_nunca_usa_o_nome_da_crianca(monkeypatch):
    # IA devolve o nome do REGISTRADO (criança) em `nome`: ele NUNCA vira âncora
    capturado, _run = fake_node({"cls": {"dados": {"nome": "CRIANCA SILVA",
                                                   "nome_completo": "CRIANCA SILVA"}}})
    monkeypatch.setattr(cx.subprocess, "run", _run)
    nome, como = cx.analisar_certidao(PDF)
    assert nome is None
    assert "mãe" in como

    # com o nome da MÃE, aí sim
    capturado, _run = fake_node({"cls": {"dados": {"nome": "CRIANCA SILVA",
                                                   "nome_mae": "GENITORA SILVA"}}})
    monkeypatch.setattr(cx.subprocess, "run", _run)
    nome, _ = cx.analisar_certidao(PDF)
    assert nome == "GENITORA SILVA"


class FakeListar:
    def __init__(self, respostas):
        self.respostas = respostas      # metodo -> callable(params) -> list
        self.chamadas = []

    def __call__(self, metodo, params=None):
        self.chamadas.append((metodo, params))
        fn = self.respostas.get(metodo)
        return fn(params) if fn else []


def test_ce03_contato_ambiguo_nao_escolhe_o_primeiro(monkeypatch):
    fake = FakeListar({"crm.contact.list": lambda p: [
        {"ID": 1, "NAME": "ANA", "LAST_NAME": "SILVA"},
        {"ID": 2, "NAME": "ANA", "LAST_NAME": "SILVA"}]})
    monkeypatch.setattr(cx, "bx_listar", fake)
    contato, motivo = cx.identificar_negocio(None, "ANA SILVA")
    assert contato is None
    assert "2 contatos" in motivo


def test_ce03_busca_paginada_casa_nome_completo(monkeypatch):
    """O %NAME do Bitrix faz LIKE no primeiro nome; só o casamento EXATO
    NAME+LAST_NAME vale — homônimo parcial não é o cliente."""
    fake = FakeListar({"crm.contact.list": lambda p: [
        {"ID": 1, "NAME": "ANA", "LAST_NAME": "SOUZA"},          # parcial (LIKE)
        {"ID": 2, "NAME": "ANA", "LAST_NAME": "SILVA"}]})        # exato
    monkeypatch.setattr(cx, "bx_listar", fake)
    assert [c["ID"] for c in cx.contatos_por_nome("ANA SILVA")] == [2]
    # a listagem é pedida com paginação estável (bitrix_cliente.listar)
    metodo, params = fake.chamadas[0]
    assert metodo == "crm.contact.list"
    assert "%NAME" in params["filter"]


def test_ce03_contato_unico_mas_telefone_nao_confere(monkeypatch):
    fake = FakeListar({"crm.contact.list": lambda p: [
        {"ID": 2, "NAME": "ANA", "LAST_NAME": "SILVA",
         "PHONE": [{"VALUE": "65999990000"}]}]})
    monkeypatch.setattr(cx, "bx_listar", fake)
    contato, motivo = cx.identificar_negocio(None, "ANA SILVA", telefones=["65988887777"])
    assert contato is None
    assert "telefone" in motivo


def test_ce03_negocio_aberto_mais_recente(monkeypatch):
    visto = {}
    fake = FakeListar({"crm.deal.list": lambda p: visto.update(p) or [
        {"ID": 1, "DATE_CREATE": "2026-01-01"},
        {"ID": 2, "DATE_CREATE": "2026-11-01"}]})
    monkeypatch.setattr(cx, "bx_listar", fake)
    deal = cx.achar_deal_por_contato(3)
    assert deal["ID"] == 2
    assert visto["filter"]["CLOSED"] == "N"        # nunca negócio fechado
    assert visto["filter"]["CONTACT_ID"] == "3"


def test_identificar_cpf_com_dv_invalido_nao_vira_ancora():
    ida = cx.identificar("CPF 291.417.776-39")     # DV trocado
    assert ida["cpf"] is None                      # cai para o caminho de nome/IA
    ida = cx.identificar("CPF 291.417.776-38")
    assert ida["cpf"] == "29141777638"


def test_identificar_prefere_mae_responsavel():
    ida = cx.identificar("Mãe: Maria Aparecida da Silva")
    assert ida["nome"] == "Maria Aparecida da Silva"
    ida = cx.identificar("Telefone 65 99999-0000 para contato")
    assert ida["telefones"] == ["65999990000"]


# ------------------------------------------------------------------ CE-04
def test_ce04_casamento_e_obito_nao_entram():
    assert not cx.e_certidao_de_nascimento("Certidão de casamento Bianca")
    assert not cx.e_certidao_de_nascimento("certidão de óbito do João")
    assert cx.e_certidao_de_nascimento("Certidão de nascimento da Ana")
    assert cx.e_certidao_de_nascimento("segue a certidão")
    assert cx.e_certidao_de_nascimento("registro civil da Ana")
    assert not cx.e_certidao_de_nascimento("comprovante de residência")


def test_ce04_historico_pagina_alem_de_50(monkeypatch):
    """A API devolve no máximo 50 por página; a leitura antiga lia SÓ a 1ª página."""
    paginas = {
        None: [{"id": i, "text": "x", "params": {}} for i in range(101, 151)],
        101: [{"id": i, "text": "x", "params": {}} for i in range(51, 101)],
        51: [{"id": i, "text": "x", "params": {}} for i in range(41, 51)],
    }
    vistos = []

    def _vibe(path, metodo="GET", corpo=None, params=None, binario=False, tentativas=4):
        vistos.append(dict(params or {}))
        return {"data": {"messages": paginas[(params or {}).get("lastId")]}}
    monkeypatch.setattr(cx, "vibe", _vibe)

    msgs = cx.ler_mensagens_chat("chat360263", max_paginas=12)

    assert len(msgs) == 110                            # > 50: o histórico foi descido
    assert [v.get("lastId") for v in vistos] == [None, 101, 51]


def test_ce04_grupo_descarta_certidao_de_casamento(monkeypatch):
    def _vibe(path, metodo="GET", corpo=None, params=None, binario=False, tentativas=4):
        return {"data": {"messages": [
            {"id": 1, "authorId": 3, "text": "Certidão de casamento Bianca",
             "params": {"fileId": "f1"}, "date": "2026-11-09"},
            {"id": 2, "authorId": 3, "text": "Certidão de nascimento da Ana",
             "params": {"fileId": "f2"}, "date": "2026-11-09"},
        ]}}
    monkeypatch.setattr(cx, "vibe", _vibe)
    novas = cx.ler_certidoes_novas({"mensagens": {}, "msgs_processadas": []}, chat=cx.CHAT_GRUPO)
    assert [n["id"] for n in novas] == [2]


# -------------------------------------------------------------------- S13
def test_s13_chave_soh_para_o_host_vibecode(monkeypatch):
    monkeypatch.setattr(cx, "chave_vibecode", lambda: "CHAVE-TESTE")
    pedidos = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"conteudo"

    def _urlopen(req, timeout=None):
        pedidos.append(req)
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    assert cx.baixar_url_externa("https://cdn.terceiro.com/arq.pdf") == b"conteudo"
    assert cx.baixar_url_externa("https://vibecode.bitrix24.com/v1/files/1/download") == b"conteudo"

    def _tem_chave(req):
        return any(k.lower() == "x-api-key" for k in req.headers)
    assert not _tem_chave(pedidos[0])      # URL de terceiro NUNCA recebe a chave (S13)
    assert _tem_chave(pedidos[1])          # host VibeCode autorizado recebe


def test_s13_baixar_do_chat_nao_manda_chave_para_url_estranha(monkeypatch):
    monkeypatch.setattr(cx, "chave_vibecode", lambda: "CHAVE-TESTE")
    pedidos = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"conteudo"

    def _urlopen(req, timeout=None):
        pedidos.append(req)
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    monkeypatch.setattr(cx, "vibe", lambda *a, **kw: {"data": {"downloadUrl":
                                                               "https://evil.example.com/x"}})

    assert cx.baixar_do_chat("f1") == b"conteudo"
    assert pedidos and all(not any(k.lower() == "x-api-key" for k in r.headers)
                           for r in pedidos)
