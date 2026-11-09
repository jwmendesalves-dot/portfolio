"""Teste estático da PONTE: ela NÃO pode parar o worker para gravar na fila.

Histórico (24/09/2026): `gravar_na_fila()` fazia `systemctl stop agente-separador-worker`
antes de enfileirar (e abortava a rodada se o worker estivesse ocupado). Efeito medido:
rodada adiada em série e job interrompido no meio. O `FilaJobs.enfileirar/persistir` já
lê o disco FRESCO e MESCLA por id ("Single-writer por OPERAÇÃO") — é o mesmo mecanismo
que o painel usa no `POST /api/disparar`, que passa com o worker trabalhando.
"""
import os
from conftest import RAIZ
import ast
import inspect
import re
import sys
import textwrap

sys.path.insert(0, RAIZ)

import lote_para_fila as lpf  # noqa: E402


def _fonte(fn) -> str:
    return inspect.getsource(fn)


def _roda_systemctl(fn) -> bool:
    """True se a função CHAMA `subprocess.run(["systemctl", ...])` (ignora comentário/docstring)."""
    arvore = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for no in ast.walk(arvore):
        if isinstance(no, ast.Call):
            for arg in no.args:
                if isinstance(arg, ast.List) and any(
                        isinstance(e, ast.Constant) and e.value == "systemctl" for e in arg.elts):
                    return True
    return False


def _nomes_usados(fn) -> set[str]:
    """Nomes/atributos efetivamente USADOS no corpo (ignora docstring)."""
    arvore = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    nomes = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Name):
            nomes.add(no.id)
        elif isinstance(no, ast.Attribute):
            nomes.add(no.attr)
    return nomes


def test_gravar_na_fila_nao_para_o_worker():
    assert not _roda_systemctl(lpf.gravar_na_fila), \
        "a ponte voltou a parar/religar o worker para gravar na fila"


def test_gravar_na_fila_nao_aborta_quando_o_worker_esta_ocupado():
    usados = _nomes_usados(lpf.gravar_na_fila)

    assert "esperar_ocioso" not in usados
    assert "WorkerOcupado" not in usados


def test_persistencia_atomica_vem_do_filajobs_do_painel():
    """A gravação atômica (tmp + rename) é do FilaJobs — o mesmo componente que o painel
    usa. Se alguém trocar por escrita não-atômica, a ponte pode corromper a fila do worker."""
    import pytest
    import caminhos

    caminho = caminhos.em_separador("src", "fila-jobs.js")
    if not os.path.exists(caminho):
        pytest.skip(f"{caminho} é do painel (agente-separador) e não está nesta cópia — "
                    "verificar no servidor: renameSync + .tmp em FilaJobs.persistir")
    fonte = open(caminho, encoding="utf-8").read()

    assert "renameSync" in fonte
    assert ".tmp" in fonte


def _perdidos(antes, depois, novos):
    return lpf.perdidos_relevantes(antes, depois, novos)


def test_job_ativo_que_sumiu_da_fila_e_reportado():
    antes = [{"id": "uuid-1", "status": "processando", "atualizado_em": "2026-09-24T14:00:00Z"}]

    assert _perdidos(antes, [], []) == ["uuid-1"]


def test_registro_nosso_que_nao_entrou_e_reportado():
    novos = [{"id": "grupo-LOTE-B", "status": "pendente"}]

    assert _perdidos([], [], novos) == ["grupo-LOTE-B"]


def test_job_terminal_antigo_que_saiu_por_retencao_nao_e_reportado():
    """FilaJobs mantém só os 100 últimos terminais: job concluído pode sair sem drama."""
    antes = [{"id": "grupo-LOTE-VELHO", "status": "concluido", "atualizado_em": "2026-09-18T10:00:00Z"},
             {"id": "grupo-LOTE-NOVO", "status": "pendente", "atualizado_em": "2026-09-24T14:00:00Z"}]
    depois = [{"id": "grupo-LOTE-NOVO", "status": "pendente", "atualizado_em": "2026-09-24T14:00:00Z"}]

    assert _perdidos(antes, depois, []) == []


def test_fila_intacta_nao_reporta_nada():
    fila = [{"id": "a", "status": "concluido"}, {"id": "b", "status": "processando"}]

    assert _perdidos(fila, fila, [{"id": "c"}]) == ["c"]


def test_perdidos_aguenta_entradas_vazias_ou_estranhas():
    assert _perdidos([], [], []) == []
    assert _perdidos(None, None, None) == []
    assert _perdidos([{"sem_id": 1}], [], []) == []


def test_script_node_usa_enfileirar_e_nao_reescreve_arquivo_na_mao():
    fonte = lpf.SCRIPT_NODE
    assert "SCRIPT_NODE" in _nomes_usados(lpf.gravar_na_fila)

    assert "FilaJobs" in fonte, "deve usar a classe FilaJobs (merge por id), não JSON manual"
    assert re.search(r"enfileirar\(", fonte), "deve chamar fila.enfileirar(...)"
    assert "/opt/" not in fonte, "o caminho do fila-jobs.js vem por argumento (caminhos.SEPARADOR)"
