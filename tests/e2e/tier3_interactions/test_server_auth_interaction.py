"""Tier 3: Cross-Feature Interactions — HTTP Server & Authentication Pipeline.

Tests complete pairwise loopback interaction between HTTP server endpoints,
client cookie transport, session state machine, and login/logout flows.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import pytest

import access
from tests.e2e.harness.simulated_server import E2ETestServer, HttpClientDouble


class AuthApiHandler(BaseHTTPRequestHandler):
    """Simulates server HTTP handler routing with real access.PanelAccess backend."""
    access_mgr: access.PanelAccess | None = None

    def log_message(self, *args):
        pass

    def _extract_token(self) -> str | None:
        cookie_header = self.headers.get("Cookie", "")
        cookie_key = access.COOKIE + "="
        if cookie_key in cookie_header:
            return cookie_header.split(cookie_key)[1].split(";")[0].strip()
        return None

    def do_GET(self):
        token = self._extract_token()
        is_auth = False
        if self.access_mgr and token:
            is_auth = self.access_mgr.session_ok(token)

        if self.path == "/api/status":
            if not is_auth:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "unauthorized"}')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "online": true, "model": "cucumber"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                pass

        if self.path == "/api/login":
            password = payload.get("password", "")
            ip = self.client_address[0]
            ua = self.headers.get("User-Agent", "TestClient")
            try:
                if not self.access_mgr:
                    raise access.AccessError("No access manager")
                token = self.access_mgr.login(ip, password, ua)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"{access.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            except access.AccessError as e:
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path == "/api/logout":
            token = self._extract_token()
            if self.access_mgr and token:
                self.access_mgr.logout(token)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"{access.COOKIE}=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT")
            self.end_headers()
            self.wfile.write(b'{"status": "logged_out"}')
        else:
            self.send_response(404)
            self.end_headers()


def test_complete_login_and_authenticated_status_flow(temp_data_dir):
    """Verify unauthenticated 401 -> POST login -> session cookie -> 200 OK."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    mgr.set_password("", "master_test_pass", "admin_desktop")

    AuthApiHandler.access_mgr = mgr
    server = E2ETestServer(AuthApiHandler)
    server.start()
    try:
        client = server.client

        # 1. Initially unauthenticated request rejected
        status, data = client.get_json("/api/status")
        assert status == 401
        assert data.get("error") == "unauthorized"

        # 2. Login with correct password
        status, body, headers = client.request("POST", "/api/login", body={"password": "master_test_pass"})
        assert status == 200
        set_cookie = headers.get("set-cookie", "")
        assert access.COOKIE + "=" in set_cookie
        cookie_val = set_cookie.split(";")[0]

        # 3. Access protected status with session cookie
        status, data = client.get_json("/api/status", cookie=cookie_val)
        assert status == 200
        assert data.get("status") == "ok"
        assert data.get("online") is True
    finally:
        server.stop()


def test_logout_revokes_session_token(temp_data_dir):
    """Verify POST /api/logout invalidates token and blocks subsequent queries."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    token = mgr.set_password("", "master_test_pass", "admin_desktop")
    cookie_str = f"{access.COOKIE}={token}"

    AuthApiHandler.access_mgr = mgr
    server = E2ETestServer(AuthApiHandler)
    server.start()
    try:
        client = server.client

        # 1. Valid request before logout
        status, data = client.get_json("/api/status", cookie=cookie_str)
        assert status == 200

        # 2. Issue logout
        status, data, headers = client.request("POST", "/api/logout", cookie=cookie_str)
        assert status == 200
        assert "logged_out" in str(data)

        # 3. Re-request with old token must now be rejected
        status, data = client.get_json("/api/status", cookie=cookie_str)
        assert status == 401
    finally:
        server.stop()


def test_invalid_password_returns_403(temp_data_dir):
    """Verify failed login returns 403 Forbidden without granting session."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    mgr.set_password("", "real_password", "client")

    AuthApiHandler.access_mgr = mgr
    server = E2ETestServer(AuthApiHandler)
    server.start()
    try:
        client = server.client
        status, body, headers = client.request("POST", "/api/login", body={"password": "incorrect_password"})
        assert status == 403
        assert "set-cookie" not in headers
    finally:
        server.stop()
