#!/usr/bin/env python3
"""
Reconcilia o estado.json com o que existe de fato no disco.

Motivo (21/10/2026): o baixar_pendentes.py gravou os arquivos direto na pasta
do lote, mas o estado.json continuou com os metadados antigos
(nome_salvo=null, caminho=null, baixado=false) porque o ciclo do
triagem_grupo nunca chegou a completar aquele download. O lote_para_fila lê o
ESTADO (não o disco), então via "arquivo ausente no disco" e mandava
0 arquivos para o staging — o lote nunca chegava na ponte.

Este script varre os lotes, casa o prefixo do índice (01_, 02_, ...) com o
meta correspondente, calcula sha256/tamanho e marca baixado=True.

Correções (auditoria A1, A2, E4, OP-C1):
  * sha256/tamanho calculados FORA da trava; a gravação é um patch por campo com
    dono "download" (`estado_store.atualizar_lotes`) — não regrava o estado inteiro;
  * ignora `.part`/`.parcial`/`.tmp`/`.meta.json`, ocultos e arquivos de 0 byte (E4);
  * dois arquivos com o mesmo índice e nenhum nome registrado que desempate -> não
    escolhe: reporta como ambíguo.

Uso:
  python3 reconciliar_estado.py [LOTE_ID] [--simular]
  (sem --simular grava, como antes: é o passo do pipeline de 10 min)
Saída: 0 ok; 1 estado corrompido.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys

import caminhos
import estado_store
import seguranca

log = logging.getLogger("reconciliar_estado")

SUFIXOS_INVALIDOS = (".part", ".parcial", ".tmp", ".meta.json")


def caminho_estado() -> str:
    return os.environ.get("ESTADO_JSON") or caminhos.em_base("estado.json")


def sha256_de(caminho: str) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def arquivo_valido(nome: str, caminho: str) -> bool:
    """E4: só conta arquivo completo — nada de parcial/temporário/oculto/0 byte."""
    n = nome.lower()
    if n.startswith(".") or n.endswith(SUFIXOS_INVALIDOS) or ".tmp." in n or ".part." in n:
        return False
    try:
        return os.path.isfile(caminho) and os.path.getsize(caminho) > 0
    except OSError:
        return False


def indice_do_nome(nome: str):
    try:
        return int(nome.split("_", 1)[0])
    except ValueError:
        return None


def arquivos_validos(pasta: str) -> list:
    try:
        nomes = sorted(os.listdir(pasta))
    except OSError:
        return []
    return [n for n in nomes if arquivo_valido(n, os.path.join(pasta, n))]


def pasta_do_lote(lid: str, lote: dict) -> str:
    return lote.get("pasta") or caminhos.em_base("lotes", lid)


def ja_ok(meta: dict) -> bool:
    return bool(meta.get("baixado") and meta.get("caminho") and os.path.exists(meta["caminho"]))


def meta_do_disco(meta: dict, pasta: str, nome: str, fonte: str) -> dict:
    """Patch de UM arquivo (campos de download + chave de casamento)."""
    cam = os.path.join(pasta, nome)
    patch = {"nome_salvo": nome, "caminho": cam, "tamanho_bits": os.path.getsize(cam),
             "sha256": sha256_de(cam), "baixado": True, "erro": None, "fonte": fonte}
    if meta.get("file_id") not in (None, ""):
        patch["file_id"] = meta["file_id"]
    if meta.get("indice") is not None:
        patch["indice"] = meta["indice"]
    return patch


def escolher(meta: dict, validos: list, por_nome: bool):
    """(nome | None, motivo). Prova: índice único, ou nome registrado no meta."""
    registrados = [meta.get("nome_salvo")] + ([meta.get("nome_original")] if por_nome else [])
    idx = meta.get("indice")
    candidatos = [n for n in validos if isinstance(idx, int) and indice_do_nome(n) == idx]
    for r in registrados:
        if r and r in candidatos:
            return r, ""
    if len(candidatos) == 1:
        return candidatos[0], ""
    if len(candidatos) > 1:
        return None, f"índice {idx}: {len(candidatos)} arquivos no disco, nenhum nome registrado desempata"
    if por_nome:
        for r in registrados:
            if r and r in validos:
                return r, ""
    return None, ""


def planejar(estado: dict, somente=None, por_nome: bool = False,
             fonte: str = "reconciliado do disco") -> tuple:
    """Calcula (fora da trava) {lote_id: {"arquivos": [patches]}} e a lista de ambíguos."""
    mudancas, ambiguos = {}, []
    for lid, lote in (estado.get("lotes") or {}).items():
        if somente and lid not in somente:
            continue
        if not isinstance(lote, dict):
            continue
        metas = [m for m in (lote.get("arquivos") or []) if isinstance(m, dict)]
        if not metas:
            continue
        pasta = pasta_do_lote(lid, lote)
        validos = arquivos_validos(pasta)
        if not validos:
            continue
        patches = []
        for meta in metas:
            if ja_ok(meta):
                continue
            nome, motivo = escolher(meta, validos, por_nome)
            if motivo:
                ambiguos.append((lid, motivo))
            if nome:
                patches.append(meta_do_disco(meta, pasta, nome, fonte))
        if patches:
            mudancas[lid] = {"arquivos": patches}
    return mudancas, ambiguos


def aplicar(mudancas: dict, caminho: str, backup_motivo: str = "") -> dict:
    """Grava com dono 'download'. Backup (se pedido) só quando há mudança real."""
    if not mudancas:
        return {}
    if backup_motivo:
        estado_store.fazer_backup(caminho, motivo=backup_motivo)
    return estado_store.atualizar_lotes(mudancas, "download", caminho=caminho)


def configurar_log() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    seguranca.instalar_filtro_log()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcilia metas de arquivo com o disco (por índice).")
    ap.add_argument("lote", nargs="?", help="só este lote")
    ap.add_argument("--simular", action="store_true", help="não grava")
    a = ap.parse_args(argv)
    configurar_log()
    caminho = caminho_estado()
    try:
        estado = estado_store.ler_estado(caminho)
        mudancas, ambiguos = planejar(estado, somente={a.lote} if a.lote else None)
        conflitos = {} if a.simular else aplicar(mudancas, caminho)
    except estado_store.EstadoCorrompido as e:
        log.error("estado corrompido — nada gravado: %s", e)
        return 1
    n_arq = sum(len(p["arquivos"]) for p in mudancas.values())
    for lid, motivo in ambiguos:
        log.warning("%s: sem associação — %s", lid, motivo)
    if conflitos:
        log.warning("campos preservados de outro dono: %s", conflitos)
    print(f"lotes ajustados: {len(mudancas)} | arquivos marcados como baixados: {n_arq}"
          f" | ambíguos: {len(ambiguos)}{' [SIMULAÇÃO]' if a.simular else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
