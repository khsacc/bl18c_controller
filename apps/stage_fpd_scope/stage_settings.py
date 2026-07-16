"""Shared stage_settings.json path, defaults, and load/save helpers.

Single source of truth for the FPD/microscope shortcut positions (Ch8/Ch9
target pulses) and the Ch6/Ch7 microscope-position presets, used by
fpd_scope_stg_controller_ui.py (GUI shortcuts/settings), exp_scheduler's
runner.py and step_editor.py (MicroscopeOutFpdInAction/FpdOutMicroscopeInAction
defaults), and pre_validator.py (which points its own strict, non-fallback
check at SETTINGS_FILE rather than re-deriving the path).
"""
import json
from pathlib import Path

SETTINGS_DIR = Path(__file__).parent / "__localdata"
SETTINGS_FILE = SETTINGS_DIR / "stage_settings.json"

DEFAULT_SETTINGS = {
    "det_out": "-40000",
    "det_in":  "1779",
    "ch6":     "12000",
    "ch7":     "120000",
    "ch8_out": "0",
    "ch8_in":  "281092",
}


def load_stage_settings() -> dict:
    """Read SETTINGS_FILE merged over DEFAULT_SETTINGS.

    Always returns a dict with all DEFAULT_SETTINGS keys present — a missing
    file, unparsable JSON, or a partially-written file all fall back to
    defaults for the affected keys rather than raising.
    """
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {**DEFAULT_SETTINGS, **data}


def save_stage_settings(data: dict) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
