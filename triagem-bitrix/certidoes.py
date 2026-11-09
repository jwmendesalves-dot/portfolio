#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CERTIDÕES DE NASCIMENTO — projeto do Bitrix → TAREFA vinculada ao negócio do cliente.

Fonte...: chat do projeto "CERTIDAO DE NASCIMENTO" (collab 117 / chat 360263), lido pela
          API do VibeCode (o webhook não vê tudo; histórico paginado além de 50).
Ação....: acha o NEGÓCIO do cliente e cria TAREFA dentro do projeto, vinculada ao negócio,
          com a CERTIDÃO ANEXADA (upload em base64 → ufTaskWebdavFiles).

Identificação do cliente (cascata) — NUNCA "primeiro contato" pelo nome da criança (CE-03):
  1. CPF na legenda (com DV válido) → contato ÚNICO → negócio
  2. nome da MÃE/RESPONSÁVEL na legenda → contato ÚNICO (NAME+LAST_NAME completos,
     busca paginada) → negócio; telefone na legenda vale como 2ª âncora
  3. leitura da certidão (IA do painel) → nome da mãe (nunca o do registrado) → contato → negócio
  0 ou >1 contatos (ambíguo) → NÃO cria tarefa: registra motivo e avisa o projeto

Máquina de estados POR MENSAGEM (estado-certidoes.json, escrita atômica) — CE-01:
  pendente → processando → tarefa_criada | falha_transitoria(n, proxima_em) | falha_permanente
  (também: sem_negocio = concluída com aviso ao humano; ignorada = fora do escopo)
  Só marca tarefa_criada DEPOIS de confirmar a tarefa no Bitrix. Idempotência: antes de
  criar, procura tarefa existente com o marcador [certidao:<chat>:<id>]. Retry SÓ para
  erro transitório (rede_bitrix.classificar); permanente → motivo para humano.

Segurança (S09/S12/S13/CE-05): chamadas do Bitrix via bitrix_cliente (webhook fora do
argv, JSON com timeout); X-Api-Key só para o host VibeCode; temporários e o JS do node
via seguranca.arquivo_temp_privado, apagados com remover_seguro no finally.

Decisões do escritório (21/09/2026): responsável Anna Luisa (65489), prazo 2 dias úteis,
tarefa dentro do projeto + vinculada ao negócio, sem negócio → aviso no projeto.

Uso:
  certidoes.py --status
  certidoes.py --simular                 # mostra o que faria com as certidões novas
  certidoes.py --enviar                  # cria as tarefas de verdade
  certidoes.py --simular --teste /tmp/certidao.pdf --legenda "Certidão — CPF 291.417.776-38"
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import bitrix_cliente
import caminhos
import cpf_util
import estado_store
import rede_bitrix
import seguranca
import tipos_documento
from rede_bitrix import FalhaBitrix

API = "https://vibecode.bitrix24.com/v1"
HOST_VIBECODE = "vibecode.bitrix24.com"          # ÚNICO host que recebe o X-Api-Key (S13)
NODE = os.environ.get("TRIAGEM_NODE") or "/root/.hermes/node/bin/node"

CHAT_PROJETO = "chat360263"       # CERTIDAO DE NASCIMENTO (projeto)
CHAT_GRUPO = "chat100001"         # grupo da triagem (as clientes mandam por aqui também)
CHATS_PADRAO = [CHAT_PROJETO, CHAT_GRUPO]
GRUPO_PROJETO = 117               # id do trabalho/projeto (para a tarefa ficar dentro)
RESPONSAVEL = 65489               # Anna Luisa Gomes
PRAZO_DIAS_UTEIS = 2
ESTADO = caminhos.em_base("estado-certidoes.json")
CPF_FIELDS = ["UF_CRM_1524762221558", "UF_CRM_5ECC0E7079BBE", "UF_CRM_685193D9B13AF"]

# --- máquina de estados POR MENSAGEM (CE-01) --------------------------------
ST_PENDENTE = "pendente"
ST_PROCESSANDO = "processando"
ST_TAREFA_CRIADA = "tarefa_criada"
ST_FALHA_TRANSITORIA = "falha_transitoria"
ST_FALHA_PERMANENTE = "falha_permanente"
ST_SEM_NEGOCIO = "sem_negocio"       # concluída, com aviso ao humano
ST_IGNORADA = "ignorada"             # fora do escopo (não é certidão de nascimento)
ESTADOS_CONCLUIDOS = (ST_TAREFA_CRIADA, ST_SEM_NEGOCIO, ST_IGNORADA)
MAX_TENTATIVAS_MSG = 8               # alinhado a POLITICA_FILA do rede_bitrix
ATRASO_MINIMO_FILA_S = 600.0         # 10 min — piso do retry entre rodadas
MSG_PAGINA = 50                      # teto real da API por página
MSG_PAGINAS_HISTORICO = 12           # 12 x 50 = 600 mensagens de histórico por ciclo


# ---------------------------------------------------------------- utilidades
def agora_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def so_digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def fmt_cpf(cpf: str) -> str:
    """Apresentação 000.000.000-00 via cpf_util; sem 11 dígitos devolve o texto cru."""
    try:
        return cpf_util.formatar(cpf)
    except ValueError:
        return str(cpf or "")


def chave_vibecode() -> str:
    return seguranca.ler_segredo("vibecode_key") or ""


def vibe(path: str, metodo: str = "GET", corpo: dict | None = None, params: dict | None = None,
         binario: bool = False, tentativas: int = 4):
    """Cliente HTTP da API VibeCode. X-Api-Key SÓ para o host VibeCode (S13); timeout
    fixo; retry SÓ para erro transitório classificado por rede_bitrix.classificar (S12/CE-05)."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo,
                                 headers={"X-Api-Key": chave_vibecode(), "Accept": "application/json",
                                          "Content-Type": "application/json"})
    ultimo = None
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                bruto = r.read()
                if binario:
                    return bruto
                txt = bruto.decode(errors="ignore")
                return json.loads(txt) if txt.strip().startswith(("{", "[")) else {"_ok": r.status, "_txt": txt[:200]}
        except urllib.error.HTTPError as e:
            corpo_erro = ""
            try:
                corpo_erro = e.read()[:300].decode(errors="ignore")
            except Exception:  # noqa: BLE001
                pass
            ultimo = {"_erro": f"HTTP {e.code}", "_corpo": corpo_erro}
            erro = rede_bitrix.classificar(e.code, corpo_erro, e, escrita=(metodo != "GET"))
        except Exception as e:  # noqa: BLE001
            ultimo = {"_erro": seguranca.mascarar(str(e))}
            erro = rede_bitrix.classificar(None, "", e, escrita=(metodo != "GET"))
        if erro.permanente:
            ultimo["_classe"] = erro.classe.value
            return ultimo
        espera = rede_bitrix.atraso(i + 1, erro)
        if espera is None:                   # política de retry esgotada
            ultimo["_classe"] = erro.classe.value
            return ultimo
        time.sleep(espera)
    return ultimo or {}


def bx_chamar(metodo: str, params: dict, *, escrita: bool = False, **kw) -> dict:
    """Chamada REST do Bitrix via cliente único (webhook fora do argv, timeout, S12)."""
    return bitrix_cliente.chamar(metodo, params, escrita=escrita, **kw)


def bx_listar(metodo: str, params: dict) -> list:
    """Listagem paginada do Bitrix (nunca só a 1ª página de 50)."""
    return bitrix_cliente.listar(metodo, params)


def carregar_estado() -> dict:
    """Estado por mensagem (máquina CE-01) + listas legadas (--status). Escrita atômica."""
    e = estado_store.ler_estado(ESTADO)
    e.setdefault("mensagens", {})
    e.setdefault("msgs_processadas", [])
    e.setdefault("tarefas", [])
    e.setdefault("sem_negocio", [])
    return e


def gravar_estado(e: dict) -> None:
    estado_store.gravar_atomico(e, ESTADO)


def prazo_util(dias: int = PRAZO_DIAS_UTEIS) -> str:
    d = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=dias)
    while d.weekday() >= 5:                     # sábado/domingo
        d += dt.timedelta(days=1)
    return d.replace(hour=21, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------- CRM
# Identificação exige UNICIDADE (CE-03): busca paginada (bitrix_cliente.listar) e só
# aceita exatamente 1 contato; 0 ou >1 → motivo para humano, NUNCA "o primeiro da lista".
def _norm_nome(nome: str) -> str:
    return " ".join((nome or "").split()).strip().casefold()


def contatos_por_cpf(cpf: str) -> list[dict]:
    """TODOS os contatos com este CPF (distintos por ID), em qualquer campo UF."""
    cpf = so_digitos(cpf)
    if not cpf_util.valido(cpf):
        return []
    achados: dict[str, dict] = {}
    for campo in CPF_FIELDS:
        for variante in dict.fromkeys([cpf, fmt_cpf(cpf)]):
            for reg in bx_listar("crm.contact.list",
                                 {"filter": {campo: variante},
                                  "select": ["ID", "NAME", "LAST_NAME", "PHONE"]}):
                achados[str(reg.get("ID"))] = reg
    return list(achados.values())


def contatos_por_nome(nome: str) -> list[dict]:
    """Busca paginada por nome; devolve SÓ os casamentos exatos NAME+LAST_NAME."""
    if not nome or len(nome.strip()) < 5:
        return []
    alvo = _norm_nome(nome)
    achados = bx_listar("crm.contact.list",
                        {"filter": {"%NAME": nome.strip()},
                         "select": ["ID", "NAME", "LAST_NAME", "PHONE"]})
    return [c for c in achados
            if _norm_nome(f"{c.get('NAME', '')} {c.get('LAST_NAME', '')}") == alvo]


def achar_deal_por_contato(contato_id) -> dict | None:
    """Negócio ABERTO mais recente do contato (CE-03: filter CLOSED=N, DATE_CREATE DESC)."""
    negs = bx_listar("crm.deal.list", {"filter": {"CONTACT_ID": str(contato_id), "CLOSED": "N"},
                                       "select": ["ID", "TITLE", "STAGE_ID", "DATE_CREATE"]})
    if not negs:
        return None
    negs.sort(key=lambda d: str(d.get("DATE_CREATE") or ""), reverse=True)
    return negs[0]


RX_CPF = re.compile(r"(?<!\d)(\d{3})[.\-\s]?(\d{3})[.\-\s]?(\d{3})[.\-\s]?(\d{2})(?!\d)")
RX_TELEFONE = re.compile(r"(?<!\d)(?:\+?55[\s.\-]?)?\(?\d{2}\)?[\s.\-]?\d{4,5}[\s.\-]?\d{4}(?!\d)")


def _telefones_do_texto(t: str) -> list[str]:
    return [so_digitos(m.group(0)) for m in RX_TELEFONE.finditer(t or "")]


def identificar(texto: str) -> dict:
    """Leitura da legenda/mensagem: CPF (só com DV válido), nome da MÃE/RESPONSÁVEL
    (nunca o nome da criança — CE-03), telefones (2ª âncora) e como foi lido."""
    t = texto or ""
    for m in RX_CPF.finditer(t):
        cpf = "".join(m.groups())
        if cpf_util.valido(cpf):
            return {"cpf": cpf, "nome": None, "telefones": [], "como": "CPF na legenda"}
    nome, como = None, "nada na legenda"
    m = re.search(r"(?:m[ãa]e|genitora|respons[áa]vel|cliente|nome)\s*[:\-]\s*"
                  r"([A-Za-zÀ-ÿ' ]{5,60})", t, re.I)
    if m:
        nome = m.group(1).strip()
        como = "nome da mãe/responsável na legenda"
    return {"cpf": None, "nome": nome, "telefones": _telefones_do_texto(t), "como": como}


def identificar_negocio(cpf: str | None, nome: str | None,
                        telefones: list[str] | None = None) -> tuple[dict | None, str]:
    """(contato, motivo). UNICIDADE exigida (CE-03): 0 ou >1 contatos → (None, motivo
    para humano); nunca "o primeiro da lista"."""
    telefones = telefones or []
    if cpf:
        if not cpf_util.valido(cpf):
            return None, f"CPF {cpf_util.mascarar(cpf)} com DV inválido — não usei como âncora"
        contatos = contatos_por_cpf(cpf)
        if len(contatos) == 1:
            return contatos[0], ""
        if not contatos:
            return None, f"nenhum contato com o CPF {cpf_util.mascarar(cpf)}"
        return None, f"CPF em {len(contatos)} contatos — preciso do nome/telefone para decidir"
    if nome:
        contatos = contatos_por_nome(nome)
        if len(contatos) > 1:
            return None, f"{len(contatos)} contatos com o nome {nome!r} — preciso do CPF ou telefone"
        if not contatos:
            return None, f"nenhum contato com o nome {nome!r}"
        contato = contatos[0]
        if telefones and not _telefone_confere(contato, telefones):
            return None, (f"contato {nome!r} não confere com o telefone da legenda "
                          f"({len(telefones)} telefone(s)) — preciso do CPF")
        return contato, ""
    return None, "sem CPF e sem nome da mãe/responsável na legenda"


def _telefone_confere(contato: dict, telefones: list[str]) -> bool:
    """True se algum telefone do contato casa com algum da legenda."""
    do_contato = {so_digitos((p or {}).get("VALUE")) for p in (contato.get("PHONE") or [])}
    return any(so_digitos(t) in do_contato for t in telefones)


NOMES_MAE_IA = ("nome_mae", "nome_da_mae", "nome_mãe", "mae", "mãe", "genitora",
                "responsavel", "nome_responsavel", "responsável")


def _nome_mae_da_ia(cls: dict) -> str | None:
    """Só o nome da MÃE/RESPONSÁVEL. O `nome`/`nome_completo` é o do REGISTRADO (a
    criança) e NUNCA vira âncora de busca (CE-03)."""
    dados = cls.get("dados") or cls.get("data") or {}
    for chave in NOMES_MAE_IA:
        v = dados.get(chave) or cls.get(chave)
        if isinstance(v, str) and len(v.strip()) >= 5:
            return v.strip()
    return None


SCRIPT_IA = r'''
process.chdir('__APP__');
const fs = require('fs');
const cfg = require('__APP__/src/config.js').loadConfig();
const ia = require('__APP__/src/ia.js');
(async () => {
  const caminho = process.argv[2];
  const mime = process.argv[3] || 'application/octet-stream';
  const buf = fs.readFileSync(caminho);
  try {
    if (mime === 'application/pdf') {
      const pdf = require('__APP__/src/pdf.js');
      const txt = await pdf.extrairTexto(caminho);
      const r = await ia.classificarDocumento(cfg, txt, { fileName: caminho, contexto: 'certidao de nascimento' });
      console.log(JSON.stringify({ texto: (txt || '').slice(0, 1500), cls: r }));
    } else {
      const r = await ia.classificarImagem(cfg, buf, { fileName: caminho, mimeType: mime, contexto: 'certidao de nascimento' });
      console.log(JSON.stringify({ cls: r }));
    }
  } catch (e) { console.log(JSON.stringify({ erro: e.message })); }
})();
'''

FORMATOS_MAGICO = (
    (b"%PDF", ".pdf", "application/pdf"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"II*\x00", ".tif", "image/tiff"),
    (b"MM\x00*", ".tif", "image/tiff"),
)


def formato_do_arquivo(bruto: bytes) -> tuple[str, str]:
    """Foto de certidão NÃO é PDF (CE-02): o tipo vem dos BYTES, nunca do nome."""
    if not bruto:
        return "", "application/octet-stream"
    for magico, ext, mime in FORMATOS_MAGICO:
        if bruto.startswith(magico):
            return ext, mime
    if bruto[:4] == b"RIFF" and bruto[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return "", "application/octet-stream"


def analisar_certidao(bruto: bytes) -> tuple[str | None, str]:
    """Lê a certidão (PDF ou FOTO) com a IA do painel e devolve o nome da MÃE/RESPONSÁVEL.

    Temporários (documento e JS) privados, de nome único, apagados no finally
    (S09/CE-05) — nunca /tmp fixo e nunca acumulando PII.
    """
    if not bruto:
        return None, "arquivo vazio"
    ext, mime = formato_do_arquivo(bruto)
    if not ext:
        return None, "formato não suportado (nem PDF nem imagem) — a IA não lê"
    arq = seguranca.arquivo_temp_privado(sufixo=ext, prefixo="certidao-")
    js = seguranca.arquivo_temp_privado(sufixo=".js", prefixo="ler-certidao-")
    try:
        with open(arq, "wb") as fh:
            fh.write(bruto)
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(SCRIPT_IA.replace("__APP__", caminhos.SEPARADOR))
        try:
            r = subprocess.run([NODE, js, arq, mime], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return None, "IA demorou mais que 300 s (timeout do node)"
        try:
            d = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            # R3/A8: saída do node pode ecoar chave/CPF — sempre mascarada
            # mascara ANTES de truncar: corte no meio do segredo escaparia do regex
            return None, (f"IA não respondeu ({seguranca.mascarar(r.stdout or '')[:80]} "
                          f"{seguranca.mascarar(r.stderr or '')[:80]})")
        if d.get("erro"):
            return None, f"IA errou: {seguranca.mascarar(str(d['erro']))[:120]}"
        cls = d.get("cls") or {}
        mae = _nome_mae_da_ia(cls)
        if mae:
            return mae, "mãe lida da certidão"
        return None, "IA não achou o nome da mãe/responsável (só o do registrado?)"
    finally:
        seguranca.remover_seguro(arq)
        seguranca.remover_seguro(js)


def nome_da_certidao(caminho: str) -> tuple[str | None, str]:
    """Compat: lê do disco e delega a analisar_certidao (não apaga o arquivo do chamador)."""
    if not caminho or not os.path.exists(caminho):
        return None, "arquivo ausente"
    with open(caminho, "rb") as fh:
        return analisar_certidao(fh.read())


# ---------------------------------------------------------------- chat
RX_FALA_NASCIMENTO = re.compile(r"certid[ãa]o|nascimento|registro civil", re.I)
RX_NAO_NASCIMENTO = re.compile(r"casamento|óbito|obito|casad[oa]", re.I)


def e_certidao_de_nascimento(texto: str) -> bool:
    """CE-04: certidão de casamento/óbito NÃO é certidão de nascimento.

    Regra: "nascimento" no texto aceita; "certidão"/"registro civil" sem "nascimento"
    só aceita se NÃO falar de casamento/óbito.
    """
    t = texto or ""
    if re.search(r"nascimento", t, re.I):
        return True
    if RX_FALA_NASCIMENTO.search(t) and not RX_NAO_NASCIMENTO.search(t):
        return True
    return False


def _ja_concluida(estado: dict, chat: str, msg_id) -> bool:
    """Concluída = máquina de estados diz que sim, OU registro legado msgs_processadas."""
    chave = f"{chat}:{msg_id}"
    reg = (estado.get("mensagens") or {}).get(chave) or {}
    if reg.get("estado") in ESTADOS_CONCLUIDOS:
        return True
    return msg_id in (estado.get("msgs_processadas") or [])


def ler_mensagens_chat(chat: str, max_paginas: int = MSG_PAGINAS_HISTORICO) -> list[dict]:
    """Histórico do chat paginado (CE-04: a API devolve no máximo 50 por página;
    ?lastId=<menor id da página> desce o histórico — mesma curva de triagem_grupo)."""
    todas: dict[int, dict] = {}
    last_id = None
    for _ in range(max(1, int(max_paginas))):
        params = {"limit": MSG_PAGINA}
        if last_id is not None:
            params["lastId"] = last_id
        d = vibe(f"/chats/{chat}/messages", params=params)
        if "_erro" in d:
            print("  ERRO lendo o chat:", seguranca.mascarar(str(d)))
            break
        msgs = ((d.get("data") or {}).get("messages")) or []
        novos = 0
        for m in msgs:
            mid = m.get("id")
            if mid is not None and mid not in todas:
                todas[mid] = m
                novos += 1
        ids = [m["id"] for m in msgs if isinstance(m.get("id"), int)]
        if len(msgs) < MSG_PAGINA or not ids or novos == 0:
            break
        menor = min(ids)
        if last_id is not None and menor >= last_id:
            break
        last_id = menor
    return [todas[k] for k in sorted(todas)]


def ler_certidoes_novas(estado: dict, chat: str = CHAT_PROJETO) -> list[dict]:
    novas = []
    for m in ler_mensagens_chat(chat):
        if _ja_concluida(estado, chat, m.get("id")):
            continue
        p = m.get("params") or {}
        fid = p.get("fileId") or p.get("FILE_ID")
        # OBS: params.attach é bloco de LINK (ex.: "criou este projeto"), não arquivo.
        tem_arq = bool(fid) or bool(p.get("files")) or bool(p.get("FILE_ID"))
        if not tem_arq:
            continue                       # só olhamos mensagem COM anexo de verdade
        nome_arq = str(p.get("fileName") or p.get("NAME") or "")
        if nome_arq and not tipos_documento.e_documento(nome_arq):
            continue                       # áudio/vídeo não é certidão (tipos_documento)
        texto = m.get("text") or ""
        # No GRUPO passa muita coisa (foto de kit, RG, comprovante, certidão de
        # CASAMENTO): só tratamos como certidão de nascimento se o texto disser isso.
        # No PROJETO todo anexo é certidão de nascimento.
        if chat != CHAT_PROJETO and not e_certidao_de_nascimento(texto):
            continue
        novas.append({"id": m.get("id"), "autor": m.get("authorId"), "texto": texto,
                      "fileId": fid, "params": p, "data": m.get("date"), "chat": chat})
    return novas


def baixar_do_chat(file_id) -> bytes | None:
    """Tenta as rotas de download de arquivo do chat/disk."""
    for rota, params in ((f"/chats/files/{file_id}", None), (f"/files/{file_id}/download", None)):
        r = vibe(rota, params=params, binario=True)
        if isinstance(r, bytes) and len(r) > 200 and not r[:1] in (b"{", b"["):
            return r
    # a rota pode devolver JSON com base64/url
    r = vibe(f"/chats/files/{file_id}")
    if isinstance(r, dict):
        dados = r.get("data") or r
        for chave in ("content", "base64", "file", "body"):
            if isinstance(dados, dict) and dados.get(chave):
                try:
                    return base64.b64decode(dados[chave])
                except Exception:  # noqa: BLE001
                    pass
        url = dados.get("downloadUrl") or dados.get("url") if isinstance(dados, dict) else None
        if url:
            return baixar_url_externa(url)
    return None


def baixar_url_externa(url: str) -> bytes | None:
    """Baixa uma URL devolvida pela API. X-Api-Key SÓ para o host VibeCode autorizado
    (S13): URL de CDN/storage de terceiro NUNCA recebe a chave."""
    headers = {}
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == HOST_VIBECODE:
        headers["X-Api-Key"] = chave_vibecode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=120) as resp:
            return resp.read()
    except Exception:  # noqa: BLE001
        return None


def pasta_do_chat(chat: str = CHAT_PROJETO) -> int | None:
    d = vibe(f"/chats/{chat.replace('chat', '')}/folder")
    dados = d.get("data") if isinstance(d.get("data"), dict) else None
    return (dados or {}).get("id")


def subir_base64(pdf_bytes: bytes, nome: str, pasta: int) -> int | None:
    r = vibe("/files/upload", "POST", {"folderId": int(pasta), "filename": nome,
                                       "content": base64.b64encode(pdf_bytes).decode()})
    dados = r.get("data") if isinstance(r.get("data"), dict) else {}
    return dados.get("id")


# ---------------------------------------------------------------- tarefa
MARCADOR_PREFIXO = "[certidao:"


def chave_da_mensagem(e: dict) -> str:
    return f"{e.get('chat') or CHAT_PROJETO}:{e['id']}"


def marcador(chave: str) -> str:
    return f"{MARCADOR_PREFIXO}{chave}]"


def tarefa_existente(chave: str, deal_id) -> dict | None:
    """Idempotência (CE-01): antes de criar, procura a tarefa JÁ criada para esta
    mensagem pelo marcador [certidao:<chat>:<id>] gravado na descrição."""
    filtro = {"UF_CRM_TASK": f"D_{deal_id}"} if deal_id else {"GROUP_ID": str(GRUPO_PROJETO)}
    filtro[">=CREATED_DATE"] = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    alvo = marcador(chave)
    for t in bx_listar("tasks.task.list", {"filter": filtro,
                                           "select": ["ID", "TITLE", "DESCRIPTION"]}):
        if alvo in (t.get("DESCRIPTION") or ""):
            return t
    return None


def criar_tarefa(titulo: str, descricao: str, deal_id, arquivo_id,
                 grupo: int = GRUPO_PROJETO) -> int | None:
    """tasks.task.add via cliente REST único (webhook fora do argv — S12; escrita=True:
    timeout NÃO é repetido às cegas). Devolve o id da tarefa ou None."""
    campos = {
        "TITLE": titulo,
        "DESCRIPTION": descricao,
        "RESPONSIBLE_ID": RESPONSAVEL,
        "GROUP_ID": grupo,
        "DEADLINE": prazo_util(),
        "UF_CRM_TASK": [f"D_{deal_id}"] if deal_id else [],
        "UF_TASK_WEBDAV_FILES": [f"n{arquivo_id}"] if arquivo_id else [],
    }
    r = bx_chamar("tasks.task.add", {"fields": campos}, escrita=True)
    res = r.get("result") or {}
    t = res.get("task") if isinstance(res, dict) else res
    t = t if isinstance(t, dict) else {}
    tid = t.get("id") or t.get("ID")
    return int(tid) if tid else None


def confirmar_tarefa(tid) -> bool:
    """Só marca tarefa_criada DEPOIS de confirmar que a tarefa existe (CE-01)."""
    r = bx_chamar("tasks.task.get", {"taskId": tid, "select": ["ID"]})
    res = r.get("result") or {}
    t = res.get("task") if isinstance(res, dict) else res
    t = t if isinstance(t, dict) else {}
    achado = t.get("id") or t.get("ID")
    return str(achado or "") == str(tid)


def avisar_no_projeto(texto: str, chat: str = CHAT_PROJETO) -> bool:
    """im.message.add via cliente REST único (S12: corpo JSON, nada de URL no argv)."""
    r = bx_chamar("im.message.add", {"DIALOG_ID": chat, "MESSAGE": texto}, escrita=True)
    ok = bool(r.get("result"))
    print("    aviso no projeto:", "ok" if ok else seguranca.mascarar(json.dumps(r))[:150])
    return ok


# ---------------------------------------------------------------- principal
def _erro_local(classe, detalhe: str) -> rede_bitrix.Erro:
    return rede_bitrix.Erro(classe=classe, repetir=True, espera_s=0.0,
                            permanente=(classe in rede_bitrix.PERMANENTES),
                            detalhe=seguranca.mascarar(detalhe)[:500])


def _registrar(estado: dict, chave: str, e: dict, dados: dict) -> dict:
    reg = estado.setdefault("mensagens", {}).setdefault(chave, {})
    reg.update(dados)
    reg.setdefault("msg", e.get("id"))
    reg.setdefault("chat", e.get("chat"))
    reg["atualizado_em"] = agora_iso()
    return reg


def _registrar_falha(estado: dict, chave: str, e: dict, erro: rede_bitrix.Erro,
                     enviar: bool, chat_e: str) -> str:
    """CE-01: falha transitória vira falha_transitoria(n, proxima_em) e é reprocessada;
    só erro PERMANENTE (ou retry esgotado) vira falha_permanente, com motivo p/ humano."""
    reg = estado.setdefault("mensagens", {}).setdefault(chave, {})
    n = int(reg.get("tentativas") or 0) + 1
    reg.update({"msg": e.get("id"), "chat": chat_e, "tentativas": n,
                "motivo": erro.detalhe, "atualizado_em": agora_iso()})
    prox = None
    if not erro.permanente and n < MAX_TENTATIVAS_MSG:
        prox = rede_bitrix.proxima_tentativa(n, erro, fila=True)
        if prox is None:
            # escrita ambígua/LOOP: a MESMA requisição não se repete (contrato), mas a
            # mensagem pode ser reprocessada na próxima rodada — com consulta de
            # idempotência antes de recriar, nunca repetindo às cegas.
            prox = time.time() + ATRASO_MINIMO_FILA_S * min(n, 6)
    if prox is not None:
        reg["estado"] = ST_FALHA_TRANSITORIA
        reg["proxima_em"] = prox
    else:
        reg["estado"] = ST_FALHA_PERMANENTE
        reg.pop("proxima_em", None)
        if enviar:
            try:
                avisar_no_projeto(("⚠️ Certidão da mensagem %s (%s) falhou de vez — "
                                   "precisa de gente.\nMotivo: %s")
                                  % (e.get("id"), chat_e, erro.detalhe), chat_e)
            except FalhaBitrix:
                pass
    return reg["estado"]


def _processar_uma(e: dict, chat_e: str, enviar: bool, pastas: dict) -> dict:
    """Toda a lógica de UMA mensagem (sem gravar estado). Devolve o resultado;
    levanta FalhaBitrix (classificado) em erro transitório/permanente."""
    if chat_e not in pastas:
        pastas[chat_e] = pasta_do_chat(chat_e)
    pasta = pastas[chat_e]
    print(f"\n--- mensagem {e['id']} em {chat_e} (autor {e.get('autor')}) arquivo={e.get('fileId')}")
    print(f"    legenda: {(e.get('texto') or '(vazia)')[:120]!r}")
    ida = identificar(e.get("texto") or "")
    cpf, nome, telefones, como = ida["cpf"], ida["nome"], ida["telefones"], ida["como"]

    arquivo_bytes = e.get("bytes") or (baixar_do_chat(e.get("fileId")) if e.get("fileId") else None)
    if not cpf and not nome and arquivo_bytes:
        # sem âncora na legenda: a IA lê a certidão e devolve o nome da MÃE (CE-02/CE-03)
        nome_ia, como_ia = analisar_certidao(arquivo_bytes)
        if nome_ia:
            nome, como = nome_ia, como_ia
        else:
            como = como_ia
    print(f"    identificação: cpf={cpf_util.mascarar(cpf) if cpf else '-'} "
          f"nome={nome!r} ({como})")

    contato, motivo = identificar_negocio(cpf, nome, telefones)
    deal = achar_deal_por_contato(contato["ID"]) if contato else None
    cliente_nome = (f"{contato.get('NAME', '')} {contato.get('LAST_NAME', '')}".strip()
                    if contato else (nome or "(sem nome)"))
    print(f"    CRM: contato={contato.get('ID') if contato else None} "
          f"deal={deal.get('ID') if deal else None} cliente={cliente_nome!r}")

    if not contato:
        aviso = ("⚠️ Não achei o cliente desta certidão.\n"
                 f"Motivo: {motivo}\n"
                 f"Nome lido: {cliente_nome}\n"
                 f"{'CPF informado: ' + fmt_cpf(cpf) if cpf else 'Sem CPF na legenda.'}\n"
                 "Me digam o nome/CPF do cliente (com o CPF eu acho o negócio e crio a tarefa).")
        if enviar and not avisar_no_projeto(aviso, chat_e):
            # "sem negócio" só fica marcado DEPOIS do aviso entregue (CE-01)
            raise FalhaBitrix(_erro_local(rede_bitrix.Classe.REDE,
                                          "aviso de sem-negócio não entregue"))
        return {"estado": ST_SEM_NEGOCIO, "motivo": motivo, "cliente": cliente_nome}

    chave = chave_da_mensagem({**e, "chat": chat_e})
    deal_id = deal.get("ID") if deal else None

    # idempotência: a tarefa desta mensagem já existe? (queda na rodada anterior)
    existente = tarefa_existente(chave, deal_id) if enviar else None
    if existente:
        print(f"    tarefa JÁ existente para esta mensagem: {existente.get('ID')} — não duplico")
        return {"estado": ST_TAREFA_CRIADA, "tarefa": existente.get("ID"),
                "cliente": cliente_nome, "deal": deal_id, "reaproveitada": True}

    titulo = f"Certidão de nascimento — {cliente_nome}"
    descricao = (f"Certidão enviada no chat {chat_e}.\n"
                 f"Cliente: {cliente_nome}\n"
                 f"{'CPF: ' + fmt_cpf(cpf) if cpf else ''}\n"
                 f"Negócio vinculado: {deal_id if deal_id else 'NÃO ENCONTRADO'}\n"
                 f"Origem: mensagem {e['id']} do chat {chat_e} ({str(e.get('data', ''))[:16]})\n"
                 f"{marcador(chave)}")
    print(f"    TAREFA: {titulo!r} | responsável={RESPONSAVEL} projeto={GRUPO_PROJETO} "
          f"prazo={prazo_util()[:10]} deal={'D_' + str(deal_id) if deal_id else '-'}")
    if not enviar:
        return {"estado": "simulado", "cliente": cliente_nome, "deal": deal_id}

    if not arquivo_bytes:
        raise FalhaBitrix(_erro_local(rede_bitrix.Classe.REDE,
                                      "não consegui baixar a certidão do chat"))

    ext, _mime = formato_do_arquivo(arquivo_bytes)
    nome_arq = f"certidao_{so_digitos(cpf) or e['id']}{ext or '.bin'}"
    arq_id = subir_base64(arquivo_bytes, nome_arq, pasta)
    tid = criar_tarefa(titulo, descricao, deal_id, arq_id)
    if not tid:
        raise FalhaBitrix(_erro_local(rede_bitrix.Classe.SERVIDOR,
                                      "tasks.task.add não devolveu id da tarefa"))
    if not confirmar_tarefa(tid):
        raise FalhaBitrix(_erro_local(
            rede_bitrix.Classe.SERVIDOR,
            f"tarefa {tid} criada mas não confirmada — confiro na próxima rodada"))
    print(f"    tarefa criada e CONFIRMADA: {tid} (arquivo {arq_id})")
    return {"estado": ST_TAREFA_CRIADA, "tarefa": tid, "arquivo": arq_id,
            "cliente": cliente_nome, "deal": deal_id}


def processar(entradas: list[dict], enviar: bool, chat: str = CHAT_PROJETO,
              estado: dict | None = None) -> dict:
    """Roda as mensagens com a máquina de estados CE-01: marca `processando`, processa e
    grava o estado DEPOIS DE CADA mensagem (escrita atômica). Em --simular nada é gravado."""
    if estado is None:
        estado = carregar_estado()
    resumo = {"ok": 0, "sem_negocio": 0, "falha_transitoria": 0, "falha_permanente": 0,
              "pulado": 0, "detalhes": []}
    pastas: dict = {}
    for e in entradas:
        chat_e = e.get("chat") or chat
        chave = chave_da_mensagem({**e, "chat": chat_e})
        if _ja_concluida(estado, chat_e, e.get("id")):
            resumo["pulado"] += 1
            continue
        reg = (estado.get("mensagens") or {}).get(chave) or {}
        if (reg.get("estado") == ST_FALHA_TRANSITORIA
                and float(reg.get("proxima_em") or 0) > time.time()):
            resumo["pulado"] += 1        # retry agendado para depois
            continue
        if enviar:
            _registrar(estado, chave, {**e, "chat": chat_e}, {"estado": ST_PROCESSANDO})
            gravar_estado(estado)        # rastro antes do trabalho (queda não zera)
        try:
            resultado = _processar_uma(e, chat_e, enviar, pastas)
        except FalhaBitrix as fb:
            estado_f = _registrar_falha(estado, chave, {**e, "chat": chat_e}, fb.erro,
                                        enviar, chat_e)
            resumo[estado_f if estado_f in resumo else "falha_permanente"] += 1
            resumo["detalhes"].append({"msg": e.get("id"), "resultado": estado_f,
                                       "motivo": fb.erro.detalhe})
        except Exception as exc:  # noqa: BLE001  (bug local: reprocessa, com teto)
            erro = rede_bitrix.classificar(None, "", exc)
            estado_f = _registrar_falha(estado, chave, {**e, "chat": chat_e}, erro, enviar, chat_e)
            resumo[estado_f if estado_f in resumo else "falha_permanente"] += 1
            resumo["detalhes"].append({"msg": e.get("id"), "resultado": estado_f,
                                       "motivo": erro.detalhe})
        else:
            est = resultado.get("estado")
            if est == ST_TAREFA_CRIADA:
                resumo["ok"] += 1
            elif est == ST_SEM_NEGOCIO:
                resumo["sem_negocio"] += 1
            resumo["detalhes"].append({"msg": e.get("id"), **resultado})
            if enviar:
                _registrar(estado, chave, {**e, "chat": chat_e}, resultado)
                if est == ST_TAREFA_CRIADA and not resultado.get("reaproveitada"):
                    if e.get("id") not in estado.setdefault("msgs_processadas", []):
                        estado["msgs_processadas"].append(e.get("id"))
                    estado.setdefault("tarefas", []).append(
                        {"em": agora_iso(), "msg": e.get("id"), "tarefa": resultado.get("tarefa"),
                         "arquivo": resultado.get("arquivo"), "cliente": resultado.get("cliente"),
                         "deal": resultado.get("deal")})
                elif est == ST_TAREFA_CRIADA:
                    if e.get("id") not in estado.setdefault("msgs_processadas", []):
                        estado["msgs_processadas"].append(e.get("id"))
                elif est == ST_SEM_NEGOCIO:
                    if e.get("id") not in estado.setdefault("msgs_processadas", []):
                        estado["msgs_processadas"].append(e.get("id"))
                    estado.setdefault("sem_negocio", []).append(
                        {"em": agora_iso(), "msg": e.get("id"), "cliente": resultado.get("cliente"),
                         "motivo": resultado.get("motivo")})
        if enviar:
            gravar_estado(estado)        # CE-01: persiste após CADA mensagem
    return resumo


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Certidões de nascimento → tarefa vinculada ao negócio")
    p.add_argument("--simular", action="store_true", help="mostra o que faria (padrão)")
    p.add_argument("--enviar", action="store_true", help="cria as tarefas de verdade")
    p.add_argument("--status", action="store_true", help="o que já foi processado")
    p.add_argument("--chat", default=None, help="um chat só (sobrepõe --chats)")
    p.add_argument("--chats", default=None,
                   help="lista separada por vírgula (padrão: projeto CERTIDAO DE NASCIMENTO + grupo)")
    p.add_argument("--teste", help="arquivo local p/ testar (não usa o chat)")
    p.add_argument("--legenda", default="", help="legenda que acompanha o arquivo no teste")
    a = p.parse_args(argv)

    try:
        estado = carregar_estado()
    except estado_store.EstadoCorrompido as ec:
        print("ESTADO CORROMPIDO — não processo sem estado confiável:", ec)
        return 2

    if a.status:
        print(f"chats vigiados: {a.chats or a.chat or ', '.join(CHATS_PADRAO)}")
        maq = estado.get("mensagens") or {}
        cont: dict = {}
        for reg in maq.values():
            cont[reg.get("estado", "?")] = cont.get(reg.get("estado", "?"), 0) + 1
        print("máquina de estados por mensagem:", ", ".join(f"{k}={v}" for k, v in sorted(cont.items()))
              or "(vazia)")
        for chave, reg in sorted(maq.items()):
            print(f"   {chave}: {reg.get('estado')} tentativas={reg.get('tentativas', 0)} "
                  f"tarefa={reg.get('tarefa')} motivo={str(reg.get('motivo') or '')[:80]}")
        print(f"mensagens processadas (legado): {len(estado.get('msgs_processadas') or [])}")
        print(f"tarefas criadas: {len(estado.get('tarefas') or [])}")
        for t in estado.get("tarefas") or []:
            print(f"   {t.get('em','')[:16]}  tarefa {t.get('tarefa')}  {t.get('cliente')}  "
                  f"deal={t.get('deal')}  arquivo={t.get('arquivo')}")
        for s in estado.get("sem_negocio") or []:
            print(f"   {s.get('em','')[:16]}  SEM NEGÓCIO: {s.get('cliente')} (msg {s.get('msg')})")
        return 0

    if a.teste:
        if not os.path.exists(a.teste):
            print("arquivo de teste não existe:", a.teste)
            return 2
        with open(a.teste, "rb") as fh:
            dados = fh.read()
        entradas = [{"id": "teste", "autor": 1, "texto": a.legenda, "fileId": None,
                     "bytes": dados, "caminho_local": a.teste, "data": agora_iso()}]
    else:
        chats = [c.strip() for c in (a.chats or a.chat or ",".join(CHATS_PADRAO)).split(",") if c.strip()]
        entradas = []
        for c in chats:
            novas = ler_certidoes_novas(estado, c)
            print(f"  {c}: {len(novas)} certidão(ões) nova(s)")
            entradas += novas

    print("=" * 78)
    print(f"CERTIDÕES — {'ENVIANDO (cria tarefas)' if a.enviar else 'SIMULAÇÃO (não grava nada)'}"
          f"  | {len(entradas)} certidão(ões) nova(s)")
    print("=" * 78)
    if not entradas:
        print("nada novo (nenhuma mensagem com anexo desde a última rodada)")
        return 0

    # a máquina de estados persiste DENTRO de processar (após cada mensagem, CE-01)
    resumo = processar(entradas, a.enviar, CHAT_PROJETO, estado=estado)

    print("\n" + "-" * 78)
    print(f"RESULTADO: {resumo['ok']} tarefa(s) criada(s) · {resumo['sem_negocio']} sem negócio "
          f"· {resumo['falha_transitoria']} falha(s) transitória(s) "
          f"· {resumo['falha_permanente']} falha(s) permanente(s) · {resumo['pulado']} pulada(s)")
    # OP-O4/CE-05: exit code REAL para o systemd — falha permanente na rodada = erro
    return 1 if resumo.get("falha_permanente") else 0


if __name__ == "__main__":
    sys.exit(main())
