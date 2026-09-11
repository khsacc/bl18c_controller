# Interactive camera — implementation details

Developer-facing detail for `apps/interactive_camera/interactive_camera.py`
(`MainWindow`) and `apps/interactive_camera/autofocus.py` (`AutoFocus`). For
the end-user-facing feature description (in Japanese) see
[docs/DOC_INTERACTIVE_CAMERA.md](../../docs/DOC_INTERACTIVE_CAMERA.md) and
[docs/DOC_SUB_CAMERA_CV2.md](../../docs/DOC_SUB_CAMERA_CV2.md).

## Click-to-move (Ch4/5)

Toggled via `on_click_to_move_toggled`; when enabled, clicking the live feed
moves the sample stage (Ch4/Ch5) so the clicked pixel becomes the frame
centre, using the calibration below.

## Calibration

Pixel↔stage-µm mapping persisted to
[calibration.json](calibration.json) (same directory). Loaded/saved once at
construction; not hot-reloaded elsewhere in the app.

## Autofocus — `AutoFocus` (`autofocus.py`)

Scans a controller channel through `current ± focus_range` in `step_size`
steps, measuring frame sharpness at each position, then moves to the best
position. Two independent `AutoFocus` instances exist:

- `self.autofocus` — focus axis **Ch3** (sample Z), the primary, user-facing
  autofocus (menu/button driven).
- `self.autofocus_ch7` — focus axis **Ch7**, a **hidden, right-click-only**
  menu feature (`Auto Focus by Ch7` context-menu section) with its own
  range/step constants (`_CH7_RANGE_UM`, `_CH7_STEP_UM`); it copies
  `method`/`n_frames`/`peak_method` from `self.autofocus` before each run
  rather than exposing separate UI controls for them.

`method` and `peak_method` (below) are set globally via **Settings → Auto
Focus…** (`AutoFocusSettingsDialog`), which writes directly to
`self.autofocus`; there is no per-tab control for them any more. The choice
is session-only — not persisted to disk — and resets to the defaults
(`'tenengrad'` / `'gaussian'`) on every app restart.

Sharpness metrics (`method=`): `'laplacian'` (`cv2.Laplacian(...).var()`,
default) or `'tenengrad'` (mean squared Sobel gradient magnitude). Optional
circular ROI (`roi={'cx','cy','r'}`) restricts the sharpness measurement to a
masked region — set via a right-click "with ROI" action, cleared after each
run.

Peak selection (`peak_method=`): `'highest'` (argmax of the scan) or
`'gaussian'` (fits `_gaussian` via `scipy.optimize.curve_fit`, with guards
against a peak outside the scan range, non-positive amplitude, or a sigma
that's noise-spike-narrow or scan-span-flat — falls back to `'highest'` on
any of these or if `scipy` isn't installed). Runs in a daemon `threading.Thread`
(`focus_thread`); progress/completion delivered via `callback`/
`completion_callback`, so callers must marshal any UI updates back to the
GUI thread themselves.

## Sample tracking ("Follow sample position")

Tracking tab (`_create_tracking_tab`) uses `cv2.matchTemplate` (`TM_CCOEFF_NORMED`)
against a saved reference image to detect XYZ drift (e.g. from cryostat
thermal expansion/contraction during low-temperature runs) and correct it by
moving the sample stage. Runs on a `follow_timer` (`QtCore.QTimer`) at a
user-configurable interval (minutes, `follow_interval_spinbox`). Autofocus
and tracking are coupled via `_af_sync_to_tracking` so the two features don't
fight over the sample stage at the same time.

Tab layout is a left/right split (`outer_layout`, 80/20 stretch): the left
column stacks the video preview and every settings control (reference photo,
log directory, interval, Auto-Focus Settings, the two movement-limit groups
side by side, Start/Stop buttons); `self.tracking_log` occupies the full
height of the right column on its own (no `setMaximumHeight` — it was moved
out of the bottom of the left column specifically to stop it from squeezing
the video preview's height). Within "Per-attempt movement limit", Ch4 and
Ch5 are stacked vertically (not side by side) so the group stays narrow
enough to sit next to "Total movement limits from start position".

When PACE5000 and/or LakeShore 335 is active, a third group beside the two
movement-limit groups lets the operator overlay selected sample-environment
values (gas pressure, ChA, ChB, and setpoint) above the timestamp. Values are
cached from the backends' existing update signals, so the camera loop never
performs instrument I/O. The overlays are applied only to display/save copies;
`self.current_frame`, reference frames, autofocus input, and tracking image
analysis remain unannotated.

The same selected values are appended to the sample-tracking CSV on every
tracking attempt. The selected column set is fixed when tracking starts and
the checkboxes are disabled for that session, keeping every CSV row aligned
with its header. Pressure is logged in MPa and LakeShore values in K; unavailable
readings are written as empty fields. Values are also echoed in the tracking
log pane after each attempt.

## Ruby spectrum collection during tracking

**Collect a ruby spectrum**, beside the tracking-image checkbox, starts one
non-blocking FluoRaPressée `POST /acquire/fit` request after each tracking
attempt that meets the similarity threshold. It uses the shared connection
from Settings > Online spectrometer and requests `fit_function="Moffat"` with
`fit_peak_count=2`. Spectrum work runs in a separate daemon thread so a slow,
unavailable, or misconfigured FRP service cannot fail or delay stage tracking
or image saving. If the previous request is still running, that attempt's
spectrum is skipped instead of starting overlapping acquisitions.

Each response is saved as `ruby_spectrum_<index>_<timestamp>.csv` in the same
`images_from_<tracking-start>` directory as tracking images. Columns contain
`x`, `x_unit`, `y_raw`, `y`, `fit_success`, `peak1_top`, and `peak2_top`. FRP returns the
acquired spectrum even when its numerical fit reports failure; in that case
the CSV is still written and the two peak columns are left blank. CSV writes
use a temporary file followed by an atomic replace. A request-level failure is
reported only in the tracking log and never propagates into `_follow_task`.
