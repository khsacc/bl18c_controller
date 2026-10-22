from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from settings import shutter_prefs
from utils import shutter


class _StarsServer:
    def __init__(self):
        self.commands: list[str] = []
        self.inserted = True
        self.override_status: str | None = None
        self.drop_reads = False
        self.connections: list[socket.socket] = []
        self.threads: list[threading.Thread] = []

    def connect(self, _address, _timeout) -> socket.socket:
        client, server = socket.socketpair()
        self.connections.extend((client, server))
        thread = threading.Thread(target=self._serve, args=(server,), daemon=True)
        self.threads.append(thread)
        thread.start()
        return client

    def _serve(self, sock: socket.socket) -> None:
        with sock.makefile("rb") as reader:
            sock.sendall(b"17\n")  # Any challenge index uses the sole keyword.
            authentication = reader.readline().decode().strip()
            self.commands.append(authentication)
            if authentication != "Auto18C test-key":
                return
            while line := reader.readline():
                command = line.decode().strip()
                self.commands.append(command)
                if command == "System flgon stopper.Status":
                    sock.sendall(b"System>Auto18C @flgon Node stopper.Status has been registered.\n")
                elif command == "stopper.Status GetValue":
                    if self.drop_reads:
                        continue
                    value = self.override_status or ("In" if self.inserted else "Out")
                    sock.sendall(f"stopper.Status>Auto18C @GetValue {value}\n".encode())
                elif command.startswith("stopper.Drive SetValue "):
                    self.inserted = command.endswith(" 1")
                    value = "In" if self.inserted else "Out"
                    # Event may precede the command reply.
                    sock.sendall(f"stopper.Status>Auto18C _ChangedValue {value}\n".encode())
                    sock.sendall(f"stopper.Drive>Auto18C @SetValue {int(self.inserted)}\n".encode())

    def stop(self) -> None:
        for sock in self.connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for thread in self.threads:
            thread.join(timeout=2)


class ShutterControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _StarsServer()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.object(shutter.socket, "create_connection", self.server.connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = shutter.ShutterController("127.0.0.1", "test-key", timeout=0.4)
        self.addCleanup(self.controller.close)

    def test_authentication_events_status_and_both_directions(self) -> None:
        observed: list[bool | None] = []
        self.controller.add_status_listener(observed.append)
        self.assertTrue(self.controller.get_status())
        self.controller.set_inserted(False)
        self.assertFalse(self.controller.get_status())
        self.controller.set_inserted(True)
        self.assertTrue(self.controller.get_status())
        self.assertIn(False, observed)
        self.assertIn(True, observed)
        self.assertEqual(self.server.commands[:3], [
            "Auto18C test-key", "System flgon stopper.Status", "stopper.Status GetValue",
        ])
        self.assertIn("stopper.Drive SetValue 0", self.server.commands)
        self.assertIn("stopper.Drive SetValue 1", self.server.commands)

    def test_unknown_status_does_not_enable_a_toggle(self) -> None:
        self.server.override_status = "Moving"
        with self.assertRaisesRegex(shutter.ShutterError, "Unexpected stopper status"):
            self.controller.toggle()
        self.assertNotIn("stopper.Drive SetValue 0", self.server.commands)

    def test_timeout_invalidates_connection_and_refresh_can_retry(self) -> None:
        self.server.drop_reads = True
        with self.assertRaisesRegex(shutter.ShutterError, "Timed out"):
            self.controller.get_status()
        self.server.drop_reads = False
        self.assertTrue(self.controller.get_status())
        self.assertEqual(self.server.commands.count("Auto18C test-key"), 2)


class ShutterPreferencesTests(unittest.TestCase):
    def test_keyword_is_written_only_to_local_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(shutter_prefs, "_PREFS_FILE", Path(temp) / "shutter.json"):
                shutter_prefs.save("192.168.1.104", "test-key")
                self.assertEqual(shutter_prefs.load(), ("192.168.1.104", "test-key"))
                with self.assertRaises(ValueError):
                    shutter_prefs.save("192.168.1.104", "two words")
                with self.assertRaises(ValueError):
                    shutter_prefs.save("not-an-ip", "test-key")


if __name__ == "__main__":
    unittest.main()
