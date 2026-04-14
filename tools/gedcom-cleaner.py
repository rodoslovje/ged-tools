#!/usr/bin/env python3
"""
gedcom-cleaner: Read a GEDCOM file and clean, strip, or transform its content.

Usage:
    python tools/gedcom-cleaner.py <input.ged> <output.ged> [OPTIONS]
    python tools/gedcom-cleaner.py --input-dir DIR --output-dir DIR [STEM ...] [OPTIONS]

Options:
    --preset PRESET                 Apply a predefined combination of processors.
    --clean CLEANER[,CLEANER...]    Apply specific formatting cleaners.
    --strip STRIPPER[,STRIPPER...]  Strip specific tags or records.
    --transform TRANS[,TRANS...]    Transform specific tags or structures.
    --verbose                       Print every change performed.
    --input-dir DIR                 Process all .ged files in DIR (batch mode).
    --output-dir DIR                Write processed files to DIR (batch mode).
    --workers N                     Parallel workers in batch mode (default: 16).
    STEM ...                        File stems to process in batch mode (default: all).

Processors
----------
Processors are the individual operations applied to the GEDCOM file. There are
three types, applied in this order: cleaners → transformers → strippers.

  Cleaners   Normalize field values in-place. The record structure is unchanged;
             only the text content of tags is cleaned (e.g. date format fixes,
             removing placeholder text). A cleaner receives a raw string value
             and returns a corrected one.

  Transformers  Restructure or reclassify records. May rename tags, move values
             between tags, or anonymize entire individuals. Transformers operate
             on the element tree and can add, remove, or rewrite child elements.

  Strippers  Remove unwanted tags or entire records from the output. Strippers
             run last so that cleaners and transformers have already processed
             any data worth keeping before it is discarded.

Presets
-------
A preset is a named combination of cleaners, strippers, and transformers that
covers a common use case. Presets and individual processor flags can be combined:
processors from both sources are merged (preset first, then explicit flags).

Available Cleaners:
    dd_mmm_yyyy          Normalize all dates to DD MMM YYYY format.
    name_placeholder     Clear empty/placeholder names (e.g. "___", "???").
    place_placeholder    Clear empty/placeholder places.
    place_slovenia_rm    Remove "Slovenia" / "Slovenija" suffix from places.
    place_duplicate_rm   Remove adjacent duplicate components in places.
    place_country_only   Reduce place to two parts: place, country.

Available Strippers:
    ste, stf, sto, bkm   Strip proprietary MacFamilyTree / other app tags.
    labl                 Remove _LABL (label) tags.
    place_tran           Remove TRAN (translation) entries under PLAC tags.
    mise                 Remove MISE tags.
    object_crop          Remove CROP entries under OBJE tags.
    addr_longlati        Remove coordinates (LATI/LONG) from addresses.
    indi_race            Remove RACE tags.
    change_date          Remove CHAN (change date) tags.
    create_date          Remove CREA (creation date) tags.
    deat_placeholder     Remove DEAT/BURI/CREM records that are entirely placeholder
                         (no real date, no real place — e.g. MFT stub "__.__.____" / "____").
                         Skipped for individuals born 100+ years ago (stub means "died, date unknown").
                         Runs before transformers so privacy evaluation sees clean DEAT state.
    noname_indi          Remove individuals with no valid name.
    noname_fam           Remove families with no named spouses.
    living               Remove individuals who are likely still living.

Available Transformers (listed in execution order):
    born20y_private      Remove individuals born in the last 20 years with no
                         confirmed death record.
    died20y_private      Anonymize individuals whose death, burial, or cremation
                         was recorded within the last 20 years (date must be
                         present). Complies with ZVOP-2 post-mortem protection.
    marriage20y_private  Remove family records where marriage occurred in the last
                         20 years.
    living100y_private   Anonymize individuals with a known birth year under 100
                         years ago and no death record: set name to "private" and
                         remove all events. Uses birth, baptism, or christening
                         date. Falls back to relative-based birth year estimation
                         (parents +35y, children -35y) when birth date is absent
                         or partial. Complies with ZVOP-2 for living persons.
    living100y_initials  Same detection as living100y_private but reduces the full
                         name to initials (e.g. Luka /Renko/ -> L. /R./).
                         All events are still removed.
    fam_partner_private  If both spouses are private: remove the entire family record.
                         If one spouse is private: replace all non-empty event field
                         values (date, place, note, links, etc.) with "private".
                         Runs last, after all individual-level privacy transformers.
    secg_givn            Append NAME:SECG content to NAME:GIVN and remove the SECG tag.
    fid_fsftid           Rename _FID to _FSFTID (FamilySearch ID tag fix).
    latr_even            Convert LATR to EVEN type="Land Transaction".
    addr_to_plac         Merge ADDR values into event PLAC tags.

Available Presets:
    mft_webtrees         WebTrees compatibility for MacFamilyTree exports.
                         Cleaners: dd_mmm_yyyy, name_placeholder.
                         Strippers: ste, stf, sto, bkm, labl, addr_longlati, place_tran, mise, object_crop,
                           change_date, create_date, indi_race.
                         Transformers: secg_givn, fid_fsftid, latr_even.
    mft_sgi              Slovenian Genealogy Institute formatting.
                         Cleaners: place_slovenia_rm.
                         Transformers: addr_to_plac, living100y_private.
    mft_public           Public sharing from MacFamilyTree exports.
                         Cleaners: place_country_only.
                         Transformers: living100y_initials.
    index_cleanup_sgi    Full cleanup and anonymization for public indices.
                         Cleaners: dd_mmm_yyyy, name_placeholder,
                           place_placeholder, place_duplicate_rm.
                         Strippers: deat_placeholder, noname_indi, noname_fam.
                         Transformers (in order): born20y_private, died20y_private,
                           marriage20y_private, living100y_private, fam_partner_private.

Examples:
    # Apply a preset to a single file
    python tools/gedcom-cleaner.py family.ged out.ged --preset index_cleanup_sgi

    # Combine a preset with an extra stripper
    python tools/gedcom-cleaner.py family.ged out.ged --preset mft_webtrees --strip change_date

    # Apply individual processors with verbose output
    python tools/gedcom-cleaner.py family.ged out.ged --clean dd_mmm_yyyy --transform living100y_private --verbose

    # Batch: process all files in a directory
    python tools/gedcom-cleaner.py --input-dir data/input --output-dir data/filtered --preset mft_webtrees

    # Batch: process specific files only
    python tools/gedcom-cleaner.py --input-dir data/input --output-dir data/filtered --preset mft_webtrees Košir Hawlina
"""

import argparse
import io
import locale
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import chardet
from gedcom.element.element import Element
from gedcom.parser import Parser
import gedcom.tags


# ---------------------------------------------------------------------------
# Thread-local stdout/stderr proxy
# ---------------------------------------------------------------------------

class _ThreadLocalStream:
    """Proxy for sys.stdout/sys.stderr that dispatches to a per-thread override
    when set, falling back to the real stream for the main thread and any thread
    that has not installed an override. This allows worker threads to capture
    their own output without clobbering the global sys.stdout/sys.stderr."""

    def __init__(self, real_stream):
        object.__setattr__(self, "_real", real_stream)
        object.__setattr__(self, "_local", threading.local())

    def _target(self):
        return getattr(self._local, "override", self._real)

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        return self._target().flush()

    # Forward any other attribute access (e.g. .encoding, .reconfigure) to real
    def __getattr__(self, name):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Encoding detection & transcoding
# ---------------------------------------------------------------------------

# Maps GEDCOM CHAR tag values to Python codec names
_GEDCOM_CHAR_MAP = {
    "UTF-8": "utf-8",
    "UNICODE": "utf-16",
    "UTF-16": "utf-16",
    "ASCII": "ascii",
    # "ANSI" is intentionally omitted — it is ambiguous (cp1252 for Western European,
    # cp1250 for Central/Eastern European). chardet is more reliable for distinguishing them.
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
    """
    Check if raw bytes contain Windows-1250 Slovenian characters in positions
    that are mathematically impossible for valid UTF-8.
    - 0x9A (š), 0x9E (ž), 0x8A (Š), 0x8E (Ž) are continuation bytes in UTF-8.
      If preceded by ASCII (<128), they are definitely not UTF-8.
    - 0xE8 (č), 0xC8 (Č) are start bytes in UTF-8.
      If followed by ASCII (<128), the sequence is definitely not UTF-8.
    """
    for i in range(1, len(raw)):
        if raw[i] in (0x9A, 0x9E, 0x8A, 0x8E) and raw[i - 1] < 128:
            return True
    for i in range(len(raw) - 1):
        if raw[i] in (0xE8, 0xC8) and raw[i + 1] < 128:
            return True
    return False


def _detect_encoding(file_path: str) -> str:
    """Detect encoding of a GEDCOM file. Returns a Python codec name."""
    with open(file_path, "rb") as f:
        raw = f.read()

    # 1. BOM detection
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"

    # 2. GEDCOM CHAR header tag (scan first 4 KB decoded as latin-1 — safe for ASCII header)
    header_text = raw[:4096].decode("latin-1", errors="replace")
    m = re.search(r"^\d\s+CHAR\s+(.+)$", header_text, re.MULTILINE | re.IGNORECASE)
    if m:
        char_value = m.group(1).strip().upper()
        if char_value in _GEDCOM_CHAR_MAP:
            return _GEDCOM_CHAR_MAP[char_value]

    # 3. chardet fallback — require reasonable confidence; mac_roman and ascii are unreliable
    #    for GEDCOM files with Slovenian/Central European content. Below threshold, prefer
    #    windows-1250 which is the most common encoding for this region.
    detected = chardet.detect(raw)
    if detected:
        enc = detected.get("encoding") or ""
        confidence = detected.get("confidence") or 0
        if enc and confidence >= 0.2 and enc.lower() not in ("mac_roman", "ascii"):
            # chardet often misidentifies Windows-1250 as Windows-1252 or ISO-8859-1.
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

    # 4. Last resort — windows-1250 is safer than latin-1 for Slovenian GEDCOM files
    return "windows-1250"


def _transcode_to_utf8(input_path: str) -> tuple[str, bool]:
    """
    Ensure the file is UTF-8. If it already is, returns (input_path, False).
    Otherwise decodes it and writes a temp UTF-8 file, returns (tmp_path, True).
    Caller must delete the temp file when done (only when second value is True).
    """
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
                return input_path, False  # genuine UTF-8 — pass through unchanged
            except UnicodeDecodeError:
                # File claims to be UTF-8 but contains some invalid bytes.
                # Check if it's mostly valid UTF-8 with minor corruption.
                test_decode = raw.decode(encoding, errors="replace")
                if test_decode.count("\ufffd") < max(10, len(raw) // 1000):
                    fd, tmp_path = tempfile.mkstemp(suffix=".ged")
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(test_decode)
                    return tmp_path, True

                # Too many bad bytes: fall back to chardet.
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

    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        # File has bytes undefined in the chosen encoding (e.g. 0x81 in a cp1250 file
        # that also contains DOS-encoded German umlauts). Retry with the same encoding
        # using replacement chars — this preserves all correctly-encoded characters
        # (Slovenian Š/Č/Ž) and substitutes only the truly undefined bytes.
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        text = raw.decode("latin-1", errors="replace")

    fd, tmp_path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)

    return tmp_path, True


_EVENT_LABELS: dict[str, str] = {
    gedcom.tags.GEDCOM_TAG_BIRTH: "birth",
    gedcom.tags.GEDCOM_TAG_DEATH: "death",
    gedcom.tags.GEDCOM_TAG_MARRIAGE: "marriage",
    "BURI": "burial",
    "CHR": "christening",
    "DIV": "divorce",
    "EMIG": "emigration",
    "IMMI": "immigration",
    "NATU": "naturalization",
    "PROB": "probate",
}


def _event_label(element) -> str:
    """Return a human-readable event name for the parent of a DATE element, or ''."""
    parent = element.get_parent_element()
    if parent is None:
        return ""
    return _EVENT_LABELS.get(parent.get_tag(), "")


def _indi_label(indi_el) -> str:
    """Return 'FirstName Surname (b.DATE d.DATE)' for an INDI element."""
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
    dates = " ".join(filter(None, [
        f"b.{birth}" if birth else "",
        f"d.{death}" if death else "",
    ]))
    if dates:
        label += f" ({dates})"
    return label


def _record_label(element) -> str:
    """Return a human-readable label for the level-0 record containing element."""
    el = element
    while el.get_parent_element() and el.get_parent_element().get_level() >= 0:
        el = el.get_parent_element()

    tag = el.get_tag()
    pointer = el.get_pointer()

    if tag == gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
        return f"INDI {pointer} — {_indi_label(el)}"

    if tag == gedcom.tags.GEDCOM_TAG_FAMILY:
        return f"FAM  {pointer}"  # use _fam_label(fam, ptr_index) for richer output

    return f"{tag} {pointer}".strip()


def _fam_label(fam_el, ptr_index: dict) -> str:
    """Return 'FAM @Fxxx@ — Name (YYYY) + Name (YYYY)' using resolved spouse pointers."""
    parts = []
    for ch in fam_el.get_child_elements():
        if ch.get_tag() in (gedcom.tags.GEDCOM_TAG_HUSBAND, gedcom.tags.GEDCOM_TAG_WIFE):
            ptr = ch.get_value().strip()
            indi = ptr_index.get(ptr)
            parts.append(_indi_label(indi) if indi is not None else ptr)
    pointer = fam_el.get_pointer()
    body = " + ".join(parts) if parts else "?"
    return f"FAM  {pointer} — {body}"


def _serialize(element) -> str:
    """Recursively serialize an element and all its descendants at any depth.
    Works around two library bugs:
    1. to_gedcom_string(recursive=True) only goes one level deep.
    2. get_pointer() can return None instead of "" for synthetic elements,
       causing a TypeError in to_gedcom_string — we build the line manually."""
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
    """Set the CHAR header tag to UTF-8 in the parsed tree."""
    for element in parser.get_element_list():
        if element.get_tag() == "CHAR":
            element.set_value("UTF-8")
            return


# ---------------------------------------------------------------------------
# Cleaner: dd_mmm_yyyy
# ---------------------------------------------------------------------------

MONTHS_LONG = {
    # English
    "january": "JAN",
    "february": "FEB",
    "march": "MAR",
    "april": "APR",
    "may": "MAY",
    "june": "JUN",
    "july": "JUL",
    "august": "AUG",
    "september": "SEP",
    "october": "OCT",
    "november": "NOV",
    "december": "DEC",
    # German
    "januar": "JAN",
    "februar": "FEB",
    "märz": "MAR",
    "maerz": "MAR",
    "mai": "MAY",
    "juni": "JUN",
    "juli": "JUL",
    "oktober": "OCT",
    "dezember": "DEC",
    # Slovenian nominative
    "januar": "JAN",
    "februar": "FEB",
    "marec": "MAR",
    "april": "APR",
    "maj": "MAY",
    "junij": "JUN",
    "julij": "JUL",
    "avgust": "AUG",
    "september": "SEP",
    "oktober": "OCT",
    "november": "NOV",
    "december": "DEC",
    # Slovenian genitive (used in dates: "30. aprila 1998")
    "januarja": "JAN",
    "februarja": "FEB",
    "marca": "MAR",
    "aprila": "APR",
    "maja": "MAY",
    "junija": "JUN",
    "julija": "JUL",
    "avgusta": "AUG",
    "septembra": "SEP",
    "oktobra": "OCT",
    "novembra": "NOV",
    "decembra": "DEC",
    # Slovenian locative/dative (used after "po": "po maju 1875")
    "januarju": "JAN",
    "februarju": "FEB",
    "marcu": "MAR",
    "aprilu": "APR",
    "maju": "MAY",
    "juniju": "JUN",
    "juliju": "JUL",
    "avgustu": "AUG",
    "septembru": "SEP",
    "oktobru": "OCT",
    "novembru": "NOV",
    "decembru": "DEC",
    # Old Slovenian long forms
    "prosinec": "JAN",
    "svečan": "FEB",
    "sušec": "MAR",
    "rožnik": "JUN",
    "srpan": "JUL",
    "kosec": "SEP",
    "vinotok": "OCT",
    "kimovec": "OCT",
    "listopad": "NOV",
    "gruden": "DEC",
    # Latin nominative
    "januarius": "JAN",
    "ianuarius": "JAN",
    "februarius": "FEB",
    "martius": "MAR",
    "aprilis": "APR",
    "maius": "MAY",
    "iunius": "JUN",
    "junius": "JUN",
    "iulius": "JUL",
    "julius": "JUL",
    "augustus": "AUG",
    "september": "SEP",  # already in English, same form
    "october": "OCT",  # already in English, same form
    "november": "NOV",  # already in English, same form
    "december": "DEC",  # already in English, same form
    # Latin genitive (common in church records: "die 3 Januarii 1750")
    "januarii": "JAN",
    "ianuarii": "JAN",
    "februarii": "FEB",
    "martii": "MAR",
    "maii": "MAY",
    "iunii": "JUN",
    "junii": "JUN",
    "iulii": "JUL",
    "julii": "JUL",
    "augusti": "AUG",
    "septembris": "SEP",
    "octobris": "OCT",
    "novembris": "NOV",
    "decembris": "DEC",
    # Latin ablative/dative (common in church records: "natus Januario 1750")
    "januario": "JAN",
    "ianuario": "JAN",
    "februario": "FEB",
    "martio": "MAR",
    "aprili": "APR",
    "maio": "MAY",
    "iunio": "JUN",
    "junio": "JUN",
    "iulio": "JUL",
    "julio": "JUL",
    "augusto": "AUG",
    "septembrio": "SEP",
    "septembr": "SEP",
    "octobrio": "OCT",
    "novembrio": "NOV",
    "decembrio": "DEC",
    # Italian
    "gennaio": "JAN",
    "febbraio": "FEB",
    "marzo": "MAR",
    "aprile": "APR",
    "maggio": "MAY",
    "giugno": "JUN",
    "luglio": "JUL",
    "agosto": "AUG",
    "settembre": "SEP",
    "ottobre": "OCT",
    "novembre": "NOV",
    "dicembre": "DEC",
}

MONTHS_SHORT = {
    # English / GEDCOM canonical
    "jan": "JAN",
    "feb": "FEB",
    "mar": "MAR",
    "apr": "APR",
    "may": "MAY",
    "jun": "JUN",
    "jul": "JUL",
    "aug": "AUG",
    "sep": "SEP",
    "oct": "OCT",
    "nov": "NOV",
    "dec": "DEC",
    "sept": "SEP",
    # German short forms
    "jan.": "JAN",
    "feb.": "FEB",
    "mär": "MAR",
    "mär.": "MAR",
    "mrz": "MAR",
    "mrz.": "MAR",
    "apr.": "APR",
    "mai.": "MAY",
    "jun.": "JUN",
    "jul.": "JUL",
    "aug.": "AUG",
    "sep.": "SEP",
    "okt": "OCT",
    "okt.": "OCT",
    "nov.": "NOV",
    "dez": "DEC",
    "dez.": "DEC",
    # Slovenian short forms
    "jan.": "JAN",
    "feb.": "FEB",
    "febr.": "FEB",
    "febr": "FEB",
    "mar.": "MAR",
    "apr.": "APR",
    "maj.": "MAY",
    "jun.": "JUN",
    "jul.": "JUL",
    "avg": "AUG",
    "avg.": "AUG",
    "sep.": "SEP",
    "okt.": "OCT",
    "nov.": "NOV",
    "novemb.": "NOV",
    "novemb": "NOV",
    "dec.": "DEC",
    # Typo / transposition forms
    "frb": "FEB",
    "apil": "APR",  # transposition of APRIL
    "naov": "NOV",  # transposition of NAOV→NOV
    "niv": "NOV",  # typo (i instead of o); NIV.1915 etc.
    "nob": "NOV",  # typo (b instead of v)
    "dac": "DEC",  # typo (a instead of e)
    "dsc": "DEC",  # typo (s instead of e)
    "ma": "MAY",  # truncation of MAJ/MAY
    # Hebrew calendar month in Slavic transcription
    "ašr": "OCT",  # Tishrei (תשרי) ≈ October; ת misread as A, ש→Š, ר→R
    # Old Slovenian short forms
    "pros.": "JAN",
    "pros": "JAN",
    "sveč.": "FEB",
    "sveč": "FEB",
    "vel.": "FEB",
    "vel": "FEB",
    "suš.": "MAR",
    "suš": "MAR",
    "rožn.": "JUN",
    "rožn": "JUN",
    "srp.": "JUL",
    "srp": "JUL",
    "kos.": "SEP",
    "kos": "SEP",
    "kim.": "OCT",
    "kim": "OCT",
    "vin.": "OCT",
    "vin": "OCT",
    "list.": "NOV",
    "list": "NOV",
    "grud.": "DEC",
    "grud": "DEC",
    # Old Latin/Italian June forms (common in old church records)
    "iûno": "JUN",
    "iunio": "JUN",
    "iugno": "JUN",
    # Slovenian August variant
    "svg": "AUG",
    # Russian/Ukrainian month abbreviations in Latin transliteration
    # (В→B, Г→G; PKT likely OCR misread of OKT where О→P)
    "abg": "AUG",  # рус. авг = август (August)
    "pkt": "OCT",  # рус. окт = октябрь (October), O misread as P
    # Typo variants
    "jjun": "JUN",
    "jum": "JUN",  # typo for jun
    "manj": "MAY",  # typo for "maj"
    "maaj": "MAY",  # typo for "maj" (double a)
    "fen": "FEB",   # typo for "feb" (n instead of b)
    "eog": "AUG",  # garbled form of "avg"/"ago" (Slovenian/Italian August)
    # Latin short forms
    "ian.": "JAN",
    "ian": "JAN",
    "mart.": "MAR",
    "mart": "MAR",
    "maij": "MAY",  # old Latin genitive short form
    "iun.": "JUN",
    "iun": "JUN",
    "iul.": "JUL",
    "iul": "JUL",
    "aug.": "AUG",
    "aug": "AUG",
    "sept.": "SEP",
    "oct.": "OCT",
    "oct": "OCT",
    "xber": "DEC",
    "xbr": "DEC",
    "xbris": "DEC",  # X=10, old Latin December abbreviation
    # Italian short forms
    "gen.": "JAN",
    "gen": "JAN",
    "genn.": "JAN",
    "genn": "JAN",
    "febb.": "FEB",
    "febb": "FEB",
    "mag.": "MAY",
    "mag": "MAY",
    "giu.": "JUN",
    "giu": "JUN",
    "lug.": "JUL",
    "lug": "JUL",
    "ago.": "AUG",
    "ago": "AUG",
    "set.": "SEP",
    "set": "SEP",
    "ott.": "OCT",
    "ott": "OCT",
    "dic.": "DEC",
    "dic": "DEC",
}

MONTHS_MULTI = {
    # Old Slovenian multi-word month names (checked before single-word)
    "mali traven": "APR",
    "mali.traven": "APR",
    "m.traven": "APR",
    "veliki traven": "MAY",
    "vel.traven": "MAY",
    "v.traven": "MAY",
    "vel. srpan": "AUG",
    "veliki srpan": "AUG",
    "v.srpan": "AUG",
    "vel.srpan": "AUG",
    "mali srpan": "JUL",
    "m.srpan": "JUL",
}

# Map all known prefix variants to their canonical GEDCOM form
PREFIX_MAP = {
    "about": "ABT",
    "abt.": "ABT",
    "abt": "ABT",
    "abtt": "ABT",  # typo for abt (double t)
    "abtg": "ABT",  # typo for abt (stray g)
    "1bt": "ABT",  # typo for abt (1 instead of a)
    "~": "ABT",
    "'": "ABT",  # leading apostrophe = circa (genealogy convention)
    "<": "BEF",
    ">": "AFT",
    "before": "BEF",
    "bef.": "BEF",
    "bef": "BEF",
    "after": "AFT",
    "aft.": "AFT",
    "aft": "AFT",
    "circa": "ABT",
    "cal.": "CAL",
    "cal": "CAL",
    "cca.": "ABT",
    "cca": "ABT",
    "okoli": "ABT",
    "okrog": "ABT",
    "približno": "ABT",
    "priblizno": "ABT",  # without diacritic
    "priblixno": "ABT",  # legacy encoding mangling of približno (ž → x)
    "od": "FROM",  # Slovenian "od" (from)
    "cir.": "ABT",  # circa
    "cir": "ABT",
    "videno": "ABT",  # Slovenian "videno" (seen/observed — implies estimated)
    "predvideno": "ABT",  # Slovenian "predvideno" (predicted/estimated)
    "prevideno": "ABT",  # typo for "predvideno" (missing d)
    "jeseni": "ABT",  # Slovenian "jeseni" (in autumn — seasonal approximation)
    "pogrešan": "ABT",  # Slovenian "pogrešan" (missing — implies approximate/unknown death)
    "pogresan": "ABT",  # without diacritic
    "okli": "ABT",  # typo for "okoli" (approximately)
    "okrig": "ABT",  # typo for "okrog" (i instead of o)
    "izračunano": "ABT",  # Slovenian "izračunano" (calculated)
    "izracunano": "ABT",  # without diacritic
    "oli": "ABT",  # truncated "okoli" (approximately)
    "olkrog": "ABT",  # typo for "okrog" (L instead of K)
    "orog": "ABT",  # typo for "okrog" (missing k)
    "krog": "ABT",  # truncation of "okrog" (missing leading o)
    "recimo": "ABT",  # Slovenian "recimo" = "let's say" (approximate)
    "around": "ABT",  # English "around"
    "say": "ABT",  # English "say" = approximately
    "at": "ABT",  # English "at" used as approximation prefix
    "urbar": "ABT",  # historical land register ("urbar") — implies date derived from records
    "etu": "ABT",  # garbled/truncated approximation prefix
    "og": "ABT",  # truncation of "okrog"
    "org": "ABT",  # truncation of "okrog" (variant)
    "okorg": "ABT",  # compound typo for "okrog"
    "estimated": "ABT",
    "abg": "ABT",  # Russian авг without day = approximately (with day handled as AUG month)
    "okr.": "ABT",
    "okr": "ABT",
    "ok.": "ABT",
    "ok": "ABT",
    "ca.": "ABT",
    "ca": "ABT",
    # Garbled "cca" (circa) variants — C/CC/CCC etc. with OCR/encoding corruption
    "çca": "ABT",  # cedilla-c variant
    "žcca": "ABT",  # diacritic corruption
    "ccca": "ABT",  # quadruple-c garble
    "ccac": "ABT",  # transposition/corruption
    "cvca": "ABT",  # v-corruption
    "ccc": "ABT",  # triple-c garble
    "cc": "ABT",  # double-c garble (must come after longer forms)
    "c": "ABT",  # single-letter circa (must come after "ca"/"ca." to not shadow them)
    "pred": "BEF",
    "prred": "BEF",  # typo for "pred" (double r)
    "prd": "BEF",
    "vor": "BEF",
    "po": "AFT",
    "ˇ": "ABT",  # modifier letter caron (U+02C7) used as ABT in some apps
    "l.": "",  # Slovenian/German "Leto/Jahr" (year) — strip prefix, keep year
    "l": "",
    "letu": "",  # Slovenian "v letu" (in the year) — strip, keep year
    "letom": "",  # Slovenian "letom" (in the year)
    "est.": "EST",
    "est": "EST",
    "ges.": "EST",
    "ges": "EST",  # German "geschätzt" (estimated)
    "act": "EST",  # Latin "actum" (dated/recorded on)
    "pribl.": "ABT",
    "pribl": "ABT",
    "wft est.": "ABT",
    "wft est": "ABT",
}

# Regex pieces
_DAY = r"(?P<day>\d{1,2})"
_MONTH = r"(?P<month>[^\W\d_]+\.?)"
_YEAR = r"(?P<year>\d{3,4}(?:/\d{1,4})?)"

# Flexible separator: one or more of space, dot, comma, slash, hyphen, colon, tilde
_SEP = r"[\s.,/\-:~]+"

# Full date patterns (most specific first)
DATE_PATTERNS = [
    # DD MMM YYYY  — any mix of separators/spaces between tokens
    re.compile(rf"^{_DAY}{_SEP}{_MONTH}{_SEP}{_YEAR}$"),
    # DD MMMYYYY  — separator before month, none between month and year (e.g. "18.FEB1732")
    re.compile(rf"^{_DAY}{_SEP}{_MONTH}(?P<year>\d{{3,4}})$"),
    # DDMMM YYYY  — no separator before month, separator before year (e.g. "11FEB.1694")
    re.compile(rf"^{_DAY}{_MONTH}{_SEP}{_YEAR}$"),
    # DDMMMYYYY  — no separators at all (e.g. "03NOV1912")
    re.compile(rf"^{_DAY}{_MONTH}(?P<year>\d{{3,4}})$"),
    # MMM DD YYYY  (e.g. "Jan 15 1900")
    re.compile(rf"^{_MONTH}{_SEP}{_DAY}{_SEP}{_YEAR}$"),
    # YYYY-MM-DD  (ISO — must come before generic numeric to avoid wrong group assignment)
    re.compile(r"^(?P<year>\d{4})-(?P<monthnum>\d{1,2})-(?P<day>\d{1,2})$"),
    # DD MM YYYY  — numeric month, any mix of separators (including mixed like "31 05.1756")
    re.compile(
        rf"^(?P<day>\d{{1,2}}){_SEP}(?P<monthnum>\d{{1,2}}){_SEP}(?P<year>\d{{3,4}})$"
    ),
    # DD.MMYYYY  — separator after day, no separator between 2-digit month and 4-digit year
    re.compile(r"^(?P<day>\d{1,2})[.,/\-:](?P<monthnum>\d{2})(?P<year>\d{4})$"),
    # DDMM.YYYY or DDMM YYYY  — no separator between day and month, separator before year
    re.compile(r"^(?P<day>\d{2})(?P<monthnum>\d{2})[.,/\-:\s](?P<year>\d{4})$"),
    # .MM.YYYY / .MM-YYYY / .MM YYYY  (unknown day, numeric month, any separator)
    re.compile(rf"^[.,]\s*(?P<monthnum>\d{{1,2}}){_SEP}(?P<year>\d{{3,4}})$"),
    # .0YYYY  — leading dot+zero = unknown day and month (e.g. ".01620", ".01865")
    re.compile(r"^[.,]\s*0(?P<year>\d{4})$"),
    # .MMYYYY  (unknown day, numeric month, no separator — e.g. ".051948")
    re.compile(r"^[.,]\s*(?P<monthnum>\d{2})(?P<year>\d{4})$"),
    # .MMM.YYYY  (unknown day, named month — leading dot placeholder, e.g. ".MAJ.1693")
    re.compile(rf"^\.\s*{_MONTH}{_SEP}{_YEAR}$"),
    # MM YYYY  (numeric month, no day — e.g. "04 1883")
    re.compile(rf"^(?P<monthnum>\d{{1,2}}){_SEP}(?P<year>\d{{3,4}})$"),
    # MMM YYYY  (no day, with separator)
    re.compile(rf"^{_MONTH}{_SEP}{_YEAR}$"),
    # MMMYYYY  (no day, no separator — e.g. "NOV1839")
    re.compile(rf"^{_MONTH}(?P<year>\d{{3,4}})$"),
    # .YYYY  (unknown day and month — leading dot placeholder, year only)
    re.compile(r"^\.\s*(?P<year>\d{3,4})$"),
    # YYYY only
    re.compile(r"^(?P<year>\d{3,4})$"),
]


def _normalize_month_name(token: str) -> str | None:
    """Return 3-letter uppercase month or None if unrecognised."""
    t = token.lower().rstrip(".")
    if t in MONTHS_SHORT:
        return MONTHS_SHORT[t]
    if t in MONTHS_LONG:
        return MONTHS_LONG[t]
    return None


def _monthnum_to_abbr(num: str) -> str | None:
    abbrs = [
        "JAN",
        "FEB",
        "MAR",
        "APR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
    ]
    n = int(num)
    if 1 <= n <= 12:
        return abbrs[n - 1]
    return None


_MONTHS_MULTI_SORTED = sorted(MONTHS_MULTI.keys(), key=len, reverse=True)


def _parse_date_value(value: str) -> tuple[str | None, str | None]:
    """
    Try to parse a date string (without prefix).
    Returns (formatted_date, None) on success or (None, reason) on failure.
    Formatted date is like 'DD MMM YYYY', 'MMM YYYY', or 'YYYY'.
    """
    v = value.strip()

    # Substitute multi-word month names before pattern matching
    vl = v.lower()
    for mw in _MONTHS_MULTI_SORTED:
        if mw in vl:
            abbr = MONTHS_MULTI[mw]
            idx = vl.index(mw)
            v = v[:idx] + abbr + v[idx + len(mw) :]
            vl = v.lower()
            break

    for pat in DATE_PATTERNS:
        m = pat.match(v)
        if not m:
            continue
        gd = m.groupdict()

        year = gd.get("year")
        day = gd.get("day")
        month = None

        if "monthnum" in gd and gd["monthnum"]:
            mn = gd["monthnum"]
            if int(mn) == 0:
                month = None
                day = None
            else:
                month = _monthnum_to_abbr(mn)
                if month is None:
                    # month number > 12: try swapping day and month (MM.DD.YYYY → DD.MM.YYYY)
                    if day and _monthnum_to_abbr(day) and int(mn) <= 31:
                        month = _monthnum_to_abbr(day)
                        day = mn
                    else:
                        # Both day and month > 12 — unresolvable, keep year only
                        month = None
                        day = None
        elif "month" in gd and gd["month"]:
            month = _normalize_month_name(gd["month"])
            if month is None:
                # Unknown month name — salvage the year as an approximate date
                if year:
                    return f"ABT {year}", None
                return None, f"unrecognised month '{gd['month']}' in '{value}'"

        # Three-digit years are assumed to be missing a leading '1' (e.g. 994 → 1994)
        if year and len(year) == 3:
            year = "1" + year

        if day == "00":
            day = None

        parts = []
        if day:
            parts.append(str(int(day)))  # strip leading zero
        if month:
            parts.append(month)
        if year:
            parts.append(year)

        return " ".join(parts), None

    # Last resort: extract a plausible 4-digit year (1000-2099) from a malformed date.
    # Use overlapping search (advance 1 char at a time) and take the rightmost match,
    # so that in digit-concatenated dates the trailing year wins over leading fragments.
    # e.g. "30.101.1871" → "1871", "100118900" → "1890", "2101802" → "1802"
    _year_pat = re.compile(r"1[0-9]{3}|20[0-2][0-9]")
    found = None
    for i in range(len(value)):
        m = _year_pat.match(value, i)
        if m:
            found = m.group()
    if found:
        return found, None

    return None, f"unrecognised date format '{value}'"


_PLACEHOLDER_RE = re.compile(
    r"_+|[-]{2,}|<>|(?<!\d)<(?!\d{3,4})"
)  # _ / __ or -- or <> or bare < (not BEF prefix)


_PARTIAL_YEAR_RE = re.compile(r"\b(\d{1,3})_+\b")


def _extract_birth_year(date_val: str) -> int | None:
    """
    Extract a birth year from a cleaned date string for the 100-year privacy check.
    Handles full years (e.g. "ABT 1927") and partial years (e.g. "ABT 19__", "ABT 20__").
    For partial years, uses the *maximum* possible value (underscores → 9) so that
    anyone who *could* be under 100 years old is treated conservatively as living.
    Returns None if no year can be determined.
    """
    m = re.search(r"\b(1[0-9]{3}|20[0-2][0-9])\b", date_val)
    if m:
        return int(m.group(1))
    pm = _PARTIAL_YEAR_RE.search(date_val)
    if pm:
        digits = pm.group(1)
        return int(digits + "9" * (4 - len(digits)))
    return None


def _handle_placeholder(value: str) -> tuple[str, None] | None:
    """
    Handle dates that use __ / ____ as placeholders for unknown day/month/year.
    Returns ("", None)   if the date is fully unknown  → caller should remove the element.
    Returns (year, None) if only the year is known     → keep just the year.
    Returns None         if the value has no placeholders at all.
    """
    # Single dot alone = fully unknown (e.g. "--.--" collapses to ".")
    if re.match(r"^\.$", value):
        return "", None

    if not _PLACEHOLDER_RE.search(value):
        return None

    # Extract any real year (3-4 digits, not underscores)
    year_match = re.search(r"\b(\d{3,4})\b", value)
    if year_match:
        return year_match.group(1), None

    # Partial year: 1-3 known digits followed by underscore placeholders (e.g. "19__" → "ABT 1900")
    partial_match = re.search(r"\b(\d{1,3})_+\b", value)
    if partial_match:
        digits = partial_match.group(1)
        missing = 4 - len(digits)
        approx_year = digits + "_" * missing
        return f"ABT {approx_year}", None

    # Fully unknown
    return "", None


def _parse_range(value: str) -> tuple[str | None, str | None, bool]:
    """
    Try to parse value as a date range.
    Returns (result, error, is_range).
    - (result, None, True)  — successfully parsed range
    - (None, error, True)   — looks like a range but dates inside are invalid
    - (None, None, False)   — not a range at all
    """
    v = value.strip()

    def _parse_part(d: str) -> tuple[str | None, str | None]:
        """Full clean of an inner date part (handles stacked prefixes like 'ABT OG 1784')."""
        result, warn = clean_date_dd_mmm_yyyy(d)
        if warn:
            return None, warn
        return result, None

    def both(d1: str, d2: str, fmt: str) -> tuple[str | None, str | None, bool]:
        r1, e1 = _parse_part(d1)
        if e1:
            return None, e1, True
        r2, e2 = _parse_part(d2)
        if e2:
            return None, e2, True
        return fmt.format(r1, r2), None, True

    def one(d: str, fmt: str) -> tuple[str | None, str | None, bool]:
        r, e = _parse_part(d)
        if e:
            return None, e, True
        return fmt.format(r), None, True

    # FROM date TO date  (TO / DO)
    m = re.match(r"^FROM\s+(.+?)\s+(?:TO|DO)\s+(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "FROM {} TO {}")

    # date DO/TO date  (no FROM, e.g. "1920 DO 1945")
    m = re.match(r"^(.+?)\s+(?:TO|DO)\s+(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "FROM {} TO {}")

    # FROM date  (open-ended)
    m = re.match(r"^FROM\s+(.+)$", v, re.IGNORECASE)
    if m:
        return one(m.group(1), "FROM {}")

    # TO date  (open-ended)
    m = re.match(r"^(?:TO|DO)\s+(.+)$", v, re.IGNORECASE)
    if m:
        return one(m.group(1), "TO {}")

    # BETWEEN/BET date AND date
    m = re.match(r"^(?:BETWEEN|BET)\s+(.+?)\s+AND\s+(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "BET {} AND {}")

    # BETWEEN/BET date - date  (hyphen separator)
    m = re.match(r"^(?:BETWEEN|BET)\s+(.+?)\s*-\s*(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "BET {} AND {}")

    # MED date - date  (Slovenian "med" = between)
    m = re.match(r"^MED\s+(.+?)\s*[-–]\s*(.+)$", v, re.IGNORECASE)
    if m:
        return both(m.group(1), m.group(2), "FROM {} TO {}")

    # BET date  (only one date, no AND/TO — treat as ABT)
    m = re.match(r"^(?:BETWEEN|BET)\s+(.+)$", v, re.IGNORECASE)
    if m:
        r, e = _parse_part(m.group(1))
        if e:
            return None, e, True
        # _parse_part may already include ABT; only add it if not already a qualifier
        if r and not re.match(r"^(ABT|EST|CAL|BEF|AFT)\b", r, re.IGNORECASE):
            r = "ABT " + r
        return r, None, True

    # YYYY-YYYY  (plain year range, e.g. "1856-1881") — left as-is, handled below

    return None, None, False


def clean_date_dd_mmm_yyyy(raw: str) -> tuple[str | None, str | None]:
    """
    Normalise a raw GEDCOM DATE value.
    Returns (cleaned_value, warning_message).
    warning_message is None on success.
    """
    v = raw.strip()
    if not v:
        return "", None  # silently keep empty date as-is

    # PRIVATE passthrough
    if v.upper() == "PRIVATE":
        return raw, None

    # Pure ? / ?? — unknown date, treat as empty
    if re.match(r"^\?+$", v):
        return "", None

    # N / NN / NNN / NO / NE / DA / Y / NOT MARRIED / +++ etc. — unknown/irrelevant markers
    if re.match(r"^[N+]+$", v, re.IGNORECASE) or v.upper() in (
        "NO",
        "NE",
        "DA",
        "Y",
        "NOT MARRIED",
        "HITRO",
        "UMRL",  # Slovenian "quickly"/"died" in date field
        "CIVILNA",  # Slovenian "civil" (marriage type)
    ):
        return "", None

    # Bare 2-digit number (e.g. "17", "18", "19") — century prefix without year, too ambiguous
    if re.match(r"^\d{2}$", v):
        return "", None

    # Strip trailing punctuation: dot, apostrophe, backtick, asterisk, slash
    v = v.rstrip(".'`*/")

    # Trailing ? or " ??" etc. — uncertain date; strip question marks, keep value as ABT
    uncertain = False
    v_stripped = v.rstrip("?").rstrip()
    if v_stripped != v and v_stripped:
        v = v_stripped
        uncertain = True

    # Strip trailing birth hour info: spaces + digits + optional H (e.g. "05.07.1913    7", "04.05.1915   2H")
    v_no_hour = re.sub(r"\s+\d{1,2}H?$", "", v)
    if v_no_hour != v and v_no_hour and v_no_hour[0].isdigit():
        v = v_no_hour

    # Inline x/X/y/Y/? placeholders within numeric tokens (e.g. "195x", "19xx", "197Y", "2?") → replace with 0, mark ABT
    v_norm, n_subs = re.subn(
        r"(?<=\d)[xXyY?]+|[xXyY?]+(?=\d)", lambda m: "0" * len(m.group()), v
    )
    if n_subs:
        v = v_norm
        uncertain = True

    # Standalone X or XX as day/month placeholder (e.g. "X.X.1965", "XX.05.2004") → replace with 0
    v = re.sub(r"(?<![A-Za-z])(X+)(?![A-Za-z])", lambda m: "0" * len(m.group()), v)

    # Strip parentheses (e.g. "(1620)", ".-.(1740)")
    v = re.sub(r"[()]", "", v).strip()

    # UNKNOWN / XXX-TEMPLATE — fully unknown date, treat as empty
    if v.upper() in ("UNKNOWN", "XXX-TEMPLATE"):
        return "", None

    # Collapse multiple leading dots/spaces/hyphens to a single dot (e.g. "..1920", ".-. 1740")
    v = re.sub(r"^[.\s\-]{2,}", ".", v)

    # Strip spurious single leading separator when followed by DD.MM... (e.g. ".24.03.1892").
    # Also strip when the remainder is already a full 3-part date (e.g. ".04.05.1923").
    # Do NOT strip when it's a .MM placeholder (2-digit ≤ 12 followed by space/year only).
    if len(v) > 1 and v[0] in ".,:" and v[1].isdigit():
        m2 = re.match(r"^[.,](\d{1,2})", v)
        num = int(m2.group(1)) if m2 else 99
        remainder = v[1:]
        if num > 12 or re.match(r"^\d{1,2}[.,\s]\d{1,2}[.,\s]\d{3,4}$", remainder):
            v = remainder

    # Strip single leading comma/dot used as unknown-day placeholder (e.g. ",MAJ 1945", ".MAJ 1945")
    if len(v) > 1 and v[0] in ".,":
        rest = v[1:].lstrip()
        # Only strip if what follows is not purely numeric (that would be .MM.YYYY — handled separately)
        if rest and not rest[0].isdigit():
            v = rest

    # Strip colon after leading word (e.g. "videno: 1762" → "videno 1762")
    v = re.sub(r"^(\S+):\s+", r"\1 ", v)

    # Strip leading = / ) and similar junk characters (repeated, e.g. "=)=)1840")
    v = re.sub(r"^[=≈≡＝\)\(]+\s*", "", v)

    # Strip stray leading non-ASCII letter before a year/date (e.g. "č2000" → "2000")
    v = re.sub(r"^[^\x00-\x7F]\s*(?=\d)", "", v)

    # Normalize letter O → digit 0 (OCR/typo): before digit, between digits, or after digit at word end
    v = re.sub(r"\bO(?=\d)|(?<=\d)O(?=\d)|(?<=\d)O\b", "0", v)

    # Collapse repeated tilde to single (e.g. "~~ 1968" → "~ 1968")
    v = re.sub(r"~+", "~", v)

    # Strip English ordinal suffixes and trailing comma (e.g. "20TH SEPTEMBER," → "20 SEPTEMBER")
    v = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", v, flags=re.IGNORECASE)
    v = v.rstrip(",").strip()

    # Strip bare single-letter markers T/M before year (e.g. "T 1640", "M 1750" → year only)
    # Only when T/M is the complete leading token (not a suffix like the T in OKT)
    if re.match(r"^[TM]\s+\d", v, re.IGNORECASE) and len(v.split()[0]) == 1:
        v = v.split(None, 1)[1]

    # Split fused qualifier+month tokens (e.g. "AFTJUL" → "AFT JUL", "BEFJUN" → "BEF JUN")
    v = re.sub(
        r"\b(ABT|AFT|BEF|CAL|EST)(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b",
        r"\1 \2",
        v,
        flags=re.IGNORECASE,
    )

    # ! as leading character is a typo for 1 (e.g. "!938" → "1938")
    if v.startswith("!") and v[1:].isdigit():
        v = "1" + v[1:]

    # Day + month with no year (e.g. "20 JUN") — pass through unchanged
    if re.match(rf"^\d{{1,2}}\s+[A-Za-z]+\.?$", v):
        return raw, None

    # Strip leading single-letter abbreviation + dot (e.g. "R.15.02.1931" → "15.02.1931")
    v = re.sub(r"^[A-Za-z]\.\s*(?=\d)", "", v)

    # Strip trailing single letter after a date (e.g. "27.05.1946 L" → "27.05.1946")
    v_no_sfx = re.sub(r"(?<=\d)\s+[A-Za-z]$", "", v)
    if v_no_sfx and v_no_sfx != v:
        v = v_no_sfx

    # Expand MA0 → MAY (stray 0 between month abbreviation and year, e.g. "22MA01970")
    v = re.sub(r"MA0(?=\d)", "MAY", v, flags=re.IGNORECASE)

    # Expand 2-digit year → 20YY for DD.MM.YY dates (e.g. "31.08.18" → "31.08.2018")
    m2y = re.match(r"^(\d{1,2})[.,\s](\d{1,2})[.,\s](\d{2})$", v)
    if m2y:
        v = f"{m2y.group(1)}.{m2y.group(2)}.20{m2y.group(3)}"

    # Latin numeric month abbreviations (old Roman-calendar positional form):
    #   7bris/7ber → SEP,  8bris/8ber → OCT,  9bris/9ber → NOV,  10bris/10ber → DEC
    # _MONTH regex only matches letters, so these must be expanded before pattern matching.
    _NUMERIC_MONTHS = (
        (r"\b10br(?:is)?\b|\b10ber\b", "DEC"),
        (r"\b9br(?:is)?\b|\b9ber\b", "NOV"),
        (r"\b8br(?:is)?\b|\b8ber\b", "OCT"),
        (r"\b7br(?:is)?\b|\b7ber\b", "SEP"),
    )
    for pat, repl in _NUMERIC_MONTHS:
        v = re.sub(pat, repl, v, flags=re.IGNORECASE)

    # Bare "/" or bare hyphen(s) — unknown date, treat as empty
    if v == "/" or re.match(r"^-+$", v):
        return "", None

    # Truncated decade: "184-" → ABT 1840 (trailing dash = uncertain decade)
    m_decade = re.match(r"^(\d{3})-$", v)
    if m_decade:
        return "ABT " + m_decade.group(1) + "0", None

    # Handle __ placeholder dates first
    placeholder = _handle_placeholder(v)
    if placeholder is not None:
        return placeholder  # ("", None) = remove  |  (year, None) = keep year only

    # YYYY-YYYY year ranges are kept as-is (no conversion to FROM/TO), spaces around hyphen allowed
    m = re.match(r"^(\d{3,4})\s*-\s*(\d{3,4})$", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}", None

    # YYYY-N or YYYY-NN — trailing sequence number / entry index, strip it, keep year only
    m = re.match(r"^(\d{3,4})-(\d{1,2})$", v)
    if m:
        return m.group(1), None

    # YYYY/Y … YYYY/YYYY — dual dating / alternative year notation, kept as-is
    if re.match(r"^\d{3,4}/\d{1,4}$", v):
        return v, None

    # Try range patterns first (before prefix handling)
    result, err, is_range = _parse_range(v)
    if is_range:
        return result, err

    # Detect and strip prefix (run up to twice for compound prefixes like "ˇ~")
    prefix_canon = None
    for _ in range(5):
        matched = False
        for variant, canon in PREFIX_MAP.items():
            # match whole word / token at start, case-insensitive.
            # If the prefix ends with a letter (e.g. "ok", "pred"), require the next char
            # to be whitespace/digit/end — not another letter — so "ok" doesn't match "okt".
            # If the prefix ends with a punctuation char (e.g. "abt.", "~"), allow any follow.
            if variant[-1].isalpha():
                lookahead = r"(?=[\s\d.~]|$)"
            else:
                lookahead = r"(?=[\s\d\w.~]|$)"
            pattern = re.compile(r"^" + re.escape(variant) + lookahead, re.IGNORECASE)
            if pattern.match(v):
                if prefix_canon is None:
                    prefix_canon = canon
                elif canon:
                    prefix_canon = canon
                v = v[len(variant) :].strip()
                matched = True
                break
        if not matched:
            break

    if not v:
        return "", None

    # Re-apply ! → 1 normalization in case it appeared after a prefix (e.g. "CCA !640")
    if v.startswith("!") and v[1:].isdigit():
        v = "1" + v[1:]

    formatted, err = _parse_date_value(v)
    if err:
        return None, err

    if uncertain and not prefix_canon:
        prefix_canon = "ABT"
    result = f"{prefix_canon} {formatted}" if prefix_canon else formatted
    return result, None


# ---------------------------------------------------------------------------
# Cleaner: name_placeholder
# ---------------------------------------------------------------------------

# Matches values that are entirely placeholder characters (_, ?, ., ,, -, /, (, ), [, ], <, >)
# plus whitespace. Handles any combination of these "unknown" markers.
_NAME_PLACEHOLDER_RE = re.compile(r"^[_.?,\s/\-()\[\]<>]+$")


def clean_name_placeholder(raw: str) -> tuple[str, None]:
    """
    Returns ("", None) if the name is a placeholder (all underscores or question marks).
    Also clears placeholder surnames from within slashes (e.g. "Jane /___/" -> "Jane //"),
    and placeholder given names (e.g. "___ /Smith/" -> "/Smith/").
    Returns (cleaned, None) otherwise.
    """
    if not raw:
        return raw, None

    if _NAME_PLACEHOLDER_RE.match(raw):
        return "", None

    # Clean placeholder surname between slashes
    def repl_surname(match):
        inner = match.group(1)
        if inner and _NAME_PLACEHOLDER_RE.match(inner):
            return "//"
        return match.group(0)

    cleaned = re.sub(r"/([^/]*)/", repl_surname, raw)

    # Clean placeholder given names (only if the ENTIRE part outside slashes is a placeholder)
    parts = re.split(r"(/[^/]*/)", cleaned)
    final_parts = []
    for p in parts:
        if p.startswith("/") and p.endswith("/"):
            final_parts.append(p)
        else:
            if _NAME_PLACEHOLDER_RE.match(p):
                final_parts.append("")
            else:
                final_parts.append(p)

    cleaned = "".join(final_parts).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    if not cleaned or _NAME_PLACEHOLDER_RE.match(cleaned):
        return "", None

    return cleaned, None


# ---------------------------------------------------------------------------
# Cleaner: place_placeholder
# ---------------------------------------------------------------------------

# Matches place values that are entirely placeholder characters (_, ?, ., ,, -, /, (, ), [, ], <, >)
# plus whitespace. Handles any combination of these "unknown" markers.
_PLACE_PLACEHOLDER_RE = re.compile(r"^[_.?,\s/\-()\[\]<>]+$")


def clean_place_placeholder(raw: str) -> tuple[str, None]:
    """
    Returns ("", None) if the place is a placeholder (all underscores, question marks,
    or comma-separated empty segments like '___, ___, ___').
    Returns (raw, None) otherwise — no change.
    """
    if _PLACE_PLACEHOLDER_RE.match(raw):
        return "", None
    return raw, None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cleaner: place_slovenia_rm
# ---------------------------------------------------------------------------

# Matches ", Slovenia" or ", Slovenija" (with optional surrounding whitespace) as a
# comma-separated segment anywhere in a place string.
_PLACE_SLOVENIA_RM_RE = re.compile(
    r",?\s*Sloveni(?:ja|a)\b.*", re.IGNORECASE | re.DOTALL
)


def clean_place_slovenia_rm(raw: str) -> tuple[str, None]:
    """
    Remove 'Slovenia' / 'Slovenija' and everything following it from a PLAC value.
    Returns (cleaned, None).
    """
    v = _PLACE_SLOVENIA_RM_RE.sub("", raw).strip(", ")
    if v == raw:
        return raw, None
    return v, None


# ---------------------------------------------------------------------------
# Cleaner: place_duplicate_rm
# ---------------------------------------------------------------------------


def clean_place_duplicate_rm(raw: str) -> tuple[str, None]:
    """
    Split the place string by commas and remove adjacent duplicate components.
    Returns (cleaned, None).
    """
    if not raw:
        return raw, None

    cleaned_parts = []
    for part in raw.split(","):
        p = part.strip()
        if not cleaned_parts or p.lower() != cleaned_parts[-1].lower():
            cleaned_parts.append(p)

    cleaned = ", ".join(cleaned_parts)
    if cleaned == raw:
        return raw, None
    return cleaned, None


# ---------------------------------------------------------------------------
# Cleaner: place_country_only
# ---------------------------------------------------------------------------


def clean_place_country_only(raw: str) -> tuple[str, None]:
    """
    Reduce a comma-separated place string to at most two parts: place, country.
    Takes the first component as the place and the last as the country.
    If there is only one component, it is returned unchanged.
    Returns (cleaned, None).
    """
    if not raw:
        return raw, None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) <= 2:
        return raw, None
    cleaned = f"{parts[0]}, {parts[-1]}"
    return cleaned, None


CLEANERS = {
    "dd_mmm_yyyy": clean_date_dd_mmm_yyyy,
    "name_placeholder": clean_name_placeholder,
    "place_placeholder": clean_place_placeholder,
    "place_slovenia_rm": clean_place_slovenia_rm,
    "place_duplicate_rm": clean_place_duplicate_rm,
    "place_country_only": clean_place_country_only,
}


# ---------------------------------------------------------------------------
# Strippers
# ---------------------------------------------------------------------------


@dataclass
class StripSpec:
    tags: set[str]
    parent_tag: str | None = (
        None  # None = level-0 records; str = children of that parent tag
    )
    grandparent_tag: str | None = None  # if set, parent's parent must match this tag
    level: int | None = None  # None = any level; int = exact level match


STRIPPERS: dict[str, StripSpec | None] = {
    "ste": StripSpec(tags={"_STE"}),  # MacFamilyTree source-template entries (level-0)
    "stf": StripSpec(tags={"_STF"}),  # MacFamilyTree source-template fields (level-0)
    "sto": StripSpec(tags={"_STO"}, level=1),
    "bkm": StripSpec(tags={"_BKM"}, level=1),
    "labl": StripSpec(tags={"_LABL"}, level=1),  # MacFamilyTree label tags
    "addr_longlati": StripSpec(
        tags={"LATI", "LONG", "MAP"}, parent_tag="ADDR"
    ),  # coords on ADDR unsupported by webtrees (direct or via MAP)
    "place_tran": StripSpec(tags={"TRAN"}, parent_tag="PLAC"),  # remove place translations
    "mise": StripSpec(tags={"MISE"}, level=1),
    "object_crop": StripSpec(tags={"CROP"}, parent_tag="OBJE"),  # remove crop rectangles from media objects
    "indi_race": StripSpec(tags={"RACE"}, parent_tag="INDI"),
    "change_date": StripSpec(tags={"CHAN"}, level=2),
    "create_date": StripSpec(tags={"CREA"}, level=2),
    # Runs between cleaners and transformers (before privacy evaluation):
    "deat_placeholder": None,  # remove DEAT/BURI/CREM stubs with no real content
    "noname_indi": None,  # remove INDI records whose every NAME value is empty
    "noname_fam": None,  # remove FAM records where all HUSB/WIFE INDIs are nameless
    "living": None,  # remove INDI records of people likely still alive, and their FAMs
}


# ---------------------------------------------------------------------------
# Transformers
# ---------------------------------------------------------------------------


@dataclass
class TagTransform:
    """Describes a structural tag transformation."""

    rename: str  # new tag name
    add_children: list[tuple[str, str]] = field(
        default_factory=list
    )  # (tag, value) prepended to children


# Each transformer maps source tag → str (simple rename) or TagTransform (rename + add children).
# A None value marks a custom transformer handled separately in the processing loop.
# Note: python-gedcom has no set_tag(); we write to the private _Element__tag
# attribute via name mangling — this is a deliberate workaround.
TRANSFORMERS: dict[str, dict[str, str | TagTransform] | None] = {
    "fid_fsftid": {"_FID": "_FSFTID"},
    "latr_even": {
        "LATR": TagTransform(rename="EVEN", add_children=[("TYPE", "Land Transaction")])
    },
    # Custom transformers (None = handled separately):
    "secg_givn": None,  # append NAME:SECG content to NAME:GIVN and remove SECG
    "addr_to_plac": None,  # merge ADDR value into PLAC (prepend with ", ") for event elements
    "living100y_private": None,
    "living100y_initials": None,
    "died20y_private": None,
    "fam_partner_private": None,
    "marriage20y_private": None,  # remove FAM records where marriage was in the last 20 years
    "born20y_private": None,  # remove INDI records of people born in the last 20 years
}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, dict[str, list[str]]] = {
    "mft_webtrees": {
        "clean": ["dd_mmm_yyyy", "name_placeholder"],
        "strip": [
            "ste",
            "stf",
            "sto",
            "bkm",
            "labl",
            "addr_longlati",
            "place_tran",
            "mise",
            "object_crop",
            "change_date",
            "create_date",
            "indi_race",
        ],
        "transform": ["secg_givn", "fid_fsftid", "latr_even"],
    },
    "mft_sgi": {
        "clean": ["place_slovenia_rm"],
        "transform": ["addr_to_plac", "living100y_private"],
    },
    "mft_public": {
        "clean": ["place_country_only"],
        "transform": ["living100y_initials"],
    },
    "index_cleanup_sgi": {
        "clean": [
            "dd_mmm_yyyy",
            "name_placeholder",
            "place_placeholder",
            "place_duplicate_rm",
        ],
        "strip": ["deat_placeholder", "noname_indi", "noname_fam"],
        "transform": ["born20y_private", "died20y_private", "marriage20y_private", "living100y_private", "fam_partner_private"],
    },
}


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


@dataclass
class CleanerStats:
    processed: int = 0
    fixed: int = 0
    warn: int = 0


@dataclass
class StripperStats:
    processed: int = 0
    removed: int = 0


@dataclass
class TransformerStats:
    processed: int = 0
    transformed: int = 0


def process_file(
    input_path: str,
    output_path: str,
    cleaners: list[str],
    strippers: list[str],
    transformers: list[str],
    warn: bool,
    verbose: bool = False,
) -> tuple[
    dict[str, CleanerStats], dict[str, StripperStats], dict[str, TransformerStats]
]:
    """Returns (per-cleaner stats, per-stripper stats, per-transformer stats)."""
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

    stats: dict[str, CleanerStats] = {c: CleanerStats() for c in cleaners}
    strip_stats: dict[str, StripperStats] = {s: StripperStats() for s in strippers}
    transform_stats: dict[str, TransformerStats] = {
        t: TransformerStats() for t in transformers
    }

    if "dd_mmm_yyyy" in cleaners:
        s = stats["dd_mmm_yyyy"]
        cleaner_fn = CLEANERS["dd_mmm_yyyy"]
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_DATE:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, warning = cleaner_fn(raw)
            if warning:
                s.warn += 1
                if warn:
                    event = _event_label(element)
                    event_str = f" ({event})" if event else ""
                    print(
                        f"WARN [dd_mmm_yyyy]: {warning}{event_str}  — {_record_label(element)}",
                        file=sys.stderr,
                    )
            else:
                if cleaned == "":
                    s.fixed += 1
                    if verbose:
                        print(f"  [clean:dd_mmm_yyyy] {raw!r} -> (removed)  — {_record_label(element)}")
                    element.get_parent_element().get_child_elements().remove(element)
                elif cleaned != raw:
                    s.fixed += 1
                    if verbose:
                        print(f"  [clean:dd_mmm_yyyy] {raw!r} -> {cleaned!r}  — {_record_label(element)}")
                    element.set_value(cleaned)

    if "name_placeholder" in cleaners:
        s = stats["name_placeholder"]
        _NAME_TAGS = (
            gedcom.tags.GEDCOM_TAG_NAME,
            "SURN",
            "GIVN",
            "NICK",
            "MARNM",
            "_MARNM",
        )
        for element in parser.get_element_list():
            if element.get_tag() not in _NAME_TAGS:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, _ = clean_name_placeholder(raw)
            if cleaned != raw:
                s.fixed += 1
                if verbose:
                    print(
                        f"  [clean:name_placeholder] {element.get_tag()} {raw!r} -> {cleaned!r}  — {_record_label(element)}"
                    )
                element.set_value(cleaned)

    if "place_placeholder" in cleaners:
        s = stats["place_placeholder"]
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_PLACE:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, _ = clean_place_placeholder(raw)
            if cleaned != raw:
                s.fixed += 1
                if verbose:
                    print(f"  [clean:place_placeholder] {raw!r} -> {cleaned!r}  — {_record_label(element)}")
                element.set_value(cleaned)

    if "place_slovenia_rm" in cleaners:
        s = stats["place_slovenia_rm"]
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_PLACE:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, _ = clean_place_slovenia_rm(raw)
            if cleaned != raw:
                s.fixed += 1
                if verbose:
                    print(f"  [clean:place_slovenia_rm] {raw!r} -> {cleaned!r}  — {_record_label(element)}")
                element.set_value(cleaned)

    if "place_duplicate_rm" in cleaners:
        s = stats["place_duplicate_rm"]
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_PLACE:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, _ = clean_place_duplicate_rm(raw)
            if cleaned != raw:
                s.fixed += 1
                if verbose:
                    print(f"  [clean:place_duplicate_rm] {raw!r} -> {cleaned!r}  — {_record_label(element)}")
                element.set_value(cleaned)

    if "place_country_only" in cleaners:
        s = stats["place_country_only"]
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_PLACE:
                continue
            raw = element.get_value()
            s.processed += 1
            cleaned, _ = clean_place_country_only(raw)
            if cleaned != raw:
                s.fixed += 1
                if verbose:
                    print(f"  [clean:place_country_only] {raw!r} -> {cleaned!r}  — {_record_label(element)}")
                element.set_value(cleaned)

    for name in transformers:
        if TRANSFORMERS[name] is None:
            continue  # custom transformer — handled separately below
        ts = transform_stats[name]
        tag_map = TRANSFORMERS[name]
        for element in parser.get_element_list():
            old_tag = element.get_tag()
            if old_tag not in tag_map:
                continue
            ts.processed += 1
            spec = tag_map[old_tag]
            if isinstance(spec, TagTransform):
                new_tag = spec.rename
                element._Element__tag = new_tag
                # Prepend each add_children entry before existing children
                for i, (child_tag, child_value) in enumerate(spec.add_children):
                    child = Element(
                        element.get_level() + 1,
                        "",
                        child_tag,
                        child_value,
                        "\n",
                        multi_line=False,
                    )
                    child.set_parent_element(element)
                    element.get_child_elements().insert(i, child)
            else:
                new_tag = spec
                element._Element__tag = new_tag
            ts.transformed += 1
            if verbose:
                print(
                    f"  [transform:{name}] {old_tag} -> {new_tag}  {element.get_value()!r}  — {_record_label(element)}"
                )

    if "secg_givn" in transformers:
        ts = transform_stats["secg_givn"]
        for element in parser.get_element_list():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_NAME:
                continue
            children = element.get_child_elements()
            secg_els = [ch for ch in children if ch.get_tag() == "SECG"]
            if not secg_els:
                continue
            ts.processed += 1
            secg_val = secg_els[0].get_value().strip()
            if not secg_val:
                continue
            givn_els = [ch for ch in children if ch.get_tag() == "GIVN"]
            if givn_els:
                old_givn = givn_els[0].get_value()
                new_givn = (old_givn.strip() + " " + secg_val).strip()
                givn_els[0].set_value(new_givn)
            else:
                givn_el = Element(
                    element.get_level() + 1,
                    "",
                    "GIVN",
                    secg_val,
                    "\n",
                    multi_line=False,
                )
                givn_el.set_parent_element(element)
                children.insert(0, givn_el)
                new_givn = secg_val
                old_givn = ""
            for secg_el in secg_els:
                children.remove(secg_el)
            ts.transformed += 1
            if verbose:
                print(
                    f"  [transform:secg_givn] SECG {secg_val!r} appended to GIVN {old_givn!r} -> {new_givn!r}  — {_record_label(element)}"
                )

    if "addr_to_plac" in transformers:
        ts = transform_stats["addr_to_plac"]
        for element in parser.get_element_list():
            children = element.get_child_elements()
            addr_els = [ch for ch in children if ch.get_tag() == "ADDR"]
            if not addr_els:
                continue
            addr_val = addr_els[0].get_value().strip()
            if not addr_val:
                continue
            ts.processed += 1
            plac_els = [
                ch for ch in children if ch.get_tag() == gedcom.tags.GEDCOM_TAG_PLACE
            ]
            if plac_els:
                old_plac = plac_els[0].get_value()
                old_plac_stripped = old_plac.strip()
                if old_plac_stripped:
                    plac_parts = [p.strip() for p in old_plac_stripped.split(",")]
                    # Check if the first component of PLAC is a substring of ADDR (case-insensitive)
                    if (
                        plac_parts
                        and plac_parts[0]
                        and plac_parts[0].lower() in addr_val.lower()
                    ):
                        plac_parts.pop(0)
                    if plac_parts:
                        new_plac = addr_val + ", " + ", ".join(plac_parts)
                    else:
                        new_plac = addr_val
                else:
                    new_plac = addr_val
                plac_els[0].set_value(new_plac)
            else:
                # No PLAC — create one at the same level as ADDR
                new_el = Element(
                    addr_els[0].get_level(),
                    "",
                    gedcom.tags.GEDCOM_TAG_PLACE,
                    addr_val,
                    "\n",
                    multi_line=False,
                )
                new_el.set_parent_element(element)
                # Insert before ADDR
                addr_idx = children.index(addr_els[0])
                children.insert(addr_idx, new_el)
                new_plac = addr_val
                old_plac = ""
            # Remove all ADDR children from this event
            for addr_el in addr_els:
                children.remove(addr_el)
            ts.transformed += 1
            if verbose:
                print(
                    f"  [transform:addr_to_plac] ADDR {addr_val!r} + PLAC {old_plac!r} -> PLAC {new_plac!r}  — {_record_label(element)}"
                )

    # deat_placeholder must run after cleaners (so placeholder DATEs are already removed)
    # but before privacy transformers (so they see clean DEAT state).
    if "deat_placeholder" in strippers:
        import datetime as _dt_dp
        ss = strip_stats["deat_placeholder"]
        _death_tags = {"DEAT", "BURI", "CREM"}
        _curr_year_dp = _dt_dp.date.today().year
        for element in parser.get_root_child_elements():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue
            # Extract birth year to decide whether a placeholder DEAT is safe to drop.
            # If born more than 100 years ago the stub means "died, date unknown" — keep it.
            birth_year_dp = None
            for ch in element.get_child_elements():
                if ch.get_tag() in ("BIRT", "BAPM", "CHR"):
                    for gch in ch.get_child_elements():
                        if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                            y = _extract_birth_year(gch.get_value())
                            if y is not None:
                                if birth_year_dp is None or ch.get_tag() == "BIRT":
                                    birth_year_dp = y
            old_enough = birth_year_dp is not None and (_curr_year_dp - birth_year_dp) >= 100
            to_remove = []
            for ch in element.get_child_elements():
                if ch.get_tag() not in _death_tags:
                    continue
                if ch.get_value().strip().upper() in ("Y", "N"):
                    continue  # explicit Y/N — keep
                if ch.get_value().strip():
                    continue  # non-empty value (e.g. "Killed in motorcycle accident") — keep
                if old_enough:
                    continue  # born 100+ years ago: stub means dead, not a living template
                # Remove only if tag value is empty AND all children are placeholder/empty.
                # A DATE with no digits (e.g. "._.____", "__.__.____") counts as empty.
                # A PLAC with only underscores/dots/spaces counts as empty.
                def _is_placeholder_child(gch):
                    val = gch.get_value().strip()
                    if not val:
                        return True
                    if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                        return not re.search(r"\d", val)
                    if gch.get_tag() == gedcom.tags.GEDCOM_TAG_PLACE:
                        return not re.search(r"[A-Za-z0-9]", val)
                    return False
                if all(_is_placeholder_child(gch) for gch in ch.get_child_elements()):
                    to_remove.append(ch)
            for ch in to_remove:
                ss.removed += 1
                if verbose:
                    _dp_label = _indi_label(element)
                    if birth_year_dp and "(b." not in _dp_label:
                        _dp_label += f" (b.{birth_year_dp})"
                    print(f"  [strip:deat_placeholder] INDI {element.get_pointer()} — {_dp_label}")
                element.get_child_elements().remove(ch)

    if "born20y_private" in transformers:
        import datetime as _dt_b20

        ts = transform_stats["born20y_private"]
        _curr_year_b20 = _dt_b20.date.today().year
        root_elements = parser.get_root_child_elements()
        indi_list = [el for el in root_elements if el.get_tag() == gedcom.tags.GEDCOM_TAG_INDIVIDUAL]
        ts.processed = len(indi_list)
        indis_to_remove = []
        for el in indi_list:
            birth_year = None
            has_confirmed_death = False
            for ch in el.get_child_elements():
                tag = ch.get_tag()
                if tag in ("BIRT", "BAPM", "CHR"):
                    for gch in ch.get_child_elements():
                        if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                            y = _extract_birth_year(gch.get_value())
                            if y is not None:
                                if birth_year is None or tag == "BIRT":
                                    birth_year = y
                elif tag in ("DEAT", "BURI", "CREM"):
                    val = ch.get_value().strip().upper()
                    if val == "Y":
                        has_confirmed_death = True
                    elif val != "N":
                        child_els = ch.get_child_elements()
                        if not child_els:
                            has_confirmed_death = True  # bare DEAT
                        else:
                            for gch in child_els:
                                d = gch.get_value().strip()
                                if not d:
                                    continue
                                if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                                    if re.search(r"\d", d):
                                        has_confirmed_death = True
                                        break
                                else:
                                    has_confirmed_death = True
                                    break
            if birth_year is not None and (_curr_year_b20 - birth_year) < 20 and not has_confirmed_death:
                indis_to_remove.append(el)
        for el in indis_to_remove:
            ts.transformed += 1
            if verbose:
                print(f"  [transform:born20y_private] INDI {el.get_pointer()} — {_indi_label(el)}")
            root_elements.remove(el)

    if "died20y_private" in transformers:
        import datetime as _dt

        ts = transform_stats["died20y_private"]
        _curr_year = _dt.date.today().year
        _year_re2 = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")

        def _anonymise_indi(element):
            children = element.get_child_elements()
            to_keep = []
            name_kept = False
            for ch in children:
                if ch.get_tag() in ("FAMC", "FAMS", "SEX"):
                    to_keep.append(ch)
                elif ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME and not name_kept:
                    ch.set_value("private")
                    ch.get_child_elements().clear()
                    to_keep.append(ch)
                    name_kept = True
            if not name_kept:
                name_el = Element(
                    element.get_level() + 1,
                    "",
                    gedcom.tags.GEDCOM_TAG_NAME,
                    "private",
                    "\n",
                    multi_line=False,
                )
                name_el.set_parent_element(element)
                to_keep.insert(0, name_el)
            children.clear()
            children.extend(to_keep)

        for element in parser.get_root_child_elements():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue
            ts.processed += 1
            death_year = None
            has_death_event = False
            _name_display = ""
            _birth_date_str = None
            _death_date_str = None
            for ch in element.get_child_elements():
                _tag = ch.get_tag()
                if _tag == gedcom.tags.GEDCOM_TAG_NAME and not _name_display:
                    _name_display = ch.get_value().replace("/", "").strip()
                elif _tag in ("DEAT", "BURI", "CREM"):
                    if ch.get_value().strip().upper() == "N":
                        continue
                    has_death_event = True
                    for gch in ch.get_child_elements():
                        if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                            _m = _year_re2.search(gch.get_value())
                            if _m:
                                y = int(_m.group(1))
                                if death_year is None or y < death_year:
                                    death_year = y
                                    _death_date_str = gch.get_value().strip()
                elif _tag in ("BIRT", "BAPM", "CHR") and _birth_date_str is None:
                    for gch in ch.get_child_elements():
                        if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                            _birth_date_str = gch.get_value().strip()
                            if _tag == "BIRT":
                                break
            if has_death_event and death_year is not None and (_curr_year - death_year) < 20:
                _anonymise_indi(element)
                ts.transformed += 1
                if verbose:
                    _by2 = re.search(r"\b\d{3,4}\b", _birth_date_str).group() if _birth_date_str and re.search(r"\b\d{3,4}\b", _birth_date_str) else None
                    _dy2 = re.search(r"\b\d{3,4}\b", _death_date_str).group() if _death_date_str and re.search(r"\b\d{3,4}\b", _death_date_str) else None
                    parts = [_name_display or "?"]
                    parts.append(f"b.{_by2}" if _by2 else "b.?")
                    parts.append(f"d.{_dy2}" if _dy2 else "d.?")
                    print(
                        f"  [transform:died20y_private] INDI {element.get_pointer()} {' '.join(parts)}"
                    )

    if "marriage20y_private" in transformers:
        import datetime as _dt_m20

        ts = transform_stats["marriage20y_private"]
        _curr_year_m20 = _dt_m20.date.today().year
        _year_re_m20 = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")
        root_elements = parser.get_root_child_elements()
        _ptr_index_m20 = {el.get_pointer(): el for el in root_elements if el.get_pointer()}
        fam_list = [el for el in root_elements if el.get_tag() == gedcom.tags.GEDCOM_TAG_FAMILY]
        ts.processed = len(fam_list)
        fams_to_remove = []
        for fam in fam_list:
            marr_year = None
            for ch in fam.get_child_elements():
                if ch.get_tag() != gedcom.tags.GEDCOM_TAG_MARRIAGE:
                    continue
                for gch in ch.get_child_elements():
                    if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                        m = _year_re_m20.search(gch.get_value())
                        if m:
                            marr_year = int(m.group(1))
                        break
                if marr_year is not None:
                    break
            if marr_year is not None and (_curr_year_m20 - marr_year) < 20:
                fams_to_remove.append(fam)
        for fam in fams_to_remove:
            ts.transformed += 1
            if verbose:
                print(f"  [transform:marriage20y_private] removing {_fam_label(fam, _ptr_index_m20)}")
            root_elements.remove(fam)

    if "living100y_private" in transformers:
        import datetime

        ts = transform_stats["living100y_private"]
        curr_year = datetime.date.today().year
        _private_ptrs: set[str] = set()

        # Build pointer index for relative-based birth year estimation
        _ptr_index_100y = {
            el.get_pointer(): el
            for el in parser.get_root_child_elements()
            if el.get_pointer()
        }

        _exact_year_re = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")

        def _get_exact_birth_year(indi_el):
            """Return birth year only when it is a full 4-digit year (no partial/fill)."""
            for _ch in indi_el.get_child_elements():
                if _ch.get_tag() == gedcom.tags.GEDCOM_TAG_BIRTH:
                    for _gch in _ch.get_child_elements():
                        if _gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                            _m = _exact_year_re.search(_gch.get_value())
                            return int(_m.group(1)) if _m else None
            return None

        def _estimate_birth_year_from_relatives(indi_el) -> tuple[int | None, list[str]]:
            """Estimate birth year from parents (+35y) and children (-35y).
            Returns (earliest_estimate, [description, ...]), or (None, []) if no relatives found.
            Only uses exact birth years of relatives to avoid cascading partial-year errors.
            """
            estimates: list[tuple[int, str]] = []
            for _ch in indi_el.get_child_elements():
                if _ch.get_tag() == "FAMC":  # this person is a child in this family
                    _fam = _ptr_index_100y.get(_ch.get_value().strip())
                    if _fam:
                        for _fch in _fam.get_child_elements():
                            if _fch.get_tag() in (gedcom.tags.GEDCOM_TAG_HUSBAND, gedcom.tags.GEDCOM_TAG_WIFE):
                                _parent = _ptr_index_100y.get(_fch.get_value().strip())
                                if _parent:
                                    _py = _get_exact_birth_year(_parent)
                                    if _py:
                                        _est = _py + 35
                                        _role = "father" if _fch.get_tag() == gedcom.tags.GEDCOM_TAG_HUSBAND else "mother"
                                        estimates.append((_est, f"{_role} {_indi_label(_parent)} +35={_est}"))
                elif _ch.get_tag() == "FAMS":  # this person is a spouse/parent in this family
                    _fam = _ptr_index_100y.get(_ch.get_value().strip())
                    if _fam:
                        for _fch in _fam.get_child_elements():
                            if _fch.get_tag() == "CHIL":
                                _child = _ptr_index_100y.get(_fch.get_value().strip())
                                if _child:
                                    _cy = _get_exact_birth_year(_child)
                                    if _cy:
                                        _est = _cy - 35
                                        estimates.append((_est, f"child {_indi_label(_child)} -35={_est}"))
            if not estimates:
                return None, []
            _min_year = min(e[0] for e in estimates)
            _descs = [e[1] for e in estimates]
            return _min_year, _descs

        for element in parser.get_root_child_elements():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue

            ts.processed += 1
            is_private = False
            name_val = ""
            name_val_display = ""

            for ch in element.get_child_elements():
                if ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME:
                    name_val_display = ch.get_value().replace("/", "").strip()
                    name_val = name_val_display.lower()
                    break

            has_death = False
            birth_year = None
            birth_date_str = None
            death_date_str = None
            if name_val == "living":
                is_private = True
            else:
                for ch in element.get_child_elements():
                    tag = ch.get_tag()
                    if tag in ("DEAT", "BURI", "CREM"):
                        val = ch.get_value().strip().upper()
                        if val == "N":
                            pass  # explicitly alive
                        elif val == "Y":
                            has_death = True  # explicitly dead
                        else:
                            # Dead if: no children (bare DEAT), DATE has a real year,
                            # or any child has a non-empty value (e.g. real PLAC).
                            # A stub template (e.g. MFT "__.__.____" / "____") leaves
                            # only empty children after cleaning → not a real death.
                            child_els = ch.get_child_elements()
                            if not child_els:
                                # Bare DEAT/BURI/CREM with no subfields → real death.
                                has_death = True
                            else:
                                # Has subfields. Only a DATE placeholder (no real year)
                                # marks this as a living-person stub (e.g. MFT template
                                # "__.__.____" cleaned away). NOTE, CONT, real PLAC, etc.
                                # all confirm a real death event.
                                for gch in child_els:
                                    d = gch.get_value().strip()
                                    if not d:
                                        continue  # empty child (e.g. cleaned PLAC "____")
                                    if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                                        # Any digit in the DATE value = real (if partial) death date.
                                        # Pure placeholders like "__.__.____" have no digits.
                                        if re.search(r"\d", d):
                                            has_death = True
                                            if death_date_str is None:
                                                death_date_str = d
                                            break
                                    else:
                                        # NOTE, CONT, real PLAC, SOUR, etc.
                                        has_death = True
                                        break
                    elif tag == "EVEN":
                        for gch in ch.get_child_elements():
                            if (
                                gch.get_tag() == "TYPE"
                                and "death" in gch.get_value().lower()
                            ):
                                has_death = True
                    elif tag in ("BIRT", "BAPM", "CHR"):
                        for gch in ch.get_child_elements():
                            if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                                y = _extract_birth_year(gch.get_value())
                                if y is not None:
                                    # prefer BIRT; only use BAPM/CHR as fallback
                                    if birth_year is None or tag == "BIRT":
                                        birth_year = y
                                        birth_date_str = gch.get_value().strip()

                if not has_death:
                    if birth_year is None:
                        # No birth date at all — try relatives to determine if living
                        _rel_year, _rel_descs = _estimate_birth_year_from_relatives(element)
                        if _rel_year is not None:
                            if (curr_year - _rel_year) < 100:
                                is_private = True
                                _rel_verdict = "private"
                            else:
                                _rel_verdict = "not private"
                            if verbose:
                                _ptr = element.get_pointer()
                                print(f"  [transform:living100y_private] INDI {_ptr} no birth date, relative estimate b.{_rel_year} ({'; '.join(_rel_descs)}) → {_rel_verdict}")
                    elif (curr_year - birth_year) < 100:
                        # If birth year came from a partial date (e.g. "1___" → 1999),
                        # try to confirm using relatives before marking private.
                        if birth_date_str and _PARTIAL_YEAR_RE.search(birth_date_str):
                            _rel_year, _rel_descs = _estimate_birth_year_from_relatives(element)
                            if _rel_year is not None and (curr_year - _rel_year) >= 100:
                                if verbose:
                                    _ptr = element.get_pointer()
                                    print(f"  [transform:living100y_private] INDI {_ptr} partial birth {birth_date_str!r}, relative estimate b.{_rel_year} ({'; '.join(_rel_descs)}) → not private")
                            else:
                                is_private = True
                                if verbose and _rel_year is not None:
                                    _ptr = element.get_pointer()
                                    print(f"  [transform:living100y_private] INDI {_ptr} partial birth {birth_date_str!r}, relative estimate b.{_rel_year} ({'; '.join(_rel_descs)}) → private")
                        else:
                            is_private = True

            if is_private:
                children = element.get_child_elements()
                to_keep = []
                name_kept = False
                for ch in children:
                    if ch.get_tag() in ("FAMC", "FAMS", "SEX"):
                        to_keep.append(ch)
                    elif ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME and not name_kept:
                        ch.set_value("private")
                        ch.get_child_elements().clear()
                        to_keep.append(ch)
                        name_kept = True

                if not name_kept:
                    name_el = Element(
                        element.get_level() + 1,
                        "",
                        gedcom.tags.GEDCOM_TAG_NAME,
                        "private",
                        "\n",
                        multi_line=False,
                    )
                    name_el.set_parent_element(element)
                    to_keep.insert(0, name_el)

                children.clear()
                children.extend(to_keep)

                _private_ptrs.add(element.get_pointer())
                ts.transformed += 1
                if verbose:
                    _by = re.search(r"\b\d{3,4}\b", birth_date_str).group() if birth_date_str and re.search(r"\b\d{3,4}\b", birth_date_str) else None
                    _dy = re.search(r"\b\d{3,4}\b", death_date_str).group() if death_date_str and re.search(r"\b\d{3,4}\b", death_date_str) else None
                    parts = [name_val_display or "?"]
                    parts.append(f"b.{_by}" if _by else "b.?")
                    parts.append(f"d.{_dy}" if _dy else "d.?")
                    print(
                        f"  [transform:living100y_private] INDI {element.get_pointer()} {' '.join(parts)}"
                    )

    if "living100y_initials" in transformers:
        import datetime as _dt2

        ts = transform_stats["living100y_initials"]
        _curr_year2 = _dt2.date.today().year
        _affected_fams: set[str] = set()

        for element in parser.get_root_child_elements():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_INDIVIDUAL:
                continue

            ts.processed += 1
            name_val = ""
            name_val_display = ""

            for ch in element.get_child_elements():
                if ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME:
                    name_val_display = ch.get_value().replace("/", "").strip()
                    name_val = name_val_display.lower()
                    break

            is_name_only = False
            if name_val == "living":
                is_name_only = True
            else:
                has_death = False
                birth_year = None
                birth_date_str = None
                for ch in element.get_child_elements():
                    tag = ch.get_tag()
                    if tag in ("DEAT", "BURI", "CREM"):
                        val = ch.get_value().strip().upper()
                        if val == "N":
                            pass  # explicitly alive
                        elif val == "Y":
                            has_death = True  # explicitly dead
                        else:
                            child_els = ch.get_child_elements()
                            if not child_els:
                                has_death = True
                            else:
                                for gch in child_els:
                                    d = gch.get_value().strip()
                                    if not d:
                                        continue
                                    if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                                        if re.search(r"\d", d):
                                            has_death = True
                                            break
                                    else:
                                        has_death = True
                                        break
                    elif tag == "EVEN":
                        for gch in ch.get_child_elements():
                            if (
                                gch.get_tag() == "TYPE"
                                and "death" in gch.get_value().lower()
                            ):
                                has_death = True
                    elif tag in ("BIRT", "BAPM", "CHR"):
                        for gch in ch.get_child_elements():
                            if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                                y = _extract_birth_year(gch.get_value())
                                if y is not None:
                                    if birth_year is None or tag == "BIRT":
                                        birth_year = y
                                        birth_date_str = gch.get_value().strip()

                if not has_death:
                    if birth_year is not None and (_curr_year2 - birth_year) < 100:
                        is_name_only = True

            def _initials(name: str) -> str:
                """Convert every word in name to its first letter and a dot."""
                return " ".join(w[0].upper() + "." for w in name.split() if w)

            def _shorten_name_value(val: str) -> str:
                """Convert given and surname parts to initials, keeping slashes.

                'Luka /Renko/'        -> 'L. /R./'
                'Ronja Sofija /Renko/' -> 'R. S. /R./'
                'Luka'                -> 'L.'
                """
                m = re.match(r"^(.*?)(/[^/]*/)(.*?)$", val.strip())
                if m:
                    given = _initials(m.group(1).strip())
                    surn = _initials(m.group(2).strip("/ "))
                    parts = [p for p in [given, f"/{surn}/"] if p]
                    return " ".join(parts)
                return _initials(val)

            if is_name_only:
                children = element.get_child_elements()
                to_keep = []
                name_kept = False
                for ch in children:
                    if ch.get_tag() in ("FAMC", "FAMS", "SEX"):
                        if ch.get_tag() == "FAMS":
                            _affected_fams.add(ch.get_value().strip())
                        to_keep.append(ch)
                    elif ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME and not name_kept:
                        ch.set_value(_shorten_name_value(ch.get_value()))
                        name_children = ch.get_child_elements()
                        kept_name_children = []
                        for gch in name_children:
                            if gch.get_tag() == "GIVN":
                                gch.set_value(_initials(gch.get_value()))
                                kept_name_children.append(gch)
                            elif gch.get_tag() == "SURN":
                                gch.set_value(_initials(gch.get_value()))
                                kept_name_children.append(gch)
                        name_children.clear()
                        name_children.extend(kept_name_children)
                        to_keep.append(ch)
                        name_kept = True

                if not name_kept:
                    name_el = Element(
                        element.get_level() + 1,
                        "",
                        gedcom.tags.GEDCOM_TAG_NAME,
                        _shorten_name_value(name_val_display),
                        "\n",
                        multi_line=False,
                    )
                    name_el.set_parent_element(element)
                    to_keep.insert(0, name_el)

                children.clear()
                children.extend(to_keep)

                ts.transformed += 1
                if verbose:
                    parts = [name_val_display or "?"]
                    parts.append(f"b.{birth_date_str}" if birth_date_str else "b.?")
                    print(
                        f"  [transform:living100y_initials] initials INDI {element.get_pointer()} {' '.join(parts)}"
                    )

        # Strip marriage date and place from families of anonymised individuals
        for element in parser.get_root_child_elements():
            if element.get_tag() != "FAM":
                continue
            if element.get_pointer() not in _affected_fams:
                continue
            for ch in element.get_child_elements():
                if ch.get_tag() == "MARR":
                    marr_children = ch.get_child_elements()
                    stripped = [
                        gch for gch in marr_children
                        if gch.get_tag() not in (
                            gedcom.tags.GEDCOM_TAG_DATE,
                            gedcom.tags.GEDCOM_TAG_PLACE,
                        )
                    ]
                    marr_children.clear()
                    marr_children.extend(stripped)
                    if verbose:
                        print(
                            f"  [transform:living100y_initials] stripped MARR date/place from {element.get_pointer()}"
                        )

    if "fam_partner_private" in transformers:
        ts = transform_stats["fam_partner_private"]

        def _indi_is_private(indi_el) -> bool:
            """Return True if the individual has been anonymised (NAME == 'private')."""
            for ch in indi_el.get_child_elements():
                if ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME:
                    return ch.get_value().strip().lower() == "private"
            return False

        _ptr_index_fpp = {
            el.get_pointer(): el
            for el in parser.get_root_child_elements()
            if el.get_pointer()
        }

        _fam_event_tags = {
            gedcom.tags.GEDCOM_TAG_MARRIAGE,
            "MARB", "MARC", "MARL", "MARS",  # marriage bann/contract/license/settlement
            "EVEN",
            "ENGA",  # engagement
        }

        _fams_to_remove = []

        for element in parser.get_root_child_elements():
            if element.get_tag() != gedcom.tags.GEDCOM_TAG_FAMILY:
                continue
            ts.processed += 1
            refs = [
                ch.get_value().strip()
                for ch in element.get_child_elements()
                if ch.get_tag() in (gedcom.tags.GEDCOM_TAG_HUSBAND, gedcom.tags.GEDCOM_TAG_WIFE)
            ]
            if not refs:
                continue

            private_flags = [
                ptr in _ptr_index_fpp and _indi_is_private(_ptr_index_fpp[ptr])
                for ptr in refs
            ]
            all_private = all(private_flags)
            any_private = any(private_flags)

            if not any_private:
                continue

            if all_private:
                # Both partners private — drop the whole family record
                _fams_to_remove.append(element)
                ts.transformed += 1
                if verbose:
                    print(
                        f"  [transform:fam_partner_private] remove FAM {_fam_label(element, _ptr_index_fpp)}"
                    )
            else:
                # Mixed — replace all non-empty event field values with "private"
                changed_any = False
                for ch in element.get_child_elements():
                    if ch.get_tag() not in _fam_event_tags:
                        continue
                    for gch in ch.get_child_elements():
                        if gch.get_value().strip():
                            gch.set_value("private")
                            gch.get_child_elements().clear()
                            changed_any = True
                if changed_any:
                    ts.transformed += 1
                    if verbose:
                        print(
                            f"  [transform:fam_partner_private] redact event fields {_fam_label(element, _ptr_index_fpp)}"
                        )

        _root = parser.get_root_child_elements()
        for _fam in _fams_to_remove:
            if _fam in _root:
                _root.remove(_fam)

    # Regular tag-based strippers (run after cleaners and transformers)
    for name in strippers:
        spec = STRIPPERS[name]
        if spec is None:
            continue  # noname strippers handled below
        ss = strip_stats[name]
        if spec.parent_tag is None and spec.level is None:
            candidates = parser.get_root_child_elements()
            ss.processed = len(candidates)
            to_remove = [el for el in candidates if el.get_tag() in spec.tags]
            for element in to_remove:
                ss.removed += 1
                if verbose:
                    print(
                        f"  [strip:{name}] removing {element.get_tag()} {element.get_pointer()}"
                    )
                candidates.remove(element)
        else:
            all_elements = parser.get_element_list()
            to_remove = []
            for el in all_elements:
                if el.get_tag() not in spec.tags:
                    continue
                if spec.parent_tag is not None:
                    parent = el.get_parent_element()
                    if parent is None or parent.get_tag() != spec.parent_tag:
                        continue
                    if spec.grandparent_tag is not None:
                        grandparent = parent.get_parent_element()
                        if grandparent is None or grandparent.get_tag() != spec.grandparent_tag:
                            continue
                if spec.level is not None:
                    if el.get_level() != spec.level:
                        continue
                to_remove.append(el)

            ss.processed = len(to_remove)
            for element in to_remove:
                ss.removed += 1
                if verbose:
                    label = _record_label(element)
                    msg = f"  [strip:{name}] removing {element.get_tag()}"
                    if spec.grandparent_tag and spec.parent_tag:
                        msg += f" under {spec.grandparent_tag}:{spec.parent_tag}"
                    elif spec.parent_tag:
                        msg += f" under {spec.parent_tag}"
                    msg += f"  — {label}"
                    print(msg)
                parent = element.get_parent_element()
                if parent and element in parent.get_child_elements():
                    parent.get_child_elements().remove(element)

    # Post-strippers: noname_indi and noname_fam (must run last, after cleaners have emptied names)
    _run_noname = "noname_indi" in strippers or "noname_fam" in strippers
    if _run_noname:
        # Build pointer → element index for the current root tree
        root_elements = parser.get_root_child_elements()
        ptr_index: dict[str, object] = {
            el.get_pointer(): el for el in root_elements if el.get_pointer()
        }

        def _indi_is_nameless(indi_el) -> bool:
            names = [
                ch
                for ch in indi_el.get_child_elements()
                if ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME
            ]
            return not names or all(ch.get_value().strip() == "" for ch in names)

        def _indi_is_anonymous(indi_el) -> bool:
            """True if the individual is nameless or has been reduced to 'private'."""
            if _indi_is_nameless(indi_el):
                return True
            names = [
                ch
                for ch in indi_el.get_child_elements()
                if ch.get_tag() == gedcom.tags.GEDCOM_TAG_NAME
            ]
            return all(ch.get_value().strip().lower() == "private" for ch in names)

    if "noname_indi" in strippers:
        ss = strip_stats["noname_indi"]
        indi_list = [
            el
            for el in parser.get_root_child_elements()
            if el.get_tag() == gedcom.tags.GEDCOM_TAG_INDIVIDUAL
        ]
        ss.processed = len(indi_list)
        for el in indi_list:
            if _indi_is_nameless(el):
                ss.removed += 1
                ptr_index.pop(el.get_pointer(), None)  # mark as gone for noname_fam
                if verbose:
                    print(f"  [strip:noname_indi] removing {_record_label(el)}")
                parser.get_root_child_elements().remove(el)

    if "noname_fam" in strippers:
        ss = strip_stats["noname_fam"]
        fam_list = [
            el
            for el in parser.get_root_child_elements()
            if el.get_tag() == gedcom.tags.GEDCOM_TAG_FAMILY
        ]
        ss.processed = len(fam_list)
        for fam in fam_list:
            refs = [
                ch.get_value().strip()
                for ch in fam.get_child_elements()
                if ch.get_tag()
                in (gedcom.tags.GEDCOM_TAG_HUSBAND, gedcom.tags.GEDCOM_TAG_WIFE)
            ]
            # A FAM with no HUSB/WIFE, or where every referenced INDI is nameless
            # (or already stripped), is considered nameless.
            all_nameless = not refs or all(
                ptr not in ptr_index or _indi_is_nameless(ptr_index[ptr])
                for ptr in refs
            )
            if all_nameless:
                ss.removed += 1
                if verbose:
                    print(f"  [strip:noname_fam] removing {_fam_label(fam, ptr_index)}")
                parser.get_root_child_elements().remove(fam)

    if "living" in strippers:
        import datetime

        _cutoff_year = (
            datetime.date.today().year - 110
        )  # born after this → may still be living

        def _indi_is_living(indi_el) -> bool:
            """Return True if this INDI is likely still alive.
            Criteria: no DEAT or BURI event, AND birth year is after cutoff (or unknown).
            """
            for ch in indi_el.get_child_elements():
                tag = ch.get_tag()
                if tag in ("DEAT", "BURI", "CREM"):
                    val = ch.get_value().strip().upper()
                    if val == "N":
                        pass  # explicitly alive
                    elif val == "Y":
                        return False  # explicitly dead
                    else:
                        child_els = ch.get_child_elements()
                        if not child_els:
                            return False  # bare DEAT → dead
                        for gch in child_els:
                            d = gch.get_value().strip()
                            if not d:
                                continue
                            if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                                if re.search(r"\d", d):
                                    return False
                            else:
                                return False  # non-empty non-DATE child (e.g. real PLAC)
                elif tag == "EVEN":
                    for gch in ch.get_child_elements():
                        if (
                            gch.get_tag() == "TYPE"
                            and "death" in gch.get_value().lower()
                        ):
                            return False
            # No death evidence — check birth year
            birth_year = None
            for ch in indi_el.get_child_elements():
                if ch.get_tag() == gedcom.tags.GEDCOM_TAG_BIRTH:
                    for gch in ch.get_child_elements():
                        if gch.get_tag() == gedcom.tags.GEDCOM_TAG_DATE:
                            m = re.search(
                                r"\b(1[0-9]{3}|20[0-2][0-9])\b", gch.get_value()
                            )
                            if m:
                                birth_year = int(m.group(1))
            # If born before cutoff, they are almost certainly dead even without a DEAT record
            if birth_year is not None and birth_year <= _cutoff_year:
                return False
            return True  # no death record + recent/unknown birth = treat as living

        ss = strip_stats["living"]
        root_elements = parser.get_root_child_elements()

        # Collect living INDIs
        living_ptrs: set[str] = set()
        indi_list = [
            el
            for el in root_elements
            if el.get_tag() == gedcom.tags.GEDCOM_TAG_INDIVIDUAL
        ]
        ss.processed = len(indi_list)
        for el in indi_list:
            if _indi_is_living(el):
                ss.removed += 1
                living_ptrs.add(el.get_pointer())
                if verbose:
                    print(f"  [strip:living] removing {_record_label(el)}")
        for ptr in living_ptrs:
            el = next((e for e in root_elements if e.get_pointer() == ptr), None)
            if el:
                root_elements.remove(el)

        # Remove FAMs where ALL referenced spouses are living (or already removed)
        remaining_ptrs = {el.get_pointer() for el in root_elements if el.get_pointer()}
        fam_list = [
            el for el in root_elements if el.get_tag() == gedcom.tags.GEDCOM_TAG_FAMILY
        ]
        fams_to_remove = []
        for fam in fam_list:
            spouse_ptrs = [
                ch.get_value().strip()
                for ch in fam.get_child_elements()
                if ch.get_tag()
                in (gedcom.tags.GEDCOM_TAG_HUSBAND, gedcom.tags.GEDCOM_TAG_WIFE)
            ]
            if not spouse_ptrs:
                continue  # no spouses — leave for noname_fam to handle
            any_living = any(
                ptr not in remaining_ptrs or ptr in living_ptrs for ptr in spouse_ptrs
            )
            if any_living:
                fams_to_remove.append(fam)
        for fam in fams_to_remove:
            ss.removed += 1
            if verbose:
                print(f"  [strip:living] removing {_fam_label(fam, {el.get_pointer(): el for el in root_elements if el.get_pointer()})}")
            root_elements.remove(fam)

    parser.invalidate_cache()
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            for element in parser.get_root_child_elements():
                f.write(_serialize(element))
    except OSError as e:
        print(f"ERROR: could not write '{output_path}': {e}", file=sys.stderr)
        sys.exit(1)

    return stats, strip_stats, transform_stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _process_one_file_batch(
    filename: str,
    input_dir: str,
    output_dir: str,
    cleaners: list[str],
    strippers: list[str],
    transformers: list[str],
    warn: bool,
    verbose: bool,
) -> tuple[str, dict, dict, dict, str, str]:
    """Process a single file for batch mode. Returns (filename, clean_stats, strip_stats,
    transform_stats, captured_stdout, captured_stderr)."""
    # Print before installing the thread-local override so this goes to the
    # real terminal (no override set yet for this thread).
    print(f"Processing: {filename}", file=sys.stderr)

    input_path = os.path.join(input_dir, filename)
    output_path = os.path.join(output_dir, filename)

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    # Install thread-local overrides so only this thread's output is captured
    sys.stdout._local.override = buf_out
    sys.stderr._local.override = buf_err
    try:
        clean_stats, strip_stats, transform_stats = process_file(
            input_path, output_path, cleaners, strippers, transformers, warn, verbose
        )
    except SystemExit:
        clean_stats, strip_stats, transform_stats = {}, {}, {}
    finally:
        del sys.stdout._local.override
        del sys.stderr._local.override

    def _prefix(text: str) -> str:
        if not text:
            return text
        return "\n".join(f"[{filename}] {line}" if line.strip() else line
                         for line in text.splitlines()) + "\n"

    return filename, clean_stats, strip_stats, transform_stats, _prefix(buf_out.getvalue()), _prefix(buf_err.getvalue())


def main():
    # Install thread-local proxies before any output so worker threads can
    # capture their own stdout/stderr without clobbering the global streams.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout = _ThreadLocalStream(sys.stdout)
    sys.stderr = _ThreadLocalStream(sys.stderr)

    parser = argparse.ArgumentParser(
        description="Clean and normalise a GEDCOM file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", nargs="?", help="Input GEDCOM file (.ged)")
    parser.add_argument("output", nargs="?", help="Output GEDCOM file (.ged)")
    parser.add_argument(
        "--input-dir",
        default="",
        metavar="DIR",
        help="Process all .ged files in DIR (batch mode)",
    )
    parser.add_argument(
        "stems",
        nargs="*",
        metavar="STEM",
        help="File stems to process in batch mode (e.g. Košir Hawlina). Default: all files.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        metavar="DIR",
        help="Write processed files to DIR (batch mode)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        metavar="N",
        help="Number of parallel workers in batch mode (default: 16)",
    )
    parser.add_argument(
        "--clean",
        default="",
        metavar="CLEANER[,CLEANER,...]",
        help=f"Comma-separated list of cleaners to apply. Available: {', '.join(CLEANERS)}",
    )
    parser.add_argument(
        "--strip",
        default="",
        metavar="STRIPPER[,STRIPPER,...]",
        help=f"Comma-separated list of strippers to apply. Available: {', '.join(STRIPPERS)}",
    )
    parser.add_argument(
        "--transform",
        default="",
        metavar="TRANSFORMER[,TRANSFORMER,...]",
        help=f"Comma-separated list of transformers to apply. Available: {', '.join(TRANSFORMERS)}",
    )
    parser.add_argument(
        "--preset",
        default="",
        metavar="PRESET",
        help=f"Apply a predefined combination of processors. Available: {', '.join(PRESETS)}",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every conversion performed",
    )

    args = parser.parse_args()

    requested_clean = [c.strip() for c in args.clean.split(",") if c.strip()]
    requested_strip = [s.strip() for s in args.strip.split(",") if s.strip()]
    requested_transform = [t.strip() for t in args.transform.split(",") if t.strip()]

    if args.preset:
        if args.preset not in PRESETS:
            print(
                f"ERROR: unknown preset '{args.preset}'. "
                f"Available: {', '.join(PRESETS)}",
                file=sys.stderr,
            )
            sys.exit(1)
        p = PRESETS[args.preset]

        # merge preset entries with any explicitly requested ones (dedup, preserve order)
        def _merge(base: list[str], extra: list[str]) -> list[str]:
            seen = set(base)
            return base + [x for x in extra if x not in seen]

        requested_clean = _merge(p.get("clean", []), requested_clean)
        requested_strip = _merge(p.get("strip", []), requested_strip)
        requested_transform = _merge(p.get("transform", []), requested_transform)

    if not requested_clean and not requested_strip and not requested_transform:
        print(
            "ERROR: at least one of --clean, --strip, or --transform must be specified.",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown_clean = [c for c in requested_clean if c not in CLEANERS]
    if unknown_clean:
        print(
            f"ERROR: unknown cleaner(s): {', '.join(unknown_clean)}. "
            f"Available: {', '.join(CLEANERS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown_strip = [s for s in requested_strip if s not in STRIPPERS]
    if unknown_strip:
        print(
            f"ERROR: unknown stripper(s): {', '.join(unknown_strip)}. "
            f"Available: {', '.join(STRIPPERS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown_transform = [t for t in requested_transform if t not in TRANSFORMERS]
    if unknown_transform:
        print(
            f"ERROR: unknown transformer(s): {', '.join(unknown_transform)}. "
            f"Available: {', '.join(TRANSFORMERS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.preset:
        print(f"Preset:       {args.preset}", file=sys.stderr)
    if requested_clean:
        print(f"Cleaners:     {', '.join(requested_clean)}", file=sys.stderr)
    if requested_strip:
        print(f"Strippers:    {', '.join(requested_strip)}", file=sys.stderr)
    if requested_transform:
        print(f"Transformers: {', '.join(requested_transform)}", file=sys.stderr)

    # --- Batch mode ---
    if args.input_dir:
        # argparse assigns positional args left-to-right: the first stem typed after
        # the flags ends up in args.input instead of args.stems.  Fold it back.
        if args.input:
            args.stems = [args.input] + list(args.stems)
            args.input = None
        if not args.output_dir:
            print("ERROR: --output-dir is required with --input-dir", file=sys.stderr)
            sys.exit(1)
        os.makedirs(args.output_dir, exist_ok=True)

        try:
            locale.setlocale(locale.LC_COLLATE, ("sl_SI", "UTF-8"))
        except locale.Error:
            locale.setlocale(locale.LC_COLLATE, "")

        all_in_dir = [f for f in os.listdir(args.input_dir) if f.lower().endswith(".ged")]
        if args.stems:
            stems_lower = {s.lower() for s in args.stems}
            ged_files = [f for f in all_in_dir if os.path.splitext(f)[0].lower() in stems_lower]
            missing = [s for s in args.stems if s.lower() not in {os.path.splitext(f)[0].lower() for f in ged_files}]
            if missing:
                print(f"WARNING: stems not found in '{args.input_dir}': {', '.join(missing)}", file=sys.stderr)
        else:
            ged_files = all_in_dir
        ged_files = sorted(ged_files, key=locale.strxfrm)
        if not ged_files:
            print(f"No .ged files found in '{args.input_dir}'.", file=sys.stderr)
            sys.exit(0)

        print(f"Input dir:    {args.input_dir}", file=sys.stderr)
        print(f"Output dir:   {args.output_dir}", file=sys.stderr)
        print(f"Files:        {len(ged_files)}  Workers: {args.workers}", file=sys.stderr)
        print(file=sys.stderr)

        all_results: list[tuple[str, dict, dict, dict]] = []

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {}
            for filename in ged_files:
                futures[executor.submit(
                    _process_one_file_batch,
                    filename,
                    args.input_dir,
                    args.output_dir,
                    requested_clean,
                    requested_strip,
                    requested_transform,
                    True,
                    args.verbose,
                )] = filename
            pending = set(futures.values())
            for future in as_completed(futures):
                pending.discard(futures[future])
                filename, c_stats, s_stats, t_stats, out, err = future.result()
                if out:
                    print(out, end="")
                if err:
                    print(err, end="", file=sys.stderr)
                all_results.append((filename, c_stats, s_stats, t_stats))
                if 0 < len(pending) <= args.workers:
                    print(f"Waiting for: {', '.join(sorted(pending, key=locale.strxfrm))}", file=sys.stderr)

        print("Completed!", file=sys.stderr)

        # --- Summary table ---
        # Collect processor names in processing order: cleaners → transformers → strippers
        proc_names: list[str] = []
        for name in requested_clean + requested_transform + requested_strip:
            if name not in proc_names:
                proc_names.append(name)

        def _changed(c_stats, s_stats, t_stats, name):
            if name in c_stats:
                return c_stats[name].fixed
            if name in s_stats:
                return s_stats[name].removed
            if name in t_stats:
                return t_stats[name].transformed
            return 0

        # Only keep processors that changed something in at least one file
        active_procs = [p for p in proc_names if any(_changed(c, s, t, p) for _, c, s, t in all_results)]

        all_results.sort(key=lambda x: locale.strxfrm(x[0]))

        # Strip .ged/.GED extension for display
        def _stem(fn: str) -> str:
            return fn[:-4] if fn.lower().endswith(".ged") else fn

        file_col = max((len(_stem(r[0])) for r in all_results), default=4)
        file_col = max(file_col, len("File"))
        proc_widths = [max(len(p), 4) for p in active_procs]

        header_parts = [f"{'File':<{file_col}}"]
        for p, w in zip(active_procs, proc_widths):
            header_parts.append(f"{p:>{w}}")
        header = "  ".join(header_parts)
        print(f"\n{header}")
        print("-" * len(header))

        totals = [0] * len(active_procs)
        for filename, c_stats, s_stats, t_stats in all_results:
            row_parts = [f"{_stem(filename):<{file_col}}"]
            for i, (p, w) in enumerate(zip(active_procs, proc_widths)):
                v = _changed(c_stats, s_stats, t_stats, p)
                totals[i] += v
                row_parts.append(f"{v if v else '':>{w}}")
            print("  ".join(row_parts))

        print("-" * len(header))
        total_parts = [f"{'TOTAL':<{file_col}}"]
        for t, w in zip(totals, proc_widths):
            total_parts.append(f"{t:>{w}}")
        print("  ".join(total_parts))

        sys.stdout.flush()
        os._exit(0)

    # --- Single-file mode ---
    if not args.input or not args.output:
        print("ERROR: provide input and output files, or use --input-dir / --output-dir", file=sys.stderr)
        sys.exit(1)

    print(f"Input:        {args.input}", file=sys.stderr)
    print(f"Output:       {args.output}", file=sys.stderr)
    print(file=sys.stderr)

    stats, strip_stats, transform_stats = process_file(
        args.input,
        args.output,
        requested_clean,
        requested_strip,
        requested_transform,
        True,
        args.verbose,
    )

    total_warn = sum(s.warn for s in stats.values())
    if total_warn:
        print(f"{total_warn} value(s) could not be converted.", file=sys.stderr)

    print(f"Saved: {args.output}", file=sys.stderr)

    rows = []
    for name, s in stats.items():
        rows.append(("cleaner", name, str(s.processed), str(s.fixed), str(s.warn)))
    for name, s in strip_stats.items():
        rows.append(("stripper", name, str(s.processed), str(s.removed), "-"))
    for name, s in transform_stats.items():
        rows.append(
            ("transformer", name, str(s.processed), str(s.transformed), "-")
        )

    if rows:
        headers = ("type", "name", "processed", "changed", "warn")
        widths = [
            max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)
        ]
        fmt = "  ".join(
            f"{{:<{w}}}" if i < 2 else f"{{:>{w}}}" for i, w in enumerate(widths)
        )
        print()
        print(fmt.format(*headers))
        print("-" * (sum(widths) + 2 * (len(widths) - 1)))
        for row in rows:
            print(fmt.format(*row))


if __name__ == "__main__":
    main()
