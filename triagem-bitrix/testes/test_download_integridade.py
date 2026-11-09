# -*- coding: utf-8 -*-
"""Frente D — integridade, fila e disjuntores do download_svc (sem rede: fakes_download).

Cada teste de DEFEITO falha no download_svc.py original (HEAD) — ver relatório
docs/remediacao/frentes/D-download.md (prova RED).
"""
import json
import os
import time

import pytest

from fakes_download import Resp, WEBHOOK, DORMIR_REAL  # fixture `rede`: conftest.py

import download_svc as d


def _fila(tmp_path):
    return d.Fila(str(tmp_path / "fila.json"))


def _sem_tmp(pasta):
    return [n for n in os.listdir(pasta) if n.endswith((".part", ".parcial", ".tmp"))]


def test_truncado_nao_e_aceito(rede, tmp_path):
    url = rede.arquivo(10, "rg.pdf", b"x" * 50, size=50)
    rede.urls[url] = [Resp(b"x" * 50, 200, {"Content-Length": "100"})]   # chegou metade
    fila = _fila(tmp_path)
    destino = str(tmp_path / "lote" / "01_rg.pdf")
    fila.enfileirar("10", destino)
    d.processar_fila(fila, WEBHOOK, "K")
    assert not os.path.exists(destino), "arquivo truncado não pode virar o destino"
    assert fila.itens[0]["estado"] != "baixado"
    assert _sem_tmp(str(tmp_path / "lote")) == []


def test_zero_byte_rejeitado(rede, tmp_path):
    rede.arquivo(11, "cnh.pdf", b"", size=0)
    fila = _fila(tmp_path)
    destino = str(tmp_path / "lote" / "02_cnh.pdf")
    fila.enfileirar("11", destino)
    d.processar_fila(fila, WEBHOOK, "K")
    assert not os.path.exists(destino)
    assert fila.itens[0]["estado"] != "baixado"


def test_tamanho_diferente_do_size_rejeitado(rede, tmp_path):
    url = rede.arquivo(12, "a.pdf", b"12345", size=9)
    rede.urls[url] = [Resp(b"12345", 200, {})]          # sem Content-Length
    fila = _fila(tmp_path)
    destino = str(tmp_path / "a.pdf")
    fila.enfileirar("12", destino)
    d.processar_fila(fila, WEBHOOK, "K")
    assert not os.path.exists(destino)
    assert fila.itens[0]["estado"] == "pendente"
    assert fila.itens[0]["ultimo_erro"]["classe"] == "INTEGRIDADE"


def test_teto_de_tamanho_nao_baixa(rede, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TETO_BYTES", 1000, raising=False)
    url = rede.arquivo(13, "video.pdf", b"x" * 10, size=5_000_000)
    fila = _fila(tmp_path)
    fila.enfileirar("13", str(tmp_path / "v.pdf"))
    d.processar_fila(fila, WEBHOOK, None)
    assert rede.n("get", url) == 0, "acima do teto não pode nem começar o download (S14)"
    assert fila.itens[0]["estado"] == "falha_permanente"


def test_sucesso_grava_sha256_tamanho_e_sem_tmp(rede, tmp_path):
    import hashlib
    corpo = b"%PDF-1.4 conteudo"
    rede.arquivo(14, "ok.pdf", corpo)
    fila = _fila(tmp_path)
    destino = str(tmp_path / "lote" / "01_ok.pdf")
    fila.enfileirar("14", destino)
    d.processar_fila(fila, WEBHOOK, None)
    it = fila.itens[0]
    assert it["estado"] == "baixado" and it["fonte"] == "bitrix"
    assert open(destino, "rb").read() == corpo
    assert it["sha256"] == hashlib.sha256(corpo).hexdigest() and it["tamanho"] == len(corpo)
    assert _sem_tmp(str(tmp_path / "lote")) == []


def test_disjuntor_bitrix_aberto_tenta_fallback(rede, tmp_path):
    import rede_bitrix as rb
    rede.arquivo(20, "doc.pdf", b"PRIMARIA")
    rede.vibecode["20"] = [Resp(b"VIBECODE", 200, {"Content-Length": "8"})]
    rb.circuito("bitrix-download").abrir("teste", duracao_s=600)
    fila = _fila(tmp_path)
    destino = str(tmp_path / "doc.pdf")
    fila.enfileirar("20", destino)
    d.processar_fila(fila, WEBHOOK, "CHAVE-VIBE")
    assert rede.n("disk") == 0, "com o disjuntor do Bitrix aberto não se chama disk.file.get"
    assert rede.n("vibe", "20") == 1, "o fallback VibeCode precisa ser tentado (B1)"
    assert fila.itens[0]["estado"] == "baixado" and open(destino, "rb").read() == b"VIBECODE"
    hdr = rede.chamadas[-1][2]
    assert {k.lower(): v for k, v in hdr.items()}.get("x-api-key") == "CHAVE-VIBE"


def test_access_denied_permanente_sem_retry(rede, tmp_path):
    rede.bitrix["30"] = [(403, '{"error":"ACCESS_DENIED","error_description":"negado"}')]
    fila = _fila(tmp_path)
    fila.enfileirar("30", str(tmp_path / "x.pdf"))
    d.processar_fila(fila, WEBHOOK, "K")              # VibeCode responde 404 (permanente)
    assert fila.itens[0]["estado"] == "falha_permanente"
    antes = len(rede.chamadas)
    futuro = time.time() + 30 * 86400
    d.processar_fila(fila, WEBHOOK, "K", agora_fn=lambda: futuro)
    assert len(rede.chamadas) == antes, "permanente nunca reentra"


def test_query_limit_para_rodada_e_agenda_no_futuro(rede, tmp_path):
    import rede_bitrix as rb
    rede.bitrix["40"] = [(503, '{"error":"QUERY_LIMIT_EXCEEDED"}')]
    rede.arquivo(41, "b.pdf", b"bbb")
    rede.vibecode["40"] = [Resp(b"v", 200)]
    fila = _fila(tmp_path)
    fila.enfileirar("40", str(tmp_path / "a.pdf"))
    fila.enfileirar("41", str(tmp_path / "b.pdf"))
    agora = time.time()
    d.processar_fila(fila, WEBHOOK, "K", agora_fn=lambda: agora)
    assert rede.n("disk") == 1, "QUERY_LIMIT: não martelar o próximo item"
    assert rede.n("vibe") == 0
    a, b = fila.itens
    assert a["estado"] == "pendente" and a["proxima_em"] >= agora + 290   # fila: 10 min c/ jitter (≥5)
    assert b["tentativas"] == 0
    assert not rb.circuito("bitrix-download").permitido(), "QUERY_LIMIT pausa a fonte para todos"


def test_url_expirada_pede_url_nova(rede, tmp_path):
    u1 = "https://teste.bitrix24.com.br/disk/download/?token=VELHO"
    u2 = "https://teste.bitrix24.com.br/disk/download/?token=NOVO"
    rede.bitrix["50"] = [{"NAME": "a.pdf", "SIZE": "3", "DOWNLOAD_URL": u1},
                         {"NAME": "a.pdf", "SIZE": "3", "DOWNLOAD_URL": u2}]
    rede.urls[u1] = [Resp(b"", 403)]
    rede.urls[u2] = [Resp(b"abc", 200, {"Content-Length": "3"})]
    fila = _fila(tmp_path)
    destino = str(tmp_path / "a.pdf")
    fila.enfileirar("50", destino)
    d.processar_fila(fila, WEBHOOK, "K")
    assert rede.n("get", u1) == 1 and rede.n("get", u2) == 1 and rede.n("disk") == 2
    assert fila.itens[0]["estado"] == "baixado" and open(destino, "rb").read() == b"abc"


def test_midia_pelo_nome_da_api_nao_e_baixada(rede, tmp_path):
    url = rede.arquivo(60, "audio do cliente.mp3", b"ID3....")
    fila = _fila(tmp_path)
    fila.enfileirar("60", str(tmp_path / "x"))
    d.processar_fila(fila, WEBHOOK, "K")
    assert rede.n("get", url) == 0
    assert fila.itens[0]["estado"] == "ignorado_midia"
    d.processar_fila(fila, WEBHOOK, "K", agora_fn=lambda: time.time() + 86400)
    assert rede.n("disk") == 1, "mídia não é retentada"


def test_id_telegram_nao_vai_ao_disk_file_get(rede, tmp_path):
    fila = _fila(tmp_path)
    fila.enfileirar("CQACAgEAAxkBAAIBY2Zteste", str(tmp_path / "audio.ogg.bin"))
    d.processar_fila(fila, WEBHOOK, "K")
    assert rede.chamadas == [], "id do Telegram nunca vai à rede"
    assert fila.itens[0]["estado"] == "telegram_legado"


def test_dois_itens_mesmo_destino_ficam_em_arquivos_distintos(rede, tmp_path):
    rede.arquivo(70, "CNH.pdf", b"PRIMEIRA")
    rede.arquivo(71, "CNH.pdf", b"SEGUNDA!")
    fila = _fila(tmp_path)
    destino = str(tmp_path / "lote" / "CNH.pdf")
    fila.enfileirar("70", destino)
    fila.enfileirar("71", destino)
    d.processar_fila(fila, WEBHOOK, None)
    conteudos = sorted(open(os.path.join(tmp_path, "lote", n), "rb").read()
                       for n in os.listdir(tmp_path / "lote"))
    assert conteudos == [b"PRIMEIRA", b"SEGUNDA!"], "o 2º anexo com o mesmo nome não pode apagar o 1º"


def test_dedupe_por_file_id(rede, tmp_path):
    fila = _fila(tmp_path)
    fila.enfileirar("80", str(tmp_path / "a"))
    fila.enfileirar("80", str(tmp_path / "a"))
    assert d.Fila(str(tmp_path / "fila.json")).tamanho() == 1


def test_fila_corrompida_nao_vira_vazia(tmp_path):
    caminho = tmp_path / "fila.json"
    caminho.write_text('{"itens": [{"file_id": "1"', encoding="utf-8")
    with pytest.raises(Exception) as exc:
        d.Fila(str(caminho))
    assert "Corrompido" in type(exc.value).__name__
    assert caminho.read_text(encoding="utf-8").startswith('{"itens"'), "não sobrescreve a fila"


def test_duas_instancias_nao_perdem_item(tmp_path):
    caminho = str(tmp_path / "fila.json")
    a, b = d.Fila(caminho), d.Fila(caminho)     # dois processos com snapshot antigo
    a.enfileirar("1", str(tmp_path / "1"))
    b.enfileirar("2", str(tmp_path / "2"))
    assert {i["file_id"] for i in d.Fila(caminho).itens} == {"1", "2"}


def test_sucesso_atualiza_estado_json_com_dono_download(rede, tmp_path):
    import estado_store
    estado = tmp_path / "estado.json"
    estado.write_text(json.dumps({"lotes": {"L1": {"file_ids": ["90"], "arquivos": [
        {"file_id": "90", "indice": 1, "baixado": False}]}}}), encoding="utf-8")
    rede.arquivo(90, "rg.pdf", b"RG")
    fila = _fila(tmp_path)
    fila.enfileirar("90", str(tmp_path / "lote" / "01_rg.pdf"))
    kw = {"caminho_estado": str(estado)} if "caminho_estado" in d.processar_fila.__code__.co_varnames else {}
    d.processar_fila(fila, WEBHOOK, None, **kw)
    meta = estado_store.ler_estado(str(estado))["lotes"]["L1"]["arquivos"][0]
    assert meta["baixado"] is True and meta["sha256"] and meta["caminho"].endswith("01_rg.pdf")


def test_guarda_de_conexao_tem_prazo_duro(rede, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TETO_CONEXAO_S", 0.2, raising=False)
    url = rede.arquivo(100, "a.pdf", b"abc")

    def pendurado():
        DORMIR_REAL(3)                       # getaddrinfo pendurado
        return Resp(b"abc", 200, {"Content-Length": "3"})
    rede.urls[url] = [pendurado]
    fila = _fila(tmp_path)
    fila.enfileirar("100", str(tmp_path / "a.pdf"))
    t0 = time.time()
    d.processar_fila(fila, WEBHOOK, None)
    assert time.time() - t0 < 1.5, "abertura de conexão precisa de prazo duro (G1)"
    assert fila.itens[0]["estado"] == "pendente"


def test_prazo_total_por_arquivo_corta_gotejamento(rede, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TETO_ITEM_S", 0.4, raising=False)
    url = rede.arquivo(101, "a.pdf", b"x" * 60)
    rede.urls[url] = [Resp(b"x" * 60, 200, {"Content-Length": "60"}, gotejar_s=0.05)]
    fila = _fila(tmp_path)
    destino = str(tmp_path / "a.pdf")
    fila.enfileirar("101", destino)
    t0 = time.time()
    d.processar_fila(fila, WEBHOOK, None)
    assert time.time() - t0 < 2.0
    assert not os.path.exists(destino) and _sem_tmp(str(tmp_path)) == []


def test_rodada_respeita_limite_de_itens_e_teto_de_tempo(rede, tmp_path):
    fila = _fila(tmp_path)
    for i in range(5):
        rede.arquivo(200 + i, f"{i}.pdf", b"abc")
        fila.enfileirar(str(200 + i), str(tmp_path / f"{i}.pdf"))
    proc = d.processar_fila(fila, WEBHOOK, None, limite_itens=2)
    assert len(proc) == 2 and fila.ultima_rodada["parar"].startswith("limite")
    proc = d.processar_fila(fila, WEBHOOK, None, teto_rodada_s=0.5)
    assert len(proc) == 0 and "teto" in fila.ultima_rodada["parar"]
