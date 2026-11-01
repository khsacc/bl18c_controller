"""Sequential Spectroscopic Measurements window.

Successor in concept to Sequential Relative Moves: several pressure markers
(ruby, Sm2+:SrB4O7) in the sample chamber are registered by their Ch3/Ch4/Ch5
positions; a fully automatic run visits each one relative to Pos1, measures
its spectrum and pressure through the FluoRaPressée API, saves everything in
a dated sub-directory and reports the mean pressure.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDoubleValidator
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

try:
    from apps.seq_spectro import spectro_core as core
    from apps.seq_spectro.spectro_runner import OWNER, SequenceRun, SimSpectroReader
    from settings import online_spectrometer_prefs
    from utils.fluora_pressee import FluoraPresseeReader
    from utils.stage.control_stage import PULSE_SCALE
    from utils.stage.qt_stop_watcher import StopProgressWatcher
except ImportError:  # standalone
    import sys as _sys
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from apps.seq_spectro import spectro_core as core
    from apps.seq_spectro.spectro_runner import OWNER, SequenceRun, SimSpectroReader
    from settings import online_spectrometer_prefs
    from utils.fluora_pressee import FluoraPresseeReader
    from utils.stage.control_stage import PULSE_SCALE
    from utils.stage.qt_stop_watcher import StopProgressWatcher

_LOCALDATA_DIR = Path(__file__).resolve().parent / "__localdata"
_LAST_SETUP_FILE = _LOCALDATA_DIR / "last_setup.json"
_LAST_DIRS_FILE = _LOCALDATA_DIR / "last_dirs.json"

COL_POS, COL_RECORD, COL_D3, COL_D4, COL_D5, COL_MARKER, COL_LAM0, COL_T0, \
    COL_LAM, COL_P, COL_STATUS = range(11)
_HEADERS = ["Pos", "", "ΔCh3 (µm)", "ΔCh4 (µm)", "ΔCh5 (µm)", "Marker",
            "λ₀ (nm)", "T₀ (K)", "λ (nm)", "P (GPa)", "Status"]


def _no_wheel(widget):
    widget.wheelEvent = lambda event: event.ignore()
    return widget


def _parse_float(text: str) -> float | None:
    try:
        return float(text.strip())
    except ValueError:
        return None


# ── Per-marker settings box ──────────────────────────────────────────────────

class MarkerSettingsBox(QGroupBox):
    changed = pyqtSignal()

    def __init__(self, marker: str, parent=None) -> None:
        self.spec = core.MARKERS[marker]
        super().__init__(f"{self.spec.label} settings", parent)
        form = QFormLayout(self)

        self.pressure_scale = _no_wheel(QComboBox())
        self.pressure_scale.addItem("— select —", "")
        for key, label in self.spec.pressure_scales.items():
            self.pressure_scale.addItem(label, key)
        form.addRow("Pressure scale:", self.pressure_scale)

        self.t_correction = QCheckBox("Temperature correction")
        form.addRow(self.t_correction)
        self.t_scale = _no_wheel(QComboBox())
        self.t_scale.addItem("— select —", "")
        for key, label in self.spec.temperature_scales.items():
            self.t_scale.addItem(label, key)
        form.addRow("Correction scale:", self.t_scale)

        self.fit_function = _no_wheel(QComboBox())
        self.fit_function.addItems(core.FIT_FUNCTIONS)
        form.addRow("Fit function:", self.fit_function)
        self.baseline = _no_wheel(QComboBox())
        for key, label in core.BASELINE_MODELS.items():
            self.baseline.addItem(label, key)
        form.addRow("Baseline:", self.baseline)
        peaks = "2 (R1 + R2; R1 is used)" if self.spec.fit_peak_count == 2 else "1 (0-0 line)"
        form.addRow("Fitted peaks:", QLabel(peaks))

        range_row = QHBoxLayout()
        self.range_full = QRadioButton("Whole spectrum")
        self.range_fixed = QRadioButton("Fixed:")
        group = QButtonGroup(self)
        group.addButton(self.range_full)
        group.addButton(self.range_fixed)
        self.range_full.setChecked(True)
        self.range_start = _no_wheel(QDoubleSpinBox())
        self.range_end = _no_wheel(QDoubleSpinBox())
        low, high = self.spec.lambda_0_range_nm
        for spin, value in ((self.range_start, low - 5.0), (self.range_end, high + 10.0)):
            spin.setRange(300.0, 1100.0)
            spin.setDecimals(2)
            spin.setSuffix(" nm")
            spin.setValue(value)
        range_row.addWidget(self.range_full)
        range_row.addWidget(self.range_fixed)
        range_row.addWidget(self.range_start)
        range_row.addWidget(QLabel("–"))
        range_row.addWidget(self.range_end)
        form.addRow("Fit range:", range_row)

        self.r2_min = _no_wheel(QDoubleSpinBox())
        self.r2_min.setRange(0.0, 1.0)
        self.r2_min.setDecimals(3)
        self.r2_min.setSingleStep(0.01)
        form.addRow("R² minimum:", self.r2_min)
        self.exposure = _no_wheel(QDoubleSpinBox())
        self.exposure.setRange(*core.EXPOSURE_RANGE_S)
        self.exposure.setDecimals(3)
        self.exposure.setSuffix(" s")
        form.addRow("Exposure time:", self.exposure)
        self.accumulations = _no_wheel(QSpinBox())
        self.accumulations.setRange(*core.ACCUMULATION_RANGE)
        form.addRow("Accumulations:", self.accumulations)

        gain_row = QHBoxLayout()
        self.em_gain_enabled = QCheckBox("Set")
        self.em_gain_enabled.setToolTip(
            "Unticked: FluoRaPressée's current EM gain is left unchanged.")
        self.em_gain = _no_wheel(QSpinBox())
        self.em_gain.setRange(*core.EM_GAIN_RANGE)
        self.em_gain.setToolTip("Checked against the camera's own range before the run starts.")
        gain_row.addWidget(self.em_gain_enabled)
        gain_row.addWidget(self.em_gain, 1)
        form.addRow("EM gain:", gain_row)

        self.optimise = QCheckBox("Optimise when R² is below the minimum")
        self.optimise.setToolTip(
            "Same algorithm as the Experimental Scheduler: halve the exposure when "
            "saturated (up to 3 times); otherwise double the EM gain up to its maximum, "
            "then add accumulations up to their maximum. Only spectra that meet the R² "
            "minimum are saved.")
        form.addRow(self.optimise)
        self.em_gain_max = _no_wheel(QSpinBox())
        self.em_gain_max.setRange(1, core.EM_GAIN_RANGE[1])
        form.addRow("EM gain maximum:", self.em_gain_max)
        self.max_accumulations = _no_wheel(QSpinBox())
        self.max_accumulations.setRange(*core.ACCUMULATION_RANGE)
        form.addRow("Maximum accumulations:", self.max_accumulations)

        self.set_settings(core.MarkerSettings())
        for signal in (self.pressure_scale.currentIndexChanged, self.t_scale.currentIndexChanged,
                       self.fit_function.currentIndexChanged, self.baseline.currentIndexChanged,
                       self.range_start.valueChanged, self.range_end.valueChanged,
                       self.r2_min.valueChanged, self.exposure.valueChanged,
                       self.accumulations.valueChanged, self.em_gain.valueChanged,
                       self.em_gain_max.valueChanged, self.max_accumulations.valueChanged):
            signal.connect(lambda *_a: self.changed.emit())
        self.t_correction.toggled.connect(self._sync_enabled)
        self.range_fixed.toggled.connect(self._sync_enabled)
        self.em_gain_enabled.toggled.connect(self._sync_enabled)
        self.optimise.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    def _sync_enabled(self, *_args) -> None:
        self.t_scale.setEnabled(self.t_correction.isChecked())
        fixed = self.range_fixed.isChecked()
        self.range_start.setEnabled(fixed)
        self.range_end.setEnabled(fixed)
        self.em_gain.setEnabled(self.em_gain_enabled.isChecked())
        self.em_gain_max.setEnabled(self.optimise.isChecked())
        self.max_accumulations.setEnabled(self.optimise.isChecked())
        self.changed.emit()

    @staticmethod
    def _select(combo: QComboBox, key: str) -> None:
        index = combo.findData(key)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def set_settings(self, s: core.MarkerSettings) -> None:
        self._select(self.pressure_scale, s.pressure_scale)
        self.t_correction.setChecked(s.temperature_correction)
        self._select(self.t_scale, s.temperature_scale)
        index = self.fit_function.findText(s.fit_function)
        self.fit_function.setCurrentIndex(max(index, 0))
        self._select(self.baseline, s.baseline_model)
        (self.range_fixed if s.fit_range_enabled else self.range_full).setChecked(True)
        if s.fit_range_start_nm < s.fit_range_end_nm:
            self.range_start.setValue(s.fit_range_start_nm)
            self.range_end.setValue(s.fit_range_end_nm)
        self.r2_min.setValue(s.r_squared_min)
        self.exposure.setValue(s.exposure_time_s)
        self.accumulations.setValue(s.accumulations)
        self.em_gain_enabled.setChecked(s.em_gain_enabled)
        self.em_gain.setValue(s.em_gain)
        self.optimise.setChecked(s.optimise)
        self.em_gain_max.setValue(s.em_gain_max)
        self.max_accumulations.setValue(s.max_accumulations)

    def settings(self) -> core.MarkerSettings:
        return core.MarkerSettings(
            pressure_scale=self.pressure_scale.currentData() or "",
            temperature_correction=self.t_correction.isChecked(),
            temperature_scale=self.t_scale.currentData() or "",
            fit_function=self.fit_function.currentText(),
            baseline_model=self.baseline.currentData() or "linear",
            fit_range_enabled=self.range_fixed.isChecked(),
            fit_range_start_nm=self.range_start.value(),
            fit_range_end_nm=self.range_end.value(),
            r_squared_min=self.r2_min.value(),
            exposure_time_s=self.exposure.value(),
            accumulations=self.accumulations.value(),
            em_gain_enabled=self.em_gain_enabled.isChecked(),
            em_gain=self.em_gain.value(),
            optimise=self.optimise.isChecked(),
            em_gain_max=self.em_gain_max.value(),
            max_accumulations=self.max_accumulations.value(),
        )


# ── Main window ──────────────────────────────────────────────────────────────

class SeqSpectroWindow(QMainWindow):
    _sig_progress = pyqtSignal(str)
    _sig_pos_started = pyqtSignal(int)
    _sig_pos_done = pyqtSignal(object)
    _sig_finished = pyqtSignal(str, str)
    _sig_return_done = pyqtSignal()
    _sig_return_error = pyqtSignal(str)

    def __init__(self, controller=None, lakeshore_getter=None, debug: bool = False,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sequential Spectroscopic Measurements")
        self.resize(1150, 900)

        self._debug = bool(debug)
        self._owns_controller = controller is None
        if controller is None:
            from utils.stage.control_stage_sim import PM16CControllerSim
            controller = PM16CControllerSim()
            controller.connect()
            self._debug = True
        self.controller = controller
        self._lakeshore_getter = lakeshore_getter or (lambda: None)

        self._positions: list[core.Position] = []
        self._results: dict[int, core.PositionResult] = {}
        self._state = "IDLE"
        self._abort_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._active_lease = None
        self._run: SequenceRun | None = None
        self._rebuilding = False
        self._stop_watcher: StopProgressWatcher | None = None
        self._stop_pending = False      # a stop request is not yet confirmed
        # A stop could not be sent or confirmed: the stage may still be moving.
        # Cleared only by a later confirmed stop or by the operator explicitly
        # closing this window after checking the stage.
        self._stop_unconfirmed = False
        self._close_requested = False   # close once the worker has finished

        self._sig_progress.connect(self._on_progress)
        self._sig_pos_started.connect(self._on_pos_started)
        self._sig_pos_done.connect(self._on_pos_done)
        self._sig_finished.connect(self._on_finished)
        self._sig_return_done.connect(self._on_return_done)
        self._sig_return_error.connect(self._on_return_error)

        self._setup_ui()
        self._load_last_setup()
        if not self._positions:
            self._positions.append(core.Position())
        self._rebuild_table()
        self._set_state("IDLE")

        self._temperature_timer = QTimer(self)
        self._temperature_timer.timeout.connect(self._update_temperature)
        self._temperature_timer.start(1000)
        self._update_temperature()

    # ── UI construction ─────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.setCentralWidget(scroll)
        central = QWidget()
        scroll.setWidget(central)
        root = QVBoxLayout(central)

        # Positions
        pos_group = QGroupBox("Positions (offsets are relative to Pos1)")
        pos_layout = QVBoxLayout(pos_group)
        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.verticalHeader().hide()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(220)
        header = self._table.horizontalHeader()
        for col in range(len(_HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Stretch)
        pos_layout.addWidget(self._table)

        row = QHBoxLayout()
        self._btn_add = QPushButton("Add position")
        self._btn_add.clicked.connect(self._on_add)
        self._btn_remove = QPushButton("Remove selected")
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_save = QPushButton("Save setup…")
        self._btn_save.clicked.connect(self._on_save_setup)
        self._btn_load = QPushButton("Load setup…")
        self._btn_load.clicked.connect(self._on_load_setup)
        for btn in (self._btn_add, self._btn_remove):
            row.addWidget(btn)
        row.addStretch()
        for btn in (self._btn_save, self._btn_load):
            row.addWidget(btn)
        pos_layout.addLayout(row)
        root.addWidget(pos_group)

        # Marker settings (only the markers present in the table are shown)
        self._marker_boxes: dict[str, MarkerSettingsBox] = {}
        settings_row = QHBoxLayout()
        for marker in core.MARKERS:
            box = MarkerSettingsBox(marker)
            box.changed.connect(self._on_settings_changed)
            self._marker_boxes[marker] = box
            settings_row.addWidget(box)
        root.addLayout(settings_row)

        # Output
        out_group = QGroupBox("Output")
        out_layout = QHBoxLayout(out_group)
        out_layout.addWidget(QLabel("Save directory:"))
        self._dir_edit = QLineEdit()
        self._dir_edit.setPlaceholderText("A dated sub-directory is created here for each run")
        out_layout.addWidget(self._dir_edit, 1)
        self._btn_browse = QPushButton("Browse…")
        self._btn_browse.clicked.connect(self._on_browse)
        out_layout.addWidget(self._btn_browse)
        root.addWidget(out_group)

        # Execution
        exec_group = QGroupBox("Execution")
        exec_layout = QVBoxLayout(exec_group)
        start_row = QHBoxLayout()
        self._btn_start = QPushButton("▶  Start sequence")
        self._btn_start.setStyleSheet("font-size: 14px; padding: 8px;")
        self._btn_start.clicked.connect(self._on_start)
        start_row.addWidget(self._btn_start, 1)
        self._temperature_label = QLabel()
        self._temperature_label.setStyleSheet("font-size: 13px; padding: 4px;")
        start_row.addWidget(self._temperature_label)
        exec_layout.addLayout(start_row)

        self._estop_btn = QPushButton("Emergency Stop")
        self._estop_btn.setStyleSheet(
            "QPushButton { background-color: #FF3333; color: white; font-weight: bold;"
            " font-size: 14px; padding: 8px; }"
            "QPushButton:disabled { background-color: #cc9999; }")
        self._estop_btn.clicked.connect(self._on_emergency_stop)
        exec_layout.addWidget(self._estop_btn)

        self._status_label = QLabel("Ready.")
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        exec_layout.addWidget(self._status_label)

        final_row = QHBoxLayout()
        self._btn_stay = QPushButton("Stay here")
        self._btn_stay.clicked.connect(self._on_stay)
        self._btn_return = QPushButton("Return to Pos1")
        self._btn_return.clicked.connect(self._on_return)
        final_row.addWidget(self._btn_stay)
        final_row.addWidget(self._btn_return)
        exec_layout.addLayout(final_row)
        root.addWidget(exec_group)

        # Results
        res_group = QGroupBox("Results")
        res_layout = QFormLayout(res_group)
        self._mean_label = QLabel("—")
        self._mean_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        self._mean_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        res_layout.addRow("Mean pressure (all):", self._mean_label)
        self._marker_mean_labels: dict[str, QLabel] = {}
        self._marker_mean_titles: dict[str, QLabel] = {}
        for marker, spec in core.MARKERS.items():
            title = QLabel(f"Mean pressure ({spec.label}):")
            label = QLabel("—")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            res_layout.addRow(title, label)
            self._marker_mean_titles[marker] = title
            self._marker_mean_labels[marker] = label
        self._run_dir_label = QLabel("")
        self._run_dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        res_layout.addRow("Saved in:", self._run_dir_label)
        note = QLabel("Mean = ΣPᵢ / n over valid points; error = √(Σσᵢ²) / n "
                      "from the FluoRaPressée fit errors.")
        note.setStyleSheet("color: gray;")
        res_layout.addRow(note)
        root.addWidget(res_group)
        self._show_averages(None)

    # ── Table ───────────────────────────────────────────────────────────────

    def _rebuild_table(self) -> None:
        self._rebuilding = True
        self._table.setRowCount(0)
        offsets = core.offsets(self._positions)
        for index, pos in enumerate(self._positions):
            self._table.insertRow(index)
            item = QTableWidgetItem(str(index + 1))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(index, COL_POS, item)

            btn = QPushButton("Record current pos")
            btn.clicked.connect(lambda _c=False, i=index: self._on_record(i))
            self._table.setCellWidget(index, COL_RECORD, btn)

            for col, ch in ((COL_D3, 3), (COL_D4, 4), (COL_D5, 5)):
                if pos.record is None:
                    text = "not recorded"
                elif index == 0:
                    text = "origin"
                elif offsets[index] is None:
                    text = "—"
                else:
                    text = f"{offsets[index][ch] * PULSE_SCALE[ch]:+.2f}"
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if pos.record is not None:
                    cell.setToolTip(f"Recorded Ch{ch} = {pos.record[ch]:+d} pulse")
                self._table.setItem(index, col, cell)

            combo = _no_wheel(QComboBox())
            for key, spec in core.MARKERS.items():
                combo.addItem(spec.label, key)
            combo.setCurrentIndex(combo.findData(pos.marker))
            combo.currentIndexChanged.connect(lambda _x, i=index, c=combo: self._on_marker_changed(i, c))
            self._table.setCellWidget(index, COL_MARKER, combo)

            for col, value, attr in ((COL_LAM0, pos.lambda_0_nm, "lambda_0_nm"),
                                     (COL_T0, pos.t0_k, "t0_k")):
                edit = QLineEdit("" if value is None else f"{value:g}")
                validator = QDoubleValidator(0.0, 2000.0, 4, edit)
                validator.setNotation(QDoubleValidator.Notation.StandardNotation)
                edit.setValidator(validator)
                edit.textChanged.connect(lambda text, i=index, a=attr: self._on_value_edited(i, a, text))
                self._table.setCellWidget(index, col, edit)

            for col in (COL_LAM, COL_P, COL_STATUS):
                self._table.setItem(index, col, QTableWidgetItem(""))
            self._show_result(index)
        self._rebuilding = False
        self._refresh_dependent_widgets()

    def _show_result(self, index: int) -> None:
        result = self._results.get(index + 1)
        lam = p = status = ""
        tip = ""
        if result is not None:
            if result.peak_nm is not None:
                lam = f"{result.peak_nm:.3f}"
            if result.pressure_gpa is not None:
                p = f"{result.pressure_gpa:.3f}"
                if result.pressure_err_gpa is not None:
                    p += f" ± {result.pressure_err_gpa:.3f}"
            status = core.STATUS_LABELS.get(result.status, result.status)
            if result.temperature_k is not None:
                status += f"  (T = {result.temperature_k:.2f} K)"
            if result.em_gain is not None:
                status += f"  (EM {result.em_gain})"
            if result.attempts > 1:
                status += f"  [{result.attempts} attempts]"
            if result.attempts and not result.saved:
                status += "  — not saved"
            tip = "\n".join(t for t in (result.error, result.warning) if t)
            if result.warning:
                status += "  ⚠"
        for col, text in ((COL_LAM, lam), (COL_P, p), (COL_STATUS, status)):
            item = self._table.item(index, col)
            if item is None:
                continue
            item.setText(text)
            item.setToolTip(tip)
            if col == COL_STATUS and result is not None:
                item.setForeground(Qt.GlobalColor.darkGreen if result.is_valid
                                   else Qt.GlobalColor.red)

    def _refresh_dependent_widgets(self) -> None:
        """Marker boxes, T₀ hints and the temperature display follow the table."""
        settings = self._current_settings()
        used = core.markers_in_use(self._positions)
        for marker, box in self._marker_boxes.items():
            box.setVisible(marker in used)
        pos1_recorded = bool(self._positions) and self._positions[0].record is not None
        for index, pos in enumerate(self._positions):
            spec = core.MARKERS[pos.marker]
            lam0 = self._table.cellWidget(index, COL_LAM0)
            if isinstance(lam0, QLineEdit):
                low, high = spec.lambda_0_range_nm
                lam0.setPlaceholderText(f"{low:g}–{high:g}")
            t0 = self._table.cellWidget(index, COL_T0)
            if isinstance(t0, QLineEdit):
                t0.setPlaceholderText("required" if settings[pos.marker].temperature_correction
                                      else "optional")
            btn = self._table.cellWidget(index, COL_RECORD)
            if isinstance(btn, QPushButton):
                allowed = index == 0 or pos1_recorded
                btn.setEnabled(self._state == "IDLE" and allowed)
                btn.setToolTip("" if allowed else "Record Pos1 first")
        self._update_temperature()

    def _current_settings(self) -> dict[str, core.MarkerSettings]:
        return {marker: box.settings() for marker, box in self._marker_boxes.items()}

    # ── Table edits ─────────────────────────────────────────────────────────

    def _on_add(self) -> None:
        marker = self._positions[-1].marker if self._positions else core.MARKER_RUBY
        self._positions.append(core.Position(marker=marker))
        self._results.clear()
        self._rebuild_table()
        self._show_averages(None)

    def _on_remove(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        if row == 0 and len(self._positions) > 1:
            answer = QMessageBox.question(
                self, "Remove Pos1",
                "Removing Pos1 makes Pos2 the new origin. Continue?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        del self._positions[row]
        self._results.clear()
        self._rebuild_table()
        self._show_averages(None)

    def _invalidate_results(self) -> None:
        """Results shown on screen belong to the inputs they were measured
        with; any edit of λ₀, T₀, marker or marker settings clears them.
        The saved files are unaffected."""
        if not self._results and self._mean_label.text() == "—":
            return
        self._results.clear()
        for index in range(self._table.rowCount()):
            self._show_result(index)
        self._show_averages(None)
        self._status_label.setText("Inputs changed — previous results cleared from the screen "
                                   "(the saved files are unchanged).")

    def _on_marker_changed(self, index: int, combo: QComboBox) -> None:
        if self._rebuilding:
            return
        self._positions[index].marker = combo.currentData()
        self._invalidate_results()
        self._refresh_dependent_widgets()

    def _on_value_edited(self, index: int, attr: str, text: str) -> None:
        if self._rebuilding:
            return
        setattr(self._positions[index], attr, _parse_float(text))
        self._invalidate_results()

    def _on_settings_changed(self) -> None:
        if not self._rebuilding:
            self._invalidate_results()
            self._refresh_dependent_widgets()

    def _read_current_stage(self) -> dict[int, int] | None:
        try:
            values = {ch: self.controller.get_ch_pos(ch) for ch in core.CHANNELS}
            if any(v is None for v in values.values()):
                raise RuntimeError("no reply")
            return {ch: int(v) for ch, v in values.items()}
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Read Error",
                                 f"Could not read the Ch3/Ch4/Ch5 positions:\n{exc}")
            return None

    def _on_record(self, index: int) -> None:
        if index > 0 and self._positions[0].record is None:
            QMessageBox.warning(self, "Record Pos1 first",
                                "Record Pos1 before the other positions.")
            return
        others = [i + 1 for i, p in enumerate(self._positions) if i > 0 and p.record is not None]
        if index == 0 and others:
            answer = QMessageBox.question(
                self, "Re-record Pos1",
                "Re-recording Pos1 clears the recorded coordinates of every other "
                "position; all of them must then be recorded again.\n\nContinue?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        current = self._read_current_stage()
        if current is None:
            return
        self._positions[index].record = current
        if index == 0:
            for pos in self._positions[1:]:
                pos.record = None
        self._results.clear()
        self._rebuild_table()
        self._show_averages(None)

    # ── Output directory and setup files ────────────────────────────────────

    def _load_last_dirs(self) -> dict:
        try:
            data = json.loads(_LAST_DIRS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _remember_dir(self, key: str, directory: str) -> None:
        data = self._load_last_dirs()
        data[key] = directory
        try:
            _LOCALDATA_DIR.mkdir(parents=True, exist_ok=True)
            _LAST_DIRS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _default_dir(self, key: str) -> str:
        directory = self._load_last_dirs().get(key, "")
        return directory if directory and os.path.isdir(directory) else os.path.expanduser("~")

    def _on_browse(self) -> None:
        start = self._dir_edit.text().strip()
        if not os.path.isdir(start):
            start = self._default_dir("output")
        directory = QFileDialog.getExistingDirectory(self, "Select save directory", start)
        if directory:
            self._dir_edit.setText(directory)
            self._remember_dir("output", directory)

    def _setup_dict(self) -> dict:
        return {
            "version": 1,
            "positions": [p.to_dict() for p in self._positions],
            "marker_settings": {m: vars(s) for m, s in self._current_settings().items()},
            "output_dir": self._dir_edit.text().strip(),
        }

    def _apply_setup(self, data: dict, *, include_output: bool) -> None:
        positions = data.get("positions")
        if isinstance(positions, list):
            self._positions = [core.Position.from_dict(p) for p in positions if isinstance(p, dict)]
        settings = data.get("marker_settings")
        if isinstance(settings, dict):
            self._rebuilding = True
            for marker, box in self._marker_boxes.items():
                if isinstance(settings.get(marker), dict):
                    box.set_settings(core.MarkerSettings.from_dict(settings[marker]))
            self._rebuilding = False
        if include_output and isinstance(data.get("output_dir"), str):
            self._dir_edit.setText(data["output_dir"])

    def _on_save_setup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save setup", os.path.join(self._default_dir("setup"), "seq_spectro_setup.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._setup_dict(), indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Save Error", str(exc))
            return
        self._remember_dir("setup", os.path.dirname(path))

    def _on_load_setup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load setup", self._default_dir("setup"),
                                              "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a setup file")
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Load Error", f"Could not load the setup:\n{exc}")
            return
        self._remember_dir("setup", os.path.dirname(path))
        self._apply_setup(data, include_output=False)
        self._results.clear()
        self._rebuild_table()
        self._show_averages(None)

    def _load_last_setup(self) -> None:
        try:
            data = json.loads(_LAST_SETUP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict):
            self._apply_setup(data, include_output=True)

    def _save_last_setup(self) -> None:
        try:
            _LOCALDATA_DIR.mkdir(parents=True, exist_ok=True)
            _LAST_SETUP_FILE.write_text(
                json.dumps(self._setup_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ── Temperature display ─────────────────────────────────────────────────

    def _update_temperature(self) -> None:
        if not hasattr(self, "_temperature_label"):
            return
        needed = core.needs_temperature(self._positions, self._current_settings())
        self._temperature_label.setVisible(needed)
        if not needed:
            return
        lakeshore = self._lakeshore_getter()
        cached = lakeshore.get_cached_readings() if lakeshore is not None else {}
        value = cached.get("temperature_b_k") if cached.get("connected") else None
        if isinstance(value, (int, float)) and value > 0:
            self._temperature_label.setText(f"Sample T (LakeShore Ch B): {value:.2f} K")
            self._temperature_label.setStyleSheet("font-size: 13px; padding: 4px;")
        else:
            self._temperature_label.setText("Sample T: LakeShore 335 not connected")
            self._temperature_label.setStyleSheet("font-size: 13px; padding: 4px; color: red;")

    # ── Run ─────────────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self.is_busy:
            QMessageBox.warning(self, "Cannot Start",
                                "The previous stop has not been confirmed. Press Emergency Stop "
                                "again and wait for the confirmation first.")
            return
        settings = self._current_settings()
        errors = core.setup_errors(self._positions, settings, self._dir_edit.text())
        if errors:
            QMessageBox.warning(self, "Cannot Start", "\n".join(f"• {e}" for e in errors))
            return
        try:
            if self._debug:
                reader = SimSpectroReader()
            else:
                reader = FluoraPresseeReader(
                    base_url=online_spectrometer_prefs.get_base_url(),
                    api_key=online_spectrometer_prefs.get_api_key(),
                )
        except ValueError as exc:
            QMessageBox.critical(
                self, "FluoRaPressée",
                f"{exc}\n\nSet the connection in Settings → Online spectrometer.")
            return

        current = self._read_current_stage()
        if current is None:
            return
        record = self._positions[0].record
        delta = ", ".join(f"Ch{ch} {(current[ch] - record[ch]) * PULSE_SCALE[ch]:+.1f} µm"
                          for ch in core.CHANNELS)
        answer = QMessageBox.question(
            self, "Start sequence",
            "The current stage position is used as Pos1.\n"
            f"Current position relative to the recorded Pos1: {delta}\n\n"
            f"Measure {len(self._positions)} position(s) now?")
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            self._active_lease = self.controller.acquire_motion(owner=OWNER, operation="Sequence")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Stage Busy", str(exc))
            return

        self._save_last_setup()
        self._remember_dir("output", self._dir_edit.text().strip())
        self._results.clear()
        for index in range(len(self._positions)):
            self._show_result(index)
        self._show_averages(None)
        self._run_dir_label.setText("")
        self._abort_event.clear()
        self._run = SequenceRun(
            controller=self.controller, motion=self._active_lease, reader=reader,
            lakeshore_getter=self._lakeshore_getter,
            positions=[core.Position.from_dict(p.to_dict()) for p in self._positions],
            settings=settings, output_dir=self._dir_edit.text().strip(),
            abort_event=self._abort_event,
            on_progress=self._sig_progress.emit,
            on_position_started=self._sig_pos_started.emit,
            on_position_done=self._sig_pos_done.emit,
        )
        self._set_state("RUNNING")
        run = self._run

        def worker() -> None:
            try:
                outcome, message = run.run(), run.message
            except BaseException as exc:  # noqa: BLE001 — the UI must leave RUNNING
                outcome, message = "error", f"Unexpected error: {exc}"
            self._sig_finished.emit(outcome, message)

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    @pyqtSlot(str)
    def _on_progress(self, message: str) -> None:
        self._status_label.setText(message)

    @pyqtSlot(int)
    def _on_pos_started(self, number: int) -> None:
        self._table.selectRow(number - 1)

    @pyqtSlot(object)
    def _on_pos_done(self, result) -> None:
        self._results[result.index] = result
        self._show_result(result.index - 1)
        if self._run is not None:
            self._show_averages(self._run)

    @pyqtSlot(str, str)
    def _on_finished(self, outcome: str, message: str) -> None:
        run = self._run
        if run is not None:
            self._show_averages(run)
            if run.run_dir is not None:
                self._run_dir_label.setText(str(run.run_dir))
        self._status_label.setText(message)
        if outcome == "completed":
            self._release_lease()
            self._set_state("IDLE")
        else:
            self._set_state("FINAL_CHOICE")
        if self._close_requested:
            QTimer.singleShot(0, self.close)
            return
        if outcome == "error":
            QMessageBox.critical(self, "Sequence Error", message)
        if run is not None and run.save_error:
            QMessageBox.critical(
                self, "Save Error",
                f"{run.save_error}\n\nThe results on screen may not have been saved completely.")

    def _show_averages(self, run: SequenceRun | None) -> None:
        if run is None:
            self._mean_label.setText("—")
            for marker in core.MARKERS:
                self._marker_mean_titles[marker].setVisible(False)
                self._marker_mean_labels[marker].setVisible(False)
            return
        averages = run.averages()
        self._mean_label.setText(core.format_mean(averages["overall"]))
        for marker in core.MARKERS:
            shown = marker in averages["per_marker"]
            self._marker_mean_titles[marker].setVisible(shown)
            self._marker_mean_labels[marker].setVisible(shown)
            if shown:
                self._marker_mean_labels[marker].setText(
                    core.format_mean(averages["per_marker"][marker]))

    def _release_lease(self) -> None:
        if self._active_lease is not None:
            try:
                self.controller.release_motion(self._active_lease)
            except Exception:
                pass
            self._active_lease = None

    def _on_stay(self) -> None:
        self._release_lease()
        self._set_state("IDLE")
        self._status_label.setText("Sequence finished. Stopped at the current position.")

    def _on_return(self) -> None:
        if self._run is None or self._run.origin is None:
            self._on_stay()
            return
        # An emergency stop revokes the lease; take a fresh one for the return.
        self._release_lease()
        try:
            self._active_lease = self.controller.acquire_motion(owner=OWNER,
                                                                operation="Return to Pos1")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Stage Busy", str(exc))
            return
        self._run.motion = self._active_lease
        self._abort_event.clear()
        self._set_state("RETURNING")
        self._status_label.setText("Returning to Pos1…")
        run = self._run

        def worker() -> None:
            try:
                run.return_to_origin()
                self._sig_return_done.emit()
            except Exception as exc:  # noqa: BLE001
                self._sig_return_error.emit(
                    "Emergency stop — return halted." if self._abort_event.is_set() else str(exc))

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    @pyqtSlot()
    def _on_return_done(self) -> None:
        self._release_lease()
        self._set_state("IDLE")
        self._status_label.setText("Returned to Pos1.")
        if self._close_requested:
            QTimer.singleShot(0, self.close)

    @pyqtSlot(str)
    def _on_return_error(self, message: str) -> None:
        self._set_state("FINAL_CHOICE")
        self._status_label.setText(f"Error: {message}")
        if self._close_requested:
            QTimer.singleShot(0, self.close)
            return
        if not self._abort_event.is_set():
            QMessageBox.critical(self, "Move Error", message)

    def _on_emergency_stop(self) -> None:
        self._request_stop(emergency=True)

    def _request_stop(self, *, emergency: bool) -> None:
        """Ask the controller to stop without blocking the Qt thread, then
        follow the stop until the controller confirms (or fails to confirm)
        that every motor has stopped."""
        self._abort_event.set()
        label = "Emergency stop" if emergency else "Stop"
        try:
            future = (self.controller.request_emergency_stop(source=OWNER) if emergency
                      else self.controller.request_normal_stop(source=OWNER))
        except Exception as exc:  # noqa: BLE001
            self._stop_pending = False
            self._stop_unconfirmed = True
            # Never close automatically after a failed stop.
            self._close_requested = False
            self._update_stop_button()
            self._status_label.setText(f"{label} FAILED: {exc}")
            QMessageBox.critical(
                self, f"{label} Failed",
                f"The stop command could not be sent to the stage controller:\n{exc}\n\n"
                "Stop the stage by hand (front panel) and check the controller.")
            return
        self._stop_pending = True
        self._update_stop_button()
        self._stop_watcher = StopProgressWatcher(self.controller, future, self)
        self._stop_watcher.progress_changed.connect(
            lambda state, l=label: self._on_stop_progress(l, state))
        self._stop_watcher.finished.connect(
            lambda ok, error, l=label: self._on_stop_finished(l, ok, error))

    def _on_stop_progress(self, label: str, state: str) -> None:
        text = {
            "queued": f"{label} requested…",
            "sent_confirming": f"{label} sent. Confirming that all motors have stopped…",
            "confirmed": f"{label}: all motors stopped.",
            "failed": f"{label}: the stop could NOT be confirmed — check the controller.",
        }.get(state)
        if text:
            self._status_label.setText(text)

    def _on_stop_finished(self, label: str, ok: bool, error: str) -> None:
        self._stop_pending = False
        self._stop_unconfirmed = not ok
        self._update_stop_button()
        if ok:
            self._status_label.setText(f"{label}: all motors stopped.")
            if self._close_requested and self._state not in ("RUNNING", "RETURNING"):
                QTimer.singleShot(0, self.close)
            return
        # Keep the window open so the operator sees the failure.
        self._close_requested = False
        self._status_label.setText(f"{label} NOT confirmed: {error}")
        QMessageBox.critical(
            self, f"{label} Not Confirmed",
            f"The controller could not confirm that the stage stopped:\n{error}\n\n"
            "Check the stage and the controller, then press Emergency Stop again "
            "to retry the stop.")

    def _update_stop_button(self) -> None:
        """Emergency Stop stays available while a stop is unconfirmed, so it
        can be retried even after the run has ended."""
        self._estop_btn.setEnabled(self._state in ("RUNNING", "RETURNING")
                                   or self._stop_pending or self._stop_unconfirmed)
        # No new run while the previous stop is unresolved.
        self._btn_start.setEnabled(self._state == "IDLE" and not self._stop_pending
                                   and not self._stop_unconfirmed)

    @property
    def is_busy(self) -> bool:
        """True while the stage may be moving: a run or return is in progress,
        a stop is being confirmed, or a stop could not be sent or confirmed."""
        return (self._state in ("RUNNING", "RETURNING")
                or self._stop_pending or self._stop_unconfirmed)

    # ── State ───────────────────────────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        self._state = state
        idle = state == "IDLE"
        final = state == "FINAL_CHOICE"
        for widget in (self._btn_add, self._btn_remove, self._btn_save, self._btn_load,
                       self._dir_edit, self._btn_browse, *self._marker_boxes.values()):
            widget.setEnabled(idle)
        for index in range(self._table.rowCount()):
            for col in (COL_MARKER, COL_LAM0, COL_T0):
                widget = self._table.cellWidget(index, col)
                if widget is not None:
                    widget.setEnabled(idle)
        self._update_stop_button()
        self._btn_stay.setVisible(final)
        self._btn_return.setVisible(final)
        self._refresh_dependent_widgets()

    def closeEvent(self, event) -> None:
        if self._state in ("RUNNING", "RETURNING"):
            # Never block the Qt thread here: request the stop, and close once
            # the worker reports that it has finished.
            if not self._close_requested:
                answer = QMessageBox.question(
                    self, "Sequence Running",
                    "Stop the sequence and close this window?\n"
                    "The window closes once the stage has stopped.")
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._close_requested = True
                self._request_stop(emergency=False)
            event.ignore()
            return
        if self._stop_pending:
            if not self._close_requested:
                QMessageBox.warning(self, "Still Stopping",
                                    "Wait until the stop has been confirmed before closing.")
            # With a pending close request, _on_stop_finished closes the window.
            event.ignore()
            return
        if self._stop_unconfirmed:
            answer = QMessageBox.question(
                self, "Stop Not Confirmed",
                "The last stop was NOT confirmed by the stage controller, so the stage "
                "may still be moving.\n\nHave you checked that the stage has stopped? "
                "Close this window anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._stop_unconfirmed = False
        self._temperature_timer.stop()
        self._release_lease()
        self._save_last_setup()
        if self._owns_controller:
            try:
                self.controller.disconnect()
            except Exception:
                pass
        event.accept()


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    # Standalone runs use the simulated stage and a simulated FluoRaPressée.
    app = QApplication(sys.argv)
    win = SeqSpectroWindow()
    win.show()
    sys.exit(app.exec())
