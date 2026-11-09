#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
circuit_breaker.py — disjuntor de falhas das fontes externas da triagem.

Motivo: a API do VibeCode é instável (502/401 intermitentes, medido 06/10/2026)
e o portal Bitrix bloqueia quem insiste (QUERY_LIMIT_EXCEEDED, ERROR_LOOP_DETECTED).
O disjuntor desativa a fonte que falhar N vezes seguidas por T segundos e o
chamador redireciona para a fonte saudável (ou pausa).

Estados (padrão circuit breaker):
  closed    -> chamadas normais; falha incrementa o contador consecutivo.
  open      -> chamadas PULADAS (CircuitOpenError) até `aberto_ate` (sempre finito).
  half_open -> prazo venceu: UMA única chamada de teste (a "sonda") é liberada,
               mesmo entre processos; sucesso fecha (zera o contador), falha
               reabre por mais T. A sonda tem prazo (`sonda_ate`): se o dono
               morrer sem registrar resultado, outra sonda é liberada depois.

Persistência (endurecida na remediação, frente R):
  - JSON com UMA chave por disjuntor; vários disjuntores podem dividir o arquivo.
  - Toda mutação é ler-modificar-gravar SOB flock (`<arquivo>.lock`): relê a
    chave do disco, aplica a transição e grava. Antes, a instância gravava o seu
    snapshot em memória e podia apagar a transição feita por outro processo (B4).
  - Gravação atômica: tmp exclusivo (pid+uuid) + fsync + os.replace + fsync do dir.
  - Campos: estado, falhas, aberto_em, aberto_ate, motivo, ultimo_erro (mascarado),
    fechado_em, ultimo_aberto_em, ultimo_motivo, sonda_dono, sonda_ate, atualizado_em.
  - "Aberto para sempre" é impossível: `aberto_ate` sempre é gravado, e um valor
    absurdo (relógio pulou, arquivo legado/editado) é limitado a `max_abertura_s`.
  - Se não der para gravar (disco/permissão), segue funcionando em memória.

Somente biblioteca padrão.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
import uuid

import caminhos as _caminhos
import seguranca  # sem ciclo: seguranca importa só caminhos/cpf_util

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

BASE = os.path.dirname(os.path.abspath(__file__))
ARQ_BREAKER = os.environ.get("CIRCUIT_BREAKER_JSON") or (
    _caminhos.em_base("circuit_breaker.json"))

MAX_FALHAS_PADRAO = 3
TIMEOUT_PADRAO = 300.0
SONDA_PADRAO_MAX_S = 120.0           # prazo da sonda em meia-abertura (teto)
MAX_ABERTURA_PADRAO_S = 24 * 3600.0  # nenhum disjuntor fica aberto mais que isso


class CircuitOpenError(RuntimeError):
    """A fonte está ABERTA (ou em meia-abertura com sonda ocupada) — pule e redirecione."""

    def __init__(self, mensagem: str = "", ate: float | None = None, motivo: str | None = None):
        super().__init__(mensagem)
        self.ate = ate
        self.motivo = motivo


# ----------------------------------------------------------------- máscara
# seguranca.mascarar já cobre `token=`/`access_token=`/`auth=`/`refresh_token=`/
# `client_secret=` em texto livre e chave JSON (C2b/Z2). Esta camada extra fica
# por defesa em profundidade: pega também x-api-key/api_key/senha/password, que
# a máscara compartilhada não trata como par livre. Idempotente sobre `***`.
_RE_CHAVE = re.compile(
    r"(?i)((?:x-api-key|api[_-]?key|access_token|token|auth|senha|password)[\"']?\s*[:=]\s*[\"']?)"
    r"[^\s\"'&,}*]+")


def mascarar_texto(texto) -> str:
    """Mascara segredos/CPF/token Telegram via seguranca.mascarar (frente S) e,
    por cima, pares chave=valor de token/auth que ela não cobre."""
    if texto is None:
        return ""
    return _RE_CHAVE.sub(r"\1***", seguranca.mascarar(str(texto)))


class CircuitBreaker:
    """Disjuntor por fonte. Thread-safe no processo e coerente entre processos."""

    def __init__(self, nome: str, arquivo: str | None = None,
                 max_falhas: int = MAX_FALHAS_PADRAO,
                 timeout_s: float = TIMEOUT_PADRAO,
                 agora=None,
                 sonda_s: float | None = None,
                 conta_falha=None,
                 max_abertura_s: float | None = None):
        self.nome = str(nome)
        self.arquivo = arquivo or ARQ_BREAKER
        self.max_falhas = max(1, int(max_falhas))
        self.timeout_s = max(1.0, float(timeout_s))
        self.max_abertura_s = max(self.timeout_s, float(max_abertura_s or MAX_ABERTURA_PADRAO_S))
        self.sonda_s = max(1.0, float(sonda_s)) if sonda_s else min(self.timeout_s, SONDA_PADRAO_MAX_S)
        # conta_falha(exc) -> bool: quais exceções de `chamar` contam como falha
        # da FONTE. Padrão (compatível): todas. rede_bitrix.circuito() passa um
        # filtro que ignora bug nosso (TypeError, KeyError...) — achado B4.
        self._conta_falha = conta_falha or (lambda exc: True)
        self._agora = agora or time.time
        self._dono = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._lock = threading.RLock()
        self._persistencia_ok = True
        with self._lock:
            self._dados = self._ler_disco() or {}

    # ------------------------------------------------------------------ leitura
    def estado(self) -> str:
        with self._lock:
            self._sincronizar()
            return self._dados.get("estado", CLOSED)

    def falhas(self) -> int:
        with self._lock:
            self._sincronizar()
            return int(self._dados.get("falhas") or 0)

    def situacao(self) -> dict:
        """Cópia do estado persistido (para painel/monitor), já normalizado."""
        with self._lock:
            self._sincronizar()
            d = dict(self._dados)
            d.setdefault("estado", CLOSED)
            d.setdefault("falhas", 0)
            d["nome"] = self.nome
            return d

    def permitido(self) -> bool:
        """True se a chamada pode seguir AGORA.

        closed -> True; open (no prazo) -> False; half_open -> True só para quem
        obtiver a sonda (uma por vez, entre processos). Quem pegou a sonda deve
        registrar sucesso/falha (ou `liberar_sonda()`).
        """
        with self._lock:
            self._sincronizar()
            est = self._dados.get("estado", CLOSED)
            if est == CLOSED:
                return True
            if est == OPEN:
                return False
            resultado = {"ok": False}

            def pegar_sonda(d: dict, agora: float) -> bool:
                if d.get("estado", CLOSED) == CLOSED:
                    resultado["ok"] = True
                    return False
                if d.get("estado") != HALF_OPEN:
                    return False
                dono = d.get("sonda_dono")
                if dono and dono != self._dono and float(d.get("sonda_ate") or 0) > agora:
                    return False            # outro processo está sondando
                d["sonda_dono"] = self._dono
                d["sonda_ate"] = agora + self.sonda_s
                resultado["ok"] = True
                return True

            self._transacao(pegar_sonda)
            return resultado["ok"]

    # ------------------------------------------------------------- transições
    def registrar_sucesso(self) -> None:
        def fechar(d: dict, agora: float) -> bool:
            if (d.get("estado", CLOSED) == CLOSED and not d.get("falhas")
                    and not d.get("sonda_dono") and "aberto_ate" not in d):
                return False                # nada mudou: não regrava
            if d.get("estado") in (OPEN, HALF_OPEN) or d.get("aberto_em"):
                d["fechado_em"] = agora
                if d.get("aberto_em"):
                    d["ultimo_aberto_em"] = d.get("aberto_em")
                if d.get("motivo"):
                    d["ultimo_motivo"] = d.get("motivo")
            d["estado"] = CLOSED
            d["falhas"] = 0
            for k in ("aberto_ate", "aberto_em", "motivo", "sonda_dono", "sonda_ate"):
                d.pop(k, None)
            return True

        with self._lock:
            self._transacao(fechar)

    def registrar_falha(self, erro: str | None = None, motivo: str | None = None) -> None:
        def falhar(d: dict, agora: float) -> bool:
            era_meia = d.get("estado") == HALF_OPEN
            falhas = int(d.get("falhas") or 0) + 1
            d["falhas"] = falhas
            if erro:
                d["ultimo_erro"] = mascarar_texto(erro)[:300]
            if era_meia or falhas >= self.max_falhas:
                padrao = "sonda da meia-abertura falhou" if era_meia else f"{falhas} falhas seguidas"
                self._abrir_em(d, agora, self.timeout_s, motivo or padrao)
            return True

        with self._lock:
            self._transacao(falhar)

    def abrir(self, motivo: str, duracao_s: float | None = None, erro: str | None = None) -> None:
        """Abre JÁ (sem esperar N falhas): ERROR_LOOP_DETECTED, OVERLOAD_LIMIT,
        OPERATION_TIME_LIMIT. `duracao_s` é limitada a [1, max_abertura_s]."""
        def abrir_ja(d: dict, agora: float) -> bool:
            d["falhas"] = max(int(d.get("falhas") or 0) + 1, self.max_falhas)
            if erro:
                d["ultimo_erro"] = mascarar_texto(erro)[:300]
            self._abrir_em(d, agora, self.timeout_s if duracao_s is None else duracao_s, motivo)
            return True

        with self._lock:
            self._transacao(abrir_ja)

    def fechar(self) -> None:
        """Fecha manualmente (equivale a um sucesso)."""
        self.registrar_sucesso()

    def liberar_sonda(self) -> None:
        """Devolve a sonda sem resultado (ex.: a chamada falhou por bug nosso)."""
        def liberar(d: dict, agora: float) -> bool:
            if d.get("sonda_dono") != self._dono:
                return False
            d.pop("sonda_dono", None)
            d.pop("sonda_ate", None)
            return True

        with self._lock:
            self._transacao(liberar)

    def chamar(self, fn, *args, **kwargs):
        """Executa fn() respeitando o disjuntor.

        - aberto (não expirado)          -> CircuitOpenError (fn NÃO roda).
        - meia-abertura, sonda ocupada   -> CircuitOpenError (outro processo testa).
        - meia-abertura, sonda nossa     -> roda fn como teste; sucesso fecha, falha reabre.
        - fechado                        -> roda fn; falha incrementa o contador.
        Exceção que `conta_falha` rejeita (bug nosso) não conta como falha da fonte.
        """
        if not self.permitido():
            with self._lock:
                d = self._dados
                ate = d.get("aberto_ate")
                if d.get("estado") == HALF_OPEN:
                    msg = (f"fonte '{self.nome}' em MEIA-ABERTURA: sonda em andamento "
                           f"(falhas={d.get('falhas')})")
                else:
                    msg = (f"fonte '{self.nome}' ABERTA até {float(ate or 0):.0f} "
                           f"(falhas={d.get('falhas')}, motivo={d.get('motivo') or '-'})")
                raise CircuitOpenError(msg, ate=ate, motivo=d.get("motivo"))
        try:
            resultado = fn(*args, **kwargs)
        except Exception as e:                      # noqa: BLE001
            try:
                conta = bool(self._conta_falha(e))
            except Exception:                       # noqa: BLE001
                conta = True
            if conta:
                self.registrar_falha(str(e))
            else:
                self.liberar_sonda()
            raise
        self.registrar_sucesso()
        return resultado

    # ------------------------------------------------------------ internos
    def _abrir_em(self, d: dict, agora: float, duracao_s: float, motivo: str | None) -> None:
        duracao = min(max(1.0, float(duracao_s)), self.max_abertura_s)
        d["estado"] = OPEN
        d["aberto_em"] = agora
        d["aberto_ate"] = agora + duracao
        d["motivo"] = mascarar_texto(motivo or "falhas")[:200]
        d.pop("sonda_dono", None)
        d.pop("sonda_ate", None)

    def _normalizar(self, d: dict, agora: float | None = None) -> bool:
        """open vencido -> half_open; prazos absurdos limitados; sonda vencida liberada.
        Devolve True se precisou LIMITAR um prazo absurdo (deve ser persistido)."""
        agora = self._agora() if agora is None else agora
        limitou = False
        if d.get("estado") == OPEN:
            ate = d.get("aberto_ate")
            try:
                ate = float(ate) if ate is not None else None
            except (TypeError, ValueError):
                ate = None
            if ate is not None and ate - agora > self.max_abertura_s:
                ate = agora + self.timeout_s      # nunca "aberto para sempre"
                d["aberto_ate"] = ate
                d.setdefault("aberto_em", agora)
                d["motivo"] = (str(d.get("motivo") or "") + " [prazo absurdo limitado]").strip()[:200]
                limitou = True
            if ate is None or agora >= ate:
                d["estado"] = HALF_OPEN
        if d.get("estado") == HALF_OPEN and d.get("sonda_dono"):
            try:
                vence = float(d.get("sonda_ate") or 0)
            except (TypeError, ValueError):
                vence = 0.0
            if vence <= agora or vence - agora > self.max_abertura_s:
                d.pop("sonda_dono", None)
                d.pop("sonda_ate", None)
        return limitou

    def _sincronizar(self) -> None:
        """Relê a chave do disco (outros processos podem ter mudado) e normaliza."""
        if self._persistencia_ok:
            item = self._ler_disco()
            if item is not None:
                self._dados = item
        if self._normalizar(self._dados) and self._persistencia_ok:
            # prazo absurdo: grava o limite já, senão cada leitura o empurraria adiante
            self._transacao(lambda d, agora: False)

    def _ler_disco(self) -> dict | None:
        """dict da chave (vazio se ausente); None se arquivo ausente/ilegível."""
        try:
            with open(self.arquivo, "r", encoding="utf-8") as fh:
                tudo = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(tudo, dict):
            return None
        item = tudo.get(self.nome)
        return dict(item) if isinstance(item, dict) else {}

    def _transacao(self, mutador) -> None:
        """flock -> relê a chave -> normaliza -> mutador(d, agora) -> grava atômico.

        mutador devolve False quando nada mudou (não regrava). Sem disco, aplica
        em memória (best-effort)."""
        agora = self._agora()
        fd = None
        try:
            pasta = os.path.dirname(os.path.abspath(self.arquivo))
            os.makedirs(pasta, exist_ok=True)
            fd = os.open(self.arquivo + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd is not None:
                os.close(fd)
            fd = None
        try:
            if fd is not None:
                tudo = self._ler_tudo()
                item = tudo.get(self.nome)
                if isinstance(item, dict):
                    d = dict(item)
                else:
                    d = {} if self._persistencia_ok else dict(self._dados)
            else:
                tudo, d = {}, dict(self._dados)
            limitou = self._normalizar(d, agora)
            mudou = mutador(d, agora)
            d["estado"] = d.get("estado", CLOSED)
            if mudou is not False or limitou:
                d["atualizado_em"] = agora
            self._dados = d
            if fd is None:
                self._persistencia_ok = False
                return
            if mudou is False and not limitou:
                return
            tudo[self.nome] = dict(d)
            self._persistencia_ok = self._gravar_atomico(tudo)
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _ler_tudo(self) -> dict:
        try:
            with open(self.arquivo, "r", encoding="utf-8") as fh:
                tudo = json.load(fh)
            return tudo if isinstance(tudo, dict) else {}
        except (OSError, ValueError):
            return {}

    def _gravar_atomico(self, tudo: dict) -> bool:
        tmp = f"{self.arquivo}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(tudo, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.arquivo)
            try:
                dfd = os.open(os.path.dirname(os.path.abspath(self.arquivo)), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
