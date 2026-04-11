"""
reset-ged-mtime.py

Sets the modification time of each *.GED file in the data/input and
data/filtered directories to the date recorded in data/output/metadata.json.

Matching is done by comparing the JSON contributor name against the GED filename
stem, case-insensitively.

Usage:
  python tools/reset-ged-mtime.py
"""

import glob
import json
import os
import time
import unicodedata
from datetime import datetime

JSON_PATH = "data/output/metadata.json"


def normalize(s):
    """Lowercase + NFC Unicode normalization for consistent matching."""
    return unicodedata.normalize("NFC", s).lower()


def load_dates(json_path):
    """Returns a dict mapping normalized name -> datetime."""
    dates = {}
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        for item in data:
            name = normalize(item["contributor"])
            date_str = item["last_modified"]
            try:
                dt = datetime.fromisoformat(date_str)
                dates[name] = dt
            except ValueError:
                print(
                    f"WARNING: Could not parse date '{date_str}' for '{item['contributor']}'"
                )
    return dates


def main():
    if not os.path.isfile(JSON_PATH):
        print(f"ERROR: JSON file not found: {JSON_PATH}")
        return

    dates = load_dates(JSON_PATH)

    target_dirs = ["data/input", "data/filtered"]

    for ged_dir in target_dirs:
        if not os.path.isdir(ged_dir):
            print(f"WARNING: Directory not found: {ged_dir}")
            continue

        ged_files = glob.glob(os.path.join(ged_dir, "*.GED")) + glob.glob(
            os.path.join(ged_dir, "*.ged")
        )

        if not ged_files:
            print(f"No .GED/.ged files found in '{ged_dir}'.")
            continue

        ged_files.sort()

        for ged_path in ged_files:
            stem = normalize(os.path.splitext(os.path.basename(ged_path))[0])
            dt = dates.get(stem)

            if dt is None:
                print(
                    f"WARNING: No date found in JSON for '{os.path.basename(ged_path)}'"
                )
                continue

            ts = dt.timestamp()
            os.utime(ged_path, (ts, ts))
            print(f"  Set mtime of '{os.path.basename(ged_path)}' to {dt}")

    print("\nDone.")


if __name__ == "__main__":
    main()
