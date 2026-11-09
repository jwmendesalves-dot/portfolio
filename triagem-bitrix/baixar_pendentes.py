#!/usr/bin/env python3
"""
Baixa os arquivos pendentes dos lotes da triagem — isolado do triagem_grupo.py.

Motivo (21/10/2026): o ciclo do triagem_grupo ficava preso no download
(conexão pendurada no portal + rate limit QUERY_LIMIT_EXCEEDED) e os lotes do
dia ficavam com file_ids mas ZERO arquivos no disco — sem arquivo não há
sha256, a dedupe do lote_para_fila não prova que é kit novo e o lote nunca
chega na ponte/fila.

Remediação (frente D — A5, B3, D1, D2, E1, E4, G1, OP-O5): este script virou o
PLANEJADOR da fila persistente do `download_svc`:
  1. lê o estado.json (só leitura) e acha os arquivos que faltam;
  2. NÃO pula para sempre por causa de `.parcial/.part` órfão nem de arquivo de 0 byte
     (são limpos quando velhos e nunca contam como baixado);
  3. áudio/vídeo (tipos_documento) é registrado como `ignorado_midia` e nunca é
     baixado/retentado; id não numérico (CQA… do Telegram legado) vira
     `telegram_legado` permanente e nunca vai ao disk.file.get;
  4. enfileira o resto (dedupe por file_id) e chama `download_svc.processar_fila`,
     que respeita os disjuntores por fonte, o limitador por host, a memória de
     tentativas (proxima_em em minutos/horas) e PARA a rodada em QUERY_LIMIT.

Uso: python3 baixar_pendentes.py [LIMITE] [--lote LOTE] [--forcar] [--teto-s S] [--simular]
Saída: 0 = ok (inclusive transitório/adiado); 1 = falha que exige atenção (permanente
nova, credencial); 2 = estado/fila ilegível ou nenhuma fonte configurada.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import caminhos  # noqa: E402
import download_svc as ds  # noqa: E402
import estado_store  # noqa: E402

log = logging.getLogger("baixar_pendentes")

BASE = caminhos.BASE
ERRO_TIPO_PERMANENTES = frozenset({"permanente", "telegram_legado", "midia"})

# compat com quem importava estes nomes
nome_limpo = ds.nome_limpo


def _metas_por_fid(lote: dict) -> dict:
    return {str(m.get("file_id")): m for m in (lote.get("arquivos") or [])
            if isinstance(m, dict) and m.get("file_id") is not None}


def _midia_ignorada(lote: dict) -> set:
    saida = set()
    for m in lote.get("midias") or lote.get("midias_ignoradas") or []:
        fid = m.get("file_id") if isinstance(m, dict) else m
        if fid is not None:
            saida.add(str(fid))
    return saida


def _situacao_no_disco(pasta: str, indice: int, idade_orfao: float, agora: float) -> str:
    """'valido' (arquivo final > 0 byte com o prefixo NN_), 'em_andamento'
    (temporário recente com o prefixo) ou 'ausente'. `.parcial/.part/.tmp`/0 byte
    NUNCA contam como baixado (E1)."""
    pref = f"{indice:02d}_"
    try:
        nomes = os.listdir(pasta)
    except OSError:
        return "ausente"
    andamento = False
    for n in nomes:
        if not n.startswith(pref):
            continue
        cam = os.path.join(pasta, n)
        if ds.arquivo_valido(cam):
            return "valido"
        try:
            if n.endswith(ds.SUFIXOS_TEMP) and agora - os.path.getmtime(cam) < idade_orfao:
                andamento = True
        except OSError:
            pass
    return "em_andamento" if andamento else "ausente"


def planejar(estado: dict, *, lote: str | None = None, limpar: bool = True,
             idade_orfao: float | None = None, agora: float | None = None) -> dict:
    """Decide, sem rede, o que fazer com cada arquivo faltante.

    Devolve {"pedidos": [kwargs de Fila.enfileirar], "contagem": {...}, "orfaos": [...]}."""
    import time
    agora = time.time() if agora is None else agora
    idade_orfao = ds.IDADE_ORFAO_S if idade_orfao is None else idade_orfao
    cont = {"lotes": 0, "ja_baixados": 0, "ja_em_disco": 0, "em_andamento": 0, "baixar": 0,
            "midia": 0, "telegram_legado": 0, "permanente_anterior": 0}
    pedidos, orfaos = [], []
    for lote_id, v in (estado.get("lotes") or {}).items():
        if not isinstance(v, dict) or (lote and lote_id != lote):
            continue
        # 🛡️ 30/10/2026: NÃO filtrar por `pronto` (lote fechado por janela/timeout também baixa).
        fids = v.get("file_ids") or []
        if not fids:
            continue
        pasta = v.get("pasta") or caminhos.em_base("lotes", lote_id)
        metas = _metas_por_fid(v)
        midias_lote = _midia_ignorada(v)
        limpou = False
        contou_lote = False
        for i, fid in enumerate(fids, start=1):
            fid_s = str(fid).strip()
            meta = metas.get(fid_s) or {}
            indice = meta.get("indice") if isinstance(meta.get("indice"), int) else i
            cam = meta.get("caminho") or ""
            if meta.get("baixado") and meta.get("sha256") and cam and ds.arquivo_valido(cam):
                cont["ja_baixados"] += 1
                continue
            if meta.get("erro_tipo") in ERRO_TIPO_PERMANENTES:
                cont["permanente_anterior"] += 1          # permanente nunca reentra
                continue
            if not contou_lote:
                cont["lotes"] += 1
                contou_lote = True
            if limpar and not limpou:
                orfaos += ds.limpar_orfaos(pasta, idade_orfao, agora)
                limpou = True
            sit = _situacao_no_disco(pasta, indice, idade_orfao, agora)
            if sit == "valido":
                cont["ja_em_disco"] += 1                   # o reconciliador registra no estado
                continue
            if sit == "em_andamento":
                cont["em_andamento"] += 1                  # outro processo baixando agora
                continue
            nome = meta.get("nome_original") or meta.get("nome_salvo") or None
            base = dict(file_id=fid_s, lote_id=lote_id, indice=indice, pasta=pasta, nome=nome)
            if ds.fid_numerico(fid_s) is None:
                cont["telegram_legado"] += 1
                pedidos.append(dict(base, estado=ds.TELEGRAM_LEGADO,
                                    motivo="id não numérico (Telegram legado): disk.file.get não se aplica"))
            elif fid_s in midias_lote or ds.e_midia(nome, None, meta.get("tipo")):
                cont["midia"] += 1
                pedidos.append(dict(base, estado=ds.IGNORADO_MIDIA,
                                    motivo="áudio/vídeo não é documento (tipos_documento): não baixado"))
            else:
                cont["baixar"] += 1
                pedidos.append(base)
    return {"pedidos": pedidos, "contagem": cont, "orfaos": orfaos}


def _parse(argv):
    p = argparse.ArgumentParser(prog="baixar_pendentes", description=__doc__.split("\n")[1])
    p.add_argument("limite", nargs="?", type=int, default=0, help="máx. de arquivos nesta rodada (0 = padrão)")
    p.add_argument("--lote", help="retry dirigido de um lote (só pendentes; permanentes não reentram)")
    p.add_argument("--forcar", action="store_true", help="com --lote: ignora o proxima_em dos pendentes")
    p.add_argument("--teto-s", type=float, default=None, help=f"tempo total da rodada (padrão {ds.TETO_RODADA_S:.0f}s)")
    p.add_argument("--simular", action="store_true", help="só mostra o plano (sem rede, sem gravar)")
    p.add_argument("--estado", default=None, help="estado.json (padrão: estado_store.ARQ_ESTADO)")
    p.add_argument("--fila", default=None, help="download_fila.json (padrão: download_svc.DEFAULT_QUEUE_FILE)")
    return p.parse_args(argv)


def _ha_fonte() -> bool:
    try:
        import bitrix_cliente
        bitrix_cliente.webhook()
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(ds.ler_chave_vibecode(obrigatorio=False))
    except Exception:  # noqa: BLE001
        return False


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    ds.configurar_log()
    caminho_estado = args.estado or estado_store.ARQ_ESTADO
    try:
        estado = estado_store.ler_estado(caminho_estado)
    except estado_store.EstadoCorrompido as e:
        log.error("estado.json corrompido (%s): nada a fazer; cópia em %s", e.caminho, e.copia)
        return 2
    plano = planejar(estado, lote=args.lote, limpar=not args.simular)
    c = plano["contagem"]
    print(f"lotes com arquivo faltando: {c['lotes']} | a baixar: {c['baixar']} | mídia ignorada: {c['midia']} "
          f"| telegram legado: {c['telegram_legado']} | já em disco: {c['ja_em_disco']} "
          f"| em andamento: {c['em_andamento']} | órfãos limpos: {len(plano['orfaos'])}")
    if args.simular:
        for p in plano["pedidos"][:50]:
            print(f"  {p['lote_id']} #{p['indice']:02d} file {p['file_id']} -> {p.get('estado') or 'baixar'}")
        return 0
    try:
        fila = ds.Fila(args.fila)
    except estado_store.EstadoCorrompido as e:
        log.error("download_fila.json corrompido (%s); cópia em %s", e.caminho, e.copia)
        return 2
    atencao = []
    resultado = fila.enfileirar_varios(plano["pedidos"])
    patches, por_fid = [], {}
    for (item, novo), pedido in zip(resultado, plano["pedidos"]):
        por_fid[item["file_id"]] = item
        if item["estado"] in (ds.TELEGRAM_LEGADO, ds.IGNORADO_MIDIA):
            # registra o desfecho no lote (dono 'download'); idempotente
            patches.append({"file_id": item["file_id"], "indice": pedido["indice"], "erro": item.get("erro"),
                            "erro_tipo": "telegram_legado" if item["estado"] == ds.TELEGRAM_LEGADO else "midia"})
            if novo and item["estado"] == ds.TELEGRAM_LEGADO:
                atencao.append(f"{item['file_id']}: telegram legado (precisa de humano)")
    if patches:
        ds._aplicar_no_estado(patches, por_fid, caminho_estado)
    if args.lote and args.forcar:
        for p in plano["pedidos"]:
            if not p.get("estado"):
                fila.atualizar_item(p["file_id"], {"proxima_em": 0})
    if not fila.pendentes(__import__("time").time()):
        print("nada devido nesta rodada")
        return 1 if atencao else 0
    if not _ha_fonte():
        log.error("nenhuma fonte configurada (webhook_bitrix / vibecode_key)")
        return 2
    so = {p["file_id"] for p in plano["pedidos"]} if args.lote else None
    processados = ds.processar_fila(fila, limite_itens=args.limite or None, teto_rodada_s=args.teto_s,
                                    so_file_ids=so, caminho_estado=caminho_estado)
    r = fila.ultima_rodada
    atencao += r.get("atencao") or []
    est = {}
    for it in processados:
        est[it["estado"]] = est.get(it["estado"], 0) + 1
    for msg in atencao[:20]:
        print(f"  ATENÇÃO {msg}")
    print(f"resumo: {est.get(ds.BAIXADO, 0)} baixados | {est.get(ds.PENDENTE, 0)} adiados | "
          f"{est.get(ds.FALHA_PERMANENTE, 0)} permanentes | {len(processados)} de {r['devidos']} devidos"
          + (f" | parou: {r['parar']}" if r.get("parar") else ""))
    return 1 if atencao else 0


if __name__ == "__main__":
    raise SystemExit(main())
