#!/usr/bin/env python3
"""
gedcom_merge: Merge multiple GEDCOM files into a single output file.

This tool structurally merges multiple trees by concatenating their records.
To ensure unique GEDCOM IDs for individuals, families, sources, and objects,
it automatically prefixes all pointers with a file-specific identifier
(e.g., @I1@ from the first file becomes @f1_I1@, and @I1@ from the second
becomes @f2_I1@).

Usage:
    python tools/gedcom_merge.py <input1.ged> <input2.ged> ... -o <output.ged>
"""

import argparse
import os
import re
import sys
import tempfile
import unicodedata

import chardet
from gedcom.parser import Parser
import gedcom.tags

# ---------------------------------------------------------------------------
# Encoding detection & transcoding (reused from gedcom_cleaner for robustness)
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
# Serialization
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


# ---------------------------------------------------------------------------
# Potential duplicate detection (report only -- never merges anything)
# ---------------------------------------------------------------------------

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_PLACEHOLDER_NAMES = {"", "n", "nn", "unknown", "neznan", "neznana", "private", "privat", "living"}


def _norm_name(text: str) -> str:
    """Lowercase, strip diacritics and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_name(name_value: str) -> tuple[str, str]:
    """Split a GEDCOM NAME value into (given, surname)."""
    m = re.match(r"^(.*?)\s*/([^/]*)/\s*(.*)$", (name_value or "").strip())
    if m:
        return (m.group(1) + " " + m.group(3)).strip(), m.group(2).strip()
    return (name_value or "").strip(), ""


def _parse_year(date_value: str) -> int | None:
    m = re.search(r"\b(\d{4})\b", date_value or "")
    return int(m.group(1)) if m else None


def _parse_date_key(date_value: str) -> tuple[int, int, int] | None:
    """Return (y, m, d) when a full date is present, else None."""
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})\b", date_value or "")
    if m and m.group(2).upper() in _MONTHS:
        return int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1))
    return None


class _Person:
    """The subset of an INDI record used for duplicate comparison."""

    __slots__ = ("pointer", "file", "given", "surname", "sex", "birth", "death", "display")

    def __init__(self, el, file_label: str):
        self.pointer = el.get_pointer()
        self.file = file_label
        given_raw, surname_raw = "", ""
        self.sex = ""
        self.birth = ""
        self.death = ""
        for ch in el.get_child_elements():
            tag = ch.get_tag()
            if tag == "NAME" and not given_raw and not surname_raw:
                given_raw, surname_raw = _split_name(ch.get_value())
            elif tag == "SEX" and not self.sex:
                self.sex = (ch.get_value() or "").strip().upper()[:1]
            elif tag in ("BIRT", "BAPM", "CHR") and not self.birth:
                self.birth = _child_date(ch)
            elif tag in ("DEAT", "BURI") and not self.death:
                self.death = _child_date(ch)
        self.given = _norm_name(given_raw)
        self.surname = _norm_name(surname_raw)
        self.display = f"{given_raw} {surname_raw}".strip()

    @property
    def first_given(self) -> str:
        return self.given.split(" ")[0] if self.given else ""

    def is_identifiable(self) -> bool:
        return (
            self.surname not in _PLACEHOLDER_NAMES
            and self.first_given not in _PLACEHOLDER_NAMES
        )


def _child_date(event_el) -> str:
    for sub in event_el.get_child_elements():
        if sub.get_tag() == "DATE":
            return (sub.get_value() or "").strip()
    return ""


def _score_pair(a: _Person, b: _Person) -> tuple[int, str] | None:
    """Score a candidate pair. Returns (confidence 0-100, reason) or None."""
    if a.sex and b.sex and a.sex != b.sex:
        return None

    a_full, b_full = _parse_date_key(a.birth), _parse_date_key(b.birth)
    a_year, b_year = _parse_year(a.birth), _parse_year(b.birth)

    # Full given-name match adds weight over a shared first name only.
    same_given = a.given == b.given

    if a_full and b_full:
        if a_full == b_full:
            return (100, "same name, identical birth date")
        if a_full[0] == b_full[0]:
            return (60 if same_given else 45, "same name, same birth year, different day")
        return None

    if a_year and b_year:
        if a_year == b_year:
            return (85 if same_given else 65, "same name, same birth year")
        if abs(a_year - b_year) <= 2:
            return (50 if same_given else 35, f"same name, birth years {a_year}/{b_year}")
        return None

    # One or both births unknown -- fall back to death date, else name only.
    a_dyear, b_dyear = _parse_year(a.death), _parse_year(b.death)
    if a_dyear and b_dyear:
        if a_dyear == b_dyear:
            return (70 if same_given else 50, "same name, same death year, birth unknown")
        return None
    if not a_year and not b_year and same_given:
        return (30, "same full name, no dates to compare")
    return None


def find_potential_duplicates(people: list[_Person], min_confidence: int) -> list[tuple]:
    """Compare people bucketed by (surname, first given name)."""
    buckets = {}
    for p in people:
        if not p.is_identifiable():
            continue
        buckets.setdefault((p.surname, p.first_given), []).append(p)

    matches = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                scored = _score_pair(bucket[i], bucket[j])
                if scored and scored[0] >= min_confidence:
                    matches.append((scored[0], scored[1], bucket[i], bucket[j]))
    matches.sort(key=lambda m: (-m[0], m[2].display, m[2].pointer))
    return matches


def _format_person(p: _Person) -> str:
    dates = []
    if p.birth:
        dates.append(f"b. {p.birth}")
    if p.death:
        dates.append(f"d. {p.death}")
    detail = ", ".join(dates) if dates else "no dates"
    return f"{p.pointer} [{p.file}] {p.display} ({detail})"


def report_duplicates(matches: list[tuple], out) -> None:
    if not matches:
        print("No potential duplicates detected.", file=out)
        return
    cross = sum(1 for m in matches if m[2].file != m[3].file)
    print(
        f"WARNING: {len(matches)} potential duplicate pair(s) detected "
        f"({cross} across different files) -- review and merge manually:",
        file=out,
    )
    for confidence, reason, a, b in matches:
        print(f"\n  [{confidence}%] {reason}", file=out)
        print(f"    {_format_person(a)}", file=out)
        print(f"    {_format_person(b)}", file=out)


def main():
    parser = argparse.ArgumentParser(
        description="Merge multiple GEDCOM files into one."
    )
    parser.add_argument(
        "inputs", nargs="*", help="Input GEDCOM files to merge (or use --input-dir)"
    )
    parser.add_argument("-o", "--output", required=True, help="Output GEDCOM file")
    parser.add_argument(
        "--input-dir", help="Merge all .ged files in this directory (sorted by name)"
    )
    parser.add_argument(
        "--no-check-duplicates",
        action="store_true",
        help="Skip the potential-duplicate scan",
    )
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=50,
        help="Only report duplicate pairs at or above this confidence (default: 50)",
    )
    parser.add_argument(
        "--duplicate-report",
        help="Write the duplicate report to this file instead of stdout",
    )
    args = parser.parse_args()

    if args.input_dir:
        input_dir = unicodedata.normalize("NFC", args.input_dir)
        if not os.path.isdir(input_dir):
            parser.error(f"--input-dir is not a directory: {input_dir}")
        args.inputs = sorted(
            os.path.join(input_dir, n)
            for n in os.listdir(input_dir)
            if n.lower().endswith(".ged")
        ) + list(args.inputs)
    if not args.inputs:
        parser.error("no input files: pass file paths or --input-dir")

    args.inputs = [unicodedata.normalize("NFC", p) for p in args.inputs]
    args.output = unicodedata.normalize("NFC", args.output)

    all_root_elements = []
    head_element = None
    people: list[_Person] = []

    for i, input_path in enumerate(args.inputs):
        print(f"Reading: {input_path}")
        prefix = f"f{i+1}_"  # Generate short prefix, e.g., 'f1_', 'f2_'

        parse_path, is_tmp = _transcode_to_utf8(input_path)
        try:
            ged_parser = Parser()
            ged_parser.parse_file(parse_path, strict=False)
        finally:
            if is_tmp:
                os.unlink(parse_path)

        # 1. Build a map of all pointers mapped to their prefixed version
        ptr_map = {}
        for el in ged_parser.get_element_list():
            ptr = el.get_pointer()
            if ptr and ptr.startswith("@") and ptr.endswith("@"):
                ptr_map[ptr] = f"@{prefix}{ptr[1:-1]}@"

        # 2. Rewrite all pointers and references
        for el in ged_parser.get_element_list():
            ptr = el.get_pointer()
            if ptr in ptr_map:
                el._Element__pointer = ptr_map[ptr]

            val = el.get_value()
            if (
                val
                and isinstance(val, str)
                and val.startswith("@")
                and val.endswith("@")
            ):
                # Rewrite exact references (e.g. `1 CHIL @I1@`)
                if val in ptr_map:
                    el.set_value(ptr_map[val])
                else:
                    # Aggressively rewrite broken references too, to avoid cross-file collision
                    el.set_value(f"@{prefix}{val[1:-1]}@")

        # 3. Collect elements (extracting only the first file's HEAD)
        file_label = os.path.basename(input_path)
        for el in ged_parser.get_root_child_elements():
            tag = el.get_tag()
            if tag == "HEAD":
                if i == 0:
                    head_element = el
            elif tag != "TRLR":
                all_root_elements.append(el)
                if not args.no_check_duplicates and tag == "INDI":
                    people.append(_Person(el, file_label))

    # Guard against pointer collisions the prefixing should have made impossible.
    seen_pointers = set()
    for el in all_root_elements:
        ptr = el.get_pointer()
        if not ptr:
            continue
        if ptr in seen_pointers:
            print(f"ERROR: duplicate GEDCOM ID after merge: {ptr}", file=sys.stderr)
        seen_pointers.add(ptr)

    print(f"Writing merged file to: {args.output}")
    with open(args.output, "w", encoding="utf-8") as f:
        if head_element:
            f.write(_serialize(head_element))
        for el in all_root_elements:
            f.write(_serialize(el))
        f.write("0 TRLR\n")
    print(f"Merged {len(args.inputs)} file(s), {len(all_root_elements)} record(s).")

    if not args.no_check_duplicates:
        matches = find_potential_duplicates(people, args.min_confidence)
        if args.duplicate_report:
            report_path = unicodedata.normalize("NFC", args.duplicate_report)
            with open(report_path, "w", encoding="utf-8") as f:
                report_duplicates(matches, f)
            print(f"Duplicate report written to: {report_path} ({len(matches)} pair(s))")
        else:
            report_duplicates(matches, sys.stdout)


if __name__ == "__main__":
    main()
