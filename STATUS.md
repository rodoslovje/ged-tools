# STATUS — ged-tools
_Updated 2026-11-01_

Python GEDCOM tools (see README). Last work: `source_unit` (parish / cemetery place) on Matricula + Geneanet records (24. 10.), `narrative_to_gedcom` (Slovenian narrative PDF → GEDCOM), Geneanet cemeteries per contributor, name placeholders (8.–11. 9.).

**Next**
- [ ] Sort out `.todo/` — old scratch scripts (publish.sh, webtrees-update.sh, extract-surnames.py …): move keepers into `tools/`, delete the rest
- [ ] `.env` stays out of git (check `.gitignore`)

**Decided**
- `gedcom_to_json` passes DATE values through verbatim; `extract_year` (partner order, estimated birth year) prefers a 4-digit year, takes a 3-digit year only as a whole token, B.C. → negative, non-Gregorian/Julian calendars → None. Output sort stays on the raw date text
- `dd_mmm_yyyy` returns values that already match the GEDCOM date grammar unchanged (ABT/EST/CAL/BEF/AFT, BET…AND, FROM/TO, INT (text), dual years, B.C./BCE, @#D…@ calendars); heuristics only run on everything else
- `source_unit` comes from the converters: Matricula = the parish of the row's book as `matricula-index.json` names it (else the row's župnija), Geneanet = cemetery place minus postcode (else the cemetery name)
- Run all scripts from the repo root; `data/` is an external symlink
- UTF-8 file with stray cp1250 bytes (BOM, or mostly-valid UTF-8): decode as UTF-8, cp1250 only for the invalid byte runs (`_decode_mostly_utf8`) — never the whole file as cp1250
