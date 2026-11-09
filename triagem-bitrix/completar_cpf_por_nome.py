#!/usr/bin/env python3
"""Completa o CPF de lotes SEM_CPF a partir do contato do Bitrix — com DUAS evidências.

Caso Samyra (2026-10-27): a ficha veio sem CPF (só nome) e o lote ficou SEM_CPF. O
contato já existe no CRM com o CPF. Este script procura o contato pelo nome e, só
quando há prova independente, grava o CPF.

Regras (auditoria ID-01..04, S11, F1-04, F1-12, A1):
  * Busca PAGINADA (`bitrix_cliente.listar`, todas as páginas) e comparação pelo nome
    COMPLETO normalizado (sem acento, sem partículas da/de/do/dos/das/e) — não pelo 1º nome.
  * Confirmar exige 2ª evidência: um telefone do lote (ficha/autor) que o
    `crm.duplicate.findbycomm` (type=PHONE, entity_type=CONTACT, até 20 valores) aponte
    para o MESMO contato, e CPF do contato válido (`cpf_util.valido`).
  * Sem 2ª evidência ou ambíguo -> grava só `cpf_sugerido` (+ `cpf_sugerido_motivo`
    quando o estado_store declarar o campo) e marca revisão (`estado_lote.marcar_revisao`).
  * NUNCA grava `pronto=True` nem limpa `motivos_revisao`/`requer_revisao`. Na confirmação
    resolve só os motivos `SEM_CPF:*` e os próprios `cpf_por_nome:*`; revisão de duplicata,
    conflito e qualquer outro motivo continuam. Quem decide a fila é `estado_lote.pode_enfileirar`.
  * Lotes descartados/divididos/ignorados/em conflito não são tocados.
  * Grava só via `estado_store.atualizar_lotes(..., dono="identificacao")`; HTTP fora da trava.
  * Cache com TTL (positivo e negativo) em `caminhos.em_base("cache-cpf-nome.json")`.
    Telefones: uma consulta findbycomm por telefone; cache de versão < 2 perde `fones`.
  * Backup só por `estado_store.fazer_backup` e só quando há mudança real.

Uso:
    python3 completar_cpf_por_nome.py              # simulação (padrão)
    python3 completar_cpf_por_nome.py --simular
    python3 completar_cpf_por_nome.py --aplicar [--limite 30]

Saída: 0 ok; 2 rodada interrompida por falha/limite/disjuntor do Bitrix (o que já foi
apurado é aplicado); 1 erro (estado corrompido, webhook ausente).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

import bitrix_cliente
import caminhos
import cpf_util
import estado_lote
import estado_store
import seguranca

log = logging.getLogger("completar_cpf")

CAMPO_CPF = "UF_CRM_1524762221558"
ORIGEM_CONFIRMADA = "bitrix-nome+telefone"
PREFIXO_MOTIVO = "cpf_por_nome:"
MOTIVO_SUGERIDO = (PREFIXO_MOTIVO + " homônimo único no CRM sem 2ª evidência (telefone) — "
                   "conferir o CPF sugerido")
MOTIVO_AMBIGUO = PREFIXO_MOTIVO + " mais de um contato homônimo no CRM — ambíguo"
MOTIVO_CPF_INVALIDO = PREFIXO_MOTIVO + " contato homônimo com CPF inválido no CRM"
MOTIVO_TELEFONE_OUTRO = (PREFIXO_MOTIVO + " telefone do lote aponta para outro contato do CRM — "
                         "conferir")

TTL_POSITIVO_S = 24 * 3600
TTL_NEGATIVO_S = 12 * 3600
LIMITE_CONTATOS = 2000          # busca que chega nisso não prova unicidade -> ambíguo
MAX_VALORES_FINDBYCOMM = 20     # doc: api-reference/crm/duplicates/crm-duplicate-find-by-comm.md
MAX_TELEFONES_CONSULTA = 10     # telefones consultados por lote (1 chamada cada)
# Versão do cache. v1 gravava em `fones` o conjunto de uma consulta em lote para cada
# telefone (contaminado, achado A1): cache de versão < 2 tem `fones` descartado ao abrir.
VERSAO_CACHE = 2

PARTICULAS = frozenset({"DA", "DE", "DO", "DOS", "DAS", "E", "D"})
ESTADOS_IGNORAR = frozenset({"DIVIDIDO", "IGNORADO", "DESCARTADO_DUPLICATA", "ERRO_PERMANENTE"})


class RodadaInterrompida(Exception):
    """Bitrix pediu para parar (limite/disjuntor/erro): aplica o que já foi apurado e sai com 2."""


# --------------------------------------------------------------------------
# nomes e telefones
# --------------------------------------------------------------------------
def _sem_acento(s) -> str:
    from nome_regras import sem_acento
    return sem_acento(s)


def chave_nome(nome) -> str:
    """Nome completo normalizado: maiúsculas, sem acento, só letras, sem partículas."""
    s = re.sub(r"[^A-Z]+", " ", _sem_acento(nome).upper())
    return " ".join(t for t in s.split() if t not in PARTICULAS)


def nome_do_contato(reg: dict) -> str:
    partes = [reg.get("NAME"), reg.get("SECOND_NAME"), reg.get("LAST_NAME")]
    return " ".join(str(p).strip() for p in partes if p and str(p).strip())


def _nome_valido(n) -> str:
    from nome_regras import normalizar_nome_candidato, validar_nome
    c = normalizar_nome_candidato(n)
    return c if validar_nome(c, exigir_completo=True) else ""


def _ficha(lote: dict) -> dict:
    """Campos da ficha pelo parser da ponte (import tardio: módulo de outra frente)."""
    try:
        import lote_para_job as lj
        texto, _ = lj.achar_ficha_em_texto(lote)
        return lj.parse_ficha(texto) if texto else {}
    except Exception as e:  # noqa: BLE001 — parser alheio não pode derrubar a rodada
        log.debug("parser da ficha falhou: %s", type(e).__name__)
        return {}


def nome_do_lote(lote: dict, ficha: dict | None = None) -> str:
    ficha = _ficha(lote) if ficha is None else ficha
    for cand in (ficha.get("nome"), lote.get("nome")):
        n = _nome_valido(cand) if cand else ""
        if n:
            return n
    return ""


def normalizar_telefone(valor) -> str:
    d = re.sub(r"\D", "", str(valor or ""))
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    return d if 10 <= len(d) <= 11 else ""


_RX_FONE = re.compile(r"\(?\b\d{2}\)?\s*9?\s*\d{4}[-.\s]?\d{4}\b")


def telefones_do_lote(lote: dict, ficha: dict | None = None) -> list:
    """Telefones do lote: ficha (parser), campos `telefone(s)` e autores com telefone."""
    ficha = _ficha(lote) if ficha is None else ficha
    brutos = list(ficha.get("telefones") or [])
    if ficha.get("fone"):
        brutos.append(ficha["fone"])
    for campo in ("telefones", "telefone", "fones"):
        v = lote.get(campo)
        brutos.extend(v if isinstance(v, list) else [v] if v else [])
    for autor in lote.get("autores") or []:
        if isinstance(autor, dict):
            brutos.extend(autor.get(k) for k in ("telefone", "fone", "phone") if autor.get(k))
    if not brutos:                       # parser indisponível: telefones soltos nas mensagens
        for m in lote.get("mensagens") or []:
            brutos.extend(_RX_FONE.findall(str((m or {}).get("texto") or "")))
    saida = []
    for b in brutos:
        n = normalizar_telefone(b)
        if n and n not in saida:
            saida.append(n)
    return saida


# --------------------------------------------------------------------------
# cache com TTL (inclusive negativo), gravação atômica 0600
# --------------------------------------------------------------------------
def caminho_cache() -> str:
    return caminhos.em_base("cache-cpf-nome.json")


class Cache:
    def __init__(self, caminho: str | None = None, ttl_pos: float = TTL_POSITIVO_S,
                 ttl_neg: float = TTL_NEGATIVO_S, agora=None):
        self.caminho = caminho or caminho_cache()
        self.ttl_pos, self.ttl_neg = ttl_pos, ttl_neg
        self._agora = agora or time.time
        self.mudou = False
        try:
            dados = estado_store.ler_estado(self.caminho)
        except estado_store.EstadoCorrompido:
            log.warning("cache corrompido — ignorado (cópia preservada pelo estado_store)")
            dados = {}
        try:
            versao = int(dados.get("versao") or 0)
        except (TypeError, ValueError):
            versao = 0
        fones = dict(dados.get("fones") or {}) if versao >= VERSAO_CACHE else {}
        if versao < VERSAO_CACHE and dados.get("fones"):
            log.warning("cache v%s: %d telefone(s) descartado(s) (consulta em lote antiga)",
                        versao, len(dados.get("fones") or {}))
            self.mudou = True                            # regrava já na versão nova
        self.dados = {"versao": VERSAO_CACHE, "nomes": dict(dados.get("nomes") or {}),
                      "fones": fones}

    def _idade(self, reg) -> float:
        try:
            return self._agora() - float(reg.get("em", 0))
        except (TypeError, ValueError, AttributeError):
            return float("inf")

    def obter(self, tipo: str, chave: str, campo: str):
        reg = self.dados[tipo].get(chave)
        if not isinstance(reg, dict) or campo not in reg:
            return None
        valor = reg[campo]
        vazio = not valor or (isinstance(valor, dict) and not valor.get("candidatos"))
        return valor if self._idade(reg) < (self.ttl_neg if vazio else self.ttl_pos) else None

    def guardar(self, tipo: str, chave: str, campo: str, valor) -> None:
        self.dados[tipo][chave] = {"em": self._agora(), campo: valor}
        self.mudou = True

    def gravar(self) -> None:
        if not self.mudou:
            return
        teto = max(self.ttl_pos, self.ttl_neg)
        for tipo in ("nomes", "fones"):                  # poda o que já expirou
            for k in [k for k, r in self.dados[tipo].items()
                      if not isinstance(r, dict) or self._idade(r) > teto]:
                del self.dados[tipo][k]
        estado_store.gravar_atomico(self.dados, self.caminho)
        self.mudou = False


# --------------------------------------------------------------------------
# Bitrix (sempre fora da trava do estado)
# --------------------------------------------------------------------------
def _bitrix(fn, *a, **k):
    try:
        return fn(*a, **k)
    except bitrix_cliente.CircuitOpenError as e:
        raise RodadaInterrompida(f"disjuntor aberto: {e}") from e
    except bitrix_cliente.FalhaBitrix as e:
        classe = getattr(getattr(getattr(e, "erro", None), "classe", None), "value", "?")
        raise RodadaInterrompida(f"falha Bitrix ({classe})") from e


def buscar_candidatos(nome: str, cache: Cache, _transporte=None) -> dict:
    """Contatos do CRM cujo nome COMPLETO normalizado é igual ao do lote.

    Duas consultas paginadas (todas as páginas, `>ID` + `start=-1`): `%NAME`=1º nome +
    `%LAST_NAME`=último sobrenome, e `%NAME`=nome inteiro (contato com tudo no NAME).
    Devolve {"candidatos": [{"id", "cpf"}], "truncado": bool}.
    """
    chave = chave_nome(nome)
    em_cache = cache.obter("nomes", chave, "res")
    if em_cache is not None:
        return em_cache
    tokens = chave.split()
    select = ["ID", "NAME", "SECOND_NAME", "LAST_NAME", CAMPO_CPF]
    filtros = [{"%NAME": tokens[0], "%LAST_NAME": tokens[-1]},
               {"%NAME": " ".join(str(nome).split())}]
    vistos, candidatos, truncado = set(), [], False
    for filtro in filtros:
        regs = _bitrix(bitrix_cliente.listar, "crm.contact.list",
                       {"filter": filtro, "select": select},
                       limite=LIMITE_CONTATOS, _transporte=_transporte)
        if len(regs) >= LIMITE_CONTATOS:
            truncado = True
        for reg in regs:
            cid = str(reg.get("ID") or "")
            if not cid or cid in vistos:
                continue
            vistos.add(cid)
            if chave_nome(nome_do_contato(reg)) == chave:
                candidatos.append({"id": cid, "cpf": cpf_util.normalizar(reg.get(CAMPO_CPF)) or ""})
    res = {"candidatos": candidatos, "truncado": truncado}
    cache.guardar("nomes", chave, "res", res)
    return res


def contatos_por_telefone(telefones: list, cache: Cache, _transporte=None) -> set:
    """IDs de contato que o `crm.duplicate.findbycomm` associa aos telefones.

    UMA chamada por telefone (valores `t` e `+55t`): a resposta do findbycomm não diz
    qual valor casou com qual contato, então consultar vários telefones juntos e
    cachear o conjunto para cada um faria o telefone de B "pertencer" a A (achado A1
    da revisão final — um homônimo virava `confirmar` sem prova). Assim o cache de
    cada telefone guarda só os contatos daquele telefone.
    """
    ids, faltam = set(), []
    for t in telefones:
        c = cache.obter("fones", t, "contatos")
        if c is None:
            faltam.append(t)
        else:
            ids.update(str(x) for x in c)
    for t in faltam[:MAX_TELEFONES_CONSULTA]:
        r = _bitrix(bitrix_cliente.chamar, "crm.duplicate.findbycomm",
                    {"entity_type": "CONTACT", "type": "PHONE",
                     "values": [t, "+55" + t][:MAX_VALORES_FINDBYCOMM]},
                    _transporte=_transporte)
        res = r.get("result") if isinstance(r, dict) else None
        achados = sorted({str(x) for x in (res.get("CONTACT") or [])}) if isinstance(res, dict) else []
        ids.update(achados)
        cache.guardar("fones", t, "contatos", achados)
    return ids


# --------------------------------------------------------------------------
# decisão (pura) e patch
# --------------------------------------------------------------------------
def decidir(busca: dict, ids_telefone, tem_telefone: bool) -> dict:
    """{"acao": "confirmar"|"sugerir"|"ambiguo"|"nada", "cpf", "contato", "motivo"}."""
    cands = busca.get("candidatos") or []
    if not cands:
        return {"acao": "nada", "motivo": "nenhum contato homônimo no CRM"}
    if busca.get("truncado"):
        return {"acao": "ambiguo", "motivo": MOTIVO_AMBIGUO}
    cpfs = {c["cpf"] for c in cands}
    if len(cpfs) > 1:                    # homônimos com CPFs diferentes (ou um sem CPF)
        return {"acao": "ambiguo", "motivo": MOTIVO_AMBIGUO}
    cpf = cpfs.pop()
    if not cpf_util.valido(cpf):
        return {"acao": "ambiguo", "motivo": MOTIVO_CPF_INVALIDO}
    ids_cand = {c["id"] for c in cands}
    casados = ids_cand & set(ids_telefone or ())
    if casados:
        return {"acao": "confirmar", "cpf": cpf, "contato": sorted(casados)[0],
                "motivo": "nome completo + telefone no mesmo contato"}
    motivo = MOTIVO_TELEFONE_OUTRO if (tem_telefone and ids_telefone) else MOTIVO_SUGERIDO
    return {"acao": "sugerir", "cpf": cpf, "contato": sorted(ids_cand)[0], "motivo": motivo}


def _campo_declarado(campo: str) -> bool:
    return estado_store.PROPRIEDADE_CAMPOS.get(campo) == "identificacao"


def _casa(motivo, padrao) -> bool:
    motivo, padrao = str(motivo), str(padrao)
    return motivo.startswith(padrao[:-1]) if padrao.endswith("*") else motivo == padrao


def montar_patch(lote: dict, nome: str, decisao: dict) -> dict | None:
    acao = decisao["acao"]
    if acao == "nada":
        return None
    motivos = [str(m) for m in (lote.get("motivos_revisao") or [])]
    if acao == "confirmar":
        cpf = decisao["cpf"]
        patch = {
            "cpf": cpf,                                   # F1-12: só os 11 dígitos
            "cpf_formatado": cpf_util.formatar(cpf),
            "cpfs_distintos": [cpf],
            "identificacao": "OK",
            "cpf_origem": ORIGEM_CONFIRMADA,
            "nome": nome,
            "nome_origem": ORIGEM_CONFIRMADA,
        }
        cands = [str(x) for x in (lote.get("nomes_candidatos") or [])]
        if nome.upper() not in {c.upper() for c in cands}:
            patch["nomes_candidatos"] = cands + [nome]
        # só o motivo "sem CPF" (agora resolvido) e o próprio; nunca os outros
        resolver = [p for p in ("SEM_CPF:*", PREFIXO_MOTIVO + "*") if any(_casa(m, p) for m in motivos)]
        if resolver:
            patch["_resolver_motivos"] = resolver
        return patch
    marcado = estado_lote.marcar_revisao(lote, decisao["motivo"])
    patch = {"cpf_sugerido": decisao.get("cpf") if acao == "sugerir" else None,
             "motivos_revisao": marcado["motivos_revisao"],
             "requer_revisao": True, "pronto": False}
    if _campo_declarado("cpf_sugerido_motivo"):
        patch["cpf_sugerido_motivo"] = decisao["motivo"]
    return patch


def muda_algo(lote: dict, patch: dict) -> bool:
    mesclado, _ = estado_store.mesclar_lote(lote, patch, "identificacao")
    return mesclado != lote


# --------------------------------------------------------------------------
# rodada
# --------------------------------------------------------------------------
def e_alvo(lote) -> bool:
    """SEM_CPF, com arquivos, fora de descarte/conflito/divisão/ignorado.
    revisao_duplicata NÃO é pulado: recebe o CPF e continua em revisão."""
    if not isinstance(lote, dict):
        return False
    if lote.get("cpf") or lote.get("cpfs_distintos"):
        return False
    if not (lote.get("arquivos") or []):
        return False
    st = str(lote.get("status") or "")
    if st.startswith("descartado_"):
        return False
    if "CONFLITO" in st.upper() or "CONFLITO" in str(lote.get("identificacao") or "").upper():
        return False
    return estado_lote.estado_logico(lote) not in ESTADOS_IGNORAR


def caminho_estado() -> str:
    return os.environ.get("ESTADO_JSON") or caminhos.em_base("estado.json")


def rodar(aplicar: bool, limite: int = 0, caminho: str | None = None, cache: Cache | None = None,
          _transporte=None) -> dict:
    caminho = caminho or caminho_estado()
    estado = estado_store.ler_estado(caminho)
    cache = cache or Cache()
    alvos = []
    for lid, lote in (estado.get("lotes") or {}).items():
        if not e_alvo(lote):
            continue
        ficha = _ficha(lote)
        nome = nome_do_lote(lote, ficha)
        if nome:
            alvos.append((lid, lote, nome, telefones_do_lote(lote, ficha)))
    if limite:
        alvos = alvos[:limite]
    log.info("lotes SEM_CPF com nome e arquivos: %d", len(alvos))

    resumo = {"alvos": len(alvos), "confirmados": [], "sugeridos": [], "ambiguos": [],
              "sem_contato": 0, "interrompido": None, "backup": None, "gravados": 0,
              "conflitos": {}, "mudancas": []}
    mudancas = {}
    try:
        for lid, lote, nome, fones in alvos:
            busca = buscar_candidatos(nome, cache, _transporte=_transporte)
            ids_fone = None
            if fones and busca.get("candidatos") and not busca.get("truncado"):
                ids_fone = contatos_por_telefone(fones, cache, _transporte=_transporte)
            dec = decidir(busca, ids_fone, bool(fones))
            if dec["acao"] == "nada":
                resumo["sem_contato"] += 1
                continue
            chave = {"confirmar": "confirmados", "sugerir": "sugeridos", "ambiguo": "ambiguos"}[dec["acao"]]
            resumo[chave].append(lid)
            log.info("%s | %s | CPF %s | %s", lid, dec["acao"],
                     cpf_util.mascarar(dec.get("cpf")) if dec.get("cpf") else "-", dec.get("motivo", ""))
            patch = montar_patch(lote, nome, dec)
            if patch and muda_algo(lote, patch):
                mudancas[lid] = patch
    except RodadaInterrompida as e:
        resumo["interrompido"] = str(e)
        log.warning("rodada interrompida: %s — aplico só o que já foi apurado", e)
    finally:
        try:
            cache.gravar()
        except OSError as e:
            log.warning("não gravei o cache: %s", type(e).__name__)

    if aplicar and mudancas:
        # relê: lote que ganhou CPF/foi descartado desde o snapshot sai do patch
        atual = estado_store.ler_estado(caminho).get("lotes") or {}
        mudancas = {lid: p for lid, p in mudancas.items()
                    if e_alvo(atual.get(lid)) and muda_algo(atual[lid], p)}
    if aplicar and mudancas:
        resumo["backup"] = estado_store.fazer_backup(caminho, motivo="completar_cpf")
        resumo["conflitos"] = estado_store.atualizar_lotes(mudancas, "identificacao", caminho=caminho)
        resumo["gravados"] = len(mudancas)
    resumo["mudancas"] = sorted(mudancas)
    return resumo


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Completa CPF por nome + telefone (Bitrix).")
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--simular", action="store_true", help="padrão: não grava nada")
    modo.add_argument("--aplicar", action="store_true")
    ap.add_argument("--limite", type=int, default=0, help="máximo de lotes por rodada (0 = todos)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    seguranca.instalar_filtro_log()
    try:
        r = rodar(aplicar=a.aplicar, limite=a.limite)
    except estado_store.EstadoCorrompido as e:
        log.error("estado corrompido — nada gravado: %s", e)
        return 1
    except seguranca.SegredoAusente as e:
        log.error("webhook do Bitrix indisponível: %s", e)
        return 1
    log.info("confirmados=%d sugeridos=%d ambíguos=%d sem contato=%d gravados=%d",
             len(r["confirmados"]), len(r["sugeridos"]), len(r["ambiguos"]),
             r["sem_contato"], r["gravados"])
    if r["conflitos"]:
        log.warning("campos preservados de outro dono: %s", r["conflitos"])
    if not a.aplicar:
        log.info("SIMULAÇÃO: nada gravado (%d lote(s) mudariam). Use --aplicar.", len(r["mudancas"]))
    return 2 if r["interrompido"] else 0


if __name__ == "__main__":
    sys.exit(main())
