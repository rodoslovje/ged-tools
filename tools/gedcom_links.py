#!/usr/bin/env python3
"""
gedcom-links: Extract all HTTP/HTTPS links from one or more GEDCOM files
and print statistics by domain and by domain + first path segment.

Usage:
    python tools/gedcom_links.py <file.ged> [<file.ged> ...]
"""

import argparse
import re
import sys
from collections import Counter
from urllib.parse import urlparse

_URL_RE = re.compile(r'https?://[^\s\'"<>]+', re.IGNORECASE)


def extract_links(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"ERROR: cannot read '{path}': {e}", file=sys.stderr)
        return []
    return _URL_RE.findall(text)


def domain_key(url: str) -> str:
    return urlparse(url).netloc.lower()


def domain_path_key(url: str, levels: int = 1) -> str:
    p = urlparse(url)
    segments = [s for s in p.path.strip("/").split("/") if s][:levels]
    domain = p.netloc.lower()
    return "/".join([domain] + segments) if segments else domain


def print_stats(counter: Counter, title: str, top: int | None = None) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for key, count in counter.most_common(top):
        print(f"  {count:5d}  {key}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract and count HTTP/HTTPS links from GEDCOM files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("files", nargs="+", metavar="FILE", help="Input GEDCOM file(s)")
    parser.add_argument("--top", type=int, default=None, metavar="N", help="Show only top N entries per stat group")
    parser.add_argument("--levels", type=int, default=1, metavar="N", help="Number of path segments to include in domain+path stats (default: 1)")
    args = parser.parse_args()

    all_links: list[str] = []
    for path in args.files:
        links = extract_links(path)
        print(f"{path}: {len(links)} links found")
        all_links.extend(links)

    if not all_links:
        print("No links found.")
        return

    print(f"\nTotal links: {len(all_links)}")

    print_stats(Counter(domain_key(u) for u in all_links), "By domain", args.top)
    print_stats(Counter(domain_path_key(u, args.levels) for u in all_links), f"By domain + {args.levels} path segment(s)", args.top)


if __name__ == "__main__":
    main()
