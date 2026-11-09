#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""429 do portal = OPERATION_TIME_LIMIT: esperar, nunca retentar em cima.

Documentação oficial (estudo 09/11/2026, /root/b24docs):
- `limits.md:108-116` — o limite `operating` são **10 cestos de 1 min**;
  o estouro devolve **429 `OPERATION_TIME_LIMIT`** apenas para aquele
  método/app e libera em `operating_reset_at`.
- `limits.md:37-53` — o limite de requisições (leaky bucket) devolve
  **503 `QUERY_LIMIT_EXCEEDED`**, que é outro erro e outro tratamento.
- `error-codes.md:121` — repetir erro de método devolve resultado idêntico.

Antes desta correção, `http_json` tratava 429 como "transitório genérico": fazia
`sleep(HTTP_BACKOFF ** i)` e tentava de novo **dentro da mesma janela de 1 min**,
o que consome a cota de novo e renova o bloqueio — o mesmo mecanismo que nos
rendeu `ERROR_LOOP_DETECTED` em produção.

Roda: /opt/triagem-bitrix/venv/bin/python -m pytest /opt/triagem-bitrix/testes/test_429_operating.py -q
      (ou python3 -m pytest, se não houver venv)
"""
import urllib.error

import pytest

import triagem_grupo as tg


class RespostaFalsa:
    """HTTPError com corpo e headers controláveis."""

    def __init__(self, status, corpo="", headers=None):
        self.code = status
        self.status = status
        self.headers = headers or {}
        self._corpo = corpo

    def read(self):
        return self._corpo.encode()


def test_429_propaga_sem_retentar(monkeypatch):
    """Uma única tentativa: o 429 não pode virar retry dentro da janela."""
    chamadas = []

    def fake_requisicao(url, dados, headers, timeout):
        chamadas.append(url)
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {},
                                     __import__("io").BytesIO(
                                         b'{"error":"OPERATION_TIME_LIMIT","operating_reset_at":1794210000}'))

    monkeypatch.setattr(tg, "_requisicao", fake_requisicao)
    monkeypatch.setattr(tg.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError) as exc:
        tg.http_json("https://x.bitrix24.com.br/rest/1/tok/crm.deal.list", {})
    assert "OPERATION_TIME_LIMIT" in str(exc.value) or "429" in str(exc.value)
    assert len(chamadas) == 1, f"não podia retentar 429 (foram {len(chamadas)} chamadas)"


def test_espera_429_usa_operating_reset_at_ou_retry_after():
    """A espera vem do portal, não de um backoff fixo nosso."""
    import time as _t
    reset = int(_t.time()) + 42
    corpo = f'{{"error":"OPERATION_TIME_LIMIT","operating_reset_at":{reset}}}'
    fake = RespostaFalsa(429, corpo)
    espera = tg.espera_429(fake, corpo)
    assert 30 <= espera <= 60, espera

    fake2 = RespostaFalsa(429, "{}", {"Retry-After": "17"})
    assert tg.espera_429(fake2, "{}") == pytest.approx(17, abs=0.5)


def test_503_query_limit_continua_sendo_rate_limit(monkeypatch):
    """503 QUERY_LIMIT_EXCEEDED: propaga SEM retentar e vai para o disjuntor DA OPERAÇÃO.

    Ajuste da onda 2 (frente C1, achado B2): antes este teste exigia que `http_json`
    abrisse o disjuntor de DOWNLOAD do Bitrix em qualquer rate limit — inclusive da
    leitura. Agora `http_json` (só VibeCode; a REST do Bitrix usa bitrix_cliente)
    registra no circuito que o chamador indicou e NUNCA no `bitrix-download`.
    """
    import rede_bitrix as rb
    for nome in ("vibecode-leitura", "bitrix-download"):
        rb.circuito(nome).fechar()
    chamadas = []

    def fake_requisicao(url, dados, headers, timeout):
        chamadas.append(url)
        raise urllib.error.HTTPError(url, 503, "Too Many Requests", {},
                                     __import__("io").BytesIO(b'{"error":"QUERY_LIMIT_EXCEEDED"}'))

    monkeypatch.setattr(tg, "_requisicao", fake_requisicao)
    monkeypatch.setattr(tg.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError):
        tg.http_json("https://vibecode.bitrix24.com/v1/chats/x/messages", None,
                     circuito="vibecode-leitura")
    assert len(chamadas) == 1, "rate limit não pode virar retry"
    assert rb.circuito("vibecode-leitura").falhas() >= 1, "devia contar no circuito da operação"
    assert rb.circuito("bitrix-download").permitido(), "leitura não abre o disjuntor de download (B2)"
