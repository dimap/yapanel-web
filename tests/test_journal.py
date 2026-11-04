import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class Redact(unittest.TestCase):
    def test_hides_secrets_and_addresses(self):
        text = server.redact("sk-proj-abcdefghijklmnop y0_AgAAAAABCDEFGHIJKLMNOP token=abcdefghijklmnopqrstu "
                             "192.168.2.115:5555 aa:bb:cc:dd:ee:ff me@mail.ru /home/dimap/x C:\\Users\\Dima\\y")
        for secret in ("abcdefghijklmnop", "ABCDEFGHIJKLMNOP", "abcdefghijklmnopqrstu", "192.168", "aa:bb", "me@", "dimap", "Dima"):
            self.assertNotIn(secret, text)

    def test_keeps_versions(self):
        for keep in ("0.369.1.50.4794654278.20261021.133", "127.0.0.1:8765", "adb 1.0.41"):
            self.assertEqual(server.redact(keep), keep)


class JournalFile(unittest.TestCase):
    def test_repeats_collapse_and_survive_restart(self):
        path = Path(tempfile.mkdtemp()) / "j.jsonl"
        j = server.Journal(); j.open(path)
        for _ in range(3):
            j.add("связь: потеряна", "warn")
        j.add("POST /api/x — ок")
        self.assertIn("(×3)", j.text())
        again = server.Journal(); again.open(path)
        self.assertEqual([i["msg"] for i in again.items], ["связь: потеряна", "POST /api/x — ок"])

    def test_trims_to_limit(self):
        path = Path(tempfile.mkdtemp()) / "j.jsonl"
        j = server.Journal(); j.LIMIT = 50; j.open(path)
        for n in range(450):
            j.add(f"событие {n}")
        self.assertLessEqual(len(path.read_text().splitlines()), 50 + 200)
        self.assertEqual(j.items[-1]["msg"], "событие 449")


class RequestJournal(unittest.TestCase):
    def handler(self, method, path, data=None, headers=None):
        h = SimpleNamespace(command=method, path=path, _jdata=data or {}, headers=headers or {}, JOURNAL_QUIET=server.Handler.JOURNAL_QUIET,
                            JOURNAL_KEYS=server.Handler.JOURNAL_KEYS)
        return lambda **kw: server.Handler._journal(h, **kw)

    def setUp(self):
        server.JOURNAL.items.clear()

    def test_actions_without_secrets(self):
        self.handler("POST", "/api/assistant", {"api_key": "sk-secret-123456789", "name": "x"})()
        self.handler("POST", "/api/volume", {"level": 5})()          # ползунок — не пишем
        self.handler("GET", "/api/status")()                          # опрос — не пишем
        self.handler("GET", "/api/status")(error="Колонка не ответила")
        msgs = [i["msg"] for i in server.JOURNAL.items]
        self.assertEqual(msgs, ["POST /api/assistant (name=x) — ок", "GET /api/status: Колонка не ответила"])
        self.assertNotIn("sk-secret", " ".join(msgs))


if __name__ == "__main__":
    unittest.main()


class SpeakerFinder(unittest.TestCase):
    """Поиск колонки из панели: адрес запоминается, панель перезапускается с ним."""

    def test_restart_replaces_device_args(self):
        from unittest.mock import patch
        cases = {("server.py", "--open", "--device", "1.2.3.4", "--port", "1"): ["server.py", "--port", "1"],
                 ("server.py", "--find", "--port", "1"): ["server.py", "--port", "1"],
                 ("server.py", "--find", "10.0.0.0/24", "--device=x"): ["server.py"]}
        for argv, rest in cases.items():
            with patch.object(server.sys, "argv", list(argv)), patch.object(server.os, "execv") as ex, \
                    patch.object(server.Handler, "panel", None, create=True):
                server.restart_self(device="192.168.1.50:5555")
            self.assertEqual(ex.call_args.args[1][1:], rest + ["--device", "192.168.1.50:5555"])

    def test_use_accepts_only_home_ip(self):
        from unittest.mock import patch
        h = SimpleNamespace()
        for bad in ("8.8.8.8", "evil.com", "abc", "", None, "192.168.1.5; reboot"):
            with self.assertRaises(server.AdbError):
                server.Handler._conn_use(h, bad)
        with patch.object(server, "save_settings") as save, patch.object(server, "load_settings", return_value={}), \
                patch.object(server.threading, "Timer") as timer:
            self.assertEqual(server.Handler._conn_use(h, "192.168.1.50")["serial"], "192.168.1.50:5555")
        save.assert_called_once_with({"device": "192.168.1.50:5555"})
        self.assertEqual(timer.call_args.kwargs["kwargs"], {"device": "192.168.1.50:5555"})


class SayOnSpeaker(unittest.TestCase):
    """«Сказать на колонке»: текст уходит файлом, в команду оболочки не попадает."""

    def test_alice_voice(self):
        p = server.Panel(Mock())
        p.detach = Mock()
        p.adb.shell.return_value = ("1\n", "", 0)
        pushed = []
        p.adb.push = lambda local, remote, *a, **k: pushed.append((Path(local).read_text(encoding="utf-8"), remote))
        p.say("ужин; rm -rf / $(id)\nвторая строка")
        self.assertEqual(pushed, [("Повтори за мной: ужин; rm -rf / $(id) вторая строка\n", "/tmp/webpanel_say.txt")])
        script = p.detach.call_args.args[1]
        self.assertNotIn("rm -rf", script)
        self.assertIn("maind --cli", script)

    def test_assistant_voice_and_limits(self):
        p = server.Panel(Mock())
        p.assistant_command = Mock(return_value={"ok": True})
        p.say("привет", "assistant")
        p.assistant_command.assert_called_once_with({"say": "привет"})
        for bad in ("", "   ", "x" * 301):
            with self.assertRaises(server.AdbError):
                p.say(bad)


class Bluez(unittest.TestCase):
    """Bluetooth — одним запросом по D-Bus: bluetoothctl на каждый опрос раздувал bluetoothd прошивки."""

    SAMPLE = '''method return time=1 sender=:1.1 -> destination=:1.2 serial=3 reply_serial=2
   array [
      dict entry(
         object path "/org/bluez/hci0"
         array [
            dict entry(
               string "org.bluez.Adapter1"
               array [
                  dict entry(
                     string "Alias"
                     variant                         string "Колонка"
                  )
                  dict entry(
                     string "Powered"
                     variant                         boolean true
                  )
                  dict entry(
                     string "Discoverable"
                     variant                         boolean false
                  )
               ]
            )
         ]
      )
      dict entry(
         object path "/org/bluez/hci0/dev_00_11_22_33_44_55"
         array [
            dict entry(
               string "org.bluez.Device1"
               array [
                  dict entry(
                     string "Address"
                     variant                         string "00:11:22:33:44:55"
                  )
                  dict entry(
                     string "Alias"
                     variant                         string "Телефон "в кармане""
                  )
                  dict entry(
                     string "Connected"
                     variant                         boolean true
                  )
               ]
            )
            dict entry(
               string "org.bluez.Battery1"
               array [
                  dict entry(
                     string "Percentage"
                     variant                         byte 87
                  )
               ]
            )
         ]
      )
   ]
'''

    def test_parse(self):
        o = server.parse_bluez(self.SAMPLE)
        self.assertEqual(o["/org/bluez/hci0"]["org.bluez.Adapter1"], {"Alias": "Колонка", "Powered": True, "Discoverable": False})
        dev = o["/org/bluez/hci0/dev_00_11_22_33_44_55"]
        self.assertEqual(dev["org.bluez.Device1"]["Alias"], 'Телефон "в кармане"')
        self.assertTrue(dev["org.bluez.Device1"]["Connected"])
        self.assertEqual(dev["org.bluez.Battery1"]["Percentage"], 87)

    def test_status_and_widget_do_not_run_bluetoothctl(self):
        p = server.Panel(Mock())
        calls = []
        p.adb.shell = lambda cmd, *a, **k: (calls.append(cmd), (self.SAMPLE + "\n@@PAIR\n", "", 0))[1]
        st = p.bt_state()
        self.assertEqual(st["devices"][0]["battery"], 87)
        self.assertTrue(st["devices"][0]["connected"] and st["powered"])
        p.version = lambda: {}
        p.adb.shell = lambda cmd, *a, **k: (calls.append(cmd), ("hostname=x\n@@BLUEZ\n" + self.SAMPLE, "", 0))[1]
        s = p.status()
        self.assertEqual((s["powered"], s["discoverable"]), ("yes", "no"))
        self.assertEqual(s["bt_connected"], ['00:11:22:33:44:55 Телефон "в кармане"'])
        self.assertFalse(any("bluetoothctl" in c for c in calls))


class BtOut(unittest.TestCase):
    """Звук через Bluetooth-колонку: подмена конца цепочки ALSA + регулятор громкости с явной картой."""
    MAC = "00:11:22:AA:BB:CC"

    def panel(self, info):
        p = server.Panel(Mock())
        self.cmds, self.pushed = [], {}
        p.adb.shell = lambda cmd, *a, **k: (self.cmds.append(cmd), (info if "bluetoothctl info" in cmd
                                                                    else "yes" if "snd-aloop" in cmd and "insmod" not in cmd
                                                                    else "changed" if "mount --bind" in cmd else "", "", 0))[1]
        p.adb.push = lambda local, remote, **k: self.pushed.__setitem__(remote, Path(local).read_text(encoding="utf-8"))
        p._original_bytes = lambda path: b"pcm.!default {\n  type asym\n}\n"
        p._ensure_hook = lambda: None
        p.bt_state = lambda: {"ok": True}
        return p

    def test_use_builds_override_and_starts_watchdog(self):
        p = self.panel("\tConnected: yes\n\tUUID: Audio Sink (0000110b)\n")
        p.btout_use(self.MAC.lower())
        conf = self.pushed[server.BTOUT_CONF]
        # смешивание — как обычно (dmix), но в петлю snd-aloop; в Bluetooth-колонку переливает сторож
        self.assertIn('pcm "hw:Loopback,0,0"', conf)
        self.assertNotIn("bluealsa", conf)
        self.assertIn("card 0", conf)                     # без карты softvol плеер Алисы не открывает вывод
        self.assertIn(server.BTOUT_MARK, conf)
        self.assertTrue(conf.startswith("pcm.!default"))  # исходная настройка — целиком, подмена дописана в конец
        self.assertEqual(self.pushed[server.BTOUT_MAC].strip(), self.MAC)
        self.assertEqual(self.pushed[server.BTOUT_LAST].strip(), self.MAC)   # «вернуться на неё» одной кнопкой
        self.assertTrue(any(f"mount --bind {server.BTOUT_CONF} /etc/asound.conf" in c for c in self.cmds))   # сразу, не ждём сторожа
        self.assertTrue(any("q restart" in c for c in self.cmds))
        sh = self.pushed[server.BTOUT_SH]
        self.assertNotIn("__", sh)
        self.assertIn("dev_$(echo", sh)
        self.assertIn("aplay -q -D bluealsa:DEV=$M,PROFILE=a2dp", sh)
        self.assertIn("arecord -q -D hw:Loopback,1,0", sh)
        self.assertIn("snd-aloop.ko", sh)
        code = "\n".join(l for l in sh.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("bluetoothctl", code)   # сторож проверяет по D-Bus, без утечки
        self.assertTrue(any("start-stop-daemon -S" in c and server.BTOUT_SH in c for c in self.cmds))
        import subprocess, shutil
        sh_bin = shutil.which("sh") or (r"C:\Program Files\Git\bin\sh.exe" if os.path.exists(r"C:\Program Files\Git\bin\sh.exe") else None)
        if sh_bin:
            self.assertEqual(subprocess.run([sh_bin, "-n"], input=sh.encode()).returncode, 0)
        else:
            self.assertTrue(len(sh.strip()) > 0 and sh.count('"') % 2 == 0)
        p.adb.shell = lambda cmd, *a, **k: (self.cmds.append(cmd), ("changed" if "umount" in cmd else "", "", 0))[1]
        self.cmds.clear(); p.btout_off()
        self.assertTrue(any("umount /etc/asound.conf" in c for c in self.cmds) and any("q restart" in c for c in self.cmds))
        self.assertFalse(any(server.BTOUT_LAST in c for c in self.cmds))   # последнюю колонку помним
        # подмены не было (колонку ждали) — Алису зря не перезапускаем
        p.adb.shell = lambda cmd, *a, **k: (self.cmds.append(cmd), ("", "", 0))[1]
        self.cmds.clear(); self.assertFalse(p.btout_off()["restarted"])
        self.assertFalse(any("q restart" in c for c in self.cmds))

    def test_disconnect_in_panel_stops_bt_output(self):
        p = self.panel("")
        calls = []
        p.btout_off = lambda: calls.append("off")
        p.adb.shell = lambda cmd, *a, **k: ((self.MAC + "\n") if cmd.startswith("cat") else "", "", 0)
        p.bt_action({"action": "disconnect", "mac": self.MAC})
        self.assertEqual(calls, ["off"])
        p.bt_action({"action": "disconnect", "mac": "11:22:33:44:55:66"})
        self.assertEqual(calls, ["off"])

    def test_refuses_without_loop_module(self):
        p = self.panel("\tConnected: yes\n\tUUID: Audio Sink\n")
        p.adb.shell = lambda cmd, *a, **k: ("", "", 0)
        with self.assertRaisesRegex(server.AdbError, "snd-aloop"):
            p.btout_use(self.MAC)

    def test_refuses_without_connection_or_a2dp(self):
        with self.assertRaisesRegex(server.AdbError, "не подключилась"):
            self.panel("\tConnected: no\n").btout_use(self.MAC)
        with self.assertRaisesRegex(server.AdbError, "A2DP"):
            self.panel("\tConnected: yes\n").btout_use(self.MAC)
        with self.assertRaises(server.AdbError):
            self.panel("").btout_use("C2:43;reboot")

    def test_info(self):
        devs = [{"mac": self.MAC, "name": "BT Speaker", "connected": True}]
        self.assertEqual(server.Panel._btout_info("", devs), {"on": False})
        o = server.Panel._btout_info(f"\n{self.MAC}\nbt\nwatch\n", devs)
        self.assertEqual((o["on"], o["name"], o["state"], o["watch"]), (True, "BT Speaker", "bt", True))
        self.assertEqual(server.Panel._btout_info(f"{self.MAC}\n\n", devs)["state"], "waiting")
        # колонки нет в списке сопряжённых — имя запомненное или общее, но не MAC-адрес
        self.assertEqual(server.Panel._btout_info(f"{self.MAC}\n", [], "BT Speaker")["name"], "BT Speaker")
        self.assertEqual(server.Panel._btout_info(f"{self.MAC}\n", [])["name"], "Bluetooth-колонка")


class BtOutVolume(unittest.TestCase):
    def test_volume_is_fixed_at_max(self):
        p = server.Panel(Mock()); cmds = []
        p.adb.shell = lambda cmd, *a, **k: (cmds.append(cmd), (
            "00:11:22:AA:BB:CC\n" if cmd.startswith("cat") else "Simple mixer control 'Колонка A2DP',0\n", "", 0))[1]
        p._btout_volume_max()
        self.assertIn("sset 'Колонка A2DP' 100%", cmds[-1])
        self.assertIn("SoftVolume variant:boolean:true", cmds[-1])
        # колонки нет — ничего не делаем
        p = server.Panel(Mock()); p.adb.shell = lambda *a, **k: ("", "", 0)
        p._btout_volume_max()


class BtPower(unittest.TestCase):
    def test_off_refused_while_sound_goes_to_bt_speaker(self):
        p = server.Panel(Mock())
        p.adb.shell.side_effect = lambda cmd, **kw: ("on\n", "", 0) if server.BTOUT_MAC in cmd and "echo on" in cmd else ("", "", 0)
        with self.assertRaisesRegex(server.AdbError, "верните его в колонку"):
            p.bt_action({"action": "power_off"})
        self.assertFalse(any("Powered variant:boolean:false" in c.args[0] for c in p.adb.shell.call_args_list))

    def test_boot_keeps_it_off_and_reset_turns_it_on(self):
        self.assertIn(server.BT_OFF_BOOT, server.BOOT_HOOK_SCRIPT)
        self.assertIn("Bluetooth выключен", dict(server.Panel.HOOK_USERS))
        p = server.Panel(MagicMock())
        p.adb.shell.return_value = ("", "", 0)
        p.ring_reset = Mock()
        p.footprint = Mock(return_value={})
        p._agent_store = Mock(return_value={})
        p.footprint_reset()
        first = p.adb.shell.call_args_list[0].args[0]
        self.assertIn(f"[ -f {server.BT_OFF} ] && {server.bt_power_sh(True)}", first)

    def test_boot_snippet_switches_adapter_off(self):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("sh"):
            self.skipTest("нужен sh")
        with tempfile.TemporaryDirectory() as td:
            flag, log = Path(td) / "bt_off", Path(td) / "log"
            flag.touch()
            fake = Path(td) / "dbus-send"
            fake.write_text(f'#!/bin/sh\ncase "$*" in *Get*) echo "variant boolean true";; *Set*) echo "$*" >> {log};; esac\n')
            fake.chmod(0o755)
            snippet = server.BT_OFF_BOOT.replace(server.BT_OFF, str(flag)).replace("sleep 5", "exit")
            snippet = snippet.replace(" >/dev/null 2>&1 &", "")
            subprocess.run(["sh", "-c", snippet], env={"PATH": f"{td}:/usr/bin:/bin"}, timeout=10)
            self.assertIn("boolean:false", log.read_text())
