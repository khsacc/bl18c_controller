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

import dataclasses
from dataclasses import dataclass

from .actions import LOOP_VAR_FIELDS, Action, ForLoopAction


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

    def counter_label(self) -> str:
        """Iteration counters only, e.g. "10/100" (outermost loop first)."""
        return ", ".join(f"{level.index + 1}/{level.count}" for level in self.loops)

    def display_action(self) -> Action:
        """A copy of the action with this iteration's loop values filled in,
        for display only. Loop-variable fields (LOOP_VAR_FIELDS) take the
        bound value and "{var}" placeholders in text fields are formatted.
        Returns the original action when nothing references a loop variable."""
        variables = self.variables
        if not variables or not dataclasses.is_dataclass(self.action):
            return self.action
        changes: dict[str, object] = {}
        loop_field = LOOP_VAR_FIELDS.get(type(self.action))
        for f in dataclasses.fields(self.action):
            value = getattr(self.action, f.name)
            if not isinstance(value, str):
                continue
            if f.name == loop_field and value in variables:
                changes[f.name] = variables[value]
            elif "{" in value:
                try:
                    formatted = value.format(**variables)
                except (KeyError, IndexError, ValueError):
                    continue
                if formatted != value:
                    changes[f.name] = formatted
        if not changes:
            return self.action
        try:
            return dataclasses.replace(self.action, **changes)
        except (TypeError, ValueError):
            return self.action

    def describe(self) -> str:
        """Display text: loop values substituted, iteration counters appended,
        e.g. "Set pressure 1 MPa at 1.2 MPa/min and wait (±0.01 MPa) [10/100]"."""
        text = self.display_action().describe()
        counter = self.counter_label()
        return f"{text} [{counter}]" if counter else text


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
