"""Make hyphenated tool modules importable under underscored names.

The CLI tools live in `tools/gedcom-cleaner.py` etc., but Python's import
syntax disallows hyphens. Tests reference them as `tools.gedcom_cleaner`,
so this conftest pre-loads each hyphenated module under that alias before
test collection runs.
"""
import importlib.util
import os
import sys

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "tools")

for _filename in os.listdir(_TOOLS_DIR):
    if not _filename.endswith(".py") or "-" not in _filename or _filename.startswith("_"):
        continue
    _module_name = "tools." + _filename[:-3].replace("-", "_")
    if _module_name in sys.modules:
        continue
    _spec = importlib.util.spec_from_file_location(
        _module_name, os.path.join(_TOOLS_DIR, _filename)
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_module_name] = _mod
    _spec.loader.exec_module(_mod)
