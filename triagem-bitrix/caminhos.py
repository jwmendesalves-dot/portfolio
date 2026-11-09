"""Caminhos de produção, sobrescrevíveis por variável de ambiente.

Em produção nada muda (/opt/...). Nos testes o conftest aponta TRIAGEM_BASE e
AGENTE_SEPARADOR_BASE para diretórios temporários.
"""
import os

BASE = os.environ.get("TRIAGEM_BASE") or "/opt/triagem-bitrix"
SEPARADOR = os.environ.get("AGENTE_SEPARADOR_BASE") or "/opt/agente-separador"
SEGREDOS_DIR = os.environ.get("TRIAGEM_SEGREDOS_DIR") or "/etc/triagem-bitrix"


def em_base(*partes: str) -> str:
    return os.path.join(BASE, *partes)


def em_separador(*partes: str) -> str:
    return os.path.join(SEPARADOR, *partes)
