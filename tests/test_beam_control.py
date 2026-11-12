"""Automatic shutter policy and acquisition integration, without beamline I/O."""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from utils.beam_control import BeamControl
from utils.shutter import ShutterControllerSim, ShutterError


class RecordingShutter(ShutterControllerSim):
    def __init__(self):
        super().__init__()
        self.events = []
        self.fail_open = False
        self.fail_close = False

    def set_inserted(self, inserted, **kwargs):
        self.events.append(inserted)
        if (inserted and self.fail_close) or (not inserted and self.fail_open):
            raise ShutterError("Simulated drive failure")
        super().set_inserted(inserted, **kwargs)


class BeamControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.policy = BeamControl()
        self.shutter = RecordingShutter()
        self.policy.configure(self.shutter)

    def enable(self):
        self.policy.set_automatic(True)
        self.shutter.events.clear()

    def test_manual_does_not_move_shutter(self):
        with self.policy.session(beam=True):
            self.assertTrue(self.shutter.get_status())
        self.assertEqual(self.shutter.events, [])

    def test_enabling_closes_and_manual_leaves_position_unchanged(self):
        self.shutter.set_inserted(False)
        self.policy.set_automatic(True)
        self.assertTrue(self.shutter.get_status())
        self.policy.set_automatic(False)
        self.assertTrue(self.shutter.get_status())

    def test_failed_enable_does_not_select_auto(self):
        self.shutter.fail_close = True
        with self.assertRaises(ShutterError):
            self.policy.set_automatic(True)
        self.assertFalse(self.policy.automatic)
        self.assertFalse(self.policy.in_use)

    def test_nested_capture_closes_after_outer_session_even_on_error(self):
        self.enable()
        with self.assertRaisesRegex(ValueError, "capture failed"):
            with self.policy.session(beam=True):
                with self.policy.session(beam=True):
                    self.assertFalse(self.shutter.get_status())
                self.assertFalse(self.shutter.get_status())
                raise ValueError("capture failed")
        self.assertEqual(self.shutter.events, [False, True])
        self.assertFalse(self.policy.in_use)

    def test_failed_open_attempts_close_without_running_capture(self):
        self.enable()
        self.shutter.fail_open = True
        with self.assertRaises(ShutterError):
            with self.policy.session(beam=True):
                self.fail("Must not capture after failed Out")
        self.assertEqual(self.shutter.events, [False, True])
        self.assertFalse(self.policy.in_use)

    def test_failed_close_is_reported_and_releases_ownership(self):
        self.enable()
        with self.assertRaises(ShutterError):
            with self.policy.session(beam=True):
                self.shutter.fail_close = True
        self.assertFalse(self.policy.in_use)

    def test_other_worker_and_mode_change_cannot_interrupt_exposure(self):
        self.enable()
        errors = []
        def contend():
            try:
                with self.policy.session(beam=True):
                    self.fail("Concurrent exposure must be refused")
            except ShutterError as exc:
                errors.append(str(exc))
        with self.policy.session(beam=True):
            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            with self.assertRaises(ShutterError):
                self.policy.set_automatic(False)
            self.assertFalse(self.shutter.get_status())
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.shutter.events, [False, True])

    def test_scan_workers_close_before_return_and_do_not_expose_fluorescence(self):
        from apps.scan2d.free_2d_scan_backend import Free2DScanWorker, Scan1DWorker
        from apps.dac_scan.collimator_scan_backend import CollimatorScanWorker
        from apps.dac_scan.dac_scan_rot_backend import DacScanRotWorker
        self.enable()
        reader = SimpleNamespace(uses_transmitted_xrays=True,
                                 set_current_position=lambda *args: None,
                                 set_theta=lambda *args: None)
        def read():
            self.assertFalse(self.shutter.get_status())
            return 1.0
        reader.read_transmitted = read
        common = dict(controller=MagicMock(), gpib_reader=reader)
        workers = [
            Free2DScanWorker(**common, ch_x=4, ch_y=5, x_pulses=[0], y_pulses=[0],
                             center_x=0, center_y=0, settle_ms=0),
            Scan1DWorker(**common, ch=4, pulses=[0], center=0, settle_ms=0),
            CollimatorScanWorker(**common, x_pulses=[0], y_pulses=[0], center_x=0, center_y=0),
            DacScanRotWorker(**common, theta_deg_list=[0], ch10_start=0,
                             ch10_stop=0, ch10_step=1, settle_ms=0),
        ]
        # All worker variants use the same reader-aware helper.
        with patch("utils.beam_control.beam_control", self.policy):
            for worker in workers:
                with self.subTest(worker=type(worker).__name__):
                    completed = []
                    worker.scan_completed.connect(lambda: completed.append(self.shutter.get_status()))
                    worker.run()
                    self.assertEqual(completed, [True])
            self.shutter.events.clear()
            reader.uses_transmitted_xrays = False
            reader.read_transmitted = lambda: 1.0
            workers[1].run()
            self.assertEqual(self.shutter.events, [])

    def test_xrd_dark_live_sequence_and_triggered_capture(self):
        import numpy as np
        from apps.Rad_icon_2022 import radicon_backend, radicon_ui
        self.enable()
        backend = radicon_backend.RadiconBackendSim(width=2, height=2)
        with (patch.object(radicon_backend, "beam_control", self.policy),
              patch.object(radicon_ui, "beam_control", self.policy)):
            for kind in ("snap", "sequence", "dark", "live"):
                self.shutter.events.clear()
                if kind == "snap":
                    worker = radicon_ui._SnapWorker(backend, 100_000, 1000)
                elif kind == "sequence":
                    worker = radicon_ui._SeqWorker(backend, 100_000, 2, 1, 1000)
                elif kind == "dark":
                    worker = radicon_ui._DarkWorker(backend, 100_000, 2, 1000)
                else:
                    worker = radicon_ui._LiveWorker(backend, 100_000, 1000)
                def snap(timeout_ms=None):
                    self.assertEqual(self.shutter.get_status(), kind == "dark")
                    if kind == "live":
                        worker.request_stop()
                    return np.ones((2, 2), dtype=np.uint16)
                backend.snap = snap
                errors = []
                worker.error.connect(errors.append)
                worker.run()
                self.assertEqual(errors, [], kind)
                self.assertTrue(self.shutter.get_status())
                self.assertEqual(self.shutter.events, [True, True] if kind == "dark" else [False, True])

    def test_global_auto_overrides_scheduler_setting_and_suppresses_restore(self):
        from apps.exp_scheduler import runner as runner_module
        from tests.test_exp_scheduler_shutter import make_runner
        self.enable()
        runner = make_runner(self.shutter, False)
        offered = []
        runner.shutter_restore_available.connect(offered.append)
        with patch.object(runner_module, "beam_control", self.policy):
            runner.run()
        self.assertTrue(runner._shutter_managed)
        self.assertEqual(offered, [])
        self.assertTrue(self.shutter.get_status())
        self.assertFalse(self.policy.in_use)

    def test_radio_switches_auto_and_keeps_policy_when_window_closes(self):
        import time
        from tools.shutter_tool import ShutterToolWindow
        window = ShutterToolWindow(self.shutter, policy=self.policy)
        def wait():
            deadline = time.monotonic() + 2
            while window.is_busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.app.processEvents()
            self.assertFalse(window.is_busy)
        try:
            wait()
            self.shutter.set_inserted(False)
            window._auto_radio.click()
            wait()
            self.assertTrue(self.policy.automatic)
            self.assertTrue(window._auto_radio.isChecked())
            self.assertFalse(window._toggle_button.isEnabled())
            self.assertTrue(self.shutter.get_status())
            window.close()
            self.assertTrue(self.policy.automatic)
        finally:
            wait()
            window.close()

    def test_auto_refuses_capture_when_status_does_not_confirm_out(self):
        self.enable()
        original = self.shutter.get_status
        self.shutter.get_status = lambda: True
        with self.assertRaisesRegex(ShutterError, "confirm shutter Out"):
            with self.policy.session(beam=True):
                self.fail("Unconfirmed Out must not expose")
        self.shutter.get_status = original
        self.assertTrue(self.shutter.get_status())

    def test_scan_abort_and_reader_error_both_close_before_terminal_signal(self):
        from apps.scan2d.free_2d_scan_backend import Scan1DWorker
        self.enable()
        for failure in (False, True):
            with self.subTest(reader_error=failure):
                worker = None
                def read():
                    self.assertFalse(self.shutter.get_status())
                    if failure:
                        raise RuntimeError("Reader failed")
                    worker.abort()
                    return 1.0
                reader = SimpleNamespace(uses_transmitted_xrays=True,
                                         set_current_position=lambda *args: None,
                                         read_transmitted=read)
                worker = Scan1DWorker(MagicMock(), reader, 4, [0, 1], 0, settle_ms=0)
                aborted = []
                completed = []
                worker.scan_aborted.connect(lambda: aborted.append(self.shutter.get_status()))
                worker.scan_completed.connect(lambda: completed.append(True))
                with patch("utils.beam_control.beam_control", self.policy):
                    worker.run()
                self.assertEqual(aborted, [True])
                self.assertEqual(completed, [])
                self.assertFalse(self.policy.in_use)

    def test_scheduler_auto_exposes_only_xrd_and_closes_on_capture_failure(self):
        from apps.exp_scheduler import runner as runner_module
        from apps.exp_scheduler.actions import TakeXrdAction
        from tests.test_exp_scheduler_shutter import make_runner
        self.enable()
        for failure in (False, True):
            with self.subTest(capture_error=failure):
                runner = make_runner(self.shutter, False)
                def capture(*args):
                    self.assertFalse(self.shutter.get_status())
                    if failure:
                        raise RuntimeError("Detector failed")
                def actions(*args):
                    self.assertTrue(self.shutter.get_status())
                    runner._do_take_xrd(TakeXrdAction(), 0)
                    self.assertTrue(self.shutter.get_status())
                runner._do_take_xrd_exposed = capture
                runner._execute_actions = actions
                with patch.object(runner_module, "beam_control", self.policy):
                    runner.run()
                self.assertEqual(runner._had_error, failure)
                self.assertTrue(self.shutter.get_status())
                self.assertFalse(self.policy.in_use)

    def test_free_run_auto_discards_frame_spanning_shutter_transition(self):
        import numpy as np
        from apps.Rad_icon_2022 import radicon_backend, radicon_ui
        self.enable()
        backend = radicon_backend.RadiconBackendSim(width=2, height=2)
        count = []
        def snap(timeout_ms=None):
            count.append(1)
            return np.full((2, 2), len(count), dtype=np.uint16)
        backend.snap = snap
        with patch.object(radicon_ui, "beam_control", self.policy):
            worker = radicon_ui._SeqWorker(backend, 100_000, 2, 0, 1000)
            images = []
            worker.done.connect(images.append)
            worker.run()
        self.assertEqual([int(image[0, 0]) for image in images[0]], [2, 3])
        self.assertTrue(self.shutter.get_status())

    def test_transmission_scan_with_stage_and_reader_simulators(self):
        from utils.stage.control_stage_sim import PM16CControllerSim
        from apps.scan2d.free_2d_scan_backend import GpibReaderSim, Scan1DWorker
        self.enable()
        stage = PM16CControllerSim()
        stage.connect()
        try:
            worker = Scan1DWorker(stage, GpibReaderSim(2.0, 0.11), 4, [-1, 1], 0,
                                  settle_ms=0)
            completed, aborted, messages = [], [], []
            worker.status_message.connect(messages.append)
            worker.scan_completed.connect(lambda: completed.append(True))
            worker.scan_aborted.connect(lambda: aborted.append(True))
            with (patch("utils.beam_control.beam_control", self.policy),
                  patch("utils.stage.control_stage_sim.SOFT_LIMITS", {4: (-100, 100)})):
                worker.run()
            self.assertEqual(completed, [True], messages)
            self.assertEqual(aborted, [])
            self.assertEqual(int(stage.get_ch_pos(4)), 0)
            self.assertEqual(self.shutter.events, [False, True])
        finally:
            stage.disconnect()

    def test_scheduler_triggered_dark_never_opens_shutter(self):
        import numpy as np
        from apps.exp_scheduler import runner as runner_module
        from apps.exp_scheduler.actions import TakeDarkAction
        from apps.Rad_icon_2022 import radicon_backend
        from tests.test_exp_scheduler_shutter import make_runner
        self.enable()
        backend = radicon_backend.RadiconBackendSim(width=2, height=2)
        def snap(timeout_ms=None):
            self.assertTrue(self.shutter.get_status())
            return np.zeros((2, 2), dtype=np.uint16)
        backend.snap = snap
        runner = make_runner(self.shutter, False)
        runner._ctx.radicon = backend
        runner._execute_actions = lambda *args: runner._do_take_dark(TakeDarkAction(exposure_ms=100))
        with (patch.object(runner_module, "beam_control", self.policy),
              patch.object(radicon_backend, "beam_control", self.policy)):
            runner.run()
        self.assertFalse(runner._had_error)
        self.assertNotIn(False, self.shutter.events)
        self.assertTrue(self.shutter.get_status())
