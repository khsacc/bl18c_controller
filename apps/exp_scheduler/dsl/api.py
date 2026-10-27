"""
DSL API — the single source of truth for each DSL command's declaration.

Every function below exists only to be decorated with @dsl_command: the
decorator (dsl/_registry.py) captures the function's category/example
metadata, Python signature, docstring, and Action factory (dsl/_factories.py)
into one CommandSpec, then REPLACES the function with a stub that always
raises NotImplementedError — REORGANISATION_PLAN.md Phase 9. None of these
functions is ever actually called; the production DSL pipeline
(dsl/compiler.py::DslCompiler: normalize() -> ASTValidator -> SequenceBuilder)
builds Actions from a validated AST via CommandSpec.factory, and
llm/prompt_builder.py renders the System Prompt from CommandSpec's
signature/docstring/example — both go through dsl/_registry.py::get_registry(),
never through calling a function here.

ALLOWED_FUNCTIONS in dsl/__init__.py is derived from this module's
@dsl_command registrations (dsl/_registry.py) — there is nothing further to
keep in sync there.

The @dsl_command decorator captures this module's single source of truth
for each command: category and example metadata (for the LLM System
Prompt), the Python signature and docstring (also for the LLM System
Prompt — docstrings are written as LLM specifications), the Action factory
(see dsl/_factories.py), and per-argument unit/bound/loop-variable rules
(consumed by dsl/validator.py and dsl/parser.py).
"""
from __future__ import annotations

from . import _factories
from ._registry import ArgumentRule, dsl_command


# ── General ──────────────────────────────────────────────────────────────────

@dsl_command(
    category="General",
    example='wait(duration=5.0, unit="min")',
    factory=_factories.wait,
    argument_rules={
        "duration": ArgumentRule(lower_bound=0.0, lower_bound_inclusive=False),
        "unit": ArgumentRule(valid_values=frozenset({"s", "min"})),
    },
)
def wait(duration: float, unit: str = "min") -> None:
    """Wait for a fixed duration without doing anything else.

    Parameters
    ----------
    duration : float
        Duration to wait. Must be positive.
    unit : str
        Time unit. Must be "s" or "min".
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="General",
    factory=_factories.log_message,
    argument_rules={"message": ArgumentRule(loop_var_allowed=True)},
)
def log_message(message: str) -> None:
    """Write a free-text message to the sequence execution log.

    Parameters
    ----------
    message : str
        Text to record. Supports f-string variable interpolation.

    Notes
    -----
    This is a convenience step — it does not affect hardware.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


# ── Stage (primitive) ─────────────────────────────────────────────────────────

@dsl_command(
    category="Stage",
    factory=_factories.move_absolute,
    argument_rules={"position": ArgumentRule(loop_var_allowed=True)},
)
def move_absolute(ch: int, position: float) -> None:
    """Move a stage channel to an absolute position (pulses).

    Parameters
    ----------
    ch : int
        Channel number (1–11). Common channels: 3=Z-focus, 4=Sample-X,
        5=Sample-Y, 8=Microscope arm, 9=Detector.
    position : float
        Target position in pulses. Range: ±2,147,483,647.

    Notes
    -----
    The move blocks until the channel stops.
    Inter-channel constraints (Ch8/Ch9 collision prevention) are enforced.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Stage",
    factory=_factories.move_relative,
    argument_rules={"delta": ArgumentRule(loop_var_allowed=True)},
)
def move_relative(ch: int, delta: float) -> None:
    """Move a stage channel by a relative offset (pulses).

    Parameters
    ----------
    ch : int
        Channel number (1–11).
    delta : float
        Displacement in pulses. Positive = forward, negative = backward.

    Notes
    -----
    The move blocks until the channel stops.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Stage",
    factory=_factories.set_speed,
    argument_rules={"speed": ArgumentRule(valid_values=frozenset({"H", "M", "L"}))},
)
def set_speed(ch: int, speed: str) -> None:
    """Set the movement speed for a stage channel.

    Parameters
    ----------
    ch : int
        Channel number (1–11).
    speed : str
        Speed level. Must be "H" (High), "M" (Medium), or "L" (Low).
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Stage", factory=_factories.normal_stop)
def normal_stop() -> None:
    """Decelerate-stop all stage channels.

    Notes
    -----
    Use this for a controlled stop. Unlike emergency_stop(), the motors
    ramp down instead of stopping abruptly.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Stage", factory=_factories.emergency_stop)
def emergency_stop() -> None:
    """Emergency stop all stage channels immediately.

    Notes
    -----
    Use only in emergency situations. This is an abrupt stop (no deceleration).
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Stage",
    example="microscope_out_and_fpd_in()  # switch to XRD measurement mode",
    factory=_factories.microscope_out_and_fpd_in,
    argument_rules={"speed": ArgumentRule(valid_values=frozenset({"H", "M", "L"}))},
)
def microscope_out_and_fpd_in(
    microscope_out_pos: int | None = None,
    fpd_in_pos: int | None = None,
    speed: str = "H",
) -> None:
    """Move microscope arm OUT (Ch8) then detector IN (Ch9) — XRD mode.

    Parameters
    ----------
    microscope_out_pos : int or None
        Ch8 target position (pulses). None uses global Positions.
    fpd_in_pos : int or None
        Ch9 target position (pulses). None uses global Positions.
    speed : str
        Movement speed. Must be "H", "M", or "L". Default "H".

    Notes
    -----
    Ch8 moves first; Ch9 moves only after Ch8 has completed.
    Omit position arguments in most cases to use calibrated presets.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Stage",
    example="fpd_out_and_microscope_in()  # switch to microscopy mode",
    factory=_factories.fpd_out_and_microscope_in,
    argument_rules={"speed": ArgumentRule(valid_values=frozenset({"H", "M", "L"}))},
)
def fpd_out_and_microscope_in(
    fpd_out_pos: int | None = None,
    microscope_in_pos: int | None = None,
    speed: str = "H",
) -> None:
    """Move detector OUT (Ch9) then microscope arm IN (Ch8) — microscopy mode.

    Parameters
    ----------
    fpd_out_pos : int or None
        Ch9 target position (pulses). None uses global Positions.
    microscope_in_pos : int or None
        Ch8 target position (pulses). None uses global Positions.
    speed : str
        Movement speed. Must be "H", "M", or "L". Default "H".

    Notes
    -----
    Ch9 moves first; Ch8 moves only after Ch9 has completed.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


# ── PACE5000 ─────────────────────────────────────────────────────────────────

@dsl_command(
    category="Pressure",
    example="""\
# Canonical pressure scan with XRD measurement
microscope_out_and_fpd_in()
for p in [1.0, 2.0, 3.0, 4.0, 5.0]:
    set_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min")
    wait_pressure(tol=0.01, unit="MPa")
    take_xrd(exposure_ms=1000, save=True, prefix="scan")
fpd_out_and_microscope_in()""",
    factory=_factories.set_pressure,
    argument_rules={
        "unit": ArgumentRule(valid_values=frozenset({"MPa", "Bar"})),
        "rate_unit": ArgumentRule(
            valid_values=frozenset({"MPa/min", "Bar/min", "MPa/sec", "Bar/sec"})
        ),
        "pressure": ArgumentRule(lower_bound=0.0, loop_var_allowed=True),
        "rate": ArgumentRule(lower_bound=0.0),
    },
)
def set_pressure(
    pressure: float,
    unit: str,
    rate: float,
    rate_unit: str,
) -> None:
    """Set the PACE5000 target pressure.

    Parameters
    ----------
    pressure : float
        Target pressure. Must be non-negative.
    unit : str
        Pressure unit. Must be "MPa" or "Bar". "GPa" is NOT supported.
    rate : float
        Slew rate (required). Must be positive.
    rate_unit : str
        Slew rate unit. Must be "MPa/min" or "Bar/min".

    Notes
    -----
    This function issues the setpoint immediately and returns.
    It does NOT wait for the pressure to stabilise.
    Call wait_pressure() afterward to block until the target is reached.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Pressure",
    example="""\
# Canonical pressure scan with XRD measurement
microscope_out_and_fpd_in()
for p in [1.0, 2.0, 3.0, 4.0, 5.0]:
    set_and_wait_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min", tol=0.01)
    take_xrd(exposure_ms=1000, save=True, prefix="scan")
fpd_out_and_microscope_in()""",
    factory=_factories.set_and_wait_pressure,
    argument_rules={
        "unit": ArgumentRule(valid_values=frozenset({"MPa", "Bar"})),
        "rate_unit": ArgumentRule(
            valid_values=frozenset({"MPa/min", "Bar/min", "MPa/sec", "Bar/sec"})
        ),
        "pressure": ArgumentRule(lower_bound=0.0, loop_var_allowed=True),
        "rate": ArgumentRule(lower_bound=0.0),
        "tol": ArgumentRule(lower_bound=0.0, lower_bound_inclusive=False),
    },
)
def set_and_wait_pressure(
    pressure: float,
    unit: str,
    rate: float,
    rate_unit: str,
    tol: float,
) -> None:
    """Set the PACE5000 target pressure and block until it's reached.

    Equivalent to calling set_pressure() immediately followed by
    wait_pressure() — use this for the common case where the two are
    always run back-to-back. `pressure` and `tol` share the same `unit`.

    Parameters
    ----------
    pressure : float
        Target pressure. Must be non-negative.
    unit : str
        Pressure unit. Must be "MPa" or "Bar". "GPa" is NOT supported.
        Also applies to `tol`.
    rate : float
        Slew rate (required). Must be positive.
    rate_unit : str
        Slew rate unit. Must be "MPa/min" or "Bar/min".
    tol : float
        Acceptable deviation from setpoint (in `unit`). Must be positive.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Pressure",
    factory=_factories.wait_pressure,
    argument_rules={
        "unit": ArgumentRule(valid_values=frozenset({"MPa", "Bar"})),
        "tol": ArgumentRule(lower_bound=0.0, lower_bound_inclusive=False),
    },
)
def wait_pressure(tol: float, unit: str) -> None:
    """Block until the current pressure is within tol of the setpoint.

    Parameters
    ----------
    tol : float
        Acceptable deviation from setpoint. Must be positive.
    unit : str
        Pressure unit. Must be "MPa" or "Bar".

    Notes
    -----
    This function polls the pressure sensor every 200 ms.
    Use after set_pressure() to ensure the target is reached before proceeding.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Pressure", factory=_factories.set_control_mode)
def set_control_mode(enabled: bool) -> None:
    """Enable or disable PACE5000 closed-loop pressure control.

    Parameters
    ----------
    enabled : bool
        True to enable control mode; False to disable.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


# ── LakeShore 335 ─────────────────────────────────────────────────────────────

@dsl_command(
    category="Temperature",
    example="""\
set_temperature(value=300.0, unit="K", ramp_rate=5.0)
wait_temperature(tol=1.0, unit="K")""",
    factory=_factories.set_temperature,
    argument_rules={
        "unit": ArgumentRule(valid_values=frozenset({"K"})),
        "ramp_rate": ArgumentRule(lower_bound=0.0),
        "value": ArgumentRule(loop_var_allowed=True),
    },
)
def set_temperature(
    value: float,
    *,
    unit: str = "K",
    ramp_rate: float,
) -> None:
    """Set the LakeShore 335 target temperature.

    Parameters
    ----------
    value : float
        Target temperature. Must be positive.
    unit : str
        Temperature unit. Only "K" is supported.
    ramp_rate : float
        Ramp rate in K/min (required). Must be positive.

    Notes
    -----
    This function issues the setpoint immediately and returns.
    It does NOT wait for the temperature to stabilise.
    Call wait_temperature() afterward if stabilisation is required before
    the next step.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Temperature",
    factory=_factories.wait_temperature,
    argument_rules={
        "unit": ArgumentRule(valid_values=frozenset({"K"})),
        "tol": ArgumentRule(lower_bound=0.0, lower_bound_inclusive=False),
        "channel": ArgumentRule(valid_values=frozenset({"A", "B"})),
    },
)
def wait_temperature(tol: float, unit: str = "K", channel: str = "B") -> None:
    """Block until the selected temperature is within tol of the setpoint.

    Parameters
    ----------
    tol : float
        Acceptable deviation from setpoint in Kelvin. Must be positive.
    unit : str
        Temperature unit. Only "K" is supported.
    channel : str
        LakeShore sensor "A" or "B". Defaults to "B" for existing scripts.

    Notes
    -----
    Polls the LakeShore sensor every 200 ms.
    Use after set_temperature() to ensure the target is reached.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Temperature",
    example='set_and_wait_temperature(value=300.0, unit="K", ramp_rate=5.0, tol=1.0, channel="B")',
    factory=_factories.set_and_wait_temperature,
    argument_rules={
        "unit": ArgumentRule(valid_values=frozenset({"K"})),
        "ramp_rate": ArgumentRule(lower_bound=0.0),
        "value": ArgumentRule(loop_var_allowed=True),
        "tol": ArgumentRule(lower_bound=0.0, lower_bound_inclusive=False),
        "channel": ArgumentRule(valid_values=frozenset({"A", "B"})),
    },
)
def set_and_wait_temperature(
    value: float, *, unit: str = "K", ramp_rate: float,
    tol: float, channel: str = "B",
) -> None:
    """Set the LakeShore 335 target and wait for the selected sensor to reach it.

    The ramp rate is verified before the setpoint is sent. Waiting ends only
    when the effective setpoint has reached the target and the sensor is
    within `tol` Kelvin. `value` may reference a loop variable.
    """
    pass


@dsl_command(category="Temperature", factory=_factories.set_heater)
def set_heater(range_index: int) -> None:
    """Set the LakeShore heater output range.

    Parameters
    ----------
    range_index : int
        Heater range. Must be one of:
        0 = Off, 1 = Low, 2 = Medium, 3 = High.

    Notes
    -----
    Call all_heaters_off() at the end of a heating sequence for safety.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Temperature", factory=_factories.all_heaters_off)
def all_heaters_off() -> None:
    """Turn off both LakeShore heater channels.

    Notes
    -----
    Always call this at the end of any sequence that uses set_heater().
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


# ── Rad-icon 2022 ─────────────────────────────────────────────────────────────

@dsl_command(
    category="Measurement",
    example='take_xrd(exposure_ms=1000, save=True)',
    factory=_factories.take_xrd,
    argument_rules={
        "osc_speed": ArgumentRule(valid_values=frozenset({"H", "M", "L"})),
    },
)
def take_xrd(
    exposure_ms: int | None = None,
    save: bool = True,
    prefix: str | None = None,
    dark_file: str | None = None,
    dark_enabled: bool | None = None,
    defect_file: str | None = None,
    defect_enabled: bool | None = None,
    defect_kernel: int | None = None,
    flip_v: bool | None = None,
    flip_h: bool | None = None,
    oscillate: bool | None = None,
    osc_pos_a_deg: float = -5.0,
    osc_pos_b_deg: float = 20.0,
    osc_dwell_ms: int = 0,
    osc_speed: str = "M",
) -> None:
    """Take an XRD frame with the Rad-icon 2022 detector.

    Parameters
    ----------
    exposure_ms : int or None
        Shutter time in milliseconds. None uses the global XRD settings.
    save : bool
        Whether to save the frame to disk, in the run's XRD folder.
        Default True.
    prefix : str or None
        File name prefix for the saved frame. None uses the global XRD setting.
    dark_file : str or None
        Per-step override for the dark-correction frame file. None uses
        the global XRD settings.
    dark_enabled : bool or None
        Per-step override for whether dark correction is applied. None
        uses the global XRD settings.
    defect_file : str or None
        Per-step override for the defect-map file. None uses the global
        XRD settings.
    defect_enabled : bool or None
        Per-step override for whether defect correction is applied. None
        uses the global XRD settings.
    defect_kernel : int or None
        Per-step override for the defect-correction interpolation kernel
        size. None uses the global XRD settings.
    flip_v : bool or None
        Per-step override for vertical image flip. None uses the global
        XRD settings.
    flip_h : bool or None
        Per-step override for horizontal image flip. None uses the global
        XRD settings.
    oscillate : bool or None
        If True, oscillate Ch11 between osc_pos_a_deg and osc_pos_b_deg
        throughout the exposure, then return Ch11 to 0° before completing.
        If False, oscillation is explicitly disabled for this step even if
        the global XRD settings have it enabled. None (default) inherits
        the global XRD settings' oscillate flag.
    osc_pos_a_deg : float
        Oscillation endpoint A in degrees. Requires oscillate=True in this
        same call — passing this without oscillate=True is a compile error.
    osc_pos_b_deg : float
        Oscillation endpoint B in degrees. Requires oscillate=True in this
        same call — passing this without oscillate=True is a compile error.
    osc_dwell_ms : int
        Dwell time at each endpoint in ms (0 = no dwell). Requires
        oscillate=True in this same call — passing this without
        oscillate=True is a compile error.
    osc_speed : str
        Ch11 oscillation speed. Must be "H", "M", or "L". Requires
        oscillate=True in this same call — passing this without
        oscillate=True is a compile error.

    Notes
    -----
    When oscillate=True, the step completes only after Ch11 has returned to 0°.
    To override only the oscillation range/dwell/speed for one step while
    still turning oscillation on, pass oscillate=True together with the
    osc_* fields you want to change — you cannot pass osc_* fields alone and
    rely on the global XRD settings' oscillate flag being on.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Measurement", factory=_factories.take_dark)
def take_dark(exposure_ms: int) -> None:
    """Take a dark (background) frame with the Rad-icon 2022 detector.

    Parameters
    ----------
    exposure_ms : int
        Shutter time in milliseconds. Must match the exposure_ms used for
        the corresponding XRD frames.

    Notes
    -----
    Acquire dark frames before the main measurement sequence.
    Dark correction is applied automatically in subsequent take_xrd() steps
    if enabled in the global XRD settings.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Ruby Fluorescence",
    example='take_spectrum(position_name="Ruby A", search_best_position_nearby=True, search_channels=(4, 5))',
    factory=_factories.take_spectrum,
)
def take_spectrum(save: bool = True, prefix: str = "spectrum",
                  position_name: str = "Default",
                  all_positions: bool = False,
                  exposure_time_s: float | None = None,
                  em_gain_max: int | None = None,
                  use_global_search: bool = False,
                  search_best_position_nearby: bool = False,
                  search_channels: tuple[int, ...] = (4, 5),
                  search_range_ch3_um: float = 10.0,
                  search_range_ch4_um: float = 10.0,
                  search_range_ch5_um: float = 10.0,
                  search_step_ch3_um: float = 2.0,
                  search_step_ch4_um: float = 2.0,
                  search_step_ch5_um: float = 2.0,
                  search_when: str = "always",
                  stop_pressure_target_gpa: float | None = None,
                  stop_pressure_tolerance_gpa: float = 0.0) -> None:
    """Acquire 1-D spectra at one or all registered ruby positions.

    The scheduler aligns the sample to the XRD reference image, moves Ch3/4/5
    by the named ruby offset, acquires with the requested exposure, and returns
    to XRD. With all_positions=True, it measures every registered position in
    name order. An omitted exposure_time_s uses Global Spectrum Settings.
    Saved spectra go to the run's spectrum folder.
    When nearby search is enabled, search_when="always" scans before the
    final spectrum; search_when="on_low_quality" first checks a fitted
    spectrum at the registered ruby position and scans only if its R² is
    below the global threshold. The scan measures every point in the
    selected Ch3/Ch4/Ch5 grid, moves to the largest integrated intensity,
    then acquires and saves the final spectrum. Search ranges are full widths
    in µm (default 10 µm, or ±5 µm); per-axis steps default to 2 µm.
    An explicit fpd_out_and_microscope_in() step is needed before this
    operation when the sequence is in XRD mode.
    If stop_pressure_target_gpa is set, a valid ruby pressure at or above
    target minus tolerance ends the sequence at the ruby position. Use this
    after wait_pressure() in a stepwise PACE5000 pressure loop. PACE5000 stays
    in Control mode with its last commanded gas-pressure setpoint.
    """
    pass


@dsl_command(
    category="PACE5000",
    example='compress_to_target_pressure_using_ruby(target_sample_pressure_gpa=1.0, gas_pressure_increment=0.1, stabilisation_duration_s=10, max_allowed_gas_pressure=5, position_name="Ruby A")',
    factory=_factories.compress_to_target_pressure_using_ruby,
    argument_rules={
        "target_sample_pressure_gpa": ArgumentRule(loop_var_allowed=True),
        "gas_pressure_unit": ArgumentRule(valid_values=frozenset({"MPa", "Bar"})),
        "gas_pressure_rate_unit": ArgumentRule(valid_values=frozenset({"MPa/min", "Bar/min"})),
    },
)
def compress_to_target_pressure_using_ruby(
    target_sample_pressure_gpa: float,
    gas_pressure_increment: float,
    stabilisation_duration_s: float,
    max_allowed_gas_pressure: float,
    position_name: str = "Default",
    gas_pressure_unit: str = "MPa",
    gas_pressure_rate: float | None = None,
    gas_pressure_rate_unit: str = "MPa/min",
    gas_pressure_tolerance: float = 0.001,
    save: bool = True,
    prefix: str = "spectrum",
    all_positions: bool = False,
    exposure_time_s: float | None = None,
    em_gain_max: int | None = None,
    use_global_search: bool = False,
    search_best_position_nearby: bool = False,
    search_channels: tuple[int, ...] = (4, 5),
    search_range_ch3_um: float = 10.0,
    search_range_ch4_um: float = 10.0,
    search_range_ch5_um: float = 10.0,
    search_step_ch3_um: float = 2.0,
    search_step_ch4_um: float = 2.0,
    search_step_ch5_um: float = 2.0,
    search_when: str = "always",
    stop_pressure_target_gpa: float | None = None,
    stop_pressure_tolerance_gpa: float = 0.0,
) -> None:
    """Raise PACE5000 gas pressure in bounded increments until one ruby reaches the target.

    Ruby acquisition uses the same settings and position options as
    take_spectrum. Pressure Calculation must be enabled globally. The
    PACE5000 must already be in Control mode. Each increment waits for the
    gas setpoint, then for the requested stabilisation duration.
    """
    pass


@dsl_command(
    category="PACE5000",
    example='decompress_to_target_pressure_using_ruby(target_sample_pressure_gpa=1.0, gas_pressure_increment=0.1, stabilisation_duration_s=10, min_allowed_gas_pressure=0.1, position_name="Ruby A")',
    factory=_factories.decompress_to_target_pressure_using_ruby,
    argument_rules={
        "target_sample_pressure_gpa": ArgumentRule(loop_var_allowed=True),
        "gas_pressure_unit": ArgumentRule(valid_values=frozenset({"MPa", "Bar"})),
        "gas_pressure_rate_unit": ArgumentRule(valid_values=frozenset({"MPa/min", "Bar/min"})),
    },
)
def decompress_to_target_pressure_using_ruby(
    target_sample_pressure_gpa: float,
    gas_pressure_increment: float,
    stabilisation_duration_s: float,
    min_allowed_gas_pressure: float,
    position_name: str = "Default",
    gas_pressure_unit: str = "MPa",
    gas_pressure_rate: float | None = None,
    gas_pressure_rate_unit: str = "MPa/min",
    gas_pressure_tolerance: float = 0.001,
    save: bool = True,
    prefix: str = "spectrum",
    all_positions: bool = False,
    exposure_time_s: float | None = None,
    em_gain_max: int | None = None,
    use_global_search: bool = False,
    search_best_position_nearby: bool = False,
    search_channels: tuple[int, ...] = (4, 5),
    search_range_ch3_um: float = 10.0,
    search_range_ch4_um: float = 10.0,
    search_range_ch5_um: float = 10.0,
    search_step_ch3_um: float = 2.0,
    search_step_ch4_um: float = 2.0,
    search_step_ch5_um: float = 2.0,
    search_when: str = "always",
    stop_pressure_target_gpa: float | None = None,
    stop_pressure_tolerance_gpa: float = 0.0,
) -> None:
    """Lower PACE5000 gas pressure in bounded steps until one ruby reaches the target."""
    pass


# ── Camera ────────────────────────────────────────────────────────────────────

@dsl_command(category="Camera", factory=_factories.save_snapshot)
def save_snapshot() -> None:
    """Capture one USB-camera frame and save it as a timestamped image.

    The image is saved to the run's photo folder; the filename is generated
    from the current timestamp.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Camera", factory=_factories.save_reference_image)
def save_reference_image(
    path: str | None = None,
    camera_index: int = 0,
) -> None:
    """Capture and save a reference frame for sample-position following.

    Parameters
    ----------
    path : str or None
        Save path for the reference image (.png or .jpg).
        None uses the default: __localdata/reference_frame.png.
    camera_index : int
        Camera device index. Default 0.

    Notes
    -----
    Call this before start_following() or follow_sample_position() when you
    want to record the initial sample position within the sequence.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Camera",
    factory=_factories.start_following,
    argument_rules={
        "interval_unit": ArgumentRule(valid_values=frozenset({"s", "min"})),
    },
)
def start_following(
    reference_path: str | None = None,
    interval: float | None = None,
    interval_unit: str = "s",
    similarity_threshold: float | None = None,
    max_correction_per_step_um: float | None = None,
    camera_index: int = 0,
    autofocus_enabled: bool = True,
    autofocus_range_um: float | None = None,
    autofocus_steps: int | None = None,
) -> None:
    """Start background sample-position following (returns immediately).

    Parameters
    ----------
    reference_path : str or None
        Path to the reference image (.png or .jpg).
        None uses the Global Follow Settings reference image.
    interval : float or None
        Correction attempt interval. None uses Global Follow Settings.
    interval_unit : str
        Time unit for interval. Must be "s" or "min".
    similarity_threshold : float or None
        Minimum template-match score (0–1) to accept a correction.
        None uses Global Follow Settings.
    max_correction_per_step_um : float or None
        Maximum XY correction per cycle in micrometres. None uses Global Follow Settings.
    camera_index : int
        Camera device index. Default 0.
    autofocus_enabled : bool
        True runs Ch3 autofocus scans before and after each XY correction cycle.
        False skips it (Ch3 is never moved). Default True.
    autofocus_range_um : float or None
        Autofocus search range in micrometres. None uses Global Follow Settings.
    autofocus_steps : int or None
        Number of autofocus search steps. None uses Global Follow Settings.

    Notes
    -----
    Returns immediately; following runs in a background thread.
    You MUST call stop_following() later to end the following thread.
    If you want fixed-duration following, use follow_sample_position() instead.
    Calling start_following() twice without stop_following() is invalid.
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Camera", factory=_factories.stop_following)
def stop_following() -> None:
    """Stop the background sample-position following thread.

    Notes
    -----
    Blocks until the following thread has fully stopped.
    Must be preceded by start_following().
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(
    category="Camera",
    example='follow_sample_position(duration=30.0, unit="min", interval=5.0, interval_unit="min")',
    factory=_factories.follow_sample_position,
    argument_rules={
        "unit": ArgumentRule(valid_values=frozenset({"s", "min"})),
        "interval_unit": ArgumentRule(valid_values=frozenset({"s", "min"})),
        "duration": ArgumentRule(lower_bound=0.0, lower_bound_inclusive=False),
    },
)
def follow_sample_position(
    duration: float,
    unit: str = "min",
    reference_path: str | None = None,
    interval: float | None = None,
    interval_unit: str = "s",
    similarity_threshold: float | None = None,
    max_correction_per_step_um: float | None = None,
    camera_index: int = 0,
    autofocus_enabled: bool = True,
    autofocus_range_um: float | None = None,
    autofocus_steps: int | None = None,
) -> None:
    """Follow sample position for a fixed duration (blocking).

    Convenience shorthand for start_following() + wait() + stop_following().

    Parameters
    ----------
    duration : float
        Total following duration. Must be positive.
    unit : str
        Time unit for duration. Must be "s" or "min".
    reference_path : str or None
        Path to the reference image. None uses the Global Follow Settings reference image.
    interval : float or None
        Correction attempt interval. None uses Global Follow Settings.
    interval_unit : str
        Time unit for interval. Must be "s" or "min".
    similarity_threshold : float or None
        Minimum template-match score. None uses Global Follow Settings.
    max_correction_per_step_um : float or None
        Maximum XY correction per cycle in micrometres. None uses Global Follow Settings.
    camera_index : int
        Camera device index. Default 0.
    autofocus_enabled : bool
        True runs Ch3 autofocus scans before and after each XY correction cycle.
        False skips it (Ch3 is never moved). Default True.
    autofocus_range_um : float or None
        Autofocus search range in micrometres. None uses Global Follow Settings.
    autofocus_steps : int or None
        Number of autofocus search steps. None uses Global Follow Settings.

    Notes
    -----
    Blocks for the full duration.  Use start_following() + stop_following()
    if you need interleaved steps (e.g., set_pressure() while following).
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass


@dsl_command(category="Camera", factory=_factories.align_to_reference)
def align_to_reference(
    reference_path: str | None = None,
    similarity_threshold: float | None = None,
    max_correction_per_step_um: float | None = None,
    camera_index: int = 0,
    autofocus_enabled: bool = True,
    autofocus_range_um: float | None = None,
    autofocus_steps: int | None = None,
) -> None:
    """Align the sample position to the reference image once (blocking).

    Performs a single following correction cycle — XY correction (with
    re-correction retries while similarity is below threshold), then an
    optional Ch3 autofocus — and proceeds to the next step.

    Parameters
    ----------
    reference_path : str or None
        Path to the reference image. None uses __localdata/reference_frame.png.
    similarity_threshold : float or None
        Minimum template-match score (0–1); XY re-correction is retried
        while similarity is below it. None uses the preset.
    max_correction_per_step_um : float or None
        Maximum XY correction per move in micrometres. None uses the preset.
    camera_index : int
        Camera device index. Default 0.
    autofocus_enabled : bool
        True runs a Ch3 autofocus scan after the XY correction.
        False skips it (Ch3 is never moved). Default True.
    autofocus_range_um : float or None
        Autofocus search range in micrometres. None uses the preset.
    autofocus_steps : int or None
        Number of autofocus search steps. None uses the preset.

    Notes
    -----
    No background thread is left running. Must not be called between
    start_following() and stop_following().
    """
    # Declaration only — dsl_command() replaces this function with a stub
    # that always raises NotImplementedError; see dsl/_registry.py.
    pass
