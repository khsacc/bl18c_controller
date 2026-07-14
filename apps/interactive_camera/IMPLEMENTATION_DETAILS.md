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
