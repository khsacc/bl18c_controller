"""Standalone clock showing the PC's local time."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QDateTime, Qt, QTimer
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import i18n

_CLOCK_FONT_PX = 80


class ClockWindow(QWidget):
    """Display the current local time in hours, minutes and seconds."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("ClockWindow")
        self.setStyleSheet("QWidget#ClockWindow { background-color: #FFFFFF; }")
        self.setWindowTitle(i18n.tr("Clock"))
        self.setMinimumSize(720, 180)
        self.resize(760, 220)

        self.time_label = QLabel(self)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPixelSize(_CLOCK_FONT_PX)
        font.setBold(True)
        self.time_label.setFont(font)
        self.time_label.setStyleSheet(
            f'color: #111827; background-color: transparent; '
            f'font-family: "{font.family()}"; font-size: {_CLOCK_FONT_PX}px; font-weight: bold;'
        )

        layout = QVBoxLayout(self)
        layout.addWidget(self.time_label)

        self._update_time()
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._update_time)
        self._timer.start()

    def _update_time(self) -> None:
        self.time_label.setText(QDateTime.currentDateTime().toString("HH:mm:ss"))


def main() -> int:
    app = QApplication(sys.argv)
    i18n.load()
    window = ClockWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
