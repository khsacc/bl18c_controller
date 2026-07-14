# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PyQt6 desktop application for controlling hardware at synchrotron beamline BL-18C (Photon Factory, KEK). Controls a PM16C stepping-motor controller (TCP), a USB camera (for visual sample observations), Keithley multimeter (for reading the transmission x-ray intensities, via GPIB), Teledyne Rad-icon 2022 flat-panel detector, and optionally a Druck PACE5000 pressure controller (TCP/SCPI) and LakeShore335 Temperature controller. 

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
python apps/ui_stage_controller/fpd_scope_stg_controller_ui.py
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

**Known pre-existing bug**: `apps/simple_stage_cont.py` (no import fallback at all) and `apps/ui_stage_controller/fpd_scope_stg_controller_ui.py` (its fallback `sys.path` insert is one `dirname()` short of the `bl18c_controller` root) cannot resolve `utils.stage.control_stage` when run directly (`python3 apps/.../*.py`) — only launching via `main.py` works for these two. Confirmed present before the `control_stage`/`control_stage_sim` → `utils/` move too, so it isn't a regression from that move. Left unfixed per user request (2026-07-05).

### PM16CController / PM16CControllerSim ([utils/stage/](utils/stage/))

`PM16CController` ([utils/stage/control_stage.py](utils/stage/control_stage.py)) is the TCP socket client for the PM16C stepping-motor controller; `PM16CControllerSim` ([utils/stage/control_stage_sim.py](utils/stage/control_stage_sim.py)) is a drop-in, hardware-free simulator with the same public interface. Both enforce the same inter-channel `MOVE_CONSTRAINTS` (currently: detector Ch9 and microscope arm Ch8 cannot collide) before every move, raising `ValueError` on violation — UIs catch this and show a warning dialog. Full command-level protocol reference, channel encoding/`PULSE_SCALE` table, and the ASCII command set for the PM16C-04XDL are in [utils/stage/IMPLEMENTATION_DETAILS.md](utils/stage/IMPLEMENTATION_DETAILS.md) — read that file before touching the stage protocol or move constraints.

### Sub-applications

| App | File | Purpose |
|-----|------|---------|
| `Bl18cStageControlApp` | [apps/ui_stage_controller/fpd_scope_stg_controller_ui.py](apps/ui_stage_controller/fpd_scope_stg_controller_ui.py) | Focused UI for BL-18C key channels (6, 7, 8, 9). Includes stage visualization, shortcut buttons that sequence two moves in order to respect constraints, and a `ControllerPoller` (QTimer at 300 ms) for live position updates. |
| `StageControllerApp` | [apps/simple_stage_cont.py](apps/simple_stage_cont.py) | Raw control for all 11 channels. One `MotorControlWidget` per channel with absolute/relative move and speed selector. |
| `MainWindow` (Interactive Camera) | [apps/interactive_camera/interactive_camera.py](apps/interactive_camera/interactive_camera.py) | Live camera feed (OpenCV), click-to-move (Ch4/5), autofocus scan (Ch3 via Laplacian sharpness), snapshot/video recording, sample-tracking tab (template matching → XYZ correction on a configurable interval). Calibration data persisted to `apps/interactive_camera/calibration.json`. |
| `Pace5000Window` | [apps/PACE5000/pace5000_app.py](apps/PACE5000/pace5000_app.py) | Druck PACE5000 pressure monitor/controller. Connects via SCPI over TCP (default port 5025). Supports manual pressure/slew-rate control, CSV logging, and a scheduled sequence runner (`ScheduledControlRunner`). Standalone launcher is [apps/PACE5000/app.py](apps/PACE5000/app.py) — see that app's own README for why. |
| `DacScanWindow` | [apps/dac_scan/dac_scan_app.py](apps/dac_scan/dac_scan_app.py) | 2-D transmission scan over Ch4 (X) / Ch5 (Y). Reads photodiode current via `Keithley2000Reader` (injected from main window). Displays live colour map and runs a Gaussian fit on completion. Thin Ch4/Ch5-fixed subclass of `Free2DScanWindow`. See [apps/scan2d/IMPLEMENTATION_DETAILS.md](apps/scan2d/IMPLEMENTATION_DETAILS.md) and [apps/dac_scan/IMPLEMENTATION_DETAILS.md](apps/dac_scan/IMPLEMENTATION_DETAILS.md). |
| `DacScanRotWindow` | [apps/dac_scan/dac_scan_rot_app.py](apps/dac_scan/dac_scan_rot_app.py) | Same as above but also rotates Ch11 (rotation stage) at each row. Independent implementation — does not use `Free2DScanWindow` (Ch11 is a rotation axis, out of scope for the generic 2D-translation scanner). |
| `CollimatorScanWindow` | [apps/dac_scan/collimator_scan_app.py](apps/dac_scan/collimator_scan_app.py) | Scans the collimator axis (Ch1/Ch2). Still its own standalone implementation — has not been migrated to `Free2DScanWindow` yet. |
| `Free2DScanWindow` | [apps/scan2d/free_2d_scan_app.py](apps/scan2d/free_2d_scan_app.py) | "2D Scan" — generic version of DAC Scan where the user picks any two translation channels (Ch1-Ch10) via pulldowns instead of a fixed axis pair. See [apps/scan2d/IMPLEMENTATION_DETAILS.md](apps/scan2d/IMPLEMENTATION_DETAILS.md). |
| `Scan1DScanWindow` | [apps/scan1d/scan1d_app.py](apps/scan1d/scan1d_app.py) | "1D Scan" — single-axis counterpart of 2D Scan. User picks one translation channel (Ch1-Ch10), scans `current ± range` over a grid, fits the transmitted-intensity profile (Gaussian / erf), and can move the channel to the fitted centre. See [apps/scan2d/IMPLEMENTATION_DETAILS.md](apps/scan2d/IMPLEMENTATION_DETAILS.md) (the `Scan1DWorker` backend lives there too). |
| `IpaPoniDialog` | [apps/ipa_poni/ipa_poni_dialog.py](apps/ipa_poni/ipa_poni_dialog.py) | File-conversion dialog (no hardware). Converts IPAnalyzer `.prm` detector parameter files to pyFAI `.poni` format for use with azimuthal integration. Backend logic (pure Python, no Qt) is in [apps/ipa_poni/ipa_to_poni.py](apps/ipa_poni/ipa_to_poni.py). See [apps/ipa_poni/IMPLEMENTATION_DETAILS.md](apps/ipa_poni/IMPLEMENTATION_DETAILS.md) for the coordinate mapping. |
| `SpeedControllerWindow` | [apps/speed_controller/speed_controller_app.py](apps/speed_controller/speed_controller_app.py) | Tools-menu tool. Reads/writes the actual pps value of each channel's L/M/H speed register (Ch1–11 × L/M/H, via `PM16CController.get_ch_speed_value`/`set_ch_speed_value`). See [apps/speed_controller/IMPLEMENTATION_DETAILS.md](apps/speed_controller/IMPLEMENTATION_DETAILS.md). |
| `XrdScanWindow` | [apps/xrd_scan/xrd_scan_app.py](apps/xrd_scan/xrd_scan_app.py) | DAC Scan (XRD) — Ch4/Ch5 grid scan using Rad-icon 2022 images instead of GPIB photodiode. Performs pyFAI in-memory azimuthal integration at each grid point; maps user-defined 2θ ROI intensities. Multiple ROIs supported; combobox switches displayed map and triggers immediate refit. Enabled in main window when Rad-icon 2022 is connected. See [apps/xrd_scan/IMPLEMENTATION_DETAILS.md](apps/xrd_scan/IMPLEMENTATION_DETAILS.md). |
| `KeithleyReaderWindow` | [apps/development/keithley_reader/keithley_reader_app.py](apps/development/keithley_reader/keithley_reader_app.py) | Development-menu tool. Reads the shared `Keithley2000Reader` on demand (`read_transmitted()`) and includes a raw SCPI console — used to confirm the Model 2000 has no remote-switchable multi-input scanning, so a second (incident/ion-chamber) reading isn't obtainable and `read_incident()` was removed entirely (2026-07-14). |
| `Pm16cConsoleWindow` | [apps/development/pm16c_console/pm16c_console_app.py](apps/development/pm16c_console/pm16c_console_app.py) | Development-menu tool. Sends a raw ASCII command typed by the user straight to the shared `PM16CController`/`Sim` connection via `send_cmd()` and displays the reply, or "No response" if the socket read times out (the controller's existing 2.0 s timeout, shared by every window on that connection). Bypasses `MOVE_CONSTRAINTS` and per-channel speed/move limits entirely. Before first opening it shows a developer-only warning and requires the exact answer `STS4?` to a basic protocol quiz; a permanent warning label remains in the console UI (2026-07-14). |

Developer-facing implementation detail for the scan/conversion apps above
(module layout, design rationale, protocol specs) lives in a co-located
`IMPLEMENTATION_DETAILS.md` per app rather than inline here — read the linked
file before making non-trivial changes in that app. For pyFAI/poni
conventions shared between `xrd_scan` and `ipa_poni`, see the project skill
`/pyfai-integration` (`.claude/commands/pyfai-integration.md`).

### Channel assignments (BL-18C)

11 channels: Ch1/Ch2/Ch10 (translation), Ch3/Ch4/Ch5 (sample X/Y/Z), Ch6/Ch7 (microscope positioning), Ch8 (microscope arm, IN/OUT), Ch9 (detector, IN/OUT — constrained vs Ch8), Ch11 (rotation stage). Per-channel µm-or-deg/pulse scale (`PULSE_SCALE`) and the full PM16C ASCII command set (moves, speed registers, status queries, `STS?` status-byte bit meanings) are in [utils/stage/IMPLEMENTATION_DETAILS.md](utils/stage/IMPLEMENTATION_DETAILS.md).

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

```
Python (RadiconBackend) → radicon_dll.dll (C++/ctypes) → SapClassBasic86.dll (Sapera LT C++) → Xtium-CL MX4 frame grabber → Rad-icon 2022 (CameraLink)
```

Controlled via **two independent channels**: Sapera LT (DMA pixel transfer)
and a separate CameraLink serial port (COM2, 115200 baud) for exposure/binning
ASCII commands — these are not interchangeable. **Critical**: exposure MUST
be set via the serial `set <ms>\r` command; the Sapera
`CORACQ_PRM_TIME_INTEGRATE_DURATION` API does NOT control it, and CC1
triggering from the frame grabber is ignored by this camera (confirmed by
experiment, 2026-06). Do NOT call `SoftwareTrigger()` — FreeRun mode handles
CC1 assertion automatically.

Full protocol tables, the triggered-acquisition design
(`snap_triggered()`), the image-processing pipeline, pixel-defect correction
algorithm, startup sequence, Sapera object configuration, production
settings, and DLL build instructions are in
[apps/Rad_icon_2022/IMPLEMENTATION_DETAILS.md](apps/Rad_icon_2022/IMPLEMENTATION_DETAILS.md)
— read that file before touching the backend, DLL, or UI image pipeline.

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
- **`apps/development/` is exempt from i18n entirely** — apps under the menu-bar **Development** menu (agreed with the user 2026-07-14) are English-only and must not use `tr()`/`settings.i18n`. This menu is for developer/diagnostic tools (e.g. `KeithleyReaderWindow`, `Pm16cConsoleWindow`), not end-user features, so translating it is not worth the upkeep. New Development tools go in their own subfolder under `apps/development/`.
- **The Development menu is for developers who know both this codebase and the BL-18C beamline well — not for general beamline users.** Its tools may skip the safety/UX guardrails used elsewhere. `Pm16cConsoleWindow`, in particular, sends raw ASCII commands straight to the PM16C and bypasses `MOVE_CONSTRAINTS` and speed/move limits; it therefore has a mandatory warning-and-protocol-quiz gate before first opening. Once admitted, commands remain direct and unmodified (agreed with the user, 2026-07-14).

## Key conventions

- **Platform target: Windows first.** The primary deployment environment is Windows. macOS is used for development. When a platform difference exists, choose the Windows-compatible approach. Cross-platform APIs are preferred; macOS-specific workarounds are acceptable only as fallbacks (never as the primary path).
- All sub-apps import `PM16CController` / `PM16CControllerSim` with a try/except fallback that manipulates `sys.path`, allowing both package-level and standalone execution.
- UI updates from background threads use `QtCore.QMetaObject.invokeMethod` or Qt signals — never direct widget calls from non-main threads.
- The PACE5000 app uses both Japanese and English in status strings; this is intentional.
- For all UI components related to choosing the directory or file paths to save a file, save the last used directory in __localdata and use it as a default value.
- As far as possible, use British spelling.
- **Spin/combo boxes never respond to mouse-wheel scrolling.** There is no scenario in this app where scrolling over a spin box or combo box while it happens to be under the cursor should change its value — it only causes accidental value changes when the user scrolls a panel/QScrollArea. Apply the `_no_wheel(widget)` helper (`widget.wheelEvent = lambda event: event.ignore()`) to every `QSpinBox`/`QDoubleSpinBox`/`QComboBox` at construction time. Existing examples: `apps/scan1d/scan1d_app.py`, `apps/calibrate_instruments/calibrate_instruments_app.py`, `apps/simple_stage_cont.py`.
