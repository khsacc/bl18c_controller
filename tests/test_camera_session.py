"""Hardware-free checks for the shared microscope capture lifecycle."""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from apps.interactive_camera.camera_session import CameraSession
from apps.interactive_camera.interactive_camera import MainWindow
from apps.exp_scheduler.actions import SaveSnapshotAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.ui.monitor_panel import MonitorPanel
from apps.exp_scheduler.validator.checks.camera_follow import check_camera
from apps.exp_scheduler.validator.execution_trace import ExecutionTrace
from apps.exp_scheduler.validator.pre_validator import PreCheckResult


class _QueuedCapture:
    def __init__(self):
        self.frames = queue.Queue()
        self.released = False

    def isOpened(self):
        return True

    def set(self, *_args):
        return True

    def get(self, _property):
        return 30.0

    def read(self):
        try:
            frame = self.frames.get(timeout=0.1)
        except queue.Empty:
            return False, None
        if frame is None:
            return False, None
        return True, frame

    def release(self):
        self.released = True


class CameraSessionTests(unittest.TestCase):
    def test_one_capture_supplies_copies_and_post_move_frames(self):
        cap = _QueuedCapture()
        with mock.patch("apps.interactive_camera.camera_session.cv2.VideoCapture",
                        return_value=cap) as open_camera:
            session = CameraSession(0)
            session.start()
            cap.frames.put(np.full((4, 5, 3), 10, np.uint8))
            first = session.frame_after(0, timeout_s=2)
            first[0, 0] = 0
            self.assertEqual(int(session.latest()[0][0, 0, 0]), 10)
            after = time.monotonic()
            # Complete the read already in flight at ``after``. Its frame
            # cannot count as a post-movement measurement.
            cap.frames.put(None)
            time.sleep(0.15)
            cap.frames.put(np.full((4, 5, 3), 20, np.uint8))
            second = session.frame_after(after, timeout_s=2)
            self.assertEqual(int(second[0, 0, 0]), 20)
            session.stop()
        self.assertEqual(open_camera.call_count, 1)
        self.assertEqual(open_camera.call_args.args[0], 0)
        self.assertTrue(cap.released)

    def test_blocked_read_is_not_released_from_stop_thread(self):
        entered = threading.Event()
        resume = threading.Event()

        class _BlockedCapture(_QueuedCapture):
            def read(self):
                entered.set()
                resume.wait()
                return False, None

        cap = _BlockedCapture()
        with mock.patch("apps.interactive_camera.camera_session.cv2.VideoCapture",
                        return_value=cap):
            session = CameraSession()
            session.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                session.stop(timeout_s=0.01)
            self.assertFalse(cap.released)
            resume.set()
            session.stop(timeout_s=1)
            self.assertTrue(cap.released)


class InteractiveCameraSharedViewTests(unittest.TestCase):
    def test_live_preview_continues_during_read_only_sequence(self):
        app = QApplication.instance() or QApplication([])
        frame = np.full((20, 30, 3), 10, np.uint8)

        class _Session:
            frame_size = (30, 20)
            fps = 30.0
            error = None

            def frame_after(self, *_args, **_kwargs):
                return frame.copy()

            def latest(self):
                return frame.copy(), time.monotonic()

        with mock.patch("apps.interactive_camera.camera_session.cv2.VideoCapture") as cap:
            window = MainWindow(controller=object(), camera_session=_Session())
            window.timer.stop()
            window.is_recording = True
            self.assertIn("Stop recording", window.set_sequence_read_only(True))
            self.assertEqual(window._camera_pages.currentIndex(), 0)
            window.is_recording = False
            self.assertIsNone(window.set_sequence_read_only(True))
            self.assertFalse(window.tab_widget.isEnabled())
            self.assertEqual(window._camera_pages.currentIndex(), 1)
            window.update_frame()
            self.assertEqual(int(window.current_frame[0, 0, 0]), 10)
            frame[:] = 20
            window.update_frame()
            self.assertEqual(int(window.current_frame[0, 0, 0]), 20)
            self.assertFalse(window.close())
            window.set_sequence_read_only(False)
            self.assertEqual(window._camera_pages.currentIndex(), 0)
            self.assertTrue(window.close())
            cap.assert_not_called()
        app.processEvents()


class SchedulerSharedSessionTests(unittest.TestCase):
    def test_monitor_never_opens_camera_when_shared_session_exists(self):
        app = QApplication.instance() or QApplication([])

        class _Session:
            def latest(self):
                return np.full((4, 4, 3), 10, np.uint8), time.monotonic()

        panel = MonitorPanel(DeviceContext(camera=_Session()))
        panel._camera_timer.stop()
        try:
            frame, _key, source, *_ = panel._current_frame()
            self.assertEqual(frame.shape, (4, 4, 3))
            self.assertEqual(source, "Shared microscope camera")
            panel._runner = object()
            with mock.patch("apps.exp_scheduler.ui.monitor_panel._CameraCapture") as cap:
                panel._open_own_capture_if_wanted()
                cap.assert_not_called()
        finally:
            panel.shutdown()
            panel.deleteLater()
            app.processEvents()

    def test_preflight_uses_shared_frame_without_opening_camera(self):
        class _Session:
            index = 0
            error = None

            def latest(self):
                return np.full((4, 4, 3), 10, np.uint8), time.monotonic()

        trace = ExecutionTrace.build([SaveSnapshotAction()])
        result = PreCheckResult()
        with mock.patch("apps.exp_scheduler.validator.checks.camera_follow.cv2.VideoCapture") as cap:
            check_camera(trace, result, ctx=DeviceContext(camera=_Session()))
            cap.assert_not_called()
        self.assertEqual(result.errors, [])

    def test_snapshot_run_reuses_camera_after_cleanup(self):
        frame = np.full((8, 8, 3), 80, np.uint8)

        class _Session:
            index = 0
            calls = 0

            def frame_after(self, *_args, **_kwargs):
                self.calls += 1
                return frame.copy()

            def latest(self):
                return frame.copy(), time.monotonic()

            def stop(self):
                raise AssertionError("The runner must not stop the shared camera")

        session = _Session()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "apps.exp_scheduler.runner.cv2.VideoCapture"
        ) as open_camera:
            runner = SequenceRunner(
                Sequence(actions=[SaveSnapshotAction()]),
                DeviceContext(camera=session), data_dir=tmp,
            )
            errors = []
            completed = []
            runner.error_occurred.connect(lambda _index, message: errors.append(message))
            runner.sequence_completed.connect(lambda: completed.append(True))
            runner.run()
            self.assertEqual(errors, [])
            self.assertEqual(completed, [True])
            self.assertGreaterEqual(session.calls, 2)
            self.assertIsNotNone(session.latest())
            open_camera.assert_not_called()


if __name__ == "__main__":
    unittest.main()
