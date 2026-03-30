#!/usr/bin/env python3
"""
gedcom-cleaner: Read a GEDCOM file and save it in a cleaned format.

Usage:
    python tools/gedcom_cleaner.py <input.ged> <output.ged> --clean <c1,c2,...> [--warn]

Available cleaners:
    dd_mmm_yyyy   Normalize all dates to DD MMM YYYY format (e.g. "15 JAN 1900").
                  Genealogy prefixes Abt./Bef. are standardized.
"""

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field

import chardet
from gedcom.element.element import Element
from gedcom.parser import Parser
import gedcom.tags


# ---------------------------------------------------------------------------
# Encoding detection & transcoding
# ---------------------------------------------------------------------------

# Maps GEDCOM CHAR tag values to Python codec names
_GEDCOM_CHAR_MAP = {
    "UTF-8": "utf-8",
    "UNICODE": "utf-16",
    "UTF-16": "utf-16",
    "ASCII": "ascii",
    # "ANSI" is intentionally omitted — it is ambiguous (cp1252 for Western European,
    # cp1250 for Central/Eastern European). chardet is more reliable for distinguishing them.
    "WINDOWS-1250": "windows-1250",
    "WINDOWS-1251": "windows-1251",
    "WINDOWS-1252": "windows-1252",
    "CP1250": "windows-1250",
    "CP1251": "windows-1251",
    "CP1252": "windows-1252",
    "IBM": "cp437",
    "IBM-PC": "cp437",
    "IBMPC": "cp437",
    "OEM": "cp437",
    "MACOS": "mac_roman",
    "MAC": "mac_roman",
    "ISO-8859-1": "iso-8859-1",
    "LATIN1": "iso-8859-1",
    "LATIN-1": "iso-8859-1",
    "ISO8859-1": "iso-8859-1",
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
    with open(input_path, "rb") as f:
        raw = f.read()

    encoding = _detect_encoding(input_path)

    # If detected as utf-8/utf-8-sig, verify it actually decodes cleanly.
    # chardet can misidentify cp1250/cp1252 as utf-8 when the file has few
    # high bytes — a failed decode means we must re-detect without that assumption.
    norm = encoding.lower().replace("-", "").replace("_", "")
    if norm in ("utf8", "utf8sig"):
        try:
            raw.decode(encoding)
            return input_path, False  # genuine UTF-8 — pass through unchanged
        except UnicodeDecodeError:
            # Not real UTF-8: fall back to chardet ignoring the utf-8 guess
            detected = chardet.detect(raw)
            enc = detected.get("encoding") if detected else None
            encoding = enc if enc and "utf" not in enc.lower() else "windows-1250"

    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode("latin-1")

    fd, tmp_path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)

    return tmp_path, True


_EVENT_LABELS: dict[str, str] = {
    gedcom.tags.GEDCOM_TAG_BIRTH:    "birth",
    gedcom.tags.GEDCOM_TAG_DEATH:    "death",
    gedcom.tags.GEDCOM_TAG_MARRIAGE: "marriage",
    "BURI": "burial",
    "CHR":  "christening",
    "DIV":  "divorce",
    "EMIG": "emigration",
    "IMMI": "immigration",
    "NATU": "naturalization",
    "PROB": "probate",
}


def _event_label(element) -> str:
    """Return a human-readable event name for the parent of a DATE element, or ''."""
    parent = element.get_parent_element()
    if parent is None:
        return ""
    return _EVENT_LABELS.get(parent.get_tag(), "")


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
    Works around two library bugs:
    1. to_gedcom_string(recursive=True) only goes one level deep.
    2. get_pointer() can return None instead of "" for synthetic elements,
       causing a TypeError in to_gedcom_string — we build the line manually."""
    level = element.get_level()
    if level < 0:
        result = ""
    else:
        pointer = element.get_pointer() or ""
        tag = element.get_tag()
        value = element.get_value()
        line = str(level)
        if pointer:
            line += " " + pointer
        line += " " + tag
        if value:
            line += " " + value
        line += "\n"
        result = line
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
    # English
    "january": "JAN",
    "february": "FEB",
    "march": "MAR",
    "april": "APR",
    "may": "MAY",
    "june": "JUN",
    "july": "JUL",
    "august": "AUG",
    "september": "SEP",
    "october": "OCT",
    "november": "NOV",
    "december": "DEC",
    # German
    "januar": "JAN",
    "februar": "FEB",
    "märz": "MAR",
    "maerz": "MAR",
    "mai": "MAY",
    "juni": "JUN",
    "juli": "JUL",
    "oktober": "OCT",
    "dezember": "DEC",
    # Slovenian nominative
    "januar": "JAN",
    "februar": "FEB",
    "marec": "MAR",
    "april": "APR",
    "maj": "MAY",
    "junij": "JUN",
    "julij": "JUL",
    "avgust": "AUG",
    "september": "SEP",
    "oktober": "OCT",
    "november": "NOV",
    "december": "DEC",
    # Slovenian genitive (used in dates: "30. aprila 1998")
    "januarja": "JAN",
    "februarja": "FEB",
    "marca": "MAR",
    "aprila": "APR",
    "maja": "MAY",
    "junija": "JUN",
    "julija": "JUL",
    "avgusta": "AUG",
    "septembra": "SEP",
    "oktobra": "OCT",
    "novembra": "NOV",
    "decembra": "DEC",
    # Slovenian locative/dative (used after "po": "po maju 1875")
    "januarju": "JAN",
    "februarju": "FEB",
    "marcu": "MAR",
    "aprilu": "APR",
    "maju": "MAY",
    "juniju": "JUN",
    "juliju": "JUL",
    "avgustu": "AUG",
    "septembru": "SEP",
    "oktobru": "OCT",
    "novembru": "NOV",
    "decembru": "DEC",
}

MONTHS_SHORT = {
    # English / GEDCOM canonical
    "jan": "JAN",
    "feb": "FEB",
    "mar": "MAR",
    "apr": "APR",
    "may": "MAY",
    "jun": "JUN",
    "jul": "JUL",
    "aug": "AUG",
    "sep": "SEP",
    "oct": "OCT",
    "nov": "NOV",
    "dec": "DEC",
    "sept": "SEP",
    # German short forms
    "jan.": "JAN",
    "feb.": "FEB",
    "mär": "MAR",
    "mär.": "MAR",
    "mrz": "MAR",
    "mrz.": "MAR",
    "apr.": "APR",
    "mai.": "MAY",
    "jun.": "JUN",
    "jul.": "JUL",
    "aug.": "AUG",
    "sep.": "SEP",
    "okt": "OCT",
    "okt.": "OCT",
    "nov.": "NOV",
    "dez": "DEC",
    "dez.": "DEC",
    # Slovenian short forms
    "jan.": "JAN",
    "feb.": "FEB",
    "febr.": "FEB",
    "febr": "FEB",
    "mar.": "MAR",
    "apr.": "APR",
    "maj.": "MAY",
    "jun.": "JUN",
    "jul.": "JUL",
    "avg": "AUG",
    "avg.": "AUG",
    "sep.": "SEP",
    "okt.": "OCT",
    "nov.": "NOV",
    "dec.": "DEC",
}

# Map all known prefix variants to their canonical GEDCOM form
PREFIX_MAP = {
    "about": "ABT",
    "abt.": "ABT",
    "abt": "ABT",
    "~": "ABT",
    "<": "BEF",
    ">": "AFT",
    "before": "BEF",
    "bef.": "BEF",
    "bef": "BEF",
    "after": "AFT",
    "aft.": "AFT",
    "aft": "AFT",
    "circa": "ABT",
    "cal.": "CAL",
    "cal": "CAL",
    "cca.": "ABT",
    "cca": "ABT",
    "okoli": "ABT",
    "okrog": "ABT",
    "približno": "ABT",
    "priblizno": "ABT",   # without diacritic
    "priblixno": "ABT",   # legacy encoding mangling of približno (ž → x)

    "okr.": "ABT",
    "okr": "ABT",
    "ok.": "ABT",
    "ok": "ABT",
    "ca.": "ABT",
    "ca": "ABT",
    "pred": "BEF",
    "vor": "BEF",
    "po": "AFT",
    "ˇ": "ABT",  # modifier letter caron (U+02C7) used as ABT in some apps
    "l.": "",   # Slovenian/German "Leto/Jahr" (year) — strip prefix, keep year
    "l": "",
    "est.": "EST",
    "est": "EST",
}

# Regex pieces
_DAY = r"(?P<day>\d{1,2})"
_MONTH = r"(?P<month>[A-Za-zÄäÖöÜüß]+\.?)"
_YEAR = r"(?P<year>\d{3,4})"

# Flexible separator: one or more of space, dot, comma, slash, hyphen, colon, tilde
_SEP = r"[\s.,/\-:~]+"

# Full date patterns (most specific first)
DATE_PATTERNS = [
    # DD MMM YYYY  — any mix of separators/spaces between tokens
    re.compile(rf"^{_DAY}{_SEP}{_MONTH}{_SEP}{_YEAR}$"),
    # DD MMMYYYY  — separator before month, none between month and year (e.g. "18.FEB1732")
    re.compile(rf"^{_DAY}{_SEP}{_MONTH}(?P<year>\d{{3,4}})$"),
    # DDMMM YYYY  — no separator before month, separator before year (e.g. "11FEB.1694")
    re.compile(rf"^{_DAY}{_MONTH}{_SEP}{_YEAR}$"),
    # MMM DD YYYY  (e.g. "Jan 15 1900")
    re.compile(rf"^{_MONTH}{_SEP}{_DAY}{_SEP}{_YEAR}$"),
    # YYYY-MM-DD  (ISO — must come before generic numeric to avoid wrong group assignment)
    re.compile(r"^(?P<year>\d{4})-(?P<monthnum>\d{1,2})-(?P<day>\d{1,2})$"),
    # DD MM YYYY  — numeric month, any mix of separators (including mixed like "31 05.1756")
    re.compile(rf"^(?P<day>\d{{1,2}}){_SEP}(?P<monthnum>\d{{1,2}}){_SEP}(?P<year>\d{{3,4}})$"),
    # DD.MMYYYY  — separator after day, no separator between 2-digit month and 4-digit year
    re.compile(r"^(?P<day>\d{1,2})[.,/\-:](?P<monthnum>\d{2})(?P<year>\d{4})$"),
    # DDMM.YYYY or DDMM YYYY  — no separator between day and month, separator before year
    re.compile(r"^(?P<day>\d{2})(?P<monthnum>\d{2})[.,/\-:\s](?P<year>\d{4})$"),
    # .MM.YYYY / .MM-YYYY / .MM YYYY  (unknown day, numeric month, any separator)
    re.compile(rf"^[.,]\s*(?P<monthnum>\d{{1,2}}){_SEP}(?P<year>\d{{3,4}})$"),
    # .MMYYYY  (unknown day, numeric month, no separator — e.g. ".051948")
    re.compile(r"^[.,]\s*(?P<monthnum>\d{2})(?P<year>\d{4})$"),
    # .MMM.YYYY  (unknown day, named month — leading dot placeholder, e.g. ".MAJ.1693")
    re.compile(rf"^\.\s*{_MONTH}{_SEP}{_YEAR}$"),
    # MM YYYY  (numeric month, no day — e.g. "04 1883")
    re.compile(rf"^(?P<monthnum>\d{{1,2}}){_SEP}(?P<year>\d{{3,4}})$"),
    # MMM YYYY  (no day, with separator)
    re.compile(rf"^{_MONTH}{_SEP}{_YEAR}$"),
    # MMMYYYY  (no day, no separator — e.g. "NOV1839")
    re.compile(rf"^{_MONTH}(?P<year>\d{{3,4}})$"),
    # .YYYY  (unknown day and month — leading dot placeholder, year only)
    re.compile(r"^\.\s*(?P<year>\d{3,4})$"),
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
    abbrs = [
        "JAN",
        "FEB",
        "MAR",
        "APR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
    ]
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

        year = gd.get("year")
        day = gd.get("day")
        month = None

        if "monthnum" in gd and gd["monthnum"]:
            month = _monthnum_to_abbr(gd["monthnum"])
            if month is None:
                # month number > 12: try swapping day and month (MM.DD.YYYY → DD.MM.YYYY)
                if day and _monthnum_to_abbr(day) and int(gd["monthnum"]) <= 31:
                    month = _monthnum_to_abbr(day)
                    day = gd["monthnum"]
                else:
                    return None, f"invalid month number in '{value}'"
        elif "month" in gd and gd["month"]:
            month = _normalize_month_name(gd["month"])
            if month is None:
                return None, f"unrecognised month '{gd['month']}' in '{value}'"

        # Three-digit years are assumed to be missing a leading '1' (e.g. 994 → 1994)
        if year and len(year) == 3:
            year = "1" + year

        parts = []
        if day:
            parts.append(str(int(day)))  # strip leading zero
        if month:
            parts.append(month)
        if year:
            parts.append(year)

        return " ".join(parts), None

    return None, f"unrecognised date format '{value}'"


_PLACEHOLDER_RE = re.compile(r"_+|[-]{2,}|<>|(?<!\d)<(?!\d{3,4})")  # _ / __ or -- or <> or bare < (not BEF prefix)


def _handle_placeholder(value: str) -> tuple[str, None] | None:
    """
    Handle dates that use __ / ____ as placeholders for unknown day/month/year.
    Returns ("", None)   if the date is fully unknown  → caller should remove the element.
    Returns (year, None) if only the year is known     → keep just the year.
    Returns None         if the value has no placeholders at all.
    """
    # Single dot alone = fully unknown (e.g. "--.--" collapses to ".")
    if re.match(r"^\.$", value):
        return "", None

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

    # FROM date TO date  (TO / DO)
    m = re.match(r"^FROM\s+(.+?)\s+(?:TO|DO)\s+(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "FROM {} TO {}")

    # date DO/TO date  (no FROM, e.g. "1920 DO 1945")
    m = re.match(r"^(.+?)\s+(?:TO|DO)\s+(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "FROM {} TO {}")

    # FROM date  (open-ended)
    m = re.match(r"^FROM\s+(.+)$", v, re.IGNORECASE)
    if m:
        return one(m.group(1), "FROM {}")

    # TO date  (open-ended)
    m = re.match(r"^(?:TO|DO)\s+(.+)$", v, re.IGNORECASE)
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
        return "", None  # silently keep empty date as-is

    # Pure ? / ?? — unknown date, treat as empty
    if re.match(r"^\?+$", v):
        return "", None

    # Strip trailing dot or apostrophe (e.g. "12.09.1945.", "06.11.1920'")
    v = v.rstrip(".'`")

    # Trailing ? or " ??" etc. — uncertain date; strip question marks, keep value as ABT
    uncertain = False
    v_stripped = v.rstrip("?").rstrip()
    if v_stripped != v and v_stripped:
        v = v_stripped
        uncertain = True

    # Strip parentheses (e.g. "(1620)", ".-.(1740)")
    v = re.sub(r"[()]", "", v).strip()

    # Collapse multiple leading dots/spaces/hyphens to a single dot (e.g. "..1920", ".-. 1740")
    v = re.sub(r"^[.\s\-]{2,}", ".", v)

    # Strip single leading comma/dot used as unknown-day placeholder (e.g. ",MAJ 1945", ".MAJ 1945")
    if len(v) > 1 and v[0] in ".,":
        rest = v[1:].lstrip()
        # Only strip if what follows is not purely numeric (that would be .MM.YYYY — handled separately)
        if rest and not rest[0].isdigit():
            v = rest

    # Strip leading = (means "exact date" in some apps — no GEDCOM equivalent, keep value)
    if v.startswith("="):
        v = v[1:].strip()

    # Normalize letter O → digit 0 (OCR/typo): at word boundary before digit, or between digits
    v = re.sub(r"\bO(?=\d)|(?<=\d)O(?=\d)", "0", v)

    # Collapse repeated tilde to single (e.g. "~~ 1968" → "~ 1968")
    v = re.sub(r"~+", "~", v)

    # Bare "/" is unknown — treat as empty
    if v == "/":
        return "", None

    # Handle __ placeholder dates first
    placeholder = _handle_placeholder(v)
    if placeholder is not None:
        return placeholder  # ("", None) = remove  |  (year, None) = keep year only

    # YYYY-YYYY year ranges are kept as-is (no conversion to FROM/TO), spaces around hyphen allowed
    m = re.match(r"^(\d{3,4})\s*-\s*(\d{3,4})$", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}", None

    # YYYY/Y … YYYY/YYYY — dual dating / alternative year notation, kept as-is
    if re.match(r"^\d{3,4}/\d{1,4}$", v):
        return v, None

    # Try range patterns first (before prefix handling)
    result, err, is_range = _parse_range(v)
    if is_range:
        return result, err

    # Detect and strip prefix
    prefix_canon = None
    for variant, canon in PREFIX_MAP.items():
        # match whole word / token at start, case-insensitive
        pattern = re.compile(r"^" + re.escape(variant) + r"(?=[\s\d\w.]|$)", re.IGNORECASE)
        if pattern.match(v):
            prefix_canon = canon
            v = v[len(variant) :].strip()
            break

    if not v:
        return None, f"date consists only of a prefix: '{raw}'"

    formatted, err = _parse_date_value(v)
    if err:
        return None, err

    if uncertain and not prefix_canon:
        prefix_canon = "ABT"
    result = f"{prefix_canon} {formatted}" if prefix_canon else formatted
    return result, None


# ---------------------------------------------------------------------------
# Cleaner: name_placeholder
# ---------------------------------------------------------------------------

# Matches values that are entirely placeholder characters (_, ?, /) plus whitespace
_NAME_PLACEHOLDER_RE = re.compile(r"^[_?\s/]+$")


def clean_name_placeholder(raw: str) -> tuple[str, None]:
    """
    Returns ("", None) if the name is a placeholder (all underscores or question marks).
    Returns (raw, None) otherwise — no change.
    """
    if _NAME_PLACEHOLDER_RE.match(raw):
        return "", None
    return raw, None


# ---------------------------------------------------------------------------
# Cleaner: place_placeholder
# ---------------------------------------------------------------------------

# Matches place values that are entirely placeholder characters (_, ?, commas) plus whitespace
_PLACE_PLACEHOLDER_RE = re.compile(r"^[_?,\s]+$")


def clean_place_placeholder(raw: str) -> tuple[str, None]:
    """
    Returns ("", None) if the place is a placeholder (all underscores, question marks,
    or comma-separated empty segments like '___, ___, ___').
    Returns (raw, None) otherwise — no change.
    """
    if _PLACE_PLACEHOLDER_RE.match(raw):
        return "", None
    return raw, None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CLEANERS = {
    "dd_mmm_yyyy": clean_date_dd_mmm_yyyy,
    "name_placeholder": clean_name_placeholder,
    "place_placeholder": clean_place_placeholder,
}


# ---------------------------------------------------------------------------
# Strippers
# ---------------------------------------------------------------------------


@dataclass
class StripSpec:
    tags: set[str]
    parent_tag: str | None = (
        None  # None = level-0 records; str = children of that parent tag
    )


STRIPPERS: dict[str, StripSpec] = {
    "ste": StripSpec(tags={"_STE"}),  # MacFamilyTree source-template entries (level-0)
    "stf": StripSpec(tags={"_STF"}),  # MacFamilyTree source-template fields (level-0)
    "addr_longlati": StripSpec(
        tags={"LATI", "LONG", "MAP"}, parent_tag="ADDR"
    ),  # coords on ADDR unsupported by webtrees (direct or via MAP)
}


# ---------------------------------------------------------------------------
# Transformers
# ---------------------------------------------------------------------------


@dataclass
class TagTransform:
    """Describes a structural tag transformation."""

    rename: str  # new tag name
    add_children: list[tuple[str, str]] = field(
        default_factory=list
    )  # (tag, value) prepended to children


# Each transformer maps source tag → str (simple rename) or TagTransform (rename + add children).
# Note: python-gedcom has no set_tag(); we write to the private _Element__tag
# attribute via name mangling — this is a deliberate workaround.
TRANSFORMERS: dict[str, dict[str, str | TagTransform]] = {
    "fid_fsftid": {"_FID": "_FSFTID"},
    "latr_even": {
        "LATR": TagTransform(rename="EVEN", add_children=[("TYPE", "Land Transaction")])
    },
}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, dict[str, list[str]]] = {
    "mft_webtrees": {
        "clean": ["dd_mmm_yyyy", "name_placeholder"],
        "strip": ["ste", "stf", "addr_longlati"],
        "transform": ["fid_fsftid", "latr_even"],
    },
    "srd_index_cleanup": {
        "clean": ["dd_mmm_yyyy", "name_placeholder", "place_placeholder"],
        "strip": [],
        "transform": [],
    },
}


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


@dataclass
class CleanerStats:
    processed: int = 0
    fixed: int = 0
    warn: int = 0


@dataclass
class StripperStats:
    processed: int = 0
    removed: int = 0


@dataclass
class TransformerStats:
    processed: int = 0
    transformed: int = 0


def process_file(
    input_path: str,
    output_path: str,
    cleaners: list[str],
    strippers: list[str],
    transformers: list[str],
    warn: bool,
    verbose: bool = False,
) -> tuple[
    dict[str, CleanerStats], dict[str, StripperStats], dict[str, TransformerStats]
]:
    """Returns (per-cleaner stats, per-stripper stats, per-transformer stats)."""
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

    stats: dict[str, CleanerStats] = {c: CleanerStats() for c in cleaners}
    strip_stats: dict[str, StripperStats] = {s: StripperStats() for s in strippers}
    transform_stats: dict[str, TransformerStats] = {
        t: TransformerStats() for t in transformers
    }

    if "dd_mmm_yyyy" in cleaners:
        s = stats["dd_mmm_yyyy"]
        cleaner_fn = CLEANERS["dd_mmm_yyyy"]
        current_label = None
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_DATE:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, warning = cleaner_fn(raw)
            if warning:
                s.warn += 1
                if warn:
                    event = _event_label(element)
                    event_str = f" ({event})" if event else ""
                    print(
                        f"WARN [dd_mmm_yyyy]: {warning}{event_str}  — {_record_label(element)}",
                        file=sys.stderr,
                    )
            else:
                if cleaned == "":
                    s.fixed += 1
                    if verbose:
                        label = _record_label(element)
                        if label != current_label:
                            print(label)
                            current_label = label
                        print(f"  [dd_mmm_yyyy] {raw!r} -> (removed)")
                    element.get_parent_element().get_child_elements().remove(element)
                elif cleaned != raw:
                    s.fixed += 1
                    if verbose:
                        label = _record_label(element)
                        if label != current_label:
                            print(label)
                            current_label = label
                        print(f"  [dd_mmm_yyyy] {raw!r} -> {cleaned!r}")
                    element.set_value(cleaned)

    if "name_placeholder" in cleaners:
        s = stats["name_placeholder"]
        current_label = None
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_NAME:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, _ = clean_name_placeholder(raw)
            if cleaned == "" and raw != "":
                s.fixed += 1
                if verbose:
                    label = _record_label(element)
                    if label != current_label:
                        print(label)
                        current_label = label
                    print(f"  [name_placeholder] {raw!r} -> (cleared)")
                element.set_value("")

    if "place_placeholder" in cleaners:
        s = stats["place_placeholder"]
        current_label = None
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_PLACE:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, _ = clean_place_placeholder(raw)
            if cleaned == "" and raw != "":
                s.fixed += 1
                if verbose:
                    label = _record_label(element)
                    if label != current_label:
                        print(label)
                        current_label = label
                    print(f"  [place_placeholder] {raw!r} -> (cleared)")
                element.set_value("")

    for name in strippers:
        ss = strip_stats[name]
        spec = STRIPPERS[name]
        if spec.parent_tag is None:
            candidates = parser.get_root_child_elements()
            ss.processed = len(candidates)
            to_remove = [el for el in candidates if el.get_tag() in spec.tags]
            for element in to_remove:
                ss.removed += 1
                if verbose:
                    print(
                        f"  [strip:{name}] removing {element.get_tag()} {element.get_pointer()}"
                    )
                candidates.remove(element)
        else:
            all_elements = parser.get_element_list()
            to_remove = [
                el
                for el in all_elements
                if el.get_tag() in spec.tags
                and el.get_parent_element() is not None
                and el.get_parent_element().get_tag() == spec.parent_tag
            ]
            ss.processed = len(to_remove)
            for element in to_remove:
                ss.removed += 1
                if verbose:
                    label = _record_label(element)
                    print(
                        f"  [strip:{name}] removing {element.get_tag()} under {spec.parent_tag}  — {label}"
                    )
                element.get_parent_element().get_child_elements().remove(element)

    for name in transformers:
        ts = transform_stats[name]
        tag_map = TRANSFORMERS[name]
        current_label = None
        for element in parser.get_element_list():
            old_tag = element.get_tag()
            if old_tag not in tag_map:
                continue
            ts.processed += 1
            spec = tag_map[old_tag]
            if isinstance(spec, TagTransform):
                new_tag = spec.rename
                element._Element__tag = new_tag
                # Prepend each add_children entry before existing children
                for i, (child_tag, child_value) in enumerate(spec.add_children):
                    child = Element(
                        element.get_level() + 1,
                        "",
                        child_tag,
                        child_value,
                        "\n",
                        multi_line=False,
                    )
                    child.set_parent_element(element)
                    element.get_child_elements().insert(i, child)
            else:
                new_tag = spec
                element._Element__tag = new_tag
            ts.transformed += 1
            if verbose:
                label = _record_label(element)
                if label != current_label:
                    print(label)
                    current_label = label
                print(
                    f"  [transform:{name}] {old_tag} -> {new_tag}  {element.get_value()!r}"
                )

    parser.invalidate_cache()
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            for element in parser.get_root_child_elements():
                f.write(_serialize(element))
    except OSError as e:
        print(f"ERROR: could not write '{output_path}': {e}", file=sys.stderr)
        sys.exit(1)

    return stats, strip_stats, transform_stats


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
    parser.add_argument("input", help="Input GEDCOM file (.ged)")
    parser.add_argument("output", help="Output GEDCOM file (.ged)")
    parser.add_argument(
        "--clean",
        default="",
        metavar="CLEANER[,CLEANER,...]",
        help=f"Comma-separated list of cleaners to apply. Available: {', '.join(CLEANERS)}",
    )
    parser.add_argument(
        "--strip",
        default="",
        metavar="STRIPPER[,STRIPPER,...]",
        help=f"Comma-separated list of strippers to apply. Available: {', '.join(STRIPPERS)}",
    )
    parser.add_argument(
        "--transform",
        default="",
        metavar="TRANSFORMER[,TRANSFORMER,...]",
        help=f"Comma-separated list of transformers to apply. Available: {', '.join(TRANSFORMERS)}",
    )
    parser.add_argument(
        "--preset",
        default="",
        metavar="PRESET",
        help=f"Apply a predefined combination of processors. Available: {', '.join(PRESETS)}",
    )
    parser.add_argument(
        "--warn",
        action="store_true",
        help="Print dates that could not be converted to stderr",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every conversion performed",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print per-cleaner statistics at the end",
    )

    args = parser.parse_args()

    requested_clean = [c.strip() for c in args.clean.split(",") if c.strip()]
    requested_strip = [s.strip() for s in args.strip.split(",") if s.strip()]
    requested_transform = [t.strip() for t in args.transform.split(",") if t.strip()]

    if args.preset:
        if args.preset not in PRESETS:
            print(
                f"ERROR: unknown preset '{args.preset}'. "
                f"Available: {', '.join(PRESETS)}",
                file=sys.stderr,
            )
            sys.exit(1)
        p = PRESETS[args.preset]

        # merge preset entries with any explicitly requested ones (dedup, preserve order)
        def _merge(base: list[str], extra: list[str]) -> list[str]:
            seen = set(base)
            return base + [x for x in extra if x not in seen]

        requested_clean = _merge(p.get("clean", []), requested_clean)
        requested_strip = _merge(p.get("strip", []), requested_strip)
        requested_transform = _merge(p.get("transform", []), requested_transform)

    if not requested_clean and not requested_strip and not requested_transform:
        print(
            "ERROR: at least one of --clean, --strip, or --transform must be specified.",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown_clean = [c for c in requested_clean if c not in CLEANERS]
    if unknown_clean:
        print(
            f"ERROR: unknown cleaner(s): {', '.join(unknown_clean)}. "
            f"Available: {', '.join(CLEANERS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown_strip = [s for s in requested_strip if s not in STRIPPERS]
    if unknown_strip:
        print(
            f"ERROR: unknown stripper(s): {', '.join(unknown_strip)}. "
            f"Available: {', '.join(STRIPPERS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown_transform = [t for t in requested_transform if t not in TRANSFORMERS]
    if unknown_transform:
        print(
            f"ERROR: unknown transformer(s): {', '.join(unknown_transform)}. "
            f"Available: {', '.join(TRANSFORMERS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.preset:
        print(f"Preset:       {args.preset}")
    if requested_clean:
        print(f"Cleaners:     {', '.join(requested_clean)}")
    if requested_strip:
        print(f"Strippers:    {', '.join(requested_strip)}")
    if requested_transform:
        print(f"Transformers: {', '.join(requested_transform)}")
    print(f"Input:        {args.input}")
    print(f"Output:       {args.output}")
    print()

    stats, strip_stats, transform_stats = process_file(
        args.input,
        args.output,
        requested_clean,
        requested_strip,
        requested_transform,
        args.warn,
        args.verbose,
    )

    total_warn = sum(s.warn for s in stats.values())
    if total_warn:
        note = " (use --warn to see details)" if not args.warn else ""
        print(f"{total_warn} value(s) could not be converted{note}.", file=sys.stderr)

    print(f"Saved: {args.output}")

    if args.stats:
        rows = []
        for name, s in stats.items():
            rows.append(("cleaner", name, str(s.processed), str(s.fixed), str(s.warn)))
        for name, s in strip_stats.items():
            rows.append(("stripper", name, str(s.processed), str(s.removed), "-"))
        for name, s in transform_stats.items():
            rows.append(
                ("transformer", name, str(s.processed), str(s.transformed), "-")
            )

        if rows:
            headers = ("type", "name", "processed", "changed", "warn")
            widths = [
                max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)
            ]
            fmt = "  ".join(
                f"{{:<{w}}}" if i < 2 else f"{{:>{w}}}" for i, w in enumerate(widths)
            )
            print()
            print(fmt.format(*headers))
            print("-" * (sum(widths) + 2 * (len(widths) - 1)))
            for row in rows:
                print(fmt.format(*row))


if __name__ == "__main__":
    main()
