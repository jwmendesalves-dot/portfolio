# -*- coding: utf-8 -*-
"""Frente C2a (onda 3) — F1-02: lote_id estável, bloco aberto entre ciclos, sem lote artificial.

Transforma em teste a simulação da auditoria (`sim_fase1.py`: 3 kits, janela andando
1 mensagem por ciclo -> 9 lotes no código antigo). Sem rede (fonte falsa do
test_fase1_cursor), CPFs sintéticos, nomes fictícios.
"""
import datetime as dt

import estado_store
import triagem_grupo as tg
from test_fase1_cursor import DIALOG, FonteFalsa, args, conf_teste, ler, montar_kits, usar_fonte


def _lotes_vivos(arq):
    return {lid: reg for lid, reg in (ler(arq).get("lotes") or {}).items()
            if str(reg.get("status") or "").upper() != "IGNORADO"}


def _ids_referencia(seq):
    """Lotes que o agrupar forma lendo a conversa INTEIRA de uma vez (a verdade)."""
    conf = conf_teste()
    msgs = [tg.classificar_mensagem(m, {}, conf) for m in seq]
    return {l["lote_id"]: l for l in tg.agrupar(msgs, conf, agora=dt.datetime.now(dt.timezone.utc))
            if l.get("file_ids")}


def test_simulacao_3_kits_janela_andando_vira_3_lotes(ambiente, monkeypatch):
    seq = montar_kits()
    fonte = FonteFalsa(seq, janela=10)          # leitura descendente só alcança a cauda
    usar_fonte(monkeypatch, fonte)
    vistos = set()
    for n in range(1, len(seq) + 1):            # chega 1 mensagem por ciclo
        fonte.visiveis = n
        tg.ciclo(args())
        vistos |= set(_lotes_vivos(ambiente["arq"]))
    for _ in range(3):                          # ciclos sem mensagem nova
        tg.ciclo(args())
    vivos = _lotes_vivos(ambiente["arq"])
    ref = _ids_referencia(seq)
    assert len(vivos) == 3, f"3 kits viraram {len(vivos)} lotes: {sorted(vivos)}"
    assert set(vivos) == set(ref), "lote_id deve ser o do bloco, não o do início da janela"
    assert vistos == set(vivos), "nenhum lote-fragmento pode ter existido no meio"
    for k, (lid, reg) in enumerate(sorted(vivos.items(), key=lambda x: x[1]["inicio"])):
        assert reg["file_ids"] == [f"{k}01", f"{k}02"]
        assert reg["identificacao"] == "OK" and reg["pronto"] is True


def test_rodar_o_ciclo_3x_com_a_mesma_fonte_nao_cria_lote_novo(ambiente, monkeypatch):
    fonte = FonteFalsa(montar_kits(), janela=10)
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    primeiro = ler(ambiente["arq"])
    assert primeiro["lotes"]
    gravados = []
    original = estado_store.atualizar_lotes

    def espiao(mudancas, dono, caminho=None):
        gravados.append(sorted(mudancas))
        return original(mudancas, dono, caminho=caminho)

    monkeypatch.setattr(estado_store, "atualizar_lotes", espiao)
    for _ in range(3):
        tg.ciclo(args())
    depois = ler(ambiente["arq"])
    assert set(depois["lotes"]) == set(primeiro["lotes"])
    assert all(not g for g in gravados), f"lotes fechados regravados/reagrupados: {gravados}"
    # fechado não é reagrupado/reescrito
    for lid, reg in primeiro["lotes"].items():
        assert depois["lotes"][lid]["visto_em"] == reg["visto_em"]


def test_kit_que_atravessa_dois_ciclos_fica_num_lote_so(ambiente, monkeypatch):
    seq = montar_kits(n_kits=1)
    fonte = FonteFalsa(seq, janela=10)
    usar_fonte(monkeypatch, fonte)
    fonte.visiveis = 3                          # conversa + 1ª foto
    tg.ciclo(args())
    assert _lotes_vivos(ambiente["arq"]) == {}, "kit em formação não vira lote ainda"
    regiao = ler(ambiente["arq"])["f1_regiao"][DIALOG]
    assert [m["id"] for m in regiao["mensagens"]] == [1001, 1002, 1003]
    fonte.visiveis = 6                          # 2ª foto, cabeçalho, traço
    tg.ciclo(args())
    vivos = _lotes_vivos(ambiente["arq"])
    assert len(vivos) == 1
    (reg,) = vivos.values()
    assert reg["file_ids"] == ["001", "002"]
    assert [m["id"] for m in reg["mensagens"]] == [1001, 1002, 1003, 1004, 1005, 1006]
    assert ler(ambiente["arq"])["f1_regiao"][DIALOG]["mensagens"] == []


def test_bloco_aberto_pelo_traco_atravessa_ciclos_com_o_mesmo_id(ambiente, monkeypatch):
    ambiente["conf"] = conf_teste(modo_traco="grupo_autor")
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    seq = []

    def add(texto="", files=None):
        i = len(seq) + 1
        seq.append({"id": 3000 + i, "author_id": 1,
                    "date": (base + dt.timedelta(minutes=i)).isoformat(),
                    "text": f"[B]Atendente A:[/B] {texto}", "params": {"FILE_ID": files or []}})

    from test_fase1_cursor import CPFS
    import cpf_util
    add("——")
    add("", ["901"])
    add("", ["902"])
    add(f"= CLARA FICTICIA\n= CPF {cpf_util.formatar(CPFS[0])}")
    add("——")
    fonte = FonteFalsa(seq, janela=10)
    usar_fonte(monkeypatch, fonte)
    fonte.visiveis = 2
    tg.ciclo(args())
    aberto = _lotes_vivos(ambiente["arq"])
    assert len(aberto) == 1 and list(aberto.values())[0]["status"] == "ABERTO"
    lid = next(iter(aberto))
    fonte.visiveis = 5
    tg.ciclo(args())
    vivos = _lotes_vivos(ambiente["arq"])
    assert set(vivos) == {lid}
    assert vivos[lid]["status"] == "FECHADO_PRONTO" and vivos[lid]["file_ids"] == ["901", "902"]


def test_conversa_solta_nao_vira_lote(ambiente, monkeypatch):
    velho = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
    seq = [{"id": 4000 + i, "author_id": 1, "date": (velho + dt.timedelta(minutes=i)).isoformat(),
            "text": f"[B]Pessoa {i % 2}:[/B] {t}", "params": {"FILE_ID": []}}
           for i, t in enumerate(["bom dia", "ok", "obrigada", "vou ver"], 1)]
    fonte = FonteFalsa(seq, janela=10)
    usar_fonte(monkeypatch, fonte)
    r = tg.ciclo(args())
    assert ler(ambiente["arq"]).get("lotes", {}) == {}
    assert r["f1_descartes"]["sem_material"] >= 1


def test_mensagem_ja_em_lote_fechado_nao_gera_lote_novo(ambiente, monkeypatch):
    seq = montar_kits(n_kits=1)
    # estado legado: fragmento FECHADO do kit (msgs 1003..1006) com outro id
    frag = {"lote_id": "LOTE-LEGADO-frag0001", "status": "FECHADO_PRONTO", "aberto": False,
            "pronto": True, "identificacao": "OK", "file_ids": ["001", "002"],
            "dialog_id": DIALOG, "mensagens": [{"id": i} for i in (1003, 1004, 1005, 1006)]}
    estado_store.gravar_atomico({"lotes": {frag["lote_id"]: frag}}, ambiente["arq"])
    fonte = FonteFalsa(seq, janela=10)
    usar_fonte(monkeypatch, fonte)
    r = tg.ciclo(args())
    lotes = ler(ambiente["arq"])["lotes"]
    assert set(lotes) == {"LOTE-LEGADO-frag0001"}, "kit já encaminhado não pode nascer de novo"
    assert lotes["LOTE-LEGADO-frag0001"]["mensagens"] == frag["mensagens"]   # histórico intacto
    assert r["f1_descartes"]["ja_incorporado"] == 1


def test_lote_existente_aberto_mantem_o_id_antigo(ambiente, monkeypatch):
    """1ª execução depois do deploy: o lote já gravado (mesma 1ª mensagem) é atualizado
    com o MESMO id — a fórmula do id não mudou, então nada é renomeado."""
    seq = montar_kits(n_kits=1)
    ref = _ids_referencia(seq)
    (lid,) = ref
    estado_store.gravar_atomico({"lotes": {lid: {"lote_id": lid, "status": "ABERTO",
                                                 "aberto": True, "dialog_id": DIALOG,
                                                 "mensagens": [{"id": 1001}]}},
                                 "cursores": {DIALOG: 1003}}, ambiente["arq"])
    fonte = FonteFalsa(seq, janela=10)
    usar_fonte(monkeypatch, fonte)
    tg.ciclo(args())
    lotes = ler(ambiente["arq"])["lotes"]
    assert set(lotes) == {lid}
    assert lotes[lid]["status"] == "FECHADO_PRONTO"
