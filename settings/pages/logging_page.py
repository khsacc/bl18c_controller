"""Settings page — Details log output directory and per-app save flags."""
from __future__ import annotations

import pathlib

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

try:
    from settings import log_prefs
    from settings.i18n import tr
except ImportError:
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from settings import log_prefs
    from settings.i18n import tr

# (log_prefs key, English display name — translated via tr() at construction time)
_APP_LABELS: list[tuple[str, str]] = [
    ("dac_scan",     "DAC Scan"),
    ("dac_scan_rot", "DAC Scan (Rot.)"),
    ("xrd_scan",     "XRD Scan"),
    ("autofocus",    "Autofocus"),
    ("free_2d_scan", "General 2D Scan"),
    ("scan1d",       "General 1D Scan"),
    ("pre_validator", "Sequence Pre-Validator"),
]


class LoggingPage(QWidget):
    """Settings page for log output directory and per-app save flags."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()
        self._refresh()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # ── Save location group ──────────────────────────────────────────────
        loc_group = QGroupBox(tr("Details log output directory"))
        loc_lay = QVBoxLayout(loc_group)
        loc_lay.setSpacing(8)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setReadOnly(True)
        self._path_edit.setMinimumWidth(320)
        self._browse_btn = QPushButton(tr("Browse…"))
        self._browse_btn.setFixedWidth(90)
        self._browse_btn.clicked.connect(self._on_browse)
        self._reset_btn = QPushButton(tr("Reset"))
        self._reset_btn.setFixedWidth(70)
        self._reset_btn.clicked.connect(self._on_reset)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(self._browse_btn, 0)
        path_row.addWidget(self._reset_btn, 0)
        loc_lay.addLayout(path_row)

        self._default_label = QLabel()
        self._default_label.setStyleSheet("font-size: 10px; color: #888;")
        loc_lay.addWidget(self._default_label)

        root.addWidget(loc_group)

        # ── Per-app preview ──────────────────────────────────────────────────
        preview_group = QGroupBox(tr("Per-app save location"))
        preview_lay = QVBoxLayout(preview_group)
        preview_lay.setSpacing(4)

        self._preview_labels: dict[str, QLabel] = {}
        for key, display in _APP_LABELS:
            row = QHBoxLayout()
            name_lbl = QLabel(f"{tr(display)}:")
            name_lbl.setFixedWidth(140)
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            path_lbl = QLabel()
            path_lbl.setStyleSheet("font-size: 10px; color: #444;")
            path_lbl.setWordWrap(True)
            row.addWidget(name_lbl)
            row.addWidget(path_lbl, 1)
            preview_lay.addLayout(row)
            self._preview_labels[key] = path_lbl

        root.addWidget(preview_group)

        # ── Per-app save flags (non-details mode only) ───────────────────────
        self._save_flags_group = QGroupBox(tr("Log Saving"))
        flags_lay = QVBoxLayout(self._save_flags_group)
        flags_lay.setSpacing(6)

        # Select all / Unselect all
        sel_row = QHBoxLayout()
        sel_all_btn = QPushButton(tr("Select all"))
        sel_all_btn.setFixedWidth(100)
        sel_all_btn.clicked.connect(self._on_select_all)
        unsel_all_btn = QPushButton(tr("Unselect all"))
        unsel_all_btn.setFixedWidth(100)
        unsel_all_btn.clicked.connect(self._on_unselect_all)
        sel_row.addWidget(sel_all_btn)
        sel_row.addWidget(unsel_all_btn)
        sel_row.addStretch(1)
        flags_lay.addLayout(sel_row)

        self._checkboxes: dict[str, QCheckBox] = {}
        for key, display in _APP_LABELS:
            cb = QCheckBox(tr(display))
            cb.setChecked(False)
            cb.toggled.connect(lambda checked, k=key: log_prefs.set_app_save(k, checked))
            flags_lay.addWidget(cb)
            self._checkboxes[key] = cb

        note = QLabel(tr("※ Checkbox selections are reset on restart"))
        note.setStyleSheet("font-size: 10px; color: #888;")
        flags_lay.addWidget(note)

        root.addWidget(self._save_flags_group)

        # ── Details-mode notice (shown instead of checkboxes) ────────────────
        self._details_notice = QLabel(
            tr("Running in --details mode. All apps save continuously.")
        )
        self._details_notice.setStyleSheet(
            "font-size: 12px; color: #1a6e1a; padding: 8px;"
            "border: 1px solid #a0d0a0; border-radius: 4px; background: #f0fff0;"
        )
        self._details_notice.setWordWrap(True)
        root.addWidget(self._details_notice)

        root.addStretch(1)

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_browse(self) -> None:
        start = str(log_prefs.get_base_dir())
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Select the Details log save folder"), start,
        )
        if not chosen:
            return
        log_prefs.set_base_dir(pathlib.Path(chosen))
        self._refresh()

    def _on_reset(self) -> None:
        log_prefs.reset_to_default()
        self._refresh()

    def _on_select_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(True)

    def _on_unselect_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        base = log_prefs.get_base_dir()
        default = log_prefs.get_default_base_dir()
        self._path_edit.setText(str(base))
        if base == default:
            self._default_label.setText(tr("Using default settings."))
        else:
            self._default_label.setText(tr("Default: {default}", default=default))
        for key, lbl in self._preview_labels.items():
            lbl.setText(str(base / key) + "/")

        in_details = log_prefs.is_details_mode()
        self._save_flags_group.setVisible(not in_details)
        self._details_notice.setVisible(in_details)
