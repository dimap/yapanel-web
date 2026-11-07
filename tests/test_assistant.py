import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class AssistantKeys(unittest.TestCase):
    """Ключи API хранятся на колонке по адресу провайдера и наружу уходят только замаскированными."""

    def panel(self, cfg):
        panel = server.Panel(Mock())
        panel.stored = cfg
        panel._assist_config = lambda: json.loads(json.dumps(panel.stored))
        panel._assist_write = lambda c: setattr(panel, "stored", c)
        panel._assist_local_md5 = lambda: None
        panel.adb.shell.return_value = ("@installed\n@running\n@status\n{}\n", "", 0)
        return panel

    def test_legacy_key_moves_under_its_provider(self):
        panel = self.panel({"llm": {"base_url": "https://api.example.com/v1/", "api_key": "sk-legacy-123456"}})
        st = panel.assistant_set({"enabled": True})
        self.assertNotIn("api_key", panel.stored["llm"])
        self.assertEqual(panel.stored["keys"], {"https://api.example.com/v1": "sk-legacy-123456"})
        self.assertEqual(st["config"]["llm"]["key"], "…3456")
        self.assertNotIn("sk-legacy-123456", json.dumps(st))

    def test_switching_provider_keeps_previous_key(self):
        panel = self.panel({"llm": {"base_url": "https://a.example/v1"}, "keys": {"https://a.example/v1": "key-a-000000001"}})
        panel.assistant_set({"llm": {"base_url": "https://B.example/v1/", "api_key": "key-b-000000002"}})
        self.assertEqual(panel.stored["keys"], {"https://a.example/v1": "key-a-000000001",
                                                "https://b.example/v1": "key-b-000000002"})
        # пустой ключ из формы не стирает сохранённый
        panel.assistant_set({"llm": {"base_url": "https://a.example/v1", "api_key": ""}})
        self.assertEqual(panel.stored["keys"]["https://a.example/v1"], "key-a-000000001")
        panel.assistant_set({"delete_key": "https://b.example/v1/"})
        self.assertEqual(list(panel.stored["keys"]), ["https://a.example/v1"])

    def test_key_needs_address_and_unknown_fields_are_dropped(self):
        panel = self.panel({})
        with self.assertRaises(server.AdbError):
            panel.assistant_set({"llm": {"api_key": "orphan-key-000001"}})
        panel.assistant_set({"llm": {"base_url": "https://x/v1", "evil": 1}, "tts": {"provider": "alice"}})
        self.assertNotIn("evil", panel.stored["llm"])
        with self.assertRaises(server.AdbError):
            panel.assistant_set({"tts": {"provider": "rm -rf"}})

    def test_mode_phrases_and_timeout_are_sanitized(self):
        panel = self.panel({})
        panel.assistant_set({"mode": {"enter": ["  Переключись на GPT ", "", "x" * 100], "exit": ["верни алису"],
                                      "timeout_min": 100000, "follow_up": False, "evil": 1}})
        mode = panel.stored["mode"]
        self.assertEqual(mode["enter"], ["Переключись на GPT", "x" * 60])
        self.assertEqual(mode["exit"], ["верни алису"])
        self.assertEqual(mode["timeout_min"], 240)
        self.assertIs(mode["follow_up"], False)
        self.assertNotIn("evil", mode)

    def test_question_length_checked_before_device(self):
        panel = self.panel({})
        for text in ("", "   ", "я" * 1001):
            with self.assertRaises(server.AdbError):
                panel.assistant_ask(text)
        panel.adb.push.assert_not_called()

    def test_provider_settings_are_saved_per_connection(self):
        panel = self.panel({})
        panel.assistant_set({"llm": {"base_url": "https://a.example/v1", "model": "model-a", "transport": "chat"}})
        panel.assistant_set({"llm": {"base_url": "https://b.example/v1", "model": "model-b",
                                     "transport": "realtime", "voice": "alloy"}})
        conns = panel.stored["connections"]
        self.assertEqual(conns["https://a.example/v1"],
                         {"base_url": "https://a.example/v1", "model": "model-a", "transport": "chat", "voice": "marin"})
        self.assertEqual(conns["https://b.example/v1"]["model"], "model-b")
        self.assertEqual(conns["https://b.example/v1"]["transport"], "realtime")
        self.assertEqual(conns["https://b.example/v1"]["voice"], "alloy")

    def test_deleting_active_provider_switches_to_another(self):
        panel = self.panel({})
        panel.assistant_set({"llm": {"base_url": "https://a.example/v1", "model": "model-a", "api_key": "key-a-000000001"}})
        panel.assistant_set({"llm": {"base_url": "https://b.example/v1", "model": "model-b", "api_key": "key-b-000000002"}})
        panel.assistant_set({"delete_connection": "https://b.example/v1/"})
        self.assertNotIn("https://b.example/v1", panel.stored["connections"])
        self.assertNotIn("https://b.example/v1", panel.stored["keys"])
        self.assertEqual(panel.stored["llm"]["base_url"], "https://a.example/v1")
        self.assertEqual(panel.stored["llm"]["model"], "model-a")

    def test_deleted_provider_does_not_come_back_on_status(self):
        panel = self.panel({"llm": {"base_url": "https://a.example/v1", "model": "model-a"}})
        panel.assistant_set({"delete_connection": "https://a.example/v1"})
        st = panel.assistant_status()
        self.assertNotIn("https://a.example/v1", st["config"]["connections"])
        self.assertEqual(st["config"]["llm"]["base_url"], "")

    def test_local_skills_and_weather_are_validated(self):
        panel = self.panel({})
        panel.assistant_set({"skills": {"time": True, "weather": True},
                             "weather": {"city": " Казань ", "lat": 55.79, "lon": 49.12}})
        self.assertEqual(panel.stored["skills"], {"time": True, "weather": True})
        self.assertEqual(panel.stored["weather"], {"city": "Казань", "lat": 55.79, "lon": 49.12})
        with self.assertRaises(server.AdbError):
            panel.assistant_set({"weather": {"lat": 200, "lon": 0}})
        with self.assertRaises(server.AdbError):
            panel.assistant_set({"weather": {"lat": "нет", "lon": 0}})

    def test_geo_uses_city_name_or_ip(self):
        panel = self.panel({})
        panel._http_json = lambda url, timeout=8: [{"lat": "55.79", "lon": "49.12", "display_name": "Казань"}]
        self.assertEqual(panel.assistant_geo({"city": "Казань"}),
                         {"city": "Казань", "lat": 55.79, "lon": 49.12})
        panel._http_json = lambda url, timeout=8: {"success": True, "city": "Самара", "latitude": 53.2, "longitude": 50.15}
        self.assertEqual(panel.assistant_geo({}), {"city": "Самара", "lat": 53.2, "lon": 50.15})
        panel._http_json = lambda url, timeout=8: {}
        with self.assertRaises(server.AdbError):
            panel.assistant_geo({})

    def test_local_timezone_is_copied_for_the_daemon(self):
        _, data = server.Panel._pc_tz()
        if not data:
            self.skipTest("на этом компьютере нет /etc/localtime")
        panel = self.panel({})
        panel.adb.push.reset_mock()
        panel._assist_timezone()
        targets = [c.args[1] for c in panel.adb.push.call_args_list if len(c.args) > 1]
        self.assertIn(f"{server.ASSIST_DIR}/localtime", targets)


class AssistantHook(unittest.TestCase):
    def test_boot_hook_starts_assistant_only_when_configured(self):
        line = next(l for l in server.BOOT_HOOK_SCRIPT.splitlines() if "assistant" in l)
        self.assertTrue(line.startswith(f"[ -f {server.ASSIST_CONF} ] && [ -x {server.ASSISTD} ]"))
        # пока помощник настроен, строку автозапуска из rootkeeper.sh не убираем
        panel = server.Panel(Mock())
        panel._drop_hook_if_unused()
        self.assertIn(f"[ -f {server.ASSIST_CONF} ] && echo", panel.adb.shell.call_args.args[0])   # ассистент держит хук


if __name__ == "__main__":
    unittest.main()


class AssistantDownload(unittest.TestCase):
    """Без Go панель берёт программу помощника с релиза своей версии и сверяет SHA256SUMS."""

    def setUp(self):
        import hashlib, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.blob = b"\x7fELF assistantd"
        self.sums = f"{hashlib.sha256(self.blob).hexdigest()}  assistantd-linux-arm64\n".encode()
        self.patches = [patch.object(server, "DATA_DIR", self.tmp), patch.object(server, "ASSIST_SRC", self.tmp / "src"),
                        patch.object(server.shutil, "which", lambda name: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def fetch(self, files):
        def urlopen(req, timeout=None):
            body = files[req.full_url.rsplit("/", 1)[1]]
            return Mock(read=lambda limit=None: body)
        return patch.object(server.urllib.request, "urlopen", urlopen)

    def test_downloads_and_caches(self):
        panel = server.Panel(Mock())
        with self.fetch({"SHA256SUMS": self.sums, "assistantd-linux-arm64": self.blob}):
            out = panel._assist_binary()
        self.assertEqual(out.read_bytes(), self.blob)
        self.assertEqual(out.name, f".assistantd-{server.VERSION}")
        with self.fetch({}):   # второй раз — из кэша, без сети
            self.assertEqual(panel._assist_binary(), out)

    def test_bad_checksum(self):
        panel = server.Panel(Mock())
        with self.fetch({"SHA256SUMS": self.sums, "assistantd-linux-arm64": b"other"}):
            with self.assertRaisesRegex(server.AdbError, "контрольная сумма"):
                panel._assist_binary()
        self.assertFalse(list(self.tmp.glob(".assistantd-*")))

    def test_status_does_not_download(self):
        panel = server.Panel(Mock())
        with self.fetch({}):
            with self.assertRaises(server.AdbError):
                panel._assist_binary(download=False)


class AssistantConfigWrite(unittest.TestCase):
    """Пустая или битая запись не должна подменять рабочий конфиг на колонке."""

    def panel(self):
        p = server.Panel(Mock())
        p.adb.shell.return_value = ("", "", 0)
        return p

    def test_blank_new_keeps_old_config(self):
        p = self.panel()
        with self.assertRaises(server.AdbError):
            p._assist_write({"enabled": False})
        cmds = [c.args[0] for c in p.adb.shell.call_args_list if c.args]
        self.assertTrue(any("rm -f" in c for c in cmds), cmds)
        self.assertFalse(any("mv " in c for c in cmds), cmds)

    def test_valid_new_is_moved(self):
        p = self.panel()
        p.adb.shell.return_value = ('{"enabled": false}', "", 0)
        p._assist_write({"enabled": False})
        cmds = [c.args[0] for c in p.adb.shell.call_args_list if c.args]
        self.assertTrue(any("chmod 600" in c and "mv " in c for c in cmds), cmds)


class AssistantVoiceSettings(AssistantKeys):
    def test_custom_stt_credentials_are_masked_and_preserved(self):
        panel = self.panel({})
        st = panel.assistant_set({'stt': {'provider': 'custom', 'base_url': 'http://stt/v1',
                                        'model': 'whisper', 'api_key': 'stt-secret-123456', 'language': 'ru'},
                                  'llm': {'transport': 'realtime', 'voice': 'marin'},
                                  'microphone': {'source': 'microphone', 'silence_ms': 950, 'gain': 32}})
        self.assertNotIn('stt-secret-123456', json.dumps(st))
        self.assertEqual(st['config']['stt']['key'], '…3456')
        self.assertIsInstance(panel.stored['microphone']['silence_ms'], int)
        panel.assistant_set({'stt': {'api_key': ''}})
        self.assertEqual(panel.stored['keys']['http://stt/v1'], 'stt-secret-123456')

    def test_rejects_invalid_audio_configuration(self):
        for update in ({'llm': {'transport': 'unsupported'}}, {'stt': {'provider': 'shell'}},
                       {'microphone': {'source': 'file'}}, {'microphone': {'gain': float('nan')}},
                       {'microphone': {'silence_ms': 0}}):
            with self.subTest(update=update), self.assertRaises(server.AdbError):
                self.panel({}).assistant_set(update)


class MicrophoneConfirmation(unittest.TestCase):
    def test_install_returns_status_after_confirmation(self):
        p = server.Panel(Mock())
        p._microphone_binary = Mock(return_value=Path('/tmp/test-mic.so'))
        p._ensure_hook = Mock()
        p.detach = Mock()
        p.assistant_status = Mock(side_effect=[{'running': True},
            {'microphone_installed': False, 'state': {'microphone': {'ready': True}}},
            {'microphone_installed': True, 'state': {'microphone': {'ready': True}}}])
        def shell(cmd, *args, **kwargs):
            if '/proc/device-tree/model' in cmd:
                return 'PDM-dummy fs2105', '', 0
            if 'cat /system/vendor/quasar/launch.json' in cmd:
                return json.dumps({'nodes': [{'tasks': [{'arg': ['--platformd']}]}]}), '', 0
            return '', '', 0
        p.adb.shell.side_effect = shell
        with patch.object(server.time, 'sleep'):
            state = p.assistant_microphone(True)
        self.assertTrue(state['microphone_installed'])
        self.assertEqual(p.assistant_status.call_count, 3)
        self.assertTrue(any('manage.sh confirm' in call.args[0] for call in p.adb.shell.call_args_list))
