"""Tipos de arquivo aceitos como DOCUMENTO de kit (fonte única para triagem, download e ponte).

Áudio, vídeo e mensagem de voz NÃO são documento de contratação: ficam registrados no lote
(para ninguém achar que sumiram) mas não vão ao worker nem contam como pendência de download.
"""
from __future__ import annotations

import os

DOCUMENTO = "documento"
MIDIA = "midia"
DESCONHECIDO = "desconhecido"

EXT_DOCUMENTO = frozenset({
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif",
    ".doc", ".docx", ".odt", ".rtf", ".txt", ".xls", ".xlsx", ".ods",
})
EXT_MIDIA = frozenset({
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".amr", ".wma",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp", ".m4v", ".wmv",
})
MIME_DOC_PREFIXOS = ("image/", "application/pdf", "application/msword",
                     "application/vnd.openxmlformats-officedocument", "application/vnd.oasis.opendocument",
                     "text/plain", "application/rtf")
MIME_MIDIA_PREFIXOS = ("audio/", "video/")


def classificar(nome: str | None, mime: str | None = None) -> str:
    """'documento' | 'midia' | 'desconhecido'. Extensão manda; MIME desempata quando não há extensão."""
    ext = os.path.splitext((nome or "").strip().lower())[1]
    if ext in EXT_DOCUMENTO:
        return DOCUMENTO
    if ext in EXT_MIDIA:
        return MIDIA
    m = (mime or "").strip().lower()
    if m.startswith(MIME_MIDIA_PREFIXOS):
        return MIDIA
    if m.startswith(MIME_DOC_PREFIXOS):
        return DOCUMENTO
    return DESCONHECIDO


def e_documento(nome: str | None, mime: str | None = None) -> bool:
    return classificar(nome, mime) == DOCUMENTO
