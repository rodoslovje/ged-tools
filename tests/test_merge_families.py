import os
import tempfile
import textwrap

from gedcom.parser import Parser

from tools.gedcom_dedupe import _serialize, merge_families

# Two chapters recorded the same couple: @F1@ and @F2@ have the same spouses
# and overlapping children, so they merge.  @F4@ is a different couple sharing
# the husband and must survive.  @F3@ names only the husband, and because that
# husband belongs to two couples it is ambiguous -- it must be left alone.
_SAMPLE = textwrap.dedent("""\
    0 HEAD
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME Blaž /Weisbacher/
    1 FAMS @F1@
    1 FAMS @F2@
    1 FAMS @F3@
    1 FAMS @F4@
    0 @I2@ INDI
    1 NAME Marija /Vogrinec/
    1 FAMS @F1@
    1 FAMS @F2@
    0 @I3@ INDI
    1 NAME Ana /Cafuta/
    1 FAMS @F4@
    0 @I4@ INDI
    1 NAME Ana /Weisbacher/
    1 FAMC @F1@
    0 @I5@ INDI
    1 NAME Ludmila /Weisbacher/
    1 FAMC @F2@
    0 @F1@ FAM
    1 HUSB @I1@
    1 WIFE @I2@
    1 MARR
    2 DATE 27 JAN 1845
    1 CHIL @I4@
    0 @F2@ FAM
    1 HUSB @I1@
    1 WIFE @I2@
    1 CHIL @I4@
    1 CHIL @I5@
    0 @F3@ FAM
    1 HUSB @I1@
    0 @F4@ FAM
    1 HUSB @I1@
    1 WIFE @I3@
    0 TRLR
""")


def _parse(content: str) -> Parser:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        parser = Parser()
        parser.parse_file(path, strict=False)
        return parser
    finally:
        os.unlink(path)


def _write(parser: Parser, deleted: set) -> str:
    return "".join(_serialize(el) for el in parser.get_root_child_elements()
                   if el.get_pointer() not in deleted)


def test_merges_records_for_the_same_couple():
    parser = _parse(_SAMPLE)
    assert merge_families(parser) == {"@F2@"}


def test_ambiguous_half_known_family_is_left_alone():
    """@F3@ names only @I1@, who has two wives -- absorbing it would guess."""
    parser = _parse(_SAMPLE)
    out = _write(parser, merge_families(parser))
    assert "0 @F3@ FAM" in out


def test_half_known_family_is_absorbed_when_unambiguous():
    sample = _SAMPLE.replace("1 FAMS @F4@\n", "", 1).replace(
        "0 @I3@ INDI\n1 NAME Ana /Cafuta/\n1 FAMS @F4@\n", ""
    ).replace("0 @F4@ FAM\n1 HUSB @I1@\n1 WIFE @I3@\n", "")
    parser = _parse(sample)
    deleted = merge_families(parser)
    assert deleted == {"@F2@", "@F3@"}


def test_children_are_unioned_without_duplicates():
    parser = _parse(_SAMPLE)
    out = _write(parser, merge_families(parser))
    fam = out.split("0 @F1@ FAM")[1].split("\n0 ")[0]
    assert fam.count("1 CHIL @I4@") == 1
    assert "1 CHIL @I5@" in fam


def test_marriage_facts_are_kept():
    parser = _parse(_SAMPLE)
    out = _write(parser, merge_families(parser))
    assert "2 DATE 27 JAN 1845" in out


def test_references_are_redirected_and_deduplicated():
    parser = _parse(_SAMPLE)
    out = _write(parser, merge_families(parser))
    indi = out.split("0 @I1@ INDI")[1].split("\n0 ")[0]
    assert indi.count("1 FAMS @F1@") == 1
    assert "@F2@" not in out
    assert indi.count("1 FAMS @F4@") == 1


def test_distinct_couple_sharing_a_spouse_is_untouched():
    parser = _parse(_SAMPLE)
    out = _write(parser, merge_families(parser))
    assert "0 @F4@ FAM" in out
    assert "1 WIFE @I3@" in out
