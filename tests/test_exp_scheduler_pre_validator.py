import sys
import types
import unittest

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )

from apps.exp_scheduler.actions import ForLoopAction, LogAction, StageAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator


class _FakeStageController:
    def __init__(self, positions: dict[int, int]):
        self.positions = {ch: 0 for ch in range(1, 12)}
        self.positions.update(positions)

    def get_ch_pos(self, ch: int) -> int:
        return self.positions[ch]

    def get_is_moving(self) -> bool:
        return False


class ExpSchedulerPreValidatorTests(unittest.TestCase):
    def test_warns_for_unused_for_loop_variable(self):
        sequence = Sequence(actions=[
            ForLoopAction(
                var="p",
                values=[1.0, 2.0],
                body=[LogAction(message="constant step")],
            )
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("for ループ変数 'p'" in w for w in result.warnings))

    def test_accepts_referenced_for_loop_variable(self):
        sequence = Sequence(actions=[
            ForLoopAction(
                var="p",
                values=[1.0, 2.0],
                body=[LogAction(message="pressure={p}")],
            )
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertFalse(any("for ループ変数 'p'" in w for w in result.warnings))

    def test_detects_move_constraint_violation_inside_for_loop(self):
        sequence = Sequence(actions=[
            ForLoopAction(
                var="det_pos",
                values=[-40000, 1000],
                body=[
                    StageAction(
                        operation="move_absolute",
                        ch=9,
                        value="det_pos",
                    )
                ],
            )
        ])
        controller = _FakeStageController({8: 100, 9: -40000})

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=controller),
        )

        self.assertTrue(any("Move blocked: Ch9" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()
