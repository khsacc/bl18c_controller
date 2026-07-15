"""Exception hierarchy for the PM16C stage-control stack.

Lives in its own module so motion_coordinator.py and command_arbiter.py can
share these types without importing control_stage (which would be a circular
import).  control_stage re-exports the PM16C* names, so existing importers
(`from utils.stage.control_stage import PM16CCommError`, ...) keep working.
"""


# ---------------------------------------------------------------------------
# Communication errors
#
# send_cmd() raises these instead of silently returning None, so a comms
# failure can never be mistaken for "no motors moving" / "position unknown
# but fine to proceed" by a caller. See utils/stage/IMPLEMENTATION_DETAILS.md.
# ---------------------------------------------------------------------------
class PM16CCommError(Exception):
    """Base class for PM16C communication failures (timeout, malformed reply,
    or the connection being closed by the controller)."""


class PM16CTimeoutError(PM16CCommError):
    """No (valid) reply was received within the socket timeout."""


class PM16CProtocolError(PM16CCommError):
    """A reply was received but didn't match the shape expected for the
    command that was sent (wrong channel, wrong token, malformed status)."""


class PM16CQueueClosedError(PM16CCommError):
    """The command queue was shut down (disconnect) before this command ran.
    Every Future still pending at shutdown is completed with this error."""


# ---------------------------------------------------------------------------
# Motion-ownership (lease) errors
#
# Raised by MotionCoordinator and by controller motion methods.  See
# utils/stage/motion_coordinator.py for the lease state machine.
# ---------------------------------------------------------------------------
class MotionLeaseError(Exception):
    """Base class for motion-ownership violations."""


class MotionLeaseRequiredError(MotionLeaseError):
    """A motion/speed/mode command was issued without a MotionLease.
    There is no lease-optional fallback: pass motion=<lease> obtained from
    acquire_motion()/motion_session()."""


class MotionNotAvailableError(MotionLeaseError):
    """acquire_motion() was refused because another owner holds motion (or a
    stop/recovery is in progress).  Carries holder info for UI messages."""

    def __init__(self, message, *, holder=None):
        super().__init__(message)
        #: dict with owner/operation/lease_id/acquired_at, or None if the
        #: refusal was due to a stop/recovery state rather than a holder.
        self.holder = holder


class MotionRevokedError(MotionLeaseError):
    """The lease used for this operation has been revoked (a stop was
    requested, possibly by another app).  The owning worker should abort its
    sequence and release the lease in its finally block."""


class MotionRecoveryRequiredError(MotionLeaseError):
    """Motion ownership is stuck in RECOVERY_REQUIRED (a stop could not be
    sent or confirmed).  New motion is refused until an explicit
    recover_motion() succeeds."""
