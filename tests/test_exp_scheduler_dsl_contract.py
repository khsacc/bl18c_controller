"""
DSL contract inventory + characterization tests — apps/exp_scheduler
REORGANISATION_PLAN.md Phase 0 (contract-surface tests) and Phase 2
(fail-closed regression tests).

Two kinds of tests live here:

1. Contract-surface tests that cross-check dsl/__init__.py::ALLOWED_FUNCTIONS,
   dsl/_registry.py's CommandSpec registry, and dsl/api.py's module attributes
   against each other and against the hand-written
   tests/exp_scheduler_dsl_inventory.py table, plus a min-valid-call /
   required-kwarg-omission matrix for every allowed command. These are meant
   to stay green and catch drift. Phase 9 removed dsl/api.py::DSL_NAMESPACE
   (a legacy exec()-globals dict) — see
   tests/test_exp_scheduler_dsl_legacy_cleanup.py for the dedicated tests on
   what replaced it (every dsl/api.py function is now a stub that always
   raises NotImplementedError; get_registry() is the single source of truth).

2. Regression tests for the silent-acceptance / argument-loss bugs recorded
   in REORGANISATION_PLAN.md §2.2. These started as Phase 0 characterization
   tests pinning down baseline (`e6cb526`) behaviour precisely; Phase 2 fixed
   most of them (unknown keyword, unbound bare name, `Assign`/`If`,
   `normal_stop`, `duration=0`), so the assertions below now pin the
   *corrected* fail-closed behaviour instead. `log_message(message="")`
   is the one item Phase 0/2 deliberately left accepted (kept for delimiter-
   style empty log lines) — see REORGANISATION_PLAN.md §12.5 decision #6.
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

from apps.exp_scheduler.actions import LogAction, StageAction, WaitAction
from apps.exp_scheduler.dsl import ALLOWED_FUNCTIONS
from apps.exp_scheduler.dsl import api
from apps.exp_scheduler.dsl._registry import get_registry
from apps.exp_scheduler.dsl.parser import SequenceBuilder, SequenceBuildError
from apps.exp_scheduler.dsl.validator import ASTValidator

from tests.exp_scheduler_dsl_inventory import (
    ALLOWED_COMMAND_INVENTORY,
    COMMAND_INVENTORY,
)

# ── AST-surgery helpers (build call variants without hand-editing strings) ──


def _parse_call(call_src: str) -> ast.Call:
    return ast.parse(call_src, mode="eval").body


def _unparse_call(call: ast.Call) -> str:
    expr = ast.Expression(body=call)
    ast.fix_missing_locations(expr)
    return ast.unparse(expr)


def _without_kwarg(call_src: str, name: str) -> str:
    call = _parse_call(call_src)
    call.keywords = [kw for kw in call.keywords if kw.arg != name]
    return _unparse_call(call)


def _with_kwarg(call_src: str, name: str, value) -> str:
    call = _parse_call(call_src)
    node = ast.Constant(value=value)
    for kw in call.keywords:
        if kw.arg == name:
            kw.value = node
            return _unparse_call(call)
    call.keywords.append(ast.keyword(arg=name, value=node))
    return _unparse_call(call)


def _compile_ok(source: str):
    """Run the real compile path (validate then build); raise on validator error."""
    errors = ASTValidator().validate(source)
    if errors:
        raise AssertionError(f"expected no validator errors for {source!r}, got {errors}")
    return SequenceBuilder().build(ast.parse(source))


# ── 1. Contract-surface tests ────────────────────────────────────────────


class CommandSurfaceContractTests(unittest.TestCase):
    """dsl/__init__.py, dsl/api.py, dsl/parser.py, dsl/_registry.py must
    agree on which command names exist, except for the one known orphan."""

    def test_registry_names_have_factories_and_correspond_to_api_attributes(self):
        registry_names = set(get_registry().keys())
        # Every registered CommandSpec carries its own Action factory
        # (dsl/_registry.py::CommandSpec.factory, set via dsl/api.py's
        # @dsl_command(factory=...)) — there is no separate builder-name
        # table to compare against post-Phase-3.
        factory_names = {
            name for name, spec in get_registry().items() if spec.factory is not None
        }
        # Phase 9 removed dsl/api.py::DSL_NAMESPACE; every registered name
        # must still resolve to an attribute on the api module itself (the
        # always-NotImplementedError stub dsl_command() substitutes in).
        api_attr_names = {name for name in registry_names if hasattr(api, name)}

        self.assertEqual(registry_names, factory_names)
        self.assertEqual(registry_names, api_attr_names)

    def test_allowed_functions_matches_registry(self):
        # Phase 2 added "normal_stop" to ALLOWED_FUNCTIONS (it was already in
        # the registry — the whitelist was the one place out of sync,
        # rejecting a fully-implemented command and, via
        # StageAction(operation="normal_stop").to_dsl(), self-destructively
        # rejecting the app's own Visual -> Script conversion).
        registry_names = set(get_registry().keys())

        self.assertEqual(registry_names, ALLOWED_FUNCTIONS)

    def test_inventory_table_matches_real_command_sets(self):
        """Guards the hand-written inventory (tests/exp_scheduler_dsl_inventory.py)
        itself against drift, so later tests that consume it are trustworthy."""
        all_names = {c.name for c in COMMAND_INVENTORY}
        allowed_names = {c.name for c in ALLOWED_COMMAND_INVENTORY}

        self.assertEqual(allowed_names, ALLOWED_FUNCTIONS)
        self.assertEqual(all_names, allowed_names)

    def test_inventory_loop_var_kwargs_matches_the_binder(self):
        """tests/exp_scheduler_dsl_inventory.py's loop_var_kwargs and
        dsl/_registry.py's CommandSpec.argument_rules (populated by
        dsl/api.py's @dsl_command) are two independently hand-maintained
        records of the same fact (which keyword arguments accept a for-loop
        variable reference) — this test is what makes the inventory an
        actual guard against the registry's allow-list drifting, rather
        than just documentation no one re-checks. A prior gap here is
        exactly why Phase 2 originally shipped with set_speed(speed=p)
        compiling — the inventory recorded
        move_absolute/move_relative/set_pressure/set_and_wait_pressure/
        set_temperature's loop_var_kwargs correctly, but nothing compared
        that record against what the compiler actually allowed."""
        registry = get_registry()
        for entry in ALLOWED_COMMAND_INVENTORY:
            with self.subTest(command=entry.name):
                spec = registry[entry.name]
                loop_var_kwargs = frozenset(
                    name for name, rule in spec.argument_rules.items()
                    if rule.loop_var_allowed
                )
                self.assertEqual(entry.loop_var_kwargs, loop_var_kwargs)


class MinValidCallContractTests(unittest.TestCase):
    """Every allowed command's minimal valid call compiles cleanly and
    builds the expected Action type — table-driven per REORGANISATION_PLAN.md
    §7 Phase 0 item 3 / §8.2."""

    def test_min_call_compiles_and_builds_expected_action(self):
        for entry in ALLOWED_COMMAND_INVENTORY:
            with self.subTest(command=entry.name):
                sequence = _compile_ok(entry.min_call + "\n")
                self.assertEqual(len(sequence.actions), 1)
                self.assertIsInstance(sequence.actions[0], entry.action_type)

    def test_each_required_kwarg_omission_is_a_compile_error(self):
        for entry in ALLOWED_COMMAND_INVENTORY:
            for missing in entry.required_kwargs:
                with self.subTest(command=entry.name, missing=missing):
                    source = _without_kwarg(entry.min_call, missing) + "\n"
                    errors = ASTValidator().validate(source)
                    self.assertTrue(
                        any(
                            "missing required argument" in e and missing in e
                            for e in errors
                        ),
                        f"{entry.name}: expected a missing-argument error for "
                        f"{missing!r}, got {errors}",
                    )

    def test_unit_and_enum_kwargs_reject_invalid_values(self):
        for fname, spec in get_registry().items():
            entry = next((c for c in ALLOWED_COMMAND_INVENTORY if c.name == fname), None)
            if entry is None:
                continue  # not (yet) in the hand-written inventory
            for kwarg, rule in spec.argument_rules.items():
                if rule.valid_values is None:
                    continue
                with self.subTest(command=fname, kwarg=kwarg):
                    source = _with_kwarg(entry.min_call, kwarg, "__not_a_valid_value__") + "\n"
                    errors = ASTValidator().validate(source)
                    self.assertTrue(
                        any(f"invalid {kwarg!r} value" in e for e in errors),
                        f"{fname}: expected an invalid-{kwarg} error, got {errors}",
                    )


# ── 2. Phase 2 fail-closed regression tests (formerly §2.2 characterization) ──


class Phase2FailClosedRegressionTests(unittest.TestCase):
    """Each of these pinned a silent-acceptance / argument-loss bug at
    baseline e6cb526 (REORGANISATION_PLAN.md §2.2); Phase 2 closed the gap,
    so the assertions now pin the corrected fail-closed behaviour."""

    def test_unknown_keyword_is_rejected(self):
        source = 'wait(duration=1.0, unit="s", foo=123)\n'

        # ASTValidator itself still has no per-function keyword-name check —
        # that responsibility lives in SequenceBuilder's signature-bound
        # call binder (REORGANISATION_PLAN.md §7 Phase 2 item 2), not in the
        # AST whitelist layer.
        errors = ASTValidator().validate(source)
        self.assertEqual(errors, [])

        with self.assertRaises(SequenceBuildError) as cm:
            SequenceBuilder().build(ast.parse(source))
        messages = [d.message for d in cm.exception.diagnostics]
        self.assertTrue(
            any(d.code == "dsl.unknown_argument" for d in cm.exception.diagnostics),
            messages,
        )
        self.assertTrue(any("foo" in m for m in messages), messages)

    def test_normal_stop_is_allowed_and_round_trips(self):
        self.assertIn("normal_stop", ALLOWED_FUNCTIONS)
        self.assertTrue(hasattr(api, "normal_stop"))
        self.assertIn("normal_stop", get_registry())

        errors = ASTValidator().validate("normal_stop()\n")
        self.assertEqual(errors, [])

        sequence = SequenceBuilder().build(ast.parse("normal_stop()\n"))
        self.assertEqual(len(sequence.actions), 1)
        action = sequence.actions[0]
        self.assertIsInstance(action, StageAction)
        self.assertEqual(action.operation, "normal_stop")

    def test_assign_statement_is_rejected(self):
        source = "x = 1.0\nlog_message(message='after')\n"

        errors = ASTValidator().validate(source)
        self.assertTrue(any("assignment" in e and "is not allowed" in e for e in errors))

    def test_if_statement_is_rejected(self):
        source = "if True:\n    log_message(message='x')\n"

        errors = ASTValidator().validate(source)
        self.assertTrue(any("if statement is not allowed" in e for e in errors))

    def test_unbound_bare_name_is_rejected_as_a_typo_not_a_loop_variable(self):
        source = (
            'set_pressure(pressure=pressure_typo, unit="MPa", '
            'rate=0.2, rate_unit="MPa/min")\n'
        )

        # Still not an ASTValidator-level check — see
        # test_unknown_keyword_is_rejected above.
        errors = ASTValidator().validate(source)
        self.assertEqual(errors, [])

        with self.assertRaises(SequenceBuildError) as cm:
            SequenceBuilder().build(ast.parse(source))
        self.assertTrue(
            any(d.code == "dsl.unbound_name" for d in cm.exception.diagnostics)
        )
        self.assertTrue(
            any("pressure_typo" in d.message for d in cm.exception.diagnostics)
        )

    def test_unbound_bare_name_inside_a_different_for_loop_is_still_rejected(self):
        # "p" is bound by the outer for loop, but the inner call sits
        # outside its body — shadowing/scope must follow AST nesting, not
        # "was this name bound *anywhere* in the source".
        source = (
            "for p in [1.0, 2.0]:\n"
            "    log_message(message='in loop')\n"
            'set_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min")\n'
        )

        with self.assertRaises(SequenceBuildError) as cm:
            SequenceBuilder().build(ast.parse(source))
        self.assertTrue(
            any(d.code == "dsl.unbound_name" for d in cm.exception.diagnostics)
        )

    def test_wait_duration_zero_is_rejected_at_compile_time(self):
        # REORGANISATION_PLAN.md §12.5 decision #6: closes the compile/
        # preflight contract gap — duration=0 used to pass compile and only
        # get caught later by PreValidator._check_durations() (see
        # tests/test_exp_scheduler_pre_validator.py).
        source = 'wait(duration=0.0, unit="s")\n'

        errors = ASTValidator().validate(source)
        self.assertTrue(any("duration must be >" in e for e in errors), errors)

    def test_follow_sample_position_duration_zero_is_rejected_at_compile_time(self):
        source = 'follow_sample_position(duration=0.0, unit="s")\n'

        errors = ASTValidator().validate(source)
        self.assertTrue(any("duration must be >" in e for e in errors), errors)

    def test_log_message_empty_string_remains_accepted(self):
        # REORGANISATION_PLAN.md §12.5 decision #6: deliberately left
        # accepted (e.g. a blank line used as a visual separator in the run
        # log), unlike wait()/follow_sample_position()'s duration=0 above.
        source = 'log_message(message="")\n'

        errors = ASTValidator().validate(source)
        self.assertEqual(errors, [])

        sequence = SequenceBuilder().build(ast.parse(source))
        self.assertEqual(sequence.actions[0].message, "")


if __name__ == "__main__":
    unittest.main()
