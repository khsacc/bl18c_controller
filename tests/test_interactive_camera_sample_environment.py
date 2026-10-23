import csv
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PyQt6 import QtCore, QtWidgets

from apps.interactive_camera import interactive_camera as camera_module
from apps.interactive_camera.interactive_camera import MainWindow


@pytest.fixture(autouse=True)
def _keep_camera_localdata_untouched(monkeypatch):
    monkeypatch.setattr(MainWindow, "_save_shapes", lambda self: None)
    monkeypatch.setattr(MainWindow, "_save_localdata_cache", lambda self: None)


class _FakeCapture:
    def isOpened(self):
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return 640
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return 480
        if prop == cv2.CAP_PROP_FPS:
            return 30.0
        return 0

    def read(self):
        return True, np.zeros((480, 640, 3), dtype=np.uint8)

    def release(self):
        pass


class _FakePace5000(QtCore.QObject):
    pressure_updated = QtCore.pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self._active_pressure_unit = "MPa"


class _FakeLakeShore(QtCore.QObject):
    data_updated = QtCore.pyqtSignal()

    def get_data(self):
        return [SimpleNamespace(
            temp_a_k=12.3456,
            temp_b_k=23.4567,
            eff_setpoint_k=20.0,
        )]


def test_sample_environment_controls_and_information_band(monkeypatch):
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["test", "-platform", "offscreen"])
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: _FakeCapture())

    pace = _FakePace5000()
    lakeshore = _FakeLakeShore()
    window = MainWindow(
        controller=object(), pace5000=pace, lakeshore=lakeshore)
    window.timer.stop()

    assert not window.sample_env_group.isHidden()
    assert not window.pace_env_row.isHidden()
    assert not window.lakeshore_env_row.isHidden()

    window.chk_pace_pressure.setChecked(True)
    window.chk_lakeshore_ch_a.setChecked(True)
    window.chk_lakeshore_ch_b.setChecked(True)
    window.chk_lakeshore_setpoint.setChecked(True)
    pace.pressure_updated.emit(1.23456)

    assert window._sample_environment_lines() == [
        "PACE5000 Gas Pressure: 1.235 MPa",
        "LakeShore ChA: 12.346 K",
        "LakeShore ChB: 23.457 K",
        "LakeShore Setpoint: 20.000 K",
    ]
    keys = window._selected_sample_environment_keys()
    assert window._sample_environment_log_headers(keys) == [
        "pace5000_gas_pressure_mpa",
        "lakeshore_ch_a_k",
        "lakeshore_ch_b_k",
        "lakeshore_setpoint_k",
    ]
    assert window._sample_environment_log_values(keys) == [
        "1.234560", "12.345600", "23.456700", "20.000000",
    ]

    pace._active_pressure_unit = "Bar"
    pace.pressure_updated.emit(12.3456)
    assert window._sample_environment_log_values(keys)[0] == "1.234560"

    raw = np.full((480, 640, 3), 37, dtype=np.uint8)
    displayed = window._add_information_band(raw)
    assert displayed.shape == (480 + window._information_band_height(5), 640, 3)
    assert np.array_equal(displayed[:480], raw)
    assert np.any(displayed[480:] != 0)
    assert np.all(displayed[480:, 0] == 0)

    window.video_label.resize(640, 600)
    scale, dx, dy = window._render_to_label(displayed, window.video_label)
    window.render_params = {'scale': scale, 'dx': dx, 'dy': dy}
    fx, fy = window.label_to_frame_coords(
        QtCore.QPoint(dx + round(10 * scale), dy + round(10 * scale)))
    assert abs(fx - 10) <= 1 and abs(fy - 10) <= 1
    assert window.label_to_frame_coords(
        QtCore.QPoint(dx + 10, dy + round(490 * scale))) == (-1, -1)

    class _FailingReader:
        def __init__(self, **kwargs):
            raise RuntimeError("FRP unavailable")

    messages = []
    window._tracking_log_signal.connect(messages.append)
    monkeypatch.setattr(camera_module, "FluoraPresseeReader", _FailingReader)
    window._collect_ruby_spectrum("unused", 1, "20260910-120000")
    assert messages

    window.set_sample_environment_backends(pace, None)
    assert not window.sample_env_group.isHidden()
    assert not window.pace_env_row.isHidden()
    assert window.lakeshore_env_row.isHidden()

    window.set_sample_environment_backends(None, lakeshore)
    assert not window.sample_env_group.isHidden()
    assert window.pace_env_row.isHidden()
    assert not window.lakeshore_env_row.isHidden()

    window.set_sample_environment_backends(None, None)
    assert window.sample_env_group.isHidden()
    window.close()


def test_snapshot_saves_information_below_camera_image(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["test", "-platform", "offscreen"])
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: _FakeCapture())
    window = MainWindow(controller=object())
    window.timer.stop()
    window.show_all_marks = False
    window.shapes = []
    window.laser_spot_pos = None
    # Do not draw a beam registered on this machine.
    window._xray_beam_cache = camera_module.XrayBeamFileCache(\"__no_xray_beam__.json\")
    window.current_frame = np.full((480, 640, 3), 37, dtype=np.uint8)
    path = tmp_path / "snapshot.png"
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getSaveFileName", lambda *args: (str(path), "PNG Image"))

    window.take_snapshot()
    saved = cv2.imread(str(path))
    assert saved.shape == (480 + window._information_band_height(1), 640, 3)
    assert np.all(saved[:480] == 37)
    assert np.any(saved[480:] != 0)

    window.show_timestamp = False
    window.take_snapshot()
    saved = cv2.imread(str(path))
    assert saved.shape == (480 + window._information_band_height(1), 640, 3)
    assert np.any(saved[480:] != 0)
    window.close()


def test_snapshot_follows_launcher_instrument_connections(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["test", "-platform", "offscreen"])
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: _FakeCapture())
    window = MainWindow(controller=object())
    window.timer.stop()
    window.show_all_marks = False
    window.shapes = []
    window.laser_spot_pos = None
    # Do not draw a beam registered on this machine.
    window._xray_beam_cache = camera_module.XrayBeamFileCache(\"__no_xray_beam__.json\")
    window.current_frame = np.full((480, 640, 3), 37, dtype=np.uint8)
    path = tmp_path / "snapshot.png"
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getSaveFileName", lambda *args: (str(path), "PNG Image"))

    pace = _FakePace5000()
    lakeshore = _FakeLakeShore()
    for connected_pace, connected_lakeshore, expected_lines in (
        (None, None, 1),
        (pace, None, 2),
        (None, lakeshore, 3),
        (pace, lakeshore, 4),
        (None, None, 1),
    ):
        window.set_sample_environment_backends(connected_pace, connected_lakeshore)
        assert len(window._sample_environment_lines(
            window._main_sample_environment_keys())) == expected_lines - 1
        window.take_snapshot()
        saved = cv2.imread(str(path))
        assert saved.shape == (480 + window._information_band_height(expected_lines), 640, 3)
        assert np.all(saved[:480] == 37)

    window.set_sample_environment_backends(pace, lakeshore)
    pace.pressure_updated.emit(1.23456)
    assert window._sample_environment_lines(window._main_sample_environment_keys()) == [
        "PACE5000 Gas Pressure: 1.235 MPa",
        "LakeShore ChA: 12.346 K",
        "LakeShore ChB: 23.457 K",
    ]
    window.show_timestamp = False
    window.take_snapshot()
    assert cv2.imread(str(path)).shape == (
        480 + window._information_band_height(4), 640, 3)
    window.close()


def test_save_marks_option_controls_snapshot_and_video_pixels(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["test", "-platform", "offscreen"])
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: _FakeCapture())
    window = MainWindow(controller=object())
    window.timer.stop()
    window.current_frame = np.full((480, 640, 3), 37, dtype=np.uint8)
    window.shapes = [{'type': 'circle', 'cx': 100, 'cy': 100, 'r': 20}]
    window.laser_spot_pos = (200, 200)
    # Do not draw a beam registered on this machine.
    window._xray_beam_cache = camera_module.XrayBeamFileCache(\"__no_xray_beam__.json\")
    window.show_all_marks = False
    window.show_timestamp = False
    path = tmp_path / "snapshot.png"
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getSaveFileName", lambda *args: (str(path), "PNG Image"))

    assert not window.chk_save_marks.isChecked()
    window.take_snapshot()
    unmarked = cv2.imread(str(path))
    assert np.all(unmarked[:480] == 37)
    assert np.any(unmarked[480:] != 0)

    window.chk_save_marks.setChecked(True)
    window.take_snapshot()
    marked = cv2.imread(str(path))
    assert not np.all(marked[100, 120] == 37)
    assert not np.all(marked[200, 208] == 37)
    assert np.any(marked[480:] != 0)

    class _Writer:
        def __init__(self):
            self.frames = []

        def write(self, frame):
            self.frames.append(frame.copy())

    writer = _Writer()
    window.video_writer = writer
    window.is_recording = True
    window._recording_band_height = window._information_band_height(4)
    window.chk_save_marks.setChecked(False)
    window.update_frame()
    assert np.all(writer.frames[-1][:480] == 0)
    assert np.any(writer.frames[-1][480:] != 0)

    window.chk_save_marks.setChecked(True)
    window.update_frame()
    assert not np.all(writer.frames[-1][100, 120] == 0)
    assert not np.all(writer.frames[-1][200, 208] == 0)
    assert np.any(writer.frames[-1][480:] != 0)
    assert np.all(window.current_frame == 0)

    window.is_recording = False
    window.video_writer = None
    window.close()


def test_calibrated_scale_bar_uses_both_motor_axis_scales(monkeypatch):
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["test", "-platform", "offscreen"])
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: _FakeCapture())
    window = MainWindow(controller=object())
    window.timer.stop()
    window.show_all_marks = False
    window.laser_spot_pos = None
    # Do not draw a beam registered on this machine.
    window._xray_beam_cache = camera_module.XrayBeamFileCache(\"__no_xray_beam__.json\")

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    window.calibration_data = None
    window.draw_marks(frame)
    assert not np.any(frame)

    window.calibration_data = {'matrix_inv': [[1, 0], [0, 1]]}
    window.draw_marks(frame)
    # Ch4 is 2 µm/pulse, so 100 µm spans 50 horizontal pixels.
    assert np.all(frame[460, 20:71] == 255)
    assert np.all(frame[460, 72] == 0)
    assert np.any(frame[430:451, 20:100] != 0)  # Unicode label above the bar

    frame.fill(0)
    window.calibration_data = {'matrix_inv': [[0, 1], [10, 0]]}
    window.draw_marks(frame)
    # Ch5 is 0.11 µm/pulse, so 10 pulses/pixel gives 91 pixels.
    assert np.all(frame[460, 20:112] == 255)
    assert np.all(frame[460, 113] == 0)

    frame.fill(0)
    window.calibration_data = {'matrix_inv': [[0, 0], [0, 0]]}
    window.draw_marks(frame)
    assert not np.any(frame)
    window.close()


def test_ruby_spectrum_csv_is_saved_when_fit_fails(tmp_path):
    acquired = {
        "x": [690.0, 691.0],
        "y_raw": [10.0, 20.0],
        "y": [9.0, 19.0],
        "fit": {"success": False, "fit": None},
    }
    path = tmp_path / "ruby_spectrum.csv"

    fit_success, peaks = MainWindow._save_ruby_spectrum_csv(
        acquired, str(path))

    assert fit_success is False
    assert peaks == (None, None)
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [float(row["x"]) for row in rows] == [690.0, 691.0]
    assert [float(row["y"]) for row in rows] == [9.0, 19.0]
    assert all(row["fit_success"] == "false" for row in rows)
    assert all(row["peak1_top"] == "" for row in rows)
    assert all(row["peak2_top"] == "" for row in rows)


def test_ruby_spectrum_csv_stores_two_peak_tops(tmp_path):
    acquired = {
        "x": [690.0, 691.0],
        "y_raw": [10.0, 20.0],
        "y": [9.0, 19.0],
        "x_axis": {"unit": "nm"},
        "fit": {
            "success": True,
            "fit": {"Peak1": 694.321, "Peak2": 692.804},
        },
    }
    path = tmp_path / "ruby_spectrum.csv"

    fit_success, peaks = MainWindow._save_ruby_spectrum_csv(
        acquired, str(path))

    assert fit_success is True
    assert peaks == (694.321, 692.804)
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert all(float(row["peak1_top"]) == 694.321 for row in rows)
    assert all(float(row["peak2_top"]) == 692.804 for row in rows)
    assert all(row["x_unit"] == "nm" for row in rows)
