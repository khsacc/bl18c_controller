"""
align_to_reference() tests — apps/exp_scheduler.

align_to_reference is the one-shot counterpart of start_following /
follow_sample_position: a single follow correction cycle (XY correction +
optional Ch3 autofocus) run synchronously on the main run thread, after
which the sequence proceeds to the next step. These tests pin down the DSL /
JSON round trip, the runner behaviour (one cycle, no background thread,
works after an earlier stop_following()), and the validator rule that it
must not be used inside an open start_following session.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )

import cv2
import numpy as np
from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from apps.exp_scheduler.actions import (
    AlignToReferenceAction,
    StartFollowingAction,
    StopFollowingAction,
    action_from_dict,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl.parser import SequenceBuilder
from apps.exp_scheduler.dsl.validator import ASTValidator
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator

from tests.exp_scheduler_fakes import FakeStageController


class AlignToReferenceSerialisationTests(unittest.TestCase):
    def test_all_fields_round_trip_through_dsl_and_json(self):
        original = AlignToReferenceAction(
            reference_path="ref.png",
            similarity_threshold=0.7,
            max_correction_per_step_um=3.0,
            camera_index=1,
            autofocus_enabled=False,
            autofocus_range_um=10.0,
            autofocus_steps=5,
        )
        dsl_text = original.to_dsl() + "\n"
        self.assertEqual(ASTValidator().validate(dsl_text), [])
        rebuilt = SequenceBuilder().build(ast.parse(dsl_text)).actions[0]
        self.assertEqual(rebuilt, original)
        self.assertEqual(action_from_dict(original.to_dict()), original)

    def test_defaults_emit_bare_call(self):
        self.assertEqual(AlignToReferenceAction().to_dsl(), "align_to_reference()")


class AlignToReferenceRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctrl = FakeStageController()
        self.runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=self.ctrl),
            log_dir=self._tmp.name,
        )
        self.runner._logger.start(
            path="test", devices=[], sequence_dict={}, global_limits_dict={},
            log_base_dir=self._tmp.name,
        )
        self.addCleanup(self.runner._logger.stop)

        frame = np.full((4, 4, 3), 200, dtype=np.uint8)
        ref_path = Path(self._tmp.name) / "ref.png"
        cv2.imwrite(str(ref_path), frame)
        self.ref_path = str(ref_path)

        # Fake an already-open camera session with a static, non-black frame.
        self.runner._camera_cap = object()
        self.runner._camera_index = 0
        self.runner._camera_current_frame = frame

    def _align(self, action, af_calls):
        with unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_xy_shift", return_value=(50, 50)), \
             unittest.mock.patch(
                "apps.exp_scheduler.runner.compute_similarity", return_value=1.0), \
             unittest.mock.patch.object(
                SequenceRunner, "_do_follow_autofocus",
                lambda *a, **kw: af_calls.append(1)):
            self.runner._do_align_to_reference(action)

    def test_single_cycle_moves_xy_and_runs_autofocus_once(self):
        af_calls: list[int] = []
        self._align(
            AlignToReferenceAction(
                reference_path=self.ref_path, max_correction_per_step_um=1e6,
            ),
            af_calls,
        )
        self.assertGreaterEqual(self.ctrl.call_count("move_ch_relative"), 1)
        self.assertEqual(af_calls, [1])
        self.assertIsNone(self.runner._follow_thread)

    def test_autofocus_disabled_skips_ch3(self):
        af_calls: list[int] = []
        self._align(
            AlignToReferenceAction(reference_path=self.ref_path, autofocus_enabled=False),
            af_calls,
        )
        self.assertEqual(af_calls, [])

    def test_runs_after_an_earlier_stop_following(self):
        # stop_following() leaves _follow_stop_event set; the one-shot cycle
        # must not mistake that for a stop request and silently do nothing.
        self.runner._follow_stop_event.set()
        af_calls: list[int] = []
        self._align(
            AlignToReferenceAction(
                reference_path=self.ref_path, max_correction_per_step_um=1e6,
            ),
            af_calls,
        )
        self.assertGreaterEqual(self.ctrl.call_count("move_ch_relative"), 1)
        self.assertEqual(af_calls, [1])

    def test_rejected_while_follow_thread_alive(self):
        self.runner._follow_thread = types.SimpleNamespace(is_alive=lambda: True)
        with self.assertRaisesRegex(RuntimeError, "follow session is active"):
            self.runner._do_align_to_reference(
                AlignToReferenceAction(reference_path=self.ref_path)
            )
        self.assertEqual(self.ctrl.call_count("move_ch_relative"), 0)


class AlignToReferenceValidatorTests(unittest.TestCase):
    def test_inside_open_follow_session_is_an_error(self):
        sequence = Sequence(actions=[
            StartFollowingAction(),
            AlignToReferenceAction(),
            StopFollowingAction(),
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any(
            "align_to_reference called while a follow session" in e
            for e in result.errors
        ))

    def test_outside_follow_session_has_no_pairing_error(self):
        sequence = Sequence(actions=[AlignToReferenceAction()])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertFalse(any("follow session" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()
