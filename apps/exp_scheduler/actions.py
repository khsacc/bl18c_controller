"""
Experimental Scheduler — Action definitions.

Each Action subclass represents one schedulable operation.
Primitive actions map 1:1 to a single device command.
Compound actions (MicroscopeOutFpdIn*, FollowSampleAction) decompose at runtime.
ForLoopAction is a control-flow node produced by the DSL parser.

JSON round-trip:
  action.to_dict() -> dict  (used by Sequence.save())
  action_from_dict(d)       (used by Sequence.load())

DSL round-trip:
  action.to_dsl() -> str    (used by DslEditor when converting Visual → Script)
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _dsl_str(value: str) -> str:
    """Render *value* as a Python string literal safe to embed in to_dsl()
    output. Windows paths (backslashes), embedded quotes, and control
    characters all need real escaping — plain f-string interpolation like
    f'path="{value}"' silently reinterprets "\\t"/"\\n" as control
    characters and raises SyntaxError on "\\U"/"\\u"/"\\N"/"\\x" sequences,
    which are common in Windows paths (e.g. C:\\Users\\...). repr() handles
    every case correctly and round-trips through ast.parse() unchanged.
    """
    return repr(value)


def _num(value) -> str:
    """Compact number for labels (30.0 -> "30"); non-numbers pass through."""
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def _overrides_suffix(overrides: list[str]) -> str:
    """" (a, b)" for per-step overrides of global settings, "" when none."""
    return f" ({', '.join(overrides)})" if overrides else ""


# ── Base ─────────────────────────────────────────────────────────────

class Action:
    def describe(self) -> str:
        raise NotImplementedError

    def to_dict(self) -> dict:
        raise NotImplementedError

    def to_dsl(self) -> str:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, d: dict) -> "Action":
        raise NotImplementedError


# ── General ──────────────────────────────────────────────────────────

@dataclass
class WaitAction(Action):
    TYPE = "wait"
    duration_s: float

    def describe(self) -> str:
        if self.duration_s >= 60 and self.duration_s % 60 == 0:
            return f"Wait {int(self.duration_s // 60)} min"
        if self.duration_s >= 60:
            return f"Wait {self.duration_s / 60:.1f} min"
        return f"Wait {self.duration_s:.0f} s"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "duration_s": self.duration_s}

    def to_dsl(self) -> str:
        if self.duration_s >= 60 and self.duration_s % 60 == 0:
            return f'wait(duration={int(self.duration_s // 60)}, unit="min")'
        return f'wait(duration={self.duration_s}, unit="s")'

    @classmethod
    def from_dict(cls, d: dict) -> "WaitAction":
        return cls(duration_s=float(d["duration_s"]))


@dataclass
class LogAction(Action):
    TYPE = "log_message"
    message: str

    def describe(self) -> str:
        return f"Log: {self.message}"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "message": self.message}

    def to_dsl(self) -> str:
        return f"log_message(message={_dsl_str(self.message)})"

    @classmethod
    def from_dict(cls, d: dict) -> "LogAction":
        return cls(message=d["message"])


# ── Stage (Primitive) ────────────────────────────────────────────────

@dataclass
class StageAction(Action):
    """
    Covers move_absolute, move_relative, set_speed, normal_stop, emergency_stop.
    `operation` is used as the JSON "type" key.

    For move_absolute / move_relative: value = position / delta (pulses).
    For set_speed: value = 0 (unused); speed carries "H"/"M"/"L".
    For normal_stop / emergency_stop: ch = 0, value = 0 (both unused).

    `value` is float|str to support loop-variable references (str = variable name).
    """
    OPERATIONS = {
        "move_absolute", "move_relative", "set_speed",
        "normal_stop", "emergency_stop",
    }

    operation: str
    ch: int = 0
    value: float | str = 0
    speed: str | None = None

    def describe(self) -> str:
        if self.operation == "move_absolute":
            suffix = f"  speed={self.speed}" if self.speed else ""
            return f"Stage Ch{self.ch} → {self.value} (abs){suffix}"
        if self.operation == "move_relative":
            suffix = f"  speed={self.speed}" if self.speed else ""
            return f"Stage Ch{self.ch} Δ{self.value}{suffix}"
        if self.operation == "set_speed":
            return f"Stage Ch{self.ch} speed={self.speed}"
        if self.operation == "normal_stop":
            return "Stage: normal stop (decelerate)"
        return "Stage: emergency stop"

    def to_dict(self) -> dict:
        d: dict = {"type": self.operation, "ch": self.ch, "speed": self.speed}
        if self.operation in {"move_absolute", "move_relative"}:
            if isinstance(self.value, str):
                d["value_var"] = self.value
            else:
                d["value"] = self.value
        return d

    def to_dsl(self) -> str:
        val = self.value if isinstance(self.value, str) else repr(self.value)
        if self.operation == "move_absolute":
            line = f"move_absolute(ch={self.ch}, position={val})"
        elif self.operation == "move_relative":
            line = f"move_relative(ch={self.ch}, delta={val})"
        elif self.operation == "set_speed":
            return f"set_speed(ch={self.ch}, speed={_dsl_str(self.speed)})"
        elif self.operation == "normal_stop":
            return "normal_stop()"
        else:
            return "emergency_stop()"
        if self.speed:
            return f"set_speed(ch={self.ch}, speed={_dsl_str(self.speed)})\n{line}"
        return line

    @classmethod
    def from_dict(cls, d: dict) -> "StageAction":
        value: float | str = 0
        if "value_var" in d:
            value = d["value_var"]
        elif "value" in d:
            value = d["value"]
        return cls(
            operation=d["type"],
            ch=d.get("ch", 0),
            value=value,
            speed=d.get("speed"),
        )


# ── Stage (Compound) ─────────────────────────────────────────────────

@dataclass
class MicroscopeOutFpdInAction(Action):
    """Ch8 OUT then Ch9 IN (XRD measurement mode). to_steps() implemented in Task 10."""
    TYPE = "microscope_out_and_fpd_in"
    microscope_out_pos: int | None = None   # None → global Positions
    fpd_in_pos: int | None = None           # None → global Positions
    speed: str = "H"

    def describe(self) -> str:
        overrides = []
        if self.microscope_out_pos is not None:
            overrides.append(f"Ch8={self.microscope_out_pos}")
        if self.fpd_in_pos is not None:
            overrides.append(f"Ch9={self.fpd_in_pos}")
        if self.speed != "H":
            overrides.append(f"speed={self.speed}")
        return "Microscope OUT → FPD IN" + _overrides_suffix(overrides)

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "microscope_out_pos": self.microscope_out_pos,
            "fpd_in_pos": self.fpd_in_pos,
            "speed": self.speed,
        }

    def to_dsl(self) -> str:
        parts = []
        if self.microscope_out_pos is not None:
            parts.append(f"microscope_out_pos={self.microscope_out_pos}")
        if self.fpd_in_pos is not None:
            parts.append(f"fpd_in_pos={self.fpd_in_pos}")
        if self.speed != "H":
            parts.append(f"speed={_dsl_str(self.speed)}")
        return f"microscope_out_and_fpd_in({', '.join(parts)})"

    def to_steps(self, stage_settings: dict) -> list["StageAction"]:
        # Ch8 OUT first (satisfies MOVE_CONSTRAINTS: Ch8 ≤ 0 before Ch9 can move IN)
        mic_out = (
            self.microscope_out_pos
            if self.microscope_out_pos is not None
            else int(stage_settings["ch8_out"])
        )
        fpd_in = (
            self.fpd_in_pos
            if self.fpd_in_pos is not None
            else int(stage_settings["det_in"])
        )
        return [
            StageAction(operation="move_absolute", ch=8, value=mic_out, speed=self.speed),
            StageAction(operation="move_absolute", ch=9, value=fpd_in, speed=self.speed),
        ]

    @classmethod
    def from_dict(cls, d: dict) -> "MicroscopeOutFpdInAction":
        return cls(
            microscope_out_pos=d.get("microscope_out_pos"),
            fpd_in_pos=d.get("fpd_in_pos"),
            speed=d.get("speed", "H"),
        )


@dataclass
class FpdOutMicroscopeInAction(Action):
    """Ch9 OUT then Ch8 IN (microscopy mode). to_steps() implemented in Task 10."""
    TYPE = "fpd_out_and_microscope_in"
    fpd_out_pos: int | None = None          # None → global Positions
    microscope_in_pos: int | None = None    # None → global Positions
    speed: str = "H"

    def describe(self) -> str:
        overrides = []
        if self.fpd_out_pos is not None:
            overrides.append(f"Ch9={self.fpd_out_pos}")
        if self.microscope_in_pos is not None:
            overrides.append(f"Ch8={self.microscope_in_pos}")
        if self.speed != "H":
            overrides.append(f"speed={self.speed}")
        return "FPD OUT → Microscope IN" + _overrides_suffix(overrides)

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "fpd_out_pos": self.fpd_out_pos,
            "microscope_in_pos": self.microscope_in_pos,
            "speed": self.speed,
        }

    def to_dsl(self) -> str:
        parts = []
        if self.fpd_out_pos is not None:
            parts.append(f"fpd_out_pos={self.fpd_out_pos}")
        if self.microscope_in_pos is not None:
            parts.append(f"microscope_in_pos={self.microscope_in_pos}")
        if self.speed != "H":
            parts.append(f"speed={_dsl_str(self.speed)}")
        return f"fpd_out_and_microscope_in({', '.join(parts)})"

    def to_steps(self, stage_settings: dict) -> list["StageAction"]:
        # Ch9 OUT first (satisfies MOVE_CONSTRAINTS: Ch9 ≤ -30000 before Ch8 can move IN)
        fpd_out = (
            self.fpd_out_pos
            if self.fpd_out_pos is not None
            else int(stage_settings["det_out"])
        )
        mic_in = (
            self.microscope_in_pos
            if self.microscope_in_pos is not None
            else int(stage_settings["ch8_in"])
        )
        return [
            StageAction(operation="move_absolute", ch=9, value=fpd_out, speed=self.speed),
            StageAction(operation="move_absolute", ch=8, value=mic_in, speed=self.speed),
        ]

    @classmethod
    def from_dict(cls, d: dict) -> "FpdOutMicroscopeInAction":
        return cls(
            fpd_out_pos=d.get("fpd_out_pos"),
            microscope_in_pos=d.get("microscope_in_pos"),
            speed=d.get("speed", "H"),
        )


# ── PACE5000 ─────────────────────────────────────────────────────────

@dataclass
class SetPressureAction(Action):
    """
    `pressure` is float | str — str means a loop-variable name (e.g. "p").
    rate and rate_unit are required; rate=0 sends an instantaneous setpoint change.
    """
    TYPE = "set_pressure"
    pressure: float | str
    unit: str
    rate: float
    rate_unit: str

    def describe(self) -> str:
        p = f"{self.pressure}" if isinstance(self.pressure, str) else f"{self.pressure}"
        return f"Set pressure {p} {self.unit} at {self.rate} {self.rate_unit}"

    def to_dict(self) -> dict:
        d: dict = {
            "type": self.TYPE,
            "unit": self.unit,
            "rate": self.rate,
            "rate_unit": self.rate_unit,
        }
        if isinstance(self.pressure, str):
            d["pressure_var"] = self.pressure
        else:
            d["pressure"] = self.pressure
        return d

    def to_dsl(self) -> str:
        p_expr = self.pressure if isinstance(self.pressure, str) else repr(self.pressure)
        return (
            f"set_pressure(pressure={p_expr}, unit={_dsl_str(self.unit)}, "
            f"rate={self.rate}, rate_unit={_dsl_str(self.rate_unit)})"
        )

    @classmethod
    def from_dict(cls, d: dict) -> "SetPressureAction":
        pressure: float | str = d["pressure_var"] if "pressure_var" in d else float(d["pressure"])
        return cls(
            pressure=pressure,
            unit=d["unit"],
            rate=float(d["rate"]),
            rate_unit=d["rate_unit"],
        )


@dataclass
class WaitPressureAction(Action):
    TYPE = "wait_pressure"
    tol: float
    unit: str

    def describe(self) -> str:
        return f"Wait pressure ±{self.tol} {self.unit}"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "tol": self.tol, "unit": self.unit}

    def to_dsl(self) -> str:
        return f"wait_pressure(tol={self.tol}, unit={_dsl_str(self.unit)})"

    @classmethod
    def from_dict(cls, d: dict) -> "WaitPressureAction":
        return cls(tol=float(d["tol"]), unit=d["unit"])


@dataclass
class SetAndWaitPressureAction(Action):
    """
    Combines SetPressureAction + WaitPressureAction — the common case of
    setting a pressure and immediately blocking until it's reached.
    Decomposes via to_set_action()/to_wait_action(); runner.py and
    validator/pre_validator.py reuse the SetPressureAction/WaitPressureAction
    logic against those instead of re-implementing it.
    `pressure` is float | str — str means a loop-variable name (e.g. "p").
    """
    TYPE = "set_and_wait_pressure"
    pressure: float | str
    unit: str
    rate: float
    rate_unit: str
    tol: float

    def to_set_action(self) -> "SetPressureAction":
        return SetPressureAction(
            pressure=self.pressure, unit=self.unit,
            rate=self.rate, rate_unit=self.rate_unit,
        )

    def to_wait_action(self) -> "WaitPressureAction":
        return WaitPressureAction(tol=self.tol, unit=self.unit)

    def describe(self) -> str:
        p = f"{self.pressure}"
        return f"Set pressure {p} {self.unit} at {self.rate} {self.rate_unit} and wait (±{self.tol} {self.unit})"

    def to_dict(self) -> dict:
        d: dict = {
            "type": self.TYPE,
            "unit": self.unit,
            "rate": self.rate,
            "rate_unit": self.rate_unit,
            "tol": self.tol,
        }
        if isinstance(self.pressure, str):
            d["pressure_var"] = self.pressure
        else:
            d["pressure"] = self.pressure
        return d

    def to_dsl(self) -> str:
        p_expr = self.pressure if isinstance(self.pressure, str) else repr(self.pressure)
        return (
            f"set_and_wait_pressure(pressure={p_expr}, unit={_dsl_str(self.unit)}, "
            f"rate={self.rate}, rate_unit={_dsl_str(self.rate_unit)}, tol={self.tol})"
        )

    @classmethod
    def from_dict(cls, d: dict) -> "SetAndWaitPressureAction":
        pressure: float | str = d["pressure_var"] if "pressure_var" in d else float(d["pressure"])
        return cls(
            pressure=pressure,
            unit=d["unit"],
            rate=float(d["rate"]),
            rate_unit=d["rate_unit"],
            tol=float(d["tol"]),
        )


@dataclass
class SetControlModeAction(Action):
    TYPE = "set_control_mode"
    enabled: bool

    def describe(self) -> str:
        return f"Pressure control {'ON' if self.enabled else 'OFF'}"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "enabled": self.enabled}

    def to_dsl(self) -> str:
        return f"set_control_mode(enabled={self.enabled})"

    @classmethod
    def from_dict(cls, d: dict) -> "SetControlModeAction":
        return cls(enabled=bool(d["enabled"]))


# ── LakeShore 335 ────────────────────────────────────────────────────

@dataclass
class SetTemperatureAction(Action):
    """
    `value_k` is float | str to support loop-variable references.
    ramp_rate (K/min) is required; rate=0 sends an instantaneous setpoint change.
    """
    TYPE = "set_temperature"
    value_k: float | str
    ramp_rate: float

    def describe(self) -> str:
        v = f"{self.value_k}" if isinstance(self.value_k, str) else f"{self.value_k} K"
        return f"Set temperature {v} ramp {self.ramp_rate} K/min"

    def to_dict(self) -> dict:
        d: dict = {"type": self.TYPE, "ramp_rate": self.ramp_rate}
        if isinstance(self.value_k, str):
            d["value_k_var"] = self.value_k
        else:
            d["value_k"] = self.value_k
        return d

    def to_dsl(self) -> str:
        v_expr = self.value_k if isinstance(self.value_k, str) else repr(self.value_k)
        return f'set_temperature(value={v_expr}, unit="K", ramp_rate={self.ramp_rate})'

    @classmethod
    def from_dict(cls, d: dict) -> "SetTemperatureAction":
        value_k: float | str = d["value_k_var"] if "value_k_var" in d else float(d["value_k"])
        return cls(value_k=value_k, ramp_rate=float(d["ramp_rate"]))


@dataclass
class WaitTemperatureAction(Action):
    TYPE = "wait_temperature"
    tol_k: float
    channel: str = "B"

    def describe(self) -> str:
        return f"Wait temperature T_{self.channel} ±{self.tol_k} K"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "tol_k": self.tol_k, "channel": self.channel}

    def to_dsl(self) -> str:
        return f'wait_temperature(tol={self.tol_k}, unit="K", channel="{self.channel}")'

    @classmethod
    def from_dict(cls, d: dict) -> "WaitTemperatureAction":
        return cls(tol_k=float(d["tol_k"]), channel=d.get("channel", "B"))


@dataclass
class SetAndWaitTemperatureAction(Action):
    TYPE = "set_and_wait_temperature"
    value_k: float | str
    ramp_rate: float
    tol_k: float
    channel: str = "B"

    def to_set_action(self) -> SetTemperatureAction:
        return SetTemperatureAction(value_k=self.value_k, ramp_rate=self.ramp_rate)

    def to_wait_action(self) -> WaitTemperatureAction:
        return WaitTemperatureAction(tol_k=self.tol_k, channel=self.channel)

    def describe(self) -> str:
        return f"{self.to_set_action().describe()} and wait T_{self.channel} ±{self.tol_k} K"

    def to_dict(self) -> dict:
        d = self.to_set_action().to_dict()
        d.update(type=self.TYPE, tol_k=self.tol_k, channel=self.channel)
        return d

    def to_dsl(self) -> str:
        value = self.value_k if isinstance(self.value_k, str) else repr(self.value_k)
        return (f'set_and_wait_temperature(value={value}, unit="K", '
                f'ramp_rate={self.ramp_rate}, tol={self.tol_k}, channel="{self.channel}")')

    @classmethod
    def from_dict(cls, d: dict) -> "SetAndWaitTemperatureAction":
        value = d["value_k_var"] if "value_k_var" in d else float(d["value_k"])
        return cls(value_k=value, ramp_rate=float(d["ramp_rate"]),
                   tol_k=float(d["tol_k"]), channel=d.get("channel", "B"))


@dataclass
class SetHeaterAction(Action):
    TYPE = "set_heater"
    range_index: int  # 0=Off 1=Low 2=Medium 3=High

    _RANGE_NAMES = {0: "Off", 1: "Low", 2: "Medium", 3: "High"}

    def describe(self) -> str:
        name = self._RANGE_NAMES.get(self.range_index, str(self.range_index))
        return f"Set heater {name}"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "range_index": self.range_index}

    def to_dsl(self) -> str:
        return f"set_heater(range_index={self.range_index})"

    @classmethod
    def from_dict(cls, d: dict) -> "SetHeaterAction":
        return cls(range_index=int(d["range_index"]))


@dataclass
class AllHeatersOffAction(Action):
    TYPE = "all_heaters_off"

    def describe(self) -> str:
        return "All heaters OFF"

    def to_dict(self) -> dict:
        return {"type": self.TYPE}

    def to_dsl(self) -> str:
        return "all_heaters_off()"

    @classmethod
    def from_dict(cls, d: dict) -> "AllHeatersOffAction":
        return cls()


# ── Rad-icon 2022 ────────────────────────────────────────────────────

@dataclass
class TakeXrdAction(Action):
    TYPE = "take_xrd"
    # None = inherit from GlobalXrdSettings
    exposure_ms: int | None = None
    save: bool = True
    prefix: str | None = None
    # Per-step overrides (None = inherit from GlobalXrdSettings)
    dark_file: str | None = None
    dark_enabled: bool | None = None
    defect_file: str | None = None
    defect_enabled: bool | None = None
    defect_kernel: int | None = None
    flip_v: bool | None = None
    flip_h: bool | None = None
    # Ch11 oscillation during exposure (None = inherit from GlobalXrdSettings)
    oscillate: bool | None = None
    osc_pos_a_deg: float | None = None   # degrees; converted to pulses at runtime
    osc_pos_b_deg: float | None = None
    osc_dwell_ms: int | None = None      # dwell at each end before reversing
    osc_speed: str | None = None         # "H"/"M"/"L"

    def describe(self) -> str:
        # Global XRD settings are implied; only per-step overrides are shown.
        text = "XRD"
        if self.exposure_ms is not None:
            text += f" {self.exposure_ms} ms"
        if self.save and self.prefix is not None:
            text += f", file prefix: {self.prefix}"
        if self.oscillate:
            text += ", +oscillation"
        return text

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "exposure_ms": self.exposure_ms,
            "save": self.save,
            "prefix": self.prefix,
            "dark_file": self.dark_file,
            "dark_enabled": self.dark_enabled,
            "defect_file": self.defect_file,
            "defect_enabled": self.defect_enabled,
            "defect_kernel": self.defect_kernel,
            "flip_v": self.flip_v,
            "flip_h": self.flip_h,
            "oscillate": self.oscillate,
            "osc_pos_a_deg": self.osc_pos_a_deg,
            "osc_pos_b_deg": self.osc_pos_b_deg,
            "osc_dwell_ms": self.osc_dwell_ms,
            "osc_speed": self.osc_speed,
        }

    def to_dsl(self) -> str:
        parts = []
        if self.exposure_ms is not None:
            parts.append(f"exposure_ms={self.exposure_ms}")
        if not self.save:
            parts.append("save=False")
        if self.prefix is not None:
            parts.append(f"prefix={_dsl_str(self.prefix)}")
        if self.dark_file is not None:
            parts.append(f"dark_file={_dsl_str(self.dark_file)}")
        if self.dark_enabled is not None:
            parts.append(f"dark_enabled={self.dark_enabled}")
        if self.defect_file is not None:
            parts.append(f"defect_file={_dsl_str(self.defect_file)}")
        if self.defect_enabled is not None:
            parts.append(f"defect_enabled={self.defect_enabled}")
        if self.defect_kernel is not None:
            parts.append(f"defect_kernel={self.defect_kernel}")
        if self.flip_v is not None:
            parts.append(f"flip_v={self.flip_v}")
        if self.flip_h is not None:
            parts.append(f"flip_h={self.flip_h}")
        if self.oscillate is False:
            # Distinct from omitting oscillate entirely (None = inherit
            # GlobalXrdSettings): an explicit per-step override to disabled
            # must survive round-trip, or a step that was deliberately
            # opted OUT of a globally-enabled oscillation silently opts
            # back in the next time this DSL is compiled.
            parts.append("oscillate=False")
        elif self.oscillate:
            parts.append("oscillate=True")
            if self.osc_pos_a_deg is not None:
                parts.append(f"osc_pos_a_deg={self.osc_pos_a_deg}")
            if self.osc_pos_b_deg is not None:
                parts.append(f"osc_pos_b_deg={self.osc_pos_b_deg}")
            if self.osc_dwell_ms is not None and self.osc_dwell_ms != 0:
                parts.append(f"osc_dwell_ms={self.osc_dwell_ms}")
            if self.osc_speed is not None and self.osc_speed != "M":
                parts.append(f"osc_speed={_dsl_str(self.osc_speed)}")
        return f"take_xrd({', '.join(parts)})"

    @classmethod
    def from_dict(cls, d: dict) -> "TakeXrdAction":
        def _bool_or_none(v) -> bool | None:
            return bool(v) if v is not None else None

        def _int_or_none(v) -> int | None:
            return int(v) if v is not None else None

        def _float_or_none(v) -> float | None:
            return float(v) if v is not None else None

        raw_exp = d.get("exposure_ms")
        return cls(
            exposure_ms=int(raw_exp) if raw_exp is not None else None,
            save=bool(d.get("save", True)),
            # Missing prefix in an older sequence retains its former "scan" default.
            prefix=d.get("prefix", "scan"),
            # A "save_dir" key from an older sequence is ignored: every file
            # is saved under the run directory.
            dark_file=d.get("dark_file"),
            dark_enabled=_bool_or_none(d.get("dark_enabled")),
            defect_file=d.get("defect_file"),
            defect_enabled=_bool_or_none(d.get("defect_enabled")),
            defect_kernel=_int_or_none(d.get("defect_kernel")),
            flip_v=_bool_or_none(d.get("flip_v")),
            flip_h=_bool_or_none(d.get("flip_h")),
            oscillate=_bool_or_none(d.get("oscillate")),
            osc_pos_a_deg=_float_or_none(d.get("osc_pos_a_deg")),
            osc_pos_b_deg=_float_or_none(d.get("osc_pos_b_deg")),
            osc_dwell_ms=_int_or_none(d.get("osc_dwell_ms")),
            osc_speed=d.get("osc_speed"),
        )


@dataclass
class TakeDarkAction(Action):
    TYPE = "take_dark"
    exposure_ms: int

    def describe(self) -> str:
        return f"Dark frame {self.exposure_ms} ms"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "exposure_ms": self.exposure_ms}

    def to_dsl(self) -> str:
        return f"take_dark(exposure_ms={self.exposure_ms})"

    @classmethod
    def from_dict(cls, d: dict) -> "TakeDarkAction":
        return cls(exposure_ms=int(d["exposure_ms"]))


@dataclass
class TakeSpectrumAction(Action):
    """Acquire ruby fluorescence at one or all registered XRD offsets."""

    TYPE = "take_spectrum"
    save: bool = True
    prefix: str = "spectrum"
    position_name: str = "Default"
    all_positions: bool = False
    exposure_time_s: float | None = None
    em_gain_max: int | None = None
    use_global_search: bool = False
    search_best_position_nearby: bool = False
    search_channels: tuple[int, ...] = (4, 5)
    search_range_ch3_um: float = 10.0
    search_range_ch4_um: float = 10.0
    search_range_ch5_um: float = 10.0
    search_step_ch3_um: float = 2.0
    search_step_ch4_um: float = 2.0
    search_step_ch5_um: float = 2.0
    search_when: str = "always"
    stop_pressure_target_gpa: float | None = None
    stop_pressure_tolerance_gpa: float = 0.0

    def describe(self) -> str:
        exposure = (f", {self.exposure_time_s:g} s" if self.exposure_time_s is not None
                    else "")
        timing = "if initial quality is low" if self.search_when == "on_low_quality" else "always"
        nearby = (f", nearby search {timing} on Ch{','.join(map(str, self.search_channels))}"
                  if self.search_best_position_nearby else "")
        position = "all registered positions" if self.all_positions else self.position_name
        stop = (f", stop sequence at ruby pressure "
                f"{self.stop_pressure_target_gpa!r} ± {self.stop_pressure_tolerance_gpa!r} GPa"
                if self.stop_pressure_target_gpa is not None else "")
        return (f"Ruby fluorescence at {position}{exposure}{nearby}"
                f"{f', file prefix: {self.prefix}' if self.save else ''}{stop}")

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "save": self.save, "prefix": self.prefix,
                "position_name": self.position_name,
                "all_positions": self.all_positions,
                "exposure_time_s": self.exposure_time_s,
                "em_gain_max": self.em_gain_max,
                "use_global_search": self.use_global_search,
                "search_best_position_nearby": self.search_best_position_nearby,
                "search_when": self.search_when,
                "search_channels": list(self.search_channels),
                "search_range_ch3_um": self.search_range_ch3_um,
                "search_range_ch4_um": self.search_range_ch4_um,
                "search_range_ch5_um": self.search_range_ch5_um,
                "search_step_ch3_um": self.search_step_ch3_um,
                "search_step_ch4_um": self.search_step_ch4_um,
                "search_step_ch5_um": self.search_step_ch5_um,
                "stop_pressure_target_gpa": self.stop_pressure_target_gpa,
                "stop_pressure_tolerance_gpa": self.stop_pressure_tolerance_gpa}

    def to_dsl(self) -> str:
        parts = []
        if not self.save:
            parts.append("save=False")
        if self.prefix != "spectrum":
            parts.append(f"prefix={_dsl_str(self.prefix)}")
        if self.all_positions:
            parts.append("all_positions=True")
        if self.position_name != "Default":
            parts.append(f"position_name={_dsl_str(self.position_name)}")
        if self.exposure_time_s is not None:
            parts.append(f"exposure_time_s={self.exposure_time_s!r}")
        if self.em_gain_max is not None:
            parts.append(f"em_gain_max={self.em_gain_max!r}")
        if self.use_global_search:
            parts.append("use_global_search=True")
        if self.search_best_position_nearby:
            parts.append("search_best_position_nearby=True")
        if self.search_when != "always":
            parts.append(f"search_when={_dsl_str(self.search_when)}")
        if self.stop_pressure_target_gpa is not None:
            parts.append(f"stop_pressure_target_gpa={self.stop_pressure_target_gpa!r}")
            parts.append(f"stop_pressure_tolerance_gpa={self.stop_pressure_tolerance_gpa!r}")
        if self.search_channels != (4, 5):
            parts.append(f"search_channels={self.search_channels!r}")
        for ch in (3, 4, 5):
            for kind in ("range", "step"):
                name = f"search_{kind}_ch{ch}_um"
                value = getattr(self, name)
                default = 10.0 if kind == "range" else 2.0
                if value != default:
                    parts.append(f"{name}={value!r}")
        return f"take_spectrum({', '.join(parts)})"

    @classmethod
    def from_dict(cls, d: dict) -> "TakeSpectrumAction":
        exposure = d.get("exposure_time_s")
        all_positions = d.get("all_positions", False)
        if type(all_positions) is not bool:
            raise ValueError("all_positions must be a boolean")
        return cls(save=bool(d.get("save", True)), prefix=str(d.get("prefix", "spectrum")),
                   position_name=str(d.get("position_name", "Default")),
                   all_positions=all_positions,
                   exposure_time_s=None if exposure is None else float(exposure),
                   em_gain_max=(None if d.get("em_gain_max") is None
                                else d["em_gain_max"]),
                   use_global_search=d.get("use_global_search", False),
                   search_best_position_nearby=d.get("search_best_position_nearby", False),
                   search_when=d.get("search_when", "always"),
                   stop_pressure_target_gpa=d.get("stop_pressure_target_gpa"),
                   stop_pressure_tolerance_gpa=d.get("stop_pressure_tolerance_gpa", 0.0),
                   search_channels=tuple(d.get("search_channels", (4, 5))),
                   search_range_ch3_um=d.get("search_range_ch3_um", 10.0),
                   search_range_ch4_um=d.get("search_range_ch4_um", 10.0),
                   search_range_ch5_um=d.get("search_range_ch5_um", 10.0),
                   search_step_ch3_um=d.get("search_step_ch3_um", 2.0),
                   search_step_ch4_um=d.get("search_step_ch4_um", 2.0),
                   search_step_ch5_um=d.get("search_step_ch5_um", 2.0))


@dataclass
class CompressToTargetPressureUsingRubyAction(TakeSpectrumAction):
    """Increment PACE5000 pressure and check one ruby after each increment."""

    TYPE = "compress_to_target_pressure_using_ruby"
    target_sample_pressure_gpa: float | str = 1.0
    gas_pressure_increment: float = 0.1
    gas_pressure_unit: str = "MPa"
    stabilisation_duration_s: float = 10.0
    max_allowed_gas_pressure: float = 10.0
    gas_pressure_rate: float | None = None
    gas_pressure_rate_unit: str = "MPa/min"
    gas_pressure_tolerance: float = 0.001

    def describe(self) -> str:
        target = (self.target_sample_pressure_gpa if isinstance(self.target_sample_pressure_gpa, str)
                  else f"{self.target_sample_pressure_gpa:g}")
        return (f"Compress to {target} GPa using ruby "
                f"{self.position_name} ({self.gas_pressure_increment:g} "
                f"{self.gas_pressure_unit} steps, max "
                f"{self.max_allowed_gas_pressure:g} {self.gas_pressure_unit})")

    def to_dict(self) -> dict:
        d = {
            **super().to_dict(), "type": self.TYPE,
            "gas_pressure_increment": self.gas_pressure_increment,
            "gas_pressure_unit": self.gas_pressure_unit,
            "stabilisation_duration_s": self.stabilisation_duration_s,
            "max_allowed_gas_pressure": self.max_allowed_gas_pressure,
            "gas_pressure_rate": self.gas_pressure_rate,
            "gas_pressure_rate_unit": self.gas_pressure_rate_unit,
            "gas_pressure_tolerance": self.gas_pressure_tolerance,
        }
        if isinstance(self.target_sample_pressure_gpa, str):
            d["target_sample_pressure_gpa_var"] = self.target_sample_pressure_gpa
        else:
            d["target_sample_pressure_gpa"] = self.target_sample_pressure_gpa
        return d

    def to_dsl(self) -> str:
        ruby_args = super().to_dsl()[len("take_spectrum("):-1]
        target = (self.target_sample_pressure_gpa if isinstance(self.target_sample_pressure_gpa, str)
                  else repr(self.target_sample_pressure_gpa))
        args = [
            f"target_sample_pressure_gpa={target}",
            f"gas_pressure_increment={self.gas_pressure_increment!r}",
            f"gas_pressure_unit={_dsl_str(self.gas_pressure_unit)}",
            f"stabilisation_duration_s={self.stabilisation_duration_s!r}",
            f"max_allowed_gas_pressure={self.max_allowed_gas_pressure!r}",
        ]
        if self.gas_pressure_rate is not None:
            args.append(f"gas_pressure_rate={self.gas_pressure_rate!r}")
        if self.gas_pressure_rate is not None or self.gas_pressure_rate_unit != "MPa/min":
            args.append(f"gas_pressure_rate_unit={_dsl_str(self.gas_pressure_rate_unit)}")
        if self.gas_pressure_tolerance != 0.001:
            args.append(f"gas_pressure_tolerance={self.gas_pressure_tolerance!r}")
        if ruby_args:
            args.append(ruby_args)
        return f"compress_to_target_pressure_using_ruby({', '.join(args)})"

    @classmethod
    def from_dict(cls, d: dict) -> "CompressToTargetPressureUsingRubyAction":
        from .compression import numeric_field

        ruby = TakeSpectrumAction.from_dict(d)
        return cls(
            **vars(ruby),
            target_sample_pressure_gpa=(
                d["target_sample_pressure_gpa_var"]
                if "target_sample_pressure_gpa_var" in d else numeric_field(
                    d["target_sample_pressure_gpa"], "target_sample_pressure_gpa",
                )
            ),
            gas_pressure_increment=numeric_field(
                d["gas_pressure_increment"], "gas_pressure_increment",
            ),
            gas_pressure_unit=str(d["gas_pressure_unit"]),
            stabilisation_duration_s=numeric_field(
                d["stabilisation_duration_s"], "stabilisation_duration_s",
            ),
            max_allowed_gas_pressure=numeric_field(
                d["max_allowed_gas_pressure"], "max_allowed_gas_pressure",
            ),
            gas_pressure_rate=(None if d.get("gas_pressure_rate") is None
                               else numeric_field(
                                   d["gas_pressure_rate"], "gas_pressure_rate",
                               )),
            gas_pressure_rate_unit=str(d.get("gas_pressure_rate_unit", "MPa/min")),
            gas_pressure_tolerance=numeric_field(
                d.get("gas_pressure_tolerance", 0.001), "gas_pressure_tolerance",
            ),
        )


@dataclass
class DecompressToTargetPressureUsingRubyAction(CompressToTargetPressureUsingRubyAction):
    """Lower PACE5000 pressure and check one ruby after each decrement."""

    TYPE = "decompress_to_target_pressure_using_ruby"
    max_allowed_gas_pressure: float = field(default=10.0, init=False, repr=False,
                                            compare=False)
    min_allowed_gas_pressure: float = 0.1

    def describe(self) -> str:
        target = (self.target_sample_pressure_gpa if isinstance(self.target_sample_pressure_gpa, str)
                  else f"{self.target_sample_pressure_gpa:g}")
        return (f"Decompress to {target} GPa using ruby "
                f"{self.position_name} ({self.gas_pressure_increment:g} "
                f"{self.gas_pressure_unit} steps, min "
                f"{self.min_allowed_gas_pressure:g} {self.gas_pressure_unit})")

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.pop("max_allowed_gas_pressure")
        d["min_allowed_gas_pressure"] = self.min_allowed_gas_pressure
        return d

    def to_dsl(self) -> str:
        dsl = super().to_dsl()
        return dsl.replace("compress_to_target_pressure_using_ruby(",
                           "decompress_to_target_pressure_using_ruby(", 1).replace(
            f"max_allowed_gas_pressure={self.max_allowed_gas_pressure!r}",
            f"min_allowed_gas_pressure={self.min_allowed_gas_pressure!r}", 1,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "DecompressToTargetPressureUsingRubyAction":
        from .compression import numeric_field

        converted = dict(d)
        minimum = numeric_field(converted.pop("min_allowed_gas_pressure"),
                                "min_allowed_gas_pressure")
        converted["max_allowed_gas_pressure"] = minimum
        compression_action = CompressToTargetPressureUsingRubyAction.from_dict(converted)
        fields = vars(compression_action).copy()
        fields.pop("max_allowed_gas_pressure")
        return cls(**fields, min_allowed_gas_pressure=minimum)


# ── Camera ───────────────────────────────────────────────────────────

@dataclass
class SaveReferenceImageAction(Action):
    TYPE = "save_reference_image"
    path: str | None = None       # None -> __localdata/reference_frame.png
    camera_index: int = 0

    def describe(self) -> str:
        dst = self.path or "__localdata/reference_frame.png"
        return f"Save reference image → {dst}"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "path": self.path, "camera_index": self.camera_index}

    def to_dsl(self) -> str:
        parts = []
        if self.path is not None:
            parts.append(f"path={_dsl_str(self.path)}")
        if self.camera_index != 0:
            parts.append(f"camera_index={self.camera_index}")
        return f"save_reference_image({', '.join(parts)})"

    @classmethod
    def from_dict(cls, d: dict) -> "SaveReferenceImageAction":
        return cls(path=d.get("path"), camera_index=int(d.get("camera_index", 0)))


@dataclass
class SaveSnapshotAction(Action):
    """Save one USB camera frame to <run directory>/photo/."""
    TYPE = "save_snapshot"

    def describe(self) -> str:
        return "Save snapshot -> run photo folder"

    def to_dict(self) -> dict:
        return {"type": self.TYPE}

    def to_dsl(self) -> str:
        return "save_snapshot()"

    @classmethod
    def from_dict(cls, d: dict) -> "SaveSnapshotAction":
        # An older sequence's "save_dir" key is ignored.
        return cls()


@dataclass
class StartFollowingAction(Action):
    """Start background sample-following thread. Returns immediately."""
    TYPE = "start_following"
    reference_path: str | None = None              # None -> GlobalFollowSettings.reference_path
    interval_s: float | None = None               # None → GlobalFollowSettings
    similarity_threshold: float | None = None     # None → GlobalFollowSettings
    max_correction_per_step_um: float | None = None  # None → GlobalFollowSettings
    camera_index: int = 0
    autofocus_enabled: bool = True
    autofocus_range_um: float | None = None       # None → use GlobalFollowSettings
    autofocus_steps: int | None = None            # None → use GlobalFollowSettings

    def describe(self) -> str:
        interval = f" (interval={_num(self.interval_s)} s)" if self.interval_s else ""
        return f"Start following{interval}" + (" + AF" if self.autofocus_enabled else "")

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "reference_path": self.reference_path,
            "interval_s": self.interval_s,
            "similarity_threshold": self.similarity_threshold,
            "max_correction_per_step_um": self.max_correction_per_step_um,
            "camera_index": self.camera_index,
            "autofocus_enabled": self.autofocus_enabled,
            "autofocus_range_um": self.autofocus_range_um,
            "autofocus_steps": self.autofocus_steps,
        }

    def to_dsl(self) -> str:
        parts = []
        if self.reference_path is not None:
            parts.append(f"reference_path={_dsl_str(self.reference_path)}")
        if self.interval_s is not None:
            parts.append(f'interval={self.interval_s}, interval_unit="s"')
        if self.similarity_threshold is not None:
            parts.append(f"similarity_threshold={self.similarity_threshold}")
        if self.max_correction_per_step_um is not None:
            parts.append(f"max_correction_per_step_um={self.max_correction_per_step_um}")
        if self.camera_index != 0:
            parts.append(f"camera_index={self.camera_index}")
        if not self.autofocus_enabled:
            # Distinct from omitting it entirely (default True) — an
            # explicit False must survive round-trip, or a step that
            # deliberately disabled Ch3 autofocus silently re-enables it
            # (and starts moving Ch3) the next time this DSL is compiled.
            parts.append("autofocus_enabled=False")
        if self.autofocus_range_um is not None:
            parts.append(f"autofocus_range_um={self.autofocus_range_um}")
        if self.autofocus_steps is not None:
            parts.append(f"autofocus_steps={self.autofocus_steps}")
        return f"start_following({', '.join(parts)})"

    @classmethod
    def from_dict(cls, d: dict) -> "StartFollowingAction":
        raw_range = d.get("autofocus_range_um")
        raw_steps = d.get("autofocus_steps")
        return cls(
            reference_path=d.get("reference_path"),
            interval_s=d.get("interval_s"),
            similarity_threshold=d.get("similarity_threshold"),
            max_correction_per_step_um=d.get("max_correction_per_step_um"),
            camera_index=int(d.get("camera_index", 0)),
            autofocus_enabled=bool(d.get("autofocus_enabled", True)),
            autofocus_range_um=float(raw_range) if raw_range is not None else None,
            autofocus_steps=int(raw_steps) if raw_steps is not None else None,
        )


@dataclass
class StopFollowingAction(Action):
    """Signal the background following thread to stop; blocks until it exits."""
    TYPE = "stop_following"

    def describe(self) -> str:
        return "Stop following"

    def to_dict(self) -> dict:
        return {"type": self.TYPE}

    def to_dsl(self) -> str:
        return "stop_following()"

    @classmethod
    def from_dict(cls, d: dict) -> "StopFollowingAction":
        return cls()


@dataclass
class AlignToReferenceAction(Action):
    """
    One-shot alignment to the reference image: performs a single follow
    correction cycle (XY correction + re-correction retries, then optional
    Ch3 autofocus) synchronously, then proceeds to the next step. Unlike
    start_following, no background thread is left running.
    """
    TYPE = "align_to_reference"
    reference_path: str | None = None              # None -> GlobalFollowSettings.reference_path
    similarity_threshold: float | None = None     # None → scheduler_presets.json
    max_correction_per_step_um: float | None = None  # None → scheduler_presets.json
    camera_index: int = 0
    autofocus_enabled: bool = True
    autofocus_range_um: float | None = None       # None → use GlobalFollowSettings
    autofocus_steps: int | None = None            # None → use GlobalFollowSettings

    def describe(self) -> str:
        return "Align to reference" + (" + AF" if self.autofocus_enabled else "")

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "reference_path": self.reference_path,
            "similarity_threshold": self.similarity_threshold,
            "max_correction_per_step_um": self.max_correction_per_step_um,
            "camera_index": self.camera_index,
            "autofocus_enabled": self.autofocus_enabled,
            "autofocus_range_um": self.autofocus_range_um,
            "autofocus_steps": self.autofocus_steps,
        }

    def to_dsl(self) -> str:
        parts = []
        if self.reference_path is not None:
            parts.append(f"reference_path={_dsl_str(self.reference_path)}")
        if self.similarity_threshold is not None:
            parts.append(f"similarity_threshold={self.similarity_threshold}")
        if self.max_correction_per_step_um is not None:
            parts.append(f"max_correction_per_step_um={self.max_correction_per_step_um}")
        if self.camera_index != 0:
            parts.append(f"camera_index={self.camera_index}")
        if not self.autofocus_enabled:
            # See StartFollowingAction.to_dsl() — must survive round-trip.
            parts.append("autofocus_enabled=False")
        if self.autofocus_range_um is not None:
            parts.append(f"autofocus_range_um={self.autofocus_range_um}")
        if self.autofocus_steps is not None:
            parts.append(f"autofocus_steps={self.autofocus_steps}")
        return f"align_to_reference({', '.join(parts)})"

    @classmethod
    def from_dict(cls, d: dict) -> "AlignToReferenceAction":
        raw_range = d.get("autofocus_range_um")
        raw_steps = d.get("autofocus_steps")
        return cls(
            reference_path=d.get("reference_path"),
            similarity_threshold=d.get("similarity_threshold"),
            max_correction_per_step_um=d.get("max_correction_per_step_um"),
            camera_index=int(d.get("camera_index", 0)),
            autofocus_enabled=bool(d.get("autofocus_enabled", True)),
            autofocus_range_um=float(raw_range) if raw_range is not None else None,
            autofocus_steps=int(raw_steps) if raw_steps is not None else None,
        )


@dataclass
class FollowSampleAction(Action):
    """
    Fixed-duration follow. Sugar for start_following + wait + stop_following.
    """
    TYPE = "follow_sample_position"
    duration_s: float
    reference_path: str | None = None              # None -> GlobalFollowSettings.reference_path
    interval_s: float | None = None
    similarity_threshold: float | None = None
    max_correction_per_step_um: float | None = None
    camera_index: int = 0
    autofocus_enabled: bool = True
    autofocus_range_um: float | None = None       # None → use GlobalFollowSettings
    autofocus_steps: int | None = None            # None → use GlobalFollowSettings

    def describe(self) -> str:
        mins = self.duration_s / 60
        dur_str = f"{mins:.0f} min" if mins >= 1 else f"{self.duration_s:.0f} s"
        interval = f" (interval={_num(self.interval_s)} s)" if self.interval_s else ""
        return f"Follow sample {dur_str}{interval}" + (" + AF" if self.autofocus_enabled else "")

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "duration_s": self.duration_s,
            "reference_path": self.reference_path,
            "interval_s": self.interval_s,
            "similarity_threshold": self.similarity_threshold,
            "max_correction_per_step_um": self.max_correction_per_step_um,
            "camera_index": self.camera_index,
            "autofocus_enabled": self.autofocus_enabled,
            "autofocus_range_um": self.autofocus_range_um,
            "autofocus_steps": self.autofocus_steps,
        }

    def to_dsl(self) -> str:
        if self.duration_s >= 60 and self.duration_s % 60 == 0:
            dur_part = f'duration={int(self.duration_s // 60)}, unit="min"'
        else:
            dur_part = f'duration={self.duration_s}, unit="s"'
        parts = [dur_part]
        if self.reference_path is not None:
            parts.append(f"reference_path={_dsl_str(self.reference_path)}")
        if self.interval_s is not None:
            parts.append(f'interval={self.interval_s}, interval_unit="s"')
        if self.similarity_threshold is not None:
            parts.append(f"similarity_threshold={self.similarity_threshold}")
        if self.max_correction_per_step_um is not None:
            parts.append(f"max_correction_per_step_um={self.max_correction_per_step_um}")
        if self.camera_index != 0:
            parts.append(f"camera_index={self.camera_index}")
        if not self.autofocus_enabled:
            # See StartFollowingAction.to_dsl() — must survive round-trip.
            parts.append("autofocus_enabled=False")
        if self.autofocus_range_um is not None:
            parts.append(f"autofocus_range_um={self.autofocus_range_um}")
        if self.autofocus_steps is not None:
            parts.append(f"autofocus_steps={self.autofocus_steps}")
        return f"follow_sample_position({', '.join(parts)})"

    def to_steps(self) -> tuple["StartFollowingAction", "WaitAction", "StopFollowingAction"]:
        return (
            StartFollowingAction(
                reference_path=self.reference_path,
                interval_s=self.interval_s,
                similarity_threshold=self.similarity_threshold,
                max_correction_per_step_um=self.max_correction_per_step_um,
                camera_index=self.camera_index,
                autofocus_enabled=self.autofocus_enabled,
                autofocus_range_um=self.autofocus_range_um,
                autofocus_steps=self.autofocus_steps,
            ),
            WaitAction(duration_s=self.duration_s),
            StopFollowingAction(),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "FollowSampleAction":
        raw_range = d.get("autofocus_range_um")
        raw_steps = d.get("autofocus_steps")
        return cls(
            duration_s=float(d["duration_s"]),
            reference_path=d.get("reference_path"),
            interval_s=d.get("interval_s"),
            similarity_threshold=d.get("similarity_threshold"),
            max_correction_per_step_um=d.get("max_correction_per_step_um"),
            camera_index=int(d.get("camera_index", 0)),
            autofocus_enabled=bool(d.get("autofocus_enabled", True)),
            autofocus_range_um=float(raw_range) if raw_range is not None else None,
            autofocus_steps=int(raw_steps) if raw_steps is not None else None,
        )


# ── Control Flow ─────────────────────────────────────────────────────

@dataclass
class ForLoopAction(Action):
    """
    Preserved loop structure (not expanded at compile time).
    Produced by the DSL parser; not available in Mode 1 (Visual) editor.
    body may contain any Action including nested ForLoopActions.
    """
    TYPE = "for_loop"
    var: str
    values: list
    body: list = field(default_factory=list)

    def describe(self) -> str:
        vals_str = str(self.values[:3])[:-1] + ("..." if len(self.values) > 3 else "]")
        return f"for {self.var} in {vals_str}  ({len(self.body)} steps, {len(self.values)} loops)"

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "var": self.var,
            "values": list(self.values),
            "body": [a.to_dict() for a in self.body],
        }

    def to_dsl(self) -> str:
        vals = "[" + ", ".join(repr(v) for v in self.values) + "]"
        header = f"for {self.var} in {vals}:"
        body_lines = []
        for action in self.body:
            for line in action.to_dsl().splitlines():
                body_lines.append("    " + line)
        return header + "\n" + "\n".join(body_lines)

    @classmethod
    def from_dict(cls, d: dict) -> "ForLoopAction":
        body = [action_from_dict(a) for a in d["body"]]
        return cls(var=d["var"], values=list(d["values"]), body=body)


# ── Factory ──────────────────────────────────────────────────────────

_STAGE_OPERATIONS = StageAction.OPERATIONS

_REGISTRY: dict[str, type[Action]] = {
    WaitAction.TYPE: WaitAction,
    LogAction.TYPE: LogAction,
    # Stage — all dispatch to StageAction; operation is encoded in "type"
    **{op: StageAction for op in _STAGE_OPERATIONS},
    MicroscopeOutFpdInAction.TYPE: MicroscopeOutFpdInAction,
    FpdOutMicroscopeInAction.TYPE: FpdOutMicroscopeInAction,
    # Pressure
    SetPressureAction.TYPE: SetPressureAction,
    WaitPressureAction.TYPE: WaitPressureAction,
    SetAndWaitPressureAction.TYPE: SetAndWaitPressureAction,
    SetControlModeAction.TYPE: SetControlModeAction,
    # Temperature
    SetTemperatureAction.TYPE: SetTemperatureAction,
    WaitTemperatureAction.TYPE: WaitTemperatureAction,
    SetAndWaitTemperatureAction.TYPE: SetAndWaitTemperatureAction,
    SetHeaterAction.TYPE: SetHeaterAction,
    AllHeatersOffAction.TYPE: AllHeatersOffAction,
    # Radicon
    TakeXrdAction.TYPE: TakeXrdAction,
    TakeDarkAction.TYPE: TakeDarkAction,
    TakeSpectrumAction.TYPE: TakeSpectrumAction,
    CompressToTargetPressureUsingRubyAction.TYPE: CompressToTargetPressureUsingRubyAction,
    DecompressToTargetPressureUsingRubyAction.TYPE: DecompressToTargetPressureUsingRubyAction,
    # Camera
    SaveReferenceImageAction.TYPE: SaveReferenceImageAction,
    SaveSnapshotAction.TYPE: SaveSnapshotAction,
    StartFollowingAction.TYPE: StartFollowingAction,
    StopFollowingAction.TYPE: StopFollowingAction,
    FollowSampleAction.TYPE: FollowSampleAction,
    AlignToReferenceAction.TYPE: AlignToReferenceAction,
    # Control flow
    ForLoopAction.TYPE: ForLoopAction,
}


def action_from_dict(d: dict) -> Action:
    """Deserialize a single action from its dict representation."""
    t = d.get("type")
    cls = _REGISTRY.get(t)
    if cls is None:
        raise ValueError(f"Unknown action type: {t!r}")
    return cls.from_dict(d)


# ── Loop-variable helpers ───────────────────────────────────────────────
#
# A handful of Action fields accept float | str, where a str means "resolve
# this from the enclosing ForLoopAction's var_context at run time" (see
# runner.py _do_stage / _do_set_pressure / _do_set_temperature /
# _do_compress_to_target_pressure_using_ruby). This table
# is the single source of truth for which (action type, field name) pairs
# support that — shared by the Visual editor (StepEditorDialog), the
# validator (validator/pre_validator.py), and rename_loop_var_refs() below.

LOOP_VAR_FIELDS: dict[type, str] = {
    StageAction: "value",
    SetPressureAction: "pressure",
    SetAndWaitPressureAction: "pressure",
    SetTemperatureAction: "value_k",
    SetAndWaitTemperatureAction: "value_k",
    CompressToTargetPressureUsingRubyAction: "target_sample_pressure_gpa",
    DecompressToTargetPressureUsingRubyAction: "target_sample_pressure_gpa",
}


def action_loop_var_ref(action: Action) -> str | None:
    """Return the loop-variable name held by `action`'s direct loop-var
    field, or None if that field holds a constant (or the action has none)."""
    field = LOOP_VAR_FIELDS.get(type(action))
    if field is None:
        return None
    value = getattr(action, field)
    return value if isinstance(value, str) else None


def rename_loop_var_refs(body: list[Action], old: str, new: str) -> None:
    """Rewrite every reference to loop variable `old` inside `body` to `new`,
    in place. Used when a ForLoopAction is edited and its `var` is renamed.

    Handles both direct field references (StageAction.value == "p") and
    f-string placeholders embedded in other string fields (e.g. a LogAction
    message containing "{p}"). A nested ForLoopAction that shadows `old`
    (redeclares the same variable name) is left untouched, since its body's
    `old` references belong to the inner loop, not this one.
    """
    placeholder_old = "{" + old + "}"
    placeholder_new = "{" + new + "}"
    for action in body:
        if isinstance(action, ForLoopAction):
            if action.var == old:
                continue
            rename_loop_var_refs(action.body, old, new)
            continue

        field = LOOP_VAR_FIELDS.get(type(action))
        if field is not None and getattr(action, field) == old:
            setattr(action, field, new)

        for key, value in vars(action).items():
            if isinstance(value, str) and placeholder_old in value:
                setattr(action, key, value.replace(placeholder_old, placeholder_new))
