"""Pendências que as frentes F e D deixaram para a ponte (frente E) + F2-12.

- F: a ponte decide pela regra única `estado_lote.pode_enfileirar`, não só por `pronto`
  (o completar_cpf_por_nome não marca mais pronto=True no lote confirmado por nome+telefone).
- F/D: pipeline.sh com `--aplicar --limite 30`, reconciliar_por_file_ids `--aplicar`
  explícito, e código de saída real (sem `| tail` engolindo), devolvendo o PIOR.
- F2-12: avisar-primeiro avisa job que sumiu da fila E do espelho, sem nome e com CPF mascarado.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

from conftest import RAIZ

sys.path.insert(0, RAIZ)

import cpf_util  # noqa: E402
import estado_store  # noqa: E402
from ponte_criterio import motivo_recusa  # noqa: E402
from test_ponte_rodada import arquivo, lote, rodar  # noqa: E402  (fixture `amb`: conftest.py)


# ---------------------------------------------------------------- pode_enfileirar
def test_lote_confirmado_sem_pronto_vai_pela_regra_unica(amb, capsys):  # noqa: F811
    lote(amb, "LOTE-SIG-NOMETEL", pronto=False, cpf_origem="bitrix-nome+telefone")
    amb.gravar()

    rodar(["--enviar", "--todos"], capsys)

    assert [j["id"] for j in amb.fila()] == ["grupo-LOTE-SIG-NOMETEL"]


def test_sem_pronto_e_sem_download_continua_fora(amb, capsys):  # noqa: F811
    lid = "LOTE-SIG-PEND"
    reg = lote(amb, lid, pronto=False, arquivos=[arquivo(amb, lid, 0, baixado=False)])
    amb.gravar()

    rodar(["--enviar", "--todos"], capsys)

    assert amb.fila() == []
    assert motivo_recusa(reg) == "lote não está pronto"


def test_motivo_recusa_aceita_lote_elegivel_sem_pronto(amb):  # noqa: F811
    reg = lote(amb, "LOTE-SIG-X", pronto=False)
    assert motivo_recusa(reg) == ""


# ---------------------------------------------------------------- pipeline.sh
FALSO = """import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "chamadas.log"), "a") as fh:
    fh.write(json.dumps([os.path.basename(__file__)] + sys.argv[1:]) + "\\n")
sys.exit({rc})
"""


def _env(tmp_path):
    """flock falso (o macOS não tem; no servidor é o util-linux) — a trava real é testada
    em test_ponte_rodada."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "flock").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "flock").chmod(0o755)
    return dict(os.environ, TRIAGEM_BASE=str(tmp_path), PYTHON=sys.executable,
                PATH=f"{bin_}{os.pathsep}{os.environ.get('PATH', '')}")


def test_pipeline_roda_todos_os_passos_e_devolve_o_pior_codigo(tmp_path):
    codigos = {"baixar_pendentes.py": 1, "reconciliar_estado.py": 0,
               "reconciliar_por_file_ids.py": 2, "completar_cpf_por_nome.py": 0,
               "lote_para_fila.py": 0}
    for nome, rc in codigos.items():
        (tmp_path / nome).write_text(FALSO.format(rc=rc))
    with open(os.path.join(RAIZ, "ponte-rodar.sh"), encoding="utf-8") as fh:
        (tmp_path / "ponte-rodar.sh").write_text(fh.read())
    env = _env(tmp_path)

    r = subprocess.run(["bash", os.path.join(RAIZ, "pipeline.sh")], capture_output=True,
                       text=True, timeout=60, env=env)

    chamadas = [json.loads(x) for x in (tmp_path / "chamadas.log").read_text().splitlines()]
    nomes = [c[0] for c in chamadas]
    assert nomes == ["baixar_pendentes.py", "reconciliar_estado.py", "reconciliar_por_file_ids.py",
                     "completar_cpf_por_nome.py", "lote_para_fila.py"], r.stdout + r.stderr
    por = {c[0]: c[1:] for c in chamadas}
    assert por["completar_cpf_por_nome.py"] == ["--aplicar", "--limite", "30"]
    assert "--aplicar" in por["reconciliar_por_file_ids.py"]
    assert "--todos-os-fechamentos" not in por["lote_para_fila.py"]
    assert r.returncode == 2, r.stdout + r.stderr


def test_pipeline_tudo_ok_sai_zero(tmp_path):
    for nome in ("baixar_pendentes.py", "reconciliar_estado.py", "reconciliar_por_file_ids.py",
                 "completar_cpf_por_nome.py", "lote_para_fila.py"):
        (tmp_path / nome).write_text(FALSO.format(rc=0))
    with open(os.path.join(RAIZ, "ponte-rodar.sh"), encoding="utf-8") as fh:
        (tmp_path / "ponte-rodar.sh").write_text(fh.read())
    env = _env(tmp_path)
    r = subprocess.run(["bash", os.path.join(RAIZ, "pipeline.sh")], capture_output=True,
                       text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------------------- F2-12 avisar-primeiro
def _avisar(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("avisar_primeiro", os.path.join(RAIZ, "avisar-primeiro.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for nome, arq in (("CTRL", "fila-enviados.json"), ("FASE1", "estado.json"), ("FILA", "fila-jobs.json"),
                      ("ESPELHO", "jobs.json"), ("ESTADO", "aviso-estado.json")):
        monkeypatch.setattr(mod, nome, str(tmp_path / arq))
    return mod


def test_job_sumido_da_fila_e_do_espelho_avisa_uma_vez_sem_nome(tmp_path, monkeypatch, capsys):
    import datetime as dt
    cpf = cpf_util.gerar_para_teste(7)
    agora = dt.datetime.now(dt.timezone.utc).isoformat()
    (tmp_path / "fila-enviados.json").write_text(json.dumps({"LOTE-SIG-SUMIU": {
        "job_id": "grupo-LOTE-SIG-SUMIU", "em": agora, "confirmado_em": agora,
        "cliente": {"nome": "Fulana Ficticia de Teste", "cpf": cpf}}}))
    (tmp_path / "fila-jobs.json").write_text("[]")
    (tmp_path / "jobs.json").write_text("[]")
    estado_store.gravar_atomico({"lotes": {}}, str(tmp_path / "estado.json"))
    mod = _avisar(tmp_path, monkeypatch)

    assert mod.main() == 0
    out1 = capsys.readouterr().out
    assert mod.main() == 0
    out2 = capsys.readouterr().out

    assert "SUMIU" in out1 and "LOTE-SIG-SUMIU" in out1
    assert cpf not in out1 and cpf_util.formatar(cpf) not in out1
    assert "Fulana" not in out1
    assert "SUMIU" not in out2                      # uma vez só
