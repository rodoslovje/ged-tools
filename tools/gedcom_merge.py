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


def main():
    parser = argparse.ArgumentParser(
        description="Merge multiple GEDCOM files into one."
    )
    parser.add_argument("inputs", nargs="+", help="Input GEDCOM files to merge")
    parser.add_argument("-o", "--output", required=True, help="Output GEDCOM file")
    args = parser.parse_args()
    args.inputs = [unicodedata.normalize("NFC", p) for p in args.inputs]
    args.output = unicodedata.normalize("NFC", args.output)

    all_root_elements = []
    head_element = None

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
        for el in ged_parser.get_root_child_elements():
            tag = el.get_tag()
            if tag == "HEAD":
                if i == 0:
                    head_element = el
            elif tag != "TRLR":
                all_root_elements.append(el)

    print(f"Writing merged file to: {args.output}")
    with open(args.output, "w", encoding="utf-8") as f:
        if head_element:
            f.write(_serialize(head_element))
        for el in all_root_elements:
            f.write(_serialize(el))
        f.write("0 TRLR\n")


if __name__ == "__main__":
    main()
