"""Tier 1: Feature 4 — Remote & Local Paths Configuration (F04_REMOTE_PATHS_CONFIG).

Validates parameterization of remote speaker directories (WEB_DIR, QUASAR_DIR, YAM_DIR)
and local host persistent storage directory (DATA_DIR).
"""
from __future__ import annotations

from pathlib import Path
import pytest
from tests.e2e.harness.config_contract import RemotePathsConfig, load_config


def test_remote_web_dir_parameterization(clean_env):
    """Verify speaker webpanel installation directory is parameterizable."""
    cfg = load_config(env={"YASTATION_REMOTE_WEB_DIR": "/data/custom/webpanel", "YASTATION_WEB_DIR": "/data/custom/webpanel"})
    assert cfg.remote.web_dir == "/data/custom/webpanel"


def test_remote_quasar_dir_parameterization(clean_env):
    """Verify system quasar vendor directory is parameterizable."""
    cfg = load_config(env={"YASTATION_QUASAR_DIR": "/system/quasar_alt"})
    assert cfg.remote.quasar_dir == "/system/quasar_alt"


def test_remote_yam_directories_defaults():
    """Verify YAM package manager paths are configured with defaults."""
    cfg = load_config()
    assert cfg.remote.yam_dir == "/data/quazar/yam"
    assert cfg.remote.yam2_root == "/data/yam"


def test_local_data_dir_parameterization(clean_env, temp_data_dir):
    """Verify host data storage directory is resolved to a Path object."""
    custom_dir = str(temp_data_dir / "station_test_data")
    cfg = load_config(env={"PANEL_DATA_DIR": custom_dir})
    assert isinstance(cfg.server.data_dir, Path)
    assert Path(cfg.server.data_dir).resolve() == Path(custom_dir).resolve()


def test_derived_remote_service_paths():
    """Verify assistant, alarm, and visualizer paths conform to expected defaults."""
    cfg = load_config()
    assert "/alarm" in cfg.remote.alarm_dir
    assert "/assistant" in cfg.remote.assist_dir
    assert "/viz" in cfg.remote.viz_dir
