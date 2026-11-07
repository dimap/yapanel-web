"""Обложка Звука через панель: у CDN нет CORS, поэтому цвет берём с прокси."""
import unittest
from unittest.mock import patch

import server


class FakeResp:
    def __init__(self, url="https://cdn.zvuk.com/a.png", data=b"\x89PNG\r\n\x1a\n" + b"x" * 100,
                 ctype="image/png"):
        self._url = url
        self._data = data
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return self._data[:n] if n and n > 0 else self._data

    def geturl(self):
        return self._url


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.p = server.Panel.__new__(server.Panel)

    def test_rejects_foreign_or_insecure(self):
        for url in ("http://cdn.zvuk.com/a.png", "https://evil.com/a.png",
                    "https://evilzvuk.com/a.png", "https://user:pass@cdn.zvuk.com/a.png"):
            with self.assertRaises(server.AdbError):
                self.p.zvuk_cover(url)

    def test_proxies_zvuk_cover(self):
        with patch.object(server.urllib.request, "urlopen", return_value=FakeResp()):
            data, ctype = self.p.zvuk_cover("https://cdn.zvuk.com/a.png")
        self.assertEqual(ctype, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_rejects_redirect_elsewhere(self):
        with patch.object(server.urllib.request, "urlopen",
                          return_value=FakeResp(url="https://evil.com/a.png")):
            with self.assertRaises(server.AdbError):
                self.p.zvuk_cover("https://cdn.zvuk.com/a.png")


if __name__ == "__main__":
    unittest.main()
