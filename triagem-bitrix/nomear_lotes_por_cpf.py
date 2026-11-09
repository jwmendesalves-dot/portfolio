#!/usr/bin/env python3
"""Nomeia lotes pelo CPF consultando o Bitrix (contatos e depois negocios).

Uso:
    python3 nomear_lotes_por_cpf.py --simular
    python3 nomear_lotes_por_cpf.py --aplicar

Regra: só grava nome que veio do CRM, que passe no validar_nome (exigir_completo)
e que seja ÚNICO entre todos os registros com aquele CPF (CPF duplicado no CRM
com nomes diferentes = ambíguo = não grava). Nada de chute.

Campo do CPF no Bitrix: UF_CRM_1524762221558 (contato) — testa 2 formatos
(só dígitos e formatado), mesma estratégia do PrevSync.

Remediação (ID-09 / AUX-02 / OP-T3):
  - importar este módulo NÃO resolve webhook nem encerra o processo; a REST vai
    pelo cliente único `bitrix_cliente` (webhook lido só na hora da chamada);
  - `bitrix_cliente.listar` lê TODAS as páginas e o nome tem de ser único;
  - "não achei" expira (TTL do cache negativo); falha de rede NÃO vira "não achei";
  - gravação via `estado_store.atualizar_lotes(..., dono="identificacao")`
    (trava + merge por campo; nada de json.dump direto no estado.json).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata

import caminhos
import cpf_util
from nome_regras import nomes_equivalentes, normalizar_nome_candidato, validar_nome  # noqa: E402

CACHE = os.environ.get("NOMEAR_CACHE") or caminhos.em_base(".cache_cpf_nome.json")
CAMPO_CPF = "UF_CRM_1524762221558"
DONO = "identificacao"
# "não achei" vale por pouco tempo (o contato pode ser criado amanhã); nome achado, 30 dias.
TTL_NEGATIVO_S = float(os.environ.get("NOMEAR_TTL_NEGATIVO_H", "72")) * 3600
TTL_POSITIVO_S = 30 * 86400


class ConsultaFalhou(RuntimeError):
    """A consulta ao Bitrix falhou (rede/limite/disjuntor/sem webhook) — não é "não achei"."""


def sem_acento(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def formatar_cpf(d: str) -> str:
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else d


def _chave_nome(nome: str) -> str:
    return " ".join(sem_acento(nome).upper().split())


def nome_do_registro(reg: dict) -> str:
    """Monta o nome a partir das partes do Bitrix (LAST_NAME traz o resto)."""
    partes = [reg.get("NAME"), reg.get("SECOND_NAME"), reg.get("LAST_NAME")]
    bruto = " ".join(p for p in partes if p).strip()
    bruto = re.sub(r"\s+", " ", bruto)
    canonico = normalizar_nome_candidato(bruto)
    return canonico if validar_nome(canonico, exigir_completo=True) else ""


def nome_do_deal(reg: dict) -> str:
    titulo = re.sub(r"(?i)^\s*(previ|prev)\s+(adm|jud)\s*[-–:]\s*", "", reg.get("TITLE") or "")
    nome = normalizar_nome_candidato(titulo)
    return nome if validar_nome(nome, exigir_completo=True) else ""


def _listar(metodo: str, params: dict) -> list:
    try:
        import bitrix_cliente  # noqa: PLC0415  (import tardio: nada acontece na importação)
        return bitrix_cliente.listar(metodo, params)
    except Exception as e:  # noqa: BLE001  (FalhaBitrix, CircuitOpenError, SegredoAusente...)
        raise ConsultaFalhou(type(e).__name__) from None


def _nome_unico(registros: list, extrair) -> tuple[str, str]:
    """(nome, origem). Nome só quando TODOS os registros com nome válido concordam.

    "Concordar" = `nome_regras.nomes_equivalentes` com o representante do grupo
    (partícula/abreviação: "MARIA DE SOUZA" ≡ "MARIA SOUZA"). Dois grupos = ambíguo.
    O representante é a grafia mais longa (por extenso) do grupo.
    """
    grupos: list = []      # [[nome_representante, id], ...]
    for reg in sorted(registros, key=lambda r: int(r.get("ID") or 0) if str(r.get("ID") or "").isdigit() else 0):
        nome = extrair(reg)
        if not nome:
            continue
        for g in grupos:
            if _chave_nome(g[0]) == _chave_nome(nome) or nomes_equivalentes(g[0], nome):
                if len(nome) > len(g[0]):
                    g[0] = nome
                break
        else:
            grupos.append([nome, reg.get("ID")])
    if len(grupos) == 1:
        return grupos[0][0], str(grupos[0][1])
    if len(grupos) > 1:
        return "", f"ambiguo ({len(grupos)} nomes)"
    return "", ""


def buscar_cpf(cpf_digitos: str) -> tuple[str, str]:
    """(nome, origem) — origem: 'contato <id>' | 'deal <id>' | 'ambiguo …' | ''.

    Levanta ConsultaFalhou se o Bitrix não respondeu (o chamador NÃO deve guardar
    isso como "não achei").
    """
    variantes = [cpf_digitos, formatar_cpf(cpf_digitos)]
    contatos: dict = {}
    for valor in variantes:
        for reg in _listar("crm.contact.list", {
                "filter": {CAMPO_CPF: valor}, "select": ["ID", "NAME", "SECOND_NAME", "LAST_NAME"]}):
            if isinstance(reg, dict):
                contatos[str(reg.get("ID"))] = reg
    nome, rid = _nome_unico(list(contatos.values()), nome_do_registro)
    if nome:
        return nome, f"contato {rid}"
    if rid.startswith("ambiguo"):
        return "", f"contatos {rid}"
    # fallback: o nome pode existir só no TITLE do negócio ("PREVI ADM - FULANO")
    deals: dict = {}
    for valor in variantes:
        for reg in _listar("crm.deal.list", {
                "filter": {CAMPO_CPF: valor}, "select": ["ID", "TITLE", "CONTACT_ID"]}):
            if isinstance(reg, dict):
                deals[str(reg.get("ID"))] = reg
    nome, rid = _nome_unico(list(deals.values()), nome_do_deal)
    if nome:
        return nome, f"deal {rid}"
    if rid.startswith("ambiguo"):
        return "", f"deals {rid}"
    return "", ""


# --------------------------------------------------------------------------- cache
def ler_cache(caminho: str | None = None) -> dict:
    import estado_store  # noqa: PLC0415
    caminho = caminho or CACHE
    try:
        bruto = estado_store.ler_estado(caminho)
    except Exception:  # noqa: BLE001  (cache corrompido: recomeça; o estado_store preserva a cópia)
        return {}
    cache = {}
    for cpf, v in (bruto or {}).items():
        if isinstance(v, dict):
            cache[cpf] = v
        elif isinstance(v, str) and v:
            cache[cpf] = {"nome": v, "origem": "cache-legado", "em": time.time()}
        # formato antigo com "" (negativo eterno) é descartado: consulta de novo
    return cache


def do_cache(cache: dict, cpf: str, agora: float | None = None) -> dict | None:
    """Entrada válida do cache ou None (ausente/expirada)."""
    ent = cache.get(cpf)
    if not isinstance(ent, dict):
        return None
    agora = time.time() if agora is None else agora
    ttl = TTL_POSITIVO_S if ent.get("nome") else TTL_NEGATIVO_S
    if agora - float(ent.get("em") or 0) > ttl:
        return None
    return ent


def gravar_cache(cache: dict, caminho: str | None = None) -> None:
    import estado_store  # noqa: PLC0415
    estado_store.gravar_atomico(cache, caminho or CACHE)


# --------------------------------------------------------------------------- alvos
def escolher_alvos(lotes: dict) -> tuple[list, int]:
    """PRONTOS, COM ARQUIVO, sem nome válido e com exatamente 1 CPF válido.

    Sem esse filtro o script gasta requisição nomeando lote que nunca vai subir
    (lote só de texto / não fechado).
    """
    alvos: list = []
    ignorados_sem_arquivo = 0
    for lid, v in lotes.items():
        if not isinstance(v, dict):
            continue
        if validar_nome(normalizar_nome_candidato(v.get("nome")), exigir_completo=True):
            continue
        if not (v.get("pronto") and (v.get("arquivos") or [])):
            ignorados_sem_arquivo += 1
            continue
        ids = [cpf_util.normalizar(c) for c in (v.get("cpfs_distintos") or []) if c]
        if not ids and v.get("cpf"):
            ids = [cpf_util.normalizar(v["cpf"])]
        ids = [c for c in ids if c and cpf_util.valido(c)]
        if len(set(ids)) == 1:
            alvos.append((lid, v, ids[0]))
    return alvos, ignorados_sem_arquivo


def main(argv=None) -> int:
    import estado_store  # noqa: PLC0415
    ap = argparse.ArgumentParser(description="Nomeia lotes pelo CPF consultando o Bitrix (contato único).")
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--estado", default=None, help="caminho do estado.json (padrão: estado_store.ARQ_ESTADO)")
    a = ap.parse_args(argv)
    if not (a.simular or a.aplicar):
        ap.error("use --simular ou --aplicar")

    caminho_estado = a.estado or estado_store.ARQ_ESTADO
    estado = estado_store.ler_estado(caminho_estado)
    alvos, _ignorados = escolher_alvos(estado.get("lotes") or {})
    if a.limite:
        alvos = alvos[: a.limite]

    cache = ler_cache()
    print(f"  lotes sem nome com 1 CPF : {len(alvos)}")
    print(f"  CPFs distintos           : {len({c for _, _, c in alvos})}")
    print(f"  cache válido             : {sum(1 for c in cache if do_cache(cache, c))} CPF(s)\n")

    achados, nao_achados, interrompido = [], [], None
    for i, (lid, v, cpf) in enumerate(alvos, 1):
        ent = do_cache(cache, cpf)
        if ent is not None:
            nome, origem = ent.get("nome") or "", "cache"
        else:
            try:
                nome, origem = buscar_cpf(cpf)
            except ConsultaFalhou as e:
                interrompido = str(e)
                break
            cache[cpf] = {"nome": nome, "origem": origem, "em": time.time()}
            if i % 25 == 0:
                gravar_cache(cache)
                print(f"    ... {i}/{len(alvos)} ({len(achados)} nomeados)")
        if nome:
            achados.append((lid, nome, origem, v.get("qtd_arquivos"), v.get("pronto")))
        else:
            nao_achados.append((lid, origem))
    gravar_cache(cache)
    if interrompido:
        print(f"\n  ⚠ consulta ao Bitrix falhou ({interrompido}) — rodada interrompida; "
              f"o que faltou NÃO foi marcado como 'sem nome'.")

    print(f"\n  NOMEADOS: {len(achados)}   |   sem nome no CRM: {len(nao_achados)}")
    for lid, nome, origem, arqs, pronto in sorted(achados, key=lambda x: -(x[3] or 0))[:30]:
        print(f"    {lid} | arqs={arqs or 0:2} pronto={str(pronto):5} | {nome}  [{origem}]")

    if a.aplicar and achados:
        estado_store.fazer_backup(caminho_estado, motivo="nomear_lotes_por_cpf")
        mudancas = {lid: {"nome": nome, "nome_origem": "bitrix-cpf"} for lid, nome, *_ in achados}
        conflitos = estado_store.atualizar_lotes(mudancas, dono=DONO, caminho=caminho_estado)
        print(f"\n  APLICADO: {len(achados) - len(conflitos)} lote(s) nomeados"
              + (f"; {len(conflitos)} com conflito de dono (preservados)" if conflitos else ""))
    elif a.simular:
        print("\n  SIMULACAO: nada gravado no estado.")
    return 1 if interrompido else 0


if __name__ == "__main__":
    sys.exit(main())
