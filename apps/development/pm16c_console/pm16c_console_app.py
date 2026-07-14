"""PM16C Console window (Development tool).

Minimal diagnostic console for the shared ``PM16CController`` /
``PM16CControllerSim`` connection. The user types a raw ASCII command and
presses Send (or Enter); the console always waits for a response and shows
"No response" if the controller's socket read times out (2.0 s, set once in
``PM16CController.connect()`` and shared by every window using this
connection — see ``utils/stage/IMPLEMENTATION_DETAILS.md``).

Raw commands go straight to ``send_cmd()`` and bypass ``MOVE_CONSTRAINTS``
and the per-channel software limits — those checks live only in
``move_ch_absolute``/``move_ch_relative`` etc., not in ``send_cmd()`` itself.
The warning label below is permanent, not a per-send confirmation (agreed
with the user, 2026-07-14).

Development-menu apps are English-only and do not use ``settings.i18n``.
"""
from __future__ import annotations

import datetime

from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

try:
    from utils.stage.control_stage import PM16CCommError, PM16CTimeoutError
except ImportError:
    import os as _os, sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from utils.stage.control_stage import PM16CCommError, PM16CTimeoutError


class Pm16cConsoleWindow(QMainWindow):
    """Sends raw ASCII commands to the shared PM16C connection and shows the
    reply (or a timeout notice). Always uses the connection shared from the
    main window (``controller``) — never opens a second connection, since
    the PM16C hardware serves one TCP client at a time."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PM16C Console (Development)")
        self.resize(560, 480)

        self._controller = controller

        self._setup_ui()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        warning = QLabel(
            "Warning: commands here are sent directly to the PM16C with no "
            "safety checks — MOVE_CONSTRAINTS (e.g. Ch8/Ch9 collision) and "
            "per-channel speed/move limits are NOT applied."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b00; font-weight: bold;")
        layout.addWidget(warning)

        send_row = QHBoxLayout()
        self._command_input = QLineEdit()
        self._command_input.setPlaceholderText("e.g. STQ? or STSx?9")
        self._command_input.returnPressed.connect(self._on_send)
        send_row.addWidget(self._command_input)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        send_row.addWidget(self._send_btn)
        layout.addLayout(send_row)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        layout.addWidget(self._log)

    def _on_send(self) -> None:
        command = self._command_input.text().strip()
        if not command:
            return
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self._send_btn.setEnabled(False)
        try:
            try:
                response = self._controller.send_cmd(command, has_response=True)
            except PM16CTimeoutError:
                self._log.appendPlainText(f"[{timestamp}] >> {command}\n<< No response (timed out)")
                return
            except PM16CCommError as e:
                self._log.appendPlainText(f"[{timestamp}] >> {command}\n<< ERROR: {e}")
                return
            except Exception as e:
                self._log.appendPlainText(f"[{timestamp}] >> {command}\n<< ERROR: {e}")
                return
            if response is None:
                self._log.appendPlainText(f"[{timestamp}] >> {command}\n<< No response")
            else:
                self._log.appendPlainText(f"[{timestamp}] >> {command}\n<< {response}")
        finally:
            self._send_btn.setEnabled(True)
            self._command_input.clear()


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from utils.stage.control_stage import PM16CController

    app = QApplication(sys.argv)
    controller = PM16CController(ip='192.168.1.55', port=7777, debug=True)
    try:
        controller.connect()
    except Exception as e:
        QMessageBox.critical(None, "PM16C Connection Error",
                              f"Could not connect to PM16C controller:\n{e}")
        sys.exit(1)
    window = Pm16cConsoleWindow(controller=controller)
    window.show()
    sys.exit(app.exec())
