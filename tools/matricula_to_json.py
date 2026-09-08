"""Convert Matricula Online index spreadsheets (.xlsx) to JSON.

Reads parish index books exported as Excel from data/matricula/<contributor>/
and emits one pair of JSON files per contributor that match the schema used by
gedcom_to_json.py:

    data/output/<contributor>-matricula-persons.json
    data/output/<contributor>-matricula-families.json

Birth books are recognised by " K " in the filename, marriage books by " P ";
workbooks that hold both kinds keep each in its own sheet and the kind is then
taken from the sheet's own columns.

data/output/matricula-index.json lists the matricula books behind those
records, one entry per book. A spreadsheet usually indexes a single book and
is named after it, but some walk several ('Indeks P in K Čepovan 1735-1910'
covers five); those are split by the book URL of each row and named
'Indeks K/P <parish> - <years>', with the year range read off the book's
parish page on matricula-online.eu and cached in .matricula_to_json.cache.
"""

import argparse
import hashlib
import html
import json
import locale
import os
import re
import ssl
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime
from glob import glob

import openpyxl

try:
    import certifi
except ImportError:  # falls back to whatever CA store this Python was built with
    certifi = None

INPUT_ROOT = "data/matricula"
OUTPUT_DIR = "data/output"
CONTRIBUTORS_FILE = "data/contributors.json"

# Book titles and year ranges are only published on matricula's parish pages,
# so files that index several books need one HTTP request per parish (the page
# lists every book of that parish). Results are cached so repeat runs and
# --no-fetch stay offline.
CACHE_FILE = ".matricula_to_json.cache"
MATRICULA_HOST = "data.matricula-online.eu"
FETCH_TIMEOUT = 20
USER_AGENT = "ged-tools/matricula_to_json"
# Parish pages list 50 books each; large parishes run to several pages.
MAX_PARISH_PAGES = 50
ALLOW_FETCH = True

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
    """Return 'K' (births), 'P' (marriages), or None.

    Files holding both kinds name both markers ('Indeks P in K Čepovan
    1735-1910'); 'K' wins here, but for those the per-sheet columns decide
    and this only serves as a fallback.
    """
    base = os.path.basename(filename)
    if re.search(r"(?:^|[ _])K(?:[ _])", base):
        return "K"
    if re.search(r"(?:^|[ _])P(?:[ _])", base):
        return "P"
    return None


BIRTH_FIELDS = frozenset({
    "birth_date", "baptism_date", "child_name", "child_alt_name",
    "father_name", "father_surname", "father_alt_surname",
    "mother_name", "mother_surname", "mother_alt_surname",
})

MARRIAGE_FIELDS = frozenset({
    "marriage_date",
    "groom_name", "groom_surname", "groom_alt_surname",
    "bride_name", "bride_surname", "bride_alt_surname",
})


def detect_sheet_kind(fields):
    """Return 'K'/'P' from a sheet's resolved column names, or None if unclear.

    The two record types share their generic columns ('zp. št.', 'župnija',
    'naslov', 'url naslov') and differ in the role-specific ones, so counting
    role columns separates them without depending on sheet names.
    """
    present = set(fields)
    births = len(present & BIRTH_FIELDS)
    marriages = len(present & MARRIAGE_FIELDS)
    if births > marriages:
        return "K"
    if marriages > births:
        return "P"
    return None


def read_rows(path):
    """Yield (kind, record) pairs, one per data row, record keyed by field name.

    A sheet qualifies as data when its header row contains at least three
    known column names. For columns whose header is None but which sit between
    'X name' and 'X surname' (or 'X surname' and the next labelled field),
    we infer alt-name slots positionally — this covers the wide variant
    of K Kranj-Šmartin 1892-1908 where alt-name columns lack headers.

    Most workbooks hold one index, but some keep one sheet per record type —
    'Indeks P in K Čepovan 1735-1910.xlsx' has a 'Poroke' (marriages) and a
    'Krsti' (births) sheet — so every qualifying sheet is read. Only the FIRST
    sheet of a given kind is used: workbooks routinely keep working copies of
    the same index in extra sheets ('List1', or per-decade splits of a merged
    sheet), and reading those would duplicate every record.
    """
    fallback_kind = detect_book_kind(path)
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    seen_kinds = set()
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

            kind = detect_sheet_kind(field_for_col) or fallback_kind
            if kind is None or kind in seen_kinds:
                continue
            seen_kinds.add(kind)

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
                    yield kind, record
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
    or 'Indeks P Golac - 1861-1884 - objavljen del'. Files carrying both kinds
    spell both markers ('Indeks P in K Čepovan 1735-1910'). The parish is
    everything between the 'Indeks K/P' prefix and the first 4-digit year, so
    trailing annotations after the year range are dropped too. Returns '' if
    the prefix doesn't match the expected pattern.
    """
    m = re.match(r"^Indeks\s+[KP](?:\s+in\s+[KP])?\s+(.+)$", name)
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


def _split_book_url(url):
    """Split a matricula row URL into (parish_path, signature).

    Row URLs address one page of one book:
      https://data.matricula-online.eu/sl/slovenia/koper/Cepovan/<sig>/?pg=108
    The last path segment is the book signature (percent-encoded, and
    double-encoded in the Koper archive); everything before it addresses the
    parish. The leading locale segment is dropped so the same book reached
    through /sl/, /de/ or /en/ groups together. Returns (None, None) for cells
    that are not matricula book URLs — some sheets keep free-text notes such
    as 'ne najdem več v knjigi' in the url column.
    """
    if not url:
        return None, None
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None, None
    if not parts.netloc.lower().endswith("matricula-online.eu"):
        return None, None
    segments = [seg for seg in parts.path.split("/") if seg]
    if segments and len(segments[0]) == 2 and segments[0].isalpha():
        segments = segments[1:]
    if len(segments) < 2:
        return None, None
    return "/".join(segments[:-1]), segments[-1]


def _decode_signature(signature):
    """Human-readable form of a URL signature segment ('ŠAK Ž Čep MKK 1')."""
    decoded = urllib.parse.unquote_plus(signature)
    if "%" in decoded:  # Koper signatures are percent-encoded twice
        decoded = urllib.parse.unquote_plus(decoded)
    return re.sub(r"\s+", " ", decoded).strip()


# One <tr> per book: view/details links, signature, title, displayed span.
_BOOK_ROW_RE = re.compile(
    r"<tr>\s*<td[^>]*>(?P<links>.*?)</td>\s*"
    r"<td[^>]*>(?P<signature>.*?)</td>\s*"
    r"<td[^>]*>(?P<title>.*?)</td>\s*"
    r"<td[^>]*>(?P<span>.*?)</td>\s*</tr>",
    re.S,
)
_DETAILS_RE = re.compile(r'id="details-(?P<id>\d+)"(?P<body>.*?)</tr>', re.S)
_DT_DD_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _strip_tags(fragment):
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", text).strip()


def _book_date(details, span_cell):
    """Year range for a book: 'Od/Do datuma' if present, else the span cell.

    The detail dates are authoritative; the displayed span is free text and
    may list several sub-ranges ('1743-1783, 1784-1819').
    """
    years = []
    for label in ("Od datuma", "Do datuma"):
        found = _YEAR_RE.search((details or {}).get(label, ""))
        if found:
            years.append(found.group(1))
    if len(years) != 2:
        years = _YEAR_RE.findall(_strip_tags(span_cell))
    if not years:
        return ""
    first, last = years[0], years[-1]
    return first if first == last else f"{first}-{last}"


def parse_parish_books(page_html):
    """Map signature segment -> {'title', 'date'} from a parish listing page.

    Every book of a parish is listed on that one page, each as a four-cell row
    followed by a collapsed detail row carrying 'Od datuma' / 'Do datuma'.
    """
    details = {
        m.group("id"): {_strip_tags(k): _strip_tags(v)
                        for k, v in _DT_DD_RE.findall(m.group("body"))}
        for m in _DETAILS_RE.finditer(page_html)
    }

    books = {}
    for m in _BOOK_ROW_RE.finditer(page_html):
        links = m.group("links")
        href = re.search(r'href="(/[^"#]+)"', links)
        if not href:
            continue
        _, signature = _split_book_url(
            urllib.parse.urljoin(f"https://{MATRICULA_HOST}/", href.group(1))
        )
        if not signature:
            continue
        detail_id = re.search(r'href="#details-(\d+)"', links)
        info = details.get(detail_id.group(1)) if detail_id else None
        books[signature] = {
            "title": _strip_tags(m.group("title")),
            "date": _book_date(info, m.group("span")),
        }
    return books


_parish_cache = None
_parish_fetched = set()


def _ssl_context():
    """Verified TLS context, using certifi's CA bundle when it is installed.

    Python builds from python.org ship without a CA store until the bundled
    'Install Certificates.command' is run, which makes every https fetch fail;
    certifi covers that case without touching the interpreter.
    """
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _load_parish_cache():
    global _parish_cache
    if _parish_cache is None:
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                _parish_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _parish_cache = {}
    return _parish_cache


def save_parish_cache():
    """Persist fetched parish listings so later runs need no network."""
    if _parish_cache is None:
        return
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_parish_cache, f, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError as exc:
        print(f"Warning: could not write {CACHE_FILE}: {exc}", file=sys.stderr)


def _fetch_page(url):
    """Return the page body, or None after reporting why it could not be read."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT,
                                    context=_ssl_context()) as response:
            return response.read().decode("utf-8", "replace")
    except ssl.SSLCertVerificationError as exc:
        print(f"Warning: could not fetch {url}: {exc}\n"
              "  This Python has no CA bundle. Install one with "
              "'pip install certifi', or run\n"
              "  /Applications/Python 3.x/Install Certificates.command on macOS.",
              file=sys.stderr)
    except Exception as exc:
        print(f"Warning: could not fetch {url}: {exc}", file=sys.stderr)
    return None


def fetch_parish_books(parish_path):
    """Fetch every book of a parish, following the listing's pagination.

    Parish pages show 50 books at a time and their pager only links to
    neighbouring pages, so we walk forward while the current page still points
    at the next one. Returns {} when the first page could not be read.
    """
    base = f"https://{MATRICULA_HOST}/sl/{parish_path}/"
    books = {}
    for page in range(1, MAX_PARISH_PAGES + 1):
        url = base if page == 1 else f"{base}?page={page}"
        print(f"Fetching book list: {url}", file=sys.stderr)
        body = _fetch_page(url)
        if body is None:
            break
        found = parse_parish_books(body)
        if not found:
            print(f"Warning: no books listed on {url}", file=sys.stderr)
            break
        books.update(found)
        if f'href="?page={page + 1}"' not in body:
            break
    return books


def lookup_books(parish_path, signatures):
    """Return {signature: {'title', 'date'}} for one parish, fetching if needed.

    One parish listing describes every book of that parish, so it covers all
    signatures a file references. We only go back to the network when a needed
    signature is missing from the cache (a newly indexed book), and at most
    once per parish per run. A failed fetch is not fatal: callers fall back to
    filename- and signature-derived values.
    """
    cache = _load_parish_cache()
    books = cache.get(parish_path) or {}
    if not ALLOW_FETCH or parish_path in _parish_fetched:
        return books
    if all(sig in books for sig in signatures):
        return books

    _parish_fetched.add(parish_path)
    fetched = fetch_parish_books(parish_path)
    if not fetched:
        return books
    merged = dict(books)
    merged.update(fetched)
    cache[parish_path] = merged
    return merged


def new_bucket_state():
    """Accumulator for grouping a file's rows by the matricula book they cite."""
    return {"buckets": [], "by_key": {}, "current": {}, "pending": {}}


def bucket_row(state, kind, row):
    """Attribute one row to its book bucket, creating it on first sight.

    A single sheet often indexes several books — 'Krsti' in the Čepovan file
    walks MKK 1, 2 and 3 — and the book is the row URL minus its '?pg='
    argument. Rows whose url cell is empty or free text are attributed to the
    book of the preceding row of the same kind, so a stray blank url does not
    split a book in two; rows seen before that kind's first URL are credited
    to the first book found.
    """
    parish_path, signature = _split_book_url(cell_str(row.get("url")))
    if signature:
        key = (kind, parish_path, signature)
        bucket = state["by_key"].get(key)
        if bucket is None:
            bucket = {
                "kind": kind,
                "parish_path": parish_path,
                "signature": signature,
                "url": cell_str(row.get("url")),
                "count": state["pending"].pop(kind, 0),
                "parishes": {},
            }
            state["by_key"][key] = bucket
            state["buckets"].append(bucket)
        state["current"][kind] = bucket
    else:
        bucket = state["current"].get(kind)
        if bucket is None:
            state["pending"][kind] = state["pending"].get(kind, 0) + 1
            return

    bucket["count"] += 1
    parish = cell_str(row.get("parish"))
    if parish:
        bucket["parishes"][parish] = bucket["parishes"].get(parish, 0) + 1


def _same_parish(a, b):
    """True when two parish spellings differ only in punctuation or accents."""
    def key(value):
        stripped = "".join(c for c in unicodedata.normalize("NFD", value)
                           if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()
    return bool(a) and key(a) == key(b)


def _bucket_parish(bucket, fallback):
    """Parish of a book: the most common 'župnija' cell, else the filename's.

    A file can span parishes, so the cell is authoritative — but when both
    agree apart from punctuation ('Sv Jurij ob Ščavnici' vs the filename's
    'Sv. Jurij ob Ščavnici') the filename spelling wins, so these entries read
    the same as the single-book ones beside them in the index.
    """
    if not bucket["parishes"]:
        return fallback
    parish = max(sorted(bucket["parishes"]), key=bucket["parishes"].get)
    return fallback if _same_parish(parish, fallback) else parish


def _book_entry(path, count, sample_url, kind=None):
    """Index entry for a file that indexes a single book: the filename is the
    curator's own label and reads better than anything we could synthesise."""
    kind = kind or detect_book_kind(path)
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


def book_entries(path, buckets, total_count):
    """Index entries for one xlsx file: one per matricula book it references.

    A file spanning several books has no single filename-derived name, parish
    or year range to give them, so each book is named
    'Indeks K/P <parish> - <years>' with the year range read off the parish
    listing page. Books whose range could not be fetched are disambiguated by
    their matricula signature instead.
    """
    if len(buckets) <= 1:
        sample_url = buckets[0]["url"] if buckets else ""
        kind = buckets[0]["kind"] if buckets else None
        return [_book_entry(path, total_count, sample_url, kind)]

    stem = os.path.splitext(os.path.basename(path))[0]
    fallback_parish = _parish_from_name(stem)
    last_modified = datetime.fromtimestamp(os.path.getmtime(path)).isoformat()

    signatures_by_parish = {}
    for bucket in buckets:
        signatures_by_parish.setdefault(bucket["parish_path"], []).append(
            bucket["signature"])
    metadata = {parish_path: lookup_books(parish_path, signatures)
                for parish_path, signatures in signatures_by_parish.items()}

    entries = []
    for bucket in buckets:
        info = metadata.get(bucket["parish_path"], {}).get(bucket["signature"], {})
        parish = _bucket_parish(bucket, fallback_parish)
        date = info.get("date", "")
        suffix = date or _decode_signature(bucket["signature"])
        entries.append({
            "name": f"Indeks {bucket['kind']} {parish} - {suffix}".strip(),
            "parish": parish,
            "type": "birth" if bucket["kind"] == "K" else "marriage",
            "date": date,
            "count": bucket["count"],
            "url": _first_page_url(bucket["url"]),
            "last_modified": last_modified,
        })

    # Two books of the same kind, parish and range would otherwise collide.
    counts = {}
    for entry in entries:
        counts[entry["name"]] = counts.get(entry["name"], 0) + 1
    for entry, bucket in zip(entries, buckets):
        if counts[entry["name"]] > 1:
            entry["name"] = f"{entry['name']} ({_decode_signature(bucket['signature'])})"
    return entries


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
    skipped; a non-empty mismatch raises ValueError so the caller can report
    the file and skip it.
    """
    if not interpreter:
        return
    if _interpreter_matches(contributor, interpreter):
        return
    raise ValueError(
        f"interpret '{interpreter}' does not contain "
        f"contributor folder name '{contributor}'"
    )


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
    failed_files = []
    latest_mtime = 0.0

    for path in source_files:
        try:
            file_births, file_marriages = [], []
            state = new_bucket_state()
            count = 0
            for kind, row in read_rows(path):
                _check_interpreter(contributor, cell_str(row.get("interpreter")), path)
                count += 1
                bucket_row(state, kind, row)
                if kind == "K":
                    file_births.append(birth_record(row))
                else:
                    file_marriages.append(marriage_record(row))
        except Exception as exc:
            failed_files.append(path)
            print(f"Error: failed to process {path}: {exc}; skipping file",
                  file=sys.stderr)
            continue

        # Only commit a file's records and advance the output mtime once it
        # parsed cleanly, so a failed file is retried on the next run.
        latest_mtime = max(latest_mtime, os.path.getmtime(path))
        births.extend(file_births)
        marriages.extend(file_marriages)
        books_index.extend(book_entries(path, state["buckets"], count))

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
        "failed_files": failed_files,
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
    """Lightweight pass: per-book row counts and URLs (for skip-mode fallback)."""
    entries = []
    for path in source_files:
        state = new_bucket_state()
        count = 0
        for kind, row in read_rows(path):
            count += 1
            bucket_row(state, kind, row)
        entries.extend(book_entries(path, state["buckets"], count))
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
    global OUTPUT_DIR, ALLOW_FETCH
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
    parser.add_argument("--no-fetch", action="store_true",
                        help="Never contact matricula-online.eu; name multi-book "
                             f"files from {CACHE_FILE} alone.")
    args = parser.parse_args()
    full_mode = args.mode == "full"

    OUTPUT_DIR = args.output_dir
    ALLOW_FETCH = not args.no_fetch

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
    save_parish_cache()

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

    failed_files = [f for s in summaries for f in s.get("failed_files", [])]
    if failed_files:
        print(f"\n{len(failed_files)} file(s) failed and were skipped:",
              file=sys.stderr)
        for f in failed_files:
            print(f"  {f}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
