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

from apps.exp_scheduler.actions import ForLoopAction, LogAction, StageAction, TakeXrdAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import GlobalXrdSettings
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


class _FakeRadicon:
    pass


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

    def test_rejects_oscillation_endpoints_that_round_to_same_pulse(self):
        sequence = Sequence(actions=[TakeXrdAction(
            oscillate=True,
            osc_pos_a_deg=0.0,
            osc_pos_b_deg=0.001,
        )])

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=_FakeStageController({}), radicon=_FakeRadicon()),
        )

        self.assertTrue(any("different pulse positions" in e for e in result.errors))

    @unittest.skip(
        "Ch8/Ch11 collision rule is currently commented out in "
        "MOVE_CONSTRAINTS (utils/stage/control_stage.py) — re-enable this "
        "test once that constraint is restored."
    )
    def test_rejects_oscillation_when_ch8_is_extended(self):
        sequence = Sequence(actions=[TakeXrdAction(oscillate=True)])
        controller = _FakeStageController({8: 1})

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=controller, radicon=_FakeRadicon()),
        )

        self.assertTrue(any("Move blocked: Ch11" in e for e in result.errors))

    def test_oscillation_requires_stage_controller(self):
        sequence = Sequence(actions=[TakeXrdAction(oscillate=True)])

        result = PreValidator().validate(
            sequence,
            DeviceContext(radicon=_FakeRadicon()),
        )

        self.assertTrue(any("required for Ch11 oscillation" in e for e in result.errors))

    def test_validates_global_oscillation_settings_used_by_step(self):
        sequence = Sequence(actions=[TakeXrdAction(oscillate=None)])

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=_FakeStageController({}), radicon=_FakeRadicon()),
            global_xrd=GlobalXrdSettings(
                oscillate=True,
                osc_speed="invalid",
            ),
        )

        self.assertTrue(any("speed must be one of L, M, or H" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()
