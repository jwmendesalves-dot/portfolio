#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FASE 2 — PONTE REAL: lote PRONTO do grupo (fase 1) → staging do painel → fila do worker.

O que faz, para cada lote escolhido:

  1. monta o job no MESMO formato da fila do painel (reusa `lote_para_job.montar_job`)
     — cliente nome/CPF, fichinha no layout do painel;
  2. copia os DOCUMENTOS para  <SEPARADOR>/data/staging/<upload_id>/
     — 1 de cada por sha256; arquivo "<file_id>.<ext>" + sidecar
       "<file_id>.<ext>.meta.json" = {id, nome_original, mime} (o worker LÊ o sidecar);
     — mídia (áudio/vídeo, `tipos_documento`) NUNCA vai ao staging; tipo desconhecido só
       vai se a assinatura do conteúdo provar que é documento (S10);
  3. grava na fila do worker via `FilaJobs.enfileirar()` do painel (node), sem parar o
     worker e sem editar o JSON na mão.

Quem pode ir (fonte única, F2-01/F2-02): `ponte_criterio.avaliar` =
`estado_lote.pode_enfileirar` sobre a visão SÓ-DOCUMENTOS do lote (identidade OK com CPF
válido, fechado, sem revisão, TODO documento baixado com sha256 e tamanho > 0) + a regra
do escritório (`ponte_criterio.motivo_recusa`). Lote com documento pendente fica em
AGUARDANDO_DOWNLOAD — não vai parcial.

Ordem e limite (F2-04): filtra TUDO primeiro (elegibilidade, janela `--dias`, duplicata,
CPF repetido), ordena por confiança ALTA (traço/LOTE-SIG) e depois pelo `inicio` mais
antigo, e só então aplica `--limite` contando os lotes que de fato montaram staging.

Duplicata (F2-03): só se decide com o kit 100% baixado; com pendência = indeterminado
(adiado, nunca descartado). Documento que chega DEPOIS de um envio fica listado em
`fila-enviados.json` → `docs_posteriores` do lote (F2-08: envio complementar não é
automático porque o contrato do painel para complemento não está confirmado).

Concorrência e falha (F2-05/F2-06/A4): `--enviar` roda sob a trava da rodada
(`caminhos.em_base("fila.lock")`, a MESMA do ponte-rodar.sh/pipeline.sh); o controle é
relido/gravado sob `estado_store` (flock + gravação atômica 0600). Cada lote é registrado
como `enfileirando` ANTES do staging e só vira `enfileirado` quando o job aparece na fila;
a rodada seguinte promove ou limpa o órfão. `upload_id` é determinístico
(lote + conjunto de sha256). Temporários do node em `seguranca.arquivo_temp_privado`.

Uso:
  lote_para_fila.py --status
  lote_para_fila.py --simular --todos                 (offline; mesmos filtros do real)
  lote_para_fila.py --enviar  --todos                 (padrão: --dias 7 --limite 4)
  lote_para_fila.py --enviar  --todos --dias 0 --limite 0     (catch-up explícito)
  lote_para_fila.py --enviar  --lote LOTE-SIG-...     (mesmos filtros, sem janela/limite)
  lote_para_fila.py --enviar  --lote LOTE-... --forcar --operador NOME --motivo "TEXTO"
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

import caminhos
import cpf_util
import estado_lote
import estado_store
import seguranca
import lote_para_job as L  # noqa: E402  (reusa parse da ficha, dedupe, montar_job)
from ponte_criterio import (avaliar, chave_ordem, hashes_do_lote, lote_documental,
                            situacao_kit, tipo_arquivo)

BASE = caminhos.BASE
APP = caminhos.SEPARADOR
STAGING = caminhos.em_separador("data", "staging")
FILA = caminhos.em_separador("data", "fila-jobs.json")
ESPELHO = caminhos.em_separador("data", "jobs.json")
FILA_JS = caminhos.em_separador("src", "fila-jobs.js")
CONTROLE = caminhos.em_base("fila-enviados.json")
BACKUPS = caminhos.em_base("backups")
ARQ_ESTADO = estado_store.ARQ_ESTADO
# trava da RODADA: estado_store.trava_arquivo(X) trava "X.lock" → caminhos.em_base("fila.lock"),
# o mesmo arquivo que ponte-rodar.sh / pipeline.sh seguram com `flock`.
TRAVA_RODADA = caminhos.em_base("fila")
NODE = os.environ.get("TRIAGEM_NODE") or "/root/.hermes/node/bin/node"
WORKER_SVC = "agente-separador-worker.service"
NODE_TIMEOUT_S = 180
BACKUPS_MANTER = 48          # backups fila-jobs-* mais recentes sempre mantidos
BACKUPS_DIAS = 14            # + 1 por dia até 14 dias (OP-O2)
NAMESPACE_UPLOAD = uuid.UUID("5b0f3f8e-3c7a-5d55-9d1e-2a6b7c8d9e10")

MIME_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "heic": "image/heic", "heif": "image/heif",
    "pdf": "application/pdf", "gif": "image/gif", "bmp": "image/bmp",
    "tif": "image/tiff", "tiff": "image/tiff",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text", "rtf": "application/rtf",
    "txt": "text/plain", "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    # Áudio/vídeo NÃO estão aqui de propósito (S10/C1): mídia nunca vai ao staging.
}


def agora() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _so_digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _cpf_mascarado(v) -> str:
    return cpf_util.mascarar(v) if _so_digitos(v) else "-"


# ---------------------------------------------------------------------------
# estado / controle (estado_store: flock + gravação atômica 0600; corrompido NÃO vira {})
# ---------------------------------------------------------------------------
def carregar_estado() -> dict:
    return estado_store.ler_estado(ARQ_ESTADO)


def carregar_controle() -> dict:
    return estado_store.ler_estado(CONTROLE)


def atualizar_controle(mutador) -> dict:
    """Relê `fila-enviados.json` sob trava, aplica o mutador e grava atômico (F2-05)."""
    return estado_store.atualizar_estado(mutador, caminho=CONTROLE)


def confirmado(c) -> bool:
    """Registro de controle que já está NA FILA (legado sem `estado` = enfileirado)."""
    return isinstance(c, dict) and c.get("estado", "enfileirado") != "enfileirando"


def _hashes_do_registro(c: dict) -> set:
    if c.get("sha256s"):
        return {str(h)[:12] for h in c["sha256s"] if h}
    return {str(nome).split(".")[0][:12] for nome in (c.get("arquivos") or [])}


def marcar_duplicatas_no_estado(descartes: dict, caminho: str | None = None) -> list:
    """Grava no estado (dono "ponte", `estado_store.atualizar_lotes`) o desfecho dos kits
    repetidos e devolve os lotes NOVOS nessa marcação — só esses geram alerta.

      status          = 'descartado_duplicata' (reenvio do mesmo kit, 100% baixado) OU
                        'revisao_duplicata'    (mesmo kit, mas o envio anterior saiu com
                        nome ruído — decisão humana, não descarte)
      status_anterior / descartado_em / lote_origem / descarte_motivo

    Dedupe do alerta: lote já marcado com o MESMO status e a MESMA origem não é
    reescrito nem reimpresso.
    """
    if not descartes:
        return []
    caminho = caminho or ARQ_ESTADO
    lotes = (estado_store.ler_estado(caminho).get("lotes") or {})
    mudancas, novos = {}, []
    for lid, info in descartes.items():
        lote = lotes.get(lid)
        if not isinstance(lote, dict):
            continue
        if lote.get("status") == info["status"] and lote.get("lote_origem") == info["lote_origem"]:
            continue
        patch = {"status": info["status"], "descartado_em": info["descartado_em"],
                 "lote_origem": info["lote_origem"], "descarte_motivo": info["motivo"]}
        if "status_anterior" not in lote:
            patch["status_anterior"] = lote.get("status")
        if info["status"] == "revisao_duplicata":
            patch.update({"requer_revisao": True, "motivos_revisao": [info["motivo"]],
                          "pronto": False})
        mudancas[lid] = patch
        novos.append(lid)
    if not mudancas:
        return []
    conflitos = estado_store.atualizar_lotes(mudancas, "ponte", caminho=caminho)
    return [lid for lid in novos
            if "<lote inexistente>" not in conflitos.get(lid, [])
            and "status" not in conflitos.get(lid, [])]


def marcar_revisao_no_estado(motivos: dict, caminho: str | None = None) -> list:
    """{lote_id: motivo} → acrescenta o motivo de revisão (dono "ponte") só se ainda não
    estiver lá. Devolve os lotes marcados agora."""
    if not motivos:
        return []
    caminho = caminho or ARQ_ESTADO
    lotes = (estado_store.ler_estado(caminho).get("lotes") or {})
    mudancas = {}
    for lid, motivo in motivos.items():
        lote = lotes.get(lid)
        if isinstance(lote, dict) and motivo not in (lote.get("motivos_revisao") or []):
            mudancas[lid] = {"motivos_revisao": [motivo], "requer_revisao": True, "pronto": False}
    if mudancas:
        estado_store.atualizar_lotes(mudancas, "ponte", caminho=caminho)
    return list(mudancas)


def cpfs_na_planilha() -> dict:
    """CPFs que JÁ TÊM LINHA na planilha (usa o node, mesma autenticação do painel)."""
    try:
        r = subprocess.run([NODE, caminhos.em_base("planilha-cpfs.js")],
                           capture_output=True, text=True, cwd=APP, timeout=180)
        if r.returncode != 0:
            print(f"  [aviso] não consegui ler a planilha: {seguranca.mascarar(r.stderr.strip()[:150])}")
            return {}
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] falha lendo a planilha: {type(e).__name__}")
        return {}


# ---------------------------------------------------------------------------
# trava da rodada (F2-01/F2-05): a MESMA de ponte-rodar.sh e pipeline.sh
# ---------------------------------------------------------------------------
def _fd_trava_herdada():
    """fd da trava já segura pelo shell (`exec 9>>fila.lock; flock -n 9`), exportado em
    TRIAGEM_FILA_LOCK_FD. Confere que é o MESMO arquivo e que a trava é nossa: reabrir o
    arquivo daria outra descrição e brigaria com a trava do próprio pai."""
    v = os.environ.get("TRIAGEM_FILA_LOCK_FD")
    if not v:
        return None
    try:
        fd = int(v)
        st, alvo = os.fstat(fd), os.stat(TRAVA_RODADA + ".lock")
        if (st.st_dev, st.st_ino) != (alvo.st_dev, alvo.st_ino):
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # mesma descrição: não bloqueia
        return fd
    except (ValueError, OSError):
        return None


@contextlib.contextmanager
def trava_rodada(timeout: float = 2):
    """Exclusão entre rodadas de --enviar. Ocupada → TimeoutError."""
    if _fd_trava_herdada() is not None:
        yield "herdada"
        return
    with estado_store.trava_arquivo(TRAVA_RODADA, timeout=timeout):
        yield "propria"


# ---------------------------------------------------------------------------
# fichinha: tira as linhas de campo vazio (o extrator do painel se perde nelas)
# ---------------------------------------------------------------------------
RX_LINHA_CAMPO = re.compile(r"^\s*•\s*\*\*(?P<rot>[^*]+)\*\*\s*[:：]?\s*(?P<val>.*)$")


def fichinha_sem_vazios(texto: str) -> tuple[str, int]:
    saida, removidas = [], 0
    for linha in texto.splitlines():
        m = RX_LINHA_CAMPO.match(linha)
        if m:
            val = m.group("val").strip()
            if re.search(r"\bSim\b.*\bNão\b", val):        # linha de checkbox
                if "x" not in val.lower():
                    removidas += 1
                    continue
            elif not val:
                removidas += 1
                continue
        saida.append(linha.rstrip())
    return "\n".join(saida).strip(), removidas


# ---------------------------------------------------------------------------
# tipo do arquivo para o staging (S10)
# ---------------------------------------------------------------------------
def _assinatura_documento(caminho: str) -> str | None:
    """Extensão de documento provada pelo CONTEÚDO (magic number) ou None."""
    try:
        with open(caminho, "rb") as fh:
            cab = fh.read(16)
    except OSError:
        return None
    if cab.startswith(b"%PDF"):
        return "pdf"
    if cab.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if cab.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if cab[:4] == b"RIFF" and cab[8:12] == b"WEBP":
        return "webp"
    if cab[4:8] == b"ftyp" and cab[8:12] in (b"heic", b"heix", b"heif", b"mif1", b"msf1", b"hevc"):
        return "heic"
    if cab.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if cab.startswith((b"II*\x00", b"MM\x00*")):
        return "tif"
    return None


def _ext_documento(meta: dict, caminho: str) -> str | None:
    """Extensão de DOCUMENTO para o staging, ou None (não vai). Mídia → None."""
    import tipos_documento

    if tipo_arquivo(meta) == "midia":
        return None
    for nome in (caminho, meta.get("nome_salvo"), meta.get("nome_original")):
        ext = os.path.splitext(str(nome or "").lower())[1]
        if ext in tipos_documento.EXT_DOCUMENTO:
            return ext.lstrip(".")
        if ext in tipos_documento.EXT_MIDIA:
            return None
    prova = _assinatura_documento(caminho)
    if prova:
        return prova                      # 'desconhecido' reclassificado pelo conteúdo
    if str(meta.get("tipo") or "").lower() == "documento":
        return "bin"                      # reclassificado explicitamente pelo download
    return None


# ---------------------------------------------------------------------------
# plano: o que iria para o staging
# ---------------------------------------------------------------------------
def chave_logica(lote_id: str, sha256s) -> str:
    """Chave estável do envio: lote + CONJUNTO de sha256 dos documentos (F2-06)."""
    base = f"{lote_id}|{','.join(sorted(str(h) for h in sha256s if h))}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def upload_id_para(lote_id: str, sha256s, reenvio: int = 0) -> str:
    sufixo = f"|r{reenvio}" if reenvio else ""
    return str(uuid.uuid5(NAMESPACE_UPLOAD, chave_logica(lote_id, sha256s) + sufixo))


@contextlib.contextmanager
def _pasta_do_cliente(offline: bool):
    """`montar_job` consulta o Bitrix (código da pasta). --simular é OFFLINE (devolve "");
    no real, qualquer falha — inclusive o SystemExit de `nomear_lotes_por_cpf` (F2-12) —
    vira "" em vez de derrubar a rodada."""
    original = getattr(L, "codigo_pasta_do_cliente", None)
    if original is None:
        yield
        return

    def seguro(cpf):
        if offline:
            return ""
        try:
            return original(cpf)
        except (Exception, SystemExit):  # noqa: BLE001
            return ""

    L.codigo_pasta_do_cliente = seguro
    try:
        yield
    finally:
        L.codigo_pasta_do_cliente = original


def planejar(lote: dict, job_id: str | None = None, reenvio: int = 0,
             offline: bool = False, forcar: bool = False) -> dict:
    """Plano de staging do lote (só documentos). `sim["recusa"]` não vazio = o lote NÃO vai."""
    view = lote_documental(lote)
    with _pasta_do_cliente(offline):
        sim = L.montar_job(view)
    fichinha, removidas = fichinha_sem_vazios(sim["job"]["fichinha"])
    sim["job"]["fichinha"] = fichinha
    sim["job"]["id"] = job_id or f"grupo-{lote['lote_id']}"
    sim["job"]["historico"] = [{"em": agora(), "evento": "enfileirado pela ponte do grupo"}]  # F2-10

    metas = [m for m in (view.get("arquivos") or [])]
    enviar, pulados, recusa, revisao = [], [], [], []
    for i, a in enumerate(sim["arquivos"]):
        meta = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
        a["file_id"] = meta.get("file_id")                                        # F2-10
        rotulo = f"arquivo #{i + 1}"
        if a.get("duplicado_de"):
            pulados.append({"no_lote": rotulo, "motivo": "duplicado (mesmo sha256) no lote"})
            continue
        if not a.get("existe"):
            recusa.append(f"{rotulo}: ausente no disco")
            continue
        if meta.get("sha256") and a.get("sha256") and str(meta["sha256"]) != str(a["sha256"]):
            recusa.append(f"{rotulo}: sha256 do disco difere do registrado")
            continue
        if not a.get("tamanho"):
            recusa.append(f"{rotulo}: arquivo de 0 byte")
            continue
        ext = _ext_documento(meta, a["caminho"])
        if not ext:
            if forcar:
                pulados.append({"no_lote": rotulo, "motivo": "tipo não reconhecido como documento"})
            else:
                revisao.append(f"{rotulo}: tipo não reconhecido como documento")
            continue
        a["_ext"] = ext
        enviar.append(a)
    sha256s = sorted({str(a["sha256"]) for a in enviar if a.get("sha256")})
    uid = upload_id_para(lote["lote_id"], sha256s, reenvio)
    sim["job"]["upload_id"] = uid
    for a in enviar:
        ext = a.pop("_ext")
        fid = re.sub(r"[^\w.-]", "_", str(a.get("file_id") or a["sha256"][:12]))
        a["staged_nome"] = f"{fid}.{ext}"
        a["staged_path"] = os.path.join(STAGING, uid, f"{fid}.{ext}")
        a["mime"] = MIME_EXT.get(ext, "application/octet-stream")
        a["nome_original_grupo"] = a.get("origem") or os.path.basename(a["caminho"])

    sim["vai_enviar"] = enviar
    sim["pulados"] = pulados
    sim["recusa"] = recusa + revisao
    sim["recusa_revisao"] = revisao
    sim["sha256s"] = sha256s
    sim["campos_vazios_removidos"] = removidas
    sim["midias_fora"] = view.get("_midias_fora", 0)
    return sim


def imprimir_plano(sim: dict, mostrar_ficha: bool = False, detalhe: bool = False) -> None:
    """Plano SEM nome de cliente e com CPF mascarado (S04); o lote é o identificador."""
    j = sim["job"]
    print(f"  job id      : {j['id']}")
    print(f"  upload_id   : {j['upload_id']}")
    print(f"  cliente     : cpf={_cpf_mascarado(j['cliente'].get('cpf'))}  "
          f"nome={'identificado' if j['cliente'].get('nome') else 'NÃO identificado'}")
    print(f"  origem      : modo={'traço' if sim.get('modo') == 'marcadores' else 'janela'}  "
          f"fechado_por={sim.get('fechado_por')}  pronto_por={sim.get('pronto_por')}")
    print(f"  arquivos    : {len(sim['vai_enviar'])} vão p/ staging "
          f"({len(sim['pulados'])} fora, {sim.get('midias_fora', 0)} mídia não enviada)  |  "
          f"fichinha {len(j['fichinha'])} chars ({sim['campos_vazios_removidos']} campo(s) vazio(s) removido(s))")
    for a in sim["vai_enviar"]:
        print(f"    + {a['staged_nome']:24s} {a['mime']:18s} {a.get('tamanho') or 0} bytes")
    for p in sim["pulados"]:
        print(f"    - {p['no_lote']}  ({p['motivo']})")
    for r in sim.get("recusa") or []:
        print(f"    x {r}")
    if sim.get("avisos"):
        if detalhe:
            for av in sim["avisos"]:
                print(f"    ! {seguranca.mascarar(str(av))}")
        else:
            print(f"    ! {len(sim['avisos'])} aviso(s) do montar_job (use --detalhe)")
    if mostrar_ficha:
        print("  ---- fichinha que iria (CPF/senha mascarados) ----")
        for ln in seguranca.mascarar(j["fichinha"]).splitlines():
            print(f"  | {ln}")


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------
def montar_staging(sim: dict) -> str:
    uid = sim["job"]["upload_id"]
    destino = os.path.join(STAGING, uid)
    os.makedirs(destino, exist_ok=True)
    for a in sim["vai_enviar"]:
        shutil.copy2(a["caminho"], a["staged_path"])
        with open(a["staged_path"] + ".meta.json", "w", encoding="utf-8") as fh:
            json.dump({"id": str(a.get("file_id") or ""), "nome_original": a["nome_original_grupo"],
                       "mime": a["mime"]}, fh, ensure_ascii=False)
    return destino


def limpar_staging(uid: str | None) -> None:
    if not uid or os.sep in str(uid) or str(uid) in (".", ".."):
        return
    d = os.path.join(STAGING, uid)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# fila (via FilaJobs do painel, sem parar o worker)
# ---------------------------------------------------------------------------
class WorkerOcupado(Exception):
    """A fila tem job em processamento — não dá para parar o worker agora."""


def esperar_ocioso(timeout_s: int = 300) -> bool:
    """True se não há job 'processando' na fila (seguro para parar o worker)."""
    t0 = time.time()
    avisou = False
    while time.time() - t0 < timeout_s:
        jobs = _ler_fila() or []
        andando = [j["id"] for j in jobs if j.get("status") == "processando"]
        if not andando:
            return True
        if not avisou:
            print(f"  worker ocupado ({len(andando)} job(s) em processamento) — esperando ficar livre…")
            avisou = True
        time.sleep(5)
    return False


def perdidos_relevantes(antes, depois, novos) -> list[str]:
    """Ids que sumiram da fila e NÃO podem ter sumido.

    O FilaJobs mantém só os 100 últimos registros TERMINAIS (retenção normal): job
    antigo já concluído/erro sair da lista é esperado. É problema quando some:
      · um registro NOSSO (acabou de ser gravado), ou
      · um registro que estava ATIVO (pendente/processando/retomavel).
    """
    terminais = {"concluido", "erro", "dead-letter"}
    ids_depois = {str((j or {}).get("id")) for j in (depois or [])}
    ids_novos = {str((j or {}).get("id")) for j in (novos or [])}
    sumidos = []
    for j in (antes or []):
        if not isinstance(j, dict) or not j.get("id"):
            continue
        ident = str(j["id"])
        if ident in ids_depois:
            continue
        if ident in ids_novos or str(j.get("status") or "") not in terminais:
            sumidos.append(ident)
    for ident in ids_novos:
        if ident not in ids_depois:
            sumidos.append(ident)
    vistos, saida = set(), []
    for ident in sumidos:
        if ident not in vistos:
            vistos.add(ident)
            saida.append(ident)
    return saida


def _ler_fila():
    """Lista de jobs da fila ou None se ilegível/ausente (não dá para afirmar nada)."""
    try:
        with open(FILA, encoding="utf-8") as fh:
            dados = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return dados if isinstance(dados, list) else None


def _rx_backup():
    return re.compile(r"^fila-jobs-(\d{8})-(\d{6})(?:-\d+)?\.json$")


def podar_backups_fila(manter_ultimos: int = BACKUPS_MANTER, dias: int = BACKUPS_DIAS,
                       simular: bool = False, agora_ts: float | None = None) -> list:
    """Retenção dos `backups/fila-jobs-*.json` (OP-O2): mantém os `manter_ultimos` mais
    recentes + o mais recente de cada dia dos últimos `dias` dias; o resto é apagado.
    Só toca arquivos com o padrão exato do nome. Devolve a lista (apagada ou que seria)."""
    if not os.path.isdir(BACKUPS):
        return []
    rx = _rx_backup()
    itens = []
    for nome in os.listdir(BACKUPS):
        m = rx.match(nome)
        if not m:
            continue
        try:
            quando = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        itens.append((quando, nome))
    itens.sort(reverse=True)
    limite = dt.datetime.fromtimestamp(agora_ts or time.time()) - dt.timedelta(days=dias)
    manter, dias_vistos = set(), set()
    for i, (quando, nome) in enumerate(itens):
        if i < manter_ultimos:
            manter.add(nome)
            dias_vistos.add(quando.date())
        elif quando >= limite and quando.date() not in dias_vistos:
            manter.add(nome)
            dias_vistos.add(quando.date())
    apagar = [nome for _q, nome in itens if nome not in manter]
    if not simular:
        for nome in apagar:
            try:
                os.unlink(os.path.join(BACKUPS, nome))
            except FileNotFoundError:
                pass
    return apagar


def backup_fila() -> str | None:
    """Cópia 0600 (dir 0700) da fila antes de enfileirar; não duplica conteúdo igual ao
    último backup; aplica a retenção (OP-O2)."""
    try:
        with open(FILA, "rb") as fh:
            dados = fh.read()
    except OSError:
        return None
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    rx = _rx_backup()
    existentes = sorted(n for n in os.listdir(BACKUPS) if rx.match(n))
    if existentes:
        ultimo = os.path.join(BACKUPS, existentes[-1])
        try:
            with open(ultimo, "rb") as fh:
                if hashlib.sha256(fh.read()).digest() == hashlib.sha256(dados).digest():
                    return ultimo
        except OSError:
            pass
    marca = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = os.path.join(BACKUPS, f"fila-jobs-{marca}.json")
    n = 0
    while True:
        try:
            fd = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
            destino = os.path.join(BACKUPS, f"fila-jobs-{marca}-{n}.json")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, dados)
        os.fsync(fd)
    finally:
        os.close(fd)
    podar_backups_fila()
    return destino


# Script do node: o caminho do fila-jobs.js do painel vem por argumento (sem /opt literal).
# O comportamento de FilaJobs.enfileirar (merge por id, retorno p/ id existente, gravação
# atômica) NÃO está nesta cópia — ver E-ponte.md, "o que verificar no servidor".
SCRIPT_NODE = r'''
const [, , filaJs, filaPath, entradasPath] = process.argv;
const { FilaJobs } = require(filaJs);
const fs = require('fs');
const silencioso = { info() {}, warn() {}, error() {}, debug() {} };
const fila = new FilaJobs(filaPath, { log: silencioso });
const entradas = JSON.parse(fs.readFileSync(entradasPath, 'utf8'));
const saida = [];
for (const e of entradas) {
  try { const j = fila.enfileirar(e); saida.push({ id: e.id, criado: !!j }); }
  catch (err) { saida.push({ id: e.id, criado: false, erro: err.message }); }
}
console.log(JSON.stringify(saida));
'''


def gravar_na_fila(jobs: list[dict]) -> list[dict]:
    """Grava os jobs na fila do worker SEM parar o worker, via `FilaJobs.enfileirar` do
    painel (mesmo caminho do `POST /api/disparar`).

    Temporários (script + entradas com fichinha/CPF) em `seguranca.arquivo_temp_privado`
    (diretório 0700, arquivo 0600, nome único) e apagados com `remover_seguro` no finally
    (S09). Falha do node (código ≠ 0, timeout, saída ilegível) levanta exceção: quem chama
    confere a fila e limpa o que não entrou.
    """
    bkp = backup_fila()
    antes_lista = _ler_fila() or []
    script = seguranca.arquivo_temp_privado(sufixo=".js", prefixo="enfileirar-")
    entradas = seguranca.arquivo_temp_privado(sufixo=".json", prefixo="entradas-")
    try:
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(SCRIPT_NODE)
        with open(entradas, "w", encoding="utf-8") as fh:
            json.dump(jobs, fh, ensure_ascii=False)
        print(f"  backup da fila: {bkp or '(fila ausente)'} ({len(antes_lista)} jobs)")
        r = subprocess.run([NODE, script, FILA_JS, FILA, entradas],
                           capture_output=True, text=True, check=True, timeout=NODE_TIMEOUT_S)
        res = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        seguranca.remover_seguro(script)
        seguranca.remover_seguro(entradas)

    depois_lista = _ler_fila() or []
    print(f"  fila: {len(antes_lista)} → {len(depois_lista)} jobs (+{len(depois_lista) - len(antes_lista)})")
    perdidos = perdidos_relevantes(antes_lista, depois_lista, jobs)
    if perdidos:
        print(f"  !! ATENÇÃO: a fila perdeu {len(perdidos)} registro(s) ATIVO/novo: {perdidos[:5]}")
    return res


# ---------------------------------------------------------------------------
# controle: enfileirando → enfileirado (F2-06)
# ---------------------------------------------------------------------------
def _confirmar(c: dict, job: dict) -> dict:
    novo = {k: v for k, v in c.items()
            if k not in ("job_id_planejado", "upload_id_planejado", "anterior")}
    novo["estado"] = "enfileirado"
    novo["job_id"] = job.get("id") or c.get("job_id_planejado")
    novo["upload_id"] = job.get("upload_id") or c.get("upload_id_planejado")   # o da FILA
    novo["confirmado_em"] = agora()
    if c.get("anterior"):
        novo["envio_anterior"] = {k: c["anterior"].get(k) for k in ("job_id", "upload_id", "em")}
    return novo


def reconciliar_controle(simular: bool = False) -> dict:
    """Registros `enfileirando` (só podem ser órfãos: estamos sob a trava da rodada):
    job na fila → `enfileirado` com o upload_id lido do job; fora da fila → staging
    removido e registro desfeito (volta o envio anterior, se havia). Fila ilegível →
    nada muda (não dá para afirmar)."""
    resumo = {"promovidos": [], "limpos": [], "fila_ilegivel": False}
    fila = _ler_fila()
    if fila is None:
        resumo["fila_ilegivel"] = True
        return resumo
    por_id = {str(j.get("id")): j for j in fila if isinstance(j, dict)}
    limpar = []

    def mut(ctrl: dict):
        for lid, c in list(ctrl.items()):
            if not isinstance(c, dict) or c.get("estado") != "enfileirando":
                continue
            job = por_id.get(str(c.get("job_id_planejado")))
            if job:
                ctrl[lid] = _confirmar(c, job)
                resumo["promovidos"].append(lid)
            else:
                limpar.append(c.get("upload_id_planejado"))
                resumo["limpos"].append(lid)
                if c.get("anterior"):
                    ctrl[lid] = c["anterior"]
                else:
                    del ctrl[lid]

    if simular:
        mut(json.loads(json.dumps(carregar_controle())))
        return resumo
    atualizar_controle(mut)
    for uid in limpar:
        limpar_staging(uid)
    return resumo


def registrar_enfileirando(lid: str, reg: dict) -> None:
    def mut(ctrl: dict):
        anterior = ctrl.get(lid)
        novo = dict(reg)
        if confirmado(anterior):
            novo["anterior"] = anterior
        ctrl[lid] = novo
    atualizar_controle(mut)


def desfazer_registro(lid: str) -> None:
    def mut(ctrl: dict):
        c = ctrl.get(lid)
        if isinstance(c, dict) and c.get("estado") == "enfileirando":
            if c.get("anterior"):
                ctrl[lid] = c["anterior"]
            else:
                del ctrl[lid]
    atualizar_controle(mut)


# ---------------------------------------------------------------------------
# documento que chegou depois do envio (F2-02/F2-08)
# ---------------------------------------------------------------------------
_RX_HEX12 = re.compile(r"^[0-9a-f]{12}$")


def docs_posteriores(lotes: dict, ctrl: dict) -> dict:
    """{lote_id: [itens novos]} — documentos do lote que NÃO estavam no envio confirmado.

    Compara por sha256 (registro novo: `sha256s`; legado: nome staged = sha256[:12]) e, para
    documento ainda não baixado, por `file_id` (só registro novo, que guarda `file_ids`).
    Registro legado com nome por file_id não é comparável → ignorado (sem falso alarme).
    Itens já listados em `docs_posteriores` não voltam como novos."""
    saida = {}
    for lid, c in (ctrl or {}).items():
        if not confirmado(c):
            continue
        lote = (lotes or {}).get(lid)
        if not isinstance(lote, dict):
            continue
        if c.get("sha256s"):
            enviados = {str(h)[:12] for h in c["sha256s"] if h}
        else:
            nomes = [str(n).split(".")[0] for n in (c.get("arquivos") or [])]
            if not nomes or not all(_RX_HEX12.match(n) for n in nomes):
                continue
            enviados = set(nomes)
        fids_enviados = {str(f) for f in (c.get("file_ids") or [])}
        ja = {str(d.get("file_id") or d.get("sha256")) for d in (c.get("docs_posteriores") or [])
              if isinstance(d, dict)}
        novos = []
        for a in (lote_documental(lote).get("arquivos") or []):
            if not isinstance(a, dict):
                continue
            h = str(a.get("sha256") or "")[:12]
            fid = a.get("file_id")
            if h:
                if h in enviados:
                    continue
            elif not fids_enviados or str(fid) in fids_enviados:
                continue
            chave = str(fid or h)
            if chave in ja:
                continue
            ja.add(chave)
            novos.append({"file_id": fid, "sha256": h or None, "baixado": a.get("baixado") is True,
                          "detectado_em": agora()})
        if novos:
            saida[lid] = novos
    return saida


def registrar_docs_posteriores(novos: dict) -> None:
    if not novos:
        return

    def mut(ctrl: dict):
        for lid, itens in novos.items():
            c = ctrl.get(lid)
            if isinstance(c, dict):
                c.setdefault("docs_posteriores", []).extend(itens)
    atualizar_controle(mut)


# ---------------------------------------------------------------------------
# seleção (filtros → duplicata → CPF repetido → ordem); o limite vem DEPOIS (F2-04)
# ---------------------------------------------------------------------------
def _inicio(lote):
    try:
        ini = dt.datetime.fromisoformat(str(lote.get("inicio")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ini if ini.tzinfo else ini.replace(tzinfo=dt.timezone.utc)


def selecionar(lotes: dict, ctrl: dict, alvos_manuais=None, dias: int = 0, forcar: bool = False,
               pular_cpf: bool = True, agora_dt: dt.datetime | None = None) -> dict:
    """Decisão da rodada, SEM efeito colateral (o --simular usa a mesma função).

    Devolve {"ordenados": [ids enviáveis, na ordem], "fora": {categoria: [(id, motivo)]},
    "descartes": {id: info}, "ignorados": {id: [motivos sobrepostos por --forcar]},
    "sobreposicao": {id: [hash12 já enviados ao CPF]}, "reavaliar": {id: motivo}}.
    """
    from nome_regras import parece_ruido

    lotes = lotes or {}
    ctrl = ctrl or {}
    agora_dt = agora_dt or dt.datetime.now(dt.timezone.utc)
    res = {"ordenados": [], "fora": {}, "descartes": {}, "ignorados": {}, "sobreposicao": {},
           "reavaliar": {}}

    def fora(cat, lid, motivo):
        res["fora"].setdefault(cat, []).append((lid, motivo))

    envios_por_cpf, hashes_por_cpf = {}, {}
    for _lid, c in ctrl.items():
        if not confirmado(c):
            continue
        cp = _so_digitos((c.get("cliente") or {}).get("cpf"))
        if not cp:
            continue
        hs = _hashes_do_registro(c)
        envios_por_cpf.setdefault(cp, []).append({
            "lote_id": _lid, "em": c.get("em", ""),
            "nome": (c.get("cliente") or {}).get("nome") or "", "hashes": hs})
        hashes_por_cpf.setdefault(cp, set()).update(hs)

    if alvos_manuais is not None:
        ids = list(alvos_manuais)
    else:
        # candidatos: `pronto` (legado) OU elegível pelo modelo único (estado_lote) — lote
        # confirmado por nome+telefone não ganha mais pronto=True (frente F)
        ids = [k for k, v in lotes.items() if isinstance(v, dict)
               and (v.get("pronto") or estado_lote.pode_enfileirar(lote_documental(v))[0])]
    aptos = []
    for lid in ids:
        lote = lotes.get(lid)
        if not isinstance(lote, dict):
            fora("INEXISTENTE", lid, "lote não existe no estado")
            continue
        c = ctrl.get(lid)
        if c and not confirmado(c):
            fora("ENFILEIRANDO", lid, "envio anterior ainda não confirmado na fila")
            continue
        if c and not forcar:
            fora("ENFILEIRADO", lid, f"já enviado em {str(c.get('em', '?'))[:16]}")
            continue
        av = avaliar(lote, forcar=forcar)
        if not av["ok"]:
            fora(av["categoria"], lid, "; ".join(av["motivos"][:3]))
            # descarte antigo (talvez com download incompleto — F2-03): kit agora 100%
            # baixado e com documento NOVO para o CPF → pede reavaliação humana
            if lote.get("status") == "descartado_duplicata":
                cp = _so_digitos(lote.get("cpf"))
                if cp in hashes_por_cpf and situacao_kit(lote, hashes_por_cpf[cp]) == "novo":
                    res["reavaliar"][lid] = (
                        "descartado como duplicata, mas o kit completo tem documento novo "
                        f"para o CPF — reavaliar (p/ mandar: --lote {lid} --forcar --operador "
                        "NOME --motivo TEXTO)")
            continue
        if forcar:
            res["ignorados"][lid] = av["ignorados"]
        if alvos_manuais is None and dias > 0:
            ini = _inicio(lote)
            if ini is None:
                fora("DATA_ILEGIVEL", lid, f"início {lote.get('inicio')!r} ilegível")
                continue
            if ini < agora_dt - dt.timedelta(days=dias):
                fora("FORA_DA_JANELA", lid, f"início {str(lote.get('inicio'))[:16]} > {dias} dia(s) "
                     "— precisa catch-up (--dias 0)")
                continue
        aptos.append(lid)

    candidatos = []
    for lid in aptos:
        lote = lotes[lid]
        cp = _so_digitos(lote.get("cpf"))
        if forcar or not pular_cpf or not cp or cp not in envios_por_cpf:
            candidatos.append(lid)
            continue
        sit = situacao_kit(lote, hashes_por_cpf[cp])
        if sit == "indeterminado":                   # F2-03: nunca descarta com pendência
            fora("AGUARDANDO_DOWNLOAD", lid, "kit incompleto — duplicata indeterminada")
            continue
        if sit == "novo":
            comum = hashes_do_lote(lote) & hashes_por_cpf[cp]
            if comum:
                res["sobreposicao"][lid] = sorted(comum)
            candidatos.append(lid)
            continue
        meus = hashes_do_lote(lote)
        origem = max(envios_por_cpf[cp], key=lambda e: (len(meus & e["hashes"]), e.get("em") or ""))
        origem_id = origem.get("lote_id") or "(desconhecido)"
        origem_nome = (origem.get("nome") or "").strip()
        origem_em = (origem.get("em") or "")[:16]
        dica = f"(p/ mandar: --lote {lid} --forcar --operador NOME --motivo TEXTO)"
        if not origem_nome or parece_ruido(origem_nome):
            status, motivo = "revisao_duplicata", (
                f"CPF {_cpf_mascarado(cp)} já teve o MESMO kit enviado como {origem_id} "
                f"({origem_em}), mas aquele saiu com nome não identificado/ruído — pede "
                f"revisão humana em vez de descarte {dica}")
        else:
            status, motivo = "descartado_duplicata", (
                f"CPF {_cpf_mascarado(cp)} já enviado pela ponte em {origem_em} como "
                f"{origem_id} e o kit (100% baixado) é o mesmo (sha256) {dica}")
        res["descartes"][lid] = {"status": status, "lote_origem": origem_id, "motivo": motivo,
                                 "descartado_em": agora()}
        fora("DUPLICATA", lid, motivo)

    escolhidos = candidatos
    if pular_cpf and not forcar and alvos_manuais is None:
        por_cpf: dict = {}
        for k in candidatos:
            por_cpf.setdefault(_so_digitos(lotes[k].get("cpf")) or k, []).append(k)
        escolhidos = []
        for _cpf, ks in por_cpf.items():
            ks_ord = sorted(ks, key=lambda k: (len(lote_documental(lotes[k]).get("arquivos") or []),
                                               str(lotes[k].get("inicio"))), reverse=True)
            escolhidos.append(ks_ord[0])
            for k in ks_ord[1:]:
                fora("CPF_REPETIDO_NA_RODADA", k, f"mesmo CPF de {ks_ord[0]} (vai só o mais completo)")
    res["ordenados"] = sorted(escolhidos, key=lambda k: chave_ordem(lotes[k]))
    return res


def imprimir_selecao(sel: dict) -> None:
    print(f"-- enviáveis nesta rodada (antes do limite): {len(sel['ordenados'])}")
    for cat, itens in sorted(sel["fora"].items(), key=lambda kv: -len(kv[1])):
        print(f"     fora: {len(itens):3d} lote(s) — {cat}")
        if cat in ("ENFILEIRADO",):
            continue
        for lid, motivo in itens[:4]:
            print(f"       ex.: {lid} — {seguranca.mascarar(str(motivo))[:160]}")
        if len(itens) > 4:
            print(f"       (+{len(itens) - 4} lote(s))")
    for lid, ign in sel["ignorados"].items():
        print(f"  !! --forcar em {lid}: ignorando {len(ign)} motivo(s): "
              f"{seguranca.mascarar('; '.join(ign))[:200]}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def _ler_lista(caminho: str) -> list:
    try:
        with open(caminho, encoding="utf-8") as fh:
            dados = json.load(fh)
        return dados if isinstance(dados, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def mostrar_status() -> None:
    ctrl = carregar_controle()
    fila = {j.get("id"): j for j in _ler_lista(FILA) if isinstance(j, dict)}
    espelho = {j.get("id"): j for j in _ler_lista(ESPELHO) if isinstance(j, dict)}
    print(f"lotes no controle da ponte: {len(ctrl)}")
    if not ctrl:
        print("  (nenhum ainda)")
        return
    for lote_id, c in sorted(ctrl.items(), key=lambda kv: str(kv[1].get("em", ""))):
        jid = c.get("job_id") or c.get("job_id_planejado")
        j = fila.get(jid) or espelho.get(jid) or {}
        det = [p for p in (j.get("passos") or []) if p.get("status") != "ok"]
        print(f"  {str(c.get('em', ''))[:16]}  {lote_id}  [{c.get('estado', 'enfileirado')}]")
        print(f"      job={jid}  status={j.get('status', '?')}  etapa={j.get('etapa') or '-'}  "
              f"arquivos={c.get('enviados')}  cpf={_cpf_mascarado((c.get('cliente') or {}).get('cpf'))}"
              + (f"  docs_posteriores={len(c['docs_posteriores'])}" if c.get("docs_posteriores") else ""))
        if det:
            d = det[-1]
            print(f"      pendente: {d.get('chave')} — {seguranca.mascarar(str(d.get('detalhe', '')))[:90]}")


# ---------------------------------------------------------------------------
# testes manuais da tradução da ficha (--testar). Dados 100% SINTÉTICOS (S02):
# e-mail @example.com, telefone e senha fictícios, CPF gerado com DV válido.
# ---------------------------------------------------------------------------
def _casos() -> list:
    cpf = cpf_util.formatar(cpf_util.gerar_para_teste(20261109))
    return [
        ("cabeçalho informal (sintético)",
         """CLIENTE
Contato: 11 90000-0001
Contatos reservas: 11 90000-0002

E-mail: cliente.ficticia@example.com
Estado civil solteira
Profissão baba
Trabalha de Carteira Assinada?nao
Data prevista pro parto: 21 de dezembro
Recebe bolsa família?nao
 Semanas💘26 semanas""",
         {"email": "cliente.ficticia@example.com", "estado civil": "solteira",
          "trabalha": "nao", "bolsa": "nao", "semanas": 26},
         {"telefones": ["11 90000-0001", "11 90000-0002"]},
         ["26 semanas"], ["21 de dezembro", "Data provável do parto"]),

        ("template do painel com texto de instrução (sintético)",
         """📝 *Envie também digitado:*

• Estado civil: solteira
• *2 números de telefone* para contato, além do seu (900000003) (900000004)
• Endereço de e-mail (seu ou de alguém próximo) outra.ficticia@example.com
• *Número do NIS,* caso tenha
🔐*Senha de acesso ao GOV.BR,* Ficticia#2026""",
         {"estado civil": "solteira", "email": "outra.ficticia@example.com"},
         {}, [], ["caso tenha", "NIS", "GOV.BR,*"]),

        ("ficha do painel preenchida (sintética)",
         f"""FICHA DE ATENDIMENTO
Nome completo: MARIA FICTICIA DE TESTE
CPF: {cpf}
RG: 00.000.000-0
Rua/Endereço: Rua das Flores
Número: 100
Bairro: Centro
Cidade: Cidade Exemplo
Estado: SP
CEP: 00000-000
Trabalha atualmente? Sim
Recebe Bolsa Família? Não
De quantos meses você está?26
Telefone 1 (WhatsApp):11 90000-0005""",
         {"nome": "MARIA FICTICIA DE TESTE", "cpf": cpf, "rg": "00.000.000-0",
          "rua": "Rua das Flores", "numero": "100", "bairro": "Centro", "cidade": "Cidade Exemplo",
          "estado uf": "SP", "cep": "00000-000", "meses_diretos": 26},
         {"telefones": ["11 90000-0005"]},
         ["MARIA FICTICIA DE TESTE", cpf, "26 meses"],
         []),
    ]


def testar() -> int:
    print("=" * 78)
    print("TESTES DA PONTE — tradução da ficha do grupo p/ o layout do painel (dados sintéticos)")
    print("=" * 78)
    casos = _casos()
    falhas = 0
    for nome, texto, esperado, esperado_livre, contem, nao_contem in casos:
        campos = L.parse_ficha(texto)
        problemas = []
        for k, v in list(esperado.items()) + list(esperado_livre.items()):
            if campos.get(k) != v:
                problemas.append(f"{k}: esperado {v!r}, veio {campos.get(k)!r}")
        lote = {"lote_id": "TESTE", "cpf": campos.get("cpf", ""), "nome": campos.get("nome", ""),
                "mensagens": [{"id": 1, "texto": texto}]}
        fichinha, removidas = fichinha_sem_vazios(L.montar_fichinha_painel(lote, campos))
        for marca in contem:
            if marca not in fichinha:
                problemas.append(f"fichinha deveria conter {marca!r}")
        for marca in nao_contem:
            if marca in fichinha:
                problemas.append(f"fichinha NÃO deveria conter {marca!r}")
        vazias = [ln for ln in fichinha.splitlines()
                  if RX_LINHA_CAMPO.match(ln) and not RX_LINHA_CAMPO.match(ln).group("val").strip()]
        if vazias:
            problemas.append(f"{len(vazias)} linha(s) de campo vazio na fichinha")
        print(f"[{'OK  ' if not problemas else 'FALHA'}] {nome}  "
              f"({len(fichinha)} chars, {removidas} campo(s) vazio(s) fora)")
        for p in problemas:
            print(f"        -> {p}")
        falhas += 1 if problemas else 0
    print("-" * 78)
    print(f"RESULTADO: {len(casos) - falhas}/{len(casos)} casos OK")
    return 1 if falhas else 0


# ---------------------------------------------------------------------------
def _int_env(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def _args(argv):
    p = argparse.ArgumentParser(description="Fase 2: lote pronto → staging → fila do worker")
    p.add_argument("--lote", action="append", default=[], help="lote_id (pode repetir)")
    p.add_argument("--todos", action="store_true", help="todos os lotes prontos ainda não enviados")
    p.add_argument("--simular", action="store_true", help="só mostra o plano, offline (padrão)")
    p.add_argument("--enviar", action="store_true", help="grava staging + fila de verdade")
    p.add_argument("--status", action="store_true", help="o que já foi enviado")
    p.add_argument("--testar", action="store_true", help="testes da tradução da ficha")
    p.add_argument("--limite", type=int, default=_int_env("PONTE_LIMITE", 4),
                   help="máximo de lotes ENVIADOS por rodada com --todos (padrão 4 ou $PONTE_LIMITE; "
                        "0 = sem limite, só de propósito)")
    p.add_argument("--dias", type=int, default=_int_env("PONTE_DIAS", 7),
                   help="só lotes INICIADOS nos últimos N dias com --todos (padrão 7 ou $PONTE_DIAS; "
                        "0 = catch-up do backlog, só de propósito)")
    p.add_argument("--pular-cpf-repetido", dest="pular_cpf", action="store_true", default=True,
                   help="dedup por CPF+sha256 contra o já enviado e na rodada (padrão)")
    p.add_argument("--manter-cpf-repetido", dest="pular_cpf", action="store_false",
                   help="não aplica a dedup por CPF (identidade/integridade continuam valendo)")
    p.add_argument("--pular-ja-na-planilha", action="store_true",
                   help="não manda lote cujo CPF já tem linha na planilha (só no --enviar)")
    p.add_argument("--so-traco", action="store_true", default=True, help=argparse.SUPPRESS)
    p.add_argument("--todos-os-fechamentos", action="store_true",
                   help="REMOVIDA (F2-01): ignorada com aviso; a regra do escritório é obrigatória")
    p.add_argument("--ficha", action="store_true", help="imprime a fichinha (mascarada) no plano")
    p.add_argument("--detalhe", action="store_true", help="imprime os avisos do montar_job (mascarados)")
    p.add_argument("--forcar", action="store_true",
                   help="força manual de UM lote (--lote): exige --operador e --motivo; ignora regra do "
                        "escritório, revisão e duplicata, NUNCA identidade/integridade")
    p.add_argument("--operador", default="", help="quem força (obrigatório com --forcar)")
    p.add_argument("--motivo", default="", help="por que força (obrigatório com --forcar)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = _args(argv)
    if a.testar:
        return testar()
    if a.todos_os_fechamentos:
        print("AVISO: --todos-os-fechamentos foi REMOVIDA (F2-01) e está sendo ignorada — a regra do "
              "escritório e a elegibilidade valem sempre. Força manual: --lote X --forcar "
              "--operador NOME --motivo TEXTO.", file=sys.stderr)
    if a.forcar:
        if a.todos or not a.lote:
            print("ERRO: --forcar só vale para lote(s) explícito(s) com --lote (nunca com --todos)")
            return 2
        if not a.operador.strip() or not a.motivo.strip():
            print("ERRO: --forcar exige --operador NOME e --motivo TEXTO (ficam registrados no controle)")
            return 2
    if a.status or not (a.lote or a.todos):
        mostrar_status()
        return 0
    if a.enviar:
        try:
            with trava_rodada():
                return _rodar(a)
        except TimeoutError:
            print(f"=== outra rodada da ponte em andamento (trava {TRAVA_RODADA}.lock) — pulando")
            return 0
    return _rodar(a)


def _rodar(a) -> int:
    enviar = bool(a.enviar)
    est = carregar_estado()
    lotes = est.get("lotes") or {}

    rec = reconciliar_controle(simular=not enviar)
    if rec["promovidos"] or rec["limpos"]:
        print(f"-- controle: {len(rec['promovidos'])} envio(s) confirmado(s) na fila, "
              f"{len(rec['limpos'])} órfão(s) {'limpo(s)' if enviar else 'seriam limpos'}")
    ctrl = carregar_controle()

    posteriores = docs_posteriores(lotes, ctrl)
    if posteriores:
        print(f"📎 {len(posteriores)} lote(s) JÁ ENVIADO(S) receberam documento depois do envio "
              "(listados em fila-enviados.json → docs_posteriores; envio complementar NÃO é "
              "automático — conferir no painel):")
        for lid, itens in list(posteriores.items())[:10]:
            print(f"     {lid}: {len(itens)} documento(s) novo(s)")
        if enviar:
            registrar_docs_posteriores(posteriores)

    sel = selecionar(lotes, ctrl, alvos_manuais=(a.lote if not a.todos else None),
                     dias=a.dias, forcar=a.forcar, pular_cpf=a.pular_cpf)
    imprimir_selecao(sel)

    if sel["descartes"]:
        if enviar:
            for k in marcar_duplicatas_no_estado(sel["descartes"]):
                d = sel["descartes"][k]
                print(f"⚠️ {d['status']}: {k} — {d['motivo']}")
        else:
            print(f"   (simulação: {len(sel['descartes'])} marca(s) de duplicata NÃO gravada(s))")
    if sel["reavaliar"]:
        if enviar:
            for k in marcar_revisao_no_estado(sel["reavaliar"]):
                print(f"⚠️ reavaliar: {k} — {sel['reavaliar'][k]}")
        else:
            print(f"   (simulação: {len(sel['reavaliar'])} descarte(s) antigo(s) pediriam reavaliação)")

    alvos = list(sel["ordenados"])
    if a.pular_ja_na_planilha and alvos:
        if not enviar:
            print("-- CPF já na planilha: NÃO consultado no --simular (offline)")
        else:
            na_planilha = cpfs_na_planilha()
            ja = [k for k in alvos if _so_digitos(lotes[k].get("cpf")) in na_planilha]
            alvos = [k for k in alvos if k not in ja]
            print(f"-- CPF já na planilha: {len(ja)} lote(s) fora (painel reusaria linha/código)")

    if not alvos:
        print("nada a fazer: nenhum lote enviável nesta rodada")
        return 0

    limite = a.limite if a.todos else 0
    print("=" * 78)
    print(f"FASE 2 — {'ENVIANDO (staging + fila)' if enviar else 'SIMULAÇÃO (offline, não grava nada)'}"
          + (f" — limite {limite}" if limite else ""))
    print("=" * 78)
    preparados, revisar = [], {}
    for lid in alvos:
        if limite and len(preparados) >= limite:
            print(f"\n-- limite de {limite} lote(s) atingido; {len(alvos) - alvos.index(lid)} "
                  "enviável(is) ficam para a próxima rodada")
            break
        lote = lotes[lid]
        c_ant = ctrl.get(lid)
        reenvio, job_id = 0, f"grupo-{lid}"
        if a.forcar and confirmado(c_ant):
            reenvio = int(c_ant.get("reenvio") or 1) + 1
            job_id = f"grupo-{lid}-r{reenvio}"
        sim = planejar(lote, job_id=job_id, reenvio=reenvio, offline=not enviar, forcar=a.forcar)
        sim.update(modo=lote.get("modo"), fechado_por=lote.get("fechado_por"),
                   pronto_por=lote.get("pronto_por"))
        print(f"\n[{lid}]")
        imprimir_plano(sim, mostrar_ficha=a.ficha, detalhe=a.detalhe)
        if sim["recusa"]:
            print(f"    !! lote NÃO vai: {len(sim['recusa'])} problema(s) nos arquivos (não conta no limite)")
            if sim["recusa_revisao"]:
                revisar[lid] = ("arquivo de tipo não reconhecido como documento (S10): "
                                + "; ".join(sim["recusa_revisao"][:3]))
            continue
        if not sim["vai_enviar"]:
            print("    !! nada a enviar — lote sem documento utilizável (não conta no limite)")
            continue
        preparados.append((lid, sim, reenvio))

    if not enviar:
        print(f"\nSIMULAÇÃO: {len(preparados)} lote(s) iriam; nada foi gravado "
              "(nem estado, nem controle, nem staging, nem fila). Use --enviar para valer.")
        return 0

    if revisar:
        for k in marcar_revisao_no_estado(revisar):
            print(f"⚠️ revisão: {k} — {revisar[k]}")
    return _enviar(preparados, sel, a)


def _enviar(preparados: list, sel: dict, a) -> int:
    entradas = []
    for lid, sim, reenvio in preparados:
        job = sim["job"]
        reg = {"estado": "enfileirando", "job_id_planejado": job["id"],
               "upload_id_planejado": job["upload_id"],
               "chave": chave_logica(lid, sim["sha256s"]), "em": agora(),
               "enviados": len(sim["vai_enviar"]), "cliente": job["cliente"],
               "arquivos": [x["staged_nome"] for x in sim["vai_enviar"]],
               "sha256s": sim["sha256s"],
               "file_ids": [str(x["file_id"]) for x in sim["vai_enviar"] if x.get("file_id")]}
        if reenvio:
            reg["reenvio"] = reenvio
        if a.forcar:
            reg["forcado"] = {"operador": a.operador.strip(), "motivo": a.motivo.strip(),
                              "em": agora(), "ignorados": sel["ignorados"].get(lid, [])}
            print(f"  !! FORÇADO por {a.operador.strip()!r}: {lid} — {a.motivo.strip()[:120]}")
        if sel["sobreposicao"].get(lid):
            reg["ja_enviados_antes"] = sel["sobreposicao"][lid]
        registrar_enfileirando(lid, reg)              # ANTES de copiar (F2-06)
        limpar_staging(job["upload_id"])              # retomada: sobra parcial do mesmo upload_id
        try:
            destino = montar_staging(sim)
            n_disco = len([f for f in os.listdir(destino) if not f.endswith(".meta.json")])
            n_meta = len([f for f in os.listdir(destino) if f.endswith(".meta.json")])
            ok = n_disco == len(sim["vai_enviar"]) == n_meta
        except OSError as e:
            print(f"    !! {lid}: falha montando o staging ({type(e).__name__})")
            ok, n_disco, n_meta = False, 0, 0
        print(f"    [{lid}] staging [{'OK' if ok else 'FALHA'}] {n_disco} arquivo(s) + {n_meta} meta.json")
        if not ok:
            limpar_staging(job["upload_id"])
            desfazer_registro(lid)
            continue
        entradas.append(job)

    if entradas:
        print("\n-- gravando na fila --")
        try:
            res = gravar_na_fila(entradas)
        except (subprocess.SubprocessError, OSError, ValueError, IndexError) as e:
            print(f"  !! falha ao gravar na fila ({type(e).__name__}) — conferindo o que entrou")
            res = []
        for r in res:
            print(f"    [{('OK  ' if r.get('criado') else 'JÁ EXISTE')}] {r.get('id')}"
                  + (f"  ERRO: {seguranca.mascarar(str(r['erro']))[:200]}" if r.get("erro") else ""))
    # confirma pelo que ESTÁ na fila (criado agora ou já existente); o resto é limpo
    rec = reconciliar_controle()
    print(f"\ncontrole: {CONTROLE} — {len(rec['promovidos'])} enfileirado(s), "
          f"{len(rec['limpos'])} desfeito(s) (staging removido)"
          + (" — FILA ILEGÍVEL: registros ficam 'enfileirando' até a próxima rodada"
             if rec["fila_ilegivel"] else ""))
    return 0 if not rec["limpos"] and not rec["fila_ilegivel"] else 1


if __name__ == "__main__":
    sys.exit(main())
