"""Regression tests for Station Max privacy ownership and migrations."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import os
import subprocess
import tempfile

import pytest
import max_privacy as m


def rules(on=True):
    ota = "".join(f"127.0.0.1 {h}\n::1 {h}\n" for h in m.OTA)
    analytics = "".join(f"0.0.0.0 {h}\n::1 {h}\n" for h in m.ANALYTICS) if on else ""
    return "127.0.0.1 localhost\n::1 localhost\n" + ota + analytics


class FakeAdb:
    def __init__(self, mode="on", profiles=True, guard="running", owns=False, frozen=False):
        self.mode, self.profiles, self.guard, self.owns = mode, profiles, guard, owns
        self.frozen = frozen
        self.commands = []
        self.corrupt_ota = False
        self.guard_script = profiles

    def shell(self, command, check=False, timeout=15):
        self.commands.append(command)
        if "PRV_HOSTS_BEGIN" not in command:
            if command.startswith("touch ") or command.startswith("rm -f "):
                if not self.frozen:
                    self.mode = "off" if command.startswith("touch ") else "on"
            return ("", "", 0)
        mounts = {
            "on": ["/etc/hosts-ota", "/etc/hosts-full"],
            "off": ["/etc/hosts-ota"],
            "panel": ["/quasar/webpanel/hosts"],
            "foreign": ["/foreign/hosts"],
            "legacy": ["/etc/hosts-block"],
            "odm": ["/etc/hosts"],
            "stacked": ["/etc/hosts-ota", "/quasar/webpanel/hosts"],
            "missing": [],
        }[self.mode]
        content = rules(on=self.mode != "off")
        if self.corrupt_ota:
            content = content.replace("127.0.0.1 ota.yandex.ru\n::1 ota.yandex.ru\n", "")
        data = [
            "PRV_GUARD=" + self.guard,
            "PRV_SCRIPT=" + ("yes" if self.guard_script else "no"),
            "PRV_PROFILES=" + ("yes" if self.profiles else "no"),
            "PRV_FLAG=" + ("off" if self.mode == "off" else "on"),
        ]
        data += ["PRV_MOUNT:" + x for x in mounts]
        data += ["PRV_OWN=" + ("yes" if self.owns else "no"),
                 "PRV_HOSTS_BEGIN", content.rstrip("\n"), "PRV_HOSTS_END"]
        return ("\n".join(data) + "\n", "", 0)


class AdbError(Exception):
    pass


def panel(adb):
    class Panel:
        def __init__(self):
            self.adb = adb
            self.changes = []

        def privacy_status(self):
            return {"enabled": True, "hosts": ["clck.yandex.net"], "blocked": True, "queued": 18}

        def privacy_set(self, on):
            self.changes.append(on)
            return {"enabled": bool(on)}

        def privacy_purge(self):
            self.changes.append("purge")
            return {"enabled": True}

    srv = SimpleNamespace(AdbError=AdbError, HOSTS_CUSTOM="/data/quasar/webpanel/hosts",
                          BOOT_HOOK="/data/quasar/webpanel/boot_hook.sh",
                          ANALYTICS_HOSTS=("clck.yandex.net",))
    m.install(srv, Panel)
    return Panel()


def test_external_on_off_on_preserves_ota(monkeypatch):
    monkeypatch.delenv("YAPANEL_MAX_PRIVACY_BACKEND", raising=False)
    adb = FakeAdb()
    p = panel(adb)
    assert p.privacy_status()["applied"]
    assert p.privacy_set(False)["mode"] == "off"
    assert p.privacy_set(False)["mode"] == "off"
    assert p.privacy_set(True)["mode"] == "on"
    assert p.privacy_status()["ota_protected"]
    assert not any("umount " in x or "mount --bind" in x for x in adb.commands)
    assert len([x for x in adb.commands if x.startswith(("touch ", "rm -f "))]) == 2


def test_no_external_guard_still_uses_legacy_panel(monkeypatch):
    monkeypatch.delenv("YAPANEL_MAX_PRIVACY_BACKEND", raising=False)
    adb = FakeAdb("missing", profiles=False, guard="")
    p = panel(adb)
    assert p.privacy_status()["backend"] == "panel"
    p.privacy_set(True)
    assert p.changes == [True]


def test_confirmed_single_panel_mount_still_works():
    adb = FakeAdb("panel", profiles=False, guard="", owns=True)
    p = panel(adb)
    assert p.privacy_status()["backend"] == "panel"
    p.privacy_set(False)
    assert p.changes == [False]


@pytest.mark.parametrize("mode", ["foreign", "legacy", "stacked"])
def test_foreign_or_stacked_mount_never_touched(mode):
    adb = FakeAdb(mode, profiles=False, guard="")
    p = panel(adb)
    assert not p.privacy_status()["managed"]
    with pytest.raises(AdbError):
        p.privacy_set(False)
    assert not p.changes


def test_legacy_odm_hosts_mount_is_conflict_and_never_touched(monkeypatch):
    """Станция Макс с root-китом: /system/etc/hosts — bind /odm/etc/hosts.

    Панель должна увидеть чужой mount, не снимать его и не трогать тумблер.
    """
    monkeypatch.delenv("YAPANEL_MAX_PRIVACY_BACKEND", raising=False)
    adb = FakeAdb("odm", profiles=False, guard="")
    original_shell = adb.shell

    def shell(command, check=False, timeout=15):
        if "PRV_HOSTS_BEGIN" not in command:
            return original_shell(command, check=check, timeout=timeout)
        adb.commands.append(command)
        content = "127.0.0.1 localhost\n::1 localhost\n"
        # Реальный /odm-кит пишет аналитику на 127.0.0.1, без ::1.
        content += "".join(f"127.0.0.1\t{host}\n" for host in m.ANALYTICS)
        content += "".join(f"127.0.0.1\t{host}\n" for host in m.OTA)
        data = [
            "PRV_GUARD=", "PRV_SCRIPT=no", "PRV_PROFILES=no", "PRV_FLAG=on",
            "PRV_MOUNT:/etc/hosts", "PRV_OWN=no",
            "PRV_HOSTS_BEGIN", content.rstrip("\n"), "PRV_HOSTS_END",
        ]
        return ("\n".join(data) + "\n", "", 0)

    adb.shell = shell
    p = panel(adb)
    status = p.privacy_status()
    assert status["backend"] == "conflict"
    assert status["blocked"] is True
    assert status["managed"] is False
    assert "внешним hosts" in status["error"]
    with pytest.raises(AdbError):
        p.privacy_set(True)
    with pytest.raises(AdbError):
        p.privacy_purge()
    assert not p.changes
    assert not any(command.startswith(("mount ", "umount ")) for command in adb.commands)


def test_conflict_migrates_old_hook_only_when_requested(monkeypatch):
    adb = FakeAdb("odm", profiles=False, guard="")
    p = panel(adb)
    calls = []
    monkeypatch.setattr(m, "update_old_hook", lambda *args: calls.append(args))
    assert p.privacy_status(migrate=False)["backend"] == "conflict"
    assert not calls
    assert p.privacy_status()["backend"] == "conflict"
    assert calls


def test_legacy_guard_never_falls_back_to_panel():
    adb = FakeAdb("legacy", profiles=False, guard="running")
    p = panel(adb)
    assert p.privacy_status()["backend"] == "adbguard-v2"
    with pytest.raises(AdbError):
        p.privacy_set(False)


def test_stopped_guard_never_falls_back_to_panel():
    adb = FakeAdb(guard="stopped")
    p = panel(adb)
    assert not p.privacy_status()["applied"]
    with pytest.raises(AdbError):
        p.privacy_set(False)


def test_missing_ota_rules_prevents_switch():
    adb = FakeAdb()
    adb.corrupt_ota = True
    p = panel(adb)
    assert not p.privacy_status()["ota_protected"]
    with pytest.raises(AdbError):
        p.privacy_set(False)
    assert not any(x.startswith("touch ") for x in adb.commands)


def test_guard_fails_to_apply_then_rollback(monkeypatch):
    adb = FakeAdb(frozen=True)
    p = panel(adb)
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    with pytest.raises(AdbError):
        p.privacy_set(False)
    assert any(x.startswith("touch ") for x in adb.commands)
    assert any(x.startswith("rm -f ") for x in adb.commands)


def test_dry_run_status_does_not_migrate_hook(monkeypatch):
    adb = FakeAdb()
    p = panel(adb)
    monkeypatch.setattr(m, "update_old_hook", Mock(side_effect=AssertionError("unexpected write")))
    assert p.privacy_status(migrate=False)["applied"]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX sh/sed")
def test_legacy_hook_migration_preserves_unrelated_lines(tmp_path):
    hook = tmp_path / "boot_hook.sh"
    source = tmp_path / "hosts"
    original = f"#!/bin/sh\nbind {source} /system/etc/hosts\necho leave-me\n"
    hook.write_text(original, encoding="utf-8")

    class ShellAdb:
        def shell(self, command, check=False, timeout=15):
            p = subprocess.run(["sh", "-c", command], text=True, capture_output=True,
                               timeout=timeout)
            return p.stdout, p.stderr, p.returncode

    m.update_old_hook(ShellAdb(), str(hook), str(source))
    assert hook.read_text() == "#!/bin/sh\necho leave-me\n"
    assert Path(str(hook) + ".pre-adbguard-v2.bak").read_text() == original
    m.update_old_hook(ShellAdb(), str(hook), str(source))


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX sh/sed")
def test_bad_hook_is_not_overwritten(tmp_path):
    hook = tmp_path / "boot_hook.sh"
    source = tmp_path / "hosts"
    original = f"#!/bin/sh\nif then\nbind {source} /system/etc/hosts\n"
    hook.write_text(original)

    class ShellAdb:
        def shell(self, command, check=False, timeout=15):
            p = subprocess.run(["sh", "-c", command], text=True, capture_output=True,
                               timeout=timeout)
            return p.stdout, p.stderr, p.returncode

    with pytest.raises(RuntimeError):
        m.update_old_hook(ShellAdb(), str(hook), str(source))
    assert hook.read_text() == original


def test_explicit_backend_requires_profiles(monkeypatch):
    monkeypatch.setenv("YAPANEL_MAX_PRIVACY_BACKEND", "adbguard-v2")
    adb = FakeAdb("missing", profiles=False, guard="")
    assert not panel(adb).privacy_status()["managed"]


def test_distributions_include_privacy_backend():
    root = Path(__file__).resolve().parents[1]
    script = (root / ".github" / "build-windows-zip.sh").read_text(encoding="utf-8")
    assert "max_privacy.py" in script
    updater_source = (root / "server.py").read_text(encoding="utf-8")
    assert '"max_profile.py", "max_privacy.py"' in updater_source
    profile_source = (root / "max_profile.py").read_text(encoding="utf-8")
    assert "max_privacy.install(s, P)" in profile_source
    assert "[ ! -f /odm/bin/adbguard.sh ]" in profile_source
