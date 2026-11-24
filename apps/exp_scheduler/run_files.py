"""Names shared by every scheduler-run output (no hardware or Qt dependencies)."""
from __future__ import annotations

import re


def normalise_run_name(value: str) -> str:
    """Validate one portable filename component, preserving dots and Unicode."""
    name = value.strip() or "run"
    if (name in (".", "..") or name.endswith(".")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", name.split(".")[0])
            or len(name.encode("utf-16-le")) // 2 > 80):
        raise ValueError("Run name must be a valid Windows filename of at most 80 characters; "
                         "paths, reserved names and a trailing dot are not allowed.")
    return name


def run_filename(run_name: str, filename: str) -> str:
    return f"{normalise_run_name(run_name)}_{filename}"


def ruby_filename_label(value: str) -> str:
    """Make an embedded ruby label portable without changing the stored name.

    Limit the label to 40 UTF-16 units to leave room for the run prefix,
    instrument/time suffixes and attempt number on Windows. Existing export
    collision checks also cover names that become equal after sanitisation.
    """
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    label = label.encode("utf-16-le")[:80].decode("utf-16-le", errors="ignore")
    return label.rstrip(" .") or "ruby"
