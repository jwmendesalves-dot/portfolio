# -*- coding: utf-8 -*-
"""Testes do vigia-bot-triagem.py.

Cobre o falso positivo de 16/10/2026: o vigia chama GET /bots/100003/events
para testar a saúde do bot, mas o PRÓPRIO bot de triagem mantém um long-poll
pendurado no MESMO endpoint. Quando os dois chamam juntos, a API devolve
HTTP 409 code=BOT_EVENTS_BUSY — que NÃO é falha: é o bot VIVO e ocupado.
Antes, isso virava "resposta inesperada" e alertava (exit 1) a cada ciclo.
"""
import os
from conftest import RAIZ
import importlib.util
import shutil
import tempfile
import unittest
from unittest import mock

_CAMINHO = os.path.join(RAIZ, "vigia-bot-triagem.py")


def _carregar_modulo():
    spec = importlib.util.spec_from_file_location("vigia_bot_triagem", _CAMINHO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestVigiaBotTriagem(unittest.TestCase):
    def setUp(self):
        self.mod = _carregar_modulo()
        # o contador do vigia NUNCA no arquivo real ao lado do script (06-testes:
        # rodar a suíte no servidor sobrescrevia o estado que o cron do vigia lê)
        tmp = tempfile.mkdtemp(prefix="vigia-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.mod.ARQ_ESTADO_VIGIA = os.path.join(tmp, ".vigia-estado.json")

    def _main_com_resposta(self, status, corpo):
        with mock.patch.object(self.mod, "_req", return_value=(status, corpo)):
            return self.mod.main()

    def test_200_eh_ok_silencioso(self):
        self.assertEqual(self._main_com_resposta(200, {"success": True}), 0)

    def test_bot_events_busy_409_eh_bot_vivo_e_nao_alerta(self):
        """CENÁRIO 14: BOT_EVENTS_BUSY (409) = long-poll ocupado = bot saudável."""
        r = self._main_com_resposta(409, {"error": {"code": "BOT_EVENTS_BUSY"}})
        self.assertEqual(r, 0, f"BOT_EVENTS_BUSY deve sair 0 (silencioso), veio {r}")

    def test_bot_access_denied_ainda_dispara_recuperacao(self):
        """Garante que o fix não engoliu o caso real de auto-recuperação."""
        chamadas = []

        def _req_fake(method, path, body=None):
            chamadas.append((method, path))
            if "transfer" in path:
                return 200, {"data": {"transferred": True}}
            if "reauth" in path:
                return 200, {"data": {"validated": True}}
            return 403, {"error": {"code": "BOT_ACCESS_DENIED",
                                   "fix": {"body": {"targetApiKeyId": "k-1"}}}}

        with mock.patch.object(self.mod, "_req", side_effect=_req_fake), \
                mock.patch.dict(os.environ, {"VIGIA_API_KEY_ID": "k-1"}):
            r = self.mod.main()
        self.assertEqual(r, 0)
        self.assertTrue(any("transfer" in p for _, p in chamadas),
                        "BOT_ACCESS_DENIED deve transferir + reauth")

    # ── H2 (auditoria 03): só transfere para a chave LOCAL ──────────────────
    def _negado_para(self, alvo, ambiente, me=None):
        chamadas = []

        def _req_fake(method, path, body=None):
            chamadas.append((method, path))
            if path == "/me":
                return me or (404, {})
            if "transfer" in path:
                return 200, {"data": {"transferred": True}}
            if "reauth" in path:
                return 200, {"data": {"validated": True}}
            return 403, {"error": {"code": "BOT_ACCESS_DENIED",
                                   "fix": {"body": {"targetApiKeyId": alvo}}}}

        limpo = {k: v for k, v in os.environ.items()
                 if k not in ("VIGIA_API_KEY_ID", "TRIAGEM_VIBECODE_KEY_ID")}
        limpo.update(ambiente)
        self.mod.ARQ_ID_CHAVE = os.path.join(os.path.dirname(__file__), "nao-existe.id")
        with mock.patch.object(self.mod, "_req", side_effect=_req_fake), \
                mock.patch.dict(os.environ, limpo, clear=True):
            r = self.mod.main()
        return r, chamadas

    def test_target_de_outra_chave_nao_transfere(self):
        r, chamadas = self._negado_para("chave-do-atacante", {"VIGIA_API_KEY_ID": "k-1"})
        self.assertEqual(r, 1)
        self.assertFalse(any("transfer" in p for _, p in chamadas))

    def test_sem_id_local_nao_transfere(self):
        r, chamadas = self._negado_para("k-1", {})
        self.assertEqual(r, 1)
        self.assertFalse(any("transfer" in p for _, p in chamadas))

    def test_id_local_lido_de_v1_me(self):
        r, chamadas = self._negado_para("k-9", {}, me=(200, {"data": {"apiKeyId": "k-9"}}))
        self.assertEqual(r, 0)
        self.assertTrue(any("transfer" in p for _, p in chamadas))

    def test_chave_vem_da_base_e_nao_de_opt(self):
        import caminhos
        self.assertEqual(self.mod.CHAVE_FILE, caminhos.em_base("vibecode.key"))

    def test_codigo_desconhecido_continua_alertando(self):
        r = self._main_com_resposta(500, {"error": {"code": "ALGO_QUEBRADO"}})
        self.assertEqual(r, 1)


if __name__ == "__main__":
    unittest.main()
