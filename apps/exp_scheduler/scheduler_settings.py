"""
Global settings dataclasses shared by SequenceRunner and PreValidator.

These were originally defined in runner.py; PreValidator imported them from
there, which meant it also had to import runner.py's private
``_validate_ch11_oscillation_settings`` alongside them (see
REORGANISATION_PLAN.md Phase 4). Moving them here lets PreValidator depend
on a settings-only module instead of the Runner module itself.
``runner.py`` re-exports these names so existing imports keep working.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# Bump when a field is added/removed/renamed in any of the four dataclasses
# below, or when the canonical JSON shape changes. Consumed by the
# Phase 8 ValidationCertificate fingerprint (REORGANISATION_PLAN.md §5.5) —
# not read anywhere yet, but fixed by test from this Phase onward so the
# canonical representation doesn't drift silently before that's built.
# v2: removed GlobalFollowSettings.autofocus_enabled — it had no UI control
# (the Follow Settings panel hardcoded autofocus_enabled=True unconditionally
# and never read this field back), Runner never consulted it (only the
# per-step StartFollowingAction/FollowSampleAction.autofocus_enabled is
# read), and it was not persisted — a dead field whose False default
# contradicted actual (always-True-unless-overridden-per-step) behaviour.
SETTINGS_SCHEMA_VERSION = "2"


@dataclass
class GlobalXrdSettings:
    """Global defaults for TakeXrdAction.  Per-step overrides (non-None fields in
    TakeXrdAction) take precedence over these values."""
    exposure_ms: int = 1000
    save_dir: str | None = None          # None → __localdata/xrd/<run-timestamp>/
    dark_file: str | None = None
    dark_enabled: bool = False
    defect_file: str | None = None
    defect_enabled: bool = True
    defect_kernel: int = 3               # 3 / 4 / 5 / 6
    flip_v: bool = True
    flip_h: bool = False
    # Ch11 oscillation during exposure
    oscillate: bool = False
    osc_pos_a_deg: float = -5.0
    osc_pos_b_deg: float = 20.0
    osc_dwell_ms: int = 0
    osc_speed: str = "M"


@dataclass
class GlobalLimits:
    """Allowed travel (mm) from each channel's position at sequence-start.

    None means not configured — PreValidator blocks Run in that case.
    0.0 means that channel/direction is locked (no movement allowed).
    Positive value is the allowed displacement in mm.
    """
    ch3_minus_mm: float | None = None
    ch3_plus_mm:  float | None = None
    ch4_minus_mm: float | None = None
    ch4_plus_mm:  float | None = None
    ch5_minus_mm: float | None = None
    ch5_plus_mm:  float | None = None

    def is_fully_configured(self) -> bool:
        return all(v is not None for v in (
            self.ch3_minus_mm, self.ch3_plus_mm,
            self.ch4_minus_mm, self.ch4_plus_mm,
            self.ch5_minus_mm, self.ch5_plus_mm,
        ))


@dataclass
class GlobalFollowSettings:
    """Global defaults for follow-sample actions.

    Per-step overrides in StartFollowingAction / FollowSampleAction take
    precedence for fields that are also present in the action (interval_s,
    similarity_threshold, max_correction_per_step_um, autofocus_range_um,
    autofocus_steps). The fields below that have no action-level counterpart
    are always taken from this object.

    autofocus_enabled has no global counterpart (removed in
    SETTINGS_SCHEMA_VERSION 2) — the per-step
    StartFollowingAction/FollowSampleAction.autofocus_enabled field (default
    True) is the sole source of truth; the Follow Settings panel has no
    checkbox for it ("Auto-Focus (Ch3) after XY correction — always
    enabled").
    """
    reference_path: str | None = None   # set via Global Settings > Follow Settings > Reference Image
    interval_s: float = 300.0
    similarity_threshold: float = 0.95
    max_correction_ch4_um: float = 400.0
    max_correction_ch5_um: float = 400.0
    xy_max_retries: int = 3
    autofocus_range_um: float = 20.0
    autofocus_steps: int = 10
    autofocus_method: str = "laplacian"
    autofocus_n_frames: int = 1
    autofocus_speed: str = "H"
    autofocus_peak_method: str = "highest"


@dataclass
class GlobalCameraSettings:
    """Global defaults for one-shot USB camera actions."""
    snapshot_save_dir: str | None = None  # None -> __localdata/snapshots/


def canonical_settings_dict(
    global_limits: GlobalLimits | None,
    global_xrd: GlobalXrdSettings,
    global_follow: GlobalFollowSettings,
    global_camera: GlobalCameraSettings,
) -> dict:
    """Stable, field-name-keyed representation of the four Global*Settings
    objects.

    Built with dataclasses.asdict() — never repr() or object identity — so
    it is stable across process restarts and Python versions. Intended as
    the basis for the settings half of the Phase 8 ValidationCertificate
    fingerprint (REORGANISATION_PLAN.md §5.5): two calls with
    field-for-field-equal settings always produce an equal dict, and
    json.dumps(..., sort_keys=True) of it always produces the same string.
    """
    return {
        "schema": "exp_scheduler.global_settings",
        "version": SETTINGS_SCHEMA_VERSION,
        "global_limits": asdict(global_limits) if global_limits is not None else None,
        "global_xrd": asdict(global_xrd),
        "global_follow": asdict(global_follow),
        "global_camera": asdict(global_camera),
    }


def canonical_settings_json(
    global_limits: GlobalLimits | None,
    global_xrd: GlobalXrdSettings,
    global_follow: GlobalFollowSettings,
    global_camera: GlobalCameraSettings,
) -> str:
    """Sorted-key JSON rendering of canonical_settings_dict() — suitable for
    hashing into a fingerprint (Phase 8) or for a stable on-disk/log form."""
    return json.dumps(
        canonical_settings_dict(global_limits, global_xrd, global_follow, global_camera),
        sort_keys=True,
    )
