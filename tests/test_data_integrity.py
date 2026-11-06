"""Регрессии целостности данных: настройки, токены, restore, cookie, pyadb и DSP.

Все внешние зависимости (adb, sysfs, HTTP) заменены фейками; реальное
устройство и ADB не нужны.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import socket
import struct
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import access
import dsp_max
import glagol
import pyadb
import server
from dsp_max import MaxDSP


# ---------- server.save_settings ----------
def test_save_settings_is_atomic_and_keeps_old_file_on_replace_error(tmp_path, monkeypatch):
    settings = tmp_path / ".panel_settings.json"
    settings.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(server, "SETTINGS_FILE", settings)

    real_replace = os.replace

    def broken_replace(src, dst):
        if Path(dst) == settings:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(server.os, "replace", broken_replace)
    assert server.save_settings({"new": True}) is False

    assert json.loads(settings.read_text(encoding="utf-8")) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == [settings.name], \
        "после ошибки записи рядом остался временный файл"


def test_save_settings_success_leaves_no_temp_file(tmp_path, monkeypatch):
    settings = tmp_path / ".panel_settings.json"
    monkeypatch.setattr(server, "SETTINGS_FILE", settings)

    assert server.save_settings({"device": "192.0.2.1:5555"}) is True
    assert json.loads(settings.read_text(encoding="utf-8")) == {"device": "192.0.2.1:5555"}
    assert [p.name for p in tmp_path.iterdir()] == [settings.name]


# ---------- YandexAuth ----------
@pytest.mark.parametrize("payload", [
    b"{broken json",
    b"",
    b"[]",
    b"null",
    b'"just-a-string"',
    b"\xff\xfe\x00",
])
def test_yandex_auth_corrupt_file_does_not_break_constructor(tmp_path, payload):
    path = tmp_path / ".yandex_auth.json"
    path.write_bytes(payload)

    auth = glagol.YandexAuth(path)          # не должно быть исключения
    assert auth.music_token is None
    info = auth.info()
    assert info["logged_in"] is False
    assert path.read_bytes() == payload     # битый файл не перезаписывается молча


def test_yandex_auth_missing_file_is_not_logged_in(tmp_path):
    auth = glagol.YandexAuth(tmp_path / ".yandex_auth.json")
    assert auth.music_token is None
    assert auth.info()["logged_in"] is False


# ---------- multi.py: имя cookie из конфигурации ----------
def test_multi_uses_configured_cookie_name(monkeypatch):
    import config

    custom = "custom_panel_session"
    monkeypatch.setattr(
        config, "get_config",
        lambda: SimpleNamespace(security=SimpleNamespace(cookie_name=custom)),
    )
    monkeypatch.setattr(access, "COOKIE", "stale_cookie_name")
    import multi

    saved_devices = dict(multi.DEVICES)
    saved_cookie = access.COOKIE
    try:
        importlib.reload(multi)
        assert multi.cookie_name() == custom
        assert access.COOKIE == custom, "multi.py перетёр настроенное имя cookie"
    finally:
        multi.DEVICES.clear()
        multi.DEVICES.update(saved_devices)
        access.COOKIE = saved_cookie


def test_multi_cookie_name_falls_back_to_access_on_broken_config(monkeypatch):
    import config

    def broken_config():
        raise RuntimeError("settings are broken")

    monkeypatch.setattr(config, "get_config", broken_config)
    monkeypatch.setattr(access, "COOKIE", "fallback_panel_session")
    import multi

    saved_devices = dict(multi.DEVICES)
    saved_cookie = access.COOKIE
    try:
        importlib.reload(multi)
        assert multi.cookie_name() == "fallback_panel_session"
        assert access.COOKIE == "fallback_panel_session"
    finally:
        multi.DEVICES.clear()
        multi.DEVICES.update(saved_devices)
        access.COOKIE = saved_cookie


# ---------- pyadb: пакет больше MAX_PAYLOAD ----------
def _oversized_adbd(payload_len):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(2)
                try:
                    conn.recv(4096)            # CNXN от клиента
                except OSError:
                    pass
                conn.sendall(struct.pack(
                    "<6I", pyadb.A_CNXN, 0, 0, payload_len, 0,
                    pyadb.A_CNXN ^ 0xFFFFFFFF,
                ))
                time.sleep(0.05)
        finally:
            srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread


@pytest.mark.parametrize("payload_len", [pyadb.MAX_PAYLOAD + 1, 0xFFFFFFFF])
def test_pyadb_rejects_packet_larger_than_max_payload(payload_len):
    port, thread = _oversized_adbd(payload_len)
    try:
        with pytest.raises(pyadb.AdbError, match="payload too large|protocol fault"):
            pyadb.Device(f"127.0.0.1:{port}", timeout=2)
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()


# ---------- backup_restore: ошибка установки возвращает старые файлы ----------
class RestoreAdb:
    is_local = False

    def __init__(self):
        self.commands = []
        self.pushes = []
        self.pulls = []

    def shell(self, cmd, timeout=15, check=True, **_):
        self.commands.append(cmd)
        if "find . -type f" in cmd:
            return "./boot.binscled.zst\n", "", 0
        return "", "", 0

    def pull(self, remote, local, timeout=120):
        self.pulls.append((remote, str(local)))
        Path(local).write_bytes(b"OLD-BOOT")
        return len(b"OLD-BOOT")

    def push(self, local, remote, timeout=120):
        data = Path(local).read_bytes()
        self.pushes.append((str(remote), data))
        return len(data)


class RestorePanel(server.Panel):
    def __init__(self, adb):
        self.adb = adb
        self.reset_calls = 0
        self.install_calls = []
        self.ensure_hook_calls = 0

    def footprint_reset(self):
        self.reset_calls += 1
        return {"items": []}

    def boot_status(self):
        return {"supported": True, "sound_supported": False}

    def _install_override(self, data, custom, target, orig=None):
        self.install_calls.append((custom, target, data))
        raise server.AdbError("не удалось записать DSP/заставку")

    def hook_status(self):
        return {"hook": True}

    def _ensure_hook(self, *args, **kwargs):
        self.ensure_hook_calls += 1
        return {}


def _backup_archive(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps({"format": 1, "created": "2026-01-01T00:00:00"}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        for name, data in files.items():
            info = tarfile.TarInfo("webpanel/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_backup_restore_rolls_back_when_install_fails():
    adb = RestoreAdb()
    panel = RestorePanel(adb)

    body = _backup_archive({"boot.binscled.zst": b"NEW-BOOT"})
    with pytest.raises(server.AdbError, match="не удалось записать"):
        panel.backup_restore(body)

    # Установка действительно началась после сброса...
    assert panel.reset_calls == 1
    assert panel.install_calls
    # ...но старый файл из /data/quasar/webpanel возвращён, а не потерян.
    assert (f"{server.WEB_DIR}/boot.binscled.zst", b"OLD-BOOT") in adb.pushes
    assert panel.ensure_hook_calls == 1


def test_backup_restore_does_not_reset_on_invalid_archive():
    adb = RestoreAdb()
    panel = RestorePanel(adb)

    with pytest.raises(server.AdbError):
        panel.backup_restore(b"this is not a tar archive")

    assert panel.reset_calls == 0
    assert adb.pushes == []


# ---------- DSP: общий lock сериализует операции ----------
class DspPanel:
    def __init__(self, adb):
        self.adb = adb

    def _load_settings(self):
        return {}


class BlockingAdb:
    """adb, который зависает на пакетной записи I2C и считает одновременные вызовы."""

    is_local = False

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.commands = []

    def shell(self, cmd, timeout=15, check=True, **_):
        self.commands.append(cmd)
        if 'echo "w ' not in cmd:
            return "0", "", 0
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.entered.set()
        try:
            self.release.wait(5)
        finally:
            with self.guard:
                self.active -= 1
        return "", "", 0


def test_dsp_lock_serializes_apply_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(dsp_max, "WOOFER_SYS", str(tmp_path / "no_woofer_settings"))
    monkeypatch.setattr(dsp_max, "TWEETER_SYS", str(tmp_path / "no_tweeter_settings"))
    monkeypatch.setattr(dsp_max, "DSP_FLAG_FILE", tmp_path / "hifi_dsp_applied")

    adb = BlockingAdb()
    dsp = MaxDSP(DspPanel(adb))

    results = []
    errors = []

    def apply(profile):
        try:
            results.append(dsp.apply_profile(profile, save=False))
        except BaseException as error:  # noqa: BLE001 - покажем в тесте
            errors.append(error)

    first = threading.Thread(target=apply, args=("night",), daemon=True)
    second = threading.Thread(target=apply, args=("bass",), daemon=True)
    first.start()
    assert adb.entered.wait(2), "первый apply_profile не дошёл до записи I2C"
    second.start()
    time.sleep(0.2)                      # даём второму потоку дойти до lock/записи
    adb.release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive(), "потоки DSP зависли (lock/deadlock)"
    assert errors == []
    assert len(results) == 2 and all(result["ok"] for result in results)
    assert adb.max_active == 1, "операции DSP выполнялись параллельно"
    assert len([cmd for cmd in adb.commands if 'echo "w ' in cmd]) >= 2
