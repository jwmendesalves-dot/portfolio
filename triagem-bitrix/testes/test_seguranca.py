"""seguranca — contrato (CONTRATOS.md). Nada aqui usa segredo/CPF/senha real:
tokens são strings inventadas, CPFs vêm de cpf_util.gerar_para_teste."""
import io
import logging
import os
import stat
import tempfile

import pytest

import caminhos
import cpf_util as C
import seguranca as S

CPF = C.gerar_para_teste(7)
CPF_FMT = C.formatar(CPF)
CPF2 = C.gerar_para_teste(8)
TOKEN_WH = "tok3nFicticio9x8y7z"
WEBHOOK = f"https://exemplo.bitrix24.com.br/rest/7/{TOKEN_WH}/"
TOKEN_TG = "1234567890:AAH" + "Zq9" * 10 + "x_"          # 35 chars depois do ':'
assert len(TOKEN_TG.split(":")[1]) == 35
CHAVE_API = "vk-ficticia-0123abcd"
PADRAO_PROIBIDOS = S.PREFIXOS_PROIBIDOS


def _sob(caminho, prefixo):
    c = os.path.realpath(str(caminho))
    return c == prefixo or c.startswith(prefixo + "/")


@pytest.fixture(autouse=True)
def _isola(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TRIAGEM_") and k not in ("TRIAGEM_BASE", "TRIAGEM_SEM_REDE"):
            monkeypatch.delenv(k)
    # sem legados padrão: teste nunca lê /root/.vibecode etc. da máquina
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "")
    etc = tmp_path / "etc"
    etc.mkdir(mode=0o700)
    monkeypatch.setattr(caminhos, "SEGREDOS_DIR", str(etc))
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path / "base"))
    # no Linux o tmp_path do pytest fica em /tmp: libera só para os testes que
    # não são sobre a recusa de /tmp (esses restauram PADRAO_PROIBIDOS)
    monkeypatch.setattr(S, "PREFIXOS_PROIBIDOS",
                        tuple(p for p in PADRAO_PROIBIDOS if not _sob(tmp_path, p)))
    return tmp_path


def _grava(caminho, conteudo, modo=0o600):
    caminho = str(caminho)
    with open(caminho, "w", encoding="utf-8") as fh:
        fh.write(conteudo)
    os.chmod(caminho, modo)
    return caminho


# ===================================================================== máscara
# (texto, valores que NÃO podem sobrar, trechos que DEVEM sobrar)
CASOS_MASCARA = [
    # webhook Bitrix: mantém portal e id, some o token
    (WEBHOOK, [TOKEN_WH], ["https://exemplo.bitrix24.com.br/rest/7/***/"]),
    (WEBHOOK + "crm.contact.list.json", [TOKEN_WH], ["/rest/7/***/crm.contact.list.json"]),
    (f"webhook {WEBHOOK.rstrip('/')} não validou", [TOKEN_WH], ["/rest/7/***", "não validou"]),
    (f"erro em {WEBHOOK}im.message.add?DIALOG_ID=chat9", [TOKEN_WH], ["DIALOG_ID=chat9"]),
    # X-Api-Key em cabeçalho, JSON e repr de dict
    (f"X-Api-Key: {CHAVE_API}", [CHAVE_API], ["X-Api-Key: ***"]),
    (f"x-api-key:{CHAVE_API}", [CHAVE_API], ["x-api-key:***"]),
    (f'{{"X-Api-Key": "{CHAVE_API}", "Accept": "json"}}', [CHAVE_API],
     ['"X-Api-Key": "***"', '"Accept": "json"']),
    (f"headers={{'X-Api-Key': '{CHAVE_API}'}}", [CHAVE_API], ["'X-Api-Key': '***'"]),
    (f"Authorization: Bearer {CHAVE_API}", [CHAVE_API], ["Authorization: Bearer ***"]),
    # token de bot Telegram (solto e na URL da API)
    # Z2: `token=` em texto livre agora esconde o valor inteiro (inclusive o id do bot)
    (f"token={TOKEN_TG}", [TOKEN_TG.split(":")[1], "1234567890"], ["token=***"]),
    (f"https://api.telegram.org/bot{TOKEN_TG}/getUpdates", [TOKEN_TG.split(":")[1]],
     ["https://api.telegram.org/bot1234567890:***/getUpdates"]),
    # DOWNLOAD_URL assinada do Bitrix e URLs de CDN
    ("https://exemplo.bitrix24.com.br/rest/download.json?auth=AUTHfict123&token=disk%7CaWQ9OTk5&fileId=55",
     ["AUTHfict123", "aWQ9OTk5"], ["download.json?auth=***&token=***&fileId=55"]),
    ("https://cdn.exemplo.com/f.pdf?X-Amz-Signature=abcdef0123&X-Amz-Credential=AKIAFICT&name=f.pdf",
     ["abcdef0123", "AKIAFICT"], ["name=f.pdf"]),
    ("https://usuario:SenhaURL9@host.exemplo/x", ["SenhaURL9", "usuario"], ["@host.exemplo/x"]),
    # CPF com e sem pontuação
    (f"cpf={CPF}", [CPF], [f"cpf=***.{CPF[3:6]}.{CPF[6:9]}-**"]),
    (f"CPF: {CPF_FMT} nome=Fulano", [CPF_FMT, CPF], ["nome=Fulano"]),
    (f'{{"cpf": "{CPF_FMT}"}}', [CPF_FMT], ['"cpf": "***.']),
    (f"CPF{CPF}", [CPF], ["CPF***."]),
    (f"{CPF[:9]}-{CPF[9:]}", [CPF[:9]], []),
    (f"duplicata {CPF} e {CPF2}", [CPF, CPF2], ["duplicata"]),
    (f"linha\n{CPF_FMT}\n", [CPF_FMT], []),
    # senha gov.br / INSS (formatos do grupo, da fichinha e da tabela do contrato)
    ("Senha: Fict@2024", ["Fict@2024"], ["Senha: ***"]),
    ("SENHA: Fict@2024.", ["Fict@2024"], ["SENHA: ***"]),
    ("=SENHA: Fict@2024.", ["Fict@2024"], ["=SENHA: ***"]),
    ("=SENHA Fict2024", ["Fict2024"], ["=SENHA ***"]),
    ("=ACESSO AO GOV.BR: fict123@", ["fict123@"], ["=ACESSO AO GOV.BR: ***"]),
    ("• Acesso ao gov.br (CPF e senha): Fict!2024", ["Fict!2024"], ["(CPF e senha): ***"]),
    ("Senha gov br: Fict@99", ["Fict@99"], ["Senha gov br: ***"]),
    ("senha gov.br Fict@99", ["Fict@99"], []),
    ("🔐Senha de acesso ao GOV.BR,* 110000Zz@", ["110000Zz@"], ["GOV.BR,* ***"]),
    ("Senha do MEU INSS      {UfCrm1725391896584}           Flor99@", ["Flor99@"],
     ["{UfCrm1725391896584}"]),
    ("• **NIS/Senha do INSS:** 12345 / Fict@1", ["Fict@1"], ["12345 / ***"]),
    ("• **Senha:** Fict123**", ["Fict123"], ["**Senha:**"]),
    ("senha - Fict12", ["Fict12"], ["senha - ***"]),
    ('{"senha": "Fict@1", "nome": "Maria"}', ["Fict@1"], ['"senha": "***"', '"nome": "Maria"']),
    ("{'senha': 'Fict@1', 'nome': 'Maria'}", ["Fict@1"], ["'nome': 'Maria'"]),
    ('{"texto": "Nome: Maria\\nSenha: Fict@1\\nCidade: Manaus"}', ["Fict@1"],
     ["Nome: Maria\\n", "\\nCidade: Manaus"]),
    ("Senha: Fict@123 Email: maria@example.com", ["Fict@123"], ["Email: maria@example.com"]),
    ("Nome: Maria\nSenha: duas palavras 12\nCidade: Manaus", ["duas palavras"],
     ["Nome: Maria\n", "\nCidade: Manaus"]),
]


@pytest.mark.parametrize("texto,proibidos,mantidos", CASOS_MASCARA)
def test_mascarar_tabela(texto, proibidos, mantidos):
    m = S.mascarar(texto)
    for p in proibidos:
        assert p not in m, (texto, m)
    for t in mantidos:
        assert t in m, (texto, m)
    assert S.mascarar(m) == m, "mascarar deve ser idempotente"


# o que NÃO pode ser mascarado (falsos positivos)
NAO_MASCARAR = [
    "mensagem 1234567 arquivo 12345678 disco 123456789 id 1234567890",   # ids de 7-10 dígitos
    "ts=1731171234 ms=1731171234567",                                    # timestamps 10/13
    "agora 20261109155230 e 202611091552",                               # carimbos 14/12 dígitos
    "chat -1001234567890",                                               # chat id Telegram
    "ip 200.100.150.25",
    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "uuid 550e8400-e29b-41d4-a716-446655440000",
    "valor 0.12345678901 e 1234567890.1",
    "LOTE-SIG-20261026-1944-a3cd2300",
    "IMG-20261026-WA0001.jpg",
    "data 09/11/2026 R$ 1.234,56",
    "https://exemplo.bitrix24.com.br/rest/download.json?fileId=55&name=a.pdf",
    "https://vibecode.bitrix24.com/v1/files/123456/download",
    "https://exemplo.bitrix24.com.br/rest/crm.contact.list.json",
    "cliente esqueceu a senha do gov.br",
    "Pedir senha ao cliente amanhã",
    "Senha incorreta",
    "S/SENHA",
    "• **NIS/Senha do INSS:** S/SENHA / S/SENHA",
    "• **NIS/Senha do INSS:**",
    "Senha: [senha removida]",
    "Senha: ***",
    "10:30:45 tudo certo",
]


@pytest.mark.parametrize("texto", NAO_MASCARAR)
def test_mascarar_nao_mexe_em_falso_positivo(texto):
    assert S.mascarar(texto) == texto


def test_telefone_11_digitos_sem_pontuacao_e_mascarado():
    # comportamento documentado: 11 dígitos colados são tratados como CPF
    # (celular com DDD também é dado pessoal); telefone formatado não muda.
    assert "92991234567" not in S.mascarar("tel 92991234567")
    assert S.mascarar("tel (92) 99123-4567") == "tel (92) 99123-4567"


def test_mascarar_tipos():
    assert S.mascarar("") == ""
    assert S.mascarar(None) == "None"
    assert S.mascarar(12345678901) == "***.456.789-**"


@pytest.mark.parametrize("texto", [
    "senha senha senha de do da ao " * 5000,       # travava ~150 s antes do rótulo limitado
    "acesso ao gov br " * 8000,
    "Senha: " + "a" * 50000,
    "senha:" * 20000,
    "NIS/senha / " * 8000,
])
def test_mascarar_sem_backtracking_catastrofico(texto):
    import time
    t = time.monotonic()
    S.mascarar(texto)
    S.remover_senhas(texto)
    assert time.monotonic() - t < 5


def test_mascarar_url():
    u = S.mascarar_url(WEBHOOK + "disk.file.get.json?auth=XYZ1&id=9")
    assert TOKEN_WH not in u and "XYZ1" not in u
    assert u.startswith("https://exemplo.bitrix24.com.br/rest/7/***/disk.file.get.json")
    assert "id=9" in u
    assert S.mascarar_url("https://exemplo.com/a?b=1") == "https://exemplo.com/a?b=1"


# ================================================================ remover_senhas
def test_remover_senhas_preserva_resto_da_ficha():
    ficha = (
        "*DADOS NECESSÁRIOS PARA DAR A ENTRADA*\n"
        "Nome: Maria Teste Ficticia\n"
        f"CPF: {CPF_FMT}\n"
        "E-mail: maria.teste@example.com\n"
        "Senha: Fict@2024\n"
        "Cidade: Manaus\n"
        "Telefone 1: (92) 99999-0000\n"
    )
    r = S.remover_senhas(ficha)
    assert "Fict@2024" not in r
    assert r == ficha.replace("Fict@2024", "[senha removida]")
    assert CPF_FMT in r, "remover_senhas não mascara CPF (é para a triagem ler)"
    assert S.remover_senhas(r) == r


@pytest.mark.parametrize("texto,senha", [
    ("=SENHA: Fict@2024.", "Fict@2024."),
    ("=SENHA Fict2024", "Fict2024"),
    ("=ACESSO AO GOV.BR: fict123@", "fict123@"),
    ("🔐Senha de acesso ao GOV.BR,* 110000Zz@", "110000Zz@"),
    ("Senha do MEU INSS      {UfCrm1725391896584}           Flor99@", "Flor99@"),
    ("• **NIS/Senha do INSS:** 12345 / Fict@1", "Fict@1"),
    ("• Acesso ao gov.br (CPF e senha): Fict!2024", "Fict!2024"),
])
def test_remover_senhas_formatos(texto, senha):
    r = S.remover_senhas(texto)
    assert senha not in r and "[senha removida]" in r
    assert r == texto.replace(senha, "[senha removida]")


def test_remover_senhas_nao_mexe_em_prosa_e_placeholders():
    for t in ["cliente esqueceu a senha do gov.br", "Senha incorreta", "S/SENHA",
              "• **NIS/Senha do INSS:** 12345 / S/SENHA", f"CPF {CPF}"]:
        assert S.remover_senhas(t) == t
    assert S.remover_senhas(None) is None
    assert S.remover_senhas("") == ""


def test_remover_senhas_integra_com_parse_ficha():
    """A ficha sem senha continua parseável: só o campo senha muda."""
    import lote_para_job as L
    fichas = [
        ("Nome: Maria Teste Ficticia\nCPF: %s\nE-mail: maria.teste@example.com\n"
         "Senha: Fict@2024\nCidade: Manaus\n" % CPF_FMT),
        ("= MARIA TESTE FICTICIA\n= CPF: %s\n= AUX DOENÇA FACULTATIVO\n"
         "=SENHA: Fict@2024.\nDATA:26/10/2026" % CPF_FMT),
        ("Nome completo: Maria Teste Ficticia\nCPF: %s\n"
         "🔐Senha de acesso ao GOV.BR,* Fict@2024\nCidade: Manaus\n" % CPF_FMT),
    ]
    for f in fichas:
        antes = L.parse_ficha(f)
        depois = L.parse_ficha(S.remover_senhas(f))
        assert antes.get("senha") and "Fict@2024" in antes["senha"]
        assert depois.get("senha") == "[senha removida]"
        antes.pop("senha"), depois.pop("senha")
        assert antes == depois


# ======================================================================= log
def test_filtro_log_mascara_args(caplog):
    lg = logging.getLogger("teste.seg.args")
    S.instalar_filtro_log(lg)
    S.instalar_filtro_log(lg)
    assert sum(isinstance(f, S.FiltroMascara) for f in lg.filters) == 1
    with caplog.at_level(logging.DEBUG, logger="teste.seg.args"):
        lg.warning("webhook %s não validou (%s)", WEBHOOK, "HTTP 401")
        lg.info("n=%d cpf=%s senha: %s", 3, CPF, "Fict@1")
        lg.info("%(u)s ok", {"u": WEBHOOK})
        lg.info("100%% feito para %s", f"X-Api-Key: {CHAVE_API}")
    texto = caplog.text
    for p in (TOKEN_WH, CPF, "Fict@1", CHAVE_API):
        assert p not in texto
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs[0] == "webhook https://exemplo.bitrix24.com.br/rest/7/***/ não validou (HTTP 401)"
    assert msgs[1].startswith("n=3 cpf=***.")
    assert msgs[3] == "100% feito para X-Api-Key: ***"
    assert all(r.args is None for r in caplog.records)


def test_filtro_log_formato_quebrado_nao_vaza(caplog):
    lg = logging.getLogger("teste.seg.quebrado")
    S.instalar_filtro_log(lg)
    with caplog.at_level(logging.INFO, logger="teste.seg.quebrado"):
        lg.info("faltam args %s %s", WEBHOOK)
    assert TOKEN_WH not in caplog.text and "/rest/7/***" in caplog.text


def test_filtro_log_traceback(caplog):
    lg = logging.getLogger("teste.seg.exc")
    S.instalar_filtro_log(lg)
    with caplog.at_level(logging.ERROR, logger="teste.seg.exc"):
        try:
            raise RuntimeError(f"falhou {WEBHOOK} cpf {CPF_FMT}")
        except RuntimeError:
            lg.exception("erro")
    assert "RuntimeError" in caplog.text
    assert TOKEN_WH not in caplog.text and CPF_FMT not in caplog.text


def test_filtro_no_handler_pega_logger_filho():
    pai = logging.getLogger("teste.seg.pai")
    pai.propagate = False
    saida = io.StringIO()
    h = logging.StreamHandler(saida)
    pai.addHandler(h)
    try:
        S.instalar_filtro_log(pai)
        assert any(isinstance(f, S.FiltroMascara) for f in h.filters)
        logging.getLogger("teste.seg.pai.filho").warning("url %s", WEBHOOK)
        assert TOKEN_WH not in saida.getvalue() and "/rest/7/***/" in saida.getvalue()
    finally:
        pai.removeHandler(h)


def test_instalar_no_root_por_padrao():
    raiz = logging.getLogger()
    antes = list(raiz.filters)
    try:
        S.instalar_filtro_log()
        assert any(isinstance(f, S.FiltroMascara) for f in raiz.filters)
    finally:
        for f in list(raiz.filters):
            if f not in antes:
                raiz.removeFilter(f)
        for h in raiz.handlers:
            for f in list(h.filters):
                if isinstance(f, S.FiltroMascara):
                    h.removeFilter(f)


# ================================================================== segredos
def test_env_tem_prioridade(_isola, monkeypatch):
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "vibecode_key"), "do-arquivo")
    monkeypatch.setenv("TRIAGEM_VIBECODE_KEY", "  da-env \n")
    assert S.ler_segredo("vibecode_key") == "da-env"


def test_arquivo_0600_em_segredos_dir(caplog):
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "vibecode_key"), CHAVE_API + "\n")
    with caplog.at_level(logging.DEBUG, logger="triagem.seguranca"):
        assert S.ler_segredo("vibecode_key") == CHAVE_API
    assert CHAVE_API not in caplog.text


@pytest.mark.parametrize("modo", [0o640, 0o604, 0o644, 0o660])
def test_arquivo_legivel_por_grupo_ou_outros_e_recusado(modo):
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "vibecode_key"), CHAVE_API, modo)
    for obrig in (True, False):
        with pytest.raises(S.SegredoInseguro) as e:
            S.ler_segredo("vibecode_key", obrigatorio=obrig)
        assert "chmod 600" in str(e.value) and CHAVE_API not in str(e.value)
    assert issubclass(S.SegredoInseguro, S.SegredoAusente)


def test_ausente():
    assert S.ler_segredo("telegram_token", obrigatorio=False) is None
    with pytest.raises(S.SegredoAusente) as e:
        S.ler_segredo("telegram_token")
    assert "TRIAGEM_TELEGRAM_TOKEN" in str(e.value)


def test_arquivo_vazio_conta_como_ausente():
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "telegram_token"), "  \n")
    assert S.ler_segredo("telegram_token", obrigatorio=False) is None


@pytest.mark.parametrize("nome", ["../etc/passwd", "a/b", "", "Maiuscula", "x.y"])
def test_nome_invalido(nome):
    with pytest.raises(ValueError):
        S.ler_segredo(nome)


def test_legado_explicito_por_nome_e_por_diretorio(_isola, monkeypatch, caplog):
    leg = _isola / "legado"
    leg.mkdir()
    arq = _grava(leg / "vibecode.key", CHAVE_API)
    _grava(leg / "telegram_token", TOKEN_TG)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", f"vibecode_key={arq}:{leg}")
    with caplog.at_level(logging.DEBUG, logger="triagem.seguranca"):
        assert S.ler_segredo("vibecode_key") == CHAVE_API
        assert S.ler_segredo("telegram_token") == TOKEN_TG
    assert CHAVE_API not in caplog.text and TOKEN_TG not in caplog.text
    # entrada nome=arquivo não vale para outro segredo
    assert S.ler_segredo("bot_token_100003", obrigatorio=False) is None


def test_legado_inseguro_avisa_e_estrito_recusa(_isola, monkeypatch, caplog):
    arq = _grava(_isola / "vibecode.key", CHAVE_API, 0o644)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", f"vibecode_key={arq}")
    with caplog.at_level(logging.WARNING, logger="triagem.seguranca"):
        assert S.ler_segredo("vibecode_key") == CHAVE_API
    assert "legível por grupo/outros" in caplog.text
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    with pytest.raises(S.SegredoRecusado) as e:
        S.ler_segredo("vibecode_key")
    assert CHAVE_API not in str(e.value)
    # não é SegredoAusente: `except SegredoAusente` + leitura direta não engole
    assert not isinstance(e.value, S.SegredoAusente)


def test_principal_vence_legado(_isola, monkeypatch):
    arq = _grava(_isola / "velho", "valor-velho")
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "vibecode_key"), "valor-novo")
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", f"vibecode_key={arq}")
    assert S.ler_segredo("vibecode_key") == "valor-novo"


@pytest.mark.parametrize("prefixo", ["/tmp", "/var/tmp"])
def test_legado_em_tmp_recusado(monkeypatch, prefixo):
    monkeypatch.setattr(S, "PREFIXOS_PROIBIDOS", PADRAO_PROIBIDOS)
    monkeypatch.setattr(caminhos, "SEGREDOS_DIR", "/nao-existe-triagem-segredos")
    fd, arq = tempfile.mkstemp(dir=prefixo, prefix="b24_webhook-teste-")
    try:
        os.write(fd, WEBHOOK.encode())
        os.close(fd)
        os.chmod(arq, 0o600)
        monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", f"webhook_bitrix={arq}")
        with pytest.raises(S.SegredoInseguro) as e:
            S.ler_segredo("webhook_bitrix", obrigatorio=False)
        assert arq in str(e.value) and TOKEN_WH not in str(e.value)
        # diretório legado em /tmp também
        monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", prefixo)
        with pytest.raises(S.SegredoInseguro):
            S.ler_segredo("webhook_bitrix")
    finally:
        os.unlink(arq)


def test_symlink_para_tmp_recusado(_isola, monkeypatch):
    monkeypatch.setattr(S, "PREFIXOS_PROIBIDOS", PADRAO_PROIBIDOS)
    monkeypatch.setattr(caminhos, "SEGREDOS_DIR", "/nao-existe-triagem-segredos")
    fd, alvo = tempfile.mkstemp(dir="/tmp", prefix="segredo-teste-")
    os.close(fd)
    link = _isola / "link-legado"
    os.symlink(alvo, link)
    try:
        monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", f"vibecode_key={link}")
        with pytest.raises(S.SegredoInseguro):
            S.ler_segredo("vibecode_key")
    finally:
        os.unlink(alvo)


def test_legados_padrao_sem_tmp_e_sem_outros_projetos(monkeypatch):
    monkeypatch.delenv("TRIAGEM_SEGREDOS_LEGADOS")
    monkeypatch.setattr(caminhos, "BASE", "/opt/triagem-bitrix")   # padrão de produção
    todos = [c for n in S.NOMES_CONHECIDOS for c in S._legados(n)]
    assert caminhos.em_base("vibecode.key") in S._legados("vibecode_key")
    assert S._legados("webhook_bitrix") == []   # nada de varrer PrevSync/mcp nem /tmp/b24_webhook.txt
    monkeypatch.setattr(S, "PREFIXOS_PROIBIDOS", PADRAO_PROIBIDOS)
    for c in todos:
        assert not S._em_dir_proibido(c)
        assert "PrevSync" not in c and "mcp" not in c and "leads-doenca" not in c


def test_legados_env_vazia_desliga_padroes(monkeypatch):
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "")
    assert S._legados("vibecode_key") == []


@pytest.mark.parametrize("valor,esperado", [
    (WEBHOOK, WEBHOOK),
    (WEBHOOK.rstrip("/"), WEBHOOK),
    (WEBHOOK + "/", WEBHOOK),
    (f"  {WEBHOOK}\n", WEBHOOK),
    (f"https://exemplo.bitrix24.com/rest/1/{TOKEN_WH}/", f"https://exemplo.bitrix24.com/rest/1/{TOKEN_WH}/"),
])
def test_webhook_valido_normaliza_barra(monkeypatch, valor, esperado):
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", valor)
    assert S.ler_segredo("webhook_bitrix") == esperado


@pytest.mark.parametrize("valor", [
    f"http://exemplo.bitrix24.com.br/rest/7/{TOKEN_WH}/",          # sem TLS
    f"https://exemplo.evil.com/rest/7/{TOKEN_WH}/",                 # outro host
    f"https://exemplo.bitrix24.com.br.evil.com/rest/7/{TOKEN_WH}/",
    f"https://exemplo.bitrix24.com.br/rest/x/{TOKEN_WH}/",
    f"https://exemplo.bitrix24.com.br/rest/7/{TOKEN_WH}/crm.contact.list.json",
    "https://exemplo.bitrix24.com.br/rest/7/",
    CHAVE_API,
])
def test_webhook_invalido(monkeypatch, valor):
    monkeypatch.setenv("TRIAGEM_WEBHOOK_BITRIX", valor)
    with pytest.raises(S.SegredoInvalido) as e:
        S.ler_segredo("webhook_bitrix")
    assert TOKEN_WH not in str(e.value) and CHAVE_API not in str(e.value)


def test_webhook_de_arquivo_tambem_valida():
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "webhook_bitrix"), WEBHOOK.rstrip("/") + "\n")
    assert S.ler_segredo("webhook_bitrix") == WEBHOOK


# ============================================================ temporários
def _modo(c):
    return stat.S_IMODE(os.lstat(c).st_mode)


def test_arquivo_temp_privado():
    a = S.arquivo_temp_privado(sufixo=".json")
    b = S.arquivo_temp_privado(prefixo="x-")
    d = caminhos.em_base("tmp")
    assert os.path.dirname(a) == d and os.path.dirname(b) == d
    assert a != b and a.endswith(".json") and os.path.basename(a).startswith("triagem-")
    assert os.path.basename(b).startswith("x-")
    assert _modo(a) == 0o600 and _modo(b) == 0o600 and _modo(d) == 0o700
    assert os.path.getsize(a) == 0


def test_arquivo_temp_privado_corrige_dir_aberto():
    d = caminhos.em_base("tmp")
    os.makedirs(d)
    os.chmod(d, 0o755)
    S.arquivo_temp_privado()
    assert _modo(d) == 0o700


def test_arquivo_temp_privado_umask_permissiva():
    velha = os.umask(0)
    try:
        a = S.arquivo_temp_privado()
    finally:
        os.umask(velha)
    assert _modo(a) == 0o600


def test_arquivo_temp_privado_recusa_symlink(_isola):
    os.makedirs(caminhos.BASE)
    outro = _isola / "outro"
    outro.mkdir()
    os.symlink(outro, caminhos.em_base("tmp"))
    with pytest.raises(RuntimeError):
        S.arquivo_temp_privado()


@pytest.mark.parametrize("kw", [{"prefixo": "a/b"}, {"sufixo": "/x"}])
def test_arquivo_temp_privado_nome_com_barra(kw):
    with pytest.raises(ValueError):
        S.arquivo_temp_privado(**kw)


def test_remover_seguro_zera_e_apaga():
    a = S.arquivo_temp_privado()
    with open(a, "wb") as fh:
        fh.write(b"conteudo-sensivel" * 1000)
    fd = os.open(a, os.O_RDONLY)          # mantém o inode vivo para conferir
    try:
        S.remover_seguro(a)
        assert not os.path.exists(a)
        os.lseek(fd, 0, 0)
        dados = os.read(fd, 1 << 20)
        assert dados == b"\0" * len(b"conteudo-sensivel" * 1000)
    finally:
        os.close(fd)


def test_remover_seguro_inexistente_nao_falha(_isola):
    S.remover_seguro(str(_isola / "nao-existe"))


def test_remover_seguro_hardlink_nao_destroi_outra_copia(_isola):
    a = _grava(_isola / "a", "documento")
    b = str(_isola / "b")
    os.link(a, b)
    S.remover_seguro(a)
    assert not os.path.exists(a)
    with open(b, encoding="utf-8") as fh:
        assert fh.read() == "documento"


def test_remover_seguro_symlink_apaga_so_o_link(_isola):
    alvo = _grava(_isola / "alvo", "documento")
    link = str(_isola / "link")
    os.symlink(alvo, link)
    S.remover_seguro(link)
    assert not os.path.lexists(link)
    with open(alvo, encoding="utf-8") as fh:
        assert fh.read() == "documento"


def test_remover_seguro_diretorio(_isola):
    with pytest.raises(IsADirectoryError):
        S.remover_seguro(str(_isola))


# ============================================ Z2: token=/auth= fora de querystring
TOK = "tk9Ficticio8Zz"


@pytest.mark.parametrize("texto", [
    f"token={TOK}", f"token={TOK}&x=1", f"erro auth={TOK} ao chamar", f"access_token={TOK}",
    f"refresh_token={TOK};", f"client_secret={TOK}", f"TOKEN={TOK}", f"token = {TOK}",
    f"dict(auth='{TOK}')", f'{{"token": "{TOK}"}}', f"{{'refresh_token': '{TOK}'}}",
    f'"client_secret":"{TOK}"', f"form: auth={TOK}&token=disk|{TOK}",
    f"(access_token={TOK})", f"?token={TOK}",
])
def test_mascara_param_secreto_em_texto_livre(texto):
    saida = S.mascarar(texto)
    assert TOK not in saida, saida
    assert S.MASCARA in saida
    assert S.mascarar(saida) == saida                       # idempotente
    assert TOK not in S.mascarar_url(texto)


@pytest.mark.parametrize("texto", [
    "tokens=5", "mytoken=abc1", "oauth=1", "auth_type=webhook", "token_count=3",
    "x.token=abc", "x-auth=abc", "author=Ana", "token expirado, renovar", "auth falhou",
    '"auth": null', '"token": 123', "authorization_code_flow=on", "client_secrets=2",
    "access_tokens=4",
])
def test_param_secreto_falso_positivo_nao_mascara(texto):
    assert S.mascarar(texto) == texto


def test_param_secreto_preserva_resto_da_linha():
    assert S.mascarar(f"GET x token={TOK} status=200 lote=L1") == "GET x token=*** status=200 lote=L1"


# ============================================ Z2: modo estrito recusa em todos os caminhos
def test_estrito_recusa_principal_inseguro_com_segredorecusado(monkeypatch):
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "vibecode_key"), CHAVE_API, 0o644)
    with pytest.raises(S.SegredoInseguro):                  # sem estrito: compatível
        S.ler_segredo("vibecode_key", obrigatorio=False)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    for obrig in (True, False):
        with pytest.raises(S.SegredoRecusado) as e:
            S.ler_segredo("vibecode_key", obrigatorio=obrig)
        assert CHAVE_API not in str(e.value) and "estrito" in str(e.value)


def test_estrito_recusa_legado_nao_regular_e_diretorio(_isola, monkeypatch):
    d = _isola / "dir-legado"
    (d / "vibecode_key").mkdir(parents=True)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", str(d))
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    with pytest.raises(S.SegredoRecusado):
        S.ler_segredo("vibecode_key", obrigatorio=False)


def test_estrito_recusa_legado_em_tmp(monkeypatch):
    monkeypatch.setattr(S, "PREFIXOS_PROIBIDOS", PADRAO_PROIBIDOS)
    monkeypatch.setattr(caminhos, "SEGREDOS_DIR", "/nao-existe-triagem-segredos")
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "vibecode_key=/tmp/triagem-z2-nao-existe")
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    with pytest.raises(S.SegredoRecusado):
        S.ler_segredo("vibecode_key", obrigatorio=False)


def test_estrito_fura_fallback_de_consumidor_que_captura_ausente(_isola, monkeypatch):
    """Padrão de vigia/esperar_bot: `except SegredoAusente: open(legado)`. Com
    ESTRITO a recusa sobe e o arquivo aberto não é lido."""
    arq = _grava(_isola / "vibecode.key", CHAVE_API, 0o644)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", f"vibecode_key={arq}")

    def consumidor():
        try:
            return S.ler_segredo("vibecode_key", obrigatorio=False)
        except S.SegredoAusente:
            with open(arq, encoding="utf-8") as fh:
                return fh.read().strip()

    assert consumidor() == CHAVE_API                        # padrão: aviso e lê
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    with pytest.raises(S.SegredoRecusado):
        consumidor()
    os.chmod(arq, 0o600)                                    # depois do chmod 600: lê normal
    assert consumidor() == CHAVE_API


def test_estrito_aceita_arquivo_seguro_e_ausente_continua_ausente(monkeypatch):
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    assert S.ler_segredo("telegram_token", obrigatorio=False) is None
    with pytest.raises(S.SegredoAusente):
        S.ler_segredo("telegram_token")
    _grava(os.path.join(caminhos.SEGREDOS_DIR, "telegram_token"), TOKEN_TG)
    assert S.ler_segredo("telegram_token") == TOKEN_TG


# --- J2: chave TypeSafe/JEV ---------------------------------------------------
CHAVE_TS = "apikey_teste_FICTICIA_ab12CD34_" + "9" * 11      # 11 dígitos no fim: não pode virar "CPF"


@pytest.mark.parametrize("texto", [
    f"falha com chave {CHAVE_TS} no lote",
    f"Authorization: Bearer {CHAVE_TS}",
    f"{{'Authorization': 'Bearer {CHAVE_TS}'}}",
    f'"authorization": "Bearer {CHAVE_TS}"',
    f"cabecalhos={{'Authorization': 'Bearer {CHAVE_TS}', 'Accept': 'x'}}",
])
def test_mascarar_chave_typesafe(texto):
    saida = S.mascarar(texto)
    assert CHAVE_TS not in saida and "FICTICIA" not in saida
    assert S.mascarar(saida) == saida                       # idempotente


def test_mascarar_apikey_nao_pega_palavra_colada():
    assert S.mascarar("minha_apikey_config") == "minha_apikey_config"
    assert S.mascarar("x apikey_abc y") == "x apikey_*** y"


def test_typesafe_key_legado_e_estrito(tmp_path, monkeypatch):
    arq = tmp_path / "typesafe.key"
    arq.write_text(CHAVE_TS)
    os.chmod(arq, 0o644)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", f"typesafe_key={arq}")
    assert S.ler_segredo("typesafe_key") == CHAVE_TS        # sem estrito: aviso e lê
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    with pytest.raises(S.SegredoRecusado) as ei:
        S.ler_segredo("typesafe_key")
    assert CHAVE_TS not in str(ei.value)
