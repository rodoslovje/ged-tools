import pytest
from tools.gedcom_cleaner import clean_name_placeholder


@pytest.mark.parametrize("raw", [
    "___",
    "???",
    "______",
    "??????",
    "/___/",
    "/???/",
    "___ /___/",
    "??? /???/",
    "_ /_/",
    "   ___   ",
])
def test_name_placeholder_cleared(raw):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == ""


@pytest.mark.parametrize("raw", [
    "John /Smith/",
    "Ana",
    "/Novak/",
    "J. /Doe/",
    "",          # already empty — no change
])
def test_name_placeholder_kept(raw):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == raw
