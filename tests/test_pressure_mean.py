"""Propagated uncertainty of a mean of several pressure-marker readings."""
from __future__ import annotations

import math
import unittest

from utils.pressure_mean import PressureReading, error_parts, mean_pressure


class PressureMeanTests(unittest.TestCase):
    def test_no_reading_gives_none(self):
        self.assertIsNone(mean_pressure([]))

    def test_single_reading_keeps_its_parts(self):
        mean = mean_pressure([PressureReading(10.0, 0.014, 0.055, "ruby_shen_2020")])
        self.assertEqual((mean.mean_gpa, mean.count), (10.0, 1))
        self.assertAlmostEqual(mean.err_peak_gpa, 0.014)
        self.assertAlmostEqual(mean.err_scale_gpa, 0.055)
        self.assertAlmostEqual(mean.err_gpa, math.hypot(0.014, 0.055))

    def test_peak_parts_average_down_but_a_shared_scale_does_not(self):
        readings = [PressureReading(10.0 + i, 0.02, 0.05, "ruby_shen_2020") for i in range(4)]
        mean = mean_pressure(readings)
        self.assertAlmostEqual(mean.mean_gpa, 11.5)
        self.assertAlmostEqual(mean.err_peak_gpa, 0.01)     # 0.02 / sqrt(4)
        self.assertAlmostEqual(mean.err_scale_gpa, 0.05)

    def test_different_scales_are_independent(self):
        mean = mean_pressure([PressureReading(5.0, 0.0, 0.03, "a"),
                              PressureReading(5.0, 0.0, 0.03, "a"),
                              PressureReading(5.0, 0.0, 0.06, "b")])
        self.assertAlmostEqual(mean.err_scale_gpa, math.hypot(0.06, 0.06) / 3)

    def test_missing_or_invalid_part_leaves_the_error_unknown(self):
        for bad in (None, float("nan"), -0.01, True):
            with self.subTest(bad=bad):
                mean = mean_pressure([PressureReading(5.0, 0.01, 0.02, "a"),
                                      PressureReading(7.0, bad, 0.02, "a")])
                self.assertAlmostEqual(mean.mean_gpa, 6.0)
                self.assertEqual(mean.count, 2)
                self.assertIsNone(mean.err_gpa)
                self.assertIsNone(mean.err_peak_gpa)
                self.assertIsNone(mean.err_scale_gpa)

    def test_error_parts_require_both_numeric_parts(self):
        self.assertEqual(error_parts({"pressure_err_peak_gpa": 0.01,
                                      "pressure_err_scale_gpa": 0}), (0.01, 0.0))
        for response in ({}, {"pressure_err_peak_gpa": 0.01},
                         {"pressure_err_peak_gpa": 0.01, "pressure_err_scale_gpa": None},
                         {"pressure_err_peak_gpa": "0.01", "pressure_err_scale_gpa": 0.02},
                         {"pressure_err_peak_gpa": False, "pressure_err_scale_gpa": 0.02},
                         {"pressure_err_peak_gpa": float("inf"), "pressure_err_scale_gpa": 0.02},
                         {"pressure_err_peak_gpa": 0.01, "pressure_err_scale_gpa": -0.02}):
            with self.subTest(response=response):
                self.assertEqual(error_parts(response), (None, None))


if __name__ == "__main__":
    unittest.main()
