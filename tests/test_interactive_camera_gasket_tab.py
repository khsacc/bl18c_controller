"""Interactive Camera's Sample Tracking (Gasket Detection) tab, hardware-free.

The simulated microscope image moves with Ch4/Ch5 (1 px per pulse), so the
tab's shared gasket follower must move the marked sample onto the beam.
"""
import csv
import json
import time
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
import pytest
from PyQt6 import QtWidgets

from apps.interactive_camera import gasket_follow, gasket_setup, gasket_tracking_tab
from apps.interactive_camera.gasket_tracking import GasketEllipse, reference_digest
from apps.interactive_camera.interactive_camera import MainWindow
from apps.interactive_camera.xray_beam import XrayBeamPosition

REF_CENTRE = (315, 230)
BEAM = (325, 236)
ORIGIN = {3: 0, 4: 1000, 5: 2000, 6: 0, 7: 0, 8: 0}


def gasket_image(cx, cy):
    rng = np.random.default_rng(1)
    yy, xx = np.indices((480, 640))
    hole = ((xx - cx) / 190) ** 2 + ((yy - cy) / 175) ** 2 <= 1
    signal = np.where(hole, 130, 105) + rng.normal(0, 5, hole.shape)
    return cv2.cvtColor(np.uint8(np.clip(signal, 0, 255)), cv2.COLOR_GRAY2BGR)


class _Stage:
    def __init__(self):
        self.pos = dict(ORIGIN)
        self.coordinator = self
        self.released = []
        self.moves = []
        self.calls = []      # ordered: move / wait / loc / release
        self.reject = set()  # channels whose relative move is refused
        self.wait_failures = 0  # next N stop confirmations raise

    def acquire_motion(self, **kwargs):
        return "lease"

    def release_motion(self, lease):
        self.calls.append(("release",))
        self.released.append(lease)

    def is_valid(self, lease):
        return True

    def switch_to_rem(self, **kwargs):
        pass

    def switch_to_loc(self, **kwargs):
        self.calls.append(("loc",))

    def set_ch_speed(self, *args, **kwargs):
        pass

    def get_is_moving(self):
        return False

    def get_ch_pos(self, ch):
        return self.pos[ch]

    def move_ch_relative(self, ch, diff, **kwargs):
        if ch in self.reject:
            raise ValueError(f"Ch{ch} move rejected")
        self.calls.append(("move", ch, diff))
        self.moves.append((ch, diff))
        self.pos[ch] += diff

    def move_ch_absolute(self, ch, value, **kwargs):
        self.pos[ch] = value

    def wait_until_stop(self, *args, **kwargs):
        if self.wait_failures:
            self.wait_failures -= 1
            self.calls.append(("wait_failed",))
            raise RuntimeError("STQ? timed out")
        self.calls.append(("wait",))

    def normal_stop(self, **kwargs):
        self.calls.append(("normal_stop",))


class _Session:
    frame_size = (640, 480)
    fps = 30.0
    error = None

    def __init__(self, stage):
        self.stage = stage
        self.blank = False

    def _frame(self):
        if self.blank:
            return np.zeros((480, 640, 3), np.uint8)
        return gasket_image(REF_CENTRE[0] + self.stage.pos[4] - ORIGIN[4],
                            REF_CENTRE[1] + self.stage.pos[5] - ORIGIN[5])

    def frame_after(self, *_args, **_kwargs):
        return self._frame()

    def latest(self):
        return self._frame(), time.monotonic()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reference = tmp_path / "reference.png"
    cv2.imwrite(str(reference), gasket_image(*REF_CENTRE))
    state = {
        "reference_path": str(reference),
        "gasket_ellipse": GasketEllipse(*REF_CENTRE, 190, 175).to_dict(),
        "gasket_sample_point": {"x": REF_CENTRE[0], "y": REF_CENTRE[1]},
        "gasket_sample_mode": "gasket_centre",
        "gasket_reference_digest": reference_digest(reference),
        "ruby_positions": {"Ruby A": {"ch3": 0, "ch4": 5, "ch5": -7}},
        "ruby_offset_origin": "gasket_centre",
        "settings": {"autofocus": False, "log_dir": str(tmp_path)},
    }
    state_path = tmp_path / "gasket_tracking.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(gasket_setup, "STATE_PATH", state_path)
    beam = XrayBeamPosition(*BEAM, 640, 480, {6: 0, 7: 0, 8: 0})
    monkeypatch.setattr(gasket_follow, "load_xray_beam", lambda: beam)
    monkeypatch.setattr(gasket_tracking_tab.XrayBeamFileCache, "get", lambda _self: beam)
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda _parent, title, text: warnings.append((title, text)))
    stage = _Stage()
    session = _Session(stage)
    window = MainWindow(controller=stage, camera_session=session)
    window.timer.stop()
    window.calibration_data = {"matrix_inv": [[1.0, 0.0], [0.0, 1.0]]}
    tab = window.gasket_tab
    tab._render_timer.stop()
    yield SimpleNamespace(app=app, window=window, tab=tab, stage=stage, session=session,
                          warnings=warnings, state_path=state_path, tmp_path=tmp_path)
    tab.stop_following()
    window.close()


def _run_attempt(s):
    s.tab.follow_thread.join(timeout=30)
    assert not s.tab.follow_thread.is_alive()
    s.app.processEvents()


def test_tabs_are_named_and_ordered(setup):
    tabs = setup.window.tab_widget
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "Interactive Camera",
        "Sample Tracking (Gasket Detection)",
        "Sample Tracking (Similarity)",
    ]
    assert "Ruby A" in setup.tab.setup_summary.text()
    assert setup.tab.btn_start.isEnabled()


def test_tracking_moves_marked_sample_onto_beam_and_logs(setup):
    tab, stage = setup.tab, setup.stage
    tab.start_following()
    assert not setup.warnings
    assert tab.is_following
    assert not setup.window.tab_widget.isTabEnabled(0)
    _run_attempt(setup)

    # The rim centre (= sample) must now sit on the beam: Δ = beam − reference.
    assert abs(stage.pos[4] - ORIGIN[4] - (BEAM[0] - REF_CENTRE[0])) <= 1
    assert abs(stage.pos[5] - ORIGIN[5] - (BEAM[1] - REF_CENTRE[1])) <= 1
    assert tab.is_following  # waits for the next interval

    tab.stop_following()
    assert stage.released == ["lease"]
    assert setup.window.tab_widget.isTabEnabled(0)
    rows = list(csv.DictReader(open(next(setup.tmp_path.glob("gasket_tracking_log_from_*.csv")),
                                    encoding="utf-8")))
    assert len(rows) == 1
    assert int(rows[0]["ch4_pulse"]) == stage.pos[4]
    assert float(rows[0]["gasket_offset_um"]) <= tab.tolerance_spin.value()


def test_total_limit_clamps_correction_and_stops(setup):
    tab, stage = setup.tab, setup.stage
    tab.limit_total[(4, 1)].setValue(0.010)  # 5 pulses of Ch4 (2 µm/pulse)
    tab.start_following()
    _run_attempt(setup)
    assert stage.pos[4] - ORIGIN[4] == 5
    assert not tab.is_following
    # 5 px (10 µm) of Ch4 offset is left, more than the 8 µm tolerance.
    assert "Gasket centre remains" in tab.log.toPlainText()
    assert stage.released == ["lease"]


def test_detection_failure_stops_tracking_without_moving(setup):
    setup.session.blank = True
    tab, stage = setup.tab, setup.stage
    tab.start_following()
    _run_attempt(setup)
    assert not tab.is_following
    assert stage.moves == []
    assert "gasket frames were detected" in tab.log.toPlainText()
    assert stage.released == ["lease"]


def test_start_refuses_unmarked_setup(setup):
    tab = setup.tab
    tab._state["gasket_sample_point"] = None
    tab.start_following()
    assert not tab.is_following
    assert setup.warnings and "sample centre" in setup.warnings[0][1]


def test_sample_positions_result_is_saved_independently(setup):
    tab = setup.tab
    new_ref = setup.tmp_path / "new_reference.png"
    cv2.imwrite(str(new_ref), gasket_image(*REF_CENTRE))
    dialog = SimpleNamespace(
        exec=lambda: None, reference_path=new_ref, reference_changed=True,
        reference_frame=gasket_image(*REF_CENTRE), save_directory=setup.tmp_path,
        gasket_ellipse=GasketEllipse(*REF_CENTRE, 190, 175).to_dict(),
        gasket_sample_point={"x": 320.0, "y": 230.0}, gasket_sample_mode="manual",
        gasket_reference_digest=reference_digest(new_ref),
        gasket_reference_position=(0, 1000, 2000), xrd_position=(0, 1000, 2000),
        ruby_positions={"Ruby B": {"ch3": 1, "ch4": 2, "ch5": 3}},
        ruby_offset_origin="gasket_centre",
    )
    with mock.patch("apps.interactive_camera.sample_positions_dialog.SamplePositionsDialog",
                    return_value=dialog) as dialog_class:
        tab.open_sample_positions()
    assert dialog_class.call_args.kwargs["tracking_method"] == "gasket_beta"
    saved = json.loads(setup.state_path.read_text(encoding="utf-8"))
    assert saved["reference_path"] == str(new_ref)
    assert saved["gasket_sample_point"] == {"x": 320.0, "y": 230.0}
    assert saved["ruby_positions"] == {"Ruby B": {"ch3": 1, "ch4": 2, "ch5": 3}}
    assert saved["gasket_reference_position"] == [0, 1000, 2000]
    assert tab._overlay is not None
    assert tab._overlay.sample_offset == pytest.approx((5.0, 0.0), abs=1.5)


def test_similarity_tracking_refuses_to_start_while_gasket_tracks(setup):
    setup.tab.start_following()
    setup.window.reference_frame = gasket_image(*REF_CENTRE)
    setup.window.start_following()
    assert not setup.window.is_following
    _run_attempt(setup)


def _kinds(stage):
    return [call[0] for call in stage.calls]


def test_normal_stop_returns_controller_to_loc_before_release(setup):
    tab, stage = setup.tab, setup.stage
    tab.start_following()
    _run_attempt(setup)
    tab.stop_following()
    assert _kinds(stage)[-2:] == ["loc", "release"]
    assert _kinds(stage).count("release") == 1


def test_rejected_ch5_waits_for_ch4_and_records_its_motion(setup):
    tab, stage = setup.tab, setup.stage
    stage.reject.add(5)
    tab.start_following()
    _run_attempt(setup)
    kinds = _kinds(stage)
    # Ch4 moved, then the stage was waited for before LOC and the release.
    assert stage.calls[0] == ("move", 4, BEAM[0] - REF_CENTRE[0])
    assert kinds.index("wait") < kinds.index("release")
    assert tab.follow_cumulative[4] == BEAM[0] - REF_CENTRE[0]
    assert not tab.is_following
    assert "Ch5 move rejected" in tab.log.toPlainText()


def test_closing_window_while_tracking_releases_lease_in_loc(setup):
    tab, stage = setup.tab, setup.stage
    tab.start_following()
    _run_attempt(setup)
    assert tab.is_following
    setup.window.close()
    assert stage.released == ["lease"]
    assert _kinds(stage)[-2:] == ["loc", "release"]


def test_interval_runs_from_end_of_each_attempt(setup):
    tab = setup.tab
    tab.interval_spin.setValue(2.0)
    tab.start_following()
    assert not tab._follow_timer.isActive()  # not while the attempt runs
    _run_attempt(setup)
    assert tab._follow_timer.isSingleShot()
    assert tab._follow_timer.isActive()
    assert tab._follow_timer.interval() == 120_000  # started after the attempt
    tab.stop_following()
    assert not tab._follow_timer.isActive()


def test_failed_stop_confirmation_sends_normal_stop_then_releases(setup):
    tab, stage = setup.tab, setup.stage
    stage.wait_failures = 3  # move wait, its retry, and the final confirmation
    tab.start_following()
    _run_attempt(setup)
    kinds = _kinds(stage)
    assert kinds.index("normal_stop") < kinds.index("wait") < kinds.index("release")
    assert stage.released == ["lease"]
    assert not tab.busy()


def test_unconfirmed_stop_keeps_lease_until_confirmed(setup):
    tab, stage = setup.tab, setup.stage
    stage.wait_failures = 10_000
    tab.start_following()
    _run_attempt(setup)
    assert stage.released == []
    assert tab.holds_motion() and tab.busy()
    assert tab.btn_stop.text() == "Confirm stage stopped"
    assert "could not be confirmed" in setup.warnings[-1][1]
    assert not setup.window.tab_widget.isTabEnabled(0)

    setup.window.close()  # refused while the lease is held
    assert stage.released == []
    assert "still holds motion control" in setup.warnings[-1][1]

    tab.btn_stop.click()  # still failing: the lease stays
    tab._confirm_thread.join(timeout=10)
    setup.app.processEvents()
    assert stage.released == []

    stage.wait_failures = 0
    tab.btn_stop.click()
    tab._confirm_thread.join(timeout=10)
    setup.app.processEvents()
    assert stage.released == ["lease"]
    assert _kinds(stage)[-2:] == ["loc", "release"]
    assert tab.btn_stop.text() == "Stop Tracking"
    assert not tab.busy()
