"""Testes da VARREDURA de lotes vencidos no estado (TDD).

Caso real (24/09/2026): `LOTE-SIG-20260922-1845-fcd74515` ficou **ABERTO por 50h** no
`estado.json`. Motivo: o vencimento (timeout de 120 min sem traço de fechamento) só é
avaliado sobre as mensagens que o ciclo ACABOU de ler; quando o cursor passa daquelas
mensagens, nenhum ciclo reavalia o lote — ele nunca sai de ABERTO.

A varredura abaixo roda sobre o ESTADO (sem API) e fecha o que venceu, para o lote não
ficar "aberto" para sempre (e para o vigia não acusar lote fantasma).
"""
from conftest import RAIZ
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, RAIZ)

from lotes_regras import lotes_vencidos  # noqa: E402

AGORA = datetime(2026, 9, 24, 22, 0, tzinfo=timezone.utc)


def lote(lid, status="ABERTO", aberto=True, minutos_atras=0, pronto=False, arquivos=0,
         fim=True):
    quando = (AGORA - timedelta(minutes=minutos_atras)).isoformat()
    reg = {"lote_id": lid, "status": status, "aberto": aberto, "pronto": pronto,
           "inicio": quando, "fim": quando if fim else None,
           "arquivos": [f"a{i}.jpg" for i in range(arquivos)]}
    return lid, reg


def test_lote_aberto_vencido_e_devolvido():
    estado = {"lotes": dict([lote("L-1", minutos_atras=150)])}

    vencidos = lotes_vencidos(estado, agora=AGORA, timeout_min=120)

    assert [v["lote_id"] for v in vencidos] == ["L-1"]
    assert "timeout" in vencidos[0]["motivo"]


def test_lote_aberto_dentro_do_prazo_nao_e_tocado():
    estado = {"lotes": dict([lote("L-2", minutos_atras=30)])}

    assert lotes_vencidos(estado, agora=AGORA, timeout_min=120) == []


def test_lote_ja_fechado_nao_e_tocado():
    estado = {"lotes": dict([lote("L-3", status="FECHADO_PRONTO", aberto=False, minutos_atras=500),
                             lote("L-4", status="FECHADO_POR_TIMEOUT", aberto=False,
                                  minutos_atras=500),
                             lote("L-5", status="CONFLITO", aberto=False, minutos_atras=500)])}

    assert lotes_vencidos(estado, agora=AGORA, timeout_min=120) == []


def test_lote_aberto_sem_fim_usa_o_inicio():
    estado = {"lotes": dict([lote("L-6", minutos_atras=400, fim=False)])}

    assert [v["lote_id"] for v in lotes_vencidos(estado, agora=AGORA, timeout_min=120)] == ["L-6"]


def test_lote_aberto_com_data_ilegivel_e_ignorado():
    reg = {"lote_id": "L-7", "status": "ABERTO", "aberto": True, "inicio": "ontem", "fim": None}
    estado = {"lotes": {"L-7": reg}}

    assert lotes_vencidos(estado, agora=AGORA, timeout_min=120) == []


def test_aguenta_estado_vazio_ou_estranho():
    assert lotes_vencidos({}, agora=AGORA, timeout_min=120) == []
    assert lotes_vencidos(None, agora=AGORA, timeout_min=120) == []
    assert lotes_vencidos({"lotes": None}, agora=AGORA, timeout_min=120) == []
    assert lotes_vencidos({"lotes": {"x": "lixo"}}, agora=AGORA, timeout_min=120) == []


def test_varios_vencidos_saem_do_mais_antigo_para_o_mais_novo():
    estado = {"lotes": dict([lote("L-NOVO", minutos_atras=130), lote("L-VELHO", minutos_atras=3000)])}

    assert [v["lote_id"] for v in lotes_vencidos(estado, agora=AGORA, timeout_min=120)] == \
        ["L-VELHO", "L-NOVO"]


def test_marca_o_motivo_e_o_status_de_fechamento():
    estado = {"lotes": dict([lote("L-8", minutos_atras=200)])}

    vencido = lotes_vencidos(estado, agora=AGORA, timeout_min=120)[0]

    assert vencido["status_novo"] == "FECHADO_POR_TIMEOUT"
    assert vencido["minutos"] == 200
