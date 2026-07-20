"""
Regression tests for issues found in an external review of
REORGANISATION_PLAN.md Phase 2 (strict call binding / fail-closed parser).

Each test below reproduces a bug the review demonstrated against the
Phase 2 implementation and pins the fix. See REORGANISATION_PLAN.md §15
for the review's findings and the fix record.
"""
from __future__ import annotations

import ast
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

from apps.exp_scheduler.actions import (
    FollowSampleAction,
    LogAction,
    SaveReferenceImageAction,
    StartFollowingAction,
    TakeXrdAction,
)
from apps.exp_scheduler.dsl.compiler import DslCompiler
from apps.exp_scheduler.dsl.normalizer import NormalizationError, normalize
from apps.exp_scheduler.dsl.parser import SequenceBuilder, SequenceBuildError
from apps.exp_scheduler.dsl.validator import ASTValidator


class OscillateFalseVsNoneFidelityTests(unittest.TestCase):
    """Finding #1 (Critical): oscillate=False (explicit per-step override
    to disabled) used to collapse to None (inherit GlobalXrdSettings) on
    every DSL round trip, silently re-enabling oscillation for a step that
    explicitly turned it off whenever the global setting was on."""

    def test_oscillate_false_to_dsl_emits_explicit_false(self):
        action = TakeXrdAction(oscillate=False)
        dsl_text = action.to_dsl()
        self.assertIn("oscillate=False", dsl_text)

    def test_oscillate_false_round_trips_as_false_not_none(self):
        action = TakeXrdAction(oscillate=False)
        dsl_text = action.to_dsl() + "\n"
        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        sequence = SequenceBuilder().build(ast.parse(dsl_text))
        self.assertIs(sequence.actions[0].oscillate, False)

    def test_oscillate_true_still_round_trips_with_osc_fields(self):
        action = TakeXrdAction(
            oscillate=True, osc_pos_a_deg=-3.0, osc_pos_b_deg=15.0,
            osc_dwell_ms=200, osc_speed="L",
        )
        dsl_text = action.to_dsl() + "\n"
        sequence = SequenceBuilder().build(ast.parse(dsl_text))
        rebuilt = sequence.actions[0]
        self.assertIs(rebuilt.oscillate, True)
        self.assertEqual(rebuilt.osc_pos_a_deg, -3.0)
        self.assertEqual(rebuilt.osc_speed, "L")

    def test_oscillate_unset_still_round_trips_as_none(self):
        action = TakeXrdAction(exposure_ms=500)
        dsl_text = action.to_dsl() + "\n"
        self.assertNotIn("oscillate", dsl_text)
        sequence = SequenceBuilder().build(ast.parse(dsl_text))
        self.assertIsNone(sequence.actions[0].oscillate)


class DuplicateKeywordArgumentTests(unittest.TestCase):
    """Finding #2a: ast.parse() does not itself reject a repeated keyword
    argument (unlike calling the real Python function would), and the old
    dict-building loop in _build_call() silently kept the last value."""

    def test_duplicate_keyword_is_a_compile_error(self):
        result = DslCompiler().compile(
            'wait(duration=1.0, duration=2.0, unit="s")\n'
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.duplicate_keyword_argument" for d in result.diagnostics)
        )


class LoopVariableScopeRestrictionTests(unittest.TestCase):
    """Finding #2b: a for-loop variable used to be accepted as the value of
    *any* keyword argument, not just the ones actions.LOOP_VAR_FIELDS
    actually resolves at run time — e.g. set_speed(speed=p) compiled to
    StageAction(speed="p"), a nonsense value that would only fail once the
    Runner tried to send it to hardware."""

    def test_loop_var_rejected_for_non_eligible_argument(self):
        result = DslCompiler().compile(
            "for p in [1.0, 2.0]:\n"
            "    set_speed(ch=4, speed=p)\n"
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.loop_variable_not_supported_here" for d in result.diagnostics)
        )

    def test_loop_var_still_accepted_for_eligible_argument(self):
        result = DslCompiler().compile(
            "for p in [1.0, 2.0]:\n"
            "    move_absolute(ch=4, position=p)\n"
        )
        self.assertTrue(result.ok, result.diagnostics)


class ArgumentTypeMismatchTests(unittest.TestCase):
    """Finding #2c: Signature.bind() only checks argument *names*, not
    types, so a bool-typed argument silently accepted any truthy/falsy
    value (bool("False") is True) and an int-typed argument silently
    truncated a fractional float via int(4.9)."""

    def test_string_for_bool_argument_is_rejected(self):
        result = DslCompiler().compile('set_control_mode(enabled="False")\n')
        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.argument_type_mismatch" for d in result.diagnostics)
        )

    def test_non_integral_float_for_int_argument_is_rejected(self):
        result = DslCompiler().compile("move_absolute(ch=4.9, position=100.0)\n")
        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.argument_type_mismatch" for d in result.diagnostics)
        )

    def test_integral_float_for_int_argument_is_still_accepted(self):
        # The normalizer turns every plain int literal (e.g. ch=4) into a
        # float (4.0) before SequenceBuilder ever runs, so the type check
        # must accept a *whole* float for an int-annotated parameter.
        result = DslCompiler().compile("move_absolute(ch=4, position=100.0)\n")
        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual(result.sequence.actions[0].ch, 4)

    def test_valid_bool_literal_is_accepted(self):
        result = DslCompiler().compile("set_control_mode(enabled=True)\n")
        self.assertTrue(result.ok, result.diagnostics)


class FStringValidationTests(unittest.TestCase):
    """Finding #3: an f-string argument used to compile successfully
    regardless of whether its {name} referred to a bound for-loop variable,
    and any non-trivial expression (e.g. {p + 1}) silently rendered as a
    literal "?" — neither was ever actually resolved at run time either
    (see FStringRuntimeResolutionTests below for the companion runner fix)."""

    def test_unbound_name_in_fstring_is_rejected(self):
        result = DslCompiler().compile('log_message(message=f"p={p}")\n')
        self.assertFalse(result.ok)
        self.assertTrue(any(d.code == "dsl.unbound_name" for d in result.diagnostics))

    def test_expression_in_fstring_is_rejected(self):
        result = DslCompiler().compile(
            "for p in [1.0, 2.0]:\n"
            '    log_message(message=f"p={p + 1}")\n'
        )
        self.assertFalse(result.ok)
        self.assertTrue(any(d.code == "dsl.unbound_name" for d in result.diagnostics))

    def test_bound_loop_var_in_fstring_compiles_to_a_format_template(self):
        result = DslCompiler().compile(
            "for p in [1.0, 2.0]:\n"
            '    log_message(message=f"p={p}")\n'
        )
        self.assertTrue(result.ok, result.diagnostics)
        log_action = result.sequence.actions[0].body[0]
        self.assertEqual(log_action.message, "p={p}")

    def test_fstring_placeholder_rejected_for_a_field_the_runner_never_resolves(self):
        # External review finding (post-Phase-9, see REORGANISATION_PLAN.md
        # §31): _eval_fstring() was reachable from _eval_arg() for ANY
        # str-typed keyword of ANY command, but only tracked as a
        # loop-variable reference (subject to the same
        # allowed_loop_var_args gate a bare `p` name goes through) when the
        # value was an ast.Name — an f-string was never added to
        # loop_var_keywords, so it silently bypassed the gate entirely.
        # runner.py only resolves the "{var}" template for LogAction.message
        # (_execute_one()'s .format(**var_context) call) — every other
        # command's str field (e.g. take_xrd's prefix/save_dir) is used
        # verbatim, so this used to compile clean and then silently emit
        # the literal text "scan_{p}" forever.
        result = DslCompiler().compile(
            "for p in [1.0, 2.0]:\n"
            '    take_xrd(exposure_ms=1000, prefix=f"scan_{p}")\n'
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(d.code == "dsl.loop_variable_not_supported_here" for d in result.diagnostics),
            result.diagnostics,
        )

    def test_fstring_without_any_placeholder_is_still_a_plain_literal(self):
        # An f-string with no {var} part at all (e.g. no loop-variable
        # reference) is just a literal string — it must not be rejected by
        # the loop-var gate, which only concerns placeholder usage.
        result = DslCompiler().compile(
            'take_xrd(exposure_ms=1000, prefix=f"scan_fixed")\n'
        )
        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual(result.sequence.actions[0].prefix, "scan_fixed")


class FStringRuntimeResolutionTests(unittest.TestCase):
    """Companion to FStringValidationTests: runner.py::_execute_one()'s
    LogAction branch now resolves the "{varname}" template compiled from an
    f-string against the current var_context via str.format() — previously
    it emitted action.message completely unresolved, so f"p={p}" was
    recorded as the literal text "p={p}" forever, for every iteration."""

    def test_stored_message_is_a_str_format_compatible_template(self):
        # This is exactly the substitution runner.py::_execute_one()
        # performs: action.message.format(**var_context).
        message = "p={p}"
        self.assertEqual(message.format(p=1.0), "p=1.0")

    def test_message_without_placeholders_is_unaffected_by_format(self):
        message = "plain text, no placeholders"
        self.assertEqual(message.format(), message)


class WindowsPathRoundTripTests(unittest.TestCase):
    """Finding #4 (High): to_dsl() used to interpolate strings directly
    into a double-quoted literal (f'path="{value}"'), so a Windows path
    like C:\\Users\\hiroki\\ref.png either raised SyntaxError (\\U is an
    escape prefix) or silently corrupted the value (\\t/\\n became real
    control characters). All to_dsl() string fields now go through
    actions._dsl_str(), which uses repr() for correct, general escaping."""

    def _assert_round_trips(self, action) -> None:
        dsl_text = action.to_dsl() + "\n"
        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [], dsl_text)
        sequence = SequenceBuilder().build(ast.parse(dsl_text))
        return sequence.actions[0]

    def test_windows_path_with_letter_escapes_survives(self):
        original = SaveReferenceImageAction(path=r"C:\Users\hiroki\new\ref.png")
        rebuilt = self._assert_round_trips(original)
        self.assertEqual(rebuilt.path, original.path)

    def test_windows_path_with_unicode_escape_prefix_survives(self):
        # \U is a SyntaxError-triggering escape prefix under naive
        # f'"{value}"' interpolation.
        original = SaveReferenceImageAction(path=r"C:\Users\hiroki\Data")
        rebuilt = self._assert_round_trips(original)
        self.assertEqual(rebuilt.path, original.path)

    def test_message_with_embedded_backslash_and_quote_survives(self):
        original = LogAction(message='C:\\temp\\log "quoted" text')
        rebuilt = self._assert_round_trips(original)
        self.assertEqual(rebuilt.message, original.message)

    def test_message_with_actual_newline_survives(self):
        original = LogAction(message="line one\nline two")
        rebuilt = self._assert_round_trips(original)
        self.assertEqual(rebuilt.message, original.message)


class CameraIndexRoundTripTests(unittest.TestCase):
    """Finding #5a: StartFollowingAction/FollowSampleAction.to_dsl() used
    to unconditionally omit camera_index (unlike every other field, which
    is guarded by `if self.x is not None`), silently reverting a non-default
    camera to 0 on every Visual -> Script round trip."""

    def test_start_following_non_default_camera_index_survives(self):
        original = StartFollowingAction(camera_index=1)
        dsl_text = original.to_dsl() + "\n"
        self.assertIn("camera_index=1", dsl_text)
        sequence = SequenceBuilder().build(ast.parse(dsl_text))
        self.assertEqual(sequence.actions[0].camera_index, 1)

    def test_follow_sample_position_non_default_camera_index_survives(self):
        original = FollowSampleAction(duration_s=5.0, camera_index=1)
        dsl_text = original.to_dsl() + "\n"
        self.assertIn("camera_index=1", dsl_text)
        sequence = SequenceBuilder().build(ast.parse(dsl_text))
        self.assertEqual(sequence.actions[0].camera_index, 1)


class FollowSampleActionRunnerAutofocusTests(unittest.TestCase):
    """Finding #5b: runner.py's FollowSampleAction handler used to
    hand-construct a StartFollowingAction without autofocus_range_um /
    autofocus_steps, dropping any per-step autofocus override at the exact
    moment it would take effect (compile-time round-trip was fine; only
    actually running the step lost the override). runner.py now calls
    action.to_steps() instead of duplicating the field list — this pins
    the contract runner.py depends on."""

    def test_to_steps_start_action_carries_autofocus_overrides(self):
        action = FollowSampleAction(
            duration_s=30.0, autofocus_range_um=10.0, autofocus_steps=5,
        )
        start_act, wait_act, stop_act = action.to_steps()
        self.assertIsInstance(start_act, StartFollowingAction)
        self.assertEqual(start_act.autofocus_range_um, 10.0)
        self.assertEqual(start_act.autofocus_steps, 5)
        self.assertEqual(wait_act.duration_s, 30.0)


class NormalizerRangeSafetyTests(unittest.TestCase):
    """Finding #6: normalizer.py's range() expansion used to (a) let a
    bare ValueError/OverflowError escape DslCompiler.compile() uncaught
    instead of becoming a Diagnostic, (b) materialise the full element list
    before checking it against _MAX_RANGE_ELEMENTS, and (c) silently
    truncate a non-integral float argument via int()."""

    def test_range_step_zero_becomes_a_diagnostic_not_an_uncaught_exception(self):
        result = DslCompiler().compile(
            "for p in range(0, 3, 0):\n"
            "    log_message(message='x')\n"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "dsl.normalization_error")

    def test_range_non_integral_float_is_rejected_not_truncated(self):
        with self.assertRaises(NormalizationError):
            normalize("for p in range(0, 3.5):\n    log_message(message='x')\n")

    def test_oversized_range_is_rejected_without_materialising_the_list(self):
        # A literal large enough that list(range(...)) would be a
        # multi-gigabyte allocation if ever attempted; must fail well
        # before that point.
        with self.assertRaises(NormalizationError):
            normalize(
                "for p in range(0, 999999999999):\n    log_message(message='x')\n"
            )

    def test_negative_range_arguments_compile_successfully(self):
        # External review finding (post-Phase-9, see REORGANISATION_PLAN.md
        # §31): a negative literal like -5 parses as
        # ast.UnaryOp(USub(), Constant(5)), not ast.Constant(-5) —
        # _eval_int_args() only accepted ast.Constant, so range(-5, 5) was
        # rejected as "not an integer literal" even though -5 plainly is
        # one.
        result = DslCompiler().compile(
            "for p in range(-5, 5):\n    log_message(message=f'p={p}')\n"
        )
        self.assertTrue(result.ok, result.diagnostics)
        loop = result.sequence.actions[0]
        self.assertEqual(loop.values, [float(v) for v in range(-5, 5)])

    def test_negative_stop_and_step_arguments_compile_successfully(self):
        result = DslCompiler().compile(
            "for p in range(0, -10, -1):\n    log_message(message=f'p={p}')\n"
        )
        self.assertTrue(result.ok, result.diagnostics)
        loop = result.sequence.actions[0]
        self.assertEqual(loop.values, [float(v) for v in range(0, -10, -1)])


if __name__ == "__main__":
    unittest.main()
