# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PyQt6 desktop application for controlling hardware at synchrotron beamline BL-18C (Photon Factory, KEK): PM16C stepping-motor controller (TCP), USB microscope camera, Keithley multimeter (GPIB, transmitted X-ray intensity), Teledyne Rad-icon 2022 flat-panel detector, and optionally Druck PACE5000 (pressure, TCP/SCPI) and LakeShore 335 (temperature, GPIB).

Python 3.13 ([.python-version](.python-version)). Dependencies: `PyQt6`, `opencv-python`, `numpy`; optional `slack_sdk`.

```bash
python main.py           # normal mode — requires hardware (PM16C at 192.168.1.55:7777)
python main.py --debug   # simulation mode — no hardware; never contacts STARS or Slack
```

Most sub-apps also run standalone (`python apps/<app>/<file>.py`) and then create their own controller. `apps/stage_simple_all/simple_stage_cont.py` and `apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py` cannot run standalone (import bug, left unfixed by request — see [utils/stage/IMPLEMENTATION_DETAILS.md#known-issues](utils/stage/IMPLEMENTATION_DETAILS.md#known-issues)).

## Architecture

### Launcher and shared resources

[main.py](main.py) hosts `ModeSelectorLauncher`. It owns the shared hardware/service objects and passes them to sub-app windows; on close it calls `controller.switch_to_loc()` then `controller.disconnect()`. Shared objects it owns:

- `PM16CController` (or `PM16CControllerSim` with `--debug`).
- `CameraSession(0)` for the USB microscope, shared by Interactive Camera and the Experimental Scheduler — see [apps/interactive_camera/IMPLEMENTATION_DETAILS.md](apps/interactive_camera/IMPLEMENTATION_DETAILS.md).
- PACE5000 / LakeShore 335 backends, including their local read-only JSON endpoints (ports 8766 / 8767; see each submodule's README).
- `PdiService`, `SlackService`, and one STARS connection (below).

**Injection pattern:** every sub-app window accepts optional `controller=` (likewise `pdi_service=` etc.). When injected, the window sets `self._owns_controller = False` and must not disconnect it on close; when omitted, it creates and owns its own. `PM16CControllerSim` has the exact same public interface.

### PM16C stage ([utils/stage/](utils/stage/))

[control_stage.py](utils/stage/control_stage.py) (TCP client) and [control_stage_sim.py](utils/stage/control_stage_sim.py) (simulator) both enforce `MOVE_CONSTRAINTS` before every move (Ch9 detector vs Ch8 microscope arm; Ch8 vs Ch11 rotation stage), raising `ValueError` — UIs catch it and show a warning.

Channels: Ch1/Ch2/Ch10 translation, Ch3/Ch4/Ch5 sample X/Y/Z, Ch6/Ch7 microscope positioning, Ch8 microscope arm (IN/OUT), Ch9 detector (IN/OUT), Ch11 rotation.

**Read [utils/stage/IMPLEMENTATION_DETAILS.md](utils/stage/IMPLEMENTATION_DETAILS.md) before touching the stage protocol, `PULSE_SCALE` or move constraints.**

### Sub-applications

Read the linked `IMPLEMENTATION_DETAILS.md` before making non-trivial changes in an app.

| App | File | Notes |
|-----|------|-------|
| `Bl18cStageControlApp` | [apps/stage_fpd_scope/](apps/stage_fpd_scope/) | Ch6–9 focused UI with constrained move shortcuts |
| `StageControllerApp` | [apps/stage_simple_all/](apps/stage_simple_all/) | Raw control of all 11 channels |
| `MainWindow` (Interactive Camera) | [apps/interactive_camera/](apps/interactive_camera/) | Live view, click-to-move, autofocus, drift tracking |
| `Pace5000Window` | [apps/PACE5000/](apps/PACE5000/) | **Git submodule** ([khsacc/PaceMaker](https://github.com/khsacc/PaceMaker)) — changes belong to that repo |
| `LakeShore335Window` | [apps/LakeShore335/](apps/LakeShore335/) | **Git submodule** ([khsacc/LS335](https://github.com/khsacc/LS335)) — changes belong to that repo |
| `Free2DScanWindow` | [apps/scan2d/](apps/scan2d/) | Generic 2D transmission scan (any two of Ch1–10) |
| `DacScanWindow` | [apps/dac_scan/dac_scan_app.py](apps/dac_scan/dac_scan_app.py) | Ch4/Ch5 subclass of `Free2DScanWindow` |
| `DacScanRotWindow` | [apps/dac_scan/dac_scan_rot_app.py](apps/dac_scan/dac_scan_rot_app.py) | Adds Ch11 rotation; independent implementation |
| `CollimatorScanWindow` | [apps/dac_scan/collimator_scan_app.py](apps/dac_scan/collimator_scan_app.py) | Ch1/Ch2; not yet migrated to `Free2DScanWindow` |
| `Scan1DScanWindow` | [apps/scan1d/](apps/scan1d/) | 1D scan + fit; worker lives in `apps/scan2d/` |
| `RubyFinderWindow` | [apps/ruby_finder/](apps/ruby_finder/) | Ch4/Ch5 ruby-fluorescence map via FluoRaPressée HTTP API |
| `XrdScanWindow` | [apps/xrd_scan/](apps/xrd_scan/) | Ch4/Ch5 grid scan with Rad-icon images + pyFAI ROI maps |
| `IpaPoniDialog` | [apps/ipa_poni/](apps/ipa_poni/) | IPAnalyzer `.prm` → pyFAI `.poni` (no hardware) |
| `SpeedControllerWindow` | [apps/speed_controller/](apps/speed_controller/) | L/M/H speed registers per channel |
| `KeithleyReaderWindow`, `Pm16cConsoleWindow` | [apps/development/](apps/development/) | Dev tools. The PM16C console **bypasses `MOVE_CONSTRAINTS`** |

For pyFAI/poni conventions use the `/pyfai-integration` skill.

### Rad-icon 2022 detector ([apps/Rad_icon_2022/](apps/Rad_icon_2022/))

Python → `radicon_dll.dll` (ctypes) → Sapera LT → Xtium-CL MX4 frame grabber → detector. Pixels come via Sapera; **exposure/binning MUST be set via the separate CameraLink serial port (COM2), not the Sapera API**, and CC1 triggering is ignored. Read [apps/Rad_icon_2022/IMPLEMENTATION_DETAILS.md](apps/Rad_icon_2022/IMPLEMENTATION_DETAILS.md) before touching the backend, DLL or image pipeline.

### PDIndexer bridge ([utils/pdindexer/](utils/pdindexer/))

Sends pyFAI 1D patterns into PDIndexer (clipboard via `PdiSender.exe` MemoryPack helper, or legacy `.pdi` file). The byte format is unforgiving and PDIndexer fails silently. **Read [utils/pdindexer/IMPLEMENTATION_DETAILS.md](utils/pdindexer/IMPLEMENTATION_DETAILS.md) and [docs/PLAN_PDINDEXER_BRIDGE.md](docs/PLAN_PDINDEXER_BRIDGE.md) before touching it.** Run `python tools/check_pdindexer_schema.py` before a beamtime.

### STARS (X-ray stopper and ring current)

[utils/shutter.py](utils/shutter.py) (stopper, ring current) and [utils/ring_bridge.py](utils/ring_bridge.py) (on Windows normal mode, keeps the local `ringEPF.exe` bridge from `__localdata/ring/` running). Port 6057 is fixed; IP and keyword are set in **Settings → STARS connection**. The keyword is stored in Git-ignored `settings/__localdata/` — never put it in source, sequence files or logs.

### Experimental Scheduler ([apps/exp_scheduler/](apps/exp_scheduler/))

Sequential experiment runner controlling all instruments (stage, PACE5000, LakeShore 335, Rad-icon, FluoRaPressée, stopper). Steps are added from the UI or written as a Python-subset DSL. See [apps/exp_scheduler/SPEC.md](apps/exp_scheduler/SPEC.md).

Optional Slack notifications ([utils/slack_notify/](utils/slack_notify/)): **Slack failures must never affect a run** — read [utils/slack_notify/IMPLEMENTATION_DETAILS.md](utils/slack_notify/IMPLEMENTATION_DETAILS.md) before changing it.

## Online manual ([docs-site/](docs-site/))

Docusaurus site (Japanese) deployed to GitHub Pages on push to `main`. Verify with `npm run typecheck && npm run build` in `docs-site/` (broken links fail the build). See [docs-site/README.md](docs-site/README.md).

## Key conventions

- **Windows first.** macOS is for development only; prefer cross-platform APIs, with macOS workarounds only as fallbacks.
- UI is **English-only**. i18n is retired: `tr()` returns its argument unchanged; new code uses plain strings without `tr()`.
- Sub-apps import `PM16CController` / `PM16CControllerSim` with a try/except `sys.path` fallback so they work both as packages and standalone.
- UI updates from background threads go through Qt signals or `QMetaObject.invokeMethod` — never direct widget calls.
- PACE5000 status strings mix Japanese and English intentionally.
- File/directory pickers remember the last used directory in `__localdata` and use it as the default.
- Use British spelling where possible.
- Apply `_no_wheel(widget)` (`widget.wheelEvent = lambda event: event.ignore()`) to every `QSpinBox`/`QDoubleSpinBox`/`QComboBox` at construction, to prevent accidental changes while scrolling. Examples: `apps/scan1d/scan1d_app.py`, `apps/stage_simple_all/simple_stage_cont.py`.
- **When you change a specification (behaviour, UI, settings, protocol), also update the corresponding page of the online manual in [docs-site/docs/](docs-site/docs/) in the same change.** Do not update `docs/` (to be removed).
- Do not commit; let the user do so.
