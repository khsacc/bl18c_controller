"""Hardware-free checks for shared XRD reduction and scheduler wiring."""
from __future__ import annotations

import os
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from apps.Rad_icon_2022.instant1d import (
    bins_for_width, excluded_angle_mask, find_spot_mask, reduce_frame, save_profile,
)
from apps.Rad_icon_2022.image_view import levels_to_qimage, tint_excluded_angles
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from apps.exp_scheduler.actions import TakeXrdAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.scheduler_settings import GlobalShutterSettings, GlobalXrdSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.checks.action_params import check_xrd_params
from apps.exp_scheduler.validator.execution_trace import ExecutionTrace
from apps.exp_scheduler.validator.pre_validator import PreCheckResult
from utils.pdindexer import Transport, Trigger


class FakeIntegrator:
    wavelength = 1e-10
    dist = 0.2

    def __init__(self):
        self.args = None

    def center_array(self, shape, *, unit, scale):
        assert unit == "2th_deg" and scale is True
        return np.array([[0.0, 0.01], [0.02, 0.03]])

    def integrate1d(self, image, **kwargs):
        self.args = image, kwargs
        return SimpleNamespace(radial=np.array([1.0, 1.5]),
                               intensity=np.array([10.0, 20.0]))


class SharedReductionTests(unittest.TestCase):
    def test_manual_pdi_send_keeps_displayed_filename_after_recompute(self):
        service = MagicMock()
        service.clipboard_available.return_value = True
        panel = SimpleNamespace(
            _img_info_label=MagicMock(), _render_preview=lambda: None,
            _maybe_run_instant_1d=lambda *_args, **_kwargs: None,
            _pdi_service=service,
            _pdi_send_chk=SimpleNamespace(isChecked=lambda: False),
            _pdi_send_now_btn=MagicMock(),
            _pdi_transport_combo=SimpleNamespace(currentData=lambda: Transport.CLIPBOARD),
            _instant1d_last_profile=None,
        )
        frame = np.ones((2, 2), dtype=np.uint16)
        result = SimpleNamespace(radial=np.array([1.0]), intensity=np.array([2.0]))

        RadiconWindow._display_image(panel, frame, "C:/measurements/sample_0042.tif",
                                     trigger=Trigger.SINGLE_SHOT)
        RadiconWindow._update_pdi_profile(panel, result, 1e-10, None,
                                          trigger=Trigger.RECOMPUTE)
        RadiconWindow._send_current_pdi_profile(panel, trigger=Trigger.MANUAL)
        self.assertEqual(service.send.call_args.args[0][0].name, "sample_0042")

        RadiconWindow._display_image(panel, frame, trigger=Trigger.LIVE)
        RadiconWindow._update_pdi_profile(panel, result, 1e-10, None,
                                          trigger=Trigger.RECOMPUTE)
        self.assertTrue(panel._instant1d_last_profile.name.startswith("BL18C_"))

    def test_radicon_window_uses_its_own_spot_switch_and_shared_threshold(self):
        ai = FakeIntegrator()
        state = SimpleNamespace(is_calibrated=lambda: True, integration_ai=ai,
                                spot_deviation_factor=4.5)
        panel = SimpleNamespace(
            _instant1d_chk=SimpleNamespace(isChecked=lambda: True),
            _instant1d_find_spots_chk=SimpleNamespace(isChecked=lambda: True),
            _poni_state=state,
            _instant1d_binwidth_spin=SimpleNamespace(value=lambda: 0.005),
            _instant1d_angle_limits=lambda: (2.0, 3.0),
            _get_cached_npt=lambda *_: 10,
            _instant1d_curve=SimpleNamespace(setData=lambda *_: None),
            _instant1d_autosave_chk=SimpleNamespace(isChecked=lambda: False),
            _update_pdi_profile=lambda *_ , **__: None,
        )
        with patch("apps.Rad_icon_2022.radicon_ui.reduce_frame", return_value=SimpleNamespace(
            radial=np.array([1.0]), intensity=np.array([2.0]),
        )) as reduce:
            RadiconWindow._maybe_run_instant_1d(panel, np.ones((2, 2)), None, trigger=None)
        self.assertTrue(reduce.call_args.kwargs["find_spots"])
        self.assertEqual(reduce.call_args.kwargs["spot_deviation_factor"], 4.5)
        self.assertEqual(reduce.call_args.kwargs["min_2theta"], 2.0)
        self.assertEqual(reduce.call_args.kwargs["max_2theta"], 3.0)
        panel._instant1d_find_spots_chk = SimpleNamespace(isChecked=lambda: False)
        with patch("apps.Rad_icon_2022.radicon_ui.reduce_frame", return_value=SimpleNamespace(
            radial=np.array([1.0]), intensity=np.array([2.0]),
        )) as reduce:
            RadiconWindow._maybe_run_instant_1d(panel, np.ones((2, 2)), None, trigger=None)
        self.assertFalse(reduce.call_args.kwargs["find_spots"])

    def test_selected_2theta_range_sets_bins_and_pyfai_interval(self):
        ai = FakeIntegrator()
        frame = np.ones((2, 2), dtype=np.uint16)
        self.assertEqual(bins_for_width(ai, frame.shape, 0.005,
                                        min_2theta=0.005, max_2theta=0.025), 10)
        reduce_frame(ai, frame, 0.005, min_2theta=0.005, max_2theta=0.025)
        self.assertEqual(ai.args[1]["radial_range"], (0.005, 0.025))
        self.assertEqual(ai.args[1]["npt"], 10)
        np.testing.assert_array_equal(
            excluded_angle_mask(ai, frame.shape, 0.005, 0.025),
            [[True, False], [False, True]],
        )
        with self.assertRaisesRegex(ValueError, "Minimum 2θ"):
            reduce_frame(ai, frame, 0.005, min_2theta=0.03, max_2theta=0.01)

    def test_preview_tints_only_excluded_pixels(self):
        from PyQt6.QtGui import QColor

        image = levels_to_qimage(np.array([[100, 100]], dtype=np.uint16), 0, 255)
        tinted = tint_excluded_angles(image, np.array([[True, False]]))
        self.assertNotEqual(tinted.pixelColor(0, 0), QColor(100, 100, 100))
        self.assertEqual(tinted.pixelColor(1, 0), QColor(100, 100, 100))

    def test_real_pyfai_reduction_respects_2theta_range(self):
        from pyFAI.detectors import Detector
        from pyFAI.integrator.azimuthal import AzimuthalIntegrator

        ai = AzimuthalIntegrator(
            dist=0.2, poni1=0.0032, poni2=0.0032, wavelength=1e-10,
            detector=Detector(pixel1=1e-4, pixel2=1e-4),
        )
        result = reduce_frame(ai, np.ones((64, 64), dtype=np.uint16), 0.01,
                              min_2theta=0.2, max_2theta=1.0)
        self.assertGreaterEqual(result.radial[0], 0.2)
        self.assertLessEqual(result.radial[-1], 1.0)

    def test_spot_mask_uses_ring_sample_deviation_and_three_pixel_radius(self):
        ai = FakeIntegrator()
        ai.detector = SimpleNamespace(pixel2=1e-4, mask=None)
        ai.center_array = lambda shape, **_: np.zeros(shape)
        frame = np.ones((9, 9), dtype=np.uint16)
        frame[4, 4] = 100

        mask = find_spot_mask(ai, frame, 5.0)

        self.assertTrue(mask[4, 4])
        self.assertTrue(mask[4, 6])
        self.assertFalse(mask[4, 7])
        self.assertTrue(mask[6, 6])
        self.assertFalse(mask[7, 7])
        self.assertEqual(frame[4, 4], 100)
        reduce_frame(ai, frame, 0.005, find_spots=True)
        np.testing.assert_array_equal(ai.args[1]["mask"], mask)

    def test_spot_finding_ignores_rings_with_one_pixel(self):
        ai = FakeIntegrator()
        ai.detector = SimpleNamespace(pixel2=1e-4, mask=None)
        ai.center_array = lambda shape, **_: np.array([[0.0, 1.0]])
        self.assertFalse(find_spot_mask(ai, np.array([[100, 1]]), 5.0).any())

    def test_spot_mask_preserves_detector_mask(self):
        ai = FakeIntegrator()
        intrinsic = np.zeros((9, 9), dtype=bool)
        intrinsic[0, 0] = True
        ai.detector = SimpleNamespace(pixel2=1e-4, mask=intrinsic)
        ai.center_array = lambda shape, **_: np.zeros(shape)
        frame = np.ones((9, 9), dtype=np.uint16)
        mask = find_spot_mask(ai, frame, 5.0)
        self.assertTrue(mask[0, 0])
        self.assertEqual(mask.sum(), 1)

    def test_real_pyfai_integration_excludes_detected_spot(self):
        from pyFAI.detectors import Detector
        from pyFAI.integrator.azimuthal import AzimuthalIntegrator

        ai = AzimuthalIntegrator(
            dist=0.2, poni1=0.0032, poni2=0.0032, wavelength=1e-10,
            detector=Detector(pixel1=1e-4, pixel2=1e-4),
        )
        frame = np.ones((64, 64), dtype=np.uint16)
        frame[32, 44] = 10000
        plain = reduce_frame(ai, frame, 0.01, npt=50)
        masked = reduce_frame(ai, frame, 0.01, npt=50, find_spots=True)
        self.assertGreater(np.nanmax(plain.intensity), 50)
        self.assertLess(np.nanmax(masked.intensity), 2)

    def test_reduction_uses_radicon_integration_options(self):
        ai = FakeIntegrator()
        result = reduce_frame(ai, np.ones((2, 2), dtype=np.uint16), 0.005)
        image, kwargs = ai.args
        self.assertEqual(image.dtype, np.float32)
        self.assertEqual(kwargs["npt"], 10)
        self.assertEqual(kwargs["method"], ("full", "histogram", "cython"))
        self.assertEqual(kwargs["unit"], "2th_deg")
        self.assertTrue(kwargs["correctSolidAngle"])
        self.assertEqual(kwargs["polarization_factor"], 0.95)
        self.assertEqual(result.intensity.tolist(), [10.0, 20.0])

    def test_exports_selected_formats_beside_tiff(self):
        stem = f"_instant1d_{uuid4().hex}"
        image_path = Path(__file__).parent / f"{stem}.tif"
        paths = []
        try:
            paths, errors = save_profile(
                np.array([1.0, 1.5]), np.array([10.0, 20.0]),
                1e-10, 0.2, image_path, formats=("csv", "gsas", "igor"),
                units=("2theta", "q_A"), exposure_ms=1000, binning="2x2",
            )
            self.assertEqual(errors, [])
            self.assertEqual({path.suffix for path in paths},
                             {".csv", ".gsa", ".histogramIgor"})
            self.assertEqual(len(paths), 4)
            self.assertIn("2theta_deg,intensity", image_path.with_name(f"{stem}_2theta.csv").read_text())
            self.assertIn("BANK 1 2 2 CONST", image_path.with_suffix(".gsa").read_text())
            self.assertIn("WAVES/O twotheta, yint", image_path.with_suffix(".histogramIgor").read_text())
        finally:
            for path in paths:
                path.unlink(missing_ok=True)


class SchedulerReductionTests(unittest.TestCase):
    def test_validation_requires_calibration_only_when_enabled(self):
        trace = ExecutionTrace.build([TakeXrdAction()])
        result = PreCheckResult()
        check_xrd_params(trace.flat, GlobalXrdSettings(instant1d_enabled=True), result,
                         DeviceContext())
        self.assertTrue(any("poni calibration" in message for message in result.errors))
        result = PreCheckResult()
        check_xrd_params(trace.flat, GlobalXrdSettings(), result, DeviceContext())
        self.assertFalse(any("poni calibration" in message for message in result.errors))

    def test_validation_rejects_reversed_2theta_limits(self):
        trace = ExecutionTrace.build([TakeXrdAction()])
        result = PreCheckResult()
        check_xrd_params(trace.flat, GlobalXrdSettings(
            instant1d_enabled=True, instant1d_min_2theta=30.0,
            instant1d_max_2theta=10.0), result, DeviceContext())
        self.assertTrue(any("Minimum 2θ" in message for message in result.errors))

    def test_validation_rejects_non_boolean_spot_switch(self):
        trace = ExecutionTrace.build([TakeXrdAction()])
        result = PreCheckResult()
        check_xrd_params(trace.flat, GlobalXrdSettings(
            instant1d_enabled=True, instant1d_find_spots_before_1d="false"),
            result, DeviceContext())
        self.assertTrue(any("spot detection" in message for message in result.errors))

    def test_reduction_happens_after_shutter_insertion(self):
        ai = FakeIntegrator()
        shutter = MagicMock()
        state = SimpleNamespace(ai=ai)
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(poni_state=state, shutter=shutter),
            global_xrd=GlobalXrdSettings(instant1d_enabled=True),
            global_shutter=GlobalShutterSettings(stop_on_ring_fault=False),
        )
        runner._logger = MagicMock()
        runner._shutter_managed = True
        events = []
        runner._open_shutter_for_xrd = lambda: events.append("Out")
        runner._do_take_xrd_exposed = lambda *_: (events.append("capture") or
                                                    (np.ones((2, 2)), None, 1000, 1000))
        runner._insert_shutter = lambda: events.append("In")
        runner._run_instant1d = lambda *_: events.append("reduce")
        runner._do_take_xrd(TakeXrdAction(), 0)
        self.assertEqual(events, ["Out", "capture", "In", "reduce"])
        def reduction_fails(*_):
            raise RuntimeError("integrator failed")

        runner._run_instant1d = reduction_fails
        with self.assertRaisesRegex(RuntimeError, "integrator failed"):
            runner._do_take_xrd(TakeXrdAction(), 0)
        self.assertEqual(events[-3:], ["Out", "capture", "In"])

    def test_runner_reduces_frame_and_emits_pdi_profile(self):
        ai = FakeIntegrator()
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(poni_state=SimpleNamespace(ai=ai)),
            global_xrd=GlobalXrdSettings(instant1d_enabled=True,
                                         instant1d_send_pdi=True),
            global_shutter=GlobalShutterSettings(stop_on_ring_fault=False),
        )
        runner._logger = MagicMock()
        profiles = []
        runner.pdi_profile_ready.connect(profiles.append)
        runner._run_instant1d(np.ones((2, 2)), None, 1000, 1000)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(len(profiles[0].x), 2)
        self.assertAlmostEqual(profiles[0].wavelength_nm, 0.1)

    def test_runner_snapshots_spot_settings_before_thread_starts(self):
        ai = FakeIntegrator()
        state = SimpleNamespace(ai=ai, spot_deviation_factor=4.5)
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(poni_state=state),
            global_xrd=GlobalXrdSettings(instant1d_enabled=True,
                                         instant1d_find_spots_before_1d=True,
                                         instant1d_min_2theta=1.0,
                                         instant1d_max_2theta=30.0),
            global_shutter=GlobalShutterSettings(stop_on_ring_fault=False),
        )
        runner._logger = MagicMock()
        runner._global_xrd.instant1d_find_spots_before_1d = False
        state.spot_deviation_factor = 8.0
        with patch("apps.exp_scheduler.runner.reduce_frame", return_value=SimpleNamespace(
            radial=np.array([1.0]), intensity=np.array([2.0]),
        )) as reduce:
            runner._run_instant1d(np.ones((2, 2)), None, 1000, 1000)
        self.assertTrue(reduce.call_args.kwargs["find_spots"])
        self.assertEqual(reduce.call_args.kwargs["spot_deviation_factor"], 4.5)
        self.assertEqual(reduce.call_args.kwargs["min_2theta"], 1.0)
        self.assertEqual(reduce.call_args.kwargs["max_2theta"], 30.0)

    def test_xrd_capture_passes_corrected_frame_to_reduction(self):
        ai = FakeIntegrator()
        frame = np.array([[1, 2], [3, 4]], dtype=np.uint16)
        backend = SimpleNamespace(width=1000, capture_triggered=lambda *_ , **__: frame)
        runner = SequenceRunner(
            Sequence(actions=[]),
            DeviceContext(radicon=backend, poni_state=SimpleNamespace(ai=ai)),
            global_xrd=GlobalXrdSettings(instant1d_enabled=True,
                                         defect_enabled=False, flip_v=True),
            global_shutter=GlobalShutterSettings(stop_on_ring_fault=False),
        )
        runner._logger = MagicMock()
        runner._do_take_xrd(TakeXrdAction(save=False), 0)
        np.testing.assert_array_equal(ai.args[0], np.flipud(frame).astype(np.float32))

    def test_runner_autosaves_selected_profile_beside_tiff(self):
        ai = FakeIntegrator()
        image_path = Path(__file__).parent / f"_instant1d_{uuid4().hex}.tif"
        csv_path = image_path.with_name(f"{image_path.stem}_2theta.csv")
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(poni_state=SimpleNamespace(ai=ai)),
            global_xrd=GlobalXrdSettings(
                instant1d_enabled=True, instant1d_autosave=True,
                instant1d_fmt_csv=True,
            ),
            global_shutter=GlobalShutterSettings(stop_on_ring_fault=False),
        )
        runner._logger = MagicMock()
        try:
            runner._run_instant1d(np.ones((2, 2)), image_path, 1000, 1000)
            self.assertTrue(csv_path.is_file())
            self.assertIn("2theta_deg,intensity", csv_path.read_text())
        finally:
            csv_path.unlink(missing_ok=True)

    def test_gui_defaults_off_and_settings_round_trip(self):
        from PyQt6.QtWidgets import QApplication
        from apps.exp_scheduler.ui import scheduler_window

        app = QApplication.instance() or QApplication([])
        saved = {}
        settings_path = MagicMock()
        settings_path.read_text.side_effect = lambda **_: json.dumps(saved)
        settings_path.write_text.side_effect = lambda data, **_: saved.update(json.loads(data))
        with patch.object(scheduler_window, "_SETTINGS_PATH", settings_path):
            first = scheduler_window.ExperimentalSchedulerWindow(DeviceContext())
            try:
                self.assertFalse(first._xrd_1d_chk.isChecked())
                self.assertTrue(first._xrd_1d_controls.isHidden())
                first._xrd_1d_chk.setChecked(True)
                first._xrd_1d_binwidth_spin.setValue(0.02)
                first._xrd_1d_find_spots_chk.setChecked(True)
                first._xrd_1d_min_spin.setValue(2.0)
                first._xrd_1d_max_spin.setValue(30.0)
                first._xrd_1d_autosave_chk.setChecked(True)
                first._xrd_1d_format_chks["csv"].setChecked(True)
                first._xrd_1d_unit_chks["q_A"].setChecked(True)
                first._save_xrd_settings()
                self.assertTrue(saved["global_xrd"]["instant1d_enabled"])
                self.assertTrue(saved["global_xrd"]["instant1d_find_spots_before_1d"])
                self.assertEqual(saved["global_xrd"]["instant1d_units"],
                                 ["2theta", "q_A"])
                sequence_file = first._sequence_with_current_settings().to_dict()
                loaded = Sequence.from_dict(sequence_file)
                self.assertTrue(loaded.global_xrd["instant1d_enabled"])
                self.assertEqual(loaded.global_xrd["instant1d_bin_width_deg"], 0.02)
                self.assertTrue(loaded.global_xrd["instant1d_find_spots_before_1d"])
                self.assertEqual(loaded.global_xrd["instant1d_min_2theta"], 2.0)
                self.assertEqual(loaded.global_xrd["instant1d_max_2theta"], 30.0)
            finally:
                first.close()
            second = scheduler_window.ExperimentalSchedulerWindow(DeviceContext())
            try:
                self.assertTrue(second._xrd_1d_chk.isChecked())
                self.assertEqual(second._build_global_xrd().instant1d_bin_width_deg,
                                 0.02)
                self.assertEqual(second._build_global_xrd().instant1d_min_2theta, 2.0)
                self.assertTrue(second._build_global_xrd().instant1d_find_spots_before_1d)
                self.assertTrue(second._xrd_1d_format_chks["csv"].isChecked())
            finally:
                second.close()

    def test_unavailable_saved_pdindexer_transport_is_preserved_and_rejected(self):
        from PyQt6.QtWidgets import QApplication
        from apps.exp_scheduler.ui import scheduler_window

        app = QApplication.instance() or QApplication([])
        settings_path = MagicMock()
        settings_path.read_text.return_value = "{}"
        with patch.object(scheduler_window, "_SETTINGS_PATH", settings_path):
            window = scheduler_window.ExperimentalSchedulerWindow(DeviceContext())
            try:
                with (patch.object(window._pdi_service, "clipboard_available", return_value=False),
                      patch.object(window._pdi_service, "watch_folder_configured", return_value=True)):
                    window._refresh_pdi_controls()
                    window._xrd_dict_to_ui({"instant1d_transport": "clipboard",
                                            "instant1d_send_pdi": True})
                    self.assertIs(window._xrd_1d_transport_combo.currentData(),
                                  Transport.CLIPBOARD)
                    self.assertFalse(window._pdi_transport_available(
                        window._xrd_1d_transport_combo.currentData()))
                    self.assertEqual(window._build_global_xrd().instant1d_transport,
                                     "clipboard")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
