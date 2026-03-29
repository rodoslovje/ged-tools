#!/usr/bin/env python3
"""
gedcom-cleaner: Read a GEDCOM file and save it in a cleaned format.

Usage:
    python tools/gedcom_cleaner.py <input.ged> <output.ged> --cleaners <c1,c2,...> [--warn]

Available cleaners:
    dd_mmm_yyyy   Normalize all dates to DD MMM YYYY format (e.g. "15 JAN 1900").
                  Genealogy prefixes Abt./Bef. are standardized.
"""

import argparse
import os
import re
import sys
import tempfile

import chardet
from gedcom.parser import Parser
import gedcom.tags


# ---------------------------------------------------------------------------
# Encoding detection & transcoding
# ---------------------------------------------------------------------------

# Maps GEDCOM CHAR tag values to Python codec names
_GEDCOM_CHAR_MAP = {
    "UTF-8":        "utf-8",
    "UNICODE":      "utf-16",
    "UTF-16":       "utf-16",
    "ASCII":        "ascii",
    "ANSI":         "windows-1252",
    "WINDOWS-1250": "windows-1250",
    "WINDOWS-1251": "windows-1251",
    "WINDOWS-1252": "windows-1252",
    "CP1250":       "windows-1250",
    "CP1251":       "windows-1251",
    "CP1252":       "windows-1252",
    "IBM":          "cp437",
    "IBM-PC":       "cp437",
    "IBMPC":        "cp437",
    "OEM":          "cp437",
    "MACOS":        "mac_roman",
    "MAC":          "mac_roman",
    "ISO-8859-1":   "iso-8859-1",
    "LATIN1":       "iso-8859-1",
    "LATIN-1":      "iso-8859-1",
    "ISO8859-1":    "iso-8859-1",
}


def _detect_encoding(file_path: str) -> str:
    """Detect encoding of a GEDCOM file. Returns a Python codec name."""
    with open(file_path, "rb") as f:
        raw = f.read()

    # 1. BOM detection
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"

    # 2. GEDCOM CHAR header tag (scan first 4 KB decoded as latin-1 — safe for ASCII header)
    header_text = raw[:4096].decode("latin-1", errors="replace")
    m = re.search(r"^\d\s+CHAR\s+(.+)$", header_text, re.MULTILINE | re.IGNORECASE)
    if m:
        char_value = m.group(1).strip().upper()
        if char_value in _GEDCOM_CHAR_MAP:
            return _GEDCOM_CHAR_MAP[char_value]

    # 3. chardet fallback
    detected = chardet.detect(raw)
    if detected and detected.get("encoding"):
        return detected["encoding"]

    # 4. Last resort — latin-1 never raises UnicodeDecodeError
    return "latin-1"


def _transcode_to_utf8(input_path: str) -> tuple[str, bool]:
    """
    Ensure the file is UTF-8. If it already is, returns (input_path, False).
    Otherwise decodes it and writes a temp UTF-8 file, returns (tmp_path, True).
    Caller must delete the temp file when done (only when second value is True).
    """
    encoding = _detect_encoding(input_path)
    if encoding.lower().replace("-", "").replace("_", "") in ("utf8", "utf8sig"):
        return input_path, False

    with open(input_path, "rb") as f:
        raw = f.read()

    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode("latin-1")

    fd, tmp_path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)

    return tmp_path, True


def _record_label(element) -> str:
    """Return a human-readable label for the level-0 record containing element."""
    el = element
    while el.get_parent_element() and el.get_parent_element().get_level() >= 0:
        el = el.get_parent_element()

    tag = el.get_tag()
    pointer = el.get_pointer()

    if tag == gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
        for child in el.get_child_elements():
            if child.get_tag() == gedcom.tags.GEDCOM_TAG_NAME:
                name = child.get_value().replace("/", " ").split()
                name = " ".join(name)  # collapse extra whitespace from slashes
                if name:
                    return f"INDI {pointer} — {name}"
        return f"INDI {pointer}"

    if tag == gedcom.tags.GEDCOM_TAG_FAMILY:
        return f"FAM  {pointer}"

    return f"{tag} {pointer}".strip()


def _serialize(element) -> str:
    """Recursively serialize an element and all its descendants at any depth.
    Works around the library bug where to_gedcom_string(recursive=True) only
    goes one level deep."""
    result = element.to_gedcom_string(recursive=False)
    for child in element.get_child_elements():
        result += _serialize(child)
    return result


def _update_char_tag(parser: Parser) -> None:
    """Set the CHAR header tag to UTF-8 in the parsed tree."""
    for element in parser.get_element_list():
        if element.get_tag() == "CHAR":
            element.set_value("UTF-8")
            return


# ---------------------------------------------------------------------------
# Cleaner: dd_mmm_yyyy
# ---------------------------------------------------------------------------

MONTHS_LONG = {
    "january": "JAN", "february": "FEB", "march": "MAR",
    "april": "APR", "may": "MAY", "june": "JUN",
    "july": "JUL", "august": "AUG", "september": "SEP",
    "october": "OCT", "november": "NOV", "december": "DEC",
}

MONTHS_SHORT = {
    "jan": "JAN", "feb": "FEB", "mar": "MAR", "apr": "APR",
    "may": "MAY", "jun": "JUN", "jul": "JUL", "aug": "AUG",
    "sep": "SEP", "oct": "OCT", "nov": "NOV", "dec": "DEC",
    # common alternates
    "sept": "SEP",
}

# Map all known prefix variants to their canonical GEDCOM form
PREFIX_MAP = {
    "about":  "ABT",
    "abt.":   "ABT",
    "abt":    "ABT",
    "~":      "ABT",
    "<":      "BEF",
    ">":      "AFT",
    "before": "BEF",
    "bef.":   "BEF",
    "bef":    "BEF",
    "after":  "AFT",
    "aft.":   "AFT",
    "aft":    "AFT",
    "circa":  "CAL",
    "cal.":   "CAL",
    "cal":    "CAL",
    "cca.":   "ABT",
    "cca":    "ABT",
    "okoli":  "ABT",
    "est.":   "EST",
    "est":    "EST",
}

# Regex pieces
_DAY   = r"(?P<day>\d{1,2})"
_MONTH = r"(?P<month>[A-Za-z]+\.?)"
_YEAR  = r"(?P<year>\d{3,4})"

# Full date patterns (most specific first)
DATE_PATTERNS = [
    # DD MMM YYYY  /  DD-MMM-YYYY  /  DD/MMM/YYYY
    re.compile(rf"^{_DAY}[\s\-/]{_MONTH}[\s\-/]{_YEAR}$"),
    # MMM DD YYYY  (e.g. "Jan 15 1900")
    re.compile(rf"^{_MONTH}\s+{_DAY}[,\s]+{_YEAR}$"),
    # YYYY-MM-DD  (ISO)
    re.compile(
        r"^(?P<year>\d{4})-(?P<monthnum>\d{1,2})-(?P<day>\d{1,2})$"
    ),
    # DD.MM.YYYY  or  DD/MM/YYYY  (numeric month, no spaces)
    re.compile(
        r"^(?P<day>\d{1,2})[./](?P<monthnum>\d{1,2})[./](?P<year>\d{3,4})$"
    ),
    # DD.MM. YYYY or DD.MM YYYY (numeric month, optional final dot, space before year)
    re.compile(
        r"^(?P<day>\d{1,2})[./]\s*(?P<monthnum>\d{1,2})\.?\s+(?P<year>\d{3,4})$"
    ),
    # .MM.YYYY  (unknown day, numeric month — leading dot placeholder)
    re.compile(
        r"^\.\s*(?P<monthnum>\d{1,2})\.\s*(?P<year>\d{3,4})$"
    ),
    # MMM YYYY  (no day)
    re.compile(rf"^{_MONTH}\s+{_YEAR}$"),
    # YYYY only
    re.compile(r"^(?P<year>\d{3,4})$"),
]


def _normalize_month_name(token: str) -> str | None:
    """Return 3-letter uppercase month or None if unrecognised."""
    t = token.lower().rstrip(".")
    if t in MONTHS_SHORT:
        return MONTHS_SHORT[t]
    if t in MONTHS_LONG:
        return MONTHS_LONG[t]
    return None


def _monthnum_to_abbr(num: str) -> str | None:
    abbrs = ["JAN","FEB","MAR","APR","MAY","JUN",
             "JUL","AUG","SEP","OCT","NOV","DEC"]
    n = int(num)
    if 1 <= n <= 12:
        return abbrs[n - 1]
    return None


def _parse_date_value(value: str) -> tuple[str | None, str | None]:
    """
    Try to parse a date string (without prefix).
    Returns (formatted_date, None) on success or (None, reason) on failure.
    Formatted date is like 'DD MMM YYYY', 'MMM YYYY', or 'YYYY'.
    """
    v = value.strip()

    for pat in DATE_PATTERNS:
        m = pat.match(v)
        if not m:
            continue
        gd = m.groupdict()

        year  = gd.get("year")
        day   = gd.get("day")
        month = None

        if "monthnum" in gd and gd["monthnum"]:
            month = _monthnum_to_abbr(gd["monthnum"])
            if month is None:
                return None, f"invalid month number in '{value}'"
        elif "month" in gd and gd["month"]:
            month = _normalize_month_name(gd["month"])
            if month is None:
                return None, f"unrecognised month '{gd['month']}' in '{value}'"

        parts = []
        if day:
            parts.append(str(int(day)))   # strip leading zero
        if month:
            parts.append(month)
        if year:
            parts.append(year)

        return " ".join(parts), None

    return None, f"unrecognised date format '{value}'"


_PLACEHOLDER_RE = re.compile(r"_+|[-]{2,}")  # _ / __ or -- placeholders


def _handle_placeholder(value: str) -> tuple[str, None] | None:
    """
    Handle dates that use __ / ____ as placeholders for unknown day/month/year.
    Returns ("", None)   if the date is fully unknown  → caller should remove the element.
    Returns (year, None) if only the year is known     → keep just the year.
    Returns None         if the value has no placeholders at all.
    """
    if not _PLACEHOLDER_RE.search(value):
        return None

    # Extract any real year (3-4 digits, not underscores)
    year_match = re.search(r"\b(\d{3,4})\b", value)
    if year_match:
        return year_match.group(1), None

    # Fully unknown
    return "", None


def _parse_range(value: str) -> tuple[str | None, str | None, bool]:
    """
    Try to parse value as a date range.
    Returns (result, error, is_range).
    - (result, None, True)  — successfully parsed range
    - (None, error, True)   — looks like a range but dates inside are invalid
    - (None, None, False)   — not a range at all
    """
    v = value.strip()

    def both(d1: str, d2: str, fmt: str) -> tuple[str | None, str | None, bool]:
        r1, e1 = _parse_date_value(d1)
        if e1:
            return None, e1, True
        r2, e2 = _parse_date_value(d2)
        if e2:
            return None, e2, True
        return fmt.format(r1, r2), None, True

    def one(d: str, fmt: str) -> tuple[str | None, str | None, bool]:
        r, e = _parse_date_value(d)
        if e:
            return None, e, True
        return fmt.format(r), None, True

    # FROM date TO date
    m = re.match(r"^FROM\s+(.+?)\s+TO\s+(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "FROM {} TO {}")

    # FROM date  (open-ended)
    m = re.match(r"^FROM\s+(.+)$", v, re.IGNORECASE)
    if m:
        return one(m.group(1), "FROM {}")

    # TO date  (open-ended)
    m = re.match(r"^TO\s+(.+)$", v, re.IGNORECASE)
    if m:
        return one(m.group(1), "TO {}")

    # BETWEEN/BET date AND date
    m = re.match(r"^(?:BETWEEN|BET)\s+(.+?)\s+AND\s+(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "BET {} AND {}")

    # BETWEEN/BET date - date  (hyphen separator)
    m = re.match(r"^(?:BETWEEN|BET)\s+(.+?)\s*-\s*(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "BET {} AND {}")

    # YYYY-YYYY  (plain year range, e.g. "1856-1881") — left as-is, handled below


    return None, None, False


def clean_date_dd_mmm_yyyy(raw: str) -> tuple[str | None, str | None]:
    """
    Normalise a raw GEDCOM DATE value.
    Returns (cleaned_value, warning_message).
    warning_message is None on success.
    """
    v = raw.strip()
    if not v:
        return None, "empty date value"

    # Handle __ placeholder dates first
    placeholder = _handle_placeholder(v)
    if placeholder is not None:
        return placeholder  # ("", None) = remove  |  (year, None) = keep year only

    # YYYY-YYYY year ranges are kept as-is (no conversion to FROM/TO)
    if re.match(r"^\d{3,4}-\d{3,4}$", v):
        return v, None

    # Try range patterns first (before prefix handling)
    result, err, is_range = _parse_range(v)
    if is_range:
        return result, err

    # Detect and strip prefix
    prefix_canon = None
    for variant, canon in PREFIX_MAP.items():
        # match whole word / token at start, case-insensitive
        pattern = re.compile(
            r"^" + re.escape(variant) + r"(?=\s|\d|$)", re.IGNORECASE
        )
        if pattern.match(v):
            prefix_canon = canon
            v = v[len(variant):].strip()
            break

    if not v:
        return None, f"date consists only of a prefix: '{raw}'"

    formatted, err = _parse_date_value(v)
    if err:
        return None, err

    result = f"{prefix_canon} {formatted}" if prefix_canon else formatted
    return result, None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CLEANERS = {
    "dd_mmm_yyyy": clean_date_dd_mmm_yyyy,
}


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_file(
    input_path: str,
    output_path: str,
    cleaners: list[str],
    warn: bool,
    verbose: bool = False,
) -> int:
    """Returns number of warnings emitted."""
    parse_path, is_tmp = _transcode_to_utf8(input_path)
    try:
        parser = Parser()
        try:
            parser.parse_file(parse_path, strict=False)
        except Exception as e:
            print(f"ERROR: could not parse '{input_path}': {e}", file=sys.stderr)
            sys.exit(1)
        _update_char_tag(parser)
    finally:
        if is_tmp:
            os.unlink(parse_path)

    warnings = 0

    if "dd_mmm_yyyy" in cleaners:
        cleaner_fn = CLEANERS["dd_mmm_yyyy"]
        current_label = None
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_DATE:
                continue
            raw = element.get_value()
            cleaned, warning = cleaner_fn(raw)
            if warning:
                warnings += 1
                if warn:
                    print(f"WARN [dd_mmm_yyyy]: {warning}", file=sys.stderr)
            else:
                if cleaned == "":
                    if verbose:
                        label = _record_label(element)
                        if label != current_label:
                            print(label)
                            current_label = label
                        print(f"  [dd_mmm_yyyy] {raw!r} -> (removed)")
                    element.get_parent_element().get_child_elements().remove(element)
                elif cleaned != raw:
                    if verbose:
                        label = _record_label(element)
                        if label != current_label:
                            print(label)
                            current_label = label
                        print(f"  [dd_mmm_yyyy] {raw!r} -> {cleaned!r}")
                    element.set_value(cleaned)

    parser.invalidate_cache()
    try:
        with open(output_path, "w", encoding="utf-8-sig") as f:
            for element in parser.get_root_child_elements():
                f.write(_serialize(element))
    except OSError as e:
        print(f"ERROR: could not write '{output_path}': {e}", file=sys.stderr)
        sys.exit(1)

    return warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Clean and normalise a GEDCOM file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input",  help="Input GEDCOM file (.ged)")
    parser.add_argument("output", help="Output GEDCOM file (.ged)")
    parser.add_argument(
        "--cleaners",
        required=True,
        metavar="CLEANER[,CLEANER,...]",
        help=f"Comma-separated list of cleaners to apply. Available: {', '.join(CLEANERS)}",
    )
    parser.add_argument(
        "--warn",
        action="store_true",
        help="Print dates that could not be converted to stderr",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every date conversion performed",
    )

    args = parser.parse_args()

    requested = [c.strip() for c in args.cleaners.split(",")]
    unknown = [c for c in requested if c not in CLEANERS]
    if unknown:
        print(
            f"ERROR: unknown cleaner(s): {', '.join(unknown)}. "
            f"Available: {', '.join(CLEANERS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    warnings = process_file(args.input, args.output, requested, args.warn, args.verbose)

    if warnings:
        note = " (use --warn to see details)" if not args.warn else ""
        print(f"{warnings} date(s) could not be converted{note}.", file=sys.stderr)

    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
