"""Configuração comum da suíte — torna os testes independentes do servidor.

- Põe a raiz do repositório no sys.path (os testes antigos fazem
  sys.path.insert(0, "/opt/triagem-bitrix"), que não existe fora do servidor).
- TRIAGEM_BASE aponta para um diretório temporário da sessão: nenhum teste lê
  ou grava estado real de produção.
- Rede bloqueada: qualquer conexão fora de localhost levanta OSError
  (nenhum teste pode chamar Bitrix, VibeCode, Telegram ou Google).
"""
import atexit
import os
import shutil
import socket
import sys
import tempfile

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

_BASE_TESTE = tempfile.mkdtemp(prefix="triagem-teste-")
atexit.register(shutil.rmtree, _BASE_TESTE, ignore_errors=True)
os.environ.setdefault("TRIAGEM_BASE", _BASE_TESTE)
os.environ.setdefault("AGENTE_SEPARADOR_BASE", os.path.join(_BASE_TESTE, "agente-separador"))
os.environ.setdefault("TRIAGEM_SEM_REDE", "1")

_connect_original = socket.socket.connect


def _connect_bloqueado(self, endereco, *a, **k):
    host = endereco[0] if isinstance(endereco, tuple) else endereco
    if not isinstance(endereco, tuple) or (
        isinstance(host, str) and (host.startswith("127.") or host in ("localhost", "::1"))
    ):
        return _connect_original(self, endereco, *a, **k)
    raise OSError("rede bloqueada nos testes: %r" % (endereco,))


socket.socket.connect = _connect_bloqueado


# ---------------------------------------------------------------------------
# Fixtures compartilhadas entre arquivos. Antes cada arquivo fazia
# `from fakes_download import rede` (padrão que o pyflakes acusa como
# "redefinition of unused"); o corpo continua no módulo de origem e é
# importado só quando o fixture é pedido. Fixture local de mesmo nome
# (test_bitrix_cliente, test_completar_cpf, test_triagem_download) prevalece.
# ---------------------------------------------------------------------------
@pytest.fixture
def rede(tmp_path, monkeypatch):
    from fakes_download import montar_rede
    return montar_rede(tmp_path, monkeypatch)


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    from test_fase1_cursor import montar_ambiente
    return montar_ambiente(tmp_path, monkeypatch)


@pytest.fixture
def amb(tmp_path, monkeypatch):
    from test_ponte_rodada import montar_amb
    return montar_amb(tmp_path, monkeypatch)
