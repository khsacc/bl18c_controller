"""Unit tests for utils/stage/motion_coordinator.py (memory-only, no I/O)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stage.motion_coordinator import (
    MotionCoordinator,
    MotionLease,
    LeaseState,
    DEFAULT_GRACE_PERIOD_S,
)
from utils.stage.errors import (
    MotionLeaseError,
    MotionLeaseRequiredError,
    MotionNotAvailableError,
    MotionRevokedError,
    MotionRecoveryRequiredError,
)


class _MemoryAudit:
    def __init__(self):
        self.events = []

    def record(self, event, **fields):
        self.events.append({"event": event, **fields})
        return self.events[-1]

    def names(self):
        return [e["event"] for e in self.events]


class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CoordinatorTestBase(unittest.TestCase):
    def setUp(self):
        self.audit = _MemoryAudit()
        self.clock = _FakeClock()
        self.coord = MotionCoordinator(
            "pm16c:test", self.audit, grace_period_s=5.0, clock=self.clock
        )


class AcquireReleaseTests(CoordinatorTestBase):
    def test_acquire_from_free(self):
        lease = self.coord.acquire("2D Scan", "grid scan")
        self.assertEqual(self.coord.state(), LeaseState.HELD)
        self.assertTrue(self.coord.is_valid(lease))
        self.assertEqual(lease.generation, 1)
        self.assertIn("motion_acquired", self.audit.names())

    def test_acquire_while_held_raises_immediately(self):
        self.coord.acquire("A", "op-a")
        with self.assertRaises(MotionNotAvailableError) as ctx:
            self.coord.acquire("B", "op-b")
        self.assertEqual(ctx.exception.holder["owner"], "A")
        self.assertIn("motion_rejected", self.audit.names())

    def test_release_returns_true_and_frees(self):
        lease = self.coord.acquire("A", "op")
        self.assertTrue(self.coord.release(lease))
        self.assertEqual(self.coord.state(), LeaseState.FREE)
        # A new owner can acquire with a bumped generation.
        lease2 = self.coord.acquire("B", "op")
        self.assertEqual(lease2.generation, 2)

    def test_double_release_is_safe_noop(self):
        lease = self.coord.acquire("A", "op")
        self.assertTrue(self.coord.release(lease))
        self.assertFalse(self.coord.release(lease))
        self.assertIn("stale_motion_release_ignored", self.audit.names())

    def test_wrong_controller_lease_release_is_noop(self):
        self.coord.acquire("A", "op")
        foreign = MotionLease("pm16c:other", "lease-x", 1, "A", "op")
        self.assertFalse(self.coord.release(foreign))
        self.assertEqual(self.coord.state(), LeaseState.HELD)

    def test_same_owner_name_different_lease_cannot_release(self):
        lease = self.coord.acquire("A", "op")
        impostor = MotionLease(
            "pm16c:test", "lease-fake", lease.generation, "A", "op"
        )
        self.assertFalse(self.coord.release(impostor))
        self.assertTrue(self.coord.is_valid(lease))

    def test_acquire_with_timeout_waits_for_release(self):
        import threading
        lease = self.coord.acquire("A", "op")
        acquired = {}

        def waiter():
            acquired["lease"] = self.coord.acquire("B", "op-b", timeout=5.0)

        t = threading.Thread(target=waiter)
        t.start()
        import time
        time.sleep(0.05)
        self.coord.release(lease)
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(acquired["lease"].owner, "B")


class ValidateTests(CoordinatorTestBase):
    def test_validate_none_raises_lease_required(self):
        with self.assertRaises(MotionLeaseRequiredError):
            self.coord.validate(None)

    def test_validate_wrong_controller(self):
        foreign = MotionLease("pm16c:other", "lease-x", 1, "A", "op")
        with self.assertRaises(MotionLeaseError):
            self.coord.validate(foreign)

    def test_validate_active_lease_passes(self):
        lease = self.coord.acquire("A", "op")
        self.coord.validate(lease)  # must not raise

    def test_validate_after_release_raises_revoked(self):
        lease = self.coord.acquire("A", "op")
        self.coord.release(lease)
        with self.assertRaises(MotionRevokedError):
            self.coord.validate(lease)

    def test_stale_generation_cannot_validate(self):
        lease1 = self.coord.acquire("A", "op")
        self.coord.release(lease1)
        self.coord.acquire("B", "op")
        with self.assertRaises(MotionRevokedError):
            self.coord.validate(lease1)


class StopLifecycleTests(CoordinatorTestBase):
    def test_revoke_invalidates_lease_immediately(self):
        lease = self.coord.acquire("A", "scan")
        ticket = self.coord.revoke_for_stop(source="B", emergency=False)
        self.assertEqual(ticket["revoked_lease_id"], lease.lease_id)
        self.assertEqual(self.coord.state(), LeaseState.REVOKED_STOPPING)
        self.assertFalse(self.coord.is_valid(lease))
        with self.assertRaises(MotionRevokedError):
            self.coord.validate(lease)
        self.assertIn("motion_revoked", self.audit.names())

    def test_stop_on_free_still_returns_ticket(self):
        ticket = self.coord.revoke_for_stop(source="B", emergency=True)
        self.assertIsNone(ticket["revoked_lease_id"])
        self.assertEqual(self.coord.state(), LeaseState.FREE)

    def test_confirmed_stop_enters_grace_then_reclaims(self):
        self.coord.acquire("A", "scan")
        ticket = self.coord.revoke_for_stop(source="B", emergency=False)
        self.coord.note_stop_confirmed(ticket)
        self.assertEqual(self.coord.state(), LeaseState.REVOKED_STOPPED_GRACE)
        # New acquire refused during grace.
        with self.assertRaises(MotionNotAvailableError):
            self.coord.acquire("B", "op-b")
        # After the grace period the lease is reclaimed lazily.
        self.clock.advance(5.1)
        lease2 = self.coord.acquire("B", "op-b")
        self.assertEqual(lease2.owner, "B")
        self.assertIn("motion_lease_reclaimed", self.audit.names())

    def test_owner_release_during_stopping_frees_at_confirmation(self):
        lease = self.coord.acquire("A", "scan")
        ticket = self.coord.revoke_for_stop(source="B", emergency=False)
        # Owner's finally runs while stop is still being confirmed.
        self.assertTrue(self.coord.release(lease))
        self.assertEqual(self.coord.state(), LeaseState.REVOKED_STOPPING)
        self.coord.note_stop_confirmed(ticket)
        self.assertEqual(self.coord.state(), LeaseState.FREE)

    def test_owner_release_during_grace_frees_immediately(self):
        lease = self.coord.acquire("A", "scan")
        ticket = self.coord.revoke_for_stop(source="B", emergency=False)
        self.coord.note_stop_confirmed(ticket)
        self.assertEqual(self.coord.state(), LeaseState.REVOKED_STOPPED_GRACE)
        self.assertTrue(self.coord.release(lease))
        self.assertEqual(self.coord.state(), LeaseState.FREE)

    def test_stop_send_failure_requires_recovery(self):
        self.coord.acquire("A", "scan")
        ticket = self.coord.revoke_for_stop(source="B", emergency=True)
        self.coord.note_stop_send_failed(ticket)
        self.assertEqual(self.coord.state(), LeaseState.RECOVERY_REQUIRED)
        with self.assertRaises(MotionRecoveryRequiredError):
            self.coord.acquire("B", "op-b")

    def test_stop_confirm_failure_requires_recovery(self):
        self.coord.acquire("A", "scan")
        ticket = self.coord.revoke_for_stop(source="B", emergency=False)
        self.coord.note_stop_sent(ticket)
        self.coord.note_stop_confirm_failed(ticket)
        self.assertEqual(self.coord.state(), LeaseState.RECOVERY_REQUIRED)

    def test_no_reclaim_without_confirmed_stop(self):
        self.coord.acquire("A", "scan")
        self.coord.revoke_for_stop(source="B", emergency=False)
        # Time passing alone must NOT free a lease whose stop was never
        # confirmed.
        self.clock.advance(60.0)
        self.assertEqual(self.coord.state(), LeaseState.REVOKED_STOPPING)
        with self.assertRaises(MotionNotAvailableError):
            self.coord.acquire("B", "op-b")

    def test_no_ttl_on_held(self):
        lease = self.coord.acquire("A", "long exposure")
        self.clock.advance(24 * 3600.0)
        self.assertTrue(self.coord.is_valid(lease))
        self.assertEqual(self.coord.state(), LeaseState.HELD)


class AbaRegressionTests(CoordinatorTestBase):
    def test_stale_release_after_reclaim_does_not_affect_new_holder(self):
        # A acquires, is revoked/stopped, grace expires, B acquires.
        lease_a = self.coord.acquire("A", "scan")
        ticket = self.coord.revoke_for_stop(source="B", emergency=False)
        self.coord.note_stop_confirmed(ticket)
        self.clock.advance(5.1)
        lease_b = self.coord.acquire("B", "op-b")
        gen_b = lease_b.generation
        # A's delayed finally arrives late.
        self.assertFalse(self.coord.release(lease_a))
        # B's ownership is completely untouched.
        self.assertEqual(self.coord.state(), LeaseState.HELD)
        self.assertTrue(self.coord.is_valid(lease_b))
        self.assertEqual(self.coord.holder_info()["lease_id"], lease_b.lease_id)
        self.assertEqual(self.coord.holder_info()["generation"], gen_b)
        self.assertIn("stale_motion_release_ignored", self.audit.names())

    def test_stale_lease_cannot_move_after_recovery(self):
        lease_a = self.coord.acquire("A", "scan")
        ticket = self.coord.force_recover_begin(source="operator")
        self.coord.force_recover_complete(True, source="operator")
        self.assertEqual(self.coord.state(), LeaseState.FREE)
        with self.assertRaises(MotionRevokedError):
            self.coord.validate(lease_a)
        # Generation was bumped: the next lease is strictly newer.
        lease_b = self.coord.acquire("B", "op-b")
        self.assertGreater(lease_b.generation, lease_a.generation + 0)
        self.assertIsNotNone(ticket)


class RecoveryTests(CoordinatorTestBase):
    def test_recovery_success_frees_and_bumps_generation(self):
        self.coord.acquire("A", "scan")
        self.coord.force_recover_begin(source="dev console")
        self.coord.force_recover_complete(True, source="dev console")
        self.assertEqual(self.coord.state(), LeaseState.FREE)
        names = self.audit.names()
        self.assertIn("motion_recovery_started", names)
        self.assertIn("motion_recovery_completed", names)

    def test_recovery_failure_stays_recovery_required(self):
        self.coord.acquire("A", "scan")
        self.coord.force_recover_begin(source="dev console")
        self.coord.force_recover_complete(False, source="dev console")
        self.assertEqual(self.coord.state(), LeaseState.RECOVERY_REQUIRED)
        self.assertIn("motion_recovery_failed", self.audit.names())
        with self.assertRaises(MotionRecoveryRequiredError):
            self.coord.acquire("B", "op")

    def test_recovery_allowed_from_recovery_required(self):
        self.coord.acquire("A", "scan")
        t = self.coord.revoke_for_stop(source="B", emergency=True)
        self.coord.note_stop_send_failed(t)
        self.assertEqual(self.coord.state(), LeaseState.RECOVERY_REQUIRED)
        self.coord.force_recover_begin(source="operator")
        self.coord.force_recover_complete(True, source="operator")
        self.assertEqual(self.coord.state(), LeaseState.FREE)


class IntrospectionTests(CoordinatorTestBase):
    def test_holder_info_and_availability(self):
        self.assertTrue(self.coord.is_available())
        self.assertIsNone(self.coord.holder_info())
        lease = self.coord.acquire("A", "op")
        self.assertFalse(self.coord.is_available())
        info = self.coord.holder_info()
        self.assertEqual(info["owner"], "A")
        self.assertEqual(info["lease_id"], lease.lease_id)


if __name__ == "__main__":
    unittest.main()
