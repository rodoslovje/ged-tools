"""Convert Matricula Online index spreadsheets (.xlsx) to JSON.

Reads parish index books exported as Excel from data/matricula/<contributor>/
and emits one pair of JSON files per contributor that match the schema used by
gedcom-to-json.py:

    data/output/<contributor>-matricula-births.json
    data/output/<contributor>-matricula-marriage.json

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

    record = {
        "name": child_name,
        "surname": child_surname,
        "sex": "",
        "birth": {
            "date": gedcom_date(row.get("birth_date")),
            "place": build_place(row),
        },
        "death": {"date": "", "place": ""},
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

    notes = cell_str(row.get("notes"))
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


def process_contributor(contributor, files):
    births, marriages = [], []
    skipped = []
    latest_mtime = 0.0

    for path in files:
        kind = detect_book_kind(path)
        if kind is None:
            skipped.append(path)
            continue
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
    births_path = os.path.join(OUTPUT_DIR, f"{contributor}-matricula-births.json")
    marriages_path = os.path.join(OUTPUT_DIR, f"{contributor}-matricula-marriage.json")

    with open(births_path, "w", encoding="utf-8") as f:
        json.dump(births, f, ensure_ascii=False, indent=4)
    with open(marriages_path, "w", encoding="utf-8") as f:
        json.dump(marriages, f, ensure_ascii=False, indent=4)

    if latest_mtime:
        os.utime(births_path, (latest_mtime, latest_mtime))
        os.utime(marriages_path, (latest_mtime, latest_mtime))

    return {
        "contributor": contributor,
        "births_count": len(births),
        "marriages_count": len(marriages),
        "skipped_files": skipped,
    }


def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="Convert Matricula xlsx index files to JSON.")
    parser.add_argument("--input-root", default=INPUT_ROOT,
                        help=f"Root directory to scan recursively (default: {INPUT_ROOT}).")
    parser.add_argument("--output-dir", default=OUTPUT_DIR,
                        help=f"Output directory for JSON files (default: {OUTPUT_DIR}).")
    args = parser.parse_args()

    OUTPUT_DIR = args.output_dir

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

    summaries = []
    for contributor in sorted(by_contributor, key=locale.strxfrm):
        summaries.append(process_contributor(contributor, by_contributor[contributor]))

    col_w = max((len(s["contributor"]) for s in summaries), default=12)
    header = f"{'Contributor':<{col_w}}  {'Births':>7}  {'Marriages':>9}"
    print(header)
    print("-" * len(header))
    for s in summaries:
        print(f"{s['contributor']:<{col_w}}  {s['births_count']:>7}  {s['marriages_count']:>9}")
        for skipped in s["skipped_files"]:
            print(f"  skipped (unknown K/P): {skipped}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
