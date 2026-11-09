#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REPARO PONTUAL do caso Josilene (04/11/2026) — script de manutenção, não roda em timer.

O que houve (reconstruído das mensagens do grupo):
  12:45:37  ficha da Josilene
  12:45:37-39  5 arquivos (fileIds 2600893..2600901)
  12:45:39  "————" (traço fecha o lote dela)
  12:45:40-42  4 arquivos (2600903..2600909)  <- ainda da Josilene, mandados depois do traço
  12:45:59  ficha da Kliciane
  -> os 4 arquivos da Josilene entraram no lote da Kliciane (índices 1-4).

Move os 4 arquivos (disco + estado) de volta para o lote da Josilene e renumera os dois.

Segurança (AUX-01):
  * padrão = simulação; gravar exige `--aplicar --confirmar`;
  * idempotente: se os 4 file_ids já estão no lote da Josilene, NÃO move nada (o código
    antigo procurava os índices 01-04 na pasta da Kliciane e, numa 2ª execução, movia os
    arquivos DELA);
  * arquivos identificados pelo file_id (meta `nome_salvo`/`caminho`), não pelo índice;
  * nunca sobrescreve destino existente (aborta antes de mover qualquer coisa);
  * estado gravado via `estado_store.atualizar_estado` (trava + atômico), com a mesma
    checagem refeita dentro da trava; backup por `estado_store.fazer_backup`.

Uso:
  python3 manutencao/reparo_josilene.py                        # simulação (padrão)
  python3 manutencao/reparo_josilene.py --aplicar --confirmar  # grava
Saída: 0 ok / nada a fazer; 1 estado corrompido; 2 estado não confere / plano inseguro.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import caminhos  # noqa: E402
import estado_store  # noqa: E402

LOTE_JOSI = "LOTE-20261104-1245-0ae0f7fd"
LOTE_KLIC = "LOTE-SIG-20261104-1245-8d6cc9ce"
FIDS_ATRASADOS = ["2600903", "2600905", "2600907", "2600909"]  # 4 arquivos da Josilene
FONTE = "reparo Josilene (04/11/2026)"


class PlanoInseguro(Exception):
    pass


def sha256_de(caminho: str) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def caminho_estado() -> str:
    return os.environ.get("ESTADO_JSON") or caminhos.em_base("estado.json")


def ja_corrigido(estado: dict) -> bool:
    j = (estado.get("lotes") or {}).get(LOTE_JOSI) or {}
    return all(f in [str(x) for x in (j.get("file_ids") or [])] for f in FIDS_ATRASADOS)


def _nome_no_disco(meta: dict, pasta: str) -> str:
    for n in (meta.get("nome_salvo"), os.path.basename(str(meta.get("caminho") or ""))):
        if n and os.path.isfile(os.path.join(pasta, n)) and os.path.getsize(os.path.join(pasta, n)) > 0:
            return n
    raise PlanoInseguro(f"arquivo do file_id {meta.get('file_id')} não está em {os.path.basename(pasta)}")


def _sem_indice(nome: str) -> str:
    return nome.split("_", 1)[1] if nome[:2].isdigit() and nome[2:3] == "_" else nome


def planejar(estado: dict) -> dict | None:
    """None = nada a fazer (já corrigido). Plano inseguro -> PlanoInseguro."""
    if ja_corrigido(estado):
        return None
    lotes = estado.get("lotes") or {}
    j, k = lotes.get(LOTE_JOSI), lotes.get(LOTE_KLIC)
    if not isinstance(j, dict) or not isinstance(k, dict):
        raise PlanoInseguro("lotes do reparo não estão no estado")
    fids_k = [str(x) for x in (k.get("file_ids") or [])]
    if not all(f in fids_k for f in FIDS_ATRASADOS):
        raise PlanoInseguro("os 4 file_ids não estão (todos) no lote da Kliciane — estado não confere")
    fj, fk = caminhos.em_base("lotes", LOTE_JOSI), caminhos.em_base("lotes", LOTE_KLIC)
    metas_k = {str(m.get("file_id")): m for m in (k.get("arquivos") or []) if isinstance(m, dict)}
    movs, metas_j_novas, metas_k_novas = [], [], []
    fids_j = [str(x) for x in (j.get("file_ids") or [])]
    for n, fid in enumerate(FIDS_ATRASADOS, start=len(fids_j) + 1):
        meta = metas_k.get(fid)
        if not meta:
            raise PlanoInseguro(f"file_id {fid} sem meta no lote da Kliciane")
        nome = _nome_no_disco(meta, fk)
        destino = f"{n:02d}_{_sem_indice(nome)}"
        movs.append((os.path.join(fk, nome), os.path.join(fj, destino)))
        metas_j_novas.append((meta, n, os.path.join(fj, destino)))
    restantes = [f for f in fids_k if f not in FIDS_ATRASADOS]
    ordenados = sorted(restantes, key=lambda f: (metas_k.get(f) or {}).get("indice") or 0)
    for n, fid in enumerate(ordenados, start=1):
        meta = metas_k.get(fid) or {"file_id": fid}
        nome = _nome_no_disco(meta, fk)
        destino = f"{n:02d}_{_sem_indice(nome)}"
        if destino != nome:
            movs.append((os.path.join(fk, nome), os.path.join(fk, destino)))
        metas_k_novas.append((meta, n, os.path.join(fk, destino)))
    # nenhum destino pode existir, a não ser que seja origem já movida antes no plano
    liberados = set()
    for origem, destino in movs:
        if os.path.exists(destino) and destino not in liberados:
            raise PlanoInseguro(f"destino já existe: {os.path.basename(destino)} — não sobrescrevo")
        liberados.add(origem)
    return {"movs": movs, "fids_j": fids_j + FIDS_ATRASADOS, "fids_k": ordenados,
            "metas_j": metas_j_novas, "metas_k": metas_k_novas}


def _meta_final(meta: dict, indice: int, caminho: str) -> dict:
    novo = copy.deepcopy(meta)
    novo.update({"indice": indice, "nome_salvo": os.path.basename(caminho), "caminho": caminho,
                 "tamanho_bits": os.path.getsize(caminho), "sha256": sha256_de(caminho),
                 "baixado": True, "erro": None, "fonte": FONTE})
    return novo


def executar(plano: dict, caminho: str) -> None:
    for origem, destino in plano["movs"]:
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        if os.path.exists(destino):
            raise PlanoInseguro(f"destino apareceu durante o reparo: {os.path.basename(destino)}")
        os.rename(origem, destino)
    metas_j = [_meta_final(m, i, c) for m, i, c in plano["metas_j"]]
    metas_k = [_meta_final(m, i, c) for m, i, c in plano["metas_k"]]   # sha256 fora da trava

    def mutar(estado):
        if ja_corrigido(estado):
            raise PlanoInseguro("outro processo já aplicou o reparo — estado não alterado")
        lotes = estado["lotes"]
        j, k = lotes[LOTE_JOSI], lotes[LOTE_KLIC]
        antigos_j = [m for m in (j.get("arquivos") or []) if str(m.get("file_id")) not in FIDS_ATRASADOS]
        j["arquivos"] = antigos_j + metas_j
        j["file_ids"] = plano["fids_j"]
        j["qtd_arquivos"] = len(j["arquivos"])
        k["arquivos"] = metas_k
        k["file_ids"] = plano["fids_k"]
        k["qtd_arquivos"] = len(metas_k)

    estado_store.fazer_backup(caminho, motivo="reparo_josilene")
    estado_store.atualizar_estado(mutar, caminho=caminho)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reparo pontual Josilene/Kliciane (04/11/2026).")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--confirmar", action="store_true", help="obrigatório junto com --aplicar")
    a = ap.parse_args(argv)
    caminho = caminho_estado()
    try:
        plano = planejar(estado_store.ler_estado(caminho))
    except estado_store.EstadoCorrompido as e:
        print(f"estado corrompido — nada feito: {e}")
        return 1
    except PlanoInseguro as e:
        print(f"ABORTADO (nada movido): {e}")
        return 2
    if plano is None:
        print("nada a mover: os 4 file_ids já estão no lote da Josilene (reparo já aplicado).")
        return 0
    print(f"renomeações: {len(plano['movs'])}")
    for o, d in plano["movs"]:
        print(f"   {os.path.basename(o)}  ->  {os.path.basename(os.path.dirname(d))}/{os.path.basename(d)}")
    if not a.aplicar:
        print("\n[SIMULAÇÃO] nada foi gravado. Use --aplicar --confirmar.")
        return 0
    if not a.confirmar:
        print("\n--aplicar exige --confirmar (script de reparo pontual). Nada foi feito.")
        return 2
    try:
        executar(plano, caminho)
    except PlanoInseguro as e:
        print(f"ABORTADO: {e}")
        return 2
    print(f"\nGRAVADO. Josilene com {len(plano['fids_j'])} arquivos, Kliciane com {len(plano['fids_k'])}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
