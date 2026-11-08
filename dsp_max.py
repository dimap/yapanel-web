"""
Station Max Hardware DSP Engine for Silergy SY6045S Power Amplifiers.
Integrates with YapAnel: biquad calculation, register flashing,
profile switching, filter toggles, persistence, and frequency response modeling.
"""

import copy
import os
import math
import time
import json
import logging
import functools
import re
import threading
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

logger = logging.getLogger("yapanel.dsp")

# Hardware sysfs interface paths
TWEETER_SYS = "/sys/bus/i2c/drivers/sy6045s/2-002a/settings"
WOOFER_SYS = "/sys/bus/i2c/drivers/sy6045s/2-002b/settings"

# 8-bit I2C addresses used in driver protocol
ADDR_TWEETER = "54"
ADDR_WOOFER = "56"

# Marker used by the boot-time persistence helper.
DSP_FLAG_FILE = Path("/tmp/hifi_dsp_applied")

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


def peaking_stage(f0: float, q: float, gain_db: float, fs: float = FS) -> Tuple[float, float, float, float, float, float]:
    """Peaking / Notch EQ stage without normalization: (b0, b1, b2, a0, a1, a2)."""
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
    return b0, b1, b2, a0, a1, a2


def calc_notch(f0: float, q: float, gain_db: float, fs: float = FS) -> str:
    """Peaking / Notch EQ biquad filter."""
    return calc_biquad_coeffs(*peaking_stage(f0, q, gain_db, fs))


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


# Свободные биквады SY6045S после golden-калибровки.
# Вуфер: 3E/3F/40 (3D занят полкой баса), твитер: 39/3A/3B/3D/3E/3F/40
# (38 — APF, 3C — полка ВЧ). Частоты примерно через октаву; Q=1.41 даёт
# мягкое перекрытие соседних полос, как у графического эквалайзера.
EQ_BANDS = [
    {"id": "40", "freq": 40.0, "q": 1.41, "channel": "woofer", "reg": "3E", "label": "40 Гц"},
    {"id": "80", "freq": 80.0, "q": 1.41, "channel": "woofer", "reg": "3F", "label": "80 Гц"},
    {"id": "160", "freq": 160.0, "q": 1.41, "channel": "woofer", "reg": "40", "label": "160 Гц"},
    {"id": "400", "freq": 400.0, "q": 1.41, "channel": "tweeter", "reg": "39", "label": "400 Гц"},
    {"id": "800", "freq": 800.0, "q": 1.41, "channel": "tweeter", "reg": "3A", "label": "800 Гц"},
    {"id": "1600", "freq": 1600.0, "q": 1.41, "channel": "tweeter", "reg": "3B", "label": "1.6 кГц"},
    {"id": "3150", "freq": 3150.0, "q": 1.41, "channel": "tweeter", "reg": "3D", "label": "3.2 кГц"},
    {"id": "6300", "freq": 6300.0, "q": 1.41, "channel": "tweeter", "reg": "3E", "label": "6.3 кГц"},
    {"id": "12500", "freq": 12500.0, "q": 1.41, "channel": "tweeter", "reg": "3F", "label": "12.5 кГц"},
    {"id": "16000", "freq": 16000.0, "q": 1.41, "channel": "tweeter", "reg": "40", "label": "16 кГц"},
]
EQ_DEFAULT = [0.0] * len(EQ_BANDS)
# Подъём ограничен +9 дБ: полка ВЧ и другие фильтры добавляют ещё немного,
# а большой буст низов легко перегружает маленький динамик.
EQ_MIN, EQ_MAX = -12.0, 9.0


def _serialized_hardware(method):
    """Run a method under the shared RLock used by every MaxDSP instance.

    The lock is re-entrant because a single public operation (for example
    ``apply_profile``) intentionally nests other hardware helpers
    (``_apply_custom_state`` -> ``_write_i2c_batch``).
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class MaxDSP:
    """Manages Station Max SY6045S hardware DSP filters and presets."""

    # Shared by all instances: max_profile.py may create more than one MaxDSP
    # object, and the HTTP server handles requests in parallel threads.
    _hw_lock = threading.RLock()
    _lock = _hw_lock

    PROFILES = [
        {
            "id": "hifi",
            "name": "Студийный звук",
            "badge": "Студия",
            "desc": "Ровный, чистый звук: без дрожи мебели, гула корпуса и отставания баса.",
        },
        {
            "id": "stock",
            "name": "Как с завода",
            "badge": "Завод",
            "desc": "Стандартное звучание без обработки — чтобы сравнить с настройками панели.",
        },
        {
            "id": "night",
            "name": "Ночной режим",
            "badge": "Ночь",
            "desc": "Меньше низких и мягче высокие: музыка не уходит к соседям.",
        },
        {
            "id": "bass",
            "name": "Плотный бас",
            "badge": "Бас",
            "desc": "Упругий бас без гула и раскачки корпуса.",
        },
        {
            "id": "club",
            "name": "Клубный бас",
            "badge": "Клуб",
            "desc": "Больше низов для вечеринки, но вокал остаётся чистым.",
        },
        {
            "id": "custom",
            "name": "Свои настройки",
            "badge": "Своё",
            "desc": "Ручная настройка баса и фильтров под ваш вкус.",
        },
    ]

    def __init__(self, panel):
        self.panel = panel
        self.adb = getattr(panel, "adb", None)
        self.active_profile = "hifi"
        self._restored = False
        self._restore_attempts = 0
        self._last_restore_ts = 0.0
        self._last_reapply_ts = 0.0
        self._expected_regs: Dict[Tuple[str, str], str] = {}
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
            "eq": list(EQ_DEFAULT),
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
                    except Exception as e:
                        logger.warning("[DSP] Failed to read fallback settings %s: %s", direct_path, e)
            if isinstance(dsp_conf, dict):
                self.active_profile = dsp_conf.get("profile", "hifi")
                if "filters" in dsp_conf and isinstance(dsp_conf["filters"], dict):
                    self.filters.update(dsp_conf["filters"])
                if "params" in dsp_conf and isinstance(dsp_conf["params"], dict):
                    self.params.update(dsp_conf["params"])
                self.params["eq"] = self._eq_values()
        except Exception as e:
            logger.warning(f"Error loading DSP settings: {e}")

    @_serialized_hardware
    def restore_hardware_state(self) -> Dict[str, Any]:
        """Restore saved DSP configuration to hardware on panel boot/startup."""
        if not self.is_supported():
            return {"ok": False, "error": "Hardware not supported"}
        # Не перечитываем настройки с диска: сюда повторно заходят из
        # maybe_restore после сбоя шины, и старая копия файла затирала бы
        # только что выбранный пользователем эквалайзер.
        logger.info(f"[DSP] Restoring hardware: profile={self.active_profile}, params={self.params}, filters={self.filters}")
        if self.active_profile == "custom":
            res = self._apply_custom_state(save=False)
        else:
            res = self.apply_profile(self.active_profile, save=False)
        if res.get("ok"):
            self._restored = True
            self._restore_attempts = 0
            self._last_restore_ts = time.time()
        else:
            self._restored = False
            self._restore_attempts += 1
        return res

    def maybe_restore(self) -> bool:
        """Повторяет restore с backoff, пока он не завершится успешно.

        Вызывается из GET /api/dsp, поэтому в успешном состоянии это только
        проверка флага. После неудачи (шина занята, драйвер ещё не готов)
        попытки идут с нарастающей паузой, а не на каждый запрос.
        """
        if self._restored:
            return True
        if not self.is_supported():
            return False
        now = time.time()
        delay = min(300, 10 * (2 ** min(self._restore_attempts, 5)))
        if now - self._last_restore_ts < delay:
            return False
        self._last_restore_ts = now
        before = self._restore_attempts
        res = self.restore_hardware_state()
        ok = bool(res.get("ok"))
        if ok:
            self._restore_attempts = 0
        elif self._restore_attempts == before:
            self._restore_attempts = before + 1
        return ok

    @staticmethod
    def _expected_from_commands(commands: List[Tuple[str, str, str]]) -> Dict[Tuple[str, str], str]:
        """Собирает ожидаемые EQ/DRC-регистры из уже готового списка команд.

        Mute/unmute-регистры (03/06/22) и паузы исключены: после успешного
        батча они возвращаются в рабочее состояние и не являются профилем.
        """
        expected: Dict[Tuple[str, str], str] = {}
        for target, _addr, payload in commands:
            if target == "__SLEEP__":
                continue
            parts = str(payload).split(None, 1)
            if len(parts) != 2:
                continue
            reg = parts[0].upper()
            if reg in ("03", "06", "22"):
                continue
            expected[(target, reg)] = " ".join(str(payload).upper().split())
        return expected

    def _remember_expected(self, commands: List[Tuple[str, str, str]]) -> None:
        self._expected_regs = self._expected_from_commands(commands)

    @staticmethod
    def _register_values(text: str) -> Dict[str, str]:
        """Достаёт пары «регистр → значение» из ответа sysfs.

        Формат драйвера не документирован: это может быть `3A FC ...`, строка
        с `w 54 3A ...` или полный дамп. Ищем hex-токены регистров и берём
        следующие за ними байты. Пустой/частичный ответ не считается ошибкой.
        """
        tokens = str(text or "").upper().split()
        values: Dict[str, str] = {}
        for index, token in enumerate(tokens):
            if re.fullmatch(r"[0-9A-F]{2}", token):
                tail = tokens[index + 1:index + 21]
                if tail and all(re.fullmatch(r"[0-9A-F]{2}", item) for item in tail):
                    values[token] = " ".join(tail)
        return values

    @_serialized_hardware
    def verify_registers(self) -> Optional[bool]:
        """True — регистры совпадают, False — явное расхождение, None — нет read-back."""
        expected = dict(self._expected_regs)
        if not expected:
            return None
        by_target: Dict[str, List[Tuple[str, str]]] = {}
        for (target, reg), value in expected.items():
            by_target.setdefault(target, []).append((reg, value))

        texts: Dict[str, str] = {}
        is_local = os.path.exists(WOOFER_SYS) and os.path.exists(TWEETER_SYS)
        if is_local:
            for target in by_target:
                try:
                    with open(target, "r", errors="replace") as f:
                        texts[target] = f.read(16384)
                except OSError:
                    return None
        else:
            if not self.adb:
                return None
            try:
                command = "; echo __DSP_SEP__; ".join(f"cat {target}" for target in by_target)
                out, _err, code = self.adb.shell(command, check=False)
                if code != 0:
                    return None
                parts = str(out).split("__DSP_SEP__")
                if len(parts) != len(by_target):
                    return None
                texts = dict(zip(by_target, parts))
            except Exception:
                return None

        found = 0
        for target, pairs in by_target.items():
            text = texts.get(target, "")
            if not text.strip():
                return None
            values = self._register_values(text)
            for reg, expected_value in pairs:
                actual = values.get(reg)
                if actual is None:
                    continue
                found += 1
                expected_bytes = " ".join(expected_value.split()[1:]).upper()
                if actual != expected_bytes:
                    logger.info("[DSP] Register %s on %s changed: expected %s, got %s",
                                reg, target, expected_bytes, actual)
                    return False
        if not found:
            return None
        # Полный дамп: всё нашли и всё совпало. Частичный — не приговор.
        return True if found == len(expected) else None

    @_serialized_hardware
    def ensure_applied(self, force: bool = False) -> Optional[bool]:
        """Проверяет/возвращает сохранённый профиль после потери регистров."""
        if not self._expected_regs or not self.is_supported():
            return None
        verified = self.verify_registers()
        if verified is True:
            return True
        now = time.time()
        cooldown = 5.0 if force else 15.0
        if now - self._last_reapply_ts < cooldown:
            return False
        if verified is False or force:
            logger.info("[DSP] Re-applying %s profile (verify=%s, force=%s)",
                        self.active_profile, verified, force)
            self._last_reapply_ts = now
            if self.active_profile == "custom":
                res = self._apply_custom_state(save=False)
            else:
                res = self.apply_profile(self.active_profile, save=False)
            if res.get("ok"):
                self._restored = True
                self._restore_attempts = 0
                return True
            self._restored = False
            self._restore_attempts += 1
            return False
        return None

    def _save_state(self) -> bool:
        """Save the DSP section to panel settings and verify persistence.

        New server.py exposes ``update_settings_section`` so only the dsp
        section is replaced under a shared lock.  Older panels fall back to a
        whole-file read-modify-write with a read-back check.
        """
        try:
            import server
        except Exception as e:
            logger.error("[DSP] Cannot import server to save DSP settings: %s", e, exc_info=True)
            return False

        try:
            section = {
                "profile": self.active_profile,
                "filters": dict(self.filters),
                "params": dict(self.params),
            }
            # Новый server.py умеет атомарно менять только dsp-секцию, не
            # затирая mute/EQ/wakeword, которые пишутся параллельно.
            update_section = getattr(server, "update_settings_section", None)
            if callable(update_section):
                if update_section("dsp", section) is False:
                    logger.error("[DSP] update_settings_section('dsp') reported a write failure")
                    return False
                return True

            # Совместимость с панелью старой версии на колонке.
            s = server.load_settings()
            if not isinstance(s, dict):
                logger.error("[DSP] Settings payload is not a dict; refusing to save DSP state")
                return False
            s["dsp"] = section
            if server.save_settings(s) is False:
                logger.error("[DSP] server.save_settings() reported a write failure")
                return False
            persisted = server.load_settings()
            saved_section = persisted.get("dsp") if isinstance(persisted, dict) else None
            if saved_section != section:
                logger.error("[DSP] DSP settings were not persisted; check settings file permissions/disk")
                return False
            return True
        except Exception as e:
            logger.error("[DSP] Error saving DSP settings: %s", e, exc_info=True)
            return False

    @staticmethod
    def _parse_eq(raw):
        """Проверяет список полос эквалайзера. Возвращает (значения, ошибка)."""
        if not isinstance(raw, (list, tuple)):
            return None, "Эквалайзер должен быть списком полос"
        if len(raw) != len(EQ_BANDS):
            return None, f"Нужно ровно {len(EQ_BANDS)} полос"
        values = []
        for index, item in enumerate(raw):
            if isinstance(item, bool):
                return None, f"Некорректное значение полосы {index + 1}"
            try:
                value = float(item)
            except (TypeError, ValueError, OverflowError):
                return None, f"Некорректное значение полосы {index + 1}"
            if not math.isfinite(value):
                return None, f"Некорректное значение полосы {index + 1}"
            values.append(round(max(EQ_MIN, min(EQ_MAX, value)), 1))
        return values, ""

    def _eq_values(self):
        """Текущие полосы в безопасном виде: 10 чисел в диапазоне -12…+9 дБ."""
        values, _error = self._parse_eq(self.params.get("eq"))
        return values if values is not None else list(EQ_DEFAULT)

    @_serialized_hardware
    def set_eq(self, bands, save: bool = True) -> Dict[str, Any]:
        """Задаёт 10-полосный эквалайзер (дБ) и применяет его к железу."""
        values, error = self._parse_eq(bands)
        if error:
            return {"ok": False, "error": error}
        old_profile = self.active_profile
        old_params = copy.deepcopy(self.params)
        self.params["eq"] = values
        self.active_profile = "custom"
        res = self._apply_custom_state(save=save)
        if not res.get("ok"):
            self.active_profile, self.params = old_profile, old_params
        return res

    def is_supported(self) -> bool:
        """Check if Silergy SY6045S dual amp hardware is present."""
        if os.path.exists(WOOFER_SYS) and os.path.exists(TWEETER_SYS):
            return True
        if self.adb:
            try:
                out, _, _ = self.adb.shell(
                    f"test -e {WOOFER_SYS} && echo 1 || echo 0", check=False
                )
                return "1" in str(out or "")
            except Exception as e:
                logger.warning("[DSP] Hardware probe via ADB failed: %s", e)
                return False
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

    def _verify_local_write(self, target_path: str, addr: str, payload: str) -> Optional[bool]:
        """Best-effort read-back of a sysfs write. Never raises.

        Returns True on a matching read-back, False on a mismatch, and None
        when the file is not readable (write-only sysfs entries are normal).
        A verification failure must not turn a successful write into an error.
        """
        try:
            if not os.access(target_path, os.R_OK):
                logger.debug("[DSP] Read-back unavailable for %s (not readable)", target_path)
                return None
            with open(target_path, "r", errors="replace") as f:
                data = f.read(4096)
        except Exception as e:  # best-effort: a read-back problem is never fatal
            logger.debug("[DSP] Read-back failed for %s: %s", target_path, e)
            return None

        if not data.strip():
            # Some sysfs attributes accept writes but read back empty.
            logger.debug("[DSP] Read-back of %s is empty; cannot verify", target_path)
            return None

        normalized = " ".join(data.split()).upper()
        expected = " ".join(str(payload).split()).upper()
        if expected and expected not in normalized:
            logger.warning(
                "[DSP] Read-back mismatch for %s: wrote %r, got %r",
                target_path, expected, data[:200],
            )
            return False
        return True

    @_serialized_hardware
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
                        except OSError as e:
                            logger.debug("[DSP] Write attempt %d failed for %s: %s", attempt + 1, target_path, e)
                            if attempt < 2:
                                time.sleep(0.008)
                    if not written:
                        logger.error(f"Local I2C write failed for {target_path} after 3 attempts")
                        return False
                    # Advisory only: never fail the batch because of read-back.
                    try:
                        self._verify_local_write(target_path, addr, payload)
                    except Exception as e:
                        logger.debug("[DSP] Read-back helper error for %s: %s", target_path, e)
                return True
            except Exception as e:
                logger.error(f"Local I2C write error: {e}", exc_info=True)
                return False
        else:
            # Remote via ADB shell
            if not self.adb:
                logger.error("[DSP] Cannot write DSP: no local sysfs and no ADB connection")
                return False
            try:
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
                if code != 0:
                    logger.error("[DSP] Remote I2C batch failed (code=%s): %s", code, (err or out or "").strip()[:300])
                    return False
                return True
            except Exception as e:
                logger.error("[DSP] Remote I2C write error: %s", e, exc_info=True)
                return False

    @_serialized_hardware
    def apply_profile(self, profile_name: str, save: bool = True) -> Dict[str, Any]:
        """Apply a named DSP profile to hardware."""
        valid_ids = [p["id"] for p in self.PROFILES]
        if not isinstance(profile_name, str) or profile_name not in valid_ids:
            return {"ok": False, "error": f"Неизвестный профиль: {profile_name}"}

        if profile_name == "custom":
            return self._apply_custom_state(save=save)

        old_profile = self.active_profile
        old_filters = copy.deepcopy(self.filters)
        old_params = copy.deepcopy(self.params)
        self.params["eq"] = list(EQ_DEFAULT)
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

        # Любой пресет сбрасывает пользовательский эквалайзер: свободные слоты — bypass.
        for band in EQ_BANDS:
            if band["channel"] == "woofer":
                cmds.append((WOOFER_SYS, ADDR_WOOFER, f"{band['reg']} {BYPASS_HEX}"))
            else:
                cmds.append((TWEETER_SYS, ADDR_TWEETER, f"{band['reg']} {BYPASS_HEX}"))

        # 6. Hardware DRC Compressor (Register 0x60: 00 00 08 00 = Bypass, 00 00 08 01 = Stock protection)
        drc_val = "00 00 08 00" if self.filters.get("drc_bypass") else "00 00 08 01"
        cmds.append((WOOFER_SYS, ADDR_WOOFER, f"60 {drc_val}"))
        cmds.append((TWEETER_SYS, ADDR_TWEETER, f"60 {drc_val}"))

        cmds.extend(self._build_unmute_cmds())

        ok = self._write_i2c_batch(cmds)
        if ok:
            self.active_profile = profile_name
            self._remember_expected(cmds)
            self._restored = True
            self._restore_attempts = 0
            if save:
                self._save_state()
                self._update_boot_script()
            return {"ok": True, "profile": profile_name}
        # Не оставляем в памяти профиль, которого нет в железе и настройках.
        self.active_profile, self.filters, self.params = old_profile, old_filters, old_params
        return {"ok": False, "error": "Ошибка записи в I2C драйвер SY6045S"}

    @_serialized_hardware
    def set_filter(self, filter_id: str, enabled: bool, save: bool = True) -> Dict[str, Any]:
        """Toggle an individual filter on or off."""
        if not isinstance(filter_id, str):
            return {"ok": False, "error": "Некорректный идентификатор фильтра"}
        if filter_id not in self.filters:
            return {"ok": False, "error": f"Неизвестный фильтр: {filter_id}"}

        if isinstance(enabled, bool):
            enabled_bool = enabled
        elif isinstance(enabled, int) and enabled in (0, 1):
            enabled_bool = bool(enabled)
        elif isinstance(enabled, str) and enabled.strip().lower() in ("true", "false", "1", "0", "on", "off"):
            enabled_bool = enabled.strip().lower() in ("true", "1", "on")
        else:
            return {"ok": False, "error": "Параметр enabled должен быть boolean"}

        old_profile = self.active_profile
        old_filters = copy.deepcopy(self.filters)
        self.filters[filter_id] = enabled_bool
        self.active_profile = "custom"
        res = self._apply_custom_state(save=save)
        if not res.get("ok"):
            self.active_profile, self.filters = old_profile, old_filters
        return res

    @_serialized_hardware
    def set_params(self, params: Dict[str, Any], save: bool = True) -> Dict[str, Any]:
        """Update custom parameters (bass_gain, treble_gain, subsonic_freq)."""
        if not isinstance(params, dict):
            return {"ok": False, "error": "Параметры DSP должны быть объектом"}

        updates: Dict[str, Any] = {}
        for key in ("bass_gain", "treble_gain"):
            if key not in params:
                continue
            raw = params[key]
            if isinstance(raw, bool):
                return {"ok": False, "error": f"Некорректное значение {key}"}
            try:
                val = float(raw)
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "error": f"Некорректное значение {key}"}
            if not math.isfinite(val):
                return {"ok": False, "error": f"Некорректное значение {key}"}
            updates[key] = round(max(-6.0, min(8.0, val)), 1)

        if "subsonic_freq" in params:
            raw = params["subsonic_freq"]
            if isinstance(raw, bool):
                return {"ok": False, "error": "Некорректное значение subsonic_freq"}
            try:
                val = float(raw)
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "error": "Некорректное значение subsonic_freq"}
            if not math.isfinite(val):
                return {"ok": False, "error": "Некорректное значение subsonic_freq"}
            updates["subsonic_freq"] = int(max(30, min(80, val)))

        if "eq" in params:
            values, error = self._parse_eq(params["eq"])
            if error:
                return {"ok": False, "error": error}
            updates["eq"] = values

        if not updates:
            return {"ok": False, "error": "Не заданы параметры DSP"}

        old_profile = self.active_profile
        old_params = copy.deepcopy(self.params)
        self.params.update(updates)
        self.active_profile = "custom"
        res = self._apply_custom_state(save=save)
        if not res.get("ok"):
            self.active_profile, self.params = old_profile, old_params
        return res

    @_serialized_hardware
    def _apply_custom_state(self, save: bool = True) -> Dict[str, Any]:
        """Compile and write the custom filter state to hardware."""
        old_profile = self.active_profile
        old_filters = copy.deepcopy(self.filters)
        old_params = copy.deepcopy(self.params)
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

        # 3b. 10-полосный эквалайзер в свободных слотах усилителей.
        for band, gain in zip(EQ_BANDS, self._eq_values()):
            payload = BYPASS_HEX if abs(gain) < 0.1 else calc_notch(band["freq"], band["q"], gain)
            if band["channel"] == "woofer":
                cmds.append((WOOFER_SYS, ADDR_WOOFER, f"{band['reg']} {payload}"))
            else:
                cmds.append((TWEETER_SYS, ADDR_TWEETER, f"{band['reg']} {payload}"))

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
            self._remember_expected(cmds)
            self._restored = True
            self._restore_attempts = 0
            if save:
                self._save_state()
                self._update_boot_script()
            return {"ok": True, "profile": self.active_profile, "filters": self.filters, "params": self.params}
        self.active_profile, self.filters, self.params = old_profile, old_filters, old_params
        return {"ok": False, "error": "Ошибка применения параметров DSP"}

    def _update_boot_script(self) -> bool:
        """Ensure the boot watchdog marker exists for every profile.

        The external watchdog applies golden Hi-Fi whenever the marker is
        missing.  Deleting it for ``stock`` therefore re-enabled Hi-Fi a couple
        of seconds later.  The marker now means "the panel owns the DSP state",
        not "the Hi-Fi profile is active".
        """
        flag_file = Path(DSP_FLAG_FILE)
        is_local = getattr(self.adb, "is_local", False) or not self.adb
        try:
            if is_local:
                flag_file.touch(exist_ok=True)
                logger.debug("[DSP] Updated boot marker %s", flag_file)
            else:
                self.adb.shell(": > /tmp/hifi_dsp_applied", check=False)
                logger.debug("[DSP] Updated boot marker on device")
            return True
        except OSError as e:
            logger.warning("[DSP] Cannot update boot marker %s: %s", flag_file, e)
            return False
        except Exception as e:
            logger.error("[DSP] Unexpected error updating boot marker %s: %s", flag_file, e, exc_info=True)
            return False

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

        # 10-полосный эквалайзер: те же биквады, что уходят в свободные слоты.
        for band, gain in zip(EQ_BANDS, self._eq_values()):
            if abs(gain) < 0.1:
                continue
            stage = peaking_stage(band["freq"], band["q"], gain)
            (w_filters if band["channel"] == "woofer" else t_filters).append(stage)

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

            # Combined system curve: вуфер ведёт низы, твитер — середину и верх;
            # между 150 и 350 Гц плавный переход, как у реального кроссовера.
            if f <= 150.0:
                sys_db = w_dsp
            elif f >= 350.0:
                sys_db = t_dsp
            else:
                mix = math.log10(f / 150.0) / math.log10(350.0 / 150.0)
                sys_db = w_dsp * (1.0 - mix) + t_dsp * mix
            system_db.append(round(max(-35.0, min(12.0, sys_db)), 1))

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
            "eq": self._eq_values(),
            "eq_bands": EQ_BANDS,
            "response": self.calculate_response() if supported else None,
        }

    @_serialized_hardware
    def action(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming POST /api/dsp command."""
        if not isinstance(d, dict):
            return {"ok": False, "error": "Запрос DSP должен быть объектом"}

        act = d.get("action")
        if act is not None and not isinstance(act, str):
            return {"ok": False, "error": "Некорректное действие DSP"}

        if not self.is_supported():
            return {"ok": False, "error": "Аппаратный DSP доступен только на Станции Макс"}

        if act == "status":
            return self.status()
        if act == "reset":
            res = self.apply_profile("stock")
            return self.status() if res.get("ok") else res

        if act == "set_filter":
            res = self.set_filter(d.get("filter"), d.get("enabled", True))
        elif act == "set_eq":
            res = self.set_eq(d.get("bands", d.get("eq")))
        elif act == "set_params":
            res = self.set_params(d.get("params", d))
        elif act == "profile":
            res = self.apply_profile(d.get("profile"))
        elif act is None and "profile" in d:
            res = self.apply_profile(d["profile"])
        elif act is None and "filter" in d:
            res = self.set_filter(d.get("filter"), d.get("enabled", True))
        elif act is None and "eq" in d:
            res = self.set_eq(d.get("eq"))
        elif act is None and "params" in d:
            res = self.set_params(d.get("params"))
        else:
            return {"ok": False, "error": "Неизвестное действие DSP"}

        return self.status() if res.get("ok") else res
