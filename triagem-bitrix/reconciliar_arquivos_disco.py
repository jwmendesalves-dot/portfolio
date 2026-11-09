#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reconcilia o estado com o DISCO: arquivo que já está na pasta do lote mas o
estado diz "não baixado" volta a ter caminho/sha256/baixado=True.

Motivo (04/11/2026): o ciclo do triagem_grupo re-montava a lista de arquivos de
TODO lote do histórico e, nos lotes adiados, gravava um placeholder vazio por cima
do que o baixar_pendentes já tinha registrado. Resultado: 337 lotes com o PDF no
disco e o painel achando que não tinha arquivo (caso Josilene).

Casa por índice (`NN_`) ou pelo nome já registrado no meta (`nome_salvo`/`nome_original`).
Mesmas garantias do reconciliar_estado (A1/E4): sha256 fora da trava, patch com dono
"download", parciais/0 byte ignorados, índice duplicado sem nome que desempate = ambíguo
(reportado, não escolhido — antes pegava "o mais recente" pela ordem alfabética).
Backup só por `estado_store.fazer_backup` e só quando há mudança (F1-04).

Uso:
  python3 reconciliar_arquivos_disco.py            # simulação (padrão)
  python3 reconciliar_arquivos_disco.py --aplicar  # grava (com backup do estado)
Saída: 0 ok; 1 estado corrompido.
"""
from __future__ import annotations

import argparse
import logging
import sys

import estado_store
import reconciliar_estado as rec

log = logging.getLogger("reconciliar_arquivos_disco")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcilia metas de arquivo com o disco (índice ou nome).")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--simular", action="store_true", help="padrão")
    args = ap.parse_args(argv)
    rec.configurar_log()
    caminho = rec.caminho_estado()
    try:
        estado = estado_store.ler_estado(caminho)
        mudancas, ambiguos = rec.planejar(estado, por_nome=True,
                                          fonte="reconciliado do disco (reconciliar_arquivos_disco)")
        lotes = estado.get("lotes") or {}
        n_arq = sum(len(p["arquivos"]) for p in mudancas.values())
        print(f"lotes corrigidos            : {len(mudancas)}")
        print(f"arquivos religados no estado: {n_arq}")
        print(f"sem associação (ambíguos)   : {len(ambiguos)}")
        for lid in sorted(mudancas)[:10]:
            print(f"   {lid} | {len(lotes[lid].get('arquivos') or [])} arquivo(s)")
        if len(mudancas) > 10:
            print(f"   ... e mais {len(mudancas) - 10} lote(s)")
        for lid, motivo in ambiguos[:20]:
            print(f"   ⚠ {lid}: {motivo}")
        if not args.aplicar or args.simular:
            print("\n[SIMULAÇÃO] nada foi gravado. Use --aplicar.")
            return 0
        if not mudancas:
            print("\nnada a gravar.")
            return 0
        conflitos = rec.aplicar(mudancas, caminho, backup_motivo="reconciliar_disco")
    except estado_store.EstadoCorrompido as e:
        log.error("estado corrompido — nada gravado: %s", e)
        return 1
    if conflitos:
        log.warning("campos preservados de outro dono: %s", conflitos)
    print("\nGRAVADO (backup em backups-estado/).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
