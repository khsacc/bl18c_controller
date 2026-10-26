"""Thread-safe snapshot of the current Experimental Scheduler run.

The scheduler window updates it from the GUI thread; the Slack slash-command
thread only reads copies, so it never touches Qt objects or the runner.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime

MAX_LISTED_STEPS = 40


@dataclass(frozen=True)
class PlannedStep:
    index: int
    description: str
    loop_label: str = ""


@dataclass(frozen=True)
class RunSnapshot:
    running: bool = False
    sequence_name: str = ""
    steps: tuple[PlannedStep, ...] = ()
    started_at: str = ""            # local time, for display
    started_monotonic: float = 0.0
    ended_at: str = ""
    ended_monotonic: float = 0.0
    outcome: str = ""               # "", "completed", "condition_stop", "stopped", "error"
    outcome_detail: str = ""
    current_index: int | None = None
    current_description: str = ""
    completed_count: int = 0
    last_progress: str = ""
    last_progress_at: str = ""
    has_run: bool = False

    def elapsed_s(self, now: float | None = None) -> float:
        if not self.has_run:
            return 0.0
        end = self.ended_monotonic if not self.running else (now or time.monotonic())
        return max(0.0, end - self.started_monotonic)


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def esc(text: str) -> str:
    """Escape the three characters Slack treats as markup control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class RunStatusBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = RunSnapshot()

    def snapshot(self) -> RunSnapshot:
        with self._lock:
            return self._snap

    def begin_run(self, sequence_name: str, steps: list[PlannedStep]) -> None:
        with self._lock:
            self._snap = RunSnapshot(
                running=True, sequence_name=sequence_name, steps=tuple(steps),
                started_at=_now_text(), started_monotonic=time.monotonic(),
                has_run=True,
            )

    def step_started(self, index: int, description: str) -> None:
        with self._lock:
            self._snap = replace(self._snap, current_index=index,
                                 current_description=description)

    def step_completed(self, index: int) -> None:
        with self._lock:
            self._snap = replace(self._snap,
                                 completed_count=max(self._snap.completed_count, index + 1))

    def progress(self, message: str) -> None:
        with self._lock:
            self._snap = replace(self._snap, last_progress=message,
                                 last_progress_at=_now_text())

    def finish(self, outcome: str, detail: str = "") -> None:
        with self._lock:
            if not self._snap.running:
                return
            self._snap = replace(self._snap, running=False, outcome=outcome,
                                 outcome_detail=detail, ended_at=_now_text(),
                                 ended_monotonic=time.monotonic())


# ── text formatting ───────────────────────────────────────────────────────────

_OUTCOME_LABELS = {
    "completed": "Completed",
    "condition_stop": "Ended — stop condition reached",
    "stopped": "Stopped by user",
    "error": "Stopped by an error",
}


def _name(snap: RunSnapshot) -> str:
    return esc(snap.sequence_name) if snap.sequence_name else "(unnamed sequence)"


def current_step_line(snap: RunSnapshot) -> str:
    total = len(snap.steps)
    if snap.current_index is None:
        return "No step has started yet."
    return f"Step {snap.current_index + 1}/{total}: {esc(snap.current_description)}"


def format_status(snap: RunSnapshot) -> str:
    if not snap.has_run:
        return "No sequence has been run since the application started."
    if not snap.running:
        lines = [
            "*No sequence is running.*",
            f"Last run: *{_name(snap)}* — {_OUTCOME_LABELS.get(snap.outcome, snap.outcome)}",
            f"Started {snap.started_at}, ended {snap.ended_at} "
            f"(elapsed {format_duration(snap.elapsed_s())})",
        ]
        if snap.outcome_detail:
            lines.append(esc(snap.outcome_detail))
        return "\n".join(lines)
    return "\n".join([
        f":arrow_forward: *Running:* {_name(snap)}",
        current_step_line(snap),
        f"Started {snap.started_at} (elapsed {format_duration(snap.elapsed_s())})",
    ])


def format_progress(snap: RunSnapshot) -> str:
    if not snap.has_run:
        return format_status(snap)
    total = len(snap.steps)
    done = min(snap.completed_count, total)
    percent = (100.0 * done / total) if total else 0.0
    filled = int(round(percent / 10))
    lines = [
        format_status(snap),
        f"Progress: `{'█' * filled}{'░' * (10 - filled)}` {done}/{total} steps ({percent:.0f} %)",
    ]
    current = (snap.steps[snap.current_index]
               if snap.current_index is not None and 0 <= snap.current_index < total else None)
    if snap.running and current is not None and current.loop_label:
        lines.append(f"Loop: {esc(current.loop_label)}")
    if snap.running and snap.last_progress:
        lines.append(f"Latest message ({snap.last_progress_at}): {esc(snap.last_progress)}")
    return "\n".join(lines)


def format_steps(snap: RunSnapshot, limit: int = MAX_LISTED_STEPS) -> str:
    if not snap.has_run:
        return format_status(snap)
    total = len(snap.steps)
    header = f"*{_name(snap)}* — {total} step(s)" + ("" if snap.running else " (last run)")
    if total == 0:
        return header
    # Show a window of steps around the current one so long runs stay readable.
    centre = snap.current_index if snap.current_index is not None else 0
    start = max(0, min(centre - limit // 4, total - limit))
    end = min(total, start + limit)
    lines = [header]
    if start > 0:
        lines.append(f"… {start} earlier step(s)")
    for step in snap.steps[start:end]:
        if step.index < snap.completed_count:
            mark = ":white_check_mark:"
        elif snap.running and step.index == snap.current_index:
            mark = ":arrow_forward:"
        else:
            mark = ":white_small_square:"
        loop = f"  _({esc(step.loop_label)})_" if step.loop_label else ""
        lines.append(f"{mark} {step.index + 1}. {esc(step.description)}{loop}")
    if end < total:
        lines.append(f"… {total - end} more step(s)")
    return "\n".join(lines)


HELP_TEXT = (
    "Usage: `{cmd} [status|progress|steps|lakeshore|pace5000|screenshot|help]`\n"
    "• `status` — whether a sequence is running and the current step (default)\n"
    "• `progress` — completed steps, current loop and the latest status message\n"
    "• `steps` — the steps of the current (or last) sequence\n"
    "• `lakeshore` — temperature, ramp and heater range\n"
    "• `pace5000` — pressure, target and ramp\n"
    "• `screenshot` — send the visible Experimental Scheduler window to this DM"
)
