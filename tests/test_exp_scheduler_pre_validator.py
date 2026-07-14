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

from apps.exp_scheduler.actions import ForLoopAction, LogAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator


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


if __name__ == "__main__":
    unittest.main()
