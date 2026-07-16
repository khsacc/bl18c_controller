"""
ExperimentalSchedulerWindow — Task 5 full implementation.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..actions import (
    FollowSampleAction,
    ForLoopAction,
    SaveSnapshotAction,
    StartFollowingAction,
    TakeXrdAction,
)
from ..device_context import DeviceContext
from ..runner import (
    GlobalCameraSettings,
    GlobalFollowSettings,
    GlobalLimits,
    GlobalXrdSettings,
    SequenceRunner,
)
from ..sequence import Sequence
from ..validator.pre_validator import PreValidator
from .dsl_editor import DslEditor
from .llm_panel import LlmPanel
from .timeline_widget import TimelineWidget
from settings.notification_sound import play_current_sound

_LOCALDATA_DIR = Path(__file__).parent.parent / "__localdata"
_DEFAULT_REF_PATH = _LOCALDATA_DIR / "reference_frame.png"
_DEFAULT_SNAPSHOT_DIR = _LOCALDATA_DIR / "snapshots"
_SETTINGS_PATH = _LOCALDATA_DIR / "scheduler_window_settings.json"

# Default limit value in mm (used when no saved value exists)
_DEFAULT_LIMIT_MM = 1.0


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


class ExperimentalSchedulerWindow(QMainWindow):

    def __init__(self, ctx: DeviceContext, main_window=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Experimental Scheduler")
        self.resize(1050, 700)

        self._ctx = ctx
        self._main_window = main_window
        self._runner: SequenceRunner | None = None
        self._sequence: Sequence = Sequence(actions=[], name="")
        self._captured_frame: np.ndarray | None = None
        self._ref_current_path: Path | None = None
        self._closed_btns: list = []
        self._validated = False
        self._validated_positions: dict[int, int] | None = None
        self._last_step_index: int | None = None
        self._last_step_description: str = ""
        self._last_tab_index = 0

        _LOCALDATA_DIR.mkdir(parents=True, exist_ok=True)

        self._build_ui()
        self._dsl_editor.set_full_validator(self._validate_sequence_from_dsl)
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

        # Body: left panel (Limit + Reference) | right panel (tabs)
        # Both sides are wrapped in independent QScrollAreas so the window
        # does not overflow even when the left panel becomes very tall.
        body = QHBoxLayout()
        body.setSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._make_limit_panel())
        left.addWidget(self._make_logging_panel())
        self._xrd_panel = self._make_xrd_panel()
        left.addWidget(self._xrd_panel)
        self._camera_panel = self._make_camera_panel()
        left.addWidget(self._camera_panel)
        self._follow_panel = self._make_follow_panel()
        left.addWidget(self._follow_panel)
        left.addStretch()

        left_content = QWidget()
        left_content.setLayout(left)

        left_scroll = QScrollArea()
        left_scroll.setWidget(left_content)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(500)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        right_tabs = self._make_tabs()
        right_scroll = QScrollArea()
        right_scroll.setWidget(right_tabs)
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        body.addWidget(left_scroll)
        body.addWidget(right_scroll, stretch=1)
        root.addLayout(body, stretch=1)

        root.addWidget(self._make_validation_panel())

        self._update_xrd_panel_visibility()
        self._update_camera_panel_visibility()
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
        """Render a PreCheckResult into the validation output panel.

        Shared by the Visual tab's Validate button and the Script tab's
        DslEditor (via its validation_result signal), so both entry points
        report into this one panel instead of keeping separate status areas.
        `ok_message` lets a caller customise the success text (e.g. DslEditor
        uses it to report "Converted N action(s) to Visual"); it is ignored
        whenever there are errors or warnings to show instead.
        """
        # Error/warning text may contain "<", ">", "&" (e.g. move-constraint
        # messages use "<="/">") — escape it or Qt's rich-text renderer will
        # parse it as (invalid, silently-swallowed) markup.
        if result.errors:
            lines = ["<span style='color:#c62828;'>&#x2717; Validation FAILED</span>"]
            for e in result.errors:
                lines.append(f"<span style='color:#c62828;'>  &#x2717; {html.escape(e)}</span>")
            for w in result.warnings:
                lines.append(f"<span style='color:darkorange;'>  &#x26a0; {html.escape(w)}</span>")
        elif result.warnings:
            lines = ["<span style='color:darkorange;'>&#x26a0; Validation passed with warnings</span>"]
            for w in result.warnings:
                lines.append(f"<span style='color:darkorange;'>  &#x26a0; {html.escape(w)}</span>")
        else:
            lines = [f"<span style='color:#2e7d32;'>&#x2713; {html.escape(ok_message)}</span>"]
        self._validation_output.setHtml("<br>".join(lines))

    def _make_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self._btn_run = QPushButton("▶  Run")
        self._btn_run.clicked.connect(self._on_run)
        self._btn_run.setEnabled(False)
        bar.addWidget(self._btn_run)

        self._btn_stop = QPushButton("■  Stop")
        self._btn_stop.setToolTip("Decelerate-stop the stage, then end the sequence.")
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_stop.setEnabled(False)
        bar.addWidget(self._btn_stop)

        self._btn_emergency_stop = QPushButton("⛔  Emergency Stop")
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

        bar.addSpacing(16)
        self._status_label = QLabel("Status: Ready")
        self._status_label.setStyleSheet("color: gray;")
        bar.addWidget(self._status_label)
        bar.addStretch()

        return bar

    def _make_limit_panel(self) -> QGroupBox:
        group = QGroupBox("Global Limits (from starting position)")
        form = QFormLayout(group)
        form.setSpacing(4)

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

    def _make_logging_panel(self) -> QGroupBox:
        group = QGroupBox("Logging")
        form = QFormLayout(group)
        form.setSpacing(4)

        self._log_path_edit = QLineEdit()
        self._log_path_edit.setPlaceholderText("run")
        self._log_path_edit.setText("run")
        self._log_path_edit.setToolTip(
            "Base name for the log directory.\n"
            "Saved as <Log directory>/<name>_<timestamp>/"
        )
        form.addRow("Run name:", self._log_path_edit)

        dir_row = QHBoxLayout()
        self._log_dir_edit = QLineEdit()
        self._log_dir_edit.setPlaceholderText("(default: __localdata/logs)")
        self._log_dir_edit.setToolTip(
            "Directory under which the <name>_<timestamp> log folder is created.\n"
            "Leave empty to use the default __localdata/logs/"
        )
        dir_row.addWidget(self._log_dir_edit, stretch=1)
        btn_browse_log_dir = QPushButton("…")
        btn_browse_log_dir.setFixedWidth(28)
        btn_browse_log_dir.clicked.connect(self._on_browse_log_dir)
        dir_row.addWidget(btn_browse_log_dir)
        form.addRow("Log directory:", dir_row)

        devices_w = QWidget()
        devices_l = QHBoxLayout(devices_w)
        devices_l.setContentsMargins(0, 0, 0, 0)
        devices_l.setSpacing(8)
        self._log_pace5000_chk = QCheckBox("PACE5000")
        self._log_pace5000_chk.setChecked(True)
        self._log_lakeshore_chk = QCheckBox("LakeShore 335")
        self._log_lakeshore_chk.setChecked(True)
        devices_l.addWidget(self._log_pace5000_chk)
        devices_l.addWidget(self._log_lakeshore_chk)
        devices_l.addStretch()
        form.addRow("Log devices:", devices_w)

        return group

    def _build_log_config(self) -> tuple[str, list[str]]:
        path = self._log_path_edit.text().strip() or "run"
        devices: list[str] = []
        if self._log_pace5000_chk.isChecked():
            devices.append("pace5000")
        if self._log_lakeshore_chk.isChecked():
            devices.append("lakeshore")
        return path, devices

    def _build_log_dir(self) -> str | None:
        return self._log_dir_edit.text().strip() or None

    def _on_browse_log_dir(self) -> None:
        current = self._log_dir_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Log Directory", current)
        if chosen:
            self._log_dir_edit.setText(chosen)

    def _log_ui_to_dict(self) -> dict:
        path, _ = self._build_log_config()
        return {
            "path": path,
            "dir": self._log_dir_edit.text().strip(),
            "pace5000": self._log_pace5000_chk.isChecked(),
            "lakeshore": self._log_lakeshore_chk.isChecked(),
        }

    def _log_dict_to_ui(self, d: dict) -> None:
        self._log_path_edit.setText(d.get("path", "run"))
        self._log_dir_edit.setText(d.get("dir", ""))
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
            if isinstance(action, (FollowSampleAction, StartFollowingAction)):
                return True
            if isinstance(action, ForLoopAction) and self._has_follow_action(action.body):
                return True
        return False

    def _has_snapshot_action(self, actions) -> bool:
        for action in actions:
            if isinstance(action, SaveSnapshotAction):
                return True
            if isinstance(action, ForLoopAction) and self._has_snapshot_action(action.body):
                return True
        return False

    def _has_xrd_action(self, actions) -> bool:
        for action in actions:
            if isinstance(action, TakeXrdAction):
                return True
            if isinstance(action, ForLoopAction) and self._has_xrd_action(action.body):
                return True
        return False

    def _update_xrd_panel_visibility(self) -> None:
        self._xrd_panel.setVisible(self._has_xrd_action(self._sequence.actions))

    def _update_camera_panel_visibility(self) -> None:
        self._camera_panel.setVisible(self._has_snapshot_action(self._sequence.actions))

    def _update_follow_panel_visibility(self) -> None:
        self._follow_panel.setVisible(self._has_follow_action(self._sequence.actions))

    def _make_camera_panel(self) -> QGroupBox:
        group = QGroupBox("Interactive Camera Settings")
        form = QFormLayout(group)
        form.setSpacing(4)

        save_row = QHBoxLayout()
        self._snapshot_save_dir_edit = QLineEdit()
        self._snapshot_save_dir_edit.setPlaceholderText(str(_DEFAULT_SNAPSHOT_DIR))
        save_row.addWidget(self._snapshot_save_dir_edit, stretch=1)
        btn_browse = QPushButton("...")
        btn_browse.setFixedWidth(28)
        btn_browse.clicked.connect(self._on_browse_snapshot_save_dir)
        save_row.addWidget(btn_browse)
        form.addRow("Snapshot dir:", save_row)
        return group

    def _on_browse_snapshot_save_dir(self) -> None:
        settings = self._get_settings()
        current = (
            self._snapshot_save_dir_edit.text().strip()
            or settings.get("last_snapshot_save_dir")
            or str(_DEFAULT_SNAPSHOT_DIR)
        )
        chosen = QFileDialog.getExistingDirectory(
            self, "Snapshot Save Directory", current
        )
        if chosen:
            self._snapshot_save_dir_edit.setText(chosen)
            self._set_setting("last_snapshot_save_dir", chosen)

    def _build_global_camera(self) -> GlobalCameraSettings:
        return GlobalCameraSettings(
            snapshot_save_dir=self._snapshot_save_dir_edit.text().strip() or None,
        )

    def _make_follow_panel(self) -> QGroupBox:
        group = QGroupBox("Follow Settings")
        form = QFormLayout(group)
        form.setSpacing(4)

        # ── Reference Image sub-section ────────────────────────────────────
        ref_group = QGroupBox("Reference Image")
        ref_vbox = QVBoxLayout(ref_group)
        ref_vbox.setSpacing(4)
        ref_vbox.setContentsMargins(4, 6, 4, 4)

        btn_row = QHBoxLayout()
        self._btn_capture_now = QPushButton("Capture Now")
        self._btn_capture_now.clicked.connect(self._on_capture_now)
        btn_row.addWidget(self._btn_capture_now)
        self._btn_load_ref = QPushButton("Load from…")
        self._btn_load_ref.clicked.connect(self._on_load_ref_file)
        btn_row.addWidget(self._btn_load_ref)
        btn_row.addStretch()
        ref_vbox.addLayout(btn_row)

        status_row = QHBoxLayout()
        self._ref_status_label = QLabel("No image")
        self._ref_status_label.setStyleSheet("color: gray;")
        status_row.addWidget(self._ref_status_label)
        status_row.addStretch()
        self._btn_preview = QPushButton("Preview")
        self._btn_preview.setEnabled(False)
        self._btn_preview.clicked.connect(self._on_preview_ref)
        status_row.addWidget(self._btn_preview)
        ref_vbox.addLayout(status_row)

        form.addRow(ref_group)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(sep1)

        # Interval
        self._follow_interval_spin = _no_wheel(QDoubleSpinBox())
        self._follow_interval_spin.setRange(0.1, 1440.0)
        self._follow_interval_spin.setDecimals(1)
        self._follow_interval_spin.setSingleStep(0.5)
        self._follow_interval_spin.setValue(5.0)
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
            "Max number of XY correction attempts per interval.\n"
            "1 = no retry (single correction only)."
        )
        form.addRow("XY retries:", self._follow_xy_retries_spin)

        # ── Autofocus sub-section ──────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(sep2)

        form.addRow("", QLabel("Auto-Focus (Ch3) after XY correction — always enabled"))

        # AF scan range and steps
        af_range_row = QWidget()
        af_range_layout = QHBoxLayout(af_range_row)
        af_range_layout.setContentsMargins(0, 0, 0, 0)
        af_range_layout.setSpacing(4)
        self._follow_af_range_spin = _no_wheel(QDoubleSpinBox())
        self._follow_af_range_spin.setRange(1.0, 2000.0)
        self._follow_af_range_spin.setDecimals(0)
        self._follow_af_range_spin.setSingleStep(5.0)
        self._follow_af_range_spin.setValue(20.0)
        self._follow_af_range_spin.setSuffix(" µm")
        self._follow_af_steps_spin = _no_wheel(QSpinBox())
        self._follow_af_steps_spin.setRange(2, 100)
        self._follow_af_steps_spin.setValue(10)
        af_range_layout.addWidget(QLabel("±"))
        af_range_layout.addWidget(self._follow_af_range_spin)
        af_range_layout.addWidget(QLabel("N:"))
        af_range_layout.addWidget(self._follow_af_steps_spin)
        form.addRow("AF range:", af_range_row)

        # AF sharpness method
        self._follow_af_method_combo = _no_wheel(QComboBox())
        self._follow_af_method_combo.addItem("Tenengrad", "tenengrad")
        self._follow_af_method_combo.addItem("Laplacian", "laplacian")
        self._follow_af_method_combo.setCurrentIndex(1)
        self._follow_af_method_combo.setToolTip(
            "Sharpness metric:\n"
            "Tenengrad — mean squared Sobel gradient\n"
            "Laplacian — variance of Laplacian (default)"
        )
        form.addRow("Method:", self._follow_af_method_combo)

        # AF frames per position
        self._follow_af_nframes_spin = _no_wheel(QSpinBox())
        self._follow_af_nframes_spin.setRange(1, 50)
        self._follow_af_nframes_spin.setValue(1)
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
            interval_s=self._follow_interval_spin.value() * 60.0,
            similarity_threshold=self._follow_similarity_spin.value(),
            max_correction_ch4_um=self._follow_lim_ch4_spin.value() * 1000.0,
            max_correction_ch5_um=self._follow_lim_ch5_spin.value() * 1000.0,
            xy_max_retries=self._follow_xy_retries_spin.value(),
            autofocus_enabled=True,
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

        # Exposure
        self._xrd_exp_spin = _no_wheel(QSpinBox())
        self._xrd_exp_spin.setRange(1, 60000)
        self._xrd_exp_spin.setSuffix(" ms")
        self._xrd_exp_spin.setValue(1000)
        form.addRow("Exposure:", self._xrd_exp_spin)

        # Save directory
        save_row = QHBoxLayout()
        self._xrd_save_dir_edit = QLineEdit()
        self._xrd_save_dir_edit.setPlaceholderText("(auto: __localdata/xrd/<timestamp>/)")
        save_row.addWidget(self._xrd_save_dir_edit, stretch=1)
        btn_browse_dir = QPushButton("…")
        btn_browse_dir.setFixedWidth(28)
        btn_browse_dir.clicked.connect(self._on_browse_xrd_save_dir)
        save_row.addWidget(btn_browse_dir)
        form.addRow("Save to:", save_row)

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

        # Defect correction
        defect_chk_row = QHBoxLayout()
        self._xrd_defect_chk = QCheckBox("Enable defect correction")
        self._xrd_defect_chk.setChecked(True)
        defect_chk_row.addWidget(self._xrd_defect_chk)
        self._xrd_defect_kernel_combo = _no_wheel(QComboBox())
        self._xrd_defect_kernel_combo.addItems(["3×3", "4×4", "5×5", "6×6"])
        defect_chk_row.addWidget(self._xrd_defect_kernel_combo)
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

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(sep)

        # Oscillation toggle
        self._xrd_osc_chk = QCheckBox("Oscillate sample stage during measurement (Ch11)")
        self._xrd_osc_chk.setChecked(False)
        form.addRow("", self._xrd_osc_chk)

        # Pos A / Pos B
        osc_pos_row = QWidget()
        osc_pos_layout = QHBoxLayout(osc_pos_row)
        osc_pos_layout.setContentsMargins(0, 0, 0, 0)
        osc_pos_layout.setSpacing(6)
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
        osc_pos_layout.addWidget(QLabel("A:"))
        osc_pos_layout.addWidget(self._xrd_osc_pos_a_spin)
        osc_pos_layout.addSpacing(8)
        osc_pos_layout.addWidget(QLabel("B:"))
        osc_pos_layout.addWidget(self._xrd_osc_pos_b_spin)
        osc_pos_layout.addStretch()
        form.addRow("Pos (deg):", osc_pos_row)

        # Dwell and speed
        osc_ds_row = QWidget()
        osc_ds_layout = QHBoxLayout(osc_ds_row)
        osc_ds_layout.setContentsMargins(0, 0, 0, 0)
        osc_ds_layout.setSpacing(6)
        self._xrd_osc_dwell_spin = _no_wheel(QSpinBox())
        self._xrd_osc_dwell_spin.setRange(0, 60000)
        self._xrd_osc_dwell_spin.setValue(0)
        self._xrd_osc_dwell_spin.setSuffix(" ms")
        self._xrd_osc_dwell_spin.setFixedWidth(80)
        osc_ds_layout.addWidget(QLabel("Dwell:"))
        osc_ds_layout.addWidget(self._xrd_osc_dwell_spin)
        osc_ds_layout.addSpacing(12)
        osc_ds_layout.addWidget(QLabel("Speed:"))
        self._xrd_osc_speed_group = QButtonGroup(group)
        for spd in ("H", "M", "L"):
            rb = QRadioButton(spd)
            self._xrd_osc_speed_group.addButton(rb)
            osc_ds_layout.addWidget(rb)
            if spd == "M":
                rb.setChecked(True)
        osc_ds_layout.addStretch()
        form.addRow("Dwell / Speed:", osc_ds_row)

        # Enable/disable sub-controls based on checkbox
        def _toggle_osc(checked: bool) -> None:
            for w in (
                self._xrd_osc_pos_a_spin, self._xrd_osc_pos_b_spin,
                self._xrd_osc_dwell_spin,
            ):
                w.setEnabled(checked)
            for btn in self._xrd_osc_speed_group.buttons():
                btn.setEnabled(checked)

        _toggle_osc(False)
        self._xrd_osc_chk.toggled.connect(_toggle_osc)

        return group

    def _on_browse_xrd_save_dir(self) -> None:
        current = self._xrd_save_dir_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "XRD Save Directory", current)
        if chosen:
            self._xrd_save_dir_edit.setText(chosen)

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
            save_dir=self._xrd_save_dir_edit.text().strip() or None,
            dark_file=self._xrd_dark_edit.text().strip() or None,
            dark_enabled=self._xrd_dark_chk.isChecked(),
            defect_file=self._xrd_defect_edit.text().strip() or None,
            defect_enabled=self._xrd_defect_chk.isChecked(),
            defect_kernel=int(kernel_str[0]),
            flip_v=self._xrd_flip_v_chk.isChecked(),
            flip_h=self._xrd_flip_h_chk.isChecked(),
            oscillate=self._xrd_osc_chk.isChecked(),
            osc_pos_a_deg=self._xrd_osc_pos_a_spin.value(),
            osc_pos_b_deg=self._xrd_osc_pos_b_spin.value(),
            osc_dwell_ms=self._xrd_osc_dwell_spin.value(),
            osc_speed=osc_speed,
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
        self._btn_validate_visual.setToolTip("Run full validation and enable Run if no errors")
        self._btn_validate_visual.clicked.connect(self._on_validate_visual)
        validate_bar.addWidget(self._btn_validate_visual)
        self._validate_visual_status = QLabel("Not validated — click Validate to enable Run")
        self._validate_visual_status.setStyleSheet("color: gray;")
        validate_bar.addWidget(self._validate_visual_status, stretch=1)
        visual_layout.addLayout(validate_bar)

        self._timeline = TimelineWidget()
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
        tabs.addTab(self._llm_panel, "AI Assist")

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

    def _on_capture_now(self) -> None:
        try:
            import cv2
        except ImportError:
            QMessageBox.critical(self, "Error", "opencv-python is not installed.")
            return
        frame = self._borrow_camera_frame()
        if frame is None:
            frame = self._grab_from_camera(camera_index=0)
        if frame is None:
            QMessageBox.warning(self, "Capture Failed",
                                "Could not obtain a frame from the camera.")
            return
        s = self._get_settings()
        default_dir = s.get("last_ref_save_dir", str(_DEFAULT_REF_PATH.parent))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Reference Photo",
            str(Path(default_dir) / f"reference_{timestamp}.png"),
            "PNG Image (*.png);;JPEG Image (*.jpg)",
        )
        if not path:
            return
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(p), frame)
        self._ref_current_path = p
        self._set_setting("last_ref_save_dir", str(p.parent))
        self._set_setting("ref_save_path", str(p))
        self._apply_ref_frame(frame, p.name)

    def _borrow_camera_frame(self) -> np.ndarray | None:
        """Return current_frame from InteractiveCameraWindow if it is open."""
        if self._main_window is None:
            return None
        try:
            from apps.interactive_camera.interactive_camera import MainWindow as ICW
        except ImportError:
            return None
        for window in self._main_window._open_windows.values():
            if isinstance(window, ICW):
                f = getattr(window, "current_frame", None)
                if f is not None:
                    return f.copy()
        return None

    def _grab_from_camera(self, camera_index: int = 0) -> np.ndarray | None:
        try:
            import cv2
        except ImportError:
            QMessageBox.critical(self, "Error", "opencv-python is not installed.")
            return None
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            return None
        ret, frame = cap.read()
        cap.release()
        return frame if ret else None

    def _on_load_ref_file(self) -> None:
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

    # ── Run / Stop / Save / Load ───────────────────────────────────────────

    def _check_stage_unchanged_since_validation(self) -> bool:
        """Block Run if the stage has moved on any channel since the last
        successful Validate. Returns True if it's safe to proceed."""
        ctrl = self._ctx.controller
        if ctrl is None or self._validated_positions is None:
            return True

        moved: list[str] = []
        for ch, baseline in self._validated_positions.items():
            try:
                current = int(ctrl.get_ch_pos(ch))
            except Exception:
                continue
            if current != baseline:
                moved.append(f"Ch{ch}: validation時 {baseline:+} → 現在 {current:+}")

        if not moved:
            return True

        QMessageBox.critical(
            self, "ステージが動いています",
            "最新のvalidation時からステージが動いています。まずvalidationを行ってください。\n\n"
            + "\n".join(moved),
        )
        self._reset_validation()
        return False

    def _on_run(self) -> None:
        if not self._check_stage_unchanged_since_validation():
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
        global_follow = self._build_global_follow()
        global_camera = self._build_global_camera()
        validator = PreValidator()
        result = validator.validate(self._sequence, self._ctx, global_limits, global_xrd)

        if not result.ok:
            msg = "Cannot run — the following errors were found:\n\n"
            msg += "\n".join(f"• {e}" for e in result.errors)
            QMessageBox.critical(self, "Validation Errors", msg)
            self._reset_validation()
            return

        if result.warnings:
            msg = "Warnings:\n\n"
            msg += "\n".join(f"• {w}" for w in result.warnings)
            msg += "\n\nContinue anyway?"
            if QMessageBox.question(
                self, "Validation Warnings", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return

        if self._main_window is not None:
            self._closed_btns = self._main_window.close_all_sub_windows()

        self._last_step_index = None
        self._last_step_description = ""
        log_path, log_devices = self._build_log_config()
        log_dir = self._build_log_dir()
        self._timeline.clear_highlights()
        self._runner = SequenceRunner(
            self._sequence, self._ctx, global_limits,
            global_xrd=global_xrd,
            global_follow=global_follow,
            global_camera=global_camera,
            log_path=log_path,
            log_devices=log_devices,
            log_dir=log_dir,
        )
        self._runner.step_started.connect(self._on_step_started)
        self._runner.step_completed.connect(self._on_step_completed)
        self._runner.progress_updated.connect(self._on_progress)
        self._runner.sequence_completed.connect(self._on_sequence_completed)
        self._runner.sequence_stopped.connect(self._on_sequence_stopped)
        self._runner.error_occurred.connect(self._on_error_occurred)

        self._btn_run.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_emergency_stop.setEnabled(True)
        self._btn_save.setEnabled(False)
        self._btn_load.setEnabled(False)
        self._set_status("Running…", "blue")
        self._runner.start()

    def _on_stop(self) -> None:
        if self._runner is not None:
            self._runner.request_stop()
        self._set_status("Stop requested…", "orange")

    def _on_emergency_stop(self) -> None:
        if self._runner is not None:
            self._runner.request_emergency_stop()
        self._set_status("Emergency stop requested…", "red")

    @pyqtSlot()
    def _on_sequence_completed(self) -> None:
        self._runner = None
        self._set_status("Completed", "green")
        self._set_idle()
        self._do_restore()
        play_current_sound()

    @pyqtSlot()
    def _on_sequence_stopped(self) -> None:
        self._runner = None
        self._set_status("Stopped — sequence did NOT complete", "orange")
        self._set_idle()
        self._do_restore()
        if self._last_step_index is not None:
            where = f"\n\nLast step started: #{self._last_step_index + 1} — {self._last_step_description}"
        else:
            where = ""
        QMessageBox.warning(
            self, "Sequence Stopped",
            "The sequence was stopped before all steps finished — it did NOT complete."
            + where,
        )

    @pyqtSlot(int, str)
    def _on_error_occurred(self, index: int, message: str) -> None:
        self._runner = None
        self._set_status(f"Error at step {index}", "red")
        self._set_idle()
        self._do_restore()
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

    @pyqtSlot(int)
    def _on_step_completed(self, index: int) -> None:
        self._timeline.mark_step_done(index)

    @pyqtSlot(str)
    def _on_progress(self, message: str) -> None:
        self._set_status(message, "blue")

    def _on_timeline_changed(self) -> None:
        """Sync _sequence whenever the user edits the timeline."""
        self._sequence = self._timeline.get_sequence()
        self._update_xrd_panel_visibility()
        self._update_camera_panel_visibility()
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
        ):
            self._dsl_editor.convert_to_visual()
        self._last_tab_index = index

    def _on_dsl_converted(self, seq: Sequence) -> None:
        """Handle successful DSL parse; run full validation before applying."""
        result = self._validate_sequence_from_dsl(seq)
        if result.ok:
            self._tabs.setCurrentIndex(0)

    def _on_ai_sequence_applied(self, seq: Sequence) -> None:
        """Handle LlmPanel sequence_applied signal; update timeline and script."""
        self._sequence = seq
        self._timeline.set_sequence(seq)
        self._dsl_editor.set_sequence(seq)
        self._tabs.setCurrentIndex(0)
        self._update_xrd_panel_visibility()
        self._update_camera_panel_visibility()
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
        self._update_camera_panel_visibility()
        self._update_follow_panel_visibility()
        self._reset_validation()

    # ── Validation helpers ─────────────────────────────────────────────────

    def _reset_validation(self) -> None:
        """Mark sequence as unvalidated and disable Run."""
        self._validated = False
        self._validated_positions = None
        self._btn_run.setEnabled(False)
        self._validate_visual_status.setText("Not validated — click Validate to enable Run")
        self._validate_visual_status.setStyleSheet("color: gray;")

    def _set_validated(self, baseline_positions: dict[int, int] | None = None) -> None:
        """Mark sequence as validated and enable Run.

        `baseline_positions` (Ch1-11 pulse positions read during this
        validation) is stored so `_on_run` can detect stage moves that happen
        between Validate and Run.
        """
        self._validated = True
        self._validated_positions = baseline_positions or None
        self._btn_run.setEnabled(True)

    def _on_validate_visual(self) -> None:
        """Validate button handler for the Visual tab."""
        global_limits = self._build_global_limits()
        global_xrd = self._build_global_xrd()
        result = PreValidator().validate(self._sequence, self._ctx, global_limits, global_xrd)
        self._show_validation_result(result)
        if result.errors:
            self._validate_visual_status.setText(f"✗ {len(result.errors)} error(s) found — fix before running")
            self._validate_visual_status.setStyleSheet("color: #c62828;")
            self._reset_validation()
        elif result.warnings:
            self._validate_visual_status.setText(f"✓ Passed with {len(result.warnings)} warning(s)")
            self._validate_visual_status.setStyleSheet("color: darkorange;")
            self._set_validated(result.baseline_positions)
        else:
            self._validate_visual_status.setText("✓ Validation passed — no errors found")
            self._validate_visual_status.setStyleSheet("color: #2e7d32;")
            self._set_validated(result.baseline_positions)

    def _validate_sequence_from_dsl(self, seq: Sequence):
        """Full validation callback for DslEditor.

        Called after DSL structural checks pass.  Runs the full PreValidator,
        updates self._sequence + timeline on success, and controls the Run button.
        Returns the PreCheckResult so DslEditor can show errors/warnings.
        """
        global_limits = self._build_global_limits()
        global_xrd = self._build_global_xrd()
        result = PreValidator().validate(seq, self._ctx, global_limits, global_xrd)
        self._show_validation_result(result)
        if result.ok:
            self._sequence = seq
            self._timeline.set_sequence(seq)
            self._update_xrd_panel_visibility()
            self._update_camera_panel_visibility()
            self._update_follow_panel_visibility()
            self._validate_visual_status.setText("✓ Validated from Script tab")
            self._validate_visual_status.setStyleSheet("color: #2e7d32;")
            self._set_validated(result.baseline_positions)
        else:
            self._validate_visual_status.setText(f"✗ {len(result.errors)} error(s) found — fix before running")
            self._validate_visual_status.setStyleSheet("color: #c62828;")
            self._reset_validation()
        return result

    def _set_idle(self) -> None:
        self._btn_run.setEnabled(self._validated)
        self._btn_stop.setEnabled(False)
        self._btn_emergency_stop.setEnabled(False)
        self._btn_save.setEnabled(True)
        self._btn_load.setEnabled(True)

    def _do_restore(self) -> None:
        if self._main_window is not None and self._closed_btns:
            self._main_window.restore_sub_windows(self._closed_btns)
        self._closed_btns = []

    def _set_status(self, text: str, color: str = "gray") -> None:
        self._status_label.setText(f"Status: {text}")
        self._status_label.setStyleSheet(f"color: {color};")

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
            self._sequence.global_xrd = self._xrd_ui_to_dict()
            self._sequence.global_follow = self._follow_ui_to_dict()
            self._sequence.global_camera = self._camera_ui_to_dict()
            self._sequence.global_limits = self._limits_ui_to_dict()
            self._sequence.save(path)
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
        self._update_camera_panel_visibility()
        self._update_follow_panel_visibility()
        self._reset_validation()

        if seq.global_xrd is not None:
            self._xrd_dict_to_ui(seq.global_xrd)
        if seq.global_follow is not None:
            self._follow_dict_to_ui(seq.global_follow)
        if seq.global_camera is not None:
            self._camera_dict_to_ui(seq.global_camera)
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

        self._xrd_dict_to_ui(s.get("global_xrd", {}))
        self._camera_dict_to_ui(s.get("global_camera", {}))
        self._follow_dict_to_ui(s.get("global_follow", {}))
        self._limits_dict_to_ui(s.get("global_limits", {}))
        self._log_dict_to_ui(s.get("logging", {}))

    # ── Settings dict helpers ──────────────────────────────────────────────

    def _xrd_ui_to_dict(self) -> dict:
        g = self._build_global_xrd()
        return {
            "exposure_ms":    g.exposure_ms,
            "save_dir":       g.save_dir or "",
            "flip_v":         g.flip_v,
            "flip_h":         g.flip_h,
            "dark_enabled":   g.dark_enabled,
            "dark_file":      g.dark_file or "",
            "defect_enabled": g.defect_enabled,
            "defect_file":    g.defect_file or "",
            "defect_kernel":  g.defect_kernel,
            "oscillate":      g.oscillate,
            "osc_pos_a_deg":  g.osc_pos_a_deg,
            "osc_pos_b_deg":  g.osc_pos_b_deg,
            "osc_dwell_ms":   g.osc_dwell_ms,
            "osc_speed":      g.osc_speed,
        }

    def _xrd_dict_to_ui(self, d: dict) -> None:
        self._xrd_exp_spin.setValue(int(d.get("exposure_ms", 1000)))
        self._xrd_save_dir_edit.setText(d.get("save_dir", ""))
        self._xrd_flip_v_chk.setChecked(bool(d.get("flip_v", True)))
        self._xrd_flip_h_chk.setChecked(bool(d.get("flip_h", False)))
        self._xrd_dark_chk.setChecked(bool(d.get("dark_enabled", False)))
        self._xrd_dark_edit.setText(d.get("dark_file", ""))
        self._xrd_defect_chk.setChecked(bool(d.get("defect_enabled", True)))
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

    def _camera_ui_to_dict(self) -> dict:
        g = self._build_global_camera()
        return {
            "snapshot_save_dir": g.snapshot_save_dir or "",
        }

    def _camera_dict_to_ui(self, d: dict) -> None:
        self._snapshot_save_dir_edit.setText(
            d.get("snapshot_save_dir", "") or ""
        )

    def _follow_ui_to_dict(self) -> dict:
        g = self._build_global_follow()
        return {
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
        self._follow_interval_spin.setValue(float(d.get("interval_min", 5.0)))
        self._follow_similarity_spin.setValue(float(d.get("similarity_threshold", 0.95)))
        self._follow_lim_ch4_spin.setValue(float(d.get("max_correction_ch4_mm", 0.400)))
        self._follow_lim_ch5_spin.setValue(float(d.get("max_correction_ch5_mm", 0.400)))
        self._follow_xy_retries_spin.setValue(int(d.get("xy_max_retries", 3)))
        self._follow_af_range_spin.setValue(float(d.get("autofocus_range_um", 20.0)))
        self._follow_af_steps_spin.setValue(int(d.get("autofocus_steps", 10)))
        _method = d.get("autofocus_method", "laplacian")
        self._follow_af_method_combo.setCurrentIndex(0 if _method == "tenengrad" else 1)
        self._follow_af_nframes_spin.setValue(int(d.get("autofocus_n_frames", 1)))
        _af_speed = d.get("autofocus_speed", "H")
        for btn in self._follow_af_speed_group.buttons():
            if btn.text() == _af_speed:
                btn.setChecked(True)
                break
        if d.get("autofocus_peak_method", "highest") == "gaussian":
            self._follow_af_peak_gaussian.setChecked(True)
        else:
            self._follow_af_peak_highest.setChecked(True)

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
        self._set_setting("global_xrd", self._xrd_ui_to_dict())

    def _save_camera_settings(self) -> None:
        self._set_setting("global_camera", self._camera_ui_to_dict())

    def _save_limit_settings(self) -> None:
        self._set_setting("global_limits", self._limits_ui_to_dict())

    def _save_follow_settings(self) -> None:
        self._set_setting("global_follow", self._follow_ui_to_dict())

    # ── Window lifecycle ───────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
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
            self._runner.wait(5000)
        if self._ref_current_path is not None:
            self._set_setting("ref_save_path", str(self._ref_current_path))
        self._save_limit_settings()
        self._save_xrd_settings()
        self._save_camera_settings()
        self._save_follow_settings()
        self._save_logging_settings()
        super().closeEvent(event)


# ── module helper ──────────────────────────────────────────────────────────────

def _wrap(widget: QWidget) -> QWidget:
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.addWidget(widget)
    return w
