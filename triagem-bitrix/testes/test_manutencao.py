"""Scripts de manutenção (AUX-01): --confirmar obrigatório, simulação padrão, idempotência."""
import os

import pytest

import estado_store
from manutencao import backfill_nome_lotes as bf
from manutencao import reparo_josilene as rj

J, K = rj.LOTE_JOSI, rj.LOTE_KLIC
FIDS_J = ["2600893", "2600895", "2600897", "2600899", "2600901"]
FIDS_K_DELA = ["2600911", "2600913", "2600915", "2600917", "2600919", "2600921"]


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTADO_JSON", str(tmp_path / "estado.json"))
    import caminhos
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path))
    return tmp_path


def _meta(fid, idx, pasta, nome):
    return {"file_id": fid, "indice": idx, "baixado": True, "nome_salvo": nome,
            "caminho": str(pasta / nome), "sha256": "x", "tamanho_bits": 3}


def montar_caso(base):
    """Estado ANTES do reparo: 4 arquivos da Josilene nos índices 01-04 da Kliciane."""
    fj, fk = base / "lotes" / J, base / "lotes" / K
    fj.mkdir(parents=True)
    fk.mkdir(parents=True)
    metas_j, metas_k = [], []
    for i, fid in enumerate(FIDS_J, 1):
        nome = f"{i:02d}_josi{i}.jpg"
        (fj / nome).write_bytes(f"J{i}".encode())
        metas_j.append(_meta(fid, i, fj, nome))
    fids_k = rj.FIDS_ATRASADOS + FIDS_K_DELA
    for i, fid in enumerate(fids_k, 1):
        nome = f"{i:02d}_{'atrasado' if i <= 4 else 'klic'}{i}.jpg"
        (fk / nome).write_bytes(f"K{i}".encode())
        metas_k.append(_meta(fid, i, fk, nome))
    estado = {"lotes": {J: {"file_ids": list(FIDS_J), "arquivos": metas_j, "status": "FECHADO_PRONTO",
                            "cpf_origem": "x"},
                        K: {"file_ids": fids_k, "arquivos": metas_k, "nome": "Kliciane"}}}
    estado_store.gravar_atomico(estado, str(base / "estado.json"))
    return fj, fk


def listagem(base):
    return {p: sorted(os.listdir(base / "lotes" / p)) for p in (J, K)}


def test_reparo_simulacao_padrao_e_aplicar_exige_confirmar(base):
    montar_caso(base)
    antes = ((base / "estado.json").read_bytes(), listagem(base))
    assert rj.main([]) == 0
    assert rj.main(["--aplicar"]) == 2
    assert ((base / "estado.json").read_bytes(), listagem(base)) == antes


def test_reparo_aplica_e_segunda_execucao_nao_move_nada(base):
    montar_caso(base)
    assert rj.main(["--aplicar", "--confirmar"]) == 0
    lotes = estado_store.ler_estado(str(base / "estado.json"))["lotes"]
    assert lotes[J]["file_ids"] == FIDS_J + rj.FIDS_ATRASADOS and lotes[J]["qtd_arquivos"] == 9
    assert lotes[K]["file_ids"] == FIDS_K_DELA and lotes[K]["qtd_arquivos"] == 6
    disco = listagem(base)
    assert disco[J][5:] == ["06_atrasado1.jpg", "07_atrasado2.jpg", "08_atrasado3.jpg", "09_atrasado4.jpg"]
    assert disco[K] == [f"{i:02d}_klic{i + 4}.jpg" for i in range(1, 7)]
    assert (base / "lotes" / J / "06_atrasado1.jpg").read_bytes() == b"K1"
    assert lotes[J]["cpf_origem"] == "x" and lotes[K]["nome"] == "Kliciane"     # resto preservado
    backups = os.listdir(base / "backups-estado")
    assert len(backups) == 1

    # 2ª execução: o código antigo moveria 01-04 DA KLICIANE para a Josilene
    estado_antes, disco_antes = (base / "estado.json").read_bytes(), listagem(base)
    assert rj.main(["--aplicar", "--confirmar"]) == 0
    assert listagem(base) == disco_antes
    assert (base / "estado.json").read_bytes() == estado_antes
    assert os.listdir(base / "backups-estado") == backups


def test_reparo_nao_sobrescreve_destino_existente(base):
    fj, _ = montar_caso(base)
    (fj / "06_atrasado1.jpg").write_bytes(b"ja existia")
    antes = listagem(base)
    assert rj.main(["--aplicar", "--confirmar"]) == 2
    assert listagem(base) == antes
    assert (fj / "06_atrasado1.jpg").read_bytes() == b"ja existia"


def test_reparo_estado_que_nao_confere_aborta(base):
    montar_caso(base)
    estado_store.atualizar_estado(lambda e: e["lotes"][K].update(file_ids=FIDS_K_DELA),
                                  caminho=str(base / "estado.json"))
    antes = listagem(base)
    assert rj.main(["--aplicar", "--confirmar"]) == 2
    assert listagem(base) == antes


# ------------------------------------------------------------------ backfill
def test_backfill_exige_confirmar_e_e_idempotente(base, monkeypatch):
    monkeypatch.setattr(bf, "extrair_nomes", lambda t: ["Helena Teste Campos"] if "Helena" in t else [])
    estado = {"lotes": {
        "A": {"nome": None, "mensagens": [{"id": 1, "texto": "NOME DO CLIENTE Helena Teste Campos"}]},
        "B": {"nome": "Rosana Teste Vieira", "mensagens": []},
        "C": {"nome": None, "nome_origem": "bitrix-nome+telefone",
              "mensagens": [{"id": 1, "texto": "Helena"}]},
    }}
    estado_store.gravar_atomico(estado, str(base / "estado.json"))
    antes = (base / "estado.json").read_bytes()
    assert bf.main([]) == 0 and bf.main(["--aplicar"]) == 2
    assert (base / "estado.json").read_bytes() == antes
    assert bf.main(["--aplicar", "--confirmar"]) == 0
    lotes = estado_store.ler_estado(str(base / "estado.json"))["lotes"]
    assert lotes["A"]["nome"] == "Helena Teste Campos" and lotes["C"]["nome"] is None
    depois, backups = (base / "estado.json").read_bytes(), os.listdir(base / "backups-estado")
    assert bf.main(["--aplicar", "--confirmar"]) == 0
    assert (base / "estado.json").read_bytes() == depois
    assert os.listdir(base / "backups-estado") == backups


def test_scripts_de_manutencao_nao_gravam_json_direto():
    for mod in (rj, bf):
        fonte = open(mod.__file__, encoding="utf-8").read()
        for proibido in ("json.dump", "shutil.copy", "os.replace(", ".bak-"):
            assert proibido not in fonte, (mod.__name__, proibido)
