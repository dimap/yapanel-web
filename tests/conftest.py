"""Root conftest for yandex-station-web test suite.

Provides automated test isolation, preventing module-level state leakage
(such as server.QUASAR or server.Panel modifications) and restoring os.environ.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


_SERVER_ATTRS = (
    "QUASAR",
    "OVR_DIR",
    "SOUNDS_DIR",
    "BOOT_HOOK_SCRIPT",
    "DATA_DIR",
    "AUTH_FILE",
    "SETTINGS_FILE",
    "AGENT_FILE",
    "GLAGOL_PLATFORM",
    "ROOT",
    "ASSIST_SRC",
)


@pytest.fixture(autouse=True)
def isolate_server_and_env():
    """Autouse fixture restoring server module attributes, methods, and os.environ."""
    env_snapshot = dict(os.environ)

    server_mod = sys.modules.get("server")
    attr_snapshot = {}
    patterns_snapshot = None
    set_volume_snapshot = None
    play_wav_snapshot = None
    do_get_snapshot = None

    if server_mod is not None:
        for attr in _SERVER_ATTRS:
            if hasattr(server_mod, attr):
                attr_snapshot[attr] = getattr(server_mod, attr)

        if hasattr(server_mod, "PATTERNS") and isinstance(server_mod.PATTERNS, dict):
            patterns_snapshot = copy.deepcopy(server_mod.PATTERNS)

        if hasattr(server_mod, "Panel"):
            if hasattr(server_mod.Panel, "set_volume"):
                set_volume_snapshot = server_mod.Panel.set_volume
            if hasattr(server_mod.Panel, "_play_remote_wav"):
                play_wav_snapshot = server_mod.Panel._play_remote_wav

        if hasattr(server_mod, "Handler") and hasattr(server_mod.Handler, "do_GET"):
            do_get_snapshot = server_mod.Handler.do_GET

    yield

    # Restore os.environ
    os.environ.clear()
    os.environ.update(env_snapshot)

    # Restore server globals and methods
    if server_mod is not None:
        for attr, val in attr_snapshot.items():
            setattr(server_mod, attr, val)

        if patterns_snapshot is not None and hasattr(server_mod, "PATTERNS"):
            server_mod.PATTERNS.clear()
            server_mod.PATTERNS.update(patterns_snapshot)

        if hasattr(server_mod, "Panel"):
            if set_volume_snapshot is not None:
                server_mod.Panel.set_volume = set_volume_snapshot
            if play_wav_snapshot is not None:
                server_mod.Panel._play_remote_wav = play_wav_snapshot

        if do_get_snapshot is not None and hasattr(server_mod, "Handler"):
            server_mod.Handler.do_GET = do_get_snapshot
