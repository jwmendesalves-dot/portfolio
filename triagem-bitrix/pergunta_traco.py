#!/usr/bin/env python3
"""
Pergunta no traço (TESTE) — quando alguém manda um traço (—) no grupo, o BOT
pergunta se a pessoa quer abrir um lote, e responde conforme a escolha.

Módulo SEPARADO de propósito: não altera triagem_grupo.py (fase 1) nem
lote_para_fila.py (fase 2). Se der problema, é só parar o timer.

Como o bot fala (provado na API do VibeCode):
    POST /v1/bots/{botId}/messages   {dialogId, fields:{message, keyboard}}
    keyboard aceita botões com ACTION=SEND + ACTION_VALUE (a resposta é enviada
    como mensagem da própria pessoa — não precisa tratar callback).

Fluxo:
    1) lê as últimas mensagens do grupo (reaproveita o leitor da fase 1);
    2) traço de PESSOA (não de bot/conector, não reação, não mensagem de sistema)
       que ainda não foi perguntado -> manda a pergunta com dois botões;
    3) quando a MESMA pessoa responde (sim/abrir ou não), o bot confirma;
    4) tudo fica em pergunta-traco-estado.json.

Uso:
    --simular            mostra o que faria, sem enviar nada (padrão)
    --ativo              envia de verdade
    --chat chat100001    grupo alvo (padrão: config.pergunta_traco.chats)
    --bot 100003         id do bot que fala
    --testar --para 1    manda a pergunta para o privado do usuário 1 (prova real)
    --limpar             zera o estado

Códigos de saída (o systemd vê o código real):
    0  ok
    1  falha (rodada com erro, gravação do estado falhou, envio de teste falhou)
    3  config.json inválido (ConfigInvalida) ou modo_traco diferente do que o bot
       implementa ('delimitador') — nada foi lido/enviado. No `--loop` o processo
       NÃO sai: avisa e tenta reler o config com pausa crescente (60 s .. 15 min).
    75 outra instância já está rodando (trava `pergunta-traco.lock`) — nada foi lido/enviado

Envio sem reenvio cego (PT-01, auditoria 05): cada aviso de traço vira um
registro em `envios` no estado, com a máquina
    PENDENTE -> (CONFIRMADO | ENVIO_INCERTO | ADIADO | FALHA)
e o PENDENTE é GRAVADO ANTES do POST. Timeout/erro de rede depois do POST =
ENVIO_INCERTO (pode ter saído): só é reenviado depois de a reconciliação não
achar a mensagem do bot no grupo E passar JANELA_REENVIO_INCERTO_S, no máximo
MAX_REENVIOS_INCERTO vez. Sem conseguir gravar o estado, nada é enviado e as
rodadas seguintes ficam em pausa crescente (sem loop de 8 s).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import errno
import fcntl
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

_AQUI = os.path.dirname(os.path.abspath(__file__))
if _AQUI not in sys.path:
    sys.path.insert(0, _AQUI)
import caminhos                                                # noqa: E402

BASE = caminhos.BASE
if BASE not in sys.path:
    sys.path.insert(1, BASE)
import triagem_grupo as tg                                     # noqa: E402
import relatorio as RL                                         # noqa: E402
import rede_bitrix                                             # noqa: E402
import seguranca                                               # noqa: E402

ARQ_ESTADO = caminhos.em_base("pergunta-traco-estado.json")
# (PT-02) trava de instância única — vale para TODOS os modos que leem o grupo,
# enviam ou mexem no estado (rodada única, --loop, --testar/--para, --limpar).
ARQ_LOCK = caminhos.em_base("pergunta-traco.lock")
# (PT-04) saída dos ciclos da esteira disparados a cada traço (antes: DEVNULL).
ARQ_DISPARO_LOG = caminhos.em_base("pergunta-disparo.log")
# (fase 5, mudanca[6]b) fila dos DESFECHOS dos lotes até o resumo diário.
# Arquivo NOVO (só esta rotina mexe) — nada de tocar em estado.json/fila-*.
ARQ_DESFECHOS = caminhos.em_base("desfechos-pendentes.json")
# (incidente 20/10/2026) estado do bloqueio do Bitrix no envio como bot e o
# arquivo de saúde que o painel de status lê. O bloqueio é da API KEY inteira
# (ERROR_LOOP_DETECTED em imbot.v2.Chat.Message.send): enquanto ele vale, NENHUM
# chat recebe mensagem — nem os grupos em que o bot é membro.
ARQ_BLOQUEIO = caminhos.em_base("bot-bloqueio.json")
ARQ_SAUDE = caminhos.em_base("saude-bot.json")
SAIDA_OCUPADO = 75          # EX_TEMPFAIL: outra instância segura a trava
SAIDA_CONFIG = 3            # config.json inválido — mesmo código do triagem_grupo.py
# (C3) --loop com config inválido: pausa crescente antes de reler (sem laço rápido).
PAUSA_CONFIG_INICIAL_S = 60
PAUSA_CONFIG_MAX_S = 900
# (C3/F1-07) o bot só implementa o modo DELIMITADOR (relatorio.decidir_resposta:
# todo traço fecha o bloco de cima e abre o de baixo). Se a esteira estiver em
# outro modo, o aviso do bot no grupo contradiz o agrupamento real.
MODOS_DO_BOT = ("delimitador",)

# Estados do envio de um aviso (PT-01). Gravados em est["envios"]["<chat>:<traço>"].
PENDENTE = "PENDENTE"            # gravado ANTES do POST; achado depois = talvez entregue
ENVIO_INCERTO = "ENVIO_INCERTO"  # timeout/rede/5xx depois do POST: NÃO repetir às cegas
CONFIRMADO = "CONFIRMADO"        # 2xx, ou reconciliação achou a mensagem do bot no grupo
ADIADO = "ADIADO"                # recusado SEM executar (bloqueio ativo, 429 esgotado)
FALHA = "FALHA"                  # definitivo (ACCESS_DENIED, 4xx, incerteza expirada)
# Incerto só é reenviado depois desta janela E de a reconciliação não achar a msg.
JANELA_REENVIO_INCERTO_S = 600
MAX_REENVIOS_INCERTO = 1
MAX_TENTATIVAS_ADIADO = 3
ESPACO_ADIADO_S = 60
# ACCESS_DENIED num chat: nenhum envio a esse chat nesta pausa (repetir envenena a key).
PAUSA_SEM_ACESSO_S = 3600
MANTER_ENVIOS = 200
# Falha ao gravar o estado: pausa crescente de envio (60 s .. 15 min), em memória.
_PAUSA_GRAVACAO = {"ate": 0.0, "passo": 0.0}
# ERROR_LOOP_DETECTED com bot-bloqueio.json sem gravar (disco cheio): vale em memória.
_BLOQUEIO_MEMORIA: dict = {}
# Traço mais velho que isto não vira aviso no grupo: o aviso atrasado só
# confunde (o lote já foi lido pela esteira) e retentar para sempre é o que
# alimenta o bloqueio do Bitrix.
JANELA_AVISO_MIN = 45
# O Bitrix pede pausa CRESCENTE (1, 5, 15, 60 min) depois de bloquear.
PAUSA_PADRAO_S = 900
API = "https://vibecode.bitrix24.com"
BOT_PADRAO = 100003
CHATS_PADRAO = ["chat100001"]


class ErroEnvioBloqueado(RuntimeError):
    """Bitrix bloqueou o envio como bot (ERROR_LOOP_DETECTED). NÃO retentar
    dentro da pausa: o bloqueio só cai na primeira chamada bem-sucedida."""


class ErroEnvioSemAcesso(RuntimeError):
    """Sem permissão de envio naquele chat (ACCESS_DENIED / BOT_ACCESS_DENIED).
    Repetir a mesma request nunca resolve — e é o que dispara o bloqueio."""


class ErroEnvioIncerto(RuntimeError):
    """Timeout/erro de rede/5xx DEPOIS de mandar o POST: a mensagem pode ter
    saído. Nunca repetir às cegas (vira spam e ERROR_LOOP_DETECTED)."""


class ErroEnvioAdiado(RuntimeError):
    """Recusa SEM execução (429 RATE_LIMITED esgotado): a mensagem não saiu."""


class ErroEnvioRecusado(RuntimeError):
    """Recusa definitiva (4xx que não é limite/acesso): repetir não resolve."""


class ErroGravacaoEstado(RuntimeError):
    """Não consegui gravar o estado do bot (disco cheio, permissão). Sem estado
    gravado, NADA é enviado — senão a rodada seguinte reenviaria."""


class ErroRede(RuntimeError):
    """Falha de transporte em `_req` (timeout, DNS, reset, resposta truncada)."""


class ErroHTTP(RuntimeError):
    """Resposta HTTP de erro em `_req`. Mantém o texto legado `HTTP <n> em <url>: <corpo>`."""

    def __init__(self, status: int, texto: str, corpo: str = ""):
        super().__init__(texto)
        self.status = status
        self.corpo = corpo


# ----------------------------------------------------------------------
# Integração com os módulos compartilhados (frentes R e S, onda 1).
# ----------------------------------------------------------------------
def _mascarar(texto) -> str:
    """Texto seguro para log (sem token/CPF/senha): seguranca.mascarar."""
    return seguranca.mascarar(str(texto))


def _classe_rede_bitrix(status, corpo: str, exc) -> str:
    """Nome da classe de `rede_bitrix.classificar` ("" se a classificação falhar —
    estamos no caminho de erro de um envio e não podemos levantar aqui)."""
    try:
        erro = rede_bitrix.classificar(status, corpo or "", exc)
    except Exception:                                          # noqa: BLE001
        return ""
    c = getattr(erro, "classe", "")
    return str(getattr(c, "name", c) or "").upper()


# Cache de membros por chat (5 min): os membros não mudam a cada 8s, e esta era
# uma das chamadas que batia no rate limit 429 do VibeCode (regressão do deploy
# 14/10: o cache foi removido e a madrugada de 15/10 virou 429 em sequência).
_CACHE_MEMBROS: dict = {}

TEXTO_PERGUNTA = (
    "🤖 *TESTE* — você mandou um traço (—).\n"
    "Quer **abrir um lote** com esse traço?"
)
TEXTO_SIM = "✅ *TESTE* — lote aberto. (Neste teste nada é enviado para a triagem.)"
TEXTO_NAO = "👍 *TESTE* — ok, traço ignorado, nada foi aberto."

BOTOES = [
    {"TEXT": "✅ Abrir lote", "ACTION": "SEND", "ACTION_VALUE": "abrir lote",
     "BG_COLOR_TOKEN": "primary"},
    {"TEXT": "❌ Não", "ACTION": "SEND", "ACTION_VALUE": "não abrir",
     "BG_COLOR_TOKEN": "secondary", "DISPLAY": "LINE"},
]

# No WhatsApp (chat tipo LINES) o teclado NÃO atravessa a linha: só o texto chega.
# Aí a pergunta vai sem botões e pede resposta escrita. No Zap o negrito é *um* asterisco.
TEXTO_PERGUNTA_ZAP = (
    "🤖 *TESTE* — vi que você mandou um traço (—).\n"
    "Quer *abrir um lote* com esse traço? Responda *sim* ou *não*."
)

# Modo "abre/fecha" (o que está em produção): o bot avisa na ABERTURA e no FECHAMENTO.
# Os textos vêm de `relatorio.py` (mesmos do Telegram) — pedido do dono 24/09/2026:
# *"responda um relatorio igual o do telegram"*.
# (fase 5, mudanca[6]a) o fechamento fala o REGISTRO do lote, SEM ✅ de
# processamento — o desfecho honesto sai depois (ver emitir_resumo_desfechos).
SINAL = RL.SINAL_BITRIX
TEXTO_ABRE = RL.texto_abre(SINAL)
TEXTO_FECHA = RL.texto_fecha(None, 0, SINAL)   # legado/não usado no fluxo: o traço fala texto_registro (mudanca[6]a)

RX_SIM = re.compile(r"^\s*(sim|s|abrir( lote)?|abre( lote)?|pode abrir|quero|ok(ay)?|isso|bora)\s*[.!👍✅]*\s*$", re.I)
RX_NAO = re.compile(r"^\s*(nao|não|n|ignora(r)?|deixa|nada|depois)\s*[.!👎❌]*\s*$", re.I)


def _e_resposta(txt: str, m: dict, conf: dict) -> bool:
    """Só considera RESPOSTA uma mensagem curta de sim/não — nunca uma ficha,
    cabeçalho ou mensagem com arquivo (senão a ficha com "( ) Sim (x) Não"
    passaria como 'sim' — medido no simulado)."""
    if len(txt or "") > 40:
        return False
    if m.get("file_ids") or m.get("cpfs"):
        return False
    if tg.e_abertura_ou_ficha(txt) or tg.tem_nome_cabecalho(txt):
        return False
    return True


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
def _req(url: str, chave: str, corpo: dict | None = None) -> dict:
    """GET/POST na API do VibeCode.

    Erro HTTP -> ErroHTTP (texto legado `HTTP <n> em <url>: <corpo>`).
    Falha de transporte (timeout, DNS, reset, resposta truncada) -> ErroRede:
    antes ela escapava crua e a rodada tratava como \"falhou, tento de novo\" (PT-01).
    """
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        url, data=dados, method="POST" if corpo is not None else "GET",
        headers={"X-Api-Key": chave, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            bruto = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            corpo_erro = e.read().decode("utf-8", "replace")[:400]
        except Exception:                                      # noqa: BLE001
            corpo_erro = ""
        raise ErroHTTP(e.code, f"HTTP {e.code} em {url}: {corpo_erro}", corpo_erro) from None
    except (OSError, http.client.HTTPException, ValueError) as e:
        # URLError, socket.timeout/TimeoutError, ConnectionReset, IncompleteRead…
        raise ErroRede(_mascarar(f"{type(e).__name__} em {url}: {e}")) from None
    try:
        return json.loads(bruto) if bruto.strip() else {}
    except ValueError:
        return {"_bruto": bruto[:200]}       # 2xx com corpo estranho: a chamada EXECUTOU


# ids de usuários-bot por chat (PT-03), preenchido junto com o cache de membros.
_BOT_IDS: dict = {}


def membros_do_chat(chave: str, chat: str) -> tuple[set, dict]:
    """Membros do chat: (nomes de BOT, {id: nome}) — para não perguntar para robô.

    Cache de 5 min: os membros não mudam a cada 8s, e esta era uma das chamadas
    que batia no rate limit 429 do VibeCode. Os IDs dos bots ficam em `_BOT_IDS`
    (a comparação de autor é por ID — PT-03).
    """
    agora = time.time()
    c = _CACHE_MEMBROS.get(chat)
    if c and agora - c[0] < 300:
        return c[1]
    try:
        d = _req(f"{API}/v1/chats/{chat}/users", chave)
    except Exception as e:                                     # noqa: BLE001
        print(f"  ⚠ não li os membros de {chat}: {_mascarar(e)}")
        return set(), {}
    us = d.get("data") or d.get("items") or []
    bots, nomes, ids_bot = set(), {}, set()
    for u in us:
        uid = u.get("id")
        nomes[uid] = u.get("name")
        if u.get("bot") is True:
            bots.add(str(u.get("name") or ""))
            ids_bot.add(str(uid))
    _CACHE_MEMBROS[chat] = (agora, (bots, nomes))
    _BOT_IDS[chat] = ids_bot
    return bots, nomes


def _ler_json(caminho: str, padrao=None):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return padrao


def _gravar_atomico(caminho: str, dados) -> None:
    """tmp único (pid+uuid) 0600 + fsync + os.replace. Levanta OSError/TypeError."""
    tmp = f"{caminho}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, caminho)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _gravar_json(caminho: str, dados) -> bool:
    try:
        _gravar_atomico(caminho, dados)
        return True
    except (OSError, TypeError, ValueError) as e:              # noqa: BLE001
        print(f"  ⚠ não gravei {os.path.basename(caminho)}: {_mascarar(e)}", flush=True)
        return False


def _codigo_erro(texto: str) -> str:
    """Código do erro devolvido pelo VibeCode/Bitrix dentro do corpo JSON."""
    m = re.search(r'"code"\s*:\s*"([A-Za-z_]+)"', str(texto))
    return (m.group(1) if m else "").upper()


def _retry_after_s(texto: str) -> float:
    """`retryAfter` (segundos) que o Bitrix manda no 429 — 0 se não vier."""
    m = re.search(r'"retryAfter"\s*:\s*(\d+)', str(texto))
    return float(m.group(1)) if m else 0.0


def _gravar_saude(**campos) -> None:
    """Saúde do envio como bot (lida pelo painel de status).

    Existe porque o painel mostrava `agente-separador` VERDE com o bot mudo há
    7h (systemd ativo ≠ bot falando). Caminho feliz também grava, senão o
    arquivo envelhece e o painel não sabe distinguir silêncio de morte.
    """
    s = _ler_json(ARQ_SAUDE, {}) or {}
    s.update(campos)
    s["atualizado_em"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    _gravar_json(ARQ_SAUDE, s)


def bloqueio_ativo() -> dict | None:
    """Bloqueio do Bitrix ainda valendo? (dict) — ou None.

    Lê `bot-bloqueio.json` (formato existente: {"ate","ate_iso","motivo","pedido_em"})
    e também o bloqueio em memória — que vale quando o arquivo não pôde ser gravado.
    """
    agora = time.time()
    b = _ler_json(ARQ_BLOQUEIO, {}) or {}
    try:
        ate = float(b.get("ate") or 0) if isinstance(b, dict) else 0.0
    except (TypeError, ValueError):
        ate = 0.0
    if ate > agora:
        return b
    if float(_BLOQUEIO_MEMORIA.get("ate") or 0) > agora:
        return dict(_BLOQUEIO_MEMORIA)
    return None


def _gravar_bloqueio(segundos: float, motivo: str) -> None:
    """Registra a pausa de envio (nunca menor que PAUSA_PADRAO_S). Vale em memória
    mesmo se o arquivo não gravar (disco cheio não pode virar chamada a cada 8 s)."""
    pausa = max(float(PAUSA_PADRAO_S), float(segundos or 0))
    ate = time.time() + pausa
    iso = _dt.datetime.fromtimestamp(ate, _dt.timezone.utc).isoformat(
        timespec="seconds")
    reg = {"ate": ate, "ate_iso": iso, "motivo": motivo,
           "pedido_em": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    _BLOQUEIO_MEMORIA.clear()
    _BLOQUEIO_MEMORIA.update(reg)
    _gravar_json(ARQ_BLOQUEIO, reg)
    _gravar_saude(ok=False, erro=f"BLOQUEADO: {motivo}", bloqueado_ate=iso)


def _variar(texto: str) -> str:
    """Mesmo aviso, texto DIFERENTE: o Bitrix bloqueia a repetição exata da
    mesma mensagem (foi assim que o `imbot.v2.Chat.Message.send` travou)."""
    return f"{texto}\n\n· reenvio {_dt.datetime.now().strftime('%H:%M')}"


_CODIGOS_ACESSO = ("BOT_ACCESS_DENIED", "BITRIX_ACCESS_DENIED", "ACCESS_DENIED",
                   "INSUFFICIENT_SCOPE", "WRONG_AUTH_TYPE")
_CODIGOS_LIMITE = ("RATE_LIMITED", "QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT")


def _tipo_falha(e: BaseException) -> str:
    """Classifica a falha de um ENVIO: LOOP | ACESSO | LIMITE | INCERTO | RECUSADO.

    Códigos do VibeCode primeiro (o proxy tem códigos próprios); depois
    `rede_bitrix.classificar`; por fim o status HTTP (classes sem mapa próprio,
    ex.: 400 INVALIDO / 404 NAO_ENCONTRADO -> RECUSADO).
    Regra da doc (07-BITRIX-DOCS §5): escrita com timeout/erro de rede NÃO se
    repete às cegas (pode ter executado); ACCESS_DENIED/4xx nunca se repetem.
    """
    texto = str(e)
    cod = _codigo_erro(texto)
    if cod == "ERROR_LOOP_DETECTED":
        return "LOOP"
    if cod in _CODIGOS_ACESSO:
        return "ACESSO"
    if cod in _CODIGOS_LIMITE:
        return "LIMITE"
    if isinstance(e, (ErroRede, OSError, http.client.HTTPException)):
        return "INCERTO"
    status = getattr(e, "status", None)
    if status is None:
        m = re.match(r"HTTP (\d{3})\b", texto)
        status = int(m.group(1)) if m else None
    classe = _classe_rede_bitrix(status, getattr(e, "corpo", "") or texto, None)
    if classe == "LOOP":
        return "LOOP"
    if classe == "ACESSO":
        return "ACESSO"
    if classe in ("RATE_LIMIT", "OPERATING"):
        return "LIMITE"
    if classe == "REDE":
        return "INCERTO"
    if status == 429:
        return "LIMITE"
    if status is not None and 400 <= status < 500:
        return "RECUSADO"
    return "INCERTO"                 # 5xx / sem status: pode ter executado


def bot_fala(chave: str, bot: int, dialog_id: str, texto: str,
             keyboard=None, tentativas: int = 4) -> dict:
    """Envia como bot. Distingue os casos que a API devolve:

    - ERROR_LOOP_DETECTED -> bloqueio da API KEY: grava a pausa e para (não
      adianta repetir; o bloqueio só cai na primeira chamada bem-sucedida);
    - ACCESS_DENIED/BOT_ACCESS_DENIED -> sem acesso àquele chat: para na hora
      (repetir é o que gera o bloqueio);
    - RATE_LIMITED (429) -> espera e repete, com o texto VARIADO (recusa sem
      execução: a mensagem não saiu); esgotado -> ErroEnvioAdiado;
    - timeout/erro de rede/5xx -> ErroEnvioIncerto (PODE ter saído: quem chama
      não repete às cegas — PT-01);
    - outro 4xx -> ErroEnvioRecusado (definitivo).
    """
    bloq = bloqueio_ativo()
    if bloq:
        falta = max(1, int((float(bloq["ate"]) - time.time()) / 60) + 1)
        raise ErroEnvioBloqueado(
            f"Bitrix bloqueou o envio como bot por ~{falta} min "
            f"({bloq.get('motivo') or 'ERROR_LOOP_DETECTED'})")
    atraso = 8
    for t in range(1, tentativas + 1):
        campos = {"message": texto if t == 1 else _variar(texto)}
        if keyboard:
            campos["keyboard"] = keyboard
        corpo = {"dialogId": dialog_id, "fields": campos}
        try:
            r = _req(f"{API}/v1/bots/{bot}/messages", chave, corpo)
        except Exception as e:                                 # noqa: BLE001
            tipo = _tipo_falha(e)
            texto_erro = _mascarar(e)
            if tipo == "LOOP":
                _gravar_bloqueio(_retry_after_s(str(e)),
                                 f"ERROR_LOOP_DETECTED em /bots/{bot}/messages")
                raise ErroEnvioBloqueado(texto_erro[:300]) from None
            if tipo == "ACESSO":
                cod = _codigo_erro(str(e)) or "ACCESS_DENIED"
                _gravar_saude(ok=False, erro=f"{cod} no envio em {dialog_id}")
                raise ErroEnvioSemAcesso(texto_erro[:300]) from None
            if tipo == "LIMITE":
                if t < tentativas:
                    print(f"   (429 rate limit — espero {atraso}s e repito ({t}/{tentativas}))",
                          flush=True)
                    time.sleep(atraso)
                    atraso = min(atraso * 2, 60)
                    continue
                raise ErroEnvioAdiado(texto_erro[:300]) from None
            if tipo == "RECUSADO":
                _gravar_saude(ok=False, erro=f"envio recusado em {dialog_id}: {texto_erro[:120]}")
                raise ErroEnvioRecusado(texto_erro[:300]) from None
            _gravar_saude(ok=False, erro=f"envio incerto em {dialog_id}: {texto_erro[:120]}")
            raise ErroEnvioIncerto(texto_erro[:300]) from None
        _gravar_saude(ok=True, erro=None, bloqueado_ate=None,
                      ultimo_envio_ok=_dt.datetime.now(_dt.timezone.utc).isoformat(
                          timespec="seconds"),
                      ultimo_envio_dialog=dialog_id)
        return r
    return {}


# ----------------------------------------------------------------------
# Estado
# ----------------------------------------------------------------------
def carregar() -> dict:
    if not os.path.exists(ARQ_ESTADO):
        return {"perguntas": {}, "testes": []}
    with open(ARQ_ESTADO, encoding="utf-8") as f:
        return json.load(f)


def salvar(est: dict) -> None:
    """Grava o estado do bot (atômico). Falha -> ErroGravacaoEstado + pausa de
    envio crescente: quem não consegue gravar não pode enviar (PT-01)."""
    est["atualizado_em"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        _gravar_atomico(ARQ_ESTADO, est)
    except (OSError, TypeError, ValueError) as e:
        passo = min(max(60.0, float(_PAUSA_GRAVACAO.get("passo") or 0) * 2), 900.0)
        _PAUSA_GRAVACAO["passo"] = passo
        _PAUSA_GRAVACAO["ate"] = time.time() + passo
        cod = errno.errorcode.get(getattr(e, "errno", None) or 0, type(e).__name__)
        print(f"  ⛔ não gravei o estado do bot ({cod}) — nenhum envio por {int(passo)}s",
              flush=True)
        _gravar_saude(ok=False, erro=f"estado do bot não grava ({cod})")
        raise ErroGravacaoEstado(f"estado do bot não grava ({cod})") from None
    _PAUSA_GRAVACAO["ate"] = 0.0
    _PAUSA_GRAVACAO["passo"] = 0.0


def pausa_gravacao_restante() -> float:
    """Segundos de pausa de envio que ainda faltam por falha de gravação."""
    return max(0.0, float(_PAUSA_GRAVACAO.get("ate") or 0) - time.time())


# ----------------------------------------------------------------------
# Desfechos dos lotes (fase 5, mudanca[6]b) — SLOTE para o desfecho POSTERIOR.
# Quem descobre o destino do lote (triagem → descartado/revisão; fila/worker →
# enviado/erro) chama `registrar_desfecho`. As chamadas ainda NÃO estão
# plugadas em todos os pontos da esteira — o importante é o desfecho nunca
# ficar impossível de emitir. A saída no grupo é 1 resumo DIÁRIO
# (`emitir_resumo_desfechos`), para não poluir com 1 msg por lote.
# ----------------------------------------------------------------------
def carregar_desfechos() -> dict:
    if not os.path.exists(ARQ_DESFECHOS):
        return {"pendentes": [], "resumos": []}
    with open(ARQ_DESFECHOS, encoding="utf-8") as f:
        return json.load(f)


def salvar_desfechos(est: dict) -> None:
    est["atualizado_em"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    _gravar_atomico(ARQ_DESFECHOS, est)


class _TravaDesfechos:
    """flock bloqueante (com prazo) do arquivo de desfechos: --registrar-desfecho
    (esteira) e --resumo (timer) rodam com o bot no ar e não podem perder registro."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        self.fd = os.open(ARQ_DESFECHOS + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fim = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= fim:
                    os.close(self.fd)
                    self.fd = None
                    raise TimeoutError("trava de desfechos ocupada") from None
                time.sleep(0.2)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
        return False


def registrar_desfecho(lote_id: str, status: str, nome: str | None = None,
                       n_arquivos: int = 0, detalhe: str = "",
                       chat: str | None = None) -> dict:
    """Guarda o desfecho de um lote para o resumo do dia (dedupe: cada registro
    sai do arquivo ao ser emitido — nunca repete no grupo)."""
    reg = RL.montar_desfecho(lote_id, status, nome=nome, n_arquivos=n_arquivos,
                             detalhe=detalhe)
    if chat:
        reg["chat"] = chat
    with _TravaDesfechos():
        est = carregar_desfechos()
        est.setdefault("pendentes", []).append(reg)
        salvar_desfechos(est)
    return reg


def falar_desfecho(chave: str, bot: int, chat: str, reg: dict) -> dict:
    """Slot de desfecho IMEDIATO (1 lote → 1 msg) — para o caso que não pode
    esperar o resumo do dia. O caminho de rotina é o resumo diário."""
    return bot_fala(chave, bot, chat, RL.texto_desfecho(reg))


def emitir_resumo_desfechos(chave: str, bot: int, chat: str,
                            ativo: bool = False) -> int:
    """Manda o RESUMO DIÁRIO dos desfechos pendentes (1 mensagem) e zera a fila.

    Caminho feliz é silencioso: sem pendente, não fala nada (contrato Hermes —
    nenhum print novo em caminho feliz). Só `--simular` mostra o texto.
    """
    with _TravaDesfechos():
        pend = list(carregar_desfechos().get("pendentes") or [])
    if not pend:
        return 0
    texto = RL.texto_resumo_desfechos(pend)
    if not ativo:
        print(f"  [simulado] resumo diário com {len(pend)} desfecho(s):\n{texto}")
        return len(pend)
    incerto = False
    try:
        bot_fala(chave, bot, chat, texto)
    except ErroEnvioIncerto as e:
        # (PT-01) pode ter saído: tira da fila mesmo assim (sem reenvio cego) e registra
        incerto = True
        print(f"  ❓ resumo diário com envio INCERTO (não reenvio): {_mascarar(e)[:160]}",
              flush=True)
    # registros que chegaram DURANTE o envio ficam para o próximo resumo
    with _TravaDesfechos():
        est = carregar_desfechos()
        atuais = list(est.get("pendentes") or [])
        est["pendentes"] = [r for r in atuais if r not in pend]
        est.setdefault("resumos", []).append(
            {"em": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
             "chat": chat, "n": len(pend), **({"incerto": True} if incerto else {})})
        est["resumos"] = est["resumos"][-20:]
        salvar_desfechos(est)
    return len(pend)


# ----------------------------------------------------------------------
# Rodada
# ----------------------------------------------------------------------
def _idade_min(ts) -> float:
    """Quantos minutos atrás foi o `ts` (0 se não der para ler)."""
    q = RL._quando(ts)                                     # noqa: SLF001
    if q is None:
        return 0.0
    return (_dt.datetime.now(_dt.timezone.utc) - q).total_seconds() / 60.0


def _motivo_sem_aviso(nomes: dict, bot: int, ts) -> str:
    """Por que NÃO vale mandar o aviso agora ("" = pode mandar).

    Duas travas aprendidas no incidente de 20/10/2026:
    - grupo onde o bot NÃO é membro -> o envio dá ACCESS_DENIED, e repetir foi
      o que fez o Bitrix bloquear o `imbot.v2.Chat.Message.send` para a key;
    - traço velho -> aviso atrasado não ajuda ninguém e vira retentativa eterna.
    """
    ids = {str(k) for k in (nomes or {})}
    if ids and str(bot) not in ids:
        return "o bot não é membro do grupo"
    idade = _idade_min(ts)
    if idade > JANELA_AVISO_MIN:
        return f"traço de {int(idade)} min atrás (janela de {JANELA_AVISO_MIN} min)"
    return ""


def _agora_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _ids_ignorados(conf: dict, chat: str, bot: int) -> set:
    """IDs de autor que NUNCA recebem resposta (PT-03): o próprio bot, os
    usuários-bot do chat (lista de membros) e `pergunta_traco.ignorar_ids`."""
    bloco = conf.get("pergunta_traco") or {}
    ids = {str(bot)} | set(_BOT_IDS.get(chat) or set())
    ids |= {str(x) for x in (bloco.get("ignorar_ids") or [])}
    return ids


def _e_do_bot(m: dict, ids_ignorar: set, nomes_ignorar: set) -> bool:
    """Autor é bot/conector? Compara o ID (PT-03). O NOME exibido só é usado
    quando a mensagem não traz `autor_id` (leitor antigo)."""
    aid = m.get("autor_id")
    if aid not in (None, ""):
        return str(aid) in ids_ignorar
    return (m.get("autor") or "") in nomes_ignorar


def _rotulo_autor(m: dict) -> str:
    """Como o autor aparece no LOG: só o ID (nome de pessoa é dado pessoal)."""
    aid = m.get("autor_id")
    return f"autor_id={aid}" if aid not in (None, "") else "autor_id=?"


def _mensagem_do_bot_apos(mensagens: list, bot: int, traco: int, ate: int | None,
                          id_msg=None) -> str | None:
    """Reconciliação: id da mensagem do bot que responde ao `traco` — a primeira
    mensagem do bot depois do traço e antes do próximo traço de pessoa (`ate`)."""
    for mm in mensagens:
        try:
            i = int(mm.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if id_msg not in (None, "") and str(i) == str(id_msg):
            return str(i)
        if i <= traco or (ate is not None and i >= ate):
            continue
        if str(mm.get("autor_id") or "") == str(bot):
            return str(i)
    return None


def _id_resposta(r) -> str | None:
    """Id da mensagem criada, se a API devolveu (formatos vistos: data.id, result, id)."""
    if not isinstance(r, dict):
        return None
    d = r.get("data")
    for v in ((d.get("id") if isinstance(d, dict) else d), r.get("result"), r.get("id")):
        if isinstance(v, (int, str)) and str(v).isdigit():
            return str(v)
    return None


def _enviar_registro(est: dict, reg: dict, chave: str, bot: int, variar: bool = False) -> str:
    """Faz UM envio do registro (já gravado como PENDENTE) e devolve o novo estado.

    Não grava: quem chama grava em seguida. Se essa gravação falhar, o disco fica
    com PENDENTE, que a rodada seguinte trata como ENVIO_INCERTO (não reenvia).
    """
    chat = reg["chat"]
    texto = _variar(reg["texto"]) if variar else reg["texto"]
    reg["tentativas"] = int(reg.get("tentativas") or 0) + 1
    reg["ultima_tentativa_ts"] = time.time()
    try:
        r = bot_fala(chave, bot, chat, texto, BOTOES if reg.get("botoes") else None)
    except ErroEnvioBloqueado as e:
        reg["estado"], reg["motivo"] = ADIADO, "bloqueio do Bitrix (ERROR_LOOP_DETECTED)"
        print(f"  ⛔ envio bloqueado pelo Bitrix (traço {reg['traco']} fica ADIADO): "
              f"{_mascarar(e)[:160]}", flush=True)
    except ErroEnvioAdiado as e:
        reg["estado"], reg["motivo"] = ADIADO, "rate limit"
        print(f"  ⏸️  rate limit esgotado em {chat} (traço {reg['traco']} ADIADO): "
              f"{_mascarar(e)[:120]}", flush=True)
    except ErroEnvioSemAcesso as e:
        reg["estado"], reg["motivo"] = FALHA, "ACCESS_DENIED"
        est.setdefault("sem_acesso", {})[chat] = time.time()
        print(f"  ⛔ sem permissão para falar em {chat} (não repito por "
              f"{PAUSA_SEM_ACESSO_S // 60} min): {_mascarar(e)[:160]}", flush=True)
    except ErroEnvioRecusado as e:
        reg["estado"], reg["motivo"] = FALHA, "recusado (4xx)"
        print(f"  ✗ envio recusado em {chat} (não repito): {_mascarar(e)[:160]}", flush=True)
    except Exception as e:                                     # noqa: BLE001
        # timeout/rede/5xx DEPOIS do POST: pode ter saído -> não repetir às cegas
        reg["estado"], reg["motivo"] = ENVIO_INCERTO, type(e).__name__
        reg["incerto_desde"] = time.time()
        print(f"  ❓ envio INCERTO em {chat} (traço {reg['traco']}; não reenvio às cegas): "
              f"{_mascarar(e)[:160]}", flush=True)
    else:
        reg["estado"], reg["motivo"] = CONFIRMADO, ""
        id_msg = _id_resposta(r)
        if id_msg:
            reg["id_msg"] = id_msg
    reg["atualizado_em"] = _agora_iso()
    return reg["estado"]


def _podar_envios(est: dict) -> None:
    envios = est.get("envios") or {}
    if len(envios) <= MANTER_ENVIOS:
        return
    finais = sorted((k for k, r in envios.items() if r.get("estado") in (CONFIRMADO, FALHA)),
                    key=lambda k: float(envios[k].get("criado_ts") or 0))
    for k in finais[: len(envios) - MANTER_ENVIOS]:
        envios.pop(k, None)


def _proximo_traco(ids_tracos: list, traco: int) -> int | None:
    maiores = [i for i in ids_tracos if i > traco]
    return min(maiores) if maiores else None


def _reconciliar_envios(est: dict, chat: str, chave: str, bot: int,
                        mensagens: list, ids_tracos: list) -> None:
    """Resolve os envios pendentes de rodadas anteriores DESTE chat (PT-01).

    - PENDENTE achado no disco: o processo caiu/gravação falhou depois de mandar
      -> ENVIO_INCERTO (pode ter saído);
    - ENVIO_INCERTO: procura a mensagem do bot no grupo -> CONFIRMADO; se a leitura
      cobre o traço, a mensagem NÃO está lá e já passou JANELA_REENVIO_INCERTO_S,
      reenvia UMA vez (texto variado). Traço velho -> FALHA;
    - ADIADO (recusa sem execução): tenta de novo fora do bloqueio, poucas vezes.
    Cada (re)envio grava PENDENTE antes do POST.
    """
    envios = est.get("envios") or {}
    agora = time.time()
    ids_lidos = []
    for mm in mensagens:
        try:
            ids_lidos.append(int(mm.get("id") or 0))
        except (TypeError, ValueError):
            pass
    menor_lido = min(ids_lidos) if ids_lidos else None
    mudou = False
    for chave_env in sorted(envios, key=lambda k: int(envios[k].get("traco") or 0)):
        reg = envios[chave_env]
        if reg.get("chat") != chat:
            continue
        traco = int(reg.get("traco") or 0)
        estado = reg.get("estado")
        if estado == PENDENTE:
            reg["estado"] = estado = ENVIO_INCERTO
            reg["incerto_desde"] = float(reg.get("ultima_tentativa_ts")
                                         or reg.get("criado_ts") or agora)
            reg["motivo"] = "PENDENTE sem desfecho gravado (talvez entregue)"
            mudou = True
        if estado not in (ENVIO_INCERTO, ADIADO):
            continue
        achada = _mensagem_do_bot_apos(mensagens, bot, traco,
                                       _proximo_traco(ids_tracos, traco), reg.get("id_msg"))
        if achada:
            reg.update(estado=CONFIRMADO, reconciliado=True, id_msg=achada,
                       motivo="", atualizado_em=_agora_iso())
            print(f"  ✔ envio do traço {traco} em {chat} confirmado na reconciliação", flush=True)
            mudou = True
            continue
        velho = _idade_min(reg.get("traco_ts")) > JANELA_AVISO_MIN
        if estado == ENVIO_INCERTO:
            reenvios = int(reg.get("reenvios") or 0)
            if velho or (reenvios >= MAX_REENVIOS_INCERTO and agora - float(
                    reg.get("incerto_desde") or agora) >= JANELA_REENVIO_INCERTO_S):
                reg.update(estado=FALHA, motivo="incerto: não confirmado no grupo",
                           atualizado_em=_agora_iso())
                mudou = True
                continue
            coberto = menor_lido is not None and menor_lido <= traco
            maduro = agora - float(reg.get("incerto_desde") or agora) >= JANELA_REENVIO_INCERTO_S
            if not (coberto and maduro) or bloqueio_ativo():
                continue
            reg["reenvios"] = reenvios + 1
        else:   # ADIADO
            if bloqueio_ativo():
                continue
            if velho or int(reg.get("tentativas") or 0) >= MAX_TENTATIVAS_ADIADO:
                reg.update(estado=FALHA, motivo=f"adiado: {reg.get('motivo') or ''}".strip(),
                           atualizado_em=_agora_iso())
                mudou = True
                continue
            if agora - float(reg.get("ultima_tentativa_ts") or 0) < ESPACO_ADIADO_S:
                continue
        reg["estado"] = PENDENTE
        salvar(est)                      # PENDENTE no disco ANTES do POST
        _enviar_registro(est, reg, chave, bot, variar=True)
        salvar(est)
        mudou = False
    if mudou:
        salvar(est)


def rodada(conf: dict, chave: str, chat: str, bot: int, ativo: bool,
           disparar=None, silencioso: bool = False) -> dict:
    """Uma rodada no grupo. `disparar(chat)` é chamado a cada traço NOVO — é o gancho
    que faz a esteira (triagem_grupo) baixar os documentos NA HORA, sem esperar o
    ciclo de 120s (pedido do dono: *"pegar na hora, sem delay"*).

    Modo ativo (PT-01): a transição do lote (aberto/hist/ultimo_traco) e o
    registro PENDENTE do aviso são gravados ANTES do POST. Sem gravar, não envia
    (ErroGravacaoEstado sobe). Durante a pausa de gravação a rodada nem lê o grupo.
    """
    if ativo and pausa_gravacao_restante() > 0:
        return {}
    est = carregar()
    est.setdefault("perguntas", {})
    bots, nomes = membros_do_chat(chave, chat)
    bloco_conf = conf.get("pergunta_traco") or {}
    ignorar = set(bots) | set(bloco_conf.get("ignorar_autores") or [])
    ignorar_ids = _ids_ignorados(conf, chat, bot)
    try:
        mensagens = tg.buscar_mensagens_api(chave, chat, max_paginas=2, cfg=conf)
    except Exception as e:                                     # noqa: BLE001
        print(f"  ⚠ não li {chat}: {_mascarar(e)[:200]}")
        return est
    mensagens = sorted(mensagens, key=lambda m: m.get("id") or 0)
    try:
        _d = (_req(f"{API}/v1/chats/{chat}", chave).get("data") or {})
        tipo_chat = str(_d.get("entityType") or _d.get("type") or "").lower()
    except Exception:                                          # noqa: BLE001
        tipo_chat = ""
    eh_zap = tipo_chat in ("lines", "imopenlines") or "zap" in tipo_chat
    if not silencioso:      # no loop de 8s isto repetia a cada rodada e inflava o log
        print(f"grupo {chat}: {len(mensagens)} mensagens lidas · tipo={tipo_chat or '?'} · "
              f"botões={'NÃO (WhatsApp: só texto)' if eh_zap else 'sim'} · "
              f"ids ignorados: {sorted(ignorar_ids) or '—'}")

    def _eh_traco(m: dict) -> bool:
        txt_ = (m.get("texto") or m.get("texto_bruto") or "")
        return m.get("sinal") is True or (
            tg.e_sinal_traco(txt_, conf) and not m.get("reacao") and not m.get("sistema"))

    ids_tracos = []
    for m in mensagens:
        try:
            if _eh_traco(m) and not _e_do_bot(m, ignorar_ids, ignorar):
                ids_tracos.append(int(m.get("id") or 0))
        except (TypeError, ValueError):
            pass

    # 0) envios que ficaram sem desfecho em rodadas anteriores (PT-01)
    if ativo:
        _reconciliar_envios(est, chat, chave, bot, mensagens, ids_tracos)

    # (o corte real é por ID: `ultimo_traco` — nunca reage a traço já respondido)
    for m in mensagens:
        mid = str(m.get("id"))
        txt = (m.get("texto") or m.get("texto_bruto") or "")
        if not mid or not txt:
            continue
        autor = m.get("autor") or ""
        # o traço chega SEM texto útil (texto="") — o campo `sinal` do fase 1 é o certo
        eh_traco = _eh_traco(m)
        eh_bot = _e_do_bot(m, ignorar_ids, ignorar)

        # TRAÇO DE PESSOA -> alterna ABRE / FECHA (o bot avisa os dois lados)
        if eh_traco and not eh_bot:
            # Marco por ID: na primeira rodada eu anoto o último id do grupo e só
            # reajo a traço NOVO. Isso evita a enxurrada de histórico (os 6/9 do
            # teste) sem impedir que um lote aberto por horas seja fechado depois.
            corte = est.setdefault("corte_id", {})
            if chat not in corte:
                corte[chat] = max([int(x.get("id") or 0) for x in mensagens] or [0])
                print(f"  marco inicial {chat}: id {corte[chat]} (só traço novo)")
                continue
            if int(mid) <= int(corte[chat]):
                continue
            # Cada traço é tratado UMA vez: sem isto, a rodada seguinte relê a
            # mesma janela e responde de novo os mesmos traços (bug grave medido:
            # "Lote aberto"/"Lote fechado" repetidos a cada 1 min no grupo).
            ultimo_traco = est.setdefault("ultimo_traco", {})
            ultimo_traco.setdefault(chat, int(corte[chat]))
            if int(mid) <= int(ultimo_traco[chat]):
                continue
            aberto = est.setdefault("aberto", {})
            st = aberto.get(chat)
            # Mensagens DESTE bloco (depois do traço que abriu, até este traço):
            # dão a contagem de arquivos e o nome do cliente do relatório.
            do_bloco = []
            for mm in mensagens:
                try:
                    if st and int(st.get("msg") or 0) < int(mm.get("id") or 0) <= int(mid):
                        do_bloco.append(mm)
                except (TypeError, ValueError):
                    pass
            n = sum(len(mm.get("file_ids") or []) for mm in do_bloco)
            nome = (RL.nome_do_bloco([mm.get("texto") or mm.get("texto_bruto")
                                      for mm in do_bloco
                                      if not _e_do_bot(mm, ignorar_ids, ignorar)])
                    or RL.nome_do_lote([x for mm in do_bloco for x in (mm.get("nomes") or [])]))
            timeout_min = int(conf.get("timeout_lote_min") or RL.TIMEOUT_PADRAO_MIN)
            marca = RL.abre_ou_fecha(st, m.get("ts"), timeout_min)
            # (fase 5, mudanca[6]a) o que sai AQUI é o REGISTRO do lote — sem ✅
            # de processamento, porque a esteira ainda nem rodou (o lote pode ser
            # descartado, morrer no worker ou ficar em revisão). O desfecho
            # honesto sai depois: registrar_desfecho(...) + --resumo.
            texto_bot, novo_aberto = RL.decidir_resposta(
                st, nome=nome, n_arquivos=n, sinal=SINAL, msg=mid, ts=m.get("ts"),
                autor=autor, timeout_min=timeout_min)
            if marca == "FECHA":
                # (log sem nome do cliente: dado pessoal fica só no estado/grupo)
                marca = f"FECHA ({n} arq.)"
                # DELIMITADOR: o MESMO traço que fecha já abre o bloco de baixo.
            quem = _rotulo_autor(m)
            if not ativo:
                print(f"  [simulado] {marca} (traço {mid} de {quem}) -> {texto_bot[:40]!r}")
            # A transição do lote AVANÇA uma vez por traço, saia ou não o aviso:
            # o aviso é notificação; o estado do lote não pode depender da rede.
            if novo_aberto is None:
                aberto.pop(chat, None)
            else:
                aberto[chat] = novo_aberto
            est.setdefault("hist", []).append(
                {"chat": chat, "msg_traco": mid, "ts": str(m.get("ts")),
                 "autor": autor, "autor_id": m.get("autor_id"), "acao": marca})
            est["hist"] = est["hist"][-20:]
            est["ultimo_traco"][chat] = int(mid)   # marca como já tratado
            if ativo:
                motivo_skip = _motivo_sem_aviso(nomes, bot, m.get("ts"))
                sem_acesso = float((est.get("sem_acesso") or {}).get(chat) or 0)
                if not motivo_skip and time.time() - sem_acesso < PAUSA_SEM_ACESSO_S:
                    motivo_skip = "ACCESS_DENIED recente neste chat (não repito)"
                if motivo_skip:
                    # Estado AVANÇA mesmo sem enviar: senão o mesmo traço é
                    # retentado a cada 8s (foi o que alimentou o bloqueio do Bitrix).
                    print(f"  ⏭️  sem aviso em {chat}: {motivo_skip} "
                          f"(traço {mid} de {quem})", flush=True)
                    salvar(est)
                    _gravar_saude(ok=False, erro=f"aviso não enviado: {motivo_skip}",
                                  chat=chat, traco=mid)
                else:
                    reg = {"estado": PENDENTE, "chat": chat, "traco": int(mid),
                           "traco_ts": str(m.get("ts")), "texto": texto_bot,
                           "botoes": not eh_zap, "tentativas": 0, "reenvios": 0,
                           "criado_ts": time.time(), "criado_em": _agora_iso()}
                    est.setdefault("envios", {})[f"{chat}:{mid}"] = reg
                    _podar_envios(est)
                    salvar(est)              # PENDENTE + transição no disco ANTES do POST
                    if _enviar_registro(est, reg, chave, bot) == CONFIRMADO:
                        print(f"  🤖 {marca} (traço {mid} de {quem})", flush=True)
                    salvar(est)
            # ⏱️ SEM DELAY: manda a esteira pegar os documentos AGORA (o ciclo
            # periódico continua existindo como rede de segurança).
            if disparar is not None:
                try:
                    disparar(chat, motivo=f"traço {mid}")
                except Exception as e:                 # noqa: BLE001
                    print(f"  ⚠ não consegui disparar o ciclo da esteira: {_mascarar(e)[:160]}")
            continue

        # 2) resposta da MESMA pessoa depois do traço -> confirma (modo legado de teste)
        for traco_id, reg in est["perguntas"].items():
            if reg.get("status") not in ("perguntou", "perguntaria", "erro"):
                continue
            mesmo_autor = (str(reg.get("autor_id")) == str(m.get("autor_id"))
                           if reg.get("autor_id") not in (None, "")
                           and m.get("autor_id") not in (None, "")
                           else (reg.get("autor") or "") == autor)
            if not mesmo_autor or not mid.isdigit() or int(mid) <= int(traco_id):
                continue
            if eh_bot or eh_traco or m.get("reacao"):
                continue
            if not _e_resposta(txt, m, conf):
                continue
            if RX_SIM.search(txt):
                reg["status"], reg["resposta"] = "aberto", txt[:120]
                destino = TEXTO_SIM
            elif RX_NAO.search(txt):
                reg["status"], reg["resposta"] = "recusado", txt[:120]
                destino = TEXTO_NAO
            else:
                continue
            if ativo:
                try:
                    bot_fala(chave, bot, chat, destino)
                    print(f"  🤖 respondi {reg['status']} para {_rotulo_autor(m)}")
                except Exception as e:                         # noqa: BLE001
                    print(f"  ✗ falha ao responder: {_mascarar(e)[:160]}")
            else:
                print(f"  [simulado] responderia '{reg['status']}' "
                      f"(resposta: {txt[:40]!r})")
            break

    if ativo:
        salvar(est)
    return est


def criar_disparador(ativo: bool):
    """`disparar(chat, motivo)` -> roda UM ciclo da esteira agora, naquele grupo.

    É o que tira o atraso: sem isso o documento esperava o próximo ciclo periódico
    (120s). Um ciclo por grupo de cada vez (se já tem um rodando, não empilha).
    """
    procs: dict = {}

    def disparar(chat: str, motivo: str = "") -> None:
        atual = procs.get(chat)
        if atual is not None and atual.poll() is None:
            print(f"  (esteira já está rodando em {chat} — não disparo outro)", flush=True)
            return
        if not ativo:
            print(f"  [simulado] dispararia a esteira em {chat} ({motivo})")
            return
        # (PT-04) a saída do ciclo disparado vai para pergunta-disparo.log (antes:
        # DEVNULL — erros desse ciclo não apareciam em lugar nenhum). A disputa com o
        # ciclo periódico já é resolvida pela `trava_de_ciclo` (ciclo.lock) do
        # próprio triagem_grupo.py.
        env = dict(os.environ)
        env.setdefault("TRIAGEM_DIR", BASE)      # triagem_grupo lê TRIAGEM_DIR
        env.setdefault("TRIAGEM_BASE", BASE)
        try:
            log = open(ARQ_DISPARO_LOG, "ab")
        except OSError as e:
            print(f"  ⚠ não abri {os.path.basename(ARQ_DISPARO_LOG)} ({_mascarar(e)}) — "
                  f"esteira não disparada; o ciclo periódico cobre", flush=True)
            return
        try:
            log.write(f"\n=== {_agora_iso()} disparo em {chat} ({motivo}) ===\n".encode())
            log.flush()
            procs[chat] = subprocess.Popen(
                [sys.executable, os.path.join(_AQUI, "triagem_grupo.py"), "--dialog-id", chat],
                cwd=BASE if os.path.isdir(BASE) else _AQUI, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True, env=env)
        finally:
            log.close()                          # o filho herdou o descritor
        print(f"  ⏱️  esteira disparada AGORA em {chat} ({motivo})", flush=True)

    return disparar


_TRAVA_FD: int | None = None


def trava_de_instancia():
    """Trava de instância única do bot (dois processos = resposta dobrada no grupo).

    (PT-02) `caminhos.em_base("pergunta-traco.lock")`, tomada em TODOS os modos que
    leem o grupo, enviam ou mexem no estado — não só no `--loop`. Devolve o fd
    (mantido aberto até o processo sair) ou None se já existe outro bot rodando.
    """
    global _TRAVA_FD
    try:
        fd = os.open(ARQ_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        print(f"  ⛔ não abri a trava {ARQ_LOCK}: {_mascarar(e)}", flush=True)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    _TRAVA_FD = fd
    return fd


def carregar_config_do_bot(exigir_modo: bool = True) -> dict:
    """(C3) Config da esteira para o bot. Levanta `tg.ConfigInvalida` se o
    config.json é inválido ou, com `exigir_modo`, se `modo_traco` (o MESMO que
    a esteira usa, via `tg.modo_traco`) não é um modo que o bot implementa."""
    conf = tg.carregar_config()
    if exigir_modo:
        modo = tg.modo_traco(conf)
        if modo not in MODOS_DO_BOT:
            raise tg.ConfigInvalida(
                f"modo_traco={modo!r} na esteira, mas o bot do traço só implementa "
                f"{', '.join(MODOS_DO_BOT)} — os avisos no grupo contradiriam o "
                f"agrupamento real")
    return conf


def _aviso_config(e: Exception) -> str:
    return (f"🚨 pergunta_traco NÃO rodou: config inválido — {_mascarar(e)[:300]}. "
            f"Corrija o config.json (nada foi lido nem enviado).")


def esperar_config_valida(exigir_modo: bool = True, dormir=None) -> dict:
    """(C3) --loop: relê o config até ficar válido, com pausa CRESCENTE
    (PAUSA_CONFIG_INICIAL_S .. PAUSA_CONFIG_MAX_S). Não sai do processo — sair
    faria o systemd reiniciar em laço rápido."""
    dormir = dormir or time.sleep
    pausa = PAUSA_CONFIG_INICIAL_S
    while True:
        try:
            return carregar_config_do_bot(exigir_modo)
        except tg.ConfigInvalida as e:
            print(f"{_aviso_config(e)} Nova tentativa em {pausa}s.", flush=True)
            dormir(pausa)
            pausa = min(pausa * 2, PAUSA_CONFIG_MAX_S)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pergunta no traço (teste)")
    ap.add_argument("--ativo", action="store_true", help="envia de verdade")
    ap.add_argument("--chat", action="append", default=None)
    ap.add_argument("--bot", type=int, default=None)
    ap.add_argument("--para", type=str, default=None,
                    help="manda a pergunta para este dialog (teste; ex.: 1 = privado do Operador)")
    ap.add_argument("--testar", action="store_true", help="prova o envio como bot e sai")
    ap.add_argument("--limpar", action="store_true")
    ap.add_argument("--loop", type=int, default=0, metavar="SEG",
                    help="fica na conversa: roda a cada SEG segundos (ex.: 8) — é o "
                         "'full time sem delay'")
    ap.add_argument("--sem-disparar", action="store_true",
                    help="não dispara o ciclo da esteira a cada traço (só responde)")
    ap.add_argument("--registrar-desfecho", choices=list(RL.DESFECHOS_VALIDOS),
                    default=None, metavar="STATUS",
                    help="fase 5: registra o desfecho de um lote para o resumo do dia "
                         "(enviado|descartado|em_revisao|erro) — exige --lote-id")
    ap.add_argument("--lote-id", type=str, default=None,
                    help="id do lote do desfecho (com --registrar-desfecho)")
    ap.add_argument("--nome", type=str, default=None,
                    help="nome do cliente do desfecho")
    ap.add_argument("--n-arquivos", type=int, default=0,
                    help="quantidade de arquivos do desfecho")
    ap.add_argument("--detalhe", type=str, default="",
                    help="motivo do desfecho ('kit repetido', 'Sem NOME', …)")
    ap.add_argument("--resumo", action="store_true",
                    help="fase 5: emite o resumo DIÁRIO dos desfechos pendentes "
                         "(o desfecho posterior do lote, 1 msg para o grupo)")
    a = ap.parse_args()

    # (PT-02) instância única em todo modo que lê o grupo/envia/mexe no estado.
    # --registrar-desfecho e --resumo usam a trava própria do arquivo de desfechos
    # (são chamados pela esteira/timer com o bot no ar; a trava do bot os barraria).
    if not (a.registrar_desfecho or a.resumo):
        if trava_de_instancia() is None:
            print("⚠️  já existe um bot rodando (trava de instância "
                  f"{os.path.basename(ARQ_LOCK)}) — saindo sem ler nem enviar "
                  f"(código {SAIDA_OCUPADO})", flush=True)
            return SAIDA_OCUPADO

    if a.limpar:
        if os.path.exists(ARQ_ESTADO):
            os.remove(ARQ_ESTADO)
        print("estado zerado")
        return 0

    # (C3) config inválido = erro visível, sem traceback e sem reenvio em laço.
    # O modo do traço só é exigido onde o bot lê traços (desfecho/resumo não).
    exigir_modo = not (a.registrar_desfecho or a.resumo)
    if a.loop:
        conf = esperar_config_valida(exigir_modo)
    else:
        try:
            conf = carregar_config_do_bot(exigir_modo)
        except tg.ConfigInvalida as e:
            print(f"{_aviso_config(e)} (código {SAIDA_CONFIG})", flush=True)
            return SAIDA_CONFIG
    bloco = conf.get("pergunta_traco") or {}
    chave = tg.ler_chave_bot()
    bot = a.bot or bloco.get("bot_id") or BOT_PADRAO
    chats = a.chat or bloco.get("chats") or CHATS_PADRAO
    ativo = bool(a.ativo or (bloco.get("ativo") and not a.para))

    print(f"bot={bot}  chats={chats}  modo={'ATIVO' if ativo else 'SIMULADO'}"
          f"{f'  loop={a.loop}s' if a.loop else ''}")

    # Fase 5 (mudanca[6]b): gatilhos do desfecho POSTERIOR do lote.
    if a.registrar_desfecho:
        if not a.lote_id:
            print("✗ --registrar-desfecho exige --lote-id")
            return 1
        reg = registrar_desfecho(a.lote_id, a.registrar_desfecho, nome=a.nome,
                                 n_arquivos=a.n_arquivos, detalhe=a.detalhe,
                                 chat=(chats[0] if chats else None))
        print(f"✓ desfecho registrado: {reg['lote_id']} -> {reg['status']} "
              f"(sai no próximo --resumo)")
        return 0

    if a.resumo:
        n = emitir_resumo_desfechos(chave, bot, chats[0] if chats else "", ativo)
        print(f"✓ resumo diário com {n} desfecho(s) "
              f"{'enviado' if ativo else '(simulado — use --ativo)'}" if n
              else "nenhum desfecho pendente")
        return 0

    if a.testar or a.para:
        destino = a.para or (chats[0] if chats else "1")
        try:
            r = bot_fala(chave, bot, destino, TEXTO_PERGUNTA, BOTOES)
            print(f"✓ falei como bot no dialog {destino}: {json.dumps(r)[:180]}")
            est = carregar()
            est.setdefault("testes", []).append(
                {"em": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 "dialog": destino, "resposta": r})
            salvar(est)
        except Exception as e:                                  # noqa: BLE001
            print(f"✗ falhou: {_mascarar(e)[:300]}")
            return 1
        if not a.chat:
            return 0

    disparar = None if a.sem_disparar else criar_disparador(ativo)

    if a.loop:
        print(f"👀 full time na conversa: a cada {a.loop}s (Ctrl+C para sair)", flush=True)
        while True:
            t0 = time.time()
            # Relê a chave do ARQUIVO a cada ciclo: se ela rotacionou depois do
            # boot, o processo ficava com a chave antiga em memória e o bot
            # falava ACCESS_DENIED até alguém reiniciar na mão (relatado em 16/10).
            try:
                chave = tg.ler_chave_bot()
            except Exception as e:                             # noqa: BLE001
                print(f"  ⚠ não reli a chave do bot ({_mascarar(e)[:160]}) — sigo com "
                      f"a anterior", flush=True)
            for chat in chats:
                try:
                    rodada(conf, chave, chat, bot, ativo, disparar=disparar,
                           silencioso=not (a.debug if hasattr(a, "debug") else False))
                except ErroGravacaoEstado:
                    break                       # já avisou; pausa vale p/ todos os chats
                except Exception as e:                          # noqa: BLE001
                    print(f"  ✗ rodada falhou em {chat} (sigo): {_mascarar(e)[:200]}",
                          flush=True)
            time.sleep(max(0.5, a.loop - (time.time() - t0)))

    codigo = 0
    for chat in chats:
        try:
            rodada(conf, chave, chat, bot, ativo, disparar=disparar)
        except ErroGravacaoEstado:
            return 1                            # sem estado gravado, não segue p/ outros chats
        except Exception as e:                                  # noqa: BLE001
            print(f"  ✗ rodada falhou em {chat}: {_mascarar(e)[:200]}", flush=True)
            codigo = 1
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
