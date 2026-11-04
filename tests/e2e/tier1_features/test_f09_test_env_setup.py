"""Tier 1: Feature 9 — Test Environment & Fixture Setup (F09_TEST_ENV_SETUP).

Validates clean test collection, sys.path configuration, fixture isolation,
and safe multi-module imports.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_sys_path_includes_project_root():
    """Verify sys.path includes the project root directory for top-level module resolution."""
    normalized_paths = [str(Path(p).resolve()).lower() for p in sys.path]
    root_str = str(PROJECT_ROOT.resolve()).lower()
    assert root_str in normalized_paths, f"PROJECT_ROOT {root_str} missing from sys.path"


def test_primary_subsystem_modules_importable():
    """Verify all core subsystems import cleanly without raising ModuleNotFoundError."""
    modules = ["access", "agent", "glagol", "max_profile", "multi", "assistant_max", "profiles", "pyadb", "server"]
    for mod_name in modules:
        try:
            mod = __import__(mod_name)
            assert mod is not None
        except Exception as e:
            pytest.fail(f"Failed to import subsystem module '{mod_name}': {e}")


def test_temp_data_dir_isolation(temp_data_dir):
    """Verify temp_data_dir creates an isolated directory containing .data."""
    assert temp_data_dir.is_dir()
    assert (temp_data_dir / ".data").is_dir()

    # Write a test file
    test_file = temp_data_dir / "test_marker.txt"
    test_file.write_text("isolated", encoding="utf-8")
    assert test_file.read_text(encoding="utf-8") == "isolated"


def test_clean_env_fixture_isolation(clean_env):
    """Verify clean_env restores os.environ after test modification."""
    test_key = "PANEL_TEST_EPHEMERAL_KEY"
    os.environ[test_key] = "ephemeral_val"
    assert os.environ.get(test_key) == "ephemeral_val"


def test_clean_env_fixture_reversion():
    """Verify that the key set in the previous test was cleanly reverted."""
    assert os.environ.get("PANEL_TEST_EPHEMERAL_KEY") is None
