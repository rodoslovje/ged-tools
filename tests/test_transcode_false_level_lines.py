import pytest
from tools.gedcom_cleaner import _rejoin_false_level_lines


def _lines(*lines: bytes) -> bytes:
    return b"\n".join(lines) + b"\n"


def test_stray_digit_prefixed_fragment_is_folded_back():
    # A note value with a raw newline embedded in it; the second physical
    # line happens to start with "7 ", which looks like a real GEDCOM
    # "<level> <tag>" line one level too deep to be valid.
    raw = _lines(
        b"0 HEAD",
        b"0 @I1@ INDI",
        b"1 NOTE by merely quoting stanza",
        b"7 from the Psalm of Life:",
        b"0 TRLR",
    )
    result = _rejoin_false_level_lines(raw)
    assert result == _lines(
        b"0 HEAD",
        b"0 @I1@ INDI",
        b"1 NOTE by merely quoting stanza 7 from the Psalm of Life:",
        b"0 TRLR",
    )


def test_legitimate_deeper_level_is_untouched():
    raw = _lines(
        b"0 @I1@ INDI",
        b"1 BIRT",
        b"2 DATE 1 JAN 1900",
        b"0 TRLR",
    )
    assert _rejoin_false_level_lines(raw) == raw


def test_bare_continuation_without_digit_prefix_is_untouched():
    # Handled by the GEDCOM parser's own quirk fallback; nothing to fold here.
    raw = _lines(
        b"0 @I1@ INDI",
        b"1 NOTE some text",
        b"from fans throughout the State.",
        b"0 TRLR",
    )
    assert _rejoin_false_level_lines(raw) == raw


def test_multiple_consecutive_false_level_fragments_fold_into_one_line():
    raw = _lines(
        b"0 @I1@ INDI",
        b"1 NOTE line one",
        b"7 line two",
        b"9 line three",
        b"0 TRLR",
    )
    result = _rejoin_false_level_lines(raw)
    assert result == _lines(
        b"0 @I1@ INDI",
        b"1 NOTE line one 7 line two 9 line three",
        b"0 TRLR",
    )
