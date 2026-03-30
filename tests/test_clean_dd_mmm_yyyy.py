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

    # Date ranges — DO (Slovenian/German "to")
    ("1920 DO 1945",          "FROM 1920 TO 1945"),
    ("FROM 1920 DO 1945",     "FROM 1920 TO 1945"),

    # Implicit year ranges (YYYY-YYYY) — kept as-is, spaces around hyphen allowed
    ("1856-1881",   "1856-1881"),
    ("1979-1980",   "1979-1980"),
    ("1941 - 1945", "1941-1945"),

    # Multiple leading dots/spaces as placeholder (unknown day/month)
    ("..1920",       "1920"),
    ("...1964",      "1964"),
    (".....1714",    "1714"),
    (".  .1993",     "1993"),
    (".  .1666",     "1666"),
    ("..MAJ 1945",   "MAY 1945"),

    # Placeholder dates — partial (year known), underscore and hyphen variants
    (".__.1933",    "1933"),
    ("__.__.1933",  "1933"),
    ("__.1933",     "1933"),
    (".--.1968",    "1968"),
    (".<>.1750",    "1750"),
    (".<>.1850",    "1850"),
    ("--.--1933",   "1933"),
    ("._.1894",     "1894"),
    ("._.1966",     "1966"),

    # .MM.YYYY (unknown day, leading dot placeholder)
    (".11.1952",  "NOV 1952"),
    (".03.1947",  "MAR 1947"),
    (".03.1970",  "MAR 1970"),

    # Trailing dot, apostrophe, or " ?" (uncertain)
    ("12.09.1945.",      "12 SEP 1945"),
    ("06.11.1920'",      "6 NOV 1920"),
    ("26.10.1903 ?",     "ABT 26 OCT 1903"),
    ("26.10.1903 ??",    "ABT 26 OCT 1903"),
    ("09.01.1958 ?",     "ABT 9 JAN 1958"),
    ("1720?",            "ABT 1720"),
    ("1720??",           "ABT 1720"),

    # MM.DD.YYYY fallback when month > 12
    ("04.14.1902",   "14 APR 1902"),
    ("06.31.1800",   "31 JUN 1800"),

    # Trailing dot
    ("01.09.1978.",  "1 SEP 1978"),

    # Numeric date with extra/missing delimiters
    ("16.07. 1947",  "16 JUL 1947"),
    ("17.11 1930",   "17 NOV 1930"),

    # Slovenian format (D. M. YYYY)
    ("31. 3. 1931",  "31 MAR 1931"),
    ("1. 1. 1900",   "1 JAN 1900"),
    ("5. 12. 2000",  "5 DEC 2000"),

    # MMM.YYYY (dot separator)
    ("FEB.1922",  "FEB 1922"),
    ("DEC.1944",  "DEC 1944"),

    # Already canonical
    ("15 JAN 1900",     "15 JAN 1900"),
    ("1 MAR 2001",      "1 MAR 2001"),
    ("MAY 1845",        "MAY 1845"),
    ("1923",            "1923"),

    # Numeric month + year only (no day)
    ("04 1883",  "APR 1883"),
    ("03 1818",  "MAR 1818"),

    # Slovenian genitive month forms
    ("30. APRILA 1998",  "30 APR 1998"),
    ("28. MAJA 1893",    "28 MAY 1893"),
    ("10. APRILA 1863",  "10 APR 1863"),

    # FEBR. abbreviation
    ("27. FEBR. 1923",  "27 FEB 1923"),
    ("1. FEBR. 1915",   "1 FEB 1915"),

    # Month name variants — English
    ("15 January 1900", "15 JAN 1900"),
    ("15 january 1900", "15 JAN 1900"),
    ("Jan 15, 1900",    "15 JAN 1900"),
    ("Jan 15 1900",     "15 JAN 1900"),

    # German long month names
    ("15 Januar 1900",   "15 JAN 1900"),
    ("15 März 1900",     "15 MAR 1900"),
    ("15 Maerz 1900",    "15 MAR 1900"),
    ("15 Mai 1900",      "15 MAY 1900"),
    ("15 Oktober 1900",  "15 OCT 1900"),
    ("15 Dezember 1900", "15 DEC 1900"),

    # German short month names
    ("15 Okt 1900",  "15 OCT 1900"),
    ("15 Okt. 1900", "15 OCT 1900"),
    ("15 Dez 1900",  "15 DEC 1900"),
    ("15 Dez. 1900", "15 DEC 1900"),
    ("15 Mär 1900",  "15 MAR 1900"),
    ("15 Mär. 1900", "15 MAR 1900"),
    ("15 Mrz 1900",  "15 MAR 1900"),

    # Slovenian long month names
    ("15 Marec 1900",    "15 MAR 1900"),
    ("15 Maj 1900",      "15 MAY 1900"),
    ("15 Junij 1900",    "15 JUN 1900"),
    ("15 Julij 1900",    "15 JUL 1900"),
    ("15 Avgust 1900",   "15 AUG 1900"),
    ("15 Oktober 1900",  "15 OCT 1900"),

    # Slovenian short month names
    ("15 Avg 1900",  "15 AUG 1900"),
    ("15 Avg. 1900", "15 AUG 1900"),

    # Separators
    ("15-JAN-1900",     "15 JAN 1900"),
    ("15/JAN/1900",     "15 JAN 1900"),

    # ISO / numeric month
    ("1900-01-15",      "15 JAN 1900"),
    ("15.01.1900",      "15 JAN 1900"),
    ("15/01/1900",      "15 JAN 1900"),

    # Extra spaces between tokens
    ("24   MRZ 1975",  "24 MAR 1975"),
    ("29   MRZ 1975",  "29 MAR 1975"),
    ("15   JAN  1900", "15 JAN 1900"),

    # Mixed delimiters and spaces between numeric day/month/year
    ("11. 09.1960",  "11 SEP 1960"),
    ("20.01,1722",   "20 JAN 1722"),
    ("31 05.1756",   "31 MAY 1756"),

    # Colon as delimiter
    ("25:NOV.1850",  "25 NOV 1850"),

    # Separator before month, none between month and year
    ("18.FEB1732",   "18 FEB 1732"),
    ("23.FEB1930",   "23 FEB 1930"),

    # No separator between day and month name
    ("11FEB.1694",   "11 FEB 1694"),

    # Leading dot/comma placeholder with named month
    (".MAJ.1693",    "MAY 1693"),
    (",MAJ 1945",    "MAY 1945"),

    # DDMM.YYYY or DDMM YYYY — no separator between day and month
    ("0208.1902",    "2 AUG 1902"),
    ("0702 1729",    "7 FEB 1729"),

    # Tilde prefix with dot separator in date
    ("~APR.1967",    "ABT APR 1967"),

    # No separator between month name and year
    ("NOV1839",      "NOV 1839"),
    ("JAN1900",      "JAN 1900"),

    # Letter O misread as digit 0 (OCR/typo) — word boundary and between digits
    ("O6 FEB 1918",   "6 FEB 1918"),
    ("09.06.19O6",    "9 JUN 1906"),

    # Missing delimiter between month and year (DD.MMYYYY)
    ("21.041831",     "21 APR 1831"),
    ("30.051694",     "30 MAY 1694"),

    # Parentheses stripped — plain year treated as-is, uncertain context as ABT
    ("(1620)",        "1620"),
    (".-.(1740)",     "1740"),
    (".-. (1775)",    "1775"),

    # L. / L prefix (Slovenian/German "Leto/Jahr" = year) → strip, keep year
    ("L.1610",   "1610"),
    ("L.1880",   "1880"),
    ("L 1880",   "1880"),

    # Leading dot with placeholder characters for unknown day/month
    (".<,1820",  "1820"),

    # Leading = stripped (exact date marker, no GEDCOM equivalent)
    ("=1971",   "1971"),
    ("=1840",   "1840"),

    # Trailing ? → ABT
    ("1964?",   "ABT 1964"),
    ("1917?",   "ABT 1917"),

    # Leading comma placeholder (unknown day)
    (",06.1590",  "JUN 1590"),

    # OKR / OK / CA prefixes → ABT
    ("OKR. 1700",  "ABT 1700"),
    ("OKR. 1733",  "ABT 1733"),
    ("okr 1850",   "ABT 1850"),
    ("OK.1890",    "ABT 1890"),
    ("OK.1910",    "ABT 1910"),
    ("CA 1972",    "ABT 1972"),
    ("CA 1780",    "ABT 1780"),
    ("ca. 1865",   "ABT 1865"),

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
    ("Pred 1900",         "BEF 1900"),
    ("pred 1900",         "BEF 1900"),
    ("Vor 1900",          "BEF 1900"),
    ("vor 1900",          "BEF 1900"),
    ("After 1900",        "AFT 1900"),
    ("Aft. MAR 1900",     "AFT MAR 1900"),
    ("PO 1736",           "AFT 1736"),
    ("PO MAJU 1875",      "AFT MAY 1875"),
    ("~~ 1968",           "ABT 1968"),

    ("Est. 1900",         "EST 1900"),
    ("Circa 1900",        "ABT 1900"),
    ("Cca. 1340",         "ABT 1340"),
    ("cca 994",           "ABT 994"),
    ("CCA 1250",          "ABT 1250"),
    ("okoli 1850",        "ABT 1850"),
    ("OKOLI 1700",        "ABT 1700"),
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
    "/",
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
