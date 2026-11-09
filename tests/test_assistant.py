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


class AssistantProxy(unittest.TestCase):
    """Пароль SOCKS5 не уходит в API, но сохраняется и доходит до демона."""

    def panel(self, cfg):
        panel = server.Panel(Mock())
        panel.stored = cfg
        panel._assist_config = lambda: json.loads(json.dumps(panel.stored))
        panel._assist_write = lambda c: setattr(panel, "stored", c)
        panel._assist_local_md5 = lambda: None
        panel.adb.shell.return_value = ("@installed\n@running\n@status\n{}\n", "", 0)
        return panel

    def test_status_masks_proxy_password(self):
        panel = self.panel({"llm": {"proxy": "socks5h://user:secret@proxy:1080"}})
        status = panel.assistant_status()
        dumped = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("secret", dumped)
        self.assertIn("•••", status["config"]["llm"]["proxy"])
        self.assertIs(status["config"]["llm"]["proxy_set"], True)

    def test_masked_proxy_is_preserved_on_save(self):
        panel = self.panel({"llm": {"proxy": "socks5h://user:secret@proxy:1080"}})
        masked = panel.assistant_status()["config"]["llm"]["proxy"]
        panel.assistant_set({"llm": {"proxy": masked, "model": "other-model"}})
        self.assertEqual(panel.stored["llm"]["proxy"], "socks5h://user:secret@proxy:1080")

    def test_changed_host_with_masked_password_is_rejected(self):
        panel = self.panel({"llm": {"proxy": "socks5h://user:secret@proxy:1080"}})
        masked = panel.assistant_status()["config"]["llm"]["proxy"]
        changed = masked.replace("proxy:1080", "other:1080")
        with self.assertRaises(server.AdbError):
            panel.assistant_set({"llm": {"proxy": changed}})
        self.assertEqual(panel.stored["llm"]["proxy"], "socks5h://user:secret@proxy:1080")

    def test_new_socks_proxy_is_validated(self):
        panel = self.panel({})
        panel.assistant_set({"llm": {"proxy": "socks5h://user:p%40ss@proxy:1080"}})
        self.assertEqual(panel.stored["llm"]["proxy"], "socks5h://user:p%40ss@proxy:1080")
        with self.assertRaises(server.AdbError):
            panel.assistant_set({"llm": {"proxy": "socks5h://user@proxy:1080"}})

    def test_models_receive_real_proxy_not_mask(self):
        panel = self.panel({"llm": {"proxy": "socks5h://user:secret@proxy:1080"}})
        masked = panel.assistant_status()["config"]["llm"]["proxy"]
        pushed = {}

        def push(local, remote):
            pushed["text"] = Path(local).read_text(encoding="utf-8")

        panel.adb.push = push
        panel.adb.shell.return_value = ('{"models":["mock-model"]}\n', "", 0)
        result = panel.assistant_models({"base_url": "https://x/v1", "proxy": masked})
        self.assertEqual(result, {"models": ["mock-model"]})
        self.assertIn("secret", pushed["text"])
        self.assertNotIn("•••", pushed["text"])


class AssistantDaemonStop(unittest.TestCase):
    """Остановка демона: процессу уходит SIGTERM, затем pid-файл убирается."""

    def test_stop_terminates_process_and_cleans_pidfile(self):
        panel = server.Panel(Mock())
        panel.adb.shell.return_value = ("", "", 0)
        st = panel.assistant_daemon_stop()
        cmd = panel.adb.shell.call_args_list[0].args[0]
        self.assertIn(f"cat {server.ASSIST_SD}", cmd)
        self.assertIn('kill "$pid"', cmd)
        self.assertIn("kill -9", cmd)
        self.assertIn(f"rm -f {server.ASSIST_SD}", cmd)
        self.assertFalse(st["running"])


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
        self.patches = [patch.object(server, "ROOT", self.tmp),
                        patch.object(server, "DATA_DIR", self.tmp), patch.object(server, "ASSIST_SRC", self.tmp / "src"),
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
