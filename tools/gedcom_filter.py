#!/usr/bin/env python3
"""
gedcom_filter: Filter a GEDCOM file to keep only selected individuals and their relationships.

Usage:
    python tools/gedcom_filter.py <input.ged> <output.ged> --ancestors|--descendants --person PERSON [OPTIONS]

Options:
    --person PERSON [PERSON ...]
                           One or more root persons. Each PERSON can be:
                             - A GEDCOM pointer  (e.g. @I123@ or I123)
                             - A full or partial name (e.g. "Luka Renko" or "Renko")
                             - A name with birth year (e.g. "Franc Renko 1901")
                           Results are unioned across all specified persons.
    --ancestors            Keep direct ancestors of the root person
                           (parents, grandparents, … all the way up) plus the
                           family records that connect them.
    --descendants          Keep all descendants of the root person
                           (children, grandchildren, …) plus the family records
                           that connect them. Can be combined with --ancestors.
    --bloodline            Keep all blood relatives — equivalent to
                           --ancestors --descendants, which collects every
                           descendant of every ancestor (siblings, cousins,
                           aunts/uncles, etc.).
    --partners             Also keep the partner (spouse) of every kept person,
                           plus the marriage family. Adds spouses that would
                           otherwise be missing — most useful with --ancestors
                           alone, where FAMS branches are not walked.
    --related              Also keep all descendants of every ancestor already
                           included (use with --ancestors). This pulls in
                           cousins, aunts/uncles, and all their descendants.
    --related-depth N      How many marriage hops --related should chase.
                           Each in-law added by --related is always run back
                           through --ancestors / --siblings (so their full
                           blood line is included). N controls only how many
                           marriage hops --related itself performs:
                             1 (default) = one --related fan-out
                             2 = two fan-outs (in-laws of in-laws)
                             0 = run until fixed point (full transitive
                                 closure — can pull in most of an
                                 interconnected GEDCOM).
    --siblings             Also keep all siblings of every included person
                           (i.e. all children in every kept parent family).
    --living-private       Redact all living individuals: replace name with
                           "<private>" and remove all event data.
    --living-name          Redact all living individuals: keep full name but
                           remove all event data.
    --living-initials      Redact all living individuals: reduce name to
                           initials and remove all event data.
    --verbose              Print each kept/removed record.

Living detection: an individual is considered living when they have no DEAT,
BURI, or CREM record.

Examples:
    # Ancestors only, by pointer
    python tools/gedcom_filter.py family.ged ancestors.ged --ancestors --person @I123@

    # Descendants only, by full name
    python tools/gedcom_filter.py family.ged descendants.ged --descendants --person "Luka Renko"

    # Both ancestors and descendants (full hourglass tree)
    python tools/gedcom_filter.py family.ged hourglass.ged --ancestors --descendants --person @I123@

    # Ancestors with their siblings, birth year inline
    python tools/gedcom_filter.py family.ged ancestors.ged --ancestors --siblings --person "Renko 1952"

    # All blood relatives reachable through the ancestor tree
    python tools/gedcom_filter.py family.ged related.ged --ancestors --related --person @I123@

    # All blood relatives (descendants of every ancestor)
    python tools/gedcom_filter.py family.ged bloodline.ged --bloodline --person @I123@

    # Ancestors plus their spouses
    python tools/gedcom_filter.py family.ged ancestors.ged --ancestors --partners --person @I123@

    # Multiple root persons — union of all their ancestors
    python tools/gedcom_filter.py family.ged out.ged --ancestors --person @I123@ @I456@
    python tools/gedcom_filter.py family.ged out.ged --ancestors --person "Luka Renko" "Ana Kovač"

    # Descendants with living people shown as initials only
    python tools/gedcom_filter.py family.ged out.ged --descendants --living-initials --person @I123@
"""

import argparse
import os
import re
import sys
import tempfile
import unicodedata

import chardet
from gedcom.element.element import Element
from gedcom.parser import Parser
import gedcom.tags

# ---------------------------------------------------------------------------
# Encoding detection & transcoding  (mirrors gedcom_cleaner.py)
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
                if test_decode.count("\ufffd") < max(10, len(raw) // 1000):
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
# Serialization  (mirrors gedcom_cleaner.py)
# ---------------------------------------------------------------------------


def _serialize(element) -> str:
    """Recursively serialize an element and all its descendants."""
    level = element.get_level()
    if level < 0:
        result = ""
    else:
        pointer = element.get_pointer() or ""
        tag = element.get_tag()
        value = element.get_value()
        line = str(level)
        if pointer:
            line += " " + pointer
        line += " " + tag
        if value:
            line += " " + value
        line += "\n"
        result = line
    for child in element.get_child_elements():
        result += _serialize(child)
    return result


def _update_char_tag(parser: Parser) -> None:
    for element in parser.get_element_list():
        if element.get_tag() == "CHAR":
            element.set_value("UTF-8")
            return


# ---------------------------------------------------------------------------
# Individual helpers
# ---------------------------------------------------------------------------


def _indi_label(indi_el) -> str:
    """Return 'FirstName Surname (b.YYYY d.YYYY)' for an INDI element."""
    name = ""
    birth = ""
    death = ""
    for ch in indi_el.get_child_elements():
        tag = ch.get_tag()
        if not name and tag == gedcom.tags.GEDCOM_TAG_NAME:
            parts = ch.get_value().replace("/", " ").split()
            name = " ".join(parts)
        elif not birth and tag == gedcom.tags.GEDCOM_TAG_BIRTH:
            for gch in ch.get_child_elements():
                if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                    m = re.search(r"\b\d{3,4}\b", gch.get_value().strip())
                    if m:
                        birth = m.group()
                    break
        elif not death and tag in ("DEAT", "BURI", "CREM"):
            for gch in ch.get_child_elements():
                if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                    m = re.search(r"\b\d{3,4}\b", gch.get_value().strip())
                    if m:
                        death = m.group()
                    break
    label = name or "?"
    dates = " ".join(
        filter(
            None,
            [
                f"b.{birth}" if birth else "",
                f"d.{death}" if death else "",
            ],
        )
    )
    if dates:
        label += f" ({dates})"
    return label


def _indi_birth_year(indi_el) -> int | None:
    """Extract the best birth year from an INDI element, or None."""
    for ch in indi_el.get_child_elements():
        if ch.get_tag() == gedcom.tags.GEDCOM_TAG_BIRTH:
            for gch in ch.get_child_elements():
                if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                    m = re.search(r"\b(\d{3,4})\b", gch.get_value())
                    if m:
                        return int(m.group(1))
    return None


def _name_parts(raw_name: str) -> tuple[str, str]:
    """Return (given_names_lower, surname_lower) from a GEDCOM NAME value like 'Luka /Renko/'."""
    surname_match = re.search(r"/([^/]*)/", raw_name)
    surname = surname_match.group(1).strip().lower() if surname_match else ""
    given = re.sub(r"/[^/]*/", "", raw_name).strip().lower()
    return given, surname


# ---------------------------------------------------------------------------
# Person lookup
# ---------------------------------------------------------------------------


def _find_person(
    root_elements: list,
    person_spec: str,
) -> str:
    """
    Return the GEDCOM pointer of the matching individual.

    person_spec may be:
      - A pointer: '@I123@' or 'I123'
      - A full or partial name: 'Luka Renko', 'Renko', 'Luka'
      - A name with birth year: 'Franc Renko 1901'

    Exits with an error message if 0 or 2+ ambiguous matches are found.
    """
    # Extract birth year from the last word if it looks like a year (1000–2029).
    birth_year: int | None = None
    parts = person_spec.strip().split()
    if parts and re.match(r"^(1[0-9]{3}|20[0-2][0-9])$", parts[-1]):
        birth_year = int(parts[-1])
        person_spec = " ".join(parts[:-1])

    # --- pointer lookup ---
    ptr_spec = person_spec.strip()
    if not ptr_spec.startswith("@"):
        ptr_spec = f"@{ptr_spec}@"
    ptr_spec = ptr_spec.upper()

    for el in root_elements:
        if el.get_tag() == gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            ptr = (el.get_pointer() or "").strip().upper()
            if ptr == ptr_spec:
                return el.get_pointer().strip()

    # --- name lookup ---
    query = person_spec.strip().lower()
    # Split query into words; match them against given + surname
    query_words = query.split()

    matches = []
    for el in root_elements:
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue
        for ch in el.get_child_elements():
            if ch.get_tag() != gedcom.tags.GEDCOM_TAG_NAME:
                continue
            given, surname = _name_parts(ch.get_value())
            full = (given + " " + surname).strip()
            # All query words must appear in the combined name
            if all(w in full for w in query_words):
                if birth_year is not None:
                    by = _indi_birth_year(el)
                    if by != birth_year:
                        continue
                matches.append(el)
            break  # only check first NAME tag per individual

    if len(matches) == 1:
        return matches[0].get_pointer().strip()

    if len(matches) == 0:
        print(
            f"ERROR: no individual found matching '{person_spec}'"
            + (f" with birth year {birth_year}" if birth_year else ""),
            file=sys.stderr,
        )
        sys.exit(1)

    # Multiple matches — print them all and ask for a more specific query
    print(
        f"ERROR: '{person_spec}' matches {len(matches)} individuals"
        + (f" (birth year {birth_year})" if birth_year else "")
        + ". Use --person with a pointer to select one:",
        file=sys.stderr,
    )
    for el in matches:
        ptr = el.get_pointer().strip()
        print(f"  {ptr}  {_indi_label(el)}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Living privacy
# ---------------------------------------------------------------------------


def _is_living(indi_el) -> bool:
    """Return True if the individual has no DEAT, BURI, or CREM record."""
    for ch in indi_el.get_child_elements():
        if ch.get_tag() in ("DEAT", "BURI", "CREM"):
            return False
    return True


def _initials(name: str) -> str:
    """'Luka Renko' → 'L. R.'"""
    return " ".join(w[0].upper() + "." for w in name.split() if w)


def _shorten_name_value(val: str) -> str:
    """Convert given and surname parts to initials, preserving the /surname/ delimiters.

    'Luka /Renko/'         -> 'L. /R./'
    'Ronja Sofija /Renko/' -> 'R. S. /R./'
    'Luka'                 -> 'L.'
    """
    m = re.match(r"^(.*?)(/[^/]*/)(.*?)$", val.strip())
    if m:
        given = _initials(m.group(1).strip())
        surn = _initials(m.group(2).strip("/ "))
        parts = [p for p in [given, f"/{surn}/"] if p]
        return " ".join(parts)
    return _initials(val)


def _apply_living_privacy(indi_el, mode: str, verbose: bool) -> None:
    """Redact a living individual's record in-place.

    mode:
      '<private>'  — set NAME to "<private>", clear all NAME sub-tags
      'name'     — keep NAME (and its GIVN/SURN children) unchanged
      'initials' — reduce NAME to initials; keep GIVN/SURN as initials

    In all modes every tag other than NAME, SEX, FAMC, FAMS is removed.
    """
    label_before = _indi_label(indi_el) if verbose else ""

    children = indi_el.get_child_elements()
    to_keep = []
    name_kept = False

    for ch in children:
        tag = ch.get_tag()
        if tag in ("SEX", "FAMC", "FAMS"):
            to_keep.append(ch)
        elif tag == gedcom.tags.GEDCOM_TAG_NAME and not name_kept:
            if mode == "<private>":
                ch.set_value("<private>")
                ch.get_child_elements().clear()
            elif mode == "initials":
                ch.set_value(_shorten_name_value(ch.get_value()))
                name_children = ch.get_child_elements()
                kept_nc = []
                for gch in name_children:
                    if gch.get_tag() == "GIVN":
                        gch.set_value(_initials(gch.get_value()))
                        kept_nc.append(gch)
                    elif gch.get_tag() == "SURN":
                        gch.set_value(_initials(gch.get_value()))
                        kept_nc.append(gch)
                name_children.clear()
                name_children.extend(kept_nc)
            # mode == "name": keep NAME element as-is
            to_keep.append(ch)
            name_kept = True

    if not name_kept and mode == "<private>":
        name_el = Element(
            indi_el.get_level() + 1,
            "",
            gedcom.tags.GEDCOM_TAG_NAME,
            "<private>",
            "\n",
            multi_line=False,
        )
        name_el.set_parent_element(indi_el)
        to_keep.insert(0, name_el)

    children.clear()
    children.extend(to_keep)

    if verbose:
        ptr = indi_el.get_pointer() or ""
        label_after = _indi_label(indi_el)
        print(f"  [living:{mode}] {ptr}  {label_before}  →  {label_after}")


# ---------------------------------------------------------------------------
# Ancestor collection
# ---------------------------------------------------------------------------


def _collect_ancestors(
    target_ptr: str,
    ptr_index: dict,
    verbose: bool,
    forbidden: frozenset[str] | set[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    """
    Collect all direct ancestors of target_ptr via FAMC -> FAM -> HUSB/WIFE.

    Returns:
        (ancestor_indi_ptrs, ancestor_fam_ptrs)
        Both sets include the target individual and all their parent families.

    `forbidden`: individuals to never traverse through or include — used to
    enforce the "do not cross root" rule (e.g. root's descendants when only
    --ancestors was requested).
    """
    indi_ptrs: set[str] = set()
    fam_ptrs: set[str] = set()
    queue = [target_ptr]
    visited: set[str] = set()

    while queue:
        ptr = queue.pop()
        if ptr in visited:
            continue
        visited.add(ptr)
        if ptr in forbidden:
            continue

        indi = ptr_index.get(ptr)
        if indi is None or indi.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue

        indi_ptrs.add(ptr)
        if verbose:
            print(f"  [keep:indi] {ptr}  {_indi_label(indi)}")

        # Walk up via FAMC links (families where this person is a child)
        for ch in indi.get_child_elements():
            if ch.get_tag() != "FAMC":
                continue
            fam_ptr = ch.get_value().strip()
            fam = ptr_index.get(fam_ptr)
            if fam is None or fam.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
                continue
            fam_ptrs.add(fam_ptr)
            # Queue both parents
            for fch in fam.get_child_elements():
                if fch.get_tag() in (
                    gedcom.tags.GEDCOM_TAG_HUSBAND,
                    gedcom.tags.GEDCOM_TAG_WIFE,
                ):
                    parent_ptr = fch.get_value().strip()
                    if parent_ptr not in visited:
                        queue.append(parent_ptr)

    return indi_ptrs, fam_ptrs


# ---------------------------------------------------------------------------
# Descendant collection
# ---------------------------------------------------------------------------


def _collect_descendants(
    target_ptr: str,
    ptr_index: dict,
    verbose: bool,
    forbidden: frozenset[str] | set[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    """
    Collect all descendants of target_ptr via FAMS -> FAM -> CHIL.

    Returns:
        (descendant_indi_ptrs, descendant_fam_ptrs)
        Both sets include the target individual and all families they created
        as a parent.

    `forbidden`: individuals to never traverse through or include.
    """
    indi_ptrs: set[str] = set()
    fam_ptrs: set[str] = set()
    queue = [target_ptr]
    visited: set[str] = set()

    while queue:
        ptr = queue.pop()
        if ptr in visited:
            continue
        visited.add(ptr)
        if ptr in forbidden:
            continue

        indi = ptr_index.get(ptr)
        if indi is None or indi.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue

        indi_ptrs.add(ptr)
        if verbose:
            print(f"  [keep:indi] {ptr}  {_indi_label(indi)}")

        # Walk down via FAMS links (families where this person is a parent)
        for ch in indi.get_child_elements():
            if ch.get_tag() != "FAMS":
                continue
            fam_ptr = ch.get_value().strip()
            fam = ptr_index.get(fam_ptr)
            if fam is None or fam.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
                continue
            fam_ptrs.add(fam_ptr)
            # Queue all children
            for fch in fam.get_child_elements():
                if fch.get_tag() == gedcom.tags.GEDCOM_TAG_CHILD:
                    child_ptr = fch.get_value().strip()
                    if child_ptr not in visited:
                        queue.append(child_ptr)

    return indi_ptrs, fam_ptrs


# ---------------------------------------------------------------------------
# Sibling collection
# ---------------------------------------------------------------------------


def _collect_siblings(
    targets: set[str],
    indi_ptrs: set[str],
    ptr_index: dict,
    verbose: bool,
    forbidden: frozenset[str] | set[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    """
    For each individual in `targets`, follow their FAMC links and collect
    every other CHIL (their siblings) plus the FAMC family itself.

    Returns (new_indi, new_fam) — additions only; targets and individuals
    already in `indi_ptrs` (and anyone in `forbidden`) are skipped. The
    FAMC families themselves are always returned (even if already kept) so
    the caller can union them in.
    """
    new_indi: set[str] = set()
    new_fam: set[str] = set()

    origin_fams: set[str] = set()
    for ptr in targets:
        if ptr in forbidden:
            continue
        indi = ptr_index.get(ptr)
        if indi is None:
            continue
        for ch in indi.get_child_elements():
            if ch.get_tag() == "FAMC":
                origin_fams.add(ch.get_value().strip())

    new_fam |= origin_fams

    for fam_ptr in origin_fams:
        fam = ptr_index.get(fam_ptr)
        if fam is None or fam.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
            continue
        for fch in fam.get_child_elements():
            if fch.get_tag() != gedcom.tags.GEDCOM_TAG_CHILD:
                continue
            sib_ptr = fch.get_value().strip()
            if sib_ptr in indi_ptrs or sib_ptr in new_indi or sib_ptr in forbidden:
                continue
            sib = ptr_index.get(sib_ptr)
            if sib is None:
                continue
            new_indi.add(sib_ptr)
            if verbose:
                print(f"  [keep:sibling] {sib_ptr}  {_indi_label(sib)}")

    return new_indi, new_fam


# ---------------------------------------------------------------------------
# Partner (spouse) collection
# ---------------------------------------------------------------------------


def _collect_partners(
    indi_ptrs: set[str],
    ptr_index: dict,
    verbose: bool,
    forbidden: frozenset[str] | set[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    """For each individual in `indi_ptrs`, follow FAMS links and collect every
    other HUSB/WIFE (their spouse) plus the FAMS family itself.

    Returns (new_indi, new_fam) — additions only; people already in indi_ptrs
    and anyone in `forbidden` are skipped.
    """
    new_indi: set[str] = set()
    new_fam: set[str] = set()

    for ptr in indi_ptrs:
        if ptr in forbidden:
            continue
        indi = ptr_index.get(ptr)
        if indi is None:
            continue
        for ch in indi.get_child_elements():
            if ch.get_tag() != "FAMS":
                continue
            fam_ptr = ch.get_value().strip()
            fam = ptr_index.get(fam_ptr)
            if fam is None or fam.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
                continue
            new_fam.add(fam_ptr)
            for fch in fam.get_child_elements():
                if fch.get_tag() not in (
                    gedcom.tags.GEDCOM_TAG_HUSBAND,
                    gedcom.tags.GEDCOM_TAG_WIFE,
                ):
                    continue
                sp_ptr = fch.get_value().strip()
                if (
                    sp_ptr == ptr
                    or sp_ptr in indi_ptrs
                    or sp_ptr in new_indi
                    or sp_ptr in forbidden
                ):
                    continue
                sp = ptr_index.get(sp_ptr)
                if sp is None:
                    continue
                new_indi.add(sp_ptr)
                if verbose:
                    print(f"  [keep:partner] {sp_ptr}  {_indi_label(sp)}")

    return new_indi, new_fam


# ---------------------------------------------------------------------------
# Related collection
# ---------------------------------------------------------------------------


def _collect_related(
    indi_ptrs: set[str],
    fam_ptrs: set[str],
    ptr_index: dict,
    stop_ptrs: set[str],
    verbose: bool,
    forbidden: frozenset[str] | set[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    """
    Two-phase collection seeded by every individual already in `indi_ptrs`
    (ancestors, descendants, and siblings collected before this call).

    Phase 1 — BFS via FAMS:
      • Children of each visited family are queued as blood relatives.
      • The other spouse is recorded but NOT queued (so their own ancestral
        line is not explored — that is Phase 2's job, one level only).
      • FAMS traversal is skipped for stop_ptrs (prevents the root person's
        own descendants from being added when --descendants is absent).

    Phase 2 — one-level FAMC pass for spouses found in Phase 1:
      • For each spouse, follow their FAMC to add parents (HUSB/WIFE) and
        siblings (CHIL). Not queued — no recursion.

    `forbidden`: individuals to never add — enforces the "do not cross root"
    rule (root's descendants when --ancestors only, or root's ancestors
    when --descendants only). Forbidden individuals are also treated as
    stop_ptrs so their families are not traversed even if they slip into
    `indi_ptrs` somehow.

    Returns updated (indi_ptrs, fam_ptrs) sets.
    """
    new_indi: set[str] = set()
    new_fam: set[str] = set()
    spouses: set[str] = set()
    queue = list(indi_ptrs)
    # Mark forbidden individuals as already-visited so they are never added
    # by either phase, no matter which path reaches them.
    visited = set(indi_ptrs) | set(forbidden)

    # ── Phase 1: FAMS BFS ────────────────────────────────────────────────────
    while queue:
        ptr = queue.pop()
        if ptr in stop_ptrs or ptr in forbidden:
            continue
        indi = ptr_index.get(ptr)
        if indi is None or indi.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue
        for ch in indi.get_child_elements():
            if ch.get_tag() != "FAMS":
                continue
            fam_ptr = ch.get_value().strip()
            fam = ptr_index.get(fam_ptr)
            if fam is None or fam.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
                continue
            if fam_ptr not in fam_ptrs:
                new_fam.add(fam_ptr)
            for fch in fam.get_child_elements():
                ftag = fch.get_tag()
                fptr = fch.get_value().strip()
                if fptr in visited:
                    continue
                el = ptr_index.get(fptr)
                if el is None:
                    continue
                visited.add(fptr)
                new_indi.add(fptr)
                if ftag == gedcom.tags.GEDCOM_TAG_CHILD:
                    queue.append(fptr)
                    if verbose:
                        print(f"  [keep:related] {fptr}  {_indi_label(el)}")
                elif (
                    ftag
                    in (
                        gedcom.tags.GEDCOM_TAG_HUSBAND,
                        gedcom.tags.GEDCOM_TAG_WIFE,
                    )
                    and fptr != ptr
                ):
                    spouses.add(fptr)
                    if verbose:
                        print(f"  [keep:spouse ] {fptr}  {_indi_label(el)}")

    # ── Phase 2: birth families of spouses (one level, no recursion) ─────────
    for ptr in spouses:
        indi = ptr_index.get(ptr)
        if indi is None:
            continue
        for ch in indi.get_child_elements():
            if ch.get_tag() != "FAMC":
                continue
            fam_ptr = ch.get_value().strip()
            fam = ptr_index.get(fam_ptr)
            if fam is None or fam.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
                continue
            if fam_ptr not in fam_ptrs and fam_ptr not in new_fam:
                new_fam.add(fam_ptr)
            for fch in fam.get_child_elements():
                ftag = fch.get_tag()
                fptr = fch.get_value().strip()
                if fptr in visited:
                    continue
                el = ptr_index.get(fptr)
                if el is None:
                    continue
                visited.add(fptr)
                new_indi.add(fptr)
                if ftag == gedcom.tags.GEDCOM_TAG_CHILD:
                    if verbose:
                        print(f"  [keep:sibling] {fptr}  {_indi_label(el)}")
                elif ftag in (
                    gedcom.tags.GEDCOM_TAG_HUSBAND,
                    gedcom.tags.GEDCOM_TAG_WIFE,
                ):
                    if verbose:
                        print(f"  [keep:parent ] {fptr}  {_indi_label(el)}")

    return indi_ptrs | new_indi, fam_ptrs | new_fam


# ---------------------------------------------------------------------------
# Per-target expansion (used by the main filter loop)
# ---------------------------------------------------------------------------


def _expand_for_target(
    target_ptr: str,
    ptr_index: dict,
    forbidden: set[str],
    mode_ancestors: bool,
    mode_descendants: bool,
    related: bool,
    related_depth: int,
    siblings: bool,
    partners: bool,
    verbose: bool,
) -> tuple[set[str], set[str]]:
    """Run the full expansion pipeline (bloodline → related closure → partners
    → consistency) for one target individual. Returns the resulting
    (indi_ptrs, fam_ptrs) sets.
    """
    indi_ptrs: set[str] = {target_ptr}
    fam_ptrs: set[str] = set()
    ancestor_walked: set[str] = set()
    descendant_walked: set[str] = set()

    def _expand_blood_line() -> None:
        if mode_ancestors:
            for ptr in list(indi_ptrs - ancestor_walked):
                a_indi, a_fam = _collect_ancestors(ptr, ptr_index, verbose, forbidden)
                indi_ptrs.update(a_indi)
                fam_ptrs.update(a_fam)
                ancestor_walked.update(a_indi)
        if mode_descendants:
            for ptr in list(indi_ptrs - descendant_walked):
                d_indi, d_fam = _collect_descendants(ptr, ptr_index, verbose, forbidden)
                indi_ptrs.update(d_indi)
                fam_ptrs.update(d_fam)
                descendant_walked.update(d_indi)
        if siblings:
            s_indi, s_fam = _collect_siblings(
                set(indi_ptrs), indi_ptrs, ptr_index, verbose, forbidden
            )
            indi_ptrs.update(s_indi)
            fam_ptrs.update(s_fam)

    _expand_blood_line()

    if related:
        SAFETY_CAP = 50
        iteration = 0
        while True:
            iteration += 1
            before_size = (len(indi_ptrs), len(fam_ptrs))
            before_set = set(indi_ptrs)
            stop_ptrs = set() if mode_descendants else {target_ptr}
            indi_ptrs, fam_ptrs = _collect_related(
                indi_ptrs, fam_ptrs, ptr_index, stop_ptrs, verbose, forbidden
            )
            if siblings:
                new_in_laws = indi_ptrs - before_set
                s2_indi, s2_fam = _collect_siblings(
                    new_in_laws, indi_ptrs, ptr_index, verbose, forbidden
                )
                indi_ptrs.update(s2_indi)
                fam_ptrs.update(s2_fam)
            _expand_blood_line()
            if related_depth > 0 and iteration >= related_depth:
                break
            if (len(indi_ptrs), len(fam_ptrs)) == before_size:
                break
            if iteration >= SAFETY_CAP:
                print(
                    f"WARN: closure stopped at safety cap ({SAFETY_CAP} iterations)",
                    file=sys.stderr,
                )
                break

    if partners:
        p_indi, p_fam = _collect_partners(indi_ptrs, ptr_index, verbose, forbidden)
        indi_ptrs.update(p_indi)
        fam_ptrs.update(p_fam)

    # Consistency: every kept family must have its HUSB/WIFE present.
    for fam_ptr in list(fam_ptrs):
        fam = ptr_index.get(fam_ptr)
        if fam is None:
            continue
        for ch in fam.get_child_elements():
            if ch.get_tag() not in (
                gedcom.tags.GEDCOM_TAG_HUSBAND,
                gedcom.tags.GEDCOM_TAG_WIFE,
            ):
                continue
            parent_ptr = ch.get_value().strip()
            if (
                parent_ptr
                and parent_ptr in ptr_index
                and parent_ptr not in indi_ptrs
                and parent_ptr not in forbidden
            ):
                indi_ptrs.add(parent_ptr)
                if verbose:
                    print(
                        f"  [keep:parent ] {parent_ptr}  {_indi_label(ptr_index[parent_ptr])}"
                    )

    return indi_ptrs, fam_ptrs


# ---------------------------------------------------------------------------
# Main filter logic
# ---------------------------------------------------------------------------


def filter_file(
    input_path: str,
    output_path: str,
    person_specs: list[str],
    mode_ancestors: bool,
    mode_descendants: bool,
    related: bool,
    related_depth: int,
    siblings: bool,
    partners: bool,
    living: str | None,
    verbose: bool,
) -> None:
    parse_path, is_tmp = _transcode_to_utf8(input_path)
    try:
        parser = Parser()
        try:
            parser.parse_file(parse_path, strict=False)
        except Exception as e:
            print(f"ERROR: could not parse '{input_path}': {e}", file=sys.stderr)
            sys.exit(1)
        _update_char_tag(parser)
    finally:
        if is_tmp:
            os.unlink(parse_path)

    root_elements = parser.get_root_child_elements()

    # Build pointer -> element index for all top-level records
    ptr_index: dict = {
        el.get_pointer().strip(): el for el in root_elements if el.get_pointer()
    }

    # Locate all root persons
    target_ptrs = [_find_person(root_elements, spec) for spec in person_specs]
    label = "Root person: " if len(target_ptrs) == 1 else "Root person:"
    for ptr in target_ptrs:
        print(f"{label}  {ptr}  {_indi_label(ptr_index[ptr])}", file=sys.stderr)

    # Forbidden zone: individuals on the side of the tree the user did NOT
    # request. Closure must not cross root persons into this zone.
    #   --ancestors only  → forbid root's descendants
    #   --descendants only → forbid root's ancestors
    #   both              → no constraint
    forbidden: set[str] = set()
    if not mode_descendants:
        for target_ptr in target_ptrs:
            d_indi, _ = _collect_descendants(target_ptr, ptr_index, verbose=False)
            forbidden |= d_indi
    if not mode_ancestors:
        for target_ptr in target_ptrs:
            a_indi, _ = _collect_ancestors(target_ptr, ptr_index, verbose=False)
            forbidden |= a_indi
    forbidden -= set(target_ptrs)  # roots themselves are always allowed

    # Run the full expansion pipeline independently for each target person, so
    # we can report per-person contribution counts. The union of these sets is
    # what gets written to the output (deduplicated — a person who appears in
    # multiple bloodlines is counted once in the total).
    indi_ptrs: set[str] = set()
    fam_ptrs: set[str] = set()
    rows: list[tuple[str, int, int]] = []
    for target_ptr in target_ptrs:
        p_indi, p_fam = _expand_for_target(
            target_ptr,
            ptr_index,
            forbidden,
            mode_ancestors,
            mode_descendants,
            related,
            related_depth,
            siblings,
            partners,
            verbose,
        )
        rows.append((_indi_label(ptr_index[target_ptr]), len(p_indi), len(p_fam)))
        indi_ptrs |= p_indi
        fam_ptrs |= p_fam

    # Print per-person table with deduplicated total
    total_label = "TOTAL (deduplicated)"
    name_w = max(
        len("Person"),
        len(total_label),
        max((len(r[0]) for r in rows), default=0),
    )
    indi_w = max(
        len("Individuals"),
        len(str(len(indi_ptrs))),
        max((len(str(r[1])) for r in rows), default=0),
    )
    fam_w = max(
        len("Families"),
        len(str(len(fam_ptrs))),
        max((len(str(r[2])) for r in rows), default=0),
    )
    fmt = f"  {{:<{name_w}}}  {{:>{indi_w}}}  {{:>{fam_w}}}"
    print("", file=sys.stderr)
    print(fmt.format("Person", "Individuals", "Families"), file=sys.stderr)
    print(fmt.format("-" * name_w, "-" * indi_w, "-" * fam_w), file=sys.stderr)
    for r in rows:
        print(fmt.format(*r), file=sys.stderr)
    print(fmt.format("-" * name_w, "-" * indi_w, "-" * fam_w), file=sys.stderr)
    print(fmt.format(total_label, len(indi_ptrs), len(fam_ptrs)), file=sys.stderr)

    # Remove INDIs and FAMs not in the keep sets
    to_remove = []
    for el in root_elements:
        tag = el.get_tag()
        ptr = (el.get_pointer() or "").strip()
        if tag == gedcom.tags.GEDCOM_TAG_INDIVIDUAL and ptr not in indi_ptrs:
            to_remove.append(el)
            if verbose:
                print(f"  [remove:indi] {ptr}  {_indi_label(el)}")
        elif tag == gedcom.tags.GEDCOM_TAG_FAMILY and ptr not in fam_ptrs:
            to_remove.append(el)
            if verbose:
                print(f"  [remove:fam]  {ptr}")
    for el in to_remove:
        root_elements.remove(el)

    # Clean up FAMS and FAMC links in kept INDIs that reference removed families
    for el in root_elements:
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue
        stale = [
            ch
            for ch in el.get_child_elements()
            if ch.get_tag() in ("FAMS", "FAMC")
            and ch.get_value().strip() not in fam_ptrs
        ]
        for ch in stale:
            el.get_child_elements().remove(ch)

    # Clean up HUSB/WIFE/CHIL links in kept FAMs that reference removed individuals
    # (e.g. siblings of an ancestor that weren't pulled in by --related/--siblings).
    for el in root_elements:
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
            continue
        stale = [
            ch
            for ch in el.get_child_elements()
            if ch.get_tag()
            in (
                gedcom.tags.GEDCOM_TAG_HUSBAND,
                gedcom.tags.GEDCOM_TAG_WIFE,
                gedcom.tags.GEDCOM_TAG_CHILD,
            )
            and ch.get_value().strip() not in indi_ptrs
        ]
        for ch in stale:
            el.get_child_elements().remove(ch)

    # Apply living privacy to all kept individuals
    if living:
        living_count = 0
        for el in root_elements:
            if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue
            if _is_living(el):
                _apply_living_privacy(el, living, verbose)
                living_count += 1
        print(
            f"Living ({living}): {living_count} individuals redacted", file=sys.stderr
        )

    parser.invalidate_cache()
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            for element in parser.get_root_child_elements():
                f.write(_serialize(element))
    except OSError as e:
        print(f"ERROR: could not write '{output_path}': {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    arg_parser = argparse.ArgumentParser(
        description="Filter a GEDCOM file to keep selected individuals and their relationships.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    arg_parser.add_argument("input", help="Input GEDCOM file (.ged)")
    arg_parser.add_argument("output", help="Output GEDCOM file (.ged)")
    arg_parser.add_argument(
        "--person",
        required=True,
        nargs="+",
        metavar="PERSON",
        help="Root person(s): GEDCOM pointer (@I123@), name, or partial name. "
        "Specify multiple to union their results.",
    )
    arg_parser.add_argument(
        "--ancestors",
        action="store_true",
        help="Keep direct ancestors of the root person",
    )
    arg_parser.add_argument(
        "--descendants",
        action="store_true",
        help="Keep all descendants of the root person",
    )
    arg_parser.add_argument(
        "--related",
        action="store_true",
        help="Also keep all descendants of every ancestor (cousins, aunts/uncles, …). "
        "Use with --ancestors.",
    )
    arg_parser.add_argument(
        "--related-depth",
        type=int,
        default=1,
        metavar="N",
        help="How many marriage hops --related should chase. Each in-law added "
        "by --related is always run back through --ancestors / --siblings. "
        "1 (default) = one fan-out, 2 = two (in-laws of in-laws), "
        "0 = run until fixed point (full transitive closure).",
    )
    arg_parser.add_argument(
        "--siblings",
        action="store_true",
        help="Also keep all siblings of every included person",
    )
    arg_parser.add_argument(
        "--bloodline",
        action="store_true",
        help="Keep all blood relatives (= --ancestors --descendants: every "
        "descendant of every ancestor)",
    )
    arg_parser.add_argument(
        "--partners",
        action="store_true",
        help="Also keep the partner (spouse) of every kept person",
    )
    living_group = arg_parser.add_mutually_exclusive_group()
    living_group.add_argument(
        "--living-private",
        action="store_true",
        dest="living_private",
        help="Redact living individuals: replace name with '<private>', remove all events",
    )
    living_group.add_argument(
        "--living-name",
        action="store_true",
        dest="living_name",
        help="Redact living individuals: keep full name, remove all events",
    )
    living_group.add_argument(
        "--living-initials",
        action="store_true",
        dest="living_initials",
        help="Redact living individuals: reduce name to initials, remove all events",
    )
    arg_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each kept/removed record",
    )

    args = arg_parser.parse_args()
    args.input = unicodedata.normalize("NFC", args.input)
    args.output = unicodedata.normalize("NFC", args.output)

    if args.bloodline:
        args.ancestors = True
        args.descendants = True

    if not args.ancestors and not args.descendants:
        print(
            "ERROR: at least one filter mode must be specified (--ancestors, --descendants, --bloodline).",
            file=sys.stderr,
        )
        sys.exit(1)

    living = (
        "<private>"
        if args.living_private
        else (
            "name" if args.living_name else "initials" if args.living_initials else None
        )
    )

    filter_file(
        input_path=args.input,
        output_path=args.output,
        person_specs=args.person,
        mode_ancestors=args.ancestors,
        mode_descendants=args.descendants,
        related=args.related,
        related_depth=args.related_depth,
        siblings=args.siblings,
        partners=args.partners,
        living=living,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
