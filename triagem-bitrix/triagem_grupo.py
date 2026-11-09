#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
triagem_grupo.py — FASE 1 da triagem automática de documentos por GRUPO do Bitrix24.

FONTE DE LEITURA (trocada em 21/09/2026): **API do VibeCode**
  GET {VIBECODE_BASE}/v1/chats/{dialogId}/messages  (header X-Api-Key)
  Resposta: {'success': true, 'data': {'chatId': N, 'messages': [...],
             'users': [...], 'files': [...]}}

  Por que trocou: o webhook do Bitrix (im.dialog.messages.get) é um
  SUBCONJUNTO do grupo — medido em 21/09/2026 no mesmo instante: API 386 x
  webhook 234 (nenhum id exclusivo do webhook; ele perdia 62 das 85 mensagens
  do dia, justamente os kits/anexos e os traços). A API do VibeCode vê tudo.
  A página da API é limitada a 50 mensagens (limit>50 é ignorado) e pagina-se
  para trás com ?lastId=<menor id da página anterior> (e para frente com
  ?firstId=<maior id já visto>). O webhook continua no arquivo apenas como
  FONTE DE DIAGNÓSTICO (--fonte-webhook), nunca como fonte primária.

O que esta fase FAZ:
  1. Lê as mensagens do grupo pela API do VibeCode (ordem cronológica) e os
     eventos do bot (GET /v1/bots/100003/events) usando cursor persistido.
  2. Descarta as REAÇÕES ('>>[B]Mensagem de reação[/B]') antes de qualquer
     processamento: reação não vira lote e não entra na extração de nome/CPF.
  3. Agrupa as mensagens em LOTES por (autor + janela de tempo), com o TRAÇO
     funcionando como TOGGLE por autor.
  4. Identifica o cliente do lote pelo cabeçalho '=NOME / =CPF' e/ou pela FICHA.
  5. Baixa os anexos pela API (GET /v1/files/{fileId}/download, com o
     X-Api-Key) IMEDIATAMENTE — nunca guarda URL/token.
  6. Grava estado em JSON + log rotacionável.

O que esta fase NÃO faz (é a fase 2): jogar os lotes na fila de processamento,
criar contato/deal no CRM, mexer em /opt/agente-separador ou PrevSync.

Restrições respeitadas:
  - SOMENTE biblioteca padrão (urllib). Nada instalado.
  - NENHUMA credencial embutida no código: chave do VibeCode e webhook vêm de
    seguranca.ler_segredo("vibecode_key"/"webhook_bitrix") (env TRIAGEM_* ou
    /etc/triagem-bitrix/, 0600). Nada de varrer outros projetos nem /tmp.
  - Somente leitura no Bitrix: não apaga, não marca como lido.

Uso:
  python3 triagem_grupo.py --dry-run           # mostra o que faria, não baixa
  python3 triagem_grupo.py --once              # processa e baixa de verdade
  python3 triagem_grupo.py --loop 300          # loop de 300s
  python3 triagem_grupo.py --testar-cpf        # valida o extrator de CPF
  python3 triagem_grupo.py --testar-sinais     # valida o toggle de traço
  python3 triagem_grupo.py --testar-fonte      # prova a fonte nova com os dados de hoje
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# disjuntor de falhas das DUAS fontes de leitura (módulo local, só stdlib)
from circuit_breaker import CircuitBreaker, CircuitOpenError

# Contratos compartilhados (docs/remediacao/CONTRATOS.md) — onda 2, frente C1.
import bitrix_cliente                        # cliente REST ÚNICO do Bitrix (segredo, limitador, disjuntor)
import caminhos
import cpf_util
import estado_lote
import estado_store
import rede_bitrix as rb
import seguranca
import tipos_documento
import threading
import uuid

# Camada 3 — microserviço de download (fila de retry persistente). Uso opcional:
# quando o download falha no fluxo principal, o fileId entra na fila dele e é
# re-tentado em background com backoff exponencial. Import com fallback (não
# derruba a triagem se o módulo estiver ausente).
try:
    import download_svc                      # noqa: F401
except ImportError:                          # noqa: BLE001
    download_svc = None

# --------------------------------------------------------------------------
# Configuração (sem credenciais no código)
# --------------------------------------------------------------------------
# Contrato: caminhos.BASE ($TRIAGEM_BASE ou /opt/triagem-bitrix). TRIAGEM_DIR continua
# aceito como ALIAS (legado das units) e, se definido, tem precedência.
BASE_DIR = os.environ.get("TRIAGEM_DIR") or caminhos.BASE
ARQ_CHAVE = os.path.join(BASE_DIR, "vibecode.key")   # só exibição; a leitura é seguranca.ler_segredo
ARQ_BOT = os.path.join(BASE_DIR, "bot.json")
ARQ_ESTADO = os.environ.get("ESTADO_JSON") or os.path.join(BASE_DIR, "estado.json")
ARQ_LOG = os.path.join(BASE_DIR, "triagem.log")
DIR_LOTES = os.path.join(BASE_DIR, "lotes")
ARQ_BREAKER = os.path.join(BASE_DIR, "circuit_breaker.json")

VIBECODE_BASE = os.environ.get("VIBECODE_BASE", "https://vibecode.bitrix24.com")
BOT_ID_PADRAO = 100003
DIALOG_ID_PADRAO = "chat100001"
DIALOG_IDS_PADRAO = ["chat100001"]

JANELA_MIN_PADRAO = 30          # janela de agrupamento por autor (minutos)
HTTP_TIMEOUT = 10               # (era 30) o portal às vezes pendura a conexão; 30s x 5 tentativas = 150s por arquivo e o ciclo nunca fecha
HTTP_TENTATIVAS = 5             # (era 3) o portal está com 503 intermitente ~20-30%: 5 tentativas reduzem a falha final a ~0,2%
HTTP_BACKOFF = 1.6              # fator exponencial de espera entre tentativas

# Circuit-breaker do download (bloqueio do Bitrix): quando o portal pausa o
# disk.file.get por ACCESS_DENIED/loop ("the block clears after the first
# successful call"), martelar com retry MANTÉM o bloqueio vivo.
# Onda 2 (B1/B2/B3/B4): o disjuntor agora é `rede_bitrix.circuito("bitrix-download")`
# (estado em circuit-bitrix-download.json, com motivo, fecha no 1º sucesso, sonda
# única). Só o passo do webhook/disk.file.get o consulta (B1); a leitura usa
# "bitrix-leitura"/"vibecode-leitura" (B2). O arquivo legado `circuit-download.json`
# é lido UMA vez (migração) e renomeado para `.migrado-<ts>`; nunca é recriado.
CIRCUIT_ARQUIVO = os.path.join(BASE_DIR, "circuit-download.json")   # LEGADO (só migração)
CIRCUIT_PAUSA_SEG = 180          # 3 min: prazo do legado (igual ao padrão de bitrix-download)
_MIGRACAO_CIRCUITO = {"feita": False}


def _migrar_circuito_legado() -> None:
    """Migra `circuit-download.json` ({"aberto_em": ts}) para o disjuntor novo, uma vez.

    Se ainda estava dentro da pausa, abre `bitrix-download` pelo tempo restante.
    Depois renomeia o arquivo (não apaga) para não ser lido de novo. Nunca levanta.
    """
    if _MIGRACAO_CIRCUITO["feita"]:
        return
    _MIGRACAO_CIRCUITO["feita"] = True
    try:
        if not os.path.exists(CIRCUIT_ARQUIVO):
            return
        try:
            with open(CIRCUIT_ARQUIVO, encoding="utf-8") as fh:
                aberto_em = float((json.load(fh) or {}).get("aberto_em") or 0)
        except (OSError, ValueError, AttributeError):
            aberto_em = 0.0
        resta = CIRCUIT_PAUSA_SEG - (time.time() - aberto_em)
        if aberto_em and resta > 1:
            rb.circuito("bitrix-download").abrir("legado circuit-download.json", duracao_s=resta)
        os.replace(CIRCUIT_ARQUIVO, f"{CIRCUIT_ARQUIVO}.migrado-{time.strftime('%Y%m%d-%H%M%S')}")
        log.info("circuit-download.json legado migrado para circuit-bitrix-download.json")
    except Exception as e:                        # noqa: BLE001 — migração nunca derruba o ciclo
        log.warning("migração do circuit-download.json legado falhou: %s", e)


def _circuit_download_aberto() -> bool:
    """True se o disjuntor de DOWNLOAD do Bitrix (`bitrix-download`) não libera chamada.

    Compat: antes lia `circuit-download.json`. Só o passo do webhook deve consultar (B1).
    """
    _migrar_circuito_legado()
    try:
        return not rb.circuito("bitrix-download").permitido()
    except Exception:                             # noqa: BLE001
        return False


def _registrar_bloqueio_download(motivo: str = "RATE_LIMIT") -> None:
    """Compat: abre `bitrix-download` pela pausa do legado. O caminho normal é
    `bitrix_cliente.chamar(..., circuito="bitrix-download")`, que classifica sozinho."""
    try:
        rb.circuito("bitrix-download").abrir(motivo, duracao_s=CIRCUIT_PAUSA_SEG)
    except Exception as e:                        # noqa: BLE001
        # F1-13: o disjuntor NÃO abriu — o próximo download martela o portal.
        log.error("não consegui abrir o disjuntor bitrix-download (%s): %s",
                  motivo, seguranca.mascarar(str(e)))

BOT_ID_AULA = BOT_ID_PADRAO     # autor a ignorar (o próprio bot)

# S06: o webhook vem SÓ de seguranca.ler_segredo("webhook_bitrix") (env
# TRIAGEM_WEBHOOK_BITRIX -> /etc/triagem-bitrix/webhook_bitrix). A antiga varredura de
# arquivos de outros projetos (mcp, PrevSync, bot.json) foi removida.

# --- Marcadores de texto -----------------------------------------------------
RX_REMETENTE = re.compile(r"^\[B\](.*?):\[/B\]", re.S)          # "[B]Jessica Nonata:[/B]"
RX_APAGADA = re.compile(r"🗑️?\s*Mensagem apagada no celular", re.I)

# --- FONTE NOVA: API do VibeCode ---------------------------------------------
# GET /v1/chats/{dialogId}/messages  -> {success, data:{chatId, messages[], users[], files[]}}
# PROVADO 21/09/2026: a página vem com no MÁXIMO 50 mensagens (limit>50 é
# ignorado) e a mensagem nova chega DENTRO da 1ª página. Para o histórico:
#   ?lastId=<menor id da página anterior>  -> mensagens MAIS ANTIGAS
#   ?firstId=<maior id já lido>            -> mensagens MAIS NOVAS
MSG_PAGINA = 50                 # teto real da API por página
MSG_PAGINAS_HISTORICO = 12      # 12 x 50 = 600 mensagens de histórico por ciclo
# F1-01: leitura incremental (cursor por chat). Teto de páginas por ciclo para
# alcançar o cursor; se acabar antes, é LACUNA (cursor não avança, alerta).
F1_MAX_PAGINAS_INCREMENTAL = 40   # 40 x 50 = 2.000 mensagens novas por ciclo e por chat
F1_MAX_REGIAO = 2000              # teto de mensagens da região provisória persistida

# As mensagens trazem os ids de arquivo em params; a API usa 'fileId' minúsculo
# (o webhook usava 'FILE_ID'). Aceitamos as duas grafias.
CHAVES_FILE_ID = ("fileId", "FILE_ID")

# REAÇÃO do WhatsApp: texto '... [B]Nome:[/B] >>[B]Mensagem de reação[/B] ...'.
# NUNCA é conteúdo de triagem: é descartada ANTES de virar lote e ANTES de
# entrar na extração de nome/CPF.
RX_REACAO = re.compile(r"Mensagem de rea[çc][ãa]o", re.I)

CAMPO_FICHA = "FICHA DE ATENDIMENTO"
ABERTURA = "DADOS NECESSÁRIOS PARA DAR A ENTRADA"

# Marcadores de FIM de lote observados em produção.
MARCADORES_FIM = [
    "assinou e agendou",
    "pasta finalizada",
    "pasta finalizada no escritorio",
    "ficha finalizada",
]
# 'Assinou e agendou' já apareceu NO MEIO do lote, com arquivos DEPOIS dele
# (o PDF do protocolo). Fechar na hora truncaria o lote -> usamos folga.
MARCADOR_FIM_FOLGA_MIN = 5

# Folga da realocação da barreira (PONTO 2.3): salto máximo entre mensagens
# para o rastro sem identidade ainda ser "colado" no cabeçalho do cliente novo.
# NÃO confundir com a janela de 30 min do fallback — aqui é 10 min.
REALOC_FOLGA_MIN = 10

# Notificações automáticas do Bitrix (não são conteúdo da equipe).
RX_RUIDO_SISTEMA = [
    re.compile(r"^Conversa #\[URL=", re.I),
    re.compile(r"^\[b\]Um novo cliente potencial foi criado", re.I),
    re.compile(r"^\[b\]Pedido anexado", re.I),
    re.compile(r"^\[b\]Informa[çc][õo]es de contato salvas", re.I),
    re.compile(r"^Consulta atribu[íi]da", re.I),
    re.compile(r"aceitou a conversa", re.I),
    re.compile(r"convidou .* para conversar", re.I),
    re.compile(r"^A conversa foi atribu[íi]da ao agente", re.I),
    re.compile(r"^>>\s*Mensagem de rea[çc][ãa]o", re.I),
    re.compile(r"Mensagem de rea[çc][ãa]o", re.I),      # reação, com ou sem prefixo de autor
    re.compile(r"^Dados recebidos\b", re.I),
    re.compile(r"^Link to original post:", re.I),
    re.compile(r"foi recebida em uma mensagem", re.I),
    re.compile(r"Contato compartilhado", re.I),
]

# Palavras que NÃO são nome (aparecem em linhas '= PALAVRA ...' do cabeçalho).
# O CONJUNTO vive em nome_regras.py (CONTRATO DE NORMALIZAÇÃO v1) — este nome
# continua aqui como alias do MESMO objeto, para não quebrar quem já importa.
import nome_regras as _nome_regras  # noqa: E402
PALAVRAS_NAO_NOME = _nome_regras.PALAVRAS_NAO_NOME   # re-export explícito (alias do contrato)

log = logging.getLogger("triagem")


# --------------------------------------------------------------------------
# Log
# --------------------------------------------------------------------------
def configurar_log(verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            ARQ_LOG, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:                      # nunca deixar o log derrubar o serviço
        print(f"[aviso] não pude abrir o log em {ARQ_LOG}: {e}", file=sys.stderr)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    # S04/S07: máscara de webhook, X-Api-Key, CPF e senha em TODO registro (arquivo e journal).
    seguranca.instalar_filtro_log(log)
    seguranca.instalar_filtro_log()


# --------------------------------------------------------------------------
# HTTP (urllib puro, com retry/backoff)
# --------------------------------------------------------------------------
# RATE LIMIT DO BITRIX (medido 21/10/2026): 20 chamadas seguidas -> 7 OK e 13
# com HTTP 503 "Service Temporarily Unavailable". O portal aceita ~2 req/s;
# sem throttle o download dos anexos vira uma chuva de 503 e o ciclo nunca fecha.
_RATE_MIN_SEG = 1.0           # 1 req/s (o portal bloqueia com QUERY_LIMIT_EXCEEDED acima disso)
_ultima_chamada = [0.0]


def _throttle_bitrix() -> None:
    """Garante o intervalo mínimo entre chamadas ao portal (evita 503)."""
    espera = _RATE_MIN_SEG - (time.time() - _ultima_chamada[0])
    if espera > 0:
        time.sleep(espera)
    _ultima_chamada[0] = time.time()


# ── GUARDA ANTI-TRAVA DE DNS (27/10/2026) ────────────────────────────────────
# `urlopen(..., timeout=T)` NÃO limita a resolução de nome: getaddrinfo() pode
# ficar pendurado (nameserver mudo) e o processo congela — em 27/10 o ciclo da
# triagem passou 28 min travado assim, com `systemctl` dizendo "active" e o
# trava_de_ciclo bloqueando o ciclo disparado pelo bot. A abertura da conexão
# roda num worker com prazo DURO; estourou, a thread é abandonada (daemon) e a
# chamada falha como erro normal (retry + circuit-breaker já existem em cima).
TETO_DNS_SEG = 8
# G2: antes era um ThreadPoolExecutor(8) compartilhado; cada DNS pendurado prendia um
# worker para sempre e, com 8 presos, TODA chamada esperava na fila e estourava.
# Agora cada abertura roda numa thread daemon própria (abandonada no estouro, sem
# ocupar pool). Teto de threads penduradas: acima dele falhamos JÁ (TimeoutError,
# classificado como REDE) em vez de empilhar threads sem fim.
MAX_ABERTURAS_PENDURADAS = 32
_aberturas_vivas: set = set()
_aberturas_lock = threading.Lock()


def _abrir_com_teto(req, timeout: int):
    """urlopen com prazo duro na fase de DNS/conexão (a leitura segue no caller).

    ⚠️ `timeout` vai por KEYWORD de propósito: como 2º argumento posicional ele
    cairia em `data` do urlopen ("message_body should be a bytes-like object…
    got <class 'int'>" — bug pego em produção em 27/10, 3 min após o deploy).
    """
    with _aberturas_lock:
        vivas = [t for t in _aberturas_vivas if t.is_alive()]
        _aberturas_vivas.clear()
        _aberturas_vivas.update(vivas)
        if len(vivas) >= MAX_ABERTURAS_PENDURADAS:
            raise TimeoutError(f"guarda de DNS: {len(vivas)} aberturas penduradas — "
                               "rede/DNS indisponível")
    caixa: dict = {}
    abandonada = threading.Event()

    def _alvo():
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except BaseException as e:                # noqa: BLE001 — repassado ao chamador
            caixa["erro"] = e
            return
        if abandonada.is_set():                   # chegou tarde: não vaza a conexão
            try:
                resp.close()
            except Exception:                     # noqa: BLE001
                pass
            return
        caixa["resp"] = resp

    t = threading.Thread(target=_alvo, name="http-triagem", daemon=True)
    with _aberturas_lock:
        _aberturas_vivas.add(t)
    t.start()
    t.join(max(0.0, float(timeout or 0)) + TETO_DNS_SEG)
    if t.is_alive():
        abandonada.set()
        raise TimeoutError(f"abertura da conexão passou de {timeout}+{TETO_DNS_SEG}s (DNS/conexão)")
    with _aberturas_lock:
        _aberturas_vivas.discard(t)
    if "erro" in caixa:
        raise caixa["erro"]
    return caixa["resp"]


def _requisicao(url: str, dados: bytes | None, headers: dict, timeout: int) -> bytes:
    _throttle_bitrix()
    req = urllib.request.Request(url, data=dados, headers=headers, method="POST" if dados else "GET")
    with _abrir_com_teto(req, timeout) as resp:
        return resp.read()


def espera_429(e, corpo: str = "") -> float:
    """Segundos até a janela de `operating` liberar.

    Documentação oficial (`limits.md:108-116`): o limite `operating` são **10 cestos
    de 1 min** e o estouro devolve **429 `OPERATION_TIME_LIMIT`**, liberando em
    `operating_reset_at` (ou `Retry-After`, quando vier). Não é backoff nosso.
    """
    hdr = getattr(e, "headers", None)
    if hdr:
        try:
            valor = hdr.get("Retry-After")
            if valor:
                return max(0.0, float(valor))
        except Exception:  # noqa: BLE001
            pass
    m = re.search(r'"operating_reset_at"\s*:\s*(\d+)', corpo or "")
    if m:
        return max(0.0, float(int(m.group(1)) - time.time()))
    return 60.0          # cestos de 1 min: esperar a janela inteira é o certo


def _circ(nome: str | None):
    return rb.circuito(nome) if nome else None


def _registrar_no_circuito(circ, erro) -> None:
    if circ is None or erro is None:
        return
    try:
        rb.aplicar_no_circuito(circ, erro)
    except Exception as e:                        # noqa: BLE001 — disjuntor nunca derruba a chamada
        log.debug("disjuntor %s: falha ao registrar (%s)", getattr(circ, "nome", "?"), e)


def http_json(url: str, body: dict | None = None, headers: dict | None = None,
              tentativas: int = HTTP_TENTATIVAS, rotulo: str = "",
              circuito: str | None = None) -> dict:
    """POST/GET JSON com retry exponencial (API do VibeCode). Erros definitivos propagam.

    Onda 2: a REST do Bitrix NÃO passa mais por aqui (usa `bitrix_cliente.chamar`).
    `circuito` = nome de `rede_bitrix.circuito` da OPERAÇÃO ("vibecode-leitura" ou
    "vibecode-download"): consultado antes e alimentado com o erro classificado
    (`rede_bitrix.classificar`). Falha de item (404/400) não conta contra a fonte (B4);
    nenhuma falha aqui abre o disjuntor de download do Bitrix (B2). URL e corpo de
    erro saem mascarados em log e exceção (S07).
    """
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    dados = json.dumps(body).encode("utf-8") if body is not None else None
    alvo = rotulo or seguranca.mascarar_url(url)
    circ = _circ(circuito)
    if circ is not None and not circ.permitido():
        raise CircuitOpenError(f"circuito {circuito} aberto ({alvo})")
    ultimo = None
    erro_cls = None
    for i in range(1, tentativas + 1):
        try:
            bruto = _requisicao(url, dados, h, HTTP_TIMEOUT)
            resposta = json.loads(bruto.decode("utf-8") or "{}")
            if circ is not None:
                circ.registrar_sucesso()
            return resposta
        except urllib.error.HTTPError as e:
            corpo = ""
            try:
                corpo = e.read().decode("utf-8", "replace")[:300]
            except Exception as e_corpo:          # noqa: BLE001
                # F1-13: sem o corpo, o marcador (ERROR_LOOP_DETECTED, QUERY_LIMIT…)
                # some e a classificação cai só no status HTTP — fica registrado.
                log.warning("HTTP %s: corpo do erro ilegível (%s); classificando só pelo status",
                            e.code, type(e_corpo).__name__)
            ultimo = e
            erro_cls = rb.classificar(e.code, corpo, e)
            corpo_log = seguranca.mascarar(corpo)
            # Bloqueio (ERROR_LOOP_DETECTED / ACCESS_DENIED): retry MANTÉM o bloqueio
            # vivo ("the block clears after the first successful call") — propaga já.
            if e.code == 429 and ("LOOP_DETECTED" in corpo or "ACCESS_DENIED" in corpo):
                _registrar_no_circuito(circ, erro_cls)
                raise RuntimeError(
                    f"bloqueio do portal em {alvo}: {corpo_log[:120]}") from e
            # RATE LIMIT (QUERY_LIMIT_EXCEEDED): retry aqui mantém o bloqueio — sai já.
            if "QUERY_LIMIT_EXCEEDED" in corpo or "Too many requests" in corpo:
                _registrar_no_circuito(circ, erro_cls)
                raise RuntimeError(
                    f"rate limit (QUERY_LIMIT_EXCEEDED) em {alvo}: {corpo_log[:120]}") from e
            # 429 sem os marcadores acima = OPERATION_TIME_LIMIT (`limits.md:108-116`):
            # liberado em `operating_reset_at` → propaga já, com a espera indicada.
            if e.code == 429:
                espera = espera_429(e, corpo)
                _registrar_no_circuito(circ, erro_cls)
                raise RuntimeError(
                    f"429 OPERATION_TIME_LIMIT em {alvo} — espere {espera:.0f}s: "
                    f"{corpo_log[:120]}") from e
            # 4xx (exceto 429) não melhora com retry; falha de ITEM não conta na fonte
            if 400 <= e.code < 500:
                _registrar_no_circuito(circ, erro_cls)
                raise RuntimeError(f"HTTP {e.code} em {alvo}: {corpo_log}") from e
            log.warning("HTTP %s em %s (tentativa %d/%d): %s", e.code, alvo, i, tentativas, corpo_log)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            ultimo = e
            erro_cls = rb.classificar(None, "", e)
            log.warning("falha em %s (tentativa %d/%d): %s", alvo, i, tentativas,
                        seguranca.mascarar(str(e)))
        if i < tentativas:
            time.sleep(HTTP_BACKOFF ** i)
    _registrar_no_circuito(circ, erro_cls)
    raise RuntimeError(f"esgotou {tentativas} tentativas em {alvo}: "
                       f"{seguranca.mascarar(str(ultimo))}")


SUFIXOS_PARCIAIS = (".parcial", ".part", ".tmp")


def _e_parcial(caminho: str | None) -> bool:
    """`.parcial`/`.part`/`.tmp` (inclusive com sufixo único de tmp) nunca contam como baixado."""
    nome = os.path.basename(str(caminho or "")).lower()
    return nome.endswith(SUFIXOS_PARCIAIS) or ".parcial." in nome or ".part." in nome


def _baixar_atomico(url: str, destino: str, tentativas: int = HTTP_TENTATIVAS,
                    headers: dict | None = None, circuito: str | None = None,
                    url_assinada_bitrix: bool = False) -> tuple:
    """Baixa url->destino de forma atômica. Devolve (tamanho, sha256).

    tmp EXCLUSIVO por chamada (`<destino>.parcial.<pid>.<uuid>`, O_EXCL) → tamanho>0 →
    fsync → sha256 calculado no fluxo → `os.replace`. O tmp é removido em qualquer
    falha (nunca sobra `.parcial` que pareça baixado).
    `url_assinada_bitrix=True` (DOWNLOAD_URL do disk.file.get): 401/403/404/410 =
    URL expirada → levanta `FalhaBitrix(URL_EXPIRADA)` na 1ª vez, SEM repetir a mesma
    URL (D3); quem chamou pede um disk.file.get novo. Erro permanente nunca repete.
    """
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    circ = _circ(circuito)
    if circ is not None and not circ.permitido():
        raise CircuitOpenError(f"circuito {circuito} aberto")
    ultimo = None
    erro_cls = None
    for i in range(1, tentativas + 1):
        tmp = f"{destino}.parcial.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            if url_assinada_bitrix:
                rb.limitador_padrao().aguardar()  # host do portal conta na mesma cota
            h = {"User-Agent": "triagem-bitrix/1.0"}
            h.update(headers or {})
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            hs = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as fh, \
                    _abrir_com_teto(urllib.request.Request(url, headers=h), HTTP_TIMEOUT) as r:
                while True:
                    bloco = r.read(65536)
                    if not bloco:
                        break
                    fh.write(bloco)
                    hs.update(bloco)
                    total += len(bloco)
                fh.flush()
                os.fsync(fh.fileno())
            if total == 0:
                raise rb.ErroIntegridade("download vazio (0 byte)")
            os.replace(tmp, destino)
            if circ is not None:
                circ.registrar_sucesso()
            return total, hs.hexdigest()
        except urllib.error.HTTPError as e:
            corpo = ""
            try:
                corpo = e.read().decode("utf-8", "replace")[:300]
            except Exception as e_corpo:          # noqa: BLE001
                log.warning("download HTTP %s: corpo do erro ilegível (%s); "
                            "classificando só pelo status", e.code, type(e_corpo).__name__)
            ultimo = e
            erro_cls = rb.classificar(e.code, corpo, e, download=url_assinada_bitrix)
            log.warning("falha no download (tentativa %d/%d): %s", i, tentativas, erro_cls.detalhe)
            if erro_cls.classe == rb.Classe.URL_EXPIRADA or erro_cls.permanente or not erro_cls.repetir:
                _registrar_no_circuito(circ, erro_cls)
                raise rb.FalhaBitrix(erro_cls) from e
        except rb.ErroIntegridade as e:
            ultimo = e
            erro_cls = rb.classificar(None, "", e)
            log.warning("falha no download (tentativa %d/%d): %s", i, tentativas, e)
        except Exception as e:                       # noqa: BLE001 - nunca derrubar o lote
            ultimo = e
            erro_cls = rb.classificar(None, "", e)
            log.warning("falha no download (tentativa %d/%d): %s", i, tentativas,
                        seguranca.mascarar(str(e)))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if i < tentativas:
            time.sleep(HTTP_BACKOFF ** i)
    _registrar_no_circuito(circ, erro_cls)
    if erro_cls is not None and erro_cls.classe != rb.Classe.DESCONHECIDO:
        raise rb.FalhaBitrix(erro_cls)
    raise RuntimeError(f"não baixou após {tentativas} tentativas: {seguranca.mascarar(str(ultimo))}")


def baixar_arquivo(url: str, destino: str, tentativas: int = HTTP_TENTATIVAS,
                   headers: dict | None = None, circuito: str | None = None,
                   url_assinada_bitrix: bool = False) -> int:
    """Baixa url->destino (atômico: tmp exclusivo + sha256 + os.replace). Retorna o tamanho."""
    return _baixar_atomico(url, destino, tentativas=tentativas, headers=headers,
                           circuito=circuito, url_assinada_bitrix=url_assinada_bitrix)[0]


# --------------------------------------------------------------------------
# Descoberta de credenciais em disco
# --------------------------------------------------------------------------
def ler_chave_bot() -> str:
    """Chave da API do VibeCode via `seguranca.ler_segredo("vibecode_key")` (S06).

    Ordem: env TRIAGEM_VIBECODE_KEY -> /etc/triagem-bitrix/vibecode_key (0600) ->
    legados EXPLÍCITOS de seguranca.py (`<BASE>/vibecode.key`, `/root/.vibecode/api_key`).
    Ausente -> seguranca.SegredoAusente (RuntimeError). O valor nunca vai a log.
    """
    return seguranca.ler_segredo("vibecode_key")


def candidatos_webhook() -> list[str]:
    """Compat: devolve o webhook configurado (0 ou 1 item). NÃO varre arquivo nenhum (S06)."""
    try:
        return [bitrix_cliente.webhook()]
    except seguranca.SegredoAusente:
        return []


def descobrir_webhook() -> str:
    """URL do webhook vinda de `seguranca.ler_segredo("webhook_bitrix")` (via bitrix_cliente).

    Sem varredura de outros projetos, sem /tmp, sem validar "candidatos" pela rede.
    Nada de URL com token em log (S07): só o portal aparece.
    Ausente/inválido -> seguranca.SegredoAusente (RuntimeError).
    """
    u = bitrix_cliente.webhook()
    log.info("webhook configurado (portal=%s)", u.split("/rest/")[0])
    return u


def ler_bot_config() -> dict:
    try:
        with open(ARQ_BOT, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:                           # noqa: BLE001
        log.warning("não li %s (%s); usando padrões", ARQ_BOT, e)
        return {}


# --------------------------------------------------------------------------
# Config dos SINAIS (o TRAÇO é o marcador; funciona como TOGGLE por autor)
# --------------------------------------------------------------------------
# O sinal real do grupo é um TRAÇO:
#   1º traço = abre o lote | 2º traço = fecha | 3º traço = abre de novo (alterna)
# Regra anti-falso-positivo: só é sinal se a mensagem inteira, depois de remover
# espaços/invisíveis, contiver APENAS caracteres de traço (ou '='). Assim
# '044.194.998-34' (CPF) e '21/09/2026' (data) NÃO são sinal.
CONFIG_PADRAO = {
    "_comentario": (
        "sinal_traco: caracteres aceitos como sinal (toggle por autor). "
        "toggle_por_autor: cada pessoa tem seu próprio estado aberto/fechado. "
        "timeout_lote_min: lote aberto sem o traço de fechamento fecha sozinho. "
        "janela_min_fallback: agrupamento por autor+tempo para o que está FORA de "
        "um par de traços (só fallback). "
        "realoc_folga_min: folga (min) para realocar mensagens sem identidade "
        "coladas no corte da barreira (padrão 10). "
        "fonte_leitura: 'api' = API do VibeCode (padrão para chave ausente: tem "
        "failover para o webhook) | 'webhook' = Bitrix im.dialog.messages.get "
        "com cursor (em uso em produção; medição de 09/11/2026: mesmas mensagens "
        "que a API). "
        "modo_traco: 'delimitador' (padrão = uso real; todo traço fecha o bloco de "
        "cima e abre o de baixo, igual ao bot do traço). "
        "Os padrões só valem para chave AUSENTE; valor inválido é erro (ConfigInvalida). "
        "historico_paginas: quantas páginas de 50 mensagens são lidas por ciclo."
    ),
    "sinal_traco": ["-", "–", "—", "="],
    # F1-07 (C3): o padrão segue o USO REAL — config.json de produção e o bot
    # do traço (pergunta_traco/relatorio.decidir_resposta) usam 'delimitador'
    # (pedido do dono, 24/09). Antes o padrão era 'grupo_autor' (decisão de
    # 21/09/2026: abre para o grupo, só fecha com o traço de QUEM ABRIU) e um
    # config sem a chave fazia a esteira divergir do bot em silêncio.
    # O padrão só vale para chave AUSENTE; valor desconhecido é ConfigInvalida.
    # Outros valores: 'grupo_autor', 'grupo', 'autor' (legado), 'delimitador_autor'.
    "modo_traco": "delimitador",
    # vigência do bloco de traço (min): se ninguém fechar, o traço de OUTRA
    # pessoa assume depois desse tempo (evita que um traço órfão trave o grupo).
    # 0 = sem vigência (só quem abriu fecha, para sempre).
    "traco_vigencia_min": JANELA_MIN_PADRAO,
    # TRAÇO NO FIM DO BLOCO (22/09/2026, caso Johanna): o grupo manda
    # ficha -> documentos -> cabeçalho -> TRAÇO. Com isto, um traço que chega
    # SEM bloco aberto FECHA o material pendente (em vez de abrir bloco vazio).
    "traco_no_fim_fecha": True,
    # legado: só é consultado quando 'modo_traco' não existe no config
    "toggle_por_autor": False,
    "timeout_lote_min": 120,
    "janela_min_fallback": JANELA_MIN_PADRAO,
    "realoc_folga_min": REALOC_FOLGA_MIN,
    # F1-07 (C3): produção usa 'webhook' EXPLÍCITO no config.json. O padrão (só
    # para chave AUSENTE) fica 'api' DE PROPÓSITO: o caminho 'api' passa pelo
    # circuit breaker com failover para o webhook; o caminho 'webhook' ainda
    # não tem failover para a API (pendência C2b). Trocar o padrão para
    # 'webhook' só depois desse failover existir.
    "fonte_leitura": "api",
    "historico_paginas": MSG_PAGINAS_HISTORICO,
    # CIRCUIT BREAKER das fontes de leitura: N falhas consecutivas abrem a
    # fonte por T segundos (redireciona para a outra). Ausente/0 usa defaults.
    "circuit_breaker_falhas": 3,
    "circuit_breaker_timeout_s": 300,
    "dialog_id": None,
    "bot_id": BOT_ID_PADRAO,
}

# Variantes completas que teclados de celular produzem (sempre aceitas, além das
# que estiverem em config.sinal_traco). Todas são traço/igual — nunca letra/dígito.
CHARS_TRACO_VARIANTES = set(
    "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\ufe58\ufe63\uff0d\uff3f="
)

ARQ_CONFIG = os.path.join(BASE_DIR, "config.json")


class ConfigInvalida(RuntimeError):
    """F1-06/F1-07: config.json ilegível, não-objeto ou com valor desconhecido.

    Antes, `carregar_config` engolia o erro e voltava a CONFIG_PADRAO
    (`grupo_autor` + `fonte_leitura=api`): a esteira mudava de regra em silêncio
    enquanto o bot seguia em `delimitador`. Agora o ciclo NÃO roda e o erro é
    visível (log + código de saída != 0)."""


FONTES_LEITURA = ("api", "webhook")


def validar_config(cfg: dict) -> dict:
    """Levanta ConfigInvalida para valor desconhecido de fonte_leitura/modo_traco
    ou historico_paginas que não é inteiro positivo. Devolve o próprio cfg."""
    fonte = str(cfg.get("fonte_leitura") if cfg.get("fonte_leitura") is not None
                else CONFIG_PADRAO["fonte_leitura"]).strip().lower()
    if fonte not in FONTES_LEITURA:
        raise ConfigInvalida(f"config.json: fonte_leitura={cfg.get('fonte_leitura')!r} "
                             f"desconhecida (aceitos: {', '.join(FONTES_LEITURA)})")
    bruto = cfg.get("modo_traco")
    if bruto not in (None, "") and _modo_traco_explicito(bruto) is None:
        raise ConfigInvalida(f"config.json: modo_traco={bruto!r} desconhecido "
                             f"(aceitos: {', '.join(MODOS_TRACO)})")
    try:
        pag = int(cfg.get("historico_paginas") or MSG_PAGINAS_HISTORICO)
        if pag <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ConfigInvalida(f"config.json: historico_paginas="
                             f"{cfg.get('historico_paginas')!r} não é inteiro positivo") from None
    return cfg


def carregar_config() -> dict:
    """Lê config.json (cria com os valores padrão se não existir).

    F1-06/F1-07: JSON inválido, arquivo ilegível, raiz não-objeto ou valor
    desconhecido de fonte_leitura/modo_traco levantam `ConfigInvalida` — nunca
    caem em silêncio nos padrões."""
    cfg = dict(CONFIG_PADRAO)
    if not os.path.exists(ARQ_CONFIG):
        try:
            os.makedirs(BASE_DIR, exist_ok=True)
            with open(ARQ_CONFIG, "w", encoding="utf-8") as fh:
                json.dump(CONFIG_PADRAO, fh, ensure_ascii=False, indent=2)
            log.warning("config.json não existia: criei com os padrões em %s", ARQ_CONFIG)
        except OSError as e:
            log.error("não pude criar %s: %s (usando padrões em memória)", ARQ_CONFIG, e)
        return cfg
    try:
        with open(ARQ_CONFIG, "r", encoding="utf-8") as fh:
            lido = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError) as e:   # JSONDecodeError é ValueError
        log.error("config.json ilegível/inválido em %s: %s — ciclo NÃO roda "
                  "(sem cair nos padrões)", ARQ_CONFIG, e)
        raise ConfigInvalida(f"config.json ilegível/inválido ({ARQ_CONFIG}): {e}") from e
    if not isinstance(lido, dict):
        log.error("config.json não é um objeto JSON (%s) — ciclo NÃO roda", type(lido).__name__)
        raise ConfigInvalida(f"config.json não é um objeto JSON ({type(lido).__name__})")
    cfg.update(lido)
    try:
        return validar_config(cfg)
    except ConfigInvalida as e:
        log.error("%s — ciclo NÃO roda (sem cair nos padrões)", e)
        raise


def chars_de_traco(cfg: dict) -> set:
    """Conjunto de caracteres aceitos como sinal = config ∪ variantes conhecidas."""
    chars = set(CHARS_TRACO_VARIANTES)
    v = cfg.get("sinal_traco")
    for item in (v if isinstance(v, list) else [v]):
        if item:
            chars.update(str(item))
    return chars


# --- Modo do traço ---------------------------------------------------------
# DECISÃO DO DONO (21/09/2026) = "grupo_autor": o traço ABRE para o grupo (as
# fotos que a conta que encaminha publica depois entram no bloco) e só FECHA
# com o traço de quem abriu (ou por janela/timeout). Assim o par de traços de
# uma pessoa continua delimitando o kit dela, sem perder as fotos do grupo.
MODOS_TRACO = {
    "grupo_autor": "abre com traço de qualquer um; fecha só com o traço de quem abriu",
    "grupo": "qualquer traço abre/fecha o bloco do grupo",
    "autor": "legado: cada pessoa com o seu próprio abrir/fechar",
    # F1-07: o config.json de produção usa 'delimitador' (pedido do dono, 24/09)
    "delimitador": "todo traço fecha o bloco de cima e abre o de baixo",
    "delimitador_autor": "como delimitador, mas traço de outra pessoa é ignorado",
}


def dialogos(conf: dict, bot_cfg: dict | None = None, arg_dialog=None) -> list[str]:
    """Chats a monitorar — aceita UM ou VÁRIOS.

    Precedência: --dialog-id (CLI) > config.dialog_ids > config.dialog_id > canal do bot > padrão.
    Cada chat é agrupado SEPARADAMENTE (mensagem de um grupo nunca entra em lote de outro).
    """
    if arg_dialog:
        return [arg_dialog] if isinstance(arg_dialog, str) else [str(x) for x in arg_dialog]
    conf = conf or {}
    lista = conf.get("dialog_ids")
    if isinstance(lista, str):
        lista = [x.strip() for x in lista.split(",") if x.strip()]
    if lista:
        return [str(x) for x in lista if str(x).strip()]
    return [str(conf.get("dialog_id") or (bot_cfg or {}).get("canal") or DIALOG_ID_PADRAO)]


def _modo_traco_explicito(valor) -> str | None:
    """Modo canônico para um valor explícito de modo_traco; None se desconhecido."""
    m = re.sub(r"[\s\-]+", "_", str(valor or "").strip().lower())
    if m.startswith("grupo_autor") or m in ("abre_grupo_fecha_autor", "grupo_fecha_autor"):
        return "grupo_autor"
    if m in ("grupo", "qualquer"):
        return "grupo"
    if m in ("autor", "por_autor"):
        return "autor"
    if m in ("delimitador", "delimitador_grupo", "rolante", "janela_movel"):
        return "delimitador"
    if m in ("delimitador_autor", "delimitador_por_autor", "rolante_autor"):
        return "delimitador_autor"
    return None


def modo_traco(cfg: dict) -> str:
    """Modo do toggle do traço: 'grupo_autor' (padrão) | 'grupo' | 'autor'.

    Lê config.modo_traco e, quando ela não existe, deriva do campo legado
    toggle_por_autor (true -> 'autor', false -> 'grupo').
    """
    explicito = _modo_traco_explicito(cfg.get("modo_traco"))
    if explicito:
        return explicito
    if cfg.get("modo_traco") not in (None, ""):
        # F1-07: valor desconhecido não vira toggle legado em silêncio
        raise ConfigInvalida(f"modo_traco={cfg.get('modo_traco')!r} desconhecido "
                             f"(aceitos: {', '.join(MODOS_TRACO)})")
    return "autor" if cfg.get("toggle_por_autor", True) else "grupo"


def e_sinal_traco(texto: str, cfg: dict) -> bool:
    """
    True SOMENTE se a mensagem (sem espaços/invisíveis) for composta apenas por
    caracteres de traço ou '='. É exatamente isso que impede:
      '044.194.998-34'  -> tem dígitos e ponto  -> NÃO é sinal
      '21/09/2026'      -> tem dígitos e barra  -> NÃO é sinal
      '= CPF 044.194.998-34' -> tem letras/dígitos -> NÃO é sinal
    """
    t = texto_limpo(texto or "")
    if not t:
        return False
    chars = chars_de_traco(cfg)
    # remove espaços e caracteres invisíveis (zero-width U+200B/U+FEFF são 'Cf')
    visiveis = [c for c in t if not c.isspace() and unicodedata.category(c) not in ("Cf", "Cc")]
    if not visiveis:
        return False
    return all(c in chars for c in visiveis)


# --------------------------------------------------------------------------
# CPF — extração e normalização
# --------------------------------------------------------------------------
# Casamento amplo: aceita 11 dígitos com ou sem separador (mas nunca dentro de
# um número maior — por isso o lookbehind/lookahead de dígito).
RX_CPF = re.compile(r"(?<!\d)(\d{3})[.\-\s_/]*(\d{3})[.\-\s_/]*(\d{3})[.\-\s_/]*(\d{2})(?!\d)")
# Pistas de contexto
RX_CTX_CPF = re.compile(r"C\.?\s?P\.?\s?F", re.I)
# Pistas de que o número NÃO é CPF (telefone/celular/contato reserva/NIS/RG/CEP).
# ⚠️ 21/09/2026: 'Contatos reservas: 45265993100' passava no DV e VIRAVA O CPF DO
# LOTE (o telefone 67 99811-6242 virou a âncora de identidade) — por isso a
# lista agora pega o PLURAL ('contatos', 'reservas') e 'telefones/whatsapp/zap'.
RX_CTX_NAO_CPF = re.compile(
    r"\b(fone|fone[s]?|fon|tel|telefone[s]?|cel|celular[e]?s?|whats|whatsapp|zap|"
    r"nis|rg|cep|contatos?|contato\s*[12]|reservas?)\b", re.I)
RX_SEPARADOR = re.compile(r"[.\-_/]")

# (PONTO 2.4, 14/10/2026) PAPEL de quem o rótulo nomeia:
#   "cliente"     -> a pessoa do caso (GESTANTE/MENOR/CLIENTE/SEGURADA/…)
#   "responsavel" -> quem responde por ela (MÃE/PAI/RESP/GENITOR/…)
# O pareamento nome×CPF só une papéis COMPATÍVEIS — o nome da MENOR nunca fica
# pareado com o CPF DA MÃE (era o produto cartesiano de `_acumular`, e o nome
# certo nascia amarrado ao CPF errado).
RX_CTX_RESP_CPF = re.compile(
    r"\b(?:m[ãa]e|pai|resp(?:ons[áa]vel)?|genitor[ao]?|acompanhante|representante)\b",
    re.I)
# Rótulo de RESPONSÁVEL com o nome NA MESMA LINHA ("MÃE RESPONSAVEL: PATRICIA",
# "NOME DA MAE: X", "= PAI DO MENOR: X"). O capturado é o NOME depois do rótulo
# — nunca o rótulo em si, que é ruído (`nome_regras.RE_RESPONSAVEL`).
RX_ROTULO_RESP_NOME = re.compile(
    r"(?:NOME\s+(?:DA|DO)\s+)?"
    r"(?:M[ÃA]E|PAI|GENITOR[AO]?|ACOMPANHANTE|REPRESENTANTE|RESP(?:ONS[ÁA]VEL)?)"
    r"(?:\s+(?:RESPONS[ÁA]VEL|D[AO]\s+(?:MENOR|CLIENTE|BENEFICI[ÁA]RIO)))?"
    r"\s*\**\s*:\s*\**\s*([^\n=*]+)", re.I)
# Rótulo de responsável SOZINHO numa linha ("= MÃE DA MENOR", "MÃE RESPONSAVEL:")
# — o nome vem na LINHA SEGUINTE.
RX_ROTULO_RESP_SOZINHO = re.compile(
    r"^\s*=?\s*"
    r"(?:NOME\s+(?:DA|DO)\s+)?"
    r"(?:M[ÃA]E|PAI|GENITOR[AO]?|ACOMPANHANTE|REPRESENTANTE|RESP(?:ONS[ÁA]VEL)?)"
    r"(?:\s+(?:RESPONS[ÁA]VEL|D[AO]\s+(?:MENOR|CLIENTE|BENEFICI[ÁA]RIO)))?"
    r"\s*:?\s*$", re.I)


def cpf_normalizar(bruto: str) -> str | None:
    """Normaliza um CPF bruto para 11 dígitos. None se não tiver 11 dígitos.

    F1-09: casca sobre `cpf_util.normalizar` (fonte única). Aceita o grupo de
    dígitos do RX_CPF com qualquer separador (o regex já isolou o número)."""
    return cpf_util.normalizar(re.sub(r"\D", "", bruto or ""))


def cpf_plausivel(cpf: str) -> bool:
    """Dígitos verificadores oficiais (F1-09: `cpf_util.valido`, fonte única).

    É o que separa CPF de TELEFONE e de CPF digitado errado. Vale COM ou SEM
    pontuação: '123.456.789-00' com DV errado não é CPF.
    """
    if not cpf or len(str(cpf)) != 11:
        return False
    return cpf_util.valido(str(cpf))


def cpf_formatado(cpf: str) -> str:
    return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}" if cpf and len(cpf) == 11 else (cpf or "")


def extrair_cpfs_com_papel(texto: str, invalidos_out: list | None = None
                           ) -> list[tuple[str, str | None]]:
    """(PONTO 2.4) Todos os CPFs do texto, na ordem, COM o papel de cada um.

    Devolve [(cpf, papel), …] onde papel é "responsavel" quando o contexto
    imediato diz MÃE/PAI/RESP/GENITOR ("= CPF MÃE DA MENOR: 900.849.399-31")
    e None quando não há rótulo (conta como CLIENTE no pareamento).

    F1-09: só aceita CPF com dígito verificador válido (com ou sem pontuação);
    rotulado/pontuado com DV errado vai para `invalidos_out` (revisão).
    Telefone rotulado ('Fone 77704710011') nunca é CPF.
    """
    if not texto:
        return []
    vistos: list[tuple[str, str | None]] = []
    for m in RX_CPF.finditer(texto):
        bruto = "".join(m.groups())
        cpf = cpf_normalizar(bruto)
        if not cpf or any(c == cpf for c, _ in vistos):
            continue
        contexto = texto[max(0, m.start() - 32):m.start()]
        rotulado_cpf = bool(RX_CTX_CPF.search(contexto))
        rotulado_outro = bool(RX_CTX_NAO_CPF.search(contexto[-14:]))
        tem_sep = bool(RX_SEPARADOR.search(m.group(0)))

        aceitar = False
        if rotulado_outro and not rotulado_cpf:
            aceitar = False                      # 'Fone 77704710011' -> nunca
        elif cpf_plausivel(cpf):
            # F1-09: DV obrigatório COM ou SEM pontuação. Antes, separador ou
            # rótulo "CPF" bastavam: um dígito errado no cabeçalho virava âncora,
            # CONFLITO, barreira e chave de CRM.
            aceitar = True
        elif (rotulado_cpf or tem_sep) and invalidos_out is not None:
            if cpf not in invalidos_out:
                invalidos_out.append(cpf)        # parece CPF, DV errado -> revisão
        if aceitar:
            papel = "responsavel" if RX_CTX_RESP_CPF.search(contexto) else None
            vistos.append((cpf, papel))
    return vistos


def extrair_cpfs_invalidos(texto: str) -> list[str]:
    """F1-09: números com cara de CPF (rotulados "CPF" ou com pontuação) cujo DV
    não confere. Não são âncora, barreira, CONFLITO nem chave de CRM — viram só
    ocorrência de revisão (`lote["cpfs_invalidos"]` + motivo)."""
    invalidos: list[str] = []
    extrair_cpfs_com_papel(texto, invalidos)
    return invalidos


def extrair_cpfs(texto: str) -> list[str]:
    """
    Todos os CPFs (11 dígitos) do texto, na ordem, sem repetição.

    Um candidato só é aceito se cumprir ao menos UMA das condições:
      F1-09: dígito verificador válido é OBRIGATÓRIO (com ou sem pontuação);
      rótulo de telefone/RG/NIS sem rótulo "CPF" nunca é CPF.

    Isso é o que impede 'Fone 77704710011' (telefone) de virar CPF.

    (PONTO 2.4) virou casca fina sobre `extrair_cpfs_com_papel` — o contrato
    público continua a mesma lista de CPFs (testar_cpf depende dele).
    """
    return [c for c, _ in extrair_cpfs_com_papel(texto)]


# --------------------------------------------------------------------------
# Texto / nome
# --------------------------------------------------------------------------
def texto_limpo(texto: str) -> str:
    """Remove prefixo '[B]Autor:[/B]' e ruído de 'mensagem apagada'."""
    t = RX_REMETENTE.sub("", texto or "", count=1)
    t = t.replace("[B]", "").replace("[/B]", "")
    t = RX_APAGADA.sub("", t)
    return t.strip()


def remetente_real(texto: str, fallback: str = "") -> str:
    """O conector WhatsApp põe o remetente real em '[B]Nome:[/B]' (author_id é o canal)."""
    m = RX_REMETENTE.match(texto or "")
    return m.group(1).strip() if m else (fallback or "")


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _sem_cpf_na_linha(s: str) -> str:
    """'GILMAR TESTE DA SILVA CPF 570.418.035-38' → 'GILMAR TESTE DA SILVA'.

    O grupo manda nome e CPF na MESMA linha (formato real 06/10/2026, lote
    LOTE-SIG-20261006-2249). O nome sai inteiro; o CPF fica com quem extrai âncora.
    Casca sobre `nome_regras.RX_CPF_NO_FIM` (contrato de normalização v1).
    """
    from nome_regras import RX_CPF_NO_FIM
    return RX_CPF_NO_FIM.sub("", s or "").strip(" *")


# F1-05: saudação/frase de serviço nunca é nome de pessoa ("bom dia",
# "Segue documentos", "Boa tarde, segue"). nome_regras.PALAVRAS_NAO_NOME não tem
# essas palavras (pendência para o dono de nome_regras); o filtro fica aqui,
# na camada de SINAL, que é onde ele cortava kit.
_CONVERSA_INICIO = frozenset({
    "BOM", "BOA", "BONS", "OLA", "OI", "SEGUE", "SEGUEM", "OBRIGADO", "OBRIGADA",
    "OBG", "BLZ", "OK", "TUDO", "ATENCIOSAMENTE", "PREZADO", "PREZADA", "AGUARDO",
    "FAVOR", "POR", "ENVIANDO", "ENVIO", "MANDANDO", "VOU", "ESTOU", "TO",
})
_CONVERSA_QUALQUER = frozenset({
    "SEGUE", "SEGUEM", "DOCUMENTO", "DOCUMENTOS", "DOCS", "DOC", "ANEXO", "ANEXOS",
    "ARQUIVO", "ARQUIVOS", "FOTO", "FOTOS", "DIA", "TARDE", "NOITE",
})


def _e_frase_conversa(s: str) -> bool:
    """F1-05: True se o candidato é saudação/frase de serviço, não nome."""
    toks = re.findall(r"[A-Z]+", _sem_acento(s or "").upper())
    if not toks:
        return False
    return toks[0] in _CONVERSA_INICIO or any(t in _CONVERSA_QUALQUER for t in toks)


def _e_nome_valido(s: str) -> bool:
    """Valida candidato a nome do lote — camada de SINAL (1 palavra é sinal).

    Casca sobre `nome_regras.validar_nome(..., exigir_completo=False)`: o corpo
    (tamanho, parece_ruido, PALAVRAS_NAO_NOME, dígito/razão de letras) vive no
    contrato. `exigir_completo=False` aqui é de PROPÓSITO — barreira/janela/C3 e
    o caso 2.5 dependem de 1 palavra virar sinal de identidade. Quem impede
    1 palavra de virar NOME DE CLIENTE é a fronteira do job (montar_job/ponte).
    """
    from nome_regras import normalizar_nome_candidato, validar_nome
    # NORMALIZAR ANTES de validar: validar_nome() reprova qualquer valor que
    # contenha PALAVRAS_NAO_NOME ('NOME', 'CLIENTE', 'MENOR'...). Quando a ficha
    # do grupo vem rotulada ("= NOME DO CLIENTE Lucas Augusto Gomes de Oliveira"),
    # o prefixo só cai no normalizador — validar o texto cru recusava o cliente
    # inteiro. O strip de borda fica: é ele que trata "  :*-\t".
    canonico = normalizar_nome_candidato((s or "").strip(" :*-\t"))
    if not canonico or _e_frase_conversa(canonico):
        return False
    return validar_nome(canonico, exigir_completo=False)


def _extrair_nomes_responsavel(texto: str) -> list[str]:
    """(PONTO 2.4) Nomes de RESPONSÁVEL (mãe/pai/resp/genitor) da mensagem.

    O grupo escreve "MÃE RESPONSAVEL: PATRICIA", "NOME DA MAE: X",
    "= PAI DO MENOR: X", ou o rótulo numa linha e o nome na de baixo
    ("= MÃE DA MENOR" / "= PATRICIA TESTE"). O capturado é o NOME depois do
    rótulo — o rótulo em si é ruído (`nome_regras.RE_RESPONSAVEL`) e nunca
    entra aqui.

    Estes nomes NÃO vão para `msg["nomes"]`: não podem virar `lote["nome"]`
    nem disparar a barreira de identidade (mesma família). Ficam só em
    `msg["nomes_papeis"]`, marcados como "responsavel".
    """
    t = texto_limpo(texto)
    if not t:
        return []
    achados: list[str] = []
    for m in RX_ROTULO_RESP_NOME.finditer(t):
        cand = _sem_cpf_na_linha(m.group(1).strip(" *"))
        if _e_nome_valido(cand):
            achados.append(cand)
    # rótulo SOZINHO numa linha -> o nome está na linha seguinte
    linhas = t.splitlines()
    for i, ln in enumerate(linhas[:-1]):
        if not RX_ROTULO_RESP_SOZINHO.search(ln):
            continue
        cand = linhas[i + 1].strip(" *")
        cand = re.sub(r"^\s*=\s*", "", cand).strip(" *")
        cand = _sem_cpf_na_linha(cand)
        if _e_nome_valido(cand):
            achados.append(cand)
    saida: list[str] = []
    for n in achados:
        n = re.sub(r"\s+", " ", n).strip()
        if not n or tem_marcador_fim(n):
            continue
        if _sem_acento(n).upper() not in {_sem_acento(x).upper() for x in saida}:
            saida.append(n)
    return saida


def extrair_nomes_com_papel(texto: str) -> tuple[list[str], dict, dict]:
    """(PONTO 2.4) Extrai os nomes SEPARANDO o papel do rótulo.

    Devolve `(nomes_cliente, nomes_papeis, nomes_rotulos)`:
      · nomes_cliente — só quem pode ser `lote["nome"]` (é o que vai para
        `msg["nomes"]` — o contrato público não muda);
      · nomes_papeis  — {nome: "cliente"|"responsavel"}, para o pareamento
        nome×CPF e para a barreira saber que mãe/pai não é intruso;
      · nomes_rotulos — {nome: rótulo bruto ("MENOR", "GESTANTE", …)} para o
        desempate em que a MENOR vence o nome sem rótulo da ficha.

    Nome de RESPONSÁVEL nunca entra em nomes_cliente.
    """
    t = texto_limpo(texto)
    if not t:
        return [], {}, {}
    # PONTO 2.5 (14/10/2026): NÃO retornar [] só porque a mensagem tem marcador
    # de fim — "= MARIA\n= CPF …\nPASTA FINALIZADA" perdia o nome da cliente e o
    # lote saía SEM_CPF/nome=None. O marcador agora é filtrado candidato a
    # candidato no dedupe (abaixo), e o marcador em si nunca vira nome.
    nomes: list[str] = []
    rotulos: dict[str, str] = {}

    # 1) rótulos explícitos: "=GESTANTE:X", "=MENOR: X", "=CLIENTE:X"
    #    e também SEM dois-pontos ("= MENOR MATHEUS TRAJANO MACHADO DA SILVA" — como
    #    o grupo escreve de verdade; sem isso o nome do menor se perdia).
    #    (PONTO 2.4) o rótulo é guardado em `rotulos` — é o que faz a MENOR
    #    vencer o nome sem rótulo da ficha no desempate do lote.
    for m in re.finditer(
            r"=\s*(GESTANTE|MENOR|CLIENTE|SEGURADA|TITULAR|BENEFICI[ÁA]RIO)\b\s*:?\s*([^\n=]+)",
            t, re.I):
        cand = m.group(2).strip(" *")
        if _e_nome_valido(cand):
            nomes.append(cand)
            rotulos[cand] = m.group(1).upper()

    # 1b) rótulo SEM '=' mas com dois-pontos — o formato que a equipe manda de
    #     verdade ("-- CLIENTE : *NAIARA…* AUX…", "*LOAS :ROSANA FICTICIA…*").
    #     Corta em '*' para não engolir o que vem depois do nome na mesma linha.
    for m in re.finditer(
            r"[\s•\-\*]*(?:CLIENTE|LOAS|BPC|BENEFICI[ÁA]RIO)\s*\**\s*:\s*\**\s*([^\n=•*]+)",
            t, re.I):
        cand = m.group(1).strip(" *")
        if _e_nome_valido(cand):
            nomes.append(cand)

    # 2) FICHA: "Nome: Fulano" — inclui o template do grupo com bullet/markdown
    #    ('• **Nome completo:', '*Nome completo*: X', '• **Nome completo:**X').
    #    Valor vazio na linha = o nome está na LINHA SEGUINTE (template 10/10/2026,
    #    lote LOTE-SIG-20261010-1757 — era assim que o nome se perdia).
    rx_ficha_nome = re.compile(
        r"^[\s•\-\*]*(?:\*\*)?Nome"
        r"(?:\s+\*{0,2}\s*(?:completo|d[ao]s?\s+cliente)\*{0,2})?"
        r"\s*\**\s*:\s*\**\s*([^\n]*)$",
        re.I | re.M)
    linhas_t = t.splitlines()
    for m in rx_ficha_nome.finditer(t):
        cand = m.group(1).strip(" *")
        # contato COMPARTILHADO do WhatsApp: o "Nome:" ali é o apelido salvo no
        # celular do operador ('Vidabbzao') — nunca é o nome do cliente.
        antes = t[max(0, m.start() - 250):m.start()]
        if re.search(r"Contatos?\s+compartilhado", antes, re.I):
            continue
        if not cand:
            proxima = t.count("\n", 0, m.end())
            for ln in linhas_t[proxima + 1:]:
                cand = ln.strip(" *")
                if cand:
                    break
        cand = _sem_cpf_na_linha(cand)      # "Nome: X CPF 123…" → só o X
        if _e_nome_valido(cand):
            nomes.append(cand)

    # 3) linha '= NOME' / '=NOME' cujo token não é palavra-chave
    for m in re.finditer(r"^\s*=\s*([^\n=]+)$", t, re.M):
        cand = m.group(1).strip(" *")
        if _e_nome_valido(cand):
            nomes.append(cand)

    # 4) primeira linha "solta" (ex.: "DAYANA TESTE MATOS" antes do '= CPF')
    #    F1-05: só quando a mensagem é cabeçalho (tem CPF ou linha '='). Fala
    #    solta ("bom dia", "Segue documentos") não é cabeçalho e não vira nome.
    primeira = t.splitlines()[0].strip(" *") if t.splitlines() else ""
    e_cabecalho = bool(RX_CPF.search(t)) or bool(re.search(r"^\s*=", t, re.M))
    if (e_cabecalho and _e_nome_valido(_sem_cpf_na_linha(primeira))
            and ":" not in primeira):
        nomes.append(_sem_cpf_na_linha(primeira))

    # 5) "GILMAR TESTE DA SILVA CPF 570.418.035-38" — nome ANTES do CPF em
    #    QUALQUER linha (formato real do grupo, 06/10/2026). É o único caso em que
    #    nome e CPF chegam juntos: dá para parear sem risco de cruzar clientes.
    for m in re.finditer(
            r"^\s*\**\s*([^\n=:][^\n:]*?)\s+\*{0,2}CPF\*{0,2}\s*[:：]?\s*[\d.\-/]{11,14}\s*$",
            t, re.M | re.I):
        cand = m.group(1).strip(" *")
        if _e_nome_valido(cand):
            nomes.append(cand)

    # dedupe preservando ordem (PONTO 2.5: descarta candidato que É/TEM o
    # marcador de fim — "PASTA FINALIZADA" não é nome de cliente).
    # AQUI o candidato passa pelo normalizador: os passos acima só VALIDAM com
    # ele (_e_nome_valido), mas guardavam a forma suja. Era o que levava
    # "NOME DO CLIENTE Lucas Augusto Gomes de Oliveira" para o estado do lote —
    # e o lote, com nome inválido, parava na revisão com 3 arquivos prontos.
    from nome_regras import normalizar_nome_candidato
    saida: list[str] = []
    for n in nomes:
        n = normalizar_nome_candidato(n) or re.sub(r"\s+", " ", n).strip()
        if not n or tem_marcador_fim(n):
            continue
        if _sem_acento(n).upper() not in {_sem_acento(x).upper() for x in saida}:
            saida.append(n)

    # (PONTO 2.4) nome de RESPONSÁVEL não é nome de cliente. As regras de cima
    # já não pegam o rótulo ("MÃE RESPONSAVEL: PATRICIA" é ruído), mas pegam o
    # valor dele quando ele vem sozinho na linha de baixo ("= MÃE DA MENOR" /
    # "= PATRICIA TESTE" — a regra 3 levantaria "PATRICIA TESTE" como cliente).
    resp_norm = {normalizar_nome(n) for n in _extrair_nomes_responsavel(texto)}
    if resp_norm:
        saida = [n for n in saida if normalizar_nome(n) not in resp_norm]
        for k in list(rotulos):
            if normalizar_nome(k) in resp_norm:
                del rotulos[k]

    papeis = {n: "cliente" for n in saida}
    for n in _extrair_nomes_responsavel(texto):
        papeis[n] = "responsavel"
    return saida, papeis, rotulos


def extrair_nomes(texto: str) -> list[str]:
    """Nomes prováveis no cabeçalho curto e/ou na FICHA. NÃO é identificador forte.

    (PONTO 2.4) Devolve só nomes de CLIENTE — nome de responsável (mãe/pai)
    fica de fora; ver `extrair_nomes_com_papel`. O contrato público continua a
    mesma lista de nomes.
    """
    return extrair_nomes_com_papel(texto)[0]


def normalizar_nome(n: str) -> str:
    return re.sub(r"\s+", " ", _sem_acento(n or "").upper()).strip()


def tem_marcador_fim(texto: str) -> bool:
    t = _sem_acento(texto_limpo(texto)).lower()
    return any(_sem_acento(m).lower() in t for m in MARCADORES_FIM)


def e_ruido_sistema(texto: str, tem_arquivo: bool) -> bool:
    """
    True para notificação automática do Bitrix e para lixo de conector.

    Só é ruído quem NÃO traz arquivo: um anexo nunca é descartado.
    """
    if tem_arquivo:
        return False
    t = (texto or "").strip()
    if not t:
        return True
    corpo = texto_limpo(t)
    # padrões são checados no texto cru E no texto sem o prefixo '[B]Autor:[/B]'
    for rx in RX_RUIDO_SISTEMA:
        if rx.search(t) or (corpo and rx.search(corpo)):
            return True
    # o conector prefixa toda fala humana com '[B]Autor:[/B]'
    sem_prefixo = not RX_REMETENTE.match(t)
    if sem_prefixo:
        return True
    # sobra só emoji / marcador vazio depois de limpar
    if re.fullmatch(r"[\s❌👆🏻👇🔹*_\-.\u200b]*", corpo or ""):
        return True
    # cartões de contato/CLIENTE não identificam o cliente (regra do dono: nome não é âncora)
    if re.search(r"^\[B\]Contato compartilhado", corpo, re.I):
        return True
    return False


def tem_nome_cabecalho(texto: str) -> bool:
    """Há uma linha de cabeçalho '= ALGO' cujo ALGO é nome de pessoa?"""
    t = texto_limpo(texto)
    if not t:
        return False
    for m in re.finditer(r"=\s*(?:GESTANTE|MENOR|CLIENTE|SEGURADA)\s*:\s*([^\n=]+)", t, re.I):
        if _e_nome_valido(m.group(1).strip(" *")):
            return True
    for m in re.finditer(r"^\s*=\s*([^\n=]+)$", t, re.M):
        if _e_nome_valido(m.group(1).strip(" *")):
            return True
    return False


def e_abertura_ou_ficha(texto: str) -> bool:
    t = _sem_acento(texto_limpo(texto)).upper()
    return _sem_acento(ABERTURA).upper() in t or CAMPO_FICHA in t


# --------------------------------------------------------------------------
# Mensagens
# --------------------------------------------------------------------------
def _parse_data(s) -> _dt.datetime | None:
    """Texto ISO-8601 (ou um `datetime` já pronto) -> datetime; inválido -> None.

    G-01: `lote["fim"]`/`msg["ts"]` já chegam como `datetime` (de `_acumular`); antes
    isso caía no except e todo lote parecia antigo (o ciclo nunca baixava)."""
    if isinstance(s, _dt.datetime):
        return s
    try:
        return _dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:                                # noqa: BLE001
        return None


def dia_local(ts: _dt.datetime | None) -> _dt.date | None:
    """Data no FUSO LOCAL da máquina (-03:00 no Brasil) — é o 'hoje' do operador.

    A API do VibeCode devolve o 'date' no fuso do portal (+03:00, Moscou), então
    comparar o texto da data direto daria o dia errado perto da meia-noite.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts.astimezone().date()


def file_ids_da_mensagem(msg: dict) -> list[str]:
    """IDs de arquivo da mensagem — a API do VibeCode usa params.fileId (lista);
    o webhook antigo usava params.FILE_ID. Aceitamos as duas grafias."""
    params = msg.get("params") or {}
    ids: list[str] = []
    for chave in CHAVES_FILE_ID:
        for x in (params.get(chave) or []):
            s = str(x)
            if s and s not in ids:
                ids.append(s)
    return ids


def classificar_mensagem(msg: dict, mapa_usuarios: dict, cfg: dict | None = None,
                         mapa_arquivos: dict | None = None) -> dict:
    """Converte uma mensagem da API no dicionário interno usado pelo agrupador.

    Aceita os DOIS formatos de mensagem: o da API do VibeCode (authorId +
    params.fileId, formato atual) e o do webhook antigo (author_id +
    params.FILE_ID), para os testes embutidos continuarem valendo.
    """
    cfg = cfg or CONFIG_PADRAO
    mapa_arquivos = mapa_arquivos or {}
    texto = msg.get("text") or ""
    file_ids = file_ids_da_mensagem(msg)
    uid = msg.get("author_id")
    if uid is None:
        uid = msg.get("authorId")
    nome_bitrix = (mapa_usuarios.get(uid) or {}).get("name", "") if uid else ""
    remetente = remetente_real(texto, nome_bitrix) or nome_bitrix or "desconhecido"

    # REAÇÃO (>>[B]Mensagem de reação[/B]): NÃO é conteúdo de triagem. Não vira
    # lote, não é sinal e NÃO entra na extração de NOME/CPF.
    reacao = bool(RX_REACAO.search(texto))

    # sinal (traço): o texto do traço NÃO entra na extração de NOME/CPF
    sinal = e_sinal_traco(texto, cfg) and not reacao
    limpo = "" if (sinal or reacao) else texto_limpo(texto)
    tem_arquivo = bool(file_ids)
    # (PONTO 2.4) CPF e nome saem COM o papel do rótulo ("cliente" /
    # "responsavel"). `cpfs` e `nomes` mantêm o contrato público: todos os CPFs
    # aceitos e só os nomes de CLIENTE (nome de mãe/pai não pode virar nome de
    # lote nem disparar a barreira — ele fica só em `nomes_papeis`).
    cpfs_invalidos: list[str] = []
    pares_cpfs = extrair_cpfs_com_papel(limpo, cpfs_invalidos) if limpo else []
    cpfs = [c for c, _ in pares_cpfs]
    cpfs_papeis = {c: p for c, p in pares_cpfs if p}
    nomes, nomes_papeis, nomes_rotulos = extrair_nomes_com_papel(limpo) if limpo else ([], {}, {})
    ruido = e_ruido_sistema(texto, tem_arquivo) and not sinal
    if reacao:
        ruido = True

    # metadados dos anexos que a própria resposta da API já traz (nome/tamanho)
    arquivos = []
    for fid in file_ids:
        info = mapa_arquivos.get(str(fid)) or mapa_arquivos.get(fid) or {}
        arquivos.append({
            "file_id": str(fid),
            "nome": info.get("nome") or info.get("name"),
            "tamanho": info.get("tamanho") or info.get("size"),
            "tipo": info.get("tipo") or info.get("type"),
        })

    return {
        "id": msg.get("id"),
        "autor_id": uid,
        "autor": remetente,
        "data": msg.get("date"),
        "ts": _parse_data(msg.get("date")),
        "texto": limpo,
        "texto_bruto": texto,
        "file_ids": file_ids,
        "arquivos_info": arquivos,
        "cpfs": cpfs,
        "cpfs_papeis": cpfs_papeis,
        "cpfs_invalidos": cpfs_invalidos,    # F1-09: só revisão, nunca âncora
        "nomes": nomes,
        "nomes_papeis": nomes_papeis,
        "nomes_rotulos": nomes_rotulos,
        "sinal": sinal,
        "reacao": reacao,
        "e_identificador": bool(cpfs) or e_abertura_ou_ficha(limpo) or tem_nome_cabecalho(limpo),
        "e_fim": tem_marcador_fim(limpo),
        "sistema": bool(msg.get("isSystem")) or uid == 0 or False,
        "apagada": bool(RX_APAGADA.search(texto)),
        "ruido": ruido,
    }


def _mapa_usuarios(res: dict) -> dict:
    return {u.get("id"): u for u in (res.get("users") or []) if isinstance(u, dict)}


def _mapa_arquivos(res: dict) -> dict:
    """data.files[] da resposta da API -> {file_id(str/int): {nome, tamanho, tipo}}."""
    saida = {}
    for f in (res.get("files") or []):
        if not isinstance(f, dict) or f.get("id") is None:
            continue
        meta = {"nome": f.get("name"), "tamanho": f.get("size"), "tipo": f.get("type")}
        saida[str(f["id"])] = meta
        saida[f["id"]] = meta
    return saida


# --------------------------------------------------------------------------
# FONTE NOVA (primária): API do VibeCode
# --------------------------------------------------------------------------
def api_get(chave: str, caminho: str, params: dict | None = None,
            rotulo: str = "", tentativas: int = HTTP_TENTATIVAS,
            circuito: str = "vibecode-leitura") -> dict:
    """GET autenticado (X-Api-Key) na API do VibeCode. Devolve o JSON cru.

    `circuito`: "vibecode-leitura" (mensagens/eventos) ou "vibecode-download" (arquivos)."""
    url = VIBECODE_BASE + caminho
    if params:
        limpos = {k: v for k, v in params.items() if v is not None}
        if limpos:
            url += "?" + urllib.parse.urlencode(limpos)
    return http_json(url, None, headers={"X-Api-Key": chave},
                     rotulo=rotulo or caminho, tentativas=tentativas, circuito=circuito)


def ler_pagina_mensagens(chave: str, dialog_id: str, last_id: int | None = None,
                         first_id: int | None = None,
                         limite: int = MSG_PAGINA) -> dict:
    """Uma página de mensagens (a API devolve no máximo 50, mais novas primeiro)."""
    params = {"limit": min(int(limite), MSG_PAGINA)}
    if last_id:
        params["lastId"] = int(last_id)
    if first_id:
        params["firstId"] = int(first_id)
    r = api_get(chave, f"/v1/chats/{urllib.parse.quote(str(dialog_id))}/messages",
                params, rotulo="chats.messages")
    if not isinstance(r, dict) or not r.get("success"):
        erro = (r or {}).get("error") or {}
        raise RuntimeError(f"API do VibeCode recusou a leitura de mensagens: "
                           f"{erro.get('code')} {erro.get('message')}")
    return r.get("data") or {}


def buscar_mensagens_api(chave: str, dialog_id: str,
                         max_paginas: int = MSG_PAGINAS_HISTORICO,
                         cfg: dict | None = None, ate_id: int | None = None,
                         info: dict | None = None) -> list[dict]:
    """FONTE PRIMÁRIA — lê o grupo pela API do VibeCode, em ordem CRONOLÓGICA.

    A 1ª página já traz as mensagens MAIS NOVAS (é isso que o webhook não
    enxergava). As páginas seguintes descem o histórico com ?lastId=<menor id
    da página anterior>, até a página vir incompleta (ou acabar max_paginas).

    Devolve a MESMA estrutura interna que o resto do serviço já usa
    (classificar_mensagem), para CPF/traço/lotes/download seguirem sem reescrita.

    F1-01 (incremental): com `ate_id` (cursor do chat) desce só até ver um id
    <= ate_id e devolve apenas os ids > ate_id. O `firstId` da VibeCode existe em
    `ler_pagina_mensagens`, mas a ORDEM/semântica dele nunca foi medida (se devolver
    as 50 MAIS NOVAS acima do cursor, as do meio sumiriam sem aviso); o `lastId`
    descendente é o comportamento já verificado. Se as páginas acabarem antes de
    alcançar o cursor -> `info["lacuna"] = True` (o ciclo não avança o cursor).
    """
    cfg = cfg or CONFIG_PADRAO
    alcancou = ate_id is None
    todas: dict[int, dict] = {}
    usuarios: dict = {}
    arquivos: dict = {}
    last_id = None
    paginas = 0
    for pagina in range(max(1, int(max_paginas))):
        dados = ler_pagina_mensagens(chave, dialog_id, last_id=last_id)
        msgs = dados.get("messages") or []
        usuarios.update(_mapa_usuarios(dados))
        arquivos.update(_mapa_arquivos(dados))
        paginas += 1
        novos = 0
        for m in msgs:
            mid = m.get("id")
            if mid is not None and mid not in todas:
                todas[mid] = m
                novos += 1
        ids = [m["id"] for m in msgs if isinstance(m.get("id"), int)]
        log.debug("API: página %d -> %d itens (%d novos)", pagina, len(msgs), novos)
        if ate_id is not None and ids and min(ids) <= int(ate_id):
            alcancou = True
            break
        # incremental: página curta no meio NÃO encerra (F1-11) — só vazio/sem id novo
        if (ate_id is None and len(msgs) < MSG_PAGINA) or not ids or novos == 0:
            alcancou = True                 # fim do histórico
            break
        menor = min(ids)
        if last_id is not None and menor >= last_id:
            alcancou = True
            break
        last_id = menor
    if not alcancou and info is not None:
        info["lacuna"] = True
    if ate_id is not None:
        todas = {k: v for k, v in todas.items() if isinstance(k, int) and k > int(ate_id)}
    log.info("API do VibeCode: %d mensagens em %d página(s)", len(todas), paginas)
    ordenadas = [todas[k] for k in sorted(todas)]
    return [classificar_mensagem(m, usuarios, cfg, arquivos) for m in ordenadas]


# --------------------------------------------------------------------------
# FONTE ANTIGA (só DIAGNÓSTICO — --fonte-webhook): webhook do Bitrix
# --------------------------------------------------------------------------
# Mantida para provar/documentar que ela é um SUBCONJUNTO do grupo: medido em
# 21/09/2026 no mesmo instante -> API 386 msgs x webhook 234 (0 ids exclusivos
# do webhook; perdia 62 das 85 mensagens do dia, justamente os kits/anexos e os
# traços). NÃO é fonte primária.
def _buscar_mensagens_desde(dialog_id: str, first_id: int, cfg: dict,
                            info: dict | None = None,
                            max_paginas: int = F1_MAX_PAGINAS_INCREMENTAL) -> list[dict]:
    """F1-01 — leitura INCREMENTAL pelo webhook: só mensagens com id > first_id.

    Doc oficial (im.dialog.messages.get): FIRST_ID devolve as mensagens com id MAIOR;
    a próxima página usa o MAIOR id recebido. A resposta pode trazer MAIS que LIMIT
    (mensagens não lidas), então a parada é "veio vazio / nenhum id novo", nunca
    len == LIMIT. FIRST_ID inexistente (mensagem apagada) devolve [] — por isso,
    quando a 1ª página vem vazia, uma SONDA da página mais nova confere se há id
    maior que o cursor; se houver, o cursor está inválido -> lê descendo até o
    cursor (LAST_ID) e, se não alcançar, marca LACUNA em `info`.
    """
    info = info if info is not None else {}
    info["first_id"] = int(first_id)
    todas: dict[int, dict] = {}
    usuarios: dict = {}
    proximo = int(first_id)
    for _ in range(max(1, int(max_paginas))):
        corpo = {"DIALOG_ID": dialog_id, "FIRST_ID": proximo, "LIMIT": 50}
        r = bitrix_cliente.chamar("im.dialog.messages.get", corpo, circuito="bitrix-leitura")
        res = r.get("result") or {}
        msgs = res.get("messages") or []
        usuarios.update(_mapa_usuarios(res))
        ids = [m["id"] for m in msgs if isinstance(m.get("id"), int) and m["id"] > proximo]
        for m in msgs:
            if isinstance(m.get("id"), int) and m["id"] > int(first_id):
                todas.setdefault(m["id"], m)
        if not ids:
            break
        proximo = max(ids)
    else:
        info["truncada"] = True        # ascendente: o que veio é contíguo; o resto fica p/ o próximo ciclo
    if not todas:
        # SONDA: FIRST_ID apagado devolve [] mesmo havendo mensagens novas.
        r = bitrix_cliente.chamar("im.dialog.messages.get",
                                  {"DIALOG_ID": dialog_id, "LAST_ID": 9999999999, "LIMIT": 50},
                                  circuito="bitrix-leitura")
        res = r.get("result") or {}
        ids_sonda = [m["id"] for m in (res.get("messages") or []) if isinstance(m.get("id"), int)]
        if ids_sonda and max(ids_sonda) > int(first_id):
            log.error("cursor %s do chat %s não devolve nada pelo FIRST_ID mas há mensagens "
                      "mais novas (mensagem do cursor apagada?) — lendo descendo até o cursor",
                      first_id, dialog_id)
            info["cursor_invalido"] = True
            return buscar_mensagens(webhook="", dialog_id=dialog_id, cfg=cfg,
                                    ate_id=int(first_id), info=info)
    ordenadas = [todas[k] for k in sorted(todas)]
    return [classificar_mensagem(m, usuarios, cfg) for m in ordenadas]


def buscar_mensagens(webhook: str, dialog_id: str, max_paginas: int = 40,
                     cfg: dict | None = None, first_id: int | None = None,
                     ate_id: int | None = None, info: dict | None = None) -> list[dict]:
    """Busca o histórico do grupo em ordem CRONOLÓGICA (paginando com LAST_ID).

    F1-01: com `first_id` (cursor do chat) lê SÓ o que é novo, com FIRST_ID
    (`_buscar_mensagens_desde`). Com `ate_id`, desce com LAST_ID e PARA ao ver um id
    <= ate_id; se as páginas acabarem antes, `info["lacuna"] = True` (o ciclo não
    avança o cursor). Sem nenhum dos dois = leitura antiga (1ª execução).
    """
    cfg = cfg or CONFIG_PADRAO
    if first_id:
        return _buscar_mensagens_desde(dialog_id, int(first_id), cfg, info=info)
    todas: dict[int, dict] = {}
    usuarios: dict = {}
    alcancou = ate_id is None
    # ⚠️ SEM LAST_ID o Bitrix devolve as 100 mensagens MAIS ANTIGAS do grupo
    # (medido 21/10/2026: sem LAST_ID vinha até 19/10; com LAST_ID alto veio
    # 21/10 18:49). O LAST_ID funciona como "mensagens com id < LAST_ID", então
    # começamos por um id acima de qualquer id real e descemos o histórico.
    last_id = 9999999999
    for pagina in range(max_paginas):
        corpo = {"DIALOG_ID": dialog_id, "LIMIT": 50}
        if last_id:
            corpo["LAST_ID"] = last_id
        # transporte: cliente REST único (segredo, limitador por host, disjuntor
        # "bitrix-leitura", classificação). Paginação/cursor inalterados (onda 3).
        r = bitrix_cliente.chamar("im.dialog.messages.get", corpo, circuito="bitrix-leitura")
        res = r.get("result") or {}
        msgs = res.get("messages") or []
        if not msgs:
            alcancou = True                 # fim do histórico: não é buraco
            break
        usuarios.update(_mapa_usuarios(res))
        novos = 0
        for m in msgs:
            if m.get("id") not in todas:
                todas[m["id"]] = m
                novos += 1
        ids = [m["id"] for m in msgs if isinstance(m.get("id"), int)]
        if not ids:
            break
        log.debug("mensagens: página %d -> %d itens (%d novos)", pagina, len(msgs), novos)
        if ate_id is not None and min(ids) <= int(ate_id):
            alcancou = True                 # chegou no cursor: o resto já foi incorporado
            break
        if novos == 0:
            alcancou = True
            break
        last_id = min(ids)
    if not alcancou and info is not None:
        info["lacuna"] = True               # páginas acabaram antes do cursor: buraco
    if ate_id is not None:
        todas = {k: v for k, v in todas.items() if isinstance(k, int) and k > int(ate_id)}
    ordenadas = [todas[k] for k in sorted(todas)]
    return [classificar_mensagem(m, usuarios, cfg) for m in ordenadas]


def ler_eventos(chave: str, cursor: int) -> tuple[list[dict], int]:
    """Lê os eventos do bot a partir do cursor. Devolve (eventos_uteis, novo_cursor)."""
    url = f"{VIBECODE_BASE}/v1/bots/{BOT_ID_PADRAO}/events"
    if cursor:
        url += "?offset=" + urllib.parse.quote(str(cursor))
    r = http_json(url, None, headers={"X-Api-Key": chave}, rotulo="events",
                  circuito="vibecode-leitura")
    dados = r.get("data") or {}
    eventos, novo_cursor = [], cursor
    for ev in dados.get("events") or []:
        d = ev.get("data") or {}
        msg = d.get("message") or {}
        # filtro anti-loop: ignora mensagens de sistema e do próprio bot
        if msg.get("isSystem") is True:
            continue
        if msg.get("authorId") == BOT_ID_PADRAO or (d.get("user") or {}).get("id") == BOT_ID_PADRAO:
            continue
        eventos.append(ev)
    if isinstance(dados.get("nextOffset"), int):
        novo_cursor = max(novo_cursor, dados["nextOffset"])
    return eventos, novo_cursor


# --------------------------------------------------------------------------
# Agrupador
#   PRIMÁRIO  : sinais explícitos INÍCIO/FIM postados pela equipe (config.json)
#   FALLBACK  : autor + janela de tempo (default 30 min) para o que não tem sinal
# --------------------------------------------------------------------------
def _novo_lote() -> dict:
    return {"mensagens": [], "file_ids": [], "cpfs": [], "nomes": [],
            "nomes_ident": [], "nomes_por_cpf": {}, "autores": [],
            # (PONTO 2.4) acervo de papéis: o CPF de quem é o caso e o da
            # mãe/pai, que é contato e NÃO pode virar o CPF do cliente.
            "cpfs_papeis": {}, "cpfs_responsavel": [],
            "cpfs_invalidos": [],                 # F1-09: DV errado, só revisão
            "nomes_papeis": {}, "nomes_rotulos": {},
            "inicio": None, "fim": None, "fechado": False, "fechado_por": None,
            "modo": None, "pronto": False, "status_lote": None,
            "sinal_abertura": None, "sinal_fechamento": None,
            "fechado_pelo_traco": False, "marcador_fim": False}


def _lote_id(lote: dict, prefixo: str = "LOTE") -> str:
    prim = lote["mensagens"][0] if lote["mensagens"] else {}
    semente = f"{prim.get('autor_id')}-{prim.get('id')}"
    h = hashlib.sha1(semente.encode()).hexdigest()[:8]
    ts = prim.get("ts")
    stamp = ts.strftime("%Y%m%d-%H%M") if ts else "sdata"
    return f"{prefixo}-{stamp}-{h}"


def _acumular(lote: dict, msg: dict) -> None:
    lote["mensagens"].append(msg)
    lote["inicio"] = lote["inicio"] or msg["ts"]
    lote["fim"] = msg["ts"] or lote["fim"]
    if msg["autor"] not in lote["autores"]:
        lote["autores"].append(msg["autor"])
    # marcador de fim ("PASTA FINALIZADA" etc.) marca o lote em QUALQUER caminho
    # (traço, janela, flush) — antes só `agrupar_por_janela` marcava.
    if msg.get("e_fim"):
        lote["marcador_fim"] = True
        lote["ts_marcador"] = msg["ts"]
    for f in msg["file_ids"]:
        if f not in lote["file_ids"]:
            lote["file_ids"].append(f)
    for c in msg["cpfs"]:
        if c not in lote["cpfs"]:
            lote["cpfs"].append(c)
    for c in (msg.get("cpfs_invalidos") or []):
        if c not in lote.setdefault("cpfs_invalidos", []):
            lote["cpfs_invalidos"].append(c)
    # (PONTO 2.4) o papel do CPF GRUDA: se o mesmo número já foi rotulado como
    # responsável em outra mensagem, não rebaixa para "sem rótulo". E o CPF de
    # responsável é acumulado à parte — é o contato do caso, não o cliente.
    papeis_msg = msg.get("cpfs_papeis") or {}
    papeis_lote = lote.setdefault("cpfs_papeis", {})
    for c, p in papeis_msg.items():
        if p == "responsavel" or c not in papeis_lote:
            papeis_lote[c] = p
        if p == "responsavel" and c not in lote.setdefault("cpfs_responsavel", []):
            lote["cpfs_responsavel"].append(c)
    for n in msg["nomes"]:
        if n not in lote["nomes"]:
            lote["nomes"].append(n)
    for n, p in (msg.get("nomes_papeis") or {}).items():
        lote.setdefault("nomes_papeis", {})[n] = p
    for n, r in (msg.get("nomes_rotulos") or {}).items():
        lote.setdefault("nomes_rotulos", {})[n] = r
    # nomes vindos de mensagem IDENTIFICADORA (ficha/cabeçalho/com CPF) ficam em
    # lista separada: no pós-processamento eles vencem o nome "solto" da conversa
    # (era assim que 'Link da procuração' virava nome de lote — 13/10/2026).
    if msg.get("e_identificador"):
        for n in msg["nomes"]:
            if n not in lote.setdefault("nomes_ident", []):
                lote["nomes_ident"].append(n)
    # pareamento SEGURO: nome que veio na MESMA mensagem do CPF
    # ("GILMAR TESTE DA SILVA CPF 570.418.035-38", formato real 06/10/2026).
    # Mesma mensagem = mesmo cliente — é o único pareamento que não cruza.
    #
    # (PONTO 2.4) e só entre papéis COMPATÍVEIS: nome de cliente NUNCA com CPF
    # de responsável. Era o produto cartesiano de antes — com
    # "= MENOR MARIA SILVA" + "= CPF MÃE DA MENOR: 863…" o nome da menor nascia
    # pareado ao CPF da mãe, e o nome certo ia parar no lugar errado.
    if msg.get("nomes") and msg.get("cpfs"):
        par = lote.setdefault("nomes_por_cpf", {})
        papeis_nome_msg = msg.get("nomes_papeis") or {}
        for cp in msg["cpfs"]:
            papel_cp = papeis_msg.get(cp)
            for n in msg["nomes"]:
                if not _papeis_compativeis(papeis_nome_msg.get(n, "cliente"), papel_cp):
                    continue
                if n not in par.setdefault(cp, []):
                    par[cp].append(n)


def _papeis_compativeis(papel_nome, papel_cpf) -> bool:
    """(PONTO 2.4) O nome pode ser pareado com este CPF?

    cliente ↔ {cliente, sem rótulo}; responsável ↔ {responsavel, sem rótulo}.
    NUNCA cruzados: é o que impede o nome da MENOR de ficar no CPF DA MÃE.
    """
    if papel_nome == "responsavel":
        return papel_cpf in (None, "responsavel")
    return papel_cpf in (None, "cliente")


def _reacumular(lote: dict) -> None:
    """(PONTO 2.3) Reconstrói as listas derivadas do lote do zero.

    Usado depois de `_realocar_sem_identidade` tirar mensagens do fim do lote:
    sem isto, file_ids/cpfs/nomes/nomes_por_cpf ficariam com resíduo do que
    saiu. Rechamar `_acumular` para cada mensagem restante reproduz exatamente
    a semântica dele (inicio = primeiro ts, fim = último ts não-nulo, listas
    sem duplicatas na ordem de chegada) sem duplicar lógica.
    """
    msgs = list(lote.get("mensagens") or [])
    lote["mensagens"] = []
    lote["autores"] = []
    lote["file_ids"] = []
    lote["cpfs"] = []
    lote["nomes"] = []
    lote["nomes_ident"] = []
    lote["nomes_por_cpf"] = {}
    lote["cpfs_papeis"] = {}
    lote["cpfs_responsavel"] = []
    lote["cpfs_invalidos"] = []
    lote["nomes_papeis"] = {}
    lote["nomes_rotulos"] = {}
    lote["inicio"] = None
    lote["fim"] = None
    for m in msgs:
        _acumular(lote, m)


def _eh_ruido(msg: dict) -> bool:
    return bool(msg.get("sistema")) or bool(msg.get("ruido"))


def _e_outra_pessoa(cpfs_lote: set, nomes_lote: set, msg: dict,
                    cpfs_papeis_lote: dict | None = None) -> tuple[bool, str]:
    """(PONTO 2.2, 14/10/2026) A msg é de OUTRA pessoa do que o bloco/lote?

    Regra ÚNICA para `_muda_identidade`, o corte de `agrupar_por_janela` e o
    bloco do `traco_no_fim_fecha` — antes cada um decidia por conta própria e
    os três discordavam (a janela cortava por nome mesmo com CPF no lote, a
    barreira não; o bloco do traço no fim engolia cliente de outro).

    Decide só com evidência forte:
      * os DOIS lados têm CPF de CLIENTE -> interseção vazia = outra pessoa;
        interseção não vazia = MESMA pessoa (mesmo que um lado tenha CPF
        extra — protege MENOR×MÃE com CPF compartilhado).
      * senão, se a msg é identificador (ficha/cabeçalho) e há NOMES dos dois
        lados disjuntos e sem um conter o outro -> outra pessoa (nome parcial
        não conta: "MARIA" x "MARIA SILVA"). Independente de CPF: msg com CPF
        e lote só com nome (ou o inverso) também é julgado pelo nome.
      * senão, não decide: arquivo/foto sem texto fica no lote.

    (PONTO 2.4, 14/10/2026) CPF de RESPONSAVEL (mãe/pai/resp) NUNCA corta:
    é a mesma família. Só CPF de CLIENTE entra na comparação — sem isso, o
    CPF da mãe em mensagem separada fechava o bloco do menor como "outra
    pessoa" (3 lotes em vez de 1, verificado em execução).
    """
    papeis_msg = msg.get("cpfs_papeis") or {}
    papeis_lote = cpfs_papeis_lote or {}
    cpfs_msg = {c for c in (msg.get("cpfs") or [])
                if papeis_msg.get(c) != "responsavel"}
    cpfs_cli_lote = {c for c in cpfs_lote
                     if papeis_lote.get(c) != "responsavel"}
    if cpfs_cli_lote and cpfs_msg:
        if not (cpfs_cli_lote & cpfs_msg):
            return True, (f"CPF {cpf_formatado(sorted(cpfs_msg)[0])} != "
                          f"{cpf_formatado(sorted(cpfs_cli_lote)[0])} do bloco")
        return False, ""
    if msg.get("e_identificador"):
        nomes_msg = {normalizar_nome(n) for n in (msg.get("nomes") or [])
                     if _e_nome_valido(n)}
        if nomes_lote and nomes_msg and nomes_lote.isdisjoint(nomes_msg):
            if not any(_mesmo_nome(a, b) for a in nomes_lote for b in nomes_msg):
                return True, (f"nome {sorted(nomes_msg)[0]!r} != "
                              f"{sorted(nomes_lote)[0]!r} do bloco")
    return False, ""


def _mesmo_nome(a: str, b: str) -> bool:
    """Nome parcial não é outra pessoa: "MARIA" x "MARIA SILVA" (substring) e,
    F1-05, "JOANA LIMA" x "JOANA TESTE LIMA" (palavras de um contidas no outro,
    ignorando partículas) — o cabeçalho completo do PRÓPRIO cliente não corta."""
    if a in b or b in a:
        return True
    part = {"DA", "DE", "DO", "DAS", "DOS", "E"}
    ta = {t for t in a.split() if t not in part}
    tb = {t for t in b.split() if t not in part}
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def _muda_identidade(lote: dict, msg: dict) -> tuple[bool, str]:
    """(22/09/2026) BARREIRA DE IDENTIDADE — o documento é de OUTRA pessoa?

    Usado dentro de um bloco ABERTO por traço. O bloco nasce para o grupo
    (as fotos que a conta que encaminha publica entram nele), mas documento
    de outro CLIENTE não pode entrar: senão o kit de um pode ser gravado na
    pasta de outro (era o furo: lote sem CPF do dono + documento alheio com
    CPF -> lote identificado com o CPF errado).

    Decide só com evidência forte:
      * o lote TEM CPF de cliente e a mensagem traz CPF de cliente diferente
        -> outra pessoa;
      * o lote NÃO tem CPF, a mensagem é identificador (ficha/cabeçalho) com
        NOME de cliente e os nomes não se contêm -> outra pessoa.
    Arquivo/foto sem texto não decide nada (não dá para julgar) — fica no lote.

    (PONTO 2.2, 14/10/2026) virou casca fina sobre `_e_outra_pessoa` — a
    decisão em si está lá, compartilhada com `agrupar_por_janela` e o corte
    do `traco_no_fim_fecha`.
    (PONTO 2.4) CPF/nome de RESPONSAVEL não conta: mãe e pai entram no bloco
    do filho sem disparar corte.
    """
    cpfs_lote = set(lote.get("cpfs") or [])
    # F1-05: só nome de evidência de identidade (ficha/cabeçalho/mensagem com
    # CPF = `nomes_ident`), nunca `nomes` cru — "bom dia" da conversa cortava o
    # kit quando chegava o cabeçalho do PRÓPRIO cliente.
    nomes_lote = {normalizar_nome(n) for n in (lote.get("nomes_ident") or [])
                  if _e_nome_valido(n)}
    return _e_outra_pessoa(cpfs_lote, nomes_lote, msg,
                           lote.get("cpfs_papeis"))


def _realocar_sem_identidade(lote: dict, msg_gatilho: dict,
                             folga: _dt.timedelta | None) -> list[dict]:
    """(PONTO 2.3) Tira do fim do lote o rastro contíguo SEM identidade.

    Caso real: ficha -> documentos -> cabeçalho -> TRAÇO. As fotos do cliente B
    (sem texto) entram no bloco do A porque "foto sem texto não decide nada" —
    aí o cabeçalho do B dispara a barreira e as fotos dele ficam presas no kit
    do A. Agora, no momento do corte, andamos PARA TRÁS e movemos para
    `pendentes` toda mensagem sem identidade colada no gatilho.

    PARA na primeira mensagem que:
      · tem sinal/traço, identificador, CPF ou nome (é do A — não pode sair);
      · o salto de tempo para a referência seguinte excede `folga` (a referência
        começa como o gatilho e vira a última movida; só quando AMBOS os ts
        existem — sem ts não se corta por tempo);
      · seria a última mensagem restante (preserva o traço de abertura em 0).
    Devolve as movidas em ordem cronológica.
    """
    msgs = lote.get("mensagens") or []
    movidas: list[dict] = []
    ts_ref = msg_gatilho.get("ts")
    while len(msgs) > 1:
        cand = msgs[-1]
        if (cand.get("sinal") or cand.get("e_identificador")
                or cand.get("cpfs") or cand.get("nomes")):
            break
        if (folga is not None and folga.total_seconds() > 0
                and ts_ref and cand.get("ts")
                and (ts_ref - cand["ts"]) > folga):
            break
        movidas.append(msgs.pop())
        ts_ref = cand.get("ts") or ts_ref
    movidas.reverse()
    return movidas


def _intruso_no_lote(lote: dict) -> str:
    """'' = o kit ficou limpo; senão, o motivo de o intruso ter entrado.

    A barreira desvia a mensagem que DISPARA o corte (ela vai para `pendentes`),
    mas evidência de outra pessoa pode ter entrado ANTES do corte:
      · foto/documento sem texto não decide nada e fica no lote;
      · (PONTO 2.2) nome sem CPF AGORA dispara a barreira mesmo com CPF no
        bloco — mas evidência anterior ao corte (foto solta, por exemplo)
        pode ter entrado e é o que este detector apanha.
    Se alguma mensagem carrega CPF — ou nome identificado — de outra pessoa,
    o kit NÃO pode sair sozinho: o motivo vai para `motivos_revisao` + alerta.

    (PONTO 2.4) CPF/nome de RESPONSAVEL (mãe/pai) NÃO é intruso: mesma
    família, entra no kit do filho sem marcar mistura.
    """
    cpfs_lote = set(lote.get("cpfs") or [])
    papeis_lote = lote.get("cpfs_papeis") or {}
    cpfs_cli_lote = {c for c in cpfs_lote if papeis_lote.get(c) != "responsavel"}
    nome_lote = lote.get("nome") or ""
    nome_norm = normalizar_nome(nome_lote)
    for m in lote.get("mensagens") or []:
        if _eh_ruido(m):
            continue
        papeis_m = m.get("cpfs_papeis") or {}
        cpfs_m = {c for c in (m.get("cpfs") or [])
                  if papeis_m.get(c) != "responsavel"}
        if cpfs_cli_lote and cpfs_m and not (cpfs_cli_lote & cpfs_m):
            return (f"mensagem {m.get('id')} carrega CPF "
                    f"{cpf_formatado(sorted(cpfs_m)[0])} de outra pessoa")
        if not nome_norm or not m.get("e_identificador"):
            continue
        for n in (m.get("nomes") or []):
            if not _e_nome_valido(n):
                continue
            if (m.get("nomes_papeis") or {}).get(n) == "responsavel":
                continue                       # mesma família — não é mistura
            nn = normalizar_nome(n)
            # nome parcial não é outra pessoa ("MARIA" x "MARIA SILVA")
            if _mesmo_nome(nome_norm, nn):
                continue
            return (f"mensagem {m.get('id')} carrega o nome {n!r} "
                    f"de outra pessoa (o lote é de {nome_lote!r})")
    return ""


def _fechar(lote: dict, motivo: str, lotes: list[dict]) -> None:
    if not lote["mensagens"]:
        return
    lote["fechado"] = True
    lote["fechado_por"] = motivo
    lote.setdefault(
        "lote_id",
        _lote_id(lote, "LOTE-SIG" if lote.get("modo") == "marcadores" else "LOTE"),
    )
    lotes.append(lote)


def agrupar_por_janela(mensagens: list[dict], janela_min: int,
                       agora: _dt.datetime | None = None,
                       reacoes_out: list[dict] | None = None) -> list[dict]:
    """
    FALLBACK — forma LOTES por (autor + janela de tempo).

    Usado só para o que NÃO está entre um sinal de INÍCIO e um sinal de FIM.
    Fecha o lote quando: muda o autor; passa a janela sem atividade; aparece
    marcador de fim ("Assinou e agendou" etc.); aparece cabeçalho/ficha de OUTRO
    cliente (CPF disjunto) do mesmo autor. NÃO assume ordem: arquivos antes do
    cabeçalho ficam no mesmo lote.
    """
    janela = _dt.timedelta(minutes=janela_min)
    folga_fim = _dt.timedelta(minutes=MARCADOR_FIM_FOLGA_MIN)
    lotes: list[dict] = []
    reacoes: list[dict] = []
    atual = _novo_lote()
    atual["modo"] = "janela"

    for msg in mensagens:
        # REAÇÃO nunca entra em lote (filtro ANTES de qualquer processamento)
        if msg.get("reacao"):
            reacoes.append(msg)
            continue
        if _eh_ruido(msg):
            continue
        if atual["mensagens"]:
            ultimo = atual["mensagens"][-1]
            gap = (msg["ts"] - ultimo["ts"]) if (msg["ts"] and ultimo["ts"]) else None
            mesmo_autor = normalizar_nome(msg["autor"]) == normalizar_nome(ultimo["autor"])

            motivo = None
            if not mesmo_autor:
                motivo = "troca de autor"
            elif gap is not None and gap > janela:
                motivo = "janela de tempo"
            elif (atual.get("ts_marcador") and msg["ts"]
                  and (msg["ts"] - atual["ts_marcador"]) > folga_fim):
                motivo = "folga após marcador de fim"
            elif msg["e_identificador"]:
                # PONTO 2.2: mesma regra da barreira (`_e_outra_pessoa`) — antes
                # a janela cortava por nome mesmo com CPF no lote e a barreira não.
                cpfs_lote = set(atual["cpfs"])
                # F1-05: nomes de evidência (nomes_ident), não conversa solta.
                nomes_lote = {normalizar_nome(n) for n in (atual.get("nomes_ident") or [])
                              if _e_nome_valido(n)}
                # (PONTO 2.4) o papel do CPF do lote acompanha — mãe/pai não
                # fecha a janela do filho.
                mudou, porque = _e_outra_pessoa(cpfs_lote, nomes_lote, msg,
                                                atual.get("cpfs_papeis"))
                if mudou:
                    motivo = f"cabeçalho de outro cliente ({porque})"

            if motivo:
                _fechar(atual, motivo, lotes)
                atual = _novo_lote()
                atual["modo"] = "janela"

        _acumular(atual, msg)
        # marcador de fim NÃO fecha na hora: já veio arquivo DEPOIS dele (PDF do
        # protocolo). Só anota e deixa a folga/janela fechar.
        if msg["e_fim"]:
            atual["marcador_fim"] = True
            atual["ts_marcador"] = msg["ts"]

    _fechar(atual, "fim da leitura", lotes)
    if reacoes_out is not None:
        reacoes_out.extend(reacoes)
    return lotes


def agrupar(mensagens: list[dict], cfg: dict, agora: _dt.datetime | None = None,
            orfaos_out: list[dict] | None = None,
            reacoes_out: list[dict] | None = None) -> list[dict]:
    """
    Agrupamento principal: SINAL DE TRAÇO como TOGGLE.

     1º traço = ABRE o lote
     2º traço = FECHA o lote (FECHADO_PRONTO)
     3º traço = abre de novo (alterna, indefinidamente)

    MODO DO TRAÇO (config.modo_traco, ver MODOS_TRACO):
      * 'grupo_autor' (padrão desde 21/09/2026, decisão do dono) = REGRA DO MEIO:
        o traço ABRE para o grupo (as fotos que a conta que encaminha publica
        depois entram no bloco) e só FECHA com o traço de QUEM ABRIU — traço de
        outra pessoa com bloco aberto é ignorado (não fecha nem abre paralelo).
        EXCEÇÃO (config.traco_vigencia_min, padrão 30 min): se o bloco ficou
        parado mais que isso, ele é fechado como FECHADO_POR_INATIVIDADE e o
        traço da outra pessoa assume — senão um traço órfão travaria o grupo.
      * 'grupo' = qualquer traço abre/fecha o bloco do grupo.
      * 'autor' = legado, cada pessoa com o seu próprio abrir/fechar.

    Fechamentos possíveis:
      * FECHADO_PRONTO            -> veio o 2º traço
      * FECHADO_POR_TIMEOUT       -> passou timeout_lote_min sem o traço de fechar
      * CONFLITO                  -> 2+ CPFs de CLIENTE diferentes no mesmo
                                      lote (revisão). Mistura cliente+mãe/pai
                                      NÃO é conflito — ver PONTO 2.4.

    Tudo que está FORA de um par de traços cai no FALLBACK por janela de tempo.
    """
    timeout_min = int(cfg.get("timeout_lote_min") or 120)
    janela_min = int(cfg.get("janela_min_fallback") or JANELA_MIN_PADRAO)
    try:
        folga_realoc_min = int(cfg.get("realoc_folga_min", REALOC_FOLGA_MIN))
    except (TypeError, ValueError):
        folga_realoc_min = REALOC_FOLGA_MIN
    vigencia_min = cfg.get("traco_vigencia_min", janela_min)
    try:
        vigencia_min = int(vigencia_min)
    except (TypeError, ValueError):
        vigencia_min = janela_min
    vigencia = _dt.timedelta(minutes=vigencia_min) if vigencia_min > 0 else None
    timeout = _dt.timedelta(minutes=timeout_min)
    modo = modo_traco(cfg)
    por_autor = (modo == "autor")
    fecha_so_do_autor = (modo == "grupo_autor")

    lotes: list[dict] = []
    aberto: dict = {}          # chave -> lote aberto ("GLOBAL" nos modos de grupo)
    pendentes: list[dict] = []
    orfaos: list[dict] = []
    reacoes: list[dict] = []

    def chave_autor(msg: dict) -> str:
        return f"{msg.get('autor_id')}|{normalizar_nome(msg['autor'])}"

    def chave(msg: dict) -> str:
        return chave_autor(msg) if por_autor else "GLOBAL"

    def flush_pendentes():
        if pendentes:
            lotes.extend(agrupar_por_janela(list(pendentes), janela_min, agora))
            pendentes.clear()

    def fechar_por_timeout(k: str) -> None:
        lote = aberto.pop(k, None)
        if lote is None:
            return
        lote["status_lote"] = "FECHADO_POR_TIMEOUT"
        _fechar(lote, f"fechado por timeout ({timeout_min} min sem o traço de fechamento)", lotes)
        log.warning("lote %s do autor %s fechado por timeout (%s min)",
                    lote.get("lote_id"), lote.get("autores"), timeout_min)

    for msg in mensagens:
        if _eh_ruido(msg):
            continue
        k = chave(msg)

        # timeout do lote aberto (esquecimento do traço de fechar)
        lote_autor = aberto.get(k)
        if lote_autor is not None and lote_autor["fim"] and msg["ts"] \
                and (msg["ts"] - lote_autor["fim"]) > timeout:
            fechar_por_timeout(k)
            lote_autor = None

        if msg.get("sinal"):
            if lote_autor is not None:
                mesma_pessoa = (lote_autor.get("_chave_abertura") == chave_autor(msg))
                # BLOCO VENCIDO: ninguém fechou e a última mensagem do bloco é
                # mais velha que traco_vigencia_min -> o traço de OUTRA pessoa
                # assume (senão um traço órfão travaria o grupo para sempre: foi
                # o que aconteceu em 21/09 com o traço do Erialdo às 12:06).
                vencido = bool(vigencia and lote_autor["fim"] and msg["ts"]
                               and (msg["ts"] - lote_autor["fim"]) > vigencia)
                if modo in ("delimitador", "delimitador_autor"):
                    # DELIMITADOR (pedido do dono, 24/09/2026): *"assim que ela mandar ——
                    # ela abre a janela e assim que ela mandar de volta embaixo já fecha o
                    # de cima e deixa o de baixo aberto"*. Bloco = intervalo entre dois
                    # traços: o traço de baixo FECHA o de cima e ABRE o novo — não espera
                    # traço de fechamento nem o timeout de 120 min (era o delay/mistura).
                    if modo == "delimitador_autor" and not mesma_pessoa:
                        _acumular(lote_autor, msg)
                        log.info("traço de %s IGNORADO (bloco de %s aberto) — modo "
                                 "delimitador_autor", msg["autor"],
                                 lote_autor["sinal_abertura"]["autor"])
                        continue
                    _acumular(lote_autor, msg)
                    lote_autor["sinal_fechamento"] = {
                        "message_id": msg["id"], "autor": msg["autor"],
                        "ts": msg["ts"].isoformat() if msg["ts"] else None,
                    }
                    lote_autor["status_lote"] = "FECHADO_PRONTO"
                    lote_autor["fechado_pelo_traco"] = True
                    _fechar(lote_autor,
                            "sinal de traço (delimitador: fechado pelo traço de baixo)", lotes)
                    aberto.pop(k, None)
                    log.info("lote %s FECHADO pelo traço delimitador de %s "
                             "(o mesmo traço abre o bloco de baixo)",
                             lote_autor.get("lote_id"), msg["autor"])
                    lote_autor = None            # segue para ABRIR o bloco de baixo
                if lote_autor is not None:       # modos antigos (grupo / grupo_autor / autor)
                    if fecha_so_do_autor and not mesma_pessoa and not vencido:
                        # REGRA DO MEIO: traço de outra pessoa não fecha nem abre
                        # paralelo — fica dentro do bloco (o bloco é de quem abriu).
                        _acumular(lote_autor, msg)
                        log.info("traço de %s IGNORADO (bloco de %s aberto em %s)",
                                 msg["autor"], lote_autor["sinal_abertura"]["autor"],
                                 lote_autor["sinal_abertura"]["ts"])
                        continue
                    if fecha_so_do_autor and not mesma_pessoa and vencido:
                        lote_autor["status_lote"] = "FECHADO_POR_INATIVIDADE"
                        _fechar(lote_autor,
                                f"bloco vencido ({vigencia_min} min sem movimento) — "
                                f"o traço de {msg['autor']} assumiu", lotes)
                        aberto.pop(k, None)
                        log.warning("bloco de %s vencido (%s min); traço de %s assume",
                                    lote_autor["sinal_abertura"]["autor"], vigencia_min,
                                    msg["autor"])
                        lote_autor = None
                    else:
                        # 2º traço (ou 4º, 6º...) = FECHAR
                        _acumular(lote_autor, msg)
                        lote_autor["sinal_fechamento"] = {
                            "message_id": msg["id"], "autor": msg["autor"],
                            "ts": msg["ts"].isoformat() if msg["ts"] else None,
                        }
                        lote_autor["status_lote"] = "FECHADO_PRONTO"
                        lote_autor["fechado_pelo_traco"] = True
                        _fechar(lote_autor, "sinal de traço (fechamento)", lotes)
                        aberto.pop(k, None)
                        log.info("lote %s FECHADO pelo traço (autor=%s)",
                                 lote_autor.get("lote_id"), msg["autor"])
                        continue
            if lote_autor is None:
                # TRAÇO NO FIM DO BLOCO (regra do dono, 22/09/2026 — caso Johanna):
                # o grupo manda ficha -> documentos -> cabeçalho -> TRAÇO. Se há
                # material pendente desde o último traço (arquivo ou identidade),
                # ESTE traço FECHA esse bloco em vez de abrir um bloco vazio.
                # Sem isso o kit cai no agrupamento por JANELA e é barrado pela
                # regra "só traço" (foi o que travou o kit da Johanna com 12 arq.).
                if cfg.get("traco_no_fim_fecha", True) and pendentes:
                    # O kit no grupo é: FICHA ("DADOS NECESSÁRIOS PARA DAR A ENTRADA" /
                    # "FICHA DE ATENDIMENTO") -> documentos -> cabeçalho "= CAMPO" -> TRAÇO.
                    # 1) delimita a estiada contígua que precede o traço;
                    gap = _dt.timedelta(minutes=int(cfg.get("janela_min_fallback")
                                                   or JANELA_MIN_PADRAO))
                    ini_estreita = len(pendentes) - 1
                    while ini_estreita > 0:
                        ant, cur = pendentes[ini_estreita - 1], pendentes[ini_estreita]
                        if ant.get("ts") and cur.get("ts") and (cur["ts"] - ant["ts"]) <= gap:
                            ini_estreita -= 1
                        else:
                            break
                    # 2) dentro dela, o bloco começa na FICHA; se não houver ficha, no
                    #    cabeçalho; se não houver marcador nenhum, NÃO fecha (senão um
                    #    traço tardio engoliria a conversa de vários clientes — medido:
                    #    269 mensagens/196 arquivos num lote).
                    inicio_bloco = None
                    for func in (e_abertura_ou_ficha, tem_nome_cabecalho):
                        for i in range(len(pendentes) - 1, ini_estreita - 1, -1):
                            if func(pendentes[i].get("texto_bruto") or ""):
                                inicio_bloco = i
                                break
                        if inicio_bloco is not None:
                            break
                    # 2b) PONTO 2.2: o bloco do traço no fim NÃO atravessa
                    #     cliente — corta na 1ª mensagem que `_e_outra_pessoa`
                    #     marcar como de outro (o resto fica em `pendentes` e
                    #     segue pelo flush, sem remixar cliente).
                    fim_bloco = len(pendentes)
                    if inicio_bloco is not None:
                        cpfs_bloco: set = set()
                        nomes_bloco: set = set()
                        # (PONTO 2.4) o papel do CPF do bloco acompanha: o CPF
                        # da mãe não fecha o bloco do filho.
                        papeis_bloco: dict = {}
                        for i in range(inicio_bloco, len(pendentes)):
                            m_pend = pendentes[i]
                            mudou_b, _ = _e_outra_pessoa(cpfs_bloco, nomes_bloco, m_pend,
                                                         papeis_bloco)
                            if mudou_b:
                                fim_bloco = i
                                break
                            cpfs_bloco |= set(m_pend.get("cpfs") or [])
                            for c, p in (m_pend.get("cpfs_papeis") or {}).items():
                                if p == "responsavel" or c not in papeis_bloco:
                                    papeis_bloco[c] = p
                            if m_pend.get("e_identificador"):     # F1-05
                                nomes_bloco |= {normalizar_nome(n)
                                                for n in (m_pend.get("nomes") or [])
                                                if _e_nome_valido(n)}
                        # (15/10/2026) a ficha pode vir NO FIM (depois dos documentos) —
                        # estende o início do bloco para trás, puxando os documentos SEM
                        # identidade de OUTRO cliente que vieram ANTES da ficha. Sem isso a
                        # ficha vira um lote 'SEM_ARQUIVOS' e os documentos um 'SEM_CPF'.
                        j = inicio_bloco - 1
                        while j >= ini_estreita:
                            m_ant = pendentes[j]
                            mudou_ant, _ = _e_outra_pessoa(cpfs_bloco, nomes_bloco,
                                                           m_ant, papeis_bloco)
                            if mudou_ant:
                                break
                            inicio_bloco = j
                            cpfs_bloco |= set(m_ant.get("cpfs") or [])
                            for c, p in (m_ant.get("cpfs_papeis") or {}).items():
                                if p == "responsavel" or c not in papeis_bloco:
                                    papeis_bloco[c] = p
                            if m_ant.get("e_identificador"):     # F1-05
                                nomes_bloco |= {normalizar_nome(n)
                                                for n in (m_ant.get("nomes") or [])
                                                if _e_nome_valido(n)}
                            j -= 1
                    bloco = pendentes[inicio_bloco:fim_bloco] \
                        if inicio_bloco is not None else []
                    tem_material = any(m.get("file_ids") for m in bloco) or \
                                   any(m.get("cpfs") for m in bloco)
                    if tem_material:
                        do_fim = _novo_lote()
                        do_fim["modo"] = "marcadores"
                        do_fim["status_lote"] = "ABERTO"
                        for m in bloco:
                            _acumular(do_fim, m)
                        pendentes[:] = pendentes[:inicio_bloco] + pendentes[fim_bloco:]
                        _acumular(do_fim, msg)
                        do_fim["sinal_fechamento"] = {
                            "message_id": msg["id"], "autor": msg["autor"],
                            "ts": msg["ts"].isoformat() if msg["ts"] else None,
                        }
                        do_fim["status_lote"] = "FECHADO_PRONTO"
                        do_fim["fechado_pelo_traco"] = True
                        _fechar(do_fim, "sinal de traço (no FIM do bloco)", lotes)
                        log.info("lote %s FECHADO pelo traço do FIM do bloco "
                                 "(autor=%s, %d mensagens, %d arquivo(s))",
                                 do_fim.get("lote_id"), msg["autor"],
                                 len(do_fim["mensagens"]), len(do_fim["file_ids"]))
                        continue
                # 1º traço (ou 3º, 5º...) = ABRIR
                novo = _novo_lote()
                novo["modo"] = "marcadores"
                novo["status_lote"] = "ABERTO"
                novo["_chave_abertura"] = chave_autor(msg)
                novo["sinal_abertura"] = {
                    "message_id": msg["id"], "autor": msg["autor"],
                    "autor_id": msg["autor_id"], "ts": msg["ts"].isoformat() if msg["ts"] else None,
                    "texto": msg["texto_bruto"][:120],
                }
                _acumular(novo, msg)
                aberto[k] = novo
                log.info("lote ABERTO pelo traço (autor=%s msg=%s ts=%s) modo_traco=%s",
                         msg["autor"], msg["id"], msg["data"], modo)
            continue

        if lote_autor is not None:                     # mensagem dentro do lote
            # BARREIRA DE IDENTIDADE (22/09/2026 — pedido do dono: "garantir que
            # não ocorra inserção de documentos de outras pessoas"): documento de
            # OUTRO cliente não entra no bloco aberto. O bloco é fechado sem o
            # traço de fechamento (fechado_pelo_traco=False) e o material do
            # novo cliente segue sozinho pelo fallback/traço dele.
            # (14/10/2026) o intruso NÃO entra no lote (a mensagem vai para
            # `pendentes`) — o que fica é o kit do 1º cliente, que pode estar
            # COMPLETO e ir para a triagem (ver pós-processamento: _kit_pela_barreira).
            mudou, porque = ((False, "") if not cfg.get("barreira_identidade", True)
                             else _muda_identidade(lote_autor, msg))
            if mudou:
                # PONTO 2.3: ANTES de fechar, tira do kit o rastro de mensagens
                # SEM identidade coladas no corte (fotos do cliente novo que
                # entraram no bloco do anterior porque "foto sem texto não
                # decide nada"). Elas seguem com o gatilho para `pendentes`.
                folga_realoc = (_dt.timedelta(minutes=folga_realoc_min)
                                if folga_realoc_min > 0 else None)
                movidas = _realocar_sem_identidade(lote_autor, msg, folga_realoc)
                for m in movidas:
                    m["_realocada"] = True
                if movidas:
                    lote_autor["_realocadas"] = [m.get("id") for m in movidas]
                    _reacumular(lote_autor)
                    log.warning("bloco %s: %d mensagem(ns) sem identidade "
                                "realocadas para fora do kit (coladas no corte)",
                                lote_autor.get("lote_id"), len(movidas))
                lote_autor["status_lote"] = "FECHADO_POR_TROCA_DE_PESSOA"
                _fechar(lote_autor,
                        f"bloco interrompido por outra pessoa ({porque})", lotes)
                aberto.pop(k, None)
                log.warning("bloco %s de %s INTERROMPIDO pela barreira de "
                            "identidade (%s); a mensagem %s seguiu fora do bloco",
                            lote_autor.get("lote_id"), lote_autor.get("autores"),
                            porque, msg.get("id"))
                pendentes.extend(movidas)
                pendentes.append(msg)
            else:
                _acumular(lote_autor, msg)
        else:                                          # fora de qualquer traço -> fallback
            pendentes.append(msg)

    # fim da leitura: lote sem traço de fechamento só fecha se estourou o timeout
    for k, lote in list(aberto.items()):
        if lote["fim"] and agora and (agora - lote["fim"]) > timeout:
            lote["status_lote"] = "FECHADO_POR_TIMEOUT"
            _fechar(lote, f"fechado por timeout ({timeout_min} min sem o traço de fechamento)", lotes)
        else:
            lote["status_lote"] = "ABERTO"
            lotes.append(lote)      # segue ABERTO: fase 2 espera o traço de fechar
        aberto.pop(k, None)
    flush_pendentes()
    if orfaos_out is not None:
        orfaos_out.extend(orfaos)
    if reacoes_out is not None:
        reacoes_out.extend(reacoes)

    # ---- pós-processamento comum ----
    agora = agora or _dt.datetime.now(_dt.timezone.utc)
    for lote in lotes:
        lote.setdefault(
            "lote_id",
            _lote_id(lote, "LOTE-SIG" if lote["modo"] == "marcadores" else "LOTE"),
        )
        distintos = list(dict.fromkeys(lote["cpfs"]))
        # (PONTO 2.4) separa o CPF de quem é o CASO do da mãe/pai. Mistura
        # cliente+responsável NÃO é conflito — é família. CONFLITO só quando
        # há 2+ CPFs de CLIENTE distintos (aí não dá para saber de quem é o kit).
        papeis = lote.get("cpfs_papeis") or {}
        cpfs_cliente = [c for c in distintos if papeis.get(c) != "responsavel"]
        cpfs_resp = [c for c in distintos if papeis.get(c) == "responsavel"]
        lote["cpfs_responsavel"] = cpfs_resp
        lote["cpf_responsavel"] = cpfs_resp[0] if len(cpfs_resp) == 1 else None
        # O CPF do caso é o de CLIENTE. Se só houver o da mãe/pai (que costuma
        # ser o contato do caso), ele entra como fallback — com
        # `cpf_responsavel` marcado para a auditoria.
        if len(cpfs_cliente) == 1:
            lote["cpf"] = cpfs_cliente[0]
        elif not cpfs_cliente and cpfs_resp:
            lote["cpf"] = cpfs_resp[0]
        else:
            lote["cpf"] = None
        lote["cpfs_distintos"] = distintos
        # Nome: o da MESMA mensagem do CPF vence (pareamento sem risco de cruzar);
        # depois o primeiro IDENTIFICADO (ficha/cabeçalho); por fim o primeiro solto.
        # O grupo manda nome e CPF em mensagens separadas e, entre eles, sobra
        # conversa ('Link da procuração', 'Solteira') que não pode virar nome.
        par_cpf = []
        if lote.get("cpf"):
            par_cpf = (lote.get("nomes_por_cpf") or {}).get(lote["cpf"]) or []
        candidatos_nome = par_cpf or lote.get("nomes_ident") or lote["nomes"] or []
        # (PONTO 2.4) desempate: o nome ROTULADO (MENOR/GESTANTE/CLIENTE/…)
        # vence o nome sem rótulo — a ficha pode trazer o nome da mãe no
        # "Nome:" e o da menor em "= MENOR …"; o da MENOR é o nome do caso.
        rotulos = lote.get("nomes_rotulos") or {}
        if len(candidatos_nome) > 1 and rotulos:
            marcados = [n for n in candidatos_nome if rotulos.get(n)]
            if marcados:
                candidatos_nome = marcados + [n for n in candidatos_nome
                                              if not rotulos.get(n)]
        lote["nome"] = candidatos_nome[0] if candidatos_nome else None
        lote["qtd_arquivos"] = len(lote["file_ids"])
        lote.setdefault("ts_marcador", None)
        # identificação (independente do ciclo de vida do lote)
        if len(cpfs_cliente) > 1:
            lote["identificacao"] = "CONFLITO"
        elif not distintos:
            lote["identificacao"] = "SEM_CPF"
        elif lote["qtd_arquivos"] == 0:
            lote["identificacao"] = "SEM_ARQUIVOS"
        else:
            lote["identificacao"] = "OK"

        motivos = []
        if len(cpfs_cliente) > 1:
            motivos.append("CONFLITO: mais de um CPF de CLIENTE diferente no mesmo lote")
        elif not distintos:
            motivos.append("SEM_CPF: nenhum CPF identificado no lote")
        if lote["qtd_arquivos"] == 0:
            motivos.append("SEM_ARQUIVOS: lote só com texto")
        # F1-09: CPF com DV errado não é âncora nem CONFLITO, mas NUNCA some:
        # vira ocorrência de revisão (pode ser o CPF certo digitado errado ou
        # o de outra pessoa). Só a contagem vai no motivo (sem o número).
        invalidos = [c for c in (lote.get("cpfs_invalidos") or []) if c not in distintos]
        lote["cpfs_invalidos"] = invalidos
        if invalidos:
            motivos.append(f"CPF_INVALIDO: {len(invalidos)} número(s) com cara de CPF e "
                           f"dígito verificador errado no lote — conferir")

        # BARREIRA DE IDENTIDADE no pós-processamento (14/10/2026 — FASE 1).
        # O comentário antigo dizia "kit incompleto e misturado -> NUNCA vai para
        # a triagem": está ERRADO. O intruso não entra no lote (a mensagem vai
        # para `pendentes`); o que fica é o kit do 1º cliente, que pode estar
        # COMPLETO — e aí ele sumia para sempre (pronto=False) sem ninguém ser
        # avisado, enquanto o bot respondia "Recebido". Agora:
        #   · kit COMPLETO e limpo (identificador + CPF + arquivo, sem evidência
        #     alheia) -> PRONTO e a ponte encaminha;
        #   · dúvida qualquer (kit incompleto, ou alguma mensagem carregou
        #     CPF/nome de outra pessoa ANTES do corte) -> NÃO pronto, com o
        #     motivo ANOTADO em motivos_revisao (antes ficava []) e alerta no
        #     stdout (contrato do Hermes: linha no stdout = mensagem no Telegram).
        interrompido = lote.get("status_lote") == "FECHADO_POR_TROCA_DE_PESSOA"
        kit_completo = (lote.get("identificacao") == "OK" and bool(lote.get("nome"))
                        and lote["qtd_arquivos"] > 0)
        intruso = _intruso_no_lote(lote) if interrompido else ""
        realocadas = lote.get("_realocadas") or []
        # (PONTO 2.3) se mensagens saíram do kit (fotos soltas realocadas para o
        # cliente novo), pode ter sobrado material do A no caminho — dúvida
        # qualquer -> NÃO vai sozinho, com o motivo anotado.
        if interrompido and kit_completo and not intruso and not realocadas:
            lote["_kit_pela_barreira"] = True
        elif interrompido:
            lote["_revisao_barreira"] = True
            porque_b = (f"bloco interrompido por outra pessoa ({lote.get('fechado_por')})"
                        if not intruso else
                        f"bloco interrompido por outra pessoa e misturado: {intruso}")
            if realocadas:
                porque_b += (f"; {len(realocadas)} mensagem(ns) sem identidade "
                             f"realocadas para fora do lote (coladas no corte)")
            motivos.append(porque_b)
            lote["_alerta_barreira"] = (
                f"🚨 {lote.get('lote_id') or '(sem id)'} NÃO vai para a triagem — {porque_b} "
                f"(nome={lote.get('nome') or '-'!r} CPF={cpf_formatado(lote.get('cpf')) if lote.get('cpf') else '-'} "
                f"arquivos={lote['qtd_arquivos']})")
        lote["motivos_revisao"] = motivos

        # status final: CONFLITO tem precedência sobre o ciclo de vida
        # (PONTO 2.4) só 2+ CPFs de CLIENTE é conflito — família não é.
        if len(cpfs_cliente) > 1:
            lote["status"] = "CONFLITO"
        elif lote.get("status_lote"):
            lote["status"] = lote["status_lote"]
        else:
            lote["status"] = "FECHADO_POR_JANELA"

        # 'pronto' = pode ir para a fase 2: identificado sem conflito E o ciclo de
        # vida do lote TERMINOU (foi fechado). DECISÃO DO DONO em 21/09/2026:
        # lote fechado pela JANELA de tempo também é PRONTO — o kit das 12:48 do
        # dia 21/09 chegou SEM traço nenhum e ficaria eternamente em revisão.
        # Lote ABERTO (traço sem fechamento e sem timeout) segue NÃO pronto:
        # ainda pode receber arquivo. CONFLITO/SEM_CPF/SEM_ARQUIVOS nunca é pronto.
        if lote.get("identificacao") != "OK":
            lote["pronto"] = False
            lote["pronto_por"] = None
        elif lote.get("_kit_pela_barreira"):
            # (14/10/2026) kit inteiro do 1º cliente, intruso fora: PRONTO.
            # Era o furo — este lote ficava pronto=False para sempre e era
            # invisível em TODOS os avisos.
            lote["pronto"] = True
            lote["pronto_por"] = ("kit completo (bloco interrompido por outra pessoa, "
                                  "mas o intruso não entrou no lote)")
        elif lote.get("status_lote") == "FECHADO_POR_TROCA_DE_PESSOA":
            # (22/09/2026) interrompido COM dúvida: só com olho humano.
            lote["pronto"] = False
            lote["pronto_por"] = None
            lote["_revisao_barreira"] = True
        elif lote.get("fechado_pelo_traco"):
            lote["pronto"] = True
            lote["pronto_por"] = "traço (fechamento)"
        elif lote.get("fechado"):
            lote["pronto"] = True
            lote["pronto_por"] = lote.get("fechado_por") or lote.get("status_lote") or "fechado"
        else:
            lote["pronto"] = False
            lote["pronto_por"] = None
        if lote.get("cpfs_invalidos") and lote.get("pronto"):
            lote["pronto"] = False               # F1-09: dúvida de identidade
            lote["pronto_por"] = None
        lote["requer_revisao"] = bool(lote.get("identificacao") != "OK"
                                      or lote.get("_revisao_barreira")
                                      or lote.get("cpfs_invalidos"))

        if lote.get("_revisao_barreira"):
            lote["confianca"] = "REVISAO (bloco interrompido: outra pessoa no meio)"
        elif lote.get("_kit_pela_barreira"):
            lote["confianca"] = "ALTA (kit completo — o intruso não entrou no lote)"
        elif lote.get("sinal_fechamento"):
            lote["confianca"] = "ALTA (fechado pelo traço)"
        elif lote["modo"] == "marcadores":
            lote["confianca"] = "MEDIA (aberto pelo traço, sem fechamento)"
        else:
            lote["confianca"] = "BAIXA (fallback janela de tempo)"
        lote["aberto"] = lote["status"] == "ABERTO"
    return lotes


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
def nome_ofuscado(nome: str) -> bool:
    """O Bitrix ofusca nomes de anexos do WhatsApp: '4AIY6mB-C5dZPySU_HYEww==.jpg'."""
    if not nome:
        return True
    base = os.path.splitext(nome)[0]
    if re.search(r"[=]{1,2}$", base):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_+\-]{16,}", base)) and "_" in base and not re.search(r"[aeiouAEIOU]{2,}", base)


def _nome_seguro(nome: str) -> str:
    nome = os.path.basename(nome or "").strip() or "arquivo"
    nome = re.sub(r"[^\w.\-]+", "_", nome, flags=re.UNICODE)
    return nome[:120] or "arquivo"


def sha256_de(caminho: str) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _meta_do_disco(pasta: str, indice: int, file_id: str) -> dict | None:
    """Meta de um arquivo já presente na pasta do lote, pelo índice "NN_".

    O baixar_pendentes grava "01_<nome>" ... "NN_<nome>" na pasta do lote. Se o
    arquivo do índice já está lá, devolve a meta completa (caminho/sha256/baixado)
    para o ciclo NÃO apagar o que o download já fez — mesmo quando o estado.json
    está corrompido por um ciclo anterior (337 lotes no caso Josilene).
    """
    try:
        if not os.path.isdir(pasta):
            return None
        prefixo = f"{int(indice):02d}_"
        candidatos = sorted(n for n in os.listdir(pasta)
                            if n.startswith(prefixo) and os.path.isfile(os.path.join(pasta, n))
                            and not _e_parcial(n) and not n.endswith(".meta.json")
                            and os.path.getsize(os.path.join(pasta, n)) > 0)
        if not candidatos:
            return None
        escolhido = os.path.join(pasta, candidatos[-1])
        return {"file_id": str(file_id), "indice": int(indice), "baixado": True,
                "erro": None, "nome_salvo": candidatos[-1], "caminho": escolhido,
                "tamanho_bits": os.path.getsize(escolhido), "sha256": sha256_de(escolhido),
                "fonte": "já em disco (ciclo auto-reparador)"}
    except OSError as e:
        log.warning("arquivo já em disco do file_id %s ilegível (%s): vai baixar de novo",
                    file_id, e)
        return None


def baixar_file_id(webhook: str, file_id: str, pasta: str, indice: int,
                   prefixo: str, force: bool = False) -> dict:
    """disk.file.get -> DOWNLOAD_URL -> baixa IMEDIATAMENTE (só o caminho do webhook).

    Compat: delega a `baixar_file_id_api` sem chave do VibeCode (mesmas regras de
    mídia, id legado, URL expirada, destino único e gravação atômica). Nunca levanta.
    """
    return baixar_file_id_api(None, file_id, pasta, indice, prefixo, force=force,
                              webhook=webhook or None)


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------
# Fila de retry do download_svc processada DENTRO do ciclo: rodada curta e limitada.
RETRY_LIMITE_ITENS = 5
RETRY_TETO_RODADA_S = 60


def _enfileirar_retry(file_id: str, destino: str | None, erro: str, *,
                      lote_id: str | None = None, indice: int | None = None,
                      nome: str | None = None) -> None:
    """Camada 3 — enfileira no microserviço de download (fila de retry).

    Chamado quando TODOS os caminhos de `baixar_file_id_api` falham: em vez de
    perder o arquivo, ele entra na fila persistente do download_svc e é
    re-tentado em background com backoff exponencial. Nunca levanta: falha de
    enfileiramento vira só um warning (não pode derrubar o lote).
    """
    if download_svc is None or not destino:
        return
    try:
        fila = download_svc.Fila(download_svc.DEFAULT_QUEUE_FILE)
        # D (pendência): lote_id/indice deixam o download_svc devolver o resultado ao
        # lote certo do estado.json (sha256/caminho) sem adivinhar pelo file_id.
        fila.enfileirar(file_id, destino, lote_id=lote_id, indice=indice, nome=nome)
        log.info("file %s enfileirado no retry (download_svc): %s", file_id, erro)
    except Exception as e:                        # noqa: BLE001
        log.warning("não enfileirou file %s no retry: %s", file_id, e)


def _fid_int(file_id):
    """file_id do Bitrix (numérico) como int — ou None se for id do BOT (string).

    🔴 04/11/2026 — anexos vindos do bot da triagem trazem file_id do Telegram
    (ex.: "CQACAgEAAxkBAAIG02rn2xwi..."). O código fazia int(file_id) e estourava
    "ValueError: invalid literal for int()", então esses arquivos NUNCA eram
    baixados. Quem é numérico continua indo para o disk.file.get; quem não é vai
    direto para a API do VibeCode, que aceita o id como string.
    """
    s = str(file_id or "").strip()
    return int(s) if s.isdigit() else None


ERRO_TELEGRAM_LEGADO = "telegram_legado"
ERRO_PERMANENTE = "permanente"
ERRO_TRANSITORIO = "transitorio"
ERROS_DEFINITIVOS = (ERRO_TELEGRAM_LEGADO, ERRO_PERMANENTE)
# classes de erro do disk.file.get/download que não mudam repetindo (arquivo apagado,
# grande demais, tipo inválido): o arquivo não volta para a fila de retry.
_CLASSES_PERMANENTES_ITEM = (rb.Classe.NAO_ENCONTRADO, rb.Classe.TAMANHO, rb.Classe.TIPO)


def _mime_de_tipo(tipo) -> str | None:
    """`files[].type` da API ("image", "audio", "video", "file"...) -> prefixo MIME."""
    t = str(tipo or "").strip().lower()
    if not t:
        return None
    if "/" in t:
        return t
    if t in ("audio", "voice", "voz"):
        return "audio/"
    if t == "video":
        return "video/"
    if t == "image":
        return "image/"
    return None


def classificar_anexo(nome: str | None, tipo_api=None) -> str:
    """'documento' | 'midia' | 'desconhecido' (fonte única: tipos_documento, C1)."""
    return tipos_documento.classificar(nome, _mime_de_tipo(tipo_api))


def _meta_midia(meta: dict, nome: str | None) -> dict:
    """Mídia (áudio/vídeo/voz) fica REGISTRADA no lote, mas não é baixada como
    documento, não conta como pendência e não vai para a fila de retry (C1)."""
    meta.update({"tipo": tipos_documento.MIDIA, "nome_original": nome or meta.get("nome_original"),
                 "baixado": False, "erro": None, "fonte": "midia: não baixada (não é documento)"})
    return meta


def _nome_destino(indice: int, nome: str, prefixo: str) -> tuple:
    """(nome_salvo, nome_derivado). SEMPRE com o prefixo do índice "NN_" (E3): dois
    anexos com o mesmo nome no lote nunca caem no mesmo arquivo. Mesma convenção do
    baixar_pendentes e dos reconciliadores."""
    ext = os.path.splitext(nome or "")[1].lower()
    if nome_ofuscado(nome):
        return f"{int(indice):02d}_{prefixo}{ext}", True
    return f"{int(indice):02d}_{_nome_seguro(nome)}", False


def _reaproveitavel(meta_ant: dict | None) -> bool:
    """Meta anterior do MESMO file_id aponta para arquivo íntegro em disco?"""
    if not isinstance(meta_ant, dict) or not meta_ant.get("baixado"):
        return False
    cam = meta_ant.get("caminho")
    try:
        return bool(cam) and not _e_parcial(cam) and os.path.isfile(cam) and os.path.getsize(cam) > 0
    except OSError:
        return False


def _disk_file_get(fid_n: int) -> dict:
    """disk.file.get pelo cliente REST único, circuito `bitrix-download` (B1/B2)."""
    r = bitrix_cliente.chamar("disk.file.get", {"id": fid_n}, circuito="bitrix-download")
    info = r.get("result") if isinstance(r, dict) else None
    if not isinstance(info, dict) or not info:
        raise RuntimeError("disk.file.get sem result")
    return info


def baixar_file_id_api(chave: str | None, file_id: str, pasta: str, indice: int,
                       prefixo: str, meta_hint: dict | None = None,
                       force: bool = False, webhook: str | None = None,
                       anterior: dict | None = None, ocupados: dict | None = None) -> dict:
    """
    Baixa um anexo do lote, IMEDIATAMENTE. Nunca levanta: devolve meta com 'erro'.

    Fontes, cada uma com o SEU disjuntor (B1 — um bloqueio do Bitrix não impede a
    VibeCode, e vice-versa):
      0) webhook do Bitrix: disk.file.get -> DOWNLOAD_URL (circuito `bitrix-download`).
         URL assinada expirada (401/403/404/410) -> pede disk.file.get NOVO (D3).
      1) GET {BASE}/v1/files/{fileId}/download com X-Api-Key (`vibecode-download`).
      2) GET {BASE}/v1/chats/files/{fileId} -> downloadUrl assinado, baixado na hora.
    Regras (C1/E3):
      - mídia (áudio/vídeo) é registrada com tipo='midia' e NÃO é baixada nem enfileirada;
      - id não numérico (Telegram legado, "CQAC…") nunca vai ao disk.file.get; se a
        VibeCode também não entrega, o erro é definitivo (`erro_tipo='telegram_legado'`);
      - destino `NN_<nome>` único no lote; reaproveita só arquivo do MESMO file_id
        (`anterior`) ou do mesmo destino ainda não reivindicado por outro file_id
        (`ocupados`: caminho -> file_id); `.parcial/.part` nunca contam;
      - gravação: tmp exclusivo -> tamanho>0 -> sha256 -> os.replace.
    """
    meta = {"file_id": str(file_id), "indice": indice, "nome_original": None,
            "nome_salvo": None, "caminho": None, "tamanho_bits": None,
            "sha256": None, "baixado": False, "erro": None, "obtido_em": None,
            "fonte": None}
    hints = meta_hint or {}
    ocupados = ocupados or {}
    fid_n = _fid_int(file_id)
    _migrar_circuito_legado()                 # lê o circuit-download.json legado uma vez
    nome = hints.get("nome") or ""
    tipo_api = hints.get("tipo")
    try:
        esperado = int(hints.get("tamanho") or 0)
    except (TypeError, ValueError):
        esperado = 0

    # C1: mídia pelo que a própria leitura já trouxe — sem nenhuma chamada de rede
    if classificar_anexo(nome, tipo_api) == tipos_documento.MIDIA:
        return _meta_midia(meta, nome)

    # mesmo file_id já baixado e íntegro (estável entre ciclos, independe do nome)
    if not force and _reaproveitavel(anterior):
        reuso = dict(anterior)
        if not reuso.get("sha256"):
            reuso["sha256"] = sha256_de(reuso["caminho"])
        reuso.update({"tamanho_bits": os.path.getsize(reuso["caminho"]), "reaproveitado": True,
                      "erro": None})
        reuso.setdefault("tipo", classificar_anexo(reuso.get("nome_original") or nome, tipo_api))
        return reuso
    # erro definitivo já registrado (id legado, arquivo apagado...): não martela de novo
    if not force and isinstance(anterior, dict) and anterior.get("erro_tipo") in ERROS_DEFINITIVOS:
        return dict(anterior)

    erros: list = []
    erro_bitrix = None
    info_disk = None
    usar_bitrix = bool(webhook) and fid_n is not None
    try:
        if not nome and usar_bitrix:
            try:
                info_disk = _disk_file_get(fid_n)
                nome = info_disk.get("NAME") or ""
                esperado = int(info_disk.get("SIZE") or esperado or 0)
            except rb.FalhaBitrix as e:
                erro_bitrix = e.erro
                erros.append(f"disk.file.get: {e}")
                usar_bitrix = False
            except CircuitOpenError as e:
                erros.append(f"webhook: {e}")
                usar_bitrix = False
            except Exception as e:                # noqa: BLE001
                erros.append(f"disk.file.get: {seguranca.mascarar(str(e))}")
                usar_bitrix = False
        if not nome and chave:
            try:
                r = api_get(chave, f"/v1/chats/files/{file_id}", rotulo=f"chats.files({file_id})",
                            tentativas=1, circuito="vibecode-download")
                info = (r.get("data") or {}) if isinstance(r, dict) and r.get("success") else {}
                nome = info.get("name") or ""
                tipo_api = tipo_api or info.get("type")
                esperado = int(info.get("size") or esperado or 0)
            except Exception as e:                # noqa: BLE001
                erros.append(f"chats.files: {seguranca.mascarar(str(e))}")
        meta["nome_original"] = nome
        tipo = classificar_anexo(nome, tipo_api)
        if tipo == tipos_documento.MIDIA:
            return _meta_midia(meta, nome)
        meta["tipo"] = tipo

        salvo, derivado = _nome_destino(indice, nome, prefixo)
        destino = os.path.join(pasta, salvo)
        meta.update({"nome_salvo": salvo, "nome_derivado": derivado, "caminho": destino,
                     "obtido_em": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})

        # ♻️ reuso por destino: o MESMO (índice, nome) já em disco e não reivindicado por
        # outro file_id do lote. (Antes: nome sem índice + "existe -> reaproveita" fazia o
        # 2º "CNH.pdf" apontar para o 1º — E3; e o laço por índice aceitava `.part`.)
        if not force:
            cands = [destino]
            if derivado:
                try:
                    pref = f"{int(indice):02d}_"
                    cands += [os.path.join(pasta, n) for n in sorted(os.listdir(pasta))
                              if n.startswith(pref) and n != salvo]
                except OSError:
                    pass
            for cam in cands:
                n = os.path.basename(cam)
                dono = ocupados.get(cam)
                if dono not in (None, str(file_id)):
                    continue
                if _e_parcial(n) or n.endswith(".meta.json"):
                    continue
                if os.path.isfile(cam) and os.path.getsize(cam) > 0:
                    meta.update({"nome_salvo": n, "caminho": cam, "tamanho_bits": os.path.getsize(cam),
                                 "sha256": sha256_de(cam), "baixado": True, "reaproveitado": True,
                                 "fonte": "já existia em disco"})
                    return meta

        baixado = False
        total = sha = None
        # 0) webhook do Bitrix (estável) — só aqui o disjuntor do Bitrix é consultado (B1)
        if usar_bitrix:
            for tentativa in range(1, 3):
                try:
                    if info_disk is None or tentativa > 1:
                        info_disk = _disk_file_get(fid_n)     # URL NOVA a cada tentativa (D3)
                    url = info_disk.get("DOWNLOAD_URL") or ""
                    info_disk = {}                           # URL assinada é de uso único
                    if not url:
                        raise RuntimeError("disk.file.get não devolveu DOWNLOAD_URL")
                    total, sha = _baixar_atomico(url, destino, url_assinada_bitrix=True,
                                                 circuito="bitrix-download")
                    meta["fonte"] = "webhook:disk.file.get"
                    baixado = True
                    break
                except rb.FalhaBitrix as e:
                    erros.append(f"webhook: {e}")
                    if e.erro.classe == rb.Classe.URL_EXPIRADA:
                        continue
                    erro_bitrix = e.erro
                    break
                except CircuitOpenError as e:
                    erros.append(f"webhook: {e}")
                    break
                except Exception as e:            # noqa: BLE001
                    erros.append(f"webhook: {seguranca.mascarar(str(e))}")
                    break
        elif fid_n is None:
            erros.append(f"{ERRO_TELEGRAM_LEGADO}: id não numérico — disk.file.get não se aplica")
        if not baixado and chave:
            try:
                # 1) binário direto pela API (sem URL para guardar).
                total, sha = _baixar_atomico(f"{VIBECODE_BASE}/v1/files/{file_id}/download", destino,
                                             tentativas=1, headers={"X-Api-Key": chave},
                                             circuito="vibecode-download")
                meta["fonte"] = "api:/v1/files/{id}/download"
                baixado = True
            except Exception as e1:               # noqa: BLE001
                erros.append(f"download direto: {seguranca.mascarar(str(e1))}")
        if not baixado and chave:
            try:
                # 2) downloadUrl assinado — mesmo endpoint, exige X-Api-Key.
                r = api_get(chave, f"/v1/chats/files/{file_id}", rotulo=f"chats.files({file_id})",
                            tentativas=2, circuito="vibecode-download")
                url = ((r.get("data") or {}) if isinstance(r, dict) else {}).get("downloadUrl") or ""
                if not url:
                    raise RuntimeError("sem downloadUrl")
                total, sha = _baixar_atomico(url, destino, headers={"X-Api-Key": chave},
                                             circuito="vibecode-download")
                meta["fonte"] = "api:downloadUrl"
                baixado = True
            except Exception as e2:               # noqa: BLE001
                erros.append(f"downloadUrl: {seguranca.mascarar(str(e2))}")
        if not baixado:
            raise RuntimeError(" | ".join(erros) or "sem caminho de download")
        meta.update({"baixado": True, "reaproveitado": False, "tamanho_bits": total, "sha256": sha,
                     "erro_tipo": None})
        if esperado and total != esperado:
            meta["aviso"] = f"tamanho baixado ({total}) != size da API ({esperado})"
            log.warning("file %s: %s", file_id, meta["aviso"])
    except Exception as e:                        # noqa: BLE001 - um arquivo não derruba o lote
        meta["baixado"] = False
        meta["erro"] = seguranca.mascarar(f"{type(e).__name__}: {e}")
        if fid_n is None:
            meta["erro_tipo"] = ERRO_TELEGRAM_LEGADO
        elif erro_bitrix is not None and not erro_bitrix.pausa_rodada and (
                erro_bitrix.permanente or erro_bitrix.classe in _CLASSES_PERMANENTES_ITEM):
            # Erro de SISTEMA (401 token revogado, OVERLOAD, limite) tem pausa_rodada=True: não é
            # culpa do arquivo -> fica transitório, senão uma credencial caída marcaria todos os
            # kits recentes como permanentes.
            # G-03: mesma régua do download_svc (rede_bitrix.classificar(...).permanente):
            # ACCESS_DENIED & cia. não melhoram repetindo -> não vão para a fila de retry.
            meta["erro_tipo"] = ERRO_PERMANENTE
        else:
            meta["erro_tipo"] = ERRO_TRANSITORIO
        log.error("erro no file %s: %s", file_id, meta["erro"])
        # Camada 3: só erro TRANSITÓRIO de id numérico vai para a fila de retry
        # (download_svc chama disk.file.get; id legado/permanente lá só martelaria).
        if meta["erro_tipo"] == ERRO_TRANSITORIO:
            _enfileirar_retry(file_id, meta.get("caminho"), meta["erro"],
                              lote_id=prefixo or None,
                              indice=indice if isinstance(indice, int) else None,
                              nome=meta.get("nome_salvo") or meta.get("nome_original"))
    return meta


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------
def estado_vazio() -> dict:
    return {"versao": 1, "cursor_eventos": 0, "cursor_msg_id": 0,
            "atualizado_em": None, "ultima_verificacao": None, "lotes": {},
            "arquivos_index": {}, "fonte": None, "reacoes_ignoradas": 0}


def carregar_estado() -> dict:
    """Estado atual via `estado_store.ler_estado` (F1-08).

    Ausente -> estado vazio. Corrompido -> `estado_store.EstadoCorrompido` (o ciclo
    ABORTA com erro claro e alerta; nunca "começa do zero", que reduzia milhares de
    lotes aos da janela). O store preserva uma cópia `.corrompido-<ts>` e não mexe
    no original.
    """
    try:
        est = estado_store.ler_estado(ARQ_ESTADO)
    except estado_store.EstadoCorrompido as e:
        log.error("estado.json CORROMPIDO (%s) — ciclo abortado sem gravar nada; "
                  "restaure de backups-estado/ ou corrija o arquivo (cópia: %s)",
                  ARQ_ESTADO, getattr(e, "copia", None))
        raise
    for k, v in estado_vazio().items():
        est.setdefault(k, v)
    return est


def serializar_lote(lote: dict, arquivos: list[dict] | None = None) -> dict:
    return {
        "lote_id": lote["lote_id"],
        "modo": lote.get("modo"),
        "status": lote["status"],
        "pronto": bool(lote.get("pronto")),
        "pronto_por": lote.get("pronto_por"),
        "requer_revisao": bool(lote.get("requer_revisao")),
        "identificacao": lote.get("identificacao"),
        "confianca": lote.get("confianca"),
        "sinal_abertura": lote.get("sinal_abertura"),
        "sinal_fechamento": lote.get("sinal_fechamento"),
        "motivos_revisao": lote["motivos_revisao"],
        "alerta": lote.get("_alerta_barreira"),
        "cpf": lote["cpf"],
        "cpf_formatado": cpf_formatado(lote["cpf"]) if lote["cpf"] else None,
        # (PONTO 2.4) o CPF da mãe/pai fica visível no estado e no painel — é o
        # contato do caso, e o `cpf` de cima pode ser o dele quando é o único.
        "cpf_responsavel": lote.get("cpf_responsavel"),
        "cpf_responsavel_formatado": (cpf_formatado(lote["cpf_responsavel"])
                                      if lote.get("cpf_responsavel") else None),
        "cpfs_responsavel": lote.get("cpfs_responsavel") or [],
        "cpfs_distintos": lote["cpfs_distintos"],
        "cpfs_invalidos": lote.get("cpfs_invalidos") or [],
        "nome": lote["nome"],
        "nomes_candidatos": lote["nomes"],
        "autores": lote["autores"],
        "inicio": lote["inicio"].isoformat() if lote["inicio"] else None,
        "fim": lote["fim"].isoformat() if lote["fim"] else None,
        "fechado": lote.get("fechado", False),
        "fechado_por": lote.get("fechado_por"),
        "marcador_fim_textual": bool(lote.get("marcador_fim")),
        "aberto": lote.get("aberto", False),
        "qtd_arquivos": lote["qtd_arquivos"],
        "file_ids": lote["file_ids"],
        "mensagens": [{"id": m["id"], "autor": m["autor"], "data": m["data"],
                       # S01: senha gov.br/INSS NUNCA entra no estado/backups/fila
                       "sinal": bool(m.get("sinal")),
                       "texto": seguranca.remover_senhas(m["texto"] or "")[:2000],
                       "realocada": bool(m.get("_realocada"))}
                      for m in lote["mensagens"]],
        "arquivos": arquivos or [],
        "pasta": os.path.join(DIR_LOTES, lote["lote_id"]),
        "visto_em": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Circuit breaker das fontes de leitura
# --------------------------------------------------------------------------
def _breaker_int(conf: dict, chave: str, padrao: int) -> int:
    try:
        return max(1, int(conf.get(chave) or padrao))
    except (TypeError, ValueError):
        return padrao


def _breaker_float(conf: dict, chave: str, padrao: float) -> float:
    try:
        return max(1.0, float(conf.get(chave) or padrao))
    except (TypeError, ValueError):
        return float(padrao)


def _ler_mensagens_com_breaker(chave, dialog, paginas, conf, webhook_fn, cursor=None, info=None):
    """Lê as mensagens aplicando o CIRCUIT BREAKER nas DUAS fontes.

    Devolve (mensagens, rotulo_da_fonte). Levanta RuntimeError se AMBAS as
    fontes estiverem abertas/indisponíveis (nada a ler).
    """
    n_falhas = _breaker_int(conf, "circuit_breaker_falhas", 3)
    t_seg = _breaker_float(conf, "circuit_breaker_timeout_s", 300.0)

    # B4: erro de programação nosso (TypeError, KeyError...), disjuntor interno aberto
    # (CircuitOpenError) e falha de item (404) NÃO contam contra a saúde da fonte.
    cb_api = CircuitBreaker("leitura.api", arquivo=ARQ_BREAKER,
                            max_falhas=n_falhas, timeout_s=t_seg,
                            conta_falha=rb.conta_falha_padrao)
    cb_webhook = CircuitBreaker("leitura.webhook", arquivo=ARQ_BREAKER,
                                max_falhas=n_falhas, timeout_s=t_seg,
                                conta_falha=rb.conta_falha_padrao)

    # 1) fonte primária: API do VibeCode (respeitando o disjuntor dela)
    try:
        mensagens = cb_api.chamar(
            lambda: buscar_mensagens_api(chave, dialog, max_paginas=paginas, cfg=conf)
            if not cursor else
            buscar_mensagens_api(chave, dialog,
                                 max_paginas=max(int(paginas or 0), F1_MAX_PAGINAS_INCREMENTAL),
                                 cfg=conf, ate_id=cursor, info=info))
        return mensagens, "api(vibecode):GET /v1/chats/{id}/messages"
    except CircuitOpenError:
        log.warning("breaker: API do VibeCode ABERTA — redirecionando para o webhook Bitrix")
    except Exception as e_api:                        # noqa: BLE001
        log.error("API do VibeCode falhou na leitura (%s); failover → webhook Bitrix", e_api)

    # 2) fonte alternativa: webhook do Bitrix (respeitando o disjuntor dela)
    w = webhook_fn()
    if not w:
        raise RuntimeError("API do VibeCode indisponível e webhook não descoberto")
    try:
        mensagens = cb_webhook.chamar(
            lambda: buscar_mensagens(w, dialog, cfg=conf,
                                     max_paginas=int(paginas or MSG_PAGINAS_HISTORICO))
            if not cursor else
            buscar_mensagens(w, dialog, cfg=conf, first_id=cursor, info=info))
    except CircuitOpenError:
        raise RuntimeError(
            "API do VibeCode falhou e o webhook Bitrix também está ABERTO (circuit breaker)")
    return mensagens, "webhook(bitrix):im.dialog.messages.get [FAILOVER]"


# --------------------------------------------------------------------------
# F1-01 / F1-02 — processamento INCREMENTAL por chat
# --------------------------------------------------------------------------
# Estado por chat:
#   est["cursores"][dialog]        maior id de mensagem JÁ INCORPORADA (lida e gravada)
#   est["f1_regiao"][dialog]       {"inicio": id, "mensagens": [...]} — a REGIÃO
#                                  PROVISÓRIA: mensagens do bloco ABERTO e da janela
#                                  ainda em formação (classificadas, senha removida).
# Cada ciclo agrupa: região provisória + mensagens com id > cursor. Lote fechado sai
# da região e nunca mais é reagrupado. A presença de f1_regiao[dialog] marca que o
# chat já está no modo incremental (1ª execução = leitura antiga limitada).
def _msg_para_estado(m: dict) -> dict:
    d = {k: v for k, v in m.items() if k != "ts" and not str(k).startswith("_")}
    for campo in ("texto", "texto_bruto"):
        if d.get(campo):
            d[campo] = seguranca.remover_senhas(str(d[campo]))[:4000]
    return d


def _msg_do_estado(d: dict) -> dict:
    m = dict(d)
    m["ts"] = _parse_data(m.get("data"))
    return m


def _texto_msg(m: dict) -> str:
    return str(m.get("texto_bruto") or m.get("texto") or "")


def _tem_material(lote: dict, mensagens: list | None = None) -> bool:
    """Kit = arquivo OU identidade (CPF, ficha, cabeçalho). Sem nada disso não há o
    que triar: o lote não é criado (F1-02 — lotes artificiais/vazios)."""
    msgs = (lote.get("mensagens") or []) if mensagens is None else mensagens
    if mensagens is None and (lote.get("file_ids") or lote.get("cpfs")
                              or lote.get("cpfs_distintos")):
        return True
    for m in msgs:
        if m.get("file_ids") or m.get("cpfs") or m.get("e_identificador"):
            return True
        t = _texto_msg(m)
        if t and (e_abertura_ou_ficha(t) or tem_nome_cabecalho(t)):
            return True
    return False


def _fim_do_lote(lote: dict):
    fim = lote.get("fim") or lote.get("inicio")
    if isinstance(fim, _dt.datetime):
        return fim if fim.tzinfo else fim.replace(tzinfo=_dt.timezone.utc)
    return _parse_data(fim) if fim else None


def _lote_provisorio(lote: dict, agora: _dt.datetime, janela_min: int) -> bool:
    """Ainda pode mudar com a próxima mensagem: bloco ABERTO (o agrupar já fechou por
    timeout o que venceu) ou lote de JANELA cuja janela ainda não terminou."""
    if str(lote.get("status") or lote.get("status_lote") or "").upper() == "ABERTO":
        return True
    if lote.get("modo") == "janela":
        fim = _fim_do_lote(lote)
        return fim is None or (agora - fim) <= _dt.timedelta(minutes=int(janela_min))
    return False


def _final_no_estado(reg) -> bool:
    if not isinstance(reg, dict):
        return False
    st = str(reg.get("status") or "").upper()
    return st not in ("", "ABERTO") and not reg.get("aberto")


def _filtrar_lotes_incrementais(lotes: list[dict], mensagens: list[dict], est: dict,
                                dialog: str, conf: dict, agora: _dt.datetime,
                                resumo: dict) -> tuple[list[dict], int | None]:
    """Decide o que este ciclo GRAVA e onde começa a região provisória.

    Devolve (lotes_a_gravar, inicio_regiao). Regras (F1-02):
      · lote provisório de JANELA não é gravado (a janela não terminou);
      · lote sem material (sem arquivo, CPF, ficha ou cabeçalho) não é criado;
      · lote que já existe FECHADO no estado não é reescrito (fechado não reagrupa);
      · lote NOVO que reaproveita mensagens de lote já FECHADO: se o que sobra não tem
        material, não é criado; se tem, nasce em REVISÃO (nunca enfileirável sozinho).
    """
    janela_min = int(conf.get("janela_min_fallback") or JANELA_MIN_PADRAO)
    lotes_estado = est.get("lotes") or {}
    incorporadas: dict = {}
    for lid, reg in lotes_estado.items():
        if not _final_no_estado(reg) or str(reg.get("status") or "").upper() == "IGNORADO":
            continue
        for m in reg.get("mensagens") or []:
            if isinstance(m, dict) and m.get("id") is not None:
                incorporadas.setdefault(m["id"], lid)
    inicio_regiao = None
    gravar: list[dict] = []
    descartes = {"janela_em_formacao": 0, "sem_material": 0, "ja_fechado": 0,
                 "ja_incorporado": 0, "sobreposto_revisao": 0}
    for lote in lotes:
        prov = _lote_provisorio(lote, agora, janela_min)
        if prov and lote.get("mensagens"):
            ids = [m.get("id") for m in lote["mensagens"] if isinstance(m.get("id"), int)]
            if ids:
                inicio_regiao = min(ids) if inicio_regiao is None else min(inicio_regiao, min(ids))
        if prov and lote.get("modo") == "janela":
            descartes["janela_em_formacao"] += 1
            continue
        if not _tem_material(lote):
            descartes["sem_material"] += 1
            continue
        lid = lote.get("lote_id")
        if lid in lotes_estado:
            if _final_no_estado(lotes_estado[lid]):
                descartes["ja_fechado"] += 1
                continue
            gravar.append(lote)
            continue
        alheias = {incorporadas[m.get("id")] for m in lote.get("mensagens") or []
                   if m.get("id") in incorporadas and incorporadas[m.get("id")] != lid}
        if alheias:
            sobra = [m for m in lote.get("mensagens") or [] if m.get("id") not in incorporadas]
            if not _tem_material(lote, sobra):
                descartes["ja_incorporado"] += 1
                continue
            motivo = ("F1-02: lote reaproveita mensagens já incorporadas em lote fechado ("
                      + ", ".join(sorted(alheias))[:200] + ") — conferir antes de encaminhar")
            novo = estado_lote.marcar_revisao(lote, motivo)
            lote.update({"motivos_revisao": novo.get("motivos_revisao") or [motivo],
                         "requer_revisao": True, "pronto": False, "pronto_por": None})
            descartes["sobreposto_revisao"] += 1
        gravar.append(lote)
    resumo["f1_descartes"] = descartes
    return gravar, inicio_regiao


# --------------------------------------------------------------------------
# Ciclo
# --------------------------------------------------------------------------
def ciclo(cfg: argparse.Namespace) -> dict:
    """Um ciclo completo. Devolve um resumo (útil para testes e para o loop).

    FONTE PRIMÁRIA das mensagens: API do VibeCode (vê as mensagens novas do
    grupo). O webhook do Bitrix ficou só como (a) diagnóstico, com
    --fonte-webhook, e (b) ÚLTIMO recurso de download, se a API falhar.
    """
    chave = ler_chave_bot()
    est = carregar_estado()
    _migrar_circuito_legado()
    conf = carregar_config()
    if not conf.get("sinal_traco"):
        log.warning("config.sinal_traco vazio — nenhum traço será reconhecido; "
                    "agrupando SÓ pelo fallback de janela de %s min", conf.get("janela_min_fallback"))
    bot_cfg = ler_bot_config()
    dialog = dialogos(conf, bot_cfg, cfg.dialog_id)[0]

    resumo = {"dialog": dialog, "cursor_antes": est.get("cursor_eventos", 0),
              "eventos": 0, "lotes": [], "dry_run": cfg.dry_run, "erros": [],
              "fonte": "webhook(bitrix):im.dialog.messages.get" if cfg.fonte_webhook
                       else "api(vibecode):GET /v1/chats/{id}/messages",
              "sinal_traco": "".join(sorted(conf.get("sinal_traco") or [])),
              "modo_traco": modo_traco(conf),
              "toggle_por_autor": (modo_traco(conf) == "autor")}

    # O webhook (fonte antiga) é descoberto SOB DEMANDA — nunca é fonte primária.
    estado_webhook = {"url": None, "tentou": False}

    def webhook():
        if not estado_webhook["tentou"]:
            estado_webhook["tentou"] = True
            try:
                estado_webhook["url"] = descobrir_webhook()
            except Exception as e:                 # noqa: BLE001
                log.warning("webhook indisponível (%s); downloads usam só a API", e)
                estado_webhook["url"] = ""
        return estado_webhook["url"] or None

    # 1) eventos do bot (cursor persistido)
    try:
        eventos, novo_cursor = ler_eventos(chave, est.get("cursor_eventos", 0))
        resumo["eventos"] = len(eventos)
        for ev in eventos[:5]:
            log.info("evento %s %s (%s)", ev.get("eventId"), ev.get("type"), ev.get("date"))
        est["cursor_eventos"] = novo_cursor
        resumo["cursor_depois"] = novo_cursor
        log.info("eventos: %d novos (cursor %s -> %s)", len(eventos),
                 resumo["cursor_antes"], novo_cursor)
    except Exception as e:                        # noqa: BLE001
        resumo["erros"].append(f"eventos: {e}")
        log.error("falha ao ler eventos: %s", e)

    # F1-01: cursor por chat. Só vale quando o chat já está no modo incremental
    # (f1_regiao[dialog] existe); senão é a 1ª execução: leitura antiga limitada.
    regiao_ant = (est.get("f1_regiao") or {}).get(dialog)
    incremental = isinstance(regiao_ant, dict) and not getattr(cfg, "reler_janela", False)
    cursor_ant = int((est.get("cursores") or {}).get(dialog) or 0) if incremental else 0
    info_leitura: dict = {}
    resumo["cursor_chat_antes"] = cursor_ant or None
    resumo["incremental"] = bool(incremental and cursor_ant)

    # 2) mensagens do grupo — FONTE NOVA (API do VibeCode)
    try:
        paginas = cfg.paginas or int(conf.get("historico_paginas") or MSG_PAGINAS_HISTORICO)
        if cfg.fonte_webhook or str(conf.get("fonte_leitura") or "api").lower() == "webhook":
            w = webhook()
            if not w:
                raise RuntimeError("webhook não descoberto (--fonte-webhook)")
            # F1-06: a 1ª leitura/releitura (sem cursor) respeita historico_paginas
            # (antes eram sempre 40 páginas = 2.000 mensagens por chat).
            if cursor_ant:
                mensagens = buscar_mensagens(w, dialog, cfg=conf, first_id=cursor_ant,
                                             info=info_leitura)
            else:
                mensagens = buscar_mensagens(w, dialog, cfg=conf, max_paginas=paginas,
                                             info=info_leitura)
            # F1-06: em produção (config.json) o webhook é a fonte PRIMÁRIA, não
            # diagnóstico; o bot do traço (pergunta_traco) lê pela API — divergência
            # documentada no relatório C2b.
            resumo["fonte"] = ("webhook(bitrix):im.dialog.messages.get"
                               + (" [--fonte-webhook]" if cfg.fonte_webhook else
                                  " [config fonte_leitura=webhook]"))
        else:
            # CIRCUIT BREAKER: desativa a fonte que falhar N vezes seguidas por
            # T segundos e redireciona para a outra (em vez de tentar a fonte
            # morta a cada ciclo). O failover abaixo continua valendo.
            mensagens, fonte = _ler_mensagens_com_breaker(
                chave, dialog, paginas, conf, webhook, cursor=cursor_ant or None,
                info=info_leitura)
            resumo["fonte"] = fonte
    except Exception as e:                        # noqa: BLE001
        # paginação interrompida/erro: nada é gravado e o cursor NÃO anda (F1-01)
        resumo["erros"].append(f"mensagens: {e}")
        log.error("falha ao ler mensagens (cursor do chat %s fica em %s): %s",
                  dialog, cursor_ant, e)
        return resumo
    lacuna = bool(info_leitura.get("lacuna"))
    if lacuna:
        txt_lacuna = (f"LACUNA na leitura do chat {dialog}: as páginas acabaram antes de "
                      f"alcançar o cursor {cursor_ant} — mensagens no meio podem não ter sido "
                      f"lidas; cursor NÃO avança (rode com --paginas maior)")
        resumo["erros"].append(txt_lacuna)
        resumo["lacuna"] = True
        log.error(txt_lacuna)
        try:
            _alertar_linha("⚠️ Triagem: " + txt_lacuna[:200])
        except Exception as e_al:                  # noqa: BLE001
            log.error("alerta de LACUNA não saiu (%s) — a lacuna segue em resumo/log", e_al)
    if info_leitura.get("truncada"):
        resumo["leitura_truncada"] = True
    mensagens = [m for m in mensagens if not (cursor_ant and isinstance(m.get("id"), int)
                                              and m["id"] <= cursor_ant)]
    ids_novos = [m["id"] for m in mensagens if isinstance(m.get("id"), int)]
    resumo["mensagens_novas"] = len(ids_novos)
    # bloco aberto entre ciclos: a região provisória volta para o agrupamento
    if incremental:
        por_id = {}
        for d in (regiao_ant.get("mensagens") or []):
            if isinstance(d, dict) and d.get("id") is not None:
                por_id[d["id"]] = _msg_do_estado(d)
        for m in mensagens:
            if m.get("id") is not None:
                por_id[m["id"]] = m            # versão nova (edição) vence
        mensagens = [por_id[k] for k in sorted(por_id, key=lambda x: (not isinstance(x, int), x))]
        resumo["mensagens_regiao"] = len(regiao_ant.get("mensagens") or [])

    # limita ao que ainda não foi consumido, se solicitado
    if cfg.desde_msg_id:
        mensagens = [m for m in mensagens if m["id"] and m["id"] > cfg.desde_msg_id]

    # REAÇÕES são descartadas AQUI, antes do agrupamento e antes de qualquer lote
    reacoes = [m for m in mensagens if m.get("reacao")]
    mensagens = [m for m in mensagens if not m.get("reacao")]
    resumo["mensagens_lidas"] = len(mensagens)
    resumo["reacoes_ignoradas"] = len(reacoes)
    resumo["reacoes_ids"] = [m["id"] for m in reacoes]
    hoje = _dt.datetime.now().date()
    resumo["mensagens_hoje"] = sum(1 for m in mensagens if dia_local(m.get("ts")) == hoje)
    if reacoes:
        log.info("reações ignoradas (não viram lote): %d %s", len(reacoes),
                 [m["id"] for m in reacoes][:12])

    log.info("mensagens lidas: %d (hoje: %d)", len(mensagens), resumo["mensagens_hoje"])
    orfaos: list[dict] = []
    reacoes_agrupador: list[dict] = []
    # janela do fallback: CLI sobrepõe o config
    if cfg.janela_min:
        conf = dict(conf)
        conf["janela_min_fallback"] = cfg.janela_min
    # 'agora' importa: é o que fecha por TIMEOUT o lote de traço que ficou aberto
    # e ninguém fechou (o kit que chegou dentro de um bloco aberto vira pronto
    # por timeout em vez de ficar eternamente ABERTO).
    lotes = agrupar(mensagens, conf, agora=_dt.datetime.now(_dt.timezone.utc),
                    orfaos_out=orfaos, reacoes_out=reacoes_agrupador)
    # F1-02: só grava o que mudou de verdade e nunca cria lote artificial
    _agora_f1 = _dt.datetime.now(_dt.timezone.utc)
    lotes, inicio_regiao = _filtrar_lotes_incrementais(lotes, mensagens, est, dialog, conf,
                                                       _agora_f1, resumo)
    n_prontos = sum(1 for l in lotes if l.get("pronto"))
    log.info("lotes formados: %d (prontos por sinal de FIM: %d, sinais de FIM órfãos: %d)",
             len(lotes), n_prontos, len(orfaos))
    if orfaos:
        resumo["sinais_fim_orfaos"] = orfaos

    # metadados de anexo que já vieram na própria leitura (nome/tamanho)
    hints_arquivos: dict = {}
    for m in mensagens:
        for a in (m.get("arquivos_info") or []):
            if (a.get("nome") or a.get("tipo")) and a["file_id"] not in hints_arquivos:
                hints_arquivos[a["file_id"]] = {"nome": a.get("nome"), "tamanho": a.get("tamanho"),
                                                "tipo": a.get("tipo")}

    # --- DELTA deste ciclo: só o que ESTE ciclo mexeu entra no estado, para não
    # sobrescrever o que outro processo (telegram_grupo) gravou no meio do ciclo.
    lotes_criados: dict = {}
    idx_arquivos: dict = {}

    # webhook do Bitrix descoberto UMA vez por ciclo (estável para download de
    # anexos; a API do VibeCode está instável no /v1/files/{id}/download — 06/10/2026).
    webhook_download = None
    if not cfg.dry_run:
        try:
            webhook_download = webhook()
        except Exception:
            webhook_download = None

    # ♻️/🚦 POLÍTICA DE DOWNLOAD DO CICLO (27/10/2026)
    # O ciclo re-tentava baixar arquivo de TODO lote do histórico (2000 mensagens
    # → ~170-230 lotes). Quando vários falhavam, um único ciclo passava de 30 min
    # baixando com o trava_de_ciclo travado: o bot anunciava "lote registrado" e
    # NADA chegava à fila (caso Sabrina, 27/10). Agora:
    #   · lote RECENTE (fim/início há <= 3 dias): baixa no ciclo, até o teto;
    #   · lote ANTIGO: não baixa aqui — quem preenche buraco é o
    #     baixar_pendentes.py no pipeline de 10 min (idempotente, por disco).
    _agora_dl = _dt.datetime.now(_dt.timezone.utc)
    MAX_DOWNLOADS_CICLO = int(os.environ.get("TRIAGEM_MAX_DOWNLOADS_CICLO") or 120)
    downloads_ciclo = 0

    def _lote_recente_dl(lote: dict) -> bool:
        ts = _parse_data(lote.get("fim") or lote.get("inicio"))
        if ts is None:
            return False
        if ts.tzinfo is None:                        # mesma convenção de dia_local: ingênuo = UTC
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return (_agora_dl - ts) <= _dt.timedelta(days=3)

    _lotes_estado = est.get("lotes") or {}
    for lote in lotes:
        arquivos: list[dict] = []
        pasta = os.path.join(DIR_LOTES, lote["lote_id"])
        # 🔴 04/11/2026 — o ciclo RE-MONTAVA a lista de arquivos de todo lote do
        # histórico. Nos lotes adiados (antigo/teto) entrava um placeholder
        # "baixado: false" e ISSO SOBRESCREVIA o caminho/sha256 que o
        # baixar_pendentes já tinha gravado — o lote aparecia sem arquivo mesmo
        # com os PDFs no disco (337 lotes nessa situação, caso Josilene).
        _anteriores = {str(a.get("file_id")): a
                       for a in ((_lotes_estado.get(lote["lote_id"]) or {}).get("arquivos") or [])}
        # E3: caminho -> file_id já reivindicado no lote. Caminho compartilhado por DOIS
        # file_ids no estado (legado do bug "CNH.pdf") não serve de reuso para ninguém.
        _contagem_cam: dict = {}
        for _f, _a in _anteriores.items():
            if _a.get("caminho"):
                _contagem_cam[_a["caminho"]] = _contagem_cam.get(_a["caminho"], 0) + 1
        ocupados = {c: f for f, a in _anteriores.items()
                    if (c := a.get("caminho")) and _contagem_cam.get(c) == 1}
        if lote["file_ids"] and not cfg.dry_run:
            os.makedirs(pasta, exist_ok=True)
            for i, fid in enumerate(lote["file_ids"], 1):
                _hint = hints_arquivos.get(str(fid)) or {}
                _ant = _anteriores.get(str(fid))
                if _ant and _contagem_cam.get(_ant.get("caminho"), 0) > 1:
                    _ant = None                           # caminho dividido (E3 legado): rebaixa
                # C1: mídia não é documento — registrada, nunca baixada nem adiada
                if classificar_anexo(_hint.get("nome"), _hint.get("tipo")) == tipos_documento.MIDIA:
                    arquivos.append(_meta_midia({"file_id": str(fid), "indice": i, "baixado": False,
                                                 "erro": None, "caminho": None, "sha256": None},
                                                _hint.get("nome")))
                    continue
                # 🚦 TETO DE DOWNLOADS POR CICLO (27/10/2026): antes o ciclo
                # re-tentava o arquivo de TODOS os lotes do histórico; quando
                # muitos falhavam, um único ciclo passava de 30 min baixando e
                # NENHUM lote novo chegava à fila (o bot anunciava "lote
                # registrado" e nada aparecia no painel). Buraco antigo é do
                # baixar_pendentes.py (pipeline de 10 min), não do ciclo.
                if (not _lote_recente_dl(lote)) or downloads_ciclo >= MAX_DOWNLOADS_CICLO:
                    ja = _ant or {}
                    if _reaproveitavel(ja) or ja.get("erro_tipo") in ERROS_DEFINITIVOS:
                        arquivos.append(dict(ja))       # já está no disco / definitivo: NÃO apaga
                        continue
                    # 🔴 04/11/2026 — o estado pode estar corrompido de um ciclo
                    # anterior (337 lotes com arquivo no disco e estado vazio). Em
                    # vez de confiar no snapshot, recupera direto do DISCO: o
                    # baixar_pendentes grava "NN_<nome>" na pasta do lote. Assim o
                    # ciclo é auto-reparador e não reescreve placeholder por cima.
                    do_disco = _meta_do_disco(pasta, i, str(fid))
                    if do_disco:
                        arquivos.append(do_disco)
                        continue
                    arquivos.append({"file_id": str(fid), "indice": i, "baixado": False,
                                     "erro": "adiado (lote antigo / teto do ciclo) — baixar_pendentes preenche",
                                     "nome_salvo": None, "caminho": None,
                                     "tamanho_bits": None, "sha256": None})
                    continue
                m = baixar_file_id_api(chave, fid, pasta, i, lote["lote_id"],
                                       meta_hint=hints_arquivos.get(str(fid)),
                                       force=cfg.force, webhook=webhook_download,
                                       anterior=_ant, ocupados=ocupados)
                if not m.get("reaproveitado") and m.get("tipo") != tipos_documento.MIDIA:
                    downloads_ciclo += 1
                if m.get("caminho"):
                    ocupados[m["caminho"]] = str(fid)
                arquivos.append(m)
                idx_arquivos[str(fid)] = {
                    "lote_id": lote["lote_id"], "sha256": m.get("sha256"),
                    "path": m.get("caminho"), "nome": m.get("nome_salvo"),
                    "nome_original": m.get("nome_original"),
                    "tamanho": m.get("tamanho_bits"), "erro": m.get("erro"),
                    "fonte": m.get("fonte"),
                }
        elif lote["file_ids"] and cfg.dry_run:
            for i, fid in enumerate(lote["file_ids"], 1):
                arquivos.append({"file_id": fid, "indice": i, "baixado": False,
                                 "acao": "BAIXARIA", "destino_pasta": pasta})

        reg = serializar_lote(lote, arquivos)
        reg["dialog_id"] = dialog          # de qual grupo veio (multi-chat)
        lotes_criados[lote["lote_id"]] = reg
        resumo["lotes"].append(reg)

    ids_lidos = [m["id"] for m in list(mensagens) + list(reacoes)
                 if isinstance(m.get("id"), int)]
    cursor_msg_id_ciclo = max(ids_lidos + [int(est.get("cursor_msg_id") or 0)])
    info_dialogo = None
    cursor_do_grupo = None
    # F1-01: região provisória a persistir junto com o cursor (mesma transação)
    regiao_nova = None
    if not lacuna:
        if inicio_regiao is None:
            regiao_msgs = []
        else:
            regiao_msgs = [_msg_para_estado(m) for m in mensagens
                           if isinstance(m.get("id"), int) and m["id"] >= inicio_regiao]
        if len(regiao_msgs) > F1_MAX_REGIAO:
            log.warning("região provisória do chat %s com %d mensagens (> %d): guardando as "
                        "%d mais novas", dialog, len(regiao_msgs), F1_MAX_REGIAO, F1_MAX_REGIAO)
            regiao_msgs = regiao_msgs[-F1_MAX_REGIAO:]
        regiao_nova = {"inicio": inicio_regiao, "mensagens": regiao_msgs,
                       "em": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
        resumo["regiao_provisoria"] = len(regiao_msgs)
    if ids_lidos and not lacuna:            # cursor POR GRUPO (multi-chat)
        cursor_do_grupo = max(ids_lidos + [cursor_ant])
    if ids_lidos:
        info_dialogo = {"mensagens": resumo.get("mensagens_lidas"),
                        "hoje": resumo.get("mensagens_hoje"),
                        "lotes": len(resumo["lotes"]),
                        "em": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    agora_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    config_sinais = {"sinal_traco": conf.get("sinal_traco"),
                     "modo_traco": modo_traco(conf),
                     "toggle_por_autor": conf.get("toggle_por_autor"),
                     "timeout_lote_min": conf.get("timeout_lote_min"),
                     "janela_min_fallback": conf.get("janela_min_fallback")}
    # VARREDURA DE ESTADO (24/09/2026): lote ABERTO cujas mensagens já saíram da janela
    # de leitura nunca é reavaliado pelo timeout — fica ABERTO para sempre no estado
    # (caso real: LOTE-SIG-20260922-1845-fcd74515, 50h). Fecha aqui, sem rede.
    try:
        from lotes_regras import fechamento_por_timeout, lotes_vencidos

        # a varredura olha o estado REAL (com os lotes deste ciclo já aplicados)
        estado_varredura = {"lotes": {**(est.get("lotes") or {}), **lotes_criados}}
        agora_varredura = _dt.datetime.now(_dt.timezone.utc)
        vencidos = lotes_vencidos(estado_varredura, agora=agora_varredura,
                                 timeout_min=int(conf.get("timeout_lote_min") or 120))
        for v in vencidos:
            # F1-10: MESMA regra do agrupar (lotes_regras.fechamento_por_timeout): kit
            # completo fecha PRONTO; incompleto vai para REVISÃO com motivo. Nunca mais
            # pronto=False + requer_revisao=False (lote invisível).
            reg_atual = lotes_criados.get(v["lote_id"]) or \
                (est.get("lotes") or {}).get(v["lote_id"]) or {}
            fechamento = fechamento_por_timeout(reg_atual, v["motivo"], agora_varredura)
            reg = lotes_criados.get(v["lote_id"])
            if isinstance(reg, dict):
                reg.update(fechamento)
            elif isinstance((est.get("lotes") or {}).get(v["lote_id"]), dict):
                # lote fora da janela: o delta é SÓ o fechamento (patch por campo), nunca
                # o registro antigo inteiro (que apagaria o que outros donos gravaram — A3)
                lotes_criados[v["lote_id"]] = dict(fechamento)
            else:
                continue
            log.warning("lote %s estava ABERTO há %s min — fechado por timeout na varredura",
                        v["lote_id"], v["minutos"])
        resumo["lotes_vencidos"] = [v["lote_id"] for v in vencidos]
    except Exception as e:                            # noqa: BLE001 — nunca derruba o ciclo
        log.warning("varredura de lotes vencidos falhou: %s", e)

    # GRAVAÇÃO SOB TRAVA (24/09/2026): relê o estado e aplica SÓ o delta deste
    # ciclo. Antes, `salvar_estado(est)` gravava a cópia lida no INÍCIO do ciclo e
    # apagava o que o telegram_grupo tivesse escrito no meio (foram 6 lotes do
    # Telegram perdidos no teste das 17:57). Ver estado_store.py.
    if cfg.dry_run:
        return resumo

    # A3/F1-03: os lotes entram por MERGE POR CAMPO com dono "triagem"
    # (estado_store.atualizar_lotes): CPF/nome completados pela identificação, marcas
    # de duplicata da ponte, motivos de revisão e downloads com sha256 sobrevivem ao
    # ciclo. Lote novo entra inteiro. Depois, numa 2ª transação, cursores e índices
    # (se cair entre as duas, o cursor só não avança — o próximo ciclo relê).
    if lotes_criados:
        estado_store.atualizar_lotes(lotes_criados, "triagem", caminho=ARQ_ESTADO)

    def _aplicar(fresco: dict) -> None:
        if idx_arquivos:
            fresco.setdefault("arquivos_index", {}).update(idx_arquivos)
        if cursor_do_grupo is not None:
            fresco.setdefault("cursores", {})[dialog] = max(
                int(cursor_do_grupo), int((fresco.get("cursores") or {}).get(dialog) or 0))
        if regiao_nova is not None:
            fresco.setdefault("f1_regiao", {})[dialog] = regiao_nova
        resumo["cursor_chat_depois"] = (fresco.get("cursores") or {}).get(dialog)
        if info_dialogo is not None:
            fresco.setdefault("dialogos", {})[dialog] = info_dialogo
        fresco["cursor_msg_id"] = max(int(cursor_msg_id_ciclo or 0),
                                      int(fresco.get("cursor_msg_id") or 0))
        fresco["cursor_eventos"] = max(int(est.get("cursor_eventos") or 0),
                                       int(fresco.get("cursor_eventos") or 0))
        fresco["fonte"] = resumo["fonte"]
        fresco["reacoes_ignoradas"] = resumo["reacoes_ignoradas"]
        fresco["ultima_verificacao"] = agora_iso
        fresco["config_sinais"] = config_sinais

    estado_store.atualizar_estado(_aplicar, caminho=ARQ_ESTADO)

    # Camada 3: processa a fila de retry do download_svc (re-tenta, com backoff,
    # os downloads que falharam no fluxo principal e foram enfileirados acima).
    # Nunca derruba o ciclo.
    if download_svc is not None and webhook_download:
        try:
            fila_retry = download_svc.Fila(download_svc.DEFAULT_QUEUE_FILE)
            if fila_retry.pendentes(time.time()):
                try:
                    chave_vibecode = download_svc.ler_chave_vibecode()
                except Exception:                      # noqa: BLE001
                    chave_vibecode = chave
                # D (pendência): rodada CURTA dentro do ciclo — no máx. 5 itens e 60 s;
                # o resto fica para o próximo ciclo (o download_svc respeita o backoff).
                processados = download_svc.processar_fila(
                    fila_retry, webhook_download, chave_vibecode,
                    limite_itens=RETRY_LIMITE_ITENS, teto_rodada_s=RETRY_TETO_RODADA_S)
                resumo["retry_download"] = len(processados)
                for it in processados:
                    if it.get("estado") == "baixado":
                        log.info("retry baixou file %s (fonte=%s)", it.get("file_id"), it.get("fonte"))
                    else:
                        log.warning("retry file %s continua %s (tent %s): %s",
                                    it.get("file_id"), it.get("estado"),
                                    it.get("tentativas"), it.get("erro"))
        except Exception as e:                        # noqa: BLE001
            log.warning("fila de retry (download_svc) falhou: %s", e)

    return resumo


def imprimir_resumo(resumo: dict) -> None:
    tag = "DRY-RUN" if resumo["dry_run"] else "EXECUÇÃO REAL"
    print("=" * 78)
    print(f"[{tag}] grupo={resumo['dialog']}  eventos={resumo.get('eventos', 0)}  "
          f"cursor={resumo.get('cursor_antes')} -> {resumo.get('cursor_depois')}")
    print(f"fonte de leitura: {resumo.get('fonte')}")
    print(f"mensagens lidas: {resumo.get('mensagens_lidas', '?')}  "
          f"(de hoje: {resumo.get('mensagens_hoje', '?')})  "
          f"REAÇÕES ignoradas: {resumo.get('reacoes_ignoradas', 0)}")
    print(f"sinal de traço: {resumo.get('sinal_traco')!r}  "
          f"modo_traco={resumo.get('modo_traco')}  "
          f"({MODOS_TRACO.get(resumo.get('modo_traco') or '', '?')})")
    print("=" * 78)
    if not resumo["lotes"]:
        print("nenhum lote formado.")
    for l in resumo["lotes"]:
        print(f"\n● {l['lote_id']}  modo={l['modo']}  status={l['status']}  "
              f"ident={l['identificacao']}  arquivos={l['qtd_arquivos']}  pronto={l['pronto']}"
              f"{' (por: ' + str(l['pronto_por']) + ')' if l.get('pronto_por') else ''}")
        print(f"   confiança: {l['confianca']}   fechado_por={l['fechado_por']}  aberto={l['aberto']}")
        if l.get("sinal_abertura"):
            print(f"   ▸ TRAÇO abre  msg={l['sinal_abertura']['message_id']} "
                  f"autor={l['sinal_abertura']['autor']} ts={l['sinal_abertura']['ts']}")
        if l.get("sinal_fechamento"):
            print(f"   ▪ TRAÇO fecha msg={l['sinal_fechamento']['message_id']} "
                  f"autor={l['sinal_fechamento']['autor']} ts={l['sinal_fechamento']['ts']}")
        print(f"   nome: {l['nome']}")
        print(f"   cpf : {l['cpf_formatado']}  ({l['cpfs_distintos']})")
        print(f"   autor: {l['autores']}   janela: {l['inicio']} -> {l['fim']}")
        if l["motivos_revisao"]:
            print(f"   ⚠ revisão: {l['motivos_revisao']}")
        if l.get("alerta"):
            print(f"   {l['alerta']}")
        for a in l["arquivos"]:
            if a.get("baixado"):
                print(f"     - file {a['file_id']} -> {a.get('nome_salvo')} "
                      f"({a.get('tamanho_bits')} B) sha256={str(a.get('sha256'))[:16]}…")
            elif a.get("erro"):
                print(f"     - file {a['file_id']} ERRO: {a['erro']}")
            else:
                print(f"     - file {a['file_id']} (BAIXARIA para {a.get('destino_pasta')})")
    if resumo.get("erros"):
        print("\nERROS:", resumo["erros"])
    print()


# --------------------------------------------------------------------------
# Testes embutidos
# --------------------------------------------------------------------------
def testar_cpf() -> int:
    casos = [
        ("165.207.179-24", ["16520717924"]),
        ("513.350.652_02", ["51335065202"]),
        ("CPF 000.000.000-00", []),   # F1-09: rotulado mas sem DV válido não é CPF
        ("= CPF   165.207.179-24", ["16520717924"]),
        ("=CPF:976.754.364-31", ["97675436431"]),
        ("= CPF MÃE DA MENOR:900.849.399-31", ["90084939931"]),
        ("texto sem CPF nenhum aqui", []),
        ("NIS 123456789012345 (não é CPF)", []),
        ("CPF 165.207.179-24 e CPF 513.350.652-02", ["16520717924", "51335065202"]),
        # ⚠️ regressão real de 21/09/2026: telefone com DV válido virando CPF do lote
        ("CLIENTE\nContato: 43726975978\nContatos reservas: 45265993100",
         []),
        ("Contatos reservas:  9300000001\nE-mail: x@y.com", []),
        ("Contato 1: 18 90000-0000", []),
        ("Telefones: 13360274017 / 85547118401", []),
        ("= CPF 599.957.527-35", ["59995752735"]),
    ]
    print("=" * 78)
    print("TESTE DO EXTRATOR DE CPF")
    print("=" * 78)
    falhas = 0
    for entrada, esperado in casos:
        obtido = extrair_cpfs(entrada)
        ok = obtido == esperado
        falhas += 0 if ok else 1
        print(f"[{'OK ' if ok else 'FALHA'}] {entrada!r}\n        esperado={esperado}\n        obtido  ={obtido}")
    print("-" * 78)
    print(f"casos={len(casos)} falhas={falhas}")
    print("amostra de plausibilidade (dígito verificador):")
    for c in ["16520717924", "51335065202", "00000000000", "97675436431", "90084939931"]:
        print(f"   {cpf_formatado(c)} -> plausivel={cpf_plausivel(c)}")
    return 1 if falhas else 0


def testar_sinais() -> int:
    """Testa o reconhecimento do TRAÇO e o TOGGLE por autor."""
    cfg = dict(CONFIG_PADRAO)
    # F1-07 (C3): os casos-base deste autoteste foram escritos para o modo
    # 'grupo_autor' (o antigo padrão). O padrão passou a ser 'delimitador' (uso
    # real); o modo fica EXPLÍCITO aqui para o autoteste não mudar de regra em
    # silêncio. 'delimitador' é coberto em testes/test_traco_delimitador.py.
    cfg["modo_traco"] = "grupo_autor"

    def M(id_, autor, ts, texto="", files=None):
        return {"id": id_, "author_id": hash(autor) % 100000, "date": ts,
                "text": f"[B]{autor}:[/B] {texto}".strip(),
                "params": {"FILE_ID": files or []}}

    def A(autor, ts, texto="", files=None, id_=None):
        A.n += 1
        return classificar_mensagem(M(id_ or 1000 + A.n, autor, ts, texto, files), {}, cfg)

    A.n = 0
    casos = []

    # ---------- 1) reconhecimento do traço (casos pedidos pelo dono) ----------
    # Regra do dono (confirmada): QUALQUER quantidade de traços é sinal — não
    # existe quantidade "certa"; 1, 2, 3 ou "==" são todos sinal, e alternam.
    reconhece = [
        ("—", True), ("--", True), ("———", True), ("==", True), ("=", True),
        ("-", True), ("–", True), ("–", True), ("— –", True), ("-\u200b", True),
        ("044.194.998-34", False), ("21/09/2026", False), ("= CPF 044.194.998-34", False),
        ("= DELISE AREDES SANTOS DA SILVA", False), ("dkakddsad", False), ("", False),
    ]
    for texto, esperado in reconhece:
        t = f"[B]Ana:[/B] {texto}"
        obtido = e_sinal_traco(t, cfg)
        casos.append((f"traço {texto!r} -> {esperado}", obtido == esperado))

    # ---------- 2) toggle: abre, fecha, abre de novo ----------
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "==="),                                  # abre
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA DA SILVA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "", files=[2, 3]),
        A("Ana", "2026-09-20T10:00:20+00:00", "—"),                                    # fecha
        A("Ana", "2026-09-20T10:00:30+00:00", "-"),                                    # abre de novo
        A("Ana", "2026-09-20T10:00:35+00:00", "= JOAO SOUZA\n= CPF 513.350.652_02", files=[4]),
    ]
    # 'agora' fixo logo após o fim: o lote 2 deve continuar ABERTO (senão o
    # timeout de 120 min o fecharia, pois os dados sintéticos são do dia anterior)
    agora = _dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")
    ls = agrupar(msgs, cfg, agora=agora)
    casos.append(("1º traço abre / 2º fecha / 3º abre", len(ls) == 2))
    if len(ls) == 2:
        casos.append(("lote 1 FECHADO_PRONTO com 3 arquivos do cliente certo",
                      ls[0]["status"] == "FECHADO_PRONTO" and ls[0]["pronto"]
                      and ls[0]["qtd_arquivos"] == 3 and ls[0]["cpf"] == "16520717924"))
        casos.append(("lote 2 ABERTO com os arquivos seguintes",
                      ls[1]["status"] == "ABERTO" and not ls[1]["pronto"]
                      and ls[1]["qtd_arquivos"] == 1 and ls[1]["cpf"] == "51335065202"))
        casos.append(("sinal de abertura/fechamento guardados com message_id",
                      bool(ls[0]["sinal_abertura"]["message_id"])
                      and bool(ls[0]["sinal_fechamento"]["message_id"])))
    else:
        casos.append(("lote 1 FECHADO_PRONTO com 3 arquivos do cliente certo", False))
        casos.append(("lote 2 ABERTO com os arquivos seguintes", False))
        casos.append(("sinal de abertura/fechamento guardados com message_id", False))

    # ---------- 3) REGRA DO MEIO (padrão do dono): abre com traço de qualquer um,
    # ------------- fecha só com o traço de QUEM ABRIU ----------
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),                 # Ana ABRE o bloco
        A("Bruno", "2026-09-20T10:00:10+00:00", "", files=[100]),   # fotos de OUTRO autor entram
        A("Bruno", "2026-09-20T10:00:20+00:00", "", files=[200]),
        A("Bruno", "2026-09-20T10:00:30+00:00", "-"),               # traço de OUTRO: NÃO fecha
        A("Ana", "2026-09-20T10:00:40+00:00", "-"),                 # traço de QUEM ABRIU: FECHA
        A("Ana", "2026-09-20T10:00:50+00:00", "", files=[300]),     # depois do fim -> fallback
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    sig = [l for l in ls if l["modo"] == "marcadores"]
    casos.append(("regra do meio: fotos do grupo entram e só o traço de quem abriu fecha",
                  len(sig) >= 1 and sig[0]["status"] == "FECHADO_PRONTO"
                  and sig[0]["qtd_arquivos"] == 2
                  and set(sig[0]["autores"]) == {"Ana", "Bruno"}
                  and sig[0]["sinal_fechamento"]["autor"] == "Ana"))
    casos.append(("traço de outro autor NÃO fecha o bloco (nem abre um paralelo)",
                  len([l for l in ls if l["modo"] == "marcadores"
                       and l["status"] == "FECHADO_PRONTO"]) == 1))

    # ---------- 3a2) bloco órfão VENCE e o traço de outra pessoa assume ----------
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),                 # Ana abre e NÃO fecha
        A("Ana", "2026-09-20T10:01:00+00:00", "", files=[500]),
        A("Bruno", "2026-09-20T10:20:00+00:00", "-"),               # 19 min: bloco ainda quente -> ignorado
        A("Bruno", "2026-09-20T11:00:00+00:00", "-"),               # 59 min sem movimento -> assume
        A("Bruno", "2026-09-20T11:05:00+00:00", "", files=[600]),
        A("Bruno", "2026-09-20T11:10:00+00:00", "-"),               # fecha o bloco que ele abriu
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T11:20:00+00:00"))
    sig = sorted([l for l in ls if l["modo"] == "marcadores"], key=lambda x: x["inicio"])
    casos.append(("bloco órfão vence (traco_vigencia_min) e o traço de outro autor assume",
                  len(sig) == 2
                  and sig[0]["status"] == "FECHADO_POR_INATIVIDADE"
                  and sig[0]["qtd_arquivos"] == 1
                  and sig[1]["status"] == "FECHADO_PRONTO"
                  and sig[1]["sinal_fechamento"]["autor"] == "Bruno"
                  and sig[1]["qtd_arquivos"] == 1))

    # ---------- 3b) modo 'grupo': QUALQUER traço fecha ----------
    cfg_grupo = dict(cfg)
    cfg_grupo["modo_traco"] = "grupo"
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Bruno", "2026-09-20T10:00:10+00:00", "", files=[100]),
        A("Bruno", "2026-09-20T10:00:20+00:00", "-"),               # fecha aqui no modo 'grupo'
    ]
    ls = agrupar(msgs, cfg_grupo, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    sig = [l for l in ls if l["modo"] == "marcadores"]
    casos.append(("modo 'grupo': traço de outro autor fecha o bloco",
                  len(sig) == 1 and sig[0]["status"] == "FECHADO_PRONTO"
                  and sig[0]["sinal_fechamento"]["autor"] == "Bruno"))

    # ---------- 3c) modo POR AUTOR (legado, modo_traco='autor') ----------
    cfg_autor = dict(cfg)
    cfg_autor["modo_traco"] = "autor"
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),                 # Ana abre
        A("Bruno", "2026-09-20T10:00:10+00:00", "-"),               # Bruno abre (independente)
        A("Ana", "2026-09-20T10:00:20+00:00", "", files=[100]),
        A("Bruno", "2026-09-20T10:00:30+00:00", "", files=[200]),
        A("Ana", "2026-09-20T10:00:40+00:00", "-"),                 # fecha a Ana
        A("Bruno", "2026-09-20T10:00:50+00:00", "", files=[201]),
    ]
    ls = agrupar(msgs, cfg_autor, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    ana = [l for l in ls if l["autores"] == ["Ana"]]
    bru = [l for l in ls if l["autores"] == ["Bruno"]]
    casos.append(("modo POR AUTOR (legado) segue independente por autor",
                  len(ana) == 1 and ana[0]["status"] == "FECHADO_PRONTO"
                  and len(bru) == 1 and bru[0]["status"] == "ABERTO"
                  and bru[0]["qtd_arquivos"] == 2))

    # ---------- 3c) NORMA DO DONO: lote fechado pela JANELA com CPF+arquivos é PRONTO ----------
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "= MARIA DA SILVA\n= CPF 165.207.179-24", files=[400]),
        A("Ana", "2026-09-20T10:45:00+00:00", "", files=[401]),     # 45 min depois -> fecha a janela
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T11:20:00+00:00"))
    jn = [l for l in ls if l["modo"] == "janela"]
    casos.append(("lote fechado pela JANELA, com 1 CPF + arquivos, é PRONTO (decisão do dono)",
                  len(jn) == 2 and jn[0]["identificacao"] == "OK" and jn[0]["pronto"]
                  and jn[0]["pronto_por"] and "janela" in str(jn[0]["pronto_por"]).lower()
                  and not jn[1]["pronto"]))
    casos.append(("lote ABERTO (traço sem fechamento) continua NÃO pronto",
                  not any(l["pronto"] for l in ls if l["status"] == "ABERTO")))

    # ---------- 4) timeout fecha lote esquecido ----------
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:01:00+00:00", "", files=[300]),
        A("Ana", "2026-09-20T14:30:00+00:00", "mensagem qualquer depois"),
    ]
    ls = agrupar(msgs, cfg)
    sig = [l for l in ls if l["modo"] == "marcadores"]
    casos.append(("esquecer o traço de fechar => FECHADO_POR_TIMEOUT",
                  len(sig) == 1 and sig[0]["status"] == "FECHADO_POR_TIMEOUT"
                  and not sig[0]["pronto"]))

    # ---------- 5) o texto do traço não vira NOME/CPF ----------
    A.n = 0
    a = A("Ana", "2026-09-20T10:00:00+00:00", "—")
    casos.append(("traço não entra na extração de NOME/CPF",
                  a["sinal"] and a["cpfs"] == [] and a["nomes"] == []
                  and not a["e_identificador"]))

    # ---------- 5b) PONTO 2.6: rótulo de BENEFÍCIO não vira nome ----------
    for rot in ("= PENSÃO POR MORTE", "= AUXÍLIO DOENÇA", "= SALÁRIO MATERNIDADE",
                "= BPC/LOAS", "= APOSENTADORIA", "= LOAS", "= BPC",
                "= APOSENTADORIA POR IDADE", "= AUXÍLIO DOENÇA ACIDENTÁRIO"):
        casos.append((f"2.6 rótulo de benefício NÃO vira nome: {rot!r}",
                      extrair_nomes(rot) == []))
    casos.append(("2.6 BPC/LOAS solto (sem '=') também não vira nome",
                  extrair_nomes("BPC/LOAS") == []))
    # rótulo + nome real: o NOME real é extraído, o rótulo não
    casos.append(("2.6 '*LOAS :ROSANA FICTICIA*' preserva o nome real",
                  extrair_nomes("*LOAS :ROSANA FICTICIA*") == ["ROSANA FICTICIA"]))
    casos.append(("2.6 'LOAS :ROSANA FICTICIA' preserva o nome real",
                  extrair_nomes("LOAS :ROSANA FICTICIA") == ["ROSANA FICTICIA"]))
    casos.append(("2.6 '= BPC :ROSANA FICTICIA' preserva o nome real",
                  extrair_nomes("= BPC :ROSANA FICTICIA") == ["ROSANA FICTICIA"]))
    # nomes reais que CONTÊM a palavra de benefício continuam passando
    casos.append(("2.6 nome real 'FULANO BENEFICIO DA SILVA' continua passando",
                  extrair_nomes("= FULANO BENEFICIO DA SILVA") == ["FULANO BENEFICIO DA SILVA"]))
    casos.append(("2.6 nome real 'AUXILIADORA TESTE LIMA' continua passando",
                  extrair_nomes("= AUXILIADORA TESTE LIMA") == ["AUXILIADORA TESTE LIMA"]))
    casos.append(("2.6 nome real 'SALGADO TESTE' continua passando",
                  extrair_nomes("= SALGADO TESTE") == ["SALGADO TESTE"]))
    # integração: rótulo de benefício + nome real num lote -> o nome real vence
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= PENSÃO POR MORTE\n= ROSANA FICTICIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    casos.append(("2.6 rótulo de benefício + nome real: o lote fica com o nome real",
                  len(ls) == 1 and ls[0].get("nome") == "ROSANA FICTICIA"))
    # rótulo de benefício sozinho NÃO dispara barreira falsa
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= LOAS\n= ROSANA FICTICIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "= BPC/LOAS"),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    casos.append(("2.6 rótulo de benefício NÃO dispara barreira falsa (1 lote, sem intruso)",
                  len(ls) == 1 and ls[0].get("nome") == "ROSANA FICTICIA"
                  and not ls[0].get("_revisao_barreira")
                  and "outra pessoa" not in str(ls[0].get("fechado_por") or "")))
    # só rótulo de benefício, sem nome real -> lote SEM nome
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= BPC/LOAS", files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    casos.append(("2.6 só rótulo de benefício: lote fica SEM nome",
                  len(ls) == 1 and not ls[0].get("nome")))

    # ---------- 6) BARREIRA DE IDENTIDADE: documento de OUTRA pessoa ----------
    # (22/09/2026 — pedido do dono: "garantir que não ocorra inserção de
    # documentos de outras pessoas"). Antes: os 2 kits caíam no MESMO lote e
    # viravam CONFLITO. Agora o bloco é INTERROMPIDO no 2º CPF: o primeiro
    # cliente NÃO vai para a triagem (fechado sem traço) e o material do
    # segundo segue sozinho, fechado pelo traço dele, com o CPF dele.
    #
    # C3 (onda 4): estes casos falhavam desde o tarball original por causa da
    # FIXTURE, não da barreira. O cabeçalho do 2º cliente era '= JOAO' (4
    # letras); a regra vigente de nome (nome_regras.parece_ruido: 5..80
    # caracteres) recusa nome < 5 letras, então não havia cabeçalho, o traço
    # final não fechava o kit do 2º (abria lote vazio) e as expectativas
    # "2 lotes" e "o 2º fecha pelo traço" contradiziam a regra atual. A barreira
    # em si já funcionava (CPFs separados, nenhum CONFLITO). Fixture trocada
    # para um nome completo ('= JOAO PEDRO'), como no cabeçalho real do grupo.
    # O caso '= JOAO' (nome curto + CPF válido no cabeçalho) segue registrado
    # como xfail estrito em testes/test_config_consumidores.py.
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "= JOAO PEDRO\n= CPF 513.350.652-02", files=[2]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")),
                key=lambda x: x["inicio"])
    maria = [l for l in ls if l.get("cpf") == "16520717924"]
    joao = [l for l in ls if l.get("cpf") == "51335065202"]
    casos.append(("barreira: documento de outra pessoa NÃO entra no bloco do 1º "
                  "(nada de lote com 2 CPFs)",
                  len(ls) == 2 and len(maria) == 1 and len(joao) == 1
                  and all(l["identificacao"] != "CONFLITO" for l in ls)))
    # (14/10/2026, FASE 1) o kit COMPLETO do interrompido não pode sumir mais:
    # a barreira desviou o intruso (ele NÃO entra) e o que ficou é o kit inteiro
    # do 1º cliente -> PRONTO, a ponte encaminha. Antes ficava pronto=False para
    # sempre e invisível em todos os avisos (o furo: "kit incompleto e misturado"
    # estava errado — o intruso nem entrou e o kit estava completo).
    casos.append(("barreira: kit COMPLETO do interrompido vira PRONTO "
                  "(intruso fora; identificador+CPF+arquivo presentes)",
                  bool(maria) and not maria[0]["fechado_pelo_traco"]
                  and maria[0]["pronto"] and not maria[0]["requer_revisao"]
                  and maria[0]["identificacao"] == "OK"
                  and maria[0]["qtd_arquivos"] == 1
                  and not maria[0]["motivos_revisao"]))
    casos.append(("barreira: o lote do 2º fecha pelo traço dele",
                  bool(joao) and joao[0]["fechado_pelo_traco"] and joao[0]["pronto"]
                  and joao[0]["status"] == "FECHADO_PRONTO"
                  and joao[0]["qtd_arquivos"] == 1))
    casos.append(("barreira: o kit do 1º cliente fica inteiro no lote dele "
                  "(nenhum arquivo do outro entra)",
                  bool(maria) and [str(f) for f in maria[0]["file_ids"]] == ["1"]
                  and bool(joao) and [str(f) for f in joao[0]["file_ids"]] == ["2"]))

    # ---------- 6a) interrompido SEM kit completo: fora da triagem, mas com o
    # ------------- motivo ANOTADO (antes motivos_revisao ficava [] e o lote
    # ------------- sumia sem rastro — o monitor nem sabia que ele existia).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24"),  # sem arquivo
        A("Ana", "2026-09-20T10:00:10+00:00", "= JOAO\n= CPF 513.350.652-02", files=[2]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")),
                key=lambda x: x["inicio"])
    maria2 = [l for l in ls if l.get("cpf") == "16520717924"]
    casos.append(("barreira: interrompido SEM kit completo fica FORA com o motivo ANOTADO "
                  "(motivos_revisao deixa de ficar vazio)",
                  bool(maria2) and not maria2[0]["pronto"]
                  and maria2[0]["requer_revisao"] and maria2[0]["qtd_arquivos"] == 0
                  and any("interrompido" in str(m).lower() for m in maria2[0]["motivos_revisao"])
                  and any("SEM_ARQUIVOS" in str(m) for m in maria2[0]["motivos_revisao"])))

    # ---------- 6b / C9) PONTO 2.2: o nome alheio AGORA é barrado ANTES de
    # ------------- entrar (a barreira por nome corta mesmo quando o bloco já
    # ------------- tem CPF). Antes o intruso entrava e o kit saía "MISTURADO".
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:08+00:00", "= JOAO PAULO SOUZA"),  # barrado ANTES de entrar
        A("Ana", "2026-09-20T10:00:10+00:00", "= CARLOS\n= CPF 513.350.652-02", files=[2]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")),
                key=lambda x: x["inicio"])
    maria3 = [l for l in ls if l.get("cpf") == "16520717924"]
    joao3 = [l for l in ls if "JOAO PAULO SOUZA" in (l.get("nomes") or [])]
    carlos3 = [l for l in ls if l.get("cpf") == "51335065202"]
    casos.append(("C9) nome alheio é barrado ANTES de entrar: kit da Maria limpo, "
                  "PRONTO, sem revisão e sem CONFLITO",
                  bool(maria3) and maria3[0]["pronto"]
                  and not maria3[0]["requer_revisao"]
                  and maria3[0]["qtd_arquivos"] == 1
                  and not maria3[0]["motivos_revisao"]
                  and "JOAO PAULO SOUZA" not in (maria3[0].get("nomes") or [])
                  and not maria3[0].get("_revisao_barreira")
                  and all(l.get("identificacao") != "CONFLITO" for l in ls)))
    casos.append(("C9b) o nome alheio vira lote próprio sem arquivo "
                  "(não se mistura com ninguém)",
                  bool(joao3) and joao3[0].get("cpf") is None
                  and joao3[0].get("nomes") == ["JOAO PAULO SOUZA"]
                  and joao3[0]["qtd_arquivos"] == 0
                  and bool(carlos3) and carlos3[0]["status"] == "FECHADO_PRONTO"))

    # ---------- 6b2 / C9c) o MESMO recorte da barreira, com o formato que o
    # ------------- CORPUS REAL usa: a FICHA "FICHA DE ATENDIMENTO / Nome: ..."
    # ------------- (o '/' do spec é quebra de linha na mensagem real). Os
    # ------------- fixtures de cima cobriam só o cabeçalho rotulado '= NOME';
    # ------------- a auditoria partia da FICHA (ressalva (a) do spec 6.2).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00",
          "FICHA DE ATENDIMENTO\nNome: JOAO SOUZA", files=[2]),   # ficha alheia
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")),
                key=lambda x: x["inicio"])
    mariaf = [l for l in ls if l.get("cpf") == "16520717924"]
    joaof = [l for l in ls if "JOAO SOUZA" in (l.get("nomes") or [])]
    casos.append(("C9c) formato FICHA do corpus extrai o nome da ficha "
                  "('FICHA DE ATENDIMENTO / Nome: JOAO SOUZA' e 'Nome completo:')",
                  extrair_nomes("FICHA DE ATENDIMENTO\nNome: JOAO SOUZA") == ["JOAO SOUZA"]
                  and extrair_nomes("FICHA DE ATENDIMENTO\nNome completo: JOAO SOUZA")
                  == ["JOAO SOUZA"]))
    casos.append(("C9c) com o formato FICHA a barreira também corta: "
                  "documento alheio NÃO entra no bloco do 1º",
                  bool(mariaf) and [str(f) for f in mariaf[0]["file_ids"]] == ["1"]
                  and "JOAO SOUZA" not in (mariaf[0].get("nomes") or [])
                  and all(l.get("identificacao") != "CONFLITO" for l in ls)))
    casos.append(("C9c) a ficha alheia vira lote próprio com o nome da FICHA "
                  "(o arquivo dela segue com ela)",
                  bool(joaof) and joaof[0].get("nomes") == ["JOAO SOUZA"]
                  and [str(f) for f in joaof[0]["file_ids"]] == ["2"]))

    # ---------- C1-C8) PONTO 2.2: helper compartilhado `_e_outra_pessoa` ----------
    # C1) a JANELA corta por NOME mesmo quando o lote já tem CPF (antes o ramo de
    #     nome só rodava quando NENHUM dos lados tinha CPF).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:05+00:00", "= JOAO PAULO SOUZA"),
    ]
    ls = agrupar_por_janela(msgs, 30)
    casos.append(("C1) janela corta por NOME mesmo com CPF no lote",
                  len(ls) == 2 and ls[0].get("nomes") == ["MARIA"]
                  and ls[1].get("nomes") == ["JOAO PAULO SOUZA"]))

    # C2) corta quando a msg tem CPF e o lote só tem nome (antes exigia os dois
    #     lados sem CPF para olhar nome).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "= JOAO PAULO SOUZA"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
    ]
    ls = agrupar_por_janela(msgs, 30)
    casos.append(("C2) janela corta quando a msg tem CPF e o lote só tem nome",
                  len(ls) == 2 and ls[0].get("nomes") == ["JOAO PAULO SOUZA"]
                  and ls[1].get("cpfs") == ["16520717924"]))

    # C3) nome parcial NÃO corta ("MARIA" x "MARIA SILVA" é a mesma pessoa).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "= MARIA SILVA"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA"),
    ]
    ls = agrupar_por_janela(msgs, 30)
    casos.append(("C3) nome parcial NÃO corta (MARIA x MARIA SILVA)", len(ls) == 1))

    # C4) foto sem texto segue o cliente NOVO (não volta para o anterior).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:05+00:00", "= JOAO PAULO SOUZA"),
        A("Ana", "2026-09-20T10:00:07+00:00", "", files=[2]),
    ]
    ls = agrupar_por_janela(msgs, 30)
    casos.append(("C4) foto sem texto segue o cliente NOVO (não volta pro anterior)",
                  len(ls) == 2 and len(ls[0].get("file_ids") or []) == 1
                  and len(ls[1].get("file_ids") or []) == 1
                  and ls[1].get("nomes") == ["JOAO PAULO SOUZA"]))

    # C5) pendentes não remix cliente depois da barreira: o material que sobrou
    #     não volta para o lote já fechado do 1º cliente.
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:08+00:00", "= JOAO PAULO SOUZA"),   # barreira
        A("Ana", "2026-09-20T10:00:10+00:00", "", files=[2]),           # foto solta
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    maria5 = [l for l in ls if l.get("cpf") == "16520717924"]
    casos.append(("C5) pendentes não remix cliente depois da barreira "
                  "(arquivo solto não volta pro lote fechado)",
                  bool(maria5) and [str(f) for f in maria5[0]["file_ids"]] == ["1"]))
    # (6.2, ressalva b) destino do arquivo solto CONGELADO: ele não volta pro
    # lote da MARIA e também não fica órfão — o lote do JOAO o absorve (é o que
    # a auditoria via: o file solto terminava no lote do 2º cliente).
    joao5 = [l for l in ls if "JOAO PAULO SOUZA" in (l.get("nomes") or [])]
    casos.append(("C5b) o arquivo solto TERMINA no lote do JOAO "
                  "(absorvido pelo cliente novo; não some nem volta pro da MARIA)",
                  bool(joao5) and [str(f) for f in joao5[0]["file_ids"]] == ["2"]
                  and bool(maria5) and [str(f) for f in maria5[0]["file_ids"]] == ["1"]))

    # C6) o bloco do traço no fim NÃO atravessa cliente: a ficha da MARIA fecha
    #     com o traço; o JOAO (que veio depois) fica de fora, em pendentes.
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "DADOS NECESSARIOS PARA DAR A ENTRADA"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:08+00:00", "= JOAO PAULO SOUZA"),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    maria6 = [l for l in ls if l.get("cpf") == "16520717924"]
    joao6 = [l for l in ls if "JOAO PAULO SOUZA" in (l.get("nomes") or [])]
    casos.append(("C6) bloco do traço no fim NÃO atravessa cliente "
                  "(ficha+arquivo da MARIA; JOAO fica de fora)",
                  bool(maria6) and maria6[0]["fechado_pelo_traco"]
                  and maria6[0]["qtd_arquivos"] == 1
                  and "JOAO PAULO SOUZA" not in (maria6[0].get("nomes") or [])
                  and bool(joao6) and joao6[0]["qtd_arquivos"] == 0))

    # C7) GUARDA: MENOR/MÃE com o MESMO CPF não é dividido e a MENOR vence
    #     (regra de pé do dono — não reabrir).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "= MENOR MARIA SILVA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "= CPF 165.207.179-24"),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    casos.append(("C7) MENOR/MÃE com o mesmo CPF: lote único e o nome da MENOR vence",
                  len(ls) == 1 and ls[0].get("nome") == "MARIA SILVA"
                  and ls[0].get("cpf") == "16520717924"))

    # C8) a barreira corta por NOME quando o bloco tem só nome (sem CPF nenhum).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA SILVA", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "= JOAO PAULO SOUZA", files=[2]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")),
                key=lambda x: x["inicio"])
    maria8 = [l for l in ls if "MARIA SILVA" in (l.get("nomes") or [])]
    joao8 = [l for l in ls if "JOAO PAULO SOUZA" in (l.get("nomes") or [])]
    casos.append(("C8) barreira corta por NOME quando o bloco tem só nome",
                  len(ls) >= 2 and bool(maria8) and bool(joao8)
                  and "JOAO PAULO SOUZA" not in (maria8[0].get("nomes") or [])
                  and "MARIA SILVA" not in (joao8[0].get("nomes") or [])))

    # ---------- 6c) PONTO 2.3: realocação de mensagens sem identidade no corte ----------
    # Fotos sem texto do cliente B entram no bloco do A ("foto não decide nada"),
    # aí o cabeçalho do B dispara a barreira. Antes as fotos ficavam presas no kit
    # do A; agora elas são realocadas para `pendentes` e seguem com o B.
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "", files=[2, 3]),
        A("Ana", "2026-09-20T10:00:15+00:00", "", files=[4]),
        A("Ana", "2026-09-20T10:00:20+00:00", "= JOAO\n= CPF 513.350.652-02", files=[5]),
        A("Ana", "2026-09-20T10:00:30+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")),
                key=lambda x: x["inicio"])
    maria4 = [l for l in ls if l.get("cpf") == "16520717924"]
    joao4 = [l for l in ls if l.get("cpf") == "51335065202"]
    casos.append(("2.3 fotos SEM identidade antes do cabeçalho do 2º são DELE "
                  "(realocadas; o kit do 1º não as contém)",
                  bool(maria4) and [str(f) for f in maria4[0]["file_ids"]] == ["1"]
                  and bool(joao4)
                  and sorted(str(f) for f in joao4[0]["file_ids"]) == ["2", "3", "4", "5"]))
    # as realocadas ficam marcadas; o cabeçalho do 2º NÃO é marcado
    realoc_joao = [m for m in (joao4[0].get("mensagens") or []) if m.get("_realocada")]
    realoc_maria = [m for m in (maria4[0].get("mensagens") or []) if m.get("_realocada")]
    casos.append(("2.3 mensagens realocadas ficam marcadas com _realocada "
                  "(só as SEM identidade; o cabeçalho do 2º fica limpo)",
                  len(realoc_joao) == 2 and not realoc_maria
                  and all(not m.get("_realocada")
                          for m in (joao4[0].get("mensagens") or [])
                          if m.get("cpfs") or m.get("e_identificador"))))
    # kit do 1º que perdeu mensagens vai para REVISÃO com o motivo ANOTADO
    casos.append(("2.3 kit do 1º que perdeu mensagens vai para REVISÃO "
                  "(dúvida: fotos soltas coladas no corte)",
                  bool(maria4) and not maria4[0]["pronto"]
                  and maria4[0]["requer_revisao"]
                  and any("realocad" in str(m).lower()
                          for m in maria4[0]["motivos_revisao"])))

    # 2.3 GUARDA: foto IDENTIFICADA do A (com CPF) colada no corte NÃO sai.
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "= CPF 165.207.179-24", files=[7]),
        A("Ana", "2026-09-20T10:00:20+00:00", "= JOAO\n= CPF 513.350.652-02", files=[9]),
        A("Ana", "2026-09-20T10:00:30+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00")),
                key=lambda x: x["inicio"])
    maria4b = [l for l in ls if l.get("cpf") == "16520717924"]
    casos.append(("2.3 GUARDA: foto COM CPF do A colada no corte NÃO sai "
                  "(nunca roubar material identificado)",
                  bool(maria4b)
                  and sorted(str(f) for f in maria4b[0]["file_ids"]) == ["1", "7"]
                  and not any(m.get("_realocada")
                              for m in (maria4b[0].get("mensagens") or []))))

    # 2.3 GUARDA: salto de ~30 min entre a foto e o cabeçalho PARA a varredura
    # (folga é 10 min, não os 30 min da janela).
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "", files=[6]),
        A("Ana", "2026-09-20T10:40:00+00:00", "= JOAO\n= CPF 513.350.652-02", files=[9]),
        A("Ana", "2026-09-20T10:50:00+00:00", "-"),
    ]
    ls = sorted(agrupar(msgs, cfg,
                        agora=_dt.datetime.fromisoformat("2026-09-20T11:00:00+00:00")),
                key=lambda x: x["inicio"])
    maria4c = [l for l in ls if l.get("cpf") == "16520717924"]
    casos.append(("2.3 GUARDA: salto > realoc_folga_min (10 min) PARA a varredura "
                  "(foto fica no kit do A)",
                  bool(maria4c)
                  and sorted(str(f) for f in maria4c[0]["file_ids"]) == ["1", "6"]
                  and not any(m.get("_realocada")
                              for m in (maria4c[0].get("mensagens") or []))))

    # ---------- 6b) CONFLITO segue como REDE FINAL (quando não dá para separar:
    # ------------- os 2 CPFs vêm na MESMA mensagem) ----------
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "= MARIA\n= CPF 165.207.179-24\n= JOAO\n= CPF 513.350.652-02", files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    casos.append(("2 CPFs na MESMA mensagem => status CONFLITO e NÃO pronto (revisão)",
                  len(ls) == 1 and ls[0]["status"] == "CONFLITO"
                  and ls[0]["identificacao"] == "CONFLITO"
                  and not ls[0]["pronto"] and ls[0]["requer_revisao"]))

    # ---------- 7) CPF dentro de mensagem normal continua sendo extraído ----------
    A.n = 0
    m = A("Ana", "2026-09-20T10:00:00+00:00", "= CPF 044.194.998-34\n= AUX MATERNIDADE")
    casos.append(("CPF numa mensagem normal é extraído", m["cpfs"] == ["04419499834"] and not m["sinal"]))

    # ---------- 8) PONTO 2.5: PASTA FINALIZADA não apaga o nome da mesma msg ----------
    A.n = 0
    m = A("Ana", "2026-09-20T10:00:00+00:00",
          "= MARIA\n= CPF 165.207.179-24\nPASTA FINALIZADA")
    casos.append(("2.5 nome e CPF sobrevivem a PASTA FINALIZADA na mesma mensagem",
                  m["nomes"] == ["MARIA"] and m["cpfs"] == ["16520717924"]
                  and m["e_fim"] and not m["sinal"]))
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "= MARIA\n= CPF 165.207.179-24\nPASTA FINALIZADA", files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    casos.append(("2.5 lote com PASTA FINALIZADA mantém nome da cliente e marcador_fim",
                  len(ls) == 1 and ls[0].get("nome") == "MARIA"
                  and ls[0].get("cpf") == "16520717924"
                  and ls[0].get("marcador_fim")))
    casos.append(("2.5 PASTA FINALIZADA sozinha não vira nome",
                  extrair_nomes("= PASTA FINALIZADA") == []))
    casos.append(("2.5 candidato inline que contém o marcador é descartado",
                  extrair_nomes("= PASTA FINALIZADA MARIA") == []))

    # ---------- 9) PONTO 2.4: pareamento consciente MENOR×MÃE ----------
    # A regra de pé NÃO se reabre: quando os dois nomes aparecem, o nome da
    # MENOR vence. E o nome da menor NUNCA fica pareado com o CPF da mãe — era
    # o produto cartesiano de `_acumular`, e o nome certo nascia no CPF errado.
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "= MENOR MARIA SILVA\n= CPF MÃE DA MENOR: 900.849.399-31", files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    l = ls[0] if len(ls) == 1 else {}
    par = l.get("nomes_por_cpf") or {}
    casos.append(("2.4a) '= MENOR MARIA SILVA' + '= CPF MÃE DA MENOR': nome da menor, "
                  "cpf_responsavel preenchido, SEM CONFLITO e sem cruzar nomes_por_cpf",
                  len(ls) == 1 and l.get("nome") == "MARIA SILVA"
                  and l.get("cpf_responsavel") == "90084939931"
                  and l.get("cpf") == "90084939931"
                  and l.get("identificacao") == "OK"
                  and l.get("identificacao") != "CONFLITO"
                  and all("MARIA SILVA" not in v for v in par.values())))

    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "= MENOR MARIA SILVA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:10+00:00", "= CPF MÃE DA MENOR: 900.849.399-31"),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    l = ls[0] if len(ls) == 1 else {}
    casos.append(("2.4b) menor (CPF próprio) + mãe (CPF dela) em msgs separadas: "
                  "lote ÚNICO, cpf do menor, mãe em cpf_responsavel, SEM CONFLITO",
                  len(ls) == 1 and l.get("cpf") == "16520717924"
                  and l.get("cpf_responsavel") == "90084939931"
                  and l.get("nome") == "MARIA SILVA"
                  and l.get("identificacao") == "OK"
                  and l.get("status") != "CONFLITO"))

    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "= MENOR MARIA SILVA\n= CPF 165.207.179-24\n= CPF MÃE DA MENOR: 900.849.399-31",
          files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    l = ls[0] if len(ls) == 1 else {}
    par = l.get("nomes_por_cpf") or {}
    # NÃO confundir com o caso "2 CPFs na MESMA mensagem => CONFLITO" de cima:
    # aquele usa dois CPFs SEM rótulo de família e continua CONFLITO.
    # Aqui a menor TEM CPF próprio, então nomes_por_cpf pode (e deve) pareá-la
    # com o CPF DELA — o que não pode é ela nascer sob o CPF da mãe.
    casos.append(("2.4b2) menor+mãe com CPFs distintos na MESMA mensagem: sem CONFLITO, "
                  "cpf do menor em lote['cpf'] e o nome dela só sob o CPF dela",
                  len(ls) == 1 and l.get("cpf") == "16520717924"
                  and l.get("cpf_responsavel") == "90084939931"
                  and l.get("identificacao") != "CONFLITO"
                  and l.get("status") != "CONFLITO"
                  and "MARIA SILVA" in (par.get("16520717924") or [])
                  and "MARIA SILVA" not in (par.get("90084939931") or [])))

    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "NOME DA MAE: PATRICIA TESTE\n= MENOR MARIA SILVA\n= CPF 165.207.179-24",
          files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    l = ls[0] if len(ls) == 1 else {}
    casos.append(("2.4c) 'NOME DA MAE: PATRICIA TESTE' nunca vira lote['nome'] — a MENOR vence",
                  len(ls) == 1 and l.get("nome") == "MARIA SILVA"
                  and l.get("nome") != "PATRICIA TESTE"
                  and "PATRICIA TESTE" not in (l.get("nomes") or [])))

    # variantes unitárias: a mãe sai de msg["nomes"] e fica só em nomes_papeis
    A.n = 0
    m = A("Ana", "2026-09-20T10:00:00+00:00",
          "MÃE RESPONSAVEL: PATRICIA\n= MENOR MARIA SILVA")
    casos.append(("2.4d) 'MÃE RESPONSAVEL: PATRICIA' fica só em nomes_papeis; "
                  "msg['nomes'] tem só a MENOR",
                  m["nomes"] == ["MARIA SILVA"]
                  and (m.get("nomes_papeis") or {}).get("PATRICIA") == "responsavel"
                  and (m.get("nomes_papeis") or {}).get("MARIA SILVA") == "cliente"
                  and (m.get("nomes_rotulos") or {}).get("MARIA SILVA") == "MENOR"))

    A.n = 0
    m = A("Ana", "2026-09-20T10:00:00+00:00", "= CPF MÃE DA MENOR: 900.849.399-31")
    casos.append(("2.4e) o CPF da mãe ganha papel 'responsavel'; extrair_cpfs "
                  "continua devolvendo só o número (contrato público)",
                  m["cpfs"] == ["90084939931"]
                  and (m.get("cpfs_papeis") or {}).get("90084939931") == "responsavel"))

    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00",
          "MÃE RESPONSAVEL: PATRICIA\n= CPF MÃE DA MENOR: 900.849.399-31", files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    l = ls[0] if len(ls) == 1 else {}
    casos.append(("2.4f) kit SÓ da mãe: o nome dela nunca vira lote['nome']; "
                  "o CPF vai para cpf_responsavel (fallback em lote['cpf'])",
                  len(ls) == 1 and not l.get("nome")
                  and l.get("cpf_responsavel") == "90084939931"
                  and l.get("cpf") == "90084939931"
                  and l.get("status") != "CONFLITO"))

    # ---------- cen8) nome de 1 palavra / RUÍDO (cenário 8, achado [20]) ----------
    # CONTRATO CONGELADO (não decidir política aqui): 1 palavra aceita pela
    # triagem Python NÃO vira identificação falsa de CLIENTE. Se o nome ficar
    # NÃO-IDENTIFICADO, o lote vai para REVISÃO com o CPF como âncora — NUNCA
    # para dead-letter só por isso. O DEFAULT de `nome.exigir_completo` está
    # congelado abaixo como True (modo seguro (b), o atual da config); a
    # política (aceitar 1 palavra nas DUAS pontas × exigir 2 e mandar para
    # revisão) é decisão do DONO e não muda sem ele.
    from nome_regras import validar_nome, _exigir_completo_cfg
    casos.append(("cen8) DEFAULT exigir_completo=True congelado "
                  "(modo seguro (b): 1 palavra → NÃO-IDENTIFICADO → revisão)",
                  _exigir_completo_cfg() is True))
    casos.append(("cen8) ruído/1 palavra: 'SOLTEIRA' não vira nome nem como sinal; "
                  "'VIDABBZAO' é sinal (1 palavra) mas NÃO vira nome de cliente",
                  extrair_nomes("= SOLTEIRA") == []
                  and extrair_nomes("= VIDABBZAO") == ["VIDABBZAO"]
                  and not validar_nome("VIDABBZAO") and not validar_nome("SOLTEIRA")))
    A.n = 0
    msgs = [
        A("Ana", "2026-09-20T10:00:00+00:00", "-"),
        A("Ana", "2026-09-20T10:00:05+00:00", "= MARIA\n= CPF 165.207.179-24", files=[1]),
        A("Ana", "2026-09-20T10:00:20+00:00", "-"),
    ]
    ls = agrupar(msgs, cfg, agora=_dt.datetime.fromisoformat("2026-09-20T10:01:00+00:00"))
    l8 = ls[0] if len(ls) == 1 else {}
    casos.append(("cen8) 1 palavra é SINAL na triagem (= MARIA) mas NÃO vira nome "
                  "de cliente (nada de identificação falsa)",
                  len(ls) == 1 and l8.get("nome") == "MARIA"
                  and not validar_nome(l8.get("nome") or "")))
    try:
        from lote_para_job import montar_job
        sim8 = montar_job(l8)
        cli8 = sim8["job"]["cliente"]
        rev8 = sim8["job"].get("revisao_nome") or {}
        casos.append(("cen8) NÃO-IDENTIFICADO vai para REVISÃO com o CPF como âncora "
                      "(revisao_nome.cpf); job segue 'pendente' — NUNCA dead-letter só por isso",
                      cli8.get("nome") == "" and cli8.get("nome_nao_identificado") is True
                      and rev8.get("motivo") == "nome_nao_identificado"
                      and rev8.get("cpf") == cli8.get("cpf") and bool(cli8.get("cpf"))
                      and sim8["job"].get("status") == "pendente"))
    except Exception as exc8:  # noqa: BLE001 — vira FALHA visível, nunca silenciosa
        casos.append((f"cen8) NÃO-IDENTIFICADO vai para REVISÃO com o CPF como âncora "
                      f"(erro ao montar o job: {type(exc8).__name__}: {exc8})", False))

    print("=" * 78)
    print("TESTE DO SINAL DE TRAÇO + TOGGLE POR AUTOR")
    print("=" * 78)
    falhas = 0
    for nome, ok in casos:
        falhas += 0 if ok else 1
        print(f"[{'OK ' if ok else 'FALHA'}] {nome}")
    print("-" * 78)
    print(f"casos={len(casos)} falhas={falhas}")
    return 1 if falhas else 0


# --------------------------------------------------------------------------
# Prova da FONTE NOVA (dados reais, sem baixar nada)
# --------------------------------------------------------------------------
def testar_fonte(dialog: str | None = None) -> int:
    """Lê o grupo pela API do VibeCode e prova, com os dados reais de hoje:

      1. quantas mensagens existem hoje (o webhook NÃO as via);
      2. quais mensagens de TRAÇO existem hoje e como o toggle abre/fecha;
      3. que as REAÇÕES foram ignoradas (não viraram lote, não extraíram CPF).
    """
    chave = ler_chave_bot()
    conf = carregar_config()
    bot_cfg = ler_bot_config()
    dialog = dialog or conf.get("dialog_id") or bot_cfg.get("canal") or DIALOG_ID_PADRAO
    print("=" * 78)
    print("FONTE NOVA (API do VibeCode) vs FONTE ANTIGA (webhook) — DADOS REAIS")
    print(f"base={VIBECODE_BASE}  grupo={dialog}  chave={ARQ_CHAVE} (não exibida)")
    print("=" * 78)

    mensagens = buscar_mensagens_api(chave, dialog, max_paginas=MSG_PAGINAS_HISTORICO, cfg=conf)
    total = len(mensagens)
    reacoes = [m for m in mensagens if m.get("reacao")]
    limpas = [m for m in mensagens if not m.get("reacao")]
    hj = _dt.datetime.now().date()
    hoje = [m for m in limpas if dia_local(m.get("ts")) == hj]
    hoje_todas = [m for m in mensagens if dia_local(m.get("ts")) == hj]

    print(f"\n[1] MENSAGENS\n    lidas pela fonte nova : {total}")
    print(f"    de hoje ({hj})       : {len(hoje_todas)} no total"
          f"  ({len(hoje)} sem contar reações)")
    por_dia: dict = {}
    for m in limpas:
        d = dia_local(m.get("ts"))
        por_dia[str(d)] = por_dia.get(str(d), 0) + 1
    print("    por dia (fuso local):", ", ".join(f"{k}={v}" for k, v in sorted(por_dia.items())))
    for m in hoje_todas:
        print(f"      {m['id']}  {m['ts'].astimezone().strftime('%H:%M:%S')}  "
              f"{(m['autor'] or '?')[:34]:34}  {(m['texto_bruto'] or '')[:60]!r}")

    print(f"\n[2] REAÇÕES (devem ser IGNORADAS — não viram lote, não extraem CPF/NOME)\n"
          f"    reações no histórico: {len(reacoes)}"
          f" | de hoje: {sum(1 for m in reacoes if dia_local(m.get('ts')) == hj)}")
    for m in reacoes:
        if dia_local(m.get("ts")) == hj:
            print(f"      {m['id']}  {m['ts'].astimezone().strftime('%H:%M:%S')}  "
                  f"reacao={m['reacao']}  ruido={m['ruido']}  sinal={m['sinal']}  "
                  f"cpfs={m['cpfs']}  nomes={m['nomes']}  texto={m['texto_bruto'][:48]!r}")

    print("\n[3] TRAÇOS DE HOJE + TRAÇO")
    modot = modo_traco(conf)
    try:
        vig_min = int(conf.get("traco_vigencia_min") or 0)
    except (TypeError, ValueError):
        vig_min = 0
    print(f"    modo_traco = {modot}: {MODOS_TRACO.get(modot)}   "
          f"vigência do bloco = {vig_min} min")
    tracos = [m for m in hoje if m.get("sinal")]
    print(f"    mensagens de traço hoje: {len(tracos)}")
    # última mensagem (que pode entrar num lote, ou seja, sem ruído) antes de cada
    # traço ≈ fim do bloco no momento do sinal
    flt = [m for m in limpas if not _eh_ruido(m)]
    anterior = {cur["id"]: prev for prev, cur in zip(flt, flt[1:])}
    aberto: dict = {}
    for m in tracos:
        ca = f"{m['autor_id']}|{normalizar_nome(m['autor'])}"
        k = ca if modot == "autor" else "GRUPO"
        if k in aberto:
            lote, dono = aberto[k]
            ult = (anterior.get(m["id"]) or {}).get("ts")
            vencido = bool(vig_min and ult and m["ts"]
                           and (m["ts"] - ult) > _dt.timedelta(minutes=vig_min))
            if modot == "grupo_autor" and dono != ca and not vencido:
                acao = "IGNORADO (bloco de " + lote["autor"] + ")"
            elif modot == "grupo_autor" and dono != ca and vencido:
                acao = "ASSUME (bloco de " + lote["autor"] + " venceu)"
                aberto[k] = (m, ca)
            else:
                acao = "FECHA"
                aberto.pop(k)
        else:
            acao = "ABRE"
            aberto[k] = (m, ca)
        print(f"      {m['ts'].astimezone().strftime('%H:%M:%S')}  msg={m['id']}  "
              f"autor={m['autor'][:30]:30} (authorId={m['autor_id']})  {acao:38}  "
              f"texto={m['texto_bruto'][:60]!r}")
    print(f"    blocos que ficaram ABERTOS no fim (sem o traço de fechar): "
          f"{[m[0]['autor'] for m in aberto.values()]}")

    # o agrupador real sobre os MESMOS dados (prova que traço vira lote e reação não)
    orfaos: list[dict] = []
    reac: list[dict] = []
    lotes = agrupar(limpas, conf, orfaos_out=orfaos, reacoes_out=reac)
    print(f"\n[4] AGRUPADOR REAL sobre essas {len(limpas)} mensagens")
    print(f"    lotes formados: {len(lotes)}   reações repassadas ao agrupador: {len(reac)}"
          f"   (== 0 pois foram filtradas antes)")
    for l in sorted([x for x in lotes if x["modo"] == "marcadores"],
                    key=lambda x: x["inicio"] or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)):
        print(f"      {l['lote_id']}  status={l['status']:19} ident={l['identificacao']:11} "
              f"arqs={l['qtd_arquivos']:2}  pronto={l['pronto']}"
              f"{' (por: ' + str(l['pronto_por']) + ')' if l.get('pronto_por') else ''}  "
              f"autor={l['autores']}  cpf={cpf_formatado(l['cpf']) if l['cpf'] else '-'}")
        if l.get("sinal_abertura"):
            print(f"          ABRE  msg={l['sinal_abertura']['message_id']} "
                  f"ts={l['sinal_abertura']['ts']}")
        if l.get("sinal_fechamento"):
            print(f"          FECHA msg={l['sinal_fechamento']['message_id']} "
                  f"ts={l['sinal_fechamento']['ts']}")
    # nenhuma reação pode estar dentro de um lote
    ids_reacao = {m["id"] for m in reacoes}
    dentro = [m["id"] for l in lotes for m in l["mensagens"] if m["id"] in ids_reacao]
    print(f"    reações dentro de algum lote: {len(dentro)} {dentro}")

    print(f"\n[4b] LOTES QUE TOCAM O DIA DE HOJE ({hj})")
    do_dia = [l for l in lotes
              if any(dia_local(m.get("ts")) == hj for m in l["mensagens"])]
    print(f"    lotes do dia: {len(do_dia)}")
    for l in sorted(do_dia, key=lambda x: x["inicio"] or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)):
        print(f"      {l['lote_id']}  modo={l['modo']:10} status={l['status']:19} "
              f"ident={l['identificacao']:11} arqs={l['qtd_arquivos']:2} pronto={l['pronto']}"
              f"{' (por: ' + str(l['pronto_por']) + ')' if l.get('pronto_por') else ''}  "
              f"autor={l['autores']}  cpf={cpf_formatado(l['cpf']) if l['cpf'] else '-'}")
        print(f"          janela {l['inicio'].astimezone().strftime('%d/%m %H:%M:%S')} -> "
              f"{l['fim'].astimezone().strftime('%d/%m %H:%M:%S') if l['fim'] else '-'}"
              f"   nome={l['nome']!r}   fechado_por={l['fechado_por']}")

    # prova do webhook (fonte antiga), se ainda estiver descobrível
    print("\n[5] FONTE ANTIGA (webhook) para comparar")
    try:
        w = descobrir_webhook()
        antigas = buscar_mensagens(w, dialog, cfg=conf)
        ult = max((m["ts"] for m in antigas if m.get("ts")), default=None)
        print(f"    webhook: {len(antigas)} mensagens; a MAIS NOVA é de "
              f"{ult.astimezone().isoformat() if ult else '?'}")
        print(f"    webhook: mensagens de hoje ({hj}): "
              f"{sum(1 for m in antigas if dia_local(m.get('ts')) == hj)}"
              f"   <-- por isso a fonte mudou")
    except Exception as e:                          # noqa: BLE001
        print(f"    webhook não pôde ser consultado ({e})")
    print()
    return 0



# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

# ── Alerta de falha no STDOUT (contrato do Hermes) ─────────────────────────
# O agendador do Hermes transforma o stdout em mensagem no Telegram; stderr NÃO
# vira mensagem. A falha de ciclo vira UMA linha compacta em stdout, com dedupe
# por mensagem — o mesmo problema não repete antes de N horas (alerta que vira
# spam é quase tão ruim quanto o silêncio atual). Caminho feliz: print nenhum.
ARQ_ALERTA = os.path.join(BASE_DIR, "alerta-falha-estado.json")
ALERTA_DEDUPE_HORAS = 6


def _alerta_ler() -> dict:
    """Lê o estado de dedupe dos alertas. Nunca levanta — sem estado, alerta de novo."""
    try:
        with open(ARQ_ALERTA, "r", encoding="utf-8") as fh:
            est = json.load(fh)
        return est if isinstance(est, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _alerta_gravar(est: dict) -> None:
    """Grava o estado de dedupe (tmp + replace, padrão do projeto). Nunca levanta.

    Se o I/O falhar, o problema vai para stderr (nunca stdout) — o alerta segue
    impresso assim mesmo; dedupe impossível é preferível ao silêncio.
    """
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        tmp = ARQ_ALERTA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(est, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, ARQ_ALERTA)
    except OSError as e:
        print(f"[aviso] não pude gravar o estado de alerta em {ARQ_ALERTA}: {e}",
              file=sys.stderr)


def _alertar_linha(msg: str) -> None:
    """Imprime UMA linha de alerta no stdout, com dedupe de N horas por mensagem."""
    chave = hashlib.sha1(msg.encode("utf-8")).hexdigest()[:16]
    est = _alerta_ler()
    agora = _dt.datetime.now(_dt.timezone.utc)
    visto = est.get(chave)
    if visto:
        try:
            quando = _dt.datetime.fromisoformat(str(visto))
            if quando.tzinfo is None:
                quando = quando.replace(tzinfo=_dt.timezone.utc)
            if (agora - quando).total_seconds() < ALERTA_DEDUPE_HORAS * 3600:
                return                    # mesma mensagem já saiu nesta janela
        except (TypeError, ValueError):
            pass                          # timestamp ilegível: deixa alertar de novo
    est[chave] = agora.isoformat(timespec="seconds")
    _alerta_gravar(est)
    print(msg)


def alertar_falha(chat: str, erro) -> None:
    """Converte a exceção de ciclo em UMA linha compacta no stdout (Hermes → Telegram)."""
    _alertar_linha(f"⚠️ Triagem: ciclo falhou em {chat}: {seguranca.mascarar(str(erro))[:160]}")


def montar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fase 1 — triagem de documentos por grupo do Bitrix24")
    p.add_argument("--once", action="store_true", help="roda UM ciclo e sai (baixa arquivos)")
    p.add_argument("--loop", type=int, metavar="SEG", help="roda continuamente a cada SEG segundos")
    p.add_argument("--dry-run", action="store_true", help="não baixa nem grava estado; só mostra o plano")
    p.add_argument("--janela-min", type=int, default=None,
                   help="janela do FALLBACK por autor (min); default: config.json")
    p.add_argument("--dialog-id", default=None, help="ID do grupo (default: config.json / bot.json)")
    p.add_argument("--desde-msg-id", type=int, default=None, help="ignora mensagens com id <= N")
    p.add_argument("--reler-janela", action="store_true",
                   help="F1-01: ignora o cursor do chat neste ciclo e relê a janela antiga "
                        "(recuperação manual após LACUNA; lotes fechados não são recriados)")
    p.add_argument("--paginas", type=int, default=None,
                   help=f"páginas de 50 mensagens lidas da API por ciclo (default: config/config "
                        f"{MSG_PAGINAS_HISTORICO})")
    p.add_argument("--fonte-webhook", action="store_true",
                   help="DIAGNÓSTICO: lê pela fonte ANTIGA (webhook im.dialog.messages.get), "
                        "que NÃO vê as mensagens novas")
    p.add_argument("--force", action="store_true", help="rebaixa/reprocessa mesmo já existente")
    p.add_argument("--testar-cpf", action="store_true", help="roda os testes do extrator de CPF e sai")
    p.add_argument("--testar-sinais", action="store_true",
                   help="roda os testes do agrupador por sinais INÍCIO/FIM e sai")
    p.add_argument("--testar-fonte", action="store_true",
                   help="lê o grupo pela API do VibeCode e prova (com dados reais) o que "
                        "a fonte antiga não via; não baixa nada")
    p.add_argument("--debug", action="store_true", help="log em nível DEBUG")
    return p


def trava_de_ciclo(timeout: float = 300.0):
    """Trava entre CICLOS (periódico x ciclo disparado na hora pelo bot).

    O serviço roda a cada 120s e o bot pode disparar um ciclo no mesmo instante em
    que um traço chega. Sem esta trava os dois leriam o grupo e baixariam os MESMOS
    arquivos em paralelo (desperdício e corrida no estado). Se a trava não sair no
    tempo, seguimos assim mesmo — a triagem nunca pode travar por causa dela.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        fd = None
        try:
            fd = os.open(os.path.join(BASE_DIR, "ciclo.lock"), os.O_CREAT | os.O_RDWR, 0o644)
            fim = time.time() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= fim:
                        log.warning("outro ciclo está rodando há mais de %ss — seguindo sem a trava",
                                    int(timeout))
                        break
                    time.sleep(0.2)
        except OSError as e:                      # sem trava, mas roda
            log.warning("não consegui abrir a trava de ciclo (%s) — seguindo", e)
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    return _ctx()


def main(argv=None) -> int:
    args = montar_parser().parse_args(argv)
    configurar_log(args.debug)

    if args.testar_cpf:
        return testar_cpf()
    if args.testar_sinais:
        return testar_sinais()
    if args.testar_fonte:
        return testar_fonte(args.dialog_id)

    try:
        conf = carregar_config()
    except ConfigInvalida as e:
        # F1-06/F1-07: visível (stdout = alerta do Hermes) e código != 0
        print(f"🚨 triagem NÃO rodou: {e}")
        return 3
    chats = dialogos(conf, ler_bot_config(), args.dialog_id)
    log.info("grupos monitorados: %s", ", ".join(chats))

    if args.loop:
        log.info("loop iniciado: intervalo=%ss dry_run=%s chats=%s",
                 args.loop, args.dry_run, chats)
        while True:
            t0 = time.time()
            for chat in chats:                    # um ciclo COMPLETO por grupo
                args.dialog_id = chat
                try:
                    with trava_de_ciclo():
                        imprimir_resumo(ciclo(args))
                except Exception as e:            # noqa: BLE001 - o loop nunca morre
                    log.exception("ciclo falhou em %s (seguindo): %s", chat, e)
                    alertar_falha(chat, e)        # avisa o Hermes (com dedupe) e segue
            time.sleep(max(1.0, args.loop - (time.time() - t0)))

    for chat in chats:
        args.dialog_id = chat
        try:
            with trava_de_ciclo():
                imprimir_resumo(ciclo(args))
        except Exception as e:                    # noqa: BLE001
            log.exception("ciclo falhou em %s: %s", chat, e)
            alertar_falha(chat, e)                # avisa o Hermes ANTES de sair
            return 2
    return 0


def _main_com_guarda() -> int:
    """Guarda final: exceção não tratada também vira linha no stdout (e exit 3)."""
    try:
        return main()
    except Exception as e:                        # noqa: BLE001
        log.exception("exceção não tratada fora do ciclo: %s", e)
        _alertar_linha(f"⚠️ Triagem: exceção não tratada: {seguranca.mascarar(str(e))[:160]}")
        return 3


if __name__ == "__main__":
    sys.exit(_main_com_guarda())
