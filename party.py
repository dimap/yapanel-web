#!/usr/bin/env python3
"""
Yandex Station Party Mode & Cinema Engine — Native Yapanel Module
Manages:
- Movie Ambilight 4D with high-priority spell overlays (Harry Potter 3, Avada Kedavra, Patronus, etc.)
- Standalone atmospheric effects (Fireplace/Камин, Police/Полиция, Carousel/Карусель)
- Dual-Band reactive audio strobe (Chandelier Kick, Sconces Snare)
- Tuya local socket communication with self-healing auto-reconnect
- Official Yandex Smart Home REST API sync for Zigbee LED strip
"""

import os
import sys
import time
import math
import json
import re
import hashlib
import struct
import socket
import select
import random
import threading
from concurrent.futures import ThreadPoolExecutor
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

from party_store import (
    PartyStore,
    as_bool,
    clamp,
    clamp_int,
    normalize_bulb,
    normalize_bulbs,
    normalize_settings,
)
from party_audio import BeatDetector

try:
    import tinytuya
except ImportError:
    for sp in ["/data/python/lib/python3.11/site-packages", "/data/python/lib/python3.10/site-packages"]:
        if sp not in sys.path and os.path.exists(sp):
            sys.path.append(sp)
    try:
        import tinytuya
    except ImportError:
        tinytuya = None


class _Cancelled(Exception):
    pass


def run_android_cmd(args, ctx="u:r:shell:s0", timeout=2.0):
    def _pre():
        try:
            with open("/proc/self/attr/current", "w") as f:
                f.write(ctx)
        except Exception:
            pass
    return subprocess.run(args, capture_output=True, text=True, preexec_fn=_pre, timeout=timeout)


# =====================================================================
#  Bulb & Strip Workers (Low-Level Device Communication)
# =====================================================================

class WorkersDict(dict):
    """Dict mapping bulb IDs to BulbWorker instances that also supports
    direct iteration over worker instances for clean thread inspection."""
    def __iter__(self):
        return iter(self.values())


class BulbWorker(threading.Thread):
    def __init__(self, dev_info, manager):
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self.name = dev_info.get("name", "Bulb")
        self.dev_id = dev_info["id"]
        self.key = dev_info["key"]
        self.ip = dev_info.get("ip", "").strip()
        self.ver = float(dev_info.get("ver", 3.5))
        self.role = dev_info.get("role", "kick")  # "kick", "snare", "ambient"
        self.side = dev_info.get("side", "")
        # Spatial positioning: -1.0 (far left) to 1.0 (far right), 0.0 (center)
        if "pos_x" in dev_info:
            self.pos_x = float(dev_info["pos_x"])
        elif self.side == "left":
            self.pos_x = -1.0
        elif self.side == "right":
            self.pos_x = 1.0
        else:
            self.pos_x = 0.0
        self.pos_y = float(dev_info.get("pos_y", 0.0))
        self.manager = manager
        self._manager_generation = getattr(manager, "_run_generation", 0)
        
        self.device = None
        self.connected = False
        self.last_version = 0
        self.is_white = False
        self.last_reconnect_time = 0.0
        self.last_heartbeat = time.time()
        self.current_val = None
        self.last_error = None
        self.reconnect_count = 0
        self.is_on = True
        self._restore_on_exit = True
        self._io_lock = threading.RLock()

    def connect(self):
        with self._io_lock:
            return self._connect_locked()

    def _connect_locked(self):
        if not tinytuya or not self.ip or self.ip == "Unknown IP":
            return False
        try:
            if self.device:
                try:
                    self.device.close()
                except Exception:
                    pass
            d = tinytuya.BulbDevice(self.dev_id, self.ip, self.key)
            d.set_version(self.ver)
            d.set_socketPersistent(True)
            try:
                from config import get_config
                _cfg_party = get_config().party
            except ImportError:
                _cfg_party = None
            d.set_socketTimeout(_cfg_party.socket_timeout if _cfg_party else 0.25)
            d.socketRetryLimit = _cfg_party.socket_retry if _cfg_party else 1
            d.socketRetryDelay = _cfg_party.socket_delay if _cfg_party else 0.05
            d.set_multiple_values({"20": bool(self.is_on), "21": "colour"}, nowait=True)
            sock = getattr(d, "socket", None)
            if not sock or not hasattr(sock, "fileno") or sock.fileno() == -1:
                self.connected = False
                self.device = None
                return False
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            self.device = d
            self.connected = True
            self.is_white = False
            self.last_heartbeat = time.time()
            self.last_error = None
            self.reconnect_count += 1
            desc = f"{self.role.upper()} ({self.side})" if self.side else self.role.upper()
            print(f"[Party] Connected {desc} worker: {self.name} ({self.ip})")
            return True
        except Exception as error:
            self.connected = False
            self.device = None
            self.last_error = f"{type(error).__name__}: {error}"
            return False

    def disconnect(self):
        with self._io_lock:
            self.connected = False
            if self.device:
                try:
                    self.device.close()
                except Exception:
                    pass
                self.device = None

    def stop(self, timeout=0.5, restore=True):
        self._stop_event.set()
        self._restore_on_exit = restore
        # Погасить свет в тёплый белый до закрытия сокета: иначе лампа
        # остаётся в последнем цвете эффекта навсегда.
        if restore and self.connected and self.device:
            try:
                self.send_white(800, 800)
            except Exception:
                pass
        self.disconnect()
        if self.is_alive():
            self.join(timeout=timeout)

    def _drain_socket(self):
        if not self.device:
            return
        sock = getattr(self.device, "socket", None)
        if not sock or not hasattr(sock, "fileno") or sock.fileno() == -1:
            return
        try:
            while True:
                r, _, _ = select.select([sock], [], [], 0)
                if not r:
                    break
                data = sock.recv(4096)
                if not data:
                    self.disconnect()
                    break
        except Exception:
            self.disconnect()

    def send_color_val(self, val, nowait=True):
        with self._io_lock:
            return self._send_color_val_locked(val, nowait)

    def _send_color_val_locked(self, val, nowait=True):
        if not self.connected or not self.device:
            return False
        sock = getattr(self.device, "socket", None)
        if not sock or not hasattr(sock, "fileno") or sock.fileno() == -1:
            self.connected = False
            return False
        try:
            self._drain_socket()
            force_on = getattr(self, "force_full_power_on", False)
            if force_on or getattr(self, "is_white", True) or not getattr(self, "is_on", True):
                self.device.set_multiple_values({"20": True, "21": "colour", "24": val}, nowait=nowait)
                self.is_white = False
                self.force_full_power_on = False
                self.is_on = True
            else:
                self.device.set_value("24", val, nowait=nowait)
            self.current_val = val
            self.last_heartbeat = time.time()
            self._drain_socket()
            return True
        except Exception as e:
            self.disconnect()
            return False

    def send_white(self, brightness=800, temp=800):
        with self._io_lock:
            return self._send_white_locked(brightness, temp)

    def _send_white_locked(self, brightness=800, temp=800):
        if not self.connected or not self.device:
            return False
        sock = getattr(self.device, "socket", None)
        if not sock or not hasattr(sock, "fileno") or sock.fileno() == -1:
            self.connected = False
            return False
        try:
            self._drain_socket()
            self.device.set_multiple_values({
                "20": True,
                "21": "white",
                "22": brightness,
                "23": temp
            }, nowait=True)
            self.is_white = True
            self.is_on = True
            self.current_val = "white"
            self.last_heartbeat = time.time()
            self._drain_socket()
            return True
        except Exception:
            self.disconnect()
            return False

    def send_off(self):
        """Turn off this specific bulb completely."""
        with self._io_lock:
            return self._send_off_locked()

    def _send_off_locked(self):
        if not self.connected or not self.device:
            return False
        try:
            self._drain_socket()
            if not self.device:
                return False
            self.device.set_value("20", False, nowait=True)
            self.current_val = "off"
            self.is_on = False
            self.last_heartbeat = time.time()
            self._drain_socket()
            return True
        except Exception:
            self.disconnect()
            return False

    def send_on(self):
        """Turn on this specific bulb."""
        with self._io_lock:
            return self._send_on_locked()

    def _send_on_locked(self):
        if not self.connected or not self.device:
            return False
        try:
            self._drain_socket()
            if not self.device:
                return False
            self.device.set_value("20", True, nowait=True)
            self.is_on = True
            self.last_heartbeat = time.time()
            self._drain_socket()
            return True
        except Exception:
            self.disconnect()
            return False

    def update_ip(self, new_ip):
        with self._io_lock:
            if self.ip != new_ip or not self.connected:
                self.ip = new_ip
                self.disconnect()
                self.connect()
                if hasattr(self.manager, "save_bulb_ip"):
                    self.manager.save_bulb_ip(self.dev_id, new_ip)

    def set_position(self, pos_x, pos_y=0.0):
        self.pos_x = float(pos_x)
        self.pos_y = float(pos_y)

    def run(self):
        self.connect()
        while (self.manager.running
               and getattr(self.manager, "_run_generation", 0) == self._manager_generation
               and not self._stop_event.is_set()):
            now = time.time()
            # Auto-reconnect if dropped
            if not self.connected or not self.device:
                if (now - self.last_reconnect_time >= 2.0) and self.ip:
                    self.last_reconnect_time = now
                    if self.connect():
                        print(f"[Party] Auto-healed: {self.name} ({self.ip})")
                if self._stop_event.wait(0.1):
                    break
                continue

            # Heartbeat keepalive every 15s
            if now - self.last_heartbeat > 15.0:
                self.last_heartbeat = now
                try:
                    with self._io_lock:
                        if self.device:
                            self.device.heartbeat(nowait=True)
                            self._drain_socket()
                except Exception:
                    self.disconnect()

            if self._stop_event.wait(0.1):
                break

        if self._restore_on_exit and self.connected and self.device:
            try:
                self.send_white()
                self.device.close()
            except Exception:
                pass


class ZigbeeStripWorker(threading.Thread):
    def __init__(self, config_path, manager):
        super().__init__(daemon=True, name="zigbee-strip-worker")
        self.config_path = config_path
        self.manager = manager
        self._manager_generation = getattr(manager, "_run_generation", 0)
        self.enabled = False
        self.device_id = None
        self.token = None
        self.configured = False
        self.target_state = None  # ("hsv", hue, sat, bright) or ("white", temp_k, bright) or ("off",)
        self.last_sent_state = None
        self.last_error = None
        self._retry_delay = 1.0
        self._send_lock = threading.Lock()
        self.cond = threading.Condition()
        self.running = True
        self.load_config()

    def stop(self, timeout=0.5):
        self.running = False
        with self.cond:
            self.cond.notify_all()
        if self.is_alive():
            self.join(timeout=timeout)

    def load_config(self):
        if not self.config_path and not getattr(self.manager, "store", None):
            return
        try:
            if getattr(self.manager, "store", None):
                cfg = self.manager.store.load_strip()
            else:
                if not self.config_path or not os.path.exists(self.config_path):
                    return
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            self.device_id = cfg.get("device_id")
            self.token = cfg.get("token")
            self.configured = bool(self.token and self.device_id)
            self.enabled = bool(cfg.get("enabled", True) and self.configured)
            if self.enabled:
                print(f"[Party] Loaded Zigbee Strip Ambient Sync: Device ID {self.device_id}")
            elif not self.configured:
                self.last_error = None
            else:
                self.last_error = "strip config is disabled"
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"[Party] Failed to load strip config: {e}")

    def _is_stopping(self):
        if not self.running:
            return True
        if getattr(self.manager, "running", True) is False:
            return True
        if getattr(self.manager, "_run_generation", 0) != self._manager_generation:
            return True
        stop_ev = getattr(self.manager, "_stop_event", None)
        if stop_ev is not None and hasattr(stop_ev, "is_set") and stop_ev.is_set() is True:
            return True
        return False

    def _actions_for_state(self, state):
        if not state:
            return None
        if state[0] == "off":
            return [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}]
        if state[0] == "white":
            temp_k, bright = state[1], state[2]
            return [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}},
                {"type": "devices.capabilities.color_setting", "state": {"instance": "temperature_k", "value": int(temp_k)}},
                {"type": "devices.capabilities.range", "state": {"instance": "brightness", "value": int(bright)}}
            ]
        if state[0] == "hsv":
            hue_deg, sat, bright = state[1], state[2], state[3]
            bright = max(0, min(100, int(bright)))
            return [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}},
                {"type": "devices.capabilities.range", "state": {"instance": "brightness", "value": bright}},
                {"type": "devices.capabilities.color_setting", "state": {"instance": "hsv", "value": {"h": int(hue_deg) % 360, "s": max(0, min(100, int(sat))), "v": bright}}}
            ]
        return None

    def run(self):
        while not self._is_stopping():
            state = None
            with self.cond:
                while not self._is_stopping() and (self.target_state is None or self.target_state == self.last_sent_state):
                    self.cond.wait(timeout=0.5)
                if self._is_stopping():
                    break
                state = self.target_state

            if not self.enabled or not self.token or not self.device_id or not state:
                time.sleep(0.2)
                continue

            actions = self._actions_for_state(state)

            if actions:
                if self._send_action_sync(actions):
                    with self.cond:
                        self.last_sent_state = state
                    self._retry_delay = 1.0
                    # Respect cloud rate limit - pause between calls
                    if self.manager._stop_event.wait(1.0):
                        break
                else:
                    if self.manager._stop_event.wait(self._retry_delay):
                        break
                    self._retry_delay = min(30.0, self._retry_delay * 2)

    def restore_white(self, temp_k=3000, brightness=80):
        """Best-effort synchronous warm-white restore used on service stop."""
        if not self.enabled or not self.token or not self.device_id:
            return False
        state = ("white", int(temp_k), int(brightness))
        with self.cond:
            self.target_state = state
            self.last_sent_state = None
            self.cond.notify()
        actions = self._actions_for_state(state)
        if self._send_action_sync(actions):
            with self.cond:
                self.last_sent_state = state
            return True
        return False

    def _send_action_sync(self, actions):
        if not self.enabled or not self.token or not self.device_id:
            return False
        try:
            from config import get_config
            iot_base = get_config().party.yandex_iot_api_base
        except ImportError:
            iot_base = "https://api.iot.yandex.net/v1.0"
        url = f"{iot_base}/devices/actions"
        payload = {"devices": [{"id": self.device_id, "actions": actions}]}
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with self._send_lock:
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    resp.read()
            self.last_error = None
            return True
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
            return False

    def set_color(self, hue_deg, brightness=100, sat=100):
        with self.cond:
            self.target_state = ("hsv", int(hue_deg) % 360, max(0, min(100, int(sat))),
                                 max(0, min(100, int(brightness))))
            self.cond.notify()

    def set_white(self, temp_k=3000, brightness=80):
        with self.cond:
            self.target_state = ("white", max(1500, min(9000, int(temp_k))),
                                 max(0, min(100, int(brightness))))
            self.cond.notify()

    def set_off(self):
        with self.cond:
            self.target_state = ("off",)
            self.cond.notify()


# =====================================================================
#  Base Effect & Specialized Mode Classes
# =====================================================================

def make_tuya_hex(hue_deg, sat_pct=100, val_pct=100):
    """Encodes HSV into standard 12-char Tuya DP 24 string: HHHHSSSSVVVV."""
    h = int(hue_deg) % 360
    s = max(0, min(1000, int(clamp(sat_pct, 0, 100, 100) * 10)))  # 0-1000
    v = max(0, min(1000, int(clamp(val_pct, 0, 100, 100) * 10)))  # 0-1000
    return f"{h:04x}{s:04x}{v:04x}"


class BaseEffect:
    name = "base"
    title = "Базовый режим"
    is_cinema = False

    def __init__(self, manager):
        self.manager = manager
        self.speed = 1.0
        self.brightness = 100

    def start(self): pass
    def stop(self): pass
    def tick(self, now): pass
    def on_kick(self, crest): pass
    def on_snare(self, crest): pass


class FireplaceEffect(BaseEffect):
    """Живой камин: псевдослучайное мерцание огня и углей."""
    name = "fireplace"
    title = "Камин / Уютный очаг"

    def __init__(self, manager):
        super().__init__(manager)
        self.last_flicker = 0.0
        # Per-bulb random phase offsets
        self.offsets = {b_id: random.uniform(0, 100) for b_id in manager.workers.keys()}

    def start(self):
        print("[Effect] Fireplace started.")

    def tick(self, now):
        if now - self.last_flicker < 0.09:
            return
        self.last_flicker = now

        for b_id, w in self.manager.workers.items():
            if b_id not in self.offsets:
                self.offsets[b_id] = random.uniform(0, 100)
            offset = self.offsets[b_id]
            t = (now * 3.5 * self.speed) + offset
            
            # Fire noise: superposition of sines + small random jitter
            noise = (math.sin(t) * 0.4 + math.sin(t * 2.3) * 0.3 + math.sin(t * 4.7) * 0.2 + random.uniform(-0.1, 0.1))
            norm = max(0.0, min(1.0, (noise + 1.0) / 2.0))

            # Chandelier = deep ember to bright yellow flame (hue 6 deg to 42 deg)
            if w.role in ["kick", "ambient"]:
                hue = int(8 + norm * 34)
                bright = int(35 + norm * 65 * (self.brightness / 100.0))
                val = make_tuya_hex(hue, sat_pct=95, val_pct=bright)
                w.send_color_val(val)
            # Sconces = soft warm wall reflection (hue 18 to 30 deg, calmer)
            elif w.role == "snare":
                hue = int(14 + norm * 18)
                bright = int(25 + norm * 50 * (self.brightness / 100.0))
                val = make_tuya_hex(hue, sat_pct=90, val_pct=bright)
                w.send_color_val(val)

        # Zigbee strip: soft hearth amber
        if self.manager.strip_worker:
            self.manager.strip_worker.set_color(24, brightness=int(50 * (self.brightness / 100.0)))


class PoliceEffect(BaseEffect):
    """Полицейская сирена: двухфазный стробоскоп (синий лево / красный право)."""
    name = "police"
    title = "Полиция / Перехватчик"

    def __init__(self, manager):
        super().__init__(manager)
        self.phase = 0
        self.last_switch = 0.0
        self.strobe_count = 0

    def start(self):
        print("[Effect] Police strobe started.")

    def tick(self, now):
        # 10 Hz pulse cycle
        dt = now - self.last_switch
        pulse_dur = 0.055 / max(0.5, self.speed)

        if dt > pulse_dur:
            self.last_switch = now
            self.strobe_count = (self.strobe_count + 1) % 6
            # 0, 1 = Blue flash 1 & 2 (Left)
            # 2 = Blackout
            # 3, 4 = Red flash 1 & 2 (Right)
            # 5 = Blackout
            
            blue_hi = make_tuya_hex(240, 100, int(self.brightness))
            blue_lo = make_tuya_hex(240, 100, 3)
            red_hi = make_tuya_hex(0, 100, int(self.brightness))
            red_lo = make_tuya_hex(0, 100, 3)

            for w in self.manager.workers.values():
                is_left = (w.side == "left") or (w.role == "kick" and "2" in w.name or "4" in w.name)
                
                if self.strobe_count in (0, 1):
                    # Blue phase
                    if is_left:
                        w.send_color_val(blue_hi)
                    else:
                        w.send_color_val(red_lo)
                elif self.strobe_count in (3, 4):
                    # Red phase
                    if not is_left:
                        w.send_color_val(red_hi)
                    else:
                        w.send_color_val(blue_lo)
                else:
                    # Brief blackout gap
                    w.send_color_val(blue_lo if is_left else red_lo)


class OrbitEffect(BaseEffect):
    """Карусель / Бегущий огонь: вращение фокуса света по кругу люстры."""
    name = "orbit"
    title = "Карусель / Бегущий огонь"

    def __init__(self, manager):
        super().__init__(manager)
        self.angle = 0.0
        self.last_tick = 0.0
        self.hue = 200 # Default cyan/azure comet

    def start(self):
        print("[Effect] Orbit chaser started.")

    def tick(self, now):
        if self.last_tick == 0.0:
            self.last_tick = now
            return
        dt = now - self.last_tick
        if dt < 0.07:
            return
        self.last_tick = now

        # Rotate angle (0 to 360 deg)
        self.angle = (self.angle + (dt * 180.0 * self.speed)) % 360.0

        # Sort chandelier bulbs radially
        kick_workers = [w for w in self.manager.workers.values() if w.role in ["kick", "ambient"]]
        n = len(kick_workers)
        if n == 0:
            return

        for i, w in enumerate(kick_workers):
            target_angle = (360.0 / n) * i
            diff = abs(self.angle - target_angle)
            if diff > 180.0:
                diff = 360.0 - diff

            # Intensity bell-curve based on angular distance
            intensity = max(0.05, math.cos(math.radians(min(diff, 90.0))))
            bright = int(intensity * self.brightness)
            val = make_tuya_hex(self.hue, 95, bright)
            w.send_color_val(val)

        # Sconces glow soft contrast background
        for w in self.manager.workers.values():
            if w.role == "snare":
                val = make_tuya_hex((self.hue + 180) % 360, 90, int(15 * (self.brightness / 100.0)))
                w.send_color_val(val)



class AuroraEffect(BaseEffect):
    """Северное сияние: текучие волны изумруда, бирюзы и фиолетового."""
    name = "aurora"
    title = "Северное сияние"

    def __init__(self, manager):
        super().__init__(manager)
        self.last_tick = 0.0
        self.offsets = {b_id: random.uniform(0, math.pi * 2) for b_id in manager.workers.keys()}

    def start(self):
        print("[Effect] Aurora Borealis started.")

    def tick(self, now):
        if now - self.last_tick < 0.08:
            return
        self.last_tick = now

        for b_id, w in self.manager.workers.items():
            if b_id not in self.offsets:
                self.offsets[b_id] = random.uniform(0, math.pi * 2)
            off = self.offsets[b_id]
            t = (now * 0.9 * self.speed) + off
            
            # Slow harmonic color drift
            if w.role in ["kick", "ambient"]:
                # Chandelier: Emerald green (140) to Cyan/Azure (205)
                hue = int(140 + 65 * ((math.sin(t) + 1.0) / 2.0))
                intensity = 0.35 + 0.65 * ((math.sin(t * 1.4) + 1.0) / 2.0)
                bright = int(intensity * self.brightness)
                val = make_tuya_hex(hue, 95, bright)
                w.send_color_val(val)
            elif w.role == "snare":
                # Sconces: Deep violet-indigo (225 to 280)
                hue = int(225 + 55 * ((math.cos(t * 0.8) + 1.0) / 2.0))
                intensity = 0.30 + 0.70 * ((math.cos(t * 1.2) + 1.0) / 2.0)
                bright = int(intensity * (self.brightness * 0.9))
                val = make_tuya_hex(hue, 90, bright)
                w.send_color_val(val)

        # Zigbee Strip: Mystic teal/cyan
        if self.manager.strip_worker:
            strip_hue = int(165 + 30 * ((math.sin(now * 0.5 * self.speed) + 1.0) / 2.0))
            self.manager.strip_worker.set_color(strip_hue, brightness=int(60 * (self.brightness / 100.0)))


class SynthwaveEffect(BaseEffect):
    """Неоновый закат / Synthwave: маджента люстры и закатный персик бра."""
    name = "synthwave"
    title = "Неоновый закат"

    def __init__(self, manager):
        super().__init__(manager)
        self.last_tick = 0.0

    def start(self):
        print("[Effect] Synthwave Sunset started.")

    def tick(self, now):
        if now - self.last_tick < 0.08:
            return
        self.last_tick = now

        # Breathing wave (0.45 to 1.0)
        breath = (math.sin(now * 1.5 * self.speed) + 1.0) / 2.0
        
        for w in self.manager.workers.values():
            if w.role in ["kick", "ambient"]:
                # Chandelier: Neon Magenta / Hot Pink (320 to 335)
                hue = int(320 + 15 * breath)
                bright = int((0.45 + 0.55 * breath) * self.brightness)
                val = make_tuya_hex(hue, 100, bright)
                w.send_color_val(val)
            elif w.role == "snare":
                # Sconces: Electric sunset amber / peach (22 to 34)
                hue = int(22 + 12 * (1.0 - breath))
                bright = int((0.40 + 0.60 * (1.0 - breath)) * self.brightness)
                val = make_tuya_hex(hue, 95, bright)
                w.send_color_val(val)

        if self.manager.strip_worker:
            # Electric purple strip
            self.manager.strip_worker.set_color(280, brightness=int(75 * (self.brightness / 100.0)))


class ThunderstormEffect(BaseEffect):
    """Гроза и молнии: штормовой тёмно-синий фон со случайными вспышками молний."""
    name = "thunderstorm"
    title = "Гроза и молнии"

    def __init__(self, manager):
        super().__init__(manager)
        self.last_tick = 0.0
        self.next_strike = time.time() + 3.0
        self.flash_step = -1
        self.flash_timer = 0.0

    def start(self):
        print("[Effect] Thunderstorm started.")
        self.next_strike = time.time() + 2.0
        self.flash_step = -1

    def tick(self, now):
        if now - self.last_tick < 0.045:
            return
        self.last_tick = now

        # Check if new lightning strike needs to be scheduled
        if self.flash_step == -1:
            if now >= self.next_strike:
                self.flash_step = 0
                self.flash_timer = now
                interval = random.uniform(3.0, 9.0) / max(0.4, self.speed)
                self.next_strike = now + interval

        # If a strike sequence is active
        if self.flash_step >= 0:
            dt = now - self.flash_timer
            # Flash sequence:
            # step 0 (0.00 - 0.05s): Strike 1 (100% cool white on chandelier)
            # step 1 (0.05 - 0.10s): Pause / Blackout
            # step 2 (0.10 - 0.18s): Strike 2 (Blinding burst all bulbs)
            # step 3 (0.18 - 0.65s): Thunder decay (rumble on sconces & strip)
            
            white_strike = make_tuya_hex(200, 10, int(self.brightness))
            storm_base = make_tuya_hex(225, 90, max(3, int(8 * (self.brightness / 100.0))))

            if dt < 0.05:
                # Flash 1
                for w in self.manager.workers.values():
                    w.send_color_val(white_strike if w.role in ["kick", "ambient"] else storm_base)
            elif dt < 0.10:
                # Blackout gap
                for w in self.manager.workers.values():
                    w.send_color_val(storm_base)
            elif dt < 0.18:
                # Flash 2 (Big strike)
                for w in self.manager.workers.values():
                    w.send_color_val(white_strike)
                if self.manager.strip_worker:
                    self.manager.strip_worker.set_white(6500, 100)
            elif dt < 0.65:
                # Rumble fadeout
                progress = (dt - 0.18) / 0.47
                rumble_bright = int((1.0 - progress) * 45 * (self.brightness / 100.0))
                rumble_val = make_tuya_hex(215, 60, max(5, rumble_bright))
                for w in self.manager.workers.values():
                    w.send_color_val(rumble_val if w.role == "snare" else storm_base)
            else:
                self.flash_step = -1
                for w in self.manager.workers.values():
                    w.send_color_val(storm_base)
                if self.manager.strip_worker:
                    self.manager.strip_worker.set_color(225, brightness=15)
        else:
            # Baseline stormy ambiance
            storm_base = make_tuya_hex(225, 90, max(3, int(8 * (self.brightness / 100.0))))
            for w in self.manager.workers.values():
                w.send_color_val(storm_base)


class OceanEffect(BaseEffect):
    """Океанская волна: пространственный перекат лазурного и индиго слева направо."""
    name = "ocean"
    title = "Океанская волна"

    def __init__(self, manager):
        super().__init__(manager)
        self.last_tick = 0.0

    def start(self):
        print("[Effect] Ocean Wave started.")

    def tick(self, now):
        if now - self.last_tick < 0.07:
            return
        self.last_tick = now

        # Wave moves from left (-1.5) to right (+1.5) in loop
        cycle_dur = 4.0 / max(0.3, self.speed)
        pos = ((now % cycle_dur) / cycle_dur) * 3.4 - 1.7

        for w in self.manager.workers.values():
            px = getattr(w, "pos_x", 0.0)
            dist = abs(px - pos)
            if dist < 0.75:
                crest = (1.0 - dist / 0.75) ** 1.3
                hue = int(185 + 20 * crest) # Aquamarine / Cyan foam
                bright = int((0.35 + 0.65 * crest) * self.brightness)
                val = make_tuya_hex(hue, 80, bright)
            else:
                # Deep abyss blue
                val = make_tuya_hex(220, 95, max(4, int(15 * (self.brightness / 100.0))))
            w.send_color_val(val)

        if self.manager.strip_worker:
            strip_bright = int((0.3 + 0.5 * ((math.sin(now * 1.2 * self.speed) + 1.0) / 2.0)) * self.brightness)
            self.manager.strip_worker.set_color(195, brightness=strip_bright)


class LavaEffect(BaseEffect):
    """Лавовая лампа / Chill: тёплые всплывающие пузыри рубина и магмы."""
    name = "lava"
    title = "Лавовая лампа"

    def __init__(self, manager):
        super().__init__(manager)
        self.last_tick = 0.0
        self.phases = {b_id: random.uniform(0, math.pi * 2) for b_id in manager.workers.keys()}

    def start(self):
        print("[Effect] Lava Lamp started.")

    def tick(self, now):
        if now - self.last_tick < 0.08:
            return
        self.last_tick = now

        for b_id, w in self.manager.workers.items():
            if b_id not in self.phases:
                self.phases[b_id] = random.uniform(0, math.pi * 2)
            ph = self.phases[b_id]
            t = (now * 1.1 * self.speed) + ph
            
            blob = (math.sin(t) * 0.5 + math.sin(t * 1.9) * 0.3 + math.sin(t * 3.1) * 0.2 + 1.0) / 2.0
            blob = max(0.0, min(1.0, blob))
            
            # Deep Ruby (355) to Hot Magma Orange (18)
            hue = int((355 + blob * 25) % 360)
            bright = int((0.25 + 0.75 * blob) * self.brightness)
            val = make_tuya_hex(hue, 95, bright)
            w.send_color_val(val)

        if self.manager.strip_worker:
            self.manager.strip_worker.set_color(10, brightness=int(45 * (self.brightness / 100.0)))


class MusicStrobeEffect(BaseEffect):
    """Клубная светомузыка: реакция на бас (Kick) и малый барабан (Snare)."""
    name = "party"
    title = "Клубная светомузыка"

    def __init__(self, manager):
        super().__init__(manager)

    def on_kick(self, crest):
        self.manager.on_kick_internal(crest)

    def on_snare(self, crest):
        self.manager.on_snare_internal(crest)


class MovieAmbilightEffect(BaseEffect):
    """Кинотеатр 4D: непрерывный Ambilight + магические спецэффекты (Гарри Поттер 3)."""
    name = "cinema"
    title = "Кинотеатр 4D: Гарри Поттер"
    is_cinema = True

    def __init__(self, manager, script_path=None):
        super().__init__(manager)
        base_dir = Path(__file__).resolve().parent
        self.script_path = script_path or str(base_dir / "movies" / "hp3_ambilight.json")
        self.script = {"scenes": [], "effects": []}
        self.load_script()

        self.playing = False
        self.current_time = 0.0
        self.offset = 0.0 # User calibration offset in seconds
        self.start_wall_time = 0.0
        self.paused_at = 0.0
        self.last_tick = 0.0
        self.last_checked_time = 0.0
        self.last_sent_val = None
        self.last_send_time = 0.0

        # Active spell overlay & directional beam state
        self.active_spell = None
        self.spell_end_time = 0.0
        self.beam_in_progress = False
        self.active_scene_desc = "Ожидание старта фильма"
        self.last_scene_idx = -1

        # Единый sequencer спецэффектов: не даём десяткам потоков
        # одновременно перекрашивать одни и те же лампы.
        self._anim_run_lock = threading.Lock()
        self._anim_state_lock = threading.Lock()
        self._anim_event = threading.Event()
        self._anim_generation = 0
        self._current_anim_event = None
        self._anim_thread = None

    def load_script(self, script_path=None):
        if script_path:
            self.script_path = str(script_path)
        if os.path.exists(self.script_path):
            try:
                with open(self.script_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("script root must be an object")
                scenes = [s for s in data.get("scenes", []) if isinstance(s, dict) and isinstance(s.get("start"), (int, float))]
                effects = [e for e in data.get("effects", []) if isinstance(e, dict) and isinstance(e.get("time"), (int, float))]
                data["scenes"] = sorted(scenes, key=lambda item: item["start"])
                data["effects"] = sorted(effects, key=lambda item: item["time"])
                self.script = data
                print(f"[Cinema] Loaded light script: {self.script.get('movie')} ({len(self.script.get('scenes', []))} scenes, {len(self.script.get('effects', []))} cues)")
            except Exception as e:
                print(f"[Cinema] Error loading script: {e}")

    def _cancel_animations(self):
        with self._anim_state_lock:
            self._anim_generation += 1
            self._anim_event.set()
            self._anim_event = threading.Event()

    def _sleep(self, seconds):
        event = self._current_anim_event or self._anim_event
        if event.wait(max(0.0, float(seconds))):
            raise _Cancelled()

    def _start_anim(self, fn, *args, **kwargs):
        """Один активный спецэффект за раз, с отменой предыдущего."""
        with self._anim_state_lock:
            self._anim_generation += 1
            generation = self._anim_generation
            self._anim_event.set()
            event = threading.Event()
            self._anim_event = event

        def runner():
            with self._anim_run_lock:
                if generation != self._anim_generation:
                    return
                self._current_anim_event = event
                try:
                    fn(*args, **kwargs)
                except _Cancelled:
                    pass
                except Exception as error:
                    print(f"[Cinema] Spell error: {type(error).__name__}: {error}")
                finally:
                    if self._current_anim_event is event:
                        self._current_anim_event = None
                    if generation == self._anim_generation:
                        self.beam_in_progress = False
                        self.active_spell = None
                        self.last_sent_val = None
                        if self.playing:
                            try:
                                self._apply_ambient(force=True)
                            except Exception:
                                pass

        thread = threading.Thread(target=runner, daemon=True, name="party-spell")
        self._anim_thread = thread
        thread.start()

    def start(self):
        """Called automatically when user switches to Cinema mode in the panel."""
        print("[Cinema] Cinema mode activated from panel -> launching light playback...")
        self.play()

    def stop(self):
        """Called when user switches away from Cinema mode."""
        self.playing = False
        self.active_spell = None
        self._bulbs_off_for_strip = False
        self._cancel_animations()

    def play(self):
        if not self.playing:
            self.playing = True
            self._bulbs_off_for_strip = False
            self.start_wall_time = time.monotonic() - self.current_time
            self.last_checked_time = self.current_time
            self.last_sent_val = None
            self.active_spell = None
            # Ensure master enabled is active so ticks run reliably
            self.manager.settings["master_enabled"] = True
            if getattr(self.manager, "_bt_ambient_prev", None):
                self.manager._bt_ambient_prev = None
            if self.manager.settings.get("active_mode") != "cinema":
                self.manager.settings["active_mode"] = "cinema"
                self.manager.current_effect = self
            self.manager.save_settings()
            print(f"[Cinema] Movie playback started at {self.current_time:.1f}s (offset: {self.offset:+.1f}s)")
            # Instantly illuminate bulbs with current scene color without waiting
            self._apply_ambient(force=True)

    def pause(self):
        if self.playing:
            self.playing = False
            self.current_time = time.monotonic() - self.start_wall_time
            self.active_spell = None
            self._cancel_animations()
            print(f"[Cinema] Movie paused at {self.current_time:.1f}s")
            # Restore cozy ambient white on pause so user can walk around
            for w in self.manager.workers.values():
                w.send_white(brightness=600, temp=700)
            if self.manager.strip_worker:
                self.manager.strip_worker.set_white(temp_k=2700, brightness=60)

    def _activate(self):
        if not self.manager.running:
            self.manager.start()
        self.manager.settings["active_mode"] = "cinema"
        self.manager.settings["master_enabled"] = True
        self.manager.current_effect = self
        self.manager.save_settings()

    def seek(self, sec, play=True):
        self._activate()
        self._bulbs_off_for_strip = False
        duration = float(self.script.get("duration_sec", 8502) or 8502)
        self.current_time = max(0.0, min(duration, float(sec)))
        self.playing = bool(play)
        if self.playing:
            self.start_wall_time = time.monotonic() - self.current_time
        self.last_checked_time = self.current_time
        self.last_sent_val = None
        self._cancel_animations()
        if self.playing:
            self._apply_ambient(force=False)

    def set_offset(self, delta):
        self.offset = clamp(self.offset + float(delta), -120.0, 120.0)
        print(f"[Cinema] Calibration offset: {self.offset:+.1f}s")

    def sync_to_cue(self, cue_time=32.0):
        """Instantly aligns current playback time to a well-known cue (e.g. 32.0s first Lumos)."""
        self._activate()
        self._bulbs_off_for_strip = False
        now = time.monotonic()
        duration = float(self.script.get("duration_sec", 8502) or 8502)
        cue_sec = max(0.0, min(duration, float(cue_time or 32.0)))
        self.playing = True
        self.start_wall_time = now - cue_sec
        self.offset = 0.0
        self.current_time = cue_sec
        self.last_checked_time = cue_sec
        self.last_sent_val = None
        self._cancel_animations()
        self.manager.settings["master_enabled"] = True
        self.manager.save_settings()
        print(f"[Cinema] Precision-synced to cue at {cue_sec:.1f}s")
        self._apply_ambient(force=True)

    def trigger_spell(self, spell_type, direction=None):
        """Instant manual or scripted spell trigger with spatial directional beams & strobes."""
        if not self.manager.settings.get("effects_enabled", True):
            return
        fx_target = self.manager.settings.get("effects_target", "bulbs_only")
        if fx_target == "off":
            return
        now = time.monotonic()
        spell_type = str(spell_type or "").strip()
        if not spell_type:
            return
        print(f"[Cinema] >>> MAGIC SPELL TRIGGERED: {spell_type} (direction={direction}) <<<")
        self.active_spell = spell_type

        animations = {
            "storm_lightning": ("_anim_lightning", {"intensity": 1.0, "direction": direction or "all"}),
            "quidditch_lightning": ("_anim_lightning", {"intensity": 1.0, "direction": direction or "all"}),
            "lightning_storm": ("_anim_lightning", {"intensity": 1.0, "direction": direction or "all"}),
            "storm_lightning_soft": ("_anim_lightning", {"intensity": 0.6, "direction": direction or "all"}),
            "wand_spark": ("_anim_lumos_spark", {"level": 1}),
            "spell_lumos": ("_anim_lumos_spark", {"level": 1}),
            "spell_lumos_2": ("_anim_lumos_spark", {"level": 2}),
            "spell_lumos_maxima": ("_anim_lumos_spark", {"level": 3}),
            "lumos_maxima_blast": ("_anim_lumos_spark", {"level": 3}),
            "bulb_switch_flicker": ("_anim_incandescent_switch", {}),
            "incandescent_switch": ("_anim_incandescent_switch", {}),
            "knight_bus_strobe": ("_anim_knight_bus_strobe", {}),
            "knight_bus_jump": ("_anim_knight_bus_strobe", {}),
            "knight_bus_brake_strobe": ("_anim_knight_bus_strobe", {}),
            "glass_shatter": ("_anim_glass_shatter", {}),
            "marge_pop": ("_anim_marge_inflate", {}),
            "marge_inflate": ("_anim_marge_inflate", {}),
            "marge_float": ("_anim_marge_float", {}),
            "street_lamp_flicker": ("_anim_street_lamp_flicker", {}),
            "dementor_freeze": ("_anim_dementor_freeze", {}),
            "dementors_swarm": ("_anim_dementor_freeze", {}),
            "time_turner_spin": ("_anim_time_turner", {}),
            "toad_choir": ("_anim_toad_choir", {}),
            "gryffindor_sweets": ("_anim_gryffindor_sweets", {}),
            "werewolf_moon": ("_anim_werewolf_moon", {}),
            "spell_patronus": ("_anim_great_patronus", {"direction": direction or "center_out"}),
            "great_patronus": ("_anim_great_patronus", {"direction": direction or "center_out"}),
            "marauders_map_ink": ("_anim_marauders_ink", {}),
        }
        if spell_type in ("spell_ridikulus", "spell_riddikulus") and direction not in ("left", "right", "center_out"):
            animations[spell_type] = ("_anim_ridikulus", {})
        if spell_type in animations:
            method, kwargs = animations[spell_type]
            self._start_anim(getattr(self, method), **kwargs)
            return

        if not direction:
            if spell_type in ("spell_avada", "spell_expelliarmus"):
                direction = "left"
            elif spell_type == "spell_bombarda":
                direction = "right"
            else:
                direction = "all"

        # Launch directional beam if requested (sub-second wave animation)
        if direction in ("left", "right", "center_out"):
            self._start_anim(self._cast_directional_beam, spell_type, direction)
            return

        dur = 0.8
        if spell_type == "spell_avada":
            dur = 0.25
        self.spell_end_time = now + dur

    def _anim_lightning(self, intensity=1.0, direction="all"):
        """Realistic jagged thunderbolt storm: pre-flash -> blinding crack -> echo rumble."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            cyan_flash = make_tuya_hex(200, 15, int(100 * intensity))
            black_cut = make_tuya_hex(220, 80, 0)
            dim_blue = make_tuya_hex(220, 80, 4)
            sconces = [w for w in workers if w.role == "snare"]
            chands = [w for w in workers if w.role in ("kick", "ambient")]
            
            # Step 1: Pre-flash on wall sconces
            self.manager.broadcast_color(sconces or workers, cyan_flash)
            self._sleep(0.035)
            self.manager.broadcast_color(sconces or workers, black_cut)
            self._sleep(0.025)
            
            # Step 2: Main thunder strike on ALL bulbs
            self.manager.broadcast_color(workers, cyan_flash)
            if self.manager.strip_worker: self.manager.strip_worker.set_color(200, 100)
            self._sleep(0.045)
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.030)
            
            # Step 3: Secondary electrical discharge
            self.manager.broadcast_color(workers, cyan_flash)
            self._sleep(0.055)
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.035)

            # Step 4: Low rumble through chandelier
            rumble = make_tuya_hex(210, 50, int(45 * intensity))
            self.manager.broadcast_color(chands, rumble)
            self._sleep(0.090)
            self.manager.broadcast_color(chands, dim_blue)
            self._sleep(0.040)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_lumos_spark(self, level=1):
        """Wand ignition spark & strobe: crackling ignition expanding outward."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            center_w = [w for w in workers if abs(getattr(w, "pos_x", 0.0)) < 0.3] or workers
            
            white_spark = make_tuya_hex(45, 10, 100)
            black_cut = make_tuya_hex(45, 10, 0)
            warm_mid = make_tuya_hex(40, 60, 45)
            
            if level == 1:
                # First whisper: 2 crisp electrical sparks on center bulb, then warm glow
                for _ in range(2):
                    self.manager.broadcast_color(center_w, white_spark)
                    self._sleep(0.035)
                    self.manager.broadcast_color(center_w, black_cut)
                    self._sleep(0.025)
                self.manager.broadcast_color(center_w, warm_mid)
                self._sleep(0.50)
            elif level == 2:
                # Second whisper: 3 sparks expanding from center outward
                for _ in range(3):
                    self.manager.broadcast_color(center_w, white_spark)
                    self._sleep(0.035)
                    self.manager.broadcast_color(center_w, black_cut)
                    self._sleep(0.025)
                self.manager.broadcast_color(workers, warm_mid)
                self._sleep(0.70)
            else:
                # LUMOS MAXIMA: 5 rapid high-voltage full-room strobes (100% white <-> 0% pitch black)
                for _ in range(5):
                    self.manager.broadcast_color(workers, white_spark)
                    if self.manager.strip_worker: self.manager.strip_worker.set_white(5000, 100)
                    self._sleep(0.045)
                    self.manager.broadcast_color(workers, black_cut)
                    self._sleep(0.035)
                # Blinding flare hold
                self.manager.broadcast_color(workers, white_spark)
                self._sleep(0.75)
                # Cinematic fade down
                for step in range(4):
                    fade_bright = int(75 - step * 18)
                    fade_val = make_tuya_hex(42, 30 + step * 15, fade_bright)
                    self.manager.broadcast_color(workers, fade_val)
                    self._sleep(0.08)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_incandescent_switch(self):
        """Classic incandescent lamp switch flicker (Uncle Vernon clicking corridor light)."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            warm_amber = make_tuya_hex(35, 80, 65)
            warm_flicker = make_tuya_hex(35, 90, 20)
            black_cut = make_tuya_hex(35, 90, 0)
            
            # Click 1: 50Hz tungsten filament stutter
            self.manager.broadcast_color(workers, warm_amber)
            self._sleep(0.030)
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.020)
            self.manager.broadcast_color(workers, warm_flicker)
            self._sleep(0.035)
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.020)
            # Steady warm hallway light while Vernon checks the room
            self.manager.broadcast_color(workers, warm_amber)
            self._sleep(3.0)
            # Vernon clicks switch off -> instant blackout
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.20)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_knight_bus_strobe(self):
        """High-energy alternating purple-cyan neon strobe (The Knight Bus arriving & speeding)."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            left_w = [w for w in workers if getattr(w, "pos_x", 0.0) < -0.2]
            right_w = [w for w in workers if getattr(w, "pos_x", 0.0) > 0.2]
            mid_w = [w for w in workers if abs(getattr(w, "pos_x", 0.0)) <= 0.2]
            
            purple_hi = make_tuya_hex(290, 100, 100)
            cyan_hi = make_tuya_hex(180, 100, 100)
            yellow_hi = make_tuya_hex(45, 90, 100)
            black_cut = make_tuya_hex(0, 0, 0)
            
            # 8-cycle alternating rapid disco neon strobe
            for cycle in range(8):
                m = {}
                if cycle % 2 == 0:
                    for w in left_w: m[w] = purple_hi
                    for w in right_w: m[w] = cyan_hi
                    for w in mid_w: m[w] = yellow_hi
                else:
                    for w in left_w: m[w] = cyan_hi
                    for w in right_w: m[w] = purple_hi
                    for w in mid_w: m[w] = black_cut
                self.manager.broadcast_map(m)
                if self.manager.strip_worker:
                    self.manager.strip_worker.set_color(290 if cycle % 2 == 0 else 180, 100)
                self._sleep(0.045)
                # Crisp black cut between pulses
                self.manager.broadcast_color(workers, black_cut)
                self._sleep(0.025)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_glass_shatter(self):
        """Wine glass explosion in Aunt Marge's hand: sudden violent crack + wine-red flash."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            white_crack = make_tuya_hex(50, 10, 100)
            red_wine = make_tuya_hex(350, 100, 90)
            black_cut = make_tuya_hex(0, 0, 0)
            self.manager.broadcast_color(workers, white_crack)
            self._sleep(0.045)
            self.manager.broadcast_color(workers, red_wine)
            if self.manager.strip_worker:
                self.manager.strip_worker.set_color(350, 100)
            self._sleep(0.120)
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.050)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_marge_inflate(self):
        """Aunt Marge swelling like a balloon: 5 escalating pressure pulses + button pop."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            chands = [w for w in workers if w.role in ("kick", "ambient")] or workers
            black_cut = make_tuya_hex(0, 0, 0)
            button_pop = make_tuya_hex(50, 10, 100)
            
            for step in range(5):
                bright = int(35 + step * 16)
                amber = make_tuya_hex(28, 90, bright)
                self.manager.broadcast_color(chands, amber)
                self._sleep(0.080)
                self.manager.broadcast_color(chands, black_cut)
                self._sleep(0.040)
            
            # Button pops off!
            self.manager.broadcast_color(workers, button_pop)
            self._sleep(0.060)
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.030)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_marge_float(self):
        """Aunt Marge floating out the patio doors into the night sky."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            sky_cyan = make_tuya_hex(210, 80, 50)
            self.manager.broadcast_color(workers, sky_cyan)
            if self.manager.strip_worker:
                self.manager.strip_worker.set_color(210, 80)
            self._sleep(0.35)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_street_lamp_flicker(self):
        """Unstable flickering street lantern buzzing in the dark."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            lamp_w = [w for w in workers if getattr(w, "pos_x", 0.0) < 0.0] or workers
            street_hi = make_tuya_hex(38, 90, 85)
            street_lo = make_tuya_hex(38, 90, 0)
            
            delays = [0.030, 0.020, 0.050, 0.025, 0.060, 0.015, 0.035]
            for i, d in enumerate(delays):
                val = street_hi if (i % 2 == 0) else street_lo
                self.manager.broadcast_color(lamp_w, val)
                self._sleep(d)
            self.manager.broadcast_color(lamp_w, street_hi)
            self._sleep(0.3)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_dementor_freeze(self):
        """Dementor frost: lights shudder, buzz, and drop down into freezing pale blue."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            black_cut = make_tuya_hex(200, 90, 0)
            for step in range(5):
                bright_hi = int(60 - step * 12)
                ice_hi = make_tuya_hex(195, 70, bright_hi)
                self.manager.broadcast_color(workers, ice_hi)
                self._sleep(0.040)
                self.manager.broadcast_color(workers, black_cut)
                self._sleep(0.025)
            # Deep frozen chill
            ice_cold = make_tuya_hex(195, 85, 10)
            self.manager.broadcast_color(workers, ice_cold)
            if self.manager.strip_worker: self.manager.strip_worker.set_color(195, 15)
            self._sleep(1.0)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_time_turner(self):
        """Golden temporal chaser: rapid clockwise spin around chandelier with sconce flashes."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            kick_workers = [w for w in workers if w.role in ("kick", "ambient")]
            sconces = [w for w in workers if w.role == "snare"]
            n = len(kick_workers) or 1
            gold_hi = make_tuya_hex(40, 95, 100)
            gold_dim = make_tuya_hex(40, 95, 5)
            sand_pulse = make_tuya_hex(35, 70, 90)
            
            for rot in range(16):
                target_idx = rot % n
                m = {w: (gold_hi if i == target_idx else gold_dim) for i, w in enumerate(kick_workers)}
                if rot % 4 == 0:
                    for s in sconces: m[s] = sand_pulse
                elif rot % 4 == 2:
                    for s in sconces: m[s] = gold_dim
                self.manager.broadcast_map(m)
                self._sleep(0.055)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_toad_choir(self):
        """Great Hall: warm undulating candle chorus and floating candles."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            for step in range(8):
                m = {}
                for w in workers:
                    hue = random.choice([25, 32, 40])
                    bright = random.randint(50, 90)
                    m[w] = make_tuya_hex(hue, 85, bright)
                self.manager.broadcast_map(m)
                self._sleep(0.12)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_gryffindor_sweets(self):
        """Gryffindor Dormitory: playful hopping candy sparks and monkey grimace flashes."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            candy_colors = [
                make_tuya_hex(25, 95, 100),  # Lion gold
                make_tuya_hex(120, 90, 100), # Monkey green
                make_tuya_hex(200, 90, 100), # Steam blue
                make_tuya_hex(340, 90, 100)  # Berry red
            ]
            black_cut = make_tuya_hex(0, 0, 0)
            for hop in range(6):
                m = {w: random.choice(candy_colors) for w in workers if random.random() > 0.4}
                for w in workers:
                    if w not in m: m[w] = black_cut
                self.manager.broadcast_map(m)
                self._sleep(0.060)
                self.manager.broadcast_color(workers, black_cut)
                self._sleep(0.030)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_werewolf_moon(self):
        """Full Moon emergence & werewolf transformation: eerie moonlight -> blood red pulse."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            chands = [w for w in workers if w.role in ("kick", "ambient")]
            sconces = [w for w in workers if w.role == "snare"]
            moon_white = make_tuya_hex(200, 20, 90)
            blood_red = make_tuya_hex(355, 100, 100)
            black_cut = make_tuya_hex(0, 0, 0)
            
            # Moonlight swell on chandelier
            self.manager.broadcast_color(chands, moon_white)
            self.manager.broadcast_color(sconces, black_cut)
            self._sleep(0.8)
            # Werewolf howl: 3 aggressive red pulses
            for _ in range(3):
                self.manager.broadcast_color(workers, blood_red)
                self._sleep(0.050)
                self.manager.broadcast_color(workers, black_cut)
                self._sleep(0.035)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_great_patronus(self, direction="center_out"):
        """EXPECTO PATRONUM: blinding silver-white explosion driving darkness away."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers: return
            silver_white = make_tuya_hex(205, 20, 100)
            silver_dim = make_tuya_hex(205, 40, 20)
            black_cut = make_tuya_hex(0, 0, 0)
            
            # Initial explosive blast 100% white
            self.manager.broadcast_color(workers, silver_white)
            if self.manager.strip_worker: self.manager.strip_worker.set_white(6500, 100)
            self._sleep(0.090)
            self.manager.broadcast_color(workers, black_cut)
            self._sleep(0.030)
            
            # 3 expanding silver ripples from center outward
            center_w = [w for w in workers if abs(getattr(w, "pos_x", 0.0)) < 0.3]
            outer_w = [w for w in workers if abs(getattr(w, "pos_x", 0.0)) >= 0.3]
            for ripple in range(3):
                self.manager.broadcast_color(center_w, silver_white)
                self._sleep(0.040)
                self.manager.broadcast_color(outer_w, silver_white)
                self.manager.broadcast_color(center_w, silver_dim)
                self._sleep(0.050)
                self.manager.broadcast_color(outer_w, silver_dim)
                self._sleep(0.030)
            # Majestic silver glow hold
            self.manager.broadcast_color(workers, silver_white)
            self._sleep(1.2)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing: self._apply_ambient(force=True)

    def _anim_marauders_ink(self):
        """Чернила Карты Мародёров: золотые волны по комнате."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers:
                return
            gold = make_tuya_hex(40, 95, 95)
            dim = make_tuya_hex(40, 90, 8)
            for _ in range(3):
                self.manager.broadcast_color(workers, gold)
                self._sleep(0.12)
                self.manager.broadcast_color(workers, dim)
                self._sleep(0.08)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing:
                self._apply_ambient(force=True)

    def _anim_ridikulus(self):
        """Ридикулус: весёлый жёлтый строб по всем лампам."""
        self.beam_in_progress = True
        try:
            workers = [w for w in self.manager.workers.values() if w.connected]
            if not workers:
                return
            yellow = make_tuya_hex(52, 90, 100)
            dark = make_tuya_hex(52, 90, 5)
            for _ in range(3):
                self.manager.broadcast_color(workers, yellow)
                self._sleep(0.08)
                self.manager.broadcast_color(workers, dark)
                self._sleep(0.06)
        finally:
            self.beam_in_progress = False
            self.last_sent_val = None
            if self.playing:
                self._apply_ambient(force=True)

    def _cast_directional_beam(self, spell_type, direction="left"):
        """Fires a localized beam of magic traveling physically across the room."""
        self.beam_in_progress = True
        try:
            # 1. Colors & Impact settings
            if spell_type == "spell_avada":
                hue = 125  # Emerald Green
                sat = 100
                val = 100
                impact_hold = 0.22
            elif spell_type == "spell_expelliarmus":
                hue = 355  # Scarlet Red
                sat = 100
                val = 100
                impact_hold = 0.20
            elif spell_type == "spell_bombarda":
                hue = 25   # Explosive fiery gold
                sat = 95
                val = 100
                impact_hold = 0.25
            elif spell_type == "spell_riddikulus":
                hue = 50   # Comic bright yellow-gold
                sat = 85
                val = 100
                impact_hold = 0.25
            elif spell_type in ("spell_patronus", "great_patronus"):
                hue = 205  # Silver-cyan
                sat = 35
                val = 100
                impact_hold = 0.40
            elif spell_type in ("spell_lumos", "spell_lumos_maxima"):
                hue = 45   # Warm brilliant white
                sat = 12
                val = 100
                impact_hold = 0.50
            elif spell_type == "quidditch_lightning":
                hue = 220  # Electric lightning blue
                sat = 25
                val = 100
                impact_hold = 0.15
            else:
                hue = 125
                sat = 100
                val = 100
                impact_hold = 0.20

            val_hi = make_tuya_hex(hue, sat, val)
            val_dim = make_tuya_hex(hue, sat, 4)

            # 2. Group active workers by spatial position
            workers = [w for w in self.manager.workers.values() if w.connected and w.role in ("kick", "snare", "ambient")]
            if not workers:
                return

            if direction in ("left", "right"):
                # Dynamic sorting through all distinct X coordinates across the room
                # left: Right (high X) -> Left (low X)
                # right: Left (low X) -> Right (high X)
                pos_list = sorted(list(set(round(getattr(w, "pos_x", 0.0), 1) for w in workers)), reverse=(direction == "left"))
                stages = []
                for p in pos_list:
                    tier = [w for w in workers if round(getattr(w, "pos_x", 0.0), 1) == p]
                    if tier:
                        stages.append(tier)
            elif direction == "center_out":
                # Radial burst: Center first, then mid, then outer walls
                center_workers = [w for w in workers if abs(getattr(w, "pos_x", 0.0)) < 0.3]
                mid_workers = [w for w in workers if 0.3 <= abs(getattr(w, "pos_x", 0.0)) < 0.7]
                outer_workers = [w for w in workers if abs(getattr(w, "pos_x", 0.0)) >= 0.7]
                stages = [s for s in [center_workers, mid_workers, outer_workers] if s]
            else:
                stages = [workers]

            # Dynamic hop delay: maintains ~0.25-0.35s total flight time across any number of bulbs
            hop_delay = max(0.040, min(0.075, 0.220 / max(1, len(stages))))

            # 3. Propagate the beam sequentially across the room
            mid_idx = max(0, len(stages) // 2)
            for i, stage in enumerate(stages):
                if not stage:
                    continue
                # Ignite current stage
                self.manager.broadcast_color(stage, val_hi)
                
                # Dim preceding stage
                if i > 0 and stages[i - 1]:
                    self.manager.broadcast_color(stages[i - 1], val_dim)

                if i == mid_idx and self.manager.strip_worker:
                    self.manager.strip_worker.set_color(hue, brightness=100)

                is_last = (i == len(stages) - 1)
                self._sleep(impact_hold if is_last else hop_delay)

            # 4. Impact dissipation
            if stages[-1]:
                self.manager.broadcast_color(stages[-1], val_dim)
                self._sleep(0.040)

            # Special ripple for Expecto Patronum
            if spell_type in ("spell_patronus", "great_patronus"):
                for step in range(3):
                    fade_val = int(80 - step * 25)
                    wave_val = make_tuya_hex(hue, sat, fade_val)
                    self.manager.broadcast_color(workers, wave_val)
                    self._sleep(0.08)

        finally:
            self.beam_in_progress = False
            self.active_spell = None
            self.last_sent_val = None
            if self.playing:
                self._apply_ambient(force=True)
            else:
                # Restore cozy ambient white if movie is paused
                for w in self.manager.workers.values():
                    w.send_white(brightness=600, temp=700)
                if self.manager.strip_worker:
                    self.manager.strip_worker.set_white(temp_k=2700, brightness=60)

    def _apply_ambient(self, force=False):
        """Applies ambient scene lighting to all bulbs with intelligent state caching."""
        now = time.monotonic()
        target_mode = self.manager.settings.get("ambilight_target", "strip_only")
        if target_mode == "off":
            return

        scenes = self.script.get("scenes", [])
        active_scene = scenes[0] if scenes else None
        for i, sc in enumerate(scenes):
            if self.current_time >= sc.get("start", 0):
                active_scene = sc
                self.last_scene_idx = i
            else:
                break

        if not active_scene:
            return

        self.active_scene_desc = active_scene.get("desc", "")
        tuya_color = str(active_scene.get("color", "002803e8"))
        bright_factor = active_scene.get("brightness", 30) / 100.0

        # Extract hue (and saturation, if the scene provides HHHHSSSS).
        try:
            hue_deg = int(tuya_color[:4], 16)
        except (TypeError, ValueError):
            hue_deg = 40
        try:
            sat_pct = clamp(int(tuya_color[4:8], 16) / 10.0, 0, 100, 95) if len(tuya_color) >= 8 else 95
        except (TypeError, ValueError):
            sat_pct = 95
        room_bright = max(5, int(bright_factor * max(60, self.brightness)))
        val = make_tuya_hex(hue_deg, sat_pct=sat_pct, val_pct=room_bright)

        # Send only when color changes, when forced, or on 4-second heartbeat
        if force or (val != self.last_sent_val) or (now - self.last_send_time >= 4.0):
            self.last_sent_val = val
            self.last_send_time = now

            # 1. Update Zigbee strip
            if target_mode in ("strip_only", "all") and self.manager.strip_worker:
                self.manager.strip_worker.set_color(hue_deg, brightness=max(50, room_bright))

            # 2. Update Bulbs
            if target_mode == "strip_only":
                if force or not getattr(self, "_bulbs_off_for_strip", False):
                    self.manager.broadcast_off(list(self.manager.workers.values()), wait=True)
                    self._bulbs_off_for_strip = True
            else:
                self._bulbs_off_for_strip = False
                workers = [w for w in self.manager.workers.values() if w.connected]
                if target_mode == "sconces_only":
                    workers = [w for w in workers if w.role == "snare"]
                self.manager.broadcast_color(workers, val)

    def tick(self, now_wall):
        now = time.monotonic()
        if self.playing:
            prev_time = self.current_time
            self.current_time = (now - self.start_wall_time) + self.offset
            
            # 1. Check for scripted spell cues in timeline using interval check (guaranteed no drops)
            for eff in self.script.get("effects", []):
                t = eff.get("time", 0.0)
                if (prev_time < t <= self.current_time) and (self.current_time - prev_time < 2.5):
                    self.trigger_spell(eff.get("type"), direction=eff.get("direction"))

        # 2. Skip ambient tick while directional beam is traveling
        if self.beam_in_progress:
            return

        # 3. Handle active spell overlay (Priority Level 1)
        if self.active_spell and now < self.spell_end_time:
            self._render_spell(self.active_spell, self.spell_end_time - now)
            return
        elif self.active_spell:
            self.active_spell = None
            self.last_sent_val = None

        if not self.playing:
            return

        # 4. Continuous Movie Ambilight (Priority Level 2)
        dt = now - self.last_tick
        if dt < 0.2:
            return
        self.last_tick = now

        self._apply_ambient(force=False)

    def _render_spell(self, spell, remain):
        """Renders high-impact magic bursts onto the room."""
        if not self.manager.settings.get("effects_enabled", True):
            return
        fx_target = self.manager.settings.get("effects_target", "bulbs_only")
        if fx_target == "off":
            return
        render_bulbs = fx_target in ("bulbs_only", "sconces_only", "all")
        render_sconces_only = fx_target == "sconces_only"
        render_strip = fx_target in ("strip_only", "all")

        # 🟢 AVADA KEDAVRA: Lethal emerald-green lightning burst
        if spell == "spell_avada":
            green_hi = make_tuya_hex(125, 100, 100)
            if render_bulbs:
                for w in self.manager.workers.values():
                    if render_sconces_only and w.role != "snare":
                        continue
                    w.send_color_val(green_hi)
            if render_strip and self.manager.strip_worker:
                self.manager.strip_worker.set_color(125, brightness=100)

        # 🔴 EXPELLIARMUS: Crimson ruby disarming pulse
        elif spell == "spell_expelliarmus":
            red_hi = make_tuya_hex(355, 100, 100)
            red_lo = make_tuya_hex(355, 100, 5)
            if render_bulbs:
                for w in self.manager.workers.values():
                    if render_sconces_only and w.role != "snare":
                        continue
                    if w.role == "snare":
                        w.send_color_val(red_hi)
                    else:
                        w.send_color_val(red_lo)
            if render_strip and self.manager.strip_worker:
                self.manager.strip_worker.set_color(355, brightness=100)

        # ⚪ LUMOS MAXIMA: Blinding white flare with slow dissipation
        elif spell in ("spell_lumos", "spell_lumos_maxima"):
            bright = int(max(20, min(100, (remain / 3.5) * 100)))
            white_val = make_tuya_hex(45, 10, bright)
            if render_bulbs:
                for w in self.manager.workers.values():
                    if render_sconces_only and w.role != "snare":
                        continue
                    w.send_color_val(white_val)
            if render_strip and self.manager.strip_worker:
                self.manager.strip_worker.set_white(temp_k=4500, brightness=bright)

        # 🦌 EXPECTO PATRONUM: Concentric silver-white stag waves
        elif spell in ("spell_patronus", "great_patronus"):
            wave = (math.sin(remain * 9.0) + 1.0) / 2.0
            silver_bright = int(50 + wave * 50)
            silver_val = make_tuya_hex(205, 30, silver_bright)
            if render_bulbs:
                for w in self.manager.workers.values():
                    if render_sconces_only and w.role != "snare":
                        continue
                    w.send_color_val(silver_val)
            if render_strip and self.manager.strip_worker:
                self.manager.strip_worker.set_color(205, brightness=silver_bright)

        # ⚡ LIGHTNING: Stereo thunderbolt strike in wall sconces
        elif spell == "quidditch_lightning":
            strobe = random.choice([True, False])
            bright_hi = make_tuya_hex(220, 20, 100)
            dim_lo = make_tuya_hex(220, 90, 5)
            if render_bulbs:
                for w in self.manager.workers.values():
                    if render_sconces_only and w.role != "snare":
                        continue
                    if w.role == "snare":
                        w.send_color_val(bright_hi if strobe else dim_lo)
                    else:
                        w.send_color_val(dim_lo)
            if render_strip and self.manager.strip_worker:
                self.manager.strip_worker.set_color(220, brightness=100 if strobe else 10)

        # ⏳ TIME TURNER: Clockwise temporal golden spin
        elif spell == "time_turner_spin":
            t = (remain * 720.0) % 360.0
            if render_bulbs:
                kick_workers = [w for w in self.manager.workers.values() if w.role in ["kick", "ambient"]]
                n = len(kick_workers)
                for i, w in enumerate(kick_workers):
                    angle = (360.0 / n) * i
                    diff = abs(t - angle)
                    if diff > 180.0: diff = 360.0 - diff
                    intensity = max(0.1, math.cos(math.radians(min(diff, 90.0))))
                    gold_val = make_tuya_hex(40, 95, int(intensity * 100))
                    w.send_color_val(gold_val)
            if render_strip and self.manager.strip_worker:
                self.manager.strip_worker.set_color(40, brightness=80)

        # ❄️ DEMENTOR FREEZE: Icy pale chill
        elif spell in ("dementor_freeze", "dementors_swarm"):
            ice_val = make_tuya_hex(195, 80, 8)
            if render_bulbs:
                for w in self.manager.workers.values():
                    if render_sconces_only and w.role != "snare":
                        continue
                    w.send_color_val(ice_val)
            if render_strip and self.manager.strip_worker:
                self.manager.strip_worker.set_color(195, brightness=15)


# =====================================================================
#  PartyManager Engine (Central Orchestration)
# =====================================================================

class PartyManager:
    HUES = [
        "0000",  # Red
        "0028",  # Orange
        "003c",  # Yellow
        "0078",  # Green
        "00b4",  # Cyan
        "00f0",  # Blue
        "0118",  # Violet
        "0140",  # Magenta
    ]

    def __init__(self, panel=None, bulbs_config_path=None, strip_config_path=None, settings_path=None, config=None):
        self.panel = panel
        base_dir = Path(__file__).resolve().parent
        try:
            from config import get_config
            self.config = config or get_config()
        except ImportError:
            self.config = None

        bulbs_def = self.config.party.bulbs_file if self.config else "bulbs.json"
        strip_def = self.config.party.strip_file if self.config else "strip_config.json"
        sett_def = self.config.party.settings_file if self.config else "party_settings.json"
        data_dir = self.config.server.data_dir if self.config else base_dir

        self.bulbs_config_path = self._resolve_path(
            bulbs_config_path,
            [data_dir / bulbs_def, base_dir / bulbs_def, Path("/data/yapanel") / bulbs_def, Path("/data/local/tmp") / bulbs_def]
        )
        self.strip_config_path = self._resolve_path(
            strip_config_path,
            [data_dir / strip_def, base_dir / strip_def, Path("/data/yapanel") / strip_def, Path("/data/local/tmp") / strip_def]
        )
        self.settings_path = self._resolve_path(
            settings_path,
            [data_dir / sett_def, base_dir / sett_def, Path("/data/yapanel") / sett_def, Path("/data/local/tmp") / sett_def]
        )
        self.store = PartyStore(self.settings_path, self.bulbs_config_path, self.strip_config_path)

        self.workers = WorkersDict()
        self.running = False
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._run_stop = threading.Event()
        self._run_generation = 0
        self._ticker_thread = None
        self._media_guard_thread = None
        self._discovery_thread = None
        self._audio_thread = None
        self._discovery_sock = None
        self._discovery_sockets = []
        self.audio_proc = None
        self.strip_worker = None
        self.is_android = os.path.exists("/system/bin/tinycap2")

        # Settings
        self.settings = self.store.load_settings()
        self._last_sound_time = 0.0
        self._last_media_ok = 0.0
        self._audio_error = None
        self._media_error = None
        self._scan_lock = threading.Lock()
        self._discovery_suspended = threading.Event()
        self._discovery_released = threading.Event()

        # Effect Registry
        self.effects = {
            "party": MusicStrobeEffect(self),
            "fireplace": FireplaceEffect(self),
            "aurora": AuroraEffect(self),
            "synthwave": SynthwaveEffect(self),
            "thunderstorm": ThunderstormEffect(self),
            "ocean": OceanEffect(self),
            "lava": LavaEffect(self),
            "orbit": OrbitEffect(self),
            "police": PoliceEffect(self),
            "cinema": MovieAmbilightEffect(self)
        }
        self.current_effect = self.effects.get(self.settings.get("active_mode", "cinema"), self.effects["cinema"])

        # Media Guard & Audio States
        self.yandex_playing = False
        self.bt_playing = False
        self.media_playing = False
        self.strobe_active = False
        self.strip_ambient_active = False
        self._bt_ambient_prev = None
        self._bt_ambient_manual = False
        self._silence_white_done = False
        self._white_hold_until = 0.0
        self.last_sound_time = time.time()
        self.in_silence = True

        # Synchronization Variables for Music Strobe
        self.kick_cond = threading.Condition()
        self.snare_cond = threading.Condition()
        self.kick_hue_index = 0
        self.kick_version = 0
        self.kick_val_hi = self.HUES[0] + "03e803e8"
        self.kick_val_lo = self.HUES[0] + "03e80032"
        self.kick_count = 0
        self.snare_hue_index = 0
        self.snare_version = 0
        self.snare_val_hi = self.kick_val_hi
        self.snare_val_lo = self.kick_val_lo
        self.snare_count = 0
        self.last_color_change_time = time.time()
        self.broadcast_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="PartyBcast")

    def broadcast_color(self, workers, val, timeout=0.060, wait=True):
        """Concurrently sends color to multiple bulbs with sub-20ms latency."""
        if not workers:
            return
        active = [w for w in workers if getattr(w, "connected", False) and getattr(w, "device", None) and getattr(w.device, "socket", None)]
        if not active:
            return
        if len(active) == 1 and wait:
            try:
                active[0].send_color_val(val)
            except Exception:
                pass
            return
        futures = [self.broadcast_pool.submit(w.send_color_val, val) for w in active]
        if not wait:
            return
        for f in futures:
            try:
                f.result(timeout=timeout)
            except Exception:
                pass

    def broadcast_map(self, worker_val_dict, timeout=0.060):
        """Concurrently sends distinct colors to distinct bulbs simultaneously."""
        if not worker_val_dict:
            return
        active_items = [(w, val) for w, val in worker_val_dict.items() if getattr(w, "connected", False) and getattr(w, "device", None) and getattr(w.device, "socket", None)]
        if not active_items:
            return
        futures = [self.broadcast_pool.submit(w.send_color_val, val) for w, val in active_items]
        for f in futures:
            try:
                f.result(timeout=timeout)
            except Exception:
                pass

    def broadcast_off(self, workers, wait=False):
        active = [w for w in workers if getattr(w, "connected", False) and getattr(w, "device", None)]
        futures = [self.broadcast_pool.submit(w.send_off) for w in active]
        if wait:
            for future in futures:
                try:
                    future.result(timeout=0.060)
                except Exception:
                    pass

    def _resolve_path(self, override, candidates):
        if override:
            return str(override)
        for c in candidates:
            if c and os.path.exists(str(c)):
                return str(c)
        return str(candidates[0])

    def load_settings(self):
        self.settings = self.store.load_settings()
        print(f"[Party] Loaded settings: {self.settings}")
        return self.settings

    def save_settings(self):
        try:
            self.settings = self.store.save_settings(self.settings)
        except Exception as error:
            print(f"[Party] Error saving settings: {error}")

    def load_bulbs(self):
        data = self.store.load_bulbs()
        for item in data:
            if not item.get("enabled", True) or item.get("role") not in ["kick", "snare", "ambient"]:
                continue
            try:
                worker = BulbWorker(item, self)
                self.workers[worker.dev_id] = worker
                worker.start()
            except Exception as error:
                print(f"[Party] Bulb worker init error ({item.get('id')}): {error}")
        print(f"[Party] Started {len(self.workers)} dedicated bulb workers.")

    def _apply_effect_settings(self, effect):
        if not effect:
            return
        effect.speed = clamp(self.settings.get("speed"), 0.2, 2.5, 1.0)
        effect.brightness = clamp_int(self.settings.get("brightness"), 10, 100, 80)

    def _select_effect(self):
        mode = self.settings.get("active_mode", "cinema")
        self.current_effect = self.effects.get(mode) or self.effects["cinema"]
        self._apply_effect_settings(self.current_effect)

    def start(self):
        with self._lifecycle_lock:
            if self.running:
                return
            self.running = True
            self._stop_event.clear()
            self._run_generation += 1
            generation = self._run_generation
            run_stop = threading.Event()
            self._run_stop = run_stop
            print("[Party] Starting Engine...")
            try:
                self.settings = normalize_settings(self.settings)
                for worker in list(self.workers.values()):
                    try:
                        worker.stop(timeout=0.2, restore=False)
                    except Exception:
                        pass
                self.workers.clear()
                self.load_bulbs()
                self.strip_worker = ZigbeeStripWorker(self.strip_config_path, self)
                self.strip_worker.start()
                self._select_effect()

                # Animation tick loop (~20 FPS)
                self._ticker_thread = threading.Thread(target=self._effect_tick_loop, args=(generation, run_stop), daemon=True, name="party-effect-ticker")
                self._ticker_thread.start()

                # Дать эффекту возможность выполнить start(): для cinema это play().
                try:
                    self.current_effect.start()
                    self.current_effect.tick(time.time())
                except Exception as error:
                    print(f"[Party] Effect start error: {error}")

                if self.is_android:
                    self._media_guard_thread = threading.Thread(target=self._media_guard_loop, args=(generation, run_stop), daemon=True, name="party-media-guard")
                    self._media_guard_thread.start()
                    self._discovery_thread = threading.Thread(target=self._discovery_loop, args=(generation, run_stop), daemon=True, name="party-discovery")
                    self._discovery_thread.start()
                    self._audio_thread = threading.Thread(target=self._audio_loop, args=(generation, run_stop), daemon=True, name="party-audio-loop")
                    self._audio_thread.start()
                else:
                    print("[Party] Not running on Android; audio capture disabled.")
                # Запоминаем явный запуск: после рестарта панели свет
                # восстановится только если пользователь его не выключал.
                try:
                    self.settings["running"] = True
                    self.save_settings()
                except Exception as error:
                    print(f"[Party] State save error: {error}")
            except Exception:
                self.running = False
                self._stop_event.set()
                if self._ticker_thread and self._ticker_thread.is_alive():
                    self._ticker_thread.join(timeout=0.2)
                    self._ticker_thread = None
                if self.strip_worker:
                    try:
                        self.strip_worker.stop(timeout=0.2)
                    except Exception:
                        pass
                    self.strip_worker = None
                for worker in list(self.workers.values()):
                    try:
                        worker.stop(timeout=0.2, restore=False)
                    except Exception:
                        pass
                self.workers.clear()
                raise

    def stop(self, persist=True):
        with self._lifecycle_lock:
            was_running = self.running
            print("[Party] Stopping Engine...")
            self.running = False
            self._stop_event.set()
            self._run_stop.set()
            self._run_generation += 1
            if self.current_effect:
                try:
                    self.current_effect.stop()
                except Exception:
                    pass
            if self._ticker_thread and self._ticker_thread.is_alive():
                self._ticker_thread.join(timeout=0.5)
                self._ticker_thread = None
            # Вернуть тёплый свет до закрытия сокетов.
            if was_running:
                try:
                    self.restore_white()
                except Exception:
                    pass
            if self.strip_worker:
                self.strip_worker.stop(timeout=0.5)
                self.strip_worker = None
            for w in list(self.workers.values()):
                w.stop(timeout=0.5, restore=False)
            self.workers.clear()
            with self.kick_cond:
                self.kick_cond.notify_all()
            with self.snare_cond:
                self.snare_cond.notify_all()
            if self._discovery_sock:
                try:
                    self._discovery_sock.close()
                except Exception:
                    pass
                self._discovery_sock = None
            for sock in self._discovery_sockets:
                try:
                    sock.close()
                except Exception:
                    pass
            self._discovery_sockets = []
            if self._discovery_thread and self._discovery_thread.is_alive():
                self._discovery_thread.join(timeout=0.5)
                self._discovery_thread = None
            if self._media_guard_thread and self._media_guard_thread.is_alive():
                self._media_guard_thread.join(timeout=0.5)
                self._media_guard_thread = None
            if self.audio_proc:
                try:
                    self.audio_proc.terminate()
                    self.audio_proc.wait(timeout=0.5)
                except Exception:
                    pass
                self.audio_proc = None
            if self._audio_thread and self._audio_thread.is_alive():
                self._audio_thread.join(timeout=0.5)
                self._audio_thread = None
            if self.broadcast_pool:
                try:
                    self.broadcast_pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                self.broadcast_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="PartyBcast")
            if persist:
                try:
                    self.settings["running"] = False
                    self.save_settings()
                except Exception as error:
                    print(f"[Party] State save error: {error}")

    def restart(self):
        # Не перетираем внешние изменения настроек остановкой старого движка.
        self.stop(persist=False)
        time.sleep(1.0)
        self.load_settings()
        self.start()

    def set_mode(self, mode_name, params=None, _internal=False):
        with self._lifecycle_lock:
            return self._set_mode_locked(mode_name, params, _internal)

    def _set_mode_locked(self, mode_name, params=None, _internal=False):
        mode_name = str(mode_name or "").strip().lower()
        if mode_name not in self.effects:
            print(f"[Party] Unknown mode: {mode_name}")
            return False
        if _internal and not self.running:
            return False
        if not _internal and self._bt_ambient_prev:
            # Пользователь сам выбрал режим — авто-эмбиент для ТВ больше не восстанавливаем.
            self._bt_ambient_prev = None
        if not _internal and mode_name == "party":
            self._bt_ambient_manual = True
        if not self.running:
            self.start()
        if self.current_effect:
            if getattr(self.current_effect, "is_cinema", False):
                self.current_effect._bulbs_off_for_strip = False
            self.current_effect.stop()

        self.settings["active_mode"] = mode_name
        self.settings["master_enabled"] = True
        self._white_hold_until = 0.0
        self.current_effect = self.effects[mode_name]
        self._apply_effect_settings(self.current_effect)
        if params and isinstance(params, dict):
            if "speed" in params:
                self.current_effect.speed = clamp(params["speed"], 0.2, 2.5, self.current_effect.speed)
                self.settings["speed"] = self.current_effect.speed
            if "brightness" in params:
                self.current_effect.brightness = clamp_int(params["brightness"], 10, 100, self.current_effect.brightness)
                self.settings["brightness"] = self.current_effect.brightness

        # Ensure all bulb workers force power ON and colour mode on preset switch
        for w in self.workers.values():
            w.force_full_power_on = True

        self.current_effect.start()
        # Immediately dispatch first tick
        try:
            self.current_effect.tick(time.time())
        except Exception:
            pass
        self.save_settings()
        print(f"[Party] Active mode switched to: {mode_name} ({self.current_effect.title})")
        return True

    def _alive(self, generation, stop_event):
        return (self.running and self._run_generation == generation
                and not stop_event.is_set())

    def _effect_tick_loop(self, generation=None, stop_event=None):
        """High-frequency animation loop (~20 FPS)."""
        generation = self._run_generation if generation is None else generation
        stop_event = self._stop_event if stop_event is None else stop_event
        while self._alive(generation, stop_event):
            now = time.time()
            if now < self._white_hold_until:
                if stop_event.wait(0.045):
                    break
                continue
            if self.current_effect:
                # In cinema mode, tick whenever movie is playing or spell is active, regardless of music master switch
                should_tick = True
                if not getattr(self.current_effect, "is_cinema", False):
                    should_tick = self.settings.get("master_enabled", True)
                elif not getattr(self.current_effect, "playing", False) and not getattr(self.current_effect, "active_spell", None) and not getattr(self.current_effect, "beam_in_progress", False):
                    should_tick = False

                if should_tick:
                    try:
                        self.current_effect.tick(now)
                    except Exception as e:
                        print(f"[Party] Effect tick error: {e}")
            if stop_event.wait(0.045):
                break

    def on_kick_internal(self, crest):
        now = time.time()
        self.last_sound_time = now
        self.in_silence = False
        self._silence_white_done = False
        self._white_hold_until = 0.0
        self.kick_count += 1

        interval = clamp_int(self.settings.get("color_interval"), 1, 256, 32)
        max_duration = 30.0 if interval >= 16 else max(5.0, interval * 1.5)

        if (self.kick_count % interval == 0) or (now - self.last_color_change_time >= max_duration):
            self.kick_hue_index = (self.kick_hue_index + 1) % len(self.HUES)
            self.last_color_change_time = now
            current_hue = self.HUES[self.kick_hue_index]
            self.kick_val_hi = current_hue + "03e803e8"
            self.kick_val_lo = current_hue + "03e80032"
            self.snare_hue_index = self.kick_hue_index
            self.snare_val_hi = self.kick_val_hi
            self.snare_val_lo = self.kick_val_lo

        self._flash_role("kick", self.kick_val_hi, self.kick_val_lo)

    def on_snare_internal(self, crest):
        self.last_sound_time = time.time()
        self.in_silence = False
        self._silence_white_done = False
        self._white_hold_until = 0.0
        self.snare_count += 1
        sync_mode = self.settings.get("sync_mode", "split")

        snare_color_hi = self.snare_val_hi
        snare_color_lo = self.snare_val_lo
        if sync_mode == "split":
            # Contrast hue for sconces
            contrast_hue = self.HUES[(self.kick_hue_index + 3) % len(self.HUES)]
            snare_color_hi = contrast_hue + "03e803e8"
            snare_color_lo = contrast_hue + "03e80032"

        self._flash_role("snare", snare_color_hi, snare_color_lo)

    def _flash_role(self, role, high, low, delay=0.055):
        """Не блокирует аудиопоток: hi сразу, lo — отдельным таймером."""
        workers = [w for w in self.workers.values() if w.role == role]
        if not workers:
            return
        self.broadcast_color(workers, high, wait=False)

        def low_pulse():
            self.broadcast_color(workers, low, wait=False)

        timer = threading.Timer(delay, low_pulse)
        timer.daemon = True
        timer.start()

    def restore_white(self):
        self._white_hold_until = time.time() + 2.5
        for w in list(self.workers.values()):
            try:
                w.send_white(800, 800)
            except Exception:
                pass
        if self.strip_worker:
            try:
                self.strip_worker.restore_white(3000, 80)
            except Exception:
                pass

    def reconnect_all(self):
        for w in self.workers.values():
            w.disconnect()
            w.last_reconnect_time = 0.0

    def get_status(self):
        bulbs_info = []
        for b_id, w in list(self.workers.items()):
            if w.role == "snare":
                friendly = "Бра правая" if w.side == "right" else "Бра левая"
                sort_order = 10 if w.side == "left" else 11
            elif w.role == "kick":
                friendly = f"Люстра ({w.name})"
                sort_order = 1
            elif w.role == "ambient":
                friendly = f"Люстра ({w.name}) • Фон"
                sort_order = 2
            else:
                friendly = w.name
                sort_order = 20
            bulbs_info.append({
                "id": b_id,
                "name": friendly,
                "raw_name": w.name,
                "role": w.role,
                "side": w.side,
                "pos_x": getattr(w, "pos_x", 0.0),
                "pos_y": getattr(w, "pos_y", 0.0),
                "connected": w.connected,
                "ip": w.ip,
                "val": w.current_val,
                "last_error": getattr(w, "last_error", None),
                "reconnects": getattr(w, "reconnect_count", 0),
                "sort": sort_order
            })
        bulbs_info.sort(key=lambda x: (x["sort"], x["ip"]))

        cinema = self.effects.get("cinema")
        cinema_info = {
            "playing": cinema.playing if cinema else False,
            "time_sec": cinema.current_time if cinema else 0.0,
            "offset_sec": cinema.offset if cinema else 0.0,
            "scene": cinema.active_scene_desc if cinema else "",
            "active_spell": cinema.active_spell if cinema else None,
            "duration_sec": cinema.script.get("duration_sec", 8502) if cinema else 8502,
            "title": cinema.script.get("movie") if cinema else "",
            "script": cinema.script_path if cinema else "",
        }

        strip = self.strip_worker
        strip_info = {
            "configured": bool(strip and getattr(strip, "configured", strip.enabled)),
            "enabled": bool(strip and strip.enabled),
            "device_id": getattr(strip, "device_id", None) if strip else None,
            "last_error": getattr(strip, "last_error", None) if strip else None,
            "last_sent": getattr(strip, "last_sent_state", None) if strip else None,
        }
        audio_info = {
            "capturing": bool(self._audio_thread and self._audio_thread.is_alive()),
            "media_playing": self.media_playing,
            "last_beat_sec": max(0.0, time.time() - self.last_sound_time) if self.last_sound_time else None,
            "error": self._audio_error,
        }
        silence_timeout = self.settings.get("silence_timeout", 10.0)
        recent = bool(self.last_sound_time) and (time.time() - self.last_sound_time <= silence_timeout)
        capturing = bool(self._audio_thread and self._audio_thread.is_alive())
        silent = not recent or (not capturing and not self.media_playing)
        self.in_silence = bool(silent)

        modes_info = [
            {"id": "party", "title": "Светомузыка", "icon": "⚡", "desc": "Клубный строб под ритм музыки"},
            {"id": "fireplace", "title": "Живой камин", "icon": "🔥", "desc": "Мерцание углей и тёплого огня"},
            {"id": "aurora", "title": "Северное сияние", "icon": "🌌", "desc": "Изумрудно-бирюзовые текучие волны"},
            {"id": "synthwave", "title": "Неоновый закат", "icon": "🌅", "desc": "Маджента и закатный персик"},
            {"id": "thunderstorm", "title": "Гроза и молнии", "icon": "⛈️", "desc": "Тёмный шторм с яркими вспышками"},
            {"id": "ocean", "title": "Океанская волна", "icon": "🌊", "desc": "Перекат волны лазури слева направо"},
            {"id": "lava", "title": "Лавовая лампа", "icon": "🌋", "desc": "Всплывающие пузыри рубина и магмы"},
            {"id": "orbit", "title": "Карусель", "icon": "💫", "desc": "Бегущий луч кометы по кругу"},
            {"id": "police", "title": "Полиция", "icon": "🚨", "desc": "Спецсигнал синий и красный"},
            {"id": "cinema", "title": "Кинотеатр 4D", "icon": "🎬", "desc": "Ambilight и магические спецэффекты"},
        ]

        return {
            "modes": modes_info,
            "running": self.running,
            "master_enabled": self.settings.get("master_enabled", True),
            "active_mode": self.settings.get("active_mode", "cinema"),
            "speed": self.settings.get("speed", 1.0),
            "brightness": self.settings.get("brightness", 80),
            "bt_mode": self.settings.get("bt_mode", "off"),
            "color_interval": self.settings.get("color_interval", 32),
            "sync_mode": self.settings.get("sync_mode", "split"),
            "ambilight_target": self.settings.get("ambilight_target", "strip_only"),
            "effects_enabled": self.settings.get("effects_enabled", True),
            "effects_target": self.settings.get("effects_target", "bulbs_only"),
            "yandex_playing": self.yandex_playing,
            "bt_playing": self.bt_playing,
            "strobe_active": self.strobe_active,
            "in_silence": self.in_silence,
            "kick_count": self.kick_count,
            "snare_count": self.snare_count,
            "workers_count": len(self.workers),
            "bulbs": bulbs_info,
            "cinema": cinema_info,
            "strip": strip_info,
            "audio": audio_info,
            "last_error": self.store.last_error or self._audio_error or self._media_error,
        }

    def update_settings(self, data):
        with self._lifecycle_lock:
            return self._update_settings_locked(data)

    def _update_settings_locked(self, data):
        if not isinstance(data, dict):
            return
        if "active_mode" in data:
            self.set_mode(data["active_mode"])

        candidate = dict(self.settings)
        for key in ("speed", "brightness", "bt_mode", "color_interval", "sync_mode",
                    "silence_timeout", "ambilight_target", "effects_enabled", "effects_target"):
            if key in data:
                candidate[key] = data[key]
        self.settings = normalize_settings(candidate)
        self._apply_effect_settings(self.current_effect)

        if "ambilight_target" in data:
            self._bulbs_off_for_strip = False
            if self.current_effect and hasattr(self.current_effect, "_apply_ambient"):
                try:
                    self.current_effect._apply_ambient(force=True)
                except Exception:
                    pass

        if "master_enabled" in data:
            self.settings = normalize_settings({**self.settings, "master_enabled": data["master_enabled"]})
            if self.settings["master_enabled"] and not self.running:
                self.start()
            elif not self.settings["master_enabled"] and not getattr(self.current_effect, "is_cinema", False):
                # Не оставлять лампы в последнем цвете выключенного эффекта.
                try:
                    self.restore_white()
                except Exception:
                    pass
        self.save_settings()

    def update_bulb_position(self, bulb_id, pos_x, pos_y=0.0, name=None):
        """Updates spatial coordinates and optional label of a bulb for directional beam effects."""
        bulb_id = str(bulb_id or "").strip()
        if not bulb_id:
            raise ValueError("Bulb ID required")
        x = clamp(pos_x, -5.0, 5.0, 0.0)
        y = clamp(pos_y, -5.0, 5.0, 0.0)
        worker = self.workers.get(bulb_id)
        if worker:
            worker.set_position(x, y)
            if name:
                worker.name = str(name)[:80]

        def mutate(items):
            for item in items:
                if item.get("id") == bulb_id:
                    item["pos_x"] = x
                    item["pos_y"] = y
                    if name:
                        item["name"] = str(name)[:80]
                    break
            return items

        try:
            self.store.update_bulbs(mutate)
        except Exception as error:
            print(f"[Party] Error saving bulb position: {error}")
            raise
        return {"status": "ok", "id": bulb_id, "pos_x": x, "pos_y": y}

    def save_bulb_ip(self, dev_id, new_ip):
        """Updates IP address in bulbs.json when dynamic auto-healing discovers a changed IP."""
        dev_id = str(dev_id or "").strip()
        new_ip = str(new_ip or "").strip()
        if not dev_id or not new_ip:
            return
        try:
            def mutate(items):
                for item in items:
                    if item.get("id") == dev_id:
                        item["ip"] = new_ip
                        break
                return items
            self.store.update_bulbs(mutate)
            print(f"[Party] Saved updated IP for {dev_id}: {new_ip}")
        except Exception as error:
            print(f"[Party] Error saving bulb IP: {error}")

    def get_bulbs(self):
        """Returns full list of configured bulbs with connectivity status."""
        raw_items = self.store.load_bulbs()

        # If bulbs.json was missing or empty, but workers exist, fallback to workers
        if not raw_items and self.workers:
            for b_id, w in self.workers.items():
                raw_items.append({
                    "id": b_id,
                    "name": w.name,
                    "ip": w.ip,
                    "key": getattr(w, "key", ""),
                    "ver": getattr(w, "ver", 3.5),
                    "role": w.role,
                    "side": getattr(w, "side", ""),
                    "pos_x": getattr(w, "pos_x", 0.0),
                    "pos_y": getattr(w, "pos_y", 0.0),
                    "enabled": True
                })

        bulbs_list = []
        for item in raw_items:
            b_id = item["id"]
            w = self.workers.get(b_id)
            bulbs_list.append({
                **item,
                "connected": bool(w.connected) if w else False,
                "val": w.current_val if w else None,
                "last_error": getattr(w, "last_error", None) if w else None,
                "reconnects": getattr(w, "reconnect_count", 0) if w else 0,
            })
        return bulbs_list

    def add_bulb(self, data):
        """Adds or updates a bulb in bulbs.json and spawns/reloads worker dynamically."""
        if not isinstance(data, dict):
            raise ValueError("Invalid payload, dict expected")
        existing_items = {item["id"]: item for item in self.store.load_bulbs()}
        bulb_id = str(data.get("id") or "").strip()
        payload = {**existing_items.get(bulb_id, {}), **data}
        if "side" in data and "pos_x" not in data:
            payload.pop("pos_x", None)
        entry = normalize_bulb(payload)
        if not entry:
            raise ValueError("Device ID (id) is required")
        if not entry["key"]:
            # При редактировании пустой ключ означает «оставить прежний».
            entry["key"] = existing_items.get(entry["id"], {}).get("key", "")
        if not entry["ip"] and entry["enabled"]:
            raise ValueError("IP address is required")
        if not entry["key"] and entry["enabled"]:
            raise ValueError("Local Key is required")

        def mutate(items):
            for index, item in enumerate(items):
                if item.get("id") == entry["id"]:
                    items[index] = entry
                    break
            else:
                items.append(entry)
            return items

        try:
            self.store.update_bulbs(mutate)
        except Exception as error:
            raise ValueError(f"Failed to save bulbs configuration: {error}")

        # Dynamically reload worker
        old_w = self.workers.get(entry["id"])
        if old_w:
            old_w.stop(timeout=0.3)
            self.workers.pop(entry["id"], None)

        if self.running and entry.get("enabled", True):
            new_w = BulbWorker(entry, self)
            self.workers[new_w.dev_id] = new_w
            new_w.start()

        return {"status": "ok", "bulb": entry}

    def delete_bulb(self, bulb_id):
        """Removes bulb from bulbs.json and terminates its worker."""
        b_id = str(bulb_id or "").strip()
        if not b_id:
            raise ValueError("Bulb ID required")

        try:
            self.store.update_bulbs(lambda items: [it for it in items if it.get("id") != b_id])
        except Exception as error:
            raise ValueError(f"Failed to update bulbs configuration: {error}")

        w = self.workers.get(b_id)
        if w:
            w.stop(timeout=0.3)
            self.workers.pop(b_id, None)

        return {"status": "ok", "id": b_id}

    def import_bulbs(self, list_data, overwrite=False):
        """Batch import bulbs from list or tinytuya devices export."""
        incoming = normalize_bulbs(list_data)
        if not incoming:
            raise ValueError("Expected list of bulbs or dict with devices")

        existing = {} if overwrite else {item["id"]: item for item in self.store.load_bulbs()}
        added = 0
        for item in incoming:
            current = existing.get(item["id"], {})
            if not item.get("key") and current.get("key"):
                item["key"] = current["key"]
            existing[item["id"]] = item
            added += 1

        try:
            final_items = self.store.save_bulbs(list(existing.values()))
        except Exception as error:
            raise ValueError(f"Failed to save bulbs: {error}")

        if self.running:
            for w in list(self.workers.values()):
                w.stop(timeout=0.3, restore=False)
            self.workers.clear()
            self.load_bulbs()

        return {"status": "ok", "count": added, "total": len(final_items)}

    @staticmethod
    def _parse_tuya_udp_packet(data, udp_key=None):
        if not data or len(data) < 16:
            return None
        # 1. Plaintext JSON
        try:
            m = re.search(rb'\{.*\}', data)
            if m:
                return json.loads(m.group(0).decode("utf-8", "ignore"))
        except Exception:
            pass

        # 2. AES-128-ECB decrypted JSON
        if udp_key is None:
            udp_key = hashlib.md5(b"yGAdlopoPVldABfn").digest()
        payload = None
        if len(data) >= 24 and data[:4] == b'\x00\x00\x55\xaa':
            payload = data[16:-8]
        elif len(data) >= 16:
            payload = data

        if payload and len(payload) >= 16 and len(payload) % 16 == 0:
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                cipher = Cipher(algorithms.AES(udp_key), modes.ECB())
                dec = cipher.decryptor()
                plain = dec.update(payload) + dec.finalize()
                pad = plain[-1]
                if isinstance(pad, int) and 1 <= pad <= 16:
                    plain = plain[:-pad]
                m = re.search(rb'\{.*\}', plain)
                if m:
                    return json.loads(m.group(0).decode("utf-8", "ignore"))
            except Exception:
                pass

        # 3. Binary regex for gwId / id
        m = re.search(rb'"gwId"\s*:\s*"([a-zA-Z0-9_-]+)"', data)
        if m:
            return {"gwId": m.group(1).decode("ascii", "ignore")}
        return None

    def scan_network(self, timeout=3.0):
        timeout = clamp(timeout, 1.0, 15.0, 3.0)
        with self._scan_lock:
            self._discovery_suspended.set()
            self._discovery_released.clear()
            self._close_discovery_sockets()
            if self._discovery_thread and self._discovery_thread.is_alive():
                self._discovery_released.wait(timeout=1.5)
            try:
                return self._scan_network_locked(timeout)
            finally:
                self._discovery_suspended.clear()

    def _scan_network_locked(self, timeout):
        """Scans LAN for Tuya smart bulbs via UDP broadcasts & optional tinytuya scan."""
        found = {}
        if tinytuya and hasattr(tinytuya, "deviceScan"):
            try:
                res = tinytuya.deviceScan(verbose=False, maxretry=1)
                if isinstance(res, dict):
                    for ip, dev in res.items():
                        if isinstance(dev, dict):
                            d_id = dev.get("gwId") or dev.get("id") or ""
                            if d_id:
                                found[ip] = {
                                    "ip": ip,
                                    "id": d_id,
                                    "ver": str(dev.get("version", "3.3")),
                                    "product_key": dev.get("productKey", "")
                                }
            except Exception as e:
                print(f"[Party] tinytuya scan error: {e}")

        udp_key = hashlib.md5(b"yGAdlopoPVldABfn").digest()
        for port in [6666, 6667]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_BROADCAST"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind(('', port))
                s.settimeout(0.4)
                t_end = time.time() + (timeout / 2.0)
                while time.time() < t_end:
                    try:
                        data, addr = s.recvfrom(2048)
                        ip = addr[0]
                        parsed = self._parse_tuya_udp_packet(data, udp_key)
                        if parsed:
                            dev_id = parsed.get("gwId") or parsed.get("id")
                            if dev_id:
                                found[ip] = {
                                    "ip": ip,
                                    "id": dev_id,
                                    "ver": str(parsed.get("version", "3.3")),
                                    "product_key": parsed.get("productKey", "")
                                }
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                s.close()
            except Exception:
                pass

        res_list = []
        configured_ids = {item["id"] for item in self.store.load_bulbs()}
        for ip, dev in found.items():
            matched_worker = None
            for w in list(self.workers.values()):
                if w.ip == ip or w.dev_id == dev["id"]:
                    matched_worker = w
                    break
            paired = matched_worker is not None or dev["id"] in configured_ids
            res_list.append({
                "ip": dev["ip"],
                "id": dev["id"],
                "ver": dev.get("ver", "3.3"),
                "product_key": dev.get("product_key", ""),
                "name": matched_worker.name if matched_worker else f"Лампа {ip}",
                "is_paired": paired
            })
        res_list.sort(key=lambda x: x["ip"])
        return res_list

    def test_bulb(self, bulb_id=None, action="solo", color="white"):
        """Individual bulb testing for spatial calibration:
        action:
          - 'solo': Turn on ONLY this bulb, turn off all others
          - 'on': Turn on this bulb
          - 'off': Turn off this bulb
          - 'flash': Flash this bulb 3 times green
          - 'all_white': Turn on all bulbs to warm white
          - 'all_off': Turn off all bulbs
        """
        if action == "all_white":
            for w in self.workers.values():
                w.send_white(brightness=800, temp=800)
            if self.strip_worker:
                self.strip_worker.set_white(3000, 80)
            return {"status": "ok", "action": "all_white"}

        if action == "all_off":
            for w in self.workers.values():
                w.send_off()
            if self.strip_worker:
                self.strip_worker.set_off()
            return {"status": "ok", "action": "all_off"}

        target = self.workers.get(bulb_id)
        if not target:
            return {"error": "Bulb not found", "id": bulb_id}

        if action == "solo":
            for b_id, w in self.workers.items():
                if b_id == bulb_id:
                    if color == "green":
                        w.send_color_val(make_tuya_hex(125, 100, 100))
                    elif color == "red":
                        w.send_color_val(make_tuya_hex(355, 100, 100))
                    elif color == "blue":
                        w.send_color_val(make_tuya_hex(220, 100, 100))
                    else:
                        w.send_white(brightness=1000, temp=800)
                else:
                    w.send_off()
            if self.strip_worker:
                self.strip_worker.set_off()
            return {"status": "ok", "action": "solo", "bulb": bulb_id}

        elif action == "on":
            target.send_white(brightness=1000, temp=800)
            return {"status": "ok", "action": "on", "bulb": bulb_id}

        elif action == "off":
            target.send_off()
            return {"status": "ok", "action": "off", "bulb": bulb_id}

        elif action == "flash":
            def _flash():
                for _ in range(3):
                    target.send_color_val(make_tuya_hex(125, 100, 100))
                    time.sleep(0.18)
                    target.send_off()
                    time.sleep(0.18)
                target.send_white(brightness=800, temp=800)
            threading.Thread(target=_flash, daemon=True).start()
            return {"status": "ok", "action": "flash", "bulb": bulb_id}

        return {"error": f"Unknown action {action}"}

    def set_ambilight_frame(self, data):
        """Live 3-zone Ambilight frame distribution across bulbs & strip."""
        if not isinstance(data, dict):
            raise ValueError("bad ambilight frame")
        target_mode = data.get("target") or self.settings.get("ambilight_target", "strip_only")
        if target_mode not in ("strip_only", "bulbs_only", "all", "off", "sconces_only"):
            raise ValueError("bad ambilight target")
        if target_mode == "off":
            return {"status": "ok", "mode": "off"}

        left_val = data.get("left")
        center_val = data.get("center")
        right_val = data.get("right")
        for value in (left_val, center_val, right_val):
            if value not in (None, "") and not re.fullmatch(r"[0-9a-fA-F]{8,12}", str(value)):
                raise ValueError("bad ambilight color")
        strobe = as_bool(data.get("strobe", False))
        
        # 1. Update Zigbee LED strip (when mode is "strip_only" or "all")
        if target_mode in ("strip_only", "all") and self.strip_worker and center_val:
            try:
                hue_deg = int(center_val[:4], 16)
                sat = min(100, max(20, int(int(center_val[4:8], 16) / 10.0)))
                bright = min(100, max(0, int(int(center_val[8:], 16) / 10.0)))
                if bright <= 4:
                    self.strip_worker.set_off()
                else:
                    self.strip_worker.set_color(hue_deg, brightness=bright, sat=sat)
            except Exception:
                pass
                
        # 2. Update Bulbs according to target_mode
        if target_mode == "strip_only":
            if not getattr(self, "_bulbs_off_for_strip", False):
                for w in self.workers.values():
                    w.send_off()
                self._bulbs_off_for_strip = True
            return {"status": "ok", "mode": "strip_only"}
            
        self._bulbs_off_for_strip = False
        mapping = {}
        for w in self.workers.values():
            if not w.connected:
                continue
            if target_mode == "sconces_only" and w.role != "snare":
                continue
            if strobe:
                mapping[w] = make_tuya_hex(45, 10, 100) # Full white strobe
            elif w.side == "left" or w.pos_x <= -0.5:
                if left_val: mapping[w] = left_val
            elif w.side == "right" or w.pos_x >= 0.5:
                if right_val: mapping[w] = right_val
            else:
                if center_val: mapping[w] = center_val
        if mapping:
            self.broadcast_map(mapping)
        return {"status": "ok"}


    def _media_guard_loop(self, generation=None, stop_event=None):
        generation = self._run_generation if generation is None else generation
        stop_event = self._stop_event if stop_event is None else stop_event
        while self._alive(generation, stop_event):
            try:
                res = run_android_cmd(["dumpsys", "media_session"])
                out = res.stdout
                yp, bp = False, False
                blocks = out.split("package=")
                for b in blocks:
                    if b.startswith("com.yandex.io.sdk") and "state=PlaybackState {state=3" in b:
                        yp = True
                    elif b.startswith("com.android.bluetooth") and "state=PlaybackState {state=3" in b:
                        bp = True

                if not bp:
                    aud = run_android_cmd(["dumpsys", "audio"]).stdout
                    if "players:" in aud:
                        players_chunk = aud.split("players:")[1].split("ducked")[0]
                        if "com.android.bluetooth" in players_chunk and "state:started" in players_chunk:
                            bp = True

                self.yandex_playing = yp
                self.bt_playing = bp
                self._media_error = None
                master_on = self.settings.get("master_enabled", True)
                bt_mode = self.settings.get("bt_mode", "off")

                if not master_on:
                    self.media_playing = False
                    self.strobe_active = False
                elif yp or (bp and bt_mode != "off"):
                    self.media_playing = True
                else:
                    self.media_playing = False

                if not self._alive(generation, stop_event):
                    break
                if master_on and bp and bt_mode == "ambilight" and self.settings.get("active_mode") == "party" and not self._bt_ambient_prev and not self._bt_ambient_manual:
                    self._bt_ambient_prev = "party"
                    self.set_mode("aurora", _internal=True)
                elif not bp:
                    self._bt_ambient_manual = False
                    if self._bt_ambient_prev:
                        previous = self._bt_ambient_prev
                        self._bt_ambient_prev = None
                        self.set_mode(previous, _internal=True)

            except Exception as error:
                self._media_error = f"{type(error).__name__}: {error}"
            if stop_event.wait(1.0):
                break

    def _close_discovery_sockets(self):
        for sock in self._discovery_sockets:
            try:
                sock.close()
            except Exception:
                pass
        self._discovery_sockets = []
        self._discovery_sock = None

    def _discovery_loop(self, generation=None, stop_event=None):
        generation = self._run_generation if generation is None else generation
        stop_event = self._stop_event if stop_event is None else stop_event
        sockets = []
        try:
            while self._alive(generation, stop_event):
                if self._discovery_suspended.is_set():
                    for sock in sockets:
                        try:
                            sock.close()
                        except Exception:
                            pass
                    sockets = []
                    self._discovery_sockets = []
                    self._discovery_sock = None
                    self._discovery_released.set()
                    if stop_event.wait(0.2):
                        break
                    continue
                if not sockets:
                    sockets = self._open_discovery_sockets()
                    self._discovery_sockets = sockets
                    self._discovery_sock = sockets[0] if sockets else None
                    if not sockets:
                        if stop_event.wait(1.0):
                            break
                        continue
                try:
                    ready, _, _ = select.select(sockets, [], [], 0.5)
                except (OSError, ValueError):
                    for sock in sockets:
                        try:
                            sock.close()
                        except Exception:
                            pass
                    sockets = []
                    continue
                for sock in ready:
                    try:
                        data, addr = sock.recvfrom(2048)
                    except (OSError, ValueError):
                        continue
                    ip = addr[0]
                    parsed = self._parse_tuya_udp_packet(data)
                    dev_id = ""
                    if isinstance(parsed, dict):
                        dev_id = str(parsed.get("gwId") or parsed.get("id") or "")
                    for b_id, w in list(self.workers.items()):
                        if dev_id:
                            matched = dev_id == b_id
                        else:
                            # Совместимость со старыми прошивками: точное вхождение ID.
                            matched = b_id.encode("ascii", "ignore") in data
                        if matched and (w.ip != ip or not w.connected):
                            w.update_ip(ip)
        except Exception as error:
            self._media_error = f"discovery: {type(error).__name__}: {error}"
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except Exception:
                    pass
            self._discovery_sockets = []
            self._discovery_sock = None

    def _open_discovery_sockets(self):
        sockets = []
        for port in (6666, 6667):
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
                sock.settimeout(0.5)
                sockets.append(sock)
            except OSError:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
        return sockets

    def _audio_loop(self, generation=None, stop_event=None):
        generation = self._run_generation if generation is None else generation
        stop_event = self._stop_event if stop_event is None else stop_event
        cmd = ["/system/bin/tinycap2", "--", "-D", "1", "-d", "1", "-c", "2", "-r", "48000", "-p", "512", "-n", "6"]
        proc = None
        try:
            self.audio_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            proc = self.audio_proc
        except Exception as e:
            self._audio_error = f"tinycap2 start: {type(e).__name__}: {e}"
            print(f"[Party] tinycap2 start error: {e}")
            return

        chunk_size = 2048
        detector = BeatDetector(fs=48000.0)

        try:
            while self._alive(generation, stop_event):
                proc = self.audio_proc
                if proc is None:
                    break
                if proc.poll() is not None:
                    if not self._alive(generation, stop_event):
                        break
                    self._audio_error = "tinycap2 exited; restarting"
                    if stop_event.wait(1.0):
                        break
                    if not self._alive(generation, stop_event):
                        break
                    detector.reset()
                    try:
                        self.audio_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                        proc = self.audio_proc
                    except Exception as error:
                        self._audio_error = f"tinycap2 restart: {type(error).__name__}: {error}"
                        if stop_event.wait(2.0):
                            break
                    continue

                raw = proc.stdout.read(chunk_size)
                if not raw or len(raw) < chunk_size:
                    if stop_event.wait(0.01):
                        break
                    continue
                self._audio_error = None

                count = len(raw) // 2
                samples = struct.unpack(f"<{count}h", raw)
                # Стерео → моно: иначе звук, панорамированный вправо, теряется.
                mono = [(samples[i] + samples[i + 1]) * 0.5 for i in range(0, count - 1, 2)]
                now = time.time()

                master = bool(self.settings.get("master_enabled", True))
                active_party = self.settings.get("active_mode") == "party"
                bt_allowed = not (self.bt_playing and self.settings.get("bt_mode") == "off")
                if master and active_party and bt_allowed:
                    events = detector.process(mono, now=now)
                    if events["kick"]:
                        if self.current_effect:
                            self.current_effect.on_kick(events["kick_level"])
                        self.strobe_active = True
                    if events["snare"]:
                        if self.current_effect:
                            self.current_effect.on_snare(events["snare_level"])
                        self.strobe_active = True

                silence_timeout = float(self.settings.get("silence_timeout", 10.0))
                if now - self.last_sound_time > silence_timeout:
                    self.in_silence = True
                    self.strobe_active = False
                    if not self._silence_white_done:
                        self._silence_white_done = True
                        try:
                            self.restore_white()
                            self._white_hold_until = time.time() + 3600.0
                        except Exception:
                            pass
                else:
                    self._silence_white_done = False
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            if self.audio_proc is proc:
                self.audio_proc = None
