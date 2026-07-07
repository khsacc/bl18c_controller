"""Application settings window.

Left sidebar (QListWidget) selects pages shown in the right QStackedWidget.
New pages are registered with _add_page(); the list and stack stay in sync
automatically via currentRowChanged → setCurrentIndex.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout, QListWidget, QMainWindow, QSplitter,
    QStackedWidget, QWidget,
)

try:
    from settings.poni_state import PoniState
    from settings.pages.detector_calibration import DetectorCalibrationPage
    from settings.pages.logging_page import LoggingPage
    from settings.pages.notification_page import NotificationPage
    from settings.i18n import tr
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from settings.poni_state import PoniState
    from settings.pages.detector_calibration import DetectorCalibrationPage
    from settings.pages.logging_page import LoggingPage
    from settings.pages.notification_page import NotificationPage
    from settings.i18n import tr


class SettingsWindow(QMainWindow):
    """Non-modal settings window with a sidebar category list."""

    _SIDEBAR_WIDTH = 170

    def __init__(
        self,
        poni_state: PoniState,
        open_calibrate_instruments: Callable[[], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.resize(960, 740)

        self._poni_state = poni_state
        self._open_calibrate_instruments = open_calibrate_instruments
        self._setup_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background: #ccc; }")

        # ── Left sidebar ─────────────────────────────────────────────────────
        self._list = QListWidget()
        self._list.setFixedWidth(self._SIDEBAR_WIDTH)
        self._list.setStyleSheet("""
            QListWidget {
                font-size: 13px;
                border: none;
                background: #f2f2f2;
                outline: none;
            }
            QListWidget::item {
                padding: 11px 14px;
                border: none;
                color: #222;
            }
            QListWidget::item:selected {
                background: #2196F3;
                color: white;
            }
            QListWidget::item:hover:!selected {
                background: #e0e0e0;
            }
        """)
        splitter.addWidget(self._list)

        # ── Right content area ───────────────────────────────────────────────
        self._stack = QStackedWidget()
        splitter.addWidget(self._stack)

        splitter.setSizes([self._SIDEBAR_WIDTH, 960 - self._SIDEBAR_WIDTH])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        layout.addWidget(splitter)

        # ── Register pages ────────────────────────────────────────────────────
        self._add_page(
            tr("Detector Calibration"),
            DetectorCalibrationPage(self._poni_state, self._open_calibrate_instruments),
        )
        self._add_page(tr("Logging"), LoggingPage())
        self._add_page(tr("Notifications"), NotificationPage())

        self._list.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._list.setCurrentRow(0)

    def _add_page(self, name: str, widget: QWidget) -> None:
        self._list.addItem(name)
        self._stack.addWidget(widget)
