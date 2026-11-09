"""Testes do adaptador TELEGRAM → esteira (TDD).

Pedido do dono (24/09/2026): *"conecta um bot do telegram na mesma esteira, a diferente
que lá vai ser o /start em vez do ——"*.

Regra: no Telegram o sinal é **/start** (a mensagem `/start` ou `/start@documentosexemploBot`);
ele entra na esteira como TRAÇO, então TODO o resto (agrupamento, delimitador, ponte,
painel, worker) funciona sem mudança.
"""
from conftest import RAIZ
import sys

sys.path.insert(0, RAIZ)

from telegram_regras import (  # noqa: E402
    arquivos_do_update,
    autor_de,
    chat_id_de,
    chat_permitido,
    chats_permitidos,
    mensagem_api,
    nome_autor_de,
    telegram_habilitado,
    texto_do_update,
)

# formato REAL do config.json: a allowlist fica ANINHADA em `telegram.chats_permitidos`
CFG = {"sinal_traco": ["-", "–", "—", "=", "_"], "traco_telegram": "/start",
       "telegram": {"habilitado": True, "chats_permitidos": ["1000000002", "-1001234567890"]}}


def update_texto(texto, chat=1000000002, autor="Karol", user_id=42, msg_id=7):
    return {"update_id": 1, "message": {"message_id": msg_id, "date": 1758700000,
                                        "chat": {"id": chat, "type": "private"},
                                        "from": {"id": user_id, "first_name": autor},
                                        "text": texto}}


def update_foto(caption="", chat=1000000002, autor="Karol", user_id=42, msg_id=8):
    return {"update_id": 2, "message": {
        "message_id": msg_id, "date": 1758700001,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": user_id, "first_name": autor},
        "caption": caption,
        "photo": [{"file_id": "pequena", "file_unique_id": "u1", "file_size": 100},
                  {"file_id": "grande", "file_unique_id": "u2", "file_size": 90000}]}}


def update_documento(nome="kit.pdf", chat=1000000002, autor="Karol", user_id=42, msg_id=9):
    return {"update_id": 3, "message": {
        "message_id": msg_id, "date": 1758700002,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": user_id, "first_name": autor},
        "document": {"file_id": "doc1", "file_unique_id": "u3", "file_name": nome,
                     "file_size": 123456, "mime_type": "application/pdf"}}}


# ── texto ───────────────────────────────────────────────────────────────────
def test_texto_do_update_pega_texto_ou_caption():
    assert texto_do_update(update_texto("bom dia")) == "bom dia"
    assert texto_do_update(update_foto("= MARIA = CPF 111.222.333-44")) == "= MARIA = CPF 111.222.333-44"
    assert texto_do_update({"update_id": 4, "message": {"message_id": 1}}) == ""


# ── sinal /start -> traço ───────────────────────────────────────────────────
def test_start_vira_traço_para_a_esteira():
    m = mensagem_api(update_texto("/start"), CFG)

    assert m["text"].endswith("—"), m["text"]
    assert m["params"]["FILE_ID"] == []


def test_start_com_arroba_do_bot_tambem_vira_traço():
    assert mensagem_api(update_texto("/start@documentosexemploBot"), CFG)["text"].endswith("—")


def test_texto_normal_nao_e_sinal():
    m = mensagem_api(update_texto("= MARIA = CPF 111.222.333-44"), CFG)

    assert "MARIA" in m["text"]
    assert "—" not in m["text"]


def test_barra_de_outro_comando_nao_vira_traço():
    assert "help" in mensagem_api(update_texto("/help"), CFG)["text"]


# ── arquivos ────────────────────────────────────────────────────────────────
def test_foto_entrega_so_a_maior_versao():
    arquivos = arquivos_do_update(update_foto())

    assert [a["file_id"] for a in arquivos] == ["grande"]
    assert arquivos[0]["tipo"] == "foto"


def test_documento_entrega_nome_e_tamanho():
    arquivos = arquivos_do_update(update_documento("kit.pdf"))

    assert arquivos == [{"file_id": "doc1", "nome": "kit.pdf", "tamanho": 123456,
                         "tipo": "documento", "mime": "application/pdf"}]


def test_sem_arquivo_devolve_vazio():
    assert arquivos_do_update(update_texto("oi")) == []
    assert arquivos_do_update({"update_id": 5}) == []


def test_arquivos_entram_no_formato_do_classificador():
    m = mensagem_api(update_documento("kit.pdf"), CFG)

    assert m["params"]["FILE_ID"] == ["doc1"]


# ── identificação do chat e do autor ────────────────────────────────────────
def test_chat_e_autor():
    assert chat_id_de(update_texto("oi")) == "1000000002"
    assert autor_de(update_texto("oi")) == "Karol"
    assert nome_autor_de({"from": {"first_name": "Ana", "last_name": "Souza"}}) == "Ana Souza"
    assert nome_autor_de({}) == "Telegram"


def test_chat_permitido_respeita_a_lista_no_formato_aninhado():
    # TG-01: o config.json real guarda a lista em `telegram.chats_permitidos` (aninhado)
    assert chat_permitido("1000000002", CFG) is True
    assert chat_permitido("-1001234567890", CFG) is True
    assert chat_permitido("999", CFG) is False


def test_lista_vazia_significa_ninguem_autorizado():
    # S03 (CRÍTICA): lista vazia = FECHADO por padrão — antes aceitava qualquer chat
    # e o arquivo de estranho entrava direto na produção.
    vazio = {**CFG, "telegram": {"habilitado": True, "chats_permitidos": []}}
    assert chat_permitido("1000000002", vazio) is False
    assert chat_permitido("999", vazio) is False


def test_config_sem_allowlist_tambem_fica_fechado():
    assert chat_permitido("1000000002", {}) is False
    assert chat_permitido("1000000002", {"telegram": {"habilitado": True}}) is False


def test_formato_legado_no_nivel_de_cima_ainda_e_aceito():
    legado = {"chats_permitidos": ["111"]}
    assert chat_permitido("111", legado) is True
    assert chat_permitido("222", legado) is False


def test_chats_permitidos_lista_efetiva():
    assert chats_permitidos(CFG) == ["1000000002", "-1001234567890"]
    assert chats_permitidos({"telegram": {"chats_permitidos": []}}) == []
    assert chats_permitidos({}) == []


def test_telegram_habilitado_exige_a_chave_verdadeira():
    # exigência da remediação: habilitado=false (ou ausente) = canal desligado
    assert telegram_habilitado(CFG) is True
    assert telegram_habilitado({"telegram": {"habilitado": False}}) is False
    assert telegram_habilitado({"telegram": {}}) is False
    assert telegram_habilitado({}) is False


def test_data_do_update_vira_iso():
    m = mensagem_api(update_texto("oi"), CFG)

    assert str(m["date"]).startswith("20") and "T" in m["date"], m["date"]
