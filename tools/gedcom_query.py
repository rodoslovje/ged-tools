#!/usr/bin/env python3
"""
gedcom_query: Query a GEDCOM file for individuals or families.

Usage:
    python tools/gedcom_query.py <input.ged> [OPTIONS]

Options:
    --person [PERSON ...]
                  List individuals. Without names: all individuals.
                  With names: only the listed persons (matched by pointer,
                  "Given Surname", or "Given Surname YYYY").
    --ancestors   With --person names: also include all ancestors.
    --descendants With --person names: also include all descendants.
    --bloodline   With --person names: include all blood relatives
                  (= all descendants of all ancestors of the person).
    --surnames    Output unique surnames instead of full person rows.
    --location    With --surnames: also output the place of the oldest
                  (earliest birth year) occurrence of each surname.
    --family      List all families: Husband Wife ⚭yyyy Place
    --home-person Print the home person (indicated by _STP tag in MacFamilyTree)
    --url [URL]   List INDI and FAM records whose subtree contains URL as a
                  substring (case-insensitive). Omit value to match any URL.
                  By default only direct (non-event) children are searched.
    --search-events   With --url: also search within event subtrees.
    --addr [ADDR] List INDI and FAM records whose ADDR subtree contains ADDR as
                  a substring (case-insensitive). Omit value to match any address.
    --duplicate-url
                  List all URLs that appear in more than one media (OBJE) record,
                  with the persons/families referencing each duplicate.
    --stat        Show counts of top-level GEDCOM records (individuals,
                  families, objects, sources, notes, etc.) plus the number of
                  unique places referenced across all events.
    --sw          Show the software that generated each GEDCOM (from the HEAD
                  record's SOUR/NAME/VERS/CORP). One row per input file.
    --sort-date   Sort --person by birth date and --family by marriage date
                  (chronologically, undated entries last) instead of by name.
                  Also switches the displayed dates from year-only to the full
                  GEDCOM date (e.g. *12 MAR 1901).
    --id          Prepend the GEDCOM pointer (@I..@/@F..@) to each --person or
                  --family row.
    --csv         Output as CSV
    --any-place   For --person/--surnames: fall back to baptism, residence, or
                  death place when birth place is absent (checked in that order)

Multiple input files may be passed (e.g. via a shell glob). For --sw they are
combined into a single table; for other queries each file's output is preceded
by a "=== filename ===" header.

At least one of --person, --surnames, --family, --home-person, --url, --addr, --duplicate-url, --stat, or --sw must be specified.

Examples:
    python tools/gedcom_query.py family.ged --person
    python tools/gedcom_query.py family.ged --person "Luka Renko"
    python tools/gedcom_query.py family.ged --home-person
    python tools/gedcom_query.py family.ged --person "Franc Renko 1901" --ancestors
    python tools/gedcom_query.py family.ged --person "@I1@" --descendants
    python tools/gedcom_query.py family.ged --person "Luka Renko" --bloodline
    python tools/gedcom_query.py family.ged --surnames
    python tools/gedcom_query.py family.ged --surnames --location
    python tools/gedcom_query.py family.ged --person "Luka Renko" --ancestors --surnames --location --any-place
    python tools/gedcom_query.py family.ged --family
    python tools/gedcom_query.py family.ged --person --sort-date
    python tools/gedcom_query.py family.ged --family --sort-date
    python tools/gedcom_query.py family.ged --person --id
    python tools/gedcom_query.py family.ged --family --id --csv > families.csv
    python tools/gedcom_query.py family.ged --person --any-place
    python tools/gedcom_query.py family.ged --person --csv > persons.csv
    python tools/gedcom_query.py family.ged --family --csv > families.csv
    python tools/gedcom_query.py family.ged --url
    python tools/gedcom_query.py family.ged --url familysearch.org
    python tools/gedcom_query.py family.ged --url matricula-online.com --search-events
    python tools/gedcom_query.py family.ged --addr "Sušica 47"
    python tools/gedcom_query.py family.ged --person "Jakob Renka 1764" --descendants --addr
    python tools/gedcom_query.py family.ged --duplicate-url
    python tools/gedcom_query.py family.ged --stat
    python tools/gedcom_query.py ../srd-data/index/filtered/*.ged --sw
    python tools/gedcom_query.py ../srd-data/index/filtered/*.ged --sw --csv > sw.csv
"""

import argparse
import csv
import os
import re
import sys
import tempfile
import unicodedata

import chardet
from gedcom.parser import Parser
import gedcom.tags

# ---------------------------------------------------------------------------
# Locale-aware collation key (č after c, š after s, ž after z)
# ---------------------------------------------------------------------------

_COLLATION_SPECIAL = {
    "č": "c\x7d",
    "ć": "c\x7e",
    "đ": "d\x7f",
    "š": "s\x7f",
    "ž": "z\x7f",
}


def _collation_key(s: str) -> str:
    result = []
    for ch in s.casefold():
        mapped = _COLLATION_SPECIAL.get(ch)
        if mapped:
            result.append(mapped)
        else:
            result.append(unicodedata.normalize("NFD", ch)[0])
    return "".join(result)


# ---------------------------------------------------------------------------
# Encoding detection & transcoding  (mirrors gedcom_filter.py)
# ---------------------------------------------------------------------------

_GEDCOM_CHAR_MAP = {
    "UTF-8": "utf-8",
    "UNICODE": "utf-16",
    "UTF-16": "utf-16",
    "ASCII": "ascii",
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


def _is_disguised_cp1250(raw: bytes) -> bool:
    for i in range(1, len(raw)):
        if raw[i] in (0x9A, 0x9E, 0x8A, 0x8E) and raw[i - 1] < 128:
            return True
    for i in range(len(raw) - 1):
        if raw[i] in (0xE8, 0xC8) and raw[i + 1] < 128:
            return True
    return False


def _build_cp1252_to_cp1250_map():
    mapping = {}
    for byte_val in range(0x80, 0x100):
        b = bytes([byte_val])
        try:
            cp1250_char = b.decode("cp1250")
            cp1252_char = b.decode("cp1252")
            if cp1250_char != cp1252_char:
                mapping[cp1252_char] = cp1250_char
        except (UnicodeDecodeError, ValueError):
            pass
    return str.maketrans(mapping)


_CP1252_TO_CP1250 = _build_cp1252_to_cp1250_map()


def fix_cp1252_as_cp1250(content: str) -> str:
    if "è" in content or "È" in content or "æ" in content or "Æ" in content:
        return content.translate(_CP1252_TO_CP1250)
    return content


def _detect_encoding(file_path: str) -> str:
    with open(file_path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    header_text = raw[:4096].decode("latin-1", errors="replace")
    m = re.search(r"^\d\s+CHAR\s+(.+)$", header_text, re.MULTILINE | re.IGNORECASE)
    if m:
        char_value = m.group(1).strip().upper()
        if char_value in _GEDCOM_CHAR_MAP:
            return _GEDCOM_CHAR_MAP[char_value]
    detected = chardet.detect(raw)
    if detected:
        enc = detected.get("encoding") or ""
        confidence = detected.get("confidence") or 0
        if enc and confidence >= 0.2 and enc.lower() not in ("mac_roman", "ascii"):
            if enc.lower() in (
                "windows-1252",
                "cp1252",
                "iso-8859-1",
                "iso-8859-2",
                "utf-8",
            ):
                if _is_disguised_cp1250(raw):
                    return "windows-1250"
            return enc
    return "windows-1250"


def _transcode_to_utf8(input_path: str) -> tuple[str, bool]:
    with open(input_path, "rb") as f:
        raw = f.read()
    encoding = _detect_encoding(input_path)
    norm = encoding.lower().replace("-", "").replace("_", "")
    if norm in ("utf8", "utf8sig"):
        try:
            text = raw.decode(encoding)
            fixed_text = fix_cp1252_as_cp1250(text)
            if fixed_text == text:
                return input_path, False
            text = fixed_text
        except UnicodeDecodeError:
            if _is_disguised_cp1250(raw):
                encoding = "windows-1250"
            else:
                test_decode = raw.decode(encoding, errors="replace")
                if test_decode.count("") < max(10, len(raw) // 1000):
                    text = fix_cp1252_as_cp1250(test_decode)
                    fd, tmp_path = tempfile.mkstemp(suffix=".ged")
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(text)
                    return tmp_path, True
                detected = chardet.detect(raw)
                enc = (detected.get("encoding") or "") if detected else ""
                confidence = (detected.get("confidence") or 0) if detected else 0
                if (
                    enc
                    and confidence >= 0.2
                    and enc.lower() not in ("mac_roman", "ascii")
                ):
                    encoding = enc
                else:
                    encoding = "windows-1250"
                if encoding.lower() in (
                    "windows-1252",
                    "cp1252",
                    "iso-8859-1",
                    "iso-8859-2",
                    "utf-8",
                ):
                    if _is_disguised_cp1250(raw):
                        encoding = "windows-1250"
    if "text" not in locals():
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("latin-1", errors="replace")
        text = fix_cp1252_as_cp1250(text)

    fd, tmp_path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return tmp_path, True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_year(date_str: str) -> str:
    """Extract 4-digit year from a GEDCOM date string, or '' if none found."""
    if not date_str:
        return ""
    m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", date_str)
    return m.group(1) if m else ""


_MONTH_NUM = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_MONTH_RE = "|".join(_MONTH_NUM)


def _date_sort_key(date_str: str) -> tuple[int, int, int]:
    """Return (year, month, day) for chronological sorting; undated sorts last.

    Missing components sort before present ones within the same year (e.g. a
    bare year precedes 'JAN', which precedes '15 JAN'). Approximation/range
    qualifiers (ABT, BEF, BET ... AND ...) are ignored beyond their year/month.
    """
    if not date_str:
        return (99999, 99, 99)
    year = _extract_year(date_str)
    y = int(year) if year else 99999
    m = 0
    mo = re.search(rf"\b({_MONTH_RE})\b", date_str, re.IGNORECASE)
    if mo:
        m = _MONTH_NUM[mo.group(1).upper()]
    d = 0
    dd = re.search(rf"\b(\d{{1,2}})\s+(?:{_MONTH_RE})\b", date_str, re.IGNORECASE)
    if dd:
        d = int(dd.group(1))
    return (y, m, d)


def _parse_name(name_value: str) -> tuple[str, str]:
    """Parse 'Given /Surname/' into (given, surname)."""
    m = re.match(r"^(.*?)\s*/([^/]*)/\s*(.*)$", (name_value or "").strip())
    if m:
        given = (m.group(1) + " " + m.group(3)).strip()
        surn = m.group(2).strip()
    else:
        given = (name_value or "").strip()
        surn = ""
    return given, surn


def _get_name(indi_el) -> tuple[str, str]:
    """Return (given, surname) of the primary birth name of an individual."""
    fallback = None
    for ch in indi_el.get_child_elements():
        if ch.get_tag() != gedcom.tags.GEDCOM_TAG_NAME:
            continue
        name_type = next(
            (
                sc.get_value().strip().lower()
                for sc in ch.get_child_elements()
                if sc.get_tag() == "TYPE"
            ),
            None,
        )
        if name_type and name_type != "birth":
            if fallback is None:
                fallback = ch.get_value()
            continue
        return _parse_name(ch.get_value())
    return _parse_name(fallback) if fallback else ("", "")


def _get_event_date(indi_el, event_tag: str) -> tuple[str, str]:
    """Return (raw date string, place) of the first occurrence of event_tag."""
    for ch in indi_el.get_child_elements():
        if ch.get_tag() != event_tag:
            continue
        date = place = ""
        for subch in ch.get_child_elements():
            t = subch.get_tag()
            if t == gedcom.tags.GEDCOM_TAG_DATE:
                date = (subch.get_value() or "").strip()
            elif t == gedcom.tags.GEDCOM_TAG_PLACE:
                place = subch.get_value().strip()
        return date, place
    return "", ""


def _get_event(indi_el, event_tag: str) -> tuple[str, str]:
    """Return (year, place) of the first occurrence of event_tag."""
    date, place = _get_event_date(indi_el, event_tag)
    return _extract_year(date), place


def _get_marriage_date(fam_el) -> tuple[str, str]:
    """Return (raw date string, place) of the marriage event of a FAM element."""
    return _get_event_date(fam_el, gedcom.tags.GEDCOM_TAG_MARRIAGE)


def _get_marriage(fam_el) -> tuple[str, str]:
    """Return (year, place) of the marriage event of a FAM element."""
    date, place = _get_marriage_date(fam_el)
    return _extract_year(date), place


def _extract_software(input_path: str) -> dict:
    """Read just the HEAD record and return {sour, name, vers, corp}.

    `2 NAME/VERS/CORP` are only collected when nested directly under `1 SOUR`,
    so the GEDCOM standard version under `1 GEDC / 2 VERS` is not mistaken for
    the software version.
    """
    parse_path, is_tmp = _transcode_to_utf8(input_path)
    try:
        sour = name = vers = corp = ""
        in_sour = False
        with open(parse_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if line.startswith("0 "):
                    if not line.startswith("0 HEAD"):
                        break
                    continue
                if line.startswith("1 "):
                    if line.startswith("1 SOUR"):
                        sour = line[6:].strip()
                        in_sour = True
                    else:
                        in_sour = False
                    continue
                if in_sour and line.startswith("2 "):
                    if line.startswith("2 NAME"):
                        name = line[6:].strip()
                    elif line.startswith("2 VERS"):
                        vers = line[6:].strip()
                    elif line.startswith("2 CORP"):
                        corp = line[6:].strip()
    finally:
        if is_tmp:
            os.unlink(parse_path)
    return {"sour": sour, "name": name, "vers": vers, "corp": corp}


# ---------------------------------------------------------------------------
# Query logic
# ---------------------------------------------------------------------------


def _get_place(indi_el, *event_tags: str) -> str:
    """Return the first non-empty place found across the given event tags."""
    for tag in event_tags:
        for ch in indi_el.get_child_elements():
            if ch.get_tag() != tag:
                continue
            for subch in ch.get_child_elements():
                if subch.get_tag() == gedcom.tags.GEDCOM_TAG_PLACE:
                    val = subch.get_value().strip()
                    if val:
                        return val
    return ""


def _find_persons(queries: list[str], root_elements, ptr_index) -> set[str]:
    """Return INDI pointer set matching any of the given query strings."""
    result = set()
    for q in queries:
        q = q.strip()
        if q.startswith("@") and q.endswith("@"):
            if q in ptr_index:
                result.add(q)
            else:
                print(f"WARNING: pointer '{q}' not found", file=sys.stderr)
            continue
        tokens = q.split()
        year = ""
        if tokens and re.fullmatch(r"\d{4}", tokens[-1]):
            year = tokens[-1]
            tokens = tokens[:-1]
        if not tokens:
            print(f"WARNING: could not parse person query '{q}'", file=sys.stderr)
            continue
        surname = tokens[-1]
        given = " ".join(tokens[:-1])
        found = set()
        for el in root_elements:
            if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue
            g, s = _get_name(el)
            if s.casefold() != surname.casefold():
                continue
            if given and g.casefold() != given.casefold():
                continue
            if year:
                birth_year, _ = _get_event(el, gedcom.tags.GEDCOM_TAG_BIRTH)
                if birth_year != year:
                    continue
            found.add(el.get_pointer().strip())
        if not found:
            print(f"WARNING: no person found for '{q}'", file=sys.stderr)
        result |= found
    return result


def _find_home_persons(root_elements) -> set[str]:
    """Return INDI pointer set for home person(s) marked with _STP."""
    result = set()
    for el in root_elements:
        if el.get_tag() == gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            for ch in el.get_child_elements():
                if ch.get_tag() == "_STP":
                    result.add(el.get_pointer().strip())
                    break
    return result


def _collect_ancestors(ptr_set: set[str], ptr_index) -> set[str]:
    """Return ptr_set expanded with all ancestors (BFS via FAMC)."""
    result = set(ptr_set)
    queue = list(ptr_set)
    while queue:
        ptr = queue.pop()
        indi = ptr_index.get(ptr)
        if indi is None:
            continue
        for ch in indi.get_child_elements():
            if ch.get_tag() != "FAMC":
                continue
            fam = ptr_index.get(ch.get_value().strip())
            if fam is None:
                continue
            for fch in fam.get_child_elements():
                if fch.get_tag() not in (
                    gedcom.tags.GEDCOM_TAG_HUSBAND,
                    gedcom.tags.GEDCOM_TAG_WIFE,
                ):
                    continue
                parent_ptr = fch.get_value().strip()
                if parent_ptr not in result:
                    result.add(parent_ptr)
                    queue.append(parent_ptr)
    return result


def _collect_descendants(ptr_set: set[str], ptr_index) -> set[str]:
    """Return ptr_set expanded with all descendants (BFS via FAMS→CHIL)."""
    result = set(ptr_set)
    queue = list(ptr_set)
    while queue:
        ptr = queue.pop()
        indi = ptr_index.get(ptr)
        if indi is None:
            continue
        for ch in indi.get_child_elements():
            if ch.get_tag() != "FAMS":
                continue
            fam = ptr_index.get(ch.get_value().strip())
            if fam is None:
                continue
            for fch in fam.get_child_elements():
                if fch.get_tag() != gedcom.tags.GEDCOM_TAG_CHILD:
                    continue
                child_ptr = fch.get_value().strip()
                if child_ptr not in result:
                    result.add(child_ptr)
                    queue.append(child_ptr)
    return result


def _get_surname_location(start_el, surn: str, ptr_index, any_place: bool) -> str:
    """Return place for start_el; if missing, walk up same-surname ancestors until found."""
    visited: set[str] = set()
    queue = [start_el]
    while queue:
        next_level = []
        for indi in queue:
            ptr = indi.get_pointer().strip()
            if ptr in visited:
                continue
            visited.add(ptr)
            _, place = _get_event(indi, gedcom.tags.GEDCOM_TAG_BIRTH)
            if any_place and not place:
                place = _get_place(indi, "CHR", "RESI", gedcom.tags.GEDCOM_TAG_DEATH)
            if place:
                return place
            for ch in indi.get_child_elements():
                if ch.get_tag() != "FAMC":
                    continue
                fam = ptr_index.get(ch.get_value().strip())
                if fam is None:
                    continue
                for fch in fam.get_child_elements():
                    if fch.get_tag() not in (
                        gedcom.tags.GEDCOM_TAG_HUSBAND,
                        gedcom.tags.GEDCOM_TAG_WIFE,
                    ):
                        continue
                    parent = ptr_index.get(fch.get_value().strip())
                    if parent is None:
                        continue
                    _, psurn = _get_name(parent)
                    if psurn.casefold() == surn.casefold():
                        next_level.append(parent)
        queue = next_level
    return ""


def _surname_rows(
    root_elements,
    any_place: bool,
    ptr_filter: set | None,
    with_location: bool,
    ptr_index=None,
) -> list:
    if with_location:
        # Group all persons by surname, then iterate oldest-first until a place is found.
        groups: dict[str, list] = {}  # surname -> [(year, el), ...]
        for el in root_elements:
            if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue
            if ptr_filter is not None and el.get_pointer().strip() not in ptr_filter:
                continue
            _, surn = _get_name(el)
            if not surn:
                continue
            year, _ = _get_event(el, gedcom.tags.GEDCOM_TAG_BIRTH)
            groups.setdefault(surn, []).append((year, el))
        rows = []
        for surn, entries in groups.items():
            # Dated persons first (ascending year), undated last
            entries.sort(key=lambda x: (not x[0], x[0]))
            place = ""
            for _, el in entries:
                place = _get_surname_location(el, surn, ptr_index, any_place)
                if place:
                    break
            rows.append((surn, place))
        rows.sort(key=lambda r: _collation_key(r[0]))
        return rows
    else:
        seen: set = set()
        rows = []
        for el in root_elements:
            if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue
            if ptr_filter is not None and el.get_pointer().strip() not in ptr_filter:
                continue
            _, surn = _get_name(el)
            if not surn or surn in seen:
                continue
            seen.add(surn)
            rows.append(surn)
        rows.sort(key=_collation_key)
        return rows


def _person_rows(
    root_elements,
    any_place: bool,
    ptr_filter: set | None = None,
    sort_by_date: bool = False,
) -> list[tuple]:
    rows = []
    for el in root_elements:
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue
        if ptr_filter is not None and el.get_pointer().strip() not in ptr_filter:
            continue
        given, surn = _get_name(el)
        if sort_by_date:
            birth, birth_place = _get_event_date(el, gedcom.tags.GEDCOM_TAG_BIRTH)
            death, _ = _get_event_date(el, gedcom.tags.GEDCOM_TAG_DEATH)
        else:
            birth, birth_place = _get_event(el, gedcom.tags.GEDCOM_TAG_BIRTH)
            death, _ = _get_event(el, gedcom.tags.GEDCOM_TAG_DEATH)
        if any_place and not birth_place:
            birth_place = _get_place(el, "CHR", "RESI", gedcom.tags.GEDCOM_TAG_DEATH)
        rows.append((given, surn, birth, death, birth_place, el.get_pointer().strip()))
    if sort_by_date:
        # Sort by full birth date (undated last), then by name.
        rows.sort(
            key=lambda r: (
                _date_sort_key(r[2]),
                _collation_key(r[1]),
                _collation_key(r[0]),
            )
        )
    else:
        rows.sort(key=lambda r: (_collation_key(r[1]), _collation_key(r[0])))
    return rows


_EVENT_TAGS = frozenset(
    {
        # Individual events
        "BIRT",
        "CHR",
        "DEAT",
        "BURI",
        "CREM",
        "ADOP",
        "BAPM",
        "BARM",
        "BASM",
        "BLES",
        "CHRA",
        "CONF",
        "FCOM",
        "ORDN",
        "NATU",
        "EMIG",
        "IMMI",
        "CENS",
        "PROB",
        "WILL",
        "GRAD",
        "RETI",
        "EVEN",
        # Individual attributes
        "CAST",
        "DSCR",
        "EDUC",
        "IDNO",
        "NATI",
        "NCHI",
        "NMR",
        "OCCU",
        "PROP",
        "RELI",
        "RESI",
        "SSN",
        "TITL",
        "FACT",
        # Family events
        "MARS",
        "DIV",
        "DIVF",
        "ENGA",
        "MARR",
        "MARB",
        "MARC",
        "MARL",
    }
)


_PTR_RE = re.compile(r"^@[^@]+@$")


def _collect_url_values(
    el,
    include_events: bool,
    ptr_index: dict,
    _toplevel: bool = True,
    _visited: set | None = None,
) -> list[str]:
    """
    Collect all text values from el's subtree.
    - At top level of an INDI/FAM record, event subtrees are skipped unless include_events is set.
    - OBJE pointer references are always followed (they carry FILE URLs).
    - _visited prevents re-entering the same referenced record.
    """
    if _visited is None:
        _visited = set()

    values = []
    for ch in el.get_child_elements():
        tag = ch.get_tag()
        val = (ch.get_value() or "").strip()

        if _toplevel and tag in _EVENT_TAGS and not include_events:
            continue

        values.append(val)

        is_ptr = bool(_PTR_RE.match(val))
        if is_ptr and val not in _visited and tag == "OBJE":
            _visited.add(val)
            ref = ptr_index.get(val)
            if ref is not None:
                values.extend(
                    _collect_url_values(ref, True, ptr_index, False, _visited)
                )
        elif not is_ptr or tag not in (
            "OBJE",
            "FAMC",
            "FAMS",
            "HUSB",
            "WIFE",
            "CHIL",
            "SOUR",
            "REPO",
        ):
            values.extend(
                _collect_url_values(ch, include_events, ptr_index, False, _visited)
            )

    return values


def _matching_urls(
    el, url_lower: str, include_events: bool, ptr_index: dict
) -> list[str]:
    seen: set[str] = set()
    result = []
    for v in _collect_url_values(el, include_events, ptr_index):
        vl = v.lower()
        if (
            (vl.startswith("http://") or vl.startswith("https://"))
            and (url_lower == "" or url_lower in vl)
            and v not in seen
        ):
            seen.add(v)
            result.append(v)
    return result


def _has_url(el, url_lower: str, include_events: bool, ptr_index: dict) -> bool:
    return bool(_matching_urls(el, url_lower, include_events, ptr_index))


def _url_rows(
    root_elements,
    ptr_index,
    url_substr: str,
    include_events: bool,
    any_place: bool,
    ptr_filter: set | None = None,
) -> tuple[list, list]:
    url_lower = url_substr.lower()
    indi_rows = []
    fam_rows = []
    for el in root_elements:
        tag = el.get_tag()
        if tag == gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            if ptr_filter is not None and el.get_pointer().strip() not in ptr_filter:
                continue
            urls = _matching_urls(el, url_lower, include_events, ptr_index)
            if urls:
                given, surn = _get_name(el)
                birth, birth_place = _get_event(el, gedcom.tags.GEDCOM_TAG_BIRTH)
                death, _ = _get_event(el, gedcom.tags.GEDCOM_TAG_DEATH)
                if any_place and not birth_place:
                    birth_place = _get_place(
                        el, "CHR", "RESI", gedcom.tags.GEDCOM_TAG_DEATH
                    )
                indi_rows.append((given, surn, birth, death, birth_place, urls))
        elif tag == gedcom.tags.GEDCOM_TAG_FAMILY:
            urls = _matching_urls(el, url_lower, include_events, ptr_index)
            if urls:
                hg = hs = wg = ws = ""
                for ch in el.get_child_elements():
                    if ch.get_tag() == gedcom.tags.GEDCOM_TAG_HUSBAND:
                        indi = ptr_index.get(ch.get_value().strip())
                        if indi:
                            hg, hs = _get_name(indi)
                    elif ch.get_tag() == gedcom.tags.GEDCOM_TAG_WIFE:
                        indi = ptr_index.get(ch.get_value().strip())
                        if indi:
                            wg, ws = _get_name(indi)
                marr, marr_place = _get_marriage(el)
                fam_rows.append((hg, hs, wg, ws, marr, marr_place, urls))
    indi_rows.sort(key=lambda r: (_collation_key(r[1]), _collation_key(r[0])))
    fam_rows.sort(
        key=lambda r: (
            _collation_key(r[1]),
            _collation_key(r[0]),
            _collation_key(r[3]),
            _collation_key(r[2]),
        )
    )
    return indi_rows, fam_rows


def _collect_addr_values(el) -> list[str]:
    result = []
    for ch in el.get_child_elements():
        if ch.get_tag() == "ADDR":
            val = (ch.get_value() or "").strip()
            if val:
                result.append(val)
        result.extend(_collect_addr_values(ch))
    return result


def _matching_addrs(el, addr_lower: str) -> list[str]:
    seen: set[str] = set()
    result = []
    for v in _collect_addr_values(el):
        vl = v.lower()
        if (addr_lower == "" or addr_lower in vl) and v not in seen:
            seen.add(v)
            result.append(v)
    return result


def _addr_rows(
    root_elements,
    ptr_index,
    addr_substr: str,
    any_place: bool,
    ptr_filter: set | None = None,
) -> tuple[list, list]:
    addr_lower = addr_substr.lower()
    indi_rows = []
    fam_rows = []
    for el in root_elements:
        tag = el.get_tag()
        if tag == gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            if ptr_filter is not None and el.get_pointer().strip() not in ptr_filter:
                continue
            addrs = _matching_addrs(el, addr_lower)
            if addrs:
                given, surn = _get_name(el)
                birth, birth_place = _get_event(el, gedcom.tags.GEDCOM_TAG_BIRTH)
                death, _ = _get_event(el, gedcom.tags.GEDCOM_TAG_DEATH)
                if any_place and not birth_place:
                    birth_place = _get_place(
                        el, "CHR", "RESI", gedcom.tags.GEDCOM_TAG_DEATH
                    )
                indi_rows.append((given, surn, birth, death, birth_place, addrs))
        elif tag == gedcom.tags.GEDCOM_TAG_FAMILY:
            addrs = _matching_addrs(el, addr_lower)
            if addrs:
                hg = hs = wg = ws = ""
                for ch in el.get_child_elements():
                    if ch.get_tag() == gedcom.tags.GEDCOM_TAG_HUSBAND:
                        indi = ptr_index.get(ch.get_value().strip())
                        if indi:
                            hg, hs = _get_name(indi)
                    elif ch.get_tag() == gedcom.tags.GEDCOM_TAG_WIFE:
                        indi = ptr_index.get(ch.get_value().strip())
                        if indi:
                            wg, ws = _get_name(indi)
                marr, marr_place = _get_marriage(el)
                fam_rows.append((hg, hs, wg, ws, marr, marr_place, addrs))
    indi_rows.sort(key=lambda r: (_collation_key(r[1]), _collation_key(r[0])))
    fam_rows.sort(
        key=lambda r: (
            _collation_key(r[1]),
            _collation_key(r[0]),
            _collation_key(r[3]),
            _collation_key(r[2]),
        )
    )
    return indi_rows, fam_rows


def _get_obje_refs(el) -> list[str]:
    """Recursively collect all OBJE pointer values from el's subtree."""
    result = []
    for ch in el.get_child_elements():
        if ch.get_tag() == gedcom.tags.GEDCOM_TAG_OBJECT:
            val = (ch.get_value() or "").strip()
            if val.startswith("@") and val.endswith("@"):
                result.append(val)
        result.extend(_get_obje_refs(ch))
    return result


def _duplicate_url_rows(
    root_elements, ptr_index
) -> list[tuple[str, list[tuple[str, list]]]]:
    """Return [(url, [(obje_ptr, [row, ...]), ...]), ...] for URLs in multiple OBJE records."""
    url_to_objes: dict[str, list[str]] = {}
    url_display: dict[str, str] = {}
    for el in root_elements:
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_OBJECT:
            continue
        obje_ptr = el.get_pointer().strip()
        for ch in el.get_child_elements():
            if ch.get_tag() != "FILE":
                continue
            val = (ch.get_value() or "").strip()
            vl = val.lower()
            if not (vl.startswith("http://") or vl.startswith("https://")):
                continue
            url_to_objes.setdefault(vl, []).append(obje_ptr)
            url_display.setdefault(vl, val)

    obje_to_records: dict[str, list] = {}
    for el in root_elements:
        if el.get_tag() not in (
            gedcom.tags.GEDCOM_TAG_INDIVIDUAL,
            gedcom.tags.GEDCOM_TAG_FAMILY,
        ):
            continue
        for obje_ptr in _get_obje_refs(el):
            obje_to_records.setdefault(obje_ptr, []).append(el)

    result = []
    for url_lower, obje_ptrs in url_to_objes.items():
        if len(obje_ptrs) <= 1:
            continue
        url = url_display[url_lower]
        groups = []
        for obje_ptr in obje_ptrs:
            rows = []
            for record_el in obje_to_records.get(obje_ptr, []):
                if record_el.get_tag() == gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                    given, surn = _get_name(record_el)
                    birth, _ = _get_event(record_el, gedcom.tags.GEDCOM_TAG_BIRTH)
                    rows.append(("INDI", given, surn, birth))
                else:
                    hg = hs = wg = ws = ""
                    for ch in record_el.get_child_elements():
                        if ch.get_tag() == gedcom.tags.GEDCOM_TAG_HUSBAND:
                            indi = ptr_index.get(ch.get_value().strip())
                            if indi:
                                hg, hs = _get_name(indi)
                        elif ch.get_tag() == gedcom.tags.GEDCOM_TAG_WIFE:
                            indi = ptr_index.get(ch.get_value().strip())
                            if indi:
                                wg, ws = _get_name(indi)
                    rows.append(("FAM", hg, hs, wg, ws))
            groups.append((obje_ptr, rows))
        result.append((url, groups))
    result.sort(key=lambda r: r[0].lower())
    return result


_STAT_TAG_LABELS = [
    (gedcom.tags.GEDCOM_TAG_INDIVIDUAL, "Individuals"),
    (gedcom.tags.GEDCOM_TAG_FAMILY, "Families"),
    (gedcom.tags.GEDCOM_TAG_OBJECT, "Objects"),
    (gedcom.tags.GEDCOM_TAG_SOURCE, "Sources"),
    ("REPO", "Repositories"),
    ("NOTE", "Notes"),
    ("SUBM", "Submitters"),
]


def _collect_places(el, out: set) -> None:
    for ch in el.get_child_elements():
        if ch.get_tag() == gedcom.tags.GEDCOM_TAG_PLACE:
            val = (ch.get_value() or "").strip()
            if val:
                out.add(val)
        _collect_places(ch, out)


def _stat_rows(root_elements) -> list[tuple[str, int]]:
    tag_counts: dict[str, int] = {}
    places: set[str] = set()
    for el in root_elements:
        tag = el.get_tag()
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
        _collect_places(el, places)

    rows = [(label, tag_counts.get(tag, 0)) for tag, label in _STAT_TAG_LABELS]
    rows.append(("Places (unique)", len(places)))

    known = {t for t, _ in _STAT_TAG_LABELS} | {"HEAD", "TRLR"}
    extras = sorted((tag, n) for tag, n in tag_counts.items() if tag not in known)
    rows.extend(extras)
    return rows


def _family_rows(root_elements, ptr_index, sort_by_date: bool = False) -> list[tuple]:
    rows = []
    for el in root_elements:
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
            continue
        hg = hs = wg = ws = ""
        for ch in el.get_child_elements():
            if ch.get_tag() == gedcom.tags.GEDCOM_TAG_HUSBAND:
                indi = ptr_index.get(ch.get_value().strip())
                if indi:
                    hg, hs = _get_name(indi)
            elif ch.get_tag() == gedcom.tags.GEDCOM_TAG_WIFE:
                indi = ptr_index.get(ch.get_value().strip())
                if indi:
                    wg, ws = _get_name(indi)
        if sort_by_date:
            marr, marr_place = _get_marriage_date(el)
        else:
            marr, marr_place = _get_marriage(el)
        rows.append((hg, hs, wg, ws, marr, marr_place, el.get_pointer().strip()))
    if sort_by_date:
        # Sort by full marriage date (undated last), then by husband/wife names.
        rows.sort(
            key=lambda r: (
                _date_sort_key(r[4]),
                _collation_key(r[1]),
                _collation_key(r[0]),
                _collation_key(r[3]),
                _collation_key(r[2]),
            )
        )
    else:
        rows.sort(
            key=lambda r: (
                _collation_key(r[1]),
                _collation_key(r[0]),
                _collation_key(r[3]),
                _collation_key(r[2]),
            )
        )
    return rows


def software_query(input_paths: list[str], use_csv: bool) -> None:
    rows = []
    for path in input_paths:
        try:
            info = _extract_software(path)
        except Exception as e:
            print(f"WARNING: could not read '{path}': {e}", file=sys.stderr)
            info = {"sour": "", "name": "", "vers": "", "corp": ""}
        rows.append(
            (
                os.path.basename(path),
                info["sour"],
                info["name"],
                info["vers"],
                info["corp"],
            )
        )

    rows.sort(key=lambda r: _collation_key(r[0]))

    headers = ("File", "SOUR", "Name", "Version", "Corporation")
    if use_csv:
        out = csv.writer(sys.stdout)
        out.writerow(headers)
        for row in rows:
            out.writerow(row)
        return

    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row).rstrip())


def query_file(
    input_path: str,
    multi: bool,
    person_queries: list[str] | None,
    do_home_person: bool,
    do_ancestors: bool,
    do_descendants: bool,
    do_bloodline: bool,
    do_surnames: bool,
    do_location: bool,
    do_family: bool,
    url_pattern: str | None,
    search_events: bool,
    addr_pattern: str | None,
    do_duplicate_url: bool,
    do_stat: bool,
    use_csv: bool,
    any_place: bool,
    sort_by_date: bool,
    show_id: bool,
) -> None:
    parse_path, is_tmp = _transcode_to_utf8(input_path)
    try:
        parser = Parser()
        try:
            parser.parse_file(parse_path, strict=False)
        except Exception as e:
            print(f"ERROR: could not parse '{input_path}': {e}", file=sys.stderr)
            sys.exit(1)
    finally:
        if is_tmp:
            os.unlink(parse_path)

    root_elements = parser.get_root_child_elements()
    ptr_index = {
        el.get_pointer().strip(): el for el in root_elements if el.get_pointer()
    }

    out = csv.writer(sys.stdout) if use_csv else None
    first_section = True

    def _print_persons(rows):
        if use_csv:
            header = ["Name", "Surname", "Birth", "Death", "Place"]
            if multi:
                header.insert(0, "File")
            out.writerow((["ID"] + header) if show_id else header)
            for given, surn, birth, death, place, ptr in rows:
                fields = [given, surn, birth, death, place]
                if multi:
                    fields.insert(0, os.path.basename(input_path))
                out.writerow(([ptr] + fields) if show_id else fields)
        else:
            for given, surn, birth, death, place, ptr in rows:
                name = f"{given} {surn}".strip() or "?"
                parts = [ptr] if show_id else []
                parts.append(name)
                if birth:
                    parts.append(f"*{birth}")
                if death:
                    parts.append(f"+{death}")
                if place:
                    parts.append(place)
                print(" ".join(parts))

    # Build person pointer filter when specific persons are requested
    ptr_filter: set | None = None
    if person_queries is not None and len(person_queries) > 0:
        ptr_filter = _find_persons(person_queries, root_elements, ptr_index)
        if do_bloodline:
            ptr_filter = _collect_descendants(
                _collect_ancestors(ptr_filter, ptr_index), ptr_index
            )
        if do_ancestors:
            ptr_filter = _collect_ancestors(ptr_filter, ptr_index)
        if do_descendants:
            ptr_filter = _collect_descendants(ptr_filter, ptr_index)

    if do_home_person:
        home_ptrs = _find_home_persons(root_elements)
        if not home_ptrs:
            print("WARNING: no home person (_STP tag) found", file=sys.stderr)
        else:
            if not first_section and not use_csv:
                print()
            first_section = False
            rows = _person_rows(root_elements, any_place, home_ptrs, sort_by_date)
            _print_persons(rows)

    if do_surnames:
        first_section = False
        rows = _surname_rows(
            root_elements, any_place, ptr_filter, do_location, ptr_index
        )
        if use_csv:
            header = ["Surname", "Location"] if do_location else ["Surname"]
            if multi:
                header.insert(0, "File")
            out.writerow(header)
            for row in rows:
                fields = list(row) if do_location else [row]
                if multi:
                    fields.insert(0, os.path.basename(input_path))
                out.writerow(fields)
        else:
            for row in rows:
                print(f"{row[0]} {row[1]}".rstrip() if do_location else row)

    if (
        person_queries is not None
        and not do_surnames
        and url_pattern is None
        and addr_pattern is None
    ):
        if not first_section and not use_csv:
            print()
        first_section = False
        rows = _person_rows(root_elements, any_place, ptr_filter, sort_by_date)
        _print_persons(rows)

    if do_family:
        if not first_section and not use_csv:
            print()
        rows = _family_rows(root_elements, ptr_index, sort_by_date)
        if use_csv:
            header = [
                "Husband_Given",
                "Husband_Surname",
                "Wife_Given",
                "Wife_Surname",
                "Marriage",
                "Marriage_Place",
            ]
            if multi:
                header.insert(0, "File")
            out.writerow((["ID"] + header) if show_id else header)
            for hg, hs, wg, ws, marr, marr_place, ptr in rows:
                fields = [hg, hs, wg, ws, marr, marr_place]
                if multi:
                    fields.insert(0, os.path.basename(input_path))
                out.writerow(([ptr] + fields) if show_id else fields)
        else:
            for hg, hs, wg, ws, marr, marr_place, ptr in rows:
                husb = f"{hg} {hs}".strip() or "?"
                wife = f"{wg} {ws}".strip() or "?"
                line = f"{ptr} " if show_id else ""
                line += f"{husb} {wife}"
                if marr:
                    line += f" ⚭{marr}"
                if marr_place:
                    line += f" {marr_place}"
                print(line)

    if url_pattern is not None:
        indi_rows, fam_rows = _url_rows(
            root_elements,
            ptr_index,
            url_pattern,
            search_events,
            any_place,
            ptr_filter if person_queries is not None else None,
        )
        if indi_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                header = ["Name", "Surname", "Birth", "Death", "Place", "URLs"]
                if multi:
                    header.insert(0, "File")
                out.writerow(header)
                for given, surn, birth, death, place, urls in indi_rows:
                    fields = [given, surn, birth, death, place, " ".join(urls)]
                    if multi:
                        fields.insert(0, os.path.basename(input_path))
                    out.writerow(fields)
            else:
                for given, surn, birth, death, place, urls in indi_rows:
                    name = f"{given} {surn}".strip() or "?"
                    parts = [name]
                    if birth:
                        parts.append(f"*{birth}")
                    if death:
                        parts.append(f"+{death}")
                    if place:
                        parts.append(place)
                    print(" ".join(parts))
                    for url in urls:
                        print(f"  {url}")
        if fam_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                header = [
                    "Husband_Given",
                    "Husband_Surname",
                    "Wife_Given",
                    "Wife_Surname",
                    "Marriage",
                    "Marriage_Place",
                    "URLs",
                ]
                if multi:
                    header.insert(0, "File")
                out.writerow(header)
                for hg, hs, wg, ws, marr, marr_place, urls in fam_rows:
                    fields = [hg, hs, wg, ws, marr, marr_place, " ".join(urls)]
                    if multi:
                        fields.insert(0, os.path.basename(input_path))
                    out.writerow(fields)
            else:
                for hg, hs, wg, ws, marr, marr_place, urls in fam_rows:
                    husb = f"{hg} {hs}".strip() or "?"
                    wife = f"{wg} {ws}".strip() or "?"
                    line = f"{husb} {wife}"
                    if marr:
                        line += f" ⚭{marr}"
                    if marr_place:
                        line += f" {marr_place}"
                    print(line)
                    for url in urls:
                        print(f"  {url}")

    if addr_pattern is not None:
        indi_rows, fam_rows = _addr_rows(
            root_elements,
            ptr_index,
            addr_pattern,
            any_place,
            ptr_filter if person_queries is not None else None,
        )
        if indi_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                header = ["Name", "Surname", "Birth", "Death", "Place", "Addresses"]
                if multi:
                    header.insert(0, "File")
                out.writerow(header)
                for given, surn, birth, death, place, addrs in indi_rows:
                    fields = [given, surn, birth, death, place, " | ".join(addrs)]
                    if multi:
                        fields.insert(0, os.path.basename(input_path))
                    out.writerow(fields)
            else:
                for given, surn, birth, death, place, addrs in indi_rows:
                    name = f"{given} {surn}".strip() or "?"
                    parts = [name]
                    if birth:
                        parts.append(f"*{birth}")
                    if death:
                        parts.append(f"+{death}")
                    if place:
                        parts.append(place)
                    print(" ".join(parts))
        if fam_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                header = [
                    "Husband_Given",
                    "Husband_Surname",
                    "Wife_Given",
                    "Wife_Surname",
                    "Marriage",
                    "Marriage_Place",
                    "Addresses",
                ]
                if multi:
                    header.insert(0, "File")
                out.writerow(header)
                for hg, hs, wg, ws, marr, marr_place, addrs in fam_rows:
                    fields = [hg, hs, wg, ws, marr, marr_place, " | ".join(addrs)]
                    if multi:
                        fields.insert(0, os.path.basename(input_path))
                    out.writerow(fields)
            else:
                for hg, hs, wg, ws, marr, marr_place, addrs in fam_rows:
                    husb = f"{hg} {hs}".strip() or "?"
                    wife = f"{wg} {ws}".strip() or "?"
                    line = f"{husb} {wife}"
                    if marr:
                        line += f" ⚭{marr}"
                    if marr_place:
                        line += f" {marr_place}"
                    print(line)

    if do_stat:
        if not first_section and not use_csv:
            print()
        first_section = False
        rows = _stat_rows(root_elements)
        if use_csv:
            header = ["Type", "Count"]
            if multi:
                header.insert(0, "File")
            out.writerow(header)
            for label, n in rows:
                out.writerow(
                    [os.path.basename(input_path), label, n] if multi else [label, n]
                )
        else:
            width = max(len(label) for label, _ in rows)
            for label, n in rows:
                print(f"{label:<{width}}  {n}")

    if do_duplicate_url:
        dup_rows = _duplicate_url_rows(root_elements, ptr_index)
        if dup_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                header = ["URL", "OBJE", "Name", "Surname", "Birth"]
                if multi:
                    header.insert(0, "File")
                out.writerow(header)
                for url, groups in dup_rows:
                    for obje_ptr, rows in groups:
                        for row in rows:
                            if row[0] == "INDI":
                                _, given, surn, birth = row
                                fields = [url, obje_ptr, given, surn, birth]
                                if multi:
                                    fields.insert(0, os.path.basename(input_path))
                                out.writerow(fields)
                            else:
                                _, hg, hs, wg, ws = row
                                fields = [
                                    url,
                                    obje_ptr,
                                    f"{hg} {hs}".strip(),
                                    f"{wg} {ws}".strip(),
                                    "",
                                ]
                                if multi:
                                    fields.insert(0, os.path.basename(input_path))
                                out.writerow(fields)
            else:
                for url, groups in dup_rows:
                    print(url)
                    for obje_ptr, rows in groups:
                        print(f"  {obje_ptr}")
                        for row in rows:
                            if row[0] == "INDI":
                                _, given, surn, birth = row
                                name = f"{given} {surn}".strip() or "?"
                                print(f"    {name}" + (f" *{birth}" if birth else ""))
                            else:
                                _, hg, hs, wg, ws = row
                                husb = f"{hg} {hs}".strip() or "?"
                                wife = f"{wg} {ws}".strip() or "?"
                                print(f"    {husb} ⚭ {wife}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    arg_parser = argparse.ArgumentParser(
        description="Query individuals and families from a GEDCOM file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    arg_parser.add_argument(
        "input", nargs="+", help="Input GEDCOM file(s) (.ged); multiple allowed"
    )
    arg_parser.add_argument(
        "--person",
        nargs="*",
        metavar="PERSON",
        help="List individuals; optionally filter by name or pointer",
    )
    arg_parser.add_argument(
        "--ancestors",
        action="store_true",
        help="With --person names: also include all ancestors",
    )
    arg_parser.add_argument(
        "--descendants",
        action="store_true",
        help="With --person names: also include all descendants",
    )
    arg_parser.add_argument(
        "--bloodline",
        action="store_true",
        help="With --person names: include all blood relatives (descendants of all ancestors)",
    )
    arg_parser.add_argument(
        "--surnames",
        action="store_true",
        help="Output unique surnames instead of full person rows",
    )
    arg_parser.add_argument(
        "--location",
        action="store_true",
        help="With --surnames: output the place of the oldest occurrence of each surname",
    )
    arg_parser.add_argument("--family", action="store_true", help="List all families")
    arg_parser.add_argument(
        "--home-person",
        action="store_true",
        dest="home_person",
        help="Print the home person (indicated by _STP tag in MacFamilyTree)",
    )
    arg_parser.add_argument(
        "--url",
        nargs="?",
        const="",
        metavar="URL",
        help="List INDI and FAM records containing this URL substring (case-insensitive); omit value to match any URL",
    )
    arg_parser.add_argument(
        "--search-events",
        action="store_true",
        dest="search_events",
        help="With --url: also search within event subtrees",
    )

    arg_parser.add_argument(
        "--addr",
        nargs="?",
        const="",
        metavar="ADDR",
        help="List INDI and FAM records with a matching ADDR value (case-insensitive substring); omit value to match any address",
    )
    arg_parser.add_argument(
        "--duplicate-url",
        action="store_true",
        dest="duplicate_url",
        help="List URLs that appear in more than one media (OBJE) record",
    )
    arg_parser.add_argument(
        "--stat",
        action="store_true",
        help="Show counts of top-level GEDCOM records and unique places",
    )
    arg_parser.add_argument(
        "--sw",
        action="store_true",
        help="Show the software that generated each GEDCOM (from HEAD/SOUR)",
    )
    arg_parser.add_argument(
        "--sort-date",
        action="store_true",
        dest="sort_date",
        help="Sort --person by birth year and --family by marriage year (undated last)",
    )
    arg_parser.add_argument(
        "--id",
        action="store_true",
        dest="show_id",
        help="Prepend the GEDCOM pointer (@I..@/@F..@) to --person/--family rows",
    )
    arg_parser.add_argument("--csv", action="store_true", help="Output as CSV")
    arg_parser.add_argument(
        "--any-place",
        action="store_true",
        dest="any_place",
        help="Fall back to baptism, residence, or death place when birth place is absent",
    )

    args = arg_parser.parse_args()

    if isinstance(args.input, list):
        args.input = [unicodedata.normalize("NFC", p) for p in args.input]

    if (
        args.person is None
        and not args.surnames
        and not args.family
        and args.url is None
        and args.addr is None
        and not args.duplicate_url
        and not args.stat
        and not args.sw
        and not args.home_person
    ):
        arg_parser.error(
            "at least one of --person, --surnames, --family, --home-person, --url, --addr, --duplicate-url, --stat, or --sw must be specified"
        )
    if (args.ancestors or args.descendants or args.bloodline) and not (
        args.person and len(args.person) > 0
    ):
        arg_parser.error(
            "--ancestors/--descendants/--bloodline require --person with at least one name"
        )
    if args.location and not args.surnames:
        arg_parser.error("--location requires --surnames")
    if args.search_events and args.url is None:
        arg_parser.error("--search-events requires --url")
    if args.sort_date and not (
        args.person is not None or args.family or args.home_person
    ):
        arg_parser.error("--sort-date requires --person, --home-person, or --family")
    if args.show_id and not (
        args.person is not None or args.family or args.home_person
    ):
        arg_parser.error("--id requires --person, --home-person, or --family")

    if args.sw:
        software_query(args.input, args.csv)
        return

    multi = len(args.input) > 1
    for i, input_path in enumerate(args.input):
        if multi and not args.csv:
            if i > 0:
                print()
            print(f"=== {input_path} ===")
        query_file(
            input_path,
            multi,
            args.person,
            args.home_person,
            args.ancestors,
            args.descendants,
            args.bloodline,
            args.surnames,
            args.location,
            args.family,
            args.url,
            args.search_events,
            args.addr,
            args.duplicate_url,
            args.stat,
            args.csv,
            args.any_place,
            args.sort_date,
            args.show_id,
        )


if __name__ == "__main__":
    main()
