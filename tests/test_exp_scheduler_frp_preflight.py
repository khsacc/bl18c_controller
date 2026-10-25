"""Hardware-free checks for the scheduler's live FRP preflight."""
from __future__ import annotations

import io
import json
import math
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from apps.exp_scheduler.actions import TakeSpectrumAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler import validation_service
from apps.exp_scheduler.validator.execution_trace import ExecutionTrace
from apps.exp_scheduler.validator.pre_validator import PreCheckResult, _check_spectrum
from utils.fluora_pressee import FluoraPresseeReader
from utils.stage.control_stage_sim import PM16CControllerSim
from tests.exp_scheduler_fakes import FakeStageController


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FrpPreflightTests(unittest.TestCase):
    def setUp(self):
        self.settings = GlobalSpectrumSettings(
            api_key="test-secret", offset_ch4_pulse=0, offset_ch5_pulse=0,
        )
        self.status = {
            "busy": False, "camera_connected": True,
            "instrument_state_token": "state-1", "roi": {"mode": "1d_roi"},
        }
        self.gain = {
            "exists": True, "available": True, "gain": 2,
            "minimum": 1, "maximum": 1000, "increment": 1,
        }
        self.spectrum = {
            "mode": "1d", "x": [690.0], "y": [2.0],
            "instrument_state_token": "state-3",
        }

    def _check(self, responses, *, action=None, settings=None, controller=None):
        requests = []
        replies = iter(responses)

        def opener(request, timeout):
            requests.append((request, timeout))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return _Response(reply)

        def reader_factory(*args, **kwargs):
            return FluoraPresseeReader(*args, opener=opener, **kwargs)

        result = PreCheckResult()
        trace = ExecutionTrace.build([action or TakeSpectrumAction()])
        with patch("utils.fluora_pressee.FluoraPresseeReader", reader_factory):
            _check_spectrum(
                trace, DeviceContext(controller=controller or object()),
                settings or self.settings, result,
            )
        return {d.code for d in result.diagnostics}, requests

    def test_success_checks_status_after_gain_then_acquires_once(self):
        codes, requests = self._check([self.status, self.gain, self.status, self.spectrum])
        self.assertEqual(codes, set())
        self.assertEqual(
            [(request.get_method(), request.full_url.rsplit("/", 1)[-1])
             for request, _ in requests],
            [("GET", "status"), ("GET", "em-gain"),
             ("GET", "status"), ("POST", "acquire")],
        )
        payload = json.loads(requests[-1][0].data)
        self.assertEqual(payload, {
            "dark": {"mode": "none"}, "expected_state_token": "state-1",
            "exposure_time_s": 0.1, "accumulations": 1,
        })
        self.assertNotIn("test-secret", str(payload))

    def test_token_change_during_gain_read_blocks_acquisition(self):
        refreshed = {**self.status, "instrument_state_token": "state-2"}
        codes, requests = self._check([self.status, self.gain, refreshed])
        self.assertIn("preflight.spectrum.state_changed_during_probe", codes)
        self.assertEqual(len(requests), 3)

    def test_busy_status_stops_before_acquisition(self):
        codes, requests = self._check([{**self.status, "busy": True}])
        self.assertIn("preflight.spectrum.not_ready", codes)
        self.assertEqual(len(requests), 1)

    def test_busy_acquisition_after_ready_status_is_an_error(self):
        busy = HTTPError(
            "http://frp/acquire", 409, "Conflict", {},
            io.BytesIO(b'{"detail":"camera busy"}'),
        )
        codes, requests = self._check([self.status, self.gain, self.status, busy])
        self.assertIn("preflight.spectrum.test_acquisition_failed", codes)
        self.assertEqual(len(requests), 4)

    def test_acquisition_without_updated_token_is_an_error(self):
        response = {"mode": "1d", "x": [690.0], "y": [2.0]}
        codes, requests = self._check([self.status, self.gain, self.status, response])
        self.assertIn("preflight.spectrum.test_acquisition_failed", codes)
        self.assertEqual(len(requests), 4)

    def test_missing_state_token_stops_before_acquisition(self):
        status = {**self.status, "instrument_state_token": ""}
        codes, requests = self._check([status, self.gain, status])
        self.assertIn("preflight.spectrum.state_token_missing", codes)
        self.assertEqual(len(requests), 3)

    def test_initially_missing_token_requires_revalidation(self):
        initial = {**self.status, "instrument_state_token": None}
        codes, requests = self._check([initial, self.gain, self.status])
        self.assertIn("preflight.spectrum.state_token_missing", codes)
        self.assertEqual(len(requests), 3)

    def test_gain_limit_uses_per_step_override(self):
        action = TakeSpectrumAction(em_gain_max=1)
        codes, requests = self._check([self.status, self.gain, self.status], action=action)
        self.assertIn("preflight.spectrum.gain_above_maximum", codes)
        self.assertEqual(len(requests), 3)

    def test_gain_read_failure_stops_before_acquisition(self):
        unavailable = HTTPError(
            "http://frp/hardware/camera/em-gain", 503, "Unavailable", {},
            io.BytesIO(b'{"detail":"camera offline"}'),
        )
        codes, requests = self._check([self.status, unavailable])
        self.assertIn("preflight.spectrum.gain_unreadable", codes)
        self.assertEqual(len(requests), 2)

    def test_unavailable_gain_still_allows_trial_acquisition(self):
        gain = {"exists": False, "available": False}
        codes, requests = self._check([self.status, gain, self.status, self.spectrum])
        self.assertNotIn("preflight.spectrum.gain_unreadable", codes)
        self.assertEqual(len(requests), 4)

    def test_pressure_requires_explicit_wavelength_unit(self):
        self.settings.pressure_enabled = True
        self.settings.pressure_scale = "ruby_shen_2020"
        self.settings.ruby_references = {"Default": {"lambda_0_nm": 694.3}}
        status = {**self.status, "calibration": {"applied": True},
                  "configuration": {"axis_mode": "calibrated"}}
        codes, requests = self._check([status, self.gain, status])
        self.assertIn("preflight.spectrum.pressure_not_ready", codes)
        self.assertEqual(len(requests), 3)

    def test_plain_spectrum_requires_1d_mode(self):
        status = {**self.status, "roi": {"mode": "2d"}}
        codes, requests = self._check([status, self.gain, status])
        self.assertIn("preflight.spectrum.mode_not_ready", codes)
        self.assertEqual(len(requests), 3)

    def test_non_finite_and_non_numeric_timing_is_static_error(self):
        self.settings.timeout_s = math.nan
        self.settings.settle_ms = "100"
        codes, requests = self._check([])
        self.assertIn("static.spectrum.timing_invalid", codes)
        self.assertEqual(requests, [])

    def test_simulation_does_not_contact_frp(self):
        codes, requests = self._check([], controller=PM16CControllerSim())
        self.assertEqual(requests, [])
        self.assertNotIn("preflight.spectrum.not_ready", codes)

    def test_validate_and_run_gate_each_take_one_test_spectrum(self):
        calls = []
        replies = iter([self.status, self.gain, self.status, self.spectrum] * 2)

        def opener(request, timeout):
            calls.append(request.get_method() + " " + request.full_url)
            return _Response(next(replies))

        def reader_factory(*args, **kwargs):
            return FluoraPresseeReader(*args, opener=opener, **kwargs)

        sequence = Sequence(actions=[TakeSpectrumAction()])
        context = DeviceContext(controller=FakeStageController())
        with patch("utils.fluora_pressee.FluoraPresseeReader", reader_factory):
            validation_service.validate_sequence(
                sequence, context, global_spectrum=self.settings,
            )
            validation_service.revalidate_for_run(
                sequence, context, global_spectrum=self.settings,
            )
        self.assertEqual(sum(call.endswith("POST " + self.settings.base_url + "/acquire")
                             for call in calls), 2)


if __name__ == "__main__":
    unittest.main()
