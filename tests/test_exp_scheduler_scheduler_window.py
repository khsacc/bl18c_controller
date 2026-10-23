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
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock
import uuid
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

from apps.exp_scheduler.actions import ForLoopAction, SaveSnapshotAction, WaitAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.run_plan import build_run_plan
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.scheduler_settings import GlobalLimits
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.ui.dsl_editor import DslEditor
from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
from apps.exp_scheduler.ui.timeline_widget import TimelineWidget
from apps.exp_scheduler.ui import scheduler_window
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


class SchedulerProgressTests(unittest.TestCase):
    def test_run_log_expands_loops_and_resets_for_next_run(self):
        timeline = TimelineWidget()
        self.addCleanup(timeline.close)
        sequence = Sequence(actions=[
            ForLoopAction(var="i", values=[1, 2], body=[WaitAction(duration_s=0)]),
        ])
        timeline.set_sequence(sequence)
        timeline.show_run_plan(build_run_plan(sequence.actions))
        self.assertEqual(timeline._stack.currentIndex(), 1)
        self.assertFalse(timeline._btn_add.isEnabled())
        self.assertEqual(timeline._run_tree.topLevelItemCount(), 2)
        first = timeline._run_tree.topLevelItem(0)
        second = timeline._run_tree.topLevelItem(1)
        self.assertIn("i=1 (1/2)", first.text(1))
        self.assertIn("i=2 (2/2)", second.text(1))

        timeline.record_step_started(0, "2026-10-21 10:00:00")
        timeline.record_file_saved(0, "C:/data/first.tif")
        timeline.record_file_saved(0, "C:/data/first.csv")
        timeline.record_step_finished(0, "2026-10-21 10:00:01")
        timeline.record_step_started(1, "2026-10-21 10:01:00")
        # Each iteration keeps its own record — nothing is overwritten.
        self.assertEqual(first.text(2), "2026-10-21 10:00:00")
        self.assertEqual(first.text(3), "2026-10-21 10:00:01")
        self.assertEqual(first.text(4), "first.tif, first.csv")
        self.assertIn("C:/data/first.csv", first.toolTip(4))
        self.assertEqual(second.text(2), "2026-10-21 10:01:00")
        self.assertEqual(second.text(3), "")

        timeline.clear_run_progress()
        self.assertEqual(timeline._run_tree.topLevelItemCount(), 0)
        self.assertEqual(timeline._stack.currentIndex(), 0)
        self.assertTrue(timeline._btn_add.isEnabled())

    def test_run_log_toggle_keeps_editing_disabled_while_running(self):
        timeline = TimelineWidget()
        self.addCleanup(timeline.close)
        sequence = Sequence(actions=[WaitAction(duration_s=0)])
        timeline.set_sequence(sequence)
        timeline.show_run_plan(build_run_plan(sequence.actions))
        timeline.set_editing_enabled(False)
        timeline._btn_toggle_view.click()
        self.assertEqual(timeline._stack.currentIndex(), 0)
        self.assertFalse(timeline._btn_add.isEnabled())
        timeline.set_editing_enabled(True)
        self.assertTrue(timeline._btn_add.isEnabled())
        timeline._btn_toggle_view.click()
        self.assertEqual(timeline._stack.currentIndex(), 1)
        self.assertFalse(timeline._btn_add.isEnabled())

    def test_nested_script_loop_keeps_later_step_indices_aligned(self):
        sequence = Sequence(actions=[
            ForLoopAction(var="outer", values=[1], body=[
                ForLoopAction(var="inner", values=[1, 2], body=[WaitAction(duration_s=0)]),
            ]),
            WaitAction(duration_s=1),
        ])
        plan = build_run_plan(sequence.actions)
        self.assertEqual([step.index for step in plan], [0, 1, 2])
        self.assertEqual(plan[1].variables, {"outer": 1, "inner": 2})
        self.assertEqual(plan[2].loops, ())
        timeline = TimelineWidget()
        self.addCleanup(timeline.close)
        timeline.set_sequence(sequence)
        timeline.show_run_plan(plan)
        timeline.record_step_started(2, "2026-10-21 10:02:00")
        self.assertEqual(timeline._run_tree.topLevelItem(2).text(2), "2026-10-21 10:02:00")

    def test_runner_passes_each_iteration_its_loop_variables(self):
        sequence = Sequence(actions=[
            ForLoopAction(var="p", values=[0.5, 1.0], body=[
                WaitAction(duration_s=0), WaitAction(duration_s=0),
            ]),
            WaitAction(duration_s=0),
        ])
        runner = SequenceRunner(sequence, DeviceContext())
        runner._logger = unittest.mock.Mock()
        calls = []
        runner._execute_one = lambda action, var_context: calls.append(dict(var_context))
        started = []
        runner.step_started.connect(lambda index, description: started.append(index))
        runner.run()
        self.assertEqual(started, [0, 1, 2, 3, 4])
        self.assertEqual(calls, [{"p": 0.5}, {"p": 0.5}, {"p": 1.0}, {"p": 1.0}, {}])
        loop_lines = [c.args[0] for c in runner._logger.log_ops.call_args_list
                      if c.args[0].startswith("[LOOP]")]
        self.assertEqual(loop_lines, [
            "[LOOP] p = 0.5  (iteration 1/2)",
            "[LOOP] p = 1.0  (iteration 2/2)",
        ])

    def test_runner_emits_step_times_without_hardware(self):
        runner = SequenceRunner(Sequence(actions=[WaitAction(duration_s=0)]), DeviceContext())
        runner._logger = unittest.mock.Mock()
        started, finished, completed = [], [], []
        runner.step_started_at.connect(lambda index, time: started.append((index, time)))
        runner.step_finished_at.connect(lambda index, time: finished.append((index, time)))
        runner.step_completed.connect(completed.append)
        runner.run()
        self.assertEqual([index for index, _ in started], [0])
        self.assertEqual([index for index, _ in finished], [0])
        self.assertEqual(completed, [0])
        self.assertLessEqual(started[0][1], finished[0][1])

    def test_run_saves_loadable_sequence_before_executing(self):
        sequence = Sequence(actions=[WaitAction(duration_s=0)])
        snapshot = Sequence.from_dict(sequence.to_dict())
        snapshot.global_limits = {"ch3_minus_mm": 1.0, "ch3_plus_mm": 1.0}
        output_dir = Path(__file__).parent / f"_run_sequence_{uuid.uuid4().hex}"
        output_dir.mkdir()
        try:
            runner = SequenceRunner(sequence, DeviceContext(), data_dir=str(output_dir),
                                    sequence_snapshot=snapshot)
            errors = []
            saved_before_step = []
            runner.error_occurred.connect(lambda index, message: errors.append(message))
            runner.step_started.connect(
                lambda index, description: saved_before_step.append(
                    (runner._logger.log_dir / "sequence.json").is_file()
                )
            )
            runner.run()
            self.assertIsNotNone(runner._logger.log_dir, errors)
            self.assertEqual(runner._logger.run_dir.parent, output_dir)
            self.assertEqual(runner._logger.log_dir, runner._logger.run_dir / "log")
            first_path = runner._logger.log_dir / "sequence.json"
            self.assertEqual(Sequence.load(first_path).to_dict(), snapshot.to_dict())
            self.assertEqual(errors, [])
            self.assertEqual(saved_before_step, [True])

            second = SequenceRunner(sequence, DeviceContext(), data_dir=str(output_dir),
                                    sequence_snapshot=snapshot)
            second.run()
            self.assertNotEqual(second._logger.log_dir, runner._logger.log_dir)
            self.assertEqual(Sequence.load(first_path).to_dict(), snapshot.to_dict())
        finally:
            self.assertEqual(output_dir.resolve().parent, Path(__file__).parent.resolve())
            shutil.rmtree(output_dir)

    def test_sequence_save_failure_blocks_execution(self):
        runner = SequenceRunner(Sequence(actions=[WaitAction(duration_s=0)]), DeviceContext())
        errors, completed = [], []
        runner.error_occurred.connect(lambda index, message: errors.append(message))
        runner.sequence_completed.connect(lambda: completed.append(True))
        with (unittest.mock.patch.object(runner._logger, "start", side_effect=OSError("disk full")),
              unittest.mock.patch.object(runner, "_execute_actions") as execute):
            runner.run()
        execute.assert_not_called()
        self.assertEqual(completed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("disk full", errors[0])

    def test_worker_signals_update_visual_columns_on_gui_thread(self):
        timeline = TimelineWidget()
        self.addCleanup(timeline.close)
        timeline.set_sequence(Sequence(actions=[WaitAction(duration_s=0)]))
        runner = SequenceRunner(Sequence(actions=[WaitAction(duration_s=0)]), DeviceContext())
        runner._logger = unittest.mock.Mock()
        timeline.show_run_plan(runner.run_plan)
        runner.step_started_at.connect(timeline.record_step_started)
        runner.step_finished_at.connect(timeline.record_step_finished)
        runner.step_started.connect(lambda index, description: timeline.highlight_step(index))
        runner.step_completed.connect(timeline.mark_step_done)
        runner.start()
        self.assertTrue(runner.wait(2000))
        _app.processEvents()
        item = timeline._run_tree.topLevelItem(0)
        self.assertTrue(item.text(2))
        self.assertTrue(item.text(3))

    def test_runner_records_end_time_on_step_error(self):
        runner = SequenceRunner(Sequence(actions=[WaitAction(duration_s=0)]), DeviceContext())
        runner._logger = unittest.mock.Mock()
        runner._execute_one = unittest.mock.Mock(side_effect=RuntimeError("device failed"))
        finished, errors, completed, order = [], [], [], []
        runner.step_finished_at.connect(lambda index, time: (finished.append(index), order.append("finished")))
        runner.error_occurred.connect(lambda index, message: (errors.append((index, message)), order.append("error")))
        runner.step_completed.connect(completed.append)
        runner.run()
        self.assertEqual(finished, [0])
        self.assertEqual(errors, [(0, "device failed")])
        self.assertEqual(completed, [])
        self.assertEqual(order, ["finished", "error"])

    def test_snapshot_reports_the_file_after_saving(self):
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
        runner._current_step_idx = 3
        runner._logger._run_dir = Path("C:/data/run_20260101_000000")
        runner._require_camera_index = lambda index: None
        runner._get_camera_frame_after = lambda captured_after, operation: np.zeros((4, 4, 3), dtype=np.uint8)
        saved = []
        runner.file_saved.connect(lambda index, path: saved.append((index, path)))
        with (unittest.mock.patch.object(Path, "mkdir"),
              unittest.mock.patch("apps.exp_scheduler.runner.cv2.imwrite", return_value=True)):
            runner._do_save_snapshot(SaveSnapshotAction())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0], 3)
        self.assertEqual(Path(saved[0][1]).parent, Path("C:/data/run_20260101_000000/photo"))

        with (unittest.mock.patch.object(Path, "mkdir"),
              unittest.mock.patch("apps.exp_scheduler.runner.cv2.imwrite", return_value=False),
              self.assertRaises(RuntimeError)):
            runner._do_save_snapshot(SaveSnapshotAction())
        self.assertEqual(len(saved), 1)


class XrdCorrectionFileDefaultsTests(unittest.TestCase):
    def test_radicon_correction_values_inherit_until_explicitly_changed(self):
        prefs_path = unittest.mock.Mock()
        prefs_path.read_text.return_value = json.dumps({
            "dark_correct_enabled": True,
            "defect_correct_mode": "neg1",
            "defect_kernel_size": "5×5",
            "flip_v": False,
            "flip_h": True,
        })
        saved_settings = {"global_xrd": {"flip_h": False}}
        settings_path = unittest.mock.Mock()
        settings_path.read_text.side_effect = lambda **_: json.dumps(saved_settings)
        settings_path.write_text.side_effect = lambda data, **_: saved_settings.update(json.loads(data))
        with (unittest.mock.patch.object(scheduler_window, "_RADICON_PREFS_PATH", prefs_path),
              unittest.mock.patch.object(scheduler_window, "_SETTINGS_PATH", settings_path)):
            first = ExperimentalSchedulerWindow(DeviceContext())
            try:
                xrd = first._build_global_xrd()
                self.assertTrue(xrd.dark_enabled)
                self.assertTrue(xrd.defect_enabled)
                self.assertEqual(xrd.defect_mode, "neg1")
                self.assertEqual(xrd.defect_kernel, 5)
                self.assertFalse(first._xrd_defect_kernel_combo.isEnabled())
                self.assertFalse(xrd.flip_v)
                self.assertFalse(xrd.flip_h)  # Explicit scheduler choice wins.
                first._save_xrd_settings()
                self.assertNotIn("dark_enabled", saved_settings["global_xrd"])
                self.assertNotIn("defect_mode", saved_settings["global_xrd"])
                self.assertNotIn("flip_v", saved_settings["global_xrd"])
            finally:
                first.close()

            prefs_path.read_text.return_value = json.dumps({
                "dark_correct_enabled": False,
                "defect_correct_mode": "none",
                "flip_v": True,
                "flip_h": True,
            })
            second = ExperimentalSchedulerWindow(DeviceContext())
            try:
                xrd = second._build_global_xrd()
                self.assertFalse(xrd.dark_enabled)
                self.assertFalse(xrd.defect_enabled)
                self.assertFalse(second._xrd_defect_mode_combo.isEnabled())
                self.assertTrue(xrd.flip_v)
                self.assertFalse(xrd.flip_h)
            finally:
                second.close()

    def test_global_prefix_round_trips_through_saved_settings(self):
        saved_settings = {}
        settings_path = unittest.mock.Mock()
        settings_path.read_text.side_effect = lambda **_: json.dumps(saved_settings)
        settings_path.write_text.side_effect = (
            lambda data, **_: saved_settings.update(json.loads(data))
        )
        with (unittest.mock.patch.object(scheduler_window, "_SETTINGS_PATH", settings_path),
              unittest.mock.patch.object(scheduler_window, "_radicon_correction_file_defaults", return_value={})):
            first = ExperimentalSchedulerWindow(DeviceContext())
            try:
                self.assertEqual(first._build_global_xrd().prefix, "scan")
                self.assertEqual(first._build_global_xrd().filename_suffixes,
                                 ("datetime_full",))
                first._xrd_prefix_edit.setText("beamtime")
                first._xrd_suffix_chks["datetime_full"].setChecked(False)
                first._xrd_suffix_chks["datetime_time"].setChecked(True)
                first._xrd_suffix_chks["index"].setChecked(True)
                first._save_xrd_settings()
                self.assertEqual(saved_settings["global_xrd"]["prefix"], "beamtime")
                self.assertEqual(saved_settings["global_xrd"]["filename_suffixes"],
                                 ["datetime_time", "index"])
            finally:
                first.close()

            second = ExperimentalSchedulerWindow(DeviceContext())
            try:
                self.assertEqual(second._xrd_prefix_edit.text(), "beamtime")
                self.assertEqual(second._build_global_xrd().prefix, "beamtime")
                self.assertEqual(second._build_global_xrd().filename_suffixes,
                                 ("datetime_time", "index"))
            finally:
                second.close()

    def test_radicon_paths_fill_empty_fields_but_preserve_scheduler_choices(self):
        prefs_path = unittest.mock.Mock()
        prefs_path.read_text.return_value = json.dumps({
            "dark_file_path": "C:/radicon/dark.tif",
            "defect_file_path": "C:/radicon/defects.txt",
        })
        settings_path = unittest.mock.Mock()
        with (unittest.mock.patch.object(scheduler_window, "_RADICON_PREFS_PATH", prefs_path),
              unittest.mock.patch.object(scheduler_window, "_SETTINGS_PATH", settings_path)):
            for saved, expected in (
                ({}, ("C:/radicon/dark.tif", "C:/radicon/defects.txt")),
                ({"dark_file": "", "defect_file": ""},
                 ("C:/radicon/dark.tif", "C:/radicon/defects.txt")),
                ({"dark_file": "C:/scheduler/dark.tif", "defect_file": ""},
                 ("C:/scheduler/dark.tif", "C:/radicon/defects.txt")),
            ):
                with self.subTest(saved=saved):
                    settings_path.read_text.return_value = json.dumps({"global_xrd": saved})
                    window = ExperimentalSchedulerWindow(DeviceContext())
                    try:
                        self.assertEqual(window._xrd_dark_edit.text(), expected[0])
                        self.assertEqual(window._xrd_defect_edit.text(), expected[1])
                    finally:
                        window.close()

    def test_missing_or_invalid_radicon_prefs_leave_fields_empty(self):
        prefs_path = unittest.mock.Mock()
        with unittest.mock.patch.object(scheduler_window, "_RADICON_PREFS_PATH", prefs_path):
            prefs_path.read_text.side_effect = FileNotFoundError
            self.assertEqual(scheduler_window._radicon_correction_file_defaults(), {})
            prefs_path.read_text.side_effect = None
            prefs_path.read_text.return_value = "{invalid"
            self.assertEqual(scheduler_window._radicon_correction_file_defaults(), {})

    def test_inherited_files_follow_new_radicon_choices_on_next_launch(self):
        prefs_path = unittest.mock.Mock()
        settings_path = unittest.mock.Mock()
        saved_settings = {"global_xrd": {"dark_file": "", "defect_file": ""}}
        settings_path.read_text.side_effect = lambda **_: json.dumps(saved_settings)
        settings_path.write_text.side_effect = lambda data, **_: saved_settings.update(json.loads(data))
        with (unittest.mock.patch.object(scheduler_window, "_RADICON_PREFS_PATH", prefs_path),
              unittest.mock.patch.object(scheduler_window, "_SETTINGS_PATH", settings_path)):
            prefs_path.read_text.return_value = json.dumps({
                "dark_file_path": "C:/radicon/old-dark.tif",
                "defect_file_path": "C:/radicon/old-defects.txt",
            })
            first = ExperimentalSchedulerWindow(DeviceContext())
            first.close()
            self.assertEqual(saved_settings["global_xrd"]["dark_file"], "")
            self.assertEqual(saved_settings["global_xrd"]["defect_file"], "")

            prefs_path.read_text.return_value = json.dumps({
                "dark_file_path": "C:/radicon/new-dark.tif",
                "defect_file_path": "C:/radicon/new-defects.txt",
            })
            second = ExperimentalSchedulerWindow(DeviceContext())
            try:
                self.assertEqual(second._xrd_dark_edit.text(), "C:/radicon/new-dark.tif")
                self.assertEqual(second._xrd_defect_edit.text(), "C:/radicon/new-defects.txt")
            finally:
                second.close()


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

    def _start_mock_run(self, window):
        report = window._validate_dsl_text(_VALID_DSL)
        self.assertTrue(report.ok, report.errors)
        with unittest.mock.patch(
            "apps.exp_scheduler.ui.scheduler_window.SequenceRunner"
        ) as runner_cls:
            runner_cls.return_value.isRunning.return_value = False
            window._on_run()

    def test_run_passes_current_settings_without_changing_validated_sequence(self):
        window = self._window()
        report = window._validate_dsl_text(_VALID_DSL)
        self.assertTrue(report.ok, report.errors)
        certificate = window._certificate
        original = window._sequence.to_dict()

        with unittest.mock.patch(
            "apps.exp_scheduler.ui.scheduler_window.SequenceRunner"
        ) as runner_cls:
            runner_cls.return_value.isRunning.return_value = False
            window._on_run()

        snapshot = runner_cls.call_args.kwargs["sequence_snapshot"]
        self.assertEqual(snapshot.actions[0].to_dict(), window._sequence.actions[0].to_dict())
        self.assertEqual(snapshot.global_limits, window._limits_ui_to_dict())
        self.assertEqual(snapshot.global_positions, window._positions_ui_to_dict())
        self.assertEqual(snapshot.global_shutter, window._shutter_ui_to_dict())
        self.assertEqual(window._sequence.to_dict(), original)
        self.assertIs(window._certificate, certificate)

    def test_running_banner_shows_until_sequence_completes(self):
        window = self._window()
        self.assertTrue(window._run_banner.isHidden())
        self._start_mock_run(window)

        self.assertFalse(window._run_banner.isHidden())
        self.assertIn("Sequence Running", window._run_banner.text())
        window._on_step_started(0, "Wait 1 s")
        self.assertIn("Sequence Running", window._run_banner.text())
        with unittest.mock.patch("apps.exp_scheduler.ui.scheduler_window.play_current_sound"):
            window._on_sequence_completed()
        self.assertTrue(window._run_banner.isHidden())

    def test_run_switches_from_script_to_visual_without_reconverting(self):
        window = self._window()
        window._tabs.setCurrentIndex(1)
        with unittest.mock.patch.object(window._dsl_editor, "convert_to_visual") as convert:
            self._start_mock_run(window)
        self.assertEqual(window._tabs.currentIndex(), 0)
        convert.assert_not_called()
        self.assertFalse(window._timeline._btn_edit.isEnabled())
        with unittest.mock.patch("apps.exp_scheduler.ui.scheduler_window.play_current_sound"):
            window._on_sequence_completed()
        # The run log stays on screen after the run; editing resumes once
        # the user switches back to the editor view.
        self.assertEqual(window._timeline._stack.currentIndex(), 1)
        self.assertFalse(window._timeline._btn_edit.isEnabled())
        window._timeline._btn_toggle_view.click()
        self.assertTrue(window._timeline._btn_edit.isEnabled())

    def test_running_banner_stays_visible_during_stop_request(self):
        window = self._window()
        self._start_mock_run(window)

        window._on_stop()

        self.assertFalse(window._run_banner.isHidden())
        self.assertIn("STILL RUNNING", window._run_banner.text())
        with unittest.mock.patch("apps.exp_scheduler.ui.scheduler_window.QMessageBox.warning"):
            window._on_sequence_stopped()
        self.assertTrue(window._run_banner.isHidden())

    def test_running_banner_hides_after_emergency_stop_error(self):
        window = self._window()
        self._start_mock_run(window)

        window._on_emergency_stop()

        self.assertFalse(window._run_banner.isHidden())
        self.assertIn("STILL RUNNING", window._run_banner.text())
        with unittest.mock.patch("apps.exp_scheduler.ui.scheduler_window.QMessageBox.critical"):
            window._on_error_occurred(0, "device error")
        self.assertTrue(window._run_banner.isHidden())

    def test_visual_validate_clears_previous_run_progress(self):
        window = self._window()
        window._sequence = Sequence(actions=[WaitAction(duration_s=1.0)])
        window._timeline.set_sequence(window._sequence)
        window._timeline.show_run_plan(build_run_plan(window._sequence.actions))
        item = window._timeline._run_tree.topLevelItem(0)
        base_colour = item.background(0).color()
        window._timeline.record_step_started(0, "2026-10-21 10:00:00")
        window._timeline.record_file_saved(0, "C:/data/previous.tif")
        window._timeline.record_step_finished(0, "2026-10-21 10:00:01")
        window._timeline.mark_step_done(0)
        self.assertNotEqual(item.background(0).color(), base_colour)
        self.assertEqual(item.text(4), "previous.tif")

        window._on_validate_visual()

        self.assertEqual(window._timeline._run_tree.topLevelItemCount(), 0)
        self.assertEqual(window._timeline._run_files, {})
        self.assertEqual(window._timeline._stack.currentIndex(), 0)
        self.assertTrue(window._timeline._btn_toggle_view.isHidden())

    def test_script_validate_failure_clears_previous_run_progress(self):
        window = self._window()
        original = Sequence(actions=[WaitAction(duration_s=1.0)])
        window._sequence = original
        window._timeline.set_sequence(original)
        window._tabs.setCurrentIndex(1)
        window._dsl_editor.set_text(_SYNTAX_ERROR_DSL)
        window._timeline.show_run_plan(build_run_plan(original.actions))
        window._timeline.record_step_started(0, "2026-10-21 10:00:00")
        window._timeline.record_file_saved(0, "C:/data/previous.tif")
        window._timeline.record_step_finished(0, "2026-10-21 10:00:01")
        window._timeline.mark_step_done(0)

        window._dsl_editor._btn_validate.click()

        self.assertEqual(window._timeline._run_tree.topLevelItemCount(), 0)
        self.assertEqual(window._timeline._run_files, {})
        self.assertEqual(window._timeline._stack.currentIndex(), 0)
        self.assertTrue(window._timeline._btn_toggle_view.isHidden())
        self.assertIs(window._sequence, original)
        self.assertFalse(window._validated)

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

    def test_global_position_change_disables_run(self):
        window = self._validated()
        window._pos_ch9_in.setValue(window._pos_ch9_in.value() + 1)
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
