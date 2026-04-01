import pytest
from tools.gedcom_cleaner import clean_name_placeholder


@pytest.mark.parametrize(
    "raw",
    [
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
        "(?)",
        "(???)",
        "(????)",
        "(?????)",
        "_____--",
        "___--___",
        "__ __",
        "(?) (?)",
        "[_?_]",
        "<_>",
        "-?-",
        "...",
        "? / ?",
    ],
)
def test_name_placeholder_cleared(raw):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == ""


@pytest.mark.parametrize(
    "raw",
    [
        "John /Smith/",
        "Ana",
        "/Novak/",
        "J. /Doe/",
        "",  # already empty — no change
    ],
)
def test_name_placeholder_kept(raw):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == raw


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Jane /___/", "Jane //"),
        ("___ /Smith/", "/Smith/"),
        ("___ Jane /Smith/", "___ Jane /Smith/"),
        ("Jane /_?_/", "Jane //"),
        ("___ /___/", ""),
        ("John  /Smith/  ", "John /Smith/"),  # checks extra space normalization
        ("Jože - Pepi", "Jože - Pepi"),
        ("Jože - Pepi /Surname/", "Jože - Pepi /Surname/"),
    ],
)
def test_name_placeholder_partial(raw, expected):
    result, warning = clean_name_placeholder(raw)
    assert warning is None
    assert result == expected
