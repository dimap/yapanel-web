"""10-полосный эквалайзер АЧХ: валидация, свободные слоты и расчётная кривая."""

import copy
import math
import unittest

import dsp_max
from dsp_max import BYPASS_HEX, EQ_BANDS, EQ_DEFAULT, MaxDSP


class MockPanel:
    def __init__(self):
        self._settings = {}
        self.adb = None

    def _load_settings(self):
        return self._settings


def slot_map(writes):
    """(target, reg) -> payload без префикса регистра."""
    result = {}
    for target, _addr, payload in writes:
        if target == "__SLEEP__":
            continue
        parts = str(payload).split(None, 1)
        if len(parts) == 2:
            result[(target, parts[0].upper())] = " ".join(parts[1].upper().split())
    return result


class TestEQ(unittest.TestCase):
    def panel(self):
        dsp = MaxDSP(MockPanel())
        dsp.is_supported = lambda: True
        self.writes = []
        dsp._write_i2c_batch = lambda cmds: (self.writes.extend(cmds), True)[1]
        dsp._save_state = lambda: True
        dsp._update_boot_script = lambda: True
        return dsp

    def test_bands_go_to_free_slots_and_bypass_zero_bands(self):
        dsp = self.panel()
        values = [6.0, -3.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 0.0, 4.0]
        res = dsp.set_eq(values, save=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(dsp.active_profile, "custom")
        self.assertEqual(dsp.params["eq"], values)

        regs = slot_map(self.writes)
        w, t = dsp_max.WOOFER_SYS, dsp_max.TWEETER_SYS
        self.assertNotEqual(regs[(w, "3E")], BYPASS_HEX)   # 40 Гц +6
        self.assertNotEqual(regs[(w, "3F")], BYPASS_HEX)   # 80 Гц -3
        self.assertEqual(regs[(w, "40")], BYPASS_HEX)      # 160 Гц 0
        self.assertEqual(regs[(t, "39")], BYPASS_HEX)      # 400 Гц 0
        self.assertNotEqual(regs[(t, "3A")], BYPASS_HEX)   # 800 Гц +2
        self.assertEqual(regs[(t, "3B")], BYPASS_HEX)      # 1.6 кГц 0
        self.assertEqual(regs[(t, "3F")], BYPASS_HEX)      # 12.5 кГц 0
        self.assertNotEqual(regs[(t, "40")], BYPASS_HEX)   # 16 кГц +4

        # Калибровочные фильтры и полки пресета не затёрты.
        self.assertIn((w, "3A"), regs)   # notch 120
        self.assertIn((w, "3B"), regs)   # HPF
        self.assertIn((w, "3C"), regs)   # HPF
        self.assertIn((t, "38"), regs)   # APF
        self.assertIn((t, "3C"), regs)   # полка ВЧ

    def test_validation_and_clamping(self):
        dsp = self.panel()
        before = copy.deepcopy(dsp.params)
        bad_values = [
            None,
            "flat",
            [],
            [0.0] * (len(EQ_BANDS) - 1),
            [0.0] * (len(EQ_BANDS) + 1),
            [0.0] * (len(EQ_BANDS) - 1) + [True],
            [0.0] * (len(EQ_BANDS) - 1) + ["громко"],
            [0.0] * (len(EQ_BANDS) - 1) + [float("nan")],
            [0.0] * (len(EQ_BANDS) - 1) + [float("inf")],
            [0.0] * (len(EQ_BANDS) - 1) + [10 ** 1000],
        ]
        for bad in bad_values:
            res = dsp.set_eq(bad, save=False)
            self.assertFalse(res["ok"], bad)
            self.assertEqual(dsp.params, before)
            self.assertEqual(self.writes, [])

        res = dsp.set_eq([99, -99, 0, 0, 0, 0, 0, 0, 0, 0], save=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(dsp.params["eq"][0], dsp_max.EQ_MAX)
        self.assertEqual(dsp.params["eq"][1], dsp_max.EQ_MIN)

    def test_presets_clear_the_equalizer(self):
        dsp = self.panel()
        self.assertTrue(dsp.set_eq([6, 0, 0, 0, 0, 0, 0, 0, 0, 0], save=False)["ok"])
        self.writes.clear()

        res = dsp.apply_profile("night", save=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(dsp.params["eq"], list(EQ_DEFAULT))

        regs = slot_map(self.writes)
        for band in EQ_BANDS:
            target = dsp_max.WOOFER_SYS if band["channel"] == "woofer" else dsp_max.TWEETER_SYS
            self.assertEqual(regs[(target, band["reg"])], BYPASS_HEX, band)

    def test_response_reflects_band_gain(self):
        dsp = self.panel()
        freqs = dsp.calculate_response()["freqs"]
        idx400 = min(range(len(freqs)), key=lambda i: abs(freqs[i] - 400.0))
        idx80 = min(range(len(freqs)), key=lambda i: abs(freqs[i] - 80.0))
        base = dsp.calculate_response()

        dsp.params["eq"] = [0.0, 0.0, 0.0, 6.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        boosted = dsp.calculate_response()
        self.assertGreater(boosted["system_db"][idx400], base["system_db"][idx400] + 4.0)

        dsp.params["eq"] = [0.0, 6.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        boosted = dsp.calculate_response()
        self.assertGreater(boosted["system_db"][idx80], base["system_db"][idx80] + 4.0)

    def test_action_shapes_and_status(self):
        dsp = self.panel()
        bands = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        res = dsp.action({"action": "set_eq", "bands": bands})
        self.assertTrue(res["supported"])
        self.assertEqual(res["eq"], bands)
        self.assertEqual(len(res["eq_bands"]), len(EQ_BANDS))
        self.assertEqual(res["active_profile"], "custom")

        res = dsp.action({"eq": [0.0] * len(EQ_BANDS)})
        self.assertTrue(res["supported"])
        self.assertEqual(res["eq"], list(EQ_DEFAULT))

        self.assertFalse(dsp.action({"action": "set_eq", "bands": [0.0]})["ok"])


if __name__ == "__main__":
    unittest.main()
