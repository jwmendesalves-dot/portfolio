"""Testes do monitor de lotes de documentos (TDD — escritos antes do código).

Pergunta que o monitor responde: "tem lote PRONTO, com documento, que não chegou ao
painel e já passou do tempo aceitável?" — hoje isso acontece em silêncio (41 lotes).
"""
import os
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

from monitor_lotes import lotes_travados  # noqa: E402

AGORA = 1_800_000_000.0


def lote(lid, prontos_min_atras, arquivos=5, pronto=True, status="FECHADO_POR_JANELA",
         nome="Fulano de Tal", cpf="11122233344", fechado_por="janela de tempo",
         cpfs_distintos=None, requer_revisao=False, identificacao="OK"):
    quando = __import__("datetime").datetime.fromtimestamp(
        AGORA - prontos_min_atras * 60, tz=__import__("datetime").timezone.utc
    ).isoformat()
    return lid, {"lote_id": lid, "pronto": pronto, "status": status, "inicio": quando, "fim": quando,
                 "nome": nome, "cpf": cpf, "cpfs_distintos": cpfs_distintos or [cpf],
                 "fechado_por": fechado_por, "requer_revisao": requer_revisao,
                 "identificacao": identificacao, "modo": "janela",
                 "arquivos": [f"a{i}.jpg" for i in range(arquivos)]}


def _meta(h, baixado=True):
    return {"file_id": h[:4], "nome_salvo": f"{h[:6]}.jpg", "baixado": baixado,
            "sha256": h if baixado else None, "tamanho_bits": 10 if baixado else 0}


def test_marca_lote_cujo_cpf_ja_foi_enviado_pela_ponte():
    lid, v = lote("LOTE-R", 90, cpf="11122233344")
    v["arquivos"] = [_meta("af08e94d2a8f0000")]          # kit 100% baixado e igual
    estado = {"lotes": {lid: v}}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30,
                              hashes_por_cpf={"11122233344": {"af08e94d2a8f"}})

    assert travados[0]["cpf_repetido"] is True


def test_kit_com_download_pendente_nao_e_marcado_repetido():
    """F2-03: com documento pendente a duplicata é indeterminada (antes: 'repetido')."""
    lid, v = lote("LOTE-P", 90, cpf="11122233344")
    v["arquivos"] = [_meta("af08e94d2a8f0000"), _meta("bbbbbbbbbbbb1111", baixado=False)]
    estado = {"lotes": {lid: v}}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30,
                              hashes_por_cpf={"11122233344": {"af08e94d2a8f"}})

    assert travados[0]["cpf_repetido"] is False
    assert travados[0]["categoria"] == "aguardando_download"


def test_lote_do_mesmo_cpf_com_arquivo_novo_nao_e_repetido():
    lid, v = lote("LOTE-N", 90, cpf="11122233344")
    v["arquivos"] = [{"sha256": "novoHash1234567890ab"}]
    estado = {"lotes": {lid: v}}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30,
                              hashes_por_cpf={"11122233344": {"af08e94d2a8f"}})

    assert travados[0]["cpf_repetido"] is False


def test_lote_de_cpf_novo_nao_vem_marcado_como_repetido():
    estado = {"lotes": dict([lote("LOTE-S", 90, cpf="11122233344")])}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30,
                              hashes_por_cpf={"99988877766": {"af08e94d2a8f"}})

    assert travados[0]["cpf_repetido"] is False


def test_item_diz_se_pode_ser_encaminhado():
    estado = {"lotes": dict([lote("LOTE-A", 90)])}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30)

    assert travados[0]["aceitavel"] is True
    assert travados[0]["motivo"] == ""


def test_item_de_lote_misturado_vem_marcado_para_revisao():
    estado = {"lotes": dict([lote("LOTE-B", 90,
                                  fechado_por="cabeçalho de outro cliente (CPF diferente)")])}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30)

    assert travados[0]["aceitavel"] is False
    assert "outro cliente" in travados[0]["motivo"]


def test_item_de_lote_com_dois_cpfs_nao_e_aceitavel():
    estado = {"lotes": dict([lote("LOTE-C", 90, cpfs_distintos=["11122233344", "55566677788"])])}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30)

    assert travados[0]["aceitavel"] is False


def test_acusa_lote_pronto_com_documento_parado_alem_do_limite():
    estado = {"lotes": dict([lote("LOTE-A", 90)])}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30)

    assert [x["lote"] for x in travados] == ["LOTE-A"]
    assert travados[0]["arquivos"] == 5
    assert travados[0]["minutos"] == 90


def test_ignora_lote_ainda_dentro_do_tempo_aceitavel():
    estado = {"lotes": dict([lote("LOTE-B", 5)])}

    assert lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30) == []


def test_ignora_lote_que_ja_foi_enviado():
    estado = {"lotes": dict([lote("LOTE-C", 200)])}

    assert lotes_travados(estado, enviados={"LOTE-C"}, agora_ts=AGORA, minimo_min=30) == []


def test_ignora_lote_sem_documento():
    estado = {"lotes": dict([lote("LOTE-D", 200, arquivos=0)])}

    assert lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30) == []


def test_lote_com_kit_mas_sem_marca_de_pronto_vem_como_preso():
    """Furo da auditoria (14/10/2026): lote com documento e nome OK mas `pronto` falso
    é justamente o que fica parado em silêncio — o monitor tem que MOSTRAR, não ignorar."""
    estado = {"lotes": dict([lote("LOTE-E", 200, pronto=False)])}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30)

    assert [x["lote"] for x in travados] == ["LOTE-E"]
    assert travados[0]["tipo"] == "preso_completo"
    assert travados[0]["aceitavel"] is False


def test_ignora_lote_sem_pronto_e_sem_kit():
    estado = {"lotes": dict([lote("LOTE-F", 200, pronto=False, arquivos=0, identificacao="")])}

    assert lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30) == []


def test_ordena_do_mais_antigo_para_o_mais_novo():
    estado = {"lotes": dict([lote("LOTE-NOVO", 40), lote("LOTE-VELHO", 600)])}

    travados = lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30)

    assert [x["lote"] for x in travados] == ["LOTE-VELHO", "LOTE-NOVO"]


def test_enviados_da_ponte_le_os_arquivos_reais_sem_explodir():
    from monitor_lotes import enviados_da_ponte

    enviados, hashes = enviados_da_ponte()

    assert isinstance(enviados, set)
    assert isinstance(hashes, dict)
    assert all(isinstance(v, set) for v in hashes.values())


def test_cli_roda_sem_estourar_e_grava_so_no_tmp(tmp_path):
    """Pega erro que só aparece no main() e (OP-T4) garante que o teste NÃO grava no
    diretório do código/produção: estado, saída e memória vêm do ambiente (tmp)."""
    import json
    import subprocess

    import cpf_util

    cpf = cpf_util.gerar_para_teste(77)
    _lid, v = lote("LOTE-CLI", 600, cpf=cpf)
    dt = __import__("datetime")
    v["inicio"] = v["fim"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=10)).isoformat()
    (tmp_path / "estado.json").write_text(json.dumps({"lotes": {"LOTE-CLI": v}}))
    env = dict(os.environ, TRIAGEM_BASE=str(tmp_path), MONITOR_SAIDA=str(tmp_path / "saida.json"),
               MONITOR_AVISOS=str(tmp_path / "avisos.json"))
    env.pop("ESTADO_JSON", None)
    antes = set(os.listdir(RAIZ))
    cmd = [sys.executable, os.path.join(RAIZ, "monitor_lotes.py"), "--minutos", "30"]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)

    assert "Traceback" not in r.stderr and r.returncode == 1
    assert (tmp_path / "saida.json").exists() and (tmp_path / "avisos.json").exists()
    assert set(os.listdir(RAIZ)) == antes
    assert cpf not in r.stdout and cpf_util.formatar(cpf) not in r.stdout          # S04
    assert "Fulano" not in r.stdout
    assert r2.returncode == 0 and r2.stdout == ""          # F2-07: nada mudou → silêncio


# ---------------------------------------------------------------- F2-07
def test_lote_sem_arquivo_em_revisao_nao_entra_no_aviso():
    estado = {"lotes": dict([lote("LOTE-V", 200, arquivos=0, pronto=False, requer_revisao=True,
                                  identificacao="SEM_ARQUIVOS")])}

    assert lotes_travados(estado, enviados=set(), agora_ts=AGORA, minimo_min=30) == []


def test_avisa_so_quando_a_categoria_muda_e_avisa_quando_chega_a_fila():
    from monitor_lotes import avisos_por_mudanca

    itens = [{"lote": "L1", "categoria": "aguardando_download"},
             {"lote": "L2", "categoria": "revisao"},
             {"lote": "L3", "categoria": "descartado"}]

    p1, m1 = avisos_por_mudanca(itens, {}, set(), "t1")
    p2, m2 = avisos_por_mudanca(itens, m1, set(), "t2")
    itens[0] = {"lote": "L1", "categoria": "pronto"}
    p3, m3 = avisos_por_mudanca(itens, m2, set(), "t3")
    p4, m4 = avisos_por_mudanca(itens[1:], m3, {"L1"}, "t4")
    p5, _m5 = avisos_por_mudanca(itens[1:], m4, {"L1"}, "t5")

    assert [x["lote"] for x in p1] == ["L1", "L2"]          # descartado é silencioso
    assert p2 == []
    assert [(x["lote"], x["categoria"]) for x in p3] == [("L1", "pronto")]
    assert [(x["lote"], x["categoria"]) for x in p4] == [("L1", "enfileirado")]
    assert p5 == []


def test_pronto_fora_da_janela_e_categoria_propria():
    import hashlib

    import cpf_util
    from monitor_lotes import categoria_aviso

    cpf = cpf_util.gerar_para_teste(78)
    dados = b"conteudo"
    v = {"lote_id": "LOTE-SIG-X", "modo": "marcadores", "status": "FECHADO_PRONTO",
         "sinal_abertura": {"m": 1}, "sinal_fechamento": {"m": 2}, "nome": "Maria Aparecida da Silva",
         "cpf": cpf, "cpfs_distintos": [cpf], "identificacao": "OK", "pronto": True,
         "requer_revisao": False, "inicio": "2026-10-01T10:00:00+00:00",
         "arquivos": [{"file_id": "1", "nome_salvo": "01.jpg", "baixado": True,
                       "sha256": hashlib.sha256(dados).hexdigest(), "tamanho_bits": 8}],
         "file_ids": ["1"]}
    agora = __import__("datetime").datetime(2026, 11, 9, tzinfo=__import__("datetime").timezone.utc)

    assert categoria_aviso(v, agora.timestamp(), dias_janela=7) == "pronto_fora_janela"
    assert categoria_aviso(v, agora.timestamp(), dias_janela=60) == "pronto"
    v["arquivos"][0]["baixado"] = False
    assert categoria_aviso(v, agora.timestamp(), dias_janela=60) == "aguardando_download"


def test_controle_gera_falha_para_enfileirando_e_revisao_para_doc_posterior():
    from monitor_lotes import itens_do_controle

    ctrl = {"A": {"estado": "enfileirando", "em": "2026-01-01T00:00:00Z"},
            "B": {"estado": "enfileirado", "docs_posteriores": [{"file_id": "9"}]},
            "C": {"job_id": "grupo-C"}}

    itens = {x["lote"]: x["categoria"] for x in itens_do_controle(ctrl, AGORA, 30)}

    assert itens == {"A": "falha", "B": "revisao"}


def test_aguenta_estado_vazio_ou_estranho():
    assert lotes_travados({}, enviados=set(), agora_ts=AGORA, minimo_min=30) == []
    assert lotes_travados({"lotes": None}, enviados=set(), agora_ts=AGORA, minimo_min=30) == []
    assert lotes_travados(None, enviados=set(), agora_ts=AGORA, minimo_min=30) == []


# ------------------------------------------------- Z: falhas permanentes de download
def _fila_download(caminho, itens):
    import json
    caminho.write_text(json.dumps({"itens": itens, "fontes": {}}))


def test_fila_download_vira_categorias_proprias_e_mascara_cpf(tmp_path):
    import cpf_util
    from monitor_lotes import itens_da_fila_download

    cpf = cpf_util.gerar_para_teste(91)
    arq = tmp_path / "download_fila.json"
    _fila_download(arq, [
        {"file_id": "10", "estado": "falha_permanente", "lote_id": "LOTE-A",
         "erro": f"404 NOT_FOUND cpf {cpf}"},
        {"file_id": "11", "estado": "telegram_legado", "lote_id": "LOTE-B", "erro": "id CQA"},
        {"file_id": "12", "estado": "pendente", "lote_id": "LOTE-A"},
        {"file_id": "13", "estado": "baixado", "lote_id": "LOTE-A"},
        {"file_id": "10", "estado": "falha_permanente", "lote_id": "LOTE-A"},   # duplicado
    ])

    itens = itens_da_fila_download(str(arq))

    assert [(x["lote"], x["categoria"], x["lote_origem"]) for x in itens] == [
        ("download:10", "download_falha_permanente", "LOTE-A"),
        ("download:11", "download_telegram_legado", "LOTE-B")]
    assert cpf not in itens[0]["motivo"]


def test_fila_download_ausente_corrompida_ou_legada(tmp_path):
    import json
    from monitor_lotes import itens_da_fila_download

    assert itens_da_fila_download(str(tmp_path / "nao-existe.json")) == []
    ruim = tmp_path / "ruim.json"
    ruim.write_text("{corrompido")
    assert itens_da_fila_download(str(ruim)) == []
    legado = tmp_path / "legado.json"                       # formato antigo: lista pura
    legado.write_text(json.dumps([{"file_id": "7", "estado": "falha_permanente"}, "lixo"]))
    assert [x["lote"] for x in itens_da_fila_download(str(legado))] == ["download:7"]


def test_cli_avisa_falha_de_download_so_na_mudanca(tmp_path):
    import json
    import subprocess

    (tmp_path / "estado.json").write_text(json.dumps({"lotes": {}}))
    fila = tmp_path / "download_fila.json"
    _fila_download(fila, [{"file_id": "55", "estado": "falha_permanente", "lote_id": "LOTE-D",
                           "erro": "ACESSO negado"}])
    env = dict(os.environ, TRIAGEM_BASE=str(tmp_path), MONITOR_SAIDA=str(tmp_path / "s.json"),
               MONITOR_AVISOS=str(tmp_path / "a.json"), DOWNLOAD_FILA_JSON=str(fila))
    env.pop("ESTADO_JSON", None)
    cmd = [sys.executable, os.path.join(RAIZ, "monitor_lotes.py")]

    r1 = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    _fila_download(fila, [{"file_id": "55", "estado": "falha_permanente", "lote_id": "LOTE-D"},
                          {"file_id": "56", "estado": "telegram_legado", "lote_id": "LOTE-D"}])
    r3 = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)

    assert r1.returncode == 1 and "falha PERMANENTE de download" in r1.stdout
    assert "download:55" in r1.stdout and "LOTE-D" in r1.stdout
    assert r2.returncode == 0 and r2.stdout == ""                 # nada mudou → silêncio
    assert r3.returncode == 1 and "download:56" in r3.stdout and "download:55" not in r3.stdout
