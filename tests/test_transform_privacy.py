import os
import tempfile
import textwrap
import datetime
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
    assert "1 NAME private" in content
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
    assert "1 NAME private" in content
    assert stats["living100y_private"].transformed == 1


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
    assert "1 NAME private" in content
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
    assert "1 NAME private" in content
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
