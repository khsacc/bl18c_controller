"""Hardware-free checks for scheduler shutter ownership and XRD ordering."""
import unittest
import os
import time
from unittest.mock import MagicMock
from unittest.mock import patch

from apps.exp_scheduler.actions import TakeXrdAction
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


def make_runner(shutter, enabled):
    runner = SequenceRunner(
        Sequence(actions=[]), DeviceContext(shutter=shutter),
        global_shutter=GlobalShutterSettings(only_open_during_xrd=enabled),
    )
    runner._logger = MagicMock()
    return runner


class SchedulerShutterTests(unittest.TestCase):
    def test_shutter_setting_round_trips_and_changes_validation_fingerprint(self):
        sequence = Sequence(actions=[TakeXrdAction()],
                            global_shutter={"only_open_during_xrd": True})
        self.assertEqual(Sequence.from_dict(sequence.to_dict()).global_shutter,
                         {"only_open_during_xrd": True})
        base = (None, GlobalXrdSettings(), GlobalFollowSettings(),
                GlobalCameraSettings())
        self.assertNotEqual(
            canonical_settings_json(*base, global_shutter=GlobalShutterSettings()),
            canonical_settings_json(*base, global_shutter=GlobalShutterSettings(True)),
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
        self.assertTrue(window._build_global_shutter().only_open_during_xrd)
        window._sequence = Sequence(actions=[])
        window._update_xrd_panel_visibility()
        self.assertFalse(window._build_global_shutter().only_open_during_xrd)
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


if __name__ == "__main__":
    unittest.main()
