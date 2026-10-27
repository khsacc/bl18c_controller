"""App-wide Slack gateway: run-end notifications and DM slash commands.

Design rules (see IMPLEMENTATION_DETAILS.md in this folder):
  * Nothing here may raise into, or block, the caller. Notifications are put
    on a bounded queue and sent by one background thread with short timeouts.
  * Tokens are entered on Settings → Slack notifications (stored in
    settings/__localdata/slack_credentials.json). A token left empty there
    falls back to the SLACK_BOT_TOKEN / SLACK_APP_TOKEN environment variable.
  * In --debug mode nothing is sent automatically and Socket Mode is not
    started, so a development PC never answers slash commands meant for the
    beamline PC. Explicit actions (channel list, test message) still work.
"""
from __future__ import annotations

import csv
import logging
import math
import io
import os
import queue
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

try:
    from settings import log_prefs, slack_prefs
    from settings.slack_prefs import SlackPrefs
except ImportError:  # standalone execution
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from settings import log_prefs, slack_prefs
    from settings.slack_prefs import SlackPrefs

from .status import (
    HELP_TEXT, RunSnapshot, RunStatusBoard, current_step_line, esc,
    format_duration, format_progress, format_status, format_steps,
)
from .throttle import ErrorAggregator, error_key

_log = logging.getLogger("bl18c.slack")

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"

_QUEUE_SIZE = 100
_MIN_POST_INTERVAL_S = 1.1      # Slack allows about one message per second per channel
_HTTP_TIMEOUT_S = 10
_MAX_TEXT = 3500
_SAMPLE_CACHE_STALE_AFTER_S = 10.0
_SCREENSHOT_TARGETS = frozenset(("steps", "microscope", "pt", "xrd", "ruby"))
_SCREENSHOT_GAP = 12
_MAX_FILE_BYTES = 1_000_000_000  # Slack's per-file upload limit

# --details log of who sent which slash command (log_prefs key and CSV columns).
COMMAND_LOG_KEY = "slack_commands"
_COMMAND_LOG_FIELDS = ("time", "user_id", "user_name", "team_id", "channel_id",
                       "command", "text", "outcome")
_command_log_lock = threading.Lock()


def _is_dm(channel_id: str, channel_name: str = "") -> bool:
    return channel_id.startswith("D") or channel_name == "directmessage"


def _slash_outcome(payload: Mapping) -> str:
    if not payload.get("user_id", ""):
        return "refused: no user ID"
    if not _is_dm(payload.get("channel_id", ""), payload.get("channel_name", "")):
        return "refused: not a DM"
    return "replied"


def log_slash_command(payload: Mapping, outcome: str) -> None:
    """Append one received slash command to the day's CSV when logging is on.

    Uses only fields Slack already sent (no extra API calls). Never raises.
    """
    try:
        if not log_prefs.should_save(COMMAND_LOG_KEY):
            return
        now = datetime.now()
        path = log_prefs.get_app_dir(COMMAND_LOG_KEY) / f"{now:%Y-%m-%d}.csv"
        row = {k: str(payload.get(k, "")) for k in _COMMAND_LOG_FIELDS}
        row["time"] = now.isoformat(timespec="seconds")
        row["outcome"] = outcome
        with _command_log_lock:
            new_file = not path.exists()
            with path.open("a", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=_COMMAND_LOG_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
    except Exception as exc:
        _log.warning("Could not write Slack command log: %s", exc)


# Bot token scopes this feature needs (see IMPLEMENTATION_DETAILS.md).
REQUIRED_BOT_SCOPES = ("chat:write", "chat:write.public", "channels:read", "commands", "files:write")


class SlackUnavailable(RuntimeError):
    """Raised by the blocking helpers (channel list, test message) only."""


def _slack_sdk_available() -> bool:
    try:
        import slack_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def _truncate(text: str, limit: int = _MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n… (truncated)"


# ── notification text ─────────────────────────────────────────────────────────

def _header(icon: str, title: str) -> str:
    return f"{icon} *BL-18C Experimental Scheduler — {title}*  _({esc(socket.gethostname())})_"


def _run_lines(snap: RunSnapshot) -> list[str]:
    name = esc(snap.sequence_name) if snap.sequence_name else "(unnamed sequence)"
    return [
        f"Sequence: *{name}*",
        f"Started {snap.started_at}, ended {snap.ended_at or '—'} "
        f"(elapsed {format_duration(snap.elapsed_s())})",
    ]


def build_completed_text(snap: RunSnapshot) -> str:
    total = len(snap.steps)
    return "\n".join([
        _header(":white_check_mark:", "sequence completed"),
        *_run_lines(snap),
        f"All {total} step(s) finished.",
    ])


def build_condition_stop_text(snap: RunSnapshot, reason: str) -> str:
    return "\n".join([
        _header(":checkered_flag:", "stop condition reached"),
        *_run_lines(snap),
        f"Reason: {esc(reason) or '(not given)'}",
        f"Last step started: {current_step_line(snap)}",
    ])


def build_error_text(snap: RunSnapshot, code: str, message: str) -> str:
    lines = [
        _header(":rotating_light:", "sequence stopped by an error"),
        *_run_lines(snap),
        f"Last step started: {current_step_line(snap)}",
    ]
    if code:
        lines.append(f"Code: `{esc(code)}`")
    lines.append(f"```{esc(message)}```")
    return "\n".join(lines)


def _reading(value: object, unit: str) -> str:
    """Format a cached numeric reading without showing invalid data as a value."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unavailable"
    return f"{number:g} {unit}" if math.isfinite(number) else "unavailable"


def _lakeshore_text(readings: Mapping[str, object] | None) -> str:
    if not readings or not readings.get("connected"):
        return "LakeShore 335 is not connected to the application."
    ramp_enabled = readings.get("ramp_enabled")
    ramp_rate = _reading(readings.get("ramp_rate_k_per_min"), "K/min")
    ramp = (ramp_rate if ramp_enabled is True else
            f"Off (configured {ramp_rate})" if ramp_enabled is False else "unavailable")
    heater_range = readings.get("heater_range") or "unavailable"
    return "\n".join((
        "*LakeShore 335*",
        f"Tset: {_reading(readings.get('target_setpoint_k'), 'K')}",
        f"TA: {_reading(readings.get('temperature_a_k'), 'K')}",
        f"TB: {_reading(readings.get('temperature_b_k'), 'K')}",
        f"Ramp: {ramp}",
        f"Heater range: {esc(str(heater_range))}",
    ))


def _pace5000_text(readings: Mapping[str, object] | None) -> str:
    if not readings or not readings.get("connected"):
        return "PACE5000 is not connected to the application."
    try:
        ramp = _reading(float(readings.get("slew_mpa_per_sec")) * 60, "MPa/min")
    except (TypeError, ValueError):
        ramp = "unavailable"
    return "\n".join((
        "*PACE5000*",
        f"Target: {_reading(readings.get('target_pressure_mpa'), 'MPa')}",
        f"Current pressure: {_reading(readings.get('pressure_mpa'), 'MPa')}",
        f"Ramp: {ramp}",
    ))


def _sample_cache_line(readings: Mapping[str, object] | None, *, label: str,
                       key: str, unit: str, now: float) -> str:
    if readings is not None and readings.get("_cache_error"):
        return f"—: {label}: unavailable (cache error)"
    if not readings or readings.get("connected") is not True:
        return f"—: {label}: unavailable (not connected)"
    try:
        value = float(readings.get(key))
        timestamp = float(readings.get("timestamp"))
        if not (math.isfinite(value) and math.isfinite(timestamp) and timestamp > 0):
            raise ValueError("invalid cached reading")
        age = now - timestamp
        if age < -2:
            raise ValueError("future timestamp")
        when = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return f"—: {label}: unavailable (no valid timestamped reading)"
    stale = f" (stale, {age:.0f} s old)" if age > _SAMPLE_CACHE_STALE_AFTER_S else ""
    return f"{when}: {label}: {value:g} {unit}{stale}"


def _sample_text(lakeshore: Mapping[str, object] | None,
                 pace5000: Mapping[str, object] | None, snap: RunSnapshot,
                 *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    lines = [
        "*BL-18C sample environment* (beamline PC local time)",
        _sample_cache_line(lakeshore, label="Temperature (LakeShore 335 Ch B)",
                           key="temperature_b_k", unit="K", now=now),
        _sample_cache_line(pace5000, label="Gas pressure (PACE5000)",
                           key="pressure_mpa", unit="MPa", now=now),
    ]
    ruby = snap.ruby_sample
    if ruby is None:
        period = "in this run" if snap.has_run else "since application start"
        lines.append(f"—: Ruby pressure: no valid measurement {period}")
        return "\n".join(lines)
    when = datetime.fromtimestamp(ruby.acquired_at).strftime("%Y-%m-%d %H:%M:%S")
    ruby_line = (f"{when}: Ruby pressure ({esc(ruby.name)}): {ruby.pressure_gpa:.3f} GPa; "
                 f"raw R1 wavelength: {ruby.peak_nm:.4f} nm")
    if ruby.temperature_correction_enabled:
        temperature = (f"{ruby.correction_temperature_k:g} K"
                       if ruby.correction_temperature_k is not None else "unavailable")
        source = ("LakeShore 335 Ch B" if ruby.correction_temperature_source == "lakeshore_b"
                  else "manual setting" if ruby.correction_temperature_source == "manual"
                  else esc(ruby.correction_temperature_source) or "unknown source")
        measured_at = ""
        if ruby.correction_temperature_timestamp:
            try:
                measured_at = (", measured " + datetime.fromisoformat(
                    ruby.correction_temperature_timestamp).strftime("%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
        ruby_line += f"; correction temperature: {temperature} ({source}{measured_at})"
    lines.append(ruby_line)
    return "\n".join(lines)


# ── service ───────────────────────────────────────────────────────────────────

class SlackService:
    def __init__(
        self,
        *,
        debug: bool = False,
        prefs: SlackPrefs | None = None,
        env: Mapping[str, str] | None = None,
        credentials: tuple[str, str] | None = None,
        web_client_factory: Callable[[str], object] | None = None,
        socket_client_factory: Callable[[str, object], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        start_threads: bool = True,
        lakeshore_readings: Callable[[], Mapping[str, object] | None] | None = None,
        pace5000_readings: Callable[[], Mapping[str, object] | None] | None = None,
        request_screenshot: Callable[[str, str], None] | None = None,
    ) -> None:
        self._debug = debug
        self._env = env if env is not None else os.environ
        # Tokens saved from the settings page; "" = fall back to the env var.
        self._saved_bot_token, self._saved_app_token = (
            credentials if credentials is not None else slack_prefs.load_credentials())
        self._web_client_factory = web_client_factory or self._default_web_client
        self._socket_client_factory = socket_client_factory or self._default_socket_client
        self._clock = clock
        self._prefs_lock = threading.Lock()
        self._prefs = prefs if prefs is not None else slack_prefs.load()
        self.status_board = RunStatusBoard()
        self._lakeshore_readings = lakeshore_readings
        self._pace5000_readings = pace5000_readings
        self._request_screenshot = request_screenshot
        self._screenshot_lock = threading.Lock()
        self._screenshot_channel: str | None = None
        self._screenshot_queue: queue.Queue = queue.Queue(maxsize=1)
        self._raw_lock = threading.Lock()
        self._raw_image_path = ""
        self._raw_image_frame = None
        self._raw_image_name = ""
        self._raw_channel: str | None = None
        self._raw_queue: queue.Queue = queue.Queue(maxsize=1)

        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
        # Owned by the sender thread only.
        self._aggregator = ErrorAggregator(self._prefs.error_suppress_minutes * 60)
        self._last_post_at = -_MIN_POST_INTERVAL_S
        self._web_client = None
        self._web_client_lock = threading.Lock()

        self._last_send_error = ""
        self._socket_client = None
        self._socket_lock = threading.Lock()
        self._socket_state = "not started"
        self._shutdown = threading.Event()

        # GUI-thread callbacks run after settings or tokens change.
        self._listeners: list[Callable[[], None]] = []

        self._sender: threading.Thread | None = None
        self._screenshot_sender: threading.Thread | None = None
        self._raw_sender: threading.Thread | None = None
        if start_threads:
            self._sender = threading.Thread(target=self._sender_loop, name="SlackSender",
                                            daemon=True)
            self._sender.start()
            self._screenshot_sender = threading.Thread(
                target=self._screenshot_loop, name="SlackScreenshot", daemon=True)
            self._screenshot_sender.start()
            self._raw_sender = threading.Thread(
                target=self._raw_loop, name="SlackXrdUpload", daemon=True)
            self._raw_sender.start()
            self._apply_socket_mode()

    # ── settings ──────────────────────────────────────────────────────────────

    @property
    def prefs(self) -> SlackPrefs:
        with self._prefs_lock:
            return replace(self._prefs)

    def update_prefs(self, prefs: SlackPrefs) -> None:
        """Apply new settings (the caller has already saved them)."""
        with self._prefs_lock:
            self._prefs = prefs
        self._put(("window", prefs.error_suppress_minutes * 60))
        self._apply_socket_mode()
        self._notify_listeners()

    def set_enabled(self, enabled: bool) -> None:
        """Switch notifications on/off and save it (used by the scheduler window)."""
        with self._prefs_lock:
            self._prefs = replace(self._prefs, enabled=bool(enabled))
            prefs = replace(self._prefs)
        try:
            slack_prefs.save(prefs)
        except OSError:
            _log.exception("Could not save the Slack on/off setting")
        self._notify_listeners()

    @property
    def debug(self) -> bool:
        return self._debug

    def setup_problem(self) -> str:
        """Why notifications cannot be sent even when switched on ("" = ready)."""
        reason = self.unavailable_reason()
        if reason:
            return reason
        if not self.prefs.channel_id:
            return "No notification channel is selected."
        return ""

    def add_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _notify_listeners(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                _log.exception("Slack settings listener failed")

    # ── availability ──────────────────────────────────────────────────────────

    def update_credentials(self, bot_token: str, app_token: str) -> None:
        """Use new tokens now (the caller has already saved them)."""
        self._saved_bot_token, self._saved_app_token = bot_token.strip(), app_token.strip()
        with self._web_client_lock:
            self._web_client = None
        self._stop_socket_mode()
        self._socket_state = "not started"
        self._last_send_error = ""
        self._apply_socket_mode()
        self._notify_listeners()

    def bot_token(self) -> str:
        return self._saved_bot_token or self._env.get(BOT_TOKEN_ENV, "").strip()

    def app_token(self) -> str:
        return self._saved_app_token or self._env.get(APP_TOKEN_ENV, "").strip()

    def token_source(self, which: str) -> str:
        """'settings', 'environment variable' or 'not set' for "bot" / "app"."""
        saved = self._saved_bot_token if which == "bot" else self._saved_app_token
        env_name = BOT_TOKEN_ENV if which == "bot" else APP_TOKEN_ENV
        if saved:
            return "settings"
        if self._env.get(env_name, "").strip():
            return f"environment variable {env_name}"
        return "not set"

    def unavailable_reason(self) -> str:
        """Why the Web API cannot be used ("" if it can)."""
        if not _slack_sdk_available():
            return "The slack_sdk package is not installed (pip install slack_sdk)."
        if not self.bot_token():
            return "The Bot User OAuth Token (xoxb-…) is not set."
        return ""

    def status_text(self) -> str:
        """One-paragraph summary for the settings page."""
        reason = self.unavailable_reason()
        prefs = self.prefs
        lines = []
        if reason:
            lines.append(f"Not available: {reason}")
        elif self._debug:
            lines.append("Debug mode: notifications are not sent automatically "
                         "and slash commands are not served.")
        elif not prefs.enabled:
            lines.append("Notifications are switched off.")
        elif not prefs.channel_id:
            lines.append("No notification channel is selected.")
        else:
            lines.append(f"Notifications go to #{prefs.channel_name or prefs.channel_id}.")
        state = self._socket_state
        client = self._socket_client
        if state == "connected" and client is not None:
            try:
                if not client.is_connected():
                    state = "reconnecting…"
            except Exception:
                pass
        lines.append(f"Slash commands: {state}.")
        if self._last_send_error:
            lines.append(f"Last send error: {self._last_send_error}")
        return "\n".join(lines)

    # ── notifications (never raise, never block) ──────────────────────────────

    def notify_completed(self) -> None:
        self._safe(lambda: self._notify("notify_on_completion",
                                        build_completed_text(self.status_board.snapshot())))

    def notify_condition_stop(self, reason: str) -> None:
        self._safe(lambda: self._notify("notify_on_condition_stop",
                                        build_condition_stop_text(self.status_board.snapshot(),
                                                                  reason)))

    def notify_error(self, code: str, message: str) -> None:
        def go() -> None:
            snap = self.status_board.snapshot()
            text = build_error_text(snap, code, message)
            channel = self._channel_if_enabled("notify_on_error", text)
            if channel:
                label = f"{snap.ended_at or ''} — {current_step_line(snap)}"
                self._put(("error", channel, error_key(code, message), text, label))
        self._safe(go)

    def _notify(self, switch: str, text: str) -> None:
        channel = self._channel_if_enabled(switch, text)
        if channel:
            self._put(("post", channel, text))

    def _channel_if_enabled(self, switch: str, text: str) -> str:
        prefs = self.prefs
        if not prefs.enabled or not getattr(prefs, switch) or not prefs.channel_id:
            return ""
        if self._debug:
            _log.info("[Slack:debug] not sent to %s:\n%s", prefs.channel_id, text)
            return ""
        if self.unavailable_reason():
            return ""
        return prefs.channel_id

    def _put(self, item: tuple) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Drop the oldest message rather than block or lose the newest.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                _log.warning("Slack queue full; notification dropped")

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # a notification must never break the caller
            _log.exception("Slack notification could not be queued")

    # ── sender thread ─────────────────────────────────────────────────────────

    def _sender_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is not None and item[0] == "stop":
                return
            try:
                self.process(item)
            except Exception:
                _log.exception("Slack sender failed")

    def process(self, item: tuple | None) -> None:
        """Handle one queue item (or None for a periodic tick). Sender thread only."""
        now = self._clock()
        if item is not None:
            kind = item[0]
            if kind == "post":
                _, channel, text = item
                self._post(channel, text)
            elif kind == "error":
                _, channel, key, text, label = item
                to_send = self._aggregator.submit(key, text, now, label)
                if to_send is not None:
                    self._post(channel, to_send)
            elif kind == "window":
                self._aggregator.window_s = max(0.0, float(item[1]))
        for summary in self._aggregator.due(now):
            channel = self.prefs.channel_id
            if channel and not self._debug:
                self._post(channel, summary)

    def _post(self, channel: str, text: str) -> None:
        wait = self._last_post_at + _MIN_POST_INTERVAL_S - self._clock()
        if wait > 0:
            self._shutdown.wait(wait)
        self._last_post_at = self._clock()
        try:
            self._client().chat_postMessage(
                channel=channel, text=_truncate(text),
                unfurl_links=False, unfurl_media=False,
            )
            self._last_send_error = ""
        except Exception as exc:
            self._last_send_error = f"{time.strftime('%H:%M:%S')} {_describe_error(exc)}"
            _log.warning("Slack notification failed: %s", _describe_error(exc))

    # ── Web API helpers ───────────────────────────────────────────────────────

    def _client(self):
        with self._web_client_lock:
            if self._web_client is None:
                token = self.bot_token()
                if not token:
                    raise SlackUnavailable("The Bot User OAuth Token (xoxb-…) is not set.")
                self._web_client = self._web_client_factory(token)
            return self._web_client

    @staticmethod
    def _default_web_client(token: str):
        from slack_sdk import WebClient
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        client = WebClient(token=token, timeout=_HTTP_TIMEOUT_S)
        client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=1))
        return client

    # ── screenshots: no GUI or hardware access from Slack threads ────────────

    def _begin_screenshot(self, channel_id: str, target: str = "window") -> str:
        if self._request_screenshot is None:
            return "Scheduler screenshots are unavailable."
        with self._screenshot_lock:
            if self._shutdown.is_set():
                return "Scheduler screenshots are unavailable."
            if self._screenshot_channel is not None:
                return "A screenshot request is already in progress. Please try again shortly."
            self._screenshot_channel = channel_id
        try:
            self._request_screenshot(channel_id, target)
        except Exception:
            with self._screenshot_lock:
                self._screenshot_channel = None
            _log.exception("Could not request scheduler screenshot")
            return "The screenshot could not be requested."
        return "Screenshot requested. The image will appear in this DM shortly."

    def deliver_screenshot(self, channel_id: str, images, error: str = "",
                           target: str = "window") -> None:
        """Non-blocking hand-off from the GUI thread to the upload worker."""
        with self._screenshot_lock:
            if self._screenshot_channel != channel_id or self._shutdown.is_set():
                return
        try:
            self._screenshot_queue.put_nowait((channel_id, images, error, target))
        except queue.Full:
            with self._screenshot_lock:
                self._screenshot_channel = None
            _log.warning("Slack screenshot queue full; image dropped")

    def _screenshot_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                item = self._screenshot_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_screenshot(item)
            except Exception:
                _log.exception("Slack screenshot worker failed")

    def process_screenshot(self, item: tuple) -> None:
        """Encode and upload one image; called only by the screenshot worker."""
        channel_id, images, error, target = item
        try:
            if error:
                self._client().chat_postMessage(channel=channel_id, text=error)
                return
            from PyQt6.QtCore import QBuffer, QIODevice, Qt
            from PyQt6.QtGui import QImage, QPainter

            if isinstance(images, QImage):
                images = (images,)
            if not images or any(image.isNull() for image in images):
                raise ValueError("Screenshot image is empty")
            if len(images) == 1:
                image = images[0]
            else:
                width = max(part.width() for part in images)
                height = sum(part.height() for part in images) + _SCREENSHOT_GAP * (len(images) - 1)
                image = QImage(width, height, QImage.Format.Format_RGB32)
                if image.isNull():
                    raise ValueError("Screenshot composition failed")
                image.fill(Qt.GlobalColor.white)
                painter = QPainter(image)
                try:
                    y = 0
                    for part in images:
                        physical_part = QImage(part)
                        physical_part.setDevicePixelRatio(1.0)
                        painter.drawImage(0, y, physical_part)
                        y += part.height() + _SCREENSHOT_GAP
                finally:
                    painter.end()
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, "PNG"):
                raise ValueError("PNG encoding failed")
            png = bytes(buffer.data())
            suffix = "" if target == "window" else f"-{target}"
            self._client().files_upload_v2(
                channel=channel_id, file=io.BytesIO(png),
                filename=f"experimental-scheduler{suffix}.png",
                title=f"Experimental Scheduler {target} screenshot",
            )
        except Exception as exc:
            _log.warning("Slack screenshot failed: %s", _describe_error(exc))
            try:
                self._client().chat_postMessage(
                    channel=channel_id, text="The screenshot could not be sent. Check Slack permissions and connection.")
            except Exception:
                _log.exception("Could not report Slack screenshot failure")
        finally:
            with self._screenshot_lock:
                if self._screenshot_channel == channel_id:
                    self._screenshot_channel = None

    # ── latest XRD numerical data: captured by the scheduler, uploaded off-thread ──

    def record_xrd_image(self, frame, saved_path: str) -> None:
        """Keep the latest completed capture without querying the detector."""
        with self._raw_lock:
            self._raw_image_path = saved_path
            self._raw_image_frame = None if saved_path else frame
            self._raw_image_name = (Path(saved_path).name if saved_path else
                                    f"xrd-{datetime.now():%Y%m%d-%H%M%S}.tif")

    def _begin_raw_upload(self, channel_id: str) -> str:
        with self._raw_lock:
            if self._shutdown.is_set():
                return "XRD data are unavailable while the application is closing."
            if self._raw_channel is not None:
                return "An XRD file upload is already in progress. Please try again shortly."
            if not self._raw_image_name:
                return "No XRD image has been captured since the application started."
            data = (self._raw_image_path, self._raw_image_frame, self._raw_image_name)
            self._raw_channel = channel_id
        try:
            self._raw_queue.put_nowait((channel_id, data))
        except queue.Full:
            with self._raw_lock:
                self._raw_channel = None
            return "An XRD file upload is already in progress. Please try again shortly."
        return "XRD file requested. It will appear in this DM shortly."

    def _raw_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                item = self._raw_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_raw_upload(item)
            except Exception:
                _log.exception("Slack XRD upload worker failed")

    def process_raw_upload(self, item: tuple) -> None:
        """Upload the requested data; called only by the XRD upload worker."""
        channel_id, data = item
        try:
            path_text, frame, name = data
            if path_text:
                path = Path(path_text)
                size = path.stat().st_size
                if size == 0 or size > _MAX_FILE_BYTES:
                    raise ValueError("The XRD image is empty or exceeds Slack's 1 GB file limit")
                with path.open("rb") as source:
                    self._client().files_upload_v2(
                        channel=channel_id, file=source, filename=name,
                        title=f"XRD image: {name}",
                    )
            else:
                import cv2

                if frame is None or frame.nbytes > _MAX_FILE_BYTES:
                    raise ValueError("The XRD image is unavailable or exceeds Slack's 1 GB file limit")
                ok, encoded = cv2.imencode(".tif", frame)
                if not ok or encoded.nbytes > _MAX_FILE_BYTES:
                    raise ValueError("The XRD image could not be encoded within Slack's 1 GB file limit")
                self._client().files_upload_v2(
                    channel=channel_id, file=io.BytesIO(encoded.tobytes()),
                    filename=name, title=f"XRD image: {name}",
                )
        except Exception as exc:
            _log.warning("Slack XRD file upload failed: %s", _describe_error(exc))
            try:
                self._client().chat_postMessage(
                    channel=channel_id,
                    text=(str(exc) if isinstance(exc, ValueError) else
                          "The XRD file could not be sent. Check the saved file, "
                          "Slack permissions and connection."),
                )
            except Exception:
                _log.exception("Could not report Slack XRD upload failure")
        finally:
            with self._raw_lock:
                if self._raw_channel == channel_id:
                    self._raw_channel = None

    def list_public_channels(self) -> list[dict]:
        """Blocking: call from a worker thread. Returns [{id, name, is_member}]."""
        reason = self.unavailable_reason()
        if reason:
            raise SlackUnavailable(reason)
        channels: list[dict] = []
        cursor = None
        try:
            while True:
                kwargs = {"types": "public_channel", "exclude_archived": True, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                response = self._client().conversations_list(**kwargs)
                for ch in response.get("channels", []):
                    channels.append({"id": ch["id"], "name": ch.get("name", ch["id"]),
                                     "is_member": bool(ch.get("is_member"))})
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackUnavailable:
            raise
        except Exception as exc:
            raise SlackUnavailable(_describe_error(exc)) from exc
        channels.sort(key=lambda c: c["name"])
        return channels

    def send_test_message(self, channel_id: str) -> None:
        """Blocking: call from a worker thread. Raises SlackUnavailable on failure."""
        reason = self.unavailable_reason()
        if reason:
            raise SlackUnavailable(reason)
        try:
            self._client().chat_postMessage(
                channel=channel_id,
                text=_header(":wave:", "test message") + "\nNotifications will be posted here.",
            )
        except Exception as exc:
            raise SlackUnavailable(_describe_error(exc)) from exc

    def check_credentials(self, bot_token: str, app_token: str) -> list[tuple[bool, str]]:
        """Blocking: call from a worker thread. Checks the given tokens against
        Slack without saving them. Returns [(ok, message), …]."""
        if not _slack_sdk_available():
            return [(False, "The slack_sdk package is not installed (pip install slack_sdk).")]
        results: list[tuple[bool, str]] = []
        bot_token, app_token = bot_token.strip(), app_token.strip()
        if not bot_token:
            results.append((False, "Bot User OAuth Token: not set."))
        else:
            try:
                response = self._web_client_factory(bot_token).auth_test()
                results.append((True, f"Bot User OAuth Token: OK — workspace "
                                      f"'{response.get('team', '?')}', bot @{response.get('user', '?')}."))
                granted = _granted_scopes(response)
                if granted is not None:
                    missing = [s for s in REQUIRED_BOT_SCOPES if s not in granted]
                    if missing:
                        results.append((False, "Missing bot token scopes: " + ", ".join(missing)
                                        + " — add them under OAuth & Permissions and reinstall the app."))
                    else:
                        results.append((True, "Bot token scopes: all required scopes are granted."))
            except Exception as exc:
                results.append((False, f"Bot User OAuth Token: {_describe_error(exc)}"))
        if not app_token:
            results.append((not self.prefs.slash_commands_enabled,
                            "App-Level Token: not set (needed only for slash commands)."))
        else:
            try:
                self._web_client_factory(app_token).apps_connections_open(app_token=app_token)
                results.append((True, "App-Level Token: OK — Socket Mode is enabled."))
            except Exception as exc:
                results.append((False, f"App-Level Token: {_describe_error(exc)}"))
        return results

    # ── slash commands (Socket Mode) ──────────────────────────────────────────

    def handle_slash_command(self, *, command: str, text: str, user_id: str,
                             channel_id: str, channel_name: str = "") -> str:
        """Return the reply text; screenshot only queues isolated background work."""
        if not user_id:
            _log.warning("Slack slash command refused without a user ID")
            return ":no_entry: Slack did not identify the member who sent this command."
        if not _is_dm(channel_id, channel_name):
            return "Please use this command in your direct-message conversation with the bot."
        sub = (text or "").strip().lower().split()
        sub_command = sub[0] if sub else "status"
        if sub_command == "screenshot":
            if len(sub) > 2 or (len(sub) == 2 and sub[1] not in _SCREENSHOT_TARGETS):
                return ("Usage: `/bl18c screenshot [steps|microscope|pt|xrd|ruby]` "
                        "(no option: whole window).")
            return self._begin_screenshot(channel_id, sub[1] if len(sub) == 2 else "window")
        if sub_command == "xrd" and len(sub) == 2 and sub[1] == "rawimg":
            return self._begin_raw_upload(channel_id)
        snap = self.status_board.snapshot()
        if sub_command == "status":
            return format_status(snap)
        if sub_command == "progress":
            return format_progress(snap)
        if sub_command in ("steps", "sequence"):
            return format_steps(snap)
        if sub_command == "sample":
            def cached(provider, name):
                if provider is None:
                    return None
                try:
                    readings = provider()
                    return (readings if readings is None or isinstance(readings, Mapping)
                            else {"_cache_error": True})
                except Exception:
                    _log.exception("Could not read cached %s readings for Slack", name)
                    return {"_cache_error": True}

            return _sample_text(
                cached(self._lakeshore_readings, "LakeShore 335"),
                cached(self._pace5000_readings, "PACE5000"), snap,
            )
        if sub_command in ("lakeshore", "pace5000"):
            provider = (self._lakeshore_readings if sub_command == "lakeshore"
                        else self._pace5000_readings)
            formatter = _lakeshore_text if sub_command == "lakeshore" else _pace5000_text
            try:
                return formatter(provider() if provider is not None else None)
            except Exception:
                _log.exception("Could not read cached %s readings for Slack", sub_command)
                name = "LakeShore 335" if sub_command == "lakeshore" else "PACE5000"
                return f"{name} readings are unavailable."
        return HELP_TEXT.format(cmd=command or "/command")

    def _apply_socket_mode(self) -> None:
        want = self.prefs.slash_commands_enabled
        if not want:
            self._stop_socket_mode()
            self._socket_state = "off"
            return
        if self._debug:
            self._socket_state = "not served in debug mode"
            return
        reason = self.unavailable_reason()
        if not reason and not self.app_token():
            reason = "the App-Level Token (xapp-…) is not set"
        if reason:
            self._socket_state = f"unavailable — {reason}"
            return
        with self._socket_lock:
            if self._socket_client is not None:
                return
            self._socket_state = "connecting…"
        threading.Thread(target=self._connect_socket_mode, name="SlackSocketMode",
                         daemon=True).start()

    def _connect_socket_mode(self) -> None:
        try:
            client = self._socket_client_factory(self.app_token(), self._client())
            client.socket_mode_request_listeners.append(self._on_socket_request)
            with self._socket_lock:
                if self._shutdown.is_set() or not self.prefs.slash_commands_enabled:
                    return
                self._socket_client = client
            client.connect()
            self._socket_state = "connected"
        except Exception as exc:
            with self._socket_lock:
                self._socket_client = None
            self._socket_state = f"connection failed — {_describe_error(exc)}"
            _log.warning("Slack Socket Mode connection failed: %s", _describe_error(exc))

    @staticmethod
    def _default_socket_client(app_token: str, web_client):
        from slack_sdk.socket_mode import SocketModeClient
        return SocketModeClient(app_token=app_token, web_client=web_client, logger=_log)

    def _on_socket_request(self, client, req) -> None:
        payload = None
        outcome = "error"
        try:
            from slack_sdk.socket_mode.response import SocketModeResponse
            if req.type != "slash_commands":
                client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
                return
            payload = req.payload or {}
            reply = self.handle_slash_command(
                command=payload.get("command", ""), text=payload.get("text", ""),
                user_id=payload.get("user_id", ""), channel_id=payload.get("channel_id", ""),
                channel_name=payload.get("channel_name", ""),
            )
            client.send_socket_mode_response(SocketModeResponse(
                envelope_id=req.envelope_id,
                payload={"response_type": "ephemeral", "text": _truncate(reply)},
            ))
            outcome = _slash_outcome(payload)
        except Exception:
            _log.exception("Slack slash command handling failed")
        finally:
            if payload is not None:
                log_slash_command(payload, outcome)

    def _stop_socket_mode(self) -> None:
        with self._socket_lock:
            client, self._socket_client = self._socket_client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                _log.exception("Slack Socket Mode close failed")

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Stop Socket Mode, give queued messages up to *timeout_s* to go out."""
        self._stop_socket_mode()
        self._put(("stop",))
        if self._sender is not None:
            self._sender.join(timeout=timeout_s)
        self._shutdown.set()
        if self._screenshot_sender is not None:
            self._screenshot_sender.join(timeout=timeout_s)
        if self._raw_sender is not None:
            self._raw_sender.join(timeout=timeout_s)


def _granted_scopes(response) -> set[str] | None:
    """Scopes from the X-OAuth-Scopes response header (None if not present)."""
    headers = getattr(response, "headers", None) or {}
    for name, value in dict(headers).items():
        if str(name).lower() == "x-oauth-scopes":
            if isinstance(value, (list, tuple)):
                value = ",".join(value)
            return {scope.strip() for scope in str(value).split(",") if scope.strip()}
    return None


def _describe_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            code = response.get("error")
        except Exception:
            code = None
        if code:
            hints = {
                "not_in_channel": "the bot is not in that channel (add the chat:write.public scope)",
                "channel_not_found": "channel not found",
                "invalid_auth": "invalid token",
                "not_authed": "no token was sent",
                "token_revoked": "the token has been revoked",
                "not_allowed_token_type": "wrong kind of token for this call",
                "missing_scope": "the Slack app is missing a required scope",
                "ratelimited": "rate limited by Slack",
            }
            return f"{code}: {hints[code]}" if code in hints else str(code)
    return f"{type(exc).__name__}: {exc}"
