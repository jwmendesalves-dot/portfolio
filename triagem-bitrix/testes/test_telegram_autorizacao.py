# -*- coding: utf-8 -*-
"""
TDD — AUTORIZAÇÃO do canal Telegram (S03 / TG-01).

S03 (CRÍTICA): com `telegram.chats_permitidos = []` (o que o config.json de produção
tem hoje) o bot aceitava arquivo de QUALQUER pessoa e o material entrava na produção.
TG-01: além disso o código lia `chats_permitidos` no nível de cima do config, enquanto
o config real guarda a lista ANINHADA em `telegram.chats_permitidos` — preencher a
lista não tinha efeito.

Regra nova (fechado por padrão):
  · lista vazia/ausente = NINGUÉM autorizado;
  · formato real do config.json: `{"telegram": {"habilitado": true, "chats_permitidos": [...]}}`;
  · a autorização vale para RECEPÇÃO, DOWNLOAD, RESPOSTA, BUFFER, GRAVAÇÃO e
    ENCAMINHAMENTO — todo este arquivo cobre os seis pontos.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

import estado_store  # noqa: E402
import telegram_grupo as TG  # noqa: E402
import telegram_regras as TR  # noqa: E402

# formato REAL do config.json (chave `telegram` aninhada, como em produção)
CFG_OK = {"sinal_traco": ["-", "–", "—", "=", "_"], "traco_telegram": "/start",
          "modo_traco": "delimitador", "timeout_lote_min": 120, "janela_min_fallback": 30,
          "traco_no_fim_fecha": True,
          "telegram": {"habilitado": True, "chats_permitidos": ["111"]}}
CFG_VAZIO = {**CFG_OK, "telegram": {"habilitado": True, "chats_permitidos": []}}


def U(msg_id, texto="", chat=111, autor="Ana", fotos=0):
    m = {"message_id": msg_id, "date": 1790251200 + msg_id,
         "chat": {"id": chat, "type": "private"},
         "from": {"id": 42, "first_name": autor}}
    if texto:
        m["text"] = texto
    if fotos:
        m["photo"] = [{"file_id": f"f{msg_id}-{i}", "file_size": 100 + i}
                      for i in range(fotos)]
    return {"update_id": msg_id, "message": m}


def lote_de(dialog_id, lid="LOTE-X"):
    return {"lote_id": lid, "status": "FECHADO_PRONTO", "file_ids": [], "arquivos": [],
            "dialog_id": dialog_id, "fonte_entrada": "telegram"}


# ── 1. recepção (processar) ────────────────────────────────────────────────
def test_recepcao_ignora_chat_fora_da_allowlist():
    resumo = TG.processar([U(1, "= MARIA = CPF 111.222.333-44", chat=999)], CFG_OK, simular=True)

    assert resumo["lotes"] == []
    assert resumo["ignorados"] == 1


def test_allowlist_vazia_nao_deixa_ninguem_entrar():
    """S03: a allowlist vazia do config de produção não pode significar 'tudo liberado'."""
    resumo = TG.processar([U(1, "= MARIA = CPF 111.222.333-44", chat=111),
                           U(2, "= JOAO = CPF 555.666.777-88", chat=999, fotos=1)],
                          CFG_VAZIO, simular=True)

    assert resumo["lotes"] == []
    assert resumo["ignorados"] == 2


# ── 2. download (baixar_arquivo_telegram) ──────────────────────────────────
def test_download_recusa_chat_nao_autorizado(monkeypatch):
    chamadas = []
    monkeypatch.setattr(TG, "chamar", lambda *a, **k: chamadas.append(k) or {"result": {}})

    meta = TG.baixar_arquivo_telegram("TOK", {"file_id": "F1", "nome": "a.pdf", "tamanho": 10,
                                             "tipo": "documento", "mime": "application/pdf"},
                                      "/nao/cria/nada", 1, "LOTE-1", cfg=CFG_OK, chat=999)

    assert meta["baixado"] is False
    assert "autorizado" in meta["erro"]
    assert meta["erro_tipo"] == "permanente"
    assert chamadas == [], "nem o getFile pode ser chamado para chat não autorizado"


def test_download_recusa_sem_config_de_autorizacao(monkeypatch):
    chamadas = []
    monkeypatch.setattr(TG, "chamar", lambda *a, **k: chamadas.append(k) or {"result": {}})

    meta = TG.baixar_arquivo_telegram("TOK", {"file_id": "F1", "nome": "a.pdf", "tamanho": 10,
                                             "tipo": "documento"}, "pasta", 1, "LOTE-1")

    assert meta["baixado"] is False and chamadas == [], "sem allowlist = fechado"


# ── 3. resposta (decidir_respostas) ────────────────────────────────────────
def test_resposta_nao_vai_para_chat_nao_autorizado():
    est = {}
    assert TG.decidir_respostas([U(1, "/start", chat=999)], est, CFG_OK) == []
    assert est.get("aberto", {}) == {}, "nem o estado do chat não autorizado pode ser aberto"


def test_resposta_com_allowlist_vazia_ninguem_recebe():
    est = {}
    assert TG.decidir_respostas([U(1, "/start", chat=111)], est, CFG_VAZIO) == []


# ── 4. buffer (acumular_buffer) ────────────────────────────────────────────
def test_buffer_nao_guarda_chat_nao_autorizado():
    est = {}
    bloco = TG.acumular_buffer(est, [U(1, fotos=2, chat=999), U(2, "/start", chat=999)], CFG_OK)

    assert bloco == []
    assert est.get("buffer", {}) == {}, "material de chat não autorizado não pode ficar bufferizado"


# ── 5. gravação (mesclar_no_estado) ────────────────────────────────────────
def test_gravacao_ignora_lote_de_chat_nao_autorizado():
    resumo = {"chats": {"111": {"mensagens": 1, "lotes": 1, "autores": ["Ana"]},
                        "999": {"mensagens": 1, "lotes": 1, "autores": ["Estranho"]}},
              "lotes": [lote_de("tg:111", "LOTE-BOM"), lote_de("tg:999", "LOTE-RUIM")]}

    TG.mesclar_no_estado(resumo, cfg=CFG_OK)

    est = estado_store.ler_estado()
    assert "LOTE-BOM" in (est.get("lotes") or {})
    assert "LOTE-RUIM" not in (est.get("lotes") or {})
    assert "tg:999" not in (est.get("dialogos") or {})


def test_gravacao_com_allowlist_vazia_nao_grava_nada():
    resumo = {"chats": {"111": {"mensagens": 1, "lotes": 1, "autores": ["Ana"]}},
              "lotes": [lote_de("tg:111", "LOTE-NINGUEM")]}

    TG.mesclar_no_estado(resumo, cfg=CFG_VAZIO)

    assert "LOTE-NINGUEM" not in (estado_store.ler_estado().get("lotes") or {})


# ── 6. encaminhamento (falar_no_chat) ──────────────────────────────────────
def test_encaminhamento_recusa_chat_nao_autorizado(monkeypatch):
    chamadas = []
    monkeypatch.setattr(TG, "chamar", lambda *a, **k: chamadas.append(k) or {"result": {}})

    assert TG.falar_no_chat("TOK", "999", "oi", cfg=CFG_OK) is False
    assert chamadas == [], "nada de sendMessage para chat não autorizado"


def test_encaminhamento_para_chat_autorizado_continua_funcionando(monkeypatch):
    chamadas = []
    monkeypatch.setattr(TG, "chamar", lambda *a, **k: chamadas.append(k) or {"result": {}})

    assert TG.falar_no_chat("TOK", "111", "oi", cfg=CFG_OK) is True
    assert [k.get("chat_id") for k in chamadas] == ["111"]


# ── formato real x legado (TG-01) ──────────────────────────────────────────
def test_allowlist_lida_no_formato_aninhado_real_do_config():
    assert TR.chat_permitido("111", CFG_OK) is True
    assert TR.chat_permitido("999", CFG_OK) is False


def test_formato_legado_no_nivel_de_cima_ainda_e_aceito():
    legado = {"sinal_traco": ["-"], "chats_permitidos": ["111"]}

    assert TR.chat_permitido("111", legado) is True
    assert TR.chat_permitido("999", legado) is False
