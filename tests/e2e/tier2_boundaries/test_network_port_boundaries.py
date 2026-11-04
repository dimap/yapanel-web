"""Tier 2: Boundary & Corner Cases — Network and Port Boundaries.

Tests boundary port handling (0, 65535, out-of-range, non-numeric strings),
leading zeroes, surrounding whitespace, and ephemeral binding.
"""
from __future__ import annotations

import socket
import pytest

from tests.e2e.harness import config_contract
from tests.e2e.harness.config_contract import load_config
from tests.e2e.harness.simulated_server import E2ETestServer, HttpClientDouble
from http.server import BaseHTTPRequestHandler


class EchoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")


def test_port_zero_boundary():
    """Verify port 0 (ephemeral OS port) is accepted by contract schema."""
    srv_cfg = config_contract.ServerConfig(port=0)
    assert srv_cfg.port == 0


def test_port_65535_boundary():
    """Verify port 65535 (maximum valid unsigned 16-bit TCP port) is accepted."""
    srv_cfg = config_contract.ServerConfig(port=65535)
    assert srv_cfg.port == 65535


def test_port_out_of_range_contract_rejection():
    """Verify negative and >65535 port numbers are rejected in contract schema."""
    for invalid_port in (-1, -8080, 65536, 70000, 100000):
        with pytest.raises(ValueError):
            config_contract.ServerConfig(port=invalid_port)


def test_env_port_with_surrounding_whitespace(clean_env):
    """Verify ports with surrounding whitespace are cast cleanly."""
    cfg = load_config(env={"PANEL_PORT": "  9123  ", "PANEL_HTTPS_PORT": "  9124  "})
    assert cfg.server.port == 9123
    assert cfg.server.https_port == 9124


def test_env_port_with_leading_zeros(clean_env):
    """Verify decimal ports formatted with leading zeros parse correctly."""
    cfg = load_config(env={"PANEL_PORT": "08080"})
    assert cfg.server.port == 8080


def test_env_port_non_numeric_graceful_fallback(clean_env):
    """Verify non-numeric port strings in environment fall back to safe default."""
    for bad_port in ("eight_thousand", "NaN", "Infinity", "8765.5", "127.0.0.1:8765"):
        cfg = load_config(env={"PANEL_PORT": bad_port})
        assert cfg.server.port == 8765


def test_server_binding_ephemeral_port():
    """Verify E2ETestServer binds port 0 and OS assigns an available non-zero port."""
    server = E2ETestServer(EchoHandler, host="127.0.0.1", port=0)
    server.start()
    try:
        assert server.port > 1024, f"Assigned port {server.port} should be > 1024"
        client = server.client
        status, data, _ = client.request("GET", "/")
        assert status == 200
        assert data == b"OK"
    finally:
        server.stop()
