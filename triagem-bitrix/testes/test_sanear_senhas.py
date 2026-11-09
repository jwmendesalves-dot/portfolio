# -*- coding: utf-8 -*-
"""sanear_senhas (S01/S05) — inventário sem vazamento, saneamento idempotente.

Tudo sintético: senhas fictícias nos formatos reais (testes/test_job_senha_s01.py e
testes/test_seguranca.py), CPFs de `cpf_util.gerar_para_teste` (via
fixtures/sinteticos.py) e nomes fictícios. Nenhum teste lê produção nem rede.

Requisitos cobertos:
  * inventário conta e NUNCA vaza senha/CPF/nome (stdout e --json);
  * aplicar SEM --confirmar não grava;
  * aplicar saneia o estado preservando os demais campos;
  * 2ª execução é no-op e reporta 0;
  * JSON inválido -> código 2 sem afetar os outros alvos;
  * backups exigem --ciente-evidencia (e preservam nome/mtime);
  * arquivos reescritos ficam 0600;
  * fila do painel só com --incluir-fila-painel.
"""
import json
import os
import stat
import time

import pytest

import caminhos
import estado_store
from fixtures import sinteticos as F
from manutencao import sanear_senhas as SS

SENHA = F.SENHA_FICTICIA                 # "Fict@Senha2024"
SENHA2 = F.SENHA_FICHA_DIVERGENTE        # "Outra#Fict99"
SENHA3 = "Fict!2024"                     # formato "Acesso ao gov.br (CPF e senha)"
CPF = F.CPF_CLIENTE
CPF_FMT = F.CPF_CLIENTE_FMT
NOME = F.NOME_CLIENTE
NIS = "12345678901"
SEGREDOS = (SENHA, SENHA2, SENHA3, CPF, CPF_FMT, NOME)

FORMATOS = [
    "• **NIS/Senha do INSS:** %s / %s" % (NIS, SENHA2),          # fichinha do painel
    "Nome completo: %s\nCPF: %s\nSenha: %s\nCidade: Manaus\n" % (NOME, CPF_FMT, SENHA),
    "• Acesso ao gov.br (CPF e senha): %s" % SENHA3,
]


def _msg(mid, texto):
    return {"id": mid, "autor": "Atendente Ficticia", "data": "2099-12-31",
            "sinal": False, "texto": texto, "realocada": False}


def _estado_com_senha():
    """Estado com a estrutura real de `triagem_grupo.serializar_lote`: 2 lotes,
    3 mensagens com senha (nos 3 formatos reais) e 2 sem."""
    lote_a = F.lote_molde(lote_id="LOTE-A", nome=NOME, cpf=CPF,
                          mensagens=[_msg(1, FORMATOS[0]), _msg(2, FORMATOS[1]),
                                     _msg(3, "ok, mensagem sem segredo")])
    lote_b = F.lote_molde(lote_id="LOTE-B",
                          mensagens=[_msg(4, FORMATOS[2]), _msg(5, "prosa comum do grupo")])
    return {"versao": 1, "cursor_eventos": 0, "cursor_msg_id": 0,
            "atualizado_em": None, "ultima_verificacao": None,
            "lotes": {"LOTE-A": lote_a, "LOTE-B": lote_b},
            "arquivos_index": {}, "fonte": None, "reacoes_ignoradas": 0}


def _fila_com_senha():
    """fila-jobs.json do painel: lista de jobs com a fichinha (formato real)."""
    return [
        {"id": "grupo-LOTE-A", "upload_id": "u-1",
         "fichinha": "**NIS/Senha do INSS:** %s / %s\n• CPF: %s" % (NIS, SENHA2, CPF_FMT),
         "cliente": {"nome": NOME, "cpf": CPF_FMT},
         "historico": [{"em": "2099-12-31T10:00:00", "evento": "enfileirado"}]},
        {"id": "grupo-LOTE-B", "upload_id": "u-2", "fichinha": "S/SENHA",
         "cliente": {"nome": "OUTRO FICTICIO", "cpf": ""}, "historico": []},
    ]


def _grava(caminho, dados, modo=0o644):
    caminho = str(caminho)
    with open(caminho, "w", encoding="utf-8") as fh:
        json.dump(dados, fh, ensure_ascii=False)
    os.chmod(caminho, modo)
    return caminho


@pytest.fixture
def alvos(tmp_path):
    """estado.json + backups (estado e fila) + fila do painel + extra (/tmp/_entradas)."""
    est = tmp_path / "estado.json"
    estado_store.gravar_atomico(_estado_com_senha(), str(est))
    bkdir = tmp_path / "backups-estado"
    bkdir.mkdir()
    bak = _grava(bkdir / "estado.json.bak-20991231-000000", _estado_com_senha())
    bak_fila = _grava(bkdir / "fila-jobs-20991231-000000.json", _fila_com_senha())
    fila = _grava(tmp_path / "fila-jobs.json", _fila_com_senha())
    extra = _grava(tmp_path / "entradas.json", _fila_com_senha())
    return {"est": str(est), "bkdir": str(bkdir), "bak": bak, "bak_fila": bak_fila,
            "fila": fila, "extra": extra}


def _args(a, *extras):
    return ["--estado", a["est"], "--backups-dir", a["bkdir"],
            "--fila-jobs", a["fila"], "--extra", a["extra"], *extras]


def _linhas(saida):
    """{arquivo: {campo: valor}} da saída texto (uma linha por arquivo)."""
    regs = {}
    for ln in saida.splitlines():
        if not ln or ln.startswith("TOTAL "):
            continue
        partes = ln.split("  ")
        campos = {}
        for p in partes[1:]:
            k, _, v = p.partition("=")
            campos[k] = v
        regs[partes[0]] = campos
    return regs


def _monitores(a):
    """(bytes, mtime_ns, modo octal) de cada arquivo de dados."""
    saida = {}
    for k, cam in a.items():
        if k == "bkdir":
            continue
        st = os.stat(cam)
        with open(cam, "rb") as fh:
            dados = fh.read()
        saida[k] = (dados, st.st_mtime_ns, "%04o" % stat.S_IMODE(st.st_mode))
    return saida


def _sem_segredos(*textos):
    juntos = "".join(textos)
    for s in SEGREDOS:
        assert s not in juntos, "VAZAMENTO: %r apareceu na saída" % (s[:4] + "…",)


# ============================================================ inventário
def test_inventario_conta_e_nao_vaza(alvos, capsys):
    antes = _monitores(alvos)
    codigo = SS.main(["--inventario"] + _args(alvos))
    capt = capsys.readouterr()
    assert codigo == 1, "senha encontrada na varredura = código 1"
    _sem_segredos(capt.out, capt.err)
    regs = _linhas(capt.out)
    assert set(regs) == {alvos["est"], alvos["bak"], alvos["bak_fila"],
                         alvos["fila"], alvos["extra"]}
    # estado: 2 lotes, os 2 com senha, 3 mensagens com senha
    est = regs[alvos["est"]]
    assert est["papel"] == "estado"
    assert (est["registros"], est["registros_com_senha"], est["itens_com_senha"]) \
        == ("2", "2", "3")
    # fila/extra/fila-backup: 2 jobs, 1 com senha, 1 campo de texto afetado
    for cam in (alvos["fila"], alvos["extra"], alvos["bak_fila"]):
        assert (regs[cam]["registros"], regs[cam]["registros_com_senha"],
                regs[cam]["itens_com_senha"]) == ("2", "1", "1")
    # backup do estado: mesma contagem do estado (caminho genérico: campos afetados)
    assert (regs[alvos["bak"]]["registros"], regs[alvos["bak"]]["registros_com_senha"],
            regs[alvos["bak"]]["itens_com_senha"]) == ("2", "2", "3")
    # tamanho, permissão (octal) e dono em TODOS os arquivos
    for cam, r in regs.items():
        assert int(r["tamanho_bytes"]) > 0, cam
        assert r["permissao"] == ("0600" if cam == alvos["est"] else "0644"), cam
    assert "dono=" in capt.out and "permissao=0644" in capt.out
    assert "modo=inventario" in capt.out and "codigo=1" in capt.out
    # somente leitura: nenhum byte, mtime ou modo mudou
    assert _monitores(alvos) == antes


def test_inventario_json_nao_vaza(alvos, capsys):
    codigo = SS.main(["--inventario", "--json"] + _args(alvos))
    capt = capsys.readouterr()
    assert codigo == 1
    _sem_segredos(capt.out, capt.err)
    doc = json.loads(capt.out)                       # saída é JSON válido
    assert doc["modo"] == "inventario"
    assert doc["resumo"]["codigo"] == 1
    assert doc["resumo"]["com_senha"] == 5           # os 5 arquivos têm senha
    assert doc["resumo"]["erros"] == 0
    por_arq = {r["arquivo"]: r for r in doc["arquivos"]}
    assert por_arq[alvos["est"]]["itens_com_senha"] == 3
    for r in doc["arquivos"]:
        assert r["permissao"] and r["dono"] is not None and r["tamanho_bytes"] > 0


def test_inventario_sem_senha_reporta_0(tmp_path, capsys):
    limpo = _grava(tmp_path / "fila-limpa.json",
                   [{"id": "x", "fichinha": "S/SENHA\n• CPF: %s" % CPF_FMT}])
    (tmp_path / "vazio").mkdir()
    codigo = SS.main(["--inventario", "--estado", limpo, "--fila-jobs", limpo,
                      "--backups-dir", str(tmp_path / "vazio"), "--extra", limpo])
    assert codigo == 0
    assert "codigo=0" in capsys.readouterr().out


# ============================================================ aplicar
def test_aplicar_sem_confirmar_nao_grava(alvos, capsys):
    antes = _monitores(alvos)
    codigo = SS.main(["--aplicar"] + _args(alvos))
    capt = capsys.readouterr()
    assert codigo == 1, "simulação encontrou senha e não gravou"
    _sem_segredos(capt.out, capt.err)
    assert "modo=simulacao" in capt.out
    assert _monitores(alvos) == antes, "NADA pode ser gravado sem --confirmar"


def test_aplicar_saneia_estado_e_preserva_os_demais_campos(alvos, capsys):
    with open(alvos["est"], encoding="utf-8") as fh:
        antes = json.load(fh)
    codigo = SS.main(["--aplicar", "--confirmar", "--estado", alvos["est"]])
    assert codigo == 0
    assert "acao=saneado" in capsys.readouterr().out

    with open(alvos["est"], encoding="utf-8") as fh:
        depois = json.load(fh)
    # saneado: senha fora, marcador no lugar; resto do texto preservado
    # (remover_senhas NÃO mexe em nome/CPF — eles são dados da triagem, S01)
    for lote in depois["lotes"].values():
        for m in lote["mensagens"]:
            for s in (SENHA, SENHA2, SENHA3):
                assert s not in m["texto"]
    assert CPF_FMT in depois["lotes"]["LOTE-A"]["mensagens"][1]["texto"]
    assert NOME in depois["lotes"]["LOTE-A"]["mensagens"][1]["texto"]
    assert depois["lotes"]["LOTE-A"]["mensagens"][0]["texto"] == FORMATOS[0].replace(
        SENHA2, SS.MARCADOR)
    assert depois["lotes"]["LOTE-A"]["mensagens"][1]["texto"] == FORMATOS[1].replace(
        SENHA, SS.MARCADOR)
    assert depois["lotes"]["LOTE-B"]["mensagens"][0]["texto"] == FORMATOS[2].replace(
        SENHA3, SS.MARCADOR)
    # preservado: todos os demais campos do lote e das mensagens
    for lid, lote in antes["lotes"].items():
        for campo, valor in lote.items():
            if campo == "mensagens":
                continue
            assert depois["lotes"][lid][campo] == valor, campo
        for m_antes, m_depois in zip(lote["mensagens"], depois["lotes"][lid]["mensagens"]):
            for campo, valor in m_antes.items():
                if campo == "texto":
                    continue
                assert m_depois[campo] == valor, campo
    # fora do escopo de mensagens, o estado não foi tocado
    for campo, valor in antes.items():
        if campo != "lotes":
            assert depois[campo] == valor


def test_aplicar_nao_toca_backups_nem_fila_do_painel_sem_flags(alvos, capsys):
    antes = _monitores(alvos)
    codigo = SS.main(["--aplicar", "--confirmar"] + _args(alvos))
    capt = capsys.readouterr()
    assert codigo == 0
    depois = _monitores(alvos)
    assert depois["est"][0] != antes["est"][0]          # estado saneado
    assert depois["extra"][0] != antes["extra"][0]      # extra saneado
    for k in ("bak", "bak_fila", "fila"):
        assert depois[k] == antes[k], "só --sanear-backups/--incluir-fila-painel libera"
    regs = _linhas(capt.out)
    assert regs[alvos["bak"]]["acao"] == "pulado"
    assert "ciente-evidencia" in regs[alvos["bak"]]["motivo"]
    assert regs[alvos["fila"]]["acao"] == "pulado"
    assert "incluir-fila-painel" in regs[alvos["fila"]]["motivo"]


def test_segunda_execucao_e_no_op(alvos, capsys):
    args = ["--aplicar", "--confirmar", "--sanear-backups", "--ciente-evidencia",
            "--incluir-fila-painel"]
    assert SS.main(_args(alvos, *args)) == 0
    capsys.readouterr()
    depois_da_primeira = _monitores(alvos)
    codigo = SS.main(_args(alvos, *args))
    capt = capsys.readouterr()
    assert codigo == 0
    assert "alteracoes=0" in capt.out
    assert "itens_com_senha=0" in capt.out
    for r in _linhas(capt.out).values():
        assert r.get("acao") in ("sem_alteracao",)
    assert _monitores(alvos) == depois_da_primeira, "2ª execução não pode mudar nada"


def test_json_invalido_codigo_2_sem_afetar_os_outros(alvos, tmp_path, capsys):
    ruim = tmp_path / "quebrado.json"
    ruim.write_text("{ isto não é json válido", encoding="utf-8")
    os.chmod(str(ruim), 0o644)
    codigo = SS.main(_args(alvos, "--extra", str(ruim), "--aplicar", "--confirmar",
                           "--sanear-backups", "--ciente-evidencia",
                           "--incluir-fila-painel"))
    capt = capsys.readouterr()
    assert codigo == 2, "erro de leitura vira código 2 no fim"
    assert "json_invalido" in capt.err and "AVISO" in capt.err
    with open(ruim, "rb") as fh:
        assert fh.read() == "{ isto não é json válido".encode("utf-8")
    # os OUTROS alvos foram saneados normalmente
    for cam in (alvos["est"], alvos["bak"], alvos["bak_fila"], alvos["fila"],
                alvos["extra"]):
        with open(cam, encoding="utf-8") as fh:
            assert SENHA2 not in fh.read()
    assert "alteracoes=5" in capt.out and "erros=1" in capt.out


def test_json_invalido_tambem_no_inventario(alvos, tmp_path, capsys):
    ruim = tmp_path / "quebrado.json"
    ruim.write_text("não é json", encoding="utf-8")
    codigo = SS.main(["--inventario"] + _args(alvos, "--extra", str(ruim)))
    capt = capsys.readouterr()
    assert codigo == 2
    _sem_segredos(capt.out, capt.err)
    assert _linhas(capt.out)[str(ruim)]["acao"] == "pulado"


def test_arquivo_inexistente_explicito_e_erro(tmp_path, capsys):
    codigo = SS.main(["--inventario", "--extra", str(tmp_path / "nao-existe.json")])
    assert codigo == 2
    assert "leitura:FileNotFoundError" in capsys.readouterr().err


# ============================================================ backups / evidência
def test_backups_exigem_ciente_evidencia(alvos, capsys):
    antes = _monitores(alvos)
    codigo = SS.main(_args(alvos, "--aplicar", "--confirmar", "--sanear-backups"))
    capt = capsys.readouterr()
    assert codigo == 2, "recusa de segurança"
    assert "EVIDÊNCIA" in capt.err and "ANPD" in capt.err
    assert "podar_backups" in capt.err, "a poda é OUTRA ferramenta e deve aparecer no aviso"
    assert "RECUSADO" in capt.err
    assert _monitores(alvos) == antes, "recusa total: nada é gravado"

    # o aviso também aparece no inventário (somente leitura), sem exigir a flag
    assert SS.main(["--inventario", "--sanear-backups"] + _args(alvos)) == 1
    assert "EVIDÊNCIA" in capsys.readouterr().err


def test_saneia_backups_mantem_nome_e_mtime_e_0600(alvos, capsys):
    antiguidade = time.time() - 7 * 86400
    for cam in (alvos["bak"], alvos["bak_fila"]):
        os.utime(cam, (antiguidade, antiguidade))
    mtime_antes = {cam: os.stat(cam).st_mtime_ns for cam in (alvos["bak"], alvos["bak_fila"])}
    codigo = SS.main(_args(alvos, "--aplicar", "--confirmar", "--sanear-backups",
                           "--ciente-evidencia"))
    assert codigo == 0
    capt = capsys.readouterr()
    _sem_segredos(capt.out, capt.err)
    for cam in (alvos["bak"], alvos["bak_fila"]):
        assert os.path.exists(cam), "nome preservado (nada é apagado/renomeado)"
        st = os.stat(cam)
        assert st.st_mtime_ns == mtime_antes[cam], "mtime preservado"
        assert "%04o" % stat.S_IMODE(st.st_mode) == "0600"
        with open(cam, encoding="utf-8") as fh:
            assert SENHA2 not in fh.read()


def test_arquivos_reescritos_ficam_0600(alvos, capsys):
    codigo = SS.main(_args(alvos, "--aplicar", "--confirmar", "--sanear-backups",
                           "--ciente-evidencia", "--incluir-fila-painel"))
    assert codigo == 0
    capsys.readouterr()
    for cam in (alvos["est"], alvos["bak"], alvos["bak_fila"], alvos["fila"],
                alvos["extra"]):
        assert "%04o" % stat.S_IMODE(os.stat(cam).st_mode) == "0600", cam


# ============================================================ fila do painel
def test_fila_do_painel_soh_com_flag_e_com_trava(alvos, capsys):
    codigo = SS.main(_args(alvos, "--aplicar", "--confirmar", "--incluir-fila-painel"))
    assert codigo == 0
    capsys.readouterr()
    with open(alvos["fila"], encoding="utf-8") as fh:
        assert SENHA2 not in fh.read()
    assert os.path.exists(alvos["fila"] + ".lock"), "usa a trava do estado_store"


# ============================================================ alvos padrão
def test_alvos_padrao_vem_dos_caminhos(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(caminhos, "BASE", str(tmp_path / "base"))
    monkeypatch.setattr(caminhos, "SEPARADOR", str(tmp_path / "sep"))
    monkeypatch.delenv("ESTADO_JSON", raising=False)
    est = tmp_path / "base" / "estado.json"
    est.parent.mkdir()
    estado_store.gravar_atomico(_estado_com_senha(), str(est))
    (tmp_path / "base" / "backups-estado").mkdir()
    _grava(tmp_path / "base" / "backups-estado" / "estado.json.bak-20991231-000000",
           _estado_com_senha())
    (tmp_path / "sep" / "data").mkdir(parents=True)
    _grava(tmp_path / "sep" / "data" / "fila-jobs.json", _fila_com_senha())
    codigo = SS.main(["--inventario", "--json"])
    capt = capsys.readouterr()
    assert codigo == 1
    _sem_segredos(capt.out, capt.err)
    doc = json.loads(capt.out)
    arqs = {r["arquivo"] for r in doc["arquivos"]}
    assert str(est) in arqs
    assert str(tmp_path / "base" / "backups-estado" / "estado.json.bak-20991231-000000") in arqs
    assert str(tmp_path / "sep" / "data" / "fila-jobs.json") in arqs
