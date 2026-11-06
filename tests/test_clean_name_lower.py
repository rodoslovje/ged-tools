import os
import tempfile
import textwrap

import pytest
from tools.gedcom_cleaner import clean_name_lower, process_file


# ---------------------------------------------------------------------------
# ALL-CAPS values: the cleaner kicks in and re-cases via the
# name_capitalization rules (particles, prefixes, parens).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Plain ASCII / diacritics
        ("JOHN /SMITH/",            "John /Smith/"),
        ("JOŽE /KOVAČ/",            "Jože /Kovač/"),
        ("ŠPELA /ČRNIČ/",           "Špela /Črnič/"),
        ("ANA MARIJA /NOVAK/",      "Ana Marija /Novak/"),

        # Particle in the middle stays lowercase, segment-start gets capitalized.
        ("FRANC DE PAULA /PIŽMOHT/",        "Franc de Paula /Pižmoht/"),
        ("MARIA VON DER LEYEN",             "Maria von der Leyen"),
        ("MARIA /VON DER LEYEN/",           "Maria /Von der Leyen/"),

        # Particle as the whole surname or starting given names.
        ("MAAR LOUISE WOPKELINE BEATRICE/DE/",
                                    "Maar Louise Wopkeline Beatrice/De/"),
        ("DE COLL URŠULA/RANGA/",   "De Coll Uršula/Ranga/"),
        ("/DE/",                    "/De/"),
        ("DE",                      "De"),

        # Plain-value tags (GIVN, SURN, NICK, MARNM).
        ("KOVAČ",       "Kovač"),
        ("ANA MARIJA",  "Ana Marija"),
        ("ČRNIČ",       "Črnič"),

        # Hyphenated all-caps.
        ("MARY /SMITH-JONES/",      "Mary /Smith-Jones/"),

        # "NN" (Nomen Nescio — name_placeholder writes this) must stay uppercase.
        ("NN /SMITH/",              "NN /Smith/"),
        ("JOHN /NN/",               "John /NN/"),
        ("NN NN /NN/",              "NN NN /NN/"),
        ("JOHN NN /SMITH/",         "John NN /Smith/"),
        ("NN",                      "NN"),
        ("/NN/",                    "/NN/"),
    ],
)
def test_all_caps_is_recased(raw, expected):
    result, warning = clean_name_lower(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Non-ALL-CAPS values are left UNCHANGED (this is what distinguishes
# name_lower from name_capitalization).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        # Already correctly title-cased — no change.
        "John /Smith/",
        "Jože /Kovač/",
        "Franc de Paula /Pižmoht/",
        "De Coll Uršula/Ranga/",

        # No ALL-CAPS word — left alone even if "wrong" (name_lower is
        # conservative; only ALL-CAPS words trigger it).
        "JoHn /sMiTh/",
        "jOžE /kOvAč/",
        "MacDonald",
        "DePaula",

        # All-lowercase — left alone.
        "john /smith/",
        "jože /kovač/",
        "franc de paula /pižmoht/",

        # Empty / whitespace only.
        "",
        "   ",
    ],
)
def test_non_all_caps_unchanged(raw):
    result, warning = clean_name_lower(raw)
    assert warning is None
    assert result == raw


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 'Given /SURNAME/' export style — the ALL-CAPS surname is re-cased.
        ("John /SMITH/",                        "John /Smith/"),
        ("Terezija /KRAMBERGER/",               "Terezija /Kramberger/"),
        ("Franciscus (Franc) /KRAMBERGER/",     "Franciscus (Franc) /Kramberger/"),
        ("JANEZ /Novak/",                       "Janez /Novak/"),
        ("Maria /VON DER LEYEN/",               "Maria /Von der Leyen/"),
        ("Mary /SMITH-JONES/",                  "Mary /Smith-Jones/"),
        ("John NN /SMITH/",                     "John NN /Smith/"),
        # Plain-value tags with mixed words.
        ("Ana MARIJA",                          "Ana Marija"),
        # Mixed-case words in the same value are kept verbatim.
        ("Ana /MacDONALD KOVAČ/",               "Ana /MacDONALD Kovač/"),
        # Parenthesized text is kept verbatim.
        ("Marija /NOVAK/ (rojena KOVAČ)",       "Marija /Novak/ (rojena KOVAČ)"),
        # Single letters and Roman-numeral suffixes are kept.
        ("John F /KENNEDY/",                    "John F /Kennedy/"),
        ("Janez /NOVAK/ III",                   "Janez /Novak/ III"),
        ("Janez /NOVAK/ IV.",                   "Janez /Novak/ IV."),
        ("Pharnabazes II., K. /IBERIJSKI/",     "Pharnabazes II., K. /Iberijski/"),
        # …but a Roman-numeral-looking surname is still re-cased.
        ("Ana /MIX/",                           "Ana /Mix/"),
    ],
)
def test_mixed_case_caps_words_recased(raw, expected):
    result, warning = clean_name_lower(raw)
    assert warning is None
    assert result == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        # ALL CAPS — recased AND whitespace-normalized.
        ("JOHN  /SMITH/  ",         "John /Smith/"),
        ("JOHN /  SMITH  /",        "John /Smith/"),
        ("JOHN   /SMITH/",          "John /Smith/"),
        # Mixed / lower input — NOT recased, but whitespace still tidied.
        ("John  /Smith/  ",         "John /Smith/"),
        ("john / smith /",          "john /smith/"),
        ("Ana   Marija  /Novak/",   "Ana Marija /Novak/"),
    ],
)
def test_whitespace_normalized(raw, expected):
    result, warning = clean_name_lower(raw)
    assert warning is None
    assert result == expected


def test_idempotent():
    """Running twice must equal running once on every sample."""
    samples = [
        "JOŽE /KOVAČ/",
        "FRANC DE PAULA /PIŽMOHT/",
        "MAAR LOUISE WOPKELINE BEATRICE/DE/",
        "DE COLL URŠULA/RANGA/",
        "John /Smith/",
        "John /SMITH/",
        "Janez /NOVAK/ III",
        "jože /kovač/",
    ]
    for raw in samples:
        once, _ = clean_name_lower(raw)
        twice, _ = clean_name_lower(once)
        assert once == twice, f"not idempotent for {raw!r}: {once!r} -> {twice!r}"


# ---------------------------------------------------------------------------
# Integration: only INDI names are touched; SUBM / SOUR / FAM untouched
# ---------------------------------------------------------------------------

def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_only_indi_names_touched():
    sample = textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME JOŽE /KOVAČ/
        0 @I2@ INDI
        1 NAME John /Smith/
        0 @I3@ INDI
        1 NAME jože /kovač/
        0 @I4@ INDI
        1 NAME Terezija /KRAMBERGER/
        2 SURN KRAMBERGER
        0 @SUBM1@ SUBM
        1 NAME SOME SUBMITTER NAME
        0 @S1@ SOUR
        1 AUTH SOME AUTHOR
        0 TRLR
    """)
    inp = _write_tmp(sample)
    out = _write_tmp("")
    try:
        cleaner_stats, _, _ = process_file(
            inp, out,
            cleaners=["name_lower"],
            strippers=[],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()
        # ALL CAPS INDI name: re-cased.
        assert "1 NAME Jože /Kovač/" in content
        # Mixed-case INDI name: untouched.
        assert "1 NAME John /Smith/" in content
        # All-lowercase INDI name: untouched.
        assert "1 NAME jože /kovač/" in content
        # Mixed-case INDI name with ALL-CAPS surname: surname re-cased.
        assert "1 NAME Terezija /Kramberger/" in content
        assert "2 SURN Kramberger" in content
        # SUBM NAME: untouched even though it's all caps.
        assert "1 NAME SOME SUBMITTER NAME" in content
        # SOUR AUTH (not a NAME tag anyway): untouched.
        assert "1 AUTH SOME AUTHOR" in content

        # JOŽE /KOVAČ/, Terezija /KRAMBERGER/ and its SURN were changed.
        assert cleaner_stats["name_lower"].fixed == 3
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_preset_index_cleanup_sgi_uses_name_lower():
    """The sgi preset should pull in name_lower (and not name_capitalization)."""
    from tools.gedcom_cleaner import PRESETS
    assert "name_lower" in PRESETS["index_cleanup_sgi"]["clean"]
    assert "name_capitalization" not in PRESETS["index_cleanup_sgi"]["clean"]


def test_preset_index_cleanup_cgi_uses_name_lower():
    from tools.gedcom_cleaner import PRESETS
    assert "name_lower" in PRESETS["index_cleanup_cgi"]["clean"]
    assert "name_capitalization" not in PRESETS["index_cleanup_cgi"]["clean"]
