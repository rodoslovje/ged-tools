import pytest
from tools.gedcom_cleaner import clean_place_placeholder


@pytest.mark.parametrize("raw", [
    "___",
    "???",
    "___, ___, ___",
    "???, ???",
    "_, _, _",
    "   ___   ",
    ",,,",
    "_, ?,",
])
def test_place_placeholder_cleared(raw):
    result, warning = clean_place_placeholder(raw)
    assert warning is None
    assert result == ""


@pytest.mark.parametrize("raw", [
    "Ljubljana",
    "Ljubljana, Slovenia",
    "Maribor, Štajerska, Slovenia",
    "/Unknown/",
    "",          # already empty — no change
])
def test_place_placeholder_kept(raw):
    result, warning = clean_place_placeholder(raw)
    assert warning is None
    assert result == raw
