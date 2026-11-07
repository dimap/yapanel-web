"""Голосовая заявка помощника: «включи сберзвук» переключает провайдера и поток."""
import threading
import unittest
from types import SimpleNamespace

import server


class AssistantMusicTests(unittest.TestCase):
    def make(self):
        p = server.Panel.__new__(server.Panel)
        p._music_lock = threading.Lock()
        p._music_wish_at = 0
        p.calls = []
        p.zvuk = SimpleNamespace(auth=SimpleNamespace(token="x"),
                                 flow=lambda: p.calls.append(("flow",)))
        p.music_provider = lambda provider: p.calls.append(("provider", provider))
        p.music_cmd = lambda cmd: p.calls.append(("music_cmd", cmd))
        p.assistant_command = lambda cmd: p.calls.append(("seen", cmd))
        return p

    def test_zvuk_switches_and_starts_flow_once(self):
        p = self.make()
        state = {"music": {"provider": "zvuk", "play": True, "at": 11}}
        p._assistant_music(state)
        p._assistant_music(state)          # повторный опрос той же заявки — без второго потока
        self.assertEqual(p.calls, [("provider", "zvuk"), ("flow",), ("seen", {"music_seen": True})])

    def test_yandex_switches_and_asks_wave(self):
        p = self.make()
        p._assistant_music({"music": {"provider": "yandex", "play": True, "at": 12}})
        self.assertEqual(p.calls, [
            ("provider", "yandex"),
            ("music_cmd", {"command": "sendText", "text": "включи мою волну"}),
            ("seen", {"music_seen": True}),
        ])

    def test_zvuk_without_token_only_switches(self):
        p = self.make()
        p.zvuk.auth.token = ""
        p._assistant_music({"music": {"provider": "zvuk", "play": True, "at": 13}})
        self.assertEqual(p.calls, [("provider", "zvuk"), ("seen", {"music_seen": True})])

    def test_ignores_unknown_and_empty(self):
        p = self.make()
        p._assistant_music({"music": {"provider": "radio", "at": 14}})
        p._assistant_music({})
        self.assertEqual(p.calls, [])


if __name__ == "__main__":
    unittest.main()
