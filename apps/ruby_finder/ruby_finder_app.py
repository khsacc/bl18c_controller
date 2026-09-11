"""Ch4/Ch5 Ruby Finder using spectra acquired from FluoRaPressée."""
from __future__ import annotations

import os

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt

from PyQt6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QSpinBox, QVBoxLayout,
)

try:
    from apps.scan2d.free_2d_scan_app import Free2DScanWindow, _no_wheel
    from apps.scan2d.free_2d_scan_backend import um_per_pulse
    from apps.ruby_finder.ruby_finder_backend import (
        FluoraPresseeReader, FluoraPresseeReaderSim, build_spectrum_cube,
    )
    from settings import online_spectrometer_prefs
    from settings.i18n import tr
except ImportError:
    import sys
    _root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    sys.path.insert(0, _root)
    from apps.scan2d.free_2d_scan_app import Free2DScanWindow, _no_wheel
    from apps.scan2d.free_2d_scan_backend import um_per_pulse
    from apps.ruby_finder.ruby_finder_backend import (
        FluoraPresseeReader, FluoraPresseeReaderSim, build_spectrum_cube,
    )
    from settings import online_spectrometer_prefs
    from settings.i18n import tr


class RubyFinderWindow(Free2DScanWindow):
    """Map integrated ruby fluorescence while scanning Ch4 and Ch5."""

    def __init__(self, controller=None, debug: bool = False, parent=None) -> None:
        self._frp_reader: FluoraPresseeReader | None = None
        self._spectrum_x: np.ndarray | None = None
        self._spectrum_cube: np.ndarray | None = None
        super().__init__(
            controller=controller,
            gpib_reader=None,
            debug=debug,
            parent=parent,
            default_ch_x=4,
            default_ch_y=5,
            allow_channel_change=False,
            log_key="ruby_finder",
            window_title="Ruby Finder",
            signal_label="Ruby fluorescence intensity",
            map_data_key="fluorescence_intensity_map",
            map_title="Ruby Fluorescence Map",
        )
        self.resize(1300, 900)
        # One FRP acquisition already uses the accumulation configured in FRP.
        self._accum_spin.setValue(1)

    def _build_plot_area(self):
        plot_area = super()._build_plot_area()
        self._spectrum_plot = self._glw.addPlot(
            row=2, col=0, colspan=3, title=tr("Selected-grid spectrum")
        )
        self._spectrum_plot.setLabel("bottom", tr("Spectral coordinate"))
        self._spectrum_plot.setLabel("left", tr("Intensity"))
        self._spectrum_plot.vb.setMouseEnabled(x=False, y=False)
        self._spectrum_plot.setMenuEnabled(False)
        self._spectrum_plot.hideButtons()
        self._spectrum_curve = self._spectrum_plot.plot(
            pen=pg.mkPen((30, 120, 220), width=2)
        )
        self._selected_cell_outline = pg.PlotCurveItem(
            pen=pg.mkPen((0, 255, 255), width=3)
        )
        self._selected_cell_outline.setZValue(11)
        self._plot_2d.addItem(self._selected_cell_outline)
        self._glw.ci.layout.setRowStretchFactor(2, 1)
        return plot_area

    def _build_param_panel(self):
        panel = super()._build_param_panel()
        acq_group = QGroupBox(tr("FluoRaPressée acquisition override"))
        acq_layout = QVBoxLayout(acq_group)

        exposure_row = QHBoxLayout()
        self._exposure_override_chk = QCheckBox(tr("Exposure time (s):"))
        exposure_row.addWidget(self._exposure_override_chk)
        self._exposure_spin = _no_wheel(QDoubleSpinBox())
        self._exposure_spin.setRange(0.001, 600.0)
        self._exposure_spin.setDecimals(3)
        self._exposure_spin.setSingleStep(0.1)
        self._exposure_spin.setValue(1.0)
        self._exposure_spin.setEnabled(False)
        self._exposure_override_chk.toggled.connect(self._exposure_spin.setEnabled)
        exposure_row.addWidget(self._exposure_spin)
        acq_layout.addLayout(exposure_row)

        accum_row = QHBoxLayout()
        self._accum_override_chk = QCheckBox(tr("Accumulations:"))
        accum_row.addWidget(self._accum_override_chk)
        self._frp_accum_spin = _no_wheel(QSpinBox())
        self._frp_accum_spin.setRange(1, 1000)
        self._frp_accum_spin.setValue(1)
        self._frp_accum_spin.setEnabled(False)
        self._accum_override_chk.toggled.connect(self._frp_accum_spin.setEnabled)
        accum_row.addWidget(self._frp_accum_spin)
        acq_layout.addLayout(accum_row)

        gain_note = QLabel(tr(
            "EM gain is not exposed by the FluoRaPressée API; set it in the "
            "FluoRaPressée application itself."
        ))
        gain_note.setWordWrap(True)
        acq_layout.addWidget(gain_note)

        panel.layout().insertWidget(1, acq_group)
        return panel

    def _on_start(self) -> None:
        if self._debug:
            try:
                center_x = int(self._controller.get_ch_pos(4))
                center_y = int(self._controller.get_ch_pos(5))
            except Exception as exc:
                QMessageBox.warning(
                    self, tr("Error"),
                    tr("Cannot read current position:\n{error}", error=exc),
                )
                return
            self._gpib_reader = FluoraPresseeReaderSim(
                um_per_pulse_x=um_per_pulse(4),
                um_per_pulse_y=um_per_pulse(5),
                center_x_pulse=center_x,
                center_y_pulse=center_y,
            )
            self._frp_reader = None
        else:
            try:
                self._frp_reader = FluoraPresseeReader(
                    base_url=online_spectrometer_prefs.get_base_url(),
                    api_key=online_spectrometer_prefs.get_api_key(),
                    retain_spectra=True,
                )
            except ValueError as exc:
                QMessageBox.warning(self, tr("FluoRaPressée API Error"), str(exc))
                return
            if self._exposure_override_chk.isChecked():
                self._frp_reader.exposure_time_s = self._exposure_spin.value()
            if self._accum_override_chk.isChecked():
                self._frp_reader.accumulations = self._frp_accum_spin.value()
            self._gpib_reader = self._frp_reader
        self._spectrum_x = None
        self._spectrum_cube = None
        self._spectrum_curve.setData([], [])
        self._selected_cell_outline.setData([], [])
        super()._on_start()

    def _finalise_spectra(self) -> None:
        reader = self._gpib_reader
        if reader is None or self._transmitted_map is None:
            return
        spectra = getattr(reader, "retained_spectra", [])
        spectrum_x = getattr(reader, "spectrum_x", None)
        if spectrum_x is None:
            return
        try:
            cube = build_spectrum_cube(
                spectra,
                n_y=self._n_y,
                n_x=self._n_x,
                reads_per_point=self._scan_accumulation,
                measured_mask=~np.isnan(self._transmitted_map),
            )
        except (MemoryError, ValueError) as exc:
            QMessageBox.warning(self, tr("Spectrum Error"), str(exc))
            return
        self._spectrum_x = np.asarray(spectrum_x, dtype=float)
        self._spectrum_cube = cube
        spectra.clear()
        self._update_spectrum_axis_label(reader)

    def _on_scan_completed(self) -> None:
        self._finalise_spectra()
        super()._on_scan_completed()

    def _on_scan_aborted(self) -> None:
        self._finalise_spectra()
        super()._on_scan_aborted()

    def _update_spectrum_axis_label(self, reader) -> None:
        acquisition = getattr(reader, "last_acquisition", None) or {}
        axis = acquisition.get("x_axis")
        label = tr("Spectral coordinate")
        if isinstance(axis, dict):
            label = str(axis.get("label") or axis.get("name") or label)
            unit = axis.get("unit") or axis.get("units")
            if unit:
                label += f" ({unit})"
        self._spectrum_plot.setLabel("bottom", label)

    def _on_scene_clicked(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            super()._on_scene_clicked(event)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        if self._spectrum_cube is None or self._spectrum_x is None:
            return
        pos = event.scenePos()
        if not self._plot_2d.vb.sceneBoundingRect().contains(pos):
            return
        view_pos = self._plot_2d.vb.mapSceneToView(pos)
        x_rel, y_rel = float(view_pos.x()), float(view_pos.y())
        col = int(np.argmin(np.abs(self._x_pulses_rel - x_rel)))
        row = int(np.argmin(np.abs(self._y_pulses_rel - y_rel)))

        xp, yp = self._x_pulses_rel, self._y_pulses_rel
        px_x = (xp[-1] - xp[0]) / max(self._n_x - 1, 1)
        px_y = (yp[-1] - yp[0]) / max(self._n_y - 1, 1)
        if not (
            xp[0] - px_x / 2 <= x_rel <= xp[-1] + px_x / 2
            and yp[0] - px_y / 2 <= y_rel <= yp[-1] + px_y / 2
        ):
            return
        spectrum = self._spectrum_cube[row, col]
        if np.all(np.isnan(spectrum)):
            return
        event.accept()
        self._spectrum_curve.setData(self._spectrum_x, spectrum)
        self._spectrum_plot.setTitle(tr(
            "Spectrum at Ch4={x_pulse}, Ch5={y_pulse} pulses",
            x_pulse=self._center_x_pulse + int(xp[col]),
            y_pulse=self._center_y_pulse + int(yp[row]),
        ))
        xc, yc = float(xp[col]), float(yp[row])
        self._selected_cell_outline.setData(
            [xc - px_x / 2, xc + px_x / 2, xc + px_x / 2,
             xc - px_x / 2, xc - px_x / 2],
            [yc - px_y / 2, yc - px_y / 2, yc + px_y / 2,
             yc + px_y / 2, yc - px_y / 2],
        )

    def _additional_saved_arrays(self) -> dict[str, np.ndarray]:
        if self._spectrum_x is None or self._spectrum_cube is None:
            return {}
        return {
            "spectrum_x": self._spectrum_x,
            "spectrum_y_map": self._spectrum_cube,
        }

    def _measurement_metadata(self) -> dict | None:
        if self._frp_reader is not None:
            return self._frp_reader.public_metadata()
        if self._debug:
            return {
                "source": "simulation",
                "intensity_reduction": "simulated 2-D Gaussian",
            }
        return None
