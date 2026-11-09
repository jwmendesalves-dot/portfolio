"""cpf_util — contrato (CONTRATOS.md). CPFs só sintéticos (gerar_para_teste)."""
import pytest

import cpf_util as C

CPF_A = C.gerar_para_teste(1)
CPF_B = C.gerar_para_teste(2)


def _fmt(c):
    return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}"


# ---------------------------------------------------------------- normalizar
@pytest.mark.parametrize("entrada,esperado", [
    (CPF_A, CPF_A),
    (_fmt(CPF_A), CPF_A),
    (f"  {_fmt(CPF_A)}  ", CPF_A),
    (f"{CPF_A[:3]} {CPF_A[3:6]} {CPF_A[6:9]} {CPF_A[9:]}", CPF_A),
    (f"{CPF_A[:9]}/{CPF_A[9:]}", CPF_A),
    (int(CPF_A), CPF_A.lstrip("0").zfill(11)),
    (1234567890, "01234567890"),          # int perde zero à esquerda: restaura
    ("0" + "1234567890", "01234567890"),
])
def test_normalizar_aceita(entrada, esperado):
    assert C.normalizar(entrada) == esperado


@pytest.mark.parametrize("entrada", [
    None, "", "   ", "123", "1234567890", "123456789012", 123456789012, -1, True, False,
    12.5, b"12345678901", ["12345678901"],
    "CPF 698.308.525-63",                 # texto com letras não é normalizado aqui
    "123.456.789-0a",
])
def test_normalizar_rejeita(entrada):
    assert C.normalizar(entrada) is None


# ---------------------------------------------------------------- valido
@pytest.mark.parametrize("semente", range(50))
def test_gerados_sao_validos_em_todos_formatos(semente):
    c = C.gerar_para_teste(semente)
    assert len(c) == 11 and c.isdigit()
    assert C.valido(c) and C.valido(_fmt(c)) and C.valido(int(c))


@pytest.mark.parametrize("d", "0123456789")
def test_sequencia_repetida_invalida(d):
    # 000.000.000-00 e 111.111.111-11 passam no cálculo do DV, mas não são CPF
    assert not C.valido(d * 11)
    assert not C.valido(_fmt(d * 11))


def test_dv_errado_invalido():
    for i in (9, 10):
        errado = CPF_A[:i] + str((int(CPF_A[i]) + 1) % 10) + CPF_A[i + 1:]
        assert not C.valido(errado)


@pytest.mark.parametrize("entrada", [None, "", "abc", "123", 0, "1234567890a"])
def test_valido_lixo(entrada):
    assert C.valido(entrada) is False


def _dv_oraculo(base9):
    """Cálculo do DV escrito do jeito da Receita (11 - resto), independente do módulo."""
    def d(nums):
        r = sum(n * p for n, p in zip(nums, range(len(nums) + 1, 1, -1))) % 11
        return 0 if r < 2 else 11 - r
    nums = [int(x) for x in base9]
    d1 = d(nums)
    return f"{d1}{d(nums + [d1])}"


@pytest.mark.parametrize("base9", ["000000001", "123456789", "987654321", "100200300", "999999998"])
def test_dv_bate_com_oraculo(base9):
    cpf = base9 + _dv_oraculo(base9)
    assert C.valido(cpf)
    errado = base9 + str((int(cpf[9]) + 1) % 10) + cpf[10]
    assert not C.valido(errado)


# ---------------------------------------------------------------- formatar / mascarar
def test_formatar():
    assert C.formatar(CPF_A) == _fmt(CPF_A)
    assert C.formatar(_fmt(CPF_B)) == _fmt(CPF_B)
    assert C.formatar(1234567890) == "012.345.678-90"


@pytest.mark.parametrize("entrada", [None, "", "123", "abc"])
def test_formatar_rejeita(entrada):
    with pytest.raises(ValueError):
        C.formatar(entrada)


def test_mascarar_cpf():
    m = C.mascarar(CPF_A)
    assert m == f"***.{CPF_A[3:6]}.{CPF_A[6:9]}-**"
    assert CPF_A not in m and C.mascarar("lixo") == "***"


# ---------------------------------------------------------------- gerar_para_teste
def test_gerar_deterministico_e_variado():
    assert C.gerar_para_teste(42) == C.gerar_para_teste(42)
    gerados = {C.gerar_para_teste(i) for i in range(200)}
    assert len(gerados) > 190
