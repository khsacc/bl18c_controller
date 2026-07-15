"""Controller-wide motion ownership (lease) state machine.

One MotionCoordinator instance guards one PM16C controller (real or
simulated).  Ownership is for the WHOLE controller, not per channel: REM/LOC,
ASSTP/AESTP, the Ch8/Ch9 collision constraint and the 4-concurrent-motor cap
are all controller-global, so per-channel leases would be unsound.

State machine::

    FREE ──acquire──▶ HELD ──revoke_for_stop──▶ REVOKED_STOPPING
      ▲                │                              │
      │                └─release──▶ FREE              ├─note_stop_confirmed─▶
      │                                               │   REVOKED_STOPPED_GRACE
      │◀── grace elapsed / owner released ────────────┘        │
      │                                                        │
      └────────── force_recover_complete(True) ◀── RECOVERY_REQUIRED
                                                       ▲
                        note_stop_send_failed / note_stop_confirm_failed

Key invariants (agreed spec):

* Leases carry a monotonically increasing ``generation``; a stale lease can
  never act again after reclaim (ABA-safe) — no tombstone list is needed.
* ``release()`` is a conditional no-op: it acts only when the passed lease
  matches the current holder on (controller_id, generation, lease_id);
  otherwise it logs ``stale_motion_release_ignored`` and returns False.
  It never raises and is idempotent — safe to call from any finally block.
* HELD has NO time-to-live: long scans/exposures are normal.  Auto-reclaim
  happens only from REVOKED_STOPPED_GRACE, i.e. only after the physical stop
  was confirmed, and only after the grace period gives the revoked worker a
  chance to release cleanly.
* A stop that cannot be sent or confirmed leaves RECOVERY_REQUIRED: new
  motion is refused until an explicit recover_motion() succeeds.

All methods are memory-only — the coordinator never performs I/O; the
controller drives the stop hooks from its communication layer.
"""

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

try:
    from .errors import (
        MotionLeaseError,
        MotionLeaseRequiredError,
        MotionNotAvailableError,
        MotionRevokedError,
        MotionRecoveryRequiredError,
    )
except ImportError:
    from errors import (
        MotionLeaseError,
        MotionLeaseRequiredError,
        MotionNotAvailableError,
        MotionRevokedError,
        MotionRecoveryRequiredError,
    )


#: Seconds a revoked-and-stopped lease is held for the original owner to
#: release cleanly before the coordinator reclaims it for new owners.
DEFAULT_GRACE_PERIOD_S = 5.0


class LeaseState(Enum):
    FREE = "free"
    HELD = "held"
    REVOKED_STOPPING = "revoked_stopping"
    REVOKED_STOPPED_GRACE = "revoked_stopped_grace"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class MotionLease:
    """Value object handed to the owner at acquire time.

    Frozen: it can be shared freely with the owning app's worker threads.
    Validity is decided by the coordinator (triple match + state), never by
    the lease object itself.
    """
    controller_id: str
    lease_id: str
    generation: int
    owner: str
    operation: str


class _Holder:
    """Mutable internal record of the current (possibly revoked) holder."""

    __slots__ = (
        "lease_id", "generation", "owner", "operation",
        "acquired_at", "acquired_monotonic", "owner_released",
    )

    def __init__(self, lease: MotionLease, acquired_monotonic: float):
        self.lease_id = lease.lease_id
        self.generation = lease.generation
        self.owner = lease.owner
        self.operation = lease.operation
        self.acquired_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        self.acquired_monotonic = acquired_monotonic
        self.owner_released = False

    def matches(self, lease: MotionLease) -> bool:
        return (
            lease.generation == self.generation
            and lease.lease_id == self.lease_id
        )

    def info(self) -> dict:
        return {
            "owner": self.owner,
            "operation": self.operation,
            "lease_id": self.lease_id,
            "generation": self.generation,
            "acquired_at": self.acquired_at,
        }


class MotionCoordinator:
    def __init__(self, controller_id: str, audit=None, *,
                 grace_period_s: float = DEFAULT_GRACE_PERIOD_S,
                 clock=time.monotonic):
        self.controller_id = controller_id
        self.grace_period_s = grace_period_s
        self._audit = audit
        self._clock = clock
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._state = LeaseState.FREE
        self._holder: "_Holder | None" = None
        self._generation = 0
        self._stop_confirmed_monotonic: "float | None" = None
        self._recovery_in_progress = False

    # ── audit helper ────────────────────────────────────────────────────────

    def _record(self, event: str, **fields) -> None:
        if self._audit is not None:
            self._audit.record(event, controller_id=self.controller_id, **fields)

    # ── acquire / release ───────────────────────────────────────────────────

    def acquire(self, owner: str, operation: str, *, timeout: "float | None" = None) -> MotionLease:
        """Grant motion ownership, or raise immediately (default).

        ``timeout=None`` means no waiting: if motion is not available the
        call raises at once — a UI button must fail fast, not fire minutes
        later.  A positive timeout waits up to that long (worker-internal
        retries only).
        """
        deadline = None if timeout is None else self._clock() + timeout
        with self._cond:
            while True:
                self._maybe_reclaim_locked()
                if self._state is LeaseState.FREE:
                    self._generation += 1
                    lease = MotionLease(
                        controller_id=self.controller_id,
                        lease_id=f"lease-{uuid.uuid4().hex[:12]}",
                        generation=self._generation,
                        owner=owner,
                        operation=operation,
                    )
                    self._holder = _Holder(lease, self._clock())
                    self._state = LeaseState.HELD
                    self._record(
                        "motion_acquired",
                        lease_id=lease.lease_id,
                        generation=lease.generation,
                        owner=owner,
                        operation=operation,
                    )
                    return lease

                remaining = None if deadline is None else deadline - self._clock()
                if remaining is None or remaining <= 0:
                    holder_info = self._holder.info() if self._holder else None
                    self._record(
                        "motion_rejected",
                        requested_owner=owner,
                        requested_operation=operation,
                        state=self._state.value,
                        holder=holder_info,
                    )
                    if self._state is LeaseState.RECOVERY_REQUIRED:
                        raise MotionRecoveryRequiredError(
                            "Motion ownership requires recovery: a stop could not "
                            "be sent or confirmed. Run motion recovery before "
                            "starting new motion."
                        )
                    if holder_info is not None:
                        raise MotionNotAvailableError(
                            f"Motion is in use by \"{holder_info['owner']}\" "
                            f"({holder_info['operation']}, since "
                            f"{holder_info['acquired_at']}). New motion cannot "
                            "start until the current operation finishes.",
                            holder=holder_info,
                        )
                    raise MotionNotAvailableError(
                        f"Motion is not available (state: {self._state.value}).",
                        holder=None,
                    )
                self._cond.wait(remaining)

    def release(self, lease: MotionLease) -> bool:
        """Conditional-no-op release.  Never raises; safe in finally blocks.

        Acts only when ``lease`` matches the current holder.  During
        REVOKED_STOPPING the holder is kept (stop confirmation still needs
        it) and only ``owner_released`` is flagged; the confirmed-stop hook
        then frees immediately instead of waiting out the grace period.
        """
        if lease is None or lease.controller_id != self.controller_id:
            self._record(
                "stale_motion_release_ignored",
                lease_id=getattr(lease, "lease_id", None),
                reason="wrong_controller" if lease is not None else "no_lease",
            )
            return False
        with self._cond:
            holder = self._holder
            if holder is None or not holder.matches(lease):
                self._record(
                    "stale_motion_release_ignored",
                    lease_id=lease.lease_id,
                    lease_generation=lease.generation,
                    current_lease_id=holder.lease_id if holder else None,
                    current_generation=holder.generation if holder else None,
                    reason="holder_mismatch" if holder else "no_holder",
                )
                return False

            if self._state is LeaseState.HELD:
                self._holder = None
                self._state = LeaseState.FREE
                self._record(
                    "motion_released",
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                    owner=lease.owner,
                )
                self._cond.notify_all()
                return True

            if self._state is LeaseState.REVOKED_STOPPED_GRACE:
                # Stop already confirmed; the owner's release completes the
                # handover immediately.
                self._holder = None
                self._state = LeaseState.FREE
                self._stop_confirmed_monotonic = None
                self._record(
                    "motion_released",
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                    owner=lease.owner,
                    after_revocation=True,
                )
                self._cond.notify_all()
                return True

            # REVOKED_STOPPING or RECOVERY_REQUIRED: remember the release but
            # keep the holder until the stop outcome is known.
            holder.owner_released = True
            self._record(
                "motion_released",
                lease_id=lease.lease_id,
                generation=lease.generation,
                owner=lease.owner,
                deferred=True,
                state=self._state.value,
            )
            return True

    # ── validation ──────────────────────────────────────────────────────────

    def is_valid(self, lease: MotionLease) -> bool:
        if lease is None or lease.controller_id != self.controller_id:
            return False
        with self._lock:
            return (
                self._state is LeaseState.HELD
                and self._holder is not None
                and self._holder.matches(lease)
            )

    def validate(self, lease: MotionLease) -> None:
        """Raise unless ``lease`` currently authorizes motion.

        Memory-only — cheap enough for the unchecked fast path and for
        between-wire-command checks inside a transaction.
        """
        if lease is None:
            raise MotionLeaseRequiredError(
                "This operation requires a MotionLease. Acquire one with "
                "acquire_motion()/motion_session() and pass it as motion=."
            )
        if not isinstance(lease, MotionLease):
            raise MotionLeaseError(
                f"motion= must be a MotionLease, got {type(lease).__name__}"
            )
        if lease.controller_id != self.controller_id:
            raise MotionLeaseError(
                f"Lease {lease.lease_id} belongs to controller "
                f"{lease.controller_id!r}, not {self.controller_id!r}."
            )
        with self._lock:
            if (
                self._state is LeaseState.HELD
                and self._holder is not None
                and self._holder.matches(lease)
            ):
                return
            raise MotionRevokedError(
                f"Lease {lease.lease_id} (gen {lease.generation}, owner "
                f"{lease.owner!r}) is no longer valid "
                f"(state: {self._state.value})."
            )

    # ── stop lifecycle (driven by the controller's stop path) ───────────────

    def revoke_for_stop(self, *, source: str, emergency: bool) -> dict:
        """Instantly invalidate the current lease (memory-only).

        Returns a ticket dict the stop path threads through the stop
        transaction and confirmation hooks.  Never blocks, never raises —
        stops must always be accepted.
        """
        with self._cond:
            revoked_lease_id = None
            if self._state is LeaseState.HELD and self._holder is not None:
                revoked_lease_id = self._holder.lease_id
                self._state = LeaseState.REVOKED_STOPPING
                self._record(
                    "motion_revoked",
                    lease_id=self._holder.lease_id,
                    generation=self._holder.generation,
                    owner=self._holder.owner,
                    operation=self._holder.operation,
                    stop_source=source,
                    emergency=emergency,
                )
            ticket = {
                "stop_source": source,
                "emergency": emergency,
                "revoked_lease_id": revoked_lease_id,
                "state_at_request": self._state.value,
            }
            self._record(
                "motion_stop_requested",
                stop_source=source,
                emergency=emergency,
                revoked_lease_id=revoked_lease_id,
                state=self._state.value,
            )
            return ticket

    def note_stop_sent(self, ticket: dict) -> None:
        self._record(
            "motion_stop_sent",
            stop_source=ticket.get("stop_source"),
            emergency=ticket.get("emergency"),
            revoked_lease_id=ticket.get("revoked_lease_id"),
        )

    def note_stop_send_failed(self, ticket: dict) -> None:
        """Stop command could not be sent: machine state unknown."""
        with self._cond:
            self._state = LeaseState.RECOVERY_REQUIRED
            self._record(
                "motion_recovery_required",
                level="ERROR",
                reason="stop_send_failed",
                stop_source=ticket.get("stop_source"),
                revoked_lease_id=ticket.get("revoked_lease_id"),
            )

    def note_stop_confirmed(self, ticket: dict) -> None:
        """All motors were confirmed stopped after the stop command."""
        with self._cond:
            if self._state is LeaseState.REVOKED_STOPPING and self._holder is not None:
                if self._holder.owner_released:
                    self._holder = None
                    self._state = LeaseState.FREE
                    self._stop_confirmed_monotonic = None
                    self._cond.notify_all()
                else:
                    self._state = LeaseState.REVOKED_STOPPED_GRACE
                    self._stop_confirmed_monotonic = self._clock()
            self._record(
                "motion_stop_confirmed",
                stop_source=ticket.get("stop_source"),
                emergency=ticket.get("emergency"),
                revoked_lease_id=ticket.get("revoked_lease_id"),
                state=self._state.value,
            )

    def note_stop_confirm_failed(self, ticket: dict) -> None:
        """Stop was sent but could not be confirmed: machine state unknown."""
        with self._cond:
            self._state = LeaseState.RECOVERY_REQUIRED
            self._record(
                "motion_recovery_required",
                level="ERROR",
                reason="stop_confirm_failed",
                stop_source=ticket.get("stop_source"),
                revoked_lease_id=ticket.get("revoked_lease_id"),
            )

    # ── explicit recovery ────────────────────────────────────────────────────

    def force_recover_begin(self, *, source: str) -> dict:
        """Start an operator-initiated recovery (always allowed).

        The controller follows with a stop transaction + confirmation and
        then calls force_recover_complete().
        """
        with self._cond:
            self._recovery_in_progress = True
            holder_info = self._holder.info() if self._holder else None
            if self._state is LeaseState.HELD:
                self._state = LeaseState.REVOKED_STOPPING
            self._record(
                "motion_recovery_started",
                source=source,
                previous_holder=holder_info,
                state=self._state.value,
            )
            return {
                "stop_source": source,
                "emergency": True,
                "revoked_lease_id": holder_info["lease_id"] if holder_info else None,
                "recovery": True,
            }

    def force_recover_complete(self, success: bool, *, source: str) -> None:
        with self._cond:
            self._recovery_in_progress = False
            if success:
                # Bump the generation so any lease that survived in a stuck
                # worker can never act again.
                self._generation += 1
                self._holder = None
                self._state = LeaseState.FREE
                self._stop_confirmed_monotonic = None
                self._record("motion_recovery_completed", source=source,
                             generation=self._generation)
                self._cond.notify_all()
            else:
                self._state = LeaseState.RECOVERY_REQUIRED
                self._record("motion_recovery_failed", level="ERROR", source=source)

    # ── introspection ───────────────────────────────────────────────────────

    def state(self) -> LeaseState:
        with self._lock:
            self._maybe_reclaim_locked()
            return self._state

    def holder_info(self) -> "dict | None":
        with self._lock:
            self._maybe_reclaim_locked()
            return self._holder.info() if self._holder else None

    def is_available(self) -> bool:
        with self._lock:
            self._maybe_reclaim_locked()
            return self._state is LeaseState.FREE

    # ── internal ────────────────────────────────────────────────────────────

    def _maybe_reclaim_locked(self) -> None:
        """Lazily reclaim a revoked lease whose grace period has elapsed.

        Only reachable from REVOKED_STOPPED_GRACE — i.e. only after the
        physical stop was confirmed.  Must be called with the lock held.
        """
        if self._state is not LeaseState.REVOKED_STOPPED_GRACE:
            return
        if self._stop_confirmed_monotonic is None:
            return
        if self._clock() - self._stop_confirmed_monotonic < self.grace_period_s:
            return
        reclaimed = self._holder.info() if self._holder else None
        self._holder = None
        self._state = LeaseState.FREE
        self._stop_confirmed_monotonic = None
        self._record("motion_lease_reclaimed", previous_holder=reclaimed,
                     grace_period_s=self.grace_period_s)
        self._cond.notify_all()
