"""Central PM16C state cache and append-only communication audit log.

The monitor is deliberately owned by one controller instance.  Every valid
``STSx?`` reply, regardless of whether it was requested by the monitor or by
an application, refreshes the same cache.  The background monitor only polls
channels whose cached observation is stale, avoiding a second independent
polling stream when a UI is already querying the controller.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import gzip
import shutil
import threading
import time
import uuid
from typing import Callable, Iterable


MONITORED_CHANNELS = tuple(range(1, 12))
IDLE_POLL_INTERVAL_S = 5.0
MOVING_POLL_INTERVAL_S = 1.0
MAX_AUDIT_FILE_BYTES = 10 * 1024 * 1024
MAX_SESSION_TOTAL_BYTES = 200 * 1024 * 1024
MAX_INCIDENT_TOTAL_BYTES = 500 * 1024 * 1024
SESSION_RETENTION_DAYS = 30
INCIDENT_RETENTION_DAYS = 90
TRACE_HISTORY_S = 10 * 60
INCIDENT_AFTER_S = 60
SNAPSHOT_INTERVAL_S = 5 * 60
HEALTH_SUMMARY_INTERVAL_S = 60 * 60
STOP_CONFIRM_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ChannelState:
    channel: int
    position: int
    motion_state: str
    mode: str
    ls_hold: str
    status_byte: str
    observed_monotonic: float
    observed_at: str
    source: str


@dataclass
class ExpectedMotion:
    operation_id: str
    command: str
    target: int | None
    source: str
    started_monotonic: float
    seen_moving: bool = False


@dataclass
class ExpectedStop:
    stop_operation_id: str
    command: str
    source: str
    requested_monotonic: float
    deadline_monotonic: float
    position_at_request: int | None
    motion_state_at_request: str | None
    motion_operation_id: str | None
    motion_command: str | None


class PM16CAuditLogger:
    """Non-blocking JSONL audit writer.

    Calls from inside the controller's communication lock only enqueue a
    small dictionary.  File I/O is performed by a dedicated daemon thread, so
    a slow or unavailable log disk cannot delay a PM16C command.
    """

    def __init__(self, base_dir: Path | None = None, *, enabled: bool = True):
        self.enabled = enabled
        self.base_dir = base_dir or self._default_base_dir()
        self.session_id = uuid.uuid4().hex[:12]
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=20_000)
        self._lifecycle_lock = threading.RLock()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._dropped = 0
        self._thread: threading.Thread | None = None
        self._running = False
        self._path: Path | None = None
        self._part = 0
        # Routine STS traffic is kept here, not in the session JSONL.  The
        # generous count limit is only a safety bound; incident extraction is
        # time based and takes the most recent TRACE_HISTORY_S seconds.
        self._ring: deque[dict] = deque(maxlen=50_000)
        self._ring_lock = threading.Lock()
        self._incident_lock = threading.Lock()
        self._incident_stop = threading.Event()
        self._incident_threads: list[threading.Thread] = []
        self._active_incidents: dict[object, float] = {}

    @staticmethod
    def _default_base_dir() -> Path:
        try:
            from settings import log_prefs
            return log_prefs.get_base_dir() / "stage_audit"
        except Exception:
            return Path(__file__).resolve().parents[2] / "__localdata" / "stage_audit"

    @property
    def path(self) -> Path | None:
        return self._path

    def start(self, **metadata) -> None:
        with self._lifecycle_lock:
            if not self.enabled or self._running:
                return
            now = datetime.now().astimezone()
            directory = self.base_dir / "sessions" / now.strftime("%Y-%m-%d")
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self._cleanup_old_logs()
                name = f"pm16c_{now:%Y%m%d_%H%M%S}_{os.getpid()}_{self.session_id}.jsonl"
                self._path = directory / name
                self._running = True
                self._incident_stop.clear()
                self._thread = threading.Thread(
                    target=self._writer_loop,
                    name="PM16C-audit-writer",
                    daemon=True,
                )
                self._thread.start()
                self.record("session_start", level="INFO", **metadata)
            except Exception as exc:
                self.enabled = False
                self._running = False
                print(f"[PM16C audit] Could not start audit log: {exc}")

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._running:
                return
            # record() uses this same RLock.  The session_stop item and sentinel
            # are therefore ordered after every already-accepted event, and no
            # later record() call can enqueue behind the sentinel.
            self.record("session_stop", level="INFO", dropped_events=self._dropped)
            self._running = False
            self._incident_stop.set()
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # The writer drains all queued events and exits on queue.Empty
                # when _running is false, so a full queue remains lossless.
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for thread in list(self._incident_threads):
            thread.join(timeout=2.0)
        self._incident_threads.clear()

    def record(
        self,
        event: str,
        *,
        level: str = "INFO",
        persist: bool = True,
        **fields,
    ) -> dict | None:
        """Record an event.

        ``persist=False`` is the flight-recorder path used by normal STS
        traffic.  Such events remain available for incident extraction but do
        not enter the unbounded session log.
        """
        with self._lifecycle_lock:
            if not self.enabled:
                return None
            now = datetime.now().astimezone()
            with self._seq_lock:
                self._seq += 1
                seq = self._seq
            item = {
                "schema": 1,
                "timestamp": now.isoformat(timespec="milliseconds"),
                "monotonic_ns": time.monotonic_ns(),
                "session_id": self.session_id,
                "seq": seq,
                "event": event,
                "level": level,
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
                "thread_id": threading.get_ident(),
                **fields,
            }
            with self._ring_lock:
                self._ring.append(item)
            if persist and self._running:
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    self._dropped += 1
            return item

    def write_incident(self, trigger: dict, states: dict[int, ChannelState]) -> None:
        """Persist ten minutes before and one minute after an anomaly.

        File I/O and the post-trigger wait run outside the controller thread.
        ``stop()`` wakes the worker so application shutdown still saves all
        post-trigger trace collected up to that point.
        """
        if not self.enabled:
            return
        with self._incident_lock:
            try:
                directory = self.base_dir / "incidents"
                directory.mkdir(parents=True, exist_ok=True)
                now = datetime.now().astimezone()
                ch = trigger.get("channel", "unknown")
                now_mono = time.monotonic()
                if self._active_incidents.get(ch, 0.0) > now_mono:
                    return
                self._active_incidents[ch] = now_mono + INCIDENT_AFTER_S
                event_name = "".join(
                    char if char.isalnum() or char in "_-" else "_"
                    for char in str(trigger.get("event", "incident"))
                )
                path = directory / f"{now:%Y%m%d_%H%M%S_%f}_ch{ch}_{event_name}.jsonl"
                thread = threading.Thread(
                    target=self._write_incident_window,
                    args=(path, trigger, dict(states)),
                    name=f"PM16C-incident-ch{ch}",
                    daemon=True,
                )
                self._incident_threads.append(thread)
                thread.start()
            except Exception as exc:
                self._active_incidents.pop(trigger.get("channel", "unknown"), None)
                print(f"[PM16C audit] Could not write incident log: {exc}")

    def _write_incident_window(
        self,
        path: Path,
        trigger: dict,
        states: dict[int, ChannelState],
    ) -> None:
        trigger_ns = int(trigger["monotonic_ns"])
        cutoff_ns = trigger_ns - TRACE_HISTORY_S * 1_000_000_000
        with self._ring_lock:
            before = [
                event for event in self._ring
                if cutoff_ns <= event["monotonic_ns"] <= trigger_ns
            ]
        snapshot = {
            "schema": 1,
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "monotonic_ns": time.monotonic_ns(),
            "session_id": self.session_id,
            "event": "incident_snapshot",
            "trigger_channel": trigger.get("channel"),
            "states": {str(k): asdict(v) for k, v in sorted(states.items())},
        }
        try:
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                for event in before:
                    fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                fh.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")

            self._incident_stop.wait(INCIDENT_AFTER_S)
            end_ns = min(
                time.monotonic_ns(),
                trigger_ns + INCIDENT_AFTER_S * 1_000_000_000,
            )
            with self._ring_lock:
                after = [
                    event for event in self._ring
                    if trigger_ns < event["monotonic_ns"] <= end_ns
                ]
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                for event in after:
                    fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                fh.write(json.dumps({
                    "schema": 1,
                    "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "monotonic_ns": end_ns,
                    "session_id": self.session_id,
                    "event": "incident_window_end",
                    "post_trigger_seconds": round((end_ns - trigger_ns) / 1_000_000_000, 3),
                }, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as exc:
            print(f"[PM16C audit] Could not write incident log: {exc}")
        finally:
            with self._incident_lock:
                self._active_incidents.pop(trigger.get("channel", "unknown"), None)
            self._cleanup_old_logs()

    def _writer_loop(self) -> None:
        try:
            assert self._path is not None
            base_path = self._path
            path = base_path
            fh = path.open("a", encoding="utf-8", newline="\n", buffering=1)
            try:
                while True:
                    try:
                        item = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        if not self._running:
                            break
                        continue
                    if item is None:
                        break
                    fh.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    if fh.tell() >= MAX_AUDIT_FILE_BYTES:
                        fh.close()
                        self._compress(path)
                        self._cleanup_old_logs()
                        self._part += 1
                        path = base_path.with_name(
                            f"{base_path.stem}_part{self._part:03d}{base_path.suffix}"
                        )
                        self._path = path
                        fh = path.open("a", encoding="utf-8", newline="\n", buffering=1)
            finally:
                if not fh.closed:
                    fh.close()
        except Exception as exc:
            self.enabled = False
            print(f"[PM16C audit] Audit writer failed: {exc}")

    @staticmethod
    def _compress(path: Path) -> None:
        try:
            with path.open("rb") as src, gzip.open(str(path) + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
        except Exception as exc:
            print(f"[PM16C audit] Could not compress {path}: {exc}")

    def _cleanup_old_logs(self) -> None:
        now = time.time()
        policies = (
            (self.base_dir / "sessions", SESSION_RETENTION_DAYS, MAX_SESSION_TOTAL_BYTES),
            (self.base_dir / "incidents", INCIDENT_RETENTION_DAYS, MAX_INCIDENT_TOTAL_BYTES),
        )
        for root, days, max_total_bytes in policies:
            if not root.exists():
                continue
            cutoff = now - days * 86400
            files = []
            for path in root.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                    else:
                        files.append(path)
                except OSError:
                    pass
            try:
                files = sorted(
                    (path for path in files if path.exists()),
                    key=lambda path: path.stat().st_mtime,
                )
                total = sum(path.stat().st_size for path in files)
                for path in files:
                    if total <= max_total_bytes:
                        break
                    size = path.stat().st_size
                    path.unlink()
                    total -= size
            except OSError:
                pass


class StageStateMonitor:
    """One background state monitor and shared cache per controller."""

    def __init__(
        self,
        query_status: Callable[[int], str],
        parse_status: Callable[[str], tuple],
        audit: PM16CAuditLogger,
        *,
        channels: Iterable[int] = MONITORED_CHANNELS,
        idle_interval: float = IDLE_POLL_INTERVAL_S,
        moving_interval: float = MOVING_POLL_INTERVAL_S,
    ):
        self._query_status = query_status
        self._parse_status = parse_status
        self.audit = audit
        self.channels = tuple(channels)
        self.idle_interval = idle_interval
        self.moving_interval = moving_interval
        self._states: dict[int, ChannelState] = {}
        self._expected: dict[int, ExpectedMotion] = {}
        self._stopping: dict[int, ExpectedStop] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures: dict[int, int] = {}
        self._retry_after: dict[int, float] = {}
        now = time.monotonic()
        self._next_snapshot = now + SNAPSHOT_INTERVAL_S
        self._initial_snapshot_written = False
        self._health_started = now
        self._health_poll_count = 0
        self._health_success_count = 0
        self._health_failure_count = 0
        self._health_latencies_ms: list[float] = []

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="PM16C-state-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.5)
        self._thread = None

    def note_motion(
        self,
        channel: int,
        command: str,
        target: int | None,
        source: str,
        *,
        relative_delta: int | None = None,
    ) -> str:
        operation_id = f"move-{uuid.uuid4().hex[:12]}"
        superseded_stop = None
        with self._lock:
            superseded_stop = self._stopping.pop(channel, None)
            if relative_delta is not None:
                previous_expected = self._expected.get(channel)
                state = self._states.get(channel)
                if previous_expected is not None and previous_expected.target is not None:
                    base_position = previous_expected.target
                elif state is not None:
                    base_position = state.position
                else:
                    base_position = None
                target = (
                    base_position + relative_delta
                    if base_position is not None else None
                )
            self._expected[channel] = ExpectedMotion(
                operation_id=operation_id,
                command=command,
                target=target,
                source=source,
                started_monotonic=time.monotonic(),
            )
        if superseded_stop is not None:
            self.audit.record(
                "stop_superseded_by_motion_command",
                level="WARNING",
                channel=channel,
                stop_command=superseded_stop.command,
                stop_operation_id=superseded_stop.stop_operation_id,
                new_motion_command=command,
                new_motion_operation_id=operation_id,
                source=source,
            )
        self.audit.record(
            "motion_command_sent",
            persist=False,
            channel=channel,
            command=command,
            target=target,
            source=source,
            operation_id=operation_id,
        )
        self._wake.set()
        return operation_id

    def note_stop(
        self,
        command: str,
        source: str,
        channels: Iterable[int] | None = None,
    ) -> str:
        """Mark channels as stopping until each one explicitly reports ``S``."""
        now = time.monotonic()
        stop_operation_id = f"stop-{uuid.uuid4().hex[:12]}"
        with self._lock:
            selected = self.channels if channels is None else tuple(channels)
            for channel in selected:
                state = self._states.get(channel)
                expected = self._expected.get(channel)
                # A stop confirmation is more urgent than an earlier monitor
                # retry backoff; force one fresh observation immediately.
                self._retry_after.pop(channel, None)
                self._stopping[channel] = ExpectedStop(
                    stop_operation_id=stop_operation_id,
                    command=command,
                    source=source,
                    requested_monotonic=now,
                    deadline_monotonic=now + STOP_CONFIRM_TIMEOUT_S,
                    position_at_request=state.position if state else None,
                    motion_state_at_request=state.motion_state if state else None,
                    motion_operation_id=expected.operation_id if expected else None,
                    motion_command=expected.command if expected else None,
                )
        self._wake.set()
        return stop_operation_id

    def observe(self, line: str, *, source: str) -> ChannelState:
        mode, channel_hex, motion, ls_hold, status, position_str = self._parse_status(line)
        channel = int(channel_hex, 16)
        now_mono = time.monotonic()
        now_text = datetime.now().astimezone().isoformat(timespec="milliseconds")
        current = ChannelState(
            channel=channel,
            position=int(position_str),
            motion_state=motion,
            mode=mode,
            ls_hold=ls_hold,
            status_byte=status,
            observed_monotonic=now_mono,
            observed_at=now_text,
            source=source,
        )
        incident = None
        completed = None
        stop_confirmed = None
        with self._lock:
            previous = self._states.get(channel)
            expected = self._expected.get(channel)
            stopping = self._stopping.get(channel)
            self._states[channel] = current

            if stopping is not None:
                if motion in ("P", "N") and expected is not None:
                    expected.seen_moving = True
                elif motion == "S":
                    self._stopping.pop(channel, None)
                    self._expected.pop(channel, None)
                    stop_confirmed = stopping
            elif expected is not None:
                if motion in ("P", "N"):
                    expected.seen_moving = True
                elif motion == "S" and (
                    current.position == expected.target
                    or (expected.target is None and expected.seen_moving)
                    or (
                        expected.target is None
                        and previous is not None
                        and previous.position != current.position
                    )
                    or (expected.seen_moving and now_mono - expected.started_monotonic >= 1.0)
                ):
                    self._expected.pop(channel, None)
                    completed = expected
                elif (
                    motion == "S"
                    and not expected.seen_moving
                    and now_mono - expected.started_monotonic >= 2.0
                ):
                    self._expected.pop(channel, None)
                    self.audit.record(
                        "motion_not_started",
                        level="ERROR",
                        channel=channel,
                        command=expected.command,
                        target=expected.target,
                        observed_position=current.position,
                        source=expected.source,
                        operation_id=expected.operation_id,
                    )

            if previous is not None and previous.position != current.position:
                delta = current.position - previous.position
                if stopping is not None:
                    incident = self.audit.record(
                        "position_change_during_stop",
                        channel=channel,
                        old_position=previous.position,
                        new_position=current.position,
                        delta=delta,
                        position_at_stop_request=stopping.position_at_request,
                        delta_since_stop_request=(
                            current.position - stopping.position_at_request
                            if stopping.position_at_request is not None else None
                        ),
                        old_motion_state=previous.motion_state,
                        new_motion_state=current.motion_state,
                        controller_mode=current.mode,
                        source=source,
                        stop_command=stopping.command,
                        stop_operation_id=stopping.stop_operation_id,
                        motion_operation_id=stopping.motion_operation_id,
                        motion_command=stopping.motion_command,
                        stop_source=stopping.source,
                    )
                else:
                    explained = expected is not None
                    event = "explained_position_change" if explained else "unexplained_position_change"
                    level = "INFO" if explained else "CRITICAL"
                    incident = self.audit.record(
                        event,
                        level=level,
                        persist=not explained,
                        channel=channel,
                        old_position=previous.position,
                        new_position=current.position,
                        delta=delta,
                        old_motion_state=previous.motion_state,
                        new_motion_state=current.motion_state,
                        controller_mode=current.mode,
                        source=source,
                        operation_id=expected.operation_id if expected else None,
                        last_local_motion_command=expected.command if expected else None,
                    )
            # The raw STSx transaction is already present in the audit log.
            # Emit a parsed observation only for the baseline or a change, so
            # one-second idle monitoring does not duplicate unchanged data.
            if previous is None or (
                previous.position != current.position
                or previous.motion_state != current.motion_state
                or previous.mode != current.mode
                or previous.ls_hold != current.ls_hold
                or previous.status_byte != current.status_byte
            ):
                self.audit.record(
                    "position_observation",
                    persist=False,
                    channel=channel,
                    position=current.position,
                    motion_state=current.motion_state,
                    mode=current.mode,
                    ls_hold=current.ls_hold,
                    status_byte=current.status_byte,
                    source=source,
                )
            snapshot = dict(self._states)

        if completed is not None:
            self.audit.record(
                "motion_complete",
                channel=channel,
                command=completed.command,
                target=completed.target,
                final_position=current.position,
                duration_ms=round((now_mono - completed.started_monotonic) * 1000, 3),
                source=completed.source,
                operation_id=completed.operation_id,
            )
        if stop_confirmed is not None:
            self.audit.record(
                "stop_confirmed",
                channel=channel,
                stop_command=stop_confirmed.command,
                stop_operation_id=stop_confirmed.stop_operation_id,
                position_at_request=stop_confirmed.position_at_request,
                final_position=current.position,
                delta_after_stop_request=(
                    current.position - stop_confirmed.position_at_request
                    if stop_confirmed.position_at_request is not None else None
                ),
                confirmation_latency_ms=round(
                    (now_mono - stop_confirmed.requested_monotonic) * 1000, 3
                ),
                motion_command=stop_confirmed.motion_command,
                motion_operation_id=stop_confirmed.motion_operation_id,
                source=stop_confirmed.source,
            )
        if incident is not None and incident["level"] == "CRITICAL":
            self.audit.write_incident(incident, snapshot)
        return current

    def _record_snapshot_if_due(self, now: float) -> None:
        with self._lock:
            states = dict(self._states)
        complete = len(states) == len(self.channels)
        if not complete:
            return
        reason = None
        if not self._initial_snapshot_written:
            reason = "initial"
            self._initial_snapshot_written = True
        elif now >= self._next_snapshot:
            reason = "periodic"
        if reason is None:
            return
        self._next_snapshot = now + SNAPSHOT_INTERVAL_S
        self.audit.record(
            "position_snapshot",
            reason=reason,
            channels={
                str(channel): {
                    "position": state.position,
                    "motion_state": state.motion_state,
                    "mode": state.mode,
                    "ls_hold": state.ls_hold,
                    "status_byte": state.status_byte,
                }
                for channel, state in sorted(states.items())
            },
        )

    def _record_health_if_due(self, now: float) -> None:
        if now - self._health_started < HEALTH_SUMMARY_INTERVAL_S:
            return
        latencies = sorted(self._health_latencies_ms)

        def percentile(fraction: float) -> float | None:
            if not latencies:
                return None
            index = min(len(latencies) - 1, round((len(latencies) - 1) * fraction))
            return round(latencies[index], 3)

        self.audit.record(
            "monitor_health_summary",
            interval_seconds=round(now - self._health_started, 3),
            poll_count=self._health_poll_count,
            success_count=self._health_success_count,
            failure_count=self._health_failure_count,
            response_latency_p50_ms=percentile(0.50),
            response_latency_p95_ms=percentile(0.95),
            response_latency_max_ms=round(latencies[-1], 3) if latencies else None,
        )
        self._health_started = now
        self._health_poll_count = 0
        self._health_success_count = 0
        self._health_failure_count = 0
        self._health_latencies_ms.clear()

    def get_state(self, channel: int, *, max_age: float | None = None) -> ChannelState | None:
        with self._lock:
            state = self._states.get(channel)
        if state is None:
            return None
        if max_age is not None and time.monotonic() - state.observed_monotonic > max_age:
            return None
        return replace(state)

    def get_states(self, channels: Iterable[int] | None = None, *, max_age: float | None = None) -> dict[int, ChannelState]:
        selected = self.channels if channels is None else tuple(channels)
        result = {}
        for channel in selected:
            state = self.get_state(channel, max_age=max_age)
            if state is not None:
                result[channel] = state
        return result

    def is_moving_cached(self) -> bool:
        with self._lock:
            return (
                bool(self._expected)
                or bool(self._stopping)
                or any(s.motion_state in ("P", "N") for s in self._states.values())
            )

    def _is_stale(self, channel: int, now: float) -> bool:
        with self._lock:
            state = self._states.get(channel)
            expected = channel in self._expected
            stopping = self._stopping.get(channel)
            retry_after = self._retry_after.get(channel, 0.0)
        if now < retry_after:
            return False
        if stopping is not None and (
            state is None or state.observed_monotonic < stopping.requested_monotonic
        ):
            return True
        interval = (
            self.moving_interval
            if expected or stopping is not None or (state and state.motion_state in ("P", "N"))
            else self.idle_interval
        )
        return state is None or now - state.observed_monotonic >= interval

    def _expire_stop_expectations(self, now: float) -> None:
        expired = []
        with self._lock:
            for channel, stopping in list(self._stopping.items()):
                if now < stopping.deadline_monotonic:
                    continue
                self._stopping.pop(channel, None)
                self._expected.pop(channel, None)
                state = self._states.get(channel)
                snapshot = dict(self._states)
                failure_count = self._failures.get(channel, 0)
                expired.append((channel, stopping, state, snapshot, failure_count))

        channels_by_operation: dict[str, list[int]] = {}
        for channel, stopping, _state, _snapshot, _failure_count in expired:
            channels_by_operation.setdefault(stopping.stop_operation_id, []).append(channel)

        incident_operations = set()
        for channel, stopping, state, snapshot, failure_count in expired:
            trigger = self.audit.record(
                "stop_not_confirmed",
                level="CRITICAL",
                channel=channel,
                stop_command=stopping.command,
                stop_operation_id=stopping.stop_operation_id,
                timeout_seconds=STOP_CONFIRM_TIMEOUT_S,
                position_at_request=stopping.position_at_request,
                last_position=state.position if state else None,
                last_motion_state=state.motion_state if state else None,
                last_observed_at=state.observed_at if state else None,
                monitor_failure_count=failure_count,
                timed_out_channels=channels_by_operation[stopping.stop_operation_id],
                motion_command=stopping.motion_command,
                motion_operation_id=stopping.motion_operation_id,
                source=stopping.source,
            )
            if (
                trigger is not None
                and stopping.stop_operation_id not in incident_operations
            ):
                incident_operations.add(stopping.stop_operation_id)
                self.audit.write_incident(trigger, snapshot)

    def _run(self) -> None:
        cycle = 0
        while not self._stop.is_set():
            cycle += 1
            self._expire_stop_expectations(time.monotonic())
            for channel in self.channels:
                if self._stop.is_set():
                    break
                if not self._is_stale(channel, time.monotonic()):
                    continue
                try:
                    query_started = time.monotonic()
                    self._query_status(channel)
                    latency_ms = (time.monotonic() - query_started) * 1000
                    self._health_poll_count += 1
                    self._health_success_count += 1
                    self._health_latencies_ms.append(latency_ms)
                    with self._lock:
                        self._failures.pop(channel, None)
                        self._retry_after.pop(channel, None)
                except Exception as exc:
                    # The broad catch is intentional: monitoring must never
                    # terminate the application or reinterpret an error as a
                    # stopped motor.  The concrete exception is audited.
                    self._health_poll_count += 1
                    self._health_failure_count += 1
                    with self._lock:
                        failures = self._failures.get(channel, 0) + 1
                        self._failures[channel] = failures
                        self._retry_after[channel] = time.monotonic() + min(5.0, 0.25 * (2 ** (failures - 1)))
                    self.audit.record(
                        "monitor_query_failed",
                        level="ERROR",
                        poll_cycle_id=cycle,
                        channel=channel,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                # Never hold the communication lock for a whole 11-channel
                # sweep; yield between transactions so motion/stop commands
                # can acquire it.
                if self._stop.wait(0.005):
                    break
            now = time.monotonic()
            self._record_snapshot_if_due(now)
            self._record_health_if_due(now)
            self._wake.wait(0.05)
            self._wake.clear()
