"""Hardening tests for MaxDSP: serialized hardware access, validation, read-back."""

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import dsp_max
from dsp_max import MaxDSP


class FakePanel:
    def __init__(self, adb=None):
        self.adb = adb
        self.settings = {}

    def _load_settings(self):
        return self.settings


class FakeAdb:
    def __init__(self, code=0, exc=None):
        self.is_local = False
        self.code = code
        self.exc = exc
        self.calls = []

    def shell(self, command, check=False, **kwargs):
        self.calls.append(command)
        if self.exc:
            raise self.exc
        return "", "", self.code


@pytest.fixture
def local_dsp(tmp_path, monkeypatch):
    """MaxDSP wired to temporary sysfs files instead of real hardware paths."""
    woofer = tmp_path / "woofer_settings"
    tweeter = tmp_path / "tweeter_settings"
    woofer.write_text("", encoding="utf-8")
    tweeter.write_text("", encoding="utf-8")

    monkeypatch.setattr(dsp_max, "WOOFER_SYS", str(woofer))
    monkeypatch.setattr(dsp_max, "TWEETER_SYS", str(tweeter))
    monkeypatch.setattr(dsp_max, "DSP_FLAG_FILE", tmp_path / "hifi_dsp_applied")

    dsp = MaxDSP(FakePanel())
    # Tests must never touch the repository panel settings file.
    dsp._save_state = lambda: True
    dsp._update_boot_script = lambda: True
    return dsp


def run_concurrently(functions, timeout=10):
    """Start all callables at the same time, return their results or re-raise."""
    barrier = threading.Barrier(len(functions))
    results = [None] * len(functions)
    errors = []

    def runner(index, fn):
        try:
            barrier.wait(timeout=timeout)
            results[index] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the test thread
            errors.append(exc)

    threads = [
        threading.Thread(target=runner, args=(i, fn), daemon=True)
        for i, fn in enumerate(functions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
    assert not any(thread.is_alive() for thread in threads), "worker thread deadlocked"
    if errors:
        raise errors[0]
    return results


def test_concurrent_apply_profile_never_interleaves_batches(local_dsp, monkeypatch):
    dsp = local_dsp
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    batches = []
    real_write_batch = dsp._write_i2c_batch

    def slow_write_batch(commands):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            batches.append(list(commands))
        try:
            time.sleep(0.03)
            return real_write_batch(commands)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(dsp, "_write_i2c_batch", slow_write_batch)

    profiles = ["hifi", "night", "bass", "club"]
    results = run_concurrently(
        [lambda profile=profile: dsp.apply_profile(profile, save=False) for profile in profiles]
    )

    assert all(result["ok"] for result in results)
    assert max_active == 1, "concurrent POST /api/dsp calls overlapped in hardware code"
    assert len(batches) == len(profiles)

    # Each batch must be a complete mute -> registers -> unmute sequence.
    for commands in batches:
        assert commands[0][2] == "06 08"
        assert commands[1][2] == "22 30"
        assert commands[-4][2] == "22 00"
        assert commands[-1][2] == "06 00"


def test_lock_is_shared_between_maxdsp_instances(local_dsp, monkeypatch):
    first = local_dsp
    second = MaxDSP(FakePanel())
    second._save_state = lambda: True
    second._update_boot_script = lambda: True

    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def instrument(instance):
        real_write_batch = instance._write_i2c_batch

        def slow_write_batch(commands):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return real_write_batch(commands)
            finally:
                with state_lock:
                    active -= 1

        monkeypatch.setattr(instance, "_write_i2c_batch", slow_write_batch)

    instrument(first)
    instrument(second)

    results = run_concurrently(
        [
            lambda: first.apply_profile("night", save=False),
            lambda: second.apply_profile("bass", save=False),
        ]
    )

    assert all(result["ok"] for result in results)
    assert max_active == 1, "separate MaxDSP instances used different hardware locks"


def test_write_i2c_batch_direct_calls_are_serialized(local_dsp, monkeypatch):
    dsp = local_dsp
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def slow_verify(target_path, addr, payload):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            return True
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(dsp, "_verify_local_write", slow_verify)

    def write_one():
        return dsp._write_i2c_batch(
            [(dsp_max.WOOFER_SYS, dsp_max.ADDR_WOOFER, "06 08")]
        )

    results = run_concurrently([write_one] * 4)

    assert results == [True, True, True, True]
    assert max_active == 1
    assert "w 56 06 08" in Path(dsp_max.WOOFER_SYS).read_text(encoding="utf-8")


def test_unknown_profile_and_filter_return_ok_false(local_dsp):
    dsp = local_dsp
    profile_before = dsp.active_profile
    filters_before = dict(dsp.filters)
    params_before = dict(dsp.params)

    for bad_profile in ("does-not-exist", None, 123, ["hifi"], {"id": "hifi"}):
        result = dsp.apply_profile(bad_profile)
        assert result["ok"] is False
        assert "профиль" in result["error"].lower()

    for bad_filter in ("does-not-exist", None, 5, ["subsonic"], {"id": "subsonic"}):
        result = dsp.set_filter(bad_filter, True)
        assert result["ok"] is False

    assert dsp.action({"profile": "does-not-exist"})["ok"] is False
    assert dsp.action({"action": "set_filter", "filter": "nope"})["ok"] is False
    assert dsp.action(
        {"action": "set_filter", "filter": "subsonic", "enabled": "maybe"}
    )["ok"] is False
    assert dsp.action({"action": "no-such-action"})["ok"] is False
    assert dsp.action(
        {"action": "no-such-action", "filter": "subsonic", "enabled": True}
    )["ok"] is False
    assert dsp.action("not-a-dict")["ok"] is False
    assert dsp.action({"action": 42})["ok"] is False

    assert dsp.active_profile == profile_before
    assert dsp.filters == filters_before
    assert dsp.params == params_before


def test_params_are_clamped_and_bad_values_rejected(local_dsp):
    dsp = local_dsp

    result = dsp.set_params(
        {"bass_gain": 99, "treble_gain": -99, "subsonic_freq": 500}, save=False
    )
    assert result["ok"] is True
    assert dsp.params["bass_gain"] == 8.0
    assert dsp.params["treble_gain"] == -6.0
    assert dsp.params["subsonic_freq"] == 80

    result = dsp.set_params(
        {"bass_gain": -99, "treble_gain": 99, "subsonic_freq": 1}, save=False
    )
    assert result["ok"] is True
    assert dsp.params["bass_gain"] == -6.0
    assert dsp.params["treble_gain"] == 8.0
    assert dsp.params["subsonic_freq"] == 30

    before = dict(dsp.params)
    bad_payloads = [
        {"bass_gain": "loud"},
        {"treble_gain": None},
        {"subsonic_freq": [50]},
        {"bass_gain": float("nan")},
        {"treble_gain": float("inf")},
        {"bass_gain": 10 ** 1000},
        {"subsonic_freq": 10 ** 1000},
        {},
        None,
        ["bass_gain"],
    ]
    for bad in bad_payloads:
        result = dsp.set_params(bad, save=False)
        assert result["ok"] is False
    assert dsp.params == before

    # Numeric strings from form/query input remain accepted.
    assert dsp.set_params({"bass_gain": "2.5"}, save=False)["ok"] is True
    assert dsp.params["bass_gain"] == 2.5

    # Top-level action payload (legacy API shape) is validated too.
    assert dsp.action({"action": "set_params", "bass_gain": 99})["ok"] is True
    assert dsp.params["bass_gain"] == 8.0


def test_nested_hardware_lock_does_not_deadlock(local_dsp):
    dsp = local_dsp
    result = {}

    def restore():
        result["value"] = dsp.restore_hardware_state()

    thread = threading.Thread(target=restore, daemon=True)
    thread.start()
    thread.join(3)

    assert not thread.is_alive(), "restore_hardware_state() deadlocked on its own RLock"
    assert result["value"]["ok"] is True


def test_readback_failure_does_not_fail_successful_write(local_dsp, monkeypatch):
    dsp = local_dsp

    def broken_readback(*args, **kwargs):
        raise OSError("sysfs is write-only here")

    monkeypatch.setattr(dsp, "_verify_local_write", broken_readback)
    assert dsp._write_i2c_batch(
        [(dsp_max.WOOFER_SYS, dsp_max.ADDR_WOOFER, "06 08")]
    ) is True
    assert "w 56 06 08" in Path(dsp_max.WOOFER_SYS).read_text(encoding="utf-8")

    # A detected mismatch is advisory only as well.
    monkeypatch.setattr(dsp, "_verify_local_write", lambda *args, **kwargs: False)
    assert dsp._write_i2c_batch(
        [(dsp_max.TWEETER_SYS, dsp_max.ADDR_TWEETER, "06 08")]
    ) is True


def test_remote_adb_batch_has_single_shell_call_and_survives_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(dsp_max, "WOOFER_SYS", str(tmp_path / "missing_woofer"))
    monkeypatch.setattr(dsp_max, "TWEETER_SYS", str(tmp_path / "missing_tweeter"))

    adb = FakeAdb()
    dsp = MaxDSP(FakePanel(adb=adb))
    dsp._save_state = lambda: True
    dsp._update_boot_script = lambda: True

    commands = [
        (dsp_max.WOOFER_SYS, dsp_max.ADDR_WOOFER, "06 08"),
        ("__SLEEP__", "", "0.001"),
        (dsp_max.TWEETER_SYS, dsp_max.ADDR_TWEETER, "06 08"),
    ]

    assert dsp._write_i2c_batch(commands) is True
    batch_calls = [call for call in adb.calls if call.startswith('echo "w ')]
    assert len(batch_calls) == 1
    assert "usleep 1000" in batch_calls[0]
    assert "&&" in batch_calls[0]

    adb.code = 1
    assert dsp._write_i2c_batch(commands) is False

    adb.exc = OSError("adb connection lost")
    assert dsp._write_i2c_batch(commands) is False
    assert dsp.is_supported() is False


def test_save_state_preserves_other_settings_and_detects_silent_failure(
    local_dsp, monkeypatch
):
    dsp = local_dsp
    store = {"volume": 7, "dsp": {"profile": "old"}}

    class FakeServer:
        @staticmethod
        def load_settings():
            return json.loads(json.dumps(store))

        @staticmethod
        def save_settings(payload):
            store.clear()
            store.update(payload)

    fake_server = types.ModuleType("server")
    fake_server.load_settings = FakeServer.load_settings
    fake_server.save_settings = FakeServer.save_settings
    monkeypatch.setitem(sys.modules, "server", fake_server)

    assert MaxDSP._save_state(dsp) is True
    assert store["volume"] == 7
    assert store["dsp"]["profile"] == dsp.active_profile
    assert store["dsp"]["filters"] == dsp.filters
    assert store["dsp"]["params"] == dsp.params

    # server.save_settings() swallows OSError; the read-back must expose it.
    store["dsp"] = {"profile": "old"}
    fake_server.save_settings = lambda payload: None
    assert MaxDSP._save_state(dsp) is False


def test_boot_marker_is_updated_and_errors_are_contained(local_dsp, monkeypatch):
    dsp = local_dsp
    marker = Path(dsp_max.DSP_FLAG_FILE)

    dsp.active_profile = "hifi"
    assert MaxDSP._update_boot_script(dsp) is True
    assert marker.exists()

    # Маркер означает «панель управляет DSP», а не «включён Hi-Fi»:
    # watchdog не должен возвращать golden-профиль поверх stock.
    dsp.active_profile = "stock"
    assert MaxDSP._update_boot_script(dsp) is True
    assert marker.exists()

    monkeypatch.setattr(
        dsp_max, "DSP_FLAG_FILE", marker.parent / "missing-dir" / "hifi_dsp_applied"
    )
    dsp.active_profile = "hifi"
    assert MaxDSP._update_boot_script(dsp) is False
