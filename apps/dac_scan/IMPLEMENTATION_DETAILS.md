# DAC Scan — GPIB intensity reader — implementation details

Developer-facing detail for `apps/dac_scan/`. See also
`apps/scan2d/IMPLEMENTATION_DETAILS.md` for the generic scan engine
`DacScanWindow` is built on.

## `dac_scan_backend.py` has been deleted

It used to hold both the Ch4/Ch5-fixed `DacScanWorker` / `GpibReader` /
`GpibReaderSim` (removed once `DacScanWindow` switched to `Free2DScanWorker` /
the `scan2d` `GpibReader`) and a set of axis constants (`CH_X`, `CH_Y`,
`UM_PER_PULSE_CH4`, `UM_PER_PULSE_CH5`, `BACKLASH_PULSES_CH4`) that
`apps/xrd_scan` still needed. Rather than keep the file alive as a bare
constants module for a different app, those constants were inlined directly
into `apps/xrd_scan/xrd_scan_backend.py` (channel numbers as local literals,
µm/pulse scales read straight from `utils.stage.control_stage.PULSE_SCALE`) —
`xrd_scan` no longer depends on `apps.dac_scan` at all.

**Do not resurrect `dac_scan_backend.py`**; extend
`apps/scan2d/free_2d_scan_backend.py` instead if new scan apps need shared
scan-worker logic.

If `gpib_reader=None` is passed to `DacScanWindow` (which forwards to
`Free2DScanWindow._on_start`), a warning dialog blocks the scan from starting
with the stub reader.

## Keithley 2000 (`utils/keithley2000_reader.py`)

The Keithley 2000 at `GPIB0::2` is a **photodiode-only reader** (transmitted
X-ray intensity) — `read_incident()` and the ion-chamber (incident
intensity) concept have been removed entirely (as of 2026-07-14). Development
(via the Development > Keithley Reader raw SCPI console) confirmed the
Model 2000 has no remote-switchable multi-input scanning — FRONT/REAR
terminal selection cannot be driven over SCPI — so a second reading is not
obtainable through this instrument. All scan apps plot raw transmitted
current directly; there is no normalised ratio.

**Auto-detected GPIB mode** — `Keithley2000Reader.__init__` sends `*IDN?` and
inspects the response:

| Response | Mode | Detection logic |
|----------|------|-----------------|
| Model string (e.g. `KEITHLEY INSTRUMENTS INC.,MODEL 2000,…`) | **Normal (SCPI)** | `float(idn)` raises `ValueError` |
| Numeric value (e.g. `+118.398E-3`) | **Talk-Only** | `float(idn)` succeeds |

**Normal (SCPI) mode** — instrument responds to commands:
- Init: `:FUNC "CURR:DC"`, `:CURR:DC:RANG:AUTO ON`, `:INIT:CONT ON`
- `read_transmitted()` → `:FETCH?`
- `close()` sends `:INIT:CONT OFF`

**Talk-Only mode** — instrument ignores all written commands and continuously
outputs measurement values:
- `read_transmitted()` drains the stale buffer with a 100 ms timeout loop,
  then returns the freshest value
- No init commands are sent; `close()` skips `:INIT:CONT OFF`
- To exit Talk-Only from the front panel: `MENU → COMMUNICATION → GPIB →
  TALK-ONLY → DISABLE`

**Main window connection behaviour:**
- Talk-Only detected → status label shows `● Connected  (Talk-Only)` in
  **orange**
- Normal SCPI → status label shows `● Connected` in **green**
- Both modes proceed to set `self.keithley_reader` and enable the DAC Scan
  buttons

## Rotation Centre: per-angle reanalysis

`dac_scan_rot_app.py` keeps raw Ch10 profiles in `_scan_data` and records
completed profiles separately in `_completed_thetas`. The table in the middle column
contains one row per angle with an inclusion checkbox, inclusive absolute Ch10
pulse bounds, fitted centre, and aperture-fit status. Each new scan resets all
rows to included with the full scan range. The common range controls only
change the rows when **Apply range to all angles** is pressed.
The window has three columns: parameters/status/results on the left, the table
and **Re-run Analysis** button in the middle, and the two plots on the right.
Horizontal splitter handles adjust the table/plot widths. The parameter panel
scrolls on smaller screens, with Emergency Stop fixed below the content.

Table edits invalidate the rotation fit and pending correction immediately.
Range edits also clear that angle's old aperture fit, centre line and legend
centre. **Re-run Analysis** refits completed profiles from the retained raw data,
using each row's bounds, then fits the rotation model using included, successful
centres. At least five finite profile points and three independent valid angles
are required. Invalid ranges and fit failures remain visible in the table;
incomplete profiles from an aborted scan never enter the fit. Excluded centres
appear as grey crosses, and an excluded zero-degree angle cannot supply a Ch10
centring target. Reanalysis never starts a hardware move.

Analysis controls are disabled while scan/correction/return workers run and
updated again on `QThread.finished`. Applying a correction cannot be re-enabled
by refitting the same scan after that correction has already been applied.
All analysis and widget updates remain on the GUI thread; motion workers and
controller ownership are unchanged.

With details saving enabled, explicit reanalysis overwrites the same scan's
JSON/NPZ/PNG files and preserves its completed/aborted outcome and raw arrays.
JSON adds `angle_analysis` (inclusion, completion, bounds, centre, status per
angle); successful `analysis_results` adds `included_theta_deg`. Failed rotation
analysis is saved as `analysis_results: null`. Existing NPZ keys are unchanged,
and its `aperture_centers` contains successful centres including excluded angles.
Save failures during reanalysis are reported in the status label.

Offline verification: `python -m unittest discover -s tests -p
test_dac_scan_rot_analysis.py -v` uses synthetic profiles and offscreen Qt,
without opening a hardware connection.

## Automatic shutter integration

The independent Collimator and Rotation Centre scan loops use the shared
`transmission_session` helper from `utils/beam_control.py`, as do DAC Normal
and generic 1D/2D Scan. Real Keithley and transmission simulation readers opt
in via `uses_transmitted_xrays=True`. Auto holds Out during the scan loop and
confirms In before successful return/terminal signals or during exception
unwinding. Manual does not send shutter movement commands. Reader failures and operator
abort follow the same cleanup scope. No stage protocol or constraints change.

In Manual mode with a launcher-provided shutter, all transmission workers check
live shutter status before scan movement. In or unconfirmed status refuses the
scan via `scan_could_not_start`, displaying a warning on the GUI thread. Manual
does not open the shutter; the operator must select Out or Auto Control and
restart. Standalone apps without a shared shutter retain their previous behaviour.
