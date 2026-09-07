"""PdiService — the app-wide gateway to PDIndexer.

Two independent transports (either can be used without the other):

  * clipboard    — via utils/pdindexer/csharp/PdiSender.exe (method A).
  * watch_folder — writes a legacy .pdi XML file for PDIndexer's own
                   FileSystemWatcher to pick up (method C, pdi_file.py).

See docs/PLAN_PDINDEXER_BRIDGE.md §1-2 and Phase 2/3 for the design
rationale — in particular why capability checks are kept independent
(clipboard_available() / watch_folder_configured()) rather than a single
is_available(), and why send() takes an explicit Trigger rather than being
wired directly to every place a 1D reduction happens to run.

``transport`` is a required argument of every send() call rather than
mutable state on this service: the service is shared app-wide (one
instance, injected into every window), but each window keeps its own combo
box for "which transport". Storing the choice on the shared service let
one window's UI refresh silently override another window's selection —
see code review 2026-09-06. Callers read their own combo box at send time
and pass the result through explicitly.
"""
from __future__ import annotations

import enum
import json
import logging
import platform
from pathlib import Path
from typing import Callable

from PyQt6 import QtCore

from .profile import PdiProfile

_log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_EXE_PATH = _HERE / "bin" / "PdiSender.exe"

# See docs/PLAN_PDINDEXER_BRIDGE.md §0.6: worst case is
# mutex-wait(5s) + Sleep(0.5s) + mutex-wait(5s) = 10.5s. Timeout must clear
# that with margin before we conclude the helper is hung.
_HELPER_TIMEOUT_MS = 15_000
_HELPER_KILL_GRACE_MS = 2_000


class Transport(enum.Enum):
    CLIPBOARD = "clipboard"
    WATCH_FOLDER = "watch_folder"


class Trigger(enum.Enum):
    """Why send() is being called — determines queueing behaviour.
    See docs/PLAN_PDINDEXER_BRIDGE.md Phase 3-1 for the full rationale and
    the table of call sites this maps to in apps/Rad_icon_2022/radicon_ui.py.
    """

    LIVE = "live"                # never auto-sent
    RECOMPUTE = "recompute"      # never auto-sent (settings/poni change)
    SINGLE_SHOT = "single_shot"  # coalesce to the latest while one is in flight
    SEQUENCE = "sequence"        # never coalesced — every frame must arrive
    MANUAL = "manual"            # "Send now" button; coalesce like single-shot

_NEVER_AUTO_SEND = (Trigger.LIVE, Trigger.RECOMPUTE)


class _QueueItem:
    __slots__ = ("profiles", "transport")

    def __init__(self, profiles: list[PdiProfile], transport: Transport) -> None:
        self.profiles = profiles
        self.transport = transport


class PdiService(QtCore.QObject):
    """One instance is meant to be owned by the application (main.py) and
    injected into windows via a ``pdi_service=`` kwarg, mirroring this
    repo's existing ``controller=`` injection pattern — because the
    clipboard and the "PDIndexer" named mutex are both process-wide, OS
    level resources: concurrent independent instances could interleave
    writes. A window that doesn't receive one creates and owns its own
    (consistent with how sub-apps already fall back to owning a
    controller when none is injected).
    """

    # (ok, message, transport) — message is an English diagnostic string;
    # callers tr() it or build their own user-facing text from `ok` +
    # `pdindexer_running()`. transport is the one actually used for this
    # particular completion (not necessarily this service's "current"
    # anything, since there is no such shared state — see module docstring).
    #
    # Every window connected to a shared PdiService receives every
    # completion, including ones another window triggered — e.g. if
    # Rad-icon 2022 and the XRD Scan ROI dialog are both open and share
    # the injected instance, sending from one will also update the
    # other's status label momentarily. Accepted as a low-severity, purely
    # cosmetic side effect of the fix for the transport-clobbering bug
    # (code review 2026-09-06): disambiguating "was this my send" would
    # need a request id threaded through every call site for no
    # correctness benefit (no wrong data is ever sent to the wrong place).
    finished = QtCore.pyqtSignal(bool, str, object)

    def __init__(self, watch_folder: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._watch_folder = watch_folder
        self._process: QtCore.QProcess | None = None
        self._kill_timer: QtCore.QTimer | None = None
        self._pending: _QueueItem | None = None       # coalesced single-shot/manual request
        self._sequence_queue: list[_QueueItem] = []
        self._busy = False
        # SEQUENCE + WATCH_FOLDER profiles accumulate here instead of being
        # written one file per frame — see _send_watch_folder_sequence()
        # for why per-frame writes risk PDIndexer silently dropping frames.
        self._sequence_watch_folder_buffer: list[PdiProfile] = []

    # ------------------------------------------------------------------
    # Capability queries — kept independent; see module docstring.
    # ------------------------------------------------------------------
    def clipboard_available(self) -> bool:
        return platform.system() == "Windows" and _EXE_PATH.is_file()

    def watch_folder_configured(self) -> bool:
        if self._watch_folder is None:
            return False
        try:
            self._watch_folder.mkdir(parents=True, exist_ok=True)
            probe = self._watch_folder / ".pdindexer_write_test"
            probe.write_bytes(b"")
            probe.unlink()
            return True
        except OSError:
            return False

    def any_available(self) -> bool:
        return self.clipboard_available() or self.watch_folder_configured()

    def set_watch_folder(self, path: Path | None) -> None:
        self._watch_folder = path

    def watch_folder(self) -> Path | None:
        return self._watch_folder

    def pdindexer_running_async(self, callback: Callable[[bool], None]) -> None:
        """Best-effort only: a window-title probe via PdiSender --check.
        Never a substitute for the V5 "did it actually show up in
        PDIndexer's list" acceptance check — see
        docs/PLAN_PDINDEXER_BRIDGE.md Phase 1-2's ACK caveat.

        Asynchronous by design: a self-contained single-file .NET exe can
        easily take a few hundred ms just to start (assembly extraction,
        JIT), and this used to be a synchronous QProcess.waitForFinished()
        called from a signal handler on the GUI thread — freezing the
        whole app's UI on every successful clipboard send (worse during a
        SEQUENCE burst, where it happens once per frame). See code review
        2026-09-06 and the Qt docs on waitForFinished() blocking the
        calling thread's event loop. *callback* is invoked exactly once,
        either synchronously (if the platform/exe check fails immediately)
        or later via the Qt event loop.
        """
        if not self.clipboard_available():
            callback(False)
            return

        proc = QtCore.QProcess(self)
        timeout_timer = QtCore.QTimer(self)
        timeout_timer.setSingleShot(True)
        done = False  # guards against calling back twice (e.g. a synchronous
                       # FailedToStart followed by the timeout still firing later)

        def _finish(result: bool) -> None:
            nonlocal done
            if done:
                return
            done = True
            timeout_timer.stop()
            proc.deleteLater()
            callback(result)

        def _on_finished(exit_code: int, exit_status) -> None:
            _finish(exit_status == QtCore.QProcess.ExitStatus.NormalExit and exit_code == 0)

        def _on_error(error) -> None:
            if error != QtCore.QProcess.ProcessError.FailedToStart:
                return  # a real finished() will still arrive — see _start_clipboard_send
            _finish(False)

        def _on_timeout() -> None:
            if proc.state() != QtCore.QProcess.ProcessState.NotRunning:
                proc.kill()
            _finish(False)

        proc.finished.connect(_on_finished)
        proc.errorOccurred.connect(_on_error)
        timeout_timer.timeout.connect(_on_timeout)

        proc.start(str(_EXE_PATH), ["--check"])
        timeout_timer.start(3000)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def send(self, profiles: list[PdiProfile], *, trigger: Trigger, transport: Transport) -> None:
        if trigger in _NEVER_AUTO_SEND:
            _log.debug("PdiService.send: ignoring trigger=%s (never auto-sent)", trigger)
            return
        if not profiles:
            return

        if transport is Transport.WATCH_FOLDER:
            if trigger is Trigger.SEQUENCE:
                # Buffered, not written immediately — see flush_sequence().
                self._sequence_watch_folder_buffer.extend(profiles)
                return
            self._send_watch_folder(profiles)
            return

        item = _QueueItem(list(profiles), transport)

        if trigger is Trigger.SEQUENCE:
            self._sequence_queue.append(item)
            if not self._busy:
                self._advance_queue()
            return

        # SINGLE_SHOT / MANUAL: coalesce to the latest request while busy.
        if self._busy:
            self._pending = item
            return
        self._start_clipboard_send(item)

    def flush_sequence(self, transport: Transport) -> None:
        """Call once a SEQUENCE trigger's burst is complete (normally or
        stopped early) — never mid-burst.

        For WATCH_FOLDER: writes every profile buffered since the last
        flush as ONE .pdi file. This is not an optimisation — writing one
        file per frame is unsafe with this transport: PDIndexer's
        FileSystemWatcher sets `EnableRaisingEvents = false` for the
        entire time it is reading a just-created file (watcher_Created in
        FormMain.cs) and does not queue/replay events raised while
        disabled (per the FileSystemWatcher docs), so any frame written
        while the previous one is still being read is silently never
        seen. See code review 2026-09-06 and
        docs/PLAN_PDINDEXER_BRIDGE.md.

        For CLIPBOARD: a no-op. Rapid per-frame clipboard sends don't have
        the same failure mode — WM_DRAWCLIPBOARD notifications are queued
        by Windows itself even while PDIndexer's handler is busy with a
        previous one, so the existing one-frame-at-a-time FIFO
        (_sequence_queue) is safe as it stands.
        """
        if transport is not Transport.WATCH_FOLDER:
            return
        if not self._sequence_watch_folder_buffer:
            return
        profiles = self._sequence_watch_folder_buffer
        self._sequence_watch_folder_buffer = []
        self._send_watch_folder(profiles)

    def _send_watch_folder(self, profiles: list[PdiProfile]) -> None:
        from .pdi_file import write_pdi_file

        if self._watch_folder is None:
            self.finished.emit(False, "no watch folder configured", Transport.WATCH_FOLDER)
            return
        try:
            write_pdi_file(self._watch_folder, profiles)
        except OSError as exc:
            self.finished.emit(False, f"could not write .pdi file: {exc}", Transport.WATCH_FOLDER)
            return
        self.finished.emit(True, "wrote .pdi file", Transport.WATCH_FOLDER)

    def _advance_queue(self) -> None:
        """The single place that starts the next clipboard send once the
        previous one has fully finished. Called at most once per
        completion (from _on_clipboard_send_done below) — never from
        inside a callback that itself might already be starting the next
        item, which is what let two sequence frames launch concurrently
        before this fix (code review 2026-09-06)."""
        if self._busy:
            return
        if self._pending is not None:
            item = self._pending
            self._pending = None
            self._start_clipboard_send(item)
        elif self._sequence_queue:
            item = self._sequence_queue.pop(0)
            self._start_clipboard_send(item)

    def _start_clipboard_send(self, item: _QueueItem) -> None:
        if not self.clipboard_available():
            self.finished.emit(False, "PdiSender.exe not available on this platform", Transport.CLIPBOARD)
            return

        self._busy = True
        proc = QtCore.QProcess(self)
        self._process = proc
        proc.setProgram(str(_EXE_PATH))
        proc.setArguments([])

        stdin_payload = json.dumps({"profiles": [p.to_json_dict() for p in item.profiles]}).encode("utf-8")

        stderr_chunks: list[bytes] = []
        proc.readyReadStandardError.connect(lambda: stderr_chunks.append(bytes(proc.readAllStandardError())))

        def _on_clipboard_send_done(ok: bool, message: str) -> None:
            self._busy = False
            self._stop_kill_timer()
            self._process = None
            self.finished.emit(ok, message, Transport.CLIPBOARD)
            self._advance_queue()

        def _on_finished(exit_code: int, exit_status) -> None:
            # A crash reports via QProcess.ExitStatus.CrashExit, and Qt
            # documents the accompanying exit code as undefined in that
            # case — it is not guaranteed nonzero. Checking exit_code alone
            # (an earlier version did) could treat a crashed helper as a
            # successful send. See code review 2026-09-06.
            if exit_status == QtCore.QProcess.ExitStatus.NormalExit and exit_code == 0:
                _on_clipboard_send_done(True, "wrote to clipboard")
            else:
                stderr = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
                status_note = "crashed" if exit_status == QtCore.QProcess.ExitStatus.CrashExit else f"exited {exit_code}"
                _on_clipboard_send_done(False, f"PdiSender {status_note}: {stderr or '(no stderr)'}")

        def _on_error(error) -> None:
            # QProcess never emits finished() after FailedToStart (see Qt
            # docs) — every other QProcess.ProcessError IS still followed
            # by finished() (e.g. a Crashed process still reports via
            # finished() with QProcess.ExitStatus.CrashExit, which
            # _on_finished checks explicitly above — its exit code alone is
            # documented as undefined for a crash, not reliably nonzero),
            # so only FailedToStart needs handling here; anything else
            # would otherwise be cleaned up twice. Keying off proc.state()
            # instead (as an earlier version did) is wrong: state is
            # already NotRunning for a FailedToStart too, so that check
            # skipped cleanup for the one case that actually needed it,
            # leaving _busy stuck True forever — see code review 2026-09-06.
            if error != QtCore.QProcess.ProcessError.FailedToStart:
                return
            _on_clipboard_send_done(False, f"failed to start PdiSender.exe: {proc.errorString()}")

        proc.finished.connect(_on_finished)
        proc.errorOccurred.connect(_on_error)

        proc.start()
        proc.write(stdin_payload)
        proc.closeWriteChannel()

        # A FailedToStart can arrive synchronously inside start() on some
        # platforms, in which case _on_clipboard_send_done has already run
        # and cleared self._process by the time we get here — guard so we
        # don't arm a kill-timer for a send that's already been reported.
        if self._process is proc:
            self._kill_timer = QtCore.QTimer(self)
            self._kill_timer.setSingleShot(True)
            self._kill_timer.timeout.connect(lambda: self._on_timeout(proc))
            self._kill_timer.start(_HELPER_TIMEOUT_MS)

    def _on_timeout(self, proc: QtCore.QProcess) -> None:
        if proc.state() == QtCore.QProcess.ProcessState.NotRunning:
            return
        _log.warning("PdiSender.exe exceeded %d ms, terminating", _HELPER_TIMEOUT_MS)
        proc.terminate()
        QtCore.QTimer.singleShot(_HELPER_KILL_GRACE_MS, lambda: self._force_kill(proc))

    def _force_kill(self, proc: QtCore.QProcess) -> None:
        if proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            proc.kill()

    def _stop_kill_timer(self) -> None:
        if self._kill_timer is not None:
            self._kill_timer.stop()
            self._kill_timer = None

    # ------------------------------------------------------------------
    # Shutdown — ensure a hung/running helper never outlives a closed
    # window or a quitting application (docs/PLAN_PDINDEXER_BRIDGE.md
    # Phase 2, QProcess bullet).
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._stop_kill_timer()
        self._sequence_queue.clear()
        self._sequence_watch_folder_buffer.clear()
        self._pending = None
        if self._process is not None and self._process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(_HELPER_KILL_GRACE_MS)
        self._process = None
