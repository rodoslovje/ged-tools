"""Transcribe a Matricula Online parish book into an index spreadsheet (.xlsx).

Takes the URL of a book on data.matricula-online.eu, walks it page by page,
has Claude read the scanned register, and writes a workbook in the column
layout that matricula_to_json.py consumes:

    python tools/matricula_transcribe.py <book-url> \
        --interpret Priimek_Ime --output-dir data/matricula/<contributor>

Birth books ('Krstna knjiga' / 'Taufbuch') produce the K layout, marriage
books ('Poročna knjiga' / 'Trauungsbuch') the P layout. Death books are
recognised but refused: matricula_to_json.py has no death column set yet.

The book page carries the whole page list in its viewer config, so one HTML
request per book is enough; the scans themselves come from the image host the
viewer points at. Downloads and transcriptions are both cached on disk, so a
re-run only pays for pages it has not seen — and --pages lets you transcribe a
book in batches.

Handwriting from the 18th and 19th century is not OCR-able; every row is a
model's best reading and needs a human pass before it is published. Rows the
model was unsure about name those fields in a 'preveri' column of their own.
"""

import argparse
import base64
import collections
import concurrent.futures
import hashlib
import io
import json
import os
import re
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import openpyxl

try:
    import certifi
except ImportError:  # falls back to whatever CA store this Python was built with
    certifi = None

MATRICULA_HOST = "data.matricula-online.eu"
USER_AGENT = "ged-tools/matricula_transcribe"
FETCH_TIMEOUT = 60
IMAGE_CACHE = ".matricula_images"
TEXT_CACHE = ".matricula_transcribe.cache"

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
# Bumped whenever the prompt or the schema changes, so cached transcriptions
# from an older prompt are not silently reused.
PROMPT_VERSION = 7

# Anthropic downscales images to fit 1568px on the long edge, so a full
# two-page spread (~2400px wide) loses a third of its detail. Cutting the
# spread into vertical strips keeps each strip under that limit; the strips
# overlap so a column split across the seam is still whole in one of them.
TILE_OVERLAP = 0.12
MAX_IMAGE_EDGE = 1568

# Column layouts, in sheet order. matricula_to_json.py matches these headers
# case- and accent-insensitively; 'naslov neveste' is not one of its fields
# but the human template carries it, so it is kept for the reviewer.
BIRTH_COLUMNS = [
    ("seq", "zp. št."),
    ("parish", "župnija"),
    ("birth_date", "datum rojstva"),
    ("baptism_date", "datum krsta"),
    ("address", "naslov"),
    ("child_name", "ime otroka"),
    ("father_name", "ime očeta"),
    ("father_surname", "priimek očeta"),
    ("father_alt_surname", "alt. priimek očeta"),
    ("mother_name", "ime matere"),
    ("mother_surname", "priimek matere"),
    ("mother_alt_surname", "alt. priimek matere"),
    ("url", "url naslov"),
    ("notes", "opombe"),
    ("interpret", "interpret"),
]

MARRIAGE_COLUMNS = [
    ("seq", "zp. št."),
    ("parish", "župnija"),
    ("marriage_date", "datum poroke"),
    ("address", "naslov"),
    ("groom_name", "ime ženina"),
    ("groom_surname", "priimek ženina"),
    ("groom_alt_surname", "alt. priimek ženina"),
    ("bride_address", "naslov neveste"),
    ("bride_name", "ime neveste"),
    ("bride_surname", "priimek neveste"),
    ("bride_alt_surname", "alt. priimek neveste"),
    ("url", "url naslov"),
    ("notes", "opombe"),
    ("interpret", "interpret"),
]

COLUMNS = {"K": BIRTH_COLUMNS, "P": MARRIAGE_COLUMNS}

# Fields the model was unsure of are named in their own column rather than
# appended to 'opombe': a reviewer can sort or filter on it, and deleting the
# column when the index is ready cannot take any real note with it.
# matricula_to_json.py does not know this header, so it ignores it.
FLAG_COLUMN = ("flags", "preveri")


def columns_for(kind, add_flags):
    return COLUMNS[kind] + ([FLAG_COLUMN] if add_flags else [])

# Fields the model fills in. 'seq', 'parish', 'url' and 'interpret' are known
# without reading the scan and are filled in by this script, not by the model.
BIRTH_MODEL_FIELDS = [
    "birth_date", "baptism_date", "address", "child_name",
    "father_name", "father_surname", "father_alt_surname",
    "mother_name", "mother_surname", "mother_alt_surname", "notes",
]

MARRIAGE_MODEL_FIELDS = [
    "marriage_date", "address",
    "groom_name", "groom_surname", "groom_alt_surname",
    "bride_address", "bride_name", "bride_surname", "bride_alt_surname",
    "notes",
]

MODEL_FIELDS = {"K": BIRTH_MODEL_FIELDS, "P": MARRIAGE_MODEL_FIELDS}


# --------------------------------------------------------------------------
# Book page: metadata and the list of scans
# --------------------------------------------------------------------------

def _ssl_context():
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def http_get(url, referer=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if referer:
        request.add_header("Referer", referer)
    with urllib.request.urlopen(
        request, timeout=FETCH_TIMEOUT, context=_ssl_context()
    ) as response:
        return response.read()


def normalize_book_url(url):
    """Return the book URL pinned to page 1, where the viewer config lives."""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != MATRICULA_HOST:
        raise SystemExit(f"not a {MATRICULA_HOST} URL: {url}")
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return urllib.parse.urlunparse(
        (parsed.scheme or "https", parsed.netloc, path, "", "pg=1", "")
    )


def page_url(book_url, page):
    """The canonical viewer URL of one page — what goes in 'url naslov'."""
    return re.sub(r"pg=\d+", f"pg={page}", book_url)


def parse_book_page(html):
    """Pull the title and the full list of scan URLs out of the viewer config.

    The viewer is initialised with every page of the book at once ('files' is
    a list of base64-wrapped paths on the image host), so page 1 alone tells
    us how long the book is and where each scan lives.
    """
    title_match = re.search(r"<title>(.*?)</title>", html, re.S)
    title = title_match.group(1).strip() if title_match else ""

    files_match = re.search(r'"files":\s*(\[.*?\]),', html, re.S)
    if not files_match:
        raise SystemExit(
            "no image list in the book page — the URL may not point at a book, "
            "or matricula changed its viewer"
        )
    files = json.loads(files_match.group(1))

    images = []
    for entry in files:
        # '/image/<base64 of the real URL>/'
        token = entry.strip("/").split("/")[-1]
        padded = token + "=" * (-len(token) % 4)
        images.append(base64.b64decode(padded).decode("utf-8"))
    return title, images


BOOK_KIND_MARKERS = [
    ("K", ("krstna", "taufbuch", "geburts", "rojstna")),
    ("P", ("poročna", "porocna", "trauungs", "trauung")),
    ("M", ("mrliška", "mrliska", "sterbe", "totenbuch")),
]


def detect_kind(title, image_path):
    """Return 'K', 'P' or 'M' from the page title, or from the scan filename.

    Titles are localised ('Poročna knjiga / Trauungsbuch'), and the scans sit
    in a directory named after the book ('P Podzemelj 1873-1922'), so the
    directory's leading letter is a reliable second opinion.
    """
    lowered = title.lower()
    for kind, markers in BOOK_KIND_MARKERS:
        if any(marker in lowered for marker in markers):
            return kind
    return parse_stem(book_stem(image_path))[0]


def book_stem(image_path):
    """'P Podzemelj 1873-1922' — the scan directory names the book itself."""
    if not image_path:
        return ""
    parts = urllib.parse.urlparse(image_path).path.rstrip("/").split("/")
    if len(parts) < 2:
        return ""
    return urllib.parse.unquote(parts[-2])


# The archive names scan folders 'R Podzemelj 1675-1703' — R for rojstna
# (births), where the index spreadsheets use K for krstna.
STEM_KINDS = {"R": "K", "K": "K", "P": "P", "M": "M"}


def parse_stem(stem):
    """Split 'R Podzemelj 1675-1703' into ('K', 'Podzemelj', '1675-1703').

    A few folders carry a trailing qualifier after the years ('M Podzemelj
    1728-1803-nemški viteški red'); the years still come out of it.
    """
    match = re.match(
        r"^([A-Za-z])\s+(.*?)\s+(\d{4}\s*-\s*\d{4})(?:\s*[-–].*)?$", stem or ""
    )
    if not match:
        return None, "", ""
    years = re.sub(r"\s*-\s*", "-", match.group(3))
    return STEM_KINDS.get(match.group(1).upper()), match.group(2).strip(), years


def lookup_book_span(book_url):
    """Year range for this book from its parish's listing page, or ''.

    The listing carries 'Od datuma'/'Do datuma' for every book of the parish;
    that is the range the existing index filenames follow, and it can differ
    from the range the scan folder was named after. Reuses the parser
    matricula_to_json.py already applies to the same page.
    """
    try:  # importable as 'tools.matricula_to_json' and as a sibling script
        from tools import matricula_to_json as mj
    except ImportError:
        import matricula_to_json as mj

    parsed = urllib.parse.urlparse(book_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return ""
    signature = parts[-1]
    parish_url = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, "/" + "/".join(parts[:-1]) + "/", "", "", "")
    )
    try:
        books = mj.parse_parish_books(http_get(parish_url).decode("utf-8", "replace"))
    except Exception as error:  # naming falls back to the scan folder
        print(f"note: could not read the parish book list ({error})")
        return ""
    return (books.get(signature) or {}).get("date", "")


def parish_from_url(book_url):
    """The parish slug sits second-to-last in the path: .../podzemelj/04455/."""
    parts = [p for p in urllib.parse.urlparse(book_url).path.split("/") if p]
    if len(parts) < 2:
        return ""
    return parts[-2].replace("-", " ").title()


def signature_from_url(book_url):
    parts = [p for p in urllib.parse.urlparse(book_url).path.split("/") if p]
    return parts[-1] if parts else "book"


# --------------------------------------------------------------------------
# Scans
# --------------------------------------------------------------------------

def parse_pages(spec, total):
    """Expand '5-40', '5,7,9-12' or '' into a sorted list of page numbers."""
    if not spec:
        return list(range(1, total + 1))
    pages = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.match(r"^(\d+)\s*-\s*(\d+)$", chunk)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
        elif chunk.isdigit():
            start = end = int(chunk)
        else:
            raise SystemExit(f"cannot read --pages value: {chunk!r}")
        if start > end:
            raise SystemExit(f"--pages range runs backwards: {chunk!r}")
        pages.update(range(start, end + 1))
    out = sorted(p for p in pages if 1 <= p <= total)
    if not out:
        raise SystemExit(f"--pages {spec!r} selects nothing in a {total}-page book")
    return out


def download_scan(image_url, dest, book_url, delay):
    """Fetch one scan into the image cache; already-cached scans are kept."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return False
    data = http_get(image_url, referer=book_url)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, dest)
    if delay:
        time.sleep(delay)
    return True


def tile_scan(path, tiles):
    """Return [whole page, strip, ...] as JPEG bytes, ready for the API.

    The whole page goes first so the model can see the table's structure; the
    strips follow at a resolution the API will not shrink as far, which is
    what makes individual names legible. Strips are cut vertically because a
    register row runs the full width of the spread — cutting horizontally
    would split entries in half.

    Two strips is the sweet spot for a spread: the scan is taller than 1568px,
    so the height already sets the scale and cutting into more than one strip
    per book page buys no further detail, only more images to pay for.
    """
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        width, height = image.size
        parts = [_encode(image)]
        if tiles > 1:
            step = width / tiles
            margin = step * TILE_OVERLAP
            for index in range(tiles):
                left = max(0, int(index * step - margin))
                right = min(width, int((index + 1) * step + margin))
                parts.append(_encode(image.crop((left, 0, right, height))))
        return parts


def _encode(image):
    """JPEG-encode, shrinking anything the API would shrink anyway."""
    longest = max(image.size)
    if longest > MAX_IMAGE_EDGE:
        from PIL import Image as PILImage

        scale = MAX_IMAGE_EDGE / longest
        size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        image = image.resize(size, PILImage.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You transcribe scanned Roman Catholic parish registers from Slovenia into a \
genealogical index. The books were kept in Latin or German, in 18th- and \
19th-century handwriting, on pre-printed tabular forms. You are reading them \
for a Slovenian genealogy project, so the index is written in modern Slovenian.

House rules the project's indexers follow, which you must follow too:

- Given names go into today's Slovenian form: Joannes/Johann/Ivan -> Janez, \
Maria -> Marija, Georgius/Georg -> Jurij, Josephus -> Jožef, Agnes -> Neža, \
Gertrud -> Jera, Catharina -> Katarina, Matthias -> Matija, Antonius -> Anton.
- Surnames go into today's Slovenian spelling (Covacig / Kowatschitsch -> \
Kovačič).
- The 'alt_surname' field is a LITERAL transcription of the letters that stand \
in the book for that surname — never a variant you construct, and never a \
re-spelling of the modern form. Copy what is written, letter for letter. If \
the book already writes the surname the modern way, leave 'alt_surname' \
EMPTY; do not manufacture a difference to fill it. If you cannot make out the \
book's spelling well enough to copy it, leave it empty rather than guessing at \
one. (A widow bride is the one exception — see the marriage rules.)
- Slovenian is written with č, š and ž. The letters ć, đ, ě, ř and ś do not \
belong in a Slovenian surname, and 19th-century clerks writing German or Latin \
did not use them either — they wrote -tsch, -ch or -cz. Never turn a č into a \
ć: 'Jakofčič' stays 'Jakofčič'. Use ć only if that exact letter is \
unmistakably on the page.
- An address is the village or street plus the house number ('Radovica 72', \
'Krašnji vrh 15'). If the entry says the place belongs to another parish, \
append that: 'Božakovo 6, ž. Metlika'.
- Place names take today's Slovenian form, the same way given names and \
surnames do. These registers were kept under Austrian administration and name \
places in German: Möttling -> Metlika, Tschernembl -> Črnomelj, Gottschee -> \
Kočevje, Rudolfswerth and Neustadtl -> Novo mesto, Laibach -> Ljubljana, \
Oberlaibach -> Vrhnika, Krainburg -> Kranj, Adelsberg -> Postojna, Pettau -> \
Ptuj, Marburg -> Maribor, Cilli -> Celje, Klagenfurt -> Celovec, Graz -> \
Gradec. Index the Slovenian name. Where the register's own form is worth \
showing a reviewer — a place outside today's Slovenia, or a name you are not \
certain you have matched — keep it in brackets after the Slovenian one: \
'Celje (Cilli) 53', 'Celovec (Klagenfurt) 347'. A village whose name the \
register already gives in Slovene is simply copied.
- Dates are ISO: 'yyyy-mm-dd'. If only a month or a year can be read, give \
'yyyy-mm' or 'yyyy'. Never invent a date component. The year is often written \
once at the top of a block and left implicit further down — carry it forward.
- Notes ('notes') record what departs from the ordinary, and nothing else. \
Write 'vdovec' / 'vdova' when the form marks a widower or a widow; never write \
'samski' / 'samska', which is what the columns say by default and adds \
nothing. Write 'nezakonski' for an illegitimate birth; never write 'zakonski'. \
Never record the priest who officiated — every entry has one and it \
identifies nobody. Do record a noted death date, twins, a legitimation, a \
later marginal annotation, and an entry struck out.
- Keep notes compact: short clauses, no full sentences, no repetition of \
anything that already stands in its own column.

Read every entry on the spread, top to bottom, and return them in that order \
— the index numbers each entry by where it sits on the page, so the order you \
return them in is the order they are numbered. Leave a field \
as an empty string when the book does not give it — never guess a name, a \
surname or a number that you cannot actually read. When you can read a field \
only partially or are unsure of it, still give your best reading and name that \
field in 'uncertain' so a human reviewer can check it.

Some pages carry no entries at all: covers, blank leaves, printed title pages, \
and the alphabetical index at the back of the book. For those return an empty \
'rows' list and say which kind of page it is in 'page_note'.\
"""

KIND_PROMPT = {
    "K": """\
This is a page from a baptism register (Krstna knjiga / Taufbuch). Each entry \
records one child: date of birth and date of baptism, the house the child was \
born in, the child's given name, and the father's and mother's names. The \
father's surname is the family surname; record the mother's own (maiden) \
surname, which the form usually gives together with her parents.

'child_name' is the given name only — the surname is the father's and is not \
repeated in the index.

Godparents ('botri') are worth recording in 'notes', after anything noted \
about the child itself:

  Nezakonski. Botra: Marija Simec in Jurij Kralj.\
""",
    "P": """\
This is a page from a marriage register (Poročna knjiga / Trauungsbuch). Each \
entry records one wedding: the date, the groom (residence with house number, \
given name, surname) and the bride (residence with house number, given name, \
surname). On the wider Austrian forms the spread continues on the right-hand \
page with the parents of the groom and of the bride, the witnesses, and the \
documents — read the whole spread as one table, matching each right-hand row \
to the left-hand entry on the same line.

'address' is the groom's residence, 'bride_address' the bride's. Record the \
bride under her own maiden surname.

A WIDOW remarrying needs care, because the register enters her under the \
surname she carries on the day — her dead husband's — and that is not the \
name the index files her under:

  * 'bride_surname' is her MAIDEN surname. The register gives it through her \
parents ('starša ...'): her father's surname is her maiden one. It may also \
be annotated directly, as 'r.' or 'roj.' (rojena, née) before a surname, \
sometimes added later in different ink.
  * 'bride_alt_surname' is the surname she carried AT THIS WEDDING — the \
previous husband's, which is normally the one written in the name column.

So a widow the book calls 'Anna Filak', whose parents are given as Marko \
Vardijan and Rozalija Vanjaš, is indexed as bride_surname 'Vardijan', \
bride_alt_surname 'Filak'. For a widow, this use of 'bride_alt_surname' \
overrides the literal-spelling rule; if the book also spells her married name \
unusually, put that spelling in 'notes' as 'v knjigi <spelling>'.

Widowhood is marked by a tick in the 'Witwe' column and by wording such as \
'vdova', 'po prvem možu X', 'vdova po X'. A groom's surname does not change \
when he marries, so a widower needs none of this.

The parents and the witnesses are the main way a researcher confirms a match, \
so put them in 'notes' when they are legible. Order the note groom first, then \
bride, then the witnesses they share:

  Ženin: vdovec, 42 let; starša Matija Jakofčič, kmet, in Barbara Simec. \
Nevesta: 32 let; starša Matija Križan, kmet, in Marija Milek. Priči: Matija \
Jakofčič, Miha Križan.

Give an age only as the form gives it. Drop a half of the note entirely when \
the form gives nothing for it — a note with no witnesses simply ends after the \
bride.\
""",
}


def build_schema(kind):
    fields = MODEL_FIELDS[kind]
    row = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in fields},
        "additionalProperties": False,
        "required": list(fields),
    }
    row["properties"]["uncertain"] = {
        "type": "array",
        "items": {"type": "string", "enum": fields},
    }
    row["required"].append("uncertain")
    return {
        "type": "object",
        "properties": {
            "page_note": {"type": "string"},
            "rows": {"type": "array", "items": row},
        },
        "required": ["page_note", "rows"],
        "additionalProperties": False,
    }


def transcribe_page(client, kind, images, model, effort, max_tokens):
    """Send one spread to the model and return its parsed reading."""
    content = []
    for index, blob in enumerate(images):
        if index == 0:
            label = "The full two-page spread:"
        else:
            label = f"Vertical strip {index} of {len(images) - 1}, enlarged:"
        content.append({"type": "text", "text": label})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(blob).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": KIND_PROMPT[kind]})

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": build_schema(kind)},
        },
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "model hit --max-tokens before finishing the page; raise it and re-run"
        )
    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError(f"model returned no text (stop_reason={response.stop_reason})")
    usage = response.usage
    return json.loads(text), (usage.input_tokens, usage.output_tokens)


# Errors that come back identically for every page, with what to do about them.
API_ERROR_HINTS = [
    ("anthropic-workspace-id",
     "This API key is identity-linked, so every request has to name the\n"
     "workspace it acts in. Find the workspace id in the Anthropic Console\n"
     "under Settings -> Workspaces (it looks like 'wrkspc_...'), then either\n"
     "  export ANTHROPIC_WORKSPACE_ID=wrkspc_...\n"
     "or pass --workspace wrkspc_... and run the same command again."),
    ("credit balance",
     "The workspace is out of credit. Top it up in the Anthropic Console\n"
     "under Settings -> Billing, then run the same command again."),
    ("model:",
     "The API does not know that model. Check --model against the model ids\n"
     "in the Anthropic Console, or drop the flag to use the default."),
]


def explain_api_error(error):
    """Turn a run-stopping API error into something actionable."""
    message = str(error)
    lines = [f"the Claude API rejected every page: {message}", ""]
    for marker, hint in API_ERROR_HINTS:
        if marker in message:
            lines.append(hint)
            break
    else:
        lines.append("Nothing was transcribed. Fix the above and run the same\n"
                     "command again — cached pages are not re-billed.")
    return "\n".join(lines)


def cache_key(scan_path, kind, model, effort, tiles, version=None):
    with open(scan_path, "rb") as handle:
        digest = hashlib.sha1(handle.read()).hexdigest()
    version = PROMPT_VERSION if version is None else version
    parts = f"{digest}|{kind}|{model}|{effort}|{tiles}|{version}"
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:16]


def find_cached(cache_dir, scan, kind, model, effort, tiles, older_ok):
    """Locate a cached reading of this scan; return (path, prompt_version).

    Every rule that can be applied to a reading after the fact — how 'zp. št.'
    is numbered, which alt surnames are inventions, where a widow's maiden name
    belongs — is applied when the workbook is written, not when the scan is
    read. A reading taken under an older prompt is therefore still worth having,
    and re-reading a whole book to pick up a change of that kind is money spent
    for nothing. With older_ok the newest available reading is reused.
    """
    if not os.path.exists(scan):
        return None, None
    newest = PROMPT_VERSION
    for version in range(newest, 0, -1):
        key = cache_key(scan, kind, model, effort, tiles, version)
        path = os.path.join(cache_dir, key + ".json")
        if os.path.exists(path):
            return path, version
        if not older_ok:
            return None, None
    return None, None


# --------------------------------------------------------------------------
# Spreadsheet
# --------------------------------------------------------------------------

def flagged_columns(kind, row):
    """Which columns the model said it was unsure of, named as the sheet names them.

    A field it flagged but then left empty is not worth a reviewer's time, so
    only fields that actually carry a reading are listed.
    """
    headers = dict(COLUMNS[kind])
    seen = []
    for field in row.get("uncertain") or []:
        if not row.get(field):
            continue
        label = headers.get(field, field)
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


# The bride's half of a note, as the marriage prompt lays it out. 'Nevesta
# vdova ...' also turns up as a clause before 'Ženin:', so both are matched.
_NEVESTA_RE = re.compile(
    r"Nevesta:(?P<seg>.*?)(?=(?:Ženin|Nevesta|Priči|Nadva|Opomba):|$)", re.S)
_NEVESTA_LEAD_RE = re.compile(
    r"Nevesta\s+vdova\b.*?(?=(?:Ženin|Nevesta|Priči|Opomba):|$)", re.S | re.I)
_WIDOW_RE = re.compile(r"\bvdova\b", re.I)
# 'starša Marko Vardijan, kmet, in Rozalija Vanjaš' -> Vardijan. The father is
# named first; his surname is the bride's maiden one.
_PARENTS_RE = re.compile(r"\bstarša\b(?P<rest>.*)|\boče\b(?P<alt>.*)")
_DECEASED_RE = re.compile(r"^(?:[†+]\s*|pok\.\s*)+")


def _bride_note(notes):
    """Return the part of the note that speaks about the bride."""
    if not notes:
        return ""
    parts = [m.group("seg") for m in _NEVESTA_RE.finditer(notes)]
    lead = _NEVESTA_LEAD_RE.search(notes)
    if lead:
        parts.append(lead.group(0))
    return " ".join(parts)


def _is_widow_bride(notes):
    """True when the note says the BRIDE was a widow.

    'vdova' also shows up describing her mother ('in Barbara vdova Šterk roj.
    Plut') or a witness ('Marija Skubic, meščanka, vdova'), and neither makes
    the bride one — so the word has to open her own description or follow the
    'Nevesta' label directly.
    """
    bride = _bride_note(notes)
    if not _WIDOW_RE.search(bride):
        return False
    if _NEVESTA_LEAD_RE.search(notes or ""):
        return True
    # Her own clauses come first; anything after 'in <Name> vdova' is a parent.
    head = re.split(r"\bin\s+\w+\s+vdova\b", bride, maxsplit=1)[0]
    if not _WIDOW_RE.search(head):
        return False
    # A trailing 'vdova' hanging off a witness or an occupation is not hers.
    return bool(re.match(r"^[^;]*\bvdova\b", bride.strip(), re.I))


def _father_surname(bride_note):
    """The bride's maiden surname, as her father's surname in the note."""
    m = _PARENTS_RE.search(bride_note)
    if not m:
        return ""
    text = (m.group("rest") or m.group("alt") or "").strip()
    text = text.split(",")[0]
    text = re.split(r"\s+in\s+", text)[0]
    text = _DECEASED_RE.sub("", text).strip(" .")
    parts = text.split()
    return parts[-1] if len(parts) >= 2 else ""


# How the register names the dead husband: 'po možu Modic', 'vdova po Požeku',
# '(po Milavcu)', 'prej por. Straus', 'v knjigi pisana Gregorič'.
_HUSBAND_RES = [re.compile(p) for p in (
    r"po prvem možu\s+([^\s,;.)]+)",
    r"po možu\s+([^\s,;.)]+)",
    r"vdova po\s+†?\s*(?:[A-ZŠČŽ][^\s,;.]*\s+)?([^\s,;.)]+)",
    r"\(po\s+([^\s,;.)]+)\)",
    r"\bpo\s+([A-ZŠČŽÖÜ][^\s,;.)]*)",
    r"prej por\.\s+([^\s,;.)]+)",
    r"prej poročena\s+([^\s,;.)]+)",
    r"\(prej\s+(?:por\.\s+)?([^\s,;.)]+)",
    r"\bpor\.\s+([^\s,;.)]+)",
    r"v knjigi pisana\s+([^\s,;.(]+)",
    r"vpisana kot\s+([^\s,;.)]+)",
    r"\bprej\s+([A-ZŠČŽÖÜ][^\s,;.)]*)",
    r"vdova\s+([A-ZŠČŽÖÜ][^\s,;.)]*)",
)]
_NOT_A_SURNAME = {"po", "let", "in", "kot", "prvem", "možu", "r", "roj", "rojena"}
_NAME_RE = re.compile(r"\b[A-ZŠČŽÖÜÄ][\wšžčćđäöüßA-Za-z]{2,}\b")


def surname_vocabulary(rows):
    """Count every surname-shaped word in the book, to settle inflections.

    Slovenian puts the dead husband in the locative — 'vdova po Tomcu' — and
    undoing that is ambiguous on its own: '-cu' is either '-c' or '-ec'. The
    book's own spelling of its families decides it, so Tomcu resolves to Tomc
    where the register writes Tomc 181 times, while Kostelcu resolves to
    Kostelec.
    """
    vocab = collections.Counter()
    for row in rows:
        for field in ("groom_surname", "groom_alt_surname",
                      "bride_surname", "bride_alt_surname",
                      "father_surname", "mother_surname"):
            value = (row.get(field) or "").strip()
            if value:
                vocab[value] += 1
        for word in _NAME_RE.findall(row.get("notes") or ""):
            vocab[word] += 1
    return vocab


def nominative(word, vocab):
    """Undo the locative: Požeku -> Požek, Strucelju -> Strucelj, Jakši -> Jakša."""
    w = word.strip(" .,;:()")
    if not w:
        return ""
    if vocab.get(w, 0) >= 3:
        return w                                  # already the plain form
    cands = []
    if w.endswith("ju"):
        # Only an l/n stem keeps the j: Strucelju -> Strucelj, Kukarju -> Kukar.
        stem = w[:-2]
        cands += [stem + "j", stem] if stem.endswith(("l", "n")) else [stem, stem + "j"]
    elif w.endswith("cu"):
        cands += [w[:-2] + "ec", w[:-1]]
    elif w.endswith("u"):
        cands.append(w[:-1])
    elif w.endswith("i"):
        cands.append(w[:-1] + "a")
    elif w.endswith(("em", "om")):
        cands.append(w[:-2])
    cands = [c for c in cands if c]
    if not cands:
        return w
    ranked = sorted(enumerate(cands), key=lambda t: (-vocab.get(t[1], 0), t[0]))
    return ranked[0][1]


def _married_surname(bride_note, vocab):
    """The surname the widow carried at this wedding, as the note gives it."""
    for pattern in _HUSBAND_RES:
        m = pattern.search(bride_note)
        if not m:
            continue
        raw = m.group(1).strip(" .,;:()")
        if not raw or raw[0].islower() or raw.casefold() in _NOT_A_SURNAME:
            continue
        return nominative(raw, vocab)
    return ""


def apply_widow_rule(kind, row, vocab=None):
    """File a widow bride under her maiden surname, married surname in alt.

    The register enters a remarrying widow under the name she carries that
    day — her first husband's — so a reading that follows the page puts the
    wrong surname in 'bride_surname'. Her maiden name is recoverable from her
    parents, whom the same entry names. The model is asked to do this itself;
    this catches the entries where it did not, and flags every one it touches
    so the correction gets a human's eye.
    """
    if kind != "P":
        return row
    notes = row.get("notes") or ""
    if not _is_widow_bride(notes):
        return row
    bride = _bride_note(notes)
    maiden = _father_surname(bride)
    surname = (row.get("bride_surname") or "").strip()
    if not maiden or not surname:
        return row
    if fold_letters(maiden) == fold_letters(surname):
        # Filed under her maiden name already; the register may still name the
        # husband she is the widow of, and that belongs in the alt column.
        married = _married_surname(bride, vocab if vocab is not None else {})
        alt = (row.get("bride_alt_surname") or "").strip()
        if not married:
            # 'Kralj p. Gorše' is the register annotating a maiden name onto a
            # married one; when the second half is her father's surname, the
            # first half is the husband's.
            m = re.match(r"^(\S+)\s+p\.\s+(\S+)$", alt)
            if m and fold_letters(m.group(2)) == fold_letters(surname):
                married = m.group(1)
        if not married or fold_letters(married) == fold_letters(surname):
            return row
        row["bride_alt_surname"] = married
        if alt and alt != married:
            note = (row.get("notes") or "").strip()
            row["notes"] = f"{note}; v knjigi {alt}" if note else f"v knjigi {alt}"
        row["uncertain"] = list(row.get("uncertain") or []) + ["bride_alt_surname"]
        return row
    row["bride_surname"] = maiden
    displaced = (row.get("bride_alt_surname") or "").strip()
    row["bride_alt_surname"] = surname
    if displaced and displaced != surname:
        # The alt column held the book's spelling; the married surname takes
        # its place, so keep the spelling where it is still readable. Compared
        # exactly, not folded: 'Strucelj' for 'Štrucelj' is precisely the kind
        # of difference the alt column exists to record.
        note = (row.get("notes") or "").strip()
        row["notes"] = f"{note}; v knjigi {displaced}" if note else f"v knjigi {displaced}"
    row["uncertain"] = list(row.get("uncertain") or []) + ["bride_surname"]
    return row


ALT_PAIRS = [
    ("father_surname", "father_alt_surname"),
    ("mother_surname", "mother_alt_surname"),
    ("groom_surname", "groom_alt_surname"),
    ("bride_surname", "bride_alt_surname"),
]


# Letters that are not Slovenian but that a model reaches for when it
# 'restores' a South-Slavic surname it was never asked to restore. Folding
# them onto their Slovenian counterparts makes 'Jakofčić' compare equal to
# 'Jakofčič', which is how that pair reaches us: not as a reading of the book
# but as an invented variant of the reading.
FOREIGN_LETTERS = str.maketrans({"ć": "č", "Ć": "Č", "đ": "d", "Đ": "D",
                                 "ś": "š", "Ś": "Š", "ě": "e", "ř": "r"})


def fold_letters(value):
    """Casefold and strip diacritics, for comparing two spellings of a name."""
    lowered = (value or "").casefold().translate(FOREIGN_LETTERS)
    return "".join(c for c in unicodedata.normalize("NFD", lowered)
                   if unicodedata.category(c) != "Mn")


def drop_redundant_alts(row):
    """Clear an alt surname that only repeats the modern one.

    The alt column exists to preserve the book's own spelling, so it earns its
    place only when that spelling actually differs. An alt that matches the
    modern surname — or matches it once non-Slovenian letters are folded away
    — carries nothing, and would put a meaningless 'alt_surname' on the person
    in the JSON output.
    """
    for surname_field, alt_field in ALT_PAIRS:
        alt = (row.get(alt_field) or "").strip()
        surname = (row.get(surname_field) or "").strip()
        if not alt:
            continue
        if alt.casefold() == surname.casefold():
            row[alt_field] = ""
        elif (alt.translate(FOREIGN_LETTERS).casefold()
              == surname.translate(FOREIGN_LETTERS).casefold()):
            row[alt_field] = ""
    return row


def build_rows(kind, results, parish, book_url, interpret, add_flags):
    """Flatten per-page readings into sheet rows, in page order."""
    columns = columns_for(kind, add_flags)
    vocab = surname_vocabulary(
        [r for _page, payload in results for r in payload.get("rows", [])])
    rows = []
    for page, payload in results:
        url = page_url(book_url, page)
        for position, raw in enumerate(payload.get("rows", []), start=1):
            raw = apply_widow_rule(kind, drop_redundant_alts(dict(raw)), vocab)
            values = []
            for field, _header in columns:
                if field == "seq":
                    values.append(position)
                elif field == "parish":
                    values.append(parish)
                elif field == "url":
                    values.append(url)
                elif field == "interpret":
                    values.append(interpret)
                elif field == "notes":
                    values.append((raw.get("notes") or "").strip() or None)
                elif field == "flags":
                    values.append(flagged_columns(kind, raw) or None)
                else:
                    values.append((raw.get(field) or "").strip() or None)
            rows.append(values)
    return rows


def write_workbook(path, kind, rows, sheet_title, add_flags=False):
    columns = columns_for(kind, add_flags)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_title[:31]
    sheet.append([header for _field, header in columns])
    for row in rows:
        sheet.append(row)

    bold = openpyxl.styles.Font(bold=True)
    for cell in sheet[1]:
        cell.font = bold
    sheet.freeze_panes = "A2"
    for index, (_field, header) in enumerate(columns, start=1):
        letter = openpyxl.utils.get_column_letter(index)
        widest = max([len(header)] + [
            len(str(row[index - 1])) for row in rows if row[index - 1]
        ])
        sheet.column_dimensions[letter].width = min(max(widest + 2, 10), 45)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    workbook.save(path)


def safe_filename(name):
    """Keep the human-readable book name, drop what a filesystem dislikes."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "-", name).strip()
    return re.sub(r"\s+", " ", cleaned)


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Transcribe a Matricula Online book into an index .xlsx",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  python tools/matricula_transcribe.py \\\n"
            "      'https://data.matricula-online.eu/sl/slovenia/ljubljana/podzemelj/04455/?pg=1' \\\n"
            "      --interpret Renko_Luka --output-dir data/matricula/Renko --pages 6-20\n"
        ),
    )
    parser.add_argument("url", help="book URL on data.matricula-online.eu")
    parser.add_argument("--interpret", required=True,
                        help="value for the 'interpret' column, e.g. Priimek_Ime")
    parser.add_argument("--output-dir", required=True,
                        help="directory the .xlsx is written to")
    parser.add_argument("--output", help="output filename (default: from the book name)")
    parser.add_argument("--pages", default="",
                        help="pages to transcribe, e.g. '6-40' or '6,8,10-12' (default: all)")
    parser.add_argument("--parish", help="override the parish name written into every row")
    parser.add_argument("--workspace", default=os.environ.get("ANTHROPIC_WORKSPACE_ID", ""),
                        help="workspace the requests act in (default: "
                             "$ANTHROPIC_WORKSPACE_ID). Required for an "
                             "identity-linked API key")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    parser.add_argument("--effort", default=DEFAULT_EFFORT,
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help=f"reasoning effort (default: {DEFAULT_EFFORT})")
    parser.add_argument("--max-tokens", type=int, default=16000,
                        help="output cap per page (default: 16000)")
    parser.add_argument("--tiles", type=int, default=2,
                        help="vertical strips sent alongside the full page (default: 2, "
                             "one per book page; 1 disables strips)")
    parser.add_argument("--workers", type=int, default=4,
                        help="pages transcribed in parallel (default: 4)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between scan downloads (default: 1.0)")
    parser.add_argument("--image-dir", default=IMAGE_CACHE,
                        help=f"scan cache directory (default: {IMAGE_CACHE})")
    parser.add_argument("--cache-dir", default=TEXT_CACHE,
                        help=f"transcription cache directory (default: {TEXT_CACHE})")
    parser.add_argument("--reuse-cached", action="store_true",
                        help="reuse readings taken under an older prompt instead "
                             "of paying to read those pages again")
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the .xlsx from cached readings only — no "
                             "API calls, no downloads (implies --reuse-cached)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-transcribe pages even when a cached reading exists")
    parser.add_argument("--no-flags", action="store_true",
                        help="leave out the 'preveri' column that names the fields "
                             "the model was unsure of")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the book, the pages and the output path, then stop")
    args = parser.parse_args()

    if args.tiles < 1:
        raise SystemExit("--tiles must be at least 1")

    book_url = normalize_book_url(args.url)
    print(f"book page: {book_url}")
    title, images = parse_book_page(http_get(book_url).decode("utf-8", "replace"))

    stem = book_stem(images[0] if images else "")
    stem_kind, stem_parish, years = parse_stem(stem)
    kind = detect_kind(title, images[0] if images else "")

    if kind is None:
        raise SystemExit(f"cannot tell what kind of book this is from: {title!r}")
    if kind == "M":
        raise SystemExit(
            "this is a death book (mrliška knjiga). matricula_to_json.py has no "
            "death column layout yet, so there is nothing useful to write — "
            "support for those needs to be added there first."
        )
    if stem_kind and stem_kind != kind:
        print(f"note: title says {kind}, scan folder says {stem_kind}; using {kind}")

    parish = args.parish or stem_parish or parish_from_url(book_url)
    span = lookup_book_span(book_url)
    if span and span != years:
        if years:
            print(f"note: parish list says {span}, scan folder says {years}; "
                  f"using {span}")
        years = span
    pages = parse_pages(args.pages, len(images))

    name = f"Indeks {kind} {parish}"
    if years:
        name += f" - {years}"
    filename = args.output or safe_filename(name)
    if not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"
    out_path = os.path.join(args.output_dir, filename)

    print(f"title:  {title}")
    print(f"book:   {stem or name} ({'births' if kind == 'K' else 'marriages'})")
    print(f"parish: {parish}")
    print(f"pages:  {len(pages)} of {len(images)}"
          f"{'' if not args.pages else f' (--pages {args.pages})'}")
    print(f"output: {out_path}")
    if args.dry_run:
        return 0

    if args.rebuild and args.refresh:
        raise SystemExit("--rebuild and --refresh ask for opposite things")

    scan_dir = os.path.join(args.image_dir, signature_from_url(book_url))
    fetched = 0
    for page in pages:
        if args.rebuild:
            break
        dest = os.path.join(scan_dir, f"{page:04d}.jpg")
        try:
            if download_scan(images[page - 1], dest, book_url,
                             args.delay if page != pages[-1] else 0):
                fetched += 1
        except urllib.error.URLError as error:
            raise SystemExit(f"cannot fetch scan for page {page}: {error}")
    if not args.rebuild:
        print(f"scans:  {fetched} downloaded, {len(pages) - fetched} already cached")

    import anthropic

    # An identity-linked API key has no workspace of its own, so the API
    # refuses every request that does not name the workspace it acts in.
    # The SDK has no parameter for it — it travels as a header.
    headers = {}
    if args.workspace:
        headers["anthropic-workspace-id"] = args.workspace

    try:
        client = anthropic.Anthropic(default_headers=headers or None)
    except anthropic.AnthropicError as error:
        raise SystemExit(f"cannot reach the Claude API: {error}")

    os.makedirs(args.cache_dir, exist_ok=True)
    older_ok = args.reuse_cached or args.rebuild
    results = {}
    pending = []
    absent = []
    stale = 0
    for page in pages:
        scan = os.path.join(scan_dir, f"{page:04d}.jpg")
        found, version = (None, None) if args.refresh else find_cached(
            args.cache_dir, scan, kind, args.model, args.effort, args.tiles,
            older_ok)
        if found:
            with open(found, encoding="utf-8") as handle:
                results[page] = json.load(handle)
            if version != PROMPT_VERSION:
                stale += 1
        elif not os.path.exists(scan):
            absent.append(page)          # never downloaded; nothing to rebuild from
        else:
            key = cache_key(scan, kind, args.model, args.effort, args.tiles)
            pending.append((page, scan, os.path.join(args.cache_dir, f"{key}.json")))

    print(f"pages:  {len(results)} read from cache, {len(pending)} to transcribe")
    if stale:
        print(f"        {stale} of those were read under an older prompt "
              f"(current is v{PROMPT_VERSION}); every rule that can be applied "
              f"afterwards still is")
    if absent:
        shown = ", ".join(str(p) for p in absent[:10])
        more = "" if len(absent) <= 10 else f" and {len(absent) - 10} more"
        print(f"        {len(absent)} page(s) have no scan cached locally and are "
              f"left out: {shown}{more}")
    if args.rebuild and pending:
        missing = ", ".join(str(p) for p, _s, _c in pending[:10])
        more = "" if len(pending) <= 10 else f" and {len(pending) - 10} more"
        raise SystemExit(
            f"--rebuild makes no API calls, but {len(pending)} page(s) have no "
            f"cached reading: {missing}{more}.\n"
            f"Drop --rebuild to transcribe them, or narrow --pages."
        )

    spent = [0, 0]
    failures = []
    # A rejected key, a missing workspace or an unknown model fails every page
    # in exactly the same way; there is no point paying for the rest of the
    # book to watch it happen 200 times.
    fatal = threading.Event()
    fatal_error = []
    stopped = []

    def work(item):
        page, scan, cached = item
        if fatal.is_set():
            raise RuntimeError("skipped after an error that stops the whole run")
        try:
            payload, usage = transcribe_page(
                client, kind, tile_scan(scan, args.tiles),
                args.model, args.effort, args.max_tokens,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, anthropic.BadRequestError) as error:
            if not fatal.is_set():
                fatal.set()
                fatal_error.append(error)
            raise
        with open(cached, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        return page, payload, usage

    if pending:
        workers = max(1, min(args.workers, len(pending)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, item): item for item in pending}
            done = 0
            for future in concurrent.futures.as_completed(futures):
                page = futures[future][0]
                done += 1
                try:
                    page, payload, usage = future.result()
                except Exception as error:  # one bad page must not lose the rest
                    if fatal.is_set():
                        # Reported once, in full, after the pool drains.
                        stopped.append(page)
                        continue
                    failures.append((page, error))
                    print(f"  [{done}/{len(pending)}] page {page}: FAILED — {error}")
                    continue
                results[page] = payload
                spent[0] += usage[0]
                spent[1] += usage[1]
                count = len(payload.get("rows", []))
                note = payload.get("page_note") or ""
                summary = f"{count} entries" if count else (note or "no entries")
                print(f"  [{done}/{len(pending)}] page {page}: {summary}")

    if fatal_error:
        if stopped:
            print(f"  stopped after the first error: "
                  f"{len(stopped)} page(s) abandoned, none billed for")
        raise SystemExit("\n" + explain_api_error(fatal_error[0]))

    ordered = [(page, results[page]) for page in pages if page in results]
    rows = build_rows(kind, ordered, parish, book_url, args.interpret,
                      not args.no_flags)
    if not rows:
        print("no entries transcribed — nothing written")
        return 1

    sheet_title = f"{kind} {years}" if years else kind
    write_workbook(out_path, kind, rows, sheet_title, add_flags=not args.no_flags)

    columns = columns_for(kind, not args.no_flags)
    fields = [field for field, _header in columns]
    flagged = (sum(1 for row in rows if row[fields.index("flags")])
               if "flags" in fields else 0)
    print(f"\nwrote {len(rows)} entries from {len(ordered)} pages to {out_path}")
    if flagged:
        print(f"{flagged} entries have something named in the 'preveri' column")
    if spent[0] or spent[1]:
        print(f"tokens: {spent[0]:,} in, {spent[1]:,} out")
    if failures:
        print(f"\n{len(failures)} page(s) failed and are missing from the sheet:")
        for page, error in sorted(failures):
            print(f"  page {page}: {error}")
        print("re-run the same command to retry them; cached pages are not re-billed")
        return 1
    print("every row is a machine reading — check it against the scan before publishing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
