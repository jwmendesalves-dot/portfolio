# -*- coding: utf-8 -*-
"""Frente C2a (onda 3) — F1-01: cursor por chat, FIRST_ID, lacuna, cursor só após gravar.

Sem rede: `bitrix_cliente.chamar` vira uma FONTE FALSA que implementa a semântica
documentada de im.dialog.messages.get (FIRST_ID = ids maiores, ascendente; LAST_ID =
ids menores, descendente). A leitura descendente só alcança as últimas `janela`
mensagens (modela o teto de páginas que fazia a janela "andar"). CPFs sintéticos.
"""
import argparse
import datetime as dt
import json

import pytest

import cpf_util
import estado_store
import lotes_regras
import triagem_grupo as tg

DIALOG = "chat1"
# test_circuit_breaker_integracao troca estas funções no módulo SEM monkeypatch (vaza
# para os testes seguintes): guardamos as reais na coleta e restauramos no fixture.
_REAIS = {n: getattr(tg, n) for n in ("buscar_mensagens", "buscar_mensagens_api",
                                      "ler_pagina_mensagens", "ARQ_BREAKER")}
CPFS = [cpf_util.gerar_para_teste(s) for s in (11, 22, 33)]


class FonteFalsa:
    """Grupo do Bitrix em memória. `visiveis` = quantas mensagens já chegaram."""

    def __init__(self, mensagens, janela=10, pagina=50, por_first=None):
        self.todas = list(mensagens)
        self.visiveis = len(self.todas)
        self.janela = janela
        self.pagina = pagina
        self.por_first = por_first or pagina      # o Bitrix pode devolver > LIMIT
        self.chamadas = []
        self.falhar_na = None                     # nº da chamada que levanta

    def _chegaram(self):
        return self.todas[:self.visiveis]

    def chamar(self, metodo, params=None, **k):
        params = dict(params or {})
        self.chamadas.append(params)
        if self.falhar_na is not None and len(self.chamadas) == self.falhar_na:
            raise RuntimeError("falha de rede simulada no meio da paginação")
        assert metodo == "im.dialog.messages.get"
        msgs = self._chegaram()
        if "FIRST_ID" in params:
            fid = int(params["FIRST_ID"])
            if fid not in {m["id"] for m in self.todas}:
                out = []                          # doc: id inexistente -> []
            else:
                out = [m for m in msgs if m["id"] > fid][:self.por_first]
        else:
            alcancaveis = msgs[-self.janela:]     # teto de páginas: só a cauda
            lid = int(params.get("LAST_ID") or 9999999999)
            out = [m for m in alcancaveis if m["id"] < lid][-self.pagina:]
            out = list(reversed(out))
        return {"result": {"messages": out, "users": [{"id": 1, "name": "Atendente"}]}}

    def chamadas_first_id(self):
        return [c for c in self.chamadas if "FIRST_ID" in c]


def montar_kits(base=None, n_kits=3):
    """3 kits no padrão do grupo (delimitador): conversa, 2 fotos, cabeçalho, traço."""
    base = base or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=6 * n_kits + 1))
    seq = []

    def add(autor, texto="", files=None):
        i = len(seq) + 1
        seq.append({"id": 1000 + i, "author_id": 1,
                    "date": (base + dt.timedelta(minutes=i)).isoformat(),
                    "text": f"[B]{autor}:[/B] {texto}", "params": {"FILE_ID": files or []}})

    nomes = ["MARIA FICTICIA SOUZA", "JOANA FICTICIA LIMA", "ANA FICTICIA ROCHA"]
    for k in range(n_kits):
        add("Atendente A", "bom dia")
        add("Atendente B", "ok")
        add("Atendente A", "", [f"{k}01"])
        add("Atendente A", "", [f"{k}02"])
        add("Atendente A", f"= {nomes[k]}\n= CPF {cpf_util.formatar(CPFS[k])}")
        add("Atendente A", "——")
    return seq


def conf_teste(**extra):
    c = dict(tg.CONFIG_PADRAO)
    c.update({"sinal_traco": ["-", "–", "—", "=", "_"], "modo_traco": "delimitador",
              "traco_vigencia_min": 30, "timeout_lote_min": 120, "janela_min_fallback": 30,
              "fonte_leitura": "webhook", "historico_paginas": 12,
              "barreira_identidade": True, "dialog_id": DIALOG})
    c.update(extra)
    return c


def montar_ambiente(tmp_path, monkeypatch):
    """Corpo do fixture `ambiente` (registrado em conftest.py)."""
    arq = str(tmp_path / "estado.json")
    for nome, real in _REAIS.items():
        monkeypatch.setattr(tg, nome, real)
    monkeypatch.setattr(tg, "ARQ_BREAKER", str(tmp_path / "breaker.json"))
    monkeypatch.setattr(tg, "ARQ_ESTADO", arq)
    monkeypatch.setattr(estado_store, "ARQ_ESTADO", arq)
    monkeypatch.setattr(tg, "DIR_LOTES", str(tmp_path / "lotes"))
    monkeypatch.setattr(tg, "CIRCUIT_ARQUIVO", str(tmp_path / "circuit-download.json"),
                        raising=False)
    monkeypatch.setattr(tg, "ler_chave_bot", lambda: "chave-teste")
    monkeypatch.setattr(tg, "ler_eventos", lambda chave, cursor: ([], cursor))
    monkeypatch.setattr(tg, "ler_bot_config", lambda: {})
    monkeypatch.setattr(tg, "descobrir_webhook", lambda: "https://exemplo.bitrix24.com.br/rest/1/x/")
    monkeypatch.setattr(tg, "download_svc", None)

    def baixar_falso(chave, fid, pasta, indice, lote_id, **k):
        return {"file_id": str(fid), "indice": indice, "baixado": False, "erro": None,
                "caminho": None, "sha256": None, "reaproveitado": True}

    monkeypatch.setattr(tg, "baixar_file_id_api", baixar_falso)
    estado = {"conf": conf_teste(), "arq": arq}
    monkeypatch.setattr(tg, "carregar_config", lambda: dict(estado["conf"]))
    return estado


def usar_fonte(monkeypatch, fonte):
    monkeypatch.setattr(tg.bitrix_cliente, "chamar", fonte.chamar)


def args(**k):
    base = dict(dry_run=False, fonte_webhook=False, dialog_id=DIALOG, paginas=None,
                desde_msg_id=None, janela_min=None, force=False)
    base.update(k)
    return argparse.Namespace(**base)


def ler(arq):
    with open(arq, encoding="utf-8") as fh:
        return json.load(fh)


def cursor_de(arq):
    return int(((ler(arq).get("cursores") or {}).get(DIALOG)) or 0)


# ------------------------------------------------------------------ FIRST_ID
def test_first_id_usado_na_segunda_leitura(ambiente, monkeypatch):
    fonte = FonteFalsa(montar_kits(), janela=100)
    fonte.visiveis = 6
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    assert cursor_de(ambiente["arq"]) == 1006
    fonte.chamadas.clear()
    fonte.visiveis = 12
    tg.ciclo(args())
    firsts = fonte.chamadas_first_id()
    assert firsts and firsts[0]["FIRST_ID"] == 1006 and firsts[0]["LIMIT"] == 50
    # não relê o histórico descendo com LAST_ID
    assert not any("LAST_ID" in c for c in fonte.chamadas)
    assert cursor_de(ambiente["arq"]) == 1012


def test_first_id_nao_para_por_limit_quando_vem_mais_que_50(ambiente, monkeypatch):
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    seq = [{"id": 5000 + i, "author_id": 1, "date": (base + dt.timedelta(seconds=i)).isoformat(),
            "text": f"[B]Atendente A:[/B] conversa {i}", "params": {"FILE_ID": []}}
           for i in range(1, 131)]
    fonte = FonteFalsa(seq, janela=200, por_first=60)   # 60 > LIMIT (não lidas)
    fonte.visiveis = 1
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    fonte.visiveis = 130
    tg.ciclo(args())
    assert cursor_de(ambiente["arq"]) == 5130            # leu as 129 novas, não parou em 60


# ------------------------------------------------------------------ cursor só após gravar
def test_cursor_nao_avanca_quando_a_gravacao_falha(ambiente, monkeypatch):
    fonte = FonteFalsa(montar_kits(), janela=100)
    fonte.visiveis = 6
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    assert cursor_de(ambiente["arq"]) == 1006
    fonte.visiveis = 12
    original = estado_store.atualizar_lotes

    def quebra(*a, **k):
        raise OSError("disco cheio (simulado)")

    monkeypatch.setattr(estado_store, "atualizar_lotes", quebra)
    with pytest.raises(OSError):
        tg.ciclo(args())
    assert cursor_de(ambiente["arq"]) == 1006, "cursor andou sem o lote ter sido gravado"
    monkeypatch.setattr(estado_store, "atualizar_lotes", original)
    fonte.chamadas.clear()
    tg.ciclo(args())                                       # próximo ciclo relê e incorpora
    firsts = fonte.chamadas_first_id()
    assert firsts and firsts[0]["FIRST_ID"] == 1006, "a releitura parte do cursor NÃO avançado"
    est = ler(ambiente["arq"])
    assert cursor_de(ambiente["arq"]) == 1012
    kit2 = [l for l in est["lotes"].values() if "101" in (l.get("file_ids") or [])]
    assert len(kit2) == 1 and kit2[0]["file_ids"] == ["101", "102"]


def test_erro_no_meio_da_paginacao_nao_avanca_cursor(ambiente, monkeypatch):
    fonte = FonteFalsa(montar_kits(), janela=100, por_first=3)
    fonte.visiveis = 6
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    fonte.visiveis = 18
    fonte.chamadas.clear()
    fonte.falhar_na = 2                                    # 2ª página do FIRST_ID
    r = tg.ciclo(args())
    assert any("mensagens:" in e for e in r["erros"])
    assert cursor_de(ambiente["arq"]) == 1006


def test_cursor_apagado_sonda_e_le_descendo_ate_o_cursor(ambiente, monkeypatch):
    seq = montar_kits()
    fonte = FonteFalsa(seq, janela=100)
    fonte.visiveis = 6
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    # a mensagem do cursor (1006) é apagada: FIRST_ID=1006 passa a devolver []
    fonte.todas = [m for m in seq if m["id"] != 1006]
    fonte.visiveis = 11
    tg.ciclo(args())
    assert cursor_de(ambiente["arq"]) == 1012


# ------------------------------------------------------------------ lacuna (API VibeCode)
def test_lacuna_na_api_nao_avanca_cursor_e_registra(ambiente, monkeypatch):
    ambiente["conf"] = conf_teste(fonte_leitura="api")
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
    todas = [{"id": 7000 + i, "authorId": 1, "date": (base + dt.timedelta(seconds=i)).isoformat(),
              "text": f"[B]Atendente A:[/B] conversa {i}", "params": {}} for i in range(1, 301)]
    estado = {"n": 1}
    chamadas = []

    def pagina(chave, dialog_id, last_id=None, first_id=None, limite=50):
        chamadas.append({"last_id": last_id, "first_id": first_id})
        vis = todas[:estado["n"]]
        if last_id:
            vis = [m for m in vis if m["id"] < last_id]
        return {"messages": list(reversed(vis[-50:])), "users": []}

    monkeypatch.setattr(tg, "ler_pagina_mensagens", pagina)
    monkeypatch.setattr(tg, "F1_MAX_PAGINAS_INCREMENTAL", 2)
    monkeypatch.setattr(tg, "MSG_PAGINAS_HISTORICO", 2)
    tg.ciclo(args(paginas=2))
    assert cursor_de(ambiente["arq"]) == 7001
    estado["n"] = 300                                      # 299 novas > 2 páginas x 50
    r = tg.ciclo(args(paginas=2))
    assert r.get("lacuna") is True
    assert any("LACUNA" in e for e in r["erros"])
    assert cursor_de(ambiente["arq"]) == 7001, "com buraco o cursor não pode andar"


def test_api_incremental_para_ao_alcancar_o_cursor(ambiente, monkeypatch):
    ambiente["conf"] = conf_teste(fonte_leitura="api")
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
    todas = [{"id": 8000 + i, "authorId": 1, "date": (base + dt.timedelta(seconds=i)).isoformat(),
              "text": f"[B]Atendente A:[/B] conversa {i}", "params": {}} for i in range(1, 501)]
    estado = {"n": 400}
    chamadas = []

    def pagina(chave, dialog_id, last_id=None, first_id=None, limite=50):
        chamadas.append(last_id)
        vis = todas[:estado["n"]]
        if last_id:
            vis = [m for m in vis if m["id"] < last_id]
        return {"messages": list(reversed(vis[-50:])), "users": []}

    monkeypatch.setattr(tg, "ler_pagina_mensagens", pagina)
    tg.ciclo(args())
    assert cursor_de(ambiente["arq"]) == 8400
    chamadas.clear()
    estado["n"] = 410
    tg.ciclo(args())
    assert len(chamadas) == 1, "com cursor basta a 1ª página (já contém o cursor)"
    assert cursor_de(ambiente["arq"]) == 8410


# ------------------------------------------------------------------ F1-10
def test_timeout_da_varredura_vai_para_revisao_visivel(ambiente, monkeypatch):
    velho = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat()
    lid = "LOTE-20261109-0700-abcd0001"
    estado_store.gravar_atomico({"lotes": {lid: {
        "lote_id": lid, "status": "ABERTO", "aberto": True, "pronto": False,
        "requer_revisao": False, "motivos_revisao": [], "identificacao": "SEM_CPF",
        "qtd_arquivos": 1, "file_ids": ["9"], "inicio": velho, "fim": velho,
        "dialog_id": DIALOG, "mensagens": [{"id": 900, "texto": "foto"}]}}}, ambiente["arq"])
    fonte = FonteFalsa(montar_kits(), janela=100)
    fonte.visiveis = 2
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    reg = ler(ambiente["arq"])["lotes"][lid]
    assert reg["status"] == "FECHADO_POR_TIMEOUT"
    assert reg["pronto"] is False
    assert reg["requer_revisao"] is True, "fechado por timeout e invisível (F1-10)"
    assert any("timeout" in m for m in reg["motivos_revisao"])


def test_regra_de_timeout_unica_kit_completo_fica_pronto():
    agora = dt.datetime.now(dt.timezone.utc)
    completo = {"identificacao": "OK", "qtd_arquivos": 2, "requer_revisao": False}
    p = lotes_regras.fechamento_por_timeout(completo, "fechado por timeout", agora)
    assert p["pronto"] is True and p["status"] == "FECHADO_POR_TIMEOUT"
    incompleto = {"identificacao": "SEM_ARQUIVOS", "qtd_arquivos": 0,
                  "motivos_revisao": ["SEM_ARQUIVOS: lote só com texto"]}
    p = lotes_regras.fechamento_por_timeout(incompleto, "fechado por timeout", agora)
    assert p["pronto"] is False and p["requer_revisao"] is True
    assert p["motivos_revisao"][0].startswith("SEM_ARQUIVOS")  # não apaga os existentes
    assert any("timeout" in m for m in p["motivos_revisao"])
    # agrupar usa a mesma regra: timeout com OK -> pronto; sem OK -> requer_revisao
    conf = conf_teste()
    base = agora - dt.timedelta(hours=5)

    def msg(i, texto, files=None):
        return tg.classificar_mensagem({"id": 2000 + i, "author_id": 1,
                                        "date": (base + dt.timedelta(minutes=i)).isoformat(),
                                        "text": f"[B]Atendente A:[/B] {texto}",
                                        "params": {"FILE_ID": files or []}}, {}, conf)
    lotes = tg.agrupar([msg(1, "——"), msg(2, "", ["77"])], conf, agora=agora)
    to = [l for l in lotes if l.get("status") == "FECHADO_POR_TIMEOUT"]
    assert to and to[0]["requer_revisao"] is True and to[0]["pronto"] is False
