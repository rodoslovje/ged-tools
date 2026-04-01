import os
import tempfile
import textwrap
from tools.gedcom_cleaner import process_file


def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _run(gedcom_str: str) -> str:
    inp = _write_tmp(gedcom_str)
    out = _write_tmp("")
    try:
        process_file(inp, out, cleaners=[], strippers=[], transformers=["addr_to_plac"], warn=False)
        return open(out, encoding="utf-8").read()
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_addr_prepended_to_existing_plac():
    content = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 BIRT
        2 DATE 1 JAN 1900
        2 ADDR Polje 31
        2 PLAC Ljubljana, Slovenija
        0 TRLR
    """))
    assert "2 PLAC Polje 31, Ljubljana, Slovenija" in content
    assert "2 ADDR" not in content


def test_addr_becomes_plac_when_no_plac():
    content = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 BIRT
        2 DATE 3 OCT 1834
        2 ADDR Polje 31
        0 TRLR
    """))
    assert "2 PLAC Polje 31" in content
    assert "2 ADDR" not in content


def test_empty_addr_left_alone():
    content = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 BIRT
        2 DATE 1900
        2 ADDR
        2 PLAC Ljubljana
        0 TRLR
    """))
    # empty ADDR should not modify PLAC
    assert "2 PLAC Ljubljana" in content


def test_no_addr_unchanged():
    content = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 BIRT
        2 DATE 1900
        2 PLAC Ljubljana
        0 TRLR
    """))
    assert "2 PLAC Ljubljana" in content
    assert "ADDR" not in content
