"""Manual/automatic STARS stopper control (Tools menu)."""
from __future__ import annotations

import threading
from contextlib import nullcontext

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QLabel, QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
    QRadioButton, QHBoxLayout,
)

from settings.i18n import tr
from utils.beam_control import beam_control
from utils.shutter import ShutterController, ShutterControllerSim


class ShutterToolWindow(QMainWindow):
    _status_received = pyqtSignal(object)
    _operation_done = pyqtSignal(object, str)

    def __init__(self, controller: ShutterController | ShutterControllerSim, parent=None, *, policy=None):
        super().__init__(parent)
        self.setWindowTitle(tr("X-ray shutter"))
        self.resize(370, 220)
        self._controller = controller
        self._policy = policy if policy is not None else beam_control
        if self._policy.controller is not controller:
            self._policy.configure(controller)
        self._inserted: bool | None = None
        self._busy = False
        self._worker: threading.Thread | None = None
        self._status_listener = self._status_received.emit

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        modes = QHBoxLayout()
        self._manual_radio = QRadioButton("Manual Control")
        self._auto_radio = QRadioButton("Auto Control")
        self._manual_radio.setChecked(not self._policy.automatic)
        self._auto_radio.setChecked(self._policy.automatic)
        modes.addWidget(self._manual_radio)
        modes.addWidget(self._auto_radio)
        layout.addLayout(modes)
        self._manual_radio.clicked.connect(lambda: self._start_operation(automatic=False))
        self._auto_radio.clicked.connect(lambda: self._start_operation(automatic=True))
        self._status_label = QLabel()
        self._status_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._status_label)
        self._detail_label = QLabel(tr("Connecting to STARS…"))
        self._detail_label.setWordWrap(True)
        layout.addWidget(self._detail_label)
        self._toggle_button = QPushButton()
        self._toggle_button.clicked.connect(self._toggle)
        layout.addWidget(self._toggle_button)
        self._refresh_button = QPushButton(tr("Refresh status"))
        self._refresh_button.clicked.connect(lambda: self._start_operation())
        layout.addWidget(self._refresh_button)
        layout.addStretch()

        self._status_received.connect(self._on_status_received)
        self._operation_done.connect(self._on_operation_done)
        self._controller.add_status_listener(self._status_listener)
        self._render_status()
        self._start_operation()

    @property
    def is_busy(self) -> bool:
        return self._busy

    def _render_status(self) -> None:
        if self._inserted is True:
            self._status_label.setText(tr("IN — X-rays blocked"))
            self._status_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #a65e64;")
            self._toggle_button.setText(tr("Shutter OUT (introduce beam)"))
        elif self._inserted is False:
            self._status_label.setText(tr("OUT — X-rays at sample"))
            self._status_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #5e83a6;")
            self._toggle_button.setText(tr("Shutter IN (block beam)"))
        else:
            self._status_label.setText(tr("Unknown shutter position"))
            self._status_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #ad6a00;")
            self._toggle_button.setText(tr("Shutter position unavailable"))
        self._toggle_button.setEnabled(not self._busy and self._inserted is not None and not self._policy.automatic)
        self._refresh_button.setEnabled(not self._busy)
        self._manual_radio.setEnabled(not self._busy)
        self._auto_radio.setEnabled(not self._busy)
        self._manual_radio.setChecked(not self._policy.automatic)
        self._auto_radio.setChecked(self._policy.automatic)

    @pyqtSlot(object)
    def _on_status_received(self, inserted: bool | None) -> None:
        self._inserted = inserted
        self._render_status()

    def _start_operation(self, *, expected: bool | None = None, target: bool | None = None,
                         automatic: bool | None = None) -> None:
        if self._busy:
            return
        self._busy = True
        self._detail_label.setText(tr("Communicating with STARS…"))
        self._render_status()

        def run() -> None:
            try:
                if automatic is not None:
                    self._policy.set_automatic(automatic)
                reservation = self._policy.session(manual=True) if target is not None else nullcontext()
                with reservation:
                    current = self._controller.get_status()
                    if target is not None:
                        if current is not expected:
                            raise RuntimeError(tr("Shutter position changed; refresh and try again"))
                        self._controller.set_inserted(target)
                        current = self._controller.get_status()
                self._operation_done.emit(current, "")
            except Exception as exc:
                self._operation_done.emit(None, str(exc))

        self._worker = threading.Thread(target=run, name="shutter-tool-operation", daemon=True)
        self._worker.start()

    @pyqtSlot(object, str)
    def _on_operation_done(self, inserted: bool | None, error: str) -> None:
        self._busy = False
        self._inserted = inserted
        self._detail_label.setText(
            tr("Could not confirm shutter position: {error}", error=error)
            if error else tr("")
        )
        self._render_status()

    def _toggle(self) -> None:
        if self._busy or self._inserted is None or self._policy.automatic:
            return
        expected = self._inserted
        target = not expected
        self._start_operation(expected=expected, target=target)

    def closeEvent(self, event) -> None:
        if self._busy:
            QMessageBox.warning(self, tr("Shutter operation in progress"),
                                tr("Wait for the shutter operation to finish before closing this window."))
            event.ignore()
            return
        self._controller.remove_status_listener(self._status_listener)
        self._controller.close()
        event.accept()
