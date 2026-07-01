import os
import tempfile
import textwrap

import pytest
from tools.gedcom_cleaner import clean_name_placeholder, process_file

# ---------------------------------------------------------------------------
# Whole-value placeholders → "NN"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Pure-punctuation placeholders
        ("___", "NN"),
        ("???", "NN"),
        ("______", "NN"),
        ("??????", "NN"),
        ("   ___   ", "NN"),
        ("(?)", "NN"),
        ("(???)", "NN"),
        ("(????)", "NN"),
        ("(?????)", "NN"),
        ("_____--", "NN"),
        ("___--___", "NN"),
        ("__ __", "NN"),
        ("(?) (?)", "NN"),
        ("[_?_]", "NN"),
        ("<_>", "NN"),
        ("-?-", "NN"),
        ("...", "NN"),
        # Stub-word placeholders (every case-form, with/without padding)
        ("XY", "NN"),
        ("xy", "NN"),
        ("Xy", "NN"),
        ("XXY", "NN"),
        ("xxy", "NN"),
        ("XXX", "NN"),
        ("xxx", "NN"),
        ("NN", "NN"),
        ("nn", "NN"),
        ("Nn", "NN"),
        ("_XY_", "NN"),
        ("__XY__", "NN"),
        ("___XY___", "NN"),
        ("  XY  ", "NN"),
        ("(XY)", "NN"),
        ("_NN_", "NN"),
        ("__NN__", "NN"),
        ("-XXX-", "NN"),
        ("(XXY)", "NN"),
        # "UNNAMED" / "Unnamed" / "unnamed" — placeholder
        ("UNNAMED", "NN"),
        ("Unnamed", "NN"),
        ("unnamed", "NN"),
        ("UnNaMeD", "NN"),
        ("_UNNAMED_", "NN"),
        ("(Unnamed)", "NN"),
        ("  unnamed  ", "NN"),
        # "UNKNOWN" / "Unknown" / "unknown" — placeholder
        ("UNKNOWN", "NN"),
        ("Unknown", "NN"),
        ("unknown", "NN"),
        ("UnKnOwN", "NN"),
        ("_UNKNOWN_", "NN"),
        ("(Unknown)", "NN"),
        ("  unknown  ", "NN"),
        # "XX" — placeholder (any case, with optional padding)
        ("XX", "NN"),
        ("xx", "NN"),
        ("Xx", "NN"),
        ("_XX_", "NN"),
        ("(XX)", "NN"),
        ("  xx  ", "NN"),
        # "NEZNAN" / "Neznan" / "neznan" — Slovenian for "unknown"
        ("NEZNAN", "NN"),
        ("Neznan", "NN"),
        ("neznan", "NN"),
        ("NeZnAn", "NN"),
        ("_Neznan_", "NN"),
        ("(neznan)", "NN"),
        # Repeated-letter runs (3+ of the same letter, case-insensitive).
        ("AAA", "NN"),
        ("aaa", "NN"),
        ("AaA", "NN"),
        ("AAAA", "NN"),
        ("BBBB", "NN"),
        ("ZZZZZZ", "NN"),
        ("qqqq", "NN"),
        ("YyYyY", "NN"),
        ("_AAA_", "NN"),
        ("(BBBB)", "NN"),
        ("  cccc  ", "NN"),
        # "N.N." family — N + dots/spaces + N (any case) → NN.
        ("N.N.", "NN"),
        ("N.N", "NN"),
        ("N N", "NN"),
        ("N. N.", "NN"),
        ("N . N", "NN"),
        ("n.n.", "NN"),
        ("n n", "NN"),
        ("Nn", "NN"),
        ("nN", "NN"),
        (".N.N.", "NN"),
        ("_N.N._", "NN"),
        # Pure dots / spaces only (no letters) — placeholder.
        (". . .", "NN"),
        ("...", "NN"),
        (".  .", "NN"),
        ("   .   ", "NN"),
    ],
)
def test_whole_value_placeholder_to_nn(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Slash-format placeholders → /NN/, "NN /Surname/", "Given /NN/", "NN /NN/"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Surname-only forms (empty given). After the all-NN simplification,
        # a value with no real content collapses to plain "NN".
        ("/___/", "NN"),
        ("/???/", "NN"),
        ("/XY/", "NN"),
        ("/NN/", "NN"),
        ("/XXX/", "NN"),
        ("/XXY/", "NN"),
        ("/_XY_/", "NN"),
        ("/N.N./", "NN"),
        # Both given and surname placeholder → collapsed to "NN".
        ("___ /___/", "NN"),
        ("??? /???/", "NN"),
        ("_ /_/", "NN"),
        ("? / ?", "NN"),  # only one slash → not a slash-pair → whole-value placeholder
        ("XY /NN/", "NN"),
        ("NN /XXX/", "NN"),
        ("XXX /XY/", "NN"),
        ("_XY_ /__NN__/", "NN"),
        ("NN /NN/", "NN"),
        ("NN //", "NN"),  # given NN, surname empty — also collapses
        # Given placeholder only — surname is real
        ("___ /Smith/", "NN /Smith/"),
        ("XY /Smith/", "NN /Smith/"),
        ("NN /Smith/", "NN /Smith/"),  # idempotent
        ("__XY__ /Smith/", "NN /Smith/"),
        # Surname placeholder only — given is real → explicit "NN" surname
        # ("Given /NN/"): an unknown surname is recorded as the "NN" stub.
        ("Jane /___/", "Jane /NN/"),
        ("Jane /_?_/", "Jane /NN/"),
        ("Jane /NN/", "Jane /NN/"),  # idempotent
        ("Jane /XXX/", "Jane /NN/"),
        ("Jane Marie /XY/", "Jane Marie /NN/"),
        ("Jane /__NN__/", "Jane /NN/"),
        # "UNNAMED" in slash-format
        ("/UNNAMED/", "NN"),  # collapsed (no real given)
        ("/Unnamed/", "NN"),
        ("Jane /UNNAMED/", "Jane /NN/"),  # real given → explicit NN surname
        ("UNNAMED /Smith/", "NN /Smith/"),
        ("Unnamed /unnamed/", "NN"),  # both placeholders → collapsed
        # "UNKNOWN" in slash-format
        ("/UNKNOWN/", "NN"),  # collapsed (no real given)
        ("/Unknown/", "NN"),
        ("Jane /UNKNOWN/", "Jane /NN/"),  # real given → explicit NN surname
        ("UNKNOWN /Smith/", "NN /Smith/"),
        # "XX" / "NEZNAN" in slash-format
        ("/XX/", "NN"),
        ("/Neznan/", "NN"),
        ("Jane /XX/", "Jane /NN/"),
        ("Jane /Neznan/", "Jane /NN/"),
        ("XX /Smith/", "NN /Smith/"),
        ("Neznan /Smith/", "NN /Smith/"),
        ("XX /Neznan/", "NN"),
        ("Neznan /XX/", "NN"),
        # All-slashes / whitespace — no signal at all → NN
        ("//", "NN"),
        ("/  /", "NN"),
        ("  //  ", "NN"),
        # Repeated-letter runs in slash-format
        ("/AAAA/", "NN"),
        ("/qqqq/", "NN"),
        ("Jane /AAAA/", "Jane /NN/"),
        ("AAAA /Smith/", "NN /Smith/"),
        ("AAAA /BBBB/", "NN"),
        ("ZZZ /YYY/", "NN"),
    ],
)
def test_slash_placeholder_to_nn(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Real names that contain stub substrings or look superficially placeholder
# but are NOT placeholders — must be left unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        # Real names — no change.
        "John /Smith/",
        "Ana",
        "J. /Doe/",
        "",  # already empty — stays empty
        # Real names that *contain* a stub substring but are not pure stubs.
        "XYla",
        "XYZ",
        "Anna",
        "XYz /smith/",
        # Real given + surname — no change.
        "___ Jane /Smith/",  # given has letters mixed with placeholder, NOT placeholder-only
        "Jože - Pepi",
        "Jože - Pepi /Surname/",
        # Real names that *contain* "Unnamed" as a substring but are not
        # pure stubs.
        "Unnameds",
        "Unnamedfoo",
        # 2-char same-letter strings stay (Roman numeral suffixes etc.).
        "II",
        "VV",
        "MM",
        # Real names with repeated letters but mixed with other characters.
        "Aaron",
        "Lee",
        "Anna",
        "Aaron /Smith/",
        # 2-char Roman-numeral suffix as part of a full name stays.
        "John /Smith/ II",
        # Note: 3+ same-letter sequences are aggressive — e.g. "III" suffix
        # would be mistaken for a placeholder. The user explicitly requested
        # this rule, so that trade-off is accepted.
    ],
)
def test_real_names_unchanged(raw):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == raw


# ---------------------------------------------------------------------------
# Whitespace normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Outer whitespace
        ("John  /Smith/  ", "John /Smith/"),
        ("  John /Smith/", "John /Smith/"),
        ("John  /Smith/", "John /Smith/"),
        # Inner whitespace inside slashes
        ("John / Smith /", "John /Smith/"),
        ("John /Mary  Ann/", "John /Mary Ann/"),
        ("John /  Smith  /", "John /Smith/"),
        # Whitespace-only surname with a real given → explicit "/NN/".
        ("Jane / /", "Jane /NN/"),
        ("Jane /   /", "Jane /NN/"),
        # Plain-value tags (no slashes)
        ("  Ana  Marija  ", "Ana Marija"),
        ("Ana   Marija", "Ana Marija"),
    ],
)
def test_whitespace_normalized(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# "Real given + placeholder/empty surname" rule: surname becomes "/NN/".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Various placeholder surname forms with a real given → explicit NN.
        ("John /???/", "John /NN/"),
        ("John /(?)/", "John /NN/"),
        ("Luka /XY/", "Luka /NN/"),
        ("Luka /XXY/", "Luka /NN/"),
        ("Luka Anton /...___.../", "Luka Anton /NN/"),
        ("Jože /__/", "Jože /NN/"),
        ("Špela /Unnamed/", "Špela /NN/"),
        # Already-NN surname with real given stays "/NN/" (idempotent).
        ("John /NN/", "John /NN/"),
        ("John /N.N./", "John /NN/"),
        ("John /. . ./", "John /NN/"),
        # No real given AND no real surname → collapsed to "NN".
        ("/???/", "NN"),
        ("___ /???/", "NN"),
        ("/Unnamed/", "NN"),
        ("/N.N./", "NN"),
        ("N.N. /N N/", "NN"),
    ],
)
def test_placeholder_surname_with_real_given(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# "Real surname + empty/placeholder given" rule: given becomes "NN"
# ("/Smith/" -> "NN /Smith/"). An unknown given name is recorded explicitly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Surname-only (empty given) → explicit "NN" given.
        ("/Smith/", "NN /Smith/"),
        ("/Novak/", "NN /Novak/"),
        ("/Xyrus/", "NN /Xyrus/"),
        ("/Unnameds/", "NN /Unnameds/"),
        ("/Aaron/", "NN /Aaron/"),
        (" /Smith/ ", "NN /Smith/"),
        ("/ Smith /", "NN /Smith/"),
        # Multi-variant surname list with no given is left as-is: the text
        # between the inner slashes parses as a "given", so no "NN" is added.
        ("/Wershnig/Verschnig/Berschnik/", "/Wershnig/Verschnig/Berschnik/"),
        # A real given already present is NOT touched by this rule.
        ("J. /Doe/", "J. /Doe/"),
        ("NN /Smith/", "NN /Smith/"),  # idempotent
    ],
)
def test_empty_given_with_real_surname(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# All-NN collapse rule: when the result has no real-name content, it collapses
# to a single "NN" (NN /NN/ etc. should never appear in the output).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("NN /NN/", "NN"),
        ("/NN/", "NN"),
        ("NN //", "NN"),
        ("nn /nn/", "NN"),
        ("N.N. /NN/", "NN"),
        ("NN /N. N./", "NN"),
        ("UNNAMED /UNNAMED/", "NN"),
        ("XY /XXX/", "NN"),
        # Real content blocks the collapse.
        ("NN /Smith/", "NN /Smith/"),
        ("Jane /NN/", "Jane /NN/"),
        ("Jane //", "Jane /NN/"),
        # Empty/all-slashes — no name signal at all → "NN".
        ("//", "NN"),
        ("/ /", "NN"),
        ("  //  ", "NN"),
        ("/  /", "NN"),
    ],
)
def test_all_nn_collapse(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Integration: SURN child must reflect the parent NAME's slash content.
# When NAME's surname becomes "NN" ("Jane //" -> "Jane /NN/") the SURN child
# is synced to "NN"; when NAME collapses to a bare "NN" the SURN is cleared.
# ---------------------------------------------------------------------------


def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_surn_sync_preserves_multi_variant_surname():
    """Multi-slash NAME values like Helena /Wershnig/Verschnig/Berschnik/ are
    surname-variant lists; the SURN child must keep the full inner content
    (the slash-greedy match), not just the first variant."""
    sample = textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Helena /Wershnig/Verschnig/Berschnik/
        2 GIVN Helena
        2 SURN Wershnig/Verschnig/Berschnik
        0 TRLR
    """)
    inp = _write_tmp(sample)
    out = _write_tmp("")
    try:
        process_file(
            inp,
            out,
            cleaners=["name_placeholder"],
            strippers=[],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()
        # NAME left alone (no placeholder content).
        assert "1 NAME Helena /Wershnig/Verschnig/Berschnik/" in content
        # SURN preserves the full surname-variant string.
        assert "2 SURN Wershnig/Verschnig/Berschnik" in content
        # Make sure we didn't truncate to just the first variant.
        assert "2 SURN Wershnig\n" not in content
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_surn_synced_to_empty_when_name_surname_empty():
    sample = textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME Jane /NN/
        2 GIVN Jane
        2 SURN NN
        0 @I2@ INDI
        1 NAME ___ /Smith/
        2 GIVN ___
        2 SURN Smith
        0 @I3@ INDI
        1 NAME NN /NN/
        2 GIVN NN
        2 SURN NN
        0 @I4@ INDI
        1 NAME John /Smith/
        2 GIVN John
        2 SURN Smith
        0 TRLR
    """)
    inp = _write_tmp(sample)
    out = _write_tmp("")
    try:
        process_file(
            inp,
            out,
            cleaners=["name_placeholder"],
            strippers=[],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()

        # I1: unknown surname recorded as "NN" → SURN synced to "NN".
        assert "1 NAME Jane /NN/" in content
        assert "1 NAME Jane /NN/\n2 GIVN Jane\n2 SURN NN" in content

        # I2: real surname kept; SURN unchanged.
        assert "1 NAME NN /Smith/" in content
        assert "2 SURN Smith" in content

        # I3: NAME collapsed to single "NN"; no slashes, surname is empty,
        # so SURN must be cleared too.
        assert "1 NAME NN\n2 GIVN NN\n2 SURN" in content

        # I4: real names — entirely untouched.
        assert "1 NAME John /Smith/" in content
        # Should still have exactly one "2 SURN Smith" for I2 and one for I4.
        assert content.count("2 SURN Smith") == 2
    finally:
        os.unlink(inp)
        os.unlink(out)
