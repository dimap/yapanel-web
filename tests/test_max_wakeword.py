"""Слова активации на Станции Макс: /data/wakeword, Frida, boot hook."""
import contextlib
import hashlib
import importlib.util
import shlex
from pathlib import Path
from unittest.mock import Mock

import pytest

import profiles

ROOT = Path(__file__).resolve().parents[1]


def fresh_server():
    spec = importlib.util.spec_from_file_location("isolated_panel_ww", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAdb:
    def __init__(self, responder=None):
        self.commands = []
        self.pushed = []
        self.responder = responder or (lambda cmd: ("", "", 0))

    def shell(self, cmd, check=True, timeout=None):
        self.commands.append(cmd)
        out, err, rc = self.responder(cmd)
        if check and rc:
            raise RuntimeError(err or out or "shell failed")
        return out, err, rc

    def push(self, local, remote, timeout=None):
        data = Path(local).read_bytes()
        self.pushed.append((data, remote))
        return ""

    def read_bytes(self, path):
        return b"{}"

    @contextlib.contextmanager
    def only_adb(self):
        yield


def max_panel(responder=None):
    s = fresh_server()
    profiles.install(s, "max")
    p = s.Panel(FakeAdb(responder))
    p._agent_forget = Mock()
    p.ring_reset = Mock()
    return s, p


def test_binary_paths_are_local_and_pinned():
    s, _ = max_panel()
    assert s.WAKE_BIN == "/data/wakeword/wakeword"
    assert s.WAKE_NEGATIVES == "/data/wakeword/negatives.bin"
    assert s.WAKE_FRIDA_DIR == "/data/wakeword/frida"
    assert s.WAKE_BIN_SHA256.startswith("643c279b")


def test_installed_parses_sha_version_and_model():
    pin = "643c279b3327f1a110a8c65bc9e48bc70d305b4bee1efba1e491d9f94f61849f"

    def responder(cmd):
        return (f"0.3.0-maxpkg\n@bin\n@sha:{pin}\n@model\n@neg\n@jar\n@inject\n@fjs\n@fsh\n@auto\n", "", 0)

    s, p = max_panel(responder)
    info = p._wakeword_installed(force=True)
    assert info["installed"] and info["bin"] and info["bin_ok"]
    assert info["version"] == "0.3.0-maxpkg"
    assert info["sha256"] == pin
    assert info["model"] and info["auto"]
    assert info["negatives"] and info["jar"] and info["inject"]
    assert info["frida_js"] and info["frida_sh"] and info["files_ok"]


def test_installed_missing_bulk_files_is_not_complete():
    pin = "643c279b3327f1a110a8c65bc9e48bc70d305b4bee1efba1e491d9f94f61849f"

    def responder(cmd):
        return (f"0.3.0-maxpkg\n@bin\n@sha:{pin}\n@fjs\n@fsh\n", "", 0)

    s, p = max_panel(responder)
    info = p._wakeword_installed(force=True)
    assert info["installed"] and info["bin_ok"]
    assert not info["negatives"] and not info["jar"] and not info["inject"]
    assert not info["files_ok"]


def test_start_refuses_unverified_binary():
    s, p = max_panel()
    p._wakeword_installed = Mock(return_value={
        "installed": True, "bin": True, "bin_ok": False, "sha256": "deadbeef"})
    p.wakeword_frida_start = Mock()
    with pytest.raises(s.AdbError):
        p.wakeword_start()
    assert not any(" start " in c for c in p.adb.commands)


def test_start_uses_android_flags_loopback_and_no_wrappers():
    s, p = max_panel()
    p._wakeword_installed = Mock(return_value={
        "installed": True, "bin": True, "bin_ok": True, "sha256": s.WAKE_BIN_SHA256})
    p.wakeword_frida_start = Mock()
    p.wakeword_status = Mock(return_value={"running": True, "error": ""})
    p.wakeword_start()

    cmd = "\n".join(p.adb.commands)
    assert "-platform android -audio-source vqe" in cmd
    assert f"-listen 127.0.0.1:{s.WAKE_PORT}" in cmd
    assert s.WAKE_BIN in cmd
    assert "/data/yam" not in cmd
    assert "maxctl" not in cmd
    assert "start-stop-daemon" in cmd


def test_boot_hook_has_panel_binds_and_both_autostarts():
    s, p = max_panel()
    p._ensure_hook()

    content = next(data.decode("utf-8") for data, remote in p.adb.pushed if remote == s.BOOT_HOOK)
    assert "bind " in content
    assert s.BOOT_ALWAYS in content
    assert s.WAKE_AUTOSTART in content
    assert s.WAKE_FRIDA_AUTOSTART in content
    assert s.WAKE_FRIDA_SH in content
    assert "-platform android -audio-source vqe" in content
    assert "127.0.0.1:%d" % s.WAKE_PORT in content
    assert "maxctl" not in content
    assert "/data/yam" not in content
    assert "yamd" not in content
    assert "start-stop-daemon" in content   # /odm/bin/start-stop-daemon с fallback nohup


def test_hook_status_is_fresh_only_for_generated_script():
    s, p = max_panel()
    p._ensure_hook()
    hook = next(data for data, remote in p.adb.pushed if remote == s.BOOT_HOOK)
    digest = hashlib.md5(hook).hexdigest()

    def responder(cmd):
        return (f"@слова активации\n@активация Алисы (Frida)\nhook\n{digest}  {s.BOOT_HOOK}\n", "", 0)

    p2 = s.Panel(FakeAdb(responder))
    st = p2.hook_status()
    assert st["hook"] and st["fresh"] and not st["broken"]
    assert "слова активации" in st["users"]
    assert "активация Алисы (Frida)" in st["users"]


def test_hook_status_detects_stale_hook():
    s, p = max_panel(lambda cmd: ("@слова активации\nhook\n0000  /data/quasar/webpanel/boot_hook.sh\n", "", 0))
    st = p.hook_status()
    assert st["hook"] and not st["fresh"] and st["broken"]


def test_frida_status_parses_heartbeat():
    def responder(cmd):
        return ("@run\n@js\n@sh\n@inject\n@age:7\n", "", 0)

    s, p = max_panel(responder)
    st = p.wakeword_frida_status()
    assert st["supported"] and st["running"] and st["heartbeat"]
    assert st["heartbeat_age"] == 7
    assert st["js"] and st["sh"] and st["inject"]


def test_frida_assets_are_bound_to_panel():
    s, p = max_panel()
    assert p.max_wakeword_push_assets() is True
    remotes = {remote for _, remote in p.adb.pushed}
    assert s.WAKE_FRIDA_JS in remotes
    assert s.WAKE_FRIDA_SH in remotes


def test_install_uses_local_binary_and_bulk_assets_not_yam():
    s, p = max_panel()
    p._wakeword_installed = Mock(return_value={
        "installed": True, "bin": True, "bin_ok": False, "sha256": "deadbeef"})
    p.wakeword_status = Mock(return_value={"running": False})
    p.wakeword_install()
    remotes = [remote for _, remote in p.adb.pushed]
    assert f"{s.WAKE_BIN}.new" in remotes
    assert f"{s.WAKE_NEGATIVES}.new" in remotes
    assert f"{s.WAKE_JAR}.new" in remotes
    assert f"{s.WAKE_FRIDA_INJECT}.new" in remotes
    assert s.WAKE_FRIDA_JS in remotes
    assert s.WAKE_FRIDA_SH in remotes
    assert not any("yam" in c for c in p.adb.commands)


def test_actions_reject_non_alice_on_max():
    s, p = max_panel()
    with pytest.raises(s.AdbError):
        p.wakeword_actions_set("алёша", "assistant")


def test_footprint_reset_stops_wakeword_but_keeps_model_data():
    s, p = max_panel()
    p.footprint = Mock(return_value={"items": [], "dir": s.WEB_DIR, "size_kb": 0,
                                     "any": False, "yam_installed": False})
    p._footprint_reset()
    cmd = "\n".join(p.adb.commands)
    assert "pidof wakeword" in cmd
    assert s.WAKE_FRIDA_PID in cmd
    assert "rm -rf '/data/wakeword'" not in cmd
    assert "rm -rf /data/wakeword'" not in cmd
