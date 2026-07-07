"""
DSL package for Experimental Scheduler.

ALLOWED_FUNCTIONS is the single source of truth for which function names are
valid in the DSL.  Both validator.py (whitelist check) and api.py (function
definitions) must stay in sync with this set.

DSL_VERSION is embedded in the LLM System Prompt so the model knows which
version of the DSL it is targeting.  Bump it whenever a breaking change is
made to the DSL syntax or available functions.
"""

DSL_VERSION: str = "1.0.0"

ALLOWED_FUNCTIONS: frozenset[str] = frozenset({
    # General
    "wait",
    "log_message",
    # Stage — primitive
    "move_absolute",
    "move_relative",
    "set_speed",
    "emergency_stop",
    # Stage — compound
    "microscope_out_and_fpd_in",
    "fpd_out_and_microscope_in",
    # PACE5000
    "set_pressure",
    "wait_pressure",
    "set_control_mode",
    # LakeShore 335
    "set_temperature",
    "wait_temperature",
    "set_heater",
    "all_heaters_off",
    # Keithley 2000
    "read_intensity",
    # Rad-icon 2022
    "take_xrd",
    "take_dark",
    # Camera
    "save_reference_image",
    "start_following",
    "stop_following",
    "follow_sample_position",
})
