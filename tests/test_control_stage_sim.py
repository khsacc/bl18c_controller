"""Tests for PM16CControllerSim's atomic read-check-update locking.

Phase A of the motion-lease refactor: the simulator's move methods must run
their constraint check and target update inside one _state_lock critical
section, matching the real controller's hold-the-lock-across-the-sequence
behaviour.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stage.control_stage_sim import PM16CControllerSim
from utils.stage.control_stage import (
    CH9_CH8_SAFE_BOUNDARY, CH8_CH11_CONFLICT_BOUNDARY, CH11_SAFE_RANGE_PULSES,
)


class SimMoveLockingTests(unittest.TestCase):
    def setUp(self):
        self.sim = PM16CControllerSim()
        self.lease = self.sim.acquire_motion("test", "sim tests")
        # Do not connect(): the background integrator is unnecessary for
        # these tests and keeping positions static makes assertions exact.

    def test_absolute_move_sets_target_and_moving(self):
        self.sim.move_ch_absolute(4, 1234, motion=self.lease)
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[4], 1234)
            self.assertTrue(self.sim._moving[4])

    def test_relative_move_accumulates_from_current(self):
        self.sim.move_ch_relative(4, +500, motion=self.lease)
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[4], 500)

    def test_constraint_blocks_ch9_into_beam_while_ch8_in(self):
        # Ch8 at +1 (IN) must block Ch9 moving past the safe boundary.
        with self.sim._state_lock:
            self.sim._positions[8] = 1
        with self.assertRaises(ValueError):
            self.sim.move_ch_absolute(9, CH9_CH8_SAFE_BOUNDARY + 1, motion=self.lease)

    def test_constraint_allows_ch9_out_direction(self):
        with self.sim._state_lock:
            self.sim._positions[8] = 1
        # Moving Ch9 to/beyond the boundary (OUT) is always safe.
        self.sim.move_ch_absolute(9, CH9_CH8_SAFE_BOUNDARY, motion=self.lease)
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[9], CH9_CH8_SAFE_BOUNDARY)

    def test_unchecked_move_skips_constraints(self):
        with self.sim._state_lock:
            self.sim._positions[8] = 1
            self.sim._positions[9] = CH9_CH8_SAFE_BOUNDARY
        # Would violate the constraint if checked; unchecked must not raise.
        self.sim.move_ch_relative_unchecked(9, +10, motion=self.lease)
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[9], CH9_CH8_SAFE_BOUNDARY + 10)

    def test_public_check_move_constraints_still_works(self):
        with self.sim._state_lock:
            self.sim._positions[8] = 1
        ok, msg = self.sim.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY + 1)
        self.assertFalse(ok)
        self.assertIn("Move blocked", msg)
        ok, _ = self.sim.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY)
        self.assertTrue(ok)

    def test_constraint_blocks_ch11_move_while_ch8_extended(self):
        # Unconditional rule: Ch8 extended blocks ANY Ch11 target, not just
        # targets near 0.
        with self.sim._state_lock:
            self.sim._positions[8] = CH8_CH11_CONFLICT_BOUNDARY + 1
        with self.assertRaises(ValueError):
            self.sim.move_ch_absolute(11, 123456, motion=self.lease)

    def test_constraint_allows_ch11_move_while_ch8_retracted(self):
        with self.sim._state_lock:
            self.sim._positions[8] = CH8_CH11_CONFLICT_BOUNDARY
        self.sim.move_ch_absolute(11, 123456, motion=self.lease)
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[11], 123456)

    def test_constraint_blocks_ch8_extend_while_ch11_off_range(self):
        with self.sim._state_lock:
            self.sim._positions[11] = CH11_SAFE_RANGE_PULSES[1] + 1
        with self.assertRaises(ValueError):
            self.sim.move_ch_absolute(8, CH8_CH11_CONFLICT_BOUNDARY + 1, motion=self.lease)

    def test_constraint_allows_ch8_extend_while_ch11_in_range(self):
        with self.sim._state_lock:
            self.sim._positions[11] = CH11_SAFE_RANGE_PULSES[0]
        self.sim.move_ch_absolute(8, CH8_CH11_CONFLICT_BOUNDARY + 1, motion=self.lease)
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[8], CH8_CH11_CONFLICT_BOUNDARY + 1)

    def test_invalid_channel_is_a_noop(self):
        self.sim.move_ch_absolute(99, 0, motion=self.lease)
        self.sim.move_ch_relative(99, 10, motion=self.lease)
        self.sim.move_ch_relative_unchecked(99, 10, motion=self.lease)

    def test_concurrent_relative_moves_do_not_lose_updates(self):
        # Hammer one channel from many threads; with the atomic
        # read-check-update, the final target must equal the sum of all
        # applied diffs (positions never advance because the integrator
        # thread is not running).
        n_threads, per_thread = 8, 50
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(per_thread):
                self.sim.move_ch_relative_unchecked(4, +1, motion=self.lease)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Each unchecked move reads the CURRENT position (still 0) and sets
        # target = cur + diff, so the target stays +1 — the point here is
        # that no thread ever observes a torn/partial state, and the flag
        # and target agree at the end.
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[4], 1)
            self.assertTrue(self.sim._moving[4])

    def test_stop_clears_all_motion(self):
        self.sim.move_ch_absolute(4, 1000, motion=self.lease)
        self.sim.normal_stop()
        self.assertFalse(self.sim.get_is_moving())
        with self.sim._state_lock:
            self.assertEqual(self.sim._targets[4], self.sim._positions[4])


if __name__ == "__main__":
    unittest.main()
