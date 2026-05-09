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
        ("___",          "NN"),
        ("???",          "NN"),
        ("______",       "NN"),
        ("??????",       "NN"),
        ("   ___   ",    "NN"),
        ("(?)",          "NN"),
        ("(???)",        "NN"),
        ("(????)",       "NN"),
        ("(?????)",      "NN"),
        ("_____--",      "NN"),
        ("___--___",     "NN"),
        ("__ __",        "NN"),
        ("(?) (?)",      "NN"),
        ("[_?_]",        "NN"),
        ("<_>",          "NN"),
        ("-?-",          "NN"),
        ("...",          "NN"),

        # Stub-word placeholders (every case-form, with/without padding)
        ("XY",           "NN"),
        ("xy",           "NN"),
        ("Xy",           "NN"),
        ("XXY",          "NN"),
        ("xxy",          "NN"),
        ("XXX",          "NN"),
        ("xxx",          "NN"),
        ("NN",           "NN"),
        ("nn",           "NN"),
        ("Nn",           "NN"),
        ("_XY_",         "NN"),
        ("__XY__",       "NN"),
        ("___XY___",     "NN"),
        ("  XY  ",       "NN"),
        ("(XY)",         "NN"),
        ("_NN_",         "NN"),
        ("__NN__",       "NN"),
        ("-XXX-",        "NN"),
        ("(XXY)",        "NN"),

        # "UNNAMED" / "Unnamed" / "unnamed" — placeholder
        ("UNNAMED",      "NN"),
        ("Unnamed",      "NN"),
        ("unnamed",      "NN"),
        ("UnNaMeD",      "NN"),
        ("_UNNAMED_",    "NN"),
        ("(Unnamed)",    "NN"),
        ("  unnamed  ",  "NN"),

        # "XX" — placeholder (any case, with optional padding)
        ("XX",           "NN"),
        ("xx",           "NN"),
        ("Xx",           "NN"),
        ("_XX_",         "NN"),
        ("(XX)",         "NN"),
        ("  xx  ",       "NN"),

        # "NEZNAN" / "Neznan" / "neznan" — Slovenian for "unknown"
        ("NEZNAN",       "NN"),
        ("Neznan",       "NN"),
        ("neznan",       "NN"),
        ("NeZnAn",       "NN"),
        ("_Neznan_",     "NN"),
        ("(neznan)",     "NN"),

        # Repeated-letter runs (3+ of the same letter, case-insensitive).
        ("AAA",          "NN"),
        ("aaa",          "NN"),
        ("AaA",          "NN"),
        ("AAAA",         "NN"),
        ("BBBB",         "NN"),
        ("ZZZZZZ",       "NN"),
        ("qqqq",         "NN"),
        ("YyYyY",        "NN"),
        ("_AAA_",        "NN"),
        ("(BBBB)",       "NN"),
        ("  cccc  ",     "NN"),

        # "N.N." family — N + dots/spaces + N (any case) → NN.
        ("N.N.",         "NN"),
        ("N.N",          "NN"),
        ("N N",          "NN"),
        ("N. N.",        "NN"),
        ("N . N",        "NN"),
        ("n.n.",         "NN"),
        ("n n",          "NN"),
        ("Nn",           "NN"),
        ("nN",           "NN"),
        (".N.N.",        "NN"),
        ("_N.N._",       "NN"),

        # Pure dots / spaces only (no letters) — placeholder.
        (". . .",        "NN"),
        ("...",          "NN"),
        (".  .",         "NN"),
        ("   .   ",      "NN"),
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
        ("/___/",        "NN"),
        ("/???/",        "NN"),
        ("/XY/",         "NN"),
        ("/NN/",         "NN"),
        ("/XXX/",        "NN"),
        ("/XXY/",        "NN"),
        ("/_XY_/",       "NN"),
        ("/N.N./",       "NN"),

        # Both given and surname placeholder → collapsed to "NN".
        ("___ /___/",    "NN"),
        ("??? /???/",    "NN"),
        ("_ /_/",        "NN"),
        ("? / ?",        "NN"),     # only one slash → not a slash-pair → whole-value placeholder
        ("XY /NN/",      "NN"),
        ("NN /XXX/",     "NN"),
        ("XXX /XY/",     "NN"),
        ("_XY_ /__NN__/", "NN"),
        ("NN /NN/",      "NN"),
        ("NN //",        "NN"),     # given NN, surname empty — also collapses

        # Given placeholder only — surname is real
        ("___ /Smith/",       "NN /Smith/"),
        ("XY /Smith/",        "NN /Smith/"),
        ("NN /Smith/",        "NN /Smith/"),    # idempotent
        ("__XY__ /Smith/",    "NN /Smith/"),

        # Surname placeholder only — given is real → surname becomes empty //
        # (do not fabricate "NN" when a real given is the only name signal).
        ("Jane /___/",        "Jane //"),
        ("Jane /_?_/",        "Jane //"),
        ("Jane /NN/",         "Jane //"),       # /NN/ → // when given is real
        ("Jane /XXX/",        "Jane //"),
        ("Jane Marie /XY/",   "Jane Marie //"),
        ("Jane /__NN__/",     "Jane //"),

        # "UNNAMED" in slash-format
        ("/UNNAMED/",         "NN"),            # collapsed (no real given)
        ("/Unnamed/",         "NN"),
        ("Jane /UNNAMED/",    "Jane //"),       # real given → empty surname
        ("UNNAMED /Smith/",   "NN /Smith/"),
        ("Unnamed /unnamed/", "NN"),            # both placeholders → collapsed

        # "XX" / "NEZNAN" in slash-format
        ("/XX/",              "NN"),
        ("/Neznan/",          "NN"),
        ("Jane /XX/",         "Jane //"),
        ("Jane /Neznan/",     "Jane //"),
        ("XX /Smith/",        "NN /Smith/"),
        ("Neznan /Smith/",    "NN /Smith/"),
        ("XX /Neznan/",       "NN"),
        ("Neznan /XX/",       "NN"),

        # All-slashes / whitespace — no signal at all → NN
        ("//",                "NN"),
        ("/  /",              "NN"),
        ("  //  ",            "NN"),

        # Repeated-letter runs in slash-format
        ("/AAAA/",            "NN"),
        ("/qqqq/",            "NN"),
        ("Jane /AAAA/",       "Jane //"),
        ("AAAA /Smith/",      "NN /Smith/"),
        ("AAAA /BBBB/",       "NN"),
        ("ZZZ /YYY/",         "NN"),
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
        "/Novak/",
        "J. /Doe/",
        "",                     # already empty — stays empty
        # Real names that *contain* a stub substring but are not pure stubs.
        "XYla",
        "XYZ",
        "/Xyrus/",
        "Anna",
        "XYz /smith/",
        # Real given + surname — no change.
        "___ Jane /Smith/",     # given has letters mixed with placeholder, NOT placeholder-only
        "Jože - Pepi",
        "Jože - Pepi /Surname/",
        # Real names that *contain* "Unnamed" as a substring but are not
        # pure stubs.
        "Unnameds",
        "Unnamedfoo",
        "/Unnameds/",
        # 2-char same-letter strings stay (Roman numeral suffixes etc.).
        "II",
        "VV",
        "MM",
        # Real names with repeated letters but mixed with other characters.
        "Aaron",
        "Lee",
        "Anna",
        "/Aaron/",
        "Aaron /Smith/",
        # 2-char Roman-numeral suffix as part of a full name stays.
        "John /Smith/ II",
        # Note: 3+ same-letter sequences are aggressive — e.g. "III" suffix
        # would be mistaken for a placeholder. The user explicitly requested
        # this rule, so that trade-off is accepted.
        # Empty surname is preserved when there is a real given:
        # "Jane //" means "surname intentionally absent / unrecorded",
        # which is different from a placeholder marker like "___" or "XY".
        "Jane //",
        "John //",
        "Jane Marie //",
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
        ("John  /Smith/  ",     "John /Smith/"),
        ("  John /Smith/",      "John /Smith/"),
        ("John  /Smith/",       "John /Smith/"),
        # Inner whitespace inside slashes
        ("John / Smith /",      "John /Smith/"),
        ("John /Mary  Ann/",    "John /Mary Ann/"),
        ("John /  Smith  /",    "John /Smith/"),
        # Whitespace-only surname collapses to empty.
        ("Jane / /",            "Jane //"),
        ("Jane /   /",          "Jane //"),
        # Plain-value tags (no slashes)
        ("  Ana  Marija  ",     "Ana Marija"),
        ("Ana   Marija",        "Ana Marija"),
    ],
)
def test_whitespace_normalized(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# "Real given + placeholder surname" rule: surname becomes empty //, not /NN/.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Various placeholder surname forms with a real given → empty surname.
        ("John /???/",          "John //"),
        ("John /(?)/",          "John //"),
        ("Luka /XY/",           "Luka //"),
        ("Luka /XXY/",          "Luka //"),
        ("Luka Anton /...___.../", "Luka Anton //"),
        ("Jože /__/",           "Jože //"),
        ("Špela /Unnamed/",     "Špela //"),

        # Already-NN surname with real given is also normalized to empty //.
        ("John /NN/",           "John //"),
        ("John /N.N./",         "John //"),
        ("John /. . ./",        "John //"),

        # No real given AND no real surname → collapsed to "NN".
        ("/???/",               "NN"),
        ("___ /???/",           "NN"),
        ("/Unnamed/",           "NN"),
        ("/N.N./",              "NN"),
        ("N.N. /N N/",          "NN"),
    ],
)
def test_placeholder_surname_with_real_given(raw, expected):
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
        ("NN /NN/",            "NN"),
        ("/NN/",               "NN"),
        ("NN //",              "NN"),
        ("nn /nn/",            "NN"),
        ("N.N. /NN/",          "NN"),
        ("NN /N. N./",         "NN"),
        ("UNNAMED /UNNAMED/",  "NN"),
        ("XY /XXX/",           "NN"),

        # Real content blocks the collapse.
        ("NN /Smith/",         "NN /Smith/"),
        ("Jane /NN/",          "Jane //"),
        ("Jane //",            "Jane //"),
        # Empty/all-slashes — no name signal at all → "NN".
        ("//",                 "NN"),
        ("/ /",                "NN"),
        ("  //  ",             "NN"),
        ("/  /",               "NN"),
    ],
)
def test_all_nn_collapse(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Integration: SURN child must reflect the parent NAME's slash content.
# When NAME's surname collapses to empty ("Jane /NN/" -> "Jane //"), the
# SURN child should not retain "NN" either.
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
            inp, out,
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
            inp, out,
            cleaners=["name_placeholder"],
            strippers=[],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()

        # I1: NAME's surname collapsed to empty → SURN must be empty.
        assert "1 NAME Jane //" in content
        assert "2 SURN NN" not in content      # the orphaned "NN" is gone
        assert "1 NAME Jane //\n2 GIVN Jane\n2 SURN" in content

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
