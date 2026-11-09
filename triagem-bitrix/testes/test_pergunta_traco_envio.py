# -*- coding: utf-8 -*-
"""Envio do bot do traço (100003) sem reenvio cego — achados PT-01..PT-04.

Defeitos que estes testes reproduzem (todos falhavam no código original):
 - PT-01: timeout DEPOIS do POST (a mensagem pode ter saído) caía no
   `except Exception` da rodada, o traço não avançava e a rodada seguinte (8 s
   depois) mandava de novo — até ~300 vezes na janela de 45 min;
 - PT-01: com o disco cheio, a rodada enviava, não conseguia gravar o estado e
   reenviava na rodada seguinte (loop de 8 s);
 - PT-01: ERROR_LOOP_DETECTED com falha ao gravar `bot-bloqueio.json` não
   pausava nada (a próxima chamada ia para a rede);
 - PT-01: ACCESS_DENIED num chat era repetido a cada traço novo do mesmo chat;
 - PT-02: a trava de instância só existia no `--loop`;
 - PT-03: `ignorar_autores` comparava o NOME exibido;
 - PT-04: o disparo da esteira jogava stdout/stderr no DEVNULL.
Nenhum teste faz rede: `urlopen` é substituído (e o conftest bloqueia socket).
"""
import datetime as _dt
import fcntl
import io
import json
import os
import socket
import stat
import subprocess
import sys
import shutil
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from conftest import RAIZ
import importlib.util

_CAMINHO = os.path.join(RAIZ, "pergunta_traco.py")
BOT = 100003
CHAT = "chatTeste1"


def _carregar_modulo():
    spec = importlib.util.spec_from_file_location("pergunta_traco_envio_t", _CAMINHO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _agora_iso(delta_min=0):
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=delta_min)).isoformat()


class _Resp:
    def __init__(self, corpo):
        self._b = json.dumps(corpo).encode()
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RedeFalsa:
    """Substitui urllib.request.urlopen. `modo_post` decide o destino do POST."""

    def __init__(self, modo_post="timeout"):
        self.modo_post = modo_post
        self.posts = []
        self.chamadas = 0

    def __call__(self, req, timeout=None):
        self.chamadas += 1
        url = req.full_url
        if req.get_method() == "POST" and "/messages" in url:
            corpo = json.loads(req.data.decode())
            self.posts.append(corpo)
            modo = self.modo_post(len(self.posts)) if callable(self.modo_post) else self.modo_post
            if modo == "timeout":
                raise socket.timeout("timed out")
            if modo == "urlerror":
                raise urllib.error.URLError("reset by peer")
            if modo == "denied":
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", None,
                    io.BytesIO(b'{"success":false,"error":{"code":"BITRIX_ACCESS_DENIED"}}'))
            return _Resp({"success": True, "data": {"id": 99000 + len(self.posts)}})
        # GET /v1/chats/<chat> (tipo do chat) e qualquer leitura
        return _Resp({"data": {"type": "chat"}})


def _msg_traco(mid, autor="Pessoa Ficticia", autor_id="777", ts=None):
    return {"id": mid, "texto": "—", "autor": autor, "autor_id": autor_id,
            "ts": ts or _agora_iso(-1), "sinal": True, "file_ids": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.mod = _carregar_modulo()
        self.tmp = tempfile.mkdtemp(prefix="traco-envio-")
        # roda depois do tearDown (que devolve a permissão de dir_estado)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir_estado = os.path.join(self.tmp, "est")
        os.makedirs(self.dir_estado)
        self.estado = os.path.join(self.dir_estado, "pergunta-traco-estado.json")
        self.mod.ARQ_ESTADO = self.estado
        self.mod.ARQ_BLOQUEIO = os.path.join(self.tmp, "bot-bloqueio.json")
        self.mod.ARQ_SAUDE = os.path.join(self.tmp, "saude-bot.json")
        self.mod.ARQ_LOCK = os.path.join(self.tmp, "pergunta-traco.lock")
        self.mod.ARQ_DISPARO_LOG = os.path.join(self.tmp, "pergunta-disparo.log")
        self.membros = (set(), {str(BOT): "Triagem de Documentos Bot", "777": "Pessoa Ficticia"})

    def tearDown(self):
        os.chmod(self.dir_estado, 0o700)

    def _estado_inicial(self, chat=CHAT, extra=None):
        est = {"perguntas": {}, "testes": [], "corte_id": {chat: 1000},
               "ultimo_traco": {chat: 1000}, "aberto": {}}
        est.update(extra or {})
        with open(self.estado, "w", encoding="utf-8") as f:
            json.dump(est, f)

    def _ler_estado(self):
        with open(self.estado, encoding="utf-8") as f:
            return json.load(f)

    def _rodada(self, msgs, rede, conf=None, chat=CHAT, disparar=None):
        conf = conf or {"timeout_lote_min": 120}
        with mock.patch.object(self.mod.tg, "buscar_mensagens_api", return_value=list(msgs)), \
                mock.patch.object(self.mod, "membros_do_chat", return_value=self.membros), \
                mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=rede), \
                mock.patch.object(self.mod.time, "sleep"):
            return self.mod.rodada(conf, "k", chat, BOT, True, disparar=disparar)


# ───────────────────────────── PT-01: envio incerto ─────────────────────────────
class TestEnvioIncerto(Base):
    def test_timeout_depois_do_envio_nao_reenvia_na_rodada_seguinte(self):
        self._estado_inicial()
        rede = _RedeFalsa("timeout")
        msgs = [_msg_traco(1001)]
        for _ in range(4):                       # 4 rodadas do loop de 8 s
            try:
                self._rodada(msgs, rede)
            except Exception:                    # noqa: BLE001 — o loop real engole
                pass
        self.assertEqual(len(rede.posts), 1,
                         "timeout depois do POST não pode virar reenvio automático")
        reg = self._ler_estado()["envios"][f"{CHAT}:1001"]
        self.assertEqual(reg["estado"], "ENVIO_INCERTO")
        self.assertEqual(self._ler_estado()["ultimo_traco"][CHAT], 1001)

    def test_urlerror_tambem_e_incerto(self):
        self._estado_inicial()
        rede = _RedeFalsa("urlerror")
        for _ in range(3):
            self._rodada([_msg_traco(1001)], rede)
        self.assertEqual(len(rede.posts), 1)

    def test_incerto_confirmado_pela_reconciliacao(self):
        self._estado_inicial()
        rede = _RedeFalsa("timeout")
        self._rodada([_msg_traco(1001)], rede)
        # rodada seguinte: a mensagem do bot APARECE no grupo (o POST tinha saído)
        msgs = [_msg_traco(1001),
                {"id": 1002, "texto": "Lote aberto", "autor": "Triagem", "autor_id": str(BOT),
                 "ts": _agora_iso(), "file_ids": []}]
        self._rodada(msgs, rede)
        reg = self._ler_estado()["envios"][f"{CHAT}:1001"]
        self.assertEqual(reg["estado"], "CONFIRMADO")
        self.assertTrue(reg.get("reconciliado"))
        self.assertEqual(len(rede.posts), 1)

    def test_incerto_reenvia_no_maximo_uma_vez_depois_da_janela(self):
        self._estado_inicial()
        rede = _RedeFalsa("timeout")
        msgs = [_msg_traco(1001)]
        self._rodada(msgs, rede)
        # envelhece a incerteza além da janela longa (reconciliação: msg não existe)
        est = self._ler_estado()
        est["envios"][f"{CHAT}:1001"]["incerto_desde"] = time.time() - 2 * self.mod.JANELA_REENVIO_INCERTO_S
        with open(self.estado, "w", encoding="utf-8") as f:
            json.dump(est, f)
        self._rodada(msgs, rede)
        self.assertEqual(len(rede.posts), 2, "depois da janela: UM reenvio")
        self.assertNotEqual(rede.posts[0]["fields"]["message"],
                            rede.posts[1]["fields"]["message"],
                            "reenvio com texto variado (Bitrix bloqueia texto repetido)")
        est = self._ler_estado()
        est["envios"][f"{CHAT}:1001"]["incerto_desde"] = time.time() - 2 * self.mod.JANELA_REENVIO_INCERTO_S
        with open(self.estado, "w", encoding="utf-8") as f:
            json.dump(est, f)
        for _ in range(3):
            self._rodada(msgs, rede)
        self.assertEqual(len(rede.posts), 2, "no máximo 1 reenvio por traço")

    def test_sucesso_confirma_e_guarda_id(self):
        self._estado_inicial()
        rede = _RedeFalsa("ok")
        disparos = []
        self._rodada([_msg_traco(1001)], rede,
                     disparar=lambda chat, motivo="": disparos.append(chat))
        reg = self._ler_estado()["envios"][f"{CHAT}:1001"]
        self.assertEqual(reg["estado"], "CONFIRMADO")
        self.assertEqual(reg["id_msg"], "99001")
        self.assertEqual(disparos, [CHAT])
        self._rodada([_msg_traco(1001)], rede)
        self.assertEqual(len(rede.posts), 1)


# ──────────────────────── PT-01: falha ao gravar o estado ───────────────────────
class TestFalhaGravacao(Base):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignora chmod")
    def test_disco_cheio_nao_envia_e_nao_entra_em_loop(self):
        self._estado_inicial()
        os.chmod(self.dir_estado, stat.S_IRUSR | stat.S_IXUSR)   # gravação falha
        rede = _RedeFalsa("ok")
        for _ in range(5):
            try:
                self._rodada([_msg_traco(1001)], rede)
            except Exception:                    # noqa: BLE001
                pass
        self.assertEqual(len(rede.posts), 0,
                         "sem conseguir gravar o PENDENTE, nada pode ser enviado")
        # e as rodadas seguintes dentro da pausa nem chegam à rede
        chamadas = rede.chamadas
        self._rodada([_msg_traco(1001)], rede)
        self.assertEqual(rede.chamadas, chamadas, "pausa de gravação: nenhuma chamada de rede")

    def test_falha_ao_gravar_depois_do_envio_nao_reenvia(self):
        self._estado_inicial()
        rede = _RedeFalsa("ok")
        real = self.mod.salvar
        n = {"c": 0}

        def salvar_falha_na_segunda(est):
            n["c"] += 1
            if n["c"] == 2:
                raise OSError(28, "No space left on device")
            return real(est)

        with mock.patch.object(self.mod, "salvar", side_effect=salvar_falha_na_segunda):
            with self.assertRaises(Exception):
                self._rodada([_msg_traco(1001)], rede)
        self.assertEqual(len(rede.posts), 1)
        self.mod._PAUSA_GRAVACAO["ate"] = 0.0          # a pausa passou
        for _ in range(3):
            self._rodada([_msg_traco(1001)], rede)
        self.assertEqual(len(rede.posts), 1, "PENDENTE no disco = talvez entregue: não reenvia")
        reg = self._ler_estado()["envios"][f"{CHAT}:1001"]
        self.assertEqual(reg["estado"], "ENVIO_INCERTO")


# ─────────────────────── PT-01: bloqueio e ACCESS_DENIED ────────────────────────
_LOOP = ('HTTP 429 em https://vibecode.bitrix24.com/v1/bots/100003/messages: '
         '{"success":false,"error":{"code":"ERROR_LOOP_DETECTED","retryAfter":60}}')


class TestBloqueio(Base):
    def test_loop_detected_pausa_mesmo_sem_conseguir_gravar_o_bloqueio(self):
        self.mod.ARQ_BLOQUEIO = os.path.join(self.tmp, "nao-existe", "sub", "bot-bloqueio.json")
        with mock.patch.object(self.mod, "_req", side_effect=RuntimeError(_LOOP)) as req:
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    self.mod.bot_fala("k", BOT, CHAT, "oi")
        self.assertEqual(req.call_count, 1, "bloqueio vale em memória se o arquivo não grava")

    def test_bloqueio_existente_em_arquivo_e_respeitado_na_rodada(self):
        self._estado_inicial()
        with open(self.mod.ARQ_BLOQUEIO, "w", encoding="utf-8") as f:
            json.dump({"ate": time.time() + 600, "motivo": "ERROR_LOOP_DETECTED"}, f)
        rede = _RedeFalsa("ok")
        for _ in range(3):
            self._rodada([_msg_traco(1001)], rede)
        self.assertEqual(len(rede.posts), 0)
        self.assertEqual(self._ler_estado()["envios"][f"{CHAT}:1001"]["estado"], "ADIADO")

    def test_access_denied_nunca_repetido_nem_em_traco_novo(self):
        self._estado_inicial()
        rede = _RedeFalsa("denied")
        self._rodada([_msg_traco(1001)], rede)
        self._rodada([_msg_traco(1001), _msg_traco(1003)], rede)
        self._rodada([_msg_traco(1001), _msg_traco(1003)], rede)
        self.assertEqual(len(rede.posts), 1, "ACCESS_DENIED não pode ser repetido")
        self.assertEqual(self._ler_estado()["envios"][f"{CHAT}:1001"]["estado"], "FALHA")


# ───────────────────────────── PT-02: instância única ───────────────────────────
class TestInstanciaUnica(Base):
    def _main(self, argv):
        # (C3) carregar_config sempre traz modo_traco (padrão 'delimitador'); o
        # bot recusa (código 3) modo diferente do que ele implementa.
        conf = {"modo_traco": "delimitador",
                "pergunta_traco": {"chats": [CHAT], "bot_id": BOT}}
        with mock.patch.object(sys, "argv", ["pergunta_traco.py"] + argv), \
                mock.patch.object(self.mod.tg, "carregar_config", return_value=conf), \
                mock.patch.object(self.mod.tg, "ler_chave_bot", return_value="k"), \
                mock.patch.object(self.mod, "rodada") as rodada, \
                mock.patch.object(self.mod, "bot_fala") as fala, \
                mock.patch.object(self.mod.urllib.request, "urlopen") as rede:
            codigo = self.mod.main()
        return codigo, rodada, fala, rede

    def _segurar_trava(self):
        fd = os.open(self.mod.ARQ_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)

    def test_segunda_instancia_rodada_unica_sai_nao_zero_sem_enviar(self):
        self._segurar_trava()
        codigo, rodada, fala, rede = self._main(["--ativo"])
        self.assertNotEqual(codigo, 0)
        rodada.assert_not_called()
        fala.assert_not_called()
        rede.assert_not_called()

    def test_segunda_instancia_loop_sai_nao_zero(self):
        self._segurar_trava()
        codigo, rodada, fala, _ = self._main(["--ativo", "--loop", "8"])
        self.assertNotEqual(codigo, 0)
        rodada.assert_not_called()

    def test_segunda_instancia_testar_nao_envia(self):
        self._segurar_trava()
        codigo, _, fala, _ = self._main(["--testar"])
        self.assertNotEqual(codigo, 0)
        fala.assert_not_called()

    def test_sem_concorrente_roda_normal(self):
        codigo, rodada, _, _ = self._main(["--ativo"])
        self.assertEqual(codigo, 0)
        self.assertEqual(rodada.call_count, 1)

    def test_trava_usa_caminho_da_base(self):
        import caminhos
        mod = _carregar_modulo()
        self.assertEqual(mod.ARQ_LOCK, caminhos.em_base("pergunta-traco.lock"))


# ───────────────────────────── PT-03: autor por ID ─────────────────────────────
class TestAutorPorId(Base):
    def test_pessoa_com_nome_igual_ao_do_bot_recebe_resposta(self):
        self._estado_inicial()
        rede = _RedeFalsa("ok")
        conf = {"timeout_lote_min": 120,
                "pergunta_traco": {"ignorar_autores": ["Triagem de Documentos Bot"]}}
        self._rodada([_msg_traco(1001, autor="Triagem de Documentos Bot", autor_id="777")],
                     rede, conf=conf)
        self.assertEqual(len(rede.posts), 1)

    def test_traco_do_proprio_bot_renomeado_e_ignorado(self):
        self._estado_inicial()
        rede = _RedeFalsa("ok")
        self._rodada([_msg_traco(1001, autor="Nome Novo do Bot", autor_id=str(BOT))], rede)
        self.assertEqual(len(rede.posts), 0)

    def test_ignorar_ids_do_config(self):
        self._estado_inicial()
        rede = _RedeFalsa("ok")
        conf = {"timeout_lote_min": 120, "pergunta_traco": {"ignorar_ids": [555]}}
        self._rodada([_msg_traco(1001, autor="Conector", autor_id="555")], rede, conf=conf)
        self.assertEqual(len(rede.posts), 0)


# ─────────────────────── PT-03/04: log sem dado pessoal ────────────────────────
class TestLogEDisparo(Base):
    def test_log_da_rodada_nao_traz_nome_da_pessoa(self):
        self._estado_inicial()
        rede = _RedeFalsa("ok")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self._rodada([_msg_traco(1001, autor="Maria Ficticia Souza")], rede)
        self.assertNotIn("Maria Ficticia", buf.getvalue())
        self.assertIn("autor_id=777", buf.getvalue())

    def test_disparo_da_esteira_vai_para_log(self):
        disparar = self.mod.criar_disparador(True)
        with mock.patch.object(self.mod.subprocess, "Popen") as popen:
            disparar(CHAT, motivo="traço 1001")
        kw = popen.call_args.kwargs
        self.assertIsNot(kw["stdout"], subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], subprocess.STDOUT)
        self.assertTrue(os.path.exists(self.mod.ARQ_DISPARO_LOG))


# ─────────────────── reconciliação: limite e modo rodada única ───────────────────
class TestReconciliacaoLimites(Base):
    def test_msg_do_bot_depois_do_proximo_traco_nao_confirma(self):
        self._estado_inicial()
        rede = _RedeFalsa("timeout")
        self._rodada([_msg_traco(1001)], rede)
        # a única msg do bot vem DEPOIS de um novo traço (1003): é a resposta ao 1003
        msgs = [_msg_traco(1001), _msg_traco(1003),
                {"id": 1004, "texto": "Lote", "autor": "Bot", "autor_id": str(BOT),
                 "ts": _agora_iso(), "file_ids": []}]
        self._rodada(msgs, rede)
        envios = self._ler_estado()["envios"]
        self.assertEqual(envios[f"{CHAT}:1001"]["estado"], "ENVIO_INCERTO")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignora chmod")
    def test_rodada_unica_com_estado_que_nao_grava_sai_1(self):
        self._estado_inicial()
        os.chmod(self.dir_estado, stat.S_IRUSR | stat.S_IXUSR)
        conf = {"modo_traco": "delimitador",          # (C3) como carregar_config entrega
                "pergunta_traco": {"chats": [CHAT], "bot_id": BOT}, "timeout_lote_min": 120}
        rede = _RedeFalsa("ok")
        with mock.patch.object(sys, "argv", ["pergunta_traco.py", "--ativo", "--sem-disparar"]), \
                mock.patch.object(self.mod.tg, "carregar_config", return_value=conf), \
                mock.patch.object(self.mod.tg, "ler_chave_bot", return_value="k"), \
                mock.patch.object(self.mod.tg, "buscar_mensagens_api",
                                  return_value=[_msg_traco(1001)]), \
                mock.patch.object(self.mod, "membros_do_chat", return_value=self.membros), \
                mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=rede):
            codigo = self.mod.main()
        self.assertEqual(codigo, 1)
        self.assertEqual(len(rede.posts), 0)


# ───────────────────── shell: código de saída real ao systemd ────────────────────
class TestShell(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="traco-sh-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_pergunta_rodar_propaga_codigo_do_python(self):
        with open(os.path.join(self.base, "pergunta_traco.py"), "w") as f:
            f.write("import sys\nprint('rodei', sys.argv[1:])\nsys.exit(75)\n")
        env = dict(os.environ, TRIAGEM_BASE=self.base, PYTHON=sys.executable)
        r = subprocess.run(["bash", os.path.join(RAIZ, "pergunta-rodar.sh")], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 75, r.stderr)
        with open(os.path.join(self.base, "pergunta.log")) as f:
            self.assertIn("--ativo", f.read())

    def _vigia(self, codigo_systemctl):
        bin_dir = os.path.join(self.base, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        falso = os.path.join(bin_dir, "systemctl")
        with open(falso, "w") as f:
            f.write(f"#!/bin/bash\nexit {codigo_systemctl}\n")
        os.chmod(falso, 0o755)
        log = os.path.join(self.base, "pergunta.log")
        with open(log, "w") as f:
            f.write("x\n")
        velho = time.time() - 3600
        os.utime(log, (velho, velho))
        env = dict(os.environ, TRIAGEM_BASE=self.base, PATH=bin_dir + os.pathsep + os.environ["PATH"])
        # `sleep 8` do script vira no-op (função exportada) para o teste não esperar
        return subprocess.run(["bash", "-c", f"sleep() {{ :; }}; export -f sleep; "
                               f"bash {os.path.join(RAIZ, 'vigia_bot.sh')}"],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_vigia_bot_restart_falhou_sai_nao_zero(self):
        r = self._vigia(1)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.base, "vigia-bot-ultimo.txt")),
                        "diagnóstico em $BASE, não em /tmp")

    def test_vigia_bot_restart_ok_sai_zero(self):
        r = self._vigia(0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ───────────────────── esperar_bot_liberar (H1, auditoria 03) ────────────────────
class TestEsperarBotLiberar(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "esperar_bot_liberar_t", os.path.join(RAIZ, "esperar_bot_liberar.py"))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.mkdtemp(prefix="esperar-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mod.ARQ_BLOQUEIO = os.path.join(self.tmp, "bot-bloqueio.json")
        self.mod.ARQ_CONFIG = os.path.join(self.tmp, "config.json")
        self.pausas = []

    def _main(self, argv, resultados):
        fila = list(resultados)
        with mock.patch.object(self.mod, "ler_chave", return_value="k"), \
                mock.patch.object(self.mod, "sondar", side_effect=lambda *a: fila.pop(0)) as s, \
                mock.patch.object(self.mod.subprocess, "run") as run:
            run.return_value.returncode = 0
            codigo = self.mod.main(argv, dormir=self.pausas.append)
        return codigo, s, run

    def test_importar_nao_le_chave_nem_envia(self):
        self.assertFalse(hasattr(self.mod, "K"))

    def test_grupo_de_clientes_e_recusado(self):
        codigo, s, _ = self._main(["--dialog", "chat100001"], [])
        self.assertEqual(codigo, 2)
        s.assert_not_called()

    def test_sem_dialog_e_recusado(self):
        codigo, s, _ = self._main([], [])
        self.assertEqual(codigo, 2)
        s.assert_not_called()

    def test_access_denied_para_na_primeira(self):
        codigo, s, _ = self._main(["--dialog", "1"], [("acesso", "HTTP 403")] * 5)
        self.assertEqual(codigo, 3)
        self.assertEqual(s.call_count, 1)

    def test_pausa_crescente_e_libera(self):
        codigo, s, run = self._main(["--dialog", "1"],
                                    [("loop", ""), ("loop", ""), ("ok", "")])
        self.assertEqual(codigo, 0)
        self.assertEqual(self.pausas, [60, 300, 900])
        run.assert_called_once()

    def test_respeita_bloqueio_gravado_pelo_bot(self):
        with open(self.mod.ARQ_BLOQUEIO, "w") as f:
            json.dump({"ate": time.time() + 1800}, f)
        self._main(["--dialog", "1", "--sem-reiniciar"], [("ok", "")])
        self.assertGreater(self.pausas[0], 1700)


if __name__ == "__main__":
    unittest.main(verbosity=2)
