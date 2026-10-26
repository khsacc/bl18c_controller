"""Manual PF ring current and operating-mode read-out through shared STARS."""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QLabel, QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget

from utils.shutter import ShutterControllerSim


_RING_MODES = {
    0: "Shutdown",
    1: "Linac",
    2: "Injection",
    3: "Storage",
    4: "User",
    5: "User (Top-UP)",
}


class RingCurrentToolWindow(QMainWindow):
    _read_done = pyqtSignal(str, str, object, str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Read ring current")
        self.resize(440, 230)
        self._controller = controller
        self._busy = False
        self._worker: threading.Thread | None = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        self._value = QLabel("No reading yet")
        self._value.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(self._value)
        self._detail = QLabel("PF ring current (Ring.DCCT), in mA.")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._status = QLabel("Ring status: No reading yet")
        self._status.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._status)
        self._status_detail = QLabel("Operating mode from Ring.Status.")
        self._status_detail.setWordWrap(True)
        layout.addWidget(self._status_detail)
        self._read_button = QPushButton("Read current and status")
        self._read_button.clicked.connect(self.read_current)
        layout.addWidget(self._read_button)
        layout.addStretch()

        self._read_done.connect(self._on_read_done)
        self.read_current()

    @property
    def is_busy(self) -> bool:
        return self._busy

    def read_current(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._read_button.setEnabled(False)
        self._value.setText("Reading current…")
        self._detail.setText("Reading from STARS…")
        self._status.setText("Ring status: Reading…")
        self._status.setStyleSheet("font-size: 18px; font-weight: bold;")
        self._status_detail.setText("Reading from STARS…")

        def run() -> None:
            try:
                current, current_error = self._controller.read_ring_current(), ""
            except Exception as exc:
                current, current_error = "", str(exc)
            try:
                status, status_error = self._controller.read_ring_status(), ""
            except Exception as exc:
                status, status_error = None, str(exc)
            self._read_done.emit(current, current_error, status, status_error)

        self._worker = threading.Thread(target=run, name="ring-current-read", daemon=True)
        self._worker.start()

    @pyqtSlot(str, str, object, str)
    def _on_read_done(self, value: str, error: str, status: int | None, status_error: str) -> None:
        self._busy = False
        self._read_button.setEnabled(True)
        if error:
            self._value.setText("Reading unavailable")
            self._detail.setText(error)
        else:
            self._value.setText(f"{value} mA")
            self._detail.setText(
                "Simulated ring current." if isinstance(self._controller, ShutterControllerSim)
                else "Ring.DCCT value from STARS."
            )
        if status_error:
            self._status.setText("Ring status: Reading unavailable")
            self._status.setStyleSheet("font-size: 18px; font-weight: bold; color: #ad6a00;")
            self._status_detail.setText(status_error)
        else:
            mode = _RING_MODES.get(status, "Unknown")
            self._status.setText(f"Ring status: {status} — {mode}")
            colour = "#2e7d32" if status in (4, 5) else "#c62828"
            self._status.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {colour};")
            self._status_detail.setText(
                "Simulated Ring.Status." if isinstance(self._controller, ShutterControllerSim)
                else "Ring.Status value from STARS."
            )

    def closeEvent(self, event) -> None:
        if self._busy:
            QMessageBox.warning(self, "Read in progress", "Wait for the ring readings to finish.")
            event.ignore()
            return
        event.accept()
