"""Hardware-free checks for the run-wide naming contract."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.exp_scheduler.actions import TakeXrdAction, TakeSpectrumAction, WaitAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl.compiler import DslCompiler
from apps.exp_scheduler.ipa_macro import write_ipa_macro
from apps.exp_scheduler.log_manager import RunLogger
from apps.exp_scheduler.run_files import normalise_run_name
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.sequence import Sequence
from tests import test_exp_scheduler_xrd_master as xrd_tests
from tests import test_exp_scheduler_spectrum as spectrum_tests
from apps.exp_scheduler.spectrum_quality import SpectrumQuality
from apps.exp_scheduler import runner as runner_module


class RunNameTests(unittest.TestCase):
    def test_windows_names_are_checked_without_truncating_dots_or_unicode(self):
        self.assertEqual(normalise_run_name(" Sample.日本語 "), "Sample.日本語")
        self.assertEqual(normalise_run_name("  "), "run")
        for name in ("../sample", "a/b", "a\\b", "CON", "com1.txt", "nul",
                     "a:", "a?", "a\nname", "..", "a.", "x" * 81):
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalise_run_name(name)

    def test_invalid_name_is_rejected_before_logger_or_device_access(self):
        with patch.object(RunLogger, "start") as start:
            with self.assertRaises(ValueError):
                SequenceRunner(Sequence(), DeviceContext(), log_path="../bad")
            start.assert_not_called()

    def test_all_logs_share_run_name_and_sequence_is_loadable(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = SequenceRunner(Sequence(actions=[WaitAction(duration_s=0)]),
                                    DeviceContext(), log_path="Sample.v1", data_dir=directory)
            runner.run()
            logs = runner.log_dir
            self.assertEqual({p.name for p in logs.iterdir()}, {
                "Sample.v1_sequence.json", "Sample.v1_metadata.json",
                "Sample.v1_ops.log", "Sample.v1_conditions.csv",
            })
            self.assertTrue(runner._logger.run_dir.name.startswith("Sample.v1_"))
            self.assertEqual(Sequence.load(logs / "Sample.v1_sequence.json").to_dict(),
                             runner._sequence.to_dict())
            self.assertEqual(json.loads((logs / "Sample.v1_metadata.json").read_text())["run_name"],
                             "Sample.v1")

    def test_old_dsl_and_json_prefixes_load_but_are_not_emitted(self):
        source = 'take_xrd(prefix="old")\ntake_spectrum(prefix="old")'
        result = DslCompiler().compile(source)
        self.assertTrue(result.ok, result.diagnostics)
        for action in result.sequence.actions:
            self.assertNotIn("prefix", action.to_dict())
            self.assertNotIn("prefix=", action.to_dsl())
        self.assertEqual(TakeSpectrumAction.from_dict({"prefix": "old"}).prefix, "old")
        self.assertEqual(TakeXrdAction.from_dict({"prefix": "old"}).prefix, "old")


class NamedXrdTests(unittest.TestCase):
    setUp = xrd_tests.XrdMasterTests.setUp

    def test_nondefault_name_preserves_suffixes_and_macro_regeneration(self):
        self.runner._log_path = "Sample.v1"
        self.runner._global_xrd.filename_suffixes = ("index",)
        self.runner._do_take_xrd(self.action, 0)
        self.assertTrue((self.save_dir / "Sample.v1_XRD_0001_2x2.tif").is_file())
        master = self.save_dir / "Sample.v1_xrd_master.csv"
        self.assertIn("Sample.v1_XRD_0001_2x2.tif", master.read_text())
        macro = write_ipa_macro(self.save_dir)
        self.assertEqual(macro.name, "Sample.v1_IPAnalyzer_analysis_macro.mcr")
        self.assertIn(master.name, macro.read_text())
        self.assertIn("Sample.v1_XRD_0001_2x2.tif", macro.read_text())


class NamedSpectrumTests(unittest.TestCase):
    _quality_runner = spectrum_tests.SpectrumActionTests._quality_runner

    def test_run_name_replaces_legacy_prefix_and_three_exports_share_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, _ = self._quality_runner(directory, settings_overrides={"filename_suffixes": ()})
            runner._log_path = "Sample.v1"
            with patch.object(runner_module, "assess_spectrum",
                              return_value=SpectrumQuality(False, True, r_squared=0.99)):
                runner._do_take_spectrum(TakeSpectrumAction(prefix="unused"), 0)
                runner._do_take_spectrum(TakeSpectrumAction(prefix="another"), 1)
            output = Path(directory) / "spectrum"
            for stem in ("Sample.v1_spectrum_Default_attempt_01", "Sample.v1_spectrum_Default_attempt_01_2"):
                for ext in (".npz", ".csv", ".png"):
                    self.assertTrue((output / (stem + ext)).is_file())
            for master in ("ruby_master_all_files.csv", "ruby_master_summary.csv"):
                self.assertTrue((output / ("Sample.v1_" + master)).is_file())

    def test_ruby_labels_are_safe_and_original_names_remain_in_exports(self):
        import csv
        import numpy as np
        from apps.exp_scheduler.run_files import ruby_filename_label

        self.assertEqual(ruby_filename_label(" ルビー A. "), "ルビー A")
        self.assertEqual(ruby_filename_label("../A\\B:*?\n"), "_A_B____")
        self.assertEqual(ruby_filename_label("... "), "ruby")
        self.assertLessEqual(len(ruby_filename_label("😀" * 50).encode("utf-16-le")), 80)
        with tempfile.TemporaryDirectory() as directory:
            names = ("ルビー/A", "ルビー?A")
            positions = {name: {"ch3": 0, "ch4": 2, "ch5": 0} for name in names}
            runner, _ = self._quality_runner(directory, settings_overrides={
                "filename_suffixes": (), "ruby_positions": positions,
            })
            with patch.object(runner_module, "assess_spectrum",
                              return_value=SpectrumQuality(False, True, r_squared=0.99)):
                for index, name in enumerate(names):
                    runner._do_take_spectrum(TakeSpectrumAction(position_name=name), index)
            output = Path(directory) / "spectrum"
            with (output / "run_ruby_master_all_files.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([row["ruby_name"] for row in rows], list(names))
            self.assertEqual([row["spectrum_file"] for row in rows], [
                "run_spectrum_ルビー_A_attempt_01.npz", "run_spectrum_ルビー_A_attempt_01_2.npz",
            ])
            for row in rows:
                path = output / row["spectrum_file"]
                for ext in (".npz", ".csv", ".png"):
                    self.assertTrue(path.with_suffix(ext).is_file())
                with np.load(path) as data:
                    self.assertEqual(str(data["ruby_position_name"]), row["ruby_name"])
