# -*- coding: utf-8 -*-
"""Testes do ENVIO como bot em pergunta_traco.py — incidente 20/10/2026.

Medido em produção: o bot tentou avisar num grupo onde NÃO é membro
(ACCESS_DENIED no imbot.v2.Chat.Message.send), repetiu a MESMA chamada no laço
de 8s e o VibeCode bloqueou o método `imbot.v2.Chat.Message.send` para a API
key INTEIRA (ERROR_LOOP_DETECTED: "36 consecutive Bitrix24 refusals ... the
block clears after the first successful call"). Resultado: bot mudo por ~7h em
TODOS os grupos (inclusive no que ele é membro) e nenhum alerta no painel.

Regras que estes testes fixam:
 1) bloqueio ativo  -> NENHUMA chamada de rede (o bloqueio precisa de pausa
    crescente: 1, 5, 15, 60 min);
 2) ERROR_LOOP_DETECTED não é retentado: vira bloqueio com pausa;
 3) ACCESS_DENIED não é retentado (mesma request não vai passar);
 4) retry de RATE_LIMITED muda o texto (o Bitrix bloqueia texto repetido);
 5) grupo sem o bot -> não envia (é o ACCESS_DENIED que envenena a key);
 6) traço fora da janela -> não envia aviso atrasado, mas o estado avança
    (senão o mesmo traço é retentado para sempre).
"""
import os
from conftest import RAIZ
import importlib.util
import json
import shutil
import tempfile
import time
import unittest
from unittest import mock

_CAMINHO = os.path.join(RAIZ, "pergunta_traco.py")


def _carregar_modulo():
    spec = importlib.util.spec_from_file_location("pergunta_traco_t", _CAMINHO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LOOP_OK = ("HTTP 429 em https://vibecode.bitrix24.com/v1/bots/100003/messages: "
            '{"success":false,"error":{"code":"ERROR_LOOP_DETECTED","message":'
            '"Repeating this request will not help: the same call received 36 '
            'consecutive Bitrix24 refusals (ACCESS_DENIED) on imbot.v2.Chat.Message.send.",'
            '"hint":"Recovery probes require both request cadence and a growing pause",'
            '"retryAfter":60}}')
_RATE = ('HTTP 429 em https://vibecode.bitrix24.com/v1/bots/100003/messages: '
         '{"success":false,"error":{"code":"RATE_LIMITED","message":"Too many requests"}}')
_DENIED = ('HTTP 403 em https://vibecode.bitrix24.com/v1/bots/100003/messages: '
           '{"success":false,"error":{"code":"BITRIX_ACCESS_DENIED","message":"ACCESS_DENIED"}}')


class TestEnvioBot(unittest.TestCase):
    def setUp(self):
        self.mod = _carregar_modulo()
        self.tmp = tempfile.mkdtemp(prefix="traco-teste-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bloq = os.path.join(self.tmp, "bot-bloqueio.json")
        self.saude = os.path.join(self.tmp, "saude-bot.json")
        self.estado = os.path.join(self.tmp, "estado.json")
        self.mod.ARQ_BLOQUEIO = self.bloq
        self.mod.ARQ_SAUDE = self.saude
        self.mod.ARQ_ESTADO = self.estado

    # ── 1. bloqueio ativo = nenhuma chamada de rede ──────────────────────
    def test_bloqueio_ativo_nao_chama_api(self):
        with open(self.bloq, "w", encoding="utf-8") as f:
            json.dump({"ate": time.time() + 600, "motivo": "ERROR_LOOP_DETECTED"}, f)
        with mock.patch.object(self.mod, "_req") as req:
            with self.assertRaises(RuntimeError):
                self.mod.bot_fala("k", 100003, "chat1", "oi")
            self.assertEqual(req.call_count, 0,
                             "com bloqueio ativo não pode existir chamada de rede")

    # ── 2. ERROR_LOOP_DETECTED -> bloqueio + sem retry ────────────────────
    def test_loop_detected_grava_bloqueio_e_nao_retenta(self):
        with mock.patch.object(self.mod, "_req", side_effect=RuntimeError(_LOOP_OK)) as req:
            with self.assertRaises(RuntimeError):
                self.mod.bot_fala("k", 100003, "chat1", "oi")
            self.assertEqual(req.call_count, 1, "não pode retentar ERROR_LOOP_DETECTED")
        self.assertTrue(os.path.exists(self.bloq), "o bloqueio tem que ficar gravado")
        with open(self.bloq, encoding="utf-8") as f:
            b = json.load(f)
        self.assertGreater(b["ate"], time.time() + 60)

    # ── 3. ACCESS_DENIED -> sem retry ─────────────────────────────────────
    def test_access_denied_nao_retenta(self):
        with mock.patch.object(self.mod, "_req", side_effect=RuntimeError(_DENIED)) as req:
            with self.assertRaises(RuntimeError):
                self.mod.bot_fala("k", 100003, "chat1", "oi")
            self.assertEqual(req.call_count, 1, "não pode retentar ACCESS_DENIED")

    # ── 4. retry de RATE_LIMITED muda o texto ─────────────────────────────
    def test_retry_de_rate_limit_varia_o_texto(self):
        enviados = []

        def falso_req(url, chave, corpo=None):
            enviados.append(corpo["fields"]["message"])
            if len(enviados) == 1:
                raise RuntimeError(_RATE)
            return {"success": True}

        with mock.patch.object(self.mod, "_req", side_effect=falso_req), \
                mock.patch.object(self.mod.time, "sleep"):
            self.mod.bot_fala("k", 100003, "chat1", "aviso do lote")
        self.assertEqual(len(enviados), 2)
        self.assertNotEqual(enviados[0], enviados[1],
                            "texto idêntico repetido é o que dispara o bloqueio do Bitrix")

    # ── 5. grupo SEM o bot -> não envia ───────────────────────────────────
    def _estado_base(self, chat):
        with open(self.estado, "w", encoding="utf-8") as f:
            json.dump({"perguntas": {}, "testes": [],
                       "corte_id": {chat: 1000}, "ultimo_traco": {chat: 1000},
                       "aberto": {}}, f)

    def _rodada(self, chat, msgs, membros, agora_ts):
        self._estado_base(chat)
        with mock.patch.object(self.mod.tg, "buscar_mensagens_api", return_value=msgs), \
                mock.patch.object(self.mod, "membros_do_chat", return_value=membros), \
                mock.patch.object(self.mod, "_req", return_value={}), \
                mock.patch.object(self.mod, "bot_fala") as fala:
            self.mod.rodada({"timeout_lote_min": 120}, "k", chat, 100003, True)
        return fala

    def test_grupo_sem_o_bot_nao_envia(self):
        chat = "chatX"
        msgs = [{"id": 1001, "texto": "—", "autor": "Maria",
                 "ts": "2026-10-20T09:00:00-03:00", "sinal": True, "file_ids": []}]
        # membros SEM o id do bot (100003)
        fala = self._rodada(chat, msgs, (set(), {"1": "Operador"}), None)
        fala.assert_not_called()
        with open(self.estado, encoding="utf-8") as f:
            est = json.load(f)
        self.assertEqual(est["ultimo_traco"][chat], 1001,
                         "o traço tem que ser marcado (senão tenta de novo a cada 8s)")

    # ── 6. traço velho -> não envia, mas avança ───────────────────────────
    def test_traco_fora_da_janela_nao_envia_aviso_atrasado(self):
        chat = "chatY"
        antigo = "2026-10-20T03:00:00-03:00"     # horas atrás
        msgs = [{"id": 2002, "texto": "—", "autor": "Joao",
                 "ts": antigo, "sinal": True, "file_ids": []}]
        fala = self._rodada(chat, msgs, (set(), {"100003": "Triagem de Documentos Bot"}),
                            antigo)
        fala.assert_not_called()
        with open(self.estado, encoding="utf-8") as f:
            est = json.load(f)
        self.assertEqual(est["ultimo_traco"][chat], 2002)


if __name__ == "__main__":
    unittest.main(verbosity=2)
