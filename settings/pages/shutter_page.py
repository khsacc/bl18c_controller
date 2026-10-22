"""Connection settings for the STARS X-ray stopper client."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from settings import shutter_prefs
from settings.i18n import tr


class ShutterPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        group = QGroupBox(tr("X-ray shutter (STARS)"))
        form = QFormLayout(group)
        self._ip = QLineEdit()
        self._ip.setPlaceholderText(shutter_prefs.DEFAULT_IP_ADDRESS)
        self._keyword = QLineEdit()
        self._keyword.setEchoMode(QLineEdit.EchoMode.Password)
        ip_address, keyword = shutter_prefs.load()
        self._ip.setText(ip_address)
        self._keyword.setText(keyword)
        form.addRow(tr("IP Address:"), self._ip)
        form.addRow(tr("Port:"), QLabel(str(shutter_prefs.PORT)))
        form.addRow(tr("Node name:"), QLabel(shutter_prefs.NODE_NAME))
        form.addRow(tr("STARS keyword:"), self._keyword)
        save = QPushButton(tr("Save connection settings"))
        save.clicked.connect(self._save)
        form.addRow("", save)
        root.addWidget(group)
        note = QLabel(tr("Reopen the shutter tool after changing these settings. The keyword is stored locally on this PC."))
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch()

    def _save(self) -> None:
        try:
            shutter_prefs.save(self._ip.text(), self._keyword.text())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("Invalid shutter settings"), str(exc))
            return
        QMessageBox.information(self, tr("X-ray shutter (STARS)"), tr("Connection settings saved."))
