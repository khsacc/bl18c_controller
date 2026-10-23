"""
Run plan — a Sequence with every ForLoopAction unrolled into a flat list of
primitive steps, in true execution order.

SequenceRunner builds this once, executes it step by step, and emits each
step's position in the list as its `flat_index`. The Visual tab displays the
same list as the run-log table, so every execution (including each loop
iteration) gets its own Started / Finished / Saved-file row.

Loop variables are NOT substituted into the actions: each RunStep carries the
original Action object plus the `variables` binding for its iteration, and the
runner resolves loop-variable fields from that binding exactly as before.

Validation (validator/execution_trace.py) rejects sequences that would unroll
beyond `_MAX_EXPANDED_STEPS` or nest beyond `_MAX_LOOP_NESTING_DEPTH`, so the
plan of any runnable sequence stays small enough to materialise in full.
"""
from __future__ import annotations

from dataclasses import dataclass

from .actions import Action, ForLoopAction


@dataclass(frozen=True)
class LoopLevel:
    """One enclosing loop's binding for a step (outermost first)."""

    var: str
    value: object
    index: int          # 0-based iteration index
    count: int          # len(values) of that loop
    loop_path: str      # structural path of the ForLoopAction, e.g. "[2]"


@dataclass(frozen=True)
class RunStep:
    index: int          # 0-based flat index (== SequenceRunner flat_index)
    action: Action
    action_path: str
    loops: tuple[LoopLevel, ...] = ()

    @property
    def variables(self) -> dict[str, object]:
        return {level.var: level.value for level in self.loops}

    def loop_label(self) -> str:
        return ", ".join(
            f"{level.var}={level.value!r} ({level.index + 1}/{level.count})"
            for level in self.loops
        )

    def describe(self) -> str:
        label = self.loop_label()
        text = self.action.describe()
        return f"{text}   [{label}]" if label else text


def build_run_plan(actions: list[Action]) -> list[RunStep]:
    steps: list[RunStep] = []
    _expand(actions, "", (), steps)
    return steps


def _expand(actions: list[Action], prefix: str, loops: tuple[LoopLevel, ...],
            out: list[RunStep]) -> None:
    for i, action in enumerate(actions):
        path = f"[{i}]" if not prefix else f"{prefix}.body[{i}]"
        if isinstance(action, ForLoopAction):
            count = len(action.values)
            for j, value in enumerate(action.values):
                level = LoopLevel(action.var, value, j, count, path)
                _expand(action.body, path, loops + (level,), out)
        else:
            out.append(RunStep(len(out), action, path, loops))
