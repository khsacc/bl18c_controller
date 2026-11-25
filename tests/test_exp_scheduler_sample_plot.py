"""Scheduler sample-pressure plot and multi-ruby aggregation, without hardware."""
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from apps.exp_scheduler.actions import (
    CompressByRelativePressureUsingRubyAction, CompressToTargetPressureUsingRubyAction,
    DecompressByRelativePressureUsingRubyAction, DecompressToTargetPressureUsingRubyAction,
    ForLoopAction, TakeSpectrumAction,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.ui.monitor_panel import MonitorPanel


_app = QApplication.instance() or QApplication([])


class _LakeShore:
    is_connected = True

    def get_data(self):
        return []

    def get_cached_readings(self):
        return {"connected": self.is_connected, "timestamp": time.time(),
                "temperature_b_k": 80.0}


class SamplePlotTests(unittest.TestCase):
    def test_visibility_axes_and_zoom_menu(self):
        lakeshore = _LakeShore()
        panel = MonitorPanel(DeviceContext(lakeshore=lakeshore))
        self.addCleanup(panel.close)
        self.addCleanup(panel.shutdown)

        self.assertTrue(panel._sample_group.isHidden())
        panel.set_ruby_pressure_enabled(True)
        self.assertFalse(panel._sample_group.isHidden())
        self.assertEqual(panel._sample_plot_combo.count(), 3)
        self.assertTrue(any(action.text() == "Zoom Out"
                            for action in panel._sample_plot.plotItem.vb.getMenu(None).actions()))
        panel._on_sample_pressure(1_720_000_000.0, 1.5, 80.0)
        clock_label = panel._sample_plot.getAxis("bottom").tickStrings(
            [1_720_000_000.0], 1, 1,
        )[0]
        self.assertRegex(clock_label, r"^\d{2}:\d{2}:\d{2}$")
        wheel = mock.Mock()
        panel._sample_plot.plotItem.vb.wheelEvent(wheel)
        wheel.ignore.assert_called_once()
        panel._sample_plot_combo.setCurrentIndex(panel._sample_plot_combo.findData("temperature"))
        x, y = panel._sample_curve.getData()
        self.assertEqual((list(x), list(y)), ([80.0], [1.5]))
        panel._zoom_out_sample_plot()
        self.assertTrue(panel._sample_user_zoomed)

        lakeshore.is_connected = False
        panel._refresh_devices()
        self.assertEqual(panel._sample_plot_combo.count(), 2)
        panel.set_ruby_pressure_enabled(False)
        self.assertTrue(panel._sample_group.isHidden())

    def test_gas_plot_keeps_measurement_coordinates_and_skips_missing_values(self):
        panel = MonitorPanel(DeviceContext())
        self.addCleanup(panel.close)
        self.addCleanup(panel.shutdown)
        panel.set_ruby_pressure_enabled(True)
        panel._on_sample_pressure(100.0, 1.5, None, 0.2)
        panel._on_sample_pressure(101.0, 2.0, None, None)
        panel._on_sample_pressure(102.0, 2.5, None, float("nan"))
        panel._on_sample_pressure(103.0, 3.0, None, 0.1)
        panel._sample_plot_combo.setCurrentIndex(panel._sample_plot_combo.findData("gas"))
        x, y = panel._sample_curve.getData()
        self.assertEqual((list(x), list(y)), ([0.2, 0.1], [1.5, 3.0]))
        self.assertEqual(panel._sample_plot.getAxis("bottom").labelText, "Gas P (MPa)")
        panel._sample_plot_combo.setCurrentIndex(panel._sample_plot_combo.findData("time"))
        self.assertEqual(len(panel._sample_curve.getData()[0]), 4)

    def test_cached_gas_pressure_is_optional_and_in_mpa(self):
        pace = mock.Mock()
        runner = SequenceRunner(Sequence(), DeviceContext(pace5000=pace))
        pace.get_cached_readings.return_value = {"connected": True, "pressure_mpa": 0.25}
        self.assertEqual(runner._cached_sample_gas_pressure(), 0.25)
        for value in (None, float("nan"), float("inf")):
            pace.get_cached_readings.return_value["pressure_mpa"] = value
            self.assertIsNone(runner._cached_sample_gas_pressure())
        pace.get_cached_readings.side_effect = RuntimeError("unavailable")
        self.assertIsNone(runner._cached_sample_gas_pressure())

    def test_all_positions_emits_one_average_of_measured_rubies(self):
        positions = {name: {"ch3": 0, "ch4": index, "ch5": 0}
                     for index, name in enumerate(("A", "B", "C"), start=1)}
        settings = GlobalSpectrumSettings(ruby_positions=positions, pressure_enabled=True)
        runner = SequenceRunner(
            Sequence(), DeviceContext(controller=object(), lakeshore=_LakeShore()),
            global_spectrum=settings,
        )
        received = []
        details = []
        remarks = []
        runner.step_remark.connect(lambda *remark: remarks.append(remark))
        runner.sample_pressure_ready.connect(lambda *sample: received.append(sample))
        runner.ruby_sample_ready.connect(details.append)

        measure_all = runner._do_take_spectrum_inner

        def measure_one(action, _step_index, **kwargs):
            if action.all_positions:
                return measure_all(action, _step_index, **kwargs)
            if action.position_name == "B":
                return  # no valid pressure from this ruby
            pressure = 1.0 if action.position_name == "A" else 3.0
            runner._report_sample_pressure(SimpleNamespace(
                ruby_position_name=action.position_name, pressure_gpa=pressure,
                peak_nm=694.3 + pressure, temperature_correction_enabled=False,
                measured_temperature_k=None, temperature_source="",
                measured_temperature_timestamp="",
            ), time.time(), 80.0, _step_index, pressure / 10)

        with (mock.patch.object(runner, "_check_pressure_settings"),
              mock.patch.object(runner, "_do_take_spectrum_inner", side_effect=measure_one)):
            SequenceRunner._do_take_spectrum(
                runner, TakeSpectrumAction(all_positions=True), 0,
            )
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1:], (2.0, 80.0, 0.2))
        self.assertEqual([item["name"] for item in details], ["A", "C"])
        self.assertEqual([item["pressure_gpa"] for item in details], [1.0, 3.0])
        self.assertIsNone(runner._ruby_pressure_batch)
        self.assertEqual(remarks[-1], (0, "Mean ruby pressure: 2.000 GPa"))

    def test_guided_pressure_remarks_use_last_reading_for_each_loop_iteration(self):
        for action_type in (
            CompressToTargetPressureUsingRubyAction, DecompressToTargetPressureUsingRubyAction,
            CompressByRelativePressureUsingRubyAction, DecompressByRelativePressureUsingRubyAction,
        ):
            with self.subTest(action=action_type.__name__):
                runner = SequenceRunner(Sequence(actions=[ForLoopAction(
                    var="i", values=[1, 2], body=[action_type(), TakeSpectrumAction()],
                )]), DeviceContext())
                remarks = []
                samples = []
                runner.step_remark.connect(lambda *remark: remarks.append(remark))
                runner.sample_pressure_ready.connect(lambda *sample: samples.append(sample))
                for step_index, pressures in enumerate(((3.0, 1.0), (2.0,), (0.5, 2.5), (4.0,))):
                    for pressure in pressures:
                        runner._report_sample_pressure(SimpleNamespace(
                            ruby_position_name="A", pressure_gpa=pressure,
                            peak_nm=695.0, temperature_correction_enabled=False,
                            measured_temperature_k=None, temperature_source="",
                            measured_temperature_timestamp="",
                        ), 100.0, None, step_index)
                self.assertEqual(remarks, [
                    (0, "Last ruby pressure: 3.000 GPa"),
                    (0, "Last ruby pressure: 1.000 GPa"),
                    (1, "Mean ruby pressure: 2.000 GPa"),
                    (2, "Last ruby pressure: 0.500 GPa"),
                    (2, "Last ruby pressure: 2.500 GPa"),
                    (3, "Mean ruby pressure: 4.000 GPa"),
                ])
                self.assertEqual([sample[1] for sample in samples], [3.0, 1.0, 2.0, 0.5, 2.5, 4.0])


if __name__ == "__main__":
    unittest.main()
