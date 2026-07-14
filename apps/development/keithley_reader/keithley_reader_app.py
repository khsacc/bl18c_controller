"""Keithley Reader window (Development tool).

Minimal diagnostic window for the shared Keithley 2000 GPIB reader
(``utils.keithley2000_reader.Keithley2000Reader``). "Read" fetches
``read_transmitted()`` (photodiode current, A) and ``read_incident()``
(ion chamber voltage, V — currently a hard-coded 1.0 placeholder, see that
module's docstring). A raw SCPI console is included so the ion-chamber
wiring (FRONT/REAR terminal selection, function switching, etc.) can be
probed on real hardware before ``read_incident()`` is given a real
implementation.

Development-menu apps are English-only and do not use ``settings.i18n``.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

try:
    from utils.keithley2000_reader import Keithley2000Reader
except ImportError:
    import os as _os, sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from utils.keithley2000_reader import Keithley2000Reader


class KeithleyReaderWindow(QMainWindow):
    """Shows transmitted/incident readings on demand; also exposes a raw
    SCPI console. Always uses the Keithley connection shared from the main
    window (``reader``) — never opens its own GPIB session, since GPIB
    access is typically exclusive to one handle at a time."""

    def __init__(self, reader: Keithley2000Reader, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keithley Reader (Development)")
        self.resize(520, 480)

        self._reader = reader

        self._setup_ui()
        self._update_status()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self._status_label = QLabel()
        layout.addWidget(self._status_label)

        readings_group = QGroupBox("Readings")
        readings_layout = QVBoxLayout(readings_group)

        transmitted_row = QHBoxLayout()
        transmitted_row.addWidget(QLabel("Transmitted (A):"))
        self._transmitted_label = QLabel("—")
        transmitted_row.addWidget(self._transmitted_label)
        transmitted_row.addStretch()
        readings_layout.addLayout(transmitted_row)

        incident_row = QHBoxLayout()
        incident_row.addWidget(QLabel("Incident (V):"))
        self._incident_label = QLabel("—")
        incident_row.addWidget(self._incident_label)
        incident_row.addStretch()
        readings_layout.addLayout(incident_row)

        readings_layout.addWidget(QLabel(
            "Incident is currently a hard-coded 1.0 placeholder — the ion "
            "chamber is not yet wired into read_incident(). Use the raw "
            "SCPI console below to find the real command, then update "
            "utils/keithley2000_reader.py."
        ))

        self._read_btn = QPushButton("Read")
        self._read_btn.clicked.connect(self._on_read)
        readings_layout.addWidget(self._read_btn)

        layout.addWidget(readings_group)

        scpi_group = QGroupBox("Raw SCPI console")
        scpi_layout = QVBoxLayout(scpi_group)

        send_row = QHBoxLayout()
        self._scpi_input = QLineEdit()
        self._scpi_input.setPlaceholderText("e.g. :ROUT:TERM? or :FUNC?")
        self._scpi_input.returnPressed.connect(self._on_scpi_query)
        send_row.addWidget(self._scpi_input)
        query_btn = QPushButton("Query")
        query_btn.clicked.connect(self._on_scpi_query)
        send_row.addWidget(query_btn)
        write_btn = QPushButton("Write (no response)")
        write_btn.clicked.connect(self._on_scpi_write)
        send_row.addWidget(write_btn)
        scpi_layout.addLayout(send_row)

        self._scpi_log = QPlainTextEdit()
        self._scpi_log.setReadOnly(True)
        scpi_layout.addWidget(self._scpi_log)

        layout.addWidget(scpi_group)

    def _update_status(self) -> None:
        mode = "Talk-Only" if self._reader.is_talk_only else "SCPI"
        self._status_label.setText(f"Connected  ({mode} mode)")

    def _on_read(self) -> None:
        self._read_btn.setEnabled(False)
        try:
            self._transmitted_label.setText(str(self._reader.read_transmitted()))
            self._incident_label.setText(str(self._reader.read_incident()))
        finally:
            self._read_btn.setEnabled(True)

    def _on_scpi_query(self) -> None:
        command = self._scpi_input.text().strip()
        if not command:
            return
        try:
            response = self._reader.query(command)
            self._scpi_log.appendPlainText(f">> {command}\n<< {response}")
        except Exception as e:
            self._scpi_log.appendPlainText(f">> {command}\n<< ERROR: {e}")

    def _on_scpi_write(self) -> None:
        command = self._scpi_input.text().strip()
        if not command:
            return
        try:
            self._reader.write(command)
            self._scpi_log.appendPlainText(f">> {command}\n(written, no response requested)")
        except Exception as e:
            self._scpi_log.appendPlainText(f">> {command}\n<< ERROR: {e}")


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    try:
        reader = Keithley2000Reader()
    except Exception as e:
        QMessageBox.critical(None, "Keithley 2000 Connection Error",
                              f"Could not connect to Keithley 2000:\n{e}")
        sys.exit(1)
    window = KeithleyReaderWindow(reader=reader)
    window.show()
    sys.exit(app.exec())
