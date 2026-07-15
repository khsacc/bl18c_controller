"""Single communication thread + priority queue for PM16C commands.

Every socket transaction is executed by ONE dedicated comm thread; callers
submit tasks to a queue.PriorityQueue and (usually) wait on the returned
Future.  Priorities::

    0  AESTP (emergency stop)
    1  ASSTP (normal stop)
    2  stop-confirmation STQ?
    3  read queries (STSx?, STQ?, SPD?, ...)
    4  motion / speed / mode-change transactions

FIFO within a priority via a monotonic sequence counter.  Guarantee: once a
stop is enqueued, no lower-priority task that has not yet been dequeued can
start before it.  The task already being executed is never preempted (single
thread, one transaction at a time).

Stop coalescing: while a stop's Future is still pending, further stop
requests of the same or lower severity attach to that Future; an emergency
stop arriving while a normal stop is still queued supersedes it (the normal
stop's Future is chained to the emergency stop's outcome and its wire
command is skipped).

Tasks carrying a MotionLease are re-validated against the coordinator at
dequeue time, so motion that was queued before a stop request dies here with
MotionRevokedError instead of reaching the wire.
"""

import itertools
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from .errors import PM16CQueueClosedError
except ImportError:
    from errors import PM16CQueueClosedError


PRIORITY_EMERGENCY_STOP = 0
PRIORITY_NORMAL_STOP = 1
PRIORITY_STOP_CONFIRM = 2
PRIORITY_QUERY = 3
PRIORITY_MOTION = 4

_PRIORITY_SENTINEL = -1  # shutdown marker; dequeues before everything

#: Sentinel an execute callable may return to keep its Future pending.
#: Used by stop transactions: the wire send finishes on the comm thread but
#: the Future resolves only at stop CONFIRMATION (completed later by the
#: controller's stop-confirmation thread).  Exceptions still complete the
#: Future immediately.
DEFERRED = object()


@dataclass(order=True)
class CommandTask:
    priority: int
    sequence: int
    # Everything below is excluded from ordering comparisons.
    execute: Optional[Callable] = field(compare=False, default=None)
    command: str = field(compare=False, default="")
    command_class: str = field(compare=False, default="")
    lease: Any = field(compare=False, default=None)
    source: str = field(compare=False, default="")
    future: Optional[Future] = field(compare=False, default=None)
    # Set (under the arbiter's stop lock) when an emergency stop supersedes
    # this still-queued normal stop; the comm thread then skips its wire
    # command and the future is completed by the chained emergency outcome.
    superseded: bool = field(compare=False, default=False)


def _safe_complete(future: Future, *, result=None, exc: "BaseException | None" = None) -> None:
    """Set a Future's outcome, tolerating a concurrent completion."""
    try:
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)
    except Exception:
        # Already completed by the other party of a benign race
        # (e.g. a superseded stop that had just started executing).
        pass


def _chain_future(src: Future, dst: Future) -> None:
    """Complete dst with src's outcome once src finishes."""
    def _copy(f: Future):
        if dst.done():
            return
        exc = f.exception()
        if exc is not None:
            _safe_complete(dst, exc=exc)
        else:
            _safe_complete(dst, result=f.result())
    src.add_done_callback(_copy)


class CommandArbiter:
    def __init__(self, wire_executor, coordinator=None, audit=None, *,
                 name: str = "PM16C-comm"):
        """
        wire_executor: object handed to each task's execute callable — for
            the real controller this is the bound _execute_wire_task method.
        coordinator: MotionCoordinator used for dequeue-time lease
            re-validation (optional for unit tests).
        """
        self._wire_executor = wire_executor
        self._coordinator = coordinator
        self._audit = audit
        self._name = name
        self._queue: "queue.PriorityQueue[CommandTask]" = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._thread: "threading.Thread | None" = None
        self._submit_lock = threading.Lock()
        self._closed = True
        self._close_exc: "Exception | None" = None
        # Pending (not yet completed) stop tasks by kind, for coalescing.
        # RLock: chaining a superseded stop can complete a Future while this
        # lock is held, and that Future's done-callback re-enters
        # _clear_pending_stop on the same thread.
        self._stop_lock = threading.RLock()
        self._pending_stops: "dict[str, CommandTask]" = {}

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._submit_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._closed = False
            self._close_exc = None
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True
            )
            self._thread.start()

    def shutdown(self, exc: "Exception | None" = None) -> None:
        """Stop the comm thread; every still-pending Future gets `exc`."""
        exc = exc or PM16CQueueClosedError("PM16C command queue was shut down")
        with self._submit_lock:
            if self._closed and self._thread is None:
                return
            self._closed = True
            self._close_exc = exc
        # The sentinel outranks everything, so the comm thread sees it next
        # and drains the remaining queue with `exc`.
        self._queue.put(CommandTask(_PRIORITY_SENTINEL, next(self._sequence)))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None

    def is_running(self) -> bool:
        return not self._closed

    # ── submission ──────────────────────────────────────────────────────────

    def submit(self, execute, *, priority: int, command: str = "",
               command_class: str = "", lease=None, source: str = "") -> Future:
        with self._submit_lock:
            if self._closed:
                raise PM16CQueueClosedError(
                    "PM16C command queue is not running (not connected?)"
                )
            task = CommandTask(
                priority=priority,
                sequence=next(self._sequence),
                execute=execute,
                command=command,
                command_class=command_class,
                lease=lease,
                source=source,
                future=Future(),
            )
            self._queue.put(task)
            return task.future

    def submit_stop(self, execute, *, emergency: bool, command: str = "",
                    source: str = "") -> Future:
        """Enqueue a stop with coalescing (see module docstring)."""
        kind = "emergency" if emergency else "normal"
        with self._stop_lock:
            pending_emergency = self._pending_stops.get("emergency")
            if pending_emergency is not None:
                # Any further stop while an emergency stop is pending shares
                # its outcome.
                self._record_coalesce(kind, "attached_to_pending_emergency")
                return pending_emergency.future
            pending_normal = self._pending_stops.get("normal")
            if not emergency and pending_normal is not None:
                self._record_coalesce(kind, "attached_to_pending_normal")
                return pending_normal.future

            with self._submit_lock:
                if self._closed:
                    raise PM16CQueueClosedError(
                        "PM16C command queue is not running (not connected?)"
                    )
                task = CommandTask(
                    priority=(PRIORITY_EMERGENCY_STOP if emergency
                              else PRIORITY_NORMAL_STOP),
                    sequence=next(self._sequence),
                    execute=execute,
                    command=command,
                    command_class=("emergency_stop" if emergency
                                   else "normal_stop"),
                    source=source,
                    future=Future(),
                )
                self._queue.put(task)

            if (
                emergency
                and pending_normal is not None
                and not pending_normal.future.running()
                and not pending_normal.future.done()
            ):
                # Supersede the still-QUEUED normal stop: skip its wire
                # command, complete its Future with the emergency outcome.
                # A normal stop that is already executing is left alone (its
                # ASSTP is already on the wire; the emergency stop simply
                # runs next).  The narrow race where it starts running right
                # here is harmless: both completions are guarded against
                # double-set.
                pending_normal.superseded = True
                _chain_future(task.future, pending_normal.future)
                self._pending_stops.pop("normal", None)
                self._record_coalesce("normal", "superseded_by_emergency")

            self._pending_stops[kind] = task
            task.future.add_done_callback(
                lambda _f, k=kind, t=task: self._clear_pending_stop(k, t)
            )
            return task.future

    def _clear_pending_stop(self, kind: str, task: CommandTask) -> None:
        with self._stop_lock:
            if self._pending_stops.get(kind) is task:
                self._pending_stops.pop(kind, None)

    def _record_coalesce(self, kind: str, action: str) -> None:
        if self._audit is not None:
            self._audit.record("stop_coalesced", kind=kind, action=action)

    # ── comm thread ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            if task.priority == _PRIORITY_SENTINEL:
                self._drain(self._close_exc
                            or PM16CQueueClosedError("queue shut down"))
                return
            if task.superseded:
                # Future is chained to the superseding emergency stop.
                continue
            future = task.future
            if future is None or not future.set_running_or_notify_cancel():
                continue
            if task.lease is not None and self._coordinator is not None:
                try:
                    self._coordinator.validate(task.lease)
                except BaseException as exc:
                    _safe_complete(future, exc=exc)
                    continue
            try:
                result = task.execute(self._wire_executor)
            except BaseException as exc:
                _safe_complete(future, exc=exc)
            else:
                if result is not DEFERRED:
                    _safe_complete(future, result=result)

    def _drain(self, exc: Exception) -> None:
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                return
            if task.future is not None and not task.future.done():
                task.future.set_exception(exc)
