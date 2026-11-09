# -*- coding: utf-8 -*-
"""
TDD — circuit breaker das DUAS fontes de leitura da triagem (06/10/2026).

A API do VibeCode é instável (502/401 intermitentes). Antes, cada ciclo TENTAVA
a API e, ao falhar, caía para o webhook — mas voltava a tentar a fonte morta no
ciclo seguinte. O disjuntor desativa a fonte que falhar N vezes seguidas por
T segundos e redireciona direto para a saudável.

Testa o módulo `circuit_breaker.CircuitBreaker` ISOLADO (sem rede), com relógio
falsificado para controlar a expiração:

  · N falhas consecutivas -> fonte ABERTA (pula/redireciona);
  · durante 'open', `chamar()` NÃO executa a função (CircuitOpenError);
  · sucesso zera o contador e FECHA o breaker;
  · passado T, vira 'half-open' (próxima chamada é permitida como teste);
  · sucesso no half-open FECHA; falha REABRE por mais T;
  · o estado PERSISTE em disco (nova instância enxerga 'open').

Roda com pytest (`pytest testes/test_circuit_breaker.py`) OU sozinho:
    /usr/bin/python3 testes/test_circuit_breaker.py
"""
import os
import shutil
from conftest import RAIZ
import atexit
import json
import sys
import tempfile

sys.path.insert(0, RAIZ)

from circuit_breaker import (                    # noqa: E402
    CLOSED, HALF_OPEN, OPEN, CircuitBreaker, CircuitOpenError,
)


class Relogio:
    """Relógio falsificado para controlar a expiração do breaker."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def avanca(self, segundos):
        self.t += segundos


_TMP_RAIZ = tempfile.mkdtemp(prefix="cb_")
atexit.register(shutil.rmtree, _TMP_RAIZ, ignore_errors=True)


def _tmp_arq():
    return os.path.join(tempfile.mkdtemp(dir=_TMP_RAIZ), "breaker.json")


def _boom():
    raise RuntimeError("boom (fonte caiu)")


# --------------------------------------------------------------------------
# RED/GREEN: comportamento do disjuntor
# --------------------------------------------------------------------------
def test_abre_apos_n_falhas_consecutivas():
    rel = Relogio()
    cb = CircuitBreaker("api", arquivo=_tmp_arq(), max_falhas=3, timeout_s=300, agora=rel)
    cb.registrar_falha()
    cb.registrar_falha()
    assert cb.estado() == CLOSED, "2 falhas ainda não abrem (default N=3)"
    cb.registrar_falha()
    assert cb.estado() == OPEN, "3ª falha consecutiva deve abrir o breaker"


def test_aberto_pula_a_chamada_sem_executar():
    rel = Relogio()
    cb = CircuitBreaker("api", arquivo=_tmp_arq(), max_falhas=2, timeout_s=300, agora=rel)
    cb.registrar_falha()
    cb.registrar_falha()
    assert cb.estado() == OPEN
    executou = []
    try:
        cb.chamar(lambda: executou.append(1))
    except CircuitOpenError:
        pass
    assert executou == [], "fonte aberta NÃO deve executar a função"
    assert cb.estado() == OPEN


def test_sucesso_zera_o_contador_e_fecha():
    rel = Relogio()
    cb = CircuitBreaker("api", arquivo=_tmp_arq(), max_falhas=3, timeout_s=300, agora=rel)
    cb.registrar_falha()
    cb.registrar_falha()
    cb.registrar_sucesso()
    assert cb.falhas() == 0
    assert cb.estado() == CLOSED


def test_expirou_vira_half_open_e_permite_teste():
    rel = Relogio()
    cb = CircuitBreaker("api", arquivo=_tmp_arq(), max_falhas=2, timeout_s=300, agora=rel)
    cb.registrar_falha()
    cb.registrar_falha()
    assert cb.estado() == OPEN
    rel.avanca(299)
    assert cb.permitido() is False, "ainda dentro de T -> segue aberta"
    rel.avanca(2)  # total 301 > 300
    assert cb.permitido() is True, "expirou -> half-open permite o teste"
    assert cb.estado() == HALF_OPEN


def test_half_open_com_sucesso_fecha():
    rel = Relogio()
    cb = CircuitBreaker("api", arquivo=_tmp_arq(), max_falhas=2, timeout_s=300, agora=rel)
    cb.registrar_falha()
    cb.registrar_falha()
    rel.avanca(301)
    assert cb.chamar(lambda: "ok") == "ok"
    assert cb.estado() == CLOSED
    assert cb.falhas() == 0


def test_half_open_com_falha_reabre_por_mais_t():
    rel = Relogio()
    cb = CircuitBreaker("api", arquivo=_tmp_arq(), max_falhas=2, timeout_s=300, agora=rel)
    cb.registrar_falha()
    cb.registrar_falha()
    rel.avanca(301)
    assert cb.estado() == HALF_OPEN
    try:
        cb.chamar(_boom)
    except RuntimeError:
        pass
    assert cb.estado() == OPEN, "falha no teste (half-open) deve REABRIR"
    # reaberto: só volta a permitir depois de MAIS T (aberto_ate = 1301+300=1601)
    rel.avanca(299)  # agora=1600 < 1601 -> ainda aberto
    assert cb.permitido() is False
    rel.avanca(2)    # agora=1602 >= 1601 -> half-open de novo
    assert cb.permitido() is True


def test_estado_persiste_em_disco_entre_instancias():
    arq = _tmp_arq()
    rel = Relogio()
    cb = CircuitBreaker("api", arquivo=arq, max_falhas=2, timeout_s=300, agora=rel)
    cb.registrar_falha()
    cb.registrar_falha()
    assert cb.estado() == OPEN
    # NOVA instância no MESMO arquivo: deve enxergar a fonte ainda aberta
    cb2 = CircuitBreaker("api", arquivo=arq, max_falhas=2, timeout_s=300, agora=rel)
    assert cb2.estado() == OPEN


def test_redireciona_para_fonte_saudavel_quando_a_abre():
    """Simula o fluxo de leitura: fonte A aberta -> chama a fonte B (saudável)."""
    arq = _tmp_arq()
    rel = Relogio()
    a = CircuitBreaker("api", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel)
    CircuitBreaker("webhook", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel)
    a.registrar_falha()  # A abre imediatamente (N=1)
    chamou_b = []

    def ler(fonte, fn, fn_alternativa):
        try:
            return fonte.chamar(fn)
        except CircuitOpenError:
            return fn_alternativa()

    resultado = ler(a, lambda: "de A", lambda: (chamou_b.append(1), "de B")[1])
    assert resultado == "de B"
    assert chamou_b == [1], "A aberta -> B deve ser chamada no lugar"


def test_falhas_em_fontes_diferentes_sao_isoladas():
    arq = _tmp_arq()
    rel = Relogio()
    api = CircuitBreaker("api", arquivo=arq, max_falhas=2, timeout_s=300, agora=rel)
    webhook = CircuitBreaker("webhook", arquivo=arq, max_falhas=2, timeout_s=300, agora=rel)
    api.registrar_falha()
    api.registrar_falha()
    assert api.estado() == OPEN
    assert webhook.estado() == CLOSED, "falha da API não pode abrir o webhook"


# --------------------------------------------------------------------------
# Endurecimento (remediação, frente R / achados B3, B4)
# --------------------------------------------------------------------------
def _ler_json(arq):
    with open(arq, encoding="utf-8") as fh:
        return json.load(fh)


def test_persiste_motivo_e_aberto_em():
    arq = _tmp_arq()
    rel = Relogio()
    cb = CircuitBreaker("dl", arquivo=arq, max_falhas=2, timeout_s=180, agora=rel)
    cb.registrar_falha("503 QUERY_LIMIT_EXCEEDED", motivo="RATE_LIMIT")
    cb.registrar_falha("503 QUERY_LIMIT_EXCEEDED", motivo="RATE_LIMIT")
    d = _ler_json(arq)["dl"]
    assert d["estado"] == OPEN and d["motivo"] == "RATE_LIMIT"
    assert d["aberto_em"] == 1000.0 and d["aberto_ate"] == 1180.0
    rel.avanca(200)
    cb.registrar_sucesso()
    d = _ler_json(arq)["dl"]
    assert d["estado"] == CLOSED and "aberto_em" not in d and "motivo" not in d
    assert d["fechado_em"] == 1200.0 and d["ultimo_aberto_em"] == 1000.0
    assert d["ultimo_motivo"] == "RATE_LIMIT", "painel não mostra 'aberto' antigo (B3)"


def test_meia_abertura_libera_uma_unica_sonda_entre_instancias():
    """Duas instâncias no mesmo arquivo = dois processos. Só UMA testa a fonte."""
    arq = _tmp_arq()
    rel = Relogio()
    a = CircuitBreaker("dl", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel)
    b = CircuitBreaker("dl", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel)
    a.registrar_falha()
    rel.avanca(301)
    assert a.permitido() is True, "A pega a sonda"
    assert a.permitido() is True, "o dono da sonda continua liberado"
    assert b.permitido() is False, "B não pode sondar ao mesmo tempo (B4)"
    executou = []
    try:
        b.chamar(lambda: executou.append(1))
        raise AssertionError("B deveria receber CircuitOpenError")
    except CircuitOpenError as e:
        assert "MEIA-ABERTURA" in str(e)
    assert executou == []
    a.registrar_sucesso()
    assert b.permitido() is True and b.estado() == CLOSED


def test_sonda_abandonada_expira():
    arq = _tmp_arq()
    rel = Relogio()
    a = CircuitBreaker("dl", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel, sonda_s=60)
    b = CircuitBreaker("dl", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel, sonda_s=60)
    a.registrar_falha()
    rel.avanca(301)
    assert a.permitido() is True       # A pega a sonda e "morre"
    rel.avanca(30)
    assert b.permitido() is False
    rel.avanca(31)
    assert b.permitido() is True, "sonda vencida é liberada para outro processo"


def test_falha_de_outra_instancia_nao_e_apagada_por_snapshot_velho():
    """B4: antes, B gravava o seu snapshot em memória (fechado) por cima do OPEN de A."""
    arq = _tmp_arq()
    rel = Relogio()
    a = CircuitBreaker("dl", arquivo=arq, max_falhas=2, timeout_s=300, agora=rel)
    b = CircuitBreaker("dl", arquivo=arq, max_falhas=2, timeout_s=300, agora=rel)  # carregou fechado
    a.registrar_falha()
    a.registrar_falha()
    assert _ler_json(arq)["dl"]["estado"] == OPEN
    b.registrar_falha("outra falha")
    d = _ler_json(arq)["dl"]
    assert d["estado"] == OPEN, "B não pode fechar o circuito aberto por A"
    assert d["falhas"] == 3


def test_nunca_aberto_para_sempre_com_prazo_absurdo_no_arquivo():
    arq = _tmp_arq()
    with open(arq, "w", encoding="utf-8") as fh:
        json.dump({"dl": {"estado": OPEN, "falhas": 9, "aberto_ate": 1000.0 + 10 * 365 * 86400}}, fh)
    rel = Relogio()
    cb = CircuitBreaker("dl", arquivo=arq, max_falhas=3, timeout_s=300, agora=rel)
    assert cb.estado() == OPEN
    rel.avanca(301)
    assert cb.permitido() is True, "prazo absurdo é limitado (max_abertura_s)"


def test_abrir_imediato_limitado_e_finito():
    arq = _tmp_arq()
    rel = Relogio()
    cb = CircuitBreaker("dl", arquivo=arq, max_falhas=3, timeout_s=300, agora=rel, max_abertura_s=3600)
    cb.abrir("LOOP", duracao_s=10 ** 9, erro="ERROR_LOOP_DETECTED")
    d = _ler_json(arq)["dl"]
    assert d["estado"] == OPEN and d["motivo"] == "LOOP"
    assert d["aberto_ate"] == 1000.0 + 3600
    rel.avanca(3601)
    assert cb.permitido() is True


def test_gravacao_atomica_preserva_outras_chaves_e_nao_deixa_tmp():
    arq = _tmp_arq()
    with open(arq, "w", encoding="utf-8") as fh:
        json.dump({"outro": {"estado": OPEN, "falhas": 5, "aberto_ate": 9e9}}, fh)
    cb = CircuitBreaker("dl", arquivo=arq, max_falhas=1, timeout_s=300, agora=Relogio())
    cb.registrar_falha()
    tudo = _ler_json(arq)
    assert tudo["outro"]["falhas"] == 5 and tudo["dl"]["estado"] == OPEN
    pasta = os.path.dirname(arq)
    assert not [n for n in os.listdir(pasta) if ".tmp." in n], "tmp órfão"


def test_conta_falha_rejeitada_nao_conta_e_libera_sonda():
    arq = _tmp_arq()
    rel = Relogio()
    cb = CircuitBreaker("dl", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel,
                        conta_falha=lambda e: not isinstance(e, TypeError))
    try:
        cb.chamar(lambda: None + 1)
    except TypeError:
        pass
    assert cb.estado() == CLOSED and cb.falhas() == 0, "bug nosso não abre o disjuntor (B4)"
    cb.registrar_falha()
    rel.avanca(301)
    try:
        cb.chamar(lambda: None + 1)
    except TypeError:
        pass
    assert cb.estado() == HALF_OPEN
    outro = CircuitBreaker("dl", arquivo=arq, max_falhas=1, timeout_s=300, agora=rel)
    assert outro.permitido() is True, "sonda devolvida"


def test_ultimo_erro_mascarado():
    arq = _tmp_arq()
    cb = CircuitBreaker("dl", arquivo=arq, max_falhas=5, agora=Relogio())
    cb.registrar_falha("HTTP 403 em https://x.test/rest/1/segredoABC987/disk.file.get")
    assert "segredoABC987" not in _ler_json(arq)["dl"]["ultimo_erro"]


def test_sem_disco_funciona_em_memoria():
    cb = CircuitBreaker("dl", arquivo="/dev/null/nao/existe/cb.json", max_falhas=2, agora=Relogio())
    cb.registrar_falha()
    cb.registrar_falha()
    assert cb.estado() == OPEN


def _sondar_processo(arq, barreira, fila):
    from circuit_breaker import CircuitBreaker as CB
    cb = CB("dl", arquivo=arq, max_falhas=1, timeout_s=1)
    barreira.wait(timeout=30)
    fila.put(cb.permitido())


def test_uma_sonda_entre_processos_reais():
    import multiprocessing
    import time as _t
    arq = _tmp_arq()
    with open(arq, "w", encoding="utf-8") as fh:
        json.dump({"dl": {"estado": OPEN, "falhas": 3, "aberto_em": _t.time() - 10,
                          "aberto_ate": _t.time() - 5}}, fh)
    ctx = multiprocessing.get_context("spawn")
    n = 4
    barreira = ctx.Barrier(n)
    fila = ctx.Queue()
    ps = [ctx.Process(target=_sondar_processo, args=(arq, barreira, fila)) for _ in range(n)]
    for p in ps:
        p.start()
    resultados = [fila.get(timeout=60) for _ in ps]
    for p in ps:
        p.join(timeout=30)
    assert sorted(resultados) == [False] * (n - 1) + [True], resultados


# --------------------------------------------------------------------------
# runner stdlib (sem pytest)
# --------------------------------------------------------------------------
def main() -> int:
    testes = sorted((n, f) for n, f in globals().items()
                    if n.startswith("test_") and callable(f))
    falhas = []
    for nome, fn in testes:
        try:
            fn()
            print(f"PASS  {nome}")
        except Exception as e:                      # noqa: BLE001
            falhas.append((nome, e))
            print(f"FAIL  {nome}: {type(e).__name__}: {e}")
    print(f"\n{len(testes) - len(falhas)}/{len(testes)} testes OK")
    if falhas:
        for nome, e in falhas:
            print(f"  - {nome}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
