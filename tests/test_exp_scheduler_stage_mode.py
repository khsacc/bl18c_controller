"""Microscope / XRD stage-mode rule shared by PreValidator and SequenceRunner."""
import contextlib
import io
import sys
import types
import unittest
from unittest.mock import patch

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1,
    )

from apps.exp_scheduler.actions import (
    AlignToReferenceAction,
    FollowSampleAction,
    ForLoopAction,
    FpdOutMicroscopeInAction,
    MicroscopeOutFpdInAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeXrdAction,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import RunnerError, SequenceRunner
from apps.exp_scheduler.safety_rules import explain_stage_mode, stage_mode_from_positions
from apps.exp_scheduler.scheduler_settings import GlobalPositions
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator
from apps.interactive_camera.xray_beam import XrayBeamPosition
from tests.exp_scheduler_fakes import FakeStageController

POSITIONS = GlobalPositions(ch8_in=280049, ch8_out=0, ch9_out=-30000, ch9_in=2801)
SETTINGS = POSITIONS.to_stage_settings()
BEAM = {6: 12000, 7: 120000, 8: 280049}
MICROSCOPE = {6: 12000, 8: 280049, 9: -30000}
XRD = {6: 12000, 8: 0, 9: 2801}

UNKNOWN = "preflight.stage.mode_ordering_camera_mode_unknown"
BLOCKED = "preflight.stage.mode_ordering_camera_blocked"


class StageModeRuleTests(unittest.TestCase):
    def test_microscope_needs_exact_beam_ch6_ch8_and_fpd_out(self):
        self.assertEqual(stage_mode_from_positions(MICROSCOPE, SETTINGS, BEAM), "microscope")
        # FPD further out than Ch9 OUT is still fine.
        self.assertEqual(stage_mode_from_positions({**MICROSCOPE, 9: -40000}, SETTINGS, BEAM),
                         "microscope")
        for changed in ({6: 12001}, {8: 280048}, {9: -29999}):
            self.assertEqual(
                stage_mode_from_positions({**MICROSCOPE, **changed}, SETTINGS, BEAM), "unknown",
            )

    def test_microscope_is_never_confirmed_without_a_registered_beam(self):
        self.assertEqual(stage_mode_from_positions(MICROSCOPE, SETTINGS, None), "unknown")
        self.assertIn("registered X-ray beam", explain_stage_mode(MICROSCOPE, SETTINGS, None))

    def test_xrd_needs_exact_ch9_in_and_microscope_out(self):
        self.assertEqual(stage_mode_from_positions(XRD, SETTINGS, BEAM), "xrd")
        # Ch6 is irrelevant and Ch8 may be further out than Ch8 OUT.
        self.assertEqual(stage_mode_from_positions({**XRD, 6: 0, 8: -500}, SETTINGS, BEAM), "xrd")
        for changed in ({9: 2800}, {9: 2802}, {8: 1}):
            self.assertEqual(
                stage_mode_from_positions({**XRD, **changed}, SETTINGS, BEAM), "unknown",
            )

    def test_missing_presets_or_positions_are_unknown(self):
        self.assertEqual(stage_mode_from_positions(XRD, None, BEAM), "unknown")
        self.assertEqual(stage_mode_from_positions({8: 0, 9: 2801}, SETTINGS, BEAM), "unknown")


def _loop_sequence() -> Sequence:
    """The shape of the failed run: a loop whose first step is start_following."""
    return Sequence(actions=[
        ForLoopAction(var="gas", values=[0.1, 0.2], body=[
            StartFollowingAction(),
            StopFollowingAction(),
            MicroscopeOutFpdInAction(),
            TakeXrdAction(),
            FpdOutMicroscopeInAction(),
        ]),
    ])


class ValidatorStageModeTests(unittest.TestCase):
    def _validate(self, stage_positions, sequence=None, beam=BEAM):
        controller = FakeStageController(positions=stage_positions)
        with patch("apps.exp_scheduler.validator.snapshots.load_beam_pulses",
                   return_value=beam), contextlib.redirect_stdout(io.StringIO()):
            return PreValidator().validate(
                sequence or _loop_sequence(), DeviceContext(controller=controller),
                global_positions=POSITIONS,
            )

    @staticmethod
    def _mode_diagnostics(result, code):
        return [d for d in result.diagnostics if d.code == code]

    def test_start_following_first_in_loop_is_an_error_when_mode_is_unknown(self):
        # Ch9 near, but not at, Ch9 IN: previously "unknown" and silently passed.
        result = self._validate({6: 12000, 8: 0, 9: 500})
        found = self._mode_diagnostics(result, UNKNOWN)
        self.assertEqual(len(found), 1)
        self.assertIn("Step1", found[0].message)
        self.assertIn("Ch9=+500", found[0].message)
        self.assertFalse(result.ok)

    def test_start_following_first_in_loop_is_blocked_in_xrd_mode(self):
        result = self._validate(XRD)
        found = self._mode_diagnostics(result, BLOCKED)
        self.assertEqual(len(found), 1)
        self.assertIn("Step1", found[0].message)

    def test_microscope_start_and_loop_return_pass(self):
        result = self._validate(MICROSCOPE)
        self.assertEqual(self._mode_diagnostics(result, UNKNOWN), [])
        self.assertEqual(self._mode_diagnostics(result, BLOCKED), [])
        self.assertEqual(
            [d for d in result.diagnostics if d.code.startswith("preflight.stage.mode_ordering_xrd")],
            [],
        )

    def test_return_to_a_ch8_in_that_differs_from_the_beam_is_an_error(self):
        result = self._validate(MICROSCOPE, beam={**BEAM, 8: 280000})
        # Iteration 1 already starts off the beam position; iteration 2
        # returns to Ch8 IN = 280049, which is still not the beam's Ch8.
        found = self._mode_diagnostics(result, UNKNOWN)
        self.assertEqual(len(found), 2)
        self.assertIn("gas=0.2", found[1].loop_context)

    def test_follow_sample_and_align_to_reference_are_checked_too(self):
        sequence = Sequence(actions=[
            FollowSampleAction(duration_s=1.0), AlignToReferenceAction(),
        ])
        result = self._validate({6: 12000, 8: 0, 9: 500}, sequence)
        self.assertEqual(len(self._mode_diagnostics(result, UNKNOWN)), 2)


class RunnerStageModeTests(unittest.TestCase):
    def _runner(self, stage_positions):
        controller = FakeStageController(positions=stage_positions)
        return SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=controller),
            global_positions=POSITIONS,
        )

    def _beam(self, pulses=BEAM):
        return XrayBeamPosition(100.0, 100.0, 640, 480, dict(pulses))

    def test_camera_tracking_requires_microscope_mode(self):
        with patch("apps.exp_scheduler.runner.load_xray_beam", return_value=self._beam()):
            self._runner(MICROSCOPE)._require_microscope_mode_for_camera("start_following")
            for positions in (XRD, {**MICROSCOPE, 6: 12001}, {**MICROSCOPE, 9: 0}):
                with self.assertRaisesRegex(RunnerError, "fpd_out_and_microscope_in"):
                    self._runner(positions)._require_microscope_mode_for_camera("start_following")

    def test_camera_tracking_requires_a_registered_beam(self):
        with patch("apps.exp_scheduler.runner.load_xray_beam", return_value=None):
            with self.assertRaisesRegex(RunnerError, "registered X-ray beam"):
                self._runner(MICROSCOPE)._require_microscope_mode_for_camera("start_following")


if __name__ == "__main__":
    unittest.main()
