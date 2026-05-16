# scripts/gedcom-to-json.py

import argparse
import locale
import os
import sys
import json
import re
import time
import urllib.request
import unicodedata
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from gedcom.parser import Parser

# --- Configuration ---
# Define paths relative to the project root.
# This script should be run from the root of the project directory.
INPUT_DIR = "data/filtered"
OUTPUT_DIR = "data/output"
CACHE_FILE = ".gedcom-to-json.cache"
CONTRIBUTORS_FILE = "data/contributors.json"


def _load_contributor_urls():
    """Load public URLs from contributors.json. Returns dict of contributor_id -> url."""
    try:
        with open(CONTRIBUTORS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {name: info.get("url") for name, info in data.items() if info.get("url")}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _split_name_value(name_val):
    if "/" in name_val:
        parts = name_val.split("/")
        return parts[0].strip(), parts[1].strip()
    return name_val.strip(), ""


def _surn_child(name_element):
    for sub in name_element.get_child_elements():
        if sub.get_tag() == "SURN":
            return (sub.get_value() or "").strip()
    return ""


def _name_type(name_element):
    for sub in name_element.get_child_elements():
        if sub.get_tag() == "TYPE":
            return (sub.get_value() or "").strip().lower()
    return ""


# Sub-tags that supply an alternate surname inline under a NAME block.
# `_MARNM` is the MyHeritage / Family Tree Maker convention for "married name".
# `_FORMERNAME` is used for a previous (often pre-marriage) surname.
_ALT_SURNAME_SUBTAGS = ("_MARNM", "_FORMERNAME")


def _inline_alt_surnames(name_element):
    """Yield alternate surnames declared as direct sub-tags of a NAME block."""
    for sub in name_element.get_child_elements():
        if sub.get_tag() in _ALT_SURNAME_SUBTAGS:
            val = (sub.get_value() or "").strip()
            if val:
                yield val


def get_name_surname(individual):
    """Extract (given, surname, alt_surnames) from an individual.

    The primary NAME (no TYPE, or TYPE 'birth') supplies given+surname. Alt
    surnames are collected from three conventions:
      - secondary NAME blocks with `2 TYPE married` (their `2 SURN` or /…/)
      - `2 _MARNM <surname>` sub-tags (MyHeritage / Family Tree Maker)
      - `2 _FORMERNAME <surname>` sub-tags (former / previous surname)
    Multiple distinct values are joined with ", ".

    If a person has ONLY a married NAME (no birth/untyped NAME), the first
    one is used as the primary instead; remaining alts (if any) still go
    into the comma-joined list.
    """
    primary_name, primary_surname = "", ""
    primary_found = False
    married_names = []  # (given, surname) pairs from TYPE=married NAMEs
    inline_alts = []   # surnames from _MARNM / _FORMERNAME sub-tags

    for child in individual.get_child_elements():
        if child.get_tag() != "NAME":
            continue
        first, last = _split_name_value(child.get_value() or "")
        if _name_type(child) == "married":
            surn = _surn_child(child) or last
            if surn:
                married_names.append((first, surn))
        elif not primary_found:
            primary_name, primary_surname = first, last
            primary_found = True
        for alt in _inline_alt_surnames(child):
            inline_alts.append(alt)

    if not primary_found and married_names:
        primary_name, primary_surname = married_names.pop(0)

    seen = set()
    if primary_surname:
        seen.add(primary_surname)
    deduped = []
    for _, surn in married_names:
        if surn not in seen:
            deduped.append(surn)
            seen.add(surn)
    for surn in inline_alts:
        if surn not in seen:
            deduped.append(surn)
            seen.add(surn)

    return primary_name, primary_surname, ", ".join(deduped)


def get_sex(individual):
    """Return "m", "f", or "" based on the SEX tag of an individual."""
    for child in individual.get_child_elements():
        if child.get_tag() == "SEX":
            val = (child.get_value() or "").strip().upper()
            if val.startswith("M"):
                return "m"
            if val.startswith("F"):
                return "f"
            return ""
    return ""


MATRICULA_RE = re.compile(
    r"https?://data\.matricula-online\.eu(?:/[^/\"\s<]+){5,}[^\"\s<]*"
)
GENEANET_CEMETERY_RE = re.compile(
    r"https?://[a-z]{2}\.geneanet\.org/(?:cemetery|friedhof)[^\"\s<]*"
)
FINDAGRAVE_RE = re.compile(
    r"https?://(?:www\.)?findagrave\.com/(?:memorial/[^\"\s<]+|cgi-bin/fg\.cgi\?[^\"\s<]*page=gr[^\"\s<]*)"
)
BILLIONGRAVES_RE = re.compile(r"https?://(?:www\.)?billiongraves\.com/grave/[^\"\s<]+")
SISTORY_RE = re.compile(r"https?://(?:www\.)?sistory\.si/ww[12][^\"\s<]*")
SISTORY_CENSUS_RE = re.compile(
    r"https?://(?:www\.)?sistory\.si/[^\"\s<]*popisi[^\"\s<]*"
)
FAMILYSEARCH_RE = re.compile(r"https?://(?:www\.)?familysearch\.org/ark:/[^\"\s<]+")
DLIB_RE = re.compile(r"https?://(?:www\.)?dlib\.si/[^\"\s<]+")


_MATRICULA_LANG_RE = re.compile(r"(https://data\.matricula-online\.eu/)[a-z]{2}(/)")


def _normalize_matricula_url(url):
    """Normalize matricula URL: upgrade http to https only. Language code is
    preserved as-is; deduplication across language variants is handled by _dedup_links.
    """
    return url.replace("http://", "https://")


def _link_sort_key(url):
    if MATRICULA_RE.search(url):
        return 1
    if FAMILYSEARCH_RE.search(url):
        return 2
    if SISTORY_CENSUS_RE.search(url):
        return 3
    if SISTORY_RE.search(url):
        return 4
    if GENEANET_CEMETERY_RE.search(url):
        return 5
    if FINDAGRAVE_RE.search(url) or BILLIONGRAVES_RE.search(url):
        return 6
    return 7


def _dedup_links(links):
    """Deduplicate and sort links. Matricula URLs differing only in language code
    (e.g. /en/ vs /sl/) are treated as the same link; the last occurrence is kept
    so that explicitly written URLs (from NOTEs) override template-derived ones.
    Links are sorted by category: Matricula, FamilySearch, SiStory Census, SiStory, Geneanet Cemetery, Other Cemeteries, Others.
    """
    seen = {}
    for url in links:
        key = _MATRICULA_LANG_RE.sub(r"\1*\2", url)
        seen[key] = url  # last wins
    return sorted(seen.values(), key=_link_sort_key)


def _find_matricula_url(text):
    """Return the first matricula-online.eu URL found in text, or empty string."""
    if not text:
        return ""
    m = MATRICULA_RE.search(text)
    return _normalize_matricula_url(m.group().rstrip(".,;)")) if m else ""


def _find_cemetery_url(text):
    """Return the first cemetery or Sistory URL found in text, or empty string."""
    if not text:
        return ""
    for pattern in (GENEANET_CEMETERY_RE, FINDAGRAVE_RE, BILLIONGRAVES_RE, SISTORY_RE):
        m = pattern.search(text)
        if m:
            return m.group().rstrip(".,;)")
    return ""


def _find_census_url(text):
    """Return the first Sistory census URL found in text, or empty string."""
    if not text:
        return ""
    m = SISTORY_CENSUS_RE.search(text)
    return m.group().rstrip(".,;)") if m else ""


def _find_familysearch_url(text):
    """Return the first FamilySearch ark URL found in text, or empty string."""
    if not text:
        return ""
    m = FAMILYSEARCH_RE.search(text)
    return m.group().rstrip(".,;)") if m else ""


def _find_all_links(text):
    """Return list of all known link URLs found in text (matricula + cemetery + sistory)."""
    if not text:
        return []
    links = []
    for m in MATRICULA_RE.finditer(text):
        url = _normalize_matricula_url(m.group().rstrip(".,;)"))
        if url not in links:
            links.append(url)
    for pattern in (
        GENEANET_CEMETERY_RE,
        FINDAGRAVE_RE,
        BILLIONGRAVES_RE,
        SISTORY_RE,
        SISTORY_CENSUS_RE,
        FAMILYSEARCH_RE,
        DLIB_RE,
    ):
        for m in pattern.finditer(text):
            url = m.group().rstrip(".,;)")
            if url not in links:
                links.append(url)
    return links


_PAGE_RE = re.compile(r"\?pg=\d+")


def _apply_page(url_template, page):
    """Replace or append the ?pg= parameter in a matricula URL."""
    if _PAGE_RE.search(url_template):
        return _PAGE_RE.sub(f"?pg={page}", url_template)
    return url_template


def _full_text(element):
    """Return the complete text of a GEDCOM element by joining CONT/CONC children.

    CONC children are appended with no separator (direct concatenation).
    CONT children are appended with a space separator (for URL-matching purposes).
    When CONT/CONC children are present, only the first line of get_value() is
    used as the base to avoid double-processing content that python-gedcom may
    have already folded into get_value() with incorrect \\n separators.
    """
    val = element.get_value() or ""
    cont_conc = [
        c for c in element.get_child_elements() if c.get_tag() in ("CONT", "CONC")
    ]
    if not cont_conc:
        return val
    parts = [val.split("\n")[0]]
    for child in cont_conc:
        if child.get_tag() == "CONC":
            parts.append(child.get_value() or "")
        else:
            parts.append(" " + (child.get_value() or ""))
    return "".join(parts)


def _link_from_subelement(element, sources_dict):
    """
    Extract all known URLs (matricula, cemetery) from a GEDCOM sub-element.
    Returns a deduplicated list of URLs.

    Patterns covered:
      P1  NOTE value (plain) or NOTE+CONT children (HTML-wrapped)
      P2  NOTE with plain URL — same tag path as P1
      P3  SOUR > PAGE
      P4  SOUR > DATA > TEXT
      P5  SOUR @ref@ resolved via sources_dict (plain URL)
      P7  SOUR @ref@ + PAGE N resolved via FILN/OBJE template in sources_dict
    """
    tag = element.get_tag()
    val = element.get_value() or ""

    if tag == "NOTE":
        return _find_all_links(_full_text(element))

    if tag == "SOUR":
        # P8: URL stored directly as SOUR value (ODAR.GED pattern: "2 SOUR https://...")
        urls = _find_all_links(val)
        if urls:
            return urls
        # P5/P7: reference pointer @Sxxx@
        if val.startswith("@") and val.endswith("@"):
            template = sources_dict.get(val, "")
            if template:
                # P7: if a PAGE child exists, resolve via page→URL map or template fallback
                if isinstance(template, dict):
                    page_map = template["pages"]
                    tmpl = template["template"]
                    for sour_child in element.get_child_elements():
                        if sour_child.get_tag() == "PAGE":
                            page_val = (sour_child.get_value() or "").strip()
                            m = re.search(r"\d+", page_val)
                            if m:
                                pg = m.group()
                                if pg in page_map:
                                    return [page_map[pg]]
                                elif tmpl:
                                    return [_apply_page(tmpl, pg)]
                    return _find_all_links(tmpl) or [tmpl]
                # legacy string template (P5 plain URL or old-style template)
                for sour_child in element.get_child_elements():
                    if sour_child.get_tag() == "PAGE":
                        page_val = (sour_child.get_value() or "").strip()
                        m = re.search(r"\d+", page_val)
                        if m and _PAGE_RE.search(template):
                            return [_apply_page(template, m.group())]
                # P5: plain URL stored directly in sources_dict (no page substitution needed)
                return _find_all_links(template) or [template]
            # pointer not in sources_dict — fall through to check inline DATA > WWW/TEXT children
        # P3: inline SOUR > PAGE / P4: inline SOUR > DATA > TEXT/WWW
        urls = []
        for sour_child in element.get_child_elements():
            if sour_child.get_tag() == "PAGE":
                for url in _find_all_links(_full_text(sour_child)):
                    if url not in urls:
                        urls.append(url)
            elif sour_child.get_tag() == "DATA":
                for data_child in sour_child.get_child_elements():
                    if data_child.get_tag() in ("TEXT", "WWW"):
                        for url in _find_all_links(_full_text(data_child)):
                            if url not in urls:
                                urls.append(url)
        return urls

    return []


def _indi_level_link(element, sources_dict, obje_dict=None):
    """
    Extract all known URLs from NOTE, SOUR, or OBJE at the INDI (or FAM) level.
    Returns a deduplicated list.
    """
    if obje_dict is None:
        obje_dict = {}
    urls = []
    for child in element.get_child_elements():
        if child.get_tag() in ("NOTE", "SOUR"):
            for url in _link_from_subelement(child, sources_dict):
                if url not in urls:
                    urls.append(url)
        elif child.get_tag() == "OBJE":
            val = child.get_value() or ""
            if val.startswith("@") and val.endswith("@"):
                url = obje_dict.get(val, "")
                if url and url not in urls:
                    urls.append(url)
    return urls


_URL_CACHE = {}
_ERROR_CACHE = set()  # transient fetch failures (not persisted)
_BROKEN_CACHE = (
    set()
)  # persisted 404s — not re-fetched until "broken" entry removed from cache


def load_url_cache():
    global _URL_CACHE, _BROKEN_CACHE
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                raw_cache = json.load(f)
                for k, v in raw_cache.items():
                    if v == "broken":
                        _BROKEN_CACHE.add(k)
                        continue
                    # Drop legacy "unknown" sentinel — outdated format
                    if v == "unknown":
                        continue
                    _URL_CACHE[k] = v
        except Exception as e:
            print(f"Warning: Could not load URL cache: {e}", file=sys.stderr)
            _URL_CACHE = {}
            _BROKEN_CACHE = set()


def save_url_cache():
    try:
        combined = dict(_URL_CACHE)
        for url in _BROKEN_CACHE:
            combined[url] = "broken"
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(combined, f, indent=4)
    except Exception as e:
        print(f"Warning: Could not save URL cache: {e}", file=sys.stderr)


def _determine_link_type(url, context=None):
    if not url:
        return []

    # FamilySearch and dLib.si links cannot be reliably typed by page fetch.
    # Return [] so they are kept on whichever event they were placed in the GEDCOM.
    if FAMILYSEARCH_RE.search(url) or DLIB_RE.search(url):
        return []

    # Strip query parameters (like ?pg=N) to cache and fetch at the book level
    base_url = url.split("?")[0]

    # Fast path: detect type from Slovenian civil-registry abbreviations in the URL path.
    # MKR/MKK = Matična knjiga rojenih/krščenih (births), MKP = porok (marriage),
    # MKU = umrlih (deaths). These are standardised and reliable.
    _url_upper = base_url.upper()
    if "MKP" in _url_upper:
        return ["marriage"]
    if "MKU" in _url_upper:
        return ["death"]
    if "MKR" in _url_upper or "MKK" in _url_upper:
        return ["birth"]

    if base_url in _BROKEN_CACHE:
        return []

    if base_url in _URL_CACHE:
        val = _URL_CACHE[base_url]
        if isinstance(val, list):
            return val
        elif isinstance(val, str):
            return [val]

    if base_url in _ERROR_CACHE:
        return []

    for attempt in range(3):
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            req = urllib.request.Request(
                base_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
                html = response.read().decode("utf-8", errors="ignore").lower()

                # Extract headings and title to avoid matching sidebar navigation lists
                headings = re.findall(
                    r"<title>(.*?)</title>|<h\d[^>]*>(.*?)</h\d>", html, flags=re.DOTALL
                )
                text_to_search = " ".join([m[0] or m[1] for m in headings])

                types = []
                BIRTH_KW = [
                    "taufbuch",
                    "krstna knjiga",
                    "krsti",
                    "taufen",
                    "baptisms",
                    "baptismal register",
                ]
                DEATH_KW = [
                    "sterbebuch",
                    "mrliška knjiga",
                    "mrliči",
                    "sterbefälle",
                    "deaths",
                    "burial register",
                    "burials",
                ]
                MARRIAGE_KW = [
                    "trauungsbuch",
                    "poročna knjiga",
                    "poroke",
                    "trauungen",
                    "kopulationsbuch",
                    "marriages",
                    "marriage register",
                ]

                if any(kw in text_to_search for kw in BIRTH_KW):
                    types.append("birth")
                if any(kw in text_to_search for kw in DEATH_KW):
                    types.append("death")
                if any(kw in text_to_search for kw in MARRIAGE_KW):
                    types.append("marriage")

                # If headings didn't yield anything, try the whole HTML but strip <a> tags
                # to avoid the navigation menu containing all book types
                if not types:
                    clean_html = re.sub(
                        r"<a\s+[^>]*>.*?</a>", "", html, flags=re.DOTALL
                    )
                    if any(kw in clean_html for kw in BIRTH_KW):
                        types.append("birth")
                    if any(kw in clean_html for kw in DEATH_KW):
                        types.append("death")
                    if any(kw in clean_html for kw in MARRIAGE_KW):
                        types.append("marriage")

                _URL_CACHE[base_url] = types
                return types
        except urllib.error.HTTPError as e:
            if e.code == 404:
                ctx_str = f" (person: {context})" if context else ""
                print(
                    f"  [!] 404 Not Found (broken link) — cached: {base_url}{ctx_str}",
                    file=sys.stderr,
                )
                _BROKEN_CACHE.add(base_url)
                return []
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
            else:
                ctx_str = f" (person: {context})" if context else ""
                display_url = url if url != base_url else base_url
                print(
                    f"  [!] Failed to fetch {display_url}{ctx_str} after 3 attempts: {e}",
                    file=sys.stderr,
                )
                _ERROR_CACHE.add(base_url)
                return []
        except Exception as e:
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
            else:
                ctx_str = f" (person: {context})" if context else ""
                display_url = url if url != base_url else base_url
                print(
                    f"  [!] Failed to fetch {display_url}{ctx_str} after 3 attempts: {e}",
                    file=sys.stderr,
                )
                _ERROR_CACHE.add(base_url)
                return []

    _URL_CACHE[base_url] = []  # Fallback
    return []


def sanitize_links(links, expected_type, context=None):
    """
    Keeps links explicitly placed on an event (always preserved),
    and also queues them for cross-routing if the detected type differs.
    """
    sanitized = []
    misplaced = []
    for url in links:
        # Always keep the link where the user explicitly placed it
        if url not in sanitized:
            sanitized.append(url)

        if _find_cemetery_url(url):
            if expected_type != "death":
                misplaced.append((url, ["death"]))
            continue

        if _find_census_url(url):
            if expected_type != "birth":
                misplaced.append((url, ["birth"]))
            continue

        types = _determine_link_type(url, context=context)
        if types and expected_type not in types:
            # Also route to the detected type(s), but keep on the original event
            misplaced.append((url, types))
    return sanitized, misplaced


def _extract_indi_links(element, sources_dict, obje_dict=None, context=None):
    """
    Extract URLs from NOTE, SOUR, or OBJE at the INDI level.
    Cemetery URLs always go to death. Matricula URLs are routed by fetching
    the page HTML to determine record type (birth/death/marriage).
    Returns (b_links, d_links, m_links) as deduplicated lists.
    """
    if obje_dict is None:
        obje_dict = {}
    b_links, d_links, m_links = [], [], []

    def _route(url):
        if _find_cemetery_url(url):
            if url not in d_links:
                d_links.append(url)
        elif _find_census_url(url):
            if url not in b_links:
                b_links.append(url)
        else:
            types = _determine_link_type(url, context=context)
            if not types:
                if url not in b_links:
                    b_links.append(url)
            else:
                if "birth" in types and url not in b_links:
                    b_links.append(url)
                if "death" in types and url not in d_links:
                    d_links.append(url)
                if "marriage" in types and url not in m_links:
                    m_links.append(url)

    for child in element.get_child_elements():
        if child.get_tag() in ("NOTE", "SOUR"):
            for url in _link_from_subelement(child, sources_dict):
                _route(url)
        elif child.get_tag() == "OBJE":
            val = child.get_value() or ""
            if val.startswith("@") and val.endswith("@"):
                url = obje_dict.get(val, "")
                if url:
                    _route(url)
    return b_links, d_links, m_links


def build_obje_dict(root_elements):
    """
    Pre-build a mapping of OBJE pointer → URL from all root OBJE records.
    Covers matricula and cemetery links. Used to resolve OBJE @ref@ pointers on INDI/FAM records.
    """
    obje = {}
    for element in root_elements:
        if element.get_tag() != "OBJE":
            continue
        pointer = element.get_pointer()
        if not pointer:
            continue
        for child in element.get_child_elements():
            if child.get_tag() == "FILE":
                urls = _find_all_links(child.get_value() or "")
                if urls:
                    obje[pointer] = urls[0]
                    break
            elif child.get_tag() == "_ORIG":
                for orig_child in child.get_child_elements():
                    if orig_child.get_tag() == "_URL":
                        urls = _find_all_links(orig_child.get_value() or "")
                        if urls:
                            obje[pointer] = urls[0]
                            break
                if pointer in obje:
                    break
    return obje


def build_sources_dict(root_elements, obje_dict=None):
    """
    Pre-build a mapping of source pointer → matricula URL (or URL template) from
    all root SOUR records. Two patterns covered:
      P5  SOUR with TITL/ABBR containing a direct URL (MAUKO.GED, MODRIJAN.GED)
      P7  SOUR with FILN + OBJE children: store first OBJE URL as template for
          page substitution (RENKO.GED pattern — caller substitutes ?pg=N)
    """
    if obje_dict is None:
        obje_dict = {}
    sources = {}
    for element in root_elements:
        if element.get_tag() != "SOUR":
            continue
        pointer = element.get_pointer()
        if not pointer:
            continue
        # P5: direct URL in TITL or ABBR
        for child in element.get_child_elements():
            if child.get_tag() in ("TITL", "ABBR"):
                url = _find_matricula_url(child.get_value() or "")
                if url:
                    sources[pointer] = url
                    break
        if pointer in sources:
            continue
        # P7: FILN-based source — build a page→URL map from all OBJE children so that
        #     the exact URL (including language code) for each page can be resolved.
        #     Falls back to template substitution for pages not explicitly listed.
        has_filn = any(c.get_tag() == "FILN" for c in element.get_child_elements())
        if has_filn:
            page_map = {}
            fallback_template = ""
            for child in element.get_child_elements():
                if child.get_tag() == "OBJE":
                    obje_url = obje_dict.get(child.get_value() or "", "")
                    if obje_url and _PAGE_RE.search(obje_url):
                        pg = _PAGE_RE.search(obje_url).group()[4:]  # strip "?pg="
                        page_map[pg] = obje_url
                        if not fallback_template:
                            fallback_template = obje_url
            if page_map:
                sources[pointer] = {"pages": page_map, "template": fallback_template}
    return sources


def get_event_data(element, event_tag, sources_dict=None, obje_dict=None):
    """
    Extract date, place, and all links for an event (BIRT/MARR/DEAT/BURI).
    sources_dict must be pre-built with build_sources_dict() for SOUR @ref@ resolution.
    Returns (date, place, links_list).
    """
    if sources_dict is None:
        sources_dict = {}
    if obje_dict is None:
        obje_dict = {}
    for child in element.get_child_elements():
        if child.get_tag() != event_tag:
            continue
        date, place = "", ""
        # P6: URL stored directly as the event tag value (RENKO.GED pattern)
        links = _find_all_links(_full_text(child))
        for subchild in child.get_child_elements():
            if subchild.get_tag() == "DATE":
                date = subchild.get_value()
            elif subchild.get_tag() == "PLAC":
                place = subchild.get_value()
            elif subchild.get_tag() == "OBJE":
                val = subchild.get_value() or ""
                if val.startswith("@") and val.endswith("@"):
                    url = obje_dict.get(val, "")
                    if url and url not in links:
                        links.append(url)
            else:
                for url in _link_from_subelement(subchild, sources_dict):
                    if url not in links:
                        links.append(url)
        return date, place, links
    return "", "", []


def _get_all_event_links(element, event_tags, sources_dict, obje_dict):
    """Extract all links from all occurrences of the given event tag(s).
    Unlike get_event_data, handles multiple instances of the same tag (e.g. RESI, OCCU).
    """
    links = []
    for child in element.get_child_elements():
        if child.get_tag() not in event_tags:
            continue
        for url in _find_all_links(_full_text(child)):
            if url not in links:
                links.append(url)
        for subchild in child.get_child_elements():
            if subchild.get_tag() == "OBJE":
                val = subchild.get_value() or ""
                if val.startswith("@") and val.endswith("@"):
                    url = obje_dict.get(val, "")
                    if url and url not in links:
                        links.append(url)
            else:
                for url in _link_from_subelement(subchild, sources_dict):
                    if url not in links:
                        links.append(url)
    return links


def extract_year(date_str):
    """Returns the 4-digit year from a GEDCOM date string, or None if not found."""
    if not date_str:
        return None
    match = re.search(r"\b(\d{4})\b", date_str)
    return int(match.group(1)) if match else None


def needs_processing(input_path, *json_paths):
    """
    Returns True if the GED file should be processed in update mode:
    either JSON output is missing or older than the GED file.
    """
    ged_mtime = os.path.getmtime(input_path)
    for json_path in json_paths:
        if not os.path.exists(json_path):
            return True
        if os.path.getmtime(json_path) < ged_mtime:
            return True
    return False


def _process_one_file(filename, full_mode, contributor_urls, input_dir, output_dir):
    """Process a single GED file. Returns (metadata_entry_or_None, log_lines).

    All output is collected into log_lines instead of printed directly, so
    callers can print it without interleaving with other workers.
    """
    log = []

    contributor_id = unicodedata.normalize(
        "NFC",
        "-".join(
            part.lower().capitalize()
            for part in os.path.splitext(filename)[0].split("-")
        ),
    )
    input_path = os.path.join(input_dir, filename)
    persons_output_path = os.path.join(output_dir, f"{contributor_id}-persons.json")
    families_output_path = os.path.join(output_dir, f"{contributor_id}-families.json")

    # --- Update mode: skip if JSON is already up to date ---
    if not full_mode and not needs_processing(
        input_path, persons_output_path, families_output_path
    ):
        try:
            with open(persons_output_path, encoding="utf-8") as f:
                persons_data_skip = json.load(f)
            with open(families_output_path, encoding="utf-8") as f:
                families_data_skip = json.load(f)
            ged_mtime = datetime.fromtimestamp(os.path.getmtime(input_path)).isoformat()
            meta = {
                "contributor": contributor_id,
                "persons_count": len(persons_data_skip),
                "families_count": len(families_data_skip),
                "links_count": sum(1 for r in persons_data_skip if r.get("links"))
                + sum(1 for r in families_data_skip if r.get("links")),
                "filtered_count": 0,
                "skipped": True,
                "last_modified": ged_mtime,
                "url": contributor_urls.get(contributor_id),
            }
        except Exception:
            meta = None
        return meta, log

    print(f"Processing: {filename}", file=sys.stderr)

    # Initialize lists to hold extracted records for this file.
    persons_data = []
    families_data = []

    # --- Parsing ---
    # Input is expected to be valid UTF-8 (produced by gedcom-cleaner.py).
    # python-gedcom's parse_file opens the file as binary and decodes lines
    # as utf-8-sig, so no charset preprocessing is needed here.
    gedcom_parser = Parser()
    try:
        gedcom_parser.parse_file(input_path, strict=False)
    except Exception as e:
        print(
            f"  ERROR: Could not parse {filename}. Skipping file. Reason: {e}",
            file=sys.stderr,
        )
        return None, log

    individuals_dict = {}
    family_elements = []

    root_elements = list(gedcom_parser.get_root_child_elements())
    obje_dict = build_obje_dict(root_elements)
    sources_dict = build_sources_dict(root_elements, obje_dict)

    for element in root_elements:
        tag = element.get_tag()

        if tag == "INDI":
            pointer = element.get_pointer()
            name, surname, married_surname = get_name_surname(element)
            sex = get_sex(element)
            birth_date, birth_place, raw_birth_links = get_event_data(
                element, "BIRT", sources_dict, obje_dict
            )
            death_date, death_place, raw_death_links = get_event_data(
                element, "DEAT", sources_dict, obje_dict
            )
            baptism_date, baptism_place, _ = get_event_data(
                element, "BAPM", sources_dict, obje_dict
            )
            if not baptism_date and not baptism_place:
                baptism_date, baptism_place, _ = get_event_data(
                    element, "CHR", sources_dict, obje_dict
                )
            for url in _get_all_event_links(
                element, {"BURI", "CREM"}, sources_dict, obje_dict
            ):
                if url not in raw_death_links:
                    raw_death_links.append(url)
            for url in _get_all_event_links(element, {"BAPM"}, sources_dict, obje_dict):
                if url not in raw_birth_links:
                    raw_birth_links.append(url)

            person_context = f"{name} {surname} [{filename}]".strip()
            birth_links, b_misplaced = sanitize_links(
                raw_birth_links, "birth", context=person_context
            )
            death_links, d_misplaced = sanitize_links(
                raw_death_links, "death", context=person_context
            )

            indi_b_links, indi_d_links, indi_m_links = _extract_indi_links(
                element, sources_dict, obje_dict, context=person_context
            )

            marr_links = list(indi_m_links)
            for url, types in b_misplaced + d_misplaced:
                if "marriage" in types and url not in marr_links:
                    marr_links.append(url)
                if "birth" in types and url not in birth_links:
                    birth_links.append(url)
                if "death" in types and url not in death_links:
                    death_links.append(url)

            for url in indi_b_links:
                if url not in birth_links:
                    birth_links.append(url)
            for url in indi_d_links:
                if url not in death_links:
                    death_links.append(url)
            for url in _get_all_event_links(
                element, {"RESI", "OCCU"}, sources_dict, obje_dict
            ):
                if url not in birth_links:
                    birth_links.append(url)

            person_links = list(birth_links)
            for url in death_links:
                if url not in person_links:
                    person_links.append(url)

            is_deceased_flag = any(
                child.get_tag() in ("DEAT", "BURI")
                for child in element.get_child_elements()
            )

            famc_pointers = [
                child.get_value()
                for child in element.get_child_elements()
                if child.get_tag() == "FAMC"
            ]

            fams_pointers = [
                child.get_value()
                for child in element.get_child_elements()
                if child.get_tag() == "FAMS"
            ]

            individuals_dict[pointer] = {
                "_ptr": pointer,
                "name": name,
                "surname": surname,
                "married_surname": married_surname,
                "sex": sex,
                "birth_date": birth_date,
                "is_deceased": is_deceased_flag,
                "marr_links": marr_links,
                "famc": famc_pointers,
                "fams": fams_pointers,
            }

            has_events = bool(
                birth_date or birth_place or death_date or death_place or person_links
            )
            name_norm = name.strip().lower()
            is_placeholder_only = name_norm in ("private", "<private>") or (
                name.strip() == "NN" and not surname.strip()
            )
            if has_events or not is_placeholder_only:
                record = {
                    "_ptr": pointer,
                    "id": pointer,
                    "name": name,
                    "surname": surname,
                    "sex": sex,
                    "birth": {
                        "date": birth_date or "",
                        "place": birth_place or "",
                    },
                    "death": {
                        "date": death_date or "",
                        "place": death_place or "",
                    },
                }
                if married_surname:
                    record["alt_surname"] = married_surname
                if baptism_date or baptism_place:
                    record["baptism"] = {
                        "date": baptism_date or "",
                        "place": baptism_place or "",
                    }
                if person_links:
                    record["links"] = _dedup_links(person_links)
                persons_data.append(record)

        elif tag == "FAM":
            family_elements.append(element)

    def is_private_name(name, surname):
        return name.strip().lower() == "private" or surname.strip().lower() == "private"

    family_dict = {}
    for family in family_elements:
        h_ptr, w_ptr = "", ""
        for child in family.get_child_elements():
            if child.get_tag() == "HUSB":
                h_ptr = child.get_value()
            elif child.get_tag() == "WIFE":
                w_ptr = child.get_value()
        family_dict[family.get_pointer()] = {"husb": h_ptr, "wife": w_ptr}

    def _person_entry(pd):
        return {
            "id": pd.get("_ptr", ""),
            "name": pd.get("name", "") or "unknown",
            "surname": pd.get("surname", ""),
            "sex": pd.get("sex", ""),
            "date_of_birth": pd.get("birth_date", "") or "",
        }

    def _resolve_parent_fields(record):
        ptr = record.pop("_ptr", None)
        if not ptr:
            return

        indi_data = individuals_dict.get(ptr, {})

        parents_list = []
        famc_list = indi_data.get("famc", [])
        if famc_list:
            fam = family_dict.get(famc_list[0], {})
            for parent_ptr in (fam.get("husb", ""), fam.get("wife", "")):
                if parent_ptr:
                    pd = individuals_dict.get(parent_ptr, {})
                    if pd:
                        parents_list.append(_person_entry(pd))
        if parents_list:
            record["parents_list"] = parents_list

        fams_list = indi_data.get("fams", [])
        if fams_list:
            partners = []
            for fams_ptr in fams_list:
                fam = family_dict.get(fams_ptr, {})
                husb_ptr = fam.get("husb", "")
                wife_ptr = fam.get("wife", "")
                partner_ptr = (
                    wife_ptr if husb_ptr == ptr else husb_ptr if wife_ptr == ptr else ""
                )
                if partner_ptr:
                    pd = individuals_dict.get(partner_ptr, {})
                    if pd:
                        partners.append(_person_entry(pd))

            if partners:

                def _partner_sort_key(p):
                    y = extract_year(p["date_of_birth"])
                    return (y is None, y or 0, p["name"])

                partners.sort(key=_partner_sort_key)
                record["partners_list"] = partners

    for record in persons_data:
        _resolve_parent_fields(record)

    person_to_family_info = {}
    for fam_el in family_elements:
        fm_date, _, _ = get_event_data(fam_el, "MARR", sources_dict)
        fh_ptr, fw_ptr = "", ""
        fc_ptrs = []
        for ch in fam_el.get_child_elements():
            ctag = ch.get_tag()
            if ctag == "HUSB":
                fh_ptr = ch.get_value()
            elif ctag == "WIFE":
                fw_ptr = ch.get_value()
            elif ctag == "CHIL":
                fc_ptrs.append(ch.get_value())
        for sp_ptr in (fh_ptr, fw_ptr):
            if sp_ptr:
                person_to_family_info.setdefault(sp_ptr, []).append((fm_date, fc_ptrs))

    for ptr, data in individuals_dict.items():
        if data.get("birth_date") or data.get("is_deceased"):
            continue
        est_years = []
        for fm_date, fc_ptrs in person_to_family_info.get(ptr, []):
            fm_year = extract_year(fm_date)
            if fm_year:
                est_years.append(fm_year - 20)
            for fc_ptr in fc_ptrs:
                fc_data = individuals_dict.get(fc_ptr, {})
                fc_year = extract_year(fc_data.get("birth_date"))
                if fc_year:
                    est_years.append(fc_year - 20)
        for famc_ptr in data.get("famc", []):
            fam_d = family_dict.get(famc_ptr, {})
            for p_ptr in (fam_d.get("husb", ""), fam_d.get("wife", "")):
                if not p_ptr:
                    continue
                p_d = individuals_dict.get(p_ptr, {})
                p_year = extract_year(p_d.get("birth_date"))
                if p_year:
                    est_years.append(p_year + 40)
        if est_years:
            data["estimated_birth_year"] = max(est_years)

    for family in family_elements:
        marr_date, marr_place, raw_marr_links = get_event_data(
            family, "MARR", sources_dict, obje_dict
        )
        for url in _indi_level_link(family, sources_dict, obje_dict):
            if url not in raw_marr_links:
                raw_marr_links.append(url)

        husb_pointer, wife_pointer = "", ""
        child_pointers = []
        for child in family.get_child_elements():
            if child.get_tag() == "HUSB":
                husb_pointer = child.get_value()
            elif child.get_tag() == "WIFE":
                wife_pointer = child.get_value()
            elif child.get_tag() == "CHIL":
                child_pointers.append(child.get_value())

        husb = individuals_dict.get(husb_pointer, {})
        wife = individuals_dict.get(wife_pointer, {})

        family_context = (
            " & ".join(
                filter(
                    None,
                    [
                        f"{husb.get('name', '')} {husb.get('surname', '')}".strip(),
                        f"{wife.get('name', '')} {wife.get('surname', '')}".strip(),
                    ],
                )
            )
            or family.get_pointer()
        ) + f" [{filename}]"
        marr_links, _ = sanitize_links(
            raw_marr_links, "marriage", context=family_context
        )

        if not marr_links:
            marr_links = list(husb.get("marr_links", []) or wife.get("marr_links", []))

        def get_parents_list(person_data):
            parents_list = []
            if not person_data:
                return parents_list
            for famc_ptr in person_data.get("famc", []):
                fam_data = family_dict.get(famc_ptr)
                if fam_data:
                    for p_ptr in (fam_data["husb"], fam_data["wife"]):
                        if not p_ptr:
                            continue
                        p_data = individuals_dict.get(p_ptr)
                        if p_data:
                            parents_list.append(_person_entry(p_data))
            return parents_list

        husband_parents = get_parents_list(husb)
        wife_parents = get_parents_list(wife)

        children_list = []
        for child_ptr in child_pointers:
            child_data = individuals_dict.get(child_ptr)
            if child_data:
                children_list.append(_person_entry(child_data))

        def _by_birth_year(p):
            y = extract_year(p["date_of_birth"])
            return (y is None, y or 0, p["name"])

        children_list.sort(key=_by_birth_year)

        def _spouse_entry(pd):
            entry = {
                "id": pd.get("_ptr", ""),
                "name": pd.get("name", ""),
                "surname": pd.get("surname", ""),
                "sex": pd.get("sex", ""),
                "date_of_birth": pd.get("birth_date", "") or "",
            }
            married_surname = pd.get("married_surname", "")
            if married_surname:
                entry["alt_surname"] = married_surname
            return entry

        record = {
            "husband": _spouse_entry(husb),
            "wife": _spouse_entry(wife),
            "marriage": {
                "date": marr_date or "",
                "place": marr_place or "",
            },
        }

        if is_private_name(
            record["husband"]["name"], record["husband"]["surname"]
        ) or is_private_name(record["wife"]["name"], record["wife"]["surname"]):
            if record["marriage"]["date"]:
                record["marriage"]["date"] = "<private>"
            if record["marriage"]["place"]:
                record["marriage"]["place"] = "<private>"

        if marr_links:
            record["links"] = _dedup_links(marr_links)
        if children_list:
            record["children_list"] = children_list
        if husband_parents:
            record["husband_parents"] = husband_parents
        if wife_parents:
            record["wife_parents"] = wife_parents

        families_data.append(record)

    # --- 3. Filter recent records (privacy) ---

    persons_before = len(persons_data)
    families_before = len(families_data)

    filtered_persons = persons_before - len(persons_data)
    filtered_families = families_before - len(families_data)
    filtered_count = filtered_persons + filtered_families

    # --- 4. Write Output JSON Files ---
    ged_mtime = os.path.getmtime(input_path)

    persons_data.sort(
        key=lambda x: (
            x.get("surname", "") or "",
            x.get("name", "") or "",
            x.get("birth", {}).get("date", "") or "",
            x.get("death", {}).get("date", "") or "",
            x.get("birth", {}).get("place", "") or "",
            x.get("death", {}).get("place", "") or "",
        )
    )
    families_data.sort(
        key=lambda x: (
            x.get("husband", {}).get("surname", "") or "",
            x.get("husband", {}).get("name", "") or "",
            x.get("wife", {}).get("surname", "") or "",
            x.get("wife", {}).get("name", "") or "",
            x.get("marriage", {}).get("date", "") or "",
            x.get("marriage", {}).get("place", "") or "",
        )
    )

    with open(persons_output_path, "w", encoding="utf-8") as f:
        json.dump(persons_data, f, ensure_ascii=False, indent=4)
    os.utime(persons_output_path, (ged_mtime, ged_mtime))

    with open(families_output_path, "w", encoding="utf-8") as f:
        json.dump(families_data, f, ensure_ascii=False, indent=4)
    os.utime(families_output_path, (ged_mtime, ged_mtime))

    links_count = sum(1 for r in persons_data if r.get("links")) + sum(
        1 for r in families_data if r.get("links")
    )
    meta = {
        "contributor": contributor_id,
        "persons_count": len(persons_data),
        "families_count": len(families_data),
        "links_count": links_count,
        "filtered_count": filtered_count,
        "skipped": False,
        "last_modified": datetime.fromtimestamp(ged_mtime).isoformat(),
        "url": contributor_urls.get(contributor_id),
    }
    return meta, log


def main():
    """
    Main function to process all GEDCOM files in the input directory,
    extracting birth and marriage data into separate JSON files.
    """
    parser = argparse.ArgumentParser(description="Convert GEDCOM files to JSON.")
    parser.add_argument(
        "--mode",
        choices=["update", "full"],
        default="update",
        help="update (default): skip files whose JSON is already up to date; "
        "full: process all files and overwrite existing JSON.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        metavar="N",
        help="Number of parallel workers (default: 16).",
    )
    args = parser.parse_args()
    full_mode = args.mode == "full"

    print(
        f"Starting GEDCOM data extraction process (mode: {args.mode}, workers: {args.workers})...",
        file=sys.stderr,
    )

    load_url_cache()

    # --- Setup ---
    # Ensure the output directory exists, creating it if necessary.
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"Created output directory: {OUTPUT_DIR}", file=sys.stderr)

    # Check if the input directory exists.
    if not os.path.isdir(INPUT_DIR):
        print(f"Error: Input directory '{INPUT_DIR}' not found.", file=sys.stderr)
        print(
            "Please create it and place your GEDCOM (.ged) files inside.",
            file=sys.stderr,
        )
        return

    # --- File Processing Loop ---
    # Find all files ending with .ged in the input directory.
    try:
        locale.setlocale(locale.LC_COLLATE, ("sl_SI", "UTF-8"))
    except locale.Error:
        locale.setlocale(locale.LC_COLLATE, "")
    gedcom_files = sorted(
        [f for f in os.listdir(INPUT_DIR) if f.lower().endswith(".ged")],
        key=locale.strxfrm,
    )

    if not gedcom_files:
        print(f"No GEDCOM files found in '{INPUT_DIR}'.", file=sys.stderr)
        return

    # Store metadata about processed files for the frontend
    metadata = []
    contributor_urls = _load_contributor_urls()

    # Process files in parallel; print each file's output as its future completes.
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _process_one_file,
                filename,
                full_mode,
                contributor_urls,
                INPUT_DIR,
                OUTPUT_DIR,
            ): filename
            for filename in gedcom_files
        }
        pending = set(futures.values())
        for future in as_completed(futures):
            pending.discard(futures[future])
            meta, log_lines = future.result()
            for line in log_lines:
                print(line)
            if meta is not None:
                metadata.append(meta)
            if 0 < len(pending) <= args.workers:
                print(
                    f"Waiting for: {', '.join(sorted(pending, key=locale.strxfrm))}",
                    file=sys.stderr,
                )

    print("Completed!", file=sys.stderr)

    # Write global metadata.json for the frontend. This script owns contributors
    # whose name does NOT end in "-matricula"; the "-matricula" entries belong to
    # matricula-to-json.py and are preserved as-is.
    metadata_output_path = os.path.join(OUTPUT_DIR, "metadata.json")
    metadata_out = [
        {k: v for k, v in m.items() if k not in ("filtered_count", "skipped")}
        for m in metadata
    ]
    preserved = []
    if os.path.exists(metadata_output_path):
        try:
            with open(metadata_output_path, encoding="utf-8") as f:
                existing = json.load(f)
            preserved = [
                e for e in existing
                if e.get("contributor", "").endswith("-matricula")
            ]
        except (json.JSONDecodeError, OSError):
            preserved = []
    combined = metadata_out + preserved
    combined.sort(key=lambda x: locale.strxfrm(x.get("contributor", "")))
    with open(metadata_output_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=4)

    save_url_cache()

    # --- Summary table ---
    col_w = max(
        (len(m["contributor"]) for m in metadata if not m.get("skipped")), default=10
    )
    header = f"{'File':<{col_w}}  {'Persons':>7}  {'Families':>8}  {'Links':>6}  {'Filtered':>8}"
    print(f"\n{header}")
    print("-" * len(header))
    for m in metadata:
        if m.get("skipped"):
            continue
        print(
            f"{m['contributor']:<{col_w}}  "
            f"{m['persons_count']:>7}  "
            f"{m['families_count']:>8}  "
            f"{m['links_count']:>6}  "
            f"{m.get('filtered_count', 0):>8}"
        )
    total_p = sum(m["persons_count"] for m in metadata if not m.get("skipped"))
    total_f = sum(m["families_count"] for m in metadata if not m.get("skipped"))
    total_l = sum(m["links_count"] for m in metadata if not m.get("skipped"))
    total_fi = sum(m.get("filtered_count", 0) for m in metadata if not m.get("skipped"))
    print("-" * len(header))
    print(
        f"{'TOTAL':<{col_w}}  {total_p:>7}  {total_f:>8}  {total_l:>6}  {total_fi:>8}"
    )


if __name__ == "__main__":
    main()
    # Force immediate exit — os._exit() bypasses atexit/threading._shutdown()
    # which otherwise blocks on lingering urllib/SSL keep-alive connections.
    os._exit(0)
