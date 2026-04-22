# GEDCOM Tools

Helper scripts for managing genealogy files in the [GEDCOM](https://en.wikipedia.org/wiki/GEDCOM) standard format.

Built with [python-gedcom](https://github.com/nickreynke/python-gedcom).

## Setup

**Requirements:** Python 3.10+

```bash
# Clone the repo
git clone <repo-url>
cd ged-tools

# Create a virtual environment
python3 -m venv .venv          # Mac / Linux
py     -m venv .venv           # Windows

# Activate it
source .venv/bin/activate      # Mac / Linux
.venv\Scripts\activate         # Windows (Command Prompt)
.venv\Scripts\Activate.ps1     # Windows (PowerShell)

# Install dependencies
pip install -r requirements.txt
```

All scripts must be run from the project root directory.

---

## Tools

## gedcom-cleaner

Cleans, strips, and transforms GEDCOM files. Processors are applied in order: **cleaners → transformers → strippers**.

```
python tools/gedcom-cleaner.py <input.ged> <output.ged> [OPTIONS]
python tools/gedcom-cleaner.py --input-dir DIR --output-dir DIR [STEM ...] [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--preset PRESET` | Apply a predefined combination of processors |
| `--clean CLEANER[,CLEANER,...]` | Apply specific formatting cleaners |
| `--strip STRIPPER[,STRIPPER,...]` | Strip specific tags or records |
| `--transform TRANS[,TRANS,...]` | Transform specific tags or structures |
| `--verbose` | Print every change performed (all types) |
| `--verbose-clean` | Print every change performed by cleaners only |
| `--verbose-transform` | Print every change performed by transformers only |
| `--verbose-strip` | Print every change performed by strippers only |
| `--input-dir DIR` | Process all `.ged` files in DIR (batch mode) |
| `--output-dir DIR` | Write processed files to DIR (batch mode) |
| `--workers N` | Parallel workers in batch mode (default: 16) |
| `STEM ...` | File stems to process in batch mode (default: all) |

At least one of `--preset`, `--clean`, `--strip`, or `--transform` must be specified.

### Processor types

| Type | Purpose |
|---|---|
| **Cleaner** | Normalizes field values in-place (e.g. date formats, placeholder text). Record structure is unchanged. |
| **Transformer** | Restructures or reclassifies records — renames tags, moves values, or anonymizes individuals. |
| **Stripper** | Removes unwanted tags or entire records. Runs last, after cleaners and transformers. |

### Cleaners

| Name | Description |
|---|---|
| `dd_mmm_yyyy` | Normalize all dates to DD MMM YYYY format. |
| `name_placeholder` | Clear empty/placeholder names (e.g. `___`, `???`). |
| `place_placeholder` | Clear empty/placeholder places. |
| `place_slovenia_rm` | Remove "Slovenia" / "Slovenija" suffix from places. |
| `place_duplicate_rm` | Remove adjacent duplicate components in places. |
| `place_country_only` | Reduce place to two parts: place, country (first and last component). |

### Strippers

| Name | Description |
|---|---|
| `ste`, `stf`, `sto`, `bkm` | Strip proprietary MacFamilyTree / other app tags. |
| `labl` | Remove `_LABL` (label) tags. |
| `place_tran` | Remove TRAN (translation) entries under PLAC tags. |
| `mise` | Remove MISE tags. |
| `object_crop` | Remove CROP entries under OBJE tags. |
| `addr_longlati` | Remove coordinates (LATI/LONG) from addresses. |
| `indi_race` | Remove RACE tags. |
| `change_date` | Remove CHAN (change date) tags. |
| `create_date` | Remove CREA (creation date) tags. |
| `deat_placeholder` | Remove DEAT/BURI/CREM records that are entirely placeholder (no real date, no real place). Skipped for individuals born 100+ years ago. Runs before transformers. |
| `noname_indi` | Remove individuals with no valid name. |
| `noname_fam` | Remove families with no named spouses. |
| `living` | Remove individuals who are likely still living. |

### Transformers

Listed in execution order.

| Name | Description |
|---|---|
| `born20y_private` | Remove individuals born in the last 20 years with no confirmed death record. |
| `died20y_private` | Anonymize individuals whose death, burial, or cremation was recorded within the last 20 years (date must be present). Complies with ZVOP-2 post-mortem protection. |
| `marriage20y_private` | Remove family records where marriage occurred in the last 20 years. |
| `born75y_private` | Anonymize individuals born in the last 75 years regardless of death status: set name to `private` and remove all events. Partial years filled conservatively (e.g. `195_` → 1959). |
| `living100y_private` | Anonymize individuals with a birth year under 100 years ago and no death record: set name to `private` and remove all events. Falls back to relative-based birth year estimation (parents +35y, children −35y) when birth date is absent or partial. Complies with ZVOP-2. |
| `living100y_initials` | Same detection as `living100y_private` but reduces the full name to initials (e.g. `Luka /Renko/` → `L. /R./`). All events are still removed. |
| `fam_partner_private` | If both spouses are private: remove the entire family record. If one spouse is private: replace all non-empty event field values with `private`. Runs after all individual-level privacy transformers. |
| `dead_child_private` | Anonymize dead individuals who have at least one living parent (no DEAT/BURI/CREM and not born more than 100 years ago). Runs last, after all other individual-level privacy transformers. |
| `secg_givn` | Append NAME:SECG content to NAME:GIVN and remove the SECG tag. |
| `fid_fsftid` | Rename `_FID` to `_FSFTID` (FamilySearch ID tag fix). |
| `latr_even` | Convert LATR to EVEN type="Land Transaction". |
| `addr_to_plac` | Merge ADDR values into event PLAC tags. |

### Presets

A preset is a named combination of processors for a common use case. Can be combined with explicit `--clean`/`--strip`/`--transform` flags.

| Preset | Description |
|---|---|
| `mft_webtrees` | WebTrees compatibility for MacFamilyTree exports. Cleaners: `dd_mmm_yyyy`, `name_placeholder`. Strippers: `ste`, `stf`, `sto`, `bkm`, `labl`, `addr_longlati`, `place_tran`, `mise`, `object_crop`, `change_date`, `create_date`, `indi_race`. Transformers: `secg_givn`, `fid_fsftid`, `latr_even`. |
| `mft_sgi` | Slovenian Genealogy Institute formatting. Cleaners: `place_slovenia_rm`. Transformers: `addr_to_plac`, `living100y_private`. |
| `mft_public` | Public sharing from MacFamilyTree exports. Cleaners: `place_country_only`. Transformers: `living100y_initials`. |
| `index_cleanup_sgi` | Full cleanup and anonymization for public indices (Slovenia). Cleaners: `dd_mmm_yyyy`, `name_placeholder`, `place_placeholder`, `place_duplicate_rm`. Strippers: `noname_indi`, `noname_fam`. Transformers (in order): `born75y_private`, `died20y_private`, `living100y_private`, `fam_partner_private`. |
| `index_cleanup_cgi` | Full cleanup and anonymization for public indices (Croatia). Same as `index_cleanup_sgi` without `died20y_private`. Cleaners: `dd_mmm_yyyy`, `name_placeholder`, `place_placeholder`, `place_duplicate_rm`. Strippers: `noname_indi`, `noname_fam`. Transformers (in order): `born75y_private`, `living100y_private`, `fam_partner_private`. |

### Examples

```bash
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
```

---

## gedcom-filter

Filters a GEDCOM file to keep only a selected subset of individuals and families relative to a root person, with optional privacy redaction of living individuals.

```
python tools/gedcom-filter.py <input.ged> <output.ged> --person PERSON [OPTIONS]
```

At least one of `--ancestors` or `--descendants` must be specified.

### Options

| Option | Description |
|---|---|
| `--person PERSON [PERSON ...]` | One or more root persons: GEDCOM pointer (`@I123@`), full name, partial name, or name with birth year (`"Franc Renko 1901"`). Results are unioned across all specified persons. |
| `--ancestors` | Keep direct ancestors (parents, grandparents, …) and their connecting families |
| `--descendants` | Keep all descendants (children, grandchildren, …) and their connecting families |
| `--related` | Also keep all descendants of every ancestor (cousins, aunts/uncles, …). Use with `--ancestors`. Does not include the root person's own descendants unless `--descendants` is also set. |
| `--siblings` | Also keep all siblings of every included person |
| `--living-private` | Redact living individuals: replace name with `private`, remove all events |
| `--living-name` | Redact living individuals: keep full name, remove all events |
| `--living-initials` | Redact living individuals: reduce name to initials, remove all events |
| `--verbose` | Print each kept/removed/redacted record |

`--ancestors` and `--descendants` can be combined to produce a full hourglass tree. `--related` extends `--ancestors` to pull in all blood relatives reachable through the ancestor tree (cousins, aunts, uncles, and their descendants), but stops at the root person so their own children are not added unless `--descendants` is also present. The three `--living-*` flags are mutually exclusive.

### Person specification

| Form | Example |
|---|---|
| GEDCOM pointer | `@I123@` or `I123` |
| Full name | `"Luka Renko"` |
| Partial name (surname only) | `Renko` |
| Name with inline birth year | `"Franc Renko 1901"` |
| Name + separate birth year option | `--person Renko --birth-year 1952` |
| Multiple persons | `--person @I123@ @I456@` or `--person "Franc Renko 1901" "Ana Kovač 1905"` |

If a name matches multiple individuals the tool prints all candidates with their pointers and exits. When specifying multiple persons, use pointers or inline birth years to avoid ambiguity. `--birth-year` applies to all persons and overrides any inline year.

### Living detection

An individual is considered living when they have no `DEAT`, `BURI`, or `CREM` record. The `--living-*` flags apply to all kept individuals that pass this check.

### Examples

```bash
# Keep only ancestors of a person, identified by pointer
python tools/gedcom-filter.py family.ged ancestors.ged --ancestors --person @I123@

# Keep only descendants, identified by full name
python tools/gedcom-filter.py family.ged descendants.ged --descendants --person "Luka Renko"

# Full hourglass tree (ancestors + descendants)
python tools/gedcom-filter.py family.ged hourglass.ged --ancestors --descendants --person @I123@

# All blood relatives reachable through the ancestor tree
python tools/gedcom-filter.py family.ged related.ged --ancestors --related --person @I123@

# Ancestors with their siblings, with name disambiguation
python tools/gedcom-filter.py family.ged out.ged --ancestors --siblings --person Renko --birth-year 1952

# Descendants with living people shown as initials only
python tools/gedcom-filter.py family.ged out.ged --descendants --living-initials --person @I123@

# Full tree, living people fully redacted
python tools/gedcom-filter.py family.ged out.ged --ancestors --descendants --living-private --person @I123@

# Multiple root persons — union of all their ancestors
python tools/gedcom-filter.py family.ged out.ged --ancestors --person @I123@ @I456@

# Descendants of two siblings
python tools/gedcom-filter.py family.ged out.ged --descendants --person @I123@ @I124@
```

---

## gedcom-to-json

Converts GEDCOM files from `data/filtered/` into JSON output files in `data/output/`. For each input file it produces three JSON files: `<stem>-births.json`, `<stem>-families.json`, and `<stem>-deaths.json`. Contributor metadata is read from `data/contributors.json`.

```
python tools/gedcom-to-json.py [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--mode update\|full` | `update` (default): skip files whose JSON output is already up to date. `full`: process all files and overwrite existing JSON. |
| `--workers N` | Number of parallel workers (default: 16) |

### Examples

```bash
# Incremental update (only changed files)
python tools/gedcom-to-json.py

# Full rebuild
python tools/gedcom-to-json.py --mode full

# Full rebuild with limited parallelism
python tools/gedcom-to-json.py --mode full --workers 4
```

---

## gedcom-links

Extracts all HTTP/HTTPS links from one or more GEDCOM files and prints frequency statistics grouped by domain and by domain + path prefix.

```
python tools/gedcom-links.py <file.ged> [<file.ged> ...] [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--top N` | Show only the top N entries per group |
| `--levels N` | Number of path segments to include in domain+path stats (default: 1) |
| `--verbose` | Print per-file link counts |

### Example output

```
Total links: 142

By domain
---------
    98  matricula-online.eu
    44  familysearch.org

By domain + 1 path segment(s)
------------------------------
    98  data.matricula-online.eu/en
    44  www.familysearch.org/ark
```

---

## compare-links

Checks that every `matricula-online.eu` link in filtered GED files is referenced in the corresponding JSON output. Reports links present in the GED but missing from all three JSON files (`-births`, `-families`, `-deaths`), along with the INDI/FAM record(s) they are attached to.

```
python tools/compare-links.py <filtered_dir> <output_dir> [STEM ...]
```

### Arguments

| Argument | Description |
|---|---|
| `filtered_dir` | Directory containing filtered `.ged` files |
| `output_dir` | Directory containing `*-births.json`, `*-families.json`, `*-deaths.json` |
| `STEM ...` | Optional list of file stems to check (default: all `.ged` files in `filtered_dir`) |

### Example output

```
=== Renko — 2 missing link(s) ===
  https://data.matricula-online.eu/en/...
    INDI @I42@ — Janez Renko (b.1850, d.1920)
Košir: OK
```

---

## reset-ged-mtime

Sets the modification time of each `.ged` file in `data/input/` and `data/filtered/` to the date recorded in `data/output/metadata.json`. Useful after cloning or syncing files to restore mtimes so that `gedcom-to-json` incremental mode (`--mode update`) can skip unchanged files correctly.

```
python tools/reset-ged-mtime.py
```

Matching between JSON contributor names and GED filenames is done case-insensitively with Unicode NFC normalization. No arguments required.

---

## Project structure

```
ged-tools/
├── tools/          # helper scripts
├── tests/          # tests
├── data/
│   ├── input/      # input .ged files (not tracked)
│   ├── filtered/   # cleaned .ged files (not tracked)
│   ├── output/     # JSON output files (not tracked)
│   └── samples/    # sample .ged files for tests
└── requirements.txt
```
