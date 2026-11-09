import tipos_documento as td


def test_documentos():
    for n in ("rg.JPG", "a.pdf", "x.jpeg", "ficha.docx", "foto.heic"):
        assert td.classificar(n) == td.DOCUMENTO


def test_midias_nao_sao_documento():
    for n in ("audio.flac", "voz.ogg", "v.mp4", "a.wav", "b.mp3", "c.opus"):
        assert td.classificar(n) == td.MIDIA
        assert not td.e_documento(n)


def test_sem_extensao_usa_mime():
    assert td.classificar("arquivo", "image/jpeg") == td.DOCUMENTO
    assert td.classificar("arquivo", "audio/ogg") == td.MIDIA
    assert td.classificar("arquivo", None) == td.DESCONHECIDO
    assert td.classificar(None, None) == td.DESCONHECIDO
