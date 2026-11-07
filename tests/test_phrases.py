"""Свои фразы: валидация, хранение и маршруты панели.

Станция и adb не нужны: настройки и say подменяются, проверяется только
логика панели и регистрация HTTP-маршрутов.
"""
import unittest
from unittest.mock import Mock, patch

import api
import server


class PhrasesStore(unittest.TestCase):
    def setUp(self):
        self.panel = server.Panel(Mock())

    @staticmethod
    def saved():
        """Патч записи настроек: словарь сохранённых секций и сам патч."""
        store = {}

        def write(key, value):
            store[key] = value
            return True

        return store, patch.object(server, "update_settings_section", side_effect=write)

    def test_save_assigns_ids_and_keeps_order(self):
        store, write = self.saved()
        with write:
            result = self.panel.phrases_save({"phrases": [
                {"name": "Утро", "text": "Доброе утро!"},
                {"name": "Готово", "text": "Ужин готов, все к столу"},
            ]})
        self.assertTrue(result["ok"])
        self.assertEqual([p["name"] for p in result["phrases"]], ["Утро", "Готово"])
        self.assertTrue(all(p["id"] for p in result["phrases"]))
        self.assertEqual(store["phrases"], result["phrases"])

    def test_save_reuses_given_id_and_sanitizes(self):
        store, write = self.saved()
        with write:
            result = self.panel.phrases_save({"phrases": [
                {"id": "Morning-1", "name": 'a"|b\nc', "text": "т\x00екст"},
            ]})
        phrase = result["phrases"][0]
        self.assertEqual(phrase["id"], "morning1")
        self.assertEqual(phrase["name"], "a b c")
        self.assertEqual(phrase["text"], "т екст")

    def test_save_rejects_bad_input(self):
        bad = [
            {},                                                  # нет списка
            {"phrases": "нет"},                                  # не список
            {"phrases": [{"name": "x", "text": ""}]},            # пустой текст
            {"phrases": [{"name": "x", "text": "y" * 301}]},     # длинный текст
            {"phrases": [{"text": "y"}] * 21},                   # больше лимита
            {"phrases": [{"id": "a", "text": "1"}, {"id": "a", "text": "2"}]},  # дубль id
        ]
        with patch.object(server, "update_settings_section", return_value=True):
            for data in bad:
                with self.subTest(data=str(data)[:60]):
                    with self.assertRaises(server.AdbError):
                        self.panel.phrases_save(data)

    def test_save_failure_is_reported(self):
        with patch.object(server, "update_settings_section", return_value=False):
            with self.assertRaises(server.AdbError):
                self.panel.phrases_save({"phrases": [{"text": "привет"}]})

    def test_list_skips_broken_items(self):
        with patch.object(server, "load_settings", return_value={
            "phrases": [{"id": "a", "name": "A", "text": "1"}, {"name": "без id"}, "мусор"],
            "greeting": {"on": False},
        }):
            result = self.panel.phrases_list()
        self.assertEqual(result["phrases"], [{"id": "a", "name": "A", "text": "1"}])
        self.assertEqual(result["limit"], server.Panel.PHRASES_LIMIT)
        self.assertFalse(result["greeting"]["on"])

    def test_say_existing_phrase_and_unknown(self):
        self.panel.say = Mock(return_value={"ok": True, "method": "glagol"})
        with patch.object(server, "load_settings", return_value={
            "phrases": [{"id": "a", "name": "Утро", "text": "Доброе утро"}], "greeting": {},
        }):
            result = self.panel.phrases_say("a")
            self.assertEqual(result, {"ok": True, "method": "glagol", "phrase": "Утро"})
            self.panel.say.assert_called_once_with("Доброе утро")
            with self.assertRaises(server.AdbError):
                self.panel.phrases_say("нет-такой")


class Greeting(unittest.TestCase):
    def setUp(self):
        self.panel = server.Panel(Mock())

    def test_defaults_are_off(self):
        with patch.object(server, "load_settings", return_value={}):
            greeting = self.panel.greeting_get()
        self.assertEqual(greeting, {"on": False, "text": "", "delay": 45})

    def test_save_clamps_delay_and_requires_text(self):
        store, write = self.saved()
        with write:
            result = self.panel.greeting_save({"on": True, "text": "Привет!", "delay": 5000})
        self.assertEqual(result["greeting"], {"on": True, "text": "Привет!", "delay": 600})
        self.assertEqual(store["greeting"], result["greeting"])
        with self.assertRaises(server.AdbError):
            self.panel.greeting_save({"on": True, "text": "  "})

    def saved(self):
        store = {}
        return store, patch.object(
            server, "update_settings_section",
            side_effect=lambda k, v: store.setdefault(k, v) is v or True)

    def test_greeting_say_uses_text(self):
        self.panel.say = Mock(return_value={"ok": True, "method": "glagol"})
        with patch.object(server, "load_settings", return_value={"greeting": {"on": True, "text": "Привет"}}):
            self.panel.greeting_say()
        self.panel.say.assert_called_once_with("Привет")

    def test_greeting_say_without_text(self):
        with patch.object(server, "load_settings", return_value={}):
            with self.assertRaises(server.AdbError):
                self.panel.greeting_say()


class Routes(unittest.TestCase):
    def test_phrases_routes_registered(self):
        for method, path in (
            ("GET", "/api/phrases"),
            ("POST", "/api/phrases"),
            ("POST", "/api/phrases/say"),
            ("POST", "/api/phrases/greeting"),
            ("POST", "/api/phrases/greeting/say"),
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(api.ROUTER.match(method, path))


if __name__ == "__main__":
    unittest.main()
