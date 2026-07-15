"""
Drop-in simulator for PM16CController.

Usage (standalone):
    from utils.stage.control_stage_sim import PM16CControllerSim
    sim = PM16CControllerSim(debug=True)
    sim.connect()

Usage (passed to any app that accepts a controller argument):
    window = Bl18cStageControlApp(controller=sim)

The simulator exposes the same public interface as PM16CController and applies
the same MOVE_CONSTRAINTS.  Positions are updated by a background thread at
~100 Hz so that callers polling get_ch_pos() / get_is_moving() see smooth
movement without any real hardware.
"""

import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime

try:
    from .control_stage import (
        MOVE_CONSTRAINTS, SOFT_LIMITS, MAX_MOVE_PULSES, _OPS,
        _command_metadata, STOP_CONFIRM_TIMEOUT_S,
    )
    from .stage_monitor import ChannelState
    from .motion_coordinator import MotionCoordinator, MotionLease
    from .errors import (
        MotionLeaseRequiredError,
        MotionNotAvailableError,
        MotionRevokedError,
        PM16CTimeoutError,
    )
except ImportError:
    from control_stage import (
        MOVE_CONSTRAINTS, SOFT_LIMITS, MAX_MOVE_PULSES, _OPS,
        _command_metadata, STOP_CONFIRM_TIMEOUT_S,
    )
    from stage_monitor import ChannelState
    from motion_coordinator import MotionCoordinator, MotionLease
    from errors import (
        MotionLeaseRequiredError,
        MotionNotAvailableError,
        MotionRevokedError,
        PM16CTimeoutError,
    )

# ---------------------------------------------------------------------------
# Simulation speed (pulses per 10 ms tick) by channel and speed setting
# ---------------------------------------------------------------------------
_SPEED_STEPS = {
    6: {'L':   50, 'M':   200, 'H':   800},
    7: {'L':  500, 'M':  2000, 'H':  8000},
    8: {'L':  500, 'M':  2000, 'H':  8000},
    9: {'L':  500, 'M':  2000, 'H':  8000},
}
_DEFAULT_STEPS = {'L': 100, 'M': 500, 'H': 2000}

# Default actual-speed register values (pps) simulated for each L/M/H setting
_SPEED_PPS_DEFAULT = {'L': 500, 'M': 2000, 'H': 10000}

# Initial positions that match BL-18C typical startup state
_INITIAL_POSITIONS = {
    1: 0, 2: 0, 3: 0, 4: 0, 5: 0,
    6: 12000, 7: 120000, 8: 0, 9: -40000,
    10: 0, 11: 0,
}


class PM16CControllerSim:
    """
    Simulated drop-in replacement for PM16CController.

    All move commands are executed by a background thread; the caller can call
    wait_until_stop() or poll get_is_moving() / get_ch_pos() exactly as it
    would with the real controller.
    """

    def __init__(self, ip=None, port=None, debug=False):
        self.ip = ip or '(sim)'
        self.port = port or 0
        self.debug = debug
        self.terminator = '\r\n'
        self.client = None  # no real socket

        self._positions = dict(_INITIAL_POSITIONS)
        self._targets   = dict(_INITIAL_POSITIONS)
        self._moving    = {ch: False for ch in _INITIAL_POSITIONS}
        self._speed     = {ch: 'M'   for ch in _INITIAL_POSITIONS}
        self._speed_pps = {ch: dict(_SPEED_PPS_DEFAULT) for ch in _INITIAL_POSITIONS}
        # Single state lock: every read-check-update sequence on positions/
        # targets/moving flags happens inside ONE critical section (see the
        # _locked helpers below).  Plain Lock, not RLock — public methods must
        # never be called re-entrantly from inside a locked section.
        self._state_lock = threading.Lock()
        self._running   = False
        self._thread    = None

        # Motion ownership: the SAME coordinator class as the real
        # controller, so lease semantics (generation, revocation, grace,
        # recovery) are byte-for-byte identical in simulation.
        self.controller_id = f"pm16c-sim:{id(self):x}"
        self.audit = None  # set to a PM16CAuditLogger/MemoryAudit by tests if needed
        self.coordinator = MotionCoordinator(self.controller_id, None)
        self._stop_progress = "idle"
        self._stop_progress_lock = threading.Lock()
        self.last_async_error = None

    # ── connection ──────────────────────────────────────────────────────────

    def connect(self):
        self._running = True
        self._thread = threading.Thread(target=self._sim_loop, daemon=True)
        self._thread.start()
        if self.debug:
            print("[Sim] Connected (simulated controller — no real hardware)")

    def disconnect(self):
        self._running = False
        if self.debug:
            print("[Sim] Disconnected")

    # ── simulation loop ─────────────────────────────────────────────────────

    def _sim_loop(self):
        while self._running:
            with self._state_lock:
                for ch, moving in self._moving.items():
                    if not moving:
                        continue
                    cur  = self._positions[ch]
                    tgt  = self._targets[ch]
                    step = _SPEED_STEPS.get(ch, _DEFAULT_STEPS)[self._speed[ch]]
                    if abs(cur - tgt) <= step:
                        self._positions[ch] = tgt
                        self._moving[ch]    = False
                    elif cur < tgt:
                        self._positions[ch] += step
                    else:
                        self._positions[ch] -= step
            time.sleep(0.01)

    # ── motion ownership API (parity with PM16CController) ──────────────────

    def acquire_motion(self, owner, operation, *, timeout=None) -> MotionLease:
        return self.coordinator.acquire(owner, operation, timeout=timeout)

    def release_motion(self, lease) -> bool:
        return self.coordinator.release(lease)

    @contextmanager
    def motion_session(self, owner, operation, *, timeout=None):
        lease = self.acquire_motion(owner, operation, timeout=timeout)
        try:
            yield lease
        finally:
            self.release_motion(lease)

    def is_motion_available(self) -> bool:
        return self.coordinator.is_available()

    def get_motion_holder(self):
        return self.coordinator.holder_info()

    def _set_stop_progress(self, state):
        with self._stop_progress_lock:
            self._stop_progress = state

    def get_stop_progress(self) -> str:
        with self._stop_progress_lock:
            return self._stop_progress

    # ── mode switches (no-op, but lease-gated like the real controller) ─────

    def switch_to_rem(self, *, motion=None):
        self.coordinator.validate(motion)

    def switch_to_loc(self, *, motion=None):
        self.coordinator.validate(motion)

    # ── send_cmd stub (no real TCP in sim; same lease policy) ───────────────

    def send_cmd(self, cmd, has_response=True, validate=None, *, motion=None):
        metadata = _command_metadata(cmd)
        command_class = metadata["command_class"]
        upper = cmd.strip().upper()
        if upper == "AESTP":
            return self.request_emergency_stop().result(
                timeout=STOP_CONFIRM_TIMEOUT_S + 10.0
            )
        if upper == "ASSTP":
            return self.request_normal_stop().result(
                timeout=STOP_CONFIRM_TIMEOUT_S + 10.0
            )
        if command_class in ("normal_stop", "emergency_stop", "query"):
            pass  # always allowed
        elif command_class in (
            "motion_absolute", "motion_relative", "motion_continuous",
            "motion_home_search", "motion_home_return", "position_preset",
            "speed_change", "mode_change",
        ):
            self.coordinator.validate(motion)
        else:  # configuration / unknown
            if not self.coordinator.is_available():
                raise MotionNotAvailableError(
                    "Unclassified/configuration commands are refused while "
                    "motion is owned or a stop/recovery is in progress.",
                    holder=self.coordinator.holder_info(),
                )
        if self.debug:
            print(f"[Sim] send_cmd ignored: {cmd}")
        return None

    # ── channel helpers ─────────────────────────────────────────────────────

    def print_invalid_ch(self):
        print("Invalid ch input.")

    def stringify_ch_numbers(self, ch):
        if 1 <= ch <= 9:
            return f"{ch}"
        elif ch == 10:
            return "A"
        elif ch == 11:
            return "B"
        else:
            self.print_invalid_ch()
            return None

    # ── status ──────────────────────────────────────────────────────────────

    def get_ch_pos(self, ch):
        with self._state_lock:
            if ch not in self._positions:
                return None
            return str(self._positions[ch])

    def get_is_moving(self):
        with self._state_lock:
            return any(self._moving.values())

    def is_all_motors_stopped(self):
        return not self.get_is_moving()

    def get_free_motor_slots(self) -> int:
        # The sim doesn't enforce the real controller's 4-concurrent-motor
        # cap, so this is only kept for API parity with PM16CController.
        with self._state_lock:
            moving = sum(1 for m in self._moving.values() if m)
        return max(0, 4 - moving)

    def get_ch_is_moving(self, ch) -> bool:
        with self._state_lock:
            return bool(self._moving.get(ch, False))

    def wait_ch_until_stop(self, ch, poll_interval=0.05, timeout=None):
        start = time.monotonic()
        while self.get_ch_is_moving(ch):
            if timeout is not None and time.monotonic() - start > timeout:
                raise TimeoutError(f"Ch{ch} did not stop within {timeout}s")
            time.sleep(poll_interval)

    def wait_channels_until_stop(self, channels, poll_interval=0.05, timeout=None):
        start = time.monotonic()
        remaining = set(channels)
        while remaining:
            remaining = {ch for ch in remaining if self.get_ch_is_moving(ch)}
            if not remaining:
                return
            if timeout is not None and time.monotonic() - start > timeout:
                raise TimeoutError(f"Channels {sorted(remaining)} did not stop within {timeout}s")
            time.sleep(poll_interval)

    def get_status(self):
        # Returns a string in the same format as the real STS? response:
        # R(L)abcd/PNNS/VVVV/HHJJKKLL/±pos1/±pos2/±pos3/±pos4
        with self._state_lock:
            ch_list = sorted(self._positions.keys())[:4]
            pnns = ''.join('P' if self._moving.get(ch) else 'S' for ch in ch_list)
            pos_str = '/'.join(f"{self._positions[ch]:+}" for ch in ch_list)
            return f"R1234/{pnns}/0000/00000000/{pos_str}"

    def get_ch_status(self, ch):
        # Returns a string in the same format as the real STSx? response:
        # R(L)aPVHH±pos
        with self._state_lock:
            pos = self._positions.get(ch, 0)
            pv = 'P' if self._moving.get(ch) else 'S'
            ch_str = self.stringify_ch_numbers(ch)
            return f"R{ch_str}{pv}000{pos:+08d}"

    def get_cached_ch_state(self, ch, max_age=None):
        """Simulation state is already in memory, so no polling is needed."""
        with self._state_lock:
            if ch not in self._positions:
                return None
            now = time.monotonic()
            return ChannelState(
                channel=ch,
                position=self._positions[ch],
                motion_state='P' if self._moving[ch] else 'S',
                mode='R',
                ls_hold='0',
                status_byte='00',
                observed_monotonic=now,
                observed_at=datetime.now().astimezone().isoformat(timespec='milliseconds'),
                source='simulator',
            )

    def get_cached_states(self, channels=None, max_age=None):
        selected = range(1, 12) if channels is None else channels
        return {
            ch: state
            for ch in selected
            if (state := self.get_cached_ch_state(ch, max_age=max_age)) is not None
        }

    def get_cached_is_moving(self):
        return self.get_is_moving()

    # ── locked internal helpers ─────────────────────────────────────────────
    # These read self._positions directly and MUST be called with
    # self._state_lock already held.  They exist so a move can do its
    # read-check-update sequence atomically without re-entering the public
    # getters (a plain Lock would deadlock).

    def _get_ch_pos_locked(self, ch):
        if ch not in self._positions:
            return None
        return str(self._positions[ch])

    def _check_move_constraints_locked(self, ch, target_pos):
        for rule in MOVE_CONSTRAINTS:
            if rule['target_ch'] != ch:
                continue
            if not _OPS[rule['target_op']](target_pos, rule['target_val']):
                continue
            for req in rule['required']:
                req_str = self._get_ch_pos_locked(req['ch'])
                if req_str is None:
                    return False, f"Cannot read Ch{req['ch']} position"
                if not _OPS[req['op']](int(req_str), req['val']):
                    return False, (
                        f"Move blocked: Ch{ch} → {target_pos:+} requires "
                        f"Ch{req['ch']} {req['op']} {req['val']:+}, "
                        f"but current position is {int(req_str):+}"
                    )
        return True, ""

    # ── constraints ─────────────────────────────────────────────────────────

    def check_move_constraints(self, ch, target_pos):
        with self._state_lock:
            return self._check_move_constraints_locked(ch, target_pos)

    # ── soft limits / max move (optional, disabled unless configured) ──────

    def check_soft_limits(self, ch, target_pos):
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
        cap = MAX_MOVE_PULSES.get(ch)
        if cap is None:
            return True, ""
        if abs(diff) > cap:
            return False, (
                f"Move blocked: Ch{ch} relative move {diff:+} exceeds the "
                f"configured max single move of {cap} pulses"
            )
        return True, ""

    # ── movement ────────────────────────────────────────────────────────────

    def move_ch_absolute(self, ch, target, *, motion=None):
        # Read-check-update runs inside ONE critical section so another
        # thread's move cannot land between the constraint check and the
        # target update (mirrors the real controller, which runs the whole
        # check-then-move sequence as one comm-thread transaction).
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return
        with self._state_lock:
            self.coordinator.validate(motion)
            if ch not in self._positions:
                return
            ok, msg = self._check_move_constraints_locked(ch, target)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_soft_limits(ch, target)
            if not ok:
                raise ValueError(msg)
            print(f"[Sim] CMD: ABS{ch_str}{target:+}")
            self._targets[ch] = target
            self._moving[ch]  = (self._positions[ch] != target)

    def move_ch_relative(self, ch, diff, *, motion=None):
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return
        with self._state_lock:
            self.coordinator.validate(motion)
            if ch not in self._positions:
                return
            cur = int(self._get_ch_pos_locked(ch) or 0)
            target = cur + diff
            ok, msg = self._check_move_constraints_locked(ch, target)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_max_move(ch, diff)
            if not ok:
                raise ValueError(msg)
            ok, msg = self.check_soft_limits(ch, target)
            if not ok:
                raise ValueError(msg)
            print(f"[Sim] CMD: REL{ch_str}{diff:+}")
            self._targets[ch] = target
            self._moving[ch]  = (self._positions[ch] != target)

    def move_ch_relative_unchecked(self, ch, diff, *, motion=None):
        # API parity with PM16CController's unchecked fast-path (used by the
        # Rad-icon rotation loop); the sim has no round-trip latency to avoid,
        # so this simply skips the constraint checks like its real counterpart.
        # Lease validation is memory-only, exactly like the real path.
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return
        with self._state_lock:
            if ch not in self._positions:
                return
            cur = int(self._get_ch_pos_locked(ch) or 0)
            print(f"[Sim] CMD: REL{ch_str}{diff:+} (unchecked)")
            target = cur + diff
            self._targets[ch] = target
            self._moving[ch]  = (self._positions[ch] != target)

    def wait_until_stop(self, confirm_count=4, stay_in_rem=False, *, motion=None):
        # Parity with PM16CController: the trailing LOC is a mode change and
        # needs a lease; a lease revoked while waiting skips the LOC.
        if not stay_in_rem and motion is None:
            raise MotionLeaseRequiredError(
                "wait_until_stop(stay_in_rem=False) switches to LOC and "
                "therefore requires motion=<lease>."
            )
        while self.get_is_moving():
            time.sleep(0.05)
        if stay_in_rem:
            return
        if not self.coordinator.is_valid(motion):
            return
        self.switch_to_loc(motion=motion)

    def set_ch_speed(self, ch, speed='M', stay_in_rem=False, *, motion=None):
        if speed in ('L', 'M', 'H'):
            self.coordinator.validate(motion)
            ch_str = self.stringify_ch_numbers(ch)
            print(f"[Sim] CMD: SPD{speed}{ch_str}")
            with self._state_lock:
                self._speed[ch] = speed

    # ── stops (no lease required; always accepted) ───────────────────────────

    def _halt_all_channels(self):
        with self._state_lock:
            for ch in self._moving:
                self._moving[ch]  = False
                self._targets[ch] = self._positions[ch]

    def _request_stop(self, *, emergency, source):
        ticket = self.coordinator.revoke_for_stop(
            source=source or "sim", emergency=emergency,
        )
        self._set_stop_progress("queued")
        future = Future()

        def worker():
            print(f"[Sim] CMD: {'AESTP' if emergency else 'ASSTP'}")
            self._halt_all_channels()
            self.coordinator.note_stop_sent(ticket)
            self._set_stop_progress("sent_confirming")
            deadline = time.monotonic() + STOP_CONFIRM_TIMEOUT_S
            while self.get_is_moving():
                if time.monotonic() > deadline:
                    self.coordinator.note_stop_confirm_failed(ticket)
                    self._set_stop_progress("failed")
                    future.set_exception(PM16CTimeoutError(
                        "Sim: could not confirm all motors stopped"
                    ))
                    return
                time.sleep(0.02)
            self.coordinator.note_stop_confirmed(ticket)
            self._set_stop_progress("confirmed")
            future.set_result(True)

        threading.Thread(target=worker, daemon=True).start()
        return future

    def request_normal_stop(self, *, source=None):
        return self._request_stop(emergency=False, source=source)

    def request_emergency_stop(self, *, source=None):
        return self._request_stop(emergency=True, source=source)

    def normal_stop(self, *, source=None):
        return self.request_normal_stop(source=source).result(
            timeout=STOP_CONFIRM_TIMEOUT_S + 10.0
        )

    def emergency_stop(self, *, source=None):
        return self.request_emergency_stop(source=source).result(
            timeout=STOP_CONFIRM_TIMEOUT_S + 10.0
        )

    def recover_motion(self, *, source):
        self.coordinator.force_recover_begin(source=source)
        future = Future()

        def worker():
            print("[Sim] CMD: AESTP (recovery)")
            self._halt_all_channels()
            while self.get_is_moving():
                time.sleep(0.02)
            self.coordinator.force_recover_complete(True, source=source)
            self._set_stop_progress("confirmed")
            future.set_result(True)

        threading.Thread(target=worker, daemon=True).start()
        return future

    def shutdown(self):
        self.disconnect()

    # ── backlash / limits / speed query (stubs) ─────────────────────────────

    def get_ch_speed_value(self, ch, level: str) -> "int | None":
        with self._state_lock:
            return self._speed_pps.get(ch, _SPEED_PPS_DEFAULT).get(level, _SPEED_PPS_DEFAULT.get(level, 500))

    def set_ch_speed_value(self, ch, level: str, pps: int, *, motion=None) -> None:
        self.coordinator.validate(motion)
        ch_str = self.stringify_ch_numbers(ch)
        if self.debug:
            print(f"[Sim] CMD: SPD{level}{ch_str}{pps}")
        with self._state_lock:
            self._speed_pps.setdefault(ch, dict(_SPEED_PPS_DEFAULT))[level] = pps

    def get_ch_lspd(self, ch) -> "int | None":
        return self.get_ch_speed_value(ch, "L")

    def set_ch_lspd(self, ch, pps: int, *, motion=None) -> None:
        self.set_ch_speed_value(ch, "L", pps, motion=motion)

    def get_ch_backlash(self, ch):
        return "+0000"

    def set_ch_backlash(self, ch, target, *, motion=None):
        self.coordinator.validate(motion)

    def get_ch_spped(self, ch):  # intentional typo: matches PM16CController
        s = self._speed.get(ch, 'M')
        return f"{s}SPD"

    def get_ch_speed(self, ch):
        """Alias for get_ch_spped (fixes the original name's typo); matches PM16CController."""
        return self.get_ch_spped(ch)

    def read_backward_limit(self, ch):
        return "-999999"

    def read_forward_limit(self, ch):
        return "+999999"
