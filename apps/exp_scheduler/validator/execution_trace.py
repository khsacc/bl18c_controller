"""
Shared execution-order / static-projection traversal for ExperimentalScheduler
Sequences — REORGANISATION_PLAN.md Phase 5 (§7 Phase 5).

Before Phase 5, `validator/pre_validator.py` had three independent, hand-rolled
ForLoopAction walkers (`_collect_all_actions`, `_expand_execution_order`,
`_walk_pace_actions`) each re-implementing the same traversal shape with small
behavioural differences. This module is the single place that owns:

- `flat` — a static, non-per-iteration "leaf projection" of the action tree
  (every AST node visited exactly once, regardless of how many times its
  enclosing loop would run it at run time; SetAndWaitPressureAction split
  into its constituent set/wait pair). Equivalent to the old
  `_collect_all_actions`. Built with a non-recursive, explicit-stack walker
  (`_collect_flat`) so it stays safe (O(depth) memory, no Python recursion)
  regardless of how deeply a Sequence's ForLoopAction chain is nested —
  this is a defensive property of PreValidator itself, independent of
  whether the Sequence's origin (JSON load via
  `actions.ForLoopAction.from_dict`, or DSL `ast.parse()`) already bounds
  nesting depth; those remain out of scope for this module.
- `ordered` — the true, per-iteration execution order (ForLoopAction bodies
  unrolled once per value, step-numbered to match
  `SequenceRunner._flat_index`, SetAndWaitPressureAction left un-split).
  Equivalent to the old `_expand_execution_order`. Only materialised when
  `LoopExpansionStats.within_limits` is True, at which point real nesting
  depth is already proven <= `_MAX_LOOP_NESTING_DEPTH`, so a plain recursive
  implementation is safe.
- `pace_primitives()` — `ordered` with SetAndWaitPressureAction split into
  its set/wait pair, each primitive entry inheriting the parent's
  action_path/step/variables/loop_context. Equivalent to the old
  `_walk_pace_actions`.

See REORGANISATION_PLAN.md §7 Phase 5 for the full design rationale
(including the three-tier gating scheme — depth_safe / candidates_safe /
within_limits — implemented by PreValidator's `_run_structural`/
`_run_candidates`/`_run_expanded` wrappers, not by this module).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Mapping

from ..actions import Action, ForLoopAction, SetAndWaitPressureAction

_MAX_LOOP_ITERATIONS = 2_000
_MAX_EXPANDED_STEPS = 20_000
_MAX_LOOP_NESTING_DEPTH = 4


@dataclass(frozen=True)
class LoopIteration:
    """One level of an enclosing ForLoopAction's binding: `var` was bound to
    `value`, the `index`-th entry (0-based) of that loop's `values` list."""

    var: str
    value: object
    index: int


def format_loop_context(chain: tuple[LoopIteration, ...]) -> str | None:
    """Render a loop-iteration chain (outermost first) into the human/debug
    readable form used for `Diagnostic.loop_context` — None when `chain` is
    empty (the action is not inside any for-loop, or a static check has no
    specific candidate to report)."""
    if not chain:
        return None
    return ", ".join(f"{li.var}={li.value!r}[{li.index}]" for li in chain)


@dataclass(frozen=True)
class LoopExpansionStats:
    """As-if-fully-unrolled statistics for a Sequence's action tree, computed
    without actually materialising the unroll (see `compute_loop_stats`).

    When `depth_truncated` is True, `compute_loop_stats` stopped descending
    into at least one ForLoopAction because doing so would already exceed
    `_MAX_LOOP_NESTING_DEPTH` — `max_nesting_depth` is then a lower bound
    ("at least this deep"), and `total_steps` under-counts (any unexplored
    subtree contributes 0, never a guessed/inflated value) rather than
    over-counts. `max_loop_iterations` is a lower bound too in that case: a
    ForLoopAction's own `len(values)` is O(1) to read and does not require
    descending into its body, so it *is* counted even for the node that
    triggers the cutoff — but any ForLoopAction nested deeper still, inside
    that cutoff node's unexplored body, is never visited, so a wider loop
    hidden below the cutoff will not be reflected here. This never weakens
    `depth_safe`/`candidates_safe`/`within_limits` themselves — all three
    already read `depth_safe` first, and `depth_truncated` implies
    `depth_safe=False`, which alone already makes `candidates_safe`/
    `within_limits` False regardless of `max_loop_iterations`'s exact
    value. The only effect of the under-count is that the standalone
    "反復回数が上限を超えています" message may not fire on its own in a
    truncated sequence — the depth-exceeded message always does.
    """

    total_steps: int
    max_loop_iterations: int
    max_nesting_depth: int
    depth_truncated: bool = False

    @property
    def depth_safe(self) -> bool:
        return self.max_nesting_depth <= _MAX_LOOP_NESTING_DEPTH

    @property
    def candidates_safe(self) -> bool:
        return self.depth_safe and self.max_loop_iterations <= _MAX_LOOP_ITERATIONS

    @property
    def within_limits(self) -> bool:
        return self.candidates_safe and self.total_steps <= _MAX_EXPANDED_STEPS


def compute_loop_stats(actions: list[Action], _depth: int = 0) -> LoopExpansionStats:
    """Compute (total_expanded_steps, max_single_loop_iterations,
    max_nesting_depth) as if every ForLoopAction in `actions` were fully
    unrolled — without actually materializing the expansion, so measuring a
    runaway loop stays cheap even though actually unrolling it would not.

    Depth-guarded: as soon as `_depth + 1` would exceed
    `_MAX_LOOP_NESTING_DEPTH`, this stops recursing into that ForLoopAction's
    body (`depth_truncated=True`) instead of descending further. Python's own
    call recursion therefore never exceeds `_MAX_LOOP_NESTING_DEPTH + 1`
    frames, regardless of how deeply nested the input actually is — a
    RecursionError here would otherwise be reachable from a directly
    constructed (or, in principle, corrupted-JSON-loaded) Sequence with a
    long chain of nested ForLoopAction, independent of whether upstream
    construction paths (DSL `ast.parse()`, `actions.ForLoopAction.from_dict`)
    already bound nesting depth themselves — this module does not rely on
    that.
    """
    total = 0
    max_iterations = 0
    max_depth = _depth
    truncated = False
    for a in actions:
        if isinstance(a, ForLoopAction):
            n = len(a.values)
            max_iterations = max(max_iterations, n)
            if _depth + 1 > _MAX_LOOP_NESTING_DEPTH:
                max_depth = max(max_depth, _depth + 1)
                truncated = True
                continue  # body left unexplored; contributes 0 to total_steps
            child = compute_loop_stats(a.body, _depth + 1)
            total += n * child.total_steps
            max_iterations = max(max_iterations, child.max_loop_iterations)
            max_depth = max(max_depth, child.max_nesting_depth)
            truncated = truncated or child.depth_truncated
        else:
            total += 1
    return LoopExpansionStats(total, max_iterations, max_depth, truncated)


def loop_limit_messages(stats: LoopExpansionStats) -> list[str]:
    """Renders the same three "exceeded" messages the old
    `_check_loop_expansion_limits` produced. When `stats.depth_truncated` is
    True, the depth/total-step/iteration-count figures are all phrased as
    lower bounds ("少なくとも...") rather than exact counts, since
    `compute_loop_stats` stopped counting once nesting exceeded the limit —
    including `max_loop_iterations`, which (see `LoopExpansionStats`
    docstring) may miss an even-wider loop nested inside the unexplored
    part of a truncated branch."""
    messages: list[str] = []
    if stats.max_loop_iterations > _MAX_LOOP_ITERATIONS:
        iterations_phrase = (
            f"少なくとも {stats.max_loop_iterations} 反復"
            if stats.depth_truncated
            else f"最大 {stats.max_loop_iterations} 反復"
        )
        messages.append(
            f"for ループの反復回数が上限（{_MAX_LOOP_ITERATIONS}）を超えています "
            f"（{iterations_phrase}）。ループの展開に依存する検証を"
            "スキップしました。"
        )
    if stats.total_steps > _MAX_EXPANDED_STEPS:
        total_phrase = (
            f"少なくとも {stats.total_steps} ステップ"
            if stats.depth_truncated
            else f"展開後 {stats.total_steps} ステップ"
        )
        messages.append(
            f"シーケンス全体を展開した際の総ステップ数が上限（{_MAX_EXPANDED_STEPS}）を"
            f"超えています（{total_phrase}）。ループの展開に依存する"
            "検証をスキップしました。"
        )
    if stats.max_nesting_depth > _MAX_LOOP_NESTING_DEPTH:
        depth_phrase = (
            f"少なくとも {stats.max_nesting_depth} 段"
            if stats.depth_truncated
            else f"最大 {stats.max_nesting_depth} 段"
        )
        messages.append(
            f"for ループのネスト深度が上限（{_MAX_LOOP_NESTING_DEPTH}）を超えています "
            f"（{depth_phrase}）。ループの展開に依存する検証を"
            "スキップしました。"
        )
    return messages


@dataclass(frozen=True)
class StaticTraceEntry:
    """One leaf action from the static (non-per-iteration) `flat` projection,
    tagged with its structural address in the original action tree."""

    action: Action
    action_path: str


@dataclass(frozen=True)
class TraceEntry:
    action: Action
    action_path: str
    step: int
    variables: Mapping[str, object]
    loop_context: tuple[LoopIteration, ...]

    @property
    def label(self) -> str:
        return f"Step{self.step}: {self.action.describe()}"


class _PathNode:
    """Cons-cell for a structural index chain: O(1) memory per tree level,
    shared by reference between stack frames rather than copied — this is
    what keeps `_collect_flat`'s total memory O(depth) instead of O(depth^2)
    (a naive implementation that stores a fully-materialised prefix string
    per stack frame would duplicate an O(depth)-length string at every one
    of the O(depth) frames)."""

    __slots__ = ("index", "parent")

    def __init__(self, index: int, parent: "_PathNode | None"):
        self.index = index
        self.parent = parent


def child_path(prefix: str, index: int) -> str:
    """Structural path format shared by every walker in this module (and by
    the raw-tree static checkers in `validator/checks/`): a bare `[i]` for a
    top-level item, `.body[i]` appended for each level of ForLoopAction
    nesting — e.g. `"[2].body[1]"`. `_path_str` below is the equivalent used
    by the non-recursive `_collect_flat`, which cannot cheaply carry a
    `prefix` string per stack frame (see `_PathNode`)."""
    return f"[{index}]" if not prefix else f"{prefix}.body[{index}]"


_EMPTY_LOOP_VALUES: Mapping[str, list] = {}


def walk_raw(
    actions: list[Action],
    path_prefix: str = "",
    _loop_values: Mapping[str, list] = _EMPTY_LOOP_VALUES,
) -> "Iterator[tuple[Action, str, list[Action], int, Mapping[str, list]]]":
    """Depth-first, pre-order walk of the raw (non-per-iteration) action
    tree, descending into every `ForLoopAction.body` — the single shared
    recursion `validator/checks/*.py`'s static (device-free) tree-shape
    checkers should build on, instead of each hand-rolling an equivalent
    local `_scan`/`_walk` closure (external review finding, see
    REORGANISATION_PLAN.md §31: `sequence_structure.py`, `action_params.py`,
    and `pace5000.py` each had their own copy of this exact recursion).

    Yields ``(action, path, siblings, index, loop_values)`` for EVERY node,
    including `ForLoopAction` nodes themselves (visited before their body,
    with the scope from *outside* that loop — matching how a check like
    `check_unused_loop_vars` needs to see the ForLoopAction node itself
    before deciding whether to descend):

    - ``siblings`` / ``index``: the list `action` came from and its
      position in it, so a caller that needs the previous or next sibling
      *within the same block* (e.g. adjacency checks) can index it directly
      — this deliberately does NOT flatten across block boundaries the way
      `flat` does, since "next action" must not silently cross a
      ForLoopAction's own end into whatever follows the loop at the parent
      level.
    - ``loop_values``: ``{var: values}`` for every ForLoopAction strictly
      enclosing this node (not including a node that is itself a
      ForLoopAction), letting a caller resolve a `str` field that names a
      loop variable to its candidate values, or just check
      ``name in loop_values`` for scope membership.

    Plain recursion (like `_collect_ordered`, not the explicit-stack
    `_collect_flat`) — callers must only use this where nesting depth is
    already bounded (`ExecutionTrace.stats.depth_safe` or stronger), which
    is how every current caller is gated.
    """
    for i, a in enumerate(actions):
        path = child_path(path_prefix, i)
        yield a, path, actions, i, _loop_values
        if isinstance(a, ForLoopAction):
            yield from walk_raw(a.body, path, {**_loop_values, a.var: a.values})


def _path_str(node: "_PathNode") -> str:
    parts: list[int] = []
    n: "_PathNode | None" = node
    while n is not None:
        parts.append(n.index)
        n = n.parent
    indices = list(reversed(parts))
    return f"[{indices[0]}]" + "".join(f".body[{i}]" for i in indices[1:])


def _collect_flat(actions: list[Action]) -> list[StaticTraceEntry]:
    """Non-recursive (explicit-stack) equivalent of the old
    `_collect_all_actions`: visits every ForLoopAction body exactly once
    (never per `values` entry), splitting SetAndWaitPressureAction into its
    set/wait pair. Because the stack lives on the heap (a plain Python
    list), not the C call stack, this completes for arbitrarily deep
    ForLoopAction nesting with memory and time proportional to depth and
    node count — it never raises RecursionError regardless of how the
    Sequence was constructed.
    """
    result: list[StaticTraceEntry] = []
    stack: list[list] = [[actions, 0, None]]
    while stack:
        frame = stack[-1]
        nodes, idx, prefix_node = frame
        if idx >= len(nodes):
            stack.pop()
            continue
        frame[1] += 1
        a = nodes[idx]
        this_node = _PathNode(idx, prefix_node)
        if isinstance(a, ForLoopAction):
            stack.append([a.body, 0, this_node])
        elif isinstance(a, SetAndWaitPressureAction):
            path = _path_str(this_node)
            result.append(StaticTraceEntry(a.to_set_action(), path))
            result.append(StaticTraceEntry(a.to_wait_action(), path))
        else:
            result.append(StaticTraceEntry(a, _path_str(this_node)))
    return result


class _StepCounter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def _collect_ordered(
    actions: list[Action],
    prefix: str = "",
    loop_context: tuple[LoopIteration, ...] = (),
    counter: "_StepCounter | None" = None,
) -> list[TraceEntry]:
    """Recursive equivalent of the old `_expand_execution_order`: unrolls
    every ForLoopAction body once per value (true execution order,
    SetAndWaitPressureAction left un-split so step numbers match
    `SequenceRunner._flat_index`, which counts it as a single step). Only
    called from `ExecutionTrace.build()` when `stats.within_limits` is
    already True, which guarantees real nesting depth <= 4 — plain
    recursion (and plain string-concatenation paths) are safe at that
    bounded depth.
    """
    if counter is None:
        counter = _StepCounter()
    out: list[TraceEntry] = []
    for i, a in enumerate(actions):
        path = child_path(prefix, i)
        if isinstance(a, ForLoopAction):
            for idx, val in enumerate(a.values):
                child_context = loop_context + (LoopIteration(a.var, val, idx),)
                out.extend(
                    _collect_ordered(a.body, path, child_context, counter)
                )
        else:
            variables = {li.var: li.value for li in loop_context}
            out.append(
                TraceEntry(
                    action=a,
                    action_path=path,
                    step=counter.next(),
                    variables=variables,
                    loop_context=loop_context,
                )
            )
    return out


@dataclass
class ExecutionTrace:
    stats: LoopExpansionStats
    flat: list[StaticTraceEntry] = field(default_factory=list)
    ordered: list[TraceEntry] = field(default_factory=list)

    @classmethod
    def build(cls, actions: list[Action]) -> "ExecutionTrace":
        stats = compute_loop_stats(actions)
        flat = _collect_flat(actions)
        ordered = _collect_ordered(actions) if stats.within_limits else []
        return cls(stats=stats, flat=flat, ordered=ordered)

    def pace_primitives(self) -> list[TraceEntry]:
        """`ordered`, with SetAndWaitPressureAction split into its set/wait
        pair — each primitive entry inherits the parent's action_path/step/
        variables/loop_context, since both primitives are internal
        representations of the same AST node and the same execution step,
        not separate nodes. Equivalent to the old `_walk_pace_actions`.
        Empty whenever `ordered` is empty (i.e. automatically gated the same
        way `ordered` is, no separate limit check needed)."""
        result: list[TraceEntry] = []
        for entry in self.ordered:
            if isinstance(entry.action, SetAndWaitPressureAction):
                result.append(
                    TraceEntry(
                        action=entry.action.to_set_action(),
                        action_path=entry.action_path,
                        step=entry.step,
                        variables=entry.variables,
                        loop_context=entry.loop_context,
                    )
                )
                result.append(
                    TraceEntry(
                        action=entry.action.to_wait_action(),
                        action_path=entry.action_path,
                        step=entry.step,
                        variables=entry.variables,
                        loop_context=entry.loop_context,
                    )
                )
            else:
                result.append(entry)
        return result
