import pytest
from tools.gedcom_cleaner import clean_date_dd_mmm_yyyy


@pytest.mark.parametrize("raw, expected", [
    # Date ranges — FROM/TO
    ("FROM 1767 TO 1770",               "FROM 1767 TO 1770"),
    ("from 1767 to 1770",               "FROM 1767 TO 1770"),
    ("FROM 15 JAN 1800 TO 20 MAR 1810", "FROM 15 JAN 1800 TO 20 MAR 1810"),
    ("FROM JAN 1800 TO MAR 1810",       "FROM JAN 1800 TO MAR 1810"),
    ("FROM 1900",                        "FROM 1900"),
    ("TO 1900",                          "TO 1900"),

    # Date ranges — BET/BETWEEN
    ("BET 1856 AND 1881",               "BET 1856 AND 1881"),
    ("BETWEEN 1856 AND 1881",           "BET 1856 AND 1881"),
    ("between 1856 and 1881",           "BET 1856 AND 1881"),
    ("BETWEEN 1856-1881",               "BET 1856 AND 1881"),
    ("BET 15 JAN 1800 AND 20 MAR 1810", "BET 15 JAN 1800 AND 20 MAR 1810"),

    # Implicit year ranges (YYYY-YYYY) — kept as-is
    ("1856-1881",   "1856-1881"),
    ("1979-1980",   "1979-1980"),

    # Placeholder dates — partial (year known), underscore and hyphen variants
    (".__.1933",    "1933"),
    ("__.__.1933",  "1933"),
    ("__.1933",     "1933"),
    (".--.1968",    "1968"),
    ("--.--1933",   "1933"),

    # Numeric date with extra/missing delimiters
    ("16.07. 1947",  "16 JUL 1947"),
    ("17.11 1930",   "17 NOV 1930"),

    # Slovenian format (D. M. YYYY)
    ("31. 3. 1931",  "31 MAR 1931"),
    ("1. 1. 1900",   "1 JAN 1900"),
    ("5. 12. 2000",  "5 DEC 2000"),

    # Already canonical
    ("15 JAN 1900",     "15 JAN 1900"),
    ("1 MAR 2001",      "1 MAR 2001"),
    ("MAY 1845",        "MAY 1845"),
    ("1923",            "1923"),

    # Month name variants
    ("15 January 1900", "15 JAN 1900"),
    ("15 january 1900", "15 JAN 1900"),
    ("Jan 15, 1900",    "15 JAN 1900"),
    ("Jan 15 1900",     "15 JAN 1900"),

    # Separators
    ("15-JAN-1900",     "15 JAN 1900"),
    ("15/JAN/1900",     "15 JAN 1900"),

    # ISO / numeric month
    ("1900-01-15",      "15 JAN 1900"),
    ("15.01.1900",      "15 JAN 1900"),
    ("15/01/1900",      "15 JAN 1900"),

    # Prefixes — normalised to GEDCOM canonical prefix
    ("Abt 15 JAN 1900",   "ABT 15 JAN 1900"),
    ("Abt. 15 JAN 1900",  "ABT 15 JAN 1900"),
    ("abt. 15 JAN 1900",  "ABT 15 JAN 1900"),
    ("About 15 JAN 1900", "ABT 15 JAN 1900"),
    ("about 15 jan 1900", "ABT 15 JAN 1900"),
    ("~ 15 JAN 1900",     "ABT 15 JAN 1900"),
    ("~1875",             "ABT 1875"),
    ("~15 JAN 1900",      "ABT 15 JAN 1900"),
    ("<1734",             "BEF 1734"),
    ("<1900",             "BEF 1900"),
    (">1900",             "AFT 1900"),
    ("> 1900",            "AFT 1900"),
    ("Bef. 1900",         "BEF 1900"),
    ("Bef 1900",          "BEF 1900"),
    ("before 1900",       "BEF 1900"),
    ("After 1900",        "AFT 1900"),
    ("Aft. MAR 1900",     "AFT MAR 1900"),
    ("Est. 1900",         "EST 1900"),
    ("Circa 1900",        "CAL 1900"),
])
def test_clean_date_success(raw, expected):
    result, warning = clean_date_dd_mmm_yyyy(raw)
    assert warning is None, f"Unexpected warning for '{raw}': {warning}"
    assert result == expected


@pytest.mark.parametrize("raw", [
    ".__.____",
    "__.__.____",
    "__.____",
    ".--.----",
    "--.--",
])
def test_clean_date_remove(raw):
    """Fully unknown placeholder dates return ('', None) — signal to remove the element."""
    result, warning = clean_date_dd_mmm_yyyy(raw)
    assert warning is None, f"Unexpected warning for '{raw}': {warning}"
    assert result == ""


@pytest.mark.parametrize("raw", [
    "not a date",
    "??",
    "sometime",
    "",
    "15 FOO 1900",   # bad month
    "15.13.1900",    # invalid month number
])
def test_clean_date_warns(raw):
    result, warning = clean_date_dd_mmm_yyyy(raw)
    assert warning is not None, f"Expected warning for '{raw}'"
    assert result is None
