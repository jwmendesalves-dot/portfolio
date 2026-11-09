# -*- coding: utf-8 -*-
"""Frente D — baixar_pendentes como planejador da fila (sem rede: fakes_download).

Roda tanto contra o código novo (main(argv)) quanto contra o original (main() lendo
sys.argv/ESTADO/WEBHOOK_CONF) para a prova RED do relatório.
"""
import inspect
import json
import os
import sys
import time

from fakes_download import Resp, WEBHOOK  # fixture `rede`: conftest.py

import baixar_pendentes as bp

VELHO = time.time() - 7200


def _estado(tmp_path, lotes):
    cam = tmp_path / "estado.json"
    cam.write_text(json.dumps({"lotes": lotes}), encoding="utf-8")
    return str(cam)


def _lote(tmp_path, fids, arquivos=None, nome="L1"):
    pasta = str(tmp_path / "lotes" / nome)
    os.makedirs(pasta, exist_ok=True)
    return pasta, {nome: {"file_ids": fids, "pasta": pasta, "arquivos": arquivos or []}}


def _rodar(monkeypatch, tmp_path, estado, extra=()):
    fila = str(tmp_path / "fila.json")
    if "argv" in inspect.signature(bp.main).parameters:
        return bp.main(["--estado", estado, "--fila", fila, *extra])
    conf = tmp_path / "leads.json"                 # código original
    conf.write_text(json.dumps({"webhook": WEBHOOK}), encoding="utf-8")
    monkeypatch.setattr(bp, "ESTADO", estado, raising=False)
    monkeypatch.setattr(bp, "WEBHOOK_CONF", str(conf), raising=False)
    monkeypatch.setattr(sys, "argv", ["baixar_pendentes.py", *extra])
    return bp.main()


def _fila_itens(tmp_path):
    cam = tmp_path / "fila.json"
    return json.loads(cam.read_text(encoding="utf-8"))["itens"] if cam.exists() else []


def test_parcial_orfao_nao_bloqueia_o_indice(rede, tmp_path, monkeypatch):
    pasta, lotes = _lote(tmp_path, ["10"])
    orfao = os.path.join(pasta, "01_rg.pdf.parcial")
    open(orfao, "wb").write(b"meio")
    os.utime(orfao, (VELHO, VELHO))
    rede.arquivo(10, "rg.pdf", b"RG-COMPLETO")
    _rodar(monkeypatch, tmp_path, _estado(tmp_path, lotes))
    assert open(os.path.join(pasta, "01_rg.pdf"), "rb").read() == b"RG-COMPLETO"
    assert not os.path.exists(orfao), "órfão velho é limpo"


def test_arquivo_de_zero_byte_nao_conta_como_baixado(rede, tmp_path, monkeypatch):
    pasta, lotes = _lote(tmp_path, ["11"])
    vazio = os.path.join(pasta, "01_cnh.pdf")
    open(vazio, "wb").close()
    os.utime(vazio, (VELHO, VELHO))
    rede.arquivo(11, "cnh.pdf", b"CNH")
    _rodar(monkeypatch, tmp_path, _estado(tmp_path, lotes))
    assert open(vazio, "rb").read() == b"CNH"


def test_id_telegram_nunca_vai_ao_disk_file_get(rede, tmp_path, monkeypatch):
    pasta, lotes = _lote(tmp_path, ["CQACAgEAAxkBAAIBteste"])
    estado = _estado(tmp_path, lotes)
    _rodar(monkeypatch, tmp_path, estado)
    assert rede.n("disk") == 0
    itens = _fila_itens(tmp_path)
    assert itens and itens[0]["estado"] == "telegram_legado"
    meta = json.load(open(estado))["lotes"]["L1"]["arquivos"][0]
    assert meta["erro_tipo"] == "telegram_legado"


def test_midia_nao_e_baixada_nem_retentada(rede, tmp_path, monkeypatch):
    pasta, lotes = _lote(tmp_path, ["12", "13"], arquivos=[
        {"file_id": "12", "indice": 1, "nome_original": "audio cliente.mp3", "baixado": False}])
    url13 = rede.arquivo(13, "video.mp4", b"....")
    rede.arquivo(12, "audio cliente.mp3", b"ID3")
    estado = _estado(tmp_path, lotes)
    _rodar(monkeypatch, tmp_path, estado)
    assert rede.n("disk", "12") == 0, "nome já conhecido como mídia: nem disk.file.get"
    assert rede.n("get", url13) == 0, "mídia revelada pelo NAME: não baixa"
    estados = {i["file_id"]: i["estado"] for i in _fila_itens(tmp_path)}
    assert estados == {"12": "ignorado_midia", "13": "ignorado_midia"}
    antes = len(rede.chamadas)
    _rodar(monkeypatch, tmp_path, estado)
    assert len(rede.chamadas) == antes


def test_query_limit_para_a_rodada(rede, tmp_path, monkeypatch):
    pasta, lotes = _lote(tmp_path, ["20", "21", "22"])
    for f in ("20", "21", "22"):
        rede.bitrix[f] = [(503, '{"error":"QUERY_LIMIT_EXCEEDED"}')]
    agora = time.time()
    _rodar(monkeypatch, tmp_path, _estado(tmp_path, lotes))
    assert rede.n("disk") == 1, "1º QUERY_LIMIT para a rodada (não 3 arquivos × 3 tentativas)"
    item = next(i for i in _fila_itens(tmp_path) if i["tentativas"])
    assert item["proxima_em"] > agora + 60


def test_disjuntor_aberto_usa_fallback_vibecode(rede, tmp_path, monkeypatch):
    import rede_bitrix as rb
    monkeypatch.setenv("TRIAGEM_VIBECODE_KEY", "CHAVE-VIBE")
    pasta, lotes = _lote(tmp_path, ["30"], arquivos=[{"file_id": "30", "indice": 1, "nome_original": "rg.pdf"}])
    rede.arquivo(30, "rg.pdf", b"X")
    rede.vibecode["30"] = [Resp(b"DO-VIBECODE", 200)]
    rb.circuito("bitrix-download").abrir("teste", duracao_s=600)
    _rodar(monkeypatch, tmp_path, _estado(tmp_path, lotes))
    assert rede.n("disk") == 0
    assert open(os.path.join(pasta, "01_rg.pdf"), "rb").read() == b"DO-VIBECODE"


def test_access_denied_permanente_codigo_de_saida_e_sem_retry(rede, tmp_path, monkeypatch):
    pasta, lotes = _lote(tmp_path, ["40"])
    rede.bitrix["40"] = [(403, '{"error":"ACCESS_DENIED"}')]
    estado = _estado(tmp_path, lotes)
    rc = _rodar(monkeypatch, tmp_path, estado)
    assert rc == 1, "falha permanente nova exige atenção (saída != 0)"
    n = rede.n("disk")
    assert n == 1, "ACCESS_DENIED não é repetido"
    assert _rodar(monkeypatch, tmp_path, estado) == 0
    assert rede.n("disk") == n
    assert json.load(open(estado))["lotes"]["L1"]["arquivos"][0]["erro_tipo"] == "permanente"


def test_sucesso_atualiza_estado_com_dono_download(rede, tmp_path, monkeypatch):
    pasta, lotes = _lote(tmp_path, ["50"], arquivos=[{"file_id": "50", "indice": 1, "baixado": False}])
    rede.arquivo(50, "rg.pdf", b"RG")
    estado = _estado(tmp_path, lotes)
    assert _rodar(monkeypatch, tmp_path, estado) == 0
    meta = json.load(open(estado))["lotes"]["L1"]["arquivos"][0]
    assert meta["baixado"] is True and meta["sha256"] and meta["caminho"].endswith("01_rg.pdf")


def test_dois_anexos_com_mesmo_nome_viram_arquivos_distintos(rede, tmp_path, monkeypatch):
    """Regressão (o original já prefixava NN_; o defeito E3 está no download_svc/ciclo)."""
    pasta, lotes = _lote(tmp_path, ["60", "61"])
    rede.arquivo(60, "CNH.pdf", b"FRENTE")
    rede.arquivo(61, "CNH.pdf", b"VERSO!")
    _rodar(monkeypatch, tmp_path, _estado(tmp_path, lotes))
    assert open(os.path.join(pasta, "01_CNH.pdf"), "rb").read() == b"FRENTE"
    assert open(os.path.join(pasta, "02_CNH.pdf"), "rb").read() == b"VERSO!"


def test_parcial_recente_nao_e_apagado(rede, tmp_path, monkeypatch):
    """Um download em andamento (temporário recente) não é apagado por outro processo."""
    pasta, lotes = _lote(tmp_path, ["70"])
    recente = os.path.join(pasta, "01_rg.pdf.parcial")
    open(recente, "wb").write(b"meio")
    rede.arquivo(70, "rg.pdf", b"RG")
    _rodar(monkeypatch, tmp_path, _estado(tmp_path, lotes))
    assert os.path.exists(recente)


def test_checar_midias_usa_tipos_documento():
    import checar_midias as cm
    assert cm.e_midia_anexo({"type": "file", "name": "gravacao.MP3"})
    assert cm.e_midia_anexo({"type": "audio", "name": "x"})
    assert not cm.e_midia_anexo({"type": "file", "name": "rg.pdf"})
    estado = {"lotes": {"L": {"file_ids": ["1", "2"], "arquivos": [
        {"file_id": "1", "nome_original": "a.mp4", "baixado": False},
        {"file_id": "2", "nome_original": "rg.pdf", "baixado": True}]}}}
    r = cm.resumo_estado(estado, [{"file_id": "1", "estado": "ignorado_midia"}])
    assert r["midia_no_lote"] == 1 and r["midia_pendente_sem_marca"] == 0 and r["fila_ignorado_midia"] == 1
