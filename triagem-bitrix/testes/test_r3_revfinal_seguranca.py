"""R3 — achados A1..A8 da revisão final 3 (segurança/JEV).

Dados 100% sintéticos: CPF gerado por cpf_util.gerar_para_teste, chaves inventadas.
Todos falham contra o HEAD anterior à frente R3.
"""
import json
import logging
import math
import os
import stat
import types

import pytest

import certidoes
import cpf_util
import decisoes_jev as dj
import seguranca
import telegram_grupo as tgg
import typesafe_cliente as tc

CPF = cpf_util.gerar_para_teste(11)
CHAVE_SEM_PREFIXO = "Zq7" + "w" * 29      # chave fora do formato apikey_<...>


def _cfg(**k):
    c = {"ativo": True, "modo": "decide", "base_url": "https://api.typesafe.ai"}
    c.update(k)
    return c


def _so_digitos(s):
    return "".join(ch for ch in s if ch.isdigit())


# ------------------------------------------------------------------ A1
@pytest.mark.parametrize("fmt", [
    "{a}.{b}.{c}/{d}", "{a}.{b}.{c} {d}", "{a} {b} {c} {d}", "{a}{b}{c}/{d}",
])
def test_a1_cpf_com_barra_ou_espaco_antes_do_dv_mascarado(fmt):
    bruto = fmt.format(a=CPF[:3], b=CPF[3:6], c=CPF[6:9], d=CPF[9:])
    m = seguranca.mascarar(f"RG de Fulano CPF {bruto} emitido")
    assert CPF not in _so_digitos(m), m


def test_a1_trecho_do_jev_e_jsonl_sem_cpf(tmp_path):
    arq = tmp_path / "s.jsonl"
    texto = f"RG de Fulano CPF {CPF[:3]}.{CPF[3:6]}.{CPF[6:9]}/{CPF[9:]}"
    enviados = []

    def p(state, q, config):
        enviados.append(json.dumps(state, ensure_ascii=False))
        return {"answers": {}}
    dj.documento_e_do_cliente({"nome": "Fulano", "cpf": CPF}, texto,
                              config=_cfg(modo="sombra", sombra_jsonl=str(arq)), _perguntar=p)
    assert enviados and CPF not in _so_digitos(enviados[0])
    assert CPF not in _so_digitos(arq.read_text())


@pytest.mark.parametrize("texto", [
    "telefone 123 456 789 00 ramal",          # espaço sem pontos e DV inválido
    "processo 111222333/44",                    # barra sem pontos e DV inválido
    "IP 200.100.150.25 ok",
    "contagem 100 200 300 40 itens",
])
def test_a1_sem_falso_positivo(texto):
    assert seguranca.mascarar(texto) == texto


# ------------------------------------------------------------------ A2
@pytest.mark.parametrize("conf", [float("nan"), float("inf"), 42, 1.0001, -0.1, True, "0.99", None])
def test_a2_confianca_invalida_vai_para_revisao(conf):
    resp = {"acao_lote_misto": {"choice": "separar", "confidence": conf}}
    r = dj.lote_misturado_acao({"n_documentos_sem_dono": 0}, config=_cfg(sombra_jsonl=os.devnull),
                               _perguntar=lambda s, q, config: {"answers": resp})
    assert r["acao"] == dj.ACAO_REVISAO, r
    assert r.get("confianca") in (None, 0.0) or math.isfinite(r["confianca"])


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, 99, -1])
def test_a2_score_invalido_nunca_promove_ao_kit(score):
    resp = {"dono_documento": {"choice": "mesmo_cliente", "confidence": 0.99},
            "risco_documento_terceiro": {"score": score}}
    r = dj.documento_e_do_cliente({"nome": "Fulano", "cpf": CPF}, "RG de Fulano sem numero",
                                  config=_cfg(sombra_jsonl=os.devnull),
                                  _perguntar=lambda s, q, config: {"answers": resp})
    assert r["acao"] != dj.ACAO_KIT, r


def test_a2_confianca_valida_ainda_age():
    resp = {"acao_lote_misto": {"choice": "separar", "confidence": 0.95}}
    r = dj.lote_misturado_acao({"n_documentos_sem_dono": 0}, config=_cfg(sombra_jsonl=os.devnull),
                               _perguntar=lambda s, q, config: {"answers": resp})
    assert r["confianca"] == pytest.approx(0.95)


# ------------------------------------------------------------------ A3
@pytest.mark.parametrize("ruim", [CHAVE_SEM_PREFIXO + "\nlinha2", "ab cd" + "e" * 20,
                                  CHAVE_SEM_PREFIXO + "\x07"])
def test_a3_chave_com_espaco_ou_controle_recusada_sem_valor(monkeypatch, ruim):
    monkeypatch.setenv("TRIAGEM_TYPESAFE_KEY", ruim)
    with pytest.raises(seguranca.SegredoAusente) as e:
        tc.ler_chave(_cfg())
    assert CHAVE_SEM_PREFIXO not in str(e.value) and "eeee" not in str(e.value)


def test_a3_valueerror_do_transporte_nao_vaza_bearer(monkeypatch):
    monkeypatch.setenv("TRIAGEM_TYPESAFE_KEY", CHAVE_SEM_PREFIXO)

    def transporte(*a):
        raise ValueError(f"Invalid header value b'Bearer {CHAVE_SEM_PREFIXO}\\n'")
    with pytest.raises(tc.ErroTypeSafe) as e:
        tc.perguntar({"x": 1}, {"q": {"type": "choice", "instructions": "x",
                                      "criteria": {"a": "a", "b": "b"}}},
                     config=_cfg(), _transporte=transporte)
    assert CHAVE_SEM_PREFIXO not in str(e.value) + repr(e.value)
    assert e.value.__cause__ is None and e.value.__context__ is None or \
        CHAVE_SEM_PREFIXO not in repr(e.value.__context__)


def test_a3_bearer_solto_mascarado():
    m = seguranca.mascarar(f"Invalid header value b'Bearer {CHAVE_SEM_PREFIXO}\\n'")
    assert CHAVE_SEM_PREFIXO not in m


# ------------------------------------------------------------------ A4
@pytest.mark.parametrize("texto", [
    json.dumps({"x": {"api_key": CHAVE_SEM_PREFIXO}}),
    json.dumps({"typesafe_key": CHAVE_SEM_PREFIXO}),
    repr({"apikey": CHAVE_SEM_PREFIXO}),
    f"api_key={CHAVE_SEM_PREFIXO} resto",
])
def test_a4_api_key_typesafe_key_mascarados(texto):
    assert CHAVE_SEM_PREFIXO not in seguranca.mascarar(texto)


# ------------------------------------------------------------------ A5
@pytest.mark.parametrize("corpo", [
    {"error": CHAVE_SEM_PREFIXO},
    {"code": "Fulano de Tal Silva"},
    {"error": {"type": "x" + CHAVE_SEM_PREFIXO, "message": "eco"}},
    {"detail": [{"type": CHAVE_SEM_PREFIXO, "loc": ["body", "Fulano Silva"], "input": "eco"}]},
])
def test_a5_resumo_erro_so_codigos_conhecidos(corpo):
    r = tc._resumo_erro(json.dumps(corpo))
    assert CHAVE_SEM_PREFIXO not in r and "Fulano" not in r and "eco" not in r, r


def test_a5_codigo_conhecido_preservado():
    r = tc._resumo_erro(json.dumps({"detail": [{"type": "dict_type", "loc": ["body", "state", 0]}]}))
    assert r == "dict_type@body.state.0"


# ------------------------------------------------------------------ A6
def _isolar_segredos(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIAGEM_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN_FILE", raising=False)
    monkeypatch.delenv("TRIAGEM_SEGREDOS_ESTRITO", raising=False)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", str(tmp_path / "nao-existe"))
    monkeypatch.setattr(seguranca.caminhos, "SEGREDOS_DIR", str(tmp_path / "segredos-vazio"))
    monkeypatch.setattr(tgg, "ARQ_TOKEN", str(tmp_path / "nao-existe-token"))


def test_a6_ler_token_usa_ler_segredo_env(monkeypatch, tmp_path):
    _isolar_segredos(monkeypatch, tmp_path)
    monkeypatch.setenv("TRIAGEM_TELEGRAM_TOKEN", "123:tok-sintetico")
    assert tgg.ler_token() == "123:tok-sintetico"


def test_a6_telegram_token_file_legado_continua(monkeypatch, tmp_path):
    _isolar_segredos(monkeypatch, tmp_path)
    arq = tmp_path / "tg.token"
    arq.write_text("456:tok-legado\n")
    os.chmod(arq, 0o600)
    monkeypatch.setenv("TELEGRAM_TOKEN_FILE", str(arq))
    monkeypatch.setattr(tgg, "ARQ_TOKEN", str(arq))
    assert tgg.ler_token() == "456:tok-legado"


def test_a6_estrito_recusa_token_file_inseguro(monkeypatch, tmp_path):
    _isolar_segredos(monkeypatch, tmp_path)
    arq = tmp_path / "tg.token"
    arq.write_text("789:tok-inseguro\n")
    os.chmod(arq, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)   # 0604
    monkeypatch.setenv("TELEGRAM_TOKEN_FILE", str(arq))
    monkeypatch.setattr(tgg, "ARQ_TOKEN", str(arq))
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    with pytest.raises(Exception) as e:
        tgg.ler_token()
    assert "tok-inseguro" not in str(e.value)


def test_a6_ausente_erro_sem_valor(monkeypatch, tmp_path):
    _isolar_segredos(monkeypatch, tmp_path)
    with pytest.raises(seguranca.SegredoAusente):
        tgg.ler_token()


# ------------------------------------------------------------------ A7
def test_a7_jsonl_sem_nan(tmp_path):
    arq = tmp_path / "s.jsonl"
    dj.registrar_jsonl({"sombra_jsonl": str(arq)},
                       {"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": float("-inf")}})

    def _proibe(c):
        raise ValueError(c)
    d = json.loads(arq.read_text().strip(), parse_constant=_proibe)
    assert d == {"a": None, "b": [None, 1.5], "c": {"d": None}}


# ------------------------------------------------------------------ A8
def test_a8_stdout_stderr_do_node_mascarados(monkeypatch, caplog):
    cpf_fmt = f"{CPF[:3]}.{CPF[3:6]}.{CPF[6:9]}-{CPF[9:]}"

    def falso_run(*a, **k):
        return types.SimpleNamespace(stdout=f"falhou cpf {cpf_fmt}\n",
                                     stderr=f"Authorization: Bearer {CHAVE_SEM_PREFIXO}", returncode=1)
    monkeypatch.setattr(certidoes.subprocess, "run", falso_run)
    caplog.set_level(logging.DEBUG)
    nome, como = certidoes.analisar_certidao(b"%PDF-1.4\n%sintetico\n")
    assert nome is None and como.startswith("IA não respondeu")
    assert CPF not in _so_digitos(como) and CHAVE_SEM_PREFIXO not in como, como
