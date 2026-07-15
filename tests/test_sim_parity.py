"""Real-vs-simulator behavioural parity for the motion-lease API.

The same scenario script runs against PM16CController(FakeTransport) and
PM16CControllerSim; lease states, exception types, and outcomes must match.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stage.control_stage import PM16CController
from utils.stage.control_stage_sim import PM16CControllerSim
from utils.stage.motion_coordinator import LeaseState
from utils.stage.errors import (
    MotionLeaseRequiredError,
    MotionNotAvailableError,
    MotionRevokedError,
)
from tests.fake_transport import FakeTransport, MemoryAudit


def make_real():
    c = PM16CController("127.0.0.1", 7777)
    mem = MemoryAudit()
    c.audit = mem
    c.coordinator._audit = mem
    c.arbiter._audit = mem
    c.state_monitor.audit = mem
    c.client = FakeTransport()
    c.arbiter.start()
    c._confirm_thread = threading.Thread(
        target=c._stop_confirm_loop, daemon=True
    )
    c._confirm_thread.start()
    return c


def teardown_real(c):
    c._confirm_queue.put(None)
    c.arbiter.shutdown()


def make_sim():
    return PM16CControllerSim()


class ParityTests(unittest.TestCase):
    """Each test runs the identical script against both controllers."""

    def controllers(self):
        real = make_real()
        sim = make_sim()
        try:
            yield "real", real
            yield "sim", sim
        finally:
            teardown_real(real)

    def test_move_without_lease_raises_identically(self):
        for name, c in self.controllers():
            with self.subTest(controller=name):
                with self.assertRaises(MotionLeaseRequiredError):
                    c.move_ch_absolute(4, 100)
                with self.assertRaises(MotionLeaseRequiredError):
                    c.move_ch_relative(4, 10)
                with self.assertRaises(MotionLeaseRequiredError):
                    c.move_ch_relative_unchecked(4, 10)
                with self.assertRaises(MotionLeaseRequiredError):
                    c.set_ch_speed(4, "H")
                with self.assertRaises(MotionLeaseRequiredError):
                    c.switch_to_rem()
                with self.assertRaises(MotionLeaseRequiredError):
                    c.wait_until_stop(confirm_count=1)

    def test_raw_console_bypass_impossible_on_both(self):
        for name, c in self.controllers():
            with self.subTest(controller=name):
                with self.assertRaises(MotionLeaseRequiredError):
                    c.send_cmd("ABS1+100", has_response=False)
                with self.assertRaises(MotionLeaseRequiredError):
                    c.send_cmd("REL4+10", has_response=False)
                with self.assertRaises(MotionLeaseRequiredError):
                    c.send_cmd("SPDH4", has_response=False)

    def test_second_owner_rejected_with_holder_info(self):
        for name, c in self.controllers():
            with self.subTest(controller=name):
                lease = c.acquire_motion("App A", "long scan")
                with self.assertRaises(MotionNotAvailableError) as ctx:
                    c.acquire_motion("App B", "quick move")
                self.assertEqual(ctx.exception.holder["owner"], "App A")
                self.assertEqual(ctx.exception.holder["operation"], "long scan")
                c.release_motion(lease)
                # Now available again.
                lease2 = c.acquire_motion("App B", "quick move")
                self.assertEqual(lease2.owner, "App B")
                c.release_motion(lease2)

    def test_stop_revokes_and_owner_release_completes_handover(self):
        for name, c in self.controllers():
            with self.subTest(controller=name):
                lease = c.acquire_motion("App A", "scan")
                fut = c.request_normal_stop(source="App B")
                self.assertTrue(fut.result(timeout=10.0))
                self.assertEqual(c.get_stop_progress(), "confirmed")
                # Owner's next motion fails.
                with self.assertRaises(MotionRevokedError):
                    c.move_ch_absolute(4, 100, motion=lease)
                # Still owned (grace) until the owner releases.
                with self.assertRaises(MotionNotAvailableError):
                    c.acquire_motion("App B", "next")
                self.assertTrue(c.release_motion(lease))
                self.assertEqual(c.coordinator.state(), LeaseState.FREE)

    def test_stale_release_is_noop_on_both(self):
        for name, c in self.controllers():
            with self.subTest(controller=name):
                lease_a = c.acquire_motion("A", "op")
                c.release_motion(lease_a)
                lease_b = c.acquire_motion("B", "op")
                self.assertFalse(c.release_motion(lease_a))
                self.assertTrue(c.coordinator.is_valid(lease_b))
                c.release_motion(lease_b)

    def test_motion_session_releases_on_exception(self):
        for name, c in self.controllers():
            with self.subTest(controller=name):
                with self.assertRaises(RuntimeError):
                    with c.motion_session("A", "failing op"):
                        raise RuntimeError("worker crashed")
                self.assertTrue(c.is_motion_available())

    def test_recover_motion_parity(self):
        for name, c in self.controllers():
            with self.subTest(controller=name):
                c.acquire_motion("A", "stuck scan")
                fut = c.recover_motion(source="operator")
                self.assertTrue(fut.result(timeout=10.0))
                self.assertEqual(c.coordinator.state(), LeaseState.FREE)
                lease = c.acquire_motion("B", "after recovery")
                self.assertIsNotNone(lease)
                c.release_motion(lease)


if __name__ == "__main__":
    unittest.main()
