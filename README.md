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

## gedcom_cleaner

Cleans, strips, and transforms GEDCOM files. Processors are applied in order: **cleaners → transformers → strippers**.

```
python tools/gedcom_cleaner.py <input.ged> <output.ged> [OPTIONS]
python tools/gedcom_cleaner.py --input-dir DIR --output-dir DIR [STEM ...] [OPTIONS]
```

### Options

| Option                            | Description                                                                                                                                                 |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--preset PRESET`                 | Apply a predefined combination of processors                                                                                                                |
| `--clean CLEANER[,CLEANER,...]`   | Apply specific formatting cleaners                                                                                                                          |
| `--strip STRIPPER[,STRIPPER,...]` | Strip specific tags or records                                                                                                                              |
| `--transform TRANS[,TRANS,...]`   | Transform specific tags or structures                                                                                                                       |
| `--verbose`                       | Print every change performed (all types)                                                                                                                    |
| `--verbose-clean`                 | Print every change performed by cleaners only                                                                                                               |
| `--verbose-transform`             | Print every change performed by transformers only                                                                                                           |
| `--verbose-strip`                 | Print every change performed by strippers only                                                                                                              |
| `--verbose-private`               | Print every privacy-related decision only (subset of `--verbose-transform`, covering `born*/died*/living*/marriage*/fam_partner*/dead_child*` transformers) |
| `--input-dir DIR`                 | Process all `.ged` files in DIR (batch mode)                                                                                                                |
| `--output-dir DIR`                | Write processed files to DIR (batch mode)                                                                                                                   |
| `--workers N`                     | Parallel workers in batch mode (default: 16)                                                                                                                |
| `STEM ...`                        | File stems to process in batch mode (default: all)                                                                                                          |

At least one of `--preset`, `--clean`, `--strip`, or `--transform` must be specified.

### Processor types

| Type            | Purpose                                                                                                |
| --------------- | ------------------------------------------------------------------------------------------------------ |
| **Cleaner**     | Normalizes field values in-place (e.g. date formats, placeholder text). Record structure is unchanged. |
| **Transformer** | Restructures or reclassifies records — renames tags, moves values, or anonymizes individuals.          |
| **Stripper**    | Removes unwanted tags or entire records. Runs last, after cleaners and transformers.                   |

### Cleaners

| Name                 | Description                                                           |
| -------------------- | --------------------------------------------------------------------- |
| `dd_mmm_yyyy`        | Normalize all dates to DD MMM YYYY format.                            |
| `name_placeholder`   | Replace empty/placeholder names (e.g. `___`, `???`, `XY`) with `NN`; empty segments are filled only on the primary NAME, secondary NAMEs (married, aka …) keep them. |
| `place_placeholder`  | Clear empty/placeholder places.                                       |
| `place_slovenia_rm`  | Remove "Slovenia" / "Slovenija" suffix from places.                   |
| `place_duplicate_rm` | Remove adjacent duplicate components in places.                       |
| `place_country_only` | Reduce place to two parts: place, country (first and last component). |

### Strippers

| Name                              | Description                                                                                                                                                       |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ste`, `stf`, `sto`, `stp`, `bkm` | Strip proprietary MacFamilyTree / other app tags (`_STE`, `_STF`, `_STO`, `_STP`, `_BKM`).                                                                        |
| `labl`                            | Remove `_LABL` (label) tags.                                                                                                                                      |
| `place_tran`                      | Remove TRAN (translation) entries under PLAC tags.                                                                                                                |
| `mise`                            | Remove MISE tags.                                                                                                                                                 |
| `object_crop`                     | Remove CROP entries under OBJE tags.                                                                                                                              |
| `addr_longlati`                   | Remove coordinates (LATI/LONG/MAP) from ADDR tags.                                                                                                                |
| `indi_race`                       | Remove RACE tags from individuals.                                                                                                                                |
| `sour_tags`                       | Remove AGNC (agency) tags from source records.                                                                                                                    |
| `change_date`                     | Remove CHAN (change date) tags.                                                                                                                                   |
| `create_date`                     | Remove CREA (creation date) tags.                                                                                                                                 |
| `deat_placeholder`                | Remove DEAT/BURI/CREM records that are entirely placeholder (no real date, no real place). Skipped for individuals born 100+ years ago. Runs before transformers. |
| `noname_indi`                     | Remove individuals with no valid name.                                                                                                                            |
| `noname_fam`                      | Remove families with no named spouses.                                                                                                                            |
| `living`                          | Remove individuals who are likely still living (no DEAT/BURI/CREM), and their families.                                                                           |

### Transformers

Listed in execution order.

| Name                  | Description                                                                                                                                                                                                                                                                                                                                                                                                     |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fid_fsftid`          | Rename `_FID` to `_FSFTID` (FamilySearch Family Tree ID tag normalisation).                                                                                                                                                                                                                                                                                                                                     |
| `nobi_fact`           | Rename `NOBI` to `FACT`.                                                                                                                                                                                                                                                                                                                                                                                        |
| `sour_filn_abbr`      | Rename `FILN` to `ABBR` inside source records (file number → abbreviation).                                                                                                                                                                                                                                                                                                                                     |
| `sour_date_publ`      | Rename `DATE` to `PUBL` inside source records (publication date fix).                                                                                                                                                                                                                                                                                                                                           |
| `sour_plac_auth`      | Rename `PLAC` to `AUTH` inside source records when `AUTH` is not already present (place → authority/archive).                                                                                                                                                                                                                                                                                                   |
| `latr_even`           | Convert `LATR` to `EVEN` with `TYPE = Land Transaction`.                                                                                                                                                                                                                                                                                                                                                        |
| `prs_even_type`       | Convert `_PRS` (civil partnership) to `EVEN` with `TYPE = Civil Partnership`.                                                                                                                                                                                                                                                                                                                                   |
| `asso_role_rela`      | Convert GEDCOM 7 `ASSO:ROLE` to 5.5.1 `ASSO:RELA` (webtrees only reads `RELA`). Enumerations map to webtrees' translatable lowercase words: `GODP` → `godfather`/`godmother` by the associate's `SEX` (`godparent` if unknown), `WITN` → `witness`, `CLERGY` → `priest`, `FRIEND` → `friend`, `NGHBR` → `neighbor`, `MULTIPLE` → `multiple`, `PARENT/FATH/MOTH/SPOU/HUSB/WIFE/CHIL` → the plain word. `OTHER` uses the `PHRASE` text. `PHRASE` is dropped. `ASSO` under an event (level 2+, not valid 5.5.1) is renamed to webtrees' `_ASSO`; record-level `1 ASSO` stays. `ROLE` inside source citations is left alone. |
| `secg_givn`           | Append `NAME:SECG` content to `NAME:GIVN` and remove the `SECG` tag.                                                                                                                                                                                                                                                                                                                                            |
| `addr_to_plac`        | Merge `ADDR` values into event `PLAC` tags.                                                                                                                                                                                                                                                                                                                                                                     |
| `sour_peri_titl`      | Rename `PERI` to `TITL` inside source records when `TITL` is not already present.                                                                                                                                                                                                                                                                                                                               |
| `born75y_private`     | Anonymize individuals born in the last 75 years regardless of death status: set name to `private` and remove all events. Partial years filled conservatively (e.g. `195_` → 1959).                                                                                                                                                                                                                              |
| `died20y_private`     | Anonymize individuals whose death, burial, or cremation was recorded within the last 20 years (date must be present). Complies with ZVOP-2 post-mortem protection.                                                                                                                                                                                                                                              |
| `living100y_private`  | Anonymize individuals with a birth year under 100 years ago and no death record: set name to `private` and remove all events. Partial years (e.g. `192_`, `19__`) are filled conservatively (underscores → 9) so anyone who could be under 100 is treated as living. Falls back to relative-based birth year estimation (parents +35y, children −35y) when birth date is entirely absent. Complies with ZVOP-2. |
| `living75y_private`   | Same as `living100y_private` but with a 75-year cutoff.                                                                                                                                                                                                                                                                                                                                                         |
| `living100y_initials` | Same detection as `living100y_private` but reduces the full name to initials (e.g. `Luka /Renko/` → `L. /R./`). All events are still removed. Combine with `fam_partner_private` to redact family events too. |
| `fam_partner_private` | A spouse counts as private if its NAME is `<private>` or if any privacy transformer in the same run anonymised it (`born75y`, `died20y`, `living*_private`, `living100y_initials`, `living100y_name_only`). If at least one spouse is private: redact every family event (`MARR`, `MARB/MARC/MARL/MARS`, `ENGA`, `EVEN`) while keeping the family record and its `HUSB/WIFE/CHIL` links: `TYPE` is kept, `ASSO/SOUR/OBJE/CHAN/CREA` are dropped, all other sub-records are replaced with `<private>`. Runs after all individual-level privacy transformers. |

### Presets

A preset is a named combination of processors for a common use case. Can be combined with explicit `--clean`/`--strip`/`--transform` flags.

| Preset              | Description                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `mft_webtrees`      | WebTrees compatibility for MacFamilyTree exports. Cleaners: `dd_mmm_yyyy`, `name_placeholder`. Strippers: `ste`, `stf`, `sto`, `bkm`, `labl`, `addr_longlati`, `place_tran`, `mise`, `object_crop`, `change_date`, `create_date`, `indi_race`, `sour_tags`, `stp`. Transformers: `asso_role_rela`, `secg_givn`, `fid_fsftid`, `latr_even`, `prs_even_type`, `nobi_fact`, `sour_peri_titl`, `sour_date_publ`, `sour_filn_abbr`, `sour_plac_auth`. |
| `mft_sgi`           | Slovenian Genealogy Institute formatting. Cleaners: `place_slovenia_rm`. Transformers: `addr_to_plac`, `living100y_private`.                                                                                                                                                                                                                                                                                                   |
| `mft_public`        | Public sharing from MacFamilyTree exports. Cleaners: `place_country_only`. Transformers: `living100y_initials`, `fam_partner_private`.                                                                                                                                                                                                                                                                                         |
| `index_cleanup_sgi` | Full cleanup and anonymization for public indices (Slovenia). Cleaners: `dd_mmm_yyyy`, `name_placeholder`, `place_placeholder`, `place_duplicate_rm`. Strippers: `noname_indi`, `noname_fam`. Transformers (in order): `died20y_private`, `living100y_private`, `fam_partner_private`.                                                                                                                                         |
| `index_cleanup_cgi` | Full cleanup and anonymization for public indices (Croatia). Same as `index_cleanup_sgi` without `died20y_private`. Cleaners: `dd_mmm_yyyy`, `name_placeholder`, `place_placeholder`, `place_duplicate_rm`. Strippers: `noname_indi`, `noname_fam`. Transformers (in order): `living100y_private`, `fam_partner_private`.                                                                                                      |

### Examples

```bash
# Apply a preset to a single file
python tools/gedcom_cleaner.py family.ged out.ged --preset index_cleanup_sgi

# Combine a preset with an extra stripper
python tools/gedcom_cleaner.py family.ged out.ged --preset mft_webtrees --strip change_date

# Apply individual processors with verbose output
python tools/gedcom_cleaner.py family.ged out.ged --clean dd_mmm_yyyy --transform living100y_private --verbose

# Batch: process all files in a directory
python tools/gedcom_cleaner.py --input-dir data/input --output-dir data/filtered --preset mft_webtrees

# Batch: process specific files only
python tools/gedcom_cleaner.py --input-dir data/input --output-dir data/filtered --preset mft_webtrees Košir Hawlina
```

---

## gedcom_filter

Filters a GEDCOM file to keep only a selected subset of individuals and families relative to a root person, with optional privacy redaction of living individuals.

```
python tools/gedcom_filter.py <input.ged> <output.ged> --person PERSON [OPTIONS]
```

At least one of `--ancestors` or `--descendants` must be specified.

### Options

| Option                         | Description                                                                                                                                                                            |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--person PERSON [PERSON ...]` | One or more root persons: GEDCOM pointer (`@I123@`), full name, partial name, or name with birth year (`"Franc Renko 1901"`). Results are unioned across all specified persons.        |
| `--ancestors`                  | Keep direct ancestors (parents, grandparents, …) and their connecting families                                                                                                         |
| `--descendants`                | Keep all descendants (children, grandchildren, …) and their connecting families                                                                                                        |
| `--related`                    | Also keep all descendants of every ancestor (cousins, aunts/uncles, …). Use with `--ancestors`. Does not include the root person's own descendants unless `--descendants` is also set. |
| `--siblings`                   | Also keep all siblings of every included person                                                                                                                                        |
| `--living-private`             | Redact living individuals: replace name with `private`, remove all events                                                                                                              |
| `--living-name`                | Redact living individuals: keep full name, remove all events                                                                                                                           |
| `--living-initials`            | Redact living individuals: reduce name to initials, remove all events                                                                                                                  |
| `--verbose`                    | Print each kept/removed/redacted record                                                                                                                                                |

`--ancestors` and `--descendants` can be combined to produce a full hourglass tree. `--related` extends `--ancestors` to pull in all blood relatives reachable through the ancestor tree (cousins, aunts, uncles, and their descendants), but stops at the root person so their own children are not added unless `--descendants` is also present. The three `--living-*` flags are mutually exclusive.

### Person specification

| Form                              | Example                                                                    |
| --------------------------------- | -------------------------------------------------------------------------- |
| GEDCOM pointer                    | `@I123@` or `I123`                                                         |
| Full name                         | `"Luka Renko"`                                                             |
| Partial name (surname only)       | `Renko`                                                                    |
| Name with inline birth year       | `"Franc Renko 1901"`                                                       |
| Name + separate birth year option | `--person Renko --birth-year 1952`                                         |
| Multiple persons                  | `--person @I123@ @I456@` or `--person "Franc Renko 1901" "Ana Kovač 1905"` |

If a name matches multiple individuals the tool prints all candidates with their pointers and exits. When specifying multiple persons, use pointers or inline birth years to avoid ambiguity. `--birth-year` applies to all persons and overrides any inline year.

### Living detection

An individual is considered living when they have no `DEAT`, `BURI`, or `CREM` record. The `--living-*` flags apply to all kept individuals that pass this check.

### Examples

```bash
# Keep only ancestors of a person, identified by pointer
python tools/gedcom_filter.py family.ged ancestors.ged --ancestors --person @I123@

# Keep only descendants, identified by full name
python tools/gedcom_filter.py family.ged descendants.ged --descendants --person "Luka Renko"

# Full hourglass tree (ancestors + descendants)
python tools/gedcom_filter.py family.ged hourglass.ged --ancestors --descendants --person @I123@

# All blood relatives reachable through the ancestor tree
python tools/gedcom_filter.py family.ged related.ged --ancestors --related --person @I123@

# Ancestors with their siblings, with name disambiguation
python tools/gedcom_filter.py family.ged out.ged --ancestors --siblings --person Renko --birth-year 1952

# Descendants with living people shown as initials only
python tools/gedcom_filter.py family.ged out.ged --descendants --living-initials --person @I123@

# Full tree, living people fully redacted
python tools/gedcom_filter.py family.ged out.ged --ancestors --descendants --living-private --person @I123@

# Multiple root persons — union of all their ancestors
python tools/gedcom_filter.py family.ged out.ged --ancestors --person @I123@ @I456@

# Descendants of two siblings
python tools/gedcom_filter.py family.ged out.ged --descendants --person @I123@ @I124@
```

---

## gedcom_merge

Merges multiple GEDCOM files into a single output file.

This tool structurally merges multiple trees by concatenating their records. To ensure unique GEDCOM IDs for individuals, families, sources, and objects, it automatically prefixes all pointers with a file-specific identifier (e.g., `@I1@` from the first file becomes `@f1_I1@`, and `@I1@` from the second becomes `@f2_I1@`).

After writing the output it scans the merged individuals and **warns about potential duplicates** — people that appear in more than one input file and should probably be merged into a single record. The scan only reports; it never changes the output. Use `gedcom_dedupe` to actually merge, or resolve them manually in your genealogy program.

```
python tools/gedcom_merge.py <input1.ged> <input2.ged> ... -o <output.ged>
python tools/gedcom_merge.py --input-dir DIR -o <output.ged>
```

### Options

| Option                   | Description                                                                             |
| ------------------------ | --------------------------------------------------------------------------------------- |
| `-o, --output`           | **Required.** Output GEDCOM file.                                                       |
| `--input-dir DIR`        | Merge all `.ged` files in DIR, sorted by name (may be combined with explicit file args). |
| `--no-check-duplicates`  | Skip the potential-duplicate scan.                                                      |
| `--min-confidence N`     | Only report pairs at or above this confidence, 0–100 (default: `50`).                   |
| `--duplicate-report FILE` | Write the duplicate report to FILE instead of stdout.                                   |

### Duplicate detection

Individuals are bucketed by normalized surname + first given name (case-, diacritic-, and punctuation-insensitive, so `Glavnik` matches `GLAVNIK` and `Božič` matches `Bozic`). Placeholder names (`N`, `NN`, `Unknown`, `Neznan`, `Private`, `Living`) are ignored. Each pair in a bucket is then scored on dates; a pair with conflicting `SEX` is never reported.

| Confidence | Signal                                          |
| ---------- | ------------------------------------------------ |
| 100%       | Identical full birth date                        |
| 85% / 65%  | Same birth year (full given-name match / first name only) |
| 70% / 50%  | Same death year, birth unknown                   |
| 60% / 45%  | Same birth year, different day                   |
| 50% / 35%  | Birth years within 2 years                       |
| 30%        | Same full name, no dates on either record        |

`BAPM`/`CHR` stand in for a missing `BIRT`, and `BURI` for a missing `DEAT`.

### Example

```bash
# Merge two family trees into one
python tools/gedcom_merge.py DruzinskoDrevo_Udovic.ged DruzinskoDrevo_Brunskole.ged -o Merged_Tree.ged

# Merge a whole folder and save the duplicate warnings for review
python tools/gedcom_merge.py --input-dir ~/Downloads/Glavnik -o Glavnik_Merged.ged \
    --duplicate-report Glavnik_duplicates.txt
```

---

## gedcom_dedupe

Finds and merges duplicate individuals in a GEDCOM file, typically after it has been combined using `gedcom_merge`.

This tool identifies potential duplicate individuals using the same scoring as `gedcom_merge`'s duplicate scan (case- and diacritic-insensitive names, partial dates tolerated). Matches are grouped transitively — if A matches B and B matches C, all three become one record. For each group it designates one as the "master" record and merges the others into it.

**Merge strategy:**

1.  All references to duplicate individuals (as spouses or children) are updated to point to the master individual.
2.  Family links (`FAMC`/`FAMS`) from duplicates are added to the master record.
3.  All other information from the duplicate records (events, notes, sources) is preserved by converting the entire duplicate record into a `NOTE` on the master record. This allows for manual review and integration.
4.  The original duplicate records are removed from the file.

Only individuals are deduplicated. Duplicate `FAM` records describing the same couple are left in place for manual review.

`python tools/gedcom_dedupe.py <input.ged> -o <output.ged>`

### Options

| Option               | Description                                                                                                    |
| -------------------- | -------------------------------------------------------------------------------------------------------------- |
| `-o, --output`       | **Required.** Output GEDCOM file.                                                                              |
| `--min-confidence N` | Minimum match confidence to merge, 0–100 (default: `85`). See the scoring table under `gedcom_merge`. Use `100` to merge only records with an identical full birth date. |
| `--dry-run`          | List the groups that would be merged without writing output.                                                   |

### Example

```bash
# Find and merge duplicates in a previously merged file
python tools/gedcom_dedupe.py Merged_Tree.ged -o Merged_Deduplicated_Tree.ged

# Preview the merges, only for records with an identical full birth date
python tools/gedcom_dedupe.py Merged_Tree.ged -o /dev/null --min-confidence 100 --dry-run
```

---

## gedcom_to_json

Converts GEDCOM files from `data/filtered/` into JSON output files in `data/output/`. For each input file it produces three JSON files: `<stem>-births.json`, `<stem>-families.json`, and `<stem>-deaths.json`. Contributor metadata is read from `data/contributors.json`.

```
python tools/gedcom_to_json.py [OPTIONS]
```

### Options

| Option                | Description                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `--mode update\|full` | `update` (default): skip files whose JSON output is already up to date. `full`: process all files and overwrite existing JSON. |
| `--workers N`         | Number of parallel workers (default: 16)                                                                                       |

### Examples

```bash
# Incremental update (only changed files)
python tools/gedcom_to_json.py

# Full rebuild
python tools/gedcom_to_json.py --mode full

# Full rebuild with limited parallelism
python tools/gedcom_to_json.py --mode full --workers 4
```

---

## gedcom_query

Queries a GEDCOM file and prints individuals, families, surname summaries, URL matches, address matches, or duplicate media URL reports in a compact human-readable format, with an optional CSV output mode.

```
python tools/gedcom_query.py <input.ged> [OPTIONS]
```

At least one of `--person`, `--surnames`, `--family`, `--url`, `--addr`, or `--duplicate-url` must be specified.

### Options

| Option                  | Description                                                                                                                            |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `--person [PERSON ...]` | List individuals. Without names: all individuals. With names: only the listed persons.                                                 |
| `--ancestors`           | With named `--person`: also include all ancestors.                                                                                     |
| `--descendants`         | With named `--person`: also include all descendants.                                                                                   |
| `--surnames`            | Output unique surnames instead of full person rows.                                                                                    |
| `--location`            | With `--surnames`: also output the place of the oldest occurrence of each surname.                                                     |
| `--family`              | List all families: `Husband Wife ⚭yyyy Place`                                                                                          |
| `--url [URL]`           | List INDI and FAM records whose media (OBJE) subtree contains URL as a case-insensitive substring. Omit value to match any URL.        |
| `--search-events`       | With `--url`: also search within event subtrees (e.g. birth, death) for linked media.                                                  |
| `--addr [ADDR]`         | List INDI and FAM records that have an ADDR value matching ADDR as a case-insensitive substring. Omit value to match any address.      |
| `--duplicate-url`       | List all URLs that appear in more than one media (OBJE) record, grouped by OBJE, with the persons/families referencing each duplicate. |
| `--any-place`           | When birth place is absent, fall back to baptism, residence, or death place (checked in that order).                                   |
| `--csv`                 | Output as CSV instead of plain text                                                                                                    |

`--url` and `--addr` can be combined with a named `--person` (including `--ancestors`/`--descendants`) to filter results within a person set. When combined, the plain `--person` listing is suppressed and replaced by the filtered `--url`/`--addr` output.

### Person specification

Persons can be identified by GEDCOM pointer, full name, or name with birth year to disambiguate:

| Form                 | Example                                        |
| -------------------- | ---------------------------------------------- |
| GEDCOM pointer       | `@I123@`                                       |
| Full name            | `"Luka Renko"`                                 |
| Name with birth year | `"Franc Renko 1901"`                           |
| Multiple persons     | `--person "Franc Renko 1901" "Ana Kovač 1905"` |

### Output formats

**`--person`** (plain text): `Name Surname *yyyy +yyyy Place`

```
Franc Renko *1901 +1964 Stara Sušica,Primorje-Gorski Kotar,Croatia
```

**`--family`** (plain text): `Husband Wife ⚭yyyy Place`

```
Franc Renko Marija Kovač ⚭1925 Zagreb,Zagreb,Croatia
```

**`--surnames`** (plain text):

```
Kovač
Renko
```

**`--surnames --location`** (plain text): `Surname Place`

```
Kovač Zagreb,Zagreb,Croatia
Renko Stara Sušica,Primorje-Gorski Kotar,Croatia
```

**`--url`** (plain text): person row followed by matching URLs

```
Franc Renko *1901 +1964 Stara Sušica,Primorje-Gorski Kotar,Croatia
  https://www.familysearch.org/ark:/61903/...
```

**`--addr`** (plain text): same person/family rows as `--person`/`--family`, filtered to those with a matching ADDR

**`--duplicate-url`** (plain text): duplicate URL, then per-OBJE groups with referencing persons

```
https://www.familysearch.org/ark:/61903/3:1:3QS7-L99C-5C34?view=index&lang=en
  @81553968@
    Valentin Kordiš *1811
    Katarina Kordiš *1792
  @38222851@
    Marija Liker *1803
```

### CSV columns

| Mode                    | Columns                                                                              |
| ----------------------- | ------------------------------------------------------------------------------------ |
| `--person`              | `Name, Surname, Birth, Death, Place`                                                 |
| `--family`              | `Husband_Given, Husband_Surname, Wife_Given, Wife_Surname, Marriage, Marriage_Place` |
| `--surnames`            | `Surname`                                                                            |
| `--surnames --location` | `Surname, Location`                                                                  |
| `--url`                 | `Name, Surname, Birth, Death, Place, URLs`                                           |
| `--addr`                | `Name, Surname, Birth, Death, Place, Addresses`                                      |
| `--duplicate-url`       | `URL, OBJE, Name, Surname, Birth`                                                    |

Output is sorted alphabetically by surname (then given name), respecting Slovenian/Croatian collation (č after c, š after s, ž after z).

### Examples

```bash
# List all individuals
python tools/gedcom_query.py family.ged --person

# List a specific person's ancestors
python tools/gedcom_query.py family.ged --person "Franc Renko 1901" --ancestors

# List a specific person's descendants
python tools/gedcom_query.py family.ged --person "@I123@" --descendants

# List all families
python tools/gedcom_query.py family.ged --family

# List unique surnames among all ancestors, with origin location
python tools/gedcom_query.py family.ged --person "Luka Renko" --ancestors --surnames --location --any-place

# Find all records with a FamilySearch link
python tools/gedcom_query.py family.ged --url familysearch.org

# Find all records with any URL, including those in event subtrees
python tools/gedcom_query.py family.ged --url --search-events

# Find descendants of a person who have a FamilySearch link
python tools/gedcom_query.py family.ged --person "Jakob Renka 1764" --descendants --url familysearch.org

# Find all records at a specific address
python tools/gedcom_query.py family.ged --addr "Sušica 47"

# Find all descendants at any recorded address
python tools/gedcom_query.py family.ged --person "Jakob Renka 1764" --descendants --addr

# Find duplicate media URLs (same scan linked in multiple OBJE records)
python tools/gedcom_query.py family.ged --duplicate-url

# Export individuals to CSV
python tools/gedcom_query.py family.ged --person --csv > persons.csv

# Export URL matches to CSV
python tools/gedcom_query.py family.ged --url familysearch.org --csv > fs-links.csv
```

---

## gedcom_links

Extracts all HTTP/HTTPS links from one or more GEDCOM files and prints frequency statistics grouped by domain and by domain + path prefix.

```
python tools/gedcom_links.py <file.ged> [<file.ged> ...] [OPTIONS]
```

### Options

| Option       | Description                                                          |
| ------------ | -------------------------------------------------------------------- |
| `--top N`    | Show only the top N entries per group                                |
| `--levels N` | Number of path segments to include in domain+path stats (default: 1) |
| `--verbose`  | Print per-file link counts                                           |

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

## compare_links

Checks that every `matricula-online.eu` link in filtered GED files is referenced in the corresponding JSON output. Reports links present in the GED but missing from all three JSON files (`-births`, `-families`, `-deaths`), along with the INDI/FAM record(s) they are attached to.

```
python tools/compare_links.py <filtered_dir> <output_dir> [STEM ...]
```

### Arguments

| Argument       | Description                                                                        |
| -------------- | ---------------------------------------------------------------------------------- |
| `filtered_dir` | Directory containing filtered `.ged` files                                         |
| `output_dir`   | Directory containing `*-births.json`, `*-families.json`, `*-deaths.json`           |
| `STEM ...`     | Optional list of file stems to check (default: all `.ged` files in `filtered_dir`) |

### Example output

```
=== Renko — 2 missing link(s) ===
  https://data.matricula-online.eu/en/...
    INDI @I42@ — Janez Renko (b.1850, d.1920)
Košir: OK
```

---

## reset_ged_mtime

Sets the modification time of each `.ged` file in `data/input/` and `data/filtered/` to the date recorded in `data/output/metadata.json`. Useful after cloning or syncing files to restore mtimes so that `gedcom_to_json` incremental mode (`--mode update`) can skip unchanged files correctly.

```
python tools/reset_ged_mtime.py
```

Matching between JSON contributor names and GED filenames is done case-insensitively with Unicode NFC normalization. No arguments required.

---

## matricula_transcribe

Transcribes a parish book on [Matricula Online](https://data.matricula-online.eu/) into an index spreadsheet, page by page, in the column layout `matricula_to_json` consumes. Claude reads each scan; the handwriting in these books is 18th- and 19th-century Latin or German and is not OCR-able, so **every row is a machine reading and needs a human pass before it is published.**

```
python tools/matricula_transcribe.py <book-url> --interpret NAME --output-dir DIR [OPTIONS]
```

Baptism books (`Krstna knjiga` / `Taufbuch`) produce the K layout, marriage books (`Poročna knjiga` / `Trauungsbuch`) the P layout. Death books are recognised and refused — `matricula_to_json` has no death column set yet.

Requires an Anthropic API key in `ANTHROPIC_API_KEY`. If the key is identity-linked, the API also needs the workspace each request acts in — set `ANTHROPIC_WORKSPACE_ID` or pass `--workspace` (the id is in the Anthropic Console under Settings → Workspaces and looks like `wrkspc_...`). Errors that would hit every page the same way — a rejected key, a missing workspace, an unknown model — stop the run after the first one rather than failing the whole book.

### Arguments

| Argument             | Description                                                                          |
| -------------------- | ------------------------------------------------------------------------------------ |
| `book-url`           | Any page URL of the book, e.g. `https://data.matricula-online.eu/sl/slovenia/ljubljana/podzemelj/04455/?pg=1` |
| `--interpret NAME`   | Value written into the `interpret` column, e.g. `Priimek_Ime` (required)              |
| `--output-dir DIR`   | Directory the `.xlsx` is written to, e.g. `data/matricula/<contributor>` (required)  |

### Options

| Option             | Description                                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------------------------------ |
| `--pages SPEC`     | Pages to transcribe: `6-40`, `6,8,10-12` (default: all)                                                       |
| `--output NAME`    | Output filename (default: derived, e.g. `Indeks P Podzemelj - 1873-1922.xlsx`)                                |
| `--parish NAME`    | Override the parish name written into every row                                                               |
| `--workspace ID`   | Workspace the requests act in (default: `$ANTHROPIC_WORKSPACE_ID`). Required for an identity-linked API key    |
| `--model ID`       | Model to transcribe with (default: `claude-opus-5`)                                                           |
| `--effort LEVEL`   | Reasoning effort: `low`/`medium`/`high`/`xhigh`/`max` (default: `high`)                                        |
| `--max-tokens N`   | Output cap per page (default: 16000)                                                                          |
| `--tiles N`        | Vertical strips sent alongside the full page (default: 2, one per book page; `1` disables strips)              |
| `--workers N`      | Pages transcribed in parallel (default: 4)                                                                    |
| `--delay SECONDS`  | Pause between scan downloads (default: 1.0)                                                                   |
| `--image-dir DIR`  | Scan cache directory (default: `.matricula_images`)                                                           |
| `--cache-dir DIR`  | Transcription cache directory (default: `.matricula_transcribe.cache`)                                        |
| `--rebuild`        | Rebuild the `.xlsx` from cached readings only — no API calls, no downloads                                     |
| `--reuse-cached`   | Reuse readings taken under an older prompt instead of paying to read those pages again                        |
| `--refresh`        | Re-transcribe pages even when a cached reading exists                                                         |
| `--no-flags`       | Leave out the `preveri` column that names the fields the model was unsure of                                  |
| `--dry-run`        | Show the book, the page count and the output path, then stop — no scans fetched, no API calls                 |

### How a page is read

The book's viewer config lists every scan at once, so one request to the book page is enough to learn its length and where each scan lives. Each spread is sent to the model three times over: the whole page for the table's structure, then one vertical strip per book page. The strips exist because the API shrinks anything wider than 1568px, which costs a two-page spread a third of its detail — cutting the spread down the gutter gets that back. The cut is vertical because a register row runs the full width of the spread.

Scans and page transcriptions are both cached on disk. Re-running the same command re-reads only what is missing, so `--pages` can walk a long book in batches, and a page that fails is retried for free on the next run.

**Reading a book is the expensive part; everything else is free.** Every rule that can be applied to a reading after the fact — how `zp. št.` is numbered, which alt surnames are inventions, where a widow's maiden name belongs, place names — runs when the workbook is written, not when the scan is read. So a change to those rules never needs the book read again:

```
# Rebuild the .xlsx from cached readings. No API calls, no downloads.
python tools/matricula_transcribe.py <book-url> \
    --interpret Priimek_Ime --output-dir data/matricula/Priimek --rebuild
```

`--rebuild` fails if any requested page has no cached reading, so it can never quietly give you a partial book; pages whose scan was never downloaded are named and left out. `--reuse-cached` is the softer form: transcribe what is genuinely missing, but reuse readings taken under an older prompt rather than paying to read those pages again. Use it whenever the prompt has changed but you do not need the change badly enough to re-read 200 pages.

The model follows the conventions the project's human indexers use: given names and surnames in modern Slovenian, addresses as village plus house number, dates as `yyyy-mm-dd`.

Fields the model could read only partially are named in a **`preveri` column** of its own, appended after `interpret` — e.g. `priimek ženina, naslov`. Keeping the flags out of `opombe` means a reviewer can sort and filter on them, and deleting the whole column once the index is checked cannot take a real note with it. `matricula_to_json` does not know that header, so it ignores the column entirely. `--no-flags` leaves it out, giving a workbook with exactly the columns a human indexer would write.

Place names take today's Slovenian form, like given names and surnames do — these registers were kept under Austrian administration and name places in German (`Möttling` → `Metlika`, `Gottschee` → `Kočevje`, `Laibach` → `Ljubljana`, `Cilli` → `Celje`). Where the register's own form is worth showing a reviewer, it is kept in brackets after the Slovenian one: `Celje (Cilli) 53`.

`opombe` records only what departs from the ordinary — `vdovec`/`vdova` but never `samski`/`samska`, `nezakonski` but never `zakonski`, and never the officiating priest. For a marriage it runs groom, then bride, then the witnesses they share:

```
Ženin: vdovec, 42 let; starša Matija Jakofčič, kmet, in Barbara Simec. Nevesta: 32 let;
starša Matija Križan, kmet, in Marija Milek. Priči: Matija Jakofčič, Miha Križan.
```

A **widow remarrying** is filed under her maiden surname, not the one the register enters her under. These books name a widow by the surname she carries on the day — her dead husband's — so `priimek neveste` is taken from her father (whom the same entry names as a parent) and the married surname goes into `alt. priimek neveste`. A widow the book calls *Anna Filak*, daughter of Marko Vardijan, is indexed `Vardijan` / `Filak`. The model is asked to do this while reading; a check afterwards catches the entries where it did not, naming `priimek neveste` in the `preveri` column so a reviewer sees it. A groom's surname does not change when he marries, so widowers need none of this.

`zp. št.` is the entry's position on its page and restarts at 1 on the next one, matching the hand-made indexes; it is counted here rather than read off the scan. The `alt. priimek` column holds a literal transcription of the book's own spelling and is left empty when that already matches the modern form — an alt that differs from the surname only by letters outside the Slovenian alphabet (`ć`, `đ`, `ś`) is discarded as an invention rather than a reading.

### Examples

```
# See what the book is and where the output would go — no downloads, no API calls
python tools/matricula_transcribe.py \
    "https://data.matricula-online.eu/sl/slovenia/ljubljana/podzemelj/04455/?pg=1" \
    --interpret Priimek_Ime --output-dir data/matricula/Priimek --dry-run

# Transcribe a first batch of pages
python tools/matricula_transcribe.py \
    "https://data.matricula-online.eu/sl/slovenia/ljubljana/podzemelj/04455/?pg=1" \
    --interpret Priimek_Ime --output-dir data/matricula/Priimek --pages 6-40

# Extend it — pages 6-40 come from the cache, only 41-80 are transcribed
python tools/matricula_transcribe.py \
    "https://data.matricula-online.eu/sl/slovenia/ljubljana/podzemelj/04455/?pg=1" \
    --interpret Priimek_Ime --output-dir data/matricula/Priimek --pages 6-80
```

### Example output

```
book page: https://data.matricula-online.eu/sl/slovenia/ljubljana/podzemelj/04455/?pg=1
title:  Poročna knjiga / Trauungsbuch - 04455 | Podzemelj | ...
book:   P Podzemelj 1873-1922 (marriages)
parish: Podzemelj
pages:  35 of 192 (--pages 6-40)
output: data/matricula/Priimek/Indeks P Podzemelj - 1873-1922.xlsx
scans:  35 downloaded, 0 already cached
pages:  0 read from cache, 35 to transcribe
  [1/35] page 7: 9 entries
  [2/35] page 6: 9 entries
  ...

wrote 312 entries from 35 pages to data/matricula/Priimek/Indeks P Podzemelj - 1873-1922.xlsx
24 entries have something named in the 'preveri' column
every row is a machine reading — check it against the scan before publishing
```

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
