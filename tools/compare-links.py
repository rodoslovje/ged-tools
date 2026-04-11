#!/usr/bin/env python3
"""
compare-links: Compare data.matricula-online.eu links between filtered GED files
and output JSON files (births, families, deaths).

Reports matricula links that are present in the GED but missing from all JSON files,
together with the name and ID of the INDI/FAM record they are attached to.

Usage:
    python tools/compare_links.py <filtered_dir> <output_dir> [STEM ...]

    filtered_dir   Directory containing filtered .ged files.
    output_dir     Directory containing *-births.json, *-families.json, *-deaths.json.
    STEM           Optional list of file stems to process (e.g. Renko Košir).
                   If omitted, all .ged files in filtered_dir are processed.
"""

import argparse
import json
import re
import sys
from pathlib import Path

_MATRICULA_RE = re.compile(r'https?://(?:data\.)?matricula-online\.eu(?:/[^/\s\'"<>\]]+){5,}[^\s\'"<>\]]*', re.IGNORECASE)


def _read_lines(path: Path) -> list[str]:
    """Read GED lines, joining CONC continuations into the preceding line."""
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    joined: list[str] = []
    _conc_re = re.compile(r'^\d+ CONC (.*)$')
    for line in raw:
        m = _conc_re.match(line)
        if m and joined:
            joined[-1] = joined[-1].rstrip("\n") + m.group(1)
        else:
            joined.append(line)
    return joined


# ---------------------------------------------------------------------------
# GED parsing
# ---------------------------------------------------------------------------

def _parse_ged(path: Path) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Return (indi_labels, fam_names, obje_links).

    indi_labels  ptr -> "Name Surname (b.YYYY d.YYYY)"
    fam_names    ptr -> "Husb / Wife"
    obje_links   obje_ptr -> [url, ...]
    """
    indi_labels: dict[str, str] = {}
    fam_names: dict[str, str] = {}
    obje_links: dict[str, list[str]] = {}

    _year_re = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")

    lines = _read_lines(path)

    cur_ptr = None
    cur_tag = None
    cur_name = None
    cur_husb = None
    cur_wife = None
    cur_birth_year = None
    cur_death_year = None
    in_birt = False
    in_deat = False

    def _save_indi():
        if cur_ptr and cur_name is not None:
            dates = []
            if cur_birth_year:
                dates.append(f"b.{cur_birth_year}")
            if cur_death_year:
                dates.append(f"d.{cur_death_year}")
            label = cur_name.strip()
            if dates:
                label += f" ({', '.join(dates)})"
            indi_labels[cur_ptr] = label

    for line in lines:
        parts = line.rstrip("\n").split(" ", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        level = int(parts[0])
        tag = parts[1]
        value = parts[2] if len(parts) > 2 else ""

        if level == 0:
            if cur_tag == "INDI":
                _save_indi()
            elif cur_tag == "FAM" and cur_ptr:
                name_parts = []
                if cur_husb:
                    name_parts.append(cur_husb.strip())
                if cur_wife:
                    name_parts.append(cur_wife.strip())
                fam_names[cur_ptr] = " / ".join(name_parts) if name_parts else ""

            cur_ptr = None
            cur_tag = None
            cur_name = None
            cur_husb = None
            cur_wife = None
            cur_birth_year = None
            cur_death_year = None
            in_birt = False
            in_deat = False

            if tag.startswith("@") and len(parts) > 2:
                cur_ptr = tag
                cur_tag = value.strip()

        elif cur_tag == "INDI":
            if level == 1 and tag == "NAME":
                cur_name = " ".join(value.replace("/", " ").split())
            elif level == 1 and tag == "BIRT":
                in_birt = True
                in_deat = False
            elif level == 1 and tag in ("DEAT", "BURI", "CREM"):
                in_deat = True
                in_birt = False
            elif level == 1:
                in_birt = False
                in_deat = False
            elif level == 2 and tag == "DATE":
                m = _year_re.search(value)
                if m:
                    if in_birt and cur_birth_year is None:
                        cur_birth_year = m.group(1)
                    elif in_deat and cur_death_year is None:
                        cur_death_year = m.group(1)

        elif cur_tag == "FAM":
            if level == 1 and tag == "HUSB":
                cur_husb = indi_labels.get(value.strip(), value.strip())
            elif level == 1 and tag == "WIFE":
                cur_wife = indi_labels.get(value.strip(), value.strip())

        elif cur_tag == "OBJE":
            if level == 1 and tag == "FILE":
                urls = _MATRICULA_RE.findall(value)
                if urls and cur_ptr:
                    obje_links.setdefault(cur_ptr, []).extend(urls)

    # Save last record
    if cur_tag == "INDI":
        _save_indi()
    elif cur_tag == "FAM" and cur_ptr:
        name_parts = []
        if cur_husb:
            name_parts.append(cur_husb.strip())
        if cur_wife:
            name_parts.append(cur_wife.strip())
        fam_names[cur_ptr] = " / ".join(name_parts) if name_parts else ""

    return indi_labels, fam_names, obje_links


def _ged_matricula_by_record(path: Path) -> list[tuple[str, str, str]]:
    """Return list of (ptr, record_label, url) for all matricula links in GED.

    Collects URLs from:
    - Any tag value directly within an INDI/FAM record (NOTE, SOUR, CONT, CONC, FILE, etc.)
    - OBJE/SOUR level-0 records referenced by pointer from INDI/FAM
    """
    indi_labels, fam_names, obje_links = _parse_ged(path)

    # Build ptr_links: any level-0 record (SOUR, OBJE, etc.) → matricula URLs in its content
    ptr_links: dict[str, list[str]] = dict(obje_links)  # already have OBJE FILE urls

    lines = _read_lines(path)

    # First sub-pass: collect URLs from all non-INDI/FAM level-0 records (SOUR, etc.)
    cur_ptr = None
    cur_tag = None
    for line in lines:
        parts = line.rstrip("\n").split(" ", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        level = int(parts[0])
        tag = parts[1]
        value = parts[2] if len(parts) > 2 else ""
        if level == 0:
            cur_ptr = tag if tag.startswith("@") else None
            cur_tag = value.strip() if cur_ptr else None
        elif cur_ptr and cur_tag not in ("INDI", "FAM"):
            for url in _MATRICULA_RE.findall(value):
                ptr_links.setdefault(cur_ptr, []).append(url)

    # Second sub-pass: for each INDI/FAM collect inline URLs + resolve pointer references
    result: list[tuple[str, str, str]] = []
    cur_ptr = None
    cur_tag = None

    for line in lines:
        parts = line.rstrip("\n").split(" ", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        level = int(parts[0])
        tag = parts[1]
        value = parts[2] if len(parts) > 2 else ""

        if level == 0:
            cur_ptr = tag if tag.startswith("@") else None
            cur_tag = value.strip() if cur_ptr else None
        elif cur_tag in ("INDI", "FAM"):
            label = (
                f"INDI {cur_ptr} — {indi_labels.get(cur_ptr, '')}"
                if cur_tag == "INDI"
                else f"FAM  {cur_ptr} — {fam_names.get(cur_ptr, '')}"
            )
            # Inline URL in any tag value
            for url in _MATRICULA_RE.findall(value):
                result.append((cur_ptr, label, url))
            # Pointer reference (@ptr@) — resolve to URLs from that record
            ref = value.strip()
            if ref.startswith("@") and ref.endswith("@"):
                for url in ptr_links.get(ref, []):
                    result.append((cur_ptr, label, url))

    return result


# ---------------------------------------------------------------------------
# JSON link collection
# ---------------------------------------------------------------------------

def _json_matricula_links(output_dir: Path, stem: str) -> set[str]:
    """Return all matricula URLs found in the three JSON files for a stem."""
    links: set[str] = set()
    for kind in ("births", "families", "deaths"):
        path = output_dir / f"{stem}-{kind}.json"
        if not path.exists():
            continue
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"WARN: could not parse {path}: {e}", file=sys.stderr)
            continue
        for record in records:
            for value in record.values():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            links.update(_MATRICULA_RE.findall(item))
                elif isinstance(value, str):
                    links.update(_MATRICULA_RE.findall(value))
    return links


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _normalise(url: str) -> str:
    """Normalise URL for comparison: lowercase, strip trailing slash."""
    return url.lower().rstrip("/")


def process_stem(stem: str, filtered_dir: Path, output_dir: Path) -> list[tuple[str, list[str]]]:
    """Return list of (url, [record_label, ...]) for matricula links missing from all JSON files."""
    ged_path = filtered_dir / f"{stem}.ged"
    if not ged_path.exists():
        matches = list(filtered_dir.glob(f"{stem}.ged")) + list(filtered_dir.glob(f"{stem}.GED"))
        if not matches:
            print(f"WARN: {ged_path} not found", file=sys.stderr)
            return []
        ged_path = matches[0]

    ged_records = _ged_matricula_by_record(ged_path)
    json_links = {_normalise(u) for u in _json_matricula_links(output_dir, stem)}

    # Group records by URL, preserving first-seen URL form
    url_records: dict[str, tuple[str, list[str]]] = {}  # norm_url -> (orig_url, [labels])
    for ptr, label, url in ged_records:
        norm = _normalise(url)
        if norm not in url_records:
            url_records[norm] = (url, [])
        entry_labels = url_records[norm][1]
        if label not in entry_labels:
            entry_labels.append(label)

    return [
        (orig_url, labels)
        for norm, (orig_url, labels) in url_records.items()
        if norm not in json_links
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Find matricula-online.eu links in GED files missing from JSON output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("filtered_dir", help="Directory with filtered .ged files")
    parser.add_argument("output_dir", help="Directory with *-births/families/deaths.json files")
    parser.add_argument("stems", nargs="*", metavar="STEM", help="File stems to process (default: all)")
    args = parser.parse_args()

    filtered_dir = Path(args.filtered_dir)
    output_dir = Path(args.output_dir)

    if args.stems:
        stems = args.stems
    else:
        stems = sorted(p.stem for p in filtered_dir.glob("*.ged"))
        stems += sorted(p.stem for p in filtered_dir.glob("*.GED"))
        stems = sorted(set(stems))

    total_missing = 0
    for stem in stems:
        missing = process_stem(stem, filtered_dir, output_dir)
        if missing:
            print(f"\n=== {stem} — {len(missing)} missing link(s) ===")
            for url, labels in missing:
                print(f"  {url}")
                for label in labels:
                    print(f"    {label}")
            total_missing += len(missing)
        else:
            print(f"{stem}: OK")

    print(f"\nTotal missing: {total_missing} link(s) across {len(stems)} file(s)")


if __name__ == "__main__":
    main()
