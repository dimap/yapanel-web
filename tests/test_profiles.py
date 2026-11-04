"""Tests for Station Max hardware profile adapter."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock

import profiles

ROOT = Path(__file__).resolve().parents[1]


def fresh_server():
    spec = importlib.util.spec_from_file_location("isolated_panel", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProfileTests(unittest.TestCase):
    def test_only_max_profile_registered(self):
        """Station Max is the sole supported hardware profile."""
        self.assertEqual(list(profiles.PROFILES.keys()), ["max"])

    def test_max_profile_specification(self):
        """Station Max hardware capabilities are correctly configured."""
        prof = profiles.PROFILES["max"]
        self.assertEqual(prof["name"], "Станция Макс")
        self.assertEqual(prof["platform"], "yandexstation_2")
        self.assertEqual(prof["led_prefix"], "max")
        self.assertTrue(prof["clock"])
        self.assertTrue(prof["boot"])
        self.assertTrue(prof["matrix"])
        self.assertEqual(prof["matrix_cols"], 25)
        self.assertEqual(prof["matrix_rows"], 16)
        self.assertEqual(prof["ring_leds"], 24)
        self.assertTrue(prof["light"])
        self.assertTrue(prof["privacy"])
        self.assertTrue(prof["apps"])
        self.assertFalse(prof["alarms"])
        self.assertFalse(prof["scenes"])
        self.assertFalse(prof["assistant"])

    def test_install_configures_station_max(self):
        """install() binds Station Max platform, routes and delegates to max_profile."""
        s = fresh_server()
        profiles.install(s, "max")
        self.assertEqual(s.GLAGOL_PLATFORM, "yandexstation_2")
        self.assertEqual(s.LED_PREFIX, "max")

    def test_install_fallback_to_max_on_unknown_model(self):
        """Any other model request falls back gracefully to max."""
        s = fresh_server()
        profiles.install(s, "lite2")
        self.assertEqual(s.GLAGOL_PLATFORM, "yandexstation_2")

    def test_model_detection_returns_max(self):
        """detect_model identifies Station Max."""
        s = fresh_server()
        adb_mock = Mock()
        adb_mock.shell.return_value = ("amlogic g12a yandexstation_2", "", 0)
        model, platform = s.detect_model(adb_mock)
        self.assertEqual(model, "max")
        self.assertEqual(platform, "yandexstation_2")


if __name__ == "__main__":
    unittest.main()
