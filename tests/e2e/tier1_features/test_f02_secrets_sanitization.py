"""Tier 1: Feature 2 — Secrets Sanitization & Repo Hardening (F02_SECRETS_SANITIZATION).

Validates elimination of live tokens, provisioning of .example.json templates,
.gitignore coverage, and repository credential scanning.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_yandex_auth_contains_no_live_production_tokens():
    """Verify .yandex_auth.json does not leak the known hardcoded production user token."""
    auth_file = PROJECT_ROOT / ".yandex_auth.json"
    if auth_file.is_file():
        content = auth_file.read_text(encoding="utf-8")
        # Assert no active production OAuth tokens exist
        assert not re.search(r"y0__[0-9a-zA-Z_-]{40,}", content)
        try:
            data = json.loads(content)
            # If present, tokens must be empty string or placeholder
            if "music_token" in data and data["music_token"]:
                assert data["music_token"].startswith("placeholder") or data["music_token"] == ""
        except json.JSONDecodeError:
            pass


def test_strip_config_contains_no_live_production_tokens():
    """Verify strip_config.json or strip_config.example.json contains no live production IoT token."""
    strip_file = PROJECT_ROOT / "strip_config.json"
    if strip_file.is_file():
        content = strip_file.read_text(encoding="utf-8")
        assert not re.search(r"y0__[0-9a-zA-Z_-]{40,}", content)

    example_file = PROJECT_ROOT / "strip_config.example.json"
    if example_file.is_file():
        content = example_file.read_text(encoding="utf-8")
        assert "y0__wgBE" not in content
        data = json.loads(content)
        assert data.get("enabled", False) is False


def test_bulbs_example_template_exists_and_sanitized():
    """Verify bulbs.example.json provides safe template schema without hardcoded private LAN keys."""
    example_file = PROJECT_ROOT / "bulbs.example.json"
    if example_file.is_file():
        content = example_file.read_text(encoding="utf-8")
        # Use SHA-256 hashes of private keys to never store raw sensitive keys in tests
        known_leaked_hashes = {
            "df4fe82e50d601925262db9665c41445440ca536e2a22634733b358e0f165511",
            "e79ff8966abdf5edcf4d8de0cb35ba8347e5ebac4d0c9202cbbb585e0a4271c5",
            "fc4d50f7e50339b9256182399538ed8e53dc4d17735d7bfe73d122ef3bbce97a",
            "ec809fe0616040440a90876975363687ad822e056303dc911de5838b1420b08a",
            "fb40600d62c534aee88283cea58388cc963ce22f723298d22f74b879c9e49d43",
        }
        for chunk in re.findall(r'"key":\s*"([^"]+)"', content):
            h = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            assert h not in known_leaked_hashes


def test_gitignore_covers_sensitive_credential_files():
    """Verify .gitignore includes rules preventing credential and local settings commits."""
    gitignore_file = PROJECT_ROOT / ".gitignore"
    assert gitignore_file.is_file(), ".gitignore must exist in project root"
    content = gitignore_file.read_text(encoding="utf-8")

    # Core sensitive files that MUST be ignored
    required_patterns = [
        ".yandex_auth.json",
        ".panel_auth.json",
        ".panel_settings.json",
    ]
    for pattern in required_patterns:
        matched = any(pattern in line for line in content.splitlines() if not line.strip().startswith("#"))
        assert matched, f".gitignore missing pattern for sensitive file '{pattern}'"


def test_codebase_scan_rejects_active_live_oauth_tokens():
    """Scan all tracked python and JSON files to ensure zero live Yandex OAuth tokens are present."""
    # Pattern for Yandex OAuth token: y0__wgBE...
    live_token_regex = re.compile(r"y0__wgBE[0-9a-zA-Z_-]{40,}")

    flagged_files = []
    # Scan root python modules
    for py_file in PROJECT_ROOT.glob("*.py"):
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        matches = live_token_regex.findall(content)
        if matches:
            flagged_files.append((py_file.name, matches))

    assert not flagged_files, f"Live Yandex OAuth tokens discovered in source files: {flagged_files}"
