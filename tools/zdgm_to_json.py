"""Convert ZDGM seznam-borcev.xlsx to ZDGM-persons.json.

Reads data/zdgm/seznam-borcev.xlsx (List of fighters for the northern Slovenian
border 1918/19 by mag. Radovan Pulko) and emits:

    data/output/ZDGM-persons.json

Columns mapped to JSON fields:
    Col 2  priimek in ime           → surname, name, alt_surname (if in parens)
    Col 3  podatki o rojstvu in smrti → birth.date, birth.place, death.date,
                                        death.place (parsed from Slovenian notation)
    Col 4  kraj ali občina bivanja  → note (Kraj)
    Col 5  poklic                   → note (Poklic)
    Col 6  polk, bataljon ...       → note (Polk)
    Col 7  čin ali položaj          → note (Čin)
    Col 8  čas služenja             → note (Služenje)
    Col 9  odlikovanja              → note (Odlikovanje)

Name prefix/titles (dr., mgr., l.) and father's initials (J., V.) extracted
from col 2 are appended to notes as "Naziv" and "Inicialka" respectively.
"""

import argparse
import hashlib
import json
import locale
import os
import re
import sys
import unicodedata
from datetime import datetime

import openpyxl

INPUT_FILE = "data/zdgm/seznam-borcev.xlsx"
OUTPUT_FILE = "data/output/Borci-Maister-military-persons.json"
OUTPUT_DIR = "data/output"
CONTRIBUTORS_FILE = "data/contributors.json"

CONTRIBUTOR = "Borci-Maister-military"
SOURCE_URL = "https://www.zvezadgm.si/seznam-borcev/"

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

DATA_START_ROW = 8

COL_NAME = 2
COL_BIRTH_DEATH = 3
COL_PLACE = 4
COL_OCCUPATION = 5
COL_UNIT = 6
COL_RANK = 7
COL_SERVICE = 8
COL_DECORATIONS = 9

# Separates birth info from death info in col 3
BIRTH_DEATH_SEP_RE = re.compile(r'[―–]|-(?=[^>])')

# A Slovenian date component: day.month.year with optional ? for unknown parts
SL_FULL_DATE_RE = re.compile(
    r'(?P<d>\?|\d{1,2})\.\s*(?P<m>\?|\d{1,2})\.\s*(?P<y>\d{4})'
)
SL_YEAR_RE = re.compile(r'(?<!\d)(?P<y>\d{4})(?!\d)')

# Token matchers for name parsing
_TITLE_RE = re.compile(r'^[a-zčšžćđ]+\.$')         # dr., mgr., inž., l., …
_INITIAL_RE = re.compile(r'^[A-ZČŠŽĆĐ]\.$')        # J., V., M., …


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell(value):
    if value is None:
        return ""
    return str(value).strip()


def _nfc(s):
    return unicodedata.normalize("NFC", s)


def _make_id(row_num, surname, name, birth_date, birth_place):
    key = "\x1f".join([
        str(row_num), surname or "", name or "", birth_date or "", birth_place or "",
    ])
    return "zdgm-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Name parsing
# ---------------------------------------------------------------------------

def parse_name(raw):
    """Parse 'Surname [titles/initials] Firstname [(AltFirstname)]'
    or 'Surname [(AltSurname)] Firstname'.

    Returns (surname, name, alt_surname, titles, initials).
    - surname: family name
    - name: given name, with alternative in parens if present, e.g. 'Janez (Ivan)'
    - alt_surname: alternative spelling of surname, e.g. 'Albrecht' from '(Albrecht)'
    - titles: space-joined title abbreviations found, e.g. 'dr. mgr.'
    - initials: space-joined father's initials found, e.g. 'J.'
    """
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return "", "", "", "", ""

    tokens = s.split(" ")
    surname = tokens[0]
    alt_surname = ""
    titles_list = []
    initials_list = []

    i = 1

    # Check for alt_surname in parens right after the surname and before firstname.
    # Detected when a paren group is followed by at least one more token.
    if i < len(tokens) and tokens[i].startswith("("):
        # Collect the full paren span (may be multi-token).
        j = i
        paren = ""
        while j < len(tokens):
            paren += (" " if paren else "") + tokens[j]
            j += 1
            if tokens[j - 1].endswith(")"):
                break
        if j < len(tokens):
            # Tokens remain after the paren → it's an alt_surname.
            alt_surname = paren.strip("() ")
            i = j

    # Collect title abbreviations and father's initials between surname and firstname.
    while i < len(tokens):
        t = tokens[i]
        if _TITLE_RE.match(t):
            titles_list.append(t)
            i += 1
        elif _INITIAL_RE.match(t):
            initials_list.append(t)
            i += 1
        else:
            break

    # Remaining tokens form the given name.
    name = " ".join(tokens[i:]).strip()

    titles = " ".join(titles_list)
    initials = " ".join(initials_list)
    return surname, name, alt_surname, titles, initials


# ---------------------------------------------------------------------------
# Date / birth-death parsing
# ---------------------------------------------------------------------------

def _sl_date_to_gedcom(s):
    """Convert a raw Slovenian date string to GEDCOM notation.

    Accepts: 'd. m. yyyy', '?. m. yyyy', '?. ?. yyyy', 'yyyy', '?'.
    Returns '' for unknown/unparseable.
    """
    s = s.strip() if s else ""
    if not s or s == "?":
        return ""
    m = SL_FULL_DATE_RE.fullmatch(s.strip())
    if m:
        d_s, mo_s, y_s = m.group("d"), m.group("m"), m.group("y")
        y = int(y_s)
        if d_s != "?" and mo_s != "?":
            d, mo = int(d_s), int(mo_s)
            if 1 <= mo <= 12:
                return f"{d} {MONTHS[mo - 1]} {y}"
        if mo_s != "?":
            mo = int(mo_s)
            if 1 <= mo <= 12:
                return f"{MONTHS[mo - 1]} {y}"
        return str(y)
    m = SL_YEAR_RE.fullmatch(s.strip())
    if m:
        return m.group("y")
    return ""


def _extract_trailing_date(s):
    """Find the last date-like token at the end of s.

    Returns (gedcom_date, place_prefix) where place_prefix is the part before
    the date (with trailing comma/space stripped).
    """
    s = s.strip()
    if not s:
        return "", ""

    # Try full date at end: d. m. yyyy (with ? variants)
    m = re.search(
        r"((?:\?|\d{1,2})\.\s*(?:\?|\d{1,2})\.\s*\d{4})\s*$", s
    )
    if m:
        date_raw = m.group(1)
        place = s[: m.start()].rstrip(", ").strip()
        return _sl_date_to_gedcom(date_raw), place

    # Try year-only at end
    m = re.search(r"(?<!\d)(\d{4})\s*$", s)
    if m:
        date_raw = m.group(1)
        place = s[: m.start()].rstrip(", ").strip()
        return _sl_date_to_gedcom(date_raw), place

    # '?' alone or trailing
    if re.fullmatch(r"\?[\s]*", s):
        return "", ""

    return "", s


def _extract_leading_date(s):
    """Find the first date-like token at the start of s.

    Returns (gedcom_date, place_suffix) where place_suffix is the part after
    the date (with leading comma/space stripped).
    """
    s = s.strip()
    if not s:
        return "", ""

    # Try full date at start
    m = re.match(
        r"^((?:\?|\d{1,2})\.\s*(?:\?|\d{1,2})\.\s*\d{4})", s
    )
    if m:
        date_raw = m.group(1)
        rest = s[m.end():].lstrip(", ").strip()
        return _sl_date_to_gedcom(date_raw), rest

    # Try year-only at start
    m = re.match(r"^(\d{4})(?!\d)", s)
    if m:
        date_raw = m.group(1)
        rest = s[m.end():].lstrip(", ").strip()
        return _sl_date_to_gedcom(date_raw), rest

    # Starts with '?' (unknown date)
    if s.startswith("?"):
        rest = s[1:].lstrip(" ").strip()
        if rest.startswith(","):
            rest = rest[1:].strip()
        # Strip parenthesized annotations like '(žrtev 2. svetovne vojne)'
        rest = re.sub(r"^\([^)]*\)", "", rest).strip()
        return "", rest

    return "", s


def parse_birth_death(raw):
    """Parse col 3 string into (birth_date, birth_place, death_date, death_place).

    The birth half uses format '[place,] date' (place first, date last).
    The death half uses format 'date[, place]' (date first, place after).
    If the separator (―/–/-) is absent, the entire string is treated as birth.
    """
    if not raw:
        return "", "", "", ""

    # Split on em-dash (U+2015), en-dash (U+2013), or hyphen-minus used as separator.
    # Hyphen-minus is used as separator in ~41 rows but must not split d.m-yyyy parts;
    # since dates use dots not hyphens, any '-' in col 3 is a birth-death separator.
    parts = re.split(r"[―–]|-", raw, maxsplit=1)
    birth_raw = parts[0].strip()
    death_raw = parts[1].strip() if len(parts) > 1 else ""

    birth_date, birth_place = _extract_trailing_date(birth_raw)
    death_date, death_place = _extract_leading_date(death_raw)

    return birth_date, birth_place, death_date, death_place


# ---------------------------------------------------------------------------
# Notes assembly
# ---------------------------------------------------------------------------

_NOTE_LABELS = [
    (COL_PLACE, "Kraj"),
    (COL_OCCUPATION, "Poklic"),
    (COL_UNIT, "Polk"),
    (COL_RANK, "Čin"),
    (COL_SERVICE, "Služenje"),
    (COL_DECORATIONS, "Odlikovanje"),
]


def build_notes(row, titles, initials):
    """Assemble a semicolon-separated notes string from remaining fields."""
    parts = []
    for col, label in _NOTE_LABELS:
        v = _cell(row.get(col))
        if v:
            parts.append(f"{label}: {v}")
    if titles:
        parts.append(f"Naziv: {titles}")
    if initials:
        parts.append(f"Inicialka: {initials}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------

def build_record(row_num, row):
    name_raw = _cell(row.get(COL_NAME))
    if not name_raw:
        return None

    surname, name, alt_surname, titles, initials = parse_name(name_raw)

    birth_death_raw = _cell(row.get(COL_BIRTH_DEATH))
    birth_date, birth_place, death_date, death_place = parse_birth_death(birth_death_raw)

    notes = build_notes(row, titles, initials)

    record = {
        "id": _make_id(row_num, surname, name, birth_date, birth_place),
        "name": name,
        "surname": surname,
        "sex": "",
        "birth": {
            "date": birth_date,
            "place": birth_place,
        },
        "death": {
            "date": death_date,
            "place": death_place,
        },
    }

    if alt_surname:
        record["alt_surname"] = alt_surname

    record["links"] = [SOURCE_URL]

    if notes:
        record["notes"] = notes

    return record


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert ZDGM seznam-borcev.xlsx to ZDGM-persons.json."
    )
    parser.add_argument("--input", default=INPUT_FILE,
                        help=f"Path to input xlsx (default: {INPUT_FILE}).")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Path to output JSON (default: {OUTPUT_FILE}).")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: input file '{args.input}' not found.", file=sys.stderr)
        return 1

    print(f"Reading {args.input} …", file=sys.stderr)

    wb = openpyxl.load_workbook(args.input, data_only=True, read_only=True)
    ws = wb.worksheets[0]

    records = []
    skipped = 0

    for row_num, row_cells in enumerate(ws.iter_rows(
        min_row=DATA_START_ROW, values_only=True
    ), start=DATA_START_ROW):
        row = {col: row_cells[col - 1] for col in range(COL_NAME, COL_DECORATIONS + 1)
               if col - 1 < len(row_cells)}
        record = build_record(row_num, row)
        if record is None:
            skipped += 1
            continue
        records.append(record)

    wb.close()

    records.sort(key=lambda r: (
        locale.strxfrm(r.get("surname", "") or ""),
        locale.strxfrm(r.get("name", "") or ""),
        r.get("birth", {}).get("date", "") or "",
    ))

    # NFC-normalise all strings
    def _nfc_obj(obj):
        if isinstance(obj, str):
            return unicodedata.normalize("NFC", obj)
        if isinstance(obj, list):
            return [_nfc_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {_nfc_obj(k): _nfc_obj(v) for k, v in obj.items()}
        return obj

    records = _nfc_obj(records)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=4)

    source_mtime = os.path.getmtime(args.input)
    os.utime(args.output, (source_mtime, source_mtime))

    _update_metadata(args.output, len(records), source_mtime)

    print(
        f"Written {len(records)} records to {args.output} "
        f"({skipped} rows skipped).",
        file=sys.stderr,
    )
    return 0


def _load_contributors():
    try:
        with open(CONTRIBUTORS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {name: {"url": info.get("url"), "intro": info.get("intro")}
                for name, info in data.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _update_metadata(output_path, count, source_mtime):
    meta_path = os.path.join(os.path.dirname(output_path), "metadata.json")
    existing = []
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    info = _load_contributors().get(CONTRIBUTOR, {})
    entry = {
        "contributor": CONTRIBUTOR,
        "persons_count": count,
        "families_count": 0,
        "links_count": count,
        "last_modified": datetime.fromtimestamp(source_mtime).isoformat(),
        "url": info.get("url") or SOURCE_URL,
        "intro": info.get("intro"),
    }

    preserved = [e for e in existing if e.get("contributor") != CONTRIBUTOR]
    combined = preserved + [entry]
    try:
        locale.setlocale(locale.LC_COLLATE, ("sl_SI", "UTF-8"))
    except locale.Error:
        locale.setlocale(locale.LC_COLLATE, "")
    combined.sort(key=lambda x: locale.strxfrm(x.get("contributor", "")))

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=4)


if __name__ == "__main__":
    sys.exit(main())
