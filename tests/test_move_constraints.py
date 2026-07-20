"""Tests for utils/stage/move_constraints.py — the shared pure MOVE_CONSTRAINTS
evaluator introduced in Phase 4 of apps/exp_scheduler/REORGANISATION_PLAN.md
to replace four independent copies of the same matching loop
(PM16CController, PM16CControllerSim, and two functions in
apps.exp_scheduler.validator.pre_validator).

PureEvaluatorTests exercises the module directly with plain dicts/callables
— no fake device needed, matching the Phase 4 completion condition "抽出した
pure rule は fake device なしで unit test できる". RealVsSimParityTests then
confirms PM16CController (via FakeTransport) and PM16CControllerSim, which
both now delegate to this module, still allow/block the same scenarios.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stage import move_constraints
from utils.stage.control_stage import PM16CController, CH9_CH8_SAFE_BOUNDARY
from utils.stage.control_stage_sim import PM16CControllerSim
from tests.fake_transport import FakeTransport, MemoryAudit, default_responder


class PureEvaluatorTests(unittest.TestCase):
    """No controller, simulator, or socket involved anywhere below."""

    # ---- check_move (first-violation-stops; used by both controllers) ----

    def test_allows_ch9_out_direction_regardless_of_ch8(self):
        ok, msg = move_constraints.check_move(
            9, CH9_CH8_SAFE_BOUNDARY, lambda ch: "1"
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_blocks_ch9_into_beam_while_ch8_in(self):
        ok, msg = move_constraints.check_move(
            9, CH9_CH8_SAFE_BOUNDARY + 1,
            lambda ch: "1" if ch == 8 else "0",
        )
        self.assertFalse(ok)
        self.assertIn("Move blocked: Ch9", msg)
        self.assertIn("Ch8", msg)

    def test_allows_ch9_into_beam_while_ch8_out(self):
        ok, _ = move_constraints.check_move(
            9, CH9_CH8_SAFE_BOUNDARY + 1, lambda ch: "0"
        )
        self.assertTrue(ok)

    def test_blocks_ch8_into_beam_while_ch9_in(self):
        ok, msg = move_constraints.check_move(
            8, 1,
            lambda ch: str(CH9_CH8_SAFE_BOUNDARY + 1) if ch == 9 else "0",
        )
        self.assertFalse(ok)
        self.assertIn("Move blocked: Ch8", msg)

    def test_allows_ch8_out_direction_regardless_of_ch9(self):
        ok, _ = move_constraints.check_move(
            8, 0, lambda ch: str(CH9_CH8_SAFE_BOUNDARY + 1)
        )
        self.assertTrue(ok)

    def test_ignores_channel_with_no_rules(self):
        ok, msg = move_constraints.check_move(4, 999_999, lambda ch: None)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_fail_closed_when_required_channel_unreadable(self):
        ok, msg = move_constraints.check_move(
            9, CH9_CH8_SAFE_BOUNDARY + 1, lambda ch: None
        )
        self.assertFalse(ok)
        self.assertIn("Cannot read Ch8 position", msg)
        self.assertIn("required for limit check on Ch9", msg)

    # ---- list_move_violations (all violations; PreValidator step sim) ----

    def test_list_move_violations_matches_check_move_message(self):
        positions = {8: 1}
        read_pos = lambda ch: (str(positions[ch]) if ch in positions else None)
        violations = move_constraints.list_move_violations(
            positions, 9, CH9_CH8_SAFE_BOUNDARY + 1
        )
        ok, msg = move_constraints.check_move(
            9, CH9_CH8_SAFE_BOUNDARY + 1, read_pos
        )
        self.assertFalse(ok)
        self.assertEqual(violations, [msg])

    def test_list_move_violations_empty_when_safe(self):
        self.assertEqual(
            move_constraints.list_move_violations(
                {8: 0}, 9, CH9_CH8_SAFE_BOUNDARY + 1
            ),
            [],
        )

    def test_list_move_violations_fail_closed_on_missing_required_channel(self):
        violations = move_constraints.list_move_violations(
            {}, 9, CH9_CH8_SAFE_BOUNDARY + 1
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("Cannot read Ch8 position", violations[0])

    # ---- list_snapshot_violations (self-consistency of a full snapshot) ----

    def test_snapshot_violations_flags_inconsistent_snapshot(self):
        # Ch9 already in the beam (> boundary) while Ch8 is also in (>0):
        # inconsistent with MOVE_CONSTRAINTS even with no move proposed.
        # Both rules see their own target_ch in violation, so this reports
        # two messages (one per rule direction) — matches the pre-Phase-4
        # _violates_move_constraints(), which also evaluated every rule
        # independently rather than deduplicating by channel pair.
        positions = {8: 1, 9: CH9_CH8_SAFE_BOUNDARY + 1}
        violations = move_constraints.list_snapshot_violations(positions)
        self.assertEqual(len(violations), 2)
        self.assertTrue(any(v.startswith("Ch9=") for v in violations), violations)
        self.assertTrue(any(v.startswith("Ch8=") for v in violations), violations)

    def test_snapshot_violations_empty_for_consistent_snapshot(self):
        positions = {8: 1, 9: CH9_CH8_SAFE_BOUNDARY}
        self.assertEqual(move_constraints.list_snapshot_violations(positions), [])

    def test_snapshot_violations_ignores_channels_with_no_rules(self):
        self.assertEqual(move_constraints.list_snapshot_violations({}), [])

    def test_snapshot_violations_is_fail_closed_on_unreadable_companion_channel(self):
        # Ch8 alone (IN the beam) with no Ch9 entry in the snapshot: cannot
        # confirm Ch9 is safely out, so this is a violation rather than a
        # silent skip. (In production, PreValidator always collects all 11
        # channels before calling this — see
        # validator/pre_validator.py::_check_stage_move_constraints — so
        # this path was previously unreachable there; unifying it to
        # fail-closed here matches PM16CController/PM16CControllerSim's
        # existing behaviour for an unreadable required channel.)
        violations = move_constraints.list_snapshot_violations({8: 1})
        self.assertEqual(len(violations), 1)
        self.assertIn("Cannot read Ch9 position", violations[0])


def _make_real_controller(responder=None):
    c = PM16CController("127.0.0.1", 7777)
    mem = MemoryAudit()
    c.audit = mem
    c.coordinator._audit = mem
    c.arbiter._audit = mem
    c.state_monitor.audit = mem
    c.client = FakeTransport(responder)
    c.arbiter.start()
    c._confirm_thread = threading.Thread(target=c._stop_confirm_loop, daemon=True)
    c._confirm_thread.start()
    return c


def _teardown_real_controller(c):
    c._confirm_queue.put(None)
    c.arbiter.shutdown()


def _responder_with_ch8(ch8_pos: int):
    def responder(cmd):
        if cmd == "STS8?":
            return f"R8S000{ch8_pos:+08d}"
        return default_responder(cmd)
    return responder


class RealVsSimParityTests(unittest.TestCase):
    """Identical Ch8/Ch9 scenarios against PM16CController (FakeTransport)
    and PM16CControllerSim — both now delegate to move_constraints.check_move,
    so allow/block outcomes must agree exactly (Phase 4 completion
    condition: "現行4実装に対するcharacterization/parity testが、同じ
    allow/block結果と安全上重要なerror情報を保つ")."""

    def test_ch9_into_beam_blocked_on_both_when_ch8_in(self):
        real = _make_real_controller(_responder_with_ch8(1))
        sim = PM16CControllerSim()
        with sim._state_lock:
            sim._positions[8] = 1
        try:
            ok_real, msg_real = real.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY + 1)
            ok_sim, msg_sim = sim.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY + 1)
            self.assertFalse(ok_real)
            self.assertFalse(ok_sim)
            self.assertIn("Move blocked: Ch9", msg_real)
            self.assertIn("Move blocked: Ch9", msg_sim)
        finally:
            _teardown_real_controller(real)

    def test_ch9_out_direction_allowed_on_both_regardless_of_ch8(self):
        real = _make_real_controller(_responder_with_ch8(1))
        sim = PM16CControllerSim()
        with sim._state_lock:
            sim._positions[8] = 1
        try:
            ok_real, _ = real.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY)
            ok_sim, _ = sim.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY)
            self.assertTrue(ok_real)
            self.assertTrue(ok_sim)
        finally:
            _teardown_real_controller(real)

    def test_ch9_into_beam_allowed_on_both_when_ch8_out(self):
        real = _make_real_controller(_responder_with_ch8(0))
        sim = PM16CControllerSim()  # Ch8 defaults to 0 (out) at startup
        try:
            ok_real, _ = real.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY + 1)
            ok_sim, _ = sim.check_move_constraints(9, CH9_CH8_SAFE_BOUNDARY + 1)
            self.assertTrue(ok_real)
            self.assertTrue(ok_sim)
        finally:
            _teardown_real_controller(real)


class Phase4ContractTests(unittest.TestCase):
    """Encodes the private-import completion condition from
    REORGANISATION_PLAN.md Phase 4: pre_validator.py must not import
    runner.py's or control_stage.py's private names (particularly _OPS)."""

    def test_pre_validator_module_has_no_leftover_private_move_constraint_names(self):
        from apps.exp_scheduler.validator import pre_validator
        for name in ("_OPS", "MOVE_CONSTRAINTS", "_validate_ch11_oscillation_settings",
                     "_violates_move_constraints", "_violates_move_constraints_for_move"):
            self.assertNotIn(
                name, vars(pre_validator),
                f"pre_validator should no longer define/import {name!r} — "
                "MOVE_CONSTRAINTS evaluation now goes through "
                "utils.stage.move_constraints (Phase 4)",
            )

    def test_pre_validator_does_not_import_runner_module(self):
        import inspect
        from apps.exp_scheduler.validator import pre_validator
        source = inspect.getsource(pre_validator)
        self.assertNotIn("from ..runner import", source)
        self.assertNotIn("from apps.exp_scheduler.runner import", source)


if __name__ == "__main__":
    unittest.main()
