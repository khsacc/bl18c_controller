"""
LakeShore 335 temperature controller window (PyQt6).

Can be used in two ways:

1. Launched from main.py with a pre-connected backend::

       window = LakeShore335Window(backend=backend)

   In this mode the connection bar is hidden; the caller owns the backend
   lifecycle.

2. Standalone (no backend provided)::

       window = LakeShore335Window()

   A connection bar is shown so the user can specify a GPIB address and connect.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QCheckBox, QRadioButton, QButtonGroup, QSpinBox,
    QFileDialog, QMessageBox,
)

import pyqtgraph as pg

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

try:
    from .lakeshore335_backend import LakeShore335Backend, DataPoint, DEFAULT_GPIB_ADDRESS
except ImportError:
    _pkg = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _pkg not in sys.path:
        sys.path.insert(0, _pkg)
    from apps.LakeShore335.lakeshore335_backend import LakeShore335Backend, DataPoint, DEFAULT_GPIB_ADDRESS

def tr(text: str, **kwargs) -> str:
    """Identity translator: this app always displays English text,
    independent of the rest of the application's language setting."""
    return text.format(**kwargs) if kwargs else text

PLOT_WINDOW_SECONDS = 300


def _no_wheel(widget):
    """Spin/combo boxes must not react to mouse-wheel scrolling — scrolling a
    panel that happens to be under the cursor should never change a value."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


class LakeShore335Window(QMainWindow):
    """Top-level window for the LakeShore 335 app.

    When *backend* is provided it is used as-is (managed by the launcher).
    When omitted the window manages its own connection via the UI.
    """

    def __init__(self, backend: LakeShore335Backend | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("LakeShore 335 Temperature Controller"))
        self.resize(1000, 740)

        self._backend      = backend
        self._owns_backend = backend is None

        self._last_setpoint_k = None
        self._last_heater_name = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Always-visible status/window bar
        root.addWidget(self._build_status_bar())

        # Connection bar only when running standalone
        if self._owns_backend:
            root.addWidget(self._build_connection_bar())

        root.addWidget(self._build_plot_frame(), stretch=1)
        root.addWidget(self._build_control_panel())
        root.addWidget(self._build_logging_bar())

        # Periodic timer for log-row-count refresh
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

        if backend is not None:
            self._connect_backend_signals()
            self._sync_controls_from_device()
            self._set_status(tr("● Connected"), "green")

    # ================================================================
    # UI construction
    # ================================================================

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self._status_label = QLabel(tr("Not connected"))
        self._status_label.setStyleSheet("font-weight: bold; color: gray;")
        layout.addWidget(self._status_label)

        layout.addStretch()
        return bar

    def _build_connection_bar(self) -> QGroupBox:
        box = QGroupBox(tr("Connection"))
        layout = QHBoxLayout(box)

        layout.addWidget(QLabel(tr("GPIB Address:")))
        self._gpib_edit = QLineEdit(DEFAULT_GPIB_ADDRESS)
        self._gpib_edit.setMinimumWidth(180)
        layout.addWidget(self._gpib_edit)

        self._sim_cb = QCheckBox(tr("Simulation"))
        layout.addWidget(self._sim_cb)

        self._connect_btn = QPushButton(tr("Connect"))
        self._connect_btn.clicked.connect(self._toggle_connect)
        layout.addWidget(self._connect_btn)

        layout.addStretch()
        return box

    def _build_plot_frame(self) -> QGroupBox:
        box = QGroupBox(tr("Temperature Monitor"))
        layout = QVBoxLayout(box)

        self._disp_ab_label = QLabel(tr("Ch A:  ---  K    Ch B:  ---  K"))
        self._disp_ab_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._disp_ab_label.setStyleSheet("font-size: 22px; color: black;")
        layout.addWidget(self._disp_ab_label)

        self._disp_sp_heater_label = QLabel(tr("Setpoint:  ---  K    Heater:  ---"))
        self._disp_sp_heater_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._disp_sp_heater_label.setStyleSheet("font-size: 10pt; color: #555;")
        layout.addWidget(self._disp_sp_heater_label)

        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setTitle(tr("Temperature vs Time"), size="13pt")
        self._plot_widget.setLabel("bottom", tr("Elapsed Time"), units="s", **{"font-size": "12pt"})
        self._plot_widget.setLabel("left", tr("Temperature (K)"), **{"font-size": "12pt"})
        self._plot_widget.getPlotItem().getAxis("left").enableAutoSIPrefix(False)
        self._plot_widget.getPlotItem().getAxis("bottom").enableAutoSIPrefix(False)
        self._plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self._plot_widget.plotItem.vb.setMouseMode(pg.ViewBox.RectMode)
        self._plot_widget.plotItem.vb.setLimits(xMin=0)
        # The grid is drawn by AxisItem (a ViewBox sibling, z=0), while the
        # ViewBox itself defaults to z=-100 — so anything inside it (curves,
        # legend) paints *under* the grid unless raised above that sibling.
        self._plot_widget.plotItem.vb.setZValue(10)
        legend = self._plot_widget.addLegend(
            labelTextSize="10pt",
            brush=pg.mkBrush(255, 255, 255, 255),
            pen=pg.mkPen("k", width=1),
        )
        legend.setZValue(100)

        self._line_a  = self._plot_widget.plot(pen=pg.mkPen("#1a6fc4", width=2), name=tr("Ch A"))
        self._line_b  = self._plot_widget.plot(pen=pg.mkPen("#2e8b57", width=2), name=tr("Ch B"))
        self._line_sp = self._plot_widget.plot(
            pen=pg.mkPen("#b0413e", width=2, style=Qt.PenStyle.DashLine),
            name=tr("Setpoint (ramp)"),
        )
        layout.addWidget(self._plot_widget)

        plot_btn_row = QHBoxLayout()
        self._clear_graph_btn = QPushButton(tr("Clear Graph"))
        self._clear_graph_btn.clicked.connect(self._clear_graph)
        plot_btn_row.addStretch()
        plot_btn_row.addWidget(QLabel(tr("Window:")))
        self._window_spin = _no_wheel(QSpinBox())
        self._window_spin.setRange(10, 7200)
        self._window_spin.setValue(PLOT_WINDOW_SECONDS)
        self._window_spin.setMaximumWidth(135)
        self._window_spin.setToolTip(tr("Display window: show only the latest N seconds"))
        plot_btn_row.addWidget(self._window_spin)
        plot_btn_row.addWidget(QLabel(tr("sec")))
        plot_btn_row.addSpacing(16)
        plot_btn_row.addWidget(self._clear_graph_btn)
        layout.addLayout(plot_btn_row)
        return box

    def _build_control_panel(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_setpoint_box())
        layout.addWidget(self._build_ramp_box())
        layout.addWidget(self._build_heater_box())
        layout.addWidget(self._build_alloff_widget())
        return widget

    def _build_setpoint_box(self) -> QGroupBox:
        box = QGroupBox(tr("Setpoint (K)"))
        g = QGridLayout(box)

        g.addWidget(QLabel(tr("Current:")), 0, 0)
        self._cur_sp_label = QLabel(tr("---"))
        self._cur_sp_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        g.addWidget(self._cur_sp_label, 0, 1)

        g.addWidget(QLabel(tr("New:")), 1, 0)
        self._new_sp_edit = QLineEdit()
        g.addWidget(self._new_sp_edit, 1, 1)

        btn = QPushButton(tr("Apply"))
        btn.clicked.connect(self._apply_setpoint)
        g.addWidget(btn, 2, 0, 1, 2)
        return box

    def _build_ramp_box(self) -> QGroupBox:
        box = QGroupBox(tr("Ramp Rate (K/min)"))
        g = QGridLayout(box)

        g.addWidget(QLabel(tr("Current:")), 0, 0)
        self._cur_ramp_label = QLabel(tr("---"))
        self._cur_ramp_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        g.addWidget(self._cur_ramp_label, 0, 1)

        self._ramp_enable_cb = QCheckBox(tr("Enable Ramp"))
        g.addWidget(self._ramp_enable_cb, 1, 0, 1, 2)

        g.addWidget(QLabel(tr("Rate:")), 2, 0)
        self._new_ramp_edit = QLineEdit("1.0")
        g.addWidget(self._new_ramp_edit, 2, 1)

        btn = QPushButton(tr("Apply"))
        btn.clicked.connect(self._apply_ramp)
        g.addWidget(btn, 3, 0, 1, 2)
        return box

    def _build_heater_box(self) -> QGroupBox:
        box = QGroupBox(tr("Heater Output"))
        g = QGridLayout(box)

        g.addWidget(QLabel(tr("Current:")), 0, 0)
        self._cur_heater_label = QLabel(tr("---"))
        self._cur_heater_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        g.addWidget(self._cur_heater_label, 0, 1)

        self._heater_bg = QButtonGroup(box)
        for i, name in enumerate(LakeShore335Backend.HEATER_RANGES):
            rb = QRadioButton(name)
            if i == 0:
                rb.setChecked(True)
            g.addWidget(rb, i + 1, 0, 1, 2)
            self._heater_bg.addButton(rb, i)

        btn = QPushButton(tr("Apply"))
        btn.clicked.connect(self._apply_heater_range)
        g.addWidget(btn, len(LakeShore335Backend.HEATER_RANGES) + 1, 0, 1, 2)
        return box

    def _build_alloff_widget(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn = QPushButton(tr("ALL\nOFF"))
        btn.setMinimumSize(80, 80)
        btn.setStyleSheet("""
            QPushButton {
                background-color: #cc0000;
                color: white;
                font-size: 15px;
                font-weight: bold;
                border-radius: 8px;
            }
            QPushButton:hover { background-color: #990000; }
            QPushButton:pressed { background-color: #660000; }
        """)
        btn.clicked.connect(self._all_off)
        layout.addWidget(btn)
        return w

    def _build_logging_bar(self) -> QGroupBox:
        box = QGroupBox(tr("Data Logging"))
        layout = QHBoxLayout(box)

        layout.addWidget(QLabel(tr("Log directory:")))
        self._log_dir_edit = QLineEdit()
        self._log_dir_edit.setMinimumWidth(220)
        layout.addWidget(self._log_dir_edit)

        browse_btn = QPushButton(tr("Browse…"))
        browse_btn.clicked.connect(self._browse_log_dir)
        layout.addWidget(browse_btn)

        self._log_start_btn = QPushButton(tr("Start Logging"))
        self._log_start_btn.clicked.connect(self._start_logging)
        layout.addWidget(self._log_start_btn)

        self._log_stop_btn = QPushButton(tr("Stop Logging"))
        self._log_stop_btn.clicked.connect(self._stop_logging)
        self._log_stop_btn.setEnabled(False)
        layout.addWidget(self._log_stop_btn)

        self._log_status_label = QLabel(tr("Idle"))
        self._log_status_label.setStyleSheet("color: gray;")
        layout.addWidget(self._log_status_label)

        layout.addStretch()
        return box

    # ================================================================
    # Backend signal wiring
    # ================================================================

    def _connect_backend_signals(self) -> None:
        self._backend.data_updated.connect(self._on_data_updated)
        self._backend.error_occurred.connect(self._on_error)

    # ================================================================
    # Connection handlers (standalone mode only)
    # ================================================================

    def _toggle_connect(self) -> None:
        if self._backend is not None and self._backend.is_connected:
            self._backend.disconnect()
            self._backend = None
            self._connect_btn.setText(tr("Connect"))
            self._set_status(tr("Not connected"), "gray")
            return

        simulate = self._sim_cb.isChecked()
        gpib_address = self._gpib_edit.text().strip() or DEFAULT_GPIB_ADDRESS

        try:
            backend = LakeShore335Backend(simulate=simulate)
            backend.connect(gpib_address=gpib_address)
        except Exception as exc:
            QMessageBox.critical(self, tr("Connection Error"), str(exc))
            return

        self._backend = backend
        self._connect_btn.setText(tr("Disconnect"))
        label = tr("Simulation") if simulate else tr("Hardware")
        self._set_status(tr("● Connected ({label})", label=label), "green")
        self._connect_backend_signals()
        self._sync_controls_from_device()

    # ================================================================
    # Instrument control handlers
    # ================================================================

    def _apply_setpoint(self) -> None:
        if not self._require_connected():
            return
        try:
            value = float(self._new_sp_edit.text())
        except ValueError:
            QMessageBox.critical(self, tr("Input Error"), tr("Setpoint must be a number."))
            return
        try:
            self._backend.set_setpoint(value)
            self._cur_sp_label.setText(tr("{value:.3f} K", value=value))
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), str(exc))

    def _apply_ramp(self) -> None:
        if not self._require_connected():
            return
        try:
            rate = float(self._new_ramp_edit.text())
        except ValueError:
            QMessageBox.critical(self, tr("Input Error"), tr("Ramp rate must be a number."))
            return
        enable = self._ramp_enable_cb.isChecked()
        try:
            self._backend.set_ramp_parameter(rate, enable)
            self._cur_ramp_label.setText(tr("{rate:.2f} K/min", rate=rate) if enable else tr("Off"))
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), str(exc))

    def _apply_heater_range(self) -> None:
        if not self._require_connected():
            return
        idx = self._heater_bg.checkedId()
        if idx < 0:
            return
        try:
            self._backend.set_heater_range(idx)
            name = LakeShore335Backend.HEATER_RANGES[idx]
            self._cur_heater_label.setText(name)
            self._update_sp_heater_label(heater_name=name)
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), str(exc))

    def _all_off(self) -> None:
        if not self._require_connected():
            return
        reply = QMessageBox.question(
            self, tr("Confirm"), tr("Turn all heaters OFF?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._backend.all_off()
            off_rb = self._heater_bg.button(0)
            if off_rb:
                off_rb.setChecked(True)
            self._cur_heater_label.setText("OFF")
            self._update_sp_heater_label(heater_name="OFF")
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    # ================================================================
    # Logging handlers
    # ================================================================

    def _browse_log_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, tr("Select Log Directory"))
        if directory:
            self._log_dir_edit.setText(directory)

    def _start_logging(self) -> None:
        if not self._require_connected():
            return
        log_dir = self._log_dir_edit.text().strip()
        if not log_dir:
            QMessageBox.critical(self, tr("Error"), tr("Please select a log directory first."))
            return
        if not os.path.isdir(log_dir):
            QMessageBox.critical(self, tr("Error"), tr("Directory not found:\n{dir}", dir=log_dir))
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"ls335_{timestamp}.csv"
        filepath  = os.path.join(log_dir, filename)

        try:
            self._backend.start_logging(filepath)
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), tr("Could not start logging:\n{error}", error=exc))
            return

        self._log_status_label.setText(tr("Logging: {filename}", filename=filename))
        self._log_status_label.setStyleSheet("color: green;")
        self._log_start_btn.setEnabled(False)
        self._log_stop_btn.setEnabled(True)

    def _stop_logging(self) -> None:
        if self._backend is not None:
            rows = self._backend.log_rows_written
            self._backend.stop_logging()
            self._log_status_label.setText(tr("Idle  (last: {rows} rows saved)", rows=rows))
        else:
            self._log_status_label.setText(tr("Idle"))
        self._log_status_label.setStyleSheet("color: gray;")
        self._log_start_btn.setEnabled(True)
        self._log_stop_btn.setEnabled(False)

    # ================================================================
    # Slots
    # ================================================================

    def _on_data_updated(self) -> None:
        self._update_plot_and_readings()

    def _on_error(self, msg: str) -> None:
        self._set_status(tr("✕ Error: {msg}", msg=msg[:60]), "red")

    def _on_timer(self) -> None:
        self._update_log_status()

    # ================================================================
    # Plot / reading refresh
    # ================================================================

    def _update_plot_and_readings(self) -> None:
        if self._backend is None or not self._backend.is_connected:
            return

        data = self._backend.get_data()
        if not data:
            return

        latest = data[-1]
        self._disp_ab_label.setText(
            tr("Ch A:  {a:.3f}  K    Ch B:  {b:.3f}  K", a=latest.temp_a_k, b=latest.temp_b_k)
        )
        self._update_sp_heater_label(
            setpoint_k=latest.eff_setpoint_k,
            heater_name=LakeShore335Backend.HEATER_RANGES[latest.heater_range_idx],
        )

        window = self._window_spin.value()

        cutoff  = latest.timestamp - window
        visible = [d for d in data if d.timestamp >= cutoff]
        if not visible:
            return

        t0    = visible[0].timestamp
        times = [d.timestamp - t0 for d in visible]
        self._line_a.setData(times,  [d.temp_a_k       for d in visible])
        self._line_b.setData(times,  [d.temp_b_k       for d in visible])
        self._line_sp.setData(times, [d.eff_setpoint_k for d in visible])

    def _clear_graph(self) -> None:
        if self._backend is not None:
            self._backend.clear_data()
        self._line_a.setData([], [])
        self._line_b.setData([], [])
        self._line_sp.setData([], [])
        self._plot_widget.plotItem.vb.setLimits(xMin=0, xMax=None)
        self._plot_widget.plotItem.enableAutoRange()

    def _update_log_status(self) -> None:
        if self._backend is None or not self._backend.is_logging:
            return
        rows    = self._backend.log_rows_written
        current = self._log_status_label.text()
        base    = current.split("  (")[0]
        self._log_status_label.setText(tr("{base}  ({rows} rows)", base=base, rows=rows))

    # ================================================================
    # Helpers
    # ================================================================

    def _update_sp_heater_label(self, setpoint_k: float | None = None, heater_name: str | None = None) -> None:
        if setpoint_k is not None:
            self._last_setpoint_k = setpoint_k
        if heater_name is not None:
            self._last_heater_name = heater_name
        sp_text = tr("{value:.3f} K", value=self._last_setpoint_k) if self._last_setpoint_k is not None else tr("---")
        heater_text = self._last_heater_name if self._last_heater_name is not None else tr("---")
        self._disp_sp_heater_label.setText(
            tr("Setpoint:  {sp}    Heater:  {heater}", sp=sp_text, heater=heater_text)
        )

    def _set_status(self, text: str, color: str) -> None:
        self._status_label.setText(text)
        self._status_label.setStyleSheet(f"font-weight: bold; color: {color};")

    def _require_connected(self) -> bool:
        if self._backend is None or not self._backend.is_connected:
            QMessageBox.warning(self, tr("Not Connected"),
                                tr("Please connect to the instrument first."))
            return False
        return True

    def _sync_controls_from_device(self) -> None:
        if self._backend is None:
            return
        try:
            sp = self._backend.get_setpoint()
            self._cur_sp_label.setText(tr("{value:.3f} K", value=sp))
            self._new_sp_edit.setText(f"{sp:.3f}")
        except Exception:
            pass
        try:
            enabled, rate = self._backend.get_ramp_parameter()
            self._ramp_enable_cb.setChecked(enabled)
            self._new_ramp_edit.setText(f"{rate:.2f}")
            self._cur_ramp_label.setText(tr("{rate:.2f} K/min", rate=rate) if enabled else tr("Off"))
        except Exception:
            pass
        try:
            hr   = self._backend.get_heater_range()
            name = LakeShore335Backend.HEATER_RANGES[hr]
            rb   = self._heater_bg.button(hr)
            if rb:
                rb.setChecked(True)
            self._cur_heater_label.setText(name)
            self._update_sp_heater_label(heater_name=name)
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self._owns_backend and self._backend is not None:
            self._backend.disconnect()
        event.accept()


# ====================================================================
# Standalone entry point
# ====================================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = LakeShore335Window()
    win.show()
    sys.exit(app.exec())
