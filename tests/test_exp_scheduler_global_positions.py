"""Scheduler positions use one value set in editing, preflight and execution."""
import contextlib
import io
import sys
import types
import unittest
from unittest.mock import patch

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1,
    )

from apps.exp_scheduler.actions import FpdOutMicroscopeInAction, MicroscopeOutFpdInAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.scheduler_settings import GlobalPositions
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator
from tests.exp_scheduler_fakes import FakeStageController


class GlobalPositionsTests(unittest.TestCase):
    def test_initial_values_come_from_saved_stage_positions(self):
        saved = {"ch8_in": "250000", "ch8_out": "-10",
                 "det_out": "-45000", "det_in": "1900"}
        with patch("apps.stage_fpd_scope.stage_settings.load_stage_settings", return_value=saved):
            positions = GlobalPositions.from_stage_settings()
        self.assertEqual(positions.to_stage_settings(), {
            "ch8_in": 250000, "ch8_out": -10,
            "det_out": -45000, "det_in": 1900,
        })

    def test_both_mode_switches_inherit_global_values_and_keep_overrides(self):
        positions = GlobalPositions(ch8_in=250000, ch8_out=-10,
                                    ch9_out=-45000, ch9_in=1900)
        defaults = positions.to_stage_settings()
        self.assertEqual(
            [(s.ch, s.value) for s in MicroscopeOutFpdInAction().to_steps(defaults)],
            [(8, -10), (9, 1900)],
        )
        self.assertEqual(
            [(s.ch, s.value) for s in FpdOutMicroscopeInAction().to_steps(defaults)],
            [(9, -45000), (8, 250000)],
        )
        self.assertEqual(
            [(s.ch, s.value) for s in MicroscopeOutFpdInAction(
                fpd_in_pos=1950).to_steps(defaults)],
            [(8, -10), (9, 1950)],
        )

    def test_preflight_uses_global_positions_without_stage_settings_file(self):
        positions = GlobalPositions(ch8_in=250000, ch8_out=0,
                                    ch9_out=-45000, ch9_in=1900)
        sequence = Sequence(actions=[FpdOutMicroscopeInAction()])
        controller = FakeStageController(positions={8: 0, 9: 1900})
        with patch("apps.exp_scheduler.validator.snapshots.load_stage_settings_dict",
                   return_value=None), contextlib.redirect_stdout(io.StringIO()):
            result = PreValidator().validate(
                sequence, DeviceContext(controller=controller),
                global_positions=positions,
            )
        self.assertEqual(result.snapshot.stage.stage_mode, "xrd")
        self.assertFalse(any(d.code.startswith("preflight.stage.settings_")
                             for d in result.diagnostics))
        self.assertFalse(any(d.code == "preflight.stage.move_blocked"
                             for d in result.diagnostics))

    def test_positions_survive_sequence_json_roundtrip(self):
        sequence = Sequence(actions=[MicroscopeOutFpdInAction()],
                            global_positions={"ch8_in": 250000, "ch8_out": 0,
                                              "ch9_out": -45000, "ch9_in": 1900})
        self.assertEqual(Sequence.from_dict(sequence.to_dict()).global_positions,
                         sequence.global_positions)


if __name__ == "__main__":
    unittest.main()
