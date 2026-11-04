"""Pytest root configuration and fixtures for E2E test suite."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Generator

import pytest

# Ensure project root is at head of sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.e2e.harness.simulated_adb import SimulatedAdb


@pytest.fixture
def clean_env() -> Generator[dict, None, None]:
    """Isolate environment variables for each test."""
    original = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(original)


@pytest.fixture
def temp_data_dir() -> Generator[Path, None, None]:
    """Provide an isolated temporary directory for config/settings/logs."""
    tmp = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp.name)
    (tmp_path / ".data").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    tmp.cleanup()


@pytest.fixture
def simulated_adb() -> SimulatedAdb:
    """Provide a fresh simulated ADB fixture."""
    return SimulatedAdb(serial="127.0.0.1:5555", root_access=True)


@pytest.fixture
def thread_tracker() -> Generator[None, None, None]:
    """Capture live threads before and after test."""
    before = [t for t in threading.enumerate() if t.is_alive()]
    yield
    # No assertion here by default, but available for tests that request it
