"""
Tests for apps/exp_scheduler/validation_service.py —
REORGANISATION_PLAN.md Phase 7 (§7 Phase 7).
"""
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

from apps.exp_scheduler import validation_service
from apps.exp_scheduler.actions import LogAction, TakeDarkAction, WaitAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl.compiler import ActionSourceMap
from apps.exp_scheduler.scheduler_settings import GlobalXrdSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator

from tests.exp_scheduler_fakes import FakeRadicon as _FakeRadicon
from tests.exp_scheduler_fakes import FakeStageController


class ValidateDslTests(unittest.TestCase):
    def test_syntax_error_returns_no_sequence_and_no_preflight(self):
        report = validation_service.validate_dsl("wait(duration=", DeviceContext())

        self.assertFalse(report.ok)
        self.assertIsNone(report.sequence)
        self.assertTrue(report.errors)

    def test_unknown_function_compile_error_returns_no_sequence(self):
        report = validation_service.validate_dsl(
            "definitely_not_a_real_command(x=1)", DeviceContext(),
        )

        self.assertFalse(report.ok)
        self.assertIsNone(report.sequence)

    def test_valid_dsl_matches_direct_pre_validator_call(self):
        source = 'wait(duration=1, unit="s")\nlog_message(message="hi")'
        ctx = DeviceContext(radicon=_FakeRadicon())

        report = validation_service.validate_dsl(source, ctx)
        self.assertIsNotNone(report.sequence)

        direct = PreValidator().validate(report.sequence, ctx)
        self.assertEqual(
            [d.message for d in report.diagnostics],
            [d.message for d in direct.diagnostics],
        )
        self.assertEqual(dict(report.baseline_positions), dict(direct.baseline_positions))

    def test_source_line_backfilled_for_top_level_action(self):
        source = (
            'wait(duration=1, unit="s")\n'
            "take_dark(exposure_ms=0)\n"
            'wait(duration=2, unit="s")\n'
        )
        report = validation_service.validate_dsl(
            source, DeviceContext(radicon=_FakeRadicon()),
        )

        diag = next(
            d for d in report.diagnostics if d.code == "static.xrd.non_finite_exposure"
        )
        self.assertEqual(diag.source_line, 2)

    def test_source_line_backfilled_to_enclosing_for_statement(self):
        # ActionSourceMap only tracks top-level statement lines, so a
        # Diagnostic on an action nested in a for-loop body is attributed
        # to the `for` statement's own line, not a (nonexistent) per-line
        # location inside the loop body.
        source = (
            'wait(duration=1, unit="s")\n'
            "for i in [1, 2]:\n"
            "    take_dark(exposure_ms=0)\n"
            'wait(duration=3, unit="s")\n'
        )
        report = validation_service.validate_dsl(
            source, DeviceContext(radicon=_FakeRadicon()),
        )

        diag = next(
            d for d in report.diagnostics if d.code == "static.xrd.non_finite_exposure"
        )
        self.assertEqual(diag.action_path, "[1].body[0]")
        self.assertEqual(diag.source_line, 2)  # the `for i in [1, 2]:` line


class ValidateSequenceTests(unittest.TestCase):
    def test_no_source_map_does_not_crash_and_leaves_source_line_none(self):
        sequence = Sequence(actions=[TakeDarkAction(exposure_ms=0)])
        report = validation_service.validate_sequence(
            sequence, DeviceContext(radicon=_FakeRadicon()),
        )

        diag = next(
            d for d in report.diagnostics if d.code == "static.xrd.non_finite_exposure"
        )
        self.assertIsNone(diag.source_line)

    def test_diagnostic_with_out_of_range_action_path_index_is_left_unchanged(self):
        sequence = Sequence(actions=[TakeDarkAction(exposure_ms=0)])
        # A source_map shorter than the Sequence it's paired with should
        # never happen in practice, but _with_source_lines() must not raise.
        report = validation_service.validate_sequence(
            sequence, DeviceContext(radicon=_FakeRadicon()),
            source_map=ActionSourceMap(statement_lines=()),
        )
        diag = next(
            d for d in report.diagnostics if d.code == "static.xrd.non_finite_exposure"
        )
        self.assertIsNone(diag.source_line)


class RevalidateForRunTests(unittest.TestCase):
    def test_matches_validate_sequence_plus_run_gate_diagnostics_given_a_certificate(self):
        # Phase 8: revalidate_for_run() is validate_sequence() plus the
        # certificate diff (REORGANISATION_PLAN.md §7 Phase 8) — given a
        # certificate for the same, unchanged inputs, it must agree with a
        # fresh validate_sequence() exactly (no extra run_gate.* diagnostics
        # beyond that). See tests/test_exp_scheduler_validation_service.py's
        # RunGateTests for the certificate-diff behaviour itself (Phase 8's
        # `test_matches_validate_sequence_for_the_same_inputs` predated
        # `certificate`, and always got `run_gate.not_validated` — this
        # replaces it with the equivalent same-inputs check that actually
        # exercises the Phase 8 contract).
        sequence = Sequence(actions=[WaitAction(duration_s=1.0), LogAction(message="hi")])
        ctx = DeviceContext(radicon=_FakeRadicon())

        a = validation_service.validate_sequence(sequence, ctx)
        b = validation_service.revalidate_for_run(sequence, ctx, certificate=a.certificate)

        self.assertEqual(
            [(d.code, d.message) for d in a.diagnostics],
            [(d.code, d.message) for d in b.diagnostics],
        )
        self.assertEqual(a.ok, b.ok)


class _AlwaysEqualDevice:
    """A fake backend whose __eq__ always returns True for another instance
    of the same class — used to prove `_same_device_identity()` compares by
    `is`, not `==`. A backend that happened to gain value-equality (e.g. via
    a future dataclass conversion) must never be able to make a genuinely
    swapped instance look 'unchanged' to the Run gate."""

    def __eq__(self, other):
        return isinstance(other, _AlwaysEqualDevice)

    def __hash__(self):
        return 0


def _run_gate_codes(report) -> list[str]:
    return [d.code for d in report.diagnostics if d.code.startswith("run_gate.")]


class CertificateTests(unittest.TestCase):
    """ValidationReport.snapshot / .certificate — REORGANISATION_PLAN.md
    Phase 8 (§7 Phase 8)."""

    def _sequence(self) -> Sequence:
        return Sequence(actions=[WaitAction(duration_s=1.0)])

    def test_success_sets_snapshot_and_certificate(self):
        ctx = DeviceContext(controller=FakeStageController())
        report = validation_service.validate_sequence(self._sequence(), ctx)

        self.assertTrue(report.ok)
        self.assertIsNotNone(report.snapshot)
        self.assertIsNotNone(report.snapshot.stage)
        self.assertIsNotNone(report.certificate)

    def test_preflight_error_still_sets_snapshot_but_not_certificate(self):
        # Regression guard for review round 1, item 1: baseline diffing at
        # Run time needs fresh device state even when this validation
        # itself found unrelated errors — report.snapshot must not be
        # gated on report.ok the way report.certificate is.
        ctx = DeviceContext(controller=FakeStageController())
        report = validation_service.validate_sequence(Sequence(actions=[]), ctx)

        self.assertFalse(report.ok)
        self.assertIsNotNone(report.snapshot)
        self.assertIsNone(report.certificate)


class RunGateTests(unittest.TestCase):
    """revalidate_for_run()'s certificate diff — REORGANISATION_PLAN.md
    Phase 8 (§7 Phase 8)."""

    def _sequence(self) -> Sequence:
        return Sequence(actions=[WaitAction(duration_s=1.0)])

    def _validated(self, ctx, **kwargs):
        report = validation_service.validate_sequence(self._sequence(), ctx, **kwargs)
        self.assertTrue(report.ok, report.errors)
        self.assertIsNotNone(report.certificate)
        return report.certificate

    def test_no_certificate_rejects_run(self):
        ctx = DeviceContext(controller=FakeStageController())
        result = validation_service.revalidate_for_run(
            self._sequence(), ctx, certificate=None,
        )

        self.assertFalse(result.ok)
        self.assertIn("run_gate.not_validated", _run_gate_codes(result))

    def test_unchanged_state_passes_with_no_run_gate_diagnostics(self):
        ctx = DeviceContext(controller=FakeStageController())
        certificate = self._validated(ctx)

        result = validation_service.revalidate_for_run(
            self._sequence(), ctx, certificate=certificate,
        )

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(_run_gate_codes(result), [])

    def test_result_certificate_is_always_none_even_on_a_clean_pass(self):
        # Regression guard for review round 1, item 2: a fresh, individually
        # clean preflight must never be mistaken for a new certifiable pass.
        ctx = DeviceContext(controller=FakeStageController())
        certificate = self._validated(ctx)

        result = validation_service.revalidate_for_run(
            self._sequence(), ctx, certificate=certificate,
        )

        self.assertIsNone(result.certificate)

    def test_sequence_changed_since_validate(self):
        ctx = DeviceContext(controller=FakeStageController())
        certificate = self._validated(ctx)

        changed = Sequence(actions=[WaitAction(duration_s=2.0)])
        result = validation_service.revalidate_for_run(
            changed, ctx, certificate=certificate,
        )

        self.assertIn("run_gate.sequence_changed", _run_gate_codes(result))

    def test_settings_changed_since_validate(self):
        ctx = DeviceContext(controller=FakeStageController())
        certificate = self._validated(ctx, global_xrd=GlobalXrdSettings())

        result = validation_service.revalidate_for_run(
            self._sequence(), ctx,
            global_xrd=GlobalXrdSettings(exposure_ms=2000),
            certificate=certificate,
        )

        self.assertIn("run_gate.settings_changed", _run_gate_codes(result))

    def test_device_context_changed_since_validate(self):
        ctx = DeviceContext(controller=FakeStageController())
        certificate = self._validated(ctx)

        # Same positions, genuinely different instance — a swap this test
        # must catch even though nothing about the stage state differs.
        swapped_ctx = DeviceContext(controller=FakeStageController())
        result = validation_service.revalidate_for_run(
            self._sequence(), swapped_ctx, certificate=certificate,
        )

        self.assertIn("run_gate.device_context_changed", _run_gate_codes(result))

    def test_device_context_changed_detected_even_with_value_equal_backend(self):
        # Regression guard for review round 2: identity comparison must use
        # `is`, not `==` — a backend whose __eq__ always returns True for
        # its own class must not defeat swap detection.
        pace_a, pace_b = _AlwaysEqualDevice(), _AlwaysEqualDevice()
        self.assertEqual(pace_a, pace_b)  # sanity: they are value-equal

        ctx = DeviceContext(pace5000=pace_a)
        certificate = self._validated(ctx)

        swapped_ctx = DeviceContext(pace5000=pace_b)
        result = validation_service.revalidate_for_run(
            self._sequence(), swapped_ctx, certificate=certificate,
        )

        self.assertIn("run_gate.device_context_changed", _run_gate_codes(result))

    def test_same_device_identity_helper_uses_is_not_eq(self):
        a, b = _AlwaysEqualDevice(), _AlwaysEqualDevice()
        self.assertEqual(a, b)

        self.assertFalse(
            validation_service._same_device_identity((a, None, None, None), (b, None, None, None))
        )
        self.assertTrue(
            validation_service._same_device_identity((a, None, None, None), (a, None, None, None))
        )

    def test_stage_moved_since_validate(self):
        controller = FakeStageController()
        ctx = DeviceContext(controller=controller)
        certificate = self._validated(ctx)

        controller.positions[4] = 123456  # moved by another window/process

        result = validation_service.revalidate_for_run(
            self._sequence(), ctx, certificate=certificate,
        )

        self.assertIn("run_gate.stage_moved_since_validate", _run_gate_codes(result))

    def test_stage_baseline_incomplete_when_fresh_read_fails(self):
        # Regression guard for review round 3: comparing only the channels
        # readable on both sides could misjudge an incomplete baseline as
        # "no movement" — Ch11 unreadable at Run time must produce
        # stage_baseline_incomplete, not stage_moved_since_validate (and
        # certainly not a silent pass).
        controller = FakeStageController()
        ctx = DeviceContext(controller=controller)
        certificate = self._validated(ctx)

        controller.fail_on = {("get_ch_pos", 11)}

        result = validation_service.revalidate_for_run(
            self._sequence(), ctx, certificate=certificate,
        )

        codes = _run_gate_codes(result)
        self.assertIn("run_gate.stage_baseline_incomplete", codes)
        self.assertNotIn("run_gate.stage_moved_since_validate", codes)

    def test_live_preflight_runs_to_completion_even_with_stale_certificate(self):
        # The fresh preflight is never short-circuited by a Run-gate
        # failure — a stale certificate AND a genuine fresh preflight error
        # must both surface at once.
        ctx = DeviceContext(controller=FakeStageController())
        certificate = self._validated(ctx)

        result = validation_service.revalidate_for_run(
            Sequence(actions=[]), ctx, certificate=certificate,
        )

        self.assertIn("run_gate.sequence_changed", _run_gate_codes(result))
        self.assertIn("static.sequence.empty", [d.code for d in result.diagnostics])


if __name__ == "__main__":
    unittest.main()
