#!/usr/bin/env python3
"""
telegram_grupo.py — a esteira de documentos com entrada pelo TELEGRAM (fonte nova).

Como funciona (pedido do dono, 24/09/2026):
  · a pessoa manda o kit no PRIVADO do bot @documentosexemploBot (não é grupo);
  · `/start` = o traço: ABRE a janela; o próximo `/start` FECHA a de cima e abre a de baixo;
  · cada CHAT é uma esteira independente (a janela de uma pessoa não interfere na de outra);
  · os lotes caem no MESMO `estado.json`/`lotes/` da triagem -> a ponte leva para o painel
    e o worker processa igual (nada muda daí para frente).

Uso:
  telegram_grupo.py --uma-vez            # processa o que chegou e sai
  telegram_grupo.py --loop 30            # fica escutando (long polling)
  telegram_grupo.py --uma-vez --simular  # mostra o que faria, sem baixar/gravar
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import telegram_regras as TR  # noqa: E402
import triagem_grupo as tg  # noqa: E402
import relatorio as RL  # noqa: E402
import rede_bitrix as rb  # noqa: E402  (classificação de erro compartilhada)
import seguranca  # noqa: E402  (máscara de segredo/CPF e temporários privados)

ARQ_TOKEN = os.environ.get("TELEGRAM_TOKEN_FILE", "/root/.telegram/documentosexemplo.token")
ARQ_ESTADO = os.path.join(BASE, "telegram-estado.json")
DIR_LOTES = tg.DIR_LOTES
API = "https://api.telegram.org/bot{token}/{metodo}"

# C2: o Bot API do Telegram não entrega mais de 20 MB por arquivo — checar o
# `file_size` ANTES do getFile e classificar como erro permanente (Classe.TAMANHO).
LIMITE_ARQUIVO_TELEGRAM = 20 * 1024 * 1024
# TG-02: memória de lotes com LIMITE (não cresce sem teto) e janela de anti-perda.
MEMORIA_MAX_LOTES = 200
MEMORIA_IDADE_MAX_H = 24


# ── urlopen forçando IPv4 ──────────────────────────────────────────────────
# O WSL não tem rota IPv6 global (só link-local), mas api.telegram.org resolve
# para IPv6 primeiro. O urllib então escolhe o IPv6 e falha com
# "Network is unreachable". Este opener usa socket.create_connection(AF_INET)
# para travar no IPv4. (Mesmo bug do vigia cota agnes, resolvido com curl -4.)
class _HTTPSConnection4(http.client.HTTPSConnection):
    def _create_connection(self, addr, timeout=None, source_address=None):
        # sobrescreve apenas a resolução do socket: força IPv4 (o WSL não tem
        # rota IPv6 global). O connect() original cuida do tunnel + wrap SSL.
        host, port = addr
        for family, socktype, proto, _canon, sockaddr in socket.getaddrinfo(
                host, port, socket.AF_INET, socket.SOCK_STREAM):
            sock = socket.socket(family, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        raise OSError(f"sem endereço IPv4 para {host!r}")


class _HTTPSHandler4(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection4, req)


_opener4 = urllib.request.build_opener(_HTTPSHandler4())


def urlopen4(url: str, timeout: float):
    """urlopen que força IPv4 (para api.telegram.org)."""
    return _opener4.open(url, timeout=timeout)


def ler_token() -> str:
    """Token via seguranca.ler_segredo("telegram_token") — env TRIAGEM_TELEGRAM_TOKEN ->
    SEGREDOS_DIR/telegram_token -> legados (padrão /root/.telegram/documentosexemplo.token),
    com checagem de permissão/dono, recusa em /tmp e TRIAGEM_SEGREDOS_ESTRITO (R3/A6).
    LEGADO: TELEGRAM_TOKEN_FILE continua valendo como caminho extra, lido com as
    mesmas checagens. Ausente => seguranca.SegredoAusente (mensagem sem valor)."""
    token = seguranca.ler_segredo("telegram_token", obrigatorio=False)
    if not token and os.environ.get("TELEGRAM_TOKEN_FILE"):
        token = seguranca._ler_arquivo("telegram_token", ARQ_TOKEN, estrito=seguranca.modo_estrito())
    if not token:
        raise seguranca.SegredoAusente(
            "token do Telegram não encontrado (env TRIAGEM_TELEGRAM_TOKEN, "
            "SEGREDOS_DIR/telegram_token, legado ou TELEGRAM_TOKEN_FILE)")
    return token


def chamar(token: str, metodo: str, **params) -> dict:
    url = API.format(token=token, metodo=metodo)
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    with urlopen4(url, 90) as r:
        return json.loads(r.read().decode())


def carregar_estado() -> dict:
    """Lê o telegram-estado.json (cache deste módulo) via estado_store.

    Corrompido: o estado_store preserva a cópia `.corrompido-<ts>` e aqui o cache
    recomeça do zero (é cache: o offset volta a 0 e o Telegram reentrega o que
    ainda estiver na janela — nada de cliente é perdido).
    """
    from estado_store import EstadoCorrompido, ler_estado
    try:
        est = ler_estado(ARQ_ESTADO)
    except EstadoCorrompido:
        print("!! telegram-estado.json corrompido (cópia preservada); cache recomeçando")
        est = {}
    return est if isinstance(est, dict) else {"offset": 0, "chats": {}}


def gravar_estado(est: dict) -> None:
    """Gravação de estado SOMENTE via estado_store (atômico, 0600, fsync)."""
    from estado_store import gravar_atomico
    gravar_atomico(est, ARQ_ESTADO)


def _ext(nome: str | None, mime: str | None, tipo: str) -> str:
    if nome and "." in nome:
        return os.path.splitext(nome)[1].lower()
    mapa = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}
    return mapa.get(str(mime or "").lower()) or {"foto": ".jpg", "video": ".mp4"}.get(tipo, ".bin")


def _sha256_arquivo(caminho: str) -> str:
    """sha256 em blocos — não carrega o arquivo inteiro na RAM (E2)."""
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        while True:
            bloco = fh.read(1 << 16)
            if not bloco:
                break
            h.update(bloco)
    return h.hexdigest()


def baixar_arquivo_telegram(token: str, arq: dict, pasta: str, indice: int, lote_id: str,
                            force: bool = False, *, cfg: dict | None = None,
                            chat=None) -> dict:
    """Baixa UM anexo do Telegram para a pasta do lote, no MESMO formato de meta da esteira.

    · S03: fechado por padrão — sem chat autorizado na allowlist não há getFile nem
      download (e sem `cfg`/`chat` o acesso é NEGADO, nunca liberado).
    · C2: `file_size` acima de 20 MB é recusado ANTES do getFile, como erro
      PERMANENTE classificado (rede_bitrix.Classe.TAMANHO) com instrução de
      recuperação por outro canal.
    · E2: o download vai para um temporário exclusivo (0600), é validado
      (tamanho > 0 e == file_size quando houver), recebe sha256 e só então é
      movido para o destino com `os.replace` — arquivo truncado NUNCA é aceito
      como baixado nem fica no destino.
    """
    meta = {"file_id": arq.get("file_id"), "indice": indice,
            "nome_original": arq.get("nome"), "nome_salvo": None, "caminho": None,
            "tamanho_bits": None, "sha256": None, "baixado": False, "reaproveitado": False,
            "erro": None, "obtido_em": None, "fonte": "telegram:getFile", "tipo": arq.get("tipo")}
    try:
        if not TR.chat_permitido(chat, cfg):
            raise rb.FalhaBitrix(rb.Erro(
                classe=rb.Classe.ACESSO, repetir=False, espera_s=0.0, permanente=True,
                detalhe=f"chat {chat!r} não autorizado (allowlist vazia ou fora da lista)"))
        esperado = int(arq.get("tamanho") or 0)
        if esperado > LIMITE_ARQUIVO_TELEGRAM:            # C2: ANTES do getFile
            raise rb.ErroTamanho(
                f"excede 20 MB do Bot API do Telegram ({esperado} B): getFile nunca entrega")
        ext = _ext(arq.get("nome"), arq.get("mime"), str(arq.get("tipo") or ""))
        salvo = f"{indice:02d}_{lote_id}{ext}"
        destino = os.path.join(pasta, salvo)
        meta["nome_salvo"], meta["caminho"] = salvo, destino
        if not force and os.path.exists(destino):
            # E2: reaproveitar SÓ arquivo completo (tamanho > 0 e == file_size se houver)
            atual = os.path.getsize(destino)
            if atual > 0 and (not esperado or atual == esperado):
                meta["baixado"] = meta["reaproveitado"] = True
                meta["fonte"] = "já existia em disco"
        if not meta["baixado"]:
            os.makedirs(pasta, exist_ok=True)
            info = chamar(token, "getFile", file_id=arq["file_id"]).get("result") or {}
            caminho_remoto = info.get("file_path")
            if not caminho_remoto:
                raise RuntimeError(f"getFile não devolveu file_path ({info})")
            url = f"https://api.telegram.org/file/bot{token}/{caminho_remoto}"
            tmp = seguranca.arquivo_temp_privado(sufixo=".part", prefixo="tg-")
            try:
                with urlopen4(url, 120) as r, open(tmp, "wb") as fh:
                    while True:
                        bloco = r.read(1 << 16)
                        if not bloco:
                            break
                        fh.write(bloco)
                    fh.flush()
                    os.fsync(fh.fileno())
                baixado = os.path.getsize(tmp)
                if baixado <= 0:
                    raise rb.ErroIntegridade("download veio vazio (0 byte)")
                if esperado and baixado != esperado:
                    raise rb.ErroIntegridade(
                        f"tamanho baixado ({baixado}) != do Telegram ({esperado}): "
                        "arquivo truncado não é aceito como baixado")
                sha = _sha256_arquivo(tmp)
                os.replace(tmp, destino)   # atômico: o destino nunca fica pela metade
                tmp = None
                meta["sha256"] = sha
            finally:
                if tmp:
                    seguranca.remover_seguro(tmp)
        meta["tamanho_bits"] = os.path.getsize(destino)
        if not meta["sha256"]:
            meta["sha256"] = _sha256_arquivo(destino)
        meta["baixado"] = True
        meta["obtido_em"] = tg._dt.datetime.now(tg._dt.timezone.utc).isoformat(timespec="seconds")
    except Exception as e:                                   # noqa: BLE001
        erro = rb.classificar(getattr(e, "code", None), exc=e)   # classificação COMPARTILHADA
        meta["erro"] = seguranca.mascarar(f"{type(e).__name__}: {e}")
        meta["erro_tipo"] = "permanente" if erro.permanente else "transitorio"
        meta["erro_classe"] = erro.classe.value
        if erro.classe == rb.Classe.TAMANHO:
            meta["recuperacao"] = ("peça o arquivo por outro canal (e-mail/WhatsApp do "
                                   "escritório) ou dividido: o Bot API do Telegram não "
                                   "entrega mais de 20 MB")
    return meta


def mensagens_do_update(u, cfg: dict) -> dict | None:
    """Update do Telegram -> mensagem no formato interno da esteira (classificada)."""
    msg = TR.mensagem_api(u, cfg)
    if not msg.get("id"):
        return None
    classificado = tg.classificar_mensagem(msg, {}, cfg)
    classificado["chat"] = TR.chat_id_de(u)
    return classificado


def processar(updates: list, cfg: dict, token: str = "", simular: bool = False) -> dict:
    """Agrupa POR CHAT, baixa os anexos e devolve o resumo (e os lotes).

    S03: só chats autorizados entram (fechado por padrão); os anexos também são
    procurados APENAS entre updates de chats autorizados.
    """
    por_chat: dict = {}
    ignorados = 0
    updates_ok: list = []
    for u in updates:
        chat = TR.chat_id_de(u)
        if not chat or not TR.chat_permitido(chat, cfg):
            ignorados += 1
            continue
        m = mensagens_do_update(u, cfg)
        if not m:
            ignorados += 1
            continue
        updates_ok.append(u)
        por_chat.setdefault(chat, []).append(m)

    resumo = {"chats": {}, "ignorados": ignorados, "lotes": []}
    for chat, msgs in por_chat.items():
        msgs.sort(key=lambda m: int(m.get("id") or 0))
        lotes = tg.agrupar(list(msgs), cfg, agora=tg._dt.datetime.now(tg._dt.timezone.utc))
        resumo["chats"][chat] = {"mensagens": len(msgs), "lotes": len(lotes),
                                 "autores": sorted({m.get("autor") for m in msgs})}
        for lote in lotes:
            arquivos = []
            pasta = os.path.join(DIR_LOTES, lote["lote_id"])
            if lote.get("file_ids") and not simular:
                os.makedirs(pasta, exist_ok=True)
                for i, fid in enumerate(lote["file_ids"], 1):
                    arq = _anexo_por_file_id(updates_ok, fid)
                    if arq is None:
                        arquivos.append({"file_id": fid, "erro": "anexo não encontrado no update"})
                        continue
                    arquivos.append(baixar_arquivo_telegram(token, arq, pasta, i, lote["lote_id"],
                                                            cfg=cfg, chat=chat))
            reg = tg.serializar_lote(lote, arquivos or None)
            reg["dialog_id"] = f"tg:{chat}"
            reg["fonte_entrada"] = "telegram"
            resumo["lotes"].append(reg)
    return resumo


def _anexo_por_file_id(updates: list, file_id) -> dict | None:
    for u in updates:
        for a in TR.arquivos_do_update(u):
            if str(a.get("file_id")) == str(file_id):
                return a
    return None


def decidir_respostas(updates: list, est: dict, cfg: dict) -> list[tuple[str, str]]:
    """Qual relatório mandar em cada conversa, para os sinais (/start) do lote.

    Função PURA (não fala com a API): recebe os updates crus e o estado, devolve
    [(chat, texto)] e atualiza o estado — `aberto` (bloco em aberto por chat),
    `arquivos_desde_aberto` e `nomes_desde_aberto` (candidatos a nome do cliente).

    Regra do traço (delimitador): o 1º /start ABRE; cada /start seguinte FECHA o de
    cima e já deixa o de baixo aberto — então o relatório de fechamento já pede os
    próximos documentos.
    """
    abertos = est.setdefault("aberto", {})
    contagem = est.setdefault("arquivos_desde_aberto", {})
    nomes = est.setdefault("nomes_desde_aberto", {})
    textos = est.setdefault("textos_desde_aberto", {})
    sinal = str(cfg.get("traco_telegram") or "/start")

    por_chat: dict[str, list] = {}
    for u in updates:
        chat = TR.chat_id_de(u)
        if chat:
            por_chat.setdefault(chat, []).append(u)

    saida: list[tuple[str, str]] = []
    for chat, us in por_chat.items():
        if not TR.chat_permitido(chat, cfg):
            continue                       # S03: resposta só para chat autorizado
        us.sort(key=lambda u: int(TR._msg(u).get("message_id") or 0))
        nomes.setdefault(chat, [])
        contagem.setdefault(chat, 0)
        for u in us:
            msg = TR._msg(u)
            mid = msg.get("message_id")
            texto = TR.texto_do_update(u)
            autor = TR.autor_de(u)
            if TR.eh_sinal_telegram(texto, cfg):
                nome = (RL.nome_do_bloco(textos.get(chat)) or RL.nome_do_lote(nomes.get(chat)))
                texto_bot, novo = RL.decidir_resposta(
                    abertos.get(chat), nome=nome,
                    n_arquivos=contagem.get(chat, 0), sinal=sinal, msg=mid,
                    ts=TR.data_iso_do_update(u), autor=autor,
                    timeout_min=int(cfg.get("timeout_lote_min") or RL.TIMEOUT_PADRAO_MIN))
                abertos[chat] = novo
                contagem[chat] = 0            # a contagem era do bloco que fechou
                nomes[chat] = []
                textos[chat] = []
                saida.append((chat, texto_bot))
            else:
                arquivos = TR.arquivos_do_update(u)
                if arquivos:
                    contagem[chat] = int(contagem.get(chat, 0)) + len(arquivos)
                if texto:
                    textos.setdefault(chat, []).append(texto)
                    del textos[chat][:-20]     # guarda só as últimas 20 mensagens
                for n in tg.extrair_nomes(texto or ""):
                    if n not in nomes[chat]:
                        nomes[chat].append(n)
    return saida


def falar_no_chat(token: str, chat: str, texto: str, cfg: dict | None = None) -> bool:
    """Manda o relatório na conversa do Telegram (best-effort, sem derrubar o loop).

    S03: encaminhamento fechado por padrão — sem `cfg` com allowlist contendo o
    chat, NADA é enviado (mesmo que outro caminho tenha deixado o texto passar).
    """
    if not TR.chat_permitido(chat, cfg):
        print(f"  !! recusado: chat {chat} fora da allowlist — nada foi enviado")
        return False
    try:
        chamar(token, "sendMessage", chat_id=chat, text=texto,
               disable_web_page_preview=True)
        return True
    except Exception as e:                                   # noqa: BLE001
        print(f"  !! não consegui responder no chat {chat}: "
              f"{seguranca.mascarar(f'{type(e).__name__}: {e}')}")
        return False


def _chat_do_reg(reg: dict) -> str:
    """chat_id do registro do lote (`dialog_id` = "tg:<chat>"); vazio se não dá p/ saber."""
    d = str(reg.get("dialog_id") or "")
    return d[3:] if d.startswith("tg:") else ""


def mesclar_no_estado(resumo: dict, simular: bool = False, cfg: dict | None = None) -> None:
    """Grava os lotes do Telegram no MESMO estado.json da triagem (a ponte lê daqui).

    TRAVA (24/09/2026): a gravação é sob `estado_store.atualizar_estado` — o estado é
    RELIDO dentro da trava. Antes, este módulo lia o arquivo no começo e gravava no
    fim, e o ciclo do Bitrix (que lê no início e grava no fim de um ciclo de 120s)
    apagava os lotes daqui: 6 dos 7 lotes do Telegram do teste das 17:57-18:01
    nunca chegaram à ponte. O anti-perda restante é o `reforcar_lotes_salvos`, que
    (TG-02) só reaplica lote JOVEM que nunca chegou ao estado — lote que chegou e
    foi apagado é decisão do operador e não volta.
    """
    if simular:
        print(f"  [simulado] {len(resumo['lotes'])} lote(s) do Telegram não gravados")
        return
    # S03: a gravação também passa pela allowlist (fechado por padrão) — lote de
    # chat não autorizado não entra no estado.json mesmo que tenha sido montado.
    permitidos = set(TR.chats_permitidos(cfg))
    lotes = [reg for reg in (resumo.get("lotes") or []) if _chat_do_reg(reg) in permitidos]
    chats = {chat: info for chat, info in (resumo.get("chats") or {}).items()
             if str(chat) in permitidos}
    recusados = len(resumo.get("lotes") or []) - len(lotes)
    if recusados:
        print(f"  ⚠️  {recusados} lote(s) de chat fora da allowlist NÃO gravados")
    if not lotes and not chats:
        return
    from estado_store import atualizar_estado

    def _aplicar(est: dict) -> None:
        est.setdefault("lotes", {})
        est.setdefault("dialogos", {})
        for reg in lotes:
            lid = reg.get("lote_id")
            if not lid:
                continue
            est["lotes"][lid] = {**est["lotes"].get(lid, {}), **reg}
            for a in (reg.get("arquivos") or []):
                if a.get("file_id") and not a.get("erro"):
                    est.setdefault("arquivos_index", {})[str(a["file_id"])] = {
                        "lote_id": lid, "sha256": a.get("sha256"), "path": a.get("caminho"),
                        "nome": a.get("nome_salvo"), "nome_original": a.get("nome_original"),
                        "tamanho": a.get("tamanho_bits"), "fonte": a.get("fonte")}
        for chat, info in chats.items():
            est["dialogos"][f"tg:{chat}"] = {
                "mensagens": info["mensagens"], "lotes": info["lotes"],
                "autores": info["autores"],
                "em": tg._dt.datetime.now(tg._dt.timezone.utc).isoformat(timespec="seconds")}
        est["telegram_atualizado_em"] = tg._dt.datetime.now(
            tg._dt.timezone.utc).isoformat(timespec="seconds")

    atualizar_estado(_aplicar)
    novos = sum(1 for reg in lotes for a in (reg.get("arquivos") or [])
                if a.get("file_id") and not a.get("erro"))
    print(f"  estado: {len(lotes)} lote(s) do Telegram, {novos} arquivo(s) indexado(s)")


_MEM_CHAVE = "_memoria"      # marcador INTERNO da memória (nunca vai para o estado.json)


def _agora():
    return tg._dt.datetime.now(tg._dt.timezone.utc)


def _ts_de(iso):
    try:
        visto = tg._dt.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return visto if visto.tzinfo else visto.replace(tzinfo=tg._dt.timezone.utc)


def memoria_de_lotes(est_tg: dict, resumo: dict, confirmado: bool = False) -> list[dict]:
    """Guarda na MEMÓRIA do próprio módulo os lotes já vistos (TG-02: COM LIMITE).

    Cada entrada leva `_memoria = {gravado_em, confirmado}`:
      · `confirmado=True` quando o lote CHEGOU ao estado.json (ou foi reaplicado);
        depois disso a ausência dele no estado é decisão do operador e ele nunca é
        ressuscitado — era o defeito TG-02: a limpeza de propósito era desfeita;
      · a memória tem teto (`MEMORIA_MAX_LOTES`) e idade máxima
        (`MEMORIA_IDADE_MAX_H`): não cresce indefinidamente nem revive material antigo.
    """
    guardados = est_tg.setdefault("lotes_memoria", {})
    agora = _agora()
    for reg in resumo.get("lotes") or []:
        lid = reg.get("lote_id")
        if not lid:
            continue
        mem_antigo = (guardados.get(lid) or {}).get(_MEM_CHAVE) or {}
        guardados[lid] = {**reg, _MEM_CHAVE: {
            "gravado_em": mem_antigo.get("gravado_em") or agora.isoformat(timespec="seconds"),
            "confirmado": bool(confirmado or mem_antigo.get("confirmado"))}}
    _podar_memoria(guardados, agora)
    return list(guardados.values())


def _podar_memoria(guardados: dict, agora) -> None:
    """Idade máxima primeiro; depois o teto, derrubando as entradas mais antigas."""
    limite = agora - tg._dt.timedelta(hours=MEMORIA_IDADE_MAX_H)
    for lid in list(guardados):
        visto = _ts_de(((guardados.get(lid) or {}).get(_MEM_CHAVE) or {}).get("gravado_em"))
        if visto is None or visto < limite:
            guardados.pop(lid, None)         # velho/legado sem carimbo: fora
    while len(guardados) > MEMORIA_MAX_LOTES:
        guardados.pop(next(iter(guardados)), None)


def reforcar_lotes_salvos(est_tg: dict, simular: bool = False) -> int:
    """Reaplica no estado.json os lotes que NUNCA chegaram lá (anti-perda, TG-02).

    Só ressuscita lote JOVEM (dentro de `MEMORIA_IDADE_MAX_H`), nunca confirmado no
    estado e não marcado como ignorado. Lote que já chegou ao estado e depois sumiu
    foi apagado de propósito e NÃO volta. O que for reaplicado vira `confirmado`,
    para não ressuscitar de novo após uma remoção deliberada.
    """
    memoria = est_tg.get("lotes_memoria") or {}
    if not memoria or simular:
        return 0
    limite = _agora() - tg._dt.timedelta(hours=MEMORIA_IDADE_MAX_H)
    candidatos: dict = {}
    for lid, reg in memoria.items():
        if not isinstance(reg, dict):
            continue
        mem = reg.get(_MEM_CHAVE) or {}
        visto = _ts_de(mem.get("gravado_em"))
        if visto is None or visto < limite:
            continue                       # legado sem carimbo / muito antigo
        if mem.get("confirmado"):
            continue                       # já chegou ao estado: sumiu = decisão
        if reg.get("ignorado") or str(reg.get("status") or "").upper() in ("IGNORADO", "DESCARTADO"):
            continue                       # descartado de propósito
        candidatos[lid] = {k: v for k, v in reg.items() if k != _MEM_CHAVE}
    if not candidatos:
        return 0
    from estado_store import atualizar_estado

    faltando: list[str] = []

    def _aplicar(est: dict) -> None:
        lotes = est.setdefault("lotes", {})
        for lid, reg in candidatos.items():
            if lid not in lotes:
                faltando.append(lid)
                lotes[lid] = reg

    atualizar_estado(_aplicar)
    for lid in faltando:                    # ressuscitou UMA vez: agora é decisão
        (memoria[lid].setdefault(_MEM_CHAVE, {}))["confirmado"] = True
    if faltando:
        print(f"  ⚠️  {len(faltando)} lote(s) do Telegram nunca tinham chegado ao estado — "
              f"reaplicados: {', '.join(sorted(faltando)[-4:])}")
    return len(faltando)


def acumular_buffer(est: dict, updates: list, cfg: dict) -> list:
    """Junta as BATIDAS do long poll por chat e devolve o bloco a processar.

    O long poll do Telegram entrega as mensagens aos poucos (2 fotos por batida).
    Agrupar só a batida atual partia o kit: em 24/09/2026 o dono mandou 4 fotos entre
    dois `/start` e a esteira montou **dois lotes de 2** — um passou na ponte (tinha o
    CPF no texto) e o outro ficou fora do painel.

    Buffer por chat = tudo desde o traço que ABRIU o bloco. Quando chega um traço, o
    bloco fechado por ele é devolvido inteiro e o buffer RECOMEÇA nesse traço (é a
    regra do delimitador: o mesmo traço que fecha já abre o próximo).
    """
    buffer = est.setdefault("buffer", {})
    novos_por_chat: dict[str, list] = {}
    for u in updates or []:
        chat = TR.chat_id_de(u)
        if not chat or not TR.chat_permitido(chat, cfg):
            continue                       # S03: chat não autorizado nem entra no buffer
        buffer.setdefault(chat, []).append(u)
        novos_por_chat.setdefault(chat, []).append(u)

    saida: list = []
    for chat, ups in buffer.items():
        if chat not in novos_por_chat:
            continue                      # sem novidade neste chat: nada a refazer
        ups.sort(key=lambda u: int(TR._msg(u).get("message_id") or 0))
        indices_sinal = [i for i, u in enumerate(ups)
                         if TR.eh_sinal_telegram(TR.texto_do_update(u), cfg)]
        # o 1º sinal do buffer é a ABERTURA do bloco (não fecha nada); só um sinal
        # DEPOIS dele fecha. Sem abertura no buffer, qualquer sinal fecha o material.
        fecha_em = [i for i in indices_sinal if i != 0] if indices_sinal else []
        if fecha_em:
            ultimo = fecha_em[-1]
            buffer[chat] = ups[ultimo:]   # fecha o de cima, deixa o de baixo aberto
            saida.extend(ups[:ultimo + 1])
        else:
            buffer[chat] = ups[-200:]     # ainda aberto / fallback por janela
            saida.extend(ups)
    return saida


def decifrar_batidas_para_log(bloco: list) -> str:
    """Resumo do bloco (para o log): quantas mensagens e de quantas batidas."""
    return f"{len(bloco)} mensagem(ns) do bloco"


SAIDA_CONFIG = 3                 # config.json inválido — mesmo código do triagem_grupo.py
PAUSA_CONFIG_INICIAL_S = 60      # --loop com config inválido: 60 → 120 → … → teto
PAUSA_CONFIG_MAX_S = 900


def _aviso_config(e: Exception) -> str:
    return (f"🚨 telegram_grupo NÃO rodou: config inválido — "
            f"{seguranca.mascarar(str(e))[:300]}. "
            f"Corrija o config.json (nada foi lido do Telegram).")


def carregar_config_valida(loop: bool, dormir=None):
    """(Z3) Config da esteira. `tg.ConfigInvalida` vira mensagem mascarada, sem
    traceback: fora do loop devolve None (main sai com SAIDA_CONFIG); no --loop
    relê com pausa CRESCENTE até ficar válido — sair faria o systemd reiniciar
    em laço rápido."""
    dormir = dormir or time.sleep
    pausa = PAUSA_CONFIG_INICIAL_S
    while True:
        try:
            cfg = dict(tg.CONFIG_PADRAO)
            cfg.update(tg.carregar_config())
            return cfg
        except tg.ConfigInvalida as e:
            if not loop:
                print(f"{_aviso_config(e)} (código {SAIDA_CONFIG})", flush=True)
                return None
            print(f"{_aviso_config(e)} Nova tentativa em {pausa}s.", flush=True)
            dormir(pausa)
            pausa = min(pausa * 2, PAUSA_CONFIG_MAX_S)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Esteira de documentos via Telegram (/start = traço)")
    p.add_argument("--uma-vez", action="store_true", help="processa o que chegou e sai")
    p.add_argument("--loop", type=int, default=0, help="escuta a cada N segundos (long polling)")
    p.add_argument("--simular", action="store_true", help="não baixa nem grava")
    p.add_argument("--timeout", type=int, default=50, help="timeout do long polling (s)")
    a = p.parse_args(argv)

    cfg = carregar_config_valida(loop=bool(a.loop))
    if cfg is None:
        return SAIDA_CONFIG
    cfg["modo_traco"] = "delimitador"        # o /start delimita: fecha o de cima, abre o de baixo
    cfg["traco_telegram"] = cfg.get("traco_telegram") or "/start"
    if not TR.telegram_habilitado(cfg):
        # exigência da remediação: habilitado=false (ou ausente) = sai com código 0,
        # mensagem clara e SEM nenhuma chamada à API (o escritório não usa Telegram).
        print("Telegram desabilitado (telegram.habilitado=false ou ausente em config.json): "
              "nada a fazer, nenhum contato com a API.")
        return 0
    token = ler_token()
    est = carregar_estado()
    falhou = False

    while True:
        try:
            dados = chamar(token, "getUpdates", offset=est.get("offset") or 0,
                           timeout=(a.timeout if a.loop else 0), allowed_updates='["message"]')
            updates = dados.get("result") or []
        except Exception as e:                              # noqa: BLE001
            print(f"!! falha no getUpdates: {seguranca.mascarar(f'{type(e).__name__}: {e}')}")
            if not a.loop:
                return 1
            time.sleep(10)
            continue

        if updates:
            est["offset"] = max(int(u.get("update_id") or 0) for u in updates) + 1
            try:
                # 🧩 o bloco vem do BUFFER por chat (as batidas do long poll chegam de 2 em 2)
                bloco = acumular_buffer(est, updates, cfg)
                resumo = processar(bloco, cfg, token=token, simular=a.simular)
                for chat, info in resumo["chats"].items():
                    print(f"  chat {chat} ({', '.join(info['autores'])}): {info['mensagens']} msg, "
                          f"{info['lotes']} lote(s)")
                for reg in resumo["lotes"]:
                    print(f"    {reg.get('lote_id')} · {reg.get('status')} · "
                          f"cpf={seguranca.mascarar(str(reg.get('cpf_formatado') or '-'))} · "
                          f"{len(reg.get('arquivos') or [])} arq.")
                respostas = decidir_respostas(updates, est, cfg)
                if not a.simular:
                    # TG-03: PERSISTIR ANTES de responder "Recebido" — falha ao gravar
                    # = sem confirmação falsa (nenhuma mensagem sai).
                    persistiu = False
                    try:
                        mesclar_no_estado(resumo, cfg=cfg)
                        persistiu = True
                    except Exception as e:                     # noqa: BLE001
                        falhou = True
                        print(f"!! falha ao gravar lote(s) no estado: "
                              f"{seguranca.mascarar(f'{type(e).__name__}: {e}')}")
                        print("!! sem confirmação no chat: nada de 'Recebido' sem lote gravado")
                    memoria_de_lotes(est, resumo, confirmado=persistiu)
                    gravar_estado(est)
                    if persistiu:
                        # 🤖 RESPONDE NA CONVERSA (pedido do dono 24/09/2026): o /start que
                        # fecha o bloco de cima já responde "Recebido — N arquivos" e pede
                        # os próximos. Antes o bot do Telegram era mudo.
                        for chat, texto in respostas:
                            falou = falar_no_chat(token, chat, texto, cfg=cfg)
                            print(f"  🤖 chat {chat}: "
                                  f"{'respondi' if falou else 'FALHOU ao responder'} "
                                  f"-> {texto.splitlines()[0][:56]!r}")
                    elif respostas:
                        print(f"  ({len(respostas)} resposta(s) retida(s) por falta de gravação)")
                else:
                    for chat, texto in respostas:
                        print(f"  [simulado] responderia no chat {chat}: {texto[:60]!r}")
            except Exception as e:                             # noqa: BLE001
                # o loop não morre com erro de processamento/gravação (TG-03)
                falhou = True
                print(f"!! erro ao processar atualização: "
                      f"{seguranca.mascarar(f'{type(e).__name__}: {e}')}")
        elif not a.loop:
            print("nada novo no Telegram")

        # Anti-perda (TG-02): reaplica só lote JOVEM que nunca chegou ao estado;
        # o reaplicado é marcado como confirmado (não ressuscita duas vezes).
        if not a.simular:
            try:
                if reforcar_lotes_salvos(est):
                    gravar_estado(est)     # guarda as marcas de confirmado
            except Exception as e:                             # noqa: BLE001
                falhou = True
                print(f"!! erro ao reforçar lotes: "
                      f"{seguranca.mascarar(f'{type(e).__name__}: {e}')}")

        if not a.loop:
            return 1 if falhou else 0
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
