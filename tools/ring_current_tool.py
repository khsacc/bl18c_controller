"""Manual PF ring current read-out through the app's shared STARS client."""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QLabel, QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget


class RingCurrentToolWindow(QMainWindow):
    _read_done = pyqtSignal(str, str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Read ring current")
        self.resize(380, 170)
        self._controller = controller
        self._busy = False
        self._worker: threading.Thread | None = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        self._value = QLabel("No reading yet")
        self._value.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(self._value)
        self._detail = QLabel("Value and units are shown as returned by STARS.")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._read_button = QPushButton("Read current")
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
        self._detail.setText("Reading from STARS…")

        def run() -> None:
            try:
                self._read_done.emit(self._controller.read_ring_current(), "")
            except Exception as exc:
                self._read_done.emit("", str(exc))

        self._worker = threading.Thread(target=run, name="ring-current-read", daemon=True)
        self._worker.start()

    @pyqtSlot(str, str)
    def _on_read_done(self, value: str, error: str) -> None:
        self._busy = False
        self._read_button.setEnabled(True)
        if error:
            self._value.setText("Reading unavailable")
            self._detail.setText(error)
        else:
            self._value.setText(value)
            self._detail.setText("Raw STARS BEAMCUR value; confirm its units on site.")

    def closeEvent(self, event) -> None:
        if self._busy:
            QMessageBox.warning(self, "Read in progress", "Wait for the ring current read to finish.")
            event.ignore()
            return
        event.accept()
