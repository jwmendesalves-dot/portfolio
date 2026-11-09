#!/usr/bin/env python3
"""Vigia do fluxo automático (grupo → IA → NAS → Bitrix → planilha).

Imprime SÓ quando há o que avisar (stdout vazio = nada é enviado):
  1. o PRIMEIRO kit que chegou ao grupo DEPOIS de o fluxo ser ligado e passou por
     todas as 7 etapas sozinho  → avisa uma vez;
  2. qualquer ERROR/dead-letter em lote enviado pela ponte → avisa uma vez por job;
  3. qualquer CONCLUSÃO PARCIAL (job 'concluido_parcial', passo em 'atencao' ou
     resultado.bitrix.falha — ex.: Bitrix sem contato) → avisa uma vez por job.

  4. (F2-12) job de lote enviado nos últimos JANELA_SUMIDO_DIAS que NÃO está nem na fila
     nem no espelho → avisa uma vez (antes virava `{}` e nada era avisado).

Estado em <BASE>/aviso-estado.json (não repete aviso; `erros_avisados` só guarda jobs que
ainda estão no controle — não cresce sem limite). Saída sem nome de cliente e com CPF
mascarado (S04): o identificador é o lote.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import caminhos  # noqa: E402
import cpf_util  # noqa: E402
import estado_store  # noqa: E402

BASE = caminhos.BASE
CTRL = caminhos.em_base("fila-enviados.json")
FASE1 = estado_store.ARQ_ESTADO
FILA = caminhos.em_separador("data", "fila-jobs.json")
ESPELHO = caminhos.em_separador("data", "jobs.json")
ESTADO = caminhos.em_base("aviso-estado.json")
JANELA_SUMIDO_DIAS = 7
MAX_AVISADOS = 5000


def _cpf(cliente) -> str:
    cpf = (cliente or {}).get("cpf")
    return cpf_util.mascarar(cpf) if cpf else "(sem CPF)"

# corte do "kit novo": tudo que a ponte mandou ANTES deste horário ela mandou na mão
# (GLENDA 17:21 e a leva de 17:31 são kits de 12:48 e de 18–19/09). A partir daqui vale a
# regra do escritório: só kit aberto com traço E fechado com traço (a ponte filtra isso).
FLUXO_LIGADO_EM = "2026-09-21T17:00:00-03:00"


def quando(valor):
    """ISO do Bitrix vem no fuso do portal (UTC+3) — comparar sempre como datetime."""
    try:
        d = datetime.fromisoformat(str(valor))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def ler(caminho):
    try:
        with open(caminho, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def falha_bitrix(j):
    """Motivo de o Bitrix NÃO ter ficado ok neste job; None se ok ou sem registro.

    Cobre os dois formatos que chegam no espelho:
      - web-server (5.2): resultado.bitrix.falha/erro, ou passo 'atencao';
      - worker legado: passo 'ok' com detalhe 'Falhou: ... (segue)' e/ou
        contact_id nulo (a falha era engolida — não pode ficar muda).
    Registro VAZIO/nulo (o espelho grava resultado.bitrix = null enquanto a
    etapa ainda não rodou) é "sem registro": NÃO se afirma falha de job em
    andamento — senão todo job em voo virava "CONCLUÍDO COM RESSALVA" falso.
    """
    r = j.get("resultado") or {}
    bx = r.get("bitrix")
    if not isinstance(bx, dict) or not bx:
        return None  # sem registro — não dá para afirmar falha
    if bx.get("falha") or bx.get("ok") is False:
        return str(bx.get("erro") or bx.get("detalhe") or "falha no Bitrix")
    det = str(bx.get("detalhe") or "")
    if "Falhou:" in det:
        return det
    if bx.get("contact_id") in (None, "", 0):
        return "sem contato/deal no Bitrix"
    return None


def main() -> int:
    ctrl = ler(CTRL) or {}
    f1 = ler(FASE1) or {}
    fila = ler(FILA) or []
    esp = ler(ESPELHO) or []
    est = ler(ESTADO) or {}
    est.setdefault("primeiro_avisado", False)
    est.setdefault("primeiro_novo_avisado", False)
    # lista única de jobs JÁ avisados (erro ou ressalva) — garante 1 aviso por job
    est.setdefault("erros_avisados", [])
    if not ctrl:
        return 0

    por_id = {}
    for j in list(fila) + list(esp):
        por_id[j.get("id")] = j

    lotes = f1.get("lotes") or {}
    recados = []

    # 1) erro OU conclusão parcial (ressalva) em lote enviado pela ponte —
    #    UMA vez por job (dedupe na lista 'erros_avisados')
    #    Resalvas novas saem no máximo MAX_RESSALVAS_POR_EXECUCAO por execução:
    #    o histórico antigo pode ter dezenas de "concluído com ressalva" e uma
    #    tacada só estouraria a mensagem do Telegram (as demais saem nos crons
    #    seguintes — cada job ainda avisa UMA vez).
    MAX_RESSALVAS_POR_EXECUCAO = 5
    ressalvas_esta_execucao = 0
    ressalvas_adiantadas = 0
    agora_dt = datetime.now(timezone.utc)
    for _lid, c in ctrl.items():
        if not isinstance(c, dict) or not c.get("job_id"):
            continue                       # 'enfileirando' (sem job confirmado): monitor_lotes avisa
        j = por_id.get(c.get("job_id"))
        if j is None:
            # F2-12: job sumiu da fila E do espelho — falha silenciosa antes
            chave = f"sumido:{c['job_id']}"
            em = quando(c.get("confirmado_em") or c.get("em"))
            recente = bool(em and em >= agora_dt - timedelta(days=JANELA_SUMIDO_DIAS))
            if recente and chave not in est["erros_avisados"]:
                est["erros_avisados"].append(chave)
                recados.append(
                    "❓ JOB SUMIU da fila e do espelho do painel\n"
                    f"Lote: {_lid} · job {c['job_id']} · CPF {_cpf(c.get('cliente'))}\n"
                    f"Enviado pela ponte em: {str(c.get('em') or '?')[:16]} — conferir no painel")
            continue
        st = j.get("status")
        passos = j.get("passos") or []
        passo_atencao = next((p for p in passos if p.get("status") in ("atencao", "parcial")), None)
        motivo_bitrix = falha_bitrix(j)
        com_erro = st in ("erro", "dead-letter")
        # 5.2: conclusão com ressalva (job 'concluido_parcial', passo 'atencao'
        # ou Bitrix sem contato) também avisa — antes só erro/dead-letter era
        # coberto e a falha do Bitrix fechava "verde" sem ninguém saber.
        # Só quando o job TERMINOU (concluido/concluido_parcial): job em voo
        # não é "CONCLUÍDO COM RESSALVA" — o aviso sairia antes da hora e o
        # dedupe engoliria o desfecho de verdade depois.
        com_ressalva = (not com_erro) and st in ("concluido", "concluido_parcial") and (
            st == "concluido_parcial" or passo_atencao is not None or motivo_bitrix is not None
        )
        if not (com_erro or com_ressalva):
            continue
        if c["job_id"] in est["erros_avisados"]:
            continue
        if com_ressalva and ressalvas_esta_execucao >= MAX_RESSALVAS_POR_EXECUCAO:
            ressalvas_adiantadas += 1  # NÃO marca como avisado — sai na próxima
            continue
        est["erros_avisados"].append(c["job_id"])
        cliente = (j.get("cliente") or c.get("cliente") or {})
        if com_erro:
            passo = next((p for p in passos if p.get("status") != "ok"), {})
            recados.append(
                "❗ ERRO no fluxo do grupo\n"
                f"Cliente: CPF {_cpf(cliente)}\n"
                f"Lote: {_lid}\n"
                f"Parou em: {passo.get('chave', '?')} — {passo.get('detalhe', '')}\n"
                f"Motivo: {str(j.get('lastError') or '')[:300]}"
            )
        else:
            ressalvas_esta_execucao += 1
            passo = passo_atencao or {}
            recados.append(
                "⚠️ CONCLUÍDO COM RESSALVA no fluxo do grupo\n"
                f"Cliente: CPF {_cpf(cliente)}\n"
                f"Lote: {_lid}\n"
                f"Ressalva em: {passo.get('chave', 'bitrix')} — {passo.get('detalhe', '')}\n"
                f"Motivo: {(motivo_bitrix or passo.get('detalhe') or '(sem detalhe)')[:300]}\n"
                f"Status do job: {st} — revisar o que ficou pendente (ex.: contato/deal no Bitrix)"
            )
    # "e mais N…" avisa UMA vez por EPISÓDIO de backlog (contrato Hermes: alerta
    # novo só com dedupe) — senão o mesmo recado repetia a cada cron enquanto a
    # fila de ressalvas drenava 5 a 5. Zera quando não há mais nada adiado.
    if ressalvas_adiantadas and not est.get("ressalvas_aviso_aberto"):
        est["ressalvas_aviso_aberto"] = True
        recados.append(
            f"… e mais {ressalvas_adiantadas} job(s) com ressalva neste ciclo — "
            "avisam nas próximas execuções (1 aviso por job)"
        )
    elif not ressalvas_adiantadas:
        est["ressalvas_aviso_aberto"] = False

    # 2) primeiros marcos: (a) primeiro kit do TRAÇO concluído no fluxo automático;
    #    (b) primeiro kit do TRAÇO que CHEGOU NO GRUPO com o fluxo já no ar.
    for marco, campo, titulo in (
        ("fluxo", "primeiro_avisado", "✅ PRIMEIRO KIT COM TRAÇO 100% AUTOMÁTICO"),
        ("novo", "primeiro_novo_avisado", "✅ PRIMEIRO KIT NOVO COM TRAÇO 100% AUTOMÁTICO"),
    ):
        if est.get(campo):
            continue
        corte = quando(FLUXO_LIGADO_EM)
        for lid, c in sorted(ctrl.items(), key=lambda kv: kv[1].get("em", "")):
            j = por_id.get(c.get("job_id")) or {}
            if j.get("status") != "concluido":
                continue
            if falha_bitrix(j):
                # o marco "100% AUTOMÁTICO" nunca sai com falha registrada no Bitrix
                continue
            if not (((j.get("resultado") or {}).get("bitrix") or {}).get("contact_id")):
                # ... nem com contact_id nulo (falta de registro conta como não ok)
                continue
            lote = lotes.get(lid) or {}
            do_traco = ((lote.get("modo") or "") == "marcadores"
                        and (lote.get("status") == "FECHADO_PRONTO"
                             or str(lote.get("fechado_por") or "").startswith("sinal de traço")))
            if not do_traco:
                continue
            inicio = quando(lote.get("inicio"))
            novo = bool(inicio and corte and inicio >= corte)
            if marco == "novo" and not novo:
                continue
            r = j.get("resultado") or {}
            pl = r.get("planilha") if isinstance(r.get("planilha"), dict) else {}
            bx = r.get("bitrix") if isinstance(r.get("bitrix"), dict) else {}
            pa = r.get("pasta") if isinstance(r.get("pasta"), dict) else {}
            t = j.get("atualizado_em") or ""
            brt = timezone(timedelta(hours=-3))
            est[campo] = True
            recados.append(
                f"{titulo} (grupo → IA → NAS → Bitrix → planilha)\n"
                f"Cliente: CPF {_cpf(j.get('cliente'))}\n"
                f"Kit: aberto e fechado pelo traço · "
                f"{'chegou no grupo com o fluxo já no ar' if novo else 'kit do backlog do dia'}\n"
                f"Chegou no grupo: {inicio.astimezone(brt).strftime('%d/%m %H:%M') if inicio else '?'} · "
                f"concluído: {quando(t).astimezone(brt).strftime('%d/%m %H:%M') if quando(t) else t}\n"
                f"Código: {pl.get('codigo')} · linha {pl.get('linha')} · "
                f"pasta \"{pa.get('nomeFinal')}\"\n"
                f"Bitrix: contato {bx.get('contact_id')} / deal {bx.get('deal_id')}\n"
                f"Lote: {lid} · {len(j.get('passos') or [])} etapas, todas ok"
            )
            break

    if recados:
        print("\n\n".join(recados))

    # F2-12: a lista de avisados só guarda jobs que ainda estão no controle (com teto)
    vivos = {c.get("job_id") for c in ctrl.values() if isinstance(c, dict) and c.get("job_id")}
    est["erros_avisados"] = [x for x in est["erros_avisados"]
                             if str(x).split("sumido:", 1)[-1] in vivos][-MAX_AVISADOS:]
    estado_store.gravar_atomico(est, ESTADO)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
