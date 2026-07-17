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
        self.fail_on: set[str] = set()

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise RuntimeError(f"FakeStageController: injected fault in {name}{args!r}")

    def call_count(self, name: str) -> int:
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


class FakePace5000:
    """Stand-in for apps/PACE5000/pace5000_backend.py::Pace5000Backend.

    Mirrors the subset of the public surface validator/pre_validator.py
    reads: ``_is_connected`` (property), ``write()``, ``get_output_state()``,
    ``get_target_pressure()``, ``get_positive_source_pressure()``.
    """

    def __init__(
        self,
        connected: bool = True,
        output_state: str = "0",
        target_pressure: float | None = 0.0,
        positive_source_pressure: float | None = 10.0,
    ) -> None:
        self.connected = connected
        self._output_state = output_state
        self._target_pressure = target_pressure
        self._positive_source_pressure = positive_source_pressure
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise RuntimeError(f"FakePace5000: injected fault in {name}{args!r}")

    def call_count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    @property
    def _is_connected(self) -> bool:
        return self.connected

    def write(self, cmd: str) -> None:
        self._record("write", cmd)

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

    def call_count(self, name: str) -> int:
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

    def call_count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)
