import os
import tempfile
import textwrap
from gedcom.parser import Parser
from tools.gedcom_to_json import build_sources_dict, get_event_data, _link_from_subelement


def _roots(gedcom_str: str):
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(gedcom_str))
    try:
        parser = Parser()
        parser.parse_file(path, False)
        return list(parser.get_root_child_elements())
    finally:
        os.unlink(path)


def _fam(gedcom_str: str):
    return next(e for e in _roots(gedcom_str) if e.get_tag() == "FAM")


# ---------------------------------------------------------------------------
# EVEN / TYPE Marriage as MARR
# ---------------------------------------------------------------------------

def test_even_type_marriage_used_as_marr():
    fam = _fam("""\
        0 HEAD
        0 @F1@ FAM
        1 EVEN
        2 TYPE Marriage
        2 DATE 9 NOV 1840
        2 PLAC Surgern
        0 TRLR
    """)
    assert get_event_data(fam, "MARR")[:2] == ("9 NOV 1840", "Surgern")


def test_real_marr_wins_over_even_marriage():
    fam = _fam("""\
        0 HEAD
        0 @F1@ FAM
        1 EVEN
        2 TYPE Marriage
        2 DATE EST 8 MAY 1874
        1 MARR
        2 DATE ABT 8 FEB 1874
        0 TRLR
    """)
    assert get_event_data(fam, "MARR")[0] == "ABT 8 FEB 1874"


def test_empty_marr_skipped_for_even_marriage():
    fam = _fam("""\
        0 HEAD
        0 @F1@ FAM
        1 MARR Y
        1 EVEN
        2 TYPE Marriage
        2 DATE 9 OCT 1848
        2 PLAC Osilnica
        0 TRLR
    """)
    assert get_event_data(fam, "MARR")[:2] == ("9 OCT 1848", "Osilnica")


def test_other_even_types_ignored():
    fam = _fam("""\
        0 HEAD
        0 @F1@ FAM
        1 EVEN
        2 TYPE Oklici
        2 DATE 1 OCT 1848
        0 TRLR
    """)
    assert get_event_data(fam, "MARR") == ("", "", [])


# ---------------------------------------------------------------------------
# Source URLs: NOTE on the source record, citation URL precedence
# ---------------------------------------------------------------------------

_URL = "https://data.matricula-online.eu/sl/slovenia/maribor/mala-nedelja/01465/?pg="


def test_source_note_url_with_citation_page():
    roots = _roots(f"""\
        0 HEAD
        0 @I1@ INDI
        1 BIRT
        2 SOUR @S1@
        3 PAGE 78
        0 @S1@ SOUR
        1 TITL Matična krstna knjiga Mala Nedelja 1894
        1 NOTE {_URL}1
        0 TRLR
    """)
    sources = build_sources_dict(roots)
    cite = roots[1].get_child_elements()[0].get_child_elements()[0]
    assert _link_from_subelement(cite, sources) == [_URL + "78"]


def test_citation_url_wins_over_source_note_url():
    roots = _roots(f"""\
        0 HEAD
        0 @I1@ INDI
        1 BIRT
        2 SOUR @S1@
        3 PAGE {_URL}19
        0 @S1@ SOUR
        1 TITL Matična krstna knjiga
        1 NOTE https://data.matricula-online.eu/sl/slovenia/ljubljana/semic/02150/?pg=140
        0 TRLR
    """)
    sources = build_sources_dict(roots)
    cite = roots[1].get_child_elements()[0].get_child_elements()[0]
    assert _link_from_subelement(cite, sources) == [_URL + "19"]


# ---------------------------------------------------------------------------
# Marriage on a spouse's INDI (EVEN / TYPE MARR) as fallback for the FAM
# ---------------------------------------------------------------------------

def _marriage_of_f1(gedcom_str: str):
    """Run the full conversion and return the marriage of the @I1@ + @I2@ family."""
    import json
    from tools import gedcom_to_json as g
    with tempfile.TemporaryDirectory() as in_dir, tempfile.TemporaryDirectory() as out_dir:
        with open(os.path.join(in_dir, "Test.ged"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(gedcom_str))
        g._process_one_file("Test.ged", True, {}, in_dir, out_dir)
        with open(os.path.join(out_dir, "Test-families.json"), encoding="utf-8") as f:
            fams = json.load(f)
    return next(r["marriage"] for r in fams if r["wife"]["id"] == "@I2@")


_INDI_MARR = """\
    0 HEAD
    0 @I1@ INDI
    1 NAME Janez /Novak/
    1 EVEN
    2 TYPE {type}
    2 DATE 3 FEB 1879
    2 PLAC Ig
    1 FAMS @F1@
    {extra_fams}
    0 @I2@ INDI
    1 NAME Marija /Kos/
    1 FAMS @F1@
    0 @F1@ FAM
    1 HUSB @I1@
    1 WIFE @I2@
    {fam_marr}
    0 @F2@ FAM
    1 HUSB @I1@
    0 TRLR
"""


def test_indi_marriage_used_when_single_family():
    marriage = _marriage_of_f1(_INDI_MARR.format(type="MARR", extra_fams="", fam_marr=""))
    assert marriage == {"date": "3 FEB 1879", "place": "Ig"}


def test_indi_marriage_poroka():
    marriage = _marriage_of_f1(_INDI_MARR.format(type="Poroka", extra_fams="", fam_marr=""))
    assert marriage["date"] == "3 FEB 1879"


def test_indi_marriage_ignored_when_several_families():
    marriage = _marriage_of_f1(_INDI_MARR.format(type="MARR", extra_fams="1 FAMS @F2@", fam_marr=""))
    assert marriage == {"date": "", "place": ""}


def test_fam_marriage_wins_over_indi_marriage():
    marriage = _marriage_of_f1(_INDI_MARR.format(
        type="MARR", extra_fams="", fam_marr="1 MARR\n    2 DATE 1 JAN 1880"))
    assert marriage["date"] == "1 JAN 1880"


def test_spouse_marriage_with_date_preferred():
    marriage = _marriage_of_f1("""\
        0 HEAD
        0 @I1@ INDI
        1 NAME Janez /Novak/
        1 EVEN
        2 TYPE MARR
        2 PLAC Ig
        1 FAMS @F1@
        0 @I2@ INDI
        1 NAME Marija /Kos/
        1 EVEN
        2 TYPE Marriage
        2 DATE 3 FEB 1879
        2 PLAC Ig
        1 FAMS @F1@
        0 @F1@ FAM
        1 HUSB @I1@
        1 WIFE @I2@
        0 TRLR
    """)
    assert marriage == {"date": "3 FEB 1879", "place": "Ig"}
