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
Before this window is created, ``confirm_pm16c_console_access()`` presents a
developer warning and requires the user to answer a basic protocol question.
The warning label below remains visible after that one-time launch gate.

Development-menu apps are English-only and do not use ``settings.i18n``.
"""
from __future__ import annotations

import datetime

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

try:
    from utils.stage.control_stage import PM16CCommError, PM16CTimeoutError
    from utils.stage.errors import MotionLeaseError
except ImportError:
    import os as _os, sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from utils.stage.control_stage import PM16CCommError, PM16CTimeoutError
    from utils.stage.errors import MotionLeaseError


_ACCESS_QUIZ_ANSWER = "STS4?"


class Pm16cConsoleAccessDialog(QDialog):
    """One-time launch gate for the unrestricted developer console."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restricted Developer Tool")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        warning = QLabel(
            "WARNING: This application is a developer-only tool. It sends raw "
            "commands directly to the PM16C and bypasses the normal movement "
            "constraints and safety limits. Do not use it unless you are "
            "experienced with PM16C command operation and understand the "
            "physical consequences of every command."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b00020; font-weight: bold;")
        layout.addWidget(warning)

        question = QLabel(
            "QUIZ: What command reads the current status of channel 4?"
        )
        question.setWordWrap(True)
        layout.addWidget(question)

        self._answer_input = QLineEdit()
        self._answer_input.setPlaceholderText("Enter the PM16C command")
        self._answer_input.returnPressed.connect(self._check_answer)
        layout.addWidget(self._answer_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._open_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._open_button.setText("Open Console")
        buttons.accepted.connect(self._check_answer)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._answer_input.setFocus()

    def _check_answer(self) -> None:
        if self._answer_input.text().strip() == _ACCESS_QUIZ_ANSWER:
            self.accept()
            return

        QMessageBox.warning(
            self,
            "Access Denied",
            "The answer is incorrect. The PM16C Console will not be opened.",
        )
        self.reject()


def confirm_pm16c_console_access(parent=None) -> bool:
    """Return True only when the developer warning quiz is answered correctly."""
    return Pm16cConsoleAccessDialog(parent).exec() == QDialog.DialogCode.Accepted


class Pm16cConsoleWindow(QMainWindow):
    """Sends raw ASCII commands to the shared PM16C connection and shows the
    reply (or a timeout notice). Always uses the connection shared from the
    main window (``controller``) — never opens a second connection, since
    the PM16C hardware serves one TCP client at a time."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PM16C Console (Development)")
        self.resize(560, 520)

        self._controller = controller
        self._lease = None  # MotionLease held by this console, or None

        self._setup_ui()
        self._update_lease_status()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        warning = QLabel(
            "Warning: commands here are sent directly to the PM16C with no "
            "safety checks — MOVE_CONSTRAINTS (e.g. Ch8/Ch9 collision) and "
            "per-channel speed/move limits are NOT applied. Motion/speed/mode "
            "commands still require holding motion ownership — use the "
            "controls below."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b00; font-weight: bold;")
        layout.addWidget(warning)

        # ── Motion ownership controls ────────────────────────────────────
        lease_row = QHBoxLayout()
        self._lease_status_label = QLabel("Motion: (checking…)")
        lease_row.addWidget(self._lease_status_label, 1)
        self._acquire_btn = QPushButton("Acquire Motion")
        self._acquire_btn.clicked.connect(self._on_acquire)
        lease_row.addWidget(self._acquire_btn)
        self._release_btn = QPushButton("Release Motion")
        self._release_btn.clicked.connect(self._on_release)
        lease_row.addWidget(self._release_btn)
        self._recover_btn = QPushButton("Recover")
        self._recover_btn.setToolTip(
            "Force-stop, confirm, and free motion ownership when it is stuck "
            "in RECOVERY_REQUIRED (a stop could not be sent or confirmed)."
        )
        self._recover_btn.clicked.connect(self._on_recover)
        lease_row.addWidget(self._recover_btn)
        layout.addLayout(lease_row)

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

    def _update_lease_status(self) -> None:
        if self._lease is not None and self._controller.coordinator.is_valid(self._lease):
            self._lease_status_label.setText(
                f"Motion: HELD by this console (lease {self._lease.lease_id})"
            )
        else:
            if self._lease is not None:
                # Held reference went stale (revoked by a stop elsewhere).
                self._lease = None
            holder = self._controller.get_motion_holder()
            state = self._controller.coordinator.state().value
            if holder:
                self._lease_status_label.setText(
                    f"Motion: in use by \"{holder['owner']}\" ({holder['operation']}) "
                    f"[state={state}]"
                )
            else:
                self._lease_status_label.setText(f"Motion: free [state={state}]")

    def _on_acquire(self) -> None:
        if self._lease is not None:
            return
        try:
            self._lease = self._controller.acquire_motion(
                owner="PM16C Console", operation="Manual raw commands",
            )
        except Exception as e:
            QMessageBox.warning(self, "Acquire Failed", str(e))
        self._update_lease_status()

    def _on_release(self) -> None:
        if self._lease is not None:
            self._controller.release_motion(self._lease)
            self._lease = None
        self._update_lease_status()

    def _on_recover(self) -> None:
        self._lease = None
        try:
            future = self._controller.recover_motion(source="PM16C Console")
            ok = future.result(timeout=45.0)
            if not ok:
                QMessageBox.warning(self, "Recovery Failed",
                                    "Motion recovery did not complete successfully.")
        except Exception as e:
            QMessageBox.warning(self, "Recovery Failed", str(e))
        self._update_lease_status()

    def _on_send(self) -> None:
        command = self._command_input.text().strip()
        if not command:
            return
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self._send_btn.setEnabled(False)
        try:
            try:
                response = self._controller.send_cmd(
                    command, has_response=True, motion=self._lease)
            except PM16CTimeoutError:
                self._log.appendPlainText(f"[{timestamp}] >> {command}\n<< No response (timed out)")
                return
            except MotionLeaseError as e:
                self._log.appendPlainText(
                    f"[{timestamp}] >> {command}\n<< REFUSED (motion ownership): {e}")
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
            self._update_lease_status()


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    from utils.stage.control_stage import PM16CController

    app = QApplication(sys.argv)
    if not confirm_pm16c_console_access():
        sys.exit(0)

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
