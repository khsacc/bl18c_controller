"""Shared pytest set-up for the test suite.

Keep temporary directories out of the repository root.

Python's ``tempfile`` falls back to the current working directory when the
system temp folder (``%TEMP%``) is not writable, which happens when tests run
inside a sandboxed agent shell. The tests then leave empty ``tmpXXXXXXXX``
folders in the repository root. When that fallback would apply, temporary
files go to the Git-ignored ``.test_tmp/`` folder instead.
"""

import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP = _REPO_ROOT / ".test_tmp"


def _inside_repo(path: str) -> bool:
    try:
        Path(path).resolve().relative_to(_REPO_ROOT)
    except ValueError:
        return False
    return True


if _inside_repo(tempfile.gettempdir()):
    _TEST_TMP.mkdir(exist_ok=True)
    tempfile.tempdir = str(_TEST_TMP)
    for _name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[_name] = str(_TEST_TMP)
