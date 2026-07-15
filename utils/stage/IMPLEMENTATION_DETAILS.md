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

All socket I/O is serialized by a single communication thread
(`CommandArbiter`, see "Motion ownership and the command arbiter" below) —
there is no `threading.Lock`/`RLock` on the controller any more. Every public
method that touches the wire enqueues a task and blocks on its `Future`
(`send_cmd`) or returns one directly (`request_normal_stop`,
`request_emergency_stop`, `recover_motion`).

## Known issues

**Standalone import resolution** (`apps/stage_simple_all/simple_stage_cont.py`, no import
fallback at all; `apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py`,
whose fallback `sys.path` insert is one `dirname()` short of the
`bl18c_controller` root) cannot resolve `utils.stage.control_stage` when run
directly (`python3 apps/.../*.py`) — only launching via `main.py` works for
these two. Confirmed present before the `control_stage`/`control_stage_sim`
→ `utils/` move too, so it isn't a regression from that move. Left unfixed
per user request (2026-07-05).

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
reply (manual p.2-3, 6-7) — all pass `has_response=False`. The stop
transaction sends `ASSTP`/`AESTP` immediately followed by `LOC` as ONE
arbiter task (see "Motion ownership and the command arbiter" below), so no
other command can land between them.

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

### Central state monitor and shared cache

[stage_monitor.py](stage_monitor.py) provides one `StageStateMonitor` per
controller instance. It polls all application channels (Ch1-Ch11) with
`STSx?`: once every five seconds while idle and once per second for a channel
with an active/expected move. A sweep never holds the communication lock
across all 11 channels; every `STSx?` is a separate transaction with a short
yield between channels.

Every valid `STSx?` response updates the same cache, including responses
requested by an app. The monitor checks each entry's observation time and
skips a channel whose cache is still fresh. Thus a UI already polling Ch9
does not cause a duplicate watchdog query for Ch9. Repeated failures use an
exponential backoff up to 5 seconds.

Non-blocking cache APIs are:
```python
controller.get_cached_ch_state(ch, max_age=None)
controller.get_cached_states([1, 2, 3], max_age=None)
controller.get_cached_is_moving()
```
`apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py` uses these methods from its
GUI-thread `QTimer`; PM16C timeouts can no longer freeze that window. Safety-
critical sequence transitions still use the direct status APIs.

`PM16CControllerSim` exposes the same cache API directly from its in-memory
simulation state and does not start another polling thread.

### Communication audit logging

`PM16CAuditLogger` in [stage_monitor.py](stage_monitor.py) is always enabled
for a real controller and writes JSON Lines beneath
`<details-log-base>/stage_audit/`. Persistent session logs contain connection
lifecycle, every non-`STS` command as one searchable `control_command`, async
`STOPx`, communication failures, five-minute all-channel snapshots, hourly
monitor-health summaries, motion completion, and unexplained position changes.
The source module is inferred by `_infer_source()`.

Normal `STSx?` sends/responses and commanded intermediate position changes are
not written to the session file. They are retained in a fixed-size in-memory
flight recorder for ten minutes. This keeps continuous all-channel monitoring
without producing an unbounded wire trace or hiding control commands in routine
status traffic.

The controller tracks motion at the `send_cmd()` wire boundary, not only in
`move_ch_absolute()`/`move_ch_relative()`. Raw `ABS`/`REL`/`JOG`/`SCAN`
(including `SCANHPx`/`SCANHNx`), `FDHPx`/`GTHPx`, and `PS` commands from the
development console are therefore attributable too. A
position change without a locally recorded command is logged as
`unexplained_position_change` at `CRITICAL` level and creates an incident
JSONL containing the preceding ten-minute flight-recorder history, all cached
channel states, and the following 60 seconds of trace. Incident collection is
performed by a background thread and is shortened cleanly if the application
shuts down during the post-trigger window.

Consecutive relative commands are accumulated against the pending expected
target under the monitor lock, falling back to the latest cached position only
when no pending target exists. Thus rapidly issued `REL` commands retain the
correct predicted final position even before the next `STSx?` observation.

`SSTPx`/`ESTPx` and all-channel `ASSTP`/`AESTP` do not immediately discard
motion attribution. Each affected channel enters a separate stop-confirmation
state, is polled immediately and then once per second, and remains attributable
until `STSx?` explicitly reports `S`. Position changes during deceleration are
persisted as `position_change_during_stop`, followed by `stop_confirmed` with
the final position and total post-request delta. If `S` is not confirmed within
30 seconds, `stop_not_confirmed` is logged at `CRITICAL`, the pending motion and
stop expectations are cleared, and an incident trace is written. A later move
command supersedes a pending stop and produces
`stop_superseded_by_motion_command`.

File I/O runs on a dedicated writer thread and never holds the PM16C socket
lock. Session files rotate at 10 MiB; closed rotated parts are gzip-compressed.
Ordinary session files are retained for 30 days with a 200 MiB total cap;
incident files are retained for 90 days with a 500 MiB total cap. The oldest
files are removed first when a cap is exceeded. A normal `STSx?` timeout or
malformed response is exceptional and is therefore still written to the
persistent session log.

Logger start, record acceptance, and stop-sentinel insertion share one
lifecycle lock. Every event accepted before `session_stop` is queued before the
writer sentinel; later events cannot be enqueued behind it.

The conventional `logging.getLogger("pm16c")` debug/info messages remain for
console diagnostics; they are separate from the persistent audit record.

### Timing-sensitive callers: `move_ch_relative_unchecked` / `set_ch_speed(stay_in_rem=...)`

`move_ch_relative()` does a position round-trip (`get_ch_pos`) plus
constraint checks before every move; `set_ch_speed()` switches back to LOC
when done. Both are the right default, but wrong for a tight loop that
deliberately avoids any extra latency between two operations (e.g. the
Rad-icon rotation scan firing a `REL` immediately after starting an exposure
so both finish together — see `apps/Rad_icon_2022/IMPLEMENTATION_DETAILS.md`).
For that case use `move_ch_relative_unchecked(ch, diff, motion=lease)` (no
round-trip, no constraint check — assumes the caller already validated the
move and is already in REM) and `set_ch_speed(ch, level, stay_in_rem=True,
motion=lease)` (skips the trailing `switch_to_loc()`). Lease validation for
the unchecked path is memory-only (`MotionCoordinator.validate()`, no I/O)
and the call returns immediately after the wire task is *enqueued* — it does
not wait for the send, preserving the latency guarantee. A send failure
surfaces via the audit log and `controller.last_async_error`, which the
caller's loop/`finally` should check.

### Motion ownership and the command arbiter

Multiple sub-apps share one `PM16CController` instance. Two cooperating
components in `utils/stage/` make that safe:

**`command_arbiter.py` — `CommandArbiter`.** One dedicated communication
thread owns the socket; every caller submits a `CommandTask` to a
`queue.PriorityQueue` and blocks on the returned `concurrent.futures.Future`
(or, for stops, gets the Future back immediately). Priorities:

| Priority | Class | Example |
|---:|---|---|
| 0 | `PRIORITY_EMERGENCY_STOP` | `AESTP` |
| 1 | `PRIORITY_NORMAL_STOP` | `ASSTP` |
| 2 | `PRIORITY_STOP_CONFIRM` | `STQ?` polled by the stop-confirmation thread |
| 3 | `PRIORITY_QUERY` | `STSx?`, `STQ?`, `SPD?x`, … |
| 4 | `PRIORITY_MOTION` | move/speed/mode-change transactions |

FIFO within a priority via a monotonic sequence counter. A task already
being executed is never preempted (single thread, one wire transaction at a
time); a stop enqueued after task T is guaranteed to run before any
lower-priority task that had not yet been dequeued when the stop was
submitted. Repeated stops coalesce onto one Future (`submit_stop`); an
emergency stop arriving while a normal stop is still queued supersedes it —
the normal stop's wire command is skipped and its Future resolves with the
emergency stop's outcome.

A task carrying a `MotionLease` is re-validated against the coordinator at
dequeue time (memory-only), so motion queued before a stop was requested
dies with `MotionRevokedError` instead of reaching the wire.

**`motion_coordinator.py` — `MotionCoordinator`.** Controller-wide (not
per-channel — REM/LOC, `ASSTP`/`AESTP`, `MOVE_CONSTRAINTS`, and the
4-concurrent-motor cap are all controller-global) ownership as a state
machine:

```
                 acquire()                    revoke_for_stop()
        FREE ───────────────▶ HELD ───────────────────────────▶ REVOKED_STOPPING
          ▲                     │                                      │
          │      release()      │                          note_stop_confirmed()
          │◀────────────────────┘                                      │
          │                                                            ▼
          │        grace period elapsed               owner_released?  REVOKED_STOPPED_GRACE
          │◀────── (lazy, on next acquire()) ─────────── no ───────────┘
          │                                              │ yes
          │◀─────────────────── release() ───────────────┘
          │
          │        force_recover_complete(True)
          └────────────────────────────────────────────── RECOVERY_REQUIRED
                                                             ▲
                                       note_stop_send_failed() / note_stop_confirm_failed()
```

- `MotionLease(controller_id, lease_id, generation, owner, operation)` is a
  frozen value handed back by `acquire_motion()`/`motion_session()`. A
  monotonically increasing `generation` counter (not a tombstone list) makes
  a reclaimed lease permanently invalid — a delayed `release_motion()` call
  from a worker that hasn't noticed the revocation yet is a safe no-op
  (`stale_motion_release_ignored` in the audit log), never a hazard for
  whoever holds the lease now.
- `acquire()` defaults to failing immediately (`MotionNotAvailableError` /
  `MotionRecoveryRequiredError`) rather than waiting — a UI button must fail
  fast, not fire minutes later.
- `revoke_for_stop()` invalidates the current lease in memory instantly, no
  matter how busy the comm thread is; the physical `ASSTP`/`AESTP` follows
  through the arbiter at top priority.
- HELD has **no TTL** — long scans/exposures are normal. Auto-reclaim only
  happens from `REVOKED_STOPPED_GRACE`, i.e. only after the stop was
  physically confirmed, and only after a grace period (default 5 s,
  `MotionCoordinator(grace_period_s=...)`) gives the original owner a chance
  to release cleanly in its own `finally`. A stop that could not be sent or
  confirmed leaves `RECOVERY_REQUIRED` — new motion is refused until an
  explicit `recover_motion()` (revoke → `AESTP` → confirm → bump generation
  → free) succeeds; see "PM16C Console" below for the operator-facing button.

**Public API** (identical on `PM16CController` and `PM16CControllerSim`):

```python
lease = controller.acquire_motion(owner, operation, timeout=None)
controller.release_motion(lease)                 # never raises
with controller.motion_session(owner, operation) as lease: ...
controller.is_motion_available()
controller.get_motion_holder()
future = controller.request_normal_stop(source=None)      # resolves at CONFIRMATION
future = controller.request_emergency_stop(source=None)
future = controller.recover_motion(source=...)
controller.get_stop_progress()   # "idle"|"queued"|"sent_confirming"|"confirmed"|"failed"
controller.shutdown()             # best-effort LOC + disconnect, no lease needed
```

`move_ch_absolute`/`move_ch_relative`/`move_ch_relative_unchecked`,
`set_ch_speed`/`set_ch_speed_value`/`set_ch_lspd`/`set_ch_backlash`,
`switch_to_rem`/`switch_to_loc`, and `wait_until_stop` (when
`stay_in_rem=False`) all require `motion=<lease>` — there is no
lease-optional fallback anywhere in `main`; omitting it raises
`MotionLeaseRequiredError`. `send_cmd()` applies the same policy by
classifying the raw command via `_command_metadata()`: queries need no
lease; `ASSTP`/`AESTP` typed into the raw console redirect into
`request_normal_stop`/`request_emergency_stop` rather than bypassing the
arbiter; `configuration`/`unknown` commands are refused while motion is
owned. This makes `apps/development/pm16c_console` unable to move a channel
without first clicking "Acquire Motion" in that window.

**Compound moves are single arbiter transactions.** `move_ch_absolute()` /
`move_ch_relative()` etc. build one closure that runs entirely on the comm
thread: read the constraint-relevant positions → check
`MOVE_CONSTRAINTS`/soft-limits/max-move in memory → re-validate the lease →
`REM` → re-validate → `ABS`/`REL`. A stop's instant in-memory revocation is
observed at the next re-validate, aborting the transaction *before* the
motion command reaches the wire — there is no window where a stop and a
queued move can both land on the controller.

**Stop-confirmation thread.** `request_normal_stop`/`request_emergency_stop`
enqueue one atomic stop transaction (`ASSTP`/`AESTP` immediately followed by
`LOC`, fixing the historical non-atomicity where another command could land
between them) and hand off to a dedicated daemon thread started in
`connect()`. That thread polls `STQ?` at `PRIORITY_STOP_CONFIRM` with a
100 ms sleep *between* polls on its own thread — never on the comm thread —
so ordinary UI position queries (`PRIORITY_QUERY`) keep flowing while a stop
is being confirmed. 4 consecutive `STQ?` readings of "all motors free" (or a
30 s timeout, `STOP_CONFIRM_TIMEOUT_S`) resolve the stop Future.

**Simulator parity.** `PM16CControllerSim` uses the exact same
`MotionCoordinator` class and exposes the identical lease API; there is no
socket/arbiter, so moves validate the lease inside a single `_state_lock`
critical section and execute immediately, and stops run on a short-lived
thread that halts all channels, waits for `not any(_moving)`, then calls
`note_stop_confirmed()`. `tests/test_sim_parity.py` runs the same scenario
script against both to keep this true.

**Caller convention.** A worker (QThread or `threading.Thread`) that drives
a whole scan/session acquires ONE lease at the start
(`with controller.motion_session(owner=..., operation=...) as motion:`) and
passes `motion=motion` to every stage call for the duration — see any of
`apps/scan2d/free_2d_scan_backend.py`, `apps/Rad_icon_2022/radicon_backend.py`
(`XrdOscillationWorker`), or `apps/exp_scheduler/runner.py`
(`SequenceRunner.run()`, one lease for the whole sequence including the
background sample-follow thread). GUI-thread sequenced apps
(`apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py`,
`apps/dac_oscillation/dac_oscillation_app.py`) acquire lazily on the first
move of a sequence and release at every terminal point (completion, abort,
error) via a small `_ensure_lease()`/`_release_lease()` pair. Stop/E-Stop
buttons everywhere use `request_normal_stop()`/`request_emergency_stop()` +
`utils/stage/qt_stop_watcher.StopProgressWatcher` (a `QTimer` polling
`future.done()` + `get_stop_progress()`) so a stop click never blocks the Qt
main thread on socket I/O — the historical up-to-2s (now, with confirmation,
up to tens of seconds) freeze is gone. `normal_stop()`/`emergency_stop()`
remain as synchronous wrappers over the request API, used only from
already-background worker threads (e.g. exception handlers, `closeEvent`
safety nets that already block on `worker.wait(...)`).

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
The table below is based on the bundled
[PM16C-04XDL remote-control manual](pm16c-04xdl_e_r17.pdf). All commands are
sent as ASCII with a `\r\n` terminator. `x` is a hexadecimal motor-channel
character (`0`–`9`, `A`–`F`); this application currently uses `1`–`9`, `A`
(Ch10), and `B` (Ch11). `None` in the **Response** column means that the
controller sends no command response; an unsolicited `STOPx` notification
may still arrive separately when LAN SRQ is armed.

| Command | Description | Response | Notes |
|---------|-------------|----------|-------|
| `REM` | Switch to REMOTE mode. | None | Required before commands marked REMOTE-only. Accepted only while all motors are stopped. |
| `LOC` | Switch to LOCAL mode. | None | Accepted only while all motors are stopped. |
| `ABSx±dddd` | Absolute move on channel x. | None | REMOTE-only. Range: ±2,147,483,647 pulses. |
| `ABSxB±dddd` / `ABSxS±dddd` | Absolute move with backlash compensation. | None | `B`: always compensate; `S`: compensate only when needed. A compensation position outside the moving range sets `BAD ABS COMMAND`. |
| `RELx±dddd` | Relative move on channel x. | None | REMOTE-only. Range: ±2,147,483,647 pulses. |
| `RELxB±dddd` / `RELxS±dddd` | Relative move with backlash compensation. | None | `B`: always compensate; `S`: compensate only when needed. |
| `JOGPx` / `JOGNx` | Move one pulse CW / CCW. | None | REMOTE-only. |
| `SCANPx` / `SCANNx` | Accelerating continuous scan CW / CCW. | None | REMOTE-only; runs until a stop or limit condition. |
| `CSCANPx` / `CSCANNx` | Constant-speed continuous scan CW / CCW. | None | REMOTE-only. |
| `SCANHPx` / `SCANHNx` | Scan CW / CCW and stop when the home-position switch is detected. | None | REMOTE-only; use a suitably low speed to avoid step-out at the sudden stop. |
| `SSTPx` / `ESTPx` | Decelerating / immediate stop of channel x. | None | REMOTE-only. |
| `ASSTP` / `AESTP` | Decelerating / immediate stop of all moving motors. | None | `AESTP` is used by `emergency_stop()`. |
| `SPDHx` / `SPDMx` / `SPDLx` | Select the High / Medium / Low speed register for channel x. | None | REMOTE-only. Selects the register used by subsequent moves; does not change its pps value. |
| `SPD?x` | Read the selected speed register. | `HSPD`, `MSPD`, or `LSPD` | Allowed in REMOTE or LOCAL mode. |
| `SPDHxddd` / `SPDMxddd` / `SPDLxddd` | Set a speed register in pulses per second. | None | Range: 1–5,000,000 pps. |
| `SPDH?x` / `SPDM?x` / `SPDL?x` | Read a speed-register value. | Decimal pps value | A motor that is busy may report `0000000`. |
| `SPDAL?` | Read the selected channels and their H/M/L speed-register values. | `abcd/Hddddddd/Mddddddd/Lddddddd/Hddddddd...` | `abcd` are the four display-channel mappings; busy-axis speed data may be `0000000`. See the manual for the complete repeated layout. |
| `RTExddd` | Set the acceleration/deceleration rate code. | None | REMOTE-only; `ddd` is 0–115 and indexes the manual's rate table. |
| `RTE?x` | Read the acceleration/deceleration rate code. | Three decimal digits (`ddd`) | Allowed in REMOTE or LOCAL mode. |
| `PSx±ddddddd` | Replace channel x's current pulse-position counter without moving the motor. | None | REMOTE-only. Use with extreme care: this deliberately changes the controller value without physical motion. |
| `PS?x` | Read channel x's current pulse-position counter. | Signed decimal, at least 7 digits | Values wider than 7 digits expand as needed. |
| `FLx±ddddddd` / `BLx±ddddddd` | Set the forward (CW) / backward (CCW) digital-limit position. | None | REMOTE-only; effective only when digital limits are enabled by `SETLSx...`. |
| `FL?x` / `BL?x` | Read the forward / backward digital-limit position. | Signed decimal, at least 7 digits | Allowed in REMOTE or LOCAL mode. |
| `SETLSxDYYY0yyy` | Configure digital-limit enable, HP/CCW/CW limit enables, and N.O./N.C. polarities. | None | REMOTE-only. `D` enables the digital limit; `YYY` and `yyy` are described in the manual. |
| `SETLS?x` | Read channel x's limit-switch configuration. | `DYYY0yyy` | Allowed in REMOTE or LOCAL mode. |
| `LS?` | Read channel mapping and limit/HOLD status for the four display channels. | `abcdHJKL` | `abcd` are channel numbers; `HJKL` are one-hex-digit status values. This covers only the four mapped channels. |
| `HDSTLS?` | Read hardware and software limit status for the four display channels. | `abcdHJKLhjkl` | `HJKL` are hardware-limit states and `hjkl` are software-limit states. |
| `SETMTxABCD` | Configure drive enable, HOLD behaviour, acceleration profile, and pulse-output mode. | None | REMOTE-only. These are low-level motor/driver settings; preserve the installed hardware configuration. |
| `SETMT?x` | Read channel x's motor/driver configuration. | `ABCD` | Allowed in REMOTE or LOCAL mode. |
| `FDHPx` | Run the automatic home-position search sequence. | None | REMOTE-only. Search directions and saved-home state come from `SETHPx...`. |
| `GTHPx` | Move to the saved home position. | None | REMOTE-only; requires valid previously detected home information. |
| `SETHPx0XYZ` | Set home-search state and directions. | None | REMOTE-only; see the manual before modifying these persistent parameters. |
| `SETHP?x` | Read home-search state and directions. | `0XYZ` | Example: `0100`. |
| `SHPx±ddddddd` | Force the saved home-position value. | None | REMOTE-only; normally set automatically by home detection. |
| `SHP?x` | Read the saved home-position value. | Signed decimal, or `NO H.P` | `NO H.P` means no valid home has been found. |
| `SHPFxdddd` | Set the home-position search offset. | None | REMOTE-only; range 0–9999. |
| `SHPF?x` | Read the home-position search offset. | Decimal offset | Up to four digits. |
| `HOLDxON` / `HOLDxOFF` | Disable / enable the external HOLD-OFF signal while stopped. | None | With `HOLDxOFF`, HOLD-OFF is asserted after the motor has been stopped for 500 ms. |
| `HOLD?x` | Read HOLD-OFF behaviour. | `ON` or `OFF` | Allowed in REMOTE or LOCAL mode. |
| `HOLDTMxddd` | Set the delay between releasing HOLD-OFF and starting the motor. | None | REMOTE-only; 50–500 ms in 10 ms increments. |
| `HOLDTM?x` | Read the HOLD-OFF release delay. | `dddms` | Example: `080ms`. |
| `STOPMDxAB` | Configure front-panel-button and limit-switch slow/immediate stopping. | None | REMOTE-only. **R17 is internally inconsistent:** its summary table says A=limit switch/B=panel button, while the detailed section says A=panel button/B=limit switch. Verify the installed firmware before writing this setting. |
| `STOPMDx?` / `STOPMD?x` | Read the configured front-panel/limit stop modes. | Two binary digits (`AB`) | R17 is also inconsistent about this query's spelling: the summary lists `STOPMDx?`, while the detailed section lists `STOPMD?x`. Verify on the installed firmware; factory data is `00`. Interpret A/B with the caveat above. |
| `SETCHabcd` | Map four motor channels to display/control positions A–D. | None | REMOTE-only. `-` leaves that display position unchanged, e.g. `SETCH01--`. Ignored if a target channel is busy. |
| `SETCH?` | Read the four display-channel mappings. | Four hexadecimal channel characters (`abcd`) | Example observed at BL-18C: `9345`. |
| `PAUSE ON` / `PAUSE OFF` | Enable / release paused (synchronised-start) operation. | None | REMOTE-only; mainly useful for synchronised multi-axis starts. |
| `PAUSE?` | Read paused-operation state. | `ON` or `OFF` | Allowed in REMOTE or LOCAL mode. |
| `STQ?` | Read REMOTE/LOCAL mode and number of idle motor slots. | `Rn` or `Ln`, `n=0`–`4` | A new move can start only when `n > 0`; `n == 4` means no channel is moving. Used by `is_all_motors_stopped()` / `wait_until_stop()`. |
| `STSx?` | Read detailed status for channel x. | `R(L)aPVHH±ddddddd` | `a` echoes x; `P/N/S` means CW/CCW/stopped; `V` is LS/HOLD status; `HH` is the motor-status byte. If x is not mapped to the LCD, `VHH` is `---`. The signed position contains 7–10 digits. Observed example: `L7S----0107000` = LOCAL, Ch7 stopped, status unavailable, position −107000. |
| `STS?` | Read detailed status for the four display-mapped channels. | `R(L)abcd/PNNS/VVVV/HHJJKKLL/±pos1/±pos2/±pos3/±pos4` | Covers only `abcd`, not all motors. Whole-controller stop confirmation must use `STQ?`. |
| `LN_SRQx1` / `LN_SRQx0` | Arm / clear the one-shot LAN stopped notification for channel x. | None | When armed, the controller sends unsolicited `STOPx` when x stops, then clears the flag. |
| `LN_SRQG0` | Clear all LAN stopped-notification flags. | None | Sent once by `connect()`. |
| `LN_SRQ?x` | Read one channel's LAN notification flag. | `1` or `0` | Allowed in REMOTE or LOCAL mode. |
| `LN_SRQ?G` | Read all LAN notification flags. | Four hexadecimal digits | Bit 15 corresponds to ChF; e.g. ChE+ChF gives `C000`. |
| `STOPx` | Unsolicited notification that channel x stopped. | Not a command response | Firmware V1.42+; filtered out of query responses by the communication layer. |
| `ERR?` | Read the highest-priority current error. | Error text such as `COMMAND ERROR`, `MCC06 BUSY ERROR`, or `BAD ABS COMMAND` | If several errors exist, the lowest error-flag bit has priority. |
| `ERRF?` | Read all error flags. | Two hexadecimal digits (`HH`) | b0: command error; b1: MCC06 busy; b2: bad absolute command. |
| `ERRC` / `ERRCx` | Clear all errors / one indexed error. | None | `x=0`: command; `1`: MCC06 busy; `2`: bad absolute command. |

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

## PM16C audit-log output

PM16C logs use UTF-8 JSON Lines (JSONL): one JSON object per line. Session
files are written below
`<details-log-base>/stage_audit/sessions/YYYY-MM-DD/`; incident files are
written below `<details-log-base>/stage_audit/incidents/`.

Ordinary session events have these common fields:

```json
{"schema":1,"timestamp":"2026-07-14T18:00:00.123+09:00","monotonic_ns":123456789,"session_id":"abc123","seq":42,"event":"control_command","level":"INFO","pid":1234,"thread":"MainThread","thread_id":5678}
```

Event-specific fields follow the common fields. `command_id` correlates a
command with its result, `operation_id`/`motion_operation_id` correlate a
move, and `stop_operation_id` correlates one stop command across channels.
Unavailable values are written as JSON `null`.

### Persistent session events

| Event | Main fields | Meaning |
|-------|-------------|---------|
| `session_start`, `session_stop` | controller metadata, `dropped_events` | Logger lifecycle. |
| `connect_attempt`, `connect_success`, `connect_failure`, `disconnect_start`, `disconnect_complete` | address, port, error | Controller connection lifecycle. |
| `control_command` | `command`, `command_class`, `channel`, `source`, `outcome`, `response`, `latency_ms` | Every non-`STS` command. `outcome` is `sent`, `success`, `send_failed`, `timeout`, or `invalid_response`. |
| `controller_notification` | `raw`, `classification`, `channel` | Unsolicited controller message such as `STOPx`. |
| `tx_failed`, `rx_timeout`, `rx_line` | `command`, `raw`, validation/error fields | Failed, timed-out, or malformed `STS` transaction. Normal `STS` traffic is not persisted. |
| `motion_complete`, `motion_not_started` | `channel`, `command`, `target`, final/observed position, `operation_id` | Locally commanded motion result. |
| `position_change_during_stop` | old/new position, `delta`, stop and motion IDs | Counter change after a stop request but before `S`; not treated as unexplained. |
| `stop_confirmed` | stop command/ID, requested/final position, `delta_after_stop_request`, `confirmation_latency_ms` | Channel explicitly reported `S`. |
| `stop_not_confirmed` | stop command/ID, last state/position, failures, timed-out channels | No `S` within 30 seconds; logged at `CRITICAL` and creates an incident. |
| `stop_superseded_by_motion_command` | old stop and new motion command/IDs | A new move replaced pending stop confirmation. |
| `unexplained_position_change` | `channel`, old/new position, `delta`, motion states | Counter changed without a local move or pending stop; logged at `CRITICAL` and creates an incident. |
| `monitor_query_failed` | `channel`, `error_type`, `error` | Background status query failed. |
| `position_snapshot` | `reason`, per-channel position/status map | All-channel baseline and five-minute snapshot. |
| `monitor_health_summary` | poll/success/failure counts, latency p50/p95/max | Hourly monitor-health aggregate. |
| `motion_acquired`, `motion_rejected`, `motion_revoked`, `motion_released` | `lease_id`, `generation`, `owner`, `operation`, holder info | `MotionCoordinator` lease lifecycle (see "Motion ownership and the command arbiter"). |
| `motion_stop_requested`, `motion_stop_sent`, `motion_stop_confirmed` | `stop_source`, `emergency`, `revoked_lease_id` | Stop-transaction lifecycle. |
| `motion_recovery_required`, `motion_recovery_started`, `motion_recovery_completed`, `motion_recovery_failed` | `source`, `reason` | Explicit `recover_motion()` lifecycle and `RECOVERY_REQUIRED` entry. |
| `stale_motion_release_ignored` | `lease_id`, `current_lease_id`/`current_generation`, `reason` | A `release_motion()` call that didn't match the current holder — safe no-op, logged for audit. |
| `stop_coalesced` | `kind`, `action` | A repeated/superseded stop request was merged onto an in-flight stop Future instead of sending a second wire command. |

### Flight recorder and incident files

Normal `STSx?` `tx_attempt`/`tx_sent`/`rx_line`, parsed
`position_observation`, `explained_position_change`, and
`motion_command_sent` events are held only in the fixed-size in-memory flight
recorder. They do not increase the normal session file.

On an unexplained position change or unconfirmed stop, the incident JSONL
contains the preceding ten minutes of flight-recorder events, an
`incident_snapshot` with all cached channel states, up to 60 seconds of
post-trigger events, and a final `incident_window_end` record. Incident file
names include the timestamp, channel, and triggering event name.
