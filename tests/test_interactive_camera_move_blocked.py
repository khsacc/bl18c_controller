import contextlib

import cv2
import numpy as np
from PyQt6 import QtCore, QtWidgets

from apps.interactive_camera.interactive_camera import MainWindow
from settings.i18n import tr


REASON = (
    "Move blocked: Ch3 target +28955 is outside the configured "
    "soft limit [+29430, +54980]"
)


class _Capture:
    def isOpened(self):
        return True

    def get(self, prop):
        return {
            cv2.CAP_PROP_FRAME_WIDTH: 640,
            cv2.CAP_PROP_FRAME_HEIGHT: 480,
            cv2.CAP_PROP_FPS: 30,
        }.get(prop, 0)

    def read(self):
        return True, np.zeros((480, 640, 3), dtype=np.uint8)

    def release(self):
        pass


class _RejectedMoveController:
    def __init__(self):
        self.coordinator = self
        self.released = False

    def acquire_motion(self, **kwargs):
        return object()

    @contextlib.contextmanager
    def motion_session(self, **kwargs):
        yield object()

    def set_ch_speed(self, *args, **kwargs):
        pass

    def switch_to_rem(self, **kwargs):
        pass

    def switch_to_loc(self, **kwargs):
        pass

    def get_ch_pos(self, ch):
        return 28985

    def move_ch_absolute(self, ch, position, **kwargs):
        raise ValueError(REASON)

    def move_ch_relative(self, ch, diff, **kwargs):
        raise ValueError(REASON)

    def is_valid(self, motion):
        return True

    def release_motion(self, motion):
        self.released = True


def test_autofocus_move_blocked_shows_reason_on_gui_thread(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"])
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: _Capture())
    warnings = []

    def capture_warning(parent, title, message):
        assert QtCore.QThread.currentThread() == app.thread()
        warnings.append((title, message))

    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", capture_warning)
    controller = _RejectedMoveController()
    window = MainWindow(controller=controller)
    window.timer.stop()

    window.start_autofocus()
    window.autofocus.focus_thread.join(timeout=2)
    app.processEvents()

    assert not window.autofocus.focus_thread.is_alive()
    assert controller.released
    assert warnings == [(tr("Move Blocked"), REASON)]
    window.close()


def test_relative_move_blocked_shows_reason_on_gui_thread(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"])
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: _Capture())
    warnings = []

    def capture_warning(parent, title, message):
        assert QtCore.QThread.currentThread() == app.thread()
        warnings.append((title, message))

    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", capture_warning)
    window = MainWindow(controller=_RejectedMoveController())
    window.timer.stop()

    window._move_relative_ch(3, -20)
    window._move_thread.join(timeout=2)
    app.processEvents()

    assert not window._move_thread.is_alive()
    assert warnings == [(tr("Move Blocked"), REASON)]
    window.close()
