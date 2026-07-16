---
description: How to add English/Japanese i18n (tr()) to a new or existing file in this project — the two call-site patterns, the catalog file convention, and known edge cases.
---

# i18n integration (English/Japanese UI switching)

The app supports English/Japanese UI switching via `settings/i18n.py`
(the `tr()` function and language singleton) and
`settings/i18n_catalog.py` (a single `JA: dict[str, str]` mapping English
source string → Japanese translation, organized into per-source-file comment
sections, e.g. `# apps/scan2d/free_2d_scan_app.py / free_2d_scan_backend.py —
2D Scan`). A missing key in `JA` silently falls back to the English source —
there is no crash, just an untranslated string, so partial coverage during a
migration is safe.

## The two call-site patterns

**(a) Sub-app windows — evaluate `tr()` once, at construction time only.**
This is the pattern for every hardware sub-app window
(`Bl18cStageControlApp`, `Scan1DScanWindow`, `Pace5000Window`, etc.). There is
**no live language switching** while the window is already open — this was
an explicit decision agreed with the user during the `main.py`
implementation; changing it requires user confirmation, don't just add it
because it looks inconsistent.

Import with the standard try/except `sys.path` fallback (same shape used for
`PM16CController` elsewhere in the project):

```python
try:
    from settings.i18n import tr
except ImportError:
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from settings.i18n import tr
```

(adjust the number of `os.path.dirname(...)` calls to match how deep the file
sits under the repo root — see `apps/scan1d/scan1d_app.py` for a real
three-level example).

Wrap every user-visible string literal passed to a Qt widget:

```python
self.setWindowTitle(tr(window_title))
chsel_grp = QGroupBox(tr("Channel Selection"))
self._start_btn = QPushButton(tr("Start Scan"))
```

For strings that need runtime interpolation, pass named kwargs through
`tr()` rather than pre-formatting an f-string — this keeps the catalog key
itself stable (`"Ch{ch} Scan"`, not a different string per channel number):

```python
self._scan_grp = QGroupBox(tr("Ch{ch} Scan", ch=self._selected_channel()))
```

`tr()` internally does `text.format(**kwargs)` after the dictionary lookup,
so the catalog only ever needs one entry (`"Ch{ch} Scan"`) covering every
possible channel value.

**(b) `ModeSelectorLauncher` in `main.py` — the one live-retranslating window.**
`main.py` calls `i18n.load()` at startup and connects
`i18n.signals.language_changed` to a `_retranslate_ui()` method:

```python
i18n.signals.language_changed.connect(lambda _: self._retranslate_ui())
```

`i18n.set_language("en"/"ja")` is called from the language radio buttons in
the launcher, persists the choice to
`settings/__localdata/language_settings.json`, and emits the signal. Only the
launcher window needs to re-run its `tr()` calls live, because it's the only
window that can still be open while the user flips the language switch — all
other sub-app windows are per-session (pattern (a) above).

## Step-by-step: adding `tr()` to a new file

1. Add the import (pattern (a) above) if the file doesn't have it yet.
2. Wrap every literal string that reaches a Qt widget (`QLabel`, button
   text, window title, group box title, status messages, dialog text) in
   `tr(...)`. Use named-kwarg interpolation for anything dynamic instead of
   an f-string, so the catalog key doesn't fragment per value.
3. Open `settings/i18n_catalog.py` and add a new comment-delimited section
   for the file (`# apps/<app>/<file>.py — <WindowClassName>`), then add
   every new English string as a `JA` dict key with its Japanese
   translation. Keep one file's strings grouped under one section — don't
   scatter them.
4. A missing key just falls back to English — so if you can't get a
   translation immediately, it's safe to land the `tr()` wrapping first and
   backfill the `JA` entries after.

## Known edge cases (do not try to "fix" these)

- **PACE5000 intentionally mixes Japanese and English** in status strings —
  this is deliberate, not partial-translation debt.
- **`apps/exp_scheduler/`** — i18n is deliberately deferred. Confirm with the
  user before starting translation work there.
- **`apps/sample_camera_viewer/`** — unused per user decision; skip unless
  the user says otherwise.
- Dynamic f-strings that must stay in English (e.g. raw hardware protocol
  strings, log file paths) are intentional exceptions, not oversights — don't
  wrap those in `tr()`.
