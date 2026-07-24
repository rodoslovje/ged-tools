import io
import os
import tempfile
import textwrap

from gedcom.parser import Parser

from tools.gedcom_merge import (
    _Person,
    find_potential_duplicates,
    report_duplicates,
)

_FILE_A = textwrap.dedent("""\
    0 HEAD
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME Aleš /Glavnik/
    1 SEX M
    1 BIRT
    2 DATE 3 JUN 1970
    0 @I2@ INDI
    1 NAME Marija /Božič/
    1 SEX F
    1 BIRT
    2 DATE ABT 1902
    0 @I3@ INDI
    1 NAME Janez /Novak/
    1 SEX M
    0 @I4@ INDI
    1 NAME N /NN/
    0 @I5@ INDI
    1 NAME Ivan /Kos/
    1 SEX M
    1 BIRT
    2 DATE 1 JAN 1880
    0 TRLR
""")

_FILE_B = textwrap.dedent("""\
    0 HEAD
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME Ales /GLAVNIK/
    1 SEX M
    1 BIRT
    2 DATE 3 JUN 1970
    0 @I2@ INDI
    1 NAME Marija /Bozic/
    1 SEX F
    1 BIRT
    2 DATE 1902
    0 @I3@ INDI
    1 NAME Janez /Novak/
    1 SEX M
    0 @I4@ INDI
    1 NAME N /NN/
    0 @I5@ INDI
    1 NAME Ivan /Kos/
    1 SEX F
    1 BIRT
    2 DATE 1 JAN 1880
    0 TRLR
""")


def _people(content: str, label: str) -> list[_Person]:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        parser = Parser()
        parser.parse_file(path, strict=False)
        return [
            _Person(el, label)
            for el in parser.get_root_child_elements()
            if el.get_tag() == "INDI"
        ]
    finally:
        os.unlink(path)


def _all_people() -> list[_Person]:
    return _people(_FILE_A, "a.ged") + _people(_FILE_B, "b.ged")


def _by_pointer(matches):
    return {(a.pointer, b.pointer): (conf, reason) for conf, reason, a, b in matches}


def test_identical_birth_date_scores_full_confidence():
    matches = _by_pointer(find_potential_duplicates(_all_people(), 30))
    conf, reason = matches[("@I1@", "@I1@")]
    assert conf == 100
    assert "identical birth date" in reason


def test_diacritics_and_case_are_normalized():
    # Marija Božič / Marija Bozic: same birth year via ABT 1902 vs 1902.
    matches = _by_pointer(find_potential_duplicates(_all_people(), 30))
    conf, reason = matches[("@I2@", "@I2@")]
    assert conf == 85
    assert "same birth year" in reason


def test_same_name_without_dates_is_low_confidence():
    matches = find_potential_duplicates(_all_people(), 30)
    janez = [m for m in matches if m[2].display.startswith("Janez")]
    assert len(janez) == 1
    assert janez[0][0] == 30


def test_conflicting_sex_is_never_reported():
    matches = find_potential_duplicates(_all_people(), 0)
    assert not [m for m in matches if m[2].display.startswith("Ivan")]


def test_placeholder_names_are_ignored():
    matches = find_potential_duplicates(_all_people(), 0)
    assert not [m for m in matches if "NN" in m[2].display]


def test_min_confidence_filters_weak_pairs():
    strong = find_potential_duplicates(_all_people(), 50)
    assert all(m[0] >= 50 for m in strong)
    assert not [m for m in strong if m[2].display.startswith("Janez")]


def test_report_lists_cross_file_pairs():
    out = io.StringIO()
    report_duplicates(find_potential_duplicates(_all_people(), 50), out)
    text = out.getvalue()
    assert "potential duplicate pair(s) detected" in text
    assert "across different files" in text
    assert "[a.ged]" in text and "[b.ged]" in text


def test_no_matches_reports_clean():
    out = io.StringIO()
    report_duplicates([], out)
    assert "No potential duplicates detected." in out.getvalue()
