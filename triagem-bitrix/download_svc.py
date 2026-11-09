# -*- coding: utf-8 -*-
"""
Serviço de download resiliente para arquivos do Bitrix24 — FORA do ciclo da triagem.

Duas fontes, cada uma com o SEU disjuntor (achado B1: o bloqueio do portal não
pode impedir a fonte livre):
  1. Bitrix  — `bitrix_cliente.chamar("disk.file.get", circuito="bitrix-download")`
     -> DOWNLOAD_URL (assinada, uso imediato). URL vencida (401/403/404/410 no host
     de download) => pede uma NOVA ao disk.file.get; nunca reusa a velha (D3).
  2. VibeCode — GET https://vibecode.bitrix24.com/v1/files/{id}/download com
     X-Api-Key (`seguranca.ler_segredo("vibecode_key")`), disjuntor
     "vibecode-download" e limitador por host.

Fila persistente `download_fila.json` (A5/D1/D2):
  · gravada SEMPRE por `estado_store.atualizar_estado` (flock + relê + tmp exclusivo +
    fsync + os.replace) — dois processos não perdem item um do outro;
  · dedupe por file_id; estados `pendente | baixado | falha_permanente |
    ignorado_midia | telegram_legado`; permanente nunca reentra;
  · por item: tentativas, ultimo_erro {classe, detalhe mascarado}, proxima_em (epoch,
    `rede_bitrix.proxima_tentativa(..., fila=True)`: 10 min -> 6 h);
  · rodada com teto de itens E de tempo total (G1); erro com `pausa_rodada`
    (QUERY_LIMIT, LOOP, credencial...) PARA a rodada — não martela os próximos.

Integridade (E1/E4/S14): streaming em blocos para um tmp exclusivo no MESMO
diretório (`.<nome>.<pid>.<uuid>.part`), teto de tamanho (DOWNLOAD_TETO_MB, padrão
50), prazo total por arquivo, 0 byte rejeitado, tamanho = esperado (Content-Length e
SIZE do disk.file.get quando conhecidos), sha256, fsync, os.replace, fsync do dir.
Qualquer falha remove o tmp. `.part/.parcial` órfãos antigos são limpos.

Mídia (tipos_documento) não é baixada nem retentada; id não numérico (CQA… do
Telegram legado) nunca vai ao disk.file.get.

Uso:
  python3 download_svc.py --enfileirar 2502027 --destino /caminho/arq.pdf
  python3 download_svc.py --processar [--limite 30] [--teto-s 300]
  python3 download_svc.py --status
Saída != 0 em --processar quando há falha que exige atenção (ver `codigo_saida`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import caminhos
import estado_store
import rede_bitrix as rb
import tipos_documento
from circuit_breaker import CircuitOpenError

log = logging.getLogger("download_svc")

# ---------------------------------------------------------------------------
# Configuração (env sobrescreve)
# ---------------------------------------------------------------------------
VIBECODE_URL = "https://vibecode.bitrix24.com/v1/files/{id}/download"


def _env_num(nome: str, padrao: float, minimo: float, maximo: float) -> float:
    try:
        v = float(os.environ.get(nome) or padrao)
    except ValueError:
        return padrao
    return min(max(v, minimo), maximo)


DEFAULT_QUEUE_FILE = os.environ.get("DOWNLOAD_FILA_JSON") or caminhos.em_base("download_fila.json")
TETO_BYTES = int(_env_num("DOWNLOAD_TETO_MB", 50, 1, 2048) * 1024 * 1024)
TIMEOUT = _env_num("DOWNLOAD_TIMEOUT_S", 30, 1, 300)            # por operação de socket
TETO_CONEXAO_S = _env_num("DOWNLOAD_TETO_CONEXAO_S", 20, 0.05, 120)  # DNS+connect+cabeçalho (guarda G1)
TETO_ITEM_S = _env_num("DOWNLOAD_TETO_ITEM_S", 120, 0.1, 1800)  # prazo TOTAL por arquivo
TETO_RODADA_S = _env_num("DOWNLOAD_TETO_RODADA_S", 300, 1, 3600)  # prazo TOTAL por rodada
LIMITE_RODADA = int(_env_num("DOWNLOAD_LIMITE_RODADA", 40, 1, 10000))
IDADE_ORFAO_S = _env_num("DOWNLOAD_IDADE_ORFAO_S", 1800, 0, 7 * 86400)
DIAS_PODA_BAIXADO = _env_num("DOWNLOAD_PODA_DIAS", 7, 0, 3650)
CHUNK = 64 * 1024
PAUSA_RATE_BITRIX_S = 180.0      # = CIRCUIT_PAUSA_SEG legado: QUERY_LIMIT abre o disjuntor já
MAX_URL_NOVA = 2                 # quantas vezes pedir DOWNLOAD_URL nova por tentativa

# compat (testes antigos/CLI): não usados no cálculo novo
DEFAULT_MAX_TENTATIVAS = 8
DEFAULT_BASE_DELAY = 600.0

PENDENTE = "pendente"
BAIXADO = "baixado"
FALHA_PERMANENTE = "falha_permanente"
IGNORADO_MIDIA = "ignorado_midia"
TELEGRAM_LEGADO = "telegram_legado"
ESTADOS_FINAIS = frozenset({BAIXADO, FALHA_PERMANENTE, IGNORADO_MIDIA, TELEGRAM_LEGADO})
ESTADOS_PERMANENTES = frozenset({FALHA_PERMANENTE, IGNORADO_MIDIA, TELEGRAM_LEGADO})
_PRIORIDADE = {BAIXADO: 3, FALHA_PERMANENTE: 2, TELEGRAM_LEGADO: 2, IGNORADO_MIDIA: 2, PENDENTE: 1}
SUFIXOS_TEMP = (".parcial", ".part", ".tmp")
TIPOS_MIDIA_BITRIX = frozenset({"audio", "video", "voice", "voz"})

# Transporte injetável (testes): `_transporte_bitrix(url, params, timeout) -> (status, texto)`
# repassado ao bitrix_cliente.chamar; `_abrir(url, headers, timeout, teto_conexao)` -> resposta.
_transporte_bitrix = None
_monotonic = time.monotonic


class FalhaDownload(Exception):
    """Compat: nenhuma fonte conseguiu baixar o item."""


class _Midia(Exception):
    """O disk.file.get revelou que o arquivo é áudio/vídeo: não baixar."""

    def __init__(self, nome: str):
        super().__init__(nome)
        self.nome = nome


class _PrazoEstourado(socket.timeout):
    """Prazo total do arquivo/rodada estourado (classificado como REDE, transitório)."""


# ---------------------------------------------------------------------------
# Segredos / clientes (nada de /tmp nem de varrer outros projetos)
# ---------------------------------------------------------------------------
def ler_webhook(caminho: str | None = None) -> str:  # noqa: ARG001 (assinatura antiga)
    """Compat: o webhook vem de `bitrix_cliente.webhook()` (seguranca.ler_segredo)."""
    import bitrix_cliente
    return bitrix_cliente.webhook()


def ler_chave_vibecode(caminho: str | None = None, obrigatorio: bool = True) -> str | None:  # noqa: ARG001
    """Chave VibeCode via seguranca.ler_segredo('vibecode_key')."""
    import seguranca
    return seguranca.ler_segredo("vibecode_key", obrigatorio=obrigatorio)


def _limitador_bitrix():
    """Host do portal (disk.file.get e DOWNLOAD_URL contam no mesmo IP/cota)."""
    return rb.limitador_padrao()


_lim_vibecode = {}


def _limitador_vibecode():
    caminho = os.environ.get("VIBECODE_RATELIMIT_FILE") or caminhos.em_base(".vibecode-ratelimit")
    if caminho not in _lim_vibecode:
        _lim_vibecode[caminho] = rb.LimitadorHost(caminho=caminho)
    return _lim_vibecode[caminho]


def _respeitar_disjuntor_legado(circ) -> None:
    """Transição (R-rede-bitrix §Migração 4): enquanto o ciclo ainda grava o legado
    `circuit-download.json` {"aberto_em": ts} (pausa de 180 s), respeitá-lo. Só lê."""
    try:
        with open(caminhos.em_base("circuit-download.json"), encoding="utf-8") as fh:
            aberto_em = float((json.load(fh) or {}).get("aberto_em") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return
    idade = time.time() - aberto_em
    if 0 <= idade < PAUSA_RATE_BITRIX_S and circ.estado == "closed":
        circ.abrir("legado circuit-download.json", duracao_s=PAUSA_RATE_BITRIX_S - idade)


# ---------------------------------------------------------------------------
# Rede: abertura com prazo duro (guarda G1/DNS) e leitura em streaming
# ---------------------------------------------------------------------------
_abandonadas = {"n": 0}
_abandonadas_lock = threading.Lock()
MAX_ABANDONADAS = 8


def _abrir_com_teto(req, timeout: float, teto: float):
    """urlopen com prazo DURO para DNS + conexão + cabeçalho.

    `urlopen(timeout=)` não limita getaddrinfo (ciclo travado 28 min em 27/10). A
    abertura roda numa thread daemon; estourou o teto -> TimeoutError e a thread é
    abandonada (fecha a resposta se chegar depois). Com muitas abandonadas vivas
    (DNS pendurado) recusa logo, em vez de empilhar threads (G2)."""
    with _abandonadas_lock:
        if _abandonadas["n"] >= MAX_ABANDONADAS:
            raise _PrazoEstourado("guarda de conexão saturada (DNS pendurado)")
    caixa: dict = {}
    pronto = threading.Event()
    lock = threading.Lock()

    def alvo():
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except BaseException as e:  # noqa: BLE001
            r, caixa["e"] = None, e
        with lock:
            if caixa.get("abandonada"):
                if r is not None:
                    try:
                        r.close()
                    except Exception:  # noqa: BLE001
                        pass
                with _abandonadas_lock:
                    _abandonadas["n"] -= 1
            else:
                caixa["r"] = r
            pronto.set()

    threading.Thread(target=alvo, name="download-abrir", daemon=True).start()
    if not pronto.wait(teto):
        with lock:
            if not pronto.is_set():
                caixa["abandonada"] = True
                with _abandonadas_lock:
                    _abandonadas["n"] += 1
                raise _PrazoEstourado(f"abertura da conexão passou de {teto:.1f}s (DNS/connect)")
    if "e" in caixa:
        raise caixa["e"]
    return caixa["r"]


def _abrir(url: str, headers: dict, timeout: float, teto_conexao: float):
    req = urllib.request.Request(url, headers=dict(headers or {}, **{"User-Agent": "triagem-download/2.0"}))
    return _abrir_com_teto(req, timeout, teto_conexao)


def _status_de(resp) -> int:
    st = getattr(resp, "status", None)
    if st is None and hasattr(resp, "getcode"):
        st = resp.getcode()
    return int(st or 200)


def _cabecalho(resp, nome: str):
    h = getattr(resp, "headers", None)
    try:
        return h.get(nome) if h is not None else None
    except Exception:  # noqa: BLE001
        return None


def _nome_tmp(destino: str) -> str:
    pasta, base = os.path.split(os.path.abspath(destino))
    return os.path.join(pasta, f".{base}.{os.getpid()}.{uuid.uuid4().hex[:12]}.part")


def _fsync_dir(pasta: str) -> None:
    try:
        fd = os.open(pasta, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _gravar_stream(resp, destino: str, esperado: int | None, prazo: float, teto: int) -> dict:
    """Grava a resposta em streaming num tmp exclusivo do MESMO diretório e só faz
    os.replace se: tamanho > 0, = Content-Length e = `esperado` (quando conhecidos),
    <= teto. Devolve {tamanho, sha256}. Qualquer falha remove o tmp."""
    pasta = os.path.dirname(os.path.abspath(destino))
    os.makedirs(pasta, exist_ok=True)
    cl = _cabecalho(resp, "Content-Length")
    try:
        cl = int(cl) if cl not in (None, "") else None
    except (TypeError, ValueError):
        cl = None
    if (cl is not None and cl > teto) or (esperado and esperado > teto):
        raise rb.ErroTamanho(f"arquivo de {max(cl or 0, esperado or 0)} B passa do teto de {teto} B")
    ler = getattr(resp, "read1", None) or resp.read
    tmp = _nome_tmp(destino)
    h = hashlib.sha256()
    total = 0
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            while True:
                if _monotonic() > prazo:
                    raise _PrazoEstourado("prazo total do download estourado")
                bloco = ler(CHUNK)
                if not bloco:
                    break
                total += len(bloco)
                if total > teto:
                    raise rb.ErroTamanho(f"download passou do teto de {teto} B")
                h.update(bloco)
                vista = memoryview(bloco)
                while vista:
                    n = os.write(fd, vista)
                    vista = vista[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if total == 0:
            raise rb.ErroIntegridade("download vazio (0 byte)")
        if cl is not None and total != cl:
            raise rb.ErroIntegridade(f"truncado: {total} B recebidos, Content-Length {cl}")
        if esperado and total != esperado:
            raise rb.ErroIntegridade(f"tamanho {total} B != esperado {esperado} B")
        os.replace(tmp, destino)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(pasta)
    return {"tamanho": total, "sha256": h.hexdigest()}


def _baixar_url(url: str, destino: str, *, headers: dict | None, esperado: int | None,
                prazo: float, teto: int, download: bool) -> dict:
    """GET + streaming. Erro HTTP/rede vira rb.FalhaBitrix(Erro classificado)."""
    restante = prazo - _monotonic()
    if restante <= 0:
        raise rb.FalhaBitrix(rb.classificar(None, "", _PrazoEstourado("sem tempo na rodada")))
    resp = None
    try:
        resp = _abrir(url, headers or {}, min(TIMEOUT, max(restante, 0.05)),
                      min(TETO_CONEXAO_S, max(restante, 0.05)))
        st = _status_de(resp)
        if not (200 <= st < 300):
            corpo = resp.read(4096) if hasattr(resp, "read") else b""
            raise rb.FalhaBitrix(rb.classificar(st, corpo, retry_after=_cabecalho(resp, "Retry-After"),
                                                download=download))
        ctype = (_cabecalho(resp, "Content-Type") or "").lower()
        if ctype.startswith("application/json"):
            corpo = resp.read(65536)
            if rb.extrair_codigo(corpo)[0]:
                raise rb.FalhaBitrix(rb.classificar(st, corpo, download=download))
            resp = _Reler(corpo, resp)
        return _gravar_stream(resp, destino, esperado, prazo, teto)
    except rb.FalhaBitrix:
        raise
    except (rb.ErroTamanho, rb.ErroTipo):
        raise
    except rb.ErroIntegridade as e:
        # truncado/0 byte costuma ser transitório (queda no meio): repete na fila,
        # com o limite de tentativas da política (vira permanente ao esgotar).
        erro = rb.classificar(None, "", e)
        erro.permanente, erro.repetir = False, True
        raise rb.FalhaBitrix(erro) from e
    except urllib.error.HTTPError as e:
        try:
            corpo = e.read(4096)
        except Exception:  # noqa: BLE001
            corpo = b""
        raise rb.FalhaBitrix(rb.classificar(e.code, corpo, e, download=download)) from e
    except Exception as e:  # noqa: BLE001  rede/DNS/timeout/IncompleteRead
        raise rb.FalhaBitrix(rb.classificar(None, "", e, download=download)) from e
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass


class _Reler:
    """Resposta cujo começo já foi lido (checagem de JSON de erro)."""

    def __init__(self, inicio: bytes, resp):
        self._inicio, self._resp = inicio, resp
        self.headers = getattr(resp, "headers", None)

    def read(self, n=-1):
        if self._inicio:
            b, self._inicio = self._inicio, b""
            return b
        return self._resp.read(n)

    def close(self):
        self._resp.close()


# ---------------------------------------------------------------------------
# Destino, nomes e órfãos
# ---------------------------------------------------------------------------
def nome_limpo(nome: str | None) -> str:
    nome = (nome or "").replace("/", "_").replace("\\", "_").replace("\x00", "").strip()
    nome = nome.lstrip(".") or ""
    return nome[:180] or "arquivo"


def e_midia(nome: str | None = None, mime: str | None = None, tipo: str | None = None) -> bool:
    if (tipo or "").strip().lower() in TIPOS_MIDIA_BITRIX:
        return True
    return tipos_documento.classificar(nome, mime) == tipos_documento.MIDIA


def fid_numerico(file_id) -> int | None:
    s = str(file_id if file_id is not None else "").strip()
    return int(s) if s.isdigit() else None


def arquivo_valido(caminho: str) -> bool:
    """Arquivo final de verdade: não é temporário/parcial e tem > 0 byte."""
    nome = os.path.basename(caminho)
    if nome.startswith(".") or nome.endswith(SUFIXOS_TEMP) or nome.endswith(".meta.json"):
        return False
    try:
        return os.path.isfile(caminho) and os.path.getsize(caminho) > 0
    except OSError:
        return False


def limpar_orfaos(pasta: str, idade_min_s: float | None = None, agora: float | None = None) -> list:
    """Remove `.parcial/.part/.tmp` (inclusive os nossos `.x.part`) e arquivos de 0 byte
    mais velhos que `idade_min_s` (um download em andamento é mais novo que isso).
    Devolve os removidos. Nunca toca arquivo final com conteúdo."""
    idade_min_s = IDADE_ORFAO_S if idade_min_s is None else idade_min_s
    agora = time.time() if agora is None else agora
    removidos = []
    try:
        nomes = os.listdir(pasta)
    except OSError:
        return removidos
    for n in nomes:
        cam = os.path.join(pasta, n)
        try:
            st = os.lstat(cam)
        except OSError:
            continue
        if not os.path.isfile(cam) or os.path.islink(cam):
            continue
        temporario = n.endswith(SUFIXOS_TEMP)
        vazio = st.st_size == 0 and not n.endswith(".meta.json")
        if (temporario or vazio) and agora - st.st_mtime >= idade_min_s:
            try:
                os.unlink(cam)
                removidos.append(cam)
            except OSError:
                pass
    if removidos:
        log.info("órfãos removidos em %s: %d", pasta, len(removidos))
    return removidos


def _destino_alternativo(destino: str, file_id) -> str:
    raiz, ext = os.path.splitext(destino)
    return f"{raiz}__fid{file_id}{ext}"


def _resolver_destino(item: dict, nome_api: str | None) -> str:
    destino = item.get("destino")
    if not destino:
        pasta = item.get("pasta") or caminhos.em_base("lotes", str(item.get("lote_id") or "sem-lote"))
        nome = nome_limpo(nome_api or item.get("nome") or f"file_{item['file_id']}")
        indice = item.get("indice")
        destino = os.path.join(pasta, f"{int(indice):02d}_{nome}" if isinstance(indice, int) else nome)
    # E3: um arquivo com conteúdo no destino que NÃO é deste item (outro anexo com o mesmo
    # nome) nunca é sobrescrito nem "reaproveitado": este vai para um nome próprio.
    if os.path.exists(destino) and arquivo_valido(destino):
        destino = _destino_alternativo(destino, item["file_id"])
    return destino


# ---------------------------------------------------------------------------
# Fontes
# ---------------------------------------------------------------------------
def fonte_bitrix(item: dict, prazo: float, teto: int = None) -> dict:
    """disk.file.get (circuito bitrix-download) -> DOWNLOAD_URL nova -> streaming.

    Levanta CircuitOpenError (disjuntor aberto), rb.FalhaBitrix(erro), _Midia, ErroTamanho."""
    import bitrix_cliente
    teto = TETO_BYTES if teto is None else teto
    fid = fid_numerico(item["file_id"])
    if fid is None:
        raise rb.ErroTipo("file_id não numérico (Telegram legado) — disk.file.get não se aplica")
    circ = rb.circuito("bitrix-download")
    _respeitar_disjuntor_legado(circ)
    ultimo = None
    for _ in range(1 + MAX_URL_NOVA):
        restante = prazo - _monotonic()
        if restante <= 0:
            raise rb.FalhaBitrix(rb.classificar(None, "", _PrazoEstourado("sem tempo na rodada")))
        try:
            r = bitrix_cliente.chamar("disk.file.get", {"id": fid}, circuito="bitrix-download",
                                      timeout=min(TIMEOUT, max(restante, 0.5)), tentativas=1,
                                      _transporte=_transporte_bitrix)
        except rb.FalhaBitrix as e:
            if e.erro.classe == rb.Classe.RATE_LIMIT:
                # QUERY_LIMIT: insistir mantém o bloqueio vivo — pausa a fonte para TODOS
                # os consumidores (ciclo, baixar_pendentes) e não só para esta rodada.
                circ.abrir("RATE_LIMIT", duracao_s=max(e.erro.espera_s, PAUSA_RATE_BITRIX_S),
                           erro=e.erro.detalhe)
            elif not (e.erro.conta_no_circuito or e.erro.abre_circuito_ja):
                circ.liberar_sonda()
            raise
        info = r.get("result") or {}
        nome = info.get("NAME") or ""
        if nome and e_midia(nome):
            raise _Midia(nome)
        try:
            esperado = int(info.get("SIZE") or 0) or None
        except (TypeError, ValueError):
            esperado = None
        if esperado and esperado > teto:
            raise rb.ErroTamanho(f"SIZE {esperado} B passa do teto de {teto} B")
        url = info.get("DOWNLOAD_URL") or ""
        if not url:
            raise rb.FalhaBitrix(rb.classificar(200, '{"error":"SEM_DOWNLOAD_URL"}'))
        destino = _resolver_destino(item, nome)
        try:
            _limitador_bitrix().aguardar(timeout=max(0.0, min(60.0, prazo - _monotonic())))
            res = _baixar_url(url, destino, headers=None, esperado=esperado, prazo=prazo,
                              teto=teto, download=True)
        except rb.FalhaBitrix as e:
            ultimo = e
            if e.erro.classe == rb.Classe.URL_EXPIRADA:
                continue                                   # pede DOWNLOAD_URL NOVA (D3)
            rb.aplicar_no_circuito(circ, e.erro)
            raise
        res.update(destino=destino, nome=nome or None, fonte="bitrix")
        return res
    raise ultimo


def fonte_vibecode(item: dict, chave: str, prazo: float, teto: int = None) -> dict:
    """GET VibeCode /v1/files/{id}/download (circuito vibecode-download + limitador)."""
    teto = TETO_BYTES if teto is None else teto
    fid = fid_numerico(item["file_id"])
    if fid is None:
        raise rb.ErroTipo("file_id não numérico (Telegram legado)")
    circ = rb.circuito("vibecode-download")
    if not circ.permitido():
        raise CircuitOpenError("circuito vibecode-download aberto")
    destino = _resolver_destino(item, None)
    try:
        _limitador_vibecode().aguardar(timeout=max(0.0, min(60.0, prazo - _monotonic())))
        res = _baixar_url(VIBECODE_URL.format(id=fid), destino, headers={"X-Api-Key": chave},
                          esperado=None, prazo=prazo, teto=teto, download=False)
    except rb.FalhaBitrix as e:
        rb.aplicar_no_circuito(circ, e.erro)
        raise
    except BaseException:
        circ.liberar_sonda()
        raise
    circ.registrar_sucesso()
    res.update(destino=destino, nome=None, fonte="vibecode")
    return res


# ---------------------------------------------------------------------------
# Fila persistente
# ---------------------------------------------------------------------------
def _agora() -> float:
    return time.time()


def _normalizar_item(it: dict) -> dict | None:
    if not isinstance(it, dict) or it.get("file_id") in (None, ""):
        return None
    it = dict(it)
    it["file_id"] = str(it["file_id"]).strip()
    est = it.get("estado") or PENDENTE
    if est == "falha":
        # D2: a 'falha' antiga era 5 tentativas em minutos (portal bloqueado ~1 h já
        # bastava). Volta a pendente e a classificação nova decide.
        est = PENDENTE
        it["tentativas"] = 0
        it["proxima_em"] = 0
        it["migrado_de"] = "falha"
    if est not in _PRIORIDADE:
        est = PENDENTE
    it["estado"] = est
    it.setdefault("tentativas", 0)
    if "proxima_em" not in it:
        it["proxima_em"] = float(it.get("proximo_retry") or 0)
    it["proximo_retry"] = it["proxima_em"]          # compat com leitores antigos
    it.setdefault("ultimo_erro", None)
    it.setdefault("erro", None)
    return it


def _melhor(a: dict, b: dict) -> dict:
    pa, pb = _PRIORIDADE.get(a["estado"], 0), _PRIORIDADE.get(b["estado"], 0)
    vence, outro = (a, b) if (pa, a.get("tentativas", 0)) >= (pb, b.get("tentativas", 0)) else (b, a)
    saida = dict(vence)
    for k, v in outro.items():
        if saida.get(k) in (None, "") and v not in (None, ""):
            saida[k] = v
    return saida


def _normalizar(dados, agora: float | None = None) -> dict:
    """Formato canônico {"itens": [...], "fontes": {...}}; dedupe por file_id; poda
    de `baixado` antigo. Aceita o formato legado (lista pura)."""
    agora = _agora() if agora is None else agora
    if isinstance(dados, list):
        dados = {"itens": dados}
    if not isinstance(dados, dict):
        dados = {}
    por_fid: dict = {}
    ordem = []
    for bruto in dados.get("itens") or []:
        it = _normalizar_item(bruto)
        if it is None:
            continue
        fid = it["file_id"]
        if fid in por_fid:
            por_fid[fid] = _melhor(por_fid[fid], it)
        else:
            por_fid[fid] = it
            ordem.append(fid)
    limite_poda = agora - DIAS_PODA_BAIXADO * 86400
    itens = [por_fid[f] for f in ordem
             if not (por_fid[f]["estado"] == BAIXADO and DIAS_PODA_BAIXADO > 0
                     and not por_fid[f].get("estado_pendente")   # desfecho ainda fora do estado
                     and float(por_fid[f].get("atualizado_em") or agora) < limite_poda)]
    fontes = dados.get("fontes") if isinstance(dados.get("fontes"), dict) else {}
    saida = dict(dados)
    saida["itens"] = itens
    saida["fontes"] = fontes
    return saida


def _ler_fila(caminho: str) -> dict:
    """Lê sem trava (troca atômica). Ausente -> vazio; lista legada aceita;
    JSON inválido -> estado_store.EstadoCorrompido (NUNCA vira fila vazia)."""
    try:
        with open(caminho, "rb") as fh:
            bruto = fh.read()
    except FileNotFoundError:
        return _normalizar({})
    try:
        dados = json.loads(bruto.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        dados = None
    if isinstance(dados, (dict, list)):
        return _normalizar(dados)
    return _normalizar(estado_store.ler_estado(caminho))   # levanta EstadoCorrompido + cópia


class Fila:
    """Fila de downloads persistida como JSON; toda mutação = flock + relê + grava atômico."""

    def __init__(self, caminho: str | None = None):
        self.caminho = caminho or DEFAULT_QUEUE_FILE
        self.ultima_rodada: dict = {}
        self._migrar_legado()
        self.recarregar()

    def _migrar_legado(self) -> None:
        try:
            with open(self.caminho, "rb") as fh:
                dados = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return
        if isinstance(dados, list):                      # raiz lista: atualizar_estado recusaria
            with estado_store.trava_arquivo(self.caminho):
                estado_store.gravar_atomico(_normalizar(dados), self.caminho)

    def recarregar(self) -> None:
        dados = _ler_fila(self.caminho)
        self.itens, self.fontes = dados["itens"], dados["fontes"]

    def _mutar(self, fn):
        caixa = {}

        def mutador(dados):
            novo = _normalizar(dados)
            caixa["r"] = fn(novo)
            return novo

        final = estado_store.atualizar_estado(mutador, caminho=self.caminho)
        self.itens, self.fontes = final["itens"], final["fontes"]
        return caixa.get("r")

    def _achar(self, dados: dict, file_id) -> dict | None:
        fid = str(file_id).strip()
        for it in dados["itens"]:
            if it["file_id"] == fid:
                return it
        return None

    def enfileirar(self, file_id, destino: str | None = None, *, lote_id=None, indice=None,
                   nome=None, pasta=None, estado: str = PENDENTE, motivo: str | None = None) -> dict:
        """Dedupe por file_id: item existente é devolvido (permanente nunca reentra;
        `baixado` cujo arquivo sumiu volta a pendente). `estado` permite registrar
        direto um desfecho conhecido (telegram_legado / ignorado_midia)."""
        pedido = dict(file_id=file_id, destino=destino, lote_id=lote_id, indice=indice, nome=nome,
                      pasta=pasta, estado=estado, motivo=motivo)
        return self._mutar(lambda dados: self._enfileirar_em(dados, _agora(), **pedido))

    def enfileirar_varios(self, pedidos: list) -> list:
        """Vários `enfileirar` numa ÚNICA gravação (cada pedido = kwargs de enfileirar).
        Devolve [(item, novo: bool)]."""
        def fn(dados):
            agora, saida = _agora(), []
            for p in pedidos:
                antes = self._achar(dados, p["file_id"]) is not None
                saida.append((self._enfileirar_em(dados, agora, **p), not antes))
            return saida
        return self._mutar(fn) if pedidos else []

    def _enfileirar_em(self, dados, agora, file_id, destino=None, lote_id=None, indice=None,
                       nome=None, pasta=None, estado=PENDENTE, motivo=None) -> dict:
        if True:
            atual = self._achar(dados, file_id)
            if atual is not None:
                for k, v in (("lote_id", lote_id), ("indice", indice), ("nome", nome), ("pasta", pasta)):
                    if atual.get(k) in (None, "") and v not in (None, ""):
                        atual[k] = v
                if (atual["estado"] == BAIXADO and atual.get("destino")
                        and not arquivo_valido(atual["destino"])):
                    atual.update(estado=PENDENTE, proxima_em=0, proximo_retry=0, atualizado_em=agora,
                                 nota="arquivo baixado sumiu do disco; reenfileirado")
                if estado in ESTADOS_PERMANENTES and atual["estado"] == PENDENTE:
                    atual.update(estado=estado, erro=motivo, atualizado_em=agora,
                                 ultimo_erro={"classe": estado, "detalhe": motivo})
                return dict(atual)
            d = destino
            if d:
                outros = {it.get("destino") for it in dados["itens"] if it.get("destino")}
                if d in outros:                          # mesmo nome, outro file_id (E3)
                    d = _destino_alternativo(d, str(file_id).strip())
            item = {"file_id": str(file_id).strip(), "destino": d, "lote_id": lote_id,
                    "indice": indice, "nome": nome, "pasta": pasta, "estado": estado,
                    "tentativas": 0, "proxima_em": 0, "proximo_retry": 0,
                    "ultimo_erro": ({"classe": estado, "detalhe": motivo}
                                    if estado in ESTADOS_PERMANENTES else None),
                    "erro": motivo if estado in ESTADOS_PERMANENTES else None,
                    "criado_em": agora, "atualizado_em": agora}
            dados["itens"].append(item)
            return dict(item)

    def atualizar_item(self, file_id, campos: dict) -> dict | None:
        def fn(dados):
            it = self._achar(dados, file_id)
            if it is None:
                return None
            if it["estado"] in ESTADOS_PERMANENTES and campos.get("estado") == PENDENTE:
                return dict(it)                         # permanente nunca reentra
            it.update(campos)
            it["proximo_retry"] = it.get("proxima_em", 0)
            it["atualizado_em"] = _agora()
            return dict(it)
        return self._mutar(fn)

    def salvar(self) -> None:
        """Compat: grava os itens em memória mesclando por file_id com o disco."""
        meus = {str(i["file_id"]): i for i in self.itens if isinstance(i, dict) and i.get("file_id")}
        fontes = dict(self.fontes)

        def fn(dados):
            vistos = set()
            for i, it in enumerate(dados["itens"]):
                if it["file_id"] in meus:
                    dados["itens"][i] = _melhor(it, _normalizar_item(meus[it["file_id"]]))
                    vistos.add(it["file_id"])
            for fid, it in meus.items():
                if fid not in vistos:
                    dados["itens"].append(_normalizar_item(it))
            dados["fontes"].update(fontes)
        self._mutar(fn)

    def pendentes(self, agora: float) -> list:
        return [i for i in self.itens
                if i.get("estado") == PENDENTE and float(i.get("proxima_em") or 0) <= agora]

    def tamanho(self) -> int:
        return len(self.itens)

    def registrar_falha_fonte(self, fonte: str, erro, quando: float) -> None:
        detalhe = getattr(getattr(erro, "erro", None), "detalhe", None) or rb.mascarar(str(erro))[:300]

        def fn(dados):
            dados["fontes"].setdefault(fonte, {})
            dados["fontes"][fonte].update(ultima_falha=quando, erro=detalhe)
        self._mutar(fn)

    def resumo(self) -> dict:
        cont = {}
        for i in self.itens:
            cont[i.get("estado")] = cont.get(i.get("estado"), 0) + 1
        return {"tamanho": len(self.itens), "pendentes": cont.get(PENDENTE, 0),
                "falhos": cont.get(FALHA_PERMANENTE, 0), "baixados": cont.get(BAIXADO, 0),
                "ignorado_midia": cont.get(IGNORADO_MIDIA, 0),
                "telegram_legado": cont.get(TELEGRAM_LEGADO, 0), "fontes": self.fontes}


# ---------------------------------------------------------------------------
# Processamento
# ---------------------------------------------------------------------------
def _erro_dict(erro: rb.Erro) -> dict:
    return {"classe": erro.classe.value, "detalhe": erro.detalhe}


def _classificar_exc(e: BaseException) -> rb.Erro:
    if isinstance(e, rb.FalhaBitrix):
        return e.erro
    return rb.classificar(None, "", e)


def baixar_item(item: dict, chave_vibecode: str | None, prazo: float) -> dict:
    """Tenta Bitrix e, se não der, VibeCode. Não mexe na fila.

    Devolve {"resultado": "baixado"|"midia"|"telegram"|"falha"|"indisponivel", ...}:
      falha: "erros" [(fonte, Erro)], "pulou_fonte" (circuito aberto em alguma fonte),
             "pausa" (Erro com pausa_rodada ou None)."""
    if fid_numerico(item.get("file_id")) is None:
        return {"resultado": "telegram"}
    if e_midia(item.get("nome")) or e_midia(item.get("destino")):
        return {"resultado": "midia", "nome": item.get("nome") or item.get("destino")}
    erros, pulou = [], []
    try:
        return dict(fonte_bitrix(item, prazo), resultado="baixado")
    except _Midia as m:
        return {"resultado": "midia", "nome": m.nome}
    except CircuitOpenError:
        pulou.append("bitrix")
    except rb.TempoEsgotado:
        pulou.append("bitrix")
    except (rb.FalhaBitrix, rb.FalhaDownloadLocal, OSError) as e:
        erro = _classificar_exc(e)
        erros.append(("bitrix", erro))
        if erro.pausa_rodada:
            # QUERY_LIMIT/LOOP/credencial: para a rodada inteira, sem martelar mais nada.
            return {"resultado": "falha", "erros": erros, "pulou_fonte": pulou, "pausa": erro}
    except Exception as e:  # noqa: BLE001 (ex.: SegredoAusente do webhook)
        log.warning("fonte bitrix indisponível: %s", rb.mascarar(f"{type(e).__name__}: {e}")[:200])
    if chave_vibecode:
        try:
            return dict(fonte_vibecode(item, chave_vibecode, prazo), resultado="baixado", erros=erros)
        except (CircuitOpenError, rb.TempoEsgotado):
            pulou.append("vibecode")
        except (rb.FalhaBitrix, rb.FalhaDownloadLocal, OSError) as e:
            erros.append(("vibecode", _classificar_exc(e)))
    if not erros:
        return {"resultado": "indisponivel", "pulou_fonte": pulou}
    pausa = next((e for _, e in erros if e.pausa_rodada), None)
    return {"resultado": "falha", "erros": erros, "pulou_fonte": pulou, "pausa": pausa}


def _meta_estado(item: dict, campos: dict) -> dict:
    meta = {"file_id": item["file_id"]}
    if isinstance(item.get("indice"), int):
        meta["indice"] = item["indice"]
    meta.update(campos)
    return meta


def processar_item(fila: Fila, item: dict, webhook=None, api_key=None, *args,  # noqa: ARG001
                   prazo: float | None = None, agora_fn=time.time, rodada: dict | None = None,
                   max_tentativas: int | None = None, **kw) -> dict:  # noqa: ARG001
    """Uma tentativa para o item; grava o desfecho na fila. Devolve o item atualizado.
    `rodada` (dict) recebe "parar" (motivo) e "atencao" (lista) quando aplicável."""
    rodada = rodada if rodada is not None else {}
    agora = agora_fn()
    prazo = prazo if prazo is not None else _monotonic() + TETO_ITEM_S
    r = baixar_item(item, api_key, prazo)
    res = r["resultado"]
    fid = item["file_id"]
    for f, e in r.get("erros") or []:                 # healthcheck por fonte (--status)
        try:
            fila.registrar_falha_fonte(f, e.detalhe, agora)
        except Exception:  # noqa: BLE001
            pass
    if res == "baixado":
        campos = {"estado": BAIXADO, "fonte": r["fonte"], "erro": None, "ultimo_erro": None,
                  "proxima_em": 0, "destino": r["destino"], "sha256": r["sha256"],
                  "tamanho": r["tamanho"], "baixado_em": agora,
                  "tentativas": int(item.get("tentativas") or 0) + 1}
        if r.get("nome"):
            campos["nome"] = r["nome"]
        rodada.setdefault("estado_json", []).append(_meta_estado(item, {
            "baixado": True, "sha256": r["sha256"], "caminho": r["destino"],
            "nome_salvo": os.path.basename(r["destino"]), "tamanho_bits": r["tamanho"],
            "fonte": f"download_svc/{r['fonte']}", "erro": None, "erro_tipo": None,
            "obtido_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "proximo_retry": None,
            "tentativas": campos["tentativas"]}))
        log.info("[OK] file %s -> %s (%s B, %s)", fid, os.path.basename(r["destino"]), r["tamanho"], r["fonte"])
    elif res in ("midia", "telegram"):
        est = IGNORADO_MIDIA if res == "midia" else TELEGRAM_LEGADO
        motivo = ("áudio/vídeo não é documento (tipos_documento): não baixado"
                  if res == "midia" else "id não numérico (Telegram legado): disk.file.get não se aplica")
        campos = {"estado": est, "erro": motivo, "ultimo_erro": {"classe": est, "detalhe": motivo},
                  "proxima_em": 0}
        if res == "midia" and r.get("nome") and not item.get("nome"):
            campos["nome"] = r["nome"]
        marca = {"erro": motivo, "erro_tipo": "midia" if res == "midia" else "telegram_legado"}
        if res == "midia":
            # G-02: a ponte decide por `tipo`/nome. Com a leitura pelo webhook o lote não
            # tem nenhum dos dois; sem isso o kit com áudio esperava download para sempre.
            # (`tipo`/`nome_original` são da triagem: o merge só preenche se estiverem vazios.)
            marca["tipo"] = tipos_documento.MIDIA
            nome_midia = r.get("nome") or item.get("nome")
            if nome_midia:
                marca["nome_original"] = os.path.basename(str(nome_midia))
        rodada.setdefault("estado_json", []).append(_meta_estado(item, marca))
        if res == "telegram":
            rodada.setdefault("atencao", []).append(f"{fid}: {motivo}")
        log.info("[%s] file %s: %s", est, fid, motivo)
    elif res == "indisponivel":
        # nenhuma fonte tentada (disjuntores abertos / sem chave): não conta tentativa.
        campos = {"proxima_em": agora + 600.0, "erro": "fontes indisponíveis (disjuntor aberto)",
                  "ultimo_erro": {"classe": "INDISPONIVEL", "detalhe": ",".join(r["pulou_fonte"])}}
        rodada["parar"] = "fontes indisponíveis (disjuntor aberto / sem credencial)"
    else:
        erros = r["erros"]
        tent = int(item.get("tentativas") or 0) + 1
        transitorios = [e for _, e in erros if not e.permanente]
        principal = transitorios[0] if transitorios else erros[-1][1]
        detalhe = "; ".join(f"{f}: {e.detalhe}" for f, e in erros)[:600]
        permanente = not transitorios and not r["pulou_fonte"]
        prox = None if permanente else rb.proxima_tentativa(tent, principal, agora, fila=True)
        if not permanente and r["pulou_fonte"] and prox is None:
            prox = agora + 600.0                        # fonte pulada ainda pode ter o arquivo
        if max_tentativas and tent >= max_tentativas:
            prox = None
        if prox is None:
            campos = {"estado": FALHA_PERMANENTE, "tentativas": tent, "erro": detalhe,
                      "ultimo_erro": _erro_dict(principal), "proxima_em": 0}
            rodada.setdefault("atencao", []).append(f"{fid}: {principal.classe.value}")
            rodada.setdefault("estado_json", []).append(_meta_estado(item, {
                "erro": detalhe, "erro_tipo": "permanente", "tentativas": tent, "proximo_retry": None}))
            log.error("[PERMANENTE] file %s: %s", fid, detalhe)
        else:
            campos = {"estado": PENDENTE, "tentativas": tent, "erro": detalhe,
                      "ultimo_erro": _erro_dict(principal), "proxima_em": prox}
            log.warning("[ERRO] file %s (tent %d, nova em %.0f min): %s", fid, tent,
                        (prox - agora) / 60, detalhe)
        if r.get("pausa") is not None:
            rodada["parar"] = f"pausa_rodada: {r['pausa'].classe.value}"
            if r["pausa"].classe == rb.Classe.ACESSO:
                rodada.setdefault("atencao", []).append(f"credencial/acesso: {r['pausa'].detalhe}")
    novo = fila.atualizar_item(fid, campos) or dict(item, **campos)
    item.clear()
    item.update(novo)
    return item


def _indice_estado(estado: dict) -> dict:
    """file_id -> (lote_id, indice, meta) a partir do estado.json (só leitura)."""
    mapa = {}
    for lid, lote in (estado.get("lotes") or {}).items():
        if not isinstance(lote, dict):
            continue
        metas = {str(m.get("file_id")): m for m in (lote.get("arquivos") or [])
                 if isinstance(m, dict) and m.get("file_id") is not None}
        for i, fid in enumerate(lote.get("file_ids") or [], start=1):
            m = metas.get(str(fid)) or {}
            mapa[str(fid)] = (lid, m.get("indice") if isinstance(m.get("indice"), int) else i, m)
    return mapa


def _lotes_por_fid(estado: dict) -> dict:
    """file_id -> [lote_id, ...] de TODOS os lotes cujo `file_ids` contém o fid."""
    mapa: dict = {}
    for lid, lote in (estado.get("lotes") or {}).items():
        if not isinstance(lote, dict):
            continue
        for fid in lote.get("file_ids") or []:
            mapa.setdefault(str(fid), []).append(lid)
    return mapa


def _lote_destino(fid: str, lid_item, estado: dict, por_fid: dict):
    """Lote do estado que deve receber o meta do fid (achado A3 da revisão final).

    1º o `lote_id` do item, se existe e contém o fid; senão um lote que contém o fid,
    preferindo o que não foi dividido (o sub-lote) ao pai DIVIDIDO. None = nenhum."""
    lotes = estado.get("lotes") or {}
    donos = por_fid.get(str(fid)) or []
    if lid_item and lid_item in donos and isinstance(lotes.get(lid_item), dict):
        return lid_item
    if not donos:
        return None
    import estado_lote
    vivos = [lid for lid in donos if not estado_lote._dividido(lotes.get(lid) or {})]
    return sorted(vivos or donos)[0]


def _marcar_estado_pendente(fila, patches: list, motivo: str) -> None:
    """Desfecho que não chegou ao estado.json fica guardado no item (recuperável)."""
    for p in patches:
        log.error("[ESTADO PENDENTE] file %s: %s — meta guardado na fila para reaplicar",
                  p.get("file_id"), motivo)
        if fila is not None:
            fila.atualizar_item(p["file_id"], {"estado_pendente": True, "estado_meta": dict(p),
                                               "estado_pendente_motivo": motivo})


def _aplicar_no_estado(patches: list, itens_por_fid: dict, caminho_estado: str | None,
                       fila=None) -> dict:
    """Agrupa os metas por lote e aplica com dono 'download' (merge por campo).

    O `lote_id` do item só é usado se o lote existe e contém o fid; senão o meta vai
    ao lote que de fato contém o fid. Sem nenhum lote (ou se o lote some entre a
    leitura e a gravação), o desfecho fica no item da fila (`estado_pendente`) e a
    próxima rodada reaplica — nunca é descartado em silêncio."""
    if not patches or not os.path.exists(caminho_estado or estado_store.ARQ_ESTADO):
        return {}                                      # nunca cria um estado.json vazio
    try:
        estado = estado_store.ler_estado(caminho_estado)
    except estado_store.EstadoCorrompido:
        log.error("estado.json corrompido: desfechos ficam só na fila")
        _marcar_estado_pendente(fila, patches, "estado.json corrompido")
        return {}
    por_fid = _lotes_por_fid(estado)
    por_lote: dict = {}
    sem_lote = []
    for p in patches:
        it = itens_por_fid.get(p["file_id"]) or {}
        lid = _lote_destino(p["file_id"], it.get("lote_id"), estado, por_fid)
        if not lid:
            sem_lote.append(p)
            continue
        if lid != it.get("lote_id"):
            if it.get("lote_id"):
                log.warning("file %s: lote %s do item não tem o fid; aplicado em %s",
                            p["file_id"], it.get("lote_id"), lid)
            idx = _indice_estado({"lotes": {lid: (estado.get("lotes") or {}).get(lid)}})
            if p["file_id"] in idx:
                if it.get("lote_id"):                  # índice do item era do lote errado
                    p["indice"] = idx[p["file_id"]][1]
                else:
                    p.setdefault("indice", idx[p["file_id"]][1])
        por_lote.setdefault(lid, {"arquivos": []})["arquivos"].append(p)
    conflitos: dict = {}
    if sem_lote:
        _marcar_estado_pendente(fila, sem_lote, "nenhum lote do estado contém o file_id")
        conflitos["<sem lote>"] = sorted(p["file_id"] for p in sem_lote)
    if not por_lote:
        return conflitos
    try:
        res = estado_store.atualizar_lotes(por_lote, dono="download", caminho=caminho_estado)
    except estado_store.EstadoCorrompido:
        log.error("estado.json corrompido: desfechos ficam só na fila")
        _marcar_estado_pendente(fila, [p for v in por_lote.values() for p in v["arquivos"]],
                                "estado.json corrompido")
        return conflitos
    for lid, negados in (res or {}).items():
        if "<lote inexistente>" in negados:            # sumiu entre a leitura e a gravação
            _marcar_estado_pendente(fila, por_lote[lid]["arquivos"], f"lote {lid} inexistente")
    if fila is not None:
        for lid, v in por_lote.items():
            if "<lote inexistente>" in ((res or {}).get(lid) or []):
                continue
            for p in v["arquivos"]:
                if (itens_por_fid.get(p["file_id"]) or {}).get("estado_pendente"):
                    fila.atualizar_item(p["file_id"], {"estado_pendente": False, "estado_meta": None,
                                                       "estado_pendente_motivo": None})
    conflitos.update(res or {})
    return conflitos


def _pendentes_de_estado(fila) -> list:
    """Itens cujo desfecho ainda não chegou ao estado.json (rodada anterior)."""
    return [it for it in fila.itens
            if isinstance(it, dict) and it.get("estado_pendente") and isinstance(it.get("estado_meta"), dict)]


def _descartar_ja_baixados(fila: Fila, devidos: list, caminho_estado: str | None) -> list:
    """Item que outro processo (o ciclo) já baixou — meta com baixado+sha256 e arquivo
    válido em disco — é fechado sem rede (evita baixar de novo e duplicar o arquivo)."""
    caminho = caminho_estado or estado_store.ARQ_ESTADO
    if not os.path.exists(caminho):
        return devidos
    try:
        mapa = _indice_estado(estado_store.ler_estado(caminho))
    except estado_store.EstadoCorrompido:
        return devidos
    restantes = []
    for it in devidos:
        meta = (mapa.get(it["file_id"]) or (None, None, {}))[2]
        cam = meta.get("caminho") or ""
        if meta.get("baixado") and meta.get("sha256") and cam and arquivo_valido(cam):
            fila.atualizar_item(it["file_id"], {"estado": BAIXADO, "fonte": "ja_no_estado", "destino": cam,
                                                "sha256": meta.get("sha256"), "erro": None,
                                                "ultimo_erro": None, "proxima_em": 0})
        else:
            restantes.append(it)
    return restantes


def processar_fila(fila: Fila, webhook=None, api_key=None, max_tentativas: int | None = None,
                   base_delay=None, agora_fn=time.time, *, limite_itens: int | None = None,  # noqa: ARG001
                   teto_rodada_s: float | None = None, so_file_ids=None,
                   atualizar_estado_json: bool = True, caminho_estado: str | None = None) -> list:
    """Processa os itens devidos (proxima_em <= agora), no máximo `limite_itens` e
    dentro de `teto_rodada_s`. Para a rodada em `pausa_rodada` ou fontes indisponíveis.
    `webhook` é ignorado (o bitrix_cliente lê o segredo); `api_key` None => lê
    `vibecode_key` (opcional). Resumo em `fila.ultima_rodada`."""
    limite = LIMITE_RODADA if limite_itens is None else max(0, int(limite_itens))
    teto = TETO_RODADA_S if teto_rodada_s is None else float(teto_rodada_s)
    if api_key is None:
        try:
            api_key = ler_chave_vibecode(obrigatorio=False)
        except Exception as e:  # noqa: BLE001  (SegredoInseguro etc.)
            log.warning("chave VibeCode indisponível: %s", rb.mascarar(str(e))[:200])
            api_key = None
    fila.recarregar()
    devidos = fila.pendentes(agora_fn())
    if so_file_ids is not None:
        so = {str(f) for f in so_file_ids}
        devidos = [i for i in devidos if i["file_id"] in so]
    fim_rodada = _monotonic() + teto
    rodada: dict = {"parar": None, "atencao": [], "estado_json": []}
    processados = []
    if devidos and atualizar_estado_json:
        devidos = _descartar_ja_baixados(fila, devidos, caminho_estado)
    for item in devidos:
        if len(processados) >= limite:
            rodada["parar"] = f"limite de {limite} itens"
            break
        restante = fim_rodada - _monotonic()
        if restante < 1.0:
            rodada["parar"] = f"teto de tempo da rodada ({teto:.0f}s)"
            break
        processar_item(fila, item, None, api_key, prazo=_monotonic() + min(TETO_ITEM_S, restante),
                       agora_fn=agora_fn, rodada=rodada, max_tentativas=max_tentativas)
        processados.append(item)
        if rodada.get("parar"):
            break
    conflitos = {}
    if atualizar_estado_json:
        itens = {i["file_id"]: i for i in processados}
        metas = list(rodada["estado_json"])
        feitos = {m.get("file_id") for m in metas}
        for it in _pendentes_de_estado(fila):          # reaplica o que ficou pendente (A3)
            if it["file_id"] not in feitos:
                metas.append(dict(it["estado_meta"]))
                itens.setdefault(it["file_id"], it)
        if metas:
            conflitos = _aplicar_no_estado(metas, itens, caminho_estado, fila=fila)
    fila.ultima_rodada = {"processados": len(processados), "devidos": len(devidos),
                          "parar": rodada["parar"], "atencao": rodada["atencao"],
                          "conflitos_estado": conflitos}
    if rodada["parar"]:
        log.warning("rodada interrompida: %s", rodada["parar"])
    return processados


def codigo_saida(rodada: dict) -> int:
    """0 = ok/transitório; 1 = falha que exige atenção (permanente nova, credencial)."""
    return 1 if (rodada or {}).get("atencao") else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def configurar_log(nivel=logging.INFO) -> None:
    raiz = logging.getLogger()
    if not raiz.handlers:
        logging.basicConfig(level=nivel, format="%(message)s", stream=sys.stdout)
    import seguranca
    seguranca.instalar_filtro_log(raiz)


def emitir_status(fila: Fila):
    r = fila.resumo()
    print(f"tamanho da fila : {r['tamanho']}")
    print(f"pendentes       : {r['pendentes']}")
    print(f"falha permanente: {r['falhos']}")
    print(f"baixados        : {r['baixados']}")
    print(f"mídia ignorada  : {r['ignorado_midia']}")
    print(f"telegram legado : {r['telegram_legado']}")
    for nome in ("bitrix-download", "vibecode-download"):
        try:
            s = rb.circuito(nome).situacao()
            print(f"disjuntor {nome:<18}: {s.get('estado')} motivo={s.get('motivo') or '-'}")
        except Exception as e:  # noqa: BLE001
            print(f"disjuntor {nome:<18}: ? ({type(e).__name__})")
    for nome, f in sorted((r["fontes"] or {}).items()):
        uf = f.get("ultima_falha")
        if uf:
            quando = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(uf))
            print(f"fonte {nome:<9}: última falha {quando} — {rb.mascarar(str(f.get('erro')))[:200]}")


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="download_svc",
                                description="Download Bitrix24 com fallback VibeCode e fila persistente.")
    p.add_argument("--fila", default=DEFAULT_QUEUE_FILE, help="arquivo JSON da fila")
    p.add_argument("--enfileirar", metavar="FILE_ID", help="enfileira um download")
    p.add_argument("--destino", metavar="CAMINHO", help="caminho de destino (com --enfileirar)")
    p.add_argument("--processar", action="store_true", help="processa os itens devidos da fila")
    p.add_argument("--status", action="store_true", help="healthcheck da fila e dos disjuntores")
    p.add_argument("--limite", type=int, default=None, help=f"itens por rodada (padrão {LIMITE_RODADA})")
    p.add_argument("--teto-s", type=float, default=None, help=f"tempo total da rodada (padrão {TETO_RODADA_S:.0f}s)")
    p.add_argument("--max-tentativas", type=int, default=None, help="teto extra de tentativas por item")
    p.add_argument("--base-delay", type=float, default=None, help="(obsoleto: backoff vem de rede_bitrix)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    configurar_log()
    try:
        fila = Fila(args.fila)
    except estado_store.EstadoCorrompido as e:
        log.error("fila corrompida (%s); cópia preservada em %s", e.caminho, e.copia)
        return 2
    if args.enfileirar:
        if not args.destino:
            print("erro: --enfileirar exige --destino", file=sys.stderr)
            return 2
        item = fila.enfileirar(args.enfileirar, args.destino)
        print(f"enfileirado file_id={item['file_id']} -> {item['destino']} "
              f"estado={item['estado']} (fila={fila.tamanho()})")
        return 0
    if args.processar:
        processados = processar_fila(fila, None, None, args.max_tentativas, None,
                                     limite_itens=args.limite, teto_rodada_s=args.teto_s)
        r = fila.ultima_rodada
        print(f"processados {len(processados)} de {r['devidos']} devido(s)"
              + (f" — parou: {r['parar']}" if r["parar"] else ""))
        for msg in r["atencao"]:
            print(f"  ATENÇÃO {rb.mascarar(msg)[:200]}")
        return codigo_saida(r)
    emitir_status(fila)
    return 0


if __name__ == "__main__":
    sys.exit(main())
