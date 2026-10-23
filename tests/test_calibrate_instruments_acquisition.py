"""Detector-calibration acquisition tests with no hardware access."""

import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.image_utils import read_tiff_metadata
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from apps.calibrate_instruments.calibrate_instruments_app import CalibrateInstrumentsWindow
from settings.i18n import tr


@contextmanager
def _test_image_path():
    path = Path.cwd() / f"test_calibrate_xrd_{uuid4().hex}.tif"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


class _Backend:
    def __init__(self, fail=False, delay_s=0.0):
        self.exposure_ms = None
        self.timeout_ms = None
        self.fail = fail
        self.delay_s = delay_s

    def capture_triggered(self, ms, timeout_ms=None):
        self.exposure_ms = ms
        self.timeout_ms = timeout_ms
        time.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("simulated detector failure")
        return np.array([[5, 7], [9, 11]], dtype=np.uint16)


class _RadiconSettings:
    _process_frame = RadiconWindow._process_frame
    _begin_external_acquisition = RadiconWindow._begin_external_acquisition
    _end_external_acquisition = RadiconWindow._end_external_acquisition

    def __init__(self, dark_enabled, defect_mode="none"):
        self._exp_spin = QtWidgets.QDoubleSpinBox()
        self._exp_spin.setRange(0.001, 60.0)
        self._exp_spin.setValue(60.0)
        self._dir_edit = QtWidgets.QLineEdit()
        self._filename_edit = QtWidgets.QLineEdit("CeO2")
        self._flip_v_chk = QtWidgets.QCheckBox()
        self._flip_v_chk.setChecked(True)
        self._flip_h_chk = QtWidgets.QCheckBox()
        self._dark_correct_chk = QtWidgets.QCheckBox()
        self._dark_correct_chk.setChecked(dark_enabled)
        self._dark_img = np.full((2, 2), 2.0)
        self._defect_mode = defect_mode
        self._defect_mask = np.array([[True, False], [False, False]])
        self._defect_kernel_combo = QtWidgets.QComboBox()
        self._defect_kernel_combo.addItem("3 x 3")
        self._external_acquisition = False
        self.controls_locked = False

    def _current_defect_mode(self):
        return self._defect_mode

    def _any_busy(self):
        return self._external_acquisition

    def _set_busy(self, busy):
        self.controls_locked = busy

    def _build_metadata(self, image_type, extra):
        return {"image_type": image_type, **extra}


def test_take_xrd_uses_radicon_exposure_and_dark_setting():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend()
    settings = _RadiconSettings(dark_enabled=True)
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    captured = []
    window._commit_position_image = (
        lambda row, image, ch9, elapsed, path: captured.append(image.copy())
    )
    window._save_captured_xrd = lambda *args: Path("capture.tif")

    window._on_take_xrd(row)
    deadline = time.monotonic() + 5
    while settings._external_acquisition and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert not settings._external_acquisition
    assert not settings.controls_locked
    assert backend.exposure_ms == 60_000
    assert backend.timeout_ms == 130_000
    assert len(captured) == 1
    np.testing.assert_array_equal(captured[0], [[7, 9], [3, 5]])
    window.close()


def test_take_xrd_shows_elapsed_time_and_keeps_duration_after_capture():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    settings = _RadiconSettings(dark_enabled=False)
    window = CalibrateInstrumentsWindow(
        backend=_Backend(delay_s=0.35), get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    window._current_ch9 = -42
    window._last_xrd_save_dir = ""
    window._save_prefs = lambda: None

    with _test_image_path() as target:
        settings._dir_edit.setText(str(target.parent))
        with mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName",
                               return_value=(str(target), "")) as save_dialog:
            window._on_take_xrd(row)
            initial_status = row.status_label.text()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and row.status_label.text() == initial_status:
                app.processEvents()
                time.sleep(0.01)

            assert window._xrd_status_timer.isActive()
            assert row.status_label.text() != initial_status
            assert row.acquisition_time_s is None

            deadline = time.monotonic() + 5
            while settings._external_acquisition and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)

        assert not settings._external_acquisition
        assert not window._xrd_status_timer.isActive()
        assert save_dialog.call_args.args[2] == str(
            target.parent / f"{time.strftime('%Y%m%d')}_CeO2_mgs1_Ch9-42.tif"
        )
        assert window._default_xrd_filename(row, 42, settings).endswith("_Ch9+42.tif")
        assert row.saved_image_path == target
        assert target.exists()
        import tifffile
        np.testing.assert_array_equal(tifffile.imread(target), [[9, 11], [5, 7]])
        metadata = read_tiff_metadata(target)
        assert metadata["calibration_mgs_mm"] == 1.0
        assert metadata["calibration_ch9_pulses"] == -42
        assert metadata["sample_name"] == "CeO2"
        assert row.acquisition_time_s is not None
        assert row.acquisition_time_s >= 0.3
        duration_text = tr("   Acquisition time: {sec:.1f} s", sec=row.acquisition_time_s)
        assert duration_text in row.status_label.text()
        assert tr("   ✓ saved") in row.status_label.text()
        assert row.status_label.toolTip() == str(target)
        row.mark_captured(-42, 4)
        assert duration_text in row.status_label.text()
        window._commit_position_image(row, row.position.image, None)
        assert row.acquisition_time_s is None
        assert row.saved_image_path is None
        assert duration_text not in row.status_label.text()
    window.close()


def test_take_xrd_uses_radicon_defect_setting():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend()
    settings = _RadiconSettings(dark_enabled=True, defect_mode="median")
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    window._save_captured_xrd = lambda *args: Path("capture.tif")
    window._on_take_xrd(row)
    deadline = time.monotonic() + 5
    while settings._external_acquisition and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert row.position.image is not None
    np.testing.assert_array_equal(row.position.image, [[7, 9], [7, 5]])
    window.close()


def test_take_xrd_cancelled_save_keeps_previous_image():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    settings = _RadiconSettings(dark_enabled=False)
    window = CalibrateInstrumentsWindow(
        backend=_Backend(), get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    previous = np.array([[1, 2], [3, 4]], dtype=np.uint16)
    window._commit_position_image(row, previous, 12, 2.0, Path("previous.tif"))
    with mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName",
                           return_value=("", "")) as save_dialog:
        window._on_take_xrd(row)
        deadline = time.monotonic() + 5
        while settings._external_acquisition and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

    assert save_dialog.call_count == 1
    assert not settings._external_acquisition
    np.testing.assert_array_equal(row.position.image, previous)
    assert row.saved_image_path == Path("previous.tif")
    assert row.status_label.text() == tr("New capture not saved; previous image retained")
    window.close()


def test_failed_xrd_save_allows_another_path():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    settings = _RadiconSettings(dark_enabled=False)
    window = CalibrateInstrumentsWindow(get_radicon_window=lambda: settings)
    window._ch9_timer.stop()
    window._save_prefs = lambda: None
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    processed = SimpleNamespace(dark_applied=False, defect_applied=False)
    bad = Path.cwd() / "bad_calibration_capture.tif"
    good = Path.cwd() / "good_calibration_capture.tif"
    with (mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName",
                            side_effect=[(str(bad), ""), (str(good), "")]) as dialog,
          mock.patch("apps.calibrate_instruments.calibrate_instruments_app.save_tiff",
                     side_effect=[False, True]) as writer,
          mock.patch.object(QtWidgets.QMessageBox, "critical") as error):
        result = window._save_captured_xrd(
            row, np.zeros((2, 2), dtype=np.uint16), processed, settings, 5, 1.0,
        )

    assert result == good
    assert dialog.call_count == 2
    assert writer.call_count == 2
    assert error.call_count == 1
    assert window._last_xrd_save_dir == str(good.parent)
    window.close()


def test_take_xrd_rejects_mismatched_correction_mask():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    settings = _RadiconSettings(dark_enabled=False, defect_mode="median")
    settings._defect_mask = np.zeros((3, 3), dtype=bool)
    window = CalibrateInstrumentsWindow(
        backend=_Backend(), get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    messages = []
    original_critical = QtWidgets.QMessageBox.critical
    QtWidgets.QMessageBox.critical = lambda *args: messages.append(args[-1])
    try:
        window._on_take_xrd(row)
        deadline = time.monotonic() + 5
        while settings._external_acquisition and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    finally:
        QtWidgets.QMessageBox.critical = original_critical

    assert row.position.image is None
    assert len(messages) == 1 and messages[0]
    assert not settings.controls_locked
    window.close()


def test_take_xrd_prompts_to_open_radicon_before_acquisition(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend()
    opened = []
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: None,
        open_radicon_window=lambda: opened.append(True),
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        lambda *args: QtWidgets.QMessageBox.StandardButton.Yes,
    )

    window._on_take_xrd(row)

    assert opened == [True]
    assert backend.exposure_ms is None
    assert row.position.image is None
    window.close()


def test_take_xrd_releases_radicon_on_detector_error():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend(fail=True)
    settings = _RadiconSettings(dark_enabled=False)
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    messages = []
    original_critical = QtWidgets.QMessageBox.critical
    QtWidgets.QMessageBox.critical = lambda *args: messages.append(args[-1])
    try:
        window._on_take_xrd(row)
        deadline = time.monotonic() + 5
        while settings._external_acquisition and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    finally:
        QtWidgets.QMessageBox.critical = original_critical

    assert not settings._external_acquisition
    assert not settings.controls_locked
    assert row.position.image is None
    assert messages == ["simulated detector failure"]
    assert not window._xrd_status_timer.isActive()
    window.close()
