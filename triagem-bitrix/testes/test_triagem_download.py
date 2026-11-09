# -*- coding: utf-8 -*-
"""Frente C1 (onda 2) — download dentro do ciclo e disjuntores (B1, B2, B4, C1, D3, E3, G2).

Rede falsa nos dois pontos de transporte: `triagem_grupo._abrir_com_teto` (VibeCode e
URLs assinadas) e `bitrix_cliente._post` (REST do Bitrix). Nenhuma conexão real.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

import bitrix_cliente
import caminhos
import rede_bitrix as rb
import triagem_grupo as tg

WH = "https://exemplo.bitrix24.com.br/rest/1/tokenficticio9876/"
# outros testes trocam tg.buscar_mensagens sem monkeypatch: guarda a função real na coleta
BUSCAR_MENSAGENS_REAL = tg.buscar_mensagens
CIRCUITOS = ("bitrix-leitura", "bitrix-download", "vibecode-leitura", "vibecode-download")


class Resp:
    def __init__(self, dados: bytes, quebra_apos: int | None = None):
        self._b = io.BytesIO(dados)
        self._quebra = quebra_apos
        self._lidos = 0

    def read(self, n=-1):
        if self._quebra is not None and self._lidos >= self._quebra:
            raise ConnectionResetError("conexão caiu no meio")
        bloco = self._b.read(n)
        self._lidos += len(bloco)
        return bloco

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Rede:
    """Roteador: rotas = lista de (trecho_da_url, função(url, corpo) -> (status, bytes|Resp))."""

    def __init__(self):
        self.rotas = []
        self.log = []

    def rota(self, trecho, fn):
        self.rotas.append((trecho, fn))

    def _resolver(self, url, corpo):
        self.log.append(url)
        for trecho, fn in self.rotas:
            if trecho in url:
                return fn(url, corpo)
        return 404, b'{"error":"NOT_FOUND"}'

    def abrir(self, req, timeout):            # substitui tg._abrir_com_teto
        corpo = json.loads(req.data.decode()) if getattr(req, "data", None) else None
        status, dados = self._resolver(req.full_url, corpo)
        if isinstance(dados, Resp):
            return dados
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "erro", {}, io.BytesIO(dados))
        return Resp(dados)

    def post(self, url, params, timeout):     # substitui bitrix_cliente._post
        status, dados = self._resolver(url, params)
        return status, dados.decode()

    def conta(self, trecho):
        return sum(1 for u in self.log if trecho in u)


@pytest.fixture
def rede(monkeypatch, tmp_path):
    r = Rede()
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", WH)
    monkeypatch.setenv("BITRIX_RATE", "100")
    monkeypatch.setenv("BITRIX_RAJADA", "1000")
    monkeypatch.setattr(bitrix_cliente, "_webhook_cache", None)
    monkeypatch.setattr(bitrix_cliente, "_post", r.post)
    monkeypatch.setattr(tg, "_abrir_com_teto", r.abrir)
    monkeypatch.setattr(tg, "_RATE_MIN_SEG", 0)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(tg, "CIRCUIT_ARQUIVO", str(tmp_path / "circuit-download.json"))
    if hasattr(tg, "_MIGRACAO_CIRCUITO"):
        monkeypatch.setitem(tg._MIGRACAO_CIRCUITO, "feita", False)
    for nome in CIRCUITOS:
        try:
            os.remove(caminhos.em_base(f"circuit-{nome}.json"))
        except FileNotFoundError:
            pass
    r.enfileirados = []
    r.enfileirados_kw = []

    def _retry(fid, destino, erro, **kw):
        r.enfileirados.append(fid)
        r.enfileirados_kw.append(dict(kw, destino=destino))

    monkeypatch.setattr(tg, "_enfileirar_retry", _retry)
    return r


def _disk_file_get(urls_por_fid, nomes=None):
    """Rota do disk.file.get: cada chamada devolve a próxima URL da lista daquele id."""
    contagem = {}

    def fn(url, corpo):
        fid = str(corpo["id"])
        i = contagem.get(fid, 0)
        contagem[fid] = i + 1
        lista = urls_por_fid[fid]
        return 200, json.dumps({"result": {"ID": fid, "NAME": (nomes or {}).get(fid, "doc.pdf"),
                                           "SIZE": 0, "DOWNLOAD_URL": lista[min(i, len(lista) - 1)]}}).encode()
    return fn


def _sha(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- C1
def test_midia_e_registrada_mas_nao_baixada_nem_enfileirada(rede, tmp_path):
    rede.rota("/v1/files/", lambda u, c: (200, b"ID3 audio"))
    m = tg.baixar_file_id_api("chave", "9001", str(tmp_path / "L"), 1, "LOTE-X",
                              meta_hint={"nome": "audio_do_cliente.mp3"}, webhook=WH)
    assert m.get("tipo") == "midia" and m["baixado"] is False and not m.get("erro")
    assert rede.log == [], "mídia não pode gerar chamada de rede"
    assert rede.enfileirados == []


def test_midia_pelo_tipo_da_api_sem_extensao(rede, tmp_path):
    m = tg.baixar_file_id_api("chave", "9002", str(tmp_path / "L"), 1, "LOTE-X",
                              meta_hint={"nome": "", "tipo": "audio"}, webhook=WH)
    assert m.get("tipo") == "midia" and rede.log == []


def test_id_telegram_legado_nunca_vai_ao_disk_file_get(rede, tmp_path):
    fid = "CQACAgEAAxkBAAIG02rnFicticio"
    m = tg.baixar_file_id_api("chave", fid, str(tmp_path / "L"), 1, "LOTE-X",
                              meta_hint={"nome": "contrato.pdf"}, webhook=WH)
    assert rede.conta("disk.file.get") == 0
    assert m["baixado"] is False and m.get("erro_tipo") == "telegram_legado"
    assert rede.enfileirados == [], "id legado na fila de retry só martela o disk.file.get"
    # ciclo seguinte: erro definitivo não volta à rede
    rede.log.clear()
    m2 = tg.baixar_file_id_api("chave", fid, str(tmp_path / "L"), 1, "LOTE-X",
                               meta_hint={"nome": "contrato.pdf"}, webhook=WH, anterior=m)
    assert rede.log == [] and m2.get("erro_tipo") == "telegram_legado"


# ---------------------------------------------------------------- D3
def test_url_expirada_pede_disk_file_get_novo_e_nao_reusa_a_url_morta(rede, tmp_path):
    url1 = "https://exemplo.bitrix24.com.br/disk/url-velha"
    url2 = "https://exemplo.bitrix24.com.br/disk/url-nova"
    rede.rota("disk.file.get", _disk_file_get({"700": [url1, url2]}))
    rede.rota("url-velha", lambda u, c: (403, b"expirada"))
    rede.rota("url-nova", lambda u, c: (200, b"%PDF-1.4 ok"))
    m = tg.baixar_file_id_api(None, "700", str(tmp_path / "L"), 1, "LOTE-X",
                              meta_hint={"nome": "doc.pdf"}, webhook=WH)
    assert m["baixado"] is True and m["sha256"] == _sha(b"%PDF-1.4 ok")
    assert rede.conta("url-velha") == 1, "a URL assinada morta foi retentada (D3)"
    assert rede.conta("disk.file.get") == 2


# ---------------------------------------------------------------- E3
def test_dois_anexos_com_mesmo_nome_tem_destinos_unicos_e_estaveis(rede, tmp_path):
    conteudo = {"101": b"%PDF frente", "102": b"%PDF verso"}
    rede.rota("disk.file.get", _disk_file_get({"101": ["https://exemplo.bitrix24.com.br/d/101"],
                                               "102": ["https://exemplo.bitrix24.com.br/d/102"]}))
    rede.rota("/d/", lambda u, c: (200, conteudo[u.rsplit("/", 1)[1]]))
    pasta = str(tmp_path / "L")
    m1 = tg.baixar_file_id_api(None, "101", pasta, 1, "LOTE-X", meta_hint={"nome": "CNH.pdf"}, webhook=WH)
    m2 = tg.baixar_file_id_api(None, "102", pasta, 2, "LOTE-X", meta_hint={"nome": "CNH.pdf"}, webhook=WH)
    assert m1["baixado"] and m2["baixado"]
    assert m1["caminho"] != m2["caminho"], "o 2º CNH.pdf reaproveitou o arquivo do 1º (E3)"
    assert not m2.get("reaproveitado")
    assert m1["sha256"] == _sha(conteudo["101"]) and m2["sha256"] == _sha(conteudo["102"])
    assert open(m2["caminho"], "rb").read() == conteudo["102"]
    # ciclo seguinte: cada file_id volta ao SEU arquivo, sem baixar de novo
    rede.log.clear()
    ocupados = {m1["caminho"]: "101", m2["caminho"]: "102"}
    r2 = tg.baixar_file_id_api(None, "102", pasta, 2, "LOTE-X", meta_hint={"nome": "CNH.pdf"},
                               webhook=WH, anterior=m2, ocupados=ocupados)
    assert r2["caminho"] == m2["caminho"] and r2.get("reaproveitado") and rede.log == []


def test_parcial_ou_part_nunca_conta_como_baixado(rede, tmp_path):
    pasta = tmp_path / "L"
    pasta.mkdir()
    (pasta / "01_LOTE-X.jpg.part").write_bytes(b"pedaco truncado")
    (pasta / "01_LOTE-X.jpg.parcial").write_bytes(b"outro pedaco")
    rede.rota("/v1/files/", lambda u, c: (200, b"\xff\xd8 jpeg inteiro"))
    m = tg.baixar_file_id_api("chave", "9100", str(pasta), 1, "LOTE-X",
                              meta_hint={"nome": "4AIY6mB-C5dZPySU_HYEww==.jpg"})
    assert m["baixado"] is True
    assert not m["caminho"].endswith((".part", ".parcial"))
    assert open(m["caminho"], "rb").read() == b"\xff\xd8 jpeg inteiro"
    # meta anterior apontando para .part também não é reaproveitada
    falso = {"file_id": "9100", "baixado": True, "caminho": str(pasta / "01_LOTE-X.jpg.part")}
    assert not tg._reaproveitavel(falso)


def test_download_usa_tmp_exclusivo_e_nao_apaga_parcial_alheio(rede, tmp_path):
    pasta = tmp_path / "L"
    pasta.mkdir()
    alheios = [pasta / "doc.pdf.parcial", pasta / "01_doc.pdf.parcial"]
    for a in alheios:
        a.write_bytes(b"download de outro processo em andamento")
    rede.rota("/v1/files/", lambda u, c: (200, Resp(b"%PDF parcial demais", quebra_apos=4)))
    m = tg.baixar_file_id_api("chave", "9200", str(pasta), 1, "LOTE-X", meta_hint={"nome": "doc.pdf"})
    assert m["baixado"] is False and m["erro"]
    for a in alheios:
        assert a.exists(), f"{a.name}: o download apagou/sobrescreveu o tmp de outro processo"
    sobras = [n for n in os.listdir(pasta) if n not in {a.name for a in alheios}]
    assert sobras == [], f"sobrou arquivo do download que falhou: {sobras}"


def test_download_vazio_nao_vira_baixado(rede, tmp_path):
    rede.rota("/v1/files/", lambda u, c: (200, b""))
    m = tg.baixar_file_id_api("chave", "9300", str(tmp_path / "L"), 1, "LOTE-X",
                              meta_hint={"nome": "doc.pdf"})
    assert m["baixado"] is False
    assert not os.path.exists(os.path.join(str(tmp_path / "L"), "01_doc.pdf"))


# ---------------------------------------------------------------- B1 + migração do legado
def test_bitrix_aberto_nao_impede_fallback_vibecode_e_legado_e_migrado(rede, tmp_path):
    with open(tg.CIRCUIT_ARQUIVO, "w", encoding="utf-8") as fh:
        json.dump({"aberto_em": time.time()}, fh)                # bloqueio do portal agora
    rede.rota("disk.file.get", lambda u, c: (200, b'{"result":{}}'))
    rede.rota("/v1/files/", lambda u, c: (200, b"%PDF via vibecode"))
    m = tg.baixar_file_id_api("chave", "800", str(tmp_path / "L"), 1, "LOTE-X",
                              meta_hint={"nome": "doc.pdf"}, webhook=WH)
    assert m["baixado"] is True, f"disjuntor do Bitrix bloqueou a VibeCode (B1): {m.get('erro')}"
    assert m["fonte"].startswith("api:")
    assert rede.conta("disk.file.get") == 0, "Bitrix aberto não pode ser chamado"
    assert not os.path.exists(tg.CIRCUIT_ARQUIVO), "legado deve ser migrado (renomeado), não relido"
    assert [n for n in os.listdir(tmp_path) if n.startswith("circuit-download.json.migrado-")]
    assert not rb.circuito("bitrix-download").permitido()


def test_vibecode_aberto_nao_impede_o_bitrix(rede, tmp_path):
    rb.circuito("vibecode-download").abrir("teste", duracao_s=300)
    rede.rota("disk.file.get", _disk_file_get({"801": ["https://exemplo.bitrix24.com.br/d/801"]}))
    rede.rota("/d/801", lambda u, c: (200, b"%PDF via bitrix"))
    m = tg.baixar_file_id_api("chave", "801", str(tmp_path / "L"), 1, "LOTE-X",
                              meta_hint={"nome": "doc.pdf"}, webhook=WH)
    assert m["baixado"] is True and m["fonte"] == "webhook:disk.file.get"
    assert rede.conta("/v1/files/") == 0


def test_rate_limit_no_disk_file_get_abre_so_o_disjuntor_novo(rede, tmp_path):
    rede.rota("disk.file.get", lambda u, c: (503, b'{"error":"QUERY_LIMIT_EXCEEDED"}'))
    rede.rota("/v1/files/", lambda u, c: (200, b"%PDF via vibecode"))
    for i in range(3):
        m = tg.baixar_file_id_api("chave", f"81{i}", str(tmp_path / "L"), i + 1, "LOTE-X",
                                  meta_hint={"nome": f"d{i}.pdf"}, webhook=WH)
        assert m["baixado"] is True                               # VibeCode cobre
    assert not os.path.exists(tg.CIRCUIT_ARQUIVO), "o arquivo legado não pode ser recriado"
    assert not rb.circuito("bitrix-download").permitido()


# ---------------------------------------------------------------- B2
def test_rate_limit_da_leitura_nao_abre_o_disjuntor_de_download(rede):
    rede.rota("im.dialog.messages.get", lambda u, c: (503, b'{"error":"QUERY_LIMIT_EXCEEDED"}'))
    with pytest.raises(Exception):
        BUSCAR_MENSAGENS_REAL(WH, "chat1")
    assert not os.path.exists(tg.CIRCUIT_ARQUIVO), "leitura abriu o disjuntor de download (B2)"
    assert rb.circuito("bitrix-download").permitido()


# ---------------------------------------------------------------- B4
def test_404_de_item_nao_abre_o_disjuntor_e_e_permanente(rede, tmp_path):
    rede.rota("disk.file.get", lambda u, c: (400, b'{"error":"ERROR_NOT_FOUND","error_description":"x"}'))
    for i in range(5):
        m = tg.baixar_file_id_api(None, f"90{i}", str(tmp_path / "L"), i + 1, "LOTE-X",
                                  meta_hint={"nome": f"d{i}.pdf"}, webhook=WH)
        assert m["baixado"] is False and m.get("erro_tipo") == "permanente"
    assert rb.circuito("bitrix-download").permitido(), "arquivo apagado não é falha da fonte (B4)"
    assert rede.enfileirados == []


def test_bug_nosso_na_leitura_nao_abre_o_disjuntor_da_fonte(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "ARQ_BREAKER", str(tmp_path / "cb.json"))
    chamadas = {"api": 0}

    def api_com_bug(*a, **k):
        chamadas["api"] += 1
        raise TypeError("bug nosso no parser")

    monkeypatch.setattr(tg, "buscar_mensagens_api", api_com_bug)
    monkeypatch.setattr(tg, "buscar_mensagens", lambda *a, **k: [{"id": 1}])
    conf = {"circuit_breaker_falhas": 1, "circuit_breaker_timeout_s": 300}
    for _ in range(3):
        tg._ler_mensagens_com_breaker("K", "chat1", 1, conf, lambda: WH)
    assert chamadas["api"] == 3, "erro de programação abriu o disjuntor da API (B4)"


# ---------------------------------------------------------------- G2
def test_guarda_de_dns_nao_esgota_com_conexoes_penduradas(monkeypatch):
    libera = threading.Event()
    original = urllib.request.urlopen

    def urlopen_falso(req, timeout=None):
        if "pendura" in req.full_url:
            libera.wait(10)                                      # getaddrinfo mudo
            raise OSError("solto no fim do teste")
        return Resp(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_falso)
    monkeypatch.setattr(tg, "TETO_DNS_SEG", 0.05)
    try:
        for _ in range(8):
            with pytest.raises(Exception):
                tg._abrir_com_teto(urllib.request.Request("https://pendura.invalid/x"), 0)
        t0 = time.time()
        resp = tg._abrir_com_teto(urllib.request.Request("https://ok.invalid/x"), 0)
        assert resp.read() == b"ok", "com 8 DNS pendurados toda chamada passou a falhar (G2)"
        assert time.time() - t0 < 1.0
    finally:
        libera.set()
        monkeypatch.setattr(urllib.request, "urlopen", original)


# ---------------------------------------------------------------- pendência da frente D
def test_erro_transitorio_enfileira_retry_com_lote_e_indice(rede, tmp_path):
    """Falha transitória (5xx) nas fontes -> fila de retry recebe lote_id e índice do anexo."""
    rede.rota("disk.file.get", lambda u, c: (500, b'{"error":"INTERNAL"}'))
    rede.rota("/v1/files/", lambda u, c: (500, b"erro"))
    rede.rota("/v1/chats/", lambda u, c: (500, b"erro"))
    m = tg.baixar_file_id_api("chave", "4242", str(tmp_path / "L"), 3, "LOTE-RETRY",
                              meta_hint={"nome": "rg.pdf"}, webhook=WH)
    assert m["baixado"] is False and m.get("erro_tipo") == "transitorio"
    assert rede.enfileirados == ["4242"]
    kw = rede.enfileirados_kw[0]
    assert kw.get("lote_id") == "LOTE-RETRY" and kw.get("indice") == 3


def test_enfileirar_retry_repassa_lote_id_e_indice_ao_download_svc(monkeypatch, tmp_path):
    pedidos = []

    class FilaFalsa:
        def __init__(self, caminho):
            pass

        def enfileirar(self, file_id, destino=None, **kw):
            pedidos.append((file_id, destino, kw))

    class SvcFalso:
        DEFAULT_QUEUE_FILE = str(tmp_path / "fila.json")
        Fila = FilaFalsa

    monkeypatch.setattr(tg, "download_svc", SvcFalso)
    tg._enfileirar_retry("55", str(tmp_path / "03_rg.pdf"), "HTTP 500",
                         lote_id="LOTE-A", indice=3, nome="rg.pdf")
    assert pedidos == [("55", str(tmp_path / "03_rg.pdf"),
                        {"lote_id": "LOTE-A", "indice": 3, "nome": "rg.pdf"})]


def test_ciclo_processa_fila_de_retry_em_rodada_curta():
    import inspect
    assert tg.RETRY_LIMITE_ITENS == 5 and tg.RETRY_TETO_RODADA_S == 60
    fonte = inspect.getsource(tg.ciclo)
    assert "limite_itens=RETRY_LIMITE_ITENS" in fonte and "teto_rodada_s=RETRY_TETO_RODADA_S" in fonte
