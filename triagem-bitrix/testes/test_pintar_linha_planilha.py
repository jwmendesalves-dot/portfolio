# -*- coding: utf-8 -*-
"""PL-01/PL-02 — pintar_linha x Google Sheets (fake em memória, sem rede).

PL-01: antes de escrever na "linha livre" a linha alvo é RELIDA na hora e só é gravada
se continuar livre (nunca sobrescreve nome/CPF de ninguém); depois de gravar, confere.
PL-02: sem lote novo a planilha NÃO é aberta/lida; a última linha é medida em B..G
(não na coluna A pré-numerada).
"""
import json
import os
import re
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpf_util
import pintar_linha as pl

CPF = cpf_util.gerar_para_teste(1)
CPF_B = cpf_util.gerar_para_teste(2)
CPF_C = cpf_util.gerar_para_teste(3)


# ---------------------------------------------------------- fake Sheets
def _col_num(letras: str) -> int:
    n = 0
    for ch in letras:
        n = n * 26 + (ord(ch) - 64)
    return n


RX_FAIXA = re.compile(r"'[^']*'!([A-Z]+)(\d*):([A-Z]+)(\d*)")
RX_CELULA = re.compile(r"'[^']*'!([A-Z]+)(\d+)$")


class _FakeCall:
    def __init__(self, resultado):
        self._r = resultado

    def execute(self):
        return self._r


class FakeServico:
    """Mini-planilha em memória no lugar do Google (mesma API encadeada do client)."""

    def __init__(self, ignorar_escritas: bool = False):
        self.celulas = {}            # (linha, coluna 1-based) -> valor
        self.valores_gravados = []   # bodies de values().batchUpdate
        self.grid_gravados = []      # bodies de batchUpdate (cor)
        self.faixas_lidas = []       # faixas de values().get
        self.ignorar_escritas = ignorar_escritas

    def cel(self, linha: int, coluna: int, valor) -> None:
        self.celulas[(linha, coluna)] = valor

    # API encadeada: svc.spreadsheets().values().get(...).execute()
    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId=None, range=None, fields=None, **kw):
        if fields:                    # spreadsheets().get(..., fields=...) = meta_aba
            return _FakeCall({"sheets": [{"properties": {
                "sheetId": 7, "title": pl.NOME_ABA,
                "gridProperties": {"columnCount": pl.COLUNAS_DA_LINHA}}}]})
        self.faixas_lidas.append(range)
        return _FakeCall({"values": self._ler_faixa(range)})

    def batchUpdate(self, spreadsheetId=None, body=None):
        if body and "valueInputOption" in body:
            self.valores_gravados.append(body)
            if not self.ignorar_escritas:
                for item in body.get("data", []):
                    m = RX_CELULA.search(item["range"])
                    self.celulas[(int(m.group(2)), _col_num(m.group(1)))] = item["values"][0][0]
        else:
            self.grid_gravados.append(body)
        return _FakeCall({})

    def _ler_faixa(self, faixa: str):
        m = RX_FAIXA.search(faixa)
        c1, r1, c2, r2 = _col_num(m.group(1)), m.group(2), _col_num(m.group(3)), m.group(4)
        ini = int(r1) if r1 else 1
        fim = int(r2) if r2 else max([r for r, _ in self.celulas] or [ini])
        linhas = []
        for r in range(ini, max(fim, ini) + 1):
            linhav = [self.celulas.get((r, c), "") for c in range(c1, c2 + 1)]
            while linhav and not str(linhav[-1] or "").strip():
                linhav.pop()          # Sheets corta células vazias no fim da linha
            linhas.append(linhav)
        while linhas and not any(str(v or "").strip() for v in linhas[-1]):
            linhas.pop()              # Sheets corta linhas vazias no fim
        return linhas


def plano_criar(linha, cpf, nome="ANA MARIA"):
    return {"acao": "criar", "linha": linha, "cpf": cpf_norm(cpf), "nome": nome,
            "lote": "LOTE-1", "escritas": {pl.COL_NOME: nome, pl.COL_CPF: cpf_util.formatar(cpf)}}


def cpf_norm(v):
    return pl.cpf_norm(v)


# --------------------------------------------------------------- PL-01
def test_pl01_grava_na_linha_livre_e_confere():
    svc = FakeServico()
    plano = plano_criar(3, CPF)
    confirmados = pl.gravar(svc, [plano], gid=7)
    assert confirmados == [plano]                       # confirmado = pode ir a pintadas
    assert svc.celulas[(3, pl.COL_CPF)] == cpf_util.formatar(CPF)
    assert svc.celulas[(3, pl.COL_NOME)] == "ANA MARIA"
    assert svc.valores_gravados and svc.grid_gravados   # valores + cor roxa


def test_pl01_linha_ocupada_na_hora_NAO_sobrescreve():
    """Corrida PL-01: painel/equipe ocupou a linha livre entre o planejamento e a escrita.
    O código antigo gravava mesmo assim e trocava NOME/CPF de outra pessoa."""
    svc = FakeServico()
    svc.cel(3, pl.COL_NOME, "OUTRA PESSOA")
    svc.cel(3, pl.COL_CPF, cpf_util.formatar(CPF_B))
    plano = plano_criar(3, CPF)
    confirmados = pl.gravar(svc, [plano], gid=7)
    assert confirmados == []
    assert svc.valores_gravados == []                   # NADA foi escrito
    assert svc.celulas[(3, pl.COL_NOME)] == "OUTRA PESSOA"
    assert svc.celulas[(3, pl.COL_CPF)] == cpf_util.formatar(CPF_B)
    assert "ocupada" in plano["motivo"]


def test_pl01_gravado_mas_nao_confere_nao_vai_para_pintadas():
    svc = FakeServico(ignorar_escritas=True)            # escrita "não gruda"
    plano = plano_criar(3, CPF)
    confirmados = pl.gravar(svc, [plano], gid=7)
    assert confirmados == []
    assert "não confere" in plano["motivo"].lower()
    assert svc.grid_gravados == []                      # não pinta linha não confirmada


def test_pl01_plano_de_linha_existente_so_pinta_sem_escrever_valores():
    svc = FakeServico()
    svc.cel(2, pl.COL_NOME, "ANA")
    svc.cel(2, pl.COL_CPF, cpf_util.formatar(CPF_C))
    plano = {"acao": "pintar", "linha": 2, "cpf": cpf_norm(CPF_C), "nome": "ANA",
             "lote": "LOTE-2", "escritas": {}}
    confirmados = pl.gravar(svc, [plano], gid=7)
    assert confirmados == [plano]
    assert svc.valores_gravados == []                   # nunca escreve em linha já usada
    assert svc.grid_gravados                            # mas pinta a cor


# --------------------------------------------------------------- PL-02
def test_pl02_meta_aba_medem_b_g_nao_a_coluna_a_pre_numerada():
    """A coluna A é pré-numerada até o fim da grade; medir por ela fazia `ultima` ≈
    total da grade e disparava leitura de ~10 mil linhas a cada rodada."""
    svc = FakeServico()
    for r in range(1, 21):
        svc.cel(r, 1, f"{r} P")                       # A pré-numerada até a linha 20
    svc.cel(2, pl.COL_NOME, "ANA")
    svc.cel(2, pl.COL_CPF, cpf_util.formatar(CPF))
    gid, ultima, colunas = pl.meta_aba(svc)
    assert gid == 7
    assert ultima == 2                                  # última linha com conteúdo em B..G
    assert colunas == pl.COLUNAS_DA_LINHA


def test_pl02_sem_lote_novo_NAO_abre_a_planilha(tmp_path, monkeypatch):
    """Tudo já pintado → a planilha inteira não pode ser lida a cada 2 min."""
    estado = {"lotes": {"a": {"lote_id": "LOTE-1", "pronto": True, "cpf": CPF,
                              "nome": "ANA", "fim": "2026-11-09T10:00:00-03:00"}}}
    pintadas = {cpf_norm(CPF): {"linha": 2, "em": "2026-11-09T10:00:00"}}
    arq_estado = tmp_path / "estado.json"
    arq_pintadas = tmp_path / "pintadas.json"
    arq_estado.write_text(json.dumps(estado), encoding="utf-8")
    arq_pintadas.write_text(json.dumps(pintadas), encoding="utf-8")
    monkeypatch.setattr(pl, "ARQ_ESTADO", str(arq_estado))
    monkeypatch.setattr(pl, "ARQ_PINTADAS", str(arq_pintadas))

    def _nao_abrir():
        raise AssertionError("PL-02: não devia abrir a planilha sem lote novo")
    monkeypatch.setattr(pl, "abrir", _nao_abrir)

    assert pl.main(["--aplicar", "--todos"]) == 0


def test_pl02_fluxo_com_lote_novo_cria_linha_e_registra(tmp_path, monkeypatch):
    svc = FakeServico()
    svc.cel(1, pl.COL_NOME, "NOME CLIENTE")            # cabeçalho real (linha 1)
    svc.cel(1, pl.COL_CPF, "CPF")
    estado = {"lotes": {"a": {"lote_id": "LOTE-1", "pronto": True, "cpf": CPF,
                              "nome": "ANA MARIA", "fim": "2026-11-09T10:00:00-03:00"}}}
    arq_estado = tmp_path / "estado.json"
    arq_pintadas = tmp_path / "pintadas.json"
    arq_estado.write_text(json.dumps(estado), encoding="utf-8")
    monkeypatch.setattr(pl, "ARQ_ESTADO", str(arq_estado))
    monkeypatch.setattr(pl, "ARQ_PINTADAS", str(arq_pintadas))
    monkeypatch.setattr(pl, "abrir", lambda: svc)

    assert pl.main(["--aplicar", "--todos"]) == 0
    assert svc.celulas[(2, pl.COL_CPF)] == cpf_util.formatar(CPF)
    pintadas = json.loads(arq_pintadas.read_text(encoding="utf-8"))
    assert cpf_norm(CPF) in pintadas
    assert pintadas[cpf_norm(CPF)]["acao"] == "criar"


def test_pl02_linha_ocupada_na_corrida_fica_para_a_proxima_rodada(tmp_path, monkeypatch):
    """PL-01+PL-02 juntos: a linha livre é ocupada entre o planejamento e a escrita →
    nada gravado e nada registrado; a próxima rodada reprocessa."""
    class CorridaFake(FakeServico):
        """1ª releitura da linha alvo (na hora da escrita) mostra a linha ocupada —
        o painel/equipe escreveu no meio da rodada."""

        def __init__(self):
            super().__init__()
            self.corrida_feita = False

        def _ler_faixa(self, faixa):
            if re.search(r"!B\d+:G\d+$", faixa) and not self.corrida_feita:
                # só a releitura da linha alvo (B..G de UMA linha) vê a corrida
                self.corrida_feita = True
                return [["CHEGOU ANTES", "", "", "", "", cpf_util.formatar(CPF_B)]]
            return super()._ler_faixa(faixa)

    svc = CorridaFake()
    svc.cel(1, pl.COL_NOME, "NOME CLIENTE")
    estado = {"lotes": {"a": {"lote_id": "LOTE-1", "pronto": True, "cpf": CPF,
                              "nome": "ANA MARIA", "fim": "2026-11-09T10:00:00-03:00"}}}
    arq_estado = tmp_path / "estado.json"
    arq_pintadas = tmp_path / "pintadas.json"
    arq_estado.write_text(json.dumps(estado), encoding="utf-8")
    monkeypatch.setattr(pl, "ARQ_ESTADO", str(arq_estado))
    monkeypatch.setattr(pl, "ARQ_PINTADAS", str(arq_pintadas))
    monkeypatch.setattr(pl, "abrir", lambda: svc)

    assert pl.main(["--aplicar", "--todos"]) == 0
    assert svc.valores_gravados == []                    # nada sobrescrito
    pintadas = json.loads(arq_pintadas.read_text(encoding="utf-8")) if arq_pintadas.exists() else {}
    assert cpf_norm(CPF) not in pintadas                 # fica para a próxima rodada
