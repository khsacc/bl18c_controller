"""
StepEditorDialog field-preservation tests — apps/exp_scheduler/ui/step_editor.py
REORGANISATION_PLAN.md Phase 9 external review, round 2.

start_following/follow_sample_position have no (or, for follow_sample_position,
only one) editable field in the Visual step editor — their pages previously
called the bare Action class as `build()`, so editing an EXISTING step
(opened via double-click, then OK) silently reset every field it didn't show
a widget for back to the dataclass default, including a DSL/JSON-authored
`autofocus_enabled=False`. This file pins the fix: editing preserves the
original action's other fields; adding a brand-new step still gets defaults.
"""
import os
import sys
import types
import unittest

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

from PyQt6.QtWidgets import QApplication, QComboBox, QLineEdit

from apps.exp_scheduler.actions import (
    FollowSampleAction, StartFollowingAction, TakeDarkAction, TakeXrdAction,
)
from apps.exp_scheduler.ui.step_editor import StepEditorDialog

_app = QApplication.instance() or QApplication([])


class WaitTemperatureEditorTests(unittest.TestCase):
    def test_new_step_defaults_to_b_and_selection_survives_edit(self):
        dlg = StepEditorDialog()
        self.addCleanup(dlg.close)
        dlg._dev_combo.setCurrentText("LakeShore")
        dlg._op_combo.setCurrentText("wait_temperature")
        page = dlg._pages["wait_temperature"]
        sensor = next(
            combo for combo in page.widget.findChildren(QComboBox)
            if combo.itemText(0) == "T_A"
        )
        self.assertEqual(sensor.currentText(), "T_B")
        sensor.setCurrentText("T_A")
        dlg._on_ok()
        self.assertEqual(dlg.get_action().channel, "A")

        edit = StepEditorDialog(action=dlg.get_action())
        self.addCleanup(edit.close)
        edit._on_ok()
        self.assertEqual(edit.get_action(), dlg.get_action())


class TakeDarkVisualAvailabilityTests(unittest.TestCase):
    def test_new_steps_hide_dark_but_existing_steps_remain_editable(self):
        new_dialog = StepEditorDialog()
        self.addCleanup(new_dialog.close)
        new_dialog._dev_combo.setCurrentText("FPD (Rad-icon2022)")
        self.assertEqual(
            [new_dialog._op_combo.itemText(i) for i in range(new_dialog._op_combo.count())],
            ["take_xrd"],
        )

        old_dialog = StepEditorDialog(action=TakeDarkAction(exposure_ms=2500))
        self.addCleanup(old_dialog.close)
        self.assertEqual(old_dialog._op_combo.currentText(), "take_dark")
        old_dialog._on_ok()
        self.assertEqual(old_dialog.get_action(), TakeDarkAction(exposure_ms=2500))


class StartFollowingEditPreservesFieldsTests(unittest.TestCase):
    def test_editing_an_existing_step_preserves_autofocus_enabled_false(self):
        original = StartFollowingAction(
            reference_path="ref.png",
            interval_s=5.0,
            similarity_threshold=0.8,
            max_correction_per_step_um=2.0,
            camera_index=1,
            autofocus_enabled=False,
            autofocus_range_um=10.0,
            autofocus_steps=5,
        )
        dlg = StepEditorDialog(action=original)
        self.addCleanup(dlg.close)

        dlg._on_ok()
        rebuilt = dlg.get_action()

        self.assertIsInstance(rebuilt, StartFollowingAction)
        self.assertEqual(rebuilt.reference_path, "ref.png")
        self.assertEqual(rebuilt.interval_s, 5.0)
        self.assertEqual(rebuilt.similarity_threshold, 0.8)
        self.assertEqual(rebuilt.max_correction_per_step_um, 2.0)
        self.assertEqual(rebuilt.camera_index, 1)
        self.assertFalse(rebuilt.autofocus_enabled)
        self.assertEqual(rebuilt.autofocus_range_um, 10.0)
        self.assertEqual(rebuilt.autofocus_steps, 5)

    def test_adding_a_new_step_still_gets_defaults(self):
        # Sanity check for the other half of the contract: Add-Step mode
        # (action=None) must still produce a fresh, all-defaults action —
        # there is nothing to "preserve" when there was no prior step.
        dlg = StepEditorDialog(action=None)
        self.addCleanup(dlg.close)
        dlg._dev_combo.setCurrentText("Interactive Camera")
        dlg._op_combo.setCurrentText("start_following")

        dlg._on_ok()
        rebuilt = dlg.get_action()

        self.assertIsInstance(rebuilt, StartFollowingAction)
        self.assertEqual(rebuilt, StartFollowingAction())


class FollowSamplePositionEditPreservesFieldsTests(unittest.TestCase):
    def test_editing_duration_preserves_every_other_field(self):
        original = FollowSampleAction(
            duration_s=90.0,
            reference_path="ref.png",
            interval_s=5.0,
            similarity_threshold=0.7,
            max_correction_per_step_um=3.0,
            camera_index=1,
            autofocus_enabled=False,
            autofocus_range_um=10.0,
            autofocus_steps=5,
        )
        dlg = StepEditorDialog(action=original)
        self.addCleanup(dlg.close)

        dlg._on_ok()  # no field changed — duration widget was pre-filled from `original`
        rebuilt = dlg.get_action()

        self.assertIsInstance(rebuilt, FollowSampleAction)
        self.assertEqual(rebuilt.duration_s, 90.0)
        self.assertEqual(rebuilt.reference_path, "ref.png")
        self.assertEqual(rebuilt.interval_s, 5.0)
        self.assertEqual(rebuilt.similarity_threshold, 0.7)
        self.assertEqual(rebuilt.max_correction_per_step_um, 3.0)
        self.assertEqual(rebuilt.camera_index, 1)
        self.assertFalse(rebuilt.autofocus_enabled)
        self.assertEqual(rebuilt.autofocus_range_um, 10.0)
        self.assertEqual(rebuilt.autofocus_steps, 5)

    def test_changing_duration_applies_the_new_value_and_still_preserves_the_rest(self):
        # duration_s=120.0 (a whole number of minutes) so fill() selects the
        # "min" unit, matching what a real edit-in-minutes session looks
        # like (see _page_follow_sample_position()'s fill()).
        original = FollowSampleAction(duration_s=120.0, autofocus_enabled=False)
        dlg = StepEditorDialog(action=original)
        self.addCleanup(dlg.close)
        page = dlg._pages["follow_sample_position"]

        # Locate the duration spinbox via the page's own widget tree rather
        # than a private page-factory detail.
        from PyQt6.QtWidgets import QDoubleSpinBox
        dur_spin = page.widget.findChild(QDoubleSpinBox)
        self.assertIsNotNone(dur_spin)
        dur_spin.setValue(45.0)

        dlg._on_ok()
        rebuilt = dlg.get_action()

        self.assertEqual(rebuilt.duration_s, 45.0 * 60)  # unit combo stayed "min"
        self.assertFalse(rebuilt.autofocus_enabled)

    def test_adding_a_new_step_still_gets_defaults(self):
        dlg = StepEditorDialog(action=None)
        self.addCleanup(dlg.close)
        dlg._dev_combo.setCurrentText("Interactive Camera")
        dlg._op_combo.setCurrentText("follow_sample_position")

        dlg._on_ok()
        rebuilt = dlg.get_action()

        self.assertIsInstance(rebuilt, FollowSampleAction)
        self.assertTrue(rebuilt.autofocus_enabled)
        self.assertIsNone(rebuilt.reference_path)


class TakeXrdPrefixEditTests(unittest.TestCase):
    def test_new_step_inherits_global_prefix_and_can_override_it(self):
        dlg = StepEditorDialog(action=None)
        self.addCleanup(dlg.close)
        dlg._dev_combo.setCurrentText("FPD (Rad-icon2022)")
        dlg._op_combo.setCurrentText("take_xrd")
        page = dlg._pages["take_xrd"]
        prefix = next(
            edit for edit in page.widget.findChildren(QLineEdit)
            if edit.placeholderText() == "Use XRD Settings prefix"
        )

        self.assertIsNone(page.build().prefix)
        prefix.setText("sample_a")
        self.assertEqual(page.build().prefix, "sample_a")
        prefix.clear()
        self.assertIsNone(page.build().prefix)

    def test_existing_explicit_prefix_is_preserved(self):
        dlg = StepEditorDialog(action=TakeXrdAction(prefix="old_scan"))
        self.addCleanup(dlg.close)
        dlg._on_ok()
        self.assertEqual(dlg.get_action().prefix, "old_scan")


if __name__ == "__main__":
    unittest.main()
