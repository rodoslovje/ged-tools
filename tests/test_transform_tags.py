import os
import tempfile
import textwrap
from tools.gedcom_cleaner import process_file


def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _run(gedcom_str: str, transformers: list[str]) -> tuple[str, dict]:
    inp = _write_tmp(gedcom_str)
    out = _write_tmp("")
    try:
        _, _, transform_stats = process_file(
            inp, out, cleaners=[], strippers=[], transformers=transformers, warn=False
        )
        return open(out, encoding="utf-8").read(), transform_stats
    finally:
        os.unlink(inp)
        os.unlink(out)


# ---------------------------------------------------------------------------
# fid_fsftid
# ---------------------------------------------------------------------------

def test_fid_renamed_to_fsftid():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 _FID ABC123
        0 TRLR
    """), ["fid_fsftid"])
    assert "_FSFTID ABC123" in content
    assert "_FID" not in content
    assert stats["fid_fsftid"].transformed == 1


# ---------------------------------------------------------------------------
# latr_even
# ---------------------------------------------------------------------------

def test_latr_converted_to_even():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 LATR
        2 DATE 15 MAR 1850
        2 PLAC Ljubljana
        0 TRLR
    """), ["latr_even"])
    assert "1 EVEN" in content
    assert "2 TYPE Land Transaction" in content
    assert "LATR" not in content
    assert stats["latr_even"].transformed == 1


# ---------------------------------------------------------------------------
# secg_givn
# ---------------------------------------------------------------------------

def test_secg_appended_to_existing_givn():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        2 GIVN John
        2 SURN Smith
        2 SECG Jr
        0 TRLR
    """), ["secg_givn"])
    assert "GIVN John Jr" in content
    assert "SECG" not in content
    assert stats["secg_givn"].transformed == 1


def test_secg_creates_givn_when_missing():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        2 SURN Smith
        2 SECG Jr
        0 TRLR
    """), ["secg_givn"])
    assert "GIVN Jr" in content
    assert "SECG" not in content
    assert stats["secg_givn"].transformed == 1


def test_secg_empty_value_not_transformed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        2 GIVN John
        2 SECG
        0 TRLR
    """), ["secg_givn"])
    # empty SECG — no transformation
    assert stats["secg_givn"].transformed == 0
