# -*- coding: utf-8 -*-
"""
TDD — MICROSERVIÇO DE DOWNLOAD resiliente (download_svc.py).

Cobre os requisitos centrais (atualizados na frente D):
  (a) fila persistente em disco, recarrega após "reinício" (formato legado aceito);
  (b) retry entre ciclos em MINUTOS/HORAS (rede_bitrix.POLITICA_FILA), não segundos (D2);
      a antiga 'falha' terminal em minutos volta a pendente (migração);
  (c) ordem de fallback: Bitrix (disk.file.get) -> VibeCode.
Rede falsa: testes/fakes_download.py.
"""
import json
import os
import time

from fakes_download import Resp, WEBHOOK  # fixture `rede`: conftest.py

import download_svc as d


# ---------------------------------------------------------------------------
# (a) fila persistente
# ---------------------------------------------------------------------------
def test_fila_persiste_e_recarrega(tmp_path):
    caminho = str(tmp_path / "fila.json")
    fila = d.Fila(caminho)
    fila.enfileirar("2502027", str(tmp_path / "alvo.bin"))

    fila2 = d.Fila(caminho)                 # "reinício"
    assert fila2.tamanho() == 1
    item = fila2.itens[0]
    assert item["file_id"] == "2502027"
    assert item["destino"] == str(tmp_path / "alvo.bin")
    assert item["tentativas"] == 0
    assert item["estado"] == "pendente"
    assert item["proxima_em"] == 0 and item["proximo_retry"] == 0
    assert os.path.exists(caminho), "a fila deve estar gravada em disco"


def test_fila_vazia_quando_arquivo_nao_existe(tmp_path):
    fila = d.Fila(str(tmp_path / "nao_existe.json"))
    assert fila.tamanho() == 0
    assert fila.itens == []


def test_formato_legado_lista_e_falha_antiga_migram(tmp_path):
    caminho = tmp_path / "fila.json"
    caminho.write_text(json.dumps([
        {"file_id": "1", "destino": "/x/a", "tentativas": 5, "proximo_retry": 99, "estado": "falha", "erro": "e"},
        {"file_id": "1", "destino": "/x/a", "tentativas": 0, "proximo_retry": 0, "estado": "pendente"},
    ]), encoding="utf-8")
    fila = d.Fila(str(caminho))
    assert fila.tamanho() == 1, "duplicatas do formato antigo são fundidas por file_id"
    it = fila.itens[0]
    assert it["estado"] == "pendente" and it["tentativas"] == 0, "'falha' em minutos (D2) volta a pendente"
    assert isinstance(json.loads(caminho.read_text(encoding="utf-8")), dict)


def test_baixado_antigo_e_podado(tmp_path):
    caminho = tmp_path / "fila.json"
    velho = time.time() - 30 * 86400
    caminho.write_text(json.dumps({"itens": [
        {"file_id": "1", "estado": "baixado", "atualizado_em": velho},
        {"file_id": "2", "estado": "falha_permanente", "atualizado_em": velho}]}), encoding="utf-8")
    fila = d.Fila(str(caminho))
    fila.enfileirar("3", str(tmp_path / "c"))
    assert {i["file_id"] for i in d.Fila(str(caminho)).itens} == {"2", "3"}


# ---------------------------------------------------------------------------
# (b) retry entre ciclos
# ---------------------------------------------------------------------------
def test_falha_transitoria_agenda_em_minutos_e_registra_erro_mascarado(rede, tmp_path):
    rede.bitrix["7"] = [ConnectionResetError("Connection reset by peer")]
    fila = d.Fila(str(tmp_path / "fila.json"))
    fila.enfileirar("7", str(tmp_path / "a.bin"))
    agora = 1_000_000.0
    d.processar_fila(fila, WEBHOOK, None, agora_fn=lambda: agora)
    it = fila.itens[0]
    assert it["estado"] == "pendente" and it["tentativas"] == 1
    assert it["proxima_em"] >= agora + 300, "fila entre ciclos: >= 5 min (era 1 s)"
    assert it["ultimo_erro"]["classe"] == "REDE"
    assert "tok3n5ecret" not in json.dumps(it), "detalhe do erro sai mascarado"

    # antes da hora não reprocessa
    d.processar_fila(fila, WEBHOOK, None, agora_fn=lambda: agora + 10)
    assert fila.itens[0]["tentativas"] == 1


def test_transitorio_esgotado_vira_falha_permanente(rede, tmp_path):
    rede.bitrix["8"] = [ConnectionResetError("reset")]
    fila = d.Fila(str(tmp_path / "fila.json"))
    fila.enfileirar("8", str(tmp_path / "a.bin"))
    t = 1_000_000.0
    import rede_bitrix as rb
    for _ in range(20):
        rb.circuito("bitrix-download").fechar()     # o disjuntor usa relógio real; aqui o tempo é falso
        d.processar_fila(fila, WEBHOOK, None, agora_fn=lambda: t)
        if fila.itens[0]["estado"] != "pendente":
            break
        t = fila.itens[0]["proxima_em"] + 1
    assert fila.itens[0]["estado"] == "falha_permanente"
    assert 1 < fila.itens[0]["tentativas"] <= 8


# ---------------------------------------------------------------------------
# (c) ordem de fallback
# ---------------------------------------------------------------------------
def test_primaria_funciona_e_fallback_nao_e_chamado(rede, tmp_path):
    rede.arquivo(42, "doc.pdf", b"DADOS-PRIMARIA")
    rede.vibecode["42"] = [Resp(b"DADOS-FALLBACK", 200)]
    fila = d.Fila(str(tmp_path / "f.json"))
    destino = str(tmp_path / "alvo.bin")
    fila.enfileirar("42", destino)
    d.processar_fila(fila, WEBHOOK, "K")
    assert rede.n("vibe") == 0, "fallback não pode rodar quando a primária funciona"
    item = fila.itens[0]
    assert item["estado"] == "baixado" and item["fonte"] == "bitrix"
    assert open(destino, "rb").read() == b"DADOS-PRIMARIA"


def test_fallback_usado_quando_primaria_falha(rede, tmp_path):
    rede.bitrix["43"] = [ConnectionResetError("webhook fora")]
    rede.vibecode["43"] = [Resp(b"DADOS-FALLBACK", 200)]
    fila = d.Fila(str(tmp_path / "f.json"))
    destino = str(tmp_path / "alvo.bin")
    fila.enfileirar("43", destino)
    d.processar_fila(fila, WEBHOOK, "K")
    tipos = [t for t, _, _ in rede.chamadas]
    assert tipos == ["disk", "vibe"], "a ordem deve ser primária -> fallback"
    item = fila.itens[0]
    assert item["estado"] == "baixado" and item["fonte"] == "vibecode"
    assert open(destino, "rb").read() == b"DADOS-FALLBACK"
    assert fila.fontes["bitrix"]["ultima_falha"] is not None


def test_resumo_do_healthcheck(rede, tmp_path):
    rede.bitrix["1"] = [(403, '{"error":"ACCESS_DENIED"}')]
    rede.bitrix["2"] = [(404, '{"error":"ERROR_NOT_FOUND"}')]
    fila = d.Fila(str(tmp_path / "f.json"))
    fila.enfileirar("1", str(tmp_path / "a.bin"))
    fila.enfileirar("2", str(tmp_path / "b.bin"))
    d.processar_fila(fila, WEBHOOK, None)          # os dois: permanentes
    fila.enfileirar("3", str(tmp_path / "c.bin"))
    r = fila.resumo()
    assert r["tamanho"] == 3 and r["falhos"] == 2 and r["pendentes"] == 1
    assert d.codigo_saida(fila.ultima_rodada) == 1
