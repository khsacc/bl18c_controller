"""Integration tests: PM16CController + CommandArbiter + FakeTransport.

The controller is wired to a FakeTransport (no real socket) with the arbiter
and stop-confirmation thread running, so these tests exercise the real
transaction/stop/lease paths end to end.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stage.control_stage import PM16CController, CH9_CH8_SAFE_BOUNDARY
from utils.stage.errors import (
    MotionLeaseRequiredError,
    MotionRevokedError,
    MotionNotAvailableError,
    PM16CQueueClosedError,
)
from utils.stage.motion_coordinator import LeaseState
from tests.fake_transport import FakeTransport, MemoryAudit, default_responder


def make_controller(responder=None, *, send_gate=None, fail_fn=None):
    c = PM16CController("127.0.0.1", 7777)
    mem = MemoryAudit()
    c.audit = mem
    c.coordinator._audit = mem
    c.arbiter._audit = mem
    c.state_monitor.audit = mem
    c.client = FakeTransport(responder, send_gate=send_gate, fail_fn=fail_fn)
    c.arbiter.start()
    c._confirm_thread = threading.Thread(
        target=c._stop_confirm_loop, daemon=True
    )
    c._confirm_thread.start()
    return c


def teardown_controller(c):
    c._confirm_queue.put(None)
    c.arbiter.shutdown()


class ControllerArbiterTestBase(unittest.TestCase):
    responder = None

    def setUp(self):
        self.c = make_controller(self.responder)
        self.transport: FakeTransport = self.c.client

    def tearDown(self):
        teardown_controller(self.c)


class TransactionTests(ControllerArbiterTestBase):
    def test_absolute_move_wire_order(self):
        lease = self.c.acquire_motion("test", "abs move")
        self.c.move_ch_absolute(4, 100, motion=lease)
        self.assertEqual(self.transport.sent, ["STS4?", "REM", "ABS4+100"])

    def test_relative_move_wire_order(self):
        lease = self.c.acquire_motion("test", "rel move")
        self.c.move_ch_relative(4, -50, motion=lease)
        self.assertEqual(self.transport.sent, ["STS4?", "REM", "REL4-50"])

    def test_constraint_read_happens_on_wire(self):
        # Ch9 into the beam requires reading Ch8 (constraint) before moving.
        lease = self.c.acquire_motion("test", "ch9 move")
        self.c.move_ch_absolute(9, CH9_CH8_SAFE_BOUNDARY + 1, motion=lease)
        # STS8? (constraint), STS9? (max-move read), REM, ABS9...
        self.assertEqual(self.transport.sent[0], "STS8?")
        self.assertIn("REM", self.transport.sent)
        self.assertEqual(self.transport.sent[-1],
                         f"ABS9{CH9_CH8_SAFE_BOUNDARY + 1:+}")

    def test_constraint_violation_blocks_before_rem(self):
        def responder(cmd):
            if cmd == "STS8?":
                return "R8S000+0000001"  # Ch8 IN the beam
            return default_responder(cmd)
        c2 = make_controller(responder)
        try:
            lease = c2.acquire_motion("test", "ch9 blocked")
            with self.assertRaises(ValueError):
                c2.move_ch_absolute(9, CH9_CH8_SAFE_BOUNDARY + 1, motion=lease)
            self.assertNotIn("REM", c2.client.sent)
            self.assertFalse(
                any(s.startswith("ABS") for s in c2.client.sent)
            )
        finally:
            teardown_controller(c2)

    def test_move_without_lease_raises_and_sends_nothing(self):
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.move_ch_absolute(4, 100)
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.move_ch_relative(4, 10)
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.set_ch_speed(4, "H")
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.switch_to_rem()
        self.assertEqual(self.transport.sent, [])

    def test_raw_console_cannot_bypass_lease(self):
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.send_cmd("ABS1+100", has_response=False)
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.send_cmd("REM", has_response=False)
        self.assertEqual(self.transport.sent, [])
        # Queries pass without a lease.
        self.assertEqual(self.c.get_ch_pos(4), "+0000000")

    def test_mid_transaction_revocation_aborts_before_motion_command(self):
        blocked = threading.Event()
        release = threading.Event()

        def gate(cmd):
            if cmd == "STS4?":
                blocked.set()
                release.wait(timeout=5.0)

        c2 = make_controller(send_gate=gate)
        try:
            lease = c2.acquire_motion("A", "scan")
            result = {}

            def do_move():
                try:
                    c2.move_ch_absolute(4, 100, motion=lease)
                except Exception as exc:
                    result["exc"] = exc

            t = threading.Thread(target=do_move)
            t.start()
            self.assertTrue(blocked.wait(timeout=2.0))
            # Stop request from "another app" while STS4? is in flight.
            c2.coordinator.revoke_for_stop(source="B", emergency=False)
            release.set()
            t.join(timeout=5.0)
            self.assertIsInstance(result.get("exc"), MotionRevokedError)
            # The motion command never reached the wire.
            self.assertEqual(c2.client.sent, ["STS4?"])
        finally:
            release.set()
            teardown_controller(c2)

    def test_speed_change_transaction(self):
        lease = self.c.acquire_motion("test", "speed")
        self.c.set_ch_speed(4, "H", motion=lease)
        self.assertEqual(self.transport.sent, ["REM", "SPDH4", "LOC"])
        self.transport.sent.clear()
        self.c.set_ch_speed(4, "L", stay_in_rem=True, motion=lease)
        self.assertEqual(self.transport.sent, ["REM", "SPDL4"])


class StopTests(ControllerArbiterTestBase):
    def test_normal_stop_is_atomic_and_confirms(self):
        fut = self.c.request_normal_stop(source="test")
        self.assertTrue(fut.result(timeout=10.0))
        self.assertEqual(self.transport.sent[0], "ASSTP")
        self.assertEqual(self.transport.sent[1], "LOC")
        self.assertTrue(
            all(s == "STQ?" for s in self.transport.sent[2:])
        )
        self.assertEqual(self.c.get_stop_progress(), "confirmed")

    def test_stop_revokes_lease_and_owner_move_fails(self):
        lease = self.c.acquire_motion("A", "scan")
        fut = self.c.request_emergency_stop(source="B")
        self.assertTrue(fut.result(timeout=10.0))
        self.assertEqual(self.transport.sent[0], "AESTP")
        with self.assertRaises(MotionRevokedError):
            self.c.move_ch_absolute(4, 100, motion=lease)
        # Owner has not released: still within grace, new acquire refused.
        with self.assertRaises(MotionNotAvailableError):
            self.c.acquire_motion("B", "next")
        # Owner's finally releases; motion becomes available.
        self.assertTrue(self.c.release_motion(lease))
        lease2 = self.c.acquire_motion("B", "next")
        self.assertIsNotNone(lease2)

    def test_queries_allowed_during_confirmation_motion_rejected(self):
        lease = self.c.acquire_motion("A", "scan")
        slow = threading.Event()

        # Delay confirmation by making STQ? report a moving motor twice.
        replies = {"n": 0}
        def responder(cmd):
            if cmd == "STQ?":
                replies["n"] += 1
                return "R3" if replies["n"] <= 2 else "R4"
            return default_responder(cmd)
        c2 = make_controller(responder)
        try:
            lease = c2.acquire_motion("A", "scan")
            fut = c2.request_normal_stop(source="B")
            # While confirmation is in progress: queries OK, motion rejected.
            time.sleep(0.15)
            self.assertEqual(c2.get_ch_pos(4), "+0000000")
            with self.assertRaises(MotionRevokedError):
                c2.move_ch_absolute(4, 100, motion=lease)
            self.assertTrue(fut.result(timeout=10.0))
        finally:
            teardown_controller(c2)
        self.assertIsNotNone(slow)  # silence lint on unused event

    def test_typed_aestp_redirects_into_stop_path(self):
        lease = self.c.acquire_motion("A", "scan")
        result = self.c.send_cmd("AESTP", has_response=False)
        self.assertTrue(result)
        self.assertEqual(self.transport.sent[0], "AESTP")
        self.assertEqual(self.transport.sent[1], "LOC")
        self.assertFalse(self.c.coordinator.is_valid(lease))

    def test_stop_send_failure_enters_recovery_required(self):
        def fail(cmd):
            if cmd == "ASSTP":
                return OSError("wire broken")
            return None
        c2 = make_controller(fail_fn=fail)
        try:
            c2.acquire_motion("A", "scan")
            fut = c2.request_normal_stop(source="B")
            with self.assertRaises(OSError):
                fut.result(timeout=10.0)
            self.assertEqual(c2.coordinator.state(),
                             LeaseState.RECOVERY_REQUIRED)
            with self.assertRaises(Exception):
                c2.acquire_motion("B", "next")
        finally:
            teardown_controller(c2)

    def test_recover_motion_frees_ownership(self):
        self.c.acquire_motion("A", "scan")
        t = self.c.coordinator.revoke_for_stop(source="B", emergency=True)
        self.c.coordinator.note_stop_send_failed(t)
        self.assertEqual(self.c.coordinator.state(),
                         LeaseState.RECOVERY_REQUIRED)
        fut = self.c.recover_motion(source="dev console")
        self.assertTrue(fut.result(timeout=10.0))
        self.assertEqual(self.c.coordinator.state(), LeaseState.FREE)
        lease = self.c.acquire_motion("B", "after recovery")
        self.assertIsNotNone(lease)


class UncheckedFastPathTests(ControllerArbiterTestBase):
    def test_unchecked_returns_before_send_completes(self):
        release = threading.Event()

        def gate(cmd):
            if cmd.startswith("REL"):
                release.wait(timeout=5.0)

        c2 = make_controller(send_gate=gate)
        try:
            lease = c2.acquire_motion("radicon", "rotation loop")
            t0 = time.perf_counter()
            c2.move_ch_relative_unchecked(11, 25, motion=lease)
            elapsed = time.perf_counter() - t0
            # Returned while the send is still gated: enqueue-only latency.
            self.assertLess(elapsed, 0.05)
            self.assertEqual(c2.client.sent, [])
            release.set()
            time.sleep(0.2)
            self.assertEqual(c2.client.sent, ["RELB+25"])
            self.assertIsNone(c2.last_async_error)
        finally:
            release.set()
            teardown_controller(c2)

    def test_unchecked_send_failure_lands_in_last_async_error(self):
        def fail(cmd):
            if cmd.startswith("REL"):
                return OSError("send failed")
            return None
        c2 = make_controller(fail_fn=fail)
        try:
            lease = c2.acquire_motion("radicon", "rotation loop")
            c2.move_ch_relative_unchecked(11, 25, motion=lease)
            time.sleep(0.3)
            self.assertIsInstance(c2.last_async_error, OSError)
            self.assertIn("async_command_failed", c2.audit.names())
        finally:
            teardown_controller(c2)

    def test_unchecked_without_lease_raises(self):
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.move_ch_relative_unchecked(11, 25)


class WaitAndShutdownTests(ControllerArbiterTestBase):
    def test_wait_until_stop_requires_lease_for_loc(self):
        with self.assertRaises(MotionLeaseRequiredError):
            self.c.wait_until_stop(confirm_count=1)

    def test_wait_until_stop_with_lease_sends_loc(self):
        lease = self.c.acquire_motion("test", "wait")
        self.c.wait_until_stop(confirm_count=1, motion=lease)
        self.assertEqual(self.transport.sent[-1], "LOC")

    def test_wait_until_stop_stay_in_rem_needs_no_lease(self):
        self.c.wait_until_stop(confirm_count=1, stay_in_rem=True)
        self.assertNotIn("LOC", self.transport.sent)

    def test_wait_skips_loc_when_lease_revoked(self):
        lease = self.c.acquire_motion("test", "wait")
        self.c.coordinator.revoke_for_stop(source="B", emergency=False)
        self.c.wait_until_stop(confirm_count=1, motion=lease)  # must not raise
        self.assertNotIn("LOC", self.transport.sent)

    def test_disconnect_drains_pending_futures(self):
        release = threading.Event()

        def gate(cmd):
            if cmd == "STS4?":
                release.wait(timeout=5.0)

        c2 = make_controller(send_gate=gate)
        result = {}

        def query():
            try:
                result["pos"] = c2.get_ch_pos(4)
            except Exception as exc:
                result["exc"] = exc

        t1 = threading.Thread(target=query)
        t1.start()
        time.sleep(0.1)
        pending = c2.arbiter.submit(lambda w: "never", priority=4)
        shutdown_thread = threading.Thread(
            target=lambda: c2.arbiter.shutdown()
        )
        shutdown_thread.start()
        release.set()
        shutdown_thread.join(timeout=5.0)
        t1.join(timeout=5.0)
        with self.assertRaises(PM16CQueueClosedError):
            pending.result(timeout=5.0)
        c2._confirm_queue.put(None)


if __name__ == "__main__":
    unittest.main()
