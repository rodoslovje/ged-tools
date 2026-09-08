import os
import tempfile
import textwrap
import datetime
import pytest
from tools.gedcom_cleaner import process_file

_CURR = datetime.date.today().year
_OLD = _CURR - 120   # clearly historical
_NEW = _CURR - 30    # clearly living
_DEATH_OLD = _CURR - 25   # died >20 years ago
_DEATH_NEW = _CURR - 10   # died <20 years ago


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
# living100y_private
# ---------------------------------------------------------------------------

def test_living100y_private_anonymises_recent_birth():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Janez /Novak/
        1 SEX M
        1 BIRT
        2 DATE {_NEW}
        1 OCCU Farmer
        0 TRLR
    """), ["living100y_private"])
    assert "1 NAME <private>" in content
    assert "Janez" not in content
    assert "OCCU" not in content
    assert "1 SEX M" in content
    assert stats["living100y_private"].transformed == 1


def test_living100y_private_leaves_historical():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Janez /Novak/
        1 BIRT
        2 DATE {_OLD}
        0 TRLR
    """), ["living100y_private"])
    assert "Janez /Novak/" in content
    assert stats["living100y_private"].transformed == 0


def test_living100y_private_leaves_person_with_death():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Kos/
        1 BIRT
        2 DATE {_NEW}
        1 DEAT Y
        0 TRLR
    """), ["living100y_private"])
    assert "Ana /Kos/" in content
    assert stats["living100y_private"].transformed == 0


def test_living100y_private_skips_unknown_birth():
    """Person with no birth date and no death — not anonymised by living100y_private."""
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Unknown /Person/
        0 TRLR
    """), ["living100y_private"])
    assert "Unknown /Person/" in content
    assert stats["living100y_private"].transformed == 0


def test_living100y_private_named_living():
    """Person whose name value is literally 'living' is always anonymised."""
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME living
        1 SEX F
        0 TRLR
    """), ["living100y_private"])
    assert "1 NAME <private>" in content
    assert stats["living100y_private"].transformed == 1


@pytest.mark.parametrize("marker", ["Private", "private", "PRIVATE", "Privat", "privat"])
def test_living100y_private_named_private(marker):
    """A name marked 'Private'/'Privat' (any case) is always anonymised,
    even with no dates."""
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME {marker}
        1 SEX F
        0 TRLR
    """), ["living100y_private"])
    assert "1 NAME <private>" in content
    assert stats["living100y_private"].transformed == 1


def test_living100y_private_marker_in_surname():
    """The privacy word inside a slash-surname also triggers anonymisation."""
    content, stats = _run(textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Private/
        1 SEX F
        0 TRLR
    """), ["living100y_private"])
    assert "1 NAME <private>" in content
    assert stats["living100y_private"].transformed == 1


def test_living100y_private_privatnik_not_a_marker():
    """A real name that merely contains 'privat' as a substring (e.g. the
    surname 'Privatnik') is NOT treated as a privacy marker."""
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Privatnik/
        1 BIRT
        2 DATE {_OLD}
        0 TRLR
    """), ["living100y_private"])
    assert "1 NAME Ana /Privatnik/" in content
    assert stats["living100y_private"].transformed == 0


def test_living100y_private_uses_baptism_as_fallback():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Janez /Novak/
        1 BAPM
        2 DATE {_NEW}
        0 TRLR
    """), ["living100y_private"])
    assert "1 NAME <private>" in content
    assert stats["living100y_private"].transformed == 1


# ---------------------------------------------------------------------------
# living100y_name_only
# ---------------------------------------------------------------------------

def test_living100y_name_only_shortens_given_name():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Luka /Renko/
        2 GIVN Luka
        2 SURN Renko
        1 SEX M
        1 BIRT
        2 DATE {_NEW}
        1 OCCU Farmer
        0 TRLR
    """), ["living100y_name_only"])
    assert "L. /Renko/" in content
    assert "GIVN L." in content
    assert "OCCU" not in content
    assert "1 SEX M" in content
    assert stats["living100y_name_only"].transformed == 1


def test_living100y_name_only_shortens_multiple_given_names():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ronja Sofija /Renko/
        2 GIVN Ronja Sofija
        2 SURN Renko
        1 BIRT
        2 DATE {_NEW}
        0 TRLR
    """), ["living100y_name_only"])
    assert "R. S. /Renko/" in content
    assert "GIVN R. S." in content
    assert stats["living100y_name_only"].transformed == 1


def test_living100y_name_only_leaves_historical():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Janez /Novak/
        1 BIRT
        2 DATE {_OLD}
        0 TRLR
    """), ["living100y_name_only"])
    assert "Janez /Novak/" in content
    assert stats["living100y_name_only"].transformed == 0


# ---------------------------------------------------------------------------
# died20y_private
# ---------------------------------------------------------------------------

def test_died20y_private_anonymises_recent_death():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Kos/
        1 SEX F
        1 DEAT
        2 DATE {_DEATH_NEW}
        1 OCCU Teacher
        0 TRLR
    """), ["died20y_private"])
    assert "1 NAME <private>" in content
    assert "Ana" not in content
    assert "OCCU" not in content
    assert "1 SEX F" in content
    assert stats["died20y_private"].transformed == 1


def test_died20y_private_leaves_old_death():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Kos/
        1 DEAT
        2 DATE {_DEATH_OLD}
        0 TRLR
    """), ["died20y_private"])
    assert "Ana /Kos/" in content
    assert stats["died20y_private"].transformed == 0


def test_died20y_private_ignores_no_death():
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Kos/
        1 BIRT
        2 DATE {_NEW}
        0 TRLR
    """), ["died20y_private"])
    assert "Ana /Kos/" in content
    assert stats["died20y_private"].transformed == 0


def test_died20y_private_ignores_death_without_date():
    """DEAT with no date — died20y_private must not anonymise (date required)."""
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Kos/
        1 DEAT Y
        0 TRLR
    """), ["died20y_private"])
    assert "Ana /Kos/" in content
    assert stats["died20y_private"].transformed == 0


def test_died20y_private_uses_earliest_death_date():
    """When DEAT and BURI both have dates, the earliest is used for the 20y check."""
    content, stats = _run(textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Ana /Kos/
        1 DEAT
        2 DATE {_DEATH_NEW}
        1 BURI
        2 DATE {_DEATH_OLD}
        0 TRLR
    """), ["died20y_private"])
    # earliest is _DEATH_OLD (>20 years) → not anonymised
    assert "Ana /Kos/" in content
    assert stats["died20y_private"].transformed == 0


# ---------------------------------------------------------------------------
# fam_partner_private
# ---------------------------------------------------------------------------

_FAM_BOTH_LIVING = textwrap.dedent(f"""\
    0 HEAD
    1 GEDC
    2 VERS 5.5.1
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME Janez /Novak/
    1 SEX M
    1 BIRT
    2 DATE 1 JAN {_NEW}
    1 FAMS @F1@
    0 @I2@ INDI
    1 NAME Marija /Kranjc/
    1 SEX F
    1 BIRT
    2 DATE 1 JAN {_NEW}
    1 FAMS @F1@
    0 @I3@ INDI
    1 NAME Edita /Krč/
    1 SEX F
    1 BIRT
    2 DATE 1 JAN {_OLD}
    0 @F1@ FAM
    1 MARR
    2 DATE 5 MAY 1990
    2 PLAC Kranj, Slovenia
    2 ADDR Sv. Martin
    2 AGNC župnija Šmartin
    2 ASSO @I3@
    3 ROLE WITN
    2 NOTE Poročna knjiga
    3 CONT priči: Edita Krč
    2 SOUR @S1@
    3 PAGE stran 14
    1 EVEN
    2 TYPE Civil Partnership
    2 DATE 18 APR 1998
    2 PLAC Preddvor, Slovenia
    3 MAP
    4 LATI N46.30247
    4 LONG E14.41632
    1 HUSB @I1@
    1 WIFE @I2@
    0 @S1@ SOUR
    1 TITL Poročna knjiga
    0 TRLR
""")


@pytest.mark.parametrize(
    "transformer",
    ["living100y_private", "living100y_initials", "living100y_name_only", "living75y_private"],
)
def test_fam_partner_private_redacts_family_when_both_anonymised(transformer):
    out, stats = _run(_FAM_BOTH_LIVING, [transformer, "fam_partner_private"])
    # family record and its links survive
    assert "0 @F1@ FAM" in out
    assert "1 HUSB @I1@" in out
    assert "1 WIFE @I2@" in out
    assert "1 FAMS @F1@" in out
    # but every event detail is redacted
    for leaked in ("5 MAY 1990", "Kranj, Slovenia", "Sv. Martin", "Šmartin", "ASSO",
                   "Edita Krč", "18 APR 1998", "Preddvor", "N46.30247", "3 CONT"):
        assert leaked not in out, leaked
    assert "2 TYPE Civil Partnership" in out
    assert "2 DATE <private>" in out
    assert stats["fam_partner_private"].transformed == 1
    # historical witness untouched
    assert "Edita /Krč/" in out


def test_fam_partner_private_initials_redacts_mixed_family():
    # Make the wife historical so the family is mixed (one private spouse).
    ged = _FAM_BOTH_LIVING.replace(
        f"1 NAME Marija /Kranjc/\n    1 SEX F\n    1 BIRT\n    2 DATE 1 JAN {_NEW}".replace("    ", ""),
        f"1 NAME Marija /Kranjc/\n1 SEX F\n1 BIRT\n2 DATE 1 JAN {_OLD}\n1 DEAT\n2 DATE 1 JAN {_OLD + 60}",
    )
    assert f"DATE 1 JAN {_OLD + 60}" in ged, "fixture replacement failed"
    out, stats = _run(ged, ["living100y_initials", "fam_partner_private"])
    assert "1 NAME J. /N./" in out
    assert "Marija /Kranjc/" in out
    assert "@F1@ FAM" in out
    assert stats["fam_partner_private"].transformed == 1
    # every MARR / EVEN detail is gone, TYPE survives
    for leaked in (
        "5 MAY 1990",
        "Kranj, Slovenia",
        "Sv. Martin",
        "Šmartin",
        "ASSO",
        "Edita Krč",
        "@S1@\n3 PAGE",
        "18 APR 1998",
        "Preddvor",
        "N46.30247",
    ):
        assert leaked not in out, leaked
    assert "2 TYPE Civil Partnership" in out
    assert "2 DATE <private>" in out
    assert "2 PLAC <private>" in out
    assert "2 NOTE <private>" in out
    assert "<private>\n3 " not in out  # no sub-records under redacted fields
    assert "3 CONT" not in out
    # SOUR record itself is untouched
    assert "0 @S1@ SOUR" in out


def test_fam_partner_private_died20y_counts_as_private():
    ged = _FAM_BOTH_LIVING.replace(
        f"2 DATE 1 JAN {_NEW}\n1 FAMS @F1@\n0 @I2@",
        f"2 DATE 1 JAN {_OLD}\n1 DEAT\n2 DATE 1 JAN {_DEATH_NEW}\n1 FAMS @F1@\n0 @I2@",
    ).replace(
        f"1 NAME Marija /Kranjc/\n1 SEX F\n1 BIRT\n2 DATE 1 JAN {_NEW}",
        f"1 NAME Marija /Kranjc/\n1 SEX F\n1 BIRT\n2 DATE 1 JAN {_OLD}\n1 DEAT\n2 DATE 1 JAN {_DEATH_NEW}",
    )
    assert ged.count(f"1 JAN {_DEATH_NEW}") == 2, "fixture replacement failed"
    out, stats = _run(ged, ["died20y_private", "fam_partner_private"])
    assert stats["died20y_private"].transformed == 2
    assert "0 @F1@ FAM" in out
    assert "5 MAY 1990" not in out
    assert "2 DATE <private>" in out
    assert stats["fam_partner_private"].transformed == 1


def test_fam_partner_private_leaves_historical_family():
    ged = _FAM_BOTH_LIVING.replace(f"2 DATE 1 JAN {_NEW}", f"2 DATE 1 JAN {_OLD}")
    out, stats = _run(ged, ["living100y_initials", "fam_partner_private"])
    assert stats["living100y_initials"].transformed == 0
    assert stats["fam_partner_private"].transformed == 0
    assert "2 DATE 5 MAY 1990" in out
    assert "2 ASSO @I3@" in out


def test_fam_partner_private_recognises_pre_existing_private_name():
    ged = _FAM_BOTH_LIVING.replace("1 NAME Janez /Novak/", "1 NAME <private>").replace(
        "1 NAME Marija /Kranjc/", "1 NAME <private>"
    )
    out, stats = _run(ged, ["fam_partner_private"])
    assert "0 @F1@ FAM" in out
    assert "5 MAY 1990" not in out
    assert stats["fam_partner_private"].transformed == 1
