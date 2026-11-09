#!/usr/bin/env python3
"""
estado_lote.py — MODELO DE ESTADOS de um lote (funções puras, sem E/S, sem rede).

Por que existe: a decisão "este lote pode ir para a fila?" estava espalhada em
`pronto`, `requer_revisao`, `status`, `identificacao` e na contagem de arquivos em
disco, cada script lendo um pedaço (auditoria F1-03, F1-10, A3). Aqui fica a
regra única, que NÃO renomeia nada do que já está gravado:

  valores EXTERNOS preservados (como estão no estado.json / fila-enviados.json):
    status        ABERTO, FECHADO_PRONTO, FECHADO_POR_TIMEOUT, FECHADO_POR_JANELA,
                  FECHADO_POR_INATIVIDADE, FECHADO_POR_TROCA_DE_PESSOA, CONFLITO,
                  descartado_duplicata, revisao_duplicata
    identificacao OK, SEM_CPF, SEM_ARQUIVOS, CONFLITO

  `estado_logico()` só LÊ esses valores e devolve um dos ESTADOS_LOGICOS.

CPF: usa `cpf_util.valido` (frente S). Enquanto `cpf_util.py` não existir, cai num
fallback local mínimo (11 dígitos + DV, rejeita repetidos) — ver `_cpf_valido`.
"""
from __future__ import annotations

import copy

import cpf_util

ESTADOS_LOGICOS = ("ABERTO", "FECHADO", "AGUARDANDO_DOWNLOAD", "AGUARDANDO_IDENTIFICACAO",
                   "REVISAO", "PRONTO", "ENFILEIRANDO", "ENFILEIRADO", "DESCARTADO_DUPLICATA",
                   "DIVIDIDO", "IGNORADO", "ERRO_PERMANENTE")

STATUS_EXTERNOS = ("ABERTO", "FECHADO_PRONTO", "FECHADO_POR_TIMEOUT", "FECHADO_POR_JANELA",
                   "FECHADO_POR_INATIVIDADE", "FECHADO_POR_TROCA_DE_PESSOA", "CONFLITO")
IDENTIFICACOES_EXTERNAS = ("OK", "SEM_CPF", "SEM_ARQUIVOS", "CONFLITO")

SUFIXOS_PARCIAIS = (".part", ".parcial", ".tmp")

# grafo de transições permitidas entre estados lógicos (o mesmo estado é sempre válido)
_TRANSICOES = {
    "ABERTO": {"FECHADO", "AGUARDANDO_DOWNLOAD", "AGUARDANDO_IDENTIFICACAO", "REVISAO",
               "PRONTO", "IGNORADO", "DIVIDIDO", "ERRO_PERMANENTE"},
    "FECHADO": {"AGUARDANDO_DOWNLOAD", "AGUARDANDO_IDENTIFICACAO", "REVISAO", "PRONTO",
                "IGNORADO", "DIVIDIDO", "ERRO_PERMANENTE"},
    "AGUARDANDO_DOWNLOAD": {"FECHADO", "AGUARDANDO_IDENTIFICACAO", "REVISAO", "PRONTO",
                            "IGNORADO", "DIVIDIDO", "ERRO_PERMANENTE"},
    "AGUARDANDO_IDENTIFICACAO": {"FECHADO", "AGUARDANDO_DOWNLOAD", "REVISAO", "PRONTO",
                                 "IGNORADO", "DIVIDIDO", "ERRO_PERMANENTE"},
    "REVISAO": {"FECHADO", "AGUARDANDO_DOWNLOAD", "AGUARDANDO_IDENTIFICACAO", "PRONTO",
                "DESCARTADO_DUPLICATA", "IGNORADO", "DIVIDIDO", "ERRO_PERMANENTE"},
    "PRONTO": {"ENFILEIRANDO", "REVISAO", "AGUARDANDO_DOWNLOAD", "DESCARTADO_DUPLICATA",
               "IGNORADO", "DIVIDIDO"},
    "ENFILEIRANDO": {"ENFILEIRADO", "PRONTO", "REVISAO", "ERRO_PERMANENTE"},
    "ENFILEIRADO": {"REVISAO"},
    "DESCARTADO_DUPLICATA": {"REVISAO"},
    "IGNORADO": {"REVISAO"},
    "ERRO_PERMANENTE": {"REVISAO"},
    "DIVIDIDO": set(),
}


# --------------------------------------------------------------------------
# CPF — fonte única: cpf_util (frente S)
# --------------------------------------------------------------------------
def _cpf_valido(valor) -> bool:
    return bool(cpf_util.valido(valor))


# --------------------------------------------------------------------------
# leitura do lote
# --------------------------------------------------------------------------
def _txt(v) -> str:
    return str(v or "").strip()


def _status(lote: dict) -> str:
    return _txt(lote.get("status"))


def _flag(lote: dict, *nomes) -> bool:
    return any(bool(lote.get(n)) for n in nomes)


def _ignorado(lote: dict) -> bool:
    return _flag(lote, "ignorado") or _status(lote).upper() == "IGNORADO"


def _dividido(lote: dict) -> bool:
    return (_flag(lote, "dividido", "dividido_em", "sub_lotes", "sublotes")
            or _status(lote).upper() == "DIVIDIDO")


def _erro_permanente(lote: dict) -> bool:
    return _flag(lote, "erro_permanente") or _status(lote).upper() == "ERRO_PERMANENTE"


def _fechado(lote: dict) -> bool:
    st = _status(lote).upper()
    if st == "ABERTO" or lote.get("aberto") is True:
        return False
    if st.startswith("FECHADO") or st == "CONFLITO":
        return True
    anterior = _txt(lote.get("status_anterior")).upper()
    if st in ("DESCARTADO_DUPLICATA", "REVISAO_DUPLICATA") and anterior.startswith("FECHADO"):
        return True
    return bool(lote.get("fechado"))


def _conflito(lote: dict) -> bool:
    return ("CONFLITO" in _status(lote).upper()
            or "CONFLITO" in _txt(lote.get("identificacao")).upper())


def _nome_arquivo(meta: dict) -> str:
    return _txt(meta.get("caminho") or meta.get("nome_salvo")).lower()


def _problemas_arquivos(lote: dict) -> list:
    problemas = []
    arquivos = [a for a in (lote.get("arquivos") or []) if isinstance(a, dict)]
    if not arquivos:
        return ["sem arquivos no lote"]
    fids = [str(f) for f in (lote.get("file_ids") or [])]
    if fids:
        com_meta = {str(a.get("file_id")) for a in arquivos if a.get("file_id") not in (None, "")}
        faltam = [f for f in fids if f not in com_meta]
        if faltam:
            problemas.append(f"{len(faltam)} file_id(s) sem registro de arquivo")
    for a in arquivos:
        rotulo = f"arquivo {a.get('indice') or a.get('file_id') or '?'}"
        if a.get("baixado") is not True:
            problemas.append(f"{rotulo}: não baixado")
            continue
        if not a.get("sha256"):
            problemas.append(f"{rotulo}: sem sha256")
        try:
            tamanho = int(a.get("tamanho_bits") or a.get("tamanho") or 0)
        except (TypeError, ValueError):
            tamanho = 0
        if tamanho <= 0:
            problemas.append(f"{rotulo}: tamanho 0 ou desconhecido")
        if _nome_arquivo(a).endswith(SUFIXOS_PARCIAIS):
            problemas.append(f"{rotulo}: arquivo parcial (.part/.parcial/.tmp)")
    return problemas


def _problemas_identificacao(lote: dict) -> list:
    problemas = []
    ident = _txt(lote.get("identificacao")).upper()
    if ident != "OK":
        problemas.append(f"identificação não está OK ({lote.get('identificacao')!r})")
    if not lote.get("cpf"):
        problemas.append("sem CPF")
    elif not _cpf_valido(lote.get("cpf")):
        problemas.append("CPF inválido (dígito verificador)")
    return problemas


def motivos_impeditivos(lote: dict) -> list:
    """Todos os motivos pelos quais o lote NÃO pode ir à fila (vazio = elegível)."""
    if not isinstance(lote, dict) or not lote:
        return ["lote inválido ou vazio"]
    motivos = []
    if _ignorado(lote):
        motivos.append("lote ignorado")
    if _dividido(lote):
        motivos.append("lote dividido (os sub-lotes é que seguem)")
    if _erro_permanente(lote):
        motivos.append("erro permanente")
    st = _status(lote)
    if st == "descartado_duplicata":
        motivos.append("descartado como duplicata")
    elif st == "revisao_duplicata":
        motivos.append("possível duplicata em revisão")
    if not _fechado(lote):
        motivos.append("lote não está fechado")
    if _conflito(lote):
        motivos.append("conflito de identificação")
    if lote.get("requer_revisao"):
        extra = "; ".join(str(m) for m in (lote.get("motivos_revisao") or [])[:3])
        motivos.append("requer revisão" + (f": {extra}" if extra else ""))
    motivos.extend(_problemas_identificacao(lote))
    motivos.extend(_problemas_arquivos(lote))
    return motivos


def pode_enfileirar(lote: dict) -> tuple:
    """(True, []) quando elegível; senão (False, motivos)."""
    motivos = motivos_impeditivos(lote)
    return (not motivos, motivos)


def estado_logico(lote: dict, controle: dict | None = None) -> str:
    """Estado lógico do lote. `controle` = registro dele em fila-enviados.json
    (`{"job_id", "upload_id", "em", "enviados"}`) ou None."""
    if not isinstance(lote, dict) or not lote:
        return "ERRO_PERMANENTE"
    if _dividido(lote):
        return "DIVIDIDO"
    if _ignorado(lote):
        return "IGNORADO"
    if _erro_permanente(lote):
        return "ERRO_PERMANENTE"
    if _status(lote) == "descartado_duplicata":
        return "DESCARTADO_DUPLICATA"
    if isinstance(controle, dict) and controle:
        if controle.get("job_id") or controle.get("upload_id"):
            return "ENFILEIRADO"
        return "ENFILEIRANDO"
    if not _fechado(lote):
        return "ABERTO"
    if (lote.get("requer_revisao") or _conflito(lote) or _status(lote) == "revisao_duplicata"
            or _txt(lote.get("identificacao")).upper() == "SEM_ARQUIVOS"):
        return "REVISAO"
    if _problemas_arquivos(lote):
        return "AGUARDANDO_DOWNLOAD"
    if _problemas_identificacao(lote):
        return "AGUARDANDO_IDENTIFICACAO"
    if pode_enfileirar(lote)[0]:
        return "PRONTO"
    return "FECHADO"


def transicao_valida(de: str, para: str) -> bool:
    if de not in _TRANSICOES or para not in _TRANSICOES:
        return False
    return de == para or para in _TRANSICOES[de]


def marcar_revisao(lote: dict, motivo: str) -> dict:
    """Cópia do lote com `motivo` acrescentado (sem apagar os existentes),
    requer_revisao=True e pronto=False. Gravar com
    `estado_store.atualizar_lotes({id: {campos}}, dono=...)`: motivo de quem não é a
    triagem é fixo e só sai por `resolver_revisao`/`_resolver_motivos`."""
    novo = copy.deepcopy(lote) if isinstance(lote, dict) else {}
    motivos = [str(m) for m in (novo.get("motivos_revisao") or [])]
    motivo = str(motivo or "").strip()
    if motivo and motivo not in motivos:
        motivos.append(motivo)
    novo["motivos_revisao"] = motivos
    novo["requer_revisao"] = True
    novo["pronto"] = False
    return novo


def resolver_revisao(lote: dict, motivo: str, por: str = "revisao") -> dict:
    """Cópia do lote sem `motivo` (texto exato ou 'prefixo*'), registrado em
    `motivos_resolvidos`; requer_revisao só cai quando não sobra motivo.
    (Acréscimo ao contrato — par explícito de `marcar_revisao`.)"""
    novo = copy.deepcopy(lote) if isinstance(lote, dict) else {}
    padrao = str(motivo or "")

    def casa(m):
        return m.startswith(padrao[:-1]) if padrao.endswith("*") else m == padrao

    motivos = [str(m) for m in (novo.get("motivos_revisao") or [])]
    removidos = [m for m in motivos if casa(m)]
    novo["motivos_revisao"] = [m for m in motivos if not casa(m)]
    novo.setdefault("motivos_resolvidos", []).append(
        {"motivo": padrao, "por": por, "removidos": removidos})
    novo["requer_revisao"] = bool(novo["motivos_revisao"])
    return novo
