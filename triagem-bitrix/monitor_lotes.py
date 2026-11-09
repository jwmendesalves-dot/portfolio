#!/usr/bin/env python3
"""
monitor_lotes.py — vigia dos lotes de documentos que ficaram FORA do painel.

Motivo (medido em 24/09/2026): a ponte encaminha por padrão só lote que abre E
fecha com traço (`--so-traco`); quem fecha por janela/timeout fica fora em silêncio — 41
lotes com documento, de até 40 arquivos, parados desde 18/09.

FASE 1 (14/10/2026): antes o filtro era só `pronto=True` — o lote INTERROMPIDO com kit
completo (FECHADO_POR_TROCA_DE_PESSOA), o lote fechado sem envio e a duplicata descartada
pela ponte eram INVISÍVEIS em todos os avisos (o único vestígio era um log.warning).
Agora a vigia cobre todo material que não chegou ao painel, agrupado por tipo:

  · revisao_duplicata    — mesmo kit, mas com dúvida (nome ruído no envio anterior /
                           sem sha256): pede DECISÃO HUMANA (a ponte não reenvia);
  · descartado_duplicata — reenvio do MESMO kit, a ponte descartou (use --lote <ID>);
  · preso_completo       — kit COMPLETO (identificação OK + arquivos) parado e NÃO
                           pronto: interrompido pela barreira ou fechado na varredura;
  · pronto               — limpo, a ponte encaminha no próximo ciclo;
  · repetido             — do MESMO cliente já enviado (kit igual);
  · revisar              — pronto porém não aceitável pela regra do escritório;
  · incompleto           — sem CPF / sem arquivo / conflito (requer_revisao).

Alerta extra: lote parado com `motivos_revisao` VAZIO = o motivo nem foi anotado
(sinal de furo no registro) — é o pior caso e sai sempre destacado.

F2-07 (onda 2): o aviso só sai quando o lote MUDA de categoria (último aviso por lote
guardado em monitor-avisos.json). Categorias de aviso, distintas:
  revisao · aguardando_download · pronto · pronto_fora_janela (> PONTE_DIAS, precisa
  catch-up) · enfileirado (lote avisado antes que chegou à fila) · falha (erro permanente
  ou envio da ponte não confirmado) · download_falha_permanente / download_telegram_legado
  (arquivo dado como perdido em download_fila.json; memória `download:<file_id>`).
  `descartado_duplicata` (a ponte já avisou) e lote sem
  arquivo ficam só no JSON. A categoria vem de `ponte_criterio.avaliar` — a MESMA regra
  da ponte. Sem nome de cliente e com CPF mascarado na saída (S04).

Roda como cron do Hermes: saída VAZIA = nada a avisar (silêncio). Tem mudança → imprime
o resumo agrupado (vira mensagem no Telegram). O detalhe completo fica em monitor-lotes.json
($MONITOR_SAIDA) e a memória de avisos em monitor-avisos.json ($MONITOR_AVISOS).

Uso:  monitor_lotes.py [--minutos 30] [--detalhe]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

_AQUI = os.path.dirname(os.path.abspath(__file__))
if _AQUI not in sys.path:
    sys.path.insert(0, _AQUI)
import caminhos  # noqa: E402
import cpf_util  # noqa: E402
import estado_store  # noqa: E402
from ponte_criterio import avaliar, lote_aceitavel, motivo_recusa, situacao_kit  # noqa: E402

BASE = caminhos.BASE
ESTADO = estado_store.ARQ_ESTADO
ENVIADOS = caminhos.em_base("fila-enviados.json")
FILA = caminhos.em_separador("data", "fila-jobs.json")
# OP-T4: saída e memória sobrescrevíveis por ambiente (o teste aponta para tmp)
SAIDA = os.environ.get("MONITOR_SAIDA") or caminhos.em_base("monitor-lotes.json")
AVISOS = os.environ.get("MONITOR_AVISOS") or caminhos.em_base("monitor-avisos.json")
# fila do download_svc (mesma variável de ambiente que ele usa)
FILA_DOWNLOAD = os.environ.get("DOWNLOAD_FILA_JSON") or caminhos.em_base("download_fila.json")

# categorias de AVISO (F2-07) — ordem de exibição
CATEGORIAS_AVISO = ("falha", "download_falha_permanente", "download_telegram_legado", "revisao",
                    "aguardando_download", "pronto_fora_janela", "pronto", "enfileirado")
# estado do item em download_fila.json -> categoria de aviso (Z: falhas permanentes de download)
CATEGORIA_DOWNLOAD = {"falha_permanente": "download_falha_permanente",
                      "telegram_legado": "download_telegram_legado"}
ROTULO_CATEGORIA = {
    "download_falha_permanente": "arquivo(s) com falha PERMANENTE de download (não será retentado)",
    "download_telegram_legado": "arquivo(s) de origem Telegram legada (não baixável pelo Bitrix)",
    "falha": "FALHA (erro permanente ou envio da ponte não confirmado na fila)",
    "revisao": "precisa(m) de revisão humana",
    "aguardando_download": "aguardando download de documento (não vai parcial)",
    "pronto_fora_janela": "pronto(s) FORA da janela da ponte — precisa catch-up (--dias 0)",
    "pronto": "pronto(s) parado(s) — a ponte deveria encaminhar",
    "enfileirado": "resolvido(s): chegou(aram) à fila do painel",
}

# rótulo curto por tipo de parada (ordem de exibição = urgência)
TIPOS_ORDEM = ("revisao_duplicata", "descartado_duplicata", "preso_completo",
               "pronto", "repetido", "revisar", "incompleto")
ROTULO_TIPO = {
    "revisao_duplicata": "duplicata com dúvida — decisão humana necessária",
    "descartado_duplicata": "duplicata descartada pela ponte (mesmo kit já enviado)",
    "preso_completo": "kit COMPLETO parado e não pronto (interrompido/timeout)",
    "pronto": "limpo(s) — a ponte encaminha no próximo ciclo",
    "repetido": "do MESMO cliente já enviado (a ponte não manda de novo)",
    "revisar": "precisa(m) de gente (regra do escritório recusou)",
    "incompleto": "lote incompleto (sem CPF / sem arquivo / conflito)",
}
STATUS_DESCARTE = ("descartado_duplicata", "revisao_duplicata")


def _ts(valor):
    try:
        t = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _so_digitos(v) -> str:
    return "".join(c for c in str(v or "") if c.isdigit())


def _kit_repetido(lote: dict, ja_mandados: dict) -> bool:
    """True só quando o cliente já mandou esse MESMO kit (sha256) antes — e o kit está
    100% baixado (F2-03: com documento pendente a duplicata é indeterminada)."""
    for cpf in (lote.get("cpfs_distintos") or [lote.get("cpf")]):
        cp = _so_digitos(cpf)
        if cp and cp in ja_mandados and situacao_kit(lote, ja_mandados[cp]) == "repetido":
            return True
    return False


def _dias_janela() -> int:
    try:
        return int(os.environ.get("PONTE_DIAS", 7))
    except ValueError:
        return 7


def categoria_aviso(lote: dict, agora_ts: float, dias_janela: int | None = None) -> str:
    """Categoria do lote para o aviso (F2-07), pela MESMA regra da ponte.
    Silenciosas: 'descartado', 'aberto', 'sem_arquivo'."""
    if str(lote.get("status") or "") == "descartado_duplicata":
        return "descartado"
    if not (lote.get("arquivos") or []):
        return "sem_arquivo"
    av = avaliar(lote)
    if av["ok"]:
        dias = _dias_janela() if dias_janela is None else dias_janela
        ini = _ts(lote.get("inicio"))
        if dias > 0 and ini is not None and ini.timestamp() < agora_ts - dias * 86400:
            return "pronto_fora_janela"
        return "pronto"
    cat = av["categoria"]
    if cat == "AGUARDANDO_DOWNLOAD":
        return "aguardando_download"
    if cat == "ERRO_PERMANENTE":
        return "falha"
    if cat == "ABERTO":
        return "aberto"
    if cat == "DESCARTADO_DUPLICATA":
        return "descartado"
    return "revisao"


def lotes_travados(estado, enviados, agora_ts: float, minimo_min: float = 30,
                   hashes_por_cpf=None) -> list[dict]:
    """TODO material que não chegou ao painel e parou há mais de `minimo_min` min.

    Entra quem tem `pronto=True` (comportamento antigo), mas TAMBÉM:
      · `requer_revisao=True` (em especial FECHADO_POR_TROCA_DE_PESSOA — kit completo
        interrompido no meio do bloco, que ficava pronto=False para sempre);
      · lote fechado, com kit completo, sem envio (o que a varredura de timeout fecha);
      · `descartado_duplicata` / `revisao_duplicata` (o que a ponte engolia em silêncio).

    `hashes_por_cpf` = {cpf_digitos: {sha256[:12], ...}} do que a ponte já mandou para
    aquele cliente. Marca `cpf_repetido=True` só quando o kit é o MESMO (reenvio); kit com
    arquivo novo entra. Devolve do mais antigo para o mais novo, cada lote UMA vez,
    com `tipo` para o resumo agrupado e `sem_motivo_anotado` quando o porquê nem foi
    registrado (motivos_revisao vazio em lote que não está pronto).
    """
    lotes = ((estado or {}).get("lotes") or {}) if isinstance(estado, dict) else {}
    enviados = set(enviados or ())
    ja_mandados = {_so_digitos(c): {str(h)[:12] for h in (hashes or []) if h}
                   for c, hashes in (hashes_por_cpf or {}).items()}
    saida = []
    for lid, v in lotes.items():
        if not isinstance(v, dict) or lid in enviados:
            continue
        arquivos = v.get("arquivos") or []
        if not arquivos:
            continue                     # F2-07: lote sem arquivo não é aviso (fica no JSON do estado)
        status = str(v.get("status") or "")
        pronto = bool(v.get("pronto"))
        descartado = status in STATUS_DESCARTE
        requer = bool(v.get("requer_revisao"))
        tem_kit = str(v.get("identificacao") or "").upper().startswith("OK") and bool(arquivos)
        if not (pronto or descartado or requer or tem_kit):
            continue                     # nada olhável (sem material e sem marca)
        if not arquivos and not (descartado or requer):
            continue                     # sem documento e sem motivo para olhar
        quando = _ts(v.get("fim") or v.get("inicio") or v.get("descartado_em"))
        if quando is None:
            continue
        minutos = (agora_ts - quando.timestamp()) / 60
        if minutos < minimo_min:
            continue
        motivos = list(v.get("motivos_revisao") or [])
        aceitavel = lote_aceitavel(v)
        cpf_repetido = _kit_repetido(v, ja_mandados)
        if descartado:
            tipo = "revisao_duplicata" if status == "revisao_duplicata" else "descartado_duplicata"
        elif pronto and aceitavel and not cpf_repetido:
            tipo = "pronto"
        elif pronto and aceitavel:
            tipo = "repetido"
        elif pronto:
            tipo = "revisar"
        elif tem_kit:
            tipo = "preso_completo"      # kit pronto de gente — o furo da auditoria
        else:
            tipo = "incompleto"
        cpf_txt = v.get("cpf_formatado") or v.get("cpf")
        saida.append({"lote": lid, "nome": "(oculto)" if v.get("nome") else "(sem nome)",
                      "cpf": cpf_util.mascarar(cpf_txt) if cpf_txt else "-",
                      "categoria": categoria_aviso(v, agora_ts),
                      "arquivos": len(arquivos), "mensagens": len(v.get("mensagens") or []),
                      "minutos": int(minutos), "fechado_em": quando.isoformat(),
                      "status": v.get("status"), "modo": v.get("modo"),
                      "fechado_por": v.get("fechado_por"),
                      "aceitavel": aceitavel, "motivo": motivo_recusa(v),
                      "cpf_repetido": cpf_repetido,
                      "tipo": tipo, "motivos_revisao": motivos,
                      "lote_origem": v.get("lote_origem"),
                      "sem_motivo_anotado": (not pronto or not aceitavel) and not motivos
                      and not descartado})
    return sorted(saida, key=lambda x: -x["minutos"])


def _ler_controle() -> dict:
    try:
        dados = json.load(open(ENVIADOS, encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def enviados_da_ponte() -> tuple[set, dict]:
    """(lotes já encaminhados, {cpf: {sha256[:12] dos arquivos já mandados}}).
    Registro `enfileirando` (ainda não confirmado na fila) NÃO conta como enviado."""
    enviados, hashes = set(), {}
    for lid, v in _ler_controle().items():
        if not isinstance(v, dict) or v.get("estado") == "enfileirando":
            continue
        enviados.add(lid)
        cpf = _so_digitos((v.get("cliente") or {}).get("cpf"))
        if not cpf:
            continue
        if v.get("sha256s"):
            hashes.setdefault(cpf, set()).update(str(h)[:12] for h in v["sha256s"] if h)
        else:
            hashes.setdefault(cpf, set()).update(
                str(nome).split(".")[0][:12] for nome in (v.get("arquivos") or []))
    try:
        for j in json.load(open(FILA, encoding="utf-8")):
            ident = str((j or {}).get("id") or "").replace("grupo-", "")
            if ident.startswith("LOTE-"):
                enviados.add(ident)
    except Exception:
        pass
    return enviados, hashes


def itens_do_controle(controle: dict, agora_ts: float, minimo_min: float = 30) -> list[dict]:
    """Avisos que vêm do controle da ponte: documento chegado DEPOIS do envio (revisão,
    F2-08) e envio que ficou `enfileirando` além do tempo (falha, F2-06)."""
    saida = []
    for lid, c in (controle or {}).items():
        if not isinstance(c, dict):
            continue
        if c.get("estado") == "enfileirando":
            quando = _ts(c.get("em"))
            if quando is None or (agora_ts - quando.timestamp()) / 60 >= minimo_min:
                saida.append({"lote": lid, "categoria": "falha",
                              "motivo": "envio da ponte não confirmado na fila (enfileirando)"})
        elif c.get("docs_posteriores"):
            saida.append({"lote": lid, "categoria": "revisao",
                          "motivo": f"{len(c['docs_posteriores'])} documento(s) chegaram depois do "
                                    "envio ao painel — conferir/complementar à mão"})
    return saida


def itens_da_fila_download(caminho: str | None = None) -> list[dict]:
    """Arquivos que o download_svc deu como perdidos (`falha_permanente` /
    `telegram_legado` em download_fila.json). Chave de memória `download:<file_id>` —
    avisa uma vez por arquivo; some da fila = esquecido. Fila ausente/ilegível = []."""
    try:
        with open(caminho or FILA_DOWNLOAD, encoding="utf-8") as fh:
            dados = json.load(fh)
    except Exception:  # noqa: BLE001 - ausente/corrompida: o download_svc é quem trata
        return []
    brutos = dados.get("itens") if isinstance(dados, dict) else dados
    saida, vistos = [], set()
    for it in brutos if isinstance(brutos, list) else []:
        if not isinstance(it, dict):
            continue
        cat = CATEGORIA_DOWNLOAD.get(str(it.get("estado") or ""))
        fid = str(it.get("file_id") or "").strip()
        if not cat or not fid or fid in vistos:
            continue
        vistos.add(fid)
        x = {"lote": f"download:{fid}", "categoria": cat,
             "motivo": cpf_util_mascarar_texto(it.get("erro") or it.get("estado"))}
        if it.get("lote_id"):
            x["lote_origem"] = str(it["lote_id"])
        saida.append(x)
    return saida


def avisos_por_mudanca(itens: list[dict], ultimo: dict, enviados: set,
                       quando: str = "") -> tuple[list[dict], dict]:
    """F2-07: só avisa o lote cuja categoria MUDOU desde o último aviso.

    `ultimo` = {lote: {"categoria", "em"}}. Lote avisado antes que agora está na fila
    gera UM aviso 'enfileirado'. Lote que sumiu das categorias é esquecido (se voltar,
    avisa de novo). Devolve (itens a imprimir, nova memória)."""
    novo, imprimir = {}, []
    for x in itens:
        cat = x.get("categoria")
        if cat not in CATEGORIAS_AVISO:
            continue
        lid = x["lote"]
        anterior = (ultimo.get(lid) or {}).get("categoria")
        novo[lid] = {"categoria": cat, "em": (ultimo.get(lid) or {}).get("em") if anterior == cat else quando}
        if anterior != cat:
            imprimir.append(x)
    for lid, reg in (ultimo or {}).items():
        if lid in novo:
            continue
        cat = (reg or {}).get("categoria")
        if lid in enviados and cat not in (None, "enfileirado"):
            imprimir.append({"lote": lid, "categoria": "enfileirado", "motivo": f"antes: {cat}"})
            novo[lid] = {"categoria": "enfileirado", "em": quando}
    return imprimir, novo


def main() -> int:
    p = argparse.ArgumentParser(description="Lotes que não chegaram ao painel (avisa só mudança)")
    p.add_argument("--minutos", type=int, default=30, help="tempo aceitável parado (padrão 30)")
    p.add_argument("--detalhe", action="store_true", help="lista todos os lotes na saída")
    a = p.parse_args()

    try:
        estado = estado_store.ler_estado(ESTADO)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Triagem: não consegui ler o estado dos lotes ({type(e).__name__})")
        return 1

    agora_ts = datetime.now().timestamp()
    quando = datetime.now().astimezone().replace(microsecond=0).isoformat()
    controle = _ler_controle()
    enviados, cpfs_mandados = enviados_da_ponte()
    travados = lotes_travados(estado, enviados, agora_ts, a.minutos, cpfs_mandados)
    itens = travados + itens_do_controle(controle, agora_ts, a.minutos) + itens_da_fila_download()
    por_tipo: dict = {}
    por_categoria: dict = {}
    for x in travados:
        por_tipo.setdefault(x["tipo"], []).append(x)
    for x in itens:
        por_categoria.setdefault(x["categoria"], []).append(x)
    sem_motivo = [x for x in travados if x.get("sem_motivo_anotado")]
    total_arquivos = sum(x["arquivos"] for x in travados)
    # chaves antigas preservadas (algo pode ler o json); novas: por_categoria
    estado_store.gravar_atomico({
        "quando": quando, "minimo_minutos": a.minutos, "travados": len(travados),
        "podem_encaminhar": len(por_tipo.get("pronto") or ()),
        "cpf_ja_enviado": len(por_tipo.get("repetido") or ()),
        "revisar": len(por_tipo.get("revisar") or ()) + len(por_tipo.get("incompleto") or ()),
        "por_tipo": {t: len(v) for t, v in por_tipo.items()},
        "por_categoria": {t: len(v) for t, v in por_categoria.items()},
        "sem_motivo_anotado": len(sem_motivo),
        "arquivos_parados": total_arquivos,
        "itens": itens[:200]}, SAIDA)

    try:
        memoria = estado_store.ler_estado(AVISOS).get("lotes") or {}
    except estado_store.EstadoCorrompido:
        memoria = {}
    imprimir, memoria_nova = avisos_por_mudanca(itens, memoria, enviados, quando)
    estado_store.gravar_atomico({"atualizado_em": quando, "lotes": memoria_nova}, AVISOS)
    if not imprimir:
        return 0                      # silêncio: nada MUDOU

    grupos: dict = {}
    for x in imprimir:
        grupos.setdefault(x["categoria"], []).append(x)
    print(f"⚠️ Lotes de documentos — {len(imprimir)} mudança(s) desde o último aviso:")
    for cat in CATEGORIAS_AVISO:
        xs = grupos.get(cat) or []
        if not xs:
            continue
        print(f"   • {len(xs)} {ROTULO_CATEGORIA[cat]}:")
        for x in (xs if a.detalhe else xs[:5]):
            linha = f"     {x['lote']}"
            if x.get("cpf"):
                linha += f" · CPF {x['cpf']}"
            if x.get("arquivos") is not None:
                linha += f" · {x['arquivos']} arq. · parado {x['minutos'] // 60}h{x['minutos'] % 60:02d}"
            if x.get("lote_origem"):
                linha += f" · origem {x['lote_origem']}"
            if x.get("motivos_revisao"):
                linha += " · motivos: " + ", ".join(cpf_util_mascarar_texto(m)[:40]
                                                    for m in x["motivos_revisao"][:2])
            elif x.get("motivo"):
                linha += f" · {cpf_util_mascarar_texto(x['motivo'])[:70]}"
            print(linha)
        if not a.detalhe and len(xs) > 5:
            print(f"     (+{len(xs) - 5} lote(s) — detalhe em {SAIDA})")
    novos_sem_motivo = [x for x in imprimir if x.get("sem_motivo_anotado")]
    if novos_sem_motivo:
        print(f"   ⚠ {len(novos_sem_motivo)} lote(s) SEM motivo anotado em motivos_revisao "
              "(o porquê nem foi registrado — investigar)")
    return 1


def cpf_util_mascarar_texto(texto) -> str:
    import seguranca

    return seguranca.mascarar(str(texto or ""))


if __name__ == "__main__":
    raise SystemExit(main())
