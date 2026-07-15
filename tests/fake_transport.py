"""Shared test doubles for the PM16C control stack.

FakeTransport is a drop-in for PM16CController.client (a socket): it records
sent commands, produces scripted replies, and supports delay/blocking-gate/
failure injection so tests can hold a command "in flight" deterministically.
"""
import queue
import re
import socket
import threading


def default_responder(cmd: str):
    """Reply generator mimicking an idle PM16C (all channels stopped at 0).

    Returns the reply line (without terminator) or None for commands that
    have no response (REM/LOC/ABS/REL/stops/speed-set/LN_SRQ...).
    """
    upper = cmd.upper()
    m = re.fullmatch(r"STS([0-9A-F])\?", upper)
    if m:
        return f"R{m.group(1)}S000+0000000"
    if upper == "STQ?":
        return "R4"
    m = re.fullmatch(r"SPD\?([0-9A-F])", upper)
    if m:
        return "MSPD"
    m = re.fullmatch(r"SPD([LMH])\?([0-9A-F])", upper)
    if m:
        return "2000"
    if upper == "STS?":
        return "R1234/SSSS/0000/00000000/+0000000/+0000000/+0000000/+0000000"
    return None


class FakeTransport:
    """Socket stand-in with scripted replies and injection hooks.

    responder(cmd) -> reply line or None (no reply).
    send_gate(cmd) -> called inside sendall; may block (e.g. wait on an
        Event) to hold this command in flight.
    fail_fn(cmd) -> exception to raise from sendall, or None.
    """

    def __init__(self, responder=None, *, send_gate=None, fail_fn=None):
        self.responder = responder or default_responder
        self.send_gate = send_gate
        self.fail_fn = fail_fn
        self.sent: "list[str]" = []
        self._rx: "queue.Queue[bytes]" = queue.Queue()
        self._timeout = 2.0
        self._lock = threading.Lock()
        self.closed = False

    # socket API used by PM16CController -------------------------------------

    def sendall(self, payload: bytes) -> None:
        cmd = payload.decode("ascii").strip()
        if self.fail_fn is not None:
            exc = self.fail_fn(cmd)
            if exc is not None:
                raise exc
        if self.send_gate is not None:
            self.send_gate(cmd)
        with self._lock:
            self.sent.append(cmd)
            reply = self.responder(cmd)
            if reply is not None:
                self._rx.put(f"{reply}\r\n".encode("ascii"))

    def recv(self, bufsize: int) -> bytes:
        try:
            return self._rx.get(timeout=self._timeout)
        except queue.Empty:
            raise socket.timeout("FakeTransport: no scripted reply")

    def settimeout(self, value) -> None:
        self._timeout = value if value is not None else 2.0

    def close(self) -> None:
        self.closed = True

    # test helpers ------------------------------------------------------------

    def push_reply(self, line: str) -> None:
        """Inject an unsolicited line (e.g. an async STOPx notification)."""
        self._rx.put(f"{line}\r\n".encode("ascii"))


class MemoryAudit:
    """In-memory PM16CAuditLogger stand-in (record/write_incident only)."""

    def __init__(self):
        self.events = []
        self.incidents = []
        self._lock = threading.Lock()

    def record(self, event, **fields):
        with self._lock:
            self.events.append({"event": event, **fields})
            return self.events[-1]

    def write_incident(self, trigger, states):
        with self._lock:
            self.incidents.append((trigger, states))

    def names(self):
        with self._lock:
            return [e["event"] for e in self.events]
