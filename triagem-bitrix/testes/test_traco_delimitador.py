"""Testes do modo de traço DELIMITADOR (TDD) — pedido do dono em 24/09/2026.

Ideia dele: *"assim que a pessoa mandar —— ela abre a janela e assim que ela mandar de
volta embaixo já fecha o de cima e deixa o de baixo aberto e assim vai"*.

Ou seja: cada traço **fecha o bloco de cima e abre o de baixo** (bloco = intervalo entre
dois traços). O modo antigo (`grupo`) só fecha com o SEGUNDO traço e o material seguinte
cai no fallback de janela — o que espera até `timeout_lote_min` (120 min) e mistura gente.

Nome do modo: `modo_traco: "delimitador"` (qualquer autor) e `"delimitador_autor"`
(só o traço da MESMA pessoa delimita; traço de outra pessoa não corta o bloco).
"""
from conftest import RAIZ
import sys
from datetime import datetime, timezone

sys.path.insert(0, RAIZ)

import triagem_grupo as tg  # noqa: E402
import cpf_util  # noqa: E402
CPF_A_FMT = cpf_util.formatar(cpf_util.gerar_para_teste(1201))  # CPF sintético (DV válido)
CPF_B_FMT = cpf_util.formatar(cpf_util.gerar_para_teste(1202))  # CPF sintético (DV válido)
CPF_C_FMT = cpf_util.formatar(cpf_util.gerar_para_teste(1203))  # CPF sintético (DV válido)
CPF_A = cpf_util.gerar_para_teste(1201)  # CPF sintético (DV válido)
CPF_B = cpf_util.gerar_para_teste(1202)  # CPF sintético (DV válido)
CPF_C = cpf_util.gerar_para_teste(1203)  # CPF sintético (DV válido)


BASE = dict(tg.CONFIG_PADRAO)
CFG_DELIM = {**BASE, "modo_traco": "delimitador", "timeout_lote_min": 120,
             "janela_min_fallback": 30, "traco_no_fim_fecha": True}
CFG_ATUAL = {**BASE, "modo_traco": "grupo", "timeout_lote_min": 120,
             "janela_min_fallback": 30, "traco_no_fim_fecha": True}


def A(autor, ts, texto="", files=None, id_=None):
    A.n += 1
    msg = {"id": id_ or (1000 + A.n), "author_id": abs(hash(autor)) % 100000, "date": ts,
           "text": f"[B]{autor}:[/B] {texto}".strip(), "params": {"FILE_ID": files or []}}
    return tg.classificar_mensagem(msg, {}, BASE)


A.n = 0


def junto(msgs, cfg):
    return tg.agrupar(msgs, cfg, agora=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc))


def conversa_dois_clientes():
    """Padrão real do grupo: traço, cliente A, traço, cliente B, traço."""
    return [
        A("Ana", "2026-09-24T10:00:00+00:00", "——"),
        A("Ana", "2026-09-24T10:01:00+00:00", f"= MARIA DA SILVA = CPF {CPF_A_FMT} = 5 SEMANAS",
          files=["1"]),
        A("Ana", "2026-09-24T10:05:00+00:00", "——"),
        A("Ana", "2026-09-24T10:06:00+00:00", f"= JOAO SOUZA = CPF {CPF_B_FMT} = 20 SEMANAS",
          files=["2"]),
        A("Ana", "2026-09-24T10:10:00+00:00", "——"),
    ]


def test_modo_delimitador_fecha_o_de_cima_e_abre_o_de_baixo():
    lotes = junto(conversa_dois_clientes(), CFG_DELIM)

    ids = [l.get("lote_id") for l in lotes]
    assert len(ids) == len(set(ids)), "não pode duplicar lote"
    fechados = [l for l in lotes if l.get("status_lote") == "FECHADO_PRONTO"]
    abertos = [l for l in lotes if l.get("status_lote") == "ABERTO"]
    assert len(fechados) == 2, f"cada traço devia fechar o bloco de cima: {[l.get('status_lote') for l in lotes]}"
    assert len(abertos) == 1, "o último traço deixa um bloco aberto"


def test_modo_delimitador_nao_mistura_os_dois_clientes():
    lotes = junto(conversa_dois_clientes(), CFG_DELIM)
    cpfs = sorted(str(l.get("cpf") or "") for l in lotes if l.get("cpf"))

    assert f"{CPF_A}" in cpfs and f"{CPF_B}" in cpfs
    for l in lotes:
        distintos = l.get("cpfs_distintos") or []
        assert len(distintos) <= 1, f"lote {l.get('lote_id')} misturou {distintos}"


def test_modo_delimitador_marca_o_motivo_do_fechamento():
    lotes = junto(conversa_dois_clientes(), CFG_DELIM)
    fechados = [l for l in lotes if l.get("status_lote") == "FECHADO_PRONTO"]

    assert all("delimitad" in str(l.get("fechado_por") or "").lower() for l in fechados), \
        [l.get("fechado_por") for l in fechados]


def test_modo_atual_nao_e_afetado():
    """O modo 'grupo' (antigo) continua fechando só no 2º traço."""
    lotes = junto(conversa_dois_clientes(), CFG_ATUAL)

    fechados_por_traco = [l for l in lotes if l.get("status_lote") == "FECHADO_PRONTO"]
    assert len(fechados_por_traco) == 1, [l.get("status_lote") for l in lotes]


def test_delimitador_autor_ignora_traco_de_outra_pessoa():
    msgs = [
        A("Ana", "2026-09-24T10:00:00+00:00", "——"),
        A("Ana", "2026-09-24T10:01:00+00:00", f"= MARIA DA SILVA = CPF {CPF_A_FMT}", files=["1"]),
        A("Beto", "2026-09-24T10:02:00+00:00", "——"),
        A("Ana", "2026-09-24T10:03:00+00:00", f"= JOAO SOUZA = CPF {CPF_B_FMT}", files=["2"]),
    ]
    cfg = {**CFG_DELIM, "modo_traco": "delimitador_autor"}

    lotes = junto(msgs, cfg)

    fechados = [l for l in lotes if l.get("status_lote") == "FECHADO_PRONTO"]
    assert fechados == [], "traço de outra pessoa não pode cortar o bloco no modo por autor"


def test_delimitador_sem_material_nao_cria_lote_vazio():
    msgs = [
        A("Ana", "2026-09-24T10:00:00+00:00", "——"),
        A("Ana", "2026-09-24T10:01:00+00:00", "bom dia"),
        A("Ana", "2026-09-24T10:02:00+00:00", "——"),
        A("Ana", "2026-09-24T10:03:00+00:00", f"= MARIA = CPF {CPF_A_FMT}", files=["1"]),
    ]

    lotes = junto(msgs, CFG_DELIM)
    com_material = [l for l in lotes if l.get("file_ids")]

    assert len(com_material) == 1, [l.get("status_lote") for l in lotes]
    assert str(com_material[0].get("cpf")) == f"{CPF_A}"


def test_ficha_no_fim_puxa_documentos_anteriores_para_o_mesmo_lote():
    """(15/10/2026) A ficha pode vir DEPOIS dos documentos — SEM traço de
    abertura antes deles. O agrupador deve puxar os documentos anteriores
    (sem identidade) para o bloco da ficha, em vez de separar em
    'SEM_ARQUIVOS' (ficha) + 'SEM_CPF' (documentos)."""
    msgs = [
        A("Ana", "2026-09-24T10:00:00+00:00", "CLIENTE: Daniela Macelino Silva"),
        A("Ana", "2026-09-24T10:01:00+00:00", "Rua : Porto Alegre"),
        A("Ana", "2026-09-24T10:02:00+00:00", "", files=["1"]),  # documento
        A("Ana", "2026-09-24T10:03:00+00:00", "", files=["2"]),  # documento
        A("Ana", "2026-09-24T10:04:00+00:00", f"= DANIELA TESTE FICTICIA = CPF {CPF_C_FMT}"),  # ficha no FIM
        A("Ana", "2026-09-24T10:05:00+00:00", "——"),
    ]

    lotes = junto(msgs, CFG_DELIM)
    com_material = [l for l in lotes if l.get("file_ids")]

    assert len(com_material) == 1, \
        f"esperava 1 lote com arquivos (ficha+documentos juntos), veio {len(com_material)}"
    l = com_material[0]
    assert str(l.get("cpf")) == f"{CPF_C}", \
        f"o CPF da ficha devia estar no lote com os arquivos: {l.get('cpf')}"
    assert len(l.get("file_ids")) == 2, \
        f"os 2 documentos deviam estar no lote: {l.get('file_ids')}"
