"""dividir_lote_misto (F2-11): sub-lote com referência ao pai, pai DIVIDIDO na mesma
transação, idempotente, sem CPF no id."""
import json

import pytest

import bitrix_cliente as bc
import cpf_util
import dividir_lote_misto as dl
import estado_lote
import estado_store

CPF1 = cpf_util.gerar_para_teste(11)
CPF2 = cpf_util.gerar_para_teste(22)


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTADO_JSON", str(tmp_path / "estado.json"))
    import caminhos
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path))
    return tmp_path


def arq(fid, idx, nome):
    return {"file_id": fid, "indice": idx, "nome_original": nome, "baixado": True,
            "sha256": f"{idx:064d}", "tamanho_bits": 10, "caminho": f"/x/{idx:02d}_{nome}"}


def lote_misto():
    return {"lote_id": "P", "status": "FECHADO_POR_JANELA", "fechado": True,
            "identificacao": "SEM_CPF", "requer_revisao": True,
            "motivos_revisao": ["SEM_CPF: lote misto"], "pronto": False,
            "file_ids": ["1", "2", "3", "4", "5"],
            "arquivos": [arq("1", 1, "rg.jpg"), arq("2", 2, "kit ana.pdf"), arq("3", 3, "cnh.jpg"),
                         arq("4", 4, "kit bia.pdf"), arq("5", 5, "extra.jpg")]}


def ler_cpf(caminho):
    return {"/x/02_kit ana.pdf": CPF1, "/x/04_kit bia.pdf": CPF2}.get(caminho, "")


def plano(base, ids=("P",), cpf=ler_cpf):
    estado = estado_store.ler_estado(str(base / "estado.json"))
    return dl.planejar(estado, list(ids), ler_cpf=cpf, buscar_nome=lambda c: "")


def gravar(base, lotes):
    estado_store.gravar_atomico({"lotes": lotes}, str(base / "estado.json"))


def ler(base):
    return estado_store.ler_estado(str(base / "estado.json"))["lotes"]


def test_divide_e_marca_pai_dividido_na_mesma_transacao(base):
    gravar(base, {"P": lote_misto()})
    feitos = dl.aplicar(plano(base), str(base / "estado.json"))
    lotes = ler(base)
    assert feitos == {"P": ["P-k01", "P-k02"]}
    pai = lotes["P"]
    assert pai["status"] == "DIVIDIDO" and pai["pronto"] is False
    assert pai["dividido_em"] == ["P-k01", "P-k02"]
    assert estado_lote.estado_logico(pai) == "DIVIDIDO"
    assert estado_lote.pode_enfileirar(pai)[0] is False
    s1, s2 = lotes["P-k01"], lotes["P-k02"]
    assert s1["dividido_de"] == "P" and s2["dividido_de"] == "P"
    assert s1["file_ids"] == ["1", "2"] and s2["file_ids"] == ["3", "4", "5"]   # sobra no último
    assert s1["cpf"] == CPF1 and CPF1 not in "P-k01"
    assert estado_lote.pode_enfileirar(s1) == (True, [])
    assert "_donos_campos" not in s1 or "status" not in s1.get("_donos_campos", {})


def test_idempotente_segunda_execucao_nao_muda_nada(base):
    gravar(base, {"P": lote_misto()})
    dl.aplicar(plano(base), str(base / "estado.json"))
    antes = (base / "estado.json").read_bytes()
    p2 = plano(base)
    assert p2["P"]["subs"] == {} and "já dividido" in p2["P"]["motivo"]
    assert dl.aplicar(p2, str(base / "estado.json")) == {}
    assert (base / "estado.json").read_bytes() == antes


def test_ciclo_da_triagem_nao_desfaz_o_dividido(base):
    gravar(base, {"P": lote_misto()})
    dl.aplicar(plano(base), str(base / "estado.json"))
    estado_store.atualizar_lotes({"P": {"status": "FECHADO_POR_JANELA", "pronto": True}}, "triagem",
                                 caminho=str(base / "estado.json"))
    pai = ler(base)["P"]
    assert pai["status"] == "DIVIDIDO" and estado_lote.pode_enfileirar(pai)[0] is False


def test_bloco_sem_cpf_valido_nao_vira_sublote(base):
    gravar(base, {"P": lote_misto()})
    invalido = CPF2[:9] + ("00" if CPF2[9:] != "00" else "11")
    p = plano(base, cpf=lambda c: CPF1 if "ana" in c else "")
    assert list(p["P"]["subs"]) == ["P-k01"] and p["P"]["ignorados"] == [2]
    assert cpf_util.valido(invalido) is False


@pytest.mark.parametrize("status", ["descartado_duplicata", "revisao_duplicata", "IGNORADO"])
def test_nao_divide_descartado_ou_em_revisao_de_duplicata(base, status):
    lote = lote_misto()
    lote["status"] = status
    gravar(base, {"P": lote})
    assert plano(base)["P"]["subs"] == {}


def test_simulacao_padrao_nao_grava(base, monkeypatch):
    gravar(base, {"P": lote_misto()})
    monkeypatch.setattr(dl, "cpf_do_kit", ler_cpf)
    monkeypatch.setattr(dl, "nome_do_cpf", lambda c: "")
    monkeypatch.setattr(dl.planejar, "__defaults__", (ler_cpf, lambda c: ""))
    antes = (base / "estado.json").read_bytes()
    assert dl.main(["--lote", "P"]) == 0
    assert (base / "estado.json").read_bytes() == antes
    assert dl.main(["--lote", "P", "--aplicar"]) == 0
    assert ler(base)["P"]["status"] == "DIVIDIDO"


def test_nome_do_cpf_ambiguo_nao_adivinha(base, monkeypatch):
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", "https://exemplo.bitrix24.com.br/rest/1/" + "c" * 16 + "/")
    monkeypatch.setenv("BITRIX_RATELIMIT_FILE", str(base / "rl"))
    monkeypatch.setenv("BITRIX_RATE", "1000")
    monkeypatch.setenv("BITRIX_RAJADA", "1000")
    monkeypatch.setattr(bc, "_webhook_cache", None)
    resp = {"result": [{"ID": "1", "NAME": "Ana", "LAST_NAME": "Lima"},
                       {"ID": "2", "NAME": "Bia", "LAST_NAME": "Costa"}]}
    assert dl.nome_do_cpf(CPF1, _transporte=lambda u, p, t: (200, json.dumps(resp))) == ""
    um = {"result": [{"ID": "1", "NAME": "ana", "LAST_NAME": "lima"}]}
    assert dl.nome_do_cpf(CPF1, _transporte=lambda u, p, t: (200, json.dumps(um))) == "Ana Lima"
