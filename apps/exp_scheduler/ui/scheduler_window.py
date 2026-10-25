"""
ExperimentalSchedulerWindow — Task 5 full implementation.
"""
from __future__ import annotations

import copy
import html
import json
import logging
import math
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QElapsedTimer, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..actions import (
    AlignToReferenceAction,
    FollowSampleAction,
    ForLoopAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    StartFollowingAction,
    TakeXrdAction,
    TakeSpectrumAction,
)
from ..device_context import DeviceContext
from ..gasket_units import legacy_tolerance_um, load_stage_um_per_pixel
from ..runner import SequenceRunner
from ..scheduler_settings import (
    GlobalCameraSettings,
    GlobalFollowSettings,
    GlobalLimits,
    GlobalPositions,
    GlobalSpectrumSettings,
    GlobalShutterSettings,
    GlobalXrdSettings,
    RUBY_ORIGIN_XRD_POSITION,
)
from .. import ruby_pressure, validation_service
from ..sequence import Sequence
from ..validator.models import (
    Severity, ValidationCertificate, ValidationPhase, ValidationReport, group_diagnostics,
)
from .dsl_editor import DslEditor
from .llm_panel import LlmPanel
from .monitor_panel import MonitorPanel
from apps.interactive_camera.gasket_tracking import reference_digest
from .timeline_widget import TimelineWidget
from settings.notification_sound import play_current_sound
from settings import online_spectrometer_prefs
from utils.pdindexer import PdiService, Transport, Trigger
from utils.slack_notify import PlannedStep

_log = logging.getLogger(__name__)

_LOCALDATA_DIR = Path(__file__).parent.parent / "__localdata"
_DEFAULT_REF_PATH = _LOCALDATA_DIR / "reference_frame.png"
_SETTINGS_PATH = _LOCALDATA_DIR / "scheduler_window_settings.json"
_RADICON_PREFS_PATH = (
    Path(__file__).parent.parent.parent / "Rad_icon_2022"
    / "__localdata" / "radicon_ui_prefs.json"
)

# Default limit value in mm (used when no saved value exists)
_DEFAULT_LIMIT_MM = 1.0


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


def _radicon_correction_file_defaults() -> dict[str, object]:
    """Read the detector UI's saved correction settings, if available."""
    try:
        prefs = json.loads(_RADICON_PREFS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(prefs, dict):
        return {}
    defaults: dict[str, object] = {
        xrd_key: value
        for xrd_key, prefs_key in (
            ("dark_file", "dark_file_path"),
            ("defect_file", "defect_file_path"),
        )
        if isinstance(value := prefs.get(prefs_key), str) and value.strip()
    }
    for xrd_key, prefs_key in (
        ("dark_enabled", "dark_correct_enabled"),
        ("flip_v", "flip_v"),
        ("flip_h", "flip_h"),
    ):
        if isinstance(value := prefs.get(prefs_key), bool):
            defaults[xrd_key] = value
    mode = prefs.get("defect_correct_mode")
    if mode in ("none", "median", "neg1"):
        defaults["defect_enabled"] = mode != "none"
        defaults["defect_mode"] = mode if mode != "none" else "median"
    elif isinstance(value := prefs.get("defect_correct_enabled"), bool):
        defaults["defect_enabled"] = value
    kernel = prefs.get("defect_kernel_size")
    if isinstance(kernel, str) and kernel[:1] in ("3", "4", "5", "6"):
        defaults["defect_kernel"] = int(kernel[0])
    return defaults


def _line_prefix(d) -> str:
    """The "Line N: " prefix for a Diagnostic's `source_line`, used when
    rendering a ValidationReport — but only for STATIC/PREFLIGHT
    Diagnostics whose `source_line` was backfilled by
    validation_service.py's `_with_source_lines()` after the fact.
    COMPILE-phase Diagnostics (dsl/compiler.py, dsl/parser.py) already
    embed "Line N: " directly in their own `.message` text, so adding
    another prefix here would duplicate it."""
    if d.source_line is None or d.phase is ValidationPhase.COMPILE:
        return ""
    return f"Line {d.source_line}: "


class ExperimentalSchedulerWindow(QMainWindow):
    _shutter_restore_done = pyqtSignal(str)

    @property
    def shutter_in_use(self) -> bool:
        runner = self._finishing_runner
        return self._shutter_restore_busy or bool(
            runner is not None and runner._global_shutter.only_open_during_xrd
        )

    @property
    def sequence_active(self) -> bool:
        """True from Run until the post-run shutter restore has finished.

        The launcher refuses to open other sub-windows while this is True;
        it turns False just before _do_restore() reopens the closed ones.
        """
        return self._finishing_runner is not None or self._shutter_restore_busy

    def __init__(self, ctx: DeviceContext, main_window=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Experimental Scheduler")
        self.resize(1650, 900)

        self._ctx = ctx
        self._main_window = main_window
        self._owns_pdi_service = main_window is None or getattr(main_window, "pdi_service", None) is None
        self._pdi_service = (getattr(main_window, "pdi_service", None) if main_window is not None
                             else None) or PdiService(watch_folder=_LOCALDATA_DIR / "pdindexer_watch")
        self._active_pdi_transport: Transport | None = None
        # App-wide Slack gateway (None when run without the launcher).
        self._slack = getattr(main_window, "slack_service", None) if main_window is not None else None
        self._runner: SequenceRunner | None = None
        self._finishing_runner: SequenceRunner | None = None
        self._shutter_restore_busy = False
        self._shutter_restore_worker: threading.Thread | None = None
        self._sequence: Sequence = Sequence(actions=[], name="")
        self._captured_frame: np.ndarray | None = None
        self._ref_current_path: Path | None = None
        self._gasket_ellipse: dict[str, float] | None = None
        self._gasket_sample_point: dict[str, float] | None = None
        self._gasket_reference_digest: str | None = None
        self._legacy_gasket_tolerance_px: float | None = None
        self._closed_btns: list = []
        self._validated = False
        self._certificate: ValidationCertificate | None = None
        self._last_step_index: int | None = None
        self._last_step_description: str = ""
        self._last_tab_index = 0
        self._xrd_inherited_defaults: dict[str, object] = {}
        self._spectrum_xrd_reference: tuple[int, int] | None = None
        self._spectrum_target_reference: tuple[int, int] | None = None
        self._spectrum_offset: tuple[int | None, int | None] = (None, None)
        self._sample_xrd_position: tuple[int, int, int] | None = None
        self._ruby_positions: dict[str, dict[str, int]] = {}
        self._ruby_offset_origin: str = RUBY_ORIGIN_XRD_POSITION
        # name -> {"lambda_0_nm", "t0_k"}; same names as _ruby_positions.
        self._ruby_references: dict[str, dict[str, float | None]] = {}
        # Guards against _on_tab_changed() re-triggering auto-convert while
        # _on_dsl_converted() switches to the Visual tab after an explicit
        # Convert-to-Visual already ran the full validator once — without
        # this, that programmatic setCurrentIndex(0) re-enters
        # _on_tab_changed() synchronously (Qt's currentChanged fires before
        # setCurrentIndex() returns), which sees the same "leaving Script
        # for Visual" condition and calls convert_to_visual() again, running
        # DslCompiler + the full device preflight a second time for one
        # click (see _on_dsl_converted()).
        self._switching_tabs_after_convert = False

        self._elapsed_clock = QElapsedTimer()
        self._elapsed_update_timer = QTimer(self)
        self._elapsed_update_timer.setInterval(250)
        self._elapsed_update_timer.timeout.connect(self._update_elapsed_time)
        self._current_time_timer = QTimer(self)
        self._current_time_timer.setInterval(250)
        self._current_time_timer.timeout.connect(self._update_current_time)

        _LOCALDATA_DIR.mkdir(parents=True, exist_ok=True)

        self._build_ui()
        self._update_current_time()
        self._current_time_timer.start()
        self._refresh_poni_status()
        self._refresh_pdi_controls()
        if self._ctx.poni_state is not None:
            self._ctx.poni_state.poni_changed.connect(self._refresh_poni_status)
            self._ctx.poni_state.poni_changed.connect(self._reset_validation)
        self._shutter_restore_done.connect(self._on_shutter_restore_done)
        self._dsl_editor.set_validator(self._validate_dsl_text)
        self._restore_settings()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Toolbar spans the full width
        root.addLayout(self._make_toolbar())

        self._run_banner = QLabel()
        self._run_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._run_banner.setMinimumHeight(64)
        self._run_banner.hide()
        root.addWidget(self._run_banner)

        # Body: left panel (global settings) | right panel (tabs)
        # Both sides are wrapped in independent QScrollAreas so the window
        # does not overflow even when the left panel becomes very tall.
        body = QHBoxLayout()
        body.setSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(6)
        self._limit_panel = self._make_limit_panel()
        left.addWidget(self._limit_panel)
        self._positions_panel = self._make_positions_panel()
        left.addWidget(self._positions_panel)
        left.addWidget(self._make_logging_panel())
        self._xrd_panel = self._make_xrd_panel()
        left.addWidget(self._xrd_panel)
        self._shutter_panel = self._make_shutter_panel()
        left.addWidget(self._shutter_panel)
        self._sample_positions_panel = self._make_sample_positions_panel()
        left.addWidget(self._sample_positions_panel)
        self._ruby_fluorescence_group = self._make_ruby_fluorescence_group()
        left.addWidget(self._ruby_fluorescence_group)
        self._follow_panel = self._make_follow_panel()
        left.addWidget(self._follow_panel)
        left.addStretch()

        # Any change to a fingerprinted Global setting after a successful
        # Validate must discard the certificate (REORGANISATION_PLAN.md §7
        # Phase 8) — the Logging panel is deliberately excluded, it is not
        # part of the settings fingerprint.
        for panel in (
            self._limit_panel, self._positions_panel, self._xrd_panel, self._shutter_panel,
            self._sample_positions_panel,
            self._ruby_fluorescence_group, self._follow_panel,
        ):
            self._wire_settings_invalidation(panel)

        left_content = QWidget()
        left_content.setLayout(left)

        left_scroll = QScrollArea()
        left_scroll.setWidget(left_content)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(500)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_content.setMaximumWidth(
            left_scroll.width() - 2 * left_scroll.frameWidth()
            - left_scroll.verticalScrollBar().sizeHint().width()
        )

        right_tabs = self._make_tabs()
        right_scroll = QScrollArea()
        right_scroll.setWidget(right_tabs)
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Validation results sit under the sequence editor only, so the
        # Global settings and run-monitor columns keep the full height.
        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        centre_layout.setSpacing(6)
        centre_layout.addWidget(right_scroll, stretch=1)
        centre_layout.addWidget(self._make_validation_panel())

        # Run monitor (microscope / PACE5000 / LakeShore 335) — rightmost
        # column, separated by a splitter so it can be resized or collapsed.
        self._monitor_panel = MonitorPanel(self._ctx, self._interactive_camera_window)
        self._monitor_panel.set_ruby_pressure_enabled(self._pressure_enable_chk.isChecked())
        monitor_scroll = QScrollArea()
        monitor_scroll.setWidget(self._monitor_panel)
        monitor_scroll.setWidgetResizable(True)
        monitor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        monitor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(centre)
        splitter.addWidget(monitor_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([600, 520])

        body.addWidget(left_scroll)
        body.addWidget(splitter, stretch=1)
        root.addLayout(body, stretch=1)

        self._update_xrd_panel_visibility()
        self._update_follow_panel_visibility()

    def _make_validation_panel(self) -> QGroupBox:
        group = QGroupBox("Validation Results")
        vbox = QVBoxLayout(group)
        vbox.setContentsMargins(4, 4, 4, 4)
        self._validation_output = QTextEdit()
        self._validation_output.setReadOnly(True)
        self._validation_output.setFixedHeight(200)
        self._validation_output.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        font = self._validation_output.font()
        font.setFamily("Courier New")
        font.setPointSize(9)
        self._validation_output.setFont(font)
        self._validation_output.setPlaceholderText("Click Validate to check the sequence…")
        vbox.addWidget(self._validation_output)
        return group

    def _show_validation_result(self, result, ok_message: str = "Validation passed — no errors found") -> None:
        """Render a ValidationReport into the validation output panel.

        Shared by the Visual tab's Validate button and the Script tab's
        DslEditor (via its validation_result signal), so both entry points
        report into this one panel instead of keeping separate status areas.
        `ok_message` lets a caller customise the success text; it is
        ignored whenever there are errors or warnings to show instead.

        Renders from `result.diagnostics` directly (not the `errors`/
        `warnings` string lists) so a DSL-sourced Diagnostic's
        `source_line` (see validation_service.py's `_with_source_lines()`)
        can be shown as a `Line N:` prefix — REORGANISATION_PLAN.md §7
        Phase 7 item 8.
        """
        # Identical errors repeated across for-loop iterations are merged
        # into one line (see validator.models.group_diagnostics).
        errors = group_diagnostics(
            [d for d in result.diagnostics if d.severity is Severity.ERROR])
        warnings = group_diagnostics(
            [d for d in result.diagnostics if d.severity is Severity.WARNING])

        def _fmt(g) -> str:
            d = g.first
            text = _line_prefix(d) + d.message
            if g.count > 1:
                if len(g.steps) > 1:
                    shown = ", ".join(str(n) for n in g.steps[:5])
                    more = ", …" if len(g.steps) > 5 else ""
                    text += f"  [×{g.count}, Steps {shown}{more}]"
                else:
                    text += f"  [×{g.count}]"
            # Message text may contain "<", ">", "&" (e.g. move-constraint
            # messages use "<="/">") — escape it or Qt's rich-text renderer
            # will parse it as (invalid, silently-swallowed) markup.
            return html.escape(text)

        if errors:
            lines = ["<span style='color:#c62828;'>&#x2717; Validation FAILED</span>"]
            lines += [f"<span style='color:#c62828;'>  &#x2717; {_fmt(d)}</span>" for d in errors]
            lines += [f"<span style='color:darkorange;'>  &#x26a0; {_fmt(d)}</span>" for d in warnings]
        elif warnings:
            lines = ["<span style='color:darkorange;'>&#x26a0; Validation passed with warnings</span>"]
            lines += [f"<span style='color:darkorange;'>  &#x26a0; {_fmt(d)}</span>" for d in warnings]
        else:
            lines = [f"<span style='color:#2e7d32;'>&#x2713; {html.escape(ok_message)}</span>"]
        self._validation_output.setHtml("<br>".join(lines))

    def _make_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self._btn_run = QPushButton("▶  Run")
        self._btn_run.clicked.connect(self._on_run)
        self._btn_run.setEnabled(False)
        bar.addWidget(self._btn_run)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setToolTip("Decelerate-stop the stage, then end the sequence.")
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_stop.setEnabled(False)
        bar.addWidget(self._btn_stop)

        self._btn_emergency_stop = QPushButton("Emergency Stop")
        self._btn_emergency_stop.setToolTip("Emergency-stop the stage immediately, then end the sequence.")
        self._btn_emergency_stop.setStyleSheet(
            "QPushButton { color: white; background-color: #c62828; font-weight: bold; }"
            "QPushButton:disabled { color: #cccccc; background-color: #e0a0a0; }"
        )
        self._btn_emergency_stop.clicked.connect(self._on_emergency_stop)
        self._btn_emergency_stop.setEnabled(False)
        bar.addWidget(self._btn_emergency_stop)

        self._btn_save = QPushButton("Save…")
        self._btn_save.clicked.connect(self._on_save)
        bar.addWidget(self._btn_save)

        self._btn_load = QPushButton("Load…")
        self._btn_load.clicked.connect(self._on_load)
        bar.addWidget(self._btn_load)

        # Optional: a run never depends on Slack being set up.
        bar.addSpacing(12)
        self._slack_chk = QCheckBox("Notify on Slack")
        self._slack_chk.toggled.connect(self._on_slack_toggled)
        bar.addWidget(self._slack_chk)
        if self._slack is not None:
            self._slack.add_listener(self._refresh_slack_checkbox)
        self._refresh_slack_checkbox()

        bar.addSpacing(16)
        self._status_label = QLabel("Status: Ready")
        self._status_label.setStyleSheet("color: gray;")
        bar.addWidget(self._status_label)
        bar.addStretch()

        self._current_time_label = QLabel()
        clock_font = self._current_time_label.font()
        clock_font.setPointSizeF(clock_font.pointSizeF() * 1.5)
        self._current_time_label.setFont(clock_font)
        self._current_time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self._current_time_label)
        bar.addSpacing(12)

        self._elapsed_label = QLabel("Elapsed: 00:00:00")
        self._elapsed_label.setFont(clock_font)
        self._elapsed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self._elapsed_label)

        return bar

    def _make_limit_panel(self) -> QGroupBox:
        group = QGroupBox("Global Limits")
        group.setToolTip("Limits relative to the starting position")
        form = QFormLayout(group)
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # Column header
        header = QHBoxLayout()
        header.addWidget(QLabel("−"), 1, Qt.AlignmentFlag.AlignCenter)
        header.addWidget(QLabel("+"), 1, Qt.AlignmentFlag.AlignCenter)
        form.addRow("Ch", header)

        def _spin() -> QDoubleSpinBox:
            s = _no_wheel(QDoubleSpinBox())
            s.setRange(0.0, 9999.99)
            s.setDecimals(2)
            s.setSingleStep(0.1)
            s.setValue(_DEFAULT_LIMIT_MM)
            s.setMinimumWidth(70)
            return s

        self._lim_ch3_minus = _spin()
        self._lim_ch3_plus  = _spin()
        self._lim_ch4_minus = _spin()
        self._lim_ch4_plus  = _spin()
        self._lim_ch5_minus = _spin()
        self._lim_ch5_plus  = _spin()

        for ch, minus_spin, plus_spin in (
            ("Ch3 (mm)", self._lim_ch3_minus, self._lim_ch3_plus),
            ("Ch4 (mm)", self._lim_ch4_minus, self._lim_ch4_plus),
            ("Ch5 (mm)", self._lim_ch5_minus, self._lim_ch5_plus),
        ):
            row = QHBoxLayout()
            row.addWidget(minus_spin, 1)
            row.addWidget(plus_spin, 1)
            form.addRow(ch + ":", row)

        return group

    def _make_positions_panel(self) -> QGroupBox:
        group = QGroupBox("Positions")
        form = QFormLayout(group)
        for attr, label in (
            ("_pos_ch8_in", "Ch8 In (pulse):"),
            ("_pos_ch8_out", "Ch8 OUT (pulse):"),
            ("_pos_ch9_out", "Ch9 OUT (pulse):"),
            ("_pos_ch9_in", "Ch9 IN (pulse):"),
        ):
            spin = _no_wheel(QSpinBox())
            spin.setRange(-2_147_483_647, 2_147_483_647)
            setattr(self, attr, spin)
            form.addRow(label, spin)
        return group

    def _build_global_positions(self) -> GlobalPositions:
        return GlobalPositions(
            ch8_in=self._pos_ch8_in.value(),
            ch8_out=self._pos_ch8_out.value(),
            ch9_out=self._pos_ch9_out.value(),
            ch9_in=self._pos_ch9_in.value(),
        )

    def _positions_ui_to_dict(self) -> dict:
        return vars(self._build_global_positions()).copy()

    def _positions_dict_to_ui(self, values: dict) -> None:
        defaults = GlobalPositions.from_stage_settings()
        for name, spin in (
            ("ch8_in", self._pos_ch8_in),
            ("ch8_out", self._pos_ch8_out),
            ("ch9_out", self._pos_ch9_out),
            ("ch9_in", self._pos_ch9_in),
        ):
            spin.setValue(int(values.get(name, getattr(defaults, name))))

    def _make_logging_panel(self) -> QGroupBox:
        group = QGroupBox("Logging")
        form = QFormLayout(group)
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self._log_path_edit = QLineEdit()
        self._log_path_edit.setPlaceholderText("run")
        self._log_path_edit.setText("run")
        self._log_path_edit.setToolTip(
            "Base name for the run directory.\n"
            "Each run is saved as <Data directory>/<name>_<timestamp>/"
        )
        form.addRow("Run name:", self._log_path_edit)

        dir_row = QHBoxLayout()
        self._data_dir_edit = QLineEdit()
        self._data_dir_edit.setPlaceholderText("(default: __localdata/runs)")
        self._data_dir_edit.setToolTip(
            "Directory under which each run's <name>_<timestamp> folder is created.\n"
            "All of a run's data is saved in that folder:\n"
            "  XRD/       XRD images (with xrd_master.csv and 1D profiles)\n"
            "  spectrum/  ruby fluorescence spectra\n"
            "  photo/     camera snapshots\n"
            "  log/       sequence.json, metadata.json, conditions.csv, ops.log\n"
            "Leave empty to use the default __localdata/runs/"
        )
        dir_row.addWidget(self._data_dir_edit, stretch=1)
        btn_browse_data_dir = QPushButton("…")
        btn_browse_data_dir.setFixedWidth(28)
        btn_browse_data_dir.clicked.connect(self._on_browse_data_dir)
        dir_row.addWidget(btn_browse_data_dir)
        form.addRow("Data directory:", dir_row)

        devices_w = QWidget()
        devices_l = QVBoxLayout(devices_w)
        devices_l.setContentsMargins(0, 0, 0, 0)
        devices_l.setSpacing(4)
        self._log_pace5000_chk = QCheckBox("PACE5000")
        self._log_pace5000_chk.setChecked(True)
        self._log_lakeshore_chk = QCheckBox("LakeShore 335")
        self._log_lakeshore_chk.setChecked(True)
        devices_l.addWidget(self._log_pace5000_chk)
        devices_l.addWidget(self._log_lakeshore_chk)
        form.addRow(QLabel("Log devices:"))
        form.addRow(devices_w)

        return group

    def _build_log_config(self) -> tuple[str, list[str]]:
        path = self._log_path_edit.text().strip() or "run"
        devices: list[str] = []
        if self._log_pace5000_chk.isChecked():
            devices.append("pace5000")
        if self._log_lakeshore_chk.isChecked():
            devices.append("lakeshore")
        return path, devices

    def _build_data_dir(self) -> str | None:
        return self._data_dir_edit.text().strip() or None

    def _on_browse_data_dir(self) -> None:
        current = self._data_dir_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Data Directory", current)
        if chosen:
            self._data_dir_edit.setText(chosen)

    def _log_ui_to_dict(self) -> dict:
        path, _ = self._build_log_config()
        return {
            "path": path,
            "data_dir": self._data_dir_edit.text().strip(),
            "pace5000": self._log_pace5000_chk.isChecked(),
            "lakeshore": self._log_lakeshore_chk.isChecked(),
        }

    def _log_dict_to_ui(self, d: dict) -> None:
        self._log_path_edit.setText(d.get("path", "run"))
        # "dir" is the former log-only directory key.
        self._data_dir_edit.setText(d.get("data_dir", d.get("dir", "")))
        self._log_pace5000_chk.setChecked(bool(d.get("pace5000", True)))
        self._log_lakeshore_chk.setChecked(bool(d.get("lakeshore", True)))

    def _save_logging_settings(self) -> None:
        self._set_setting("logging", self._log_ui_to_dict())

    def _build_global_limits(self) -> GlobalLimits | None:
        """Read limit spinboxes and return a GlobalLimits instance.

        Returns None only when the controller is absent and limits cannot be
        applied (safeguard). In normal use the spinboxes always have values
        (default 1.0 mm), so None is never returned from this path.
        """
        return GlobalLimits(
            ch3_minus_mm=self._lim_ch3_minus.value(),
            ch3_plus_mm=self._lim_ch3_plus.value(),
            ch4_minus_mm=self._lim_ch4_minus.value(),
            ch4_plus_mm=self._lim_ch4_plus.value(),
            ch5_minus_mm=self._lim_ch5_minus.value(),
            ch5_plus_mm=self._lim_ch5_plus.value(),
        )

    def _has_follow_action(self, actions) -> bool:
        for action in actions:
            if isinstance(action, (FollowSampleAction, StartFollowingAction,
                                   AlignToReferenceAction)):
                return True
            if isinstance(action, ForLoopAction) and self._has_follow_action(action.body):
                return True
        return False

    def _has_xrd_action(self, actions) -> bool:
        for action in actions:
            if isinstance(action, TakeXrdAction):
                return True
            if isinstance(action, ForLoopAction) and self._has_xrd_action(action.body):
                return True
        return False

    def _has_spectrum_action(self, actions) -> bool:
        for action in actions:
            if isinstance(action, TakeSpectrumAction):
                return True
            if isinstance(action, ForLoopAction) and self._has_spectrum_action(action.body):
                return True
        return False

    def _uses_camera(self, actions) -> bool:
        """True when SequenceRunner may open its own camera session (camera
        steps, or ruby spectra, whose image alignment uses camera 0)."""
        for action in actions:
            if isinstance(action, (SaveSnapshotAction, SaveReferenceImageAction, StartFollowingAction,
                                   AlignToReferenceAction, FollowSampleAction, TakeSpectrumAction)):
                return True
            if isinstance(action, ForLoopAction) and self._uses_camera(action.body):
                return True
        return False

    def _update_xrd_panel_visibility(self) -> None:
        has_xrd = self._has_xrd_action(self._sequence.actions)
        self._xrd_panel.setVisible(has_xrd)
        self._shutter_panel.setVisible(has_xrd)

    def _make_shutter_panel(self) -> QGroupBox:
        group = QGroupBox("X-ray Shutter Settings")
        layout = QVBoxLayout(group)
        self._shutter_xrd_only_chk = QCheckBox("Only open shutter during\nXRD measurements")
        layout.addWidget(self._shutter_xrd_only_chk)
        return group

    def _build_global_shutter(self) -> GlobalShutterSettings:
        return GlobalShutterSettings(
            only_open_during_xrd=(self._has_xrd_action(self._sequence.actions)
                                  and self._shutter_xrd_only_chk.isChecked())
        )

    def _update_follow_panel_visibility(self) -> None:
        has_follow = self._has_follow_action(self._sequence.actions)
        has_spectrum = self._has_spectrum_action(self._sequence.actions)
        self._ruby_fluorescence_group.setVisible(has_spectrum)
        # Spectrum image alignment also uses the Ch4/Ch5 follow correction limits.
        self._follow_panel.setVisible(has_follow or has_spectrum)

    def _make_ruby_fluorescence_group(self) -> QGroupBox:
        group = QGroupBox("Ruby Fluorescence Settings")
        form = QFormLayout(group)
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        connection_note = QLabel("Connection: Settings > Online spectrometer")
        connection_note.setWordWrap(True)
        connection_note.setStyleSheet("font-size: 11px; color: #666;")
        form.addRow(connection_note)

        self._spectrum_exposure_spin = _no_wheel(QDoubleSpinBox())
        self._spectrum_exposure_spin.setRange(0.01, 5.0)
        self._spectrum_exposure_spin.setDecimals(3)
        self._spectrum_exposure_spin.setSingleStep(0.1)
        self._spectrum_exposure_spin.setValue(0.1)
        self._spectrum_exposure_spin.setSuffix(" s")
        form.addRow("Default exposure:", self._spectrum_exposure_spin)

        suffix_row = QGridLayout()
        suffix_row.setContentsMargins(0, 0, 0, 0)
        suffix_row.setHorizontalSpacing(8)
        self._spectrum_suffix_chks = {}
        for index, (key, label) in enumerate((
            ("pressure_gas", "Pgas"),
            ("temperature_set", "Tset"),
            ("temperature_b", "TB"),
            ("datetime_full", "YYYY-MM-DD-HH-MM-SS"),
            ("datetime_time", "HH-MM-SS"),
        )):
            checkbox = QCheckBox(label)
            checkbox.setChecked(key in GlobalSpectrumSettings.filename_suffixes)
            self._spectrum_suffix_chks[key] = checkbox
            suffix_row.addWidget(checkbox, index // 2, index % 2)
        suffix_row.setColumnStretch(2, 1)
        suffix_widget = QWidget()
        suffix_widget.setLayout(suffix_row)
        suffix_widget.setToolTip(
            "Suffixes are appended after the prefix in this order: Pgas, Tset, "
            "TB, date and time. Instrument values require a connected device; "
            "Pgas and TB also require a recent reading."
        )
        form.addRow(QLabel("File name suffix:"))
        form.addRow(suffix_widget)

        self._spectrum_r2_spin = _no_wheel(QDoubleSpinBox())
        self._spectrum_r2_spin.setRange(0.0, 1.0)
        self._spectrum_r2_spin.setDecimals(3)
        self._spectrum_r2_spin.setSingleStep(0.01)
        self._spectrum_r2_spin.setValue(0.95)
        form.addRow("Minimum R²:", self._spectrum_r2_spin)

        self._spectrum_em_gain_max_spin = _no_wheel(QSpinBox())
        self._spectrum_em_gain_max_spin.setRange(1, 1000)
        self._spectrum_em_gain_max_spin.setValue(10)
        form.addRow("Maximum EM Gain:", self._spectrum_em_gain_max_spin)

        self._spectrum_max_accum_spin = _no_wheel(QSpinBox())
        self._spectrum_max_accum_spin.setRange(1, 10)
        self._spectrum_max_accum_spin.setValue(4)
        form.addRow("Maximum accumulations:", self._spectrum_max_accum_spin)

        self._spectrum_search_chk = QCheckBox("Search best position nearby")
        form.addRow("Default nearby search:", self._spectrum_search_chk)
        self._spectrum_search_when_combo = _no_wheel(QComboBox())
        self._spectrum_search_when_combo.addItem("Always", "always")
        self._spectrum_search_when_combo.addItem(
            "If initial R² is below threshold", "on_low_quality",
        )
        form.addRow("Default search timing:", self._spectrum_search_when_combo)
        self._spectrum_search_axes = {}
        for ch in (3, 4, 5):
            selected = QCheckBox(f"Ch{ch}")
            selected.setChecked(ch in (4, 5))
            width = _no_wheel(QDoubleSpinBox())
            width.setRange(0.1, 100.0)
            width.setDecimals(1)
            width.setValue(10.0)
            width.setSuffix(" µm")
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(selected)
            layout.addWidget(width)
            layout.addWidget(QLabel("step 2 µm"))
            form.addRow(f"Default Ch{ch} full range:", row)
            self._spectrum_search_axes[ch] = (selected, width)

        self._spectrum_fit_combo = _no_wheel(QComboBox())
        self._spectrum_fit_combo.addItems(["Moffat", "Pseudo Voigt", "Gauss", "Lorentz"])
        form.addRow("Ruby fit function:", self._spectrum_fit_combo)

        self._spectrum_baseline_combo = _no_wheel(QComboBox())
        for label, value in (("Constant", "constant"), ("Linear", "linear"),
                             ("Quadratic", "quadratic"),
                             ("Auto polynomial", "auto_polynomial")):
            self._spectrum_baseline_combo.addItem(label, value)
        self._spectrum_baseline_combo.setCurrentIndex(
            self._spectrum_baseline_combo.findData("linear"))
        form.addRow("Ruby fit background:", self._spectrum_baseline_combo)

        self._spectrum_speed_combo = _no_wheel(QComboBox())
        self._spectrum_speed_combo.addItems(["H", "M", "L"])
        self._spectrum_speed_combo.setCurrentText("M")
        form.addRow("Stage speed:", self._spectrum_speed_combo)
        self._spectrum_settle_spin = _no_wheel(QSpinBox())
        self._spectrum_settle_spin.setRange(0, 60_000)
        self._spectrum_settle_spin.setValue(100)
        self._spectrum_settle_spin.setSuffix(" ms")
        form.addRow("Settle time:", self._spectrum_settle_spin)
        form.addRow(self._make_pressure_group())
        return group

    def _make_pressure_group(self) -> QGroupBox:
        group = QGroupBox("Pressure Calculation")
        form = QFormLayout(group)
        self._pressure_form = form
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._pressure_enable_chk = QCheckBox("Calculate pressure\n(FluoRaPressée)")
        self._pressure_enable_chk.setToolTip(
            "The final acquisition uses POST /acquire/pressure; FluoRaPressée "
            "calculates the pressure from Peak1 (R1)."
        )
        form.addRow(self._pressure_enable_chk)

        self._pressure_scale_combo = _no_wheel(QComboBox())
        self._pressure_scale_combo.addItem("(select a pressure scale)", "")
        for key, label in ruby_pressure.PRESSURE_SCALES.items():
            self._pressure_scale_combo.addItem(label, key)
        form.addRow("Pressure scale:", self._pressure_scale_combo)

        self._temp_correction_chk = QCheckBox("Temperature correction of λ₀")
        form.addRow(self._temp_correction_chk)
        self._temp_scale_combo = _no_wheel(QComboBox())
        self._temp_scale_combo.addItem("(select a correction scale)", "")
        for key, (label, _) in ruby_pressure.TEMPERATURE_SCALES.items():
            self._temp_scale_combo.addItem(label, key)
        form.addRow("Correction scale:", self._temp_scale_combo)
        self._temp_source_combo = _no_wheel(QComboBox())
        for key, label in ruby_pressure.TEMPERATURE_SOURCES.items():
            self._temp_source_combo.addItem(label, key)
        form.addRow("Temperature source:", self._temp_source_combo)
        self._manual_temp_spin = _no_wheel(QDoubleSpinBox())
        self._manual_temp_spin.setRange(*ruby_pressure.TEMPERATURE_RANGE_K)
        self._manual_temp_spin.setDecimals(2)
        self._manual_temp_spin.setValue(298.15)
        self._manual_temp_spin.setSuffix(" K")
        form.addRow("Manual temperature:", self._manual_temp_spin)
        self._temp_max_age_spin = _no_wheel(QDoubleSpinBox())
        self._temp_max_age_spin.setRange(*ruby_pressure.TEMPERATURE_MAX_AGE_RANGE_S)
        self._temp_max_age_spin.setDecimals(0)
        self._temp_max_age_spin.setValue(10.0)
        self._temp_max_age_spin.setSuffix(" s")
        form.addRow("Max. reading age:", self._temp_max_age_spin)

        self._btn_ruby_references = QPushButton("Zero-pressure references\n(λ₀, T₀)…")
        self._btn_ruby_references.clicked.connect(self._open_ruby_references)
        form.addRow(self._btn_ruby_references)
        self._ruby_references_summary = QLabel()
        self._ruby_references_summary.setWordWrap(True)
        self._ruby_references_summary.setStyleSheet("font-size: 11px; color: #666;")
        form.addRow(self._ruby_references_summary)

        for widget in (self._pressure_enable_chk, self._temp_correction_chk):
            widget.toggled.connect(self._update_pressure_widgets)
        self._temp_source_combo.currentIndexChanged.connect(self._update_pressure_widgets)
        self._update_pressure_widgets()
        return group

    def _update_pressure_widgets(self) -> None:
        form = self._pressure_form
        enabled = self._pressure_enable_chk.isChecked()
        correction = enabled and self._temp_correction_chk.isChecked()
        lakeshore = self._temp_source_combo.currentData() == "lakeshore_b"
        # Rows below an unchecked toggle are hidden; within the visible
        # correction rows, the temperature-source-specific field is greyed
        # out together with its label.
        for widget in (self._pressure_scale_combo, self._temp_correction_chk,
                       self._btn_ruby_references, self._ruby_references_summary):
            widget.setEnabled(enabled)
            form.setRowVisible(widget, enabled)
        for widget in (self._temp_scale_combo, self._temp_source_combo):
            widget.setEnabled(correction)
            form.setRowVisible(widget, correction)
        for widget, on in ((self._manual_temp_spin, correction and not lakeshore),
                           (self._temp_max_age_spin, correction and lakeshore)):
            form.setRowVisible(widget, correction)
            widget.setEnabled(on)
            form.labelForField(widget).setEnabled(on)
        self._update_ruby_references_summary()
        if hasattr(self, "_monitor_panel"):
            self._monitor_panel.set_ruby_pressure_enabled(enabled)

    def _update_ruby_references_summary(self) -> None:
        if not hasattr(self, "_ruby_references_summary"):
            return
        names = self._spectrum_position_names()
        if not names:
            self._ruby_references_summary.setText("No ruby positions recorded")
            return
        parts = []
        for name in names:
            values = self._ruby_references.get(name) or {}
            lambda_0, t0 = values.get("lambda_0_nm"), values.get("t0_k")
            text = f"λ₀={lambda_0:g} nm" if lambda_0 is not None else "λ₀ not set"
            if t0 is not None:
                text += f", T₀={t0:g} K"
            parts.append(f"{name}: {text}")
        self._ruby_references_summary.setText("; ".join(parts))

    def _open_ruby_references(self) -> None:
        from .ruby_reference_dialog import RubyReferenceDialog
        names = self._spectrum_position_names()
        if not names:
            QMessageBox.information(
                self, "Ruby zero-pressure references",
                "Record ruby positions first (Sample Positions > Set up sample positions).",
            )
            return
        dlg = RubyReferenceDialog(
            names, self._ruby_references,
            temperature_correction=self._temp_correction_chk.isChecked(), parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._ruby_references = dlg.references
        self._update_ruby_references_summary()
        self._save_spectrum_settings()
        self._reset_validation()

    def _prune_ruby_references(self) -> None:
        """Drop references whose ruby position no longer exists."""
        names = set(self._spectrum_position_names())
        self._ruby_references = {
            name: values for name, values in self._ruby_references.items() if name in names
        }
        self._update_ruby_references_summary()

    def _make_sample_positions_panel(self) -> QGroupBox:
        group = QGroupBox("Sample Positions")
        layout = QVBoxLayout(group)
        guidance = QLabel(
            "Capture the reference photo in Set up sample positions, or load an existing "
            "photo below. The same image is used for following and ruby alignment. "
            "Use Set up sample positions to record the XRD position and named ruby "
            "offsets (Ch3/4/5). Gasket outline (Beta) moves the "
            "marked sample centre onto the X-ray beam registered in Interactive Camera; "
            "the gasket outline is marked in Set up sample positions, and its ruby "
            "offsets are relative to the gasket centre. Replacing the image "
            "clears recorded positions and the gasket outline."
        )
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        layout.addWidget(self._make_reference_image_group())
        tracking_form = QFormLayout()
        self._follow_method_combo = _no_wheel(QComboBox())
        self._follow_method_combo.addItem("Image similarity (current)", "similarity")
        self._follow_method_combo.addItem("Gasket outline (Beta)", "gasket_beta")
        self._follow_method_combo.currentIndexChanged.connect(self._on_follow_method_changed)
        tracking_form.addRow("Position tracking:", self._follow_method_combo)
        self._gasket_tolerance_spin = _no_wheel(QDoubleSpinBox())
        self._gasket_tolerance_spin.setRange(0.0, 100.0)
        self._gasket_tolerance_spin.setDecimals(1)
        self._gasket_tolerance_spin.setSpecialValueText("Enter µm limit")
        self._gasket_tolerance_spin.setValue(8.0)
        self._gasket_tolerance_spin.setSuffix(" µm")
        self._gasket_tolerance_spin.valueChanged.connect(self._on_gasket_tolerance_changed)
        self._gasket_tolerance_spin.setToolTip(
            "Maximum gasket-centre offset after correction. Each follow attempt corrects its measured shift down to whole Ch4/Ch5 pulses."
        )
        tracking_form.addRow("Gasket tolerance:", self._gasket_tolerance_spin)
        # Only gasket outline tracking uses these rows; hidden for image similarity.
        tracking_form.setRowVisible(self._gasket_tolerance_spin, False)
        self._gasket_average_frames_spin = _no_wheel(QSpinBox())
        self._gasket_average_frames_spin.setRange(1, 10)
        self._gasket_average_frames_spin.setValue(5)
        self._gasket_average_frames_spin.setToolTip(
            "Fresh camera frames averaged for gasket alignment and verification"
        )
        tracking_form.addRow("Average frames (Beta):", self._gasket_average_frames_spin)
        tracking_form.setRowVisible(self._gasket_average_frames_spin, False)
        self._gasket_allow_scale_chk = QCheckBox("Allow uniform ellipse scaling (Beta)")
        self._gasket_allow_scale_chk.setToolTip(
            "Track aperture diameter changes while keeping the marked ellipse aspect ratio "
            "(up to 20% smaller or larger)."
        )
        tracking_form.addRow("", self._gasket_allow_scale_chk)
        tracking_form.setRowVisible(self._gasket_allow_scale_chk, False)
        self._gasket_allow_scale_chk.toggled.connect(self._on_follow_method_changed)
        layout.addLayout(tracking_form)
        self._tracking_form = tracking_form
        button = QPushButton("Set up sample positions…")
        button.clicked.connect(self._open_sample_positions)
        layout.addWidget(button)
        self._sample_positions_summary = QLabel()
        self._sample_positions_summary.setWordWrap(True)
        layout.addWidget(self._sample_positions_summary)
        self._update_sample_positions_summary()
        return group

    def _make_reference_image_group(self) -> QGroupBox:
        group = QGroupBox("Reference Image")
        layout = QVBoxLayout(group)
        layout.setSpacing(4)
        layout.setContentsMargins(4, 6, 4, 4)

        button_row = QHBoxLayout()
        self._btn_load_ref = QPushButton("Load from…")
        self._btn_load_ref.clicked.connect(self._on_load_ref_file)
        button_row.addWidget(self._btn_load_ref)
        button_row.addStretch()
        layout.addLayout(button_row)

        status_row = QHBoxLayout()
        self._ref_status_label = QLabel("No image")
        self._ref_status_label.setStyleSheet("color: gray;")
        status_row.addWidget(self._ref_status_label)
        status_row.addStretch()
        self._btn_preview = QPushButton("Preview")
        self._btn_preview.setEnabled(False)
        self._btn_preview.clicked.connect(self._on_preview_ref)
        status_row.addWidget(self._btn_preview)
        layout.addLayout(status_row)
        return group

    def _spectrum_position_names(self) -> tuple[str, ...]:
        names = set(self._ruby_positions)
        if all(v is not None for v in self._spectrum_offset):
            names.add("Default")
        return tuple(sorted(names))

    def _update_sample_positions_summary(self) -> None:
        if not hasattr(self, "_sample_positions_summary"):
            return
        names = self._spectrum_position_names()
        self._sample_positions_summary.setText(
            "XRD Ch3/4/5: " + (str(self._sample_xrd_position) if self._sample_xrd_position else "not recorded")
            + " | Ruby positions: " + (", ".join(names) if names else "none recorded")
            + (" (offsets from the gasket centre)"
               if names and self._ruby_offset_origin != RUBY_ORIGIN_XRD_POSITION else "")
        )

    def _open_sample_positions(self) -> None:
        from .sample_positions_dialog import SamplePositionsDialog
        if self._runner is not None and self._runner.isRunning():
            QMessageBox.warning(self, "Sample Positions", "Wait for the sequence to finish before setting positions.")
            return
        s = self._get_settings()
        dlg = SamplePositionsDialog(
            self._ctx.controller, self._captured_frame, self._ref_current_path,
            self._sample_xrd_position, self._ruby_positions,
            tracking_method=self._follow_method_combo.currentData(),
            gasket_ellipse=copy.deepcopy(self._gasket_ellipse),
            gasket_sample_point=copy.deepcopy(self._gasket_sample_point),
            ruby_offset_origin=self._ruby_offset_origin,
            gasket_reference_digest=self._gasket_reference_digest,
            gasket_tolerance_um=(None if self._legacy_gasket_tolerance_px is not None
                                 else self._gasket_tolerance_spin.value()),
            gasket_tolerance_px=self._legacy_gasket_tolerance_px,
            gasket_allow_uniform_scale=self._gasket_allow_scale_chk.isChecked(),
            borrowed_frame=self._borrow_camera_frame if self._interactive_camera_window() is not None else None,
            save_directory=s.get("last_ref_save_dir", str(_DEFAULT_REF_PATH.parent)),
            parent=self,
        )
        dlg.exec()
        self._sample_xrd_position = dlg.xrd_position
        self._ruby_positions = dlg.ruby_positions
        self._ruby_offset_origin = dlg.ruby_offset_origin
        if dlg.reference_changed and dlg.reference_path is not None:
            self._ref_current_path = Path(dlg.reference_path)
            self._set_setting("ref_save_path", str(self._ref_current_path))
            self._set_setting("last_ref_save_dir", str(dlg.save_directory))
            if dlg.reference_frame is not None:
                self._apply_ref_frame(dlg.reference_frame, self._ref_current_path.name)
            self._clear_gasket_outline()
        if dlg.gasket_changed:
            self._gasket_ellipse = dlg.gasket_ellipse
            self._gasket_sample_point = dlg.gasket_sample_point
            self._gasket_reference_digest = dlg.gasket_reference_digest
            self._save_follow_settings()
            self._on_follow_method_changed()
        if self._sample_xrd_position is not None:
            self._spectrum_xrd_reference = self._sample_xrd_position[1:]
        if "Default" in self._ruby_positions:
            values = self._ruby_positions["Default"]
            self._spectrum_offset = (values["ch4"], values["ch5"])
            self._spectrum_target_reference = (
                self._spectrum_xrd_reference[0] + values["ch4"],
                self._spectrum_xrd_reference[1] + values["ch5"],
            ) if self._spectrum_xrd_reference else None
        else:
            self._spectrum_target_reference = None
            self._spectrum_offset = (None, None)
        self._prune_ruby_references()
        self._update_sample_positions_summary()
        self._save_spectrum_settings()
        self._reset_validation()

    def _clear_sample_positions(self) -> None:
        self._sample_xrd_position = None
        self._spectrum_xrd_reference = None
        self._spectrum_target_reference = None
        self._spectrum_offset = (None, None)
        self._ruby_positions.clear()
        self._prune_ruby_references()
        self._update_sample_positions_summary()

    def _build_global_spectrum(self) -> GlobalSpectrumSettings:
        d4, d5 = self._spectrum_offset
        xrd = self._spectrum_xrd_reference or (None, None)
        target = self._spectrum_target_reference or (None, None)
        return GlobalSpectrumSettings(
            base_url=online_spectrometer_prefs.get_base_url(),
            api_key=online_spectrometer_prefs.get_api_key(),
            offset_ch4_pulse=d4, offset_ch5_pulse=d5,
            offset_ch3_pulse=(self._ruby_positions.get("Default") or {}).get("ch3"),
            ruby_positions=copy.deepcopy(self._ruby_positions),
            ruby_offset_origin=self._ruby_offset_origin,
            xrd_reference_ch3_pulse=(self._sample_xrd_position or (None, None, None))[0],
            xrd_reference_ch4_pulse=xrd[0], xrd_reference_ch5_pulse=xrd[1],
            spectrum_reference_ch3_pulse=(
                self._sample_xrd_position[0] + self._ruby_positions["Default"]["ch3"]
                if self._sample_xrd_position and "Default" in self._ruby_positions else None
            ),
            spectrum_reference_ch4_pulse=target[0], spectrum_reference_ch5_pulse=target[1],
            exposure_time_s=self._spectrum_exposure_spin.value(),
            filename_suffixes=tuple(key for key, chk in self._spectrum_suffix_chks.items()
                                    if chk.isChecked()),
            r_squared_min=self._spectrum_r2_spin.value(),
            em_gain_max=self._spectrum_em_gain_max_spin.value(),
            max_accumulations=self._spectrum_max_accum_spin.value(),
            search_enabled=self._spectrum_search_chk.isChecked(),
            search_when=self._spectrum_search_when_combo.currentData(),
            search_channels=tuple(ch for ch, (selected, _) in self._spectrum_search_axes.items()
                                  if selected.isChecked()),
            **{f"search_range_ch{ch}_um": width.value()
               for ch, (_, width) in self._spectrum_search_axes.items()},
            fit_function=self._spectrum_fit_combo.currentText(),
            baseline_model=self._spectrum_baseline_combo.currentData(),
            speed=self._spectrum_speed_combo.currentText(),
            settle_ms=self._spectrum_settle_spin.value(),
            pressure_enabled=self._pressure_enable_chk.isChecked(),
            pressure_scale=self._pressure_scale_combo.currentData() or "",
            temperature_correction_enabled=self._temp_correction_chk.isChecked(),
            temperature_scale=self._temp_scale_combo.currentData() or "",
            temperature_source=self._temp_source_combo.currentData() or "lakeshore_b",
            manual_temperature_k=self._manual_temp_spin.value(),
            temperature_max_age_s=self._temp_max_age_spin.value(),
            ruby_references=copy.deepcopy(self._ruby_references),
        )

    def _build_global_camera(self) -> GlobalCameraSettings:
        return GlobalCameraSettings()

    def _make_follow_panel(self) -> QGroupBox:
        group = QGroupBox("Follow Settings")
        form = QFormLayout(group)
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # Interval
        self._follow_interval_spin = _no_wheel(QDoubleSpinBox())
        self._follow_interval_spin.setRange(0.1, 1440.0)
        self._follow_interval_spin.setDecimals(1)
        self._follow_interval_spin.setSingleStep(0.5)
        self._follow_interval_spin.setValue(1.0)
        self._follow_interval_spin.setSuffix(" min")
        form.addRow("Interval:", self._follow_interval_spin)

        # Similarity threshold
        self._follow_similarity_spin = _no_wheel(QDoubleSpinBox())
        self._follow_similarity_spin.setRange(0.0, 1.0)
        self._follow_similarity_spin.setDecimals(2)
        self._follow_similarity_spin.setSingleStep(0.01)
        self._follow_similarity_spin.setValue(0.95)
        self._follow_similarity_spin.setToolTip(
            "Normalized cross-correlation similarity (0–1).\n"
            "1.0 = perfect match with reference."
        )
        form.addRow("Similarity:", self._follow_similarity_spin)

        # Per-step correction limits (Ch4 / Ch5)
        lim_row = QWidget()
        lim_layout = QHBoxLayout(lim_row)
        lim_layout.setContentsMargins(0, 0, 0, 0)
        lim_layout.setSpacing(4)
        self._follow_lim_ch4_spin = _no_wheel(QDoubleSpinBox())
        self._follow_lim_ch4_spin.setRange(0.0, 50.0)
        self._follow_lim_ch4_spin.setDecimals(3)
        self._follow_lim_ch4_spin.setSingleStep(0.010)
        self._follow_lim_ch4_spin.setValue(0.400)
        self._follow_lim_ch4_spin.setToolTip("Max XY correction per step for Ch4 (mm)")
        self._follow_lim_ch5_spin = _no_wheel(QDoubleSpinBox())
        self._follow_lim_ch5_spin.setRange(0.0, 50.0)
        self._follow_lim_ch5_spin.setDecimals(3)
        self._follow_lim_ch5_spin.setSingleStep(0.010)
        self._follow_lim_ch5_spin.setValue(0.400)
        self._follow_lim_ch5_spin.setToolTip("Max XY correction per step for Ch5 (mm)")
        lim_layout.addWidget(QLabel("Ch4:"))
        lim_layout.addWidget(self._follow_lim_ch4_spin)
        lim_layout.addWidget(QLabel("Ch5:"))
        lim_layout.addWidget(self._follow_lim_ch5_spin)
        form.addRow("Limit/step:", lim_row)

        # XY re-correction retries
        self._follow_xy_retries_spin = _no_wheel(QSpinBox())
        self._follow_xy_retries_spin.setRange(1, 10)
        self._follow_xy_retries_spin.setValue(3)
        self._follow_xy_retries_spin.setToolTip(
            "Max number of XY re-corrections after the initial correction."
        )
        form.addRow("XY retries:", self._follow_xy_retries_spin)

        # ── Autofocus sub-section ──────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(sep2)

        af_note = QLabel("Auto-Focus (Ch3) before and after XY correction")
        af_note.setWordWrap(True)
        form.addRow(af_note)

        # AF scan range and steps
        af_range_row = QWidget()
        af_range_layout = QHBoxLayout(af_range_row)
        af_range_layout.setContentsMargins(0, 0, 0, 0)
        af_range_layout.setSpacing(4)
        self._follow_af_range_spin = _no_wheel(QDoubleSpinBox())
        self._follow_af_range_spin.setRange(1.0, 2000.0)
        self._follow_af_range_spin.setDecimals(0)
        self._follow_af_range_spin.setSingleStep(5.0)
        self._follow_af_range_spin.setValue(30.0)
        self._follow_af_range_spin.setSuffix(" µm")
        self._follow_af_steps_spin = _no_wheel(QSpinBox())
        self._follow_af_steps_spin.setRange(2, 100)
        self._follow_af_steps_spin.setValue(16)
        af_range_layout.addWidget(QLabel("±"))
        af_range_layout.addWidget(self._follow_af_range_spin)
        af_range_layout.addWidget(QLabel("N:"))
        af_range_layout.addWidget(self._follow_af_steps_spin)
        form.addRow("AF range:", af_range_row)

        # AF sharpness method
        self._follow_af_method_combo = _no_wheel(QComboBox())
        self._follow_af_method_combo.addItem("Tenengrad", "tenengrad")
        self._follow_af_method_combo.addItem("Laplacian", "laplacian")
        self._follow_af_method_combo.setCurrentIndex(0)
        self._follow_af_method_combo.setToolTip(
            "Sharpness metric:\n"
            "Tenengrad — mean squared Sobel gradient\n"
            "Laplacian — variance of Laplacian (default)"
        )
        form.addRow("Method:", self._follow_af_method_combo)

        # AF frames per position
        self._follow_af_nframes_spin = _no_wheel(QSpinBox())
        self._follow_af_nframes_spin.setRange(1, 50)
        self._follow_af_nframes_spin.setValue(10)
        self._follow_af_nframes_spin.setToolTip(
            "Frames averaged per scan position.\n"
            "Higher values reduce noise but slow the scan."
        )
        form.addRow("Frames/pos:", self._follow_af_nframes_spin)

        # AF speed
        af_speed_row = QWidget()
        af_speed_layout = QHBoxLayout(af_speed_row)
        af_speed_layout.setContentsMargins(0, 0, 0, 0)
        af_speed_layout.setSpacing(4)
        self._follow_af_speed_group = QButtonGroup(group)
        for spd in ("H", "M", "L"):
            rb = QRadioButton(spd)
            self._follow_af_speed_group.addButton(rb)
            af_speed_layout.addWidget(rb)
            if spd == "H":
                rb.setChecked(True)
        af_speed_layout.addStretch()
        form.addRow("Speed:", af_speed_row)

        # AF peak method
        af_peak_row = QWidget()
        af_peak_layout = QHBoxLayout(af_peak_row)
        af_peak_layout.setContentsMargins(0, 0, 0, 0)
        af_peak_layout.setSpacing(4)
        self._follow_af_peak_group = QButtonGroup(group)
        self._follow_af_peak_highest = QRadioButton("Highest")
        self._follow_af_peak_gaussian = QRadioButton("Gaussian")
        self._follow_af_peak_group.addButton(self._follow_af_peak_highest)
        self._follow_af_peak_group.addButton(self._follow_af_peak_gaussian)
        self._follow_af_peak_highest.setChecked(True)
        af_peak_layout.addWidget(self._follow_af_peak_highest)
        af_peak_layout.addWidget(self._follow_af_peak_gaussian)
        af_peak_layout.addStretch()
        form.addRow("Peak:", af_peak_row)

        return group

    def _build_global_follow(self) -> GlobalFollowSettings:
        af_speed = next(
            (btn.text() for btn in self._follow_af_speed_group.buttons()
             if btn.isChecked()),
            "H",
        )
        af_peak = (
            "gaussian" if self._follow_af_peak_gaussian.isChecked() else "highest"
        )
        return GlobalFollowSettings(
            reference_path=(
                str(self._ref_current_path) if self._ref_current_path is not None else None
            ),
            tracking_method=self._follow_method_combo.currentData(),
            gasket_ellipse=copy.deepcopy(self._gasket_ellipse),
            gasket_sample_point=copy.deepcopy(self._gasket_sample_point),
            gasket_reference_digest=self._gasket_reference_digest,
            gasket_tolerance_um=(None if self._legacy_gasket_tolerance_px is not None
                                 else self._gasket_tolerance_spin.value()),
            gasket_tolerance_px=self._legacy_gasket_tolerance_px,
            gasket_average_frames=self._gasket_average_frames_spin.value(),
            gasket_allow_uniform_scale=self._gasket_allow_scale_chk.isChecked(),
            interval_s=self._follow_interval_spin.value() * 60.0,
            similarity_threshold=self._follow_similarity_spin.value(),
            max_correction_ch4_um=self._follow_lim_ch4_spin.value() * 1000.0,
            max_correction_ch5_um=self._follow_lim_ch5_spin.value() * 1000.0,
            xy_max_retries=self._follow_xy_retries_spin.value(),
            autofocus_range_um=self._follow_af_range_spin.value(),
            autofocus_steps=self._follow_af_steps_spin.value(),
            autofocus_method=self._follow_af_method_combo.currentData(),
            autofocus_n_frames=self._follow_af_nframes_spin.value(),
            autofocus_speed=af_speed,
            autofocus_peak_method=af_peak,
        )

    def _make_xrd_panel(self) -> QGroupBox:
        group = QGroupBox("XRD Settings")
        form = QFormLayout(group)
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # Exposure
        self._xrd_exp_spin = _no_wheel(QSpinBox())
        self._xrd_exp_spin.setRange(1, 60000)
        self._xrd_exp_spin.setSuffix(" ms")
        self._xrd_exp_spin.setValue(1000)
        form.addRow("Exposure:", self._xrd_exp_spin)

        self._xrd_prefix_edit = QLineEdit("scan")
        self._xrd_prefix_edit.setToolTip(
            "Default prefix for saved XRD images. A step can override it."
        )
        form.addRow("File prefix:", self._xrd_prefix_edit)

        # Filename suffixes (several may be combined; appended in this order)
        suffix_row = QGridLayout()
        suffix_row.setContentsMargins(0, 0, 0, 0)
        suffix_row.setHorizontalSpacing(8)
        self._xrd_suffix_chks = {}
        suffix_tooltips = {
            "pressure_gas": "PACE5000 gas pressure, e.g. Pgas_0.80MPa. "
                            "Written only while PACE5000 is ON and connected.",
            "temperature_set": "LakeShore 335 setpoint, e.g. Tset_200.00K. "
                               "Written only while LakeShore 335 is ON and connected.",
            "temperature_b": "LakeShore 335 Ch B temperature, e.g. TB_200.30K. "
                             "Written only while LakeShore 335 is ON and connected.",
        }
        for index, (key, label) in enumerate((
            ("pressure_gas", "Pgas"),
            ("temperature_set", "Tset"),
            ("temperature_b", "TB"),
            ("datetime_full", "YYYY-MM-DD-HH-MM-SS"),
            ("datetime_time", "HH-MM-SS"),
            ("index", "index"),
        )):
            checkbox = QCheckBox(label)
            if key in suffix_tooltips:
                checkbox.setToolTip(suffix_tooltips[key])
            checkbox.setChecked(key in GlobalXrdSettings.filename_suffixes)
            self._xrd_suffix_chks[key] = checkbox
            suffix_row.addWidget(checkbox, index // 2, index % 2)
        suffix_row.setColumnStretch(2, 1)
        suffix_widget = QWidget()
        suffix_widget.setLayout(suffix_row)
        suffix_widget.setToolTip(
            "Suffixes appended after the prefix, in this order, e.g. "
            "prefix_Pgas_0.80MPa_Tset_200.00K_TB_200.30K_<other suffixes>_2x2.tif. "
            "Pgas / Tset / TB are written only while PACE5000 / LakeShore 335 "
            "is ON. index is a 4-digit sequential number. "
            "The binning (_1x1 / _2x2) is always appended last."
        )
        form.addRow(QLabel("Suffix:"))
        form.addRow(suffix_widget)

        # Flip
        flip_row = QHBoxLayout()
        self._xrd_flip_v_chk = QCheckBox("Vertical")
        self._xrd_flip_v_chk.setChecked(True)
        self._xrd_flip_h_chk = QCheckBox("Horizontal")
        self._xrd_flip_h_chk.setChecked(False)
        flip_row.addWidget(self._xrd_flip_v_chk)
        flip_row.addWidget(self._xrd_flip_h_chk)
        flip_row.addStretch()
        form.addRow("Flip:", flip_row)

        # Dark correction
        self._xrd_dark_chk = QCheckBox("Enable dark correction")
        self._xrd_dark_chk.setChecked(False)
        form.addRow("", self._xrd_dark_chk)

        dark_row = QHBoxLayout()
        self._xrd_dark_edit = QLineEdit()
        self._xrd_dark_edit.setPlaceholderText("dark .tif file")
        dark_row.addWidget(self._xrd_dark_edit, stretch=1)
        btn_browse_dark = QPushButton("…")
        btn_browse_dark.setFixedWidth(28)
        btn_browse_dark.clicked.connect(self._on_browse_xrd_dark)
        dark_row.addWidget(btn_browse_dark)
        form.addRow("Dark file:", dark_row)

        def _sync_dark_controls(enabled: bool) -> None:
            for widget in (self._xrd_dark_edit, btn_browse_dark,
                           form.labelForField(dark_row)):
                widget.setEnabled(enabled)

        self._xrd_dark_chk.toggled.connect(_sync_dark_controls)

        # Defect correction
        self._xrd_defect_chk = QCheckBox("Enable defect correction")
        self._xrd_defect_chk.setChecked(True)
        form.addRow("", self._xrd_defect_chk)
        defect_chk_row = QHBoxLayout()
        self._xrd_defect_mode_combo = _no_wheel(QComboBox())
        self._xrd_defect_mode_combo.addItem("Median", "median")
        self._xrd_defect_mode_combo.addItem("Replace with -1", "neg1")
        defect_chk_row.addWidget(self._xrd_defect_mode_combo)
        self._xrd_defect_kernel_combo = _no_wheel(QComboBox())
        self._xrd_defect_kernel_combo.addItems(["3×3", "4×4", "5×5", "6×6"])
        defect_chk_row.addWidget(self._xrd_defect_kernel_combo)
        def _sync_defect_controls() -> None:
            enabled = self._xrd_defect_chk.isChecked()
            self._xrd_defect_mode_combo.setEnabled(enabled)
            for widget in (self._xrd_defect_edit, btn_browse_defect,
                           form.labelForField(defect_row)):
                widget.setEnabled(enabled)
            self._xrd_defect_kernel_combo.setEnabled(
                enabled and self._xrd_defect_mode_combo.currentData() == "median"
            )

        self._xrd_defect_chk.toggled.connect(lambda _: _sync_defect_controls())
        self._xrd_defect_mode_combo.currentIndexChanged.connect(
            lambda _: _sync_defect_controls()
        )
        defect_chk_row.addStretch()
        form.addRow("", defect_chk_row)

        defect_row = QHBoxLayout()
        self._xrd_defect_edit = QLineEdit()
        self._xrd_defect_edit.setPlaceholderText("XFPCAP01 defect .txt file")
        defect_row.addWidget(self._xrd_defect_edit, stretch=1)
        btn_browse_defect = QPushButton("…")
        btn_browse_defect.setFixedWidth(28)
        btn_browse_defect.clicked.connect(self._on_browse_xrd_defect)
        defect_row.addWidget(btn_browse_defect)
        form.addRow("Defect file:", defect_row)
        _sync_dark_controls(self._xrd_dark_chk.isChecked())
        _sync_defect_controls()

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(sep)

        # Oscillation toggle
        self._xrd_osc_chk = QCheckBox("Oscillate sample stage\nduring measurement (Ch11)")
        self._xrd_osc_chk.setChecked(False)
        form.addRow(self._xrd_osc_chk)

        # Pos A / Pos B
        self._xrd_osc_pos_a_spin = _no_wheel(QDoubleSpinBox())
        self._xrd_osc_pos_a_spin.setRange(-180.0, 180.0)
        self._xrd_osc_pos_a_spin.setValue(-5.0)
        self._xrd_osc_pos_a_spin.setSuffix("°")
        self._xrd_osc_pos_a_spin.setDecimals(2)
        self._xrd_osc_pos_b_spin = _no_wheel(QDoubleSpinBox())
        self._xrd_osc_pos_b_spin.setRange(-180.0, 180.0)
        self._xrd_osc_pos_b_spin.setValue(20.0)
        self._xrd_osc_pos_b_spin.setSuffix("°")
        self._xrd_osc_pos_b_spin.setDecimals(2)
        form.addRow("Pos A (deg):", self._xrd_osc_pos_a_spin)
        form.addRow("Pos B (deg):", self._xrd_osc_pos_b_spin)

        # Dwell and speed
        self._xrd_osc_dwell_spin = _no_wheel(QSpinBox())
        self._xrd_osc_dwell_spin.setRange(0, 60000)
        self._xrd_osc_dwell_spin.setValue(0)
        self._xrd_osc_dwell_spin.setSuffix(" ms")
        self._xrd_osc_dwell_spin.setFixedWidth(80)
        form.addRow("Dwell:", self._xrd_osc_dwell_spin)
        osc_speed_row = QHBoxLayout()
        osc_speed_row.setSpacing(6)
        self._xrd_osc_speed_group = QButtonGroup(group)
        for spd in ("H", "M", "L"):
            rb = QRadioButton(spd)
            self._xrd_osc_speed_group.addButton(rb)
            osc_speed_row.addWidget(rb)
            if spd == "M":
                rb.setChecked(True)
        osc_speed_row.addStretch()
        form.addRow("Speed:", osc_speed_row)

        # Show the oscillation parameters only while oscillation is on
        def _toggle_osc(checked: bool) -> None:
            for row in (
                self._xrd_osc_pos_a_spin, self._xrd_osc_pos_b_spin,
                self._xrd_osc_dwell_spin, osc_speed_row,
            ):
                form.setRowVisible(row, checked)

        _toggle_osc(False)
        self._xrd_osc_chk.toggled.connect(_toggle_osc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        form.addRow(sep)
        self._xrd_1d_chk = QCheckBox("Instant 1D reduction (XRD)")
        form.addRow("", self._xrd_1d_chk)
        self._xrd_1d_controls = QWidget()
        one_d = QFormLayout(self._xrd_1d_controls)
        one_d.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self._xrd_1d_poni_status = QLabel()
        self._xrd_1d_poni_status.setWordWrap(True)
        one_d.addRow("Calibration:", self._xrd_1d_poni_status)
        self._xrd_1d_calibration_btn = QPushButton("XRD analysis settings…")
        self._xrd_1d_calibration_btn.setEnabled(
            self._main_window is not None and hasattr(self._main_window, "_on_open_settings")
        )
        self._xrd_1d_calibration_btn.clicked.connect(self._open_detector_calibration)
        one_d.addRow(self._xrd_1d_calibration_btn)
        self._xrd_1d_binwidth_spin = _no_wheel(QDoubleSpinBox())
        self._xrd_1d_binwidth_spin.setRange(0.001, 1.0)
        self._xrd_1d_binwidth_spin.setDecimals(3)
        self._xrd_1d_binwidth_spin.setSingleStep(0.001)
        self._xrd_1d_binwidth_spin.setValue(0.005)
        one_d.addRow(QLabel("Angular resolution\n(deg/bin):"), self._xrd_1d_binwidth_spin)
        for name, label in (("min", "Min 2θ (°):"), ("max", "Max 2θ (°):")):
            spin = _no_wheel(QDoubleSpinBox())
            spin.setRange(-1.0, 180.0)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSpecialValueText("Auto")
            spin.setValue(-1.0)
            setattr(self, f"_xrd_1d_{name}_spin", spin)
            one_d.addRow(label, spin)
        self._xrd_1d_find_spots_chk = QCheckBox("Find spots before 1D integration")
        self._xrd_1d_find_spots_chk.setToolTip(
            "Spot threshold is set under Settings → XRD analysis."
        )
        one_d.addRow(self._xrd_1d_find_spots_chk)
        self._xrd_1d_autosave_chk = QCheckBox("Auto-save 1D data")
        one_d.addRow(self._xrd_1d_autosave_chk)
        format_widget = QWidget()
        format_row = QGridLayout(format_widget)
        format_row.setContentsMargins(0, 0, 0, 0)
        self._xrd_1d_format_chks = {}
        for index, (key, label) in enumerate((
            ("csv", "CSV"), ("tsv", "TSV"),
            ("gsas", "GSAS (.gsa)"), ("igor", "Z-Rietveld (.histogramIgor)"),
        )):
            checkbox = QCheckBox(label)
            self._xrd_1d_format_chks[key] = checkbox
            format_row.addWidget(checkbox, index, 0)
        format_row.setColumnStretch(1, 1)
        one_d.addRow(QLabel("Format:"))
        one_d.addRow(format_widget)
        unit_widget = QWidget()
        unit_row = QGridLayout(unit_widget)
        unit_row.setContentsMargins(0, 0, 0, 0)
        self._xrd_1d_unit_chks = {}
        for index, (key, label) in enumerate((
            ("2theta", "2θ (deg)"), ("q_A", "Q (Å⁻¹)"),
            ("q_nm", "Q (nm⁻¹)"), ("d_A", "d (Å)"), ("d_nm", "d (nm)"),
        )):
            checkbox = QCheckBox(label)
            checkbox.setChecked(key == "2theta")
            self._xrd_1d_unit_chks[key] = checkbox
            unit_row.addWidget(checkbox, index // 2, index % 2)
        unit_row.setColumnStretch(2, 1)
        one_d.addRow(QLabel("X-axis units to save:"))
        one_d.addRow(unit_widget)
        self._xrd_1d_send_pdi_chk = QCheckBox("Send to PDIndexer")
        one_d.addRow(self._xrd_1d_send_pdi_chk)
        self._xrd_1d_transport_combo = _no_wheel(QComboBox())
        one_d.addRow("via:", self._xrd_1d_transport_combo)
        self._xrd_1d_pdi_status = QLabel()
        one_d.addRow(self._xrd_1d_pdi_status)
        self._xrd_1d_chk.toggled.connect(self._xrd_1d_controls.setVisible)
        self._xrd_1d_controls.setVisible(False)
        form.addRow(self._xrd_1d_controls)

        return group

    def _refresh_poni_status(self) -> None:
        state = self._ctx.poni_state
        if state is None or not state.is_calibrated():
            self._xrd_1d_poni_status.setText("No poni calibration registered")
            return
        path = state.poni_path or state.prm_path
        self._xrd_1d_poni_status.setText(
            f"Registered: {path.name}" if path is not None else "In-session calibration registered"
        )

    def _open_detector_calibration(self) -> None:
        if self._main_window is not None:
            self._main_window._on_open_settings()

    def _refresh_pdi_controls(self) -> None:
        combo = self._xrd_1d_transport_combo
        current = combo.currentData()
        combo.clear()
        if self._pdi_transport_available(Transport.CLIPBOARD):
            combo.addItem("Clipboard", Transport.CLIPBOARD)
        if self._pdi_transport_available(Transport.WATCH_FOLDER):
            combo.addItem(".pdi folder", Transport.WATCH_FOLDER)
        if current is not None and combo.findData(current) >= 0:
            combo.setCurrentIndex(combo.findData(current))
        combo.setEnabled(combo.count() > 0)

    def _pdi_transport_available(self, transport: Transport | None) -> bool:
        return bool(
            transport is Transport.CLIPBOARD and self._pdi_service.clipboard_available()
            or transport is Transport.WATCH_FOLDER and self._pdi_service.watch_folder_configured()
        )

    def _on_browse_xrd_dark(self) -> None:
        s = self._get_settings()
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Dark Image",
            s.get("last_xrd_dark_dir", ""),
            "TIFF images (*.tif *.tiff);;All files (*)",
        )
        if path:
            self._xrd_dark_edit.setText(path)
            self._set_setting("last_xrd_dark_dir", str(Path(path).parent))

    def _on_browse_xrd_defect(self) -> None:
        s = self._get_settings()
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Defect File",
            s.get("last_xrd_defect_dir", ""),
            "Text files (*.txt);;All files (*)",
        )
        if path:
            self._xrd_defect_edit.setText(path)
            self._set_setting("last_xrd_defect_dir", str(Path(path).parent))

    def _build_global_xrd(self) -> GlobalXrdSettings:
        kernel_str = self._xrd_defect_kernel_combo.currentText()  # "3×3"
        osc_speed = next(
            (btn.text() for btn in self._xrd_osc_speed_group.buttons() if btn.isChecked()),
            "M",
        )
        return GlobalXrdSettings(
            exposure_ms=self._xrd_exp_spin.value(),
            prefix=self._xrd_prefix_edit.text().strip() or "scan",
            filename_suffixes=tuple(key for key, chk in self._xrd_suffix_chks.items()
                                    if chk.isChecked()),
            dark_file=self._xrd_dark_edit.text().strip() or None,
            dark_enabled=self._xrd_dark_chk.isChecked(),
            defect_file=self._xrd_defect_edit.text().strip() or None,
            defect_enabled=self._xrd_defect_chk.isChecked(),
            defect_mode=self._xrd_defect_mode_combo.currentData(),
            defect_kernel=int(kernel_str[0]),
            flip_v=self._xrd_flip_v_chk.isChecked(),
            flip_h=self._xrd_flip_h_chk.isChecked(),
            oscillate=self._xrd_osc_chk.isChecked(),
            osc_pos_a_deg=self._xrd_osc_pos_a_spin.value(),
            osc_pos_b_deg=self._xrd_osc_pos_b_spin.value(),
            osc_dwell_ms=self._xrd_osc_dwell_spin.value(),
            osc_speed=osc_speed,
            instant1d_enabled=self._xrd_1d_chk.isChecked(),
            instant1d_bin_width_deg=self._xrd_1d_binwidth_spin.value(),
            instant1d_find_spots_before_1d=self._xrd_1d_find_spots_chk.isChecked(),
            instant1d_min_2theta=(None if self._xrd_1d_min_spin.value() == -1
                                  else self._xrd_1d_min_spin.value()),
            instant1d_max_2theta=(None if self._xrd_1d_max_spin.value() == -1
                                  else self._xrd_1d_max_spin.value()),
            instant1d_autosave=self._xrd_1d_autosave_chk.isChecked(),
            instant1d_fmt_csv=self._xrd_1d_format_chks["csv"].isChecked(),
            instant1d_fmt_tsv=self._xrd_1d_format_chks["tsv"].isChecked(),
            instant1d_fmt_gsas=self._xrd_1d_format_chks["gsas"].isChecked(),
            instant1d_fmt_igor=self._xrd_1d_format_chks["igor"].isChecked(),
            instant1d_units=tuple(key for key, chk in self._xrd_1d_unit_chks.items()
                                  if chk.isChecked()),
            instant1d_send_pdi=self._xrd_1d_send_pdi_chk.isChecked(),
            instant1d_transport=(self._xrd_1d_transport_combo.currentData() or Transport.CLIPBOARD).value,
        )

    def _make_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        self._tabs = tabs

        # Tab 0 — Visual (TimelineWidget + validate bar)
        visual_container = QWidget()
        visual_layout = QVBoxLayout(visual_container)
        visual_layout.setContentsMargins(0, 4, 0, 0)
        visual_layout.setSpacing(4)

        validate_bar = QHBoxLayout()
        self._btn_validate_visual = QPushButton("Validate")
        self._btn_validate_visual.setToolTip(
            "Run full validation and enable Run if no errors. Ruby steps take one FRP test spectrum."
        )
        self._btn_validate_visual.clicked.connect(self._on_validate_visual)
        validate_bar.addWidget(self._btn_validate_visual)
        self._validate_visual_status = QLabel("Not validated — click Validate to enable Run")
        self._validate_visual_status.setStyleSheet("color: gray;")
        validate_bar.addWidget(self._validate_visual_status, stretch=1)
        visual_layout.addLayout(validate_bar)

        self._timeline = TimelineWidget()
        self._timeline.spectrum_position_names = self._spectrum_position_names
        self._timeline.global_positions = self._build_global_positions
        self._timeline.sequence_changed.connect(self._on_timeline_changed)
        self._timeline.set_sequence(self._sequence)
        visual_layout.addWidget(self._timeline, stretch=1)

        visual_bottom_bar = QHBoxLayout()
        visual_bottom_bar.addStretch()
        self._btn_clear_visual = QPushButton("Clear All")
        self._btn_clear_visual.setToolTip("Remove all sequence steps and return to the initial state")
        self._btn_clear_visual.clicked.connect(self._on_clear_all)
        visual_bottom_bar.addWidget(self._btn_clear_visual)
        visual_layout.addLayout(visual_bottom_bar)

        tabs.addTab(visual_container, "Visual")

        # Tab 1 — Script (DslEditor)
        script_container = QWidget()
        script_layout = QVBoxLayout(script_container)
        script_layout.setContentsMargins(0, 0, 0, 0)
        script_layout.setSpacing(4)

        self._dsl_editor = DslEditor()
        self._dsl_editor.sequence_changed.connect(self._on_dsl_converted)
        self._dsl_editor.validation_result.connect(self._show_validation_result)
        # A user edit to the Script tab text invalidates any certificate
        # earned for the previously-displayed text — otherwise Run stays
        # enabled and executes self._sequence (the last successfully
        # converted/validated Sequence), which silently diverges from
        # whatever the user has since typed but never re-validated
        # (REORGANISATION_PLAN.md §7 Phase 8).
        self._dsl_editor.text_edited.connect(self._reset_validation)
        script_layout.addWidget(self._dsl_editor, stretch=1)

        script_bottom_bar = QHBoxLayout()
        script_bottom_bar.addStretch()
        self._btn_clear_script = QPushButton("Clear All")
        self._btn_clear_script.setToolTip("Remove all sequence steps and return to the initial state")
        self._btn_clear_script.clicked.connect(self._on_clear_all)
        script_bottom_bar.addWidget(self._btn_clear_script)
        script_layout.addLayout(script_bottom_bar)

        tabs.addTab(script_container, "Script")

        # Tab 2 — AI Assist (LlmPanel)
        self._llm_panel = LlmPanel(
            get_dsl_fn=self._dsl_editor.get_text,
            parent=self,
        )
        self._llm_panel.sequence_applied.connect(self._on_ai_sequence_applied)
        # Keep LlmPanel wired for future use without adding it to the GUI.

        # Auto-populate Script tab when user switches to it
        tabs.currentChanged.connect(self._on_tab_changed)

        return tabs

    # ── Reference Image ────────────────────────────────────────────────────

    def _apply_ref_frame(self, frame: np.ndarray, display_name: str) -> None:
        self._captured_frame = frame
        ts = datetime.now().strftime("%H:%M:%S")
        self._ref_status_label.setText(f"✓ {display_name} ({ts})")
        self._ref_status_label.setStyleSheet("color: green;")
        self._btn_preview.setEnabled(True)

    def _interactive_camera_window(self):
        if self._main_window is None:
            return None
        try:
            from apps.interactive_camera.interactive_camera import MainWindow as ICW
        except ImportError:
            return None
        for window in self._main_window._open_windows.values():
            if isinstance(window, ICW):
                return window
        return None

    def _borrow_camera_frame(self) -> np.ndarray | None:
        """Return current_frame from InteractiveCameraWindow if it is open."""
        window = self._interactive_camera_window()
        if window is not None:
            f = getattr(window, "current_frame", None)
            if f is not None:
                return f.copy()
        return None

    def _on_load_ref_file(self) -> None:
        if self._runner is not None and self._runner.isRunning():
            QMessageBox.warning(self, "Sample Positions", "Wait for the sequence to finish before changing the reference image.")
            return
        s = self._get_settings()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Reference Photo",
            s.get("last_ref_dir", ""),
            "PNG Image (*.png);;JPEG Image (*.jpg)",
        )
        if not path:
            return
        self._set_setting("last_ref_dir", str(Path(path).parent))
        try:
            import cv2
            frame = cv2.imread(path)
            if frame is None:
                raise ValueError("cv2.imread returned None")
        except Exception as e:
            QMessageBox.critical(self, "Load Failed",
                                 f"Could not load reference image:\n{e}")
            return
        self._ref_current_path = Path(path)
        self._set_setting("ref_save_path", str(self._ref_current_path))
        self._apply_ref_frame(frame, Path(path).name)
        self._clear_gasket_outline()
        # A file selected from disk does not carry a trustworthy capture-time
        # stage coordinate. Do not associate the current position with it.
        self._clear_sample_positions()
        self._save_spectrum_settings()
        # reference_path has no widget signal of its own, so invalidate explicitly.
        self._reset_validation()

    def _on_preview_ref(self) -> None:
        if self._captured_frame is None:
            return
        try:
            import cv2
        except ImportError:
            QMessageBox.critical(self, "Error", "opencv-python is not installed.")
            return

        frame = self._captured_frame
        if frame.dtype != np.uint8:
            frame = np.clip(frame.astype(np.float64), 0, 255).astype(np.uint8)

        if len(frame.shape) == 3 and frame.shape[2] >= 3:
            rgb = np.ascontiguousarray(cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2RGB))
            h, w, _ = rgb.shape
            img = QImage(rgb.tobytes(), w, h, rgb.strides[0], QImage.Format.Format_RGB888)
        else:
            gray = np.ascontiguousarray(
                frame if len(frame.shape) == 2 else frame[:, :, 0]
            )
            h, w = gray.shape
            img = QImage(gray.tobytes(), w, h, gray.strides[0],
                         QImage.Format.Format_Grayscale8)

        pixmap = QPixmap.fromImage(img).scaled(
            640, 480,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("Reference Image Preview")
        layout = QVBoxLayout(dlg)
        lbl = QLabel()
        lbl.setPixmap(pixmap)
        layout.addWidget(lbl)
        dlg.adjustSize()
        dlg.exec()

    def _clear_gasket_outline(self) -> None:
        self._gasket_ellipse = None
        self._gasket_sample_point = None
        self._gasket_reference_digest = None
        self._monitor_panel.configure_gasket_overlay(None, None)
        self._save_follow_settings()

    def _on_follow_method_changed(self) -> None:
        selected = self._follow_method_combo.currentData() == "gasket_beta"
        running = self._runner is not None and self._runner.isRunning()
        self._gasket_tolerance_spin.setEnabled(selected and not running)
        self._tracking_form.setRowVisible(self._gasket_tolerance_spin, selected)
        self._tracking_form.setRowVisible(self._gasket_average_frames_spin, selected)
        self._gasket_average_frames_spin.setEnabled(selected and not running)
        self._tracking_form.setRowVisible(self._gasket_allow_scale_chk, selected)
        self._gasket_allow_scale_chk.setEnabled(selected and not running)
        self._follow_similarity_spin.setEnabled(not selected and not running)
        if not hasattr(self, "_monitor_panel"):
            return
        if selected and self._ref_current_path is not None and self._gasket_ellipse:
            try:
                digest = reference_digest(self._ref_current_path)
                if digest == self._gasket_reference_digest:
                    self._monitor_panel.configure_gasket_overlay(
                        str(self._ref_current_path), self._gasket_ellipse,
                        self._gasket_sample_point,
                        allow_uniform_scale=self._gasket_allow_scale_chk.isChecked(),
                    )
                    return
            except OSError:
                pass
        self._monitor_panel.configure_gasket_overlay(None, None)

    def _on_gasket_tolerance_changed(self) -> None:
        self._legacy_gasket_tolerance_px = None
        self._gasket_tolerance_spin.setSpecialValueText("Enter µm limit")
        self._gasket_tolerance_spin.setToolTip(
            "Maximum gasket-centre offset after correction. Each follow attempt corrects its measured shift down to whole Ch4/Ch5 pulses."
        )

    # ── Run / Stop / Save / Load ───────────────────────────────────────────

    def _on_run(self) -> None:
        if self._shutter_restore_busy or self._finishing_runner is not None:
            return
        # Build global limits from UI (safeguard: None values block run)
        global_limits = self._build_global_limits()
        if global_limits is not None and not global_limits.is_fully_configured():
            QMessageBox.critical(
                self, "Limit Not Configured",
                "All six global limit values (Ch3/4/5 ± mm) must be set before running.\n"
                "A value of 0.0 locks the channel in that direction.",
            )
            return

        global_xrd = self._build_global_xrd()
        global_shutter = self._build_global_shutter()
        global_follow = self._build_global_follow()
        global_camera = self._build_global_camera()
        global_spectrum = self._build_global_spectrum()
        global_positions = self._build_global_positions()
        result = validation_service.revalidate_for_run(
            self._sequence, self._ctx, global_limits, global_xrd, global_follow,
            global_camera, certificate=self._certificate,
            global_spectrum=global_spectrum,
            global_positions=global_positions,
            global_shutter=global_shutter,
        )

        def _fmt(d):
            return _line_prefix(d) + d.message

        if not result.ok:
            msg = "Cannot run — the following errors were found:\n\n"
            msg += "\n".join(
                f"• {_fmt(d)}" for d in result.diagnostics if d.severity is Severity.ERROR
            )
            QMessageBox.critical(self, "Validation Errors", msg)
            self._reset_validation()
            return

        if result.warnings:
            msg = "Warnings:\n\n"
            msg += "\n".join(
                f"• {_fmt(d)}" for d in result.diagnostics if d.severity is Severity.WARNING
            )
            msg += "\n\nContinue anyway?"
            if QMessageBox.question(
                self, "Validation Warnings", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return

        if global_xrd.instant1d_enabled and global_xrd.instant1d_send_pdi:
            transport = self._xrd_1d_transport_combo.currentData()
            if not self._pdi_transport_available(transport):
                QMessageBox.critical(self, "Cannot start sequence",
                                     "PDIndexer transport is unavailable; select an available transport or disable sending.")
                return

        try:
            sequence_snapshot = self._sequence_with_current_settings()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot start sequence",
                                 f"Could not prepare the sequence file:\n{exc}")
            return

        if self._main_window is not None:
            try:
                self._closed_btns = self._main_window.close_all_sub_windows()
            except RuntimeError as exc:
                QMessageBox.warning(self, "Cannot start sequence", str(exc))
                return

        self._last_step_index = None
        self._last_step_description = ""
        log_path, log_devices = self._build_log_config()
        data_dir = self._build_data_dir()
        self._timeline.clear_run_progress()
        self._runner = SequenceRunner(
            self._sequence, self._ctx, global_limits,
            global_xrd=global_xrd,
            global_shutter=global_shutter,
            global_follow=global_follow,
            global_camera=global_camera,
            global_spectrum=global_spectrum,
            global_positions=global_positions,
            log_path=log_path,
            log_devices=log_devices,
            data_dir=data_dir,
            sequence_snapshot=sequence_snapshot,
        )
        self._finishing_runner = self._runner
        # The table becomes the run log: loops unrolled, one row per execution.
        self._timeline.show_run_plan(self._runner.run_plan)
        self._slack_call(lambda slack: slack.status_board.begin_run(
            self._sequence.name,
            [PlannedStep(step.index, step.display_action().describe(), step.loop_label())
             for step in self._runner.run_plan],
        ))
        self._runner.step_started.connect(self._on_step_started)
        self._runner.step_completed.connect(self._on_step_completed)
        self._runner.step_started_at.connect(self._timeline.record_step_started)
        self._runner.step_finished_at.connect(self._timeline.record_step_finished)
        self._runner.file_saved.connect(self._timeline.record_file_saved)
        self._runner.pdi_profile_ready.connect(self._on_pdi_profile_ready)
        self._runner.progress_updated.connect(self._on_progress)
        self._runner.sequence_completed.connect(self._on_sequence_completed)
        self._runner.sequence_stopped.connect(self._on_sequence_stopped)
        self._runner.sequence_condition_stopped.connect(self._on_sequence_condition_stopped)
        self._runner.error_occurred.connect(self._on_error_occurred)
        self._runner.shutter_restore_available.connect(self._on_shutter_restore_available)
        self._runner.finished.connect(self._on_runner_finished)

        self._btn_run.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_emergency_stop.setEnabled(True)
        self._btn_save.setEnabled(False)
        self._btn_load.setEnabled(False)
        self._follow_method_combo.setEnabled(False)
        self._gasket_tolerance_spin.setEnabled(False)
        self._gasket_average_frames_spin.setEnabled(False)
        self._gasket_allow_scale_chk.setEnabled(False)
        self._follow_similarity_spin.setEnabled(False)
        self._timeline.set_editing_enabled(False)
        self._dsl_editor.setEnabled(False)
        self._btn_clear_visual.setEnabled(False)
        self._btn_validate_visual.setEnabled(False)
        self._set_status("Running…", "blue")
        self._set_run_banner("Sequence Running", "#1565c0")
        self._elapsed_clock.start()
        self._update_elapsed_time()
        self._elapsed_update_timer.start()
        self._tabs.setCurrentIndex(0)
        self._active_pdi_transport = (
            self._xrd_1d_transport_combo.currentData()
            if global_xrd.instant1d_enabled and global_xrd.instant1d_send_pdi else None
        )
        self._monitor_panel.run_started(self._runner, self._uses_camera(self._sequence.actions),
                                        shutter_managed=global_shutter.only_open_during_xrd,
                                        uses_xrd=self._has_xrd_action(self._sequence.actions),
                                        xrd_1d=global_xrd.instant1d_enabled,
                                        xrd_ai=self._runner._poni_ai,
                                        xrd_min_2theta=global_xrd.instant1d_min_2theta,
                                        xrd_max_2theta=global_xrd.instant1d_max_2theta)
        self._runner.start()

    @pyqtSlot(object)
    def _on_pdi_profile_ready(self, profile) -> None:
        if self._active_pdi_transport is None:
            return
        try:
            self._pdi_service.send([profile], trigger=Trigger.SEQUENCE,
                                   transport=self._active_pdi_transport)
            self._xrd_1d_pdi_status.setText("PDIndexer profile queued")
        except Exception as exc:
            self._xrd_1d_pdi_status.setText(f"PDIndexer send failed: {exc}")

    def _update_elapsed_time(self) -> None:
        seconds = self._elapsed_clock.elapsed() // 1000
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self._elapsed_label.setText(f"Elapsed: {hours:02d}:{minutes:02d}:{seconds:02d}")

    def _update_current_time(self) -> None:
        self._current_time_label.setText(datetime.now().strftime("Time: %H:%M:%S"))

    def _finish_elapsed_time(self) -> None:
        self._elapsed_update_timer.stop()
        self._update_elapsed_time()

    def _on_stop(self) -> None:
        if self._runner is not None:
            self._runner.request_stop()
        self._set_status("Stop requested…", "orange")
        self._set_run_banner("STOP REQUESTED — STILL RUNNING", "#b45309")

    def _on_emergency_stop(self) -> None:
        if self._runner is not None:
            self._runner.request_emergency_stop()
        self._set_status("Emergency stop requested…", "red")
        self._set_run_banner("EMERGENCY STOP REQUESTED — STILL RUNNING", "#b91c1c")

    def _refresh_slack_checkbox(self) -> None:
        """Show whether end-of-run Slack notifications are on, or why they can't be."""
        chk = self._slack_chk
        chk.blockSignals(True)
        try:
            if self._slack is None:
                chk.setVisible(False)
                return
            problem = self._slack.setup_problem()
            if problem:
                chk.setEnabled(False)
                chk.setChecked(False)
                chk.setText("Notify on Slack (not set up)")
                chk.setToolTip(f"{problem}\nSet it up under Settings → Slack notifications. "
                               "Sequences run normally without it.")
                return
            chk.setEnabled(True)
            chk.setChecked(self._slack.prefs.enabled)
            channel = self._slack.prefs.channel_name or self._slack.prefs.channel_id
            tip = (f"Post to #{channel} when the sequence completes, ends on its stop "
                   "condition, or stops with an error (not on Stop).")
            if self._slack.debug:
                chk.setText("Notify on Slack (debug: not sent)")
                tip += "\nDebug mode: messages are logged, not sent."
            else:
                chk.setText("Notify on Slack")
            chk.setToolTip(tip)
        finally:
            chk.blockSignals(False)

    def _on_slack_toggled(self, checked: bool) -> None:
        self._slack_call(lambda slack: slack.set_enabled(checked))

    def _slack_call(self, fn) -> None:
        """Run *fn(slack_service)*; Slack problems must never affect a run."""
        if self._slack is None:
            return
        try:
            fn(self._slack)
        except Exception:
            _log.exception("Slack update failed")

    @pyqtSlot()
    def _on_sequence_completed(self) -> None:
        self._slack_call(lambda slack: slack.status_board.finish("completed"))
        self._slack_call(lambda slack: slack.notify_completed())
        self._finish_elapsed_time()
        self._runner = None
        self._run_banner.hide()
        self._set_status("Completed", "green")
        self._set_idle()
        play_current_sound()

    @pyqtSlot(str)
    def _on_sequence_condition_stopped(self, reason: str) -> None:
        """A step's own stop condition ended the run — a successful end."""
        self._slack_call(lambda slack: slack.status_board.finish("condition_stop", reason))
        self._slack_call(lambda slack: slack.notify_condition_stop(reason))
        self._finish_elapsed_time()
        self._runner = None
        self._run_banner.hide()
        self._set_status(f"Ended — stop condition reached: {reason}" if reason
                         else "Ended — stop condition reached", "green")
        self._set_idle()
        play_current_sound()
        QMessageBox.information(
            self, "Sequence Ended",
            (reason or "A step's stop condition was reached.")
            + "\n\nThe remaining steps were not run.",
        )

    @pyqtSlot()
    def _on_sequence_stopped(self) -> None:
        reason = self._runner.stop_reason if self._runner is not None else ""
        # User stops are deliberately not notified on Slack.
        self._slack_call(lambda slack: slack.status_board.finish("stopped", reason))
        self._finish_elapsed_time()
        self._runner = None
        self._run_banner.hide()
        self._set_status(reason or "Stopped — sequence did NOT complete", "orange")
        self._set_idle()
        if self._last_step_index is not None:
            where = f"\n\nLast step started: #{self._last_step_index + 1} — {self._last_step_description}"
        else:
            where = ""
        QMessageBox.warning(
            self, "Sequence Stopped",
            (reason or "The sequence was stopped before all steps finished — it did NOT complete.")
            + where,
        )

    @pyqtSlot(int, str)
    def _on_error_occurred(self, index: int, message: str) -> None:
        runner = self._runner or self._finishing_runner
        if runner is not None and runner.user_stop_requested:
            # An error caused by the user's own Stop / Emergency stop is
            # part of a user stop, which is not notified.
            self._slack_call(lambda slack: slack.status_board.finish("stopped", message))
        else:
            code = runner.last_error_code if runner is not None else ""
            self._slack_call(lambda slack: slack.status_board.finish("error", message))
            # Queued before the blocking dialog below.
            self._slack_call(lambda slack: slack.notify_error(code, message))
        self._finish_elapsed_time()
        self._runner = None
        self._run_banner.hide()
        self._set_status(f"Error at step {index}", "red")
        self._set_idle()
        QMessageBox.critical(
            self, "Sequence Error",
            f"Error at step {index}:\n{message}",
        )

    @pyqtSlot(int, str)
    def _on_step_started(self, index: int, description: str) -> None:
        self._last_step_index = index
        self._last_step_description = description
        self._set_status(f"Step {index + 1}: {description}", "blue")
        self._timeline.highlight_step(index)
        self._slack_call(lambda slack: slack.status_board.step_started(index, description))

    @pyqtSlot(int)
    def _on_step_completed(self, index: int) -> None:
        self._timeline.mark_step_done(index)
        self._slack_call(lambda slack: slack.status_board.step_completed(index))

    @pyqtSlot(str)
    def _on_progress(self, message: str) -> None:
        self._set_status(message, "blue")
        self._slack_call(lambda slack: slack.status_board.progress(message))

    def _on_timeline_changed(self) -> None:
        """Sync _sequence whenever the user edits the timeline."""
        self._sequence = self._timeline.get_sequence()
        self._update_xrd_panel_visibility()
        self._update_follow_panel_visibility()
        self._reset_validation()

    def _on_tab_changed(self, index: int) -> None:
        """When switching to Script tab, auto-populate DSL editor from current sequence.

        When leaving Script for Visual, auto-convert the script if the
        DslEditor's "Automatically convert to Visual when switching tabs"
        checkbox is enabled (the default) — this mirrors clicking
        "Convert to Visual →" by hand.
        """
        if index == 1:
            self._dsl_editor.set_sequence(self._sequence)
        elif (
            index == 0
            and self._last_tab_index == 1
            and self._dsl_editor.auto_convert_enabled()
            and not self._switching_tabs_after_convert
            and self._runner is None
        ):
            self._dsl_editor.convert_to_visual()
        self._last_tab_index = index

    def _on_dsl_converted(self, seq: Sequence) -> None:
        """Handle a successful Convert-to-Visual.

        DslEditor only emits sequence_changed after its own call to the
        `_validate_dsl_text` callback (set via set_validator()) already
        succeeded — that callback already applied `seq` to
        self._sequence/self._timeline and updated the validated/Run state,
        so this handler only needs to switch tabs. Calling
        `_validate_dsl_text` again here would run the full device preflight
        a second time for one click (the exact duplication Phase 7 removes
        — REORGANISATION_PLAN.md §7 Phase 7).

        `_switching_tabs_after_convert` suppresses `_on_tab_changed()`'s own
        auto-convert while this programmatic tab switch is in flight —
        `setCurrentIndex()` emits `currentChanged` (and so calls
        `_on_tab_changed()`) synchronously, before returning here, and that
        handler would otherwise see the same "leaving Script for Visual"
        condition and call `convert_to_visual()` again, running the full
        validator a second time for this one click.
        """
        self._switching_tabs_after_convert = True
        try:
            self._tabs.setCurrentIndex(0)
        finally:
            self._switching_tabs_after_convert = False

    def _on_ai_sequence_applied(self, seq: Sequence) -> None:
        """Handle LlmPanel sequence_applied signal; update timeline and script."""
        self._sequence = seq
        self._timeline.set_sequence(seq)
        self._dsl_editor.set_sequence(seq)
        self._tabs.setCurrentIndex(0)
        self._update_xrd_panel_visibility()
        self._update_follow_panel_visibility()
        self._reset_validation()

    def _on_clear_all(self) -> None:
        """Clear-All handler shared by the Visual and Script tabs.

        Removes every sequence step and resets both views to the initial
        (empty) state.
        """
        if not self._sequence.actions:
            return
        reply = QMessageBox.question(
            self, "Clear All",
            "Remove all sequence steps? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._sequence = Sequence(actions=[], name=self._sequence.name)
        self._timeline.set_sequence(self._sequence)
        self._dsl_editor.set_text("")
        self._update_xrd_panel_visibility()
        self._update_follow_panel_visibility()
        self._reset_validation()

    # ── Validation helpers ─────────────────────────────────────────────────

    def _wire_settings_invalidation(self, panel: QWidget) -> None:
        """Connect every settings widget inside `panel` to _reset_validation()
        so a change made after a successful Validate immediately discards the
        certificate and disables Run (REORGANISATION_PLAN.md §7 Phase 8),
        rather than only being caught later at the Run gate's settings
        fingerprint diff.

        findChildren() is called once per concrete widget type rather than
        with a type tuple — QButtonGroup is a QObject, not a QWidget (it is
        still found here via Qt parent-child ownership, not the widget
        tree), and its change signal is `buttonToggled`, not the per-button
        `toggled` a plain QCheckBox/QRadioButton exposes, so it needs its
        own connect call regardless. Extra signal arguments (the new
        value/index/button) are silently dropped by PyQt when connecting to
        a plain no-argument callable.
        """
        for w in panel.findChildren(QSpinBox):
            w.valueChanged.connect(self._reset_validation)
        for w in panel.findChildren(QDoubleSpinBox):
            w.valueChanged.connect(self._reset_validation)
        for w in panel.findChildren(QLineEdit):
            w.textChanged.connect(self._reset_validation)
        for w in panel.findChildren(QCheckBox):
            w.toggled.connect(self._reset_validation)
        for w in panel.findChildren(QComboBox):
            w.currentIndexChanged.connect(self._reset_validation)
        for w in panel.findChildren(QButtonGroup):
            w.buttonToggled.connect(self._reset_validation)

    def _reset_validation(self) -> None:
        """Mark sequence as unvalidated and disable Run."""
        self._validated = False
        self._certificate = None
        self._btn_run.setEnabled(False)
        self._validate_visual_status.setText("Not validated — click Validate to enable Run")
        self._validate_visual_status.setStyleSheet("color: gray;")

    def _set_validated(self, report: ValidationReport) -> None:
        """Mark sequence as validated and enable Run.

        `report.certificate` (set only on a clean validation pass — see
        validation_service.validate_sequence()) is stored so `_on_run` can
        hand it to `revalidate_for_run()`'s Run gate (REORGANISATION_PLAN.md
        §7 Phase 8). Every current caller only reaches this method with a
        clean, certificate-bearing report, but Run being enabled with no
        certificate to back it would be a worse failure mode than staying
        disabled (Run would then always be rejected at the gate with a
        confusing "not validated" message despite the UI showing Run as
        enabled) — so this is guarded explicitly rather than trusted.
        """
        if report.certificate is None:
            self._reset_validation()
            return
        self._validated = True
        self._certificate = report.certificate
        self._btn_run.setEnabled(True)

    def _on_validate_visual(self) -> None:
        """Validate button handler for the Visual tab."""
        self._timeline.clear_run_progress()
        self._validate_visual_status.setText("Validating, please wait...")
        self._validate_visual_status.setStyleSheet("color: gray;")
        QApplication.processEvents()
        global_limits = self._build_global_limits()
        global_xrd = self._build_global_xrd()
        global_shutter = self._build_global_shutter()
        global_follow = self._build_global_follow()
        global_camera = self._build_global_camera()
        global_spectrum = self._build_global_spectrum()
        global_positions = self._build_global_positions()
        result = validation_service.validate_sequence(
            self._sequence, self._ctx, global_limits, global_xrd, global_follow,
            global_camera, global_spectrum=global_spectrum,
            global_positions=global_positions,
            global_shutter=global_shutter,
        )
        self._show_validation_result(result)
        if result.errors:
            self._validate_visual_status.setText(f"✗ {len(result.errors)} error(s) found — fix before running")
            self._validate_visual_status.setStyleSheet("color: #c62828;")
            self._reset_validation()
        elif result.warnings:
            self._validate_visual_status.setText(f"✓ Passed with {len(result.warnings)} warning(s)")
            self._validate_visual_status.setStyleSheet("color: darkorange;")
            self._set_validated(result)
        else:
            self._validate_visual_status.setText("✓ Validation passed — no errors found")
            self._validate_visual_status.setStyleSheet("color: #2e7d32;")
            self._set_validated(result)

    def _validate_dsl_text(self, text: str):
        """Full validate callback for DslEditor (set via set_validator()).

        Called for every Validate/Convert-to-Visual attempt on the Script
        tab — including a syntax error or an empty script, not just a
        successfully-compiled one (REORGANISATION_PLAN.md §7 Phase 7): runs
        `validation_service.validate_dsl()` (compile + full device
        preflight in one call), updates self._sequence + timeline on
        success, and controls the Run button either way. Returns the
        ValidationReport so DslEditor's Convert-to-Visual can decide
        whether to switch to the Visual tab.
        """
        self._timeline.clear_run_progress()
        global_limits = self._build_global_limits()
        global_xrd = self._build_global_xrd()
        global_shutter = self._build_global_shutter()
        global_follow = self._build_global_follow()
        global_camera = self._build_global_camera()
        global_spectrum = self._build_global_spectrum()
        global_positions = self._build_global_positions()
        result = validation_service.validate_dsl(
            text, self._ctx, global_limits, global_xrd, global_follow,
            global_camera, global_spectrum, global_positions, global_shutter,
        )
        self._show_validation_result(result)
        if result.ok:
            self._sequence = result.sequence
            self._timeline.set_sequence(result.sequence)
            self._update_xrd_panel_visibility()
            self._update_follow_panel_visibility()
            self._validate_visual_status.setText("✓ Validated from Script tab")
            self._validate_visual_status.setStyleSheet("color: #2e7d32;")
            self._set_validated(result)
        else:
            self._validate_visual_status.setText(f"✗ {len(result.errors)} error(s) found — fix before running")
            self._validate_visual_status.setStyleSheet("color: #c62828;")
            self._reset_validation()
        return result

    def _set_idle(self) -> None:
        self._timeline.set_editing_enabled(True)
        self._dsl_editor.setEnabled(True)
        self._btn_clear_visual.setEnabled(True)
        self._btn_validate_visual.setEnabled(True)
        self._btn_run.setEnabled(self._validated and not self._shutter_restore_busy
                                 and self._finishing_runner is None)
        self._btn_stop.setEnabled(False)
        self._btn_emergency_stop.setEnabled(False)
        self._btn_save.setEnabled(True)
        self._btn_load.setEnabled(True)
        self._follow_method_combo.setEnabled(True)
        self._on_follow_method_changed()

    def _do_restore(self) -> None:
        if self._shutter_restore_busy:
            return
        if self._main_window is not None and self._closed_btns:
            self._main_window.restore_sub_windows(self._closed_btns)
        self._closed_btns = []

    @pyqtSlot()
    def _on_runner_finished(self) -> None:
        runner = self._finishing_runner
        if self._active_pdi_transport is not None:
            try:
                self._pdi_service.flush_sequence(self._active_pdi_transport)
            except Exception as exc:
                self._xrd_1d_pdi_status.setText(f"PDIndexer flush failed: {exc}")
            self._active_pdi_transport = None
        self._finishing_runner = None
        self._btn_run.setEnabled(self._validated and not self._shutter_restore_busy)
        # Release the monitor's camera before Interactive Camera is restored.
        self._monitor_panel.run_finished()
        self._do_restore()
        log_dir = runner.log_dir if runner is not None else None
        if log_dir is not None and log_dir.is_dir():
            pdf_path = log_dir / "run_log.pdf"
            try:
                self._timeline.save_run_log_pdf(pdf_path, log_dir.parent.name)
            except Exception as exc:
                _log.exception("Could not save run log PDF to %s", pdf_path)
                QMessageBox.warning(
                    self, "Run log PDF could not be saved",
                    f"Could not save the printable run log to:\n{pdf_path}\n\n{exc}",
                )

    @pyqtSlot(bool)
    def _on_shutter_restore_available(self, initial_inserted: bool) -> None:
        position = "In (X-rays blocked)" if initial_inserted else "Out (beam at sample)"
        answer = QMessageBox.question(
            self, "Restore X-ray shutter?",
            f"The shutter was {position} when the sequence started.\n"
            "It has been set to In. Restore its initial position?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._shutter_restore_busy = True
        self._btn_run.setEnabled(False)

        def restore() -> None:
            shutter = self._ctx.shutter
            error = ""
            try:
                # Another operator may have moved it after the sequence.
                if shutter.get_status() is not True:
                    raise RuntimeError("Shutter changed after the sequence; restore cancelled")
                if not initial_inserted:
                    shutter.set_inserted(False)
                if shutter.get_status() is not initial_inserted:
                    raise RuntimeError("Could not confirm the restored shutter position")
            except Exception as exc:
                error = str(exc)
            finally:
                try:
                    shutter.close()
                except Exception as exc:
                    error = f"{error}; {exc}" if error else str(exc)
                self._shutter_restore_done.emit(error)

        self._shutter_restore_worker = threading.Thread(
            target=restore, name="scheduler-shutter-restore", daemon=True,
        )
        self._shutter_restore_worker.start()

    @pyqtSlot(str)
    def _on_shutter_restore_done(self, error: str) -> None:
        self._shutter_restore_busy = False
        self._shutter_restore_worker = None
        self._btn_run.setEnabled(self._validated and self._runner is None
                                 and self._finishing_runner is None)
        self._do_restore()
        if error:
            QMessageBox.critical(self, "Shutter restoration failed", error)

    def _set_status(self, text: str, color: str = "gray") -> None:
        self._status_label.setText(f"Status: {text}")
        self._status_label.setStyleSheet(f"color: {color};")

    def _set_run_banner(self, text: str, color: str) -> None:
        self._run_banner.setText(text)
        self._run_banner.setStyleSheet(
            "QLabel { color: " + color + "; background-color: white;"
            " border: 4px solid " + color + ";"
            " font-size: 20pt; font-weight: bold; border-radius: 1px; }"
        )
        self._run_banner.show()

    def _sequence_with_current_settings(self) -> Sequence:
        # The validated Sequence must stay untouched: its fingerprint is
        # checked again by revalidate_for_run() at the Run gate.
        sequence_to_save = copy.deepcopy(self._sequence)
        sequence_to_save.global_xrd = self._xrd_ui_to_dict()
        sequence_to_save.global_shutter = self._shutter_ui_to_dict()
        sequence_to_save.global_follow = self._follow_ui_to_dict()
        sequence_to_save.global_spectrum = self._spectrum_ui_to_dict()
        sequence_to_save.global_limits = self._limits_ui_to_dict()
        sequence_to_save.global_positions = self._positions_ui_to_dict()
        return sequence_to_save

    def _on_save(self) -> None:
        s = self._get_settings()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Sequence",
            s.get("last_seq_dir", ""),
            "Sequence JSON (*.json)",
        )
        if not path:
            return
        self._set_setting("last_seq_dir", str(Path(path).parent))
        try:
            self._sequence_with_current_settings().save(path)
            self._set_status(f"Saved: {Path(path).name}", "green")
        except Exception as e:
            QMessageBox.critical(self, "Save Failed",
                                 f"Could not save sequence:\n{e}")

    def _on_load(self) -> None:
        s = self._get_settings()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Sequence",
            s.get("last_seq_dir", ""),
            "Sequence JSON (*.json)",
        )
        if not path:
            return
        self._set_setting("last_seq_dir", str(Path(path).parent))
        try:
            seq = Sequence.load(path)
        except Exception as e:
            QMessageBox.critical(self, "Load Failed",
                                 f"Could not load sequence:\n{e}")
            return

        self._sequence = seq
        self._timeline.set_sequence(seq)
        self._update_xrd_panel_visibility()
        self._update_follow_panel_visibility()
        self._reset_validation()

        if seq.global_xrd is not None:
            self._xrd_dict_to_ui(seq.global_xrd)
        self._shutter_dict_to_ui(seq.global_shutter or {})
        if seq.global_follow is not None:
            self._follow_dict_to_ui(seq.global_follow)
        if seq.global_spectrum is not None:
            self._spectrum_dict_to_ui(seq.global_spectrum)
        if seq.global_positions is not None:
            self._positions_dict_to_ui(seq.global_positions)
        if seq.global_limits is not None:
            gl = seq.global_limits
            msg = (
                "The loaded file contains Global Limits settings:\n\n"
                f"  Ch3:  −{gl.get('ch3_minus_mm', '?')} mm / +{gl.get('ch3_plus_mm', '?')} mm\n"
                f"  Ch4:  −{gl.get('ch4_minus_mm', '?')} mm / +{gl.get('ch4_plus_mm', '?')} mm\n"
                f"  Ch5:  −{gl.get('ch5_minus_mm', '?')} mm / +{gl.get('ch5_plus_mm', '?')} mm\n\n"
                "Apply these limits to current settings?"
            )
            reply = QMessageBox.question(
                self, "Global Limits in File", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._limits_dict_to_ui(gl)

        self._set_status(f"Loaded: {Path(path).name}", "green")

    # ── Settings persistence ───────────────────────────────────────────────

    def _get_settings(self) -> dict:
        try:
            return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _set_setting(self, key: str, value) -> None:
        s = self._get_settings()
        s[key] = value
        try:
            _SETTINGS_PATH.write_text(
                json.dumps(s, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _restore_settings(self) -> None:
        s = self._get_settings()

        # Ref image path
        ref_path = s.get("ref_save_path", str(_DEFAULT_REF_PATH))
        self._ref_current_path = Path(ref_path)
        p = self._ref_current_path
        if p.exists() and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
            try:
                import cv2
                frame = cv2.imread(str(p))
                if frame is not None:
                    self._captured_frame = frame
                    self._ref_status_label.setText(f"✓ {p.name}")
                    self._ref_status_label.setStyleSheet("color: green;")
                    self._btn_preview.setEnabled(True)
            except Exception:
                pass

        xrd_settings = s.get("global_xrd", {})
        xrd_settings = xrd_settings.copy() if isinstance(xrd_settings, dict) else {}
        self._xrd_inherited_defaults.clear()
        for key, value in _radicon_correction_file_defaults().items():
            if (key not in xrd_settings
                    or key in ("dark_file", "defect_file") and not xrd_settings[key]):
                xrd_settings[key] = value
                self._xrd_inherited_defaults[key] = value
        self._xrd_dict_to_ui(xrd_settings)
        self._shutter_dict_to_ui(s.get("global_shutter", {}))
        self._spectrum_dict_to_ui(s.get("global_spectrum", {}))
        self._follow_dict_to_ui(s.get("global_follow", {}))
        self._limits_dict_to_ui(s.get("global_limits", {}))
        self._positions_dict_to_ui(s.get("global_positions", {}))
        self._log_dict_to_ui(s.get("logging", {}))

    # ── Settings dict helpers ──────────────────────────────────────────────

    def _shutter_ui_to_dict(self) -> dict:
        return {"only_open_during_xrd": self._shutter_xrd_only_chk.isChecked()}

    def _shutter_dict_to_ui(self, settings: dict) -> None:
        self._shutter_xrd_only_chk.setChecked(bool(settings.get("only_open_during_xrd", False)))

    def _save_shutter_settings(self) -> None:
        self._set_setting("global_shutter", self._shutter_ui_to_dict())

    def _xrd_ui_to_dict(self) -> dict:
        g = self._build_global_xrd()
        return {
            "exposure_ms":    g.exposure_ms,
            "prefix":         g.prefix,
            "filename_suffixes": list(g.filename_suffixes),
            "flip_v":         g.flip_v,
            "flip_h":         g.flip_h,
            "dark_enabled":   g.dark_enabled,
            "dark_file":      g.dark_file or "",
            "defect_enabled": g.defect_enabled,
            "defect_mode":    g.defect_mode,
            "defect_file":    g.defect_file or "",
            "defect_kernel":  g.defect_kernel,
            "oscillate":      g.oscillate,
            "osc_pos_a_deg":  g.osc_pos_a_deg,
            "osc_pos_b_deg":  g.osc_pos_b_deg,
            "osc_dwell_ms":   g.osc_dwell_ms,
            "osc_speed":      g.osc_speed,
            "instant1d_enabled": g.instant1d_enabled,
            "instant1d_bin_width_deg": g.instant1d_bin_width_deg,
            "instant1d_find_spots_before_1d": g.instant1d_find_spots_before_1d,
            "instant1d_min_2theta": g.instant1d_min_2theta,
            "instant1d_max_2theta": g.instant1d_max_2theta,
            "instant1d_autosave": g.instant1d_autosave,
            "instant1d_fmt_csv": g.instant1d_fmt_csv,
            "instant1d_fmt_tsv": g.instant1d_fmt_tsv,
            "instant1d_fmt_gsas": g.instant1d_fmt_gsas,
            "instant1d_fmt_igor": g.instant1d_fmt_igor,
            "instant1d_units": list(g.instant1d_units),
            "instant1d_send_pdi": g.instant1d_send_pdi,
            "instant1d_transport": g.instant1d_transport,
        }

    def _xrd_dict_to_ui(self, d: dict) -> None:
        self._xrd_exp_spin.setValue(int(d.get("exposure_ms", 1000)))
        self._xrd_prefix_edit.setText(d.get("prefix") or "scan")
        suffixes = d.get("filename_suffixes", list(GlobalXrdSettings.filename_suffixes))
        for key, checkbox in self._xrd_suffix_chks.items():
            checkbox.setChecked(key in suffixes)
        self._xrd_flip_v_chk.setChecked(bool(d.get("flip_v", True)))
        self._xrd_flip_h_chk.setChecked(bool(d.get("flip_h", False)))
        self._xrd_dark_chk.setChecked(bool(d.get("dark_enabled", False)))
        self._xrd_dark_edit.setText(d.get("dark_file", ""))
        self._xrd_defect_chk.setChecked(bool(d.get("defect_enabled", True)))
        mode_index = self._xrd_defect_mode_combo.findData(d.get("defect_mode", "median"))
        self._xrd_defect_mode_combo.setCurrentIndex(max(0, mode_index))
        self._xrd_defect_edit.setText(d.get("defect_file", ""))
        kernel = int(d.get("defect_kernel", 3))
        if 3 <= kernel <= 6:
            self._xrd_defect_kernel_combo.setCurrentIndex(kernel - 3)
        self._xrd_osc_chk.setChecked(bool(d.get("oscillate", False)))
        self._xrd_osc_pos_a_spin.setValue(float(d.get("osc_pos_a_deg", -5.0)))
        self._xrd_osc_pos_b_spin.setValue(float(d.get("osc_pos_b_deg", 20.0)))
        self._xrd_osc_dwell_spin.setValue(int(d.get("osc_dwell_ms", 0)))
        osc_speed = d.get("osc_speed", "M")
        for btn in self._xrd_osc_speed_group.buttons():
            if btn.text() == osc_speed:
                btn.setChecked(True)
                break
        self._xrd_1d_chk.setChecked(bool(d.get("instant1d_enabled", False)))
        self._xrd_1d_binwidth_spin.setValue(float(d.get("instant1d_bin_width_deg", 0.005)))
        self._xrd_1d_find_spots_chk.setChecked(
            d.get("instant1d_find_spots_before_1d", False) is True
        )
        for name in ("min", "max"):
            value = d.get(f"instant1d_{name}_2theta")
            getattr(self, f"_xrd_1d_{name}_spin").setValue(
                -1.0 if value is None else float(value))
        self._xrd_1d_autosave_chk.setChecked(bool(d.get("instant1d_autosave", False)))
        for key, checkbox in self._xrd_1d_format_chks.items():
            checkbox.setChecked(bool(d.get(f"instant1d_fmt_{key}", False)))
        units = d.get("instant1d_units", ["2theta"])
        for key, checkbox in self._xrd_1d_unit_chks.items():
            checkbox.setChecked(key in units)
        self._xrd_1d_send_pdi_chk.setChecked(bool(d.get("instant1d_send_pdi", False)))
        transport = d.get("instant1d_transport", "clipboard")
        for index in range(self._xrd_1d_transport_combo.count()):
            if self._xrd_1d_transport_combo.itemData(index).value == transport:
                self._xrd_1d_transport_combo.setCurrentIndex(index)
                break
        else:
            try:
                unavailable = Transport(transport)
            except (TypeError, ValueError):
                unavailable = None
            if unavailable is not None:
                self._xrd_1d_transport_combo.addItem(
                    f"{unavailable.value} (unavailable)", unavailable,
                )
                self._xrd_1d_transport_combo.setCurrentIndex(
                    self._xrd_1d_transport_combo.count() - 1,
                )

    def _spectrum_ui_to_dict(self) -> dict:
        g = self._build_global_spectrum()
        return {
            "offset_ch4_pulse": g.offset_ch4_pulse,
            "offset_ch5_pulse": g.offset_ch5_pulse,
            "offset_ch3_pulse": g.offset_ch3_pulse,
            "ruby_positions": g.ruby_positions,
            "ruby_offset_origin": g.ruby_offset_origin,
            "xrd_reference_ch3_pulse": g.xrd_reference_ch3_pulse,
            "xrd_reference_ch4_pulse": g.xrd_reference_ch4_pulse,
            "xrd_reference_ch5_pulse": g.xrd_reference_ch5_pulse,
            "spectrum_reference_ch4_pulse": g.spectrum_reference_ch4_pulse,
            "spectrum_reference_ch5_pulse": g.spectrum_reference_ch5_pulse,
            "spectrum_reference_ch3_pulse": g.spectrum_reference_ch3_pulse,
            "exposure_time_s": g.exposure_time_s,
            "filename_suffixes": list(g.filename_suffixes),
            "r_squared_min": g.r_squared_min,
            "em_gain_max": g.em_gain_max,
            "max_accumulations": g.max_accumulations,
            "search_enabled": g.search_enabled,
            "search_when": g.search_when,
            "search_channels": list(g.search_channels),
            **{f"search_range_ch{ch}_um": getattr(g, f"search_range_ch{ch}_um")
               for ch in (3, 4, 5)},
            "fit_function": g.fit_function,
            "baseline_model": g.baseline_model,
            "speed": g.speed,
            "settle_ms": g.settle_ms,
            "timeout_s": g.timeout_s,
            "pressure_enabled": g.pressure_enabled,
            "pressure_scale": g.pressure_scale,
            "temperature_correction_enabled": g.temperature_correction_enabled,
            "temperature_scale": g.temperature_scale,
            "temperature_source": g.temperature_source,
            "manual_temperature_k": g.manual_temperature_k,
            "temperature_max_age_s": g.temperature_max_age_s,
            "ruby_references": g.ruby_references,
        }

    def _spectrum_dict_to_ui(self, d: dict) -> None:
        self._spectrum_exposure_spin.setValue(float(d.get("exposure_time_s", 0.1)))
        suffixes = d.get("filename_suffixes", GlobalSpectrumSettings.filename_suffixes)
        for key, checkbox in self._spectrum_suffix_chks.items():
            checkbox.setChecked(key in suffixes)
        self._spectrum_r2_spin.setValue(float(d.get("r_squared_min", 0.95)))
        self._spectrum_em_gain_max_spin.setValue(int(d.get("em_gain_max", 10)))
        self._spectrum_max_accum_spin.setValue(int(d.get("max_accumulations", 4)))
        self._spectrum_search_chk.setChecked(bool(d.get("search_enabled", False)))
        index = self._spectrum_search_when_combo.findData(d.get("search_when", "always"))
        self._spectrum_search_when_combo.setCurrentIndex(max(0, index))
        selected_channels = d.get("search_channels", (4, 5))
        for ch, (selected, width) in self._spectrum_search_axes.items():
            selected.setChecked(ch in selected_channels)
            width.setValue(float(d.get(f"search_range_ch{ch}_um", 10.0)))
        fit_index = self._spectrum_fit_combo.findText(d.get("fit_function", "Moffat"))
        self._spectrum_fit_combo.setCurrentIndex(max(0, fit_index))
        baseline_index = self._spectrum_baseline_combo.findData(
            d.get("baseline_model", "linear"))
        self._spectrum_baseline_combo.setCurrentIndex(max(0, baseline_index))
        speed = d.get("speed", "M")
        index = self._spectrum_speed_combo.findText(speed)
        if index >= 0:
            self._spectrum_speed_combo.setCurrentIndex(index)
        self._spectrum_settle_spin.setValue(int(d.get("settle_ms", 100)))
        x4, x5 = d.get("xrd_reference_ch4_pulse"), d.get("xrd_reference_ch5_pulse")
        r4, r5 = d.get("spectrum_reference_ch4_pulse"), d.get("spectrum_reference_ch5_pulse")
        self._spectrum_xrd_reference = None if x4 is None or x5 is None else (int(x4), int(x5))
        self._spectrum_target_reference = None if r4 is None or r5 is None else (int(r4), int(r5))
        d4, d5 = d.get("offset_ch4_pulse"), d.get("offset_ch5_pulse")
        self._spectrum_offset = (
            None if d4 is None else int(d4), None if d5 is None else int(d5)
        )
        x3 = d.get("xrd_reference_ch3_pulse")
        self._sample_xrd_position = (
            (int(x3), int(x4), int(x5))
            if x3 is not None and x4 is not None and x5 is not None else None
        )
        self._ruby_positions = {
            str(name): {f"ch{ch}": int(values[f"ch{ch}"]) for ch in (3, 4, 5)}
            for name, values in (d.get("ruby_positions") or {}).items()
            if isinstance(values, dict) and all(f"ch{ch}" in values for ch in (3, 4, 5))
        }
        self._ruby_offset_origin = str(d.get("ruby_offset_origin", RUBY_ORIGIN_XRD_POSITION))
        if not self._ruby_positions and d4 is not None and d5 is not None:
            self._ruby_positions["Default"] = {
                "ch3": int(d.get("offset_ch3_pulse") or 0),
                "ch4": int(d4), "ch5": int(d5),
            }
        self._pressure_dict_to_ui(d)
        self._update_sample_positions_summary()

    def _pressure_dict_to_ui(self, d: dict) -> None:
        """Restore pressure settings; absent keys (older files) mean off."""
        def number(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        def select(combo: QComboBox, key) -> None:
            index = combo.findData(key if isinstance(key, str) else "")
            combo.setCurrentIndex(max(0, index))

        self._pressure_enable_chk.setChecked(d.get("pressure_enabled") is True)
        select(self._pressure_scale_combo, d.get("pressure_scale"))
        self._temp_correction_chk.setChecked(d.get("temperature_correction_enabled") is True)
        select(self._temp_scale_combo, d.get("temperature_scale"))
        select(self._temp_source_combo, d.get("temperature_source", "lakeshore_b"))
        manual = number(d.get("manual_temperature_k"))
        if manual is not None:
            self._manual_temp_spin.setValue(manual)
        max_age = number(d.get("temperature_max_age_s"))
        self._temp_max_age_spin.setValue(10.0 if max_age is None else max_age)
        self._ruby_references = {}
        for name, values in (d.get("ruby_references") or {}).items():
            if isinstance(values, dict):
                self._ruby_references[str(name)] = {
                    "lambda_0_nm": number(values.get("lambda_0_nm")),
                    "t0_k": number(values.get("t0_k")),
                }
        self._update_pressure_widgets()

    def _follow_ui_to_dict(self) -> dict:
        g = self._build_global_follow()
        return {
            "tracking_method":       g.tracking_method,
            "gasket_ellipse":        g.gasket_ellipse,
            "gasket_sample_point":   g.gasket_sample_point,
            "gasket_reference_digest": g.gasket_reference_digest,
            **({"gasket_tolerance_um": g.gasket_tolerance_um}
               if g.gasket_tolerance_um is not None
               else {"gasket_tolerance_px": g.gasket_tolerance_px}),
            "gasket_average_frames": g.gasket_average_frames,
            "gasket_allow_uniform_scale": g.gasket_allow_uniform_scale,
            "interval_min":           g.interval_s / 60.0,
            "similarity_threshold":   g.similarity_threshold,
            "max_correction_ch4_mm":  g.max_correction_ch4_um / 1000.0,
            "max_correction_ch5_mm":  g.max_correction_ch5_um / 1000.0,
            "xy_max_retries":         g.xy_max_retries,
            "autofocus_range_um":     g.autofocus_range_um,
            "autofocus_steps":        g.autofocus_steps,
            "autofocus_method":       g.autofocus_method,
            "autofocus_n_frames":     g.autofocus_n_frames,
            "autofocus_speed":        g.autofocus_speed,
            "autofocus_peak_method":  g.autofocus_peak_method,
        }

    def _follow_dict_to_ui(self, d: dict) -> None:
        method = d.get("tracking_method", "similarity")
        self._follow_method_combo.setCurrentIndex(1 if method == "gasket_beta" else 0)
        self._gasket_ellipse = d.get("gasket_ellipse") if isinstance(d.get("gasket_ellipse"), dict) else None
        sample = d.get("gasket_sample_point")
        self._gasket_sample_point = (
            {"x": float(sample["x"]), "y": float(sample["y"])}
            if isinstance(sample, dict) and {"x", "y"} <= sample.keys() else None
        )
        self._gasket_reference_digest = d.get("gasket_reference_digest")
        legacy_unconverted = None
        if d.get("gasket_tolerance_um") is not None:
            tolerance_um = float(d["gasket_tolerance_um"])
        elif d.get("gasket_tolerance_px") is not None:
            legacy_px = float(d["gasket_tolerance_px"])
            try:
                if not math.isfinite(legacy_px) or legacy_px <= 0:
                    raise ValueError("invalid legacy gasket tolerance")
                converted = legacy_tolerance_um(legacy_px, load_stage_um_per_pixel())
                # The spin box displays tenths of a micrometre. Round down so
                # migration cannot widen the old pixel acceptance region.
                tolerance_um = math.floor(converted * 10.0) / 10.0
                if tolerance_um == 0:
                    legacy_unconverted = legacy_px
            except (OSError, ValueError, KeyError, OverflowError):
                # Keep the old value intact until calibration can be read.
                legacy_unconverted = legacy_px
                tolerance_um = 0.0
        else:
            tolerance_um = 8.0
        self._gasket_tolerance_spin.setValue(tolerance_um)
        self._legacy_gasket_tolerance_px = legacy_unconverted
        self._gasket_tolerance_spin.setSpecialValueText(
            "Legacy px limit" if legacy_unconverted is not None else "Enter µm limit"
        )
        self._gasket_tolerance_spin.setToolTip(
            "A saved pixel limit is retained. Restore camera calibration and reload "
            "to convert it, or enter a new µm limit."
            if legacy_unconverted is not None else
            "Maximum gasket-centre offset after correction. Each follow attempt corrects its measured shift down to whole Ch4/Ch5 pulses."
        )
        self._gasket_tolerance_spin.setEnabled(
            method == "gasket_beta"
            and not (self._runner is not None and self._runner.isRunning())
        )
        self._gasket_average_frames_spin.setValue(int(d.get("gasket_average_frames", 5)))
        self._gasket_allow_scale_chk.setChecked(bool(d.get("gasket_allow_uniform_scale", False)))
        self._follow_interval_spin.setValue(float(d.get("interval_min", 1.0)))
        self._follow_similarity_spin.setValue(float(d.get("similarity_threshold", 0.95)))
        self._follow_lim_ch4_spin.setValue(float(d.get("max_correction_ch4_mm", 0.400)))
        self._follow_lim_ch5_spin.setValue(float(d.get("max_correction_ch5_mm", 0.400)))
        self._follow_xy_retries_spin.setValue(int(d.get("xy_max_retries", 3)))
        self._follow_af_range_spin.setValue(float(d.get("autofocus_range_um", 30.0)))
        self._follow_af_steps_spin.setValue(int(d.get("autofocus_steps", 16)))
        _method = d.get("autofocus_method", "tenengrad")
        self._follow_af_method_combo.setCurrentIndex(0 if _method == "tenengrad" else 1)
        self._follow_af_nframes_spin.setValue(int(d.get("autofocus_n_frames", 10)))
        _af_speed = d.get("autofocus_speed", "H")
        for btn in self._follow_af_speed_group.buttons():
            if btn.text() == _af_speed:
                btn.setChecked(True)
                break
        if d.get("autofocus_peak_method", "highest") == "gaussian":
            self._follow_af_peak_gaussian.setChecked(True)
        else:
            self._follow_af_peak_highest.setChecked(True)
        self._on_follow_method_changed()

    def _limits_ui_to_dict(self) -> dict:
        return {
            "ch3_minus_mm": self._lim_ch3_minus.value(),
            "ch3_plus_mm":  self._lim_ch3_plus.value(),
            "ch4_minus_mm": self._lim_ch4_minus.value(),
            "ch4_plus_mm":  self._lim_ch4_plus.value(),
            "ch5_minus_mm": self._lim_ch5_minus.value(),
            "ch5_plus_mm":  self._lim_ch5_plus.value(),
        }

    def _limits_dict_to_ui(self, d: dict) -> None:
        self._lim_ch3_minus.setValue(float(d.get("ch3_minus_mm", _DEFAULT_LIMIT_MM)))
        self._lim_ch3_plus.setValue( float(d.get("ch3_plus_mm",  _DEFAULT_LIMIT_MM)))
        self._lim_ch4_minus.setValue(float(d.get("ch4_minus_mm", _DEFAULT_LIMIT_MM)))
        self._lim_ch4_plus.setValue( float(d.get("ch4_plus_mm",  _DEFAULT_LIMIT_MM)))
        self._lim_ch5_minus.setValue(float(d.get("ch5_minus_mm", _DEFAULT_LIMIT_MM)))
        self._lim_ch5_plus.setValue( float(d.get("ch5_plus_mm",  _DEFAULT_LIMIT_MM)))

    def _save_xrd_settings(self) -> None:
        settings = self._xrd_ui_to_dict()
        for key, inherited_value in self._xrd_inherited_defaults.items():
            if settings[key] == inherited_value:
                if key in ("dark_file", "defect_file"):
                    settings[key] = ""
                else:
                    settings.pop(key)
        self._set_setting("global_xrd", settings)

    def _save_limit_settings(self) -> None:
        self._set_setting("global_limits", self._limits_ui_to_dict())

    def _save_positions_settings(self) -> None:
        self._set_setting("global_positions", self._positions_ui_to_dict())

    def _save_follow_settings(self) -> None:
        self._set_setting("global_follow", self._follow_ui_to_dict())

    def _save_spectrum_settings(self) -> None:
        # API key is deliberately absent from this persisted dictionary.
        self._set_setting("global_spectrum", self._spectrum_ui_to_dict())

    # ── Window lifecycle ───────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._shutter_restore_busy:
            QMessageBox.warning(self, "Shutter operation in progress",
                                "Wait for the shutter restoration to finish before closing.")
            event.ignore()
            return
        if self._runner is not None and self._runner.isRunning():
            reply = QMessageBox.question(
                self, "Sequence Running",
                "A sequence is currently running. Stop it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._runner.request_stop()
            event.ignore()
            return
        if self._finishing_runner is not None:
            event.ignore()
            return
        self._elapsed_update_timer.stop()
        self._current_time_timer.stop()
        if self._ref_current_path is not None:
            self._set_setting("ref_save_path", str(self._ref_current_path))
        self._save_limit_settings()
        self._save_positions_settings()
        self._save_xrd_settings()
        self._save_shutter_settings()
        self._save_follow_settings()
        self._save_spectrum_settings()
        self._save_logging_settings()
        self._monitor_panel.shutdown()
        if self._slack is not None:
            self._slack.remove_listener(self._refresh_slack_checkbox)
        if self._owns_pdi_service:
            self._pdi_service.shutdown()
        super().closeEvent(event)


# ── module helper ──────────────────────────────────────────────────────────────

def _wrap(widget: QWidget) -> QWidget:
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.addWidget(widget)
    return w
