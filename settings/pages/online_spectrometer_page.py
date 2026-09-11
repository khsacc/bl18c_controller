"""Settings page for the app-wide FluoRaPressée connection."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

try:
    from settings import online_spectrometer_prefs
    from settings.i18n import tr
except ImportError:
    import os
    import sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    from settings import online_spectrometer_prefs
    from settings.i18n import tr


class OnlineSpectrometerPage(QWidget):
    """Edit the shared online-spectrometer IP address and API key."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        group = QGroupBox(tr("FluoRaPressée connection"))
        form = QFormLayout(group)
        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText(online_spectrometer_prefs.DEFAULT_IP_ADDRESS)
        form.addRow(tr("IP Address:"), self._ip_edit)
        form.addRow(tr("Port:"), QLabel(str(online_spectrometer_prefs.DEFAULT_PORT)))

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(tr("API key:"), self._api_key_edit)

        show_key = QCheckBox(tr("Show API key"))
        show_key.toggled.connect(
            lambda shown: self._api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        form.addRow("", show_key)

        save_button = QPushButton(tr("Save connection settings"))
        save_button.clicked.connect(self._save)
        form.addRow("", save_button)
        root.addWidget(group)

        note = QLabel(tr(
            "These settings are shared by Ruby Finder and Experimental Scheduler. "
            "The API key is stored locally on this control PC and is not written "
            "to experiment sequences or logs."
        ))
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 11px; color: #666;")
        root.addWidget(note)
        root.addStretch(1)
        self._refresh()

    def _refresh(self) -> None:
        self._ip_edit.setText(online_spectrometer_prefs.get_ip_address())
        self._api_key_edit.setText(online_spectrometer_prefs.get_api_key())

    def _save(self) -> None:
        try:
            online_spectrometer_prefs.set_connection(
                self._ip_edit.text(), self._api_key_edit.text()
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("Invalid spectrometer settings"), tr(str(exc)))
            return
        QMessageBox.information(
            self, tr("Online spectrometer"), tr("Connection settings saved.")
        )
