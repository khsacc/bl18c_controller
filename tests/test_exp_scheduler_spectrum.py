from __future__ import annotations

import csv
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import cv2

from apps.exp_scheduler.actions import FpdOutMicroscopeInAction, TakeSpectrumAction, action_from_dict
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl.compiler import DslCompiler
from apps.exp_scheduler.nearby_search import build_search_axes, search_positions
from apps.exp_scheduler import runner as runner_module
from apps.exp_scheduler.runner import SequenceRunner, RunnerError
from apps.exp_scheduler.scheduler_settings import (
    GlobalFollowSettings, GlobalLimits, GlobalPositions, GlobalSpectrumSettings,
)
from apps.exp_scheduler.spectrum_quality import SpectrumQuality, assess_spectrum, next_em_gain
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator
from utils.stage.control_stage_sim import PM16CControllerSim


class SpectrumActionTests(unittest.TestCase):
    def _quality_runner(self, save_dir, *, settings_overrides=None):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        with ctrl._state_lock:
            for ch, value in ((3, 40000), (4, 20000), (5, 20000)):
                ctrl._positions[ch] = ctrl._targets[ch] = value
        settings = GlobalSpectrumSettings(
            offset_ch4_pulse=1, offset_ch5_pulse=0,
            settle_ms=0, **(settings_overrides or {}),
        )
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=ctrl),
            global_limits=GlobalLimits(
                ch3_minus_mm=10, ch3_plus_mm=10,
                ch4_minus_mm=10, ch4_plus_mm=10,
                ch5_minus_mm=10, ch5_plus_mm=10,
            ), global_spectrum=settings,
        )
        class Logger:
            run_dir = Path(save_dir)

            def __init__(self):
                self.ops = []
                self.science = []
            def log_ops(self, message):
                self.ops.append(message)
            def log_science(self, event, **values):
                self.science.append((event, values))
        runner._logger = Logger()
        runner._baseline_pos = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        runner._motion_lease = ctrl.acquire_motion("test", "spectrum quality")
        self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
        return runner, ctrl

    def test_saved_exports_share_selected_filename_without_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, _ = self._quality_runner(
                temporary, settings_overrides={"filename_suffixes": ()},
            )
            with patch.object(runner_module, "assess_spectrum", return_value=SpectrumQuality(
                    False, True, r_squared=0.99)):
                runner._do_take_spectrum(TakeSpectrumAction(prefix="ruby"), 0)
                runner._do_take_spectrum(TakeSpectrumAction(prefix="ruby"), 1)
            save_dir = Path(temporary, "spectrum")
            for stem in ("ruby_attempt_01", "ruby_attempt_01_2"):
                for extension in (".npz", ".csv", ".png"):
                    self.assertTrue((save_dir / f"{stem}{extension}").exists())
            with (save_dir / "ruby_master_all_files.csv").open(
                    newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["spectrum_file"] for row in rows],
                             ["ruby_attempt_01.npz", "ruby_attempt_01_2.npz"])
            for row in rows:
                self.assertEqual(row["ruby_name"], "Default")
                self.assertEqual(row["quality_outcome"], "passed")
                self.assertEqual(row["fit_r_squared"], "0.99")
                self.assertEqual(row["peak1_nm"], "694.2")
                self.assertLessEqual(row["start_time"], row["end_time"])

    def test_r_squared_quality_and_invalid_fit(self):
        response = {
            "saturated": False, "y": [10, 20, 11, 19, 10],
            "fit": {"success": True, "y_fit": [12, 21, 11, 18, 8],
                    "fit": {"R2": 0.95, "peaks": [
                        {"amplitude": 12}, {"amplitude": 6},
                    ]}},
        }
        quality = assess_spectrum(response)
        self.assertTrue(quality.fit_success)
        self.assertEqual(quality.r_squared, 0.95)
        self.assertTrue(assess_spectrum({
            "saturated": False, "fit": {"success": True, "fit": {"R2": 0.95}},
        }).fit_success)
        self.assertTrue(assess_spectrum({"saturated": True}).saturated)
        self.assertFalse(assess_spectrum({"saturated": False, "fit": {"success": False}}).fit_success)
        self.assertIsNotNone(assess_spectrum({"fit": response["fit"]}).fit_error)
        self.assertIsNotNone(assess_spectrum({
            "saturated": False, "fit": {"success": True, "fit": {"R2": float("nan")}},
        }).fit_error)

    def test_em_gain_steps_respect_device_grid_and_global_ceiling(self):
        self.assertEqual([next_em_gain(gain, 10) for gain in (1, 2, 4, 8, 10)],
                         [2, 4, 8, 10, None])
        self.assertEqual(next_em_gain(3, 10, minimum=1, increment=2), 7)
        self.assertEqual(next_em_gain(7, 8, minimum=1, increment=2), None)

    def test_em_gain_retries_before_accumulations(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            results = [SpectrumQuality(False, True, r_squared=0.94)] * 5 + [
                SpectrumQuality(False, True, r_squared=0.95),
            ]
            with patch.object(runner_module, "assess_spectrum", side_effect=results):
                runner._do_take_spectrum(TakeSpectrumAction(), 0)
            paths = sorted(Path(temporary, "spectrum").glob("*.npz"))
            self.assertEqual(len(paths), 6)
            measured = []
            for path in paths:
                with np.load(path) as data:
                    measured.append((int(data["measured_em_gain"]),
                                     int(data["requested_accumulations"])))
                    self.assertEqual(int(data["quality_em_gain_max"]), 10)
                    self.assertEqual(str(data["quality_baseline_model"]), "linear")
            self.assertEqual(measured, [(1, 1), (2, 1), (4, 1), (8, 1), (10, 1), (10, 2)])
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_remote_em_gain_is_changed_before_accumulations(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            runner._global_spectrum.baseline_model = "quadratic"
            calls = []

            class Reader:
                exposure_time_s = None
                accumulations = None
                timeout_s = None
                gain = 0

                def acquire_spectrum_with_fit(self, **kwargs):
                    if kwargs.get("baseline_model") != "quadratic":
                        raise AssertionError("Selected fit background was not sent to FRP")
                    calls.append(("acquire", self.gain, self.accumulations))
                    r2 = 0.94 if self.gain == 0 else 0.95
                    return {"x": [690.0], "y_raw": [100.0], "y": [100.0],
                            "saturated": False,
                            "fit": {"success": True, "fit": {"R2": r2}}}

                def set_em_gain(self, gain):
                    calls.append(("gain", gain))
                    self.gain = gain
                    return gain

            with patch.object(runner, "_align_to_xrd_reference"):
                runner._acquire_spectrum_at_ruby(
                    TakeSpectrumAction(save=False), 0, runner._global_spectrum,
                    {3: 0, 4: 1, 5: 0}, 0.1, False, Reader(), (),
                    {"available": True, "gain": 0, "minimum": 0,
                     "maximum": 300, "increment": 1},
                )
            self.assertEqual(calls, [("acquire", 0, 1), ("gain", 1), ("acquire", 1, 1)])
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_remote_em_gain_failure_stops_retries_and_returns_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            attempts = []

            class Reader:
                def acquire_spectrum_with_fit(self, **kwargs):
                    attempts.append(self.accumulations)
                    return {"x": [690.0], "y_raw": [100.0], "y": [100.0],
                            "saturated": False,
                            "fit": {"success": True, "fit": {"R2": 0.94}}}

                def set_em_gain(self, gain):
                    raise RuntimeError("FRP gain write failed")

            with patch.object(runner, "_align_to_xrd_reference"):
                with self.assertRaisesRegex(RuntimeError, "FRP gain write failed"):
                    runner._acquire_spectrum_at_ruby(
                        TakeSpectrumAction(save=False), 0, runner._global_spectrum,
                        {3: 0, 4: 1, 5: 0}, 0.1, False, Reader(), (),
                        {"available": True, "gain": 1, "minimum": 1,
                         "maximum": 1000, "increment": 1},
                    )
            self.assertEqual(attempts, [1])
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_saturation_then_accumulation_retry_saves_every_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary, settings_overrides={"em_gain_max": 1})
            results = [
                SpectrumQuality(True, False),
                SpectrumQuality(False, True, r_squared=0.94),
                SpectrumQuality(False, True, r_squared=0.95),
            ]
            with patch.object(runner_module, "assess_spectrum", side_effect=results):
                runner._do_take_spectrum(TakeSpectrumAction(), 0)
            paths = sorted(Path(temporary, "spectrum").glob("*.npz"))
            self.assertEqual(len(paths), 3)
            with np.load(paths[0]) as data:
                self.assertEqual(str(data["quality_outcome"]), "saturated")
                self.assertEqual(float(data["requested_exposure_time_s"]), 0.1)
            with np.load(paths[1]) as data:
                self.assertEqual(float(data["requested_exposure_time_s"]), 0.05)
                self.assertEqual(int(data["requested_accumulations"]), 1)
            with np.load(paths[2]) as data:
                self.assertEqual(int(data["requested_accumulations"]), 2)
                self.assertEqual(str(data["quality_outcome"]), "passed")
                self.assertEqual(float(data["quality_r_squared_min"]), 0.95)
                self.assertNotIn("quality_snr", data.files)
                self.assertNotIn("quality_snr_min", data.files)
            with Path(temporary, "spectrum", "ruby_master_all_files.csv").open(
                    newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["quality_outcome"] for row in rows],
                             ["saturated", "quality_low", "passed"])
            self.assertEqual([row["attempt"] for row in rows], ["1", "2", "3"])
            self.assertEqual(len(runner._logger.science), 1)
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_sequence_master_groups_all_positions_and_uses_last_passing_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, _ = self._quality_runner(temporary, settings_overrides={
                "ruby_positions": {"Ruby B": {"ch3": 0, "ch4": 2, "ch5": 0}},
            })
            qualities = [
                SpectrumQuality(False, True, r_squared=0.94),
                SpectrumQuality(False, True, r_squared=0.99),
                SpectrumQuality(False, True, r_squared=0.98),
            ]
            with patch.object(runner_module, "assess_spectrum", side_effect=qualities):
                runner._do_take_spectrum(TakeSpectrumAction(all_positions=True), 7)
            directory = Path(temporary, "spectrum")
            with (directory / "ruby_master_all_files.csv").open(newline="", encoding="utf-8-sig") as handle:
                attempts = list(csv.DictReader(handle))
            with (directory / "ruby_master_summary.csv").open(
                    newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(attempts), 3)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["sequence_status"], "completed")
            self.assertEqual(row["step_index"], "7")
            self.assertEqual(row["ruby_count"], "2")
            self.assertEqual(row["ruby_01_name"], "Default")
            self.assertEqual(row["ruby_01_attempt"], "2")
            self.assertEqual(row["ruby_01_fit_r_squared"], "0.99")
            self.assertEqual(row["ruby_02_name"], "Ruby B")
            self.assertEqual(row["ruby_02_attempt"], "1")
            self.assertEqual(row["ruby_02_fit_r_squared"], "0.98")
            self.assertNotIn(attempts[0]["spectrum_file"], row.values())

    def test_failed_fit_creates_sequence_row_without_accepted_spectrum(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, _ = self._quality_runner(temporary)
            with patch.object(runner_module, "assess_spectrum", return_value=SpectrumQuality(
                    False, False, fit_error="FRP two-peak fit failed")):
                with self.assertRaisesRegex(RunnerError, "fit failed"):
                    runner._do_take_spectrum(TakeSpectrumAction(), 0)
            with Path(temporary, "spectrum", "ruby_master_summary.csv").open(
                    newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["sequence_status"], "failed")
            self.assertEqual(row["ruby_count"], "0")
            self.assertEqual(row["ruby_01_spectrum_file"], "")

    def test_sequence_master_keeps_accepted_ruby_when_later_position_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, _ = self._quality_runner(temporary, settings_overrides={
                "ruby_positions": {"Ruby B": {"ch3": 0, "ch4": 2, "ch5": 0}},
            })
            original = runner._acquire_spectrum_at_ruby

            def fail_second(action, *args, **kwargs):
                if action.position_name == "Ruby B":
                    raise RunnerError("runtime.spectrum_failed", "second ruby failed")
                return original(action, *args, **kwargs)

            with patch.object(runner, "_acquire_spectrum_at_ruby", side_effect=fail_second):
                with self.assertRaisesRegex(RunnerError, "second ruby failed"):
                    runner._do_take_spectrum(TakeSpectrumAction(all_positions=True), 0)
            with Path(temporary, "spectrum", "ruby_master_summary.csv").open(
                    newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["sequence_status"], "failed")
            self.assertEqual(rows[0]["ruby_count"], "1")
            self.assertEqual(rows[0]["ruby_01_quality_outcome"], "passed")
            self.assertEqual(rows[0]["ruby_02_spectrum_file"], "")

    def test_failed_fit_terminates_measurement_and_returns_to_xrd(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            with patch.object(runner_module, "assess_spectrum", return_value=SpectrumQuality(
                    False, False, fit_error="FRP two-peak fit failed")):
                with self.assertRaisesRegex(RunnerError, "fit failed"):
                    runner._do_take_spectrum(TakeSpectrumAction(), 0)
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)
            self.assertEqual(runner._logger.science, [])
            self.assertEqual(len(list(Path(temporary, "spectrum").glob("*.npz"))), 1)
            with Path(temporary, "spectrum", "ruby_master_all_files.csv").open(
                    newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["quality_outcome"], "fit_failed")
            self.assertEqual(row["fit_r_squared"], "")

    def test_incompatible_ruby_master_stops_step_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            save_dir = Path(temporary, "spectrum")
            save_dir.mkdir()
            master = save_dir / "ruby_master_all_files.csv"
            master.write_text("other,columns\nold,data\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incompatible columns"):
                runner._do_take_spectrum(TakeSpectrumAction(), 0)
            self.assertEqual(master.read_text(encoding="utf-8"), "other,columns\nold,data\n")
            self.assertFalse((save_dir / "ruby_master_summary.csv").exists())
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_unsaved_ruby_measurement_does_not_create_master(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, _ = self._quality_runner(temporary)
            runner._do_take_spectrum(TakeSpectrumAction(save=False), 0)
            self.assertFalse(Path(temporary, "spectrum", "ruby_master_all_files.csv").exists())
            self.assertFalse(Path(temporary, "spectrum", "ruby_master_summary.csv").exists())

    def test_quality_and_saturation_bounds_end_with_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(
                temporary, settings_overrides={"em_gain_max": 1, "max_accumulations": 2},
            )
            low = SpectrumQuality(False, True, r_squared=0.5)
            with patch.object(runner_module, "assess_spectrum", return_value=low):
                with self.assertRaisesRegex(RunnerError, "maximum accumulations 2"):
                    runner._do_take_spectrum(TakeSpectrumAction(save=False), 0)
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)
            with patch.object(runner_module, "assess_spectrum", return_value=SpectrumQuality(True, False)):
                with self.assertRaisesRegex(RunnerError, "still saturated"):
                    runner._do_take_spectrum(TakeSpectrumAction(save=False), 1)
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)
            self.assertEqual(runner._logger.science, [])
            self.assertEqual(len([line for line in runner._logger.ops if "outcome=saturated" in line]), 4)

    def test_nearby_grid_default_and_per_axis_step(self):
        action = TakeSpectrumAction(search_best_position_nearby=True, search_channels=(3, 4, 5))
        axes = build_search_axes(action)
        self.assertEqual([ch for ch, _ in axes], [3, 4, 5])
        self.assertEqual(axes[0][1], (-2, -1, 0, 1, 2))
        self.assertEqual(axes[1][1], (-2, -1, 0, 1, 2))
        self.assertEqual(len(search_positions({3: 0, 4: 0, 5: 0}, axes)), 175)
        customised = TakeSpectrumAction(
            search_best_position_nearby=True, search_channels=(5,),
            search_range_ch5_um=6.0, search_step_ch5_um=1.0,
        )
        self.assertEqual(len(build_search_axes(customised)[0][1]), 7)

    def test_search_action_round_trip_and_invalid_grid(self):
        action = TakeSpectrumAction(
            search_best_position_nearby=True, search_channels=(3, 5),
            search_range_ch3_um=8.0, search_step_ch5_um=1.0,
        )
        self.assertEqual(action_from_dict(action.to_dict()), action)
        compiled = DslCompiler().compile(action.to_dsl())
        self.assertTrue(compiled.ok, compiled.diagnostics)
        self.assertEqual(compiled.sequence.actions, [action])
        conditional = TakeSpectrumAction(
            search_best_position_nearby=True, search_when="on_low_quality",
            search_channels=(4,),
        )
        self.assertEqual(action_from_dict(conditional.to_dict()), conditional)
        conditional_compiled = DslCompiler().compile(conditional.to_dsl())
        self.assertTrue(conditional_compiled.ok, conditional_compiled.diagnostics)
        self.assertEqual(conditional_compiled.sequence.actions, [conditional])
        self.assertEqual(action_from_dict({"type": "take_spectrum",
                                           "search_best_position_nearby": True}).search_when,
                         "always")
        disabled = TakeSpectrumAction(
            search_best_position_nearby=False, search_channels=(3,),
            search_range_ch3_um=8.0, search_step_ch3_um=1.0,
        )
        disabled_compiled = DslCompiler().compile(disabled.to_dsl())
        self.assertTrue(disabled_compiled.ok, disabled_compiled.diagnostics)
        self.assertEqual(disabled_compiled.sequence.actions, [disabled])
        for invalid in (
            TakeSpectrumAction(search_best_position_nearby=True, search_channels=()),
            TakeSpectrumAction(search_best_position_nearby=True, search_channels=(4, 4)),
            TakeSpectrumAction(search_best_position_nearby=True,
                               search_channels=(3, 4, 5), search_range_ch4_um=100.0),
            TakeSpectrumAction(search_best_position_nearby=True, search_step_ch4_um=0.0),
            TakeSpectrumAction(search_best_position_nearby=True, search_when="invalid"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    build_search_axes(invalid)

    def test_json_and_dsl_round_trip(self):
        action = TakeSpectrumAction(
            save=False, prefix="ruby", position_name="Ruby B",
            exposure_time_s=2.5,
        )
        self.assertEqual(action_from_dict(action.to_dict()), action)
        compiled = DslCompiler().compile(action.to_dsl())
        self.assertTrue(compiled.ok, compiled.diagnostics)
        self.assertEqual(compiled.sequence.actions, [action])

        all_action = TakeSpectrumAction(all_positions=True, save=False)
        self.assertEqual(action_from_dict(all_action.to_dict()), all_action)
        all_compiled = DslCompiler().compile(all_action.to_dsl())
        self.assertTrue(all_compiled.ok, all_compiled.diagnostics)
        self.assertEqual(all_compiled.sequence.actions, [all_action])
        with self.assertRaisesRegex(ValueError, "all_positions must be a boolean"):
            action_from_dict({"type": "take_spectrum", "all_positions": "false"})

    def test_named_offsets_include_ch3_and_legacy_default(self):
        settings = GlobalSpectrumSettings(
            offset_ch4_pulse=4, offset_ch5_pulse=5,
            ruby_positions={"Ruby B": {"ch3": -2, "ch4": 3, "ch5": 7}},
        )
        self.assertEqual(settings.offset_for("Ruby B"), {3: -2, 4: 3, 5: 7})
        self.assertEqual(settings.offset_for("Default"), {3: 0, 4: 4, 5: 5})
        self.assertIsNone(settings.offset_for("missing"))
        self.assertEqual(settings.position_names(), ("Default", "Ruby B"))

    def test_all_positions_require_registration_and_check_each_excursion(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        action = TakeSpectrumAction(all_positions=True)
        missing = PreValidator().validate(
            Sequence(actions=[action]), DeviceContext(controller=ctrl),
            global_spectrum=GlobalSpectrumSettings(),
        )
        self.assertTrue(any("at least one ruby position" in message for message in missing.errors))
        runner = SequenceRunner(
            Sequence(actions=[action]), DeviceContext(controller=ctrl),
            global_spectrum=GlobalSpectrumSettings(),
        )
        with self.assertRaisesRegex(RunnerError, "No ruby positions are registered"):
            runner._do_take_spectrum(action, 0)

        settings = GlobalSpectrumSettings(ruby_positions={
            "Near": {"ch3": 0, "ch4": 0, "ch5": 0},
            "Far": {"ch3": 0, "ch4": 10_000, "ch5": 0},
        })
        checked = PreValidator().validate(
            Sequence(actions=[action]), DeviceContext(controller=ctrl),
            global_limits=GlobalLimits(
                ch3_minus_mm=10, ch3_plus_mm=10,
                ch4_minus_mm=0.001, ch4_plus_mm=0.001,
                ch5_minus_mm=10, ch5_plus_mm=10,
            ),
            global_spectrum=settings,
        )
        self.assertTrue(any("Global limit exceeded: Ch4" in message for message in checked.errors))

    def test_all_positions_execute_in_name_order_with_simulated_stage(self):
        runner, ctrl = self._quality_runner(Path("."), settings_overrides={
            "ruby_positions": {
                "Ruby B": {"ch3": 0, "ch4": 2, "ch5": 0},
                "Ruby A": {"ch3": 1, "ch4": 0, "ch5": 0},
            },
        })
        names = []
        original = runner._acquire_spectrum_at_ruby

        def record_position(action, *args):
            names.append(action.position_name)
            return original(action, *args)

        with patch.object(runner, "_acquire_spectrum_at_ruby", side_effect=record_position):
            runner._do_take_spectrum(TakeSpectrumAction(all_positions=True, save=False), 0)
        self.assertEqual(names, ["Default", "Ruby A", "Ruby B"])
        self.assertEqual(len(runner._logger.science), 3)
        self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)},
                         runner._baseline_pos)

    def test_all_positions_honours_stop_before_the_next_position(self):
        runner, _ = self._quality_runner(Path("."), settings_overrides={
            "ruby_positions": {"Ruby A": {"ch3": 0, "ch4": 2, "ch5": 0}},
        })
        measured = []

        def stop_after_first(action, *args):
            measured.append(action.position_name)
            runner._stop_event.set()

        with patch.object(runner, "_acquire_spectrum_at_ruby", side_effect=stop_after_first):
            with self.assertRaises(runner_module._StopRequested):
                runner._do_take_spectrum(TakeSpectrumAction(all_positions=True, save=False), 0)
        self.assertEqual(measured, ["Default"])

    def test_preflight_rejects_unknown_named_position(self):
        ctrl = PM16CControllerSim()
        result = PreValidator().validate(
            Sequence(actions=[TakeSpectrumAction(position_name="missing")]),
            DeviceContext(controller=ctrl),
            global_spectrum=GlobalSpectrumSettings(
                ruby_positions={"Ruby B": {"ch3": 1, "ch4": 2, "ch5": 3}},
            ),
        )
        self.assertTrue(any("missing" in message for message in result.errors))

    def test_preflight_rejects_invalid_quality_and_exposure_settings(self):
        ctrl = PM16CControllerSim()
        settings = GlobalSpectrumSettings(
            offset_ch4_pulse=0, offset_ch5_pulse=0,
            exposure_time_s=0.001, r_squared_min=1.2, em_gain_max=1001,
            max_accumulations=11, fit_function="Diamond Raman Edge",
            baseline_model="unsupported",
            filename_suffixes=("index",),
        )
        result = PreValidator().validate(
            Sequence(actions=[TakeSpectrumAction()]), DeviceContext(controller=ctrl),
            global_spectrum=settings,
        )
        codes = {diagnostic.code for diagnostic in result.diagnostics}
        self.assertIn("static.spectrum.exposure_invalid", codes)
        self.assertIn("static.spectrum.quality_invalid", codes)
        self.assertIn("static.spectrum.filename_suffix_invalid", codes)
        self.assertTrue(any("background" in message for message in result.errors))

    def test_preflight_requires_recorded_offset(self):
        ctrl = PM16CControllerSim()
        result = PreValidator().validate(
            Sequence(actions=[TakeSpectrumAction()]),
            DeviceContext(controller=ctrl),
            global_spectrum=GlobalSpectrumSettings(),
        )
        self.assertTrue(any("Record both" in message for message in result.errors))

    def test_preflight_checks_excursion_from_sequence_position(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        sequence = Sequence(actions=[TakeSpectrumAction()])
        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=ctrl),
            global_limits=GlobalLimits(
                ch3_minus_mm=10, ch3_plus_mm=10,
                ch4_minus_mm=0.001, ch4_plus_mm=0.001,
                ch5_minus_mm=10, ch5_plus_mm=10,
            ),
            global_spectrum=GlobalSpectrumSettings(
                offset_ch4_pulse=10_000, offset_ch5_pulse=0,
            ),
        )
        self.assertTrue(any(
            "Global limit exceeded: Ch4" in message for message in result.errors
        ))

    def test_preflight_rejects_bad_search_and_requires_explicit_microscope_step(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        settings = GlobalSpectrumSettings(offset_ch4_pulse=0, offset_ch5_pulse=0)
        action = TakeSpectrumAction(search_best_position_nearby=True, search_channels=())
        result = PreValidator().validate(
            Sequence(actions=[action]), DeviceContext(controller=ctrl),
            global_spectrum=settings,
        )
        codes = {diagnostic.code for diagnostic in result.diagnostics}
        self.assertIn("static.spectrum.search_invalid", codes)
        self.assertIn("preflight.stage.spectrum_microscope_required", codes)
        # Microscope mode also needs Ch6/Ch8 at the registered X-ray beam.
        positions = GlobalPositions()
        beam = {6: int(ctrl.get_ch_pos(6)), 7: 0, 8: positions.ch8_in}
        with patch("apps.exp_scheduler.validator.snapshots.load_beam_pulses",
                   return_value=beam):
            with_switch = PreValidator().validate(
                Sequence(actions=[FpdOutMicroscopeInAction(), action]),
                DeviceContext(controller=ctrl), global_spectrum=settings,
                global_positions=positions,
            )
        self.assertNotIn(
            "preflight.stage.spectrum_microscope_required",
            {diagnostic.code for diagnostic in with_switch.diagnostics},
        )

    def test_simulated_acquisition_moves_to_offset_and_returns(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        # Use positions inside the configured Ch4/Ch5 soft limits.
        with ctrl._state_lock:
            ctrl._positions[3] = ctrl._targets[3] = 40_000
            ctrl._positions[4] = ctrl._targets[4] = 20_000
            ctrl._positions[5] = ctrl._targets[5] = 20_000
        start = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=ctrl),
            global_limits=GlobalLimits(
                ch3_minus_mm=10, ch3_plus_mm=10,
                ch4_minus_mm=10, ch4_plus_mm=10,
                ch5_minus_mm=10, ch5_plus_mm=10,
            ),
            global_spectrum=GlobalSpectrumSettings(
                ruby_positions={"Ruby B": {"ch3": 10, "ch4": 20, "ch5": -30}},
                settle_ms=0,
            ),
        )

        class Logger:
            def __init__(self):
                self.ops = []
                self.science = []
            def log_ops(self, message):
                self.ops.append(message)
            def log_science(self, event, **values):
                self.science.append((event, values))

        runner._logger = Logger()
        runner._baseline_pos = start.copy()
        runner._motion_lease = ctrl.acquire_motion("test", "spectrum")
        self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
        runner._do_take_spectrum(TakeSpectrumAction(save=False, position_name="Ruby B"), 0)

        self.assertEqual(int(ctrl.get_ch_pos(3)), start[3])
        self.assertEqual(int(ctrl.get_ch_pos(4)), start[4])
        self.assertEqual(int(ctrl.get_ch_pos(5)), start[5])
        self.assertEqual(runner._logger.science[0][0], "spectrum_taken")
        self.assertTrue(any("intensity=" in message for message in runner._logger.ops))

    def test_success_returns_to_saved_xrd_position_after_alignment_changes_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            initial = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
            aligned = {3: initial[3] - 20, 4: initial[4] + 30, 5: initial[5] - 10}
            with ctrl._state_lock:
                for ch, position in aligned.items():
                    ctrl._positions[ch] = ctrl._targets[ch] = position

            runner._acquire_spectrum_at_ruby(
                TakeSpectrumAction(), 0, runner._global_spectrum,
                runner._global_spectrum.offset_for("Default"), 0.1,
                True, None, (), {"available": False},
                initial_xrd_position=initial,
            )

            self.assertEqual(
                {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, initial,
            )
            (path,) = Path(temporary, "spectrum").glob("*.npz")
            with np.load(path) as data:
                for ch in (3, 4, 5):
                    self.assertEqual(int(data[f"xrd_ch{ch}_pulse"]), initial[ch])
                    self.assertEqual(int(data[f"aligned_xrd_ch{ch}_pulse"]), aligned[ch])

    def test_xrd_return_rejects_a_move_without_position_readback(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            initial = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
            with ctrl._state_lock:
                ctrl._positions[4] = ctrl._targets[4] = initial[4] + 10
            with patch.object(runner, "_do_stage"):
                with self.assertRaisesRegex(RunnerError, "position mismatch") as caught:
                    runner._return_to_xrd_position(ctrl, initial, "H")
            self.assertEqual(caught.exception.code, "runtime.spectrum_return_position_mismatch")

    def test_successful_ruby_with_failed_xrd_return_ends_in_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            initial_ch4 = int(ctrl.get_ch_pos(4))
            move = ctrl.move_ch_absolute

            def ignore_return(ch, target, **kwargs):
                if ch == 4 and target == initial_ch4:
                    return None
                return move(ch, target, **kwargs)

            with patch.object(ctrl, "move_ch_absolute", side_effect=ignore_return):
                with self.assertRaisesRegex(RunnerError, "position mismatch") as caught:
                    runner._do_take_spectrum(TakeSpectrumAction(save=False), 0)
            self.assertEqual(caught.exception.code, "runtime.spectrum_return_failed")
            self.assertEqual(runner._logger.science, [])

    def test_spectrum_step_snapshots_xrd_position_before_image_alignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            initial = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}

            class Reader:
                def __init__(self, **kwargs):
                    self.prepare_count = 0
                    self.last_status = None

                def prepare(self):
                    self.prepare_count += 1
                    self.last_status = {
                        "instrument_state_token": "state-1",
                    }

                def get_em_gain_info(self):
                    return {"available": False}

                def acquire_spectrum_with_fit(self, **kwargs):
                    self_outer.assertEqual(self.prepare_count, 2)
                    y = [10.0, 100.0, 20.0, 80.0, 10.0]
                    return {
                        "x": [1, 2, 3, 4, 5], "y_raw": y, "y": y,
                        "saturated": False,
                        "fit": {"success": True, "y_fit": y,
                                "fit": {"R2": 0.99, "peaks": [
                                    {"amplitude": 100.0}, {"amplitude": 80.0},
                                ]}},
                    }

            self_outer = self

            def align(_settings):
                with ctrl._state_lock:
                    ctrl._positions[3] = ctrl._targets[3] = initial[3] - 20
                    ctrl._positions[4] = ctrl._targets[4] = initial[4] + 30

            with (patch("utils.stage.control_stage_sim.PM16CControllerSim", type("OtherSim", (), {})),
                  patch("utils.fluora_pressee.FluoraPresseeReader", Reader),
                  patch.object(runner, "_require_microscope_mode_for_spectrum"),
                  patch.object(runner, "_align_to_xrd_reference", side_effect=align)):
                runner._do_take_spectrum(TakeSpectrumAction(), 0)

            self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, initial)
            (path,) = Path(temporary, "spectrum").glob("*.npz")
            with np.load(path) as data:
                self.assertEqual(int(data["xrd_ch3_pulse"]), initial[3])
                self.assertEqual(int(data["aligned_xrd_ch3_pulse"]), initial[3] - 20)

    def test_state_change_during_gain_read_stops_before_ruby_motion(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            initial = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}

            class Reader:
                def __init__(self, **kwargs):
                    self.prepare_count = 0
                    self.last_status = None

                def prepare(self):
                    self.prepare_count += 1
                    self.last_status = {
                        "instrument_state_token": f"state-{self.prepare_count}",
                    }

                def get_em_gain_info(self):
                    return {"available": False}

            with (patch("utils.stage.control_stage_sim.PM16CControllerSim", type("OtherSim", (), {})),
                  patch("utils.fluora_pressee.FluoraPresseeReader", Reader),
                  patch.object(runner, "_require_microscope_mode_for_spectrum"),
                  patch.object(runner, "_align_to_xrd_reference") as align):
                with self.assertRaisesRegex(RunnerError, "state changed while reading EM Gain"):
                    runner._do_take_spectrum(TakeSpectrumAction(save=False), 0)
            align.assert_not_called()
            self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, initial)

    def test_exposure_override_and_global_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctrl = PM16CControllerSim()
            ctrl.connect()
            self.addCleanup(ctrl.disconnect)
            with ctrl._state_lock:
                for ch, value in ((3, 40000), (4, 20000), (5, 20000)):
                    ctrl._positions[ch] = ctrl._targets[ch] = value
            settings = GlobalSpectrumSettings(
                ruby_positions={"A": {"ch3": 0, "ch4": 1, "ch5": 0}},
                exposure_time_s=4.0, settle_ms=0,
            )
            stage_limits = GlobalLimits(
                ch3_minus_mm=10, ch3_plus_mm=10,
                ch4_minus_mm=10, ch4_plus_mm=10,
                ch5_minus_mm=10, ch5_plus_mm=10,
            )
            runner = SequenceRunner(
                Sequence(actions=[]), DeviceContext(controller=ctrl),
                global_limits=stage_limits, global_spectrum=settings,
            )
            class Logger:
                run_dir = root
                def log_ops(self, message):
                    pass
                def log_science(self, event, **values):
                    pass
            runner._logger = Logger()
            runner._baseline_pos = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
            runner._motion_lease = ctrl.acquire_motion("test", "spectrum")
            self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
            runner._do_take_spectrum(TakeSpectrumAction(position_name="A"), 0)
            runner._do_take_spectrum(TakeSpectrumAction(
                position_name="A", exposure_time_s=2.5,
            ), 1)
            paths = sorted((root / "spectrum").glob("*.npz"))
            self.assertEqual(len(paths), 2)
            for path, expected in zip(paths, (4.0, 2.5)):
                with np.load(path) as data:
                    self.assertEqual(float(data["requested_exposure_time_s"]), expected)
                    self.assertEqual(int(data["offset_ch4_pulse"]), 1)
                    self.assertEqual(json.loads(str(data["metadata_json"]))["exposure_time_s"], expected)

    def test_simulated_nearby_search_exposes_at_best_and_returns_to_xrd(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctrl = PM16CControllerSim()
            ctrl.connect()
            self.addCleanup(ctrl.disconnect)
            with ctrl._state_lock:
                for ch, value in ((3, 40000), (4, 20000), (5, 20000)):
                    ctrl._positions[ch] = ctrl._targets[ch] = value
            departure = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
            runner = SequenceRunner(
                Sequence(actions=[]), DeviceContext(controller=ctrl),
                global_limits=GlobalLimits(
                    ch3_minus_mm=10, ch3_plus_mm=10,
                    ch4_minus_mm=10, ch4_plus_mm=10,
                    ch5_minus_mm=10, ch5_plus_mm=10,
                ),
                global_spectrum=GlobalSpectrumSettings(
                    ruby_positions={"A": {"ch3": 0, "ch4": 20, "ch5": 0}},
                    settle_ms=0,
                ),
            )
            class Logger:
                run_dir = Path(temporary)
                def log_ops(self, message):
                    pass
                def log_science(self, event, **values):
                    pass
            runner._logger = Logger()
            runner._baseline_pos = departure.copy()
            runner._motion_lease = ctrl.acquire_motion("test", "nearby search")
            self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
            action = TakeSpectrumAction(
                position_name="A", search_best_position_nearby=True,
                search_channels=(4,),
            )
            runner._do_take_spectrum(action, 0)
            self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, departure)
            (path,) = Path(temporary, "spectrum").glob("*.npz")
            with np.load(path) as data:
                self.assertEqual(len(data["search_intensities"]), 5)
                self.assertEqual(int(data["search_nominal_position_pulse"][1]), 20020)
                self.assertEqual(int(data["search_best_position_pulse"][1]), 20021)
                self.assertEqual(int(data["spectrum_ch4_pulse"]), 20021)
                self.assertEqual(int(np.argmax(data["search_intensities"])), 3)
            with patch.object(runner_module.np, "savez_compressed",
                              side_effect=OSError("simulated disk failure")):
                with self.assertRaisesRegex(OSError, "simulated disk failure"):
                    runner._do_take_spectrum(action, 1)
            self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, departure)

    def test_spectrum_is_also_saved_as_csv_and_png(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, _ctrl = self._quality_runner(temporary)
            runner._do_take_spectrum(TakeSpectrumAction(), 0)
            (npz_path,) = Path(temporary, "spectrum").glob("*.npz")
            csv_path = npz_path.with_suffix(".csv")
            self.assertTrue(npz_path.with_suffix(".png").stat().st_size > 0)
            text = csv_path.read_text(encoding="utf-8")
            self.assertIn("# peak1_position: 694.2", text)
            self.assertIn("# peak2_position: 692.8", text)
            rows = list(csv.DictReader(
                line for line in text.splitlines() if not line.startswith("#")
            ))
            with np.load(npz_path) as archived:
                np.testing.assert_allclose([float(r["x"]) for r in rows], archived["x"])
                np.testing.assert_allclose([float(r["y"]) for r in rows], archived["y"])
                np.testing.assert_allclose([float(r["y_fit"]) for r in rows], archived["y"])

    def test_conditional_search_skips_grid_when_initial_quality_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            action = TakeSpectrumAction(
                search_best_position_nearby=True, search_when="on_low_quality",
                search_channels=(4,),
            )
            runner._do_take_spectrum(action, 0)
            (path,) = Path(temporary, "spectrum").glob("*.npz")
            with np.load(path) as data:
                self.assertFalse(bool(data["search_performed"]))
                self.assertEqual(len(data["search_intensities"]), 0)
                self.assertEqual(int(data["spectrum_ch4_pulse"]), 20001)
            self.assertEqual(len(runner._logger.science), 1)
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_conditional_search_runs_once_after_initial_low_quality(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            action = TakeSpectrumAction(
                search_best_position_nearby=True, search_when="on_low_quality",
                search_channels=(4,),
            )
            results = [
                SpectrumQuality(False, True, r_squared=0.94),
                SpectrumQuality(False, True, r_squared=0.95),
            ]
            with patch.object(runner_module, "assess_spectrum", side_effect=results) as assess:
                runner._do_take_spectrum(action, 0)
            self.assertEqual(assess.call_count, 2)
            paths = sorted(Path(temporary, "spectrum").glob("*.npz"))
            self.assertEqual(len(paths), 2)
            with np.load(paths[0]) as data:
                self.assertEqual(str(data["quality_outcome"]), "quality_low")
                self.assertFalse(bool(data["search_performed"]))
            with np.load(paths[1]) as data:
                self.assertTrue(bool(data["search_performed"]))
                self.assertEqual(len(data["search_intensities"]), 5)
                self.assertEqual(int(data["requested_accumulations"]), 1)
                self.assertEqual(int(data["spectrum_ch4_pulse"]), 20002)
            self.assertEqual(len(runner._logger.science), 1)
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_conditional_search_does_not_scan_for_saturation_or_fit_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(temporary)
            action = TakeSpectrumAction(
                save=False, search_best_position_nearby=True,
                search_when="on_low_quality", search_channels=(4,),
            )
            results = [SpectrumQuality(True, False),
                       SpectrumQuality(False, True, r_squared=0.95)]
            with patch.object(runner_module, "assess_spectrum", side_effect=results):
                runner._do_take_spectrum(action, 0)
            self.assertFalse(any("nearby search best" in line for line in runner._logger.ops))
            with patch.object(runner_module, "assess_spectrum", return_value=SpectrumQuality(
                    False, False, fit_error="FRP two-peak fit failed")):
                with self.assertRaisesRegex(RunnerError, "fit failed"):
                    runner._do_take_spectrum(action, 1)
            self.assertFalse(any("nearby search best" in line for line in runner._logger.ops))
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_conditional_search_then_low_r_squared_ends_with_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, ctrl = self._quality_runner(
                temporary, settings_overrides={"em_gain_max": 1, "max_accumulations": 1},
            )
            action = TakeSpectrumAction(
                save=False, search_best_position_nearby=True,
                search_when="on_low_quality", search_channels=(4,),
            )
            low = SpectrumQuality(False, True, r_squared=0.5)
            with patch.object(runner_module, "assess_spectrum", return_value=low) as assess:
                with self.assertRaisesRegex(RunnerError, "maximum accumulations 1"):
                    runner._do_take_spectrum(action, 0)
            self.assertEqual(assess.call_count, 2)
            self.assertEqual(sum("nearby search best" in line for line in runner._logger.ops), 1)
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)
            self.assertEqual(runner._logger.science, [])

    def test_nearby_search_soft_limit_is_checked_before_ruby_motion(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        with ctrl._state_lock:
            ctrl._positions[4] = ctrl._targets[4] = 16680
        departure = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=ctrl),
            global_spectrum=GlobalSpectrumSettings(
                offset_ch3_pulse=0, offset_ch4_pulse=0, offset_ch5_pulse=0,
                settle_ms=0,
            ),
        )
        with self.assertRaisesRegex(RunnerError, "soft limit"):
            runner._do_take_spectrum(
                TakeSpectrumAction(
                    save=False, search_best_position_nearby=True,
                    search_channels=(4,),
                ), 0,
            )
        self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, departure)

    def test_explicit_microscope_mode_is_required_without_switching_stage(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext(controller=ctrl))
        initial = {ch: int(ctrl.get_ch_pos(ch)) for ch in (8, 9)}
        with patch("apps.exp_scheduler.runner.stage_mode_from_positions", return_value="xrd"):
            with self.assertRaisesRegex(RunnerError, "fpd_out_and_microscope_in"):
                runner._require_microscope_mode_for_spectrum()
        with patch("apps.exp_scheduler.runner.stage_mode_from_positions", return_value="unknown"):
            with self.assertRaises(RunnerError):
                runner._require_microscope_mode_for_spectrum()
        with patch("apps.exp_scheduler.runner.stage_mode_from_positions", return_value="microscope"):
            runner._require_microscope_mode_for_spectrum()
        self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (8, 9)}, initial)

    def test_image_alignment_moves_ch4_from_live_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = np.full((40, 40, 3), 100, dtype=np.uint8)
            reference = root / "reference.png"
            cv2.imwrite(str(reference), frame)
            calibration = root / "calibration.json"
            calibration.write_text(json.dumps({"matrix_inv": [[1, 0], [0, 1]]}))
            ctrl = PM16CControllerSim()
            ctrl.connect()
            self.addCleanup(ctrl.disconnect)
            with ctrl._state_lock:
                for ch, value in ((3, 40000), (4, 20000), (5, 20000)):
                    ctrl._positions[ch] = ctrl._targets[ch] = value
            runner = SequenceRunner(
                Sequence(actions=[]), DeviceContext(controller=ctrl),
                global_follow=GlobalFollowSettings(reference_path=str(reference)),
                global_limits=GlobalLimits(
                    ch3_minus_mm=10, ch3_plus_mm=10,
                    ch4_minus_mm=10, ch4_plus_mm=10,
                    ch5_minus_mm=10, ch5_plus_mm=10,
                ),
            )
            class Logger:
                def log_ops(self, message):
                    pass
            runner._logger = Logger()
            runner._baseline_pos = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
            runner._motion_lease = ctrl.acquire_motion("test", "alignment")
            self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
            with (patch.object(runner_module, "_CALIBRATION_PATH", calibration),
                  patch.object(runner, "_start_camera_session_if_needed"),
                  patch.object(runner, "_require_camera_index"),
                  patch.object(runner, "_get_camera_frame", return_value=frame),
                  patch.object(runner, "_get_camera_frame_after", return_value=frame),
                  patch.object(runner_module, "measure_xy_shift_strict",
                               side_effect=[(5, 0, 0.9), (0, 0, 0.9)])):
                runner._align_to_xrd_reference(GlobalSpectrumSettings())
            self.assertEqual(int(ctrl.get_ch_pos(4)), 19995)


if __name__ == "__main__":
    unittest.main()
