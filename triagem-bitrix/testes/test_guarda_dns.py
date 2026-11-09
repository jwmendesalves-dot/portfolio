#!/usr/bin/env python3
"""Regressão da GUARDA ANTI-TRAVA DE DNS (27/10/2026).

O que aconteceu de verdade: `urlopen(timeout=10)` não limita getaddrinfo; o ciclo
da triagem ficou 28 min pendurado num lookup e o `systemctl` seguia "active".
Aqui provamos que a abertura da conexão tem PRAZO DURO.
"""
from conftest import RAIZ
import sys
import time
import urllib.request

sys.path.insert(0, RAIZ)
import triagem_grupo as tg  # noqa: E402


def test_abertura_estoura_no_prazo(monkeypatch=None):
    original = urllib.request.urlopen

    def lento(*a, **k):
        time.sleep(3)                      # simula getaddrinfo pendurado
        return original(*a, **k)

    urllib.request.urlopen = lento
    tg.TETO_DNS_SEG = 0.2                  # prazo curto para o teste
    t0 = time.time()
    try:
        tg._abrir_com_teto(urllib.request.Request("https://example.invalid/x"), 0)
        raise AssertionError("deveria ter estourado o prazo")
    except Exception as e:
        dt = time.time() - t0
        assert dt < 1.5, f"estourou tarde demais: {dt:.1f}s"
        assert type(e).__name__ == "TimeoutError", f"tipo inesperado: {type(e).__name__}"
    finally:
        urllib.request.urlopen = original
        tg.TETO_DNS_SEG = 8


def test_erro_de_dns_vira_timeout_tratado_em_http_json():
    """http_json precisa absorver o estouro (retry/circuit-breaker) — não propagar cru."""
    original = urllib.request.urlopen

    def explode(*a, **k):
        raise OSError("Temporary failure in name resolution")

    urllib.request.urlopen = explode
    try:
        tg.http_json("https://example.invalid/x", tentativas=1)
        raise AssertionError("deveria ter levantado RuntimeError após as tentativas")
    except RuntimeError as e:
        assert "esgotou" in str(e)
    finally:
        urllib.request.urlopen = original


if __name__ == "__main__":
    test_abertura_estoura_no_prazo()
    print("ok: prazo duro na abertura de conexão")
    test_erro_de_dns_vira_timeout_tratado_em_http_json()
    print("ok: erro de DNS vira RuntimeError tratado (retry/circuit-breaker)")
