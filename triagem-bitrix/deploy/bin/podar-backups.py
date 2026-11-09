#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Poda de backups da triagem (S05 / OP-O1 / OP-O2) — SEMPRE começa em modo simulação.

Política inicial: manter os 24 backups mais recentes + 1 por dia nos últimos 14 dias
(contrato `estado_store.podar_backups`, docs/remediacao/CONTRATOS.md). ATENÇÃO: a cota é
sobre a UNIÃO dos formatos (novos `backups-estado/` + legados ao lado do estado); com os
dois coexistindo, ela se reparte entre eles (verificado em fixture: 12 novos + 20 legados
mantidos com 24+14). A cota da fila é separada.

Categorias tratadas (a separação é o ponto: nem tudo pode ser apagado igual):

  OPERACIONAL   backups-estado/estado.json.bak-<ts>[-motivo]   (gerados por estado_store.fazer_backup)
                estado.json.bak-<ts> ao lado do estado         (legado, 1.833 cópias de 27 GB)
                backups/fila-jobs-<ts>.json                    (gerados por lote_para_fila.gravar_na_fila)
                -> retenção normal (este script).

  RECUPERACAO   estado.json.corrompido-<ts>, dumps e cópias manuais em backups/
                -> NUNCA apagados por este script; só listados. Servem para recuperação
                   e para investigação.

  EVIDENCIA     os backups LEGADOS (estado.json.bak-*) são cópias integrais do estado e
                CONTÊM senha gov.br/INSS de cliente em texto (achado S01 da auditoria).
                Enquanto o saneamento (S01) não acontece, o descarte deles é tratado como
                possível evidência de incidente: exige autorização explícita e usa descarte
                seguro (zerar + fsync + unlink, `seguranca.remover_seguro`). NUNCA automático.

Uso:
  podar-backups.py                          # SIMULAÇÃO (padrão): mostra o plano, não apaga nada
  podar-backups.py --aplicar                # apaga excedentes OPERACIONAIS (novos + fila)
  podar-backups.py --aplicar --descarte-seguro --autorizado-por "Nome Sobrenome"
                                            # inclui os legados (evidência) no descarte seguro

Só com --aplicar algo é removido. Sem --descarte-seguro --autorizado-por, os legados
nunca são tocados — mesmo com --aplicar.

Toda a saída é caminho/tamanho/contagem: nenhum conteúdo de arquivo é impresso.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import estado_store  # noqa: E402  (produtor da política de retenção — contrato)
from seguranca import remover_seguro  # noqa: E402  (descarte seguro: zera + fsync + unlink)

DIR_BACKUPS_ESTADO = "backups-estado"
PADRAO_FILA = "fila-jobs-"
SUFIXO_FILA = ".json"


def _dir_fila(estado: str) -> str:
    # derivado do diretório do estado: --estado aponta a instância inteira (estado,
    # backups-estado/, backups/, .corrompido-*); caminhos.BASE só entra no padrão.
    return os.path.join(os.path.dirname(os.path.abspath(estado)), "backups")


def _hum(tam: float) -> str:
    for un in ("B", "KB", "MB", "GB"):
        if tam < 1024 or un == "GB":
            return f"{tam:.1f} {un}" if un != "B" else f"{int(tam)} B"
        tam /= 1024
    return f"{tam:.1f} GB"


def _tamanho_total(caminhos_: list) -> float:
    total = 0.0
    for c in caminhos_:
        try:
            total += os.path.getsize(c)
        except OSError:
            pass
    return total


def _listar_fila(dir_fila: str) -> list:
    """[(ts 'YYYYMMDD-HHMMSS', caminho)] dos backups da fila, do mais novo ao mais antigo."""
    try:
        nomes = os.listdir(dir_fila)
    except OSError:
        return []
    achados = []
    for nome in nomes:
        if not (nome.startswith(PADRAO_FILA) and nome.endswith(SUFIXO_FILA)):
            continue
        ts = nome[len(PADRAO_FILA):-len(SUFIXO_FILA)]
        if len(ts) != 15 or ts[8] != "-":
            continue
        cam = os.path.join(dir_fila, nome)
        if os.path.isfile(cam):
            achados.append((ts.replace("-", ""), cam))
    achados.sort(reverse=True)
    return achados


def _podar_fila(dir_fila: str, manter_ultimos: int, diarios: int) -> list:
    """Mesma política do contrato, para fila-jobs-*: N mais novos + 1 por dia nos últimos
    `diarios` dias. Política local (o contrato de estado_store cobre só o estado); o
    sugerido na auditoria OP-O2 é manter_ultimos=50, diarios=14."""
    todos = _listar_fila(dir_fila)
    manter = {cam for _, cam in todos[:max(0, int(manter_ultimos))]}
    dias: list = []
    for ts, cam in todos:
        dia = ts[:8]
        if dia in dias:
            continue
        if len(dias) >= max(0, int(diarios)):
            break
        dias.append(dia)
        manter.add(cam)
    return [cam for _, cam in todos if cam not in manter]


def _listar_recuperacao(estado: str) -> list:
    """.corrompido-* ao lado do estado + outros arquivos de backups/ (dumps manuais)."""
    saida = []
    base = os.path.basename(estado)
    dir_estado = os.path.dirname(os.path.abspath(estado))
    for nome in sorted(os.listdir(dir_estado)):
        if nome.startswith(base + ".corrompido"):
            saida.append(os.path.join(dir_estado, nome))
    dir_fila = _dir_fila(estado)
    try:
        nomes = os.listdir(dir_fila)
    except OSError:
        nomes = []
    for nome in sorted(nomes):
        cam = os.path.join(dir_fila, nome)
        if not os.path.isfile(cam):
            continue
        if nome.startswith(PADRAO_FILA) and nome.endswith(SUFIXO_FILA):
            continue
        saida.append(cam)
    return saida


def _apagar_seguro(caminhos_: list, rotulo: str) -> int:
    n = 0
    for cam in caminhos_:
        try:
            remover_seguro(cam)
            n += 1
            print(f"  [REMOVIDO] ({rotulo}) {cam}")
        except OSError as e:
            print(f"  [ERRO]     ({rotulo}) {cam}: {type(e).__name__}: {e}")
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Poda de backups da triagem (padrão: SIMULA — nada é apagado)")
    p.add_argument("--simular", action="store_true", default=True,
                   help="só mostra o plano (padrão; não há efeito)")
    p.add_argument("--aplicar", action="store_true",
                   help="apaga de verdade os excedentes operacionais")
    p.add_argument("--manter-ultimos", type=int, default=24,
                   help="backups do estado a manter, do mais novo (padrão 24)")
    p.add_argument("--diarios", type=int, default=14,
                   help="dias dos quais manter 1 backup do estado (padrão 14)")
    p.add_argument("--fila-ultimos", type=int, default=50,
                   help="backups de fila-jobs a manter (padrão 50; OP-O2)")
    p.add_argument("--fila-diarios", type=int, default=14,
                   help="dias dos quais manter 1 backup de fila-jobs (padrão 14)")
    p.add_argument("--estado", default=None,
                   help="caminho do estado.json (padrão: estado_store.ARQ_ESTADO)")
    p.add_argument("--sem-fila", action="store_true", help="não trata backups/fila-jobs-*")
    p.add_argument("--tmp-orfaos", action="store_true",
                   help="inclui <estado>.tmp* órfãos (estado_store.limpar_tmp_orfaos)")
    p.add_argument("--descarte-seguro", action="store_true",
                   help="inclui os backups LEGADOS (contêm senha de cliente) no descarte")
    p.add_argument("--autorizado-por", default="",
                   help="quem autorizou o descarte dos legados (obrigatório com "
                        "--descarte-seguro; fica registrado na saída/journal)")
    a = p.parse_args(argv)

    aplicar = bool(a.aplicar)
    if a.descarte_seguro and not a.autorizado_por.strip():
        print("ERRO: --descarte-seguro exige --autorizado-por \"<nome de quem autorizou>\".")
        return 2
    if aplicar and a.descarte_seguro:
        print(f"AUTORIZACAO DE DESCARTE SEGURO: {a.autorizado_por.strip()} "
              f"em {time.strftime('%Y-%m-%d %H:%M:%S')}")

    estado = os.path.abspath(a.estado or estado_store.ARQ_ESTADO)
    modo = "APLICANDO" if aplicar else "SIMULACAO (nada sera apagado)"
    print(f"podar-backups — {modo}")
    print(f"estado: {estado}")
    print(f"politica estado: {a.manter_ultimos} ultimos + 1/dia por {a.diarios} dias")
    if not a.sem_fila:
        print(f"politica fila:   {a.fila_ultimos} ultimos + 1/dia por {a.fila_diarios} dias")

    # ---- 1) política do estado: fonte única = estado_store.podar_backups (contrato)
    excedentes = estado_store.podar_backups(
        caminho=estado, manter_ultimos=a.manter_ultimos,
        diarios=a.diarios, simular=True)

    dir_novos = os.path.join(os.path.dirname(estado), DIR_BACKUPS_ESTADO)
    novos = [c for c in excedentes
             if os.path.dirname(os.path.abspath(c)) == os.path.abspath(dir_novos)]
    legados = [c for c in excedentes if c not in novos]

    print(f"\n[OPERACIONAL] backups do estado novos ({DIR_BACKUPS_ESTADO}/): "
          f"{len(novos)} excedente(s), {_hum(_tamanho_total(novos))}")
    for c in novos:
        print(f"  - {c}")

    print(f"\n[EVIDENCIA] backups LEGADOS do estado (contem senha de cliente, S01): "
          f"{len(legados)} excedente(s), {_hum(_tamanho_total(legados))}")
    for c in legados:
        print(f"  - {c}")
    if legados and not (aplicar and a.descarte_seguro):
        print("  (nao serao tocados: descarte exige --aplicar --descarte-seguro "
              "--autorizado-por; ver RUNBOOK-OPERACIONAL.md)")

    # ---- 2) backups da fila (OP-O2)
    excedentes_fila: list = []
    if not a.sem_fila:
        excedentes_fila = _podar_fila(_dir_fila(estado), a.fila_ultimos, a.fila_diarios)
        print(f"\n[OPERACIONAL] backups da fila (fila-jobs-*): "
              f"{len(excedentes_fila)} excedente(s), {_hum(_tamanho_total(excedentes_fila))}")
        for c in excedentes_fila:
            print(f"  - {c}")

    # ---- 3) cópias de recuperação: só informa
    recup = _listar_recuperacao(estado)
    print(f"\n[RECUPERACAO] .corrompido-* e dumps/cópias manuais em backups/: "
          f"{len(recup)} arquivo(s), {_hum(_tamanho_total(recup))} — NUNCA apagados por aqui")
    for c in recup:
        print(f"  - {c}")

    # ---- 4) tmp órfãos do estado (opcional)
    if a.tmp_orfaos:
        orfaos = estado_store.limpar_tmp_orfaos(estado, simular=not aplicar)
        print(f"\n[OPERACIONAL] <estado>.tmp* orfaos: {len(orfaos)}")
        for c in orfaos:
            print(f"  - {c}")

    if not aplicar:
        total = _tamanho_total(novos) + _tamanho_total(legados) + _tamanho_total(excedentes_fila)
        print(f"\nSIMULACAO: nada foi apagado. Reaproveitaria {_hum(total)}. "
              f"Rode com --aplicar para executar.")
        return 0

    # ---- execução (só aqui algo é removido)
    print("\nexecutando descarte seguro (zerar + fsync + unlink):")
    removidos = _apagar_seguro(novos, "operacional")
    removidos += _apagar_seguro(excedentes_fila, "operacional-fila")
    if a.descarte_seguro:
        removidos += _apagar_seguro(legados, f"evidencia/legado, autorizado por {a.autorizado_por.strip()}")
    print(f"\ntotal removido: {removidos} arquivo(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
