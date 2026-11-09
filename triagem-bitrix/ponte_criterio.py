#!/usr/bin/env python3
"""
ponte_criterio.py — REGRA de quais lotes de documento podem ser encaminhados ao painel.

Decisão do dono (24/09/2026): além do lote com traço de abertura E fechamento, vale
também o lote fechado por janela/timeout/inatividade QUANDO é de UM cliente só e está
limpo. Continua fora o lote com cara de misturado: "cabeçalho de outro cliente",
mais de um CPF, identificação não-OK, conflito ou marcado para revisão manual.

Usado por `lote_para_fila.py` (encaminhar) e por `monitor_lotes.py` (avisar).
Puro e testado em testes/test_ponte_criterio.py.
"""
from __future__ import annotations

FECHAMENTOS_FALLBACK = ("FECHADO_POR_JANELA", "FECHADO_POR_TIMEOUT",
                        "FECHADO_POR_INATIVIDADE", "FECHADO_POR_TROCA_DE_PESSOA")


def _tem_traco(lote: dict) -> bool:
    return (str(lote.get("modo") or "") == "marcadores"
            and bool(lote.get("sinal_abertura")) and bool(lote.get("sinal_fechamento")))


def motivo_recusa(lote) -> str:
    """"" = pode encaminhar. Senão, o motivo (texto curto) de por que NÃO pode."""
    if not isinstance(lote, dict) or not lote:
        return "lote vazio"
    # `pronto` deixou de ser a única porta (pendência da frente F): lote confirmado por
    # nome+telefone (completar_cpf_por_nome) não ganha mais pronto=True; vale o modelo
    # único estado_lote.pode_enfileirar (fechado, identidade OK, documentos íntegros).
    if not lote.get("pronto"):
        import estado_lote
        if not estado_lote.pode_enfileirar(lote_documental(lote))[0]:
            return "lote não está pronto"
    if not (lote.get("arquivos") or []):
        return "sem arquivo (documento) no lote"
    if "CONFLITO" in str(lote.get("status") or "").upper() \
            or "CONFLITO" in str(lote.get("identificacao") or "").upper():
        return "lote marcado como conflito"
    if lote.get("requer_revisao"):
        return "lote marcado para revisão manual"
    # PONTO 2 (CONTRATO DE NORMALIZAÇÃO v1): nome de 1 palavra/ruído NÃO vai para
    # o painel — o lote fica em REVISÃO com o CPF como âncora (nunca dead-letter).
    # Vale para o lote fechado por traço E pelo fallback: o nome passa pelo MESMO
    # normalizador/validador das duas pontas (ver nome_regras.py).
    from nome_regras import normalizar_nome_candidato, validar_nome, _exigir_completo_cfg
    if not validar_nome(normalizar_nome_candidato(lote.get("nome")), _exigir_completo_cfg()):
        return "nome não identificado (1 palavra/ruído) — CPF é a âncora; vai para revisão"
    if _tem_traco(lote):
        return ""
    # fechado por fallback: só passa se for de um cliente só e limpo
    ids = [str(x) for x in (lote.get("cpfs_distintos") or []) if x]
    if not ids and lote.get("cpf"):
        ids = [str(lote["cpf"])]
    if len(set(ids)) > 1:
        return f"lote com mais de um CPF ({len(set(ids))}) — precisa revisão"
    if not ids:
        return "lote sem CPF identificado"
    if not str(lote.get("identificacao") or "").upper().startswith("OK"):
        return f"identificação não está OK ({lote.get('identificacao')!r})"
    fechou = str(lote.get("fechado_por") or "").lower()
    if "outro cliente" in fechou or "cpf diferente" in fechou:
        return "cabeçalho de outro cliente (CPF diferente)"
    return ""


def lote_aceitavel(lote) -> bool:
    """True quando o lote pode ir para o painel."""
    return motivo_recusa(lote) == ""


def hashes_do_lote(lote) -> set[str]:
    """sha256[:12] de cada DOCUMENTO do lote — mesmo formato do registro da ponte.
    Mídia (áudio/vídeo, ver tipos_documento) não conta: não vai ao painel."""
    saida = set()
    for a in ((lote or {}).get("arquivos") or []):
        if not isinstance(a, dict) or tipo_arquivo(a) == "midia":
            continue
        h = a.get("sha256")
        if h:
            saida.add(str(h)[:12])
    return saida


def kit_novo(lote, hashes_ja_enviados) -> bool:
    """True quando o lote tem ALGUM arquivo que o cliente ainda não tinha mandado.

    Decisão do dono (24/09/2026): o 2º kit do mesmo CPF entra quando os arquivos são
    DIFERENTES (compara sha256); reenvio do mesmo kit continua fora. Sem hash no estado
    não dá para provar que é novo — devolve False (fica fora, como hoje).

    ATENÇÃO (F2-03): `False` aqui NÃO prova duplicata quando o download está
    incompleto. Para DECIDIR descarte use `situacao_kit` (devolve "indeterminado").
    """
    meus = hashes_do_lote(lote)
    antigos = {str(h)[:12] for h in (hashes_ja_enviados or ()) if h}
    return bool(meus - antigos)


# ---------------------------------------------------------------------------
# documento × mídia (C1/S10) e decisão de duplicata (F2-03)
# ---------------------------------------------------------------------------
TIPOS_VALIDOS = ("documento", "midia", "desconhecido")


def tipo_arquivo(meta) -> str:
    """'documento' | 'midia' | 'desconhecido' de um item de `lote["arquivos"]`.

    Usa o `tipo` gravado pela triagem/download quando é um dos três valores; senão
    classifica pelo nome (original, salvo ou caminho) com `tipos_documento`.
    """
    if not isinstance(meta, dict):
        return "desconhecido"
    t = str(meta.get("tipo") or "").strip().lower()
    import tipos_documento

    if t in (tipos_documento.DOCUMENTO, tipos_documento.MIDIA):
        return t
    # G-02: o download_svc marca mídia com erro_tipo='midia' (lotes já gravados podem
    # não ter `tipo` nem nome — leitura pelo webhook). Mídia não é pendência de download;
    # vale também sobre um `tipo='desconhecido'` provisório gravado antes do download.
    if str(meta.get("erro_tipo") or "").strip().lower() == tipos_documento.MIDIA:
        return tipos_documento.MIDIA
    if t in TIPOS_VALIDOS:
        return t

    mime = meta.get("mime") or meta.get("mime_type")
    melhor = "desconhecido"
    for nome in (meta.get("nome_original"), meta.get("nome_salvo"), meta.get("caminho")):
        if nome:
            c = tipos_documento.classificar(str(nome), mime)
            if c != "desconhecido":
                return c
    if mime:
        melhor = tipos_documento.classificar(None, mime)
    return melhor


def lote_documental(lote) -> dict:
    """Cópia RASA do lote só com os arquivos que contam como documento/pendência.

    Mídia sai de `arquivos` e de `file_ids` (não conta como pendência de download nem
    vai ao staging); fica contada em `_midias_fora`. `desconhecido` FICA (é baixado e
    reclassificado antes de ir ao painel)."""
    if not isinstance(lote, dict):
        return {}
    view = dict(lote)
    docs, midias = [], []
    for a in (lote.get("arquivos") or []):
        (midias if tipo_arquivo(a) == "midia" else docs).append(a)
    view["arquivos"] = docs
    fids_midia = {str(a.get("file_id")) for a in midias
                  if isinstance(a, dict) and a.get("file_id") not in (None, "")}
    if lote.get("file_ids") and fids_midia:
        view["file_ids"] = [f for f in lote["file_ids"] if str(f) not in fids_midia]
    view["_midias_fora"] = len(midias)
    return view


def kit_completo(lote) -> bool:
    """True quando TODO documento do lote está baixado, com sha256 e tamanho > 0.

    Todo `file_id` documental precisa ter registro em `arquivos`: file_id sem meta é
    documento ainda não registrado/baixado, logo o kit não está completo."""
    view = lote_documental(lote)
    docs = view.get("arquivos") or []
    if not docs:
        return False
    com_meta = {str(a.get("file_id")) for a in docs
                if isinstance(a, dict) and a.get("file_id") not in (None, "")}
    if any(str(f) not in com_meta for f in (view.get("file_ids") or []) if f not in (None, "")):
        return False
    for a in docs:
        if not isinstance(a, dict) or a.get("baixado") is not True or not a.get("sha256"):
            return False
        try:
            if int(a.get("tamanho_bits") or a.get("tamanho") or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def situacao_kit(lote, hashes_ja_enviados) -> str:
    """'novo' | 'repetido' | 'indeterminado' — base da decisão de duplicata (F2-03).

    Só existe 'repetido' com o kit 100% baixado (todo documento com sha256): com
    qualquer pendência a resposta é 'indeterminado' (adiar, nunca descartar)."""
    if not kit_completo(lote):
        return "indeterminado"
    return "novo" if kit_novo(lote, hashes_ja_enviados) else "repetido"


def alta_confianca(lote) -> bool:
    """Lote de traço (LOTE-SIG / modo marcadores / confianca ALTA) — vai antes na fila."""
    lote = lote or {}
    return (str(lote.get("confianca") or "").upper() == "ALTA"
            or str(lote.get("lote_id") or "").startswith("LOTE-SIG")
            or str(lote.get("modo") or "") == "marcadores")


def chave_ordem(lote):
    """Ordem da ponte (F2-04): 1º confiança ALTA (traço), depois o MAIS ANTIGO pelo
    `inicio` (datetime, não pelo id); data ilegível vai para o fim; desempate por id."""
    import datetime as _dt

    lote = lote or {}
    try:
        ini = _dt.datetime.fromisoformat(str(lote.get("inicio")).replace("Z", "+00:00"))
        if ini.tzinfo is None:
            ini = ini.replace(tzinfo=_dt.timezone.utc)
        ts = ini.timestamp()
    except (TypeError, ValueError):
        ts = float("inf")
    return (0 if alta_confianca(lote) else 1, ts, str(lote.get("lote_id") or ""))


# ---------------------------------------------------------------------------
# elegibilidade única da ponte (F2-01/F2-02): estado_lote.pode_enfileirar sobre a
# visão SÓ-DOCUMENTOS + regra do escritório. Força manual nunca pula identidade
# nem integridade dos arquivos.
# ---------------------------------------------------------------------------
STATUS_DUPLICATA = ("descartado_duplicata", "revisao_duplicata")


def sem_decisao_humana(lote) -> dict:
    """Cópia do lote sem as marcas que um OPERADOR pode sobrepor com --forcar:
    requer_revisao/motivos_revisao e status de duplicata (volta ao status_anterior).
    Identidade (CPF/identificação/conflito), fechamento e arquivos continuam valendo."""
    novo = dict(lote or {})
    novo["requer_revisao"] = False
    novo["motivos_revisao"] = []
    if str(novo.get("status") or "") in STATUS_DUPLICATA:
        novo["status"] = novo.get("status_anterior") or "FECHADO_POR_JANELA"
    return novo


def avaliar(lote, forcar: bool = False) -> dict:
    """{"ok", "categoria", "motivos", "ignorados"} — decisão da ponte para UM lote.

    categoria (quando não ok) = `estado_lote.estado_logico` da visão só-documentos
    (AGUARDANDO_DOWNLOAD, REVISAO, AGUARDANDO_IDENTIFICACAO, ABERTO, DESCARTADO_DUPLICATA…)
    ou "REGRA" quando quem recusou foi a regra do escritório (`motivo_recusa`).
    Com `forcar=True` a regra do escritório, a revisão e a marca de duplicata são
    ignoradas (e listadas em `ignorados`), mas `pode_enfileirar` continua exigindo
    identidade OK com CPF válido e todo documento baixado/íntegro.
    """
    import estado_lote

    view = lote_documental(lote)
    base = sem_decisao_humana(view) if forcar else view
    ok, motivos = estado_lote.pode_enfileirar(base)
    if not ok:
        return {"ok": False, "categoria": estado_lote.estado_logico(base),
                "motivos": list(motivos), "ignorados": []}
    recusa = motivo_recusa(view)
    if not forcar:
        if recusa:
            return {"ok": False, "categoria": "REGRA", "motivos": [recusa], "ignorados": []}
        return {"ok": True, "categoria": "PRONTO", "motivos": [], "ignorados": []}
    ignorados = list(estado_lote.pode_enfileirar(view)[1]) + ([recusa] if recusa else [])
    return {"ok": True, "categoria": "PRONTO", "motivos": [], "ignorados": ignorados}
