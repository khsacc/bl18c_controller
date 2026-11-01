from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import concurrent.futures
import csv
import time
from unittest import mock
import json
import tempfile
import threading
import unittest
from pathlib import Path

from apps.seq_spectro import spectro_core as core
from apps.seq_spectro.spectro_runner import SequenceRun, SimSpectroReader
from utils.stage.control_stage_sim import PM16CControllerSim

ORIGIN = {3: 30000, 4: 20000, 5: 0}


def _controller() -> PM16CControllerSim:
    ctrl = PM16CControllerSim()
    ctrl.connect()
    with ctrl._state_lock:
        for ch, value in ORIGIN.items():
            ctrl._positions[ch] = ctrl._targets[ch] = value
    return ctrl


def _positions() -> list[core.Position]:
    return [
        core.Position(core.MARKER_RUBY, 694.25, 295.0, dict(ORIGIN)),
        core.Position(core.MARKER_SM, 685.41, None, {3: 30005, 4: 20010, 5: -50}),
        core.Position(core.MARKER_RUBY, 694.30, 295.0, {3: 30000, 4: 19980, 5: 30}),
    ]


def _settings(**ruby) -> dict[str, core.MarkerSettings]:
    return {
        core.MARKER_RUBY: core.MarkerSettings(pressure_scale="ruby_shen_2020", **ruby),
        core.MARKER_SM: core.MarkerSettings(pressure_scale="sm_srb4o7_wei_2024",
                                            fit_range_enabled=True,
                                            fit_range_start_nm=684.0, fit_range_end_nm=700.0),
    }


class FakeLakeShore:
    def __init__(self, value_k=150.0):
        self.value_k = value_k

    def get_cached_readings(self):
        import time
        return {"connected": True, "temperature_b_k": self.value_k, "timestamp": time.time()}


class CoreTests(unittest.TestCase):
    def test_offsets_and_targets_follow_pos1(self):
        positions = _positions()
        offsets = core.offsets(positions)
        self.assertEqual(offsets[1], {3: 5, 4: 10, 5: -50})
        current = {3: 31000, 4: 100, 5: 7}
        self.assertEqual(core.targets(positions, current)[2], {3: 31000, 4: 80, 5: 37})

    def test_t0_only_required_with_correction(self):
        positions = _positions()
        positions[0].t0_k = None
        self.assertEqual(core.setup_errors(positions, _settings(), "/tmp"), [])
        errors = core.setup_errors(positions, _settings(temperature_correction=True,
                                                        temperature_scale="ruby_ragan_1992"), "/tmp")
        self.assertTrue(any("Pos1: enter T₀" in e for e in errors))

    def test_missing_scale_and_lambda0_range(self):
        positions = _positions()
        positions[1].lambda_0_nm = 694.0   # ruby-like value for Sm
        settings = _settings()
        settings[core.MARKER_RUBY].pressure_scale = ""
        errors = core.setup_errors(positions, settings, "/tmp")
        self.assertTrue(any("Ruby: select a pressure scale" in e for e in errors))
        self.assertTrue(any("Pos2: λ₀ of Sm2+:SrB4O7" in e for e in errors))

    def test_temperature_request(self):
        settings = _settings(temperature_correction=True, temperature_scale="ruby_ragan_1992")
        request = core.build_request(_positions()[0], settings[core.MARKER_RUBY], 80.0)
        self.assertEqual(request["sensor"], "ruby")
        self.assertEqual(request["temperature_correction"]["current_t"], 80.0)
        self.assertEqual(request["temperature_correction"]["t0"], 295.0)
        sm = core.build_request(_positions()[1], settings[core.MARKER_SM], 80.0)
        self.assertNotIn("temperature_correction", sm)

    def test_mean_propagates_errors(self):
        results = [core.PositionResult(1, "ruby", core.STATUS_VALID, pressure_gpa=5.0, pressure_err_gpa=0.3),
                   core.PositionResult(2, "ruby", core.STATUS_VALID, pressure_gpa=7.0, pressure_err_gpa=0.4),
                   core.PositionResult(3, "ruby", core.STATUS_LOW_R2, pressure_gpa=99.0, pressure_err_gpa=0.1)]
        mean = core.mean_pressure(results)
        self.assertAlmostEqual(mean.mean_gpa, 6.0)
        self.assertAlmostEqual(mean.err_gpa, 0.25)
        self.assertEqual(mean.count, 2)

    def test_evaluate_rejects_low_r2_and_swapped_doublet(self):
        pos = _positions()[0]
        settings = _settings()[core.MARKER_RUBY]
        response = SimSpectroReader().acquire_spectrum_with_pressure(
            core.build_request(pos, settings, None), fit_function="Moffat", fit_peak_count=2,
            peak_sort_order="x_desc", baseline_model="linear")
        self.assertEqual(core.evaluate(core.PositionResult(1, "ruby"), pos, settings, response).status,
                         core.STATUS_VALID)
        response["fit"]["fit"]["R2"] = 0.5
        self.assertEqual(core.evaluate(core.PositionResult(1, "ruby"), pos, settings, response).status,
                         core.STATUS_LOW_R2)
        response["fit"]["fit"]["R2"] = 0.99
        peaks = response["fit"]["fit"]["peaks"]
        peaks[0], peaks[1] = peaks[1], peaks[0]
        self.assertEqual(core.evaluate(core.PositionResult(1, "ruby"), pos, settings, response).status,
                         core.STATUS_INVALID)


class RunnerTests(unittest.TestCase):
    def _run(self, settings=None, lakeshore=None, ctrl=None, reader=None):
        ctrl = ctrl or _controller()
        out = tempfile.mkdtemp()
        reader = reader or SimSpectroReader()
        reader.exposure_time_s = 0.0
        lease = ctrl.acquire_motion(owner="test", operation="test")
        run = SequenceRun(controller=ctrl, motion=lease, reader=reader,
                          lakeshore_getter=lambda: lakeshore, positions=_positions(),
                          settings=settings or _settings(), output_dir=out,
                          abort_event=threading.Event())
        run.run()
        ctrl.release_motion(lease)
        self.addCleanup(ctrl.disconnect)
        return run, ctrl

    def test_full_run_saves_files_and_returns_to_pos1(self):
        run, ctrl = self._run()
        self.assertEqual(run.outcome, "completed", run.message)
        self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, ORIGIN)
        self.assertTrue(all(r.is_valid for r in run.results))
        averages = run.averages()
        self.assertEqual(averages["overall"].count, 3)
        self.assertEqual(set(averages["per_marker"]), {core.MARKER_RUBY, core.MARKER_SM})
        names = {p.name for p in run.run_dir.iterdir()}
        self.assertIn("master.csv", names)
        self.assertIn("summary.json", names)
        self.assertEqual(len([n for n in names if n.endswith(".npz")]), 3)
        with open(run.run_dir / "master.csv", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r["status"] for r in rows], ["valid"] * 3)
        self.assertEqual(rows[1]["fit_range_nm"], "684-700")
        summary = json.loads((run.run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["outcome"], "completed")

    def test_temperature_correction_requires_lakeshore(self):
        settings = _settings(temperature_correction=True, temperature_scale="ruby_ragan_1992")
        run, _ctrl = self._run(settings=settings, lakeshore=None)
        self.assertEqual(run.outcome, "error")
        self.assertIn("LakeShore", run.message)
        self.assertIsNone(run.run_dir)   # nothing written, stage never moved

        run, _ctrl = self._run(settings=settings, lakeshore=FakeLakeShore(150.0))
        self.assertEqual(run.outcome, "completed", run.message)
        self.assertAlmostEqual(run.results[0].temperature_k, 150.0)
        self.assertIsNotNone(run.results[0].lambda_0_at_t_nm)

    def test_soft_limit_blocks_before_motion(self):
        ctrl = _controller()
        with ctrl._state_lock:
            for ch in (3, 4, 5):
                ctrl._positions[ch] = ctrl._targets[ch] = 0
        run, ctrl = self._run(ctrl=ctrl)
        self.assertEqual(run.outcome, "error")
        self.assertIn("soft limit", run.message)
        self.assertEqual(int(ctrl.get_ch_pos(4)), 0)


    def test_em_gain_is_set_per_marker_and_recorded(self):
        settings = _settings()
        settings[core.MARKER_RUBY].em_gain_enabled = True
        settings[core.MARKER_RUBY].em_gain = 50
        settings[core.MARKER_SM].em_gain_enabled = True
        settings[core.MARKER_SM].em_gain = 200
        reader = SimSpectroReader()
        calls = []
        original = reader.set_em_gain
        reader.set_em_gain = lambda gain: calls.append(gain) or original(gain)
        run, _ctrl = self._run(settings=settings, reader=reader)
        self.assertEqual(run.outcome, "completed", run.message)
        # ruby → Sm → ruby: one PUT per change, none when already set.
        self.assertEqual(calls, [50, 200, 50])
        self.assertEqual([r.em_gain for r in run.results], [50, 200, 50])
        with open(run.run_dir / "master.csv", encoding="utf-8-sig") as handle:
            self.assertEqual([r["em_gain"] for r in csv.DictReader(handle)], ["50", "200", "50"])

    def test_em_gain_outside_camera_range_blocks_before_motion(self):
        settings = _settings()
        settings[core.MARKER_SM].em_gain_enabled = True
        settings[core.MARKER_SM].em_gain = 500          # sim camera maximum is 300
        run, ctrl = self._run(settings=settings)
        self.assertEqual(run.outcome, "error")
        self.assertIn("outside the camera range", run.message)
        self.assertIsNone(run.run_dir)
        self.assertEqual({ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}, ORIGIN)

    def test_em_gain_untouched_when_not_enabled(self):
        reader = SimSpectroReader()
        reader.em_gain = 7
        reader.set_em_gain = mock.Mock(side_effect=AssertionError("must not be called"))
        run, _ctrl = self._run(reader=reader)
        self.assertEqual(run.outcome, "completed", run.message)
        self.assertEqual([r.em_gain for r in run.results], [7, 7, 7])

    def test_camera_without_em_gain_needs_it_unticked(self):
        info = {"exists": False, "available": False, "gain": None}
        positions, settings = _positions(), _settings()
        self.assertEqual(core.em_gain_errors(info, positions, settings), [])
        settings[core.MARKER_RUBY].em_gain_enabled = True
        self.assertTrue(core.em_gain_errors(info, positions, settings))

    def test_low_r2_raises_em_gain_then_accumulations_and_saves_only_the_pass(self):
        settings = _settings()
        ruby = settings[core.MARKER_RUBY]
        ruby.em_gain_enabled, ruby.em_gain, ruby.em_gain_max = True, 2, 4
        ruby.max_accumulations = 3
        reader = SimSpectroReader()
        # Pos1 (ruby): low, low, low, pass → gain 2→4, then accumulations 1→2→3.
        reader.script = [{"R2": 0.5}, {"R2": 0.5}, {"R2": 0.5}, {}]
        run, _ctrl = self._run(settings=settings, reader=reader)
        self.assertEqual(run.outcome, "completed", run.message)
        first = run.results[0]
        self.assertEqual((first.attempts, first.em_gain, first.accumulations), (4, 4, 3))
        self.assertTrue(first.is_valid and first.saved)
        # Pos3 (ruby) starts again from its own conditions.
        self.assertEqual((run.results[2].em_gain, run.results[2].accumulations), (2, 1))
        npz = [p.name for p in run.run_dir.iterdir() if p.suffix == ".npz"]
        self.assertEqual(len(npz), 3)                 # one file per position, passes only

    def test_saturation_halves_exposure(self):
        settings = _settings()
        settings[core.MARKER_RUBY].exposure_time_s = 0.4
        reader = SimSpectroReader()
        reader.script = [{"saturated": True}, {"saturated": True}, {}]
        run, _ctrl = self._run(settings=settings, reader=reader)
        self.assertEqual(run.results[0].exposure_s, 0.1)
        self.assertEqual(run.results[0].attempts, 3)
        self.assertTrue(run.results[0].is_valid)

    def test_spectrum_below_criterion_is_not_saved(self):
        settings = _settings()
        settings[core.MARKER_SM].max_accumulations = 2
        settings[core.MARKER_SM].em_gain_max = 2
        reader = SimSpectroReader()
        reader.script = [{}, {"R2": 0.5}, {"R2": 0.5}, {"R2": 0.5}]  # Sm: gain 1→2, acc 2, give up
        run, _ctrl = self._run(settings=settings, reader=reader)
        self.assertEqual(run.outcome, "completed", run.message)
        sm = run.results[1]
        self.assertEqual(sm.status, core.STATUS_LOW_R2)
        self.assertFalse(sm.saved)
        self.assertEqual(sm.spectrum_file, "")
        names = [p.name for p in run.run_dir.iterdir()]
        self.assertFalse(any(n.startswith("pos02_") for n in names))
        self.assertEqual(run.averages()["overall"].count, 2)
        with open(run.run_dir / "master.csv", encoding="utf-8-sig") as handle:
            row = list(csv.DictReader(handle))[1]
        self.assertEqual((row["status"], row["spectrum_file"], row["attempts"]), ("low_r2", "", "3"))

    def test_no_retry_when_optimisation_is_off(self):
        settings = _settings()
        settings[core.MARKER_RUBY].optimise = False
        reader = SimSpectroReader()
        reader.script = [{"R2": 0.5}]
        run, _ctrl = self._run(settings=settings, reader=reader)
        self.assertEqual((run.results[0].attempts, run.results[0].status), (1, core.STATUS_LOW_R2))
        self.assertFalse(run.results[0].saved)

    def test_master_file_failure_is_reported_not_raised(self):
        with mock.patch.object(SequenceRun, "_write_master_files",
                               side_effect=OSError("No space left on device")):
            run, _ctrl = self._run()
        self.assertEqual(run.outcome, "completed", run.message)
        self.assertIn("No space left on device", run.save_error)


class WindowTests(unittest.TestCase):
    """Offscreen checks of the window's run/stop/close behaviour."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from apps.seq_spectro import seq_spectro_app as app_module
        self.m = app_module
        local = Path(tempfile.mkdtemp())
        for name, value in (("_LOCALDATA_DIR", local), ("_LAST_SETUP_FILE", local / "s.json"),
                            ("_LAST_DIRS_FILE", local / "d.json")):
            patcher = mock.patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        from PyQt6.QtWidgets import QMessageBox
        self.QMessageBox = QMessageBox
        for name, value in (("question", QMessageBox.StandardButton.Yes),
                            ("critical", None), ("warning", None)):
            patcher = mock.patch.object(QMessageBox, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.win = app_module.SeqSpectroWindow()
        self.addCleanup(self._force_close)
        ctrl = self.win.controller
        with ctrl._state_lock:
            for ch, value in ORIGIN.items():
                ctrl._positions[ch] = ctrl._targets[ch] = value

    def _force_close(self):
        self.win._abort_event.set()
        if self.win._worker_thread is not None:
            self.win._worker_thread.join(timeout=10)
        self._pump(lambda: True, 0.2)
        self.win._state = "IDLE"
        self.win._stop_pending = False
        self.win._stop_unconfirmed = False
        self.win.close()

    def _pump(self, condition, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def _ready_two_positions(self):
        win, m = self.win, self.m
        win._on_record(0)
        win._on_add()
        with win.controller._state_lock:
            win.controller._positions[4] += 50
        win._on_record(1)
        win._table.cellWidget(0, m.COL_LAM0).setText("694.25")
        win._table.cellWidget(1, m.COL_LAM0).setText("694.30")
        win._marker_boxes["ruby"].pressure_scale.setCurrentIndex(1)
        win._dir_edit.setText(tempfile.mkdtemp())
        with win.controller._state_lock:
            win.controller._positions[4] -= 50

    def test_editing_lambda0_clears_stale_results(self):
        self._ready_two_positions()
        self.win._on_start()
        self.assertTrue(self._pump(lambda: self.win._state == "IDLE"))
        self.assertEqual(len(self.win._results), 2)
        self.assertNotEqual(self.win._table.item(0, self.m.COL_P).text(), "")
        self.win._table.cellWidget(0, self.m.COL_LAM0).setText("694.40")
        self.assertEqual(self.win._results, {})
        self.assertEqual(self.win._table.item(0, self.m.COL_P).text(), "")
        self.assertEqual(self.win._mean_label.text(), "—")

    def test_emergency_stop_send_failure_is_shown(self):
        with mock.patch.object(self.win.controller, "request_emergency_stop",
                               side_effect=RuntimeError("socket closed")):
            self.win._on_emergency_stop()
        self.assertIn("FAILED", self.win._status_label.text())
        self.assertFalse(self.win._stop_pending)
        self.assertTrue(self.win.is_busy)          # the stage may still be moving
        self.assertTrue(self.win._estop_btn.isEnabled())
        self.critical.assert_called_once()

    def test_failed_stop_send_cancels_pending_close(self):
        self._ready_two_positions()
        self.win._marker_boxes["ruby"].exposure.setValue(1.0)
        self.win.show()
        self.win._on_start()
        with mock.patch.object(self.win.controller, "request_normal_stop",
                               side_effect=RuntimeError("socket closed")):
            self.win.close()
        self.assertFalse(self.win._close_requested)
        self.assertTrue(self._pump(lambda: self.win._state == "FINAL_CHOICE", 20.0))
        self._pump(lambda: False, 0.3)               # let any queued close() run
        self.assertTrue(self.win.isVisible())
        self.assertTrue(self.win.is_busy)

    def test_unconfirmed_emergency_stop_is_shown(self):
        future = concurrent.futures.Future()
        future.set_exception(RuntimeError("motors still moving"))
        with mock.patch.object(self.win.controller, "request_emergency_stop", return_value=future):
            self.win._on_emergency_stop()
        self.assertTrue(self.win.is_busy)
        self.assertTrue(self._pump(lambda: not self.win._stop_pending, 5.0))
        self.assertIn("NOT confirmed", self.win._status_label.text())
        self.critical.assert_called_once()
        # The launcher guard must hold even once the run itself has ended.
        self.win._set_state("FINAL_CHOICE")
        self.assertTrue(self.win.is_busy)
        self.assertFalse(self.win._btn_start.isEnabled())
        self.assertTrue(self.win._estop_btn.isEnabled())
        # Closing needs an explicit "stage checked" answer.
        self.win.show()
        self.question.return_value = self.QMessageBox.StandardButton.No
        self.win.close()
        self.assertTrue(self.win.isVisible())
        # A retried stop that is confirmed clears the state.
        ok = concurrent.futures.Future()
        ok.set_result(True)
        with mock.patch.object(self.win.controller, "request_emergency_stop", return_value=ok):
            self.win._on_emergency_stop()
        self.assertTrue(self._pump(lambda: not self.win.is_busy, 5.0))

    def test_close_during_run_does_not_block_and_closes_after_stop(self):
        self._ready_two_positions()
        self.win._marker_boxes["ruby"].exposure.setValue(1.0)   # sim sleeps ~1 s
        self.win.show()
        self.win._on_start()
        self.assertTrue(self.win.is_busy)
        started = time.time()
        self.win.close()
        self.assertLess(time.time() - started, 1.0)
        self.assertTrue(self.win.isVisible())
        self.assertTrue(self._pump(lambda: not self.win.isVisible(), 20.0))
        self.assertFalse(self.win.is_busy)


if __name__ == "__main__":
    unittest.main()
