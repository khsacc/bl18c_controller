"""Process-wide, opt-in shutter policy. All hardware calls run on the caller's worker.

The launcher supplies its shared controller. Standalone applications default to
Manual; this policy is deliberately neither persisted nor shared across processes.
"""
from contextlib import contextmanager
import threading

from utils.shutter import ShutterError


class TransmissionPreflightError(ShutterError):
    """The shutter state prevents a transmission scan from starting."""


class BeamControl:
    def __init__(self):
        self._lock = threading.RLock()
        self._controller = None
        self._automatic = False
        self._depth = 0
        self._owner = None
        self._beams = []

    @property
    def controller(self):
        return self._controller

    @property
    def automatic(self):
        with self._lock:
            return self._automatic

    def configure(self, controller):
        with self._lock:
            if self._depth or self._automatic:
                raise ShutterError("Cannot replace the shutter controller while in use")
            self._controller = controller

    @property
    def in_use(self):
        with self._lock:
            return bool(self._depth)

    def set_automatic(self, automatic):
        with self._lock:
            if self._depth:
                raise ShutterError("Wait for the current measurement to finish before changing shutter control")
            if automatic and self._controller is None:
                raise ShutterError("X-ray shutter is not available")
            self._depth = 1
            self._owner = threading.get_ident()
            self._beams.append(None)
        try:
            if automatic:
                # Commit Auto only after confirmed In. Do not hold the state
                # lock across network I/O: GUI properties must remain responsive.
                self._position(True)
            with self._lock:
                self._automatic = bool(automatic)
        finally:
            with self._lock:
                self._beams.pop()
                self._depth = 0
                self._owner = None

    def _position(self, inserted):
        self._controller.set_inserted(inserted)
        if self._controller.get_status() is not inserted:
            raise ShutterError("Cannot confirm shutter " + ("In" if inserted else "Out"))

    def capture_session(self):
        """Triggered detector capture inherits an explicit dark scope."""
        with self._lock:
            beam = False if False in self._beams else True
        return self.session(beam=beam)

    @contextmanager
    def session(self, *, beam=None, manual=False):
        """Reserve shutter ownership; beam=None reserves without moving it.

        Nested calls on the owning worker support scheduler -> detector capture.
        Other workers fail immediately instead of queueing a late exposure.
        """
        owner = threading.get_ident()
        with self._lock:
            if self._depth and self._owner != owner:
                raise ShutterError("X-ray shutter is in use by another measurement")
            if manual and self._automatic:
                raise ShutterError("Select Manual Control before moving the shutter manually")
            if beam is not None and any(b is not None and b != beam for b in self._beams):
                raise ShutterError("Conflicting shutter requirements in nested measurement")
            automatic = self._automatic and beam is not None and beam not in self._beams
            self._beams.append(beam)
            self._owner = owner
            self._depth += 1
        try:
            if automatic:
                self._position(not beam)
            yield
        finally:
            try:
                # Also attempt In if opening or its status confirmation failed.
                if automatic:
                    self._position(True)
            finally:
                with self._lock:
                    self._beams.pop()
                    self._depth -= 1
                    if not self._depth:
                        self._owner = None


beam_control = BeamControl()


@contextmanager
def transmission_session(reader):
    """Fluorescence and other non-Keithley readers must never introduce X-rays."""
    if not getattr(reader, "uses_transmitted_xrays", False):
        yield
        return

    with beam_control.session(beam=True):
        # Ownership keeps the mode and local manual controls stable while the
        # worker checks the live status. Standalone apps have no shared shutter.
        controller = beam_control.controller
        if not beam_control.automatic and controller is not None:
            try:
                inserted = controller.get_status()
            except ShutterError as exc:
                raise TransmissionPreflightError(
                    "Cannot confirm the X-ray shutter position. "
                    "Check the STARS connection and confirm OUT before starting "
                    f"a transmission scan.\n{exc}"
                ) from exc
            if inserted is True:
                raise TransmissionPreflightError(
                    "The X-ray shutter is IN, blocking X-rays. "
                    "Move the shutter OUT or select Auto Control, then start "
                    "the transmission scan again."
                )
            if inserted is not False:
                raise TransmissionPreflightError(
                    "Cannot confirm the X-ray shutter is OUT. "
                    "Check the shutter position before starting a transmission scan."
                )
        yield
