"""Simulated HTTP server and client fixture for E2E testing."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class HttpClientDouble:
    """Convenience HTTP client for issuing requests against loopback test server."""
    __test__ = False

    def __init__(self, host: str, port: int, default_cookie: Optional[str] = None):
        self.host = host
        self.port = port
        self.default_cookie = default_cookie

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes | str | Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        cookie: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        req_headers = dict(headers or {})
        active_cookie = cookie if cookie is not None else self.default_cookie
        if active_cookie and "Cookie" not in req_headers:
            req_headers["Cookie"] = active_cookie

        req_body: Optional[bytes] = None
        if body is not None:
            if isinstance(body, dict):
                req_body = json.dumps(body).encode("utf-8")
                if "Content-Type" not in req_headers:
                    req_headers["Content-Type"] = "application/json"
            elif isinstance(body, str):
                req_body = body.encode("utf-8")
            else:
                req_body = body
            req_headers["Content-Length"] = str(len(req_body))

        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, body=req_body, headers=req_headers)
            resp = conn.getresponse()
            data = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, data, resp_headers
        finally:
            conn.close()

    def get_json(self, path: str, **kwargs) -> Tuple[int, Any]:
        status, data, _ = self.request("GET", path, **kwargs)
        try:
            return status, json.loads(data.decode("utf-8"))
        except Exception:
            return status, None

    def post_json(self, path: str, payload: Dict[str, Any], **kwargs) -> Tuple[int, Any]:
        status, data, _ = self.request("POST", path, body=payload, **kwargs)
        try:
            return status, json.loads(data.decode("utf-8"))
        except Exception:
            return status, None


class E2ETestServer:
    """Threaded HTTP server wrapper with clean lifecycle management."""

    def __init__(self, handler_cls, host: str = "127.0.0.1", port: int = 0):
        self.server = ThreadingHTTPServer((host, port), handler_cls)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._running = False

    def start(self):
        if not self._running:
            self._running = True
            self.thread.start()
            time.sleep(0.05)  # brief wait for socket listener

    def stop(self):
        if self._running:
            self._running = False
            self.server.shutdown()
            self.server.server_close()

    @property
    def client(self) -> HttpClientDouble:
        return HttpClientDouble(self.host, self.port)


# Alias for compatibility
TestHttpClient = HttpClientDouble
