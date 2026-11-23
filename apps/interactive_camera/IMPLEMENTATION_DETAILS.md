# Interactive camera — implementation details

Developer-facing detail for `apps/interactive_camera/interactive_camera.py`
(`MainWindow`) and `apps/interactive_camera/autofocus.py` (`AutoFocus`). For
the end-user-facing feature description (in Japanese) see
[docs/DOC_INTERACTIVE_CAMERA.md](../../docs/DOC_INTERACTIVE_CAMERA.md) and
[docs/DOC_SUB_CAMERA_CV2.md](../../docs/DOC_SUB_CAMERA_CV2.md).

## Shared USB camera lifecycle

`apps/interactive_camera/camera_session.py` owns `VideoCapture` on one capture
thread and publishes copies of raw frames with a monotonic timestamp from the
start of each `read()`. The launcher starts camera 0 at application startup and
keeps it open until shutdown, including across Experimental Scheduler runs.
Camera absence is retried in the background. A blocked driver read is never
raced by `release()` on another thread. A standalone Interactive Camera owns
its own session and stops it when the window closes.

Interactive Camera reads `latest()` for its previews and waits for
`frame_after()` for autofocus and tracking measurements. During a scheduler
run, the window switches to a live view page, disables menus and interaction,
and refuses closing; it returns to its original controls on completion.
The scheduler refuses to start while recording, tracking, autofocus or a
manual stage move remains active in this window. A missing live frame clears
the preview instead of leaving a stale image visible.

## Click-to-move (Ch4/5)

Toggled via `on_click_to_move_toggled`; when enabled, clicking the live feed
moves the sample stage (Ch4/Ch5) so the clicked pixel becomes the frame
centre, using the calibration below.

`manual_stage_moves.py` owns manual Ch3/4/5 movement for both MainWindow and
Sample Positions. `pixel_move` validates the calibration and image bounds,
then truncates the calibrated Ch4/Ch5 displacement to whole pulses. The
`ManualStageMoves` worker acquires one motion lease, checks the stopped stage,
validates X-ray targets against Ch6/Ch8 under that lease, sets requested
speeds and calls the existing constrained controller moves. It waits for stop
and returns to LOC before releasing the lease (including left-click centring).
Relative buttons in IC retain the controller's current speed; Sample Positions
explicitly selects L/M/H, default M, for both relative and pixel moves.

Busy, completion and errors reach widgets through Qt signals. A failed move
attempt, including a second-axis failure, requests normal stop and awaits its
confirmation before releasing ownership. An unconfirmed stop retains the
controller coordinator's recovery-required state. Stop and closing Sample
Positions cancel pending work and request an asynchronous stop only while its
worker still owns a valid lease. Closing is refused until the worker finishes;
registration controls are disabled while busy. The dialog clears unavailable
camera frames and excludes letterboxing from click coordinates. No camera or
controller ownership is transferred by manual movement.

## X-ray beam position (`xray_beam.py`)

**Remember this position as → X-ray beam position** writes
`__localdata/xray_beam.json` atomically: pixel, frame size, Ch6/Ch7/Ch8 pulses
(read from a stopped stage) and a timestamp. `MainWindow.xray_beam_pos` is a
property over `XrayBeamFileCache`, so the window never keeps its own copy; the
Experimental Scheduler reads the same file through the same module. The beam is
drawn as a thin red tilted cross. **Move this position to → X-ray beam** refuses to
move when the frame size or Ch6/Ch8 differ from the registration
(`check_xray_beam`), because the pixel no longer marks the beam. Ch7 may change
for focus adjustment; its registered pulse is metadata only. All manual moves,
gasket tracking, Sample Positions and scheduler mode checks use the same
Ch6/Ch8 selection (`gasket_units.XRAY_BEAM_CHANNELS`, also exposed as
`GASKET_BEAM_CHANNELS`). Registration still records all three axes, preserving
the existing file format. Runtime validity reads do not query Ch7.

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
During either Ch3 or Ch7 autofocus, the main live preview displays a centred
white progress message over the camera pixels. It is drawn only on the preview
copy, so snapshots, recordings and analysis frames do not contain the message.

## Sample tracking ("Follow sample position")

Tabs, in order: **Interactive Camera**, **Sample Tracking (Gasket Detection)**
(`gasket_tracking_tab.GasketTrackingTab`, `self.gasket_tab`) and **Sample
Tracking (Similarity)** (`_create_tracking_tab`, `self.tracking_tab`). Only one
tracking session runs at a time: `update_frame()` disables the other tracking
tab, each tab disables the Interactive Camera tab while it runs, and
`start_following()` refuses while the gasket tab is busy. Both tabs are
disabled during autofocus. `set_sequence_read_only()` and `closeEvent()` also
wait for the gasket worker thread.

### Shared with the Experimental Scheduler

These modules are used by both apps; keep gasket logic here, not in either app:

| Module | Contents |
|--------|----------|
| `gasket_tracking.py` | Qt-independent aperture-rim detector (`detect_gasket_hole[_near]`). Input is unmarked 8-bit frames and an axis-aligned ellipse; raises on uncertain detection. |
| `gasket_follow.py` | Reference validation (`resolve_reference`), the beam target (`gasket_target`), frame averaging (`measure_average`), tolerance, the correction cycle (`correction_cycle`) and verification with one post-focus retry (`align_and_verify`). Host operations come in through `FollowHooks`; failures are `GasketFollowError` with the runner's `runtime.gasket_*` codes. |
| `gasket_overlay.py` | Display-only overlay: loading the marked reference, live detection, drawing and status text. |
| `gasket_units.py` | px → Ch4/Ch5 µm conversion, `GASKET_BEAM_CHANNELS`, ruby offset origins. No OpenCV/Qt, so settings and safety-rule modules import it. |
| `sample_positions_dialog.py`, `gasket_mark_dialog.py` | **Set up sample positions** and **Mark gasket outline** dialogs. |
| `gasket_setup.py` | The Gasket Detection tab's saved setup (`__localdata/gasket_tracking.json`) and `sample_positions_result()`, which the scheduler's **Import from Interactive Camera** applies like a closed Sample Positions dialog. |

The Sample Positions live preview shows the registered laser position in both
tracking methods, using `MarksFileCache` and `draw_laser_spot` from `marks.py`.
Saved position changes are picked up on the next preview frame. The white
circle is drawn only on a display copy; reference captures and image analysis
continue to use unmarked frames.

The runner keeps thin wrappers (`_measure_gasket_average`, `_follow_correction_cycle`, …)
that convert `GasketFollowError` to `RunnerError` and supply its own limits
(`_move_follow_xy`), motion lease and autofocus.

### Gasket Detection tab

The tab's setup (reference photo path, ellipse, sample point and mode,
reference digest and capture position, ruby positions) and its settings are
saved in `__localdata/gasket_tracking.json`, independent of scheduler
sequences. The dialog is opened with `tracking_method="gasket_beta"`; ruby
positions are only recorded (offsets from the gasket centre).

Each attempt runs on a worker thread: `switch_to_rem`, then
`gasket_follow.align_and_verify(pre_focus=True)` with hooks for fresh frames
(`_fresh_camera_frame`), Ch4/Ch5 moves clamped by `clamp_xy_correction` against
the per-attempt and total limits, and Ch3 autofocus via `_z_sharpness_scan`
(given the tab's own origin and stop condition; Ch3 total is re-read from
the stage afterwards). The first attempt runs at Start; `follow_timer` is
single-shot and restarted when an attempt finishes, so the interval runs
from the end of one attempt to the start of the next. Detection failures first
try the one-excursion recovery below. An unrecovered detection failure,
beam/microscope mismatch, residual over tolerance or move constraint stops
tracking and logs the reason. If a move command fails
after the other axis was commanded, `_move_xy` still waits for the stage and
re-reads the Ch4/Ch5 totals before re-raising. Stop takes effect between
stage moves. As its last step each attempt confirms the stop
(`_confirm_stopped()`: `wait_until_stop`, else `normal_stop` and a
lease-free `wait_until_stop`). Once the worker has exited, `_finish_stop()`
switches the controller to LOC, releases the lease and closes the CSV,
exactly once per session — but only if the stop was confirmed. Otherwise it
keeps the lease (`holds_motion()`, so `busy()` stays true), warns, and the
Stop button becomes **Confirm stage stopped**, which retries the
confirmation on a worker thread. On window close, `shutdown()` requests the
stop and `finish_close()` completes it after `closeEvent()` has joined the
worker; `closeEvent()` refuses to close while the lease is kept. The CSV adds the averaged offset (px, µm) and score; plots
come from `_save_tracking_plots(csv_path, log)`.

Detection recovery is enabled for IC tracking and all ES gasket alignments:
following, final `stop_following`, one-shot `align_to_reference` and alignment
before ruby acquisition. Ruby alignment retains its separate normal correction
loop and Ch3-restoration policy (no additional autofocus), calling the shared
`recover_at_reference` only after a detection failure. Its returned inferred
residual is also used to calculate the gasket-centre origin for ruby offsets. After a
`runtime.gasket_detection_failed` batch, `align_and_verify` makes at most one
excursion per attempt, assuming the sample is already near the beam. With
`T = beam - sample_offset`, reference rim centre `R` and pixel-to-pulse matrix
`M_inv`, the outbound request is `M_inv @ (R - T)`. Both legs use the existing
per-move and total limits and motion lease. ES also holds its re-entrant
`_limited_stage_motion_lock` throughout the excursion, focus, measurement and
return so another scheduler Ch3/Ch4/Ch5 move cannot invalidate the result; stop
requests bypass that lock. Recovery reads Ch4/Ch5 before and
after each completed move and rejects a readback displacement different from
the applied command. The applied displacement also predicts the new search
centre if the outbound move was clamped. It optionally autofocuses at the
reference location, then reacquires with the same fresh-frame averaging and
confidence thresholds. From detected centre `C`, it requests
`M_inv @ (T - C)` to put the sample back on the beam. Beam and microscope
validity are refreshed before the excursion, during measurement, before the
return and after it. Successful return requires the inferred residual from the
detection plus the readback displacement to satisfy the usual tolerance.
There is no further autofocus or image verification at the beam, where the
rim may be clipped again. `gasket_recovered` distinguishes the inferred
residual in the host's log/status; CSV columns stay unchanged. Any repeat
detection failure, motion/readback/beam error, or excessive return residual
uses the existing stop/cleanup path. Stop requests never initiate another leg;
failed reacquisition leaves the stage at the excursion position. No automatic
return is attempted in a `finally` block.

The live overlay detects at most every 0.5 s on a worker thread while
**Activate gasket detection** is on or tracking runs, pausing during Ch3
autofocus, and shows the analysed frame with its result.

### Similarity tab

The Similarity tab (`_create_tracking_tab`) uses `cv2.matchTemplate` (`TM_CCOEFF_NORMED`)
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
laser position is drawn as a white circle with 70% opacity and an 8-pixel radius.
The Annotation group's preview checkboxes do not change this choice. Saved snapshots
and videos always include the timestamp and connected sample-environment text
in the black band, even when hidden in the preview.

The Annotation group's four checkboxes (all checked by default) control only the
live preview: **Show marks** (drawn shapes and the laser position mark),
**Show scale** (100 µm scale bar), **Show timestamp**, and **Show sample env.
prms.** (PACE5000/LakeShore 335 readings). The X-ray beam mark is not affected. The checkbox is
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
