"""Display labels of scheduler steps (GUI run log, PDF, Slack)."""
import unittest

from apps.exp_scheduler.actions import (
    ForLoopAction,
    FpdOutMicroscopeInAction,
    LogAction,
    MicroscopeOutFpdInAction,
    SetAndWaitPressureAction,
    StartFollowingAction,
    TakeXrdAction,
    WaitAction,
)
from apps.exp_scheduler.run_plan import build_run_plan


class StepLabelTests(unittest.TestCase):
    def test_global_settings_are_not_mentioned(self):
        self.assertEqual(TakeXrdAction().describe(), "XRD")
        self.assertEqual(StartFollowingAction().describe(), "Start following + AF")
        self.assertEqual(MicroscopeOutFpdInAction().describe(), "Microscope OUT → FPD IN")
        self.assertEqual(FpdOutMicroscopeInAction().describe(), "FPD OUT → Microscope IN")

    def test_overrides_are_mentioned(self):
        self.assertEqual(TakeXrdAction(exposure_ms=500).describe(), "XRD 500 ms")
        self.assertEqual(
            StartFollowingAction(interval_s=30.0, autofocus_enabled=False).describe(),
            "Start following (interval=30 s)",
        )
        self.assertEqual(
            MicroscopeOutFpdInAction(fpd_in_pos=1200).describe(),
            "Microscope OUT → FPD IN (Ch9=1200)",
        )

    def test_loop_value_is_substituted_and_counter_kept(self):
        loop = ForLoopAction(var="p", values=[1, 2], body=[
            SetAndWaitPressureAction(pressure="p", unit="MPa", rate=1.2,
                                     rate_unit="MPa/min", tol=0.01),
            WaitAction(duration_s=10),
            LogAction(message="p={p}"),
        ])
        steps = build_run_plan([loop])
        self.assertEqual(
            steps[0].describe(),
            "Set pressure 1 MPa at 1.2 MPa/min and wait (±0.01 MPa) [1/2]",
        )
        self.assertEqual(steps[1].describe(), "Wait 10 s [1/2]")
        self.assertEqual(steps[5].describe(), "Log: p=2 [2/2]")
        # The executed action itself is left untouched.
        self.assertEqual(steps[0].action.pressure, "p")

    def test_nested_loop_counters(self):
        inner = ForLoopAction(var="j", values=[1, 2, 3], body=[WaitAction(duration_s=1)])
        steps = build_run_plan([ForLoopAction(var="i", values=[1, 2], body=[inner])])
        self.assertEqual(steps[4].describe(), "Wait 1 s [2/2, 2/3]")


if __name__ == "__main__":
    unittest.main()
