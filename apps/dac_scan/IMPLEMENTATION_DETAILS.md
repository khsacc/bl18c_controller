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
µm/pulse scales read straight from `utils.control_stage.PULSE_SCALE`) —
`xrd_scan` no longer depends on `apps.dac_scan` at all.

**Do not resurrect `dac_scan_backend.py`**; extend
`apps/scan2d/free_2d_scan_backend.py` instead if new scan apps need shared
scan-worker logic.

If `gpib_reader=None` is passed to `DacScanWindow` (which forwards to
`Free2DScanWindow._on_start`), a warning dialog blocks the scan from starting
with the stub reader.

## Keithley 2000 (`apps/dac_scan/keithley2000_reader.py`) — TEMPORARY SPECIFICATION

**Current status (as of 2026-06-21):** The Keithley 2000 at `GPIB0::2` is
operated as a **photodiode-only reader** (transmitted X-ray intensity). The
ion chamber (incident intensity) is not yet wired; `read_incident()` returns
the constant `1.0`, so the scan plots raw transmitted current rather than a
normalised ratio.

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
