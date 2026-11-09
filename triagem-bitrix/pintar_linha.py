#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pintar_linha.py — quando chegam documentos de um cliente no GRUPO, pinta de ROXO
a linha inteira dele na planilha "*NOVA* 2 PASTAS PREVI GERAL" (aba PASTAS PREVI GERAL).

Regra (pedida pelo dono em 06/11/2026):
  • acha a linha pelo **CPF** (mesma âncora da triagem);
  • achou → pinta a linha inteira (A..Q) de roxo;
  • cliente NOVO (sem linha) → usa a próxima linha livre, escreve NOME e CPF e pinta de
    roxo — SÓ com identidade CONCLUSA: CPF com DV válido (cpf_util.valido) e nome;
    identidade inconclusiva (CPF achado por nome, vários CPFs no lote, sem nome) NÃO
    cria linha (PL-03);
  • nunca sobrescreve célula preenchida: a linha alvo é RELIDA na hora da escrita e só
    é gravada se continuar livre; depois de gravar, confere (PL-01);
  • idempotente: o que já foi pintado fica registrado em `pintadas.json`;
  • não repinta histórico por acidente: por padrão só lotes a partir do corte (`--desde`);
  • sem lote novo, NÃO se lê a planilha (PL-02).

⚠️ A linha criada aqui é REUSADA pelo painel (app2.planilha casa por CPF e devolve
`novo:false`), então não gera linha duplicada.

Uso:
  /opt/qc-planilha/venv/bin/python /opt/triagem-bitrix/pintar_linha.py            # simulação
  ... pintar_linha.py --aplicar                                                   # grava
  ... pintar_linha.py --aplicar --lote LOTE-SIG-20261106-1010-abcdef12
  ... pintar_linha.py --aplicar --todos          # backfill (todo o histórico de prontos)
  ... pintar_linha.py --aplicar --desde 2026-11-06
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import caminhos
import cpf_util
import estado_store

# ---------------------------------------------------------------- ajustes
COR_ROXO = (180, 166, 217)          # #B4A6D9 — amostrada da imagem mandada pelo dono
SPREADSHEET_ID = "ID_DA_PLANILHA_EXEMPLO"
NOME_ABA = "PASTAS PREVI GERAL"
CONTA = caminhos.em_separador("service-account.json")
ARQ_ESTADO = caminhos.em_base("estado.json")
ARQ_PINTADAS = caminhos.em_base("pintadas.json")

COL_NOME = 2      # B
COL_CIDADE = 3    # C
COL_CPF = 7       # G
COLUNAS_QUE_CONTAM = (COL_NOME, COL_CIDADE, COL_CPF)   # A é pré-numerada pela equipe
PRIMEIRA_LINHA = 2
COLUNAS_DA_LINHA = 17    # A..Q (Q = "JÁ NA PLANILHA")
# 🔴 N, O e P são protegidas na planilha ("Coluna (N)", "Coluna (O)", "Coluna (P)"):
# pintar A..Q inteiro devolve 400 "You are trying to edit a protected cell". Pinta-se os vãos.
COLUNAS_PROTEGIDAS = ((13, 14), (14, 15), (15, 16))   # N, O, P (0-based)
TZ_BR = dt.timezone(dt.timedelta(hours=-3))


# ------------------------------------------------------------------ CPF
def cpf_norm(bruto) -> str:
    """Só dígitos; CPF de 10 dígitos ganha o zero à esquerda. Inválido -> ''."""
    d = "".join(c for c in str(bruto or "") if c.isdigit())
    if len(d) == 10:
        d = "0" + d
    return d if len(d) == 11 else ""


def cpf_formatado(bruto) -> str:
    d = cpf_norm(bruto)
    if not d:
        return ""
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def cor_hex() -> str:
    return "#%02X%02X%02X" % COR_ROXO


# ------------------------------------------------------- regras (puras)
def indice_cpf(valores: list[list]) -> dict[str, list[int]]:
    """{cpf: [linhas 1-based]} a partir do bloco lido da aba (linha 1 = cabeçalho)."""
    idx: dict[str, list[int]] = {}
    for i, linhav in enumerate(valores):
        if i == 0:
            continue
        cpf = cpf_norm(linhav[COL_CPF - 1] if len(linhav) >= COL_CPF else "")
        if not cpf:
            continue
        idx.setdefault(cpf, []).append(i + 1)
    return idx


def _ocupada(linhav: list) -> bool:
    """Linha com algum conteúdo em NOME/CIDADE/CPF (a numeração da coluna A não conta)."""
    for col in COLUNAS_QUE_CONTAM:
        if str((linhav[col - 1] if len(linhav) >= col else "") or "").strip():
            return True
    return False


def linha_livre(valores: list[list]) -> int | None:
    """Primeira linha livre DEPOIS da última linha usada.

    🔴 Nunca reaproveita buraco no meio do histórico: medido em 06/11/2026 a planilha tem
    **55 linhas vazias no meio** (1222, 1679, 3492, 7588...). Cliente novo entra no fim.
    Devolve None se não houver linha livre (a planilha é pré-numerada até o fim da grade,
    então na prática sempre há).
    """
    ultima_usada = 1                              # linha 1 é cabeçalho: NUNCA é linha livre
    for i, linhav in enumerate(valores):
        if i == 0:
            continue
        if _ocupada(linhav):
            ultima_usada = i + 1
    for i in range(ultima_usada, len(valores)):        # a partir da 1ª linha depois da última usada
        if not _ocupada(valores[i]):
            return i + 1
    return ultima_usada + 1                            # o bloco lido acabou: a próxima é a livre


def dedupe_por_cpf(lotes: list[dict]) -> list[dict]:
    """Um lote por CPF (o primeiro na ordem recebida). O mesmo cliente cai em 2-3 lotes."""
    vistos: set[str] = set()
    unicos = []
    for lote in lotes:
        cpf = cpf_norm(lote.get("cpf"))
        if not cpf or cpf in vistos:
            continue
        vistos.add(cpf)
        unicos.append(lote)
    return unicos


def decidir(valores: list[list], cpf: str, nome: str, pintadas: dict, *,
            cpf_origem: str | None = None, cpfs_distintos: list | None = None) -> dict:
    """O que fazer com este cliente. Devolve {acao, linha, escritas, motivo}.

    PL-03: linha NOVA só com identidade CONCLUSA — CPF com DV válido (cpf_util.valido)
    e sem indício de identidade inconclusa (CPF achado por homônimo / vários CPFs).
    """
    cpf = cpf_norm(cpf)
    if not cpf:
        return {"acao": "nada", "linha": None, "escritas": {}, "motivo": "cpf ausente/inválido"}

    if cpf in (pintadas or {}):
        return {"acao": "nada", "linha": pintadas[cpf].get("linha"), "escritas": {},
                "motivo": "já pintada em " + str(pintadas[cpf].get("em"))}

    linha = (indice_cpf(valores).get(cpf) or [None])[0]
    if linha:
        return {"acao": "pintar", "linha": linha, "escritas": {}, "motivo": "cliente já tem linha"}

    # cliente novo → criar linha exige identidade conclusa (PL-03)
    if not cpf_util.valido(cpf):
        return {"acao": "nada", "linha": None, "escritas": {},
                "motivo": "CPF com DV inválido — não crio linha"}
    if str(cpf_origem or "") == "bitrix-nome":
        return {"acao": "nada", "linha": None, "escritas": {},
                "motivo": "identidade inconclusiva (CPF achado por nome) — não crio linha"}
    distintos = {cpf_norm(c) for c in (cpfs_distintos or []) if cpf_norm(c)}
    if len(distintos) > 1:
        return {"acao": "nada", "linha": None, "escritas": {},
                "motivo": f"identidade inconclusiva ({len(distintos)} CPFs no lote) — não crio linha"}

    livre = linha_livre(valores)
    if not livre:
        return {"acao": "nada", "linha": None, "escritas": {},
                "motivo": "cliente novo e não achei linha livre"}

    escritas = {COL_CPF: cpf_formatado(cpf)}
    if str(nome or "").strip():
        escritas[COL_NOME] = " ".join(str(nome).split()).upper()
    return {"acao": "criar", "linha": livre, "escritas": escritas,
            "motivo": "cliente novo — linha criada e pintada"}


def planejar(valores: list[list], lotes: list[dict], pintadas: dict) -> tuple[list[dict], list[dict]]:
    """Decide linha por linha RESERVANDO as linhas novas (dois clientes novos não caem na mesma)."""
    copia = [list(x) + [""] * max(0, COLUNAS_DA_LINHA - len(x)) for x in valores]
    planos, ignorados = [], []
    for lote in lotes:
        plano = decidir(copia, lote.get("cpf"), lote.get("nome"), pintadas,
                        cpf_origem=lote.get("cpf_origem"),
                        cpfs_distintos=lote.get("cpfs_distintos"))
        plano["lote"] = lote.get("lote_id")
        plano["cpf"] = cpf_norm(lote.get("cpf"))
        plano["nome"] = lote.get("nome") or ""
        if plano["acao"] == "nada":
            ignorados.append(plano)
            continue
        planos.append(plano)
        # reserva a linha para o próximo cliente da mesma rodada
        while len(copia) < plano["linha"]:
            copia.append([""] * COLUNAS_DA_LINHA)
        for col, valor in (plano.get("escritas") or {}).items():
            copia[plano["linha"] - 1][col - 1] = valor
    return planos, ignorados


def faixas_livres(colunas: int = COLUNAS_DA_LINHA) -> list[tuple[int, int]]:
    """Trechos da linha que podem ser pintados (a linha inteira menos as colunas protegidas)."""
    livres, inicio = [], 0
    for (p_ini, p_fim) in sorted(COLUNAS_PROTEGIDAS):
        if p_ini > inicio:
            livres.append((inicio, min(p_ini, colunas)))
        inicio = max(inicio, p_fim)
    if inicio < colunas:
        livres.append((inicio, colunas))
    return [f for f in livres if f[0] < f[1]]


def requests_pintar(gid: int, linha: int, colunas: int = COLUNAS_DA_LINHA) -> list[dict]:
    """Pinta a linha inteira de roxo, pulando as colunas protegidas (N, O, P)."""
    cor = {"red": COR_ROXO[0] / 255, "green": COR_ROXO[1] / 255, "blue": COR_ROXO[2] / 255}
    return [{
        "repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": linha - 1, "endRowIndex": linha,
                      "startColumnIndex": ini, "endColumnIndex": fim},
            "cell": {"userEnteredFormat": {"backgroundColor": cor}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    } for (ini, fim) in faixas_livres(colunas)]


# -------------------------------------------------------------- estado
def ler_json(caminho: str, padrao):
    try:
        with open(caminho, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return padrao


def salvar_json_atomico(caminho: str, dados) -> None:
    """Escrita atômica via contrato compartilhado (estado_store, onda 1)."""
    estado_store.gravar_atomico(dados, caminho)


def ler_lotes_prontos(estado: dict, desde: dt.datetime | None) -> list[dict]:
    """Lotes prontos (com CPF), a partir do corte `desde`, do mais antigo para o mais novo."""
    saida = []
    for lote in (estado.get("lotes") or {}).values():
        if not lote.get("pronto"):
            continue
        if not cpf_norm(lote.get("cpf")):
            continue
        quando = lote.get("fim") or lote.get("inicio") or ""
        if desde is not None:
            try:
                if dt.datetime.fromisoformat(str(quando)) < desde:
                    continue
            except Exception:
                continue
        saida.append(lote)
    saida.sort(key=lambda l: str(l.get("fim") or l.get("inicio") or ""))
    return saida


# --------------------------------------------------------------- planilha
def abrir():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    cred = service_account.Credentials.from_service_account_file(
        CONTA, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=cred)


def meta_aba(svc) -> tuple[int, int, int]:
    """(gid, última linha com conteúdo, colunas da grade).

    PL-02: mede a última linha em B..G (a coluna A é pré-numerada até o fim da grade e
    fazia `ultima` ≈ total da grade, disparando leitura de ~10 mil linhas por rodada).
    """
    meta = svc.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        fields="sheets(properties.sheetId,properties.title,"
               "properties.gridProperties(columnCount))").execute()
    for sh in meta["sheets"]:
        p = sh["properties"]
        if p["title"] == NOME_ABA:
            gid = p["sheetId"]
            colunas = p["gridProperties"].get("columnCount", COLUNAS_DA_LINHA)
            valores = svc.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{NOME_ABA}'!B:G").execute().get("values", [])
            return gid, len(valores), colunas
    raise SystemExit(f"aba '{NOME_ABA}' não existe na planilha")


def ler_bloco(svc, ate_linha: int, colunas: int = COLUNAS_DA_LINHA) -> list[list]:
    faixa = f"'{NOME_ABA}'!A1:{chr(64 + colunas) if colunas <= 26 else 'Q'}{ate_linha}"
    return svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=faixa).execute().get("values", [])


def reler_linha_alvo(svc, linha: int) -> list:
    """Relê B..G da linha alvo NA HORA da escrita (PL-01). Devolve a linha (1 lista)."""
    faixa = f"'{NOME_ABA}'!B{linha}:G{linha}"
    vals = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=faixa).execute().get("values", [])
    return list(vals[0]) if vals else []


def linha_ocupada_agora(svc, linha: int) -> bool:
    return any(str(c or "").strip() for c in reler_linha_alvo(svc, linha))


def conferir_escrita(svc, plano: dict) -> bool:
    """Depois de gravar, relê e confere CPF (e nome, se escrito) — PL-01."""
    linhav = reler_linha_alvo(svc, plano["linha"])
    # B..G: nome=B(0), cidade=C(1), D(2), E(3), F(4), cpf=G(5)
    cpf_lido = cpf_norm(linhav[5] if len(linhav) >= 6 else "")
    if cpf_lido != plano["cpf"]:
        return False
    nome_escrito = (plano.get("escritas") or {}).get(COL_NOME)
    if nome_escrito:
        nome_lido = str(linhav[0] if linhav else "").strip()
        if nome_lido != nome_escrito:
            return False
    return True


def gravar(svc, planos: list[dict], gid: int) -> list[dict]:
    """Grava valores (cliente novo) e pinta as linhas. Valores primeiro, cor depois.

    PL-01: para cada linha nova, RELÊ o alvo na hora e só grava se continuar livre
    (nunca sobrescreve nome/CPF de ninguém); depois de gravar, confere. Devolve os
    planos CONFIRMADOS (só eles entram em pintadas.json).
    """
    prontos, escritas = [], []
    for p in planos:
        if p.get("escritas"):
            if linha_ocupada_agora(svc, p["linha"]):
                # alguém (painel/equipe) ocupou a linha entre o planejamento e a escrita
                p["motivo"] = ("linha ocupada na hora da escrita — NÃO sobrescrevi; "
                               "replanejo na próxima rodada")
                print(f"   !! linha {p['linha']} ocupada agora — não gravei "
                      f"(cpf {cpf_formatado(p['cpf'])})")
                continue
            for col, valor in (p.get("escritas") or {}).items():
                letra = chr(64 + col)
                escritas.append({"range": f"'{NOME_ABA}'!{letra}{p['linha']}", "values": [[valor]]})
        prontos.append(p)
    if escritas:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "RAW", "data": escritas}).execute()

    confirmados = []
    for p in prontos:
        if p.get("escritas") and not conferir_escrita(svc, p):
            p["motivo"] = "gravado mas NÃO confere na releitura — verificar manualmente"
            print(f"   !! linha {p['linha']} gravada mas não confere "
                  f"(cpf {cpf_formatado(p['cpf'])})")
            continue
        confirmados.append(p)

    reqs = []
    for p in confirmados:
        reqs.extend(requests_pintar(gid, p["linha"]))
    if reqs:
        svc.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
                                       body={"requests": reqs}).execute()
    return confirmados


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="pinta de roxo a linha do cliente que mandou documentos no grupo")
    p.add_argument("--aplicar", action="store_true", help="grava na planilha (padrão: só simula)")
    p.add_argument("--lote", help="processa só este lote")
    p.add_argument("--desde", help="corte AAAA-MM-DD (padrão: hoje)")
    p.add_argument("--todos", action="store_true", help="sem corte: todo o histórico de prontos")
    p.add_argument("--limite", type=int, default=0, help="máximo de lotes por rodada")
    a = p.parse_args(argv)

    estado = ler_json(ARQ_ESTADO, {})
    pintadas = ler_json(ARQ_PINTADAS, {})

    if a.todos:
        desde = None
    elif a.desde:
        desde = dt.datetime.strptime(a.desde, "%Y-%m-%d").replace(tzinfo=TZ_BR)
    else:
        desde = dt.datetime.now(TZ_BR).replace(hour=0, minute=0, second=0, microsecond=0)

    lotes = dedupe_por_cpf(ler_lotes_prontos(estado, desde))
    if a.lote:
        lotes = [l for l in lotes if l.get("lote_id") == a.lote]
    if a.limite:
        lotes = lotes[:a.limite]

    # PL-02: tira o que JÁ foi pintado ANTES de tocar no Google: sem lote novo não se
    # relê a planilha inteira a cada 2 min.
    lotes = [l for l in lotes if cpf_norm(l.get("cpf")) not in pintadas]

    print(f"corte: {desde or 'sem corte (backfill)'} · lotes prontos a considerar: {len(lotes)}")
    if not lotes:
        print("nada a fazer.")
        return 0

    svc = abrir()
    gid, ultima, colunas = meta_aba(svc)
    valores = ler_bloco(svc, ultima + len(lotes) + 5, colunas)

    planos, ignorados = planejar(valores, lotes, pintadas)

    print(f"\n{'AÇÃO':7} {'LINHA':>6}  {'CPF':15} NOME / motivo")
    for pl in planos:
        print(f"{pl['acao']:7} {str(pl['linha']):>6}  {cpf_formatado(pl['cpf']):15} "
              f"{pl['nome'][:34]} — {pl['motivo']}")
    for pl in ignorados[:10]:
        print(f"{'—':7} {str(pl['linha']):>6}  {cpf_formatado(pl['cpf']):15} "
              f"{pl['nome'][:34]} — {pl['motivo']}")
    if len(ignorados) > 10:
        print(f"  (+{len(ignorados) - 10} ignorados)")

    pintar = sum(1 for pl in planos if pl["acao"] == "pintar")
    criar = sum(1 for pl in planos if pl["acao"] == "criar")
    print(f"\nresumo: {pintar} linha(s) existente(s) para pintar · {criar} cliente(s) novo(s) "
          f"a criar+pintar · {len(ignorados)} ignorado(s)")

    if not a.aplicar:
        print("\nSIMULAÇÃO (nada foi gravado). Use --aplicar para valer.")
        return 0

    if planos:
        confirmados = gravar(svc, planos, gid)   # PL-01: só o confirmado entra em pintadas
        agora = dt.datetime.now(TZ_BR).isoformat(timespec="seconds")
        for pl in confirmados:
            pintadas[pl["cpf"]] = {"linha": pl["linha"], "em": agora, "lote": pl["lote"],
                                   "acao": pl["acao"]}
        if confirmados:
            salvar_json_atomico(ARQ_PINTADAS, pintadas)
        print(f"\nGRAVADO: {len(confirmados)}/{len(planos)} linha(s) roxa(s). "
              f"Registro em {ARQ_PINTADAS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
