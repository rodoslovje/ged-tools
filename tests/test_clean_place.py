import pytest
from tools.gedcom_cleaner import (
    clean_place_slovenia_rm,
    clean_place_duplicate_rm,
    clean_place_country_only,
)


# ---------------------------------------------------------------------------
# place_slovenia_rm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Ljubljana, Slovenia",              "Ljubljana"),
    ("Ljubljana, Slovenija",             "Ljubljana"),
    ("Maribor, Štajerska, Slovenia",     "Maribor, Štajerska"),
    ("Koper, Slovenia, extra",           "Koper"),  # everything after Slovenia removed
    ("Ljubljana, SLOVENIA",              "Ljubljana"),
    ("Ljubljana, slovenija",             "Ljubljana"),
    # no Slovenia — unchanged
    ("Ljubljana",                        "Ljubljana"),
    ("Vienna, Austria",                  "Vienna, Austria"),
    ("",                                 ""),
])
def test_place_slovenia_rm(raw, expected):
    result, warning = clean_place_slovenia_rm(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# place_duplicate_rm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Ljubljana, Ljubljana",             "Ljubljana"),
    ("Ljubljana, LJUBLJANA",             "Ljubljana"),
    ("Ljubljana, Ljubljana, Slovenia",   "Ljubljana, Slovenia"),
    ("A, B, B, C",                       "A, B, C"),
    # no duplicates — unchanged
    ("Ljubljana, Slovenia",              "Ljubljana, Slovenia"),
    ("Ljubljana",                        "Ljubljana"),
    ("",                                 ""),
])
def test_place_duplicate_rm(raw, expected):
    result, warning = clean_place_duplicate_rm(raw)
    assert warning is None
    assert result == expected


# ---------------------------------------------------------------------------
# place_country_only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Ljubljana, Osrednjeslovenska, Slovenia",    "Ljubljana, Slovenia"),
    ("Celje, Savinjska, Štajerska, Slovenia",     "Celje, Slovenia"),
    # already two parts — unchanged
    ("Ljubljana, Slovenia",                       "Ljubljana, Slovenia"),
    # single component — unchanged
    ("Ljubljana",                                 "Ljubljana"),
    ("",                                          ""),
])
def test_place_country_only(raw, expected):
    result, warning = clean_place_country_only(raw)
    assert warning is None
    assert result == expected
