import io
import json
import unittest
from contextlib import contextmanager
from urllib.error import HTTPError

import numpy as np

from apps.ruby_finder.ruby_finder_backend import (
    FluoraPresseeError, FluoraPresseeReader, build_spectrum_cube,
)
from apps.scan2d.free_2d_scan_backend import Free2DScanWorker


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class FluoraPresseeReaderTests(unittest.TestCase):
    def test_optional_retention_keeps_spectrum_and_axis(self):
        reader = FluoraPresseeReader(
            api_key="secret",
            retain_spectra=True,
            opener=lambda request, timeout: _Response(
                {"mode": "1d", "x": [690.0, 691.0], "y": [2.0, 3.0]}
            ),
        )
        self.assertEqual(reader.read_transmitted(), 5.0)
        self.assertEqual(reader.spectrum_x.tolist(), [690.0, 691.0])
        self.assertEqual(reader.retained_spectra[0].tolist(), [2.0, 3.0])

    def test_retention_rejects_changed_spectral_axis(self):
        responses = iter([
            {"mode": "1d", "x": [1.0, 2.0], "y": [3.0, 4.0]},
            {"mode": "1d", "x": [1.0, 2.1], "y": [3.0, 4.0]},
        ])
        reader = FluoraPresseeReader(
            api_key="secret", retain_spectra=True,
            opener=lambda request, timeout: _Response(next(responses)),
        )
        reader.read_transmitted()
        with self.assertRaisesRegex(FluoraPresseeError, "x axis changed"):
            reader.read_transmitted()

    def test_prepare_checks_status_without_moving_or_acquiring(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return _Response({"busy": False, "camera_connected": True})

        reader = FluoraPresseeReader(api_key="secret", opener=opener)
        reader.prepare()

        request, timeout = requests[0]
        self.assertEqual(request.full_url, "http://192.168.1.102:8765/status")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("X-api-key"), "secret")
        self.assertEqual(timeout, 120.0)

    def test_prepare_rejects_busy_server(self):
        reader = FluoraPresseeReader(
            api_key="secret",
            opener=lambda request, timeout: _Response(
                {"busy": True, "camera_connected": True}
            ),
        )
        with self.assertRaisesRegex(FluoraPresseeError, "busy"):
            reader.prepare()

    def test_acquire_sums_all_processed_spectrum_points(self):
        captured = []

        def opener(request, timeout):
            captured.append(request)
            return _Response(
                {
                    "mode": "1d",
                    "y": [1.5, 2.0, 3.5],
                    "exposure_time_s": 0.25,
                    "accumulations": 3,
                }
            )

        reader = FluoraPresseeReader(api_key="secret", opener=opener)
        self.assertEqual(reader.read_transmitted(), 7.0)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(
            json.loads(captured[0].data.decode("utf-8")),
            {"dark": {"mode": "none"}},
        )
        self.assertEqual(reader.public_metadata()["exposure_time_s"], 0.25)

    def test_acquire_with_two_moffat_fit_uses_fit_endpoint(self):
        captured = []
        response = {
            "mode": "1d", "x": [690.0, 691.0],
            "y_raw": [10.0, 20.0], "y": [10.0, 20.0],
            "fit": {"success": False, "fit": None},
        }

        def opener(request, timeout):
            captured.append(request)
            return _Response(response)

        reader = FluoraPresseeReader(api_key="secret", opener=opener)
        result = reader.acquire_spectrum_with_fit(
            fit_function="Moffat", fit_peak_count=2)

        self.assertEqual(result, response)
        self.assertEqual(captured[0].full_url,
                         "http://192.168.1.102:8765/acquire/fit")
        payload = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(payload["fit_function"], "Moffat")
        self.assertEqual(payload["fit_peak_count"], 2)
        self.assertEqual(payload["dark"], {"mode": "none"})

    def test_prepare_locks_instrument_state_for_following_acquisitions(self):
        responses = iter(
            [
                {
                    "busy": False,
                    "camera_connected": True,
                    "instrument_state_token": "opaque:14",
                },
                {"mode": "1d", "y": [2.0, 3.0]},
            ]
        )
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return _Response(next(responses))

        reader = FluoraPresseeReader(api_key="secret", opener=opener)
        reader.prepare()
        self.assertEqual(reader.read_transmitted(), 5.0)
        self.assertEqual(
            json.loads(requests[1].data.decode("utf-8"))["expected_state_token"],
            "opaque:14",
        )

    def test_acquire_rejects_2d_detector_data(self):
        reader = FluoraPresseeReader(
            api_key="secret",
            opener=lambda request, timeout: _Response(
                {"mode": "2d", "y": [[1.0, 2.0]]}
            ),
        )
        with self.assertRaisesRegex(FluoraPresseeError, "1-D"):
            reader.read_transmitted()

    def test_http_error_includes_frp_detail(self):
        def opener(request, timeout):
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"detail":"Invalid API key"}'),
            )

        reader = FluoraPresseeReader(api_key="wrong", opener=opener)
        with self.assertRaisesRegex(FluoraPresseeError, "Invalid API key"):
            reader.prepare()

    def test_api_key_is_not_exposed_in_metadata(self):
        reader = FluoraPresseeReader(api_key="top-secret")
        self.assertNotIn("top-secret", repr(reader.public_metadata()))


class ReaderPreparationSafetyTests(unittest.TestCase):
    def test_failed_prepare_does_not_acquire_motion_lease(self):
        class Reader:
            def prepare(self):
                raise FluoraPresseeError("authentication failed")

        class Controller:
            def motion_session(self, **kwargs):
                raise AssertionError("motion session must not be requested")

        failures = []
        worker = Free2DScanWorker(
            Controller(), Reader(), 4, 5, [1], [2], 1, 2,
        )
        worker.scan_could_not_start.connect(failures.append)
        worker.run()
        self.assertEqual(failures, ["authentication failed"])

    def test_reader_without_prepare_remains_compatible(self):
        class Coordinator:
            @staticmethod
            def is_valid(motion):
                return False

        class Controller:
            coordinator = Coordinator()

            @contextmanager
            def motion_session(self, **kwargs):
                yield object()

            def set_ch_speed(self, *args, **kwargs):
                pass

            def move_ch_absolute(self, *args, **kwargs):
                pass

            def wait_until_stop(self, *args, **kwargs):
                pass

        class LegacyReader:
            def set_current_position(self, x, y):
                pass

            def read_transmitted(self):
                return 4.0

        completed = []
        worker = Free2DScanWorker(
            Controller(), LegacyReader(), 4, 5, [1], [2], 1, 2,
            settle_ms=0,
        )
        worker.scan_completed.connect(lambda: completed.append(True))
        worker.run()
        self.assertEqual(completed, [True])


class SpectrumCubeTests(unittest.TestCase):
    def test_averages_reads_and_preserves_grid_order(self):
        spectra = [
            np.array([1.0, 3.0]), np.array([3.0, 5.0]),
            np.array([10.0, 20.0]), np.array([14.0, 24.0]),
        ]
        cube = build_spectrum_cube(
            spectra, n_y=2, n_x=2, reads_per_point=2,
            measured_mask=np.array([[True, True], [False, False]]),
        )
        np.testing.assert_allclose(cube[0, 0], [2.0, 4.0])
        np.testing.assert_allclose(cube[0, 1], [12.0, 22.0])
        self.assertTrue(np.all(np.isnan(cube[1])))


if __name__ == "__main__":
    unittest.main()
