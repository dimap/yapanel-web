"""Offline end-to-end SOCKS5 transport tests (RFC 1928 and RFC 1929)."""

import http.server
import os
import pathlib
import select
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from assistant_socks import SocksProxyError, build_socks_opener, parse_socks_url


class HttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[{"id":"gemini-test"}]}')

    def log_message(self, *args):
        pass


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SocksHandler(socketserver.BaseRequestHandler):
    def read_exact(self, count):
        result = bytearray()
        while len(result) < count:
            fragment = self.request.recv(count - len(result))
            if not fragment:
                raise EOFError("SOCKS client disconnected")
            result.extend(fragment)
        return bytes(result)

    def handle(self):
        client = self.request
        if self.read_exact(1) != b"\x05":
            return
        methods = self.read_exact(self.read_exact(1)[0])
        expected = self.server.expected_auth
        selected = 2 if expected is not None else 0
        client.sendall(bytes((5, selected if selected in methods else 255)))
        if selected not in methods:
            return
        if selected == 2:
            if self.read_exact(1) != b"\x01":
                return
            user = self.read_exact(self.read_exact(1)[0])
            password = self.read_exact(self.read_exact(1)[0])
            ok = (user, password) == expected
            client.sendall(b"\x01\x00" if ok else b"\x01\x01")
            if not ok:
                return
        version, command, reserved, atyp = self.read_exact(4)
        if (version, command, reserved) != (5, 1, 0):
            return
        if atyp == 3:
            host = self.read_exact(self.read_exact(1)[0]).decode("idna")
        elif atyp == 1:
            host = socket.inet_ntoa(self.read_exact(4))
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, self.read_exact(16))
        else:
            return
        port = struct.unpack("!H", self.read_exact(2))[0]
        self.server.observed = (atyp, host, port, selected)
        try:
            upstream = socket.create_connection((host, port), timeout=5)
        except OSError:
            client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return
        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        with upstream:
            while True:
                ready, _, _ = select.select([client, upstream], [], [], 5)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)


class TestSocks(unittest.TestCase):
    def setUp(self):
        self.http = ThreadedServer(("127.0.0.1", 0), HttpHandler)
        self.proxy = ThreadedServer(("127.0.0.1", 0), SocksHandler)
        self.proxy.expected_auth = None
        self.proxy.observed = None
        for server in (self.http, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.proxy, self.http):
            server.shutdown()
            server.server_close()

    def request(self, scheme="socks5h", credentials=""):
        port = self.proxy.server_address[1]
        proxy_url = f"{scheme}://{credentials}127.0.0.1:{port}"
        host = "127.0.0.1" if scheme == "socks5" else "localhost"
        endpoint = f"http://{host}:{self.http.server_address[1]}/models"
        with build_socks_opener(proxy_url).open(endpoint, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"gemini-test", response.read())

    def test_remote_dns(self):
        self.request()
        self.assertEqual(self.proxy.observed[0], 3)
        self.assertEqual(self.proxy.observed[1], "localhost")

    def test_local_dns(self):
        self.request(scheme="socks5")
        self.assertIn(self.proxy.observed[0], (1, 4))

    def test_authenticated_socks5h_with_encoded_password(self):
        self.proxy.expected_auth = (b"user", b"p@ss")
        self.request(credentials="user:p%40ss@")
        self.assertEqual(self.proxy.observed[3], 2)

    def test_wrong_password_is_rejected_without_secret_in_error(self):
        self.proxy.expected_auth = (b"user", b"correct")
        with self.assertRaises(Exception) as result:
            self.request(credentials="user:secret@")
        self.assertNotIn("secret", str(result.exception))

    def test_unauthenticated_fallback_is_forbidden(self):
        self.proxy.expected_auth = None
        with self.assertRaises(Exception):
            self.request(credentials="user:secret@")

    def test_incomplete_auth_is_rejected(self):
        for url in (
            "socks5://user@localhost:1080",
            "socks5h://:password@localhost:1080",
            "socks5h://user:@localhost:1080",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SocksProxyError):
                    parse_socks_url(url)

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_https_certificate_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = pathlib.Path(directory) / "cert.pem"
            key = pathlib.Path(directory) / "key.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                 "-days", "1", "-keyout", str(key), "-out", str(cert),
                 "-subj", "/CN=localhost", "-addext",
                 "subjectAltName=DNS:localhost"],
                check=True, capture_output=True,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            self.http.socket = context.wrap_socket(self.http.socket, server_side=True)
            proxy = f"socks5h://127.0.0.1:{self.proxy.server_address[1]}"
            endpoint = f"https://localhost:{self.http.server_address[1]}/models"
            with self.assertRaises(urllib.error.URLError) as failure:
                build_socks_opener(proxy).open(endpoint, timeout=5)
            self.assertIsInstance(failure.exception.reason, ssl.SSLError)

            original = os.environ.get("SSL_CERT_FILE")
            os.environ["SSL_CERT_FILE"] = str(cert)
            try:
                with build_socks_opener(proxy).open(endpoint, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            finally:
                if original is None:
                    os.environ.pop("SSL_CERT_FILE", None)
                else:
                    os.environ["SSL_CERT_FILE"] = original


class TestDistribution(unittest.TestCase):
    def test_socks_helper_in_distribution_lists(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        self.assertIn("assistant_socks.py", (root / ".github" / "build-windows-zip.sh").read_text(encoding="utf-8"))
        self.assertIn("assistant_socks.py", (root / "Dockerfile").read_text(encoding="utf-8"))
        self.assertIn('"assistant_socks.py"', (root / "server.py").read_text(encoding="utf-8"))

    def test_proxy_credentials_are_redacted(self):
        import assistant_max
        proxy = "socks5h://user:secret@proxy.example:1080"
        secrets = assistant_max._config_secrets({"llm": {"proxy": proxy}})
        self.assertIn(proxy, secrets)
        self.assertIn("secret", secrets)
        self.assertNotIn("secret", assistant_max._redact_secrets(f"failed {proxy}", secrets))


if __name__ == "__main__":
    unittest.main()
