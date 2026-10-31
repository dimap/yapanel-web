"""Подключение хука к загрузке: команды выполняются настоящим sh во временной папке вместо колонки."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class LocalAdb:
    def __init__(self, root):
        self.root = root

    def shell(self, command, timeout=15, check=True):
        p = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        return p.stdout, p.stderr, p.returncode

    def push(self, local, remote, timeout=120):
        shutil.copy(local, remote)


def gnu_sed():
    """На колонке sed из busybox (как GNU); BSD sed в macOS понимает -i и «1a» иначе — там этот тест не о чём."""
    try:
        return subprocess.run(["sed", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


@unittest.skipIf(shutil.which("sh") is None or not gnu_sed(), "нужны sh и sed как на колонке (GNU/busybox)")
class RootkeeperHook(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        web, keeper, caller = self.root / "webpanel", self.root / "rootkeeper.sh", self.root / "onboot.sh"
        values = {"WEB_DIR": str(web), "BOOT_HOOK": str(web / "boot_hook.sh"), "ROOTKEEPER": str(keeper),
                  "ROOTKEEPER_CREATED": f"{keeper}.webpanel.created", "ROOTKEEPER_CALLER": str(caller),
                  "BOOT_CUSTOM": str(web / "boot.bin"), "SOUND_CUSTOM": str(web / "s"), "BOOT_ALWAYS": str(web / "a"),
                  "YAM_AUTOSTART": str(web / "y"), "ALARM_CONF": str(web / "al"), "HOSTS_CUSTOM": str(web / "h"),
                  "ASSIST_CONF": str(web / "as"), "INITD_DIR": str(self.root / "init.d"),
                  "INITD_HOOK": str(self.root / "init.d" / "S99zwebpanel"),
                  "BOOT_MOUNTS": str(self.root / "mounts")}
        values["ROOTKEEPER_DROP_CREATED"] = server.ROOTKEEPER_DROP_CREATED.replace(server.ROOTKEEPER, str(keeper))
        for name, value in values.items():
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.keeper, self.caller = keeper, caller
        (self.root / "mounts").write_text("root / rootfs rw 0 0\n")
        self.initd = self.root / "init.d"
        self.initd.mkdir()
        self.initd.chmod(0o555)          # как на большинстве колонок: корень только для чтения
        self.addCleanup(self.initd.chmod, 0o755)
        self.panel = server.Panel(LocalAdb(self.root))

    def test_missing_rootkeeper_is_created_when_boot_calls_it_and_removed_on_rollback(self):
        self.caller.write_text(f"[ -x {self.keeper} ] && {self.keeper} &\n")
        self.panel._ensure_hook()
        text = self.keeper.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn(server.HOOK_MARK, text)
        self.assertTrue(Path(server.ROOTKEEPER_CREATED).exists())
        self.panel._drop_hook_if_unused()
        self.assertFalse(self.keeper.exists())
        self.assertFalse(Path(server.ROOTKEEPER_CREATED).exists())

    def test_not_created_when_boot_does_not_call_it(self):
        self.caller.write_text("echo nothing\n")
        with self.assertRaises(server.AdbError):
            self.panel._ensure_hook()
        self.assertFalse(self.keeper.exists())

    def test_writable_initd_gets_own_script_when_no_rootkeeper(self):
        # колонка рутована иначе (alicebridge, dropbear): rootkeeper.sh нет, но /etc/init.d можно писать
        self.caller.write_text("echo nothing\n")
        self.initd.chmod(0o755)
        (self.initd / "S99zalicebridge-ssh").write_text("#!/bin/sh\n")
        self.panel._ensure_hook()
        self.panel._ensure_hook()                      # повторно — не дублирует
        script = self.initd / "S99zwebpanel"
        self.assertEqual(script.read_text(), server.initd_hook_script())
        self.assertFalse(self.keeper.exists())
        Path(server.BOOT_HOOK).write_text("#!/bin/sh\necho booted $1 > " + str(self.root / "ran") + "\n")
        Path(server.BOOT_HOOK).chmod(0o755)
        subprocess.run(["sh", str(script), "stop"], check=True)
        self.assertFalse((self.root / "ran").exists())      # rcK зовёт со stop — ничего не запускаем
        subprocess.run(["sh", str(script), "start"], check=True)
        self.assertTrue((self.root / "ran").exists())
        st = self.panel.hook_status()
        self.assertTrue(st["hook"] and st["possible"])
        self.assertEqual(st["file"], str(script))
        self.panel._drop_hook_if_unused()
        self.assertFalse(script.exists())
        self.assertTrue((self.initd / "S99zalicebridge-ssh").exists())

    def test_rootkeeper_wins_over_initd(self):
        self.caller.write_text(f"[ -x {self.keeper} ] && {self.keeper} &\n")
        self.initd.chmod(0o755)
        self.panel._ensure_hook()
        self.assertIn(server.HOOK_MARK, self.keeper.read_text())
        self.assertFalse((self.initd / "S99zwebpanel").exists())

    def test_readonly_initd_gets_script_by_remount_and_drops_cleanly(self):
        # Корень колонки в рантайме только для чтения: rootkeeper.sh нет, /etc/init.d не пишется, v0.2.0
        # тут честно отказывала. Хук кладём в init.d коротким перемонтированием rw→ro (mount подменён),
        # откат («выключить последнюю свою функцию», «Убрать всё») убирает файл тем же способом.
        self.caller.write_text("echo nothing\n")
        orphan = "#!/bin/sh\n" + server.HOOK_MARK + "\nkeep_owner_settings\n"
        self.keeper.write_text(orphan)
        self.assertFalse(self.panel.hook_status()['hook'])
        self.initd.chmod(0o755)
        (self.initd / "S99zdropbear").write_text("#!/bin/sh\n")   # чужой скрипт рута — не трогаем
        self.initd.chmod(0o555)
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "mount").write_text(
            "#!/bin/sh\n"
            f'case "$*" in *remount,rw*) chmod 755 {self.initd};; *remount,ro*) chmod 555 {self.initd};; esac\n'
            "exit 0\n")
        (bindir / "mount").chmod(0o755)
        path = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        with patch.dict("os.environ", path):
            with self.assertRaises(server.AdbError):
                self.panel._ensure_hook()                    # без перемонтирования — отказ, файл не появился
            self.assertFalse((self.initd / "S99zwebpanel").exists())
            self.panel.hook_repair(remount=True)             # с перемонтированием — кладём
        script = self.initd / "S99zwebpanel"
        self.assertEqual(script.read_text(), server.initd_hook_script())
        self.assertEqual(oct(script.stat().st_mode)[-3:], "755")
        self.assertEqual(oct(self.initd.stat().st_mode)[-3:], "555")   # корень вернулся в только-чтение
        st = self.panel.hook_status()
        self.assertTrue(st["hook"] and st["broken"] is False)
        self.assertEqual(st["file"], str(script))
        self.assertTrue(st["possible"])                      # установленный init.d-хук уже достаточен
        with patch.dict("os.environ", path):
            self.panel._drop_hook_if_unused()                # откат одной кнопкой — без adb и консоли
        self.assertFalse(script.exists())
        self.assertTrue((self.initd / "S99zdropbear").exists())
        self.assertEqual(self.keeper.read_text(), orphan.replace(server.HOOK_MARK + "\n", ""))
        self.assertEqual(oct(self.initd.stat().st_mode)[-3:], "555")

    def test_installed_initd_hook_needs_no_second_remount(self):
        # Issue #22: кнопка уже положила S99zwebpanel, корень снова ro. Аналитика и автозапуск yamd
        # вызывают _ensure_hook() без перемонтирования — раньше это падало с «нет rootkeeper.sh».
        self.caller.write_text("echo nothing\n")
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "mount").write_text(
            "#!/bin/sh\n"
            f'case "$*" in *remount,rw*) chmod 755 {self.initd};; *remount,ro*) chmod 555 {self.initd};; esac\n'
            "exit 0\n")
        (bindir / "mount").chmod(0o755)
        path = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        with patch.dict("os.environ", path):
            self.panel.hook_repair(remount=True)
            self.assertEqual(oct(self.initd.stat().st_mode)[-3:], "555")
            self.panel._ensure_hook()                        # без remount — хук уже на месте
            self.panel._ensure_hook()
        self.assertTrue((self.initd / "S99zwebpanel").exists())
        self.assertEqual(Path(server.BOOT_HOOK).read_text(), server.BOOT_HOOK_SCRIPT)

    def test_orphan_rootkeeper_uses_initd_without_changing_owner_file(self):
        self.caller.write_text("#!/bin/sh\n# " + str(self.keeper) + "\necho normal_boot\n")
        original = "#!/bin/sh\n" + server.HOOK_MARK + "\nowner_commands\n"
        self.keeper.write_text(original)
        self.initd.chmod(0o755)
        self.assertFalse(self.panel.hook_status()['hook'])
        self.panel._ensure_hook()
        self.assertEqual(self.keeper.read_text(), original)
        self.assertEqual((self.initd / 'S99zwebpanel').read_text(), server.initd_hook_script())
        self.assertEqual(self.panel.hook_status()['file'], server.INITD_HOOK)

    def test_existing_rootkeeper_keeps_its_own_lines(self):
        self.caller.write_text(f"[ -x {self.keeper} ] && {self.keeper} &\n")
        self.keeper.write_text("#!/bin/sh\nstart_root_things\n")
        self.panel._ensure_hook()
        self.panel._drop_hook_if_unused()
        self.assertEqual(self.keeper.read_text(), "#!/bin/sh\nstart_root_things\n")


if __name__ == "__main__":
    unittest.main()
