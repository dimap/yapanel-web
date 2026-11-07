"""Провайдер «Сбер Звук»: разбор API, команда audio_play и очередь/раздача."""
import base64
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import glagol
import server
import zvuk
import zvuk_player

TRACKS = [
    {"id": "1", "title": "Первый", "artists": "Артист", "duration": 180, "cover": "https://cdn/1.jpg",
     "album_id": "10", "album": "Альбом", "year": "2024"},
    {"id": "2", "title": "Второй", "artists": "Артист", "duration": 200, "cover": "", "album_id": "",
     "album": "", "year": ""},
]


class FakeAuth:
    def __init__(self):
        self.token = "x" * 32

    def save(self, token):
        self.token = token


class FakeGlagol:
    def __init__(self):
        self.calls = []

    def audio_play(self, url, title="", subtitle="", offset_ms=0, set_pause=False, timeout=6):
        self.calls.append({"url": url, "title": title, "subtitle": subtitle,
                           "offset": offset_ms, "pause": set_pause})
        return {"status": "SUCCESS"}

    def send(self, payload, timeout=6):
        self.calls.append({"send": payload})
        return {"status": "SUCCESS"}


class FakeApi:
    def __init__(self, token):
        self.token = token

    def personal_wave(self, count=10, mood=None, popular=None, genre=None, language=None, vocal=None):
        self.mood = mood
        self.popular = popular
        self.genre = genre
        self.language = language
        self.vocal = vocal
        return [dict(t) for t in TRACKS]

    def search(self, q, limit=10):
        return {"query": q, "tracks": [dict(TRACKS[0])], "albums": [], "artists": [], "playlists": []}

    def stream_url(self, track_id):
        return f"https://zvuk/stream/{track_id}"

    def download(self, url, dest):
        Path(dest).write_bytes(b"ID3" + b"x" * 5000)
        return 5003

    def release_tracks(self, album_id):
        return {"id": str(album_id), "title": "Альбом", "tracks": [dict(t) for t in TRACKS]}

    def artist(self, artist_id):
        return {"id": str(artist_id), "name": "Артист", "cover": "https://cdn/a.jpg",
                "popular": [dict(t) for t in TRACKS]}

    def like(self, track_id, on):
        self.liked = (str(track_id), on)
        return on

    def dislike(self, track_id, on=True):
        self.disliked = (str(track_id), on)
        return on

    def liked_tracks(self):
        return [dict(t) for t in TRACKS]


def make_player(cache):
    panel = SimpleNamespace(glagol=FakeGlagol(), adb=SimpleNamespace(serial="192.168.1.50:5555"))
    p = zvuk_player.ZvukPlayer(panel, FakeAuth(), cache)
    p._ensure_source = lambda: setattr(p, "media_port", 12345)
    p._lan_ip = lambda: "127.0.0.1"
    return p


class PayloadTests(unittest.TestCase):
    def test_audio_play_protobuf(self):
        raw = base64.b64decode(glagol.audio_play_data("http://pc/1.mp3", "Название", "Артист", 1250, True))
        fields, i = {}, 0
        while i < len(raw):
            key = raw[i]; i += 1
            self.assertEqual(key & 7, 2)
            n = shift = 0
            while True:
                b = raw[i]; i += 1
                n |= (b & 0x7f) << shift; shift += 7
                if not b & 0x80:
                    break
            fields[key >> 3] = raw[i:i + n]; i += n
        self.assertEqual(fields[1], b"audio_play")
        body = json.loads(fields[2].decode())
        self.assertEqual(body["stream"]["url"], "http://pc/1.mp3")
        self.assertEqual(body["stream"]["format"], "MP3")
        self.assertEqual(body["stream"]["title"], "Название")
        self.assertEqual(body["stream"]["subtitle"], "Артист")
        self.assertEqual(body["stream"]["offset_ms"], 1250)
        self.assertTrue(body["set_pause"])


class ParseTests(unittest.TestCase):
    def test_track_parser(self):
        node = {"id": 5, "title": "Песня", "duration": 181,
                "artists": [{"id": 9, "title": "Артист", "image": {"src": "https://cdn/a{size}.jpg"}}],
                "release": {"id": 7, "title": "Альбом", "date": "2020-05-01T00:00:00", "image": {"src": "https://cdn/r{size}.jpg"}}}
        t = zvuk.track_from(node)
        self.assertEqual((t["id"], t["title"], t["artists"], t["duration"]), ("5", "Песня", "Артист", 181))
        self.assertEqual(t["cover"], "https://cdn/r400x400.jpg")
        self.assertEqual(t["artist_ids"], ["9"])
        self.assertEqual(t["album_id"], "7")
        self.assertEqual((t["album_id"], t["album"], t["year"]), ("7", "Альбом", "2020"))
        self.assertIsNone(zvuk.track_from({"id": 1, "title": "Без длительности"}))

    def test_search_parser(self):
        fixture = {"search": {
            "tracks": {"items": [{"id": "1", "title": "Трек", "duration": 100,
                                  "artists": [{"title": "A"}],
                                  "release": {"id": "9", "title": "R", "date": "2021-01-01",
                                              "image": {"src": "https://cdn/{size}.jpg"}}}]},
            "releases": {"items": [{"id": "2", "title": "Альбом", "date": "2022-01-01",
                                    "type": "album", "image": {"src": "https://cdn/{size}.jpg"}, "artists": [{"title": "A"}]}]},
            "artists": {"items": [{"id": "3", "title": "Артист", "image": {"src": "https://cdn/{size}.jpg"}}]},
            "playlists": {"items": [{"id": "4", "title": "Плейлист", "duration": 600,
                                     "image": {"src": "https://cdn/{size}.jpg"}}]},
        }}

        class ApiNoNet(zvuk.Api):
            def __init__(self):
                pass

            def _gql(self, query, op, variables):
                return fixture

        found = ApiNoNet().search("x")
        self.assertEqual(found["tracks"][0]["title"], "Трек")
        self.assertEqual(found["albums"][0]["id"], "2")
        self.assertEqual(found["artists"][0]["id"], "3")
        self.assertEqual(found["playlists"][0]["id"], "4")


class ApiExtraTests(unittest.TestCase):
    def test_release_artist_and_like(self):
        api = zvuk.Api(FakeAuth())

        def gql(query, op, variables):
            if op == "getReleases":
                return {"getReleases": [{"id": variables["ids"][0], "title": "Альбом", "tracks": [
                    {"id": 1, "title": "A", "duration": 100, "artists": [{"id": 9, "title": "Арт"}],
                     "release": {"id": 7, "title": "Альбом"}}]}]}
            if op == "getArtists":
                return {"getArtists": [{"id": variables["ids"][0], "title": "Арт",
                                        "image": {"src": "https://cdn/a{size}.jpg"}, "popularTracks": [
                                            {"id": 2, "title": "B", "duration": 120, "artists": [{"id": 9, "title": "Арт"}],
                                             "release": {"id": 7, "title": "Альбом"}}]}]}
            if op == "addItemToCollection":
                return {"collection": {"addItem": True}}
            if op == "removeItemFromCollection":
                return {"collection": {"removeItem": True}}
            if op == "addItemToHidden":
                return {"hidden_collection": {"addItem": True}}
            if op == "removeItemFromHidden":
                return {"hidden_collection": {"removeItem": True}}
            return {}

        api._gql = gql
        album = api.release_tracks("7")
        self.assertEqual((album["title"], album["tracks"][0]["artist_ids"]), ("Альбом", ["9"]))
        artist = api.artist("9")
        self.assertEqual((artist["name"], artist["cover"]), ("Арт", "https://cdn/a400x400.jpg"))
        self.assertEqual(artist["popular"][0]["id"], "2")
        self.assertTrue(api.like("1", True))
        self.assertTrue(api.like("1", False))
        self.assertTrue(api.dislike("1", True))
        self.assertTrue(api.dislike("1", False))

    def test_wave_popular_option(self):
        api = zvuk.Api(FakeAuth())
        seen = {}

        def gql(query, op, variables):
            seen.update(variables)
            return {"personalWaveContent": [{"id": 1, "title": "A", "duration": 100,
                                             "artists": [{"id": 9, "title": "Арт"}],
                                             "release": {"id": 7, "title": "Альбом"}}]}

        api._gql = gql
        api.personal_wave(10, "energy:0.1,fun:0.9", 0.7, ["rock", "pop"], "russian", 1)
        self.assertEqual(seen["options"], {"mood": "energy:0.1,fun:0.9", "popular": 0.7,
                                           "genre": [{"name": "rock", "type": "LVL1"},
                                                     {"name": "pop", "type": "LVL1"}],
                                           "language": "russian", "vocal": 1})
        api.personal_wave(10, None, 5, None, "", 0)
        self.assertEqual(seen["options"]["popular"], 1.0)   # clamp
        self.assertEqual(seen["options"]["vocal"], 0)
        self.assertNotIn("genre", seen["options"])

    def test_liked_tracks_order(self):
        api = zvuk.Api(FakeAuth())

        def gql(query, op, variables):
            if op == "userTracks":
                return {"collection": {"tracks": [{"id": 2}, {"id": 1}]}}
            if op == "getTracks":
                return {"getTracks": [
                    {"id": 1, "title": "A", "duration": 100, "artists": [{"id": 9, "title": "Арт"}],
                     "release": {"id": 7, "title": "Альбом"}},
                    {"id": 2, "title": "B", "duration": 100, "artists": [{"id": 9, "title": "Арт"}],
                     "release": {"id": 7, "title": "Альбом"}},
                ]}
            return {}

        api._gql = gql
        self.assertEqual([t["id"] for t in api.liked_tracks()], ["2", "1"])   # порядок коллекции


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.player = make_player(Path(self.tmp.name))
        self.patch = patch.object(zvuk_player.zvuk, "Api", FakeApi)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_flow_queue_and_pause(self):
        state = self.player.flow()
        self.assertTrue(state["playing"])
        self.assertEqual(state["track"]["id"], "1")
        self.assertEqual(len(state["queue"]), 2)
        first = self.player.panel.glagol.calls[-1]
        self.assertIn("http://127.0.0.1:12345/zvuk/stream/1.mp3", first["url"])
        self.assertFalse(first["pause"])
        state = self.player.next()
        self.assertEqual(state["track"]["id"], "2")
        state = self.player.prev()
        self.assertEqual(state["track"]["id"], "1")
        state = self.player.pause()
        self.assertTrue(state["paused"])
        self.assertTrue(self.player.panel.glagol.calls[-1]["pause"])
        state = self.player.resume()
        self.assertTrue(state["playing"])
        self.assertFalse(self.player.panel.glagol.calls[-1]["pause"])

    def test_search_play_and_info(self):
        found = self.player.search("запрос")
        self.assertEqual(found["tracks"][0]["id"], "1")
        state = self.player.play_track(found["tracks"][0])
        self.assertEqual(state["track"]["title"], "Первый")
        info = self.player.music_info()
        p = info["glagol"]["state"]["playerState"]
        self.assertEqual(info["provider"], "zvuk")
        self.assertEqual(p["title"], "Первый")
        self.assertEqual(p["subtitle"], "Артист")
        self.assertEqual(p["extra"]["coverURI"], "https://cdn/1.jpg")
        self.assertTrue(info["auth"]["logged_in"])

    def test_dislike_hides_track(self):
        self.player.play_track(dict(TRACKS[0]))
        self.assertTrue(self.player.dislike("1", True)["hidden"])
        self.assertTrue(self.player.track["hidden"])
        self.assertFalse(self.player.dislike("1", False)["hidden"])
        self.assertFalse(self.player.track["hidden"])

    def test_flow_passes_popular(self):
        self.player.flow("energy:0.1,fun:0.9", 0.25, "Jazz", "russian", 0)
        self.assertEqual(self.player.mood, "energy:0.1,fun:0.9")
        self.assertEqual(self.player.popular, 0.25)
        self.assertEqual((self.player.genre, self.player.language, self.player.vocal), (["Jazz"], "russian", 0))

    def test_favorites_settings_and_volume(self):
        self.assertEqual([t["id"] for t in self.player.favorites()["tracks"]], ["1", "2"])
        state = self.player.set_settings("energy:0.2,fun:0.8", 0.3, "Rock", "russian", 1)
        self.assertEqual((state["mood"], state["popular"]), ("energy:0.2,fun:0.8", 0.3))
        self.assertEqual((state["genre"], state["language"], state["vocal"]), (["Rock"], "russian", 1))
        state = self.player.set_settings(genre=[], language="any", popular=None, vocal=0)
        self.assertEqual((state["genre"], state["language"], state["vocal"]), ([], "any", 0))
        self.assertIsNone(state["popular"])
        self.player.panel.glagol.state = {"volume": 0.65}
        self.assertEqual(self.player.music_info()["glagol"]["state"]["volume"], 0.65)

    def test_flow_extends_when_queue_ends(self):
        self.player.flow()
        self.assertEqual([t["id"] for t in self.player.state()["queue"]], ["1", "2"])
        self.player.next()
        state = self.player.next()          # конец очереди → сразу новый поток
        self.assertTrue(state["playing"])
        self.assertEqual(state["track"]["id"], "1")
        self.assertEqual(state["index"], 0)
        self.assertEqual([t["id"] for t in state["queue"]], ["1", "2"])

    def test_search_queue_does_not_extend(self):
        self.player.play_track(dict(TRACKS[0]))
        self.assertFalse(self.player.flow_active)
        with self.assertRaises(zvuk.Error):
            self.player.next()

    def test_cache_keeps_only_current_queue(self):
        cache = self.player.cache
        (cache / "old.mp3").write_bytes(b"x" * 5000)
        self.player.play_track(dict(TRACKS[0]))
        self.assertFalse((cache / "old.mp3").exists())
        self.assertTrue((cache / "1.mp3").exists())
        (cache / "2.mp3").write_bytes(b"x" * 5000)
        self.player.queue_add(dict(TRACKS[1]))
        self.player._prune_cache()
        self.assertTrue((cache / "1.mp3").exists())
        self.assertTrue((cache / "2.mp3").exists())

    def test_queue_add_clear_and_remove(self):
        self.player.play_track(dict(TRACKS[0]))
        self.player.queue_add(dict(TRACKS[1]))
        self.assertEqual(len(self.player.state()["queue"]), 2)
        self.player.remove(1)
        self.assertEqual(len(self.player.state()["queue"]), 1)
        self.player.queue_add(dict(TRACKS[1]))
        self.player.queue_clear()
        self.assertEqual(len(self.player.state()["queue"]), 1)   # играющий остаётся

    def test_album_similar_artist_and_like(self):
        track = dict(TRACKS[0], artist_ids=["77"])
        self.player.play_track(track)
        album = self.player.album_tracks()
        self.assertEqual((album["id"], album["title"]), ("10", "Альбом"))
        self.assertEqual([t["id"] for t in album["tracks"]], ["1", "2"])
        similar = self.player.similar_tracks()
        self.assertEqual(similar["artist"], "Артист")
        self.assertEqual([t["id"] for t in similar["tracks"]], ["2"])   # без текущего трека
        artist = self.player.artist_info()
        self.assertEqual((artist["id"], artist["name"]), ("77", "Артист"))
        self.assertEqual(self.player.like("1", True)["liked"], True)
        self.assertTrue(self.player.track["liked"])
        self.assertFalse(self.player.like("1", False)["liked"])
        self.assertFalse(self.player.track["liked"])

    def test_play_list_and_reset(self):
        self.player.play_list([dict(t) for t in TRACKS], 1)
        state = self.player.state()
        self.assertEqual(state["track"]["id"], "2")
        self.assertEqual([t["id"] for t in state["queue"]], ["1", "2"])
        self.player.reset_state()
        self.assertIsNone(self.player.state()["track"])
        self.assertFalse(self.player.state()["playing"])

    def test_token_accepts_json_or_plain(self):
        self.player.set_token('{"result": {"token": "' + "t" * 32 + '"}, "login": "x"}')
        self.assertEqual(self.player.auth.token, "t" * 32)
        self.player.set_token("p" * 32)
        self.assertEqual(self.player.auth.token, "p" * 32)
        with self.assertRaises(zvuk.AuthError):
            self.player.set_token("не токен")


    def test_station_foreign_detects_voice_switch(self):
        import time
        self.player.track = {"id": "1", "title": "Наша песня", "duration": 100}
        self.player.playing = True
        self.player.started = time.monotonic() - 10
        self.player.panel.glagol.state = {"playerState": {"title": "Другая песня"}}
        self.assertTrue(self.player.station_foreign())
        self.player.panel.glagol.state = {"playerState": {"title": "наша песня"}}
        self.assertFalse(self.player.station_foreign())
        self.player.started = time.monotonic()       # сразу после старта — ещё не трогаем
        self.player.panel.glagol.state = {"playerState": {"title": "Другая песня"}}
        self.assertFalse(self.player.station_foreign())
        self.player.acknowledge_foreign()
        self.assertFalse(self.player.playing)


class LyricsTests(unittest.TestCase):
    def test_falls_back_to_lrclib_by_name(self):
        class FakeY:
            def lyrics(self, tid):
                return {"sync": None, "text": None}

            def lyrics_by_name(self, title, artist, album=None, duration=0):
                return {"sync": None, "text": "LRCLIB", "source": "LRCLIB"}

        panel = server.Panel.__new__(server.Panel)
        panel.auth = SimpleNamespace(music_token="x")
        panel.ymusic = FakeY()
        panel._need_login = lambda: None
        self.assertEqual(panel.music_lyrics("1", "Песня", "Артист")["source"], "LRCLIB")
        # Если Яндекс дал текст — LRCLIB не дёргаем.
        panel.ymusic.lyrics = lambda tid: {"sync": None, "text": "Яндекс"}
        self.assertEqual(panel.music_lyrics("1", "Песня", "Артист")["text"], "Яндекс")

    def test_lyrics_without_yandex_login(self):
        class FakeY:
            def lyrics(self, tid):
                raise AssertionError("без входа в Яндекс Яндекс не спрашиваем")

            def lyrics_by_name(self, title, artist, album=None, duration=0):
                return {"sync": None, "text": "LRCLIB", "source": "LRCLIB"}

        panel = server.Panel.__new__(server.Panel)
        panel.auth = SimpleNamespace(music_token="")
        panel.ymusic = FakeY()
        r = panel.music_lyrics("", title="Песня", artist="Артист")
        self.assertEqual(r["text"], "LRCLIB")

    def test_music_auto_switches_when_station_plays_other(self):
        panel = server.Panel.__new__(server.Panel)
        panel.zvuk = SimpleNamespace(station_foreign=lambda: True,
                                     acknowledge_foreign=lambda: None,
                                     stop=lambda: None)
        panel.auth = SimpleNamespace(info=lambda: {"logged_in": True}, music_token="x")
        panel.glagol = SimpleNamespace(info=lambda: {"connected": True, "state": None})
        with patch.object(server, "load_settings", return_value={"music_provider": "zvuk"}), \
             patch.object(server, "save_settings"):
            self.assertEqual(panel.music()["provider"], "yandex")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.panel = server.Panel.__new__(server.Panel)

    def test_music_provider_routing(self):
        self.panel.zvuk = SimpleNamespace(
            music_info=lambda: {"provider": "zvuk", "auth": {"logged_in": True}, "glagol": {}},
            station_foreign=lambda: False, acknowledge_foreign=lambda: None)
        with patch.object(server, "load_settings", return_value={"music_provider": "zvuk"}):
            self.assertEqual(self.panel.music()["provider"], "zvuk")
        self.panel.auth = SimpleNamespace(info=lambda: {"logged_in": True})
        self.panel.glagol = SimpleNamespace(info=lambda: {"connected": True, "state": None})
        with patch.object(server, "load_settings", return_value={}):
            self.assertEqual(self.panel.music()["provider"], "yandex")

    def test_music_cmd_routing(self):
        self.panel.zvuk = SimpleNamespace(
            command=lambda d: {"cmd": d.get("command")},
            flow=lambda: {"flow": True})
        self.panel.glagol = SimpleNamespace(send=lambda payload, timeout=6: {"payload": payload})
        with patch.object(server, "load_settings", return_value={"music_provider": "zvuk"}):
            self.assertEqual(self.panel.music_cmd({"command": "next"})["cmd"], "next")
            self.assertEqual(self.panel.music_cmd({"command": "sendText", "text": "Включи мою волну"})["flow"], True)
            self.assertEqual(self.panel.music_cmd({"command": "setVolume", "volume": 0.5})["payload"]["command"], "setVolume")

    def test_provider_switch_saves_and_stops(self):
        saved, sends = {}, []
        self.panel.auth = SimpleNamespace(music_token="x")
        self.panel.zvuk = SimpleNamespace(auth=SimpleNamespace(token=""), stop=lambda: None)
        self.panel.glagol = SimpleNamespace(
            send=lambda payload, timeout=6: sends.append(payload) or {"status": "SUCCESS"})
        with patch.object(server, "load_settings", return_value={}), \
             patch.object(server, "save_settings", side_effect=lambda d: saved.update(d)):
            self.assertEqual(self.panel.music_provider("zvuk")["provider"], "zvuk")
        self.assertEqual(saved["music_provider"], "zvuk")
        self.assertEqual(sends[0]["command"], "stop")


class SourceTests(unittest.TestCase):
    def test_head_get_and_range(self):
        data = b"ID3" + bytes(range(256)) * 40
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            player = zvuk_player.ZvukPlayer(
                SimpleNamespace(glagol=FakeGlagol(), adb=SimpleNamespace(serial="127.0.0.1:5555")),
                FakeAuth(), cache)
            (cache / "42.mp3").write_bytes(data)   # после старта: стартовая уборка кеша уже прошла
            player._lan_ip = lambda: "127.0.0.1"
            player._ensure_source()
            try:
                url = f"http://127.0.0.1:{player.media_port}/zvuk/stream/42.mp3?t={player.secret}"
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(int(r.headers["Content-Length"]), len(data))
                    self.assertEqual(r.headers["Accept-Ranges"], "bytes")
                req = urllib.request.Request(url, headers={"Range": "bytes=10-19"})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.status, 206)
                    self.assertEqual(r.read(), data[10:20])
                    self.assertEqual(r.headers["Content-Range"], f"bytes 10-19/{len(data)}")
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(f"http://127.0.0.1:{player.media_port}/zvuk/stream/42.mp3?t=nope")
                self.assertEqual(e.exception.code, 403)
                e.exception.close()
            finally:
                player.source_srv.shutdown()
                player.source_srv.server_close()


if __name__ == "__main__":
    unittest.main()
