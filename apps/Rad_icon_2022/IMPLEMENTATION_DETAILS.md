# Rad-icon 2022 detector — implementation details

Developer-facing detail for `apps/Rad_icon_2022/`. Read this before touching
the backend, DLL, or UI image pipeline.

## Architecture

```
Python (RadiconBackend) → radicon_dll.dll (C++/ctypes) → SapClassBasic86.dll (Sapera LT C++) → Xtium-CL MX4 frame grabber → Rad-icon 2022 (CameraLink)
```

The DLL source is in [dll/radicon_dll.cpp](dll/radicon_dll.cpp). Build with
Visual Studio 2019, Release | x64.

## Two independent control channels

The Rad-icon 2022 is controlled via **two independent channels** that must
both be used correctly:

1. **Sapera LT (CameraLink frame grabber)** — handles DMA transfer of pixel
   data from sensor → PC memory. Controlled via `SapAcquisition` /
   `SapAcqToBuf` / `SapBuffer` C++ classes.
2. **CameraLink serial port (COM2, 115200 baud)** — sends ASCII commands to
   the camera's internal controller for exposure, binning, and readout mode.
   This is a separate RS-232-over-CameraLink channel, NOT the Sapera API.

## Camera serial command protocol (COM2, 115200 baud, `\r` terminator)

Determined by reverse-engineering the commercial control software
XFPCAP01.exe (ILSpy decompile; sources saved in
[__localdata/decompiled_XFPCAP01/](__localdata/decompiled_XFPCAP01/)).

| Command | Meaning |
|---------|---------|
| `sbn 0\r` | Set binning: 1x1 (full resolution) |
| `sbn 1\r` | Set binning: 2x2 |
| `seu 0\r` | Unknown init command sent once at startup |
| `set <ms>\r` | Set exposure time in **milliseconds** (e.g. `set 1000\r` = 1 s) |

Camera replies `USER` (or a string containing "USER") when ready after serial
open. Startup sequence must wait for this before issuing the first `Grab()`.

**Critical**: `CORACQ_PRM_TIME_INTEGRATE_DURATION` (Sapera API) does NOT
control exposure for this camera. Exposure MUST be set via serial
`set <ms>` command.

**CC1 triggering**: CC1 signals from the frame grabber are **ignored** by the
Rad-icon 2022. The camera runs on its own internal FreeRun timer set by
`set <ms>`. CamExpert Time Integration methods (Methods 1, 3, 5, 6, 8) and
External Trigger mode on the frame grabber have no effect on when the camera
starts or stops exposing. Confirmed by experiment (2026-06).

**Triggered acquisition** (`snap_triggered()` in `radicon_backend.py`):
Because CC1 and `seu 0` mid-session do not reset the camera's integration
timer, triggered mode is achieved by keeping the camera at a short idle
exposure (`_IDLE_EXPOSURE_MS = 100 ms`) between snaps. On user trigger:
1. Send `set <real_ms>` — camera switches within at most one idle cycle
   (≤100 ms).
2. Any transition idle frame is discarded via a 300 ms snap attempt.
3. Clean real-exposure frame is returned.
4. `set 100` reverts the camera to idle for the next trigger.
Max latency from button press to exposure start: ≈100 ms.

`capture_triggered(exposure_ms, timeout_ms=None)` is the shared one-shot entry
point for RadiconWindow and the experimental scheduler. It stores the requested
exposure without an early serial command, then calls `snap_triggered()` under
the backend's acquisition lock. Direct `snap()` and exposure-setting calls use
the same lock. A failed triggered capture also attempts to restore the 100 ms
idle exposure; the underlying blocking DLL snap cannot be cancelled mid-frame.

The single-shot status shows elapsed seconds from the acquisition request.
The camera does not report its exact integration-start instant, so the UI
timer can lead the physical exposure by up to one idle cycle (100 ms).
After a successful save, the status shows the elapsed time sampled when the
frame reaches the UI, before image correction and TIFF saving.

## Sequential acquisition frame skipping

The sequential acquisition UI takes a saved-frame count and a **Skip frames**
count. The first acquired frame is saved; the worker then calls `snap()` once
for each frame to discard before capturing the next saved frame. A skip count
of 0 saves every acquired frame, while 1 saves alternating frames. Skipping
occurs only between saved frames, so `n` saved frames require
`n + (n - 1) * skip_frames` snap calls. Discarded frames are neither displayed
nor included in the average.

The estimate uses this snap count times the requested exposure duration. It
does not include serial exposure changes, image processing, or file writes.
The preference is `seq_skip_frames` and the sequence TIFF metadata field is
`skip_frames`; old `seq_interval_ms` preferences are ignored and default to 0
skipped frames. Each `snap()` waits for the next frame callback, but the DLL
does not queue every physical frame. If processing or scheduling runs longer
than an exposure, additional frames may pass without a `snap()` call, so exact
physical frame parity must be verified on the detector.

Sequence averages use an independent filename series,
`<prefix>_ave_<index>.tif`, starting at 1 and advancing beyond the highest
existing average index in the save directory. This applies regardless of the
selected suffix mode. Individual frames keep their existing filename rules.

For individual frames and single shots, the numbered suffix is four digits
(`_0001`, `_0002`, …). The next number is one greater than the highest
existing `<prefix>_<number>.tif` or `.tiff` with at least four digits in the
selected directory. Other file types, average images, date suffixes and old
one- or three-digit suffixes do not advance this series. After `_9999`, the
suffix expands to `_10000`. Saved `index` and `index3` preferences select the
four-digit mode when loaded.

## STARS shutter confirmation

Before single-shot, sequential, or live acquisition, `RadiconWindow` queries
`stopper.Status GetValue` on a background thread. A confirmed `In` position
opens a Yes/No confirmation (No is the default); declining leaves the detector
idle. `Out`, `Neutral`, and failed or unavailable STARS communication proceed
without a confirmation. Dark-current acquisition skips this check. The launcher
uses its shared simulated shutter in `--debug` mode and reuses the manual
shutter tool's controller while that tool is open; otherwise the status check
owns and closes a short-lived STARS connection. The detector never moves the
shutter as part of this check.

## PONI calibration at startup

The launcher restores the saved `.poni` path from
`settings/__localdata/detector_calibration.json` into the shared `PoniState`
before opening any detector window. Rad-icon's Instant 1D status therefore
reflects the registered calibration immediately, even if Settings has not
been opened. Missing or unreadable files leave the state uncalibrated; a
calibration already created in the current session takes precedence over the
saved path.

Settings → XRD analysis also restores the optional downstream diamond
thickness (mm) before detector windows open. `PoniState.ai` and the `.poni`
file retain the standard-sample distance. `PoniState.integration_ai` is a
separate geometry with the thickness added to `dist` only when the correction
is enabled. Rad-icon Instant 1D, XRD Scan (including ROI test shots), and the
Experimental Scheduler use this reduction geometry. Calibration-standard
refinement and its preview keep the original geometry.

## Instant 1D integration compared with IPAnalyzer

Settings → XRD analysis holds the shared spot threshold (default 5.0 standard
deviations, matching IPAnalyzer). Rad-icon Instant 1D and Experimental
Scheduler each have their own "Find spots before 1D integration" checkbox,
off by default and saved with that app's 1D settings. Rad-icon reads the
shared threshold when reducing a frame; Experimental Scheduler snapshots it
before a run. The corrected TIFF is unchanged; only the pyFAI integration
mask excludes detected spots.
`instant1d.find_spot_mask` ports IPAnalyzer's `Ring.FindSpots`: calibrated
2θ is converted to perpendicular-plane radius in horizontal pixel units,
rounded to a ring index; each ring uses mean plus factor × sample standard
deviation as its upper threshold, and a detected pixel masks neighbours
strictly within a 3-pixel radius. Rings with fewer than two valid pixels
cannot yield spots. Unlike the linear-index C# implementation, expansion
respects image row boundaries. This is a port of spot rejection, not of the
rest of IPAnalyzer's integration engine.

IPAnalyzer's documented concentric integration distributes a pixel's
intensity among angular bins in proportion to the area in each bin. Instant 1D
uses pyFAI full pixel splitting (`("full", "histogram", "cython")`) for the
same purpose. The default angular bin width is 0.005 degrees, matching the
IPAnalyzer UI default; a previously saved bin width is preserved. Splitting
smooths the profile at fine bin widths and correlates neighbouring bins.

This is an approximation of IPAnalyzer's integration, not an exact numerical
port. Instant 1D offers optional minimum and maximum 2θ inputs beside the
angular bin width. Each input defaults to Auto, retaining the full-detector
range. A specified bound is passed to pyFAI as part of `radial_range`; the bin
count follows the selected interval. The detector preview shades excluded
pixel centres with translucent purple. This display overlay does not alter
the corrected TIFF or the spot-rejection mask. Instant 1D applies pyFAI
solid-angle correction and a fixed 0.95 polarization factor.

Recomputing a displayed image retains its filename stem as the PDI profile
title for a later manual "Send now". An unsaved live frame uses a timestamped
title.

For comparisons, use the same corrected image, geometry, angular step, masks,
and correction settings in both programs. IPAnalyzer's integration engine is
in the Crystallography submodule, which is not populated in the local source
checkout, so its exact normalization cannot be verified here.

## Image processing pipeline

### TIFF instrument metadata

Every saved single, sequence-frame, sequence-average, and dark TIFF uses the
same JSON ImageDescription header. At save time, the shared
`build_instrument_metadata()` helper reads the
PM16C pulse positions for Ch3, Ch4, Ch5, Ch7, and Ch9 into
`stage_ch<N>_pulses`. A failed position query is recorded as `null`; it does
not prevent the image from being saved. No stage keys are added when the
window has no stage controller (standalone detector mode).

When enabled in the launcher, PACE5000 contributes `pace5000_pressure_mpa`
and `pace5000_pressure_timestamp`. LakeShore335 contributes
`lakeshore_t_a_k`, `lakeshore_t_b_k`, and
`lakeshore_temperature_timestamp`. These values come from the backends'
existing poll caches; saving never sends a pressure or temperature command.
The timestamps are Unix seconds. If there is no sample from the last 10
seconds, the corresponding values and timestamp are `null`. Disabled
instruments have no keys in the header. The positions and measurements are
sampled when the frame is saved, which can be later than the exposure; an
averaged image records the state when that average is saved. The experimental
scheduler uses the same helper for saved XRD TIFFs.
Corrected TIFFs record the applied `defect_mode` (`median`, `neg1`, or
`none`) and a `defect_kernel` only when median correction was applied.

`image_utils.py` provides the shared processing and TIFF writer for
RadiconWindow and the experimental scheduler. `radicon_backend.py` returns raw
frames:

```
raw frame (uint16)
  → process_frame()      # flip, subtract dark, correct defects in order
  → save_tiff() / display
```

**Image flip** (`_apply_flip()`): Default: vertical flip only (`flip_v=True`,
`flip_h=False`) matching the sensor read direction. Controlled by checkboxes
in "検出器設定". Dark images must be acquired with the same flip settings as
measurement images — changing flip mid-session requires re-acquiring dark.
`process_frame()` flips the raw-coordinate defect mask with the frame and
reports whether each correction was applied; callers use those results in
their TIFF metadata. Filename and app-specific instrument metadata remain
with each caller.

The last successfully loaded or acquired dark-image path is saved as
`dark_file_path` in `__localdata/radicon_ui_prefs.json`. On window startup the
image is loaded with the same grayscale validation as manual loading, then
the saved dark-correction setting determines whether it is subtracted from
new frames. If the file is unavailable or invalid, startup continues without
dark correction and shows a load error; the saved path remains available for
a later launch.

## Pixel-defect correction

Implemented entirely in `radicon_ui.py` (no changes to backend or DLL). Three
module-level helpers:

| Function | Role |
|---|---|
| `_parse_defect_file(path, binning, h_blank, width, height)` | Parse XFPCAP01 defect file → `set` of `(row, col)` in unflipped acquisition coords |
| `_build_defect_mask(defects, height, width)` | Convert set → `bool ndarray` mask |
| `_apply_defect_correction(img, defect_mask, kernel)` | Replace defect pixels with neighbourhood median |

**Defect file format** (XFPCAP01 `.txt`):

```
Sensor:Rad-icon 2022 2064x2236   ← ignored
$defect                           ← ignored
C,<col> <row_start>-<row_end>    ← column-segment defect (col = horizontal index)
R,<row> <col_start>-<col_end>    ← row-segment defect
P,<col>,<row>                    ← single-pixel defect
```

All coordinates are in **1×1 sensor space** (width 2064 × height 2236). The
file at `__localdata/XFPCAP01_defects/欠陥ファイル03.txt` is the production
defect map shipped with the commercial XFPCAP01 software.

**Coordinate conversion to image space**:

| Binning | Column | Row |
|---|---|---|
| 1×1 | `col_sensor + 7 − h_blank` | `row_sensor` |
| 2×2 | `col_sensor // 2 + 7 // 2 − h_blank` | `row_sensor // 2` |

XFPCAP01's `iSensorXoff = 7` is in 1×1 sensor pixels; its 2×2 crop starts at
column `7 // 2 = 3`. Our backend crops `h_blank = 4` pixels from the raw
frame's left side. Thus the 2×2 defect-file columns need an offset of
`3 − 4 = −1`, rather than `−4`. This was verified against the 2026-10-21
commercial and Python TIFF pair: the Python image is shifted one column left
of the commercial image, and the remaining vertical defects were three columns
right of the old mask. The 1×1 offset follows the XFPCAP01 configuration but
has not been checked against a 1×1 image. Out-of-bounds results are silently
discarded.

The UI flips the acquired frame before dark and defect correction. It applies
the same vertical/horizontal flip to the defect mask when correcting a frame,
so the mask and saved image use the same coordinates. The stored mask remains
in unflipped acquisition coordinates.

**Algorithm**: For each defect pixel `(r, c)`, replace with `median` of valid
(non-defect) pixels in an N×N window (N = 3, 4, 5, or 6 — user selectable).
All lookups use the **original** pixel values (`img`), not the in-progress
`result`, so adjacent defects do not corrupt each other's replacement values.

**Notable defects in the production file**: tap-boundary column artefacts at
regular 172-pixel intervals (86, 258, 430, …, 1979 for the lower half; 85,
257, …, 1978 for the upper half), the center column (1032), one partial row,
and three isolated point defects.

**Auto-load**: on `RadiconWindow` startup, `_build_ui` tries the saved prefs
path first, then falls back to the bundled XFPCAP01 defect file. File
selection and kernel size are persisted in `radicon_ui_prefs.json` under keys
`defect_file_path`, `defect_correct_enabled`, `defect_kernel_size`.

## Startup sequence (matches XFPCAP01.exe behaviour)

```
1. SapInit: SapAcquisition + SapBufferWithTrash (ScatterGather) + SapAcqToBuf
2. Check Acq.SignalStatus != None (camera connected check)
3. Open COM2 @ 115200 baud
4. Send "sbn 0\r" or "sbn 1\r" (binning)
5. Send "seu 0\r"
6. Send "set 100\r" (startup exposure 100 ms)
7. Wait up to 10 s for "USER" in serial receive buffer
8. Call Xfer.Grab() — starts continuous FreeRun capture
9. Send "set <actual_ms>\r" (set real exposure)
```

**Do NOT call `SoftwareTrigger()`** — the CCF (FreeRun mode) handles CC1
assertion automatically. Calling `SoftwareTrigger()` interferes and can cause
timeouts.

## Sapera object configuration (correct settings)

```cpp
// Buffer: SapBufferWithTrash + ScatterGather (NOT plain SapBuffer)
ctx->buf = new SapBufferWithTrash(RING_BUF_COUNT, ctx->acq,
                                  SapBuffer::MemoryType::ScatterGather);

// Transfer: must explicitly set EndOfFrame event type
ctx->xfer->Pairs[0].EventType = SapXferPair::XferEventType::EndOfFrame;

// Enable StartOfFrame event on acquisition
ctx->acq->EnableEvent(SapAcquisition::AcqEventType::StartOfFrame);

// Flip for sensor direction 2 (current production setting)
ctx->acq->SetFlip(SapAcquisition::FlipMode::None);

// Check signal before Grab()
if (ctx->acq->SignalStatus() == SapAcquisition::AcqSignalStatus::None) → fail

// First frame after Grab() is unreliable — skip it (gbStartFlag pattern)
```

## Production settings (from XFPCAP01.ini, as of 2026-06-18)

| Parameter | Value |
|-----------|-------|
| CCF | `T_Rad-icon_2022_Xtium_FullFOV_2x2_FreeRun.ccf` (2x2 binning) |
| Exposure | 1000 ms (`set 1000\r`) |
| Binning | 2x2 (`sbn 1\r`) |
| Sensor direction | 2 (flip None, image rotated 180° in software) |
| Ring buffer depth | 20 (`iMaxGrabBuf`) |
| Serial port | COM2, 115200 baud |
| Server name | `Xtium-CL_MX4_1` |

## Building the DLL

**Prerequisites**:
- Visual Studio 2019 (or 2022) with C++ workload — found by vswhere
  automatically
- Sapera LT SDK installed (headers/libs expected at
  `C:\Program Files\Teledyne DALSA\Sapera\Classes\Basic\`)

**Before building**: the DLL must NOT be loaded by any Python process. Close
the interactive camera app or any script that calls `RadiconBackend`.

**Build command (PowerShell)**:

```powershell
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsPath = & $vswhere -version "[16.0,19.0)" -property installationPath 2>$null | Select-Object -First 1
$vcvars = "$vsPath\VC\Auxiliary\Build\vcvars64.bat"
$proj = 'D:\FPD-PC_User Data\Kagi\Hiroki_pyCodesLab\bl18c_controller\apps\Rad_icon_2022\dll\RadiconDll_2019.vcxproj'
cmd /c "call `"$vcvars`" && msbuild `"$proj`" /p:Configuration=Release /p:Platform=x64 /m /nologo /v:minimal" 2>&1
```

Output DLL: `dll/Release/radicon_dll.dll`

The `build.bat` in the same directory does the same thing but cannot be
called directly from PowerShell (use the command above instead, or run
`cmd.exe /c build.bat` from a cmd prompt).

## Key files

### Shared Instant 1D reduction

`instant1d.py` owns the pyFAI integration options, detector-wide angular-bin
calculation, axis conversion, and CSV/TSV/GSAS/Igor export used by both the
Rad-icon window, Experimental Scheduler, and XRD Scan. Callers supply an already
corrected image and the shared `PoniState.ai` calibration. The integration
uses full pixel splitting, solid-angle correction and a polarisation factor
of 0.95. Auto-saved profiles are placed beside the corresponding TIFF;
PDIndexer delivery remains a caller responsibility on the GUI thread.

| File | Purpose |
|------|---------|
| [radicon_backend.py](radicon_backend.py) | Python high-level API (ctypes wrapper) |
| [radicon_ui.py](radicon_ui.py) | PyQt6 UI — acquisition, dark correction, pixel-defect correction, display |
| [dll/radicon_dll.cpp](dll/radicon_dll.cpp) | C++ DLL — Sapera LT acquisition logic |
| [dll/radicon_dll.h](dll/radicon_dll.h) | C public API exported by the DLL |
| [dll/RadiconDll_2019.vcxproj](dll/RadiconDll_2019.vcxproj) | VS 2019 project file |
| [dll/build.bat](dll/build.bat) | Build script (run from cmd.exe, not PowerShell) |
| [__localdata/XFPCAP01_defects/欠陥ファイル03.txt](__localdata/XFPCAP01_defects/欠陥ファイル03.txt) | XFPCAP01 defect map (1×1 sensor coords; auto-loaded on startup) |
| [__localdata/decompiled_XFPCAP01/](__localdata/decompiled_XFPCAP01/) | ILSpy-decompiled source of the commercial XFPCAP01.exe |
