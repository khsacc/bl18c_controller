"""
StepEditorDialog — add / edit a single sequence step.

ForLoopAction itself is NOT available here — loops are created/edited via
ForLoopEditorDialog (see ui/for_loop_editor.py) and TimelineWidget's
"+ Add Loop" button. What IS available here is the `available_loop_vars`
parameter: when a step is being added/edited inside a loop, the
loop-var-capable fields (move_absolute.position, move_relative.delta,
set_pressure.pressure, set_and_wait_pressure.pressure, set_temperature.value_k)
show a Constant / Loop variable toggle.
See SPEC.md "Visual Editor での for ループ編集（Phase 2）".
"""
from __future__ import annotations

from typing import Callable, NamedTuple

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..actions import (
    Action,
    AllHeatersOffAction,
    FollowSampleAction,
    FpdOutMicroscopeInAction,
    LogAction,
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    SetAndWaitPressureAction,
    SetControlModeAction,
    SetHeaterAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitAction,
    WaitPressureAction,
    WaitTemperatureAction,
)
from apps.stage_fpd_scope.stage_settings import load_stage_settings

# ── Device → operations ────────────────────────────────────────────────────

_DEVICE_OPS: dict[str, list[str]] = {
    "General": ["wait", "log_message"],
    "Stage": [
        "microscope_out_and_fpd_in",
        "fpd_out_and_microscope_in",
        "move_absolute",
        "move_relative",
        "set_speed",
        "normal_stop",
        "emergency_stop",
    ],
    "Interactive Camera": [
        "save_snapshot",
        "start_following",
        "stop_following",
        "follow_sample_position",
        "save_reference_image",
    ],
    "FPD (Rad-icon2022)": ["take_xrd", "take_dark"],
    "PACE5000": ["set_and_wait_pressure", "set_pressure", "wait_pressure", "set_control_mode"],
    "LakeShore": ["set_temperature", "wait_temperature", "set_heater", "all_heaters_off"],
}

# Flat ordered list so _stack indices are stable
_ALL_OPS: list[str] = [op for ops in _DEVICE_OPS.values() for op in ops]


def _action_to_device_op(action: Action) -> tuple[str, str] | None:
    if isinstance(action, StageAction):
        return ("Stage", action.operation)
    if isinstance(action, MicroscopeOutFpdInAction):
        return ("Stage", "microscope_out_and_fpd_in")
    if isinstance(action, FpdOutMicroscopeInAction):
        return ("Stage", "fpd_out_and_microscope_in")
    if isinstance(action, SetPressureAction):
        return ("PACE5000", "set_pressure")
    if isinstance(action, WaitPressureAction):
        return ("PACE5000", "wait_pressure")
    if isinstance(action, SetAndWaitPressureAction):
        return ("PACE5000", "set_and_wait_pressure")
    if isinstance(action, SetControlModeAction):
        return ("PACE5000", "set_control_mode")
    if isinstance(action, SetTemperatureAction):
        return ("LakeShore", "set_temperature")
    if isinstance(action, WaitTemperatureAction):
        return ("LakeShore", "wait_temperature")
    if isinstance(action, SetHeaterAction):
        return ("LakeShore", "set_heater")
    if isinstance(action, AllHeatersOffAction):
        return ("LakeShore", "all_heaters_off")
    if isinstance(action, TakeXrdAction):
        return ("FPD (Rad-icon2022)", "take_xrd")
    if isinstance(action, TakeDarkAction):
        return ("FPD (Rad-icon2022)", "take_dark")
    if isinstance(action, SaveReferenceImageAction):
        return ("Interactive Camera", "save_reference_image")
    if isinstance(action, SaveSnapshotAction):
        return ("Interactive Camera", "save_snapshot")
    if isinstance(action, StartFollowingAction):
        return ("Interactive Camera", "start_following")
    if isinstance(action, StopFollowingAction):
        return ("Interactive Camera", "stop_following")
    if isinstance(action, FollowSampleAction):
        return ("Interactive Camera", "follow_sample_position")
    if isinstance(action, WaitAction):
        return ("General", "wait")
    if isinstance(action, LogAction):
        return ("General", "log_message")
    return None


# ── Page helper ─────────────────────────────────────────────────────────────

class _Page(NamedTuple):
    widget: QWidget
    fill: Callable[[Action], None]   # pre-fill from an existing action
    build: Callable[[], "Action | None"]  # None = validation failed


# ── Widget factories ─────────────────────────────────────────────────────────

def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


def _ch_spin() -> QSpinBox:
    s = _no_wheel(QSpinBox())
    s.setRange(1, 11)
    s.setValue(4)
    return s


def _int_spin(lo: int = -9_999_999, hi: int = 9_999_999, v: int = 0) -> QSpinBox:
    s = _no_wheel(QSpinBox())
    s.setRange(lo, hi)
    s.setValue(v)
    return s


def _float_spin(lo: float = 0.0, hi: float = 9_999_999.0,
                v: float = 0.0, dec: int = 3) -> QDoubleSpinBox:
    s = _no_wheel(QDoubleSpinBox())
    s.setDecimals(dec)
    s.setRange(lo, hi)
    s.setValue(v)
    return s


def _combo(*items: str, default: str | None = None) -> QComboBox:
    cb = _no_wheel(QComboBox())
    cb.addItems(items)
    if default is not None:
        cb.setCurrentText(default)
    return cb


def _opt_int(v: int = 0) -> tuple[QCheckBox, QSpinBox, QWidget]:
    """Use-default checkbox + int spinbox packed in a row widget."""
    chk = QCheckBox("Use latest stage settings")
    chk.setChecked(True)
    spin = _int_spin(v=v)
    spin.setEnabled(False)
    spin.setStyleSheet(
        "QSpinBox { background-color: white; }"
        "QSpinBox:disabled { background-color: #d8d8d8; color: #606060; }"
    )
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(spin)
    hl.addWidget(chk)
    chk.toggled.connect(lambda c: spin.setEnabled(not c))
    return chk, spin, row


def _opt_speed(default: str = "M") -> tuple[QCheckBox, QComboBox, QWidget]:
    """Keep-current-speed checkbox + H/M/L combo packed in a row widget."""
    chk = QCheckBox("Keep current speed")
    chk.setChecked(True)
    combo = _combo("H", "M", "L", default=default)
    combo.setEnabled(False)
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(combo)
    hl.addWidget(chk)
    chk.toggled.connect(lambda c: combo.setEnabled(not c))
    return chk, combo, row


_DISABLED_BG = "background-color: #d8d8d8;"


def _opt_float(lo: float = 0.0, hi: float = 9999.0,
               v: float = 0.0, dec: int = 3) -> tuple[QCheckBox, QDoubleSpinBox, QWidget]:
    chk = QCheckBox("Use Global Settings")
    chk.setChecked(True)
    spin = _float_spin(lo, hi, v, dec)
    spin.setEnabled(False)
    spin.setStyleSheet(_DISABLED_BG)
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(spin)
    hl.addWidget(chk)

    def _toggle_float(checked: bool):
        spin.setEnabled(not checked)
        spin.setStyleSheet(_DISABLED_BG if checked else "")

    chk.toggled.connect(_toggle_float)
    return chk, spin, row


def _val_or_var(
    spin: QWidget, available_vars: tuple[str, ...],
) -> tuple[QRadioButton | None, QRadioButton | None, QComboBox | None, QWidget]:
    """Wrap a constant-value spin widget with a Constant / Loop variable
    toggle, for fields that support a loop-variable reference (float | str).

    When `available_vars` is empty (step is being added/edited outside any
    loop), returns (None, None, None, spin) unchanged — callers see exactly
    today's constant-only behaviour, and _get_val_or_var / _set_val_or_var
    degrade gracefully to plain spin.value()/setValue().
    """
    if not available_vars:
        return None, None, None, spin

    rb_const = QRadioButton("Constant")
    rb_var = QRadioButton("Loop variable")
    rb_const.setChecked(True)
    combo = _combo(*available_vars)
    combo.setEnabled(False)

    row = QWidget()
    grp = QButtonGroup(row)
    grp.addButton(rb_const)
    grp.addButton(rb_var)
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(rb_const)
    hl.addWidget(spin)
    hl.addWidget(rb_var)
    hl.addWidget(combo)

    def _toggle(checked: bool) -> None:
        spin.setEnabled(not checked)
        combo.setEnabled(checked)

    rb_var.toggled.connect(_toggle)
    return rb_const, rb_var, combo, row


def _get_val_or_var(rb_var: QRadioButton | None, combo: QComboBox | None, spin) -> float | str:
    if rb_var is not None and rb_var.isChecked():
        return combo.currentText()
    return spin.value()


def _set_val_or_var(
    rb_const: QRadioButton | None,
    rb_var: QRadioButton | None,
    combo: QComboBox | None,
    spin,
    value: float | str,
) -> None:
    if isinstance(value, str) and rb_var is not None:
        rb_var.setChecked(True)
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        return
    if rb_const is not None:
        rb_const.setChecked(True)
    if isinstance(value, (int, float)):
        spin.setValue(value)


def _opt_str(placeholder: str = "") -> tuple[QCheckBox, QLineEdit, QWidget]:
    chk = QCheckBox("Use Global Settings")
    chk.setChecked(True)
    le = QLineEdit()
    le.setPlaceholderText(placeholder)
    le.setEnabled(False)
    le.setStyleSheet(_DISABLED_BG)
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(le)
    hl.addWidget(chk)

    def _toggle_str(checked: bool):
        le.setEnabled(not checked)
        le.setStyleSheet(_DISABLED_BG if checked else "")

    chk.toggled.connect(_toggle_str)
    return chk, le, row


def _dur_row(default_min: float = 5.0, unit_default: str = "min",
             ) -> tuple[QDoubleSpinBox, QComboBox, QWidget]:
    spin = _float_spin(0.0, 99999.0, default_min, 1)
    unit = _combo("min", "s", default=unit_default)
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(spin)
    hl.addWidget(unit)
    return spin, unit, row


def _radicon_exposure_s() -> float:
    """Return the current Rad-icon exposure in seconds, or 60.0 if the window is not open."""
    for w in QApplication.topLevelWidgets():
        if type(w).__name__ == "RadiconWindow":
            spin = getattr(w, "_exp_spin", None)
            if spin is not None:
                return float(spin.value())
    return 60.0


def _empty_page(text: str, build_fn: Callable[[], Action]) -> _Page:
    w = QWidget()
    vl = QVBoxLayout(w)
    vl.addWidget(QLabel(text))
    vl.addStretch()
    return _Page(w, lambda _: None, build_fn)


# ── Per-operation page factories ─────────────────────────────────────────────

def _page_move_absolute(available_loop_vars: tuple[str, ...] = ()) -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    ch = _ch_spin()
    pos = _int_spin()
    rb_const, rb_var, var_combo, pos_row = _val_or_var(pos, available_loop_vars)
    chk_speed, speed_combo, row_speed = _opt_speed()
    form.addRow("Channel:", ch)
    form.addRow("Position (pulses):", pos_row)
    form.addRow("Speed:", row_speed)

    def fill(a: StageAction):
        ch.setValue(a.ch)
        _set_val_or_var(rb_const, rb_var, var_combo, pos, a.value)
        if a.speed:
            chk_speed.setChecked(False)
            speed_combo.setCurrentText(a.speed)
        else:
            chk_speed.setChecked(True)

    def build() -> StageAction:
        return StageAction(
            operation="move_absolute", ch=ch.value(),
            value=_get_val_or_var(rb_var, var_combo, pos),
            speed=None if chk_speed.isChecked() else speed_combo.currentText(),
        )

    return _Page(w, fill, build)


def _page_move_relative(available_loop_vars: tuple[str, ...] = ()) -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    ch = _ch_spin()
    delta = _int_spin()
    rb_const, rb_var, var_combo, delta_row = _val_or_var(delta, available_loop_vars)
    chk_speed, speed_combo, row_speed = _opt_speed()
    form.addRow("Channel:", ch)
    form.addRow("Delta (pulses):", delta_row)
    form.addRow("Speed:", row_speed)

    def fill(a: StageAction):
        ch.setValue(a.ch)
        _set_val_or_var(rb_const, rb_var, var_combo, delta, a.value)
        if a.speed:
            chk_speed.setChecked(False)
            speed_combo.setCurrentText(a.speed)
        else:
            chk_speed.setChecked(True)

    def build() -> StageAction:
        return StageAction(
            operation="move_relative", ch=ch.value(),
            value=_get_val_or_var(rb_var, var_combo, delta),
            speed=None if chk_speed.isChecked() else speed_combo.currentText(),
        )

    return _Page(w, fill, build)


def _page_set_speed() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    ch = _ch_spin()
    speed = _combo("H", "M", "L")
    form.addRow("Channel:", ch)
    form.addRow("Speed:", speed)

    def fill(a: StageAction):
        ch.setValue(a.ch)
        if a.speed:
            speed.setCurrentText(a.speed)

    def build() -> StageAction:
        return StageAction(operation="set_speed", ch=ch.value(), speed=speed.currentText())

    return _Page(w, fill, build)


def _page_microscope_out_fpd_in() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    speed = _combo("H", "M", "L")
    chk_out, spin_out, row_out = _opt_int()
    chk_in, spin_in, row_in = _opt_int()
    form.addRow("Speed:", speed)
    form.addRow("Microscope OUT pos:", row_out)
    form.addRow("FPD IN pos:", row_in)

    s = load_stage_settings()
    def_out = int(s.get("ch8_out", 0))
    def_in = int(s.get("det_in", 0))
    spin_out.setValue(def_out)
    spin_in.setValue(def_in)
    chk_out.toggled.connect(lambda c: spin_out.setValue(def_out) if c else None)
    chk_in.toggled.connect(lambda c: spin_in.setValue(def_in) if c else None)

    def fill(a: MicroscopeOutFpdInAction):
        speed.setCurrentText(a.speed)
        if a.microscope_out_pos is not None:
            chk_out.setChecked(False)
            spin_out.setValue(a.microscope_out_pos)
        else:
            chk_out.setChecked(True)
        if a.fpd_in_pos is not None:
            chk_in.setChecked(False)
            spin_in.setValue(a.fpd_in_pos)
        else:
            chk_in.setChecked(True)

    def build() -> MicroscopeOutFpdInAction:
        return MicroscopeOutFpdInAction(
            microscope_out_pos=None if chk_out.isChecked() else spin_out.value(),
            fpd_in_pos=None if chk_in.isChecked() else spin_in.value(),
            speed=speed.currentText(),
        )

    return _Page(w, fill, build)


def _page_fpd_out_microscope_in() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    speed = _combo("H", "M", "L")
    chk_out, spin_out, row_out = _opt_int()
    chk_in, spin_in, row_in = _opt_int()
    form.addRow("Speed:", speed)
    form.addRow("FPD OUT pos:", row_out)
    form.addRow("Microscope IN pos:", row_in)

    s = load_stage_settings()
    def_out = int(s.get("det_out", 0))
    def_in = int(s.get("ch8_in", 0))
    spin_out.setValue(def_out)
    spin_in.setValue(def_in)
    chk_out.toggled.connect(lambda c: spin_out.setValue(def_out) if c else None)
    chk_in.toggled.connect(lambda c: spin_in.setValue(def_in) if c else None)

    def fill(a: FpdOutMicroscopeInAction):
        speed.setCurrentText(a.speed)
        if a.fpd_out_pos is not None:
            chk_out.setChecked(False)
            spin_out.setValue(a.fpd_out_pos)
        else:
            chk_out.setChecked(True)
        if a.microscope_in_pos is not None:
            chk_in.setChecked(False)
            spin_in.setValue(a.microscope_in_pos)
        else:
            chk_in.setChecked(True)

    def build() -> FpdOutMicroscopeInAction:
        return FpdOutMicroscopeInAction(
            fpd_out_pos=None if chk_out.isChecked() else spin_out.value(),
            microscope_in_pos=None if chk_in.isChecked() else spin_in.value(),
            speed=speed.currentText(),
        )

    return _Page(w, fill, build)


def _page_set_pressure(available_loop_vars: tuple[str, ...] = ()) -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    pressure = _float_spin(0.0, 9999.0, 0.0, 4)
    rb_const, rb_var, var_combo, pressure_row = _val_or_var(pressure, available_loop_vars)
    unit = _combo("MPa", "Bar")
    spin_rate = _float_spin(0.0, 100.0, 0.05, 4)
    rate_unit = _combo("MPa/min", "Bar/min")
    form.addRow("Pressure:", pressure_row)
    form.addRow("Unit:", unit)
    form.addRow("Rate:", spin_rate)
    form.addRow("Rate unit:", rate_unit)

    def fill(a: SetPressureAction):
        _set_val_or_var(rb_const, rb_var, var_combo, pressure, a.pressure)
        unit.setCurrentText(a.unit)
        spin_rate.setValue(a.rate)
        rate_unit.setCurrentText(a.rate_unit)

    def build() -> SetPressureAction:
        return SetPressureAction(
            pressure=_get_val_or_var(rb_var, var_combo, pressure), unit=unit.currentText(),
            rate=spin_rate.value(), rate_unit=rate_unit.currentText(),
        )

    return _Page(w, fill, build)


def _page_wait_pressure() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    tol = _float_spin(0.0, 10.0, 0.001, 4)
    unit = _combo("MPa", "Bar")
    form.addRow("Tolerance:", tol)
    form.addRow("Unit:", unit)

    def fill(a: WaitPressureAction):
        tol.setValue(a.tol)
        unit.setCurrentText(a.unit)

    def build() -> WaitPressureAction:
        return WaitPressureAction(tol=tol.value(), unit=unit.currentText())

    return _Page(w, fill, build)


def _page_set_and_wait_pressure(available_loop_vars: tuple[str, ...] = ()) -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    pressure = _float_spin(0.0, 9999.0, 0.0, 4)
    rb_const, rb_var, var_combo, pressure_row = _val_or_var(pressure, available_loop_vars)
    unit = _combo("MPa", "Bar")
    spin_rate = _float_spin(0.0, 100.0, 0.05, 4)
    rate_unit = _combo("MPa/min", "Bar/min")
    tol = _float_spin(0.0, 10.0, 0.001, 4)
    form.addRow("Pressure:", pressure_row)
    form.addRow("Unit (pressure & tol):", unit)
    form.addRow("Rate:", spin_rate)
    form.addRow("Rate unit:", rate_unit)
    form.addRow("Tolerance:", tol)

    def fill(a: SetAndWaitPressureAction):
        _set_val_or_var(rb_const, rb_var, var_combo, pressure, a.pressure)
        unit.setCurrentText(a.unit)
        spin_rate.setValue(a.rate)
        rate_unit.setCurrentText(a.rate_unit)
        tol.setValue(a.tol)

    def build() -> SetAndWaitPressureAction:
        return SetAndWaitPressureAction(
            pressure=_get_val_or_var(rb_var, var_combo, pressure), unit=unit.currentText(),
            rate=spin_rate.value(), rate_unit=rate_unit.currentText(), tol=tol.value(),
        )

    return _Page(w, fill, build)


def _page_set_control_mode() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    rb_control = QRadioButton("Control")
    rb_measure = QRadioButton("Measure")
    rb_control.setChecked(True)
    grp = QButtonGroup(w)
    grp.addButton(rb_control)
    grp.addButton(rb_measure)
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(rb_control)
    hl.addWidget(rb_measure)
    form.addRow("Mode:", row)

    def fill(a: SetControlModeAction):
        rb_control.setChecked(a.enabled)
        rb_measure.setChecked(not a.enabled)

    def build() -> SetControlModeAction:
        return SetControlModeAction(enabled=rb_control.isChecked())

    return _Page(w, fill, build)


def _page_set_temperature(available_loop_vars: tuple[str, ...] = ()) -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    value = _float_spin(0.0, 1500.0, 300.0, 1)
    rb_const, rb_var, var_combo, value_row = _val_or_var(value, available_loop_vars)
    spin_ramp = _float_spin(0.0, 100.0, 5.0, 2)
    form.addRow("Temperature (K):", value_row)
    form.addRow("Ramp rate (K/min):", spin_ramp)

    def fill(a: SetTemperatureAction):
        _set_val_or_var(rb_const, rb_var, var_combo, value, a.value_k)
        spin_ramp.setValue(a.ramp_rate)

    def build() -> SetTemperatureAction:
        return SetTemperatureAction(
            value_k=_get_val_or_var(rb_var, var_combo, value), ramp_rate=spin_ramp.value(),
        )

    return _Page(w, fill, build)


def _page_wait_temperature() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    tol = _float_spin(0.0, 100.0, 0.1, 2)
    form.addRow("Tolerance (K):", tol)

    def fill(a: WaitTemperatureAction):
        tol.setValue(a.tol_k)

    def build() -> WaitTemperatureAction:
        return WaitTemperatureAction(tol_k=tol.value())

    return _Page(w, fill, build)


def _page_set_heater() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    ri = _combo("Off", "Low", "Medium", "High")
    form.addRow("Range:", ri)

    def fill(a: SetHeaterAction):
        ri.setCurrentIndex(a.range_index)

    def build() -> SetHeaterAction:
        return SetHeaterAction(range_index=ri.currentIndex())

    return _Page(w, fill, build)


def _page_take_xrd() -> _Page:  # noqa: PLR0915

    class _TakeXrdWidget(QWidget):
        validity_changed = pyqtSignal(bool)

    w = _TakeXrdWidget()
    outer = QVBoxLayout(w)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(6)

    # ── Top-level: Use Global Settings ────────────────────────────────────
    chk_use_global = QCheckBox("Use Global Settings")
    chk_use_global.setChecked(True)
    outer.addWidget(chk_use_global)

    # File prefix is not part of GlobalXrdSettings; it belongs to each
    # TakeXrdAction and should stay visible even when acquisition parameters
    # are inherited from the XRD Settings panel.
    prefix_form = QFormLayout()
    prefix_form.setContentsMargins(0, 0, 0, 0)
    prefix_edit = QLineEdit("scan")
    prefix_edit.setToolTip(
        "Prefix used for saved XRD files. The final name is "
        "<prefix>_<timestamp>_<binning>.tif."
    )
    prefix_form.addRow("File prefix:", prefix_edit)
    outer.addLayout(prefix_form)

    # ── Step-specific container (visible only when use_global=False) ───────
    step_container = QWidget()
    step_vl = QVBoxLayout(step_container)
    step_vl.setContentsMargins(0, 4, 0, 0)
    step_vl.setSpacing(4)
    outer.addWidget(step_container)
    step_container.setVisible(False)

    def _browse_btn() -> QPushButton:
        b = QPushButton("…")
        b.setFixedWidth(28)
        return b

    def _file_row(placeholder: str, btn: QPushButton) -> tuple[QLineEdit, QWidget]:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        row_w = QWidget()
        hl = QHBoxLayout(row_w)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(edit, stretch=1)
        hl.addWidget(btn)
        return edit, row_w

    # ──────────────────────────────────────────────────────────────────────
    # Acquisition settings sub-section
    # ──────────────────────────────────────────────────────────────────────
    chk_acq = QCheckBox("Change Acquisition Settings")
    step_vl.addWidget(chk_acq)

    acq_container = QWidget()
    acq_form = QFormLayout(acq_container)
    acq_form.setSpacing(4)
    acq_form.setContentsMargins(16, 2, 0, 4)
    step_vl.addWidget(acq_container)
    acq_container.setVisible(False)

    # Exposure
    exp_spin = QSpinBox()
    exp_spin.setRange(1, 60000)
    exp_spin.setSuffix(" ms")
    exp_spin.setValue(1000)
    acq_form.addRow("Exposure:", exp_spin)

    # Save toggle
    save_chk = QCheckBox("Save to file")
    save_chk.setChecked(True)
    acq_form.addRow("", save_chk)

    # Save directory
    savedir_btn = _browse_btn()
    savedir_edit, savedir_file_row = _file_row("directory path", savedir_btn)
    savedir_btn.clicked.connect(
        lambda: (d := QFileDialog.getExistingDirectory(
            w, "Save Directory", savedir_edit.text() or ""))
        and savedir_edit.setText(d)
    )
    acq_form.addRow("Save dir:", savedir_file_row)

    # Flip
    flip_v_val = QCheckBox("Vertical")
    flip_v_val.setChecked(True)
    flip_h_val = QCheckBox("Horizontal")
    flip_row = QWidget()
    _hl_flip = QHBoxLayout(flip_row)
    _hl_flip.setContentsMargins(0, 0, 0, 0)
    _hl_flip.addWidget(flip_v_val)
    _hl_flip.addWidget(flip_h_val)
    _hl_flip.addStretch()
    acq_form.addRow("Flip:", flip_row)

    # Dark correction
    dark_en_val = QCheckBox("Enable")
    dark_btn = _browse_btn()
    dark_edit, dark_file_row = _file_row("dark .tif file", dark_btn)
    dark_btn.clicked.connect(
        lambda: (p := QFileDialog.getOpenFileName(
            w, "Dark Image", dark_edit.text() or "",
            "TIFF (*.tif *.tiff);;All (*)")[0]) and dark_edit.setText(p)
    )
    dark_en_row = QWidget()
    _hl_dark = QHBoxLayout(dark_en_row)
    _hl_dark.setContentsMargins(0, 0, 0, 0)
    _hl_dark.addWidget(dark_en_val)
    _hl_dark.addStretch()
    acq_form.addRow("Dark:", dark_en_row)
    acq_form.addRow("Dark file:", dark_file_row)

    # Defect correction
    defect_en_val = QCheckBox("Enable")
    defect_en_val.setChecked(True)
    defect_kernel_combo = QComboBox()
    defect_kernel_combo.addItems(["3×3", "4×4", "5×5", "6×6"])
    defect_btn = _browse_btn()
    defect_edit, defect_file_row = _file_row("XFPCAP01 defect .txt file", defect_btn)
    defect_btn.clicked.connect(
        lambda: (p := QFileDialog.getOpenFileName(
            w, "Defect File", defect_edit.text() or "",
            "Text (*.txt);;All (*)")[0]) and defect_edit.setText(p)
    )
    defect_en_row = QWidget()
    _hl_defect = QHBoxLayout(defect_en_row)
    _hl_defect.setContentsMargins(0, 0, 0, 0)
    _hl_defect.addWidget(defect_en_val)
    _hl_defect.addWidget(defect_kernel_combo)
    _hl_defect.addStretch()
    acq_form.addRow("Defect:", defect_en_row)
    acq_form.addRow("Defect file:", defect_file_row)

    # ──────────────────────────────────────────────────────────────────────
    # Oscillation settings sub-section
    # ──────────────────────────────────────────────────────────────────────
    chk_osc = QCheckBox("Change Oscillation Settings")
    step_vl.addWidget(chk_osc)

    osc_container = QWidget()
    osc_form = QFormLayout(osc_container)
    osc_form.setSpacing(4)
    osc_form.setContentsMargins(16, 2, 0, 4)
    step_vl.addWidget(osc_container)
    osc_container.setVisible(False)

    osc_en_val = QCheckBox("Oscillate sample stage (Ch11)")
    osc_form.addRow("Oscillation:", osc_en_val)

    osc_pos_row = QWidget()
    osc_pos_layout = QHBoxLayout(osc_pos_row)
    osc_pos_layout.setContentsMargins(0, 0, 0, 0)
    osc_pos_layout.setSpacing(6)
    osc_pos_a_spin = QDoubleSpinBox()
    osc_pos_a_spin.setRange(-180.0, 180.0)
    osc_pos_a_spin.setValue(-5.0)
    osc_pos_a_spin.setSuffix("°")
    osc_pos_a_spin.setDecimals(2)
    osc_pos_b_spin = QDoubleSpinBox()
    osc_pos_b_spin.setRange(-180.0, 180.0)
    osc_pos_b_spin.setValue(20.0)
    osc_pos_b_spin.setSuffix("°")
    osc_pos_b_spin.setDecimals(2)
    osc_pos_layout.addWidget(QLabel("A:"))
    osc_pos_layout.addWidget(osc_pos_a_spin)
    osc_pos_layout.addSpacing(8)
    osc_pos_layout.addWidget(QLabel("B:"))
    osc_pos_layout.addWidget(osc_pos_b_spin)
    osc_pos_layout.addStretch()
    osc_form.addRow("Pos (deg):", osc_pos_row)

    osc_ds_row = QWidget()
    osc_ds_layout = QHBoxLayout(osc_ds_row)
    osc_ds_layout.setContentsMargins(0, 0, 0, 0)
    osc_ds_layout.setSpacing(6)
    osc_dwell_spin = QSpinBox()
    osc_dwell_spin.setRange(0, 60000)
    osc_dwell_spin.setValue(0)
    osc_dwell_spin.setSuffix(" ms")
    osc_dwell_spin.setFixedWidth(80)
    osc_ds_layout.addWidget(QLabel("Dwell:"))
    osc_ds_layout.addWidget(osc_dwell_spin)
    osc_ds_layout.addSpacing(12)
    osc_ds_layout.addWidget(QLabel("Speed:"))
    osc_speed_group = QButtonGroup(w)
    for spd in ("H", "M", "L"):
        rb = QRadioButton(spd)
        osc_speed_group.addButton(rb)
        osc_ds_layout.addWidget(rb)
        if spd == "M":
            rb.setChecked(True)
    osc_ds_layout.addStretch()
    osc_form.addRow("Dwell / Speed:", osc_ds_row)

    # ── Warning label ──────────────────────────────────────────────────────
    warn_label = QLabel("⚠ Please select at least one of the options above.")
    warn_label.setStyleSheet("color: #c0392b; font-weight: bold;")
    warn_label.setWordWrap(True)
    step_vl.addWidget(warn_label)
    warn_label.setVisible(False)

    outer.addStretch()

    # ── Visibility / validity state ───────────────────────────────────────
    def _update_state(_=None) -> None:
        use_global = chk_use_global.isChecked()
        step_container.setVisible(not use_global)
        acq_container.setVisible(chk_acq.isChecked() and not use_global)
        osc_container.setVisible(chk_osc.isChecked() and not use_global)
        warn_label.setVisible(
            not use_global and not chk_acq.isChecked() and not chk_osc.isChecked()
        )
        w.validity_changed.emit(
            use_global or chk_acq.isChecked() or chk_osc.isChecked()
        )

    chk_use_global.toggled.connect(_update_state)
    chk_acq.toggled.connect(_update_state)
    chk_osc.toggled.connect(_update_state)
    w.refresh_validity = _update_state  # called by dialog on page switch

    # ── fill / build ──────────────────────────────────────────────────────

    def fill(a: TakeXrdAction) -> None:
        has_acq = (
            a.exposure_ms is not None
            or a.save_dir is not None
            or a.flip_v is not None
            or a.dark_enabled is not None
            or a.defect_enabled is not None
        )
        has_osc = a.oscillate is not None
        chk_use_global.setChecked(not has_acq and not has_osc)
        chk_acq.setChecked(has_acq)
        chk_osc.setChecked(has_osc)

        if a.exposure_ms is not None:
            exp_spin.setValue(a.exposure_ms)
        save_chk.setChecked(a.save)
        prefix_edit.setText(a.prefix)
        if a.save_dir is not None:
            savedir_edit.setText(a.save_dir)
        if a.flip_v is not None:
            flip_v_val.setChecked(a.flip_v)
        if a.flip_h is not None:
            flip_h_val.setChecked(a.flip_h)
        if a.dark_enabled is not None:
            dark_en_val.setChecked(a.dark_enabled)
            dark_edit.setText(a.dark_file or "")
        if a.defect_enabled is not None:
            defect_en_val.setChecked(a.defect_enabled)
            defect_edit.setText(a.defect_file or "")
            if a.defect_kernel is not None and 3 <= a.defect_kernel <= 6:
                defect_kernel_combo.setCurrentIndex(a.defect_kernel - 3)
        if a.oscillate is not None:
            osc_en_val.setChecked(a.oscillate)
        if a.osc_pos_a_deg is not None:
            osc_pos_a_spin.setValue(a.osc_pos_a_deg)
        if a.osc_pos_b_deg is not None:
            osc_pos_b_spin.setValue(a.osc_pos_b_deg)
        if a.osc_dwell_ms is not None:
            osc_dwell_spin.setValue(a.osc_dwell_ms)
        if a.osc_speed is not None:
            for btn in osc_speed_group.buttons():
                if btn.text() == a.osc_speed:
                    btn.setChecked(True)
                    break
        _update_state()

    def _get_osc_speed() -> str:
        return next(
            (btn.text() for btn in osc_speed_group.buttons() if btn.isChecked()), "M"
        )

    def build() -> TakeXrdAction | None:
        use_global = chk_use_global.isChecked()
        has_acq = chk_acq.isChecked()
        has_osc = chk_osc.isChecked()
        if not use_global and not has_acq and not has_osc:
            return None
        return TakeXrdAction(
            exposure_ms=exp_spin.value() if has_acq else None,
            save=save_chk.isChecked() if has_acq else True,
            prefix=prefix_edit.text().strip() or "scan",
            save_dir=(savedir_edit.text().strip() or None) if has_acq else None,
            flip_v=flip_v_val.isChecked() if has_acq else None,
            flip_h=flip_h_val.isChecked() if has_acq else None,
            dark_enabled=dark_en_val.isChecked() if has_acq else None,
            dark_file=(dark_edit.text().strip() or None) if has_acq else None,
            defect_enabled=defect_en_val.isChecked() if has_acq else None,
            defect_file=(defect_edit.text().strip() or None) if has_acq else None,
            defect_kernel=int(defect_kernel_combo.currentText()[0]) if has_acq else None,
            oscillate=osc_en_val.isChecked() if has_osc else None,
            osc_pos_a_deg=osc_pos_a_spin.value() if has_osc else None,
            osc_pos_b_deg=osc_pos_b_spin.value() if has_osc else None,
            osc_dwell_ms=osc_dwell_spin.value() if has_osc else None,
            osc_speed=_get_osc_speed() if has_osc else None,
        )

    return _Page(w, fill, build)


def _page_take_dark() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    exp = QSpinBox()
    exp.setRange(1, 60_000)
    exp.setValue(1000)
    exp.setSuffix(" ms")
    form.addRow("Exposure:", exp)

    def fill(a: TakeDarkAction):
        exp.setValue(a.exposure_ms)

    def build() -> TakeDarkAction:
        return TakeDarkAction(exposure_ms=exp.value())

    return _Page(w, fill, build)


def _page_save_reference_image() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    chk_path, le_path, row_path = _opt_str("__localdata/reference_frame.png")
    cam = QSpinBox()
    cam.setRange(0, 9)
    form.addRow("Save path:", row_path)
    form.addRow("Camera index:", cam)

    def fill(a: SaveReferenceImageAction):
        if a.path is not None:
            chk_path.setChecked(False)
            le_path.setText(a.path)
        cam.setValue(a.camera_index)

    def build() -> SaveReferenceImageAction:
        path = None if chk_path.isChecked() else (le_path.text() or None)
        return SaveReferenceImageAction(path=path, camera_index=cam.value())

    return _Page(w, fill, build)


def _page_save_snapshot() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    chk_dir, le_dir, row_dir = _opt_str("__localdata/snapshots")
    form.addRow("Save directory:", row_dir)

    def fill(a: SaveSnapshotAction):
        if a.save_dir is not None:
            chk_dir.setChecked(False)
            le_dir.setText(a.save_dir)

    def build() -> SaveSnapshotAction:
        save_dir = None if chk_dir.isChecked() else (le_dir.text().strip() or None)
        return SaveSnapshotAction(save_dir=save_dir)

    return _Page(w, fill, build)


def _page_follow_sample_position() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    spin_dur, unit_dur, row_dur = _dur_row(30.0, "min")
    form.addRow("Duration:", row_dur)

    def fill(a: FollowSampleAction):
        if a.duration_s >= 60 and a.duration_s % 60 == 0:
            spin_dur.setValue(a.duration_s / 60); unit_dur.setCurrentText("min")
        else:
            spin_dur.setValue(a.duration_s); unit_dur.setCurrentText("s")

    def build() -> FollowSampleAction:
        d = spin_dur.value()
        dur_s = d * 60 if unit_dur.currentText() == "min" else d
        return FollowSampleAction(duration_s=dur_s)

    return _Page(w, fill, build)


def _page_wait() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    spin_dur, unit_dur, row_dur = _dur_row(5.0, "min")
    form.addRow("Duration:", row_dur)

    def fill(a: WaitAction):
        if a.duration_s >= 60 and a.duration_s % 60 == 0:
            spin_dur.setValue(a.duration_s / 60); unit_dur.setCurrentText("min")
        else:
            spin_dur.setValue(a.duration_s); unit_dur.setCurrentText("s")

    def build() -> WaitAction:
        d = spin_dur.value()
        return WaitAction(duration_s=d * 60 if unit_dur.currentText() == "min" else d)

    return _Page(w, fill, build)


def _page_log_message() -> _Page:
    w = QWidget()
    form = QFormLayout(w)
    msg = QLineEdit()
    msg.setPlaceholderText("Enter log message")
    form.addRow("Message:", msg)

    def fill(a: LogAction):
        msg.setText(a.message)

    def build() -> LogAction | None:
        text = msg.text().strip()
        return LogAction(message=text) if text else None

    return _Page(w, fill, build)


# ── Page factory registry ──────────────────────────────────────────────────

# Ops whose page factory accepts `available_loop_vars` (see _val_or_var).
# All other factories remain zero-arg.
_LOOP_VAR_OPS: frozenset[str] = frozenset({
    "move_absolute", "move_relative", "set_pressure", "set_and_wait_pressure", "set_temperature",
})

_PAGE_FACTORIES: dict[str, Callable[[], _Page]] = {
    "move_absolute": _page_move_absolute,
    "move_relative": _page_move_relative,
    "set_speed": _page_set_speed,
    "normal_stop": lambda: _empty_page(
        "Decelerate-stop all stage channels (normal stop).",
        lambda: StageAction(operation="normal_stop"),
    ),
    "emergency_stop": lambda: _empty_page(
        "Stop all stage channels immediately (no deceleration).",
        lambda: StageAction(operation="emergency_stop"),
    ),
    "microscope_out_and_fpd_in": _page_microscope_out_fpd_in,
    "fpd_out_and_microscope_in": _page_fpd_out_microscope_in,
    "set_pressure": _page_set_pressure,
    "wait_pressure": _page_wait_pressure,
    "set_and_wait_pressure": _page_set_and_wait_pressure,
    "set_control_mode": _page_set_control_mode,
    "set_temperature": _page_set_temperature,
    "wait_temperature": _page_wait_temperature,
    "set_heater": _page_set_heater,
    "all_heaters_off": lambda: _empty_page(
        "Turn off all heaters (both channels).",
        AllHeatersOffAction,
    ),
    "take_xrd": _page_take_xrd,
    "take_dark": _page_take_dark,
    "save_snapshot": _page_save_snapshot,
    "save_reference_image": _page_save_reference_image,
    "start_following": lambda: _empty_page(
        "Start background sample-following. All settings are taken from Global Settings.",
        StartFollowingAction,
    ),
    "stop_following": lambda: _empty_page(
        "Stop the background sample-following thread.",
        StopFollowingAction,
    ),
    "follow_sample_position": _page_follow_sample_position,
    "wait": _page_wait,
    "log_message": _page_log_message,
}


# ── Dialog ─────────────────────────────────────────────────────────────────

class StepEditorDialog(QDialog):
    """
    Dialog for adding or editing one sequence step.

    action=None  → new-step mode (device/op combos start at defaults)
    action=<obj> → edit mode (form pre-filled from the given action)

    available_loop_vars: names of loop variables in scope at the insertion
    point (empty outside any loop). Only affects the fields wrapped by
    _val_or_var (move_absolute/move_relative position/delta, set_pressure
    pressure, set_and_wait_pressure pressure, set_temperature value_k) —
    everywhere else this is a no-op.
    """

    def __init__(
        self,
        action: Action | None = None,
        parent=None,
        available_loop_vars: tuple[str, ...] = (),
    ):
        super().__init__(parent)
        self.setWindowTitle("Edit Step" if action is not None else "Add Step")
        self.setMinimumWidth(440)

        self._available_loop_vars = tuple(available_loop_vars)

        # Build all pages once; they are reused across device/op switches
        self._pages: dict[str, _Page] = {
            key: (
                _PAGE_FACTORIES[key](self._available_loop_vars)
                if key in _LOOP_VAR_OPS
                else _PAGE_FACTORIES[key]()
            )
            for key in _ALL_OPS
        }
        self._op_stack_index: dict[str, int] = {
            key: i for i, key in enumerate(_ALL_OPS)
        }
        self._built_action: Action | None = None
        self._ok_btn: QPushButton | None = None
        self._validity_connected_op: str | None = None

        self._build_ui()

        if action is not None:
            self._fill(action)

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        sel_form = QFormLayout()
        self._dev_combo = QComboBox()
        self._dev_combo.addItems(list(_DEVICE_OPS.keys()))
        self._op_combo = QComboBox()
        sel_form.addRow("Device:", self._dev_combo)
        sel_form.addRow("Operation:", self._op_combo)
        root.addLayout(sel_form)

        self._stack = QStackedWidget()
        for key in _ALL_OPS:
            self._stack.addWidget(self._pages[key].widget)
        root.addWidget(self._stack)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        self._dev_combo.currentTextChanged.connect(self._on_device_changed)
        self._op_combo.currentTextChanged.connect(self._on_op_changed)

        # Trigger initialisation
        self._on_device_changed(self._dev_combo.currentText())

    def _on_device_changed(self, device: str) -> None:
        ops = _DEVICE_OPS.get(device, [])
        self._op_combo.blockSignals(True)
        self._op_combo.clear()
        self._op_combo.addItems(ops)
        self._op_combo.blockSignals(False)
        if ops:
            self._on_op_changed(ops[0])

    def _on_op_changed(self, op: str) -> None:
        idx = self._op_stack_index.get(op)
        if idx is not None:
            self._stack.setCurrentIndex(idx)
        self._connect_page_validity(op)

    # ── Page validity (OK button enable/disable) ───────────────────────

    def _connect_page_validity(self, op: str) -> None:
        if self._ok_btn is None:
            return
        if self._validity_connected_op is not None:
            prev = self._pages.get(self._validity_connected_op)
            if prev and hasattr(prev.widget, "validity_changed"):
                try:
                    prev.widget.validity_changed.disconnect(self._on_page_validity_changed)
                except TypeError:
                    pass
        self._validity_connected_op = None
        page = self._pages.get(op)
        if page and hasattr(page.widget, "validity_changed"):
            page.widget.validity_changed.connect(self._on_page_validity_changed)
            self._validity_connected_op = op
            if hasattr(page.widget, "refresh_validity"):
                page.widget.refresh_validity()
        else:
            self._ok_btn.setEnabled(True)

    def _on_page_validity_changed(self, is_valid: bool) -> None:
        if self._ok_btn is not None:
            self._ok_btn.setEnabled(is_valid)

    # ── Fill (edit mode) ───────────────────────────────────────────────

    def _fill(self, action: Action) -> None:
        info = _action_to_device_op(action)
        if info is None:
            return
        device, op = info
        # Set device first (rebuilds op combo), then op
        self._dev_combo.setCurrentText(device)
        self._op_combo.setCurrentText(op)
        page = self._pages.get(op)
        if page is not None:
            page.fill(action)

    # ── OK handler ────────────────────────────────────────────────────

    def _on_ok(self) -> None:
        op = self._op_combo.currentText()
        page = self._pages.get(op)
        if page is None:
            self.accept()
            return
        result = page.build()
        if result is None:
            QMessageBox.warning(
                self, "Incomplete", "Please fill in all required fields."
            )
            return
        self._built_action = result
        self.accept()

    # ── Public API ─────────────────────────────────────────────────────

    def get_action(self) -> Action | None:
        """Return the constructed Action after OK was pressed, or None."""
        return self._built_action
