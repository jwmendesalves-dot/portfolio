"""Decisões candidatas ao JEV (TypeSafe System One) na triagem — frente J2.

O CÓDIGO é dono do fluxo; o JEV só devolve julgamento sobre fatos já derivados.
Cada decisão tem três peças:
    montar_<decisao>(...)  -> (state, questions)       pura; só fatos e texto mascarado
    rotear_<decisao>(...)  -> dict                     pura; aplica limiares + regra de ouro
    decidir_<decisao>(...) -> dict                     orquestra modo/JEV/falha/sombra; nunca levanta

Todo roteador devolve {"acao", "motivo", "confianca", "fonte"} com
fonte in {"regra", "jev", "falha_jev"}. Ações possíveis:
    "manter_no_kit" | "quarentena" | "revisao_humana" | "separar" | "pedir_reenvio"

Modos (config.json -> typesafe.modo; typesafe.ativo=false força "desligado"):
    desligado -> comportamento atual (regra), nenhuma chamada.
    sombra    -> chama o JEV, grava JSONL (sem CPF cru), DEVOLVE A REGRA. Para medir.
    decide    -> devolve o roteamento do JEV, limitado pela regra de ouro.
Falha do JEV (qualquer exceção, chave ausente, timeout) => regra com fonte "falha_jev".

REGRA DE OURO: o JEV nunca PROMOVE documento de dono desconhecido para o kit.
"manter_no_kit" só sai da regra determinística (CPF com DV válido igual ao do cliente).

PII / trade-off: o state não leva CPF cru. O código calcula o fato
`cpf_do_documento_igual_ao_do_cliente` (sim/não/ausente) e manda só ele; o trecho de OCR
vai truncado (LIMITE_TRECHO_OCR) e passado por seguranca.mascarar (CPF vira ***.456.789-**,
senha some). Perde-se a capacidade do JEV de comparar CPFs com erro de OCR (um dígito
trocado) — de propósito: CPF com DV inválido não é identidade (C2b) e essa comparação fica
com humano. O nome do cliente VAI (é o que o JEV precisa comparar com o texto).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time

import caminhos
import cpf_util
import seguranca
import typesafe_cliente

log = logging.getLogger("triagem.decisoes_jev")

# ---------------------------------------------------------------------------
# Limiares (política do escritório — revisáveis; mudar aqui não exige nova inferência)
# ---------------------------------------------------------------------------
CONFIANCA_MINIMA_PARA_AGIR = 0.85      # abaixo disso o JEV não age: revisão humana
RISCO_MAXIMO_ACEITAVEL = 1.5           # score de risco 0..3; >= isso força revisão humana
LIMITE_TRECHO_OCR = 1500               # caracteres de OCR enviados (menos PII, menos tokens)
MODOS = ("desligado", "sombra", "decide")

ACAO_KIT = "manter_no_kit"
ACAO_QUARENTENA = "quarentena"
ACAO_REVISAO = "revisao_humana"
ACAO_SEPARAR = "separar"
ACAO_REENVIO = "pedir_reenvio"

_RX_CPF_TEXTO = re.compile(r"(?<![\d.,])\d{3}\.?\d{3}\.?\d{3}[-/\s]?\d{2}(?!\d)")


def _resultado(acao: str, motivo: str, fonte: str, confianca: float | None = None, **extra) -> dict:
    d = {"acao": acao, "motivo": motivo, "confianca": confianca, "fonte": fonte}
    d.update(extra)
    return d


def _modo(config: dict | None) -> tuple[str, dict]:
    cfg = typesafe_cliente.carregar_config() if config is None else dict(config)
    modo = cfg.get("modo") if cfg.get("ativo") is True else "desligado"
    return (modo if modo in MODOS else "desligado"), cfg


def _trecho_seguro(texto) -> str:
    """Trecho truncado e mascarado. seguranca.mascarar cobre os formatos de CPF que
    `_RX_CPF_TEXTO` reconhece (inclusive "/" e espaço antes do DV — R3/A1)."""
    return seguranca.mascarar(str(texto or "")[:LIMITE_TRECHO_OCR])


def _numero_finito(v) -> float | None:
    """float só para int/float de verdade (bool NÃO conta) e finito; senão None."""
    if type(v) not in (int, float):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _conf(v) -> float:
    """Confiança do JEV: float finito em [0, 1]. NaN, inf, bool, fora do intervalo,
    string... => 0.0 (abaixo do limiar => revisão humana / padrão). R3/A2."""
    f = _numero_finito(v)
    return f if f is not None and 0.0 <= f <= 1.0 else 0.0


def _score(v, n_niveis: int):
    """-> (score | None, invalido: bool). Válido: finito em [0, n_niveis-1]."""
    if v is None:
        return None, False
    f = _numero_finito(v)
    if f is None or not 0.0 <= f <= n_niveis - 1:
        return None, True
    return f, False


def cpfs_validos_no_texto(texto) -> list[str]:
    """CPFs com DV válido (cpf_util) encontrados no OCR, sem repetição, em ordem."""
    vistos = []
    for m in _RX_CPF_TEXTO.finditer(str(texto or "")):
        n = re.sub(r"\D", "", m.group(0))
        if cpf_util.valido(n) and n not in vistos:
            vistos.append(n)
    return vistos


def fato_cpf(cliente: dict, trecho_ocr) -> str:
    """'sim' (há CPF válido igual ao do cliente e nenhum outro), 'nao' (há CPF válido
    e nenhum é do cliente), 'ambiguo' (o do cliente e outro), 'ausente'."""
    achados = cpfs_validos_no_texto(trecho_ocr)
    cpf_cli = cpf_util.normalizar((cliente or {}).get("cpf"))
    if not achados:
        return "ausente"
    if cpf_cli and cpf_cli in achados:
        return "sim" if len(achados) == 1 else "ambiguo"
    return "nao"


# ---------------------------------------------------------------------------
# (a) o documento é do cliente?
# ---------------------------------------------------------------------------
OPCOES_DONO = {
    "mesmo_cliente": "O documento pertence à pessoa em `cliente.nome` (nome do titular coincide, "
                     "considerando abreviações e erros de leitura do OCR).",
    "outro_cliente": "O documento pertence claramente a OUTRA pessoa (outro nome de titular).",
    "indefinido": "O texto não permite dizer de quem é o documento (sem nome legível do titular).",
}
NIVEIS_RISCO = [
    "Sem risco: titular identificado e coerente com o cliente.",
    "Risco baixo: pequenas divergências de grafia ou leitura.",
    "Risco médio: elementos de outra pessoa ou dados contraditórios.",
    "Risco alto: indícios fortes de documento de terceiro misturado ao kit.",
]


def regra_documento(cliente: dict, trecho_ocr) -> dict:
    """Comportamento ATUAL (determinístico)."""
    f = fato_cpf(cliente, trecho_ocr)
    if f == "sim":
        return _resultado(ACAO_KIT, "CPF válido do documento igual ao do cliente", "regra", fato=f)
    if f == "nao":
        return _resultado(ACAO_QUARENTENA, "CPF válido do documento é de outra pessoa", "regra", fato=f)
    if f == "ambiguo":
        return _resultado(ACAO_REVISAO, "documento traz o CPF do cliente e outro CPF válido",
                          "regra", fato=f)
    return _resultado(ACAO_QUARENTENA, "documento sem CPF válido: dono desconhecido", "regra", fato=f)


def montar_documento(cliente: dict, trecho_ocr) -> tuple[dict, dict]:
    f = fato_cpf(cliente, trecho_ocr)
    state = {
        "cliente": {"nome": str((cliente or {}).get("nome") or "")},
        "documento": {"texto_ocr": _trecho_seguro(trecho_ocr)},
        "fatos": {"cpf_do_documento_igual_ao_do_cliente": f},
    }
    questions = {
        "dono_documento": {
            "type": "choice",
            "instructions": ("Este documento de um kit previdenciário pertence ao cliente? Compare o "
                             "titular em `documento.texto_ocr` com `cliente.nome`. CPFs vêm mascarados; "
                             "`fatos.cpf_do_documento_igual_ao_do_cliente` já diz se o CPF confere."),
            "criteria": OPCOES_DONO,
        },
        "risco_documento_terceiro": {
            "type": "score",
            "instructions": ("Qual o risco de `documento.texto_ocr` ser de outra pessoa que não "
                             "`cliente.nome`, inserido por engano no kit do cliente?"),
            "criteria": NIVEIS_RISCO,
        },
    }
    return state, questions


def rotear_documento(answers: dict, regra: dict) -> dict:
    dono = answers.get("dono_documento") or {}
    risco = answers.get("risco_documento_terceiro") or {}
    escolha, conf = dono.get("choice"), dono.get("confidence")
    conf = _conf(conf)
    score, score_invalido = _score(risco.get("score"), len(NIVEIS_RISCO))
    # score inválido (NaN, bool, fora de 0..3) conta como risco alto: nunca afrouxa
    risco_alto = score_invalido or (score is not None and score >= RISCO_MAXIMO_ACEITAVEL)
    extra = {"jev_escolha": escolha, "jev_risco": score}
    fato = regra.get("fato")
    if fato in ("sim", "nao", "ambiguo"):
        # a regra determinística decide; o JEV só confirma/registra
        esperado = {"sim": "mesmo_cliente", "nao": "outro_cliente"}.get(fato)
        concorda = escolha == esperado and conf >= CONFIANCA_MINIMA_PARA_AGIR
        return _resultado(regra["acao"], regra["motivo"] + ("; jev confirma" if concorda
                                                          else "; jev diverge ou incerto (registrado)"),
                          "regra", conf, divergencia_jev=not concorda, **extra)
    # dono desconhecido (sem CPF válido): JEV nunca promove ao kit
    if conf < CONFIANCA_MINIMA_PARA_AGIR:
        return _resultado(ACAO_REVISAO, f"confiança {conf:.2f} < {CONFIANCA_MINIMA_PARA_AGIR}",
                          "jev", conf, **extra)
    if escolha == "outro_cliente":
        return _resultado(ACAO_QUARENTENA, "jev: documento de outra pessoa", "jev", conf, **extra)
    if escolha == "mesmo_cliente" and not risco_alto:
        return _resultado(ACAO_REVISAO, "jev sugere mesmo cliente, mas sem CPF válido a promoção ao "
                          "kit exige humano", "jev", conf, **extra)
    return _resultado(ACAO_REVISAO, f"jev: {escolha} / risco {score}", "jev", conf, **extra)


def documento_e_do_cliente(cliente: dict, trecho_ocr, *, config: dict | None = None,
                           _perguntar=None) -> dict:
    regra = regra_documento(cliente, trecho_ocr)
    state, questions = montar_documento(cliente, trecho_ocr)
    return _decidir("documento_e_do_cliente", regra, state, questions, rotear_documento,
                    config=config, _perguntar=_perguntar)


# ---------------------------------------------------------------------------
# (b) foto/arquivo sem CPF: vale olhar?
# ---------------------------------------------------------------------------
OPCOES_FOTO = {
    "provavelmente_do_cliente": "O conteúdo indica documento ou foto do próprio `cliente.nome` "
                                "(nome do titular legível e coincidente).",
    "provavelmente_de_outra_pessoa": "O conteúdo indica outra pessoa como titular.",
    "sem_elementos": "Não há texto suficiente para relacionar o arquivo a alguém.",
}


def regra_foto(cliente: dict, trecho_ocr, tipo_arquivo) -> dict:
    return _resultado(ACAO_QUARENTENA, "arquivo sem CPF válido fica em quarentena (padrão)", "regra")


def montar_foto(cliente: dict, trecho_ocr, tipo_arquivo) -> tuple[dict, dict]:
    state = {
        "cliente": {"nome": str((cliente or {}).get("nome") or "")},
        "arquivo": {"tipo": str(tipo_arquivo or "desconhecido")[:40],
                    "texto_ocr": _trecho_seguro(trecho_ocr)},
    }
    questions = {
        "foto_relacionada": {
            "type": "choice",
            "instructions": ("Um arquivo SEM CPF chegou no kit de `cliente.nome`. Pelo texto em "
                             "`arquivo.texto_ocr` e pelo tipo `arquivo.tipo`, de quem ele parece ser?"),
            "criteria": OPCOES_FOTO,
        },
    }
    return state, questions


def rotear_foto(answers: dict, regra: dict) -> dict:
    """Saídas só quarentena | revisao_humana; nunca aceitar automático nesta fase.
    Confiança baixa mantém o padrão (quarentena): o JEV não acrescentou informação."""
    r = answers.get("foto_relacionada") or {}
    escolha, conf = r.get("choice"), r.get("confidence")
    conf = _conf(conf)
    if conf >= CONFIANCA_MINIMA_PARA_AGIR and escolha == "provavelmente_do_cliente":
        return _resultado(ACAO_REVISAO, "jev: provavelmente do cliente; humano decide se entra",
                          "jev", conf, jev_escolha=escolha)
    return _resultado(ACAO_QUARENTENA, f"jev: {escolha} (confiança {conf:.2f}); mantém quarentena",
                      "jev", conf, jev_escolha=escolha)


def foto_sem_cpf_vale(cliente: dict, trecho_ocr, tipo_arquivo, *, config: dict | None = None,
                      _perguntar=None) -> dict:
    regra = regra_foto(cliente, trecho_ocr, tipo_arquivo)
    state, questions = montar_foto(cliente, trecho_ocr, tipo_arquivo)
    return _decidir("foto_sem_cpf_vale", regra, state, questions, rotear_foto,
                    config=config, _perguntar=_perguntar)


# ---------------------------------------------------------------------------
# (c) lote misturado: o que fazer?
# ---------------------------------------------------------------------------
OPCOES_LOTE = {
    "separar": "Os documentos têm donos claramente identificáveis e podem ser divididos por cliente.",
    "pedir_reenvio": "O material está incompleto ou ilegível; o certo é pedir que reenviem separado.",
    "revisao_humana": "A mistura é confusa demais para decidir sem uma pessoa olhar.",
}
CAMPOS_RESUMO_LOTE = ("n_arquivos", "n_documentos", "n_cpfs_validos_distintos", "n_kits_assinados",
                      "n_documentos_sem_dono", "n_ilegiveis", "tipos", "observacao")


def regra_lote(resumo_lote: dict) -> dict:
    return _resultado(ACAO_REVISAO, "lote misturado vai para revisão humana (comportamento atual)",
                      "regra")


def montar_lote(resumo_lote: dict) -> tuple[dict, dict]:
    """Só FATOS agregados (contagens/tipos). Campos fora da lista são descartados."""
    resumo = {k: (resumo_lote or {}).get(k) for k in CAMPOS_RESUMO_LOTE if k in (resumo_lote or {})}
    if "observacao" in resumo:
        resumo["observacao"] = _trecho_seguro(resumo["observacao"])[:300]
    state = {"lote": resumo}
    questions = {
        "acao_lote_misto": {
            "type": "choice",
            "instructions": ("Um lote de documentos previdenciários mistura mais de um cliente. Pelo "
                             "resumo em `lote`, qual a melhor ação?"),
            "criteria": OPCOES_LOTE,
        },
    }
    return state, questions


def rotear_lote(answers: dict, regra: dict, resumo_lote: dict | None = None) -> dict:
    r = answers.get("acao_lote_misto") or {}
    escolha, conf = r.get("choice"), r.get("confidence")
    conf = _conf(conf)
    if conf < CONFIANCA_MINIMA_PARA_AGIR:
        return _resultado(ACAO_REVISAO, f"confiança {conf:.2f} < {CONFIANCA_MINIMA_PARA_AGIR}",
                          "jev", conf, jev_escolha=escolha)
    sem_dono = (resumo_lote or {}).get("n_documentos_sem_dono")
    if escolha == "separar" and sem_dono != 0:
        # separar automaticamente com documento sem dono = promover dono desconhecido
        return _resultado(ACAO_REVISAO, "jev sugere separar, mas há documento sem dono "
                          f"(n_documentos_sem_dono={sem_dono!r})", "jev", conf, jev_escolha=escolha)
    acao = {"separar": ACAO_SEPARAR, "pedir_reenvio": ACAO_REENVIO}.get(escolha, ACAO_REVISAO)
    return _resultado(acao, f"jev: {escolha}", "jev", conf, jev_escolha=escolha)


def lote_misturado_acao(resumo_lote: dict, *, config: dict | None = None, _perguntar=None) -> dict:
    regra = regra_lote(resumo_lote)
    state, questions = montar_lote(resumo_lote)
    return _decidir("lote_misturado_acao", regra, state, questions,
                    lambda a, rg: rotear_lote(a, rg, resumo_lote),
                    config=config, _perguntar=_perguntar)


# ---------------------------------------------------------------------------
# Orquestração comum
# ---------------------------------------------------------------------------
def caminho_jsonl(cfg: dict) -> str:
    return str(cfg.get("sombra_jsonl") or caminhos.em_base("jev-decisoes.jsonl"))


def _sem_nao_finitos(v):
    """Cópia com NaN/inf trocados por None (recursivo em dict/list/tuple)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _sem_nao_finitos(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sem_nao_finitos(x) for x in v]
    return v


def registrar_jsonl(cfg: dict, registro: dict) -> None:
    """Uma linha JSON por decisão (0600, append). Passa por seguranca.mascarar:
    CPF cru nunca chega ao disco. Falha de gravação só avisa."""
    caminho = caminho_jsonl(cfg)
    # R3/A7: JSON estrito (jq/JS) — valor não finito vira null; allow_nan=False garante
    linha = seguranca.mascarar(json.dumps(_sem_nao_finitos(registro), ensure_ascii=False,
                                          sort_keys=True, allow_nan=False, default=str))
    try:
        d = os.path.dirname(caminho)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
                     0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(linha + "\n")
    except OSError as e:
        log.warning("jev: não consegui gravar %s (%s)", caminho, type(e).__name__)


def _decidir(nome: str, regra: dict, state, questions, roteador, *, config, _perguntar) -> dict:
    """Nunca levanta. desligado -> regra; falha -> regra (falha_jev); sombra -> regra + JSONL;
    decide -> roteador(answers, regra) + JSONL."""
    try:
        modo, cfg = _modo(config)
    except Exception:  # config quebrado nunca muda o comportamento
        return regra
    if modo == "desligado":
        return regra
    perguntar = _perguntar or typesafe_cliente.perguntar
    base = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "decisao": nome, "modo": modo,
            "questoes": sorted(questions), "regra": {k: regra.get(k) for k in ("acao", "motivo", "fato")
                                                      if k in regra}}
    try:
        resp = perguntar(state, questions, config=cfg)
        jev = roteador(resp.get("answers") or {}, regra)
    except Exception as e:
        falha = dict(regra, fonte="falha_jev",
                     motivo=regra["motivo"] + f"; jev indisponível ({getattr(e, 'classe', type(e).__name__)})")
        log.warning("jev %s falhou (%s); mantida a regra", nome, seguranca.mascarar(str(e))[:200])
        registrar_jsonl(cfg, dict(base, erro=getattr(e, "classe", type(e).__name__), devolvido="regra"))
        return falha
    registrar_jsonl(cfg, dict(base, jev={k: jev.get(k) for k in jev}, latencia_ms=resp.get("latencia_ms"),
                              usage=resp.get("usage"), devolvido="regra" if modo == "sombra" else "jev",
                              concorda=jev.get("acao") == regra.get("acao")))
    return regra if modo == "sombra" else jev
