"""typesafe_cliente — só transporte falso; nenhuma chamada real (API paga).
Chave sempre fictícia `apikey_teste_...`."""
import json
import logging
import os
import socket

import pytest

import caminhos
import cpf_util
import seguranca
import typesafe_cliente as T

CHAVE = "apikey_teste_FICTICIA_0123456789abcdef"
CFG = {"ativo": True, "modo": "decide", "base_url": "https://api.exemplo.invalid", "timeout": 3}

Q_OK = {
    "dono": {"type": "choice", "instructions": "De quem é?", "criteria": {"a": "A", "b": "B"}},
    "risco": {"type": "score", "instructions": "Risco?", "criteria": ["baixo", "alto"]},
    "tem_nome": {"type": "noul", "instructions": "Há nome legível?"},
}
RESP_OK = {
    "model": "jev-1.13.0",
    "answers": {
        "dono": {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 0.95, "b": 0.05}},
        "risco": {"type": "score", "score": 0.2, "confidence": 0.8, "legend": {"0": "baixo", "1": "alto"},
                  "probabilities": {"0": 0.8, "1": 0.2}},
        "tem_nome": {"type": "noul", "noul": 0.97},
    },
    "usage": {"input_tokens": 120, "output_tokens": 30},
}


@pytest.fixture(autouse=True)
def _isola(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TRIAGEM_") and k not in ("TRIAGEM_BASE", "TRIAGEM_SEM_REDE"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TRIAGEM_SEGREDOS_LEGADOS", "")
    etc = tmp_path / "etc"
    etc.mkdir(mode=0o700)
    monkeypatch.setattr(caminhos, "SEGREDOS_DIR", str(etc))
    monkeypatch.setenv("TRIAGEM_TYPESAFE_KEY", CHAVE)
    monkeypatch.setattr(T, "_dormir", lambda s: None)


class Falso:
    """Transporte falso: fila de respostas (status, headers, corpo) ou exceções."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def __call__(self, metodo, url, cab, corpo, timeout):
        self.chamadas.append({"metodo": metodo, "url": url, "cab": cab,
                              "corpo": json.loads(corpo) if corpo else None, "timeout": timeout})
        r = self.respostas.pop(0)
        if isinstance(r, BaseException):
            raise r
        status, cab_r, texto = r
        return status, cab_r, texto if isinstance(texto, str) else json.dumps(texto)


def test_sucesso_monta_requisicao_e_devolve_answers(caplog):
    caplog.set_level(logging.DEBUG)
    t = Falso((200, {}, RESP_OK))
    r = T.perguntar({"texto": "x"}, Q_OK, _transporte=t, config=CFG)
    assert r["answers"]["dono"]["choice"] == "a"
    assert r["answers"]["tem_nome"]["noul"] == 0.97
    assert r["usage"]["input_tokens"] == 120
    c = t.chamadas[0]
    assert c["metodo"] == "POST" and c["url"] == "https://api.exemplo.invalid/v1/systemone"
    assert c["cab"]["Authorization"] == "Bearer " + CHAVE
    assert c["corpo"]["model"] == "jev-latest" and c["corpo"]["questions"] == Q_OK
    assert c["timeout"] == 3
    assert CHAVE not in caplog.text and "dono" in caplog.text


def test_modelo_explicito_e_timeout():
    t = Falso((200, {}, RESP_OK))
    T.perguntar("txt", Q_OK, model="jev-preview", timeout=7, _transporte=t, config=CFG)
    assert t.chamadas[0]["corpo"]["model"] == "jev-preview" and t.chamadas[0]["timeout"] == 7


@pytest.mark.parametrize("q, trecho", [
    ({"x": {"type": "choice", "instructions": "?", "criteria": ["a", "b"]}}, "OBJETO"),
    ({"x": {"instructions": "?", "criteria": {"a": "", "b": ""}}}, "type"),
    ({"x": {"type": "score", "instructions": "?", "criteria": {"a": "b"}}}, "LISTA"),
    ({"x": {"type": "score", "instructions": "?", "criteria": ["só um"]}}, "2..10"),
    ({"x": {"type": "score", "instructions": "?", "criteria": [str(i) for i in range(11)]}}, "2..10"),
    ({"x": {"type": "box", "instructions": "?"}}, "não suportado"),
    ({"x": {"type": "noul"}}, "instructions"),
    ({"x": {"type": "noul", "instructions": "?", "criteria": ["sim"]}}, "noul"),
    ({}, "não vazio"),
])
def test_shape_invalido_levanta_valueerror_sem_chamar(q, trecho):
    t = Falso()
    with pytest.raises(ValueError, match=trecho):
        T.perguntar({}, q, _transporte=t, config=CFG)
    assert t.chamadas == []


def test_noul_com_criteria_true_false_e_valido():
    T.validar_questoes({"n": {"type": "noul", "instructions": "?",
                              "criteria": {"true": "sim", "false": "não"}}})


def test_desligado_por_padrao_nao_chama(tmp_path):
    t = Falso()
    cfg_vazio = T.carregar_config(str(tmp_path / "nao-existe.json"))
    assert cfg_vazio["ativo"] is False and cfg_vazio["modo"] == "desligado"
    with pytest.raises(T.TypeSafeDesligado):
        T.perguntar({}, Q_OK, _transporte=t, config=cfg_vazio)
    assert t.chamadas == []


def test_carregar_config_le_bloco_typesafe(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"outro": 1, "typesafe": {"ativo": True, "modo": "sombra", "timeout": 9}}))
    cfg = T.carregar_config(str(p))
    assert cfg["ativo"] is True and cfg["modo"] == "sombra" and cfg["timeout"] == 9
    assert cfg["model"] == "jev-latest"
    p.write_text(json.dumps({"typesafe": {"ativo": "sim"}}))   # só True literal liga
    assert T.carregar_config(str(p))["ativo"] is False


def test_config_json_do_repo_nao_liga():
    assert T.carregar_config()["ativo"] is False


def test_401_credencial_sem_retry_e_sem_chave(caplog):
    caplog.set_level(logging.DEBUG)
    t = Falso((401, {}, {"detail": "invalid key " + CHAVE}))
    with pytest.raises(T.ErroCredencial) as ei:
        T.perguntar({}, Q_OK, _transporte=t, config=CFG)
    assert ei.value.permanente and ei.value.status == 401
    assert len(t.chamadas) == 1
    assert CHAVE not in str(ei.value) and CHAVE not in repr(ei.value) and CHAVE not in caplog.text


@pytest.mark.parametrize("status", [429, 529, 503])
def test_transitorio_repete_e_recupera(status, monkeypatch):
    esperas = []
    monkeypatch.setattr(T, "_dormir", esperas.append)
    t = Falso((status, {}, "{}"), (200, {}, RESP_OK))
    r = T.perguntar({}, Q_OK, _transporte=t, config=CFG)
    assert r["answers"] and len(t.chamadas) == 2 and esperas == [0.5]


def test_retry_after_respeitado_com_teto(monkeypatch):
    esperas = []
    monkeypatch.setattr(T, "_dormir", esperas.append)
    t = Falso((429, {"Retry-After": "2"}, "{}"), (429, {"retry-after": "999"}, "{}"), (200, {}, RESP_OK))
    T.perguntar({}, Q_OK, _transporte=t, config=CFG)
    assert esperas == [2.0, T.ESPERA_MAX_S]


def test_transitorio_esgota_e_levanta(caplog):
    caplog.set_level(logging.DEBUG)
    t = Falso((529, {}, "{}"), (529, {}, "{}"), (529, {}, "{}"))
    with pytest.raises(T.ErroTransitorio) as ei:
        T.perguntar({}, Q_OK, _transporte=t, config=CFG)
    assert not ei.value.permanente and len(t.chamadas) == 1 + T.MAX_REPETICOES
    assert CHAVE not in caplog.text


@pytest.mark.parametrize("exc", [socket.timeout("timed out"), TimeoutError(), ConnectionResetError()])
def test_timeout_e_rede_sao_transitorios(exc):
    t = Falso(exc, exc, exc)
    with pytest.raises(T.ErroTransitorio, match="rede"):
        T.perguntar({}, Q_OK, _transporte=t, config=CFG)
    assert len(t.chamadas) == 3


def test_422_shape_e_erro_de_programacao_sem_ecoar_state():
    cpf = cpf_util.gerar_para_teste(3)
    corpo = {"detail": [{"loc": ["body", "questions", "x", "criteria"], "type": "dict_type",
                         "msg": "Input should be a valid dictionary", "input": f"cpf {cpf}"}]}
    t = Falso((422, {}, corpo))
    with pytest.raises(T.ErroRequisicao) as ei:
        T.perguntar({"cpf": cpf}, Q_OK, _transporte=t, config=CFG)
    assert "dict_type" in str(ei.value) and cpf not in str(ei.value)
    assert len(t.chamadas) == 1


def test_resposta_sem_answer_pedida():
    t = Falso((200, {}, {"answers": {"dono": RESP_OK["answers"]["dono"]}}))
    with pytest.raises(T.ErroResposta):
        T.perguntar({}, Q_OK, _transporte=t, config=CFG)


def test_resposta_nao_json():
    with pytest.raises(T.ErroResposta):
        T.perguntar({}, Q_OK, _transporte=Falso((200, {}, "<html>")), config=CFG)


def test_state_nunca_logado(caplog):
    caplog.set_level(logging.DEBUG)
    segredo_state = "NOME FICTICIO SEGREDO DO STATE"
    T.perguntar({"texto": segredo_state}, Q_OK, _transporte=Falso((200, {}, RESP_OK)), config=CFG)
    assert segredo_state not in caplog.text


def test_chave_ausente_levanta_segredo_ausente(monkeypatch):
    monkeypatch.delenv("TRIAGEM_TYPESAFE_KEY")
    t = Falso()
    with pytest.raises(seguranca.SegredoAusente):
        T.perguntar({}, Q_OK, _transporte=t, config=CFG)
    assert t.chamadas == []


def test_chave_por_arquivo_principal_e_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIAGEM_TYPESAFE_KEY")
    p = os.path.join(caminhos.SEGREDOS_DIR, "typesafe_key")
    with open(p, "w") as fh:
        fh.write(CHAVE + "\n")
    os.chmod(p, 0o600)
    assert T.ler_chave(CFG) == CHAVE
    os.remove(p)
    kf = tmp_path / "outra.key"
    kf.write_text(CHAVE)
    os.chmod(kf, 0o600)
    assert T.ler_chave(dict(CFG, key_file=str(kf))) == CHAVE


def test_key_file_aberto_recusado_em_modo_estrito(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIAGEM_TYPESAFE_KEY")
    monkeypatch.setenv("TRIAGEM_SEGREDOS_ESTRITO", "1")
    kf = tmp_path / "aberta.key"
    kf.write_text(CHAVE)
    os.chmod(kf, 0o644)
    with pytest.raises(seguranca.SegredoRecusado) as ei:
        T.ler_chave(dict(CFG, key_file=str(kf)))
    assert CHAVE not in str(ei.value)


def test_legado_padrao_inclui_typesafe():
    assert caminhos.em_base("typesafe.key") in seguranca._legados_padrao("typesafe_key")
    assert "typesafe_key" in seguranca.NOMES_CONHECIDOS


def test_modelos():
    t = Falso((200, {}, {"data": [{"id": "jev-latest"}, {"id": "jev-preview"}]}))
    assert T.modelos(_transporte=t, config=CFG) == ["jev-latest", "jev-preview"]
    assert t.chamadas[0]["metodo"] == "GET" and t.chamadas[0]["url"].endswith("/v1/models")
    with pytest.raises(T.TypeSafeDesligado):
        T.modelos(_transporte=Falso(), config={"ativo": False})
