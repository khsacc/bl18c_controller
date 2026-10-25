"""Hardware-free checks for the auto-generated IPAnalyzer batch macro."""
from __future__ import annotations

import ast
import csv
import shutil
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from apps.exp_scheduler.ipa_macro import (
    DEVELOPER_LINE, build_macro, macro_filename, write_ipa_macro,
)
from apps.exp_scheduler.xrd_master import FIELDS, MASTER_FILENAME

from tests import test_exp_scheduler_xrd_master as xrd_master_tests


def _files_list(text: str) -> list[str]:
    """FILES from the macro (the macro itself needs IPAnalyzer to run)."""
    module = ast.parse(text)
    for node in module.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "FILES":
            return ast.literal_eval(node.value)
    raise AssertionError("FILES not found")


class BuildMacroTests(unittest.TestCase):
    def setUp(self):
        run_dir = Path(__file__).resolve().parent / f"ipa_macro_test_{uuid.uuid4().hex}"
        self.addCleanup(shutil.rmtree, run_dir, True)
        self.xrd_dir = run_dir / "run_001_20261025_140000" / "XRD"
        self.xrd_dir.mkdir(parents=True)

    def add_image(self, name: str, **values):
        (self.xrd_dir / name).write_bytes(b"tif")
        master = self.xrd_dir / MASTER_FILENAME
        new = not master.exists()
        with master.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                writer.writeheader()
            writer.writerow({"xrd_file": name, **values})

    def test_layout_and_developer_line(self):
        text = build_macro("run_x", [{"xrd_file": "a.tif"}], datetime(2026, 10, 25))
        lines = text.splitlines()
        self.assertEqual(lines[1], DEVELOPER_LINE)
        order = [text.index(s) for s in (
            "= settings =", "= processing =", "= images =", "FILES = [", "= run =")]
        self.assertEqual(order, sorted(order))
        for setting in ("FIND_SPOTS = False", "READ_MASK = False"):
            self.assertIn(setting, text)
        self.assertIn("IPA.Profile.MaskSpotsBeforeGetProfile = FIND_SPOTS", text)
        self.assertIn("IPA.IntegralProperty.ReadMask(maskFile)", text)
        ast.parse(text)

    def test_files_follow_master_order_and_skip_missing_images(self):
        self.add_image("s_Pgas_1.5MPa_2026-10-25-14-10-00_2x2.tif",
                       start_time="2026-10-25T14:10:00.000", pressure_start_MPa="1.5",
                       temperature_b_mean_K="300.04")
        self.add_image("s_Pgas_0.5MPa_2026-10-25-14-20-00_2x2.tif")
        self.add_image("gone.tif")
        (self.xrd_dir / "gone.tif").unlink()
        (self.xrd_dir / "manual.tif").write_bytes(b"tif")

        path = write_ipa_macro(self.xrd_dir)

        self.assertEqual(path.name, macro_filename("run_001_20261025_140000"))
        self.assertEqual(path.name, "IPAnalyzer_analysis_macro_run_001_20261025_140000.mcr")
        raw = path.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\r\r", raw)
        text = raw.decode("utf-8")
        self.assertEqual(_files_list(text), [
            "s_Pgas_1.5MPa_2026-10-25-14-10-00_2x2.tif",
            "s_Pgas_0.5MPa_2026-10-25-14-20-00_2x2.tif",
        ])
        self.assertIn("# 2026-10-25 14:10:00  P 1.500 MPa  TB 300.0 K", text)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_without_master_writes_empty_list(self):
        self.assertEqual(_files_list(write_ipa_macro(self.xrd_dir).read_text("utf-8")), [])


class RunnerWritesMacroTests(unittest.TestCase):
    # Same runner fixture as the XRD master tests.
    setUp = xrd_master_tests.XrdMasterTests.setUp
    rows = xrd_master_tests.XrdMasterTests.rows

    def macro_path(self):
        return self.save_dir / macro_filename(self.save_dir.parent.name)

    def test_each_saved_image_updates_macro(self):
        self.runner._do_take_xrd(self.action, 0)
        self.runner._do_take_xrd(self.action, 1)
        files = _files_list(self.macro_path().read_text("utf-8"))
        self.assertEqual(files, [row["xrd_file"] for row in self.rows()])
        self.assertEqual(len(files), 2)

    def test_macro_failure_does_not_stop_capture(self):
        with patch("apps.exp_scheduler.runner.write_ipa_macro", side_effect=OSError("locked")):
            self.runner._do_take_xrd(self.action, 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.macro_path().exists())


if __name__ == "__main__":
    unittest.main()
