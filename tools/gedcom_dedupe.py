#!/usr/bin/env python3
"""
gedcom_dedupe: Find and merge duplicate individuals in a GEDCOM file.

This tool identifies potential duplicate individuals based on their name and
birth date. For each set of duplicates, it designates one as the "master"
record and merges the others into it.

Merge strategy:
1.  All references to duplicate individuals (as spouses or children) are
    updated to point to the master individual.
2.  Family links (FAMC/FAMS) from duplicates are added to the master record.
3.  All other information from the duplicate records (events, notes, sources)
    is preserved by converting the entire duplicate record into a NOTE on the
    master record. This allows for manual review and integration.
4.  The original duplicate records are removed from the file.

Usage:
    python tools/gedcom_dedupe.py <input.ged> -o <output.ged>
"""

import argparse
import os
import re
import sys
import tempfile
import unicodedata
from collections import defaultdict

import chardet
from gedcom.element.element import Element
from gedcom.parser import Parser
import gedcom.tags

# ---------------------------------------------------------------------------
# Encoding detection & transcoding (reused from gedcom_merge)
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


def _detect_encoding(raw: bytes) -> str:
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
    encoding = _detect_encoding(raw)
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
# Serialization & Helpers
# ---------------------------------------------------------------------------


def _serialize(element) -> str:
    level = element.get_level()
    if level < 0:
        return ""
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


def _parse_name(name_value: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)\s*/([^/]*)/\s*(.*)$", (name_value or "").strip())
    if m:
        given = (m.group(1) + " " + m.group(3)).strip()
        surn = m.group(2).strip()
    else:
        given = (name_value or "").strip()
        surn = ""
    return given, surn


def _get_name(indi_el) -> tuple[str, str]:
    for ch in indi_el.get_child_elements():
        if ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME:
            return _parse_name(ch.get_value())
    return "", ""


def _get_event_date(indi_el, event_tag: str) -> str:
    for ch in indi_el.get_child_elements():
        if ch.get_tag() == event_tag:
            for subch in ch.get_child_elements():
                if subch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                    return subch.get_value().strip()
    return ""


# ---------------------------------------------------------------------------
# Deduplication Logic
# ---------------------------------------------------------------------------


def find_duplicates(parser: Parser) -> list[list[Element]]:
    key_to_indis = defaultdict(list)
    for el in parser.get_root_child_elements():
        if el.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
            continue
        given, surname = _get_name(el)
        birth_date = _get_event_date(el, gedcom.tags.GEDCOM_TAG_BIRTH)
        if not surname or not given or not birth_date:
            continue
        key = (surname.lower(), given.lower(), birth_date)
        key_to_indis[key].append(el)
    return [group for group in key_to_indis.values() if len(group) > 1]


def merge_and_redirect(
    duplicate_groups: list[list[Element]], parser: Parser
) -> set[str]:
    redirect_map = {}
    delete_set = set()

    for group in duplicate_groups:
        group.sort(key=lambda el: el.get_pointer())
        master, sources = group[0], group[1:]
        master_ptr = master.get_pointer()
        print(
            f"Merging {len(sources)} duplicate(s) into {master_ptr} ({_get_name(master)[0]} {_get_name(master)[1]})"
        )

        for source in sources:
            source_ptr = source.get_pointer()
            redirect_map[source_ptr] = master_ptr
            delete_set.add(source_ptr)

            master_famc = {
                c.get_value()
                for c in master.get_child_elements()
                if c.get_tag() == "FAMC"
            }
            master_fams = {
                c.get_value()
                for c in master.get_child_elements()
                if c.get_tag() == "FAMS"
            }

            for child in source.get_child_elements():
                if child.get_tag() == "FAMC" and child.get_value() not in master_famc:
                    new_el = Element(
                        level=master.get_level() + 1,
                        pointer="",
                        tag="FAMC",
                        value=child.get_value(),
                    )
                    master.add_child_element(new_el)
                elif child.get_tag() == "FAMS" and child.get_value() not in master_fams:
                    new_el = Element(
                        level=master.get_level() + 1,
                        pointer="",
                        tag="FAMS",
                        value=child.get_value(),
                    )
                    master.add_child_element(new_el)

            source_info_lines = []
            for child in source.get_child_elements():
                if child.get_tag() not in ("FAMC", "FAMS"):
                    source_info_lines.append(_serialize(child).strip())

            note_text = f"Merged from duplicate record {source_ptr}."
            if source_info_lines:
                note_text += "\nOriginal data:\n" + "\n".join(source_info_lines)

            note_lines = note_text.split("\n")
            note_el = Element(
                level=master.get_level() + 1,
                pointer="",
                tag="NOTE",
                value=note_lines[0],
            )
            master.add_child_element(note_el)
            for line in note_lines[1:]:
                cont_el = Element(
                    level=master.get_level() + 2, pointer="", tag="CONT", value=line
                )
                note_el.add_child_element(cont_el)

    for el in parser.get_element_list():
        val = el.get_value()
        if val and isinstance(val, str) and val in redirect_map:
            el.set_value(redirect_map[val])

    return delete_set


def main():
    parser = argparse.ArgumentParser(
        description="Find and merge duplicate individuals in a GEDCOM file."
    )
    parser.add_argument("input", help="Input GEDCOM file to process")
    parser.add_argument("-o", "--output", required=True, help="Output GEDCOM file")
    args = parser.parse_args()
    args.input = unicodedata.normalize("NFC", args.input)
    args.output = unicodedata.normalize("NFC", args.output)

    print(f"Reading: {args.input}")
    parse_path, is_tmp = _transcode_to_utf8(args.input)
    try:
        ged_parser = Parser()
        ged_parser.parse_file(parse_path, strict=False)
    finally:
        if is_tmp:
            os.unlink(parse_path)

    duplicate_groups = find_duplicates(ged_parser)

    if not duplicate_groups:
        print("No duplicates found based on name and birth date.")
        # Still write to output to ensure a clean UTF-8 copy
        with open(args.output, "w", encoding="utf-8") as f_out:
            with open(parse_path, "r", encoding="utf-8") as f_in:
                f_out.write(f_in.read())
        print(f"Wrote clean copy to: {args.output}")
        return

    print(f"Found {len(duplicate_groups)} group(s) of potential duplicates.")

    delete_set = merge_and_redirect(duplicate_groups, ged_parser)

    print(f"Writing deduplicated file to: {args.output}")
    root_elements = ged_parser.get_root_child_elements()
    with open(args.output, "w", encoding="utf-8") as f:
        for el in root_elements:
            if el.get_pointer() in delete_set:
                continue
            f.write(_serialize(el))


if __name__ == "__main__":
    main()
