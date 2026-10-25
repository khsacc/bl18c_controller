"""Scheduler sample-pressure plot and multi-ruby aggregation, without hardware."""
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from apps.exp_scheduler.actions import TakeSpectrumAction
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
        self.assertEqual(panel._sample_plot_combo.count(), 2)
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
        panel._sample_plot_combo.setCurrentIndex(1)
        x, y = panel._sample_curve.getData()
        self.assertEqual((list(x), list(y)), ([80.0], [1.5]))
        panel._zoom_out_sample_plot()
        self.assertTrue(panel._sample_user_zoomed)

        lakeshore.is_connected = False
        panel._refresh_devices()
        self.assertEqual(panel._sample_plot_combo.count(), 1)
        panel.set_ruby_pressure_enabled(False)
        self.assertTrue(panel._sample_group.isHidden())

    def test_all_positions_emits_one_average_of_measured_rubies(self):
        positions = {name: {"ch3": 0, "ch4": index, "ch5": 0}
                     for index, name in enumerate(("A", "B", "C"), start=1)}
        settings = GlobalSpectrumSettings(ruby_positions=positions, pressure_enabled=True)
        runner = SequenceRunner(
            Sequence(), DeviceContext(controller=object(), lakeshore=_LakeShore()),
            global_spectrum=settings,
        )
        received = []
        runner.sample_pressure_ready.connect(lambda *sample: received.append(sample))

        def measure_one(action, _step_index):
            if action.position_name == "B":
                return  # no valid pressure from this ruby
            pressure = 1.0 if action.position_name == "A" else 3.0
            runner._ruby_pressure_batch.append((time.time(), pressure, 80.0))

        with (mock.patch.object(runner, "_check_pressure_settings"),
              mock.patch.object(runner, "_do_take_spectrum", side_effect=measure_one)):
            SequenceRunner._do_take_spectrum(
                runner, TakeSpectrumAction(all_positions=True), 0,
            )
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1:], (2.0, 80.0))
        self.assertIsNone(runner._ruby_pressure_batch)


if __name__ == "__main__":
    unittest.main()
