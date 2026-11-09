# -*- coding: utf-8 -*-
"""
TDD — escrita concorrente no estado.json (24/09/2026).

BUG REAL: 6 dos 7 lotes do Telegram do teste das 17:57-18:01 NUNCA chegaram à
ponte e sumiram do `estado.json`. Causa: dois processos fazem leitura-modificação-
gravação do MESMO arquivo sem trava —
  · `triagem_grupo.ciclo()`  (lê no começo, grava no fim; ciclo de 120s por grupo)
  · `telegram_grupo.mesclar_no_estado()` (lê, mescla, grava a cada lote do Telegram)
Quem grava por último vence e o lote do outro some. Os lotes do Bitrix se
recuperam (o ciclo relê o histórico); os do Telegram NÃO (só lê update novo) →
kit perdido de verdade.

`atualizar_estado(mutador)` serializa: relê DENTRO da trava, aplica o mutador e
grava atômico (tmp + replace). Escrita de outro processo nunca é sobrescrita.
"""
import os
from conftest import RAIZ
import json
import sys
import threading
import time

sys.path.insert(0, RAIZ)

import pytest

from estado_store import atualizar_estado, ler_estado


@pytest.fixture
def arq(tmp_path):
    return str(tmp_path / "estado.json")


def test_grava_e_le(arq):
    atualizar_estado(lambda e: e.setdefault("lotes", {}).update({"A": {"n": 1}}), caminho=arq)
    assert ler_estado(arq)["lotes"]["A"] == {"n": 1}


def test_escrita_concorrente_nao_perde_lote(arq):
    """O teste que falhava: dois escritores, cada um com 'seu' lote."""
    atualizar_estado(lambda e: e.setdefault("lotes", {}).update({"base": {}}), caminho=arq)

    def escritor_esteira():
        # simula o ciclo longo: demora DENTRO da leitura-modificação-gravação
        def mut(e):
            e.setdefault("lotes", {})["bitrix"] = {}
            time.sleep(0.4)
        atualizar_estado(mut, caminho=arq)

    def escritor_telegram():
        time.sleep(0.15)          # entra no meio do ciclo da esteira
        atualizar_estado(lambda e: e.setdefault("lotes", {}).update({"telegram": {}}), caminho=arq)

    t1 = threading.Thread(target=escritor_esteira)
    t2 = threading.Thread(target=escritor_telegram)
    t1.start(); t2.start(); t1.join(); t2.join()

    lotes = ler_estado(arq)["lotes"]
    assert set(lotes) == {"base", "bitrix", "telegram"}, f"lote perdido: {sorted(lotes)}"


def test_varias_escritas_em_rajada_preservam_todos(arq):
    def escreve(i):
        atualizar_estado(lambda e: e.setdefault("lotes", {}).update({f"L{i}": {}}), caminho=arq)

    threads = [threading.Thread(target=escreve, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ler_estado(arq)["lotes"]) == 12


def test_mutador_que_levanta_excecao_nao_corrompe(arq):
    atualizar_estado(lambda e: e.setdefault("lotes", {}).update({"fica": {}}), caminho=arq)

    def mut_ruim(e):
        e["lotes"]["novo"] = {}
        raise ValueError("falhou no meio")
        return e

    with pytest.raises(ValueError):
        atualizar_estado(mut_ruim, caminho=arq)

    conteudo = json.load(open(arq, encoding="utf-8"))
    assert set(conteudo["lotes"]) == {"fica"}, "estado parcial foi gravado"


def test_estado_inexistente_comeca_vazio(arq):
    atualizar_estado(lambda e: e.setdefault("lotes", {}).update({"x": {}}), caminho=arq)
    assert ler_estado(arq)["lotes"]["x"] == {}


# ===========================================================================
# Remediação frente B (09/11/2026) — A2/F1-08: corrompido NUNCA vira {}
# ===========================================================================
import glob
import stat

import estado_store
from estado_store import (EstadoCorrompido, PROPRIEDADE_CAMPOS, atualizar_lotes,
                          fazer_backup, gravar_atomico, mesclar_lote, podar_backups)


def _cpf_sintetico(semente: int) -> str:
    """CPF de TESTE (DV calculado), nunca de pessoa real."""
    base = f"{(semente * 7919 + 123456789) % 10 ** 9:09d}"
    d = [int(c) for c in base]
    for n in (9, 10):
        d.append((sum(d[i] * (n + 1 - i) for i in range(n)) * 10) % 11 % 10)
    return "".join(map(str, d))


CPF_T = _cpf_sintetico(42)
CPF_T_FMT = f"{CPF_T[:3]}.{CPF_T[3:6]}.{CPF_T[6:9]}-{CPF_T[9:]}"


def _escrever(caminho, texto):
    with open(caminho, "w", encoding="utf-8") as fh:
        fh.write(texto)


def test_ausente_devolve_vazio(arq):
    assert ler_estado(arq) == {}


@pytest.mark.parametrize("conteudo", ['{"lotes": {"A": ', "", "[1, 2]", "null", "\xff\xfe"])
def test_corrompido_levanta_e_preserva_copia(arq, conteudo):
    """Código antigo: ler_estado devolvia {} (e atualizar_estado gravava por cima)."""
    with open(arq, "wb") as fh:
        fh.write(conteudo.encode("latin-1") if conteudo == "\xff\xfe" else conteudo.encode())
    with pytest.raises(EstadoCorrompido) as exc:
        ler_estado(arq)
    assert exc.value.copia and os.path.exists(exc.value.copia)
    assert ".corrompido-" in exc.value.copia


def test_copia_do_corrompido_e_uma_por_conteudo(arq):
    _escrever(arq, '{"quebrado": ')
    for _ in range(3):
        with pytest.raises(EstadoCorrompido):
            ler_estado(arq)
    assert len(glob.glob(arq + ".corrompido-*")) == 1
    _escrever(arq, '{"outro quebrado": ')
    with pytest.raises(EstadoCorrompido):
        ler_estado(arq)
    assert len(glob.glob(arq + ".corrompido-*")) == 2


def test_atualizar_nao_grava_sobre_estado_corrompido(arq):
    """O defeito A2: estado ilegível -> {} -> ciclo grava só o delta (2.722 lotes perdidos)."""
    original = '{"lotes": {"L1": {"cpf": "1"}, "L2": {}'          # truncado
    _escrever(arq, original)
    chamado = []
    with pytest.raises(EstadoCorrompido):
        atualizar_estado(lambda e: chamado.append(1) or e.update({"lotes": {"novo": {}}}),
                         caminho=arq)
    assert chamado == [], "mutador não deveria nem rodar"
    assert open(arq, encoding="utf-8").read() == original
    with pytest.raises(EstadoCorrompido):
        atualizar_lotes({"novo": {"status": "ABERTO"}}, "triagem", caminho=arq)
    assert open(arq, encoding="utf-8").read() == original


def test_gravar_atomico_0600_e_sem_tmp(arq):
    gravar_atomico({"a": 1}, arq)
    assert stat.S_IMODE(os.stat(arq).st_mode) == 0o600
    assert ler_estado(arq) == {"a": 1}
    assert glob.glob(arq + ".tmp*") == []


def test_gravar_atomico_recusa_nao_dict(arq):
    with pytest.raises(TypeError):
        gravar_atomico([1, 2], arq)
    assert not os.path.exists(arq)


def test_queda_antes_do_replace_preserva_original_e_remove_tmp(arq, monkeypatch):
    gravar_atomico({"lotes": {"fica": {}}}, arq)

    def queda(*a, **k):
        raise OSError("simulação: energia caiu antes do os.replace")

    monkeypatch.setattr(estado_store.os, "replace", queda)
    with pytest.raises(OSError):
        atualizar_estado(lambda e: e["lotes"].update({"novo": {}}), caminho=arq)
    monkeypatch.undo()
    assert set(ler_estado(arq)["lotes"]) == {"fica"}
    assert glob.glob(arq + ".tmp*") == [], "tmp órfão ficou para trás"


def test_tmp_e_exclusivo_por_gravacao(arq, monkeypatch):
    vistos = []
    real = os.replace

    def espia(src, dst):
        vistos.append(src)
        return real(src, dst)

    monkeypatch.setattr(estado_store.os, "replace", espia)
    gravar_atomico({"a": 1}, arq)
    gravar_atomico({"a": 2}, arq)
    assert len(set(vistos)) == 2 and all(f".tmp.{os.getpid()}." in v for v in vistos)


def test_arq_estado_respeita_caminhos_e_env(tmp_path):
    """Em processo separado: recarregar o módulo aqui trocaria a classe EstadoCorrompido
    vista pelos outros testes."""
    import subprocess
    codigo = "import estado_store, caminhos; print(estado_store.ARQ_ESTADO)"
    env = dict(os.environ, TRIAGEM_BASE=str(tmp_path))
    env.pop("ESTADO_JSON", None)
    saida = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=env,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert saida == os.path.join(str(tmp_path), "estado.json")
    env["ESTADO_JSON"] = str(tmp_path / "outro.json")
    saida = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=env,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert saida == str(tmp_path / "outro.json")


def test_sem_mudanca_nao_regrava(arq):
    gravar_atomico({"a": 1}, arq)
    antes = os.stat(arq).st_ino
    atualizar_estado(lambda e: None, caminho=arq)
    assert os.stat(arq).st_ino == antes


# ===========================================================================
# A3/F1-03 — merge por campo com dono
# ===========================================================================
def _reg_triagem(**extra):
    reg = {"lote_id": "L1", "status": "FECHADO_POR_JANELA", "pronto": False,
           "requer_revisao": True, "identificacao": "SEM_CPF", "cpf": None,
           "cpf_formatado": None, "cpfs_distintos": [], "nome": "MARIA TESTE",
           "motivos_revisao": ["SEM_CPF: nenhum CPF identificado no lote"],
           "file_ids": ["10"], "mensagens": [{"id": 1}], "qtd_arquivos": 1,
           "arquivos": [{"file_id": "10", "indice": 1, "baixado": False, "sha256": None,
                         "erro": "adiado (lote antigo / teto do ciclo) — baixar_pendentes preenche"}]}
    reg.update(extra)
    return reg


PATCH_CPF = {"cpf": CPF_T, "cpf_formatado": CPF_T_FMT,
             "cpfs_distintos": [CPF_T], "identificacao": "OK",
             "cpf_origem": "bitrix-nome", "nome_origem": "bitrix-nome", "nome": "MARIA TESTE",
             "pronto": True, "requer_revisao": False,
             "_resolver_motivos": ["SEM_CPF*"]}


def test_triagem_nao_apaga_cpf_da_identificacao():
    base, _ = mesclar_lote({}, _reg_triagem(), "triagem")
    com_cpf, conf = mesclar_lote(base, PATCH_CPF, "identificacao")
    assert conf == []
    assert com_cpf["pronto"] is True and com_cpf["requer_revisao"] is False
    assert com_cpf["motivos_revisao"] == []
    # próximo ciclo da triagem regrava o registro INTEIRO, ainda sem CPF
    depois, conf = mesclar_lote(com_cpf, _reg_triagem(mensagens=[{"id": 1}, {"id": 2}]), "triagem")
    assert depois["cpf"] == CPF_T
    assert depois["identificacao"] == "OK" and depois["cpf_origem"] == "bitrix-nome"
    assert depois["pronto"] is True and depois["requer_revisao"] is False
    assert depois["motivos_revisao"] == [], "motivo resolvido voltou pela triagem"
    assert depois["mensagens"] == [{"id": 1}, {"id": 2}], "a triagem perdeu o que é dela"
    assert "cpf" in conf


def test_estado_legado_cpf_origem_tambem_protege():
    """Registro gravado pelo completar_cpf antigo (sem _donos_campos)."""
    legado = _reg_triagem(cpf=CPF_T_FMT, identificacao="OK", cpf_origem="bitrix-nome",
                          nome_origem="bitrix-nome", pronto=True, requer_revisao=False,
                          motivos_revisao=[])
    depois, _ = mesclar_lote(legado, _reg_triagem(), "triagem")
    assert depois["cpf"] == CPF_T_FMT and depois["identificacao"] == "OK"
    assert depois["pronto"] is True


def test_ponte_status_duplicata_sobrevive_a_triagem():
    base, _ = mesclar_lote({}, _reg_triagem(identificacao="OK", pronto=True,
                                            requer_revisao=False, motivos_revisao=[]), "triagem")
    marcado, conf = mesclar_lote(base, {"status": "descartado_duplicata",
                                        "status_anterior": "FECHADO_POR_JANELA",
                                        "lote_origem": "L0", "descartado_em": "x",
                                        "descarte_motivo": "mesmo kit"}, "ponte")
    assert conf == []
    depois, _ = mesclar_lote(marcado, _reg_triagem(identificacao="OK", pronto=True,
                                                   requer_revisao=False, motivos_revisao=[]),
                             "triagem")
    assert depois["status"] == "descartado_duplicata"
    assert depois["lote_origem"] == "L0" and depois["status_anterior"] == "FECHADO_POR_JANELA"


def test_ponte_nao_escreve_status_arbitrario():
    _, conf = mesclar_lote({"status": "FECHADO_PRONTO"}, {"status": "ABERTO"}, "ponte")
    assert conf == ["status"]


def test_motivo_de_marcar_revisao_so_cresce():
    base, _ = mesclar_lote({}, _reg_triagem(motivos_revisao=[]), "triagem")
    m1, _ = mesclar_lote(base, {"motivos_revisao": ["duplicata: L0"], "requer_revisao": True,
                                "pronto": False}, "ponte")
    # triagem regrava com motivos [] e requer_revisao False -> não apaga
    m2, _ = mesclar_lote(m1, _reg_triagem(motivos_revisao=[], requer_revisao=False), "triagem")
    assert m2["motivos_revisao"] == ["duplicata: L0"] and m2["requer_revisao"] is True
    # identificacao não remove sem resolver explicitamente
    m3, conf = mesclar_lote(m2, {"requer_revisao": False}, "identificacao")
    assert m3["requer_revisao"] is True and "requer_revisao" in conf
    # triagem não pode resolver
    m4, conf = mesclar_lote(m3, {"_resolver_motivos": ["duplicata: L0"]}, "triagem")
    assert m4["motivos_revisao"] == ["duplicata: L0"]
    # resolução explícita
    m5, conf = mesclar_lote(m4, {"_resolver_motivos": ["duplicata: L0"]}, "revisao")
    assert m5["motivos_revisao"] == [] and m5["requer_revisao"] is False
    assert m5["motivos_resolvidos"][-1]["removidos"] == ["duplicata: L0"]


def test_motivos_derivados_da_triagem_sao_recalculados():
    """Lote ABERTO: primeiro sem CPF, depois chega a mensagem com CPF — não pode travar."""
    m1, _ = mesclar_lote({}, _reg_triagem(), "triagem")
    assert m1["requer_revisao"] is True
    m2, _ = mesclar_lote(m1, _reg_triagem(cpf=CPF_T, identificacao="OK",
                                          motivos_revisao=[], requer_revisao=False,
                                          pronto=True), "triagem")
    assert m2["motivos_revisao"] == [] and m2["requer_revisao"] is False and m2["pronto"]


def test_motivo_novo_da_triagem_rebaixa_pronto_reivindicado():
    base, _ = mesclar_lote({}, _reg_triagem(), "triagem")
    ok, _ = mesclar_lote(base, PATCH_CPF, "identificacao")
    novo_conflito = _reg_triagem(identificacao="CONFLITO", pronto=False, motivos_revisao=[
        "CONFLITO: mais de um CPF de CLIENTE diferente no mesmo lote"])
    depois, _ = mesclar_lote(ok, novo_conflito, "triagem")
    assert depois["requer_revisao"] is True and depois["pronto"] is False


def test_download_monotonico_triagem_nao_rebaixa():
    base, _ = mesclar_lote({}, _reg_triagem(), "triagem")
    baixado, conf = mesclar_lote(base, {"arquivos": [{"file_id": "10", "baixado": True,
                                                      "sha256": "ab" * 32, "tamanho_bits": 9,
                                                      "caminho": "/x/01_a.pdf", "erro": None}]},
                                 "download")
    assert conf == []
    depois, conf = mesclar_lote(baixado, _reg_triagem(), "triagem")
    meta = depois["arquivos"][0]
    assert meta["baixado"] is True and meta["sha256"] == "ab" * 32 and meta["erro"] is None
    assert any(c.startswith("arquivos[10].") for c in conf)


def test_download_nao_apaga_meta_de_erro_dos_outros():
    """E5: reconciliar_por_file_ids substituía a lista e sumia com o meta de erro."""
    lote = {"file_ids": ["1", "2"], "arquivos": [
        {"file_id": "1", "indice": 1, "baixado": False, "erro": "x"},
        {"file_id": "2", "indice": 2, "baixado": False, "erro": "ACCESS_DENIED"}]}
    depois, _ = mesclar_lote(lote, {"arquivos": [{"file_id": "1", "indice": 1, "baixado": True,
                                                  "sha256": "c" * 64}]}, "download")
    assert [a["file_id"] for a in depois["arquivos"]] == ["1", "2"]
    assert depois["arquivos"][1]["erro"] == "ACCESS_DENIED"


def test_download_recusa_file_id_fora_do_lote():
    _, conf = mesclar_lote({"file_ids": ["1"], "arquivos": []},
                           {"arquivos": [{"file_id": "99", "baixado": True}]}, "download")
    assert conf and "99" in conf[0]


def test_dono_alheio_e_negado_e_reportado():
    _, conf = mesclar_lote({"cpf": "1"}, {"cpf": "2", "mensagens": []}, "download")
    assert set(conf) == {"cpf", "mensagens"}
    with pytest.raises(ValueError):
        mesclar_lote({}, {}, "hacker")


def test_campo_desconhecido_e_da_triagem_e_registrado():
    estado_store.CAMPOS_DESCONHECIDOS.discard("campo_novo_x")
    depois, conf = mesclar_lote({}, {"campo_novo_x": 1}, "triagem")
    assert depois["campo_novo_x"] == 1 and conf == []
    assert "campo_novo_x" in estado_store.CAMPOS_DESCONHECIDOS
    _, conf = mesclar_lote(depois, {"campo_novo_x": 2}, "ponte")
    assert conf == ["campo_novo_x"]


def test_nao_forja_metadado_interno():
    depois, _ = mesclar_lote({}, {"_donos_campos": {"cpf": "triagem"}, "cpf": "1"}, "identificacao")
    assert depois["_donos_campos"] == {"cpf": "identificacao"}


def test_mesclar_nao_altera_argumentos():
    atual = _reg_triagem()
    copia = json.loads(json.dumps(atual))
    mesclar_lote(atual, PATCH_CPF, "identificacao")
    assert atual == copia


def test_propriedade_cobre_campos_de_serializar_lote():
    campos = ["lote_id", "modo", "status", "pronto", "pronto_por", "requer_revisao",
              "identificacao", "confianca", "sinal_abertura", "sinal_fechamento",
              "motivos_revisao", "alerta", "cpf", "cpf_formatado", "cpf_responsavel",
              "cpf_responsavel_formatado", "cpfs_responsavel", "cpfs_distintos", "nome",
              "nomes_candidatos", "autores", "inicio", "fim", "fechado", "fechado_por",
              "marcador_fim_textual", "aberto", "qtd_arquivos", "file_ids", "mensagens",
              "arquivos", "pasta", "visto_em", "dialog_id"]
    assert [c for c in campos if c not in PROPRIEDADE_CAMPOS] == []
    assert set(PROPRIEDADE_CAMPOS.values()) <= set(estado_store.DONOS)


def test_atualizar_lotes_inexistente_so_triagem_cria(arq):
    conf = atualizar_lotes({"NOVO": {"cpf": "1"}}, "identificacao", caminho=arq)
    assert conf == {"NOVO": ["<lote inexistente>"]}
    assert atualizar_lotes({"NOVO": {"status": "ABERTO"}}, "triagem", caminho=arq) == {}
    assert ler_estado(arq)["lotes"]["NOVO"]["status"] == "ABERTO"


# ===========================================================================
# F1-04 — backups com retenção
# ===========================================================================
def test_fazer_backup_0600_no_subdiretorio_e_sem_duplicar(arq):
    assert fazer_backup(arq) is None                 # estado ausente
    gravar_atomico({"a": 1}, arq)
    b1 = fazer_backup(arq, motivo="completar CPF")
    assert os.path.dirname(b1).endswith("backups-estado")
    assert os.path.basename(b1).startswith("estado.json.bak-")
    assert stat.S_IMODE(os.stat(b1).st_mode) == 0o600
    assert fazer_backup(arq) == b1, "conteúdo igual não deve gerar outra cópia"
    gravar_atomico({"a": 2}, arq)
    b2 = fazer_backup(arq)
    assert b2 != b1 and json.load(open(b2)) == {"a": 2}


def test_podar_backups_mantem_24_mais_diarios_e_reconhece_legado(arq):
    gravar_atomico({"a": 1}, arq)
    d = os.path.dirname(arq)
    novos = os.path.join(d, "backups-estado")
    os.makedirs(novos)
    criados = []
    # 30 dias x 6 backups por dia (metade legado ao lado do estado, metade no subdir)
    for dia in range(1, 31):
        for h in range(6):
            nome = f"estado.json.bak-202610{dia:02d}-{h:02d}0000"
            pasta = d if h % 2 == 0 else novos
            cam = os.path.join(pasta, nome)
            _escrever(cam, "{}")
            criados.append(cam)
    _escrever(os.path.join(d, "estado.json.corrompido-20261001-000000"), "x")
    _escrever(os.path.join(d, "outro.json.bak-20261001-000000"), "x")

    simulado = podar_backups(arq, manter_ultimos=24, diarios=14, simular=True)
    assert all(os.path.exists(c) for c in criados), "simular=True apagou arquivo"
    restantes = set(criados) - set(simulado)
    # 24 mais novos = dias 30..27 (4 dias x 6); +1 por dia para os 14 dias mais recentes
    # (30..17) -> os dias 26..17 acrescentam 10
    assert len(restantes) == 24 + 10
    for dia in range(17, 31):
        assert any(f"202610{dia:02d}-" in r for r in restantes), dia
    apagados = podar_backups(arq, simular=False)
    assert sorted(apagados) == sorted(simulado)
    assert all(not os.path.exists(c) for c in apagados)
    assert os.path.exists(arq)
    assert os.path.exists(os.path.join(d, "estado.json.corrompido-20261001-000000"))
    assert os.path.exists(os.path.join(d, "outro.json.bak-20261001-000000"))


# ===========================================================================
# Z2 — donos dos campos usados pelo plano de recuperação (R1 ignorado, R2 cpf_confiavel)
# ===========================================================================
def test_donos_campos_do_plano_de_recuperacao_declarados():
    for c in ("ignorado", "ignorado_em", "ignorado_motivo", "ignorado_por"):
        assert PROPRIEDADE_CAMPOS[c] == "revisao", c
    assert PROPRIEDADE_CAMPOS["cpf_confiavel"] == "identificacao"
    estado_store.CAMPOS_DESCONHECIDOS.clear()
    for c in ("ignorado", "ignorado_motivo", "cpf_confiavel"):
        estado_store.dono_do_campo(c)
    assert estado_store.CAMPOS_DESCONHECIDOS == set()


def test_ignorado_pela_revisao_sobrevive_ao_ciclo_da_triagem():
    base = {"lote_id": "L1", "status": "SEM_CPF", "file_ids": []}
    marcado, conf = mesclar_lote(base, {"ignorado": True, "ignorado_motivo": "residuo R1",
                                        "ignorado_por": "recuperacao-r1"}, "revisao")
    assert conf == [] and marcado["ignorado"] is True
    # ciclo da triagem regrava o lote (e eventualmente um ignorado=False) — não desfaz
    depois, conf = mesclar_lote(marcado, {"lote_id": "L1", "status": "SEM_CPF",
                                          "ignorado": False, "ignorado_motivo": None}, "triagem")
    assert depois["ignorado"] is True and depois["ignorado_motivo"] == "residuo R1"
    assert set(conf) == {"ignorado", "ignorado_motivo"}
    # outro dono (download/ponte) não mexe; rollback é da revisão
    _, conf = mesclar_lote(depois, {"ignorado": False}, "ponte")
    assert conf == ["ignorado"]
    volta, conf = mesclar_lote(depois, {"ignorado": False}, "revisao")
    assert conf == [] and volta["ignorado"] is False


def test_ignorado_ainda_gravavel_pela_triagem_enquanto_ninguem_reivindica():
    """Compat com o plano já escrito (dono="triagem"): sem reivindicação a triagem,
    produtora-base, preenche — então o script antigo não dá conflito."""
    depois, conf = mesclar_lote({"lote_id": "L1"}, {"ignorado": True}, "triagem")
    assert conf == [] and depois["ignorado"] is True


def test_cpf_confiavel_e_da_identificacao_e_nao_e_reivindicado_pela_heuristica_legada():
    legado = {"lote_id": "L2", "cpf": CPF_T, "cpf_origem": "bitrix-nome"}
    # heurística legada (cpf_origem) reivindica `cpf`, mas não `cpf_confiavel`
    depois, conf = mesclar_lote(legado, {"cpf_confiavel": False}, "triagem")
    assert conf == [] and depois["cpf_confiavel"] is False
    depois, conf = mesclar_lote(legado, {"cpf_confiavel": False}, "identificacao")
    assert conf == [] and depois["_donos_campos"]["cpf_confiavel"] == "identificacao"
    depois2, conf = mesclar_lote(depois, {"cpf_confiavel": True}, "triagem")
    assert depois2["cpf_confiavel"] is False and conf == ["cpf_confiavel"]
    _, conf = mesclar_lote(depois, {"cpf_confiavel": True}, "download")
    assert conf == ["cpf_confiavel"]
