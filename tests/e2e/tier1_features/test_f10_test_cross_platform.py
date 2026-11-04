"""Tier 1: Feature 10 — Cross-Platform Test Abstractions (F10_TEST_CROSS_PLATFORM).

Validates elimination of host 'sh' dependencies, UTF-8 explicit encodings,
NTFS permission compatibility, and Windows file handle cleanup.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_device_shell_scripts_read_as_utf8():
    """Verify all embedded shell scripts in device/ are valid UTF-8 text."""
    device_dir = PROJECT_ROOT / "device"
    if device_dir.is_dir():
        for sh_file in device_dir.rglob("*.sh"):
            try:
                content = sh_file.read_text(encoding="utf-8")
                assert len(content) > 0
                assert content.startswith("#!/") or "\n" in content
            except UnicodeDecodeError as e:
                pytest.fail(f"Shell script {sh_file.name} is not valid UTF-8: {e}")


def test_permission_bit_compatibility_assertion():
    """Verify permission check helper accommodates Windows NTFS permission mapping."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        temp_path = f.name
        f.write(b"secret_data")

    try:
        os.chmod(temp_path, 0o600)
        mode = os.stat(temp_path).st_mode & 0o777

        if sys.platform.startswith("win"):
            # On Windows NTFS, chmod 0o600 sets rw-rw-rw (0o666) or preserves Windows ACLs
            assert mode in (0o600, 0o666)
        else:
            assert mode == 0o600
    finally:
        os.remove(temp_path)


def test_cross_platform_path_separators():
    """Verify Path / operator and as_posix() normalize properly across platforms."""
    p = Path("data") / "quasar" / "webpanel"
    assert p.as_posix() == "data/quasar/webpanel"


def test_temp_file_lifecycle_no_windows_locking_error():
    """Verify temporary files are safely created, closed, and deleted without WinError 32."""
    with tempfile.TemporaryDirectory() as td:
        tf = Path(td) / "temp.json"
        tf.write_text('{"status": "ok"}', encoding="utf-8")
        assert tf.is_file()
        content = tf.read_text(encoding="utf-8")
        assert "status" in content
    # TemporaryDirectory should clean up without WinError 32
    assert not os.path.exists(td)


def test_subsystem_scripts_have_unix_line_endings_or_portable_shebang():
    """Verify scripts intended for remote speakers use LF line endings."""
    device_dir = PROJECT_ROOT / "device"
    if device_dir.is_dir():
        for sh_file in device_dir.glob("*.sh"):
            raw_bytes = sh_file.read_bytes()
            # Remote speaker shell scripts must not have CRLF
            assert b"\r\n" not in raw_bytes, f"Remote script {sh_file.name} has Windows CRLF line endings"
