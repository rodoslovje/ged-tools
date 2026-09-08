import os
import tempfile
import textwrap

import openpyxl
import pytest

from tools import matricula_to_json as m

BIRTH_HEADER = [
    "zp. št.", "župnija", "datum rojstva", "datum krsta", "naslov",
    "ime otroka", "ime očeta", "priimek očeta", "alt. priimek očeta",
    "ime matere", "priimek matere", "url naslov", "opombe", "interpret",
]
MARRIAGE_HEADER = [
    "zp. št.", "župnija", "datum poroke", "naslov",
    "ime ženina", "priimek ženina", "alt. priimek ženina",
    "ime neveste", "priimek neveste", "alt. priimek neveste",
    "url naslov", "opombe", "interpret",
]

CEPOVAN = "https://data.matricula-online.eu/sl/slovenia/koper/Cepovan"
MKK1 = f"{CEPOVAN}/%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1/"
MKK2 = f"{CEPOVAN}/%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+2/"
MKU1 = f"{CEPOVAN}/%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKU+1/"

# Two books as they appear on a parish listing page: the four-cell row, then
# the collapsed detail row carrying the authoritative 'Od/Do datuma'.
_PARISH_PAGE = textwrap.dedent("""\
    <table>
                    <tr>
                        <td nowrap>
                            <a class="mr-2 btn" href="/sl/slovenia/koper/Cepovan/%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1/" target="_blank">
                                <span title="Ogled knjige" class="fa fa-camera"></span>
                            </a>
                            <a class="btn" data-toggle="collapse" href="#details-564354">
                                <span class="fa fa-book"></span>
                            </a>
                        </td>
                        <td nowrap>ŠAK Ž Čep MKK 1</td>
                        <td>Matična knjiga krščenih, zvezek I</td>
                        <td>1743-1783, 1784-1819</td>
                    </tr>
                    <tr class="collapse" id="details-564354">
                        <td colspan="4">
                            <dl class="dl-horizontal">
                                <dt>Vrsta matične knjige</dt><dd>Matična knjiga krščenih</dd>
                                <dt>Od datuma</dt><dd>01. januar 1743</dd>
                                <dt>Do datuma</dt><dd>31. december 1819</dd>
                            </dl>
                        </td>
                    </tr>
                    <tr>
                        <td nowrap>
                            <a class="mr-2 btn" href="/sl/slovenia/maribor/bizeljsko/00070/" target="_blank">
                                <span class="fa fa-camera"></span>
                            </a>
                        </td>
                        <td nowrap>00070</td>
                        <td>Krstna knjiga / Taufbuch</td>
                        <td>1903-1911</td>
                    </tr>
    </table>
""")


def _write_workbook(path, sheets):
    """Build an xlsx from [(title, header, rows), ...]."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, header, rows in sheets:
        ws = wb.create_sheet(title=title)
        ws.append(header)
        for row in rows:
            ws.append(row)
    wb.save(path)


def _birth_row(seq, url, parish="Čepovan"):
    return [seq, parish, "1743-05-01", "1743-05-01", "Trebuša", "Jakob",
            "Urban", "Bončina", "Bonzhina", "Katarina", "Kogoj", url, None,
            "Kovačič_Martin"]


def _marriage_row(seq, url, parish="Čepovan"):
    return [seq, parish, "1735-05-02", "Trebuša", "Janez", "Kogoj", "Cogoi",
            "Neža", "Bratuž", "Bratus", url, None, "Kovačič_Martin"]


def test_detect_sheet_kind_from_columns():
    assert m.detect_sheet_kind(m._resolve_columns(BIRTH_HEADER)) == "K"
    assert m.detect_sheet_kind(m._resolve_columns(MARRIAGE_HEADER)) == "P"
    # Only the columns both kinds share: nothing to tell them apart.
    assert m.detect_sheet_kind(["seq", "parish", "address", "url"]) is None


@pytest.mark.parametrize("url, expected", [
    (f"{MKK1}?pg=202",
     ("slovenia/koper/Cepovan", "%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1")),
    # Locale segment and scheme must not split one book into several.
    ("http://data.matricula-online.eu/de/slovenia/maribor/sv-jurij/03110/?pg=5",
     ("slovenia/maribor/sv-jurij", "03110")),
    ("https://data.matricula-online.eu/sl/slovenia/maribor/sv-jurij/03110/",
     ("slovenia/maribor/sv-jurij", "03110")),
    # Free text and malformed hosts appear in url cells and are not books.
    ("ne najdem več v knjigi", (None, None)),
    ("https://datamatricula-onlineeu/sl/slovenia/x/01477/?pg=1", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_split_book_url(url, expected):
    assert m._split_book_url(url) == expected


def test_decode_signature():
    assert m._decode_signature(
        "%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1") == "ŠAK Ž Čep MKK 1"
    assert m._decode_signature("03110") == "03110"


def test_parse_parish_books():
    books = m.parse_parish_books(_PARISH_PAGE)
    assert set(books) == {"%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1", "00070"}
    mkk1 = books["%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1"]
    # Detail dates win over the displayed span's sub-ranges.
    assert mkk1["date"] == "1743-1819"
    assert mkk1["title"] == "Matična knjiga krščenih, zvezek I"
    # No detail row: fall back to the span cell.
    assert books["00070"]["date"] == "1903-1911"


def test_fetch_parish_books_follows_pagination(monkeypatch):
    """Parishes with more than 50 books span several pages, and the pager only
    links to the neighbouring ones."""
    page_two = _PARISH_PAGE.replace(
        "/sl/slovenia/maribor/bizeljsko/00070/", "/sl/slovenia/maribor/bizeljsko/00071/"
    ).replace(">00070<", ">00071<").replace(
        "/sl/slovenia/koper/Cepovan/%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1/",
        "/sl/slovenia/maribor/bizeljsko/00072/")
    pages = {
        "https://data.matricula-online.eu/sl/slovenia/maribor/bizeljsko/":
            _PARISH_PAGE + '<a href="?page=2">2</a>',
        "https://data.matricula-online.eu/sl/slovenia/maribor/bizeljsko/?page=2":
            page_two,
    }
    requested = []

    def fake_fetch(url):
        requested.append(url)
        return pages.get(url)

    monkeypatch.setattr(m, "_fetch_page", fake_fetch)
    books = m.fetch_parish_books("slovenia/maribor/bizeljsko")
    assert len(requested) == 2  # stops once a page no longer links onward
    assert "00070" in books and "00071" in books


def test_book_date_single_year():
    assert m._book_date({"Od datuma": "01. januar 1885",
                         "Do datuma": "31. december 1885"}, "") == "1885"


def test_read_rows_reads_one_sheet_per_kind(tmp_path):
    path = str(tmp_path / "Indeks P in K Čepovan 1735-1910.xlsx")
    _write_workbook(path, [
        ("Poroke", MARRIAGE_HEADER, [_marriage_row(1, f"{MKU1}?pg=108")]),
        ("Krsti", BIRTH_HEADER, [_birth_row(1, f"{MKK1}?pg=202"),
                                 _birth_row(2, f"{MKK2}?pg=3")]),
        # A working copy of the births sheet must not double every record.
        ("Krsti-kopija", BIRTH_HEADER, [_birth_row(1, f"{MKK1}?pg=202")]),
        ("imena", ["ime", "priimek"], [["Janez", "Kogoj"]]),
    ])
    kinds = [kind for kind, _ in m.read_rows(path)]
    assert kinds == ["P", "K", "K"]


def test_read_rows_falls_back_to_filename_kind(tmp_path):
    """A sheet with no role columns takes its kind from the filename."""
    path = str(tmp_path / "Indeks K Čepovan - 1819-1861.xlsx")
    _write_workbook(path, [
        ("Sheet1", ["zp. št.", "župnija", "naslov", "url naslov"],
         [[1, "Čepovan", "Trebuša", f"{MKK2}?pg=3"]]),
    ])
    assert [kind for kind, _ in m.read_rows(path)] == ["K"]


def _buckets(pairs):
    state = m.new_bucket_state()
    for kind, row in pairs:
        m.bucket_row(state, kind, row)
    return state["buckets"]


def test_bucket_row_groups_by_book():
    buckets = _buckets([
        ("K", {"url": f"{MKK1}?pg=202", "parish": "Čepovan"}),
        ("K", {"url": f"{MKK1}?pg=203", "parish": "Čepovan"}),
        ("K", {"url": f"{MKK2}?pg=1", "parish": "Čepovan"}),
        ("P", {"url": f"{MKU1}?pg=108", "parish": "Čepovan"}),
    ])
    assert [(b["kind"], m._decode_signature(b["signature"]), b["count"])
            for b in buckets] == [
        ("K", "ŠAK Ž Čep MKK 1", 2),
        ("K", "ŠAK Ž Čep MKK 2", 1),
        ("P", "ŠAK Ž Čep MKU 1", 1),
    ]


def test_bucket_row_attributes_url_less_rows():
    """Rows without a usable url join the preceding book of their own kind;
    rows seen before the first url join the first book found."""
    buckets = _buckets([
        ("K", {"url": "", "parish": "Čepovan"}),
        ("K", {"url": f"{MKK1}?pg=202", "parish": "Čepovan"}),
        ("K", {"url": "ne najdem več v knjigi", "parish": "Čepovan"}),
        ("K", {"url": f"{MKK2}?pg=1", "parish": "Čepovan"}),
        ("K", {"url": None, "parish": "Čepovan"}),
    ])
    assert [(m._decode_signature(b["signature"]), b["count"]) for b in buckets] == [
        ("ŠAK Ž Čep MKK 1", 3),
        ("ŠAK Ž Čep MKK 2", 2),
    ]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Keep tests off the network and off the developer's on-disk cache."""
    monkeypatch.setattr(m, "ALLOW_FETCH", False)
    monkeypatch.setattr(m, "_parish_cache", {})


@pytest.fixture
def cached_books(monkeypatch):
    """Serve book metadata as a previous fetch would have cached it."""
    monkeypatch.setattr(m, "_parish_cache", {
        "slovenia/koper/Cepovan": {
            "%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+1": {"date": "1743-1819"},
            "%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKK+2": {"date": "1819-1861"},
            "%25C5%25A0AK+%25C5%25BD+%25C4%258Cep+MKU+1": {"date": "1735-1819"},
        },
    })


def test_book_entries_single_book_keeps_filename(tmp_path, cached_books):
    path = str(tmp_path / "Indeks K Čepovan - 1819-1861.xlsx")
    open(path, "w").close()
    buckets = _buckets([("K", {"url": f"{MKK2}?pg=1", "parish": "Čepovan"})])
    entry, = m.book_entries(path, buckets, 1)
    assert entry["name"] == "Indeks K Čepovan - 1819-1861"
    assert entry["parish"] == "Čepovan"
    assert entry["type"] == "birth"
    assert entry["date"] == "1819-1861"
    assert entry["url"] == f"{MKK2}?pg=1"


def test_book_entries_one_entry_per_book(tmp_path, cached_books):
    path = str(tmp_path / "Indeks P in K Čepovan 1735-1910.xlsx")
    open(path, "w").close()
    buckets = _buckets([
        ("P", {"url": f"{MKU1}?pg=108", "parish": "Čepovan"}),
        ("K", {"url": f"{MKK1}?pg=202", "parish": "Čepovan"}),
        ("K", {"url": f"{MKK2}?pg=1", "parish": "Čepovan"}),
    ])
    entries = m.book_entries(path, buckets, 3)
    assert [(e["name"], e["type"], e["date"], e["url"]) for e in entries] == [
        ("Indeks P Čepovan - 1735-1819", "marriage", "1735-1819", f"{MKU1}?pg=1"),
        ("Indeks K Čepovan - 1743-1819", "birth", "1743-1819", f"{MKK1}?pg=1"),
        ("Indeks K Čepovan - 1819-1861", "birth", "1819-1861", f"{MKK2}?pg=1"),
    ]
    assert all(e["parish"] == "Čepovan" for e in entries)


def test_book_entries_fall_back_to_signature_without_metadata(tmp_path):
    """An unknown book still gets a name that tells it apart from its siblings."""
    path = str(tmp_path / "Indeks K Čepovan 1735-1910.xlsx")
    open(path, "w").close()
    buckets = _buckets([
        ("K", {"url": f"{MKK1}?pg=1", "parish": "Čepovan"}),
        ("K", {"url": f"{MKK2}?pg=1", "parish": "Čepovan"}),
    ])
    entries = m.book_entries(path, buckets, 2)
    assert [e["name"] for e in entries] == [
        "Indeks K Čepovan - ŠAK Ž Čep MKK 1",
        "Indeks K Čepovan - ŠAK Ž Čep MKK 2",
    ]
    assert all(e["date"] == "" for e in entries)


def test_bucket_parish_prefers_filename_spelling():
    bucket = {"parishes": {"Sv Jurij ob Ščavnici": 3}}
    assert m._bucket_parish(bucket, "Sv. Jurij ob Ščavnici") == "Sv. Jurij ob Ščavnici"
    # A genuinely different parish in the cell still wins.
    assert m._bucket_parish(bucket, "Ljutomer") == "Sv Jurij ob Ščavnici"
    assert m._bucket_parish({"parishes": {}}, "Ljutomer") == "Ljutomer"


def test_parish_from_name_handles_both_kinds():
    assert m._parish_from_name("Indeks P in K Čepovan 1735-1910") == "Čepovan"
    assert m._parish_from_name("Indeks K Čepovan - 1819-1861") == "Čepovan"
