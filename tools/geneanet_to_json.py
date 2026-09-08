"""Convert a Geneanet cemetery (pokopališča) CSV export to JSON.

Reads the latest CSV (by filename) from data/geneanet/ and emits one pair of
JSON files per contributor (schema matching gedcom_to_json.py /
matricula_to_json.py), keyed off the row's Geneanet username:

    data/output/<contributor>-geneanet-persons.json
    data/output/<contributor>-geneanet-families.json

With --single-file it instead emits one combined pair:

    data/output/Pokopališča-geneanet-persons.json
    data/output/Pokopališča-geneanet-families.json

The username is resolved to a contributor id through data/geneanet/
username-map.csv (its contributor_id column). The map is also folded into
contributors.json — each mapped contributor gets a "geneanetID" string holding
its username (missing contributors are created from the map's full_name /
profile_url, and a contributor with a geneanetID but no url gets its
Geneanet profile page as url) — so contributors.json always mirrors the
sheet. Rows whose
username has no contributor_id — or no map row at all — stay in the
Pokopališča-geneanet set, and every export username that has no row in the
map at all is reported as a warning so the sheet can be extended. Plus a
per-cemetery statistics index (one entry per cemetery and username, each
carrying its contributor id) — a dict keyed by contributor, or a flat list
with --single-file:

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
from collections import Counter, OrderedDict
from datetime import datetime
from glob import glob

INPUT_ROOT = "data/geneanet"
# Username -> contributor worksheet, looked up inside INPUT_ROOT by default.
USERNAME_MAP = "username-map.csv"
OUTPUT_DIR = "data/output"
CONTRIBUTORS_FILE = "data/contributors.json"

# Contributor id for rows whose Geneanet username is empty or unmapped (every
# row carries a username, so this is only a safety net). Suffixed with
# CONTRIB_SUFFIX it yields the historical "Pokopališča-geneanet" name.
FALLBACK_ID = "Pokopališča"
# Output files and metadata entries are named "<contributor>-geneanet-*", one
# set per contributor, mirroring matricula_to_json's "<contributor>-matricula".
CONTRIB_SUFFIX = "-geneanet"
CEMETERY_URL = "https://en.geneanet.org/cemetery"
DEPOT_URL = "https://en.geneanet.org/cemetery/view/{}"
# Public Geneanet profile page for a username (used for auto-created stubs).
PROFILE_URL = "https://en.geneanet.org/profil/{}"

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
    # otherwise a mapped username wins, else the generic fallback.
    contributor = force_contributor or handle_to_id.get(username) or FALLBACK_ID
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
    stat = cemetery_stats.get((nom_projet, username))
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
            "contributor": contributor,
            "username": username,
            "_depots": set(),
        }
        cemetery_stats[(nom_projet, username)] = stat
    if url_projet and stat["url"] == CEMETERY_URL:
        stat["url"] = url_projet
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


def load_contributors(path):
    """Load contributors.json as an ordered dict (empty on error/absence)."""
    if not os.path.exists(path):
        return OrderedDict()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=OrderedDict)
    except (json.JSONDecodeError, OSError):
        return OrderedDict()


def finalize_index(stats):
    """Turn a contributor's stats dict into a sorted list, resolving scratch keys."""
    index = []
    for stat in stats.values():
        stat["graves_count"] = len(stat.pop("_depots"))
        index.append(stat)
    index.sort(key=lambda s: (locale.strxfrm(s.get("name", "")), s.get("username", "")))
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


def load_username_map(path):
    """Read username-map.csv -> OrderedDict username -> (contributor_id, full_name, url).

    Every row with a username is returned; contributor_id is '' when the sheet
    leaves it blank, which routes that username to FALLBACK_ID.
    """
    mapping = OrderedDict()
    if not os.path.exists(path):
        return mapping
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            handle = (row.get("username") or "").strip()
            if not handle:
                continue
            mapping[handle] = (
                (row.get("contributor_id") or "").strip(),
                (row.get("full_name") or "").strip(),
                (row.get("profile_url") or "").strip() or PROFILE_URL.format(handle),
            )
    return mapping


def sync_contributors(contributors, username_map):
    """Make the geneanetID strings in contributors.json mirror the username map.

    Each mapped username is stored as the geneanetID of its contributor_id
    (creating the entry from full_name / profile_url when missing) and removed
    from any other contributor that still carried it, so re-assigning an id in
    the sheet moves the handle. Usernames with a blank contributor_id are
    removed from everyone. geneanetID holds a single username; when the sheet
    maps several usernames to one contributor the first is kept and the rest
    are reported (their rows still route to that contributor via the map).
    Any contributor with a geneanetID but no url gets its Geneanet profile
    page as url. Returns a list of human-readable changes.
    """
    changes = []
    # Legacy list values -> single string.
    for cid, info in contributors.items():
        gid = info.get("geneanetID")
        if isinstance(gid, list):
            if gid:
                info["geneanetID"] = gid[0]
            else:
                del info["geneanetID"]
            changes.append(f"{cid}: geneanetID list -> single id")
    for handle, (cid, full_name, url) in username_map.items():
        for other, info in contributors.items():
            if other != cid and info.get("geneanetID") == handle:
                del info["geneanetID"]
                changes.append(f"{handle}: detached from {other}")
        if not cid:
            continue
        info = contributors.get(cid)
        if info is None:
            contributors[cid] = OrderedDict([
                ("full_name", full_name or cid),
                ("email", None),
                ("url", url),
                ("geneanetID", handle),
            ])
            changes.append(f"{handle} -> {cid} (new contributor)")
        elif not info.get("geneanetID"):
            info["geneanetID"] = handle
            changes.append(f"{handle} -> {cid}")
        elif info["geneanetID"] != handle:
            print(f"Warning: {cid} already has geneanetID '{info['geneanetID']}'; "
                  f"'{handle}' is routed to it but not recorded.", file=sys.stderr)
    # A contributor known only by a Geneanet username gets its profile page
    # as url so the site can link to it; an explicit url is left alone.
    for cid, info in contributors.items():
        gid = info.get("geneanetID")
        if gid and not info.get("url"):
            info["url"] = PROFILE_URL.format(gid)
            changes.append(f"{cid}: url <- {info['url']}")
    return changes


def save_contributors(path, contributors):
    """Write contributors.json with entries in Slovenian alphabetical order."""
    ordered = OrderedDict(
        (k, contributors[k]) for k in sorted(contributors, key=locale.strxfrm))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_nfc(ordered), f, ensure_ascii=False, indent=2)


def import_username_map(map_path, contributors_path):
    """Fold username-map.csv into contributors.json (see sync_contributors)."""
    if not os.path.exists(map_path):
        print(f"Error: worksheet '{map_path}' not found.", file=sys.stderr)
        return 1
    contributors = load_contributors(contributors_path)
    changes = sync_contributors(contributors, load_username_map(map_path))
    save_contributors(contributors_path, contributors)
    print(f"{len(changes)} change(s) to {contributors_path}.", file=sys.stderr)
    for line in changes:
        print(f"  {line}", file=sys.stderr)
    return 0


def register_fallback_contributor(contributors):
    """Ensure the "Pokopališča-geneanet" entry exists; True when it was added."""
    key = f"{FALLBACK_ID}{CONTRIB_SUFFIX}"
    if key in contributors:
        return False
    contributors[key] = OrderedDict([
        ("full_name", "Pokopališča Geneanet"),
        ("email", None),
        ("url", CEMETERY_URL),
    ])
    return True


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
    parser.add_argument("--username-map", default=None, metavar="CSV",
                        help=f"Username -> contributor worksheet (default: "
                             f"<input-root>/{USERNAME_MAP}).")
    parser.add_argument("--import-map", action="store_true",
                        help="Only fold the username map into contributors.json, then exit.")
    parser.add_argument("--single-file", action="store_true",
                        help="Emit a single combined Pokopališča-geneanet set instead of "
                             "one <contributor>-geneanet-* set per contributor.")
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir
    per_contributor = not args.single_file

    try:
        locale.setlocale(locale.LC_COLLATE, ("sl_SI", "UTF-8"))
    except locale.Error:
        locale.setlocale(locale.LC_COLLATE, "")

    map_path = args.username_map or os.path.join(args.input_root, USERNAME_MAP)
    if args.import_map:
        return import_username_map(map_path, args.contributors)

    csv_path = args.csv or latest_csv(args.input_root)
    if not csv_path or not os.path.exists(csv_path):
        print(f"Error: no CSV found in '{args.input_root}'.", file=sys.stderr)
        return 1

    print(f"Reading: {csv_path}", file=sys.stderr)
    source_mtime = os.path.getmtime(csv_path)

    contributors = load_contributors(args.contributors)
    contributors_dirty = False
    # Username -> contributor routing only matters when splitting per
    # contributor; single-file mode funnels every row into the FALLBACK_ID
    # bucket. The worksheet is the routing source and is mirrored into
    # contributors.json (geneanetID) at the same time.
    handle_to_id = {}
    username_map = OrderedDict()
    if per_contributor:
        username_map = load_username_map(map_path)
        changes = sync_contributors(contributors, username_map)
        contributors_dirty = bool(changes)
        for line in changes:
            print(f"  contributors.json: {line}", file=sys.stderr)
        handle_to_id = {h: cid for h, (cid, _, _) in username_map.items() if cid}

    buckets = {}
    handle_rows = Counter()
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
            handle_rows[(row.get("username") or "").strip()] += 1
            process_row(row_index, row, buckets, handle_to_id,
                        force_contributor=None if per_contributor else FALLBACK_ID)

    if per_contributor:
        unmapped = sorted((h, n) for h, n in handle_rows.items()
                          if h and h not in username_map)
        if unmapped:
            print(f"Warning: {len(unmapped)} username(s) in the export have no row in "
                  f"{map_path} (their rows go to {FALLBACK_ID}{CONTRIB_SUFFIX}):",
                  file=sys.stderr)
            for handle, n in unmapped:
                print(f"  {handle} ({n} rows)", file=sys.stderr)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    written, meta_entries = [], []
    index = {} if per_contributor else []
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
        # The fallback set is registered under its full "-geneanet" name.
        info = contributors.get(name if cid == FALLBACK_ID else cid) or {}
        url = info.get("url") or CEMETERY_URL
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
    if FALLBACK_ID in buckets and register_fallback_contributor(contributors):
        contributors_dirty = True
    if contributors_dirty and os.path.exists(args.contributors):
        save_contributors(args.contributors, contributors)

    all_stats = (s for stats in index.values() for s in stats) \
        if per_contributor else iter(index)
    cemeteries = len({s["name"] for s in all_stats})
    print("Completed!", file=sys.stderr)
    if per_contributor:
        print(f"\nContributors: {len(buckets)}")
    print(f"Cemeteries:   {cemeteries}")
    print(f"Persons:      {total_persons}")
    print(f"Families:     {total_families}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
