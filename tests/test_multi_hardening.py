"""Hardening regressions for the multi-speaker gateway.

Covers configurable session cookies, proxy header/body handling, client-IP
filtering and WebSocket origin checks.
"""
import http.client
import ipaddress
import json
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import multi as gateway
import access
import server

DEVICE = "hardening"


class EchoWorker(BaseHTTPRequestHandler):
    """Minimal worker that reflects request headers and body as JSON."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def _reply(self):
        if self.headers.get("X-Panel-Gateway") != "test-gateway-secret":
            self.send_error(403)
            return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            payload = b""
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("X-Panel-Gateway", "should-never-reach-the-client")
            self.end_headers()
            self.wfile.flush()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        data = json.dumps({
            "path": self.path,
            "body": body.decode(),
            "gateway": self.headers.get("X-Panel-Gateway"),
            "connection": self.headers.get("Connection"),
            "keep_alive": self.headers.get("Keep-Alive"),
            "custom_hop": self.headers.get("X-Custom-Hop"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "keep-alive, X-Worker-Hop")
        self.send_header("Keep-Alive", "timeout=5")
        self.send_header("X-Worker-Hop", "must-be-dropped")
        self.send_header("X-Panel-Gateway", "must-never-be-forwarded")
        self.end_headers()
        self.wfile.write(data)


class HardeningGateway(gateway.Gateway):
    __test__ = False
    secret = "test-gateway-secret"
    networks = []


class GatewayHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        gateway.DEVICES[DEVICE] = {"name": "Hardening", "model": "station2"}
        cls.worker = ThreadingHTTPServer(("127.0.0.1", 0), EchoWorker)
        gateway.DEVICES[DEVICE]["port"] = cls.worker.server_port
        HardeningGateway.data_dir = Path(cls.tmp.name)
        HardeningGateway.access = access.PanelAccess(Path(cls.tmp.name) / "auth.json")
        HardeningGateway.networks = []
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), HardeningGateway)
        cls.host = "127.0.0.1:" + str(cls.httpd.server_port)
        for item in (cls.worker, cls.httpd):
            threading.Thread(target=item.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for item in (cls.httpd, cls.worker):
            item.shutdown()
            item.server_close()
        gateway.DEVICES.pop(DEVICE, None)
        cls.tmp.cleanup()

    def request(self, path, method="GET", cookie=None, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        values = dict(headers or {})
        if cookie:
            values["Cookie"] = cookie
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        data = response.read()
        result = response.status, data, dict(response.getheaders())
        connection.close()
        return result

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=5) as sock:
            sock.sendall(request.encode("latin1"))
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            return int(data.split(b" ", 2)[1]), data

    # ---------- session cookie ----------
    def test_cookie_name_comes_from_security_config_with_safe_fallback(self):
        configured = SimpleNamespace(security=SimpleNamespace(cookie_name="panel_sid"))
        with patch.object(config, "get_config", return_value=configured):
            self.assertEqual(gateway.cookie_name(), "panel_sid")

        broken = SimpleNamespace(security=SimpleNamespace(cookie_name="bad name; injected"))
        with patch.object(config, "get_config", return_value=broken), \
             patch.object(gateway.access, "COOKIE", "fallback_sid"):
            self.assertEqual(gateway.cookie_name(), "fallback_sid")

        with patch.object(config, "get_config", side_effect=RuntimeError("broken settings")), \
             patch.object(gateway.access, "COOKIE", "fallback_sid"):
            self.assertEqual(gateway.cookie_name(), "fallback_sid")

    def test_custom_cookie_name_is_used_by_the_gateway(self):
        original = gateway.access.COOKIE
        self.addCleanup(setattr, gateway.access, "COOKIE", original)
        with patch.object(config, "get_config", return_value=SimpleNamespace(
                security=SimpleNamespace(cookie_name="panel_sid"))):
            gateway.access.COOKIE = gateway.cookie_name()
        self.assertEqual(gateway.access.COOKIE, "panel_sid")

        def session_ok(token):
            return token == "token-value"

        with patch.object(self.httpd.RequestHandlerClass.access, "session_ok", side_effect=session_ok):
            self.assertEqual(self.request("/speaker/" + DEVICE + "/api/echo",
                                          cookie="yl2_session=token-value")[0], 401)
            status, data, _ = self.request("/speaker/" + DEVICE + "/api/echo",
                                           cookie="panel_sid=token-value")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["gateway"], "test-gateway-secret")

    # ---------- proxy ----------
    def test_proxy_preserves_content_length_and_drops_hop_by_hop_and_gateway_key(self):
        body = b"payload-bytes"
        status, data, headers = self.request(
            "/speaker/" + DEVICE + "/api/echo", "POST",
            headers={
                "Content-Type": "application/octet-stream",
                "Connection": "keep-alive, X-Custom-Hop",
                "Keep-Alive": "timeout=5",
                "X-Custom-Hop": "must-be-dropped",
                "X-Panel-Gateway": "forged-client-value",
            },
            body=body,
        )
        self.assertEqual(status, 200)
        seen = json.loads(data)
        self.assertEqual(seen["body"], body.decode())
        self.assertEqual(seen["gateway"], "test-gateway-secret")
        self.assertIsNone(seen["custom_hop"])
        self.assertIsNone(seen["keep_alive"])

        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(int(lowered["content-length"]), len(data))
        self.assertEqual(lowered["connection"], "close")
        self.assertNotIn("keep-alive", lowered)
        self.assertNotIn("x-worker-hop", lowered)
        self.assertNotIn("x-panel-gateway", lowered)

    def test_oversized_content_length_is_rejected_with_413(self):
        status, data = self.raw(
            "POST /speaker/" + DEVICE + "/api/echo HTTP/1.1\r\n"
            "Host: " + self.host + "\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: 536870913\r\n"
            "\r\n"
        )
        self.assertEqual(status, 413)
        self.assertIn("Слишком большой запрос".encode("utf8"), data)

    def test_invalid_content_length_is_rejected_without_crashing(self):
        status, data = self.raw(
            "POST /speaker/" + DEVICE + "/api/echo HTTP/1.1\r\n"
            "Host: " + self.host + "\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: not-a-number\r\n"
            "\r\n"
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Content-Length", data)

    # ---------- client address filtering ----------
    def _probe(self, address, networks):
        probe = object.__new__(gateway.Gateway)
        probe.networks = networks
        probe.client_address = (address, 12345)
        probe.sent = []
        probe._send = lambda code, body, **kwargs: probe.sent.append((code, body))
        return probe

    def test_allowed_handles_ipv6_invalid_and_mapped_addresses(self):
        probe = self._probe("::1", [ipaddress.ip_network("10.0.0.0/8")])
        self.assertFalse(probe._allowed("/api/status"))
        self.assertEqual(probe.sent[-1][0], 403)

        probe = self._probe("not-an-ip", [ipaddress.ip_network("10.0.0.0/8")])
        self.assertFalse(probe._allowed("/api/status"))
        self.assertEqual(probe.sent[-1][0], 403)

        probe = self._probe("10.1.2.3", ["not-a-network"])
        self.assertFalse(probe._allowed("/api/status"))
        self.assertEqual(probe.sent[-1][0], 403)

        probe = self._probe("10.1.2.3", [ipaddress.ip_network("10.0.0.0/8")])
        with patch.object(server.Handler, "_guard", return_value=False):
            self.assertTrue(probe._allowed("/api/status"))

        probe = self._probe("::ffff:10.1.2.3", [ipaddress.ip_network("10.0.0.0/8")])
        with patch.object(server.Handler, "_guard", return_value=False):
            self.assertTrue(probe._allowed("/api/status"))

        probe = self._probe("2001:db8::1", [ipaddress.ip_network("2001:db8::/32")])
        with patch.object(server.Handler, "_guard", return_value=False):
            self.assertTrue(probe._allowed("/api/status"))

    def test_allow_network_parser_rejects_invalid_cidr(self):
        self.assertEqual(gateway.network("192.168.1.5/24").network_address,
                         ipaddress.ip_network("192.168.1.0/24").network_address)
        with self.assertRaises(Exception):
            gateway.network("not-a-network")

    # ---------- tunnel ----------
    def test_tunnel_rejects_foreign_and_null_origins_before_connecting(self):
        original_port = gateway.DEVICES[DEVICE]["port"]
        gateway.DEVICES[DEVICE]["port"] = 1  # unreachable: origin must be checked first
        try:
            for origin in ("https://evil.example", "null", ""):
                request = (
                    "GET /speaker/" + DEVICE + "/api/term HTTP/1.1\r\n"
                    "Host: " + self.host + "\r\n"
                    + ("Origin: " + origin + "\r\n" if origin else "") +
                    "Connection: Upgrade\r\n"
                    "Upgrade: websocket\r\n"
                    "\r\n"
                )
                status, data = self.raw(request)
                self.assertEqual(status, 403, origin)
                self.assertIn("чужого сайта".encode("utf8"), data)
        finally:
            gateway.DEVICES[DEVICE]["port"] = original_port

    def test_tunnel_handshake_does_not_leak_gateway_key(self):
        with socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=5) as sock:
            packet = (
                "GET /speaker/" + DEVICE + "/api/term HTTP/1.1\r\n"
                "Host: " + self.host + "\r\n"
                "Origin: http://" + self.host + "\r\n"
                "Connection: Upgrade\r\n"
                "Upgrade: websocket\r\n"
                "\r\n"
            )
            sock.sendall(packet.encode("latin1"))
            reply = b""
            while b"\r\n\r\n" not in reply:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
        self.assertIn(b" 101 ", reply)
        self.assertNotIn(b"X-Panel-Gateway", reply)
        self.assertNotIn(b"should-never-reach-the-client", reply)

    # ---------- worker environment ----------
    def test_workers_inherit_the_same_cookie_name(self):
        captured = {}

        def fake_popen(command, cwd, env, stdout, stderr):
            captured.update(env)
            Path(env["PANEL_READY_FILE"]).write_text(json.dumps({"port": 4321}), encoding="utf8")
            return Mock(poll=lambda: None)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(gateway.subprocess, "Popen", side_effect=fake_popen), \
             patch.object(gateway.secrets, "token_urlsafe", return_value="gateway-secret"):
            workers = gateway.Workers(
                {"speaker": {"device": "1.2.3.4:5555", "model": "lite2"}},
                Path(directory), timeout=2, cookie_name="panel_sid")
            self.assertEqual(workers.start(), "gateway-secret")
            workers.stop()

        self.assertEqual(captured["PANEL_COOKIE_NAME"], "panel_sid")
        self.assertEqual(captured["PANEL_SESSION_COOKIE"], "panel_sid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
