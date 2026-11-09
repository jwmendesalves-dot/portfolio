"""Segurança comum — fonte única (CONTRATOS.md): máscara, segredos, temporários.

Todas as frentes importam daqui; não crie máscara/leitura de segredo paralela.

    mascarar(texto) -> str            webhook rest/<id>/<token>, X-Api-Key, Authorization,
                                      token Telegram, querystring assinada (auth/token/...),
                                      token=/auth=/access_token=/refresh_token=/client_secret=
                                      em texto livre e chave JSON/dict ("token": "x"),
                                      usuário:senha em URL, CPF (com/sem pontuação),
                                      senha gov.br/INSS ("Senha: x", "=SENHA x", ...),
                                      chave TypeSafe "apikey_<...>" em texto livre (J2),
                                      "Bearer <v>" solto, api_key/apikey/typesafe_key (R3)
    mascarar_url(url) -> str          só os padrões de URL (+ CPF)
    FiltroMascara / instalar_filtro_log(logger=None)
    remover_senhas(texto) -> str      PERSISTÊNCIA: só o VALOR da senha vira "[senha removida]"
    ler_segredo(nome, obrigatorio=True) -> str | None
    SegredoAusente (RuntimeError) > SegredoInseguro, SegredoInvalido
    SegredoRecusado (RuntimeError, NÃO é SegredoAusente) — só com TRIAGEM_SEGREDOS_ESTRITO=1
    arquivo_temp_privado(sufixo="", prefixo="triagem-") -> str
    remover_seguro(caminho) -> None

Nada aqui loga ou devolve em mensagem de erro o VALOR de um segredo.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import stat

import caminhos
import cpf_util

log = logging.getLogger("triagem.seguranca")

MASCARA = "***"
SENHA_REMOVIDA = "[senha removida]"

# ---------------------------------------------------------------------------
# Padrões de máscara
# ---------------------------------------------------------------------------
# usuário:senha@ embutido em URL
_RX_USERINFO = re.compile(r"(?i)\b(https?://)[^/\s:@\"'<>]+:[^/\s@\"'<>]+@")
# webhook Bitrix: https://<portal>/rest/<id>/<token>/... -> mantém portal e id
_RX_WEBHOOK = re.compile(r"(/rest/\d+/)[^/\s?#\"'<>]+")
# token de bot Telegram: <id numérico>:<35 chars> (também dentro de /bot<token>/)
_RX_TELEGRAM = re.compile(r"(?<!\d)(\d{6,12}):[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")
# parâmetros de querystring que carregam credencial (DOWNLOAD_URL assinada do
# Bitrix: /rest/download.json?auth=...&token=disk|...; URLs S3/CDN; OAuth)
_PARAMS_SECRETOS = (
    "auth", "token", "access_token", "refresh_token", "client_secret", "_esd",
    "sessid", "signature", "sig", "key", "api_key", "apikey", "secret",
    "password", "senha", "hash", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token",
)
_RX_QUERY = re.compile(
    r"(?i)([?&](?:%s)=)[^&#\s\"'<>]+" % "|".join(re.escape(p) for p in _PARAMS_SECRETOS))
# mesmos nomes FORA de querystring (texto livre de log, kwargs, form, dict/JSON):
#   "token=abc", "erro auth=x", "access_token='x'", '"refresh_token": "x"', "{'auth': 'x'}".
# Só a lista curta de OAuth/Bitrix (nomes genéricos como key/hash/sig dariam falso
# positivo em prosa). Nome EXATO: não pode estar colado a letra/dígito/_/-/. à
# esquerda (oauth=, mytoken=, x.token=) nem continuar à direita (tokens=, auth_type=).
# R3/A4: nomes de chave de API (TypeSafe sem prefixo apikey_) — específicos, sem risco de prosa.
_PARAMS_LIVRES = ("auth", "token", "access_token", "refresh_token", "client_secret",
                  "api_key", "apikey", "typesafe_key")
_NOMES_LIVRES = "|".join(re.escape(p) for p in _PARAMS_LIVRES)
_RX_PARAM_LIVRE = re.compile(
    r"(?i)(?<![\w.\-])((?:%s)[ \t]*=[ \t]*[\"']?)[^&#\s\"'<>,;)}\]]+" % _NOMES_LIVRES)
_RX_PARAM_CHAVE = re.compile(
    r"(?i)([\"'](?:%s)[\"'][ \t]*:[ \t]*([\"']))(?:(?!\2)[^\n\\]|\\.)+(?=\2)" % _NOMES_LIVRES)
# cabeçalhos: "X-Api-Key: v", '"X-Api-Key": "v"', "'x-api-key': 'v'"
_RX_APIKEY = re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+")
_RX_AUTHZ = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token)\s+)?)[^\"'\s,;}]+")
# R3/A3: "Bearer <valor>" fora de cabeçalho nomeado (ex.: ValueError do http.client
# 'Invalid header value b"Bearer <chave>\\n..."'). Independe do formato da chave.
_RX_BEARER = re.compile(r"(?i)\b(bearer\s+)[^\s\"',;}]+")
# chave TypeSafe/JEV em texto livre (formato "apikey_<...>"), fora de cabeçalho (J2)
_RX_APIKEY_TYPESAFE = re.compile(r"(?<![A-Za-z0-9_])(apikey_)[A-Za-z0-9_]+")

# CPF: 000.000.000-00, 00000000000 e variações parciais (698308525-63).
# Fronteiras: não pode estar colado a outro dígito (ids de 7-10 dígitos e
# timestamps de 10/13 dígitos não casam), nem ser parte decimal de número.
# IPs (200.100.150.25) não casam porque o último par exige hífen, "/", espaço ou nada.
# R3/A1: OCR também produz "000.000.000/00", "000.000.000 00" e "000 000 000 00".
# Esses separadores (/ e espaço) só são mascarados se houver pontos nos grupos
# (formato inequívoco de CPF) ou se o DV for válido — evita falso positivo em
# números quaisquer separados por espaço (telefone, processo, contagens).
_RX_CPF = re.compile(r"(?<![\d.,])\d{3}[. ]?\d{3}[. ]?\d{3}[-/ ]?\d{2}(?!\d|[.,]\d)")
_RX_CPF_CLASSICO = re.compile(r"^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$")
_RX_CPF_PONTUADO = re.compile(r"^\d{3}\.\d{3}\.\d{3}[-/ ]\d{2}$")
_RX_HEX = re.compile(r"^[0-9A-Fa-f]+$")

# --- senha gov.br / Meu INSS -------------------------------------------------
# Rótulo: "senha" ou "acesso (ao) gov.br", seguido de palavras de rótulo
# ("Senha de acesso ao GOV.BR", "Senha do MEU INSS", "Acesso ao gov.br (CPF e senha)").
_PALAVRA_ROTULO = (r"(?:de|do|da|ao|a|o|no|na|meu|minha|acesso|gov[ \t]*\.?[ \t]*br|govbr|"
                   r"gov|inss|cpf|e|senha|conta|app|aplicativo)")
# Repetição LIMITADA e sem ambiguidade de espaços: rótulo ilimitado com
# "[ \t]*" encadeados dava backtracking quadrático/exponencial em prosa longa.
_ROTULO = (r"(?:acesso[ \t]+(?:ao[ \t]+|do[ \t]+|no[ \t]+)?gov[ \t]*\.?[ \t]*br|senha)"
           r"(?:(?:[ \t]+|[ \t]*[()][ \t]*)" + _PALAVRA_ROTULO + r"(?![\w.])){0,8}\)?")
# tabela de variáveis do contrato: "Senha do MEU INSS   {UfCrm1725391896584}   valor"
_PLACEHOLDER_UF = r"(?:\{[^}\n]{0,40}\}[ \t]*)?"
# separador: ":", "=", ",", "：" ou " - "; aceita aspas de chave JSON e negrito
_SEP_CORE = r"(?:[\"'][ \t]*)?(?:[:=,：]|[ \t][-–](?=[ \t]))"
_NEGRITO = r"[ \t]*(?:\*{1,2}(?!\*))?[ \t]*"
# a tabela "{UfCrm...}" já serve de separador; separador explícito é opcional depois dela
_SEP = r"(?:" + _PLACEHOLDER_UF + _SEP_CORE + "|" + r"\{[^}\n]{0,40}\}[ \t]*" + ")" + _NEGRITO
# fim do valor: fim de linha, \n escapado (texto dentro de JSON), aspas, ou o
# começo de outro campo na mesma linha ("Senha: x  Email: y", "x • Bairro: y")
_FIM = (r"(?<=\S)(?=(?:(?<!\*)\*\*)?[ \t]*(?:$|\\[nrt]|\"|[ \t](?:[•|;]|\*\*(?=[^\s*])|"
        r"[A-Za-zÀ-ÿ][\wÀ-ÿ.()/-]{0,30}(?:[ \t]+[\wÀ-ÿ.()/-]{1,30}){0,3}[ \t]*[:：])))")
# fronteira à esquerda: não colado a letra — exceto o "n" de um \n escapado
_SEM_LETRA_ANTES = r"(?:(?<!\w)|(?<=\\[nrt]))"
_RX_SENHA_SEP = re.compile(
    r"(?P<pre>(?P<nis>\bnis[ \t]*/[ \t]*)?" + _SEM_LETRA_ANTES + _ROTULO + r"[ \t]*" + _SEP + r")"
    r"(?:\"(?P<vq>[^\"\n]*)\"|'(?P<vs>[^'\n]*)'|(?P<val>[^\s\"\\](?:[^\n\r\"\\]|\\(?![nrt]))*?)" + _FIM + r")",
    re.I | re.M)
# cabeçalho curto sem separador: "=SENHA Abc123" / "senha gov br Abc@1" — só no
# começo da linha e só se o valor for UM token com dígito ou símbolo (evita
# mascarar prosa como "Senha incorreta").
# O rótulo é "atômico" ((?=(?P<r>...))(?P=r)): sem isso o regex devolveria
# "GOV.BR" do rótulo para o valor.
_RX_SENHA_LINHA = re.compile(
    r"(?:^|(?<=\\n))(?P<pre>[^\w\n\\]*(?=(?P<rot>" + _ROTULO + r"))(?P=rot)[ \t]*)(?<=[ \t])"
    r"(?P<val>(?=[^\s\"\\]*[\d\W_])[^\s\"\\]+)(?=[ \t]*(?:$|\\[nr]|\"))",
    re.I | re.M)
_PLACEHOLDERS_SENHA = {MASCARA, SENHA_REMOVIDA.lower(), "s/senha", "sem senha", "[informada]", ""}


def _e_placeholder(v: str) -> bool:
    return v.strip().strip("*").strip().lower() in _PLACEHOLDERS_SENHA or set(v.strip()) <= {"*"}


def _substituir_senhas(texto: str, troca: str) -> str:
    def _sep(m: re.Match) -> str:
        for grupo, aspa in (("vq", '"'), ("vs", "'")):
            if m.group(grupo) is not None:
                if _e_placeholder(m.group(grupo)):
                    return m.group(0)
                return m.group("pre") + aspa + troca + aspa
        v = m.group("val")
        if _e_placeholder(v):
            return m.group(0)
        if m.group("nis") and " / " in v:
            # fichinha: "NIS/Senha do INSS:** <nis> / <senha>" -> preserva o NIS
            nis, senha = v.rsplit(" / ", 1)
            if _e_placeholder(senha):
                return m.group(0)
            return m.group("pre") + nis + " / " + troca
        return m.group("pre") + troca

    def _linha(m: re.Match) -> str:
        if _e_placeholder(m.group("val")):
            return m.group(0)
        return m.group("pre") + troca

    texto = _RX_SENHA_SEP.sub(_sep, texto)
    return _RX_SENHA_LINHA.sub(_linha, texto)


def _mascarar_cpfs(texto: str) -> str:
    def _cb(m: re.Match) -> str:
        ini, fim = m.span()
        # token alfanumérico ao redor: hash/sha hex longo não é CPF
        a, b = ini, fim
        while a > 0 and texto[a - 1].isalnum():
            a -= 1
        while b < len(texto) and texto[b].isalnum():
            b += 1
        token = texto[a:b]
        if len(token) >= 16 and token != m.group(0) and _RX_HEX.match(token):
            return m.group(0)
        bruto = m.group(0)
        if not (_RX_CPF_CLASSICO.match(bruto) or _RX_CPF_PONTUADO.match(bruto)
                or cpf_util.valido(re.sub(r"\D", "", bruto))):
            return bruto  # separador "/"/espaço sem pontos e DV inválido: não é CPF
        return cpf_util.mascarar(bruto)

    return _RX_CPF.sub(_cb, texto)


def _mascarar_urls(texto: str) -> str:
    texto = _RX_USERINFO.sub(r"\1***:***@", texto)
    texto = _RX_WEBHOOK.sub(r"\1" + MASCARA, texto)
    texto = _RX_QUERY.sub(r"\1" + MASCARA, texto)
    texto = _RX_PARAM_LIVRE.sub(r"\1" + MASCARA, texto)
    texto = _RX_PARAM_CHAVE.sub(r"\1" + MASCARA, texto)
    return _RX_TELEGRAM.sub(r"\1:" + MASCARA, texto)


def mascarar_url(url: str) -> str:
    """Mascara credenciais de uma URL mantendo host, caminho e id do webhook."""
    if not isinstance(url, str):
        url = str(url)
    return _mascarar_cpfs(_mascarar_urls(url))


def mascarar(texto: str) -> str:
    """Texto seguro para log/stdout/mensagem de erro. Idempotente."""
    if not isinstance(texto, str):
        texto = str(texto)
    if not texto:
        return texto
    texto = _RX_APIKEY_TYPESAFE.sub(r"\1" + MASCARA, texto)
    texto = _mascarar_urls(texto)
    texto = _RX_APIKEY.sub(r"\1" + MASCARA, texto)
    texto = _RX_AUTHZ.sub(r"\1" + MASCARA, texto)
    texto = _RX_BEARER.sub(r"\1" + MASCARA, texto)
    texto = _substituir_senhas(texto, MASCARA)
    return _mascarar_cpfs(texto)


def remover_senhas(texto: str) -> str:
    """Para PERSISTÊNCIA (estado.json, fila, backups): troca só o VALOR da senha
    gov.br/INSS por "[senha removida]". O resto do texto (nome, CPF, telefones,
    demais campos da ficha) fica intacto para a triagem. Idempotente."""
    if not isinstance(texto, str) or not texto:
        return texto
    return _substituir_senhas(texto, SENHA_REMOVIDA)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class FiltroMascara(logging.Filter):
    """Aplica `mascarar` na mensagem JÁ FORMATADA (record.getMessage()).

    Depois do filtro, record.msg é o texto mascarado e record.args fica vazio,
    então nenhum handler/formatter volta a interpolar o valor cru. Traceback
    (exc_info) e stack_info também são mascarados.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # formato inválido: não deixa o handler imprimir args crus
            msg = "%s %r" % (record.msg, record.args)
        record.msg = mascarar(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mascarar(record.exc_text)
        if record.stack_info:
            record.stack_info = mascarar(record.stack_info)
        return True


def instalar_filtro_log(logger: logging.Logger | None = None) -> None:
    """Instala FiltroMascara no logger (padrão: raiz) e em TODOS os seus handlers.

    Filtro de logger só vale para registros feitos nele; filtro de handler vale
    também para os que chegam por propagação de loggers filhos. Chame depois de
    configurar os handlers. Idempotente.
    """
    alvo = logger if logger is not None else logging.getLogger()
    for obj in [alvo, *alvo.handlers]:
        if not any(isinstance(f, FiltroMascara) for f in obj.filters):
            obj.addFilter(FiltroMascara())


# ---------------------------------------------------------------------------
# Segredos
# ---------------------------------------------------------------------------
class SegredoAusente(RuntimeError):
    """Segredo obrigatório não encontrado (ou recusado). Mensagem nunca traz o valor."""


class SegredoInseguro(SegredoAusente):
    """Arquivo de segredo legível por grupo/outros, de outro dono, ou em /tmp."""


class SegredoInvalido(SegredoAusente):
    """Valor fora do formato esperado (ex.: webhook Bitrix)."""


class SegredoRecusado(RuntimeError):
    """Modo ESTRITO (TRIAGEM_SEGREDOS_ESTRITO=1): arquivo de segredo inseguro.

    De propósito NÃO herda de SegredoAusente: consumidores que fazem
    `except SegredoAusente:` e caem para leitura direta de arquivo legado
    (vigia-bot-triagem, esperar_bot_liberar) não engolem a recusa — o processo
    para. Sem ESTRITO, os mesmos casos levantam SegredoInseguro (compatível).
    """


def modo_estrito() -> bool:
    return os.environ.get("TRIAGEM_SEGREDOS_ESTRITO") == "1"


def _inseguro(mensagem: str) -> Exception:
    return SegredoRecusado(mensagem + " [modo estrito]") if modo_estrito() else SegredoInseguro(mensagem)


NOMES_CONHECIDOS = ("webhook_bitrix", "vibecode_key", "telegram_token", "bot_token_100003",
                    "typesafe_key")
# diretórios graváveis por todos: nunca fonte de segredo (S06)
PREFIXOS_PROIBIDOS = ("/tmp", "/var/tmp", "/dev/shm", "/private/tmp", "/private/var/tmp")
_RX_NOME = re.compile(r"^[a-z0-9_]+$")
_RX_WEBHOOK_VALIDO = re.compile(
    r"^https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.bitrix24\.[a-z]{2,6}(?:\.[a-z]{2})?"
    r"/rest/\d+/[A-Za-z0-9_]+/$")


def _legados_padrao(nome: str) -> list[str]:
    """Caminhos legados EXPLÍCITOS em uso hoje (sem /tmp, sem outros projetos)."""
    padrao = {
        "vibecode_key": [caminhos.em_base("vibecode.key"), "/root/.vibecode/api_key"],
        "telegram_token": ["/root/.telegram/documentosexemplo.token"],
        # J2: chave TypeSafe/JEV (API paga) — nunca no repo
        "typesafe_key": [caminhos.em_base("typesafe.key"), "/root/.typesafe-jev.key"],
    }
    return padrao.get(nome, [])


def _legados(nome: str) -> list[str]:
    """TRIAGEM_SEGREDOS_LEGADOS (separado por ':') substitui os padrões se definida
    (vazia = nenhum legado). Entrada "nome=/arquivo" vale só para aquele segredo;
    entrada "/diretorio" procura /diretorio/<nome>."""
    bruto = os.environ.get("TRIAGEM_SEGREDOS_LEGADOS")
    if bruto is None:
        return _legados_padrao(nome)
    saida = []
    for item in bruto.split(":"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            alvo, _, caminho = item.partition("=")
            if alvo.strip() == nome and caminho.strip():
                saida.append(caminho.strip())
        else:
            saida.append(os.path.join(item, nome))
    return saida


def _em_dir_proibido(caminho: str) -> bool:
    for c in {os.path.abspath(caminho), os.path.realpath(caminho)}:
        for p in PREFIXOS_PROIBIDOS:
            if c == p or c.startswith(p.rstrip("/") + "/"):
                return True
    return False


def _ler_arquivo(nome: str, caminho: str, estrito: bool) -> str | None:
    if _em_dir_proibido(caminho):
        raise _inseguro(
            f"segredo {nome!r}: {caminho} fica em diretório temporário compartilhado; recusado")
    try:
        fd = os.open(caminho, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        raise _inseguro(f"segredo {nome!r}: {caminho} é diretório")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _inseguro(f"segredo {nome!r}: {caminho} não é arquivo regular")
        problema = None
        if st.st_mode & 0o077:
            problema = "permissão %s legível por grupo/outros (use chmod 600)" % oct(st.st_mode & 0o777)
        elif st.st_uid not in (0, os.geteuid()):
            problema = "dono uid=%d não é root nem o usuário do processo" % st.st_uid
        if problema:
            if estrito or modo_estrito():
                raise _inseguro(f"segredo {nome!r}: {caminho}: {problema}; recusado")
            log.warning("segredo %s em caminho legado %s: %s", nome, caminho, problema)
        dados = b""
        while True:
            pedaco = os.read(fd, 65536)
            if not pedaco:
                break
            dados += pedaco
    finally:
        os.close(fd)
    valor = dados.decode("utf-8", errors="strict").strip()
    if not valor:
        log.warning("segredo %s: arquivo %s está vazio; ignorado", nome, caminho)
        return None
    return valor


def _validar(nome: str, valor: str, origem: str) -> str:
    if nome == "webhook_bitrix":
        v = valor.strip().rstrip("/") + "/"
        if not _RX_WEBHOOK_VALIDO.match(v):
            raise SegredoInvalido(
                f"segredo 'webhook_bitrix' ({origem}) fora do formato "
                "https://<portal>.bitrix24.<tld>/rest/<id>/<token>/ (valor não exibido)")
        return v
    return valor


def ler_segredo(nome: str, obrigatorio: bool = True) -> str | None:
    """Lê um segredo sem varrer nada e sem logar o valor.

    Ordem: env TRIAGEM_<NOME>  ->  caminhos.SEGREDOS_DIR/<nome> (recusa se
    st_mode & 0o077 ou dono estranho)  ->  caminhos legados explícitos
    (TRIAGEM_SEGREDOS_LEGADOS ou os padrões de `_legados_padrao`), nunca sob
    /tmp ou /var/tmp. Legado com permissão aberta só gera aviso, a menos que
    TRIAGEM_SEGREDOS_ESTRITO=1. Com ESTRITO, TODA recusa por arquivo inseguro
    (principal, legado, /tmp, não-regular) vira SegredoRecusado, que não é
    SegredoAusente e portanto não cai no fallback de leitura direta dos consumidores.
    """
    if not isinstance(nome, str) or not _RX_NOME.match(nome):
        raise ValueError("nome de segredo inválido (use [a-z0-9_])")
    var = "TRIAGEM_" + nome.upper()
    valor = (os.environ.get(var) or "").strip()
    if valor:
        log.debug("segredo %s lido da variável %s", nome, var)
        return _validar(nome, valor, f"env {var}")

    principal = os.path.join(caminhos.SEGREDOS_DIR, nome)
    valor = _ler_arquivo(nome, principal, estrito=True)
    if valor:
        log.debug("segredo %s lido de %s", nome, principal)
        return _validar(nome, valor, principal)

    estrito_legado = modo_estrito()
    tentados = [f"env {var}", principal]
    for caminho in _legados(nome):
        tentados.append(caminho)
        valor = _ler_arquivo(nome, caminho, estrito=estrito_legado)
        if valor:
            log.info("segredo %s lido de caminho LEGADO %s; migre para %s", nome, caminho, principal)
            return _validar(nome, valor, caminho)

    if obrigatorio:
        raise SegredoAusente(f"segredo {nome!r} não encontrado; tentei: " + ", ".join(tentados))
    return None


# ---------------------------------------------------------------------------
# Temporários privados
# ---------------------------------------------------------------------------
def _dir_tmp_privado() -> str:
    d = caminhos.em_base("tmp")
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"{d} não é diretório real; recusado")
    if st.st_uid != os.geteuid():
        raise RuntimeError(f"{d} pertence a outro usuário (uid={st.st_uid}); recusado")
    if st.st_mode & 0o077:
        os.chmod(d, 0o700)
    return d


def arquivo_temp_privado(sufixo: str = "", prefixo: str = "triagem-") -> str:
    """Cria (vazio) e devolve um arquivo 0600 de nome único em
    caminhos.em_base("tmp") (diretório 0700). O_EXCL + O_NOFOLLOW: nunca
    reaproveita nem segue arquivo pré-existente. Apague com remover_seguro."""
    if os.sep in sufixo or os.sep in prefixo:
        raise ValueError("prefixo/sufixo não podem conter separador de diretório")
    d = _dir_tmp_privado()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(100):
        caminho = os.path.join(d, f"{prefixo}{secrets.token_hex(8)}{sufixo}")
        try:
            fd = os.open(caminho, flags, 0o600)
        except FileExistsError:
            continue
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return caminho
    raise RuntimeError("não consegui criar arquivo temporário único")


def remover_seguro(caminho: str) -> None:
    """Sobrescreve com zeros (se arquivo regular com um único link) e apaga.

    Não falha se não existir. Link simbólico: apaga só o link. Arquivo com
    vários hard links: só desvincula (zerar destruiria a outra cópia).
    Diretório: IsADirectoryError.
    """
    try:
        st = os.lstat(caminho)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(caminho)
    if stat.S_ISREG(st.st_mode) and st.st_size > 0 and st.st_nlink == 1:
        try:
            fd = os.open(caminho, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                zeros = b"\0" * 65536
                restante = st.st_size
                while restante > 0:
                    restante -= os.write(fd, zeros[:min(restante, len(zeros))])
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as e:
            log.debug("não consegui sobrescrever %s antes de apagar (%s)", caminho, type(e).__name__)
    try:
        os.unlink(caminho)
    except FileNotFoundError:
        pass
