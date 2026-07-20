"""
Unit tests for apps/exp_scheduler/validator/execution_trace.py —
REORGANISATION_PLAN.md Phase 5 (§7 Phase 5, §8.2/§8.3 test matrix).
"""
import sys
import types
import unittest

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )

from apps.exp_scheduler.actions import (
    ForLoopAction,
    SetAndWaitPressureAction,
    SetPressureAction,
    WaitAction,
    WaitPressureAction,
)
from apps.exp_scheduler.validator.execution_trace import (
    ExecutionTrace,
    LoopIteration,
    _PathNode,
    _path_str,
    child_path,
    compute_loop_stats,
    format_loop_context,
    loop_limit_messages,
)


class PathFormattingTests(unittest.TestCase):
    def test_child_path_top_level(self):
        self.assertEqual(child_path("", 2), "[2]")

    def test_child_path_nested(self):
        self.assertEqual(child_path("[2]", 1), "[2].body[1]")
        self.assertEqual(child_path("[2].body[1]", 0), "[2].body[1].body[0]")

    def test_path_node_str_matches_child_path_format(self):
        root = _PathNode(2, None)
        child = _PathNode(1, root)
        grandchild = _PathNode(0, child)
        self.assertEqual(_path_str(root), "[2]")
        self.assertEqual(_path_str(child), "[2].body[1]")
        self.assertEqual(_path_str(grandchild), "[2].body[1].body[0]")


class LoopContextFormattingTests(unittest.TestCase):
    def test_empty_chain_is_none(self):
        self.assertIsNone(format_loop_context(()))

    def test_single_iteration(self):
        chain = (LoopIteration("p", 1.0, 0),)
        self.assertEqual(format_loop_context(chain), "p=1.0[0]")

    def test_nested_chain_outermost_first(self):
        chain = (LoopIteration("i", 1.0, 0), LoopIteration("j", 2.0, 1))
        self.assertEqual(format_loop_context(chain), "i=1.0[0], j=2.0[1]")


class ComputeLoopStatsTests(unittest.TestCase):
    def test_empty_actions(self):
        stats = compute_loop_stats([])
        self.assertEqual(stats.total_steps, 0)
        self.assertEqual(stats.max_loop_iterations, 0)
        self.assertEqual(stats.max_nesting_depth, 0)
        self.assertFalse(stats.depth_truncated)
        self.assertTrue(stats.within_limits)

    def test_flat_actions_count_as_one_step_each(self):
        stats = compute_loop_stats([WaitAction(duration_s=1.0), WaitAction(duration_s=2.0)])
        self.assertEqual(stats.total_steps, 2)
        self.assertEqual(stats.max_nesting_depth, 0)

    def test_single_loop(self):
        stats = compute_loop_stats([
            ForLoopAction(var="p", values=[1.0, 2.0, 3.0], body=[WaitAction(duration_s=1.0)]),
        ])
        self.assertEqual(stats.total_steps, 3)
        self.assertEqual(stats.max_loop_iterations, 3)
        self.assertEqual(stats.max_nesting_depth, 1)
        self.assertFalse(stats.depth_truncated)

    def test_deep_nesting_is_truncated_not_overflowed(self):
        """A 100-level-deep ForLoopAction chain (far beyond
        _MAX_LOOP_NESTING_DEPTH=4) must not raise RecursionError — the
        default Python recursion limit is not touched (no
        sys.setrecursionlimit tampering); compute_loop_stats's own
        recursion is bounded to _MAX_LOOP_NESTING_DEPTH+1 frames by
        construction."""
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]

        stats = compute_loop_stats(actions)  # must not raise RecursionError

        self.assertTrue(stats.depth_truncated)
        self.assertFalse(stats.depth_safe)
        self.assertEqual(stats.max_nesting_depth, 5)  # 4+1: the cutoff point

    def test_truncated_empty_body_does_not_inflate_total_steps(self):
        """A truncated ForLoopAction's unexplored body contributes 0 to
        total_steps (a valid lower bound), not `n` — an empty body would
        genuinely expand to 0 steps, so treating the cutoff node as "1 step"
        would overstate a lower bound rather than merely under-count."""
        # 5 levels deep (nesting depth 5 > _MAX_LOOP_NESTING_DEPTH=4), each
        # with an empty body except the innermost has none either — the
        # cutoff itself happens at depth 5, so what's "unexplored" is
        # whatever the 5th ForLoopAction's `body` contains. Use an empty
        # body there.
        actions = [ForLoopAction(var="v4", values=[1.0, 2.0], body=[])]
        for i in range(3, -1, -1):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]

        stats = compute_loop_stats(actions)
        self.assertTrue(stats.depth_truncated)
        self.assertEqual(stats.total_steps, 0)  # not 2 (n at the cutoff node)

    def test_max_loop_iterations_includes_the_cutoff_node_itself(self):
        """len(a.values) is O(1) to read without descending into a.body, so
        it's counted even for the ForLoopAction that triggers the depth
        cutoff — it is real, known data, not a guess."""
        actions = [WaitAction(duration_s=1.0)]
        for i in range(4):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]
        # 5th (outermost) level, triggers the cutoff, has a large `values`
        actions = [ForLoopAction(var="v4", values=[float(x) for x in range(50)], body=actions)]

        stats = compute_loop_stats(actions)
        self.assertTrue(stats.depth_truncated)
        self.assertEqual(stats.max_loop_iterations, 50)

    def test_a_wide_loop_hidden_below_the_cutoff_is_not_seen_but_stays_safe(self):
        """Documented limitation (see LoopExpansionStats docstring): a wide
        ForLoopAction nested *inside* an already-truncated branch is never
        visited, so max_loop_iterations under-counts in that specific case
        (unlike the cutoff node's own len(values), which is always exact).
        This must never weaken safety — depth_truncated already forces
        depth_safe/candidates_safe/within_limits to False regardless, and
        the depth-exceeded message always fires on its own."""
        # 6 levels deep; only the innermost (6th) loop is wide (3001), but
        # it sits below the depth-5 cutoff (_MAX_LOOP_NESTING_DEPTH=4) and
        # is therefore never visited.
        actions = [
            ForLoopAction(
                var="inner", values=[float(x) for x in range(3001)],
                body=[WaitAction(duration_s=1.0)],
            ),
        ]
        for i in range(5):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]

        stats = compute_loop_stats(actions)
        self.assertTrue(stats.depth_truncated)
        self.assertEqual(stats.max_loop_iterations, 1)  # the hidden 3001 is not seen
        self.assertFalse(stats.depth_safe)
        self.assertFalse(stats.candidates_safe)  # still unsafe via depth_safe alone
        self.assertFalse(stats.within_limits)

        messages = loop_limit_messages(stats)
        self.assertTrue(any("ネスト深度" in m and "少なくとも" in m for m in messages))

    def test_width_only_violation(self):
        stats = compute_loop_stats([
            ForLoopAction(
                var="i", values=[float(x) for x in range(3000)],
                body=[WaitAction(duration_s=1.0)],
            ),
        ])
        self.assertTrue(stats.depth_safe)
        self.assertFalse(stats.candidates_safe)
        self.assertFalse(stats.within_limits)

    def test_total_steps_only_violation(self):
        stats = compute_loop_stats([
            ForLoopAction(var="i", values=[float(x) for x in range(200)], body=[
                ForLoopAction(var="j", values=[float(x) for x in range(200)], body=[
                    WaitAction(duration_s=1.0),
                ]),
            ]),
        ])
        self.assertTrue(stats.candidates_safe)
        self.assertFalse(stats.within_limits)
        self.assertEqual(stats.total_steps, 40_000)


class LoopLimitMessagesTests(unittest.TestCase):
    def test_no_messages_when_within_limits(self):
        stats = compute_loop_stats([WaitAction(duration_s=1.0)])
        self.assertEqual(loop_limit_messages(stats), [])

    def test_exact_phrasing_when_not_truncated(self):
        stats = compute_loop_stats([
            ForLoopAction(
                var="i", values=[float(x) for x in range(3000)],
                body=[WaitAction(duration_s=1.0)],
            ),
        ])
        messages = loop_limit_messages(stats)
        self.assertEqual(len(messages), 1)
        self.assertIn("最大 3000 反復", messages[0])

    def test_lower_bound_phrasing_when_truncated(self):
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]
        stats = compute_loop_stats(actions)
        messages = loop_limit_messages(stats)
        depth_msg = next(m for m in messages if "ネスト深度" in m)
        self.assertIn("少なくとも", depth_msg)
        self.assertNotIn("少なくとも5段以上", depth_msg)  # no redundant "以上"


class ExecutionTraceFlatTests(unittest.TestCase):
    def test_flat_visits_loop_body_once_regardless_of_iteration_count(self):
        actions = [
            ForLoopAction(var="i", values=[1.0, 2.0, 3.0], body=[WaitAction(duration_s=0.0)]),
        ]
        trace = ExecutionTrace.build(actions)
        self.assertEqual(len(trace.flat), 1)
        self.assertEqual(trace.flat[0].action_path, "[0].body[0]")

    def test_flat_splits_set_and_wait_pressure_action(self):
        actions = [
            SetAndWaitPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01),
        ]
        trace = ExecutionTrace.build(actions)
        self.assertEqual(len(trace.flat), 2)
        self.assertIsInstance(trace.flat[0].action, SetPressureAction)
        self.assertIsInstance(trace.flat[1].action, WaitPressureAction)
        self.assertEqual(trace.flat[0].action_path, trace.flat[1].action_path)

    def test_flat_is_always_available_even_when_deeply_nested(self):
        """flat is never gated on trace.stats — it must find a WaitAction
        100 ForLoopAction levels deep without raising RecursionError."""
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]

        trace = ExecutionTrace.build(actions)  # must not raise RecursionError
        self.assertEqual(len(trace.flat), 1)
        self.assertIsInstance(trace.flat[0].action, WaitAction)
        self.assertTrue(trace.flat[0].action_path.startswith("[0].body[0]"))
        self.assertFalse(trace.stats.within_limits)
        self.assertEqual(trace.ordered, [])

    def test_flat_is_width_independent(self):
        """A single loop with 3000 iterations (exceeding within_limits) does
        not change how many times flat visits its body — flat is the static
        leaf projection, not a per-iteration unroll."""
        actions = [
            ForLoopAction(
                var="i", values=[float(x) for x in range(3000)],
                body=[WaitAction(duration_s=1.0)],
            ),
        ]
        trace = ExecutionTrace.build(actions)
        self.assertFalse(trace.stats.within_limits)
        self.assertTrue(trace.stats.depth_safe)
        self.assertEqual(len(trace.flat), 1)
        self.assertEqual(trace.ordered, [])


class ExecutionTraceOrderedTests(unittest.TestCase):
    def test_ordered_empty_when_over_limit(self):
        actions = [
            ForLoopAction(
                var="i", values=[float(x) for x in range(3000)],
                body=[WaitAction(duration_s=1.0)],
            ),
        ]
        trace = ExecutionTrace.build(actions)
        self.assertEqual(trace.ordered, [])

    def test_step_numbers_treat_set_and_wait_pressure_action_as_one_step(self):
        """Matches SequenceRunner._flat_index, which advances its counter
        once per leaf action regardless of type — SetAndWaitPressureAction
        internally does two device calls but is one execution step."""
        actions = [
            WaitAction(duration_s=1.0),
            SetAndWaitPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01),
            WaitAction(duration_s=2.0),
        ]
        trace = ExecutionTrace.build(actions)
        steps = [e.step for e in trace.ordered]
        self.assertEqual(steps, [1, 2, 3])

    def test_ordered_carries_loop_context_and_variables(self):
        actions = [
            ForLoopAction(var="p", values=[10.0, 20.0], body=[WaitAction(duration_s=1.0)]),
        ]
        trace = ExecutionTrace.build(actions)
        self.assertEqual(len(trace.ordered), 2)
        first, second = trace.ordered
        self.assertEqual(first.variables, {"p": 10.0})
        self.assertEqual(first.loop_context, (LoopIteration("p", 10.0, 0),))
        self.assertEqual(second.variables, {"p": 20.0})
        self.assertEqual(second.loop_context, (LoopIteration("p", 20.0, 1),))
        # Same structural node, different iteration -> same action_path
        self.assertEqual(first.action_path, second.action_path)

    def test_ordered_action_path_matches_flat_style_for_top_level(self):
        actions = [WaitAction(duration_s=1.0), WaitAction(duration_s=2.0)]
        trace = ExecutionTrace.build(actions)
        self.assertEqual(trace.ordered[0].action_path, "[0]")
        self.assertEqual(trace.ordered[1].action_path, "[1]")


class PacePrimitivesTests(unittest.TestCase):
    def test_splits_set_and_wait_pressure_action(self):
        actions = [
            SetAndWaitPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01),
        ]
        trace = ExecutionTrace.build(actions)
        primitives = trace.pace_primitives()
        self.assertEqual(len(primitives), 2)
        self.assertIsInstance(primitives[0].action, SetPressureAction)
        self.assertIsInstance(primitives[1].action, WaitPressureAction)

    def test_primitives_inherit_parent_action_path_step_and_context(self):
        actions = [
            ForLoopAction(var="p", values=[1.0], body=[
                SetAndWaitPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01),
            ]),
        ]
        trace = ExecutionTrace.build(actions)
        [parent] = trace.ordered
        set_entry, wait_entry = trace.pace_primitives()
        for e in (set_entry, wait_entry):
            self.assertEqual(e.action_path, parent.action_path)
            self.assertEqual(e.step, parent.step)
            self.assertEqual(e.variables, parent.variables)
            self.assertEqual(e.loop_context, parent.loop_context)

    def test_empty_when_ordered_is_empty(self):
        actions = [
            ForLoopAction(
                var="i", values=[float(x) for x in range(3000)],
                body=[SetAndWaitPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01)],
            ),
        ]
        trace = ExecutionTrace.build(actions)
        self.assertEqual(trace.pace_primitives(), [])

    def test_non_pace_actions_pass_through_unsplit(self):
        actions = [WaitAction(duration_s=1.0)]
        trace = ExecutionTrace.build(actions)
        primitives = trace.pace_primitives()
        self.assertEqual(len(primitives), 1)
        self.assertIs(primitives[0].action, actions[0])


if __name__ == "__main__":
    unittest.main()
