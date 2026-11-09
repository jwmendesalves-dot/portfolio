# -*- coding: utf-8 -*-
"""
TDD — integração do circuit breaker no fluxo de leitura de triagem_grupo.py.

Prova, SEM rede, que `_ler_mensagens_com_breaker` realmente:
  · registra falhas da API e, ao abrir (N seguidas), REDIRECIONA para o webhook
    SEM chamar a API de novo;
  · sucesso pela API fecha o breaker e não redireciona;
  · não grava nada fora do arquivo de breaker apontado (ARQ_BREAKER temporário).

C2b: tudo via `monkeypatch` (antes trocava tg.buscar_mensagens/tg.ARQ_BREAKER
direto no módulo e vazava para os testes seguintes); os fakes aceitam
`first_id`/`max_paginas`/`info` (assinatura atual do leitor).
"""
import json
import time

import pytest

import triagem_grupo as tg

CONF = {"circuit_breaker_falhas": 3, "circuit_breaker_timeout_s": 300}


@pytest.fixture
def arq(tmp_path, monkeypatch):
    caminho = str(tmp_path / "cb.json")
    monkeypatch.setattr(tg, "ARQ_BREAKER", caminho)
    return caminho


def _ler(conf=CONF, cursor=None):
    return tg._ler_mensagens_com_breaker("K", "chat1", 12, conf, lambda: "https://wh/",
                                         cursor=cursor)


def test_redireciona_para_webhook_apos_n_falhas_da_api(arq, monkeypatch):
    chamadas = {"api": 0, "webhook": 0}

    def fake_api(chave, dialog, max_paginas=None, cfg=None, ate_id=None, info=None):
        chamadas["api"] += 1
        raise RuntimeError("API do VibeCode caiu")

    def fake_webhook(w, dialog, max_paginas=40, cfg=None, first_id=None, ate_id=None, info=None):
        chamadas["webhook"] += 1
        return [{"id": 1, "texto": "msg via webhook"}]

    monkeypatch.setattr(tg, "buscar_mensagens_api", fake_api)
    monkeypatch.setattr(tg, "buscar_mensagens", fake_webhook)

    # 3 ciclos: API falha (contador 1..3) e cai para o webhook a cada vez.
    for _ in range(3):
        msgs, fonte = _ler()
        assert fonte.endswith("FAILOVER]"), fonte
    assert chamadas["api"] == 3 and chamadas["webhook"] == 3

    # 4º ciclo: a API está ABERTA -> NÃO pode ser chamada; vai direto ao webhook.
    antes_api = chamadas["api"]
    msgs, fonte = _ler()
    assert chamadas["api"] == antes_api, "API aberta NÃO deve ser chamada de novo"
    assert msgs == [{"id": 1, "texto": "msg via webhook"}]
    assert fonte.endswith("FAILOVER]")
    assert chamadas["webhook"] == 4


def test_failover_webhook_respeita_historico_paginas_e_first_id(arq, monkeypatch):
    """F1-06: 1ª leitura pelo webhook usa historico_paginas (não 40 fixo);
    leitura incremental passa o cursor como first_id."""
    vistos = []

    def fake_webhook(w, dialog, max_paginas=40, cfg=None, first_id=None, ate_id=None, info=None):
        vistos.append({"max_paginas": max_paginas, "first_id": first_id})
        return []

    monkeypatch.setattr(tg, "buscar_mensagens_api",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api")))
    monkeypatch.setattr(tg, "buscar_mensagens", fake_webhook)
    _ler()
    _ler(cursor=500)
    assert vistos[0] == {"max_paginas": 12, "first_id": None}
    assert vistos[1]["first_id"] == 500


def test_sucesso_pela_api_nao_redireciona(arq, monkeypatch):
    monkeypatch.setattr(tg, "buscar_mensagens_api", lambda *a, **k: [{"id": 2, "texto": "via api"}])
    monkeypatch.setattr(tg, "buscar_mensagens", lambda *a, **k: [{"id": 3, "texto": "via webhook"}])
    msgs, fonte = _ler()
    assert msgs == [{"id": 2, "texto": "via api"}]
    assert fonte.startswith("api(vibecode)")


def test_ambas_abertas_levanta_runtimeerror(arq, monkeypatch):
    monkeypatch.setattr(tg, "buscar_mensagens_api",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api")))
    monkeypatch.setattr(tg, "buscar_mensagens",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("webhook")))
    conf = {"circuit_breaker_falhas": 1, "circuit_breaker_timeout_s": 300}
    for _ in range(2):  # abre API e webhook (N=1)
        with pytest.raises(RuntimeError):
            _ler(conf)
    # agora as DUAS estão abertas -> RuntimeError imediato (sem tentar rede)
    with pytest.raises(RuntimeError, match="ABERTO"):
        _ler(conf)


def test_arquivo_legado_sem_campos_novos_continua_valido(arq, monkeypatch):
    """circuit_breaker.json gravado pela versão antiga (sem aberto_em/motivo/sonda)
    precisa ser lido igual: API aberta -> não é chamada, vai direto ao webhook."""
    with open(arq, "w", encoding="utf-8") as fh:
        json.dump({"leitura.api": {"estado": "open", "falhas": 3, "aberto_ate": time.time() + 300,
                                   "ultimo_erro": "502"},
                   "leitura.webhook": {"estado": "closed", "falhas": 0}}, fh)
    chamadas = {"api": 0}

    def fake_api(*a, **k):
        chamadas["api"] += 1
        return []

    monkeypatch.setattr(tg, "buscar_mensagens_api", fake_api)
    monkeypatch.setattr(tg, "buscar_mensagens", lambda *a, **k: [{"id": 9}])
    msgs, fonte = _ler()
    assert chamadas["api"] == 0 and msgs == [{"id": 9}] and fonte.endswith("FAILOVER]")
    # e a chave legada continua no arquivo (nada foi apagado)
    with open(arq, encoding="utf-8") as fh:
        assert json.load(fh)["leitura.api"]["estado"] == "open"


def test_nada_vaza_para_o_modulo_depois_dos_testes():
    """Com monkeypatch, as funções reais do módulo continuam lá."""
    assert tg.buscar_mensagens.__module__ == "triagem_grupo"
    assert tg.buscar_mensagens_api.__module__ == "triagem_grupo"
