# XRD Scan — implementation details

Developer-facing detail for `apps/xrd_scan/`. Performs a Ch4/Ch5 grid scan
using Rad-icon 2022 XRD images; integrates each frame in-memory via pyFAI and
maps ROI intensities. **For pyFAI / poni-file patterns used here, see the
project skill `/pyfai-integration`.**

## Module layout

| File | Role |
|------|------|
| [xrd_scan_backend.py](xrd_scan_backend.py) | `RoiSpec` dataclass, `parse_poni` / `build_ai` helpers, `XrdScanWorker` (QThread) |
| [roi_dialog.py](roi_dialog.py) | `RoiDialog` – non-modal window: pyqtgraph 1D spectrum + `pg.LinearRegionItem` per ROI + QTableWidget; bidirectional sync; "Take Test Shot" button. The 2D test-shot frame (corrected exactly as integrated) is shown beside the spectrum in a `QSplitter`, using the shared `ImageLabel` / `levels_to_qimage` from `apps/Rad_icon_2022/image_view.py` with Min/Max slider+spin and Auto. It is auto-levelled on the first shot only, so later shots can be compared at the same levels. `set_spectrum()` (scan data) does not change the image; the info line says it is still the last test shot |
| [xrd_scan_app.py](xrd_scan_app.py) | `XrdScanWindow` — main window; mirrors `DacScanWindow` layout; poni file selector; "Set ROI…" button opens `RoiDialog`; 表示ROI combobox switches displayed map |

## Key design decisions

- **Shared detector reduction** – both the scan worker and ROI test shot pass
  the corrected frame to `apps/Rad_icon_2022/instant1d.py::reduce_frame`.
  This uses full pixel splitting, solid-angle correction and a 0.95
  polarisation factor, just like Rad-icon Instant 1D. TIFF saving is not
  required for integration. Their integrator comes from
  `PoniState.integration_ai`, so the optional downstream diamond thickness
  changes only the distance used for reduction; the saved `.poni` is unchanged.
- **Shared image processing** – raw scan and test-shot frames pass through
  `apps/Rad_icon_2022/image_utils.py::process_frame` (flip, optional dark
  subtraction, optional defect correction). The current Rad-icon window's
  flip and defect settings are captured on the GUI thread before a scan; when
  that window is closed, its saved preferences are used. XRD Scan's own dark
  selection supplies the dark frame. Saved scan TIFFs contain the corrected
  frame used for integration.
- **Angular resolution control** – the UI takes an approximate 2θ step in degrees per bin (default 0.005, matching Rad-icon Instant 1D). The shared `bins_for_width` calculation uses the calibrated detector's full-image 2θ centre span; a ROI test shot uses the captured image shape. XRD Scan rejects invalid geometry and bin counts above 10,000. Scan JSON records both the requested step (`angular_resolution_deg`) and the resulting `n_bins`.
- **TIFF save is optional** — checkbox in the UI; TIFFs go to `apps/xrd_scan/__localdata/<timestamp>/`.
- **Multiple ROIs** — stored as `intensity_maps[n_roi, n_ch5, n_ch4]`; all ROIs are computed in a single scan pass.
- **No ROI → full 2θ range** — when no ROI is defined (the initial state, or after every ROI is deleted), `_effective_roi_list()` supplies a single implicit "Full 2θ range" ROI (`sum` over the whole radial axis). The map, the combobox entry, the fit and the saved `roi_list` in the JSON metadata all use it. The ROI dialog does not show it, so defining a real ROI replaces it.
- **ROI change → immediate refit** — `RoiDialog.roi_list_changed` signal is connected to `_on_roi_list_changed`, which calls `_run_fit()` if scan data exists and scan is not running.
- **Combobox switch → immediate refit** — `_roi_display_combo.currentIndexChanged` also calls `_run_fit()`.
- **Backlash** — identical pattern to `DacScanWorker`: Ch4 always approached from `target − BACKLASH_PULSES_CH4` before each row.

## Why this app doesn't use `apps/scan2d`

`xrd_scan` operates on the same fixed Ch4 (X) / Ch5 (Y) sample stage axes as
DAC Scan (Normal), but has **not** been migrated to the generic
`Free2DScanWorker` engine in `apps/scan2d/` — it remains an independent
implementation. `CH_X`/`CH_Y`/`UM_PER_PULSE_CH4`/`UM_PER_PULSE_CH5`/
`BACKLASH_PULSES_CH4` are inlined as local constants in
`xrd_scan_backend.py` (channel numbers as literals, µm/pulse scales read
straight from `utils.stage.control_stage.PULSE_SCALE`) rather than imported from
`apps.dac_scan`, which no longer has a backend module at all (see
`apps/dac_scan/IMPLEMENTATION_DETAILS.md` for that history).
