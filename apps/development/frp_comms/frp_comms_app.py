"""FluoRaPressée Comms window (Development tool).

Minimal diagnostic window for the FluoRaPressée online-spectrometer HTTP API
(``apps.ruby_finder.ruby_finder_backend.FluoraPresseeReader``). Checks
``GET /status`` on demand and acquires a single spectrum via
``POST /acquire``, plotting it with pyqtgraph. Connection settings (IP
address, API key) are the shared ones from Settings > Online spectrometer
(``settings.online_spectrometer_prefs``) — there is nothing to configure
here.

A fresh reader is built for every request rather than reusing one shared
instance: unlike the Keithley's exclusive GPIB handle, the FRP HTTP API has
no single-session concept to share.

Development-menu apps are English-only and do not use ``settings.i18n``.
"""
from __future__ import annotations

import json

import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

try:
    from apps.ruby_finder.ruby_finder_backend import (
        FluoraPresseeError, FluoraPresseeReader,
    )
    from settings import online_spectrometer_prefs
except ImportError:
    import os as _os
    import sys as _sys
    _pkg = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from apps.ruby_finder.ruby_finder_backend import (
        FluoraPresseeError, FluoraPresseeReader,
    )
    from settings import online_spectrometer_prefs


class FrpCommsWindow(QMainWindow):
    """On-demand status check and single-spectrum acquisition against the
    FluoRaPressée HTTP API."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FluoRaPressée Comms (Development)")
        self.resize(760, 600)

        self._setup_ui()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self._target_label = QLabel(f"Target: {online_spectrometer_prefs.get_base_url()}")
        layout.addWidget(self._target_label)

        btn_row = QHBoxLayout()
        self._status_btn = QPushButton("Check status")
        self._status_btn.clicked.connect(self._on_check_status)
        btn_row.addWidget(self._status_btn)
        self._acquire_btn = QPushButton("Acquire spectrum")
        self._acquire_btn.clicked.connect(self._on_acquire)
        btn_row.addWidget(self._acquire_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        plot_group = QGroupBox("Spectrum")
        plot_layout = QVBoxLayout(plot_group)
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setBackground("w")
        self._plot_widget.setLabel("bottom", "x")
        self._plot_widget.setLabel("left", "y")
        self._plot_curve = self._plot_widget.plot([], [], pen="k")
        plot_layout.addWidget(self._plot_widget)
        layout.addWidget(plot_group, stretch=1)

        log_group = QGroupBox("Raw response")
        log_layout = QVBoxLayout(log_group)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        log_layout.addWidget(self._log)
        layout.addWidget(log_group, stretch=1)

    def _build_reader(self) -> FluoraPresseeReader | None:
        try:
            return FluoraPresseeReader(
                base_url=online_spectrometer_prefs.get_base_url(),
                api_key=online_spectrometer_prefs.get_api_key(),
            )
        except ValueError as exc:
            self._log.appendPlainText(f"ERROR: {exc}")
            return None

    def _on_check_status(self) -> None:
        self._target_label.setText(f"Target: {online_spectrometer_prefs.get_base_url()}")
        reader = self._build_reader()
        if reader is None:
            return
        self._status_btn.setEnabled(False)
        try:
            status = reader.get_status()
            self._log.appendPlainText(
                f">> GET /status\n<< {json.dumps(status, indent=2, ensure_ascii=False)}"
            )
        except FluoraPresseeError as exc:
            self._log.appendPlainText(f">> GET /status\n<< ERROR: {exc}")
        finally:
            self._status_btn.setEnabled(True)

    def _on_acquire(self) -> None:
        self._target_label.setText(f"Target: {online_spectrometer_prefs.get_base_url()}")
        reader = self._build_reader()
        if reader is None:
            return
        self._acquire_btn.setEnabled(False)
        try:
            result = reader.acquire_spectrum()
            y = result["y"]
            x = result.get("x") or list(range(len(y)))
            self._plot_curve.setData(x, y)
            self._log.appendPlainText(
                f">> POST /acquire\n<< {len(y)} points, "
                f"exposure={result.get('exposure_time_s')}s, "
                f"accumulations={result.get('accumulations')}"
            )
        except FluoraPresseeError as exc:
            self._log.appendPlainText(f">> POST /acquire\n<< ERROR: {exc}")
        finally:
            self._acquire_btn.setEnabled(True)


if __name__ == "__main__":
    import sys

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    window = FrpCommsWindow()
    window.show()
    sys.exit(app.exec())
