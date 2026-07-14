import json
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch

from utils.stage.control_stage import PM16CController, _command_metadata, _parse_stsx_reply
from utils.stage.stage_monitor import (
    IDLE_POLL_INTERVAL_S,
    MOVING_POLL_INTERVAL_S,
    PM16CAuditLogger,
    StageStateMonitor,
)


class _FakeSocket:
    def __init__(self, *replies):
        self.replies = [f"{reply}\r\n".encode("ascii") for reply in replies]
        self.sent = []

    def sendall(self, payload):
        self.sent.append(payload)

    def recv(self, _size):
        if not self.replies:
            raise AssertionError("No fake response available")
        return self.replies.pop(0)


class _FailingSocket:
    def sendall(self, _payload):
        raise OSError("simulated send failure")


class _MemoryAudit:
    def __init__(self):
        self.events = []
        self.incidents = []

    def record(self, event, *, level="INFO", **fields):
        item = {"event": event, "level": level, **fields}
        self.events.append(item)
        return item

    def write_incident(self, trigger, states):
        self.incidents.append((trigger, dict(states)))


class StageStateMonitorTests(unittest.TestCase):
    def make_monitor(self, audit=None, **kwargs):
        return StageStateMonitor(
            lambda channel: "",
            _parse_stsx_reply,
            audit or _MemoryAudit(),
            **kwargs,
        )

    def test_uncommanded_position_change_creates_critical_incident(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.observe("L7S---+0120000", source="baseline")
        monitor.observe("L7S---+0000000", source="watchdog")

        events = [e for e in audit.events if e["event"] == "unexplained_position_change"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["channel"], 7)
        self.assertEqual(events[0]["delta"], -120000)
        self.assertEqual(events[0]["level"], "CRITICAL")
        self.assertEqual(len(audit.incidents), 1)

    def test_default_poll_intervals_are_coarse(self):
        monitor = self.make_monitor()
        self.assertEqual(IDLE_POLL_INTERVAL_S, 5.0)
        self.assertEqual(MOVING_POLL_INTERVAL_S, 1.0)
        self.assertEqual(monitor.idle_interval, 5.0)
        self.assertEqual(monitor.moving_interval, 1.0)

    def test_commanded_position_change_is_explained(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.observe("R9S000-0040000", source="baseline")
        monitor.note_motion(9, "ABS9-30000", -30000, "test")
        monitor.observe("R9P002-0035000", source="watchdog")
        monitor.observe("R9S000-0030000", source="watchdog")

        self.assertFalse(any(e["event"] == "unexplained_position_change" for e in audit.events))
        self.assertTrue(any(e["event"] == "explained_position_change" for e in audit.events))
        self.assertFalse(monitor.is_moving_cached())

    def test_consecutive_relative_moves_accumulate_expected_target(self):
        audit = _MemoryAudit()
        controller = PM16CController("127.0.0.1", 7777)
        controller.state_monitor.audit = audit
        controller.state_monitor.observe("R7S000+0000000", source="baseline")

        for command in ("REL7+100", "REL7+100"):
            controller._track_sent_command(
                command,
                _command_metadata(command),
                "development_console",
            )

        self.assertEqual(controller.state_monitor._expected[7].target, 200)
        targets = [
            event["target"] for event in audit.events
            if event["event"] == "motion_command_sent"
        ]
        self.assertEqual(targets, [100, 200])

    def test_relative_move_uses_pending_absolute_target(self):
        controller = PM16CController("127.0.0.1", 7777)
        controller.state_monitor.observe("R7S000+0000000", source="baseline")
        controller._track_sent_command(
            "ABS7+1000", _command_metadata("ABS7+1000"), "development_console"
        )
        controller._track_sent_command(
            "REL7+200", _command_metadata("REL7+200"), "development_console"
        )

        self.assertEqual(controller.state_monitor._expected[7].target, 1200)

    def test_deceleration_after_stop_is_not_an_unexplained_change(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.observe("L7S---+0000000", source="baseline")
        monitor.note_motion(7, "ABS7+1000", 1000, "test")
        monitor.observe("L7P---+0000100", source="watchdog")

        monitor.note_stop("SSTP7", "test", [7])
        monitor.observe("L7P---+0000150", source="watchdog")
        monitor.observe("L7S---+0000175", source="watchdog")

        self.assertFalse(any(
            event["event"] == "unexplained_position_change" for event in audit.events
        ))
        changes = [
            event for event in audit.events
            if event["event"] == "position_change_during_stop"
        ]
        self.assertEqual([event["delta"] for event in changes], [50, 25])
        confirmed = [event for event in audit.events if event["event"] == "stop_confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["final_position"], 175)
        self.assertEqual(confirmed[0]["delta_after_stop_request"], 75)
        self.assertFalse(any(event["event"] == "motion_not_started" for event in audit.events))
        self.assertFalse(monitor.is_moving_cached())

    def test_global_stop_waits_for_all_channels_to_report_stopped(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.note_stop("ASSTP", "test")

        self.assertEqual(set(monitor._stopping), set(range(1, 12)))
        self.assertTrue(monitor.is_moving_cached())
        for channel in range(1, 12):
            monitor.observe(f"L{channel:X}S---+0000000", source="watchdog")

        self.assertEqual(monitor._stopping, {})
        self.assertEqual(
            len([event for event in audit.events if event["event"] == "stop_confirmed"]),
            11,
        )
        self.assertFalse(monitor.is_moving_cached())

    def test_stop_tracks_cached_motion_without_expected_move(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.observe("L9P---+0001000", source="external")
        monitor.note_stop("ESTP9", "test", [9])
        monitor.observe("L9S---+0001010", source="watchdog")

        self.assertFalse(any(
            event["event"] == "unexplained_position_change" for event in audit.events
        ))
        self.assertTrue(any(
            event["event"] == "position_change_during_stop" for event in audit.events
        ))
        self.assertTrue(any(event["event"] == "stop_confirmed" for event in audit.events))

    def test_stop_timeout_restores_unexplained_change_detection(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.observe("L7P---+0000100", source="baseline")
        monitor.note_motion(7, "JOGP7", None, "test")
        monitor.note_stop("SSTP7", "test", [7])
        monitor._stopping[7].deadline_monotonic = time.monotonic() - 1

        monitor._expire_stop_expectations(time.monotonic())
        monitor.observe("L7P---+0000200", source="watchdog")

        timed_out = [event for event in audit.events if event["event"] == "stop_not_confirmed"]
        self.assertEqual(len(timed_out), 1)
        self.assertEqual(timed_out[0]["level"], "CRITICAL")
        self.assertGreaterEqual(len(audit.incidents), 1)
        self.assertEqual(audit.incidents[0][0]["event"], "stop_not_confirmed")
        self.assertTrue(any(
            event["event"] == "unexplained_position_change" for event in audit.events
        ))

    def test_global_stop_timeout_writes_one_incident_per_command(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.note_stop("AESTP", "test")
        for stopping in monitor._stopping.values():
            stopping.deadline_monotonic = time.monotonic() - 1

        monitor._expire_stop_expectations(time.monotonic())

        timed_out = [event for event in audit.events if event["event"] == "stop_not_confirmed"]
        self.assertEqual(len(timed_out), 11)
        self.assertEqual(len(audit.incidents), 1)
        self.assertEqual(timed_out[0]["timed_out_channels"], list(range(1, 12)))

    def test_emergency_stop_replaces_pending_normal_stop(self):
        monitor = self.make_monitor()
        motion_id = monitor.note_motion(7, "ABS7+1000", 1000, "test")
        monitor.note_stop("SSTP7", "test", [7])
        first_stop_id = monitor._stopping[7].stop_operation_id
        monitor.note_stop("ESTP7", "test", [7])

        stopping = monitor._stopping[7]
        self.assertNotEqual(stopping.stop_operation_id, first_stop_id)
        self.assertEqual(stopping.command, "ESTP7")
        self.assertEqual(stopping.motion_operation_id, motion_id)

    def test_new_motion_supersedes_pending_stop(self):
        audit = _MemoryAudit()
        monitor = self.make_monitor(audit)
        monitor.note_stop("ESTP7", "test", [7])
        operation_id = monitor.note_motion(7, "ABS7+1000", 1000, "test")

        self.assertNotIn(7, monitor._stopping)
        self.assertEqual(monitor._expected[7].operation_id, operation_id)
        self.assertTrue(any(
            event["event"] == "stop_superseded_by_motion_command"
            for event in audit.events
        ))

    def test_pending_stop_forces_immediate_status_query(self):
        monitor = self.make_monitor()
        monitor.observe("L7S---+0000000", source="baseline")
        monitor.note_stop("SSTP7", "test", [7])

        self.assertTrue(monitor._is_stale(7, time.monotonic()))

    def test_existing_observations_suppress_duplicate_background_poll(self):
        audit = _MemoryAudit()
        calls = []
        holder = {}

        def query(channel):
            calls.append(channel)
            ch = f"{channel:X}"
            holder["monitor"].observe(f"L{ch}S---+0000000", source="monitor")
            return ""

        monitor = StageStateMonitor(
            query,
            _parse_stsx_reply,
            audit,
            channels=(1, 2, 3),
            idle_interval=1.0,
            moving_interval=0.05,
        )
        holder["monitor"] = monitor

        # Simulate a UI query immediately before the monitor starts.  Only
        # channels 2 and 3 are stale and should be sent to the PM16C.
        monitor.observe("L1S---+0000000", source="stage_controller")
        monitor.start()
        deadline = time.time() + 1.0
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        monitor.stop()

        self.assertEqual(calls, [2, 3])

    def test_raw_position_preset_is_attributed_at_wire_boundary(self):
        controller = PM16CController("127.0.0.1", 7777)
        controller._track_sent_command(
            "PS7+0001000",
            _command_metadata("PS7+0001000"),
            "development_console",
        )
        self.assertTrue(controller.get_cached_is_moving())
        controller.state_monitor.observe("R7S000+0001000", source="watchdog")
        self.assertFalse(controller.get_cached_is_moving())

    def test_home_motion_commands_extract_channel(self):
        cases = (
            ("SCANHP7", "motion_continuous", 7),
            ("SCANHNB", "motion_continuous", 11),
            ("FDHP7", "motion_home_search", 7),
            ("GTHPB", "motion_home_return", 11),
        )
        for command, command_class, channel in cases:
            with self.subTest(command=command):
                metadata = _command_metadata(command)
                self.assertEqual(metadata["command_class"], command_class)
                self.assertEqual(metadata["channel"], channel)

    def test_speed_commands_extract_channel(self):
        cases = (
            ("SPDH7", "speed_change", 7),
            ("SPDM71000", "speed_change", 7),
            ("SPDLB", "speed_change", 11),
            ("SPD?7", "query", 7),
            ("SPDH?7", "query", 7),
            ("SPDM?B", "query", 11),
            ("SPDAL?", "query", None),
        )
        for command, command_class, channel in cases:
            with self.subTest(command=command):
                metadata = _command_metadata(command)
                self.assertEqual(metadata["command_class"], command_class)
                self.assertEqual(metadata["channel"], channel)

    def test_home_motion_from_raw_console_is_attributed(self):
        for command in ("SCANHP7", "SCANHN7", "FDHP7", "GTHP7"):
            with self.subTest(command=command):
                audit = _MemoryAudit()
                controller = PM16CController("127.0.0.1", 7777)
                controller.state_monitor.audit = audit
                controller.state_monitor.observe("R7S000+0000000", source="baseline")
                controller._track_sent_command(
                    command,
                    _command_metadata(command),
                    "development_console",
                )
                controller.state_monitor.observe("R7P000+0000100", source="watchdog")
                controller.state_monitor.observe("R7S000+0000200", source="watchdog")

                self.assertFalse(any(
                    event["event"] == "unexplained_position_change"
                    for event in audit.events
                ))
                self.assertTrue(any(
                    event["event"] == "motion_complete" for event in audit.events
                ))

    def test_home_configuration_commands_do_not_create_motion_expectation(self):
        for command in ("SETHP70100", "SHP7+0001000", "SHPF70010"):
            with self.subTest(command=command):
                controller = PM16CController("127.0.0.1", 7777)
                controller._track_sent_command(
                    command,
                    _command_metadata(command),
                    "development_console",
                )
                self.assertEqual(controller.state_monitor._expected, {})

    def test_successful_global_stop_enters_stop_confirmation(self):
        controller = PM16CController("127.0.0.1", 7777)
        controller.client = _FakeSocket()

        controller.send_cmd("ASSTP", has_response=False)

        self.assertEqual(set(controller.state_monitor._stopping), set(range(1, 12)))
        self.assertEqual(
            {stop.command for stop in controller.state_monitor._stopping.values()},
            {"ASSTP"},
        )

    def test_failed_stop_send_does_not_enter_stop_confirmation(self):
        controller = PM16CController("127.0.0.1", 7777)
        controller.client = _FailingSocket()

        with self.assertRaises(OSError):
            controller.send_cmd("AESTP", has_response=False)

        self.assertEqual(controller.state_monitor._stopping, {})


class AuditLoggerTests(unittest.TestCase):
    def test_jsonl_session_is_written(self):
        tmp = Path.cwd() / "__localdata" / "stage_audit_test"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            audit = PM16CAuditLogger(tmp)
            audit.start(controller_ip="127.0.0.1", controller_port=7777, simulation=False)
            audit.record("tx_attempt", command="STS7?")
            path = audit.path
            audit.stop()

            self.assertIsNotNone(path)
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["event"], "session_start")
            self.assertTrue(any(line["event"] == "tx_attempt" for line in lines))
            self.assertEqual(lines[-1]["event"], "session_stop")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_flight_recorder_event_is_not_written_to_session(self):
        tmp = Path.cwd() / "__localdata" / "stage_audit_trace_test"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            audit = PM16CAuditLogger(tmp)
            audit.start()
            audit.record("tx_attempt", persist=False, command="STS7?")
            path = audit.path
            audit.stop()

            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(line.get("command") == "STS7?" for line in lines))
            self.assertTrue(any(line.get("command") == "STS7?" for line in audit._ring))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sts_is_trace_only_but_control_command_is_persistent(self):
        tmp = Path.cwd() / "__localdata" / "stage_audit_command_test"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            audit = PM16CAuditLogger(tmp)
            audit.start()
            controller = PM16CController("127.0.0.1", 7777)
            controller.audit = audit
            controller.state_monitor.audit = audit
            controller.client = _FakeSocket("L7S---+0000000")

            controller.send_cmd("STS7?")
            controller.send_cmd("REM", has_response=False)
            path = audit.path
            audit.stop()

            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(line.get("command") == "STS7?" for line in lines))
            commands = [line for line in lines if line["event"] == "control_command"]
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]["command"], "REM")
            self.assertEqual(commands[0]["outcome"], "sent")
            self.assertEqual(commands[0]["importance"], "high")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_incident_contains_trace_only_history(self):
        tmp = Path.cwd() / "__localdata" / "stage_audit_incident_test"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            audit = PM16CAuditLogger(tmp)
            audit.record("tx_attempt", persist=False, command="STS9?")
            trigger = audit.record(
                "unexplained_position_change",
                level="CRITICAL",
                channel=9,
                old_position=0,
                new_position=100,
            )
            with patch("utils.stage.stage_monitor.INCIDENT_AFTER_S", 0):
                audit.write_incident(trigger, {})
                for thread in audit._incident_threads:
                    thread.join(timeout=2.0)

            paths = list((tmp / "incidents").glob("*.jsonl"))
            self.assertEqual(len(paths), 1)
            lines = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(line.get("command") == "STS9?" for line in lines))
            self.assertTrue(any(line["event"] == "incident_snapshot" for line in lines))
            self.assertTrue(any(line["event"] == "incident_window_end" for line in lines))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_concurrent_stop_does_not_leave_events_behind_writer_sentinel(self):
        tmp = Path.cwd() / "__localdata" / "stage_audit_stop_race_test"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        producers = []
        begin = threading.Event()
        finish = threading.Event()
        try:
            audit = PM16CAuditLogger(tmp)
            audit.start()

            def produce():
                begin.wait()
                while not finish.is_set():
                    audit.record("producer_event")
                    time.sleep(0.0001)

            producers = [threading.Thread(target=produce) for _ in range(4)]
            for producer in producers:
                producer.start()
            begin.set()
            time.sleep(0.01)
            path = audit.path
            audit.stop()
            finish.set()
            for producer in producers:
                producer.join(timeout=1.0)

            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[-1]["event"], "session_stop")
            stop_seq = lines[-1]["seq"]
            written_sequences = {line["seq"] for line in lines}
            with audit._ring_lock:
                accepted_before_stop = {
                    event["seq"] for event in audit._ring
                    if event["seq"] < stop_seq
                }
            self.assertTrue(accepted_before_stop.issubset(written_sequences))
            self.assertTrue(audit._queue.empty())
        finally:
            finish.set()
            for producer in producers:
                producer.join(timeout=1.0)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
