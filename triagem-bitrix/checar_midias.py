#!/usr/bin/env python3
"""Vídeos/áudios que o connector entregou × o que chegou no lote (SÓ LEITURA).

Pergunta do dono (27/10/2026): *"parece que as pessoas tem alguns videos que nao
ta indo pro servidor, como a gente resolve isso de uma vez?"*.

Este script responde com DADO, não com palpite: lê os anexos que a API do
VibeCode devolve (`files` da página de mensagens) e cruza com o `arquivo_index`
do estado.json. Se o connector entregou e o lote não pegou → é bug NOSSO. Se o
connector não entregou nada de vídeo → o problema é ANTES da triagem.

Frente D (OP-O5/C1): "mídia" agora é a MESMA regra de todo o pipeline
(`tipos_documento` — extensão manda, MIME desempata — mais o `type` audio/video/voice
do Bitrix). A parte do estado mostra quantas mídias estão em lotes e se alguma ainda
está pendente sem marca de `ignorado_midia` (seria retentada como documento).

Uso: python3 checar_midias.py [paginas] [--so-estado]
Saída vazia = nada a reportar (padrão vigia). Nunca grava nada.

Códigos de saída: 0 ok | 1 mídia fora de lote / pendente sem marca |
2 estado.json corrompido | 3 config.json inválido (C3: antes era traceback cru).
"""
from __future__ import annotations

import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tipos_documento  # noqa: E402

TIPOS_MIDIA = ("video", "audio", "voice")
SAIDA_CONFIG = 3   # config.json inválido — mesmo código do triagem_grupo.py


def e_midia_anexo(f: dict) -> bool:
    """Anexo da API (dict com type/name/extension/mime) é áudio/vídeo?"""
    if (f.get("type") or "").lower() in TIPOS_MIDIA:
        return True
    nome = f.get("name") or ""
    ext = (f.get("extension") or "").strip(".").lower()
    if ext and not nome.lower().endswith("." + ext):
        nome = f"{nome}.{ext}"
    return tipos_documento.classificar(nome, f.get("mime") or f.get("mimeType")) == tipos_documento.MIDIA


def resumo_estado(estado: dict, itens_fila: list) -> dict:
    """Mídias registradas em lotes × marcação na fila de download (só leitura)."""
    na_fila = {str(i.get("file_id")): i.get("estado") for i in itens_fila or [] if isinstance(i, dict)}
    midia_no_lote = pendente_sem_marca = 0
    for lote in (estado.get("lotes") or {}).values():
        if not isinstance(lote, dict):
            continue
        for m in lote.get("arquivos") or []:
            if not isinstance(m, dict):
                continue
            nome = m.get("nome_original") or m.get("nome_salvo") or ""
            if not ((m.get("tipo") or "").lower() in TIPOS_MIDIA
                    or tipos_documento.classificar(nome) == tipos_documento.MIDIA):
                continue
            midia_no_lote += 1
            fid = str(m.get("file_id"))
            if (not m.get("baixado") and m.get("erro_tipo") != "midia"
                    and na_fila.get(fid) != "ignorado_midia"):
                pendente_sem_marca += 1
    return {"midia_no_lote": midia_no_lote, "midia_pendente_sem_marca": pendente_sem_marca,
            "fila_ignorado_midia": sum(1 for e in na_fila.values() if e == "ignorado_midia")}


def _ler_json(caminho: str):
    try:
        with open(caminho, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    so_estado = "--so-estado" in argv
    nums = [a for a in argv if a.isdigit()]
    paginas = int(nums[0]) if nums else 12

    import estado_store
    import download_svc
    try:
        estado = estado_store.ler_estado()
    except estado_store.EstadoCorrompido as e:
        print(f"⚠️ estado.json corrompido ({e.caminho}); nada lido")
        return 2
    fila = _ler_json(download_svc.DEFAULT_QUEUE_FILE) or {}
    itens = fila.get("itens") if isinstance(fila, dict) else (fila if isinstance(fila, list) else [])
    r = resumo_estado(estado, itens or [])
    print(f"estado: mídias em lotes {r['midia_no_lote']} | pendentes sem marca de mídia "
          f"{r['midia_pendente_sem_marca']} | fila ignorado_midia {r['fila_ignorado_midia']}")
    if so_estado:
        return 1 if r["midia_pendente_sem_marca"] else 0

    import triagem_grupo as tg   # só para LER páginas do connector (nenhuma escrita)
    try:
        conf = tg.carregar_config()
    except tg.ConfigInvalida as e:
        # (C3) config inválido: mensagem clara e código != 0, sem traceback cru.
        import seguranca
        print(f"🚨 config.json inválido — nada lido do grupo: "
              f"{seguranca.mascarar(str(e))[:300]}")
        return SAIDA_CONFIG
    chave = tg.ler_chave_bot()
    idx = estado.get('arquivos_index') or {}
    entregues = []          # (dialog, id, tipo, ext, data, tamanho)
    tipos = collections.Counter()
    for dlg in conf.get('dialog_ids') or []:
        last = None
        for _ in range(paginas):
            try:
                d = tg.ler_pagina_mensagens(chave, dlg, last_id=last, limite=50)
            except Exception as e:                      # noqa: BLE001
                import seguranca
                print(f"⚠️ falha lendo {dlg}: {seguranca.mascarar(str(e))[:90]}")
                break
            for f in (d.get('files') or []):
                tipos[f.get('type')] += 1
                if e_midia_anexo(f):
                    entregues.append((dlg, f.get('id'), f.get('type'),
                                      (f.get('extension') or '').lower(),
                                      f.get('date'), f.get('size')))
            msgs = d.get('messages') or []
            if not msgs or len(msgs) < 50:
                break
            last = min(m.get('id') for m in msgs if m.get('id'))

    perdidos = [e for e in entregues if str(e[1]) not in idx]
    print(f"anexos vistos: {dict(tipos)}")
    print(f"vídeo/áudio entregues pelo connector: {len(entregues)} | fora de lote: {len(perdidos)}")
    for e in entregues[-10:]:
        marca = "OK no lote" if str(e[1]) in idx else "FORA DO LOTE"
        print(f"  {e[0]} | {e[4]} | {e[2]}/{e[3]} {e[5]}B | {marca}")
    if perdidos:
        print("\n🚨 mídia entregue e NÃO vinculada a lote — investigar file_ids_da_mensagem")
        return 1
    if not entregues:
        print("\n(nenhum vídeo/áudio no período lido — a quebra está ANTES da triagem: connector WhatsApp→Bitrix)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
