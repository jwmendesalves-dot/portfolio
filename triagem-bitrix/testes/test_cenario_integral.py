# -*- coding: utf-8 -*-
"""Frente G (onda 4) — cenário integral de ponta a ponta, OFFLINE.

Cadeia real: triagem_grupo.ciclo (fonte falsa em bitrix_cliente._post: im.dialog.messages.get)
-> download dentro do ciclo (disk.file.get falso + triagem_grupo._abrir_com_teto falso, bytes
sintéticos; VibeCode sempre 404) -> completar_cpf_por_nome.rodar (CRM falso no mesmo _post)
-> lote_para_fila.main --simular/--enviar (node falso de FilaJobs de test_ponte_rodada).

Tudo em tmp_path (TRIAGEM_BASE / AGENTE_SEPARADOR_BASE). CPFs de cpf_util.gerar_para_teste,
nomes fictícios. Nenhum código de produção é alterado aqui.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import os
import sys
import urllib.error
import urllib.request

import pytest

from conftest import RAIZ

sys.path.insert(0, RAIZ)

import baixar_pendentes as bp  # noqa: E402
import bitrix_cliente as bc  # noqa: E402
import caminhos  # noqa: E402
import completar_cpf_por_nome as cc  # noqa: E402
import cpf_util  # noqa: E402
import download_svc as ds  # noqa: E402
import estado_store  # noqa: E402
import lote_para_fila as lpf  # noqa: E402
import lote_para_job  # noqa: E402
import triagem_grupo as tg  # noqa: E402
from test_fase1_cursor import _REAIS, FonteFalsa, args, conf_teste  # noqa: E402
from fakes_download import Resp  # noqa: E402
from test_ponte_rodada import NODE_FALSO  # noqa: E402

WH = "https://exemplo.bitrix24.com.br/rest/1/tokzfict1c10seg/"
TOKEN = "tokzfict1c10seg"
SENHA = "Fict@Senha2026"
DIALOG = "chat1"

CPF_MARIA = cpf_util.gerar_para_teste(501)
CPF_BEA = cpf_util.gerar_para_teste(502)
CPF_CLARA = cpf_util.gerar_para_teste(503)
CPF_ANA_1 = cpf_util.gerar_para_teste(504)      # homônimos no CRM
CPF_ANA_2 = cpf_util.gerar_para_teste(505)
CPF_INVALIDO = CPF_MARIA[:9] + ("00" if CPF_MARIA[9:] != "00" else "11")
assert not cpf_util.valido(CPF_INVALIDO)
TODOS_CPFS = [CPF_MARIA, CPF_BEA, CPF_CLARA, CPF_ANA_1, CPF_ANA_2]


def _b(fid: str, ext="pdf") -> bytes:
    cab = b"%PDF-1.4\n" if ext == "pdf" else b"\xff\xd8\xff\xe0"
    return cab + f"conteudo sintetico do arquivo {fid}".encode() * 3


# file_id -> (NAME no disk.file.get, bytes)
ARQUIVOS = {
    "101": ("RG.pdf", _b("101")),            # dois anexos com o MESMO nome no mesmo kit
    "102": ("RG.pdf", _b("102")),
    "103": ("audio_cliente.ogg", b"OggS\x00audio sintetico"),   # áudio no meio do kit
    "104": ("comprovante.pdf", _b("104")),
    "201": ("cnh.jpg", _b("201", "jpg")),
    "202": ("cpf.jpg", _b("202", "jpg")),
    "301": ("rg_ana.pdf", _b("301")),
    "302": ("laudo_ana.pdf", _b("302")),
    "401": ("rg_bea.pdf", _b("401")),
    "402": ("laudo_bea.pdf", _b("402")),     # 503 QUERY_LIMIT no 1º ciclo (transitório)
    "501": ("rg_clara.pdf", _b("501")),
    "502": ("laudo_clara.pdf", _b("502")),   # ACCESS_DENIED sempre (permanente)
    # kit reenviado idêntico ao da Maria (file_ids novos, mesmos bytes, sem o áudio)
    "601": ("RG.pdf", _b("101")),
    "602": ("RG.pdf", _b("102")),
    "604": ("comprovante.pdf", _b("104")),
    # documento complementar novo da Maria
    "705": ("extrato_inss.pdf", _b("705")),
    # lote aberto no fim
    "801": ("rg_dora.pdf", _b("801")),
}
SHA = {fid: hashlib.sha256(b).hexdigest() for fid, (_n, b) in ARQUIVOS.items()}
DOCS = {fid for fid, (n, _b2) in ARQUIVOS.items() if not n.endswith(".ogg")}


def montar_conversa(audio_em="K6"):
    """Ordem do grupo (delimitador = traço no fim de cada kit). Devolve (seq, marcos)."""
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=90)
    seq, marcos = [], {}

    def add(texto="", files=None, autor="Atendente A"):
        i = len(seq) + 1
        seq.append({"id": 5000 + i, "author_id": 1,
                    "date": (base + dt.timedelta(minutes=i)).isoformat(),
                    "text": f"[B]{autor}:[/B] {texto}", "params": {"FILE_ID": files or []}})

    def kit(chave, nome, cpf, fids, conversa_no_meio=True, extra=""):
        add("bom dia, segue a documentação")
        for k, fid in enumerate(fids):
            add("", [fid])
            if conversa_no_meio and k == 0:
                add("ok, aguardando o resto", autor="Atendente B")   # conversa entre documentos
        cab = f"= {nome}" + (f"\n= CPF {cpf}" if cpf else "") + extra
        add(cab)
        add("——")
        marcos[chave] = seq[-1]["id"]

    k0 = ["101", "102", "103", "104"] if audio_em == "K0" else ["101", "102", "104"]
    kit("K0", "MARIA FICTICIA SOUZA", cpf_util.formatar(CPF_MARIA), k0,
        extra=f"\n= Senha gov.br: {SENHA}")
    kit("K1", "JOANA FICTICIA LIMA", CPF_INVALIDO[:3] + "." + CPF_INVALIDO[3:6] + "." +
        CPF_INVALIDO[6:9] + "-" + CPF_INVALIDO[9:], ["201", "202"])
    kit("K2", "ANA FICTICIA ROCHA", None, ["301", "302"])
    kit("K3", "BEATRIZ FICTICIA NUNES", cpf_util.formatar(CPF_BEA), ["401", "402"])
    marcos["ciclo1"] = seq[-1]["id"]
    add("pessoal, alguém viu o malote?", autor="Atendente B")          # conversa solta
    kit("K4", "CLARA FICTICIA PRADO", cpf_util.formatar(CPF_CLARA), ["501", "502"])
    kit("K5", "MARIA FICTICIA SOUZA", cpf_util.formatar(CPF_MARIA), ["601", "602", "604"])
    marcos["ciclo2"] = seq[-1]["id"]
    kit("K6", "MARIA FICTICIA SOUZA", cpf_util.formatar(CPF_MARIA),
        ["705", "103"] if audio_em == "K6" else ["705"])         # áudio no meio da conversa do kit
    # lote ainda ABERTO: documento + cabeçalho, sem o traço de fechamento (recente)
    agora = dt.datetime.now(dt.timezone.utc)
    for txt, files in (("segue doc da Dora", None), ("", ["801"]),
                       ("= DORA FICTICIA MELO", None)):
        i = len(seq) + 1
        seq.append({"id": 5000 + i, "author_id": 1,
                    "date": (agora - dt.timedelta(minutes=3) + dt.timedelta(seconds=i)).isoformat(),
                    "text": f"[B]Atendente A:[/B] {txt}", "params": {"FILE_ID": files or []}})
    return seq, marcos


class Portal:
    """Bitrix + URLs assinadas + VibeCode, tudo em memória."""

    def __init__(self, seq):
        self.fonte = FonteFalsa(seq, janela=200)
        self.falhas_disk = {"402": ["QUERY_LIMIT"], "502": ["ACCESS_DENIED"] * 999}
        self.crm = [
            {"ID": 10, "NAME": "ANA", "SECOND_NAME": "FICTICIA", "LAST_NAME": "ROCHA", "CPF": CPF_ANA_1},
            {"ID": 11, "NAME": "ANA", "SECOND_NAME": "FICTICIA", "LAST_NAME": "ROCHA", "CPF": CPF_ANA_2},
            {"ID": 12, "NAME": "JOANA", "SECOND_NAME": "FICTICIA", "LAST_NAME": "LIMA",
             "CPF": cpf_util.gerar_para_teste(506)},
        ]
        self.log = []

    def post(self, url, params, timeout):
        metodo = url.rsplit("/", 1)[1].replace(".json", "")
        self.log.append((metodo, params.get("id")))
        if metodo == "im.dialog.messages.get":
            return 200, json.dumps(self.fonte.chamar(metodo, params))
        if metodo == "disk.file.get":
            fid = str(params["id"])
            fila = self.falhas_disk.get(fid) or []
            if fila:
                cod = fila.pop(0)
                st = 503 if cod == "QUERY_LIMIT" else 403
                cod = "QUERY_LIMIT_EXCEEDED" if cod == "QUERY_LIMIT" else cod
                return st, json.dumps({"error": cod, "error_description": "simulado"})
            nome, corpo = ARQUIVOS[fid]
            return 200, json.dumps({"result": {"ID": fid, "NAME": nome, "SIZE": len(corpo),
                                               "DOWNLOAD_URL": f"https://exemplo.bitrix24.com.br/dl/{fid}"}})
        if metodo == "crm.contact.list":
            f = params.get("filter") or {}
            itens = []
            for c in sorted(self.crm, key=lambda c: c["ID"]):
                if ">ID" in f and c["ID"] <= int(f[">ID"]):
                    continue
                completo = " ".join(x for x in (c["NAME"], c["SECOND_NAME"], c["LAST_NAME"]) if x)
                if "%NAME" in f and f["%NAME"].upper() not in (c["NAME"].upper(), completo.upper()):
                    if f["%NAME"].upper() not in c["NAME"].upper():
                        continue
                if "%LAST_NAME" in f and f["%LAST_NAME"].upper() not in c["LAST_NAME"].upper():
                    continue
                itens.append({"ID": str(c["ID"]), "NAME": c["NAME"], "SECOND_NAME": c["SECOND_NAME"],
                              "LAST_NAME": c["LAST_NAME"], cc.CAMPO_CPF: c["CPF"]})
            return 200, json.dumps({"result": itens})
        if metodo == "crm.duplicate.findbycomm":
            return 200, json.dumps({"result": []})
        return 404, json.dumps({"error": "ERROR_METHOD_NOT_FOUND"})

    def abrir(self, req, timeout):            # substitui tg._abrir_com_teto
        url = req.full_url
        self.log.append(("GET", url))
        if "/dl/" in url:
            fid = url.rsplit("/", 1)[1]
            return io.BytesIO(ARQUIVOS[fid][1])
        raise urllib.error.HTTPError(url, 404, "nao", {}, io.BytesIO(b'{"error":{"code":"NOT_FOUND"}}'))

    def urlopen(self, req, timeout=None, *a, **k):   # substitui urllib.request.urlopen (download_svc)
        if isinstance(req, urllib.request.Request) and req.data is not None and "/rest/" in req.full_url:
            st, txt = self.post(req.full_url, json.loads(req.data.decode() or "{}"), timeout)
            if st >= 400:
                raise urllib.error.HTTPError(req.full_url, st, "erro", {}, io.BytesIO(txt.encode()))
            return Resp(txt.encode(), st)
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        self.log.append(("GET", url))
        if "/dl/" in url:
            corpo = ARQUIVOS[url.rsplit("/", 1)[1]][1]
            return Resp(corpo, 200, {"Content-Length": str(len(corpo))})
        raise urllib.error.HTTPError(url, 404, "nao", {}, io.BytesIO(b'{"error":{"code":"NOT_FOUND"}}'))

    def gets(self, fid):
        return sum(1 for m, u in self.log if m == "GET" and str(u).endswith(f"/dl/{fid}"))


class BytesResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cena(tmp_path, monkeypatch):
    base, sep = tmp_path / "base", tmp_path / "sep"
    (base / "lotes").mkdir(parents=True)
    (sep / "data" / "staging").mkdir(parents=True)
    (sep / "src").mkdir(parents=True)
    (sep / "data" / "fila-jobs.json").write_text("[]")
    arq = str(base / "estado.json")
    monkeypatch.setenv("TRIAGEM_BASE", str(base))
    monkeypatch.setenv("AGENTE_SEPARADOR_BASE", str(sep))
    monkeypatch.setenv("ESTADO_JSON", arq)
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", WH)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "")
    monkeypatch.setenv("BITRIX_RATELIMIT_FILE", str(tmp_path / "rl"))
    monkeypatch.setenv("VIBECODE_RATELIMIT_FILE", str(tmp_path / "vrl"))
    monkeypatch.setenv("BITRIX_RATE", "100")
    monkeypatch.setenv("BITRIX_RAJADA", "1000")
    monkeypatch.setenv("DOWNLOAD_FILA_JSON", str(base / "download_fila.json"))
    monkeypatch.delenv("TRIAGEM_FILA_LOCK_FD", raising=False)
    monkeypatch.setattr(caminhos, "BASE", str(base))
    monkeypatch.setattr(caminhos, "SEPARADOR", str(sep))
    monkeypatch.setattr(caminhos, "SEGREDOS_DIR", str(tmp_path / "segredos"))
    monkeypatch.setattr(bc, "_webhook_cache", None)
    monkeypatch.setattr(bc.time, "sleep", lambda *a, **k: None)
    # triagem
    for nome, real in _REAIS.items():
        monkeypatch.setattr(tg, nome, real)
    monkeypatch.setattr(tg, "ARQ_BREAKER", str(base / "breaker.json"))
    monkeypatch.setattr(tg, "ARQ_ESTADO", arq)
    monkeypatch.setattr(estado_store, "ARQ_ESTADO", arq)
    monkeypatch.setattr(tg, "DIR_LOTES", str(base / "lotes"))
    monkeypatch.setattr(tg, "CIRCUIT_ARQUIVO", str(base / "circuit-download.json"))
    monkeypatch.setitem(tg._MIGRACAO_CIRCUITO, "feita", False)
    monkeypatch.setattr(tg, "_RATE_MIN_SEG", 0)
    monkeypatch.setattr(tg, "ler_chave_bot", lambda: "chave-vibe-ficticia")
    monkeypatch.setattr(tg, "ler_eventos", lambda chave, cursor: ([], cursor))
    monkeypatch.setattr(tg, "ler_bot_config", lambda: {})
    monkeypatch.setattr(tg, "descobrir_webhook", lambda: WH)
    monkeypatch.setattr(tg, "download_svc", None)
    monkeypatch.setattr(tg, "carregar_config", lambda: conf_teste(dialog_id=DIALOG))
    retry = []
    monkeypatch.setattr(tg, "_enfileirar_retry", lambda fid, destino, erro, **kw: retry.append(fid))
    # ponte (constantes calculadas no import: apontam para o tmp)
    node = tmp_path / "node"
    node.write_text(NODE_FALSO.format(py=sys.executable))
    node.chmod(0o755)
    for nome, val in (("ARQ_ESTADO", arq), ("CONTROLE", str(base / "fila-enviados.json")),
                      ("BACKUPS", str(base / "backups")), ("TRAVA_RODADA", str(base / "fila")),
                      ("STAGING", str(sep / "data" / "staging")),
                      ("FILA", str(sep / "data" / "fila-jobs.json")),
                      ("ESPELHO", str(sep / "data" / "jobs.json")),
                      ("FILA_JS", str(sep / "src" / "fila-jobs.js")), ("NODE", str(node))):
        monkeypatch.setattr(lpf, nome, val)
    monkeypatch.setattr(lote_para_job, "codigo_pasta_do_cliente", lambda cpf: "")

    # áudio NO KIT PRINCIPAL (K0): com G-01/G-02 corrigidos o kit com áudio tem de ir à fila
    seq, marcos = montar_conversa(audio_em="K0")
    portal = Portal(seq)
    monkeypatch.setattr(bc, "_post", portal.post)
    monkeypatch.setattr(tg, "_abrir_com_teto", lambda req, timeout: BytesResp(portal.abrir(req, timeout).getvalue()))
    monkeypatch.setattr(urllib.request, "urlopen", portal.urlopen)
    monkeypatch.setattr(ds.time, "sleep", lambda *a, **k: None)

    class C:
        pass

    c = C()
    c.base, c.sep, c.arq, c.seq, c.marcos, c.portal, c.retry = base, sep, arq, seq, marcos, portal, retry

    def ver_ate(chave):
        ultimo = c.marcos[chave] if chave in c.marcos else c.seq[-1]["id"]
        c.portal.fonte.visiveis = sum(1 for m in c.seq if m["id"] <= ultimo)

    def estado():
        return estado_store.ler_estado(arq)

    def vivos():
        return {lid: r for lid, r in (estado().get("lotes") or {}).items()
                if str(r.get("status") or "").upper() != "IGNORADO" and r.get("file_ids")}

    def por_fid(fid):
        for lid, r in vivos().items():
            if fid in [str(x) for x in r.get("file_ids") or []]:
                return lid, r
        raise AssertionError(f"file_id {fid} sumiu de todos os lotes")

    def fila():
        return json.loads((sep / "data" / "fila-jobs.json").read_text())

    def tempo_passou():
        """Disjuntores e backoff da fila vencidos entre ciclos (o teste não espera minutos reais)."""
        for n in os.listdir(base):
            if n.startswith("circuit-") and n.endswith(".json"):
                os.remove(base / n)
        if (base / "download_fila.json").exists():
            fila_dl = ds.Fila(str(base / "download_fila.json"))
            for it in list(fila_dl.itens):
                if it.get("estado") == ds.PENDENTE:
                    fila_dl.atualizar_item(it["file_id"], {"proxima_em": 0})

    def baixar():
        """Passo do pipeline de 10 min: baixar_pendentes.py (download real é aqui — ver relatório)."""
        return bp.main(["--estado", arq, "--fila", str(base / "download_fila.json")])

    c.ver_ate, c.estado, c.vivos, c.por_fid, c.fila, c.tempo_passou, c.baixar = (
        ver_ate, estado, vivos, por_fid, fila, tempo_passou, baixar)
    return c


def _rodar_ciclo():
    return tg.ciclo(args(dialog_id=DIALOG))


def _ponte(argv, capsys):
    rc = lpf.main(argv)
    return rc, capsys.readouterr().out


def _arquivo(reg, fid):
    return next(a for a in reg["arquivos"] if str(a.get("file_id")) == fid)


def _conferir_documentos(c, fids):
    """Nenhum documento perdido nem trocado: sha256 do estado == do disco == do original."""
    for fid in fids:
        _lid, reg = c.por_fid(fid)
        a = _arquivo(reg, fid)
        assert a.get("baixado") is True, (fid, a.get("erro"))
        assert a["sha256"] == SHA[fid], f"file {fid}: sha256 trocado no estado"
        with open(a["caminho"], "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == SHA[fid], f"file {fid}: disco trocado"


# =====================================================================================
def test_cenario_integral_tres_ciclos_reinicio_falha_de_gravacao_e_ponte(cena, capsys, caplog, monkeypatch):
    c = cena
    caplog.set_level(logging.DEBUG)

    # ---------------- ciclo 1: K0..K3 visíveis; 402 toma 503 QUERY_LIMIT
    c.ver_ate("ciclo1")
    r1 = _rodar_ciclo()
    assert not [e for e in r1["erros"] if "mensagens" in e], r1["erros"]
    ids1 = set(c.vivos())
    assert len(ids1) == 4, sorted(ids1)
    # G-01: o PRÓPRIO ciclo baixa os kits recentes (antes, só o baixar_pendentes)
    _conferir_documentos(c, ["101", "102", "104", "201", "202", "301", "302", "401"])
    # dois "RG.pdf" no mesmo kit: destinos distintos
    _lid0, k0 = c.por_fid("101")
    assert _arquivo(k0, "101")["caminho"] != _arquivo(k0, "102")["caminho"]
    # áudio dentro do K0: mídia, sem GET da URL e sem sha256
    au0 = _arquivo(k0, "103")
    assert "midia" in (au0.get("tipo"), au0.get("erro_tipo")), au0
    assert au0.get("baixado") is False and not au0.get("sha256") and c.portal.gets("103") == 0
    # transitório (503): não baixado, erro transitório e na fila de retry do ciclo
    _lid3, k3 = c.por_fid("402")
    assert _arquivo(k3, "402")["baixado"] is False
    assert _arquivo(k3, "402").get("erro_tipo") == "transitorio"
    assert "402" in c.retry
    cursor1 = c.estado()["cursores"][DIALOG]
    assert cursor1 == c.marcos["ciclo1"]

    # ponte no meio: Maria (com áudio no kit) vai; Bea (pendente) NÃO; Joana/Ana não
    rc, out = _ponte(["--enviar", "--todos", "--limite", "15"], capsys)
    jobs = {j["id"] for j in c.fila()}
    assert jobs == {f"grupo-{_lid0}"}, (jobs, out)

    # baixar_pendentes (timer de 10 min) completa o transitório
    c.baixar()
    assert set(c.vivos()) == ids1

    # ---------------- ciclo 2: K4 (ACCESS_DENIED) e K5 (reenvio idêntico)
    c.tempo_passou()
    c.ver_ate("ciclo2")
    r2 = _rodar_ciclo()
    assert not [e for e in r2["erros"] if "mensagens" in e], r2["erros"]
    c.baixar()
    ids2 = set(c.vivos())
    assert ids1 <= ids2, "lote do ciclo 1 mudou de id"
    assert len(ids2) == 6, sorted(ids2)
    _conferir_documentos(c, ["401", "402", "501", "601", "602", "604"])   # 402 baixou no retry do ciclo
    _lid4, k4 = c.por_fid("502")
    assert _arquivo(k4, "502")["baixado"] is False
    # G-03: ACCESS_DENIED é permanente também no ciclo (não reentra na fila de retry)
    assert _arquivo(k4, "502").get("erro_tipo") == "permanente", _arquivo(k4, "502")
    assert "502" not in c.retry

    # ---------------- falha de gravação no meio do ciclo 3 (2ª transação: cursores)
    c.tempo_passou()
    c.ver_ate("fim")
    antes = json.loads(open(c.arq).read())
    real = estado_store._gravar_bytes
    chamadas = {"n": 0}

    def gravar_que_falha(dados, caminho):
        chamadas["n"] += 1
        if chamadas["n"] == 2:                      # 1ª = lotes (atualizar_lotes); 2ª = cursores
            raise OSError(28, "No space left on device (simulado)")
        return real(dados, caminho)

    monkeypatch.setattr(estado_store, "_gravar_bytes", gravar_que_falha)
    with pytest.raises(OSError):
        _rodar_ciclo()
    monkeypatch.setattr(estado_store, "_gravar_bytes", real)
    assert chamadas["n"] == 2
    meio = json.loads(open(c.arq).read())          # continua JSON válido
    assert not [n for n in os.listdir(c.base) if n.startswith("estado.json") and n != "estado.json"
                and not n.endswith(".lock")], os.listdir(c.base)
    assert meio["cursores"][DIALOG] == antes["cursores"][DIALOG], "cursor andou sem a 2ª transação"

    # ---------------- "reinício": novo processo lógico (caches de memória zerados; tudo do disco)
    monkeypatch.setattr(bc, "_webhook_cache", None)
    monkeypatch.setitem(tg._MIGRACAO_CIRCUITO, "feita", False)
    r3 = _rodar_ciclo()
    assert not r3["erros"] or not [e for e in r3["erros"] if "mensagens" in e], r3["erros"]
    c.baixar()
    c.tempo_passou()
    r4 = _rodar_ciclo()                               # ciclo extra sem mensagem nova
    c.baixar()
    ids3 = set(c.vivos())
    assert ids2 <= ids3
    est = c.estado()
    aberto_no_estado = any("801" in [str(x) for x in r.get("file_ids") or []] for r in c.vivos().values())
    regiao = [m.get("id") for m in (est.get("f1_regiao") or {}).get(DIALOG, {}).get("mensagens") or []]
    # 7 kits fechados (+ o aberto, se a triagem o persistir como ABERTO), sem lotes artificiais
    assert len(ids3) == 7 + int(aberto_no_estado), sorted(ids3)
    assert aberto_no_estado or c.seq[-1]["id"] in regiao, "mensagens do lote aberto sumiram"
    assert r4.get("mensagens_novas", 0) == 0
    est = c.estado()
    assert est["cursores"][DIALOG] == c.seq[-1]["id"]
    assert est["cursor_msg_id"] == c.seq[-1]["id"]
    _conferir_documentos(c, sorted(DOCS - {"502"} - (set() if aberto_no_estado else {"801"})))
    # áudio no meio do kit: registrado como mídia, nunca baixado (nem GET da URL)
    _lid6, k6 = c.por_fid("103")
    au = _arquivo(k6, "103")
    assert "midia" in (au.get("tipo"), au.get("erro_tipo")), au
    assert au.get("baixado") is False and not au.get("sha256")
    assert c.portal.gets("103") == 0

    # ---------------- identificação por nome (CRM falso)
    res_cpf = cc.rodar(aplicar=True)
    assert not res_cpf["confirmados"], res_cpf
    est = c.estado()
    _lid2, k2 = c.por_fid("301")
    assert not cpf_util.valido(k2.get("cpf") or ""), "homônimo sem 2ª evidência recebeu CPF"
    assert k2.get("requer_revisao") is True
    _lid1, k1 = c.por_fid("201")
    assert k1.get("requer_revisao") is True and k1.get("cpf") in (None, "", CPF_INVALIDO) \
        or not cpf_util.valido(k1.get("cpf") or ""), "CPF inválido não foi para revisão"
    assert k1.get("requer_revisao") is True

    # ---------------- ponte: simular (não grava) e enviar 2x (idempotente)
    fila_antes = c.fila()
    estado_antes_sim = open(c.arq).read()
    rc, out_sim = _ponte(["--simular", "--todos", "--limite", "15"], capsys)
    assert c.fila() == fila_antes and open(c.arq).read() == estado_antes_sim

    rc, out1 = _ponte(["--enviar", "--todos", "--limite", "15"], capsys)
    fila1 = c.fila()
    rc, out2 = _ponte(["--enviar", "--todos", "--limite", "15"], capsys)
    fila2 = c.fila()
    ids_fila = [j["id"] for j in fila2]
    assert len(ids_fila) == len(set(ids_fila)), "job duplicado"
    assert fila1 == fila2, "2ª rodada da ponte mexeu na fila"

    est = c.estado()
    lid = {k: c.por_fid(f)[0] for k, f in (("K0", "101"), ("K1", "201"), ("K2", "301"), ("K3", "401"),
                                           ("K4", "501"), ("K5", "601"), ("K6", "705"))}
    if aberto_no_estado:
        lid["AB"] = c.por_fid("801")[0]
    enviados = {j["id"] for j in fila2}
    assert f"grupo-{lid['K0']}" in enviados
    assert f"grupo-{lid['K3']}" in enviados, "kit da Bea (baixado no retry) não foi"
    for k in [x for x in ("K1", "K2", "K4", "AB") if x in lid]:
        assert f"grupo-{lid[k]}" not in enviados, f"{k} não podia ir à fila"
    assert est["lotes"][lid["K5"]]["status"] == "descartado_duplicata", est["lotes"][lid["K5"]].get("status")
    assert est["lotes"][lid["K6"]]["status"] != "descartado_duplicata", "doc complementar novo descartado"
    if "AB" in lid:
        assert est["lotes"][lid["AB"]]["status"] == "ABERTO"
    assert not any("801" in json.dumps(j) for j in fila2)

    # staging do K0: só os 3 documentos (sem o áudio), bytes íntegros
    job0 = next(j for j in fila2 if j["id"] == f"grupo-{lid['K0']}")
    pasta = c.sep / "data" / "staging" / job0["upload_id"]
    shas = sorted(hashlib.sha256((pasta / n).read_bytes()).hexdigest()
                  for n in os.listdir(pasta) if not n.endswith(".meta.json"))
    assert shas == sorted(SHA[f] for f in ("101", "102", "104"))

    # ---------------- nenhum segredo/CPF/senha em log/stdout; estado sem senha
    saida = out_sim + out1 + out2 + capsys.readouterr().out + caplog.text
    for cpf in TODOS_CPFS + [CPF_INVALIDO]:
        assert cpf not in saida and cpf_util.formatar(cpf) not in saida, "CPF em claro na saída/log"
    assert TOKEN not in saida and SENHA not in saida
    assert SENHA not in open(c.arq).read(), "senha gov.br persistida no estado.json"


# =====================================================================================
# Defeitos encontrados pelo cenário (xfail strict: passam a falhar = XPASS quando corrigidos)
# G-01 (corrigido na C4): _lote_recente_dl recebia lote['fim'] datetime e _parse_data só
# aceitava str -> todo lote era 'adiado (lote antigo)'; o ciclo nunca baixava.
def test_g01_ciclo_baixa_kit_recente(cena):
    cena.ver_ate("ciclo1")
    _rodar_ciclo()
    _lid, reg = cena.por_fid("101")
    a = _arquivo(reg, "101")
    assert a.get("baixado") is True, a.get("erro")


# G-02 (corrigido na C4): download_svc gravava mídia só como erro_tipo='midia' e a ponte não lia
# esse campo -> o kit com áudio ficava AGUARDANDO_DOWNLOAD para sempre.
def test_g02_kit_com_audio_vai_a_fila(cena, capsys, monkeypatch):
    seq, marcos = montar_conversa(audio_em="K0")
    cena.portal.fonte.todas = seq
    cena.seq, cena.marcos = seq, marcos
    cena.ver_ate("K0")
    _rodar_ciclo()
    cena.baixar()
    lid0, k0 = cena.por_fid("103")
    au = _arquivo(k0, "103")
    assert "midia" in (au.get("tipo"), au.get("erro_tipo")), au   # o áudio foi reconhecido...
    assert au.get("baixado") is False and cena.portal.gets("103") == 0
    _ponte(["--enviar", "--todos", "--limite", "15"], capsys)
    assert f"grupo-{lid0}" in {j["id"] for j in cena.fila()}   # ...e o kit vai à fila
