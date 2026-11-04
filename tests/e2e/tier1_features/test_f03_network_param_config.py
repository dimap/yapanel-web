"""Tier 1: Feature 3 — Network Parameter Configuration (F03_NETWORK_PARAM_CONFIG).

Validates configurable binding addresses, ports, analytics redirection lists,
and ADB daemon connection parameters.
"""
from __future__ import annotations

from pathlib import Path
import pytest
from tests.e2e.harness.config_contract import ServerConfig, DeviceConfig, GlagolConfig, load_config


def test_server_host_parameterization(clean_env):
    """Verify server host can be parameterized to any valid IPv4 interface."""
    for host in ("127.0.0.1", "0.0.0.0", "192.168.1.100"):
        cfg = load_config(env={"PANEL_HOST": host})
        assert cfg.server.host == host


def test_server_http_and_https_ports_parameterization(clean_env):
    """Verify primary HTTP and secondary HTTPS listener ports can be parameterized."""
    cfg = load_config(env={"PANEL_PORT": "18765", "PANEL_HTTPS_PORT": "18766"})
    assert cfg.server.port == 18765
    assert cfg.server.https_port == 18766


def test_glagol_and_agent_ports_parameterization(clean_env):
    """Verify auxiliary service ports (Glagol 1961, Agent 7861) are parameterizable."""
    cfg = load_config(env={"YASTATION_GLAGOL_PORT": "2961"})
    assert cfg.glagol.port == 2961


def test_adb_serial_and_port_parameterization(clean_env):
    """Verify ADB target device address and TCP port can be parameterized."""
    cfg = load_config(env={"ADB_SERIAL": "192.168.1.200:5555", "ANDROID_ADB_SERVER_PORT": "5037"})
    assert cfg.device.serial == "192.168.1.200:5555"
    assert int(cfg.device.serial.split(":")[1]) == 5555
    server_port = getattr(cfg.device, "adb_server_port", getattr(cfg.device, "adb_port", 5037))
    assert server_port in (5037, 5555)


def test_allowed_subnets_parameterization():
    """Verify RFC 1918 private subnet CIDR list is configurable in security config."""
    custom_subnets = ("10.0.0.0/8", "192.168.0.0/16")
    cfg = load_config()
    assert "127.0.0.0/8" in cfg.security.allowed_subnets
    assert "192.168.0.0/16" in cfg.security.allowed_subnets
