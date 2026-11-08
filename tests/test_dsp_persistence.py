"""DSP persistence: retry, reapply after track changes and atomic settings."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import dsp_max
import server
from dsp_max import MaxDSP


class Panel:
    def __init__(self, adb=None):
        self.adb = adb
        self.settings = {}

    def _load_settings(self):
        return self.settings


class Adb:
    def __init__(self):
        self.is_local = False
        self.calls = []

    def shell(self, command, check=False, **kwargs):
        self.calls.append(command)
        return "", "", 0


@pytest.fixture
def dsp(tmp_path, monkeypatch):
    woofer = tmp_path / "woofer"
    tweeter = tmp_path / "tweeter"
    woofer.write_text("", encoding="utf-8")
    tweeter.write_text("", encoding="utf-8")
    monkeypatch.setattr(dsp_max, "WOOFER_SYS", str(woofer))
    monkeypatch.setattr(dsp_max, "TWEETER_SYS", str(tweeter))
    monkeypatch.setattr(dsp_max, "DSP_FLAG_FILE", tmp_path / "hifi_dsp_applied")
    monkeypatch.setattr(server, "SETTINGS_FILE", tmp_path / ".panel_settings.json")
    monkeypatch.setattr(server, "JOURNAL", server.Journal())
    inst = MaxDSP(Panel())
    inst._save_state = lambda: True
    inst._update_boot_script = lambda: True
    return inst


def test_restore_does_not_mark_success_when_write_fails(dsp, monkeypatch):
    monkeypatch.setattr(dsp, "_write_i2c_batch", lambda commands: False)
    dsp.active_profile = "hifi"
    result = dsp.restore_hardware_state()

    assert result["ok"] is False
    assert dsp._restored is False
    assert dsp._restore_attempts == 1


def test_restore_does_not_reload_stale_settings(dsp, monkeypatch):
    """maybe_restore не должен затирать только что выбранный эквалайзер файлом."""
    dsp.is_supported = lambda: True
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 0.0]
    dsp.active_profile = "custom"
    dsp.params["eq"] = values
    dsp.panel.settings = {"dsp": {"profile": "hifi", "params": {"eq": [0.0] * 10}}}
    monkeypatch.setattr(dsp, "_write_i2c_batch", lambda commands: True)

    result = dsp.restore_hardware_state()

    assert result["ok"] is True
    assert dsp.active_profile == "custom"
    assert dsp.params["eq"] == values


def test_maybe_restore_retries_with_backoff(dsp, monkeypatch):
    dsp._restored = False
    dsp._restore_attempts = 0
    dsp._last_restore_ts = 0.0
    calls = []

    monkeypatch.setattr(dsp, "is_supported", lambda: True)
    monkeypatch.setattr(
        dsp,
        "restore_hardware_state",
        lambda: calls.append(1) or {"ok": False, "error": "busy"},
    )

    assert dsp.maybe_restore() is False
    assert dsp.maybe_restore() is False
    assert len(calls) == 1, "backoff must suppress immediate retries"
    assert dsp._restore_attempts == 1


def test_force_reapply_is_coalesced(dsp, monkeypatch):
    calls = []
    real_write = dsp._write_i2c_batch

    def write(commands):
        calls.append(commands)
        return real_write(commands)

    monkeypatch.setattr(dsp, "_write_i2c_batch", write)
    dsp.apply_profile("hifi", save=False)
    assert dsp._expected_regs
    before = len(calls)

    dsp._last_reapply_ts = 0.0
    assert dsp.ensure_applied(force=True) is True
    assert len(calls) == before + 1
    assert dsp.ensure_applied(force=True) is False
    assert len(calls) == before + 1, "reapply cooldown must prevent a second write"


def test_verify_is_unknown_for_write_only_sysfs(dsp):
    # The real SY6045S settings files are mode --w------- (write-only).
    assert dsp._expected_regs is not None
    assert dsp.verify_registers() is None


def test_remote_boot_marker_is_created(dsp):
    adb = Adb()
    dsp.adb = adb
    assert MaxDSP._update_boot_script(dsp) is True
    assert any(": > /tmp/hifi_dsp_applied" in call for call in adb.calls)


def test_settings_section_updates_do_not_clobber_each_other(tmp_path, monkeypatch):
    path = tmp_path / ".panel_settings.json"
    monkeypatch.setattr(server, "SETTINGS_FILE", path)
    monkeypatch.setattr(server, "JOURNAL", server.Journal())

    errors = []

    def dsp_writer():
        try:
            for i in range(50):
                server.update_settings_section("dsp", {"profile": "hifi", "seq": i})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def mute_writer():
        try:
            for i in range(50):
                server.merge_settings(mute_before=i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=dsp_writer), threading.Thread(target=mute_writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["dsp"]["profile"] == "hifi"
    assert data["mute_before"] == 49
    assert set(data) >= {"dsp", "mute_before"}
