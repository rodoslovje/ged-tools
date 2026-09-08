"""Convert an indented Slovenian narrative family report (PDF) to GEDCOM.

Written for J. Hojnik, "Dravčani – zgodovina Dravcev in njenih prebivalcev",
whose Part III lists each house's inhabitants as an indented descendant
register.  Generation depth is carried by the horizontal indent of the bullet,
and each entry is one Slovenian sentence of the shape

    Ime Priimek, rojen <datum>, <kraj>, starša <oče> in <mati>, rojena
    <dekliški priimek>, botra <...>, umrl <datum>, star <n> let,
    poročen <datum> z <zakoncem v orodniku>, rojena <datum>, ...

The pipeline runs end to end in this one script:

    1. outline    PDF text + geometry  -> records with a generation level
    2. parse      one record           -> Person + spouses + parent names
    3. build      records + levels     -> INDI / FAM graph
    3.5 dedupe    graph                -> the same person/couple merged
    4. emit       graph                -> GEDCOM 5.5.1

Deduplication belongs here rather than in a pass over the finished GEDCOM,
because at this point we still know each person's stated parents, their
spouses, whether a record is a real entry or only a spouse mention, and the
sentence and page it came from.  It happens in three places:

  * entry_key()        folds entries the book repeats verbatim in two chapters,
                       matching on the whole sentence -- no fuzzy name/date
                       guessing, and two namesakes never share a sentence;
  * dedupe_people()    merges people scored by match_score(), keeping the best
                       version of every fact and both source sentences;
  * dedupe_families()  merges the couples that follow, then reconcile() makes
                       FAMC and CHIL agree again.

tools/gedcom_dedupe.py does the same job generically for any GEDCOM and is
still the right tool for files this script did not produce.

Nothing is silently discarded: every entry keeps its verbatim sentence in
`1 SOUR @S1@ / 3 TEXT`, a merged record keeps the sentence from each chapter,
and a lineage the source contradicts becomes a NOTE rather than a wrong link.
The book is published online, so its address is a multimedia record cited both
by the source and by every person read out of it.

A woman is recorded under her maiden name, with each surname she married into
added as a surname-only NAME of `2 TYPE married` -- the one the book states
("Blanka Kerec, poročena Slana") first, then her husbands'.

Usage:
    python3 tools/narrative_to_gedcom.py Dravčani.pdf 1041 1153 \
        --page-offset 14 -o data/output/Dravcani.ged

    --dump-outline      print the levelled outline and stop
    --no-dedupe         emit every entry as its own person
    --min-confidence N  confidence needed to merge two people (default 85)
    --no-estimates      do not infer ABT birth years or presume deaths
    --as-of YEAR        year to presume death from (default: this year)
    -q / --quiet        summarise skipped records instead of listing them

Records that produce no person are reported on stderr, one warning per line,
so what the grammar cannot read stays visible instead of vanishing.  The
register's own scaffolding -- numbered section headings, family-group
headings -- is skipped silently, since it is expected.  Land records ("21.9.1855
kupi Martin Weisbacher") are kept as a NOTE on the first person their section
lists; the note carries the record's own date, which is often outside that
person's lifetime, because it describes the house rather than them.

A person recorded with nothing but a name ("Jožef Fošnarič, odšel v ZDA") is
still a person, and is admitted when the line has that exact shape: two or
three capitalised words, then a comma, a dash, or the end.

A house is known locally by its owners' nickname, and the book gives it on a
chapter title, in the running head, or in a section heading:

    Dravci 9 in 10: Belšak (po domače Tumpovi)
    2 Pfeiferjevi (po domače Labiševi) - Dravci 1, Dravci 2a in 2b

Settlement names are inflected as freely as personal names -- a birth is in
"Dravci 17", a death "v Dravcih 10" -- so every name is reduced to a stem, and
all its forms are then written the one way: the form that does not look
locative, preferring one the book uses without a preposition.  A spelling used
once or twice that agrees with a far commoner one from its fourth letter back
is treated as a misprint of it -- "Dravcen 11" and "Dravic 18" are Dravci --
while genuinely different villages are left alone.

A house is also named without those words, by the possessive plural the
village uses -- "2 Žepekovi: Habjanič – Milošič", "1 Petrovič (Kosovi)" -- and
a chapter often runs through several households in turn, so such a name binds
to the people written under that section rather than to the address forever.

A heading like "3 Drugi prebivalci te hiše:" introduces people who shared the
house but not the family.  GEDCOM has no household, only families, so the
lineage is broken there -- otherwise the indent would read them as descendants
of the family above -- and each of them instead gets a RESI at the address and
a note saying the house, not blood, is the connection.

Those house names are collected and written next to the address wherever it appears, the
way the family would say it -- "Dravci 9 (pd Tumpovi)".  A declaration naming
one house beats one that names several, so a sub-chapter overrides the
grouping it sits inside.  A "po domače" name attached to a person instead of a
house ("Jožef Vindiš (Kolarjev Pepek)") becomes `2 NICK`, and an alternative
form of a given name ("Janez (Ivan) Fajfar") a second NAME of `2 TYPE aka`.

Where the book never dates a birth, one is estimated from the best evidence
available -- age at death, year of marriage, the parents' or the children's
years -- written as `ABT <year>` with a `2 NOTE ocenjeno iz ...` saying what
it came from.  Anyone born more than a century ago with no recorded death
gets `1 DEAT Y` and `2 NOTE domnevno, preko 100 let`.  Both are off under
--no-estimates.
"""

import argparse
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTTextContainer, LTTextLine

# --------------------------------------------------------------------------
# Stage 0 -- Slovenian micro-grammar: dates, gender agreement, case reversal
# --------------------------------------------------------------------------

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
MONTH_GEN = {  # genitive singular, as used in "rojen januarja 1758"
    "januarja": 1, "februarja": 2, "marca": 3, "aprila": 4, "maja": 5,
    "junija": 6, "julija": 7, "avgusta": 8, "septembra": 9, "oktobra": 10,
    "novembra": 11, "decembra": 12,
}

# ---------------------------------------------------------------- dates
RE_DMY = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\b")
RE_MY = re.compile(r"\b(" + "|".join(MONTH_GEN) + r")\s+(\d{4})\b", re.I)
RE_SPAN = re.compile(r"\b(\d{4})\s*[-–/]\s*(\d{2,4})\b")
RE_Y = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def parse_date(s):
    """Slovenian date phrase -> (GEDCOM date, chars consumed)."""
    s = s.strip()
    approx = bool(re.match(r"^(okrog|okoli|ok\.|pribl\.|verjetno)\s+", s, re.I))
    if approx:
        s = re.sub(r"^\S+\s+", "", s, count=1)
    m = RE_DMY.match(s)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if 1 <= mo <= 12:
            v = f"{d} {MONTHS[mo - 1]} {y}"
            return (f"ABT {v}" if approx else v), m.end()
    m = RE_MY.match(s)
    if m:
        v = f"{MONTHS[MONTH_GEN[m[1].lower()] - 1]} {m[2]}"
        return (f"ABT {v}" if approx else v), m.end()
    m = RE_SPAN.match(s)
    if m:
        a = m[1]
        b = m[2] if len(m[2]) == 4 else a[:2] + m[2]
        return f"BET {a} AND {b}", m.end()
    m = RE_Y.match(s)
    if m:
        return (f"ABT {m[1]}" if approx else m[1]), m.end()
    return None, 0


# ---------------------------------------------------- instrumental -> nominative
VOWELS = "aeiouAEIOUáéíóúeêô"

# Given names whose nominative cannot be derived mechanically.
GIVEN_IRREGULAR = {
    "matijo": "Matija", "matevžem": "Matevž", "vidom": "Vid", "juri": "Juri",
    "jurijem": "Jurij", "lukom": "Luka", "lukas": "Lukas", "miho": "Miha",
    "anžetom": "Anže", "tonetom": "Tone", "jožetom": "Jože",
}


def _masc_nom(tok, lexicon=None):
    """Reverse the masculine instrumental singular (-om / -em / -o)."""
    low = tok.lower()
    if low in GIVEN_IRREGULAR:
        return GIVEN_IRREGULAR[low]
    if low.endswith(("om", "em")):
        stem = tok[:-2]
        # -jem after a consonant is an inserted glide: Voglarjem -> Voglar
        if stem.endswith("j") and len(stem) > 2 and stem[-2] not in VOWELS:
            stem = stem[:-1]
        # a dropped schwa reappears: Plajnščkom -> Plajnšček, Krajncem -> Krajnc
        if lexicon:
            # the corpus lexicon disambiguates o-stems (Jerenko) from bare
            # stems (Krajnc) and restores an elided schwa (Plajnščk -> Plajnšček)
            cands = [stem, stem + "o", stem + "a"]
            if len(stem) > 2:
                cands.append(stem[:-1] + "e" + stem[-1])
            for cand in cands:
                if cand.lower() in lexicon:
                    return lexicon[cand.lower()]
        return stem
    if low.endswith("o"):                       # a-stem: Cafuto -> Cafuta
        return tok[:-1] + "a"
    return tok


def _fem_nom(tok, lexicon=None):
    """Reverse the feminine instrumental singular (-o)."""
    if tok.lower().endswith("o") and len(tok) > 2:
        cand = tok[:-1] + "a"
        return cand
    return tok


def denominalize(phrase, sex, lexicon=None):
    """'Jakobom Plajnščkom' + m -> ('Jakob', 'Plajnšček')."""
    toks = [t for t in phrase.split() if t]
    fn = _masc_nom if sex == "m" else _fem_nom
    out = []
    for i, t in enumerate(toks):
        core = t.strip(".,;:()")
        if not core or not core[0].isupper():
            out.append(core)
            continue
        # surnames that never inflect (foreign / consonant-final feminine)
        if sex == "f" and i > 0 and not core.lower().endswith("o"):
            out.append(core)
        else:
            out.append(fn(core, lexicon))
    given = out[0] if out else ""
    surname = " ".join(out[1:]) if len(out) > 1 else ""
    return given, surname


def norm(s):
    """Casefold and strip diacritics, so Neža/Neza and Jozef/Jožef compare equal."""
    s = "".join(c for c in unicodedata.normalize("NFD", s or "")
                if unicodedata.category(c) != "Mn")
    return s.lower().strip()


# --------------------------------------------------- given-name equivalence
# Slovenian parish records write one man's name several ways, and this book
# follows them: the German and Slovene form of a saint's name, a short form,
# or two names the scribes used interchangeably.  Comparing a child's stated
# parent against the entry it is nested under has to allow for that, or every
# "Ivan" nested under a "Janez" reads as a contradiction.
GIVEN_VARIANTS = [
    {"janez", "ivan", "johan", "johann", "hans", "janko"},
    {"jožef", "josip", "jože", "pepek", "joseph"},
    {"jožefa", "jožica", "pepca"},
    {"jurij", "juri", "georg", "đuro"},
    {"matija", "matevž", "mathaus", "matjaž"},
    {"sebastijan", "fabijan", "boštjan"},
    {"andrej", "andraž"},
    {"alojz", "alojzij", "lojze", "aloisius"},
    {"frančišek", "franc", "francis"},
    {"elizabeta", "liza", "elisabeth", "betka"},
    {"terezija", "treza", "theresia"},
    {"neža", "agnes"},
    {"urša", "uršula", "ursula"},
    {"gera", "jedrt", "gertruda"},
    {"ludmila", "ljudmila"},
    {"radoslav", "slavko"},
    {"vid", "vito"},
    {"marija", "maria", "mimika"},
    {"apolonija", "polona"},
    {"antonija", "tonika"},
]
_VARIANT = {}
for _grp in GIVEN_VARIANTS:
    _g = {norm(x) for x in _grp}          # keys must match norm()'s output
    for _n in _g:
        _VARIANT.setdefault(_n, set()).update(_g)


def same_given(a, b):
    """Do two given names denote the same person's name?

    Tolerates the variants above, the "Sebastijan/Fabijan" double form the
    book uses when the records disagree, and truncation.
    """
    a, b = norm(a), norm(b)
    if not a or not b:
        return True                      # nothing stated -> nothing to contradict
    alts_a = {x for p in a.split("/") for x in (_VARIANT.get(p.strip(), {p.strip()}))}
    alts_b = {x for p in b.split("/") for x in (_VARIANT.get(p.strip(), {p.strip()}))}
    if alts_a & alts_b:
        return True
    return any(x.startswith(y) or y.startswith(x)
               for x in alts_a for y in alts_b if len(x) > 3 and len(y) > 3)


# ------------------------------------------------------------------ gender
FEM_HINT = re.compile(r"\b(rojena|umrla|poročena|stara|krščena|vdova|hči|hčerka|dekla|mama)\b")
MASC_HINT = re.compile(r"\b(rojen|umrl|poročen|star|krščen|vdovec|sin|oče|hlapec)\b")


def sex_of(text):
    f = len(FEM_HINT.findall(text))
    m = len(MASC_HINT.findall(text))
    return "f" if f > m else ("m" if m > f else "")


# --------------------------------------------------------------------------
# Stage 1 -- PDF -> indented outline records
# --------------------------------------------------------------------------

BULLET_CHARS = "−–—-•"
LEFT_MARGIN = 56.6      # level 0
BULLET_X0 = 74.6        # level 1
STEP = 36.0             # one generation
CONT = 18.0             # wrapped-line indent
SIZE_TOL = 1.0          # a body line's size may differ from the page body by this
Y_TOL = 2.5             # bullet glyph vs. its body line
LINE_PITCH = 20.0       # bigger vertical gap => paragraph break


@dataclass
class Rec:
    page: int
    level: int
    text: str
    bullet: bool
    y: float
    chapter: str = ""     # the house this chapter is about, "Dravci 9 in 10"
    vulgo: str = ""       # its "po domače" name, "Tumpovi"


# A house is known by its owners' nickname as much as by its number, and the
# book gives it three ways: on a chapter title page, in the running head, and
# in a section heading inside the text.
#
#   Dravci 9 in 10: Belšak (po domače Tumpovi)
#   III. del: Soviče 10: Petrovič (po domače Kosovi)
#   2 Pfeiferjevi (po domače Labiševi) – Dravci 1, Dravci 2a in 2b
VULGO = re.compile(r"\(\s*(?:po\s+doma[čc]e|vulgo|pd)\s+([^)]+?)\s*\)", re.I)
CHAPTER_HEAD = re.compile(
    r"^(?:(?:I{1,3}|IV)\.\s*del:\s*)?"
    r"([A-ZŠČŽĐĆ][\wčšžćđ]*(?:\s+[a-zA-ZŠČŽĐĆ][\wčšžćđ]*)?\s+"
    r"\d+[a-z]?(?:\s*(?:in|-|–)\s*\d+[a-z]?)*)\s*:")
HOUSE_DECL = re.compile(r"^\d+\s+[^(]*\(\s*(?:po\s+doma[čc]e|vulgo)\s+([^)]+?)\s*\)"
                        r"(?:\s*[-–—]\s*(.+))?$", re.I)
# "Dravci 1, Dravci 2a in 2b" -> the individual houses
ADDR = re.compile(r"([A-ZŠČŽĐĆ][\wčšžćđ]*(?:\s+[a-zA-ZŠČŽĐĆ][\wčšžćđ]*)?)?\s*"
                  r"(\d+[a-z]?)")


# A house is also named without the words "po domače", by the possessive
# plural the village uses: "2 Žepekovi: Habjanič – Milošič", "1 Gnilškovi",
# "1 Petrovič (Kosovi)".  Plain surnames ("2 Horvat", "1 Krajnc") are not.
POSSESSIVE = r"[A-ZŠČŽĐĆ][\wčšžćđ]*(?:ovi|evi|ovci|ovo|evo)"
SECTION_HOUSE = re.compile(
    r"^\d+\s+(" + POSSESSIVE + r"(?:\s*[-–—]\s*" + POSSESSIVE + r")*)"
    r"\s*(?::|[-–—]|$|\s+v\s+)")
PAREN_HOUSE = re.compile(r"\((" + POSSESSIVE + r")\)\s*$")


# "3 Drugi prebivalci te hiše:", "4 Viničarji na Kovačevem, Dravci 11:" --
# everyone after this heading shared the house but not the family.  The indent
# keeps counting, so without a break they would be read as descendants of
# whoever the section above ended on.
CORESIDENT = re.compile(
    r"^\d+\s+(drugi|ostali|vini[čc]arji|najemniki|posli|slu[žz]in[čc]ad)\b", re.I)
# The same thing said in prose: "Konec 19. stoletja najemniki pri
# Weisbacherjevih HLUPIČEVI:", "Več otrok na služenju pri Letičevih:",
# "Pri Janezu Letiču sta služila".
TENANTS = re.compile(
    r"\b(najemnik\w*|vini[čc]arj\w*|slu[žz]il\w*|slu[žz]enj\w*|slu[žz]ita|"
    r"posli|slu[žz]in[čc]ad|stanovalc\w*|prebivalc\w*)\b", re.I)


def coresident_heading(text):
    """(True, address) when a heading introduces the household's non-family."""
    if CORESIDENT.match(text):
        return True, " ".join(_expand_addrs(text))
    # not a person's own entry, but names tenants or servants of the house
    if (TENANTS.search(text) and not text.isupper()
            and not NAME_ONLY.match(text) and not MARKER.search(text)):
        return True, " ".join(_expand_addrs(text))
    return False, ""


def section_house(text):
    """(house name, address) a numbered section heading declares.

    The address is only set when the heading gives one -- "1 Tejekovi v
    Sovičah 6 (danes Soviče 12)" is about a house in a different settlement
    from the chapter it sits in.
    """
    if VULGO.search(text):
        return "", ""                 # an explicit "po domače" is read elsewhere
    m = SECTION_HOUSE.match(text)
    if m:
        name = " / ".join(x.strip() for x in re.split(r"[-–—]", m.group(1)))
        return name, " ".join(_expand_addrs(text[m.end():]))
    m = PAREN_HOUSE.search(text)
    if m and re.match(r"^\d+\s", text):
        return m.group(1), " ".join(_expand_addrs(text[:m.start()]))
    return "", ""


def place_key(place):
    """(settlement stem, house number) for an address, or None.

    The book declines the settlement -- Dravci / Dravce / Dravcih / Dravcah --
    so only the stem is comparable.
    """
    if not place:
        return None
    m = re.match(r"^([A-Za-zŠČŽĐĆšččžćđ]+(?:\s+[A-Za-zŠČŽĐĆšččžćđ]+)?)\s+(\d+[a-z]?)\b",
                 place.strip())
    if not m:
        return None
    stem = norm(m.group(1))[:5]
    return stem, m.group(2).lower()


def _expand_addrs(text):
    """'Dravci 1, Dravci 2a in 2b' -> ['Dravci 1', 'Dravci 2a', 'Dravci 2b'].

    A dash is read as a separator, not a range: the book writes "Soviče 8 - 10"
    for a chapter covering two houses whose neighbour has a chapter of its own.
    """
    out, last_town = [], ""
    for town, num in ADDR.findall(text or ""):
        town = (town or "").strip() or last_town
        if not town:
            continue
        last_town = town
        out.append(f"{town} {num}")
    return out


def _clean_vulgo(name):
    parts = [n.strip() for n in re.split(r"\s+ali\s+", name or "") if n.strip()]
    return " / ".join(parts)


def _house_names(vulgo, addr_text):
    """[(address, vulgo)] for one declaration."""
    vulgo = _clean_vulgo(vulgo)
    if not vulgo:
        return []
    return [(a, vulgo) for a in _expand_addrs(addr_text)]


def collect_houses(recs):
    """Map every house the book nicknames to that nickname.

    A declaration naming one house beats one that names several, so a
    sub-chapter ("Soviče 9: Cafuta (po domače Pozničar)") overrides the
    grouping it sits inside ("Soviče 8 - 10: Gabrovec (po domače Kukmačevi)").
    """
    houses, breadth = {}, {}
    def offer(pairs):
        for addr, vulgo in pairs:
            key = place_key(addr)
            if not key:
                continue
            if key not in houses or len(pairs) < breadth.get(key, 99):
                houses[key], breadth[key] = vulgo, len(pairs)

    for r in recs:
        if r.vulgo and r.chapter:                    # chapter title / running head
            offer(_house_names(r.vulgo, r.chapter))
        m = HOUSE_DECL.match(r.text)
        if m:                                        # section heading in the text
            offer(_house_names(m.group(1), m.group(2) or r.chapter))
    return houses


# Slovenian inflects the settlement as readily as the name: a birth is "Dravci
# 17" but a death is "v Dravcih 10".  Reduce a name to a stem so the forms can
# be recognised as one place, then write them all the way the book writes the
# dictionary form.
_ENDING = re.compile(r"(ih|ah|em|u|i|e|a|o)$")
# Only the -ec / -ek schwa is elided when the word inflects: Leskovec ->
# Leskovcu, Bukovec -> Bukovci.  Anything wider eats real stems (Pobrežje).
_SCHWA = re.compile(r"e(?=[ck]$)")


def town_stem(word):
    """'Dravcih' / 'Dravce' -> 'dravc'; 'Leskovcu' / 'Leskovec' -> 'leskovc'."""
    w = norm(word).strip(".,;:()")
    if len(w) <= 3:
        return w
    stripped = _ENDING.sub("", w)
    if len(stripped) >= 3:
        w = stripped
    # a schwa that only appears in the nominative: Leskovec / Leskovcu
    return _SCHWA.sub("", w) if len(w) > 4 else w


def town_key(place):
    """Stem every word of the settlement, so 'Spodnja Hajdina' and 'Spodnji
    Hajdini' agree while 'Spodnja Pristava' stays apart."""
    words = _settlement(place).split()
    return tuple(town_stem(x) for x in words if x) or None


def _settlement(place):
    """The place without its house number."""
    return re.sub(r"\s*\d+[a-z]?\s*$", "", (place or "").strip()).strip()


def _iter_places(tree):
    for p in tree.indi:
        for ev in (p.birth, p.death, p.bapt):
            if ev.get("place"):
                yield ev, "place"
        for i, (pl, decl) in enumerate(p.resi):
            yield {"place": pl, "declined": decl, "_resi": (p, i)}, "place"
    for f in tree.fam:
        if f.get("place"):
            yield f, "place"


# -u, -ih, -ah, -em on a place name are locative: "v Mariboru", "v Dravcih",
# "v Cirkulanah".  A nominative plural in -i or -e is not.
LOCATIVE_END = re.compile(r"(u|ih|ah|em)$", re.I)


# A misprint should not become a settlement of its own.  Folding one in is
# only safe when the two agree from the start -- an edit-distance test alone
# would swallow "Borovci" into "Bukovci" and "Švici" (Switzerland) into
# "Soviče", which are different places, not typos.
TYPO_MAX_USES = 2        # a real place gets mentioned more than this
TYPO_DOMINANCE = 20      # ... and the form it is folded into, far more often
TYPO_EDITS = 2
TYPO_SHARED_PREFIX = 4


def _edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_typo_of(rare, common):
    if len(rare) != len(common):
        return False
    if not all(a[:TYPO_SHARED_PREFIX] == b[:TYPO_SHARED_PREFIX]
               for a, b in zip(rare, common)):
        return False
    return sum(_edit_distance(a, b) for a, b in zip(rare, common)) <= TYPO_EDITS


def fold_typos(counts):
    """{misprinted key: the key it belongs to} -- 'Dravcen 11' is Dravci 11."""
    folds = {}
    for key, n in counts.items():
        if n > TYPO_MAX_USES:
            continue
        targets = [k2 for k2, n2 in counts.items()
                   if k2 != key and n2 >= TYPO_DOMINANCE * n and _is_typo_of(key, k2)]
        if len(targets) == 1:
            folds[key] = targets[0]
    return folds


def canonical_towns(tree):
    """Pick one spelling per settlement.

    Preferred, in order: a form that does not look locative, then one the book
    writes without a preposition, then the commonest.  The book often has no
    dictionary form at all for a place it only ever mentions as somewhere
    something happened, so all three tests can be needed.
    """
    plain, seen = defaultdict(Counter), defaultdict(Counter)
    for ev, _k in _iter_places(tree):
        town = _settlement(ev["place"])
        key = town_key(ev["place"])
        if not key or not town:
            continue
        seen[key][town] += 1
        if not ev.get("declined"):
            plain[key][town] += 1

    def looks_locative(town, counts):
        last = town.split()[-1]
        if LOCATIVE_END.search(last):
            return True
        # -i is also the locative of a feminine -a name (Ljubljana ->
        # Ljubljani), which no ending can tell from a nominative plural like
        # Dravci -- but if the -a form is attested too, this one is inflected.
        return last.endswith("i") and any(
            c.split()[-1] == last[:-1] + "a" for c in counts)

    totals = Counter({key: sum(c.values()) for key, c in seen.items()})
    for typo, target in fold_typos(totals).items():
        seen[target].update(seen.pop(typo))
        plain[target].update(plain.pop(typo, Counter()))

    return {key: max(counts, key=lambda t: (not looks_locative(t, counts),
                                            plain[key][t], counts[t]))
            for key, counts in seen.items()}


def canonicalise_places(tree):
    """Rewrite every settlement to its chosen form, keeping the house number."""
    towns = canonical_towns(tree)
    for typo, target in fold_typos(_town_totals(tree)).items():
        if target in towns:
            towns[typo] = towns[target]
    n = 0
    for p in tree.indi:
        for ev in (p.birth, p.death, p.bapt):
            if ev.get("place"):
                new = _rewrite_town(ev["place"], towns)
                n += new != ev["place"]
                ev["place"] = new
        fixed = []
        for pl, decl in p.resi:
            new = _rewrite_town(pl, towns)
            n += new != pl
            fixed.append((new, decl))
        p.resi = fixed
    for f in tree.fam:
        if f.get("place"):
            new = _rewrite_town(f["place"], towns)
            n += new != f["place"]
            f["place"] = new
    return n


def _town_totals(tree):
    totals = Counter()
    for ev, _k in _iter_places(tree):
        key = town_key(ev["place"])
        if key:
            totals[key] += 1
    return totals


def _rewrite_town(place, towns):
    key = town_key(place)
    canon = towns.get(key) if key else None
    if not canon:
        return place
    number = place[len(_settlement(place)):].strip()
    return f"{canon} {number}".strip() if number else canon


def annotate_places(tree, houses):
    """Write the house name next to the address, as the family would say it.

    A declaration wins where there is one; otherwise the section a person was
    written under can still name their household, so this runs even when the
    book declares no houses at all.
    """
    n = 0
    def tag(place, person=None):
        nonlocal n
        key = place_key(place)
        if not key or "(pd " in place:
            return place
        vulgo = houses.get(key)
        if not vulgo and person is not None and person.house:
            # no declaration for this address, but the section this person was
            # written under names the household -- and this is that address
            if key in {place_key(a) for a in _expand_addrs(person.house_addr)}:
                vulgo = person.house
        if not vulgo:
            return place
        n += 1
        return f"{place} (pd {vulgo})"

    for p in tree.indi:
        for ev in (p.birth, p.death, p.bapt):
            if ev.get("place"):
                ev["place"] = tag(ev["place"], p)
        p.resi = [(tag(x, p), d) for x, d in p.resi]
    for f in tree.fam:
        if f["place"]:
            f["place"] = tag(f["place"])
    return n


def dedupe_residences(tree):
    """One row per address.

    Normalising the spellings makes entries that read differently in the book
    -- "Dravci 9", "Dravcih 9", "Dravce 9" -- into the same address, and
    merging two records can bring the same one along twice.
    """
    n = 0
    for p in tree.indi:
        seen, uniq = set(), []
        for place, decl in p.resi:
            if place in seen:
                n += 1
                continue
            seen.add(place)
            uniq.append((place, decl))
        p.resi = uniq
    return n


def _lines(page):
    out = []
    for el in page:
        if not isinstance(el, LTTextContainer):
            continue
        for ln in el:
            if not isinstance(ln, LTTextLine):
                continue
            txt = re.sub(r"\s+", " ", ln.get_text()).strip()
            if not txt:
                continue
            sizes = [round(c.size, 1) for c in ln if isinstance(c, LTChar)]
            # The commonest glyph size, not the largest or the smallest.  A
            # body line can carry a tiny superscript footnote marker
            # ("...Sv. Barbara;1947 drugič poročen..."), and a footnote's
            # second line can carry one full-size glyph -- either extreme
            # misreads the line it came from.
            out.append({"x": round(ln.x0, 1), "y": round(ln.y0, 1),
                        "size": Counter(sizes).most_common(1)[0][0] if sizes else 0.0,
                        "text": txt})
    return out


def _attach_bullets(lines):
    """pdfminer emits the bullet glyph as its own line, sometimes out of order.
    Re-attach each lone bullet to the body line on its baseline."""
    bullets = [l for l in lines if len(l["text"]) == 1 and l["text"] in BULLET_CHARS
               and _is_bullet_x(l["x"])]
    bodies = [l for l in lines if l not in bullets]
    for b in bullets:
        cand = [l for l in bodies
                if abs(l["y"] - b["y"]) <= Y_TOL and b["x"] < l["x"] <= b["x"] + CONT + 4]
        if cand:
            t = min(cand, key=lambda l: l["x"])
            t["x"], t["text"] = b["x"], "− " + t["text"]
    for l in bodies:
        if l["text"][0] in BULLET_CHARS and _is_bullet_x(l["x"]):
            l["bullet"] = True
            l["text"] = l["text"].lstrip("".join(BULLET_CHARS)).strip()
        else:
            l["bullet"] = False
    bodies.sort(key=lambda l: (-l["y"], l["x"]))
    return [l for l in bodies if l["text"]]


def _is_bullet_x(x):
    """Bullets sit on the 36pt generation grid; a dash anywhere else is
    ordinary punctuation ("... 17.4.1928 (peritonitis - vnetje potrebušnice);")."""
    return abs((x - BULLET_X0) / STEP - round((x - BULLET_X0) / STEP)) < 0.15


def _level(x, bullet):
    if bullet:
        return max(1, int(round((x - BULLET_X0) / STEP)) + 1)
    if abs(x - LEFT_MARGIN) < 4:
        return 0
    return max(1, int(round((x - CONT - BULLET_X0) / STEP)) + 1)


SKIP = re.compile(r"^(\d{3,4}\.?$|Slika\s+\d+|(I{1,3}|IV|V)\.\s+del:)")


def extract(pdf, first, last, page_offset=14):
    idx = [p + page_offset - 1 for p in range(first, last + 1)]
    la = LAParams(char_margin=2.0, line_margin=0.35, word_margin=0.1,
                  detect_vertical=False)
    recs, prev_y, prev_page = [], None, None
    chapter, vulgo, vulgo_page = "", "", None
    for n, page in zip(range(first, last + 1),
                       extract_pages(pdf, page_numbers=idx, laparams=la)):
        raw = _lines(page)
        if not raw:
            continue
        # Everything that is not set at the page's body size is furniture:
        # footnotes (5-8pt), running heads (12pt), chapter titles (28pt).
        body = Counter(l["size"] for l in raw).most_common(1)[0][0]
        # The furniture we drop still carries the chapter's house and its
        # "po domače" name, so read those off before discarding it.
        for l in raw:
            if abs(l["size"] - body) <= SIZE_TOL:
                continue
            head = CHAPTER_HEAD.match(l["text"])
            if head:
                chapter = head.group(1)
            if VULGO.search(l["text"]):
                vulgo = VULGO.search(l["text"]).group(1)
                vulgo_page = n
            elif vulgo_page != n:
                vulgo = ""
        lines = [l for l in raw if abs(l["size"] - body) <= SIZE_TOL]
        for l in _attach_bullets(lines):
            t = l["text"]
            if SKIP.match(t) or "ZGODOVINA DRAVCEV" in t.upper() or t.isupper():
                continue
            lvl = _level(l["x"], l["bullet"])
            gap = 999.0 if prev_page != n or prev_y is None else prev_y - l["y"]
            prev_y, prev_page = l["y"], n
            # Inside the register every new entry carries a bullet, so an
            # indented line without one continues the entry before it -- even
            # across a page break, where the entry is simply cut in half.
            if l["bullet"] or not recs or t.isupper():
                # a shouted line is a title, never the rest of someone's entry
                cont = False
            elif l["x"] > LEFT_MARGIN + 4:
                cont = True
            else:
                cont = recs[-1].level == 0 and gap < LINE_PITCH
            if cont:
                a, b = recs[-1].text, t
                recs[-1].text = a[:-1] + b if a.endswith("-") and b[:1].islower() else a + " " + b
            else:
                recs.append(Rec(n, lvl, t, l["bullet"], l["y"], chapter, vulgo))
    return recs


# --------------------------------------------------------------------------
# Stage 2 -- one record -> a structured Person
# --------------------------------------------------------------------------

MARKER = re.compile(
    r"\b("
    r"rojen[ai]?|umrl[ai]?|krščen[ai]?|star[ai]?|"
    r"(?:prvič|drugič|tretjič|četrtič)?\s*poroč(?:en[ai]?|il[ai]?)|"
    r"starša|oče|mama|mati|botr[ai]|boter|babica pri porodu|"
    r"stanujoč[ai]?|stanoval[ai]?|bival[ai]?|vpisan[ai]?|dvojč[ie][cč]?[ai]?"
    r")\b", re.I)

# Occupations worth an OCCU tag.  Deliberately case-sensitive: Slovenian
# writes a trade in lower case and a surname in upper, and the two overlap
# almost completely -- kovač / Kovač, mlinar / Mlinar, krčmar / Krčmar.
OCCU = re.compile(r"\b("
    r"viničar(?:ka|ji)?|kmet(?:ica|je)?|želar(?:ka)?|kočar(?:ka)?|hlapec|dekla|"
    r"dninar(?:ka)?|gostilničar(?:ka)?|krčmar(?:ica)?|natakar(?:ica)?|"
    r"posestni(?:k|ca)|najemni(?:k|ca)|prevzemni(?:k|ca)|skrbni(?:k|ca)|"
    r"preužitkar(?:ica)?|užitkar(?:ica)?|služkinja|sluga|šolski sluga|"
    r"delav(?:ec|ka)|zdravni(?:k|ca)|učitelj(?:ica)?|profesor(?:ica)?|"
    r"ravnatelj(?:ica)?|uradni(?:k|ca)|pisar|notar|odvetni(?:k|ca)|sodni(?:k|ca)|"
    r"duhovni(?:k|ca)|župni(?:k|ca)|kaplan|mašni(?:k|ca)|redovnica|"
    r"železničar(?:ka)?|sprevodni(?:k|ca)|cestar|voznik|poštar(?:ica)?|"
    r"krojač(?:ica)?|čevljar|mlinar|kovač|sodar|kolar|mizar|tesar|zidar|klepar|"
    r"pek|mesar|trgovec|trgovka|brivec|frizer(?:ka)?|kuhar(?:ica)?|šivilja|"
    r"gospodinja|rudar|gozdar|vojak|častnik|orožni(?:k|ca)|policist|gasilec|"
    r"strojni(?:k|ca)|tehni(?:k|ca)|inženir(?:ka)?"
    # not "babica": in this book it is always the midwife attending someone
    # else's birth ("babica pri porodu Elizabeta Weisbacher"), read separately
    r")\b")

PLACE_STOP = re.compile(r"^(in|z|s|iz|v|na|pri|kot|ki|je|so|ob|od|do|kasneje|potem|nato)$", re.I)


@dataclass(eq=False)     # identity, not field equality: two brothers born the
class Person:            # same day with the same name are still two people
    given: str = ""
    surname: str = ""
    alt_surname: str = ""
    sex: str = ""
    birth: dict = field(default_factory=dict)
    death: dict = field(default_factory=dict)
    bapt: dict = field(default_factory=dict)
    occu: list = field(default_factory=list)
    resi: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    father: str = ""
    mother: str = ""
    mother_surname: str = ""
    marriages: list = field(default_factory=list)   # [{date,place,spouse:Person,order}]
    prose: bool = False      # head is a sentence, not a name
    stub: bool = False       # known only from someone else's marriage clause
    married_surname: str = ""   # "Blanka Kerec, poročena Slana"
    house: str = ""          # "po domače" name of the section's household
    house_addr: str = ""     # the chapter address that name belongs to
    pedi: str = ""           # adopted / foster, when the book says so
    page: int = 0
    nick: str = ""           # "po domače" house nickname, "Kolarjev Pepek"
    aka_given: list = field(default_factory=list)   # "Janez (Ivan)" -> [Ivan]
    age_death: int = 0       # "star 62 let" after an umrl clause
    age_marr: int = 0        # "poročen ... star 47 let"
    source: dict = field(default_factory=dict)      # page, raw text
    level: int = 0

    def label(self):
        return f"{self.given} {self.surname}".strip()


def _split_origin(phrase):
    """'Jožefom Bedračem iz Dravc 5' -> ('Jožefom Bedračem', 'Dravc 5')."""
    m = PLACE_TAIL.search(phrase)
    return (phrase[:m.start()].strip(), m.group(2).strip()) if m else (phrase, "")


def _take_place(rest):
    """Trailing place phrase after a date: 'Dravci 17', 'Spodnja Hajdina 82'.

    Returns (place, declined).  A preposition puts the name in the locative --
    "umrl v Dravcih" -- which is how we later tell the dictionary form from an
    inflected one.
    """
    rest = rest.lstrip(" ,")
    declined = False
    m = re.match(r"^(?:v|na|iz|pri)\s+", rest, re.I)
    if m:
        rest = rest[m.end():]
        declined = True
    toks, out = rest.split(), []
    for t in toks:
        core = t.strip(".,;")
        if not core:
            break
        if out and re.fullmatch(r"\d{1,3}", core):        # house number
            out.append(core)
            break
        if core[0].isupper() and not PLACE_STOP.match(core):
            out.append(core)
            if t.endswith((",", ";", ".")):
                break
        elif out and core.lower() in ("vas", "vrh", "breg", "gora", "polje"):
            out.append(core)
        else:
            break
    return " ".join(out), declined


# "Posvojenka: Ana Vidovič", "Pastorek: Franc Krajnc" -- a role, then the name
ROLE_LABEL = re.compile(r"^(posvojen(?:ec|ka)|pastor(?:ek|ka)|rejen(?:ec|ka)|"
                        r"sin|hči|hčerka|vnuk(?:inja)?)\s*[:\-–]\s*", re.I)

# "Prva poroka Terezije Furman v Majšperku, mož Janez Vindiš" -- a marriage
# heading rather than a person; the entries under it are that couple's children.
MARRIAGE_HEAD = re.compile(
    r"^(?:prva|druga|tretja|četrta)\s+poroka\b.*?\b(?:mož|soprog)\s+"
    r"([A-ZŠČŽĐĆÖÜ][\wčšžćđ.-]*(?:\s+[A-ZŠČŽĐĆÖÜ][\wčšžćđ.-]*)?)", re.I)

# A head that opens with any of these is running prose, not a person entry.
PROSE_OPENER = re.compile(
    r"^(v|na|pri|iz|od|do|po|za|nad|pod|med|ob|k|h|s|z|in|ter|tudi|še|nato|"
    r"tukaj|tam|sem|tega|temu|njegov\w*|njen\w*|njun\w*|oba|obe|sin|hčerka|"
    r"potem|kasneje|leta|hišo|hiša|dve|tri|štiri|dvojčič\w*|izročilna|"
    r"prva|druga|tretja|četrta|kupita|kupi|umrl\w*|rojen\w*)$", re.I)


# Prose that reports one event about one person, with the verb in front of the
# name: "V Dravcih 2 je umrl Jurij Zrnec (Sernez), 16.10.1782", "14.7.1892
# umrla Marija Mihelač, Dravci 18", "pri Horvatovih je kot dekla živela Marija
# (Micika) Topolovec, rojena 15.9.1907".
# a name may carry a parenthetical on either half: "Marija (Micika) Topolovec",
# "Jurij Zrnec (Sernez)" -- both belong to the name, not to what follows it
PROSE_NAME = (r"([A-ZŠČŽĐĆÖÜ][\wčšžćđ-]{2,}(?:\s*\([^)]*\))?"
              r"\s+[A-ZŠČŽĐĆÖÜ][\wčšžćđ-]{2,}(?:\s*\([^)]*\))?)")
LEAD_EVENT = re.compile(
    r"\b(umrl[ai]?|umre|rojen[ai]?|krščen[ai]?|živel[ai]|bil[ai]?|prišla?|"
    r"pride|stanoval[ai]?)\s+(?:\w+\s+){0,2}?" + PROSE_NAME)
# "Na Dravci 2 se 25.1.1796 poročita 54-letni Matevž Bezjak in 40-letna Marija Zavec."
PROSE_MARRIAGE = re.compile(
    r"\bporo[čc]it[ai]\b.*?" + PROSE_NAME + r".*?\bin\b.*?" + PROSE_NAME, re.S)
# "mrtvorojena deklica Habjanič, rojena 22.12.1915"
STILLBORN = re.compile(
    r"^mrtvorojen[ai]\s+(deklic\w+|de[čc]k\w+)\s+([A-ZŠČŽĐĆ][\wčšžćđ-]+)", re.I)


def _undative(name):
    """'Jožefu Miložiču' -> 'Jožef Miložič'.

    A name reached through prose can be in whatever case the sentence needs:
    "botra Jožefu Miložiču" is dative.  No Slovenian name in this book ends in
    -u in the nominative, so every part ending that way is inflected.
    """
    parts = name.split()
    if len(parts) < 2 or not all(len(x) > 3 and x.endswith("u") for x in parts):
        return name
    return " ".join(x[:-1] for x in parts)


def normalise_prose(text):
    """Rewrite a leading-verb sentence so the facts follow the name.

    The parser reads left to right from the subject, so "14.7.1892 umrla
    Marija Mihelač, Dravci 18" has to become "Marija Mihelač, umrla 14.7.1892,
    Dravci 18" before its date can be found.
    """
    m = LEAD_EVENT.search(text)
    if not m:
        return text
    marker, name = m.group(1), _undative(m.group(2))
    prefix, tail = text[:m.start()], text[m.end():]
    if not MARKER.fullmatch(marker):
        # "je bil", "je živela", "pride" only point at the subject; the facts
        # are already behind the name, and re-inserting the verb would leave
        # it stuck to the surname
        return f"{name}{tail}"
    date = RE_DMY.search(prefix) or RE_Y.search(prefix)
    return f"{name}, {marker} {date.group(0) if date else ''}{tail}"


def stillborn_child(text):
    """(surname, sex) for 'mrtvorojena deklica Habjanič, rojena ...'."""
    m = STILLBORN.match(text)
    if not m:
        return None, ""
    return m.group(2), ("f" if m.group(1).lower().startswith("deklic") else "m")


# The person a prose sentence is really about: the name its facts hang off.
INNER_NAME = re.compile(
    r"\b([A-ZŠČŽĐĆÖÜ][\wčšžćđ-]+(?:\s+[A-ZŠČŽĐĆÖÜ][\wčšžćđ-]+)?)"
    r"\s*(?:\([^)]*\))?\s*,\s*(?=rojen|umrl|krščen|poročen)")


# "Amalija Finžger iz Lovrenca na Dravskem polju", "Ana Smodiš v Ljubljani" --
# the book identifies a person by where they came from, and that is a place,
# not part of their name.
PLACE_TAIL = re.compile(r"\s+(iz|v|na|pri|od)\s+([A-ZŠČŽĐĆÖÜ][\wčšžćđ]*"
                        r"(?:\s+[\wčšžćđ]+){0,3})\s*$")


@dataclass
class Head:
    given: str
    surname: str
    alt: str
    nick: str
    aka: list
    place: str
    married: str
    remark: str
    offset: int
    prose: bool


def _split_head(text):
    """Leading 'Ime Priimek (Alt)' before the first marker.

    Returns (given, surname, alt_surname, offset, prose) where `prose` marks an
    entry whose head is a sentence rather than a name -- we can still read the
    facts out of it, but it must not be treated as a named parent.
    """
    mh = MARRIAGE_HEAD.match(text)
    if mh:
        toks = mh.group(1).split()
        return Head(given=toks[0], surname=" ".join(toks[1:]), alt="", nick="",
                    aka=[], place="", married="", remark="", offset=mh.end(), prose=False)

    text = ROLE_LABEL.sub("", text, count=1)
    m = MARKER.search(text)
    if m:
        head = text[:m.start()]
    else:
        # nothing dated follows, so the name ends at the first comma or dash
        cut = re.search(r"[,;:–—]|\s-\s", text)
        head = text[:cut.start()] if cut else text
    head = head.strip().rstrip(",;").strip()
    # A name never runs past a comma, so whatever follows one belongs to the
    # facts -- "Jurij Weisbacher, neznani starši iz Dravc, je bil 27.1.1789
    # poročen ..." has no marker until "poročen", and without this the whole
    # clause would become the surname.  Commas inside a parenthetical stay.
    depth, cut = 0, None
    for i, ch in enumerate(head):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch in ",;" and depth == 0:
            cut = i
            break
    if cut is not None:
        head = head[:cut].strip()

    # "Janez (Ivan) Fajfar", "Janez (Johan, Ivan) Letič" -- other forms of the
    # same given name
    aka = []
    inner = re.match(r"^(\S+)\s*\(([^)]+)\)\s+(?=\S)", head)
    if inner:
        forms = [x.strip() for x in re.split(r"[,/]", inner.group(2))]
        if forms and all(re.fullmatch(r"[A-ZŠČŽĐĆÖÜ][\wčšžćđ-]+", f) for f in forms):
            aka = forms
            head = inner.group(1) + " " + head[inner.end():]

    # A trailing parenthetical is one of three things, by length:
    #   (Weispoher)                     a spelling of the surname
    #   (Kolarjev Pepek)                the "po domače" house nickname
    #   (sprva v matični knjigi ...)    an editorial remark
    alt = nick = remark = ""
    p = re.search(r"\(([^)]+)\)\s*$", head)
    if p:
        body = p.group(1).strip()
        words = body.split()
        if len(words) == 1:
            alt = body
        elif len(words) <= 3 and all(w[:1].isupper() for w in words):
            nick = body
        else:
            remark = body
        head = head[:p.start()].strip()

    # an unbalanced "(" is a parenthetical the marker scan cut in half
    head = re.sub(r"\s*\($", "", head).rstrip(",").strip()
    # "Marija Kranjc pozakonjena Korpič" -- the name she was legitimated or
    # married into is not part of the surname she was born with
    married = ""
    mt = re.search(r"\s+(?:poročena|pozakonjena|por\.)\s+"
                   r"([A-ZŠČŽĐĆÖÜ][\wčšžćđ-]+)\s*$", head)
    if mt:
        married, head = mt.group(1), head[:mt.start()].strip()

    place = ""
    pt = PLACE_TAIL.search(head)
    if pt:
        place, head = pt.group(2).strip(), head[:pt.start()].strip()
    toks = head.split()
    given = toks[0] if toks else ""
    surname = " ".join(toks[1:]) if len(toks) > 1 else ""
    prose = bool(given) and bool(PROSE_OPENER.match(given))

    if prose:
        # "Pri njih je živel Jurij Hlupič, rojen 1873" -- running prose that
        # still introduces a person.  Re-anchor to the name the facts hang off.
        nm = INNER_NAME.search(text)
        if nm:
            toks = nm.group(1).split()
            return Head(given=toks[0], surname=" ".join(toks[1:]), alt="",
                        nick="", aka=[], place="", married="", remark="",
                        offset=nm.end(), prose=False)
    return Head(given=given, surname=surname, alt=alt, nick=nick, aka=aka,
                place=place, married=married, remark=remark,
                offset=(m.start() if m else len(text)), prose=prose)


def renominalise(given, names):
    """'Rozo' -> 'Roza' when that is how the book otherwise writes the name.

    A name reached through a sentence carries whatever case the sentence
    needed.  Only the corpus can say which form is the dictionary one, so a
    change is made solely when it lands on a name the book actually uses.
    """
    if not given or names is None:
        return given
    first = given.split()[0]
    if names.given[norm(first)]:
        return given
    for cand in (first[:-1] + "a", first[:-1], first[:-2]):
        if len(cand) > 2 and names.given[norm(cand)]:
            return given.replace(first, cand, 1)
    return given


def _event_owner(kw, subject, person, recent):
    """Whose event a participle describes.

    "poročena s sosedom Jožefom Belšakom, rojen 19.9.1843, umrla 27.10.1886"
    -- the feminine "umrla" hands the clause back to the woman the entry is
    about, not to the husband just named.
    """
    want = "f" if re.search(r"(?:en|l)a$", kw) else "m" if re.search(r"(?:en|l)$", kw) else ""
    if not want or not subject.sex or subject.sex == want:
        return subject
    for cand in (person, recent):
        if cand is not None and cand.sex == want:
            return cand
    return subject


def parse_record(text, page=0, level=0, lexicon=None, full_text=None, names=None):
    """Read one entry.  `full_text` is the sentence as the book wrote it, when
    `text` has been rewritten from prose -- trades and the source note come
    from the original."""
    original = full_text or text
    p = Person(source={"page": page, "text": original}, level=level)
    h = _split_head(text)
    if names is not None and names.swapped(h.given, h.surname):
        h.given, h.surname = h.surname, h.given      # "Cafuta Sonja"
    h.given = renominalise(h.given, names)
    p.given, p.surname, p.alt_surname = h.given, h.surname, h.alt
    p.nick, p.aka_given, p.prose, pos = h.nick, h.aka, h.prose, h.offset
    if h.remark:
        p.notes.append(h.remark)
    if h.place:
        p.resi.append((h.place, True))
    if h.married:
        p.married_surname = h.married
    # gender comes from the FIRST agreeing participle only -- later clauses
    # describe the spouse and would otherwise flip it
    first = MARKER.search(text, pos)
    p.sex = sex_of(text[first.start():first.end()]) if first else sex_of(text[:120])

    subject = p          # clauses bind here until a spouse is introduced
    nee_target = None    # who a following bare 'rojena <Priimek>' belongs to
    last_event = None    # which event a following "star N let" measures
    recent = None        # the last spouse named, across semicolons
    last_end = pos

    for m in MARKER.finditer(text, pos):
        kw = m.group(1).lower().strip()
        rest = text[m.end():]
        # a semicolon ends the spouse's sub-clause and returns to the subject
        if ";" in text[last_end:m.start()]:
            subject, nee_target = p, None
        last_end = m.end()

        if "poroč" in kw:
            last_event = "marr"
            order = {"prvič": 1, "drugič": 2, "tretjič": 3,
                     "četrtič": 4}.get(kw.split()[0], 0)
            date, n = parse_date(rest.lstrip(" ,"))
            rest2 = rest.lstrip(" ,")[n:]
            place = ""
            pm = re.match(r"\s*(?:v|na|pri)\s+([A-ZŠČŽĐĆ][\w.-]*(?:\s+[A-ZŠČŽĐĆ]\w+)?)", rest2)
            if pm:
                place = pm.group(1)
                rest2 = rest2[pm.end():]
            # the spouse's name can sit behind a few modifiers, including a
            # parenthetical: "poročen s 23 let mlajšo Marijo Vogrinec",
            # "drugič poročen z (16 let mlajšo) Jero Širovnik"
            sm = re.search(r"\b[zs]\s+"
                           r"(?:(?:\([^)]*\)|[a-zčšž0-9][\wčšžćđ.]*)\s+){0,4}"
                           # stop at a dash: "poročena z Maksom Vaupotičem −
                           # Nataša Vaupotič, rojena ..." is two entries whose
                           # bullet ended up mid-line
                           r"([A-ZŠČŽĐĆÖÜ][^,;(−–—]*)", rest2[:160])
            # The participle says who is marrying: "poročen" a man, "poročena"
            # a woman.  In "Janko Vaupotič ... poročen z Leopoldino ..., vnovič
            # poročena 3.12.1960 z Ljubomirjem Lukićem" the second wedding is
            # hers, not his, and its groom is therefore a man.
            wed_sex = "f" if re.search(r"(?:en|il)[ai]$", kw) else "m"
            spouse_sex = "m" if wed_sex == "f" else "f"
            spouse = None
            if sm:
                phrase, origin = _split_origin(sm.group(1).strip())
                g, sn = denominalize(phrase, spouse_sex, lexicon)
                if not sn and names is not None and names.is_surname(g):
                    g, sn = "", g        # "poročena z Adamičem" names no first name
                g = renominalise(g, names)
                spouse = Person(given=g, surname=sn, sex=spouse_sex, stub=True,
                                source={"page": page, "text": text})
                if origin:
                    spouse.resi.append((origin, True))
            if spouse is None:
                # "Blanka Kerec, poročena Slana" -- no husband named, only the
                # name she took.  An instrumental ending means the book simply
                # dropped the "z" and this is the husband after all.
                bare = re.match(r"\s*,?\s*([A-ZŠČŽĐĆÖÜ][\wčšžćđ-]{2,})(?=[,;.)]|\s|$)",
                                rest)
                if bare and not bare.group(1).lower().endswith(("om", "em")):
                    subject.married_surname = (subject.married_surname
                                               or bare.group(1))

            # Whoever is marrying owns the marriage.  A semicolon ends a
            # spouse's sub-clause, but "... s Terezijo Šmigoc, ...; drugič
            # poročena 12.1.1919 s Francem Cafuto" is still her remarriage --
            # so when the participle disagrees with the entry's subject, fall
            # back to the last spouse of the right sex.
            owner = p
            if p.sex and p.sex != wed_sex:
                if subject is not p and subject is not None and subject.sex == wed_sex:
                    owner = subject
                elif recent is not None and recent.sex == wed_sex:
                    owner = recent
                else:
                    # The participle names a sex nobody in scope has, so this
                    # wedding is somebody else's -- a descendant mentioned in
                    # passing.  Recording it against the entry's subject would
                    # marry them to their own sex.
                    owner = None
            if owner is not None:
                owner.marriages.append({"date": date or "", "place": place,
                                        "order": order, "spouse": spouse})
            subject = spouse if spouse else Person(sex=spouse_sex)
            if spouse is not None:
                recent = spouse
            nee_target = None
            continue

        if kw.startswith("rojen"):
            last_event = "birth"
            head = rest.lstrip(" ,")
            if parse_date(head)[0]:
                subject = _event_owner(kw, subject, p, recent)
            # 'rojena Vogrinec' = maiden surname of the woman just named
            if re.match(r"^[A-ZŠČŽĐĆÖÜ]\w+", head) and not parse_date(head)[0]:
                sur = re.match(r"^([A-ZŠČŽĐĆÖÜ][\wčćžšđ-]*)", head).group(1)
                if nee_target == "mother":
                    subject.mother_surname = sur
                elif not subject.surname:
                    subject.surname = sur
                else:
                    subject.alt_surname = subject.alt_surname or sur
                nee_target = None
                continue
            date, n = parse_date(head)
            if date and not subject.birth.get("date"):
                pl, decl = _take_place(head[n:])
                subject.birth = {"date": date, "place": pl, "declined": decl}
            continue

        if kw.startswith("umrl"):
            last_event = "death"
            subject = _event_owner(kw, subject, p, recent)
            head = rest.lstrip(" ,")
            date, n = parse_date(head)
            place, decl = _take_place(head[n:] if date else head)
            if not subject.death.get("date"):
                # "umrla v otroštvu" states the death without dating it
                subject.death = {"date": date or "", "place": place,
                                 "declined": decl, "known": True}
            continue

        if kw.startswith("krščen"):
            head = rest.lstrip(" ,")
            date, n = parse_date(head)
            pl, decl = _take_place(head[n:])
            subject.bapt = {"date": date or "", "place": pl, "declined": decl}
            continue

        if kw.startswith("star") and kw != "starša":
            am = re.match(r"\s*[„“\"]?\s*(okrog\s+)?(\d{1,3})\s*(let|leta|leti)", rest)
            if am:
                label = {"death": "Starost ob smrti", "marr": "Starost ob poroki"}
                subject.notes.append(
                    f"{label.get(last_event, 'Starost')}: {am.group(2)} let")
                if last_event == "death" and not subject.age_death:
                    subject.age_death = int(am.group(2))
                elif last_event == "marr" and not subject.age_marr:
                    subject.age_marr = int(am.group(2))
            continue

        if kw == "starša":
            sm = re.match(r"\s*([A-ZŠČŽĐĆ][\w.-]*(?:\s+[A-ZŠČŽĐĆ][\w.-]*)?)\s+in\s+"
                          r"([A-ZŠČŽĐĆ][\w.-]*)", rest)
            if sm and not (subject.father or subject.mother):
                subject.father, subject.mother = sm.group(1), sm.group(2)
                nee_target = "mother"
            continue

        if kw in ("oče",):
            fm = re.match(r"\s*(NN|[A-ZŠČŽĐĆ][\w.-]*(?:\s+[A-ZŠČŽĐĆ][\w.-]*)?)", rest)
            if fm and not subject.father:
                subject.father = fm.group(1)
            nee_target = None
            continue

        if kw in ("mama", "mati"):
            mm = re.match(r"\s*(NN|[A-ZŠČŽĐĆ][\w.-]*(?:\s+[A-ZŠČŽĐĆ][\w.-]*)?)", rest)
            if mm and not subject.mother:
                subject.mother = mm.group(1)
                nee_target = "mother"
            continue

        if kw.startswith("botr") or kw == "boter":
            nm = re.match(r"\s*([^,;]{2,60})", rest)
            if nm:
                p.notes.append("Botri: " + nm.group(1).strip())
            continue

        if kw == "babica pri porodu":
            nm = re.match(r"\s*([A-ZŠČŽĐĆ][^,;]{2,40})", rest)
            if nm:
                p.notes.append("Babica: " + nm.group(1).strip())
            continue

        if kw.startswith("stanuj") or kw.startswith("stanoval") or kw.startswith("bival"):
            pl, decl = _take_place(rest)
            if pl:
                subject.resi.append((pl, decl))
            continue

        if kw.startswith("dvojč"):
            p.notes.append("Dvojček")
            continue

    for o in sorted(set(x.group(1).lower() for x in OCCU.finditer(original))):
        p.occu.append(o)
    seen, uniq = set(), []
    for n in p.notes:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    p.notes = uniq
    return p


# --------------------------------------------------------------------------
# Stages 3 & 4 -- outline -> INDI/FAM graph -> GEDCOM
# --------------------------------------------------------------------------

SOURCE_TITLE = "J. Hojnik: Dravčani – zgodovina Dravcev in njenih prebivalcev"
SOURCE_AUTHOR = "J. Hojnik"
SOURCE_PUBLISHER = "Univerzitetna založba Univerze v Mariboru"
SOURCE_URL = "https://press.um.si/index.php/ump/sl/catalog/book/931"
SOURCE_OBJE = "@O1@"


# ------------------------------------------------------------ name lexicon
NOM_SURNAME = re.compile(
    r"(?:^|,\s*)(?:rojena?|starša)\s+[A-ZŠČŽĐĆ][\w-]*(?:\s+in\s+[A-ZŠČŽĐĆ][\w-]*)?"
    r"|(?:^|,\s*)rojena\s+([A-ZŠČŽĐĆ][\w-]*)")


class Gazetteer:
    """How the book uses each name: as a given name, as a surname, or both.

    Read off the positions where the role is unambiguous -- the second word of
    an entry's head is a surname, the name after "oče"/"mama"/"starša" is a
    given name -- and then used to settle the cases where it is not.
    """

    def __init__(self, recs):
        self.given, self.surname = Counter(), Counter()
        for r in recs:
            toks = r.text.split()
            if len(toks) > 1 and toks[0][:1].isupper() and toks[1][:1].isupper():
                self.given[norm(toks[0])] += 1
                self.surname[norm(toks[1].strip(",;.()"))] += 1
            for m in re.finditer(r"\b(?:oče|mama|mati)\s+([A-ZŠČŽĐĆ][\wčšžćđ-]+)", r.text):
                self.given[norm(m.group(1))] += 1
            for m in re.finditer(r"\bstarša\s+([A-ZŠČŽĐĆ][\wčšžćđ-]+)\s+in\s+"
                                 r"([A-ZŠČŽĐĆ][\wčšžćđ-]+)", r.text):
                self.given[norm(m.group(1))] += 1
                self.given[norm(m.group(2))] += 1
            for m in re.finditer(r"\brojena?\s+([A-ZŠČŽĐĆ][\wčšžćđ-]{2,})", r.text):
                self.surname[norm(m.group(1))] += 1

    def is_surname(self, word):
        w = norm(word)
        return self.surname[w] > max(2, self.given[w])

    def swapped(self, given, surname):
        """True when the entry is written surname first: 'Cafuta Sonja'."""
        if not given or not surname:
            return False
        g, sn = norm(given), norm(surname)
        return (self.surname[g] > self.given[g]
                and self.given[sn] > self.surname[sn])


def build_lexicon(recs):
    """Surnames observed in the nominative, used later to undo declension."""
    lex = {}
    for r in recs:
        toks = r.text.split()
        if len(toks) > 1 and toks[0][:1].isupper() and toks[1][:1].isupper():
            s = toks[1].strip(",;.()")
            if s.isalpha():
                lex.setdefault(s.lower(), s)
        for m in re.finditer(r"\brojena?\s+([A-ZŠČŽĐĆ][\w-]{2,})", r.text):
            lex.setdefault(m.group(1).lower(), m.group(1))
    return lex


# ------------------------------------------------------------------ tree
class Tree:
    def __init__(self):
        self.indi = []       # Person objects, in id order
        self.fam = []        # dicts

    def add_person(self, p):
        p.xref = f"@I{len(self.indi) + 1}@"
        self.indi.append(p)
        return p

    def add_family(self, husb, wife, marriage=None):
        f = {"xref": f"@F{len(self.fam) + 1}@", "husb": husb, "wife": wife,
             "chil": [], "date": (marriage or {}).get("date", ""),
             "place": (marriage or {}).get("place", "")}
        self.fam.append(f)
        return f


def entry_key(text):
    """Identity key for a register entry: the sentence itself, normalised.

    A family that moved house is listed under both houses, and the author
    reuses the very same sentence.  Matching on the normalised sentence
    collapses those without any of the risk of fuzzy name/date matching --
    two genuine namesakes born the same day never share a whole sentence.
    """
    return re.sub(r"[^\w]+", " ", norm(text)).strip()


def build(recs, lexicon, dedupe=True, names=None):
    tree = Tree()
    names = names if names is not None else Gazetteer(recs)
    stack = {}            # level -> Person whose children nest under it
    seen = {}             # entry_key -> the Person already built for it
    tree.repeats = 0
    tree.unlinked = 0
    tree.relinked = 0
    tree.pending = []
    tree.skipped = []
    tree.anonymous = 0
    tree.continued = 0
    tree.coresidents = 0
    tree.structure = 0
    tree.land_records = []
    tree.prose_marriages = 0
    section, section_chapter, section_of = "", None, None
    coresident, coresident_of = "", None
    head = None
    for r in recs:
        # A person entry starts with a name and states at least one dated or
        # relational fact.  Anything else is a section heading, a property
        # transfer, or a remark -- worth reporting, since a few of them do
        # mention people the register never gives an entry to.
        text = LIST_LABEL.sub("", r.text, count=1)
        labelled = text is not r.text and text != r.text

        surname, sex = stillborn_child(text)
        if surname:
            # a child that never drew breath: named only by the family it was
            # born into, so it goes in as NN like the counted ones
            parent = stack.get(r.level - 1)
            body = parse_record(f"{UNNAMED} {surname}" + text[STILLBORN.match(text).end():],
                                r.page, r.level, lexicon, full_text=r.text)
            body.given, body.surname, body.sex = UNNAMED, surname, sex
            body.notes.append("Mrtvorojen otrok")
            if body.birth.get("date") and not body.death.get("date"):
                body.death = {"date": body.birth["date"], "place": "",
                              "known": True, "note": "mrtvorojen"}
            body.page, body.fams = r.page, []
            tree.add_person(body)
            if parent is not None:
                link_child(tree, parent, body)
            tree.anonymous += 1
            continue

        mm = PROSE_MARRIAGE.search(text)
        if mm and not MARKER.search(text[:mm.start()]):
            # "Na Dravci 2 se 25.1.1796 poročita Matevž Bezjak in Marija Zavec."
            tree.prose_marriages += _add_prose_marriage(tree, mm, text, r, lexicon)
            continue

        n, sex = counted_children(text)
        if n:
            parent = stack.get(r.level - 1)
            made = expand_counted(tree, text, n, sex, parent, r, lexicon)
            tree.anonymous += made
            if not made:
                tree.skipped.append((r.page, r.level,
                                     "counted children, no family to attach to",
                                     r.text))
            continue

        if HOUSE_DECL.match(r.text):
            continue                      # a house-name heading, read separately

        if is_land_record(r.text):
            # The property's history belongs to whoever headed the household
            # at the time -- the first person the section lists.  If none has
            # been read yet, hold it until one is.
            tree.land_records.append((r.text, r.page, head))
            continue

        other, other_addr = coresident_heading(r.text)
        if other:
            # break the lineage: nobody below belongs to the family above
            stack.clear()
            coresident = other_addr or r.chapter
            coresident_of = r.chapter
            continue
        # the household's non-family ends where the next section begins
        if r.chapter != coresident_of or SECTION_HEADING.match(r.text):
            coresident, coresident_of = "", r.chapter

        house, house_addr = section_house(r.text)
        if house:
            # A chapter can hold several households in turn -- Dravci 8 is
            # Habjaničevi, then Cafutovi, then Žlahtičevi -- so the name binds
            # to the people in this section, not to the address for all time.
            section, section_chapter = house, (house_addr or r.chapter)
            section_of = r.chapter
            head = None
            continue
        if r.chapter != section_of:
            section, section_chapter, section_of = "", r.chapter, r.chapter
            head = None

        why = _not_a_person(Rec(r.page, r.level, text, r.bullet, r.y))
        if why:
            if skip_kind(r.text) == "structure":
                tree.structure += 1
            else:
                tree.skipped.append((r.page, r.level, why, r.text))
            continue

        canon = seen.get(entry_key(r.text)) if dedupe else None
        if canon is not None:
            # Same entry, second chapter.  Reuse the person already built so
            # this chapter's descendants nest under it, and record where the
            # repeat was found instead of creating a twin.
            canon.notes.append(f"Isti vpis tudi na str. {r.page}")
            tree.repeats += 1
            _push(stack, r.level, canon)
            continue

        # Only rewrite an entry that opens as prose.  Applied to one that
        # already begins with a name, the rewrite would re-anchor on some
        # phrase further along -- "Katarina Breznik, ... umrla 1969 Slivniško
        # Pohorje" would become a person called Slivniško Pohorje.
        body = text
        if _split_head(text).prose or not re.match(r"^[A-ZŠČŽĐĆÖÜ]", text):
            body = normalise_prose(text)
        p = parse_record(body, r.page, r.level, lexicon,
                         full_text=r.text, names=names)
        if not p.given or p.prose:
            # A head that stays prose after re-anchoring names no one
            # ("Dve hčerki umrli v otroštvu") -- not a person.
            tree.skipped.append((r.page, r.level,
                                 "prose, names no one" if p.prose
                                 else "no name in the head", r.text))
            continue
        seen[entry_key(r.text)] = p
        p.page = r.page
        p.house, p.house_addr = section, (section_chapter or r.chapter)
        if head is None:
            head = p
            # records read before anyone in this section belong to its head
            tree.land_records[:] = [
                (t, pg, hd or head) for t, pg, hd in tree.land_records]
        if coresident:
            # GEDCOM has no household, only families -- so say where they
            # lived and that the house, not blood, is what connects them
            for addr in _expand_addrs(coresident) or [coresident]:
                if all(a != addr for a, _d in p.resi):
                    p.resi.append((addr, False))
            p.notes.append("Drugi prebivalec hiše, ne član družine")
            tree.coresidents += 1
        tree.add_person(p)
        p.fams = []
        # A spouse introduced here may have remarried, and that wedding was
        # recorded against her rather than against the entry's subject -- and
        # the new husband may in turn have remarried.  Keep going until every
        # marriage the sentence recorded has a family.
        pending = [p]
        while pending:
            person = pending.pop()
            for _f, sp in _wed(tree, person, person.marriages):
                if sp is not None and sp.marriages:
                    pending.append(sp)

        # "b) <the same man> drugič poročen ..." continues the entry above it,
        # so fold it in -- with its families, which is why this waits until
        # they exist.
        prev = stack.get(r.level)
        if (labelled and prev is not None and prev is not p and prev.given
                and same_given(p.given, prev.given)
                and norm(p.surname) == norm(prev.surname)):
            _absorb(tree, prev, p)
            tree.continued += 1
            _push(stack, r.level, prev)
            continue

        # Attach to the entry one level up -- but only if that entry both
        # agrees with the parent this one names and could be a parent at all.
        # Anything else is held over: the person it names may be recorded
        # elsewhere in the book, which we can only tell once all of them exist.
        parent = stack.get(r.level - 1)
        if parent is not None:
            pedi = pedigree_of(p)
            if pedi:
                # the book says outright this is not the household head's own
                link_child(tree, parent, p, pedi)
            elif not _parent_agrees(parent, p):
                tree.pending.append((p, parent, r.page, "names another parent"))
            elif not generation_ok(parent, p):
                tree.pending.append((p, parent, r.page, "implausible by year"))
            else:
                link_child(tree, parent, p)
        _push(stack, r.level, p)

    resolve_pending(tree)
    return tree


def resolve_pending(tree):
    """Second pass: try to place the children the indent could not justify.

    By now every entry in the book exists, so the parent a child names can be
    looked for across the whole register rather than just up the current
    indent.  A link is only made when exactly one person fits.
    """
    for child, enclosing, page, why in tree.pending:
        cands = parent_candidates(tree, child)
        if len(cands) == 1:
            link_child(tree, cands[0], child)
            child.notes.append(
                f"Starša določena po imenu iz vpisa, ne po zamiku (str. {page})")
            tree.relinked += 1
            continue
        stated = child.father if enclosing.sex != "f" else child.mother
        detail = (f"vpis navaja {stated}" if why == "names another parent"
                  else f"letnici rojstva se ne ujemata")
        if len(cands) > 1:
            detail += f"; ustreza {len(cands)} oseb"
        child.notes.append(
            f"Starš ni potrjen: {detail}, zamik pa ga uvršča pod "
            f"{enclosing.label()} (str. {page})")
        tree.unlinked += 1


def parent_candidates(tree, child):
    """People who could be the parent this child's own entry names.

    Requires the stated given name, a compatible surname, a plausible
    generation gap, and one corroboration: the candidate's spouse is the
    child's other stated parent, or they were written up on the same pages.
    """
    # Only the surname makes this safe.  Matching a mother instead was tried
    # and had to be dropped: her maiden name never matches the child's, which
    # leaves nothing but given names, and a pair like "Mihael in Marija"
    # confirms half the village.
    if not child.surname or not child.father or norm(child.father) == "nn":
        return []
    stated = child.father.split()[0]
    out = []
    for cand in tree.indi:
        if cand is child or not cand.given or cand.sex == "f":
            continue
        if norm(cand.surname) != norm(child.surname):
            continue
        if not same_given(stated, cand.given):
            continue
        if birth_year(cand) is None or birth_year(child) is None:
            continue                      # too little to go on either way
        if not generation_ok(cand, child):
            continue
        # one corroboration beyond the name: his wife is the mother the child
        # names, or the two were written up on the same pages
        spouse_confirms = bool(child.mother) and any(
            sp and sp.given and same_given(child.mother.split()[0], sp.given)
            for _f, sp in getattr(cand, "fams", []) if sp)
        # page 0 means "no entry of its own" (a spouse stub); proximity says
        # nothing about those, so it may not stand in for corroboration
        near = (cand.page and child.page
                and abs(cand.page - child.page) <= 2)
        if spouse_confirms or near:
            out.append(cand)
    return out


# "a) Jožef Korpič rojen 11.2.1846 ... prvič poročen ...", "b) Jožef Korpič
# drugič poročen ..." -- one man's entry split into labelled parts.
LIST_LABEL = re.compile(r"^(?:[a-zčšžA-ZČŠŽ]\)|\d+[.)])\s+")

# "Dve hčerki umrli v otroštvu", "Še trije otroci", "Dvojčiči, rojeni 2000"
# -- children the book counts but never names.
NUMERAL = {"en": 1, "ena": 1, "eden": 1, "dva": 2, "dve": 2, "oba": 2, "obe": 2,
           "trije": 3, "tri": 3, "štirje": 4, "štiri": 4, "pet": 5, "šest": 6,
           "sedem": 7, "osem": 8, "devet": 9, "deset": 10}
COUNTED = re.compile(r"^(?:še\s+)?(" + "|".join(NUMERAL) + r")\s+"
                     r"(otro\w*|hčer\w*|sin\w*|deklic\w*|dečk\w*)\b", re.I)
TWINS = re.compile(r"^(dvojčk\w*|dvojčič\w*|dvojčic\w*)\b", re.I)
UNNAMED = "NN"


def counted_children(text):
    """(how many, sex) for an entry that counts children without naming them.

    A trailing colon means the entry introduces a list that follows -- those
    children get their own entries and must not be invented here.
    """
    if text.rstrip().endswith(":"):
        return 0, ""
    m = COUNTED.match(text)
    if m:
        noun = m.group(2).lower()
        sex = ("f" if noun.startswith(("hčer", "deklic"))
               else "m" if noun.startswith(("sin", "dečk")) else "")
        return NUMERAL[m.group(1).lower()], sex
    if TWINS.match(text):
        return 2, ""
    return 0, ""


def expand_counted(tree, text, n, sex, parent, rec, lexicon):
    """Create the n unnamed children an entry only counts.

    They get NN for a given name and the household surname, so the family is
    the right size and the source line survives, without claiming an identity
    the book never gives.
    """
    if parent is None:
        return 0
    # children carry the father's surname; the entry they hang under may be
    # the mother, whose surname is her maiden name
    fam = getattr(parent, "fams", [{}])
    husb = fam[0][0]["husb"] if fam and fam[0][0].get("husb") else None
    sibling = next((c for f, _sp in getattr(parent, "fams", [])
                    for c in f["chil"] if c.surname), None)
    surname = ((husb.surname if husb and husb.surname else "")
               or (sibling.surname if sibling else "")
               or parent.surname)
    # re-read the sentence as if it were one child's, to pick up its facts
    stripped = COUNTED.sub("", text, count=1) if COUNTED.match(text) else \
        TWINS.sub("", text, count=1)
    template = parse_record(f"{UNNAMED} {surname}{stripped}",
                            rec.page, rec.level, lexicon)
    for _ in range(n):
        c = Person(given=UNNAMED, surname=surname, sex=sex,
                   source={"page": rec.page, "text": text})
        c.page = rec.page
        c.birth = dict(template.birth)
        c.death = dict(template.death)
        c.father, c.mother = template.father, template.mother
        c.mother_surname = template.mother_surname
        c.notes = list(template.notes) + [
            f"Neimenovan otrok: vpis navaja samo število ({n})"]
        c.fams = []
        tree.add_person(c)
        link_child(tree, parent, c)
    return n


def _absorb(tree, master, part):
    """Fold a labelled continuation record into the person it continues."""
    merge_person(master, part)
    master.fams = _uniq_fams(list(getattr(master, "fams", []))
                             + list(getattr(part, "fams", [])))
    master.marriages.extend(part.marriages)
    _rewrite(tree, {part: master})


# Structure the register is made of, not content the grammar failed on.  A
# numbered heading names the families a section covers; the land records track
# who owned the house.  Both are expected, so neither is worth a warning.
SECTION_HEADING = re.compile(r"^\d+\s+\S")
OWNERSHIP = re.compile(
    r"^(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{4}|leta\s+\d{4}|dne\s+\d|"
    r"(?:prvi|nov|drugi)\s+lastnik|do\s+leta\s+\d{4}|konec\s+\d)", re.I)
OWNERSHIP_WORDS = re.compile(
    r"\b(kupi|kupita|kupca|kupec|kupcema|proda|prodata|prodajo|prodala|"
    r"lastnik\w*|posest\w*|dražb\w*|domačij\w*|vpisan\w*|služnost\w*)\b", re.I)


# Some people are recorded with nothing but a name and what became of them:
# "Jožef Fošnarič, odšel v ZDA", "Franc Hentak, viničar, Dravci 11".  They are
# still people.  Admitting them safely needs a strict shape -- two or three
# capitalised words and then a comma, a dash, or the end of the line -- which
# a narrative sentence ("Teden dni po vpisu ...", "Kam sodi Jakob Krajnc, ...")
# does not have.
NAME_ONLY = re.compile(
    r"^[A-ZŠČŽĐĆÖÜ][\wčšžćđ-]+(?:\s+[A-ZŠČŽĐĆÖÜ][\wčšžćđ-]+){1,2}\s*(?:,|[-–—]|$)")


def is_land_record(text):
    """A sale, purchase or inheritance of the house -- the history of the
    property rather than of a person.

    Two shapes: a dated opening with an ownership word, or -- for the ones
    written as running prose -- ownership words in a line that is not somebody's
    own entry and reports no event about them.
    """
    if OWNERSHIP.match(text) and OWNERSHIP_WORDS.search(text):
        return True
    if len(OWNERSHIP_WORDS.findall(text)) < 2:
        return False
    if NAME_ONLY.match(text) or LEAD_EVENT.search(text) or PROSE_MARRIAGE.search(text):
        return False
    # "Elizabeta (Liza) Habjanič, rojena 26.10.1910, ... prevzemnica domačije"
    # is a person who happens to inherit; a land record opens with the land.
    head = MARKER.search(text)
    return not (head and head.start() < 60)


def skip_kind(text):
    """'structure' for the register's own scaffolding, 'unread' for content
    the grammar could not turn into a person."""
    if SECTION_HEADING.match(text):
        return "structure"
    if text.isupper():
        return "structure"            # the book's title, on every chapter page
    if text.rstrip().endswith(":") and COUNTED.match(text):
        return "structure"            # "Šest otrok ...:" -- they follow below
    # a lone word is a family-group heading -- "Brezniki", "Lozinški",
    # "Letonjevi" -- never a person, who needs at least a name and a fact
    if len(text.split()) == 1 and text[:1].isupper():
        return "structure"
    if OWNERSHIP.match(text) and OWNERSHIP_WORDS.search(text):
        return "structure"
    return "unread"


def _not_a_person(rec):
    """Why this outline record cannot be read as a person entry, or ''."""
    t = rec.text
    if len(t) < 12:
        return "too short"
    if not re.match(r"^[A-ZŠČŽĐĆÖÜ]", t):
        # "hlapec je bil Janez Fleis, rojen 18.10.1866" and "14.7.1892 umrla
        # Marija Mihelač" open with a trade or a date, but they still report
        # an event about someone the sentence names.
        return "" if LEAD_EVENT.search(t) else "does not start with a capital"
    if t.isupper():
        return "all caps (heading)"
    if not MARKER.search(t):
        # a bare "Ime Priimek, ..." is still a person, just an undated one,
        # and so is "Kasneje pride še Blaž Lozinšek, kočar"
        if NAME_ONLY.match(t) or LEAD_EVENT.search(t):
            return ""
        return "no genealogical marker"
    return ""


def _add_prose_marriage(tree, match, text, rec, lexicon):
    """Two people and their wedding, reported in a sentence of their own."""
    made = []
    for raw, sex in ((match.group(1), "m"), (match.group(2), "f")):
        body = parse_record(raw, rec.page, rec.level, lexicon, full_text=rec.text)
        if not body.given:
            return 0
        body.sex = body.sex or sex
        body.page, body.fams = rec.page, []
        tree.add_person(body)
        made.append(body)
    date, _n = parse_date(text[match.start():]) if False else (None, 0)
    d = RE_DMY.search(text) or RE_Y.search(text)
    when = parse_date(d.group(0))[0] if d else ""
    fam = tree.add_family(made[0], made[1], {"date": when or "", "place": ""})
    for person, spouse in ((made[0], made[1]), (made[1], made[0])):
        person.fams = [(fam, spouse)]
        person.notes.append(f"Poroka zabeležena v besedilu (str. {rec.page})")
    return 1


def _wed(tree, person, marriages):
    """Create a family for each of this person's marriages; returns the pairs."""
    if getattr(person, "fams", None) is None:
        person.fams = []
    made = []
    for mg in marriages:
        if mg.get("built"):
            continue
        mg["built"] = True
        sp = mg["spouse"]
        if sp is not None and getattr(sp, "xref", None) is None:
            tree.add_person(sp)
            sp.fams = []
        # Complementary roles.  When one sex is unknown the other decides,
        # otherwise two women can end up as husband and wife.
        if sp is not None and person.sex and sp.sex == person.sex:
            continue                      # not a couple; the entry misread it
        if person.sex == "f" or (not person.sex and sp is not None and sp.sex == "m"):
            h, w = sp, person
        else:
            h, w = person, sp
        f = tree.add_family(h, w, mg)
        person.fams.append((f, sp))
        if sp is not None:
            sp.fams.append((f, person))
        made.append((f, sp))
    return made


def _push(stack, level, person):
    stack[level] = person
    for lv in [k for k in stack if k > level]:
        del stack[lv]


# A parent is at least this much older than a child, and at most this much.
MIN_PARENT_AGE = 14
MAX_FATHER_AGE = 70
MAX_MOTHER_AGE = 50


def birth_year(p):
    d = _date_parts((p.birth or {}).get("date")) if p else None
    return d[0] if d else None


def generation_ok(parent, child):
    """Could this person plausibly be this child's parent, by year alone?

    Unknown on either side means 'no objection' -- the book leaves plenty of
    births undated.  Only two known years can rule a link out.
    """
    py, cy = birth_year(parent), birth_year(child)
    if py is None or cy is None:
        return True
    span = cy - py
    top = MAX_MOTHER_AGE if (parent.sex == "f") else MAX_FATHER_AGE
    return MIN_PARENT_AGE <= span <= top


def _parent_agrees(parent, child):
    """Does the child's own entry name the entry it is nested under as a parent?

    Only a stated name can disagree: entries that name no parent, name `NN`,
    or whose enclosing entry is prose rather than a name are accepted on the
    strength of the indent alone.
    """
    if getattr(parent, "prose", False) or not parent.given:
        return True
    stated = child.father if parent.sex != "f" else child.mother
    if not stated or norm(stated) == "nn":
        return True
    return same_given(stated.split()[0], parent.given)


# The book says outright when a child is not the household head's own.
PEDIGREE = [
    (re.compile(r"\bposvojen|\bposvojit", re.I), "adopted"),
    (re.compile(r"\bv reji pri\b|\brejen(?:ec|ka)\b", re.I), "foster"),
    (re.compile(r"\bpastor(?:ek|ka)\b|ko se mama poroči|"
                r"iz (?:prve|prejšnje) (?:zakona|poroke)", re.I), "foster"),
]


def pedigree_of(child):
    for rx, kind in PEDIGREE:
        if rx.search((child.source or {}).get("text", "")):
            return kind
    return None


def link_child(tree, parent, child, pedi=None):
    """Attach child to the right family of parent; returns the family or None."""
    fam = _pick_family(tree, parent, child)
    if fam is None:
        return None
    if all(c is not child for c in fam["chil"]):
        fam["chil"].append(child)
    child.famc = fam
    if pedi:
        child.pedi = pedi
    return fam


def _pick_family(tree, parent, child):
    """Choose which of the parent's marriages this child belongs to, using the
    mother/father named in the child's own entry."""
    fams = getattr(parent, "fams", [])
    if not fams:
        # the book names children without ever naming a spouse: keep the
        # single-parent family so the lineage link survives
        f = tree.add_family(parent if parent.sex != "f" else None,
                            parent if parent.sex == "f" else None)
        parent.fams = [(f, None)]
        return f
    if len(fams) == 1:
        return fams[0][0]
    want = norm(child.mother if parent.sex != "f" else child.father)
    for f, sp in fams:
        if sp and want and norm(sp.given) == want:
            return f
    return fams[0][0]


# --------------------------------------------------------------------------
# Stage 3.5 -- collapse the same person, and the same couple, recorded twice
# --------------------------------------------------------------------------
# Three kinds of duplicate survive stage 3:
#
#   * the same person written up in two house chapters, in different words
#     (the verbatim repeats are already folded away by entry_key);
#   * a person who has a full entry in one chapter and is named only as
#     someone's spouse in another -- a "stub";
#   * the couples that follow from the two above.
#
# Matching here beats matching the finished GEDCOM, because at this point we
# still know each person's stated parents, their spouses, whether the record
# is a stub, and the sentence and page they came from.

_MON = {m: i for i, m in enumerate(MONTHS, 1)}


def _date_parts(d):
    """'12 MAY 1900' -> (1900, 5, 12); '1900' -> (1900, 0, 0); '' -> None."""
    if not d:
        return None
    d = re.sub(r"^(ABT|EST|CAL|BEF|AFT)\s+", "", d.strip())
    m = re.match(r"^(?:(\d{1,2})\s+)?(?:([A-Z]{3})\s+)?(\d{4})$", d)
    if not m:
        m = re.match(r"^BET\s+(\d{4})", d)
        return (int(m.group(1)), 0, 0) if m else None
    return (int(m.group(3)), _MON.get(m.group(2) or "", 0), int(m.group(1) or 0))


def _precision(d):
    p = _date_parts(d)
    return 0 if not p else (3 if p[2] else (2 if p[1] else 1))


def _conflict(a, b):
    """True when two dates are both precise enough to disagree."""
    pa, pb = _date_parts(a), _date_parts(b)
    if not pa or not pb:
        return False
    if pa[0] != pb[0]:
        return True
    if pa[1] and pb[1] and pa[1] != pb[1]:
        return True
    return bool(pa[2] and pb[2] and pa[2] != pb[2])


def _parents_agree(a, b):
    """(agree_on_something, contradict) for two people's stated parents."""
    agree = contra = False
    for x, y in ((a.father, b.father), (a.mother, b.mother)):
        if not x or not y or norm(x) == "nn" or norm(y) == "nn":
            continue
        if same_given(x.split()[0], y.split()[0]):
            agree = True
        else:
            contra = True
    return agree, contra


def _spouse_names(p):
    out = set()
    for mg in p.marriages:
        sp = mg.get("spouse")
        if sp and sp.given:
            out.add((norm(sp.given), norm(sp.surname)))
    for _f, sp in getattr(p, "fams", []):
        if sp and sp.given:
            out.add((norm(sp.given), norm(sp.surname)))
    return out


def match_score(a, b):
    """Confidence 0-100 that a and b are one person, or None for 'not a pair'."""
    # --- vetoes ---------------------------------------------------------
    if a.given == UNNAMED or b.given == UNNAMED:
        return None                      # placeholders are distinct by design
    if a.sex and b.sex and a.sex != b.sex:
        return None
    if a.surname and b.surname and norm(a.surname) != norm(b.surname):
        return None
    if not same_given(a.given, b.given):
        return None
    if _conflict(a.birth.get("date"), b.birth.get("date")):
        return None
    if _conflict(a.death.get("date"), b.death.get("date")):
        return None
    agree, contra = _parents_agree(a, b)
    if contra:
        return None

    shared_spouse = bool(_spouse_names(a) & _spouse_names(b))
    ba, bb = _date_parts(a.birth.get("date")), _date_parts(b.birth.get("date"))

    if ba and bb:                                   # same year, no conflict
        if ba[2] and bb[2]:                         # both to the day
            return 100, "same name, identical birth date"
        if agree:
            return 95, "same name and birth year, parents agree"
        if shared_spouse:
            return 92, "same name and birth year, same spouse"
        return 85, "same name, same birth year"

    da, db = _date_parts(a.death.get("date")), _date_parts(b.death.get("date"))
    if da and db and da[2] and db[2]:
        return 80, "same name, identical death date, birth unknown"
    if agree and shared_spouse:
        return 78, "same name, parents and spouse agree, no dates"
    if agree:
        return 70, "same name, parents agree, no dates"
    if shared_spouse:
        return 65, "same name, same spouse, no dates"
    if (a.stub or b.stub) and (da or db):
        return 55, "same name, one record is a spouse mention"
    return 30, "same name only"


def _filled(p):
    return sum(bool(x) for x in (
        p.surname, p.sex, p.birth.get("date"), p.birth.get("place"),
        p.death.get("date"), p.death.get("place"), p.bapt.get("date"),
        p.father, p.mother, p.mother_surname)) + len(p.marriages) + len(p.occu)


def _better_date(x, y):
    return x if _precision(x) >= _precision(y) else y


def _merge_event(master, dup, attr):
    a, b = getattr(master, attr) or {}, getattr(dup, attr) or {}
    best = a if _precision(a.get("date", "")) >= _precision(b.get("date", "")) else b
    src = a if a.get("place") else b
    setattr(master, attr, {
        "date": best.get("date", ""),
        "place": src.get("place", ""),
        # keep whether that place was governed by a preposition -- it is what
        # tells the dictionary form from an inflected one later
        "declined": bool(src.get("declined")),
        "known": bool(a.get("known") or b.get("known")),
        "note": best.get("note", ""),
    })


def merge_person(master, dup):
    """Fold `dup` into `master`, keeping the better of every fact."""
    master.surname = master.surname or dup.surname
    master.alt_surname = master.alt_surname or dup.alt_surname
    master.sex = master.sex or dup.sex
    master.father = master.father or dup.father
    master.mother = master.mother or dup.mother
    master.mother_surname = master.mother_surname or dup.mother_surname
    master.married_surname = master.married_surname or dup.married_surname
    master.house = master.house or dup.house
    master.house_addr = master.house_addr or dup.house_addr
    master.nick = master.nick or dup.nick
    master.aka_given = master.aka_given or dup.aka_given
    for attr in ("birth", "death", "bapt"):
        _merge_event(master, dup, attr)
    for a in dup.occu:
        if a not in master.occu:
            master.occu.append(a)
    for a in dup.resi:
        if a not in master.resi:
            master.resi.append(a)
    for n in dup.notes:
        if n not in master.notes:
            master.notes.append(n)
    master.stub = master.stub and dup.stub
    # the other chapter's wording is evidence too -- keep it verbatim
    other = (dup.source or {}).get("text")
    if other and other != (master.source or {}).get("text"):
        master.extra_sources = getattr(master, "extra_sources", [])
        master.extra_sources.append(dup.source)


def dedupe_people(tree, min_score=85):
    """Union-find over candidate pairs, then merge each group into one person."""
    buckets = {}
    for p in tree.indi:
        if not p.given:
            continue
        key = (norm(p.surname), sorted(_VARIANT.get(norm(p.given.split("/")[0]),
                                                    {norm(p.given.split("/")[0])}))[0])
        buckets.setdefault(key, []).append(p)

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    order = {id(p): i for i, p in enumerate(tree.indi)}
    reasons = {}
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                got = match_score(bucket[i], bucket[j])
                if got and got[0] >= min_score:
                    union(bucket[i], bucket[j])
                    reasons[find(bucket[i])] = got[1]

    groups = {}
    for x in parent:
        groups.setdefault(find(x), []).append(x)

    canon = {}
    merged = 0
    for root, members in groups.items():
        if len(members) < 2:
            continue
        # a real entry beats a spouse mention; then the fullest; then the first
        members.sort(key=lambda p: (p.stub, -_filled(p),
                                    (p.source or {}).get("page", 0),
                                    order[id(p)]))
        master, dups = members[0], members[1:]
        master.notes.append(
            f"Združenih vpisov: {len(members)} – {reasons.get(root, '')}")
        for d in dups:
            merge_person(master, d)
            canon[d] = master
            merged += 1
    _rewrite(tree, canon)
    return merged


def _uniq_fams(pairs):
    """Dedupe (family, spouse) pairs by the family alone -- families are plain
    dicts so they cannot go in a set, and one family must not be listed twice
    just because two merged records named the spouse differently."""
    seen, out = set(), []
    for f, sp in pairs:
        if id(f) not in seen:
            seen.add(id(f))
            out.append((f, sp))
    return out


def _rewrite(tree, canon):
    """Replace every reference to a merged person with its master."""
    if not canon:
        return
    c = lambda p: canon.get(p, p) if p is not None else None
    tree.indi = [p for p in tree.indi if p not in canon]
    for f in tree.fam:
        f["husb"], f["wife"] = c(f["husb"]), c(f["wife"])
        f["chil"] = list(dict.fromkeys(c(x) for x in f["chil"]))
    for p in tree.indi:
        p.fams = _uniq_fams((f, c(sp)) for f, sp in getattr(p, "fams", []))
        for mg in p.marriages:
            mg["spouse"] = c(mg.get("spouse"))
    # a master inherits the families of everyone folded into it
    for dup, master in canon.items():
        master.fams = _uniq_fams(list(getattr(master, "fams", []))
                                 + [(f, c(sp)) for f, sp in getattr(dup, "fams", [])])
        famc = getattr(dup, "famc", None)
        if famc is not None:
            if getattr(master, "famc", None) is None:
                master.famc = famc
            # a disagreement is settled later, in reconcile()


def dedupe_families(tree):
    """Merge FAM records for the same couple; absorb an unambiguous half-couple."""
    groups = {}
    for f in tree.fam:
        if f["husb"] and f["wife"]:
            groups.setdefault((f["husb"], f["wife"]), []).append(f)

    by_spouse = {}
    for key in groups:
        for sp in key:
            by_spouse.setdefault(sp, set()).add(key)
    for f in tree.fam:
        if bool(f["husb"]) == bool(f["wife"]):
            continue                       # both known, or neither
        known = f["husb"] or f["wife"]
        targets = by_spouse.get(known, set())
        if len(targets) == 1:              # only absorb when there is no choice
            groups[next(iter(targets))].append(f)

    forder = {id(f): i for i, f in enumerate(tree.fam)}
    replace, merged = {}, 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda f: (0 if (f["husb"] and f["wife"]) else 1,
                                  -len(f["chil"]), forder[id(f)]))
        master, dups = group[0], group[1:]
        for d in dups:
            master["husb"] = master["husb"] or d["husb"]
            master["wife"] = master["wife"] or d["wife"]
            master["date"] = _better_date(master["date"], d["date"])
            master["place"] = master["place"] or d["place"]
            for ch in d["chil"]:
                if ch not in master["chil"]:
                    master["chil"].append(ch)
            replace[id(d)] = master
            merged += 1

    if replace:
        keep = [f for f in tree.fam if id(f) not in replace]
        for p in tree.indi:
            p.fams = _uniq_fams((replace.get(id(f), f), sp)
                                for f, sp in getattr(p, "fams", []))
            famc = getattr(p, "famc", None)
            if famc is not None:
                p.famc = replace.get(id(famc), famc)
        tree.fam = keep
    return merged


def _agreement(fam, child):
    """How many of the couple this child names are in this family."""
    score = 0
    for role, stated in (("husb", child.father), ("wife", child.mother)):
        par = fam[role]
        if par and par.given and stated and norm(stated) != "nn":
            if same_given(stated.split()[0], par.given):
                score += 2
            else:
                score -= 1
        if par and not generation_ok(par, child):
            score -= 2
    return score


def reconcile(tree):
    """Make FAMC and CHIL agree again after merging.

    Folding two records into one can leave a person listed as a child of two
    different couples.  Only one can be their parents, so keep the family the
    person already points at and drop them from the others, saying so.
    """
    listed = {}
    for f in tree.fam:
        for c in f["chil"]:
            listed.setdefault(id(c), []).append(f)
    dropped = 0
    for p in tree.indi:
        fams_as_child = listed.get(id(p), [])
        if not fams_as_child:
            p.famc = None
            continue
        famc = getattr(p, "famc", None)
        if len(fams_as_child) > 1:
            # Prefer the couple the person's own entry actually names, not
            # whichever link happened to be made first.
            famc = max(fams_as_child, key=lambda f: _agreement(f, p))
        elif famc is None or all(famc is not f for f in fams_as_child):
            famc = fams_as_child[0]
        p.famc = famc
        for f in fams_as_child:
            if f is not famc:
                f["chil"] = [c for c in f["chil"] if c is not p]
                dropped += 1
                p.notes.append(
                    "Drug vpis ga uvršča k drugim staršem -- preveri poreklo")
    for f in tree.fam:
        f["chil"] = list(dict.fromkeys(f["chil"]))
    return dropped


def prune_families(tree):
    """Drop families that carry no information: a lone parent, no children and
    nothing about a marriage.  They only exist because a child was attached and
    then moved away by reconcile()."""
    dead = [f for f in tree.fam
            if not f["chil"] and not (f["husb"] and f["wife"])
            and not f["date"] and not f["place"]]
    if not dead:
        return 0
    ids = {id(f) for f in dead}
    tree.fam = [f for f in tree.fam if id(f) not in ids]
    for p in tree.indi:
        p.fams = [(f, sp) for f, sp in getattr(p, "fams", []) if id(f) not in ids]
        if getattr(p, "famc", None) is not None and id(p.famc) in ids:
            p.famc = None
    return len(dead)


def flag_implausible(tree):
    """Note any parent-child pair the years cannot support.

    build() vetoes the entry a child is nested under, but the other half of
    the couple is whoever the marriage clause named -- and a child assigned to
    the wrong one of several wives shows up here.  These are flagged, not cut:
    the child is in the right family, only the spouse may be wrong.
    """
    n = 0
    for f in tree.fam:
        for role in ("husb", "wife"):
            par = f[role]
            if par is None:
                continue
            for c in f["chil"]:
                if generation_ok(par, c):
                    continue
                n += 1
                c.notes.append(
                    f"Letnici se ne ujemata: {par.label()} r. "
                    f"{par.birth.get('date', '?')} kot starš osebe r. "
                    f"{c.birth.get('date', '?')} – preveri")
    return n


# Rough spans used only where the book gives nothing better.
TYPICAL_MARRIAGE_AGE = 25
TYPICAL_GENERATION = 30
PRESUMED_DEAD_AGE = 100


def _marriage_year(person):
    years = [_date_parts(f["date"])[0]
             for f, _sp in getattr(person, "fams", [])
             if f["date"] and _date_parts(f["date"])]
    return min(years) if years else None


def estimate_births(tree):
    """Give an ABT birth year to people the book never dates.

    Each estimate says what it was derived from, so it can be judged or
    thrown away later.  Sources are tried best-evidence first, and the pass
    runs twice so an estimate can support the generation next to it.
    """
    n = 0
    for _round in range(2):
        for p in tree.indi:
            if p.birth.get("date"):
                continue
            dy = _date_parts(p.death.get("date"))
            my = _marriage_year(p)
            year = basis = None

            if dy and p.age_death:
                year, basis = dy[0] - p.age_death, "starosti ob smrti"
            elif my and p.age_marr:
                year, basis = my - p.age_marr, "starosti ob poroki"
            elif my:
                year, basis = my - TYPICAL_MARRIAGE_AGE, "leta poroke"
            else:
                famc = getattr(p, "famc", None)
                pyears = [birth_year(x) for x in (famc["husb"], famc["wife"])
                          if x is not None and birth_year(x)] if famc else []
                kids = [birth_year(c) for f, _sp in getattr(p, "fams", [])
                        for c in f["chil"] if birth_year(c)]
                if pyears:
                    year, basis = max(pyears) + TYPICAL_GENERATION, "letnic staršev"
                elif kids:
                    year, basis = min(kids) - TYPICAL_GENERATION, "letnic otrok"

            if year is None or not (1400 < year < 2100):
                continue
            p.birth = {"date": f"ABT {year}", "place": p.birth.get("place", ""),
                       "note": f"ocenjeno iz {basis}"}
            n += 1
    return n


def presume_deaths(tree, as_of):
    """Mark anyone born long enough ago that they cannot still be living."""
    n = 0
    for p in tree.indi:
        if p.death.get("date") or p.death.get("known"):
            continue
        by = birth_year(p)
        if by is None or as_of - by <= PRESUMED_DEAD_AGE:
            continue
        p.death = {"date": "", "place": p.death.get("place", ""), "known": True,
                   "note": f"domnevno, preko {PRESUMED_DEAD_AGE} let"}
        n += 1
    return n


# A land record names the people it concerns -- "kupita Jožef in Neža Korpič",
# "proda sosedom: Andreju Belšaku, Geri Korpič in Janezu ter Mariji Vidovič".
# The names are declined by whatever case the sentence puts them in.
NAME_PAIR = re.compile(
    r"\b([A-ZŠČŽĐĆÖÜ][\wčšžćđ-]{2,})\s+([A-ZŠČŽĐĆÖÜ][\wčšžćđ-]{2,})\b")


def _record_year(text):
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", text)
    return int(m.group(1)) if m else None


def people_named_in(tree, text, lexicon=None):
    """Everyone in the tree a land record names.

    Each capitalised pair is tried as written and de-declined both ways, since
    the sentence may put it in any case.  A record with a year only matches
    people who could have been alive in it.
    """
    year = _record_year(text)
    index = defaultdict(list)
    for p in tree.indi:
        if p.given and p.surname:
            index[norm(p.surname)].append(p)

    found = []
    for a, b in NAME_PAIR.findall(text):
        forms = [(a, b)]
        for sex in ("m", "f"):
            forms.append(denominalize(f"{a} {b}", sex, lexicon))
        for given, surname in forms:
            for cand in index.get(norm(surname), ()):
                if not same_given(given, cand.given):
                    continue
                by = birth_year(cand)
                if year and by and not (by <= year <= by + 100):
                    continue
                if all(cand is not x for x in found):
                    found.append(cand)
    return found


def attach_land_records(tree, lexicon=None):
    """Put each land record on everyone it names, not only the household head."""
    n = 0
    for text, page, head in tree.land_records:
        note = f"Zemljiški zapis (str. {page}): {text}"
        targets = people_named_in(tree, text, lexicon)
        if not targets and head is not None:
            targets = [head]          # nobody named -- keep it with the household
        for person in targets:
            if note not in person.notes:
                person.notes.append(note)
                n += 1
    return n


# Slovenian and German female given names end in -a, or in -e / -ine for the
# German forms the parish books use (Albertine, Wilhelmine, Rosalie).  Male
# names end in a consonant.  Used only where the sentence itself said nothing.
FEM_ENDING = re.compile(r"(a|ica|ina|ine|ette|ie)$", re.I)
# Slavic short forms end in -o and are male: Stanko, Janko, Marko, Slavko.
MASC_ENDING = re.compile(r"([bcčdfghjklmnprsštvzž]|[kntlrv]o)$", re.I)


def sex_gazetteer(tree):
    """given name -> sex, learned from the entries that state it.

    The book's own usage is the best authority available: a name is only
    accepted when every reading of it agrees.
    """
    counts = defaultdict(Counter)
    for p in tree.indi:
        if p.sex and p.given and p.given != UNNAMED:
            counts[norm(p.given.split("/")[0])][p.sex] += 1
    out = {}
    for name, c in counts.items():
        (sex, n), = c.most_common(1)
        if n == sum(c.values()):          # never seen as the other sex
            out[name] = sex
    return out


def infer_sex(tree):
    """Fill in SEX where the participles left it open."""
    known = sex_gazetteer(tree)
    n = 0
    for p in tree.indi:
        if p.sex or not p.given or p.given == UNNAMED:
            continue
        first = norm(p.given.split("/")[0].split()[0]) if p.given.split() else ""
        guess = known.get(first)
        if not guess and len(first) > 2:
            if FEM_ENDING.search(first):
                guess = "f"
            elif MASC_ENDING.search(first):
                guess = "m"
        if guess:
            p.sex = guess
            n += 1
    return n


def reindex(tree):
    """Renumber xrefs so the output is contiguous after merging."""
    for i, p in enumerate(tree.indi, 1):
        p.xref = f"@I{i}@"
    for i, f in enumerate(tree.fam, 1):
        f["xref"] = f"@F{i}@"


# --------------------------------------------------------------- GEDCOM
def married_surnames(person):
    """The surnames a woman was known by after marrying.

    The book states it outright often enough ("Blanka Kerec, poročena Slana");
    otherwise it is her husband's, which is how the parish books and the
    village would have written her.
    """
    if person.sex != "f":
        return []
    out, seen = [], {norm(person.surname)}
    for name in [person.married_surname] + [
            (fam["husb"].surname if fam["husb"] else "")
            for fam, _sp in getattr(person, "fams", [])]:
        key = norm(name)
        if name and key not in seen:
            seen.add(key)
            out.append(name.replace("/", " "))
    return out


def ged_escape(s):
    return (s or "").replace("@", "@@")


def emit(tree, out):
    w = out.write
    w("0 HEAD\n1 SOUR ged-tools/narrative_to_gedcom\n1 GEDC\n2 VERS 5.5.1\n"
      "2 FORM LINEAGE-LINKED\n1 CHAR UTF-8\n1 SUBM @SUB1@\n")
    w("0 @SUB1@ SUBM\n1 NAME Luka Renko\n")
    # The book is online, so the link is a multimedia object rather than a
    # note: readers can follow it, and it can be cited from more than one
    # record without repeating the address.
    w(f"0 {SOURCE_OBJE} OBJE\n"
      f"1 FILE {ged_escape(SOURCE_URL)}\n"
      "2 FORM URL\n"
      "3 TYPE electronic\n"
      f"2 TITL {ged_escape(SOURCE_TITLE)}\n")
    w("0 @S1@ SOUR\n"
      f"1 TITL {ged_escape(SOURCE_TITLE)}\n"
      f"1 AUTH {ged_escape(SOURCE_AUTHOR)}\n"
      f"1 PUBL {ged_escape(SOURCE_PUBLISHER)}\n"
      f"1 OBJE {SOURCE_OBJE}\n")

    for p in tree.indi:
        w(f"0 {p.xref} INDI\n")
        # "Sebastijan/Fabijan" -- the book gives both forms the records use.
        # A slash is GEDCOM's surname delimiter, so only the first form can go
        # in the primary NAME; the rest become aka names.
        forms = [f.strip() for f in (p.given or "").split("/") if f.strip()]
        given = forms[0] if forms else ""
        if not given and p.surname:
            given = UNNAMED                # surname known, first name not
        surname = (p.surname or "").replace("/", " ")
        w(f"1 NAME {ged_escape(given)} /{ged_escape(surname)}/\n")
        if p.alt_surname:
            w(f"2 NSFX {ged_escape(p.alt_surname)}\n")
        if p.nick:
            w(f"2 NICK {ged_escape(p.nick)}\n")
        for extra in forms[1:] + list(p.aka_given):
            w(f"1 NAME {ged_escape(extra)} /{ged_escape(surname)}/\n2 TYPE aka\n")
        for married in married_surnames(p):
            # surname only: the given name does not change on marrying
            w(f"1 NAME /{ged_escape(married)}/\n2 TYPE married\n")
        if p.sex:
            w(f"1 SEX {p.sex.upper()}\n")
        for tag, ev in (("BIRT", p.birth), ("CHR", p.bapt), ("DEAT", p.death)):
            if not ev:
                continue
            has_detail = ev.get("date") or ev.get("place")
            if not has_detail and not ev.get("known"):
                continue
            # GEDCOM spells "this happened, we have no details" as a bare Y
            w(f"1 {tag}" + ("" if has_detail else " Y") + "\n")
            if ev.get("date"):
                w(f"2 DATE {ev['date']}\n")
            if ev.get("place"):
                w(f"2 PLAC {ged_escape(ev['place'])}\n")
            if ev.get("note"):
                w(f"2 NOTE {ged_escape(ev['note'])}\n")
        for o in p.occu:
            w(f"1 OCCU {ged_escape(o)}\n")
        for r, _decl in p.resi:
            w(f"1 RESI\n2 PLAC {ged_escape(r)}\n")
        for n in dict.fromkeys(p.notes):
            w(f"1 NOTE {ged_escape(n)}\n")
        cited = False
        for src in [p.source or {}] + getattr(p, "extra_sources", []):
            if src.get("text"):
                cited = True
                w(f"1 SOUR @S1@\n2 PAGE str. {src.get('page','')}\n2 DATA\n"
                  f"3 TEXT {ged_escape(src['text'])}\n")
        if cited:
            w(f"1 OBJE {SOURCE_OBJE}\n")
        famc = getattr(p, "famc", None)
        if famc:
            w(f"1 FAMC {famc['xref']}\n")
            if p.pedi:
                w(f"2 PEDI {p.pedi}\n")
        for f, _ in getattr(p, "fams", []):
            w(f"1 FAMS {f['xref']}\n")

    for f in tree.fam:
        w(f"0 {f['xref']} FAM\n")
        if f["husb"]:
            w(f"1 HUSB {f['husb'].xref}\n")
        if f["wife"]:
            w(f"1 WIFE {f['wife'].xref}\n")
        if f["date"] or f["place"]:
            w("1 MARR\n")
            if f["date"]:
                w(f"2 DATE {f['date']}\n")
            if f["place"]:
                w(f"2 PLAC {ged_escape(f['place'])}\n")
        for c in f["chil"]:
            w(f"1 CHIL {c.xref}\n")
    w("0 TRLR\n")

# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pdf")
    ap.add_argument("first", type=int, help="first printed page number")
    ap.add_argument("last", type=int, help="last printed page number")
    ap.add_argument("--page-offset", type=int, default=14,
                    help="pdf page index - printed page number (default 14)")
    ap.add_argument("-o", "--out", default="narrative.ged")
    ap.add_argument("--dump-outline", action="store_true",
                    help="print the levelled outline and stop")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="emit every entry as its own person, merging nothing")
    ap.add_argument("--no-estimates", action="store_true",
                    help="do not infer ABT birth years or presume deaths")
    ap.add_argument("--as-of", type=int, default=date.today().year, metavar="YEAR",
                    help="year used to presume death after "
                         f"{PRESUMED_DEAD_AGE} years (default: this year)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="summarise skipped records instead of listing them")
    ap.add_argument("--min-confidence", type=int, default=85, metavar="N",
                    help="confidence needed to merge two people, 0-100 "
                         "(default 85; see match_score for the scoring table)")
    a = ap.parse_args(argv)

    recs = extract(a.pdf, a.first, a.last, a.page_offset)
    if a.dump_outline:
        for r in recs:
            print(f'{r.page} L{r.level} {"  " * r.level}{r.text}')
        return 0

    tree = build(recs, build_lexicon(recs), dedupe=not a.no_dedupe)
    raw_people, raw_fams = len(tree.indi), len(tree.fam)
    people_merged = fams_merged = 0
    if not a.no_dedupe:
        people_merged = dedupe_people(tree, a.min_confidence)
        fams_merged = dedupe_families(tree)
    contested = reconcile(tree)
    # judge the recorded dates before inventing any, so an estimate can never
    # be the evidence that condemns a link
    sexed = infer_sex(tree)
    implausible = flag_implausible(tree)
    tree.land = attach_land_records(tree, build_lexicon(recs))
    normalised = canonicalise_places(tree)
    tagged = annotate_places(tree, collect_houses(recs))
    dedupe_residences(tree)
    estimated = 0 if a.no_estimates else estimate_births(tree)
    presumed = 0 if a.no_estimates else presume_deaths(tree, a.as_of)
    prune_families(tree)
    reindex(tree)

    with open(a.out, "w", encoding="utf-8") as fh:
        emit(tree, fh)

    print(f"{len(tree.indi)} persons, {len(tree.fam)} families -> {a.out}",
          file=sys.stderr)
    for page, level, why, text in getattr(tree, "skipped", []):
        if not a.quiet:
            print(f"warning: skipped p.{page} L{level} ({why}): {text}",
                  file=sys.stderr)
    if sexed:
        print(f"  {sexed} sex(es) inferred from the given name", file=sys.stderr)
    if getattr(tree, "prose_marriages", 0):
        print(f"  {tree.prose_marriages} marriage(s) read from a sentence "
              f"rather than an entry", file=sys.stderr)
    if getattr(tree, "land", 0):
        print(f"  {len(tree.land_records)} land record(s) attached to "
              f"{tree.land} person(s) they name", file=sys.stderr)
    if getattr(tree, "structure", 0):
        print(f"  {tree.structure} section heading(s) skipped as expected",
              file=sys.stderr)
    if tree.skipped:
        by_reason = Counter(why for _p, _l, why, _t in tree.skipped)
        print(f"  {len(tree.skipped)} record(s) the grammar could not read: "
              + ", ".join(f"{n} {why}" for why, n in by_reason.most_common()),
              file=sys.stderr)
    if getattr(tree, "repeats", 0):
        print(f"  {tree.repeats} verbatim repeats folded at source",
              file=sys.stderr)
    if getattr(tree, "continued", 0):
        print(f"  {tree.continued} labelled continuation record(s) folded into "
              f"the entry above", file=sys.stderr)
    if getattr(tree, "anonymous", 0):
        print(f"  {tree.anonymous} unnamed child(ren) created as {UNNAMED} from "
              f"entries that only count them", file=sys.stderr)
    if people_merged or fams_merged:
        print(f"  {people_merged} duplicate persons merged "
              f"(of {raw_people}), {fams_merged} duplicate families "
              f"(of {raw_fams}), at >= {a.min_confidence}% confidence",
              file=sys.stderr)
    if getattr(tree, "relinked", 0):
        print(f"  {tree.relinked} children re-linked to the parent their own "
              f"entry names, found elsewhere in the book", file=sys.stderr)
    pedi = sum(1 for p in tree.indi if p.pedi)
    if pedi:
        print(f"  {pedi} adopted/foster link(s) recorded as such", file=sys.stderr)
    if normalised:
        print(f"  {normalised} place name(s) normalised to one spelling",
              file=sys.stderr)
    if getattr(tree, "coresidents", 0):
        print(f"  {tree.coresidents} person(s) recorded as sharing a house "
              f"without belonging to its family", file=sys.stderr)
    if tagged:
        print(f"  {tagged} place(s) annotated with a \"po domače\" house name",
              file=sys.stderr)
    if estimated:
        print(f"  {estimated} birth year(s) estimated as ABT (see event NOTE)",
              file=sys.stderr)
    if presumed:
        print(f"  {presumed} person(s) presumed dead, over {PRESUMED_DEAD_AGE} "
              f"years since birth", file=sys.stderr)
    if implausible:
        print(f"  {implausible} surviving parent-child pair(s) flagged as "
              f"implausible by year (see NOTE)", file=sys.stderr)
    if getattr(tree, "unlinked", 0):
        print(f"  {tree.unlinked} children left unlinked: the entry names a "
              f"different parent than the indent (see NOTE)", file=sys.stderr)
    if contested:
        print(f"  {contested} contested parent link(s) resolved to one family "
              f"(see NOTE)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
