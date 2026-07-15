"""Deterministic tests for utils/stage/command_arbiter.py.

The technique throughout: hold the comm thread inside an in-flight task with
a gate Event, enqueue the scenario's tasks in a known order, then release the
gate and assert the executed order.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stage.command_arbiter import (
    CommandArbiter,
    PRIORITY_EMERGENCY_STOP,
    PRIORITY_NORMAL_STOP,
    PRIORITY_STOP_CONFIRM,
    PRIORITY_QUERY,
    PRIORITY_MOTION,
)
from utils.stage.errors import PM16CQueueClosedError, MotionRevokedError
from utils.stage.motion_coordinator import MotionCoordinator


class _Recorder:
    """Stands in for the wire executor; records execution order."""

    def __init__(self):
        self.executed = []
        self.lock = threading.Lock()

    def run(self, label):
        with self.lock:
            self.executed.append(label)
        return label


class ArbiterTestBase(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self.coord = MotionCoordinator("pm16c:test")
        self.arbiter = CommandArbiter(self.rec, self.coord)
        self.arbiter.start()

    def tearDown(self):
        self.arbiter.shutdown()

    def _task(self, label):
        return lambda wire: wire.run(label)

    def _gated_task(self, label, started: threading.Event, gate: threading.Event):
        def execute(wire):
            started.set()
            gate.wait(timeout=5.0)
            return wire.run(label)
        return execute


class PriorityOrderingTests(ArbiterTestBase):
    def test_stop_overtakes_queued_lower_priority_tasks(self):
        started, gate = threading.Event(), threading.Event()
        f0 = self.arbiter.submit(
            self._gated_task("inflight", started, gate),
            priority=PRIORITY_QUERY, command="STS4?",
        )
        self.assertTrue(started.wait(timeout=2.0))
        # Queue up motion and queries, THEN a stop.
        f1 = self.arbiter.submit(self._task("motion1"), priority=PRIORITY_MOTION)
        f2 = self.arbiter.submit(self._task("query1"), priority=PRIORITY_QUERY)
        f3 = self.arbiter.submit_stop(self._task("stop"), emergency=False)
        gate.set()
        for f in (f0, f1, f2, f3):
            f.result(timeout=5.0)
        # In-flight completes first (never preempted); the stop beats every
        # task that had not been dequeued yet.
        self.assertEqual(self.rec.executed[0], "inflight")
        self.assertEqual(self.rec.executed[1], "stop")
        self.assertEqual(set(self.rec.executed[2:]), {"motion1", "query1"})

    def test_emergency_beats_normal_stop_and_confirm_beats_query(self):
        started, gate = threading.Event(), threading.Event()
        self.arbiter.submit(
            self._gated_task("inflight", started, gate), priority=PRIORITY_QUERY
        )
        self.assertTrue(started.wait(timeout=2.0))
        fq = self.arbiter.submit(self._task("query"), priority=PRIORITY_QUERY)
        fc = self.arbiter.submit(self._task("confirm"), priority=PRIORITY_STOP_CONFIRM)
        fe = self.arbiter.submit(self._task("emergency"), priority=PRIORITY_EMERGENCY_STOP)
        fn = self.arbiter.submit(self._task("normal"), priority=PRIORITY_NORMAL_STOP)
        gate.set()
        for f in (fq, fc, fe, fn):
            f.result(timeout=5.0)
        self.assertEqual(
            self.rec.executed,
            ["inflight", "emergency", "normal", "confirm", "query"],
        )

    def test_fifo_within_same_priority(self):
        started, gate = threading.Event(), threading.Event()
        self.arbiter.submit(
            self._gated_task("inflight", started, gate), priority=PRIORITY_QUERY
        )
        self.assertTrue(started.wait(timeout=2.0))
        futures = [
            self.arbiter.submit(self._task(f"q{i}"), priority=PRIORITY_QUERY)
            for i in range(5)
        ]
        gate.set()
        for f in futures:
            f.result(timeout=5.0)
        self.assertEqual(self.rec.executed[1:], [f"q{i}" for i in range(5)])


class StopCoalescingTests(ArbiterTestBase):
    def test_repeated_normal_stops_share_one_future_and_one_wire_command(self):
        started, gate = threading.Event(), threading.Event()
        self.arbiter.submit(
            self._gated_task("inflight", started, gate), priority=PRIORITY_QUERY
        )
        self.assertTrue(started.wait(timeout=2.0))
        f1 = self.arbiter.submit_stop(self._task("stop"), emergency=False)
        f2 = self.arbiter.submit_stop(self._task("stop-dup"), emergency=False)
        f3 = self.arbiter.submit_stop(self._task("stop-dup2"), emergency=False)
        self.assertIs(f1, f2)
        self.assertIs(f1, f3)
        gate.set()
        f1.result(timeout=5.0)
        self.assertEqual(self.rec.executed.count("stop"), 1)
        self.assertNotIn("stop-dup", self.rec.executed)

    def test_emergency_supersedes_queued_normal_stop(self):
        started, gate = threading.Event(), threading.Event()
        self.arbiter.submit(
            self._gated_task("inflight", started, gate), priority=PRIORITY_QUERY
        )
        self.assertTrue(started.wait(timeout=2.0))
        fn = self.arbiter.submit_stop(self._task("normal-stop"), emergency=False)
        fe = self.arbiter.submit_stop(self._task("emergency-stop"), emergency=True)
        gate.set()
        self.assertEqual(fe.result(timeout=5.0), "emergency-stop")
        # The normal stop's wire command never ran; its Future carries the
        # emergency stop's outcome.
        self.assertEqual(fn.result(timeout=5.0), "emergency-stop")
        self.assertNotIn("normal-stop", self.rec.executed)

    def test_normal_stop_after_emergency_attaches_to_emergency(self):
        started, gate = threading.Event(), threading.Event()
        self.arbiter.submit(
            self._gated_task("inflight", started, gate), priority=PRIORITY_QUERY
        )
        self.assertTrue(started.wait(timeout=2.0))
        fe = self.arbiter.submit_stop(self._task("emergency-stop"), emergency=True)
        fn = self.arbiter.submit_stop(self._task("late-normal"), emergency=False)
        self.assertIs(fe, fn)
        gate.set()
        self.assertEqual(fn.result(timeout=5.0), "emergency-stop")
        self.assertNotIn("late-normal", self.rec.executed)


class LeaseRevalidationTests(ArbiterTestBase):
    def test_queued_motion_rejected_after_revocation(self):
        lease = self.coord.acquire("A", "scan")
        started, gate = threading.Event(), threading.Event()
        self.arbiter.submit(
            self._gated_task("inflight", started, gate), priority=PRIORITY_QUERY
        )
        self.assertTrue(started.wait(timeout=2.0))
        f_motion = self.arbiter.submit(
            self._task("motion"), priority=PRIORITY_MOTION, lease=lease
        )
        # Stop request revokes the lease while the motion task is queued.
        self.coord.revoke_for_stop(source="B", emergency=False)
        gate.set()
        with self.assertRaises(MotionRevokedError):
            f_motion.result(timeout=5.0)
        self.assertNotIn("motion", self.rec.executed)

    def test_valid_lease_executes(self):
        lease = self.coord.acquire("A", "scan")
        f = self.arbiter.submit(
            self._task("motion"), priority=PRIORITY_MOTION, lease=lease
        )
        self.assertEqual(f.result(timeout=5.0), "motion")


class ShutdownTests(ArbiterTestBase):
    def test_shutdown_drains_pending_futures(self):
        started, gate = threading.Event(), threading.Event()
        f0 = self.arbiter.submit(
            self._gated_task("inflight", started, gate), priority=PRIORITY_QUERY
        )
        self.assertTrue(started.wait(timeout=2.0))
        pending = [
            self.arbiter.submit(self._task(f"t{i}"), priority=PRIORITY_MOTION)
            for i in range(3)
        ]
        # Shutdown from another thread while a task is in flight.
        t = threading.Thread(target=self.arbiter.shutdown)
        t.start()
        gate.set()
        t.join(timeout=5.0)
        f0.result(timeout=5.0)  # in-flight task still completed
        for f in pending:
            with self.assertRaises(PM16CQueueClosedError):
                f.result(timeout=5.0)

    def test_submit_after_shutdown_raises(self):
        self.arbiter.shutdown()
        with self.assertRaises(PM16CQueueClosedError):
            self.arbiter.submit(self._task("x"), priority=PRIORITY_QUERY)
        with self.assertRaises(PM16CQueueClosedError):
            self.arbiter.submit_stop(self._task("x"), emergency=True)


if __name__ == "__main__":
    unittest.main()
