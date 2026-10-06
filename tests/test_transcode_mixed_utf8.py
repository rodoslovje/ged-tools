import os

from tools.gedcom_cleaner import _transcode_to_utf8


def _transcode(tmp_path, raw: bytes) -> str:
    src = tmp_path / "in.ged"
    src.write_bytes(raw)
    out, is_tmp = _transcode_to_utf8(str(src))
    try:
        with open(out, encoding="utf-8") as f:
            return f.read()
    finally:
        if is_tmp:
            os.unlink(out)


def _crlf(*lines: bytes) -> bytes:
    return b"\r\n".join(lines) + b"\r\n"


def test_stray_cp1250_lines_do_not_garble_utf8_file(tmp_path):
    # MyHeritage UTF-8 export with a few cp1250 lines carried over verbatim.
    raw = b"\xef\xbb\xbf" + _crlf(
        b"0 HEAD",
        b"1 CHAR UTF-8",
        b"0 @I1@ INDI",
        b"1 NAME Toma\xc5\xbe /\xc5\xa0alamun/",
        b"1 NOTE Sredi\xc5\xa1\xc4\x8de ob Dravi",
        b"1 OCCU u\xc4\x8ditelj",
        b"0 @I2@ INDI",
        b"1 NOTE Spouse: Marija Makarovi\xe8 (born Ga\x9aper\xe8i\xe8)",
        b"0 TRLR",
    )
    text = _transcode(tmp_path, raw)
    assert "Tomaž /Šalamun/" in text
    assert "Središče ob Dravi" in text
    assert "učitelj" in text
    assert "Marija Makarovič (born Gašperčič)" in text


def test_utf8_and_cp1250_on_the_same_line(tmp_path):
    # Only the invalid bytes fall back to cp1250; the valid UTF-8 č survives.
    raw = b"\xef\xbb\xbf" + _crlf(
        b"0 HEAD",
        b"1 CHAR UTF-8",
        b"0 @I1@ INDI",
        b"1 NOTE Sredi\xc5\xa1\xc4\x8de, u\xe8itelj",
        b"0 TRLR",
    )
    assert "Središče, učitelj" in _transcode(tmp_path, raw)


def test_mostly_utf8_file_without_bom(tmp_path):
    raw = _crlf(
        b"0 HEAD",
        b"1 CHAR UTF-8",
        b"0 @I1@ INDI",
        b"1 NAME Toma\xc5\xbe /\xc5\xa0alamun/",
        b"1 NOTE Sredi\xc5\xa1\xc4\x8de ob Dravi",
        b"1 OCCU u\xe8itelj",
        b"0 TRLR",
    )
    text = _transcode(tmp_path, raw)
    assert "Tomaž /Šalamun/" in text
    assert "Središče ob Dravi" in text
    assert "učitelj" in text


def test_cp1250_file_with_lying_utf8_header_still_decodes_as_cp1250(tmp_path):
    raw = _crlf(
        b"0 HEAD",
        b"1 CHAR UTF-8",
        b"0 @I1@ INDI",
        b"1 NAME Jo\x9eef /Ko\x9aak/",
        b"1 OCCU u\xe8itelj",
        b"0 TRLR",
    )
    text = _transcode(tmp_path, raw)
    assert "Jožef /Košak/" in text
    assert "učitelj" in text


def test_conc_split_utf8_is_stitched_before_mixed_fallback(tmp_path):
    raw = b"\xef\xbb\xbf" + _crlf(
        b"0 HEAD",
        b"1 CHAR UTF-8",
        b"0 @I1@ INDI",
        b"1 NOTE rojena Kari\xc5\xbe)<br />\xc5",
        b"2 CONC \xbeene: Josipina",
        b"1 OCCU u\xe8itelj",
        b"0 TRLR",
    )
    text = _transcode(tmp_path, raw)
    assert "rojena Kariž)<br />žene: Josipina" in text
    assert "učitelj" in text
