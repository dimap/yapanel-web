"""Tier 4: Real-World Scenarios — End-to-End Operational Workflows.

Tests complete end-to-end production lifecycles:
1. Panel boot and initial setup (uninitialized -> password creation -> session established).
2. Authenticated client connection, state querying, and device command execution.
3. Configuration update and hot reload without server restart or session revocation.
4. Security violation rejection and system resilience under simulated attack.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import pytest

import access
import config
from tests.e2e.harness.simulated_adb import SimulatedAdb
from tests.e2e.harness.simulated_server import E2ETestServer


class RealWorldPanelHandler(BaseHTTPRequestHandler):
    """End-to-end simulated panel HTTP handler matching production server routing."""
    access_mgr: access.PanelAccess | None = None
    adb_client: SimulatedAdb | None = None
    panel_config: config.PanelConfig | None = None

    def log_message(self, *args):
        pass

    def _get_token(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        key = access.COOKIE + "="
        if key in cookie:
            return cookie.split(key)[1].split(";")[0].strip()
        return None

    def _is_authenticated(self) -> bool:
        if not self.access_mgr:
            return False
        if not self.access_mgr.enabled:
            return True  # Setup mode: uninitialized panel allows access to setup routes
        token = self._get_token()
        return bool(token and self.access_mgr.session_ok(token))

    def do_GET(self):
        # 1. Path traversal security check
        if ".." in self.path:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "path_traversal_blocked"}')
            return

        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><title>Yandex Station Web</title></html>")

        elif self.path == "/api/setup_status":
            needs_setup = not self.access_mgr.enabled if self.access_mgr else False
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"needs_setup": needs_setup}).encode("utf-8"))

        elif self.path == "/api/status":
            if not self._is_authenticated():
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "unauthorized"}')
                return

            model = "cucumber"
            if self.adb_client:
                m_out, _, _ = self.adb_client.shell("getprop ro.product.model", check=False)
                if m_out.strip():
                    model = m_out.strip()

            resp = {
                "status": "ok",
                "online": True,
                "model": model,
                "server_port": self.panel_config.server.port if self.panel_config else 8765,
                "party_auto_start": self.panel_config.party.auto_start if self.panel_config else False,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
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

        if self.path == "/api/setup":
            new_pwd = payload.get("password", "")
            ip = self.client_address[0]
            ua = self.headers.get("User-Agent", "TestBrowser")
            try:
                token = self.access_mgr.set_password("", new_pwd, ua)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"{access.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
                self.wfile.write(b'{"status": "setup_complete"}')
            except access.AccessError as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path == "/api/login":
            pwd = payload.get("password", "")
            ip = self.client_address[0]
            ua = self.headers.get("User-Agent", "TestBrowser")
            try:
                token = self.access_mgr.login(ip, pwd, ua)
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

        elif self.path == "/api/cmd":
            if not self._is_authenticated():
                self.send_response(401)
                self.end_headers()
                return
            cmd = payload.get("cmd", "")
            # Sanitize dangerous injection characters if not permitted
            if ";" in cmd or "&&" in cmd or "|" in cmd:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "command_injection_rejected"}')
                return

            out, _, _ = self.adb_client.shell(cmd, check=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "output": out.strip()}).encode("utf-8"))

        elif self.path == "/api/config/reload":
            if not self._is_authenticated():
                self.send_response(401)
                self.end_headers()
                return
            # Reload config on class so subsequent request handler instances see updated state
            if self.panel_config and hasattr(self.panel_config, "server"):
                settings_file = self.panel_config.server.settings_file
                type(self).panel_config = config.PanelConfig.load(settings_path=settings_file)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "reloaded"}')
        else:
            self.send_response(404)
            self.end_headers()


def test_scenario1_panel_boot_and_initial_onboarding(temp_data_dir):
    """Scenario 1: Complete uninitialized panel boot, onboarding password setup, and login."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    adb = SimulatedAdb()
    cfg = config.PanelConfig.load(settings_path=temp_data_dir / ".panel_settings.json")

    RealWorldPanelHandler.access_mgr = mgr
    RealWorldPanelHandler.adb_client = adb
    RealWorldPanelHandler.panel_config = cfg

    server = E2ETestServer(RealWorldPanelHandler)
    server.start()
    try:
        client = server.client

        # 1. Fresh panel: setup_status shows needs_setup == True
        status, data = client.get_json("/api/setup_status")
        assert status == 200
        assert data.get("needs_setup") is True

        # 2. User completes onboarding setup with new password
        status, data, headers = client.request("POST", "/api/setup", body={"password": "initial_admin_password_999"})
        assert status == 200
        set_cookie = headers.get("set-cookie", "")
        assert access.COOKIE + "=" in set_cookie
        session_cookie = set_cookie.split(";")[0]

        # 3. Setup is now marked complete
        status, data = client.get_json("/api/setup_status")
        assert status == 200
        assert data.get("needs_setup") is False

        # 4. Status queries with session cookie succeed
        status, data = client.get_json("/api/status", cookie=session_cookie)
        assert status == 200
        assert data.get("status") == "ok"
    finally:
        server.stop()


def test_scenario2_client_connection_state_query_and_command(temp_data_dir):
    """Scenario 2: Authenticated client queries device info and dispatches hardware command."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    mgr.set_password("", "valid_panel_pass", "browser")
    adb = SimulatedAdb()
    cfg = config.PanelConfig.load(settings_path=temp_data_dir / ".panel_settings.json")

    RealWorldPanelHandler.access_mgr = mgr
    RealWorldPanelHandler.adb_client = adb
    RealWorldPanelHandler.panel_config = cfg

    server = E2ETestServer(RealWorldPanelHandler)
    server.start()
    try:
        client = server.client

        # 1. Client logs in
        status, _, headers = client.request("POST", "/api/login", body={"password": "valid_panel_pass"})
        assert status == 200
        session_cookie = headers.get("set-cookie", "").split(";")[0]

        # 2. Client queries status
        status, data = client.get_json("/api/status", cookie=session_cookie)
        assert status == 200
        assert data.get("model") == "Yandex Station"

        # 3. Client sends volume command
        status, data = client.post_json("/api/cmd", {"cmd": "input keyevent 24"}, cookie=session_cookie)
        assert status == 200
        assert data.get("status") == "ok"
        assert "input keyevent 24" in adb.commands
    finally:
        server.stop()


def test_scenario3_configuration_hot_reload_without_restart(temp_data_dir):
    """Scenario 3: Modify settings on disk, issue reload, verify new settings active while session persists."""
    settings_file = temp_data_dir / ".panel_settings.json"
    settings_file.write_text(json.dumps({"party": {"auto_start": False}}), encoding="utf-8")

    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    token = mgr.set_password("", "hot_reload_pass", "admin")
    session_cookie = f"{access.COOKIE}={token}"

    cfg = config.PanelConfig.load(settings_path=settings_file)
    adb = SimulatedAdb()

    RealWorldPanelHandler.access_mgr = mgr
    RealWorldPanelHandler.adb_client = adb
    RealWorldPanelHandler.panel_config = cfg

    server = E2ETestServer(RealWorldPanelHandler)
    server.start()
    try:
        client = server.client

        # 1. Verify initial status before reload
        status, data = client.get_json("/api/status", cookie=session_cookie)
        assert status == 200
        assert data.get("party_auto_start") is False

        # 2. Admin updates configuration on disk
        settings_file.write_text(json.dumps({"party": {"auto_start": True}}), encoding="utf-8")

        # 3. Admin requests configuration hot reload
        status, data = client.post_json("/api/config/reload", {}, cookie=session_cookie)
        assert status == 200
        assert data.get("status") == "reloaded"

        # 4. Status reflects updated configuration WITHOUT server socket restart or session logout
        status, data = client.get_json("/api/status", cookie=session_cookie)
        assert status == 200
        assert data.get("party_auto_start") is True
    finally:
        server.stop()


def test_scenario4_security_violation_deflection_and_resilience(temp_data_dir):
    """Scenario 4: Path traversal, command injection, and brute force attacks are rejected cleanly."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    mgr.set_password("", "legitimate_admin_pass", "admin_browser")
    adb = SimulatedAdb()
    cfg = config.PanelConfig.load(settings_path=temp_data_dir / ".panel_settings.json")

    RealWorldPanelHandler.access_mgr = mgr
    RealWorldPanelHandler.adb_client = adb
    RealWorldPanelHandler.panel_config = cfg

    server = E2ETestServer(RealWorldPanelHandler)
    server.start()
    try:
        client = server.client

        # 1. Attacker attempts path traversal
        status, data, _ = client.request("GET", "/static/../../etc/passwd")
        assert status == 403
        assert b"path_traversal_blocked" in data

        # 2. Attacker attempts command injection without auth
        status, _, _ = client.request("POST", "/api/cmd", body={"cmd": "reboot; rm -rf /"})
        assert status == 401

        # 3. Legitimate admin logs in simultaneously during attack
        status, _, headers = client.request("POST", "/api/login", body={"password": "legitimate_admin_pass"})
        assert status == 200
        admin_cookie = headers.get("set-cookie", "").split(";")[0]

        # 4. Even with valid session, injection metacharacters are rejected
        status, data = client.post_json("/api/cmd", {"cmd": "id; cat /data/secret"}, cookie=admin_cookie)
        assert status == 400
        assert data.get("error") == "command_injection_rejected"

        # 5. Legitimate safe command succeeds
        status, data = client.post_json("/api/cmd", {"cmd": "echo health_check"}, cookie=admin_cookie)
        assert status == 200
        assert data.get("output") == "health_check"
    finally:
        server.stop()
