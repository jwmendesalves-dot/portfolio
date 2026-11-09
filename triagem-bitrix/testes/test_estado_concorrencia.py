# -*- coding: utf-8 -*-
"""
Concorrência REAL entre processos (multiprocessing, spawn) sobre o estado.json.

Reproduz os cenários da auditoria (03-download-estado A1/A2/A3, 02-fase1 F1-03/F1-08):
  · dois escritores em lotes diferentes — nenhum lote some;
  · dois escritores em campos distintos do MESMO lote — nenhum perde;
  · JSON inválido — ninguém grava estado vazio por cima;
  · trava ocupada por outro processo — TimeoutError (não grava sem trava);
  · CPF da 'identificacao' sobrevive à regravação do lote inteiro pela 'triagem'
    feita a partir de uma leitura VELHA (o ciclo lê no início e grava no fim);
  · marca de duplicata da 'ponte' sobrevive à reconciliação de download.
"""
import json
import multiprocessing as mp
import os
import sys
import time

import pytest

from conftest import RAIZ

sys.path.insert(0, RAIZ)

from estado_store import (EstadoCorrompido, atualizar_estado, atualizar_lotes,  # noqa: E402
                          gravar_atomico, ler_estado)

CTX = mp.get_context("spawn")

def _cpf_sintetico(semente: int) -> str:
    """CPF de TESTE (DV calculado), nunca de pessoa real."""
    base = f"{(semente * 7919 + 123456789) % 10 ** 9:09d}"
    d = [int(c) for c in base]
    for n in (9, 10):
        d.append((sum(d[i] * (n + 1 - i) for i in range(n)) * 10) % 11 % 10)
    return "".join(map(str, d))


CPF_T = _cpf_sintetico(42)
CPF_T_FMT = f"{CPF_T[:3]}.{CPF_T[3:6]}.{CPF_T[6:9]}-{CPF_T[9:]}"

N = 15


# ---------------------------------------------------------------------------
# trabalhadores (nível de módulo: o spawn reimporta este arquivo no filho)
# ---------------------------------------------------------------------------
def _w_lotes_proprios(caminho, prefixo, n, inicio):
    sys.path.insert(0, RAIZ)
    from estado_store import atualizar_estado
    inicio.wait(10)
    for i in range(n):
        def mut(e, i=i):
            e.setdefault("lotes", {})[f"{prefixo}{i}"] = {"lote_id": f"{prefixo}{i}"}
            time.sleep(0.002)
        atualizar_estado(mut, caminho=caminho)


def _w_identificacao(caminho, n, inicio):
    sys.path.insert(0, RAIZ)
    from estado_store import atualizar_estado, atualizar_lotes
    inicio.wait(10)
    for i in range(n):
        atualizar_lotes({"L1": {"cpf": CPF_T, "identificacao": "OK",
                                "cpf_origem": "bitrix-nome", "cpf_sugerido": str(i)}},
                        "identificacao", caminho=caminho)
        # contador acumulado (ler-modificar-gravar): perda de escrita apareceria aqui
        atualizar_estado(lambda e, i=i: e["lotes"]["L1"].setdefault("ids_vistos", []).append(i),
                         caminho=caminho)


def _w_download(caminho, n, inicio):
    sys.path.insert(0, RAIZ)
    from estado_store import atualizar_estado, atualizar_lotes
    inicio.wait(10)

    def soma_tentativa(e):
        meta = e["lotes"]["L1"]["arquivos"][0]
        meta["tentativas"] = int(meta.get("tentativas") or 0) + 1

    for i in range(n):
        atualizar_lotes({"L1": {"arquivos": [{"file_id": "10", "indice": 1, "baixado": True,
                                              "sha256": "ab" * 32, "tamanho_bits": 10 + i,
                                              "erro": None}]}},
                        "download", caminho=caminho)
        atualizar_estado(soma_tentativa, caminho=caminho)


def _w_segura_trava(caminho, segurando, soltar):
    sys.path.insert(0, RAIZ)
    from estado_store import trava_arquivo
    with trava_arquivo(caminho):
        segurando.set()
        soltar.wait(20)


def _w_ciclo_triagem_lento(caminho, leu, pode_gravar):
    """Simula o ciclo: lê o estado no INÍCIO, demora, e regrava o lote inteiro."""
    sys.path.insert(0, RAIZ)
    from estado_store import atualizar_lotes, ler_estado
    velho = ler_estado(caminho)["lotes"]["L1"]          # leitura do início do ciclo
    leu.set()
    pode_gravar.wait(20)
    reg = dict(velho)                                   # serializar_lote: dict novo, sem CPF
    reg["mensagens"] = [{"id": 1}, {"id": 2}]
    reg["cpf"] = None
    reg["identificacao"] = "SEM_CPF"
    reg["pronto"] = False
    reg["requer_revisao"] = True
    reg["motivos_revisao"] = ["SEM_CPF: nenhum CPF identificado no lote"]
    atualizar_lotes({"L1": reg}, "triagem", caminho=caminho)


def _w_reconciliar(caminho, leu, pode_gravar):
    """Reconciliador de download: calcula FORA da trava e aplica só as diferenças."""
    sys.path.insert(0, RAIZ)
    from estado_store import atualizar_lotes, ler_estado
    snapshot = ler_estado(caminho)
    leu.set()
    pode_gravar.wait(20)
    mudancas = {}
    for lid, lote in snapshot["lotes"].items():
        metas = [{"file_id": m["file_id"], "indice": m["indice"], "baixado": True,
                  "sha256": "cd" * 32, "tamanho_bits": 5, "erro": None,
                  "fonte": "reconciliado do disco"} for m in lote.get("arquivos") or []]
        mudancas[lid] = {"arquivos": metas}
    atualizar_lotes(mudancas, "download", caminho=caminho)


def _rodar(*procs):
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    for p in procs:
        assert p.exitcode == 0, f"processo {p.name} saiu com {p.exitcode}"


@pytest.fixture
def arq(tmp_path):
    return str(tmp_path / "estado.json")


def _lote_base():
    return {"lote_id": "L1", "status": "FECHADO_POR_JANELA", "identificacao": "SEM_CPF",
            "cpf": None, "pronto": False, "requer_revisao": True,
            "motivos_revisao": ["SEM_CPF: nenhum CPF identificado no lote"],
            "file_ids": ["10"], "mensagens": [{"id": 1}], "qtd_arquivos": 1,
            "arquivos": [{"file_id": "10", "indice": 1, "baixado": False, "sha256": None,
                          "erro": "adiado"}]}


# ---------------------------------------------------------------------------
def test_dois_processos_em_lotes_diferentes_nao_perdem_nada(arq):
    gravar_atomico({"lotes": {}}, arq)
    inicio = CTX.Event()
    a = CTX.Process(target=_w_lotes_proprios, args=(arq, "BITRIX-", N, inicio), name="a")
    b = CTX.Process(target=_w_lotes_proprios, args=(arq, "TG-", N, inicio), name="b")
    a.start(); b.start()
    inicio.set()
    for p in (a, b):
        p.join(60)
        assert p.exitcode == 0
    lotes = ler_estado(arq)["lotes"]
    assert len(lotes) == 2 * N, f"lotes perdidos: {2 * N - len(lotes)}"


def test_dois_processos_em_campos_distintos_do_mesmo_lote(arq):
    gravar_atomico({"lotes": {"L1": _lote_base()}}, arq)
    inicio = CTX.Event()
    a = CTX.Process(target=_w_identificacao, args=(arq, N, inicio), name="ident")
    b = CTX.Process(target=_w_download, args=(arq, N, inicio), name="down")
    a.start(); b.start()
    inicio.set()
    for p in (a, b):
        p.join(60)
        assert p.exitcode == 0
    lote = ler_estado(arq)["lotes"]["L1"]
    assert lote["cpf"] == CPF_T and lote["identificacao"] == "OK"
    assert lote["cpf_sugerido"] == str(N - 1)
    assert lote["ids_vistos"] == list(range(N)), "escrita da identificação perdida"
    assert lote["arquivos"][0]["baixado"] is True
    assert lote["arquivos"][0]["tamanho_bits"] == 10 + N - 1
    assert lote["arquivos"][0]["tentativas"] == N, "escrita do download perdida"
    assert lote["mensagens"] == [{"id": 1}], "campo da triagem foi mexido"


def test_json_invalido_nao_vira_estado_vazio_nem_gravacao(arq):
    original = '{"lotes": {"L1": {"cpf": "1"}, "L2"'
    with open(arq, "w", encoding="utf-8") as fh:
        fh.write(original)
    inicio = CTX.Event()
    p = CTX.Process(target=_w_lotes_proprios, args=(arq, "X-", 1, inicio), name="escritor")
    p.start()
    inicio.set()
    p.join(60)
    assert p.exitcode != 0, "o escritor deveria falhar com EstadoCorrompido"
    assert open(arq, encoding="utf-8").read() == original
    with pytest.raises(EstadoCorrompido):
        ler_estado(arq)
    assert not [n for n in os.listdir(os.path.dirname(arq)) if ".tmp" in n]


def test_trava_ocupada_por_outro_processo_da_timeout(arq):
    gravar_atomico({"lotes": {"fica": {}}}, arq)
    segurando, soltar = CTX.Event(), CTX.Event()
    p = CTX.Process(target=_w_segura_trava, args=(arq, segurando, soltar))
    p.start()
    try:
        assert segurando.wait(20)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            atualizar_estado(lambda e: e["lotes"].update({"novo": {}}), caminho=arq, timeout=0.4)
        assert time.monotonic() - t0 >= 0.35
        assert set(ler_estado(arq)["lotes"]) == {"fica"}
    finally:
        soltar.set()
        p.join(30)
    atualizar_estado(lambda e: e["lotes"].update({"novo": {}}), caminho=arq, timeout=5)
    assert set(ler_estado(arq)["lotes"]) == {"fica", "novo"}


def test_cpf_da_identificacao_sobrevive_a_regravacao_da_triagem(arq):
    """F1-03/A3: o ciclo lê no início, o completar_cpf grava no meio, o ciclo regrava."""
    gravar_atomico({"lotes": {"L1": _lote_base()}}, arq)
    leu, pode_gravar = CTX.Event(), CTX.Event()
    ciclo = CTX.Process(target=_w_ciclo_triagem_lento, args=(arq, leu, pode_gravar))
    ciclo.start()
    assert leu.wait(20)
    conf = atualizar_lotes({"L1": {"cpf": CPF_T, "cpf_formatado": CPF_T_FMT,
                                   "cpfs_distintos": [CPF_T], "identificacao": "OK",
                                   "cpf_origem": "bitrix-nome", "pronto": True,
                                   "requer_revisao": False, "_resolver_motivos": ["SEM_CPF*"]}},
                           "identificacao", caminho=arq)
    assert conf == {}
    pode_gravar.set()
    ciclo.join(60)
    assert ciclo.exitcode == 0
    lote = ler_estado(arq)["lotes"]["L1"]
    assert lote["cpf"] == CPF_T and lote["identificacao"] == "OK"
    assert lote["pronto"] is True and lote["requer_revisao"] is False
    assert lote["motivos_revisao"] == []
    assert lote["mensagens"] == [{"id": 1}, {"id": 2}], "delta da triagem perdido"


def test_marca_de_duplicata_da_ponte_sobrevive_a_reconciliacao(arq):
    """A1: reconciliar_* gravava o snapshot inteiro e desfazia o descartado_duplicata."""
    base = _lote_base()
    base.update(identificacao="OK", cpf=CPF_T, pronto=True, requer_revisao=False,
                motivos_revisao=[])
    gravar_atomico({"lotes": {"L1": base}}, arq)
    leu, pode_gravar = CTX.Event(), CTX.Event()
    rec = CTX.Process(target=_w_reconciliar, args=(arq, leu, pode_gravar))
    rec.start()
    assert leu.wait(20)
    atualizar_lotes({"L1": {"status": "descartado_duplicata", "status_anterior": "FECHADO_POR_JANELA",
                            "lote_origem": "L0", "descartado_em": "2026-11-09T10:00:00",
                            "descarte_motivo": "mesmo kit já enviado"}}, "ponte", caminho=arq)
    pode_gravar.set()
    rec.join(60)
    assert rec.exitcode == 0
    lote = ler_estado(arq)["lotes"]["L1"]
    assert lote["status"] == "descartado_duplicata" and lote["lote_origem"] == "L0"
    assert lote["arquivos"][0]["baixado"] is True and lote["arquivos"][0]["sha256"] == "cd" * 32
    # e um ciclo seguinte da triagem também não desfaz a marca
    atualizar_lotes({"L1": dict(base, status="FECHADO_POR_JANELA")}, "triagem", caminho=arq)
    assert ler_estado(arq)["lotes"]["L1"]["status"] == "descartado_duplicata"


def test_padrao_antigo_update_inteiro_perderia_o_cpf(arq):
    """Documenta o defeito: `fresco["lotes"].update(lotes_criados)` (triagem_grupo:2870)."""
    gravar_atomico({"lotes": {"L1": dict(_lote_base(), cpf=CPF_T, cpf_origem="bitrix-nome")}},
                   arq)
    velho = _lote_base()
    atualizar_estado(lambda e: e["lotes"].update({"L1": velho}), caminho=arq)
    assert ler_estado(arq)["lotes"]["L1"]["cpf"] is None      # perdido no jeito antigo
    gravar_atomico({"lotes": {"L1": dict(_lote_base(), cpf=CPF_T, cpf_origem="bitrix-nome",
                                         identificacao="OK")}}, arq)
    atualizar_lotes({"L1": velho}, "triagem", caminho=arq)
    assert ler_estado(arq)["lotes"]["L1"]["cpf"] == CPF_T   # jeito novo preserva
    assert json.load(open(arq))["lotes"]["L1"]["identificacao"] == "OK"
