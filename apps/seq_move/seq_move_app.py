"""Sequential Relative Moves window.

Lets the user define a pattern of relative stage moves (up to 4 channels per
step, unlimited steps) and execute them one step at a time with interactive
confirmation between steps.  The pattern can be saved to and loaded from JSON.
"""
from __future__ import annotations

import json
import os
import threading

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

try:
    from settings.i18n import tr
except ImportError:
    import os as _os, sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.i18n import tr

_LOCALDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__localdata")
_LAST_DIR_FILE = os.path.join(_LOCALDATA_DIR, "last_dir.txt")

_MAX_MOVES_PER_STEP = 4


class SeqMoveWindow(QMainWindow):
    _move_done = pyqtSignal()
    _return_done = pyqtSignal()
    _move_error = pyqtSignal(str)
    _move_aborted = pyqtSignal()

    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Sequential Relative Moves"))
        self.resize(880, 540)

        self.controller = controller

        self._pattern: list[list[dict]] = []
        self._original_positions: dict[int, int] = {}
        self._step_index = 0
        self._state = "IDLE"
        self._abort_requested = threading.Event()

        self._move_done.connect(self._on_move_done)
        self._return_done.connect(self._on_return_done)
        self._move_error.connect(self._on_move_error)
        self._move_aborted.connect(self._on_move_aborted)

        self._setup_ui()
        self._set_state("IDLE")

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Pattern editor ──────────────────────────────────────────────────
        editor_group = QGroupBox(tr("Move Pattern"))
        editor_vlayout = QVBoxLayout(editor_group)

        # Columns: Step | Ch Δpulse (×4)
        self._table = QTableWidget(0, 1 + _MAX_MOVES_PER_STEP * 2)
        headers = [tr("Step")]
        for i in range(1, _MAX_MOVES_PER_STEP + 1):
            headers += [f"Ch{i}", f"Δpulse{i}"]
        self._table.setHorizontalHeaderLabels(headers)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for c in range(1, 1 + _MAX_MOVES_PER_STEP * 2):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().hide()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(180)
        editor_vlayout.addWidget(self._table)

        btn_row = QHBoxLayout()
        self._btn_add = QPushButton(tr("Add Step"))
        self._btn_add.clicked.connect(self._on_add_step)
        self._btn_remove = QPushButton(tr("Remove Selected"))
        self._btn_remove.clicked.connect(self._on_remove_step)
        self._btn_save = QPushButton(tr("Save JSON…"))
        self._btn_save.clicked.connect(self._on_save)
        self._btn_load = QPushButton(tr("Load JSON…"))
        self._btn_load.clicked.connect(self._on_load)
        btn_row.addWidget(self._btn_add)
        btn_row.addWidget(self._btn_remove)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_save)
        btn_row.addWidget(self._btn_load)
        editor_vlayout.addLayout(btn_row)
        root.addWidget(editor_group)

        # ── Execution controls ──────────────────────────────────────────────
        exec_group = QGroupBox(tr("Execution"))
        exec_vlayout = QVBoxLayout(exec_group)

        self._status_label = QLabel(tr("Ready."))
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("font-size: 13px; padding: 4px;")
        exec_vlayout.addWidget(self._status_label)

        self._btn_start = QPushButton(tr("▶  Start Sequential Move"))
        self._btn_start.setStyleSheet("font-size: 14px; padding: 8px;")
        self._btn_start.clicked.connect(self._on_start)
        exec_vlayout.addWidget(self._btn_start)

        self._estop_btn = QPushButton(tr("Emergency Stop"))
        self._estop_btn.setStyleSheet(
            "QPushButton { background-color: #FF3333; color: white; font-weight: bold;"
            " font-size: 16px; border-radius: 4px; }"
            " QPushButton:pressed { background-color: #CC0000; }"
        )
        self._estop_btn.clicked.connect(self._on_emergency_stop)
        exec_vlayout.addWidget(self._estop_btn)

        mid_row = QHBoxLayout()
        self._btn_next = QPushButton(tr("Go to Next Step  →"))
        self._btn_next.setStyleSheet(
            "font-size: 13px; padding: 6px; background-color: #4CAF50; color: white;"
        )
        self._btn_next.clicked.connect(self._on_next_step)
        self._btn_stop_seq = QPushButton(tr("Stop Sequence  ■"))
        self._btn_stop_seq.setStyleSheet(
            "font-size: 13px; padding: 6px; background-color: #FF9800; color: white;"
        )
        self._btn_stop_seq.clicked.connect(self._on_stop_sequence)
        mid_row.addWidget(self._btn_next)
        mid_row.addWidget(self._btn_stop_seq)
        exec_vlayout.addLayout(mid_row)

        end_row = QHBoxLayout()
        self._btn_return = QPushButton(tr("Return to Original Position"))
        self._btn_return.setStyleSheet(
            "font-size: 13px; padding: 6px; background-color: #9C27B0; color: white;"
        )
        self._btn_return.clicked.connect(self._on_return_to_origin)
        self._btn_stay = QPushButton(tr("Stop at Present Position"))
        self._btn_stay.setStyleSheet(
            "font-size: 13px; padding: 6px; background-color: #2196F3; color: white;"
        )
        self._btn_stay.clicked.connect(self._on_stay)
        end_row.addWidget(self._btn_return)
        end_row.addWidget(self._btn_stay)
        exec_vlayout.addLayout(end_row)

        root.addWidget(exec_group)

    # ── Pattern table helpers ─────────────────────────────────────────────────

    def _add_table_row(self, step_data: list[dict] | None = None) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        step_item = QTableWidgetItem(str(row + 1))
        step_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        step_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, 0, step_item)

        for pair_idx in range(_MAX_MOVES_PER_STEP):
            ch_col = 1 + pair_idx * 2
            diff_col = 2 + pair_idx * 2

            ch_spin = QSpinBox()
            ch_spin.setRange(0, 11)
            ch_spin.setSpecialValueText("—")

            diff_spin = QSpinBox()
            diff_spin.setRange(-9_999_999, 9_999_999)

            def _link(val, ds=diff_spin):
                ds.setEnabled(val != 0)

            ch_spin.valueChanged.connect(_link)

            if step_data is not None and pair_idx < len(step_data):
                ch_spin.setValue(step_data[pair_idx]["Ch"])
                diff_spin.setValue(step_data[pair_idx]["diff"])
            else:
                ch_spin.setValue(0)
                diff_spin.setEnabled(False)

            self._table.setCellWidget(row, ch_col, ch_spin)
            self._table.setCellWidget(row, diff_col, diff_spin)

    def _renumber_steps(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item:
                item.setText(str(row + 1))

    def _read_pattern(self) -> list[list[dict]]:
        pattern = []
        for row in range(self._table.rowCount()):
            step = []
            for pair_idx in range(_MAX_MOVES_PER_STEP):
                ch_spin = self._table.cellWidget(row, 1 + pair_idx * 2)
                diff_spin = self._table.cellWidget(row, 2 + pair_idx * 2)
                if ch_spin is not None and ch_spin.value() > 0:
                    step.append({"Ch": ch_spin.value(), "diff": diff_spin.value()})
            if step:
                pattern.append(step)
        return pattern

    # ── Pattern editor handlers ───────────────────────────────────────────────

    def _on_add_step(self) -> None:
        self._add_table_row()
        self._set_state("IDLE")

    def _on_remove_step(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        self._table.removeRow(rows[0].row())
        self._renumber_steps()
        self._set_state("IDLE")

    def _on_save(self) -> None:
        pattern = self._read_pattern()
        if not pattern:
            QMessageBox.warning(self, tr("Empty Pattern"), tr("No moves to save."))
            return
        last_dir = self._load_last_dir()
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save Pattern"),
            os.path.join(last_dir, "seq_move_pattern.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        self._save_last_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pattern, f, indent=2, ensure_ascii=False)

    def _on_load(self) -> None:
        last_dir = self._load_last_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Load Pattern"), last_dir, "JSON files (*.json)"
        )
        if not path:
            return
        self._save_last_dir(os.path.dirname(path))
        try:
            with open(path, "r", encoding="utf-8") as f:
                pattern = json.load(f)
            if not isinstance(pattern, list):
                raise ValueError(tr("Expected a JSON array of steps"))
            for i, step in enumerate(pattern):
                if not isinstance(step, list):
                    raise ValueError(tr("Step {n} must be a list", n=i + 1))
                if len(step) > _MAX_MOVES_PER_STEP:
                    raise ValueError(
                        tr("Step {n} has {count} moves; max is {max_count}",
                           n=i + 1, count=len(step), max_count=_MAX_MOVES_PER_STEP)
                    )
                for move in step:
                    if not isinstance(move, dict) or "Ch" not in move or "diff" not in move:
                        raise ValueError(
                            tr("Each move in step {n} must have 'Ch' and 'diff' keys", n=i + 1)
                        )
        except Exception as e:
            QMessageBox.critical(self, tr("Load Error"), str(e))
            return
        self._table.setRowCount(0)
        for step_data in pattern:
            self._add_table_row(step_data)
        self._set_state("IDLE")

    # ── Last-dir persistence ──────────────────────────────────────────────────

    def _load_last_dir(self) -> str:
        try:
            with open(_LAST_DIR_FILE, "r", encoding="utf-8") as f:
                d = f.read().strip()
            if os.path.isdir(d):
                return d
        except Exception:
            pass
        return os.path.expanduser("~")

    def _save_last_dir(self, directory: str) -> None:
        os.makedirs(_LOCALDATA_DIR, exist_ok=True)
        with open(_LAST_DIR_FILE, "w", encoding="utf-8") as f:
            f.write(directory)

    # ── Execution logic ───────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self.controller is None:
            QMessageBox.critical(self, tr("No Controller"),
                                 tr("No stage controller is connected."))
            return
        pattern = self._read_pattern()
        if not pattern:
            QMessageBox.warning(self, tr("Empty Pattern"),
                                tr("Add at least one step with a channel and Δpulse value."))
            return

        channels: set[int] = {m["Ch"] for step in pattern for m in step}
        original: dict[int, int] = {}
        for ch in channels:
            pos_str = self.controller.get_ch_pos(ch)
            if pos_str is None:
                QMessageBox.critical(self, tr("Read Error"),
                                     tr("Could not read current position of Ch{ch}.", ch=ch))
                return
            original[ch] = int(pos_str)

        self._pattern = pattern
        self._original_positions = original
        self._step_index = 0
        self._abort_requested.clear()
        self._execute_current_step()

    def _execute_current_step(self) -> None:
        step = self._pattern[self._step_index]
        total = len(self._pattern)
        self._set_state("MOVING")
        self._status_label.setText(
            tr("Executing step {n} / {total}…", n=self._step_index + 1, total=total)
        )

        def _worker() -> None:
            try:
                self.controller.switch_to_rem()
                for move in step:
                    if self._abort_requested.is_set():
                        break
                    self.controller.move_ch_relative(move["Ch"], move["diff"])
                self.controller.wait_until_stop()  # switches to LOC on completion
                if self._abort_requested.is_set():
                    self._move_aborted.emit()
                else:
                    self._move_done.emit()
            except Exception as e:
                try:
                    self.controller.wait_until_stop()
                except Exception:
                    pass
                self._move_error.emit(str(e))

        threading.Thread(target=_worker, daemon=True).start()

    @pyqtSlot()
    def _on_move_done(self) -> None:
        total = len(self._pattern)
        if self._step_index >= total - 1:
            self._set_state("FINAL_CHOICE")
            self._status_label.setText(tr("All {total} step(s) completed.", total=total))
        else:
            self._set_state("WAITING")
            self._status_label.setText(
                tr("Step {n} / {total} done. Ready for step {next}.",
                   n=self._step_index + 1, total=total, next=self._step_index + 2)
            )

    def _on_next_step(self) -> None:
        self._step_index += 1
        self._execute_current_step()

    def _on_stop_sequence(self) -> None:
        self._set_state("FINAL_CHOICE")
        self._status_label.setText(
            tr("Stopped after step {n} / {total}.", n=self._step_index + 1, total=len(self._pattern))
        )

    def _on_stay(self) -> None:
        self._set_state("IDLE")
        self._status_label.setText(tr("Sequence finished. Stopped at current position."))

    def _on_return_to_origin(self) -> None:
        self._abort_requested.clear()
        self._set_state("RETURNING")
        self._status_label.setText(tr("Returning to original position…"))
        original = dict(self._original_positions)

        def _worker() -> None:
            try:
                for ch, pos in original.items():
                    if self._abort_requested.is_set():
                        break
                    self.controller.move_ch_absolute(ch, pos)
                self.controller.wait_until_stop()
                if self._abort_requested.is_set():
                    self._move_aborted.emit()
                else:
                    self._return_done.emit()
            except Exception as e:
                try:
                    self.controller.wait_until_stop()
                except Exception:
                    pass
                self._move_error.emit(str(e))

        threading.Thread(target=_worker, daemon=True).start()

    @pyqtSlot()
    def _on_return_done(self) -> None:
        self._set_state("IDLE")
        self._status_label.setText(tr("Returned to original position."))

    @pyqtSlot(str)
    def _on_move_error(self, message: str) -> None:
        self._set_state("FINAL_CHOICE")
        self._status_label.setText(tr("Error: {msg}", msg=message))
        QMessageBox.critical(self, tr("Move Error"), message)

    def _on_emergency_stop(self) -> None:
        self._abort_requested.set()
        if self.controller is not None:
            try:
                self.controller.emergency_stop()
            except Exception:
                pass
        self._status_label.setText(tr("EMERGENCY STOP — AESTP sent."))
        if self._state == "WAITING":
            self._set_state("FINAL_CHOICE")
        # MOVING/RETURNING: the worker thread lands on FINAL_CHOICE via
        # _move_aborted once it observes the flag. IDLE: nothing in flight.

    @pyqtSlot()
    def _on_move_aborted(self) -> None:
        self._abort_requested.clear()
        self._set_state("FINAL_CHOICE")
        self._status_label.setText(tr("Emergency stop — sequence halted."))

    # ── State machine ─────────────────────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        self._state = state
        is_idle = state == "IDLE"
        is_moving = state == "MOVING"
        is_waiting = state == "WAITING"
        is_final = state == "FINAL_CHOICE"

        for w in (self._table, self._btn_add, self._btn_remove,
                  self._btn_save, self._btn_load):
            w.setEnabled(is_idle)

        self._btn_start.setEnabled(is_idle and self._table.rowCount() > 0)

        self._btn_next.setVisible(is_moving or is_waiting)
        self._btn_stop_seq.setVisible(is_moving or is_waiting)
        self._btn_next.setEnabled(is_waiting)
        self._btn_stop_seq.setEnabled(is_waiting)

        self._btn_stay.setVisible(is_final)
        self._btn_return.setVisible(is_final)
        self._btn_stay.setEnabled(is_final)
        self._btn_return.setEnabled(is_final)


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = SeqMoveWindow()
    win.show()
    sys.exit(app.exec())
