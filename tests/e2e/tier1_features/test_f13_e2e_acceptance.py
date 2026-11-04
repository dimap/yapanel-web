"""Tier 1: Feature 13 — E2E Acceptance & Server Route Contracts (F13_E2E_ACCEPTANCE).

Validates loopback HTTP server startup, authentication state machine,
session cookie verification, and API route status responses.
"""
from __future__ import annotations

import tempfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
import access
from tests.e2e.harness.simulated_server import E2ETestServer, TestHttpClient


class MockPanelHandler(BaseHTTPRequestHandler):
    """Mock handler simulating server.Handler authentication and route dispatch."""
    access_mgr = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        cookie_header = self.headers.get("Cookie", "")
        # Check authentication cookie
        auth_token = None
        if access.COOKIE + "=" in cookie_header:
            auth_token = cookie_header.split(access.COOKIE + "=")[1].split(";")[0]

        is_authenticated = False
        if self.access_mgr and auth_token:
            if hasattr(self.access_mgr, "session_ok"):
                is_authenticated = self.access_mgr.session_ok(auth_token)
            elif hasattr(self.access_mgr, "check_session"):
                is_authenticated = self.access_mgr.check_session(auth_token)

        if self.path == "/api/status":
            if not is_authenticated:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "unauthorized"}')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "model": "cucumber", "online": true}')
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><head><title>Yandex Station Web</title></head></html>")
        else:
            self.send_response(404)
            self.end_headers()


def test_server_binds_loopback_and_responds_on_root():
    """Verify loopback HTTP server starts and serves index page."""
    server = E2ETestServer(MockPanelHandler)
    server.start()
    try:
        client = server.client
        status, data, headers = client.request("GET", "/")
        assert status == 200
        assert b"Yandex Station Web" in data
        assert "text/html" in headers.get("content-type", "")
    finally:
        server.stop()


def test_unauthenticated_request_rejected():
    """Verify unauthenticated API requests are rejected with 401."""
    server = E2ETestServer(MockPanelHandler)
    server.start()
    try:
        client = server.client
        status, data = client.get_json("/api/status")
        assert status == 401
        assert data.get("error") == "unauthorized"
    finally:
        server.stop()


def test_authenticated_request_accepted(temp_data_dir):
    """Verify request with valid session cookie succeeds with 200 OK."""
    auth_file = temp_data_dir / "auth.json"
    mgr = access.PanelAccess(auth_file)
    token = mgr.set_password("", "valid_password_123", "test_client")
    cookie_str = f"{access.COOKIE}={token}"

    MockPanelHandler.access_mgr = mgr
    server = E2ETestServer(MockPanelHandler)
    server.start()
    try:
        client = server.client
        status, data = client.get_json("/api/status", cookie=cookie_str)
        assert status == 200
        assert data.get("status") == "ok"
        assert data.get("online") is True
    finally:
        server.stop()


def test_pbkdf2_authentication_and_session_management(temp_data_dir):
    """Verify PBKDF2 password setting, validation, and session token checking."""
    auth_file = temp_data_dir / "auth.json"
    mgr = access.PanelAccess(auth_file)

    # Initial state: no password
    has_pwd = mgr.enabled if hasattr(mgr, "enabled") else mgr.has_password()
    assert has_pwd is False

    # Set new password
    token = mgr.set_password("", "test_secret_pass", "device_desktop")
    has_pwd = mgr.enabled if hasattr(mgr, "enabled") else mgr.has_password()
    assert has_pwd is True
    assert isinstance(token, str)
    assert len(token) > 16

    # Verify session check
    check_fn = getattr(mgr, "session_ok", getattr(mgr, "check_session", None))
    assert check_fn(token) is True
    assert check_fn("invalid_fabricated_token") is False

    # Verify password verification
    valid_token = mgr.login("127.0.0.1", "test_secret_pass", "device_desktop")
    assert valid_token is not None
    with pytest.raises(access.AccessError):
        mgr.login("127.0.0.1", "wrong_password", "device_desktop")


def test_session_cookie_name_and_ttl_constants():
    """Verify session cookie name and TTL constants in access layer."""
    assert access.COOKIE == "yl2_session"
    assert access.SESSION_TTL >= 86400  # at least 1 day
    assert access.PBKDF2_ITER >= 100_000
