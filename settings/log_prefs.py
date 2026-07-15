"""Module-level singleton for the details-log base directory and per-app save flags.

:func:`load` and :func:`set_details_mode` must both be called once at startup
(in ``main.py``) before any sub-window is opened.

Layout on disk::

    <base_dir>/
        dac_scan/
        dac_scan_rot/
        xrd_scan/
        autofocus/
        free_2d_scan/
"""
from __future__ import annotations

import json
import pathlib

# ── App keys ─────────────────────────────────────────────────────────────────

APP_KEYS: list[str] = [
    "dac_scan", "dac_scan_rot", "xrd_scan", "autofocus", "free_2d_scan", "scan1d",
    "pre_validator",
]

# ── Base directory (persisted) ────────────────────────────────────────────────

# Default: bl18c_controller/__localdata/   (one level above this file's package)
_DEFAULT_BASE: pathlib.Path = pathlib.Path(__file__).parent.parent / "__localdata"
_PREFS_FILE:   pathlib.Path = pathlib.Path(__file__).parent / "__localdata" / "log_settings.json"

_base_dir: pathlib.Path = _DEFAULT_BASE

# ── Runtime state (never persisted, resets every launch) ─────────────────────

_details_mode: bool = False
_app_save: dict[str, bool] = {k: False for k in APP_KEYS}


# ── Public API — startup ──────────────────────────────────────────────────────

def load() -> None:
    """Read persisted base-dir preference; falls back to default on any error."""
    global _base_dir
    try:
        with _PREFS_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        _base_dir = pathlib.Path(data["base_dir"])
    except Exception:
        _base_dir = _DEFAULT_BASE


def set_details_mode(enabled: bool) -> None:
    """Call once at startup with the value of the ``--details`` CLI flag."""
    global _details_mode
    _details_mode = enabled


# ── Public API — queries ──────────────────────────────────────────────────────

def is_details_mode() -> bool:
    return _details_mode


def should_save(key: str) -> bool:
    """Return True if logs should be saved for *key* this session."""
    return _details_mode or _app_save.get(key, False)


def get_base_dir() -> pathlib.Path:
    return _base_dir


def get_default_base_dir() -> pathlib.Path:
    return _DEFAULT_BASE


def get_app_dir(key: str) -> pathlib.Path:
    """Return ``<base_dir>/<key>`` and create it if necessary."""
    d = _base_dir / key
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Public API — checkbox state (not persisted) ───────────────────────────────

def set_app_save(key: str, enabled: bool) -> None:
    """Toggle per-app save flag from the Settings UI checkbox."""
    _app_save[key] = enabled


def get_app_save(key: str) -> bool:
    return _app_save.get(key, False)


# ── Public API — base-dir persistence ────────────────────────────────────────

def set_base_dir(path: pathlib.Path) -> None:
    """Persist *path* as the new base directory."""
    global _base_dir
    _base_dir = path
    _save()


def reset_to_default() -> None:
    """Revert to the built-in default and persist that choice."""
    global _base_dir
    _base_dir = _DEFAULT_BASE
    _save()


# ── Internal ──────────────────────────────────────────────────────────────────

def _save() -> None:
    try:
        _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _PREFS_FILE.open("w", encoding="utf-8") as fh:
            json.dump({"base_dir": str(_base_dir)}, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass
