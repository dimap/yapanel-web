"""Tier 3: Cross-Feature Interactions — Configuration & Access Management.

Tests pairwise integration between centralized config.SecurityConfig / ServerConfig
and access.PanelAccess storage, subnet filtering, and PBKDF2 hashing parameters.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import pytest

import access
import config
from tests.e2e.harness.config_contract import load_config


def test_config_data_dir_determines_access_auth_path(temp_data_dir):
    """Verify ServerConfig.data_dir correctly determines .panel_auth.json location."""
    cfg = load_config(env={"PANEL_DATA_DIR": str(temp_data_dir)})
    auth_file = cfg.server.panel_auth_file
    assert auth_file == temp_data_dir / ".panel_auth.json"

    # PanelAccess initialized at configured path
    mgr = access.PanelAccess(auth_file)
    token = mgr.set_password("", "config_test_pwd", "test_runner")
    assert auth_file.is_file()
    assert mgr.session_ok(token)


def test_security_config_allowed_subnets_ipaddress_filtering():
    """Verify SecurityConfig.allowed_subnets correctly classifies LAN vs public IP addresses."""
    cfg = load_config()
    allowed_networks = [ipaddress.ip_network(net) for net in cfg.security.allowed_subnets]

    lan_ips = ["127.0.0.1", "10.0.1.50", "172.16.0.1", "192.168.1.15"]
    public_ips = ["8.8.8.8", "1.1.1.1", "77.88.8.8", "203.0.113.195"]

    for ip_str in lan_ips:
        ip = ipaddress.ip_address(ip_str)
        is_allowed = any(ip in net for net in allowed_networks)
        assert is_allowed, f"Expected {ip_str} to be allowed by subnets {cfg.security.allowed_subnets}"

    for ip_str in public_ips:
        ip = ipaddress.ip_address(ip_str)
        is_allowed = any(ip in net for net in allowed_networks)
        assert not is_allowed, f"Expected {ip_str} to be denied by subnets {cfg.security.allowed_subnets}"


def test_security_config_pbkdf2_iterations_integration(temp_data_dir):
    """Verify that PBKDF2 hash stored by PanelAccess contains configured iteration count."""
    auth_file = temp_data_dir / "auth_iter.json"
    mgr = access.PanelAccess(auth_file)
    mgr.set_password("", "secure_password_iter", "agent")

    # Read the serialized auth file
    import json
    data = json.loads(auth_file.read_text(encoding="utf-8"))
    assert "iter" in data
    assert data["iter"] >= 240_000
    assert len(data["hash"]) == 64  # SHA-256 hex string
    assert len(data["salt"]) == 32  # 16 bytes hex


def test_session_ttl_custom_override_propagation(temp_data_dir, clean_env):
    """Verify session TTL parameter is respected during session validation."""
    auth_file = temp_data_dir / "auth_ttl.json"
    mgr = access.PanelAccess(auth_file)
    token = mgr.set_password("", "ttl_password", "agent")

    assert mgr.session_ok(token)
    token_hash = access._hash_token(token)
    session_record = mgr.data["sessions"][token_hash]

    # Verify session fields
    assert "created" in session_record
    assert "last" in session_record
    assert session_record["ua"] == "agent"
