"""Shared notification sound library.

``SOUND_OPTIONS`` and ``play_done_sound()`` are imported by every app that
wants to play a completion sound (Rad-icon, DAC scan, collimator scan, …).
The actual selection is persisted by :mod:`settings.notification_prefs`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SOUNDS_DIR = Path(__file__).parent.parent / "assets" / "sounds"

# (key, label, wav_filename | None)
# key   — stable identifier stored in prefs JSON; never rename existing keys
# label — display string shown in the UI combo box
SOUND_OPTIONS: list[tuple[str, str, str | None]] = [
    ("none",     "None",  None),
    ("pastel",   "🎉",    "pastel.wav"),
    ("piano",    "🎹",    "piano.wav"),
    ("chime",    "✨",   "chime.wav"),
    ("hyoshigi", "🏮",    "Hyoshigi.wav"),
    ("boat",     "🚢",    "boat.wav"),
    ("glocken",  "🔑",    "Glocken.wav"),
    ("cuckoo",   "🐦",    "Cuckoo.wav"),
    ("switch",   "🎠",    "switch.wav"),
    ("digital",  "🔊",    "digital.wav"),
    ("digital2", "💎",   "digital2.wav"),
    ("fireworks",  "🎆",    "fireworks.wav"),
    ("furin",  "🎐",    "furin.wav"),
    ("done",     "😶",    "done.wav"),
    ("bell",     "🔔",    "bell.wav"),
]

_sound_effect = None   # lazy-init QSoundEffect singleton
_afplay_proc = None    # track macOS afplay subprocess for stop support


def _get_sound_effect():
    global _sound_effect
    if _sound_effect is not None:
        return _sound_effect
    try:
        from PyQt6.QtMultimedia import QSoundEffect
        _sound_effect = QSoundEffect()
        return _sound_effect
    except Exception:
        return None


def stop_current_sound() -> None:
    """Stop any currently playing notification sound."""
    global _afplay_proc
    se = _get_sound_effect()
    if se is not None:
        se.stop()
        return
    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
    else:
        if _afplay_proc is not None and _afplay_proc.poll() is None:
            _afplay_proc.terminate()
            _afplay_proc = None


def play_done_sound(wav_name: str | None) -> None:
    """Play *wav_name* (e.g. ``'chime.wav'``) as a non-blocking notification.

    Pass ``None`` to play nothing. Stops any in-progress playback first.
    """
    global _afplay_proc
    stop_current_sound()
    if wav_name is None:
        return
    wav_path = _SOUNDS_DIR / wav_name
    if not wav_path.exists():
        return

    se = _get_sound_effect()
    if se is not None:
        from PyQt6.QtCore import QUrl
        se.setSource(QUrl.fromLocalFile(str(wav_path)))
        se.play()
        return

    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            pass
    else:
        try:
            import subprocess
            _afplay_proc = subprocess.Popen(["afplay", str(wav_path)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def play_current_sound() -> None:
    """Read the current selection from notification_prefs and play it."""
    try:
        from settings import notification_prefs
    except ImportError:
        import os
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from settings import notification_prefs
    key = notification_prefs.get_sound()
    wav_name = next((w for k, _, w in SOUND_OPTIONS if k == key), None)
    play_done_sound(wav_name)
