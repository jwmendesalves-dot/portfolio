"""Rodada da PONTE de ponta a ponta, offline, em diretório temporário (frente E).

Cada teste reproduz um achado da auditoria 04 (F2-xx), 01 (S02/S04/S09/S10) ou 06 (OP-O2)
e falha no código original. O `node` do painel é substituído por um falso (Python) que
imita o que o legado ASSUME de `FilaJobs.enfileirar` (merge por id, gravação atômica,
`criado=false` para id existente) — o comportamento real precisa ser conferido no
servidor (ver docs/remediacao/frentes/E-ponte.md).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import textwrap

import pytest

from conftest import RAIZ

sys.path.insert(0, RAIZ)

import caminhos  # noqa: E402
import cpf_util  # noqa: E402
import estado_store  # noqa: E402
import lote_para_fila as lpf  # noqa: E402
import lote_para_job  # noqa: E402

NOME_OK = "Maria Aparecida da Silva"

NODE_FALSO = r'''#!{py}
import json, os, sys
script, filajs, fila, entradas = sys.argv[1:5]
log = os.environ.get("FAKE_NODE_LOG")
if log:
    with open(log, "w") as fh:
        json.dump({{"script": script, "entradas": entradas,
                   "modo_script": oct(os.stat(script).st_mode & 0o777),
                   "modo_entradas": oct(os.stat(entradas).st_mode & 0o777),
                   "tem_filajobs": "FilaJobs" in open(script).read(),
                   "filajs": filajs}}, fh)
if os.environ.get("FAKE_NODE_FALHA") == "antes":
    sys.exit(3)
extra = os.environ.get("FAKE_NODE_CONTROLE_EXTRA")
if extra:  # outra instância grava no controle enquanto esta roda o node
    ctrl_path, lid = extra.split("::")
    ctrl = json.load(open(ctrl_path)) if os.path.exists(ctrl_path) else {{}}
    ctrl[lid] = {{"job_id": "grupo-" + lid, "upload_id": "u-outra", "em": "2026-11-09T10:00:00Z",
                 "enviados": 1, "cliente": {{"nome": "", "cpf": ""}}, "arquivos": []}}
    tmp = ctrl_path + ".outra"
    json.dump(ctrl, open(tmp, "w")); os.replace(tmp, ctrl_path)
jobs = json.load(open(fila)) if os.path.exists(fila) else []
ids = {{j["id"] for j in jobs}}
saida = []
for e in json.load(open(entradas)):
    if e["id"] in ids:
        saida.append({{"id": e["id"], "criado": False}})
    else:
        jobs.append(e); ids.add(e["id"]); saida.append({{"id": e["id"], "criado": True}})
tmp = fila + ".tmp"
json.dump(jobs, open(tmp, "w")); os.replace(tmp, fila)
if os.environ.get("FAKE_NODE_FALHA") == "depois":
    sys.exit(4)
print(json.dumps(saida))
'''


# ---------------------------------------------------------------- ambiente
def montar_amb(tmp_path, monkeypatch):
    """Corpo do fixture `amb` (registrado em conftest.py)."""
    base = tmp_path / "base"
    sep = tmp_path / "sep"
    (base / "lotes").mkdir(parents=True)
    (sep / "data" / "staging").mkdir(parents=True)
    (sep / "src").mkdir(parents=True)
    node = tmp_path / "node"
    node.write_text(NODE_FALSO.format(py=sys.executable))
    node.chmod(0o755)
    monkeypatch.setattr(lpf, "ARQ_ESTADO", str(base / "estado.json"))
    monkeypatch.setattr(lpf, "CONTROLE", str(base / "fila-enviados.json"))
    monkeypatch.setattr(lpf, "BACKUPS", str(base / "backups"))
    monkeypatch.setattr(lpf, "TRAVA_RODADA", str(base / "fila"))
    monkeypatch.setattr(lpf, "STAGING", str(sep / "data" / "staging"))
    monkeypatch.setattr(lpf, "FILA", str(sep / "data" / "fila-jobs.json"))
    monkeypatch.setattr(lpf, "ESPELHO", str(sep / "data" / "jobs.json"))
    monkeypatch.setattr(lpf, "FILA_JS", str(sep / "src" / "fila-jobs.js"))
    monkeypatch.setattr(lpf, "NODE", str(node))
    monkeypatch.setattr(lote_para_job, "codigo_pasta_do_cliente", lambda cpf: "")
    monkeypatch.delenv("TRIAGEM_FILA_LOCK_FD", raising=False)
    (sep / "data" / "fila-jobs.json").write_text("[]")

    class A:
        pass

    a = A()
    a.base, a.sep, a.tmp = base, sep, tmp_path
    a.lotes = {}

    def gravar():
        estado_store.gravar_atomico({"lotes": a.lotes}, str(base / "estado.json"))

    def estado():
        return estado_store.ler_estado(str(base / "estado.json"))

    def controle():
        return estado_store.ler_estado(str(base / "fila-enviados.json"))

    def fila():
        return json.loads((sep / "data" / "fila-jobs.json").read_text())

    a.gravar, a.estado, a.controle, a.fila = gravar, estado, controle, fila
    return a


def _iso(dias_atras=0.0):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias_atras)).isoformat()


def arquivo(a, lid, i, baixado=True, ext=".jpg", conteudo=None, tipo=None, nome_original=None):
    dados = conteudo if conteudo is not None else b"\xff\xd8\xff" + f"{lid}-{i}".encode()
    fid = f"{lid[-6:]}{i}"
    meta = {"file_id": fid, "indice": i + 1, "nome_original": nome_original or f"doc{i}{ext}"}
    if tipo:
        meta["tipo"] = tipo
    if baixado:
        p = a.base / "lotes" / f"{lid}_{i}{ext}"
        p.write_bytes(dados)
        meta.update({"nome_salvo": p.name, "caminho": str(p), "baixado": True,
                     "sha256": hashlib.sha256(dados).hexdigest(), "tamanho_bits": len(dados)})
    else:
        meta.update({"baixado": False, "caminho": None, "erro": "pausado"})
    return meta


def lote(a, lid, semente=1, n=2, dias_atras=1.0, traco=True, nome=NOME_OK, arquivos=None, **extra):
    cpf = cpf_util.gerar_para_teste(semente)
    arqs = arquivos if arquivos is not None else [arquivo(a, lid, i) for i in range(n)]
    reg = {"lote_id": lid, "modo": "marcadores" if traco else "janela",
           "status": "FECHADO_PRONTO" if traco else "FECHADO_POR_JANELA",
           "sinal_abertura": {"message_id": 1} if traco else None,
           "sinal_fechamento": {"message_id": 2} if traco else None,
           "fechado_por": "sinal de traço (fechamento)" if traco else "janela de tempo",
           "nome": nome, "cpf": cpf, "cpf_formatado": cpf_util.formatar(cpf), "cpfs_distintos": [cpf],
           "identificacao": "OK", "requer_revisao": False, "pronto": True, "arquivos": arqs,
           "file_ids": [x["file_id"] for x in arqs], "qtd_arquivos": len(arqs), "autores": [],
           "inicio": _iso(dias_atras), "fim": _iso(dias_atras), "mensagens": [],
           "confianca": "ALTA" if traco else "BAIXA"}
    reg.update(extra)
    a.lotes[lid] = reg
    return reg


def rodar(args, capsys):
    rc = lpf.main(args)
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------- F2-02 / C1
def test_kit_com_documento_nao_baixado_nao_vai_parcial(amb, capsys):
    lote(amb, "LOTE-SIG-A", arquivos=[arquivo(amb, "LOTE-SIG-A", 0),
                                      arquivo(amb, "LOTE-SIG-A", 1, baixado=False)])
    amb.gravar()

    rc, out = rodar(["--enviar", "--todos"], capsys)

    assert amb.fila() == []
    assert amb.controle() == {}
    assert "AGUARDANDO_DOWNLOAD" in out
    assert not os.listdir(lpf.STAGING)


def test_midia_nao_e_pendencia_nem_vai_ao_staging(amb, capsys):
    lid = "LOTE-SIG-M"
    arqs = [arquivo(amb, lid, 0), arquivo(amb, lid, 1),
            arquivo(amb, lid, 2, baixado=False, ext=".mp4"),
            arquivo(amb, lid, 3, ext=".ogg", conteudo=b"OggS-audio")]
    lote(amb, lid, arquivos=arqs)
    amb.gravar()

    rodar(["--enviar", "--todos"], capsys)

    assert [j["id"] for j in amb.fila()] == [f"grupo-{lid}"]
    uid = amb.fila()[0]["upload_id"]
    nomes = sorted(os.listdir(os.path.join(lpf.STAGING, uid)))
    assert len([n for n in nomes if not n.endswith(".meta.json")]) == 2
    assert not any(n.endswith((".mp4", ".ogg", ".ogg.meta.json")) for n in nomes)


# ---------------------------------------------------------------- S10
def test_desconhecido_so_vai_se_o_conteudo_provar_documento(amb, capsys):
    lid_ok, lid_ruim = "LOTE-SIG-PDF", "LOTE-SIG-BIN"
    lote(amb, lid_ok, semente=2, arquivos=[
        arquivo(amb, lid_ok, 0, ext="", conteudo=b"%PDF-1.4 sintetico", nome_original="arquivo")])
    lote(amb, lid_ruim, semente=3, arquivos=[
        arquivo(amb, lid_ruim, 0, ext="", conteudo=b"MZ\x90\x00executavel", nome_original="arquivo")])
    amb.gravar()

    rodar(["--enviar", "--todos"], capsys)

    ids = [j["id"] for j in amb.fila()]
    assert ids == [f"grupo-{lid_ok}"]
    uid = amb.fila()[0]["upload_id"]
    assert any(n.endswith(".pdf") for n in os.listdir(os.path.join(lpf.STAGING, uid)))
    ruim = amb.estado()["lotes"][lid_ruim]
    assert ruim["requer_revisao"] is True
    assert any("tipo não reconhecido" in m for m in ruim["motivos_revisao"])


# ---------------------------------------------------------------- F2-03
def _controle_com_envio(amb, lid_origem, semente, metas):
    cpf = cpf_util.gerar_para_teste(semente)
    estado_store.gravar_atomico({lid_origem: {
        "job_id": f"grupo-{lid_origem}", "upload_id": "u0", "em": "2026-11-01T10:00:00Z",
        "enviados": len(metas), "cliente": {"nome": NOME_OK, "cpf": cpf_util.formatar(cpf)},
        "arquivos": [f"{m['sha256'][:12]}.jpg" for m in metas]}}, lpf.CONTROLE)


def test_duplicata_com_download_pendente_e_indeterminada(amb):
    velho = [arquivo(amb, "LOTE-V", 0), arquivo(amb, "LOTE-V", 1)]
    _controle_com_envio(amb, "LOTE-V", 7, velho)
    lid = "LOTE-SIG-NOVO"
    lote(amb, lid, semente=7, arquivos=[dict(velho[0]), dict(velho[1]),
                                        arquivo(amb, lid, 2, baixado=False)])

    sel = lpf.selecionar(amb.lotes, amb.controle())

    assert sel["descartes"] == {}
    assert lid in [x for x, _m in sel["fora"].get("AGUARDANDO_DOWNLOAD", [])]


def test_duplicata_com_kit_completo_e_descartada_com_cpf_mascarado(amb, capsys):
    velho = [arquivo(amb, "LOTE-V", 0), arquivo(amb, "LOTE-V", 1)]
    _controle_com_envio(amb, "LOTE-V", 8, velho)
    lid = "LOTE-SIG-REP"
    reg = lote(amb, lid, semente=8, arquivos=[dict(velho[0]), dict(velho[1])])
    amb.gravar()

    rc, out = rodar(["--enviar", "--todos"], capsys)

    assert amb.estado()["lotes"][lid]["status"] == "descartado_duplicata"
    assert amb.fila() == []
    assert reg["cpf"] not in out and reg["cpf_formatado"] not in out          # S04
    assert NOME_OK not in out


# ---------------------------------------------------------------- F2-04
def test_limite_conta_so_enviaveis_e_traco_vai_primeiro(amb, capsys):
    for i in range(4):                     # os mais antigos, sem download: não ocupam vaga
        lid = f"LOTE-2026110{i}-SEM"
        lote(amb, lid, semente=20 + i, dias_atras=5 - i * 0.1, traco=False,
             arquivos=[arquivo(amb, lid, 0, baixado=False)])
    lote(amb, "LOTE-20261108-JAN", semente=30, dias_atras=3, traco=False)
    lote(amb, "LOTE-SIG-20261109-TRACO", semente=31, dias_atras=0.5)
    lote(amb, "LOTE-SIG-20261108-TRACO", semente=32, dias_atras=2)
    amb.gravar()

    rodar(["--enviar", "--todos", "--limite", "2"], capsys)

    assert [j["id"] for j in amb.fila()] == ["grupo-LOTE-SIG-20261108-TRACO",
                                              "grupo-LOTE-SIG-20261109-TRACO"]


def test_janela_de_dias_padrao_e_7(amb, capsys):
    lote(amb, "LOTE-SIG-VELHO", semente=40, dias_atras=10)
    amb.gravar()

    rc, out = rodar(["--enviar", "--todos"], capsys)

    assert amb.fila() == []
    assert "FORA_DA_JANELA" in out


# ---------------------------------------------------------------- F2-05 / A4
def test_controle_e_relido_sob_trava_e_nao_perde_registro_de_outra_instancia(amb, capsys, monkeypatch):
    lote(amb, "LOTE-SIG-C1", semente=50)
    amb.gravar()
    monkeypatch.setenv("FAKE_NODE_CONTROLE_EXTRA", f"{lpf.CONTROLE}::LOTE-DA-OUTRA")

    rodar(["--enviar", "--todos"], capsys)

    ctrl = amb.controle()
    assert "LOTE-DA-OUTRA" in ctrl and "LOTE-SIG-C1" in ctrl
    assert ctrl["LOTE-SIG-C1"]["estado"] == "enfileirado"
    assert stat.S_IMODE(os.stat(lpf.CONTROLE).st_mode) == 0o600


def test_rodada_concorrente_pula_quando_a_trava_esta_ocupada(amb, capsys):
    lote(amb, "LOTE-SIG-T", semente=51)
    amb.gravar()
    trava = lpf.TRAVA_RODADA + ".lock"
    segura = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import fcntl, os, sys, time
        fd = os.open({trava!r}, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("ok", flush=True)
        time.sleep(30)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert segura.stdout.readline().strip() == "ok"
        rc, out = rodar(["--enviar", "--todos"], capsys)
    finally:
        segura.kill()
        segura.wait()

    assert rc == 0 and "outra rodada" in out
    assert amb.fila() == []


def test_trava_herdada_do_shell_e_reconhecida(amb, monkeypatch):
    import fcntl

    trava = lpf.TRAVA_RODADA + ".lock"
    fd = os.open(trava, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        monkeypatch.setenv("TRIAGEM_FILA_LOCK_FD", str(fd))
        with lpf.trava_rodada() as modo:
            assert modo == "herdada"
        monkeypatch.setenv("TRIAGEM_FILA_LOCK_FD", "99999")     # fd falso não vale
        assert lpf._fd_trava_herdada() is None
    finally:
        os.close(fd)


def test_scripts_shell_usam_a_mesma_trava_e_nao_desligam_a_regra():
    rodar_sh = open(os.path.join(RAIZ, "ponte-rodar.sh"), encoding="utf-8").read()
    pipe = open(os.path.join(RAIZ, "pipeline.sh"), encoding="utf-8").read()

    assert "--todos-os-fechamentos" not in rodar_sh + pipe.replace("nada de --todos-os-fechamentos", "")
    assert '"$BASE/fila.lock"' in rodar_sh and "flock -n 9" in rodar_sh
    assert "TRIAGEM_FILA_LOCK_FD=9" in rodar_sh
    assert "ponte-rodar.sh" in pipe and "lote_para_fila.py" not in pipe.split("4/4")[-1]
    assert lpf.TRAVA_RODADA + ".lock" == caminhos.em_base("fila.lock") or \
        os.path.basename(lpf.TRAVA_RODADA) == "fila"
    for sh in ("ponte-rodar.sh", "pipeline.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(RAIZ, sh)]).returncode == 0


# ---------------------------------------------------------------- S09
def test_temporarios_do_node_sao_privados_e_removidos(amb, capsys, monkeypatch):
    lote(amb, "LOTE-SIG-TMP", semente=52)
    amb.gravar()
    log = amb.tmp / "node-log.json"
    monkeypatch.setenv("FAKE_NODE_LOG", str(log))

    rodar(["--enviar", "--todos"], capsys)

    info = json.loads(log.read_text())
    assert info["script"].startswith(caminhos.em_base("tmp") + os.sep)
    assert not info["script"].startswith("/tmp/")
    assert info["modo_script"] == "0o600" and info["modo_entradas"] == "0o600"
    assert info["tem_filajobs"] and info["filajs"] == lpf.FILA_JS
    assert not os.path.exists(info["script"]) and not os.path.exists(info["entradas"])


# ---------------------------------------------------------------- F2-06
def test_falha_do_node_limpa_staging_e_registro_e_reexecucao_usa_o_mesmo_upload_id(amb, capsys, monkeypatch):
    lid = "LOTE-SIG-F"
    reg = lote(amb, lid, semente=53)
    amb.gravar()
    monkeypatch.setenv("FAKE_NODE_FALHA", "antes")

    rc, _ = rodar(["--enviar", "--todos"], capsys)

    assert rc == 1
    assert amb.fila() == [] and amb.controle() == {}
    assert os.listdir(lpf.STAGING) == []

    monkeypatch.delenv("FAKE_NODE_FALHA")
    rodar(["--enviar", "--todos"], capsys)

    esperado = lpf.upload_id_para(lid, sorted(x["sha256"] for x in reg["arquivos"]))
    assert amb.fila()[0]["upload_id"] == esperado
    assert amb.controle()[lid]["upload_id"] == esperado
    assert os.listdir(lpf.STAGING) == [esperado]


def test_node_grava_e_falha_depois_o_controle_confirma_pelo_que_esta_na_fila(amb, capsys, monkeypatch):
    lote(amb, "LOTE-SIG-D", semente=54)
    amb.gravar()
    monkeypatch.setenv("FAKE_NODE_FALHA", "depois")

    rodar(["--enviar", "--todos"], capsys)

    c = amb.controle()["LOTE-SIG-D"]
    assert c["estado"] == "enfileirado"
    assert c["upload_id"] == amb.fila()[0]["upload_id"]


def test_orfao_enfileirando_sem_job_e_limpo_e_o_lote_volta(amb, capsys):
    lid = "LOTE-SIG-O"
    lote(amb, lid, semente=55)
    amb.gravar()
    orfao = os.path.join(lpf.STAGING, "uid-orfao")
    os.makedirs(orfao)
    open(os.path.join(orfao, "x.jpg"), "wb").close()
    estado_store.gravar_atomico({lid: {"estado": "enfileirando", "job_id_planejado": f"grupo-{lid}",
                                       "upload_id_planejado": "uid-orfao", "em": "2026-11-09T00:00:00Z"}},
                                lpf.CONTROLE)

    rodar(["--enviar", "--todos"], capsys)

    assert not os.path.exists(orfao)
    assert amb.controle()[lid]["estado"] == "enfileirado"
    assert [j["id"] for j in amb.fila()] == [f"grupo-{lid}"]


def test_enfileirando_com_job_na_fila_e_promovido_com_upload_id_da_fila(amb, capsys):
    lid = "LOTE-SIG-P"
    lote(amb, lid, semente=56)
    amb.gravar()
    (amb.sep / "data" / "fila-jobs.json").write_text(json.dumps(
        [{"id": f"grupo-{lid}", "upload_id": "uid-da-fila", "status": "pendente"}]))
    estado_store.gravar_atomico({lid: {"estado": "enfileirando", "job_id_planejado": f"grupo-{lid}",
                                       "upload_id_planejado": "uid-planejado", "em": "x"}}, lpf.CONTROLE)

    rodar(["--enviar", "--todos"], capsys)

    c = amb.controle()[lid]
    assert c["estado"] == "enfileirado" and c["upload_id"] == "uid-da-fila"
    assert len(amb.fila()) == 1


# ---------------------------------------------------------------- F2-01 força manual
def test_forcar_exige_operador_e_motivo(amb, capsys):
    lote(amb, "LOTE-R", semente=57, requer_revisao=True, motivos_revisao=["conferir"])
    amb.gravar()

    assert rodar(["--enviar", "--lote", "LOTE-R", "--forcar"], capsys)[0] == 2
    assert rodar(["--enviar", "--todos", "--forcar", "--operador", "x", "--motivo", "y"], capsys)[0] == 2
    assert amb.fila() == []


def test_forcar_ignora_revisao_mas_registra_quem_e_por_que(amb, capsys):
    lote(amb, "LOTE-R", semente=58, requer_revisao=True, motivos_revisao=["conferir"])
    amb.gravar()

    rodar(["--enviar", "--lote", "LOTE-R", "--forcar", "--operador", "Operadora Teste",
           "--motivo", "conferido no painel"], capsys)

    assert [j["id"] for j in amb.fila()] == ["grupo-LOTE-R"]
    f = amb.controle()["LOTE-R"]["forcado"]
    assert f["operador"] == "Operadora Teste" and f["motivo"] == "conferido no painel"
    assert f["ignorados"]


@pytest.mark.parametrize("estrago", ["download", "cpf"])
def test_forcar_nao_ignora_integridade_nem_identidade(amb, capsys, estrago):
    lid = "LOTE-X"
    if estrago == "download":
        lote(amb, lid, semente=59, arquivos=[arquivo(amb, lid, 0), arquivo(amb, lid, 1, baixado=False)])
    else:
        lote(amb, lid, semente=59, cpf="12345678900")          # DV inválido
    amb.gravar()

    rodar(["--enviar", "--lote", lid, "--forcar", "--operador", "Op", "--motivo", "teste"], capsys)

    assert amb.fila() == [] and amb.controle() == {}


def test_todos_os_fechamentos_nao_desliga_a_regra_do_escritorio(amb, capsys):
    lote(amb, "LOTE-SIG-RUIDO", semente=60, nome="Solteira")
    amb.gravar()

    rodar(["--enviar", "--todos", "--todos-os-fechamentos"], capsys)

    assert amb.fila() == []


# ---------------------------------------------------------------- --simular
def test_simular_e_offline_nao_grava_e_decide_igual_ao_real(amb, capsys, monkeypatch):
    lote(amb, "LOTE-SIG-S1", semente=61)
    lote(amb, "LOTE-SIG-S2", semente=62, arquivos=[arquivo(amb, "LOTE-SIG-S2", 0, baixado=False)])
    amb.gravar()
    antes = open(lpf.ARQ_ESTADO, "rb").read()

    def rede(_cpf):
        raise AssertionError("--simular não pode consultar o Bitrix")

    monkeypatch.setattr(lote_para_job, "codigo_pasta_do_cliente", rede)
    rc, out = rodar(["--simular", "--todos"], capsys)

    assert rc == 0 and "1 lote(s) iriam" in out
    assert open(lpf.ARQ_ESTADO, "rb").read() == antes
    assert not os.path.exists(lpf.CONTROLE) and os.listdir(lpf.STAGING) == []
    assert amb.fila() == [] and not os.path.exists(lpf.BACKUPS)


# ---------------------------------------------------------------- F2-10
def test_job_real_sem_historico_de_simulacao_e_sidecar_com_file_id(amb, capsys):
    reg = lote(amb, "LOTE-SIG-J", semente=63)
    amb.gravar()

    rodar(["--enviar", "--todos"], capsys)

    job = amb.fila()[0]
    assert "SIMULA" not in json.dumps(job["historico"])
    d = os.path.join(lpf.STAGING, job["upload_id"])
    metas = [json.load(open(os.path.join(d, n))) for n in os.listdir(d) if n.endswith(".meta.json")]
    assert sorted(m["id"] for m in metas) == sorted(x["file_id"] for x in reg["arquivos"])


# ---------------------------------------------------------------- F2-08
def test_documento_que_chega_depois_do_envio_e_listado_sem_reenviar_o_kit(amb, capsys):
    lid = "LOTE-SIG-POS"
    reg = lote(amb, lid, semente=64)
    amb.gravar()
    rodar(["--enviar", "--todos"], capsys)
    reg["arquivos"].append(arquivo(amb, lid, 5))
    reg["file_ids"].append(reg["arquivos"][-1]["file_id"])
    amb.gravar()

    rodar(["--enviar", "--todos"], capsys)
    rodar(["--enviar", "--todos"], capsys)

    assert len(amb.fila()) == 1
    pos = amb.controle()[lid]["docs_posteriores"]
    assert [p["file_id"] for p in pos] == [reg["arquivos"][-1]["file_id"]]


# ---------------------------------------------------------------- OP-O2
def test_backup_da_fila_0600_sem_duplicar_e_com_retencao(amb, monkeypatch):
    b1 = lpf.backup_fila()
    b2 = lpf.backup_fila()
    assert b1 == b2
    assert stat.S_IMODE(os.stat(b1).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(lpf.BACKUPS).st_mode) == 0o700

    agora = dt.datetime(2026, 11, 9, 12, 0, 0)
    for h in range(30 * 24 // 4):                       # 1 backup a cada 4 h por 30 dias
        t = agora - dt.timedelta(hours=4 * h)
        open(os.path.join(lpf.BACKUPS, f"fila-jobs-{t:%Y%m%d-%H%M%S}.json"), "w").close()
    open(os.path.join(lpf.BACKUPS, "outro-arquivo.json"), "w").close()

    apagaria = lpf.podar_backups_fila(simular=True, agora_ts=agora.timestamp())
    assert len(os.listdir(lpf.BACKUPS)) > 180            # simular não apaga
    lpf.podar_backups_fila(agora_ts=agora.timestamp())
    sobra = [n for n in os.listdir(lpf.BACKUPS) if n.startswith("fila-jobs-")]
    assert "outro-arquivo.json" in os.listdir(lpf.BACKUPS)
    assert 48 <= len(sobra) <= 48 + 15 and len(apagaria) == 181 - len(sobra)


# ---------------------------------------------------------------- S02 / S04
ARQS_DA_FRENTE = ["lote_para_fila.py", "ponte_criterio.py", "monitor_lotes.py", "relatorio.py",
                  "avisar-primeiro.py", "testes/test_ponte_fila.py", "testes/test_ponte_criterio.py",
                  "testes/test_monitor_lotes.py", "testes/test_relatorio.py",
                  "testes/test_limpeza_servico.py", "testes/test_ponte_rodada.py"]


@pytest.mark.parametrize("rel", ARQS_DA_FRENTE)
def test_sem_cpf_real_nem_credencial_de_cliente_no_codigo(rel):
    texto = open(os.path.join(RAIZ, rel), encoding="utf-8").read()
    # contagens (int) — a mensagem de falha NUNCA mostra o valor encontrado
    n_cpfs = sum(1 for m in re.finditer(r"(?<![\d.])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![\d])", texto)
                 if cpf_util.valido(m.group()))
    n_emails = sum(1 for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", texto)
                   if not e.endswith("@example.com"))
    ok = n_cpfs == 0 and n_emails == 0
    assert ok, f"{rel}: {n_cpfs} CPF(s) com DV válido e {n_emails} e-mail(s) reais — use dados sintéticos"
