"""Module-level singleton for UI language (English source / Japanese translation).

Call :func:`load` once at startup. Call sites translate an English source
string via :func:`tr`. UI reads/writes the active language via
:func:`get_language` / :func:`set_language`; the latter persists the choice
and emits ``signals.language_changed`` so already-open windows that opted in
(currently only ``ModeSelectorLauncher``) can retranslate live.
"""
from __future__ import annotations

import json
import pathlib

from PyQt6.QtCore import QObject, pyqtSignal

try:
    from settings.i18n_catalog import JA as _JA_CATALOG
except ImportError:
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from settings.i18n_catalog import JA as _JA_CATALOG

_PREFS_FILE = pathlib.Path(__file__).parent / "__localdata" / "language_settings.json"

_DEFAULT_LANG = "en"
_lang: str = _DEFAULT_LANG


class _Signals(QObject):
    language_changed = pyqtSignal(str)


signals = _Signals()


def load() -> None:
    """Read persisted language preference; falls back to default on any error."""
    global _lang
    try:
        with _PREFS_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        _lang = data.get("language", _DEFAULT_LANG)
    except Exception:
        _lang = _DEFAULT_LANG


def get_language() -> str:
    """Return the active language code ('en' or 'ja')."""
    return _lang


def set_language(code: str) -> None:
    """Persist *code* as the new active language and notify listeners."""
    global _lang
    if code == _lang:
        return
    _lang = code
    _save()
    signals.language_changed.emit(_lang)


def _save() -> None:
    try:
        _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _PREFS_FILE.open("w", encoding="utf-8") as fh:
            json.dump({"language": _lang}, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass


def tr(text: str, **kwargs) -> str:
    """Translate *text* (an English source string) to the active language.

    If ``kwargs`` are given, the translated (or source) string is passed
    through ``str.format(**kwargs)`` — write dynamic strings with named
    placeholders, e.g. ``tr("Connected ({label})", label=label)``, instead
    of pre-formatting an f-string, so the catalog key stays stable.
    """
    result = text if _lang == "en" else _JA_CATALOG.get(text, text)
    return result.format(**kwargs) if kwargs else result
