"""CPF: normalização, validação e apresentação — fonte única (CONTRATOS.md).

Todas as frentes importam daqui; não reimplemente cálculo de DV em outro módulo.

    normalizar(valor) -> str | None   só dígitos; 11 dígitos -> str; senão None
    valido(valor) -> bool             DV correto, rejeita sequências repetidas
    formatar(valor) -> str            000.000.000-00 (só apresentação)
    gerar_para_teste(semente) -> str  CPF sintético com DV válido (fixtures)
    mascarar(valor) -> str            ***.000.000-** (para log; extra ao contrato)
"""
from __future__ import annotations

import random
import re

# Só aceita dígitos e a pontuação usual de CPF digitado (ponto, hífen, barra,
# espaços). Texto com letras ("CPF 123...", "RG 123...") NÃO é normalizado aqui:
# quem extrai CPF de texto livre deve isolar o número antes.
_PERMITIDOS = re.compile(r"^[\d.\-/\s]+$")


def normalizar(valor) -> str | None:
    """Devolve os 11 dígitos do CPF ou None.

    Aceita int (zeros à esquerda são restaurados: 1234567890 -> "01234567890")
    e str com pontos, hífen, barra e espaços. Não verifica o DV (use `valido`).
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        if valor < 0:
            return None
        s = str(valor)
        return s.zfill(11) if len(s) <= 11 else None
    if not isinstance(valor, str):
        return None
    s = valor.strip()
    if not s or not _PERMITIDOS.match(s):
        return None
    digitos = re.sub(r"\D", "", s)
    return digitos if len(digitos) == 11 else None


def _dv(base: str) -> str:
    """Calcula os 2 dígitos verificadores para os 9 primeiros dígitos."""
    soma = sum(int(d) * p for d, p in zip(base, range(10, 1, -1)))
    d1 = (soma * 10) % 11 % 10
    soma = sum(int(d) * p for d, p in zip(base + str(d1), range(11, 1, -1)))
    d2 = (soma * 10) % 11 % 10
    return f"{d1}{d2}"


def valido(valor) -> bool:
    """True se `valor` é um CPF com DV correto e não é sequência repetida."""
    n = normalizar(valor)
    if n is None or n == n[0] * 11:
        return False
    return _dv(n[:9]) == n[9:]


def formatar(valor) -> str:
    """000.000.000-00. Só apresentação: não valida o DV.

    Levanta ValueError se `valor` não tiver 11 dígitos (não devolve lixo
    formatado que pareça CPF).
    """
    n = normalizar(valor)
    if n is None:
        raise ValueError("CPF não normalizável (esperado 11 dígitos)")
    return f"{n[:3]}.{n[3:6]}.{n[6:9]}-{n[9:]}"


def mascarar(valor) -> str:
    """***.456.789-** — mantém só os 6 dígitos do meio (padrão de órgão público).

    Valor não normalizável vira "***" (nunca devolve o valor original).
    """
    n = normalizar(valor)
    if n is None:
        return "***"
    return f"***.{n[3:6]}.{n[6:9]}-**"


def gerar_para_teste(semente: int) -> str:
    """CPF sintético (11 dígitos, DV válido), determinístico pela semente.

    Para fixtures: a mesma semente sempre gera o mesmo CPF; nunca devolve
    sequência repetida. Não use CPF real em teste.
    """
    rnd = random.Random(int(semente))
    while True:
        base = "".join(str(rnd.randint(0, 9)) for _ in range(9))
        cpf = base + _dv(base)
        if cpf != cpf[0] * 11:
            return cpf
