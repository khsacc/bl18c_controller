"""
UI wiring tests for apps/exp_scheduler/ui/dsl_editor.py and
ui/scheduler_window.py — REORGANISATION_PLAN.md Phase 7 (§7 Phase 7).

This is this repository's first PyQt-widget-level test module. The DSL
compile/preflight logic itself is exercised elsewhere (test_exp_scheduler_
pre_validator.py, test_exp_scheduler_validation_service.py); the concern
here is purely the *wiring* — whether Validate/Convert-to-Visual/Run reach
the validator exactly once, whether a failed attempt always resets the
host's validated/Run-enabled state, and whether a successful one applies
the Sequence exactly once. An external review of this Phase's initial plan
flagged this wiring as the most fragile part and asked for it to be
covered by an automated test rather than manual verification alone.
"""
import dataclasses
import os
import sys
import tempfile
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
from PyQt6.QtWidgets import QApplication, QMessageBox

from apps.exp_scheduler.actions import WaitAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.scheduler_settings import GlobalLimits
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.ui.dsl_editor import DslEditor
from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
from apps.exp_scheduler.validator.models import ValidationReport
from apps.exp_scheduler.validator.pre_validator import PreValidator

from tests.exp_scheduler_fakes import FakePace5000, FakeStageController

_app = QApplication.instance() or QApplication([])

_FULLY_CONFIGURED_LIMITS = GlobalLimits(
    ch3_minus_mm=1.0, ch3_plus_mm=1.0,
    ch4_minus_mm=1.0, ch4_plus_mm=1.0,
    ch5_minus_mm=1.0, ch5_plus_mm=1.0,
)

_VALID_DSL = 'wait(duration=1, unit="s")'
_SYNTAX_ERROR_DSL = "this is not ) valid (("
# Compiles, but fails preflight (no stage controller connected — see
# validator/checks/stage.py::check_stage()).
_PREFLIGHT_FAIL_DSL = "move_absolute(ch=4, value=1000)"
# Produces exactly one warning and zero errors (PACE5000 fake in Control
# mode, set_pressure not followed by a wait_pressure).
_WARNING_ONLY_DSL = 'set_pressure(pressure=1.0, unit="MPa", rate=1.0, rate_unit="MPa/min")'


class DslEditorValidatorWiringTests(unittest.TestCase):
    """DslEditor alone, driven by a mock validator — no scheduler window."""

    def _editor_with_mock_validator(self):
        editor = DslEditor()
        validator = unittest.mock.MagicMock(return_value=ValidationReport())
        editor.set_validator(validator)
        return editor, validator

    def test_validate_calls_validator_exactly_once_with_raw_text(self):
        editor, validator = self._editor_with_mock_validator()
        editor.set_text(_VALID_DSL)

        editor._on_validate()

        validator.assert_called_once_with(_VALID_DSL)

    def test_validate_calls_validator_even_on_syntax_error(self):
        # The whole point of Phase 7's redesign: DslEditor never
        # short-circuits before reaching the host, even for text that
        # will not compile.
        editor, validator = self._editor_with_mock_validator()
        editor.set_text(_SYNTAX_ERROR_DSL)

        editor._on_validate()

        validator.assert_called_once_with(_SYNTAX_ERROR_DSL)

    def test_validate_calls_validator_on_empty_text_too(self):
        editor, validator = self._editor_with_mock_validator()
        editor.set_text("")

        editor._on_validate()

        validator.assert_called_once_with("")

    def test_convert_emits_sequence_changed_only_when_report_ok(self):
        editor, validator = self._editor_with_mock_validator()
        editor.set_text(_VALID_DSL)
        seq = Sequence(actions=[])
        validator.return_value = ValidationReport(sequence=seq)  # ok (no errors)
        emitted = []
        editor.sequence_changed.connect(emitted.append)

        editor._on_convert()

        validator.assert_called_once_with(_VALID_DSL)
        self.assertEqual(emitted, [seq])

    def test_convert_does_not_emit_sequence_changed_when_report_not_ok(self):
        from apps.exp_scheduler.validator.models import (
            Diagnostic, Severity, ValidationPhase,
        )
        editor, validator = self._editor_with_mock_validator()
        editor.set_text(_PREFLIGHT_FAIL_DSL)
        validator.return_value = ValidationReport(diagnostics=[
            Diagnostic(Severity.ERROR, "x", "nope", ValidationPhase.PREFLIGHT)
        ])
        emitted = []
        editor.sequence_changed.connect(emitted.append)

        editor._on_convert()

        validator.assert_called_once_with(_PREFLIGHT_FAIL_DSL)
        self.assertEqual(emitted, [])

    def test_editing_the_editor_text_emits_text_edited(self):
        # High-severity regression test: QPlainTextEdit.textChanged was
        # never connected to anything, so a host had no way to learn that
        # the on-screen DSL text had changed since the last Validate/
        # Convert — see the companion scheduler_window-level test below.
        editor = DslEditor()
        emitted = []
        editor.text_edited.connect(lambda: emitted.append(1))

        editor._editor.setPlainText('wait(duration=1, unit="s")')

        self.assertEqual(len(emitted), 1)

    def test_set_text_does_not_emit_text_edited(self):
        # set_text() is the host's own repopulation path (e.g. switching to
        # the Script tab re-renders the already-validated Sequence as DSL
        # text) — it must not look like a user edit, or the certificate
        # from a Visual-tab Validate would be discarded the instant the
        # user merely looks at the Script tab.
        editor = DslEditor()
        emitted = []
        editor.text_edited.connect(lambda: emitted.append(1))

        editor.set_text('wait(duration=1, unit="s")')

        self.assertEqual(emitted, [])

    def test_set_sequence_does_not_emit_text_edited(self):
        editor = DslEditor()
        emitted = []
        editor.text_edited.connect(lambda: emitted.append(1))

        editor.set_sequence(Sequence(actions=[WaitAction(duration_s=5.0)]))

        self.assertEqual(emitted, [])


class SchedulerWindowValidationWiringTests(unittest.TestCase):
    """ExperimentalSchedulerWindow constructed with an all-disconnected
    DeviceContext — real object, not a mock, so we exercise the actual
    _validate_dsl_text/_on_validate_visual wiring end to end."""

    def _window(self, ctx=None):
        window = ExperimentalSchedulerWindow(ctx if ctx is not None else DeviceContext())
        self.addCleanup(window.close)
        return window

    def test_syntax_error_disables_run(self):
        window = self._window()
        report = window._validate_dsl_text(_SYNTAX_ERROR_DSL)

        self.assertFalse(report.ok)
        self.assertFalse(window._validated)
        self.assertFalse(window._btn_run.isEnabled())

    def test_empty_script_disables_run(self):
        window = self._window()
        report = window._validate_dsl_text("")

        self.assertFalse(report.ok)
        self.assertFalse(window._validated)
        self.assertFalse(window._btn_run.isEnabled())
        self.assertIn("シーケンスにアクションが一つもありません", report.errors)

    def test_previously_validated_state_is_reset_by_a_later_syntax_error(self):
        window = self._window()
        good = window._validate_dsl_text(_VALID_DSL)
        self.assertTrue(good.ok)
        self.assertTrue(window._btn_run.isEnabled())

        bad = window._validate_dsl_text(_SYNTAX_ERROR_DSL)

        self.assertFalse(bad.ok)
        self.assertFalse(window._validated)
        self.assertFalse(window._btn_run.isEnabled())

    def test_preflight_failure_does_not_change_visual_sequence_or_timeline(self):
        window = self._window()
        original = Sequence(actions=[], name="original")
        window._sequence = original
        window._timeline.set_sequence(original)

        report = window._validate_dsl_text(_PREFLIGHT_FAIL_DSL)

        self.assertFalse(report.ok)
        self.assertIs(window._sequence, original)
        self.assertFalse(window._validated)

    def test_success_applies_sequence_exactly_once(self):
        window = self._window()
        with unittest.mock.patch.object(
            window._timeline, "set_sequence", wraps=window._timeline.set_sequence,
        ) as set_sequence:
            report = window._validate_dsl_text(_VALID_DSL)

        self.assertTrue(report.ok)
        set_sequence.assert_called_once_with(report.sequence)
        self.assertIs(window._sequence, report.sequence)

    def test_warning_only_dsl_enables_run(self):
        window = self._window(DeviceContext(pace5000=FakePace5000(output_state="1")))
        report = window._validate_dsl_text(_WARNING_ONLY_DSL)

        self.assertTrue(report.ok)
        self.assertTrue(report.warnings)
        self.assertFalse(report.errors)
        self.assertTrue(window._validated)
        self.assertTrue(window._btn_run.isEnabled())

    def test_convert_to_visual_calls_pre_validator_exactly_once(self):
        # Must start on the Script tab (index 1), not the default Visual
        # tab: switching to Visual from Script re-enters _on_tab_changed()
        # (QTabWidget.currentChanged fires synchronously from
        # setCurrentIndex()), which previously re-triggered auto-convert
        # and called PreValidator.validate() a second time for one click —
        # a regression the earlier version of this test (which never left
        # the Visual tab, so _last_tab_index was never 1) did not exercise.
        window = self._window()
        window._tabs.setCurrentIndex(1)
        window._dsl_editor.set_text(_VALID_DSL)
        with unittest.mock.patch.object(
            PreValidator, "validate", wraps=PreValidator.validate, autospec=True,
        ) as validate:
            window._dsl_editor._btn_convert.click()

        self.assertEqual(validate.call_count, 1)
        self.assertEqual(window._tabs.currentIndex(), 0)  # switched to Visual

    def test_leaving_script_tab_without_convert_button_still_auto_converts_once(self):
        # The companion path to the above: switching tabs manually (not
        # clicking Convert) must still trigger auto-convert exactly once —
        # the re-entrancy guard must not suppress this legitimate trigger.
        window = self._window()
        window._tabs.setCurrentIndex(1)
        window._dsl_editor.set_text(_VALID_DSL)
        with unittest.mock.patch.object(
            PreValidator, "validate", wraps=PreValidator.validate, autospec=True,
        ) as validate:
            window._tabs.setCurrentIndex(0)

        self.assertEqual(validate.call_count, 1)
        self.assertEqual(
            [a.describe() for a in window._sequence.actions],
            ["Wait 1 s"],
        )


class _IsolatedSettingsTestCase(unittest.TestCase):
    """Redirects ui/scheduler_window.py's `_SETTINGS_PATH` to a throwaway
    temp file for the duration of each test.

    `ExperimentalSchedulerWindow` persists Global Limits/XRD/Camera/Follow
    values to a real on-disk JSON file on close and reloads them on
    construction (`_restore_settings()`/`closeEvent()`) — without this
    isolation, a test that changes one of those values (as every test below
    does, to trigger REORGANISATION_PLAN.md Phase 8's certificate
    invalidation) leaks that value into the real settings file, silently
    changing the *next* test's (or the next `python -m unittest` process's)
    starting widget state and making `.setText()`/`.setValue()` calls that
    happen to match a leftover value emit no change signal at all.
    """

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = unittest.mock.patch(
            "apps.exp_scheduler.ui.scheduler_window._SETTINGS_PATH",
            Path(tmpdir.name) / "scheduler_window_settings.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class Phase8CertificateInvalidationTests(_IsolatedSettingsTestCase):
    """Any fingerprinted Global setting (or Sequence/timeline) change made
    after a successful Validate must discard the certificate and disable
    Run — REORGANISATION_PLAN.md Phase 8 (§7 Phase 8, item 2)."""

    def _window(self, ctx=None, main_window=None):
        window = ExperimentalSchedulerWindow(
            ctx if ctx is not None else DeviceContext(), main_window=main_window,
        )
        self.addCleanup(window.close)
        return window

    def _validated(self, ctx=None, dsl=_VALID_DSL):
        window = self._window(ctx)
        report = window._validate_dsl_text(dsl)
        self.assertTrue(report.ok, report.errors)
        self.assertIsNotNone(window._certificate)
        self.assertTrue(window._btn_run.isEnabled())
        return window

    def test_global_limit_change_disables_run(self):
        window = self._validated()
        window._lim_ch3_minus.setValue(window._lim_ch3_minus.value() + 0.5)

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_xrd_oscillation_speed_change_disables_run(self):
        # The oscillation speed radio buttons are disabled while "Oscillate"
        # is unchecked (_toggle_osc()) — a disabled QRadioButton ignores
        # .click(), so oscillation must be enabled *before* Validate for
        # this test to exercise the speed group's own wiring in isolation.
        window = self._window()
        window._xrd_osc_chk.setChecked(True)
        report = window._validate_dsl_text(_VALID_DSL)
        self.assertTrue(report.ok, report.errors)
        self.assertIsNotNone(window._certificate)

        other = next(b for b in window._xrd_osc_speed_group.buttons() if not b.isChecked())
        other.click()

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_follow_autofocus_speed_change_disables_run(self):
        window = self._validated()
        other = next(b for b in window._follow_af_speed_group.buttons() if not b.isChecked())

        other.click()

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_follow_autofocus_peak_change_disables_run(self):
        window = self._validated()
        other = (
            window._follow_af_peak_highest
            if window._follow_af_peak_gaussian.isChecked()
            else window._follow_af_peak_gaussian
        )

        other.click()

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_global_camera_change_disables_run(self):
        window = self._validated()

        window._snapshot_save_dir_edit.setText("/tmp/some-other-snapshot-dir")

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_load_reference_image_disables_run(self):
        window = self._validated()
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "ref.png")
            cv2.imwrite(img_path, np.zeros((4, 4, 3), dtype=np.uint8))
            with unittest.mock.patch(
                "apps.exp_scheduler.ui.scheduler_window.QFileDialog.getOpenFileName",
                return_value=(img_path, ""),
            ):
                window._on_load_ref_file()

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_timeline_edit_disables_run(self):
        window = self._validated()

        window._on_timeline_changed()

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_editing_dsl_text_after_validate_disables_run(self):
        # High-severity regression test found in external review: editing
        # the Script tab's text after a successful Validate left Run
        # enabled and the certificate intact, because
        # QPlainTextEdit.textChanged was never connected to invalidation.
        # revalidate_for_run() checks window._sequence (only updated by a
        # successful Validate/Convert), not the editor's live text, so Run
        # would silently execute the stale, previously-validated Sequence
        # instead of what is now on screen. Edits the raw QPlainTextEdit
        # directly (not via DslEditor.set_text(), which is the host's own
        # repopulation path and must NOT trigger this — see
        # DslEditorValidatorWiringTests) to simulate an actual user
        # keystroke.
        window = self._validated(dsl='wait(duration=1, unit="s")')

        window._dsl_editor._editor.setPlainText('wait(duration=999, unit="s")')

        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_switching_to_script_tab_after_validate_does_not_invalidate(self):
        # Companion sanity check for the fix above: switching to the Script
        # tab calls DslEditor.set_sequence() (-> set_text()), which
        # repopulates the editor from the already-validated Sequence — a
        # programmatic change, not a user edit — and must NOT discard a
        # still-valid certificate merely because the user looked at the
        # Script tab.
        window = self._validated()

        window._tabs.setCurrentIndex(1)  # Visual -> Script

        self.assertIsNotNone(window._certificate)
        self.assertTrue(window._btn_run.isEnabled())


class Phase8RunGateOrderingTests(_IsolatedSettingsTestCase):
    """`close_all_sub_windows()` / `SequenceRunner` must never be invoked
    when the Run gate rejects a Run attempt, for any reason — external
    review round 1 item 4 and round 3 (REORGANISATION_PLAN.md §7 Phase 8,
    items 9-10)."""

    def _window_with_main(self, ctx=None):
        main_window = unittest.mock.MagicMock()
        main_window.close_all_sub_windows.return_value = []
        window = ExperimentalSchedulerWindow(
            ctx if ctx is not None else DeviceContext(), main_window=main_window,
        )
        self.addCleanup(window.close)
        return window, main_window

    def _validate(self, window, dsl=_VALID_DSL):
        report = window._validate_dsl_text(dsl)
        self.assertTrue(report.ok, report.errors)
        self.assertIsNotNone(window._certificate)
        return report

    def _run_with_patches(self, window, question_answer=QMessageBox.StandardButton.No):
        with unittest.mock.patch(
            "apps.exp_scheduler.ui.scheduler_window.SequenceRunner"
        ) as runner_cls, unittest.mock.patch(
            "apps.exp_scheduler.ui.scheduler_window.QMessageBox.critical"
        ), unittest.mock.patch(
            "apps.exp_scheduler.ui.scheduler_window.QMessageBox.question",
            return_value=question_answer,
        ):
            # A MagicMock's .isRunning() is truthy by default — without this,
            # a Run that does reach SequenceRunner construction would make
            # window.close() (in this test's addCleanup) think a sequence
            # is running and pop a real, unmocked confirmation QMessageBox
            # that blocks forever under the offscreen platform.
            runner_cls.return_value.isRunning.return_value = False
            window._on_run()
        return runner_cls

    def _assert_run_blocked(self, main_window, runner_cls):
        main_window.close_all_sub_windows.assert_not_called()
        runner_cls.assert_not_called()

    def test_no_certificate_blocks_run(self):
        window, main_window = self._window_with_main()
        # Never validated — window._certificate is None straight from
        # construction.

        runner_cls = self._run_with_patches(window)

        self._assert_run_blocked(main_window, runner_cls)

    def test_sequence_changed_blocks_run(self):
        window, main_window = self._window_with_main()
        self._validate(window)
        window._certificate = dataclasses.replace(
            window._certificate, sequence_fingerprint="stale-fingerprint",
        )

        runner_cls = self._run_with_patches(window)

        self._assert_run_blocked(main_window, runner_cls)

    def test_settings_changed_blocks_run(self):
        window, main_window = self._window_with_main()
        self._validate(window)
        window._certificate = dataclasses.replace(
            window._certificate, settings_fingerprint="stale-fingerprint",
        )

        runner_cls = self._run_with_patches(window)

        self._assert_run_blocked(main_window, runner_cls)

    def test_stage_moved_since_validate_blocks_run(self):
        controller = FakeStageController()
        window, main_window = self._window_with_main(DeviceContext(controller=controller))
        self._validate(window)
        controller.positions[4] = 999999  # moved via another window/process

        runner_cls = self._run_with_patches(window)

        self._assert_run_blocked(main_window, runner_cls)

    def test_device_context_changed_blocks_run(self):
        controller = FakeStageController()
        window, main_window = self._window_with_main(DeviceContext(controller=controller))
        self._validate(window)
        # Same positions, genuinely different instance.
        window._ctx.controller = FakeStageController()

        runner_cls = self._run_with_patches(window)

        self._assert_run_blocked(main_window, runner_cls)

    def test_fresh_preflight_error_blocks_run(self):
        controller = FakeStageController()
        window, main_window = self._window_with_main(DeviceContext(controller=controller))
        self._validate(window)
        # Unrelated to the certificate's fingerprint/device identity: every
        # channel read now fails, producing genuine PREFLIGHT-phase errors.
        # This also independently trips run_gate.stage_baseline_incomplete
        # (both must block Run) — this test only asserts that neither side
        # effect below escapes, regardless of which Diagnostic caused it.
        controller.fail_on = {"get_ch_pos"}

        runner_cls = self._run_with_patches(window)

        self._assert_run_blocked(main_window, runner_cls)

    def test_warning_dialog_declined_blocks_run(self):
        window, main_window = self._window_with_main(
            DeviceContext(pace5000=FakePace5000(output_state="1")),
        )
        report = self._validate(window, dsl=_WARNING_ONLY_DSL)
        self.assertTrue(report.warnings)

        runner_cls = self._run_with_patches(
            window, question_answer=QMessageBox.StandardButton.No,
        )

        self._assert_run_blocked(main_window, runner_cls)

    def test_normal_path_closes_windows_before_starting_runner(self):
        window, main_window = self._window_with_main()
        self._validate(window)

        with unittest.mock.patch(
            "apps.exp_scheduler.ui.scheduler_window.SequenceRunner"
        ) as runner_cls:
            runner_cls.return_value.isRunning.return_value = False
            parent = unittest.mock.Mock()
            parent.attach_mock(main_window.close_all_sub_windows, "close_all_sub_windows")
            parent.attach_mock(runner_cls, "SequenceRunner")
            window._on_run()

        main_window.close_all_sub_windows.assert_called_once()
        runner_cls.assert_called_once()
        call_names = [c[0] for c in parent.mock_calls]
        self.assertLess(
            call_names.index("close_all_sub_windows"),
            call_names.index("SequenceRunner"),
        )

    def test_save_after_validate_preserves_certificate_and_allows_run(self):
        # Regression test: _on_save() used to stamp the current Global
        # settings directly onto self._sequence before saving, which
        # changed self._sequence.to_dict() — and so the certificate's
        # sequence_fingerprint — on every Save. That made the very next
        # Run get rejected with run_gate.sequence_changed even though the
        # user never edited the Sequence itself.
        window, main_window = self._window_with_main()
        certificate_before = self._validate(window).certificate

        with tempfile.TemporaryDirectory() as tmp:
            save_path = os.path.join(tmp, "seq.json")
            with unittest.mock.patch(
                "apps.exp_scheduler.ui.scheduler_window.QFileDialog.getSaveFileName",
                return_value=(save_path, ""),
            ):
                window._on_save()
            self.assertTrue(os.path.exists(save_path))

        self.assertIs(window._certificate, certificate_before)
        self.assertTrue(window._btn_run.isEnabled())

        runner_cls = self._run_with_patches(window)

        main_window.close_all_sub_windows.assert_called_once()
        runner_cls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
