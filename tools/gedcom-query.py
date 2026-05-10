#!/usr/bin/env python3
"""
gedcom-query: Query a GEDCOM file for individuals or families.

Usage:
    python tools/gedcom-query.py <input.ged> [OPTIONS]

Options:
    --person [PERSON ...]
                  List individuals. Without names: all individuals.
                  With names: only the listed persons (matched by pointer,
                  "Given Surname", or "Given Surname YYYY").
    --ancestors   With --person names: also include all ancestors.
    --descendants With --person names: also include all descendants.
    --surnames    Output unique surnames instead of full person rows.
    --location    With --surnames: also output the place of the oldest
                  (earliest birth year) occurrence of each surname.
    --family      List all families: Husband Wife ⚭yyyy Place
    --url [URL]   List INDI and FAM records whose subtree contains URL as a
                  substring (case-insensitive). Omit value to match any URL.
                  By default only direct (non-event) children are searched.
    --search-events   With --url: also search within event subtrees.
    --addr [ADDR] List INDI and FAM records whose ADDR subtree contains ADDR as
                  a substring (case-insensitive). Omit value to match any address.
    --duplicate-url
                  List all URLs that appear in more than one media (OBJE) record,
                  with the persons/families referencing each duplicate.
    --csv         Output as CSV
    --any-place   For --person/--surnames: fall back to baptism, residence, or
                  death place when birth place is absent (checked in that order)

At least one of --person, --surnames, --family, --url, --addr, or --duplicate-url must be specified.

Examples:
    python tools/gedcom-query.py family.ged --person
    python tools/gedcom-query.py family.ged --person "Luka Renko"
    python tools/gedcom-query.py family.ged --person "Franc Renko 1901" --ancestors
    python tools/gedcom-query.py family.ged --person "@I1@" --descendants
    python tools/gedcom-query.py family.ged --surnames
    python tools/gedcom-query.py family.ged --surnames --location
    python tools/gedcom-query.py family.ged --person "Luka Renko" --ancestors --surnames --location --any-place
    python tools/gedcom-query.py family.ged --family
    python tools/gedcom-query.py family.ged --person --any-place
    python tools/gedcom-query.py family.ged --person --csv > persons.csv
    python tools/gedcom-query.py family.ged --family --csv > families.csv
    python tools/gedcom-query.py family.ged --url
    python tools/gedcom-query.py family.ged --url familysearch.org
    python tools/gedcom-query.py family.ged --url matricula-online.com --search-events
    python tools/gedcom-query.py family.ged --addr "Sušica 47"
    python tools/gedcom-query.py family.ged --person "Jakob Renka 1764" --descendants --addr
    python tools/gedcom-query.py family.ged --duplicate-url
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
    'č': 'c\x7d', 'ć': 'c\x7e',
    'đ': 'd\x7f',
    'š': 's\x7f',
    'ž': 'z\x7f',
}


def _collation_key(s: str) -> str:
    result = []
    for ch in s.casefold():
        mapped = _COLLATION_SPECIAL.get(ch)
        if mapped:
            result.append(mapped)
        else:
            result.append(unicodedata.normalize('NFD', ch)[0])
    return ''.join(result)


# ---------------------------------------------------------------------------
# Encoding detection & transcoding  (mirrors gedcom-filter.py)
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
            if enc.lower() in ("windows-1252", "cp1252", "iso-8859-1", "iso-8859-2", "utf-8"):
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
        if _is_disguised_cp1250(raw):
            encoding = "windows-1250"
        else:
            try:
                raw.decode(encoding)
                return input_path, False
            except UnicodeDecodeError:
                test_decode = raw.decode(encoding, errors="replace")
                if test_decode.count("�") < max(10, len(raw) // 1000):
                    fd, tmp_path = tempfile.mkstemp(suffix=".ged")
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(test_decode)
                    return tmp_path, True
                detected = chardet.detect(raw)
                enc = (detected.get("encoding") or "") if detected else ""
                confidence = (detected.get("confidence") or 0) if detected else 0
                if enc and confidence >= 0.2 and enc.lower() not in ("mac_roman", "ascii"):
                    encoding = enc
                else:
                    encoding = "windows-1250"
                if encoding.lower() in ("windows-1252", "cp1252", "iso-8859-1", "iso-8859-2", "utf-8"):
                    if _is_disguised_cp1250(raw):
                        encoding = "windows-1250"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        text = raw.decode("latin-1", errors="replace")
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
            (sc.get_value().strip().lower()
             for sc in ch.get_child_elements()
             if sc.get_tag() == "TYPE"),
            None,
        )
        if name_type and name_type != "birth":
            if fallback is None:
                fallback = ch.get_value()
            continue
        return _parse_name(ch.get_value())
    return _parse_name(fallback) if fallback else ("", "")


def _get_event(indi_el, event_tag: str) -> tuple[str, str]:
    """Return (year, place) of the first occurrence of event_tag."""
    for ch in indi_el.get_child_elements():
        if ch.get_tag() != event_tag:
            continue
        year = place = ""
        for subch in ch.get_child_elements():
            t = subch.get_tag()
            if t == gedcom.tags.GEDCOM_TAG_DATE:
                year = _extract_year(subch.get_value())
            elif t == gedcom.tags.GEDCOM_TAG_PLACE:
                place = subch.get_value().strip()
        return year, place
    return "", ""


def _get_marriage(fam_el) -> tuple[str, str]:
    """Return (year, place) of the marriage event of a FAM element."""
    for ch in fam_el.get_child_elements():
        if ch.get_tag() != gedcom.tags.GEDCOM_TAG_MARRIAGE:
            continue
        year = place = ""
        for subch in ch.get_child_elements():
            t = subch.get_tag()
            if t == gedcom.tags.GEDCOM_TAG_DATE:
                year = _extract_year(subch.get_value())
            elif t == gedcom.tags.GEDCOM_TAG_PLACE:
                place = subch.get_value().strip()
        return year, place
    return "", ""


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
                if fch.get_tag() not in (gedcom.tags.GEDCOM_TAG_HUSBAND, gedcom.tags.GEDCOM_TAG_WIFE):
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
                    if fch.get_tag() not in (gedcom.tags.GEDCOM_TAG_HUSBAND, gedcom.tags.GEDCOM_TAG_WIFE):
                        continue
                    parent = ptr_index.get(fch.get_value().strip())
                    if parent is None:
                        continue
                    _, psurn = _get_name(parent)
                    if psurn.casefold() == surn.casefold():
                        next_level.append(parent)
        queue = next_level
    return ""


def _surname_rows(root_elements, any_place: bool, ptr_filter: set | None, with_location: bool, ptr_index=None) -> list:
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


def _person_rows(root_elements, any_place: bool, ptr_filter: set | None = None) -> list[tuple]:
    rows = []
    for el in root_elements:
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue
        if ptr_filter is not None and el.get_pointer().strip() not in ptr_filter:
            continue
        given, surn = _get_name(el)
        birth, birth_place = _get_event(el, gedcom.tags.GEDCOM_TAG_BIRTH)
        death, _ = _get_event(el, gedcom.tags.GEDCOM_TAG_DEATH)
        if any_place and not birth_place:
            birth_place = _get_place(el, "CHR", "RESI", gedcom.tags.GEDCOM_TAG_DEATH)
        rows.append((given, surn, birth, death, birth_place))
    rows.sort(key=lambda r: (_collation_key(r[1]), _collation_key(r[0])))
    return rows


_EVENT_TAGS = frozenset({
    # Individual events
    "BIRT", "CHR", "DEAT", "BURI", "CREM", "ADOP", "BAPM", "BARM", "BASM",
    "BLES", "CHRA", "CONF", "FCOM", "ORDN", "NATU", "EMIG", "IMMI", "CENS",
    "PROB", "WILL", "GRAD", "RETI", "EVEN",
    # Individual attributes
    "CAST", "DSCR", "EDUC", "IDNO", "NATI", "NCHI", "NMR", "OCCU", "PROP",
    "RELI", "RESI", "SSN", "TITL", "FACT",
    # Family events
    "MARS", "DIV", "DIVF", "ENGA", "MARR", "MARB", "MARC", "MARL",
})


_PTR_RE = re.compile(r'^@[^@]+@$')


def _collect_url_values(el, include_events: bool, ptr_index: dict,
                        _toplevel: bool = True, _visited: set | None = None) -> list[str]:
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
                values.extend(_collect_url_values(ref, True, ptr_index, False, _visited))
        elif not is_ptr or tag not in ("OBJE", "FAMC", "FAMS", "HUSB", "WIFE", "CHIL", "SOUR", "REPO"):
            values.extend(_collect_url_values(ch, include_events, ptr_index, False, _visited))

    return values


def _matching_urls(el, url_lower: str, include_events: bool, ptr_index: dict) -> list[str]:
    seen: set[str] = set()
    result = []
    for v in _collect_url_values(el, include_events, ptr_index):
        vl = v.lower()
        if (vl.startswith("http://") or vl.startswith("https://")) and (url_lower == "" or url_lower in vl) and v not in seen:
            seen.add(v)
            result.append(v)
    return result


def _has_url(el, url_lower: str, include_events: bool, ptr_index: dict) -> bool:
    return bool(_matching_urls(el, url_lower, include_events, ptr_index))


def _url_rows(root_elements, ptr_index, url_substr: str, include_events: bool, any_place: bool,
              ptr_filter: set | None = None) -> tuple[list, list]:
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
                    birth_place = _get_place(el, "CHR", "RESI", gedcom.tags.GEDCOM_TAG_DEATH)
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
    fam_rows.sort(key=lambda r: (_collation_key(r[1]), _collation_key(r[0]), _collation_key(r[3]), _collation_key(r[2])))
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


def _addr_rows(root_elements, ptr_index, addr_substr: str, any_place: bool,
               ptr_filter: set | None = None) -> tuple[list, list]:
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
                    birth_place = _get_place(el, "CHR", "RESI", gedcom.tags.GEDCOM_TAG_DEATH)
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
    fam_rows.sort(key=lambda r: (_collation_key(r[1]), _collation_key(r[0]), _collation_key(r[3]), _collation_key(r[2])))
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


def _duplicate_url_rows(root_elements, ptr_index) -> list[tuple[str, list[tuple[str, list]]]]:
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
        if el.get_tag() not in (gedcom.tags.GEDCOM_TAG_INDIVIDUAL, gedcom.tags.GEDCOM_TAG_FAMILY):
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


def _family_rows(root_elements, ptr_index) -> list[tuple]:
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
        marr, marr_place = _get_marriage(el)
        rows.append((hg, hs, wg, ws, marr, marr_place))
    rows.sort(key=lambda r: (_collation_key(r[1]), _collation_key(r[0]), _collation_key(r[3]), _collation_key(r[2])))
    return rows


def query_file(
    input_path: str,
    person_queries: list[str] | None,
    do_ancestors: bool,
    do_descendants: bool,
    do_surnames: bool,
    do_location: bool,
    do_family: bool,
    url_pattern: str | None,
    search_events: bool,
    addr_pattern: str | None,
    do_duplicate_url: bool,
    use_csv: bool,
    any_place: bool,
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
        el.get_pointer().strip(): el
        for el in root_elements
        if el.get_pointer()
    }

    out = csv.writer(sys.stdout) if use_csv else None
    first_section = True

    # Build person pointer filter when specific persons are requested
    ptr_filter: set | None = None
    if person_queries is not None and len(person_queries) > 0:
        ptr_filter = _find_persons(person_queries, root_elements, ptr_index)
        if do_ancestors:
            ptr_filter = _collect_ancestors(ptr_filter, ptr_index)
        if do_descendants:
            ptr_filter = _collect_descendants(ptr_filter, ptr_index)

    if do_surnames:
        first_section = False
        rows = _surname_rows(root_elements, any_place, ptr_filter, do_location, ptr_index)
        if use_csv:
            out.writerow(["Surname", "Location"] if do_location else ["Surname"])
            for row in rows:
                out.writerow(list(row) if do_location else [row])
        else:
            for row in rows:
                print(f"{row[0]} {row[1]}".rstrip() if do_location else row)

    if person_queries is not None and not do_surnames and url_pattern is None and addr_pattern is None:
        first_section = False
        rows = _person_rows(root_elements, any_place, ptr_filter)
        if use_csv:
            out.writerow(["Name", "Surname", "Birth", "Death", "Place"])
            for row in rows:
                out.writerow(row)
        else:
            for given, surn, birth, death, place in rows:
                name = f"{given} {surn}".strip() or "?"
                parts = [name]
                if birth:
                    parts.append(f"*{birth}")
                if death:
                    parts.append(f"+{death}")
                if place:
                    parts.append(place)
                print(" ".join(parts))

    if do_family:
        if not first_section and not use_csv:
            print()
        rows = _family_rows(root_elements, ptr_index)
        if use_csv:
            out.writerow(["Husband_Given", "Husband_Surname", "Wife_Given", "Wife_Surname", "Marriage", "Marriage_Place"])
            for row in rows:
                out.writerow(row)
        else:
            for hg, hs, wg, ws, marr, marr_place in rows:
                husb = f"{hg} {hs}".strip() or "?"
                wife = f"{wg} {ws}".strip() or "?"
                line = f"{husb} {wife}"
                if marr:
                    line += f" ⚭{marr}"
                if marr_place:
                    line += f" {marr_place}"
                print(line)

    if url_pattern is not None:
        indi_rows, fam_rows = _url_rows(root_elements, ptr_index, url_pattern, search_events, any_place,
                                         ptr_filter if person_queries is not None else None)
        if indi_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                out.writerow(["Name", "Surname", "Birth", "Death", "Place", "URLs"])
                for given, surn, birth, death, place, urls in indi_rows:
                    out.writerow([given, surn, birth, death, place, " ".join(urls)])
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
                out.writerow(["Husband_Given", "Husband_Surname", "Wife_Given", "Wife_Surname", "Marriage", "Marriage_Place", "URLs"])
                for hg, hs, wg, ws, marr, marr_place, urls in fam_rows:
                    out.writerow([hg, hs, wg, ws, marr, marr_place, " ".join(urls)])
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
        indi_rows, fam_rows = _addr_rows(root_elements, ptr_index, addr_pattern, any_place,
                                          ptr_filter if person_queries is not None else None)
        if indi_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                out.writerow(["Name", "Surname", "Birth", "Death", "Place", "Addresses"])
                for given, surn, birth, death, place, addrs in indi_rows:
                    out.writerow([given, surn, birth, death, place, " | ".join(addrs)])
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
                out.writerow(["Husband_Given", "Husband_Surname", "Wife_Given", "Wife_Surname", "Marriage", "Marriage_Place", "Addresses"])
                for hg, hs, wg, ws, marr, marr_place, addrs in fam_rows:
                    out.writerow([hg, hs, wg, ws, marr, marr_place, " | ".join(addrs)])
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

    if do_duplicate_url:
        dup_rows = _duplicate_url_rows(root_elements, ptr_index)
        if dup_rows:
            if not first_section and not use_csv:
                print()
            first_section = False
            if use_csv:
                out.writerow(["URL", "OBJE", "Name", "Surname", "Birth"])
                for url, groups in dup_rows:
                    for obje_ptr, rows in groups:
                        for row in rows:
                            if row[0] == "INDI":
                                _, given, surn, birth = row
                                out.writerow([url, obje_ptr, given, surn, birth])
                            else:
                                _, hg, hs, wg, ws = row
                                out.writerow([url, obje_ptr, f"{hg} {hs}".strip(), f"{wg} {ws}".strip(), ""])
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
    arg_parser.add_argument("input", help="Input GEDCOM file (.ged)")
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
    arg_parser.add_argument("--csv", action="store_true", help="Output as CSV")
    arg_parser.add_argument(
        "--any-place",
        action="store_true",
        dest="any_place",
        help="Fall back to baptism, residence, or death place when birth place is absent",
    )

    args = arg_parser.parse_args()

    if args.person is None and not args.surnames and not args.family and args.url is None and args.addr is None and not args.duplicate_url:
        arg_parser.error("at least one of --person, --surnames, --family, --url, --addr, or --duplicate-url must be specified")
    if (args.ancestors or args.descendants) and not (args.person and len(args.person) > 0):
        arg_parser.error("--ancestors/--descendants require --person with at least one name")
    if args.location and not args.surnames:
        arg_parser.error("--location requires --surnames")
    if args.search_events and args.url is None:
        arg_parser.error("--search-events requires --url")

    query_file(args.input, args.person, args.ancestors, args.descendants,
               args.surnames, args.location, args.family, args.url,
               args.search_events, args.addr, args.duplicate_url, args.csv, args.any_place)


if __name__ == "__main__":
    main()
