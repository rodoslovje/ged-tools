import os
import tempfile
import textwrap
from tools.gedcom_cleaner import process_file

_SAMPLE = textwrap.dedent("""\
    0 HEAD
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME John /Smith/
    1 ADDR 123 Main St
    2 LATI N46.0
    2 LONG E14.5
    1 DEAT Y
    2 ADDR Some Place
    3 MAP
    4 LATI N45.618392002777782
    4 LONG E15.236669038888888
    0 @T1@ _STE
    1 _NKY SourceTemplate_ImmigrationRecord_PhysicalCopy
    1 _STF @TF1@
    2 _TTL
    0 @T2@ _STE
    1 _NKY AnotherTemplate
    0 TRLR
""")


def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".ged")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_strip_mft_removes_ste_records():
    inp = _write_tmp(_SAMPLE)
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(inp, out, cleaners=[], strippers=["ste", "stf"], transformers=[], warn=False)
        assert strip_stats["ste"].removed == 2
        assert strip_stats["stf"].removed == 0

        content = open(out, encoding="utf-8-sig").read()
        assert "_STE" not in content
        assert "@I1@" in content   # individual preserved
        assert "TRLR" in content   # trailer preserved
    finally:
        os.unlink(inp)
        os.unlink(out)


def test_strip_addr_longlati():
    inp = _write_tmp(_SAMPLE)
    out = _write_tmp("")
    try:
        _, strip_stats, _ = process_file(inp, out, cleaners=[], strippers=["addr_longlati"], transformers=[], warn=False)
        assert strip_stats["addr_longlati"].removed == 3  # 2 direct + 1 MAP block

        content = open(out, encoding="utf-8-sig").read()
        assert "2 LATI" not in content
        assert "2 LONG" not in content
        assert "1 ADDR" in content   # ADDR itself preserved
    finally:
        os.unlink(inp)
        os.unlink(out)
