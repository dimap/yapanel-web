"""Слова активации: определение пакета, статус и прокси к API демона."""
import json
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server


class Resp:
    def __init__(self, raw, ctype="application/json"):
        self.raw, self.headers = raw, {"Content-Type": ctype}

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def panel(platform="plum", shell=None):
    p = server.Panel(Mock())
    p.glagol = SimpleNamespace(platform=platform, host="192.0.2.10")
    p.adb.serial = "192.0.2.10:5555"
    p.adb.shell.return_value = shell or ("0.2.2\n@bin\n@script\n", "", 0)
    return p


class Installed(unittest.TestCase):
    def test_version_and_files(self):
        p = panel()
        info = p._wakeword_installed()
        self.assertTrue(info["installed"])
        self.assertEqual(info["version"], "0.2.2")
        self.assertTrue(info["bin"] and info["script"])

    def test_not_installed(self):
        p = panel(shell=("", "", 0))
        self.assertFalse(p._wakeword_installed()["installed"])

    def test_autostart_marker(self):
        p = panel(shell=("0.2.2\n@bin\n@script\n@auto\n", "", 0))
        self.assertTrue(p._wakeword_installed()["auto"])
        with patch.object(p, "_wakeword_api", return_value=("application/json", {"audio": {"ok": True}})):
            st = p.wakeword_status()
        self.assertTrue(st["autostart"])

    def test_supported_platforms(self):
        self.assertTrue(panel("plum")._wakeword_supported()[0])
        self.assertTrue(panel("yandexmidi")._wakeword_supported()[0])
        self.assertTrue(panel("yandexstation_2")._wakeword_supported()[0])   # Макс
        self.assertEqual(panel("yandexstation_2")._wakeword_supported()[1], "yandexstation_2")


class Api(unittest.TestCase):
    def test_forward_base(self):
        p = panel()
        p.adb.forward.return_value = 4567
        self.assertEqual(p._wakeword_base(), "http://127.0.0.1:4567")
        p.adb.forward.assert_called_once_with(server.WAKE_PORT)

    def test_missing_adb_is_reconnected_for_loopback_api(self):
        p = panel()
        p.adb.adb = "test-adb"
        p.adb.forward.side_effect = [server.AdbError("device not found"), 4567]
        with patch("server.subprocess.run") as run:
            self.assertEqual(p._wakeword_base(), "http://127.0.0.1:4567")
            self.assertEqual(p._wakeword_base(), "http://127.0.0.1:4567")
        run.assert_called_once_with(["test-adb", "connect", "192.0.2.10:5555"], capture_output=True, timeout=10)
        self.assertEqual(p.adb.forward.call_count, 2)

    def test_failed_reconnect_is_throttled_without_direct_ip_fallback(self):
        p = panel()
        p.adb.forward.side_effect = server.AdbError("device not found")
        with patch("server.subprocess.run") as run, patch("server.time.monotonic", side_effect=[100, 101, 131]):
            for _ in range(3):
                with self.assertRaisesRegex(server.AdbError, "adb-проброс"):
                    p._wakeword_base()
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(p._ww_port)

    def test_usb_failure_never_tries_network_connect(self):
        p = panel()
        p.adb.serial = "test-usb-device"
        p.adb.forward.side_effect = server.AdbError("device not found")
        with patch("server.subprocess.run") as run:
            with self.assertRaisesRegex(server.AdbError, "adb-проброс"):
                p._wakeword_base()
        run.assert_not_called()

    def test_json_and_wav(self):
        p = panel()
        with patch("server.Panel._wakeword_base", return_value="http://127.0.0.1:1"):
            with patch("urllib.request.urlopen", return_value=Resp(b'{"audio":{"ok":true}}')):
                ctype, data = p._wakeword_api("/api/state")
            self.assertTrue(ctype.startswith("application/json"))
            self.assertTrue(data["audio"]["ok"])
            with patch("urllib.request.urlopen", return_value=Resp(b"RIFF...", "audio/wav")):
                ctype, data = p._wakeword_api("/api/hits/1")
            self.assertEqual(ctype, "audio/wav")
            self.assertEqual(data, b"RIFF...")

    def test_bad_path(self):
        p = panel()
        for path in ("/admin", "/api/../x", ""):
            with self.assertRaises(server.AdbError):
                p._wakeword_api(path)

    def test_api_error(self):
        p = panel()
        err = urllib.error.HTTPError("http://x", 400, "bad", {}, None)
        err.read = lambda: b'{"error":"no audio"}'
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(server.AdbError, "no audio"):
                p._wakeword_api("/api/state")


class Status(unittest.TestCase):
    def test_status_reads_state(self):
        p = panel()
        state = {"audio": {"ok": True, "channel": "vqe_0", "level": 0.2},
                 "words": [{"id": "jarvis", "name": "Джарвис", "enabled": True, "takes": 8}],
                 "hits": [], "enroll": None, "train": {"active": False}, "station": "Лайт 2"}
        with patch.object(p, "_wakeword_api", return_value=("application/json", state)):
            st = p.wakeword_status()
        self.assertTrue(st["installed"] and st["running"] and st["supported"])
        self.assertTrue(st["audio"]["ok"])
        self.assertEqual(st["words"][0]["name"], "Джарвис")
        self.assertEqual(st["version"], "0.2.2")

    def test_status_reports_api_error(self):
        p = panel(shell=("0.2.2\n@bin\n@script\n@pid\n", "", 0))
        with patch.object(p, "_wakeword_api", side_effect=server.AdbError("timeout")):
            st = p.wakeword_status()
        self.assertTrue(st["installed"])
        self.assertTrue(st["running"])
        self.assertIn("timeout", st["error"])

    def test_stopped_daemon_is_not_an_api_error(self):
        p = panel()
        with patch.object(p, "_wakeword_api", side_effect=server.AdbError("closed")):
            st = p.wakeword_status()
        self.assertFalse(st["running"])
        self.assertEqual(st["error"], "")


class Autostart(unittest.TestCase):
    def test_on_creates_marker_and_hook(self):
        p = panel()
        p._ensure_hook = Mock()
        p.wakeword_status = Mock(return_value={"autostart": True})
        self.assertEqual(p.wakeword_autostart(True), {"autostart": True})
        p.adb.shell.assert_any_call(f"mkdir -p {server.WEB_DIR} && touch {server.WAKE_AUTOSTART}", check=False)
        p._ensure_hook.assert_called_once()

    def test_off_removes_marker_and_drops_hook(self):
        p = panel()
        p._drop_hook_if_unused = Mock()
        p.wakeword_status = Mock(return_value={"autostart": False})
        self.assertEqual(p.wakeword_autostart(False), {"autostart": False})
        p.adb.shell.assert_any_call(f"rm -f {server.WAKE_AUTOSTART}", check=False)
        p._drop_hook_if_unused.assert_called_once()

    def test_requires_package(self):
        p = panel(shell=("", "", 0))
        with self.assertRaisesRegex(server.AdbError, "не установлен"):
            p.wakeword_autostart(True)

    def test_boot_hook_starts_daemon(self):
        self.assertIn(f"-listen 127.0.0.1:{server.WAKE_PORT}", server.BOOT_HOOK_SCRIPT)
        self.assertIn(server.WAKE_BIN, server.BOOT_HOOK_SCRIPT)
        self.assertIn(server.WAKE_AUTOSTART, server.BOOT_HOOK_SCRIPT)
        self.assertIn(("слова активации", server.WAKE_AUTOSTART), server.Panel.HOOK_USERS)


class StartStop(unittest.TestCase):
    def test_start_runs_script_and_waits(self):
        p = panel()
        p.wakeword_status = Mock(side_effect=[{"running": False}, {"running": True}])
        with patch("server.time.sleep", return_value=None):
            st = p.wakeword_start()
        self.assertTrue(st["running"])
        call = p.adb.shell.call_args[0][0]
        self.assertIn(server.WAKE_BIN, call)
        self.assertIn(f"-listen 127.0.0.1:{server.WAKE_PORT}", call)
        self.assertIn("start-stop-daemon -S", call)

    def test_start_requires_package(self):
        p = panel(shell=("", "", 0))
        with self.assertRaisesRegex(server.AdbError, "не установлен"):
            p.wakeword_start()

    def test_stop_runs_script(self):
        p = panel()
        p.wakeword_status = Mock(return_value={"running": False})
        p.wakeword_stop()
        self.assertIn(f"{server.WAKE_SHARE}/S97wakeword stop", p.adb.shell.call_args[0][0])


class Proxy(unittest.TestCase):
    def test_get_and_method_validation(self):
        p = panel()
        with patch.object(p, "_wakeword_api", return_value=("application/json", {"ok": True})):
            data, ctype = p.wakeword_proxy("get", "/api/state")
        self.assertEqual(data, {"ok": True})
        with self.assertRaises(server.AdbError):
            p.wakeword_proxy("PUT", "/api/state")


if __name__ == "__main__":
    unittest.main()


class Routing(unittest.TestCase):
    def setUp(self):
        self.p = panel()
        self.p.scenes = Mock(return_value={"scenes": [{"id": "s1", "name": "Свет"}]})
        self.p._wakeword_apply_dry_run = Mock()
        self.saved = {}
        for target, effect in (("server.load_settings", lambda: dict(self.saved)),
                               ("server.save_settings", lambda d: (self.saved.clear(), self.saved.update(d)))):
            p = patch(target, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def test_alice_is_default_and_removed(self):
        self.p.wakeword_actions_set("дипсик", "alice")
        self.assertNotIn("дипсик", self.saved.get("wakeword_actions", {}))

    def test_none_and_scene(self):
        self.p.wakeword_actions_set("дипсик", "none")
        self.assertEqual(self.saved["wakeword_actions"]["дипсик"], {"type": "none", "scene": ""})
        self.p.wakeword_actions_set("дипсик", "scene", "s1")
        self.assertEqual(self.saved["wakeword_actions"]["дипсик"], {"type": "scene", "scene": "s1"})
        self.p.wakeword_actions_set("дипсик", "assistant")
        self.assertEqual(self.saved["wakeword_actions"]["дипсик"], {"type": "assistant", "scene": ""})

    def test_scene_validation(self):
        with self.assertRaisesRegex(server.AdbError, "нет такого сценария"):
            self.p.wakeword_actions_set("x", "scene", "nope")

    def test_fire_actions(self):
        self.p._wakeword_api = Mock()
        self.p.scene_run = Mock()
        self.p.assistant_command = Mock()
        self.p._wakeword_fire({"word": "x", "name": "Икс"}, {"x": {"type": "alice"}})
        self.p._wakeword_api.assert_called_once_with("/api/listen", "POST", timeout=3)
        self.p._wakeword_api.reset_mock()
        self.p._wakeword_fire({"word": "x"}, {"x": {"type": "none"}})
        self.p._wakeword_api.assert_not_called()
        self.p._wakeword_fire({"word": "x"}, {"x": {"type": "scene", "scene": "s1"}})
        self.p.scene_run.assert_called_once_with("s1")
        self.p._wakeword_api.assert_not_called()
        self.p._wakeword_fire({"word": "x"}, {"x": {"type": "assistant"}})
        self.p.assistant_command.assert_called_once_with({"route": True})
        self.p._wakeword_api.assert_called_once_with("/api/listen", "POST", timeout=3)

    def test_apply_dry_run_writes_and_restarts(self):
        p = panel()
        p._wakeword_settings = Mock(return_value={"dry_run": False})
        p._wakeword_settings_write = Mock()
        p._wakeword_restart = Mock()
        with patch("server.load_settings", return_value={"wakeword_actions": {"x": {"type": "none"}}}):
            self.assertTrue(p._wakeword_apply_dry_run())
        p._wakeword_settings_write.assert_called_once_with({"dry_run": True})
        p._wakeword_restart.assert_called_once()

    def test_status_dry_run(self):
        self.saved["wakeword_actions"] = {"x": {"type": "none"}}
        with patch.object(self.p, "_wakeword_installed",
                          return_value={"installed": True, "version": "0.2.2", "pid": True, "auto": False}), \
             patch.object(self.p, "_wakeword_api", return_value=("application/json", {"audio": {"ok": True}})):
            st = self.p.wakeword_status()
        self.assertTrue(st["dry_run"])


class RouterRestart(unittest.TestCase):
    def run_hits(self, last, snapshots):
        p = panel()
        p._ww_last_hit = last
        p._ww_router_stop = Mock()
        p._ww_router_stop.is_set.side_effect = [False] * len(snapshots) + [True]
        p.wakeword_status = Mock(return_value={"running": True})
        p._wakeword_api = Mock(side_effect=[
            ("application/json", {"hits": [{"id": i, "word": "x"} for i in ids]})
            for ids in snapshots])
        p._wakeword_fire = Mock()
        with patch("server.load_settings", return_value={"wakeword_actions": {"x": {"type": "none"}}}):
            p._wakeword_router_loop()
        return [call.args[0]["id"] for call in p._wakeword_fire.call_args_list]

    def test_restart_delivers_new_hits_once_in_order(self):
        self.assertEqual(self.run_hits(42, [[2, 1], [1, 2], [2, 3]]), [1, 2, 3])

    def test_restart_with_empty_history_then_first_hit(self):
        self.assertEqual(self.run_hits(42, [[], [1], [1]]), [1])

    def test_panel_start_skips_history_but_delivers_next_hit(self):
        self.assertEqual(self.run_hits(None, [[1, 2], [2, 3], [3]]), [3])

    def test_running_daemon_does_not_repeat_hits(self):
        self.assertEqual(self.run_hits(2, [[1, 2], [3, 2], [3]]), [3])


class PackageNavigation(unittest.TestCase):
    def test_catalog_install_invalidates_cached_missing_package(self):
        for name in ("tangenx/wakeword", "tangenx/wakeword@1.2.3"):
            with self.subTest(name=name):
                p = panel()
                p._ww_info = {"installed": False}
                p._yam2_services_raw = Mock(return_value=[])
                p._yam2 = Mock(return_value=("installed", 0))
                p.yam2_install_pkg(name)
                self.assertIsNone(p._ww_info)
                self.assertTrue(p._wakeword_installed()["installed"])

    def test_failed_install_keeps_cached_state(self):
        p = panel()
        cached = {"installed": False}
        p._ww_info = cached
        p._yam2_services_raw = Mock(return_value=[])
        p._yam2 = Mock(return_value=("failed", 1))
        with self.assertRaises(server.AdbError):
            p.yam2_install_pkg("tangenx/wakeword")
        self.assertIs(p._ww_info, cached)
