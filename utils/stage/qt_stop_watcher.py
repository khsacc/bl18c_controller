"""Qt adapter for the asynchronous stop API.

Stop buttons must never block the Qt main thread on socket I/O.  UIs call
``controller.request_normal_stop()/request_emergency_stop()`` (returns a
concurrent.futures.Future immediately) and hand the Future to a
StopProgressWatcher, which polls it with a QTimer and emits signals the app
connects to its status label / buttons.

Progress states (from controller.get_stop_progress()):
    "queued"          stop accepted, waiting for the comm thread
    "sent_confirming" ASSTP/AESTP on the wire, confirming all motors stopped
    "confirmed"       all motors confirmed stopped
    "failed"          stop could not be sent or confirmed

The watcher stops its timer automatically once the Future completes.
User-facing strings are the APP's responsibility (wrap in tr() there);
this module deliberately emits state keys, not display text.
"""

from PyQt6 import QtCore


class StopProgressWatcher(QtCore.QObject):
    #: Emitted whenever the progress state changes; carries the state key.
    progress_changed = QtCore.pyqtSignal(str)
    #: Emitted once, when the stop Future completes: (ok, error_message).
    finished = QtCore.pyqtSignal(bool, str)

    def __init__(self, controller, future, parent=None, *, interval_ms=100):
        super().__init__(parent)
        self._controller = controller
        self._future = future
        self._last_state = None
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        # Report the initial state straight away.
        QtCore.QTimer.singleShot(0, self._poll)

    def _poll(self):
        state = self._controller.get_stop_progress()
        if state != self._last_state:
            self._last_state = state
            self.progress_changed.emit(state)
        if self._future.done():
            self._timer.stop()
            exc = self._future.exception()
            if exc is None:
                self.finished.emit(True, "")
            else:
                self.finished.emit(False, str(exc))
