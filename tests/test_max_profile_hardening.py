"""Hardening tests for max_profile.py: BT config, /proc writes, reset safety, preexec, play log."""
import contextlib
import importlib.util
import logging
import os
import re
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import max_profile
import profiles

ROOT = Path(__file__).resolve().parents[1]


def fresh_server():
    spec = importlib.util.spec_from_file_location("isolated_panel_hardening", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _restore_ctrl_cache():
    old = max_profile._cached_quasar_ctrl
    max_profile._cached_quasar_ctrl = None
    yield
    max_profile._cached_quasar_ctrl = old


# ---------------------------------------------------------------------------
# bt_config.conf: backup before write, atomic replace, restore on error
# ---------------------------------------------------------------------------

class FakeAdb:
    is_local = False

    def __init__(self, responder=None):
        self.commands = []
        self.pushed = []
        self.responder = responder

    def shell(self, cmd, check=False, timeout=None):
        self.commands.append(cmd)
        if self.responder is not None:
            res = self.responder(cmd)
            if res is not None:
                return res
        return ("", "", 0)

    def push(self, local, remote):
        self.pushed.append((Path(local).read_text(encoding="utf-8"), remote))


class PushFailAdb(FakeAdb):
    def push(self, local, remote):
        raise OSError("push failed")


@pytest.fixture
def bt_paths(tmp_path, monkeypatch):
    target = tmp_path / "bt_config.conf"
    backup = tmp_path / "bt_config.conf.bak-webpanel"
    monkeypatch.setattr(max_profile, "BT_CONF_PATH", str(target))
    monkeypatch.setattr(max_profile, "BT_CONF_BACKUP", str(backup))
    return target, backup


def test_bt_conf_local_backup_and_atomic_write(bt_paths):
    target, backup = bt_paths
    target.write_text("[AA:BB:CC:DD:EE:FF]\nName=Old\n", encoding="utf-8")

    max_profile.max_write_bt_conf(SimpleNamespace(is_local=True), "[11:22:33:44:55:66]\nName=New\n")

    assert target.read_text(encoding="utf-8") == "[11:22:33:44:55:66]\nName=New\n"
    assert backup.read_text(encoding="utf-8") == "[AA:BB:CC:DD:EE:FF]\nName=Old\n"
    assert not list(target.parent.glob(".bt_config.conf.tmp-*"))


def test_bt_conf_local_restores_previous_content_when_write_fails(bt_paths, monkeypatch):
    target, backup = bt_paths
    old = "[AA:BB:CC:DD:EE:FF]\nName=Old\n"
    target.write_text(old, encoding="utf-8")

    real_replace = os.replace

    def broken_replace(src, dst):
        if Path(dst) == target:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(max_profile.os, "replace", broken_replace)
    with pytest.raises(OSError):
        max_profile.max_write_bt_conf(SimpleNamespace(is_local=True), "[11:22:33:44:55:66]\nName=New\n")

    assert target.read_text(encoding="utf-8") == old
    assert backup.read_text(encoding="utf-8") == old
    assert not list(target.parent.glob(".bt_config.conf.tmp-*"))


def test_bt_conf_local_rejects_empty_content(bt_paths):
    target, _ = bt_paths
    target.write_text("[AA:BB:CC:DD:EE:FF]\nName=Old\n", encoding="utf-8")

    with pytest.raises(ValueError):
        max_profile.max_write_bt_conf(SimpleNamespace(is_local=True), "")

    assert target.read_text(encoding="utf-8") == "[AA:BB:CC:DD:EE:FF]\nName=Old\n"


def test_bt_conf_remote_backs_up_before_atomic_move():
    adb = FakeAdb()

    max_profile.max_write_bt_conf(adb, "[11:22:33:44:55:66]\nName=New\n")

    joined = "\n".join(adb.commands)
    assert "/data/misc/bluedroid/bt_config.conf.bak-webpanel" in joined
    assert "mv -f" in joined
    assert ">/data/misc/bluedroid/bt_config.conf" not in joined

    backup_idx = next(i for i, c in enumerate(adb.commands) if "bak-webpanel" in c and "cp -f" in c)
    move_idx = next(i for i, c in enumerate(adb.commands) if "mv -f" in c)
    assert backup_idx < move_idx

    content, remote = adb.pushed[0]
    assert content == "[11:22:33:44:55:66]\nName=New\n"
    assert remote.startswith("/data/local/tmp/")


def test_bt_conf_remote_restores_backup_when_move_fails():
    def responder(cmd):
        if "cp -f" in cmd and "webpanel.tmp" in cmd:
            return ("", "mv failed", 1)
        return ("", "", 0)

    adb = FakeAdb(responder)
    with pytest.raises(OSError):
        max_profile.max_write_bt_conf(adb, "[11:22:33:44:55:66]\nName=New\n")

    restore = [c for c in adb.commands if "bak-webpanel" in c and "cp -f" in c]
    # first command is the backup, the last one is the rollback copy
    assert len(restore) >= 2
    assert "if [ -f" in restore[-1]
    assert "cp -f /data/misc/bluedroid/bt_config.conf.bak-webpanel " in restore[-1]


def test_bt_conf_remote_does_not_restore_after_failed_backup():
    def responder(cmd):
        if "bak-webpanel" in cmd and "mv -f" not in cmd and "webpanel.tmp" not in cmd:
            return ("", "backup failed", 3)
        return ("", "", 0)

    adb = FakeAdb(responder)
    with pytest.raises(OSError):
        max_profile.max_write_bt_conf(adb, "[11:22:33:44:55:66]\nName=New\n")

    assert adb.pushed == []
    assert len([c for c in adb.commands if "bak-webpanel" in c]) == 1


def test_bt_conf_remote_restores_backup_when_push_fails():
    adb = PushFailAdb()
    with pytest.raises(OSError):
        max_profile.max_write_bt_conf(adb, "[11:22:33:44:55:66]\nName=New\n")

    joined = "\n".join(adb.commands)
    assert "bak-webpanel" in joined
    assert "cp -f /data/misc/bluedroid/bt_config.conf.bak-webpanel " in joined


# ---------------------------------------------------------------------------
# Quasar /proc/<pid>/mem: pid, cmdline and address validation
# ---------------------------------------------------------------------------

PID = 4242
ADDR = 0x1000
MAPS = "00001000-00002000 rw-p 00000000 00:00 0\n"


def make_fake_proc(tmp_path, monkeypatch, pid=PID, cmdline=b"com.yandex.io.sdk\x00",
                   maps=MAPS, mem=b"\xaa" * 0x2000):
    proc = tmp_path / "proc"
    pdir = proc / str(pid)
    pdir.mkdir(parents=True)
    (pdir / "cmdline").write_bytes(cmdline)
    (pdir / "maps").write_text(maps, encoding="utf-8")
    (pdir / "mem").write_bytes(mem)
    monkeypatch.setattr(max_profile, "_PROC_ROOT", str(proc))
    return pdir


def patch_found_addr(monkeypatch, pid=PID, addr=ADDR):
    monkeypatch.setattr(max_profile, "find_quasar_controller_addr", lambda target_pid=None: (pid, addr))


def test_clock_pause_writes_for_valid_quasar_pid(tmp_path, monkeypatch):
    pdir = make_fake_proc(tmp_path, monkeypatch)
    patch_found_addr(monkeypatch)
    max_profile._cached_quasar_ctrl = (PID, ADDR)

    assert max_profile.max_set_quasar_clock_paused(SimpleNamespace(is_local=True), pause=False) is True

    data = (pdir / "mem").read_bytes()
    assert data[ADDR] == 1
    assert data[:ADDR] == b"\xaa" * ADDR


@pytest.mark.parametrize(
    "pid,cmdline,addr",
    [
        (PID + 1, b"com.yandex.io.sdk\x00", ADDR),        # pid does not exist
        (PID, b"com.android.bluetooth\x00", ADDR),        # not Quasar
        (PID, b"com.yandex.io.sdk\x00", 0),               # zero address
        (PID, b"com.yandex.io.sdk\x00", 0x10),            # below user space
        (PID, b"com.yandex.io.sdk\x00", 1 << 60),         # above 48-bit user space
        (PID, b"com.yandex.io.sdk\x00", 0x5000),          # not in /proc/<pid>/maps
    ],
)
def test_clock_pause_rejects_bad_pid_or_address(tmp_path, monkeypatch, pid, cmdline, addr):
    pdir = make_fake_proc(tmp_path, monkeypatch, pid=PID, cmdline=cmdline)
    patch_found_addr(monkeypatch, pid=pid, addr=addr)
    max_profile._cached_quasar_ctrl = (pid, addr)

    assert max_profile.max_set_quasar_clock_paused(SimpleNamespace(is_local=True), pause=False) is False
    assert (pdir / "mem").read_bytes() == b"\xaa" * 0x2000
    assert max_profile._cached_quasar_ctrl is None


def test_clock_pause_resets_cache_when_mem_open_fails(tmp_path, monkeypatch):
    pdir = make_fake_proc(tmp_path, monkeypatch)
    (pdir / "mem").unlink()
    patch_found_addr(monkeypatch)
    max_profile._cached_quasar_ctrl = (PID, ADDR)

    assert max_profile.max_set_quasar_clock_paused(SimpleNamespace(is_local=True), pause=True) is False
    assert max_profile._cached_quasar_ctrl is None


# ---------------------------------------------------------------------------
# footprint reset: only panel-owned paths, dry-run is read-only
# ---------------------------------------------------------------------------

class FakeDeviceAdb:
    is_local = False

    def __init__(self):
        self.commands = []

    def shell(self, cmd, check=True, timeout=None):
        self.commands.append(cmd)
        return ("", "", 0)

    @contextlib.contextmanager
    def only_adb(self):
        yield


def installed_max_panel():
    s = fresh_server()
    profiles.install(s, "max")
    p = s.Panel(FakeDeviceAdb())
    p._agent_forget = Mock()
    p.ring_reset = Mock()
    return s, p


def test_footprint_reset_removes_only_known_panel_paths(caplog):
    s, p = installed_max_panel()
    footprint = {"items": [], "dir": s.WEB_DIR, "size_kb": 0, "any": False, "yam_installed": False}
    p.footprint = Mock(return_value=footprint)

    with caplog.at_level(logging.INFO, logger="max_profile"):
        res = p._footprint_reset()

    assert res is footprint
    assert p.ring_reset.call_count == 1
    cmd = "\n".join(p.adb.commands)
    assert "svc bluetooth enable" in cmd
    assert "/data/quasar/rootkeeper.sh" not in cmd
    assert "panel-owned paths" in caplog.text

    known = {
        s.BOOT_CUSTOM, s.SOUND_CUSTOM, s.BOOT_ALWAYS, s.BOOT_HOOK, s.BT_OFF,
        s.HOSTS_CUSTOM, s.ROOTKEEPER_CREATED, s.ASSIST_NO_AUTOSTART,
        s.WAKE_AUTOSTART, s.WAKE_FRIDA_AUTOSTART,
        f"{s.WEB_DIR}/override", f"{s.WEB_DIR}/patterns", f"{s.WEB_DIR}/viz",
        f"{s.WEB_DIR}/bt_off", f"{s.WEB_DIR}/hosts",
    }
    removed = []
    for match in re.finditer(r"rm -rf ('(?:[^']*)'|\S+)", cmd):
        raw = match.group(1)
        removed.append(shlex.split(raw)[0] if raw.startswith("'") else raw)
    assert removed, "reset must remove known panel files"
    assert set(removed) <= known


def test_footprint_reset_dry_run_changes_nothing():
    s, p = installed_max_panel()
    footprint = {"items": [], "dir": s.WEB_DIR, "size_kb": 0, "any": False, "yam_installed": False}
    p.footprint = Mock(return_value=footprint)

    res = p._footprint_reset(dry_run=True)

    assert res is footprint
    assert p.adb.commands == []
    assert p._agent_forget.call_count == 0
    assert p.ring_reset.call_count == 0
    assert any("пути панели" in item["msg"] for item in s.JOURNAL.items)


# ---------------------------------------------------------------------------
# max_android_cmd: safe preexec_fn fallback
# ---------------------------------------------------------------------------

def local_adb():
    return SimpleNamespace(is_local=True)


@pytest.mark.parametrize("posix,threads", [(True, 4), (False, 1)])
def test_android_cmd_falls_back_without_preexec(monkeypatch, posix, threads):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(max_profile, "_POSIX", posix)
    monkeypatch.setattr(max_profile.threading, "active_count", lambda: threads)
    monkeypatch.setattr(max_profile.subprocess, "run", fake_run)

    assert max_profile.max_android_cmd(local_adb(), ["dumpsys", "media_session"]) == "ok"
    assert len(calls) == 1
    assert "preexec_fn" not in calls[0]


def test_android_cmd_keeps_preexec_on_single_threaded_posix(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(max_profile, "_POSIX", True)
    monkeypatch.setattr(max_profile.threading, "active_count", lambda: 1)
    monkeypatch.setattr(max_profile.subprocess, "run", fake_run)

    assert max_profile.max_android_cmd(local_adb(), ["dumpsys", "media_session"]) == "ok"
    assert len(calls) == 1
    assert callable(calls[0]["preexec_fn"])


def test_android_cmd_retries_without_preexec_when_platform_rejects_it(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        if "preexec_fn" in kwargs:
            raise ValueError("preexec_fn is not supported on this platform")
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(max_profile, "_POSIX", True)
    monkeypatch.setattr(max_profile.threading, "active_count", lambda: 1)
    monkeypatch.setattr(max_profile.subprocess, "run", fake_run)

    assert max_profile.max_android_cmd(local_adb(), ["dumpsys", "media_session"]) == "ok"
    assert len(calls) == 2
    assert "preexec_fn" in calls[0]
    assert "preexec_fn" not in calls[1]


def test_android_cmd_uses_shell_context_on_android(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="Bluetooth Status\n  enabled: true")

    monkeypatch.setattr(max_profile, "_android_shell_available", lambda: True)
    monkeypatch.setattr(max_profile.subprocess, "run", fake_run)

    out = max_profile.max_android_cmd(local_adb(), ["dumpsys", "bluetooth_manager"])
    assert out.startswith("Bluetooth")
    assert len(calls) == 1
    args, _ = calls[0]
    assert args[:2] == ["/system/bin/sh", "-c"]
    assert "u:r:shell:s0" in args[2]
    assert "dumpsys bluetooth_manager" in args[2]


# ---------------------------------------------------------------------------
# max_play_remote_wav: do not fail while playaudio is still starting
# ---------------------------------------------------------------------------

class PlayAdb:
    def __init__(self, tails):
        self.tails = list(tails)
        self.commands = []

    def shell(self, cmd, check=True, timeout=None):
        self.commands.append(cmd)
        if "tail -c" in cmd:
            return self.tails.pop(0) if self.tails else ("", "", 0)
        return ("", "", 0)


def play_panel(tails):
    s = fresh_server()
    profiles.install(s, "max")
    panel = Mock()
    panel.adb = PlayAdb(tails)
    panel.stop_sound = Mock()
    panel.detach = Mock()
    return s, s.Panel._play_remote_wav, panel


def test_play_remote_wav_waits_for_playaudio_log(monkeypatch):
    monkeypatch.setattr(max_profile.time, "sleep", lambda _: None)
    s, play, panel = play_panel([("", "", 0), ("started\n", "", 0)])

    play(panel, "/tmp/a.wav")

    tails = [c for c in panel.adb.commands if "tail -c" in c]
    assert len(tails) >= 2


def test_play_remote_wav_transient_error_is_not_fatal(monkeypatch):
    monkeypatch.setattr(max_profile.time, "sleep", lambda _: None)
    s, play, panel = play_panel([("busy\n", "", 0), ("playing\n", "", 0)])

    play(panel, "/tmp/a.wav")

    assert "playaudio" in panel.detach.call_args[0][1]


def test_play_remote_wav_still_reports_real_error(monkeypatch):
    monkeypatch.setattr(max_profile.time, "sleep", lambda _: None)
    s, play, panel = play_panel([("cannot open /tmp/a.wav\n", "", 0)] * 3)

    with pytest.raises(s.AdbError, match="Не удалось проиграть звук"):
        play(panel, "/tmp/a.wav")
