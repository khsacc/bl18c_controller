import inspect
import logging
import os
import queue
import re
import socket
import threading
import time
from contextlib import contextmanager
from operator import ge, le, gt, lt, eq
from typing import Callable, Iterable, Optional

try:
    from .stage_monitor import PM16CAuditLogger, StageStateMonitor
    # Exceptions live in errors.py (shared with motion_coordinator /
    # command_arbiter); re-exported here so existing importers keep working.
    from .errors import (
        PM16CCommError,
        PM16CTimeoutError,
        PM16CProtocolError,
        PM16CQueueClosedError,
        MotionLeaseError,
        MotionLeaseRequiredError,
        MotionNotAvailableError,
        MotionRevokedError,
        MotionRecoveryRequiredError,
    )
    from .motion_coordinator import MotionCoordinator, MotionLease, LeaseState
    from .command_arbiter import (
        CommandArbiter,
        DEFERRED,
        PRIORITY_EMERGENCY_STOP,
        PRIORITY_NORMAL_STOP,
        PRIORITY_STOP_CONFIRM,
        PRIORITY_QUERY,
        PRIORITY_MOTION,
    )
except ImportError:
    from stage_monitor import PM16CAuditLogger, StageStateMonitor
    from errors import (
        PM16CCommError,
        PM16CTimeoutError,
        PM16CProtocolError,
        PM16CQueueClosedError,
        MotionLeaseError,
        MotionLeaseRequiredError,
        MotionNotAvailableError,
        MotionRevokedError,
        MotionRecoveryRequiredError,
    )
    from motion_coordinator import MotionCoordinator, MotionLease, LeaseState
    from command_arbiter import (
        CommandArbiter,
        DEFERRED,
        PRIORITY_EMERGENCY_STOP,
        PRIORITY_NORMAL_STOP,
        PRIORITY_STOP_CONFIRM,
        PRIORITY_QUERY,
        PRIORITY_MOTION,
    )

# Seconds the stop-confirmation thread keeps polling STQ? before declaring
# the stop unconfirmed (mirrors stage_monitor.STOP_CONFIRM_TIMEOUT_S).
STOP_CONFIRM_TIMEOUT_S = 30.0
# Consecutive "all 4 motor slots free" readings required to call it stopped.
STOP_CONFIRM_COUNT = 4


logger = logging.getLogger("pm16c")
logger.addHandler(logging.NullHandler())


def _infer_source() -> str:
    """Best-effort name of the module that called into this class's public API.

    Used to tag TX/RX/MOVE log lines with the originating app without
    threading a `source=` parameter through every one of this class's ~15
    call sites across the repo. Walks the stack past frames belonging to
    this file and returns the immediate external caller's module name.
    """
    frame = inspect.currentframe()
    try:
        frame = frame.f_back
        while frame is not None and frame.f_code.co_filename == __file__:
            frame = frame.f_back
        if frame is None:
            return "unknown"
        return frame.f_globals.get("__name__") or os.path.splitext(
            os.path.basename(frame.f_code.co_filename)
        )[0]
    finally:
        del frame


def _command_metadata(cmd: str) -> dict:
    """Return stable audit fields for a raw PM16C command."""
    upper = cmd.upper()
    command_class = "unknown"
    if upper.startswith("STS") or upper.startswith("STQ") or "?" in upper:
        command_class = "query"
    elif upper in ("REM", "LOC"):
        command_class = "mode_change"
    elif upper.startswith("ABS"):
        command_class = "motion_absolute"
    elif upper.startswith("REL"):
        command_class = "motion_relative"
    elif upper.startswith(("JOG", "SCAN", "CSCAN")):
        command_class = "motion_continuous"
    elif upper.startswith("FDHP"):
        command_class = "motion_home_search"
    elif upper.startswith("GTHP"):
        command_class = "motion_home_return"
    elif upper.startswith("PS"):
        command_class = "position_preset"
    elif upper in ("ASSTP",) or upper.startswith("SSTP"):
        command_class = "normal_stop"
    elif upper in ("AESTP",) or upper.startswith("ESTP"):
        command_class = "emergency_stop"
    elif upper.startswith("SPD"):
        command_class = "speed_change"
    elif upper.startswith(("LN_SRQ", "RS_SRQ")):
        command_class = "configuration"

    channel = None
    match = re.match(
        r"^(?:ABS|REL|JOG[PN]|CSCAN[PN]|SCANH[PN]|SCAN[PN]|FDHP|GTHP|PS|STS|SSTP|ESTP|SPD(?:[HML]\??|\?))([0-9A-F])",
        upper,
    )
    if match:
        channel = int(match.group(1), 16)
    return {"command_class": command_class, "channel": channel}


# ---------------------------------------------------------------------------
# Move constraints (inter-channel software limits)
#
# Each rule is evaluated before every absolute or relative move.
# If the intended target position of `target_ch` satisfies (`target_op`,
# `target_val`), then the *current* position of `required_ch` must satisfy
# (`required_op`, `required_val`) — otherwise the move is rejected.
# `target_op`/`target_val` may be omitted entirely to make a rule
# unconditional — it then applies to every move of `target_ch`, regardless
# of the requested target position (used below for Ch11, where any rotation
# is unsafe while Ch8 is extended, not just rotation past some threshold).
#
# To add a new constraint, append a dict with the keys shown above.
# ---------------------------------------------------------------------------
# Collision boundary between the Detector (Ch9) and Microscope arm (Ch8).
# Ch9 must be at or beyond this pulse position (i.e. ≤ value) before Ch8 can
# move into the beam path (positive direction), and vice versa.
# This constant is the single source of truth: MOVE_CONSTRAINTS below and all
# UI-level validation code import or reference it.
CH9_CH8_SAFE_BOUNDARY = -30000

# Ch8 pulse position beyond which a rotating Ch11 (or a further-IN Ch8 move)
# risks colliding with the rotation stage. Ch8 does not conflict with Ch11
# immediately at Ch8 > 0 — there is some real mechanical margin before an
# actual collision is possible. NOT YET VERIFIED against real BL-18C
# hardware; re-check/adjust after hardware testing (see
# utils/stage/IMPLEMENTATION_DETAILS.md).
CH8_CH11_CONFLICT_BOUNDARY = 0

# Ch11 pulse range considered non-colliding while Ch8 is extended past
# CH8_CH11_CONFLICT_BOUNDARY (inclusive min, max). Not just exact θ=0° —
# real arm geometry likely tolerates some angular margin. NOT YET VERIFIED;
# re-check/adjust after hardware testing.
CH11_SAFE_RANGE_PULSES = (0, 0)

MOVE_CONSTRAINTS = [
    # Ch9 > CH9_CH8_SAFE_BOUNDARY requires Ch8 <= 0
    # Moving Ch9 TO the boundary or more negative (OUT direction) is always safe.
    # Only moving Ch9 INTO the beam path is restricted.
    {
        'target_ch': 9, 'target_op': '>', 'target_val': CH9_CH8_SAFE_BOUNDARY,
        'required': [
            {'ch': 8, 'op': '<=', 'val': 0},
        ],
    },
    # Ch8 > 0 requires Ch9 <= CH9_CH8_SAFE_BOUNDARY
    # Moving Ch8 TO 0 or more negative (OUT direction) is always safe.
    # Only moving Ch8 INTO the beam path is restricted.
    {
        'target_ch': 8, 'target_op': '>', 'target_val': 0,
        'required': [
            {'ch': 9, 'op': '<=', 'val': CH9_CH8_SAFE_BOUNDARY},
        ],
    },
    # Ch11 (rotation) may move only while Ch8 is retracted past the conflict
    # boundary. Unconditional: any rotation while Ch8 is extended is unsafe,
    # not just rotation toward a particular direction.
    {
        'target_ch': 11,
        'required': [
            {'ch': 8, 'op': '<=', 'val': CH8_CH11_CONFLICT_BOUNDARY},
        ],
    },
    # Ch8 may extend past the conflict boundary only while Ch11 sits within
    # CH11_SAFE_RANGE_PULSES of its home/zero position.
    {
        'target_ch': 8, 'target_op': '>', 'target_val': CH8_CH11_CONFLICT_BOUNDARY,
        'required': [
            {'ch': 11, 'op': '>=', 'val': CH11_SAFE_RANGE_PULSES[0]},
            {'ch': 11, 'op': '<=', 'val': CH11_SAFE_RANGE_PULSES[1]},
        ],
    },
]

_OPS = {'>=': ge, '<=': le, '>': gt, '<': lt, '==': eq}

# ---------------------------------------------------------------------------
# Software position limits and per-command move cap (optional, per channel).
#
# Both are `None` (disabled) by default — no mechanically-safe range has been
# supplied for the real hardware yet. Fill in real numbers here once known;
# the checks in move_ch_absolute()/move_ch_relative() start enforcing them
# immediately, same as MOVE_CONSTRAINTS above (raises ValueError, UIs already
# catch that and show a warning).
# ---------------------------------------------------------------------------
SOFT_LIMITS: dict = {ch: None for ch in range(1, 12)}       # ch -> (min, max) pulses, or None
MAX_MOVE_PULSES: dict = {ch: None for ch in range(1, 12)}   # ch -> max |diff| pulses, or None

# ---------------------------------------------------------------------------
# Pulse-to-physical-unit conversion for each channel
# Translation stages (Ch1–10): µm/pulse
# Rotation stage (Ch11): degrees/pulse
# ---------------------------------------------------------------------------
PULSE_SCALE: dict[int, float] = {
    1:  1.0,    # µm/pulse
    2:  2.0,    # µm/pulse
    3:  2.0,    # µm/pulse  Focus X
    4:  2.0,    # µm/pulse  Sample Y
    5:  0.11,   # µm/pulse  Sample Z
    6:  1.0,    # µm/pulse Microscope Z
    7:  0.2,    # µm/pulse Microscope X
    8:  1.0,    # µm/pulse  Microscope Y
    9:  10.0,   # µm/pulse  Detector (IN/OUT, X)
    10: 2.0,    # µm/pulse
    11: 0.004,  # deg/pulse
}

# ---------------------------------------------------------------------------
# Response validators, for send_cmd(..., validate=...).
#
# Each returns (True, "") when the reply matches what the command should
# return, or (False, reason) otherwise. A failed validation raises
# PM16CProtocolError instead of the caller silently adopting a bogus value
# (e.g. a leaked async "STOPx" notification, or a reply for the wrong
# channel) as a position/status reading.
# ---------------------------------------------------------------------------
_STOPX_RE = re.compile(r'^STOP([0-9A-Fa-f])$')

# Verified against the PM16C-04XD(L) rev.19 manual and real-controller
# responses.  A position is always an explicit sign followed by at least
# seven zero-padded decimal digits; values wider than seven digits expand as
# needed, up to the controller's signed pulse-position range.
_POSITION_RE = re.compile(r'^[+-][0-9]{7,10}$', re.ASCII)
_POSITION_MIN = -2_147_483_647
_POSITION_MAX = 2_147_483_647

# STSx? normally returns R(L)aPVHH+/-position.  If channel x is not one of
# the four channels mapped to the LCD, the controller preserves the fixed
# six-character header by returning V="-" and HH="--" (observed response:
# ``L7S----0107000`` -> header ``L7S---`` + position ``-0107000``).
_STSX_RE = re.compile(
    r'^(?P<mode>[RL])'
    r'(?P<channel>[0-9A-F])'
    r'(?P<state>[PNS])'
    r'(?:'
        r'(?P<ls_hold>[0-9A-F])(?P<status>[0-9A-F]{2})'
        r'|(?P<unmapped>---)'
    r')'
    r'(?P<position>[+-][0-9]{7,10})$',
    re.ASCII,
)


def _validate_position(position: str) -> "tuple[bool, str]":
    """Validate a signed, zero-padded PM16C pulse-position field."""
    if _POSITION_RE.fullmatch(position) is None:
        return False, (
            f"invalid position {position!r} "
            "(expected explicit sign and 7-10 decimal digits)"
        )
    value = int(position)
    if not (_POSITION_MIN <= value <= _POSITION_MAX):
        return False, (
            f"position {position!r} is outside the PM16C range "
            f"[{_POSITION_MIN}, {_POSITION_MAX}]"
        )
    return True, ""


def _validate_stsx(ch_str: str) -> Callable[[str], "tuple[bool, str]"]:
    """Validate an STSx? reply, including its queried channel and position."""
    def _validate(line: str):
        match = _STSX_RE.fullmatch(line)
        if match is None:
            return False, (
                "expected R/L + channel + P/N/S + either VHH or '---' + "
                "signed 7-10 digit position"
            )
        actual_ch = match.group('channel')
        if actual_ch != ch_str.upper():
            return False, (
                f"channel mismatch (expected Ch{ch_str.upper()}, "
                f"got Ch{actual_ch})"
            )
        return _validate_position(match.group('position'))
    return _validate


def _validate_sts_full(line: str):
    """Validate the complete four-display-channel STS? response."""
    parts = line.split('/')
    if len(parts) != 8:
        return False, f"expected 8 '/'-delimited fields, got {len(parts)}"

    header, states, ls_hold, statuses, *positions = parts
    if re.fullmatch(r'[RL][0-9A-F]{4}', header, re.ASCII) is None:
        return False, f"invalid mode/channel header {header!r}"
    if re.fullmatch(r'[PNS]{4}', states, re.ASCII) is None:
        return False, f"invalid motor-state field {states!r}"
    if re.fullmatch(r'[0-9A-F]{4}', ls_hold, re.ASCII) is None:
        return False, f"invalid LS/hold field {ls_hold!r}"
    if re.fullmatch(r'[0-9A-F]{8}', statuses, re.ASCII) is None:
        return False, f"invalid motor-status field {statuses!r}"
    for index, position in enumerate(positions, start=1):
        ok, reason = _validate_position(position)
        if not ok:
            return False, f"invalid position field {index}: {reason}"
    return True, ""


def _validate_spd(line: str):
    """SPD?x reply: exactly one of HSPD/MSPD/LSPD."""
    if line not in ('HSPD', 'MSPD', 'LSPD'):
        return False, f"expected HSPD/MSPD/LSPD, got {line!r}"
    return True, ""


def _validate_stq(line: str):
    """STQ? reply: R/L followed by the free-motor-slot count 0-4."""
    if len(line) != 2 or line[0] not in 'RL' or line[1] not in '01234':
        return False, f"expected R/L + digit 0-4, got {line!r}"
    return True, ""


def _parse_stsx_reply(line: str):
    """Parse an STSx?-shaped reply: R(L)aPVHH±digits.

    Returns (mode, channel_hex, state, ls_hold_nibble, status_byte, position)
    where `position` is the raw (still string) signed pulse count.
    """
    match = _STSX_RE.fullmatch(line)
    if match is None:
        raise PM16CProtocolError(f"Malformed STSx? response: {line!r}")
    ok, reason = _validate_position(match.group('position'))
    if not ok:
        raise PM16CProtocolError(f"Malformed STSx? response: {line!r} ({reason})")

    mode = match.group('mode')
    channel_hex = match.group('channel')
    state = match.group('state')
    ls_hold_nibble = match.group('ls_hold') or '-'
    status_byte = match.group('status') or '--'
    position = match.group('position')
    return mode, channel_hex, state, ls_hold_nibble, status_byte, position


class PM16CController:
    def __init__(self, ip, port, debug=False):
        self.ip = ip
        self.port = port
        self.debug = debug
        self.terminator = '\r\n'
        self.client = None
        # Bytes received but not yet consumed as a full \r\n-terminated line.
        # Touched ONLY by the arbiter's communication thread (via
        # _execute_wire_task); kept across commands so a second line arriving
        # in the same TCP segment as the first (e.g. a real reply following
        # an unsolicited STOPx notification) is never silently discarded.
        self._recv_buffer = b""
        self.audit = PM16CAuditLogger()
        self._command_id_lock = threading.Lock()
        self._next_command_id = 0
        # Motion ownership + single-comm-thread arbiter.  There is no
        # communication RLock any more: serialization of socket transactions
        # is the comm thread itself, and compound sequences (constraint
        # check → REM → move) run as single arbiter transactions.
        self.controller_id = f"pm16c:{ip}:{port}"
        self.coordinator = MotionCoordinator(self.controller_id, self.audit)
        self.arbiter = CommandArbiter(
            self._execute_wire_task, self.coordinator, self.audit
        )
        # Stop confirmation runs on its own thread so its 100 ms waits never
        # occupy the comm thread (UI queries interleave during confirmation).
        self._confirm_queue: "queue.Queue" = queue.Queue()
        self._confirm_thread: "threading.Thread | None" = None
        self._stop_progress = "idle"
        self._stop_progress_lock = threading.Lock()
        # Last exception from a fire-and-forget command
        # (move_ch_relative_unchecked); workers check this in their finally.
        self.last_async_error: "Exception | None" = None
        self.state_monitor = StageStateMonitor(
            self.get_ch_status,
            _parse_stsx_reply,
            self.audit,
        )

    def connect(self):
        """ Connect the controller and delete remaining buffers if exist """
        self.audit.start(
            controller_ip=self.ip,
            controller_port=self.port,
            simulation=False,
            monitored_channels=list(range(1, 12)),
            hostname=socket.gethostname(),
        )
        self.audit.record("connect_attempt", controller_ip=self.ip, controller_port=self.port)
        print(f"Attempting to connect, {self.ip}:{self.port}...")
        try:
            self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client.settimeout(2.0)
            self.client.connect((self.ip, self.port))
        except Exception as exc:
            self.audit.record(
                "connect_failure", level="ERROR", error_type=type(exc).__name__, error=str(exc)
            )
            if self.client is not None:
                self.client.close()
                self.client = None
            self.audit.stop()
            raise

        # Delete rec. buffer
        self.client.settimeout(0.1)
        try:
            while True:
                self.client.recv(1024)
        except socket.timeout:
            pass
        self.client.settimeout(2.0)
        self._recv_buffer = b""
        print(f"Connected to the stepping motor controller at {self.ip} (PORT: {self.port})")
        self.audit.record("connect_success")

        # Start the communication thread before anything can enqueue.
        self.arbiter.start()
        # Clear any stale LAN-SRQ arm flags left over from a previous client.
        # STOPx filtering in the wire executor stays in place regardless of
        # this flag's state, since another client/interface on the unit
        # could re-arm it at any time.  Internal path: no lease policy.
        self._send_internal("LN_SRQG0", has_response=False)
        self._confirm_thread = threading.Thread(
            target=self._stop_confirm_loop, name="PM16C-stop-confirm", daemon=True
        )
        self._confirm_thread.start()
        self.state_monitor.start()

    def disconnect(self):
        """ Disconnect from the contrlller """
        self.audit.record("disconnect_start")
        self.state_monitor.stop()
        # Wake and stop the confirmation thread.
        if self._confirm_thread is not None:
            self._confirm_queue.put(None)
            self._confirm_thread.join(timeout=2.0)
            self._confirm_thread = None
        # Complete every still-queued Future with a comms error and join the
        # comm thread, THEN close the socket.
        self.arbiter.shutdown(
            PM16CQueueClosedError("PM16C controller was disconnected")
        )
        if self.client:
            self.client.close()
            self.client = None
            self._recv_buffer = b""
            print("Disconnected.")
        self.audit.record("disconnect_complete")
        self.audit.stop()

    def shutdown(self):
        """Application-exit teardown: best-effort LOC, then disconnect.

        Replaces the old ``switch_to_loc(); disconnect()`` pair in
        main.py's closeEvent — switch_to_loc() now requires a lease, and at
        shutdown there is nothing to own.
        """
        try:
            self._send_internal("LOC", has_response=False)
        except Exception:
            pass
        self.disconnect()

    def _read_line(self) -> str:
        """Return the next \\r\\n-terminated line, blocking on the socket as needed.

        Serves a line already sitting in self._recv_buffer first (from a
        previous recv() that returned more than one line) instead of
        discarding it — the bug this fixes: the old implementation started
        from an empty buffer on every send_cmd() call, so any bytes after
        the first line in a batch were silently dropped forever.
        """
        term = self.terminator.encode('ascii')
        while term not in self._recv_buffer:
            chunk = self.client.recv(4096)
            if not chunk:
                raise PM16CCommError("Connection closed by the controller")
            self._recv_buffer += chunk
        line, self._recv_buffer = self._recv_buffer.split(term, 1)
        return line.decode('ascii').strip()

    # ── command submission (callable from any thread) ────────────────────────

    def _next_cmd_id(self) -> int:
        with self._command_id_lock:
            self._next_command_id += 1
            return self._next_command_id

    def send_cmd(self, cmd, has_response=True,
                 validate: Optional[Callable[[str], "tuple[bool, str]"]] = None,
                 *, motion: "MotionLease | None" = None):
        """
        Send a command to the controller via the priority command queue.

        The command is classified (see _command_metadata) and gated:

        * queries — no lease, query priority;
        * motion / speed / mode-change / position-preset — require a valid
          MotionLease passed as ``motion=`` (MotionLeaseRequiredError /
          MotionRevokedError otherwise);
        * a typed ``ASSTP``/``AESTP`` is redirected into the full stop path
          (lease revocation, coalescing, confirmation) — it can NOT be used
          to bypass the ownership machinery;
        * per-channel SSTPx/ESTPx — no lease, stop priority;
        * configuration / unknown — refused while motion is owned.

        Blocks until the comm thread has executed the command (sync
        semantics preserved from the pre-queue implementation).

        `validate`, if given, is called with the response line and must
        return (True, "") to accept it or (False, reason) to reject it —
        rejection raises PM16CProtocolError rather than the bogus line being
        handed back to the caller.
        """
        metadata = _command_metadata(cmd)
        command_class = metadata["command_class"]
        source = _infer_source()
        upper = cmd.strip().upper()

        if upper == "AESTP":
            return self.request_emergency_stop(source=source).result(
                timeout=STOP_CONFIRM_TIMEOUT_S + 10.0
            )
        if upper == "ASSTP":
            return self.request_normal_stop(source=source).result(
                timeout=STOP_CONFIRM_TIMEOUT_S + 10.0
            )

        if command_class in ("normal_stop", "emergency_stop"):
            # Per-channel stop: always allowed, runs at stop priority, but
            # does not revoke the whole-controller lease.
            priority = (PRIORITY_EMERGENCY_STOP
                        if command_class == "emergency_stop"
                        else PRIORITY_NORMAL_STOP)
            lease = None
        elif command_class == "query":
            priority = PRIORITY_QUERY
            lease = None
        elif command_class in (
            "motion_absolute", "motion_relative", "motion_continuous",
            "motion_home_search", "motion_home_return", "position_preset",
            "speed_change", "mode_change",
        ):
            self.coordinator.validate(motion)
            priority = PRIORITY_MOTION
            lease = motion
        elif command_class == "configuration":
            if not self.coordinator.is_available():
                raise MotionNotAvailableError(
                    "Configuration commands are refused while motion is "
                    "owned or a stop/recovery is in progress.",
                    holder=self.coordinator.holder_info(),
                )
            priority = PRIORITY_MOTION
            lease = None
        else:  # unknown
            if not self.coordinator.is_available():
                raise MotionNotAvailableError(
                    "Unclassified commands are refused while motion is "
                    "owned or a stop/recovery is in progress.",
                    holder=self.coordinator.holder_info(),
                )
            priority = PRIORITY_QUERY
            lease = None

        return self._submit_wire(
            cmd, has_response, validate,
            source=source, metadata=metadata, priority=priority, lease=lease,
        ).result()

    def _send_internal(self, cmd, has_response=True, validate=None):
        """Private no-lease enqueue for controller-internal commands only:
        connect-time configuration (LN_SRQG0) and the shutdown LOC.  Never
        expose this to application code."""
        metadata = _command_metadata(cmd)
        return self._submit_wire(
            cmd, has_response, validate,
            source=_infer_source(), metadata=metadata,
            priority=PRIORITY_MOTION, lease=None,
        ).result()

    def _submit_wire(self, cmd, has_response, validate, *, source, metadata,
                     priority, lease):
        command_id = self._next_cmd_id()

        def execute(wire):
            return wire(
                cmd, has_response=has_response, validate=validate,
                command_id=command_id, source=source, metadata=metadata,
                lease=lease,
            )

        return self.arbiter.submit(
            execute, priority=priority, command=cmd,
            command_class=metadata["command_class"], lease=lease, source=source,
        )

    def _should_log_to_console(self, is_sts_trace: bool) -> bool:
        """Suppress terminal spam from the background STS? idle poll.

        Every other command (motion, stop, speed, ...) always logs; STS?
        traces only log while a channel is actually moving/stopping, since
        the background monitor otherwise prints one line per channel every
        idle_interval seconds forever.
        """
        if not is_sts_trace:
            return True
        return self.state_monitor.is_moving_cached()

    # ── wire execution (COMM THREAD ONLY) ────────────────────────────────────

    def _execute_wire_task(self, cmd, has_response=True, validate=None, *,
                           command_id=None, source="unknown", metadata=None,
                           lease=None, stop_context=None):
        """Perform one socket transaction.  Runs exclusively on the
        arbiter's communication thread — never call this directly."""
        if self.client is None:
            raise ConnectionError(
                "PM16C controller is not connected (client is None) — call connect() first."
            )
        if metadata is None:
            metadata = _command_metadata(cmd)
        if command_id is None:
            command_id = self._next_cmd_id()
        full_cmd = f"{cmd}{self.terminator}"
        is_sts_trace = cmd.upper().startswith("STS") and "?" in cmd
        tx_started = time.monotonic()
        if is_sts_trace:
            self.audit.record(
                "tx_attempt",
                persist=False,
                command_id=command_id,
                command=cmd,
                wire=full_cmd.replace("\r", "\\r").replace("\n", "\\n"),
                expects_response=has_response,
                source=source,
                **metadata,
            )
        logger.debug("TX source=%s command=%s", source, cmd)
        try:
            payload = full_cmd.encode('ascii')
            self.client.sendall(payload)
        except Exception as exc:
            self.audit.record(
                "tx_failed" if is_sts_trace else "control_command",
                level="ERROR",
                command_id=command_id,
                command=cmd,
                source=source,
                outcome="send_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **metadata,
            )
            raise
        if is_sts_trace:
            self.audit.record(
                "tx_sent",
                persist=False,
                command_id=command_id,
                command=cmd,
                bytes_sent=len(payload),
                source=source,
            )
        self._track_sent_command(cmd, metadata, source, lease=lease,
                                 stop_context=stop_context)

        if not has_response:
            if not is_sts_trace:
                self.audit.record(
                    "control_command",
                    command_id=command_id,
                    command=cmd,
                    source=source,
                    outcome="sent",
                    importance="high" if metadata["command_class"] != "query" else "normal",
                    **metadata,
                )
            if self.debug and self._should_log_to_console(is_sts_trace):
                print(f"Sending: {cmd:<10} without waiting for the response")
            return None

        # Read lines until we get one that isn't an unsolicited async
        # notification (e.g. "STOP4" pushed the instant channel 4 stops,
        # firmware V1.42+) — such a line is never the reply to the
        # command we just sent, and must not be consumed as one.
        while True:
            try:
                line = self._read_line()
            except socket.timeout:
                self.audit.record(
                    "rx_timeout" if is_sts_trace else "control_command",
                    level="ERROR",
                    command_id=command_id,
                    command=cmd,
                    source=source,
                    outcome="timeout",
                    **metadata,
                )
                raise PM16CTimeoutError(f"'{cmd}' timed out waiting for a response")
            logger.debug("RX raw=%s", line)
            if _STOPX_RE.match(line):
                stop_match = _STOPX_RE.match(line)
                self.audit.record(
                    "controller_notification",
                    command_id=None,
                    raw=line,
                    classification="stop_notification",
                    channel=int(stop_match.group(1), 16),
                    source="controller_async",
                )
                continue
            break

        if self.debug and self._should_log_to_console(is_sts_trace):
            print(f"Command: {cmd:<10} -> Response: {line}")

        if validate is not None:
            ok, reason = validate(line)
            if not ok:
                self.audit.record(
                    "rx_line" if is_sts_trace else "control_command",
                    level="ERROR",
                    command_id=command_id,
                    command=cmd,
                    raw=line,
                    classification="unexpected_response",
                    outcome="invalid_response",
                    validation_error=reason,
                    source=source,
                    **metadata,
                )
                raise PM16CProtocolError(f"Unexpected response to {cmd!r}: {line!r} ({reason})")

        latency_ms = round((time.monotonic() - tx_started) * 1000, 3)
        if is_sts_trace:
            self.audit.record(
                "rx_line",
                persist=False,
                command_id=command_id,
                raw=line,
                classification="query_response",
                source=source,
                latency_ms=latency_ms,
            )
        else:
            self.audit.record(
                "control_command",
                command_id=command_id,
                command=cmd,
                response=line,
                source=source,
                outcome="success",
                importance="high" if metadata["command_class"] != "query" else "normal",
                latency_ms=latency_ms,
                **metadata,
            )

        return line

    def _track_sent_command(self, cmd: str, metadata: dict, source: str,
                            lease: "MotionLease | None" = None,
                            stop_context: "dict | None" = None) -> None:
        """Associate every raw motion/preset/stop command with later positions.

        This lives at the wire boundary rather than only in move_ch_*(), so a
        development console or future direct send_cmd() caller is still fully
        attributable in the audit log.
        """
        command_class = metadata.get("command_class")
        channel = metadata.get("channel")
        if command_class in ("normal_stop", "emergency_stop"):
            self.state_monitor.note_stop(
                command=cmd,
                source=source,
                channels=None if channel is None else [channel],
                revoked_lease_id=(stop_context or {}).get("revoked_lease_id"),
            )
            return
        if channel is None or command_class not in (
            "motion_absolute",
            "motion_relative",
            "motion_continuous",
            "motion_home_search",
            "motion_home_return",
            "position_preset",
        ):
            return

        target = None
        relative_delta = None
        upper = cmd.upper()
        try:
            if command_class in ("motion_absolute", "position_preset"):
                prefix_len = 4 if command_class == "motion_absolute" else 3
                target = int(upper[prefix_len:])
            elif command_class == "motion_relative":
                relative_delta = int(upper[4:])
        except (TypeError, ValueError):
            target = None
            relative_delta = None
        self.state_monitor.note_motion(
            channel,
            cmd,
            target,
            source,
            relative_delta=relative_delta,
            lease=lease,
        )

    def switch_to_rem(self, *, motion: "MotionLease | None" = None):
        self.send_cmd("REM", has_response=False, motion=motion)

    def switch_to_loc(self, *, motion: "MotionLease | None" = None):
        self.send_cmd("LOC", has_response=False, motion=motion)

    def get_free_motor_slots(self) -> int:
        """STQ?: number of motor slots (0-4) not currently driving — the only
        status query that reflects *all* channels, not just the 4 mapped to
        the front-panel display window (see is_all_motors_stopped)."""
        line = self.send_cmd("STQ?", validate=_validate_stq)
        return int(line[1])

    def is_all_motors_stopped(self) -> bool:
        """True if no channel (of all 11 in use) is currently moving.

        Deliberately based on STQ?'s free-slot count rather than STS?'s PNNS
        field: STS? only reports the 4 channels currently mapped to the
        display window, so a moving channel outside that window used to be
        invisible to this check.
        """
        return self.get_free_motor_slots() == 4

    def wait_until_stop(self, confirm_count=4, stay_in_rem=False,
                        *, motion: "MotionLease | None" = None):
        """Poll until all motors report stopped, then (unless stay_in_rem)
        switch back to LOC.

        The STQ? polls are plain queries (no lease); the trailing LOC is a
        mode change, so ``motion=`` is required when ``stay_in_rem=False``.
        If the lease was revoked while waiting (another app requested a
        stop), the LOC is skipped — the stop transaction already sent it —
        and the method returns normally; the owner's next motion call will
        raise MotionRevokedError.
        """
        if not stay_in_rem and motion is None:
            raise MotionLeaseRequiredError(
                "wait_until_stop(stay_in_rem=False) switches to LOC and "
                "therefore requires motion=<lease>."
            )
        if self.debug: print("--- Waiting until the operation is completed ---")
        consecutive = 0
        while True:
            if self.is_all_motors_stopped():
                consecutive += 1
                if consecutive >= confirm_count:
                    break
            else:
                consecutive = 0
            time.sleep(0.1)

        if stay_in_rem:
            if self.debug: print("--- Operation completed --- (staying in REM)")
            return
        if not self.coordinator.is_valid(motion):
            # Revoked while waiting: the stop path owns the LOC.
            if self.debug: print("--- Operation completed --- (lease revoked; LOC left to the stop path)")
            return
        if self.debug: print("--- Operation completed ---\n--- Switch to LOC ---")
        self.switch_to_loc(motion=motion)

    def get_ch_is_moving(self, ch) -> bool:
        """True if channel `ch` specifically is currently driving (P or N)."""
        line = self.get_ch_status(ch)
        _, _, state, _, _, _ = _parse_stsx_reply(line)
        return state in ('P', 'N')

    def wait_ch_until_stop(self, ch, poll_interval=0.1, timeout: Optional[float] = None):
        """Poll channel `ch`'s own status until it reports stopped.

        Raises PM16CTimeoutError on timeout — a timeout is never treated as
        "stopped".
        """
        start = time.monotonic()
        while self.get_ch_is_moving(ch):
            if timeout is not None and time.monotonic() - start > timeout:
                raise PM16CTimeoutError(f"Ch{ch} did not stop within {timeout}s")
            time.sleep(poll_interval)

    def wait_channels_until_stop(self, channels: Iterable[int], poll_interval=0.1, timeout: Optional[float] = None):
        """Like wait_ch_until_stop, for several channels moved together."""
        start = time.monotonic()
        remaining = set(channels)
        while remaining:
            remaining = {ch for ch in remaining if self.get_ch_is_moving(ch)}
            if not remaining:
                return
            if timeout is not None and time.monotonic() - start > timeout:
                raise PM16CTimeoutError(f"Channels {sorted(remaining)} did not stop within {timeout}s")
            time.sleep(poll_interval)

    def print_invalid_ch(self):
        print("Invalid ch input.")

    def stringify_ch_numbers(self, ch):
        if ch <= 0 or ch >= 12:
            self.print_invalid_ch()
            return None # error
        elif 1 <= ch <= 9:
            return f"{ch}"
        elif ch == 10:
            return "A"
        elif ch == 11:
            return "B"
        else:
            self.print_invalid_ch()
            return None

    def _check_move_constraints_using(self, ch, target_pos, read_pos):
        """MOVE_CONSTRAINTS check with an injectable position reader.

        ``read_pos(ch) -> str | None`` supplies the current position of a
        required channel.  Transactions running on the comm thread pass a
        wire-level reader; the public check_move_constraints passes
        self.get_ch_pos.
        """
        for rule in MOVE_CONSTRAINTS:
            if rule['target_ch'] != ch:
                continue
            target_op = rule.get('target_op')
            if target_op is not None and not _OPS[target_op](target_pos, rule['target_val']):
                continue
            for req in rule['required']:
                req_str = read_pos(req['ch'])
                if req_str is None:
                    return False, (
                        f"Cannot read Ch{req['ch']} position "
                        f"(required for limit check on Ch{ch})"
                    )
                if not _OPS[req['op']](int(req_str), req['val']):
                    return False, (
                        f"Move blocked: Ch{ch} → {target_pos:+} requires "
                        f"Ch{req['ch']} {req['op']} {req['val']:+}, "
                        f"but current position is {int(req_str):+}"
                    )
        return True, ""

    def check_move_constraints(self, ch, target_pos):
        """Check MOVE_CONSTRAINTS before a move.

        Returns (True, "") when safe.
        Returns (False, reason) when a constraint would be violated.
        Each rule's 'required' list is checked in order; all conditions must hold.
        """
        return self._check_move_constraints_using(ch, target_pos, self.get_ch_pos)

    def check_soft_limits(self, ch, target_pos):
        """Optional absolute-position soft limit (see SOFT_LIMITS). Disabled
        (returns True) for any channel with no configured range."""
        limits = SOFT_LIMITS.get(ch)
        if limits is None:
            return True, ""
        lo, hi = limits
        if not (lo <= target_pos <= hi):
            return False, (
                f"Move blocked: Ch{ch} target {target_pos:+} is outside the "
                f"configured soft limit [{lo:+}, {hi:+}]"
            )
        return True, ""

    def check_max_move(self, ch, diff):
        """Optional per-command move-distance cap (see MAX_MOVE_PULSES).
        Disabled (returns True) for any channel with no configured cap."""
        cap = MAX_MOVE_PULSES.get(ch)
        if cap is None:
            return True, ""
        if abs(diff) > cap:
            return False, (
                f"Move blocked: Ch{ch} relative move {diff:+} exceeds the "
                f"configured max single move of {cap} pulses"
            )
        return True, ""

    def _wire_read_pos(self, wire, ch, *, source):
        """Read channel ch's position via a raw wire call (COMM THREAD ONLY,
        from inside a transaction closure)."""
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        line = wire(
            f"STS{ch_str}?", has_response=True,
            validate=_validate_stsx(ch_str), source=source,
        )
        self.state_monitor.observe(line, source=source)
        _, _, _, _, _, position = _parse_stsx_reply(line)
        return position

    def move_ch_relative(self, ch, diff, *, motion: "MotionLease | None" = None):
        # Runs as ONE transaction on the comm thread, so no other command
        # can land between the constraint check and the actual move (e.g.
        # switching back to LOC, or moving the channel this move's
        # constraint check depends on).  The lease is re-validated between
        # wire commands: a stop request revokes it in memory and aborts the
        # transaction BEFORE the motion command reaches the wire.
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        source = _infer_source()

        def txn(wire):
            read_pos = lambda rch: self._wire_read_pos(wire, rch, source=source)
            current_str = read_pos(ch)
            if current_str is None:
                raise ValueError(
                    f"Ch{ch} の現在位置を取得できませんでした。\n"
                    "通信エラーの可能性があるため、衝突防止のため相対値移動をブロックしました。"
                )
            target = int(current_str) + diff
            ok, msg = self._check_move_constraints_using(ch, target, read_pos)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_max_move(ch, diff)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_soft_limits(ch, target)
            if not ok:
                raise ValueError(msg)
            self.coordinator.validate(motion)
            wire("REM", has_response=False, source=source, lease=motion)
            self.coordinator.validate(motion)
            logger.info(
                "MOVE source=%s ch=%s current=%s target=%+d",
                source, ch, current_str, target,
            )
            wire(f"REL{ch_str}{diff:+}", has_response=False, source=source,
                 lease=motion)
            return None

        return self.arbiter.submit(
            txn, priority=PRIORITY_MOTION, command=f"REL{ch_str}{diff:+}",
            command_class="motion_relative", lease=motion, source=source,
        ).result()

    def move_ch_absolute(self, ch, target, *, motion: "MotionLease | None" = None):
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        source = _infer_source()

        def txn(wire):
            read_pos = lambda rch: self._wire_read_pos(wire, rch, source=source)
            ok, msg = self._check_move_constraints_using(ch, target, read_pos)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_soft_limits(ch, target)
            if not ok:
                raise ValueError(msg)
            current_str = read_pos(ch)
            if current_str is not None:
                ok, msg = self.check_max_move(ch, target - int(current_str))
                if not ok:
                    raise ValueError(msg)
            self.coordinator.validate(motion)
            wire("REM", has_response=False, source=source, lease=motion)
            self.coordinator.validate(motion)
            logger.info(
                "MOVE source=%s ch=%s current=%s target=%+d",
                source, ch, current_str, target,
            )
            wire(f"ABS{ch_str}{target:+}", has_response=False, source=source,
                 lease=motion)
            return None

        return self.arbiter.submit(
            txn, priority=PRIORITY_MOTION, command=f"ABS{ch_str}{target:+}",
            command_class="motion_absolute", lease=motion, source=source,
        ).result()

    def move_ch_relative_unchecked(self, ch, diff, *, motion: "MotionLease | None" = None):
        """Fire a relative move with no position round-trip and no
        constraint check — assumes the caller is already in REM mode and has
        already validated the move.

        For timing-sensitive loops only (e.g. the Rad-icon rotation scan's
        per-step REL, fired immediately after starting an exposure so both
        finish at roughly the same time).  Lease validation is memory-only
        and the method returns as soon as the command is ENQUEUED — it does
        not wait for the send.  A send failure is recorded in the audit log
        and in self.last_async_error, which latency-loop workers should
        check in their finally block.
        """
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        source = _infer_source()
        logger.info("MOVE source=%s ch=%s target=%+d (unchecked)", source, ch, diff)

        def txn(wire):
            wire(f"REL{ch_str}{diff:+}", has_response=False, source=source,
                 lease=motion)
            return None

        future = self.arbiter.submit(
            txn, priority=PRIORITY_MOTION, command=f"REL{ch_str}{diff:+}",
            command_class="motion_relative", lease=motion, source=source,
        )
        future.add_done_callback(self._note_async_outcome)
        return None

    def _note_async_outcome(self, future):
        exc = future.exception()
        if exc is not None:
            self.last_async_error = exc
            self.audit.record(
                "async_command_failed",
                level="ERROR",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def get_ch_pos(self, ch):
        line = self.get_ch_status(ch)
        if line is None:
            return None
        _, _, _, _, _, position = _parse_stsx_reply(line)
        return position

    def get_ch_status(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is not None:
            source = _infer_source()
            line = self.send_cmd(f"STS{ch_str}?", validate=_validate_stsx(ch_str))
            self.state_monitor.observe(line, source=source)
            return line

    def get_cached_ch_state(self, ch, max_age=None):
        """Return the latest central-monitor observation without socket I/O."""
        return self.state_monitor.get_state(ch, max_age=max_age)

    def get_cached_states(self, channels=None, max_age=None):
        """Return cached ChannelState objects keyed by channel, without I/O."""
        return self.state_monitor.get_states(channels, max_age=max_age)

    def get_cached_is_moving(self):
        """Return cached/expected motion state without socket I/O."""
        return self.state_monitor.is_moving_cached()

    def get_status(self):
        return self.send_cmd("STS?", validate=_validate_sts_full)

    def get_is_moving(self):
        return not self.is_all_motors_stopped()

    def get_ch_backlash(self, ch):
        return self.send_cmd(f"B{ch}?")

    def set_ch_backlash(self, ch, target, *, motion: "MotionLease | None" = None):
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        source = _infer_source()

        def txn(wire):
            self.coordinator.validate(motion)
            wire("REM", has_response=False, source=source, lease=motion)
            wire(f"B{ch_str}{target:+04}", has_response=False, source=source,
                 lease=motion)
            wire("LOC", has_response=False, source=source, lease=motion)
            return None

        return self.arbiter.submit(
            txn, priority=PRIORITY_MOTION, command=f"B{ch_str}{target:+04}",
            command_class="configuration", lease=motion, source=source,
        ).result()

    def get_ch_spped(self, ch):
        """ return HSPD, MSPD, LSPD """
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"SPD?{ch_str}", validate=_validate_spd)

    def get_ch_speed(self, ch):
        """Alias for get_ch_spped (fixes the original name's typo).

        Prefer this name in new code; get_ch_spped is kept for existing callers.
        """
        return self.get_ch_spped(ch)

    def get_ch_speed_value(self, ch, level: str) -> "int | None":
        """Read the actual pps register value for channel ch's L/M/H speed setting.

        *level* is one of 'L', 'M', 'H'. Returns pps as int, or None on error.
        """
        if level not in ("L", "M", "H"):
            return None
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        response = self.send_cmd(f"SPD{level}?{ch_str}")
        if response is None:
            return None
        try:
            return int(response.strip())
        except ValueError:
            return None

    def set_ch_speed_value(self, ch, level: str, pps: int,
                           *, motion: "MotionLease | None" = None) -> None:
        """Set the actual pps register value for channel ch's L/M/H speed setting."""
        if level not in ("L", "M", "H"):
            return
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return
        source = _infer_source()

        def txn(wire):
            self.coordinator.validate(motion)
            wire("REM", has_response=False, source=source, lease=motion)
            wire(f"SPD{level}{ch_str}{pps}", has_response=False, source=source,
                 lease=motion)
            wire("LOC", has_response=False, source=source, lease=motion)
            return None

        self.arbiter.submit(
            txn, priority=PRIORITY_MOTION, command=f"SPD{level}{ch_str}{pps}",
            command_class="speed_change", lease=motion, source=source,
        ).result()

    def get_ch_lspd(self, ch) -> "int | None":
        """Read the LSPD register value for channel ch.  Returns pps as int, or None on error."""
        return self.get_ch_speed_value(ch, "L")

    def set_ch_lspd(self, ch, pps: int, *, motion: "MotionLease | None" = None) -> None:
        """Set the LSPD register for channel ch to pps [pulses per second]."""
        self.set_ch_speed_value(ch, "L", pps, motion=motion)

    def set_ch_speed(self, ch, speed="M", stay_in_rem=False,
                     *, motion: "MotionLease | None" = None):
        if speed not in ("L", "M", "H"):
            return
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return
        source = _infer_source()

        def txn(wire):
            self.coordinator.validate(motion)
            wire("REM", has_response=False, source=source, lease=motion)
            wire(f"SPD{speed}{ch_str}", has_response=False, source=source,
                 lease=motion)
            if not stay_in_rem:
                wire("LOC", has_response=False, source=source, lease=motion)
            return None

        self.arbiter.submit(
            txn, priority=PRIORITY_MOTION, command=f"SPD{speed}{ch_str}",
            command_class="speed_change", lease=motion, source=source,
        ).result()

    def read_backward_limit(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"BL?{ch_str}")

    def read_forward_limit(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"FL?{ch_str}")

    # ── motion ownership API ─────────────────────────────────────────────────

    def acquire_motion(self, owner: str, operation: str,
                       *, timeout: "float | None" = None) -> MotionLease:
        """Acquire controller-wide motion ownership (raises immediately if
        another app owns motion — see MotionCoordinator.acquire)."""
        return self.coordinator.acquire(owner, operation, timeout=timeout)

    def release_motion(self, lease: MotionLease) -> bool:
        """Conditional-no-op release; never raises (safe in finally)."""
        return self.coordinator.release(lease)

    @contextmanager
    def motion_session(self, owner: str, operation: str,
                       *, timeout: "float | None" = None):
        """Context manager: acquire on entry, always release on exit."""
        lease = self.acquire_motion(owner, operation, timeout=timeout)
        try:
            yield lease
        finally:
            self.release_motion(lease)

    def is_motion_available(self) -> bool:
        """Advisory only — the authoritative check is acquire_motion()."""
        return self.coordinator.is_available()

    def get_motion_holder(self) -> "dict | None":
        return self.coordinator.holder_info()

    # ── stops (no lease required; always accepted) ───────────────────────────

    def _set_stop_progress(self, state: str) -> None:
        with self._stop_progress_lock:
            self._stop_progress = state

    def get_stop_progress(self) -> str:
        """One of "idle" | "queued" | "sent_confirming" | "confirmed" |
        "failed" — for UI stop-progress display (poll alongside the stop
        Future)."""
        with self._stop_progress_lock:
            return self._stop_progress

    def request_normal_stop(self, *, source: "str | None" = None):
        """Decelerated all-axis stop (ASSTP).  Returns a Future that
        resolves once the stop is CONFIRMED (all motors observed stopped);
        never blocks the calling thread on socket I/O."""
        return self._request_stop(emergency=False, source=source)

    def request_emergency_stop(self, *, source: "str | None" = None):
        """Immediate all-axis stop (AESTP).  See request_normal_stop."""
        return self._request_stop(emergency=True, source=source)

    def _request_stop(self, *, emergency: bool, source: "str | None"):
        source = source or _infer_source()
        # Instant memory revocation: queued motion dies at dequeue, a
        # running transaction aborts at its next between-wire validate.
        ticket = self.coordinator.revoke_for_stop(source=source,
                                                  emergency=emergency)
        cmd = "AESTP" if emergency else "ASSTP"
        self._set_stop_progress("queued")
        command_id = self._next_cmd_id()
        metadata = _command_metadata(cmd)
        holder = {}
        future_ready = threading.Event()

        def stop_txn(wire):
            # One atomic transaction: stop + LOC with nothing in between
            # (fixes the historical normal_stop() non-atomicity).
            try:
                wire(cmd, has_response=False, command_id=command_id,
                     source=source, metadata=metadata, stop_context=ticket)
            except Exception:
                self.coordinator.note_stop_send_failed(ticket)
                self._set_stop_progress("failed")
                raise
            self.coordinator.note_stop_sent(ticket)
            try:
                wire("LOC", has_response=False, source=source)
            except Exception as exc:
                # The stop itself is on the wire; a LOC failure must not be
                # reported as a failed stop.
                self.audit.record(
                    "stop_loc_failed", level="ERROR",
                    error_type=type(exc).__name__, error=str(exc),
                )
            self._set_stop_progress("sent_confirming")
            future_ready.wait(timeout=5.0)
            self._confirm_queue.put({
                "ticket": ticket,
                "future": holder.get("future"),
                "on_success": lambda: self.coordinator.note_stop_confirmed(ticket),
                "on_failure": lambda: self.coordinator.note_stop_confirm_failed(ticket),
                "label": cmd,
            })
            return DEFERRED  # resolved by the confirmation thread

        try:
            future = self.arbiter.submit_stop(
                stop_txn, emergency=emergency, command=cmd, source=source,
            )
        except Exception:
            # The lease was already revoked above (memory-only, cannot
            # fail); if the stop task couldn't even be enqueued (e.g. the
            # arbiter isn't running), there is no guarantee AESTP/ASSTP
            # ever reaches the wire. Treat this the same as a send failure:
            # fall to RECOVERY_REQUIRED rather than leaving the coordinator
            # stuck in REVOKED_STOPPING with no task to resolve it.
            self.coordinator.note_stop_send_failed(ticket)
            self._set_stop_progress("failed")
            raise
        holder["future"] = future
        future_ready.set()
        return future

    def normal_stop(self, *, source: "str | None" = None):
        """Synchronous wrapper for worker-thread use — blocks until the stop
        is confirmed.  Do NOT call from the Qt main thread; use
        request_normal_stop() there."""
        return self.request_normal_stop(
            source=source or _infer_source()
        ).result(timeout=STOP_CONFIRM_TIMEOUT_S + 10.0)

    def emergency_stop(self, *, source: "str | None" = None):
        """Synchronous wrapper for worker-thread use — see normal_stop."""
        return self.request_emergency_stop(
            source=source or _infer_source()
        ).result(timeout=STOP_CONFIRM_TIMEOUT_S + 10.0)

    def recover_motion(self, *, source: str):
        """Operator-initiated ownership recovery: revoke, emergency-stop,
        confirm, bump generation, force-release.  Returns a Future resolving
        True on success; on failure the coordinator stays RECOVERY_REQUIRED
        and new motion remains refused."""
        ticket = self.coordinator.force_recover_begin(source=source)
        command_id = self._next_cmd_id()
        metadata = _command_metadata("AESTP")
        holder = {}
        future_ready = threading.Event()

        def recover_txn(wire):
            try:
                wire("AESTP", has_response=False, command_id=command_id,
                     source=source, metadata=metadata)
                wire("LOC", has_response=False, source=source)
            except Exception:
                self.coordinator.force_recover_complete(False, source=source)
                self._set_stop_progress("failed")
                raise
            self._set_stop_progress("sent_confirming")
            future_ready.wait(timeout=5.0)
            self._confirm_queue.put({
                "ticket": ticket,
                "future": holder.get("future"),
                "on_success": lambda: self.coordinator.force_recover_complete(
                    True, source=source),
                "on_failure": lambda: self.coordinator.force_recover_complete(
                    False, source=source),
                "label": "RECOVERY",
            })
            return DEFERRED

        try:
            future = self.arbiter.submit(
                recover_txn, priority=PRIORITY_EMERGENCY_STOP, command="AESTP",
                command_class="emergency_stop", source=source,
            )
        except Exception:
            # force_recover_begin() already moved the coordinator out of
            # HELD; if the recovery task couldn't even be enqueued, there is
            # no task left to resolve that transition — fail the recovery
            # explicitly instead of leaving it stuck.
            self.coordinator.force_recover_complete(False, source=source)
            self._set_stop_progress("failed")
            raise
        holder["future"] = future
        future_ready.set()
        return future

    # ── stop confirmation thread ─────────────────────────────────────────────

    def _confirm_free_slots(self) -> int:
        """STQ? at stop-confirmation priority (beats UI queries, so a
        polling storm cannot starve the confirmation)."""
        metadata = _command_metadata("STQ?")
        future = self._submit_wire(
            "STQ?", True, _validate_stq,
            source="stop_confirm", metadata=metadata,
            priority=PRIORITY_STOP_CONFIRM, lease=None,
        )
        line = future.result(timeout=10.0)
        return int(line[1])

    def _stop_confirm_loop(self):
        """Long-lived daemon thread.  Sleeps happen HERE, never on the comm
        thread, so UI queries interleave with the confirmation STQ? polls."""
        while True:
            item = self._confirm_queue.get()
            if item is None:
                return
            ticket = item["ticket"]
            future = item["future"]
            deadline = time.monotonic() + STOP_CONFIRM_TIMEOUT_S
            consecutive = 0
            errors = 0
            confirmed = False
            while time.monotonic() < deadline:
                try:
                    free = self._confirm_free_slots()
                except Exception:
                    errors += 1
                    if errors >= 5:
                        break
                    time.sleep(0.2)
                    continue
                consecutive = consecutive + 1 if free == 4 else 0
                if consecutive >= STOP_CONFIRM_COUNT:
                    confirmed = True
                    break
                time.sleep(0.1)

            if confirmed:
                item["on_success"]()
                self._set_stop_progress("confirmed")
                if future is not None and not future.done():
                    try:
                        future.set_result(True)
                    except Exception:
                        pass
            else:
                item["on_failure"]()
                self._set_stop_progress("failed")
                if future is not None and not future.done():
                    try:
                        future.set_exception(PM16CTimeoutError(
                            f"{item['label']}: could not confirm all motors "
                            f"stopped within {STOP_CONFIRM_TIMEOUT_S:.0f}s"
                        ))
                    except Exception:
                        pass
