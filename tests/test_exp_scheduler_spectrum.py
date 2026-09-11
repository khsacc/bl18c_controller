from __future__ import annotations

import unittest

from apps.exp_scheduler.actions import TakeSpectrumAction, action_from_dict
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl.compiler import DslCompiler
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.scheduler_settings import GlobalLimits, GlobalSpectrumSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator
from utils.stage.control_stage_sim import PM16CControllerSim


class SpectrumActionTests(unittest.TestCase):
    def test_json_and_dsl_round_trip(self):
        action = TakeSpectrumAction(save=False, prefix="ruby")
        self.assertEqual(action_from_dict(action.to_dict()), action)
        compiled = DslCompiler().compile(action.to_dsl())
        self.assertTrue(compiled.ok, compiled.diagnostics)
        self.assertEqual(compiled.sequence.actions, [action])

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

    def test_simulated_acquisition_moves_to_offset_and_returns(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        # Use positions inside the configured Ch4/Ch5 soft limits.
        with ctrl._state_lock:
            ctrl._positions[4] = ctrl._targets[4] = 20_000
            ctrl._positions[5] = ctrl._targets[5] = 20_000
        start = {4: int(ctrl.get_ch_pos(4)), 5: int(ctrl.get_ch_pos(5))}
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=ctrl),
            global_limits=GlobalLimits(
                ch3_minus_mm=10, ch3_plus_mm=10,
                ch4_minus_mm=10, ch4_plus_mm=10,
                ch5_minus_mm=10, ch5_plus_mm=10,
            ),
            global_spectrum=GlobalSpectrumSettings(
                offset_ch4_pulse=20, offset_ch5_pulse=-30, settle_ms=0,
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
        runner._baseline_pos = {3: int(ctrl.get_ch_pos(3)), 4: start[4], 5: start[5]}
        runner._motion_lease = ctrl.acquire_motion("test", "spectrum")
        self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
        runner._do_take_spectrum(TakeSpectrumAction(save=False), 0)

        self.assertEqual(int(ctrl.get_ch_pos(4)), start[4])
        self.assertEqual(int(ctrl.get_ch_pos(5)), start[5])
        self.assertEqual(runner._logger.science[0][0], "spectrum_taken")
        self.assertTrue(any("intensity=" in message for message in runner._logger.ops))


if __name__ == "__main__":
    unittest.main()
