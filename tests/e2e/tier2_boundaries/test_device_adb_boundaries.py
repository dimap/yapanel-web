"""Tier 2: Boundary & Corner Cases — Device & ADB Boundaries.

Tests extreme command payloads, CRLF permutations, large buffer processing,
metacharacter safety, and file push/pull boundaries.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from tests.e2e.harness.simulated_adb import SimulatedAdb


def test_empty_command_execution():
    """Verify empty string command execution does not raise and completes cleanly."""
    adb = SimulatedAdb()
    stdout, stderr, code = adb.shell("")
    assert code == 0
    assert isinstance(stdout, str)


def test_large_stdout_buffering():
    """Verify handling of large output buffers (10,000 lines)."""
    adb = SimulatedAdb()
    large_payload = "line_data_test_entry_123456789\r\n" * 10_000
    adb.register_command_handler("large_cmd", lambda cmd: (large_payload, "", 0))

    stdout, _, code = adb.shell("large_cmd")
    assert code == 0
    assert "\r" not in stdout
    lines = stdout.splitlines()
    assert len(lines) == 10_000


def test_mixed_crlf_and_lf_handling():
    """Verify arbitrary mixtures of CRLF and LF are normalized strictly to LF."""
    adb = SimulatedAdb()
    mixed = "part1\r\npart2\npart3\r\npart4\n"
    adb.register_command_handler("mixed_cmd", lambda cmd: (mixed, "", 0))

    stdout, _, code = adb.shell("mixed_cmd")
    assert code == 0
    assert "\r" not in stdout
    assert stdout == "part1\npart2\npart3\npart4\n"


def test_shell_metacharacters_handling():
    """Verify commands containing pipes, redirection, and variable substitutions execute cleanly."""
    adb = SimulatedAdb()
    complex_cmds = [
        "cat /proc/cpuinfo | grep 'Hardware' || echo 'unknown'",
        "echo \"hello 'world'\" > /dev/null",
        "for i in $(seq 1 3); do echo $i; done",
    ]
    for cmd in complex_cmds:
        stdout, stderr, code = adb.shell(cmd)
        assert code == 0
        assert isinstance(stdout, str)


def test_adb_custom_handler_dispatch():
    """Verify command prefix routing dispatches to custom handlers properly."""
    adb = SimulatedAdb()
    adb.register_command_handler("getprop ro.custom.", lambda cmd: ("custom_value\n", "", 0))

    # Matching prefix
    out1, _, code1 = adb.shell("getprop ro.custom.key")
    assert code1 == 0
    assert out1.strip() == "custom_value"

    # Default fallback
    out2, _, code2 = adb.shell("getprop ro.product.model")
    assert code2 == 0
    assert "Yandex Station" in out2


def test_adb_push_missing_local_file(temp_data_dir):
    """Verify pushing non-existent local file is safely handled."""
    adb = SimulatedAdb()
    missing_file = temp_data_dir / "non_existent_file.bin"
    # Should not raise exception
    res = adb.push(missing_file, "/data/remote.bin")
    # File was not on filesystem, so remote file system should not have it
    assert "/data/remote.bin" not in adb.file_system
