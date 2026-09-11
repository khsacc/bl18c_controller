from __future__ import annotations

import unittest
from unittest import mock

from settings import online_spectrometer_prefs as prefs


class OnlineSpectrometerPreferencesTests(unittest.TestCase):
    def setUp(self):
        previous = (prefs._ip_address, prefs._api_key, prefs._loaded)
        self.addCleanup(self._restore, previous)

    @staticmethod
    def _restore(previous):
        prefs._ip_address, prefs._api_key, prefs._loaded = previous

    def test_connection_is_shared_as_a_base_url(self):
        with mock.patch.object(prefs, "_save") as save:
            prefs.set_connection("192.168.1.123", "secret")

        save.assert_called_once_with("192.168.1.123", "secret")
        self.assertEqual(prefs.get_ip_address(), "192.168.1.123")
        self.assertEqual(prefs.get_api_key(), "secret")
        self.assertEqual(prefs.get_base_url(), "http://192.168.1.123:8765")

    def test_invalid_ipv4_address_is_rejected(self):
        with mock.patch.object(prefs, "_save") as save:
            with self.assertRaisesRegex(ValueError, "IPv4"):
                prefs.set_connection("spectrometer.local", "secret")
        save.assert_not_called()

    def test_empty_api_key_is_rejected(self):
        with mock.patch.object(prefs, "_save") as save:
            with self.assertRaisesRegex(ValueError, "API key"):
                prefs.set_connection("192.168.1.102", "  ")
        save.assert_not_called()

    def test_failed_persistence_does_not_change_live_connection(self):
        prefs._ip_address = "192.168.1.102"
        prefs._api_key = "old"
        prefs._loaded = True
        with mock.patch.object(prefs, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                prefs.set_connection("192.168.1.123", "new")
        self.assertEqual(prefs.get_ip_address(), "192.168.1.102")
        self.assertEqual(prefs.get_api_key(), "old")


if __name__ == "__main__":
    unittest.main()
