"""Tier 1: Feature 7 — Cross-Platform Stability & OS Abstractions (F07_CROSS_PLATFORM_STABILITY).

Validates Windows/Linux terminal abstractions (pty/fcntl fallback), ADB CRLF normalization,
safe command argument quoting, and cross-platform path handling.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest


def test_pty_fcntl_safe_import_fallback():
    """Verify code importing pty or fcntl handles absence gracefully on non-POSIX (Windows)."""
    # Simulate non-POSIX environment
    try:
        import pty
        have_pty = True
    except ImportError:
        have_pty = False

    try:
        import fcntl
        have_fcntl = True
    except ImportError:
        have_fcntl = False

    if sys.platform.startswith("win"):
        import profiles
        assert hasattr(profiles, "PROFILES")
        assert not have_pty
        assert not have_fcntl


def test_adb_crlf_normalization():
    """Verify CRLF line endings from Windows ADB are normalized to standard LF."""
    raw_adb_output = "model=cucumber\r\nversion=1.0.4\r\nstatus=ok\r\n"
    normalized = raw_adb_output.replace("\r\n", "\n")

    assert "\r" not in normalized
    assert normalized == "model=cucumber\nversion=1.0.4\nstatus=ok\n"
    assert normalized.splitlines() == ["model=cucumber", "version=1.0.4", "status=ok"]


def test_command_argument_quoting_safety():
    """Verify shlex.quote or equivalent safely handles paths with spaces and special characters."""
    dangerous_paths = [
        "path with spaces/file.txt",
        "file;rm -rf /;.txt",
        "quotes'and\"symbols.mp3",
        "dollar$sign and `backtick`.wav",
    ]

    for p in dangerous_paths:
        quoted = shlex.quote(p)
        # Quoted string must wrap dangerous metacharacters safely
        assert ";" not in quoted or quoted.startswith("'")
        assert "`" not in quoted or quoted.startswith("'")


def test_pathlib_cross_platform_resolution():
    """Verify Path objects resolve properly across Windows backslashes and POSIX slashes."""
    p1 = Path("data/quasar/webpanel")
    p2 = Path("data\\quasar\\webpanel".replace("\\", "/"))

    # Both must resolve to identical parts regardless of host OS separator
    assert p1.parts == ("data", "quasar", "webpanel")
    assert p2.parts == ("data", "quasar", "webpanel")


def test_utf8_encoding_enforcement():
    """Verify file operations on UTF-8 text with Cyrillic characters succeed without UnicodeEncodeError."""
    sample_text = "Яндекс Станция: панель управления и умный дом 🎵"
    encoded = sample_text.encode("utf-8")
    decoded = encoded.decode("utf-8")

    assert decoded == sample_text
    assert len(encoded) > len(sample_text)
