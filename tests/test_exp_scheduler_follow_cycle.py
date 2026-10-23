"""Hardware-free checks for scheduler tracking and camera timing."""
import json
import sys
import threading
import time
import types
from unittest import mock

import cv2
import numpy as np
import pytest

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1,
    )

from apps.exp_scheduler.actions import FollowSampleAction, StartFollowingAction, StopFollowingAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import RunnerError, SequenceRunner
from apps.exp_scheduler.scheduler_settings import GlobalLimits
from apps.exp_scheduler.sequence import Sequence
from apps.interactive_camera.sample_tracking import clamp_xy_correction
from tests.exp_scheduler_fakes import FakeStageController
from utils.stage.control_stage import PULSE_SCALE
from utils.stage.control_stage import MotionRevokedError


def test_shared_xy_limit_applies_both_bounds_before_a_move():
    assert clamp_xy_correction(100, 20, 9, -10, 10) == 1
    assert clamp_xy_correction(-100, 20, -8, -10, 10) == -2
    assert clamp_xy_correction(7, 20, 0, None, None) == 7


def test_scheduler_follow_clamps_to_sequence_limit_before_move():
    baseline = 100
    scale = PULSE_SCALE[4]
    ctrl = FakeStageController(positions={4: baseline + 8})
    runner = SequenceRunner(
        Sequence(actions=[]), DeviceContext(controller=ctrl),
        global_limits=GlobalLimits(ch4_minus_mm=10 * scale / 1000,
                                   ch4_plus_mm=10 * scale / 1000),
    )
    runner._baseline_pos = {4: baseline}

    assert runner._limit_follow_xy_correction(4, 100, 50) == 2
    assert ctrl.positions[4] == baseline + 8  # calculation precedes hardware I/O
    ctrl.positions[4] = baseline - 9
    assert runner._limit_follow_xy_correction(4, -100, 50) == -1


def test_snapshot_waits_for_a_frame_captured_after_its_step(tmp_path):
    runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
    runner._logger._run_dir = tmp_path
    runner._camera_cap = object()
    runner._camera_index = 0
    runner._camera_current_frame = np.full((4, 4, 3), 10, dtype=np.uint8)
    runner._camera_last_frame_at = time.monotonic() - 1
    written = []

    def publish_new_frame():
        time.sleep(0.05)
        with runner._camera_frame_lock:
            runner._camera_current_frame = np.full((4, 4, 3), 80, dtype=np.uint8)
            runner._camera_last_frame_at = time.monotonic()

    publisher = threading.Thread(target=publish_new_frame)
    publisher.start()
    with mock.patch("apps.exp_scheduler.runner.cv2.imwrite",
                    side_effect=lambda path, frame: written.append(frame.copy()) or True):
        from apps.exp_scheduler.actions import SaveSnapshotAction
        runner._do_save_snapshot(SaveSnapshotAction())
    publisher.join(timeout=1)
    assert len(written) == 1
    assert np.all(written[0] == 80)


@pytest.mark.parametrize("fixed_duration", [False, True])
def test_follow_end_waits_for_whole_active_cycle_before_next_step(
        tmp_path, monkeypatch, fixed_duration):
    frame = np.full((8, 8, 3), 100, dtype=np.uint8)
    reference = tmp_path / "reference.png"
    calibration = tmp_path / "calibration.json"
    assert cv2.imwrite(str(reference), frame)
    calibration.write_text(json.dumps({"matrix_inv": [[1, 0], [0, 1]]}))
    monkeypatch.setattr("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration)

    ctrl = FakeStageController()
    runner = SequenceRunner(Sequence(actions=[]), DeviceContext(controller=ctrl))
    runner._logger = mock.Mock()
    runner._camera_cap = object()
    runner._camera_index = 0
    runner._motion_lease = object()
    runner._get_camera_frame_after = lambda captured_after, purpose: frame
    runner._compute_xy_shift = lambda ref, current: (1, 0)
    runner._compute_similarity = lambda ref, current: 1.0
    events = []
    xy_waiting = threading.Event()
    release_xy = threading.Event()
    final_focus = threading.Event()
    release_focus = threading.Event()

    def wait_for_xy(**kwargs):
        xy_waiting.set()
        assert release_xy.wait(2)
        events.append("xy_complete")

    def focus(*args, **kwargs):
        if "pre_focus" not in events:
            events.append("pre_focus")
        else:
            final_focus.set()
            assert release_focus.wait(2)
            events.append("post_focus")

    ctrl.wait_until_stop = wait_for_xy
    runner._do_follow_autofocus = focus
    action = StartFollowingAction(reference_path=str(reference), interval_s=60)

    if fixed_duration:
        def end_follow():
            runner._execute_one(FollowSampleAction(
                duration_s=0.05, reference_path=str(reference), interval_s=60), {})
            events.append("next_step_allowed")
        ending = threading.Thread(target=end_follow)
        ending.start()
    else:
        runner._start_follow(action)
        assert xy_waiting.wait(2)
        def end_follow():
            runner._execute_one(StopFollowingAction(), {})
            events.append("next_step_allowed")
        ending = threading.Thread(target=end_follow)
        ending.start()

    try:
        assert xy_waiting.wait(2)
        assert ending.is_alive()
        assert not runner._follow_stop_event.is_set()
        release_xy.set()
        assert final_focus.wait(2)
        assert ending.is_alive()
        release_focus.set()
        ending.join(timeout=2)
        assert not ending.is_alive()
        assert runner._follow_stop_event.is_set()
        assert events == ["pre_focus", "xy_complete", "post_focus", "next_step_allowed"]
        assert runner._follow_exception is None
    finally:
        release_xy.set()
        release_focus.set()
        runner._follow_stop_event.set()
        ending.join(timeout=2)


def test_interrupted_cycle_does_not_count_as_gracefully_completed(tmp_path, monkeypatch):
    frame = np.full((8, 8, 3), 100, dtype=np.uint8)
    reference = tmp_path / "reference.png"
    calibration = tmp_path / "calibration.json"
    assert cv2.imwrite(str(reference), frame)
    calibration.write_text(json.dumps({"matrix_inv": [[1, 0], [0, 1]]}))
    monkeypatch.setattr("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration)
    runner = SequenceRunner(Sequence(actions=[]), DeviceContext(controller=FakeStageController()))
    runner._camera_cap = object()
    runner._camera_index = 0
    runner._get_camera_frame_after = lambda captured_after, purpose: frame
    runner._compute_xy_shift = lambda ref, current: (1, 0)
    runner._compute_similarity = lambda ref, current: 1.0
    aborts = []
    runner._abort_follow_thread = lambda code, message: aborts.append((code, message))

    def interrupted_move(ch, proposed, step_limit):
        runner._follow_stop_event.set()
        raise MotionRevokedError("stage move interrupted")

    runner._limit_follow_xy_correction = interrupted_move
    runner._follow_loop(StartFollowingAction(
        reference_path=str(reference), interval_s=0, autofocus_enabled=False))

    assert runner._follow_exception is not None
    assert aborts and aborts[0][0] == "runtime.follow_thread_failed"


def test_stop_timeout_never_allows_the_next_step():
    class StuckThread:
        def join(self, timeout):
            assert 0 <= timeout <= 0.01

        def is_alive(self):
            return True

    runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
    runner._logger = mock.Mock()
    runner._follow_thread = StuckThread()
    aborts = []
    runner._abort_follow_thread = lambda code, message: aborts.append(code)

    with mock.patch("apps.exp_scheduler.runner._FOLLOW_STOP_TIMEOUT_S", 0.01):
        with pytest.raises(RunnerError, match="still running"):
            runner._execute_one(StopFollowingAction(), {})

    assert aborts == ["runtime.follow_thread_stop_timeout"]
    assert runner._follow_thread is not None


def test_immediate_stop_waits_for_first_verified_cycle(tmp_path, monkeypatch):
    frame = np.full((8, 8, 3), 100, dtype=np.uint8)
    reference = tmp_path / "reference.png"
    calibration = tmp_path / "calibration.json"
    assert cv2.imwrite(str(reference), frame)
    calibration.write_text(json.dumps({"matrix_inv": [[1, 0], [0, 1]]}))
    monkeypatch.setattr("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration)

    runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
    runner._logger = mock.Mock()
    runner._camera_cap = object()
    runner._camera_index = 0
    runner._get_camera_frame_after = lambda captured_after, purpose: frame
    runner._compute_xy_shift = lambda ref, current: (0, 0)
    runner._compute_similarity = lambda ref, current: 1.0
    entered = threading.Event()
    release = threading.Event()

    def correction(*args):
        entered.set()
        assert release.wait(2)
        return 0, 0

    runner._move_follow_xy = correction
    runner._start_follow(StartFollowingAction(
        reference_path=str(reference), interval_s=60, autofocus_enabled=False,
    ))
    assert entered.wait(2)  # no initial 60-second delay
    stopping = threading.Thread(target=runner._stop_follow)
    stopping.start()
    try:
        time.sleep(0.05)
        assert stopping.is_alive()
        assert not runner._follow_first_cycle_done.is_set()
    finally:
        release.set()
        stopping.join(timeout=2)
        runner._follow_stop_event.set()
    assert not stopping.is_alive()
    assert runner._follow_first_cycle_done.is_set()


def test_unrecovered_similarity_aborts_the_follow_session(tmp_path, monkeypatch):
    frame = np.full((8, 8, 3), 100, dtype=np.uint8)
    reference = tmp_path / "reference.png"
    calibration = tmp_path / "calibration.json"
    assert cv2.imwrite(str(reference), frame)
    calibration.write_text(json.dumps({"matrix_inv": [[1, 0], [0, 1]]}))
    monkeypatch.setattr("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration)

    ctrl = FakeStageController()
    runner = SequenceRunner(Sequence(actions=[]), DeviceContext(controller=ctrl))
    runner._logger = mock.Mock()
    runner._camera_cap = object()
    runner._camera_index = 0
    runner._get_camera_frame_after = lambda captured_after, purpose: frame
    runner._compute_xy_shift = lambda ref, current: (0, 0)
    runner._compute_similarity = lambda ref, current: 0.2
    runner._move_follow_xy = lambda *args: (0, 0)

    runner._follow_loop(StartFollowingAction(
        reference_path=str(reference), interval_s=60, autofocus_enabled=False,
    ))

    assert isinstance(runner._follow_exception, RunnerError)
    assert runner._follow_exception.code == "runtime.follow_similarity_below_threshold"
    assert runner._stop_event.is_set()
    assert not runner._follow_first_cycle_done.is_set()
    assert ctrl.call_count("normal_stop") == 1


def test_autofocus_worker_error_is_not_treated_as_success():
    runner = SequenceRunner(Sequence(actions=[]), DeviceContext())

    class FailedAutofocus:
        def perform_autofocus(self, *, motion):
            self.focus_thread = threading.Thread(
                target=lambda: self.error_callback("camera frame unavailable")
            )
            self.focus_thread.start()
            return True

    with pytest.raises(RunnerError, match="camera frame unavailable"):
        runner._run_autofocus_sync(FailedAutofocus(), object())
