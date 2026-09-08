"""Fill the `full_name` column of username-map.csv from Geneanet profile pages.

Geneanet sits behind Cloudflare's bot challenge, so plain HTTP (curl / requests)
gets a 403 "Just a moment..." page. This script drives a real Chromium via
Playwright, which clears the challenge — especially when you are logged in.

Setup (one time):

    pip install playwright
    playwright install chromium

Usage:

    # Headed (default): a browser window opens. On the FIRST run, log in to
    # Geneanet in that window if prompted / solve the Cloudflare check once;
    # the session is saved in the profile dir and reused on later runs.
    python tools/geneanet_fetch_names.py

    # Options
    python tools/geneanet_fetch_names.py \
        --map data/geneanet/username-map.csv \
        --profile-dir ~/.geneanet-playwright \
        --delay 3 --limit 20 --headless --all

Only rows with an empty `full_name` are fetched (resumable); pass --all to
refetch every row. The CSV is rewritten after each row so progress is never
lost. The contributor_id column is left for you to fill / confirm.
"""

import argparse
import csv
import os
import sys
import time
from random import uniform

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright is not installed. Run:\n"
             "  pip install playwright\n"
             "  playwright install chromium")

DEFAULT_MAP = "data/geneanet/username-map.csv"
DEFAULT_PROFILE_DIR = os.path.expanduser("~/.geneanet-playwright")
CHALLENGE_MARKERS = ("just a moment", "verifying you are human", "checking your browser")


def clean_name(text):
    """Strip a Geneanet page title down to the bare display name."""
    if not text:
        return ""
    text = text.strip()
    for sep in (" - Geneanet", " | Geneanet", " – Geneanet", " - geneanet"):
        if text.endswith(sep):
            text = text[: -len(sep)]
    # Titles are sometimes "Family tree of X" / "Arbre de X".
    for prefix in ("Family tree of ", "Genealogy of ", "Arbre de ", "Généalogie de "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip()


def is_challenge(title):
    t = (title or "").lower()
    return any(m in t for m in CHALLENGE_MARKERS)


def wait_past_challenge(page, timeout):
    """Wait until the Cloudflare interstitial clears; return the final title."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = page.title()
        if not is_challenge(title):
            return title
        page.wait_for_timeout(1000)
    return page.title()


def extract_name(page):
    """Best-effort display name from a loaded Geneanet profile page."""
    # 1. Open Graph title is the most reliable when present.
    try:
        og = page.locator('meta[property="og:title"]').first
        if og.count():
            name = clean_name(og.get_attribute("content"))
            if name and not is_challenge(name):
                return name
    except Exception:
        pass
    # 2. First visible <h1>.
    try:
        h1 = page.locator("h1").first
        if h1.count():
            name = clean_name(h1.inner_text(timeout=2000))
            if name and not is_challenge(name):
                return name
    except Exception:
        pass
    # 3. Fall back to the document title.
    name = clean_name(page.title())
    return "" if is_challenge(name) else name


def read_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def write_rows(path, fieldnames, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--map", default=DEFAULT_MAP, help=f"Worksheet CSV (default: {DEFAULT_MAP}).")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR,
                        help="Persistent browser profile dir (keeps you logged in).")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="Base seconds between profiles (plus jitter). Default 3.")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Max seconds to wait for the Cloudflare check per page.")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N fetches (0 = all).")
    parser.add_argument("--headless", action="store_true",
                        help="Run without a visible window (only after login is cached).")
    parser.add_argument("--all", action="store_true",
                        help="Refetch every row, not just those with an empty full_name.")
    args = parser.parse_args()

    if not os.path.exists(args.map):
        sys.exit(f"Worksheet not found: {args.map}")

    fieldnames, rows = read_rows(args.map)
    for col in ("username", "full_name"):
        if col not in (fieldnames or []):
            sys.exit(f"Worksheet is missing the '{col}' column.")

    todo = [r for r in rows
            if (r.get("username") or "").strip()
            and (args.all or not (r.get("full_name") or "").strip())]
    if not todo:
        print("Nothing to fetch (every row already has a full_name).")
        return 0
    if args.limit:
        todo = todo[: args.limit]
    print(f"Fetching {len(todo)} profile(s); window {'hidden' if args.headless else 'visible'}.",
          file=sys.stderr)

    os.makedirs(args.profile_dir, exist_ok=True)
    fetched = 0
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            args.profile_dir,
            headless=args.headless,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, row in enumerate(todo, 1):
            user = row["username"].strip()
            url = (row.get("profile_url") or "").strip() or f"https://en.geneanet.org/profil/{user}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
                title = wait_past_challenge(page, args.timeout)
                if is_challenge(title):
                    print(f"  [{i}/{len(todo)}] {user}: still challenged — "
                          f"solve it in the window (run headed) then rerun.", file=sys.stderr)
                    break
                name = extract_name(page)
            except PWTimeout:
                name = ""
                print(f"  [{i}/{len(todo)}] {user}: timeout", file=sys.stderr)
            except Exception as exc:
                name = ""
                print(f"  [{i}/{len(todo)}] {user}: error {exc}", file=sys.stderr)

            row["full_name"] = name
            write_rows(args.map, fieldnames, rows)  # persist after every row
            print(f"  [{i}/{len(todo)}] {user} -> {name or '(none)'}")
            fetched += 1
            if i < len(todo):
                page.wait_for_timeout(int((args.delay + uniform(0, args.delay)) * 1000))
        ctx.close()

    print(f"\nDone. Filled {fetched} name(s) in {args.map}.")
    print("Review names, set the contributor_id column, then run:", file=sys.stderr)
    print(f"  python tools/geneanet_to_json.py --import-map {args.map}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
