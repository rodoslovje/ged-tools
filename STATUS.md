# STATUS — ged-tools
_Updated 2026-10-24_

Python GEDCOM tools (see README). Last work: `source_unit` (parish / cemetery place) on Matricula + Geneanet records (24. 10.), `narrative_to_gedcom` (Slovenian narrative PDF → GEDCOM), Geneanet cemeteries per contributor, name placeholders (8.–11. 9.).

**Next**
- [ ] Sort out `.todo/` — old scratch scripts (publish.sh, webtrees-update.sh, extract-surnames.py …): move keepers into `tools/`, delete the rest
- [ ] `.env` stays out of git (check `.gitignore`)

**Decided**
- `source_unit` comes from the converters: Matricula = the parish of the row's book as `matricula-index.json` names it (else the row's župnija), Geneanet = cemetery place minus postcode (else the cemetery name)
- Run all scripts from the repo root; `data/` is an external symlink
- UTF-8 file with stray cp1250 bytes (BOM, or mostly-valid UTF-8): decode as UTF-8, cp1250 only for the invalid byte runs (`_decode_mostly_utf8`) — never the whole file as cp1250
