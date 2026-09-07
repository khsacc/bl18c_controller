"""ROI definition dialog for XRD DAC Scan.

Non-modal dialog (Qt.WindowType.Window) showing a live 1D spectrum with
draggable pg.LinearRegionItem overlays.  Any number of ROIs can be added;
each gets a distinct colour.  ROI changes are broadcast via roi_list_changed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

try:
    from apps.xrd_scan.xrd_scan_backend import ROI_COLORS, RoiSpec
    from settings.i18n import tr
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from apps.xrd_scan.xrd_scan_backend import ROI_COLORS, RoiSpec
    from settings.i18n import tr

try:
    from utils.pdindexer import PdiProfile, PdiService, Transport, Trigger
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from utils.pdindexer import PdiProfile, PdiService, Transport, Trigger


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


class RoiDialog(QDialog):
    """Non-modal dialog for defining one or more 2θ ROIs.

    params_getter: Callable[[], tuple[int, int, AI | None]]
        Called when "Take Test Shot" is pressed.
        Returns (n_bins, exposure_ms, AzimuthalIntegrator_or_None).
    open_settings_callback: Callable[[], None] | None
        Called after the "No poni file" warning is dismissed, to open the
        Settings window on the Detector Calibration page.
    """

    roi_list_changed = pyqtSignal(list)   # list[RoiSpec]

    def __init__(
        self,
        backend,                                           # RadiconBackend
        params_getter: Callable[[], tuple],                # () → (n_bins, exp_ms, ai)
        open_settings_callback: Callable[[], None] | None = None,
        pdi_service: "PdiService | None" = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(tr("XRD ROI Settings"))
        self.resize(760, 520)

        # PdiService (bridge to PDIndexer, see utils/pdindexer/) — shared
        # when injected, owned by this dialog otherwise. Mirrors the
        # controller= injection pattern used elsewhere in this repo.
        if pdi_service is not None:
            self._pdi_service = pdi_service
            self._owns_pdi_service = False
        else:
            self._pdi_service = PdiService(watch_folder=Path(__file__).parent / "__localdata" / "pdindexer_watch")
            self._owns_pdi_service = True
        self._pdi_service.finished.connect(self._on_pdi_finished)
        self._last_wavelength_m: float | None = None

        self._backend       = backend
        self._params_getter = params_getter
        self._open_settings_callback = open_settings_callback

        self._rois:    list[RoiSpec]              = []
        self._regions: list[pg.LinearRegionItem]  = []
        self._color_idx = 0
        self._updating  = False        # guard against recursive signal loops

        self._last_radial:    np.ndarray | None = None
        self._last_intensity: np.ndarray | None = None

        self._setup_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # top bar: test shot button
        top = QHBoxLayout()
        self._shot_btn = QPushButton(tr("Take Test Shot"))
        self._shot_btn.clicked.connect(self._on_test_shot)
        top.addWidget(self._shot_btn)
        top.addStretch()
        root.addLayout(top)

        # PDIndexer row — sends the last test shot's spectrum on demand.
        # See docs/PLAN_PDINDEXER_BRIDGE.md / utils/pdindexer/.
        pdi_row = QHBoxLayout()
        self._pdi_transport_combo = _no_wheel(QComboBox())
        self._pdi_transport_combo.currentIndexChanged.connect(self._on_pdi_transport_changed)
        pdi_row.addWidget(QLabel(tr("PDIndexer via:")))
        pdi_row.addWidget(self._pdi_transport_combo)
        self._pdi_send_btn = QPushButton(tr("Send to PDIndexer"))
        self._pdi_send_btn.setToolTip(tr("Send the last test shot's spectrum to PDIndexer."))
        self._pdi_send_btn.clicked.connect(self._on_pdi_send_clicked)
        pdi_row.addWidget(self._pdi_send_btn)
        self._pdi_status_label = QLabel()
        self._pdi_status_label.setWordWrap(True)
        pdi_row.addWidget(self._pdi_status_label, 1)
        root.addLayout(pdi_row)
        self._refresh_pdi_controls()

        # 1D spectrum plot
        self._plot = pg.PlotWidget(background="w")
        self._plot.setLabel("bottom", tr("2θ (deg)"))
        self._plot.setLabel("left", tr("Intensity (a.u.)"))
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.getAxis("bottom").setTextPen("k")
        self._plot.getAxis("left").setTextPen("k")
        self._plot.getAxis("bottom").setPen("k")
        self._plot.getAxis("left").setPen("k")
        self._plot.setMinimumHeight(220)
        self._spectrum_curve = self._plot.plot(
            pen=pg.mkPen((40, 80, 160), width=1)
        )
        root.addWidget(self._plot)

        # ROI table
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            [tr("Label"), tr("2θ min (deg)"), tr("2θ max (deg)"), tr("Mode"), ""]
        )
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(4, 36)
        self._table.setMaximumHeight(160)
        self._table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._table)

        # add-ROI button
        add_row = QHBoxLayout()
        add_btn = QPushButton(tr("+ Add ROI"))
        add_btn.clicked.connect(self._on_add_roi)
        add_row.addWidget(add_btn)
        add_row.addStretch()
        root.addLayout(add_row)

        # preview intensities
        self._preview_lbl = QLabel("")
        self._preview_lbl.setWordWrap(True)
        self._preview_lbl.setStyleSheet("font-size: 11px; color: #aaa;")
        root.addWidget(self._preview_lbl)

        close_btn = QPushButton(tr("Close"))
        close_btn.clicked.connect(self.hide)
        root.addWidget(close_btn)

    # ── Test shot ──────────────────────────────────────────────────────────────

    def _on_test_shot(self) -> None:
        try:
            params = self._params_getter()
            n_bins, exposure_ms, ai = params[0], params[1], params[2]
            dark = params[3] if len(params) > 3 else None
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), tr("Cannot get scan parameters:\n{error}", error=exc))
            return

        if ai is None:
            QMessageBox.warning(self, tr("No poni file"),
                                tr("Please load a poni file in the main window first."))
            if self._open_settings_callback is not None:
                self._open_settings_callback()
            return

        self._shot_btn.setEnabled(False)
        self._shot_btn.setText(tr("Acquiring…"))
        QApplication.processEvents()

        try:
            img = self._backend.snap_triggered(exposure_ms)
            img_f = img.astype(np.float32)
            if dark is not None:
                img_f = np.clip(img_f - dark.astype(np.float32), 0.0, None)
            result = ai.integrate1d(
                img_f,
                npt=n_bins,
                unit="2th_deg",
                method=("no", "histogram", "cython"),
                correctSolidAngle=True,
                polarization_factor=0.95,
            )
            self._last_radial    = result.radial
            self._last_intensity = result.intensity
            self._last_wavelength_m = ai.wavelength
            self._spectrum_curve.setData(result.radial, result.intensity)

            # Auto-set plot range on first shot
            self._plot.setXRange(float(result.radial[0]), float(result.radial[-1]),
                                 padding=0.02)

            self._update_preview()
            self._refresh_pdi_controls()
        except Exception as exc:
            QMessageBox.critical(self, tr("Shot failed"), str(exc))
        finally:
            self._shot_btn.setEnabled(True)
            self._shot_btn.setText(tr("Take Test Shot"))

    def _update_preview(self) -> None:
        if self._last_radial is None:
            return
        parts = []
        for i, roi in enumerate(self._rois):
            val = roi.compute(self._last_radial, self._last_intensity)
            parts.append(tr("ROI#{n} ({label}): {val:.1f}", n=i + 1, label=roi.label, val=val))
        self._preview_lbl.setText("  |  ".join(parts) if parts else "")

    # ── PDIndexer bridge ─────────────────────────────────────────────────────

    def _refresh_pdi_controls(self) -> None:
        clip_ok = self._pdi_service.clipboard_available()
        folder_ok = self._pdi_service.watch_folder_configured()

        # Only (re)populate items — never assign a "current transport" onto
        # the shared service here (it has none; transport is passed
        # explicitly with each send() call in _on_pdi_send_clicked below).
        # Writing it here would let this dialog silently override
        # whatever another window sharing the same PdiService had picked —
        # see code review 2026-09-06.
        current = self._pdi_transport_combo.currentData()
        self._pdi_transport_combo.blockSignals(True)
        self._pdi_transport_combo.clear()
        if clip_ok:
            self._pdi_transport_combo.addItem(tr("Clipboard"), Transport.CLIPBOARD)
        if folder_ok:
            self._pdi_transport_combo.addItem(
                tr(".pdi folder ({path})", path=str(self._pdi_service.watch_folder())),
                Transport.WATCH_FOLDER,
            )
        if current is not None:
            idx = self._pdi_transport_combo.findData(current)
            if idx >= 0:
                self._pdi_transport_combo.setCurrentIndex(idx)
        self._pdi_transport_combo.blockSignals(False)

        any_ok = clip_ok or folder_ok
        self._pdi_transport_combo.setEnabled(any_ok)
        self._pdi_send_btn.setEnabled(
            any_ok and self._last_radial is not None and self._last_wavelength_m is not None
        )
        if not any_ok:
            self._pdi_status_label.setText(
                tr("PDIndexer bridge unavailable here (no clipboard helper on this "
                   "platform, and no writable .pdi folder configured).")
            )
            self._pdi_status_label.setStyleSheet("color: gray;")

    def _on_pdi_transport_changed(self, _index: int) -> None:
        pass  # nothing to do — transport is read from the combo box at send time

    def _on_pdi_send_clicked(self) -> None:
        if self._last_radial is None or self._last_wavelength_m is None:
            return
        transport = self._pdi_transport_combo.currentData()
        if transport is None:
            return
        try:
            profile = PdiProfile(
                name="XRD_ROI_test_shot",
                x=self._last_radial,
                y=self._last_intensity,
                wavelength_nm=self._last_wavelength_m * 1e9,
            )
        except ValueError as exc:
            self._pdi_status_label.setText(tr("✕ Send failed: {message}", message=str(exc)))
            self._pdi_status_label.setStyleSheet("color: #a00;")
            return
        self._pdi_service.send([profile], trigger=Trigger.MANUAL, transport=transport)

    def _on_pdi_finished(self, ok: bool, message: str, transport: Transport) -> None:
        if not ok:
            self._pdi_status_label.setText(tr("✕ Send failed: {message}", message=message))
            self._pdi_status_label.setStyleSheet("color: #a00;")
            return
        if transport is not Transport.CLIPBOARD:
            self._pdi_status_label.setText(tr("● Wrote .pdi file"))
            self._pdi_status_label.setStyleSheet("color: green;")
            return
        # pdindexer_running() is a separate, asynchronous subprocess check —
        # never block the GUI thread waiting on it here (see
        # code review 2026-09-06). Show an interim message immediately.
        self._pdi_status_label.setText(tr("● Wrote to clipboard"))
        self._pdi_status_label.setStyleSheet("color: green;")
        self._pdi_service.pdindexer_running_async(self._on_pdindexer_running_probe)

    def _on_pdindexer_running_probe(self, running: bool) -> None:
        text = (tr("● Sent to PDIndexer") if running
                else tr("● Wrote to clipboard (PDIndexer not detected running)"))
        self._pdi_status_label.setText(text)
        self._pdi_status_label.setStyleSheet("color: green;")

    # ── Add / delete ROI ───────────────────────────────────────────────────────

    def _on_add_roi(self) -> None:
        # default position: visible centre ± 5% of range
        try:
            x_lo, x_hi = self._plot.getViewBox().viewRange()[0]
        except Exception:
            x_lo, x_hi = 10.0, 40.0
        center = (x_lo + x_hi) / 2.0
        half   = (x_hi - x_lo) * 0.05
        tth_min = max(x_lo, center - half)
        tth_max = min(x_hi, center + half)

        idx   = len(self._rois)
        color = ROI_COLORS[self._color_idx % len(ROI_COLORS)]
        self._color_idx += 1

        roi = RoiSpec(
            label=f"ROI{idx + 1}",
            tth_min=tth_min,
            tth_max=tth_max,
            mode="sum",
            color=color,
        )
        self._rois.append(roi)

        r, g, b = color
        region = pg.LinearRegionItem(
            values=[tth_min, tth_max],
            brush=pg.mkBrush(r, g, b, 55),
            pen=pg.mkPen(r, g, b, width=2),
            movable=True,
        )
        region.sigRegionChanged.connect(
            lambda reg=region, i=idx: self._on_region_changed(reg, i)
        )
        self._plot.addItem(region)
        self._regions.append(region)

        self._rebuild_table()
        self._emit_changed()

    def _on_delete_clicked(self) -> None:
        """Find which row's delete button was clicked and remove that ROI."""
        sender = self.sender()
        for row in range(self._table.rowCount()):
            if self._table.cellWidget(row, 4) is sender:
                self._do_delete(row)
                return

    def _do_delete(self, idx: int) -> None:
        if idx >= len(self._rois):
            return
        self._plot.removeItem(self._regions[idx])
        del self._rois[idx]
        del self._regions[idx]
        # Reconnect region signals with updated indices
        for i, region in enumerate(self._regions):
            try:
                region.sigRegionChanged.disconnect()
            except Exception:
                pass
            region.sigRegionChanged.connect(
                lambda reg=region, ii=i: self._on_region_changed(reg, ii)
            )
        self._rebuild_table()
        self._emit_changed()

    # ── LinearRegionItem ↔ table sync ─────────────────────────────────────────

    def _on_region_changed(self, region: pg.LinearRegionItem, idx: int) -> None:
        if self._updating or idx >= len(self._rois):
            return
        lo, hi = region.getRegion()
        self._rois[idx].tth_min = lo
        self._rois[idx].tth_max = hi

        self._updating = True
        try:
            if idx < self._table.rowCount():
                self._table.item(idx, 1).setText(f"{lo:.3f}")
                self._table.item(idx, 2).setText(f"{hi:.3f}")
        finally:
            self._updating = False

        self._update_preview()
        self._emit_changed()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        row = item.row()
        col = item.column()
        if row >= len(self._rois):
            return

        self._updating = True
        try:
            if col == 0:
                self._rois[row].label = item.text()
            elif col == 1:
                try:
                    val = float(item.text())
                    self._rois[row].tth_min = val
                    self._regions[row].setRegion([val, self._rois[row].tth_max])
                except ValueError:
                    pass
            elif col == 2:
                try:
                    val = float(item.text())
                    self._rois[row].tth_max = val
                    self._regions[row].setRegion([self._rois[row].tth_min, val])
                except ValueError:
                    pass
        finally:
            self._updating = False

        self._update_preview()
        self._emit_changed()

    # ── Table rebuild ──────────────────────────────────────────────────────────

    def _rebuild_table(self) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for roi in self._rois:
            self._append_table_row(roi)
        self._table.blockSignals(False)

    def _append_table_row(self, roi: RoiSpec) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        r, g, b = roi.color
        bg = QColor(r, g, b, 80)

        # Label
        lbl_item = QTableWidgetItem(roi.label)
        lbl_item.setBackground(bg)
        self._table.setItem(row, 0, lbl_item)

        # 2θ min
        min_item = QTableWidgetItem(f"{roi.tth_min:.3f}")
        self._table.setItem(row, 1, min_item)

        # 2θ max
        max_item = QTableWidgetItem(f"{roi.tth_max:.3f}")
        self._table.setItem(row, 2, max_item)

        # Mode combo
        mode_combo = QComboBox()
        mode_combo.addItems(["Sum", "Mean"])
        mode_combo.setCurrentText(roi.mode.capitalize())
        mode_combo.currentTextChanged.connect(
            lambda text, r=row: self._on_mode_changed(r, text)
        )
        self._table.setCellWidget(row, 3, mode_combo)

        # Delete button
        del_btn = QPushButton("×")
        del_btn.setFixedWidth(34)
        del_btn.setToolTip(tr("Delete this ROI"))
        del_btn.clicked.connect(self._on_delete_clicked)
        self._table.setCellWidget(row, 4, del_btn)

    def _on_mode_changed(self, row: int, text: str) -> None:
        # row index captured at table-build time is still valid because
        # _on_mode_changed only fires on user interaction (not during rebuild)
        if row < len(self._rois):
            self._rois[row].mode = text.lower()
            self._update_preview()
            self._emit_changed()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _emit_changed(self) -> None:
        self.roi_list_changed.emit(list(self._rois))

    def get_roi_list(self) -> list[RoiSpec]:
        return list(self._rois)

    def set_spectrum(self, radial: np.ndarray, intensity: np.ndarray) -> None:
        """Display an externally provided 1D spectrum (e.g., post-scan mean or per-point).

        Called by XrdScanWindow after scan completion or on map-click so the user
        can define ROIs on real measured data without taking a new test shot.
        """
        self._last_radial    = radial
        self._last_intensity = intensity
        # This spectrum arrives without its own wavelength — invalidate any
        # wavelength left over from an earlier test shot so "Send to
        # PDIndexer" can't pair it with data it doesn't actually belong to.
        self._last_wavelength_m = None
        self._spectrum_curve.setData(radial, intensity)
        self._plot.setXRange(float(radial[0]), float(radial[-1]), padding=0.02)
        self._update_preview()
        self._refresh_pdi_controls()

    def closeEvent(self, event) -> None:
        if self._owns_pdi_service:
            self._pdi_service.shutdown()
        super().closeEvent(event)
