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
        escaped = self.message.replace('"', '\\"')
        return f'log_message(message="{escaped}")'

    @classmethod
    def from_dict(cls, d: dict) -> "LogAction":
        return cls(message=d["message"])


# ── Stage (Primitive) ────────────────────────────────────────────────

@dataclass
class StageAction(Action):
    """
    Covers move_absolute, move_relative, set_speed, emergency_stop.
    `operation` is used as the JSON "type" key.

    For move_absolute / move_relative: value = position / delta (pulses).
    For set_speed: value = 0 (unused); speed carries "H"/"M"/"L".
    For emergency_stop: ch = 0, value = 0 (both unused).

    `value` is float|str to support loop-variable references (str = variable name).
    """
    OPERATIONS = {"move_absolute", "move_relative", "set_speed", "emergency_stop"}

    operation: str
    ch: int = 0
    value: float | str = 0
    speed: str | None = None

    def describe(self) -> str:
        if self.operation == "move_absolute":
            return f"Stage Ch{self.ch} → {self.value} (abs)"
        if self.operation == "move_relative":
            return f"Stage Ch{self.ch} Δ{self.value}"
        if self.operation == "set_speed":
            return f"Stage Ch{self.ch} speed={self.speed}"
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
            return f"move_absolute(ch={self.ch}, position={val})"
        if self.operation == "move_relative":
            return f"move_relative(ch={self.ch}, delta={val})"
        if self.operation == "set_speed":
            return f'set_speed(ch={self.ch}, speed="{self.speed}")'
        return "emergency_stop()"

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
    microscope_out_pos: int | None = None   # None → read from stage_settings.json
    fpd_in_pos: int | None = None           # None → read from stage_settings.json
    speed: str = "H"

    def describe(self) -> str:
        return "Microscope OUT → FPD IN (XRD mode)"

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
            parts.append(f'speed="{self.speed}"')
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
    fpd_out_pos: int | None = None          # None → read from stage_settings.json
    microscope_in_pos: int | None = None    # None → read from stage_settings.json
    speed: str = "H"

    def describe(self) -> str:
        return "FPD OUT → Microscope IN (microscopy mode)"

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
            parts.append(f'speed="{self.speed}"')
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
            f'set_pressure(pressure={p_expr}, unit="{self.unit}", '
            f'rate={self.rate}, rate_unit="{self.rate_unit}")'
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
        return f'wait_pressure(tol={self.tol}, unit="{self.unit}")'

    @classmethod
    def from_dict(cls, d: dict) -> "WaitPressureAction":
        return cls(tol=float(d["tol"]), unit=d["unit"])


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

    def describe(self) -> str:
        return f"Wait temperature ±{self.tol_k} K"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "tol_k": self.tol_k}

    def to_dsl(self) -> str:
        return f'wait_temperature(tol={self.tol_k}, unit="K")'

    @classmethod
    def from_dict(cls, d: dict) -> "WaitTemperatureAction":
        return cls(tol_k=float(d["tol_k"]))


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


# ── Keithley 2000 ────────────────────────────────────────────────────

@dataclass
class ReadIntensityAction(Action):
    TYPE = "read_intensity"
    variable_name: str

    def describe(self) -> str:
        return f"Read intensity → {self.variable_name}"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "variable_name": self.variable_name}

    def to_dsl(self) -> str:
        return f'read_intensity(variable="{self.variable_name}")'

    @classmethod
    def from_dict(cls, d: dict) -> "ReadIntensityAction":
        return cls(variable_name=d["variable_name"])


# ── Rad-icon 2022 ────────────────────────────────────────────────────

@dataclass
class TakeXrdAction(Action):
    TYPE = "take_xrd"
    # None = inherit from GlobalXrdSettings
    exposure_ms: int | None = None
    save: bool = True
    prefix: str = "scan"
    # Per-step overrides (None = inherit from GlobalXrdSettings)
    save_dir: str | None = None
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
        exp_str = f"{self.exposure_ms} ms" if self.exposure_ms is not None else "global exp"
        save_str = f" save→{self.prefix}" if self.save else ""
        osc_str = " +OSC" if self.oscillate else ""
        return f"XRD {exp_str}{save_str}{osc_str}"

    def to_dict(self) -> dict:
        return {
            "type": self.TYPE,
            "exposure_ms": self.exposure_ms,
            "save": self.save,
            "prefix": self.prefix,
            "save_dir": self.save_dir,
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
        if self.prefix != "scan":
            parts.append(f'prefix="{self.prefix}"')
        if self.save_dir is not None:
            parts.append(f'save_dir="{self.save_dir}"')
        if self.dark_file is not None:
            parts.append(f'dark_file="{self.dark_file}"')
        if self.dark_enabled is not None:
            parts.append(f"dark_enabled={self.dark_enabled}")
        if self.defect_file is not None:
            parts.append(f'defect_file="{self.defect_file}"')
        if self.defect_enabled is not None:
            parts.append(f"defect_enabled={self.defect_enabled}")
        if self.defect_kernel is not None:
            parts.append(f"defect_kernel={self.defect_kernel}")
        if self.flip_v is not None:
            parts.append(f"flip_v={self.flip_v}")
        if self.flip_h is not None:
            parts.append(f"flip_h={self.flip_h}")
        if self.oscillate:
            parts.append("oscillate=True")
            if self.osc_pos_a_deg is not None:
                parts.append(f"osc_pos_a_deg={self.osc_pos_a_deg}")
            if self.osc_pos_b_deg is not None:
                parts.append(f"osc_pos_b_deg={self.osc_pos_b_deg}")
            if self.osc_dwell_ms is not None and self.osc_dwell_ms != 0:
                parts.append(f"osc_dwell_ms={self.osc_dwell_ms}")
            if self.osc_speed is not None and self.osc_speed != "M":
                parts.append(f'osc_speed="{self.osc_speed}"')
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
            prefix=d.get("prefix", "scan"),
            save_dir=d.get("save_dir"),
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


# ── Camera ───────────────────────────────────────────────────────────

@dataclass
class SaveReferenceImageAction(Action):
    TYPE = "save_reference_image"
    path: str | None = None       # None → __localdata/reference_frame.npz
    camera_index: int = 0

    def describe(self) -> str:
        dst = self.path or "__localdata/reference_frame.npz"
        return f"Save reference image → {dst}"

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "path": self.path, "camera_index": self.camera_index}

    def to_dsl(self) -> str:
        parts = []
        if self.path is not None:
            parts.append(f'path="{self.path}"')
        if self.camera_index != 0:
            parts.append(f"camera_index={self.camera_index}")
        return f"save_reference_image({', '.join(parts)})"

    @classmethod
    def from_dict(cls, d: dict) -> "SaveReferenceImageAction":
        return cls(path=d.get("path"), camera_index=int(d.get("camera_index", 0)))


@dataclass
class StartFollowingAction(Action):
    """Start background sample-following thread. Returns immediately."""
    TYPE = "start_following"
    reference_path: str | None = None              # None → __localdata/reference_frame.npz
    interval_s: float | None = None               # None → scheduler_presets.json
    similarity_threshold: float | None = None     # None → scheduler_presets.json
    max_correction_per_step_um: float | None = None  # None → scheduler_presets.json
    camera_index: int = 0
    autofocus_enabled: bool = True
    autofocus_range_um: float | None = None       # None → use GlobalFollowSettings
    autofocus_steps: int | None = None            # None → use GlobalFollowSettings

    def describe(self) -> str:
        interval = f"{self.interval_s}s" if self.interval_s else "preset"
        return f"Start following (interval={interval}) +AF"

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
            parts.append(f'reference_path="{self.reference_path}"')
        if self.interval_s is not None:
            parts.append(f"interval={self.interval_s}, interval_unit=\"s\"")
        if self.similarity_threshold is not None:
            parts.append(f"similarity_threshold={self.similarity_threshold}")
        if self.max_correction_per_step_um is not None:
            parts.append(f"max_correction_per_step_um={self.max_correction_per_step_um}")
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
            autofocus_enabled=True,
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
class FollowSampleAction(Action):
    """
    Fixed-duration follow. Sugar for start_following + wait + stop_following.
    """
    TYPE = "follow_sample_position"
    duration_s: float
    reference_path: str | None = None
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
        return f"Follow sample {dur_str} +AF"

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
            parts.append(f'reference_path="{self.reference_path}"')
        if self.interval_s is not None:
            parts.append(f'interval={self.interval_s}, interval_unit="s"')
        if self.similarity_threshold is not None:
            parts.append(f"similarity_threshold={self.similarity_threshold}")
        if self.max_correction_per_step_um is not None:
            parts.append(f"max_correction_per_step_um={self.max_correction_per_step_um}")
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
            autofocus_enabled=True,
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
        return f"for {self.var} in {vals_str}  ({len(self.body)} steps)"

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
    SetControlModeAction.TYPE: SetControlModeAction,
    # Temperature
    SetTemperatureAction.TYPE: SetTemperatureAction,
    WaitTemperatureAction.TYPE: WaitTemperatureAction,
    SetHeaterAction.TYPE: SetHeaterAction,
    AllHeatersOffAction.TYPE: AllHeatersOffAction,
    # Keithley
    ReadIntensityAction.TYPE: ReadIntensityAction,
    # Radicon
    TakeXrdAction.TYPE: TakeXrdAction,
    TakeDarkAction.TYPE: TakeDarkAction,
    # Camera
    SaveReferenceImageAction.TYPE: SaveReferenceImageAction,
    StartFollowingAction.TYPE: StartFollowingAction,
    StopFollowingAction.TYPE: StopFollowingAction,
    FollowSampleAction.TYPE: FollowSampleAction,
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
