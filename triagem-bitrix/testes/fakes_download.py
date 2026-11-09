# -*- coding: utf-8 -*-
"""Rede FALSA para os testes de download (frente D). Nenhuma conexão real.

Substitui `urllib.request.urlopen` — o ponto comum ao código ORIGINAL e ao novo
(bitrix_cliente._post, download_svc._abrir, baixar_pendentes antigo) — para que os
mesmos testes provem o defeito no código antigo e a correção no novo.
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.request


WEBHOOK = "https://teste.bitrix24.com.br/rest/1/tok3n5ecret/"
DORMIR_REAL = time.sleep


class Resp:
    """Resposta HTTP falsa (context manager, read/read1, headers)."""

    def __init__(self, corpo: bytes = b"", status: int = 200, headers: dict | None = None,
                 gotejar_s: float = 0.0, url: str = ""):
        self._buf = io.BytesIO(corpo)
        self.status = status
        self.headers = dict(headers or {})
        self.gotejar_s = gotejar_s
        self.url = url

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if self.gotejar_s:
            DORMIR_REAL(self.gotejar_s)
            return self._buf.read(1)
        return self._buf.read() if n is None or n < 0 else self._buf.read(n)

    def read1(self, n=-1):
        return self.read(n if not self.gotejar_s else 1)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def erro_http(url, status, corpo=b"", headers=None):
    return urllib.error.HTTPError(url, status, "erro", headers or {}, io.BytesIO(corpo))


class Rede:
    """Roteador: disk.file.get (Bitrix), DOWNLOAD_URL assinada e VibeCode.

    bitrix[fid]  -> lista (consumida em ordem; o último repete) de dict `result`
                    ou (status, corpo_json_str)
    urls[url]    -> lista de Resp | Exception | callable() -> Resp
    vibecode[fid]-> idem urls
    """

    def __init__(self):
        self.bitrix: dict = {}
        self.urls: dict = {}
        self.vibecode: dict = {}
        self.chamadas: list = []          # (tipo, chave, headers)

    # contagens --------------------------------------------------------------
    def n(self, tipo, chave=None):
        return sum(1 for t, k, _ in self.chamadas if t == tipo and (chave is None or k == chave))

    @staticmethod
    def _proximo(lista):
        return lista.pop(0) if len(lista) > 1 else lista[0]

    def _resposta(self, alvo, url):
        if callable(alvo) and not isinstance(alvo, (Resp, BaseException)):
            alvo = alvo()
        if isinstance(alvo, BaseException):
            raise alvo
        if isinstance(alvo, Resp) and alvo.status >= 400:
            raise erro_http(url, alvo.status, alvo._buf.getvalue(), alvo.headers)
        return alvo

    def urlopen(self, req, timeout=None, *a, **k):
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        headers = dict(req.header_items()) if isinstance(req, urllib.request.Request) else {}
        if "disk.file.get" in url:
            corpo = json.loads((req.data or b"{}").decode("utf-8"))
            fid = str(corpo.get("id"))
            self.chamadas.append(("disk", fid, headers))
            if fid not in self.bitrix:
                raise erro_http(url, 404, b'{"error":"ERROR_NOT_FOUND"}')
            r = self._proximo(self.bitrix[fid])
            if isinstance(r, BaseException):
                raise r
            if isinstance(r, tuple):
                st, txt = r
                if st >= 400:
                    raise erro_http(url, st, txt.encode())
                return Resp(txt.encode(), st)
            return Resp(json.dumps({"result": r}).encode(), 200)
        if "vibecode.bitrix24.com" in url:
            fid = url.rstrip("/").split("/")[-2]
            self.chamadas.append(("vibe", fid, headers))
            if fid not in self.vibecode:
                raise erro_http(url, 404, b'{"error":{"code":"NOT_FOUND"}}')
            return self._resposta(self._proximo(self.vibecode[fid]), url)
        self.chamadas.append(("get", url, headers))
        if url not in self.urls:
            raise erro_http(url, 404, b"")
        return self._resposta(self._proximo(self.urls[url]), url)

    # atalhos ------------------------------------------------------------------
    def arquivo(self, fid, nome, corpo: bytes, size=None, url=None, headers=None):
        url = url or f"https://teste.bitrix24.com.br/disk/download/?token=T{fid}"
        self.bitrix[str(fid)] = [{"ID": str(fid), "NAME": nome, "SIZE": str(len(corpo) if size is None else size),
                                  "DOWNLOAD_URL": url}]
        self.urls[url] = [Resp(corpo, 200, dict({"Content-Length": str(len(corpo))}, **(headers or {})))]
        return url


def montar_rede(tmp_path, monkeypatch):
    """Ambiente isolado: base em tmp, segredos por env, limitador sem espera, rede falsa."""
    import caminhos
    r = Rede()
    monkeypatch.setattr(urllib.request, "urlopen", r.urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path / "base"))
    monkeypatch.setattr(caminhos, "SEGREDOS_DIR", str(tmp_path / "segredos"))
    os.makedirs(str(tmp_path / "base"), exist_ok=True)
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", WEBHOOK)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "")
    monkeypatch.delenv("TRIAGEM_VIBECODE_KEY", raising=False)
    monkeypatch.setenv("BITRIX_RATELIMIT_FILE", str(tmp_path / "rl"))
    monkeypatch.setenv("VIBECODE_RATELIMIT_FILE", str(tmp_path / "vrl"))
    monkeypatch.setenv("BITRIX_RATE", "100")
    monkeypatch.setenv("BITRIX_RAJADA", "1000")
    try:
        import bitrix_cliente
        monkeypatch.setattr(bitrix_cliente, "_webhook_cache", None)
    except ImportError:
        pass
    return r


def tem(mod, nome):
    return hasattr(mod, nome)
