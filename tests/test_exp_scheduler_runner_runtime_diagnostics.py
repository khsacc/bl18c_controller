"""
SequenceRunner runtime-diagnostic tests — apps/exp_scheduler
REORGANISATION_PLAN.md Phase 9.

Exercises SequenceRunner.run() synchronously (calling .run() directly rather
than .start(), so no real QThread/event loop is needed) against
tests/exp_scheduler_fakes.py::FakeStageController, to pin down the new
runtime-layer safety behaviour added in Phase 9:

  - MOVE_CONSTRAINTS is now checked before every ordinary stage move, not
    only before Ch11 oscillation (previously the runtime layer was a no-op
    for ordinary moves; only the controller's own internal enforcement
    caught a violation).
  - A relative move whose current position can't be read blocks the move
    (fail-closed) instead of silently skipping the safety pre-check.
  - Global limits / MOVE_CONSTRAINTS / motion-lease / oscillation failures
    each carry a stable Diagnostic `code`, readable off
    SequenceRunner._last_diagnostic and (for the classified cases)
    surfaced identically whether the failure originates on the main run
    thread or (Global limits only) the background follow thread.
  - The Ch11 oscillation background thread's own exceptions are no longer
    swallowed; a stuck oscillation thread is never silently treated as a
    successful step.

Ch11-oscillation-thread-timeout/stop-failure scenarios monkeypatch
threading.Thread narrowly around the run() call under test, rather than
waiting out the real 30s/5s join()s.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )

import cv2
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from apps.exp_scheduler.actions import (
    LogAction, StageAction, StartFollowingAction, TakeXrdAction, WaitAction,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import RunnerError, SequenceRunner, _StopRequested
from apps.exp_scheduler.scheduler_settings import GlobalLimits
from apps.exp_scheduler.sequence import Sequence
from utils.stage.control_stage import MotionRevokedError

from tests.exp_scheduler_fakes import FakeStageController


class _FakeRadicon:
    """Minimal stand-in for apps/Rad_icon_2022/radicon_backend.py —
    just enough for _do_take_xrd() with action.save=False (so backend.width/
    height, only read on the save=True path, are never needed)."""

    def __init__(self, frame: np.ndarray | None = None, fail_on_snap: bool = False) -> None:
        self._frame = frame if frame is not None else np.zeros((4, 4), dtype=np.uint16)
        self.fail_on_snap = fail_on_snap
        self.calls: list[tuple] = []

    def set_exposure_ms(self, ms) -> None:
        self.calls.append(("set_exposure_ms", ms))

    def snap_triggered(self, timeout_ms=None):
        self.calls.append(("snap_triggered", timeout_ms))
        if self.fail_on_snap:
            raise RuntimeError("capture failed")
        return self._frame


def _make_runner(sequence, ctrl=None, radicon=None, tmp_dir=None, **kwargs) -> SequenceRunner:
    ctx = DeviceContext(controller=ctrl, radicon=radicon)
    return SequenceRunner(sequence, ctx, log_dir=tmp_dir, **kwargs)


def _run(runner: SequenceRunner):
    """Run synchronously (no QThread.start()) and capture the three
    terminal-outcome signals."""
    errors: list[tuple[int, str]] = []
    completed: list[bool] = []
    stopped: list[bool] = []
    runner.error_occurred.connect(lambda idx, msg: errors.append((idx, msg)))
    runner.sequence_completed.connect(lambda: completed.append(True))
    runner.sequence_stopped.connect(lambda: stopped.append(True))
    runner.run()
    return errors, completed, stopped


class _RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def make_runner(self, sequence, ctrl=None, radicon=None, **kwargs):
        return _make_runner(sequence, ctrl, radicon, tmp_dir=self._tmp.name, **kwargs)


class MoveConstraintsPreCheckTests(_RunnerTestCase):
    def test_absolute_move_blocked_by_move_constraints(self):
        ctrl = FakeStageController()
        ctrl.constraint_violations[9] = "Move blocked: Ch9 -> +100 requires Ch8 <= 0"
        seq = Sequence(actions=[StageAction(operation="move_absolute", ch=9, value=100)])
        runner = self.make_runner(seq, ctrl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertIn("Ch9", errors[0][1])
        self.assertEqual(runner._last_diagnostic.code, "runtime.move_constraint_violation")
        self.assertEqual(ctrl.call_count("move_ch_absolute"), 0)
        self.assertEqual(completed, [])
        self.assertEqual(stopped, [])

    def test_relative_move_position_unreadable_is_fail_closed(self):
        ctrl = FakeStageController()
        ctrl.fail_on = {("get_ch_pos", 4)}
        seq = Sequence(actions=[StageAction(operation="move_relative", ch=4, value=10)])
        runner = self.make_runner(seq, ctrl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(runner._last_diagnostic.code, "runtime.position_unreadable")
        self.assertEqual(ctrl.call_count("move_ch_relative"), 0)

    def test_unrestricted_channel_move_passes_through_the_precheck(self):
        # Sanity check: the new all-channel pre-check must not become a
        # false positive for a channel with no MOVE_CONSTRAINTS rule.
        ctrl = FakeStageController()
        seq = Sequence(actions=[StageAction(operation="move_absolute", ch=1, value=50)])
        runner = self.make_runner(seq, ctrl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(errors, [])
        self.assertEqual(completed, [True])
        self.assertEqual(ctrl.call_count("check_move_constraints", 1, 50), 1)
        self.assertEqual(ctrl.call_count("move_ch_absolute", 1, 50), 1)


class GlobalLimitDiagnosticTests(_RunnerTestCase):
    def _generous_limits(self, **overrides) -> GlobalLimits:
        fields = dict(
            ch3_minus_mm=100.0, ch3_plus_mm=100.0,
            ch4_minus_mm=100.0, ch4_plus_mm=100.0,
            ch5_minus_mm=100.0, ch5_plus_mm=100.0,
        )
        fields.update(overrides)
        return GlobalLimits(**fields)

    def test_global_limit_exceeded_before_move_reports_the_violating_step_index(self):
        gl = self._generous_limits(ch4_minus_mm=0.001, ch4_plus_mm=0.001)
        ctrl = FakeStageController()
        seq = Sequence(actions=[
            LogAction(message="step0"),
            StageAction(operation="move_absolute", ch=4, value=100000),
        ])
        runner = self.make_runner(seq, ctrl, global_limits=gl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        idx, msg = errors[0]
        self.assertEqual(idx, 1)  # the StageAction step — not _flat_index's post-increment value
        self.assertIn("Global limit exceeded", msg)
        self.assertEqual(runner._last_diagnostic.code, "runtime.global_limit_exceeded")
        self.assertTrue(runner._stop_event.is_set())
        self.assertEqual(completed, [])
        self.assertEqual(stopped, [])
        # The move must never have been sent — this is the pre-move gate.
        self.assertEqual(ctrl.call_count("move_ch_absolute"), 0)

    def test_post_move_position_unreadable_is_fail_closed_not_skipped(self):
        gl = self._generous_limits()
        ctrl = FakeStageController()
        counts: dict[int, int] = {}
        orig_get_ch_pos = ctrl.get_ch_pos

        def flaky_get_ch_pos(ch):
            # RunLogger.log_science() unconditionally reads Ch3/4/5 for
            # conditions.csv on every event (start/error/step-done/...) via
            # its own log_manager._safe_get_pos() helper, which silently
            # swallows failures — those incidental reads must not count
            # toward the fault below, or the very first log_science() call
            # inside RunLogger.start() would consume the "first call
            # succeeds" budget intended for the real baseline capture.
            if sys._getframe(1).f_code.co_name == "_safe_get_pos":
                return orig_get_ch_pos(ch)
            counts[ch] = counts.get(ch, 0) + 1
            if ch == 4 and counts[ch] >= 2:
                raise RuntimeError("comm fail ch4")
            return orig_get_ch_pos(ch)

        ctrl.get_ch_pos = flaky_get_ch_pos
        seq = Sequence(actions=[StageAction(operation="move_absolute", ch=4, value=10)])
        runner = self.make_runner(seq, ctrl, global_limits=gl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertIn("Cannot read Ch4 position", errors[0][1])
        self.assertEqual(
            runner._last_diagnostic.code, "runtime.global_limit_position_unreadable"
        )
        self.assertTrue(runner._stop_event.is_set())

    def test_baseline_read_failure_aborts_before_the_sequence_starts(self):
        gl = self._generous_limits()
        ctrl = FakeStageController()
        ctrl.fail_on = {("get_ch_pos", 3)}
        seq = Sequence(actions=[StageAction(operation="move_absolute", ch=1, value=5)])
        runner = self.make_runner(seq, ctrl, global_limits=gl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], 0)
        self.assertEqual(
            runner._last_diagnostic.code, "controller.global_limit_baseline_unavailable"
        )
        # The sequence must never have actually started.
        self.assertEqual(ctrl.call_count("move_ch_absolute"), 0)
        self.assertEqual(ctrl.call_count("acquire_motion"), 0)

    def test_global_limit_abort_sets_both_stop_events_directly(self):
        # Focused regression test for the High-severity gap found in
        # external review: _trigger_global_limit_error() previously set
        # only _follow_stop_event, so a violation raised from the
        # background follow thread stopped follow but left the main
        # _execute_actions() loop running (_check_stop() only looks at
        # _stop_event). Calling the trigger directly here is the same call
        # _follow_loop() makes on a Global-limit violation, without needing
        # to stand up the full camera/AutoFocus/calibration pipeline
        # start_following() depends on.
        ctrl = FakeStageController()
        seq = Sequence(actions=[])
        runner = self.make_runner(seq, ctrl)
        runner._ctx.controller = ctrl

        errors: list[tuple[int, str]] = []
        runner.error_occurred.connect(lambda idx, msg: errors.append((idx, msg)))

        from apps.exp_scheduler.runner import _StopRequested

        with self.assertRaises(_StopRequested):
            runner._trigger_global_limit_exceeded(4, 5.0, "+1.000 mm", moving=False)

        self.assertTrue(runner._stop_event.is_set())
        self.assertTrue(runner._follow_stop_event.is_set())
        self.assertTrue(runner._had_error)
        self.assertEqual(runner._last_diagnostic.code, "runtime.global_limit_exceeded")
        self.assertEqual(len(errors), 1)
        # And the consequence: _execute_actions()'s stop-check now sees it.
        with self.assertRaises(_StopRequested):
            runner._check_stop()

    def test_global_limit_abort_logs_normal_stop_failure_without_overriding_the_code(self):
        # The oscillation-timeout path already logs a failed forced
        # normal_stop() instead of swallowing it (except Exception: pass);
        # this pins the same behaviour for the Global-limit abort path,
        # found missing in external review. The primary Diagnostic must
        # stay the Global-limit one — the stop-confirmation failure is
        # secondary investigation context, not a replacement cause.
        ctrl = FakeStageController()
        ctrl.fail_on = {"normal_stop"}
        seq = Sequence(actions=[])
        runner = self.make_runner(seq, ctrl)
        runner._ctx.controller = ctrl
        runner._logger.start(
            path="test", devices=[], sequence_dict={}, global_limits_dict={},
            log_base_dir=self._tmp.name,
        )

        from apps.exp_scheduler.runner import _StopRequested

        try:
            with self.assertRaises(_StopRequested):
                runner._trigger_global_limit_exceeded(4, 5.0, "+1.000 mm", moving=True)

            self.assertEqual(runner._last_diagnostic.code, "runtime.global_limit_exceeded")
            ops_log = (runner._logger.log_dir / "ops.log").read_text(encoding="utf-8")
            self.assertIn("normal_stop() failed during global-limit abort", ops_log)
        finally:
            runner._logger.stop()

    def test_follow_thread_abort_racing_a_main_thread_hardware_call_is_not_double_reported(self):
        # Regression test for a race found in external review:
        # _abort_for_global_limit() running on the follow thread revokes
        # the motion lease (normal_stop()); if the main thread is
        # concurrently inside a stage API call, that call can independently
        # raise (e.g. MotionRevokedError in real code) right after the
        # follow thread has already reported the terminal Global-limit
        # error. Without _terminal_error_reported, that side-effect
        # exception would hit _execute_actions()'s generic handler, emit a
        # second error_occurred, and overwrite _last_diagnostic with
        # "runtime.unexpected_error". Uses two real threads (not just a
        # direct method call) so the race is genuine, and a
        # DirectConnection so error_occurred is observed synchronously
        # regardless of which thread emits it (a plain lambda slot is
        # otherwise queued to the runner's own — here, the test's — thread
        # and would never be delivered without a running Qt event loop).
        ctrl = FakeStageController()
        move_started = threading.Event()
        release_move = threading.Event()
        orig_move_absolute = ctrl.move_ch_absolute

        def blocking_move_absolute(ch, target, *, motion=None):
            if ch == 5:
                move_started.set()
                release_move.wait(timeout=5)
                raise RuntimeError("motion lease revoked mid-call")
            return orig_move_absolute(ch, target, motion=motion)

        ctrl.move_ch_absolute = blocking_move_absolute

        seq = Sequence(actions=[
            StageAction(operation="move_absolute", ch=1, value=1),
            StageAction(operation="move_absolute", ch=5, value=1),
        ])
        runner = self.make_runner(seq, ctrl)

        errors: list[tuple[int, str]] = []
        completed: list[bool] = []
        stopped: list[bool] = []
        runner.error_occurred.connect(
            lambda idx, msg: errors.append((idx, msg)), Qt.ConnectionType.DirectConnection
        )
        runner.sequence_completed.connect(
            lambda: completed.append(True), Qt.ConnectionType.DirectConnection
        )
        runner.sequence_stopped.connect(
            lambda: stopped.append(True), Qt.ConnectionType.DirectConnection
        )

        def follow_thread_abort():
            move_started.wait(timeout=5)
            try:
                runner._trigger_global_limit_exceeded(4, 1.0, "+0.100 mm", moving=False)
            except Exception:
                pass  # _StopRequested on this thread's own call stack — expected, discarded
            release_move.set()

        bg = threading.Thread(target=follow_thread_abort)
        bg.start()
        runner.run()
        bg.join(timeout=5)

        self.assertEqual(len(errors), 1)
        self.assertEqual(runner._last_diagnostic.code, "runtime.global_limit_exceeded")
        self.assertEqual(completed, [])
        self.assertEqual(stopped, [])
        ops_log = (runner._logger.log_dir / "ops.log").read_text(encoding="utf-8")
        self.assertIn("Exception after external abort (not re-reported)", ops_log)
        self.assertIn("motion lease revoked mid-call", ops_log)


class MotionLeaseDiagnosticTests(_RunnerTestCase):
    def test_motion_lease_acquire_failure_reports_controller_code(self):
        ctrl = FakeStageController()
        ctrl.fail_on = {"acquire_motion"}
        seq = Sequence(actions=[StageAction(operation="move_absolute", ch=1, value=5)])
        runner = self.make_runner(seq, ctrl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            runner._last_diagnostic.code, "controller.motion_lease_acquire_failed"
        )
        self.assertEqual(ctrl.call_count("move_ch_absolute"), 0)


class GenericFallbackDiagnosticTests(_RunnerTestCase):
    def test_unclassified_exception_gets_the_generic_fallback_code(self):
        ctrl = FakeStageController()
        radicon = _FakeRadicon(fail_on_snap=True)
        seq = Sequence(actions=[TakeXrdAction(
            exposure_ms=5, save=False, oscillate=False,
            dark_enabled=False, defect_enabled=False,
        )])
        runner = self.make_runner(seq, ctrl, radicon=radicon)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertIn("capture failed", errors[0][1])
        self.assertEqual(runner._last_diagnostic.code, "runtime.unexpected_error")


class _AlwaysAliveThread:
    """threading.Thread stand-in that never reports stopped, and never
    actually runs `target` — simulates a genuine wire-call hang without
    waiting out the real 30s/5s join()s."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        pass

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return True


class _TimeoutThenClearsThread:
    """Reports alive after the first join() (the initial 30s wait) but not
    alive after the second (the 5s post-forced-stop grace join) — the
    "timed out once, then happened to clear" scenario that must still be
    treated as a failure, not a silent success."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._joins = 0

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        self._joins += 1

    def is_alive(self) -> bool:
        return self._joins < 2


class _CleanlyStoppedThread:
    """Reports stopped immediately after the first join() — the ordinary
    successful-oscillation-stop case, used to isolate the recovery
    (_return_ch11_to_zero) failure path."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        pass

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return False


def _osc_sequence() -> Sequence:
    return Sequence(actions=[TakeXrdAction(
        exposure_ms=5, save=False, oscillate=True,
        osc_pos_a_deg=-5.0, osc_pos_b_deg=20.0, osc_dwell_ms=0, osc_speed="M",
        dark_enabled=False, defect_enabled=False,
    )])


class OscillationThreadDiagnosticTests(_RunnerTestCase):
    def test_oscillation_loop_exception_is_not_swallowed(self):
        # No thread-class patch here: the fault fires on the very first
        # get_is_moving() call inside the real background _osc_loop
        # thread, so it finishes almost instantly — no need to simulate a
        # hang for this case.
        ctrl = FakeStageController()
        ctrl.fail_on = {"get_is_moving"}
        radicon = _FakeRadicon()
        runner = self.make_runner(_osc_sequence(), ctrl, radicon=radicon)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            runner._last_diagnostic.code, "runtime.ch11_oscillation_execution_failed"
        )

    def test_invalid_oscillation_speed_is_classified_not_generic(self):
        # validate_ch11_oscillation_settings() (safety_rules.py) raises a
        # plain ValueError for these — found unclassified (falling through
        # to the generic "runtime.unexpected_error" fallback) in external
        # review, since _do_take_xrd() called it unwrapped.
        ctrl = FakeStageController()
        radicon = _FakeRadicon()
        seq = Sequence(actions=[TakeXrdAction(
            exposure_ms=5, save=False, oscillate=True,
            osc_pos_a_deg=-5.0, osc_pos_b_deg=20.0, osc_dwell_ms=0,
            osc_speed="NOT_A_SPEED",
            dark_enabled=False, defect_enabled=False,
        )])
        runner = self.make_runner(seq, ctrl, radicon=radicon)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(runner._last_diagnostic.code, "runtime.ch11_oscillation_invalid")

    def test_oscillation_endpoints_resolving_to_the_same_pulse_is_classified(self):
        ctrl = FakeStageController()
        radicon = _FakeRadicon()
        seq = Sequence(actions=[TakeXrdAction(
            exposure_ms=5, save=False, oscillate=True,
            # Both resolve to the same Ch11 pulse position (PULSE_SCALE[11]
            # is comparatively coarse), which validate_ch11_oscillation_settings()
            # rejects outright — a config mistake, not a hardware failure.
            osc_pos_a_deg=0.0, osc_pos_b_deg=0.0, osc_dwell_ms=0, osc_speed="M",
            dark_enabled=False, defect_enabled=False,
        )])
        runner = self.make_runner(seq, ctrl, radicon=radicon)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(runner._last_diagnostic.code, "runtime.ch11_oscillation_invalid")
        # Nothing hardware-related should ever have been attempted.
        self.assertEqual(ctrl.call_count("move_ch_absolute"), 0)
        self.assertEqual(radicon.calls, [])

    def test_oscillation_stop_failure_after_forced_normal_stop_logs_both_exceptions(self):
        ctrl = FakeStageController()
        ctrl.fail_on = {"normal_stop"}
        radicon = _FakeRadicon()
        runner = self.make_runner(_osc_sequence(), ctrl, radicon=radicon)

        with unittest.mock.patch("threading.Thread", _AlwaysAliveThread):
            errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            runner._last_diagnostic.code, "runtime.ch11_oscillation_stop_failed"
        )
        ops_log = (runner._logger.log_dir / "ops.log").read_text(encoding="utf-8")
        self.assertIn("forced normal_stop() also failed", ops_log)

    def test_oscillation_stop_timeout_is_a_failure_even_if_it_later_clears(self):
        ctrl = FakeStageController()
        radicon = _FakeRadicon()
        runner = self.make_runner(_osc_sequence(), ctrl, radicon=radicon)

        with unittest.mock.patch("threading.Thread", _TimeoutThenClearsThread):
            errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            runner._last_diagnostic.code, "runtime.ch11_oscillation_stop_timeout"
        )
        # _return_ch11_to_zero() must not have been called in this branch —
        # if it had been (and succeeded), the code above would instead be
        # whatever _do_take_xrd falls through to next, not stop_timeout.
        ops_log = (runner._logger.log_dir / "ops.log").read_text(encoding="utf-8")
        self.assertNotIn("returned to θ=0°", ops_log)

    def test_return_to_zero_failure_is_reported_distinctly(self):
        ctrl = FakeStageController()
        radicon = _FakeRadicon()
        runner = self.make_runner(_osc_sequence(), ctrl, radicon=radicon)

        with unittest.mock.patch("threading.Thread", _CleanlyStoppedThread), \
             unittest.mock.patch.object(
                 SequenceRunner, "_return_ch11_to_zero",
                 side_effect=RuntimeError("recovery boom"),
             ):
            errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            runner._last_diagnostic.code, "runtime.ch11_return_to_zero_failed"
        )
        self.assertIn("recovery boom", errors[0][1])

    def test_stop_during_return_to_zero_is_a_clean_stop_not_an_error(self):
        # External review finding: _do_take_xrd() caught _return_ch11_to_zero()'s
        # exception with a bare `except Exception`, which also catches
        # _StopRequested (a subclass of Exception) — so a user Stop landing
        # exactly during the post-oscillation return-to-zero move was
        # misreported as runtime.ch11_return_to_zero_failed instead of a
        # graceful stop.
        ctrl = FakeStageController()
        radicon = _FakeRadicon()
        runner = self.make_runner(_osc_sequence(), ctrl, radicon=radicon)

        def _stop_during_recovery(self, speed):
            self._stop_event.set()
            raise _StopRequested()

        with unittest.mock.patch("threading.Thread", _CleanlyStoppedThread), \
             unittest.mock.patch.object(
                 SequenceRunner, "_return_ch11_to_zero", _stop_during_recovery,
             ):
            errors, completed, stopped = _run(runner)

        self.assertEqual(errors, [])
        self.assertEqual(completed, [])
        self.assertEqual(stopped, [True])
        self.assertIsNone(runner._last_diagnostic)


class _AlwaysAliveFollowThread:
    """threading.Thread stand-in that never reports stopped and never runs
    `target` — same role as _AlwaysAliveThread above, but kept distinct
    (rather than reused directly) so a future change to one Ch11-oscillation
    fixture doesn't silently also change follow-thread test behaviour."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        pass

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return True


class _CleanlyStoppedFollowThread:
    """Reports stopped immediately after the first join() — isolates
    _stop_follow()'s self._follow_exception handling from its join/timeout
    handling."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        pass

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return False


class StopFollowFailClosedTests(_RunnerTestCase):
    """Regression tests for the two High-severity follow-thread gaps found
    in external review (REORGANISATION_PLAN.md Phase 9):

    1. _stop_follow() (dsl/api.py::stop_following()) used to treat a join()
       timeout as success — clearing self._follow_thread unconditionally
       and letting the caller proceed to the next step while the follow
       thread could still be moving Ch3/4/5.
    2. Any exception _follow_loop() itself raised (MOVE_CONSTRAINTS
       violation, camera/stage-comms failure, autofocus failure, ...) only
       ever reached the user as an informational progress_updated message —
       nothing recorded the failure, so the sequence went on to report
       success regardless.

    Both are exercised by driving _stop_follow()/_follow_loop() directly
    (as GlobalLimitDiagnosticTests does for _trigger_global_limit_exceeded())
    rather than through a full run(), since a real StartFollowingAction step
    would require actually opening a camera device.
    """

    def _started_runner(self) -> SequenceRunner:
        ctrl = FakeStageController()
        runner = self.make_runner(Sequence(actions=[]), ctrl)
        runner._logger.start(
            path="test", devices=[], sequence_dict={}, global_limits_dict={},
            log_base_dir=self._tmp.name,
        )
        self.addCleanup(runner._logger.stop)
        return runner

    def test_stop_follow_timeout_is_fail_closed_and_keeps_thread_reference(self):
        runner = self._started_runner()
        runner._follow_thread = _AlwaysAliveFollowThread()

        with self.assertRaises(RunnerError) as cm:
            runner._stop_follow()

        self.assertEqual(cm.exception.code, "runtime.follow_thread_stop_timeout")
        # The thread reference must be KEPT (not cleared) so a further stop
        # attempt / cleanup pass still has something to act on.
        self.assertIsNotNone(runner._follow_thread)

    def test_stop_follow_surfaces_a_follow_loop_exception(self):
        runner = self._started_runner()
        runner._follow_thread = _CleanlyStoppedFollowThread()
        runner._follow_exception = RuntimeError("camera comms failed")

        with self.assertRaises(RunnerError) as cm:
            runner._stop_follow()

        self.assertEqual(cm.exception.code, "runtime.follow_thread_failed")
        self.assertIn("camera comms failed", str(cm.exception))
        # Cleanly stopped (not a timeout) — the reference IS cleared.
        self.assertIsNone(runner._follow_thread)
        # Consumed, not left behind for a later spurious re-report.
        self.assertIsNone(runner._follow_exception)

    def test_stop_follow_succeeds_when_thread_exits_cleanly_without_error(self):
        # Sanity check: the ordinary success path must still work —
        # fail-closed must not mean "always fails".
        runner = self._started_runner()
        runner._follow_thread = _CleanlyStoppedFollowThread()

        progress: list[str] = []
        runner.progress_updated.connect(progress.append)

        runner._stop_follow()  # must not raise

        self.assertIsNone(runner._follow_thread)
        self.assertIn("Sample following stopped", progress)

    def test_follow_loop_records_its_own_exception_instead_of_only_logging_it(self):
        # A real (cheap, hardware-free) trigger for _follow_loop()'s own
        # exception handling: a reference image that doesn't exist. Calling
        # _follow_loop() directly (it catches its own exceptions and
        # returns normally — no thread needed) isolates this from
        # _stop_follow()'s join/timeout handling, covered above.
        ctrl = FakeStageController()
        runner = self.make_runner(Sequence(actions=[]), ctrl)
        runner._logger.start(
            path="test", devices=[], sequence_dict={}, global_limits_dict={},
            log_base_dir=self._tmp.name,
        )
        self.addCleanup(runner._logger.stop)
        missing_ref = str(Path(self._tmp.name) / "does_not_exist.png")
        action = StartFollowingAction(reference_path=missing_ref)

        runner._follow_loop(action)

        self.assertIsNotNone(runner._follow_exception)
        self.assertIn("Could not load reference image", str(runner._follow_exception))
        # Round-2 external review finding: _follow_loop() must not just
        # record the failure for _stop_follow() to notice later — it must
        # abort the whole run immediately (see FollowFailureImmediateAbortTests
        # below for the end-to-end consequence of this).
        self.assertTrue(runner._stop_event.is_set())
        self.assertTrue(runner._had_error)
        self.assertTrue(runner._terminal_error_reported)
        self.assertEqual(ctrl.call_count("normal_stop"), 1)


class FollowFailureImmediateAbortTests(_RunnerTestCase):
    """End-to-end regression test for the High-severity gap found in round-2
    external review: a follow-thread failure must stop the main run thread
    before its NEXT action, not only once an eventual stop_following() step
    is reached. Exercises this via a genuine background thread calling
    _abort_follow_thread() (the same method _follow_loop()'s own exception
    handler now calls) while run() is mid-WaitAction on the main thread —
    a real StartFollowingAction step is avoided here since it would need an
    actual camera device; the WaitAction/LogAction pair stand in for
    "whatever step happens to be running or comes next" from the reviewer's
    example (`start_following(); set_pressure(); take_xrd(); stop_following()`)."""

    def test_a_background_follow_failure_stops_the_run_before_the_next_step(self):
        # DirectConnection (not the shared _run() helper's plain-lambda
        # AutoConnection) for the same reason as GlobalLimitDiagnosticTests.
        # test_follow_thread_abort_racing_a_main_thread_hardware_call_is_not_
        # double_reported() above: a plain lambda slot is otherwise queued
        # to the runner QObject's own thread affinity (here, this test's
        # thread — run() is called synchronously, not via .start()) and
        # would never be delivered without a running Qt event loop, since
        # the emitting thread this time is genuinely a different one (the
        # background `aborter` thread below), not run()'s own thread.
        ctrl = FakeStageController()
        seq = Sequence(actions=[
            WaitAction(duration_s=5.0),
            LogAction(message="must never run"),
        ])
        runner = self.make_runner(seq, ctrl)
        started_steps: list[int] = []
        errors: list[tuple[int, str]] = []
        completed: list[bool] = []
        stopped: list[bool] = []
        runner.step_started.connect(
            lambda idx, desc: started_steps.append(idx), Qt.ConnectionType.DirectConnection
        )
        runner.error_occurred.connect(
            lambda idx, msg: errors.append((idx, msg)), Qt.ConnectionType.DirectConnection
        )
        runner.sequence_completed.connect(
            lambda: completed.append(True), Qt.ConnectionType.DirectConnection
        )
        runner.sequence_stopped.connect(
            lambda: stopped.append(True), Qt.ConnectionType.DirectConnection
        )

        def fail_follow_soon() -> None:
            time.sleep(0.1)
            runner._abort_follow_thread(
                "runtime.follow_thread_failed", "Sample following failed: boom",
            )

        aborter = threading.Thread(target=fail_follow_soon)
        aborter.start()
        runner.run()
        aborter.join(timeout=5)

        self.assertEqual(started_steps, [0])  # the WaitAction only — never the LogAction
        self.assertEqual(len(errors), 1)
        self.assertIn("Sample following failed: boom", errors[0][1])
        self.assertEqual(completed, [])
        self.assertEqual(stopped, [])
        self.assertTrue(runner._had_error)
        self.assertEqual(ctrl.call_count("normal_stop"), 1)


class FollowAutofocusEnabledTests(_RunnerTestCase):
    """Regression tests for the High-severity autofocus_enabled=False data
    loss found in external review: Runner._follow_loop() used to hardcode
    `eff_af_enabled = True`, ignoring action.autofocus_enabled entirely, so
    a step that explicitly disabled Ch3 autofocus still moved Ch3 every
    correction cycle."""

    def _make_ready_runner(self, autofocus_enabled: bool, interval_s: float):
        ctrl = FakeStageController()
        runner = self.make_runner(Sequence(actions=[]), ctrl)
        runner._logger.start(
            path="test", devices=[], sequence_dict={}, global_limits_dict={},
            log_base_dir=self._tmp.name,
        )
        self.addCleanup(runner._logger.stop)

        frame = np.full((4, 4, 3), 200, dtype=np.uint8)
        ref_path = Path(self._tmp.name) / "ref.png"
        cv2.imwrite(str(ref_path), frame)

        # Bypass _start_camera_session_if_needed()'s real cv2.VideoCapture —
        # fake an already-open session with a static, non-black frame.
        runner._camera_cap = object()
        runner._camera_index = 0
        runner._camera_current_frame = frame

        action = StartFollowingAction(
            reference_path=str(ref_path),
            interval_s=interval_s,
            camera_index=0,
            autofocus_enabled=autofocus_enabled,
        )
        return runner, action

    def test_autofocus_disabled_action_never_triggers_ch3_autofocus(self):
        runner, action = self._make_ready_runner(autofocus_enabled=False, interval_s=0.05)
        calls: list[int] = []

        with unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_xy_shift", return_value=(0, 0)), \
             unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_similarity", return_value=1.0), \
             unittest.mock.patch.object(
                SequenceRunner, "_do_follow_autofocus",
                lambda *a, **kw: calls.append(1)):
            t = threading.Thread(target=runner._follow_loop, args=(action,))
            t.start()
            time.sleep(0.3)
            runner._follow_stop_event.set()
            t.join(timeout=5)

        self.assertFalse(t.is_alive())
        self.assertEqual(calls, [])
        self.assertIsNone(runner._follow_exception)

    def test_autofocus_enabled_action_triggers_ch3_autofocus(self):
        runner, action = self._make_ready_runner(autofocus_enabled=True, interval_s=0.0)
        calls: list[int] = []

        def fake_af(self_runner, *a, **kw):
            calls.append(1)
            self_runner._follow_stop_event.set()

        with unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_xy_shift", return_value=(0, 0)), \
             unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_similarity", return_value=1.0), \
             unittest.mock.patch.object(
                SequenceRunner, "_do_follow_autofocus", fake_af):
            t = threading.Thread(target=runner._follow_loop, args=(action,))
            t.start()
            t.join(timeout=5)

        self.assertFalse(t.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertIsNone(runner._follow_exception)


class FollowThreadMotionRevokedTests(_RunnerTestCase):
    """External review finding: the main run thread and _follow_loop() share
    a single self._motion_lease. A self-triggered stop the sequence is meant
    to continue past (Ch11 oscillation's _return_ch11_to_zero(), or a
    normal_stop()/emergency_stop() DSL step) revokes and reacquires that
    lease — if the follow thread is concurrently mid-move on the
    now-invalid lease object, it gets MotionRevokedError. Previously any
    exception here (including this benign, self-inflicted one) escalated
    via _abort_follow_thread() into a full-sequence abort. This class pins
    down that a MotionRevokedError with no sequence-wide stop requested is
    now treated as transient (retried), while one that races with a real
    stop is not re-reported."""

    def _make_ready_runner(self, interval_s: float):
        ctrl = FakeStageController()
        runner = self.make_runner(Sequence(actions=[]), ctrl)
        runner._logger.start(
            path="test", devices=[], sequence_dict={}, global_limits_dict={},
            log_base_dir=self._tmp.name,
        )
        self.addCleanup(runner._logger.stop)

        frame = np.full((4, 4, 3), 200, dtype=np.uint8)
        ref_path = Path(self._tmp.name) / "ref.png"
        cv2.imwrite(str(ref_path), frame)

        runner._camera_cap = object()
        runner._camera_index = 0
        runner._camera_current_frame = frame

        action = StartFollowingAction(
            reference_path=str(ref_path),
            interval_s=interval_s,
            camera_index=0,
            autofocus_enabled=False,
        )
        return runner, ctrl, action

    def test_motion_revoked_without_stop_requested_retries_instead_of_aborting(self):
        runner, ctrl, action = self._make_ready_runner(interval_s=0.0)
        abort_calls: list[tuple[str, str]] = []
        runner._abort_follow_thread = lambda code, msg: abort_calls.append((code, msg))
        real_move = ctrl.move_ch_relative
        move_calls = {"n": 0}

        def flaky_move(ch, diff, *, motion=None):
            move_calls["n"] += 1
            if move_calls["n"] == 1:
                # Simulates the main thread's own self-triggered
                # normal_stop()/emergency_stop() revoking the shared lease
                # while this move was in flight — no sequence-wide stop was
                # requested.
                raise MotionRevokedError("lease revoked by a concurrent self-stop")
            runner._follow_stop_event.set()
            return real_move(ch, diff, motion=motion)

        ctrl.move_ch_relative = flaky_move

        with unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_xy_shift", return_value=(100000, 0)), \
             unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_similarity", return_value=1.0):
            t = threading.Thread(target=runner._follow_loop, args=(action,))
            t.start()
            t.join(timeout=5)

        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(move_calls["n"], 2)  # retried after the revocation
        self.assertEqual(abort_calls, [])
        self.assertIsNone(runner._follow_exception)

    def test_motion_revoked_with_stop_already_requested_returns_without_reaborting(self):
        runner, ctrl, action = self._make_ready_runner(interval_s=0.0)
        abort_calls: list[tuple[str, str]] = []
        runner._abort_follow_thread = lambda code, msg: abort_calls.append((code, msg))

        def revoke_and_stop(ch, diff, *, motion=None):
            # A real abort (Stop button, global-limit violation) is already
            # in progress — the resulting MotionRevokedError here must not
            # trigger a second report.
            runner._stop_event.set()
            raise MotionRevokedError("lease revoked by Stop button")

        ctrl.move_ch_relative = revoke_and_stop

        with unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_xy_shift", return_value=(100000, 0)), \
             unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_similarity", return_value=1.0):
            t = threading.Thread(target=runner._follow_loop, args=(action,))
            t.start()
            t.join(timeout=5)

        self.assertFalse(t.is_alive())
        self.assertEqual(abort_calls, [])
        self.assertIsNone(runner._follow_exception)


class CleanupResilienceTests(_RunnerTestCase):
    def test_camera_cleanup_failure_does_not_skip_motion_lease_release(self):
        # Regression test (Medium, REORGANISATION_PLAN.md Phase 9 external
        # review): run()'s finally block used to call its five cleanup
        # steps back-to-back with no isolation — an exception from any one
        # of them (e.g. _cleanup_camera_session()'s VideoCapture.release()
        # failing) would propagate out of the finally block and skip every
        # step after it, most importantly motion-lease release /
        # switch_to_loc, leaving the PM16C lease held (and in REM)
        # indefinitely.
        ctrl = FakeStageController()
        seq = Sequence(actions=[LogAction(message="hi")])
        runner = self.make_runner(seq, ctrl)

        with unittest.mock.patch.object(
            SequenceRunner, "_cleanup_camera_session",
            side_effect=RuntimeError("VideoCapture.release() boom"),
        ):
            errors, completed, stopped = _run(runner)

        # Round-2 external review finding: a cleanup failure must not be
        # silently reported as success — _safe_cleanup() previously only
        # logged it, so run() still ended in sequence_completed/"Completed"
        # even though the camera (and, by extension, whatever else fails
        # the same way) never actually released.
        self.assertEqual(len(errors), 1)
        self.assertIn("VideoCapture.release() boom", errors[0][1])
        self.assertEqual(completed, [])
        self.assertEqual(stopped, [])
        self.assertTrue(runner._had_error)
        # The steps AFTER the failing one must still have run.
        self.assertEqual(ctrl.call_count("switch_to_loc"), 1)
        self.assertEqual(ctrl.call_count("release_motion"), 1)
        self.assertIsNone(runner._motion_lease)
        ops_log = (runner._logger.log_dir / "ops.log").read_text(encoding="utf-8")
        self.assertIn("camera session cleanup failed", ops_log)
        self.assertIn("VideoCapture.release() boom", ops_log)
        self.assertIn("[SEQ:ABORT] Sequence aborted due to error", ops_log)

    def test_switch_to_loc_failure_is_reported_not_silently_swallowed(self):
        # _release_motion_lease() previously wrapped switch_to_loc() in a
        # bare `except: pass` for every failure mode, not just the expected
        # "lease already revoked" case is_valid() already filters out — a
        # genuine communication fault left the PM16C in REM with the run
        # still reporting success.
        ctrl = FakeStageController()
        ctrl.switch_to_loc = unittest.mock.Mock(
            side_effect=RuntimeError("switch_to_loc comms fault")
        )
        seq = Sequence(actions=[LogAction(message="hi")])
        runner = self.make_runner(seq, ctrl)

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertIn("switch_to_loc comms fault", errors[0][1])
        self.assertEqual(completed, [])
        self.assertTrue(runner._had_error)
        # release_motion() must still have been attempted regardless.
        self.assertEqual(ctrl.call_count("release_motion"), 1)
        self.assertIsNone(runner._motion_lease)

    def test_camera_thread_hang_skips_release_instead_of_racing_it(self):
        # _cleanup_camera_session() previously cleared self._camera_thread
        # and called self._camera_cap.release() unconditionally after a
        # bare join(timeout=2.0), even if the background capture thread was
        # still alive (cap.read() hung) — racing release() against that
        # thread's own still-in-flight read() on the same VideoCapture.
        ctrl = FakeStageController()
        seq = Sequence(actions=[LogAction(message="hi")])
        runner = self.make_runner(seq, ctrl)
        fake_cap = unittest.mock.Mock()
        runner._camera_cap = fake_cap
        runner._camera_index = 0
        runner._camera_thread = _AlwaysAliveFollowThread()

        errors, completed, stopped = _run(runner)

        self.assertEqual(len(errors), 1)
        self.assertIn("camera capture thread did not stop within 2s", errors[0][1])
        self.assertEqual(completed, [])
        fake_cap.release.assert_not_called()
        # Both references are kept (not cleared) — mirrors the follow-
        # thread timeout handling, so a still-alive thread/cap stay visible
        # for diagnostics rather than being silently forgotten.
        self.assertIsNotNone(runner._camera_thread)
        self.assertIsNotNone(runner._camera_cap)


if __name__ == "__main__":
    unittest.main()
