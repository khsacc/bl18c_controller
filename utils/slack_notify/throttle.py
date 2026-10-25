"""Suppress and summarise repeated identical error notifications.

Pure logic with an injectable clock so it can be tested without threads.

Rule per error key:
  * first occurrence → send now, open a suppression window of `window_s`;
  * repeats inside the window → counted, not sent;
  * when the window ends with repeats counted → send one summary and open a
    new window (so a continuous stream gives at most one message per window);
  * when the window ends with no repeats → the key is forgotten.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Numbers vary between otherwise identical errors (positions, timings,
# step indices), so they are blanked out when building the key.
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def error_key(code: str, message: str) -> str:
    normalised = _NUMBER_RE.sub("#", " ".join(message.split()))[:300]
    return f"{code or 'unknown'}|{normalised}"


@dataclass
class _Window:
    first_text: str
    ends_at: float
    repeats: int = 0
    last_seen_text: str = ""
    last_seen_label: str = ""


class ErrorAggregator:
    def __init__(self, window_s: float) -> None:
        self.window_s = max(0.0, float(window_s))
        self._windows: dict[str, _Window] = {}

    def submit(self, key: str, text: str, now: float, label: str = "") -> str | None:
        """Register an error. Returns the text to send now, or None if suppressed."""
        if self.window_s <= 0:
            return text
        window = self._windows.get(key)
        if window is not None and now < window.ends_at:
            window.repeats += 1
            window.last_seen_text = text
            window.last_seen_label = label
            return None
        self._windows[key] = _Window(first_text=text, ends_at=now + self.window_s)
        return text

    def due(self, now: float) -> list[str]:
        """Return summaries for windows that have ended (call periodically)."""
        out: list[str] = []
        for key in list(self._windows):
            window = self._windows[key]
            if now < window.ends_at:
                continue
            if window.repeats == 0:
                del self._windows[key]
                continue
            minutes = self.window_s / 60.0
            out.append(
                f":repeat: The same error occurred {window.repeats} more time(s) "
                f"in the last {minutes:g} min (further notifications were suppressed)."
                + (f"\nLast occurrence: {window.last_seen_label}" if window.last_seen_label else "")
                + f"\n{window.last_seen_text}"
            )
            self._windows[key] = _Window(first_text=window.first_text, ends_at=now + self.window_s)
        return out

    def pending(self) -> bool:
        return bool(self._windows)
