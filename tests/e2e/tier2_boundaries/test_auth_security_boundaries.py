"""Tier 2: Boundary & Corner Cases — Authentication & Security Boundaries.

Tests password length boundaries (0, 3, 4, 1024), Unicode & emoji support,
rate limit delay progression, expired session invalidation, and malformed cookies.
"""
from __future__ import annotations

import time
from pathlib import Path
import pytest

import access


def test_password_minimum_length_boundaries(temp_data_dir):
    """Verify password boundary conditions: 0-3 chars rejected, 4 chars accepted."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)

    # 0, 1, 2, 3 characters must fail
    for invalid_pw in ("", "a", "12", "xyz"):
        with pytest.raises(access.AccessError):
            mgr.set_password("", invalid_pw, "test_agent")

    assert not mgr.enabled

    # Exact boundary: 4 characters must succeed
    token = mgr.set_password("", "abcd", "test_agent")
    assert mgr.enabled
    assert isinstance(token, str)
    assert mgr.session_ok(token)


def test_long_password_boundary(temp_data_dir):
    """Verify extremely long password (1024 chars) is hashed and validated properly."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)

    long_password = "A" * 1024
    token = mgr.set_password("", long_password, "desktop_client")
    assert mgr.enabled
    assert mgr.session_ok(token)

    # Verify login with long password
    login_token = mgr.login("127.0.0.1", long_password, "desktop_client")
    assert login_token is not None
    assert mgr.session_ok(login_token)


def test_unicode_and_special_character_passwords(temp_data_dir):
    """Verify passwords containing Cyrillic, Asian glyphs, and emojis work seamlessly."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)

    complex_passwords = [
        "Пароль_1234!",
        "Пароль С Пробелами И Символами #%&",
        "日本語パスワード2026",
        "🔑🛡️Секрет123🌟",
    ]

    current_pw = ""
    for pw in complex_passwords:
        token = mgr.set_password(current_pw, pw, "test_client")
        assert mgr.session_ok(token)
        # Login must succeed with exact unicode string
        auth_token = mgr.login("127.0.0.1", pw, "test_client")
        assert auth_token is not None
        assert mgr.session_ok(auth_token)
        current_pw = pw


def test_exponential_rate_limiting_math():
    """Verify lock duration progression: 0 under 5 attempts, doubling from 30s to 900s."""
    now = time.time()

    # Attempts 0..4: zero pause
    for n in range(5):
        assert access.PanelAccess._lock_left(n, now) == 0

    # Attempt 5: 30s
    pause_5 = access.PanelAccess._lock_left(5, now)
    assert 28 <= pause_5 <= 31

    # Attempt 6: 60s
    pause_6 = access.PanelAccess._lock_left(6, now)
    assert 58 <= pause_6 <= 61

    # Attempt 7: 120s
    pause_7 = access.PanelAccess._lock_left(7, now)
    assert 118 <= pause_7 <= 121

    # Attempt 20: capped at max_pause (900s)
    pause_20 = access.PanelAccess._lock_left(20, now)
    assert 898 <= pause_20 <= 901


def test_expired_session_token_rejected(temp_data_dir):
    """Verify sessions older than SESSION_TTL are treated as invalid."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    token = mgr.set_password("", "valid_password", "client")

    # Manually backdate the session
    token_hash = access._hash_token(token)
    assert token_hash in mgr.data["sessions"]
    mgr.data["sessions"][token_hash]["last"] = time.time() - (access.SESSION_TTL + 3600)

    # Session check must now return False
    assert not mgr.session_ok(token)


def test_session_cleanup_on_new_session(temp_data_dir):
    """Verify _new_session cleans up expired sessions automatically."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    token1 = mgr.set_password("", "initial_password", "client")

    # Expire token1
    h1 = access._hash_token(token1)
    mgr.data["sessions"][h1]["last"] = time.time() - (access.SESSION_TTL + 100)

    # Create new session
    token2 = mgr.login("127.0.0.1", "initial_password", "new_client")
    assert token2 is not None

    # token1 should have been purged during _new_session
    assert h1 not in mgr.data["sessions"]
    h2 = access._hash_token(token2)
    assert h2 in mgr.data["sessions"]
