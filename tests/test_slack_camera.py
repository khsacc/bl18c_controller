"""Network camera command tests using local HTTP and fake Slack clients only."""
from __future__ import annotations

import base64
import io
import threading
import unittest
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.error import URLError

import cv2
import numpy as np

from settings.slack_prefs import SlackPrefs
from utils.slack_notify import camera, service


def jpeg_image():
    ok, encoded = cv2.imencode(".jpg", np.zeros((4, 6, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


class CameraFetchTests(unittest.TestCase):
    def fake_response(self, data, content_type="image/jpeg"):
        response = io.BytesIO(data)
        response.headers = Message()
        response.headers["Content-Type"] = content_type
        opener = mock.Mock()
        opener.open.return_value = response
        patcher = mock.patch.object(camera, "build_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return response

    def test_authenticated_get_bypasses_proxy_and_returns_original_jpeg(self):
        image = jpeg_image()
        requests = []
        authorisation = "Basic " + base64.b64encode(b"viewer:test-password").decode("ascii")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != authorisation:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="GeneralUser/Administrator"')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(image)))
                self.end_headers()
                self.wfile.write(image)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (f"http://127.0.0.1:{server.server_port}/SnapshotJPEG"
                   "?Resolution=640x480&Quality=Standard")
            with mock.patch.object(camera, "CAMERA_SNAPSHOT_URL", url), mock.patch.dict(
                "os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""},
            ):
                result = camera.fetch_snapshot({
                    "BL18C_CAMERA2_USERNAME": "viewer", "BL18C_CAMERA2_PASSWORD": "test-password",
                })
            self.assertEqual(result, image)
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(path == url.split(str(server.server_port), 1)[1]
                                for path, _ in requests))
            self.assertEqual(requests[0][1], None)
            self.assertEqual(requests[1][1], authorisation)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_html_empty_truncated_and_invalid_jpeg_and_closes_response(self):
        for data, content_type in (
            (b"<html>login required</html>", "text/html"),
            (b"", "image/jpeg"),
            (jpeg_image()[:-2], "image/jpeg"),
            (b"\xff\xd8invalid\xff\xd9", "image/jpeg"),
        ):
            with self.subTest(content_type=content_type, size=len(data)):
                response = self.fake_response(data, content_type)
                with self.assertRaises(camera.CameraSnapshotError):
                    camera.fetch_snapshot({})
                self.assertTrue(response.closed)

    def test_read_is_bounded(self):
        response = self.fake_response(b"x" * 100)
        with mock.patch.object(camera, "_MAX_IMAGE_BYTES", 10):
            with self.assertRaisesRegex(camera.CameraSnapshotError, "size limit"):
                camera.fetch_snapshot({})
        self.assertTrue(response.closed)

    def test_connection_failures_are_safe_and_use_short_timeout(self):
        for error in (URLError("connection refused"), TimeoutError("timeout")):
            with self.subTest(error=error):
                opener = mock.Mock()
                opener.open.side_effect = error
                with mock.patch.object(camera, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(camera.CameraSnapshotError, "timed out"):
                        camera.fetch_snapshot({})
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)

    def test_http_errors_are_actionable_without_echoing_credentials(self):
        from urllib.error import HTTPError

        for status in (401, 403, 404, 503):
            with self.subTest(status=status):
                error_body = io.BytesIO(b"private error")
                error = HTTPError(camera.CAMERA_SNAPSHOT_URL, status, "secret", {}, error_body)
                opener = mock.Mock()
                opener.open.side_effect = error
                with mock.patch.object(camera, "build_opener", return_value=opener):
                    with self.assertRaises(camera.CameraSnapshotError) as caught:
                        camera.fetch_snapshot({camera.CAMERA_PASSWORD_ENV: "secret"})
                message = str(caught.exception)
                self.assertNotIn("secret", message)
                self.assertIn("BL18C_CAMERA2_PASSWORD" if status in (401, 403) else str(status), message)
                self.assertTrue(error_body.closed)

    def test_redirect_is_refused(self):
        with self.assertRaisesRegex(camera.CameraSnapshotError, "redirected"):
            camera._NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere/")


class FakeSlack:
    def __init__(self):
        self.uploads = []
        self.posts = []
        self.fail_upload = False
        self.fail_post = False

    def files_upload_v2(self, **kwargs):
        if self.fail_upload:
            raise RuntimeError("Slack unavailable")
        self.uploads.append({**kwargs, "data": kwargs["file"].read()})

    def chat_postMessage(self, **kwargs):
        if self.fail_post:
            raise RuntimeError("Slack unavailable")
        self.posts.append(kwargs)


class CameraCommandTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSlack()
        self.now = 1000.0
        self.svc = service.SlackService(
            env={}, credentials=("", ""), start_threads=False,
            prefs=SlackPrefs(channel_id="Cnotifications"), clock=lambda: self.now,
            web_client_factory=lambda token: self.client,
        )
        self.svc._web_client = self.client
        patcher = mock.patch.object(service, "fetch_snapshot", return_value=jpeg_image())
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, text="camera", user="U1", channel="D1"):
        return self.svc.handle_slash_command(
            command="/bl18c", text=text, user_id=user, channel_id=channel)

    def process(self):
        self.svc.process_camera_upload(self.svc._camera_queue.get_nowait())

    def test_screenshots_uploads_window_and_camera_to_same_dm(self):
        from PyQt6.QtGui import QImage

        requests = []
        self.svc._request_screenshot = lambda channel, target: requests.append((channel, target))
        reply = self.ask("screenshots", user="Uanymember")
        self.assertIn("Scheduler: Screenshot requested", reply)
        self.assertIn("Camera2: Camera snapshot requested", reply)
        self.assertEqual(requests, [("D1", "window")])
        self.fetch.assert_not_called()
        self.assertIn("`screenshots`", self.ask("help"))

        image = QImage(6, 4, QImage.Format.Format_RGB32)
        image.fill(0)
        self.svc.deliver_screenshot("D1", image)
        self.svc.process_screenshot(self.svc._screenshot_queue.get_nowait())
        self.process()
        self.assertEqual([item["channel"] for item in self.client.uploads], ["D1", "D1"])
        self.assertEqual(self.client.uploads[0]["filename"], "experimental-scheduler.png")
        self.assertTrue(self.client.uploads[0]["data"].startswith(b"\x89PNG"))
        self.assertEqual(self.client.uploads[1]["data"], self.fetch.return_value)
        self.assertIsNone(self.svc._screenshot_channel)
        self.assertIsNone(self.svc._camera_channel)

    def test_screenshots_rejects_invalid_context_and_arguments_without_requests(self):
        request = self.svc._request_screenshot = mock.Mock()
        self.assertIn("did not identify", self.ask("screenshots", user=""))
        self.assertIn("direct-message", self.ask("screenshots", channel="C1"))
        self.assertIn("Usage", self.ask("screenshots pt"))
        request.assert_not_called()
        self.assertTrue(self.svc._camera_queue.empty())
        self.fetch.assert_not_called()

    def test_screenshots_camera_proceeds_when_scheduler_is_unavailable_or_busy(self):
        reply = self.ask("screenshots")
        self.assertIn("Scheduler: Scheduler screenshots are unavailable", reply)
        self.assertIn("Camera2: Camera snapshot requested", reply)
        self.process()

        self.now += 3
        self.svc._request_screenshot = mock.Mock()
        self.assertIn("requested", self.ask("screenshot", channel="Dother"))
        reply = self.ask("screenshots")
        self.assertIn("Scheduler: A screenshot request is already in progress", reply)
        self.assertIn("Camera2: Camera snapshot requested", reply)
        self.process()
        self.svc._request_screenshot.assert_called_once_with("Dother", "window")
        self.assertEqual([item["channel"] for item in self.client.uploads], ["D1", "D1"])

    def test_screenshots_scheduler_proceeds_when_camera_is_busy_or_rate_limited(self):
        request = self.svc._request_screenshot = mock.Mock()
        self.assertIn("requested", self.ask("camera", channel="Dother"))
        reply = self.ask("screenshots")
        self.assertIn("Scheduler: Screenshot requested", reply)
        self.assertIn("Camera2: A camera request is already in progress", reply)
        self.process()
        self.svc.deliver_screenshot("D1", None, "The window is closed.")
        self.svc.process_screenshot(self.svc._screenshot_queue.get_nowait())
        reply = self.ask("screenshots")
        self.assertIn("Scheduler: Screenshot requested", reply)
        self.assertIn("Camera2: Please wait at least 3 seconds", reply)
        self.assertTrue(self.svc._camera_queue.empty())
        self.assertEqual(request.call_count, 2)

    def test_screenshots_capture_failure_does_not_cancel_camera_and_vice_versa(self):
        self.svc._request_screenshot = lambda channel, target: self.svc.deliver_screenshot(
            channel, None, "The window is closed.")
        self.assertIn("Camera2: Camera snapshot requested", self.ask("screenshots"))
        self.svc.process_screenshot(self.svc._screenshot_queue.get_nowait())
        self.process()
        self.assertEqual(self.client.posts[-1], {"channel": "D1", "text": "The window is closed."})
        self.assertEqual(len(self.client.uploads), 1)

        self.now += 3
        self.svc._request_screenshot = mock.Mock()
        self.fetch.side_effect = camera.CameraSnapshotError("Camera authentication failed.")
        self.assertIn("Scheduler: Screenshot requested", self.ask("screenshots"))
        self.process()
        self.svc._request_screenshot.assert_called_once_with("D1", "window")
        self.assertEqual(self.svc._screenshot_channel, "D1")
        self.assertIsNone(self.svc._camera_channel)
        self.assertEqual(self.client.posts[-1]["channel"], "D1")
        self.assertIn("authentication failed", self.client.posts[-1]["text"])

    def test_only_identified_dm_requests_with_no_arguments_are_queued(self):
        self.assertIn("did not identify", self.ask(user=""))
        self.assertIn("direct-message", self.ask(channel="C1"))
        self.assertIn("Usage", self.ask("camera http://other-camera/"))
        self.assertTrue(self.svc._camera_queue.empty())
        self.assertIn("camera", self.ask("help"))
        self.assertIn("requested", self.ask(user="Uanymember"))
        self.assertIn("already in progress", self.ask(channel="D2"))
        self.fetch.assert_not_called()
        self.process()
        self.assertEqual(self.client.uploads[0]["channel"], "D1")
        self.assertEqual(self.client.uploads[0]["data"], self.fetch.return_value)
        self.assertTrue(self.client.uploads[0]["filename"].endswith(".jpg"))
        self.assertIsNone(self.svc._camera_channel)
        self.assertIn("3 seconds", self.ask())
        self.now += 3
        self.assertIn("requested", self.ask())

    def test_fetch_and_upload_failures_report_in_requesting_dm_and_allow_retry(self):
        self.fetch.side_effect = camera.CameraSnapshotError("Camera authentication failed.")
        self.assertIn("requested", self.ask())
        self.process()
        self.assertEqual(self.client.posts[-1], {"channel": "D1", "text": "Camera authentication failed."})
        self.assertIsNone(self.svc._camera_channel)
        self.assertEqual(self.client.uploads, [])
        self.now += 3
        self.fetch.side_effect = None
        self.client.fail_upload = True
        self.assertIn("requested", self.ask(channel="D2"))
        self.process()
        self.assertEqual(self.client.posts[-1]["channel"], "D2")
        self.assertIn("could not be sent", self.client.posts[-1]["text"])
        self.assertIsNone(self.svc._camera_channel)
        self.now += 3
        self.client.fail_post = True
        self.assertIn("requested", self.ask())
        self.process()  # Neither Slack failure may escape into the application.
        self.assertIsNone(self.svc._camera_channel)

    def test_debug_and_shutdown_do_not_contact_camera(self):
        self.svc._debug = True
        self.assertIn("unavailable", self.ask())
        self.svc._debug = False
        self.assertIn("requested", self.ask())
        self.svc.shutdown(timeout_s=0)
        self.process()
        self.assertIn("unavailable", self.ask())
        self.fetch.assert_not_called()
        self.assertEqual(self.client.uploads, [])

    def test_camera_wait_does_not_block_status_or_notifications_and_shutdown_skips_upload(self):
        started, release = threading.Event(), threading.Event()
        worker_ids = []

        def slow_fetch(env):
            worker_ids.append(threading.get_ident())
            started.set()
            release.wait(timeout=3)
            return jpeg_image()

        self.fetch.side_effect = slow_fetch
        svc = service.SlackService(
            env={"SLACK_BOT_TOKEN": "test"}, credentials=("", ""),
            prefs=SlackPrefs(), web_client_factory=lambda token: self.client,
        )
        self.addCleanup(svc.shutdown)
        self.addCleanup(release.set)
        try:
            self.assertIn("requested", svc.handle_slash_command(
                command="/bl18c", text="camera", user_id="U1", channel_id="D1"))
            self.assertTrue(started.wait(timeout=2))
            self.assertNotEqual(worker_ids, [threading.get_ident()])
            self.assertIn("No sequence", svc.handle_slash_command(
                command="/bl18c", text="status", user_id="U1", channel_id="D1"))
            svc._put(("post", "Cnotifications", "still responsive"))
            svc.shutdown(timeout_s=0.1)
            self.assertEqual(self.client.posts[0]["text"], "still responsive")
        finally:
            release.set()
            svc._camera_sender.join(timeout=2)
        self.assertFalse(svc._camera_sender.is_alive())
        self.assertIsNone(svc._camera_channel)
        self.assertEqual(self.client.uploads, [])


if __name__ == "__main__":
    unittest.main()
