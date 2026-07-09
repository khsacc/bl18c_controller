# XRD Scan — implementation details

Developer-facing detail for `apps/xrd_scan/`. Performs a Ch4/Ch5 grid scan
using Rad-icon 2022 XRD images; integrates each frame in-memory via pyFAI and
maps ROI intensities. **For pyFAI / poni-file patterns used here, see the
project skill `/pyfai-integration`.**

## Module layout

| File | Role |
|------|------|
| [xrd_scan_backend.py](xrd_scan_backend.py) | `RoiSpec` dataclass, `parse_poni` / `build_ai` helpers, `XrdScanWorker` (QThread) |
| [roi_dialog.py](roi_dialog.py) | `RoiDialog` — non-modal window: pyqtgraph 1D spectrum + `pg.LinearRegionItem` per ROI + QTableWidget; bidirectional sync; "Take Test Shot" button |
| [xrd_scan_app.py](xrd_scan_app.py) | `XrdScanWindow` — main window; mirrors `DacScanWindow` layout; poni file selector; "Set ROI…" button opens `RoiDialog`; 表示ROI combobox switches displayed map |

## Key design decisions

- **In-memory integration** — `ai.integrate1d(img.astype(np.float32), npt=n_bins, unit="2th_deg", ...)` is called on the worker thread; no TIFF save required for integration.
- **TIFF save is optional** — checkbox in the UI; TIFFs go to `apps/xrd_scan/__localdata/<timestamp>/`.
- **Multiple ROIs** — stored as `intensity_maps[n_roi, n_ch5, n_ch4]`; all ROIs are computed in a single scan pass.
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
