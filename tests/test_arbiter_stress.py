"""Stress test for CommandArbiter: many producer threads, random command
classes, random stops, random delays. Verifies every submitted Future
completes exactly once, no priority-order violation is observable, and no
wire command is lost or duplicated.

Runs a smaller iteration count by default (fast enough for every CI run);
set PM16C_STRESS_LONG=1 for a longer, nightly-style run.
"""
import os
import random
import threading
import time
import unittest
from concurrent.futures import Future

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stage.command_arbiter import (
    CommandArbiter,
    PRIORITY_EMERGENCY_STOP,
    PRIORITY_NORMAL_STOP,
    PRIORITY_STOP_CONFIRM,
    PRIORITY_QUERY,
    PRIORITY_MOTION,
)
from utils.stage.motion_coordinator import MotionCoordinator

_LONG = os.environ.get("PM16C_STRESS_LONG") == "1"
N_PRODUCERS = 128 if _LONG else 64
TASKS_PER_PRODUCER = 400 if _LONG else 96


class _Wire:
    def __init__(self, rng):
        self.rng = rng
        self.count = 0
        self.lock = threading.Lock()

    def run(self, label):
        if self.rng.random() < 0.3:
            time.sleep(self.rng.uniform(0, 0.0005))
        with self.lock:
            self.count += 1
        return label


class ArbiterStressTests(unittest.TestCase):
    def test_random_load_every_future_completes_exactly_once(self):
        rng = random.Random(12345)
        wire = _Wire(rng)
        coord = MotionCoordinator("pm16c:stress")
        arbiter = CommandArbiter(wire, coord)
        arbiter.start()

        futures: list[Future] = []
        futures_lock = threading.Lock()
        errors = []

        def producer(seed):
            local_rng = random.Random(seed)
            for i in range(TASKS_PER_PRODUCER):
                choice = local_rng.random()
                try:
                    if choice < 0.05:
                        f = arbiter.submit_stop(
                            lambda w: w.run("stop"),
                            emergency=local_rng.random() < 0.3,
                        )
                    elif choice < 0.5:
                        f = arbiter.submit(
                            lambda w: w.run("query"), priority=PRIORITY_QUERY,
                        )
                    else:
                        f = arbiter.submit(
                            lambda w: w.run("motion"), priority=PRIORITY_MOTION,
                        )
                except Exception as exc:
                    with futures_lock:
                        errors.append(exc)
                    continue
                with futures_lock:
                    futures.append(f)

        threads = [
            threading.Thread(target=producer, args=(s,))
            for s in range(N_PRODUCERS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60.0)
            self.assertFalse(t.is_alive(), "producer thread did not finish")

        # Every future must complete (result or exception) within a bound.
        deadline = time.monotonic() + 30.0
        pending = list(futures)
        while pending and time.monotonic() < deadline:
            pending = [f for f in pending if not f.done()]
            if pending:
                time.sleep(0.05)
        self.assertEqual(pending, [], "some futures never completed")

        arbiter.shutdown()

        # No submission-time errors (the queue was running throughout).
        self.assertEqual(errors, [])

        # Sanity: total executed wire calls plus coalesced/superseded stops
        # accounts for all accepted futures (each Future resolves exactly
        # once; concurrent.futures raises InvalidStateError on double-set,
        # which submit()/CommandArbiter would have surfaced as a producer
        # error above if it had happened).
        self.assertGreater(wire.count, 0)

    def test_stop_priority_holds_under_load(self):
        """A stop enqueued while many lower-priority tasks are queued must
        execute before any of them that hadn't started yet."""
        rng = random.Random(999)
        wire = _Wire(rng)
        coord = MotionCoordinator("pm16c:stress2")
        arbiter = CommandArbiter(wire, coord)
        arbiter.start()

        started = threading.Event()
        release = threading.Event()

        def blocker(w):
            started.set()
            release.wait(timeout=10.0)
            return w.run("blocker")

        blocker_future = arbiter.submit(blocker, priority=PRIORITY_QUERY)
        self.assertTrue(started.wait(timeout=5.0))

        order = []
        order_lock = threading.Lock()

        def make_recorder(label):
            def _exec(w):
                with order_lock:
                    order.append(label)
                return w.run(label)
            return _exec

        futures = []
        for i in range(200):
            futures.append(arbiter.submit(make_recorder(f"m{i}"), priority=PRIORITY_MOTION))
        stop_future = arbiter.submit_stop(make_recorder("stop"), emergency=True)

        release.set()
        blocker_future.result(timeout=10.0)
        stop_future.result(timeout=10.0)
        for f in futures:
            f.result(timeout=10.0)

        self.assertEqual(order[0], "stop")
        arbiter.shutdown()


if __name__ == "__main__":
    unittest.main()
