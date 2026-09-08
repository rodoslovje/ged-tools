"""Convert a Geneanet cemetery (pokopališča) CSV export to JSON.

Reads the latest CSV (by filename) from data/geneanet/ and, by default, emits a
single pair of JSON files (schema matching gedcom_to_json.py /
matricula_to_json.py):

    data/output/Pokopališča-geneanet-persons.json
    data/output/Pokopališča-geneanet-families.json

With --per-contributor it instead emits one pair per contributor, keyed off the
row's Geneanet username:

    data/output/<contributor>-geneanet-persons.json
    data/output/<contributor>-geneanet-families.json

The username is resolved to a contributor id via the "geneanetID" lists in
contributors.json; an unmapped username becomes its own contributor (and is
registered with a stub entry). Plus a per-cemetery statistics index — a flat
list in single-file mode, a dict keyed by contributor with --per-contributor:

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

# Fallback contributor id for rows whose Geneanet username is empty (every real
# row carries a username, so this is only a safety net). Suffixed with
# CONTRIB_SUFFIX it yields the historical "Pokopališča-geneanet" name.
FALLBACK_ID = "Pokopališča"
# Output files and metadata entries are named "<contributor>-geneanet-*", one
# set per contributor, mirroring matricula_to_json's "<contributor>-matricula".
CONTRIB_SUFFIX = "-geneanet"
CEMETERY_URL = "https://en.geneanet.org/cemetery"
DEPOT_URL = "https://en.geneanet.org/cemetery/view/{}"
# Public Geneanet profile page for a username (used for auto-created stubs).
PROFILE_URL = "https://en.geneanet.org/{}"

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


def get_bucket(buckets, contributor):
    """Return (creating if needed) the per-contributor accumulator."""
    bucket = buckets.get(contributor)
    if bucket is None:
        bucket = {"persons": [], "families": [], "stats": {}}
        buckets[contributor] = bucket
    return bucket


def process_row(row_index, row, buckets, handle_to_id, force_contributor=None):
    depot_id = (row.get("depot_id") or "").strip()
    place = (row.get("place") or "").strip()
    nom_projet = (row.get("nom_projet") or "").strip()
    url_projet = (row.get("url_projet") or "").strip()
    username = (row.get("username") or "").strip()
    burial_place = build_burial_place(place, nom_projet)
    link = DEPOT_URL.format(depot_id) if depot_id else ""

    # Route the row to a contributor: single-file mode forces one bucket;
    # otherwise a mapped geneanetID handle wins, else the raw username becomes
    # its own contributor id, else the generic fallback.
    contributor = force_contributor or handle_to_id.get(username) or username or FALLBACK_ID
    bucket = get_bucket(buckets, contributor)
    persons = bucket["persons"]
    families = bucket["families"]
    cemetery_stats = bucket["stats"]

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
            # Real per-cemetery collection URL from the export; the generic
            # cemetery landing page is used only when a row lacks one.
            "url": url_projet or CEMETERY_URL,
            "_depots": set(),
            "_usernames": set(),
        }
        cemetery_stats[nom_projet] = stat
    if url_projet and stat["url"] == CEMETERY_URL:
        stat["url"] = url_projet
    if username:
        stat["_usernames"].add(username)
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
    """Return the lexicographically-latest export .csv under input_root, or None.

    Geneanet exports are named with a 'YYYYMMDD...' timestamp, so the max
    filename is the most recent export. Only timestamp-named files qualify, so
    helper files like username-map.csv sitting in the same directory are ignored.
    """
    files = [p for p in glob(os.path.join(input_root, "*.csv"))
             if re.match(r"^\d{8}", os.path.basename(p))]
    if not files:
        return None
    return max(files, key=lambda p: os.path.basename(p))


def write_json(path, data, mtime):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(data), f, ensure_ascii=False, indent=4)
    if mtime:
        os.utime(path, (mtime, mtime))


# Extracts the Geneanet username from a contributor's tree/profile url, e.g.
# 'https://gw.geneanet.org/gregorcd_w' -> 'gregorcd_w'.
GENEANET_URL_RE = re.compile(r"geneanet\.org/(?:gw/)?([A-Za-z0-9_]+)")


def load_contributors(path):
    """Load contributors.json as an ordered dict (empty on error/absence)."""
    if not os.path.exists(path):
        return OrderedDict()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=OrderedDict)
    except (json.JSONDecodeError, OSError):
        return OrderedDict()


def build_handle_map(contributors):
    """Map each geneanetID handle -> contributor id, from contributors.json."""
    handle_to_id = {}
    for cid, info in contributors.items():
        for handle in (info.get("geneanetID") or []):
            handle = (handle or "").strip()
            if handle:
                handle_to_id[handle] = cid
    return handle_to_id


def seed_handles_from_urls(contributors, handle_to_id, csv_handles):
    """Auto-fill geneanetID from existing tree/profile urls (URL-confirmed only).

    For a contributor whose url is a Geneanet page, the username there (and its
    tree-suffix-stripped form, 'gregorcd_w' -> 'gregorcd') is added to their
    geneanetID when it is an actual cemetery handle that isn't mapped yet.
    Returns the list of (handle, contributor_id) pairs newly seeded.
    """
    seeded = []
    for cid, info in contributors.items():
        m = GENEANET_URL_RE.search(info.get("url") or "")
        if not m:
            continue
        candidates = {m.group(1), re.sub(r"_[a-z0-9]+$", "", m.group(1))}
        for handle in candidates:
            if handle in csv_handles and handle not in handle_to_id:
                info.setdefault("geneanetID", [])
                if handle not in info["geneanetID"]:
                    info["geneanetID"].append(handle)
                handle_to_id[handle] = cid
                seeded.append((handle, cid))
    return seeded


def merge_stats(dst, src):
    """Merge one contributor's per-cemetery stats dict into another."""
    for name, s in src.items():
        d = dst.get(name)
        if d is None:
            dst[name] = s
            continue
        d["persons_count"] += s["persons_count"]
        d["families_count"] += s["families_count"]
        d["_depots"] |= s["_depots"]
        d["_usernames"] |= s["_usernames"]
        if d["url"] == CEMETERY_URL and s["url"] != CEMETERY_URL:
            d["url"] = s["url"]


def merge_bucket(buckets, src_id, dst_id):
    """Fold the src contributor's accumulated data into the dst contributor."""
    if src_id == dst_id or src_id not in buckets:
        return
    src = buckets.pop(src_id)
    dst = get_bucket(buckets, dst_id)
    dst["persons"].extend(src["persons"])
    dst["families"].extend(src["families"])
    merge_stats(dst["stats"], src["stats"])


def finalize_index(stats):
    """Turn a contributor's stats dict into a sorted list, resolving scratch keys."""
    index = []
    for stat in stats.values():
        stat["graves_count"] = len(stat.pop("_depots"))
        stat["username"] = sorted(stat.pop("_usernames"))
        index.append(stat)
    index.sort(key=lambda s: locale.strxfrm(s.get("name", "")))
    return index


def update_metadata_file(entries, output_dir):
    """Write the per-contributor "-geneanet" entries, preserving all others."""
    path = os.path.join(output_dir, "metadata.json")
    existing = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = []
    preserved = [e for e in existing
                 if not e.get("contributor", "").endswith(CONTRIB_SUFFIX)]
    combined = preserved + entries
    try:
        combined.sort(key=lambda x: locale.strxfrm(x.get("contributor", "")))
    except Exception:
        combined.sort(key=lambda x: x.get("contributor", ""))
    write_json(path, combined, None)


def write_contributors_file(path, contributors, new_ids):
    """Persist seeded geneanetID edits and register auto-created stubs.

    `new_ids` are contributor ids produced from unmapped usernames; each gets a
    minimal stub (full_name = handle, Geneanet profile url, geneanetID = handle)
    so every handle is documented and self-maps on the next run.
    """
    for cid in new_ids:
        if cid == FALLBACK_ID:
            contributors.setdefault(cid, OrderedDict([
                ("full_name", "Pokopališča Geneanet"),
                ("email", None),
                ("url", CEMETERY_URL),
                ("geneanetID", []),
            ]))
        else:
            contributors.setdefault(cid, OrderedDict([
                ("full_name", cid),
                ("email", None),
                ("url", PROFILE_URL.format(cid)),
                ("geneanetID", [cid]),
            ]))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(contributors), f, ensure_ascii=False, indent=2)


def derive_id_from_name(full_name):
    """Contributor id from a full name: the last (surname) token, title-cased.

    'Jana Pahovnik' -> 'Pahovnik'. Multi-word surnames keep their spaces.
    """
    tokens = (full_name or "").strip().split()
    return tokens[-1] if tokens else ""


def import_username_map(map_path, contributors_path):
    """Fold a filled username-map.csv into contributors.json.

    For every worksheet row that has a full_name and/or contributor_id, the
    username is attached (via geneanetID) to that contributor, creating the
    entry when new. The contributor id is the contributor_id column, else the
    surname derived from full_name. Existing entries are never overwritten;
    the username is only added to their geneanetID. Rows left blank are
    ignored, so the sheet can be filled incrementally.
    """
    if not os.path.exists(map_path):
        print(f"Error: worksheet '{map_path}' not found.", file=sys.stderr)
        return 1
    contributors = load_contributors(contributors_path)
    # username -> owning contributor id, to catch a handle mapped twice.
    claimed = {h: cid for cid, info in contributors.items()
               for h in (info.get("geneanetID") or [])}

    added, attached, skipped = [], [], []
    with open(map_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            handle = (row.get("username") or "").strip()
            full_name = (row.get("full_name") or "").strip()
            cid = (row.get("contributor_id") or "").strip() or derive_id_from_name(full_name)
            if not handle or not cid:
                continue
            if handle in claimed and claimed[handle] != cid:
                skipped.append(f"{handle}: already mapped to {claimed[handle]}, not {cid}")
                continue
            info = contributors.get(cid)
            if info is None:
                contributors[cid] = OrderedDict([
                    ("full_name", full_name or cid),
                    ("email", None),
                    ("url", PROFILE_URL.format(handle)),
                    ("geneanetID", [handle]),
                ])
                added.append(f"{handle} -> {cid}")
            else:
                ids = info.setdefault("geneanetID", [])
                if handle not in ids:
                    ids.append(handle)
                    attached.append(f"{handle} -> {cid}")
            claimed[handle] = cid

    with open(contributors_path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(contributors), f, ensure_ascii=False, indent=2)

    print(f"Created {len(added)} contributor(s), attached {len(attached)} handle(s) "
          f"to existing contributors.", file=sys.stderr)
    for line in added + attached:
        print(f"  {line}", file=sys.stderr)
    for line in skipped:
        print(f"  SKIPPED {line}", file=sys.stderr)
    return 0


def register_single_contributor(path, contributors):
    """Register the single "Pokopališča-geneanet" entry (single-file mode).

    Mirrors the historical behaviour: add the entry when missing and leave the
    rest of contributors.json untouched. No handle stubs are created.
    """
    key = f"{FALLBACK_ID}{CONTRIB_SUFFIX}"
    if not os.path.exists(path) or key in contributors:
        return
    contributors[key] = OrderedDict([
        ("full_name", "Pokopališča Geneanet"),
        ("email", None),
        ("url", CEMETERY_URL),
    ])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(contributors), f, ensure_ascii=False, indent=2)


def remove_stale_outputs(output_dir, keep):
    """Delete "*-geneanet-persons/families.json" no longer produced this run."""
    for suffix in ("-geneanet-persons.json", "-geneanet-families.json"):
        for path in glob(os.path.join(output_dir, f"*{suffix}")):
            if path not in keep:
                os.remove(path)


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
    parser.add_argument("--contributors", default=CONTRIBUTORS_FILE,
                        help=f"Path to contributors.json (default: {CONTRIBUTORS_FILE}).")
    parser.add_argument("--import-map", default=None, metavar="CSV",
                        help="Fold a filled username-map.csv (full_name / contributor_id "
                             "columns) into contributors.json, then exit.")
    parser.add_argument("--per-contributor", action="store_true",
                        help="Split output into one <contributor>-geneanet-* set per "
                             "contributor (default: a single Pokopališča-geneanet set).")
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir
    per_contributor = args.per_contributor

    try:
        locale.setlocale(locale.LC_COLLATE, ("sl_SI", "UTF-8"))
    except locale.Error:
        locale.setlocale(locale.LC_COLLATE, "")

    if args.import_map:
        return import_username_map(args.import_map, args.contributors)

    csv_path = args.csv or latest_csv(args.input_root)
    if not csv_path or not os.path.exists(csv_path):
        print(f"Error: no CSV found in '{args.input_root}'.", file=sys.stderr)
        return 1

    print(f"Reading: {csv_path}", file=sys.stderr)
    source_mtime = os.path.getmtime(csv_path)

    contributors = load_contributors(args.contributors)
    # Handle -> contributor routing only matters when splitting per contributor;
    # single-file mode funnels every row into the FALLBACK_ID bucket.
    handle_to_id = build_handle_map(contributors) if per_contributor else {}

    buckets = {}
    csv_handles = set()
    csv.field_size_limit(10 * 1024 * 1024)
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row_index, row in enumerate(reader):
            if not any((v or "").strip() for v in row.values()):
                continue
            # The export concatenates one CSV per cemetery, so a header line
            # is repeated before each block; skip these embedded headers.
            if (row.get("nom") or "").strip() == "nom" \
                    and (row.get("prenom") or "").strip() == "prenom":
                continue
            handle = (row.get("username") or "").strip()
            if handle:
                csv_handles.add(handle)
            process_row(row_index, row, buckets, handle_to_id,
                        force_contributor=None if per_contributor else FALLBACK_ID)

    # Auto-map handles that match an existing contributor's Geneanet url, then
    # fold those handle-keyed buckets into the resolved contributor.
    seeded = []
    if per_contributor:
        seeded = seed_handles_from_urls(contributors, handle_to_id, csv_handles)
        for handle, cid in seeded:
            merge_bucket(buckets, handle, cid)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    written, meta_entries = [], []
    index = {} if per_contributor else []
    new_ids = []
    total_persons = total_families = 0

    for cid in sorted(buckets, key=locale.strxfrm):
        bucket = buckets[cid]
        persons, families = bucket["persons"], bucket["families"]
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

        name = f"{cid}{CONTRIB_SUFFIX}"
        persons_path = os.path.join(OUTPUT_DIR, f"{name}-persons.json")
        families_path = os.path.join(OUTPUT_DIR, f"{name}-families.json")
        write_json(persons_path, persons, source_mtime)
        write_json(families_path, families, source_mtime)
        written += [persons_path, families_path]

        cem_index = finalize_index(bucket["stats"])
        if per_contributor:
            index[cid] = cem_index
        else:
            index = cem_index
        if cid not in contributors:
            new_ids.append(cid)
        url = (contributors.get(cid) or {}).get("url") or (
            CEMETERY_URL if cid == FALLBACK_ID else PROFILE_URL.format(cid))
        meta_entries.append({
            "contributor": name,
            "persons_count": len(persons),
            "families_count": len(families),
            "links_count": sum(1 for p in persons if p.get("links")),
            "last_modified": datetime.fromtimestamp(source_mtime).isoformat(),
            "url": url,
        })
        total_persons += len(persons)
        total_families += len(families)

    index_path = os.path.join(OUTPUT_DIR, "geneanet-index.json")
    write_json(index_path, index, source_mtime)
    remove_stale_outputs(OUTPUT_DIR, set(written))
    update_metadata_file(meta_entries, OUTPUT_DIR)
    if per_contributor:
        if os.path.exists(args.contributors):
            write_contributors_file(args.contributors, contributors, new_ids)
    else:
        register_single_contributor(args.contributors, contributors)

    all_stats = (s for stats in index.values() for s in stats) \
        if per_contributor else iter(index)
    cemeteries = len({s["name"] for s in all_stats})
    print("Completed!", file=sys.stderr)
    if per_contributor:
        print(f"\nContributors: {len(buckets)}")
    print(f"Cemeteries:   {cemeteries}")
    print(f"Persons:      {total_persons}")
    print(f"Families:     {total_families}")
    if seeded:
        print(f"Auto-mapped:  {', '.join(f'{h}->{c}' for h, c in seeded)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
