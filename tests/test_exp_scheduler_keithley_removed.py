import ast
import csv
import tempfile
import unittest
from dataclasses import fields

from apps.exp_scheduler.actions import action_from_dict
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl import ALLOWED_FUNCTIONS, DSL_VERSION
from apps.exp_scheduler.dsl.api import DSL_NAMESPACE
from apps.exp_scheduler.dsl.parser import SequenceBuilder, SequenceBuildError
from apps.exp_scheduler.dsl.validator import ASTValidator
from apps.exp_scheduler.log_manager import RunLogger
from apps.exp_scheduler.ui.step_editor import _DEVICE_OPS, _PAGE_FACTORIES


class ExpSchedulerKeithleyRemovalTests(unittest.TestCase):
    def test_dsl_contract_does_not_export_read_intensity(self):
        self.assertEqual(DSL_VERSION, "2.0.0")
        self.assertNotIn("read_intensity", ALLOWED_FUNCTIONS)
        self.assertNotIn("read_intensity", DSL_NAMESPACE)

        errors = ASTValidator().validate('read_intensity(variable="I")')

        self.assertTrue(errors)
        self.assertIn("Unknown function: 'read_intensity'", errors[0])

    def test_parser_and_action_registry_do_not_build_read_intensity(self):
        # Pre-Phase-2, SequenceBuilder silently built an empty Sequence for
        # an unrecognised command (no builder registered -> dropped, not
        # rejected). Phase 2 made this fail-closed: SequenceBuilder now
        # rejects unknown functions on its own, even when called directly
        # without going through ASTValidator first (as here).
        tree = ast.parse('read_intensity(variable="I")')

        with self.assertRaises(SequenceBuildError) as cm:
            SequenceBuilder().build(tree)
        self.assertTrue(
            any(d.code == "dsl.unknown_function" for d in cm.exception.diagnostics)
        )

        with self.assertRaises(ValueError):
            action_from_dict({"type": "read_intensity", "variable_name": "I"})

    def test_device_context_has_no_keithley_backend(self):
        self.assertNotIn("keithley", {field.name for field in fields(DeviceContext)})

    def test_step_editor_has_no_keithley_option(self):
        self.assertNotIn("Keithley", _DEVICE_OPS)
        self.assertNotIn("read_intensity", _PAGE_FACTORIES)

    def test_conditions_csv_header_excludes_keithley_column(self):
        logger = RunLogger(DeviceContext())
        with tempfile.TemporaryDirectory() as tmp:
            logger.start(
                path="test_run",
                devices=[],
                sequence_dict={"schema": "exp_scheduler", "actions": []},
                global_limits_dict={},
                log_base_dir=tmp,
            )
            log_dir = logger.log_dir
            logger.stop()

            with (log_dir / "conditions.csv").open(newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh))

        self.assertNotIn("keithley_I", header)


if __name__ == "__main__":
    unittest.main()
