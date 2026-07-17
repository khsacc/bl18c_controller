"""
Tests for apps/exp_scheduler/dsl/compiler.py::DslCompiler — introduced in
REORGANISATION_PLAN.md Phase 1 as the single DSL text -> Sequence entry
point (normalize -> AST safety validation -> SequenceBuilder.build()).

`DslCompilerPhase2FailClosedTests` covers Phase 2's addition: SequenceBuilder
can now itself fail (unknown keyword argument, unbound bare name, unsupported
statement) and raises `SequenceBuildError`, which `compile()` must unpack
into the same `CompileResult.diagnostics` shape as every other failure mode
— these tests exercise that end-to-end through DslCompiler, complementing
the SequenceBuilder-level tests in test_exp_scheduler_dsl_contract.py and
test_exp_scheduler_dsl_roundtrip.py.
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

from apps.exp_scheduler.actions import LogAction, StageAction, WaitAction
from apps.exp_scheduler.dsl.compiler import DslCompiler
from apps.exp_scheduler.validator.models import Severity, ValidationPhase


class DslCompilerSuccessTests(unittest.TestCase):
    def test_valid_source_compiles_to_sequence_with_no_diagnostics(self):
        result = DslCompiler().compile('wait(duration=5.0, unit="min")\n')

        self.assertTrue(result.ok)
        self.assertEqual(result.diagnostics, [])
        self.assertIsNotNone(result.sequence)
        self.assertEqual(len(result.sequence.actions), 1)
        self.assertIsInstance(result.sequence.actions[0], WaitAction)

    def test_empty_source_compiles_to_empty_sequence(self):
        result = DslCompiler().compile("")

        self.assertTrue(result.ok)
        self.assertEqual(result.sequence.actions, [])

    def test_normalizer_runs_unlike_a_raw_ast_parse(self):
        """Phase 1's one intentional behaviour change: DslCompiler always
        normalizes first (int -> float constants, range() expansion),
        whereas ui/dsl_editor.py's pre-Phase-1 direct ast.parse() path did
        not. Demonstrated here via range() expansion, which only normalize()
        knows how to do — a raw ast.parse() would leave `range(...)` as an
        unresolvable Call and ASTValidator/SequenceBuilder would treat it as
        an invalid (non-literal-list) for-loop iterable."""
        source = "for p in range(3):\n    log_message(message='x')\n"
        result = DslCompiler().compile(source)

        self.assertTrue(result.ok, result.diagnostics)
        self.assertIn("[0.0, 1.0, 2.0]", result.normalised_source)

    def test_source_map_records_top_level_statement_lines(self):
        source = 'log_message(message="a")\nwait(duration=1.0, unit="s")\n'
        result = DslCompiler().compile(source)

        self.assertEqual(result.source_map.statement_lines, (1, 2))


class DslCompilerDiagnosticTests(unittest.TestCase):
    def test_syntax_error_becomes_a_diagnostic(self):
        result = DslCompiler().compile("wait(duration=\n")

        self.assertFalse(result.ok)
        self.assertIsNone(result.sequence)
        self.assertEqual(len(result.diagnostics), 1)
        d = result.diagnostics[0]
        self.assertEqual(d.severity, Severity.ERROR)
        self.assertEqual(d.code, "dsl.syntax_error")
        self.assertEqual(d.phase, ValidationPhase.COMPILE)

    def test_normalization_error_becomes_a_diagnostic(self):
        # _MAX_RANGE_ELEMENTS is 200 in dsl/normalizer.py
        source = "for p in range(10000):\n    log_message(message='x')\n"
        result = DslCompiler().compile(source)

        self.assertFalse(result.ok)
        self.assertIsNone(result.sequence)
        self.assertEqual(result.diagnostics[0].code, "dsl.normalization_error")

    def test_missing_required_argument_gets_a_stable_code_and_line(self):
        result = DslCompiler().compile('set_temperature(value=300.0)\n')

        self.assertFalse(result.ok)
        d = result.diagnostics[0]
        self.assertEqual(d.code, "dsl.required_argument_missing")
        self.assertEqual(d.source_line, 1)
        self.assertIn("ramp_rate", d.message)

    def test_positional_argument_gets_a_stable_code(self):
        result = DslCompiler().compile("move_absolute(4, 10000)\n")

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.positional_argument_not_supported")

    def test_non_finite_literal_gets_a_stable_code(self):
        result = DslCompiler().compile('wait(duration=1e400, unit="s")\n')

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.non_finite_literal")

    def test_unknown_function_gets_a_stable_code(self):
        # normal_stop() was this test's original fixture pre-Phase-2, back
        # when it was implemented everywhere except ALLOWED_FUNCTIONS (see
        # DslCompilerPhase2FailClosedTests.test_normal_stop_compiles_successfully
        # for that fix) — use a name that was never a real DSL command.
        result = DslCompiler().compile("totally_unknown_function()\n")

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.unknown_function")

    def test_invalid_unit_gets_a_stable_code(self):
        result = DslCompiler().compile(
            'set_pressure(pressure=1.0, unit="GPa", rate=0.1, rate_unit="MPa/min")\n'
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.invalid_unit_value")

    def test_all_diagnostics_from_one_call_are_reported_not_just_the_first(self):
        # Two independent errors on the same line: missing rate_unit AND an
        # out-of-bounds rate literal.
        result = DslCompiler().compile(
            'set_pressure(pressure=1.0, unit="MPa", rate=-1.0)\n'
        )

        self.assertFalse(result.ok)
        codes = {d.code for d in result.diagnostics}
        self.assertIn("dsl.required_argument_missing", codes)
        self.assertIn("dsl.numeric_bound_violation", codes)


class DslCompilerPhase2FailClosedTests(unittest.TestCase):
    def test_unknown_keyword_argument_becomes_a_diagnostic(self):
        result = DslCompiler().compile('wait(duration=1.0, unit="s", foo=123)\n')

        self.assertFalse(result.ok)
        self.assertIsNone(result.sequence)
        self.assertTrue(any(d.code == "dsl.unknown_argument" for d in result.diagnostics))

    def test_unbound_bare_name_becomes_a_diagnostic_with_a_line_number(self):
        result = DslCompiler().compile(
            'log_message(message="x")\n'
            'set_pressure(pressure=pressure_typo, unit="MPa", '
            'rate=0.2, rate_unit="MPa/min")\n'
        )

        self.assertFalse(result.ok)
        d = next(d for d in result.diagnostics if d.code == "dsl.unbound_name")
        self.assertEqual(d.source_line, 2)

    def test_assign_statement_becomes_a_diagnostic(self):
        result = DslCompiler().compile("x = 1.0\nlog_message(message='after')\n")

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.construct_not_allowed")

    def test_if_statement_becomes_a_diagnostic(self):
        result = DslCompiler().compile("if True:\n    log_message(message='x')\n")

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.construct_not_allowed")

    def test_normal_stop_compiles_successfully(self):
        result = DslCompiler().compile("normal_stop()\n")

        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual(len(result.sequence.actions), 1)
        self.assertEqual(result.sequence.actions[0].operation, "normal_stop")

    def test_wait_duration_zero_becomes_a_diagnostic(self):
        result = DslCompiler().compile('wait(duration=0.0, unit="s")\n')

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.numeric_bound_violation")

    def test_take_xrd_all_13_override_fields_compile_and_survive(self):
        result = DslCompiler().compile(
            "take_xrd(\n"
            "    exposure_ms=1000, save=True, prefix=\"scan\",\n"
            "    save_dir=\"D:/data\", dark_file=\"dark.tif\", dark_enabled=True,\n"
            "    defect_file=\"defect.tif\", defect_enabled=True, defect_kernel=3,\n"
            "    flip_v=True, flip_h=True,\n"
            "    oscillate=True, osc_pos_a_deg=-3.0, osc_pos_b_deg=15.0,\n"
            "    osc_dwell_ms=200, osc_speed=\"L\",\n"
            ")\n"
        )

        self.assertTrue(result.ok, result.diagnostics)
        action = result.sequence.actions[0]
        self.assertEqual(action.save_dir, "D:/data")
        self.assertEqual(action.dark_file, "dark.tif")
        self.assertEqual(action.defect_kernel, 3)
        self.assertEqual(action.osc_pos_a_deg, -3.0)
        self.assertEqual(action.osc_speed, "L")

    def test_wait_without_unit_now_defaults_to_minutes_not_seconds(self):
        """Phase 2's signature-bound defaulting fixed a real drift between
        dsl/api.py's documented default (unit="min", what the LLM prompt
        shows) and the old dict.get("unit", "s") fallback SequenceBuilder
        actually used when a call omitted unit — see dsl/parser.py's
        _build_wait()."""
        result = DslCompiler().compile("wait(duration=5.0)\n")

        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual(result.sequence.actions[0].duration_s, 300.0)


if __name__ == "__main__":
    unittest.main()
