"""Rad-icon TIFF headers include the sampled instrument state without hardware."""

import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.image_utils import read_tiff_metadata, save_tiff
from apps.Rad_icon_2022.radicon_backend import RadiconBackendSim
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from utils.pdindexer import PdiService


class _Stage:
    def __init__(self):
        self.calls = []
        self.failing_channel = None

    def get_ch_pos(self, channel):
        self.calls.append(channel)
        if channel == self.failing_channel:
            raise TimeoutError("stage query failed")
        return str(channel * 100)  # Both real and simulated PM16C APIs return text.


class _Instrument:
    def __init__(self, readings):
        self.readings = readings

    def get_cached_readings(self):
        return self.readings


class InstrumentMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"]
        )

    def setUp(self):
        root = Path(__file__).parent
        token = uuid.uuid4().hex
        prefs = root / f".radicon_metadata_{token}.json"
        self.image_path = root / f".radicon_metadata_{token}.tif"
        self.addCleanup(lambda: prefs.unlink(missing_ok=True))
        self.addCleanup(lambda: self.image_path.unlink(missing_ok=True))
        self.patchers = [
            mock.patch("apps.Rad_icon_2022.radicon_ui._LOCALDATA", root),
            mock.patch("apps.Rad_icon_2022.radicon_ui._PREFS_FILE", prefs),
            mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE", root / "missing.txt"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = PdiService()
        self.addCleanup(self.service.shutdown)
        self.stage = _Stage()
        sampled_at = time.time()
        self.pace = _Instrument({
            "connected": True, "timestamp": sampled_at, "pressure_mpa": 1.25,
        })
        self.lakeshore = _Instrument({
            "connected": True, "timestamp": sampled_at,
            "temperature_a_k": 12.5, "temperature_b_k": 24.75,
        })
        self.window = RadiconWindow(
            RadiconBackendSim(width=8, height=6), controller=self.stage,
            pdi_service=self.service, pace5000=self.pace, lakeshore=self.lakeshore,
        )
        self.addCleanup(self.window.close)

    def test_saved_tiff_header_contains_stage_pressure_and_temperatures(self):
        metadata = self.window._build_metadata("single")
        self.assertTrue(save_tiff(self.image_path, np.zeros((6, 8), dtype=np.uint16), metadata))
        saved = read_tiff_metadata(self.image_path)

        self.assertEqual(self.stage.calls, [3, 4, 5, 7, 9])
        self.assertEqual([saved[f"stage_ch{ch}_pulses"] for ch in self.stage.calls],
                         [300, 400, 500, 700, 900])
        self.assertEqual(saved["pace5000_pressure_mpa"], 1.25)
        self.assertEqual(saved["lakeshore_t_a_k"], 12.5)
        self.assertEqual(saved["lakeshore_t_b_k"], 24.75)
        self.assertIsInstance(saved["pace5000_pressure_timestamp"], float)
        self.assertIsInstance(saved["lakeshore_temperature_timestamp"], float)

    def test_stale_or_failed_readings_are_null_and_disabled_instruments_omitted(self):
        self.stage.failing_channel = 7
        self.pace.readings["timestamp"] = time.time() - 30
        self.lakeshore.readings["temperature_b_k"] = float("nan")
        with self.assertLogs("apps.Rad_icon_2022.radicon_ui", level="WARNING"):
            metadata = self.window._build_metadata("sequence_frame")
        self.assertIsNone(metadata["stage_ch7_pulses"])
        self.assertIsNone(metadata["pace5000_pressure_mpa"])
        self.assertIsNone(metadata["pace5000_pressure_timestamp"])
        self.assertEqual(metadata["lakeshore_t_a_k"], 12.5)
        self.assertIsNone(metadata["lakeshore_t_b_k"])

        self.window.set_sample_environment_backends(None, None)
        disabled = self.window._build_metadata("dark")
        self.assertNotIn("pace5000_pressure_mpa", disabled)
        self.assertNotIn("lakeshore_t_a_k", disabled)


if __name__ == "__main__":
    unittest.main()
