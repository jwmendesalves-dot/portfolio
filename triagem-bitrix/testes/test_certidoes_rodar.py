# -*- coding: utf-8 -*-
"""OP-O4/CE-05 — certidoes-rodar.sh: o CÓDIGO DE SAÍDA REAL do certidoes.py precisa chegar
ao systemd (o script antigo terminava com `echo ... (exit $?)` e saía SEMPRE 0).

Sem rede: o python e o flock são stubs; nada de produção é tocado.
"""
import os
import subprocess

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(RAIZ, "certidoes-rodar.sh")


def _stub(caminho, corpo):
    with open(caminho, "w", encoding="utf-8") as fh:
        fh.write(corpo)
    os.chmod(caminho, 0o755)
    return str(caminho)


def _rodar(tmp_path, exit_do_python=0, exit_do_flock=0):
    python_stub = _stub(str(tmp_path / "python-stub"),
                        f"#!/bin/sh\nexit {exit_do_python}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(str(bin_dir / "flock"), f"#!/bin/sh\nexit {exit_do_flock}\n")
    env = dict(os.environ,
               TRIAGEM_BASE=str(tmp_path),
               TRIAGEM_LOCK=str(tmp_path / "certidoes.lock"),
               PYTHON=python_stub,
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env)


def test_op_o4_exit_code_real_propaga_ao_systemd(tmp_path):
    r = _rodar(tmp_path, exit_do_python=7)
    assert r.returncode == 7, r.stdout + r.stderr   # antigo: SEMPRE 0 (o echo engolia)
    assert "(exit 7)" in r.stdout


def test_op_o4_sucesso_continua_saindo_0(tmp_path):
    r = _rodar(tmp_path, exit_do_python=0)
    assert r.returncode == 0, r.stdout + r.stderr


def test_op_o4_trava_ocupada_sai_0_com_aviso(tmp_path):
    r = _rodar(tmp_path, exit_do_python=0, exit_do_flock=1)
    assert r.returncode == 0
    assert "pulando" in r.stdout


def test_op_o4_script_tem_pipefail_e_exit_real():
    with open(SCRIPT, encoding="utf-8") as fh:
        fonte = fh.read()
    assert "set -euo pipefail" in fonte
    assert 'exit "$code"' in fonte
