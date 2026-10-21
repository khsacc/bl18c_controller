from __future__ import annotations

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
from apps.exp_scheduler.scheduler_settings import GlobalFollowSettings, GlobalLimits, GlobalSpectrumSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator
from utils.stage.control_stage_sim import PM16CControllerSim


class SpectrumActionTests(unittest.TestCase):
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
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    build_search_axes(invalid)

    def test_json_and_dsl_round_trip(self):
        action = TakeSpectrumAction(
            save=False, prefix="ruby", position_name="Ruby B",
            exposure_time_s=2.5, save_dir="C:/data/ruby",
        )
        self.assertEqual(action_from_dict(action.to_dict()), action)
        compiled = DslCompiler().compile(action.to_dsl())
        self.assertTrue(compiled.ok, compiled.diagnostics)
        self.assertEqual(compiled.sequence.actions, [action])

    def test_named_offsets_include_ch3_and_legacy_default(self):
        settings = GlobalSpectrumSettings(
            offset_ch4_pulse=4, offset_ch5_pulse=5,
            ruby_positions={"Ruby B": {"ch3": -2, "ch4": 3, "ch5": 7}},
        )
        self.assertEqual(settings.offset_for("Ruby B"), {3: -2, 4: 3, 5: 7})
        self.assertEqual(settings.offset_for("Default"), {3: 0, 4: 4, 5: 5})
        self.assertIsNone(settings.offset_for("missing"))

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
        with_switch = PreValidator().validate(
            Sequence(actions=[FpdOutMicroscopeInAction(), action]),
            DeviceContext(controller=ctrl), global_spectrum=settings,
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
        runner._run_timestamp = "test"
        runner._baseline_pos = start.copy()
        runner._motion_lease = ctrl.acquire_motion("test", "spectrum")
        self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
        runner._do_take_spectrum(TakeSpectrumAction(save=False, position_name="Ruby B"), 0)

        self.assertEqual(int(ctrl.get_ch_pos(3)), start[3])
        self.assertEqual(int(ctrl.get_ch_pos(4)), start[4])
        self.assertEqual(int(ctrl.get_ch_pos(5)), start[5])
        self.assertEqual(runner._logger.science[0][0], "spectrum_taken")
        self.assertTrue(any("intensity=" in message for message in runner._logger.ops))

    def test_exposure_and_directory_override_and_global_fallback(self):
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
                exposure_time_s=4.0, save_dir=str(root / "global"), settle_ms=0,
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
                def log_ops(self, message):
                    pass
                def log_science(self, event, **values):
                    pass
            runner._logger = Logger()
            runner._run_timestamp = "test"
            runner._baseline_pos = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
            runner._motion_lease = ctrl.acquire_motion("test", "spectrum")
            self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
            runner._do_take_spectrum(TakeSpectrumAction(position_name="A"), 0)
            runner._do_take_spectrum(TakeSpectrumAction(
                position_name="A", exposure_time_s=2.5,
                save_dir=str(root / "step"),
            ), 1)
            for directory, expected in ((root / "global", 4.0), (root / "step", 2.5)):
                (path,) = directory.glob("*.npz")
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
                    save_dir=temporary, settle_ms=0,
                ),
            )
            class Logger:
                def log_ops(self, message):
                    pass
                def log_science(self, event, **values):
                    pass
            runner._logger = Logger()
            runner._run_timestamp = "test"
            runner._baseline_pos = departure.copy()
            runner._motion_lease = ctrl.acquire_motion("test", "nearby search")
            self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
            action = TakeSpectrumAction(
                position_name="A", search_best_position_nearby=True,
                search_channels=(4,),
            )
            runner._do_take_spectrum(action, 0)
            self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, departure)
            (path,) = Path(temporary).glob("*.npz")
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
        with patch("apps.exp_scheduler.validator.snapshots._stage_mode_from_positions", return_value="xrd"):
            with self.assertRaisesRegex(RunnerError, "fpd_out_and_microscope_in"):
                runner._require_microscope_mode_for_spectrum()
        with patch("apps.exp_scheduler.validator.snapshots._stage_mode_from_positions", return_value="unknown"):
            with self.assertRaises(RunnerError):
                runner._require_microscope_mode_for_spectrum()
        with patch("apps.exp_scheduler.validator.snapshots._stage_mode_from_positions", return_value="microscope"):
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
