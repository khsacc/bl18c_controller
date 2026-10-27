"""Hardware-free checks for scheduler shutter ownership and XRD ordering."""
import unittest
import os
import time
from unittest.mock import MagicMock
from unittest.mock import patch

from apps.exp_scheduler.actions import LogAction, TakeXrdAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.scheduler_settings import (
    GlobalCameraSettings, GlobalFollowSettings, GlobalShutterSettings,
    GlobalXrdSettings, canonical_settings_json,
)
from apps.exp_scheduler.sequence import Sequence

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakeShutter:
    def __init__(self, inserted=False):
        self.inserted = inserted
        self.events = []
        self.fail_open = False
        self.fail_in = False
        self.ring_statuses = [5]
        self.ring_error = None
        self.ring_currents = ["450.0"]
        self.ring_current_error = None

    def read_ring_current(self):
        self.events.append("ring_current")
        if self.ring_current_error is not None:
            raise self.ring_current_error
        return self.ring_currents.pop(0) if len(self.ring_currents) > 1 else self.ring_currents[0]

    def read_ring_status(self):
        self.events.append("ring_status")
        if self.ring_error is not None:
            raise self.ring_error
        return self.ring_statuses.pop(0) if len(self.ring_statuses) > 1 else self.ring_statuses[0]

    def get_status(self):
        self.events.append("status")
        return self.inserted

    def set_inserted(self, inserted):
        self.events.append("In" if inserted else "Out")
        if inserted and self.fail_in:
            raise RuntimeError("Insertion failed")
        if not inserted and self.fail_open:
            raise RuntimeError("Drive failed")
        self.inserted = inserted

    def close(self):
        self.events.append("close")


def make_runner(shutter, enabled, *, ring_enabled=True):
    runner = SequenceRunner(
        Sequence(actions=[]), DeviceContext(shutter=shutter),
        global_shutter=GlobalShutterSettings(
            only_open_during_xrd=enabled, stop_on_ring_fault=ring_enabled,
        ),
    )
    runner._logger = MagicMock()
    return runner


class SchedulerShutterTests(unittest.TestCase):
    def test_shutter_setting_round_trips_and_changes_validation_fingerprint(self):
        sequence = Sequence(actions=[TakeXrdAction()],
                            global_shutter={"only_open_during_xrd": True,
                                            "stop_on_ring_fault": False})
        self.assertEqual(Sequence.from_dict(sequence.to_dict()).global_shutter,
                         {"only_open_during_xrd": True, "stop_on_ring_fault": False})
        base = (None, GlobalXrdSettings(), GlobalFollowSettings(),
                GlobalCameraSettings())
        self.assertNotEqual(
            canonical_settings_json(*base, global_shutter=GlobalShutterSettings()),
            canonical_settings_json(*base, global_shutter=GlobalShutterSettings(True)),
        )
        self.assertNotEqual(
            canonical_settings_json(*base, global_shutter=GlobalShutterSettings()),
            canonical_settings_json(*base, global_shutter=GlobalShutterSettings(stop_on_ring_fault=False)),
        )

    def test_unchecked_run_never_touches_shutter(self):
        shutter = FakeShutter()
        runner = make_runner(shutter, False)
        runner.run()
        self.assertEqual(shutter.events, [])

    def test_checked_run_records_initial_state_and_inserts(self):
        shutter = FakeShutter(inserted=False)
        runner = make_runner(shutter, True)
        offered = []
        runner.shutter_restore_available.connect(offered.append)
        runner.run()
        self.assertEqual(offered, [False])
        self.assertTrue(shutter.inserted)
        self.assertEqual(shutter.events[0:3], ["status", "In", "status"])
        self.assertEqual(shutter.events[-1], "close")

    def test_shutter_panel_is_independent_and_xrd_only(self):
        from PyQt6.QtWidgets import QApplication
        from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow

        app = QApplication.instance() or QApplication([])
        window = ExperimentalSchedulerWindow(DeviceContext())
        self.assertTrue(window._shutter_panel.isHidden())
        window._sequence = Sequence(actions=[TakeXrdAction()])
        window._update_xrd_panel_visibility()
        self.assertFalse(window._shutter_panel.isHidden())
        self.assertIsNot(window._shutter_panel, window._xrd_panel)
        window._shutter_xrd_only_chk.setChecked(True)
        self.assertTrue(window._ring_status_chk.isChecked())
        self.assertTrue(window._build_global_shutter().only_open_during_xrd)
        self.assertTrue(window._build_global_shutter().stop_on_ring_fault)
        self.assertTrue(window._shutter_ui_to_dict()["stop_on_ring_fault"])
        window._ring_status_chk.setChecked(False)
        self.assertFalse(window._build_global_shutter().stop_on_ring_fault)
        window._shutter_dict_to_ui({"only_open_during_xrd": False})
        self.assertTrue(window._ring_status_chk.isChecked())  # old file defaults on
        window._sequence = Sequence(actions=[])
        window._update_xrd_panel_visibility()
        self.assertFalse(window._build_global_shutter().only_open_during_xrd)
        self.assertFalse(window._build_global_shutter().stop_on_ring_fault)
        window.deleteLater()

    def test_restore_prompt_returns_to_initial_out_after_confirmation(self):
        from PyQt6.QtWidgets import QApplication, QMessageBox
        from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow

        app = QApplication.instance() or QApplication([])
        shutter = FakeShutter(inserted=True)
        window = ExperimentalSchedulerWindow(DeviceContext(shutter=shutter))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            window._on_shutter_restore_available(False)
        deadline = time.monotonic() + 2
        while window._shutter_restore_busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertFalse(window._shutter_restore_busy)
        self.assertFalse(shutter.inserted)
        self.assertIn("Out", shutter.events)
        self.assertEqual(shutter.events[-1], "close")
        window.deleteLater()

    def test_xrd_is_exposed_only_between_confirmed_out_and_in(self):
        shutter = FakeShutter(inserted=True)
        runner = make_runner(shutter, True)
        runner._prepare_shutter()
        def capture_and_save(*_):
            self.assertFalse(shutter.inserted)
            shutter.events.append("saved")
        runner._do_take_xrd_exposed = capture_and_save
        runner._do_take_xrd(TakeXrdAction(save=True), 0)
        self.assertLess(shutter.events.index("Out"), shutter.events.index("saved"))
        self.assertLess(shutter.events.index("saved"), len(shutter.events) - 2)
        self.assertTrue(shutter.inserted)

    def test_xrd_failure_still_inserts_shutter(self):
        shutter = FakeShutter(inserted=True)
        runner = make_runner(shutter, True)
        runner._prepare_shutter()
        runner._do_take_xrd_exposed = MagicMock(side_effect=RuntimeError("save failed"))
        with self.assertRaisesRegex(RuntimeError, "save failed"):
            runner._do_take_xrd(TakeXrdAction(save=True), 0)
        self.assertTrue(shutter.inserted)

    def test_out_failure_does_not_capture_and_attempts_in(self):
        shutter = FakeShutter(inserted=True)
        shutter.fail_open = True
        runner = make_runner(shutter, True)
        runner._prepare_shutter()
        runner._do_take_xrd_exposed = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "Drive failed"):
            runner._do_take_xrd(TakeXrdAction(), 0)
        runner._do_take_xrd_exposed.assert_not_called()
        self.assertTrue(shutter.inserted)

    def test_failed_reinsertion_reports_error_and_does_not_offer_restore(self):
        shutter = FakeShutter(inserted=True)
        runner = make_runner(shutter, True)
        runner._sequence = Sequence(actions=[TakeXrdAction()])
        runner._do_take_xrd_exposed = lambda *_: setattr(shutter, "fail_in", True)
        errors, offered = [], []
        runner.error_occurred.connect(lambda _index, message: errors.append(message))
        runner.shutter_restore_available.connect(offered.append)
        runner.run()
        self.assertTrue(any("Insertion failed" in message for message in errors))
        self.assertEqual(offered, [])
        self.assertFalse(shutter.inserted)

    def test_ring_is_checked_before_and_after_xrd_with_shutter_safe(self):
        shutter = FakeShutter(inserted=True)
        shutter.ring_statuses = [4, 5]
        runner = make_runner(shutter, True)
        runner._prepare_shutter()
        runner._do_take_xrd_exposed = lambda *_: shutter.events.append("capture")
        runner._do_take_xrd(TakeXrdAction(save=False), 0)
        first_check, second_check = [i for i, event in enumerate(shutter.events)
                                     if event == "ring_status"]
        self.assertLess(first_check, shutter.events.index("Out"))
        self.assertLess(shutter.events.index("capture"), second_check)
        self.assertLess(shutter.events.index("In", shutter.events.index("Out")), second_check)
        self.assertTrue(shutter.inserted)

    def test_ring_fault_before_xrd_prevents_capture_and_shutter_open(self):
        for status in range(4):
            with self.subTest(status=status):
                shutter = FakeShutter(inserted=True)
                shutter.ring_statuses = [status]
                runner = make_runner(shutter, True)
                runner._prepare_shutter()
                runner._do_take_xrd_exposed = MagicMock()
                with self.assertRaisesRegex(RuntimeError, "expected User"):
                    runner._do_take_xrd(TakeXrdAction(save=False), 0)
                runner._do_take_xrd_exposed.assert_not_called()
                self.assertNotIn("Out", shutter.events)
                self.assertTrue(runner._ring_fault_detected)

    def test_ring_fault_after_xrd_stops_sequence_without_restore_prompt(self):
        shutter = FakeShutter(inserted=False)
        shutter.ring_statuses = [5, 3]
        runner = make_runner(shutter, True)
        runner._sequence = Sequence(actions=[TakeXrdAction(save=False), LogAction("next")])
        runner._do_take_xrd_exposed = lambda *_: (shutter.events.append("capture") or
                                                  (object(), None, 1000, 1000))
        runner._run_instant1d = MagicMock()
        errors, started, restore, completed = [], [], [], []
        runner.error_occurred.connect(lambda _index, message: errors.append(message))
        runner.step_started.connect(lambda index, _description: started.append(index))
        runner.shutter_restore_available.connect(restore.append)
        runner.sequence_completed.connect(lambda: completed.append(True))
        runner.run()
        self.assertEqual(started, [0])
        self.assertEqual(len(errors), 1)
        self.assertIn("Ring.Status is 3 after XRD", errors[0])
        self.assertEqual(restore, [])
        self.assertEqual(completed, [])
        self.assertTrue(shutter.inserted)
        runner._run_instant1d.assert_not_called()

    def test_ring_read_failure_stops_before_capture(self):
        shutter = FakeShutter(inserted=True)
        shutter.ring_error = RuntimeError("STARS connection closed")
        runner = make_runner(shutter, False)
        runner._do_take_xrd_exposed = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "Could not read Ring.Status before XRD"):
            runner._do_take_xrd(TakeXrdAction(save=False), 0)
        runner._do_take_xrd_exposed.assert_not_called()

    def test_low_ring_current_before_xrd_prevents_capture_and_shutter_open(self):
        for current in ("9.99", "0", "-0.1"):
            with self.subTest(current=current):
                shutter = FakeShutter(inserted=True)
                shutter.ring_currents = [current]
                runner = make_runner(shutter, True)
                runner._prepare_shutter()
                runner._do_take_xrd_exposed = MagicMock()
                with self.assertRaisesRegex(RuntimeError, "Ring current is .* before XRD"):
                    runner._do_take_xrd(TakeXrdAction(save=False), 0)
                runner._do_take_xrd_exposed.assert_not_called()
                self.assertNotIn("Out", shutter.events)
                self.assertTrue(runner._ring_fault_detected)

    def test_ring_current_at_threshold_is_accepted(self):
        shutter = FakeShutter(inserted=True)
        shutter.ring_currents = ["10", "10.0"]
        runner = make_runner(shutter, False)
        runner._do_take_xrd_exposed = MagicMock(return_value=None)
        runner._do_take_xrd(TakeXrdAction(save=False), 0)
        runner._do_take_xrd_exposed.assert_called_once()
        self.assertEqual(shutter.events.count("ring_current"), 2)

    def test_low_ring_current_after_xrd_stops_sequence(self):
        shutter = FakeShutter(inserted=False)
        shutter.ring_currents = ["450", "3.2"]
        runner = make_runner(shutter, True)
        runner._sequence = Sequence(actions=[TakeXrdAction(save=False), LogAction("next")])
        runner._do_take_xrd_exposed = lambda *_: (shutter.events.append("capture") or
                                                  (object(), None, 1000, 1000))
        runner._run_instant1d = MagicMock()
        errors, started, restore = [], [], []
        runner.error_occurred.connect(lambda _index, message: errors.append(message))
        runner.step_started.connect(lambda index, _description: started.append(index))
        runner.shutter_restore_available.connect(restore.append)
        runner.run()
        self.assertEqual(started, [0])
        self.assertEqual(len(errors), 1)
        self.assertIn("Ring current is 3.2 mA after XRD", errors[0])
        self.assertEqual(restore, [])
        self.assertTrue(shutter.inserted)
        runner._run_instant1d.assert_not_called()

    def test_ring_current_read_failure_or_garbage_stops_before_capture(self):
        cases = {"error": RuntimeError("STARS connection closed"), "garbage": "n/a", "nan": "nan"}
        for name, value in cases.items():
            with self.subTest(case=name):
                shutter = FakeShutter(inserted=True)
                if isinstance(value, Exception):
                    shutter.ring_current_error = value
                    pattern = "Could not read ring current before XRD"
                else:
                    shutter.ring_currents = [value]
                    pattern = "Invalid ring current before XRD"
                runner = make_runner(shutter, False)
                runner._do_take_xrd_exposed = MagicMock()
                with self.assertRaisesRegex(RuntimeError, pattern):
                    runner._do_take_xrd(TakeXrdAction(save=False), 0)
                runner._do_take_xrd_exposed.assert_not_called()

    def test_disabled_ring_option_does_not_query_stars(self):
        shutter = FakeShutter(inserted=True)
        runner = make_runner(shutter, False, ring_enabled=False)
        runner._do_take_xrd_exposed = MagicMock(return_value=None)
        runner._do_take_xrd(TakeXrdAction(save=False), 0)
        self.assertNotIn("ring_status", shutter.events)
        self.assertNotIn("ring_current", shutter.events)


if __name__ == "__main__":
    unittest.main()
