"""Hardware-free checks for the ruby spectrum master CSV."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from apps.exp_scheduler.ruby_master import FIELDS, MASTER_FILENAME, append_ruby_record
from apps.exp_scheduler.ruby_sequence_master import (
    MASTER_FILENAME as SEQUENCE_FILENAME,
    SUMMARY_FIELDS, append_ruby_sequence_record, fields as sequence_fields,
)
from apps.exp_scheduler.xrd_master import FIELDS as XRD_FIELDS


class RubyMasterTests(unittest.TestCase):
    def test_sequence_master_puts_means_before_accepted_rubies(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            append_ruby_sequence_record(
                directory, position_names=("Ruby A", "Ruby B"),
                start_time="start", end_time="end", step_index=3,
                status="completed", accepted=[
                    {"ruby_name": "Ruby A", "attempt": 2, "quality_outcome": "passed",
                     "spectrum_file": "a_attempt_02.npz", "pressure_status": "valid",
                     "pressure_GPa": 2.3, "peak1_nm": 694.2, "peak2_nm": 692.8,
                     "temperature_used_K": 80.0},
                    {"ruby_name": "Ruby B", "attempt": 1, "quality_outcome": "passed",
                     "spectrum_file": "b_attempt_01.npz", "pressure_status": "valid",
                     "pressure_GPa": 2.5, "peak1_nm": 694.4, "peak2_nm": 693.0,
                     "temperature_used_K": 82.0},
                ],
            )
            with (directory / SEQUENCE_FILENAME).open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, sequence_fields(2))
                row = next(reader)
                self.assertEqual(list(reader), [])
            self.assertEqual(reader.fieldnames[:len(SUMMARY_FIELDS)], SUMMARY_FIELDS)
            self.assertEqual(row["ruby_count"], "2")
            self.assertEqual(row["pressure_valid_count"], "2")
            self.assertEqual(row["pressure_mean_GPa"], "2.4")
            self.assertEqual(row["peak1_mean_nm"], "694.3")
            self.assertEqual(row["temperature_used_mean_K"], "81.0")
            self.assertEqual(row["ruby_01_attempt"], "2")
            self.assertEqual(row["ruby_02_spectrum_file"], "b_attempt_01.npz")

    def test_sequence_master_omits_invalid_pressure_and_unmeasured_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            append_ruby_sequence_record(
                directory, position_names=("Ruby A", "Ruby B"),
                start_time="start", end_time="end", step_index=0,
                status="failed", accepted=[
                    {"ruby_name": "Ruby A", "quality_outcome": "passed",
                     "pressure_status": "temperature_out_of_range",
                     "pressure_GPa": 2.3, "peak1_nm": 694.2},
                ],
            )
            with (directory / SEQUENCE_FILENAME).open(newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["sequence_status"], "failed")
            self.assertEqual(row["pressure_valid_count"], "0")
            self.assertEqual(row["pressure_mean_GPa"], "")
            self.assertEqual(row["ruby_02_name"], "Ruby B")
            self.assertEqual(row["ruby_02_spectrum_file"], "")

    def test_appends_xrd_conditions_fit_peaks_and_pressure_correction(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_dir = Path(temporary)
            start = {"Ch3_pulse": 30, "Ch4_pulse": 40, "Ch5_pulse": 50,
                     "Ch7_pulse": 70, "Ch9_pulse": 90,
                     "pressure_MPa": 1.2, "temperature_a_K": 100.0,
                     "temperature_b_K": 200.0}
            end = {"pressure_MPa": 1.4, "temperature_a_K": 102.0,
                   "temperature_b_K": 204.0}
            acquired = {"fit": {"success": True, "fit": {"peaks": [
                {"position": 694.2, "position_err": 0.01},
                {"position": 692.8, "position_err": 0.02},
            ]}}}
            pressure = SimpleNamespace(
                status="valid", pressure_scale="ruby_shen_2020",
                pressure_gpa=2.3, pressure_err_gpa=0.04,
                temperature_correction_enabled=True,
                temperature_scale="ruby_datchi_2007_lt",
                temperature_source="lakeshore_b",
                measured_temperature_k=80.0, lambda_0_nm=694.3,
                t0_k=298.15, zero_pressure_peak_at_current_t_nm=692.8,
            )
            for attempt in (1, 2):
                append_ruby_record(
                    save_dir / f"ruby_attempt_{attempt:02d}.npz",
                    "2026-10-25T10:00:00.000", "2026-10-25T10:00:01.000",
                    start, end, ruby_name="Ruby A", attempt=attempt,
                    outcome="passed", acquired=acquired, r_squared=0.99,
                    pressure_record=pressure,
                )
            with (save_dir / MASTER_FILENAME).open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, FIELDS)
                rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertTrue(set(XRD_FIELDS) - {"xrd_file"} <= set(FIELDS))
            row = rows[0]
            self.assertEqual(row["ruby_name"], "Ruby A")
            self.assertEqual(row["spectrum_file"], "ruby_attempt_01.npz")
            self.assertEqual(row["attempt"], "1")
            self.assertEqual(row["Ch4_pulse"], "40")
            self.assertEqual(row["pressure_start_MPa"], "1.2")
            self.assertEqual(row["pressure_end_MPa"], "1.4")
            self.assertEqual(row["temperature_b_mean_K"], "202.0")
            self.assertEqual(row["fit_r_squared"], "0.99")
            self.assertEqual(row["peak1_nm"], "694.2")
            self.assertEqual(row["peak1_error_nm"], "0.01")
            self.assertEqual(row["peak2_nm"], "692.8")
            self.assertEqual(row["peak2_error_nm"], "0.02")
            self.assertEqual(row["pressure_scale"], "ruby_shen_2020")
            self.assertEqual(row["pressure_GPa"], "2.3")
            self.assertEqual(row["temperature_scale"], "ruby_datchi_2007_lt")
            self.assertEqual(row["temperature_source"], "lakeshore_b")
            self.assertEqual(row["temperature_used_K"], "80.0")
            self.assertEqual(row["lambda_0_corrected_nm"], "692.8")

    def test_missing_fit_and_pressure_leave_fields_blank(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_dir = Path(temporary)
            append_ruby_record(
                save_dir / "ruby.npz", "start", "end", {}, {},
                ruby_name="Ruby B", attempt=1, outcome="fit_failed",
                acquired={"fit": {"success": False}}, r_squared=None,
            )
            with (save_dir / MASTER_FILENAME).open(newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["quality_outcome"], "fit_failed")
            for field in ("fit_r_squared", "peak1_nm", "pressure_GPa",
                          "temperature_scale", "temperature_used_K",
                          "lambda_0_corrected_nm", "pressure_start_MPa"):
                self.assertEqual(row[field], "")

    def test_pressure_without_temperature_correction_leaves_correction_blank(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_dir = Path(temporary)
            pressure = SimpleNamespace(
                status="valid", pressure_scale="ruby_shen_2020",
                pressure_gpa=1.5, pressure_err_gpa=0.02,
                temperature_correction_enabled=False,
                lambda_0_nm=694.3,
            )
            append_ruby_record(
                save_dir / "ruby.npz", "start", "end", {}, {},
                ruby_name="Ruby A", attempt=1, outcome="passed",
                acquired={"fit": {"success": True, "fit": {
                    "Peak1": 694.2, "Peak1_Err": 0.01,
                }}}, r_squared=0.99, pressure_record=pressure,
            )
            with (save_dir / MASTER_FILENAME).open(newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["peak1_nm"], "694.2")
            self.assertEqual(row["peak1_error_nm"], "0.01")
            self.assertEqual(row["pressure_GPa"], "1.5")
            self.assertEqual(row["lambda_0_nm"], "694.3")
            self.assertEqual(row["temperature_scale"], "")
            self.assertEqual(row["temperature_source"], "")
            self.assertEqual(row["temperature_used_K"], "")
            self.assertEqual(row["lambda_0_corrected_nm"], "")

    def test_incompatible_master_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_dir = Path(temporary)
            master = save_dir / MASTER_FILENAME
            master.write_text("other,columns\nold,data\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incompatible columns"):
                append_ruby_record(
                    save_dir / "ruby.npz", "start", "end", {}, {},
                    ruby_name="Ruby A", attempt=1, outcome="passed",
                    acquired={}, r_squared=0.99,
                )
            self.assertEqual(master.read_text(encoding="utf-8"), "other,columns\nold,data\n")


if __name__ == "__main__":
    unittest.main()
