#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regras de "isso NÃO é nome de cliente" para a triagem (corpus real, 24/09/2026).

Espelha `src/nome-regras.js` do painel — as duas pontas precisam concordar.

Corpus: 138 jobs com ficha + os lotes da triagem. O sistema aceitou como CLIENTE
coisas como 'Solteira', 'Casada', 'Certo', 'Domestica', 'Vidabbzao', 'Fatura.pdf',
'É isso aq ?', 'Dpp está nessa página', 'aperto em continuar e não vai',
'MÃE RESPONSAVEL: PATRICIA' e 'Certidão de casamento Bianca' — e esses valores
viraram nome de lote/pasta.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

# estado civil sozinho
RE_ESTADO_CIVIL = re.compile(
    r"^(?:solteir[ao]|casad[ao]|divorciad[ao]|desquitad[ao]|vi[uú]v[ao]|separad[ao]|"
    r"uni[aã]o\s+est[aá]vel|amasiad[ao])$", re.I)

# começando por rótulo de DOCUMENTO (o nome do cliente não é o nome do documento)
RE_DOCUMENTO = re.compile(
    r"^(?:certid[aã]o|fatura|boleto|comprovante|documento|nota\s+fiscal|extrato|"
    r"contrato|procura[cç][aã]o|declara[cç][aã]o|atestado|laudo|caderneta|kit|guia|"
    r"recibo|protocolo|link|rg|cpf|cnis|nis)\b", re.I)

# começando por PREPOSIÇÃO: 'Da marya', 'Do Matheus' são POSSE/nome de parente
# solto na conversa, não nome do cliente (corpus 13/10/2026). Nomes próprios que
# COMEÇAM com 'De/Da' como 'Deolinda' não casam (não há fronteira de palavra).
RE_PREPOSICAO_INICIO = re.compile(
    r"^(?:d[aeo]s?|e|em|no|na|nos|nas|com|para|por|ao|[àa]s?)\b", re.I)

# rótulo de RESPONSÁVEL (o titular está em outra linha: "= MENOR <nome>")
RE_RESPONSAVEL = re.compile(
    r"^(?:m[ãa]e|pai|respons[aá]vel|genitor[ar]|representante|acompanhante|"
    r"titular\s+do\s+benef[ií]cio)\b", re.I)

# rótulo de BENEFÍCIO (PONTO 2.6, 14/10/2026): 'PENSÃO POR MORTE', 'AUXÍLIO
# DOENÇA', 'BPC/LOAS' viravam nome de cliente/lote. Espelho exato de
# BENEFICIO do nome-regras.js — as duas pontas precisam concordar.
# Âncora ^ + fronteira de palavra (ou '/'): 'AUXILIADORA MARIA' e
# 'FULANO BENEFICIO DA SILVA' (nomes reais) NÃO casam.
RE_BENEFICIO = re.compile(
    r"^(?:aux(?:[íi]lio)?|sal[áa]rio|aposentadoria|loas|bpc|pens[ãa]o|"
    r"bolsa(?:\s+fam(?:[íi]lia)?)?|maternidade|doen[çc]a|acidente|invalidez|"
    r"rural|idade|benef[íi]cio)(?:\b|/)", re.I)

# conversa solta (o que a pessoa escreve no grupo)
RE_CONVERSA = re.compile(
    r"\b(?:vai|vou|vamos|est[aá]|estou|estava|foi|fui|n[ãa]o|sim|aperto|apertar|"
    r"assinar|assinou|assinatura|consegui|consegue|conseguindo|avan[çc]a|avan[çc]ar|"
    r"manda|mandei|mandar|bot[ãa]o|p[áa]gina|dpp|isso|aquilo|aqui|certo|ok|"
    r"dom[eé]stica|trabalho|trabalha|recebo|preciso|precisa|falta|cad[eê]|qual|"
    r"quando|onde|como|porque|gov|link|ela|ele|essa|esse|este|fez|faz|fiz|"
    r"semana|passada|passado|ontem|hoje|amanh[ãa]|agora|depois|antes|entendi|"
    r"olha|obrigada|brigada|ainda|conseguiu|deixou|passou|pegou|trouxe|testei|"
    r"funcionou|apareceu|menina|gente|pessoal)\b", re.I)

# POSSE/PARENTESCO: 'Meu namorado' foi gravado como NOME DO CLIENTE (caso real
# 25/09/2026 — a ficha do template tem campo livre e a cliente escreveu isso).
RE_POSSE = re.compile(r"^(?:meu|minha|meus|minhas|nosso|nossa|nossos|nossas|teu|tua|seu|sua)\b", re.I)
# possessivo + parentesco em QUALQUER posição: "Da minha mae", "nome do meu filho",
# "filha da minha esposa" (caso real 25/09: cliente preencheu "Da minha mae" no Nome)
RE_POSSE_PARENTESCO = re.compile(
    r"\b(?:meu|minha|meus|minhas|nosso|nossa|nossos|nossas|seu|sua|teu|tua)\s+"
    r"(?:namorado|namorada|marido|esposa|esposo|companheiro|companheira|noivo|noiva|filho|filha|"
    r"pai|m[ãa]e|irm[ãa]o|irm[ãa]|av[ôo]|av[óo]|tio|tia|primo|prima|sogra|sogro|genro|nora|"
    r"enteado|enteada|cunhado|cunhada|sobrinho|sobrinha|neto|neta|beb[êe]|nen[êe]m)\b", re.I)

RE_PARENTESCO = re.compile(
    r"^(?:namorado|namorada|marido|esposa|esposo|companheiro|companheira|noivo|noiva|filho|filha|"
    r"pai|m[ãa]e|irm[ãa]o|irm[ãa]|av[ôo]|av[óo]|tio|tia|primo|prima|sogra|sogro|genro|nora|"
    r"enteado|enteada|cunhado|cunhada|sobrinho|sobrinha|neto|neta)\b", re.I)

RE_ARQUIVO = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|docx?|xlsx?|txt)$", re.I)
RE_PONTUACAO = re.compile(r"[?!]|\d{4,}|@|https?:")

# ---------------------------------------------------------------------------
# CONTRATO DE NORMALIZAÇÃO DE NOME v1 (espelhado Python↔JS — ver nome-regras.js
# e os fixtures de testes/contrato-nome.json). Os dois runtimes precisam
# concordar CASO A CASO; o contrato de testes falha se divergirem.
# ---------------------------------------------------------------------------

# Prefixo de PAPEL (e rótulo de benefício colado no começo) no início do valor.
# HOJE o parse_ficha não tirava nada ("MENOR MATHEUS…" virava nome de cliente
# com o prefixo — PONTO 1) e o extract-data.js só tirava MENOR.
# \b no fim de cada alternativa: 'AUXILIADORA MARIA' NÃO casa ('AUX' só com
# separador/espaço depois), 'AUX : MARIA' casa.
RX_PAPEL_NO_INICIO = re.compile(
    r"^(?:nome(?:\s+do)?(?:\s+clientes?)?|menor(?:\s+imp[uú]bere)?|m[ãa]e|pai|clientes?|gestante|titular|"
    r"resp(?:ons[aá]vel)?|segurada|benefici[áa]rio|"
    r"loas|bpc|aux(?:[íi]lio)?|sal[áa]rio|aposentadoria|pens[ãa]o|"
    r"bolsa(?:\s+fam(?:[íi]lia)?)?|maternidade|doen[çc]a|acidente|invalidez|"
    r"rural|idade|benef[íi]cio)\b\s*[:：\-]?\s*", re.I)

# CPF colado no FIM do nome ('FULANO FICTICIO DA SILVA CPF 471.903.315-65').
# Texto IDÊNTICO ao que vivia em relatorio.py/triagem_grupo.py — agora só aqui
# (as antigas viraram cascas sobre esta constante).
RX_CPF_NO_FIM = re.compile(r"\s*\**\s*CPF\**\s*[:：]?\s*[\d.\-/]{11,14}\s*$", re.I)

# Token de benefício COLADO NO FIM do nome, descascado em loop (contrato passo 4).
# Casamento de token INTEIRO: 'AUXILIADORA' não casa; 'FULANO BENEFICIO DA SILVA'
# termina em SILVA e não é tocado.
ROTULOS_BENEFICIO_FIM = {
    "AUX", "AUX.", "AUXILIO", "SALARIO", "APOSENTADORIA", "LOAS", "BPC",
    "PENSAO", "BOLSA", "MATERNIDADE", "DOENCA", "ACIDENTE", "INVALIDEZ",
    "RURAL", "IDADE", "BENEFICIO",
}

# Palavras que NÃO são nome (aparecem em linhas '= PALAVRA ...' do cabeçalho).
# Viviam em triagem_grupo.py; o JS (nome-regras.js) passa a portar o MESMO
# conjunto — era o que divergia em 'CLIENTE : NAIARA CRISTINA AUX MATERNIDADE'.
PALAVRAS_NAO_NOME = {
    "CPF", "NIS", "SENHA", "AUX", "RG", "DATA", "CONTRATO", "AUTORIZADO",
    "PASTA", "RESP", "GESTACAO", "GESTANTE", "MENOR", "INDICACAO", "SUA",
    "SE", "SOMENTE", "COMPROVANTE", "FOTO", "FICHA", "DADOS", "NECESSARIOS",
    "NECESSÁRIOS", "RURAL", "IDADE", "NOME", "EMAIL", "CEP", "RUA", "BAIRRO",
    "CIDADE", "ESTADO", "TRABALHA", "BOLSA", "QUANTOS", "ACESSO", "FONE",
    "ENDERECOS", "ENDEREÇOS", "NUMERO", "NÚMERO", "MESES", "SEMANAS",
    "CARTEIRINHA", "PROTOCOLO", "NOTA", "FISCAL", "OBS", "ENVIAR",
    "CLIENTE", "CLIENTES", "CONTATO", "CONTATOS", "TELEFONE", "TELEFONES",
    "PROFISSAO", "PROFISSÃO", "EMAILS", "COMPARTILHADO", "LEMBRESE",
    # PONTO 2.6 (14/10/2026): rótulos de BENEFÍCIO viravam nome de cliente
    # ('= PENSÃO POR MORTE', '= BPC/LOAS'). AUX/BOLSA/RURAL/IDADE já estavam.
    # BENEFICIO NÃO entra aqui: o teste de "palavra no meio" mataria
    # 'FULANO BENEFICIO DA SILVA' (nome real) — quem filtra o rótulo solto é o
    # RE_BENEFICIO, ancorado no começo do valor.
    "LOAS", "BPC", "APOSENTADORIA", "AUXILIO", "AUXÍLIO", "SALARIO",
    "SALÁRIO", "PENSAO", "PENSÃO", "MATERNIDADE", "DOENCA", "DOENÇA",
    "ACIDENTE", "INVALIDEZ",
}

# Chave de config compartilhada pelas DUAS pontas (triagem-bitrix/config.json e
# agente-separador/config.json). None no cache = ainda não lida.
_CFG_EXIGIR_COMPLETO: bool | None = None


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(s or ""))
                   if unicodedata.category(c) != "Mn")


def _exigir_completo_cfg() -> bool:
    """Valor padrão de `exigir_completo` = config.json → nome.exigir_completo.

    Chave NOS DOIS config.json (lockstep — o contrato de testes falha se os lados
    divergirem). Default TRUE quando a chave ou o config faltar: é o modo seguro
    (b) — 1 palavra vira NÃO-IDENTIFICADO e vai para REVISÃO com o CPF como
    âncora, nunca dead-letter. Ver o _comentario da chave nos config.
    """
    global _CFG_EXIGIR_COMPLETO
    if _CFG_EXIGIR_COMPLETO is not None:
        return _CFG_EXIGIR_COMPLETO
    try:
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(caminho, encoding="utf-8") as fh:
            cfg = json.load(fh)
        _CFG_EXIGIR_COMPLETO = bool((cfg.get("nome") or {}).get("exigir_completo", True))
    except Exception:
        _CFG_EXIGIR_COMPLETO = True
    return _CFG_EXIGIR_COMPLETO


def colapsar_bordas(s: str) -> str:
    """Passo 1a do contrato: trim + colapsar espaços + marcadores de borda
    (* • - _). Não mexe em acento nem no miolo do valor."""
    v = re.sub(r"^[*•_\-\s]+", "", str(s or ""))
    v = re.sub(r"[*•_\-\s]+$", "", v)
    return " ".join(v.split())


def _limpar_bordas(s: str) -> str:
    """Passo 1 do contrato: colapsar_bordas + de-acentar (forma canônica sem acento)."""
    return sem_acento(colapsar_bordas(s))


def normalizar_nome_candidato(valor) -> str:
    """CAMADA 1 do CONTRATO DE NORMALIZAÇÃO v1 → str canônico ('' = nada aproveitável).

    Ordem FIXA (igual em nome-regras.js normalizarNomeCandidato):
      1) trim + colapsar espaços + marcadores de borda + de-acentar;
      2) strip de PREFIXO DE PAPEL / rótulo de benefício no início;
      3) strip de CPF colado no FIM (RX_CPF_NO_FIM);
      4) descarte de RÓTULO DE BENEFÍCIO colado no fim, em loop (token inteiro);
      5) janela de tamanho 5..80 (a mesma de parece_ruido — o mínimo de 5 é o que
         segura as 2 falhas protegidas de --testar-sinais e NÃO pode mudar).

    O descascado do passo 4 só é aceito se o resto não for ruído; senão volta ''
    (NUNCA cai de volta para a forma suja). A forma final também passa pelo
    mesmo crivo: ruído → '' (ex.: 'MÃE RESPONSAVEL: PATRICIA', 'Solteira').
    """
    v = _limpar_bordas(valor)
    if not v:
        return ""
    v = RX_PAPEL_NO_INICIO.sub("", v).strip()
    v = RX_CPF_NO_FIM.sub("", v).strip()
    while True:
        tokens = [p for p in v.split() if p]
        if not tokens:
            return ""
        ultimo = sem_acento(tokens[-1]).strip("*:.,").upper()
        if ultimo in ROTULOS_BENEFICIO_FIM:
            tokens.pop()
            v = " ".join(tokens)
            if not v:
                return ""
        else:
            break
    if not (5 <= len(v) <= 80):
        return ""
    if parece_ruido(v, exigir_completo=False):
        return ""
    return v


def validar_nome(valor_canonico, exigir_completo: bool | None = None) -> bool:
    """CAMADA 2 do CONTRATO: o canônico pode virar nome de CLIENTE?

    1) tamanho 5..80; 2) nº de palavras >= 2 se exigir_completo, senão >= 1;
    3) parece_ruido NA ORDEM Python (tamanho -> palavras -> REs, com
    CONVERSA/POSSE_PARENTESCO sobre sem_acento); 4) PALAVRAS_NAO_NOME na 1ª
    palavra (começo alfabético dela) e em qualquer palavra; 5) sem dígitos e
    razão de letras >= 0,75.
    """
    if exigir_completo is None:
        exigir_completo = _exigir_completo_cfg()
    s = str(valor_canonico or "").strip()
    if not s:
        return False
    if not (5 <= len(s) <= 80):
        return False
    if exigir_completo:
        if len([p for p in re.split(r"\s+", s) if p]) < 2:
            return False
    if parece_ruido(s, exigir_completo=exigir_completo):
        return False
    palavras = [p for p in re.split(r"\s+", s) if p]
    if not palavras:
        return False
    # pega só o começo alfabético da 1ª palavra: 'CLIENTE*Lislayne' -> 'CLIENTE'
    m0 = re.match(r"[A-Z]+", sem_acento(palavras[0]).upper())
    if not m0 or m0.group(0) in PALAVRAS_NAO_NOME:
        return False
    # rótulo em QUALQUER palavra também desqualifica ("MENOR DE IDADE", "DATA 18/09")
    for p in palavras:
        if sem_acento(p).strip("*:.,").upper() in PALAVRAS_NAO_NOME:
            return False
    if re.search(r"\d", s):                             # "26 SEMANAS", "5 MESES"
        return False
    letras = sum(c.isalpha() or c.isspace() for c in s)
    if letras / max(len(s), 1) < 0.75:
        return False
    return any(c.isalpha() for c in s)


def parece_ruido(valor, exigir_completo: bool | None = None) -> bool:
    """True quando o valor NÃO pode ser nome de cliente.

    `exigir_completo=True` (padrão do painel/ficha, lido de config
    nome.exigir_completo): nome de pessoa tem pelo menos DOIS nomes — é o que
    derruba 'Domestica', 'Certo', 'Solteira', 'Vidabbzao'.
    `exigir_completo=False` (camada de SINAL da triagem: cabeçalho do grupo,
    barreira/janela/C3): aceita UMA palavra ('= MARIA = CPF ...'), desde que não
    seja estado civil/documento/conversa. O sinal continua aceitando 1 palavra
    de propósito — quem impede 1 palavra de virar NOME DE CLIENTE é
    validar_nome na fronteira do job (ver decisao 'exigir_completo').
    `None` lê a chave de config (default True = modo seguro).
    """
    if exigir_completo is None:
        exigir_completo = _exigir_completo_cfg()
    v = str(valor or "").strip()
    if not v:
        return True
    if not (5 <= len(v) <= 80):
        return True
    if exigir_completo:
        if len([p for p in re.split(r"\s+", v) if p]) < 2:
            return True
    if RE_ARQUIVO.search(v):
        return True
    if RE_ESTADO_CIVIL.match(v):
        return True
    if RE_POSSE.match(v):
        return True            # 'Meu namorado', 'Minha mãe', 'Meu marido'
    if RE_PREPOSICAO_INICIO.match(v):
        return True            # 'Da marya', 'Do Matheus', 'Para o cliente'
    if RE_PARENTESCO.match(v):
        return True            # 'Namorado', 'Mãe do cliente'
    if RE_POSSE_PARENTESCO.search(sem_acento(v)):
        return True            # 'Da minha mae', 'nome do meu filho'
    if RE_DOCUMENTO.match(v):
        return True
    if RE_RESPONSAVEL.match(v):
        return True
    if RE_BENEFICIO.match(v):
        return True    # 'PENSÃO POR MORTE', 'AUXÍLIO DOENÇA', 'BPC/LOAS'
    if RE_PONTUACAO.search(v):
        return True
    # a conversa é comparada sem acento ("não"/"nao", "está"/"esta")
    if RE_CONVERSA.search(sem_acento(v)):
        return True
    return False


# ---------------------------------------------------------------------------
# NR-01 — comparação de nomes com PARTÍCULAS e ABREVIAÇÕES (conservadora)
# ---------------------------------------------------------------------------
# "MARIA DE SOUZA" ≡ "MARIA SOUZA" (mesmos nomes, mesma ordem; só a partícula
# muda) e "M. APARECIDA SANTOS" ≡ "MARIA APARECIDA SANTOS" (inicial no lugar de
# um prenome/nome do meio). NÃO é equivalência: sobrenome a menos ("MARIA SOUZA"
# × "MARIA SILVA SOUZA"), ordem trocada, inicial no ÚLTIMO sobrenome, ou menos de
# DOIS nomes por extenso iguais ("J. SILVA" × "JOSE SILVA"). Não é transitiva de
# propósito: "MARIA A. SANTOS" casa com "MARIA ANTONIA SANTOS" e com "MARIA
# APARECIDA SANTOS", mas estes dois NÃO casam entre si — quem usa (casamento por
# nome) tem de exigir candidato ÚNICO. Afrouxar além disso só com 2ª âncora (ID-02).
PARTICULAS_NOME = {"DA", "DE", "DO", "DAS", "DOS", "E"}      # "E." / "D." com ponto = inicial
_RX_INICIAL = re.compile(r"^[A-Z]\.?$")


def tokens_nome(nome) -> list:
    """Tokens comparáveis: sem acento, maiúsculos, sem partículas, "D'ÁVILA" → "DAVILA"."""
    s = sem_acento(colapsar_bordas(nome)).upper()
    s = re.sub(r"[’'`´]", "", s)
    s = re.sub(r"[^A-Z.\s]", " ", s)
    return [t for t in s.split() if t and t not in PARTICULAS_NOME and t.strip(".")]


def chave_comparacao(nome) -> str:
    """Chave sem acento/partículas (para agrupar grafias do MESMO nome completo)."""
    return " ".join(t.rstrip(".") for t in tokens_nome(nome))


def nomes_equivalentes(a, b) -> bool:
    """True quando `a` e `b` são grafias do mesmo nome (partícula/abreviação).

    Regras (todas obrigatórias): mesmo nº de tokens depois de tirar partículas;
    par a par igual OU um deles é inicial ("M"/"M.") da outra palavra; nunca
    inicial dos dois lados; o ÚLTIMO token (sobrenome) por extenso e igual; pelo
    menos DOIS tokens por extenso iguais.
    """
    ta, tb = tokens_nome(a), tokens_nome(b)
    if len(ta) < 2 or len(ta) != len(tb):
        return False
    iguais = 0
    for i, (x, y) in enumerate(zip(ta, tb)):
        ix, iy = bool(_RX_INICIAL.match(x)), bool(_RX_INICIAL.match(y))
        if ix and iy:
            return False
        if ix or iy:
            if i == len(ta) - 1:
                return False                  # sobrenome abreviado: não arrisca
            inicial, palavra = (x, y) if ix else (y, x)
            if palavra[0] != inicial[0]:
                return False
            continue
        if x.rstrip(".") != y.rstrip("."):
            return False
        iguais += 1
    return iguais >= 2
