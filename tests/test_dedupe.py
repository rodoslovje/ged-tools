import os
import tempfile
import textwrap

from gedcom.parser import Parser

from tools.gedcom_dedupe import find_duplicates, merge_and_redirect, _serialize

_SAMPLE = textwrap.dedent("""\
    0 HEAD
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME Aleš /Glavnik/
    1 SEX M
    1 BIRT
    2 DATE 3 JUN 1970
    1 FAMS @F1@
    0 @I2@ INDI
    1 NAME Ales /GLAVNIK/
    1 SEX M
    1 BIRT
    2 DATE 3 JUN 1970
    1 FAMC @F2@
    0 @I3@ INDI
    1 NAME Ales /Glavnik/
    1 SEX M
    1 BIRT
    2 DATE 1970
    0 @I4@ INDI
    1 NAME Marija /Kos/
    1 SEX F
    1 BIRT
    2 DATE 12 MAY 1900
    0 @F1@ FAM
    1 HUSB @I1@
    1 WIFE @I4@
    0 @F2@ FAM
    1 CHIL @I2@
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


def test_matching_ignores_case_and_diacritics():
    groups = find_duplicates(_parse(_SAMPLE))
    assert len(groups) == 1
    assert [el.get_pointer() for el in groups[0]] == ["@I1@", "@I2@", "@I3@"]


def test_grouping_is_transitive():
    # @I3@ only shares a birth year with the other two, yet joins the same group.
    groups = find_duplicates(_parse(_SAMPLE))
    assert len(groups[0]) == 3


def test_min_confidence_100_requires_full_date_match():
    groups = find_duplicates(_parse(_SAMPLE), min_confidence=100)
    assert [el.get_pointer() for el in groups[0]] == ["@I1@", "@I2@"]


def test_unique_individual_is_not_grouped():
    groups = find_duplicates(_parse(_SAMPLE))
    assert not [g for g in groups if any(el.get_pointer() == "@I4@" for el in g)]


def test_merge_redirects_references_and_keeps_family_links():
    parser = _parse(_SAMPLE)
    groups = find_duplicates(parser)
    delete_set = merge_and_redirect(groups, parser)

    assert delete_set == {"@I2@", "@I3@"}
    out = "".join(
        _serialize(el)
        for el in parser.get_root_child_elements()
        if el.get_pointer() not in delete_set
    )
    # The child pointer in @F2@ now targets the surviving master record.
    assert "1 CHIL @I1@" in out
    assert "@I2@" not in out.split("0 @F2@ FAM")[1]
    # The master inherited the duplicate's FAMC link.
    master = out.split("0 @I1@ INDI")[1].split("0 @")[0]
    assert "1 FAMS @F1@" in master
    assert "1 FAMC @F2@" in master
    # Duplicate content is preserved as a NOTE for manual review.
    assert "Merged from duplicate record @I2@." in master


def test_output_retains_trailer():
    parser = _parse(_SAMPLE)
    out = "".join(_serialize(el) for el in parser.get_root_child_elements())
    assert out.rstrip().endswith("0 TRLR")
