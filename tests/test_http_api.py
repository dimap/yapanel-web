"""HTTP-тесты настоящего server.Handler: оболочка, файлы и вход в панель.

Роутер домена проверяется через реальный HTTP-сервер, а колонка заменена
фейковой панелью: тесты не трогают adb и не требуют устройства.
"""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import access
import server

PASSWORD = "unit-test-password"


class FakeConn:
    online = True
    state = "online"
    attempts = 1

    def info(self):
        return {"state": self.state, "online": True}

    def retry_now(self):
        return {"ok": True}


class _Anything:
    """Заглушка для методов панели, которые перечисляются, но не вызываются в тесте."""

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self


class FakePanel:
    def __init__(self):
        self.conn = FakeConn()
        self.calls = []
        self.uploads = []

    def __getattr__(self, name):
        # Остальные маршруты Handler перечисляет заранее; для них достаточно заглушки.
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything()

    def fs_list(self, path):
        self.calls.append(("fs_list", path))
        return {"path": path, "entries": [{"name": "track.mp3", "dir": False}]}

    def fs_read(self, path):
        self.calls.append(("fs_read", path))
        return {"path": path, "text": "hello"}

    def fs_write(self, path, text):
        self.calls.append(("fs_write", path, text))
        return {"ok": True}

    def fs_delete(self, path):
        self.calls.append(("fs_delete", path))
        return {"ok": True}

    def fs_download(self, path):
        self.calls.append(("fs_download", path))
        directory = tempfile.mkdtemp()
        local = Path(directory) / "track.mp3"
        local.write_bytes(b"fake-audio")
        return "track.mp3", local, directory

    def fs_upload(self, directory, name, body):
        self.uploads.append((directory, name, body))
        return {"ok": True}

    def bt_memes_status(self):
        return {"supported": True, "installed": True, "running": True,
                "autostart": False, "sounds": {}}

    def bt_memes_set(self, on):
        self.calls.append(("bt_memes_set", on))
        return {"ok": True}

    def bt_memes_upload(self, kind, data):
        self.calls.append(("bt_memes_upload", kind, data))
        return {"ok": True}

    def bt_memes_reset(self, kind):
        self.calls.append(("bt_memes_reset", kind))
        return {"ok": True}


class TestHandler(server.Handler):
    __test__ = False


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.auth = access.PanelAccess(Path(cls.tmp.name) / ".panel_auth.json")
        cls.auth.set_password("", PASSWORD, "unittest")
        cls.panel = FakePanel()
        TestHandler.panel = cls.panel
        TestHandler.access = cls.auth
        TestHandler.updater = None
        TestHandler.ports = {}
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        self.cookie = f"{access.COOKIE}={self.auth._new_session('unittest')}"

    def request(self, method, path, data=None, raw=None, cookie=None, headers=None):
        body = raw
        hdrs = dict(headers or {})
        if data is not None:
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
        token = self.cookie if cookie is None else cookie
        if token:
            hdrs["Cookie"] = token
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def json(self, response):
        return json.loads(response[1])

    def test_shell_requires_login(self):
        status, body, _ = self.request("GET", "/api/term", cookie="")
        self.assertEqual(status, 401)
        self.assertTrue(self.json((status, body))["auth"])

    def test_shell_requires_websocket_upgrade(self):
        status, body, _ = self.request("GET", "/api/term?cols=80&rows=24")
        self.assertEqual(status, 400)
        self.assertEqual(self.json((status, body))["error"], "нужен WebSocket")

    def test_shell_rejects_foreign_origin(self):
        headers = {"Upgrade": "websocket", "Connection": "Upgrade",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                   "Origin": "https://evil.example"}
        status, body, _ = self.request("GET", "/api/term", headers=headers)
        self.assertEqual(status, 403)
        self.assertIn("чужого сайта", self.json((status, body))["error"])

    def test_shell_limits_open_terminals_before_touching_adb(self):
        headers = {"Upgrade": "websocket", "Connection": "Upgrade",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                   "Origin": f"http://127.0.0.1:{self.httpd.server_port}"}
        old = server.Terminal.active
        server.Terminal.active = server.Terminal.MAX
        try:
            status, body, _ = self.request("GET", "/api/term", headers=headers)
        finally:
            server.Terminal.active = old
        self.assertEqual(status, 429)
        self.assertIn("консолей", self.json((status, body))["error"])

    def test_fs_list_and_read(self):
        status, body, _ = self.request("GET", "/api/fs/list?path=%2Fdata%2Fmusic")
        self.assertEqual(status, 200)
        self.assertEqual(self.json((status, body))["path"], "/data/music")
        self.assertEqual(self.panel.calls[-1], ("fs_list", "/data/music"))

        status, body, _ = self.request("GET", "/api/fs/read?path=%2Fdata%2Fmusic%2Fa.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(self.json((status, body))["text"], "hello")

    def test_fs_write_and_missing_field(self):
        status, body, _ = self.request("POST", "/api/fs/write",
                                       {"path": "/data/a.txt", "text": "new"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("fs_write", "/data/a.txt", "new"))

        status, body, _ = self.request("POST", "/api/fs/delete", {})
        self.assertEqual(status, 400)
        self.assertIn("path", self.json((status, body))["error"])

    def test_bt_memes_routes(self):
        status, body, _ = self.request("GET", "/api/bt/memes")
        self.assertEqual(status, 200)
        self.assertTrue(self.json((status, body))["supported"])

        status, body, _ = self.request("POST", "/api/bt/memes", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("bt_memes_set", True))

        status, body, _ = self.request("POST", "/api/bt/memes/upload?kind=off", raw=b"OggS-payload",
                                       headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("bt_memes_upload", "off", b"OggS-payload"))

        status, body, _ = self.request("POST", "/api/bt/memes/reset", {"kind": "on"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("bt_memes_reset", "on"))

    def test_fs_upload_is_raw_and_fs_download_streams(self):
        status, body, _ = self.request("POST", "/api/fs/upload", raw=b"payload",
                                       headers={"Content-Type": "application/octet-stream",
                                                "X-Filename": "voice.mp3", "X-Dir": "/tmp"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.uploads[-1], ("/tmp", "voice.mp3", b"payload"))

        status, body, headers = self.request("GET", "/api/fs/download?path=%2Fdata%2Fa.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"fake-audio")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("track.mp3", headers["Content-Disposition"])

    def test_fs_post_rejects_foreign_origin(self):
        status, body, _ = self.request("POST", "/api/fs/delete", {"path": "/data/a.txt"},
                                       headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("чужого сайта", self.json((status, body))["error"])

    def login(self, password):
        status, body, headers = self.request("POST", "/api/panel-auth/login",
                                             {"password": password}, cookie="")
        return status, body, headers

    def test_auth_login_session_and_logout(self):
        status, body, _ = self.request("GET", "/api/panel-auth", cookie="")
        self.assertEqual(status, 200)
        info = self.json((status, body))
        self.assertTrue(info["enabled"])
        self.assertFalse(info["authed"])

        status, body, _ = self.login("wrong-password")
        self.assertEqual(status, 400)
        self.assertIn("Неверный пароль", self.json((status, body))["error"])

        status, body, headers = self.login(PASSWORD)
        self.assertEqual(status, 200)
        self.assertTrue(self.json((status, body))["ok"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn(access.COOKIE + "=", cookie)

        status, body, _ = self.request("GET", "/api/fs/list?path=%2F", cookie=cookie)
        self.assertEqual(status, 200)

        status, body, _ = self.request("POST", "/api/panel-auth/logout", {}, cookie=cookie)
        self.assertEqual(status, 200)
        status, body, _ = self.request("GET", "/api/fs/list?path=%2F", cookie=cookie)
        self.assertEqual(status, 401)

    def test_auth_logout_others_keeps_current_session(self):
        status, body, headers = self.login(PASSWORD)
        self.assertEqual(status, 200)
        other = headers["Set-Cookie"].split(";", 1)[0]
        status, body, _ = self.request("POST", "/api/panel-auth/logout-others", {}, cookie=other)
        self.assertEqual(status, 200)
        self.assertIsInstance(self.json((status, body))["closed"], int)
        status, body, _ = self.request("GET", "/api/fs/list?path=%2F", cookie=other)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
