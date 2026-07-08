# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PyQt6 desktop application for controlling hardware at synchrotron beamline BL-18C (Photon Factory, KEK). Controls a PM16C stepping-motor controller (TCP) and optionally a Druck PACE5000 pressure controller (TCP/SCPI), plus a USB camera. In future, we plan to further include other hardwares, such as Rad-icon 2022 and LakeShore 335. 

Python version: 3.13 (see [.python-version](.python-version)). Dependencies: `PyQt6`, `opencv-python`, `numpy`.

The app also includes a standalone file-conversion tool (no hardware needed) for converting IPAnalyzer `.prm` files to pyFAI `.poni` format, accessible via **Tools → Convert IPA prm file to poni format** in the menu bar.

## Running the app

```bash
# Normal mode — requires hardware at 192.168.1.55:7777
python main.py

# Simulation mode — no hardware needed, uses PM16CControllerSim
python main.py --debug
```

Each sub-app can also be run standalone (it creates its own controller if not passed one):

```bash
python apps/ui_stage_controller/stage_controller.py
python apps/simple_stage_cont.py
python apps/interactive_camera/interactive_camera.py
python apps/PACE5000/app.py
python apps/ipa_poni/ipa_poni_dialog.py   # no controller needed
```

## Architecture

### Entry point and hardware lifecycle

[main.py](main.py) hosts `ModeSelectorLauncher` (a `QWidget`). On startup it:
1. Connects to `PM16CController(ip='192.168.1.55', port=7777)`, or falls back to `PM16CControllerSim` in `--debug` mode.
2. Optionally connects to `Pace5000Backend` when the user checks the PACE5000 checkbox.
3. Opens sub-app windows on button click, passing the shared `controller` instance. On close it calls `controller.switch_to_loc()` then `controller.disconnect()`.

### Controller interface pattern

Every sub-app window accepts an optional `controller=` kwarg. When provided the window uses it and sets `self._owns_controller = False` (no disconnect on close). When omitted, the window creates its own `PM16CController` and owns its lifecycle. `PM16CControllerSim` is a drop-in replacement that implements the exact same public interface and is safe to pass in place of the real controller.

**Known pre-existing bug**: `apps/simple_stage_cont.py` (no import fallback at all) and `apps/ui_stage_controller/stage_controller.py` (its fallback `sys.path` insert is one `dirname()` short of the `bl18c_controller` root) cannot resolve `utils.control_stage` when run directly (`python3 apps/.../*.py`) — only launching via `main.py` works for these two. Confirmed present before the `control_stage`/`control_stage_sim` → `utils/` move too, so it isn't a regression from that move. Left unfixed per user request (2026-07-05).

### PM16CController ([utils/control_stage.py](utils/control_stage.py))

TCP socket client for the PM16C controller. Protocol: ASCII commands + `\r\n` terminator. Key design detail: the controller must be switched to **REM** (remote) mode before any move command, and back to **LOC** (local) after. Move methods call `switch_to_rem()` automatically; `wait_until_stop()` calls `switch_to_loc()` when done.

Channel encoding: channels 1–9 → `"1"`–`"9"`, channel 10 → `"A"`, channel 11 → `"B"` (see `stringify_ch_numbers`).

#### Inter-channel move constraints

`MOVE_CONSTRAINTS` at the top of [utils/control_stage.py](utils/control_stage.py) defines safety rules evaluated before every absolute or relative move. Current rules prevent the detector (Ch9) and microscope arm (Ch8) from colliding:

- Ch9 ≥ −30000 only allowed when Ch8 ≤ 0
- Ch8 ≥ 0 only allowed when Ch9 ≤ −30000

`check_move_constraints(ch, target_pos)` returns `(True, "")` or `(False, reason)`. Move methods raise `ValueError(reason)` on violation — UIs catch this and show a warning dialog.

### PM16CControllerSim ([utils/control_stage_sim.py](utils/control_stage_sim.py))

Background thread runs at ~100 Hz, incrementing channel positions toward their targets. Applies the same `MOVE_CONSTRAINTS`. Initial positions match BL-18C typical startup state. Speed steps per channel are defined in `_SPEED_STEPS`.

### Sub-applications

| App | File | Purpose |
|-----|------|---------|
| `Bl18cStageControlApp` | [apps/ui_stage_controller/stage_controller.py](apps/ui_stage_controller/stage_controller.py) | Focused UI for BL-18C key channels (6, 7, 8, 9). Includes stage visualization, shortcut buttons that sequence two moves in order to respect constraints, and a `ControllerPoller` (QTimer at 300 ms) for live position updates. |
| `StageControllerApp` | [apps/simple_stage_cont.py](apps/simple_stage_cont.py) | Raw control for all 11 channels. One `MotorControlWidget` per channel with absolute/relative move and speed selector. |
| `MainWindow` (Interactive Camera) | [apps/interactive_camera/interactive_camera.py](apps/interactive_camera/interactive_camera.py) | Live camera feed (OpenCV), click-to-move (Ch4/5), autofocus scan (Ch3 via Laplacian sharpness), snapshot/video recording, sample-tracking tab (template matching → XYZ correction on a configurable interval). Calibration data persisted to `apps/interactive_camera/calibration.json`. |
| `Pace5000Window` | [apps/PACE5000/app.py](apps/PACE5000/app.py) | Druck PACE5000 pressure monitor/controller. Connects via SCPI over TCP (default port 5025). Supports manual pressure/slew-rate control, CSV logging, and a scheduled sequence runner (`ScheduledControlRunner`). |
| `DacScanWindow` | [apps/dac_scan/dac_scan_app.py](apps/dac_scan/dac_scan_app.py) | 2-D transmission scan over Ch4 (X) / Ch5 (Y). Reads photodiode current via `Keithley2000Reader` (injected from main window). Displays live colour map and runs a Gaussian fit on completion. Thin Ch4/Ch5-fixed subclass of `Free2DScanWindow` — see [2D Scan](#free-2d-scan-appsscan2d). |
| `DacScanRotWindow` | [apps/dac_scan/dac_scan_rot_app.py](apps/dac_scan/dac_scan_rot_app.py) | Same as above but also rotates Ch11 (rotation stage) at each row. Independent implementation — does not use `Free2DScanWindow` (Ch11 is a rotation axis, out of scope for the generic 2D-translation scanner). |
| `CollimatorScanWindow` | [apps/dac_scan/collimator_scan_app.py](apps/dac_scan/collimator_scan_app.py) | Scans the collimator axis (Ch1/Ch2). Still its own standalone implementation — has not been migrated to `Free2DScanWindow` yet. |
| `Free2DScanWindow` | [apps/scan2d/free_2d_scan_app.py](apps/scan2d/free_2d_scan_app.py) | "2D Scan" — generic version of DAC Scan where the user picks any two translation channels (Ch1-Ch10) via pulldowns instead of a fixed axis pair. See [2D Scan](#free-2d-scan-appsscan2d). |
| `Scan1DScanWindow` | [apps/scan1d/scan1d_app.py](apps/scan1d/scan1d_app.py) | "1D Scan" — single-axis counterpart of 2D Scan. User picks one translation channel (Ch1-Ch10), scans `current ± range` over a grid, fits the transmitted-intensity profile (Gaussian / erf), and can move the channel to the fitted centre. See [1D Scan](#1d-scan-appsscan1d). |
| `IpaPoniDialog` | [apps/ipa_poni/ipa_poni_dialog.py](apps/ipa_poni/ipa_poni_dialog.py) | File-conversion dialog (no hardware). Converts IPAnalyzer `.prm` detector parameter files to pyFAI `.poni` format for use with azimuthal integration. Backend logic (pure Python, no Qt) is in [apps/ipa_poni/ipa_to_poni.py](apps/ipa_poni/ipa_to_poni.py). |
| `SpeedControllerWindow` | [apps/speed_controller/speed_controller_app.py](apps/speed_controller/speed_controller_app.py) | Tools-menu tool. Reads/writes the actual pps value of each channel's L/M/H speed register (Ch1–11 × L/M/H, via `PM16CController.get_ch_speed_value`/`set_ch_speed_value`). See [Speed Controller](#speed-controller-appsspeed_controller). |
| `XrdScanWindow` | [apps/xrd_scan/xrd_scan_app.py](apps/xrd_scan/xrd_scan_app.py) | DAC Scan (XRD) — Ch4/Ch5 grid scan using Rad-icon 2022 images instead of GPIB photodiode. Performs pyFAI in-memory azimuthal integration at each grid point; maps user-defined 2θ ROI intensities. Multiple ROIs supported; combobox switches displayed map and triggers immediate refit. Enabled in main window when Rad-icon 2022 is connected. |

### XRD Scan ([apps/xrd_scan/](apps/xrd_scan/))

Performs a Ch4/Ch5 grid scan using Rad-icon 2022 XRD images; integrates each frame in-memory via pyFAI and maps ROI intensities. **For all pyFAI / poni-file patterns used here, see the project-level skill `/pyfai-integration` (`.claude/commands/pyfai-integration.md`).**

#### Module layout

| File | Role |
|------|------|
| [apps/xrd_scan/xrd_scan_backend.py](apps/xrd_scan/xrd_scan_backend.py) | `RoiSpec` dataclass, `parse_poni` / `build_ai` helpers, `XrdScanWorker` (QThread) |
| [apps/xrd_scan/roi_dialog.py](apps/xrd_scan/roi_dialog.py) | `RoiDialog` — non-modal window: pyqtgraph 1D spectrum + `pg.LinearRegionItem` per ROI + QTableWidget; bidirectional sync; "Take Test Shot" button |
| [apps/xrd_scan/xrd_scan_app.py](apps/xrd_scan/xrd_scan_app.py) | `XrdScanWindow` — main window; mirrors `DacScanWindow` layout; poni file selector; "Set ROI…" button opens `RoiDialog`; 表示ROI combobox switches displayed map |

#### Key design decisions

- **In-memory integration** — `ai.integrate1d(img.astype(np.float32), npt=n_bins, unit="2th_deg", ...)` is called on the worker thread; no TIFF save required for integration.
- **TIFF save is optional** — checkbox in the UI; TIFFs go to `apps/xrd_scan/__localdata/<timestamp>/`.
- **Multiple ROIs** — stored as `intensity_maps[n_roi, n_ch5, n_ch4]`; all ROIs are computed in a single scan pass.
- **ROI change → immediate refit** — `RoiDialog.roi_list_changed` signal is connected to `_on_roi_list_changed`, which calls `_run_fit()` if scan data exists and scan is not running.
- **Combobox switch → immediate refit** — `_roi_display_combo.currentIndexChanged` also calls `_run_fit()`.
- **Backlash** — identical pattern to `DacScanWorker`: Ch4 always approached from `target − BACKLASH_PULSES_CH4` before each row.

### 2D Scan ([apps/scan2d/](apps/scan2d/))

Generic 2-D grid-scan engine that `DacScanWindow` is now built on top of. The user (or a fixed-axis subclass) picks **any two translation channels** — `CHANNEL_CHOICES` in `free_2d_scan_backend.py` is `range(1, 11)`, i.e. Ch1-Ch10. **Ch11 is intentionally excluded**: it's a rotation stage (deg/pulse), not a translation axis, and mixing units into this scanner's µm-based UI/plots would be misleading (`DacScanRotWindow`, which does drive Ch11, remains a separate standalone implementation for that reason).

- **`Free2DScanWorker`** (`free_2d_scan_backend.py`) — same scan loop as the old `DacScanWorker`, generalized to take `ch_x` / `ch_y` instead of hard-coded channel constants. Backlash compensation (always-+-direction final approach, `BACKLASH_PULSES_X = 5`) applies to `ch_x` only, matching the original DAC Scan / Collimator Scan convention.
- **Constraint-violation safety** — because the axes are user-selectable, a scan can hit `MOVE_CONSTRAINTS` (e.g. the Ch8/Ch9 collision guard) in a way the old fixed-axis workers never could. `Free2DScanWorker.run()` wraps the whole scan in `try/except`; any exception (including `ValueError` from `move_ch_absolute`) reports the reason via `status_message` and aborts cleanly instead of silently killing the `QThread`.
- **`GpibReader` / `GpibReaderSim`** (`free_2d_scan_backend.py`) — same interface as the old `dac_scan_backend` versions; `GpibReaderSim` takes explicit `um_per_pulse_x` / `um_per_pulse_y` since the axes aren't fixed.
- **`Free2DScanWindow`** (`free_2d_scan_app.py`) — full UI (channel pulldowns, Gaussian/Aperture(erf) fit toggle, settle time, colour map, log saving, right-click "Go to this position"). Designed to be subclassed into a fixed-axis app via constructor kwargs:
  - `default_ch_x` / `default_ch_y` — initial (and, if locked, permanent) channel selection.
  - `allow_channel_change` — when `False`, the "Channel Selection" group box is hidden and the combo boxes are disabled; the window behaves like a classic single-axis-pair scan app.
  - `log_key` — passed to `log_prefs.should_save()` / `get_app_dir()`, so a subclass can keep saving to its own `__localdata/<key>/` directory (e.g. `DacScanWindow` still uses `"dac_scan"`, not `"free_2d_scan"`).
  - `window_title` — overrides the default `"2D Scan"` title.
  - `DacScanWindow` (`apps/dac_scan/dac_scan_app.py`) is the reference example: `Free2DScanWindow(default_ch_x=4, default_ch_y=5, allow_channel_change=False, log_key="dac_scan", window_title="DAC Scan (Normal)")`. `CollimatorScanWindow` has not been migrated yet and remains a standalone implementation.
- **Why `apps/scan2d/` and not `apps/2d_scan/`** — a directory name starting with a digit breaks ordinary `from apps.2d_scan.x import y` import statements (`SyntaxError: invalid decimal literal`); confirmed during implementation. Do not rename it back.

### 1D Scan ([apps/scan1d/](apps/scan1d/))

Single-axis sibling of 2D Scan. The user picks **one** translation channel (`CHANNEL_CHOICES` = Ch1-Ch10; Ch11 rotation excluded for the same reason as scan2d), enters a **± range in µm** (half-width — one-sided, per user preference) and a grid-point count, and the scan steps `current ± range` while reading transmitted intensity. The profile is fit with a Gaussian or erf aperture model and the "Go to fitted center" button moves the channel to the fitted centre (button press, not automatic).

- **`Scan1DWorker`** — lives in `apps/scan2d/free_2d_scan_backend.py` (next to `Free2DScanWorker`, per the "extend scan2d backend" rule below), **not** in `apps/scan1d/`. It is the 1-D reduction of `Free2DScanWorker`'s inner scan line: same `+`-direction backlash approach (`BACKLASH_PULSES_X`), same clean-abort-on-exception contract, emits `point_measured(col, transmitted, incident)`.
- **`Scan1DScanWindow`** (`scan1d_app.py`) — dedicated single-plot UI (not a subclass of `Free2DScanWindow`, whose 2-D colour-map layout doesn't reduce cleanly to 1-D). Reuses the leaf components instead: `_PulseAxisItem` / `_MicronAxisItem` from `free_2d_scan_app`, `GpibReader` / `GpibReaderSim` from the scan2d backend (the 2-D simulator is sliced at `y = 0`, its peak line, for a clean 1-D profile), and the shared fit module below. Own single-channel move worker `_Move1DWorker`. Saves `.npz/.json/.png` under `log_key="scan1d"`.
- Registered in `main.py` (`open_scan1d`, launcher **button** "1D Scan" in the "Scan" section) and in `settings.log_prefs.APP_KEYS` + `settings/pages/logging_page.py`. The "Scan" section buttons are, in order: Collimator Scan, DAC Scan (Normal), DAC Scan (Rotation Centre), DAC Scan (XRD), **1D Scan**, **2D Scan** (last). Both `Scan1DScanWindow` and `Free2DScanWindow` are launcher buttons — they used to be Tools-menu items and were moved into the "Scan" section.

### Shared profile fitting ([utils/fitting/](utils/fitting/))

`utils/` is a top-level package for pure, Qt-free helpers shared across apps. `utils/fitting/` holds the 1-D profile fit maths that scan1d **and** scan2d both call, so the Gaussian / erf models live in exactly one place:

- `models.py` — `gaussian(x, A, x0, sigma, C)`, `aperture_model(x, A, x1, x2, w, bg)`.
- `profile_fit.py` — `fit_aperture_1d(x, profile)` and the high-level `fit_profile_1d(x, profile, model) -> ProfileFit | None`. `model` accepts the UI combo strings `"Gaussian"` / `"Aperture (erf)"`; `ProfileFit` carries `center`, `width` (+ `width_kind` label hint `"σ"`/`"width"`), `popt`, and a ready-to-plot `curve_x`/`curve_y` (already un-flipped for the aperture case).
- `Free2DScanWindow._run_fit` was refactored onto `fit_profile_1d`, collapsing its former per-axis Gaussian/erf duplication into one call per axis (the Y profile just plots with `curve_x`/`curve_y` swapped). The saved-JSON key names (`sigma_pulse` / `width_pulse`) and label formats are unchanged.
- The older scan apps (`xrd_scan`, `collimator_scan`, `dac_scan_rot`) still carry their own private copies of these fit helpers — they were left untouched to limit blast radius. Migrate them onto `utils.fitting` if you touch them.

### DAC Scan — GPIB intensity reader ([apps/dac_scan/](apps/dac_scan/))

`apps/dac_scan/dac_scan_backend.py` has been **deleted**. It used to hold both the Ch4/Ch5-fixed `DacScanWorker` / `GpibReader` / `GpibReaderSim` (removed once `DacScanWindow` switched to `Free2DScanWorker` / the `scan2d` `GpibReader`) and a set of axis constants (`CH_X`, `CH_Y`, `UM_PER_PULSE_CH4`, `UM_PER_PULSE_CH5`, `BACKLASH_PULSES_CH4`) that `apps/xrd_scan` still needed. Rather than keep the file alive as a bare constants module for a different app, those constants were inlined directly into `apps/xrd_scan/xrd_scan_backend.py` (channel numbers as local literals, µm/pulse scales read straight from `utils.control_stage.PULSE_SCALE`) — `xrd_scan` no longer depends on `apps.dac_scan` at all. Do not resurrect `dac_scan_backend.py`; extend `apps/scan2d/free_2d_scan_backend.py` instead if new scan apps need shared scan-worker logic.

If `gpib_reader=None` is passed to `DacScanWindow` (which forwards to `Free2DScanWindow._on_start`), a warning dialog blocks the scan from starting with the stub reader.

#### Keithley 2000 (`apps/dac_scan/keithley2000_reader.py`) — TEMPORARY SPECIFICATION

**Current status (as of 2026-06-21):** The Keithley 2000 at `GPIB0::2` is operated as a **photodiode-only reader** (transmitted X-ray intensity). The ion chamber (incident intensity) is not yet wired; `read_incident()` returns the constant `1.0`, so the scan plots raw transmitted current rather than a normalised ratio.

**Auto-detected GPIB mode** — `Keithley2000Reader.__init__` sends `*IDN?` and inspects the response:

| Response | Mode | Detection logic |
|----------|------|-----------------|
| Model string (e.g. `KEITHLEY INSTRUMENTS INC.,MODEL 2000,…`) | **Normal (SCPI)** | `float(idn)` raises `ValueError` |
| Numeric value (e.g. `+118.398E-3`) | **Talk-Only** | `float(idn)` succeeds |

**Normal (SCPI) mode** — instrument responds to commands:
- Init: `:FUNC "CURR:DC"`, `:CURR:DC:RANG:AUTO ON`, `:INIT:CONT ON`
- `read_transmitted()` → `:FETCH?`
- `close()` sends `:INIT:CONT OFF`

**Talk-Only mode** — instrument ignores all written commands and continuously outputs measurement values:
- `read_transmitted()` drains the stale buffer with a 100 ms timeout loop, then returns the freshest value
- No init commands are sent; `close()` skips `:INIT:CONT OFF`
- To exit Talk-Only from the front panel: `MENU → COMMUNICATION → GPIB → TALK-ONLY → DISABLE`

**Main window connection behaviour:**
- Talk-Only detected → status label shows `● Connected  (Talk-Only)` in **orange**
- Normal SCPI → status label shows `● Connected` in **green**
- Both modes proceed to set `self.keithley_reader` and enable the DAC Scan buttons

### IPA → poni coordinate mapping ([apps/ipa_poni/](apps/ipa_poni/))

Converts IPAnalyzer (IPA) `.prm` XML files to pyFAI `.poni` v2 format for azimuthal integration with pyFAI.

**IPA coordinate system** (origin = DirectSpot on detector):
- Z along beam, X = image right, Y = image down. Sample at (0, 0, −CL).
- Tilt: Rodrigues rotation by angle τ (tiltTau) around axis (cos φ, sin φ, 0) where φ = tiltPhi.
- `FootX`/`FootY`: foot of the perpendicular from sample to the tilted detector plane = pyFAI PONI.

**IPA units**: CameraLength in mm, PixSize in mm, wavelength in Å, tiltPhi/Tau in degrees.

**Parameter mapping**:

| pyFAI poni | IPA source | Formula |
|---|---|---|
| `Distance` | `CameraLength1`, `tiltTau` | `CameraLength1 × cos(τ) × 1e-3` m |
| `Poni1` | `FootY`, `pixSizeY` | `FootY × pixSizeY × 1e-3` m |
| `Poni2` | `FootX`, `pixSizeX` | `FootX × pixSizeX × 1e-3` m |
| `Rot1` | `tiltPhi`, `tiltTau` | `−arcsin(sin(τ)·sin(φ))` |
| `Rot2` | `tiltPhi`, `tiltTau` | `arcsin(−sin(τ)·cos(φ) / cos(Rot1))` |
| `Rot3` | — | `0` (in-plane rotation doesn't affect rings) |
| `PixelSize1` | `pixSizeY` | `pixSizeY × 1e-3` m |
| `PixelSize2` | `pixSizeX` | `pixSizeX × 1e-3` m |
| `Wavelength` | `waveLength` | `waveLength × 1e-10` m |

`pixKsi` (pixel skew angle) is not representable in poni format and is silently ignored. The output uses `Detector: Flat` with pixel sizes in `Detector_config`.

### Speed Controller ([apps/speed_controller/](apps/speed_controller/))

Tools-menu window (`SpeedControllerWindow`) for reading/writing the actual pps value of each channel's L/M/H speed register — Ch1–11 × L/M/H = 33 values, via the generalized `PM16CController.get_ch_speed_value(ch, level)` / `set_ch_speed_value(ch, level, pps)` (level is `"L"`/`"M"`/`"H"`; range 1–5,000,000 pps). `get_ch_lspd`/`set_ch_lspd` remain as thin backward-compatible wrappers around the `"L"` level (still used by `apps/dac_oscillation` for the rotation-speed save/restore around a scan).

- **Mandatory backup before any change**: on open, all 33 values are read in a background thread; the UI stays fully disabled until the user confirms a popup and picks a directory (no filename prompt) to save `speed_{YYYYMMDD_HHMMSS}.json`. Canceling the popup or the directory picker closes the window instead of leaving it in a half-ready state. The just-saved values are also kept in memory as the revert-on-close baseline.
- **Per-field Apply**: each Ch × L/M/H cell has its own current-value label, input spinbox, and Apply button; Apply is enabled only while the spinbox differs from the last known-good value for that cell, and disables again once a write + read-back round trip confirms the new value.
- **Close confirmation**: closing asks whether to revert all channels to the values captured at open time (Yes writes them back best-effort, no read-back retry); declining leaves whatever was applied during the session.
- **Load previous speed data**: loads a same-format JSON, validates it structurally (11 channels × L/M/H, integers in range) with no partial application on failure, then writes + reads back all 33 values with **one retry per field** on mismatch; failures after the retry are reported in a single summary dialog but don't block the other (independent) channels from applying.

### Channel assignments (BL-18C)

Pulse-to-physical-unit conversions are defined centrally in `PULSE_SCALE` in [utils/control_stage.py](utils/control_stage.py).

| Channel | Component | Scale |
|---------|-----------|-------|
| Ch1 | (X) | 1 µm/pulse |
| Ch2 | (Y) | 2 µm/pulse |
| Ch3 | Sample (X) [Focus] | 2 µm/pulse |
| Ch4 | Sample (Y) | 2 µm/pulse |
| Ch5 | Sample (Z) | 0.11 µm/pulse |
| Ch6 | Microscope positioning (Z) | 1 µm/pulse |
| Ch7 | Microscope positioning (X) | 0.2 µm/pulse |
| Ch8 | Microscope arm (Y, IN/OUT) | 1 µm/pulse — constrained vs Ch9 |
| Ch9 | Detector (IN/OUT) | 10 µm/pulse — constrained vs Ch8 |
| Ch10 | (Y, translation) | 2 µm/pulse |
| Ch11 | Rotation stage | 0.004 deg/pulse |

### PM16C command reference

The pulse motor stages are controlled by a PM16C-04XDL (https://www.tsuji-denshi.co.jp/product/lineup/maintenance/pm16c-04xdl/).
All commands are sent as ASCII with `\r\n` terminator. `x` is the channel string (1–9, A, B).

| Command | Description |
|---------|-------------|
| `ABSx±dddd` | Absolute move on channel x. Range: ±2,147,483,647. |
| `RELx±dddd` | Relative move on channel x. Same range. |
| `SSTPx` | Decelerate-stop channel x. |
| `ESTPx` | Emergency-stop (immediate) channel x. |
| `ASSTP` | Decelerate-stop all moving motors. |
| `AESTP` | Emergency-stop all motors (used by `emergency_stop()`). |
| `SPDHx` / `SPDMx` / `SPDLx` | Set speed to High / Medium / Low for channel x (selects which register the next move uses — does not change the register's pps value). |
| `SPD?x` | Read speed setting; response is `HSPD`, `MSPD`, or `LSPD`. |
| `SPDLxddd` / `SPDMxddd` / `SPDHxddd` | Set the LSPD/MSPD/HSPD register of channel x to ddd [pps] (pulses per second), range 1–5,000,000. |
| `SPDL?x` / `SPDM?x` / `SPDH?x` | Read the LSPD/MSPD/HSPD register value of channel x; response is the numeric pps value. |
| `STQ?` | Read REMOTE/LOCAL mode and number of idle motor slots (0–4). Response: `Rn` or `Ln`. A new move can be issued only when n > 0. |
| `STSx?` | Read position of channel x. Response: 6-char header + signed position value (e.g. `STSx: +1234`). `get_ch_pos` strips the first 6 chars. |
| `STS?` | Full status: mode, 4 selected channels, LS status, per-motor status bytes, and 4 current positions. Format: `R(L)abcd/PNNS/VVVV/HHJJKKLL/±pos1/±pos2/±pos3/±pos4`. `/SSSS/` in the response means all 4 selected motors are stopped. |
| `REM` | Switch to REMOTE mode (required before move commands). No response. |
| `LOC` | Switch to LOCAL mode. No response. |

**STS? per-motor status byte bits** (HH, JJ, KK, LL — 2 hex digits each):

| Bit | Meaning |
|-----|---------|
| b7 | ESEND — emergency-stop command received |
| b6 | SSEND — decelerate-stop command received |
| b5 | LSEND — limit-switch stop |
| b4 | COMERR — command error |
| b3 | ACCN — decelerating |
| b2 | ACCP — accelerating |
| b1 | DRIVE — outputting pulses |
| b0 | BUSY — processing command or driving |

## Related tools

### SpeMonitor & Pressure Calc ([../SpeMonitor_and_PressureCalc/app.py](../SpeMonitor_and_PressureCalc/app.py))

PyQt5 application for post-measurement wavelength calibration and ruby pressure calculation.

**Purpose**: WinSpec (the spectrometer acquisition software at BL-18C) does not save wavelength-axis calibration data into SPE files. When the spectrometer grating is repositioned to different center wavelengths during a session, each grating position requires a separate Ne-lamp calibration. This tool applies those calibrations after the fact and computes pressure from the ruby R1 fluorescence peak position.

**Calibration workflow**:
1. Take a Ne-lamp SPE file at a given center wavelength.
2. Run `speCalibrator.py` ("Make a calibration file" button) to fit Ne lines against literature values and save a wavelength array as a `.txt` file.
3. Register the `.txt` file in the **Calibration Registry** at the matching center wavelength (read automatically from the SPE header at offset 72, float32).
4. Load ruby SPE files — the app matches center wavelength **exactly** (±1 nm tolerance for float32 rounding) to the registry and applies the correct calibration automatically. If no matching calibration exists, the user is warned and prompted to calibrate.

**SPE 2.x binary format — relevant offsets**:
- Offset 72 (float32): center wavelength set in WinSpec — used for registry matching
- Offset 3000+: embedded calibration polynomial written by WinSpec's own calibration (if used) — treated as fallback only and flagged as potentially inaccurate, because WinSpec's built-in calibration is less rigorous than the Ne-lamp fit done by `speCalibrator.py`

## Rad-icon 2022 detector ([apps/Rad_icon_2022/](apps/Rad_icon_2022/))

### Architecture

```
Python (RadiconBackend) → radicon_dll.dll (C++/ctypes) → SapClassBasic86.dll (Sapera LT C++) → Xtium-CL MX4 frame grabber → Rad-icon 2022 (CameraLink)
```

The DLL source is in [apps/Rad_icon_2022/dll/radicon_dll.cpp](apps/Rad_icon_2022/dll/radicon_dll.cpp). Build with Visual Studio 2019, Release | x64.

### Two independent control channels

The Rad-icon 2022 is controlled via **two independent channels** that must both be used correctly:

1. **Sapera LT (CameraLink frame grabber)** — handles DMA transfer of pixel data from sensor → PC memory. Controlled via `SapAcquisition` / `SapAcqToBuf` / `SapBuffer` C++ classes.
2. **CameraLink serial port (COM2, 115200 baud)** — sends ASCII commands to the camera's internal controller for exposure, binning, and readout mode. This is a separate RS-232-over-CameraLink channel, NOT the Sapera API.

### Camera serial command protocol (COM2, 115200 baud, `\r` terminator)

Determined by reverse-engineering the commercial control software XFPCAP01.exe (ILSpy decompile; sources saved in [apps/Rad_icon_2022/__localdata/decompiled_XFPCAP01/](apps/Rad_icon_2022/__localdata/decompiled_XFPCAP01/)).

| Command | Meaning |
|---------|---------|
| `sbn 0\r` | Set binning: 1x1 (full resolution) |
| `sbn 1\r` | Set binning: 2x2 |
| `seu 0\r` | Unknown init command sent once at startup |
| `set <ms>\r` | Set exposure time in **milliseconds** (e.g. `set 1000\r` = 1 s) |

Camera replies `USER` (or a string containing "USER") when ready after serial open. Startup sequence must wait for this before issuing the first `Grab()`.

**Critical**: `CORACQ_PRM_TIME_INTEGRATE_DURATION` (Sapera API) does NOT control exposure for this camera. Exposure MUST be set via serial `set <ms>` command.

**CC1 triggering**: CC1 signals from the frame grabber are **ignored** by the Rad-icon 2022. The camera runs on its own internal FreeRun timer set by `set <ms>`. CamExpert Time Integration methods (Methods 1, 3, 5, 6, 8) and External Trigger mode on the frame grabber have no effect on when the camera starts or stops exposing. Confirmed by experiment (2026-06).

**Triggered acquisition** (`snap_triggered()` in `radicon_backend.py`): Because CC1 and `seu 0` mid-session do not reset the camera's integration timer, triggered mode is achieved by keeping the camera at a short idle exposure (`_IDLE_EXPOSURE_MS = 100 ms`) between snaps. On user trigger:
1. Send `set <real_ms>` — camera switches within at most one idle cycle (≤100 ms).
2. Any transition idle frame is discarded via a 300 ms snap attempt.
3. Clean real-exposure frame is returned.
4. `set 100` reverts the camera to idle for the next trigger.
Max latency from button press to exposure start: ≈100 ms.

**Image processing pipeline** (all in `radicon_ui.py`, UI layer only — `radicon_backend.py` returns raw frames):

```
raw frame (uint16)
  → _apply_flip()        # vertical/horizontal flip
  → _dark_correct()      # subtract dark image (float64 clip → uint16)
  → _defect_correct()    # bad-pixel median replacement
  → save TIFF / display
```

**Image flip** (`_apply_flip()`): Default: vertical flip only (`flip_v=True`, `flip_h=False`) matching the sensor read direction. Controlled by checkboxes in "検出器設定". Dark images must be acquired with the same flip settings as measurement images — changing flip mid-session requires re-acquiring dark.

### Pixel-defect correction

Implemented entirely in `radicon_ui.py` (no changes to backend or DLL). Three module-level helpers:

| Function | Role |
|---|---|
| `_parse_defect_file(path, binning, h_blank, width, height)` | Parse XFPCAP01 defect file → `set` of `(row, col)` in image coords |
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

All coordinates are in **1×1 sensor space** (width 2064 × height 2236). The file at `__localdata/XFPCAP01_defects/欠陥ファイル03.txt` is the production defect map shipped with the commercial XFPCAP01 software.

**Coordinate conversion to image space**:

| Binning | Column | Row |
|---|---|---|
| 1×1 | `col_sensor − h_blank` | `row_sensor` |
| 2×2 | `col_sensor // 2 − h_blank` | `row_sensor // 2` |

`h_blank = 4` (pixels cropped from each side of the raw frame width). Out-of-bounds results are silently discarded.

**Algorithm**: For each defect pixel `(r, c)`, replace with `median` of valid (non-defect) pixels in an N×N window (N = 3, 4, 5, or 6 — user selectable). All lookups use the **original** pixel values (`img`), not the in-progress `result`, so adjacent defects do not corrupt each other's replacement values.

**Notable defects in the production file**: tap-boundary column artefacts at regular 172-pixel intervals (86, 258, 430, …, 1979 for the lower half; 85, 257, …, 1978 for the upper half), the center column (1032), one partial row, and three isolated point defects.

**Auto-load**: on `RadiconWindow` startup, `_build_ui` tries the saved prefs path first, then falls back to the bundled XFPCAP01 defect file. File selection and kernel size are persisted in `radicon_ui_prefs.json` under keys `defect_file_path`, `defect_correct_enabled`, `defect_kernel_size`.

### Startup sequence (matches XFPCAP01.exe behaviour)

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

**Do NOT call `SoftwareTrigger()`** — the CCF (FreeRun mode) handles CC1 assertion automatically. Calling `SoftwareTrigger()` interferes and can cause timeouts.

### Sapera object configuration (correct settings)

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

### Production settings (from XFPCAP01.ini, as of 2026-06-18)

| Parameter | Value |
|-----------|-------|
| CCF | `T_Rad-icon_2022_Xtium_FullFOV_2x2_FreeRun.ccf` (2x2 binning) |
| Exposure | 1000 ms (`set 1000\r`) |
| Binning | 2x2 (`sbn 1\r`) |
| Sensor direction | 2 (flip None, image rotated 180° in software) |
| Ring buffer depth | 20 (`iMaxGrabBuf`) |
| Serial port | COM2, 115200 baud |
| Server name | `Xtium-CL_MX4_1` |

### Building the DLL

**Prerequisites**:
- Visual Studio 2019 (or 2022) with C++ workload — found by vswhere automatically
- Sapera LT SDK installed (headers/libs expected at `C:\Program Files\Teledyne DALSA\Sapera\Classes\Basic\`)

**Before building**: the DLL must NOT be loaded by any Python process. Close the interactive camera app or any script that calls `RadiconBackend`.

**Build command (PowerShell)**:

```powershell
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsPath = & $vswhere -version "[16.0,19.0)" -property installationPath 2>$null | Select-Object -First 1
$vcvars = "$vsPath\VC\Auxiliary\Build\vcvars64.bat"
$proj = 'D:\FPD-PC_User Data\Kagi\Hiroki_pyCodesLab\bl18c_controller\apps\Rad_icon_2022\dll\RadiconDll_2019.vcxproj'
cmd /c "call `"$vcvars`" && msbuild `"$proj`" /p:Configuration=Release /p:Platform=x64 /m /nologo /v:minimal" 2>&1
```

Output DLL: `apps/Rad_icon_2022/dll/Release/radicon_dll.dll`

The `build.bat` in the same directory does the same thing but cannot be called directly from PowerShell (use the command above instead, or run `cmd.exe /c build.bat` from a cmd prompt).

### Key files

| File | Purpose |
|------|---------|
| [apps/Rad_icon_2022/radicon_backend.py](apps/Rad_icon_2022/radicon_backend.py) | Python high-level API (ctypes wrapper) |
| [apps/Rad_icon_2022/radicon_ui.py](apps/Rad_icon_2022/radicon_ui.py) | PyQt6 UI — acquisition, dark correction, pixel-defect correction, display |
| [apps/Rad_icon_2022/dll/radicon_dll.cpp](apps/Rad_icon_2022/dll/radicon_dll.cpp) | C++ DLL — Sapera LT acquisition logic |
| [apps/Rad_icon_2022/dll/radicon_dll.h](apps/Rad_icon_2022/dll/radicon_dll.h) | C public API exported by the DLL |
| [apps/Rad_icon_2022/dll/RadiconDll_2019.vcxproj](apps/Rad_icon_2022/dll/RadiconDll_2019.vcxproj) | VS 2019 project file |
| [apps/Rad_icon_2022/dll/build.bat](apps/Rad_icon_2022/dll/build.bat) | Build script (run from cmd.exe, not PowerShell) |
| [apps/Rad_icon_2022/__localdata/XFPCAP01_defects/欠陥ファイル03.txt](apps/Rad_icon_2022/__localdata/XFPCAP01_defects/欠陥ファイル03.txt) | XFPCAP01 defect map (1×1 sensor coords; auto-loaded on startup) |
| [apps/Rad_icon_2022/__localdata/decompiled_XFPCAP01/](apps/Rad_icon_2022/__localdata/decompiled_XFPCAP01/) | ILSpy-decompiled source of the commercial XFPCAP01.exe |

## Experimental Scheduler ([apps/exp_scheduler/](apps/exp_scheduler/))

**全装置の操作をタイムライン形式で登録・実行する実験シーケンスシステム（開発中）。**
完全な設計仕様は [apps/exp_scheduler/SPEC.md](apps/exp_scheduler/SPEC.md) を参照すること。
`apps/exp_scheduler/` 以下の作業を始める前に必ずそのファイルを Read すること。

対象装置：Stage (PM16C) / PACE5000 / LakeShore 335 / Keithley 2000 / Rad-icon 2022。
入力モード：(1) UI からステップを追加、(2) Python サブセット DSL でスクリプト記述。
将来的に (2) をローカル LLM で自然言語から生成する機能を追加予定。

## Internationalization (i18n) ([settings/i18n.py](settings/i18n.py), [settings/i18n_catalog.py](settings/i18n_catalog.py))

The app supports English/Japanese UI switching. Call sites wrap English source strings in `tr("...")`; `settings/i18n_catalog.py` holds a single `JA` dict (English string → Japanese translation, one file, per-source-file comment sections) that `tr()` looks up when the active language is `"ja"` — a missing key silently falls back to English. **For the full architecture, the two call-site patterns (launcher vs. sub-app), the step-by-step procedure for adding `tr()` to a new file, and known edge cases (intentional bilingual mixing, dynamic f-strings), see the project-level skill `/i18n-integration` (`.claude/commands/i18n-integration.md`).**

- Sub-app windows evaluate `tr()` once at construction time only — no live language switching while already open (agreed with the user during `main.py` implementation; changing this requires user confirmation). Only `ModeSelectorLauncher` in `main.py` retranslates live via `i18n.signals.language_changed`.
- Sub-app translation is effectively complete. The two known exceptions: `apps/exp_scheduler/` (deferred until its `IMPLEMENTATION_PLAN.md` BUG list and UI spec stabilize — confirm with the user before starting) and `apps/sample_camera_viewer/` (unused per user decision — skip unless the user says otherwise). Use the `/i18n-integration` skill when tackling either.

## Key conventions

- **Platform target: Windows first.** The primary deployment environment is Windows. macOS is used for development. When a platform difference exists, choose the Windows-compatible approach. Cross-platform APIs are preferred; macOS-specific workarounds are acceptable only as fallbacks (never as the primary path).
- All sub-apps import `PM16CController` / `PM16CControllerSim` with a try/except fallback that manipulates `sys.path`, allowing both package-level and standalone execution.
- UI updates from background threads use `QtCore.QMetaObject.invokeMethod` or Qt signals — never direct widget calls from non-main threads.
- The PACE5000 app uses both Japanese and English in status strings; this is intentional.
- For all UI components related to choosing the directory or file paths to save a file, save the last used directory in __localdata and use it as a default value.
- As far as possible, use British spelling.
- **Spin/combo boxes never respond to mouse-wheel scrolling.** There is no scenario in this app where scrolling over a spin box or combo box while it happens to be under the cursor should change its value — it only causes accidental value changes when the user scrolls a panel/QScrollArea. Apply the `_no_wheel(widget)` helper (`widget.wheelEvent = lambda event: event.ignore()`) to every `QSpinBox`/`QDoubleSpinBox`/`QComboBox` at construction time. Existing examples: `apps/scan1d/scan1d_app.py`, `apps/calibrate_instruments/calibrate_instruments_app.py`, `apps/simple_stage_cont.py`. 