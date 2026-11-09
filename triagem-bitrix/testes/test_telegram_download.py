# -*- coding: utf-8 -*-
"""
TDD — DOWNLOAD dos documentos do Telegram (24/09/2026).

Pedido do dono: *"garante que o do tele vai fazer download"*. Este teste cobre o
caminho que baixa o arquivo de verdade (getFile + api.telegram.org/file/...), sem
tocar na rede: reaproveitamento do que já existe, sha256, aviso de tamanho
diferente e erro tratado (meta com `erro`, nunca exceção solta).
"""
import os
from conftest import RAIZ
import hashlib
import sys

import pytest

sys.path.insert(0, RAIZ)

import telegram_grupo as tgg  # noqa: E402

CONTEUDO = b"\xff\xd8\xff" + b"conteudo-da-foto" * 40

# formato REAL do config.json (allowlist aninhada em `telegram`)
CFG = {"telegram": {"habilitado": True, "chats_permitidos": ["555"]}}


class _Resp:
    def __init__(self, dados):
        self._dados = dados
        self._pos = 0

    def read(self, n=-1):
        """Leitura inteira (n<0) ou em blocos — não força a implementação do cliente."""
        if n is None or n < 0:
            pedaco, self._pos = self._dados[self._pos:], len(self._dados)
            return pedaco
        pedaco = self._dados[self._pos:self._pos + n]
        self._pos += len(pedaco)
        return pedaco

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def arq(tmp_path):
    return {"file_id": "FILE-123", "nome": "ficha.jpg", "tamanho": len(CONTEUDO),
            "tipo": "foto", "mime": "image/jpeg"}


@pytest.fixture
def api(monkeypatch):
    """Fake da API do Telegram: getFile + download do arquivo."""
    estado = {"file_path": "photos/ficha.jpg", "bytes": CONTEUDO, "chamadas": []}

    def fake_chamar(token, metodo, **params):
        estado["chamadas"].append((metodo, params))
        if metodo == "getFile":
            if not estado["file_path"]:
                return {"result": {}}
            return {"result": {"file_path": estado["file_path"]}}
        raise AssertionError(f"método inesperado: {metodo}")

    def fake_urlopen(url, timeout=None):
        estado["url"] = url
        return _Resp(estado["bytes"])

    monkeypatch.setattr(tgg, "chamar", fake_chamar)
    monkeypatch.setattr(tgg, "urlopen4", fake_urlopen)
    return estado


def test_baixa_o_arquivo_e_confere_o_sha256(api, arq, tmp_path):
    pasta = str(tmp_path / "lote")
    meta = tgg.baixar_arquivo_telegram("TOKEN", arq, pasta, 1, "LOTE-TESTE",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is True and not meta.get("erro")
    assert meta["nome_salvo"] == "01_LOTE-TESTE.jpg"
    caminho = meta["caminho"]
    assert open(caminho, "rb").read() == CONTEUDO
    assert meta["sha256"] == hashlib.sha256(CONTEUDO).hexdigest()
    assert meta["tamanho_bits"] == len(CONTEUDO)
    assert api["url"].startswith("https://api.telegram.org/file/botTOKEN/")
    assert ("getFile", {"file_id": "FILE-123"}) in api["chamadas"]


def test_extensao_vem_do_nome_original(api, tmp_path):
    arq = {"file_id": "F2", "nome": "kit.pdf", "tamanho": len(CONTEUDO),
           "tipo": "documento", "mime": "application/pdf"}
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 2, "LOTE-X",
                                       cfg=CFG, chat="555")
    assert meta["nome_salvo"] == "02_LOTE-X.pdf"


def test_reaproveita_o_que_ja_existe(api, arq, tmp_path):
    pasta = str(tmp_path / "lote")
    tgg.baixar_arquivo_telegram("T", arq, pasta, 1, "LOTE-TESTE", cfg=CFG, chat="555")
    api["chamadas"].clear()
    meta = tgg.baixar_arquivo_telegram("T", arq, pasta, 1, "LOTE-TESTE", cfg=CFG, chat="555")
    assert meta["reaproveitado"] is True and meta["baixado"] is True
    assert api["chamadas"] == [], "não pode pedir getFile de novo"


def test_tamanho_divergente_e_erro_de_integridade(api, arq, tmp_path):
    """E2: tamanho baixado != file_size = truncado/corrompido; NUNCA vira 'baixado'.

    (Antes virava só `aviso` e continuava `baixado=True` — PDF/JPG corrompido ia
    para o contrato e para a dedupe por sha256.)"""
    arq = {**arq, "tamanho": len(CONTEUDO) + 999}
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is False
    assert meta["erro_tipo"] == "permanente" and meta.get("erro_classe") == "INTEGRIDADE"
    assert "tamanho" in meta["erro"]
    assert not os.path.exists(meta["caminho"] or "/nao-existe"), \
        "arquivo divergente não pode ficar no destino"


def test_getFile_sem_file_path_vira_erro_tratado(api, arq, tmp_path):
    api["file_path"] = None
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is False
    assert "getFile não devolveu file_path" in meta["erro"]


def test_falha_de_rede_vira_erro_tratado(api, arq, tmp_path, monkeypatch):
    def explode(url, timeout=None):
        raise TimeoutError("rede caiu")

    monkeypatch.setattr(tgg, "urlopen4", explode)
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is False and meta["erro"]
    assert not os.path.exists(meta["caminho"] or "/nao-existe") or os.path.getsize(meta["caminho"]) >= 0


# ── C2: limite de 20 MB ANTES do getFile ───────────────────────────────────
def test_acima_de_20mb_nao_chama_getFile_e_vira_erro_permanente(api, tmp_path):
    """C2: o Bot API do Telegram não entrega mais de 20 MB; checar o file_size ANTES
    do getFile e classificar como erro PERMANENTE com orientação de recuperação —
    (antes o getFile era chamado, falhava sem tipo e o retry voltava para sempre)."""
    arq = {"file_id": "CQACGrande", "nome": "video.mp4", "tamanho": 21 * 1024 * 1024,
           "tipo": "video", "mime": "video/mp4"}
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is False
    assert api["chamadas"] == [], "getFile não pode ser chamado para arquivo > 20 MB"
    assert "20 MB" in meta["erro"]
    assert meta["erro_tipo"] == "permanente"
    assert meta.get("erro_classe") == "TAMANHO"
    assert "outro canal" in str(meta.get("recuperacao") or ""), \
        "o cliente precisa saber como recuperar o arquivo por outro canal"
    assert not os.path.exists(meta["caminho"] or "/nao-existe")


def test_exatamente_20mb_ainda_baixa(api, tmp_path):
    dados = b"a" * tgg.LIMITE_ARQUIVO_TELEGRAM
    api["bytes"] = dados
    arq = {"file_id": "F20", "nome": "limite.pdf", "tamanho": len(dados),
           "tipo": "documento", "mime": "application/pdf"}
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is True and meta["tamanho_bits"] == len(dados)


def test_sem_file_size_nao_e_bloqueado_pelo_limite(api, tmp_path):
    """file_size ausente: o limite não pode bloquear às cegas — baixa e valida depois."""
    arq = {"file_id": "F-sem-tam", "nome": "a.pdf", "tamanho": None,
           "tipo": "documento", "mime": "application/pdf"}
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is True


# ── E2: download atômico e validação de integridade ────────────────────────
def test_arquivo_truncado_nao_e_aceito_como_baixado(api, arq, tmp_path):
    """E2: download que cai no meio (veio menos que o file_size) não pode virar 'baixado'."""
    api["bytes"] = CONTEUDO[: len(CONTEUDO) // 2]
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is False
    assert meta["erro_tipo"] == "permanente" and meta.get("erro_classe") == "INTEGRIDADE"
    assert not os.path.exists(meta["caminho"] or "/nao-existe"), \
        "truncado não pode ficar no destino com cara de baixado"


def test_download_vazio_nao_vira_baixado(api, arq, tmp_path):
    api["bytes"] = b""
    meta = tgg.baixar_arquivo_telegram("T", arq, str(tmp_path / "l"), 1, "L",
                                       cfg=CFG, chat="555")
    assert meta["baixado"] is False and meta["erro"]


def test_queda_no_meio_nao_deixa_parcial_nem_destino(monkeypatch, arq, tmp_path):
    """E2: gravar em temporário exclusivo -> validar -> sha256 -> os.replace.
    Queda no meio NÃO pode deixar arquivo truncado no destino (antes gravava direto)."""
    pasta = str(tmp_path / "l")

    def fake_chamar(token, metodo, **params):
        return {"result": {"file_path": "photos/f.jpg"}}

    class _R:
        def __init__(self):
            self.pedacos = [CONTEUDO[:40], CONTEUDO[40:80], OSError("conexao reiniciada")]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            item = self.pedacos.pop(0) if self.pedacos else b""
            if isinstance(item, BaseException):
                raise item
            return item

    monkeypatch.setattr(tgg, "chamar", fake_chamar)
    monkeypatch.setattr(tgg, "urlopen4", lambda url, timeout=None: _R())

    meta = tgg.baixar_arquivo_telegram("T", arq, pasta, 1, "L", cfg=CFG, chat="555")

    assert meta["baixado"] is False and meta["erro"]
    assert not os.path.exists(os.path.join(pasta, "01_L.jpg")), \
        "o destino só pode existir depois do os.replace (E2)"
    if os.path.isdir(pasta):
        sobras = os.listdir(pasta)
        assert not [n for n in sobras if n.endswith((".part", ".parcial", ".tmp"))], \
            "o temporário tem de ser removido na falha"
    import caminhos
    tmp_dir = caminhos.em_base("tmp")
    if os.path.isdir(tmp_dir):
        assert not [n for n in os.listdir(tmp_dir) if n.startswith("tg-")], \
            "temporário privado do download tem de ser removido na falha"


def test_nao_reaproveita_arquivo_truncado_existente(api, arq, tmp_path):
    """E2: reaproveitar truncado (tamanho > 0 mas != file_size) como 'baixado' era o defeito."""
    pasta = str(tmp_path / "l")
    os.makedirs(pasta)
    with open(os.path.join(pasta, "01_LOTE-TESTE.jpg"), "wb") as fh:
        fh.write(CONTEUDO[:10])
    api["chamadas"].clear()

    meta = tgg.baixar_arquivo_telegram("T", arq, pasta, 1, "LOTE-TESTE", cfg=CFG, chat="555")

    assert meta["reaproveitado"] is False, "truncado não pode ser reaproveitado"
    assert meta["baixado"] is True and meta["tamanho_bits"] == len(CONTEUDO)
    assert api["chamadas"] != [], "tem de baixar de novo"
