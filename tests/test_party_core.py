"""Core hardening tests for Party Mode: state, validation, storage, strip."""
import json
import tempfile
import threading
import time
from pathlib import Path

import party
import party_store


def manager(tmp_path):
    return party.PartyManager(
        bulbs_config_path=str(tmp_path / "bulbs.json"),
        settings_path=str(tmp_path / "settings.json"),
        strip_config_path=str(tmp_path / "strip.json"),
    )


class FakeWorker:
    def __init__(self, dev_id="b1", role="kick", pos_x=0.0):
        self.dev_id = dev_id
        self.role = role
        self.name = "Лампа"
        self.ip = "192.168.1.10"
        self.key = "0123456789abcdef"
        self.ver = 3.5
        self.side = ""
        self.pos_x = pos_x
        self.pos_y = 0.0
        self.connected = True
        self.current_val = None
        self.last_error = None
        self.reconnect_count = 0
        self.force_full_power_on = False
        self.device = object()
        self.values = []
        self.white = []
        self.off = 0
        self.stopped = []

    def send_color_val(self, value, nowait=True):
        self.values.append(value)
        self.current_val = value
        return True

    def send_white(self, *args, **kwargs):
        self.white.append((args, kwargs))
        return True

    def send_off(self):
        self.off += 1
        return True

    def set_position(self, x, y=0.0):
        self.pos_x, self.pos_y = x, y

    def stop(self, timeout=0.5, restore=True):
        self.stopped.append((timeout, restore))

    def disconnect(self):
        self.connected = False

    def update_ip(self, ip):
        self.ip = ip


def test_mode_switch_keeps_speed_and_brightness(tmp_path):
    pm = manager(tmp_path)
    pm.running = True
    pm.settings["speed"] = 1.9
    pm.settings["brightness"] = 70
    pm.set_mode("fireplace")
    assert pm.current_effect.speed == 1.9
    assert pm.current_effect.brightness == 70
    pm.running = False


def test_settings_are_clamped_and_kick_does_not_divide_by_zero(tmp_path):
    pm = manager(tmp_path)
    pm.running = True
    pm.update_settings({"color_interval": 0, "brightness": 999, "speed": 99,
                        "ambilight_target": "bogus", "slence_timeout": 0})
    assert pm.settings["color_interval"] == 1
    assert pm.settings["brightness"] == 100
    assert pm.settings["speed"] == 2.5
    assert pm.settings["ambilight_target"] == "strip_only"
    pm.settings["color_interval"] = 0
    pm.on_kick_internal(1000)          # defensive clamp inside the audio path
    pm.running = False


def test_corrupt_bulbs_are_backed_up_and_engine_starts(tmp_path):
    bulbs = tmp_path / "bulbs.json"
    bulbs.write_text("{broken", encoding="utf-8")
    pm = manager(tmp_path)
    pm.start()
    try:
        assert pm.running is True
        assert pm.workers == {}
        assert pm.store.last_error
        assert list(tmp_path.glob("bulbs.json.corrupt-*"))
    finally:
        pm.stop()


def test_restart_selects_effect_from_reloaded_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"active_mode": "police"}), encoding="utf-8")
    pm = manager(tmp_path)
    settings.write_text(json.dumps({"active_mode": "cinema"}), encoding="utf-8")
    pm.restart()
    try:
        assert pm.settings["active_mode"] == "cinema"
        assert pm.current_effect.name == "cinema"
        assert pm.current_effect.playing is True
    finally:
        pm.stop()


def test_stop_restores_warm_white_and_marks_workers_stopped(tmp_path):
    pm = manager(tmp_path)
    pm.running = True
    worker = FakeWorker()
    pm.workers[worker.dev_id] = worker
    pm.stop()
    assert worker.white                    # restore_white called before stop
    assert worker.stopped and worker.stopped[0][1] is False


def test_status_exposes_strip_audio_and_silence(tmp_path):
    pm = manager(tmp_path)
    pm.running = False
    pm.strip_worker = party.ZigbeeStripWorker(None, pm)
    pm.strip_worker.enabled = True
    pm.strip_worker.last_error = "cloud 503"
    pm.last_sound_time = time.time() - 100
    status = pm.get_status()
    assert status["strip"]["enabled"] is True
    assert status["strip"]["last_error"] == "cloud 503"
    assert "audio" in status
    assert status["in_silence"] is True


def test_strip_payload_uses_real_brightness_and_retries(tmp_path, monkeypatch):
    pm = manager(tmp_path)
    strip = party.ZigbeeStripWorker(None, pm)
    strip.enabled = True
    strip.token = "token"
    strip.device_id = "device"
    actions = strip._actions_for_state(("hsv", 120, 50, 30))
    hsv = [a for a in actions if a["type"].endswith("color_setting")][0]
    assert hsv["state"]["value"]["v"] == 30
    assert hsv["state"]["value"]["s"] == 50

    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        if len(calls) == 1:
            raise OSError("temporary")
        return Response()

    monkeypatch.setattr(party_store, "json", json, raising=False)
    monkeypatch.setattr(party.urllib.request, "urlopen", fake_urlopen)
    assert strip._send_action_sync(actions) is False
    assert strip.last_error
    assert strip._send_action_sync(actions) is True
    assert strip.last_error is None


def test_party_store_keeps_original_on_corruption(tmp_path):
    path = tmp_path / "bulbs.json"
    path.write_text("[{bad", encoding="utf-8")
    store = party_store.PartyStore(tmp_path / "settings.json", path)
    assert store.load_bulbs() == []
    backups = list(tmp_path.glob("bulbs.json.corrupt-*"))
    assert backups and backups[0].read_text(encoding="utf-8") == "[{bad"


def test_dimensionless_animation_sequencer_cancels_previous(tmp_path):
    pm = manager(tmp_path)
    cinema = pm.effects["cinema"]
    cinema.beam_in_progress = True
    cinema.active_spell = "old"
    generation = cinema._anim_generation
    old_event = cinema._anim_event
    cinema._cancel_animations()
    assert cinema._anim_generation == generation + 1
    assert old_event.is_set()
    assert not cinema._anim_event.is_set()


def test_animation_sequencer_serializes_and_cancels(tmp_path):
    pm = manager(tmp_path)
    cinema = pm.effects["cinema"]
    order = []
    started = threading.Event()

    def slow():
        order.append("slow-start")
        started.set()
        cinema._sleep(5.0)
        order.append("slow-end")

    def quick():
        order.append("quick")

    cinema._start_anim(slow)
    assert started.wait(timeout=1.0)
    cinema._start_anim(quick)
    time.sleep(0.3)
    assert order == ["slow-start", "quick"]
