"""Tests for validator.models.group_diagnostics (merging repeated loop errors)."""
import unittest

from apps.exp_scheduler.validator.models import (
    Diagnostic, Severity, ValidationPhase, group_diagnostics,
)


def _d(message, code="preflight.stage.move_blocked", path="2.0", sev=Severity.ERROR):
    return Diagnostic(sev, code, message, ValidationPhase.PREFLIGHT, action_path=path)


class GroupDiagnosticsTest(unittest.TestCase):
    def test_same_error_across_iterations_is_merged(self):
        groups = group_diagnostics([
            _d("Step3: move Ch9: blocked"),
            _d("Step5: move Ch9: blocked"),
            _d("Step7: move Ch9: blocked"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].count, 3)
        self.assertEqual(groups[0].steps, (3, 5, 7))
        self.assertEqual(groups[0].first.message, "Step3: move Ch9: blocked")

    def test_different_cause_is_not_merged(self):
        groups = group_diagnostics([
            _d("Step3: move Ch4: limit exceeded (target 100)"),
            _d("Step5: move Ch4: limit exceeded (target 200)"),
            _d("Step7: move Ch4: blocked", code="preflight.stage.global_limit_exceeded"),
        ])
        self.assertEqual([g.count for g in groups], [1, 1, 1])

    def test_different_action_is_not_merged(self):
        groups = group_diagnostics([_d("Step3: x", path="0"), _d("Step4: x", path="1")])
        self.assertEqual(len(groups), 2)

    def test_message_without_step_prefix(self):
        groups = group_diagnostics([_d("no prefix"), _d("no prefix")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].count, 2)
        self.assertEqual(groups[0].steps, ())


if __name__ == "__main__":
    unittest.main()
