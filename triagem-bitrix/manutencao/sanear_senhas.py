#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Saneamento do HISTÓRICO de senhas gov.br/Meu INSS (S01/S05) — ferramenta manual.

O código novo (C1) não grava mais a senha no estado; mas o histórico anterior
continua em `estado.json`, nos `estado.json.bak-*` (~1.833), nos
`backups/fila-jobs-*.json`, na `fila-jobs.json` do painel e em `/tmp/_entradas.json`.
Esta ferramenta remove a senha DESSE histórico, no servidor, com autorização —
sempre depois de um inventário.

Modos (o padrão é o inventário, SOMENTE LEITURA):
    python3 manutencao/sanear_senhas.py --inventario [--json]   # só contagens
    python3 manutencao/sanear_senhas.py --aplicar               # simulação (nada grava)
    python3 manutencao/sanear_senhas.py --aplicar --confirmar   # saneia (os 2 obrigatórios)

Alvos (padrões via caminhos.em_base/em_separador; explícito substitui o padrão):
    --estado <arq>       estado.json (padrão: $ESTADO_JSON ou <BASE>/estado.json)
    --backups-dir <dir>  diretório de backups (REPETÍVEL; padrão: <BASE>/backups-estado,
                         <BASE>/backups e o diretório do estado — onde ficam os
                         `estado.json.bak-*` legados). Cada diretório contribui com
                         `*.bak-*` e `fila-jobs-*.json`
    --fila-jobs <arq>    fila-jobs.json DO PAINEL (padrão: <SEPARADOR>/data/fila-jobs.json)
    --extra <arq>        arquivo JSON a mais (REPETÍVEL; ex.: /tmp/_entradas.json)

Gravação (só com --aplicar --confirmar):
    * estado.json: `estado_store.atualizar_estado` (flock + gravação atômica),
      aplicando `seguranca.remover_senhas` em todo campo de texto de
      `lotes[*].mensagens[*]` (estrutura real de `triagem_grupo.serializar_lote`);
    * demais JSONs: cópia saneada atômica (tmp exclusivo 0600 + fsync + os.replace),
      preservando a estrutura JSON; arquivos reescritos ficam 0600;
    * backups SÓ com `--sanear-backups --ciente-evidencia` (nada é apagado aqui:
      cada backup é reescrito saneado mantendo nome e mtime). O DESCARTE (poda) é
      OUTRA ferramenta — `estado_store.podar_backups` (estado) e
      `lote_para_fila.podar_backups_fila` (fila) — e deve respeitar a preservação
      de evidência de incidente LGPD (ver AVISO_EVIDENCIA);
    * fila do painel SÓ com `--incluir-fila-painel`, sob `estado_store.trava_arquivo`
      (flock em `<arq>.lock`). O painel (Node) escreve esse arquivo ao vivo e a
      trava dele NÃO está nesta cópia do agente-separador: a compatibilidade é
      "conferir no servidor" (ou rode com o painel/worker parados).

Saída: só CONTAGENS por arquivo (registros/mensagens com senha, tamanho, permissão
octal, dono) + a ação tomada. NUNCA imprime trecho de texto, nome, CPF ou senha.

Códigos de saída:
    0  ok (inventário limpo, ou aplicação concluída sem erro de leitura)
    1  a varredura ENCONTROU senha e nada foi gravado (inventário/simulação)
    2  erro de leitura/JSON inválido em algum alvo, ou recusa de segurança
       (--sanear-backups sem --ciente-evidencia, --confirmar sem --aplicar)

Idempotente: a 2ª execução não muda nada e reporta 0. Arquivo JSON inválido é
pulado com aviso (código 2 no fim), sem parar os demais alvos.

Detector de senha: `seguranca.remover_senhas(texto) != texto` (o mesmo padrão do
grupo/fichinha/tabela do contrato, do teste test_seguranca.py).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import sys
import uuid

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import caminhos  # noqa: E402
import estado_store  # noqa: E402
import seguranca  # noqa: E402

CODIGO_OK = 0
CODIGO_SENHA = 1
CODIGO_ERRO = 2

MARCADOR = seguranca.SENHA_REMOVIDA
_RX_FILA_BACKUP = re.compile(r"^fila-jobs-.*\.json$")

AVISO_EVIDENCIA = (
    "AVISO LGPD / EVIDÊNCIA DE INCIDENTE (S01):\n"
    "  Reescrever os backups DESTRÓI a evidência original do incidente de vazamento\n"
    "  de senha gov.br/INSS. Se houver (ou puder haver) investigação de incidente,\n"
    "  RIPD ou comunicação à ANPD, PRESERVE uma cópia íntegra (imagem/hash) dos\n"
    "  arquivos originais ANTES de sanear — por exemplo:\n"
    "      sha256sum <arquivos> > evidencia-s01-<data>.sha256\n"
    "  e guarde a cópia fora do disco de dados, com controle de acesso.\n"
    "  O DESCARTE dos backups é OUTRA ferramenta e NÃO faz parte desta:\n"
    "  estado_store.podar_backups (estado) e lote_para_fila.podar_backups_fila (fila).\n"
    "  Sanear backups exige --ciente-evidencia para confirmar que a preservação de\n"
    "  evidência foi avaliada."
)

MOTIVO_BACKUPS = "backups só com --sanear-backups --ciente-evidencia (evidência LGPD)"
MOTIVO_FILA_PAINEL = ("fila do painel só com --incluir-fila-painel "
                      "(arquivo de outro sistema, consumido ao vivo)")


# ---------------------------------------------------------------------------
# utilitários de arquivo
# ---------------------------------------------------------------------------
def _permissao(st: os.stat_result) -> str:
    return "%04o" % stat.S_IMODE(st.st_mode)


def _dono(st: os.stat_result):
    """(nome, uid). Nunca falha; não há dado sensível aqui (é o dono do arquivo)."""
    nome = "?"
    try:
        import pwd
        nome = pwd.getpwuid(st.st_uid).pw_name
    except Exception:
        pass
    return nome, st.st_uid


def _ler_json(caminho: str):
    """(dados, erro). `erro` é curto e NUNCA traz conteúdo do arquivo."""
    try:
        with open(caminho, "rb") as fh:
            bruto = fh.read()
    except OSError as e:
        return None, "leitura:" + type(e).__name__
    try:
        return json.loads(bruto), None
    except (UnicodeDecodeError, ValueError) as e:
        return None, "json_invalido:" + type(e).__name__


def _escrever_atomico(caminho: str, dados: bytes) -> None:
    """tmp exclusivo 0600 + fsync + os.replace + fsync do diretório.

    Mesmo mecanismo de `estado_store._gravar_bytes`; existe aqui porque
    `estado_store.gravar_atomico` só aceita raiz dict e a fila do painel é lista.
    """
    caminho = os.path.abspath(caminho)
    diretorio = os.path.dirname(caminho)
    tmp = "%s.sanear-tmp.%d.%s" % (caminho, os.getpid(), uuid.uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            vista = memoryview(dados)
            while vista:
                n = os.write(fd, vista)
                vista = vista[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, caminho)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        fd = os.open(diretorio, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# detecção/saneamento
# ---------------------------------------------------------------------------
def _sanear_generico(valor):
    """(valor_saneado, n_campos_afetados) — recursivo; preserva a estrutura JSON.

    Aplica `seguranca.remover_senhas` em TODA string (o padrão do contrato é
    idempotente e não mexe em prosa/nome/CPF). Usado para os JSONs que não são o
    estado (fila, backups, extras), onde a senha aparece em qualquer campo de
    texto (fichinha, histórico, avisos...).
    """
    if isinstance(valor, str):
        novo = seguranca.remover_senhas(valor)
        return novo, (1 if novo != valor else 0)
    if isinstance(valor, list):
        total = 0
        saida = []
        for item in valor:
            novo, n = _sanear_generico(item)
            saida.append(novo)
            total += n
        return saida, total
    if isinstance(valor, dict):
        total = 0
        saida = {}
        for chave, item in valor.items():
            novo, n = _sanear_generico(item)
            saida[chave] = novo
            total += n
        return saida, total
    return valor, 0


def _registros_de(dados) -> list:
    """Unidades de contagem de 1º nível: lotes (estado), itens de fila (lista) etc."""
    if isinstance(dados, dict) and isinstance(dados.get("lotes"), dict):
        return list(dados["lotes"].values())
    if isinstance(dados, list):
        return list(dados)
    if isinstance(dados, dict):
        return list(dados.values())
    return [dados]


def _contar_generico(dados):
    """(registros, registros_com_senha, itens_com_senha) sem alterar nada.

    `itens` = campos de texto afetados.
    """
    regs = rcs = itens = 0
    for registro in _registros_de(dados):
        _novo, n = _sanear_generico(registro)
        regs += 1
        if n:
            rcs += 1
            itens += n
    return regs, rcs, itens


def _saneia_mensagens_do_estado(estado, aplicar: bool = True):
    """`remover_senhas` em todo campo de texto de `lotes[*].mensagens[*]`.

    Escopo do estado, casado com o que `triagem_grupo.serializar_lote` grava
    (mensagens com id/autor/data/sinal/texto/realocada). Devolve
    (registros_afetados, itens_afetados); `itens` = MENSAGENS afetadas. Com
    `aplicar=True` mutaciona `estado` no lugar (para o mutador de
    `estado_store.atualizar_estado`).
    """
    lotes = estado.get("lotes") if isinstance(estado, dict) else None
    if not isinstance(lotes, dict):
        return 0, 0
    regs = itens = 0
    for lote in lotes.values():
        if not isinstance(lote, dict):
            continue
        msgs = lote.get("mensagens")
        if not isinstance(msgs, list):
            continue
        achou = False
        for i, m in enumerate(msgs):
            if isinstance(m, dict):
                mudou = False
                for campo, valor in list(m.items()):
                    if isinstance(valor, str):
                        novo = seguranca.remover_senhas(valor)
                        if novo != valor:
                            if aplicar:
                                m[campo] = novo
                            mudou = True
                if mudou:
                    itens += 1
                    achou = True
            elif isinstance(m, str):
                novo = seguranca.remover_senhas(m)
                if novo != m:
                    if aplicar:
                        msgs[i] = novo
                    itens += 1
                    achou = True
        if achou:
            regs += 1
    return regs, itens


def _contar_estado(dados):
    """(registros=lotes, registros_com_senha, itens_com_senha=mensagens)."""
    regs, itens = _saneia_mensagens_do_estado(dados, aplicar=False)
    lotes = dados.get("lotes") if isinstance(dados, dict) else None
    total = len(lotes) if isinstance(lotes, dict) else 0
    return total, regs, itens


# ---------------------------------------------------------------------------
# aplicação
# ---------------------------------------------------------------------------
def _aplicar_estado(caminho: str):
    """(acao, erro) — saneia o estado via `estado_store.atualizar_estado`
    (flock + gravação atômica; sem mudança o store não regrava)."""
    cont = {"itens": 0}

    def mutador(estado):
        _regs, itens = _saneia_mensagens_do_estado(estado, aplicar=True)
        cont["itens"] = itens
        return estado

    try:
        estado_store.atualizar_estado(mutador, caminho=caminho)
    except estado_store.EstadoCorrompido:
        return "pulado", "estado_corrompido"
    except OSError as e:
        return "pulado", "escrita:" + type(e).__name__
    return ("saneado" if cont["itens"] else "sem_alteracao"), None


def _aplicar_generico(caminho: str, com_trava: bool = False, manter_mtime: bool = False):
    """(acao, erro) — cópia saneada atômica preservando a estrutura JSON.

    `com_trava`: `estado_store.trava_arquivo` (fila do painel — compatibilidade da
    trava com o painel Node: CONFERIR NO SERVIDOR). `manter_mtime`: backups são
    reescritos mantendo nome e mtime (a evidência de quando existiram).
    """
    trava = estado_store.trava_arquivo(caminho) if com_trava else contextlib.nullcontext()
    try:
        with trava:
            try:
                with open(caminho, "rb") as fh:
                    dados = json.loads(fh.read())
            except (OSError, UnicodeDecodeError, ValueError) as e:
                return "pulado", "leitura:" + type(e).__name__
            novo, _itens = _sanear_generico(dados)
            if novo == dados:
                return "sem_alteracao", None
            antes = os.stat(caminho)
            _escrever_atomico(caminho, json.dumps(novo, ensure_ascii=False).encode("utf-8"))
            if manter_mtime:
                os.utime(caminho, (antes.st_atime, antes.st_mtime))
            os.chmod(caminho, 0o600)
            return "saneado", None
    except OSError as e:
        return "pulado", "escrita:" + type(e).__name__


# ---------------------------------------------------------------------------
# alvos
# ---------------------------------------------------------------------------
def _e_backup(nome: str) -> bool:
    return ".bak-" in nome or _RX_FILA_BACKUP.match(nome) is not None


def _alvos_de_backups(diretorio: str):
    try:
        nomes = sorted(os.listdir(diretorio))
    except OSError:
        return []
    return [os.path.join(diretorio, n) for n in nomes
            if _e_backup(n) and os.path.isfile(os.path.join(diretorio, n))]


def _alvos_padrao(args, estado: str, fila: str):
    """[(caminho, papel, explicito)] na ordem de processamento, já sem duplicatas."""
    alvos = [("estado", estado, args.estado is not None),
             ("fila-painel", fila, args.fila_jobs is not None)]
    dirs = args.backups_dir
    explicitos_dirs = dirs is not None
    if dirs is None:
        dirs = [caminhos.em_base("backups-estado"), caminhos.em_base("backups"),
                os.path.dirname(os.path.abspath(estado))]
    vistos_dirs = set()
    for d in dirs:
        chave = os.path.realpath(d)
        if chave in vistos_dirs:
            continue
        vistos_dirs.add(chave)
        if not os.path.isdir(d):
            if explicitos_dirs:
                alvos.append(("backup-dir", d, True))
            continue
        for cam in _alvos_de_backups(d):
            alvos.append(("backup", cam, explicitos_dirs))
    for cam in (args.extra or []):
        alvos.append(("extra", cam, True))
    saida, vistos = [], set()
    for papel, cam, explicito in alvos:
        chave = os.path.realpath(cam)
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append({"caminho": cam, "papel": papel, "explicito": explicito})
    return saida


# ---------------------------------------------------------------------------
# processamento por arquivo
# ---------------------------------------------------------------------------
def _registro_base(alvo: dict) -> dict:
    return {"arquivo": alvo["caminho"], "papel": alvo["papel"],
            "registros": None, "registros_com_senha": None, "itens_com_senha": None,
            "tamanho_bytes": None, "permissao": None, "dono": None, "dono_uid": None,
            "acao": None, "motivo": None, "erro": None}


def _processar_arquivo(alvo: dict, modo: str, sanear_backups: bool,
                       incluir_fila_painel: bool) -> dict:
    """`modo`: "inventario" (só conta) | "simulacao" (prévia da escrita) | "escrita"."""
    rec = _registro_base(alvo)
    caminho = alvo["caminho"]

    if alvo["papel"] == "backup-dir":
        rec["erro"] = "leitura:FileNotFoundError"
        rec["acao"] = "pulado"
        return rec
    try:
        st = os.stat(caminho)
    except FileNotFoundError:
        if alvo["explicito"]:
            rec["erro"] = "leitura:FileNotFoundError"
            rec["acao"] = "pulado"
        else:
            rec["acao"] = "ausente"
        return rec
    except OSError as e:
        rec["erro"] = "leitura:" + type(e).__name__
        rec["acao"] = "pulado"
        return rec
    rec["tamanho_bytes"] = st.st_size
    rec["permissao"] = _permissao(st)
    rec["dono"], rec["dono_uid"] = _dono(st)

    dados, erro = _ler_json(caminho)
    if erro:
        rec["erro"] = erro
        rec["acao"] = "pulado"
        return rec
    if alvo["papel"] == "estado":
        regs, rcs, itens = _contar_estado(dados)
    else:
        regs, rcs, itens = _contar_generico(dados)
    rec["registros"], rec["registros_com_senha"], rec["itens_com_senha"] = regs, rcs, itens

    if modo == "inventario":
        rec["acao"] = "inventariado"
        return rec

    if alvo["papel"] == "backup" and not sanear_backups:
        rec["acao"] = "pulado"
        rec["motivo"] = MOTIVO_BACKUPS
        return rec
    if alvo["papel"] == "fila-painel" and not incluir_fila_painel:
        rec["acao"] = "pulado"
        rec["motivo"] = MOTIVO_FILA_PAINEL
        return rec

    if modo == "simulacao":
        rec["acao"] = "simulado"
        return rec

    if alvo["papel"] == "estado":
        acao, erro = _aplicar_estado(caminho)
    else:
        acao, erro = _aplicar_generico(caminho,
                                       com_trava=(alvo["papel"] == "fila-painel"),
                                       manter_mtime=(alvo["papel"] == "backup"))
    rec["acao"] = acao
    rec["erro"] = erro
    return rec


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _montar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sanear_senhas.py",
        description=("Inventaria e saneia senhas gov.br/Meu INSS do HISTÓRICO "
                     "(estado.json, backups, fila do painel, extras). "
                     "O modo padrão é --inventario (somente leitura): só contagens, "
                     "nunca trecho de texto, nome, CPF ou senha."),
        epilog=("Gravação exige --aplicar --confirmar. Backups exigem ainda "
                "--sanear-backups --ciente-evidencia (evidência de incidente LGPD: "
                "preserve cópia/hash ANTES; poda é outra ferramenta — "
                "estado_store.podar_backups). Fila do painel exige "
                "--incluir-fila-painel (trava compatível: conferir no servidor). "
                "Códigos: 0 ok; 1 senha encontrada na varredura (nada gravado); "
                "2 erro de leitura/JSON inválido ou recusa de segurança."))
    modo = p.add_mutually_exclusive_group()
    modo.add_argument("--inventario", action="store_true",
                      help="só leitura: contagens por arquivo (modo padrão)")
    modo.add_argument("--aplicar", action="store_true",
                      help="modo de aplicação (grava só com --confirmar; sem ele é simulação)")
    p.add_argument("--confirmar", action="store_true",
                   help="confirma a escrita (obrigatório junto com --aplicar)")
    p.add_argument("--estado", metavar="ARQ",
                   help="estado.json (padrão: $ESTADO_JSON ou <BASE>/estado.json)")
    p.add_argument("--backups-dir", metavar="DIR", action="append",
                   help="diretório de backups: pega *.bak-* e fila-jobs-*.json "
                        "(repetível; padrão: <BASE>/backups-estado, <BASE>/backups "
                        "e o diretório do estado)")
    p.add_argument("--fila-jobs", metavar="ARQ",
                   help="fila-jobs.json do painel (padrão: <SEPARADOR>/data/fila-jobs.json)")
    p.add_argument("--extra", metavar="ARQ", action="append",
                   help="arquivo JSON extra a sanear (repetível)")
    p.add_argument("--sanear-backups", action="store_true",
                   help="reescreve os backups saneados (mantém nome e mtime; NÃO apaga)")
    p.add_argument("--ciente-evidencia", action="store_true",
                   help="confirma que a preservação de evidência LGPD foi avaliada "
                        "(obrigatório com --sanear-backups em modo de escrita)")
    p.add_argument("--incluir-fila-painel", action="store_true",
                   help="autoriza escrever na fila-jobs.json do painel (consumida ao vivo)")
    p.add_argument("--json", action="store_true", help="saída em JSON")
    return p


def _imprimir_linha(rec: dict) -> None:
    campos = ["papel", "registros", "registros_com_senha", "itens_com_senha",
              "tamanho_bytes", "permissao", "acao", "motivo", "erro"]
    partes = [rec["arquivo"]]
    for c in campos:
        if rec.get(c) not in (None, ""):
            partes.append("%s=%s" % (c, rec[c]))
    if rec.get("dono") is not None:
        partes.append("dono=%s(%s)" % (rec["dono"], rec["dono_uid"]))
    print("  ".join(partes))


def main(argv=None) -> int:
    parser = _montar_parser()
    args = parser.parse_args(argv)

    if args.confirmar and not args.aplicar:
        print("ERRO: --confirmar só tem sentido com --aplicar "
              "(o modo padrão é o inventário, somente leitura).", file=sys.stderr)
        return CODIGO_ERRO
    escrever = bool(args.aplicar and args.confirmar)

    if args.sanear_backups:
        print(AVISO_EVIDENCIA, file=sys.stderr)
        if escrever and not args.ciente_evidencia:
            print("RECUSADO: --sanear-backups em modo de escrita exige --ciente-evidencia "
                  "(evidência de incidente LGPD avaliada?). NADA foi gravado.",
                  file=sys.stderr)
            return CODIGO_ERRO

    estado = args.estado or os.environ.get("ESTADO_JSON") or caminhos.em_base("estado.json")
    fila = args.fila_jobs or caminhos.em_separador("data", "fila-jobs.json")
    alvos = _alvos_padrao(args, estado, fila)

    registros = []
    modo = "escrita" if escrever else ("simulacao" if args.aplicar else "inventario")
    for alvo in alvos:
        rec = _processar_arquivo(alvo, modo, bool(args.sanear_backups),
                                 bool(args.incluir_fila_painel))
        registros.append(rec)
        if rec.get("erro"):
            print("AVISO: %s: %s — pulado" % (rec["arquivo"], rec["erro"]), file=sys.stderr)

    resumo = {"arquivos": len(registros),
              "com_senha": sum(1 for r in registros if (r["registros_com_senha"] or 0) > 0
                               or (r["itens_com_senha"] or 0) > 0),
              "itens_com_senha": sum(r["itens_com_senha"] or 0 for r in registros),
              "alteracoes": sum(1 for r in registros if r["acao"] == "saneado"),
              "erros": sum(1 for r in registros if r.get("erro"))}
    if resumo["erros"]:
        codigo = CODIGO_ERRO
    elif escrever:
        codigo = CODIGO_OK
    elif resumo["com_senha"]:
        codigo = CODIGO_SENHA
    else:
        codigo = CODIGO_OK
    resumo["codigo"] = codigo

    if args.json:
        print(json.dumps({"modo": modo, "arquivos": registros, "resumo": resumo},
                         ensure_ascii=False, indent=2))
    else:
        for rec in registros:
            _imprimir_linha(rec)
        print("TOTAL modo=%s arquivos=%d com_senha=%d itens_com_senha=%d "
              "alteracoes=%d erros=%d codigo=%d"
              % (modo, resumo["arquivos"], resumo["com_senha"], resumo["itens_com_senha"],
                 resumo["alteracoes"], resumo["erros"], codigo))
    if not escrever and resumo["com_senha"]:
        print("Há senha no histórico varrido. Para sanear: --aplicar --confirmar "
              "(backups: acrescente --sanear-backups --ciente-evidencia; "
              "fila do painel: --incluir-fila-painel).", file=sys.stderr)
    return codigo


if __name__ == "__main__":
    sys.exit(main())
