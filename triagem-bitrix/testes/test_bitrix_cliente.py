"""bitrix_cliente: cliente REST único (webhook por segredo, limitador, disjuntor, classificação)."""
import json

import pytest

import bitrix_cliente as bc
from circuit_breaker import CircuitOpenError
from rede_bitrix import FalhaBitrix, Classe

WEBHOOK = "https://exemplo.bitrix24.com.br/rest/1/" + "a" * 16 + "/"


@pytest.fixture(autouse=True)
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGEM_BASE", str(tmp_path))
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", WEBHOOK)
    monkeypatch.setenv("BITRIX_RATELIMIT_FILE", str(tmp_path / "rl"))
    monkeypatch.setenv("BITRIX_RATE", "100")
    monkeypatch.setenv("BITRIX_RAJADA", "100")
    import caminhos
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path))
    monkeypatch.setattr(bc, "_webhook_cache", None)
    monkeypatch.setattr(bc.time, "sleep", lambda s: None)
    yield


def transporte(respostas):
    chamadas = []

    def t(url, params, timeout):
        chamadas.append((url, params))
        r = respostas.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r
    t.chamadas = chamadas
    return t


def test_sucesso_monta_url_do_segredo():
    t = transporte([(200, json.dumps({"result": {"ID": "1"}}))])
    r = bc.chamar("crm.contact.get", {"id": 1}, _transporte=t)
    assert r["result"]["ID"] == "1"
    assert t.chamadas[0][0] == WEBHOOK + "crm.contact.get.json"


def test_access_denied_nao_repete():
    t = transporte([(403, json.dumps({"error": "ACCESS_DENIED", "error_description": "x"}))] * 3)
    with pytest.raises(FalhaBitrix) as e:
        bc.chamar("crm.contact.get", {"id": 1}, _transporte=t)
    assert e.value.erro.classe == Classe.ACESSO
    assert len(t.chamadas) == 1


def test_rede_transitoria_repete_leitura():
    t = transporte([TimeoutError("timed out"), (200, json.dumps({"result": []}))])
    assert bc.chamar("crm.contact.list", {}, _transporte=t)["result"] == []
    assert len(t.chamadas) == 2


def test_rate_limit_respeita_politica_do_rede_bitrix():
    import rede_bitrix as rb
    erro = rb.classificar(503, json.dumps({"error": "QUERY_LIMIT_EXCEEDED"}))
    t = transporte([(503, json.dumps({"error": "QUERY_LIMIT_EXCEEDED"})), (200, json.dumps({"result": []}))])
    if erro.pausa_rodada:
        with pytest.raises(FalhaBitrix):
            bc.chamar("crm.contact.list", {}, _transporte=t)
        assert len(t.chamadas) == 1
    else:
        assert bc.chamar("crm.contact.list", {}, _transporte=t)["result"] == []


def test_escrita_com_timeout_nao_repete():
    t = transporte([TimeoutError("timed out"), (200, json.dumps({"result": 1}))])
    with pytest.raises(FalhaBitrix):
        bc.chamar("tasks.task.add", {"fields": {}}, escrita=True, _transporte=t)
    assert len(t.chamadas) == 1


def test_erro_nao_vaza_webhook():
    t = transporte([OSError("falha em " + WEBHOOK + "x.json")] * 5)
    with pytest.raises(FalhaBitrix) as e:
        bc.chamar("crm.contact.get", {}, _transporte=t)
    assert "a" * 16 not in str(e.value)


def test_circuito_aberto_bloqueia_sem_chamar():
    import rede_bitrix as rb
    rb.circuito("bitrix-leitura").abrir("teste", duracao_s=600)
    t = transporte([(200, json.dumps({"result": 1}))])
    with pytest.raises(CircuitOpenError):
        bc.chamar("crm.contact.get", {}, _transporte=t)
    assert t.chamadas == []


def test_listar_pagina_tudo_por_id():
    p1 = [{"ID": str(i)} for i in range(1, 51)]
    p2 = [{"ID": str(i)} for i in range(51, 61)]
    t = transporte([(200, json.dumps({"result": p1})), (200, json.dumps({"result": p2})),
                    (200, json.dumps({"result": []}))])
    itens = bc.listar("crm.contact.list", {"filter": {"NAME": "Ana"}}, _transporte=t)
    assert len(itens) == 60
    seg = t.chamadas[1][1]
    assert seg["filter"][">ID"] == "50" and seg["start"] == -1 and seg["filter"]["NAME"] == "Ana"


def test_listar_nao_para_em_pagina_menor_que_50():
    """A1 (Revisor OpenBot): método com página de 20 não pode perder a 2ª página."""
    p1 = [{"ID": str(i)} for i in range(1, 21)]
    p2 = [{"ID": str(i)} for i in range(21, 41)]
    t = transporte([(200, json.dumps({"result": p1})), (200, json.dumps({"result": p2})),
                    (200, json.dumps({"result": []}))])
    assert len(bc.listar("crm.contact.list", {}, _transporte=t)) == 40
