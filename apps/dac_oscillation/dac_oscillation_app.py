import sys
import os
import json
import time

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLabel, QLineEdit, QPushButton,
    QRadioButton, QButtonGroup, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer

try:
    from utils.stage.control_stage import PM16CController, PULSE_SCALE
    from utils.stage.control_stage_sim import PM16CControllerSim
except ImportError:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    from utils.stage.control_stage import PM16CController, PULSE_SCALE
    from utils.stage.control_stage_sim import PM16CControllerSim

try:
    from settings.i18n import tr
except ImportError:
    import os as _os, sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.i18n import tr


_SETTINGS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__localdata")
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "osc_settings.json")
_SETTINGS_DEFAULTS = {
    "osc_pos_a":  "-5.0",
    "osc_pos_b":  "20.0",
    "osc_dwell":  "0",
    "osc_cycles": "0",
    "osc_speed":  "M",
    "osc_unit":   "degrees",
}

_STYLE_START = (
    "QPushButton { font-size: 14px; font-weight: bold;"
    " background-color: #4CAF50; color: white; border-radius: 4px; padding: 8px; }"
    "QPushButton:disabled { background-color: #A0A0A0; }"
)
_STYLE_STOP = (
    "QPushButton { font-size: 14px; font-weight: bold;"
    " background-color: #FF3333; color: white; border-radius: 4px; padding: 8px; }"
)


class DacOscillationWindow(QMainWindow):
    def __init__(self, controller=None):
        super().__init__()
        self.setWindowTitle(tr("DAC Stage Oscillation (Ch11)"))
        self.setMinimumWidth(340)

        if controller is not None:
            self.controller = controller
            self._owns_controller = False
        else:
            self.controller = PM16CController(ip='192.168.1.55', port=7777, debug=True)
            try:
                self.controller.connect()
            except Exception as exc:
                ret = QMessageBox.critical(
                    None, tr("Connection Error"),
                    tr("Could not connect to stage controller:\n{error}\n\n"
                       "Run in simulation mode instead?", error=exc),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if ret == QMessageBox.StandardButton.Yes:
                    self.controller = PM16CControllerSim(debug=True)
                    self.controller.connect()
                else:
                    raise
            self._owns_controller = True

        self._osc_state = "IDLE"  # "IDLE", "GOING_A", "DWELL_A", "GOING_B", "DWELL_B", "GOING_ZERO"
        self._osc_pos_a = 0
        self._osc_pos_b = 0
        self._osc_dwell_ms = 0
        self._osc_cycles_total = 0
        self._osc_cycles_done = 0
        self._osc_start_secs = None
        self._current_unit = "degrees"  # matches the default-checked radio button in _init_ui
        self._loading_settings = False

        self._init_ui()
        self._load_settings()

        self._osc_poll_timer = QTimer(self)
        self._osc_poll_timer.timeout.connect(self._osc_poll)

        QTimer.singleShot(100, self._refresh_ch11_pos)

    # ── UI construction ────────────────────────────────────────────────────

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # ── Parameters ──────────────────────────────────────────────────
        param_group = QGroupBox(tr("Oscillation Parameters"))
        param_layout = QGridLayout()
        param_layout.setSpacing(6)
        param_layout.setColumnStretch(1, 1)

        # ── Unit selection (row 0) ──────────────────────────────────────
        # Button-group ids are the stable identifier (0=Pulse, 1=Degrees);
        # translated button text must not be used for state comparisons.
        unit_row = QWidget()
        unit_layout = QHBoxLayout(unit_row)
        unit_layout.setContentsMargins(0, 0, 0, 0)
        unit_layout.addWidget(QLabel(tr("Unit:")))
        self._unit_group = QButtonGroup(self)
        for _id, _u in enumerate(("Pulse", "Degrees")):
            rb = QRadioButton(tr(_u))
            self._unit_group.addButton(rb, _id)
            unit_layout.addWidget(rb)
            if _u == "Degrees":
                rb.setChecked(True)
        unit_layout.addStretch()
        param_layout.addWidget(unit_row, 0, 0, 1, 4)

        self.line_osc_pos_a = QLineEdit()
        self.line_osc_pos_b = QLineEdit()
        self.line_osc_dwell = QLineEdit()
        self.line_osc_dwell.setFixedWidth(65)
        self.line_osc_cycles = QLineEdit()
        self.line_osc_cycles.setFixedWidth(55)

        self.lbl_pos_a = QLabel(tr("Pos A ({suffix}):", suffix="deg"))
        self.lbl_pos_b = QLabel(tr("Pos B ({suffix}):", suffix="deg"))
        param_layout.addWidget(self.lbl_pos_a, 1, 0)
        param_layout.addWidget(self.line_osc_pos_a, 1, 1, 1, 3)
        param_layout.addWidget(self.lbl_pos_b, 2, 0)
        param_layout.addWidget(self.line_osc_pos_b, 2, 1, 1, 3)

        dwell_row = QWidget()
        dwell_layout = QHBoxLayout(dwell_row)
        dwell_layout.setContentsMargins(0, 0, 0, 0)
        dwell_layout.addWidget(QLabel(tr("Dwell (ms):")))
        dwell_layout.addWidget(self.line_osc_dwell)
        dwell_layout.addSpacing(10)
        dwell_layout.addWidget(QLabel(tr("Cycles (0=∞):")))
        dwell_layout.addWidget(self.line_osc_cycles)
        dwell_layout.addStretch()
        param_layout.addWidget(dwell_row, 3, 0, 1, 4)

        spd_row = QWidget()
        spd_layout = QHBoxLayout(spd_row)
        spd_layout.setContentsMargins(0, 0, 0, 0)
        spd_layout.addWidget(QLabel(tr("Speed:")))
        self._osc_speed_group = QButtonGroup(self)
        for spd in ("H", "M", "L"):
            rb = QRadioButton(spd)
            self._osc_speed_group.addButton(rb)
            spd_layout.addWidget(rb)
            if spd == "M":
                rb.setChecked(True)
        spd_layout.addStretch()
        param_layout.addWidget(spd_row, 4, 0, 1, 4)

        param_group.setLayout(param_layout)
        layout.addWidget(param_group)

        # ── Control ─────────────────────────────────────────────────────
        ctrl_group = QGroupBox(tr("Control"))
        ctrl_layout = QVBoxLayout()
        ctrl_layout.setSpacing(6)

        self.btn_osc_start = QPushButton(tr("▶ Start Oscillation"))
        self.btn_osc_start.setMinimumHeight(44)
        self.btn_osc_start.setStyleSheet(_STYLE_START)
        self.btn_osc_start.clicked.connect(self._osc_start_clicked)
        ctrl_layout.addWidget(self.btn_osc_start)

        self.btn_go_zero = QPushButton(tr("Go to θ = 0°"))
        self.btn_go_zero.setMinimumHeight(32)
        self.btn_go_zero.setStyleSheet(
            "QPushButton { font-size: 12px; background-color: #1565C0; color: white;"
            " border-radius: 4px; padding: 6px; }"
            "QPushButton:disabled { background-color: #A0A0A0; }"
        )
        self.btn_go_zero.clicked.connect(self._go_to_zero)
        ctrl_layout.addWidget(self.btn_go_zero)

        self.lbl_osc_ch11_pos = QLabel(tr("Ch11: — pulse"))
        self.lbl_osc_ch11_pos.setStyleSheet("color: #555; font-size: 11px;")
        ctrl_layout.addWidget(self.lbl_osc_ch11_pos)

        self.lbl_osc_status = QLabel(tr("Ready"))
        self.lbl_osc_status.setStyleSheet("color: #1a6fbf; font-size: 13px; font-weight: bold;")
        self.lbl_osc_status.setWordWrap(True)
        ctrl_layout.addWidget(self.lbl_osc_status)

        ctrl_group.setLayout(ctrl_layout)
        layout.addWidget(ctrl_group)

        layout.addStretch()

        for w in (self.line_osc_pos_a, self.line_osc_pos_b,
                  self.line_osc_dwell, self.line_osc_cycles):
            w.textChanged.connect(self._save_settings)
        for btn in self._unit_group.buttons():
            btn.toggled.connect(self._on_unit_toggled)

    # ── Settings persistence ───────────────────────────────────────────────

    def _load_settings(self):
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        d = {**_SETTINGS_DEFAULTS, **data}
        self._loading_settings = True
        try:
            self.line_osc_pos_a.setText(d["osc_pos_a"])
            self.line_osc_pos_b.setText(d["osc_pos_b"])
            self.line_osc_dwell.setText(d["osc_dwell"])
            self.line_osc_cycles.setText(d["osc_cycles"])
            saved_spd = d.get("osc_speed", "M")
            for btn in self._osc_speed_group.buttons():
                if btn.text() == saved_spd:
                    btn.setChecked(True)
                    break
            saved_unit = d.get("osc_unit", "degrees")
            target_id = 1 if saved_unit == "degrees" else 0
            btn = self._unit_group.button(target_id)
            if btn:
                btn.setChecked(True)
            # setChecked() above only emits toggled() if it actually changes
            # state, so make sure _current_unit reflects the loaded value
            # (and the pos-field suffix labels) even when it doesn't.
            self._on_unit_changed()
        finally:
            self._loading_settings = False

    def _save_settings(self):
        data = {
            "osc_pos_a":  self.line_osc_pos_a.text(),
            "osc_pos_b":  self.line_osc_pos_b.text(),
            "osc_dwell":  self.line_osc_dwell.text(),
            "osc_cycles": self.line_osc_cycles.text(),
            "osc_speed":  self._get_osc_speed(),
            "osc_unit":   self._get_osc_unit(),
        }
        try:
            os.makedirs(_SETTINGS_DIR, exist_ok=True)
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[OscSettings] Failed to save: {e}")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get_osc_speed(self) -> str:
        for btn in self._osc_speed_group.buttons():
            if btn.isChecked():
                return btn.text()
        return "M"

    def _get_osc_unit(self) -> str:
        return "degrees" if self._unit_group.checkedId() == 1 else "pulse"

    def _on_unit_toggled(self, checked: bool):
        if checked:
            self._on_unit_changed()

    def _on_unit_changed(self):
        new_unit = self._get_osc_unit()
        if not self._loading_settings and new_unit != self._current_unit:
            self._convert_pos_fields(self._current_unit, new_unit)
        self._current_unit = new_unit
        suffix = "deg" if new_unit == "degrees" else "pulse"
        self.lbl_pos_a.setText(tr("Pos A ({suffix}):", suffix=suffix))
        self.lbl_pos_b.setText(tr("Pos B ({suffix}):", suffix=suffix))
        self._save_settings()

    def _convert_pos_fields(self, old_unit: str, new_unit: str):
        for line in (self.line_osc_pos_a, self.line_osc_pos_b):
            text = line.text().strip()
            if not text:
                continue
            try:
                val = float(text)
            except ValueError:
                continue
            if old_unit == "pulse" and new_unit == "degrees":
                line.setText(f"{val * PULSE_SCALE[11]:.4f}")
            elif old_unit == "degrees" and new_unit == "pulse":
                line.setText(str(round(val / PULSE_SCALE[11])))

    def _set_osc_input_enabled(self, enabled: bool):
        for w in (self.line_osc_pos_a, self.line_osc_pos_b,
                  self.line_osc_dwell, self.line_osc_cycles):
            w.setEnabled(enabled)
        for btn in self._osc_speed_group.buttons():
            btn.setEnabled(enabled)
        for btn in self._unit_group.buttons():
            btn.setEnabled(enabled)
        self.btn_go_zero.setEnabled(enabled)

    def _refresh_ch11_pos(self):
        try:
            pos_str = self.controller.get_ch_pos(11)
            if pos_str is not None:
                pos = int(pos_str)
                deg = pos * PULSE_SCALE[11]
                self.lbl_osc_ch11_pos.setText(tr("Ch11: {pos:+d} pulse  ({deg:+.3f}°)", pos=pos, deg=deg))
        except Exception:
            pass

    def _osc_update_status(self):
        if self._osc_state == "IDLE":
            self.lbl_osc_status.setText(tr("Ready"))
            return
        if self._osc_state == "GOING_ZERO":
            self.lbl_osc_status.setText(tr("Moving to θ=0°…"))
            return
        cycles_str = (f"{self._osc_cycles_done}/{self._osc_cycles_total}"
                      if self._osc_cycles_total > 0
                      else f"{self._osc_cycles_done}/∞")
        elapsed = ""
        if self._osc_start_secs is not None:
            secs = int(time.time() - self._osc_start_secs)
            m, s = divmod(secs, 60)
            elapsed = f"  [{m:02d}:{s:02d}]"
        state_labels = {
            "GOING_A": tr("← Moving to A  Cycle {cycles}{elapsed}", cycles=cycles_str, elapsed=elapsed),
            "DWELL_A": tr("⏸ At A  Cycle {cycles}{elapsed}", cycles=cycles_str, elapsed=elapsed),
            "GOING_B": tr("→ Moving to B  Cycle {cycles}{elapsed}", cycles=cycles_str, elapsed=elapsed),
            "DWELL_B": tr("⏸ At B  Cycle {cycles}{elapsed}", cycles=cycles_str, elapsed=elapsed),
        }
        self.lbl_osc_status.setText(state_labels.get(self._osc_state, self._osc_state))

    def _go_to_zero(self):
        if self._osc_state != "IDLE":
            return
        try:
            self.controller.set_ch_speed(11, self._get_osc_speed())
            self.controller.move_ch_absolute(11, 0)
        except Exception as e:
            QMessageBox.critical(self, tr("Move Error"), str(e))
            return
        self._osc_state = "GOING_ZERO"
        self._set_osc_input_enabled(False)
        self._osc_update_status()
        self._osc_poll_timer.start(200)

    # ── Oscillation state machine ──────────────────────────────────────────

    def _osc_start_clicked(self):
        if self._osc_state == "IDLE":
            self._osc_start()
        else:
            self._osc_stop()

    def _osc_start(self):
        try:
            if self._get_osc_unit() == "degrees":
                pos_a = round(float(self.line_osc_pos_a.text() or "0") / PULSE_SCALE[11])
                pos_b = round(float(self.line_osc_pos_b.text() or "0") / PULSE_SCALE[11])
            else:
                pos_a = int(self.line_osc_pos_a.text() or "0")
                pos_b = int(self.line_osc_pos_b.text() or "0")
            dwell  = int(self.line_osc_dwell.text()  or "0")
            cycles = int(self.line_osc_cycles.text() or "0")
        except ValueError:
            QMessageBox.warning(self, tr("Invalid Input"),
                                tr("All oscillation fields must be valid numbers."))
            return
        if pos_a == pos_b:
            QMessageBox.warning(self, tr("Invalid Input"),
                                tr("Pos A and Pos B must be different."))
            return
        if dwell < 0 or cycles < 0:
            QMessageBox.warning(self, tr("Invalid Input"),
                                tr("Dwell and Cycles must be ≥ 0."))
            return

        self._osc_pos_a        = pos_a
        self._osc_pos_b        = pos_b
        self._osc_dwell_ms     = dwell
        self._osc_cycles_total = cycles
        self._osc_cycles_done  = 0
        self._osc_start_secs   = time.time()
        self._osc_state        = "GOING_A"

        self._set_osc_input_enabled(False)
        self.btn_osc_start.setText(tr("■ Stop Oscillation"))
        self.btn_osc_start.setStyleSheet(_STYLE_STOP)
        self._save_settings()
        self._osc_update_status()

        try:
            self.controller.set_ch_speed(11, self._get_osc_speed())
            self.controller.move_ch_absolute(11, pos_a)
        except Exception as e:
            self._osc_state = "IDLE"
            self._osc_finish_ui()
            QMessageBox.critical(self, tr("Oscillation Error"), str(e))
            return

        self._osc_poll_timer.start(200)

    def _osc_stop(self):
        self._osc_state = "IDLE"
        try:
            self.controller.normal_stop()
        except Exception:
            pass
        try:
            self.controller.switch_to_loc()
        except Exception:
            pass
        self._osc_finish_ui()

    def _osc_poll(self):
        try:
            pos_str = self.controller.get_ch_pos(11)
            if pos_str is not None:
                pos = int(pos_str)
                deg = pos * PULSE_SCALE[11]
                self.lbl_osc_ch11_pos.setText(tr("Ch11: {pos:+d} pulse  ({deg:+.3f}°)", pos=pos, deg=deg))
        except Exception:
            pass
        self._osc_update_status()
        if self._osc_state in ("GOING_A", "GOING_B", "GOING_ZERO"):
            try:
                if not self.controller.get_is_moving():
                    self._osc_on_stopped()
            except Exception:
                pass

    def _osc_on_stopped(self):
        if self._osc_state == "GOING_ZERO":
            self._osc_state = "IDLE"
            try:
                self.controller.switch_to_loc()
            except Exception:
                pass
            self._osc_finish_ui()
            return
        if self._osc_state == "GOING_A":
            self._osc_state = "DWELL_A"
            self._osc_update_status()
            if self._osc_dwell_ms > 0:
                QTimer.singleShot(self._osc_dwell_ms, self._osc_from_dwell_a)
            else:
                self._osc_from_dwell_a()
        elif self._osc_state == "GOING_B":
            self._osc_cycles_done += 1
            if self._osc_cycles_total > 0 and self._osc_cycles_done >= self._osc_cycles_total:
                self._osc_finish()
            else:
                self._osc_state = "DWELL_B"
                self._osc_update_status()
                if self._osc_dwell_ms > 0:
                    QTimer.singleShot(self._osc_dwell_ms, self._osc_from_dwell_b)
                else:
                    self._osc_from_dwell_b()

    def _osc_from_dwell_a(self):
        if self._osc_state != "DWELL_A":
            return
        self._osc_state = "GOING_B"
        self._osc_update_status()
        try:
            self.controller.set_ch_speed(11, self._get_osc_speed())
            self.controller.move_ch_absolute(11, self._osc_pos_b)
        except Exception as e:
            self._osc_state = "IDLE"
            self._osc_finish_ui()
            QMessageBox.critical(self, tr("Oscillation Error"), str(e))

    def _osc_from_dwell_b(self):
        if self._osc_state != "DWELL_B":
            return
        self._osc_state = "GOING_A"
        self._osc_update_status()
        try:
            self.controller.set_ch_speed(11, self._get_osc_speed())
            self.controller.move_ch_absolute(11, self._osc_pos_a)
        except Exception as e:
            self._osc_state = "IDLE"
            self._osc_finish_ui()
            QMessageBox.critical(self, tr("Oscillation Error"), str(e))

    def _osc_finish(self):
        self._osc_state = "IDLE"
        try:
            self.controller.switch_to_loc()
        except Exception:
            pass
        self._osc_finish_ui()

    def _osc_finish_ui(self):
        self._osc_poll_timer.stop()
        self._set_osc_input_enabled(True)
        self.btn_osc_start.setText(tr("▶ Start Oscillation"))
        self.btn_osc_start.setStyleSheet(_STYLE_START)
        self._osc_update_status()

    # ── Window lifecycle ───────────────────────────────────────────────────

    def closeEvent(self, event):
        if self._osc_state != "IDLE":
            self._osc_state = "IDLE"
            try:
                self.controller.normal_stop()
            except Exception:
                pass
        self._osc_poll_timer.stop()
        if self._owns_controller:
            try:
                self.controller.switch_to_loc()
                self.controller.disconnect()
            except Exception:
                pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DacOscillationWindow()
    window.show()
    sys.exit(app.exec())
