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

# Bump when a field is added/removed/renamed in any Global settings dataclass
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
# v4: named Ch3/Ch4/Ch5 ruby offsets and the Ch3 reference coordinates.
# v5: global ruby-fluorescence exposure time.
# v6: scheduler-wide Ch8/Ch9 shortcut positions.
# v7: XRD file prefix can be inherited from GlobalXrdSettings.
# v8: optional X-ray shutter setting.
# v9: optional shared Rad-icon Instant 1D reduction and export settings.
# v10: ruby spectrum quality uses R² only.
# v11: optional automatic ruby-spectrum EM-gain ceiling.
# v12: selectable XRD filename suffixes.
# v13: optional ruby pressure calculation (scale keys, temperature correction
#      and per-ruby zero-pressure references).
# v14: optional PACE5000 gas-pressure and LakeShore 335 setpoint / Ch B
#      temperature XRD filename suffixes.
# v15: XRD / spectrum / snapshot save directories removed; every file goes
#      under the run directory (GlobalCameraSettings is now empty).
# v16: optional beta gasket-outline tracking settings.
# v17: configurable number of beta gasket detections to average.
# v18: beta gasket tracking moves a marked sample centre onto the shared
#      X-ray beam pixel; ruby offsets record their origin (XRD position or
#      gasket centre on the beam).
# v19: selectable ruby-spectrum fit baseline model.
# v20: calibrated gasket tolerance in µm; retain the old pixel field for
#      previously saved settings.
# v21: optional common scaling of the gasket ellipse during tracking.
SETTINGS_SCHEMA_VERSION = "21"

# Origins of GlobalSpectrumSettings.ruby_positions Ch4/Ch5 offsets.
RUBY_ORIGIN_XRD_POSITION = "xrd_position"      # image-similarity tracking
RUBY_ORIGIN_GASKET_CENTRE = "gasket_centre"    # gasket centre on the X-ray beam


def ruby_origin_for(tracking_method: str) -> str:
    return (RUBY_ORIGIN_GASKET_CENTRE if tracking_method == "gasket_beta"
            else RUBY_ORIGIN_XRD_POSITION)

# XRD filename suffixes, in the order they are appended after the prefix.
# Same choices as the Rad-icon 2022 app, except that several may be combined
# and the index is always zero-padded to four digits. The instrument suffixes
# (PACE5000 gas pressure, LakeShore 335 setpoint and Ch B temperature) come
# first and are only written while that instrument is connected.
XRD_FILENAME_SUFFIXES = (
    "pressure_gas", "temperature_set", "temperature_b",
    "datetime_full", "datetime_time", "index",
)


@dataclass
class GlobalShutterSettings:
    only_open_during_xrd: bool = False


@dataclass
class GlobalXrdSettings:
    """Global defaults for TakeXrdAction.  Per-step overrides (non-None fields in
    TakeXrdAction) take precedence over these values."""
    exposure_ms: int = 1000
    dark_file: str | None = None
    dark_enabled: bool = False
    defect_file: str | None = None
    defect_enabled: bool = True
    defect_mode: str = "median"         # median / neg1; enabled=False skips correction
    defect_kernel: int = 3               # 3 / 4 / 5 / 6
    flip_v: bool = True
    flip_h: bool = False
    # Ch11 oscillation during exposure
    oscillate: bool = False
    osc_pos_a_deg: float = -5.0
    osc_pos_b_deg: float = 20.0
    osc_dwell_ms: int = 0
    osc_speed: str = "M"
    prefix: str = "scan"
    filename_suffixes: tuple[str, ...] = ("datetime_full",)
    instant1d_enabled: bool = False
    instant1d_bin_width_deg: float = 0.005
    instant1d_autosave: bool = False
    instant1d_fmt_csv: bool = False
    instant1d_fmt_tsv: bool = False
    instant1d_fmt_gsas: bool = False
    instant1d_fmt_igor: bool = False
    instant1d_units: tuple[str, ...] = ("2theta",)
    instant1d_send_pdi: bool = False
    instant1d_transport: str = "clipboard"


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
class GlobalPositions:
    """Pulse targets shared by the two FPD/microscope mode switches."""
    ch8_in: int = 281092
    ch8_out: int = 0
    ch9_out: int = -40000
    ch9_in: int = 1779

    @classmethod
    def from_stage_settings(cls) -> "GlobalPositions":
        from apps.stage_fpd_scope.stage_settings import load_stage_settings

        settings = load_stage_settings()
        return cls(
            ch8_in=int(settings["ch8_in"]),
            ch8_out=int(settings["ch8_out"]),
            ch9_out=int(settings["det_out"]),
            ch9_in=int(settings["det_in"]),
        )

    def to_stage_settings(self) -> dict[str, int]:
        return {
            "ch8_in": self.ch8_in, "ch8_out": self.ch8_out,
            "det_out": self.ch9_out, "det_in": self.ch9_in,
        }


@dataclass
class GlobalFollowSettings:
    """Global defaults for following and shared XRD image alignment.

    Per-step overrides in StartFollowingAction / FollowSampleAction take
    precedence for fields that are also present in the action (interval_s,
    similarity_threshold, max_correction_per_step_um, autofocus_range_um,
    autofocus_steps). The fields below that have no action-level counterpart
    are always taken from this object.

    autofocus_enabled has no global counterpart (removed in
    SETTINGS_SCHEMA_VERSION 2) — the per-step
    StartFollowingAction/FollowSampleAction.autofocus_enabled field (default
    True) is the sole source of truth; the Follow Settings panel has no
    checkbox for it ("Auto-Focus (Ch3) before and after XY correction").
    """
    reference_path: str | None = None   # set via Global Settings > Sample Positions > Reference Image
    # These position-tracking controls appear in Sample Positions. Keep their
    # existing global_follow JSON keys so saved sequences and preferences load.
    tracking_method: str = "similarity"  # similarity | gasket_beta
    gasket_ellipse: dict[str, float] | None = None
    # {"x", "y"} in reference-image pixels, tied to gasket_reference_digest.
    # Beta alignment moves this point onto the shared X-ray beam pixel.
    gasket_sample_point: dict[str, float] | None = None
    gasket_reference_digest: str | None = None
    gasket_tolerance_px: float | None = 8.0  # legacy saved settings only
    gasket_tolerance_um: float | None = None  # calibrated Ch4/Ch5 distance
    gasket_average_frames: int = 5
    gasket_allow_uniform_scale: bool = False
    interval_s: float = 60.0
    similarity_threshold: float = 0.95
    max_correction_ch4_um: float = 400.0
    max_correction_ch5_um: float = 400.0
    xy_max_retries: int = 3
    autofocus_range_um: float = 30.0
    autofocus_steps: int = 16
    autofocus_method: str = "tenengrad"
    autofocus_n_frames: int = 10
    autofocus_speed: str = "H"
    autofocus_peak_method: str = "highest"


@dataclass
class GlobalCameraSettings:
    """Global defaults for one-shot USB camera actions.

    Currently empty: snapshots are always saved to <run directory>/photo/.
    Kept so the settings plumbing has a place for future camera options."""


@dataclass
class GlobalSpectrumSettings:
    """FluoRaPressee connection and named Ch3/Ch4/Ch5 ruby offsets.

    The connection fields are populated from the app-wide Online spectrometer
    settings. ``api_key`` is deliberately omitted from sequence files,
    metadata and the canonical settings representation.
    """

    base_url: str = "http://192.168.1.102:8765"
    api_key: str = ""
    offset_ch4_pulse: int | None = None
    offset_ch5_pulse: int | None = None
    offset_ch3_pulse: int | None = None
    ruby_positions: dict[str, dict[str, int]] | None = None
    # Ch4/Ch5 of ruby_positions are relative to the XRD position (similarity
    # tracking) or to the stage position that puts the gasket centre on the
    # X-ray beam (gasket beta). Ch3 is always relative to the XRD Ch3.
    ruby_offset_origin: str = RUBY_ORIGIN_XRD_POSITION
    xrd_reference_ch3_pulse: int | None = None
    xrd_reference_ch4_pulse: int | None = None
    xrd_reference_ch5_pulse: int | None = None
    spectrum_reference_ch3_pulse: int | None = None
    spectrum_reference_ch4_pulse: int | None = None
    spectrum_reference_ch5_pulse: int | None = None
    exposure_time_s: float = 0.1
    r_squared_min: float = 0.95
    em_gain_max: int = 10
    max_accumulations: int = 4
    fit_function: str = "Moffat"
    baseline_model: str = "linear"
    speed: str = "M"
    settle_ms: int = 100
    timeout_s: float = 120.0
    # Pressure calculation by FRP POST /acquire/pressure. No scale is chosen
    # implicitly: an empty key blocks a pressure-enabled run in validation.
    pressure_enabled: bool = False
    pressure_scale: str = ""
    temperature_correction_enabled: bool = False
    temperature_scale: str = ""
    temperature_source: str = "lakeshore_b"     # "lakeshore_b" | "manual"
    manual_temperature_k: float | None = None
    temperature_max_age_s: float = 10.0
    # name -> {"lambda_0_nm": float, "t0_k": float | None}; keyed by the same
    # names as ruby_positions, with no fallback between rubies.
    ruby_references: dict[str, dict[str, float | None]] | None = None

    def position_names(self) -> tuple[str, ...]:
        """Return every registered ruby position, including a legacy Default."""
        names = set(self.ruby_positions or {})
        if self.offset_ch4_pulse is not None and self.offset_ch5_pulse is not None:
            names.add("Default")
        return tuple(sorted(names))

    def offset_for(self, name: str = "Default") -> dict[int, int] | None:
        """Resolve a named ruby position, including old single-position files."""
        if self.ruby_positions and name in self.ruby_positions:
            values = self.ruby_positions[name]
            if isinstance(values, dict) and all(
                isinstance(values.get(f"ch{ch}"), int)
                and not isinstance(values[f"ch{ch}"], bool) for ch in (3, 4, 5)
            ):
                return {ch: values[f"ch{ch}"] for ch in (3, 4, 5)}
        if name == "Default" and self.offset_ch4_pulse is not None and self.offset_ch5_pulse is not None:
            return {3: int(self.offset_ch3_pulse or 0),
                    4: int(self.offset_ch4_pulse), 5: int(self.offset_ch5_pulse)}
        return None


def canonical_settings_dict(
    global_limits: GlobalLimits | None,
    global_xrd: GlobalXrdSettings,
    global_follow: GlobalFollowSettings,
    global_camera: GlobalCameraSettings,
    global_spectrum: GlobalSpectrumSettings | None = None,
    global_positions: GlobalPositions | None = None,
    global_shutter: GlobalShutterSettings | None = None,
) -> dict:
    """Stable, field-name-keyed representation of the Global*Settings
    objects.

    Built with dataclasses.asdict() — never repr() or object identity — so
    it is stable across process restarts and Python versions. Intended as
    the basis for the settings half of the Phase 8 ValidationCertificate
    fingerprint (REORGANISATION_PLAN.md §5.5): two calls with
    field-for-field-equal settings always produce an equal dict, and
    json.dumps(..., sort_keys=True) of it always produces the same string.
    """
    spectrum = global_spectrum or GlobalSpectrumSettings()
    spectrum_dict = asdict(spectrum)
    spectrum_dict.pop("api_key", None)
    spectrum_dict["api_key_configured"] = bool(spectrum.api_key.strip())
    return {
        "schema": "exp_scheduler.global_settings",
        "version": SETTINGS_SCHEMA_VERSION,
        "global_limits": asdict(global_limits) if global_limits is not None else None,
        "global_positions": asdict(global_positions) if global_positions is not None else None,
        "global_xrd": asdict(global_xrd),
        "global_follow": asdict(global_follow),
        "global_camera": asdict(global_camera),
        "global_spectrum": spectrum_dict,
        "global_shutter": asdict(global_shutter or GlobalShutterSettings()),
    }


def canonical_settings_json(
    global_limits: GlobalLimits | None,
    global_xrd: GlobalXrdSettings,
    global_follow: GlobalFollowSettings,
    global_camera: GlobalCameraSettings,
    global_spectrum: GlobalSpectrumSettings | None = None,
    global_positions: GlobalPositions | None = None,
    global_shutter: GlobalShutterSettings | None = None,
) -> str:
    """Sorted-key JSON rendering of canonical_settings_dict() — suitable for
    hashing into a fingerprint (Phase 8) or for a stable on-disk/log form."""
    return json.dumps(
        canonical_settings_dict(
            global_limits, global_xrd, global_follow, global_camera,
            global_spectrum, global_positions, global_shutter,
        ),
        sort_keys=True,
    )
