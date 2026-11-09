#!/usr/bin/env python3
"""
estado_store.py — LEITURA-MODIFICAÇÃO-GRAVAÇÃO do `estado.json` SOB TRAVA.

Motivo de existir (bug real medido em 24/09/2026): dois processos faziam
ler-modificar-gravar no MESMO `estado.json` sem trava —

  · `triagem_grupo.ciclo()` — lê no começo do ciclo e grava no fim;
  · `telegram_grupo.mesclar_no_estado()` — lê, mescla e grava a cada lote.

Quem gravava por último apagava o que o outro escreveu. Resultado no teste das
17:57-18:01: **6 dos 7 lotes do Telegram sumiram** do estado e nunca chegaram à
ponte (os do Bitrix se recuperam porque o ciclo relê o histórico; os do Telegram
NÃO, porque ele só lê update novo → kit perdido de verdade).

Aqui a gravação é sempre: trava o arquivo (flock) → RELÊ o estado atual →
aplica o mutador → grava atômico. Assim nada é perdido.

Remediação (frente B — auditoria A1/A2/A3/A4, F1-03/F1-04/F1-08):

  · JSON inválido NUNCA vira `{}`: `ler_estado` levanta `EstadoCorrompido` e guarda
    uma cópia `<arq>.corrompido-<ts>` (uma por conteúdo). `atualizar_estado` não
    grava nada quando a leitura falha — antes, o ciclo seguinte gravava só o seu
    delta por cima e reduzia 2.722 lotes aos da janela.
  · Gravação atômica de verdade: tmp exclusivo (pid+uuid, O_EXCL, 0600), fsync do
    arquivo e do diretório, `os.replace`; o tmp é removido em qualquer falha.
  · Merge POR CAMPO com dono (`PROPRIEDADE_CAMPOS`, `mesclar_lote`, `atualizar_lotes`):
    o ciclo da triagem, que regrava o lote inteiro a cada 2 min, não apaga mais o
    CPF completado por nome, a marcação de duplicata da ponte, nem o download
    reconciliado do disco.
  · Backups com retenção (`fazer_backup`, `podar_backups`) em `backups-estado/`.

REGRA PARA CONSUMIDORES: HTTP, sha256 e leitura demorada FORA do mutador; dentro
dele só se aplicam diferenças já calculadas (a trava deve durar milissegundos).
"""
from __future__ import annotations

import copy
import datetime as _dt
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid

import caminhos

log = logging.getLogger("estado_store")

BASE = caminhos.BASE                # compat: antes era o diretório deste arquivo
ARQ_ESTADO = os.environ.get("ESTADO_JSON") or caminhos.em_base("estado.json")

_RLOCK = threading.RLock()          # serializa threads DESTE processo
_local = threading.local()          # detecta reentrância na mesma thread
TIMEOUT_PADRAO = 120                # segundos esperando a trava de outro processo
DIR_BACKUPS = "backups-estado"      # subdiretório (ao lado do estado) dos backups novos


class EstadoCorrompido(Exception):
    """`estado.json` existe mas não é um JSON-objeto legível. NUNCA vira `{}`.

    Atributos: `caminho` (arquivo lido) e `copia` (onde o conteúdo ruim foi
    preservado, ou None se não deu para copiar).
    """

    def __init__(self, mensagem: str, caminho: str = "", copia: str | None = None):
        super().__init__(mensagem)
        self.caminho = caminho
        self.copia = copia


# --------------------------------------------------------------------------
# utilitários de arquivo
# --------------------------------------------------------------------------
def _carimbo() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _sha256_bytes(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def _sha256_arquivo(caminho: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(caminho, "rb") as fh:
            for bloco in iter(lambda: fh.read(1 << 20), b""):
                h.update(bloco)
    except OSError:
        return None
    return h.hexdigest()


def _escrever_exclusivo(destino: str, dados: bytes) -> None:
    """Cria `destino` (falha se já existir), modo 0600, grava tudo e faz fsync."""
    fd = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        vista = memoryview(dados)
        while vista:
            n = os.write(fd, vista)
            vista = vista[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(diretorio: str) -> None:
    try:
        fd = os.open(diretorio, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# leitura
# --------------------------------------------------------------------------
def _preservar_corrompido(caminho: str, dados: bytes) -> str | None:
    """Guarda UMA cópia por conteúdo em `<caminho>.corrompido-<ts>`."""
    diretorio = os.path.dirname(os.path.abspath(caminho)) or "."
    prefixo = os.path.basename(caminho) + ".corrompido-"
    alvo_sha = _sha256_bytes(dados)
    try:
        existentes = sorted(n for n in os.listdir(diretorio) if n.startswith(prefixo))
    except OSError:
        existentes = []
    for nome in existentes:
        cam = os.path.join(diretorio, nome)
        try:
            if os.path.getsize(cam) == len(dados) and _sha256_arquivo(cam) == alvo_sha:
                return cam                      # já preservado — não duplica
        except OSError:
            continue
    base = os.path.join(diretorio, prefixo + _carimbo())
    for i in range(1, 100):
        destino = base if i == 1 else f"{base}-{i}"
        try:
            _escrever_exclusivo(destino, dados)
            return destino
        except FileExistsError:
            continue
        except OSError as e:
            log.error("não consegui preservar o estado corrompido %s: %s", caminho, e)
            return None
    return None


def _ler_bruto(caminho: str) -> tuple:
    """(estado, bytes lidos). Ausente -> ({}, None). Corrompido -> EstadoCorrompido.

    Outros erros de E/S (permissão, disco) PROPAGAM: não são "estado vazio".
    """
    try:
        with open(caminho, "rb") as fh:
            dados = fh.read()
    except FileNotFoundError:
        return {}, None
    try:
        estado = json.loads(dados.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        copia = _preservar_corrompido(caminho, dados)
        log.critical("estado corrompido em %s (%s); cópia em %s — NADA será gravado",
                     caminho, type(e).__name__, copia)
        raise EstadoCorrompido(f"{caminho}: JSON inválido ({type(e).__name__}: {e})",
                               caminho, copia) from e
    if not isinstance(estado, dict):
        copia = _preservar_corrompido(caminho, dados)
        log.critical("estado em %s não é objeto JSON (%s); cópia em %s",
                     caminho, type(estado).__name__, copia)
        raise EstadoCorrompido(f"{caminho}: raiz é {type(estado).__name__}, esperado objeto",
                               caminho, copia)
    return estado, dados


def ler_estado(caminho: str | None = None) -> dict:
    """Lê o estado. Ausente -> {}. Corrompido/raiz não-dict -> `EstadoCorrompido`."""
    return _ler_bruto(caminho or ARQ_ESTADO)[0]


# --------------------------------------------------------------------------
# gravação
# --------------------------------------------------------------------------
def _serializar(estado: dict) -> bytes:
    return json.dumps(estado, ensure_ascii=False).encode("utf-8")


def _gravar_bytes(dados: bytes, caminho: str) -> None:
    caminho = os.path.abspath(caminho)
    diretorio = os.path.dirname(caminho)
    os.makedirs(diretorio, exist_ok=True)
    tmp = f"{caminho}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        _escrever_exclusivo(tmp, dados)
        os.replace(tmp, caminho)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(diretorio)


def gravar_atomico(estado: dict, caminho: str | None = None) -> None:
    """Grava atômico: tmp exclusivo (pid+uuid, 0600) + fsync + os.replace + fsync do dir.

    Em qualquer falha o arquivo original fica intacto e o tmp é removido.
    """
    if not isinstance(estado, dict):
        raise TypeError(f"estado deve ser dict, não {type(estado).__name__}")
    _gravar_bytes(_serializar(estado), caminho or ARQ_ESTADO)


# --------------------------------------------------------------------------
# trava
# --------------------------------------------------------------------------
def _segurando() -> dict:
    s = getattr(_local, "segurando", None)
    if s is None:
        s = _local.segurando = {}
    return s


def _trava(caminho: str, timeout: float = TIMEOUT_PADRAO):
    """Context manager: flock exclusivo no `<caminho>.lock` (reentrante na thread)."""
    chave = os.path.abspath(caminho)
    if _segurando().get(chave):
        return _Reentrada(chave)
    return _TravaDeArquivo(chave, timeout)


def trava_arquivo(caminho: str, timeout: float = TIMEOUT_PADRAO):
    """Trava pública (o MESMO flock de `atualizar_estado`) para quem precisa segurar
    um arquivo JSON enquanto faz algo além de um mutador (ex.: fila-enviados.json)."""
    return _trava(caminho, timeout)


class _Reentrada:
    def __init__(self, chave: str):
        self.chave = chave

    def __enter__(self):
        _segurando()[self.chave] += 1
        return self

    def __exit__(self, *a):
        _segurando()[self.chave] -= 1
        return False


class _TravaDeArquivo:
    def __init__(self, caminho: str, timeout: float):
        self.caminho = caminho
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        _RLOCK.acquire()
        try:
            os.makedirs(os.path.dirname(self.caminho), exist_ok=True)
            self.fd = os.open(self.caminho + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fim = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= fim:
                        raise TimeoutError(
                            f"trava de {self.caminho} não liberada em {self.timeout}s")
                    time.sleep(0.05)
            _segurando()[self.caminho] = 1
            return self
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            _RLOCK.release()
            raise

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
        finally:
            self.fd = None
            _segurando().pop(self.caminho, None)
            _RLOCK.release()
        return False


def atualizar_estado(mutador, caminho: str | None = None, timeout: float = TIMEOUT_PADRAO) -> dict:
    """Relê o estado sob trava, aplica `mutador(estado)` e grava.

    `mutador` pode alterar o dicionário no lugar e/ou devolver um novo dict.
    Se ele levantar exceção, NADA é gravado. Se o estado em disco estiver
    corrompido, `EstadoCorrompido` propaga e NADA é gravado (nunca regrava um
    estado "vazio" por cima). Trava ocupada além de `timeout` -> TimeoutError.
    Resultado idêntico ao que está em disco não é regravado.
    """
    caminho = caminho or ARQ_ESTADO
    with _trava(caminho, timeout):
        estado, bruto = _ler_bruto(caminho)
        resultado = mutador(estado)
        if isinstance(resultado, dict):
            estado = resultado
        dados = _serializar(estado)
        if bruto is None or dados != bruto:
            _gravar_bytes(dados, caminho)
        return estado


# --------------------------------------------------------------------------
# PROPRIEDADE DE CAMPOS (A3 / F1-03)
# --------------------------------------------------------------------------
# Quem é DONO de cada campo de um registro `estado["lotes"][id]`. Levantado dos
# gravadores reais (09/11/2026):
#   triagem       = triagem_grupo.serializar_lote (:2512) + ciclo (:2812, varredura :2848),
#                   telegram_grupo.mesclar_no_estado, dividir_lote_misto (cria sub-lotes)
#   identificacao = completar_cpf_por_nome (:126-139), nomear_lotes_por_cpf, backfill_nome_lotes
#   download      = baixar_pendentes, reconciliar_estado, reconciliar_arquivos_disco,
#                   reconciliar_por_file_ids (metas em arquivos[*]). O ciclo e o Telegram
#                   também baixam: por isso o download é MONOTÔNICO — quem não é o dono
#                   "download" não rebaixa um arquivo `baixado` com sha256 para não-baixado.
#   ponte         = lote_para_fila.marcar_duplicatas_no_estado (:125-147). O controle de envio
#                   (job_id/upload_id) mora hoje em fila-enviados.json, não no lote; os nomes
#                   ficam reservados aqui para quando forem gravados no lote.
#   revisao       = requer_revisao/motivos_revisao/pronto (decisão de encaminhar).
#
# Regras de escrita (`mesclar_lote`):
#   1. o dono escreve os seus campos;
#   2. ESCRITORES_EXTRA: outro dono autorizado num campo específico (identificacao marca
#      `pronto` ao achar o CPF; ponte põe `status` em descartado_duplicata/revisao_duplicata).
#      Um escritor extra não passa por cima de decisão explícita do dono nominal;
#   3. a TRIAGEM é o produtor-base (monta o registro a partir das mensagens): preenche
#      campos de qualquer dono ENQUANTO ninguém os reivindicou. Quem não é a triagem e
#      escreve um campo o reivindica (`_donos_campos[campo] = dono`); daí em diante o
#      ciclo da triagem não sobrescreve. Estado LEGADO (sem `_donos_campos`):
#      `cpf_origem` preenchido reivindica o grupo de identificação (+pronto/requer_revisao),
#      `nome_origem` reivindica `nome`, e `status` em descartado_*/revisao_duplicata
#      reivindica o status para a ponte;
#   4. motivos_revisao: motivos de outros donos (marcar_revisao) são fixos e só saem por
#      `_resolver_motivos` (nunca pela triagem). Os motivos DERIVADOS da triagem
#      (SEM_CPF/CONFLITO/SEM_ARQUIVOS/bloco interrompido) são recalculados a cada ciclo
#      (guardados em `_motivos_triagem`), e motivo já resolvido não volta pela triagem.
#      Havendo motivo ativo, requer_revisao=True; motivo NOVO pode rebaixar `pronto`;
#   5. campo desconhecido: dono 'triagem' (registrado em CAMPOS_DESCONHECIDOS + log).
DONOS = ("triagem", "identificacao", "download", "ponte", "revisao")

PROPRIEDADE_CAMPOS: dict = {
    # --- triagem: mensagens, file_ids, abertura/fechamento, nomes do cabeçalho
    "lote_id": "triagem", "modo": "triagem", "status": "triagem", "status_lote": "triagem",
    "sinal_abertura": "triagem", "sinal_fechamento": "triagem", "alerta": "triagem",
    "confianca": "triagem", "nomes_candidatos": "triagem", "nomes": "triagem",
    "nomes_ident": "triagem", "nomes_rotulos": "triagem", "nomes_papeis": "triagem",
    "nomes_por_cpf": "triagem", "cpfs": "triagem", "cpfs_papeis": "triagem",
    "cpf_responsavel": "triagem", "cpf_responsavel_formatado": "triagem",
    "cpfs_responsavel": "triagem", "autores": "triagem", "inicio": "triagem", "fim": "triagem",
    "fechado": "triagem", "fechado_por": "triagem", "marcador_fim_textual": "triagem",
    "ts_marcador": "triagem", "aberto": "triagem", "qtd_arquivos": "triagem",
    "file_ids": "triagem", "mensagens": "triagem", "arquivos": "triagem", "midias": "triagem",
    "pasta": "triagem", "visto_em": "triagem", "dialog_id": "triagem", "chat": "triagem",
    "fonte_entrada": "triagem", "dividido_de": "triagem", "dividido_em": "triagem",
    # F1-09/C2b: CPFs com DV errado lidos do TEXTO do kit (só revisão, nunca âncora).
    # Dono = triagem, como `cpfs`/`cpfs_papeis`: é recalculado das mensagens a cada
    # ciclo; se fosse da identificação, uma reivindicação congelaria o valor e o ciclo
    # não refletiria mensagem nova/editada. Ninguém mais grava este campo.
    "cpfs_invalidos": "triagem",
    # --- identificacao: quem é o cliente
    "cpf": "identificacao", "cpf_formatado": "identificacao", "cpfs_distintos": "identificacao",
    "cpf_origem": "identificacao", "nome_origem": "identificacao",
    "identificacao": "identificacao", "cpf_sugerido": "identificacao", "cpf_sugerido_motivo": "identificacao", "nome": "identificacao",
    # --- download: metas por arquivo; a ESTRUTURA da lista (quais file_ids) é da triagem
    "arquivos[*].baixado": "download", "arquivos[*].sha256": "download",
    "arquivos[*].caminho": "download", "arquivos[*].erro": "download",
    "arquivos[*].erro_tipo": "download", "arquivos[*].tentativas": "download",
    "arquivos[*].proximo_retry": "download", "arquivos[*].tamanho_bits": "download",
    "arquivos[*].nome_salvo": "download", "arquivos[*].obtido_em": "download",
    "arquivos[*].fonte": "download", "arquivos[*].reaproveitado": "download",
    "arquivos[*].aviso": "download", "arquivos[*].nome_derivado": "download",
    "arquivos[*].file_id": "triagem", "arquivos[*].indice": "triagem",
    "arquivos[*].nome_original": "triagem", "arquivos[*].acao": "triagem",
    "arquivos[*].destino_pasta": "triagem", "arquivos[*].tipo": "triagem",
    # --- ponte: desfecho de duplicata e (reservado) controle de envio
    "status_anterior": "ponte", "descartado_em": "ponte", "lote_origem": "ponte",
    "descarte_motivo": "ponte", "enviado": "ponte", "enviado_em": "ponte",
    "job_id": "ponte", "upload_id": "ponte", "status_envio": "ponte",
    # --- revisao: decisão de encaminhar
    "requer_revisao": "revisao", "motivos_revisao": "revisao",
    "motivos_resolvidos": "revisao", "pronto": "revisao", "pronto_por": "revisao",
    # `ignorado` (+ carimbo) é decisão de NÃO encaminhar o lote (estado_lote._ignorado
    # tira o lote da fila e do painel): mesma natureza de pronto/requer_revisao. Sendo
    # da revisão, o script que marca reivindica o campo e o ciclo da triagem (que
    # regrava o lote inteiro) nunca o desfaz. Plano de recuperação R1.
    "ignorado": "revisao", "ignorado_em": "revisao", "ignorado_motivo": "revisao",
    "ignorado_por": "revisao",
    # --- identificacao (cont.): confiança no CPF âncora (plano R2: CPF achado só por
    # nome fica como candidato com cpf_confiavel=False). É juízo sobre `cpf`, cujo dono
    # é a identificação. NÃO entra em _GRUPO_IDENTIFICACAO de propósito: a heurística
    # legada (cpf_origem preenchido) não deve reivindicá-lo antes de alguém gravá-lo.
    "cpf_confiavel": "identificacao",
}

ESCRITORES_EXTRA: dict = {
    "pronto": frozenset({"identificacao"}),
    "pronto_por": frozenset({"identificacao"}),
    "requer_revisao": frozenset({"identificacao", "ponte"}),
    "nomes_candidatos": frozenset({"identificacao"}),
    "status": frozenset({"ponte", "revisao"}),     # ponte: só valores de STATUS_PONTE
}

STATUS_PONTE = frozenset({"descartado_duplicata", "revisao_duplicata"})

_GRUPO_IDENTIFICACAO = frozenset({"cpf", "cpf_formatado", "cpfs_distintos", "identificacao",
                                  "cpf_origem", "cpf_sugerido", "pronto", "pronto_por",
                                  "requer_revisao"})
_MOTIVO_DERIVADO_TRIAGEM = re.compile(
    r"^(CONFLITO:|SEM_CPF:|SEM_ARQUIVOS:|bloco interrompido)", re.IGNORECASE)

CAMPOS_DESCONHECIDOS: set = set()
CHAVE_RESOLVER = "_resolver_motivos"


def dono_do_campo(campo: str) -> str:
    """Dono declarado; desconhecido -> 'triagem' (e fica registrado)."""
    dono = PROPRIEDADE_CAMPOS.get(campo)
    if dono is None:
        if campo not in CAMPOS_DESCONHECIDOS:
            CAMPOS_DESCONHECIDOS.add(campo)
            log.info("campo de lote sem dono declarado: %r -> 'triagem' "
                     "(declarar em PROPRIEDADE_CAMPOS)", campo)
        return "triagem"
    return dono


def _vazio(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _reivindicado_por(atual: dict, campo: str):
    donos = atual.get("_donos_campos") or {}
    if isinstance(donos, dict) and donos.get(campo) and donos[campo] != "triagem":
        return donos[campo]
    # heurística do estado legado (ainda sem `_donos_campos`)
    if campo == "status" and atual.get("status") in STATUS_PONTE:
        return "ponte"
    if campo == "nome" and atual.get("nome_origem"):
        return "identificacao"
    if campo in _GRUPO_IDENTIFICACAO and atual.get("cpf_origem"):
        return "identificacao"
    return None


def _pode_escrever(campo: str, dono: str, atual: dict, valor) -> bool:
    nominal = dono_do_campo(campo)
    reivindicante = _reivindicado_por(atual, campo)
    if dono == "triagem":
        return reivindicante is None
    if campo == "status" and dono == "ponte" and valor not in STATUS_PONTE:
        return False
    if nominal == dono:
        return True
    if dono in ESCRITORES_EXTRA.get(campo, ()):
        return nominal == "triagem" or reivindicante != nominal
    return False


def _casa_motivo(motivo, padrao) -> bool:
    motivo, padrao = str(motivo), str(padrao)
    if padrao.endswith("*"):
        return motivo.startswith(padrao[:-1])
    return motivo == padrao


def _agora_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _chave_arquivo(meta: dict):
    if meta.get("file_id") not in (None, ""):
        return ("fid", str(meta["file_id"]))
    return ("idx", meta.get("indice"))


def _baixado_ok(meta: dict) -> bool:
    return bool(meta.get("baixado")) and bool(meta.get("sha256"))


def _ordenar_arquivos(lista: list) -> list:
    return sorted(lista, key=lambda m: (not isinstance(m.get("indice"), int),
                                        m.get("indice") if isinstance(m.get("indice"), int) else 0))


def _mesclar_arquivos(atual_lote: dict, novo_lote: dict, dono: str, conflitos: list) -> list:
    atuais = [m for m in (atual_lote.get("arquivos") or []) if isinstance(m, dict)]
    novos = [m for m in (novo_lote.get("arquivos") or []) if isinstance(m, dict)]
    por_chave = {_chave_arquivo(m): m for m in atuais}
    fids = {str(f) for f in (novo_lote.get("file_ids", atual_lote.get("file_ids")) or [])}

    def mesclar_item(velho, novo: dict) -> dict:
        if velho is None:
            return copy.deepcopy(novo)
        saida = copy.deepcopy(velho)
        regressao = _baixado_ok(velho) and not _baixado_ok(novo)
        negados = []
        for campo, valor in novo.items():
            if dono_do_campo(f"arquivos[*].{campo}") == "download":
                ok = dono == "download" or (dono == "triagem" and not regressao)
            else:                                   # estrutura: triagem (download só preenche vazio)
                ok = dono == "triagem" or _vazio(velho.get(campo))
            if ok:
                saida[campo] = copy.deepcopy(valor)
            elif velho.get(campo) != valor:
                negados.append(campo)
        if negados:
            conflitos.append(f"arquivos[{_chave_arquivo(velho)[1]}]." + ",".join(sorted(negados)))
        return saida

    if dono == "triagem":
        resultado, vistos = [], set()
        for m in novos:
            k = _chave_arquivo(m)
            vistos.add(k)
            resultado.append(mesclar_item(por_chave.get(k), m))
        # meta antiga de file_id que continua no lote mas não veio neste ciclo: fica
        for m in atuais:
            k = _chave_arquivo(m)
            if k not in vistos and k[0] == "fid" and (not fids or k[1] in fids):
                resultado.append(copy.deepcopy(m))
        return _ordenar_arquivos(resultado)

    if dono == "download":
        resultado = [copy.deepcopy(m) for m in atuais]
        posicao = {_chave_arquivo(m): i for i, m in enumerate(resultado)}
        for m in novos:
            k = _chave_arquivo(m)
            if k in posicao:
                resultado[posicao[k]] = mesclar_item(resultado[posicao[k]], m)
            elif k[0] == "fid" and (not fids or k[1] in fids):
                resultado.append(copy.deepcopy(m))
                posicao[k] = len(resultado) - 1
            else:
                conflitos.append(f"arquivos[{k[1]}] (file_id fora do lote)")
        return _ordenar_arquivos(resultado)

    if novos != atuais:
        conflitos.append("arquivos")
    return [copy.deepcopy(m) for m in atuais]


def mesclar_lote(atual: dict, novo: dict, dono: str) -> tuple:
    """Merge POR CAMPO respeitando `PROPRIEDADE_CAMPOS`. Não altera os argumentos.

    Devolve (lote_mesclado, conflitos). Conflito = campo que `dono` tentou mudar
    sem ter direito (o valor atual é preservado). Campo ausente em `novo` nunca é
    apagado (é patch). Chave especial em `novo`: `_resolver_motivos: [texto |
    "prefixo*"]` remove motivos de revisão explicitamente (qualquer dono, exceto
    a triagem) e registra em `motivos_resolvidos`. Outras chaves iniciadas por
    `_` são ignoradas (não se forja `_donos_campos`).
    """
    if dono not in DONOS:
        raise ValueError(f"dono desconhecido: {dono!r} (use um de {DONOS})")
    atual = atual if isinstance(atual, dict) else {}
    novo = novo if isinstance(novo, dict) else {}
    saida = copy.deepcopy(atual)
    conflitos: list = []
    donos_campos = dict(atual.get("_donos_campos") or {})

    def reivindicar(campo):
        if dono != "triagem":
            donos_campos[campo] = dono

    especiais = {"arquivos", "motivos_revisao", "requer_revisao", "pronto", "motivos_resolvidos"}
    for campo, valor in novo.items():
        if campo in especiais or campo.startswith("_"):
            continue
        if _pode_escrever(campo, dono, atual, valor):
            saida[campo] = copy.deepcopy(valor)
            reivindicar(campo)
        elif atual.get(campo) != valor:
            conflitos.append(campo)

    if "arquivos" in novo:
        saida["arquivos"] = _mesclar_arquivos(atual, novo, dono, conflitos)

    # ---- revisão: resolução explícita, motivos, requer_revisao, pronto
    motivos_antes = [str(m) for m in (atual.get("motivos_revisao") or [])]
    motivos = list(motivos_antes)
    resolvidos = list(atual.get("motivos_resolvidos") or [])
    pedidos = [str(p) for p in (novo.get(CHAVE_RESOLVER) or [])]
    if pedidos and dono == "triagem":
        conflitos.append(CHAVE_RESOLVER)
        pedidos = []
    for p in pedidos:
        removidos = [m for m in motivos if _casa_motivo(m, p)]
        motivos = [m for m in motivos if not _casa_motivo(m, p)]
        resolvidos.append({"motivo": p, "por": dono, "em": _agora_iso(), "removidos": removidos})
    if pedidos:
        reivindicar("requer_revisao")
        reivindicar("motivos_revisao")
    padroes_resolvidos = [r.get("motivo") for r in resolvidos if isinstance(r, dict) and r.get("motivo")]

    if "motivos_revisao" in novo:
        vindos = [str(m) for m in (novo.get("motivos_revisao") or [])]
        if dono == "triagem":
            anteriores = atual.get("_motivos_triagem")
            if anteriores is None:                  # legado: reconhece pelos prefixos
                anteriores = [m for m in motivos_antes if _MOTIVO_DERIVADO_TRIAGEM.match(m)]
            anteriores = set(anteriores)
            vindos = [m for m in vindos if not any(_casa_motivo(m, p) for p in padroes_resolvidos)]
            # CPF já completado por outro dono (inclusive no estado legado, sem registro
            # de resolução): o "SEM_CPF" recalculado das mensagens não vale mais
            if (_reivindicado_por(atual, "identificacao")
                    and str(atual.get("identificacao") or "").upper() == "OK"):
                vindos = [m for m in vindos if not m.upper().startswith("SEM_CPF:")]
            fixos = [m for m in motivos if m not in anteriores]
            motivos = fixos + [m for m in vindos if m not in fixos]
            saida["_motivos_triagem"] = vindos
        else:
            motivos = motivos + [m for m in vindos if m not in motivos]
    novo_motivo = any(m not in motivos_antes for m in motivos)
    if "motivos_revisao" in atual or "motivos_revisao" in novo or pedidos:
        saida["motivos_revisao"] = motivos
    if resolvidos:
        saida["motivos_resolvidos"] = resolvidos

    rr_atual = bool(atual.get("requer_revisao"))
    if motivos:
        rr = True
        if "requer_revisao" in novo and not novo["requer_revisao"] and dono != "triagem":
            conflitos.append("requer_revisao")
    elif "requer_revisao" in novo and _pode_escrever("requer_revisao", dono, atual,
                                                     novo["requer_revisao"]):
        rr = bool(novo["requer_revisao"])
        reivindicar("requer_revisao")
    elif pedidos:
        rr = False                                   # todos os motivos resolvidos agora
    elif "requer_revisao" in novo and novo["requer_revisao"] and dono != "triagem":
        rr = True                                    # marcar revisão é sempre permitido
        reivindicar("requer_revisao")
    else:
        rr = rr_atual
        if "requer_revisao" in novo and bool(novo["requer_revisao"]) != rr:
            conflitos.append("requer_revisao")
    if "requer_revisao" in atual or "requer_revisao" in novo or rr:
        saida["requer_revisao"] = rr

    if "pronto" in novo:
        valor = novo["pronto"]
        if _pode_escrever("pronto", dono, atual, valor):
            saida["pronto"] = valor
            reivindicar("pronto")
        elif not valor and novo_motivo:
            saida["pronto"] = False                  # rebaixar por motivo NOVO é seguro
        elif atual.get("pronto") != valor:
            conflitos.append("pronto")

    if donos_campos:
        saida["_donos_campos"] = donos_campos
    return saida, conflitos


def atualizar_lotes(mudancas: dict, dono: str, caminho: str | None = None,
                    timeout: float = TIMEOUT_PADRAO) -> dict:
    """Aplica `mudancas[lote_id] = {campo: valor}` sob trava, por campo, com dono.

    Lote inexistente: a triagem cria; outro dono recebe o conflito
    `"<lote inexistente>"`. Devolve `{lote_id: [campos negados]}` só dos lotes
    com conflito (dict vazio = tudo aplicado).
    """
    if dono not in DONOS:
        raise ValueError(f"dono desconhecido: {dono!r} (use um de {DONOS})")
    conflitos: dict = {}

    def _mutar(estado: dict) -> None:
        conflitos.clear()
        lotes = estado.setdefault("lotes", {})
        for lid, patch in (mudancas or {}).items():
            atual = lotes.get(lid)
            if not isinstance(atual, dict):
                if dono != "triagem":
                    conflitos[lid] = ["<lote inexistente>"]
                    continue
                atual = {}
            mesclado, negados = mesclar_lote(atual, patch, dono)
            lotes[lid] = mesclado
            if negados:
                conflitos[lid] = negados

    atualizar_estado(_mutar, caminho=caminho, timeout=timeout)
    nivel = logging.DEBUG if dono == "triagem" else logging.WARNING
    for lid, negados in conflitos.items():
        log.log(nivel, "atualizar_lotes(%s): %s — preservado o valor de outro dono em %s",
                dono, lid, negados)
    return dict(conflitos)


# --------------------------------------------------------------------------
# backups (F1-04)
# --------------------------------------------------------------------------
def _dir_backups(caminho: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(caminho)), DIR_BACKUPS)


def _limpo(motivo: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (motivo or "").lower()).strip("_")[:40]


def _listar_backups(caminho: str) -> list:
    """[(ts 'YYYYMMDDHHMMSS', caminho)] do mais novo para o mais antigo: os novos em
    `backups-estado/` e os legados `<estado>.bak-YYYYMMDD-HHMMSS*` ao lado do estado."""
    caminho = os.path.abspath(caminho)
    base = os.path.basename(caminho)
    padrao = re.compile(re.escape(base) + r"\.bak-(\d{8})-(\d{6})(?:[-_.][A-Za-z0-9_.-]*)?$")
    achados = []
    for diretorio in (_dir_backups(caminho), os.path.dirname(caminho)):
        try:
            nomes = os.listdir(diretorio)
        except OSError:
            continue
        for nome in nomes:
            m = padrao.match(nome)
            cam = os.path.join(diretorio, nome)
            if not m or not os.path.isfile(cam):
                continue
            try:
                mtime = os.path.getmtime(cam)
            except OSError:
                mtime = 0.0
            achados.append((m.group(1) + m.group(2), mtime, cam))
    achados.sort(reverse=True)
    return [(ts, cam) for ts, _, cam in achados]


def fazer_backup(caminho: str | None = None, motivo: str = "") -> str | None:
    """Cópia 0600 em `<dir>/backups-estado/<estado>.bak-<ts>[-motivo]`.

    Estado ausente -> None. Conteúdo igual ao backup mais recente -> não copia de
    novo e devolve o caminho dele (eram ~2,1 GB/dia de cópias iguais).
    """
    caminho = os.path.abspath(caminho or ARQ_ESTADO)
    try:
        with open(caminho, "rb") as fh:
            dados = fh.read()
    except FileNotFoundError:
        return None
    destino_dir = _dir_backups(caminho)
    os.makedirs(destino_dir, mode=0o700, exist_ok=True)
    existentes = _listar_backups(caminho)
    if existentes:
        ultimo = existentes[0][1]
        if os.path.getsize(ultimo) == len(dados) and _sha256_arquivo(ultimo) == _sha256_bytes(dados):
            return ultimo
    sufixo = f"-{_limpo(motivo)}" if _limpo(motivo) else ""
    raiz = os.path.join(destino_dir, f"{os.path.basename(caminho)}.bak-{_carimbo()}")
    for i in range(1, 1000):
        destino = f"{raiz}{'' if i == 1 else f'-{i}'}{sufixo}"
        try:
            _escrever_exclusivo(destino, dados)
        except FileExistsError:
            continue
        _fsync_dir(destino_dir)
        return destino
    raise OSError(f"não consegui criar backup único em {destino_dir}")


def podar_backups(caminho: str | None = None, manter_ultimos: int = 24, diarios: int = 14,
                  simular: bool = True) -> list:
    """Retenção: mantém os `manter_ultimos` mais novos + o mais novo de cada um dos
    `diarios` dias mais recentes que têm backup. Reconhece também o padrão legado
    `<estado>.bak-YYYYMMDD-HHMMSS` ao lado do estado. Com `simular=True` (padrão)
    NUNCA apaga. Devolve a lista do que apagaria/apagou (do mais novo ao mais antigo).
    """
    caminho = os.path.abspath(caminho or ARQ_ESTADO)
    todos = _listar_backups(caminho)
    manter = {cam for _, cam in todos[:max(0, int(manter_ultimos))]}
    dias = []
    for ts, cam in todos:                           # do mais novo para o mais antigo
        if ts[:8] in dias:
            continue
        if len(dias) >= max(0, int(diarios)):
            break
        dias.append(ts[:8])
        manter.add(cam)
    apagar = [cam for _, cam in todos if cam not in manter and cam != caminho]
    if not simular:
        for cam in apagar:
            try:
                os.unlink(cam)
            except FileNotFoundError:
                pass
    return apagar


def limpar_tmp_orfaos(caminho: str | None = None, idade_min_s: float = 3600,
                      simular: bool = True) -> list:
    """Lista/remove `<estado>.tmp*` mais velhos que `idade_min_s` (gravação morta no meio)."""
    caminho = os.path.abspath(caminho or ARQ_ESTADO)
    diretorio, base = os.path.split(caminho)
    agora = time.time()
    saida = []
    try:
        nomes = os.listdir(diretorio)
    except OSError:
        return saida
    for nome in nomes:
        if not nome.startswith(base + ".tmp"):
            continue
        cam = os.path.join(diretorio, nome)
        try:
            if agora - os.path.getmtime(cam) < idade_min_s:
                continue
        except OSError:
            continue
        saida.append(cam)
        if not simular:
            try:
                os.unlink(cam)
            except FileNotFoundError:
                pass
    return saida
