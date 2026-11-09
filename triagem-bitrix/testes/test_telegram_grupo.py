"""Testes da orquestração do Telegram (TDD) — cada CHAT é uma esteira própria.

O dono foi claro (24/09/2026): o bot é canal de ENVIO dos documentos (privado de cada
pessoa), não grupo. Então a janela de uma pessoa não pode ser fechada pelo `/start` de
outra: o agrupamento é POR CHAT.
"""
from conftest import RAIZ
import sys
from datetime import datetime, timezone

sys.path.insert(0, RAIZ)

import telegram_grupo as TG  # noqa: E402
import cpf_util  # noqa: E402
CPF_A_FMT = cpf_util.formatar(cpf_util.gerar_para_teste(1101))  # CPF sintético (DV válido)
CPF_B_FMT = cpf_util.formatar(cpf_util.gerar_para_teste(1102))  # CPF sintético (DV válido)
CPF_A = cpf_util.gerar_para_teste(1101)  # CPF sintético (DV válido)
CPF_B = cpf_util.gerar_para_teste(1102)  # CPF sintético (DV válido)


# formato REAL do config.json: allowlist aninhada em `telegram.chats_permitidos`
CFG = {"sinal_traco": ["-", "–", "—", "=", "_"], "traco_telegram": "/start",
       "modo_traco": "delimitador", "timeout_lote_min": 120,
       "janela_min_fallback": 30, "traco_no_fim_fecha": True,
       "telegram": {"habilitado": True, "chats_permitidos": ["111", "222"]}}


def msg(texto, chat, autor, user_id, msg_id):
    # data relativa a AGORA, sempre 5 min atrás (o timeout de 120 min é avaliado
    # contra 'agora'): com a base FIXA o teste passava de manhã e falhava depois
    # de 2h de relógio — armadilha que já derrubou a suíte em produção.
    base = int(datetime.now(timezone.utc).timestamp()) - 300
    return {"update_id": msg_id, "message": {
        "message_id": msg_id, "date": base + msg_id * 60,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": user_id, "first_name": autor}, "text": texto}}


def test_dois_chats_nao_interferem_um_no_outro():
    updates = [
        msg("/start", 111, "Karol", 1, 1),
        msg(f"= MARIA DA SILVA = CPF {CPF_A_FMT}", 111, "Karol", 1, 2),
        # outra pessoa manda o /start dela no MEIO — não pode fechar a janela da Karol
        msg("/start", 222, "Jessica", 2, 3),
        msg(f"= JOAO SOUZA = CPF {CPF_B_FMT}", 222, "Jessica", 2, 4),
        msg("/start", 111, "Karol", 1, 5),
    ]

    resumo = TG.processar(updates, CFG, simular=True)

    assert set(resumo["chats"]) == {"111", "222"}
    cpfs_por_chat = {}
    for reg in resumo["lotes"]:
        if reg.get("cpf"):
            cpfs_por_chat.setdefault(reg["dialog_id"], set()).add(reg["cpf"])
    assert cpfs_por_chat.get("tg:111") == {f"{CPF_A}"}
    assert cpfs_por_chat.get("tg:222") == {f"{CPF_B}"}


def test_janela_por_chat_fecha_quando_a_pessoa_manda_start_de_novo():
    updates = [
        msg("/start", 111, "Karol", 1, 1),
        msg(f"= MARIA = CPF {CPF_A_FMT}", 111, "Karol", 1, 2),
        msg("/start", 111, "Karol", 1, 3),
        msg(f"= JOAO = CPF {CPF_B_FMT}", 111, "Karol", 1, 4),
    ]

    resumo = TG.processar(updates, CFG, simular=True)
    por_cpf = {reg.get("cpf"): reg for reg in resumo["lotes"]}

    assert por_cpf[f"{CPF_A}"]["status"] == "FECHADO_PRONTO"
    assert "delimitad" in str(por_cpf[f"{CPF_A}"].get("fechado_por", "")).lower()
    assert por_cpf[f"{CPF_B}"]["status"] == "ABERTO"


def test_chat_fora_da_lista_e_ignorado():
    updates = [msg(f"= MARIA = CPF {CPF_A_FMT}", 999, "Estranho", 9, 1)]

    resumo = TG.processar(updates, {**CFG, "telegram": {"habilitado": True,
                                                        "chats_permitidos": ["111"]}},
                          simular=True)

    assert resumo["lotes"] == []
    assert resumo["ignorados"] == 1


def test_lote_do_telegram_guarda_a_origem():
    updates = [msg("/start", 111, "Karol", 1, 1),
               msg(f"= MARIA = CPF {CPF_A_FMT}", 111, "Karol", 1, 2)]

    reg = TG.processar(updates, CFG, simular=True)["lotes"][0]

    assert reg["dialog_id"] == "tg:111"
    assert reg["fonte_entrada"] == "telegram"


def test_sem_anexo_nao_quebra():
    updates = [msg("/start", 111, "Karol", 1, 1), msg("bom dia", 111, "Karol", 1, 2)]

    resumo = TG.processar(updates, CFG, simular=True)

    assert all(not r.get("arquivos") for r in resumo["lotes"])


def test_update_sem_mensagem_e_ignorado():
    resumo = TG.processar([{"update_id": 1}], CFG, simular=True)

    assert resumo["lotes"] == [] and resumo["ignorados"] == 1
