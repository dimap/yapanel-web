"""Надёжное хранилище Party Mode.

Все JSON-файлы светомузыки пишутся атомарно и только через один lock.
Битый файл не удаляется: рядом остаётся копия ``*.corrupt-*``, а панель
продолжает работать с безопасными значениями по умолчанию.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_SETTINGS = {
    "master_enabled": True,
    "active_mode": "cinema",
    "speed": 1.0,
    "brightness": 80,
    "bt_mode": "off",
    "color_interval": 32,
    "sync_mode": "split",
    "silence_timeout": 10.0,
    "ambilight_target": "strip_only",
    "effects_enabled": True,
    "effects_target": "bulbs_only",
}

MODES = ("party", "fireplace", "aurora", "synthwave", "thunderstorm", "ocean", "lava", "orbit", "police", "cinema")
BT_MODES = ("off", "party", "ambilight")
SYNC_MODES = ("unison", "split")
AMBILIGHT_TARGETS = ("strip_only", "bulbs_only", "all", "off")
EFFECTS_TARGETS = ("bulbs_only", "sconces_only", "all", "strip_only", "off")
ROLES = ("kick", "snare", "ambient")
SIDES = ("", "left", "right")


def clamp(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(high, number))


def clamp_int(value, low, high, default):
    return int(clamp(value, low, high, default))


def as_bool(value, default=False):
    """Строгий bool: строки "false"/"0"/"off" не должны включать свет."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return bool(value)


def normalize_settings(raw):
    source = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)
    out["master_enabled"] = as_bool(source.get("master_enabled", out["master_enabled"]), out["master_enabled"])
    active = str(source.get("active_mode", out["active_mode"])).strip().lower()
    out["active_mode"] = active if active in MODES else out["active_mode"]
    out["speed"] = clamp(source.get("speed"), 0.2, 2.5, out["speed"])
    out["brightness"] = clamp_int(source.get("brightness"), 10, 100, out["brightness"])
    bt_mode = str(source.get("bt_mode", out["bt_mode"])).strip().lower()
    out["bt_mode"] = bt_mode if bt_mode in BT_MODES else out["bt_mode"]
    out["color_interval"] = clamp_int(source.get("color_interval"), 1, 256, out["color_interval"])
    sync_mode = str(source.get("sync_mode", out["sync_mode"])).strip().lower()
    out["sync_mode"] = sync_mode if sync_mode in SYNC_MODES else out["sync_mode"]
    out["silence_timeout"] = clamp(source.get("silence_timeout"), 1.0, 120.0, out["silence_timeout"])
    ambilight = str(source.get("ambilight_target", out["ambilight_target"])).strip().lower()
    out["ambilight_target"] = ambilight if ambilight in AMBILIGHT_TARGETS else out["ambilight_target"]
    out["effects_enabled"] = as_bool(source.get("effects_enabled", out["effects_enabled"]), out["effects_enabled"])
    effects = str(source.get("effects_target", out["effects_target"])).strip().lower()
    out["effects_target"] = effects if effects in EFFECTS_TARGETS else out["effects_target"]
    return out


def normalize_bulb(item):
    if not isinstance(item, dict):
        return None
    bulb_id = str(item.get("id") or item.get("dev_id") or item.get("gwId") or "").strip()
    if not bulb_id:
        return None
    ip = str(item.get("ip") or "").strip()
    key = str(item.get("key") or item.get("local_key") or "").strip()
    name = str(item.get("name") or f"Лампа {ip or bulb_id[:6]}").strip()[:80]
    role = str(item.get("role", "kick")).strip().lower()
    if role not in ROLES:
        role = "kick"
    side = str(item.get("side", "")).strip().lower()
    if side not in SIDES:
        side = ""
    if "pos_x" in item:
        pos_x = clamp(item.get("pos_x"), -5.0, 5.0, -1.0 if side == "left" else (1.0 if side == "right" else 0.0))
    else:
        pos_x = -1.0 if side == "left" else (1.0 if side == "right" else 0.0)
    pos_y = clamp(item.get("pos_y"), -5.0, 5.0, 0.0)
    ver = clamp(item.get("ver", item.get("version", 3.5)), 3.1, 3.5, 3.5)
    return {
        "id": bulb_id,
        "name": name or "Лампа",
        "ip": ip,
        "key": key,
        "ver": round(ver, 1),
        "role": role,
        "side": side,
        "pos_x": pos_x,
        "pos_y": pos_y,
        "enabled": as_bool(item.get("enabled", True), True),
    }


def normalize_bulbs(raw):
    if isinstance(raw, dict):
        if isinstance(raw.get("bulbs"), list):
            raw = raw["bulbs"]
        elif isinstance(raw.get("devices"), list):
            raw = raw["devices"]
        else:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for item in raw:
        bulb = normalize_bulb(item)
        if bulb and bulb["id"] not in seen:
            out.append(bulb)
            seen.add(bulb["id"])
    return out


class PartyStore:
    """Locked, atomic JSON store for Party settings and bulbs."""

    def __init__(self, settings_path, bulbs_path, strip_path=None, log=None):
        self.settings_path = Path(settings_path) if settings_path else None
        self.bulbs_path = Path(bulbs_path) if bulbs_path else None
        self.strip_path = Path(strip_path) if strip_path else None
        self.log = log or print
        self.last_error = None
        self._last_error_path = None
        self._lock = threading.RLock()

    # ---------- низкий уровень ----------
    def _backup_corrupt(self, path, error):
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        self.last_error = f"{path.name}: {error}"
        self._last_error_path = path
        self.log(f"[Party] Corrupt {path.name}: {error}; backup: {backup}")

    def _read(self, path):
        if not path or not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as error:
            self._backup_corrupt(path, error)
            return None

    def _write(self, path, data):
        if not path:
            return
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                os.replace(temporary, path)
                if path == self._last_error_path:
                    self.last_error = None
                    self._last_error_path = None
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    # ---------- чтение / запись ----------
    def load_settings(self):
        with self._lock:
            return normalize_settings(self._read(self.settings_path))

    def save_settings(self, settings):
        with self._lock:
            normalized = normalize_settings(settings)
            self._write(self.settings_path, normalized)
            return normalized

    def load_bulbs(self):
        with self._lock:
            return normalize_bulbs(self._read(self.bulbs_path))

    def save_bulbs(self, bulbs):
        with self._lock:
            normalized = normalize_bulbs(bulbs)
            self._write(self.bulbs_path, normalized)
            return normalized

    def update_bulbs(self, mutator):
        """Атомарно прочитать, изменить и записать список ламп."""
        with self._lock:
            items = self.load_bulbs()
            changed = mutator(items)
            if isinstance(changed, list):
                items = changed
            self._write(self.bulbs_path, normalize_bulbs(items))
            return normalize_bulbs(items)

    def update_settings(self, mutator):
        with self._lock:
            settings = self.load_settings()
            changed = mutator(settings)
            if isinstance(changed, dict):
                settings = changed
            self._write(self.settings_path, normalize_settings(settings))
            return normalize_settings(settings)

    def load_strip(self):
        with self._lock:
            raw = self._read(self.strip_path)
            return raw if isinstance(raw, dict) else {}

    def save_strip(self, data):
        with self._lock:
            self._write(self.strip_path, data if isinstance(data, dict) else {})
