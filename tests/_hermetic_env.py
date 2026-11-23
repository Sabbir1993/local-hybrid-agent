"""Point the app at a throwaway config tree before any core module is imported.

The test suite must not read a developer's machine-local config/app.json: several
tests pin launch argv and default budgets, which vary with the local runtime.
This module is imported by tests/conftest.py (pytest) and, first, by
tests/test_00_isolation.py (unittest discover). It is intentionally dependency-free.
"""
import os
import shutil
import tempfile
from pathlib import Path


def setup() -> None:
    if os.environ.get("A770_TEST_CONFIG_DIR"):
        return
    repo = Path(__file__).resolve().parents[1]
    src = repo / "config"
    dst = Path(tempfile.mkdtemp(prefix="a770_test_config_"))
    for example in sorted(src.glob("*.example.json")):
        shutil.copyfile(example, dst / example.name.replace(".example.json", ".json"))
    os.environ["A770_TEST_CONFIG_DIR"] = str(dst)


setup()
