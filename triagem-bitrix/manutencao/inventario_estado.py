#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inventario_estado.py — inventário agregado do estado da triagem (baseline sem PII).

Ferramenta da "Etapa B — Baseline" do plano de implantação. É SOMENTE LEITURA:
não grava nada, não chama rede, não imprime nome, CPF, telefone, senha nem texto
de mensagem — só CONTAGENS.

Lê o estado com `estado_store.ler_estado` (estado corrompido -> código de saída 2
e mensagem clara; jamais vira `{}`). Usa os módulos compartilhados para tudo que
tem fonte única: `cpf_util.valido` (CPF), `tipos_documento.classificar` (tipo de
arquivo), `estado_lote.estado_logico`/`pode_enfileirar` (estados), `seguranca`
(máscara e detecção de senha), `estado_store._listar_backups` (backups).

Uso:
  manutencao/inventario_estado.py [--estado CAMINHO] [--fila-enviados CAMINHO]
      [--download-fila CAMINHO] [--lotes-dir CAMINHO] [--top N] [--json]

Saída (texto ou --json): apenas contagens.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import caminhos  # noqa: E402
import cpf_util  # noqa: E402
import estado_lote  # noqa: E402
import estado_store  # noqa: E402
import seguranca  # noqa: E402
import tipos_documento  # noqa: E402

# Sufixos de arquivo parcial — mesmos de estado_lote.SUFIXOS_PARCIAIS.
SUFIXOS_PARCIAIS = (".part", ".parcial", ".tmp")


# ---------------------------------------------------------------------------
# normalização (mascara PII e colapsa detalhes voláteis)
# ---------------------------------------------------------------------------
def _normalizar_erro(erro) -> str:
    """Versão segura e estável de um texto de erro para agrupar por contagem."""
    if erro is None or str(erro).strip() == "":
        return "(sem erro registrado)"
    s = seguranca.mascarar(str(erro))
    s = re.sub(r"\b\d{4,}\b", "<n>", s)          # ids / timestamps / tamanhos
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80] if s else "(sem erro registrado)"


def _norm_fonte(fonte) -> str:
    if fonte is None or str(fonte).strip() == "":
        return "(sem fonte)"
    s = seguranca.mascarar(str(fonte))
    s = re.sub(r"\b\d{4,}\b", "<n>", s)
    return s.strip()[:80]


def _mascarar_rotulo(v) -> str:
    """Mascara um rótulo livre (motivo, origem) antes de virar chave de contagem."""
    s = seguranca.mascarar(str(v))
    s = re.sub(r"\b\d{4,}\b", "<n>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80] if s else "(vazio)"


def _nome_do_arquivo(meta: dict) -> str:
    nome = meta.get("nome_original") or meta.get("nome_salvo") or ""
    if not nome and meta.get("caminho"):
        nome = os.path.basename(str(meta["caminho"]))
    return str(nome or "")


def _texto_das_mensagens(lote: dict) -> str:
    textos = []
    for m in (lote.get("mensagens") or []):
        if isinstance(m, dict) and m.get("texto"):
            textos.append(str(m["texto"]))
    return "\n".join(textos)


def _contar_backups(caminho_estado: str) -> dict:
    """(quantidade, tamanho total em bytes) dos backups `estado.json.bak-*`."""
    try:
        backups = estado_store._listar_backups(caminho_estado)
    except Exception:
        backups = []
    total_bytes = 0
    for _ts, cam in backups:
        try:
            total_bytes += os.path.getsize(cam)
        except OSError:
            pass
    return {"quantidade": len(backups), "tamanho_bytes": total_bytes}


def _varrer_lotes_dir(lotes_dir: str | None) -> dict:
    """Conta `.part`/`.parcial`/`.tmp` e arquivos de 0 byte em `lotes_dir`."""
    parciais = 0
    zero_bytes = 0
    if not lotes_dir:
        lotes_dir = caminhos.em_base("lotes")
    if not os.path.isdir(lotes_dir):
        return {"parciais": parciais, "zero_bytes": zero_bytes}
    for raiz, _dirs, arquivos in os.walk(lotes_dir):
        for nome in arquivos:
            if nome.endswith(SUFIXOS_PARCIAIS):
                parciais += 1
            try:
                if os.path.getsize(os.path.join(raiz, nome)) == 0:
                    zero_bytes += 1
            except OSError:
                pass
    return {"parciais": parciais, "zero_bytes": zero_bytes}


def _resumo_download_fila(caminho: str) -> dict | None:
    """Resumo SÓ de contagens da fila de download (estados + erros normalizados)."""
    try:
        with open(caminho, encoding="utf-8") as fh:
            dados = json.load(fh)
    except (OSError, ValueError):
        return None
    itens = dados.get("itens") if isinstance(dados, dict) else dados
    if not isinstance(itens, list):
        itens = []
    pendentes = falhos = baixados = 0
    erros = Counter()
    for it in itens:
        if not isinstance(it, dict):
            continue
        estado = str(it.get("estado") or "")
        if estado == "baixado":
            baixados += 1
        elif estado == "falha":
            falhos += 1
        else:
            pendentes += 1
        if estado != "baixado" and it.get("erro"):
            erros[_normalizar_erro(it["erro"])] += 1
    return {"tamanho": len(itens), "pendentes": pendentes, "falhos": falhos,
            "baixados": baixados, "erros_por_normalizado": dict(erros)}


# ---------------------------------------------------------------------------
# inventário
# ---------------------------------------------------------------------------
def inventariar(estado, fila_enviados=None, download_fila=None,
                lotes_dir=None, top=10, caminho_estado=None) -> dict:
    """Devolve o dict de CONTAGENS (sem PII) do estado e das fontes opcionais."""
    lotes = (estado or {}).get("lotes") or {}
    if not isinstance(lotes, dict):
        lotes = {}

    por_status = Counter()
    por_identificacao = Counter()
    por_estado_logico = Counter()

    pronto_revisao = pronto_sem = nao_pronto_revisao = nao_pronto_sem = 0
    motivos_revisao = Counter()
    lotes_sem_arquivo = 0
    arquivos_por_tipo = Counter()

    total_nao_baixados = 0
    nao_baixados_fonte = Counter()
    nao_baixados_erro = Counter()

    ids_telegram = set()          # file_ids legados DISTINTOS (CQA...)
    cpf_origem_total = 0
    cpf_origem_por = Counter()
    cpfs_invalidos = 0
    lotes_com_senha = 0

    pode_enfileirar_aprovados = 0
    motivos_bloqueio = Counter()

    controle_por_lote = fila_enviados if isinstance(fila_enviados, dict) else {}

    for lote_id, lote in lotes.items():
        if not isinstance(lote, dict):
            continue

        por_status[str(lote.get("status") or "(vazio)")] += 1
        por_identificacao[str(lote.get("identificacao") or "(vazio)")] += 1
        por_estado_logico[estado_lote.estado_logico(lote, controle_por_lote.get(lote_id))] += 1

        pronto = bool(lote.get("pronto"))
        requer = bool(lote.get("requer_revisao"))
        if pronto and requer:
            pronto_revisao += 1
        elif pronto:
            pronto_sem += 1
        elif requer:
            nao_pronto_revisao += 1
        else:
            nao_pronto_sem += 1

        for m in (lote.get("motivos_revisao") or []):
            motivos_revisao[_mascarar_rotulo(m)] += 1

        arquivos = [a for a in (lote.get("arquivos") or []) if isinstance(a, dict)]
        if not arquivos:
            lotes_sem_arquivo += 1
        for a in arquivos:
            tipo = tipos_documento.classificar(_nome_do_arquivo(a), a.get("mime"))
            arquivos_por_tipo[tipo] += 1
            if a.get("baixado") is not True:
                total_nao_baixados += 1
                nao_baixados_fonte[_norm_fonte(a.get("fonte"))] += 1
                nao_baixados_erro[_normalizar_erro(a.get("erro"))] += 1
            fid = a.get("file_id")
            if fid and str(fid).startswith("CQA"):
                ids_telegram.add(str(fid))

        for f in (lote.get("file_ids") or []):
            if str(f).startswith("CQA"):
                ids_telegram.add(str(f))

        co = lote.get("cpf_origem")
        if co:
            cpf_origem_total += 1
            cpf_origem_por[_mascarar_rotulo(co)] += 1

        cpf = lote.get("cpf")
        if cpf is not None and str(cpf).strip() != "" and not cpf_util.valido(cpf):
            cpfs_invalidos += 1

        texto = _texto_das_mensagens(lote)
        if texto and seguranca.remover_senhas(texto) != texto:
            lotes_com_senha += 1

        pode, motivos = estado_lote.pode_enfileirar(lote)
        if pode:
            pode_enfileirar_aprovados += 1
        else:
            for m in motivos:
                motivos_bloqueio[_mascarar_rotulo(m)] += 1

    inv = {
        "total_lotes": len(lotes),
        "por_status": dict(por_status),
        "por_identificacao": dict(por_identificacao),
        "por_estado_logico": dict(por_estado_logico),
        "pronto_x_requer_revisao": {
            "pronto_revisao": pronto_revisao,
            "pronto_sem_revisao": pronto_sem,
            "nao_pronto_revisao": nao_pronto_revisao,
            "nao_pronto_sem_revisao": nao_pronto_sem,
        },
        "motivos_revisao_top": motivos_revisao.most_common(top),
        "lotes_sem_arquivo": lotes_sem_arquivo,
        "arquivos_por_tipo": dict(arquivos_por_tipo),
        "nao_baixados": {
            "total": total_nao_baixados,
            "por_fonte": dict(nao_baixados_fonte),
            "por_erro_normalizado": dict(nao_baixados_erro),
        },
        "ids_telegram_legados": len(ids_telegram),
        "cpf_origem": {"total": cpf_origem_total, "por_origem": dict(cpf_origem_por)},
        "cpfs_invalidos": cpfs_invalidos,
        "lotes_com_senha_no_texto": lotes_com_senha,
        "pode_enfileirar": {
            "aprovados": pode_enfileirar_aprovados,
            "motivos_bloqueio": dict(motivos_bloqueio),
        },
    }

    if download_fila:
        inv["download_fila"] = _resumo_download_fila(download_fila)
    if lotes_dir is not None or caminhos.em_base("lotes"):
        inv["arquivos_parciais"] = _varrer_lotes_dir(lotes_dir)
    if caminho_estado:
        inv["backups_estado"] = _contar_backups(caminho_estado)

    return inv


# ---------------------------------------------------------------------------
# saída
# ---------------------------------------------------------------------------
def _imprimir_texto(inv: dict) -> None:
    print(f"total de lotes: {inv['total_lotes']}")

    print("por status:")
    for k, v in sorted(inv["por_status"].items()):
        print(f"  {k}: {v}")

    print("por identificacao:")
    for k, v in sorted(inv["por_identificacao"].items()):
        print(f"  {k}: {v}")

    print("por estado logico:")
    for k, v in sorted(inv["por_estado_logico"].items()):
        print(f"  {k}: {v}")

    pr = inv["pronto_x_requer_revisao"]
    print("pronto x requer_revisao:")
    print(f"  pronto + revisao: {pr['pronto_revisao']}")
    print(f"  pronto sem revisao: {pr['pronto_sem_revisao']}")
    print(f"  nao pronto + revisao: {pr['nao_pronto_revisao']}")
    print(f"  nao pronto sem revisao: {pr['nao_pronto_sem_revisao']}")

    print(f"motivos_revisao (top {len(inv['motivos_revisao_top'])}):")
    for motivo, n in inv["motivos_revisao_top"]:
        print(f"  {motivo}: {n}")

    print(f"lotes sem arquivo: {inv['lotes_sem_arquivo']}")

    print("arquivos por tipo:")
    for k, v in sorted(inv["arquivos_por_tipo"].items()):
        print(f"  {k}: {v}")

    print(f"nao baixados (total): {inv['nao_baixados']['total']}")
    print("  por fonte:")
    for k, v in sorted(inv["nao_baixados"]["por_fonte"].items()):
        print(f"    {k}: {v}")
    print("  por erro normalizado:")
    for k, v in sorted(inv["nao_baixados"]["por_erro_normalizado"].items()):
        print(f"    {k}: {v}")

    print(f"ids Telegram legados (CQA...): {inv['ids_telegram_legados']}")

    print(f"cpf_origem (total): {inv['cpf_origem']['total']}")
    for k, v in sorted(inv["cpf_origem"]["por_origem"].items()):
        print(f"  {k}: {v}")

    print(f"CPFs invalidos: {inv['cpfs_invalidos']}")
    print(f"lotes com senha no texto: {inv['lotes_com_senha_no_texto']}")

    print(f"pode_enfileirar aprovados: {inv['pode_enfileirar']['aprovados']}")
    print("motivos de bloqueio:")
    for k, v in sorted(inv["pode_enfileirar"]["motivos_bloqueio"].items()):
        print(f"  {k}: {v}")

    if "download_fila" in inv and inv["download_fila"]:
        df = inv["download_fila"]
        print(f"download_fila: tamanho={df['tamanho']} pendentes={df['pendentes']} "
              f"falhos={df['falhos']} baixados={df['baixados']}")

    if "arquivos_parciais" in inv:
        ap = inv["arquivos_parciais"]
        print(f"arquivos parciais (.part/.parcial/.tmp): {ap['parciais']}")
        print(f"arquivos de 0 byte: {ap['zero_bytes']}")

    if "backups_estado" in inv:
        b = inv["backups_estado"]
        print(f"backups estado.json.bak-*: {b['quantidade']} "
              f"({b['tamanho_bytes']} bytes)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Inventário agregado do estado da triagem (só contagens, sem PII).")
    p.add_argument("--estado", default=None,
                   help="caminho do estado.json (padrão: caminhos.em_base('estado.json'))")
    p.add_argument("--fila-enviados", default=None,
                   help="caminho do fila-enviados.json (opcional; enriquece estado lógico)")
    p.add_argument("--download-fila", default=None,
                   help="caminho do download_fila.json (opcional)")
    p.add_argument("--lotes-dir", default=None,
                   help="diretório dos lotes baixados (opcional; varre .part/0 byte)")
    p.add_argument("--top", type=int, default=10,
                   help="quantos motivos de revisão no top (padrão 10)")
    p.add_argument("--json", action="store_true", help="saída JSON em vez de texto")
    a = p.parse_args(argv)

    caminho_estado = a.estado or caminhos.em_base("estado.json")

    try:
        estado = estado_store.ler_estado(caminho_estado)
    except estado_store.EstadoCorrompido as e:
        print(f"erro: estado corrompido — {e}", file=sys.stderr)
        if getattr(e, "copia", None):
            print(f"  (cópia preservada em {e.copia})", file=sys.stderr)
        return 2

    fila_enviados = None
    if a.fila_enviados:
        try:
            with open(a.fila_enviados, encoding="utf-8") as fh:
                fila_enviados = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"aviso: não consegui ler fila-enviados ({type(e).__name__}); ignorando",
                  file=sys.stderr)
            fila_enviados = None

    inv = inventariar(estado, fila_enviados=fila_enviados,
                      download_fila=a.download_fila, lotes_dir=a.lotes_dir,
                      top=a.top, caminho_estado=caminho_estado)

    if a.json:
        json.dump(inv, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        _imprimir_texto(inv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
