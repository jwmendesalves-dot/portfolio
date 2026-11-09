#!/usr/bin/env python3
"""Backfill PONTUAL: re-extrai o NOME dos lotes já fechados usando o parser corrigido.

Contexto (26/10/2026): o parser não descascava o prefixo "NOME DO CLIENTE" e o
`_e_nome_valido` validava o texto CRU. Resultado: lotes com 3 a 24 arquivos
baixados ficavam PRONTOS mas paravam em revisão ("nome não identificado"),
nunca chegando ao agente-separador.

Segurança (AUX-01 / A1 / F1-04):
  * padrão = simulação; gravar exige `--aplicar --confirmar`;
  * idempotente: lote com nome válido é mantido (2ª execução não grava nada);
  * não toca nome confirmado pelo CRM (`nome_origem` preenchido);
  * grava só `nome`/`nomes_candidatos` via `estado_store.atualizar_lotes(..., "identificacao")`;
  * backup por `estado_store.fazer_backup`, só quando há mudança.

Uso:
    python3 manutencao/backfill_nome_lotes.py                        # simulação
    python3 manutencao/backfill_nome_lotes.py --aplicar --confirmar
Saída: 0 ok; 1 estado corrompido; 2 --aplicar sem --confirmar.
"""
from __future__ import annotations

import argparse
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import caminhos  # noqa: E402
import estado_store  # noqa: E402
from nome_regras import normalizar_nome_candidato, validar_nome  # noqa: E402


def extrair_nomes(texto: str) -> list:
    """Parser da fase 1 (import tardio: triagem_grupo é pesado e de outra frente)."""
    import triagem_grupo as tg
    return tg.extrair_nomes(texto)


def nomes_das_mensagens(lote: dict) -> list:
    textos = []
    for m in sorted(lote.get("mensagens") or [], key=lambda x: x.get("id") or 0):
        t = (m.get("texto") or "").strip()
        if t:
            textos.append(t)
    return textos


def nome_do_lote(lote: dict) -> str:
    for texto in nomes_das_mensagens(lote):
        for cand in extrair_nomes(texto):
            c = normalizar_nome_candidato(cand)
            if validar_nome(c, exigir_completo=True):
                return c
    return ""


def planejar(estado: dict) -> tuple:
    mudancas, sem_nome, mantidos = {}, [], 0
    for lid, lote in (estado.get("lotes") or {}).items():
        if not isinstance(lote, dict):
            continue
        if lote.get("nome_origem") or validar_nome(normalizar_nome_candidato(lote.get("nome")),
                                                    exigir_completo=True):
            mantidos += 1
            continue
        novo = nome_do_lote(lote)
        if not novo:
            sem_nome.append(lid)
            continue
        patch = {"nome": novo}
        cands = [str(x) for x in (lote.get("nomes_candidatos") or [])]
        if novo.upper() not in {c.upper() for c in cands}:
            patch["nomes_candidatos"] = cands + [novo]
        mudancas[lid] = patch
    return mudancas, sem_nome, mantidos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill pontual de nome de lote.")
    ap.add_argument("--simular", action="store_true", help="padrão")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--confirmar", action="store_true", help="obrigatório junto com --aplicar")
    a = ap.parse_args(argv)
    caminho = os.environ.get("ESTADO_JSON") or caminhos.em_base("estado.json")
    try:
        mudancas, sem_nome, mantidos = planejar(estado_store.ler_estado(caminho))
    except estado_store.EstadoCorrompido as e:
        print(f"estado corrompido — nada feito: {e}")
        return 1
    print(f"  nome já válido (mantido): {mantidos}")
    print(f"  ganhariam nome agora    : {len(mudancas)}")
    print(f"  seguem sem nome         : {len(sem_nome)}")
    for lid in sorted(mudancas)[:25]:
        print(f"    {lid} -> {mudancas[lid]['nome']}")
    if not a.aplicar or a.simular:
        print("\n  SIMULAÇÃO: nada gravado. Use --aplicar --confirmar.")
        return 0
    if not a.confirmar:
        print("\n  --aplicar exige --confirmar (script de manutenção). Nada foi feito.")
        return 2
    if not mudancas:
        print("\n  nada a gravar.")
        return 0
    estado_store.fazer_backup(caminho, motivo="backfill_nome")
    conflitos = estado_store.atualizar_lotes(mudancas, "identificacao", caminho=caminho)
    print(f"\n  APLICADO em {len(mudancas) - len(conflitos)} lote(s); conflitos: {len(conflitos)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
