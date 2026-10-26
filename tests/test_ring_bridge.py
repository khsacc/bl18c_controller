from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from utils import ring_bridge


class RingBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = ring_bridge.RingBridge(Path("C:/private/ring"), "192.168.1.101")
        platform = mock.patch.object(ring_bridge.sys, "platform", "win32")
        platform.start()
        self.addCleanup(platform.stop)

    def test_existing_manual_process_is_never_owned_or_stopped(self) -> None:
        with (mock.patch.object(ring_bridge, "_ring_process_running", return_value=True),
              mock.patch.object(ring_bridge.subprocess, "Popen") as popen):
            self.assertFalse(self.bridge.ensure_running())
            self.bridge.close()
        popen.assert_not_called()

    def test_start_matches_command_file_and_close_stops_only_owned_process(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (mock.patch.object(ring_bridge, "_ring_process_running", return_value=False),
              mock.patch.object(Path, "is_file", return_value=True),
              mock.patch.object(ring_bridge.subprocess, "Popen", return_value=process) as popen):
            self.assertTrue(self.bridge.ensure_running())
            self.assertFalse(self.bridge.ensure_running())
            self.bridge.close()

        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(self.bridge.directory / "ringEPF.exe"), "Ring", "192.168.1.101"])
        self.assertEqual(kwargs["cwd"], str(self.bridge.directory))
        self.assertEqual(kwargs["env"]["TINE_HOME"], ".")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)

    def test_exited_owned_process_is_restarted(self) -> None:
        first = mock.Mock()
        first.poll.return_value = 1
        second = mock.Mock()
        second.poll.return_value = None
        with (mock.patch.object(ring_bridge, "_ring_process_running", return_value=False),
              mock.patch.object(Path, "is_file", return_value=True),
              mock.patch.object(ring_bridge.subprocess, "Popen", side_effect=[first, second]) as popen):
            self.assertTrue(self.bridge.ensure_running())
            with self.assertLogs(level="WARNING") as logs:
                self.assertTrue(self.bridge.ensure_running())
            self.bridge.close()
        self.assertEqual(popen.call_count, 2)
        self.assertIn("exited with code 1", logs.output[0])
        first.terminate.assert_not_called()
        second.terminate.assert_called_once()

    def test_missing_local_package_does_not_start_a_process(self) -> None:
        with (mock.patch.object(ring_bridge, "_ring_process_running", return_value=False),
              mock.patch.object(Path, "is_file", return_value=False),
              mock.patch.object(ring_bridge.subprocess, "Popen") as popen):
            with self.assertRaisesRegex(ring_bridge.RingBridgeError, "incomplete"):
                self.bridge.ensure_running()
        popen.assert_not_called()

    def test_close_kills_an_owned_process_that_does_not_terminate(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ringEPF.exe", 2), None]
        self.bridge._process = process
        self.bridge.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
