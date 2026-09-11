import csv
from types import SimpleNamespace

import cv2
import numpy as np
from PyQt6 import QtCore, QtWidgets

from apps.interactive_camera import interactive_camera as camera_module
from apps.interactive_camera.interactive_camera import MainWindow


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


def test_sample_environment_controls_and_overlay(monkeypatch):
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

    raw = np.zeros((480, 640, 3), dtype=np.uint8)
    displayed = raw.copy()
    window._draw_sample_environment(displayed)
    assert np.any(displayed != 0)
    assert not np.any(raw != 0)

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
