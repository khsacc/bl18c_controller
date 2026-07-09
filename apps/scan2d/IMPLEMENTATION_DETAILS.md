# 2D Scan / 1D Scan — implementation details

Developer-facing detail for `apps/scan2d/` (2D Scan) and `apps/scan1d/`
(1D Scan). Documented together because the code is intertwined: `Scan1DWorker`
lives in `apps/scan2d/free_2d_scan_backend.py`, not under `apps/scan1d/`, and
both apps share `utils/fitting/`.

## 2D Scan (`apps/scan2d/`)

Generic 2-D grid-scan engine that `DacScanWindow` is now built on top of. The
user (or a fixed-axis subclass) picks **any two translation channels** —
`CHANNEL_CHOICES` in `free_2d_scan_backend.py` is `range(1, 11)`, i.e.
Ch1-Ch10. **Ch11 is intentionally excluded**: it's a rotation stage
(deg/pulse), not a translation axis, and mixing units into this scanner's
µm-based UI/plots would be misleading (`DacScanRotWindow`, which does drive
Ch11, remains a separate standalone implementation for that reason).

- **`Free2DScanWorker`** (`free_2d_scan_backend.py`) — same scan loop as the
  old `DacScanWorker`, generalized to take `ch_x` / `ch_y` instead of
  hard-coded channel constants. Backlash compensation (always-+-direction
  final approach, `BACKLASH_PULSES_X = 5`) applies to `ch_x` only, matching
  the original DAC Scan / Collimator Scan convention.
- **Constraint-violation safety** — because the axes are user-selectable, a
  scan can hit `MOVE_CONSTRAINTS` (e.g. the Ch8/Ch9 collision guard) in a way
  the old fixed-axis workers never could. `Free2DScanWorker.run()` wraps the
  whole scan in `try/except`; any exception (including `ValueError` from
  `move_ch_absolute`) reports the reason via `status_message` and aborts
  cleanly instead of silently killing the `QThread`.
- **`GpibReader` / `GpibReaderSim`** (`free_2d_scan_backend.py`) — same
  interface as the old `dac_scan_backend` versions; `GpibReaderSim` takes
  explicit `um_per_pulse_x` / `um_per_pulse_y` since the axes aren't fixed.
- **`Free2DScanWindow`** (`free_2d_scan_app.py`) — full UI (channel
  pulldowns, Gaussian/Aperture(erf) fit toggle, settle time, colour map, log
  saving, right-click "Go to this position"). Designed to be subclassed into
  a fixed-axis app via constructor kwargs:
  - `default_ch_x` / `default_ch_y` — initial (and, if locked, permanent)
    channel selection.
  - `allow_channel_change` — when `False`, the "Channel Selection" group box
    is hidden and the combo boxes are disabled; the window behaves like a
    classic single-axis-pair scan app.
  - `log_key` — passed to `log_prefs.should_save()` / `get_app_dir()`, so a
    subclass can keep saving to its own `__localdata/<key>/` directory (e.g.
    `DacScanWindow` still uses `"dac_scan"`, not `"free_2d_scan"`).
  - `window_title` — overrides the default `"2D Scan"` title.
  - `DacScanWindow` (`apps/dac_scan/dac_scan_app.py`) is the reference
    example: `Free2DScanWindow(default_ch_x=4, default_ch_y=5,
    allow_channel_change=False, log_key="dac_scan", window_title="DAC Scan
    (Normal)")`. `CollimatorScanWindow` has not been migrated yet and remains
    a standalone implementation.
- **Why `apps/scan2d/` and not `apps/2d_scan/`** — a directory name starting
  with a digit breaks ordinary `from apps.2d_scan.x import y` import
  statements (`SyntaxError: invalid decimal literal`); confirmed during
  implementation. Do not rename it back.

## 1D Scan (`apps/scan1d/`)

Single-axis sibling of 2D Scan. The user picks **one** translation channel
(`CHANNEL_CHOICES` = Ch1-Ch10; Ch11 rotation excluded for the same reason as
scan2d), enters a **± range in µm** (half-width — one-sided, per user
preference) and a grid-point count, and the scan steps `current ± range`
while reading transmitted intensity. The profile is fit with a Gaussian or
erf aperture model and the "Go to fitted center" button moves the channel to
the fitted centre (button press, not automatic).

- **`Scan1DWorker`** — lives in `apps/scan2d/free_2d_scan_backend.py` (next
  to `Free2DScanWorker`, per the "extend scan2d backend" rule below), **not**
  in `apps/scan1d/`. It is the 1-D reduction of `Free2DScanWorker`'s inner
  scan line: same `+`-direction backlash approach (`BACKLASH_PULSES_X`), same
  clean-abort-on-exception contract, emits `point_measured(col, transmitted,
  incident)`.
- **`Scan1DScanWindow`** (`apps/scan1d/scan1d_app.py`) — dedicated
  single-plot UI (not a subclass of `Free2DScanWindow`, whose 2-D colour-map
  layout doesn't reduce cleanly to 1-D). Reuses the leaf components instead:
  `_PulseAxisItem` / `_MicronAxisItem` from `free_2d_scan_app`, `GpibReader` /
  `GpibReaderSim` from the scan2d backend (the 2-D simulator is sliced at
  `y = 0`, its peak line, for a clean 1-D profile), and the shared fit module
  below. Own single-channel move worker `_Move1DWorker`. Saves
  `.npz/.json/.png` under `log_key="scan1d"`.
- Registered in `main.py` (`open_scan1d`, launcher **button** "1D Scan" in
  the "Scan" section) and in `settings.log_prefs.APP_KEYS` +
  `settings/pages/logging_page.py`. The "Scan" section buttons are, in order:
  Collimator Scan, DAC Scan (Normal), DAC Scan (Rotation Centre), DAC Scan
  (XRD), **1D Scan**, **2D Scan** (last). Both `Scan1DScanWindow` and
  `Free2DScanWindow` are launcher buttons — they used to be Tools-menu items
  and were moved into the "Scan" section.

**New scan apps that need shared scan-worker logic should extend
`apps/scan2d/free_2d_scan_backend.py`** — do not resurrect a per-app backend
module (see `apps/dac_scan/IMPLEMENTATION_DETAILS.md` for why
`dac_scan_backend.py` was deleted rather than kept alive).

## Shared profile fitting (`utils/fitting/`)

`utils/` is a top-level package for pure, Qt-free helpers shared across apps.
`utils/fitting/` holds the 1-D profile fit maths that scan1d **and** scan2d
both call, so the Gaussian / erf models live in exactly one place:

- `models.py` — `gaussian(x, A, x0, sigma, C)`, `aperture_model(x, A, x1, x2,
  w, bg)`.
- `profile_fit.py` — `fit_aperture_1d(x, profile)` and the high-level
  `fit_profile_1d(x, profile, model) -> ProfileFit | None`. `model` accepts
  the UI combo strings `"Gaussian"` / `"Aperture (erf)"`; `ProfileFit`
  carries `center`, `width` (+ `width_kind` label hint `"σ"`/`"width"`),
  `popt`, and a ready-to-plot `curve_x`/`curve_y` (already un-flipped for the
  aperture case).
- `Free2DScanWindow._run_fit` was refactored onto `fit_profile_1d`,
  collapsing its former per-axis Gaussian/erf duplication into one call per
  axis (the Y profile just plots with `curve_x`/`curve_y` swapped). The
  saved-JSON key names (`sigma_pulse` / `width_pulse`) and label formats are
  unchanged.
- The older scan apps (`xrd_scan`, `collimator_scan`, `dac_scan_rot`) still
  carry their own private copies of these fit helpers — they were left
  untouched to limit blast radius. Migrate them onto `utils.fitting` if you
  touch them.
