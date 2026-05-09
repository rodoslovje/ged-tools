import os
import tempfile
import textwrap

import pytest
from tools.gedcom_cleaner import clean_name_capitalization, process_file


# ---------------------------------------------------------------------------
# Cases that should change
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Plain ASCII — lowercase / uppercase / mixed-noise
        ("john /smith/",        "John /Smith/"),
        ("JOHN /SMITH/",        "John /Smith/"),
        ("JoHn /sMiTh/",        "John /Smith/"),
        ("ana marija /novak/",  "Ana Marija /Novak/"),

        # Non-ASCII — Slovenian / Croatian diacritics, lowercase → titled
        ("jože /kovač/",        "Jože /Kovač/"),
        ("špela /črnič/",       "Špela /Črnič/"),
        ("žarko /šuštar/",      "Žarko /Šuštar/"),

        # Non-ASCII — uppercase → titled
        ("JOŽE /KOVAČ/",        "Jože /Kovač/"),
        ("ŠPELA /ČRNIČ/",       "Špela /Črnič/"),
        ("ŽARKO /ŠUŠTAR/",      "Žarko /Šuštar/"),

        # Non-ASCII — random-noise mixed case (NOT a known prefix pattern) is
        # normalized.
        ("jOžE /kOvAč/",        "Jože /Kovač/"),

        # Hyphenated and apostrophe names
        ("jean-pierre /dupont/", "Jean-Pierre /Dupont/"),
        ("MARY /SMITH-JONES/",   "Mary /Smith-Jones/"),
        ("o'brien",              "O'Brien"),

        # Plain-value tags (no slashes — GIVN, SURN, NICK, MARNM)
        ("KOVAČ",       "Kovač"),
        ("kovač",       "Kovač"),
        ("ana marija",  "Ana Marija"),
        ("ČRNIČ",       "Črnič"),

        # Surname-only / given-only slash forms
        ("/novak/",     "/Novak/"),
        ("/NOVAK/",     "/Novak/"),
        ("LUKA //",     "Luka //"),
        ("luka //",     "Luka //"),
    ],
)
def test_basic_capitalization(raw, expected):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Particles ("de", "von", "der", "des", "v.", …) must stay lowercase
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # User's headline example — already correct, must not be broken.
        ("Franc de Paula /Pižmoht/",        "Franc de Paula /Pižmoht/"),
        ("Franc de Paula/Pižmoht/",         "Franc de Paula/Pižmoht/"),

        # All-uppercase input → particles still go lowercase
        ("FRANC DE PAULA /PIŽMOHT/",        "Franc de Paula /Pižmoht/"),

        # All-lowercase input → particles stay lowercase, the rest title-cased
        ("franc de paula /pižmoht/",        "Franc de Paula /Pižmoht/"),

        # Multiple particles in a row
        ("MARIA VON DER LEYEN",             "Maria von der Leyen"),
        ("maria van den berg",              "Maria van den Berg"),
        ("LUDWIG VAN BEETHOVEN",            "Ludwig van Beethoven"),

        # Slovenian/Slavic abbreviation "v."
        ("MARIJA V. KOVAČ",                 "Marija v. Kovač"),
        ("Marija v. Kovač",                 "Marija v. Kovač"),

        # Particle inside a slash-delimited surname — the leading "von" of the
        # surname segment is capitalized; "der" in the middle stays lowercase.
        ("Maria /von der Leyen/",           "Maria /Von der Leyen/"),
        ("MARIA /VON DER LEYEN/",           "Maria /Von der Leyen/"),
    ],
)
def test_particles_stay_lowercase(raw, expected):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Particles at the START of a segment (or as the only word) get capitalized
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # User examples — particle as the WHOLE surname.
        ("Maar Louise Wopkeline Beatrice/De/",  "Maar Louise Wopkeline Beatrice/De/"),
        ("Maar Louise Wopkeline Beatrice/de/",  "Maar Louise Wopkeline Beatrice/De/"),
        ("Maar Louise Wopkeline Beatrice/DE/",  "Maar Louise Wopkeline Beatrice/De/"),

        # User example — particle at the start of the given names.
        ("De Coll Uršula/Ranga/",   "De Coll Uršula/Ranga/"),
        ("de Coll Uršula/Ranga/",   "De Coll Uršula/Ranga/"),
        ("DE COLL URŠULA/RANGA/",   "De Coll Uršula/Ranga/"),

        # Particle as initial word of given names — capitalized.
        ("de Vries /Smith/",        "De Vries /Smith/"),
        ("DE VRIES /SMITH/",        "De Vries /Smith/"),

        # Particle alone in plain-value tags (e.g. SURN value "De").
        ("De",                      "De"),
        ("de",                      "De"),
        ("DE",                      "De"),
        ("von",                     "Von"),
        ("V.",                      "V."),

        # Particle alone inside slashes.
        ("/De/",                    "/De/"),
        ("/de/",                    "/De/"),
        ("/DE/",                    "/De/"),
        ("/Von/",                   "/Von/"),

        # Particle starting the surname segment with another word after.
        ("/von Trapp/",             "/Von Trapp/"),
        ("/VON TRAPP/",             "/Von Trapp/"),

        # Particle starting the given names with a non-particle middle word.
        ("Von Trapp /Smith/",       "Von Trapp /Smith/"),
    ],
)
def test_particle_at_segment_start_capitalized(raw, expected):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Compound-name prefixes: MacDonald / DePaula / VanDijk … must be preserved
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # User's headline examples — already correct, must NOT be flattened.
        ("Franc DePaula /Tonin/",   "Franc DePaula /Tonin/"),
        ("Franc /MacDonald/",       "Franc /MacDonald/"),

        # Other prefix patterns
        ("/McDonald/",              "/McDonald/"),
        ("/DiCaprio/",              "/DiCaprio/"),
        ("/VanDijk/",               "/VanDijk/"),
        ("/DuPont/",                "/DuPont/"),
        ("/LaFleur/",               "/LaFleur/"),
        ("/O'Brien/",               "/O'Brien/"),
        ("/D'Angelo/",              "/D'Angelo/"),

        # Compound prefix with a particle elsewhere in the value
        ("Franc de Paula /MacDonald/",  "Franc de Paula /MacDonald/"),
    ],
)
def test_compound_prefixes_preserved(raw, expected):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Parenthesized text must be left verbatim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Parens stay untouched even when surrounding text is normalized.
        ("FRANC (Pepi) /KOVAČ/",     "Franc (Pepi) /Kovač/"),
        ("franc (pepi) /kovač/",     "Franc (pepi) /Kovač/"),
        ("JOHN (Big J) SMITH",       "John (Big J) Smith"),
        ("john (BIG j) smith",       "John (BIG j) Smith"),

        # Multiple parenthesized groups
        ("(a) FRANC (b)",            "(a) Franc (b)"),

        # Parens inside the slash-surname
        ("/SMITH (married)/",        "/Smith (married)/"),

        # Parenthesized particle should also stay verbatim (the rule wins).
        ("FRANC (de) PAULA",         "Franc (de) Paula"),
    ],
)
def test_parentheses_preserved(raw, expected):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# "NN" (Nomen Nescio — name unknown) must NOT be converted to "Nn".
# It is the conventional stub written by name_placeholder.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("NN /SMITH/",          "NN /Smith/"),
        ("nn /smith/",          "NN /Smith/"),
        ("Nn /Smith/",          "NN /Smith/"),
        ("JOHN /NN/",           "John /NN/"),
        ("john /nn/",           "John /NN/"),
        ("john /Nn/",           "John /NN/"),
        ("NN /NN/",             "NN /NN/"),
        ("NN NN /NN/",          "NN NN /NN/"),
        ("nn nn /nn/",          "NN NN /NN/"),
        ("JOHN NN SMITH",       "John NN Smith"),
        # Plain-value tag with NN.
        ("NN",                  "NN"),
        ("nn",                  "NN"),
        ("Nn",                  "NN"),
        ("/NN/",                "/NN/"),
        ("/nn/",                "/NN/"),
    ],
)
def test_nn_preserved(raw, expected):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# Idempotency: already-canonical names pass through unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "John /Smith/",
        "Jože /Kovač/",
        "Špela /Črnič/",
        "Ana Marija /Novak/",
        "Jean-Pierre /Dupont/",
        "Kovač",
        "/Novak/",
        # Particle / prefix combinations that are already correct
        "Franc de Paula /Pižmoht/",
        "Franc DePaula /Tonin/",
        "Franc /MacDonald/",
        "Maria von der Leyen",
        # Particle at segment start, already capitalized
        "Maar Louise Wopkeline Beatrice/De/",
        "De Coll Uršula/Ranga/",
        "Maria /Von der Leyen/",
        # Parenthesized
        "Franc (Pepi) /Kovač/",
        # Empty / whitespace pass through unchanged
        "",
        "   ",
    ],
)
def test_unchanged_when_already_canonical(raw):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == raw


def test_idempotent_on_arbitrary_inputs():
    """Running the cleaner twice must produce the same output as running it once."""
    samples = [
        "JOŽE /KOVAČ/",
        "špela /črnič/",
        "ŽARKO /ŠUŠTAR/",
        "ana /novak/",
        "FRANC DE PAULA /PIŽMOHT/",
        "MARIA VON DER LEYEN",
        "Franc DePaula /Tonin/",
        "Franc /MacDonald/",
        "JOHN (Pepi) SMITH",
        "Maar Louise Wopkeline Beatrice/De/",
        "De Coll Uršula/Ranga/",
        "DE COLL URŠULA/RANGA/",
        "Maria /VON der Leyen/",
        "/De/",
        "DE",
    ]
    for raw in samples:
        once, _ = clean_name_capitalization(raw)
        twice, _ = clean_name_capitalization(once)
        assert once == twice, f"not idempotent for {raw!r}: {once!r} -> {twice!r}"


# ---------------------------------------------------------------------------
# Diacritic codepoints survive verbatim (no NFC/NFD normalization or stripping)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Outer whitespace
        ("John  /Smith/  ",     "John /Smith/"),
        ("  john /smith/",      "John /Smith/"),
        ("JOHN   /SMITH/",      "John /Smith/"),
        # Inner whitespace inside slashes
        ("john / smith /",      "John /Smith/"),
        ("john /mary  ann/",    "John /Mary Ann/"),
        # Plain-value tags
        ("  ana  marija  ",     "Ana Marija"),
        ("ana   marija",        "Ana Marija"),
    ],
)
def test_whitespace_normalized(raw, expected):
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == expected


def test_preserves_diacritic_codepoints():
    """Č/Š/Ž survive the round-trip in both cases."""
    raw = "č š ž /Č Š Ž/"
    result, warning = clean_name_capitalization(raw)
    assert warning is None
    assert result == "Č Š Ž /Č Š Ž/"
    for ch in "ČŠŽ":
        assert ch in result, f"missing {ch!r}"


# ---------------------------------------------------------------------------
# Integration: only INDI names are touched; SUBM / SOUR NAME tags are left alone
# ---------------------------------------------------------------------------

def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_only_indi_names_are_capitalized():
    sample = textwrap.dedent("""\
        0 HEAD
        1 CHAR UTF-8
        0 @I1@ INDI
        1 NAME jože /KOVAČ/
        0 @I2@ INDI
        1 NAME De Coll Uršula/Ranga/
        0 @I3@ INDI
        1 NAME Maar Louise Wopkeline Beatrice/De/
        0 @SUBM1@ SUBM
        1 NAME some SUBMITTER name
        0 @S1@ SOUR
        1 AUTH some AUTHOR
        1 TITL some TITLE
        0 TRLR
    """)
    inp = _write_tmp(sample)
    out = _write_tmp("")
    try:
        process_file(
            inp, out,
            cleaners=["name_capitalization"],
            strippers=[],
            transformers=[],
            warn=False,
        )
        content = open(out, encoding="utf-8").read()
        # INDI names normalized.
        assert "1 NAME Jože /Kovač/" in content
        assert "1 NAME De Coll Uršula/Ranga/" in content
        assert "1 NAME Maar Louise Wopkeline Beatrice/De/" in content
        # SUBM NAME is NOT touched.
        assert "1 NAME some SUBMITTER name" in content
        # SOUR AUTH/TITL untouched (they are not name tags anyway, but check
        # that we did not accidentally normalize them through some other path).
        assert "1 AUTH some AUTHOR" in content
        assert "1 TITL some TITLE" in content
    finally:
        os.unlink(inp)
        os.unlink(out)
