"""Shared STARS client for the BL-18C stopper and PF ring read-out.

``True`` means In (beam blocked); ``False`` means Out (beam reaches sample).
The separate Status node is authoritative. Drive replies alone never prove
that the stopper moved.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from settings.shutter_prefs import NODE_NAME, PORT


class ShutterError(RuntimeError):
    pass


class ShutterTransitioning(ShutterError):
    """The stopper is between its confirmed In and Out positions."""


RING_CURRENT_NODE = "Ring.DCCT"
RING_STATUS_NODE = "Ring.Status"


class StarsController:
    def __init__(self, ip_address: str, keyword: str, *, timeout: float = 3.0):
        self.ip_address = ip_address
        self._keyword = keyword
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stopping = threading.Event()
        self._command_lock = threading.Lock()
        self._connect_lock = threading.RLock()
        self._status_subscription_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: tuple[str, str, threading.Event] | None = None
        self._reply: str | None = None
        self._failure: str | None = None
        self._status_subscribed = False
        self._listeners: set[Callable[[bool | None], None]] = set()
        self._status: bool | None = None

    def add_status_listener(self, listener: Callable[[bool | None], None]) -> None:
        with self._state_lock:
            self._listeners.add(listener)

    def remove_status_listener(self, listener: Callable[[bool | None], None]) -> None:
        with self._state_lock:
            self._listeners.discard(listener)

    def _publish_status(self, inserted: bool | None) -> None:
        with self._state_lock:
            self._status = inserted
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(inserted)

    @staticmethod
    def _parse_status(value: str) -> bool:
        if value == "In":
            return True
        if value == "Out":
            return False
        if value == "Neutral":
            raise ShutterTransitioning("Stopper position is Neutral")
        raise ShutterError(f"Unexpected stopper status: {value!r}")

    @staticmethod
    def _read_challenge(sock: socket.socket) -> tuple[str, bytes]:
        data = bytearray()
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ShutterError("STARS closed the connection before authentication")
            data.extend(chunk)
            if len(data) > 4096:
                raise ShutterError("STARS authentication challenge is too long")
        line, remainder = bytes(data).split(b"\n", 1)
        try:
            challenge = line.strip().decode("ascii")
        except UnicodeDecodeError as exc:
            raise ShutterError("Invalid STARS authentication challenge") from exc
        if not challenge.isdecimal():
            raise ShutterError(f"Invalid STARS authentication challenge: {challenge!r}")
        return challenge, remainder

    def connect(self) -> None:
        with self._connect_lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        if self._socket is not None:
            with self._state_lock:
                failed = self._failure is not None
            if not failed:
                return
            self.close()
        if not self._keyword or any(ch.isspace() for ch in self._keyword):
            raise ShutterError("Set the STARS keyword in Settings first")
        try:
            sock = socket.create_connection((self.ip_address, PORT), self._timeout)
            sock.settimeout(self._timeout)
            try:
                _, remainder = self._read_challenge(sock)
                sock.sendall(f"{NODE_NAME} {self._keyword}\n".encode("ascii"))
            except Exception:
                sock.close()
                raise
        except (OSError, UnicodeError) as exc:
            raise ShutterError(f"Could not connect to STARS: {exc}") from exc
        self._socket = sock
        self._stopping.clear()
        self._status_subscribed = False
        self._reader = threading.Thread(
            target=self._reader_loop, args=(sock, remainder), name="stars-shutter-reader", daemon=True,
        )
        self._reader.start()

    def _subscribe_status(self) -> None:
        with self._status_subscription_lock:
            self.connect()
            if not self._status_subscribed:
                self._request("System flgon stopper.Status", "System", "flgon")
                self._status_subscribed = True

    def _reader_loop(self, sock: socket.socket, initial: bytes) -> None:
        data = bytearray(initial)
        try:
            while not self._stopping.is_set():
                while b"\n" in data:
                    line, _, rest = data.partition(b"\n")
                    data = bytearray(rest)
                    self._handle_line(line.strip().decode("ascii", errors="replace"))
                if len(data) > 16384:
                    raise ShutterError("STARS message is too long")
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ShutterError("STARS connection closed")
                data.extend(chunk)
        except (OSError, ShutterError) as exc:
            if not self._stopping.is_set():
                with self._state_lock:
                    self._failure = str(exc)
                    pending = self._pending
                if pending is not None:
                    pending[2].set()
                self._publish_status(None)

    def _handle_line(self, line: str) -> None:
        if " " not in line:
            return
        source_target, message = line.split(" ", 1)
        if ">" not in source_target:
            return
        source, target = source_target.split(">", 1)
        if target != NODE_NAME:
            return
        if source == "stopper.Status" and message.startswith("_ChangedValue "):
            value = message[len("_ChangedValue "):].strip()
            try:
                self._publish_status(self._parse_status(value))
            except ShutterError:
                self._publish_status(None)
            return
        if not message.startswith("@"):
            return
        with self._state_lock:
            pending = self._pending
            if pending is not None and (
                (source == pending[0] and (
                    message == f"@{pending[1]}" or message.startswith(f"@{pending[1]} ")
                )) or (source == "System" and message.startswith(f"@{pending[1]} Er:"))
            ):
                self._reply = message
                pending[2].set()

    def _request(self, command: str, source: str, reply_name: str) -> str:
        with self._command_lock:
            if self._socket is None:
                raise ShutterError("STARS is not connected")
            event = threading.Event()
            with self._state_lock:
                if self._failure:
                    raise ShutterError(self._failure)
                self._pending = (source, reply_name, event)
                self._reply = None
            try:
                self._socket.sendall((command + "\n").encode("ascii"))
                if not event.wait(self._timeout):
                    raise ShutterError(f"Timed out waiting for {reply_name} reply")
                with self._state_lock:
                    if self._failure:
                        raise ShutterError(self._failure)
                    if self._reply is None:
                        raise ShutterError(f"No {reply_name} reply")
                    if self._reply.startswith(f"@{reply_name} Er:"):
                        raise ShutterError(f"STARS {reply_name} failed: {self._reply}")
                    return self._reply
            except (OSError, ShutterError) as exc:
                self.close()
                if isinstance(exc, OSError):
                    raise ShutterError(f"STARS communication failed: {exc}") from exc
                raise
            finally:
                with self._state_lock:
                    self._pending = None

    def get_status(self) -> bool:
        self._subscribe_status()
        reply = self._request("stopper.Status GetValue", "stopper.Status", "GetValue")
        fields = reply.split()
        if len(fields) != 2:
            self._publish_status(None)
            raise ShutterError(f"Unexpected GetValue reply: {reply!r}")
        try:
            inserted = self._parse_status(fields[1])
        except ShutterError:
            self._publish_status(None)
            raise
        self._publish_status(inserted)
        return inserted

    def read_ring_current(self) -> str:
        """Return the Ring.DCCT value in mA, without adding display units."""
        self.connect()
        reply = self._request(
            f"{RING_CURRENT_NODE} GetValue", RING_CURRENT_NODE, "GetValue",
        )
        prefix = "@GetValue "
        if not reply.startswith(prefix) or not reply[len(prefix):].strip():
            raise ShutterError(f"Unexpected ring current reply: {reply!r}")
        return reply[len(prefix):].strip()

    def read_ring_status(self) -> int:
        """Return the documented PF ring operating mode (0–5)."""
        self.connect()
        reply = self._request(
            f"{RING_STATUS_NODE} GetValue", RING_STATUS_NODE, "GetValue",
        )
        fields = reply.split()
        if len(fields) != 2 or fields[0] != "@GetValue" or fields[1] not in {"0", "1", "2", "3", "4", "5"}:
            raise ShutterError(f"Unexpected Ring.Status reply: {reply!r}")
        return int(fields[1])

    def set_inserted(self, inserted: bool, *, settle_timeout: float = 10.0) -> None:
        self._subscribe_status()
        self._request(f"stopper.Drive SetValue {1 if inserted else 0}", "stopper.Drive", "SetValue")
        deadline = time.monotonic() + settle_timeout
        while True:
            try:
                if self.get_status() is inserted:
                    return
            except ShutterTransitioning:
                pass
            if time.monotonic() >= deadline:
                raise ShutterError("Stopper did not reach the requested position")
            time.sleep(0.2)

    def toggle(self) -> bool:
        target = not self.get_status()
        self.set_inserted(target)
        return target

    def close(self) -> None:
        with self._connect_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        self._stopping.set()
        self._status_subscribed = False
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        self._reader = None
        with self._state_lock:
            self._failure = None
        self._publish_status(None)


# Preserve standalone users of the shutter-specific name.
ShutterController = StarsController


class SharedShutterController:
    """Shutter API whose close releases a consumer, not the app-owned socket."""

    def __init__(self, stars: StarsController):
        self._stars = stars

    def add_status_listener(self, listener: Callable[[bool | None], None]) -> None:
        self._stars.add_status_listener(listener)

    def remove_status_listener(self, listener: Callable[[bool | None], None]) -> None:
        self._stars.remove_status_listener(listener)

    def get_status(self) -> bool:
        return self._stars.get_status()

    def read_ring_current(self) -> str:
        return self._stars.read_ring_current()

    def read_ring_status(self) -> int:
        return self._stars.read_ring_status()

    def set_inserted(self, inserted: bool, *, settle_timeout: float = 10.0) -> None:
        self._stars.set_inserted(inserted, settle_timeout=settle_timeout)

    def toggle(self) -> bool:
        return self._stars.toggle()

    def close(self) -> None:
        pass


class ShutterControllerSim:
    """Hardware-free controller for ``main.py --debug``."""

    def __init__(self):
        self._inserted = True
        self.ring_status = 5
        self.ring_current = "450"
        self._listeners: set[Callable[[bool | None], None]] = set()

    def add_status_listener(self, listener: Callable[[bool | None], None]) -> None:
        self._listeners.add(listener)

    def remove_status_listener(self, listener: Callable[[bool | None], None]) -> None:
        self._listeners.discard(listener)

    def get_status(self) -> bool:
        return self._inserted

    def set_inserted(self, inserted: bool, *, settle_timeout: float = 10.0) -> None:
        self._inserted = inserted
        for listener in tuple(self._listeners):
            listener(inserted)

    def toggle(self) -> bool:
        self.set_inserted(not self._inserted)
        return self._inserted

    def read_ring_current(self) -> str:
        return self.ring_current

    def read_ring_status(self) -> int:
        return self.ring_status

    def close(self) -> None:
        pass
