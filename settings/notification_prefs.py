"""Module-level singleton for sound notification preferences.

Call :func:`load` once at startup.  UI reads/writes via :func:`get_sound` and
:func:`set_sound`.
"""
from __future__ import annotations

import json
import pathlib

_PREFS_FILE = pathlib.Path(__file__).parent / "__localdata" / "notification_settings.json"

_DEFAULT_SOUND = "chime"
_sound_label: str = _DEFAULT_SOUND


def load() -> None:
    """Read persisted preference; falls back to default on any error."""
    global _sound_label
    try:
        with _PREFS_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        _sound_label = data.get("done_sound", _DEFAULT_SOUND)
    except Exception:
        _sound_label = _DEFAULT_SOUND


def get_sound() -> str:
    """Return the current sound label (e.g. 'なし', '2音チャイム', …)."""
    return _sound_label


def set_sound(label: str) -> None:
    """Persist *label* as the new sound selection."""
    global _sound_label
    _sound_label = label
    _save()


def _save() -> None:
    try:
        _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _PREFS_FILE.open("w", encoding="utf-8") as fh:
            json.dump({"done_sound": _sound_label}, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass
