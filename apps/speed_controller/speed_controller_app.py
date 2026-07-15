"""Speed Controller window.

Reads and writes the actual pulse/sec value of each channel's L/M/H speed
register (``SPDx?ch`` / ``SPDxch<pps>`` commands, see
``utils.stage.control_stage.PM16CController.get_ch_speed_value`` /
``set_ch_speed_value``). Before any change is allowed, the current values of
all 11 channels are backed up to a user-chosen directory as JSON; those
values are also kept internally so the window can offer to restore them when
closed. A previously saved backup can be reloaded and re-applied via
"Load previous speed data".
"""
from __future__ import annotations

import datetime
import json
import os
import threading

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFileDialog, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

try:
    from settings.i18n import tr
except ImportError:
    import os as _os, sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.i18n import tr

try:
    from utils.stage.control_stage import PM16CController
except ImportError:
    import os as _os, sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from utils.stage.control_stage import PM16CController

_CHANNELS: list[int] = list(range(1, 12))
_LEVELS: tuple[str, ...] = ("L", "M", "H")
_PPS_MIN, _PPS_MAX = 1, 5_000_000

_LOCALDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__localdata")
_LAST_DIR_FILE = os.path.join(_LOCALDATA_DIR, "last_dir.txt")


class SpeedControllerWindow(QMainWindow):
    _read_all_done = pyqtSignal(dict)
    _read_all_failed = pyqtSignal(str)
    _apply_result = pyqtSignal(int, str, object)
    _load_apply_done = pyqtSignal(dict, list)

    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Speed Controller"))
        self.resize(760, 640)

        self._owns_controller = controller is None
        if controller is None:
            controller = PM16CController(ip='192.168.1.55', port=7777, debug=True)
            try:
                controller.connect()
            except Exception as e:
                QMessageBox.critical(self, tr("Connection Error"), tr("Could not connect: {error}", error=e))
                raise
        self.controller = controller

        self._ready = False
        # Last known-good pps value per (ch, level) — comparison baseline for Apply-enabled state.
        self._current: dict[int, dict[str, "int | None"]] = {}
        # Values captured at window-open, used to revert on close.
        self._initial: dict[int, dict[str, int]] = {}

        self._current_labels: dict[tuple[int, str], QLabel] = {}
        self._inputs: dict[tuple[int, str], QSpinBox] = {}
        self._apply_buttons: dict[tuple[int, str], QPushButton] = {}
        # Every read/apply worker thread ever started, so closeEvent can
        # refuse to close while one is still in flight (only ever appended
        # to/read from the GUI thread, so no lock is needed).
        self._active_threads: list[threading.Thread] = []

        self._read_all_done.connect(self._on_initial_read_done)
        self._read_all_failed.connect(self._on_initial_read_failed)
        self._apply_result.connect(self._on_apply_result)
        self._load_apply_done.connect(self._on_load_apply_done)

        self._setup_ui()
        self._set_all_enabled(False)
        self._start_initial_read()

    # ── UI construction ─────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self._status_label = QLabel(tr("Reading current speed values…"))
        self._status_label.setStyleSheet("font-weight: bold; padding: 4px;")
        root.addWidget(self._status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        table_widget = QWidget()
        grid = QGridLayout(table_widget)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        headers = [tr("Channel")]
        for level in _LEVELS:
            headers += [tr("{level} current", level=level), tr("{level} new value", level=level), ""]
        for col, text in enumerate(headers):
            grid.addWidget(QLabel(f"<b>{text}</b>"), 0, col)

        for row, ch in enumerate(_CHANNELS, start=1):
            grid.addWidget(QLabel(f"Ch{ch}"), row, 0)
            col = 1
            for level in _LEVELS:
                cur_lbl = QLabel("—")
                cur_lbl.setMinimumWidth(70)
                self._current_labels[(ch, level)] = cur_lbl
                grid.addWidget(cur_lbl, row, col)

                spin = QSpinBox()
                spin.setRange(_PPS_MIN, _PPS_MAX)
                spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
                spin.setMinimumWidth(90)
                self._inputs[(ch, level)] = spin
                grid.addWidget(spin, row, col + 1)

                apply_btn = QPushButton(tr("Apply"))
                apply_btn.setEnabled(False)
                self._apply_buttons[(ch, level)] = apply_btn
                grid.addWidget(apply_btn, row, col + 2)

                spin.valueChanged.connect(
                    lambda _val, ch=ch, level=level: self._on_input_changed(ch, level)
                )
                apply_btn.clicked.connect(
                    lambda _checked=False, ch=ch, level=level: self._on_apply(ch, level)
                )

                col += 3

        scroll.setWidget(table_widget)
        root.addWidget(scroll)

        bottom_row = QHBoxLayout()
        self._btn_load = QPushButton(tr("Load previous speed data"))
        self._btn_load.clicked.connect(self._on_load_previous)
        bottom_row.addStretch()
        bottom_row.addWidget(self._btn_load)
        root.addLayout(bottom_row)

    def _set_all_enabled(self, enabled: bool) -> None:
        for spin in self._inputs.values():
            spin.setEnabled(enabled)
        for btn in self._apply_buttons.values():
            btn.setEnabled(False)
        self._btn_load.setEnabled(enabled)

    # ── Initial read + mandatory backup ─────────────────────────────────

    def _start_initial_read(self) -> None:
        def _worker():
            try:
                data: dict[int, dict[str, int]] = {}
                for ch in _CHANNELS:
                    data[ch] = {}
                    for level in _LEVELS:
                        pps = self.controller.get_ch_speed_value(ch, level)
                        if pps is None:
                            raise RuntimeError(f"Ch{ch} {level}")
                        data[ch][level] = pps
                self._read_all_done.emit(data)
            except Exception as e:
                self._read_all_failed.emit(str(e))

        t = threading.Thread(target=_worker, daemon=True)
        self._active_threads.append(t)
        t.start()

    @pyqtSlot(dict)
    def _on_initial_read_done(self, data: dict) -> None:
        self._current = {ch: dict(levels) for ch, levels in data.items()}
        for ch in _CHANNELS:
            for level in _LEVELS:
                pps = data[ch][level]
                self._current_labels[(ch, level)].setText(str(pps))
                spin = self._inputs[(ch, level)]
                spin.blockSignals(True)
                spin.setValue(pps)
                spin.blockSignals(False)

        reply = QMessageBox.information(
            self, tr("Backup Required"),
            tr("Current speed values will be saved before any operation."),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            self.close()
            return

        last_dir = self._load_last_dir()
        directory = QFileDialog.getExistingDirectory(self, tr("Select Backup Directory"), last_dir)
        if not directory:
            self.close()
            return
        self._save_last_dir(directory)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(directory, f"speed_{timestamp}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._build_json_payload(data), f, indent=2, ensure_ascii=False)
        except Exception as e:
            QMessageBox.critical(self, tr("Save Error"),
                                 tr("Could not save backup file:\n{error}", error=e))
            self.close()
            return

        self._initial = {ch: dict(levels) for ch, levels in data.items()}
        self._ready = True
        self._status_label.setText(tr("Ready.  Backup saved to {path}", path=path))
        self._set_all_enabled(True)

    @pyqtSlot(str)
    def _on_initial_read_failed(self, message: str) -> None:
        QMessageBox.critical(self, tr("Read Error"),
                             tr("Could not read current speed values:\n{error}", error=message))
        self.close()

    @staticmethod
    def _build_json_payload(data: dict) -> dict:
        return {
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "channels": {str(ch): {level: data[ch][level] for level in _LEVELS} for ch in _CHANNELS},
        }

    # ── Individual Apply ─────────────────────────────────────────────────

    def _on_input_changed(self, ch: int, level: str) -> None:
        spin = self._inputs[(ch, level)]
        current = self._current.get(ch, {}).get(level)
        btn = self._apply_buttons[(ch, level)]
        btn.setEnabled(self._ready and (current is None or spin.value() != current))

    def _on_apply(self, ch: int, level: str) -> None:
        target = self._inputs[(ch, level)].value()
        self._apply_buttons[(ch, level)].setEnabled(False)

        def _worker():
            try:
                with self.controller.motion_session(
                    owner="Speed Controller", operation=f"Set Ch{ch} {level} speed",
                ) as motion:
                    self.controller.set_ch_speed_value(ch, level, target, motion=motion)
                readback = self.controller.get_ch_speed_value(ch, level)
            except Exception:
                readback = None
            self._apply_result.emit(ch, level, readback)

        t = threading.Thread(target=_worker, daemon=True)
        self._active_threads.append(t)
        t.start()

    @pyqtSlot(int, str, object)
    def _on_apply_result(self, ch: int, level: str, readback) -> None:
        label = self._current_labels[(ch, level)]
        spin = self._inputs[(ch, level)]
        self._current.setdefault(ch, {})[level] = readback
        if readback is None:
            label.setText(tr("read error"))
            label.setStyleSheet("color: red;")
        else:
            label.setText(str(readback))
            label.setStyleSheet("" if readback == spin.value() else "color: red;")
        self._on_input_changed(ch, level)

    # ── Close handling ───────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if any(t.is_alive() for t in self._active_threads):
            QMessageBox.warning(
                self, tr("Still Working"),
                tr("A speed read/apply operation is still in progress. Please wait a moment and try closing again."),
            )
            event.ignore()
            return

        if self._ready:
            reply = QMessageBox.question(
                self, tr("Confirm Close"),
                tr("Revert all channels to the speed values recorded when this window opened?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    with self.controller.motion_session(
                        owner="Speed Controller", operation="Revert speed values",
                    ) as motion:
                        for ch in _CHANNELS:
                            for level in _LEVELS:
                                try:
                                    self.controller.set_ch_speed_value(
                                        ch, level, self._initial[ch][level], motion=motion)
                                except Exception:
                                    pass
                except Exception:
                    pass

        if self._owns_controller:
            try:
                self.controller.disconnect()
            except Exception:
                pass
        event.accept()

    # ── Load previous speed data ─────────────────────────────────────────

    def _on_load_previous(self) -> None:
        last_dir = self._load_last_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Load Previous Speed Data"), last_dir, "JSON files (*.json)"
        )
        if not path:
            return
        self._save_last_dir(os.path.dirname(path))

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            loaded = self._parse_payload(payload)
        except Exception as e:
            QMessageBox.critical(self, tr("Load Error"),
                                 tr("Invalid speed data file:\n{error}", error=e))
            return

        reply = QMessageBox.question(
            self, tr("Apply Loaded Speeds"),
            tr("Apply the speed values loaded from this file to all channels?"),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return

        self._set_all_enabled(False)
        self._status_label.setText(tr("Applying loaded speed values…"))

        def _worker():
            failures = []
            results: dict[int, dict[str, "int | None"]] = {}
            try:
                with self.controller.motion_session(
                    owner="Speed Controller", operation="Apply loaded speed values",
                ) as motion:
                    for ch in _CHANNELS:
                        results[ch] = {}
                        for level in _LEVELS:
                            target = loaded[ch][level]
                            ok, actual = self._write_verify_retry(ch, level, target, motion)
                            results[ch][level] = actual
                            if not ok:
                                failures.append((ch, level, target, actual))
            except Exception as e:
                for ch in _CHANNELS:
                    results.setdefault(ch, {level: None for level in _LEVELS})
                failures.append((None, None, None, str(e)))
            self._load_apply_done.emit(results, failures)

        t = threading.Thread(target=_worker, daemon=True)
        self._active_threads.append(t)
        t.start()

    def _write_verify_retry(self, ch: int, level: str, target: int, motion) -> tuple[bool, "int | None"]:
        actual = None
        for _attempt in range(2):  # initial attempt + one retry
            try:
                self.controller.set_ch_speed_value(ch, level, target, motion=motion)
                actual = self.controller.get_ch_speed_value(ch, level)
            except Exception:
                actual = None
            if actual == target:
                return True, actual
        return False, actual

    @pyqtSlot(dict, list)
    def _on_load_apply_done(self, results: dict, failures: list) -> None:
        for ch in _CHANNELS:
            for level in _LEVELS:
                actual = results[ch][level]
                self._current.setdefault(ch, {})[level] = actual
                if actual is not None:
                    self._current_labels[(ch, level)].setText(str(actual))
                    self._current_labels[(ch, level)].setStyleSheet("")
                    spin = self._inputs[(ch, level)]
                    spin.blockSignals(True)
                    spin.setValue(actual)
                    spin.blockSignals(False)
                else:
                    self._current_labels[(ch, level)].setText(tr("read error"))
                    self._current_labels[(ch, level)].setStyleSheet("color: red;")

        self._set_all_enabled(True)
        for ch in _CHANNELS:
            for level in _LEVELS:
                self._on_input_changed(ch, level)

        if failures:
            lines = [
                tr("Ch{ch} {level}: expected {target}, got {actual}",
                   ch=ch, level=level, target=target,
                   actual=actual if actual is not None else tr("read error"))
                for ch, level, target, actual in failures
            ]
            self._status_label.setText(tr("Loaded with {n} failure(s).", n=len(failures)))
            QMessageBox.warning(self, tr("Some Speeds Not Applied"), "\n".join(lines))
        else:
            self._status_label.setText(tr("Loaded speed values applied successfully."))

    @staticmethod
    def _parse_payload(payload) -> dict[int, dict[str, int]]:
        if not isinstance(payload, dict) or "channels" not in payload:
            raise ValueError(tr("Missing 'channels' key"))
        channels = payload["channels"]
        if not isinstance(channels, dict):
            raise ValueError(tr("'channels' must be an object"))
        result: dict[int, dict[str, int]] = {}
        for ch in _CHANNELS:
            key = str(ch)
            if key not in channels:
                raise ValueError(tr("Missing channel {ch}", ch=ch))
            entry = channels[key]
            if not isinstance(entry, dict):
                raise ValueError(tr("Channel {ch} entry must be an object", ch=ch))
            levels: dict[str, int] = {}
            for level in _LEVELS:
                if level not in entry:
                    raise ValueError(tr("Channel {ch} missing '{level}'", ch=ch, level=level))
                value = entry[level]
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(tr("Channel {ch} '{level}' must be an integer", ch=ch, level=level))
                if not (_PPS_MIN <= value <= _PPS_MAX):
                    raise ValueError(
                        tr("Channel {ch} '{level}' out of range ({min}-{max})",
                           ch=ch, level=level, min=_PPS_MIN, max=_PPS_MAX)
                    )
                levels[level] = value
            result[ch] = levels
        return result

    # ── Last-dir persistence ─────────────────────────────────────────────

    @staticmethod
    def _load_last_dir() -> str:
        try:
            with open(_LAST_DIR_FILE, "r", encoding="utf-8") as f:
                d = f.read().strip()
            if os.path.isdir(d):
                return d
        except Exception:
            pass
        return os.path.expanduser("~")

    @staticmethod
    def _save_last_dir(directory: str) -> None:
        os.makedirs(_LOCALDATA_DIR, exist_ok=True)
        with open(_LAST_DIR_FILE, "w", encoding="utf-8") as f:
            f.write(directory)


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    try:
        win = SpeedControllerWindow()
    except Exception:
        sys.exit(1)
    win.show()
    sys.exit(app.exec())
