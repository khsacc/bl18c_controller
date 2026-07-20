"""
Shared hardware-free test doubles for apps/exp_scheduler.

Centralises fake Stage / PACE5000 / LakeShore 335 / Rad-icon 2022 doubles so
individual test modules don't redefine ad hoc stand-ins with diverging
interfaces. Each fake exposes (a subset of) the same public surface that
apps/exp_scheduler/validator/pre_validator.py reads today, plus call
recording and fault injection so tests can assert exactly what was read and
simulate communication failures without touching real hardware.

See apps/exp_scheduler/REORGANISATION_PLAN.md Phase 0, item 9. This module
is deliberately independent of any device backend import so it stays usable
with no optional dependencies (pyserial, PyQt6, etc.) installed.
"""
from __future__ import annotations


class _FakeMotionLease:
    """Minimal stand-in for utils.stage.motion_coordinator.MotionLease —
    just enough identity for FakeStageController.coordinator.is_valid()
    (runner.py's run() checks this in its finally block before sending the
    final switch_to_loc())."""

    def __init__(self) -> None:
        self.valid = True


class _FakeCoordinator:
    """Minimal stand-in for utils.stage.motion_coordinator.MotionCoordinator
    — only the one method runner.py actually calls on ctrl.coordinator."""

    def is_valid(self, lease) -> bool:
        return lease is not None and getattr(lease, "valid", False)


class FakeStageController:
    """Stand-in for PM16CController / PM16CControllerSim.

    ``calls`` records every read/write as a ``(method_name, *args)`` tuple,
    in order — use it to assert a checker didn't re-read the same channel
    twice, or read channels it doesn't need. ``fail_on`` names methods that
    should raise on their next call, to simulate a communication fault.
    """

    def __init__(
        self,
        positions: dict[int, int] | None = None,
        is_moving: bool = False,
    ) -> None:
        self.positions: dict[int, int] = {ch: 0 for ch in range(1, 12)}
        if positions:
            self.positions.update(positions)
        self._is_moving = is_moving
        self.calls: list[tuple] = []
        # Either bare method names (fail every call to that method) or
        # (method_name, *args) tuples (fail only that exact call — e.g.
        # {("get_ch_pos", 5)} to simulate just Ch5 being unreadable while
        # every other channel still reads fine).
        self.fail_on: set = set()

        # ── runner.py-facing motion-ownership/move surface (Phase 9) ──────
        self.coordinator = _FakeCoordinator()
        self._active_lease: _FakeMotionLease | None = None
        # ch -> (False, message) returned by check_move_constraints(ch, ...)
        # — everything else reports (True, "").
        self.constraint_violations: dict[int, str] = {}

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on or (name, *args) in self.fail_on:
            raise RuntimeError(f"FakeStageController: injected fault in {name}{args!r}")

    def call_count(self, name: str, *args) -> int:
        if args:
            return sum(1 for c in self.calls if c == (name, *args))
        return sum(1 for c in self.calls if c[0] == name)

    # ── reads used by PreValidator ──────────────────────────────────
    def get_ch_pos(self, ch: int) -> int:
        self._record("get_ch_pos", ch)
        return self.positions[ch]

    def get_is_moving(self) -> bool:
        self._record("get_is_moving")
        return self._is_moving

    # ── writes (not exercised by PreValidator, which is read-only, but
    #    kept for Runner-facing tests) ───────────────────────────────
    def move_absolute(self, ch: int, position: int, speed: str = "H") -> None:
        self._record("move_absolute", ch, position, speed)
        self.positions[ch] = position

    def move_relative(self, ch: int, delta: int, speed: str = "H") -> None:
        self._record("move_relative", ch, delta, speed)
        self.positions[ch] += delta

    def set_speed(self, ch: int, speed: str) -> None:
        self._record("set_speed", ch, speed)

    # ── runner.py-facing surface (Phase 9: MOVE_CONSTRAINTS pre-check,
    #    motion lease, oscillation/follow moves) ────────────────────────
    def acquire_motion(self, owner: str, operation: str, *, timeout: float | None = None):
        self._record("acquire_motion", owner, operation)
        lease = _FakeMotionLease()
        self._active_lease = lease
        return lease

    def release_motion(self, lease) -> bool:
        self._record("release_motion")
        if lease is not None and lease is self._active_lease:
            lease.valid = False
            self._active_lease = None
            return True
        return False

    def switch_to_loc(self, *, motion=None) -> None:
        self._record("switch_to_loc")

    def check_move_constraints(self, ch: int, target_pos: int) -> tuple[bool, str]:
        self._record("check_move_constraints", ch, target_pos)
        if ch in self.constraint_violations:
            return False, self.constraint_violations[ch]
        return True, ""

    def move_ch_absolute(self, ch: int, target: int, *, motion=None) -> None:
        self._record("move_ch_absolute", ch, target)
        self.positions[ch] = target

    def move_ch_relative(self, ch: int, diff: int, *, motion=None) -> None:
        self._record("move_ch_relative", ch, diff)
        self.positions[ch] += diff

    def set_ch_speed(self, ch: int, speed: str = "M", stay_in_rem: bool = False, *, motion=None) -> None:
        self._record("set_ch_speed", ch, speed)

    def wait_until_stop(self, confirm_count: int = 4, stay_in_rem: bool = False, *,
                        motion=None, should_stop=None) -> None:
        self._record("wait_until_stop")

    def normal_stop(self, *, source: str | None = None) -> None:
        self._record("normal_stop")
        if self._active_lease is not None:
            self._active_lease.valid = False

    def emergency_stop(self, *, source: str | None = None) -> None:
        self._record("emergency_stop")
        if self._active_lease is not None:
            self._active_lease.valid = False

    def request_normal_stop(self, *, source: str | None = None):
        self.normal_stop(source=source)

    def request_emergency_stop(self, *, source: str | None = None):
        self.emergency_stop(source=source)


class FakePace5000:
    """Stand-in for apps/PACE5000/pace5000_backend.py::Pace5000Backend.

    Mirrors the subset of the public surface validator/ reads:
    ``connected``/``_is_connected`` (property), ``write()``, ``query()``,
    ``get_output_state()``, ``get_target_pressure()``,
    ``get_positive_source_pressure()``. ``unit`` defaults to ``"MPA"`` (the
    device's raw, uppercase SCPI response format) so
    validator/snapshots.py::collect_pace_snapshot's fail-closed unit
    normalization resolves it to "MPa" (factor 1.0) the same way callers
    used to implicitly assume — pass a different string (including
    lowercase/unknown values) to exercise the fail-closed path.
    """

    def __init__(
        self,
        connected: bool = True,
        output_state: str = "0",
        target_pressure: float | None = 0.0,
        positive_source_pressure: float | None = 10.0,
        unit: str = "MPA",
    ) -> None:
        self.connected = connected
        self._output_state = output_state
        self._target_pressure = target_pressure
        self._positive_source_pressure = positive_source_pressure
        self._unit = unit
        self.calls: list[tuple] = []
        self.fail_on: set = set()

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on or (name, *args) in self.fail_on:
            raise RuntimeError(f"FakePace5000: injected fault in {name}{args!r}")

    def call_count(self, name: str, *args) -> int:
        if args:
            return sum(1 for c in self.calls if c == (name, *args))
        return sum(1 for c in self.calls if c[0] == name)

    @property
    def _is_connected(self) -> bool:
        return self.connected

    def write(self, cmd: str) -> None:
        self._record("write", cmd)

    def query(self, cmd: str) -> str | None:
        self._record("query", cmd)
        if cmd == ":UNIT:PRES?":
            return self._unit
        return None

    def get_output_state(self) -> str | None:
        self._record("get_output_state")
        return self._output_state

    def get_target_pressure(self):
        self._record("get_target_pressure")
        return self._target_pressure

    def get_positive_source_pressure(self) -> float | None:
        self._record("get_positive_source_pressure")
        return self._positive_source_pressure


class FakeLakeshore:
    """Stand-in for apps/LakeShore335/lakeshore335_backend.py::LakeShore335Backend."""

    def __init__(
        self,
        connected: bool = True,
        setpoint: float = 300.0,
        heater_range: int = 1,
        data: list | None = None,
    ) -> None:
        self.connected = connected
        self._setpoint = setpoint
        self._heater_range = heater_range
        self._data = data if data is not None else []
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise RuntimeError(f"FakeLakeshore: injected fault in {name}{args!r}")

    def call_count(self, name: str, *args) -> int:
        if args:
            return sum(1 for c in self.calls if c == (name, *args))
        return sum(1 for c in self.calls if c[0] == name)

    @property
    def is_connected(self) -> bool:
        return self.connected

    def get_setpoint(self, output: int = 1) -> float:
        self._record("get_setpoint", output)
        return self._setpoint

    def get_heater_range(self, output: int = 1) -> int:
        self._record("get_heater_range", output)
        return self._heater_range

    def get_data(self) -> list:
        self._record("get_data")
        return self._data


class FakeRadicon:
    """Stand-in for apps/Rad_icon_2022/radicon_backend.py::RadiconBackend.

    Baseline `validator/pre_validator.py` only checks ``ctx.radicon is None``
    (presence), so this fake exposes just enough state to be non-None and
    call-recordable. Phase 6 (device-snapshot split) will need to extend
    this with real readiness/detector fields as those checks are written.
    """

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise RuntimeError(f"FakeRadicon: injected fault in {name}{args!r}")

    def call_count(self, name: str, *args) -> int:
        if args:
            return sum(1 for c in self.calls if c == (name, *args))
        return sum(1 for c in self.calls if c[0] == name)
