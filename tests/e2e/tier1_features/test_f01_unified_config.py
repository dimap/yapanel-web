"""Tier 1: Feature 1 — Unified Centralized Configuration (F01_UNIFIED_CONFIG).

Validates @dataclass configuration loading, 4-tier precedence, type safety,
and safe defaults across all subsystems.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.e2e.harness.config_contract import (
    DeviceConfig,
    GlagolConfig,
    PanelConfig,
    PartyConfig,
    RemotePathsConfig,
    SecurityConfig,
    ServerConfig,
    load_config,
)


def test_config_loads_safe_defaults():
    """Verify default instantiation yields safe, valid defaults across all subconfigs."""
    cfg = load_config(settings_path=Path("non_existent_settings.json"), env={}, cli_args={})
    assert hasattr(cfg, "server")
    assert hasattr(cfg, "device")
    assert hasattr(cfg, "glagol")
    assert hasattr(cfg, "security")
    assert hasattr(cfg, "remote")
    assert hasattr(cfg, "party")

    # Server safe defaults
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8765
    assert cfg.server.https_port == 8766
    assert isinstance(cfg.server.data_dir, Path)
    cookie = getattr(cfg.security, "cookie_name", getattr(cfg.server, "session_cookie", None))
    assert cookie == "yl2_session"

    # Device safe defaults
    dev_port = getattr(cfg.device, "adb_server_port", getattr(cfg.device, "adb_port", None))
    assert dev_port in (5037, 5555)
    dev_timeout = getattr(cfg.device, "shell_timeout", getattr(cfg.device, "adb_timeout", None))
    assert dev_timeout == 15.0

    # Glagol safe defaults
    assert cfg.glagol.port == 1961

    # Security safe defaults
    pbkdf2_iter = getattr(cfg.security, "pbkdf2_iter", getattr(cfg.security, "pbkdf2_iterations", 0))
    assert pbkdf2_iter >= 240_000
    min_pw = getattr(cfg.security, "min_password_len", getattr(cfg.security, "min_password_length", 0))
    assert min_pw >= 4
    ttl = getattr(cfg.security, "session_ttl_seconds", getattr(cfg.security, "session_ttl_sec", 0))
    assert ttl == 7_776_000
    assert "127.0.0.0/8" in cfg.security.allowed_subnets

    # Remote paths safe defaults
    assert cfg.remote.web_dir == "/data/quasar/webpanel"
    assert cfg.remote.quasar_dir == "/system/vendor/quasar"

    # Party safe defaults (decoupled/disabled by default)
    auto_start = getattr(cfg.party, "auto_start", getattr(cfg.party, "enabled", False))
    assert auto_start is False


def test_config_precedence_env_overrides_defaults(clean_env, temp_data_dir):
    """Verify environment variables override defaults (Tier 2 in precedence)."""
    target_data_dir = str(temp_data_dir / "custom_data")
    env_override = {
        "PANEL_HOST": "0.0.0.0",
        "PANEL_PORT": "9090",
        "PANEL_HTTPS_PORT": "9091",
        "PANEL_DATA_DIR": target_data_dir,
        "ADB_SERIAL": "192.168.1.150:5555",
        "YASTATION_GLAGOL_PORT": "1962",
        "PARTY_AUTO_START": "true",
        "PARTY_ENABLED": "true",
    }
    cfg = load_config(settings_path=Path("non_existent.json"), env=env_override, cli_args={})

    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9090
    assert cfg.server.https_port == 9091
    assert Path(cfg.server.data_dir).resolve() == Path(target_data_dir).resolve()
    assert cfg.device.serial == "192.168.1.150:5555"
    assert cfg.glagol.port == 1962
    auto_start = getattr(cfg.party, "auto_start", getattr(cfg.party, "enabled", False))
    assert auto_start is True


def test_config_precedence_cli_overrides_env_and_defaults(clean_env):
    """Verify CLI arguments override environment variables and defaults (Tier 1 in precedence)."""
    env = {
        "PANEL_HOST": "192.168.1.20",
        "PANEL_PORT": "7000",
    }
    cli_args = {
        "host": "10.0.0.5",
        "port": 8888,
        "serial": "device-cli-override",
    }
    cfg = load_config(settings_path=Path("non_existent.json"), env=env, cli_args=cli_args)

    # CLI win over ENV
    assert cfg.server.host == "10.0.0.5"
    assert cfg.server.port == 8888
    assert cfg.device.serial == "device-cli-override"


def test_config_json_store_loading(temp_data_dir):
    """Verify configuration loads cleanly from persistent JSON settings file."""
    settings_file = temp_data_dir / ".panel_settings.json"
    settings_content = {
        "server": {"port": 9999, "host": "127.0.0.2"},
        "device": "192.168.1.50:5555",
        "party": {"auto_start": True, "enabled": True},
    }
    settings_file.write_text(json.dumps(settings_content), encoding="utf-8")

    cfg = load_config(settings_path=settings_file, env={}, cli_args={})

    assert cfg.server.port == 9999
    assert cfg.server.host == "127.0.0.2"
    assert cfg.device.serial == "192.168.1.50:5555"
    auto_start = getattr(cfg.party, "auto_start", getattr(cfg.party, "enabled", False))
    assert auto_start is True


def test_config_dataclass_type_integrity():
    """Verify that all configuration structures are pure dataclasses with standard library types."""
    cfg = load_config(settings_path=Path("non_existent.json"), env={}, cli_args={})
    assert type(cfg.server).__name__ == "ServerConfig"
    assert type(cfg.device).__name__ == "DeviceConfig"
    assert type(cfg.glagol).__name__ == "GlagolConfig"
    assert type(cfg.security).__name__ == "SecurityConfig"
    assert type(cfg.remote).__name__ == "RemotePathsConfig"
    assert type(cfg.party).__name__ == "PartyConfig"

    # Verify no external non-stdlib classes leaked into dataclass
    for sub in (cfg.server, cfg.device, cfg.glagol, cfg.security, cfg.remote, cfg.party):
        for field_name, field_val in sub.__dict__.items():
            assert isinstance(field_val, (str, int, float, bool, Path, tuple, bytes, type(None))), (
                f"Non-standard type {type(field_val)} in {sub.__class__.__name__}.{field_name}"
            )


def test_config_port_range_validation():
    """Verify invalid port numbers trigger ValueError in schema validation."""
    from tests.e2e.harness import config_contract
    with pytest.raises(ValueError):
        config_contract.ServerConfig(port=-1)

    with pytest.raises(ValueError):
        config_contract.ServerConfig(port=70000)

    # Valid boundaries
    assert config_contract.ServerConfig(port=0).port == 0
    assert config_contract.ServerConfig(port=65535).port == 65535
