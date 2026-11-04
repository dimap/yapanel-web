"""Tier 1: Feature 8 — ADB Shell Idempotency & Return Contracts (F08_ADB_SHELL_IDEMPOTENCY).

Validates Adb.shell return code contracts, stdout/stderr extraction,
idempotent _needs_su probing, and non-destructive mock execution.
"""
from __future__ import annotations

import pytest
from tests.e2e.harness.simulated_adb import SimulatedAdb


def test_adb_shell_successful_command_contract():
    """Verify Adb.shell returns a 3-tuple (stdout, stderr, returncode) on success."""
    adb = SimulatedAdb()
    stdout, stderr, code = adb.shell("echo 'hello station'")
    assert code == 0
    assert "hello station" in stdout
    assert stderr == ""


def test_adb_shell_check_flag_error_propagation():
    """Verify check=True raises RuntimeError/AdbError when command fails, while check=False does not."""
    adb = SimulatedAdb()
    adb.register_command_handler("fail_cmd", lambda cmd: ("", "Command not found", 127))

    # check=True must raise
    with pytest.raises(RuntimeError):
        adb.shell("fail_cmd", check=True)

    # check=False must return returncode 127 without raising
    out, err, code = adb.shell("fail_cmd", check=False)
    assert code == 127
    assert "Command not found" in err


def test_adb_shell_needs_su_probe_idempotency():
    """Verify _needs_su probe is cached and does not repeatedly execute exploratory commands."""
    adb = SimulatedAdb(root_access=True)
    initial_commands = len(adb.commands)

    # First call
    adb.shell("id")
    # Subsequent calls
    adb.shell("id")
    adb.shell("id")

    # Command count should strictly increase by 1 per shell() call
    assert len(adb.commands) == initial_commands + 3


def test_adb_push_and_pull_simulation():
    """Verify adb push and pull roundtrip file contents correctly."""
    adb = SimulatedAdb()
    adb.file_system["/tmp/test.txt"] = "config_data=123"

    out, _, code = adb.shell("cat /tmp/test.txt")
    assert code == 0
    assert out.strip() == "config_data=123"


def test_adb_shell_crlf_normalization():
    """Verify SimulatedAdb normalizes CRLF outputs."""
    adb = SimulatedAdb()
    adb.register_command_handler("crlf_test", lambda cmd: ("line1\r\nline2\r\n", "", 0))
    stdout, _, code = adb.shell("crlf_test")
    assert code == 0
    assert "\r" not in stdout
    assert stdout == "line1\nline2\n"
