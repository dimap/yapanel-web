"""Защитные тесты Glagol: битый файл токенов, атомарная запись, лимиты WebSocket,
синхронизация ожиданий send и выбор режима проверки TLS."""
import json
import os
import ssl
import stat
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import glagol


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class ConversationTokenErrorTests(unittest.TestCase):
    def _glagol(self):
        value = object.__new__(glagol.Glagol)
        value.device_id = "device"
        value.platform = "yandexstation_2"
        value.auth = SimpleNamespace(music_token="token")
        value.config = None
        return value

    def test_tls_eof_is_reported_as_network_not_certificate(self):
        with patch.object(
            glagol.urllib.request,
            "urlopen",
            side_effect=glagol.urllib.error.URLError(ssl.SSLEOFError("eof")),
        ):
            with self.assertRaises(glagol.AuthError) as error:
                self._glagol()._conversation_token()
        self.assertIn("TLS", str(error.exception))
        self.assertNotIn("сертификат", str(error.exception))

    def test_certificate_verification_error_still_mentions_certificates(self):
        with patch.object(
            glagol.urllib.request,
            "urlopen",
            side_effect=glagol.urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")),
        ):
            with self.assertRaises(glagol.AuthError) as error:
                self._glagol()._conversation_token()
        self.assertIn("сертификат", str(error.exception))


class YandexAuthFailSafeTests(_TempDirCase):
    def test_corrupt_json_does_not_break_panel_and_is_backed_up(self):
        broken = '{"music_token": "secret", '
        path = self.dir / ".yandex_auth.json"
        path.write_text(broken, encoding="utf-8")

        auth = glagol.YandexAuth(path)

        self.assertEqual(auth.data, {})
        self.assertIsNone(auth.music_token)
        self.assertTrue(auth.load_error)
        self.assertIn("load_error", auth.info())
        self.assertFalse(auth.info()["logged_in"])
        backup = self.dir / ".yandex_auth.json.corrupt"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), broken)
        self.assertIn(backup.name, auth.load_error)

    def test_non_object_json_is_treated_as_corrupt(self):
        path = self.dir / ".yandex_auth.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")

        auth = glagol.YandexAuth(path)

        self.assertEqual(auth.data, {})
        self.assertIn("объект JSON", auth.load_error)
        self.assertTrue((self.dir / ".yandex_auth.json.corrupt").exists())

    def test_unreadable_file_does_not_raise(self):
        path = self.dir / ".yandex_auth.json"
        path.write_text("{}", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=PermissionError("нет доступа")):
            auth = glagol.YandexAuth(path)

        self.assertEqual(auth.data, {})
        self.assertIn("не удалось прочитать", auth.load_error)

    def test_missing_file_is_not_an_error(self):
        auth = glagol.YandexAuth(self.dir / "missing.json")

        self.assertEqual(auth.data, {})
        self.assertIsNone(auth.load_error)
        self.assertFalse(auth.info()["logged_in"])

    def test_valid_file_is_loaded(self):
        path = self.dir / ".yandex_auth.json"
        path.write_text(json.dumps({"music_token": "token", "login": "user", "method": "qr"}), encoding="utf-8")

        auth = glagol.YandexAuth(path)

        self.assertEqual(auth.music_token, "token")
        self.assertIsNone(auth.load_error)
        self.assertEqual(auth.info()["login"], "user")

    def test_save_after_corrupt_file_recovers(self):
        path = self.dir / ".yandex_auth.json"
        path.write_text("{broken", encoding="utf-8")
        auth = glagol.YandexAuth(path)

        auth._save(music_token="fresh", login="user", method="qr")

        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["music_token"], "fresh")
        self.assertIsNone(auth.load_error)
        self.assertTrue(auth.info()["logged_in"])


class YandexAuthSaveTests(_TempDirCase):
    def test_save_is_atomic_and_fsynced(self):
        path = self.dir / ".yandex_auth.json"
        auth = glagol.YandexAuth(path)

        with patch("glagol.os.fsync", wraps=os.fsync) as fsync:
            auth._save(music_token="token", x_token="x", login="user", method="qr")

        self.assertTrue(fsync.called)
        self.assertEqual(auth.data, {"music_token": "token", "x_token": "x", "login": "user", "method": "qr"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), auth.data)
        self.assertIsNone(auth.load_error)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_failed_replace_keeps_old_file_and_cleans_temp(self):
        path = self.dir / ".yandex_auth.json"
        old = '{"music_token": "old"}'
        path.write_text(old, encoding="utf-8")
        auth = glagol.YandexAuth(path)

        with patch("glagol.os.replace", side_effect=OSError("диск переполнен")):
            with self.assertRaises(OSError):
                auth._save(music_token="new")

        self.assertEqual(path.read_text(encoding="utf-8"), old)
        self.assertEqual(auth.data["music_token"], "old")
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "Windows не хранит POSIX-права файла")
    def test_saved_file_has_private_mode(self):
        path = self.dir / ".yandex_auth.json"
        auth = glagol.YandexAuth(path)
        auth._save(music_token="token")

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class WebSocketLimitTests(unittest.TestCase):
    @staticmethod
    def ws_with_buf(buf):
        ws = glagol.WebSocket.__new__(glagol.WebSocket)
        ws.buf = buf
        ws.sock = Mock()
        ws.sock.recv.return_value = b""
        ws.wlock = threading.Lock()
        return ws

    def test_frame_over_limit_is_rejected_before_reading_payload(self):
        size = glagol.WebSocket.MAX_FRAME_BYTES + 1
        frame = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", size)
        ws = self.ws_with_buf(frame)

        with self.assertRaisesRegex(ConnectionError, "кадр"):
            ws.recv()

        ws.sock.recv.assert_not_called()

    def test_fill_rejects_target_larger_than_buffer_limit(self):
        ws = self.ws_with_buf(b"")

        with self.assertRaisesRegex(ConnectionError, "кадр"):
            ws._fill(glagol.WebSocket.MAX_BUFFER_BYTES + 1)

        ws.sock.recv.assert_not_called()

    def test_fragmented_message_total_is_limited(self):
        ws = self.ws_with_buf(b"\x01\x06aaaaaa" b"\x80\x06bbbbbb")
        ws.MAX_MESSAGE_BYTES = 10

        with self.assertRaisesRegex(ConnectionError, "сообщение"):
            ws.recv()

        ws.sock.recv.assert_not_called()

    def test_handshake_response_size_is_limited(self):
        sock = _FakeSocket(junk=True)
        raw = Mock()
        ctx = Mock()
        ctx.wrap_socket.return_value = sock

        with patch("glagol.socket.create_connection", return_value=raw):
            with self.assertRaisesRegex(ConnectionError, "рукопожат"):
                glagol.WebSocket("192.0.2.1", 1961, timeout=5, ssl_context=ctx)

        self.assertLess(sock.recv_calls, 10)
        self.assertTrue(sock.closed)

    def test_supplied_tls_context_is_used_for_socket(self):
        sock = _FakeSocket()
        raw = Mock()
        ctx = Mock()
        ctx.wrap_socket.return_value = sock

        with patch("glagol.socket.create_connection", return_value=raw):
            ws = glagol.WebSocket("192.0.2.1", 1961, timeout=5, ssl_context=ctx)

        ctx.wrap_socket.assert_called_once_with(raw, server_hostname="192.0.2.1")
        self.assertIs(ws.sock, sock)
        self.assertTrue(sock.sent.startswith(b"GET / HTTP/1.1"))


class _FakeSocket:
    def __init__(self, junk=False):
        self.junk = junk
        self.sent = b""
        self.recv_calls = 0
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        self.recv_calls += 1
        if self.junk:
            return b"x" * n
        return b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"

    def close(self):
        self.closed = True


class GlagolWaiterTests(unittest.TestCase):
    @staticmethod
    def make_glagol(ws):
        g = glagol.Glagol.__new__(glagol.Glagol)
        g.ws = ws
        g.token = "token"
        g.waiters = {}
        g.waiters_lock = threading.Lock()
        g.config = SimpleNamespace(cmd_timeout=0.2)
        g.wake = threading.Event()
        g.error = None
        g.auth = SimpleNamespace(music_token="token")
        g.state = None
        g.updated = 0
        g.tls_verified = False
        g.tls_warning = None
        return g

    def test_fail_waiters_wakes_send_and_cleans_dict(self):
        started = threading.Event()

        class Ws:
            def send(self, data):
                started.set()

        g = self.make_glagol(Ws())
        result = {}

        def worker():
            result["value"] = g.send({"command": "ping"}, timeout=10)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(2))

        g._fail_waiters("disconnected")

        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["value"], {"status": "disconnected"})
        self.assertEqual(g.waiters, {})

    def test_send_without_connection_fails_fast(self):
        g = self.make_glagol(None)
        started = time.monotonic()

        with self.assertRaises(glagol.AuthError):
            g.send({"command": "ping"}, timeout=10)

        self.assertLess(time.monotonic() - started, 0.5)

    def test_send_error_removes_waiter(self):
        class Ws:
            def send(self, data):
                raise OSError("соединение закрыто")

        g = self.make_glagol(Ws())

        with self.assertRaises(OSError):
            g.send({"command": "ping"})

        self.assertEqual(g.waiters, {})

    def test_reconnect_wakes_waiter_and_closes_socket(self):
        started = threading.Event()
        closed = threading.Event()

        class Ws:
            def send(self, data):
                started.set()

            def close(self):
                closed.set()

        g = self.make_glagol(Ws())
        result = {}

        def worker():
            result["value"] = g.send({"command": "ping"}, timeout=10)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(2))

        g.reconnect()

        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["value"], {"status": "reconnect"})
        self.assertTrue(closed.is_set())
        self.assertIsNone(g.ws)
        self.assertEqual(g.waiters, {})

    def test_resolve_waiter_sets_response_and_event(self):
        g = self.make_glagol(Mock())
        waiter = {"event": threading.Event(), "resp": None}
        g.waiters["rid"] = waiter

        g._resolve_waiter("rid", {"status": "SUCCESS"})

        self.assertEqual(waiter["resp"], {"status": "SUCCESS"})
        self.assertTrue(waiter["event"].is_set())


class _ca_env:
    """Окружение только с нужным PANEL_GLAGOL_CA (или без него)."""

    def __init__(self, ca=None):
        self.env = {k: v for k, v in os.environ.items() if k != glagol.GLAGOL_CA_ENV}
        if ca is not None:
            self.env[glagol.GLAGOL_CA_ENV] = ca

    def __enter__(self):
        self._patch = patch.dict(os.environ, self.env, clear=True)
        self._patch.start()

    def __exit__(self, *exc):
        self._patch.stop()


class TlsModeTests(_TempDirCase):
    def test_no_ca_keeps_local_column_working_but_reports_warning(self):
        ctx = Mock()
        with _ca_env(), patch.object(glagol.ssl, "create_default_context", return_value=ctx) as maker:
            result, verified, warning = glagol.glagol_ssl_context()

        self.assertIs(result, ctx)
        self.assertFalse(verified)
        self.assertIn(glagol.GLAGOL_CA_ENV, warning)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)
        ctx.load_verify_locations.assert_not_called()
        maker.assert_called_once()

    def test_ca_file_enables_required_verification(self):
        ca = self.dir / "column-ca.pem"
        ca.write_text("test ca", encoding="utf-8")
        ctx = Mock()
        with _ca_env(str(ca)), patch.object(glagol.ssl, "create_default_context", return_value=ctx):
            result, verified, warning = glagol.glagol_ssl_context()

        self.assertIs(result, ctx)
        self.assertTrue(verified)
        self.assertIsNone(warning)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        ctx.load_verify_locations.assert_called_once_with(cafile=str(ca))

    def test_ca_directory_is_loaded_as_capath(self):
        ctx = Mock()
        with _ca_env(str(self.dir)), patch.object(glagol.ssl, "create_default_context", return_value=ctx):
            _, verified, warning = glagol.glagol_ssl_context()

        self.assertTrue(verified)
        self.assertIsNone(warning)
        ctx.load_verify_locations.assert_called_once_with(capath=str(self.dir))

    def test_missing_ca_falls_back_to_insecure_mode_with_reason(self):
        missing = self.dir / "missing-ca.pem"
        ctx = Mock()
        with _ca_env(str(missing)), patch.object(glagol.ssl, "create_default_context", return_value=ctx):
            _, verified, warning = glagol.glagol_ssl_context()

        self.assertFalse(verified)
        self.assertIn("не найден", warning)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        ctx.load_verify_locations.assert_not_called()

    def test_info_exposes_tls_flag_and_warning(self):
        g = glagol.Glagol.__new__(glagol.Glagol)
        g.ws = None
        g.error = None
        g.state = None
        g.updated = 0
        g.tls_verified = False
        g.tls_warning = glagol.TLS_INSECURE_WARNING

        info = g.info()

        self.assertFalse(info["tls_verified"])
        self.assertIn(glagol.GLAGOL_CA_ENV, info["tls_warning"])

        g.tls_verified, g.tls_warning = True, None
        info = g.info()
        self.assertTrue(info["tls_verified"])
        self.assertIsNone(info["tls_warning"])


if __name__ == "__main__":
    unittest.main()
