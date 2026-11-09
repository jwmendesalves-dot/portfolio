#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rede_bitrix.py — POLÍTICA de rede para Bitrix24 / VibeCode (sem cliente HTTP).

Fonte única (docs/remediacao/CONTRATOS.md) para:
  * classificar(status, corpo, exc) -> Erro    : o que fazer com cada falha;
  * proxima_tentativa(tentativas, erro, agora) : backoff exponencial com jitter;
  * LimitadorHost                              : token bucket ENTRE PROCESSOS
                                                 (arquivo + fcntl.flock);
  * circuito(nome)                             : disjuntores persistidos
                                                 (circuit_breaker.CircuitBreaker).

Nenhuma chamada de rede acontece aqui. O consumidor faz:
    limitador_padrao().aguardar()
    circ = circuito("bitrix-download")
    if not circ.permitido(): ...pula / usa outra fonte...
    try: resposta = <sua chamada HTTP>; circ.registrar_sucesso()
    except Exception as e:
        erro = classificar(status, corpo, e)
        aplicar_no_circuito(circ, erro)
        quando = proxima_tentativa(tentativas, erro)   # None = desistir

Base documental (doc oficial local ~/Documents/bitrix24-docs):
  - settings/performance/limits.md: leaky bucket POR IP (2 req/s, rajada 50 fora
    do Enterprise), 503 QUERY_LIMIT_EXCEEDED ("retry with increasing delay"; não
    rodar séries paralelas do mesmo IP); 429 OPERATION_TIME_LIMIT (esperar
    operating_reset_at da última resposta boa / próximo cesto).
  - error-codes.md: ramificar pelo campo `error`, NUNCA por error_description nem
    só pelo status (erro de batch chega com 200); repetir erro de método dá o
    mesmo resultado; OVERLOAD_LIMIT é bloqueio manual.
  - ERROR_LOOP_DETECTED não está na doc; empírico do escritório: aparece após
    ACCESS_DENIED repetidos e cada nova tentativa renova o bloqueio (~15 min).

Python 3.9, só biblioteca padrão.
"""
from __future__ import annotations

import concurrent.futures
import email.utils
import enum
import errno
import fcntl
import http.client
import json
import logging
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
from dataclasses import dataclass

import circuit_breaker
from circuit_breaker import CircuitBreaker, CircuitOpenError, mascarar_texto

log = logging.getLogger("rede_bitrix")


# ===========================================================================
# Máscara
# ===========================================================================
def mascarar(texto) -> str:
    """circuit_breaker.mascarar_texto = seguranca.mascarar (fonte única, frente S)
    + par chave=valor de token/auth. Sem fallback local: `seguranca` é obrigatório."""
    return mascarar_texto(texto)


# ===========================================================================
# Classificação
# ===========================================================================
class Classe(str, enum.Enum):
    RATE_LIMIT = "RATE_LIMIT"            # 503 QUERY_LIMIT_EXCEEDED
    OPERATING = "OPERATING"              # 429 OPERATION_TIME_LIMIT (e QUERYTIMELIMIT do REST 3)
    REDE = "REDE"                        # timeout / DNS / reset / conexão
    URL_EXPIRADA = "URL_EXPIRADA"        # 401/403/404/410 do host de download (URL assinada)
    ACESSO = "ACESSO"                    # 401/403, ACCESS_DENIED, insufficient_scope, WRONG_AUTH_TYPE...
    LOOP = "LOOP"                        # ERROR_LOOP_DETECTED
    NAO_ENCONTRADO = "NAO_ENCONTRADO"    # 404 / *NOT_FOUND
    TAMANHO = "TAMANHO"                  # arquivo grande demais (413, FILE_TOO_LARGE, >20 MB Telegram)
    TIPO = "TIPO"                        # tipo/fonte inválida (415, id não numérico p/ disk.file.get)
    INTEGRIDADE = "INTEGRIDADE"          # 0 byte, tamanho != SIZE, hash divergente
    OVERLOAD = "OVERLOAD"                # OVERLOAD_LIMIT (bloqueio manual)
    SERVIDOR = "SERVIDOR"                # (acréscimo) 5xx / INTERNAL_SERVER_ERROR: transitório
    INVALIDO = "INVALIDO"                # (acréscimo) erro de método/requisição (400, ERROR_ARGUMENT, batch)
    DESCONHECIDO = "DESCONHECIDO"


PERMANENTES = frozenset({Classe.ACESSO, Classe.NAO_ENCONTRADO, Classe.TAMANHO, Classe.TIPO,
                         Classe.INTEGRIDADE, Classe.OVERLOAD, Classe.INVALIDO})
# Falhas que dizem algo sobre a SAÚDE da fonte (contam no disjuntor). Erros de
# item (arquivo apagado, grande demais, URL vencida) não devem abrir o circuito.
CONTA_NO_CIRCUITO = frozenset({Classe.RATE_LIMIT, Classe.OPERATING, Classe.REDE, Classe.SERVIDOR,
                               Classe.LOOP, Classe.OVERLOAD, Classe.ACESSO, Classe.DESCONHECIDO})
# Abrem o disjuntor JÁ (sem esperar N falhas): insistir renova o bloqueio.
ABRE_CIRCUITO_JA = frozenset({Classe.LOOP, Classe.OVERLOAD, Classe.OPERATING})


@dataclass
class Erro:
    classe: Classe
    repetir: bool          # vale repetir a MESMA requisição (após espera_s)
    espera_s: float        # espera mínima antes da próxima tentativa
    permanente: bool       # nunca repetir (corrigir causa / humano)
    detalhe: str           # já mascarado
    codigo: str = ""       # (acréscimo) campo `error` do Bitrix, normalizado em MAIÚSCULAS
    status: int | None = None
    ambiguo: bool = False  # (acréscimo) a requisição PODE ter executado (timeout/reset)
    pausa_rodada: bool = False  # (acréscimo) parar a rodada inteira (não tentar o próximo item)

    @property
    def conta_no_circuito(self) -> bool:
        return self.classe in CONTA_NO_CIRCUITO

    @property
    def abre_circuito_ja(self) -> bool:
        return self.classe in ABRE_CIRCUITO_JA


class FalhaBitrix(RuntimeError):
    """Exceção que carrega um Erro já classificado (consumidor levanta; o
    disjuntor e classificar() entendem)."""

    def __init__(self, erro: Erro):
        super().__init__(f"{erro.classe.value}: {erro.detalhe}")
        self.erro = erro


class FalhaDownloadLocal(Exception):
    """Base das falhas detectadas pelo downloader (sem HTTP): permanentes."""
    classe = Classe.DESCONHECIDO


class ErroTamanho(FalhaDownloadLocal):
    classe = Classe.TAMANHO


class ErroTipo(FalhaDownloadLocal):
    classe = Classe.TIPO


class ErroIntegridade(FalhaDownloadLocal):
    classe = Classe.INTEGRIDADE


# --- códigos do campo `error` (comparação em MAIÚSCULAS) -------------------
_COD_RATE = {"QUERY_LIMIT_EXCEEDED"}
_COD_OPERATING = {"OPERATION_TIME_LIMIT", "BITRIX_REST_V3_EXCEPTION_LIMITER_QUERYTIMELIMITEXCEPTION"}
_COD_LOOP = {"ERROR_LOOP_DETECTED", "LOOP_DETECTED"}
_COD_OVERLOAD = {"OVERLOAD_LIMIT"}
_COD_ACESSO = {"ACCESS_DENIED", "INSUFFICIENT_SCOPE", "INVALID_CREDENTIALS", "WRONG_AUTH_TYPE",
               "NO_AUTH_FOUND", "INVALID_TOKEN", "EXPIRED_TOKEN", "USER_ACCESS_ERROR",
               "PORTAL_DELETED", "INVALID_REQUEST", "AUTHORIZATION_ERROR", "INVALID_GRANT",
               "ERROR_METHOD_NOT_FOUND", "METHOD_NOT_FOUND", "UNAUTHORIZED", "FORBIDDEN"}
# ACESSO de sistema/autenticação: credencial ruim vale para TODA chamada -> parar a rodada.
_COD_ACESSO_SISTEMA = {"INSUFFICIENT_SCOPE", "INVALID_CREDENTIALS", "WRONG_AUTH_TYPE", "NO_AUTH_FOUND",
                       "INVALID_TOKEN", "EXPIRED_TOKEN", "PORTAL_DELETED", "INVALID_REQUEST",
                       "USER_ACCESS_ERROR", "UNAUTHORIZED"}
_COD_SERVIDOR = {"INTERNAL_SERVER_ERROR", "ERROR_UNEXPECTED_ANSWER", "SERVICE_UNAVAILABLE", "BAD_GATEWAY"}
_COD_TAMANHO = {"FILE_TOO_LARGE", "ATTACH_OVERSIZE", "REQUEST_ENTITY_TOO_LARGE"}
# ordem importa na varredura de texto bruto (corpo truncado): LOOP antes de ACCESS_DENIED,
# porque a mensagem do loop cita "ACCESS_DENIED".
_VARREDURA = (("ERROR_LOOP_DETECTED",) + tuple(sorted(_COD_OVERLOAD)) + ("QUERY_LIMIT_EXCEEDED",)
              + tuple(sorted(_COD_OPERATING)) + ("INSUFFICIENT_SCOPE", "INVALID_CREDENTIALS",
                                                  "WRONG_AUTH_TYPE", "NO_AUTH_FOUND", "ACCESS_DENIED",
                                                  "ERROR_METHOD_NOT_FOUND", "ERROR_NOT_FOUND"))

# --- política de repetição por classe: (base_s, teto_s, max_tentativas) ----
POLITICA = {
    Classe.RATE_LIMIT: (10.0, 300.0, 5),     # leaky bucket drena 2/s; 1-2-4 s cai na mesma janela
    Classe.OPERATING: (60.0, 900.0, 4),      # cestos de 1 min; 1ª em operating_reset_at
    Classe.REDE: (2.0, 120.0, 5),
    Classe.SERVIDOR: (5.0, 300.0, 4),
    Classe.URL_EXPIRADA: (1.0, 10.0, 3),     # pedir DOWNLOAD_URL nova a cada tentativa
    Classe.LOOP: (900.0, 3600.0, 3),         # pausa longa; nunca em loop curto
    Classe.DESCONHECIDO: (5.0, 60.0, 2),
}
MAX_TENTATIVAS = {c: POLITICA[c][2] if c in POLITICA else 1 for c in Classe}

def _env_pausa_loop() -> float:
    try:
        v = float(os.environ.get("BITRIX_PAUSA_LOOP_S") or 900)
    except ValueError:
        v = 900.0
    return min(max(v, 60.0), 3600.0)


PAUSA_LOOP_S = _env_pausa_loop()   # doc não dá prazo; empírico ~15 min
ESPERA_OPERATING_PADRAO_S = 60.0
ESPERA_MAX_S = 3600.0
PAUSA_OVERLOAD_S = 3600.0   # disjuntor reabre a sonda de hora em hora (nunca "para sempre")

_EXC_PROGRAMACAO = (TypeError, AttributeError, NameError, KeyError, IndexError,
                    AssertionError, ImportError, SyntaxError, NotImplementedError)
_ERRNO_REDE = {errno.ECONNRESET, errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH,
               errno.ENETUNREACH, errno.EPIPE, errno.ECONNABORTED, errno.ENETDOWN,
               getattr(errno, "EHOSTDOWN", errno.EHOSTUNREACH), errno.ENOTCONN}


def _texto(corpo) -> str:
    if corpo is None:
        return ""
    if isinstance(corpo, (bytes, bytearray)):
        return bytes(corpo).decode("utf-8", "replace")
    if isinstance(corpo, (dict, list)):
        try:
            return json.dumps(corpo, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(corpo)
    return str(corpo)


def extrair_codigo(corpo) -> tuple[str, str, float | None]:
    """(codigo_MAIÚSCULO, descricao, operating_reset_at) do corpo de erro.

    Aceita {"error": "X", "error_description": "..."} (Bitrix), {"error": {"code": ..}}
    (VibeCode/REST 3), batch item, JSON truncado (regex) e, em último caso,
    varredura de códigos CONHECIDOS no texto bruto (nunca da descrição livre).
    """
    if isinstance(corpo, dict):
        obj = corpo
        txt = ""
    else:
        txt = _texto(corpo)
        obj = None
        s = txt.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except ValueError:
                obj = None
    codigo, desc, reset = "", "", None
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            codigo = str(err.get("code") or err.get("error") or "")
            desc = str(err.get("message") or err.get("description") or "")
        elif isinstance(err, str):
            codigo = err
            desc = str(obj.get("error_description") or obj.get("message") or "")
        elif isinstance(obj.get("code"), str) and obj.get("ok") is not True:
            codigo = obj["code"]
            desc = str(obj.get("message") or "")
        t = obj.get("time")
        cand = (t.get("operating_reset_at") if isinstance(t, dict) else None) or obj.get("operating_reset_at")
        if isinstance(err, dict) and not cand:
            cand = err.get("operating_reset_at")
        try:
            reset = float(cand) if cand else None
        except (TypeError, ValueError):
            reset = None
        return codigo.strip().upper(), desc, reset
    # JSON truncado / não-JSON
    m = re.search(r'"error"\s*:\s*"([^"]*)"', txt) or re.search(r'"code"\s*:\s*"([A-Za-z0-9_.]+)"', txt)
    if m:
        codigo = m.group(1)
    md = re.search(r'"error_description"\s*:\s*"([^"]*)', txt)
    if md:
        desc = md.group(1)
    mr = re.search(r'"operating_reset_at"\s*:\s*(\d+(?:\.\d+)?)', txt)
    if mr:
        reset = float(mr.group(1))
    if not codigo:
        alto = txt.upper()
        for cod in _VARREDURA:
            if re.search(r"(?<![A-Z_])" + re.escape(cod) + r"(?![A-Z_])", alto):
                codigo = cod
                break
    return codigo.strip().upper(), desc, reset


def operating_reset_at_de(resposta) -> float | None:
    """operating_reset_at do bloco `time` de uma resposta (guarde o da última
    resposta BOA e passe em classificar(..., operating_reset_at=...))."""
    return extrair_codigo(resposta)[2] if resposta is not None else None


def _retry_after(valor, agora: float) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return max(0.0, float(valor))
    except (TypeError, ValueError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(str(valor))
        return max(0.0, dt.timestamp() - agora)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _status_de_exc(exc) -> tuple[int | None, object]:
    """(status, headers) de HTTPError (urllib) ou exceção estilo requests."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code, getattr(exc, "headers", None)
    resp = getattr(exc, "response", None)
    cod = getattr(resp, "status_code", None)
    if isinstance(cod, int):
        return cod, getattr(resp, "headers", None)
    return None, None


def _rede(exc) -> tuple[bool, bool]:
    """(é_erro_de_rede, ambíguo). Ambíguo = a requisição pode ter chegado ao
    servidor (timeout, reset, resposta truncada); não-ambíguo = nem saiu (DNS,
    conexão recusada)."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        motivo = getattr(exc, "reason", None)
        if isinstance(motivo, BaseException):
            return True, _rede(motivo)[1]
        return True, "timed out" in str(motivo).lower()
    if isinstance(exc, (socket.gaierror, socket.herror, ConnectionRefusedError)):
        return True, False
    if isinstance(exc, (socket.timeout, TimeoutError, concurrent.futures.TimeoutError)):
        return True, True
    if isinstance(exc, (ConnectionError, http.client.HTTPException, ssl.SSLError)):
        return True, True
    mod = type(exc).__module__ or ""
    if mod.startswith(("requests", "urllib3")):
        nome = type(exc).__name__.lower()
        return True, not ("connection" in nome and "timeout" not in nome)
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _ERRNO_REDE:
        return True, exc.errno not in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH)
    return False, False


def classificar(status: int | None, corpo: str = "", exc: BaseException | None = None, *,
                retry_after=None, operating_reset_at: float | None = None,
                download: bool = False, escrita: bool = False,
                agora: float | None = None) -> Erro:
    """Classifica uma falha. Precedência: Erro já pronto (FalhaBitrix) > falha local
    do downloader > código `error` do corpo > status HTTP > tipo da exceção.

    Parâmetros extras (opcionais, acréscimo ao contrato):
      retry_after         valor do header Retry-After (s ou data HTTP);
      operating_reset_at  timestamp da última resposta BOA (doc: referência da 1ª repetição);
      download            True se a resposta veio do host de download após URL assinada
                          (401/403/404/410 => URL_EXPIRADA: peça DOWNLOAD_URL nova);
      escrita             True em chamada que altera dados: timeout/reset viram
                          "não repetir" (pode ter executado — doc b24gosdk);
      agora               relógio (testes).
    """
    agora = time.time() if agora is None else agora
    if isinstance(exc, FalhaBitrix):
        return exc.erro
    if isinstance(exc, FalhaDownloadLocal):
        return _mk(exc.classe, status, "", f"{type(exc).__name__}: {exc}")

    headers = None
    if exc is not None and status is None:
        status, headers = _status_de_exc(exc)
    elif exc is not None:
        headers = _status_de_exc(exc)[1]
    if retry_after is None and headers is not None:
        try:
            retry_after = headers.get("Retry-After")
        except Exception:  # noqa: BLE001
            retry_after = None
    ra = _retry_after(retry_after, agora)

    codigo, desc, reset_corpo = extrair_codigo(corpo)
    reset = operating_reset_at or reset_corpo
    base_det = f"status={status} codigo={codigo or '-'}"
    if desc:
        base_det += f" desc={desc[:160]}"
    if exc is not None:
        base_det += f" exc={type(exc).__name__}: {str(exc)[:160]}"

    # 1) código do corpo (a doc manda ramificar por `error`)
    if codigo:
        if codigo in _COD_LOOP:
            return _mk(Classe.LOOP, status, codigo, base_det, espera=ra or PAUSA_LOOP_S, pausa_rodada=True)
        if codigo in _COD_OVERLOAD:
            return _mk(Classe.OVERLOAD, status, codigo, base_det, pausa_rodada=True)
        if codigo in _COD_RATE:
            return _mk(Classe.RATE_LIMIT, status, codigo, base_det,
                       espera=ra if ra is not None else POLITICA[Classe.RATE_LIMIT][0], pausa_rodada=True)
        if codigo in _COD_OPERATING:
            return _mk(Classe.OPERATING, status, codigo, base_det,
                       espera=_espera_operating(ra, reset, agora), pausa_rodada=True)
        if download and status in (401, 403, 404, 410):
            return _mk(Classe.URL_EXPIRADA, status, codigo, base_det)
        if codigo in _COD_ACESSO:
            sistema = codigo in _COD_ACESSO_SISTEMA or status == 401
            return _mk(Classe.ACESSO, status, codigo, base_det, pausa_rodada=sistema)
        if codigo.endswith("NOT_FOUND"):
            return _mk(Classe.NAO_ENCONTRADO, status, codigo, base_det)
        if codigo in _COD_TAMANHO or codigo.endswith("_OVERSIZE"):
            return _mk(Classe.TAMANHO, status, codigo, base_det)
        if codigo in _COD_SERVIDOR:
            return _mk(Classe.SERVIDOR, status, codigo, base_det, espera=ra or POLITICA[Classe.SERVIDOR][0],
                       ambiguo=True, escrita=escrita)
        # código desconhecido: decide pelo status; sem status de transporte = erro de método
        if status is None or 200 <= status < 300 or status == 400:
            return _mk(Classe.INVALIDO, status, codigo, base_det)

    # 2) status HTTP
    if status is not None and not (200 <= status < 300):
        if download and status in (401, 403, 404, 410):
            return _mk(Classe.URL_EXPIRADA, status, codigo, base_det)
        if status == 503:
            return _mk(Classe.RATE_LIMIT, status, codigo, base_det,
                       espera=ra if ra is not None else POLITICA[Classe.RATE_LIMIT][0], pausa_rodada=True)
        if status == 429:
            return _mk(Classe.OPERATING, status, codigo, base_det,
                       espera=_espera_operating(ra, reset, agora), pausa_rodada=True)
        if status in (401, 403):
            return _mk(Classe.ACESSO, status, codigo, base_det, pausa_rodada=(status == 401))
        if status in (404, 410):
            return _mk(Classe.NAO_ENCONTRADO, status, codigo, base_det)
        if status == 408:
            return _rede_erro(status, codigo, base_det, ambiguo=True, escrita=escrita)
        if status == 413:
            return _mk(Classe.TAMANHO, status, codigo, base_det)
        if status == 415:
            return _mk(Classe.TIPO, status, codigo, base_det)
        if 500 <= status < 600:
            return _mk(Classe.SERVIDOR, status, codigo, base_det,
                       espera=ra or POLITICA[Classe.SERVIDOR][0], ambiguo=True, escrita=escrita)
        if 400 <= status < 500:
            return _mk(Classe.INVALIDO, status, codigo, base_det)

    # 3) exceção sem resposta HTTP
    if exc is not None:
        eh_rede, ambiguo = _rede(exc)
        if eh_rede:
            return _rede_erro(status, codigo, base_det, ambiguo=ambiguo, escrita=escrita)
    return _mk(Classe.DESCONHECIDO, status, codigo, base_det)


def _espera_operating(ra: float | None, reset: float | None, agora: float) -> float:
    if ra is not None:
        return min(max(1.0, ra), ESPERA_MAX_S)
    if reset:
        return min(max(1.0, float(reset) - agora), ESPERA_MAX_S)
    return ESPERA_OPERATING_PADRAO_S


def _rede_erro(status, codigo, det, ambiguo: bool, escrita: bool) -> Erro:
    return _mk(Classe.REDE, status, codigo, det, espera=POLITICA[Classe.REDE][0],
               ambiguo=ambiguo, escrita=escrita)


def _mk(classe: Classe, status, codigo, det, espera: float = 0.0, pausa_rodada: bool = False,
        ambiguo: bool = False, escrita: bool = False) -> Erro:
    permanente = classe in PERMANENTES
    repetir = not permanente and classe != Classe.LOOP
    detalhe = f"{classe.value} {det}"
    if escrita and ambiguo and repetir:
        repetir = False      # escrita pode ter executado: não repetir às cegas
        detalhe += " [escrita ambígua: pode ter executado; não repetir automaticamente]"
    if classe == Classe.OVERLOAD:
        espera = PAUSA_OVERLOAD_S
    return Erro(classe=classe, repetir=repetir, espera_s=float(espera or 0.0),
                permanente=permanente, detalhe=mascarar(detalhe)[:500], codigo=codigo or "",
                status=status, ambiguo=ambiguo, pausa_rodada=pausa_rodada)


# ===========================================================================
# Retry
# ===========================================================================
_rnd = random.Random()

# Escala de FILA persistida (download_fila / tentativas entre ciclos de 10 min —
# achados D1/D2): base 10 min, teto 6 h. (base_s, teto_s, max_tentativas)
POLITICA_FILA = {
    Classe.RATE_LIMIT: (600.0, 21600.0, 8),
    Classe.OPERATING: (600.0, 21600.0, 8),
    Classe.REDE: (600.0, 21600.0, 8),
    Classe.SERVIDOR: (600.0, 21600.0, 8),
    Classe.URL_EXPIRADA: (600.0, 3600.0, 4),
    Classe.LOOP: (900.0, 21600.0, 8),
    Classe.DESCONHECIDO: (1800.0, 21600.0, 3),
}


def atraso(tentativas: int, erro: Erro, fila: bool = False) -> float | None:
    """Segundos até a próxima tentativa; None = desistir.

    tentativas = quantas já foram feitas (incluindo a que falhou agora).
    Exponencial base*2^(n-1) com teto e "equal jitter" (metade fixa + metade
    aleatória), nunca menor que erro.espera_s (Retry-After/operating_reset_at/pausa).
    fila=True usa POLITICA_FILA (minutos/horas, para retry agendado entre ciclos)."""
    if erro.permanente:
        return None
    if not erro.repetir and erro.classe != Classe.LOOP:
        return None          # ex.: escrita ambígua após timeout
    tabela = POLITICA_FILA if fila else POLITICA
    base, teto, maximo = tabela.get(erro.classe, tabela[Classe.DESCONHECIDO])
    n = max(1, int(tentativas))
    if n >= maximo:
        return None
    bruto = min(teto, base * (2 ** (n - 1)))
    valor = bruto / 2.0 + _rnd.uniform(0.0, bruto / 2.0)
    if erro.espera_s > 0:
        piso = erro.espera_s + _rnd.uniform(0.0, min(5.0, 0.1 * erro.espera_s))
        valor = max(valor, piso)
    return float(valor)


def proxima_tentativa(tentativas: int, erro: Erro, agora: float | None = None,
                      fila: bool = False) -> float | None:
    """Instante (epoch, mesma escala de time.time()) da próxima tentativa; None = desistir
    (permanente ou excedeu MAX_TENTATIVAS[classe]). Para segundos relativos use atraso().
    fila=True: escala de fila persistida (POLITICA_FILA)."""
    a = atraso(tentativas, erro, fila=fila)
    if a is None:
        return None
    return (time.time() if agora is None else agora) + a


# ===========================================================================
# Limitador por host (token bucket entre processos)
# ===========================================================================
TAXA_PADRAO = 1.5
RAJADA_PADRAO = 10


class TempoEsgotado(TimeoutError):
    """aguardar() precisaria esperar mais que `timeout` — nada foi reservado."""


def _env_float(nome: str, padrao: float, minimo: float, maximo: float) -> float:
    bruto = os.environ.get(nome)
    if not bruto:
        return padrao
    try:
        v = float(bruto)
    except ValueError:
        log.warning("%s inválido (%r); usando %s", nome, bruto, padrao)
        return padrao
    if not (minimo <= v <= maximo):
        log.warning("%s=%s fora de [%s, %s]; usando %s", nome, v, minimo, maximo, padrao)
        return padrao
    return v


def _caminho_ratelimit_padrao() -> str:
    env = os.environ.get("BITRIX_RATELIMIT_FILE")
    if env:
        return env
    import caminhos
    return caminhos.em_base(".bitrix-ratelimit")


class LimitadorHost:
    """Token bucket compartilhado por TODOS os processos que usam o mesmo arquivo.

    O estado {tokens, ts} mora num arquivo pequeno travado com fcntl.flock. Cada
    aguardar() reserva o seu "lugar na fila" (os tokens podem ficar negativos =
    dívida) e dorme FORA da trava até a sua vez. Garante, em qualquer janela de
    T segundos, no máximo rajada + taxa*T liberações somando todos os processos.

    Padrão 1.5 req/s, rajada 10 (abaixo dos 2 req/s / 50 da doc porque o IP é
    compartilhado com outras integrações do servidor). Env: BITRIX_RATE,
    BITRIX_RAJADA, BITRIX_RATELIMIT_FILE. Sem acesso ao arquivo, cai para um
    bucket em memória (só protege o próprio processo) e avisa no log.
    """

    _memoria: dict = {}
    _memoria_lock = threading.Lock()

    def __init__(self, caminho=None, taxa=None, rajada=None):
        self.caminho = caminho or _caminho_ratelimit_padrao()
        self.taxa = float(taxa) if taxa is not None else _env_float("BITRIX_RATE", TAXA_PADRAO, 0.01, 100.0)
        self.rajada = float(rajada) if rajada is not None else _env_float(
            "BITRIX_RAJADA", float(RAJADA_PADRAO), 1.0, 1000.0)
        if self.taxa <= 0 or self.rajada < 1:
            raise ValueError("taxa deve ser > 0 e rajada >= 1")
        self._agora = time.time
        self._dormir = time.sleep
        self._avisou = False

    # -- estado --------------------------------------------------------------
    def _repor(self, est: dict | None, agora: float) -> tuple[float, float]:
        if not est:
            return self.rajada, agora
        try:
            tokens = float(est["tokens"])
            ts = float(est["ts"])
        except (KeyError, TypeError, ValueError):
            return 0.0, agora            # corrompido: conservador (balde vazio)
        if ts > agora:                   # relógio voltou: não credita nada
            ts = agora
        tokens = min(self.rajada, tokens + (agora - ts) * self.taxa)
        return tokens, agora

    def _reservar(self, custo: float, timeout: float) -> float:
        agora = self._agora()
        fd = None
        try:
            pasta = os.path.dirname(os.path.abspath(self.caminho))
            os.makedirs(pasta, exist_ok=True)
            fd = os.open(self.caminho, os.O_RDWR | os.O_CREAT, 0o664)
            # A4 (Revisor OpenBot): trava com prazo — o timeout de aguardar() também cobre a espera
            # pela trava de outro processo, não só a espera por tokens.
            prazo = time.monotonic() + max(0.5, float(timeout))
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= prazo:
                        os.close(fd)
                        fd = None
                        raise TempoEsgotado(
                            f"limitador: trava de {self.caminho} ocupada por mais de {timeout:.1f}s")
                    time.sleep(0.02)
        except OSError as e:
            if fd is not None:
                os.close(fd)
            if not self._avisou:
                log.warning("limitador: sem acesso a %s (%s); usando bucket em memória", self.caminho, e)
                self._avisou = True
            return self._reservar_memoria(custo, timeout, agora)
        try:
            bruto = os.read(fd, 4096)
            est = None
            if bruto.strip():
                try:
                    est = json.loads(bruto.decode("utf-8"))
                    if not isinstance(est, dict):
                        est = {"tokens": None}
                except ValueError:
                    est = {"tokens": None}     # corrompido -> conservador
            tokens, ts = self._repor(est, agora)
            novos = tokens - custo
            espera = max(0.0, -novos / self.taxa)
            if espera > timeout:
                raise TempoEsgotado(
                    f"limitador: precisaria esperar {espera:.1f}s (> timeout {timeout:.1f}s)")
            dados = json.dumps({"tokens": round(novos, 6), "ts": ts, "taxa": self.taxa,
                                "rajada": self.rajada}).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, dados)
            return espera
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _reservar_memoria(self, custo: float, timeout: float, agora: float) -> float:
        with self._memoria_lock:
            tokens, ts = self._repor(self._memoria.get(self.caminho), agora)
            novos = tokens - custo
            espera = max(0.0, -novos / self.taxa)
            if espera > timeout:
                raise TempoEsgotado(
                    f"limitador: precisaria esperar {espera:.1f}s (> timeout {timeout:.1f}s)")
            self._memoria[self.caminho] = {"tokens": novos, "ts": ts}
            return espera

    # -- API -------------------------------------------------------------------
    def aguardar(self, custo=1, timeout=60) -> float:
        """Bloqueia até poder fazer `custo` requisições; devolve os segundos esperados.
        Levanta TempoEsgotado (sem consumir nada) se a espera passaria de `timeout`."""
        custo = float(custo)
        if custo <= 0:
            return 0.0
        espera = self._reservar(custo, float(timeout))
        if espera > 0:
            self._dormir(espera)
        return espera


_padroes: dict = {}
_padroes_lock = threading.Lock()


def limitador_padrao() -> LimitadorHost:
    """Limitador do host (caminho/taxa/rajada vindos do ambiente), um por processo."""
    lim = LimitadorHost()
    chave = (os.path.abspath(lim.caminho), lim.taxa, lim.rajada)
    with _padroes_lock:
        return _padroes.setdefault(chave, lim)


# ===========================================================================
# Disjuntores
# ===========================================================================
# nome -> (max_falhas, timeout_s). bitrix-* com 180 s (CIRCUIT_PAUSA_SEG do legado).
CIRCUITOS_PADRAO = {
    "bitrix-leitura": (3, 180.0),
    "bitrix-download": (3, 180.0),
    "vibecode-leitura": (3, 300.0),
    "vibecode-download": (3, 300.0),
}
_RE_NOME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def conta_falha_padrao(exc: BaseException) -> bool:
    """Quais exceções contam contra a SAÚDE da fonte (achado B4): bug nosso
    (TypeError, KeyError...) e falha de item (404, tamanho, URL vencida) não contam."""
    if isinstance(exc, CircuitOpenError):
        return False
    if isinstance(exc, _EXC_PROGRAMACAO):
        return False
    return classificar(None, "", exc).conta_no_circuito


def circuito(nome: str, max_falhas: int | None = None, timeout_s: float | None = None) -> CircuitBreaker:
    """Disjuntor `nome` com estado em caminhos.em_base(f"circuit-{nome}.json")."""
    if not isinstance(nome, str) or not _RE_NOME.match(nome):
        raise ValueError(f"nome de circuito inválido: {nome!r}")
    import caminhos
    mf, ts = CIRCUITOS_PADRAO.get(nome, (circuit_breaker.MAX_FALHAS_PADRAO, circuit_breaker.TIMEOUT_PADRAO))
    return CircuitBreaker(nome, arquivo=caminhos.em_base(f"circuit-{nome}.json"),
                          max_falhas=max_falhas or mf, timeout_s=timeout_s or ts,
                          conta_falha=conta_falha_padrao)


def aplicar_no_circuito(circ: CircuitBreaker, erro: Erro) -> None:
    """Registra um Erro classificado no disjuntor.

    LOOP/OVERLOAD/OPERATING abrem JÁ pela espera indicada (insistir renova o
    bloqueio); demais falhas de saúde contam para as N seguidas; falhas de item
    (404, tamanho, tipo, integridade, URL vencida, inválido) não mexem no circuito."""
    if erro.abre_circuito_ja:
        circ.abrir(motivo=erro.classe.value, duracao_s=max(erro.espera_s, 1.0), erro=erro.detalhe)
    elif erro.conta_no_circuito:
        circ.registrar_falha(erro.detalhe, motivo=erro.classe.value)
    else:
        circ.liberar_sonda()
