"""
Adversarial Stress & Verification Harness for Milestone 2 (m2_challenger_2).

Empirically challenges and stress-tests:
- F06_UPDATER_RESTORE:
    * PanelUpdater multi-threaded concurrency lock contention.
    * Guaranteed lock release in finally: on simulated download/check/network exceptions.
    * Platform dispatch (docker, android, git, zip) and update states.
- F07_CROSS_PLATFORM_STABILITY:
    * CRLF, CRCRLF, stray CR, and noise envelope normalization in normalize_adb_output() and profiles.py:shell().
    * Command injection and metacharacter prevention across shlex.quote() wrapped callers.
    * PtyProc instantiation, stdio communication, resize safety, and process termination on Windows.
- F08_ADB_SHELL_IDEMPOTENCY:
    * Adb.shell() mock fidelity: guarantees exactly 1 subprocess call per command without unsolicited probes.
    * probe_needs_su() caching idempotency and local execution bypass.

Executable directly via:
    python tests/test_m2_adversarial_challenger.py
Or via pytest:
    python -m pytest tests/test_m2_adversarial_challenger.py -v
"""
import concurrent.futures
import io
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Ensure server, profiles, max_profile, access, config are on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import server
from server import Adb, AdbError, PanelUpdater, PtyProc, PtyStdin, normalize_adb_output


# ============================================================================
# 1. F06: PANEL UPDATER CONCURRENCY LOCK & EXCEPTION RECOVERY
# ============================================================================

class TestPanelUpdaterAdversarial:
    """Rigorous stress-testing of PanelUpdater concurrency and lock lifecycle."""

    def test_concurrent_update_lock_contention_ten_threads(self):
        """10 threads race to call update(). Exactly 1 acquires the lock, 9 fail with
        'обновление уже идёт', and the lock is cleanly released afterwards."""
        updater = PanelUpdater(restart=Mock())

        # Mock check() to advertise an available update
        updater.check = Mock(return_value={"update": True, "latest": "9.9.9", "install": "zip"})

        started_barrier = threading.Barrier(10)
        hold_in_zip = threading.Event()
        zip_entered = threading.Event()

        def slow_from_zip(version):
            zip_entered.set()
            # Hold lock until main test thread signals release
            hold_in_zip.wait(timeout=5.0)

        updater._from_zip = slow_from_zip

        results = []
        errors = []

        def worker():
            started_barrier.wait()
            try:
                res = updater.update()
                results.append(res)
            except AdbError as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"UNEXPECTED: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()

        # Wait until the winning thread enters _from_zip (holding the lock)
        assert zip_entered.wait(timeout=3.0), "Winning thread failed to enter _from_zip"

        # Ensure all 9 contending threads have reached contention
        deadline = time.time() + 5.0
        while len(errors) < 9 and time.time() < deadline:
            time.sleep(0.01)

        # Allow winning thread to finish
        hold_in_zip.set()

        for t in threads:
            t.join(timeout=5.0)

        # Assertions
        assert len(results) == 1, f"Expected exactly 1 winner, got {len(results)}: {results}"
        assert results[0] == {"ok": True, "version": "9.9.9", "install": "zip"}
        assert len(errors) == 9, f"Expected exactly 9 blocked callers, got {len(errors)}: {errors}"
        for err in errors:
            assert "обновление уже идёт" in err, f"Unexpected error message: {err}"

        # Crucial check: Lock MUST be released
        assert not updater.lock.locked(), "Updater lock was not released after completion"

        # Subsequent call must immediately succeed
        updater._from_zip = Mock()
        next_res = updater.update()
        assert next_res["ok"] is True
        assert not updater.lock.locked()

    def test_lock_release_on_simulated_download_exception(self):
        """Simulate an unhandled network error during zip download.
        Lock must be released in finally:, and subsequent update must succeed."""
        updater = PanelUpdater(restart=Mock())
        updater.check = Mock(return_value={"update": True, "latest": "9.9.9", "install": "zip"})
        updater._from_zip = Mock(side_effect=urllib.error.URLError("Connection reset by peer"))

        with pytest.raises(urllib.error.URLError):
            updater.update()

        assert not updater.lock.locked(), "Lock remained locked after download exception!"

        # Re-try with restored network: must acquire and succeed
        updater._from_zip = Mock(return_value=None)
        res = updater.update()
        assert res["ok"] is True
        assert not updater.lock.locked()

    def test_lock_release_on_bad_zip_corruption(self):
        """Simulate corrupt ZIP payload during extraction.
        Lock must be released in finally:, not deadlocked."""
        updater = PanelUpdater(restart=Mock())
        updater.check = Mock(return_value={"update": True, "latest": "9.9.9", "install": "zip"})
        updater._from_zip = Mock(side_effect=zipfile.BadZipFile("Truncated zip file"))

        with pytest.raises(zipfile.BadZipFile):
            updater.update()

        assert not updater.lock.locked(), "Lock remained locked after BadZipFile!"

    def test_lock_release_on_check_rate_limit_or_http_error(self):
        """Simulate GitHub API 403 / 404 / 500 error during check.
        Lock must be released in finally:, not deadlocked."""
        updater = PanelUpdater(restart=Mock())
        updater.check = Mock(return_value={"error": "GitHub ответил 403", "latest": ""})

        with pytest.raises(AdbError) as exc_info:
            updater.update()

        assert "GitHub ответил 403" in str(exc_info.value)
        assert not updater.lock.locked(), "Lock remained locked after API check error!"

    def test_lock_release_on_no_update_available(self):
        """When check reports no newer version available, raise AdbError and release lock."""
        updater = PanelUpdater(restart=Mock())
        updater.check = Mock(return_value={"update": False, "latest": "0.1.0"})

        with pytest.raises(AdbError) as exc_info:
            updater.update()

        assert "уже установлена последняя версия" in str(exc_info.value)
        assert not updater.lock.locked(), "Lock remained locked when no update was needed!"

    def test_lock_release_on_docker_and_android_installs(self):
        """Docker and Android environments must provide instructions, raise AdbError, and release lock."""
        updater = PanelUpdater(restart=Mock())

        # Docker
        updater.check = Mock(return_value={"update": True, "latest": "2.0.0", "install": "docker"})
        with pytest.raises(AdbError) as exc_docker:
            updater.update()
        assert "в Docker обновите образ" in str(exc_docker.value)
        assert not updater.lock.locked(), "Lock remained locked after docker instruction!"

        # Android
        updater.check = Mock(return_value={"update": True, "latest": "2.0.0", "install": "android"})
        with pytest.raises(AdbError) as exc_android:
            updater.update()
        assert "в приложении для Android установите новый APK" in str(exc_android.value)
        assert not updater.lock.locked(), "Lock remained locked after android instruction!"

    def test_lock_release_on_git_pull_failure(self):
        """When git pull fails with non-zero exit code, raise AdbError and release lock."""
        updater = PanelUpdater(restart=Mock())
        updater.check = Mock(return_value={"update": True, "latest": "2.0.0", "install": "git"})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "pull"],
                returncode=128,
                stdout="",
                stderr="fatal: refusing to merge unrelated histories"
            )
            with pytest.raises(AdbError) as exc_git:
                updater.update()
            assert "git pull не удался" in str(exc_git.value)
            assert not updater.lock.locked(), "Lock remained locked after git failure!"


# ============================================================================
# 2. F07: CRLF / CRCRLF NORMALIZATION & ADVERSARIAL TRANSPORT CORRUPTIONS
# ============================================================================

class TestAdbOutputNormalizationAdversarial:
    """Stress tests for CRLF, CRCRLF, stray CR, and noise envelope parsing."""

    @pytest.mark.parametrize("raw_input, expected_output", [
        # Canonical UNIX LF unchanged
        ("line1\nline2\nline3\n", "line1\nline2\nline3\n"),
        # Standard Windows CRLF normalized to UNIX LF
        ("line1\r\nline2\r\nline3\r\n", "line1\nline2\nline3\n"),
        # Double CRCRLF normalized to UNIX LF
        ("line1\r\r\nline2\r\r\nline3\r\r\n", "line1\nline2\nline3\n"),
        # Extreme quadruple CRCRCRCRLF normalized
        ("line1\r\r\r\r\nline2\r\r\r\r\n", "line1\nline2\n"),
        # Stray CR progress ticks (without LF) stripped cleanly
        ("0%\r50%\r100%\n", "0%50%100%\n"),
        # Mixed line endings within single payload
        ("head\r\r\nbody1\r\nbody2\nbody3\r\r\r\ntail\r", "head\nbody1\nbody2\nbody3\ntail"),
        # Empty and edge cases
        ("", ""),
        ("\r\r\r", ""),
        ("\r\n", "\n"),
        ("\r\r\n", "\n"),
        # Unicode Cyrillic and emojis with Windows CRLF
        ("Яндекс Станция 2 🎵\r\nУмный дом работает\r\r\n", "Яндекс Станция 2 🎵\nУмный дом работает\n"),
    ])
    def test_normalize_adb_output_matrix(self, raw_input, expected_output):
        """normalize_adb_output must strip all carriage returns before newlines and stray CRs."""
        result = normalize_adb_output(raw_input)
        assert result == expected_output
        assert "\r" not in result

    @pytest.mark.parametrize("payload", [
        "simple_text",
        "file with spaces.txt",
        "path; rm -rf /",
        "$(whoami)",
        "`cat /etc/passwd`",
        "hello && echo pwned",
        "arg'with'quotes",
        'arg"double"quotes',
        "multi\nline\npayload",
        "русский текст со спецсимволами: !@#$%^&*()",
    ])
    def test_shlex_quote_integrity(self, payload):
        """shlex.quote must wrap any dangerous characters into a single safe POSIX token."""
        quoted = shlex.quote(payload)
        tokens = shlex.split(quoted)
        assert len(tokens) == 1, f"Quoting failed to preserve single token for {payload!r}: {tokens}"
        assert tokens[0] == payload, f"Token mismatch: {tokens[0]!r} != {payload!r}"

    def test_server_install_override_quotes_paths(self):
        """server.py:Panel._install_override quotes custom, target, and orig paths."""
        mock_adb = Mock()
        mock_adb.shell = Mock(return_value=("", "", 0))
        mock_adb.push = Mock()

        panel = server.Panel.__new__(server.Panel)
        panel.adb = mock_adb
        panel._ensure_hook = Mock()

        malicious_custom = "/tmp/custom dir; rm -rf /;.bin"
        malicious_target = "/tmp/target 'quotes' \"double\"/target.bin"
        malicious_orig = "/tmp/orig 'quotes'/orig.bin"

        panel._install_override(b"fake_data", malicious_custom, malicious_target, orig=malicious_orig)

        # Inspect all shell calls to ensure paths were quoted
        assert mock_adb.shell.call_count == 2
        call1_cmd = mock_adb.shell.call_args_list[0][0][0]
        call2_cmd = mock_adb.shell.call_args_list[1][0][0]

        # Call 1: mkdir -p and copy orig
        assert shlex.quote(posixpath.dirname(malicious_custom)) in call1_cmd
        assert shlex.quote(malicious_orig) in call1_cmd
        assert shlex.quote(malicious_target) in call1_cmd

        # Call 2: mount --bind
        assert shlex.quote(malicious_custom) in call2_cmd
        assert shlex.quote(malicious_target) in call2_cmd

        # Verify raw injection was neutralized into quotes
        assert "rm -rf /;" not in call1_cmd.replace(shlex.quote(posixpath.dirname(malicious_custom)), "")

    def test_max_play_remote_wav_quotes_paths(self):
        """max_profile.py:max_play_remote_wav quotes audio path and play_log."""
        import profiles
        from test_profiles import fresh_server

        mock_adb = Mock()
        mock_adb.shell = Mock(return_value=("", "", 0))

        # Install Max profile onto fresh server
        s = fresh_server()
        profiles.install(s, "max")

        panel = Mock()
        panel.adb = mock_adb
        panel.stop_sound = Mock()
        panel.detach = Mock()

        # Extract the installed max_play_remote_wav from s.Panel
        play_fn = s.Panel._play_remote_wav

        # Call with mock panel and malicious path
        malicious_path = "/tmp/audio file; reboot #.wav"
        play_fn(panel, malicious_path)

        # Verify detach was called with safely quoted command
        assert panel.detach.called
        call_cmd = panel.detach.call_args[0][1]
        assert f"aplay -q {shlex.quote(malicious_path)}" in call_cmd


# ============================================================================
# 4. F08: ADB SHELL IDEMPOTENCY & MOCK FIDELITY
# ============================================================================

class TestAdbShellIdempotencyAdversarial:
    """Stress tests verifying Adb.shell executes exactly 1 command without unsolicited probing."""

    def test_adb_shell_default_makes_strictly_one_subprocess_call(self):
        """A standard adb.shell() call must execute exactly 1 subprocess.run call.
        No exploratory 'id' probe must consume mock return values."""
        adb = Adb(adb_path="adb", serial="192.168.1.150:5555")
        assert adb._needs_su is False, "_needs_su must default to False for idempotency"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"Linux station 4.9.113\n@@webpanel-rc=0\n",
                stderr=b""
            )

            out, err, rc = adb.shell("uname -a")

            assert rc == 0
            assert "Linux station" in out
            assert mock_run.call_count == 1, (
                f"Expected exactly 1 subprocess.run invocation, but got {mock_run.call_count}!"
            )

            # Verify the command executed was the requested shell command, not 'id' probe
            executed_args = mock_run.call_args[0][0]
            assert "id" not in executed_args
            assert "uname -a" in executed_args[-1]

    def test_adb_probe_needs_su_caching_idempotency(self):
        """When _needs_su is explicitly None, probe_needs_su() probes once and caches the result.
        Subsequent shell calls must not re-probe."""
        adb = Adb(adb_path="adb", serial="192.168.1.150:5555", needs_su=None)
        assert adb._needs_su is None

        with patch("subprocess.run") as mock_run:
            # First call: probe returns non-root (needs su)
            mock_run.side_effect = [
                # probe_needs_su call
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"uid=1000(shell)\n", stderr=b""),
                # command 1 call
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"cmd1_out\n@@webpanel-rc=0\n", stderr=b""),
                # command 2 call
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"cmd2_out\n@@webpanel-rc=0\n", stderr=b""),
            ]

            # First shell execution: 1 probe + 1 command = 2 calls
            out1, _, rc1 = adb.shell("cmd1")
            assert rc1 == 0
            assert adb._needs_su is True
            assert mock_run.call_count == 2

            # Second shell execution: exactly 1 command call (0 probes!)
            out2, _, rc2 = adb.shell("cmd2")
            assert rc2 == 0
            assert mock_run.call_count == 3, (
                f"Expected 3 total subprocess calls (1 probe + 2 commands), got {mock_run.call_count}"
            )

    def test_adb_is_local_never_probes(self):
        """When adb is local (is_local=True), probe_needs_su returns False immediately with 0 subprocess calls."""
        adb = Adb(adb_path="local", serial="local")
        assert adb.is_local is True

        with patch("subprocess.run") as mock_run:
            needs_su = adb.probe_needs_su()
            assert needs_su is False
            assert mock_run.call_count == 0, "probe_needs_su on local must not call subprocess.run!"


# ============================================================================
# 5. F07: PTYPROC WINDOWS FALLBACK & LIFECYCLE
# ============================================================================

class TestPtyProcWindowsFallbackAdversarial:
    """Stress tests verifying PtyProc functionality on current host without pty/fcntl crashes."""

    def test_ptyproc_instantiation_and_stdio_execution(self):
        """PtyProc must instantiate cleanly on Windows and communicate via stdio."""
        test_msg = "PTY_TEST_OK_12345"
        cmd = f"echo {test_msg}"

        proc = PtyProc(cmd)
        try:
            assert proc.proc is not None
            # Read stdout
            line = proc.stdout.readline()
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            assert test_msg in line, f"Expected {test_msg} in output, got: {line!r}"

            rc = proc.wait(timeout=5.0)
            assert rc == 0
            assert proc.returncode == 0
        finally:
            proc.close()

    def test_ptyproc_resize_is_safe_noop_on_windows(self):
        """PtyProc.resize() must not raise AttributeError or OSError on Windows."""
        cmd = "echo resize_test"
        proc = PtyProc(cmd)
        try:
            # Must not crash
            proc.resize(120, 40)
            proc.resize(80, 24)
            proc.wait(timeout=5.0)
        finally:
            proc.close()

    def test_ptyproc_termination_and_kill_lifecycle(self):
        """PtyProc must support graceful terminate() and kill() on Windows."""
        sh = shutil.which("bash") or shutil.which("sh")
        cmd = "sleep 10" if sh else "ping -n 10 127.0.0.1"
        proc = PtyProc(cmd)
        try:
            assert proc.poll() is None, "Process should be running initially"

            # Terminate
            proc.terminate()
            rc = proc.wait(timeout=5.0)
            assert proc.poll() is not None, "Process should have terminated"

            # Kill on already terminated process must not raise
            proc.kill()
        finally:
            proc.close()

    def test_ptystdin_safe_writes_and_flush(self):
        """PtyStdin must safely handle str, bytes, and flush() without unhandled exceptions."""
        mock_pipe = Mock()
        stdin_wrapper = PtyStdin(mock_pipe)

        stdin_wrapper.write("hello string\n")
        assert mock_pipe.write.called
        assert mock_pipe.write.call_args[0][0] == b"hello string\n"

        stdin_wrapper.write(b"hello bytes\n")
        assert mock_pipe.write.call_args[0][0] == b"hello bytes\n"

        stdin_wrapper.flush()
        assert mock_pipe.flush.called


# ============================================================================
# STANDALONE CLI RUNNER
# ============================================================================

def run_all_tests():
    """Execute all test methods and report empirical pass/fail metrics."""
    test_classes = [
        TestPanelUpdaterAdversarial,
        TestAdbOutputNormalizationAdversarial,
        TestShellEscapingSafetyAdversarial,
        TestAdbShellIdempotencyAdversarial,
        TestPtyProcWindowsFallbackAdversarial,
    ]

    total = 0
    passed = 0
    failed = 0
    start_time = time.time()

    print("=" * 78)
    print("M2 EMPIRICAL ADVERSARIAL CHALLENGER — EXECUTION HARNESS")
    print("Target: F06 (PanelUpdater), F07 (Cross-Platform / Shell), F08 (ADB Idempotency)")
    print("Host OS:", sys.platform, "| Python:", sys.version.split()[0])
    print("=" * 78)

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(cls) if m.startswith("test_")]
        print(f"\n--- Running {cls.__name__} ({len(methods)} tests) ---")
        for m in methods:
            total += 1
            fn = getattr(instance, m)
            try:
                # Handle parameterized test manually if needed
                if hasattr(fn, "pytestmark"):
                    # Check if parametrized
                    for mark in fn.pytestmark:
                        if mark.name == "parametrize":
                            param_names = mark.args[0].split(",")
                            param_values = mark.args[1]
                            for val in param_values:
                                fn(*val if isinstance(val, (tuple, list)) else (val,))
                            break
                    else:
                        fn()
                else:
                    fn()
                print(f"  [PASS] {m}")
                passed += 1
            except Exception as e:
                print(f"  [FAIL] {m}: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1

    elapsed = time.time() - start_time
    print("\n" + "=" * 78)
    print(f"RESULTS: Total: {total} | Passed: {passed} | Failed: {failed} | Time: {elapsed:.2f}s")
    print("=" * 78)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
