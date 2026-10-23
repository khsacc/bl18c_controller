from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from apps.exp_scheduler.actions import (
    AlignToReferenceAction, ForLoopAction, SaveSnapshotAction, TakeSpectrumAction, WaitAction,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.ui import monitor_panel, scheduler_window
from apps.exp_scheduler.ui.monitor_panel import MonitorPanel
from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
from apps.interactive_camera.gasket_tracking import GasketEllipse
from apps.interactive_camera.marks import MarksFileCache, draw_saved_marks

_app = QApplication.instance() or QApplication([])


class _FakePace(QObject):
    pressure_updated = pyqtSignal(float)
    setpoint_updated = pyqtSignal(float, float)

    def __init__(self, unit: str = "MPa") -> None:
        super().__init__()
        self.connected = True
        self._active_pressure_unit = unit


class _FakeLakeshore:
    HEATER_RANGES = ("OFF", "LOW", "MEDIUM", "HIGH")

    def __init__(self, data) -> None:
        self.is_connected = True
        self._data = data

    def get_data(self):
        return list(self._data)


def _point(t, a, b, sp=10.0):
    return SimpleNamespace(timestamp=t, temp_a_k=a, temp_b_k=b, eff_setpoint_k=sp,
                           heater_range_idx=1, heater_output_pct=12.5)


class MarksTests(unittest.TestCase):
    def test_file_cache_reads_shapes_and_laser_spot(self):
        with tempfile.TemporaryDirectory() as tmp:
            shapes = Path(tmp) / "shapes.json"
            laser = Path(tmp) / "laser_spot.json"
            shapes.write_text(json.dumps({"shapes": [{"type": "circle", "cx": 5, "cy": 5, "r": 3}]}))
            laser.write_text(json.dumps({"x": 10, "y": 12}))
            got_shapes, got_laser = MarksFileCache(shapes, laser).get()
        self.assertEqual(got_shapes[0]["type"], "circle")
        self.assertEqual(got_laser, (10, 12))

    def test_missing_files_give_no_marks_and_bad_shapes_are_skipped(self):
        cache = MarksFileCache(Path("__missing_shapes__.json"), Path("__missing_laser__.json"))
        self.assertEqual(cache.get(), ([], None))
        frame = np.zeros((40, 40, 3), np.uint8)
        draw_saved_marks(frame, [{"type": "circle"}, {"type": "cross", "cx": 20, "cy": 20, "r": 5}], None)
        self.assertGreater(int(frame.max()), 0)


class MonitorPanelTests(unittest.TestCase):
    def _panel(self, ctx=None, icw=None):
        panel = MonitorPanel(ctx or DeviceContext(), (lambda: icw))
        self.addCleanup(panel.shutdown)
        self.addCleanup(panel.deleteLater)
        return panel

    def test_gasket_preview_runs_only_during_follow_and_discards_af_result(self):
        panel = self._panel()
        panel.show()
        panel._camera_timer.stop()
        frame = np.full((40, 40, 3), 100, np.uint8)
        panel._gasket_reference = frame
        panel._gasket_marked = GasketEllipse(20, 20, 10, 10)
        panel._current_frame = lambda: (frame, ("test", 1), "Test camera", [], None)
        runner = SimpleNamespace(gasket_preview_state=(False, 0))
        panel._runner = runner

        with mock.patch.object(monitor_panel.threading, "Thread") as thread:
            panel._refresh_camera()
            thread.assert_not_called()
            self.assertEqual(panel._camera_source_label.text(), "Source: Test camera")

            runner.gasket_preview_state = (True, 1)
            panel._refresh_camera()
            thread.assert_called_once()
            active_generation = panel._gasket_preview_token()

            runner.gasket_preview_state = (False, 2)  # autofocus begins
            panel._refresh_camera()
            thread.assert_called_once()
            panel._on_gasket_result((
                active_generation, frame, SimpleNamespace(ellipse=panel._gasket_marked),
                None, [], None, "Test camera",
            ))
            self.assertEqual(panel._camera_source_label.text(), "Source: Test camera")
            with mock.patch.object(monitor_panel, "detect_gasket_hole") as detector:
                panel._scan_gasket(
                    frame, frame, panel._gasket_marked, active_generation,
                    [], None, "Test camera",
                )
                detector.assert_not_called()

            runner.gasket_preview_state = (True, 3)  # autofocus ends
            panel._refresh_camera()
            self.assertEqual(thread.call_count, 2)
            new_generation = panel._gasket_preview_token()
            runner.gasket_preview_state = (False, 4)
            runner.gasket_preview_state = (True, 5)  # AF faster than a refresh
            panel._on_gasket_result((
                new_generation, frame, SimpleNamespace(ellipse=panel._gasket_marked),
                None, [], None, "Test camera",
            ))
            self.assertEqual(panel._camera_source_label.text(), "Source: Test camera")

    def test_device_groups_follow_connection_state(self):
        pace = _FakePace()
        lakeshore = _FakeLakeshore([_point(1000.0, 10.0, 11.0)])
        panel = self._panel(DeviceContext(pace5000=pace, lakeshore=lakeshore))
        self.assertFalse(panel._pace_group.isHidden())
        self.assertFalse(panel._lakeshore_group.isHidden())
        self.assertIn("10.000", panel._ls_ab_label.text())

        pace.connected = False
        lakeshore.is_connected = False
        panel._refresh_devices()
        self.assertTrue(panel._pace_group.isHidden())
        self.assertTrue(panel._lakeshore_group.isHidden())

    def test_hidden_when_not_configured(self):
        panel = self._panel()
        self.assertTrue(panel._pace_group.isHidden())
        self.assertTrue(panel._lakeshore_group.isHidden())

    def test_pace_readings_are_converted_to_mpa(self):
        pace = _FakePace(unit="Bar")
        panel = self._panel(DeviceContext(pace5000=pace))
        pace.pressure_updated.emit(20.0)
        pace.setpoint_updated.emit(30.0, 0.01)
        self.assertEqual(list(panel._pace_pressures), [2.0])
        self.assertIn("2.0000  MPa", panel._pace_label.text())
        self.assertIn("3.0000 MPa", panel._pace_setpoint_label.text())

    def test_lakeshore_clear_is_local(self):
        lakeshore = _FakeLakeshore([_point(1.0, 10.0, 11.0)])
        panel = self._panel(DeviceContext(lakeshore=lakeshore))
        panel._clear_lakeshore()
        panel._refresh_lakeshore()
        self.assertEqual(len(panel._ls_line_a.getData()[0] or []), 0)
        self.assertEqual(len(lakeshore.get_data()), 1)

    def test_idle_mirrors_interactive_camera_frame(self):
        icw = SimpleNamespace(current_frame=np.full((30, 40, 3), 50, np.uint8),
                              shapes=[], laser_spot_pos=None, show_all_marks=True)
        panel = self._panel(icw=icw)
        frame, _key, source, *_ = panel._current_frame()
        self.assertIs(frame, icw.current_frame)
        self.assertEqual(source, "Interactive Camera")

    def test_idle_without_source_never_opens_camera(self):
        with mock.patch.object(monitor_panel, "_CameraCapture") as capture:
            panel = self._panel()
            frame, *_ = panel._current_frame()
        self.assertIsNone(frame)
        capture.assert_not_called()

    def test_run_with_camera_uses_runner_frames(self):
        frame = np.ones((10, 10, 3), np.uint8)
        class _Runner(QObject):
            started = pyqtSignal()
            ops_logged = pyqtSignal(str)

            def latest_camera_frame(self):
                return frame, 1.0

        runner = _Runner()
        with mock.patch.object(monitor_panel, "_CameraCapture") as capture:
            panel = self._panel()
            panel.run_started(runner, uses_camera=True)
            got, _key, source, *_ = panel._current_frame()
        self.assertIs(got, frame)
        self.assertEqual(source, "Sequence camera session")
        capture.assert_not_called()

    def test_run_without_camera_opens_own_capture_on_start_and_releases_at_finish(self):
        class _Runner(QObject):
            started = pyqtSignal()
            ops_logged = pyqtSignal(str)

        runner = _Runner()
        with mock.patch.object(monitor_panel, "_CameraCapture") as capture:
            panel = self._panel()
            panel.run_started(runner, uses_camera=False)
            capture.assert_not_called()
            runner.started.emit()
            capture.assert_called_once_with(monitor_panel._CAMERA_INDEX)
            panel.run_finished()
            capture.return_value.stop.assert_called_once()

    def test_run_log_shows_ops_lines_and_clears_on_next_run(self):
        class _Runner(QObject):
            started = pyqtSignal()
            ops_logged = pyqtSignal(str)

        first, second = _Runner(), _Runner()
        with mock.patch.object(monitor_panel, "_CameraCapture"):
            panel = self._panel()
            panel.run_started(first, uses_camera=True)
            first.ops_logged.emit("2026-01-01 00:00:00.000 [SEQ:START] run_1")
            first.ops_logged.emit("2026-01-01 00:00:01.000 [SEQ:DONE] ok")
            panel.run_finished()
            self.assertEqual(panel._log_view.toPlainText().splitlines(),
                             ["2026-01-01 00:00:00.000 [SEQ:START] run_1",
                              "2026-01-01 00:00:01.000 [SEQ:DONE] ok"])
            panel.run_started(second, uses_camera=True)
            self.assertEqual(panel._log_view.toPlainText(), "")
            second.ops_logged.emit("line B")
            self.assertEqual(panel._log_view.toPlainText(), "line B")

    def test_run_without_camera_respects_disabled_live_image(self):
        class _Runner(QObject):
            started = pyqtSignal()
            ops_logged = pyqtSignal(str)

        runner = _Runner()
        with mock.patch.object(monitor_panel, "_CameraCapture") as capture:
            panel = self._panel()
            panel._camera_enable_chk.setChecked(False)
            panel.run_started(runner, uses_camera=False)
            runner.started.emit()
        capture.assert_not_called()

    def test_xrd_row_shows_latest_image_and_profile_and_keeps_user_levels(self):
        class _Runner(QObject):
            started = pyqtSignal()
            ops_logged = pyqtSignal(str)
            xrd_image_ready = pyqtSignal(object, str)
            xrd_profile_ready = pyqtSignal(object, object, str)

        panel = self._panel()
        self.assertTrue(panel._xrd_image_group.isHidden())
        self.assertTrue(panel._xrd_profile_group.isHidden())

        runner = _Runner()
        panel.run_started(runner, uses_camera=True, uses_xrd=True, xrd_1d=True)
        self.assertFalse(panel._xrd_image_group.isHidden())
        self.assertFalse(panel._xrd_profile_group.isHidden())

        image = np.arange(100, 500, dtype=np.uint16).reshape(20, 20)
        runner.xrd_image_ready.emit(image, "a_2x2.tif")
        # The first frame is auto-levelled.
        self.assertEqual((panel._xrd_min_spin.value(), panel._xrd_max_spin.value()), (100, 499))
        self.assertIn("a_2x2.tif", panel._xrd_info_label.text())
        self.assertIsNotNone(panel._xrd_img_label.pixmap())

        panel._xrd_max_spin.setValue(300)
        runner.xrd_image_ready.emit(image * 2, "b_2x2.tif")
        self.assertEqual(panel._xrd_max_slider.value(), 300)
        panel._xrd_auto_levels()
        self.assertEqual(panel._xrd_max_slider.value(), 998)

        runner.xrd_profile_ready.emit(np.array([1.0, 2.0, 3.0]), np.array([5.0, 9.0, 4.0]),
                                      "b_2x2.tif")
        x, y = panel._xrd_curve.getData()
        self.assertEqual(list(y), [5.0, 9.0, 4.0])
        self.assertEqual(panel._xrd_profile_label.text(), "b_2x2.tif")
        panel.run_finished()
        self.assertFalse(panel._xrd_image_group.isHidden())  # kept after the run

        panel.run_started(_Runner(), uses_camera=True, uses_xrd=True, xrd_1d=False)
        self.assertFalse(panel._xrd_image_group.isHidden())
        self.assertTrue(panel._xrd_profile_group.isHidden())
        panel.run_started(_Runner(), uses_camera=True)
        self.assertTrue(panel._xrd_image_group.isHidden())


class _FakeDragEvent:
    def __init__(self, button, start, end, finish):
        self._button, self._start, self._end, self._finish = button, start, end, finish
        self.accepted = False

    def button(self):
        return self._button

    def buttonDownPos(self, _button=None):
        return self._start

    def pos(self):
        return self._end

    def isFinish(self):
        return self._finish

    def accept(self):
        self.accepted = True


class PlotZoomTests(unittest.TestCase):
    def _pace_panel(self):
        pace = _FakePace()
        panel = MonitorPanel(DeviceContext(pace5000=pace), lambda: None)
        self.addCleanup(panel.shutdown)
        self.addCleanup(panel.deleteLater)
        panel.resize(500, 900)
        panel.show()
        _app.processEvents()
        for i, p in enumerate((1.0, 2.0, 3.0)):
            panel._pace_times.append(1000.0 + i)
            panel._pace_pressures.append(p)
        panel._redraw_pace()
        return panel, pace

    def test_wheel_is_ignored_on_both_plots(self):
        panel = MonitorPanel(DeviceContext(), lambda: None)
        self.addCleanup(panel.shutdown)
        self.addCleanup(panel.deleteLater)
        for plot in (panel._pace_plot, panel._ls_plot):
            vb = plot.plotItem.vb
            before = vb.viewRange()
            event = mock.Mock()
            vb.wheelEvent(event)
            event.ignore.assert_called_once()
            self.assertEqual(vb.viewRange(), before)

    def test_right_drag_zooms_to_rectangle_and_holds_against_new_readings(self):
        from PyQt6.QtCore import QPointF, Qt
        panel, pace = self._pace_panel()
        vb = panel._pace_plot.plotItem.vb
        rect = vb.boundingRect()
        start = QPointF(rect.left() + rect.width() * 0.25, rect.top() + rect.height() * 0.25)
        end = QPointF(rect.left() + rect.width() * 0.5, rect.top() + rect.height() * 0.5)
        x_before = vb.viewRange()[0]
        vb.mouseDragEvent(_FakeDragEvent(Qt.MouseButton.RightButton, start, start + QPointF(1, 1), False))
        event = _FakeDragEvent(Qt.MouseButton.RightButton, start, end, True)
        vb.mouseDragEvent(event)
        self.assertTrue(event.accepted)
        self.assertTrue(panel._pace_user_zoomed)
        zoomed = vb.viewRange()
        self.assertLess(zoomed[0][1] - zoomed[0][0], x_before[1] - x_before[0])

        pace.pressure_updated.emit(4.0)
        self.assertEqual(vb.viewRange(), zoomed)

        vb.autoRange()  # context menu "View All"
        self.assertFalse(panel._pace_user_zoomed)

    def test_clear_graph_resets_zoom(self):
        panel, _pace = self._pace_panel()
        panel._set_pace_zoomed(True)
        panel._clear_pace()
        self.assertFalse(panel._pace_user_zoomed)


class _FakeShutter:
    def __init__(self, inserted=True, error=None):
        self.inserted = inserted
        self.error = error
        self.listeners = set()
        self.calls = []

    def add_status_listener(self, listener):
        self.listeners.add(listener)

    def remove_status_listener(self, listener):
        self.listeners.discard(listener)

    def publish(self, inserted):
        for listener in tuple(self.listeners):
            listener(inserted)

    def get_status(self):
        self.calls.append("get_status")
        if self.error:
            raise RuntimeError(self.error)
        self.publish(self.inserted)
        return self.inserted

    def close(self):
        self.calls.append("close")


class ShutterRowTests(unittest.TestCase):
    def _panel(self, shutter):
        panel = MonitorPanel(DeviceContext(shutter=shutter), lambda: None)
        self.addCleanup(panel.shutdown)
        self.addCleanup(panel.deleteLater)
        return panel

    @staticmethod
    def _runner():
        class _Runner(QObject):
            started = pyqtSignal()
            ops_logged = pyqtSignal(str)
        return _Runner()

    def _wait_for_query(self, panel):
        if panel._shutter_query_thread is not None:
            panel._shutter_query_thread.join(timeout=2)
        _app.processEvents()

    def test_idle_shows_placeholder_not_a_position(self):
        panel = self._panel(_FakeShutter())
        text = panel._shutter_label.text()
        self.assertIn("not monitored", text)
        self.assertNotIn("IN", text)
        self.assertNotIn("OUT", text)

    def test_unmanaged_run_queries_once_follows_changes_and_closes_at_finish(self):
        shutter = _FakeShutter(inserted=True)
        panel = self._panel(shutter)
        with mock.patch.object(monitor_panel, "_CameraCapture"):
            panel.run_started(self._runner(), uses_camera=True, shutter_managed=False)
        self._wait_for_query(panel)
        self.assertEqual(panel._shutter_label.text(), "IN — X-rays blocked")

        shutter.publish(False)
        _app.processEvents()
        self.assertEqual(panel._shutter_label.text(), "OUT — X-rays at sample")

        panel.run_finished()
        self.assertEqual(shutter.calls, ["get_status", "close"])
        self.assertFalse(shutter.listeners)
        self.assertIn("not monitored", panel._shutter_label.text())

    def test_managed_run_only_listens(self):
        shutter = _FakeShutter()
        panel = self._panel(shutter)
        with mock.patch.object(monitor_panel, "_CameraCapture"):
            panel.run_started(self._runner(), uses_camera=True, shutter_managed=True)
        self.assertIn("Checking", panel._shutter_label.text())
        shutter.publish(True)
        _app.processEvents()
        self.assertEqual(panel._shutter_label.text(), "IN — X-rays blocked")
        panel.run_finished()
        self.assertEqual(shutter.calls, [])

    def test_query_failure_shows_unknown(self):
        panel = self._panel(_FakeShutter(error="STARS down"))
        with mock.patch.object(monitor_panel, "_CameraCapture"):
            panel.run_started(self._runner(), uses_camera=True)
        self._wait_for_query(panel)
        self.assertIn("Unknown", panel._shutter_label.text())
        self.assertIn("STARS down", panel._shutter_label.text())

    def test_late_update_after_finish_is_ignored(self):
        shutter = _FakeShutter()
        panel = self._panel(shutter)
        with mock.patch.object(monitor_panel, "_CameraCapture"):
            panel.run_started(self._runner(), uses_camera=True, shutter_managed=True)
        listener = next(iter(shutter.listeners))
        panel.run_finished()
        listener(False)  # emitted by the reader thread just before removal
        _app.processEvents()
        self.assertIn("not monitored", panel._shutter_label.text())


class SchedulerWindowMonitorTests(unittest.TestCase):
    def test_window_hosts_monitor_and_detects_camera_use(self):
        missing_settings = Path("__missing_exp_scheduler_test_settings__.json")
        with mock.patch.object(scheduler_window, "_SETTINGS_PATH", missing_settings):
            window = ExperimentalSchedulerWindow(DeviceContext())
        self.addCleanup(window.deleteLater)
        self.assertIsInstance(window._monitor_panel, MonitorPanel)
        self.assertFalse(window._uses_camera([WaitAction(duration_s=1.0)]))
        self.assertTrue(window._uses_camera([ForLoopAction(var="i", values=[1], body=[SaveSnapshotAction()])]))
        self.assertTrue(window._uses_camera([TakeSpectrumAction()]))
        self.assertTrue(window._uses_camera([AlignToReferenceAction()]))


if __name__ == "__main__":
    unittest.main()
