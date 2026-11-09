import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assistant_max


class TestAssistantMaxUtils(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(assistant_max.normalize("Алиса, СПРОСИ GPT, что такое ёж?"), "алиса спроси gpt что такое еж")
        self.assertEqual(assistant_max.normalize(""), "")

    def test_phrase_from_real_max_log_lines(self):
        max_line = ("11-08 12:00:00.000  4406  4814 I quark-native: "
                    "[sdk] [info] [alice_state_machine.cpp:423] "
                    "onAliceStateChanged: BUSY, recognizedPhrase='Сколько времени?'")
        self.assertEqual(assistant_max.phrase_from_log_line(max_line), "Сколько времени?")
        request_line = ("11-08 12:00:00.000  4406  4814 I quark-native: "
                        "[sdk] [info] onRequestStateChanged: LISTENING, recognizedPhrase='спроси gpt про погоду'")
        self.assertEqual(assistant_max.phrase_from_log_line(request_line), "спроси gpt про погоду")
        self.assertEqual(assistant_max.phrase_from_log_line("onAliceStateChanged: LISTENING"), "")
        self.assertEqual(assistant_max.phrase_from_log_line("onAliceStateChanged: IDLE"), "")

    def test_match_trigger_simple(self):
        triggers = ["спроси gpt", "спроси модель"]
        q, matched = assistant_max.match_trigger("Алиса, спроси gpt, какая погода?", triggers)
        self.assertFalse(matched)  # "Алиса, спроси gpt" starts with "алиса"

        q, matched = assistant_max.match_trigger("спроси gpt какая погода?", triggers)
        self.assertTrue(matched)
        self.assertEqual(q, "какая погода?")

    def test_match_trigger_punctuation(self):
        triggers = ["спроси gpt", "нейросеть"]
        q, matched = assistant_max.match_trigger("Спроси GPT: сколько будет 2 + 2?", triggers)
        self.assertTrue(matched)
        self.assertEqual(q, "сколько будет 2 + 2?")

    def test_for_speech_clean(self):
        s = assistant_max.for_speech("**Привет**! Это `тест` «нейросети» #1.")
        self.assertNotIn("*", s)
        self.assertNotIn("`", s)
        self.assertNotIn("«", s)
        self.assertNotIn("»", s)
        self.assertNotIn("#", s)

    def test_for_speech_limit(self):
        long_text = "Слово. " * 200
        s = assistant_max.for_speech(long_text, limit=100)
        self.assertLessEqual(len(s), 105)
        self.assertTrue(s.endswith(".") or s.endswith("…"))

    def test_voice_error_messages(self):
        self.assertIn("Ключ", assistant_max.voice_error("API error 401 unauthorized"))
        self.assertIn("лимит", assistant_max.voice_error("API rate limit 429"))
        self.assertIn("вовремя", assistant_max.voice_error("Timed out waiting for response"))
        self.assertIn("интернету", assistant_max.voice_error("Failed to establish connection"))


class TestAssistantInstanceLock(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "fcntl/flock доступны только на POSIX")
    def test_second_instance_is_rejected_and_stale_lock_can_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            lockfile = Path(directory) / "assistant.sd.lock"
            first, first_fd = assistant_max._acquire_assistant_lock(lockfile)
            self.assertTrue(first)
            self.assertIsNotNone(first_fd)
            try:
                second, second_fd = assistant_max._acquire_assistant_lock(lockfile)
                self.assertFalse(second)
                self.assertIsNone(second_fd)
            finally:
                os.close(first_fd)
            # Файл lock остаётся, но flock освобождён после закрытия fd.
            third, third_fd = assistant_max._acquire_assistant_lock(lockfile)
            self.assertTrue(third)
            os.close(third_fd)


class TestAssistantMaxDaemon(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.daemon = assistant_max.AssistantDaemon(work_dir=str(self.tmp))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_load_and_defaults(self):
        cfg = self.daemon.load_config()
        self.assertTrue(cfg.get("enabled"))
        self.assertIn("спроси gpt", cfg.get("triggers"))
        self.assertEqual(cfg["llm"]["model"], "gpt-4o-mini")

    def test_status_file_written(self):
        self.daemon.status_path = self.tmp / "test_status.json"
        self.daemon.write_status()
        self.assertTrue(self.daemon.status_path.is_file())
        st = json.loads(self.daemon.status_path.read_text(encoding="utf-8"))
        self.assertEqual(st["version"], assistant_max.VERSION)
        self.assertEqual(st["state"], "idle")

    def test_stop_command(self):
        self.daemon.status_path = self.tmp / "test_status.json"
        self.daemon.state = "thinking"
        with patch.object(self.daemon.glagol, "stop", return_value=True):
            self.daemon.stop_current()
        self.assertEqual(self.daemon.state, "idle")

    def test_call_llm_mock(self):
        mock_resp = json.dumps({
            "choices": [{"message": {"content": "Ответ искусственного интеллекта."}}]
        }).encode("utf-8")

        mock_cm = Mock()
        mock_cm.read.return_value = mock_resp
        mock_cm.__enter__ = Mock(return_value=mock_cm)
        mock_cm.__exit__ = Mock(return_value=False)

        with patch("urllib.request.OpenerDirector.open", return_value=mock_cm):
            ans = self.daemon.call_llm("Привет")
            self.assertEqual(ans, "Ответ искусственного интеллекта.")


if __name__ == "__main__":
    unittest.main()
