"""Tests for the Phase 4 extraction in apps/exp_scheduler/REORGANISATION_PLAN.md:

- GlobalXrdSettings / GlobalLimits / GlobalFollowSettings / GlobalCameraSettings
  moved from runner.py to scheduler_settings.py. Phase 4 had runner.py import
  them back by name with a "re-exported for backward compatibility" comment
  + `# noqa: F401` — but runner.py itself always needed these classes too
  (e.g. `GlobalXrdSettings()` as a constructor default in
  SequenceRunner.__init__), so the noqa was stale, and the by-name import
  meant runner.py kept re-exporting them as its own attributes regardless.
  Phase 9 removed the re-export for real: runner.py now does
  `from . import scheduler_settings` and refers to
  `scheduler_settings.GlobalXrdSettings` etc. internally, so
  `runner.GlobalXrdSettings` no longer exists. Every other importer
  (apps/exp_scheduler/ui/scheduler_window.py,
  tests/test_exp_scheduler_pre_validator.py) already used
  scheduler_settings.py directly — see REORGANISATION_PLAN.md Phase 9.
- validate_ch11_oscillation_settings moved from runner.py's private
  _validate_ch11_oscillation_settings to the public safety_rules module.
- The canonical settings serializer added alongside the dataclass move.
- global_limits_for_channel / global_limit_delta_mm / exceeded_global_limit
  extracted to safety_rules.py, replacing independent copies of the same
  judgment in runner.py and validator/pre_validator.py.
"""
import inspect
import sys
import types
import unittest

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )

from apps.exp_scheduler import runner
from apps.exp_scheduler import scheduler_settings as settings
from apps.exp_scheduler.safety_rules import (
    exceeded_global_limit,
    global_limit_delta_mm,
    global_limits_for_channel,
    validate_ch11_oscillation_settings,
)
from utils.stage.control_stage import PULSE_SCALE


class ReExportIdentityTests(unittest.TestCase):
    """Phase 9 removed runner.py's by-name re-export of the settings
    dataclasses (see this module's docstring) — runner.py now imports the
    scheduler_settings module itself and refers to
    scheduler_settings.GlobalXrdSettings etc., so these names must no
    longer exist as runner.* attributes at all."""

    def test_runner_no_longer_reexports_settings_classes(self):
        for name in (
            "GlobalXrdSettings", "GlobalLimits",
            "GlobalFollowSettings", "GlobalCameraSettings",
        ):
            self.assertFalse(hasattr(runner, name), f"runner.{name} should not exist")

    def test_runner_uses_the_scheduler_settings_module_directly(self):
        self.assertIs(runner.scheduler_settings, settings)

    def test_runner_no_longer_defines_ch11_oscillation_validator(self):
        self.assertNotIn("_validate_ch11_oscillation_settings", vars(runner))


class GlobalLimitsTests(unittest.TestCase):
    def test_defaults_are_unconfigured(self):
        gl = settings.GlobalLimits()
        self.assertFalse(gl.is_fully_configured())

    def test_fully_configured_requires_all_six_fields(self):
        gl = settings.GlobalLimits(
            ch3_minus_mm=1.0, ch3_plus_mm=1.0,
            ch4_minus_mm=1.0, ch4_plus_mm=1.0,
            ch5_minus_mm=1.0, ch5_plus_mm=0.0,  # 0.0 (locked) still counts as configured
        )
        self.assertTrue(gl.is_fully_configured())


class CanonicalSettingsTests(unittest.TestCase):
    # Hardcoded (not derived from the dataclasses/module constant) so that
    # adding, removing, or renaming a field — or bumping
    # SETTINGS_SCHEMA_VERSION — fails one of these tests and forces a
    # conscious update here. A test that instead derived its expectation
    # from dataclasses.fields()/the module constant would silently track
    # any such change and could never catch a fingerprint-relevant drift.

    def test_includes_schema_and_pinned_version_literal(self):
        d = settings.canonical_settings_dict(
            None, settings.GlobalXrdSettings(), settings.GlobalFollowSettings(),
            settings.GlobalCameraSettings(),
        )
        self.assertEqual(d["schema"], "exp_scheduler.global_settings")
        self.assertEqual(d["version"], "2")
        self.assertIsNone(d["global_limits"])

    def test_global_limits_field_set_is_pinned(self):
        d = settings.canonical_settings_dict(
            settings.GlobalLimits(), settings.GlobalXrdSettings(),
            settings.GlobalFollowSettings(), settings.GlobalCameraSettings(),
        )
        self.assertEqual(
            set(d["global_limits"].keys()),
            {
                "ch3_minus_mm", "ch3_plus_mm",
                "ch4_minus_mm", "ch4_plus_mm",
                "ch5_minus_mm", "ch5_plus_mm",
            },
        )

    def test_global_xrd_field_set_is_pinned(self):
        d = settings.canonical_settings_dict(
            None, settings.GlobalXrdSettings(), settings.GlobalFollowSettings(),
            settings.GlobalCameraSettings(),
        )
        self.assertEqual(
            set(d["global_xrd"].keys()),
            {
                "exposure_ms", "save_dir", "dark_file", "dark_enabled",
                "defect_file", "defect_enabled", "defect_kernel",
                "flip_v", "flip_h",
                "oscillate", "osc_pos_a_deg", "osc_pos_b_deg",
                "osc_dwell_ms", "osc_speed",
            },
        )

    def test_global_follow_field_set_is_pinned(self):
        d = settings.canonical_settings_dict(
            None, settings.GlobalXrdSettings(), settings.GlobalFollowSettings(),
            settings.GlobalCameraSettings(),
        )
        self.assertEqual(
            set(d["global_follow"].keys()),
            {
                "reference_path", "interval_s", "similarity_threshold",
                "max_correction_ch4_um", "max_correction_ch5_um",
                "xy_max_retries",
                "autofocus_range_um", "autofocus_steps",
                "autofocus_method", "autofocus_n_frames", "autofocus_speed",
                "autofocus_peak_method",
            },
        )

    def test_global_camera_field_set_is_pinned(self):
        d = settings.canonical_settings_dict(
            None, settings.GlobalXrdSettings(), settings.GlobalFollowSettings(),
            settings.GlobalCameraSettings(),
        )
        self.assertEqual(set(d["global_camera"].keys()), {"snapshot_save_dir"})

    def test_field_values_round_trip_through_the_dict(self):
        gl = settings.GlobalLimits(ch3_minus_mm=1.5, ch3_plus_mm=2.5)
        d = settings.canonical_settings_dict(
            gl, settings.GlobalXrdSettings(exposure_ms=500),
            settings.GlobalFollowSettings(interval_s=10.0),
            settings.GlobalCameraSettings(snapshot_save_dir="/tmp/x"),
        )
        self.assertEqual(d["global_limits"]["ch3_minus_mm"], 1.5)
        self.assertEqual(d["global_xrd"]["exposure_ms"], 500)
        self.assertEqual(d["global_follow"]["interval_s"], 10.0)
        self.assertEqual(d["global_camera"]["snapshot_save_dir"], "/tmp/x")

    def test_json_is_deterministic_for_equal_values(self):
        args = (
            settings.GlobalLimits(ch3_minus_mm=1.0),
            settings.GlobalXrdSettings(),
            settings.GlobalFollowSettings(),
            settings.GlobalCameraSettings(),
        )
        first = settings.canonical_settings_json(*args)
        second = settings.canonical_settings_json(*args)
        self.assertEqual(first, second)
        # A fresh, field-for-field-equal set of objects (different instances)
        # must produce the identical string — this is the fingerprint-
        # readiness property from REORGANISATION_PLAN.md §5.5: never rely on
        # repr() or object identity.
        third = settings.canonical_settings_json(
            settings.GlobalLimits(ch3_minus_mm=1.0),
            settings.GlobalXrdSettings(),
            settings.GlobalFollowSettings(),
            settings.GlobalCameraSettings(),
        )
        self.assertEqual(first, third)

    def test_json_changes_when_a_field_changes(self):
        base = settings.canonical_settings_json(
            None, settings.GlobalXrdSettings(), settings.GlobalFollowSettings(),
            settings.GlobalCameraSettings(),
        )
        changed = settings.canonical_settings_json(
            None, settings.GlobalXrdSettings(exposure_ms=999),
            settings.GlobalFollowSettings(), settings.GlobalCameraSettings(),
        )
        self.assertNotEqual(base, changed)


class Ch11OscillationValidatorTests(unittest.TestCase):
    def test_valid_settings_return_distinct_pulse_targets(self):
        pos_a, pos_b = validate_ch11_oscillation_settings(-5.0, 20.0, 0, "M")
        self.assertEqual(pos_a, round(-5.0 / PULSE_SCALE[11]))
        self.assertEqual(pos_b, round(20.0 / PULSE_SCALE[11]))
        self.assertNotEqual(pos_a, pos_b)

    def test_rejects_non_numeric_position(self):
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings("not-a-number", 20.0, 0, "M")

    def test_rejects_non_finite_position(self):
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings(float("nan"), 20.0, 0, "M")
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings(float("inf"), 20.0, 0, "M")

    def test_rejects_negative_dwell(self):
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings(-5.0, 20.0, -1, "M")

    def test_rejects_non_integer_dwell(self):
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings(-5.0, 20.0, 1.5, "M")

    def test_rejects_bool_dwell(self):
        # bool is a subclass of int in Python; must not silently pass as 0/1 ms.
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings(-5.0, 20.0, True, "M")

    def test_rejects_invalid_speed(self):
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings(-5.0, 20.0, 0, "X")

    def test_rejects_endpoints_resolving_to_the_same_pulse(self):
        with self.assertRaises(ValueError):
            validate_ch11_oscillation_settings(0.0, PULSE_SCALE[11] / 2, 0, "M")


class GlobalLimitPureRuleTests(unittest.TestCase):
    """global_limits_for_channel / global_limit_delta_mm /
    exceeded_global_limit — was independently re-implemented in both
    runner.py (SequenceRunner._limits_for_ch / _check_global_limits_before_
    move / _check_global_limits) and validator/pre_validator.py
    (_violates_global_limits); both now delegate to these three pure
    functions (no controller/backend needed to exercise them)."""

    def test_limits_for_channel_none_when_global_limits_unset(self):
        self.assertIsNone(global_limits_for_channel(None, 3))

    def test_limits_for_channel_none_for_non_limited_channel(self):
        gl = settings.GlobalLimits(ch3_minus_mm=1.0, ch3_plus_mm=1.0)
        for ch in (1, 2, 6, 7, 8, 9, 10, 11):
            self.assertIsNone(global_limits_for_channel(gl, ch))

    def test_limits_for_channel_returns_configured_pair(self):
        gl = settings.GlobalLimits(ch4_minus_mm=2.0, ch4_plus_mm=3.0)
        self.assertEqual(global_limits_for_channel(gl, 4), (2.0, 3.0))

    def test_delta_mm_matches_pulse_to_mm_conversion(self):
        # 1000 pulses at 2.0 um/pulse (Ch4's PULSE_SCALE) = 2.0 mm
        self.assertAlmostEqual(
            global_limit_delta_mm(1000, 0, PULSE_SCALE[4]), 2.0
        )
        self.assertAlmostEqual(
            global_limit_delta_mm(0, 1000, PULSE_SCALE[4]), -2.0
        )

    def test_exceeded_global_limit_within_range_is_none(self):
        self.assertIsNone(exceeded_global_limit(0.5, 1.0, 1.0))

    def test_exceeded_global_limit_plus(self):
        self.assertEqual(exceeded_global_limit(1.5, 1.0, 1.0), "plus")

    def test_exceeded_global_limit_minus(self):
        self.assertEqual(exceeded_global_limit(-1.5, 1.0, 1.0), "minus")

    def test_exceeded_global_limit_ignores_unconfigured_direction(self):
        # plus_mm=None: no matter how large delta_mm is in the + direction,
        # only the configured (minus) direction can report a violation.
        self.assertIsNone(exceeded_global_limit(1000.0, 1.0, None))
        self.assertEqual(exceeded_global_limit(-1000.0, 1.0, None), "minus")

    def test_runner_and_pre_validator_no_longer_inline_the_delta_mm_formula(self):
        # The old duplicated formula was `(target_pos - baseline) *
        # PULSE_SCALE[ch] / 1000.0` (or `current`/`baseline_pos` variants)
        # inlined directly in both files. Both must now go through
        # global_limit_delta_mm() instead. REORGANISATION_PLAN.md Phase 6
        # moved the PreValidator side of this (_violates_global_limits) from
        # validator/pre_validator.py into validator/checks/stage.py.
        for module in (runner, __import__(
            "apps.exp_scheduler.validator.checks.stage", fromlist=["_x"]
        )):
            source = inspect.getsource(module)
            self.assertNotIn("PULSE_SCALE[ch] / 1000", source)
            self.assertIn("global_limit_delta_mm", source)
            self.assertIn("exceeded_global_limit", source)


if __name__ == "__main__":
    unittest.main()
