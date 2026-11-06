"""Transport hardening tests for Party Mode.

Covers:
- BulbWorker public send_* serialization through _io_lock;
- non-blocking broadcast_color(wait=False);
- non-blocking _flash_role() high/low pulses;
- Zigbee strip action payloads and error bookkeeping;
- discovery socket cleanup;
- get_status() strip/audio/silence reporting.

Everything is faked: no real sockets, no HTTP, no adb, no worker threads.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import party


def manager(tmp_path):
    return party.PartyManager(
        bulbs_config_path=str(tmp_path / "bulbs.json"),
        settings_path=str(tmp_path / "settings.json"),
        strip_config_path=str(tmp_path / "strip.json"),
    )


class FakeSocket:
    def __init__(self):
        self.closed = 0

    def fileno(self):
        return 7

    def close(self):
        self.closed += 1


class FakeDevice:
    """Records Tuya calls and detects overlapping device IO."""

    def __init__(self):
        self.socket = FakeSocket()
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def _record(self, name, args, kwargs):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.005)  # widen the race window
        with self._guard:
            self.calls.append((name, args, kwargs))
            self.active -= 1

    def set_value(self, *args, **kwargs):
        self._record("set_value", args, kwargs)

    def set_multiple_values(self, *args, **kwargs):
        self._record("set_multiple_values", args, kwargs)

    def close(self):
        pass


def make_bulb(monkeypatch):
    """BulbWorker with a fake device and drained (but real) socket code path."""
    worker = party.BulbWorker(
        {
            "id": "b1",
            "key": "0123456789abcdef",
            "ip": "192.168.1.10",
            "role": "kick",
            "name": "Лампа",
        },
        SimpleNamespace(running=False),
    )
    device = FakeDevice()
    worker.device = device
    worker.connected = True
    # select() must never touch a real descriptor in unit tests.
    monkeypatch.setattr(party, "select", SimpleNamespace(select=lambda *a, **k: ([], [], [])))
    return worker, device


def test_bulb_send_methods_are_serialized_by_io_lock(monkeypatch):
    worker, device = make_bulb(monkeypatch)
    color = "00f003e803e8"
    threads = 6
    rounds = 3

    def exercise(_):
        for _ in range(rounds):
            assert worker.send_color_val(color) is True
            assert worker.send_white(800, 800) is True
            assert worker.send_off() is True
            assert worker.send_on() is True

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(exercise, range(threads)))

    # No overlapping device calls: _io_lock serialized all four methods.
    assert device.max_active == 1
    assert len(device.calls) == threads * rounds * 4

    # Every value actually reached the device, not just the lock.
    color_calls = [
        call for call in device.calls
        if (call[0] == "set_value" and call[1][0] == "24")
        or (call[0] == "set_multiple_values" and "24" in call[1][0])
    ]
    white_calls = [
        call for call in device.calls
        if call[0] == "set_multiple_values" and call[1][0].get("21") == "white"
    ]
    off_calls = [call for call in device.calls if call[0] == "set_value" and call[1] == ("20", False)]
    on_calls = [call for call in device.calls if call[0] == "set_value" and call[1] == ("20", True)]
    assert len(color_calls) == threads * rounds
    assert len(white_calls) == threads * rounds
    assert len(off_calls) == threads * rounds
    assert len(on_calls) == threads * rounds

    # No send raised (a failed send drops the connection).
    assert worker.connected is True
    assert worker.current_val in (color, "white", "off")


def test_bulb_stop_restores_white_before_disconnect(monkeypatch):
    worker, _ = make_bulb(monkeypatch)
    events = []

    class OrderedDevice:
        socket = FakeSocket()

        def set_multiple_values(self, *args, **kwargs):
            events.append("white")

        def close(self):
            events.append("close")

    worker.device = OrderedDevice()
    worker.connected = True
    worker._stop_event.clear()

    worker.stop(timeout=0.01, restore=True)

    assert events == ["white", "close"]
    assert worker.connected is False
    assert worker.device is None
    assert worker._stop_event.is_set()


class SlowWorker:
    """Looks like an active BulbWorker but its send is deliberately slow."""

    def __init__(self, delay=0.25):
        self.role = "kick"
        self.connected = True
        self.device = SimpleNamespace(socket=object())
        self.delay = delay
        self.values = []
        self.done = threading.Event()
        self._lock = threading.Lock()

    def send_color_val(self, val, nowait=True):
        time.sleep(self.delay)
        with self._lock:
            self.values.append(val)
        self.done.set()
        return True


def test_broadcast_color_wait_false_does_not_wait_for_slow_send(tmp_path):
    pm = manager(tmp_path)
    workers = [SlowWorker(0.25), SlowWorker(0.25)]
    color = "00f003e803e8"
    try:
        started = time.perf_counter()
        pm.broadcast_color(workers, color, wait=False)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.1
        assert all(w.values == [] for w in workers)  # still in flight
        for worker in workers:
            assert worker.done.wait(2.0)
        assert all(w.values == [color] for w in workers)
    finally:
        pm.broadcast_pool.shutdown(wait=True)


class RoleWorker:
    def __init__(self, role):
        self.role = role
        self.name = role


def test_flash_role_sends_high_immediately_and_low_after_timer(tmp_path, monkeypatch):
    pm = manager(tmp_path)
    kick = RoleWorker("kick")
    snare = RoleWorker("snare")
    pm.workers["k1"] = kick
    pm.workers["s1"] = snare

    calls = []
    guard = threading.Lock()

    def spy(workers, val, timeout=0.060, wait=True):
        with guard:
            calls.append((time.perf_counter(), val, wait, list(workers)))

    monkeypatch.setattr(pm, "broadcast_color", spy)

    started = time.perf_counter()
    pm._flash_role("kick", "HIGH", "LOW", delay=0.12)
    elapsed = time.perf_counter() - started

    # The caller is not blocked by the low pulse (timer delay is 0.12 s).
    assert elapsed < 0.06
    assert len(calls) == 1
    assert calls[0][1] == "HIGH"
    assert calls[0][2] is False
    assert calls[0][3] == [kick]

    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline and len(calls) < 2:
        time.sleep(0.005)

    assert len(calls) == 2
    low_time, low_val, low_wait, low_workers = calls[1]
    assert low_val == "LOW"
    assert low_wait is False
    assert low_workers == [kick]
    assert low_time - started >= 0.09  # really deferred, not sent inline


def test_strip_actions_for_hsv_white_and_off(tmp_path):
    pm = manager(tmp_path)
    strip = party.ZigbeeStripWorker(None, pm)

    hsv_actions = strip._actions_for_state(("hsv", 120, 50, 30))
    by_type = {action["type"]: action for action in hsv_actions}
    color = by_type["devices.capabilities.color_setting"]["state"]["value"]
    brightness = by_type["devices.capabilities.range"]["state"]["value"]
    assert color == {"h": 120, "s": 50, "v": 30}
    assert color["v"] == brightness == 30  # v follows brightness, never hardcoded 100
    assert by_type["devices.capabilities.on_off"]["state"] == {"instance": "on", "value": True}

    # Zero brightness must stay zero as well.
    zero_actions = strip._actions_for_state(("hsv", 10, 100, 0))
    zero_color = [a for a in zero_actions if a["type"].endswith("color_setting")][0]
    assert zero_color["state"]["value"]["v"] == 0

    white = strip._actions_for_state(("white", 3000, 70))
    assert white == [
        {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}},
        {"type": "devices.capabilities.color_setting",
         "state": {"instance": "temperature_k", "value": 3000}},
        {"type": "devices.capabilities.range",
         "state": {"instance": "brightness", "value": 70}},
    ]

    off = strip._actions_for_state(("off",))
    assert off == [
        {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}
    ]
    assert strip._actions_for_state(None) is None


def test_send_action_sync_sets_and_clears_last_error(tmp_path, monkeypatch):
    pm = manager(tmp_path)
    strip = party.ZigbeeStripWorker(None, pm)
    strip.enabled = True
    strip.token = "token"
    strip.device_id = "device"
    strip.last_error = None

    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout=0):
        attempts.append(request)
        if len(attempts) == 1:
            raise OSError("cloud is down")
        return Response()

    monkeypatch.setattr(party.urllib.request, "urlopen", fake_urlopen)
    actions = strip._actions_for_state(("off",))

    assert strip._send_action_sync(actions) is False
    assert strip.last_error and "OSError" in strip.last_error
    assert len(attempts) == 1

    assert strip._send_action_sync(actions) is True
    assert strip.last_error is None
    assert len(attempts) == 2

    request = attempts[1]
    assert request.full_url.endswith("/devices/actions")
    assert request.get_header("Authorization") == "Bearer token"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload == {"devices": [{"id": "device", "actions": actions}]}


class FakeDiscoverySocket:
    def __init__(self, fail_close=False):
        self.close_attempts = 0
        self.closed = 0
        self.fail_close = fail_close

    def close(self):
        self.close_attempts += 1
        if self.fail_close:
            raise OSError("already closed")
        self.closed += 1


def test_close_discovery_sockets_closes_all_and_clears_lists(tmp_path):
    pm = manager(tmp_path)
    sockets = [
        FakeDiscoverySocket(),
        FakeDiscoverySocket(fail_close=True),
        FakeDiscoverySocket(),
    ]
    pm._discovery_sockets = list(sockets)
    pm._discovery_sock = sockets[0]

    pm._close_discovery_sockets()  # must not raise on the failing socket

    assert [s.close_attempts for s in sockets] == [1, 1, 1]
    assert sockets[0].closed == 1
    assert sockets[2].closed == 1  # a failing close does not abort the cleanup
    assert pm._discovery_sockets == []
    assert pm._discovery_sock is None


def test_get_status_reports_strip_audio_and_silence_timeout(tmp_path):
    pm = manager(tmp_path)
    strip = party.ZigbeeStripWorker(None, pm)
    strip.enabled = True
    strip.device_id = "strip-device"
    strip.last_error = "cloud 503"
    strip.last_sent_state = ("hsv", 1, 2, 3)
    pm.strip_worker = strip
    pm.running = False  # no ticker/audio/discovery threads

    pm.settings["silence_timeout"] = 10.0
    pm.media_playing = True
    pm.last_sound_time = time.time()
    fresh = pm.get_status()

    assert fresh["strip"]["enabled"] is True
    assert fresh["strip"]["device_id"] == "strip-device"
    assert fresh["strip"]["last_error"] == "cloud 503"
    assert fresh["strip"]["last_sent"] == ("hsv", 1, 2, 3)
    assert fresh["audio"]["capturing"] is False
    assert fresh["audio"]["media_playing"] is True
    assert fresh["audio"]["error"] is None
    assert fresh["audio"]["last_beat_sec"] is not None
    assert "last_error" in fresh
    assert fresh["in_silence"] is False

    pm.last_sound_time = time.time() - 11.0
    assert pm.get_status()["in_silence"] is True

    pm.last_sound_time = time.time()
    pm.media_playing = False
    assert pm.get_status()["in_silence"] is True

    pm.store.last_error = "bulbs.json: broken"
    assert pm.get_status()["last_error"] == "bulbs.json: broken"

    pm.store.last_error = None
    pm._audio_error = "mic dead"
    assert pm.get_status()["last_error"] == "mic dead"
