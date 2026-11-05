"""
Station Max Hardware DSP Engine for Silergy SY6045S Power Amplifiers.
Integrates with YapAnel: biquad calculation, register flashing,
profile switching, filter toggles, persistence, and frequency response modeling.
"""

import os
import math
import time
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

logger = logging.getLogger("yapanel.dsp")

# Hardware sysfs interface paths
TWEETER_SYS = "/sys/bus/i2c/drivers/sy6045s/2-002a/settings"
WOOFER_SYS = "/sys/bus/i2c/drivers/sy6045s/2-002b/settings"

# 8-bit I2C addresses used in driver protocol
ADDR_TWEETER = "54"
ADDR_WOOFER = "56"

# Bypass biquad coefficient (unity gain: b0=1.0, a1=0, a2=0, b1=0, b2=0 in Q5.26)
BYPASS_HEX = "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 04 00 00 00"
Q26_SCALE = 67108864.0  # 2^26

# Sampling rate on Station Max Amlogic A113X BSP
FS = 48000.0


def to_q26_hex(val: float) -> str:
    """Convert float to 4-byte big-endian Q5.26 signed fixed-point hex string."""
    scaled = int(round(val * Q26_SCALE))
    if scaled < 0:
        scaled = (1 << 32) + scaled
    return f"{(scaled >> 24) & 0xFF:02X} {(scaled >> 16) & 0xFF:02X} {(scaled >> 8) & 0xFF:02X} {scaled & 0xFF:02X}"


def calc_biquad_coeffs(b0: float, b1: float, b2: float, a0: float, a1: float, a2: float) -> str:
    """Normalize by a0 and format as [-a2, -a1, b2, b1, b0] in Q5.26 hex."""
    nb0, nb1, nb2 = b0 / a0, b1 / a0, b2 / a0
    na1, na2 = a1 / a0, a2 / a0
    parts = [
        to_q26_hex(-na2),
        to_q26_hex(-na1),
        to_q26_hex(nb2),
        to_q26_hex(nb1),
        to_q26_hex(nb0),
    ]
    return " ".join(parts)


def calc_hpf_stage(f0: float, q: float, fs: float = FS) -> str:
    """2nd-order High-Pass Butterworth stage."""
    w0 = 2.0 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    b0 = (1.0 + cos_w0) / 2.0
    b1 = -(1.0 + cos_w0)
    b2 = (1.0 + cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return calc_biquad_coeffs(b0, b1, b2, a0, a1, a2)


def calc_notch(f0: float, q: float, gain_db: float, fs: float = FS) -> str:
    """Peaking / Notch EQ biquad filter."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    b0 = 1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / A
    return calc_biquad_coeffs(b0, b1, b2, a0, a1, a2)


def calc_apf2(f0: float, q: float, fs: float = FS) -> str:
    """2nd-order All-Pass filter for group delay & phase alignment."""
    w0 = 2.0 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    b0 = 1.0 - alpha
    b1 = -2.0 * cos_w0
    b2 = 1.0 + alpha
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return calc_biquad_coeffs(b0, b1, b2, a0, a1, a2)


def calc_high_shelf(f0: float, q: float, gain_db: float, fs: float = FS) -> str:
    """High-Shelf filter."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * q)
    beta = 2.0 * math.sqrt(A) * alpha
    b0 = A * ((A + 1.0) + (A - 1.0) * cos_w0 + beta)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) + (A - 1.0) * cos_w0 - beta)
    a0 = (A + 1.0) - (A - 1.0) * cos_w0 + beta
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w0)
    a2 = (A + 1.0) - (A - 1.0) * cos_w0 - beta
    return calc_biquad_coeffs(b0, b1, b2, a0, a1, a2)


def calc_low_shelf(f0: float, q: float, gain_db: float, fs: float = FS) -> str:
    """Low-Shelf filter for punch / dynamic bass boost."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * q)
    beta = 2.0 * math.sqrt(A) * alpha
    b0 = A * ((A + 1.0) - (A - 1.0) * cos_w0 + beta)
    b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) - (A - 1.0) * cos_w0 - beta)
    a0 = (A + 1.0) + (A - 1.0) * cos_w0 + beta
    a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)
    a2 = (A + 1.0) + (A - 1.0) * cos_w0 - beta
    return calc_biquad_coeffs(b0, b1, b2, a0, a1, a2)


def biquad_response_db(b0: float, b1: float, b2: float, a0: float, a1: float, a2: float, f: float, fs: float = FS) -> float:
    """Evaluate magnitude response in dB of a biquad at frequency f."""
    w = 2.0 * math.pi * f / fs
    cos_w = math.cos(w)
    sin_w = math.sin(w)
    cos_2w = math.cos(2.0 * w)
    sin_2w = math.sin(2.0 * w)

    num_re = b0 + b1 * cos_w + b2 * cos_2w
    num_im = -b1 * sin_w - b2 * sin_2w
    den_re = a0 + a1 * cos_w + a2 * cos_2w
    den_im = -a1 * sin_w - a2 * sin_2w

    den_mag = den_re * den_re + den_im * den_im
    if den_mag < 1e-12:
        return -80.0
    num_mag = num_re * num_re + num_im * num_im
    mag = math.sqrt(num_mag / den_mag)
    return 20.0 * math.log10(max(mag, 1e-4))


# Golden preset hex constants (verified bit-exact against physical speaker calibration)
GOLDEN_HIFI = {
    "woofer": {
        "0x3A": "FC 08 4D 63 07 F7 72 32 03 F9 60 4D F8 08 8D CE 03 FE 52 50",  # Notch 120Hz (-4.5dB)
        "0x3B": "FC 0B D1 D6 07 F4 23 E0 03 FA 14 82 F8 0B D6 FB 03 FA 14 82",  # HPF Stage 1 (48Hz, Q=0.5412)
        "0x3C": "FC 04 E9 AA 07 FB 0C 03 03 FD 88 96 F8 04 EE D3 03 FD 88 96",  # HPF Stage 2 (48Hz, Q=1.3065)
        "0x3D": BYPASS_HEX,
        "0x3E": BYPASS_HEX,
        "0x3F": BYPASS_HEX,
        "0x40": BYPASS_HEX,
    },
    "tweeter": {
        "0x38": "FC 30 1E 99 07 CE B8 EC 04 00 00 00 F8 31 47 14 03 CF E1 67",  # APF-2 260Hz
        "0x39": BYPASS_HEX,
        "0x3A": BYPASS_HEX,
        "0x3B": BYPASS_HEX,
        "0x3C": "FF 4A C4 30 00 B3 1A 5E 01 12 8C 92 FD B8 0D 6E 05 37 87 72",  # Treble High-Shelf (+3.0dB @ 13.5kHz)
        "0x3D": BYPASS_HEX,
        "0x3E": BYPASS_HEX,
        "0x3F": BYPASS_HEX,
        "0x40": BYPASS_HEX,
    }
}


class MaxDSP:
    """Manages Station Max SY6045S hardware DSP filters and presets."""

    PROFILES = [
        {
            "id": "hifi",
            "name": "Hi-Fi Studio Flat",
            "badge": "Hi-Fi",
            "desc": "Линейная студийная АЧХ: срез вибраций стола на 48 Гц, режектор гула корпуса на 120 Гц, фазовая компенсация сабвуфера и воздух ВЧ",
        },
        {
            "id": "stock",
            "name": "Заводской (Stock)",
            "badge": "Stock",
            "desc": "Стандартный заводской звук без фильтрации: бубнение на 120 Гц и паразитная раскачка мебели инфразвуком",
        },
        {
            "id": "night",
            "name": "Ночной (Без вибраций)",
            "badge": "Night",
            "desc": "Глубокий срез низких частот (65 Гц) и мягкие ВЧ: музыка не передается по конструкциям стен и перекрытий",
        },
        {
            "id": "bass",
            "name": "Deep Punch",
            "badge": "Punch",
            "desc": "Энергичный плотный бас (65 Гц) с защитой пассивных диффузоров от разбалтывания и очищенным пластиковым гулом",
        },
        {
            "id": "club",
            "name": "Club Bass",
            "badge": "Club",
            "desc": "Мощный клубный бас (+6.5 дБ @ 60 Гц) со срезом инфразвука на 40 Гц и чистым вокалом",
        },
        {
            "id": "custom",
            "name": "Пользовательский",
            "badge": "Custom",
            "desc": "Индивидуальная конфигурация активных фильтров и коррекции",
        },
    ]

    def __init__(self, panel):
        self.panel = panel
        self.adb = getattr(panel, "adb", None)
        self.active_profile = "hifi"
        self._restored = False
        self.filters = {
            "subsonic": True,
            "notch120": True,
            "phase": True,
            "treble": True,
            "drc_bypass": False,
        }
        self.params = {
            "bass_gain": 0.0,
            "treble_gain": 3.0,
            "subsonic_freq": 48,
        }
        self._load_saved_state()
        if self.is_supported():
            try:
                self.restore_hardware_state()
            except Exception as e:
                logger.warning(f"[DSP] Error restoring hardware on init: {e}")

    def _load_saved_state(self):
        """Load saved DSP state from panel settings."""
        try:
            s_func = getattr(self.panel, "_load_settings", None)
            if not s_func:
                import server
                s_func = server.load_settings
            s = s_func()
            dsp_conf = s.get("dsp")
            if not isinstance(dsp_conf, dict):
                direct_path = Path("/data/yapanel/.panel_settings.json")
                if direct_path.exists():
                    try:
                        s_direct = json.loads(direct_path.read_text(encoding="utf-8"))
                        dsp_conf = s_direct.get("dsp")
                    except Exception:
                        pass
            if isinstance(dsp_conf, dict):
                self.active_profile = dsp_conf.get("profile", "hifi")
                if "filters" in dsp_conf and isinstance(dsp_conf["filters"], dict):
                    self.filters.update(dsp_conf["filters"])
                if "params" in dsp_conf and isinstance(dsp_conf["params"], dict):
                    self.params.update(dsp_conf["params"])
        except Exception as e:
            logger.warning(f"Error loading DSP settings: {e}")

    def restore_hardware_state(self) -> Dict[str, Any]:
        """Restore saved DSP configuration to hardware on panel boot/startup."""
        if not self.is_supported():
            return {"ok": False, "error": "Hardware not supported"}
        self._load_saved_state()
        logger.info(f"[DSP] Restoring hardware: profile={self.active_profile}, params={self.params}, filters={self.filters}")
        if self.active_profile == "custom":
            res = self._apply_custom_state(save=False)
        else:
            res = self.apply_profile(self.active_profile, save=False)
        self._restored = True
        return res

    def _save_state(self):
        """Save DSP state to panel settings."""
        try:
            import server
            s = server.load_settings()
            s["dsp"] = {
                "profile": self.active_profile,
                "filters": self.filters,
                "params": self.params,
            }
            server.save_settings(s)
        except Exception as e:
            logger.warning(f"Error saving DSP settings: {e}")

    def is_supported(self) -> bool:
        """Check if Silergy SY6045S dual amp hardware is present."""
        if os.path.exists(WOOFER_SYS) and os.path.exists(TWEETER_SYS):
            return True
        if self.adb:
            out, _, _ = self.adb.shell(f"test -e {WOOFER_SYS} && echo 1 || echo 0", check=False)
            return "1" in out
        return False

    def _build_mute_cmds(self) -> List[Tuple[str, str, str]]:
        """Zero-crossing soft-mute sequence for SY6045S power stages."""
        return [
            (TWEETER_SYS, ADDR_TWEETER, "06 08"),
            (TWEETER_SYS, ADDR_TWEETER, "22 30"),
            (WOOFER_SYS, ADDR_WOOFER, "06 08"),
            (WOOFER_SYS, ADDR_WOOFER, "22 30"),
            ("__SLEEP__", "", "0.015"),
            (TWEETER_SYS, ADDR_TWEETER, "03 5F"),
            (WOOFER_SYS, ADDR_WOOFER, "03 5F"),
        ]

    def _build_unmute_cmds(self) -> List[Tuple[str, str, str]]:
        """Lock EQ and execute zero-crossing soft-unmute for SY6045S."""
        return [
            (TWEETER_SYS, ADDR_TWEETER, "03 5E"),
            (WOOFER_SYS, ADDR_WOOFER, "03 5E"),
            ("__SLEEP__", "", "0.005"),
            (TWEETER_SYS, ADDR_TWEETER, "22 00"),
            (TWEETER_SYS, ADDR_TWEETER, "06 00"),
            (WOOFER_SYS, ADDR_WOOFER, "22 00"),
            (WOOFER_SYS, ADDR_WOOFER, "06 00"),
        ]

    def _write_i2c_batch(self, commands: List[Tuple[str, str, str]]) -> bool:
        """
        Execute I2C write batch commands with soft-mute zero-cross ramp to prevent clicks/pops.
        commands: list of (target_sysfs_path, addr_hex, payload_str)
        """
        is_local = (os.path.exists(WOOFER_SYS) and os.path.exists(TWEETER_SYS)) or getattr(self.adb, "is_local", False)

        if is_local:
            try:
                for target_path, addr, payload in commands:
                    if target_path == "__SLEEP__":
                        time.sleep(float(payload))
                        continue
                    line = f"w {addr} {payload}\n"
                    written = False
                    for attempt in range(3):
                        try:
                            with open(target_path, "w") as f:
                                f.write(line)
                                f.flush()
                            written = True
                            break
                        except OSError:
                            time.sleep(0.008)
                    if not written:
                        logger.error(f"Local I2C write failed for {target_path} after 3 attempts")
                        return False
                return True
            except Exception as e:
                logger.error(f"Local I2C write error: {e}")
                return False
        else:
            # Remote via ADB shell
            sh_segments = []
            cur_cmds = []
            for target_path, addr, payload in commands:
                if target_path == "__SLEEP__":
                    if cur_cmds:
                        sh_segments.append(" && ".join(cur_cmds))
                        cur_cmds = []
                    ms = int(float(payload) * 1000)
                    sh_segments.append(f"usleep {ms * 1000}")
                else:
                    cur_cmds.append(f'echo "w {addr} {payload}" > {target_path}')
            if cur_cmds:
                sh_segments.append(" && ".join(cur_cmds))
            full_cmd = " && ".join(sh_segments)
            out, err, code = self.adb.shell(full_cmd, check=False)
            return code == 0

    def apply_profile(self, profile_name: str, save: bool = True) -> Dict[str, Any]:
        """Apply a named DSP profile to hardware."""
        valid_ids = [p["id"] for p in self.PROFILES]
        if profile_name not in valid_ids:
            return {"ok": False, "error": f"Неизвестный профиль: {profile_name}"}

        if profile_name == "custom":
            return self._apply_custom_state(save=save)

        cmds: List[Tuple[str, str, str]] = []
        cmds.extend(self._build_mute_cmds())

        if profile_name == "stock":
            # Set all registers to bypass
            for reg in ("3A", "3B", "3C", "3D", "3E", "3F", "40"):
                cmds.append((WOOFER_SYS, ADDR_WOOFER, f"{reg} {BYPASS_HEX}"))
            for reg in ("38", "39", "3A", "3B", "3C", "3D", "3E", "3F", "40"):
                cmds.append((TWEETER_SYS, ADDR_TWEETER, f"{reg} {BYPASS_HEX}"))
            self.filters = {k: False for k in self.filters}
            self.params["bass_gain"] = 0.0
            self.params["treble_gain"] = 0.0

        elif profile_name == "hifi":
            # Golden Hi-Fi calibration
            for reg, hex_str in GOLDEN_HIFI["woofer"].items():
                r = reg.replace("0x", "")
                cmds.append((WOOFER_SYS, ADDR_WOOFER, f"{r} {hex_str}"))
            for reg, hex_str in GOLDEN_HIFI["tweeter"].items():
                r = reg.replace("0x", "")
                cmds.append((TWEETER_SYS, ADDR_TWEETER, f"{r} {hex_str}"))
            cur_drc = self.filters.get("drc_bypass", False)
            self.filters = {
                "subsonic": True,
                "notch120": True,
                "phase": True,
                "treble": True,
                "drc_bypass": cur_drc,
            }
            self.params["bass_gain"] = 0.0
            self.params["treble_gain"] = 3.0
            self.params["subsonic_freq"] = 48

        elif profile_name == "night":
            # Subsonic at 65 Hz + notch 120Hz + soft treble rolloff
            s1 = calc_hpf_stage(65, 0.5412)
            s2 = calc_hpf_stage(65, 1.3065)
            notch = calc_notch(120, 2.5, -5.0)
            treble_soft = calc_high_shelf(10000, 0.7, -2.5)
            apf = calc_apf2(260, 0.7071)

            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3A {notch}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3B {s1}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3C {s2}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3D {BYPASS_HEX}"))

            cmds.append((TWEETER_SYS, ADDR_TWEETER, f"38 {apf}"))
            cmds.append((TWEETER_SYS, ADDR_TWEETER, f"3C {treble_soft}"))

            cur_drc = self.filters.get("drc_bypass", False)
            self.filters = {
                "subsonic": True,
                "notch120": True,
                "phase": True,
                "treble": False,
                "drc_bypass": cur_drc,
            }
            self.params["subsonic_freq"] = 65
            self.params["treble_gain"] = -2.5

        elif profile_name == "bass":
            # Subsonic at 45 Hz + notch 120Hz + Low Shelf 65Hz (+3.5dB) + Air 13.5kHz (+2dB)
            s1 = calc_hpf_stage(45, 0.5412)
            s2 = calc_hpf_stage(45, 1.3065)
            notch = calc_notch(120, 2.5, -4.5)
            bass_boost = calc_low_shelf(65, 0.8, 3.5)
            apf = calc_apf2(260, 0.7071)
            treble = calc_high_shelf(13500, 0.7, 2.0)

            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3A {notch}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3B {s1}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3C {s2}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3D {bass_boost}"))

            cmds.append((TWEETER_SYS, ADDR_TWEETER, f"38 {apf}"))
            cmds.append((TWEETER_SYS, ADDR_TWEETER, f"3C {treble}"))

            cur_drc = self.filters.get("drc_bypass", False)
            self.filters = {
                "subsonic": True,
                "notch120": True,
                "phase": True,
                "treble": True,
                "drc_bypass": cur_drc,
            }
            self.params["bass_gain"] = 3.5
            self.params["treble_gain"] = 2.0
            self.params["subsonic_freq"] = 45

        elif profile_name == "club":
            # Subsonic at 40 Hz + notch 120Hz + Low Shelf 60Hz (+6.5dB) + Air 13.5kHz (+2.5dB)
            s1 = calc_hpf_stage(40, 0.5412)
            s2 = calc_hpf_stage(40, 1.3065)
            notch = calc_notch(120, 2.5, -4.5)
            bass_boost = calc_low_shelf(60, 0.85, 6.5)
            apf = calc_apf2(260, 0.7071)
            treble = calc_high_shelf(13500, 0.7, 2.5)

            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3A {notch}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3B {s1}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3C {s2}"))
            cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3D {bass_boost}"))

            cmds.append((TWEETER_SYS, ADDR_TWEETER, f"38 {apf}"))
            cmds.append((TWEETER_SYS, ADDR_TWEETER, f"3C {treble}"))

            cur_drc = self.filters.get("drc_bypass", False)
            self.filters = {
                "subsonic": True,
                "notch120": True,
                "phase": True,
                "treble": True,
                "drc_bypass": cur_drc,
            }
            self.params["bass_gain"] = 6.5
            self.params["treble_gain"] = 2.5
            self.params["subsonic_freq"] = 40

        # 6. Hardware DRC Compressor (Register 0x60: 00 00 08 00 = Bypass, 00 00 08 01 = Stock protection)
        drc_val = "00 00 08 00" if self.filters.get("drc_bypass") else "00 00 08 01"
        cmds.append((WOOFER_SYS, ADDR_WOOFER, f"60 {drc_val}"))
        cmds.append((TWEETER_SYS, ADDR_TWEETER, f"60 {drc_val}"))

        cmds.extend(self._build_unmute_cmds())

        ok = self._write_i2c_batch(cmds)
        if ok:
            self.active_profile = profile_name
            if save:
                self._save_state()
                self._update_boot_script()
            return {"ok": True, "profile": profile_name}
        return {"ok": False, "error": "Ошибка записи в I2C драйвер SY6045S"}

    def set_filter(self, filter_id: str, enabled: bool) -> Dict[str, Any]:
        """Toggle an individual filter on or off."""
        if filter_id not in self.filters:
            return {"ok": False, "error": f"Неизвестный фильтр: {filter_id}"}

        self.filters[filter_id] = bool(enabled)
        self.active_profile = "custom"
        return self._apply_custom_state(save=save)

    def set_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Update custom parameters (bass_gain, treble_gain, subsonic_freq)."""
        if "bass_gain" in params:
            self.params["bass_gain"] = round(max(-6.0, min(8.0, float(params["bass_gain"]))), 1)
        if "treble_gain" in params:
            self.params["treble_gain"] = round(max(-6.0, min(8.0, float(params["treble_gain"]))), 1)
        if "subsonic_freq" in params:
            self.params["subsonic_freq"] = int(max(30, min(80, float(params["subsonic_freq"]))))

        self.active_profile = "custom"
        return self._apply_custom_state(save=save)

    def _apply_custom_state(self, save: bool = True) -> Dict[str, Any]:
        """Compile and write the custom filter state to hardware."""
        cmds: List[Tuple[str, str, str]] = []
        cmds.extend(self._build_mute_cmds())

        # 1. Notch 120 Hz
        if self.filters.get("notch120"):
            notch = GOLDEN_HIFI["woofer"]["0x3A"]
        else:
            notch = BYPASS_HEX
        cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3A {notch}"))

        # 2. Subsonic HPF 4th-order
        if self.filters.get("subsonic"):
            f_sub = self.params.get("subsonic_freq", 48)
            s1 = calc_hpf_stage(f_sub, 0.5412)
            s2 = calc_hpf_stage(f_sub, 1.3065)
        else:
            s1 = BYPASS_HEX
            s2 = BYPASS_HEX
        cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3B {s1}"))
        cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3C {s2}"))

        # 3. Bass boost / shelf
        bgain = self.params.get("bass_gain", 0.0)
        if abs(bgain) > 0.1:
            bass_biquad = calc_low_shelf(60, 0.85, bgain)
        else:
            bass_biquad = BYPASS_HEX
        cmds.append((WOOFER_SYS, ADDR_WOOFER, f"3D {bass_biquad}"))

        # 4. Tweeter Phase APF-2
        if self.filters.get("phase"):
            apf = GOLDEN_HIFI["tweeter"]["0x38"]
        else:
            apf = BYPASS_HEX
        cmds.append((TWEETER_SYS, ADDR_TWEETER, f"38 {apf}"))

        # 5. Tweeter Treble High-Shelf
        if self.filters.get("treble"):
            tgain = self.params.get("treble_gain", 3.0)
            if abs(tgain - 3.0) < 0.1:
                treble_biquad = GOLDEN_HIFI["tweeter"]["0x3C"]
            else:
                treble_biquad = calc_high_shelf(13500, 0.7, tgain)
        else:
            treble_biquad = BYPASS_HEX
        cmds.append((TWEETER_SYS, ADDR_TWEETER, f"3C {treble_biquad}"))

        # 6. Hardware DRC Compressor (Register 0x60: 00 00 08 00 = Bypass, 00 00 08 01 = Stock protection)
        drc_val = "00 00 08 00" if self.filters.get("drc_bypass") else "00 00 08 01"
        cmds.append((WOOFER_SYS, ADDR_WOOFER, f"60 {drc_val}"))
        cmds.append((TWEETER_SYS, ADDR_TWEETER, f"60 {drc_val}"))

        cmds.extend(self._build_unmute_cmds())

        ok = self._write_i2c_batch(cmds)
        if ok:
            if save:
                self._save_state()
                self._update_boot_script()
            return {"ok": True, "profile": self.active_profile, "filters": self.filters, "params": self.params}
        return {"ok": False, "error": "Ошибка применения параметров DSP"}

    def _update_boot_script(self):
        """Update /tmp/hifi_dsp_applied and persistence script."""
        try:
            flag_file = Path("/tmp/hifi_dsp_applied")
            if self.active_profile == "stock":
                if flag_file.exists():
                    flag_file.unlink(missing_ok=True)
            else:
                flag_file.touch(exist_ok=True)
        except Exception:
            pass

    def calculate_response(self) -> Dict[str, Any]:
        """Compute frequency response curve points for real-time frontend visualization."""
        # 60 log-spaced frequencies from 20 Hz to 20 kHz
        freqs = []
        for i in range(60):
            f = 20.0 * (1000.0 ** (i / 59.0))
            freqs.append(round(f, 1))

        woofer_db = []
        tweeter_db = []
        system_db = []

        subsonic_on = self.filters.get("subsonic", True)
        sub_f0 = self.params.get("subsonic_freq", 48)
        notch_on = self.filters.get("notch120", True)
        bass_gain = float(self.params.get("bass_gain", 0.0))

        treble_on = self.filters.get("treble", True)
        treble_gain = self.params.get("treble_gain", 3.0) if treble_on else 0.0

        # Precalculate biquads for curve evaluation
        w_filters = []
        if subsonic_on:
            # Stage 1: HPF (sub_f0, 0.5412)
            w0 = 2.0 * math.pi * sub_f0 / FS
            alpha = math.sin(w0) / (2.0 * 0.5412)
            cos_w0 = math.cos(w0)
            w_filters.append(((1.0 + cos_w0)/2.0, -(1.0 + cos_w0), (1.0 + cos_w0)/2.0, 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha))
            # Stage 2: HPF (sub_f0, 1.3065)
            alpha2 = math.sin(w0) / (2.0 * 1.3065)
            w_filters.append(((1.0 + cos_w0)/2.0, -(1.0 + cos_w0), (1.0 + cos_w0)/2.0, 1.0 + alpha2, -2.0 * cos_w0, 1.0 - alpha2))

        if notch_on:
            # Notch 120Hz (-4.5dB, Q=2.5)
            A = 10.0 ** (-4.5 / 40.0)
            w0 = 2.0 * math.pi * 120.0 / FS
            alpha = math.sin(w0) / (2.0 * 2.5)
            cos_w0 = math.cos(w0)
            w_filters.append((1.0 + alpha * A, -2.0 * cos_w0, 1.0 - alpha * A, 1.0 + alpha / A, -2.0 * cos_w0, 1.0 - alpha / A))

        if abs(bass_gain) > 0.1:
            A = 10.0 ** (bass_gain / 40.0)
            w0 = 2.0 * math.pi * 60.0 / FS
            alpha = math.sin(w0) / (2.0 * 0.85)
            beta = 2.0 * math.sqrt(A) * alpha
            cos_w0 = math.cos(w0)
            w_filters.append((
                A * ((A + 1.0) - (A - 1.0) * cos_w0 + beta),
                2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0),
                A * ((A + 1.0) - (A - 1.0) * cos_w0 - beta),
                (A + 1.0) + (A - 1.0) * cos_w0 + beta,
                -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0),
                (A + 1.0) + (A - 1.0) * cos_w0 - beta
            ))

        t_filters = []
        if treble_on and abs(treble_gain) > 0.2:
            A = 10.0 ** (treble_gain / 40.0)
            w0 = 2.0 * math.pi * 13500.0 / FS
            alpha = math.sin(w0) / (2.0 * 0.7)
            beta = 2.0 * math.sqrt(A) * alpha
            cos_w0 = math.cos(w0)
            t_filters.append((
                A * ((A + 1.0) + (A - 1.0) * cos_w0 + beta),
                -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0),
                A * ((A + 1.0) + (A - 1.0) * cos_w0 - beta),
                (A + 1.0) - (A - 1.0) * cos_w0 + beta,
                2.0 * ((A - 1.0) - (A + 1.0) * cos_w0),
                (A + 1.0) - (A - 1.0) * cos_w0 - beta
            ))

        for f in freqs:
            # Woofer acoustic passband (acoustic roll-off below 40Hz and above 350Hz)
            w_base = 0.0
            if f > 300.0:
                w_base -= 24.0 * math.log10(f / 300.0)
            w_dsp = sum(biquad_response_db(*b, f) for b in w_filters)
            w_tot = round(max(-40.0, min(12.0, w_base + w_dsp)), 1)
            woofer_db.append(w_tot)

            # Tweeter acoustic passband (acoustic high-pass above 2500Hz)
            t_base = 0.0
            if f < 2500.0:
                t_base -= 24.0 * math.log10(2500.0 / max(f, 20.0))
            t_dsp = sum(biquad_response_db(*b, f) for b in t_filters)
            t_tot = round(max(-40.0, min(12.0, t_base + t_dsp)), 1)
            tweeter_db.append(t_tot)

            # Combined system curve
            sys_db = w_dsp if f < 300.0 else (t_dsp if f > 3000.0 else (w_dsp + t_dsp) / 2.0)
            if self.filters.get("phase", True) and 200.0 <= f <= 350.0:
                sys_db += 2.5  # Phase alignment restoration in crossover region
            system_db.append(round(max(-35.0, min(10.0, sys_db)), 1))

        return {
            "freqs": freqs,
            "woofer_db": woofer_db,
            "tweeter_db": tweeter_db,
            "system_db": system_db,
        }

    def status(self) -> Dict[str, Any]:
        """Return full DSP status payload for YapAnel UI."""
        supported = self.is_supported()
        return {
            "ok": True,
            "supported": supported,
            "active_profile": self.active_profile,
            "profile": self.active_profile,
            "profiles": self.PROFILES,
            "filters": self.filters,
            "params": self.params,
            "response": self.calculate_response() if supported else None,
        }

    def action(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming POST /api/dsp command."""
        if not self.is_supported():
            return {"ok": False, "error": "Аппаратный DSP доступен только на Станции Макс"}

        act = d.get("action")
        if "profile" in d:
            res = self.apply_profile(d["profile"])
            return self.status() if res.get("ok") else res
        elif act == "set_filter" or "filter" in d:
            fid = d.get("filter")
            val = d.get("enabled", True)
            res = self.set_filter(fid, val)
            return self.status() if res.get("ok") else res
        elif act == "set_params" or "params" in d:
            p = d.get("params", d)
            res = self.set_params(p)
            return self.status() if res.get("ok") else res
        elif act == "status":
            return self.status()
        elif act == "reset":
            res = self.apply_profile("stock")
            return self.status() if res.get("ok") else res

        return {"ok": False, "error": "Неизвестное действие DSP"}
