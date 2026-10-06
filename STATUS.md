# STATUS — ged-tools
_Updated 2026-10-06_

Python GEDCOM tools (see README). Last work: `narrative_to_gedcom` (Slovenian narrative PDF → GEDCOM), Geneanet cemeteries per contributor, name placeholders (8.–11. 9.).

**Next**
- [ ] Sort out `.todo/` — old scratch scripts (publish.sh, webtrees-update.sh, extract-surnames.py …): move keepers into `tools/`, delete the rest
- [ ] `.env` stays out of git (check `.gitignore`)

**Decided**
- Run all scripts from the repo root; `data/` is an external symlink
- UTF-8 file with stray cp1250 bytes (BOM, or mostly-valid UTF-8): decode as UTF-8, cp1250 only for the invalid byte runs (`_decode_mostly_utf8`) — never the whole file as cp1250
