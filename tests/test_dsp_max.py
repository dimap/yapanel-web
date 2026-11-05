"""
Comprehensive unit tests for dsp_max.py (Station Max DSP integration for YapAnel).
"""

import unittest
from dsp_max import (
    to_q26_hex,
    calc_hpf_stage,
    calc_notch,
    calc_apf2,
    calc_high_shelf,
    calc_low_shelf,
    biquad_response_db,
    MaxDSP,
    GOLDEN_HIFI,
    BYPASS_HEX,
)


class MockPanel:
    def __init__(self):
        self._settings = {}
        self.adb = None

    def _load_settings(self):
        return self._settings


class TestDSPMath(unittest.TestCase):
    def test_q26_conversion(self):
        self.assertEqual(to_q26_hex(1.0), "04 00 00 00")
        self.assertEqual(to_q26_hex(0.0), "00 00 00 00")
        self.assertEqual(to_q26_hex(-1.0), "FC 00 00 00")

    def test_golden_120hz_notch(self):
        gen = calc_notch(120.0, 2.5, -4.5)
        expected = GOLDEN_HIFI["woofer"]["0x3A"]
        self.assertEqual(gen, expected)

    def test_golden_subsonic_stage1(self):
        gen = calc_hpf_stage(48.0, 0.5412)
        expected = GOLDEN_HIFI["woofer"]["0x3B"]
        self.assertEqual(gen, expected)

    def test_golden_subsonic_stage2(self):
        gen = calc_hpf_stage(48.0, 1.3065)
        expected = GOLDEN_HIFI["woofer"]["0x3C"]
        self.assertEqual(gen, expected)

    def test_golden_apf2_phase(self):
        gen = calc_apf2(260.0, 0.7071)
        expected = GOLDEN_HIFI["tweeter"]["0x38"]
        self.assertEqual(gen, expected)

    def test_response_calculation(self):
        panel = MockPanel()
        dsp = MaxDSP(panel)
        # Force is_supported for calculation test
        dsp.is_supported = lambda: True
        resp = dsp.calculate_response()
        self.assertIn("freqs", resp)
        self.assertIn("woofer_db", resp)
        self.assertIn("tweeter_db", resp)
        self.assertIn("system_db", resp)
        self.assertEqual(len(resp["freqs"]), 60)
        self.assertEqual(len(resp["woofer_db"]), 60)
        self.assertEqual(len(resp["system_db"]), 60)

        # Infrasound attenuation at 20 Hz with subsonic filter enabled
        self.assertLess(resp["woofer_db"][0], -15.0)

        # 120 Hz notch check
        # Find index near 120 Hz
        idx_120 = min(range(len(resp["freqs"])), key=lambda i: abs(resp["freqs"][i] - 120))
        idx_80 = min(range(len(resp["freqs"])), key=lambda i: abs(resp["freqs"][i] - 80))
        # 120 Hz should be dipped compared to 80 Hz
        self.assertLess(resp["system_db"][idx_120], resp["system_db"][idx_80])

    def test_profiles_and_actions(self):
        panel = MockPanel()
        dsp = MaxDSP(panel)
        dsp.is_supported = lambda: True
        # Mock I2C batch write
        writes = []
        dsp._write_i2c_batch = lambda cmds: (writes.extend(cmds), True)[1]
        dsp._save_state = lambda: None
        dsp._update_boot_script = lambda: None

        # Test apply profile hifi
        writes.clear()
        res = dsp.action({"profile": "hifi"})
        self.assertTrue(res["ok"])
        self.assertTrue(res["supported"])
        self.assertIn("response", res)
        self.assertEqual(dsp.active_profile, "hifi")
        self.assertTrue(dsp.filters["subsonic"])
        self.assertTrue(dsp.filters["notch120"])

        # Verify mute and unmute sequences in writes
        # First operations must be soft-mute on tweeter and woofer
        self.assertEqual(writes[0][2], "06 08")  # Tweeter master mute
        self.assertEqual(writes[1][2], "22 30")  # Tweeter channel mute
        self.assertEqual(writes[2][2], "06 08")  # Woofer master mute
        self.assertEqual(writes[3][2], "22 30")  # Woofer channel mute
        self.assertEqual(writes[4][0], "__SLEEP__")  # 15ms zero-cross ramp
        # Last operations must be soft-unmute
        self.assertEqual(writes[-4][2], "22 00")
        self.assertEqual(writes[-3][2], "06 00")
        self.assertEqual(writes[-2][2], "22 00")
        self.assertEqual(writes[-1][2], "06 00")

        # Test apply profile stock
        res = dsp.action({"profile": "stock"})
        self.assertTrue(res["ok"])
        self.assertEqual(dsp.active_profile, "stock")
        self.assertFalse(dsp.filters["subsonic"])

        # Test filter toggle
        res = dsp.action({"action": "set_filter", "filter": "subsonic", "enabled": True})
        self.assertTrue(res["ok"])
        self.assertEqual(dsp.active_profile, "custom")
        self.assertTrue(dsp.filters["subsonic"])

        # Test status payload
        st = dsp.status()
        self.assertTrue(st["supported"])
        self.assertEqual(st["active_profile"], "custom")
        self.assertEqual(len(st["profiles"]), 6)

        # Test apply profile club
        res = dsp.action({"profile": "club"})
        self.assertTrue(res["ok"])
        self.assertEqual(dsp.active_profile, "club")
        self.assertEqual(dsp.params["bass_gain"], 6.5)
        self.assertEqual(dsp.params["subsonic_freq"], 40)
        self.assertEqual(dsp.params["treble_gain"], 2.5)
        self.assertTrue(dsp.filters["subsonic"])
        self.assertTrue(dsp.filters["notch120"])
        self.assertTrue(dsp.filters["phase"])
        self.assertTrue(dsp.filters["treble"])

        # Test set_params with bass_gain and clamping
        res = dsp.action({"action": "set_params", "bass_gain": 4.5})
        self.assertTrue(res["ok"])
        self.assertEqual(dsp.active_profile, "custom")
        self.assertEqual(dsp.params["bass_gain"], 4.5)

        dsp.action({"action": "set_params", "bass_gain": 15.0})
        self.assertEqual(dsp.params["bass_gain"], 8.0)
        dsp.action({"action": "set_params", "bass_gain": -20.0})
        self.assertEqual(dsp.params["bass_gain"], -6.0)

        # Test DRC compressor bypass toggle (full uncompressed dynamic range)
        writes.clear()
        res = dsp.action({"action": "set_filter", "filter": "drc_bypass", "enabled": True})
        self.assertTrue(res["ok"])
        self.assertTrue(dsp.filters["drc_bypass"])
        self.assertTrue(any(w[2] == "60 00 00 08 00" for w in writes))

        writes.clear()
        res = dsp.action({"action": "set_filter", "filter": "drc_bypass", "enabled": False})
        self.assertTrue(res["ok"])
        self.assertFalse(dsp.filters["drc_bypass"])
        self.assertTrue(any(w[2] == "60 00 00 08 01" for w in writes))


if __name__ == "__main__":
    unittest.main()
