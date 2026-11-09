# -*- coding: utf-8 -*-
"""Frente C1 (onda 2) — persistência do ciclo da triagem (A3, F1-03, F1-08, S01).

Sem rede: leitura, agrupamento e config são substituídos; o ciclo real grava num
estado.json temporário. CPFs sintéticos (cpf_util.gerar_para_teste), nomes fictícios.
"""
import argparse
import datetime as dt
import json

import pytest

import cpf_util
import estado_store
import triagem_grupo as tg

SENHA = "Zq7senhaFicticia!"


def lote_falso(lid, texto="= MARIA FICTICIA\nobs", status="FECHADO_PRONTO", inicio=None,
               file_ids=()):
    ini = inicio or dt.datetime.now(dt.timezone.utc)
    return {"lote_id": lid, "modo": "marcadores", "status": status, "pronto": False,
            "pronto_por": None, "requer_revisao": True, "identificacao": "SEM_CPF",
            "confianca": "baixa", "motivos_revisao": ["SEM_CPF: nenhum CPF no lote"],
            "cpf": None, "cpfs_distintos": [], "nome": None, "nomes": [], "autores": ["Ana"],
            "inicio": ini, "fim": ini, "fechado": True, "aberto": False,
            "qtd_arquivos": len(file_ids), "file_ids": list(file_ids),
            "mensagens": [{"id": 10, "autor": "Ana", "data": ini.isoformat(), "texto": texto}]}


@pytest.fixture
def estado_tmp(tmp_path, monkeypatch):
    arq = str(tmp_path / "estado.json")
    monkeypatch.setattr(tg, "ARQ_ESTADO", arq)
    monkeypatch.setattr(estado_store, "ARQ_ESTADO", arq)   # o código antigo gravava no padrão do store
    monkeypatch.setattr(tg, "DIR_LOTES", str(tmp_path / "lotes"))
    monkeypatch.setattr(tg, "CIRCUIT_ARQUIVO", str(tmp_path / "circuit-download.json"), raising=False)
    return arq


@pytest.fixture
def ciclo_sem_rede(monkeypatch, estado_tmp):
    """Substitui tudo que sairia para a rede; devolve a lista mutável de lotes do agrupador."""
    lotes = []
    monkeypatch.setattr(tg, "ler_chave_bot", lambda: "chave-teste")
    monkeypatch.setattr(tg, "ler_eventos", lambda chave, cursor: ([], cursor))
    monkeypatch.setattr(tg, "carregar_config", lambda: dict(tg.CONFIG_PADRAO))
    monkeypatch.setattr(tg, "ler_bot_config", lambda: {})
    monkeypatch.setattr(tg, "_ler_mensagens_com_breaker",
                        lambda *a, **k: ([{"id": 10, "texto": "x", "ts": None}], "fake"))
    monkeypatch.setattr(tg, "agrupar", lambda *a, **k: [dict(l) for l in lotes])

    def sem_webhook():
        raise RuntimeError("sem webhook no teste")

    monkeypatch.setattr(tg, "descobrir_webhook", sem_webhook)
    monkeypatch.setattr(tg, "download_svc", None)
    return lotes


def _args():
    return argparse.Namespace(dry_run=False, fonte_webhook=False, dialog_id="chat1", paginas=None,
                              desde_msg_id=None, janela_min=None, force=False)


def _ler(arq):
    with open(arq, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- F1-08
def test_carregar_estado_corrompido_aborta_sem_mexer_no_arquivo(estado_tmp):
    with open(estado_tmp, "w", encoding="utf-8") as fh:
        fh.write('{"lotes": {"L1": {"cpf": "1')          # truncado no meio
    antes = open(estado_tmp, "rb").read()
    with pytest.raises(estado_store.EstadoCorrompido):
        tg.carregar_estado()
    assert open(estado_tmp, "rb").read() == antes, "o original não pode ser renomeado/zerado"


def test_carregar_estado_ausente_devolve_vazio(estado_tmp):
    est = tg.carregar_estado()
    assert est["lotes"] == {} and est["cursor_eventos"] == 0


def test_ciclo_com_estado_corrompido_nao_comeca_do_zero(ciclo_sem_rede, estado_tmp):
    ciclo_sem_rede.append(lote_falso("LOTE-20261109-1000-aaaa0001"))
    with open(estado_tmp, "w", encoding="utf-8") as fh:
        fh.write("{nao e json")
    antes = open(estado_tmp, "rb").read()
    with pytest.raises(estado_store.EstadoCorrompido):
        tg.ciclo(_args())
    assert open(estado_tmp, "rb").read() == antes


def test_salvar_estado_morto_foi_removido():
    assert not hasattr(tg, "salvar_estado")


# ---------------------------------------------------------------- A3 / F1-03
def test_cpf_da_identificacao_e_marca_da_ponte_sobrevivem_ao_ciclo(ciclo_sem_rede, estado_tmp):
    lid = "LOTE-20261109-1000-aaaa0002"
    ciclo_sem_rede.append(lote_falso(lid))
    tg.ciclo(_args())
    assert _ler(estado_tmp)["lotes"][lid]["cpf"] is None

    cpf = cpf_util.gerar_para_teste(7)
    estado_store.atualizar_lotes({lid: {"cpf": cpf, "cpf_formatado": cpf_util.formatar(cpf),
                                        "identificacao": "OK", "cpf_origem": "bitrix-nome",
                                        "nome": "MARIA FICTICIA", "nome_origem": "bitrix-nome"}},
                                 "identificacao", caminho=estado_tmp)
    estado_store.atualizar_lotes({lid: {"status": "descartado_duplicata", "lote_origem": "LOTE-OUTRO",
                                        "descartado_em": "2026-11-09T10:00:00+00:00",
                                        "descarte_motivo": "mesmo sha256"}},
                                 "ponte", caminho=estado_tmp)

    tg.ciclo(_args())                                       # ciclo seguinte, lote ainda na janela
    reg = _ler(estado_tmp)["lotes"][lid]
    assert reg["cpf"] == cpf, "o ciclo apagou o CPF completado pela identificação (A3)"
    assert reg["identificacao"] == "OK" and reg["nome"] == "MARIA FICTICIA"
    assert reg["status"] == "descartado_duplicata", "o ciclo desfez o descarte da ponte (F1-03)"
    assert reg["lote_origem"] == "LOTE-OUTRO" and reg["descarte_motivo"] == "mesmo sha256"
    assert reg["mensagens"] and reg["visto_em"]           # campos da triagem continuam atualizados


def test_download_com_sha256_sobrevive_ao_ciclo(ciclo_sem_rede, estado_tmp, tmp_path):
    """Lote antigo (fora da janela de download): o ciclo não rebaixa o que o download gravou."""
    lid = "LOTE-20261001-1000-aaaa0003"
    velho = dt.datetime(2026, 10, 1, 10, tzinfo=dt.timezone.utc)
    ciclo_sem_rede.append(lote_falso(lid, inicio=velho, file_ids=["555"]))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 conteudo")
    estado_store.atualizar_lotes({lid: dict(tg.serializar_lote(lote_falso(lid, inicio=velho,
                                                                            file_ids=["555"])),
                                            arquivos=[{"file_id": "555", "indice": 1}])},
                                 "triagem", caminho=estado_tmp)
    estado_store.atualizar_lotes({lid: {"arquivos": [{"file_id": "555", "indice": 1, "baixado": True,
                                                      "sha256": "ab" * 32, "caminho": str(pdf),
                                                      "tamanho_bits": pdf.stat().st_size}]}},
                                 "download", caminho=estado_tmp)
    tg.ciclo(_args())
    meta = _ler(estado_tmp)["lotes"][lid]["arquivos"][0]
    assert meta["baixado"] is True and meta["sha256"] == "ab" * 32 and meta["caminho"] == str(pdf)


def test_varredura_fecha_lote_fora_da_janela_sem_regravar_snapshot_velho(ciclo_sem_rede, estado_tmp,
                                                                         monkeypatch):
    """O fechamento por timeout de lote que já saiu da janela grava SÓ o fechamento.

    Simula a identificação gravando o CPF no MEIO do ciclo (depois da leitura do estado):
    o código antigo regravava o registro inteiro lido no início e apagava o CPF.
    """
    lid = "LOTE-20261101-0800-aaaa0004"
    velho = dt.datetime(2026, 11, 1, 8, tzinfo=dt.timezone.utc)
    estado_store.atualizar_lotes({lid: tg.serializar_lote(lote_falso(lid, status="ABERTO", inicio=velho))},
                                 "triagem", caminho=estado_tmp)
    cpf = cpf_util.gerar_para_teste(11)

    def agrupar_com_concorrente(*a, **k):
        estado_store.atualizar_lotes({lid: {"cpf": cpf, "identificacao": "OK", "cpf_origem": "bitrix-nome"}},
                                     "identificacao", caminho=estado_tmp)
        return []

    monkeypatch.setattr(tg, "agrupar", agrupar_com_concorrente)
    resumo = tg.ciclo(_args())
    assert lid in resumo.get("lotes_vencidos", [])
    reg = _ler(estado_tmp)["lotes"][lid]
    assert reg["status"] == "FECHADO_POR_TIMEOUT" or reg["status"].startswith("FECHADO")
    assert reg["cpf"] == cpf, "a varredura regravou o snapshot velho e apagou o CPF"


# ---------------------------------------------------------------- S01
def test_ciclo_nao_grava_senha_no_estado(ciclo_sem_rede, estado_tmp):
    lid = "LOTE-20261109-1000-aaaa0005"
    ciclo_sem_rede.append(lote_falso(lid, texto=f"= MARIA FICTICIA\n=SENHA: {SENHA}\nfim"))
    tg.ciclo(_args())
    bruto = open(estado_tmp, encoding="utf-8").read()
    assert SENHA not in bruto, "a senha gov.br foi persistida no estado.json (S01)"
    texto = _ler(estado_tmp)["lotes"][lid]["mensagens"][0]["texto"]
    assert "[senha removida]" in texto and "MARIA FICTICIA" in texto
