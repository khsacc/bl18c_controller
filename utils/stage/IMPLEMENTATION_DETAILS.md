# Stage control implementation details

Covers [control_stage.py](control_stage.py) (`PM16CController`) and
[control_stage_sim.py](control_stage_sim.py) (`PM16CControllerSim`) — the
shared TCP client for the PM16C stepping-motor controller used by every
sub-app in this repo, and its drop-in hardware-free simulator.

## PM16CController

TCP socket client for the PM16C controller. Protocol: ASCII commands +
`\r\n` terminator. Key design detail: the controller must be switched to
**REM** (remote) mode before any move command, and back to **LOC** (local)
after. Move methods call `switch_to_rem()` automatically; `wait_until_stop()`
calls `switch_to_loc()` when done.

Channel encoding: channels 1–9 → `"1"`–`"9"`, channel 10 → `"A"`, channel 11
→ `"B"` (see `stringify_ch_numbers`).

### Inter-channel move constraints

`MOVE_CONSTRAINTS` at the top of [control_stage.py](control_stage.py) defines
safety rules evaluated before every absolute or relative move. Current rules
prevent the detector (Ch9) and microscope arm (Ch8) from colliding:

- Ch9 ≥ −30000 only allowed when Ch8 ≤ 0
- Ch8 ≥ 0 only allowed when Ch9 ≤ −30000

`check_move_constraints(ch, target_pos)` returns `(True, "")` or
`(False, reason)`. Move methods raise `ValueError(reason)` on violation — UIs
catch this and show a warning dialog.

## PM16CControllerSim

Background thread runs at ~100 Hz, incrementing channel positions toward
their targets. Applies the same `MOVE_CONSTRAINTS`. Initial positions match
BL-18C typical startup state. Speed steps per channel are defined in
`_SPEED_STEPS`.

## Channel assignments (BL-18C)

Pulse-to-physical-unit conversions are defined centrally in `PULSE_SCALE` in
[control_stage.py](control_stage.py).

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

## PM16C command reference

The pulse motor stages are controlled by a PM16C-04XDL
(https://www.tsuji-denshi.co.jp/product/lineup/maintenance/pm16c-04xdl/).
All commands are sent as ASCII with `\r\n` terminator. `x` is the channel
string (1–9, A, B).

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
