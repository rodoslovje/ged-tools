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


# ---------------------------------------------------------------------------
# asso_role_rela
# ---------------------------------------------------------------------------

_ASSO_GED = textwrap.dedent("""\
    0 HEAD
    1 GEDC
    2 VERS 7.0
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME Janez /Novak/
    1 SEX M
    1 BAPM
    2 DATE 1 JAN 1900
    2 ASSO @I2@
    3 ROLE GODP
    2 ASSO @I3@
    3 ROLE GODP
    2 ASSO @I4@
    3 ROLE GODP
    2 ASSO @I5@
    3 ROLE WITN
    2 ASSO @I5@
    3 ROLE CLERGY
    1 ASSO @I5@
    2 ROLE OTHER
    3 PHRASE Neighbor
    1 ASSO @I5@
    2 ROLE OTHER
    1 ASSO @I5@
    2 ROLE FRIEND
    3 PHRASE best mate
    1 SOUR @S1@
    2 DATA
    3 EVEN Smart Matching
    4 ROLE 1500024
    0 @I2@ INDI
    1 NAME Pavel /Jekovec/
    1 SEX M
    0 @I3@ INDI
    1 NAME Marija /Jekovec/
    1 SEX F
    0 @I4@ INDI
    1 NAME NN /Jekovec/
    0 @I5@ INDI
    1 NAME Ciril /Berglez/
    1 SEX M
    0 @S1@ SOUR
    1 TITL MyHeritage
    0 TRLR
""")


def test_asso_role_rela_maps_roles_to_webtrees_words():
    content, stats = _run(_ASSO_GED, ["asso_role_rela"])
    # event-level associations become webtrees' _ASSO
    assert "2 _ASSO @I2@\n3 RELA godfather\n" in content
    assert "2 _ASSO @I3@\n3 RELA godmother\n" in content
    assert "2 _ASSO @I4@\n3 RELA godparent\n" in content
    assert "2 _ASSO @I5@\n3 RELA witness\n" in content
    assert "2 _ASSO @I5@\n3 RELA priest\n" in content
    assert "2 ASSO " not in content
    assert stats["asso_role_rela"].transformed == 8


def test_asso_role_rela_other_uses_phrase_and_drops_phrase():
    content, _ = _run(_ASSO_GED, ["asso_role_rela"])
    # record-level associations keep the standard ASSO tag
    assert "1 ASSO @I5@\n2 RELA Neighbor\n" in content
    assert "1 ASSO @I5@\n2 RELA other\n" in content
    # known enumeration wins over PHRASE, PHRASE never survives
    assert "1 ASSO @I5@\n2 RELA friend\n" in content
    assert "1 _ASSO" not in content
    assert "PHRASE" not in content
    assert "ROLE GODP" not in content and "ROLE WITN" not in content


def test_asso_role_rela_leaves_citation_role_alone():
    content, _ = _run(_ASSO_GED, ["asso_role_rela"])
    assert "3 EVEN Smart Matching\n4 ROLE 1500024\n" in content


def test_asso_role_rela_leaves_existing_rela_alone():
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Janez /Novak/
        1 ASSO @I2@
        2 RELA Witness
        0 @I2@ INDI
        1 NAME Ana /Kos/
        0 TRLR
    """), ["asso_role_rela"])
    assert "2 RELA Witness" in content
    assert stats["asso_role_rela"].processed == 0
