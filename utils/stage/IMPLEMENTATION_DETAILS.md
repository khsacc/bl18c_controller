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

### Communication layer

`send_cmd()` keeps a persistent `self._recv_buffer` across calls (via a
private `_read_line()`), instead of starting from an empty buffer on every
call — a second line arriving in the same TCP segment as the first is no
longer silently discarded.

Since firmware V1.42 the controller can push an unsolicited `STOPx` line down
the LAN socket the instant channel `x` stops (see the manual,
`pm16c-04xdl_e_r17.pdf`, p.15). `send_cmd()`'s response loop filters any line
matching `^STOP[0-9A-Fa-f]$` out as an async notification and keeps reading —
it is never returned as the answer to whatever command was actually sent.
`connect()` sends `LN_SRQG0` to clear stale LAN-SRQ arm flags from a previous
client, but the filtering above stays in place regardless (another
client/interface on the unit could re-arm it at any time, and the manual is
ambiguous about whether `STOPx` over LAN requires arming at all).

Comms failures raise instead of returning `None`: `PM16CTimeoutError` on
socket timeout, `PM16CProtocolError` when a response fails validation
(wrong channel, unexpected token, malformed status — see `validate=` on
`send_cmd()` and the `_validate_*` functions), both subclassing
`PM16CCommError`. Existing callers mostly already wrap controller calls in
broad `except Exception`/`except ValueError`, so this surfaces as a clear
error instead of a silently-adopted stale/bogus value.

`ASSTP`/`AESTP`/`REM`/`LOC`/`ABSx`/`RELx`/`SPDHx`/`SPDMx`/`SPDLx` have no
reply (manual p.2-3, 6-7) — `normal_stop()`/`emergency_stop()` now correctly
pass `has_response=False` for `ASSTP`/`AESTP` (previously they didn't, so
every normal/emergency stop blocked for the full 2s socket timeout waiting
for a reply that never comes).

### Stop confirmation: whole-controller vs per-channel

`wait_until_stop()`/`get_is_moving()` are based on `STQ?`'s free-motor-slot
count (`is_all_motors_stopped()` / `get_free_motor_slots()`), not `STS?`'s
`PNNS` field. `STS?` only reports the 4 channels currently mapped to the
front-panel display window — a channel outside that window could keep moving
while the old `STS?`-based check reported "all stopped". `STQ?` reflects all
channels (max 4 can drive concurrently; free slots == 4 means none are
moving).

For confirming a *specific* channel finished (rather than "everything"),
use the channel-scoped API instead:
```python
controller.get_ch_is_moving(ch)                 # bool, from STSx?'s P/N/S state
controller.wait_ch_until_stop(ch, timeout=...)  # raises PM16CTimeoutError, never silently "stopped"
controller.wait_channels_until_stop([chx, chy], timeout=...)
```

### Soft limits / max move per command (optional, disabled by default)

`SOFT_LIMITS` (per-channel absolute `(min, max)` pulse range) and
`MAX_MOVE_PULSES` (per-channel max `|diff|` for a single relative move) are
both `{ch: None for ch in range(1, 12)}` — no mechanically-safe range has
been supplied for the real hardware yet, so both are fully inert until real
numbers are filled in. `check_soft_limits()`/`check_max_move()` follow the
same `(True, "")`/`(False, reason)` convention as `check_move_constraints()`
and are checked from `move_ch_absolute()`/`move_ch_relative()`, raising
`ValueError` the same way (UIs already catch that).

### Logging

`send_cmd()` and `move_ch_absolute()`/`move_ch_relative()` log to
`logging.getLogger("pm16c")` (`TX source=... command=...`, `RX raw=...`,
`MOVE source=... ch=... current=... target=...`). `source` is inferred from
the call stack (`_infer_source()`) rather than threaded through as a
parameter, so none of this module's ~15 calling files need to change to get
attribution. Attach a handler (e.g. in `main.py`) with
`logging.Formatter('%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S')`
to get console/file output — the logger has only a `NullHandler` by default,
so nothing is emitted until something configures one.

### Timing-sensitive callers: `move_ch_relative_unchecked` / `set_ch_speed(stay_in_rem=...)`

`move_ch_relative()` does a position round-trip (`get_ch_pos`) plus
constraint checks before every move; `set_ch_speed()` switches back to LOC
when done. Both are the right default, but wrong for a tight loop that
deliberately avoids any extra latency between two operations (e.g. the
Rad-icon rotation scan firing a `REL` immediately after starting an exposure
so both finish together — see `apps/Rad_icon_2022/IMPLEMENTATION_DETAILS.md`).
For that case use `move_ch_relative_unchecked(ch, diff)` (no round-trip, no
constraint check — assumes the caller already validated the move and is
already in REM) and `set_ch_speed(ch, level, stay_in_rem=True)` (skips the
trailing `switch_to_loc()`).

### Motion ownership — not implemented

Multiple sub-apps can share one `PM16CController` instance and move channels
from independent worker threads with no cross-app exclusivity beyond the
low-level socket lock. A motion-ownership lock (acquire before a scan starts,
release when it ends, emergency stop always bypasses it) was scoped out for
now — deferred until the user decides how broadly to wire it in.

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
| `STQ?` | Read REMOTE/LOCAL mode and number of idle motor slots (0–4). Response: `Rn` or `Ln`. A new move can be issued only when n > 0; n == 4 means no channel anywhere is moving — this is what `is_all_motors_stopped()`/`wait_until_stop()` poll. |
| `STSx?` | Read the detail status of channel x (available from firmware V1.47). Response: `R(L)aPVHH±digits` — `a` is the channel's own hex digit (echoed back, validated against the queried channel), `P`/`N`/`S` is cw/ccw/stopped, `V` is the LS/hold-off nibble, `HH` is the 2-hex-digit motor status byte, then the signed position. `get_ch_pos`/`get_ch_is_moving` use `_parse_stsx_reply()` to pull these apart. |
| `STS?` | Full status: mode, 4 selected channels, LS status, per-motor status bytes, and 4 current positions. Format: `R(L)abcd/PNNS/VVVV/HHJJKKLL/±pos1/±pos2/±pos3/±pos4`. **`abcd` are only the 4 channels currently mapped to the front-panel display window** — a channel outside that window doesn't appear here at all, which is why whole-controller stop confirmation uses `STQ?` instead (see above). |
| `REM` | Switch to REMOTE mode (required before move commands). No response. Only takes effect while all motors are stopped. |
| `LOC` | Switch to LOCAL mode. No response. Only takes effect while all motors are stopped. |
| `STOPx` | **Unsolicited**, pushed by the controller (firmware V1.42+) the instant channel x stops — not a reply to anything `send_cmd()` sent. Filtered out of the response stream automatically (see Communication layer above); x is a single hex digit `0`-`F`. |
| `LN_SRQG0` | Clears all channels' LAN-SRQ "stopped" arm flags. Sent once at `connect()`. No response. |

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
