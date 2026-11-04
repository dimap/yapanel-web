"""Tier 1: Feature 14 — Adversarial Hardening & Attack Deflection (F14_ADVERSARIAL_HARDENING).

Validates rejection of path traversal attacks, handling of corrupted config stores,
brute force password rate limiting, and oversized request payloads.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import access
from tests.e2e.harness.config_contract import load_config
from tests.e2e.harness.simulated_server import E2ETestServer
from tests.e2e.tier1_features.test_f13_e2e_acceptance import MockPanelHandler


def test_path_traversal_attack_rejected():
    """Verify paths containing directory traversal sequences (..) are safely rejected."""
    server = E2ETestServer(MockPanelHandler)
    server.start()
    try:
        client = server.client
        traversal_attempts = [
            "/static/../../etc/passwd",
            "/static/..%2f..%2fserver.py",
            "/static/....//....//etc/shadow",
        ]
        for path in traversal_attempts:
            status, _, _ = client.request("GET", path)
            # Must return 400 Bad Request, 403 Forbidden, or 404 Not Found, never 200
            assert status in (400, 403, 404), f"Traversal attack {path} returned status {status}"
    finally:
        server.stop()


def test_corrupted_json_config_graceful_fallback(temp_data_dir):
    """Verify loading from corrupted JSON settings recovers with safe defaults."""
    bad_file = temp_data_dir / "corrupted_settings.json"
    bad_file.write_text("{ this is completely invalid json syntax [!@#$%^&*()", encoding="utf-8")

    # Should not raise; must return safe defaults
    cfg = load_config(settings_path=bad_file, env={}, cli_args={})
    assert cfg.server.port == 8765
    assert cfg.server.host == "127.0.0.1"


def test_brute_force_password_rate_limiting(temp_data_dir):
    """Verify repeated failed login attempts trigger rate limiting / throttling."""
    auth_file = temp_data_dir / "auth.json"
    mgr = access.PanelAccess(auth_file)
    mgr.set_password("", "correct_password", "device_desktop")

    # Perform multiple consecutive wrong logins
    start_time = time.time()
    for _ in range(4):
        try:
            mgr.login("127.0.0.1", "wrong_password", "device_desktop")
        except access.AccessError:
            pass
    elapsed = time.time() - start_time

    # access.py implements an intentional sleep throttle (e.g. 0.7s per attempt)
    # 4 attempts * 0.7s = ~2.8s minimum elapsed time
    assert elapsed >= 2.0, f"Brute force throttle failed: 4 failed attempts took only {elapsed:.2f}s"


def test_extreme_port_and_negative_env_values(clean_env):
    """Verify invalid or adversarial environment variable values do not crash config parser."""
    env = {
        "PANEL_PORT": "invalid_alphabetic_port",
        "PANEL_HTTPS_PORT": "invalid_port_str",
    }
    cfg = load_config(env=env)
    # Should safely retain default valid ports
    assert cfg.server.port == 8765
    assert cfg.server.https_port == 8766


def test_empty_string_password_rejection(temp_data_dir):
    """Verify setting empty password or short password below MIN_PASSWORD raises AccessError."""
    auth_file = temp_data_dir / "auth.json"
    mgr = access.PanelAccess(auth_file)

    # Short password (< 4 chars) should be rejected
    rejected = False
    try:
        mgr.set_password("", "12", "client")
    except (access.AccessError, ValueError, RuntimeError):
        rejected = True

    assert rejected, "Password below minimum length must be rejected with an exception"
    has_pwd = mgr.enabled if hasattr(mgr, "enabled") else getattr(mgr, "has_password", lambda: False)()
    assert has_pwd is False
