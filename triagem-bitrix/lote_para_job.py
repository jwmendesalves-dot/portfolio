#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lote_para_job.py — SIMULAÇÃO da FASE 2: converte um LOTE PRONTO do grupo
(estado.json da fase 1, /opt/triagem-bitrix/) na forma de JOB da fila do painel
(/opt/agente-separador/data/fila-jobs.json).

⚠️ ESTE SCRIPT NÃO ESCREVE NADA NA FILA. Ele só MONTA e MOSTRA o job que seria
enfileirado (+ os arquivos que iriam para o staging). Quem grava é a ponte
(`lote_para_fila.py`).

⚠️ A SIMULAÇÃO DAQUI NÃO É A PONTE (F2-09): `main()` pega os lotes `pronto`
sem os filtros da ponte (ponte_criterio, --dias, dedup CPF/sha, limite,
fichinha_sem_vazios) e roda OFFLINE (não consulta o Bitrix: o código da pasta
sai vazio e a senha removida da persistência não é buscada). Para simular
fielmente o que a ponte faria use:
    python3 lote_para_fila.py --simular --todos --dias 7 --limite 4

`montar_job()` (usada pela ponte) é a montagem REAL do job: histórico sem marca
de simulação e `file_id` de cada arquivo preservado para o sidecar do staging.
Em `montar_job`, a rede (código da pasta; senha da mensagem original) só é usada
quando `SEM_REDE` é False e a env `TRIAGEM_SEM_REDE` não está ligada.

SENHA gov.br/INSS (S01): só pode existir no campo que o painel consome — a linha
"NIS/Senha do INSS" da `fichinha` (o extract-data.js do painel lê dali e grava no
contato/planilha). Ela NÃO vai para `variaveis_contrato`, avisos, prints, --json
nem nomes de arquivo. Quando a ficha persistida traz "[senha removida]", a senha é
relida da mensagem original (bitrix_cliente, leitura, só com rede); sem isso a linha
sai sem senha ("" ou "<nis> / S/SENHA" — o painel trata como sem senha e preserva a
que já existe na planilha) + aviso.

Forma do job (inspecionada na fila real em 21/09/2026):
  id, upload_id, fichinha, status, etapa, cliente{nome,cpf}, passos, resultado,
  tentativas, lastError, historico[], criado_em, atualizado_em
  - upload_id aponta para o staging: /opt/agente-separador/data/staging/<upload_id>/

A "fichinha" da fila é a FICHA PREENCHIDA no layout do painel
("• **Nome completo:**X", "• **CPF:**...", "• **De quantos meses você está?**N").
O grupo manda a mesma informação em 3 formatos diferentes; aqui eles são
traduzidos para o layout do painel, e o campo "avisos" diz o que não veio.

Uso:
  python3 lote_para_job.py                       # todos os prontos de HOJE
  python3 lote_para_job.py --lote LOTE-SIG-...   # um lote específico
  python3 lote_para_job.py --dia 2026-09-21      # prontos de um dia
  python3 lote_para_job.py --todos-prontos       # todos os prontos do estado
  python3 lote_para_job.py --json                # despeja os jobs em JSON
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
import uuid

import caminhos
import cpf_util

BASE_DIR = os.environ.get("TRIAGEM_DIR") or caminhos.BASE
ARQ_ESTADO = os.path.join(BASE_DIR, "estado.json")
STAGING_RAIZ = os.environ.get("STAGING_DIR") or caminhos.em_separador("data", "staging")

# Rede em montar_job (código da pasta + senha da mensagem original). O main()
# desta simulação liga SEM_REDE; a env TRIAGEM_SEM_REDE=1 também desliga.
SEM_REDE = False
SENHA_REMOVIDA = "[senha removida]"          # = seguranca.SENHA_REMOVIDA
# valores que NÃO são senha utilizável (placeholder, máscara, marcador de remoção)
_SENHAS_PLACEHOLDER = {SENHA_REMOVIDA, "***", "s/senha", "sem senha", "[informada]", ""}


def _sem_rede() -> bool:
    return SEM_REDE or os.environ.get("TRIAGEM_SEM_REDE", "").strip().lower() in ("1", "true", "sim", "yes")


def _senha_utilizavel(valor) -> bool:
    v = str(valor or "").strip()
    return bool(v) and v.lower() not in _SENHAS_PLACEHOLDER and set(v) != {"*"} \
        and SENHA_REMOVIDA not in v.lower()

# --------------------------------------------------------------------------
# Layout da ficha do PAINEL (copiado de um job real da fila, 21/09/2026)
# --------------------------------------------------------------------------
FICHA_PAINEL = """🌸 **DOCUMENTAÇÃO E DADOS PARA O {titulo_caps}** 🌸

Olá! Para darmos início ao seu pedido de {titulo}, por favor, preencha as informações abaixo:

👤 **DADOS PESSOAIS**
• **Nome completo:**{nome}
• **Idade:**{idade}
• **RG:**{rg}
• **CPF:**{cpf}
• **NIS/Senha do INSS:**{nis_senha}
• **Estado Civil:**{estado_civil}
• **E-mail:**{email}
{pasta_linha}
📍 **ENDEREÇO** {endereco_livre}
• **Rua/Endereço:**{rua}
• **Número:**{numero}
• **Bairro:**{bairro}
• **Cidade:**{cidade}
• **Estado:**{estado}
• **CEP:**{cep}

💼 **SITUAÇÃO TRABALHISTA E BENEFÍCIOS**
• **Trabalha atualmente?** ({trabalha_sim} ) Sim  ({trabalha_nao} ) Não
• **Recebe Bolsa Família?** ({bolsa_sim} ) Sim  ({bolsa_nao} ) Não
{linha_servico}
{bloco_gestacao}
📞 **TELEFONES PARA CONTATO**
{linhas_telefone}

---

⚠️ *Favor enviar também fotos legíveis do RG/CPF (ou CNH){doc_extra} e do comprovante de endereço atualizado.*
"""

CAMPOS_FICHA = ["nome", "idade", "rg", "cpf", "estado_civil", "email", "senha",
                "endereco_livre", "rua", "numero", "bairro", "cidade", "estado",
                "cep", "trabalha_sim", "trabalha_nao", "bolsa_sim", "bolsa_nao",
                "data_previsao_parto", "meses", "fone1", "fone2", "fone3"]


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def _chave(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _sem_acento(s).lower()).strip()


# --------------------------------------------------------------------------
# Parser dos 3 formatos de ficha do GRUPO
# --------------------------------------------------------------------------
# A) "*DADOS NECESSÁRIOS PARA DAR A ENTRADA* / FICHA DE ATENDIMENTO* / Nome: ..."
# B) "📋 DADOS NECESSÁRIOS ... 📝 Ficha de Atendimento / Nome completo: ..."
# C) cabeçalho curto do grupo: "= NOME", "= CPF x", "= 8 SEMANAS", "DATA dd/mm/aaaa"
ROTULOS = {
    "nome": ["nome completo", "nome da cliente", "nome"],
    "idade": ["idade"],
    "rg": ["rg", "identidade"],
    "cpf": ["cpf"],
    "nis": ["nis nit pasep", "numero do nis", "nis", "nit", "pasep"],
    "estado civil": ["estado civil"],
    "email": ["e mail", "email"],
    "senha": ["acesso ao govbr senha", "acesso ao gov br cpf e senha", "senha gov br", "senha"],
    "endereco": ["endereços", "endereco"],
    "rua": ["rua endereco", "rua", "endereços rua", "logradouro"],
    "numero": ["numero", "nº", "n"],
    "bairro": ["bairro"],
    "cidade": ["cidade", "municipio"],
    "estado uf": ["estado", "uf"],
    "cep": ["cep"],
    "trabalha": ["trabalha de carteira assinada", "trabalha atualmente", "trabalha"],
    "bolsa": ["recebe bolsa familia", "bolsa familia", "bolsa"],
    # "Data prevista pro parto" (informal, 21/09), "Data provável do parto", "DPP"
    "parto": ["data prevista pro parto", "data prevista do parto", "data provavel do parto",
              "data provavel que o bebe vai nascer", "data prevista parto", "dpp"],
    "semanas": ["quantidade de semanas", "quantas semanas", "semanas de gestacao", "semanas"],
    "meses": ["quantos meses", "de quantos meses voce esta", "meses de gestacao", "meses"],
    "profissao": ["profissao", "ocupacao"],
    "fone": ["fone", "telefone 1 whatsapp", "telefone 1", "telefone 2", "telefone 3",
             "contatos reservas", "contato reserva", "contato", "contato 1", "contato 2"],
}
# rótulos que NUNCA são campo do cliente (aparecem no corpo das fichinhas)
BLOQUEADOS = ["nome da mae", "nome do bebe", "nome do pai", "mae", "bebe", "testemunha",
              "medico", "acompanhante", "enviar", "envie", "favor", "caso tenha",
              "seu ou de", "alguem proximo", "opcional", "observacao", "duvida"]
# trechos que invalidam a LINHA inteira (texto de instrução da própria fichinha)
BLOQUEADOS_LINHA = ["caso tenha", "seu ou de", "alguem proximo", "nome da mae",
                    "nome do bebe", "testemunha", "acompanhante", "favor enviar",
                    "envie tambem", "opcional", "se tiver"]
RX_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
RX_FONE = re.compile(r"(?:\+?55\s*)?\(?\d{2}\)?\s*9?\d{4}[\s.-]?\d{4}\b")
RX_LINHA_FONE = re.compile(r"telefone|fone|contato|celular|zap|whats", re.I)
# separadores aceitos: ":", "?" (o grupo escreve "Trabalha de Carteira Assinada?nao")
RX_LINHA_ROTULO = re.compile(r"^\s*[*•\-\s]*(?P<rot>[^:：?\n]{2,60}?)\s*[:：?]\s*(?P<val>.*)$")
# rótulo SEM separador: "Estado civil solteira", "Semanas 26 semanas"
RX_LINHA_SEM_SEP = re.compile(r"^\s*[*•\-\s]*(?P<rot>[A-Za-zÀ-ÿ][^:：?\n]{2,40}?)\s+(?P<val>\S.*)$")
# emoji/símbolo no meio da linha ("Semanas💘26 semanas") — vira espaço, não some
RX_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u2764\u2b50\u2705]")


def _campo_do_rotulo(rot: str, exato_apenas: bool = False) -> str | None:
    """Casa o rótulo com um campo. Ordem: exato → prefixo (>=4 letras).

    NÃO existe casamento "contido": medido nos lotes reais, isso pegava texto de
    instrução da fichinha ("...de e-mail (seu ou de alguém próximo)...") e criava
    campo com valor-lixo.
    """
    if any(b in rot for b in BLOQUEADOS):
        return None
    for campo, nomes in ROTULOS.items():
        if rot in nomes:
            return campo
    if exato_apenas:
        return None
    for campo, nomes in ROTULOS.items():
        for nome in nomes:
            if len(nome) >= 4 and rot.startswith(nome):
                return campo
    return None


def _split_sem_separador(ln: str) -> tuple[str | None, str | None]:
    """Linha sem separador ("Estado civil solteira", "Semanas 26 semanas").

    Testa o prefixo MAIOR primeiro e exige casamento EXATO — senão "Estado civil"
    seria lido como "Estado" (UF) e o valor sairia "civil* Solteira".
    """
    palavras = ln.split()
    for n in range(min(6, len(palavras) - 1), 0, -1):
        campo = _campo_do_rotulo(_chave(" ".join(palavras[:n])), exato_apenas=True)
        if campo:
            return campo, " ".join(palavras[n:])
    return None, None



def parse_ficha(texto: str) -> dict:
    """Extrai os campos do texto da ficha do grupo (formatos A/B/C)."""
    campos: dict = {}
    telefones: list[str] = []
    t = (texto or "").replace("\r", "")
    # tira o prefixo '[B]Nome:[/B]' do conector
    t = re.sub(r"^\[B\].*?\[/B\]\s*", "", t, count=1, flags=re.S)
    t = t.replace("[B]", "").replace("[/B]", "")
    # cabeçalho de mensagem ENCAMINHADA do WhatsApp: "[26/10, 10:57] Samyra: ..."
    # fica grudado na linha do campo e engolia o "Nome:" (caso Samyra). Remove
    # só o prefixo "[data, hora] Remetente:" — o resto (o campo) permanece.
    t = re.sub(r"^\[\d{1,2}/\d{1,2}(?:/\d{2,4})?,?\s*\d{1,2}:\d{2}(?::\d{2})?\]\s*[^:\n]*:\s*",
               "", t, count=1, flags=re.S)
    linhas = [ln.strip() for ln in t.splitlines()]

    for idx, ln in enumerate(linhas):
        if not ln:
            continue
        # emoji no meio da linha vira espaço ("Semanas💘26 semanas")
        ln = RX_EMOJI.sub(" ", ln).strip()
        # C) cabeçalho curto "= CAMPO"
        m = re.match(r"^=\s*(.+)$", ln)
        if m:
            corpo = m.group(1).strip()
            mcpf = re.search(r"CPF\D*([\d.\-/]{11,14})", corpo, re.I)
            if mcpf:
                campos.setdefault("cpf", mcpf.group(1))
                continue
            msem = re.search(r"(\d{1,2})\s*(SEMANAS?|MESES?|MÊS)", corpo, re.I)
            if msem:
                n = int(msem.group(1))
                campos.setdefault("semanas" if "sem" in _chave(msem.group(2)) else "meses_diretos", n)
                continue
            if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", corpo):
                campos.setdefault("parto", corpo)
                continue
            if _chave(corpo):
                if re.match(r"^(AUX|SALARIO|BPC|LOAS|APOSENTADORIA|BENEFICIO)", _sem_acento(corpo).upper()):
                    campos.setdefault("beneficio", corpo)
                elif re.match(r"^(NIS|PIS|NIT|PASEP)", _sem_acento(corpo).upper()):
                    campos.setdefault("nis", corpo.split(None, 1)[-1] if " " in corpo else "")
                elif re.match(r"^(INDICA|CAPTADOR)", _sem_acento(corpo).upper()):
                    campos.setdefault("indicacao", corpo.split(":", 1)[-1].strip())
                elif re.match(r"^(SENHA|ACESSO)", _sem_acento(corpo).upper()):
                    # "=SENHA: <senha>." / "=SENHA <senha>" /
                    # "=ACESSO AO GOV.BR: senha". Sem este ramo a senha caía no
                    # `continue` e o contato nascia com "S/SENHA" (caso Júlio).
                    v = corpo.split(":", 1)[-1] if ":" in corpo else (
                        corpo.split(None, 1)[-1] if " " in corpo else "")
                    v = v.strip().strip("* ")
                    if v:
                        campos.setdefault("senha", v)
                elif re.match(r"^[A-ZÀ-Ÿ][A-ZÀ-Ÿ\s.'-]{5,}$", _sem_acento(corpo).upper()) and \
                        not re.search(r"\d", corpo) and "NECESS" not in _sem_acento(corpo).upper():
                    # CONTRATO v1: o corpo NÃO é gravado cru — prefixo de papel
                    # ('MENOR X'…) e CPF colado no fim saem aqui (PONTO 1).
                    from nome_regras import normalizar_nome_candidato
                    campos.setdefault("nome", normalizar_nome_candidato(corpo))
                continue
        # A/B) "Rótulo: valor" (aceita "?") ou "Rótulo valor" (sem separador)
        # E-MAIL manda antes de qualquer filtro: a linha costuma vir como
        # "• Endereço de e-mail (seu ou de alguém próximo) fulano@example.com"
        achou_email = RX_EMAIL.search(ln)
        if achou_email:
            campos.setdefault("email", achou_email.group(0))
            continue
        linha_chave = _chave(ln)
        if any(b in linha_chave for b in BLOQUEADOS_LINHA):
            continue
        # linha de telefone: pega TODOS os números da linha ("2 números ... (992073447) (993908521)")
        if RX_LINHA_FONE.search(linha_chave):
            for f in RX_FONE.findall(ln):
                if f not in telefones:
                    telefones.append(f.strip())
            if telefones:
                continue
        m = RX_LINHA_ROTULO.match(ln)
        if m:
            campo = _campo_do_rotulo(_chave(m.group("rot")))
            val = re.sub(r"^[*•\s]+", "", (m.group("val") or "").strip()).strip("* ")
        else:
            campo, val = _split_sem_separador(ln)
            val = (val or "").strip().strip("* ")
        if not val:
            # "• **Nome completo:" com o VALOR na linha de baixo (template do grupo
            # 10/10/2026, lote LOTE-SIG-20261010-1757 — era aqui que o nome sumia).
            # Só aceita a próxima linha se ela NÃO for outro rótulo ("• **CPF:**…").
            if campo == "nome":
                for prox in linhas[idx + 1:]:
                    cand = re.sub(r"^[*•\s]+", "", prox.strip()).strip("* ")
                    if not cand:
                        continue
                    if re.search(r"[:：?]", cand) or RX_EMAIL.search(cand) or RX_FONE.search(cand):
                        break
                    val = cand
                    break
            if not val:
                continue
        # o VALOR manda quando o rótulo é ambíguo:
        #   "Endereço de e-mail (...) fulano@example.com" → é E-MAIL, não endereço
        if RX_EMAIL.search(val):
            campo = "email"
        elif campo == "numero" and re.search(r"\b(nis|nit|pasep)\b", linha_chave):
            campo = "nis"
        if not campo:
            continue
        if campo == "fone":
            for f in RX_FONE.findall(val) or [val]:
                if f not in telefones:
                    telefones.append(f.strip())
        elif campo in ("semanas", "meses"):
            n = re.search(r"(\d{1,2})", val)
            if n:
                # semanas: guarda o NÚMERO (o painel calcula meses e DPP a partir dele)
                campos.setdefault("semanas" if campo == "semanas" else "meses_diretos", int(n.group(1)))
        else:
            if campo == "senha" and "," in val:
                # "🔐Senha de acesso ao GOV.BR,* <senha>" → só o valor depois da vírgula
                val = val.rsplit(",", 1)[-1].strip().strip("* ")
            if campo == "nome":
                # CONTRATO v1: prefixo de papel / CPF colado no fim saem na fronteira
                # (PONTO 1) — 'MENOR MATHEUS…' e 'FULANO … CPF <cpf>' não nascem sujos.
                from nome_regras import normalizar_nome_candidato
                val = normalizar_nome_candidato(val)
            campos.setdefault(campo, val)
    if telefones:
        campos["telefones"] = telefones
    # "DATA 21/09/2026" solto no fim do cabeçalho curto
    if "parto" not in campos:
        m = re.search(r"DATA\s*[:\s]\s*(\d{1,2}/\d{1,2}/\d{4})", _sem_acento(t), re.I)
        if m:
            campos["data_mensagem"] = m.group(1)
    return campos


def semanas_para_meses(semanas: int) -> int:
    return max(1, round(semanas / 4.345))


def cpf_formatado(cpf: str) -> str:
    d = re.sub(r"\D", "", cpf or "")
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else (cpf or "")


# Serviço contratado (texto que vem da tabela) -> (título do formulário, é gestação?)
ROTULOS_SERVICO = (
    (("maternidade",), "Salário-Maternidade", True),
    (("doenca", "acidente"), "Auxílio-Doença e Auxílio-Acidente", False),
    (("doenca", "facultativo"), "Auxílio-Doença Facultativo", False),
    (("doenca", "mei"), "Auxílio-Doença (MEI)", False),
    (("doenca",), "Auxílio-Doença", False),
    (("acidente",), "Auxílio-Acidente", False),
    (("loas",), "Benefício Assistencial (LOAS/BPC)", False),
    (("bpc",), "Benefício Assistencial (LOAS/BPC)", False),
    (("pensao",), "Pensão por Morte", False),
    (("invalidez",), "Aposentadoria por Invalidez", False),
    (("rural",), "Aposentadoria Rural", False),
    (("aposentadoria",), "Aposentadoria", False),
)


def _rotulo_servico(servico: str) -> tuple[str, bool]:
    """Título do formulário e se é caso de gestação.

    Antes o cabeçalho dizia "SALÁRIO-MATERNIDADE" para TODO cliente — inclusive
    auxílio-doença — e a ficha pedia dados de gestação para quem não era gestante.
    """
    chave = _sem_acento_minusculo(servico or "")
    if not chave:
        return "Benefício Previdenciário", False
    for palavras, rotulo, gestacao in ROTULOS_SERVICO:
        if all(p in chave for p in palavras):
            return rotulo, gestacao
    return "Benefício Previdenciário", False


def _nis_e_senha(nis, senha) -> str:
    """NIS e senha do INSS numa linha só (decisão do dono, 04/11/2026).

    Formato: "<nis> / <senha>" — o separador (extract-data.js) sabe ler os dois
    de volta dessa mesma linha; se um dos dois faltar, vira "SENHA DO INSS" / "s/senha".
    """
    n = str(nis or "").strip()
    # "[senha removida]" (persistência sem senha — S01), "***" e afins NUNCA vão
    # ao painel: ele gravaria o marcador como senha no contato/planilha.
    s = str(senha or "").strip() if _senha_utilizavel(senha) else ""
    if not n and not s:
        return ""
    return f"{n or 'S/SENHA'} / {s or 'S/SENHA'}"


def _cpf_formatado(cpf: str) -> str:
    d = re.sub(r"\D", "", str(cpf or ""))
    if len(d) != 11:
        return ""
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


_CACHE_PASTA: dict[str, str] = {}


def codigo_pasta_do_cliente(cpf: str) -> str:
    """Código ATUALIZADO da pasta (planilha PASTAS PREVI GERAL, coluna A).

    Lê do contato no Bitrix (campo "Pasta Previ", UF_CRM_1726587995894) — é o mesmo
    código que a criação da pasta grava ("2188 P", "8502 P"...). Cliente novo = "".

    Consulta pelo cliente REST único (`bitrix_cliente.listar`: JSON no corpo,
    limitador, disjuntor, todas as páginas). CPF com DV inválido não consulta.
    Sem rede (SEM_REDE / TRIAGEM_SEM_REDE), sem webhook, disjuntor aberto ou
    qualquer falha → "" (a montagem do job NUNCA cai por causa da pasta — ID-09).
    Falha não entra no cache: o próximo lote da rodada tenta de novo.
    """
    if not cpf_util.valido(cpf):
        return ""
    cpf_fmt = cpf_util.formatar(cpf)
    if cpf_fmt in _CACHE_PASTA:          # mesma rodada, mesmo cliente
        return _CACHE_PASTA[cpf_fmt]
    if _sem_rede():
        return ""
    try:
        import bitrix_cliente  # noqa: PLC0415  (import tardio: nada de efeito na importação)
        contatos = bitrix_cliente.listar(
            "crm.contact.list",
            {"filter": {"UF_CRM_1524762221558": cpf_fmt}, "select": ["ID", "UF_CRM_1726587995894"]})
    except BaseException as e:  # noqa: BLE001  (SegredoAusente, FalhaBitrix, disjuntor, SystemExit legado)
        if isinstance(e, KeyboardInterrupt):
            raise
        return ""
    contatos = [c for c in (contatos or []) if isinstance(c, dict)]
    if not contatos:
        _CACHE_PASTA[cpf_fmt] = ""
        return ""
    # CPF duplicado no CRM é comum (2 contatos, mesmo cliente): aceita quando TODOS
    # apontam para o mesmo código; se divergirem, não arrisca.
    codigos = set()
    for c in contatos:
        codigo = c.get("UF_CRM_1726587995894") or ""
        if isinstance(codigo, list):
            codigo = codigo[0] if codigo else ""
        codigo = str(codigo).strip()
        if codigo:
            codigos.add(codigo)
    codigo_final = codigos.pop() if len(codigos) == 1 else ""
    _CACHE_PASTA[cpf_fmt] = codigo_final
    return codigo_final


def _linhas_de_telefone(fones: list) -> str:
    """Linhas de telefone da fichinha: no máximo DOIS, sem repetir o mesmo número.

    O ZapSign repete o número em "celular" e "trabalho"; antes a fichinha saía com
    "Telefone 1" e "Telefone 2" idênticos e uma terceira linha vazia.
    """
    vistos, unicos = set(), []
    for f in fones:
        digitos = re.sub(r"\D", "", str(f or ""))
        if not digitos or digitos in vistos:
            continue
        vistos.add(digitos)
        unicos.append(str(f).strip())
    if not unicos:
        return "• **Telefone 1 (WhatsApp):**"
    rotulos = ["• **Telefone 1 (WhatsApp):**", "• **Telefone 2:**"]
    return "\n".join(f"{rotulos[i]}{fone}" for i, fone in enumerate(unicos[:2]))


def montar_fichinha_painel(lote: dict, campos: dict) -> str:
    """Traduz o que o grupo mandou para o LAYOUT do painel (rótulo por rótulo)."""
    titulo_servico, eh_gestacao = _rotulo_servico(
        campos.get("servico_contratado") or lote.get("servico_contratado") or "")
    trabalha = _chave(campos.get("trabalha", ""))
    bolsa = _chave(campos.get("bolsa", ""))
    semanas = campos.get("semanas")
    meses = campos.get("meses")
    if not meses and semanas:
        # ⚠️ NÃO escrever "6 (de 26 semanas)": o painel lê o PRIMEIRO número do campo e
        # entenderia 6 SEMANAS (ele calcula meses e DPP a partir das semanas:
        # DPP = hoje + (40 − semanas)×7, e "meses" = semanas/4,345). Então vão as
        # semanas explícitas e o painel faz a conta.
        try:
            meses = f"{int(str(semanas).strip().split()[0])} semanas"
        except (TypeError, ValueError):
            meses = ""      # valor não numérico nunca derruba a montagem da ficha
    elif not meses and campos.get("meses_diretos"):
        meses = f"{int(campos['meses_diretos'])} meses"
    # DPP: o painel só converte data numérica (toIsoDate). Texto solto ("21 de dezembro")
    # não vira data e sujaria a ficha — nesse caso quem manda é a idade gestacional.
    parto = (campos.get("parto") or "").strip()
    if parto and not re.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$", parto):
        parto = ""
    return FICHA_PAINEL.format(
        nome=campos.get("nome") or lote.get("nome") or "",
        idade=campos.get("idade", ""),
        rg=campos.get("rg", ""),
        cpf=cpf_formatado(campos.get("cpf") or lote.get("cpf") or ""),
        nis_senha=_nis_e_senha(campos.get("nis"), campos.get("senha")),
        pasta_linha=(f"• **Pasta:** {campos['pasta']}\n" if campos.get("pasta") else ""),
        estado_civil=campos.get("estado civil", ""),
        email=campos.get("email", ""),
        senha=campos.get("senha", ""),
        endereco_livre=campos.get("endereco", ""),
        rua=campos.get("rua", ""),
        numero=campos.get("numero", ""),
        bairro=campos.get("bairro", ""),
        cidade=campos.get("cidade", ""),
        estado=campos.get("estado uf", ""),
        cep=campos.get("cep", ""),
        trabalha_sim="x" if "sim" in trabalha and "nao" not in trabalha else " ",
        trabalha_nao="x" if "nao" in trabalha else " ",
        bolsa_sim="x" if "sim" in bolsa and "nao" not in bolsa else " ",
        bolsa_nao="x" if "nao" in bolsa else " ",
        data_previsao_parto=parto,
        meses=meses or "",
        linhas_telefone=_linhas_de_telefone(campos.get("telefones") or []),
        titulo_caps=titulo_servico.upper(),
        titulo=titulo_servico,
        bloco_gestacao=(
            "👶 **DADOS DA GESTAÇÃO / BEBÊ**\n"
            f"• **Data provável do parto:**{parto}\n"
            f"• **De quantos meses você está?**{meses or ''}\n\n"
        ) if eh_gestacao else "",
        doc_extra=", do Cartão da Gestante ou exame" if eh_gestacao else "", 
        linha_servico=(f"• **Serviço contratado:** {campos['servico_contratado']}\n"
                       if campos.get("servico_contratado") else ""),
    )


# --------------------------------------------------------------------------
# Lote pronto -> job
# --------------------------------------------------------------------------
def achar_ficha_em_texto(lote: dict) -> tuple[str, str]:
    """Devolve (texto_da_ficha, origem). Procura a ficha em texto DENTRO do lote."""
    melhor, origem = "", "nenhuma"
    for m in lote.get("mensagens") or []:
        txt = m.get("texto") or ""
        if not txt:
            continue
        if "FICHA DE ATENDIMENTO" in _sem_acento(txt).upper() or \
           "DADOS NECESSARIOS PARA DAR A ENTRADA" in _sem_acento(txt).upper() or \
           "DADOS NECESSARIOS PARA DAR ENTRADA" in _sem_acento(txt).upper():
            if len(txt) > len(melhor):
                melhor, origem = txt, f"mensagem {m['id']}"
    if not melhor:      # sem ficha rotulada: usa o texto mais longo do lote como base
        for m in lote.get("mensagens") or []:
            txt = m.get("texto") or ""
            if len(txt) > len(melhor):
                melhor, origem = txt, f"mensagem {m['id']} (cabeçalho curto)"
    return melhor, origem


# --------------------------------------------------------------------------
# PÁGINA DE VARIÁVEIS DO CONTRATO ASSINADO (04/11/2026)
# --------------------------------------------------------------------------
# A última página dos contratos do ZapSign traz a tabela:
#     CAMPOS                 BT VARIAVEL                    ZAPSIGN VARIAVEL
#     Nome                   {Name}                         MARIA FICTICIA DOS TESTES
#     Cidade                 {UfCrm1753903279519} ,         Manaus
#     Senha do MEU INSS      {UfCrm1725391896584}           <senha>
#     Serviço Contratado     {UfCrm1725033408826}           Auxílio Maternidade Facultativo
# Era o único lugar com o dado ESTRUTURADO: o texto do grupo vem solto e trocado
# (senha errada, serviço ausente). A triagem agora lê a tabela e ela vence a ficha.
RE_SERVICO_CONTRATADO = re.compile(
    r"servi[cç]o\s*contratad[oa]\s*[:]?\s*(?:\{\s*ufcrm\s*1725033408826\s*\})?\s*([^\n]{0,160})",
    re.I)

# rótulo da tabela (normalizado, sem acento) -> chave de `campos`
# ordem importa: o mais específico primeiro (casado por prefixo)
MAPA_VARIAVEIS = [
    ("estado civil", "estado civil"),
    ("serviço contratado", "servico_contratado"),
    ("senha do meu inss", "senha"),
    ("previsão de nascimento", "parto"),
    ("semana de gestação", "semanas"),
    ("telefone celular", "tel_celular"),
    ("telefone trabalho", "tel_trabalho"),
    ("telefone outro", "tel_outro"),
    ("% dos honorários", "honorarios"),
    ("número de parcelas", "parcelas"),
    ("nome do menor", "menor_nome"),
    ("cpf do menor", "menor_cpf"),
    ("tipo do menor", "menor_tipo"),
    ("nº da casa", "numero"),
    ("origem do lead", "origem_lead"),
    ("nome", "nome"),
    ("sobrenome", "sobrenome"),
    ("rg", "rg"),
    ("cpf", "cpf"),
    ("rua", "rua"),
    ("bairro", "bairro"),
    ("cidade", "cidade"),
    ("estado", "estado uf"),
    ("cep", "cep"),
    ("e-mail", "email"),
    ("profissão", "profissao"),
    ("nis", "nis"),
]

# o rótulo lido da tabela vem sem acento; normaliza as chaves uma vez
def _sem_acento_minusculo_pre(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKD", s or "")
                  .encode("ascii", "ignore").decode().lower()).strip()


MAPA_VARIAVEIS_NORM = [(_sem_acento_minusculo_pre(r), c) for r, c in MAPA_VARIAVEIS]

# valores que não são dado (a tabela imprime 0 quando o campo ficou vazio)
LIXO_VALOR = {"0", "-", "--", "n/a", "na", "não", "nao", "não informado", "nao informado",
              "n informado", "nenhum", "nenhuma", "não se aplica", "nao se aplica"}


def _sem_acento_minusculo(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def _limpar_valor_tabela(valor: str) -> str:
    """Tira pontuação/espaço sobrando, sem mexer no conteúdo."""
    v = re.sub(r"\{\s*[^}]{0,60}\}", " ", valor or "")   # tags {UfCrm...} não são dado
    v = re.sub(r"[*_]", "", v)
    v = re.sub(r"\s+", " ", v).strip(" .-:;|,")   # parênteses ficam: "(92) 99327-2232", "Solteiro(a)"
    if _sem_acento_minusculo(v) in LIXO_VALOR or len(v) < 2:
        return ""
    return v[:200]


def _limpar_servico(valor: str) -> str:
    """Corta o rabicho da frase, preservando o nome do serviço.

    Exemplos reais que chegam da tabela:
      "Auxílio Maternidade Facultativo, e para isso"   -> "Auxílio Maternidade Facultativo"
      "Auxílio-Doença Facultativo — com"               -> "Auxílio-Doença Facultativo"
      "Auxílio-Doença e Auxílio-Acidente: O(a)"        -> "Auxílio-Doença e Auxílio-Acidente"
    """
    v = _limpar_valor_tabela(valor)
    for sep in (r"\s+[—–]\s+", r":\s*o\(a\)?", r",\s*e\s+para\b", r"\be\s+para\s+isso\b",
                r",\s*e\s*$", r"\bo\(a\)\s+contratante\b", r"%\s*dos",
                r"\bn[uú]mero\s+de\s+parcelas\b"):
        v = re.split(sep, v, flags=re.I)[0]
    v = re.sub(r"[\s,;:.—–-]+$", "", v)
    for rabicho in (" e", " com", " que", " para"):
        if v.lower().endswith(rabicho):
            v = v[: -len(rabicho)].rstrip(" ,;:.—–-")
    return v[:120] if len(v) >= 4 else ""


def _colunas_da_linha(linha: str) -> list[str]:
    """A tabela é alinhada em colunas separadas por 2+ espaços."""
    return [c for c in re.split(r"\s{2,}", linha.rstrip()) if c.strip()]


def _valor_das_colunas(colunas: list[str]) -> str:
    """Percorre as colunas depois do rótulo: joga fora as tags {UfCrm...} e pega o texto."""
    for col in colunas:
        sem_tag = re.sub(r"\{\s*[^}]{0,60}\}", " ", col)
        v = _limpar_valor_tabela(sem_tag)
        v = re.sub(r"^(rg|cpf|cep|nis)\s*[:\-]?\s+", "", v, flags=re.I)  # "RG 000.000..." -> "000.000..."
        if v and not re.match(r"^[\s,;|.()\-–—]*$", v):
            return v
    return ""


def _texto_pdf(caminho: str) -> str:
    """Texto do PDF no layout de colunas (`pdftotext -layout`). Falha → ""."""
    try:
        return subprocess.run(["pdftotext", "-q", "-layout", caminho, "-"],
                              capture_output=True, text=True, timeout=90).stdout or ""
    except Exception:  # noqa: BLE001  (pdftotext ausente/timeout: sem tabela, nunca derruba)
        return ""


def ler_tabela_variaveis(lote: dict) -> dict:
    """Lê a tabela de variáveis do contrato assinado e devolve {campo: valor}.

    Varre TODOS os PDFs do lote: o kit manda 3 arquivos (procuração, contrato e o
    documento do benefício) e só um costuma trazer a tabela preenchida. O primeiro
    valor não vazio de cada campo é o que vale.
    """
    tabela: dict[str, str] = {}
    for a in lote.get("arquivos") or []:
        caminho = a.get("caminho")
        if not caminho or not str(caminho).lower().endswith(".pdf") or not os.path.exists(caminho):
            continue
        for campo, valor in ler_tabela_do_texto(_texto_pdf(caminho)).items():
            tabela.setdefault(campo, valor)
    return tabela


def ler_tabela_do_texto(txt: str) -> dict:
    """Parser da tabela de variáveis sobre o texto (layout) de UM PDF."""
    tabela: dict[str, str] = {}
    if "zapsign variavel" not in _sem_acento_minusculo(txt) and "bt variavel" not in _sem_acento_minusculo(txt):
        return tabela
    linhas = txt.splitlines()
    for i, linha in enumerate(linhas):
        colunas = _colunas_da_linha(linha)
        if len(colunas) < 2:
            continue
        rotulo = _sem_acento_minusculo(colunas[0]).rstrip(":")
        campo = next((c for r, c in MAPA_VARIAVEIS_NORM
                      if rotulo == r or rotulo.startswith(r)), None)
        if not campo:
            continue
        valor = _valor_das_colunas(colunas[1:])
        if not valor and campo == "parto":
            # rótulo quebrado em duas linhas: "Previsão de" / "Nascimento?"
            prox = _colunas_da_linha(linhas[i + 1]) if i + 1 < len(linhas) else []
            if prox and _sem_acento_minusculo(prox[0]).startswith("nascimento"):
                valor = _valor_das_colunas(prox[1:])
        if not valor:
            continue
        if campo == "semanas":
            # o campo é "Semana de Gestação", mas o cliente digita "9 meses" ou
            # até texto solto. Nunca deixar texto aqui: o painel faz int().
            m_sem = re.search(r"(\d{1,2})\s*(?:semanas?|sem\b)", valor, re.I)
            m_mes = re.search(r"(\d{1,2})\s*mes", valor, re.I)
            if m_sem:
                valor = m_sem.group(1)
            elif re.fullmatch(r"\d{1,2}", valor.strip()):
                valor = valor.strip()
            elif m_mes:
                campo, valor = "meses", m_mes.group(1)   # digitou meses no lugar
            else:
                continue
        elif campo in ("parcelas", "honorarios"):
            m_num = re.search(r"\d{1,3}", valor)
            if not m_num:
                continue
            valor = m_num.group(0) + ("%" if campo == "honorarios" else "")
        if campo == "servico_contratado":
            # a frase do serviço continua nas colunas seguintes (sem as tags)
            pedacos = [_limpar_valor_tabela(re.sub(r"\{\s*[^}]{0,60}\}", " ", c))
                       for c in colunas[1:]]
            valor = _limpar_servico(" ".join(x for x in pedacos if x))
        if campo in tabela:
            continue
        tabela[campo] = valor
    return tabela


def servico_contratado_dos_pdfs(lote: dict) -> tuple[str, str]:
    """Compatibilidade: devolve (serviço, arquivo de origem)."""
    servico = ler_tabela_variaveis(lote).get("servico_contratado") or ""
    origem = ""
    if servico:
        for a in lote.get("arquivos") or []:
            if str(a.get("caminho") or "").lower().endswith(".pdf"):
                origem = a.get("nome_salvo") or os.path.basename(a.get("caminho") or "")
                break
    return servico, origem


def aplicar_tabela_nas_variaveis(campos: dict, tabela: dict, avisos: list) -> None:
    """Contrato assinado VENCE a ficha do grupo; divergência vira aviso.

    Motivo: a ficha passa por digitação/transcrição no WhatsApp (senha trocada,
    cidade faltando), enquanto a tabela é o que o cliente conferiu e assinou.
    """
    for campo, valor in tabela.items():
        if not valor:
            continue
        if campo in ("tel_celular", "tel_trabalho", "tel_outro"):
            continue
        atual = campos.get(campo)
        if campo == "semanas":
            valor = re.sub(r"\D", "", valor) or valor      # "9 SEMANAS" -> "9"
        if campo == "parto" and not re.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$", valor):
            continue                                        # data ilegível não entra
        if atual and _sem_acento_minusculo(str(atual)) != _sem_acento_minusculo(valor):
            if campo == "senha":
                # S01: aviso vai para log/estado/painel — NUNCA com o valor da senha.
                if _senha_utilizavel(atual):
                    avisos.append("senha: a ficha do grupo divergia do contrato assinado "
                                  "(usado o do contrato; valores omitidos)")
            else:
                avisos.append(f"{campo}: a ficha do grupo dizia {str(atual)[:40]!r}, "
                              f"o contrato assinado diz {valor[:40]!r} (usado o do contrato)")
        campos[campo] = valor
    # telefones: os da tabela completam/limpam a lista da ficha.
    # 🔴 04/11/2026 — o ZapSign repete o MESMO número em "celular" e "trabalho"
    # (o cliente preenche igual), e a fichinha saía com Telefone 1 e 2 idênticos.
    # Deduplica pelo número em dígitos e fica só com DOIS (decisão do dono).
    da_tabela = [tabela.get(k) for k in ("tel_celular", "tel_trabalho", "tel_outro")]
    vistos_tel = set()
    unicos = []
    for f in da_tabela:
        if not f:
            continue
        digitos = re.sub(r"\D", "", str(f))
        if digitos in vistos_tel:
            continue
        vistos_tel.add(digitos)
        unicos.append(f)
    da_tabela = unicos[:2]
    if da_tabela:
        atuais = [f for f in (campos.get("telefones") or []) if f]
        if _sem_acento_minusculo(" ".join(map(str, atuais))) != _sem_acento_minusculo(" ".join(map(str, da_tabela))):
            if atuais:
                avisos.append(f"telefones: ficha {atuais[:3]} / contrato {da_tabela} (usado o do contrato)")
            campos["telefones"] = da_tabela[:2]


def sha256_de(caminho: str) -> str | None:
    if not caminho or not os.path.exists(caminho):
        return None
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def senha_da_mensagem_original(lote: dict) -> str:
    """S01 — caminho mínimo: relê do Bitrix a mensagem cuja senha foi removida da
    persistência e devolve SÓ a senha (vai apenas para a fichinha do job).

    Leitura via `bitrix_cliente.chamar("im.dialog.messages.get", FIRST_ID=id-1)`.
    Nada é gravado, impresso ou cacheado. Sem rede / sem `dialog_id` / mensagem não
    visível ao webhook (a fonte primária é o VibeCode e o webhook vê um subconjunto)
    / qualquer falha → "".
    """
    if _sem_rede():
        return ""
    dialog = str(lote.get("dialog_id") or "").strip()
    alvos = []
    for m in lote.get("mensagens") or []:
        if SENHA_REMOVIDA in str(m.get("texto") or ""):
            try:
                alvos.append(int(m.get("id")))
            except (TypeError, ValueError):
                continue
    if not dialog or not alvos:
        return ""
    try:
        import bitrix_cliente  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    for mid in alvos:
        try:
            r = bitrix_cliente.chamar("im.dialog.messages.get",
                                      {"DIALOG_ID": dialog, "FIRST_ID": mid - 1, "LIMIT": 1})
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                raise
            return ""
        for msg in ((r or {}).get("result") or {}).get("messages") or []:
            if str(msg.get("id")) != str(mid):
                continue
            senha = parse_ficha(str(msg.get("text") or "")).get("senha")
            if _senha_utilizavel(senha):
                return str(senha).strip()
    return ""


def montar_job(lote: dict, quando: _dt.datetime | None = None) -> dict:
    """Monta o job (mesma forma da fila) + o plano de staging + avisos."""
    quando = quando or _dt.datetime.now(_dt.timezone.utc)
    iso = quando.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    texto, origem_ficha = achar_ficha_em_texto(lote)
    campos = parse_ficha(texto) if texto else {}
    # Sub-lote dividido por kit assinado (dividir_lote_misto.py): as `mensagens` são
    # HERDADAS do lote pai (o script faz `dict(lote)`) e trazem a ficha de OUTRO
    # cliente. CPF e nome do LOTE (kit assinado + CRM) são os autoritativos; o resto
    # da ficha em texto é do cliente errado e deve ser descartado (caso real 2026-10-03:
    # o sub-lote de uma cliente saiu com o nome de OUTRA cliente por causa disso).
    if lote.get("dividido_de"):
        campos = {"nome": lote.get("nome") or "",
                  "cpf": lote.get("cpf_formatado") or lote.get("cpf") or ""}
    # NOME (CONTRATO DE NORMALIZAÇÃO v1 — PONTO 1 + PONTO 2): os DOIS candidatos
    # passam pelo normalizador ANTES de comparar. A FONTE AUTORITATIVA é o
    # `nome` do LOTE (Fase 1 já aplicou a regra MENOR-vence em agrupar): ele
    # vence quando valida; o nome da ficha só entra se o do lote não validar.
    # Nada validando → nome VAZIO + revisão com o CPF como âncora (nunca
    # dead-letter) — 'Vidabbzao'/'janaina'/'MENOR X' não viram mais cliente.
    upload_id = str(uuid.uuid4())
    arquivos, avisos = [], []        # antes de qualquer avisos.append (UnboundLocalError)
    from nome_regras import normalizar_nome_candidato, validar_nome, _exigir_completo_cfg, nomes_equivalentes
    exigir_completo = _exigir_completo_cfg()
    nome_lote = normalizar_nome_candidato(lote.get("nome"))
    nome_ficha = normalizar_nome_candidato(campos.get("nome"))
    if validar_nome(nome_lote, exigir_completo):
        campos["nome"] = nome_lote
        # NR-01: grafia diferente do MESMO nome (partícula/abreviação) não é aviso
        if nome_ficha and nome_ficha != nome_lote and not nomes_equivalentes(nome_ficha, nome_lote):
            avisos.append(f"nome da ficha {nome_ficha!r} não venceu o do lote {nome_lote!r}")
    elif validar_nome(nome_ficha, exigir_completo):
        campos["nome"] = nome_ficha
    else:
        if nome_ficha or nome_lote:
            avisos.append(f"nome descartado (ficha={nome_ficha!r}, lote={nome_lote!r}) "
                          f"— não valida como nome de pessoa (1 palavra/ruído)")
        campos["nome"] = ""
    if not campos.get("cpf"):
        campos["cpf"] = lote.get("cpf_formatado") or lote.get("cpf") or ""
    for a in lote.get("arquivos") or []:
        caminho = a.get("caminho")
        existe = bool(caminho and os.path.exists(caminho))
        arquivos.append({
            "no_lote": a.get("nome_salvo"),
            "origem": a.get("nome_original"),
            # F2-10: sem o file_id o staging nomeava por sha256 e o sidecar saía {"id": ""}
            "file_id": a.get("file_id"),
            "caminho": caminho,
            "existe": existe,
            "tamanho": os.path.getsize(caminho) if existe else a.get("tamanho_bits"),
            "sha256": sha256_de(caminho) if existe else a.get("sha256"),
            "iria_para": os.path.join(STAGING_RAIZ, upload_id, a.get("nome_salvo") or ""),
        })
        if not existe:
            avisos.append(f"arquivo ausente no disco: {a.get('nome_salvo')}")

    if not texto:
        avisos.append("NENHUMA ficha em texto no lote — a ficha só existe dentro do anexo (depende de OCR/leitura do PDF)")
    elif not campos.get("cpf"):
        avisos.append("ficha em texto sem CPF legível")

    # DEDUPE por sha256: o grupo reenvia o MESMO kit (21/09: o lote da Luzia tinha
    # 18 arquivos = 9 pares com o mesmo sha256). Sem dedupe, o staging/IA/NAS
    # recebem o dobro.
    vistos: dict = {}
    for a in arquivos:
        s = a.get("sha256")
        if not s:
            continue
        if s in vistos:
            a["duplicado_de"] = vistos[s]
        else:
            vistos[s] = a["no_lote"]
    dup = [a for a in arquivos if a.get("duplicado_de")]
    if dup:
        avisos.append(f"{len(dup)} arquivo(s) DUPLICADO(S) por sha256 (o mesmo kit foi reenviado) "
                      f"— no staging iria só 1 de cada")
    if not campos.get("nome"):
        avisos.append("nome do cliente NÃO identificado (CPF é a âncora)")
    if not campos.get("parto"):
        avisos.append("data provável do parto não veio (o painel calcula pela idade gestacional)")
    if not (campos.get("telefones") or []) and not campos.get("fone"):
        avisos.append("nenhum telefone na ficha em texto")
    if lote.get("identificacao") != "OK":
        avisos.append(f"lote está como {lote.get('identificacao')} — revisão antes de enfileirar")

    # EMISSÃO: cliente.nome só recebe valor canônico VALIDADO; senão fica vazio
    # + nome_nao_identificado, com o CPF como âncora e a marca de revisão.
    # 💼 SERVIÇO PRESTADO (04/11/2026) — lido da página de variáveis do contrato
    # assinado. Antes a triagem não levava o serviço e o separador adivinhava o
    # benefício. Aqui o valor viaja no job (cliente.beneficio) e na fichinha.
    tabela = ler_tabela_variaveis(lote)
    if tabela:
        aplicar_tabela_nas_variaveis(campos, tabela, avisos)
    servico = campos.get("servico_contratado") or ""
    servico_de = "tabela de variáveis do contrato assinado" if servico else ""
    if not servico:
        avisos.append("serviço contratado não encontrado no PDF (tabela de variáveis vazia ou ausente)")
    if tabela and not _senha_utilizavel(campos.get("senha")):
        avisos.append("senha do MEU INSS não veio na tabela do contrato")
    # S01: a ficha persistida pode trazer "[senha removida]" (estado.json sem senha).
    # Busca a senha na mensagem original só agora, só para a fichinha.
    if campos.get("senha") is not None and not _senha_utilizavel(campos.get("senha")):
        recuperada = senha_da_mensagem_original(lote) \
            if SENHA_REMOVIDA in str(campos.get("senha")) else ""
        if recuperada:
            campos["senha"] = recuperada
        else:
            if SENHA_REMOVIDA in str(campos.get("senha")):
                avisos.append("senha gov.br/INSS removida da persistência e não recuperada — "
                              "fichinha sai com S/SENHA; ver a mensagem original no grupo")
            campos["senha"] = ""

    cliente = {"nome": campos.get("nome") or "", "cpf": cpf_formatado(campos.get("cpf") or "")}
    cliente["beneficio"] = servico
    # 📁 código ATUALIZADO da pasta (cliente que já é do escritório); vazio = cliente novo
    campos["pasta"] = codigo_pasta_do_cliente(campos.get("cpf") or lote.get("cpf") or "")
    cliente["pasta"] = campos["pasta"]
    revisao_nome = None
    if not cliente["nome"]:
        cliente["nome_nao_identificado"] = True
        revisao_nome = {"motivo": "nome_nao_identificado", "cpf": cliente["cpf"]}
    job = {
        "id": str(uuid.uuid4()),
        "upload_id": upload_id,
        "fichinha": montar_fichinha_painel(lote, campos),
        "servico_contratado": servico,
        "servico_de": servico_de,
        # S01: a senha só viaja na fichinha (campo que o painel lê); aqui não.
        "variaveis_contrato": {k: v for k, v in tabela.items() if v and k != "senha"},
        "cliente": cliente,
        "revisao_nome": revisao_nome,
        "status": "pendente",
        "etapa": None,
        "passos": [],
        "resultado": {},
        "tentativas": 0,
        "lastError": None,
        # F2-10: este é o job REAL da ponte; a marca de simulação é posta pelo main()
        "historico": [{"em": iso, "evento": "enfileirado pela ponte do grupo"}],
        "criado_em": iso,
        "atualizado_em": iso,
    }
    return {
        "job": job,
        "origem": {"lote_id": lote["lote_id"], "modo": lote["modo"], "status": lote["status"],
                   "pronto_por": lote.get("pronto_por"), "autores": lote["autores"],
                   "janela": [lote["inicio"], lote["fim"]], "ficha_de": origem_ficha,
                   "qtd_arquivos": lote["qtd_arquivos"]},
        "campos_lidos_da_ficha": campos,
        "arquivos": arquivos,
        "staging_planejado": os.path.join(STAGING_RAIZ, upload_id),
        "avisos": avisos,
    }


def para_exibir(sim: dict) -> dict:
    """Cópia do resultado de montar_job segura para stdout/log/--json (S01).

    Senha fora (campos e fichinha), CPF e demais segredos mascarados por
    `seguranca.mascarar`. Não altera o `sim` original (o job real continua inteiro).
    """
    import copy  # noqa: PLC0415
    import seguranca  # noqa: PLC0415
    s = copy.deepcopy(sim)
    campos = s.get("campos_lidos_da_ficha") or {}
    if campos.get("senha"):
        campos["senha"] = "***"
    for k in ("cpf",):
        if campos.get(k):
            campos[k] = cpf_util.mascarar(campos[k])
    job = s.get("job") or {}
    if job.get("fichinha"):
        job["fichinha"] = seguranca.mascarar(job["fichinha"])
    cli = job.get("cliente") or {}
    if cli.get("cpf"):
        cli["cpf"] = cpf_util.mascarar(cli["cpf"])
    (job.get("variaveis_contrato") or {}).pop("senha", None)
    if isinstance(job.get("variaveis_contrato"), dict) and job["variaveis_contrato"].get("cpf"):
        job["variaveis_contrato"]["cpf"] = cpf_util.mascarar(job["variaveis_contrato"]["cpf"])
    s["avisos"] = [seguranca.mascarar(a) for a in s.get("avisos") or []]
    return s


def imprimir(sim: dict, completo: bool = True) -> None:
    sim = para_exibir(sim)
    j, o = sim["job"], sim["origem"]
    print("=" * 78)
    print(f"LOTE {o['lote_id']}  modo={o['modo']}  status={o['status']}  "
          f"pronto_por={o['pronto_por']}")
    print(f"  autores={o['autores']}   janela={o['janela'][0]} -> {o['janela'][1]}")
    print(f"  ficha lida de: {o['ficha_de']}   arquivos={o['qtd_arquivos']}")
    print("-" * 78)
    print("JOB QUE SERIA ENFILEIRADO (nada foi gravado):")
    print(f"  id          : {j['id']}")
    print(f"  upload_id   : {j['upload_id']}")
    print(f"  status      : {j['status']}")
    print(f"  cliente     : nome={j['cliente']['nome']!r} cpf={j['cliente']['cpf']!r}")
    print(f"  staging     : {sim['staging_planejado']}")
    print(f"  fichinha    : {len(j['fichinha'])} chars (layout do painel)")
    if completo:
        print("-" * 78)
        print(j["fichinha"])
    print("-" * 78)
    print(f"ARQUIVOS ({len(sim['arquivos'])}) que iriam para o staging:")
    for a in sim["arquivos"]:
        marca = f"  ⤫ DUPLICADO de {a['duplicado_de']}" if a.get("duplicado_de") else ""
        print(f"  - {a['no_lote']}  ({a['tamanho']} B) sha256={str(a['sha256'])[:16]}…  "
              f"existe={a['existe']}{marca}")
    if sim["avisos"]:
        print("-" * 78)
        print("AVISOS:")
        for x in sim["avisos"]:
            print(f"  ⚠ {x}")
    print()


def lotes_prontos(est: dict, dia: str | None, todos: bool) -> list[dict]:
    saida = []
    for l in est.get("lotes", {}).values():
        if not l.get("pronto"):
            continue
        if todos or not dia:
            saida.append(l)
        elif str(l.get("inicio") or "").startswith(dia):
            saida.append(l)
    return sorted(saida, key=lambda x: x.get("inicio") or "")


def main(argv=None) -> int:
    global SEM_REDE
    p = argparse.ArgumentParser(
        description="SIMULAÇÃO OFFLINE: lote pronto do grupo -> job da fila (não grava nada, não usa rede).",
        epilog="NÃO aplica os filtros da ponte (ponte_criterio, --dias, dedup, limite) e não consulta o "
               "Bitrix (pasta vazia, senha removida não é buscada). Simulação FIEL da ponte: "
               "python3 lote_para_fila.py --simular --todos --dias 7 --limite 4")
    p.add_argument("--lote", action="append", default=[], help="lote_id (pode repetir)")
    p.add_argument("--dia", default=None, help="prontos com inicio nesse dia (AAAA-MM-DD); default=hoje")
    p.add_argument("--todos-prontos", action="store_true", help="todos os prontos do estado")
    p.add_argument("--json", action="store_true", help="despeja os jobs (JSON, senha/CPF mascarados) em vez do texto")
    p.add_argument("--sem-ficha", action="store_true", help="não imprime o texto da fichinha")
    p.add_argument("--estado", default=ARQ_ESTADO)
    args = p.parse_args(argv)
    anterior, SEM_REDE = SEM_REDE, True     # F2-09: esta simulação nunca chama o Bitrix
    try:
        return _executar_simulacao(args)
    finally:
        SEM_REDE = anterior


def _executar_simulacao(args) -> int:
    if not os.path.exists(args.estado):
        print(f"estado não encontrado: {args.estado}", file=sys.stderr)
        return 2
    import estado_store  # noqa: PLC0415
    est = estado_store.ler_estado(args.estado)

    if args.lote:
        lotes = [est["lotes"][i] for i in args.lote if i in est.get("lotes", {})]
        faltando = [i for i in args.lote if i not in est.get("lotes", {})]
        for f in faltando:
            print(f"lote não encontrado no estado: {f}", file=sys.stderr)
        if not lotes:
            return 2
    else:
        dia = args.dia if args.dia is not None else (
            _dt.datetime.now().date().isoformat() if not args.todos_prontos else None)
        lotes = lotes_prontos(est, dia, args.todos_prontos)
        if not lotes:
            print(f"nenhum lote PRONTO para {('todos' if args.todos_prontos else dia)}")
            return 0
        if not args.json:
            print(f"\n>>> {len(lotes)} lote(s) pronto(s) "
                  f"{'de todo o estado' if args.todos_prontos else 'de ' + str(dia)}\n")
        else:
            print(f">>> {len(lotes)} lote(s) pronto(s)", file=sys.stderr)

    sims = [montar_job(l) for l in lotes]
    for s in sims:      # F2-10: a marca de simulação vive só aqui, nunca no job da ponte
        s["job"]["historico"] = [{"em": s["job"]["criado_em"],
                                  "evento": "enfileirado (SIMULAÇÃO — não gravado)"}]
    if args.json:
        print(json.dumps([para_exibir(s) for s in sims], ensure_ascii=False, indent=2))
    else:
        for s in sims:
            imprimir(s, completo=not args.sem_ficha)
        print("=" * 78)
        print(f"RESUMO: {len(sims)} job(s) SIMULADO(S) — NADA foi gravado na fila.")
        print(f"  cliente identificado: {sum(1 for s in sims if s['job']['cliente']['cpf'])}/"
              f"{len(sims)}   com avisos: {sum(1 for s in sims if s['avisos'])}/{len(sims)}")
        print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
