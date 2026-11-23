"""Startup-only LakeShore UI availability; all devices are fakes."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1,
    )

from PyQt6.QtWidgets import QApplication, QDialog

from apps.exp_scheduler.actions import (
    AllHeatersOffAction, ForLoopAction, SetAndWaitTemperatureAction,
    SetHeaterAction, SetTemperatureAction, WaitAction, WaitTemperatureAction,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.ui import scheduler_window
from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
from apps.exp_scheduler.ui.step_editor import StepEditorDialog
from apps.exp_scheduler.validator.pre_validator import PreValidator
from tests.exp_scheduler_fakes import FakeLakeshore

_app = QApplication.instance() or QApplication([])


class LakeShoreAvailabilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = patch.object(
            scheduler_window, "_SETTINGS_PATH", Path(directory.name) / "settings.json",
        )
        settings.start()
        self.addCleanup(settings.stop)

    def _window(self, backend=None):
        window = ExperimentalSchedulerWindow(DeviceContext(lakeshore=backend))
        self.addCleanup(window.close)
        return window

    def _ls_widgets(self, window):
        return [window._log_lakeshore_chk] + [
            checks[key]
            for checks in (window._xrd_suffix_chks, window._spectrum_suffix_chks)
            for key in ("temperature_set", "temperature_b")
        ]

    def test_global_controls_follow_startup_connection_and_keep_values(self):
        for backend, available in ((None, False), (FakeLakeshore(False), False),
                                   (FakeLakeshore(), True)):
            with self.subTest(available=available, backend=backend):
                window = self._window(backend)
                window._log_dict_to_ui({"lakeshore": True})
                window._xrd_dict_to_ui({"filename_suffixes": ["temperature_set", "temperature_b"]})
                window._spectrum_dict_to_ui({
                    "filename_suffixes": ["temperature_set", "temperature_b"],
                    "pressure_enabled": True,
                    "temperature_correction_enabled": True,
                    "temperature_source": "lakeshore_b",
                })
                # Restoring a run's settings and unlocking the parent after
                # a run must not re-enable unavailable controls.
                window._global_settings_content.setEnabled(False)
                window._global_settings_content.setEnabled(True)
                for widget in self._ls_widgets(window):
                    self.assertEqual(widget.isEnabled(), available)
                    self.assertTrue(widget.isChecked())
                self.assertEqual(window._temp_max_age_spin.isEnabled(), available)
                item = window._temp_source_combo.model().item(
                    window._temp_source_combo.findData("lakeshore_b")
                )
                self.assertEqual(item.isEnabled(), available)
                self.assertEqual(window._temp_source_combo.currentData(), "lakeshore_b")
                self.assertEqual(window._timeline.lakeshore_available, available)
                self.assertTrue(window._log_pace5000_chk.isEnabled())
                self.assertTrue(window._xrd_suffix_chks["datetime_full"].isEnabled())

    def test_manual_temperature_correction_remains_available(self):
        window = self._window()
        window._pressure_enable_chk.setChecked(True)
        window._temp_correction_chk.setChecked(True)
        window._temp_source_combo.setCurrentIndex(window._temp_source_combo.findData("manual"))
        self.assertTrue(window._temp_source_combo.isEnabled())
        self.assertTrue(window._temp_correction_chk.isEnabled())
        self.assertTrue(window._temp_scale_combo.isEnabled())
        self.assertTrue(window._manual_temp_spin.isEnabled())
        self.assertFalse(window._temp_max_age_spin.isEnabled())
        window._manual_temp_spin.setValue(295)
        self.assertEqual(window._build_global_spectrum().manual_temperature_k, 295)

    def test_availability_is_fixed_until_reopened_but_validation_is_live(self):
        backend = FakeLakeshore()
        window = self._window(backend)
        backend.connected = False
        window._update_pressure_widgets()
        self.assertTrue(window._log_lakeshore_chk.isEnabled())
        result = PreValidator().validate(Sequence([AllHeatersOffAction()]), window._ctx)
        self.assertIn("preflight.lakeshore.controller_not_connected",
                      [d.code for d in result.diagnostics])
        reopened = self._window(backend)
        backend.connected = True
        reopened._update_pressure_widgets()
        self.assertFalse(reopened._log_lakeshore_chk.isEnabled())

    def test_unavailable_saved_steps_can_be_inspected_without_replacement(self):
        for action in (SetTemperatureAction(280, 2), WaitTemperatureAction(1),
                       SetAndWaitTemperatureAction(280, 2, 1, "B"),
                       SetHeaterAction(2), AllHeatersOffAction()):
            with self.subTest(action=action):
                dialog = StepEditorDialog(action=action, lakeshore_available=False)
                self.addCleanup(dialog.close)
                self.assertEqual(dialog._dev_combo.currentText(), "LakeShore")
                self.assertFalse(dialog._ok_btn.isEnabled())
                self.assertFalse(dialog._stack.isEnabled())
                self.assertFalse(dialog._op_combo.isEnabled())
                dialog._on_ok()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
                self.assertIsNone(dialog.get_action())
                dialog._dev_combo.setCurrentText("General")
                dialog._op_combo.setCurrentText("wait")
                self.assertTrue(dialog._ok_btn.isEnabled())
                self.assertTrue(dialog._stack.isEnabled())
                dialog._on_ok()
                self.assertIsInstance(dialog.get_action(), WaitAction)

    def test_lakeshore_category_is_disabled_only_when_unavailable(self):
        for available in (False, True):
            dialog = StepEditorDialog(lakeshore_available=available)
            self.addCleanup(dialog.close)
            item = dialog._dev_combo.model().item(dialog._dev_combo.findText("LakeShore"))
            self.assertEqual(item.isEnabled(), available)

    def test_timeline_passes_availability_for_add_and_edit_inside_and_outside_loop(self):
        window = self._window()
        timeline = window._timeline
        timeline.set_sequence(Sequence([
            WaitAction(1), ForLoopAction("i", [1], [WaitAction(2)]),
        ]))
        for item in (timeline._tree.topLevelItem(0), timeline._tree.topLevelItem(1).child(0)):
            for handler in (timeline._on_add, timeline._on_edit):
                with self.subTest(item=item, handler=handler):
                    timeline._tree.setCurrentItem(item)
                    with patch("apps.exp_scheduler.ui.step_editor.StepEditorDialog") as dialog:
                        dialog.return_value.exec.return_value = 0
                        handler()
                        self.assertFalse(dialog.call_args.kwargs["lakeshore_available"])


if __name__ == "__main__":
    unittest.main()
