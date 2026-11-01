"""Experimental Scheduler: Import from Interactive Camera (gasket setup)."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PyQt6.QtWidgets import QApplication, QMessageBox

from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.ui import scheduler_window
from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
from apps.interactive_camera import gasket_setup
from apps.interactive_camera.gasket_tracking import GasketEllipse, reference_digest

_app = QApplication.instance() or QApplication([])

ELLIPSE = GasketEllipse(315, 230, 190, 175).to_dict()
RUBIES = {"Ruby A": {"ch3": 1, "ch4": 5, "ch5": -7}}


class _ImportFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.reference = self.tmp / "ic_reference.png"
        cv2.imwrite(str(self.reference), np.full((480, 640, 3), 120, np.uint8))
        self.state = {
            "reference_path": str(self.reference),
            "gasket_ellipse": ELLIPSE,
            "gasket_sample_point": {"x": 320.0, "y": 226.0},
            "gasket_sample_mode": "manual",
            "gasket_reference_digest": reference_digest(self.reference),
            "gasket_reference_position": [10, 1000, 2000],
            "xrd_position": [10, 1000, 2000],
            "ruby_positions": RUBIES,
            "ruby_offset_origin": "gasket_centre",
            "settings": {"tolerance_um": 4.5, "average_frames": 7,
                         "allow_uniform_scale": True},
        }
        state_path = self.tmp / "gasket_tracking.json"
        state_path.write_text(json.dumps(self.state), encoding="utf-8")
        patcher = mock.patch.object(gasket_setup, "STATE_PATH", state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state_path = state_path

    def _window(self):
        missing = Path("__missing_exp_scheduler_test_settings__.json")
        with mock.patch.object(scheduler_window, "_SETTINGS_PATH", missing):
            window = ExperimentalSchedulerWindow(DeviceContext())
        self.addCleanup(window.deleteLater)
        return window

    def _import(self, window, answer=QMessageBox.StandardButton.Yes):
        with (mock.patch.object(scheduler_window.QMessageBox, "question",
                                return_value=answer) as question,
              mock.patch.object(scheduler_window.QMessageBox, "warning") as warning,
              mock.patch.object(window, "_set_setting")):
            window._import_interactive_camera_gasket()
        return question, warning


class ImportInteractiveCameraGasketTests(_ImportFixture):
    def test_import_takes_over_outline_sample_rubies_and_settings(self):
        window = self._window()
        question, warning = self._import(window)
        warning.assert_not_called()
        self.assertIn("Ruby A", question.call_args.args[2])
        self.assertEqual(window._follow_method_combo.currentData(), "gasket_beta")
        self.assertEqual(window._ref_current_path, self.reference)
        self.assertEqual(window._gasket_ellipse, ELLIPSE)
        self.assertEqual(window._gasket_sample_point, {"x": 320.0, "y": 226.0})
        self.assertEqual(window._gasket_sample_mode, "manual")
        self.assertEqual(window._gasket_reference_position, (10, 1000, 2000))
        self.assertEqual(window._sample_xrd_position, (10, 1000, 2000))
        self.assertEqual(window._ruby_positions, RUBIES)
        self.assertEqual(window._ruby_offset_origin, "gasket_centre")
        follow = window._build_global_follow()
        self.assertEqual(follow.reference_path, str(self.reference))
        self.assertEqual(follow.tracking_method, "gasket_beta")
        self.assertEqual(follow.gasket_reference_digest, self.state["gasket_reference_digest"])
        self.assertEqual(follow.gasket_tolerance_um, 4.5)
        self.assertEqual(follow.gasket_average_frames, 7)
        self.assertTrue(follow.gasket_allow_uniform_scale)
        # The scheduler keeps its own copy; IC's saved setup is untouched.
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8")), self.state)

    def test_declining_keeps_current_positions(self):
        window = self._window()
        before = (window._ref_current_path, window._gasket_ellipse, dict(window._ruby_positions))
        self._import(window, QMessageBox.StandardButton.No)
        self.assertEqual(
            (window._ref_current_path, window._gasket_ellipse, dict(window._ruby_positions)),
            before)

    def test_outline_for_other_image_is_refused(self):
        self.state["gasket_reference_digest"] = "different"
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        window = self._window()
        question, warning = self._import(window)
        question.assert_not_called()
        self.assertIn("different reference image", warning.call_args.args[2])
        self.assertIsNone(window._gasket_ellipse)

    def test_missing_setup_is_refused(self):
        self.state_path.unlink()
        window = self._window()
        question, warning = self._import(window)
        question.assert_not_called()
        self.assertIn("no gasket tracking reference photo", warning.call_args.args[2])

    def test_refused_during_a_run(self):
        window = self._window()
        window._runner = SimpleNamespace(isRunning=lambda: True)
        question, warning = self._import(window)
        question.assert_not_called()
        warning.assert_called_once()


class InvalidInteractiveCameraSetupTests(_ImportFixture):
    """Any invalid saved value refuses the import before anything changes."""

    def _assert_refused(self, message):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        window = self._window()
        before = (window._follow_method_combo.currentData(), window._ref_current_path,
                  window._gasket_ellipse, window._gasket_tolerance_spin.value(),
                  dict(window._ruby_positions))
        question, warning = self._import(window)
        question.assert_not_called()
        self.assertIn(message, warning.call_args.args[2])
        self.assertEqual(
            (window._follow_method_combo.currentData(), window._ref_current_path,
             window._gasket_ellipse, window._gasket_tolerance_spin.value(),
             dict(window._ruby_positions)), before)

    def test_invalid_tolerance(self):
        self.state["settings"]["tolerance_um"] = "invalid"
        self._assert_refused("Invalid gasket tolerance")

    def test_invalid_average_frames(self):
        self.state["settings"]["average_frames"] = 0
        self._assert_refused("Invalid average frame count")

    def test_invalid_ruby_positions(self):
        self.state["ruby_positions"] = {"Ruby A": {"ch3": 1, "ch4": "x", "ch5": 0}}
        self._assert_refused("Invalid ruby positions")

    def test_invalid_ellipse(self):
        self.state["gasket_ellipse"] = {"cx": 1, "cy": 1, "rx": 2, "ry": 2}
        self._assert_refused("radii of at least 20 px")

    def test_malformed_sample_point(self):
        self.state["gasket_sample_point"] = {"x": 1}
        self._assert_refused("not readable")
