# -*- coding: utf-8 -*-
"""Fixtures SINTÉTICAS para os testes do job/ficha (frente J, OP-T2).

Substituem a leitura do `estado.json` de produção e os PDFs de contrato reais.
Tudo aqui é fictício: CPFs gerados por `cpf_util.gerar_para_teste` (DV válido),
nomes inventados, e-mail @example.com, telefones fora de uso, senha fictícia.

- `lote_molde(...)`: lote com a MESMA estrutura que `triagem_grupo.serializar_lote`
  grava no estado.json (todas as chaves), para `lote_para_job.montar_job`.
- `TEXTO_TABELA_VARIAVEIS`: o texto que `pdftotext -layout` devolve da página de
  variáveis de um contrato ZapSign (colunas separadas por 2+ espaços).
- `escrever_pdf(caminho, linhas)`: PDF mínimo (stdlib, Courier) com essas linhas,
  para exercitar o caminho real `pdftotext` quando ele existir na máquina.
"""
from __future__ import annotations

import cpf_util

CPF_CLIENTE = cpf_util.gerar_para_teste(9101)
CPF_CLIENTE_FMT = cpf_util.formatar(CPF_CLIENTE)
CPF_OUTRO = cpf_util.gerar_para_teste(9102)
CPF_OUTRO_FMT = cpf_util.formatar(CPF_OUTRO)

NOME_CLIENTE = "MARIA FICTICIA DOS TESTES"
SENHA_FICTICIA = "Fict@Senha2024"
SENHA_FICHA_DIVERGENTE = "Outra#Fict99"
EMAIL_FICTICIO = "maria.ficticia@example.com"
BAIRRO = "Bairro Ficticio"
CIDADE = "Manaus"
UF = "AM"
CEP = "69000000"
TEL_CELULAR = "(92) 90000-1111"       # repetido em "trabalho" (como o ZapSign faz)
TEL_OUTRO = "(92) 90000-2222"
SERVICO = "Auxílio Maternidade Facultativo"


def _linha(rotulo: str, tag: str, valor: str) -> str:
    return f"{rotulo:<26}{tag:<34}{valor}"


LINHAS_TABELA = [
    "PÁGINA DE VARIÁVEIS DO CONTRATO",
    "",
    _linha("CAMPOS", "BT VARIAVEL", "ZAPSIGN VARIAVEL"),
    _linha("Nome", "{Name}", NOME_CLIENTE),
    _linha("Sobrenome", "{LastName}", "0"),
    _linha("RG", "{UfCrm1700000000001}", "1234567-8"),
    _linha("CPF", "{UfCrm1524762221558}", CPF_CLIENTE_FMT),
    _linha("Estado Civil", "{UfCrm1700000000002}", "Solteiro(a)"),
    _linha("Rua", "{UfCrm1700000000003}", "Rua das Flores Ficticias"),
    _linha("Nº da Casa", "{UfCrm1700000000004}", "123"),
    _linha("Bairro", "{UfCrm1700000000005}", BAIRRO),
    _linha("Cidade", "{UfCrm1753903279519} ,", CIDADE),
    _linha("Estado", "{UfCrm1700000000006}", UF),
    _linha("CEP", "{UfCrm1700000000007}", CEP),
    _linha("E-mail", "{Email}", EMAIL_FICTICIO),
    _linha("Telefone celular", "{Phone}", TEL_CELULAR),
    _linha("Telefone trabalho", "{Phone2}", TEL_CELULAR),
    _linha("Telefone outro", "{Phone3}", TEL_OUTRO),
    _linha("NIS", "{UfCrm1700000000008}", "0"),
    _linha("Senha do MEU INSS", "{UfCrm1725391896584}", SENHA_FICTICIA),
    _linha("Serviço Contratado", "{UfCrm1725033408826}", SERVICO + ", e para isso"),
]
TEXTO_TABELA_VARIAVEIS = "\n".join(LINHAS_TABELA) + "\n"

LINHAS_SEM_TABELA = [
    "CONTRATO DE PRESTACAO DE SERVICOS (FICTICIO)",
    "",
    "O(a) contratante declara ter lido e concordado com as clausulas abaixo.",
    "Este documento nao traz a pagina de variaveis.",
]
TEXTO_SEM_TABELA = "\n".join(LINHAS_SEM_TABELA) + "\n"


def lote_molde(lote_id: str = "LOTE-SIG-20991231-0000-sintetico", *, nome: str = "", cpf: str = "",
               mensagens: list | None = None, arquivos: list | None = None,
               dialog_id: str = "chat000000") -> dict:
    """Lote com todas as chaves de `serializar_lote` (valores fictícios)."""
    arquivos = list(arquivos or [])
    return {
        "lote_id": lote_id,
        "modo": "marcadores",
        "status": "FECHADO_PRONTO",
        "pronto": True,
        "pronto_por": "traco",
        "requer_revisao": False,
        "identificacao": "OK" if cpf else "SEM_CPF",
        "confianca": "alta",
        "sinal_abertura": "traco",
        "sinal_fechamento": "traco",
        "motivos_revisao": [],
        "alerta": None,
        "cpf": cpf or "",
        "cpf_formatado": cpf_util.formatar(cpf) if cpf else "",
        "cpf_responsavel": None,
        "cpf_responsavel_formatado": None,
        "cpfs_responsavel": [],
        "cpfs_distintos": [cpf] if cpf else [],
        "nome": nome,
        "nomes_candidatos": [nome] if nome else [],
        "autores": ["Atendente Ficticia"],
        "inicio": "2099-12-31T10:00:00-04:00",
        "fim": "2099-12-31T10:20:00-04:00",
        "fechado": True,
        "fechado_por": "traco",
        "marcador_fim_textual": False,
        "aberto": False,
        "qtd_arquivos": len(arquivos),
        "file_ids": [str(a.get("file_id")) for a in arquivos if a.get("file_id")],
        "mensagens": list(mensagens or []),
        "arquivos": arquivos,
        "pasta": "/nao-existe/lotes/" + lote_id,
        "visto_em": "2099-12-31T14:20:00+00:00",
        "dialog_id": dialog_id,
    }


def arquivo_pdf(caminho: str, nome: str = "CONTRATO ASSINADO.pdf", file_id: str = "990001") -> dict:
    import os
    return {"file_id": file_id, "indice": 1, "baixado": True, "erro": None,
            "nome_salvo": nome, "nome_original": nome, "caminho": caminho,
            "tamanho_bits": os.path.getsize(caminho), "sha256": None}


def _escapar_pdf(s: str) -> bytes:
    b = s.encode("cp1252", "replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def escrever_pdf(caminho: str, linhas: list) -> str:
    """PDF de 1 página, Courier 9pt (monoespaçada: o -layout preserva as colunas)."""
    corpo = [b"BT", b"/F1 9 Tf", b"11 TL", b"30 800 Td"]
    for ln in linhas:
        corpo.append(b"(" + _escapar_pdf(ln) + b") Tj T*")
    corpo.append(b"ET")
    stream = b"\n".join(corpo)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
    ]
    saida = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(saida))
        saida += str(i).encode() + b" 0 obj\n" + o + b"\nendobj\n"
    xref = len(saida)
    saida += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        saida += b"%010d 00000 n \n" % off
    saida += (b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n"
              + str(xref).encode() + b"\n%%EOF\n")
    with open(caminho, "wb") as fh:
        fh.write(bytes(saida))
    return caminho


def contrato_sintetico(tmp_dir: str, com_tabela: bool = True) -> str:
    """Grava um PDF sintético (com ou sem a página de variáveis) e devolve o caminho."""
    import os
    nome = "contrato_com_tabela.pdf" if com_tabela else "contrato_sem_tabela.pdf"
    return escrever_pdf(os.path.join(str(tmp_dir), nome),
                        LINHAS_TABELA if com_tabela else LINHAS_SEM_TABELA)


def instalar_texto_pdf(monkeypatch, modulo_job) -> None:
    """Faz `lote_para_job._texto_pdf` devolver o texto layout do PDF sintético.

    O `pdftotext` (poppler) não existe em toda máquina de desenvolvimento; o que
    estes testes verificam é o parser da tabela + a montagem do job. O caminho
    real do pdftotext é coberto por `test_job_senha_s01.py::test_pdftotext_real_*`
    (pulado quando o binário não existe).
    """
    def _falso(caminho: str) -> str:
        nome = str(caminho)
        if nome.endswith("contrato_com_tabela.pdf"):
            return TEXTO_TABELA_VARIAVEIS
        if nome.endswith("contrato_sem_tabela.pdf"):
            return TEXTO_SEM_TABELA
        return ""
    monkeypatch.setattr(modulo_job, "_texto_pdf", _falso)
