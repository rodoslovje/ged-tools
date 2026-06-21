"""Convert a Geneanet cemetery (pokopališča) CSV export to JSON.

Reads the latest CSV (by filename) from data/geneanet/ and emits a single pair
of JSON files matching the schema used by gedcom_to_json.py / matricula_to_json.py:

    data/output/Pokopališča-geneanet-persons.json
    data/output/Pokopališča-geneanet-families.json

Plus a per-cemetery statistics index:

    data/output/geneanet-index.json

The CSV has French headers. Each row is one grave entry and may list a primary
person plus a partner ("conjoint"); in that case two person records and one
family record are produced. The source has no person identifiers, no gender,
and no burial date — sex is guessed from the given name, ids are derived, and
burial carries only a place ("<place>, <nom_projet>"). The image URL is ignored
in favour of the cemetery view URL built from depot_id.
"""

import argparse
import csv
import hashlib
import json
import locale
import os
import re
import sys
import unicodedata
from collections import OrderedDict
from datetime import datetime
from glob import glob

INPUT_ROOT = "data/geneanet"
OUTPUT_DIR = "data/output"
CONTRIBUTORS_FILE = "data/contributors.json"

CONTRIBUTOR = "Pokopališča-geneanet"
CEMETERY_URL = "https://en.geneanet.org/cemetery"
DEPOT_URL = "https://en.geneanet.org/cemetery/view/{}"

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Slovenian given-name sex heuristic. The reliable rule is: names ending in -a
# are female, everything else male. These override sets catch the common
# exceptions in both directions. csv first names are diacritic-stripped, so
# the sets below are written without diacritics too.
MALE_A_NAMES = {
    "luka", "miha", "matija", "andrija", "nikola", "jaka", "grega",
    "ilija", "aljosa", "ambroz", "joza", "nedeljko",
}
FEMALE_NON_A_NAMES = {
    "ines", "karmen", "nives", "doris", "iris", "ingrid", "nirvana",
    "noemi", "ruzic", "klementin", "magdalen",
}
# Unisex names default to male (they land in the husband slot when paired).
UNISEX_NAMES = {"sasa", "vanja", "matija"}


def _fold(s):
    """Lowercase + strip diacritics, for case/diacritic-insensitive matching."""
    s = (s or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def guess_sex(given_name):
    """Return 'm', 'f', or '' for a Slovenian given name (first token)."""
    if not given_name:
        return ""
    first = _fold(given_name).split()[0] if given_name.split() else ""
    if not first:
        return ""
    if first in FEMALE_NON_A_NAMES:
        return "f"
    if first in MALE_A_NAMES:
        return "m"
    return "f" if first.endswith("a") else "m"


def gedcom_date(value):
    """Convert a Geneanet 'YYYYMMDD' date (zero-filled) to a GEDCOM date string.

    Month and/or day may be '00' when unknown:
      '19130000' -> '1913'
      '19130500' -> 'MAY 1913'
      '19130512' -> '12 MAY 1913'
    Returns '' for empty / all-zero / unparseable values.
    """
    if value in (None, ""):
        return ""
    s = str(value).strip()
    if not s or set(s) == {"0"}:
        return ""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if not m:
        # Fall back: a bare 4-digit year.
        ym = re.match(r"^(\d{4})$", s)
        return ym.group(1) if ym and ym.group(1) != "0000" else ""
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year == 0:
        return ""
    if month == 0 or month > 12:
        return str(year)
    if day == 0:
        return f"{MONTHS[month - 1]} {year}"
    return f"{day} {MONTHS[month - 1]} {year}"


def title_surname(nom):
    """Title-case an all-caps Geneanet surname ('PRINCIC' -> 'Princic').

    Diacritics are already lost in the source; this only fixes the casing so the
    output reads like the other JSON files. Multi-word surnames are handled
    word by word; an empty cell stays empty.
    """
    nom = (nom or "").strip()
    if not nom:
        return ""
    return " ".join(w.capitalize() for w in nom.split())


# Matches a Slovenian née marker in the note, e.g. 'roj. Simčič', 'r. Rožič'.
NEE_RE = re.compile(r"\b(?:roj\.?|r\.)\s+([A-Za-zČŠŽčšžĆĐćđ][\wČŠŽčšžĆĐćđ'-]*)",
                    re.IGNORECASE)


def parse_note(note):
    """Split a note into (maiden_surname, remaining_note).

    Pulls a 'roj. X' / 'r. X' née surname out of the free-text note and returns
    the surname plus whatever text is left (vulgo names, occupations, contributor
    tags). Returns ('', note) when no née marker is present.
    """
    note = (note or "").strip()
    if not note:
        return "", ""
    m = NEE_RE.search(note)
    if not m:
        return "", note
    maiden = m.group(1).strip()
    rest = (note[:m.start()] + note[m.end():]).strip()
    rest = re.sub(r"\s{2,}", " ", rest).strip(" ,;")
    return maiden, rest


def make_id(row_index, depot_id, role, name, surname):
    """Stable 8-char hex id for a person occurrence.

    The source has no person ids and depot_id repeats across rows (a grave may
    hold several entries), so the row index is folded in to guarantee
    uniqueness, mirroring matricula_to_json's use of the row sequence number.
    """
    key = "\x1f".join([
        str(row_index), depot_id or "", role, name or "", surname or "",
    ])
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def summary_entry(person):
    """Compact reference to a person, as used in partners_list / husband / wife."""
    return {
        "id": person["id"],
        "name": person["name"],
        "surname": person["surname"],
        "sex": person["sex"],
        "date_of_birth": person["birth"]["date"],
    }


def build_burial_place(place, nom_projet):
    parts = [p for p in ((place or "").strip(), (nom_projet or "").strip()) if p]
    return ", ".join(parts)


def process_row(row_index, row, persons, families, cemetery_stats):
    depot_id = (row.get("depot_id") or "").strip()
    place = (row.get("place") or "").strip()
    nom_projet = (row.get("nom_projet") or "").strip()
    burial_place = build_burial_place(place, nom_projet)
    link = DEPOT_URL.format(depot_id) if depot_id else ""

    name = (row.get("prenom") or "").strip()
    surname = title_surname(row.get("nom"))
    sex = guess_sex(name)

    partner_name = (row.get("prenom_conjoint") or "").strip()
    partner_surname = title_surname(row.get("nom_conjoint"))
    has_partner = bool(partner_name or partner_surname)
    partner_sex = guess_sex(partner_name) if has_partner else ""

    maiden, rest_note = parse_note(row.get("note"))

    # The née surname belongs to the female of the row; fall back to primary.
    primary_alt = partner_alt = ""
    if maiden:
        if sex == "f" or not has_partner:
            primary_alt = maiden
        elif partner_sex == "f":
            partner_alt = maiden
        else:
            primary_alt = maiden

    # Skip people with no name and no surname: the grave isn't indexed yet.
    primary = None
    if name or surname:
        # Make ids deterministic from row + role (depot_id repeats across rows).
        primary = {
            "id": make_id(row_index, depot_id, "primary", name, surname),
            "name": name,
            "surname": surname,
            "sex": sex,
        }
        if primary_alt:
            primary["alt_surname"] = primary_alt
        primary["birth"] = {"date": gedcom_date(row.get("date_naissance")), "place": ""}
        primary["death"] = {"date": gedcom_date(row.get("date_deces")), "place": ""}
        primary["burial"] = {"date": "", "place": burial_place}
        primary["links"] = [link] if link else []
        if rest_note:
            primary["notes"] = rest_note

    partner = None
    if has_partner:
        partner = {
            "id": make_id(row_index, depot_id, "partner", partner_name, partner_surname),
            "name": partner_name,
            "surname": partner_surname,
            "sex": partner_sex,
        }
        if partner_alt:
            partner["alt_surname"] = partner_alt
        partner["birth"] = {
            "date": gedcom_date(row.get("date_naissance_conjoint")), "place": ""}
        partner["death"] = {
            "date": gedcom_date(row.get("date_deces_conjoint")), "place": ""}
        partner["burial"] = {"date": "", "place": burial_place}
        partner["links"] = [link] if link else []

    # Cross-link the two people when both are present.
    if primary is not None and partner is not None:
        primary["partners_list"] = [summary_entry(partner)]
        partner["partners_list"] = [summary_entry(primary)]

    if primary is not None:
        persons.append(primary)
    if partner is not None:
        persons.append(partner)

    if primary is not None and partner is not None:
        # Slot male -> husband, female -> wife; default primary->husband.
        if primary["sex"] == "f" and partner["sex"] == "m":
            husband, wife = partner, primary
        else:
            husband, wife = primary, partner
        families.append({
            "husband": summary_entry(husband),
            "wife": summary_entry(wife),
            "marriage": {"date": "", "place": ""},
            "links": [link] if link else [],
        })

    # Skip unindexed rows entirely (no person produced -> no grave/stats).
    person_count = (primary is not None) + (partner is not None)
    if person_count == 0:
        return

    # Per-cemetery statistics.
    stat = cemetery_stats.get(nom_projet)
    if stat is None:
        stat = {
            "name": nom_projet,
            "place": place,
            "type": (row.get("type_projet") or "").strip(),
            "lat": (row.get("lat") or "").strip(),
            "lon": (row.get("lon") or "").strip(),
            "persons_count": 0,
            "families_count": 0,
            "graves_count": 0,
            "url": CEMETERY_URL,
            "_depots": set(),
        }
        cemetery_stats[nom_projet] = stat
    stat["persons_count"] += person_count
    if primary is not None and partner is not None:
        stat["families_count"] += 1
    if depot_id:
        stat["_depots"].add(depot_id)


def _to_nfc(obj):
    """Recursively normalize all strings in a JSON-like structure to NFC."""
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, list):
        return [_to_nfc(x) for x in obj]
    if isinstance(obj, dict):
        return {_to_nfc(k): _to_nfc(v) for k, v in obj.items()}
    return obj


def latest_csv(input_root):
    """Return the lexicographically-latest .csv under input_root, or None.

    Geneanet exports are named with a 'YYYYMMDDhhmmss' timestamp, so the max
    filename is the most recent export.
    """
    files = glob(os.path.join(input_root, "*.csv"))
    if not files:
        return None
    return max(files, key=lambda p: os.path.basename(p))


def write_json(path, data, mtime):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(data), f, ensure_ascii=False, indent=4)
    if mtime:
        os.utime(path, (mtime, mtime))


def update_metadata_file(entry, output_dir):
    """Write the Geneanet entry into metadata.json, preserving all others."""
    path = os.path.join(output_dir, "metadata.json")
    existing = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = []
    preserved = [e for e in existing if e.get("contributor") != CONTRIBUTOR]
    combined = preserved + [entry]
    try:
        combined.sort(key=lambda x: locale.strxfrm(x.get("contributor", "")))
    except Exception:
        combined.sort(key=lambda x: x.get("contributor", ""))
    write_json(path, combined, None)


def update_contributors_file(path):
    """Register Geneanet-Pokopališča in contributors.json if not already present."""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=OrderedDict)
    except (json.JSONDecodeError, OSError):
        return
    if CONTRIBUTOR in data:
        return
    data[CONTRIBUTOR] = OrderedDict([
        ("full_name", "Pokopališča Geneanet"),
        ("email", None),
        ("url", CEMETERY_URL),
    ])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(data), f, ensure_ascii=False, indent=2)


def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(
        description="Convert the latest Geneanet cemetery CSV export to JSON.")
    parser.add_argument("--input-root", default=INPUT_ROOT,
                        help=f"Directory holding the CSV export(s) (default: {INPUT_ROOT}).")
    parser.add_argument("--output-dir", default=OUTPUT_DIR,
                        help=f"Output directory for JSON files (default: {OUTPUT_DIR}).")
    parser.add_argument("--csv", default=None,
                        help="Use this CSV file instead of the latest one in --input-root.")
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir

    try:
        locale.setlocale(locale.LC_COLLATE, ("sl_SI", "UTF-8"))
    except locale.Error:
        locale.setlocale(locale.LC_COLLATE, "")

    csv_path = args.csv or latest_csv(args.input_root)
    if not csv_path or not os.path.exists(csv_path):
        print(f"Error: no CSV found in '{args.input_root}'.", file=sys.stderr)
        return 1

    print(f"Reading: {csv_path}", file=sys.stderr)
    source_mtime = os.path.getmtime(csv_path)

    persons, families = [], []
    cemetery_stats = {}

    csv.field_size_limit(10 * 1024 * 1024)
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row_index, row in enumerate(reader):
            if not any((v or "").strip() for v in row.values()):
                continue
            process_row(row_index, row, persons, families, cemetery_stats)

    # Sort persons and families to match the other tools' stable ordering.
    persons.sort(key=lambda r: (
        r.get("surname", "") or "",
        r.get("name", "") or "",
        r.get("burial", {}).get("place", "") or "",
        r.get("birth", {}).get("date", "") or "",
    ))
    families.sort(key=lambda r: (
        r.get("husband", {}).get("surname", "") or "",
        r.get("husband", {}).get("name", "") or "",
        r.get("wife", {}).get("surname", "") or "",
        r.get("wife", {}).get("name", "") or "",
    ))

    # Finalize the per-cemetery index (resolve grave counts, drop scratch keys).
    index = []
    for stat in cemetery_stats.values():
        stat["graves_count"] = len(stat.pop("_depots"))
        index.append(stat)
    index.sort(key=lambda s: locale.strxfrm(s.get("name", "")))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    persons_path = os.path.join(OUTPUT_DIR, f"{CONTRIBUTOR}-persons.json")
    families_path = os.path.join(OUTPUT_DIR, f"{CONTRIBUTOR}-families.json")
    index_path = os.path.join(OUTPUT_DIR, "geneanet-index.json")

    write_json(persons_path, persons, source_mtime)
    write_json(families_path, families, source_mtime)
    write_json(index_path, index, source_mtime)

    links_count = sum(1 for p in persons if p.get("links"))
    update_metadata_file({
        "contributor": CONTRIBUTOR,
        "persons_count": len(persons),
        "families_count": len(families),
        "links_count": links_count,
        "last_modified": datetime.fromtimestamp(source_mtime).isoformat(),
        "url": CEMETERY_URL,
    }, OUTPUT_DIR)
    update_contributors_file(CONTRIBUTORS_FILE)

    print("Completed!", file=sys.stderr)
    print(f"\nCemeteries: {len(index)}")
    print(f"Persons:    {len(persons)}")
    print(f"Families:   {len(families)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
