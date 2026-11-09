#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Divide um lote MISTO em lotes de UM cliente cada, usando o KIT ASSINADO como corte.

Por que existe (caso real 24/09/2026): o grupo mandou 2-4 kits de clientes diferentes
no MESMO lote, sem traço e sem cabeçalho → o lote ficou `SEM_CPF` + `requer_revisao`
e a ponte (por desenho) NÃO encaminha → os documentos nunca chegaram ao painel.

Como corta: os arquivos chegam na ordem das mensagens e o último arquivo de cada
cliente é o PRÓPRIO KIT ASSINADO (o PDF traz o CPF do titular). Então cada bloco =
os arquivos até o kit, inclusive.

F2-11 (auditoria 04): na MESMA transação (`estado_store.atualizar_estado`):
  * cada sub-lote nasce com `dividido_de=<pai>` e só os file_ids/arquivos do seu bloco;
  * o pai fica `status=DIVIDIDO` (dono revisao, o ciclo não desfaz), `pronto=False` e
    `dividido_em=[sub-lotes]` (para `estado_lote` = DIVIDIDO, não enfileirável).
  * id do sub-lote = `<pai>-k<NN>` (índice do bloco; o CPF não vai mais no id/job/log).
  * idempotente: pai já dividido ou sub-lote existente -> nada é sobrescrito.
CPF do kit validado por `cpf_util.valido`; nome pelo `bitrix_cliente` (só se único).
Webhook pelo `seguranca` (não lê mais o .env de outro projeto).

Uso:
  dividir_lote_misto.py --lote LOTE-... [--lote LOTE-...]        # simula (padrão)
  dividir_lote_misto.py --lote LOTE-... --aplicar                # grava os sub-lotes
Saída: 0 ok; 1 erro (estado corrompido / transação recusada); 2 nada dividido.
"""
from __future__ import annotations

import argparse
import copy
import logging
import os
import sys

import cpf_util
import estado_lote
import estado_store
import seguranca

log = logging.getLogger("dividir_lote_misto")

CAMPO_CPF = "UF_CRM_1524762221558"
PREVSYNC_DIR = os.environ.get("PREVSYNC_DIR") or "/root/trampo/PrevSync-CRM"
NAO_COPIAR = {"_donos_campos", "_motivos_triagem", "motivos_resolvidos", "motivos_revisao",
              "requer_revisao", "pronto", "pronto_por", "cpf_sugerido", "cpf_sugerido_motivo",
              "cpf_origem", "nome_origem", "dividido_em", "status_anterior", "descartado_em",
              "lote_origem", "descarte_motivo"}
RECUSAR = frozenset({"DESCARTADO_DUPLICATA", "IGNORADO", "ERRO_PERMANENTE"})


class DivisaoRecusada(Exception):
    pass


def nome_do_cpf(cpf: str, _transporte=None) -> str:
    """Nome do titular no Bitrix (campo CPF). Mais de um nome distinto -> '' (não adivinha)."""
    import bitrix_cliente
    try:
        regs = bitrix_cliente.listar("crm.contact.list", {
            "filter": {CAMPO_CPF: cpf_util.formatar(cpf)}, "select": ["ID", "NAME", "LAST_NAME"]},
            limite=50, _transporte=_transporte)
    except Exception as e:  # noqa: BLE001 — nome é cosmético; a divisão segue sem ele
        log.warning("falha ao buscar nome do CPF %s: %s", cpf_util.mascarar(cpf), type(e).__name__)
        return ""
    nomes = {f"{r.get('NAME') or ''} {r.get('LAST_NAME') or ''}".strip().title() for r in regs}
    nomes.discard("")
    return nomes.pop() if len(nomes) == 1 else ""


def cpf_do_kit(caminho: str) -> str:
    """CPF do titular lido do PDF do kit (parser do PrevSync). Inválido -> ''."""
    if PREVSYNC_DIR not in sys.path:
        sys.path.insert(0, PREVSYNC_DIR)
    try:
        from app.services.zapsign_parser import ZapsignParser
        with open(caminho, "rb") as fh:
            dados, _ = ZapsignParser.parse_pdf(fh.read())
    except Exception:  # noqa: BLE001
        return ""
    cpf = cpf_util.normalizar((dados or {}).get("CPF") or "")
    return cpf if cpf and cpf_util.valido(cpf) else ""


def dividir(lote: dict, ler_cpf=cpf_do_kit) -> list:
    """Blocos de arquivos, um por cliente (corte = kit assinado)."""
    arquivos = sorted(lote.get("arquivos") or [], key=lambda a: a.get("indice") or 0)
    blocos, atual = [], []
    for a in arquivos:
        atual.append(a)
        nome = str(a.get("nome_original") or "").lower()
        if nome.startswith("kit") and nome.endswith(".pdf"):
            blocos.append({"arquivos": atual, "cpf": ler_cpf(a.get("caminho") or ""),
                           "kit": a.get("nome_original")})
            atual = []
    if atual:                      # sobra depois do último kit → vai para o último cliente
        if blocos:
            blocos[-1]["arquivos"] += atual
            blocos[-1]["sobra"] = len(atual)
        else:
            blocos.append({"arquivos": atual, "cpf": "", "kit": None})
    return blocos


def montar_sublote(pai_id: str, pai: dict, i: int, bloco: dict, nome: str) -> tuple:
    cpf = bloco["cpf"]
    sub_id = f"{pai_id}-k{i:02d}"
    sub = {k: copy.deepcopy(v) for k, v in pai.items() if k not in NAO_COPIAR}
    arquivos = copy.deepcopy(bloco["arquivos"])
    sub.update({
        "lote_id": sub_id,
        "arquivos": arquivos,
        "file_ids": [str(a["file_id"]) for a in arquivos if a.get("file_id") not in (None, "")],
        "qtd_arquivos": len(arquivos),
        "cpf": cpf,
        "cpf_formatado": cpf_util.formatar(cpf),
        "cpfs_distintos": [cpf],
        "nome": nome or None,
        "identificacao": "OK",
        "cpf_origem": "kit-assinado",
        "requer_revisao": False,
        "motivos_revisao": [],
        "pronto": True,
        "pronto_por": "divisão por kit assinado (um cliente por lote)",
        "status": "FECHADO_PRONTO",
        "fechado": True,
        "fechado_por": "divisão por kit assinado",
        "confianca": "ALTA (kit assinado identifica o titular)",
        "dividido_de": pai_id,
    })
    return sub_id, sub


def planejar(estado: dict, lotes_ids: list, ler_cpf=cpf_do_kit, buscar_nome=nome_do_cpf) -> dict:
    """{pai_id: {"subs": {id: sub}, "ignorados": [i], "motivo": str|None}} — fora da trava."""
    plano = {}
    for lid in lotes_ids:
        lote = (estado.get("lotes") or {}).get(lid)
        if not isinstance(lote, dict):
            plano[lid] = {"subs": {}, "ignorados": [], "motivo": "não está no estado"}
            continue
        logico = estado_lote.estado_logico(lote)
        if logico == "DIVIDIDO":
            plano[lid] = {"subs": {}, "ignorados": [], "motivo": "já dividido (idempotente)"}
            continue
        if logico in RECUSAR or lote.get("status") == "revisao_duplicata":
            plano[lid] = {"subs": {}, "ignorados": [], "motivo": f"estado {logico}/{lote.get('status')}: não divido"}
            continue
        subs, ignorados = {}, []
        for i, b in enumerate(dividir(lote, ler_cpf), 1):
            if not b["cpf"]:
                ignorados.append(i)        # sem CPF válido no kit: não adivinha
                continue
            sub_id, sub = montar_sublote(lid, lote, i, b, buscar_nome(b["cpf"]))
            subs[sub_id] = sub
        plano[lid] = {"subs": subs, "ignorados": ignorados,
                      "motivo": None if len(subs) >= 1 else "nenhum bloco com CPF válido"}
    return plano


def aplicar(plano: dict, caminho: str | None = None) -> dict:
    """Uma transação: cria os sub-lotes e marca o pai DIVIDIDO. Devolve {pai: [subs]}."""
    feitos: dict = {}

    def mutar(estado):
        feitos.clear()
        lotes = estado.setdefault("lotes", {})
        for pai_id, p in plano.items():
            if not p["subs"]:
                continue
            pai = lotes.get(pai_id)
            if not isinstance(pai, dict) or estado_lote.estado_logico(pai) == "DIVIDIDO":
                continue                                  # sumiu / já dividido por outro
            for sub_id, sub in p["subs"].items():
                if sub_id not in lotes:                   # nunca sobrescreve
                    lotes[sub_id], _ = estado_store.mesclar_lote({}, sub, "triagem")
            ids = sorted(p["subs"])
            novo, _ = estado_store.mesclar_lote(pai, {"dividido_em": ids}, "triagem")
            novo, _ = estado_store.mesclar_lote(novo, {"status": "DIVIDIDO", "pronto": False}, "revisao")
            if estado_lote.estado_logico(novo) != "DIVIDIDO" or estado_lote.pode_enfileirar(novo)[0]:
                raise DivisaoRecusada(f"{pai_id}: pai não ficou DIVIDIDO — transação abortada")
            lotes[pai_id] = novo
            feitos[pai_id] = ids

    estado_store.atualizar_estado(mutar, caminho=caminho)
    return dict(feitos)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Divide lote misto por kit assinado.")
    p.add_argument("--lote", action="append", default=[])
    p.add_argument("--aplicar", action="store_true")
    p.add_argument("--simular", action="store_true", help="padrão")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    seguranca.instalar_filtro_log()
    caminho = os.environ.get("ESTADO_JSON") or None
    try:
        plano = planejar(estado_store.ler_estado(caminho), args.lote)
    except estado_store.EstadoCorrompido as e:
        log.error("estado corrompido: %s", e)
        return 1
    total = 0
    for lid, pl in plano.items():
        resumo = pl["motivo"] or "%d sub-lote(s)" % len(pl["subs"])
        print(f"══ {lid}: {resumo}")
        for sub_id, sub in pl["subs"].items():
            print(f"   → {sub_id}: {sub['qtd_arquivos']} arq | CPF {cpf_util.mascarar(sub['cpf'])}"
                  f" | {sub.get('nome') or '(nome não achado no CRM)'}")
        if pl["ignorados"]:
            print(f"   ⚠ bloco(s) {pl['ignorados']} sem CPF válido no kit — NÃO subo (não dá para adivinhar)")
        total += len(pl["subs"])
    if not args.aplicar or args.simular:
        print(f"\n(simulação) {total} sub-lote(s) seriam gravados. Rode com --aplicar para valer.")
        return 0 if total else 2
    if not total:
        return 2
    try:
        feitos = aplicar(plano, caminho)
    except (DivisaoRecusada, estado_store.EstadoCorrompido) as e:
        log.error("nada gravado: %s", e)
        return 1
    for pai, subs in feitos.items():
        print(f"✅ {pai} → DIVIDIDO em {', '.join(subs)}")
    return 0 if feitos else 2


if __name__ == "__main__":
    sys.exit(main())
