"""R1 — regressão dos achados A1/A2/A3 da revisão final (foco 1, documentos).

Ver docs/remediacao/revisao/final-1-documentos.md. Bitrix falso por monkeypatch
(sem rede); CPFs sintéticos de `cpf_util.gerar_para_teste`; nomes fictícios.
"""
import json

import bitrix_cliente
import completar_cpf_por_nome as C
import cpf_util
import download_svc
import ponte_criterio as P

CPF_A = cpf_util.gerar_para_teste(101)
CPF_B = cpf_util.gerar_para_teste(202)
FONE_X = "65999990001"   # é do contato 2 (Pedro)
FONE_Y = "65999990002"   # é do contato 1 (Joao)


def _fakes(monkeypatch, contatos, fones, chamadas=None):
    """contatos: registros CRM; fones: {telefone: [ids]} (verdade do CRM)."""
    def listar(metodo, params=None, **k):
        return list(contatos)

    def chamar(metodo, params=None, **k):
        assert metodo == "crm.duplicate.findbycomm"
        if chamadas is not None:
            chamadas.append(list(params["values"]))
        ids = set()
        for v in params["values"]:
            ids.update(fones.get(v.replace("+55", ""), []))
        return {"result": {"CONTACT": sorted(ids)}}
    monkeypatch.setattr(bitrix_cliente, "listar", listar)
    monkeypatch.setattr(bitrix_cliente, "chamar", chamar)


def _contatos():
    return [{"ID": "1", "NAME": "Joao", "LAST_NAME": "Silva", C.CAMPO_CPF: CPF_A},
            {"ID": "2", "NAME": "Pedro", "LAST_NAME": "Souza", C.CAMPO_CPF: CPF_B}]


# ------------------------------------------------------------------ A1
def test_a1_cache_de_telefone_nao_promove_homonimo_a_confirmar(tmp_path, monkeypatch):
    _fakes(monkeypatch, _contatos(), {FONE_X: ["2"], FONE_Y: ["1"]})
    cache = C.Cache(str(tmp_path / "cache.json"))
    C.contatos_por_telefone([FONE_X, FONE_Y], cache)          # lote 1: dois telefones
    busca = C.buscar_candidatos("Joao Silva", cache)
    ids = C.contatos_por_telefone([FONE_X], cache)             # lote 2: só o fone do Pedro
    assert ids == {"2"}
    assert C.decidir(busca, ids, True)["acao"] == "sugerir"


def test_a1_uma_consulta_por_telefone(tmp_path, monkeypatch):
    chamadas = []
    _fakes(monkeypatch, _contatos(), {FONE_X: ["2"], FONE_Y: ["1"]}, chamadas)
    cache = C.Cache(str(tmp_path / "cache.json"))
    assert C.contatos_por_telefone([FONE_X, FONE_Y], cache) == {"1", "2"}
    assert sorted(chamadas) == [[FONE_X, "+55" + FONE_X], [FONE_Y, "+55" + FONE_Y]]
    assert cache.obter("fones", FONE_X, "contatos") == ["2"]
    assert cache.obter("fones", FONE_Y, "contatos") == ["1"]


def test_a1_cache_v1_tem_fones_descartados(tmp_path, monkeypatch):
    arq = tmp_path / "cache.json"
    C.Cache(str(arq))                                          # garante o formato
    import time
    arq.write_text(json.dumps({"versao": 1, "nomes": {}, "fones": {
        FONE_X: {"contatos": ["1", "2"], "ts": time.time(), "em": time.time()}}}))
    _fakes(monkeypatch, _contatos(), {FONE_X: ["2"]})
    cache = C.Cache(str(arq))
    assert cache.dados["versao"] >= 2 and cache.dados["fones"] == {}
    assert C.contatos_por_telefone([FONE_X], cache) == {"2"}


# ------------------------------------------------------------------ A2
def test_a2_file_id_sem_meta_nao_e_kit_completo_nem_repetido():
    lote = {"file_ids": ["10", "11"],
            "arquivos": [{"file_id": "10", "baixado": True, "sha256": "a" * 64,
                          "tamanho_bits": 10, "nome_original": "rg.pdf"}]}
    assert P.kit_completo(lote) is False
    assert P.situacao_kit(lote, {"a" * 12}) != "repetido"
    lote["file_ids"] = ["10"]                                  # kit inteiro: volta a valer
    assert P.kit_completo(lote) is True


# ------------------------------------------------------------------ A3
def _patch(tmp_path, fid="77"):
    return [{"file_id": fid, "indice": 1, "baixado": True, "sha256": "b" * 64,
             "caminho": str(tmp_path / "01_rg.pdf"), "tamanho_bits": 5}]


def test_a3_lote_id_velho_aplica_no_lote_que_tem_o_fid(tmp_path):
    estado = tmp_path / "estado.json"
    estado.write_text(json.dumps({"lotes": {"LOTE-NOVO": {
        "file_ids": ["77"], "arquivos": [{"file_id": "77", "indice": 1}]}}}))
    fila = download_svc.Fila(str(tmp_path / "fila.json"))
    fila.enfileirar("77", str(tmp_path / "01_rg.pdf"), lote_id="LOTE-VELHO", indice=1)
    itens = {it["file_id"]: it for it in fila.itens}
    download_svc._aplicar_no_estado(_patch(tmp_path), itens, str(estado), fila=fila)
    meta = json.loads(estado.read_text())["lotes"]["LOTE-NOVO"]["arquivos"][0]
    assert meta.get("baixado") is True and meta.get("sha256") == "b" * 64


def test_a3_pai_dividido_cai_no_sub_lote(tmp_path):
    estado = tmp_path / "estado.json"
    estado.write_text(json.dumps({"lotes": {
        "PAI": {"status": "DIVIDIDO", "dividido_em": ["PAI-a"], "file_ids": ["77"],
                "arquivos": [{"file_id": "77", "indice": 1}]},
        "PAI-a": {"file_ids": ["77"], "arquivos": [{"file_id": "77", "indice": 1}]}}}))
    fila = download_svc.Fila(str(tmp_path / "fila.json"))
    fila.enfileirar("77", str(tmp_path / "01_rg.pdf"), indice=1)
    itens = {it["file_id"]: it for it in fila.itens}
    download_svc._aplicar_no_estado(_patch(tmp_path), itens, str(estado), fila=fila)
    meta = json.loads(estado.read_text())["lotes"]["PAI-a"]["arquivos"][0]
    assert meta.get("baixado") is True


def test_a3_sem_lote_fica_pendente_e_reaplica_depois(tmp_path):
    estado = tmp_path / "estado.json"
    estado.write_text(json.dumps({"lotes": {"OUTRO": {"file_ids": ["1"], "arquivos": []}}}))
    fila = download_svc.Fila(str(tmp_path / "fila.json"))
    fila.enfileirar("77", str(tmp_path / "01_rg.pdf"), lote_id="LOTE-VELHO", indice=1)
    itens = {it["file_id"]: it for it in fila.itens}
    download_svc._aplicar_no_estado(_patch(tmp_path), itens, str(estado), fila=fila)
    fila.recarregar()
    it = next(i for i in fila.itens if i["file_id"] == "77")
    assert it.get("estado_pendente") is True and it["estado_meta"]["sha256"] == "b" * 64

    # o lote aparece depois: a próxima rodada reaplica e limpa a marca
    estado.write_text(json.dumps({"lotes": {"LOTE-NOVO": {
        "file_ids": ["77"], "arquivos": [{"file_id": "77", "indice": 1}]}}}))
    pend = download_svc._pendentes_de_estado(fila)
    assert [p["file_id"] for p in pend] == ["77"]
    download_svc._aplicar_no_estado([dict(p["estado_meta"]) for p in pend],
                                    {p["file_id"]: p for p in pend}, str(estado), fila=fila)
    meta = json.loads(estado.read_text())["lotes"]["LOTE-NOVO"]["arquivos"][0]
    assert meta.get("baixado") is True
    fila.recarregar()
    assert not next(i for i in fila.itens if i["file_id"] == "77").get("estado_pendente")
