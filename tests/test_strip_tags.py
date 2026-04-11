import os
import tempfile
import textwrap
from tools.gedcom_cleaner import process_file


def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _run(gedcom_str: str, strippers: list[str]) -> tuple[str, dict]:
    inp = _write_tmp(gedcom_str)
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(
            inp, out, cleaners=[], strippers=strippers, transformers=[], warn=False
        )
        return open(out, encoding="utf-8").read(), strip_stats
    finally:
        os.unlink(inp)
        os.unlink(out)


# ---------------------------------------------------------------------------
# labl
# ---------------------------------------------------------------------------

def test_labl_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 _LABL Green
        0 TRLR
    """), ["labl"])
    assert "_LABL" not in content
    assert "John /Smith/" in content
    assert stats["labl"].removed == 1


# ---------------------------------------------------------------------------
# place_tran
# ---------------------------------------------------------------------------

def test_place_tran_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 BIRT
        2 PLAC Ljubljana
        3 TRAN Laibach
        4 LANG de
        0 TRLR
    """), ["place_tran"])
    assert "TRAN" not in content
    assert "Ljubljana" in content
    assert stats["place_tran"].removed == 1


# ---------------------------------------------------------------------------
# mise
# ---------------------------------------------------------------------------

def test_mise_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 MISE SomeValue
        0 TRLR
    """), ["mise"])
    assert "MISE" not in content
    assert stats["mise"].removed == 1


# ---------------------------------------------------------------------------
# object_crop
# ---------------------------------------------------------------------------

def test_object_crop_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 OBJE
        2 FILE photo.jpg
        2 CROP
        3 TOP 10
        3 LEFT 20
        0 TRLR
    """), ["object_crop"])
    assert "CROP" not in content
    assert "photo.jpg" in content
    assert stats["object_crop"].removed == 1


# ---------------------------------------------------------------------------
# change_date / create_date
# ---------------------------------------------------------------------------

def test_change_date_removed():
    # MacFamilyTree exports CHAN at level 2 (under a sub-record)
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 BIRT
        2 CHAN
        3 DATE 1 JAN 2020
        0 TRLR
    """), ["change_date"])
    assert "CHAN" not in content
    assert stats["change_date"].removed == 1


def test_create_date_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 BIRT
        2 CREA
        3 DATE 1 JAN 2020
        0 TRLR
    """), ["create_date"])
    assert "CREA" not in content
    assert stats["create_date"].removed == 1


# ---------------------------------------------------------------------------
# indi_race
# ---------------------------------------------------------------------------

def test_indi_race_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 RACE White
        0 TRLR
    """), ["indi_race"])
    assert "RACE" not in content
    assert stats["indi_race"].removed == 1


# ---------------------------------------------------------------------------
# sto / bkm
# ---------------------------------------------------------------------------

def test_sto_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 _STO SomeValue
        0 TRLR
    """), ["sto"])
    assert "_STO" not in content
    assert stats["sto"].removed == 1


def test_bkm_removed():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME John /Smith/
        1 _BKM SomeValue
        0 TRLR
    """), ["bkm"])
    assert "_BKM" not in content
    assert stats["bkm"].removed == 1
