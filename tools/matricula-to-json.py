"""Convert Matricula Online index spreadsheets (.xlsx) to JSON.

Reads parish index books exported as Excel from data/matricula/<contributor>/
and emits one pair of JSON files per contributor that match the schema used by
gedcom-to-json.py:

    data/output/<contributor>-matricula-persons.json
    data/output/<contributor>-matricula-families.json

Birth books are recognised by " K " in the filename, marriage books by " P ".
"""

import argparse
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
                if all(c in (None, "") for c in raw):
                    continue
                record = {}
                for idx, value in enumerate(raw):
                    if idx >= len(field_for_col):
                        break
                    field = field_for_col[idx]
                    if not field:
                        continue
                    if value in (None, ""):
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


def make_person_entry(name, surname, sex, alt_surname=""):
    entry = {
        "name": name or "",
        "surname": surname or "",
        "sex": sex,
        "date_of_birth": "",
    }
    if alt_surname:
        entry["alt_surname"] = alt_surname
    return entry


def birth_record(row):
    child_name = cell_str(row.get("child_name")) or cell_str(row.get("child_alt_name"))
    father_name = cell_str(row.get("father_name"))
    father_surname = cell_str(row.get("father_surname"))
    father_alt = cell_str(row.get("father_alt_surname"))
    mother_name = cell_str(row.get("mother_name"))
    mother_surname = cell_str(row.get("mother_surname"))
    mother_alt = cell_str(row.get("mother_alt_surname"))

    child_surname = (
        mother_surname if father_name.lower() == "ni znan" else father_surname
    )

    notes = cell_str(row.get("notes"))
    death_date = extract_death_from_notes(notes)

    record = {
        "name": child_name,
        "surname": child_surname,
        "sex": "",
        "birth": {
            "date": gedcom_date(row.get("birth_date")),
            "place": build_place(row),
        },
        "death": {"date": death_date, "place": ""},
    }

    baptism = gedcom_date(row.get("baptism_date"))
    if baptism:
        record["baptism"] = {"date": baptism, "place": ""}

    parents_list = []
    if father_name or father_surname:
        parents_list.append(make_person_entry(father_name, father_surname, "m", father_alt))
    if mother_name or mother_surname:
        parents_list.append(make_person_entry(mother_name, mother_surname, "f", mother_alt))
    if parents_list:
        record["parents_list"] = parents_list

    url = cell_str(row.get("url"))
    if url:
        record["links"] = [url]

    if notes:
        record["notes"] = notes

    return record


def marriage_record(row):
    groom_name = cell_str(row.get("groom_name"))
    groom_surname = cell_str(row.get("groom_surname"))
    groom_alt = cell_str(row.get("groom_alt_surname"))
    bride_name = cell_str(row.get("bride_name"))
    bride_surname = cell_str(row.get("bride_surname"))
    bride_alt = cell_str(row.get("bride_alt_surname"))

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


def _load_contributor_urls():
    try:
        with open(CONTRIBUTORS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {name: info.get("url") for name, info in data.items() if info.get("url")}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _is_up_to_date(input_paths, output_paths):
    """True iff all output_paths exist and are at least as new as every input."""
    if not output_paths or not input_paths:
        return False
    try:
        oldest_output = min(os.path.getmtime(p) for p in output_paths)
        newest_input = max(os.path.getmtime(p) for p in input_paths)
    except OSError:
        return False
    return oldest_output >= newest_input


def _skipped_meta(contributor, births_path, marriages_path, source_mtime, contributor_urls):
    """Build a metadata entry by re-reading existing JSONs (no reprocessing)."""
    try:
        with open(births_path, encoding="utf-8") as f:
            births = json.load(f)
        with open(marriages_path, encoding="utf-8") as f:
            marriages = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    links_count = (sum(1 for r in births if r.get("links"))
                   + sum(1 for r in marriages if r.get("links")))
    return {
        "contributor": f"{contributor}-matricula",
        "persons_count": len(births),
        "families_count": len(marriages),
        "links_count": links_count,
        "last_modified": datetime.fromtimestamp(source_mtime).isoformat(),
        "url": contributor_urls.get(contributor),
        "skipped": True,
    }


def process_contributor(contributor, files, contributor_urls, full_mode):
    births_path = os.path.join(OUTPUT_DIR, f"{contributor}-matricula-persons.json")
    marriages_path = os.path.join(OUTPUT_DIR, f"{contributor}-matricula-families.json")

    source_files = [f for f in files if detect_book_kind(f) is not None]
    skipped_files = [f for f in files if detect_book_kind(f) is None]

    if not full_mode and source_files and _is_up_to_date(
        source_files, [births_path, marriages_path]
    ):
        latest_mtime = max(os.path.getmtime(p) for p in source_files)
        meta_entry = _skipped_meta(
            contributor, births_path, marriages_path, latest_mtime, contributor_urls
        )
        if meta_entry is not None:
            return {
                "contributor": contributor,
                "births_count": meta_entry["persons_count"],
                "marriages_count": meta_entry["families_count"],
                "skipped_files": skipped_files,
                "meta_entry": meta_entry,
            }
        # fall through and reprocess if existing JSON couldn't be read

    print(f"Processing: {contributor}", file=sys.stderr)

    births, marriages = [], []
    latest_mtime = 0.0

    for path in source_files:
        kind = detect_book_kind(path)
        latest_mtime = max(latest_mtime, os.path.getmtime(path))
        for row in read_rows(path):
            if kind == "K":
                births.append(birth_record(row))
            else:
                marriages.append(marriage_record(row))

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

    with open(births_path, "w", encoding="utf-8") as f:
        json.dump(births, f, ensure_ascii=False, indent=4)
    with open(marriages_path, "w", encoding="utf-8") as f:
        json.dump(marriages, f, ensure_ascii=False, indent=4)

    if latest_mtime:
        os.utime(births_path, (latest_mtime, latest_mtime))
        os.utime(marriages_path, (latest_mtime, latest_mtime))

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
        "url": contributor_urls.get(contributor),
        "skipped": False,
    }
    return {
        "contributor": contributor,
        "births_count": len(births),
        "marriages_count": len(marriages),
        "skipped_files": skipped_files,
        "meta_entry": meta_entry,
    }


def update_metadata_file(new_entries, output_dir):
    """Write metadata.json with this script's "*-matricula" entries, preserving
    all non-"-matricula" entries owned by gedcom-to-json.py.
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
        json.dump(combined, f, ensure_ascii=False, indent=4)


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

    files = sorted(glob(os.path.join(args.input_root, "**", "*.xlsx"), recursive=True),
                   key=locale.strxfrm)
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

    contributor_urls = _load_contributor_urls()

    summaries = []
    for contributor in sorted(by_contributor, key=locale.strxfrm):
        summaries.append(process_contributor(
            contributor, by_contributor[contributor], contributor_urls, full_mode))

    update_metadata_file([s["meta_entry"] for s in summaries], OUTPUT_DIR)

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
