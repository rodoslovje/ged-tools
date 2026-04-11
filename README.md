# GEDCOM Tools

Helper scripts for managing genealogy files in the [GEDCOM](https://en.wikipedia.org/wiki/GEDCOM) standard format.

Built with [python-gedcom](https://github.com/nickreynke/python-gedcom).

## Setup

**Requirements:** Python 3.x

```bash
# Clone the repo
git clone <repo-url>
cd ged-tools

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # on Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Tools

## gedcom-cleaner

Cleans, strips, and transforms GEDCOM files. Processors are applied in order: **cleaners → transformers → strippers**.

```bash
python tools/gedcom-cleaner.py <input.ged> <output.ged> [OPTIONS]

Options:
  --preset PRESET                 Apply a predefined combination of processors
  --clean CLEANER[,CLEANER...]    Apply specific formatting cleaners
  --strip STRIPPER[,STRIPPER...]  Strip specific tags or records
  --transform TRANS[,TRANS...]    Transform specific tags or structures
  --warn                          Print warnings to stderr (e.g. unparsed dates)
  --verbose                       Print every change performed
  --stats                         Print summary statistics at the end
```

### Processor types

| Type | Purpose |
|---|---|
| **Cleaner** | Normalizes field values in-place (e.g. date formats, placeholder text). Structure is unchanged. |
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
| `noname_indi` | Remove individuals with no valid name. |
| `noname_fam` | Remove families with no named spouses. |
| `living` | Remove individuals who are likely still living. |

### Transformers

| Name | Description |
|---|---|
| `secg_givn` | Append NAME:SECG content to NAME:GIVN and remove the SECG tag. |
| `fid_fsftid` | Rename `_FID` to `_FSFTID` (FamilySearch ID tag fix). |
| `latr_even` | Convert LATR to EVEN type="Land Transaction". |
| `addr_to_plac` | Merge ADDR values into event PLAC tags. |
| `living100y_private` | Anonymize individuals with a known birth year under 100 years ago and no death record: set name to `private` and remove all events. Uses birth, baptism, or christening date. Complies with ZVOP-2 for living persons. |
| `living100y_name_only` | Same detection as `living100y_private` but keeps surname and shortens given/middle names to initials (e.g. `Luka Renko` → `L. Renko`). All events are still removed. |
| `died20y_private` | Anonymize individuals whose death, burial, or cremation was recorded within the last 20 years (date must be present). Complies with ZVOP-2 post-mortem protection. |

### Presets

A preset is a named combination of processors for a common use case. Can be combined with explicit `--clean`/`--strip`/`--transform` flags.

| Preset | Description |
|---|---|
| `mft_webtrees` | WebTrees compatibility for MacFamilyTree exports. Cleaners: `dd_mmm_yyyy`, `name_placeholder`. Strippers: `ste`, `stf`, `sto`, `bkm`, `labl`, `addr_longlati`, `place_tran`, `mise`, `object_crop`, `change_date`, `create_date`, `indi_race`. Transformers: `secg_givn`, `fid_fsftid`, `latr_even`. |
| `mft_sgi` | Slovenian Genealogy Institute formatting. Cleaners: `place_slovenia_rm`. Transformers: `addr_to_plac`, `living100y_private`. |
| `mft_public` | Public sharing from MacFamilyTree exports. Cleaners: `place_country_only`. Transformers: `living100y_name_only`. |
| `index_cleanup_sgi` | Full cleanup and anonymization for public indices. Cleaners: `dd_mmm_yyyy`, `name_placeholder`, `place_placeholder`, `place_duplicate_rm`. Strippers: `noname_indi`, `noname_fam`. Transformers: `living100y_private`, `died20y_private`. |

### Examples

```bash
# Apply a preset
python tools/gedcom-cleaner.py family.ged out.ged --preset index_cleanup_sgi

# Combine a preset with an extra stripper
python tools/gedcom-cleaner.py family.ged out.ged --preset mft_webtrees --strip change_date

# Apply individual processors with verbose output
python tools/gedcom-cleaner.py family.ged out.ged --clean dd_mmm_yyyy --transform living100y_private --verbose --stats
```

## gedcom-to-json

Converts GEDCOM files from `data/filtered/` into JSON output files in `data/output/`. For each input file it produces three JSON files: `<stem>-births.json`, `<stem>-families.json`, and `<stem>-deaths.json`. Contributor metadata is read from `data/contributors.json`.

```bash
python tools/gedcom-to-json.py [--mode update|full]

Options:
  --mode update  (default) Skip files whose JSON output is already up to date
  --mode full    Process all files and overwrite existing JSON
```

## gedcom-links

Extracts all HTTP/HTTPS links from one or more GEDCOM files and prints frequency statistics grouped by domain and by domain + path prefix.

```bash
python tools/gedcom-links.py <file.ged> [<file.ged> ...] [OPTIONS]

Options:
  --top N      Show only the top N entries per group
  --levels N   Number of path segments to include in domain+path stats (default: 1)
  --verbose    Print per-file link counts
```

Example output:

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

## compare-links

Checks that every `matricula-online.eu` link in filtered GED files is referenced in the corresponding JSON output. Reports links present in the GED but missing from all three JSON files (`-births`, `-families`, `-deaths`), along with the INDI/FAM record(s) they are attached to.

```bash
python tools/compare-links.py <filtered_dir> <output_dir> [STEM ...]

  filtered_dir   Directory containing filtered .ged files
  output_dir     Directory containing *-births.json, *-families.json, *-deaths.json
  STEM           Optional list of file stems to process (default: all .ged files)
```

Example output:

```
=== Renko — 2 missing link(s) ===
  https://data.matricula-online.eu/en/...
    INDI @I42@ — Janez Renko (b.1850, d.1920)
Košir: OK
```

## reset-ged-mtime

Sets the modification time of each `.ged`/`.GED` file in `data/input/` and `data/filtered/` to the date recorded in `data/output/metadata.json`. Useful after cloning or syncing files to restore mtimes so that `gedcom-to-json` incremental mode (`--mode update`) can skip unchanged files correctly.

```bash
python tools/reset-ged-mtime.py
```

Matching between JSON contributor names and GED filenames is done case-insensitively with Unicode NFC normalization.

## Project Structure

```
ged-tools/
├── tools/          # helper scripts
├── tests/          # tests
├── data/
│   ├── input/      # input .ged files (not tracked)
│   ├── output/     # output files (not tracked)
│   └── samples/    # sample .ged files
└── requirements.txt
```
