"""Convert Matricula Online index spreadsheets (.xlsx) to JSON.

Reads parish index books exported as Excel from data/matricula/<contributor>/
and emits one pair of JSON files per contributor that match the schema used by
gedcom_to_json.py:

    data/output/<contributor>-matricula-persons.json
    data/output/<contributor>-matricula-families.json

Birth books are recognised by " K " in the filename, marriage books by " P ".
"""

import argparse
import hashlib
import json
import locale
import os
import re
import sys
import unicodedata
from datetime import date, datetime
from glob import glob

import openpyxl

INPUT_ROOT = "data/matricula"
OUTPUT_DIR = "data/output"
CONTRIBUTORS_FILE = "data/contributors.json"

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def normalize_header(s):
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


# Map normalized header -> canonical field name.
HEADER_MAP = {
    "zp. st.": "seq",
    "zupnija": "parish",
    "datum rojstva": "birth_date",
    "datum krsta": "baptism_date",
    "datum poroke": "marriage_date",
    "naslov": "address",
    "ime otroka": "child_name",
    "ime oceta": "father_name",
    "priimek oceta": "father_surname",
    "alt. priimek oceta": "father_alt_surname",
    "ime matere": "mother_name",
    "priimek matere": "mother_surname",
    "alt. priimek matere": "mother_alt_surname",
    "ime zenina": "groom_name",
    "priimek zenina": "groom_surname",
    "alt. priimek zenina": "groom_alt_surname",
    "ime neveste": "bride_name",
    "priimek neveste": "bride_surname",
    "alt. priimek neveste": "bride_alt_surname",
    "url naslov": "url",
    "opombe": "notes",
    "interpret": "interpreter",
}


# Matches Slovenian "died" markers followed by a date:
#   "umrl 03.12.1914", "umrla 18.05.1866", "umrl 29.2.1895", "umrla 1863".
# Day/month may be 1 or 2 digits; year-only form is also accepted.
DEATH_RE = re.compile(
    r"\bumrl[ao]?\b\s+"
    r"(?:(?P<d>\d{1,2})\.\s*(?P<m>\d{1,2})\.\s*(?P<y1>\d{4})"
    r"|(?P<y2>\d{4}))",
    re.IGNORECASE,
)


def extract_death_from_notes(notes):
    """Return a GEDCOM death date parsed from the notes, or '' if none."""
    if not notes:
        return ""
    m = DEATH_RE.search(notes)
    if not m:
        return ""
    if m.group("y1"):
        d, mo, y = int(m.group("d")), int(m.group("m")), int(m.group("y1"))
        if not (1 <= mo <= 12):
            return ""
        return f"{d} {MONTHS[mo - 1]} {y}"
    return m.group("y2")


def gedcom_date(value):
    """Convert ISO 'yyyy-mm-dd' / 'yyyy-mm' / 'yyyy' (or datetime) to GEDCOM."""
    if value in (None, ""):
        return ""
    if isinstance(value, (datetime, date)):
        return f"{value.day} {MONTHS[value.month - 1]} {value.year}"
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12:
            return f"{d} {MONTHS[mo - 1]} {y}"
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{MONTHS[mo - 1]} {y}"
    m = re.match(r"^(\d{4})$", s)
    if m:
        return s
    return s


def cell_str(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def _is_blank(value):
    """True for cells that carry no data: None, '', or whitespace-only strings.

    Excel files sometimes have a stray space (' ') filled down an entire column,
    producing tens of thousands of phantom rows. Treating whitespace-only cells
    as blank drops those rows instead of emitting them as records.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def normalize_name_surname(name, surname):
    """Replace Slovenian 'ni znan' (unknown) with 'NN'. If both name and
    surname are unknown, only the name carries 'NN' and surname is cleared.
    """
    name_ni = name.strip().lower() == "ni znan"
    surname_ni = surname.strip().lower() == "ni znan"
    if name_ni and surname_ni:
        return "NN", ""
    if name_ni:
        return "NN", surname
    if surname_ni:
        return name, "NN"
    return name, surname


def clean_paren_name(value):
    """Normalize parenthesized name cells to 'primary (alt1 alt2)' form.

    Source cells use parens to mark alternative or uncertain spellings:
      '(Janez'                    → 'Janez'              (stray opening paren)
      '(Janez Nepomuk)'           → 'Janez Nepomuk'      (single name in parens)
      '(Fere, Flere)'             → 'Fere (Flere)'       (first is primary, rest are alts)
      'Janez (Ivan)'              → 'Janez (Ivan)'       (already canonical)
      'Alojzija Marija (Slavica)' → 'Alojzija Marija (Slavica)'
    """
    s = (value or "").strip()
    if not s:
        return s
    m = re.match(r"^([^()]+?)\s*\((.+)\)\s*$", s)
    if m:
        primary = re.sub(r"\s+", " ", m.group(1).strip())
        alts = re.sub(r"\s*,\s*", " ", m.group(2).strip())
        alts = re.sub(r"\s+", " ", alts).strip()
        return f"{primary} ({alts})" if alts else primary
    m = re.match(r"^\((.+)\)\s*$", s)
    if m:
        parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
        if not parts:
            return ""
        primary = re.sub(r"\s+", " ", parts[0])
        alts = " ".join(parts[1:])
        return f"{primary} ({alts})" if alts else primary
    if s.startswith("("):
        s = s[1:].lstrip()
    if s.endswith(")"):
        s = s[:-1].rstrip()
    return re.sub(r"\s+", " ", s).strip()


def clean_paren_surnames(primary, alt):
    """Drop enclosing parens from a (surname, alt_surname) pair.

    Source data uses parens to mark alternative spellings, sometimes split
    across the two cells, sometimes packed into one:
      'Štibelj (Stibilj)', ''               → 'Štibelj', 'Stibilj'
      '(Belehar', 'Belihar, Bilhar, Bobnar)' → 'Belehar', 'Belihar, Bilhar, Bobnar'
      '(Fere, Flere)', ''                   → 'Fere', 'Flere'
      '(Lah', ''                            → 'Lah', ''
    """
    p = (primary or "").strip()
    a = (alt or "").strip()
    if not a:
        m = re.match(r"^([^()]+?)\s*\((.+)\)\s*$", p)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        m = re.match(r"^\((.+)\)\s*$", p)
        if m:
            parts = [x.strip() for x in m.group(1).split(",")]
            return parts[0], ", ".join(parts[1:])
    if p.startswith("("):
        p = p[1:].lstrip()
    if p.endswith(")"):
        p = p[:-1].rstrip()
    if a.startswith("("):
        a = a[1:].lstrip()
    if a.endswith(")"):
        a = a[:-1].rstrip()
    return p, a


def build_place(row):
    parish = cell_str(row.get("parish"))
    address = cell_str(row.get("address"))
    parts = [p for p in (parish, address) if p]
    return ", ".join(parts)


def detect_book_kind(filename):
    """Return 'K' (births), 'P' (marriages), or None."""
    base = os.path.basename(filename)
    if re.search(r"(?:^|[ _])K(?:[ _])", base):
        return "K"
    if re.search(r"(?:^|[ _])P(?:[ _])", base):
        return "P"
    return None


def read_rows(path):
    """Yield dicts keyed by canonical field name for each data row.

    Reads the first sheet whose header row contains at least three known
    column names. For columns whose header is None but which sit between
    'X name' and 'X surname' (or 'X surname' and the next labelled field),
    we infer alt-name slots positionally — this covers the wide variant
    of K Kranj-Šmartin 1892-1908 where alt-name columns lack headers.
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        for sheet in wb.worksheets:
            rows = sheet.iter_rows(values_only=True)
            try:
                header_row = next(rows)
            except StopIteration:
                continue

            field_for_col = _resolve_columns(header_row)
            if sum(1 for f in field_for_col if f) < 3:
                continue  # not a data sheet (e.g. 'imena' helper sheet)

            for raw in rows:
                if raw is None:
                    continue
                if all(_is_blank(c) for c in raw):
                    continue
                record = {}
                for idx, value in enumerate(raw):
                    if idx >= len(field_for_col):
                        break
                    field = field_for_col[idx]
                    if not field:
                        continue
                    if _is_blank(value):
                        continue
                    record[field] = value
                if record:
                    yield record
            return  # use only the first valid sheet per file
    finally:
        wb.close()


def _resolve_columns(header_row):
    """Map each column index to a canonical field, inferring alt slots."""
    fields = []
    for h in header_row:
        fields.append(HEADER_MAP.get(normalize_header(h), ""))

    # Infer unlabeled columns positionally. The wide K variant places alt-
    # name columns immediately after 'child_name', 'father_surname', and
    # 'mother_surname' with empty headers.
    for i in range(len(fields) - 1):
        if fields[i] == "child_name" and not fields[i + 1]:
            fields[i + 1] = "child_alt_name"
        elif fields[i] == "father_surname" and not fields[i + 1]:
            fields[i + 1] = "father_alt_surname"
        elif fields[i] == "mother_surname" and not fields[i + 1]:
            fields[i + 1] = "mother_alt_surname"
    return fields


def make_id(seq, url, role, name, surname, date, place):
    """Stable 8-char hex id for a person-occurrence within a JSON file.

    Source xlsx has no person identifiers, so we derive one from the row
    sequence number ('zp. št.'), the matricula page URL, the role, and the
    person's name/surname/date/place. Including the per-row seq guarantees
    uniqueness even for twin records that share name+date+address.
    """
    key = "\x1f".join([
        str(seq) if seq not in (None, "") else "",
        url or "", role, name or "", surname or "", date or "", place or "",
    ])
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def make_person_entry(name, surname, sex, alt_surname="", person_id=None):
    entry = {}
    if person_id is not None:
        entry["id"] = person_id
    entry["name"] = name or ""
    entry["surname"] = surname or ""
    entry["sex"] = sex
    entry["date_of_birth"] = ""
    if alt_surname:
        entry["alt_surname"] = alt_surname
    return entry


def birth_record(row):
    child_name = clean_paren_name(
        cell_str(row.get("child_name")) or cell_str(row.get("child_alt_name"))
    )
    father_name = clean_paren_name(cell_str(row.get("father_name")))
    father_surname, father_alt = clean_paren_surnames(
        cell_str(row.get("father_surname")),
        cell_str(row.get("father_alt_surname")),
    )
    mother_name = clean_paren_name(cell_str(row.get("mother_name")))
    mother_surname, mother_alt = clean_paren_surnames(
        cell_str(row.get("mother_surname")),
        cell_str(row.get("mother_alt_surname")),
    )

    # Inherit mother's surname when father is unknown (must run before normalization).
    child_surname = (
        mother_surname if father_name.lower() == "ni znan" else father_surname
    )

    child_name, child_surname = normalize_name_surname(child_name, child_surname)
    father_name, father_surname = normalize_name_surname(father_name, father_surname)
    mother_name, mother_surname = normalize_name_surname(mother_name, mother_surname)

    notes = cell_str(row.get("notes"))
    death_date = extract_death_from_notes(notes)
    seq = row.get("seq")
    url = cell_str(row.get("url"))
    birth_date_g = gedcom_date(row.get("birth_date"))
    place = build_place(row)

    record = {
        "id": make_id(seq, url, "child", child_name, child_surname, birth_date_g, place),
        "name": child_name,
        "surname": child_surname,
        "sex": "",
        "birth": {
            "date": birth_date_g,
            "place": place,
        },
        "death": {"date": death_date, "place": ""},
    }

    baptism = gedcom_date(row.get("baptism_date"))
    if baptism:
        record["baptism"] = {"date": baptism, "place": ""}

    parents_list = []
    if father_name or father_surname:
        parents_list.append(make_person_entry(
            father_name, father_surname, "m", father_alt))
    if mother_name or mother_surname:
        parents_list.append(make_person_entry(
            mother_name, mother_surname, "f", mother_alt))
    if parents_list:
        record["parents_list"] = parents_list

    if url:
        record["links"] = [url]

    if notes:
        record["notes"] = notes

    return record


def marriage_record(row):
    groom_name = clean_paren_name(cell_str(row.get("groom_name")))
    groom_surname, groom_alt = clean_paren_surnames(
        cell_str(row.get("groom_surname")),
        cell_str(row.get("groom_alt_surname")),
    )
    bride_name = clean_paren_name(cell_str(row.get("bride_name")))
    bride_surname, bride_alt = clean_paren_surnames(
        cell_str(row.get("bride_surname")),
        cell_str(row.get("bride_alt_surname")),
    )

    groom_name, groom_surname = normalize_name_surname(groom_name, groom_surname)
    bride_name, bride_surname = normalize_name_surname(bride_name, bride_surname)

    record = {
        "husband": make_person_entry(groom_name, groom_surname, "m", groom_alt),
        "wife": make_person_entry(bride_name, bride_surname, "f", bride_alt),
        "marriage": {
            "date": gedcom_date(row.get("marriage_date")),
            "place": build_place(row),
        },
    }

    url = cell_str(row.get("url"))
    if url:
        record["links"] = [url]

    notes = cell_str(row.get("notes"))
    if notes:
        record["notes"] = notes

    return record


def _first_page_url(url):
    """Rewrite a row's matricula URL to point at page 1 of the same book."""
    if not url:
        return ""
    new_url, n = re.subn(r"pg=\d+", "pg=1", url)
    if n:
        return new_url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}pg=1"


def _parish_from_name(name):
    """Extract the parish from a book filename stem like
    'Indeks K Cerklje na Gorenjskem 1635-1643', 'Indeks P Adlešiči - 1791-1879',
    or 'Indeks P Golac - 1861-1884 - objavljen del'. The parish is everything
    between the 'Indeks K/P' prefix and the first 4-digit year, so trailing
    annotations after the year range are dropped too. Returns '' if the prefix
    doesn't match the expected pattern.
    """
    m = re.match(r"^Indeks\s+[KP]\s+(.+)$", name)
    if not m:
        return ""
    rest = m.group(1)
    ym = re.search(r"(?<!\d)\d{4}(?:-\d{4})?(?!\d)", rest)
    if ym:
        rest = rest[:ym.start()]
    return re.sub(r"[\s-]+$", "", rest.strip())


def _date_from_name(name):
    """Extract the year range (e.g. '1791-1879' or '1635') from a book name.
    Only accepts 4-digit years and matches the first one anywhere in the name,
    so ranges followed by annotations ('... 1861-1884 - objavljen del') still
    parse. Returns '' if the name has no 4-digit year.
    """
    m = re.search(r"(?<!\d)(\d{4}(?:-\d{4})?)(?!\d)", name)
    return m.group(1) if m else ""


def _book_entry(path, count, sample_url):
    kind = detect_book_kind(path)
    name = os.path.splitext(os.path.basename(path))[0]
    return {
        "name": name,
        "parish": _parish_from_name(name),
        "type": "birth" if kind == "K" else "marriage",
        "date": _date_from_name(name),
        "count": count,
        "url": _first_page_url(sample_url),
        "last_modified": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
    }


def _load_contributors():
    try:
        with open(CONTRIBUTORS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {name: {"url": info.get("url"), "intro": info.get("intro")}
                for name, info in data.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _is_up_to_date(input_paths, output_paths):
    """True iff at least one output exists and every existing output is at
    least as new as the newest input. A missing output is treated as
    intentionally absent (no records of that type)."""
    if not input_paths:
        return False
    present_outputs = [p for p in output_paths if os.path.exists(p)]
    if not present_outputs:
        return False
    try:
        oldest_output = min(os.path.getmtime(p) for p in present_outputs)
        newest_input = max(os.path.getmtime(p) for p in input_paths)
    except OSError:
        return False
    return oldest_output >= newest_input


def _load_json_list(path):
    """Read a JSON list from disk; return [] if the file is absent."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _skipped_meta(contributor, births_path, marriages_path, source_mtime, contributors):
    """Build a metadata entry by re-reading existing JSONs (no reprocessing)."""
    try:
        births = _load_json_list(births_path)
        marriages = _load_json_list(marriages_path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    links_count = (sum(1 for r in births if r.get("links"))
                   + sum(1 for r in marriages if r.get("links")))
    info = contributors.get(contributor, {})
    return {
        "contributor": f"{contributor}-matricula",
        "persons_count": len(births),
        "families_count": len(marriages),
        "links_count": links_count,
        "last_modified": datetime.fromtimestamp(source_mtime).isoformat(),
        "url": info.get("url"),
        "intro": info.get("intro"),
        "skipped": True,
    }


def _interpreter_matches(contributor, interpreter):
    """True if the interpret field identifies the contributor folder.

    Two forms are accepted:

    * Direct substring — the plain case, e.g. folder 'Kovačič' inside interpret
      'Kovačič_Martin', or 'Janez Kovačič'.
    * Abbreviated disambiguator — when two contributors share a surname the
      folder appends an initial ('Kovačič-B'), while interpret spells the first
      name out ('Kovačič_Brane'). Both are split on '-', '_' and whitespace and
      we require each folder token to be a prefix of a later interpret token,
      in order, so 'kovačič','b' matches 'kovačič','brane'.
    """
    needle = unicodedata.normalize("NFC", contributor).casefold()
    hay = unicodedata.normalize("NFC", interpreter).casefold()
    if needle in hay:
        return True
    folder_tokens = [t for t in re.split(r"[-_\s]+", needle) if t]
    interp_tokens = [t for t in re.split(r"[-_\s]+", hay) if t]
    if not folder_tokens:
        return False
    j = 0
    for ft in folder_tokens:
        while j < len(interp_tokens) and not interp_tokens[j].startswith(ft):
            j += 1
        if j >= len(interp_tokens):
            return False
        j += 1
    return True


def _check_interpreter(contributor, interpreter, path):
    """Verify the row's interpret field identifies the contributor folder.

    Catches xlsx files placed under the wrong contributor directory before
    they pollute the per-contributor JSON outputs. Empty interpret cells are
    skipped; the first non-empty mismatch aborts the whole run.
    """
    if not interpreter:
        return
    if _interpreter_matches(contributor, interpreter):
        return
    print(
        f"Error: interpret '{interpreter}' in {path} does not contain "
        f"contributor folder name '{contributor}'",
        file=sys.stderr,
    )
    sys.exit(1)


def process_contributor(contributor, files, contributors, full_mode, existing_index):
    births_path = os.path.join(OUTPUT_DIR, f"{contributor}-matricula-persons.json")
    marriages_path = os.path.join(OUTPUT_DIR, f"{contributor}-matricula-families.json")

    source_files = [f for f in files if detect_book_kind(f) is not None]
    skipped_files = [f for f in files if detect_book_kind(f) is None]

    if not full_mode and source_files and _is_up_to_date(
        source_files, [births_path, marriages_path]
    ):
        latest_mtime = max(os.path.getmtime(p) for p in source_files)
        meta_entry = _skipped_meta(
            contributor, births_path, marriages_path, latest_mtime, contributors
        )
        if meta_entry is not None:
            books_index = existing_index.get(contributor)
            if books_index is None:
                books_index = _read_books_index(source_files)
            return {
                "contributor": contributor,
                "births_count": meta_entry["persons_count"],
                "marriages_count": meta_entry["families_count"],
                "skipped_files": skipped_files,
                "meta_entry": meta_entry,
                "books_index": books_index,
            }
        # fall through and reprocess if existing JSON couldn't be read

    print(f"Processing: {contributor}", file=sys.stderr)

    births, marriages = [], []
    books_index = []
    latest_mtime = 0.0

    for path in source_files:
        kind = detect_book_kind(path)
        latest_mtime = max(latest_mtime, os.path.getmtime(path))
        count = 0
        sample_url = ""
        for row in read_rows(path):
            _check_interpreter(contributor, cell_str(row.get("interpreter")), path)
            count += 1
            if not sample_url:
                sample_url = cell_str(row.get("url"))
            if kind == "K":
                births.append(birth_record(row))
            else:
                marriages.append(marriage_record(row))
        books_index.append(_book_entry(path, count, sample_url))

    births.sort(key=lambda r: (
        r.get("surname", "") or "",
        r.get("name", "") or "",
        r.get("birth", {}).get("date", "") or "",
        r.get("birth", {}).get("place", "") or "",
    ))
    marriages.sort(key=lambda r: (
        r.get("husband", {}).get("surname", "") or "",
        r.get("husband", {}).get("name", "") or "",
        r.get("wife", {}).get("surname", "") or "",
        r.get("wife", {}).get("name", "") or "",
        r.get("marriage", {}).get("date", "") or "",
    ))

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    _write_or_remove(births_path, births, latest_mtime)
    _write_or_remove(marriages_path, marriages, latest_mtime)

    links_count = (
        sum(1 for r in births if r.get("links"))
        + sum(1 for r in marriages if r.get("links"))
    )
    last_modified = (
        datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else None
    )
    meta_entry = {
        "contributor": f"{contributor}-matricula",
        "persons_count": len(births),
        "families_count": len(marriages),
        "links_count": links_count,
        "last_modified": last_modified,
        "url": contributors.get(contributor, {}).get("url"),
        "intro": contributors.get(contributor, {}).get("intro"),
        "skipped": False,
    }
    return {
        "contributor": contributor,
        "births_count": len(births),
        "marriages_count": len(marriages),
        "skipped_files": skipped_files,
        "meta_entry": meta_entry,
        "books_index": books_index,
    }


def _to_nfc(obj):
    """Recursively normalize all strings in a JSON-like structure to NFC.

    Filesystem-derived strings come back as NFD on macOS; the JSON outputs we
    surface to other tools should use NFC for stable byte-for-byte comparisons.
    """
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, list):
        return [_to_nfc(x) for x in obj]
    if isinstance(obj, dict):
        return {_to_nfc(k): _to_nfc(v) for k, v in obj.items()}
    return obj


def _write_or_remove(path, records, mtime):
    """Write the JSON if non-empty; otherwise remove any stale file at that path."""
    if records:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=4)
        if mtime:
            os.utime(path, (mtime, mtime))
    elif os.path.exists(path):
        os.remove(path)


def _read_books_index(source_files):
    """Lightweight pass: row count + first URL per book (for skip-mode fallback)."""
    entries = []
    for path in source_files:
        count = 0
        sample_url = ""
        for row in read_rows(path):
            count += 1
            if not sample_url:
                sample_url = cell_str(row.get("url"))
        entries.append(_book_entry(path, count, sample_url))
    return entries


def write_matricula_index(summaries, output_dir):
    """Write matricula-index.json grouping book entries by contributor."""
    index = {}
    for s in summaries:
        books = s.get("books_index") or []
        if books:
            index[s["contributor"]] = books
    path = os.path.join(output_dir, "matricula-index.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(index), f, ensure_ascii=False, indent=4)


def update_metadata_file(new_entries, output_dir):
    """Write metadata.json with this script's "*-matricula" entries, preserving
    all non-"-matricula" entries owned by gedcom_to_json.py.
    """
    path = os.path.join(output_dir, "metadata.json")
    existing = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = []

    new_clean = [{k: v for k, v in e.items() if k != "skipped"} for e in new_entries]
    preserved = [
        e for e in existing
        if not e.get("contributor", "").endswith("-matricula")
    ]
    combined = preserved + new_clean
    combined.sort(key=lambda x: locale.strxfrm(x.get("contributor", "")))

    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(combined), f, ensure_ascii=False, indent=4)


def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="Convert Matricula xlsx index files to JSON.")
    parser.add_argument(
        "--mode",
        choices=["update", "full"],
        default="update",
        help="update (default): skip contributors whose JSON is already up to date; "
             "full: process all contributors and overwrite existing JSON.",
    )
    parser.add_argument("--input-root", default=INPUT_ROOT,
                        help=f"Root directory to scan recursively (default: {INPUT_ROOT}).")
    parser.add_argument("--output-dir", default=OUTPUT_DIR,
                        help=f"Output directory for JSON files (default: {OUTPUT_DIR}).")
    args = parser.parse_args()
    full_mode = args.mode == "full"

    OUTPUT_DIR = args.output_dir

    print(f"Starting Matricula data extraction process (mode: {args.mode})...",
          file=sys.stderr)

    if not os.path.isdir(args.input_root):
        print(f"Error: input directory '{args.input_root}' not found.", file=sys.stderr)
        return 1

    try:
        locale.setlocale(locale.LC_COLLATE, ("sl_SI", "UTF-8"))
    except locale.Error:
        locale.setlocale(locale.LC_COLLATE, "")

    files = sorted(
        (unicodedata.normalize("NFC", p)
         for p in glob(os.path.join(args.input_root, "**", "*.xlsx"), recursive=True)),
        key=locale.strxfrm,
    )
    files = [f for f in files if not os.path.basename(f).startswith("~$")]

    by_contributor = {}
    for f in files:
        rel = os.path.relpath(f, args.input_root)
        parts = rel.split(os.sep)
        contributor = parts[0] if len(parts) > 1 else "matricula"
        by_contributor.setdefault(contributor, []).append(f)

    if not by_contributor:
        print(f"No xlsx files found under '{args.input_root}'.", file=sys.stderr)
        return 0

    contributors = _load_contributors()

    existing_index = {}
    index_path = os.path.join(OUTPUT_DIR, "matricula-index.json")
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                existing_index = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing_index = {}

    summaries = []
    for contributor in sorted(by_contributor, key=locale.strxfrm):
        summaries.append(process_contributor(
            contributor, by_contributor[contributor], contributors,
            full_mode, existing_index))

    update_metadata_file([s["meta_entry"] for s in summaries], OUTPUT_DIR)
    write_matricula_index(summaries, OUTPUT_DIR)

    print("Completed!", file=sys.stderr)

    processed = [s for s in summaries if not s["meta_entry"].get("skipped")]
    col_w = max((len(s["contributor"]) for s in processed), default=12)
    header = f"{'Contributor':<{col_w}}  {'Births':>7}  {'Marriages':>9}  {'Links':>6}"
    print(f"\n{header}")
    print("-" * len(header))
    for s in processed:
        print(f"{s['contributor']:<{col_w}}  "
              f"{s['births_count']:>7}  {s['marriages_count']:>9}  "
              f"{s['meta_entry']['links_count']:>6}")
        for skipped in s["skipped_files"]:
            print(f"  skipped (unknown K/P): {skipped}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
