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

from apps.exp_scheduler.dsl.validator import ASTValidator


class DslAstValidatorNumericTests(unittest.TestCase):
    """Covers the three ASTValidator additions that close gaps where a
    malformed DSL call previously built a silently-broken Action instead of
    failing at the syntax-validation stage: missing required keyword
    arguments, positional-argument calls, and non-finite numeric literals."""

    # ── missing required keyword arguments ──────────────────────────────

    def test_set_temperature_missing_ramp_rate_is_rejected(self):
        errors = ASTValidator().validate("set_temperature(value=300.0)\n")

        self.assertTrue(
            any("missing required argument" in e and "ramp_rate" in e for e in errors)
        )

    def test_set_pressure_missing_rate_is_rejected(self):
        errors = ASTValidator().validate(
            'set_pressure(pressure=1.0, unit="MPa", rate_unit="MPa/min")\n'
        )

        self.assertTrue(
            any("missing required argument" in e and "rate" in e for e in errors)
        )

    def test_fully_specified_set_temperature_is_accepted(self):
        errors = ASTValidator().validate(
            'set_temperature(value=300.0, unit="K", ramp_rate=5.0)\n'
        )

        self.assertEqual(errors, [])

    # ── positional arguments ─────────────────────────────────────────────

    def test_positional_arguments_are_rejected(self):
        errors = ASTValidator().validate("move_absolute(4, 10000)\n")

        self.assertTrue(any("positional arguments are not supported" in e for e in errors))

    def test_keyword_only_call_is_accepted(self):
        errors = ASTValidator().validate("move_absolute(ch=4, position=10000)\n")

        self.assertEqual(errors, [])

    # ── non-finite numeric literals ──────────────────────────────────────

    def test_numeric_overflow_literal_is_rejected(self):
        # 1e400 overflows to `inf` at the Python parser level — no function
        # call involved, so this cannot be caught by validating call
        # arguments against ALLOWED_FUNCTIONS/_NUMERIC_BOUNDS alone.
        errors = ASTValidator().validate('wait(duration=1e400, unit="s")\n')

        self.assertTrue(any("must be a finite number" in e for e in errors))

    def test_finite_wait_duration_is_accepted(self):
        errors = ASTValidator().validate('wait(duration=5.0, unit="min")\n')

        self.assertEqual(errors, [])

    def test_for_loop_with_infinite_literal_is_rejected(self):
        errors = ASTValidator().validate(
            "for p in [1e400, 2.0]:\n"
            "    log_message(message=\"x\")\n"
        )

        self.assertTrue(any("finite numbers" in e for e in errors))

    def test_for_loop_with_finite_literals_is_accepted(self):
        errors = ASTValidator().validate(
            "for p in [1.0, 2.0]:\n"
            '    set_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min")\n'
            '    wait_pressure(tol=0.01, unit="MPa")\n'
        )

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
