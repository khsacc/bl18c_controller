"""Slack notifications: settings, error grouping, sending, slash commands,
and the runner's end-of-run signals the notifications rely on."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from apps.exp_scheduler.actions import WaitAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner, _StopRequested
from apps.exp_scheduler.sequence import Sequence
from settings import slack_prefs
from settings.slack_prefs import SlackPrefs
from utils.slack_notify import service as service_module
from utils.slack_notify.service import SlackService, SlackUnavailable
from utils.slack_notify.status import PlannedStep, format_progress, format_steps
from utils.slack_notify.throttle import ErrorAggregator, error_key

ENV = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}
_QT_APP = None


def qt_app():
    """Keep QApplication alive through unittest cleanup on Windows."""
    global _QT_APP
    from PyQt6.QtWidgets import QApplication
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def setUpModule():
    # The tests use fake clients, so they must not depend on slack_sdk being installed.
    patcher = mock.patch.object(service_module, "_slack_sdk_available", return_value=True)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeWebClient:
    def __init__(self, fail: Exception | None = None, pages=None) -> None:
        self.posts: list[dict] = []
        self.uploads: list[dict] = []
        self.fail = fail
        self.upload_fail: Exception | None = None
        self.pages = pages or []

    def chat_postMessage(self, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.posts.append(kwargs)
        return {"ok": True}

    def conversations_list(self, **kwargs):
        index = int(kwargs.get("cursor") or 0)
        return self.pages[index]

    def files_upload_v2(self, **kwargs):
        if self.upload_fail is not None:
            raise self.upload_fail
        kwargs["data"] = kwargs["file"].read()
        self.uploads.append(kwargs)
        return {"ok": True}


def make_service(prefs: SlackPrefs | None = None, *, debug=False, client=None, env=ENV,
                 credentials=("", "")):
    client = client or FakeWebClient()
    clock = FakeClock()
    svc = SlackService(
        debug=debug, env=env, credentials=credentials, start_threads=False, clock=clock,
        prefs=prefs or SlackPrefs(enabled=True, channel_id="C1", channel_name="beamline",
                                  allowed_user_ids=["U111"]),
        web_client_factory=lambda token: client,
    )
    svc._MIN_WAIT_PATCH = mock.patch.object(service_module, "_MIN_POST_INTERVAL_S", 0)
    svc._MIN_WAIT_PATCH.start()
    return svc, client, clock


def drain(svc: SlackService) -> None:
    while not svc._queue.empty():
        svc.process(svc._queue.get_nowait())


class SlackPrefsTests(unittest.TestCase):
    def test_round_trip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slack_settings.json"
            with mock.patch.object(slack_prefs, "_PREFS_FILE", path):
                self.assertEqual(slack_prefs.load(), SlackPrefs())
                prefs = SlackPrefs(enabled=True, channel_id="C9", channel_name="xrd",
                                   error_suppress_minutes=3, slash_commands_enabled=True,
                                   allowed_user_ids=["U1AB"])
                slack_prefs.save(prefs)
                self.assertEqual(slack_prefs.load(), prefs)
                path.write_text('{"enabled": "yes", "error_suppress_minutes": -1}')
                self.assertEqual(slack_prefs.load(), SlackPrefs())

    def test_credentials_round_trip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slack_credentials.json"
            with mock.patch.object(slack_prefs, "_CREDENTIALS_FILE", path):
                self.assertEqual(slack_prefs.load_credentials(), ("", ""))
                slack_prefs.save_credentials(" xoxb-1-2 ", "")
                self.assertEqual(slack_prefs.load_credentials(), ("xoxb-1-2", ""))
                with self.assertRaises(ValueError):
                    slack_prefs.save_credentials("xapp-wrong-kind", "")
                with self.assertRaises(ValueError):
                    slack_prefs.save_credentials("xoxb-1", "xoxb-not-app")
                path.write_text('{"bot_token": "garbage"}')
                self.assertEqual(slack_prefs.load_credentials(), ("", ""))

    def test_parse_user_ids(self):
        self.assertEqual(slack_prefs.parse_user_ids(" u01abc, U01ABC  W02XYZ;"),
                         ["U01ABC", "W02XYZ"])
        with self.assertRaises(ValueError):
            slack_prefs.parse_user_ids("@hiroki")


class ErrorAggregatorTests(unittest.TestCase):
    def test_repeats_are_grouped_into_one_summary_per_window(self):
        agg = ErrorAggregator(window_s=600)
        key = error_key("runtime.x", "Ch4 position 1234 unreadable")
        self.assertEqual(agg.submit(key, "first", now=0), "first")
        # Numbers are ignored, so this is the same error.
        key2 = error_key("runtime.x", "Ch4 position 9999 unreadable")
        self.assertEqual(key, key2)
        for t in (10, 20, 30):
            self.assertIsNone(agg.submit(key2, f"repeat {t}", now=t))
        self.assertEqual(agg.due(now=599), [])
        summaries = agg.due(now=600)
        self.assertEqual(len(summaries), 1)
        self.assertIn("3 more time(s)", summaries[0])
        self.assertIn("repeat 30", summaries[0])
        # Next window has no repeats → forgotten, and a new error sends again.
        self.assertEqual(agg.due(now=1200), [])
        self.assertFalse(agg.pending())
        self.assertEqual(agg.submit(key, "again", now=1300), "again")

    def test_different_errors_are_not_suppressed(self):
        agg = ErrorAggregator(window_s=600)
        self.assertIsNotNone(agg.submit(error_key("a", "x"), "a", now=0))
        self.assertIsNotNone(agg.submit(error_key("b", "x"), "b", now=1))

    def test_zero_window_never_suppresses(self):
        agg = ErrorAggregator(window_s=0)
        key = error_key("a", "x")
        self.assertEqual(agg.submit(key, "1", now=0), "1")
        self.assertEqual(agg.submit(key, "2", now=0), "2")


class SlackServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.client, self.clock = make_service()
        self.addCleanup(self.svc._MIN_WAIT_PATCH.stop)
        self.svc.status_board.begin_run("Compression run", [
            PlannedStep(0, "Wait 1 s"), PlannedStep(1, "Take XRD", "P=1 (1/3)"),
        ])

    def test_completed_is_posted_to_selected_channel(self):
        self.svc.status_board.finish("completed")
        self.svc.notify_completed()
        drain(self.svc)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.posts[0]["channel"], "C1")
        self.assertIn("sequence completed", self.client.posts[0]["text"])
        self.assertIn("Compression run", self.client.posts[0]["text"])

    def test_identical_errors_are_grouped(self):
        self.svc.status_board.step_started(1, "Take XRD")
        for _ in range(5):
            self.svc.notify_error("runtime.unexpected_error", "Detector timeout <x>")
        drain(self.svc)
        self.assertEqual(len(self.client.posts), 1)
        self.assertIn("Detector timeout &lt;x&gt;", self.client.posts[0]["text"])
        self.clock.now += 10 * 60
        self.svc.process(None)  # periodic tick
        self.assertEqual(len(self.client.posts), 2)
        self.assertIn("4 more time(s)", self.client.posts[1]["text"])

    def test_switches_and_debug_block_sending(self):
        self.svc.update_prefs(SlackPrefs(enabled=True, channel_id="C1", notify_on_completion=False))
        self.svc.notify_completed()
        drain(self.svc)
        self.assertEqual(self.client.posts, [])

        debug_svc, debug_client, _ = make_service(debug=True)
        self.addCleanup(debug_svc._MIN_WAIT_PATCH.stop)
        debug_svc.notify_error("x", "boom")
        drain(debug_svc)
        self.assertEqual(debug_client.posts, [])

        no_token, no_token_client, _ = make_service(env={})
        self.addCleanup(no_token._MIN_WAIT_PATCH.stop)
        no_token.notify_completed()
        drain(no_token)
        self.assertEqual(no_token_client.posts, [])

    def test_send_failure_never_raises(self):
        svc, client, _ = make_service(client=FakeWebClient(fail=RuntimeError("network down")))
        self.addCleanup(svc._MIN_WAIT_PATCH.stop)
        svc.notify_completed()
        drain(svc)  # must not raise
        self.assertIn("network down", svc.status_text())

    def test_queue_is_bounded(self):
        for i in range(service_module._QUEUE_SIZE + 20):
            self.svc.notify_error("code", f"distinct error {chr(65 + i % 26)}{i}")
        self.assertEqual(self.svc._queue.qsize(), service_module._QUEUE_SIZE)

    def test_channel_list_follows_pagination(self):
        client = FakeWebClient(pages=[
            {"channels": [{"id": "C2", "name": "zeta", "is_member": True}],
             "response_metadata": {"next_cursor": "1"}},
            {"channels": [{"id": "C3", "name": "alpha"}], "response_metadata": {"next_cursor": ""}},
        ])
        svc, _, _ = make_service(client=client)
        self.addCleanup(svc._MIN_WAIT_PATCH.stop)
        self.assertEqual(svc.list_public_channels(), [
            {"id": "C3", "name": "alpha", "is_member": False},
            {"id": "C2", "name": "zeta", "is_member": True},
        ])
        svc_no_token, _, _ = make_service(env={})
        self.addCleanup(svc_no_token._MIN_WAIT_PATCH.stop)
        with self.assertRaises(SlackUnavailable):
            svc_no_token.list_public_channels()


class TokenClient:
    """Fake WebClient that remembers which token created it."""

    def __init__(self, token, scopes="chat:write,chat:write.public,channels:read,commands,files:write",
                 app_error=None):
        self.token, self.scopes, self.app_error = token, scopes, app_error

    def auth_test(self):
        if not self.token.startswith("xoxb-good"):
            raise RuntimeError("invalid_auth")
        response = mock.MagicMock()
        response.get = {"team": "KEK", "user": "bl18c_bot"}.get
        response.headers = {"X-OAuth-Scopes": self.scopes}
        return response

    def apps_connections_open(self, app_token):
        if self.app_error:
            raise RuntimeError(self.app_error)
        return {"ok": True, "url": "wss://example"}


class CredentialTests(unittest.TestCase):
    def test_saved_token_wins_over_environment_and_applies_live(self):
        created = []

        def factory(token):
            created.append(token)
            return FakeWebClient()

        svc = SlackService(env=ENV, credentials=("xoxb-saved", ""), start_threads=False,
                           prefs=SlackPrefs(), web_client_factory=factory)
        self.assertEqual(svc.bot_token(), "xoxb-saved")
        self.assertEqual(svc.token_source("bot"), "settings")
        self.assertEqual(svc.app_token(), "xapp-test")
        self.assertIn("SLACK_APP_TOKEN", svc.token_source("app"))
        svc._client()
        svc.update_credentials("xoxb-new", "")
        svc._client()
        self.assertEqual(created, ["xoxb-saved", "xoxb-new"])
        svc.update_credentials("", "")
        self.assertEqual(svc.bot_token(), "xoxb-test")   # back to the env var
        empty = SlackService(env={}, credentials=("", ""), start_threads=False, prefs=SlackPrefs())
        self.assertEqual(empty.token_source("bot"), "not set")
        self.assertIn("not set", empty.unavailable_reason())

    def test_check_credentials_reports_scopes_and_tokens(self):
        svc = SlackService(env={}, credentials=("", ""), start_threads=False,
                           prefs=SlackPrefs(slash_commands_enabled=True),
                           web_client_factory=lambda token: TokenClient(token))
        results = svc.check_credentials("xoxb-good", "xapp-1")
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertIn("KEK", results[0][1])

        svc._web_client_factory = lambda token: TokenClient(token, scopes="chat:write")
        results = svc.check_credentials("xoxb-good", "")
        failed = [message for ok, message in results if not ok]
        self.assertTrue(any("chat:write.public" in m and "channels:read" in m for m in failed))
        self.assertTrue(any("App-Level Token: not set" in m for m in failed))

        svc._web_client_factory = lambda token: TokenClient(token, app_error="socket mode off")
        results = svc.check_credentials("xoxb-bad", "xapp-1")
        self.assertFalse(results[0][0])
        self.assertFalse(results[-1][0])


class OnOffSwitchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(slack_prefs, "_PREFS_FILE", Path(tmp.name) / "s.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_setup_problem_and_saved_switch(self):
        svc, client, _ = make_service(SlackPrefs(enabled=False, channel_id=""))
        self.addCleanup(svc._MIN_WAIT_PATCH.stop)
        self.assertIn("channel", svc.setup_problem())
        no_token, _, _ = make_service(env={})
        self.addCleanup(no_token._MIN_WAIT_PATCH.stop)
        self.assertIn("not set", no_token.setup_problem())

        svc.update_prefs(SlackPrefs(enabled=False, channel_id="C1"))
        self.assertEqual(svc.setup_problem(), "")
        calls = []
        svc.add_listener(lambda: calls.append(svc.prefs.enabled))
        svc.notify_completed()
        drain(svc)
        self.assertEqual(client.posts, [])          # switched off → nothing sent
        svc.set_enabled(True)
        self.assertEqual(calls, [True])
        self.assertTrue(slack_prefs.load().enabled)  # persisted
        svc.notify_completed()
        drain(svc)
        self.assertEqual(len(client.posts), 1)

    def test_scheduler_checkbox_is_optional(self):
        from types import SimpleNamespace
        from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
        app = qt_app()
        self.assertIsNotNone(app)

        def window(slack):
            w = ExperimentalSchedulerWindow(DeviceContext(),
                                            main_window=SimpleNamespace(slack_service=slack, _open_windows={}))
            self.addCleanup(w.close)
            return w

        # Without the launcher (no service) the checkbox is hidden.
        self.assertFalse(ExperimentalSchedulerWindow(DeviceContext())._slack_chk.isVisibleTo(None))

        not_set_up, _, _ = make_service(SlackPrefs(enabled=True, channel_id=""))
        self.addCleanup(not_set_up._MIN_WAIT_PATCH.stop)
        w = window(not_set_up)
        self.assertFalse(w._slack_chk.isEnabled())
        self.assertFalse(w._slack_chk.isChecked())
        self.assertIn("not set up", w._slack_chk.text())
        # Run stays governed by validation only; Slack never blocks it.
        w._slack_call(lambda slack: (_ for _ in ()).throw(RuntimeError("slack broken")))

        ready, _, _ = make_service(SlackPrefs(enabled=False, channel_id="C1", channel_name="xrd"))
        self.addCleanup(ready._MIN_WAIT_PATCH.stop)
        w = window(ready)
        self.assertTrue(w._slack_chk.isEnabled())
        self.assertFalse(w._slack_chk.isChecked())
        w._slack_chk.setChecked(True)
        self.assertTrue(ready.prefs.enabled)
        ready.update_prefs(SlackPrefs(enabled=False, channel_id="C1"))   # e.g. from Settings
        self.assertFalse(w._slack_chk.isChecked())
        w.close()
        self.assertEqual(ready._listeners, [])


class SlashCommandTests(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = make_service()
        self.addCleanup(self.svc._MIN_WAIT_PATCH.stop)

    def ask(self, text="", user="U111", channel="D123"):
        return self.svc.handle_slash_command(command="/bl18c", text=text, user_id=user,
                                             channel_id=channel)

    def test_only_allowed_users_in_dm(self):
        self.assertIn("not allowed", self.ask(user="U999"))
        self.assertIn("U999", self.ask(user="U999"))
        self.assertIn("direct-message", self.ask(channel="C1"))
        self.assertIn("No sequence has been run", self.ask())

    def test_status_progress_steps(self):
        board = self.svc.status_board
        board.begin_run("Run A", [PlannedStep(i, f"Step {i}") for i in range(4)])
        board.step_started(0, "Step 0")
        board.step_completed(0)
        board.step_started(1, "Step 1")
        board.progress("Waiting for pressure")
        self.assertIn("Running:* Run A", self.ask("status"))
        self.assertIn("Step 2/4: Step 1", self.ask())
        progress = self.ask("progress")
        self.assertIn("1/4 steps (25 %)", progress)
        self.assertIn("Waiting for pressure", progress)
        steps = self.ask("steps")
        self.assertIn(":white_check_mark: 1. Step 0", steps)
        self.assertIn(":arrow_forward: 2. Step 1", steps)
        self.assertIn("Usage", self.ask("what"))
        board.finish("error", "boom")
        self.assertIn("No sequence is running", self.ask())
        self.assertIn("Stopped by an error", self.ask())

    def test_long_step_lists_are_windowed(self):
        board = self.svc.status_board
        board.begin_run("Long", [PlannedStep(i, f"S{i}") for i in range(200)])
        board.step_started(100, "S100")
        text = format_steps(board.snapshot(), limit=20)
        self.assertIn("… 95 earlier step(s)", text)
        self.assertIn("101. S100", text)
        self.assertIn("more step(s)", text)
        self.assertIn("0/200", format_progress(board.snapshot()))

    def test_instrument_readings_and_disconnection(self):
        self.svc._lakeshore_readings = lambda: None
        self.svc._pace5000_readings = lambda: {"connected": False}
        self.assertIn("not connected", self.ask("lakeshore"))
        self.assertIn("not connected", self.ask("pace5000"))

        self.svc._lakeshore_readings = lambda: {
            "connected": True, "target_setpoint_k": 250.0,
            "temperature_a_k": 249.1, "temperature_b_k": 248.2,
            "ramp_enabled": True, "ramp_rate_k_per_min": 2.5,
            "heater_range": "MEDIUM",
        }
        self.svc._pace5000_readings = lambda: {
            "connected": True, "target_pressure_mpa": 1.5,
            "pressure_mpa": 1.25, "slew_mpa_per_sec": 0.02,
        }
        lake = self.ask("LakeShore")
        self.assertIn("Tset: 250 K", lake)
        self.assertIn("TA: 249.1 K", lake)
        self.assertIn("TB: 248.2 K", lake)
        self.assertIn("Ramp: 2.5 K/min", lake)
        self.assertIn("Heater range: MEDIUM", lake)
        pace = self.ask("pace5000")
        self.assertIn("Target: 1.5 MPa", pace)
        self.assertIn("Current pressure: 1.25 MPa", pace)
        self.assertIn("Ramp: 1.2 MPa/min", pace)
        self.assertIn("lakeshore", self.ask("help"))
        self.assertIn("pace5000", self.ask("help"))

    def test_instrument_cache_is_not_read_for_denied_commands(self):
        provider = mock.Mock(side_effect=AssertionError("cache must not be read"))
        self.svc._lakeshore_readings = provider
        self.assertIn("not allowed", self.ask("lakeshore", user="U999"))
        self.assertIn("direct-message", self.ask("lakeshore", channel="C1"))
        provider.assert_not_called()

    def test_instrument_missing_reading_and_provider_error(self):
        self.svc._lakeshore_readings = lambda: {
            "connected": True, "ramp_enabled": False,
            "ramp_rate_k_per_min": 2.5, "heater_range": "OFF",
        }
        self.assertIn("Tset: unavailable", self.ask("lakeshore"))
        self.assertIn("Ramp: Off (configured 2.5 K/min)", self.ask("lakeshore"))
        self.svc._pace5000_readings = lambda: {"connected": True}
        self.assertIn("Ramp: unavailable", self.ask("pace5000"))
        self.svc._pace5000_readings = mock.Mock(side_effect=RuntimeError("cache failed"))
        self.assertIn("readings are unavailable", self.ask("pace5000"))

    def test_screenshot_authorisation_busy_and_upload_are_isolated(self):
        from PyQt6.QtGui import QImage
        requests = []
        self.svc._request_screenshot = requests.append
        self.assertIn("not allowed", self.ask("screenshot", user="U999"))
        self.assertIn("direct-message", self.ask("screenshot", channel="C1"))
        self.assertEqual(requests, [])
        self.assertIn("requested", self.ask("screenshot"))
        self.assertEqual(requests, ["D123"])
        self.assertIn("already in progress", self.ask("screenshot"))
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(0x123456)
        self.svc.deliver_screenshot("D123", image)
        item = self.svc._screenshot_queue.get_nowait()
        client = self.svc._client()
        self.svc.process_screenshot(item)
        self.assertEqual(client.uploads[0]["channel"], "D123")
        self.assertTrue(client.uploads[0]["data"].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(client.posts, [])
        self.assertIn("requested", self.ask("screenshot"))

    def test_screenshot_failure_does_not_raise_or_post_to_notification_channel(self):
        self.svc._request_screenshot = lambda channel: None
        self.assertIn("requested", self.ask("screenshot"))
        self.svc.deliver_screenshot("D123", None, "The window is closed.")
        self.svc.process_screenshot(self.svc._screenshot_queue.get_nowait())
        client = self.svc._client()
        self.assertEqual(client.posts[-1]["channel"], "D123")
        self.assertIn("closed", client.posts[-1]["text"])
        self.assertIn("requested", self.ask("screenshot"))
        from PyQt6.QtGui import QImage
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(0)
        client.upload_fail = RuntimeError("network down")
        self.svc.deliver_screenshot("D123", image)
        self.svc.process_screenshot(self.svc._screenshot_queue.get_nowait())
        self.assertEqual(client.posts[-1]["channel"], "D123")
        self.assertIn("could not be sent", client.posts[-1]["text"])

    def test_screenshot_bridge_uses_queued_gui_capture(self):
        from PyQt6.QtWidgets import QWidget
        from utils.slack_notify.screenshot import SchedulerScreenshotBridge
        app = qt_app()
        window = QWidget()
        window.resize(80, 40)
        window.show()
        self.addCleanup(window.close)
        delivered = []
        bridge = SchedulerScreenshotBridge(lambda: window,
                                           lambda channel, image, error: delivered.append((channel, image, error)))
        bridge.request("D123")
        self.assertEqual(delivered, [])
        app.processEvents()
        self.assertEqual(delivered[0][0], "D123")
        self.assertFalse(delivered[0][1].isNull())
        self.assertEqual(delivered[0][2], "")
        window.hide()
        bridge.request("D123")
        app.processEvents()
        self.assertIsNone(delivered[1][1])
        self.assertIn("not open", delivered[1][2])

    def test_slow_upload_does_not_block_other_commands(self):
        from PyQt6.QtGui import QImage
        started = threading.Event()
        release = threading.Event()

        class SlowClient(FakeWebClient):
            def files_upload_v2(self, **kwargs):
                started.set()
                release.wait(timeout=2)
                return super().files_upload_v2(**kwargs)

        client = SlowClient()
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(0)
        svc = None
        svc = SlackService(
            env=ENV, credentials=("", ""), start_threads=True,
            prefs=SlackPrefs(allowed_user_ids=["U111"], slash_commands_enabled=False),
            web_client_factory=lambda token: client,
            request_screenshot=lambda channel: svc.deliver_screenshot(channel, image),
        )
        self.addCleanup(svc.shutdown)
        self.addCleanup(release.set)
        svc.status_board.begin_run("Safe run", [PlannedStep(0, "Wait")])
        svc.status_board.step_started(0, "Wait")
        self.assertIn("requested", svc.handle_slash_command(
            command="/bl18c", text="screenshot", user_id="U111", channel_id="D123"))
        self.assertTrue(started.wait(timeout=2))
        self.assertIn("Running:* Safe run", svc.handle_slash_command(
            command="/bl18c", text="status", user_id="U111", channel_id="D123"))
        self.assertTrue(svc.status_board.snapshot().running)
        release.set()
        deadline = time.monotonic() + 2
        while svc._screenshot_channel is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(svc._screenshot_channel)
        self.assertEqual(client.uploads[0]["channel"], "D123")


class RunnerEndSignalTests(unittest.TestCase):
    def _run(self, runner):
        outcomes = []
        runner.error_occurred.connect(lambda i, m: outcomes.append(("error", m)))
        runner.sequence_completed.connect(lambda: outcomes.append(("completed",)))
        runner.sequence_stopped.connect(lambda: outcomes.append(("stopped",)))
        runner.sequence_condition_stopped.connect(lambda r: outcomes.append(("condition", r)))
        runner.run()
        return outcomes

    def _runner(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SequenceRunner(Sequence(actions=[WaitAction(duration_s=0.0)]), DeviceContext(),
                              data_dir=tmp.name, log_path=str(Path(tmp.name) / "run"))

    def test_condition_stop_has_its_own_signal(self):
        runner = self._runner()

        def condition_stop(_plan):
            runner._condition_stop = True
            runner._stop_reason = "Ruby pressure reached"
            runner._stop_event.set()
            raise _StopRequested()

        runner._execute_actions = condition_stop
        self.assertEqual(self._run(runner), [("condition", "Ruby pressure reached")])
        self.assertFalse(runner.user_stop_requested)

    def test_user_stop_and_completion(self):
        runner = self._runner()
        runner.request_stop()
        self.assertEqual(self._run(runner), [("stopped",)])
        self.assertTrue(runner.user_stop_requested)
        self.assertEqual(self._run(self._runner()), [("completed",)])

    def test_last_error_code(self):
        runner = self._runner()

        def fail(_plan):
            raise RuntimeError("boom")

        runner._execute_actions = fail
        outcomes = self._run(runner)
        self.assertEqual(outcomes, [("error", "boom")])
        self.assertEqual(runner.last_error_code, "runtime.unexpected_error")


if __name__ == "__main__":
    unittest.main()
