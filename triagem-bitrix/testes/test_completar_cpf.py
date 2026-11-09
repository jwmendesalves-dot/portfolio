"""completar_cpf_por_nome: CPF por nome só com 2ª evidência (telefone no MESMO contato).

Bitrix falso via `bitrix_cliente._post` (mesma interface do `_transporte`), CPFs de
`cpf_util.gerar_para_teste`, nomes fictícios. Nenhuma rede.
"""
import json
import os

import pytest

import bitrix_cliente as bc
import completar_cpf_por_nome as cc
import cpf_util
import estado_lote
import estado_store

WEBHOOK = "https://exemplo.bitrix24.com.br/rest/1/" + "b" * 16 + "/"
CPF_A = cpf_util.gerar_para_teste(101)
CPF_B = cpf_util.gerar_para_teste(202)
NOME = "Clarice Teste Ribeiro"
FONE = "92991234567"


class BitrixFalso:
    def __init__(self, contatos=None, fones=None, erro=None):
        self.contatos = contatos or []          # dicts com ID, NAME, SECOND_NAME, LAST_NAME, CPF
        self.fones = fones or {}                # telefone (dígitos) -> [IDs]
        self.erro = erro
        self.chamadas = []

    def __call__(self, url, params, timeout):
        metodo = url.rsplit("/", 1)[1].replace(".json", "")
        self.chamadas.append((metodo, params))
        if self.erro:
            return self.erro
        if metodo == "crm.contact.list":
            f = params.get("filter") or {}
            itens = []
            for c in sorted(self.contatos, key=lambda c: int(c["ID"])):
                if ">ID" in f and int(c["ID"]) <= int(f[">ID"]):
                    continue
                if "%NAME" in f and f["%NAME"].upper() not in (c.get("NAME") or "").upper():
                    continue
                if "%LAST_NAME" in f and f["%LAST_NAME"].upper() not in (c.get("LAST_NAME") or "").upper():
                    continue
                itens.append({"ID": str(c["ID"]), "NAME": c.get("NAME"), "SECOND_NAME": c.get("SECOND_NAME"),
                              "LAST_NAME": c.get("LAST_NAME"), cc.CAMPO_CPF: c.get("CPF")})
            return 200, json.dumps({"result": itens[:50]})
        if metodo == "crm.duplicate.findbycomm":
            assert params["type"] == "PHONE" and params["entity_type"] == "CONTACT"
            assert len(params["values"]) <= 20
            ids = set()
            for v in params["values"]:
                ids.update(self.fones.get(cc.normalizar_telefone(v), []))
            return 200, json.dumps({"result": {"CONTACT": sorted(ids)} if ids else []})
        raise AssertionError(metodo)

    def n(self, metodo):
        return sum(1 for m, _ in self.chamadas if m == metodo)


@pytest.fixture(autouse=True)
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGEM_BASE", str(tmp_path))
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", WEBHOOK)
    monkeypatch.setenv("BITRIX_RATELIMIT_FILE", str(tmp_path / "rl"))
    monkeypatch.setenv("BITRIX_RATE", "1000")
    monkeypatch.setenv("BITRIX_RAJADA", "1000")
    monkeypatch.setenv("ESTADO_JSON", str(tmp_path / "estado.json"))
    import caminhos
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path))
    monkeypatch.setattr(bc, "_webhook_cache", None)
    monkeypatch.setattr(bc.time, "sleep", lambda s: None)
    yield tmp_path


def instalar(monkeypatch, falso):
    monkeypatch.setattr(bc, "_post", falso)
    return falso


def contato(i, nome, sobrenome, cpf, meio=None):
    return {"ID": i, "NAME": nome, "SECOND_NAME": meio, "LAST_NAME": sobrenome, "CPF": cpf}


def lote_sem_cpf(**extra):
    base = {
        "lote_id": "L1", "status": "FECHADO_PRONTO", "fechado": True, "nome": NOME,
        "identificacao": "SEM_CPF", "cpf": None, "cpfs_distintos": [],
        "file_ids": ["10"],
        "arquivos": [{"file_id": "10", "indice": 1, "baixado": True, "sha256": "ab" * 32,
                      "tamanho_bits": 100, "caminho": "/x/01_doc.pdf"}],
        "requer_revisao": True, "motivos_revisao": ["SEM_CPF: ficha sem CPF"],
        "pronto": False,
    }
    base.update(extra)
    return base


def gravar_estado(tmp_path, lotes):
    estado_store.gravar_atomico({"lotes": lotes}, str(tmp_path / "estado.json"))


def ler(tmp_path):
    return estado_store.ler_estado(str(tmp_path / "estado.json"))["lotes"]


def backups(tmp_path):
    d = tmp_path / "backups-estado"
    return sorted(os.listdir(d)) if d.exists() else []


# ---------------------------------------------------------------- normalização
def test_chave_nome_ignora_acento_particulas_e_caixa():
    assert cc.chave_nome("Clárice da Fontana  Ribeiro") == cc.chave_nome("CLARICE FONTANA RIBEIRO")
    assert cc.chave_nome("Ana dos Santos e Silva") == "ANA SANTOS SILVA"
    assert cc.chave_nome("Clarice Ribeiro") != cc.chave_nome(NOME)       # nome COMPLETO


# ---------------------------------------------------------------- ID-02 / S11
def test_homonimo_unico_sem_telefone_vira_so_sugestao(ambiente, monkeypatch):
    f = instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)]))
    gravar_estado(ambiente, {"L1": lote_sem_cpf()})
    r = cc.rodar(aplicar=True)
    lote = ler(ambiente)["L1"]
    assert r["sugeridos"] == ["L1"] and not r["confirmados"]
    assert lote["cpf_sugerido"] == CPF_A
    assert not lote.get("cpf") and lote["identificacao"] == "SEM_CPF"
    assert cc.MOTIVO_SUGERIDO in lote["motivos_revisao"]
    assert "SEM_CPF: ficha sem CPF" in lote["motivos_revisao"]
    assert lote["requer_revisao"] is True and lote["pronto"] is False
    assert estado_lote.pode_enfileirar(lote)[0] is False
    assert f.n("crm.duplicate.findbycomm") == 0       # lote sem telefone: nem consulta


def test_dois_homonimos_mesmo_na_pagina_3_e_ambiguo(ambiente, monkeypatch):
    """O código antigo lia só os 50 primeiros: o 2º homônimo (ID 130) passava batido."""
    contatos = [contato(1, "Clarice", "Teste Ribeiro", CPF_A)]
    contatos += [contato(i, "Clarice", f"Teste Ribeiro Outra{i}", cpf_util.gerar_para_teste(i))
                 for i in range(2, 130)]
    contatos.append(contato(130, "Clarice", "da Teste Ribeiro", CPF_B))
    f = instalar(monkeypatch, BitrixFalso(contatos, fones={FONE: ["1"]}))
    gravar_estado(ambiente, {"L1": lote_sem_cpf(telefones=[FONE])})
    r = cc.rodar(aplicar=True)
    lote = ler(ambiente)["L1"]
    assert r["ambiguos"] == ["L1"]
    assert lote.get("cpf_sugerido") is None and not lote.get("cpf")
    assert cc.MOTIVO_AMBIGUO in lote["motivos_revisao"] and lote["requer_revisao"] is True
    assert f.n("crm.contact.list") >= 3                   # paginou (>ID + start=-1)
    assert all(p.get("start") == -1 for m, p in f.chamadas if m == "crm.contact.list")


def test_nome_e_telefone_do_mesmo_contato_confirma_sem_liberar_duplicata(ambiente, monkeypatch):
    instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A),
                                       contato(8, "Outra", "Pessoa", CPF_B)], fones={FONE: ["7"]}))
    lote = lote_sem_cpf(status="revisao_duplicata", status_anterior="FECHADO_PRONTO",
                        telefones=["(92) 99123-4567"],
                        motivos_revisao=["SEM_CPF: ficha sem CPF", "possível duplicata do lote L0"],
                        _donos_campos={"status": "ponte"})
    gravar_estado(ambiente, {"L1": lote})
    r = cc.rodar(aplicar=True)
    novo = ler(ambiente)["L1"]
    assert r["confirmados"] == ["L1"]
    assert novo["cpf"] == CPF_A and len(novo["cpf"]) == 11            # F1-12
    assert novo["cpf_formatado"] == cpf_util.formatar(CPF_A)
    assert novo["cpf_origem"] == "bitrix-nome+telefone" and novo["identificacao"] == "OK"
    assert novo["status"] == "revisao_duplicata"                        # ponte preservada
    assert "possível duplicata do lote L0" in novo["motivos_revisao"]
    assert not any(m.startswith("SEM_CPF:") for m in novo["motivos_revisao"])
    assert novo["requer_revisao"] is True and novo.get("pronto") is not True
    assert estado_lote.pode_enfileirar(novo)[0] is False


def test_confirmado_limpo_fica_elegivel_pelo_estado_lote_sem_forcar_pronto(ambiente, monkeypatch):
    instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)],
                                      fones={FONE: ["7"]}))
    gravar_estado(ambiente, {"L1": lote_sem_cpf(telefones=[FONE])})
    cc.rodar(aplicar=True)
    novo = ler(ambiente)["L1"]
    assert novo["requer_revisao"] is False and novo["motivos_revisao"] == []
    assert novo["pronto"] is False                       # não força: a fila decide
    assert estado_lote.pode_enfileirar(novo) == (True, [])
    assert novo["motivos_resolvidos"][0]["motivo"] == "SEM_CPF:*"


def test_telefone_de_outro_contato_nao_confirma(ambiente, monkeypatch):
    instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)],
                                      fones={FONE: ["99"]}))
    gravar_estado(ambiente, {"L1": lote_sem_cpf(telefones=[FONE])})
    r = cc.rodar(aplicar=True)
    novo = ler(ambiente)["L1"]
    assert r["sugeridos"] == ["L1"] and not novo.get("cpf")
    assert cc.MOTIVO_TELEFONE_OUTRO in novo["motivos_revisao"]


def test_cpf_invalido_no_crm_nao_confirma(ambiente, monkeypatch):
    invalido = CPF_A[:9] + ("00" if CPF_A[9:] != "00" else "11")
    instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", invalido)],
                                      fones={FONE: ["7"]}))
    gravar_estado(ambiente, {"L1": lote_sem_cpf(telefones=[FONE])})
    cc.rodar(aplicar=True)
    novo = ler(ambiente)["L1"]
    assert not novo.get("cpf") and cc.MOTIVO_CPF_INVALIDO in novo["motivos_revisao"]


# ---------------------------------------------------------------- ID-01: não mexe no que não é dele
@pytest.mark.parametrize("extra", [
    {"status": "descartado_duplicata"},
    {"status": "CONFLITO"},
    {"identificacao": "CONFLITO"},
    {"dividido_em": ["L1-01"]},
    {"status": "IGNORADO"},
])
def test_descartado_conflito_dividido_nao_sao_tocados(ambiente, monkeypatch, extra):
    f = instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)],
                                          fones={FONE: ["7"]}))
    original = lote_sem_cpf(telefones=[FONE], **extra)
    gravar_estado(ambiente, {"L1": original})
    r = cc.rodar(aplicar=True)
    assert r["alvos"] == 0 and f.chamadas == []
    assert ler(ambiente)["L1"] == original


# ---------------------------------------------------------------- F1-04 / OP-O1 / ID-04
def test_segunda_execucao_nao_cria_backup_nem_consulta_o_bitrix(ambiente, monkeypatch):
    f = instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)]))
    gravar_estado(ambiente, {"L1": lote_sem_cpf(), "L2": lote_sem_cpf(lote_id="L2", nome="Rosana Teste Vieira")})
    r1 = cc.rodar(aplicar=True)
    assert r1["backup"] and len(backups(ambiente)) == 1
    chamadas = len(f.chamadas)
    mtime = os.path.getmtime(ambiente / "estado.json")
    r2 = cc.rodar(aplicar=True)
    assert r2["gravados"] == 0 and r2["backup"] is None
    assert len(backups(ambiente)) == 1
    assert len(f.chamadas) == chamadas                     # cache (inclusive negativo de L2)
    assert os.path.getmtime(ambiente / "estado.json") == mtime
    assert oct(os.stat(cc.caminho_cache()).st_mode & 0o777) == "0o600"


def test_cache_negativo_expira_pelo_ttl(ambiente, monkeypatch):
    f = instalar(monkeypatch, BitrixFalso([]))
    agora = [1000.0]
    cache = cc.Cache(agora=lambda: agora[0], ttl_neg=60, ttl_pos=3600)
    assert cc.buscar_candidatos(NOME, cache) == {"candidatos": [], "truncado": False}
    n = len(f.chamadas)
    cc.buscar_candidatos(NOME, cache)
    assert len(f.chamadas) == n
    agora[0] += 61
    cc.buscar_candidatos(NOME, cache)
    assert len(f.chamadas) > n


def test_simulacao_nao_grava_nem_faz_backup(ambiente, monkeypatch):
    instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)],
                                      fones={FONE: ["7"]}))
    gravar_estado(ambiente, {"L1": lote_sem_cpf(telefones=[FONE])})
    antes = (ambiente / "estado.json").read_bytes()
    assert cc.main(["--simular"]) == 0
    assert cc.main([]) == 0                                # padrão = simulação
    assert (ambiente / "estado.json").read_bytes() == antes and backups(ambiente) == []


def test_falha_do_bitrix_sai_com_codigo_2_e_nao_grava(ambiente, monkeypatch, caplog):
    instalar(monkeypatch, BitrixFalso(erro=(403, json.dumps({"error": "ACCESS_DENIED"}))))
    gravar_estado(ambiente, {"L1": lote_sem_cpf()})
    antes = (ambiente / "estado.json").read_bytes()
    assert cc.main(["--aplicar"]) == 2
    assert (ambiente / "estado.json").read_bytes() == antes


def test_log_nao_traz_cpf_em_claro(ambiente, monkeypatch, caplog):
    instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)]))
    gravar_estado(ambiente, {"L1": lote_sem_cpf()})
    with caplog.at_level("INFO"):
        cc.rodar(aplicar=False)
    assert CPF_A not in caplog.text and cpf_util.formatar(CPF_A) not in caplog.text
    assert cpf_util.mascarar(CPF_A) in caplog.text


def test_motivo_do_sugerido_grava_quando_o_store_declara_o_campo(ambiente, monkeypatch):
    monkeypatch.setitem(estado_store.PROPRIEDADE_CAMPOS, "cpf_sugerido_motivo", "identificacao")
    instalar(monkeypatch, BitrixFalso([contato(7, "Clarice", "Teste Ribeiro", CPF_A)]))
    gravar_estado(ambiente, {"L1": lote_sem_cpf()})
    r = cc.rodar(aplicar=True)
    assert r["conflitos"] == {}
    assert ler(ambiente)["L1"]["cpf_sugerido_motivo"] == cc.MOTIVO_SUGERIDO


def test_nao_importa_mais_nomear_lotes_nem_grava_json_direto():
    fonte = open(cc.__file__, encoding="utf-8").read()
    assert "nomear_lotes_por_cpf" not in fonte.replace("PARAR de importar", "")
    for proibido in ("json.dump", "shutil.copy", "os.replace", '"pronto": True', "requer_revisao\"] = False"):
        assert proibido not in fonte
