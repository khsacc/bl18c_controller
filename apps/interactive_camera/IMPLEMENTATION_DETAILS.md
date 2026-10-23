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

When calibration is available, the main camera preview shows a white horizontal
100 µm scale bar and white label in the lower-left corner of the camera pixels
(above the information band). Its pixel length comes from the norm of the two
stage-axis displacements produced by `matrix_inv @ [1, 0]`, scaled by the Ch4
and Ch5 µm/pulse values. An invalid calibration or a bar too wide to fit the
frame is omitted rather than labelled with an inaccurate length. The bar stays
visible when other marks are hidden; saved snapshots and videos include it only
when **Include marks in snapshots and videos** is selected.

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

An autofocus worker exception is logged to the terminal and forwarded through
`error_callback` to `MainWindow`'s queued Qt signal. The GUI shows the exact
reason in a warning dialog, including soft-limit and move-constraint rejections.
Other interactive stage moves use the same signal for `Move blocked:` errors;
the frame timer overwrites the transient status label, so it cannot serve as
the only error notification. The dialog is suppressed while the window closes.

## Sample tracking ("Follow sample position")

`gasket_tracking.py` is a Qt-independent beta aperture-rim detector shared
with Experiment Scheduler. Its input is unmarked 8-bit camera images and an
operator-drawn, axis-aligned ellipse; it returns the tracked outline and
quality measures or raises on uncertain detection. Interactive Camera's own
tracking controls still use the established image-similarity method. The
scheduler's Sample Positions panel currently owns the beta selection, mark
dialog, and live overlay; its ruby alignment and following use that selection.

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
movement-limit groups lets the operator include selected sample-environment
values (gas pressure, ChA, ChB, and setpoint) above the timestamp. The text is
drawn in a black band below the camera pixels for both previews and saved
images; clicks in the band do not map to camera coordinates. Video recording
reserves enough band height for its four possible lines so its frame size remains
fixed when instrument connections change. On the main camera tab, the launcher's
PACE5000 and LakeShore 335 connections automatically select gas pressure and
ChA/ChB respectively for previews, snapshots, and video. The tracking tab retains
its own explicit selections, including the optional setpoint, for its preview,
saved images, and CSV. Values are cached from the backends' existing update
signals, so the camera loop never performs instrument I/O. The band is applied
only to display/save copies; `self.current_frame`, reference frames, autofocus
input, and tracking image analysis remain unannotated.

The recording controls include an **Include marks in snapshots and videos**
checkbox (unchecked by default). It selects whether drawn shapes, the laser
position, and other marks are copied into snapshots and recorded frames. The
preview's Show/Hide Marks control does not change this choice. Saved snapshots
and videos always include the timestamp and connected sample-environment text
in the black band, even when the preview timestamp is hidden. The checkbox is
fixed while a video is being recorded. Tracking images continue to use unmarked
camera frames.

The same selected values are appended to the sample-tracking CSV on every
tracking attempt. The selected column set is fixed when tracking starts and
the checkboxes are disabled for that session, keeping every CSV row aligned
with its header. Pressure is logged in MPa and LakeShore values in K; unavailable
readings are written as empty fields. Values are also echoed in the tracking
log pane after each attempt.

When **Save image after every tracking attempt** is selected, one image is
saved per tracking interval, regardless of whether the similarity threshold
is met. After an unsuccessful similarity check, the image is captured after
the final XY correction. Ruby spectrum collection still requires a successful
similarity check.

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
