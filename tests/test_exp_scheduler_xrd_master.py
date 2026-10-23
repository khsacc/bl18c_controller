"""Hardware-free checks for the XRD image master CSV."""
from __future__ import annotations

import csv
import os
import shutil
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from apps.exp_scheduler.actions import TakeXrdAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.xrd_master import FIELDS, MASTER_FILENAME
from apps.Rad_icon_2022.image_utils import read_tiff_metadata

from tests.exp_scheduler_fakes import FakeStageController


class _Pace:
    def __init__(self):
        self.pressure = 1.2
        self.connected = True

    def get_cached_readings(self):
        return {"connected": self.connected, "timestamp": time.time(),
                "pressure_mpa": self.pressure}


class _LakeShore:
    def __init__(self):
        self.a, self.b = 100.0, 200.0
        self.connected = True

    def get_cached_readings(self):
        return {
            "connected": self.connected,
            "timestamp": time.time(),
            "temperature_a_k": self.a,
            "temperature_b_k": self.b,
        }


class _Radicon:
    width = 1000

    def __init__(self, pace, lake):
        self.pace = pace
        self.lake = lake

    def capture_triggered(self, exposure_ms, timeout_ms=None):
        self.pace.pressure = 1.4
        self.lake.a, self.lake.b = 102.0, 204.0
        return np.zeros((4, 4), dtype=np.uint16)


class XrdMasterTests(unittest.TestCase):
    def setUp(self):
        self.save_dir = Path(__file__).resolve().parent / f"xrd_master_test_{uuid.uuid4().hex}"
        self.save_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.save_dir)
        self.pace = _Pace()
        self.lake = _LakeShore()
        self.stage = FakeStageController({3: 30, 4: 40, 5: 50, 7: 70, 9: 90})
        self.radicon = _Radicon(self.pace, self.lake)
        self.runner = SequenceRunner(Sequence(), DeviceContext(
            controller=self.stage, pace5000=self.pace,
            lakeshore=self.lake, radicon=self.radicon,
        ))
        self.action = TakeXrdAction(
            exposure_ms=10, save=True, prefix="sample", save_dir=str(self.save_dir),
            defect_enabled=False, dark_enabled=False,
        )

    def rows(self):
        with (self.save_dir / MASTER_FILENAME).open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, FIELDS)
            return list(reader)

    def test_saved_images_append_distinct_rows_with_start_end_conditions(self):
        self.runner._do_take_xrd(self.action, 0)
        self.runner._do_take_xrd(self.action, 1)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["xrd_file"], rows[1]["xrd_file"])
        for row in rows:
            self.assertTrue((self.save_dir / row["xrd_file"]).is_file())
            self.assertLessEqual(row["start_time"], row["end_time"])
            self.assertEqual([row[f"Ch{ch}_pulse"] for ch in (3, 4, 5, 7, 9)],
                             ["30", "40", "50", "70", "90"])
        self.assertEqual(rows[0]["pressure_start_MPa"], "1.2")
        self.assertEqual(rows[0]["pressure_end_MPa"], "1.4")
        self.assertEqual(rows[0]["temperature_a_mean_K"], "101.0")
        self.assertEqual(rows[0]["temperature_b_mean_K"], "202.0")

    def test_saved_tiff_header_contains_shared_instrument_metadata(self):
        self.runner._do_take_xrd(self.action, 0)
        image = self.save_dir / self.rows()[0]["xrd_file"]
        header = read_tiff_metadata(image)
        self.assertEqual(header["exposure_ms"], 10)
        self.assertEqual(header["stage_ch3_pulses"], 30)
        self.assertEqual(header["stage_ch9_pulses"], 90)
        self.assertEqual(header["pace5000_pressure_mpa"], 1.4)
        self.assertEqual(header["lakeshore_t_a_k"], 102.0)
        self.assertEqual(header["lakeshore_t_b_k"], 204.0)

    def test_disconnected_instruments_leave_values_empty(self):
        self.pace.connected = False
        self.lake.connected = False
        self.runner._do_take_xrd(self.action, 0)
        row = self.rows()[0]
        for field in FIELDS:
            if field.startswith("pressure_") or field.startswith("temperature_"):
                self.assertEqual(row[field], "")

    def test_unavailable_readings_leave_only_affected_fields_empty(self):
        self.pace.pressure = float("nan")
        self.lake.b = None
        self.stage.fail_on = {("get_ch_pos", 9)}
        self.runner._do_take_xrd(self.action, 0)
        row = self.rows()[0]
        self.assertEqual(row["Ch9_pulse"], "")
        self.assertEqual(row["Ch7_pulse"], "70")
        self.assertEqual(row["pressure_start_MPa"], "")
        self.assertEqual(row["pressure_end_MPa"], "1.4")
        self.assertEqual(row["temperature_a_mean_K"], "101.0")
        self.assertEqual(row["temperature_b_mean_K"], "")

    def test_existing_incompatible_master_is_preserved(self):
        master = self.save_dir / MASTER_FILENAME
        master.write_text("other,columns\nold,data\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incompatible columns"):
            self.runner._do_take_xrd(self.action, 0)
        self.assertEqual(master.read_text(encoding="utf-8"), "other,columns\nold,data\n")

    def test_failed_image_save_does_not_create_master_row(self):
        with patch("apps.Rad_icon_2022.image_utils.save_tiff", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Could not save XRD image"):
                self.runner._do_take_xrd(self.action, 0)
        self.assertFalse((self.save_dir / MASTER_FILENAME).exists())

    def test_unsaved_capture_does_not_create_master(self):
        self.action.save = False
        self.runner._do_take_xrd(self.action, 0)
        self.assertFalse((self.save_dir / MASTER_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
