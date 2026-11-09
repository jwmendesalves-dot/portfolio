"""Reconciliadores (A1, E4, E5, OP-C1): patch por campo com dono 'download', sem
parciais/0 byte, sem associação por ordem alfabética, metas de erro preservadas."""
import hashlib
import os

import pytest

import estado_store
import reconciliar_arquivos_disco as rad
import reconciliar_estado as rec
import reconciliar_por_file_ids as rfi


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTADO_JSON", str(tmp_path / "estado.json"))
    import caminhos
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path))
    return tmp_path


def sha(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def pasta(base, lid, arquivos: dict):
    p = base / "lotes" / lid
    p.mkdir(parents=True, exist_ok=True)
    for nome, dados in arquivos.items():
        (p / nome).write_bytes(dados)
    return p


def gravar(base, lotes):
    estado_store.gravar_atomico({"lotes": lotes}, str(base / "estado.json"))


def ler(base):
    return estado_store.ler_estado(str(base / "estado.json"))["lotes"]


def meta(fid, idx, **extra):
    m = {"file_id": fid, "indice": idx, "baixado": False, "caminho": None, "sha256": None}
    m.update(extra)
    return m


# ------------------------------------------------------------------ reconciliar_estado
def test_reconciliar_estado_marca_por_indice_e_ignora_parciais(base):
    pasta(base, "L1", {"01_doc.pdf": b"um", "02_foto.jpg.part": b"pedaco", "03_x.pdf": b"",
                       "02_foto.jpg.parcial": b"p2", "04_y.tmp": b"t"})
    gravar(base, {"L1": {"file_ids": ["a", "b", "c", "d"],
                         "arquivos": [meta("a", 1), meta("b", 2), meta("c", 3), meta("d", 4)]}})
    assert rec.main([]) == 0
    arqs = {m["file_id"]: m for m in ler(base)["L1"]["arquivos"]}
    assert arqs["a"]["baixado"] is True and arqs["a"]["sha256"] == sha(b"um")
    assert arqs["a"]["tamanho_bits"] == 2
    for fid in ("b", "c", "d"):                       # .part/.parcial/0 byte/.tmp
        assert arqs[fid]["baixado"] is False and arqs[fid]["sha256"] is None


def test_reconciliar_estado_indice_duplicado_nao_escolhe(base):
    pasta(base, "L1", {"01_a.pdf": b"a", "01_b.pdf": b"b"})
    gravar(base, {"L1": {"file_ids": ["a"], "arquivos": [meta("a", 1)]}})
    estado = estado_store.ler_estado(str(base / "estado.json"))
    mudancas, ambiguos = rec.planejar(estado)
    assert mudancas == {} and ambiguos and "índice 1" in ambiguos[0][1]


def test_reconciliar_estado_simular_nao_grava(base):
    pasta(base, "L1", {"01_doc.pdf": b"um"})
    gravar(base, {"L1": {"file_ids": ["a"], "arquivos": [meta("a", 1)]}})
    antes = (base / "estado.json").read_bytes()
    assert rec.main(["--simular"]) == 0
    assert (base / "estado.json").read_bytes() == antes


def test_reconciliador_nao_desfaz_marca_da_ponte_gravada_no_meio(base, monkeypatch):
    """A1: o snapshot é velho; a gravação é patch por campo e preserva o que a ponte gravou."""
    pasta(base, "L1", {"01_doc.pdf": b"um"})
    gravar(base, {"L1": {"status": "FECHADO_PRONTO", "file_ids": ["a"], "arquivos": [meta("a", 1)]}})
    estado = estado_store.ler_estado(str(base / "estado.json"))
    mudancas, _ = rec.planejar(estado)                         # fora da trava (snapshot)
    estado_store.atualizar_lotes({"L1": {"status": "descartado_duplicata", "lote_origem": "L0"}},
                                 "ponte", caminho=str(base / "estado.json"))
    assert rec.aplicar(mudancas, str(base / "estado.json")) == {}
    lote = ler(base)["L1"]
    assert lote["status"] == "descartado_duplicata" and lote["lote_origem"] == "L0"
    assert lote["arquivos"][0]["baixado"] is True


def test_estado_corrompido_sai_com_1_sem_gravar(base):
    (base / "estado.json").write_text("{trunc")
    assert rec.main([]) == 1
    assert (base / "estado.json").read_text() == "{trunc"


# ------------------------------------------------------------------ reconciliar_arquivos_disco
def test_arquivos_disco_por_nome_registrado_com_backup_so_quando_muda(base):
    pasta(base, "L1", {"Documento RG.pdf": b"rg"})
    gravar(base, {"L1": {"file_ids": ["a"],
                         "arquivos": [meta("a", 1, nome_original="Documento RG.pdf")]}})
    assert rad.main([]) == 0                                 # padrão = simulação
    assert not (base / "backups-estado").exists()
    assert rad.main(["--aplicar"]) == 0
    assert ler(base)["L1"]["arquivos"][0]["sha256"] == sha(b"rg")
    assert len(os.listdir(base / "backups-estado")) == 1
    assert rad.main(["--aplicar"]) == 0                      # 2ª vez: nada muda, sem backup novo
    assert len(os.listdir(base / "backups-estado")) == 1
    assert not list(base.glob("estado.json.bak-*"))          # nada de .bak ao lado do estado


# ------------------------------------------------------------------ reconciliar_por_file_ids (E5)
def test_file_ids_sem_indice_nao_associa_por_ordem_alfabetica(base):
    """Antes: 'a.pdf' ia para o 1º file_id sem índice, 'b.pdf' para o 2º — sem prova."""
    pasta(base, "L1", {"01_x.pdf": b"x", "a.pdf": b"A", "b.pdf": b"B"})
    gravar(base, {"L1": {"file_ids": ["f1", "f2", "f3"], "arquivos": []}})
    estado = estado_store.ler_estado(str(base / "estado.json"))
    mudancas, relatorio = rfi.planejar(estado)
    patches = mudancas["L1"]["arquivos"]
    assert [p["file_id"] for p in patches] == ["f1"]
    assert sorted(relatorio[0]["sem_prova"]) == ["a.pdf", "b.pdf"]
    assert relatorio[0]["nao_resolvidos"] == ["f2", "f3"]


def test_file_ids_associa_por_nome_salvo_e_por_sha256(base):
    pasta(base, "L1", {"rg.pdf": b"RG", "cnh.jpg": b"CNH"})
    gravar(base, {"L1": {"file_ids": ["f1", "f2"], "arquivos": [
        meta("f1", 1, nome_salvo="rg.pdf", erro="timeout"),
        meta("f2", 2, sha256=sha(b"CNH"))]}})
    assert rfi.main(["--aplicar"]) == 0
    arqs = {m["file_id"]: m for m in ler(base)["L1"]["arquivos"]}
    assert arqs["f1"]["caminho"].endswith("rg.pdf") and arqs["f1"]["erro"] is None
    assert arqs["f2"]["caminho"].endswith("cnh.jpg") and arqs["f2"]["baixado"] is True


def test_file_ids_preserva_meta_de_erro_nao_resolvido(base):
    pasta(base, "L1", {"01_x.pdf": b"x"})
    gravar(base, {"L1": {"file_ids": ["f1", "f2"], "arquivos": [
        meta("f1", 1), meta("f2", 2, erro="NAO_ENCONTRADO", erro_tipo="permanente")]}})
    assert rfi.main(["L1", "--aplicar"]) == 0
    arqs = {m["file_id"]: m for m in ler(base)["L1"]["arquivos"]}
    assert arqs["f1"]["baixado"] is True
    assert arqs["f2"]["erro"] == "NAO_ENCONTRADO" and arqs["f2"]["erro_tipo"] == "permanente"


def test_file_ids_simulacao_padrao_e_dias_nao_vira_lote(base):
    """Antes `--dias 7` virava o alvo '7' (nenhum lote casava) — argparse corrige."""
    pasta(base, "L1", {"01_x.pdf": b"x"})
    gravar(base, {"L1": {"inicio": "2999-01-01T00:00:00", "file_ids": ["f1"], "arquivos": []}})
    antes = (base / "estado.json").read_bytes()
    assert rfi.main(["--dias", "7"]) == 0
    assert (base / "estado.json").read_bytes() == antes
    assert rfi.main(["--dias", "7", "--aplicar"]) == 0
    assert ler(base)["L1"]["arquivos"][0]["file_id"] == "f1"


def test_arquivo_valido():
    assert rec.arquivo_valido("01_a.pdf", __file__)
    for nome in ("01_a.pdf.part", "01_a.parcial", "01_a.tmp", "x.meta.json", ".oculto"):
        assert not rec.arquivo_valido(nome, __file__)
