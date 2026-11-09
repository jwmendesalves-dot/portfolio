# -*- coding: utf-8 -*-
"""
Testes de rede_bitrix.py (frente R): classificação de erro do Bitrix pelo campo
`error` do corpo, política de retry, limitador por host ENTRE PROCESSOS e
fábrica de disjuntores. Nenhuma chamada de rede (conftest bloqueia socket).
"""
import concurrent.futures
import email.message
import http.client
import io
import atexit
import json
import multiprocessing
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error

import pytest

from conftest import RAIZ

sys.path.insert(0, RAIZ)

import caminhos                                   # noqa: E402
import circuit_breaker                            # noqa: E402
import rede_bitrix as rb                          # noqa: E402
from rede_bitrix import Classe                    # noqa: E402

AGORA = 1_800_000_000.0
WEBHOOK_FALSO = "https://portal.exemplo.test/rest/1/tokenSecretoXYZ123/disk.file.get.json"


def _corpo(codigo, desc="descricao qualquer", **extra):
    d = {"error": codigo, "error_description": desc}
    d.update(extra)
    return json.dumps(d)


# ---------------------------------------------------------------- classificar
def test_query_limit_exceeded_e_rate_limit_repetivel():
    e = rb.classificar(503, _corpo("QUERY_LIMIT_EXCEEDED", "Too many requests"))
    assert e.classe == Classe.RATE_LIMIT
    assert e.repetir and not e.permanente
    assert e.espera_s > 0 and e.pausa_rodada
    assert e.codigo == "QUERY_LIMIT_EXCEEDED"


def test_codigo_do_corpo_vence_o_status_http():
    # batch: erro de subconsulta chega com 200 (error-codes.md)
    assert rb.classificar(200, _corpo("ACCESS_DENIED")).classe == Classe.ACESSO
    # 503 com corpo ACCESS_DENIED não é rate limit
    assert rb.classificar(503, _corpo("ACCESS_DENIED")).classe == Classe.ACESSO
    # 400 com QUERY_LIMIT_EXCEEDED é rate limit
    assert rb.classificar(400, _corpo("QUERY_LIMIT_EXCEEDED")).classe == Classe.RATE_LIMIT


def test_nao_ramifica_por_error_description():
    # descrição citando outro código não muda a classe (doc: ramificar por `error`)
    e = rb.classificar(403, _corpo("ACCESS_DENIED", "QUERY_LIMIT_EXCEEDED parece mas não é"))
    assert e.classe == Classe.ACESSO and e.permanente


def test_operation_time_limit_espera_operating_reset_at_do_corpo():
    corpo = _corpo("OPERATION_TIME_LIMIT", time={"operating_reset_at": AGORA + 120})
    e = rb.classificar(429, corpo, agora=AGORA)
    assert e.classe == Classe.OPERATING and e.repetir and not e.permanente
    assert e.espera_s == pytest.approx(120, abs=1)


def test_operation_time_limit_prefere_retry_after_e_parametro_reset():
    e = rb.classificar(429, _corpo("OPERATION_TIME_LIMIT"), retry_after="30", agora=AGORA)
    assert e.espera_s == pytest.approx(30)
    e2 = rb.classificar(429, _corpo("OPERATION_TIME_LIMIT"), operating_reset_at=AGORA + 200, agora=AGORA)
    assert e2.espera_s == pytest.approx(200)
    e3 = rb.classificar(429, "", agora=AGORA)        # sem nada: cesto inteiro (60 s)
    assert e3.classe == Classe.OPERATING and e3.espera_s == pytest.approx(60)


def test_operating_reset_at_de_resposta_boa():
    resp = {"result": [], "time": {"operating_reset_at": AGORA + 33, "operating": 2.1}}
    assert rb.operating_reset_at_de(resp) == AGORA + 33
    assert rb.operating_reset_at_de({"result": []}) is None


@pytest.mark.parametrize("status,codigo", [
    (401, "ACCESS_DENIED"), (403, "ACCESS_DENIED"), (401, "insufficient_scope"),
    (401, "INVALID_CREDENTIALS"), (400, "WRONG_AUTH_TYPE"), (401, None), (403, None),
    (404, "ERROR_METHOD_NOT_FOUND"),
])
def test_acesso_e_permanente_e_nunca_repete(status, codigo):
    corpo = _corpo(codigo) if codigo else ""
    e = rb.classificar(status, corpo)
    assert e.classe == Classe.ACESSO
    assert e.permanente and not e.repetir
    assert rb.proxima_tentativa(1, e, agora=AGORA) is None, "repetir ACCESS_DENIED arma ERROR_LOOP_DETECTED"


def test_acesso_de_sistema_para_a_rodada_e_de_metodo_nao():
    assert rb.classificar(401, _corpo("INVALID_CREDENTIALS")).pausa_rodada
    assert rb.classificar(401, _corpo("insufficient_scope")).pausa_rodada
    assert not rb.classificar(403, _corpo("ACCESS_DENIED")).pausa_rodada   # erro de método (1 arquivo)


def test_loop_detected_pausa_sem_repetir_em_loop():
    e = rb.classificar(429, _corpo("ERROR_LOOP_DETECTED", "5 consecutive refusals ACCESS_DENIED"))
    assert e.classe == Classe.LOOP
    assert not e.repetir and not e.permanente
    assert e.espera_s >= 60 and e.abre_circuito_ja and e.pausa_rodada
    prox = rb.proxima_tentativa(1, e, agora=AGORA)
    assert prox is not None and prox - AGORA >= e.espera_s, "só volta depois da pausa"


def test_loop_em_corpo_truncado_nao_vira_access_denied():
    bruto = '{"error":{"message":"ERROR_LOOP_DETECTED: 5 consecutive refusals ACCESS_DENIED, block cle'
    assert rb.classificar(429, bruto).classe == Classe.LOOP


def test_overload_limit_permanente():
    e = rb.classificar(401, _corpo("OVERLOAD_LIMIT"))
    assert e.classe == Classe.OVERLOAD and e.permanente and not e.repetir
    assert rb.proxima_tentativa(1, e, agora=AGORA) is None
    assert e.abre_circuito_ja and e.espera_s > 0


@pytest.mark.parametrize("status,codigo", [(404, None), (400, "ERROR_NOT_FOUND"), (200, "NOT_FOUND"),
                                           (410, None)])
def test_nao_encontrado_permanente(status, codigo):
    e = rb.classificar(status, _corpo(codigo) if codigo else "")
    assert e.classe == Classe.NAO_ENCONTRADO and e.permanente
    assert not e.conta_no_circuito


@pytest.mark.parametrize("exc", [
    socket.timeout("timed out"), TimeoutError("t"), concurrent.futures.TimeoutError(),
    urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
    urllib.error.URLError("timed out"), socket.gaierror(-3, "Temporary failure"),
    ConnectionResetError(54, "Connection reset by peer"), ConnectionRefusedError(61, "refused"),
    http.client.RemoteDisconnected("fechou"), http.client.IncompleteRead(b"abc", 10),
])
def test_erros_de_rede_sao_repetiveis(exc):
    e = rb.classificar(None, "", exc)
    assert e.classe == Classe.REDE, (exc, e)
    assert e.repetir and not e.permanente and e.conta_no_circuito


def test_escrita_apos_timeout_nao_repete_mas_dns_repete():
    e = rb.classificar(None, "", TimeoutError("t"), escrita=True)
    assert e.classe == Classe.REDE and e.ambiguo and not e.repetir
    assert rb.proxima_tentativa(1, e, agora=AGORA) is None
    e2 = rb.classificar(None, "", socket.gaierror(-2, "x"), escrita=True)
    assert e2.repetir and not e2.ambiguo, "DNS falhou = a requisição nem saiu"


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_url_de_download_expirada(status):
    e = rb.classificar(status, "", download=True)
    assert e.classe == Classe.URL_EXPIRADA and e.repetir and not e.permanente
    assert not e.conta_no_circuito
    # o mesmo status fora do host de download não é URL expirada
    assert rb.classificar(status, "").classe in (Classe.ACESSO, Classe.NAO_ENCONTRADO)


def test_download_com_loop_continua_loop():
    assert rb.classificar(403, _corpo("ERROR_LOOP_DETECTED"), download=True).classe == Classe.LOOP


def test_httperror_urllib_usa_code_e_retry_after():
    hdr = email.message.Message()
    hdr["Retry-After"] = "45"
    exc = urllib.error.HTTPError(WEBHOOK_FALSO, 429, "Too Many", hdr, io.BytesIO(b""))
    e = rb.classificar(None, _corpo("OPERATION_TIME_LIMIT"), exc, agora=AGORA)
    assert e.status == 429 and e.classe == Classe.OPERATING and e.espera_s == pytest.approx(45)


def test_excecao_estilo_requests_com_response():
    class Resp:
        status_code = 503
        headers = {}

    class HTTPErrorFalso(OSError):
        response = Resp()

    assert rb.classificar(None, "", HTTPErrorFalso("503")).classe == Classe.RATE_LIMIT


@pytest.mark.parametrize("exc,classe", [
    (rb.ErroTamanho("excede 20 MB do Bot API"), Classe.TAMANHO),
    (rb.ErroTipo("id não numérico CQAC... não é do disco"), Classe.TIPO),
    (rb.ErroIntegridade("0 byte / tamanho != SIZE"), Classe.INTEGRIDADE),
])
def test_falhas_locais_do_downloader_sao_permanentes(exc, classe):
    e = rb.classificar(None, "", exc)
    assert e.classe == classe and e.permanente and not e.repetir
    assert rb.proxima_tentativa(1, e) is None


def test_status_413_415():
    assert rb.classificar(413, "").classe == Classe.TAMANHO
    assert rb.classificar(415, "").classe == Classe.TIPO
    assert rb.classificar(200, _corpo("FILE_TOO_LARGE")).classe == Classe.TAMANHO


def test_5xx_e_internal_server_error_transitorios():
    for st, corpo in ((500, ""), (502, ""), (504, ""), (500, _corpo("INTERNAL_SERVER_ERROR"))):
        e = rb.classificar(st, corpo)
        assert e.classe == Classe.SERVIDOR and e.repetir, (st, corpo)


def test_erro_de_metodo_desconhecido_e_invalido_permanente():
    e = rb.classificar(400, _corpo("ERROR_ARGUMENT"))
    assert e.classe == Classe.INVALIDO and e.permanente
    assert rb.classificar(200, _corpo("ERROR_BATCH_LENGTH_EXCEEDED")).classe == Classe.INVALIDO


def test_falhabitrix_devolve_erro_pronto():
    erro = rb.classificar(503, _corpo("QUERY_LIMIT_EXCEEDED"))
    assert rb.classificar(None, "", rb.FalhaBitrix(erro)) is erro


def test_detalhe_sempre_mascarado():
    corpo = _corpo("ACCESS_DENIED", f"falhou em {WEBHOOK_FALSO}")
    e = rb.classificar(403, corpo, urllib.error.URLError(f"erro {WEBHOOK_FALSO}"))
    assert "tokenSecretoXYZ123" not in e.detalhe
    assert "rest/1/" in e.detalhe
    e2 = rb.classificar(None, "", ConnectionResetError(f"reset em {WEBHOOK_FALSO}"))
    assert "tokenSecretoXYZ123" not in e2.detalhe


def test_mascarar_cobre_webhook_token_senha_e_cpf():
    # rb.mascarar = circuit_breaker.mascarar_texto = seguranca.mascarar (+ _RE_CHAVE).
    # Não há mais fallback "sem seguranca" (import é direto): o teste prova a cobertura.
    txt = rb.mascarar(f"url {WEBHOOK_FALSO} token=abc123def senha: s3nh4 cpf 698.308.525-63")
    assert "tokenSecretoXYZ123" not in txt and "abc123def" not in txt
    assert "s3nh4" not in txt and "698.308.525-63" not in txt


def test_corpo_bytes_e_json_truncado():
    assert rb.classificar(503, b'{"error":"QUERY_LIMIT_EXCEEDED","error_desc').classe == Classe.RATE_LIMIT
    assert rb.classificar(None, '{"error": "", "error_description": "algo"}', None).classe == Classe.DESCONHECIDO


# --------------------------------------------------------- proxima_tentativa
def test_backoff_exponencial_com_jitter_e_teto():
    e = rb.classificar(None, "", ConnectionResetError())
    base, teto, maximo = rb.POLITICA[Classe.REDE]
    vistos = []
    for n in range(1, maximo):
        bruto = min(teto, base * 2 ** (n - 1))
        amostras = [rb.proxima_tentativa(n, e, agora=AGORA) - AGORA for _ in range(50)]
        lo = max(bruto / 2, e.espera_s)
        hi = max(bruto, e.espera_s + min(5.0, 0.1 * e.espera_s))
        assert all(lo - 1e-9 <= a <= hi + 1e-9 for a in amostras), (n, lo, hi, amostras)
        assert len(set(round(a, 6) for a in amostras)) > 1, "tem jitter"
        vistos.append(max(amostras))
    assert vistos[-1] > vistos[0], "cresce com as tentativas"


def test_max_tentativas_por_classe():
    e = rb.classificar(503, _corpo("QUERY_LIMIT_EXCEEDED"))
    m = rb.MAX_TENTATIVAS[Classe.RATE_LIMIT]
    assert rb.proxima_tentativa(m - 1, e, agora=AGORA) is not None
    assert rb.proxima_tentativa(m, e, agora=AGORA) is None
    assert rb.MAX_TENTATIVAS[Classe.ACESSO] == 1


def test_proxima_tentativa_respeita_espera_s():
    e = rb.classificar(429, _corpo("OPERATION_TIME_LIMIT"), operating_reset_at=AGORA + 500, agora=AGORA)
    prox = rb.proxima_tentativa(1, e, agora=AGORA)
    assert prox - AGORA >= 500


def test_teto_do_backoff():
    e = rb.classificar(500, "")
    base, teto, maximo = rb.POLITICA[Classe.SERVIDOR]
    a = rb.atraso(maximo - 1, e)
    assert a <= teto


def test_escala_de_fila_em_minutos_e_horas():
    """D1/D2: fila processada 1x por ciclo precisa de backoff em minutos/horas."""
    e = rb.classificar(503, _corpo("QUERY_LIMIT_EXCEEDED"))
    a1 = rb.atraso(1, e, fila=True)
    assert 300 <= a1 <= 600
    assert rb.atraso(7, e, fila=True) <= 21600
    assert rb.proxima_tentativa(8, e, agora=AGORA, fila=True) is None
    assert rb.proxima_tentativa(1, rb.classificar(404, ""), fila=True) is None


# ------------------------------------------------------------- LimitadorHost
_RL_RAIZ = tempfile.mkdtemp(prefix="rl_")
atexit.register(shutil.rmtree, _RL_RAIZ, ignore_errors=True)


def _arq_rl():
    return os.path.join(tempfile.mkdtemp(dir=_RL_RAIZ), ".bitrix-ratelimit")


def test_limitador_rajada_imediata_depois_espera():
    lim = rb.LimitadorHost(caminho=_arq_rl(), taxa=20, rajada=3)
    t0 = time.monotonic()
    esperas = [lim.aguardar() for _ in range(3)]
    assert all(w == 0 for w in esperas), "rajada sai sem espera"
    w = lim.aguardar()
    assert w == pytest.approx(1 / 20, abs=0.02)
    assert time.monotonic() - t0 >= 0.04


def test_limitador_timeout_nao_consome():
    arq = _arq_rl()
    lim = rb.LimitadorHost(caminho=arq, taxa=0.1, rajada=1)
    lim.aguardar()
    with pytest.raises(rb.TempoEsgotado):
        lim.aguardar(timeout=0.5)
    est = json.load(open(arq))
    assert est["tokens"] <= 0.01 and est["tokens"] > -0.5, "timeout não deixou dívida"


def test_limitador_estado_corrompido_e_conservador():
    arq = _arq_rl()
    with open(arq, "w") as fh:
        fh.write("{lixo")
    lim = rb.LimitadorHost(caminho=arq, taxa=10, rajada=5)
    assert lim.aguardar() == pytest.approx(0.1, abs=0.02), "arquivo corrompido = balde vazio"


def test_limitador_env_e_padrao(monkeypatch):
    arq = _arq_rl()
    monkeypatch.setenv("BITRIX_RATELIMIT_FILE", arq)
    monkeypatch.setenv("BITRIX_RATE", "0.7")
    monkeypatch.setenv("BITRIX_RAJADA", "4")
    lim = rb.limitador_padrao()
    assert (lim.caminho, lim.taxa, lim.rajada) == (arq, 0.7, 4.0)
    assert rb.limitador_padrao() is lim
    monkeypatch.setenv("BITRIX_RATE", "abc")
    monkeypatch.delenv("BITRIX_RATELIMIT_FILE")
    lim2 = rb.LimitadorHost()
    assert lim2.taxa == 1.5 and lim2.caminho == caminhos.em_base(".bitrix-ratelimit")


def test_limitador_padrao_contrato():
    lim = rb.LimitadorHost(caminho=_arq_rl())
    assert (lim.taxa, lim.rajada) == (1.5, 10.0)


def test_limitador_sem_acesso_ao_arquivo_cai_para_memoria():
    lim = rb.LimitadorHost(caminho="/dev/null/nao/existe/.rl", taxa=50, rajada=2)
    assert lim.aguardar() == 0 and lim.aguardar() == 0
    assert lim.aguardar() > 0


def _trabalhador_limitador(arq, taxa, rajada, n, fila):
    import rede_bitrix as r
    lim = r.LimitadorHost(caminho=arq, taxa=taxa, rajada=rajada)
    marcas = []
    for _ in range(n):
        lim.aguardar(timeout=30)
        marcas.append(time.time())
    fila.put(marcas)


def test_limitador_compartilhado_entre_processos_respeita_taxa_e_rajada():
    """3 processos x 8 pedidos com taxa 10/s e rajada 5, balde COMPARTILHADO (arquivo):
    em QUALQUER janela [t0, t] o total liberado <= rajada + taxa*(t - t0 + folga).

    Robustez sob carga (C4): a marca é tirada DEPOIS de `aguardar()` voltar; com a
    máquina carregada o processo pode perder a CPU entre a concessão e a marca, o que
    aproxima marcas e simulava excesso com taxa 25/s e folga 0,02 s (= 0,5 token).
    Agora a taxa é menor e a folga é 0,15 s (= 1,5 token): ainda reprova com folga
    qualquer balde não compartilhado (3 baldes = 15 de rajada e 30/s) ou rajada
    ignorada, que dão excesso de vários tokens."""
    arq = _arq_rl()
    taxa, rajada, n, procs = 10.0, 5, 8, 3
    ctx = multiprocessing.get_context("spawn")
    fila = ctx.Queue()
    ps = [ctx.Process(target=_trabalhador_limitador, args=(arq, taxa, rajada, n, fila)) for _ in range(procs)]
    for p in ps:
        p.start()
    marcas = []
    for _ in ps:
        marcas.extend(fila.get(timeout=90))
    for p in ps:
        p.join(timeout=30)
        assert p.exitcode == 0
    marcas.sort()
    total = n * procs
    assert len(marcas) == total
    folga = 0.15  # jitter de escalonamento entre a concessão e a marca (s)
    # tempo mínimo teórico: (total - rajada)/taxa (atraso de escalonamento só AUMENTA a duração,
    # exceto o da 1ª marca — coberto pela folga)
    duracao = marcas[-1] - marcas[0]
    assert duracao + folga >= (total - rajada) / taxa, duracao
    for i, t0 in enumerate(marcas):
        for j in range(i, len(marcas)):
            liberados = j - i + 1
            assert liberados <= rajada + taxa * (marcas[j] - t0 + folga) + 1e-6, \
                f"excedeu: {liberados} em {marcas[j] - t0:.3f}s"


# ----------------------------------------------------------------- circuito()
def test_circuito_usa_arquivo_por_nome_em_base():
    c = rb.circuito("bitrix-download")
    assert isinstance(c, circuit_breaker.CircuitBreaker)
    assert c.arquivo == caminhos.em_base("circuit-bitrix-download.json")
    assert c.timeout_s == 180.0 and c.max_falhas == 3
    c.registrar_sucesso()
    for nome in ("bitrix-leitura", "vibecode-leitura", "vibecode-download"):
        assert rb.circuito(nome).arquivo.endswith(f"circuit-{nome}.json")


@pytest.mark.parametrize("nome", ["../x", "a/b", "", "X Y", None])
def test_circuito_rejeita_nome_perigoso(nome):
    with pytest.raises(ValueError):
        rb.circuito(nome)


def test_circuito_nao_conta_bug_nosso_nem_falha_de_item():
    c = rb.circuito("teste-bug", max_falhas=1)
    c.registrar_sucesso()
    for exc in (TypeError("bug"), KeyError("x"),
                rb.FalhaBitrix(rb.classificar(404, "")), rb.ErroTamanho("grande")):
        with pytest.raises(type(exc)):
            c.chamar(lambda e=exc: (_ for _ in ()).throw(e))
    assert c.estado() == circuit_breaker.CLOSED and c.falhas() == 0
    with pytest.raises(ConnectionResetError):
        c.chamar(lambda: (_ for _ in ()).throw(ConnectionResetError()))
    assert c.estado() == circuit_breaker.OPEN


def test_aplicar_no_circuito_loop_abre_ja_pela_pausa():
    c = rb.circuito("teste-loop")
    c.registrar_sucesso()
    e = rb.classificar(429, _corpo("ERROR_LOOP_DETECTED"))
    antes = time.time()
    rb.aplicar_no_circuito(c, e)
    s = c.situacao()
    assert s["estado"] == circuit_breaker.OPEN and s["motivo"] == "LOOP"
    assert s["aberto_ate"] - antes >= e.espera_s - 1
    assert "aberto_em" in json.load(open(c.arquivo))["teste-loop"]


def test_aplicar_no_circuito_rate_limit_conta_e_404_nao():
    c = rb.circuito("teste-rate", max_falhas=2)
    c.registrar_sucesso()
    rb.aplicar_no_circuito(c, rb.classificar(404, ""))
    assert c.falhas() == 0
    rate = rb.classificar(503, _corpo("QUERY_LIMIT_EXCEEDED"))
    rb.aplicar_no_circuito(c, rate)
    assert c.estado() == circuit_breaker.CLOSED and c.falhas() == 1
    rb.aplicar_no_circuito(c, rate)
    assert c.estado() == circuit_breaker.OPEN
    assert c.situacao()["motivo"] == "RATE_LIMIT"


def test_modulo_nao_tem_cliente_http():
    src = open(os.path.join(RAIZ, "rede_bitrix.py"), encoding="utf-8").read()
    for proibido in ("urlopen(", "requests.get", "requests.post", "http.client.HTTPConnection(",
                     "socket.create_connection"):
        assert proibido not in src


def test_trava_ocupada_respeita_timeout(tmp_path):
    """A4 (Revisor OpenBot): trava do limitador presa por outro processo não bloqueia além do timeout."""
    import fcntl as _f
    import os as _os
    import time as _t
    import rede_bitrix as _rb
    arq = str(tmp_path / "rl")
    fd = _os.open(arq, _os.O_RDWR | _os.O_CREAT, 0o600)
    _f.flock(fd, _f.LOCK_EX)
    try:
        lim = _rb.LimitadorHost(caminho=arq, taxa=50, rajada=50)
        t0 = _t.monotonic()
        try:
            lim.aguardar(timeout=0.5)
        except TimeoutError:
            pass
        assert _t.monotonic() - t0 < 3.0
    finally:
        _f.flock(fd, _f.LOCK_UN)
        _os.close(fd)
