#!/usr/bin/env python3
"""
relatorio.py — o TEXTO que o bot fala na conversa (Bitrix e Telegram) e a decisão
de qual mensagem mandar em cada traço.

Pedido do dono (24/09/2026):
  · o bot do Bitrix fica *"full time na conversa, sem delay"*: pega o traço na hora,
    junta os documentos, vê o fechamento e o início do próximo pela nossa regra e
    **responde um relatório**;
  · no Telegram, o relatório é *"recebido - (nome do cliente) próximos"*.

REGRA DO TRAÇO (delimitador): o 1º traço ABRE o lote; cada traço seguinte FECHA o de
cima e **já deixa o de baixo aberto**. Então, na conversa, o bot fala:
  1º traço  -> "Lote aberto…"
  2º traço  -> "Lote registrado para triagem — <cliente> · N arquivo(s)…" (FASE 5:
              registra SEM ✅ de processamento; sem nome: "sem nome identificado")
  3º traço  -> "Lote registrado…" (de novo: fechou o anterior e abriu o seguinte)

O DESFECHO ("enviado para a pasta/Bitrix", "descartado", "em revisão", "erro")
sai DEPOIS — ver `montar_desfecho`/`texto_desfecho`/`texto_resumo_desfechos`.
"""
from __future__ import annotations

import datetime as _dt
import re

# CONTRATO DE NORMALIZAÇÃO DE NOME v1: a regex de CPF colado no fim e o crivo de
# "é nome de pessoa?" vivem em nome_regras.py (as DUAS pontas usam o mesmo).
from nome_regras import RX_CPF_NO_FIM, normalizar_nome_candidato, validar_nome

SINAL_BITRIX = "—"
SINAL_TELEGRAM = "/start"
TIMEOUT_PADRAO_MIN = 120

# ---------------------------------------------------------------------------
# FASE 5 — textos do bot. CONSTANTES no topo para o dono trocar fácil.
# ---------------------------------------------------------------------------
# Quando o bot não acha o nome do lote, ele FALA isto (antes saía
# "Recebido . N arquivos", como se o nome tivesse vindo tudo certo).
TEXTO_SEM_NOME = "sem nome identificado"

# (mudanca[6]a) O traço REGISTRA o lote — não afirma processamento. O ✅ de
# "Recebido" só pode sair no DESFECHO, depois que o material teve destino
# certo (pasta/Bitrix, descarte, revisão ou erro — mudanca[6]b).
MOLDE_REGISTRO = "📥 *Lote registrado para triagem* — {quem} · {n} {plural}."
MOLDE_REGISTRO_PROXIMO = ("Já deixei o próximo aberto: mande os documentos "
                          "e finalize com {sinal}.")

# (mudanca[6]b) desfechos POSTERIORES do lote — 1 por lote; o resumo diário
# agrega para não poluir o grupo. `detalhe` é o motivo ("kit repetido",
# "Sem NOME", "dead-letter", …).
DESFECHO_ENVIADO = "enviado"
DESFECHO_DESCARTADO = "descartado"
DESFECHO_REVISAO = "em_revisao"
DESFECHO_ERRO = "erro"
DESFECHOS_VALIDOS = (DESFECHO_ENVIADO, DESFECHO_DESCARTADO,
                     DESFECHO_REVISAO, DESFECHO_ERRO)
ROTULOS_DESFECHO = {
    DESFECHO_ENVIADO: "enviado para a pasta/Bitrix",
    DESFECHO_DESCARTADO: "descartado",
    DESFECHO_REVISAO: "em revisão",
    DESFECHO_ERRO: "erro",
}
MOLDE_DESFECHO = "✅ *Desfecho do lote* — {quem} · {n} {plural}: {rotulo}{detalhe_suf}."
MOLDE_RESUMO_CABECALHO = "📊 *Resumo dos lotes* ({quando}):"


def texto_abre(sinal: str = SINAL_BITRIX) -> str:
    """Mensagem do traço que ABRE (o primeiro da conversa)."""
    return (f"👋 *Lote aberto.*\n"
            f"Mande os documentos do cliente e *finalize com {sinal}*.")


def _rotulo_nome(nome: str | None) -> str:
    """Nome falado pelo bot — sem nome, o bot DIZ que não achou (fase 5)."""
    return str(nome or "").strip() or TEXTO_SEM_NOME


def texto_registro(nome: str | None = None, n: int = 0, sinal: str = SINAL_BITRIX) -> str:
    """(mudanca[6]a) o que o bot fala NO TRAÇO: registra o lote, SEM check.

    O lote ainda nem foi validado (pode ser descartado, morrer no worker ou
    ficar em revisão) — o ✅ de processamento fica para `texto_desfecho`, que
    só sai quando o material teve destino certo.
    """
    plural = "arquivo" if int(n or 0) == 1 else "arquivos"
    return (f"{MOLDE_REGISTRO.format(quem=_rotulo_nome(nome), n=int(n or 0), plural=plural)}\n"
            f"{MOLDE_REGISTRO_PROXIMO.format(sinal=sinal)}")


def texto_fecha(nome: str | None = None, n: int = 0, sinal: str = SINAL_BITRIX) -> str:
    """Mensagem do traço que FECHA — já anuncia o próximo lote aberto.

    O nome só entra na fala quando valida como nome de pessoa (contrato v1) —
    acaba o feedback falso do bot ("Recebido — Vidabbzao" seguido de morte do
    material no worker). Sem nome validado, o bot diz `TEXTO_SEM_NOME` em vez
    de omitir. LEGADO (fase 5, mudanca[6]a): o traço de produção agora fala
    `texto_registro` (sem ✅) e o desfecho honesto usa `texto_desfecho` — este
    texto fica para os testes/compatibilidade; o ✅ dele NÃO é o molde do
    desfecho (o molde é `MOLDE_DESFECHO`).
    """
    nome_limpo = _limpar_nome(nome)
    quem = TEXTO_SEM_NOME
    # sai o CANÔNICO do contrato (PONTO 1): 'MENOR ANA SOFIA…' é anunciado como
    # 'ANA SOFIA TESTE LEITE' — a MESMA string que vira cliente.nome na pasta.
    canonico = normalizar_nome_candidato(nome_limpo)
    if canonico and validar_nome(canonico):
        quem = canonico
    plural = "arquivo" if int(n or 0) == 1 else "arquivos"
    return (f"✅ *Recebido — {quem}* · {int(n or 0)} {plural}.\n"
            f"Já deixei o próximo aberto: mande os documentos e finalize com {sinal}.")


def _limpar_nome(t) -> str:
    # Casca sobre o passo 1a do contrato (borda/espaços de nome_regras) + os
    # pontinhos próprios do relatório.
    from nome_regras import colapsar_bordas
    return colapsar_bordas(t).strip(".,;:")


# Palavras que NUNCA são nome de cliente. Montada com o que o extrator genérico
# pegou de errado no grupo em 24/09/2026 ("Dpp está nessa página", "É isso aq ?",
# "Não avança nesse botão já assistir todo esse vídeo", "BENEFICIO BPC LOAS FICHA").
_PALAVRAS_LIXO = {
    "dpp", "pagina", "página", "isso", "aq", "aqui", "nessa", "nesse", "essa", "esse",
    "nao", "não", "avanca", "avança", "botao", "botão", "video", "vídeo", "assistir",
    "todo", "toda", "está", "esta", "dele", "dela", "ficha", "atendimento", "dados",
    "necessarios", "necessários", "beneficio", "benefício", "bpc", "loas", "mae", "mãe",
    "responsavel", "responsável", "cliente", "cpf", "rg", "nis", "senha", "gov",
    "auxilio", "auxílio", "maternidade", "doenca", "doença", "aposentadoria", "pensao",
    "pensão", "documento", "documentos", "endereco", "endereço", "telefone", "email",
    "lote", "aberto", "recebido", "segue", "triagem", "pessoais", "para", "com",
    "por", "esta", "estão", "sera", "será", "vai", "favor", "obrigado", "ok",
    "boa", "bom", "tarde", "noite", "dia", "nome", "completo", "estado", "civil",
    "anexo", "anexos", "tudo", "bem", "nada", "obrigada",
}
_CONECTORES = {"da", "de", "do", "das", "dos", "e", "di", "du"}

RX_NOME_FICHA = re.compile(
    r"\bNome(?:\s+\*{0,2}\s*(?:completo|d[ao]s?\s+cliente)\*{0,2})?"
    r"\s*\**\s*:\s*\**\s*([^\n*]*)", re.I)
# grupo 1 = rótulo (decide a prioridade), grupo 2 = nome. "= MENOR ANA SOFIA",
# "= CLIENTE : NAIARA…".
RX_NOME_CABECALHO = re.compile(
    r"=\s*(GESTANTE|MENOR|CLIENTE|SEGURADA|TITULAR|BENEFICI[ÁA]RIO)\b\s*:?\s*([^\n=]+)", re.I)
# (mudanca[5]) rótulo SEM '=' mas com dois-pontos — o formato que a equipe
# manda de verdade ("CLIENTE : NAIARA…", "LOAS : ROSANA…", "BPC : X").
# Corta em '*' para não engolir o que vem depois do nome na mesma linha.
RX_NOME_ROTULO_SOLTO = re.compile(
    r"(GESTANTE|MENOR|CLIENTE|LOAS|BPC|SEGURADA|TITULAR|BENEFICI[ÁA]RIO)"
    r"\s*\**\s*:\s*\**\s*([^\n=•*]+)", re.I)
RX_MAE = re.compile(r"\b(m[ãa]e|respons[áa]vel|genitora)\b", re.I)
RX_CPF_LINHA = re.compile(r"\bcpf\b", re.I)
# CPF solto em qualquer formato ("000.000.000-00" ou só os 11 dígitos) — o grupo manda
# nome numa mensagem e CPF noutra; a linha do CPF é a âncora do nome de cima.
RX_CPF_NUM = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}[-.]?\d{2}\b")
# RX_CPF_NO_FIM é IMPORTADO de nome_regras (contrato v1) — ver o topo do arquivo.

# (mudanca[5]) PRIORIDADE dos candidatos de nome (menor = mais confiável).
# A MENOR/GESTANTE vence o nome sem rótulo da ficha — regra do dono, não reabrir.
PRIO_ROTULO_TITULAR = 0      # "= MENOR …" / "= GESTANTE …"
PRIO_ROTULO_CLIENTE = 1      # "= CLIENTE : X", "LOAS : X", "BPC : X", …
PRIO_FICHA = 2               # "Nome: …" da ficha de atendimento
PRIO_PERTO_DO_CPF = 3        # linha que encosta num CPF (o CPF é descascado)
PRIO_QUALQUER = 4            # qualquer linha que pareça nome de pessoa


def _nome_canonico(bruto) -> str | None:
    """(mudanca[5]) UM canônico de nome do lote — contrato v1 (nome_regras).

    É o MESMO par `normalizar_nome_candidato` + `validar_nome` que vira
    lote['nome']/cliente.nome em lote_para_job.montar_job: o bot só anuncia
    o que a pasta vai ter. Devolve None quando nada aproveitável.
    """
    can = normalizar_nome_candidato(bruto)
    if can and validar_nome(can):
        return can
    return None


def extrair_nome_do_lote(candidatos) -> str | None:
    """(mudanca[5]) UM extrator de nome do lote — o mesmo do bot e da pasta.

    Usado por `nome_do_bloco` (texto cru do grupo) e `nome_do_lote` (nomes já
    extraídos). `candidatos` aceita str ou (prioridade, str) — ver PRIO_*.
    Dentro da mesma prioridade vale o mais completo (mais palavras, maior).

    Sai SEMPRE no canônico do contrato (CPF colado no fim descascado, rótulo
    de papel/benefício tirado, limites 5..80 alinhados) — é a string que a
    equipe busca no sistema. Nada válido → None (o bot diz TEXTO_SEM_NOME).
    """
    melhor, chave = None, None
    for item in (candidatos or []):
        prio, bruto = item if isinstance(item, tuple) else (PRIO_QUALQUER, item)
        can = _nome_canonico(bruto)
        if not can:
            continue
        ordem = (int(prio), -len(can.split()), -len(can))
        if chave is None or ordem < chave:
            melhor, chave = can, ordem
    return melhor


def _parece_nome(t) -> bool:
    """Heurística conservadora para LINHA SOLTA: nome de pessoa, sem número/lixo.

    Casos reais do grupo que ela recusa (o extrator genérico aceitava todos):
    "Dpp está nessa página", "É isso aq ?", "Não avança nesse botão…",
    "• **E-mail:**Viviane Domingues antonio", "👤 **DADOS PESSOAIS",
    "✅ *Lote aberto*", "Segue para a triagem".

    Limites ALINHADOS ao contrato v1 (5..80, sem teto de palavras): "Ana Li"
    e nomes de 7+ palavras passam aqui como passam na pasta. Depois da
    heurística local, o contrato tem a palavra final (`_nome_canonico`).
    """
    t = _limpar_nome(RX_CPF_NO_FIM.sub("", str(t or "")))
    if not (5 <= len(t) <= 80):
        return False
    # só letras (com acento), espaço, apóstrofo, hífen e ponto: qualquer ':' '*' '•'
    # dígito ou emoji já elimina linha de formulário/markdown/mensagem do bot.
    if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'´`^~.\- ]+", t):
        return False
    palavras = t.split()
    if len(palavras) < 2:
        return False
    baixas = [p.lower().strip(".") for p in palavras]
    if any(p in _PALAVRAS_LIXO for p in baixas):
        return False
    nucleo = [p for p in palavras if p.lower() not in _CONECTORES]
    if len(nucleo) < 2:
        return False
    minusculas = sum(1 for p in nucleo if p[:1].islower())
    if minusculas == len(nucleo):
        # nome todo minúsculo em 2 palavras é nome de verdade ("aleandra alves",
        # "gilmar silva" — formato real do grupo 13/10/2026); frase de conversa
        # cai na lista _PALAVRAS_LIXO acima ("boa tarde", "segue anexo").
        return len(nucleo) >= 2 and validar_nome(t)
    if minusculas == 0:                    # nome próprio (MARIA JOSE / Raquel Silva)
        return validar_nome(t)
    return False                           # maiúscula misturada com minúscula = frase


def nome_do_lote(nomes) -> str | None:
    """Melhor nome de cliente entre os candidatos do lote (o mais completo).

    (mudanca[5]) Delega no extrator único — sai no canônico do contrato, a
    MESMA string que a pasta grava (antes aqui ainda mandava .upper(), e o
    bot anunciava uma grafia que o sistema não tinha).
    """
    return extrair_nome_do_lote([_limpar_nome(n) for n in (nomes or [])])


def nome_do_bloco(textos) -> str | None:
    """Nome do cliente a partir dos textos do bloco (mudanca[5]).

    Extrator ÚNICO: os candidatos passam por `extrair_nome_do_lote` — o mesmo
    contrato v1 que vira lote['nome'] na pasta. Ordem de confiança (PRIO_*):

    1. `= MENOR/GESTANTE: <nome>` (o nome da MENOR vence — regra do dono);
    2. `= CLIENTE/SEGURADA/… : <nome>` e os formatos SEM '=' da equipe
       ("CLIENTE : X", "LOAS : X", "BPC : X");
    3. `Nome:` (cabeçalho da ficha de atendimento);
    4. linha que encosta num CPF — na frente ou com o CPF colado no FIM
       ("GILMAR TESTE DA SILVA CPF 606…"), sempre descascado antes de sair;
    5. qualquer linha que pareça nome de pessoa (ignora MÃE/responsável).

    Sem candidato válido devolve None — quem fala é `texto_registro`, que diz
    "sem nome identificado" em vez de omitir como se estivesse tudo certo.
    """
    linhas = []
    for t in (textos or []):
        for linha in str(t or "").splitlines():
            limpa = _limpar_nome(linha)
            if limpa:
                linhas.append(linha.strip())
    if not linhas:
        return None
    inteiro = "\n".join(linhas)
    candidatos = []

    for rx in (RX_NOME_CABECALHO, RX_NOME_ROTULO_SOLTO):
        for m in rx.finditer(inteiro):
            rotulo = (m.group(1) or "").upper()
            prio = (PRIO_ROTULO_TITULAR
                    if rotulo in ("GESTANTE", "MENOR") else PRIO_ROTULO_CLIENTE)
            candidato = _limpar_nome(m.group(2))
            if not candidato:
                # "• **Nome completo:" com o valor na LINHA SEGUINTE (template do
                # grupo 10/10/2026) — sem isso o nome do bloco sumia.
                for prox in inteiro[m.end():].splitlines():
                    candidato = _limpar_nome(prox)
                    if candidato:
                        break
            candidatos.append((prio, candidato))

    for m in RX_NOME_FICHA.finditer(inteiro):          # 3) ficha
        candidato = _limpar_nome(m.group(1))
        if not candidato:
            for prox in inteiro[m.end():].splitlines():
                candidato = _limpar_nome(prox)
                if candidato:
                    break
        candidatos.append((PRIO_FICHA, candidato))

    for i, linha in enumerate(linhas):                 # 4) encosta no CPF
        prox = linhas[i + 1] if i + 1 < len(linhas) else ""
        if (RX_CPF_LINHA.search(prox) or RX_CPF_NUM.search(prox)
                or RX_CPF_NO_FIM.search(linha)):
            candidatos.append((PRIO_PERTO_DO_CPF, linha))

    for linha in linhas:                              # 5) qualquer linha
        if RX_MAE.search(linha):
            continue
        candidato = _limpar_nome(linha)
        if _parece_nome(candidato):
            candidatos.append((PRIO_QUALQUER, candidato))
    return extrair_nome_do_lote(candidatos)


def _quando(valor):
    if isinstance(valor, _dt.datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=_dt.timezone.utc)
    if not valor:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def abre_ou_fecha(aberto: dict | None, ts, timeout_min: int = TIMEOUT_PADRAO_MIN) -> str:
    """'ABRE' ou 'FECHA' para um traço que chegou agora.

    Sem bloco aberto = ABRE (primeiro da conversa). Com bloco aberto = FECHA,
    SEMPRE — recente OU vencido por timeout. O fechamento do anterior tem que
    aparecer no grupo, senão o lote anterior fica "aberto" para sempre na
    conversa (bug 15/10/2026: o vencido virava "Lote aberto" solto e o grupo
    nunca via o fechamento do que tinha ficado aberto).
    """
    if not aberto:
        return "ABRE"
    return "FECHA"


def decidir_resposta(aberto: dict | None, nome: str | None = None, n_arquivos: int = 0,
                     sinal: str = SINAL_BITRIX, msg=None, ts=None, autor: str = "",
                     timeout_min: int = TIMEOUT_PADRAO_MIN) -> tuple[str, dict | None]:
    """Qual mensagem mandar AGORA e qual passa a ser o bloco aberto.

    `aberto` é o bloco aberto do chat ({"msg","ts","autor"}) ou None. O bloco devolvido
    é sempre o NOVO (o mesmo traço que fecha já abre o de baixo — regra do delimitador).

    (mudanca[6]a) o traço fala o REGISTRO do lote, sem ✅ de processamento: a
    esteira ainda nem rodou (o lote pode ser descartado, morrer no worker ou
    ficar em revisão). O desfecho sai depois, por `texto_desfecho`.
    """
    quando = _quando(ts)
    novo = {"msg": msg, "ts": quando.isoformat() if quando else None, "autor": autor}

    if abre_ou_fecha(aberto, ts, timeout_min) == "FECHA":
        return texto_registro(nome, n_arquivos, sinal), novo
    return texto_abre(sinal), novo


# ---------------------------------------------------------------------------
# (mudanca[6]b) DESFECHO POSTERIOR do lote — o mecanismo (função/slot) que a
# esteira chama quando o material tem destino certo. A chamada ainda NÃO está
# plugada em todos os pontos (triagem → descartado/revisão; fila/worker →
# enviado/erro): quem descobre o destino monta o registro com
# `montar_desfecho` e a emissão no grupo sai por `texto_desfecho` (1 lote) ou
# `texto_resumo_desfechos` (resumo DIÁRIO, para não poluir o grupo).
# ---------------------------------------------------------------------------
def montar_desfecho(lote_id, status: str, nome: str | None = None,
                    n_arquivos: int = 0, detalhe: str = "",
                    quando=None) -> dict:
    """Registro de desfecho de um lote. `status` ∈ DESFECHOS_VALIDOS."""
    if status not in DESFECHOS_VALIDOS:
        raise ValueError(f"desfecho desconhecido: {status!r} — use um de {DESFECHOS_VALIDOS}")
    q = _quando(quando) or _dt.datetime.now(_dt.timezone.utc)
    return {"lote_id": str(lote_id or ""), "status": status,
            "nome": str(nome or "").strip(), "n_arquivos": int(n_arquivos or 0),
            "detalhe": str(detalhe or "").strip(),
            "quando": q.isoformat(timespec="seconds")}


def texto_desfecho(desfecho: dict) -> str:
    """1 desfecho = 1 mensagem honesta. O ✅ de processamento só vive aqui."""
    d = desfecho or {}
    n = int(d.get("n_arquivos") or 0)
    plural = "arquivo" if n == 1 else "arquivos"
    detalhe = str(d.get("detalhe") or "").strip()
    rotulo = ROTULOS_DESFECHO.get(d.get("status"), ROTULOS_DESFECHO[DESFECHO_ERRO])
    return MOLDE_DESFECHO.format(
        quem=_rotulo_nome(d.get("nome")), n=n, plural=plural, rotulo=rotulo,
        detalhe_suf=(f" — {detalhe}" if detalhe else ""))


def texto_resumo_desfechos(desfechos, quando=None) -> str:
    """Resumo DIÁRIO dos desfechos — 1 mensagem para o grupo, não 1 por lote."""
    ds = list(desfechos or [])
    q = _quando(quando) or _dt.datetime.now(_dt.timezone.utc)
    linhas = [MOLDE_RESUMO_CABECALHO.format(quando=q.strftime("%d/%m/%Y"))]
    for d in ds:
        n = int(d.get("n_arquivos") or 0)
        plural = "arquivo" if n == 1 else "arquivos"
        detalhe = str(d.get("detalhe") or "").strip()
        rotulo = ROTULOS_DESFECHO.get(d.get("status"), d.get("status") or "?")
        linhas.append(f"· {_rotulo_nome(d.get('nome'))} · {n} {plural} — {rotulo}"
                      + (f" ({detalhe})" if detalhe else ""))
    linhas.append(f"Total: {len(ds)} lote(s).")
    return "\n".join(linhas)
