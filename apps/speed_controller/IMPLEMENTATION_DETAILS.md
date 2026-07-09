# Speed Controller — implementation details

Developer-facing detail for `apps/speed_controller/`. Reads/writes the actual
pps value of each channel's L/M/H speed register — Ch1–11 × L/M/H = 33
values, via the generalized `PM16CController.get_ch_speed_value(ch, level)` /
`set_ch_speed_value(ch, level, pps)` (level is `"L"`/`"M"`/`"H"`; range
1–5,000,000 pps). `get_ch_lspd`/`set_ch_lspd` remain as thin
backward-compatible wrappers around the `"L"` level (still used by
`apps/dac_oscillation` for the rotation-speed save/restore around a scan).

## Mandatory backup before any change

On open, all 33 values are read in a background thread; the UI stays fully
disabled until the user confirms a popup and picks a directory (no filename
prompt) to save `speed_{YYYYMMDD_HHMMSS}.json`. Canceling the popup or the
directory picker closes the window instead of leaving it in a half-ready
state. The just-saved values are also kept in memory as the revert-on-close
baseline.

## Per-field Apply

Each Ch × L/M/H cell has its own current-value label, input spinbox, and
Apply button; Apply is enabled only while the spinbox differs from the last
known-good value for that cell, and disables again once a write + read-back
round trip confirms the new value.

## Close confirmation

Closing asks whether to revert all channels to the values captured at open
time (Yes writes them back best-effort, no read-back retry); declining leaves
whatever was applied during the session.

## Load previous speed data

Loads a same-format JSON, validates it structurally (11 channels × L/M/H,
integers in range) with no partial application on failure, then writes +
reads back all 33 values with **one retry per field** on mismatch; failures
after the retry are reported in a single summary dialog but don't block the
other (independent) channels from applying.
