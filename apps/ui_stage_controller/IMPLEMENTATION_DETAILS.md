# FPD/Scope stage controller — implementation details

Developer-facing detail for `apps/ui_stage_controller/fpd_scope_stg_controller_ui.py`
(`Bl18cStageControlApp`), the focused UI for BL-18C's key channels (6, 7, 8, 9).

## Live position polling — `ControllerPoller`

`ControllerPoller` (`QObject` + `QTimer`, 300 ms interval) runs on the GUI
thread and reads only `controller.get_cached_is_moving()` /
`get_cached_states()` — no socket I/O happens on this timer. The actual PM16C
communication is done by the controller-owned `StageStateMonitor`, so a
slow/timed-out PM16C reply cannot freeze this window. Emits
`positionChanged(channel, pulse)` and `movementStateChanged(bool)`, consumed
by the window to update the position readouts and the visualization widget.

## Stage visualization — `StageVisualizationView`

Custom-painted `QWidget` (`paintEvent`) that draws the detector (Ch9) and
microscope (Ch8) stages as two tracks converging on the beam position, scaled
from pulse counts to µm via `CH9_UM_PER_PULSE` / `CH8_UM_PER_PULSE` and the
`*_VIZ_IN_PULSE` / `*_VIZ_OUT_PULSE` constants. Purely a readout — it has no
effect on motion.

## Shortcut buttons — two-step sequenced moves

`shortcut_1()` (Det OUT → Mic IN) and `shortcut_2()` (Mic OUT → Det IN) exist
because `MOVE_CONSTRAINTS` (see
[utils/stage/IMPLEMENTATION_DETAILS.md](../../utils/stage/IMPLEMENTATION_DETAILS.md))
forbids moving one of Ch8/Ch9 IN while the other is still in the collision
zone — so the two channels must move in a specific order, not simultaneously.
Each shortcut:

1. Validates the requested step-1 target is on the correct side of
   `CH9_CH8_SAFE_BOUNDARY` (or `0` for Ch8), so that completing step 1
   guarantees step 2's constraint is already satisfied — the constraint
   itself is never bypassed, this just avoids a step 2 that would otherwise
   be rejected.
2. Fires the step-1 move; if the axis is already at the target position
   (`_move()` returns `None`), step 2 runs immediately instead of waiting on
   a move-completion callback that would never fire.
3. Otherwise waits for step-1 completion (`_start_sequence(...,
   verify_ch=..., verify_target=..., verify_speed=...)`) before firing step 2.

Both use `speed="H"`.
