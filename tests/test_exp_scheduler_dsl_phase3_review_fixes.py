"""
Regression tests for issues found in an external review of
REORGANISATION_PLAN.md Phase 3 (CommandSpec and Action factory
unification).

Each test below reproduces a bug the review demonstrated against the
Phase 3 implementation and pins the fix. See REORGANISATION_PLAN.md §17
for the review's findings and the fix record.
"""
from __future__ import annotations

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


class TakeXrdOscillationSubfieldGroupTests(unittest.TestCase):
    """Finding #1 (High): osc_pos_a_deg/osc_pos_b_deg/osc_dwell_ms/osc_speed
    used to compile successfully without oscillate=True in the same call,
    then silently vanish — dsl/_factories.py::take_xrd() only carries them
    into the Action when the call's own `oscillate` is truthy, matching
    ui/step_editor.py (Visual editor) treating oscillate + these 4 fields as
    one atomic group, and TakeXrdAction.to_dsl() only ever emitting them
    when self.oscillate is truthy."""

    def test_osc_pos_a_deg_without_oscillate_is_a_compile_error(self):
        result = DslCompiler().compile(
            "take_xrd(osc_pos_a_deg=1.0, osc_pos_b_deg=2.0, osc_speed=\"L\")\n"
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                d.code == "dsl.oscillation_subfield_without_oscillate"
                for d in result.diagnostics
            ),
            result.diagnostics,
        )

    def test_osc_dwell_ms_alone_without_oscillate_is_a_compile_error(self):
        result = DslCompiler().compile("take_xrd(osc_dwell_ms=200)\n")
        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                d.code == "dsl.oscillation_subfield_without_oscillate"
                for d in result.diagnostics
            ),
            result.diagnostics,
        )

    def test_osc_subfield_with_oscillate_false_is_also_a_compile_error(self):
        # oscillate=False resolves the effective oscillate flag to False, so
        # osc_pos_a_deg would be a dead value if silently accepted — reject
        # rather than accept-and-ignore. TakeXrdAction.to_dsl() never emits
        # this combination either (only "oscillate=False" alone), so this
        # never occurs on a legitimate Visual -> Script round trip.
        result = DslCompiler().compile(
            "take_xrd(oscillate=False, osc_pos_a_deg=1.0)\n"
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                d.code == "dsl.oscillation_subfield_without_oscillate"
                for d in result.diagnostics
            ),
            result.diagnostics,
        )

    def test_osc_subfields_with_oscillate_true_still_compile_and_are_kept(self):
        result = DslCompiler().compile(
            'take_xrd(oscillate=True, osc_pos_a_deg=1.0, osc_pos_b_deg=2.0, '
            'osc_dwell_ms=200, osc_speed="L")\n'
        )
        self.assertTrue(result.ok, result.diagnostics)
        action = result.sequence.actions[0]
        self.assertIs(action.oscillate, True)
        self.assertEqual(action.osc_pos_a_deg, 1.0)
        self.assertEqual(action.osc_pos_b_deg, 2.0)
        self.assertEqual(action.osc_dwell_ms, 200)
        self.assertEqual(action.osc_speed, "L")

    def test_take_xrd_without_any_oscillation_kwargs_still_compiles(self):
        result = DslCompiler().compile(
            'take_xrd(exposure_ms=1000, save=True, prefix="scan")\n'
        )
        self.assertTrue(result.ok, result.diagnostics)
        action = result.sequence.actions[0]
        self.assertIsNone(action.oscillate)
        self.assertIsNone(action.osc_pos_a_deg)


class TakeXrdOscSpeedEnumTests(unittest.TestCase):
    """Finding #2 (Medium): osc_speed had no ArgumentRule(valid_values=...)
    despite the docstring documenting "H"/"M"/"L" as the only valid values —
    every other H/M/L speed argument in the DSL (set_speed, ...) enforces
    this at compile time; osc_speed didn't."""

    def test_invalid_osc_speed_is_a_compile_error(self):
        result = DslCompiler().compile('take_xrd(oscillate=True, osc_speed="X")\n')
        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.invalid_unit_value" for d in result.diagnostics),
            result.diagnostics,
        )

    def test_valid_osc_speed_values_are_accepted(self):
        for speed in ("H", "M", "L"):
            with self.subTest(speed=speed):
                result = DslCompiler().compile(
                    f'take_xrd(oscillate=True, osc_speed="{speed}")\n'
                )
                self.assertTrue(result.ok, result.diagnostics)
                self.assertEqual(result.sequence.actions[0].osc_speed, speed)


if __name__ == "__main__":
    unittest.main()
