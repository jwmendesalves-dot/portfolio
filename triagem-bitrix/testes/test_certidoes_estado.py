# -*- coding: utf-8 -*-
"""CE-01 — certidoes.py: máquina de estados por mensagem, idempotência e retry.

Reproduz os defeitos antigos:
  (a) falha marcava a mensagem como processada e ela SUMIA sem tarefa nem retry;
  (b) queda no meio da rodada fazia a próxima rodada CRIAR TAREFA DUPLICADA;
  (c) "tarefa criada" era marcada sem confirmar a tarefa.
Sem rede: Bitrix (bitrix_cliente) e VibeCode (vibe) são falsos.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import certidoes as cx
import cpf_util
import rede_bitrix
from rede_bitrix import FalhaBitrix

CPF = cpf_util.gerar_para_teste(1)
NOME = "ANA MARIA DA SILVA"


class FakeBitrix:
    """bitrix_cliente falso: registra chamar/listar e devolve respostas scriptadas."""

    def __init__(self):
        self.chamadas = []      # (metodo, params, escrita)
        self.listagens = []     # (metodo, params)
        self.on_chamar = {}     # metodo -> callable(params) -> dict (ou exceção)
        self.on_listar = {}     # metodo -> callable(params) -> list

    def chamar(self, metodo, params=None, *, escrita=False, **kw):
        self.chamadas.append((metodo, params, escrita))
        fn = self.on_chamar.get(metodo)
        if fn is None:
            return {"result": {}}
        return fn(params)

    def listar(self, metodo, params=None, **kw):
        self.listagens.append((metodo, params))
        fn = self.on_listar.get(metodo)
        return fn(params) if fn else []

    def metodos(self):
        return [m for m, _p, _e in self.chamadas]


def fake_vibe(rota_por_nome):
    def _vibe(path, metodo="GET", corpo=None, params=None, binario=False, tentativas=4):
        for trecho, resposta in rota_por_nome.items():
            if trecho in path:
                return resposta(path, params) if callable(resposta) else resposta
        return {"_erro": "rota não prevista: " + path}
    return _vibe


def preparar(monkeypatch, tmp_path, fake=None):
    """Certidoes apontando p/ estado temporário + Bitrix falso. Devolve (fake, caminho)."""
    fake = fake or FakeBitrix()
    monkeypatch.setattr(cx, "bx_chamar", fake.chamar)
    monkeypatch.setattr(cx, "bx_listar", fake.listar)
    monkeypatch.setattr(cx, "vibe", fake_vibe({
        "/folder": {"data": {"id": 5}},
        "/files/upload": {"data": {"id": 99}},
    }))
    caminho = str(tmp_path / "estado-certidoes.json")
    monkeypatch.setattr(cx, "ESTADO", caminho)
    return fake, caminho


def entrada_de_teste(msg_id=101):
    return {"id": msg_id, "autor": 7, "texto": f"Nome: {NOME}", "fileId": None,
            "bytes": b"%PDF-1.4 fake", "chat": cx.CHAT_PROJETO, "data": "2026-11-09"}


def crm_de_contato_unico(fake):
    fake.on_listar["crm.contact.list"] = lambda p: [
        {"ID": 3, "NAME": "ANA MARIA", "LAST_NAME": "DA SILVA", "PHONE": []}]
    fake.on_listar["crm.deal.list"] = lambda p: [
        {"ID": 77, "TITLE": "PREVI", "STAGE_ID": "NEW", "DATE_CREATE": "2026-11-01"}]
    fake.on_listar["tasks.task.list"] = lambda p: []


def erro(classe, **kw):
    base = dict(repetir=True, espera_s=0.0,
                permanente=(classe in rede_bitrix.PERMANENTES), detalhe="falha de teste")
    base.update(kw)
    return rede_bitrix.Erro(classe=classe, **base)


# ------------------------------------------------------------------ CE-01
def test_ce01_falha_transitoria_NAO_marca_como_processada(tmp_path, monkeypatch):
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    fake.on_chamar["tasks.task.add"] = lambda p: (_ for _ in ()).throw(
        FalhaBitrix(erro(rede_bitrix.Classe.SERVIDOR)))

    resumo = cx.processar([entrada_de_teste()], enviar=True)

    assert resumo["falha_transitoria"] == 1
    estado = json.load(open(caminho, encoding="utf-8"))
    reg = estado["mensagens"]["chat360263:101"]
    assert reg["estado"] == "falha_transitoria"
    assert reg["tentativas"] == 1
    assert reg["proxima_em"] > time.time()
    # defeito antigo: entrava em msgs_processadas e sumia sem tarefa
    assert estado["msgs_processadas"] == []
    assert estado["tarefas"] == []


def test_ce01_erro_permanente_vira_falha_permanente_com_aviso(tmp_path, monkeypatch):
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    fake.on_chamar["tasks.task.add"] = lambda p: (_ for _ in ()).throw(
        FalhaBitrix(erro(rede_bitrix.Classe.ACESSO, repetir=False, permanente=True)))

    resumo = cx.processar([entrada_de_teste()], enviar=True)

    assert resumo["falha_permanente"] == 1
    reg = json.load(open(caminho, encoding="utf-8"))["mensagens"]["chat360263:101"]
    assert reg["estado"] == "falha_permanente"
    assert not reg.get("proxima_em")
    assert "im.message.add" in fake.metodos()          # motivo registrado p/ humano


def test_ce01_idempotencia_nao_duplica_tarefa(tmp_path, monkeypatch):
    """Tarefa já criada (marcador [certidao:chat:id]) → a rodada seguinte NÃO recria."""
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    fake.on_listar["tasks.task.list"] = lambda p: [
        {"ID": 555, "TITLE": "Certidão de nascimento — ANA MARIA DA SILVA",
         "DESCRIPTION": "Qualquer coisa\n[certidao:chat360263:101]"}]

    resumo = cx.processar([entrada_de_teste()], enviar=True)

    assert resumo["ok"] == 1
    assert "tasks.task.add" not in fake.metodos()      # NENHUMA tarefa nova
    reg = json.load(open(caminho, encoding="utf-8"))["mensagens"]["chat360263:101"]
    assert reg["estado"] == "tarefa_criada"
    assert str(reg["tarefa"]) == "555"


def test_ce01_somente_marca_concluido_depois_de_confirmar(tmp_path, monkeypatch):
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    fake.on_chamar["tasks.task.add"] = lambda p: {"result": {"task": {"id": 777}}}
    fake.on_chamar["tasks.task.get"] = lambda p: {"result": {}}   # não confirmou

    resumo = cx.processar([entrada_de_teste()], enviar=True)

    assert resumo["falha_transitoria"] == 1
    estado = json.load(open(caminho, encoding="utf-8"))
    reg = estado["mensagens"]["chat360263:101"]
    assert reg["estado"] != "tarefa_criada"            # não confirmada => não concluída
    assert estado["msgs_processadas"] == []


def test_ce01_queda_no_meio_da_rodada_nao_duplica_tarefa(tmp_path, monkeypatch):
    """A queda interrompe a rodada: o que já foi feito fica gravado e a próxima rodada
    não cria tarefa duplicada (defeito antigo: estado só era gravado no fim)."""
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    fake.on_chamar["tasks.task.add"] = lambda p: {"result": {"task": {"id": 42}}}
    fake.on_chamar["tasks.task.get"] = lambda p: {"result": {"task": {"id": 42}}}

    e1, e2 = entrada_de_teste(101), entrada_de_teste(102)
    original = cx._processar_uma

    def _com_queda(e, chat_e, enviar, pastas):
        if e["id"] == 102:
            raise KeyboardInterrupt("queda no meio da rodada")
        return original(e, chat_e, enviar, pastas)

    monkeypatch.setattr(cx, "_processar_uma", _com_queda)
    with pytest.raises(KeyboardInterrupt):
        cx.processar([e1, e2], enviar=True)

    # a mensagem 1 JÁ está concluída em disco, mesmo com a queda
    estado = json.load(open(caminho, encoding="utf-8"))
    assert estado["mensagens"]["chat360263:101"]["estado"] == "tarefa_criada"

    # próxima rodada (sem queda): 101 não é reprocessada → sem tarefa duplicada
    monkeypatch.setattr(cx, "_processar_uma", original)
    cx.processar([e1, e2], enviar=True)
    criadas = [p for m, p, _e in fake.chamadas if m == "tasks.task.add"]
    # a tarefa da mensagem 101 foi criada EXATAMENTE uma vez (nenhuma duplicata)
    assert sum(1 for p in criadas if "[certidao:chat360263:101]" in p["fields"]["DESCRIPTION"]) == 1


def test_ce01_retry_agendado_so_depois_do_prazo(tmp_path, monkeypatch):
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    estado = {"mensagens": {"chat360263:101": {
        "msg": 101, "chat": cx.CHAT_PROJETO, "estado": "falha_transitoria",
        "tentativas": 1, "proxima_em": time.time() + 3600}}, "msgs_processadas": []}
    cx.gravar_estado(estado)

    resumo = cx.processar([entrada_de_teste()], enviar=True)
    assert resumo["pulado"] == 1                        # ainda dentro da janela
    assert "tasks.task.add" not in fake.metodos()

    estado["mensagens"]["chat360263:101"]["proxima_em"] = time.time() - 1
    cx.gravar_estado(estado)
    fake.on_chamar["tasks.task.add"] = lambda p: {"result": {"task": {"id": 1}}}
    fake.on_chamar["tasks.task.get"] = lambda p: {"result": {"task": {"id": 1}}}
    resumo = cx.processar([entrada_de_teste()], enviar=True)
    assert resumo["ok"] == 1                            # janela vencida → reprocessa


def test_ce01_retry_esgotado_vira_permanente(tmp_path, monkeypatch):
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    fake.on_chamar["tasks.task.add"] = lambda p: (_ for _ in ()).throw(
        FalhaBitrix(erro(rede_bitrix.Classe.REDE)))
    estado = {"mensagens": {"chat360263:101": {
        "msg": 101, "chat": cx.CHAT_PROJETO, "estado": "falha_transitoria",
        "tentativas": cx.MAX_TENTATIVAS_MSG - 1, "proxima_em": time.time() - 1}},
        "msgs_processadas": []}
    cx.gravar_estado(estado)

    resumo = cx.processar([entrada_de_teste()], enviar=True)
    assert resumo["falha_permanente"] == 1
    reg = json.load(open(caminho, encoding="utf-8"))["mensagens"]["chat360263:101"]
    assert reg["estado"] == "falha_permanente"


def test_ce01_sem_negocio_so_fica_marcado_depois_do_aviso(tmp_path, monkeypatch):
    fake, caminho = preparar(monkeypatch, tmp_path)
    fake.on_listar["crm.contact.list"] = lambda p: []   # ninguém no CRM
    fake.on_chamar["im.message.add"] = lambda p: {"result": 0}   # aviso FALHOU

    resumo = cx.processar([entrada_de_teste()], enviar=True)
    assert resumo["falha_transitoria"] == 1             # não confirmado => não marcado
    estado = json.load(open(caminho, encoding="utf-8"))
    assert estado["msgs_processadas"] == []
    assert estado["sem_negocio"] == []

    fake.on_chamar["im.message.add"] = lambda p: {"result": 8}
    estado["mensagens"]["chat360263:101"]["proxima_em"] = time.time() - 1
    cx.gravar_estado(estado)
    resumo = cx.processar([entrada_de_teste()], enviar=True)
    assert resumo["sem_negocio"] == 1
    estado = json.load(open(caminho, encoding="utf-8"))
    assert estado["mensagens"]["chat360263:101"]["estado"] == "sem_negocio"
    assert estado["msgs_processadas"] == [101]


# ------------------------------------------------------------------ S12
def test_s12_aviso_com_e_comercial_chega_inteiro(tmp_path, monkeypatch):
    """O `curl -d k=v` antigo partia o texto em &/= e punha a URL do webhook no argv;
    o corpo JSON do bitrix_cliente entrega a mensagem inteira e sem URL visível."""
    fake, caminho = preparar(monkeypatch, tmp_path)
    avisos = []
    fake.on_chamar["im.message.add"] = lambda p: avisos.append(p) or {"result": 9}
    texto = "Atenção: Maria & Cia = 100% — CPF 291.417.776-38"

    ok = cx.avisar_no_projeto(texto, cx.CHAT_PROJETO)

    assert ok
    assert avisos == [{"DIALOG_ID": cx.CHAT_PROJETO, "MESSAGE": texto}]   # intacto, 1 corpo


def test_s12_fonte_sem_curl_nem_webhook_no_argv():
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "certidoes.py"), encoding="utf-8") as fh:
        fonte = fh.read()
    assert "curl" not in fonte
    assert "subprocess.run([\"curl\"" not in fonte
    assert "/tmp/_ler_certidao.js" not in fonte          # S09


def test_ce01_simular_nao_grava_estado(tmp_path, monkeypatch):
    fake, caminho = preparar(monkeypatch, tmp_path)
    crm_de_contato_unico(fake)
    cx.processar([entrada_de_teste()], enviar=False)
    assert not os.path.exists(caminho)
    assert "tasks.task.add" not in fake.metodos()
