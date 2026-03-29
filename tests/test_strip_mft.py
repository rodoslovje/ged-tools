import os
import tempfile
import textwrap
from tools.gedcom_cleaner import process_file

_SAMPLE = textwrap.dedent("""\
    0 HEAD
    1 CHAR UTF-8
    0 @I1@ INDI
    1 NAME John /Smith/
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
        _, strip_stats = process_file(inp, out, cleaners=[], strippers=["mft"], warn=False)
        assert strip_stats["mft"].removed == 2

        content = open(out, encoding="utf-8-sig").read()
        assert "_STE" not in content
        assert "@I1@" in content   # individual preserved
        assert "TRLR" in content   # trailer preserved
    finally:
        os.unlink(inp)
        os.unlink(out)
