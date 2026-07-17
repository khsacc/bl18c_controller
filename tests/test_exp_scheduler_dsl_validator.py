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

from apps.exp_scheduler.dsl.compiler import DslCompiler


class DslAstValidatorNumericTests(unittest.TestCase):
    """Covers the three ASTValidator additions that close gaps where a
    malformed DSL call previously built a silently-broken Action instead of
    failing at the syntax-validation stage: missing required keyword
    arguments, positional-argument calls, and non-finite numeric literals.

    REORGANISATION_PLAN.md §7 Phase 1 item 8: these used to assert on
    ASTValidator's free-text message substrings directly; they now compile
    through DslCompiler (the same entry point ui/dsl_editor.py and
    llm/session.py use) and assert on the stable Diagnostic.code instead, so
    the test stops being coupled to exact wording. ASTValidator itself is
    unchanged in this Phase and is still exercised directly by
    test_exp_scheduler_dsl_contract.py / test_exp_scheduler_dsl_roundtrip.py
    / test_exp_scheduler_keithley_removed.py — this file's job is now to
    protect the *compiled* contract, not the validator implementation."""

    # ── missing required keyword arguments ──────────────────────────────

    def test_set_temperature_missing_ramp_rate_is_rejected(self):
        result = DslCompiler().compile("set_temperature(value=300.0)\n")

        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                d.code == "dsl.required_argument_missing" and "ramp_rate" in d.message
                for d in result.diagnostics
            )
        )

    def test_set_pressure_missing_rate_is_rejected(self):
        result = DslCompiler().compile(
            'set_pressure(pressure=1.0, unit="MPa", rate_unit="MPa/min")\n'
        )

        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                d.code == "dsl.required_argument_missing" and "rate" in d.message
                for d in result.diagnostics
            )
        )

    def test_fully_specified_set_temperature_is_accepted(self):
        result = DslCompiler().compile(
            'set_temperature(value=300.0, unit="K", ramp_rate=5.0)\n'
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.diagnostics, [])

    # ── positional arguments ─────────────────────────────────────────────

    def test_positional_arguments_are_rejected(self):
        result = DslCompiler().compile("move_absolute(4, 10000)\n")

        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.positional_argument_not_supported" for d in result.diagnostics)
        )

    def test_keyword_only_call_is_accepted(self):
        result = DslCompiler().compile("move_absolute(ch=4, position=10000)\n")

        self.assertTrue(result.ok)
        self.assertEqual(result.diagnostics, [])

    # ── non-finite numeric literals ──────────────────────────────────────

    def test_numeric_overflow_literal_is_rejected(self):
        # 1e400 overflows to `inf` at the Python parser level — no function
        # call involved, so this cannot be caught by validating call
        # arguments against ALLOWED_FUNCTIONS/_NUMERIC_BOUNDS alone.
        result = DslCompiler().compile('wait(duration=1e400, unit="s")\n')

        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.non_finite_literal" for d in result.diagnostics)
        )

    def test_finite_wait_duration_is_accepted(self):
        result = DslCompiler().compile('wait(duration=5.0, unit="min")\n')

        self.assertTrue(result.ok)
        self.assertEqual(result.diagnostics, [])

    def test_for_loop_with_infinite_literal_is_rejected(self):
        result = DslCompiler().compile(
            "for p in [1e400, 2.0]:\n"
            "    log_message(message=\"x\")\n"
        )

        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.non_finite_literal" for d in result.diagnostics)
        )

    def test_for_loop_with_finite_literals_is_accepted(self):
        result = DslCompiler().compile(
            "for p in [1.0, 2.0]:\n"
            '    set_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min")\n'
            '    wait_pressure(tol=0.01, unit="MPa")\n'
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.diagnostics, [])


if __name__ == "__main__":
    unittest.main()
