import os
import tempfile
import textwrap
from tools.gedcom_cleaner import process_file

_SAMPLE = textwrap.dedent("""\
    0 HEAD
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME John /Smith/
    0 @I2@ INDI
    1 NAME ___
    0 @I3@ INDI
    1 NAME
    0 @I4@ INDI
    0 @I5@ INDI
    1 NAME Jane /Doe/
    0 @F1@ FAM
    1 HUSB @I1@
    1 WIFE @I5@
    0 @F2@ FAM
    1 HUSB @I2@
    1 WIFE @I3@
    0 @F3@ FAM
    1 HUSB @I4@
    0 @F4@ FAM
    1 HUSB @I1@
    1 WIFE @I2@
    0 TRLR
""")


def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_noname_indi_removes_nameless():
    inp = _write_tmp(_SAMPLE)
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(
            inp, out,
            cleaners=["name_placeholder"],
            strippers=["noname_indi"],
            transformers=[],
            warn=False,
        )
        # @I2@ has "___" (cleared by name_placeholder), @I3@ has empty NAME, @I4@ has no NAME
        assert strip_stats["noname_indi"].removed == 3

        content = open(out, encoding="utf-8").read()
        assert "0 @I1@ INDI" in content   # named — kept
        assert "0 @I5@ INDI" in content   # named — kept
        assert "0 @I2@ INDI" not in content
        assert "0 @I3@ INDI" not in content
        assert "0 @I4@ INDI" not in content
        # FAM records untouched by this stripper
        assert "@F1@" in content
        assert "@F2@" in content
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_noname_fam_removes_all_nameless():
    inp = _write_tmp(_SAMPLE)
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(
            inp, out,
            cleaners=["name_placeholder"],
            strippers=["noname_indi", "noname_fam"],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()
        # @F1@: I1(named) + I5(named) → kept
        assert "@F1@" in content
        # @F2@: I2(nameless) + I3(nameless) → removed
        assert "@F2@" not in content
        # @F3@: I4(nameless, no WIFE) → removed
        assert "@F3@" not in content
        # @F4@: I1(named) + I2(nameless) → kept (one named spouse is enough)
        assert "@F4@" in content

        assert strip_stats["noname_fam"].removed == 2
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_noname_fam_no_refs_removed():
    """A FAM with no HUSB or WIFE is removed."""
    sample = textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @F1@ FAM
        1 MARR Y
        0 TRLR
    """)
    inp = _write_tmp(sample)
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(
            inp, out,
            cleaners=[],
            strippers=["noname_fam"],
            transformers=[],
            warn=False,
        )
        assert strip_stats["noname_fam"].removed == 1
        assert "@F1@" not in open(out, encoding="utf-8").read()
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_noname_fam_without_noname_indi():
    """noname_fam alone resolves INDI names from the live tree."""
    inp = _write_tmp(_SAMPLE)
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(
            inp, out,
            cleaners=["name_placeholder"],
            strippers=["noname_fam"],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()
        # @F2@: I2(cleared by cleaner) + I3(empty) → removed
        assert "@F2@" not in content
        # @F3@: I4(no name) → removed
        assert "@F3@" not in content
        # @F1@ and @F4@ kept
        assert "@F1@" in content
        assert "@F4@" in content
    finally:
        os.unlink(inp)
        os.unlink(out)


# ---------------------------------------------------------------------------
# living stripper tests
# ---------------------------------------------------------------------------

import datetime

_CURRENT_YEAR = datetime.date.today().year
_OLD_YEAR = _CURRENT_YEAR - 115   # definitely dead
_NEW_YEAR = _CURRENT_YEAR - 30    # definitely living


def _living_sample(old_year=_OLD_YEAR, new_year=_NEW_YEAR) -> str:
    return textwrap.dedent(f"""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Old /Dead/
        1 BIRT
        2 DATE {old_year}
        1 DEAT
        2 DATE {old_year + 70}
        0 @I2@ INDI
        1 NAME Young /Living/
        1 BIRT
        2 DATE {new_year}
        0 @I3@ INDI
        1 NAME No /Birth/
        0 @I4@ INDI
        1 NAME Old /NoDeat/
        1 BIRT
        2 DATE {old_year}
        0 @F1@ FAM
        1 HUSB @I1@
        1 WIFE @I2@
        0 @F2@ FAM
        1 HUSB @I2@
        1 WIFE @I3@
        0 @F3@ FAM
        1 HUSB @I1@
        1 WIFE @I4@
        0 TRLR
    """)


def test_living_strips_recent_and_unknown_birth():
    inp = _write_tmp(_living_sample())
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(
            inp, out,
            cleaners=[],
            strippers=["living"],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()
        # I1: old birth + DEAT → kept (confirmed dead)
        assert "0 @I1@ INDI" in content
        # I2: recent birth, no DEAT → removed (living)
        assert "0 @I2@ INDI" not in content
        # I3: no birth, no DEAT → removed (unknown = assume living)
        assert "0 @I3@ INDI" not in content
        # I4: old birth, no DEAT → kept (born before cutoff = presumed dead)
        assert "0 @I4@ INDI" in content
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_living_strips_fam_with_any_living_spouse():
    inp = _write_tmp(_living_sample())
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(
            inp, out,
            cleaners=[],
            strippers=["living"],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()
        # F1: I1(dead) + I2(living) → removed (any living spouse = GDPR risk)
        assert "@F1@" not in content
        # F2: I2(living) + I3(living) → removed (all living)
        assert "@F2@" not in content
        # F3: I1(dead) + I4(old, presumed dead) → kept (no living spouse)
        assert "@F3@" in content
    finally:
        os.unlink(inp)
        os.unlink(out)
