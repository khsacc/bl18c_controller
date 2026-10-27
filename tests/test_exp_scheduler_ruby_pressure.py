from __future__ import annotations

import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from apps.exp_scheduler import ruby_pressure
from apps.exp_scheduler.actions import TakeSpectrumAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import RunnerError, SequenceRunner, _StopRequested
from apps.exp_scheduler.scheduler_settings import GlobalLimits, GlobalSpectrumSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator
from utils.fluora_pressee import FluoraPresseeReader
from utils.stage.control_stage_sim import PM16CControllerSim


class FakeLakeShore:
    def __init__(self, value_k=80.0, age_s=0.5, connected=True):
        self.value_k, self.age_s, self.connected = value_k, age_s, connected

    def get_cached_readings(self):
        return {
            "connected": self.connected,
            "timestamp": time.time() - self.age_s,
            "temperature_a_k": 77.0,
            "temperature_b_k": self.value_k,
            "target_setpoint_k": 300.0,  # must never be used
        }


def pressure_settings(**overrides) -> GlobalSpectrumSettings:
    values = dict(
        offset_ch4_pulse=1, offset_ch5_pulse=0, settle_ms=0,
        pressure_enabled=True, pressure_scale="ruby_shen_2020",
        ruby_references={"Default": {"lambda_0_nm": 694.30, "t0_k": 298.15}},
    )
    values.update(overrides)
    return GlobalSpectrumSettings(**values)


def pressure_response(**overrides) -> dict:
    response = {
        "x": [690.0, 694.0, 698.0], "y": [1.0, 2.0, 1.0], "y_raw": [1.0, 2.0, 1.0],
        "mode": "1d", "saturated": False,
        "x_axis": {"source": "calibrated", "unit": "nm", "calibrated": True},
        "fit": {"success": True, "fit": {"R2": 0.99, "peaks": [
            {"position": 695.0, "position_err": 0.01},
            {"position": 693.6, "position_err": 0.02},
        ]}},
        "pressure_gpa": 1.92, "pressure_err_gpa": 0.03,
        "zero_pressure_peak_at_current_t": 694.30, "temperature_warning": None,
    }
    response.update(overrides)
    return response


class RubyPressureLogicTests(unittest.TestCase):
    def test_pressure_stop_uses_lower_edge_and_valid_measurements_only(self):
        action = TakeSpectrumAction(stop_pressure_target_gpa=1.0,
                                    stop_pressure_tolerance_gpa=0.1)
        settings = pressure_settings()
        self.assertIsNone(ruby_pressure.stop_condition_error(action, settings))
        record = ruby_pressure.new_record(
            settings, "Default", ruby_pressure.reference_for(settings, "Default"), None,
        )
        record.status, record.pressure_gpa = "valid", 0.9
        self.assertTrue(ruby_pressure.stop_condition_met(action, record))
        record.pressure_gpa = 1.2
        self.assertTrue(ruby_pressure.stop_condition_met(action, record))
        record.pressure_gpa = 0.899
        self.assertFalse(ruby_pressure.stop_condition_met(action, record))
        record.status, record.pressure_gpa = "temperature_out_of_range", 1.2
        self.assertFalse(ruby_pressure.stop_condition_met(action, record))
        self.assertIsNotNone(ruby_pressure.stop_condition_error(
            action, pressure_settings(pressure_enabled=False)))
        self.assertIsNotNone(ruby_pressure.stop_condition_error(
            TakeSpectrumAction(stop_pressure_target_gpa=1.0,
                               stop_pressure_tolerance_gpa=1.0), settings))

    def test_scale_must_be_selected_explicitly_and_each_ruby_needs_its_own_lambda0(self):
        settings = pressure_settings(
            pressure_scale="",
            ruby_references={"Ruby A": {"lambda_0_nm": 694.3, "t0_k": None}},
        )
        codes = [code for code, _ in ruby_pressure.settings_errors(settings, ["Ruby A", "Ruby B"])]
        self.assertIn("scale_missing", codes)
        # No fallback from Ruby B to Ruby A's reference.
        self.assertEqual(codes.count("reference_missing"), 1)
        self.assertIsNone(ruby_pressure.reference_for(settings, "Ruby B"))

    def test_temperature_correction_needs_scale_and_t0(self):
        settings = pressure_settings(
            temperature_correction_enabled=True, temperature_scale="",
            ruby_references={"Default": {"lambda_0_nm": 694.3, "t0_k": None}},
        )
        codes = {code for code, _ in ruby_pressure.settings_errors(settings, ["Default"])}
        self.assertEqual(codes, {"temperature_scale_missing", "reference_missing"})
        self.assertEqual(ruby_pressure.settings_errors(
            pressure_settings(pressure_enabled=False, pressure_scale=""), ["Default"]), [])

    def test_lakeshore_reading_must_be_connected_finite_and_fresh(self):
        reading = ruby_pressure.read_lakeshore_b(FakeLakeShore(80.0, 0.5), 10.0)
        self.assertAlmostEqual(reading.value_k, 80.0)
        self.assertEqual(reading.source, "lakeshore_b")
        for lakeshore in (None, FakeLakeShore(connected=False),
                          FakeLakeShore(float("nan")), FakeLakeShore(age_s=30.0)):
            with self.assertRaises(ruby_pressure.PressureSetupError):
                ruby_pressure.read_lakeshore_b(lakeshore, 10.0)

    def test_status_requires_1d_wavelength_calibration(self):
        good = {"roi": {"mode": "1d_roi"}, "calibration": {"applied": True, "unit": "Wavelength"},
                "configuration": {"axis_mode": "calibrated", "unit": "Wavelength"}}
        ruby_pressure.check_status(good)
        for bad in (
            {**good, "roi": {"mode": "2d"}},
            {**good, "calibration": {"applied": False}, "configuration": {"axis_mode": "pixel"}},
            {**good, "configuration": {"axis_mode": "calibrated", "unit": "Raman shift"}},
        ):
            with self.assertRaises(ruby_pressure.PressureSetupError):
                ruby_pressure.check_status(bad)

    def test_request_contains_reference_and_measured_temperature(self):
        settings = pressure_settings()
        reference = ruby_pressure.reference_for(settings, "Default")
        request = ruby_pressure.build_request(settings, reference, None)
        self.assertEqual(request, {
            "sensor": "ruby", "pressure_scale": "ruby_shen_2020",
            "zero_pressure_peak": 694.30, "pressure_peak_index": 1,
        })
        settings = pressure_settings(temperature_correction_enabled=True,
                                     temperature_scale="ruby_datchi_2007_lt")
        temperature = ruby_pressure.TemperatureReading(80.0, "lakeshore_b", time.time(), 0.2)
        request = ruby_pressure.build_request(settings, reference, temperature)
        self.assertEqual(request["temperature_correction"], {
            "enabled": True, "scale": "ruby_datchi_2007_lt", "current_t": 80.0,
            "t0": 298.15, "zero_pressure_peak_at_t0": 694.30,
        })

    def _evaluate(self, response, **settings_overrides):
        settings = pressure_settings(**settings_overrides)
        record = ruby_pressure.new_record(
            settings, "Default", ruby_pressure.reference_for(settings, "Default"), None,
        )
        return ruby_pressure.evaluate(record, response, quality_passed=True)

    def test_valid_response_and_rejections(self):
        record = self._evaluate(pressure_response())
        self.assertEqual(record.status, "valid")
        self.assertEqual((record.peak_nm, record.pressure_gpa), (695.0, 1.92))
        for overrides in (
            {"x_axis": {"unit": "cm-1"}},
            {"pressure_gpa": None},
            {"zero_pressure_peak_at_current_t": 680.0},
            {"fit": {"success": True, "fit": {"R2": 0.99, "peaks": [
                {"position": 693.6}, {"position": 695.0}]}}},
            {"fit": {"success": True, "fit": {"R2": 0.99, "peaks": [
                {"position": 720.0}, {"position": 693.6}]}}},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(self._evaluate(pressure_response(**overrides)).status, "invalid")

    def test_temperature_warning_is_not_a_valid_pressure(self):
        record = self._evaluate(pressure_response(
            temperature_warning="Temperature 400 K is outside the valid range",
        ))
        self.assertEqual(record.status, "temperature_out_of_range")
        self.assertFalse(record.is_measured)
        self.assertEqual(record.pressure_gpa, 1.92)  # kept for re-analysis
        self.assertTrue(any("Warning" in line for line in ruby_pressure.summary_lines(record)))

    def test_reader_posts_pressure_fields_to_acquire_pressure(self):
        requests = []

        class Response:
            def __init__(self, body):
                self._body = json.dumps(body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._body

        def opener(request, timeout):
            requests.append(request)
            return Response(pressure_response())

        reader = FluoraPresseeReader(api_key="secret", opener=opener)
        reader.acquire_spectrum_with_pressure({"sensor": "ruby", "pressure_scale": "ruby_shen_2020",
                                               "zero_pressure_peak": 694.3,
                                               "pressure_peak_index": 1})
        self.assertTrue(requests[0].full_url.endswith("/acquire/pressure"))
        body = json.loads(requests[0].data)
        self.assertEqual(body["fit_peak_count"], 2)
        self.assertEqual(body["peak_sort_order"], "x_desc")
        self.assertEqual(body["pressure_peak_index"], 1)
        self.assertNotIn("secret", requests[0].data.decode())


class RubyPressureRunnerTests(unittest.TestCase):
    def test_pressure_stop_keeps_ruby_position_and_skips_return(self):
        runner, ctrl = self._runner(pressure_settings())
        action = TakeSpectrumAction(save=False, stop_pressure_target_gpa=1.0,
                                    stop_pressure_tolerance_gpa=0.1)
        with mock.patch.object(ruby_pressure, "simulated_fields", return_value={
            "x_axis": {"unit": "nm"}, "pressure_gpa": 0.95,
            "zero_pressure_peak_at_current_t": 694.30,
            "temperature_warning": None,
        }):
            with self.assertRaises(_StopRequested):
                runner._do_take_spectrum(action, 0)
        self.assertTrue(runner._stop_event.is_set())
        self.assertTrue(runner._condition_stop)   # → sequence_condition_stopped, not a user stop
        self.assertFalse(runner.user_stop_requested)
        self.assertIn("0.95", runner.stop_reason)
        self.assertEqual(int(ctrl.get_ch_pos(4)), 20001)
        self.assertIn("pressure_stop_triggered=true", runner._logger.science[-1][1]["note"])

    def _runner(self, settings, lakeshore=None, run_dir=None):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        with ctrl._state_lock:
            for ch, value in ((3, 40000), (4, 20000), (5, 20000)):
                ctrl._positions[ch] = ctrl._targets[ch] = value
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=ctrl, lakeshore=lakeshore),
            global_limits=GlobalLimits(
                ch3_minus_mm=10, ch3_plus_mm=10, ch4_minus_mm=10,
                ch4_plus_mm=10, ch5_minus_mm=10, ch5_plus_mm=10,
            ), global_spectrum=settings,
        )

        class Logger:
            def __init__(self):
                self.ops, self.science = [], []
                self.run_dir = run_dir

            def log_ops(self, message):
                self.ops.append(message)

            def log_science(self, event, **values):
                self.science.append((event, values))

        runner._logger = Logger()
        runner._baseline_pos = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        runner._motion_lease = ctrl.acquire_motion("test", "ruby pressure")
        self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
        return runner, ctrl

    def test_simulated_pressure_is_saved_logged_and_plotted(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = pressure_settings(
                temperature_correction_enabled=True,
                temperature_scale="ruby_datchi_2007_lt",
            )
            runner, ctrl = self._runner(settings, FakeLakeShore(80.0), Path(temporary))
            plotted = []
            details = []
            runner.sample_pressure_ready.connect(lambda *point: plotted.append(point))
            runner.ruby_sample_ready.connect(details.append)
            runner._do_take_spectrum(TakeSpectrumAction(), 0)
            [path] = sorted(Path(temporary, "spectrum").glob("*.npz"))
            with np.load(path) as data:
                self.assertEqual(str(data["pressure_status"]), "valid")
                self.assertAlmostEqual(float(data["pressure_lambda_0_nm"]), 694.30)
                self.assertAlmostEqual(float(data["pressure_t0_k"]), 298.15)
                self.assertAlmostEqual(float(data["pressure_measured_temperature_k"]), 80.0)
                self.assertEqual(str(data["pressure_scale"]), "ruby_shen_2020")
                self.assertEqual(str(data["temperature_scale"]), "ruby_datchi_2007_lt")
                self.assertTrue(np.isfinite(float(data["pressure_gpa"])))
                request = json.loads(str(data["pressure_request_json"]))
                self.assertEqual(request["temperature_correction"]["current_t"], 80.0)
                self.assertNotIn("api_key", str(data["metadata_json"]))
                saved_pressure = float(data["pressure_gpa"])
                corrected_lambda_0 = float(data["pressure_zero_pressure_peak_at_current_t_nm"])
            self.assertTrue(path.with_suffix(".png").exists())
            self.assertIn("pressure.pressure_gpa", path.with_suffix(".csv").read_text(encoding="utf-8"))
            with (path.parent / "ruby_master.csv").open(
                    newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["ruby_name"], "Default")
            self.assertEqual(row["pressure_status"], "valid")
            self.assertEqual(row["pressure_scale"], "ruby_shen_2020")
            self.assertAlmostEqual(float(row["pressure_GPa"]), saved_pressure)
            self.assertEqual(row["temperature_scale"], "ruby_datchi_2007_lt")
            self.assertEqual(row["temperature_source"], "lakeshore_b")
            self.assertEqual(row["temperature_used_K"], "80.0")
            self.assertAlmostEqual(float(row["lambda_0_corrected_nm"]), corrected_lambda_0)
            note = runner._logger.science[-1][1]["note"]
            self.assertIn("pressure_status=valid", note)
            self.assertIn("T_used_K=80.000", note)
            self.assertEqual(len(plotted), 1)
            self.assertAlmostEqual(plotted[0][1], saved_pressure)
            self.assertEqual(plotted[0][2], 80.0)
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["name"], "Default")
            self.assertAlmostEqual(details[0]["pressure_gpa"], saved_pressure)
            self.assertTrue(np.isfinite(details[0]["peak_nm"]))
            self.assertTrue(details[0]["temperature_correction_enabled"])
            self.assertEqual(details[0]["correction_temperature_k"], 80.0)
            self.assertEqual(details[0]["correction_temperature_source"], "lakeshore_b")
            self.assertTrue(details[0]["correction_temperature_timestamp"])
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_temperature_out_of_range_keeps_spectrum_but_not_the_pressure(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = pressure_settings(
                temperature_correction_enabled=True,
                temperature_scale="ruby_kobayashi_unpublished",
            )
            runner, ctrl = self._runner(settings, FakeLakeShore(350.0), Path(temporary))
            plotted = []
            details = []
            runner.sample_pressure_ready.connect(lambda *point: plotted.append(point))
            runner.ruby_sample_ready.connect(details.append)
            runner._do_take_spectrum(TakeSpectrumAction(), 0)
            [path] = sorted(Path(temporary, "spectrum").glob("*.npz"))
            with np.load(path) as data:
                self.assertEqual(str(data["pressure_status"]), "temperature_out_of_range")
                self.assertIn("outside the valid range", str(data["temperature_warning"]))
            self.assertIn("pressure_status=temperature_out_of_range",
                          runner._logger.science[-1][1]["note"])
            self.assertEqual(plotted, [])
            self.assertEqual(details, [])
            self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)

    def test_missing_reference_or_stale_temperature_stops_before_motion(self):
        cases = (
            (pressure_settings(ruby_references=None), None, "runtime.spectrum_pressure_invalid"),
            (pressure_settings(temperature_correction_enabled=True,
                               temperature_scale="ruby_datchi_2007_lt"),
             FakeLakeShore(age_s=60.0), "runtime.spectrum_temperature_unavailable"),
        )
        for settings, lakeshore, code in cases:
            with self.subTest(code=code):
                runner, ctrl = self._runner(settings, lakeshore)
                with mock.patch.object(runner, "_do_stage") as stage:
                    with self.assertRaises(RunnerError) as raised:
                        runner._do_take_spectrum(TakeSpectrumAction(save=False), 0)
                self.assertEqual(raised.exception.code, code)
                stage.assert_not_called()

    def test_without_pressure_old_settings_still_measure_a_spectrum(self):
        runner, ctrl = self._runner(GlobalSpectrumSettings(
            offset_ch4_pulse=1, offset_ch5_pulse=0, settle_ms=0,
        ))
        runner._do_take_spectrum(TakeSpectrumAction(save=False), 0)
        self.assertNotIn("pressure_status", runner._logger.science[-1][1]["note"])

    def test_remote_final_acquisition_uses_pressure_endpoint_and_invalid_result_returns(self):
        runner, ctrl = self._runner(pressure_settings(baseline_model="auto_polynomial"))
        calls = []

        class Reader:
            def acquire_spectrum_with_pressure(self, pressure, **kwargs):
                calls.append((pressure, kwargs))
                return pressure_response(pressure_gpa=None)

            def acquire_spectrum_with_fit(self, **kwargs):
                raise AssertionError("final acquisition must use /acquire/pressure")

        with mock.patch.object(runner, "_align_to_xrd_reference"):
            with self.assertRaises(RunnerError) as raised:
                runner._acquire_spectrum_at_ruby(
                    TakeSpectrumAction(save=False), 0, runner._global_spectrum,
                    {3: 0, 4: 1, 5: 0}, 0.1, False, Reader(), (),
                    {"available": False}, ruby_pressure.reference_for(runner._global_spectrum, "Default"),
                )
        self.assertEqual(raised.exception.code, "runtime.spectrum_pressure_invalid")
        self.assertEqual(calls[0][0]["zero_pressure_peak"], 694.30)
        self.assertNotIn("temperature_correction", calls[0][0])
        self.assertEqual(calls[0][1]["baseline_model"], "auto_polynomial")
        self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)


class RubyPressurePreValidatorTests(unittest.TestCase):
    def _codes(self, settings, lakeshore=None):
        result = PreValidator().validate(
            Sequence(actions=[TakeSpectrumAction()]),
            DeviceContext(controller=PM16CControllerSim(), lakeshore=lakeshore),
            global_spectrum=settings,
        )
        return {diagnostic.code for diagnostic in result.diagnostics}

    def test_pressure_run_needs_reference_but_spectrum_only_run_does_not(self):
        self.assertIn("static.spectrum.pressure_reference_missing",
                      self._codes(pressure_settings(ruby_references=None)))
        self.assertFalse(any("pressure" in code for code in self._codes(
            pressure_settings(pressure_enabled=False, ruby_references=None))))

    def test_temperature_correction_checks_lakeshore_reading(self):
        settings = pressure_settings(temperature_correction_enabled=True,
                                     temperature_scale="ruby_datchi_2007_lt")
        self.assertIn("preflight.spectrum.temperature_unavailable", self._codes(settings))
        self.assertNotIn("preflight.spectrum.temperature_unavailable",
                         self._codes(settings, FakeLakeShore()))

    def test_stop_condition_requires_pressure_calculation(self):
        result = PreValidator().validate(
            Sequence(actions=[TakeSpectrumAction(
                stop_pressure_target_gpa=1.0, stop_pressure_tolerance_gpa=0.1,
            )]),
            DeviceContext(controller=PM16CControllerSim()),
            global_spectrum=pressure_settings(pressure_enabled=False),
        )
        self.assertIn("static.spectrum.pressure_stop_invalid",
                      {diagnostic.code for diagnostic in result.diagnostics})


class RubyPressureUiTests(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        self._app = QApplication.instance() or QApplication([])

    def _window(self):
        from apps.exp_scheduler.ui import scheduler_window
        missing = Path("__missing_exp_scheduler_test_settings__.json")
        with mock.patch.object(scheduler_window, "_SETTINGS_PATH", missing):
            window = scheduler_window.ExperimentalSchedulerWindow(DeviceContext())
        self.addCleanup(window.deleteLater)
        return window

    def test_pressure_settings_round_trip_and_widget_enabling(self):
        window = self._window()
        window._ruby_positions = {"Ruby A": {"ch3": 0, "ch4": 1, "ch5": 2}}
        window._ruby_references = {"Ruby A": {"lambda_0_nm": 694.25, "t0_k": 297.0}}
        self.assertFalse(window._pressure_scale_combo.isEnabled())
        window._pressure_enable_chk.setChecked(True)
        self.assertTrue(window._pressure_scale_combo.isEnabled())
        self.assertFalse(window._temp_scale_combo.isEnabled())
        self.assertFalse(window._manual_temp_spin.isEnabled())
        window._pressure_scale_combo.setCurrentIndex(
            window._pressure_scale_combo.findData("ruby_mao_1986"))
        window._temp_correction_chk.setChecked(True)
        self.assertTrue(window._temp_scale_combo.isEnabled())
        window._temp_scale_combo.setCurrentIndex(
            window._temp_scale_combo.findData("ruby_datchi_2007_lt"))

        data = window._spectrum_ui_to_dict()
        restored = self._window()
        restored._spectrum_dict_to_ui(data)
        built = restored._build_global_spectrum()
        self.assertTrue(built.pressure_enabled)
        self.assertEqual(built.pressure_scale, "ruby_mao_1986")
        self.assertEqual(built.temperature_scale, "ruby_datchi_2007_lt")
        self.assertEqual(built.ruby_references,
                         {"Ruby A": {"lambda_0_nm": 694.25, "t0_k": 297.0}})

    def test_old_sequence_without_pressure_keys_is_off_with_no_scale(self):
        window = self._window()
        window._spectrum_dict_to_ui({"exposure_time_s": 0.2})
        built = window._build_global_spectrum()
        self.assertFalse(built.pressure_enabled)
        self.assertEqual(built.pressure_scale, "")
        self.assertEqual(built.ruby_references, {})

    def test_clearing_positions_drops_their_references(self):
        window = self._window()
        window._ruby_positions = {"Ruby A": {"ch3": 0, "ch4": 1, "ch5": 2}}
        window._ruby_references = {"Ruby A": {"lambda_0_nm": 694.25, "t0_k": None}}
        window._clear_sample_positions()
        self.assertEqual(window._ruby_references, {})


if __name__ == "__main__":
    unittest.main()
