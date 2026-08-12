from tools.gedcom_cleaner import _fold_embedded_lf_lines, _rejoin_false_level_lines


def _crlf(*lines: bytes) -> bytes:
    return b"\r\n".join(lines) + b"\r\n"


def _filler(count: int) -> list[bytes]:
    # _fold_embedded_lf_lines only trusts the terminator when CRLF clearly
    # dominates, so a fixture needs more CRLF lines than embedded LFs.
    return [b"1 RIN MH:I%d" % i for i in range(count)]


def test_embedded_lf_becomes_cont_one_level_deeper():
    raw = _crlf(
        b"0 @I1@ INDI",
        b"1 NOTE Krstna knjiga\nPoro\xc4\x8dna knjiga",
        *_filler(5),
        b"0 TRLR",
    )
    assert _fold_embedded_lf_lines(raw) == _crlf(
        b"0 @I1@ INDI",
        b"1 NOTE Krstna knjiga",
        b"2 CONT Poro\xc4\x8dna knjiga",
        *_filler(5),
        b"0 TRLR",
    )


def test_embedded_lf_under_conc_stays_at_the_same_level():
    raw = _crlf(
        b"0 @I1@ INDI",
        b"1 NOTE zapis",
        b"2 CONC ---\n1822 k.o. Rakitna, Alphabetical list of landowners",
        *_filler(5),
        b"0 TRLR",
    )
    assert _fold_embedded_lf_lines(raw) == _crlf(
        b"0 @I1@ INDI",
        b"1 NOTE zapis",
        b"2 CONC ---",
        b"2 CONT 1822 k.o. Rakitna, Alphabetical list of landowners",
        *_filler(5),
        b"0 TRLR",
    )


def test_blank_fragment_becomes_a_valueless_cont():
    raw = _crlf(
        b"0 @I1@ INDI",
        b"1 NOTE prva\n\ndruga",
        *_filler(5),
        b"0 TRLR",
    )
    assert _fold_embedded_lf_lines(raw) == _crlf(
        b"0 @I1@ INDI",
        b"1 NOTE prva",
        b"2 CONT",
        b"2 CONT druga",
        *_filler(5),
        b"0 TRLR",
    )


def test_lf_terminated_file_is_left_alone():
    # No CRLF at all: the bare LFs are the record separators, not stray text.
    raw = b"\n".join([b"0 @I1@ INDI", b"1 NOTE besedilo", b"0 TRLR"]) + b"\n"
    assert _fold_embedded_lf_lines(raw) == raw


def test_mostly_lf_file_is_left_alone():
    # A handful of CRLF lines in an otherwise LF-terminated export must not
    # flip the whole file into CRLF interpretation.
    raw = b"0 @I1@ INDI\n1 NOTE besedilo\r\n1 SEX M\n0 TRLR\n"
    assert _fold_embedded_lf_lines(raw) == raw


def test_fragment_of_a_non_gedcom_line_is_left_to_the_parser():
    # The head is already loose text, so there is no level to hang a CONT
    # off — leave the whole run for the parser's bare-continuation fallback.
    raw = _crlf(
        b"0 @I1@ INDI",
        b"1 NOTE besedilo",
        b"<p>pasted html</p>\nse\xc5\xbee naprej",
        *_filler(5),
        b"0 TRLR",
    )
    assert _fold_embedded_lf_lines(raw) == raw


def test_clean_crlf_file_is_unchanged():
    raw = _crlf(b"0 HEAD", b"0 @I1@ INDI", b"1 BIRT", b"2 DATE 1 JAN 1900", b"0 TRLR")
    assert _fold_embedded_lf_lines(raw) == raw


def test_utf8_bom_does_not_swallow_the_header():
    # The BOM sits in front of "0 HEAD"; if it hides the level of the first
    # line, every subordinate header line looks like a level jump and the
    # whole HEAD record gets folded onto one line.
    raw = _crlf(
        b"\xef\xbb\xbf0 HEAD",
        b"1 GEDC",
        b"2 VERS 5.5.1",
        b"1 CHAR UTF-8",
        b"0 TRLR",
    )
    assert _rejoin_false_level_lines(raw) == raw


def test_rejoin_handles_fragment_whose_remainder_is_not_a_valid_value():
    # "1822 k.o. Rakitna" parses as level 1822 / tag "k" in the GEDCOM
    # parser's non-strict fallback, so the LF-only path has to catch it too.
    raw = (
        b"\n".join(
            [
                b"0 @I1@ INDI",
                b"1 NOTE zemlji\xc5\xa1ka knjiga",
                b"1822 k.o. Rakitna, Alphabetical list of landowners",
                b"0 TRLR",
            ]
        )
        + b"\n"
    )
    assert _rejoin_false_level_lines(raw) == (
        b"\n".join(
            [
                b"0 @I1@ INDI",
                b"1 NOTE zemlji\xc5\xa1ka knjiga 1822 k.o. Rakitna, "
                b"Alphabetical list of landowners",
                b"0 TRLR",
            ]
        )
        + b"\n"
    )
