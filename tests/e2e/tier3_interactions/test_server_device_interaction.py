"""Tier 3: Cross-Feature Interactions — Server Endpoint & ADB Device Dispatch.

Tests HTTP API dispatch to device management layer, ADB property queries,
command execution recording, and error recovery on command failure.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
import pytest

from tests.e2e.harness.simulated_adb import SimulatedAdb
from tests.e2e.harness.simulated_server import E2ETestServer


class DeviceApiHandler(BaseHTTPRequestHandler):
    """Simulates server handler invoking ADB for device operations."""
    adb_client: SimulatedAdb | None = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/device/info":
            if not self.adb_client:
                self.send_response(500)
                self.end_headers()
                return
            try:
                model, _, _ = self.adb_client.shell("getprop ro.product.model")
                android_ver, _, _ = self.adb_client.shell("getprop ro.build.version.release")
                payload = {
                    "model": model.strip(),
                    "android_version": android_ver.strip(),
                    "serial": self.adb_client.serial,
                    "root": self.adb_client.root_access,
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        payload = json.loads(body.decode("utf-8")) if body else {}

        if self.path == "/api/device/command":
            cmd = payload.get("cmd", "")
            try:
                out, err, code = self.adb_client.shell(cmd, check=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "output": out.strip()}).encode("utf-8"))
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "command_failed", "detail": str(e)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def test_device_info_query_dispatches_to_adb():
    """Verify GET /api/device/info triggers ADB property probes and packages response."""
    adb = SimulatedAdb(serial="192.168.1.100:5555", root_access=True)
    DeviceApiHandler.adb_client = adb

    server = E2ETestServer(DeviceApiHandler)
    server.start()
    try:
        client = server.client
        status, data = client.get_json("/api/device/info")
        assert status == 200
        assert data["model"] == "Yandex Station"
        assert data["android_version"] == "9"
        assert data["serial"] == "192.168.1.100:5555"
        assert data["root"] is True
        # Verify ADB recorded the calls
        assert any("getprop ro.product.model" in cmd for cmd in adb.commands)
    finally:
        server.stop()


def test_device_command_execution_records_in_adb():
    """Verify POST /api/device/command sends command through ADB client."""
    adb = SimulatedAdb()
    DeviceApiHandler.adb_client = adb

    server = E2ETestServer(DeviceApiHandler)
    server.start()
    try:
        client = server.client
        status, data = client.post_json("/api/device/command", {"cmd": "input keyevent 24"})
        assert status == 200
        assert data["status"] == "ok"
        assert "input keyevent 24" in adb.commands
    finally:
        server.stop()


def test_adb_command_failure_propagates_502_safely():
    """Verify ADB command failure results in HTTP 502 without crashing server thread."""
    adb = SimulatedAdb()
    adb.register_command_handler("broken_command", lambda cmd: ("", "Operation not permitted", 1))
    DeviceApiHandler.adb_client = adb

    server = E2ETestServer(DeviceApiHandler)
    server.start()
    try:
        client = server.client
        status, data = client.post_json("/api/device/command", {"cmd": "broken_command"})
        assert status == 502
        assert data["error"] == "command_failed"

        # Verify server remains responsive for subsequent commands
        status2, data2 = client.post_json("/api/device/command", {"cmd": "echo alive"})
        assert status2 == 200
        assert data2["output"] == "alive"
    finally:
        server.stop()
