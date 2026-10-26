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
        self.status_sequence: list[str] = []
        self.drop_reads = False
        self.drop_ring_reads = False
        self.ring_error = False
        self.ring_status_reply = "5"
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
                    value = (self.status_sequence.pop(0) if self.status_sequence else
                             self.override_status or ("In" if self.inserted else "Out"))
                    sock.sendall(f"stopper.Status>Auto18C @GetValue {value}\n".encode())
                elif command == "Ring.DCCT GetValue":
                    if self.ring_error:
                        sock.sendall(b"System>Auto18C @GetValue Er: Ring.DCCT unavailable.\n")
                    elif not self.drop_ring_reads:
                        sock.sendall(
                            b"Ring.DCCT>Auto18C @GetValue 449.8\n"
                        )
                elif command == "Ring.Status GetValue":
                    sock.sendall(
                        f"Ring.Status>Auto18C @GetValue {self.ring_status_reply}\n".encode()
                    )
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
        for status in ("Moving", "Neutral"):
            with self.subTest(status=status):
                self.server.override_status = status
                with self.assertRaises(shutter.ShutterError):
                    self.controller.toggle()
        self.assertNotIn("stopper.Drive SetValue 0", self.server.commands)

    def test_neutral_during_motion_is_polled_until_position_is_confirmed(self) -> None:
        self.server.status_sequence = ["Neutral", "Out"]
        self.controller.set_inserted(False, settle_timeout=1)
        self.assertFalse(self.controller.get_status())
        self.assertEqual(self.server.commands.count("stopper.Status GetValue"), 3)

    def test_neutral_during_motion_still_obeys_settle_timeout(self) -> None:
        self.server.override_status = "Neutral"
        with self.assertRaisesRegex(shutter.ShutterError, "did not reach"):
            self.controller.set_inserted(False, settle_timeout=0.25)

    def test_timeout_invalidates_connection_and_refresh_can_retry(self) -> None:
        self.server.drop_reads = True
        with self.assertRaisesRegex(shutter.ShutterError, "Timed out"):
            self.controller.get_status()
        self.server.drop_reads = False
        self.assertTrue(self.controller.get_status())
        self.assertEqual(self.server.commands.count("Auto18C test-key"), 2)

    def test_ring_current_and_shutter_share_one_authenticated_connection(self) -> None:
        consumer = shutter.SharedShutterController(self.controller)
        self.assertEqual(self.controller.read_ring_current(), "449.8")
        self.assertEqual(self.server.commands[:2], [
            "Auto18C test-key", "Ring.DCCT GetValue",
        ])
        self.assertTrue(consumer.get_status())
        consumer.close()
        self.assertTrue(consumer.get_status())
        self.assertEqual(self.server.commands.count("Auto18C test-key"), 1)
        self.assertIn("Ring.DCCT GetValue", self.server.commands)

    def test_ring_status_uses_shared_connection_and_accepts_user_modes(self) -> None:
        consumer = shutter.SharedShutterController(self.controller)
        for raw, expected in (("4", 4), ("5", 5)):
            self.server.ring_status_reply = raw
            self.assertEqual(consumer.read_ring_status(), expected)
        self.assertEqual(self.server.commands.count("Auto18C test-key"), 1)
        self.assertEqual(self.server.commands.count("Ring.Status GetValue"), 2)
        consumer.close()
        self.assertEqual(self.controller.read_ring_status(), 5)

    def test_ring_status_rejects_unknown_or_malformed_reply(self) -> None:
        for raw in ("6", "4.0", "45", "4 extra", ""):
            with self.subTest(raw=raw):
                self.server.ring_status_reply = raw
                with self.assertRaisesRegex(shutter.ShutterError, "Unexpected Ring.Status reply"):
                    self.controller.read_ring_status()

    def test_simulated_ring_status_can_exercise_fault_path(self) -> None:
        simulator = shutter.ShutterControllerSim()
        self.assertEqual(simulator.read_ring_status(), 5)
        simulator.ring_status = 2
        self.assertEqual(simulator.read_ring_status(), 2)

    def test_ring_status_server_error_is_reported(self) -> None:
        self.server.ring_status_reply = "Er: Ring unavailable"
        with self.assertRaisesRegex(shutter.ShutterError, "Ring unavailable"):
            self.controller.read_ring_status()

    def test_ring_timeout_disconnects_and_shutter_can_reconnect(self) -> None:
        self.server.drop_ring_reads = True
        with self.assertRaisesRegex(shutter.ShutterError, "Timed out"):
            self.controller.read_ring_current()
        self.server.drop_ring_reads = False
        self.assertTrue(self.controller.get_status())
        self.assertEqual(self.server.commands.count("Auto18C test-key"), 2)

    def test_ring_server_error_is_reported_and_shutter_can_reconnect(self) -> None:
        self.server.ring_error = True
        with self.assertRaisesRegex(shutter.ShutterError, "Ring.DCCT unavailable"):
            self.controller.read_ring_current()
        self.server.ring_error = False
        self.assertTrue(self.controller.get_status())
        self.assertEqual(self.server.commands.count("Auto18C test-key"), 2)

    def test_concurrent_ring_and_shutter_reads_share_the_socket(self) -> None:
        results: list[object] = []
        workers = [
            threading.Thread(target=lambda: results.append(self.controller.read_ring_current())),
            threading.Thread(target=lambda: results.append(self.controller.get_status())),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(results, ["449.8", True])
        self.assertEqual(self.server.commands.count("Auto18C test-key"), 1)


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
