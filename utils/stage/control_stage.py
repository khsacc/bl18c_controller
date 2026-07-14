import inspect
import logging
import os
import re
import socket
import threading
import time
from operator import ge, le, gt, lt, eq
from typing import Callable, Iterable, Optional

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


# ---------------------------------------------------------------------------
# Move constraints (inter-channel software limits)
#
# Each rule is evaluated before every absolute or relative move.
# If the intended target position of `target_ch` satisfies (`target_op`,
# `target_val`), then the *current* position of `required_ch` must satisfy
# (`required_op`, `required_val`) — otherwise the move is rejected.
#
# To add a new constraint, append a dict with the five keys shown below.
# ---------------------------------------------------------------------------
# Collision boundary between the Detector (Ch9) and Microscope arm (Ch8).
# Ch9 must be at or beyond this pulse position (i.e. ≤ value) before Ch8 can
# move into the beam path (positive direction), and vice versa.
# This constant is the single source of truth: MOVE_CONSTRAINTS below and all
# UI-level validation code import or reference it.
CH9_CH8_SAFE_BOUNDARY = -30000

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
    }
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


def _validate_stsx(ch_str: str) -> Callable[[str], "tuple[bool, str]"]:
    """STSx? reply: R(L)aPVHH±digits — 'a' must be the queried channel,
    state char (index 2) must be P/N/S."""
    def _validate(line: str):
        if len(line) < 7 or line[0] not in 'RL':
            return False, "missing R/L prefix"
        if line[1].upper() != ch_str.upper():
            return False, f"channel mismatch (expected Ch{ch_str}, got {line[1]!r})"
        if line[2] not in 'PNS':
            return False, f"invalid motion state {line[2]!r} (expected P/N/S)"
        return True, ""
    return _validate


def _validate_sts_full(line: str):
    """STS? reply: R(L)abcd/PNNS/VVVV/HHJJKKLL/±pos.../..."""
    if not line or line[0] not in 'RL':
        return False, "missing R/L prefix"
    if line.count('/') < 4:
        return False, "missing '/'-delimited STS? sections"
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
    mode = line[0]
    channel_hex = line[1]
    state = line[2]
    ls_hold_nibble = line[3]
    status_byte = line[4:6]
    position = line[6:]
    return mode, channel_hex, state, ls_hold_nibble, status_byte, position


class PM16CController:
    def __init__(self, ip, port, debug=False):
        self.ip = ip
        self.port = port
        self.debug = debug
        self.terminator = '\r\n'
        self.client = None
        # RLock (not Lock): several public methods below hold this lock for
        # their entire compound REM/command/LOC sequence and call send_cmd()
        # (which also acquires it) internally — a plain Lock would deadlock
        # on that reentrant acquisition from the same thread.
        self._lock = threading.RLock()
        # Bytes received but not yet consumed as a full \r\n-terminated line.
        # Kept across send_cmd() calls so a second line arriving in the same
        # TCP segment as the first (e.g. a real reply following an
        # unsolicited STOPx notification) is never silently discarded.
        self._recv_buffer = b""

    def connect(self):
        """ Connect the controller and delete remaining buffers if exist """
        print(f"Attempting to connect, {self.ip}:{self.port}...")
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client.settimeout(2.0)
        self.client.connect((self.ip, self.port))

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

        # Clear any stale LAN-SRQ arm flags left over from a previous client.
        # STOPx filtering in send_cmd()/_read_line() stays in place regardless
        # of this flag's state, since another client/interface on the unit
        # could re-arm it at any time.
        self.send_cmd("LN_SRQG0", has_response=False)

    def disconnect(self):
        """ Disconnect from the contrlller """
        if self.client:
            self.client.close()
            self.client = None
            self._recv_buffer = b""
            print("Disconnected.")

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

    def send_cmd(self, cmd, has_response=True, validate: Optional[Callable[[str], "tuple[bool, str]"]] = None):
        """
        Send a command to the controller.
        Acquires a lock so concurrent threads don't interleave commands/responses.

        `validate`, if given, is called with the response line and must
        return (True, "") to accept it or (False, reason) to reject it —
        rejection raises PM16CProtocolError rather than the bogus line being
        handed back to the caller.
        """
        with self._lock:
            if self.client is None:
                raise ConnectionError(
                    "PM16C controller is not connected (client is None) — call connect() first."
                )
            source = _infer_source()
            full_cmd = f"{cmd}{self.terminator}"
            logger.debug("TX source=%s command=%s", source, cmd)
            self.client.sendall(full_cmd.encode('ascii'))

            if not has_response:
                if self.debug: print(f"Sending: {cmd:<10} without waiting for the response")
                return None

            # Read lines until we get one that isn't an unsolicited async
            # notification (e.g. "STOP4" pushed the instant channel 4 stops,
            # firmware V1.42+) — such a line is never the reply to the
            # command we just sent, and must not be consumed as one.
            while True:
                try:
                    line = self._read_line()
                except socket.timeout:
                    raise PM16CTimeoutError(f"'{cmd}' timed out waiting for a response")
                logger.debug("RX raw=%s", line)
                if _STOPX_RE.match(line):
                    continue
                break

            if self.debug: print(f"Command: {cmd:<10} -> Response: {line}")

            if validate is not None:
                ok, reason = validate(line)
                if not ok:
                    raise PM16CProtocolError(f"Unexpected response to {cmd!r}: {line!r} ({reason})")

            return line

    def switch_to_rem(self):
        self.send_cmd("REM", has_response=False)

    def switch_to_loc(self):
        self.send_cmd("LOC", has_response=False)

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

    def wait_until_stop(self, confirm_count=4, stay_in_rem=False):
        """ check the current status and wait until the motors are stopped """
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
        if self.debug: print("--- Operation completed ---\n--- Switch to LOC ---")
        self.switch_to_loc()

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

    def check_move_constraints(self, ch, target_pos):
        """Check MOVE_CONSTRAINTS before a move.

        Returns (True, "") when safe.
        Returns (False, reason) when a constraint would be violated.
        Each rule's 'required' list is checked in order; all conditions must hold.
        """
        for rule in MOVE_CONSTRAINTS:
            if rule['target_ch'] != ch:
                continue
            if not _OPS[rule['target_op']](target_pos, rule['target_val']):
                continue
            for req in rule['required']:
                req_str = self.get_ch_pos(req['ch'])
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

    def move_ch_relative(self, ch, diff):
        # Held for the whole check-then-move sequence so another thread's
        # command can't land between the constraint check and the actual
        # move (e.g. switching back to LOC, or moving the channel this
        # move's constraint check depends on).
        with self._lock:
            current_str = self.get_ch_pos(ch)
            if current_str is None:
                raise ValueError(
                    f"Ch{ch} の現在位置を取得できませんでした。\n"
                    "通信エラーの可能性があるため、衝突防止のため相対値移動をブロックしました。"
                )
            target = int(current_str) + diff
            ok, msg = self.check_move_constraints(ch, target)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_max_move(ch, diff)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_soft_limits(ch, target)
            if not ok:
                raise ValueError(msg)
            self.switch_to_rem()
            ch_str = self.stringify_ch_numbers(ch)
            if ch_str is None:
                return None
            logger.info(
                "MOVE source=%s ch=%s current=%s target=%+d",
                _infer_source(), ch, current_str, target,
            )
            self.send_cmd(f"REL{ch_str}{diff:+}", has_response=False)

    def move_ch_absolute(self, ch, target):
        with self._lock:
            ok, msg = self.check_move_constraints(ch, target)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_soft_limits(ch, target)
            if not ok:
                raise ValueError(msg)
            current_str = self.get_ch_pos(ch)
            if current_str is not None:
                ok, msg = self.check_max_move(ch, target - int(current_str))
                if not ok:
                    raise ValueError(msg)
            self.switch_to_rem()
            ch_str = self.stringify_ch_numbers(ch)
            if ch_str is None:
                return None
            logger.info(
                "MOVE source=%s ch=%s current=%s target=%+d",
                _infer_source(), ch, current_str, target,
            )
            self.send_cmd(f"ABS{ch_str}{target:+}", has_response=False)

    def move_ch_relative_unchecked(self, ch, diff):
        """Fire a relative move with no position round-trip and no
        constraint check — assumes the caller is already in REM mode and has
        already validated the move.

        For timing-sensitive loops only (e.g. the Rad-icon rotation scan's
        per-step REL, fired immediately after starting an exposure so both
        finish at roughly the same time): move_ch_relative()'s extra STSx?
        round-trip and constraint check would introduce latency such a loop
        is specifically designed to avoid.
        """
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        logger.info("MOVE source=%s ch=%s target=%+d (unchecked)", _infer_source(), ch, diff)
        self.send_cmd(f"REL{ch_str}{diff:+}", has_response=False)

    def get_ch_pos(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        line = self.send_cmd(f"STS{ch_str}?", validate=_validate_stsx(ch_str))
        _, _, _, _, _, position = _parse_stsx_reply(line)
        return position

    def get_ch_status(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is not None:
            return self.send_cmd(f"STS{ch_str}?", validate=_validate_stsx(ch_str))

    def get_status(self):
        return self.send_cmd("STS?", validate=_validate_sts_full)

    def get_is_moving(self):
        return not self.is_all_motors_stopped()

    def get_ch_backlash(self, ch):
        return self.send_cmd(f"B{ch}?")

    def set_ch_backlash(self, ch, target):
        with self._lock:
            self.switch_to_rem()
            ch_str = self.stringify_ch_numbers(ch)
            if ch_str is not None:
                self.send_cmd(f"B{ch_str}{target:+04}", has_response=False)
            self.switch_to_loc()

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

    def set_ch_speed_value(self, ch, level: str, pps: int) -> None:
        """Set the actual pps register value for channel ch's L/M/H speed setting."""
        if level not in ("L", "M", "H"):
            return
        with self._lock:
            ch_str = self.stringify_ch_numbers(ch)
            if ch_str is None:
                return
            self.switch_to_rem()
            self.send_cmd(f"SPD{level}{ch_str}{pps}", has_response=False)
            self.switch_to_loc()

    def get_ch_lspd(self, ch) -> "int | None":
        """Read the LSPD register value for channel ch.  Returns pps as int, or None on error."""
        return self.get_ch_speed_value(ch, "L")

    def set_ch_lspd(self, ch, pps: int) -> None:
        """Set the LSPD register for channel ch to pps [pulses per second]."""
        self.set_ch_speed_value(ch, "L", pps)

    def set_ch_speed(self, ch, speed="M", stay_in_rem=False):
        if speed not in ("L", "M", "H"):
            return
        with self._lock:
            ch_str = self.stringify_ch_numbers(ch)
            if ch_str is None:
                return
            self.switch_to_rem()
            self.send_cmd(f"SPD{speed}{ch_str}", has_response=False)
            if not stay_in_rem:
                self.switch_to_loc()

    def read_backward_limit(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"BL?{ch_str}")

    def read_forward_limit(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"FL?{ch_str}")

    def normal_stop(self):
        self.send_cmd("ASSTP", has_response=False)
        self.switch_to_loc()

    def emergency_stop(self):
        self.send_cmd("AESTP", has_response=False)
        self.switch_to_loc()
