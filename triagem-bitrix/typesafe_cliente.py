"""Cliente TypeSafe/JEV (System One) — stdlib, desligado por padrão (frente J2).

    perguntar(state, questions, *, model=None, timeout=20, _transporte=None, config=None) -> dict
        POST {base_url}/v1/systemone com `Authorization: Bearer <chave>`.
        Devolve {"answers": {...}, "usage": {...}, "model": str, "latencia_ms": int}.
    modelos(*, timeout=10, _transporte=None, config=None) -> list[str]   GET /v1/models
    validar_questoes(questions) -> None          ValueError local ANTES de qualquer envio
    carregar_config(caminho=None) -> dict        bloco "typesafe" do config.json com padrões
    ler_chave(config=None) -> str                seguranca.ler_segredo("typesafe_key")

Contrato (https://docs.typesafe.ai/llms.txt, conferido em 09/11/2026):
  choice -> criteria OBJETO opção->descrição; resposta {choice, confidence, probabilities}
  score  -> criteria LISTA ordenada 2..10; resposta {score (0-indexado, pode ser fracionário),
            confidence, legend, probabilities}
  noul   -> criteria OPCIONAL {true, false}; resposta {noul: 0..1} SEM confidence
  (a doc também tem `box` — localização em imagem; não usado aqui, recusado na validação)
  IDs das questões não vão ao modelo: o sentido completo fica em `instructions`.

Erros:
  401/403          -> ErroCredencial (permanente; não repete)
  429/529/5xx/rede -> ErroTransitorio (repete até MAX_REPETICOES vezes, pausa curta,
                      respeita Retry-After limitado a ESPERA_MAX_S)
  400/404/422      -> ErroRequisicao (erro de programação: shape/modelo; não repete)
Segurança: a chave nunca vai a log, exceção ou repr; o `state` nunca é logado
(pode conter CPF/nome) — o log traz só ids das questões, latência e tokens.
O corpo de erro 422 da API ecoa trecho da entrada (`input`): só `type`/`loc` são aproveitados.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

import seguranca

log = logging.getLogger("triagem.typesafe")

ARQ_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
NOME_SEGREDO = "typesafe_key"          # /etc/triagem-bitrix/typesafe_key (ou env TRIAGEM_TYPESAFE_KEY)
# legados (em seguranca._legados_padrao): caminhos.em_base("typesafe.key"), /root/.typesafe-jev.key

CONFIG_PADRAO = {
    "ativo": False,                     # interruptor geral: false = nenhuma chamada
    "modo": "desligado",                # desligado | sombra | decide (usado por decisoes_jev)
    "base_url": "https://api.typesafe.ai",
    "model": "jev-latest",
    "key_file": None,                   # caminho extra explícito (opcional), mesmas checagens de permissão
    "timeout": 20,
    "sombra_jsonl": None,               # padrão: caminhos.em_base("jev-decisoes.jsonl")
}

TIPOS_ACEITOS = ("choice", "score", "noul")
NIVEIS_SCORE_MIN = 2
NIVEIS_SCORE_MAX = 10
MAX_REPETICOES = 2                      # tentativas extras em erro transitório
ESPERA_BASE_S = 0.5                     # 0,5 s, 1 s
ESPERA_MAX_S = 5.0                      # teto para Retry-After (o lote não pode travar)
STATUS_TRANSITORIOS = {429, 529, 500, 502, 503, 504}

_dormir = time.sleep                    # trocado nos testes


class ErroTypeSafe(RuntimeError):
    """Falha ao consultar o JEV. `classe`: credencial | transitorio | requisicao |
    resposta | desligado. Mensagem nunca contém a chave nem o state."""

    classe = "desconhecido"
    permanente = True

    def __init__(self, mensagem: str, status: int | None = None):
        super().__init__(seguranca.mascarar(mensagem))
        self.status = status


class ErroCredencial(ErroTypeSafe):
    classe = "credencial"


class ErroTransitorio(ErroTypeSafe):
    classe = "transitorio"
    permanente = False


class ErroRequisicao(ErroTypeSafe):
    """4xx de shape/modelo: erro de programação, não de operação."""
    classe = "requisicao"


class ErroResposta(ErroTypeSafe):
    """Resposta 200 fora do contrato (JSON inválido ou sem a resposta pedida)."""
    classe = "resposta"


class TypeSafeDesligado(ErroTypeSafe):
    classe = "desligado"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def carregar_config(caminho: str | None = None) -> dict:
    """Bloco `typesafe` do config.json mesclado aos padrões. Arquivo ausente,
    ilegível ou bloco ausente/não-objeto => padrão (ativo=False): o JEV é opcional
    e nunca pode derrubar a triagem."""
    cfg = dict(CONFIG_PADRAO)
    try:
        with open(caminho or ARQ_CONFIG, encoding="utf-8") as fh:
            bruto = json.load(fh)
    except (OSError, ValueError):
        return cfg
    bloco = bruto.get("typesafe") if isinstance(bruto, dict) else None
    if isinstance(bloco, dict):
        cfg.update({k: v for k, v in bloco.items() if not str(k).startswith("_")})
    cfg["ativo"] = cfg.get("ativo") is True
    return cfg


def _cfg(config: dict | None) -> dict:
    if config is None:
        return carregar_config()
    cfg = dict(CONFIG_PADRAO)
    cfg.update(config)
    return cfg


_RX_CHAVE_VALIDA = re.compile(r"[\x21-\x7e]+")   # ASCII imprimível, sem espaço


def ler_chave(config: dict | None = None) -> str:
    """Chave via seguranca.ler_segredo (env -> /etc/triagem-bitrix/typesafe_key ->
    legados explícitos). `key_file` do config é um caminho extra, lido com as mesmas
    checagens de permissão (modo estrito vira SegredoRecusado). Ausente => SegredoAusente."""
    cfg = _cfg(config)
    chave = seguranca.ler_segredo(NOME_SEGREDO, obrigatorio=False)
    if not chave and cfg.get("key_file"):
        chave = seguranca._ler_arquivo(NOME_SEGREDO, str(cfg["key_file"]),
                                       estrito=seguranca.modo_estrito())
    if not chave:
        raise seguranca.SegredoAusente(
            f"segredo {NOME_SEGREDO!r} não encontrado (env TRIAGEM_TYPESAFE_KEY, "
            "SEGREDOS_DIR/typesafe_key, legados ou typesafe.key_file)")
    if not _RX_CHAVE_VALIDA.fullmatch(chave):
        # R3/A3: espaço/quebra de linha/controle no meio (arquivo com 2 linhas) faria o
        # http.client levantar ValueError com "Bearer <chave>" na mensagem
        raise seguranca.SegredoInvalido(
            f"segredo {NOME_SEGREDO!r} contém espaço ou caractere de controle (valor não exibido)")
    return chave


# ---------------------------------------------------------------------------
# Validação local do shape (antes de gastar uma chamada paga)
# ---------------------------------------------------------------------------
def validar_questoes(questions) -> None:
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions deve ser um dict não vazio {id: questão}")
    for qid, q in questions.items():
        if not isinstance(qid, str) or not qid:
            raise ValueError("id de questão deve ser string não vazia")
        if not isinstance(q, dict):
            raise ValueError(f"questão {qid!r}: deve ser dict")
        tipo = q.get("type")
        if tipo is None:
            raise ValueError(f"questão {qid!r}: 'type' obrigatório (union_tag_not_found)")
        if tipo not in TIPOS_ACEITOS:
            raise ValueError(f"questão {qid!r}: type={tipo!r} não suportado {TIPOS_ACEITOS}")
        instr = q.get("instructions")
        if not instr or not isinstance(instr, (str, dict, list)):
            raise ValueError(f"questão {qid!r}: 'instructions' obrigatório (str, dict ou list)")
        crit = q.get("criteria")
        if tipo == "choice":
            if not isinstance(crit, dict):
                raise ValueError(f"questão {qid!r}: choice exige criteria OBJETO opção->descrição "
                                 "(lista dá dict_type na API)")
            if len(crit) < 2:
                raise ValueError(f"questão {qid!r}: choice exige ao menos 2 opções")
        elif tipo == "score":
            if not isinstance(crit, list):
                raise ValueError(f"questão {qid!r}: score exige criteria LISTA ordenada")
            if not NIVEIS_SCORE_MIN <= len(crit) <= NIVEIS_SCORE_MAX:
                raise ValueError(f"questão {qid!r}: score exige {NIVEIS_SCORE_MIN}..{NIVEIS_SCORE_MAX} "
                                 f"níveis (veio {len(crit)})")
        elif tipo == "noul" and crit is not None:
            if not isinstance(crit, dict) or set(crit) - {"true", "false"}:
                raise ValueError(f"questão {qid!r}: criteria de noul é opcional e, se houver, "
                                 "objeto {true, false}")


# ---------------------------------------------------------------------------
# Transporte
# ---------------------------------------------------------------------------
def _transporte_urllib(metodo: str, url: str, cabecalhos: dict, corpo: bytes | None,
                       timeout: float):
    """-> (status, cabecalhos_resposta: dict, texto). Exceções de rede sobem."""
    req = urllib.request.Request(url, data=corpo, method=metodo, headers=cabecalhos)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            texto = e.read().decode("utf-8", "replace")
        except Exception:
            texto = ""
        return e.code, dict(e.headers.items()) if e.headers else {}, texto


def _retry_after(cabecalhos: dict) -> float | None:
    for k, v in (cabecalhos or {}).items():
        if str(k).lower() == "retry-after":
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                return None
    return None


# R3/A5: só códigos de máquina CONHECIDOS saem na mensagem; o resto vira "<detalhe omitido>".
# (um 5xx de proxy pode ecoar a entrada: nome de cliente, chave sem prefixo...)
CODIGOS_ERRO_CONHECIDOS = frozenset({
    "dict_type", "list_type", "string_type", "int_type", "float_type", "bool_type", "model_type",
    "union_tag_not_found", "union_tag_invalid", "missing", "extra_forbidden", "literal_error",
    "enum", "value_error", "type_error", "json_invalid", "json_type", "too_short", "too_long",
    "string_too_short", "string_too_long", "greater_than", "greater_than_equal", "less_than",
    "less_than_equal", "invalid_request_error", "authentication_error", "permission_error",
    "not_found_error", "rate_limit_error", "rate_limited", "api_error", "overloaded_error",
    "internal_error", "internal_server_error", "server_error", "bad_request", "unauthorized",
    "forbidden", "not_found", "timeout", "service_unavailable", "bad_gateway", "gateway_timeout",
    "validation_error", "insufficient_credits", "quota_exceeded",
})
_OMITIDO = "<detalhe omitido>"
_RX_LOC = re.compile(r"[a-z_]{1,40}")


def _codigo(v) -> str:
    return v if isinstance(v, str) and v in CODIGOS_ERRO_CONHECIDOS else _OMITIDO


def _loc(partes) -> str:
    saida = []
    for x in (partes if isinstance(partes, list) else [])[:4]:
        if type(x) is int or (isinstance(x, str) and _RX_LOC.fullmatch(x)):
            saida.append(str(x))
        else:
            saida.append("?")
    return ".".join(saida)


def _resumo_erro(texto: str) -> str:
    """Só o TIPO do erro (lista CODIGOS_ERRO_CONHECIDOS) e o caminho (segmentos
    [a-z_] ou inteiros). Nunca `input`/`msg`/texto livre: a API ecoa trecho do state."""
    try:
        dados = json.loads(texto)
    except (TypeError, ValueError):
        return "corpo não-JSON"
    tipos = []
    detalhe = dados.get("detail") if isinstance(dados, dict) else None
    if isinstance(detalhe, list):
        for d in detalhe[:5]:
            if isinstance(d, dict):
                tipos.append(f"{_codigo(d.get('type'))}@{_loc(d.get('loc'))}")
    elif isinstance(dados, dict):
        for k in ("type", "code", "error"):
            v = dados.get(k)
            if isinstance(v, dict):  # {"error": {"type": "...", "message": "..."}}
                v = v.get("type") or v.get("code")
            if isinstance(v, str):
                tipos.append(_codigo(v))
                break
    return seguranca.mascarar(", ".join(tipos) or "sem detalhe")


def _chamar(metodo: str, caminho: str, corpo: dict | None, *, cfg: dict, timeout: float,
            transporte, rotulo: str) -> tuple[dict, int]:
    chave = ler_chave(cfg)
    url = str(cfg.get("base_url") or CONFIG_PADRAO["base_url"]).rstrip("/") + caminho
    cab = {"Authorization": "Bearer " + chave, "Accept": "application/json"}
    dados = None
    if corpo is not None:
        cab["Content-Type"] = "application/json"
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
    transporte = transporte or _transporte_urllib
    inicio = time.monotonic()
    ultimo: ErroTypeSafe | None = None
    for tentativa in range(MAX_REPETICOES + 1):
        espera = ESPERA_BASE_S * (2 ** tentativa)
        try:
            invalido = False
            try:
                status, cab_resp, texto = transporte(metodo, url, cab, dados, timeout)
            except ValueError:
                # http.client: "Invalid header value b'Bearer <chave>'" / URL inválida.
                # Nunca propaga a mensagem original (traria a chave). Levanta FORA do
                # except: nem __cause__ nem __context__ guardam o ValueError original.
                invalido = True
            if invalido:
                raise ErroRequisicao(f"{rotulo}: cabeçalho ou URL inválido (valor não exibido)")
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            # socket.timeout é OSError; mensagem da exceção pode trazer a URL, não a chave
            ultimo = ErroTransitorio(f"{rotulo}: falha de rede ({type(e).__name__})")
        else:
            latencia = int((time.monotonic() - inicio) * 1000)
            if 200 <= status < 300:
                try:
                    return json.loads(texto), latencia
                except (TypeError, ValueError):
                    raise ErroResposta(f"{rotulo}: resposta 2xx não é JSON", status)
            if status in (401, 403):
                raise ErroCredencial(f"{rotulo}: HTTP {status} — chave TypeSafe recusada "
                                     "(valor não exibido)", status)
            if status in STATUS_TRANSITORIOS:
                ultimo = ErroTransitorio(f"{rotulo}: HTTP {status} ({_resumo_erro(texto)})", status)
                ra = _retry_after(cab_resp)
                if ra is not None:
                    espera = ra
            else:
                raise ErroRequisicao(f"{rotulo}: HTTP {status} ({_resumo_erro(texto)}) — "
                                     "shape/modelo inválido: erro de programação", status)
        if tentativa < MAX_REPETICOES:
            espera = min(espera, ESPERA_MAX_S)
            log.warning("%s; repetindo em %.1fs (tentativa %d/%d)", ultimo, espera,
                        tentativa + 1, MAX_REPETICOES)
            _dormir(espera)
    assert ultimo is not None
    raise ultimo


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def perguntar(state, questions, *, model: str | None = None, timeout: float | None = None,
              _transporte=None, config: dict | None = None) -> dict:
    """Uma requisição System One. Valida o shape localmente (ValueError) antes de
    ler a chave ou tocar a rede. `config` padrão = bloco typesafe do config.json;
    ativo=False => TypeSafeDesligado (nenhuma chamada)."""
    if not isinstance(state, (dict, list, str)):
        raise ValueError("state deve ser dict, list ou str")
    validar_questoes(questions)
    cfg = _cfg(config)
    if cfg.get("ativo") is not True:
        raise TypeSafeDesligado("typesafe.ativo=false: nenhuma chamada feita")
    corpo = {"model": model or cfg.get("model") or CONFIG_PADRAO["model"],
             "state": state, "questions": questions}
    t = float(timeout if timeout is not None else (cfg.get("timeout") or 20))
    dados, latencia = _chamar("POST", "/v1/systemone", corpo, cfg=cfg, timeout=t,
                              transporte=_transporte, rotulo="typesafe systemone")
    answers = dados.get("answers") if isinstance(dados, dict) else None
    if not isinstance(answers, dict) or any(not isinstance(answers.get(q), dict) for q in questions):
        raise ErroResposta("typesafe systemone: resposta sem todas as answers pedidas")
    usage = dados.get("usage") if isinstance(dados.get("usage"), dict) else {}
    log.info("typesafe: questoes=%s modelo=%s latencia_ms=%d tokens_in=%s tokens_out=%s",
             sorted(questions), dados.get("model"), latencia,
             usage.get("input_tokens"), usage.get("output_tokens"))
    return {"answers": answers, "usage": usage, "model": dados.get("model"),
            "latencia_ms": latencia}


def modelos(*, timeout: float = 10, _transporte=None, config: dict | None = None) -> list:
    """Nomes de modelos disponíveis (GET /v1/models). Mesmas regras de erro/ativo."""
    cfg = _cfg(config)
    if cfg.get("ativo") is not True:
        raise TypeSafeDesligado("typesafe.ativo=false: nenhuma chamada feita")
    dados, _ = _chamar("GET", "/v1/models", None, cfg=cfg, timeout=timeout,
                       transporte=_transporte, rotulo="typesafe models")
    itens = dados.get("data", dados.get("models")) if isinstance(dados, dict) else dados
    saida = []
    for it in itens or []:
        if isinstance(it, str):
            saida.append(it)
        elif isinstance(it, dict) and isinstance(it.get("id") or it.get("name"), str):
            saida.append(it.get("id") or it.get("name"))
    return saida
