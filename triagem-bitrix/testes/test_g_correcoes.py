"""C4 — correções dos defeitos achados pelo cenário integral (G-01, G-02, G-03).

Testes de unidade de cada causa (o cenário ponta a ponta fica em test_cenario_integral.py).
Tudo offline e sintético.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import download_svc as ds
import estado_store
import ponte_criterio as pc
import rede_bitrix as rb
import triagem_grupo as tg


# ------------------------------------------------------------------ G-01
def test_g01_parse_data_aceita_datetime_aware():
    agora = dt.datetime.now(dt.timezone.utc)
    assert tg._parse_data(agora) is agora


def test_g01_parse_data_aceita_datetime_ingenuo_e_texto():
    ing = dt.datetime(2026, 11, 9, 12, 0, 0)
    assert tg._parse_data(ing) is ing
    assert tg._parse_data("2026-11-09T12:00:00Z") == dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
    assert tg._parse_data(None) is None and tg._parse_data("lixo") is None and tg._parse_data(123) is None


# ------------------------------------------------------------------ G-02 (ponte)
def test_g02_ponte_trata_erro_tipo_midia_como_midia():
    # o que o download_svc gravava (leitura pelo webhook: sem tipo e sem nome)
    meta = {"file_id": "9", "baixado": False, "erro": "áudio", "erro_tipo": "midia"}
    assert pc.tipo_arquivo(meta) == "midia"
    # tipo provisório 'desconhecido' não esconde a marcação de mídia do download
    assert pc.tipo_arquivo(dict(meta, tipo="desconhecido")) == "midia"
    # tipo explícito de documento continua mandando; erro de download comum não vira mídia
    assert pc.tipo_arquivo(dict(meta, tipo="documento")) == "documento"
    assert pc.tipo_arquivo({"file_id": "9", "erro_tipo": "transitorio"}) == "desconhecido"


def test_g02_lote_com_audio_marcado_pelo_download_nao_fica_pendente():
    lote = {"file_ids": ["1", "2"],
            "arquivos": [{"file_id": "1", "baixado": True, "sha256": "a" * 64, "tipo": "documento"},
                         {"file_id": "2", "baixado": False, "erro_tipo": "midia"}]}
    view = pc.lote_documental(lote)
    assert [a["file_id"] for a in view["arquivos"]] == ["1"]
    assert "2" not in [str(f) for f in view.get("file_ids") or []]


# ------------------------------------------------------------------ G-02 (download_svc)
def _estado_com_lote(tmp_path):
    arq = tmp_path / "estado.json"
    arq.write_text(json.dumps({"lotes": {"L1": {
        "lote_id": "L1", "file_ids": ["77"],
        "arquivos": [{"file_id": "77", "indice": 1, "baixado": False, "erro": "adiado"}]}}}))
    return str(arq)


def test_g02_download_marca_midia_com_tipo_e_nome_no_estado(tmp_path, monkeypatch):
    arq = _estado_com_lote(tmp_path)
    fila = ds.Fila(str(tmp_path / "fila.json"))
    item = {"file_id": "77", "lote_id": "L1", "indice": 1, "estado": ds.PENDENTE}
    monkeypatch.setattr(ds, "baixar_item", lambda *a, **k: {"resultado": "midia", "nome": "audio_cliente.ogg"})
    rodada: dict = {}
    ds.processar_item(fila, item, rodada=rodada)
    patch = rodada["estado_json"][0]
    assert patch["erro_tipo"] == "midia" and patch["tipo"] == "midia"
    assert patch["nome_original"] == "audio_cliente.ogg"
    ds._aplicar_no_estado(rodada["estado_json"], {"77": item}, arq)
    a = estado_store.ler_estado(arq)["lotes"]["L1"]["arquivos"][0]
    assert (a["tipo"], a["erro_tipo"], a["nome_original"]) == ("midia", "midia", "audio_cliente.ogg")
    assert pc.tipo_arquivo(a) == "midia"


def test_g02_download_nao_sobrescreve_tipo_da_triagem(tmp_path, monkeypatch):
    """`tipo` é da triagem: o download só preenche se vazio (merge por campo)."""
    arq = tmp_path / "estado.json"
    arq.write_text(json.dumps({"lotes": {"L1": {"lote_id": "L1", "file_ids": ["77"], "arquivos": [
        {"file_id": "77", "indice": 1, "tipo": "documento", "nome_original": "x.pdf"}]}}}))
    item = {"file_id": "77", "lote_id": "L1", "indice": 1}
    monkeypatch.setattr(ds, "baixar_item", lambda *a, **k: {"resultado": "midia", "nome": "a.ogg"})
    rodada: dict = {}
    ds.processar_item(ds.Fila(str(tmp_path / "f.json")), item, rodada=rodada)
    ds._aplicar_no_estado(rodada["estado_json"], {"77": item}, str(arq))
    a = estado_store.ler_estado(str(arq))["lotes"]["L1"]["arquivos"][0]
    assert (a["tipo"], a["nome_original"]) == ("documento", "x.pdf")


# ------------------------------------------------------------------ G-03
@pytest.fixture
def ciclo_dl(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "CIRCUIT_ARQUIVO", str(tmp_path / "circuit-download.json"))
    monkeypatch.setitem(tg._MIGRACAO_CIRCUITO, "feita", True)
    retry = []
    monkeypatch.setattr(tg, "_enfileirar_retry", lambda fid, *a, **k: retry.append(str(fid)))
    return tmp_path, retry


def _falha(status, corpo):
    def _get(_fid):
        raise rb.FalhaBitrix(rb.classificar(status, corpo))
    return _get


def test_g03_access_denied_no_ciclo_e_permanente_e_nao_vai_ao_retry(ciclo_dl, monkeypatch):
    tmp, retry = ciclo_dl
    monkeypatch.setattr(tg, "_disk_file_get", _falha(403, '{"error":"ACCESS_DENIED","error_description":"x"}'))
    m = tg.baixar_file_id_api(None, "502", str(tmp), 1, "L1", webhook="https://wh.invalid/rest/1/x/")
    assert m["baixado"] is False
    assert m["erro_tipo"] == tg.ERRO_PERMANENTE, m
    assert retry == []
    # e o ciclo seguinte não martela o mesmo arquivo (erro definitivo reaproveitado)
    monkeypatch.setattr(tg, "_disk_file_get", lambda _f: pytest.fail("repetiu ACCESS_DENIED"))
    m2 = tg.baixar_file_id_api(None, "502", str(tmp), 1, "L1", webhook="https://wh.invalid/rest/1/x/",
                               anterior=m)
    assert m2["erro_tipo"] == tg.ERRO_PERMANENTE


def test_g03_transitorio_continua_indo_ao_retry(ciclo_dl, monkeypatch):
    tmp, retry = ciclo_dl
    monkeypatch.setattr(tg, "_disk_file_get",
                        _falha(503, '{"error":"QUERY_LIMIT_EXCEEDED","error_description":"x"}'))
    m = tg.baixar_file_id_api(None, "402", str(tmp), 1, "L1", webhook="https://wh.invalid/rest/1/x/")
    assert m["erro_tipo"] == tg.ERRO_TRANSITORIO
    assert retry == ["402"]


@pytest.mark.parametrize("status,corpo", [
    (401, '{"error":"expired_token","error_description":"x"}'),
    (503, '{"error":"OVERLOAD_LIMIT","error_description":"x"}'),
])
def test_g03_erro_de_sistema_nao_marca_arquivo_permanente(ciclo_dl, monkeypatch, status, corpo):
    """Credencial caída/portal bloqueado não é culpa do arquivo: fica transitório (pausa_rodada)."""
    tmp, retry = ciclo_dl
    monkeypatch.setattr(tg, "_disk_file_get", _falha(status, corpo))
    m = tg.baixar_file_id_api(None, "602", str(tmp), 1, "L1", webhook="https://wh.invalid/rest/1/x/")
    assert m["erro_tipo"] != tg.ERRO_PERMANENTE, m


# ------------------------------------------------------------------ cpfs_invalidos
def test_cpfs_invalidos_tem_dono_declarado():
    assert estado_store.PROPRIEDADE_CAMPOS.get("cpfs_invalidos") == "triagem"
