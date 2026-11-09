"""Reconcilia os METADADOS de arquivo a partir do disco, inclusive para lotes
cujo `arquivos` veio VAZIO (só file_ids) — caso do lote da Sabrina (27/10):
12 arquivos baixados na pasta, `arquivos: []`, e a ponte recusava com
"arquivo ausente no disco: None".

Associação file_id -> arquivo SÓ com prova (E5):
  1. `nome_salvo` do meta existente presente no disco;
  2. sha256 registrado no meta igual ao de um arquivo do disco;
  3. prefixo de índice `NN_` igual ao `indice` do meta (ou à posição em file_ids, que
     é a convenção do download, quando o meta não tem índice);
  4. `nome_original` do meta presente no disco.
Arquivo sem índice e sem prova NÃO é associado por ordem alfabética (o código antigo
dava o arquivo do file_id 1 ao file_id 3): fica sem associação e é reportado.
Metas de file_ids não resolvidos (com `erro`) são preservados: o patch tem dono
"download" e o merge do estado_store nunca remove meta que não veio no patch.
sha256 calculado FORA da trava (A1). Parciais/0 byte ignorados (E4).

Uso: python3 reconciliar_por_file_ids.py [LOTE_ID ...] [--dias N] [--aplicar]
Sem LOTE_ID: todos os lotes com file_ids e metadados incompletos. Sem --aplicar: simulação.
Saída: 0 ok; 1 estado corrompido.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys

import estado_store
import reconciliar_estado as rec

log = logging.getLogger("reconciliar_por_file_ids")
FONTE = "reconciliado por file_ids"


def _dentro_da_janela(lote: dict, limite: str | None) -> bool:
    return limite is None or str(lote.get("inicio") or "") >= limite


def planejar_lote(lid: str, lote: dict) -> tuple:
    """(patches, sem_prova: [nomes], nao_resolvidos: [file_ids])."""
    fids = [str(f) for f in (lote.get("file_ids") or [])]
    metas = {str(m.get("file_id")): m for m in (lote.get("arquivos") or [])
             if isinstance(m, dict) and m.get("file_id") not in (None, "")}
    pendentes = [(pos, fid) for pos, fid in enumerate(fids, 1) if not rec.ja_ok(metas.get(fid) or {})]
    if not pendentes:
        return [], [], []
    pasta = rec.pasta_do_lote(lid, lote)
    validos = rec.arquivos_validos(pasta)
    if not validos:
        return [], [], [fid for _, fid in pendentes]
    # arquivos já associados a outros file_ids do lote não podem ser reutilizados
    usados = {os.path.basename(str(m.get("caminho") or m.get("nome_salvo") or ""))
              for fid, m in metas.items() if rec.ja_ok(m)}
    livres = [n for n in validos if n not in usados]
    sha_cache: dict = {}

    def sha(nome):
        if nome not in sha_cache:
            sha_cache[nome] = rec.sha256_de(os.path.join(pasta, nome))
        return sha_cache[nome]

    por_indice: dict = {}
    for n in livres:
        i = rec.indice_do_nome(n)
        if i is not None:
            por_indice.setdefault(i, []).append(n)

    patches, resolvidos = [], set()
    for pos, fid in pendentes:
        meta = metas.get(fid) or {}
        escolhido = None
        if meta.get("nome_salvo") in livres:
            escolhido = meta["nome_salvo"]
        if escolhido is None and meta.get("sha256"):
            iguais = [n for n in livres if sha(n) == meta["sha256"]]
            escolhido = iguais[0] if len(iguais) == 1 else None
        if escolhido is None:
            idx = meta.get("indice") if isinstance(meta.get("indice"), int) else pos
            cands = por_indice.get(idx) or []
            escolhido = cands[0] if len(cands) == 1 else None
        if escolhido is None and meta.get("nome_original") in livres:
            escolhido = meta["nome_original"]
        if escolhido is None:
            continue
        livres.remove(escolhido)
        for lista in por_indice.values():
            if escolhido in lista:
                lista.remove(escolhido)
        base = {"file_id": fid, "indice": meta.get("indice") if meta.get("indice") is not None else pos}
        patches.append(rec.meta_do_disco(base, pasta, escolhido, FONTE))
        resolvidos.add(fid)
    sem_prova = [n for n in livres if rec.indice_do_nome(n) is None]
    nao_resolvidos = [fid for _, fid in pendentes if fid not in resolvidos]
    return patches, sem_prova, nao_resolvidos


def planejar(estado: dict, alvos=None, dias: int = 0) -> tuple:
    limite = None
    if dias:
        limite = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=dias)).isoformat()[:10]
    mudancas, relatorio = {}, []
    for lid, lote in (estado.get("lotes") or {}).items():
        if not isinstance(lote, dict) or (alvos and lid not in alvos):
            continue
        if not lote.get("file_ids") or not _dentro_da_janela(lote, limite):
            continue
        patches, sem_prova, nao_resolvidos = planejar_lote(lid, lote)
        if patches:
            mudancas[lid] = {"arquivos": patches}
        if patches or sem_prova or nao_resolvidos:
            relatorio.append({"lote": lid, "associados": len(patches), "file_ids": len(lote["file_ids"]),
                              "sem_prova": sem_prova, "nao_resolvidos": nao_resolvidos})
    return mudancas, relatorio


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcilia metas por file_ids (só com prova).")
    ap.add_argument("lotes", nargs="*")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--simular", action="store_true", help="padrão")
    ap.add_argument("--dias", type=int, default=0)
    a = ap.parse_args(argv)
    rec.configurar_log()
    caminho = rec.caminho_estado()
    try:
        estado = estado_store.ler_estado(caminho)
        mudancas, relatorio = planejar(estado, set(a.lotes) or None, a.dias)
        gravar = a.aplicar and not a.simular
        conflitos = rec.aplicar(mudancas, caminho) if gravar else {}
    except estado_store.EstadoCorrompido as e:
        log.error("estado corrompido — nada gravado: %s", e)
        return 1
    rotulo = "lotes reconciliados" if gravar else "[SIMULAÇÃO] lotes que seriam reconciliados"
    print(f"{rotulo}: {len(mudancas)}")
    for r in sorted(relatorio, key=lambda x: -x["associados"])[:20]:
        print(f"   {r['lote']} | associados {r['associados']}/{r['file_ids']}"
              f" | sem prova: {len(r['sem_prova'])} | file_ids sem arquivo: {len(r['nao_resolvidos'])}")
    if conflitos:
        log.warning("campos preservados de outro dono: %s", conflitos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
