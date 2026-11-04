"""Tier 2: Boundary & Corner Cases — Configuration Robustness.

Tests handling of corrupted JSON, non-dict payloads, extreme paths,
and type casting failures in configuration subsystem.
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

import config
from tests.e2e.harness.config_contract import load_config


def test_corrupted_json_syntax_fallback(temp_data_dir):
    """Verify corrupted JSON syntax falls back safely without unhandled crashes."""
    bad_json = temp_data_dir / ".panel_settings.json"
    bad_json.write_text("{\n  \"server\": {\"port\": 8888,\n  // missing closing brace", encoding="utf-8")

    cfg = load_config(settings_path=bad_json)
    assert cfg.server.port == 8765
    assert cfg.server.host == "127.0.0.1"


def test_json_root_not_dict(temp_data_dir):
    """Verify non-dict JSON root structures (arrays, primitives) are safely ignored."""
    bad_json = temp_data_dir / ".panel_settings.json"
    for non_dict_val in ([1, 2, 3], "raw string payload", 42, False):
        bad_json.write_text(json.dumps(non_dict_val), encoding="utf-8")
        cfg = load_config(settings_path=bad_json)
        assert cfg.server.port == 8765
        assert cfg.server.host == "127.0.0.1"


def test_json_fields_with_invalid_types(temp_data_dir):
    """Verify sections containing non-dict values do not cause AttributeError."""
    bad_json = temp_data_dir / ".panel_settings.json"
    bad_payload = {
        "server": "not_a_dict_string",
        "device": 12345,
        "party": ["not", "a", "dict"],
        "security": None,
    }
    bad_json.write_text(json.dumps(bad_payload), encoding="utf-8")

    cfg = load_config(settings_path=bad_json)
    assert cfg.server.port == 8765
    assert cfg.server.host == "127.0.0.1"
    assert cfg.glagol.port == 1961


def test_nonexistent_settings_file_uses_defaults():
    """Verify non-existent path loads default configuration without error."""
    missing_path = Path("this_file_definitely_does_not_exist_xyz123.json")
    cfg = load_config(settings_path=missing_path)
    assert cfg.server.port == 8765
    assert cfg.server.host == "127.0.0.1"


def test_deeply_nested_data_dir_resolution(temp_data_dir):
    """Verify deeply nested data_dir path resolution."""
    deep_path = temp_data_dir / "level1" / "level2" / "level3" / "data"
    cfg = load_config(env={"PANEL_DATA_DIR": str(deep_path)})
    assert Path(cfg.server.data_dir).resolve() == deep_path.resolve()
    assert (cfg.server.data_dir / ".tls").name == ".tls"


def test_config_to_dict_roundtrip():
    """Verify cfg.to_dict() returns valid JSON-serializable dictionary."""
    cfg = load_config()
    dumped = cfg.to_dict()
    assert isinstance(dumped, dict)
    assert "server" in dumped
    assert "device" in dumped
    assert "glagol" in dumped
    assert "security" in dumped
    assert "remote" in dumped
    assert "party" in dumped
    assert "assistant" in dumped

    # Verify standard json serialization succeeds
    serialized = json.dumps(dumped)
    assert len(serialized) > 0
    deserialized = json.loads(serialized)
    assert deserialized["server"]["port"] == cfg.server.port


def test_config_strict_mode_error_reporting(temp_data_dir):
    """Verify strict=True raises ConfigError on corrupted JSON."""
    bad_json = temp_data_dir / ".panel_settings.json"
    bad_json.write_text("INVALID_JSON", encoding="utf-8")

    with pytest.raises(config.ConfigError):
        config.PanelConfig.load(settings_path=bad_json, strict=True)
