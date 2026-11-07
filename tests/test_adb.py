import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import server


class AdbDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="adb discovery ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        for context in (
            patch.dict(os.environ, {"ADB": "", "ANDROID_HOME": "", "ANDROID_SDK_ROOT": ""}),
            patch.object(server, "ROOT", self.home / "project"),
            patch.object(server.shutil, "which", return_value=None),
            patch.object(Path, "home", return_value=self.home),
        ):
            context.start()
            self.addCleanup(context.stop)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def test_android_studio_on_macos_without_path_entry(self):
        adb = self.executable(self.home / "Library/Android/sdk/platform-tools/adb")
        with patch.object(server.sys, "platform", "darwin"):
            self.assertEqual(server.find_adb(), adb)

    def test_custom_sdk_directory_with_spaces(self):
        executable = "adb.exe" if os.name == "nt" else "adb"
        sdk = self.home / "custom Android SDK"
        adb = self.executable(sdk / "platform-tools" / executable)
        for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: str(sdk)}):
                self.assertEqual(server.find_adb(), adb)

    def test_explicit_adb_takes_precedence_over_macos_sdk(self):
        self.executable(self.home / "Library/Android/sdk/platform-tools/adb")
        explicit = self.executable(self.home / "chosen tools/adb")
        with patch.object(server.sys, "platform", "darwin"), patch.dict(os.environ, {"ADB": explicit}):
            self.assertEqual(server.find_adb(), explicit)


class TransferTests(unittest.TestCase):
    def test_slow_reply_during_file_transfer_is_not_a_disconnect(self):
        adb = server.Adb("adb", "192.0.2.1:5555")
        conn = server.Connection(adb)
        with patch.object(server.subprocess, "run", side_effect=server.subprocess.TimeoutExpired("adb", 4)) as run:
            with adb._transfer():
                self.assertTrue(conn._alive())
            run.assert_not_called()
            self.assertFalse(conn._alive())
        self.assertEqual(adb.transfers, 0)


class ConnectionProbeTests(unittest.TestCase):
    def monitor(self, replies, agent_replies=None):
        class StopMonitor(Exception):
            pass

        conn = server.Connection(server.Adb("adb", "192.0.2.1:5555"))
        conn._set("online")
        conn._probe_error = "таймаут 10 с"
        conn._agent_alive = Mock(side_effect=agent_replies) if agent_replies else Mock(return_value=False)
        conn._adb_state = Mock(return_value=("device", ""))
        conn._alive = Mock(side_effect=[*replies, StopMonitor()])
        conn._drop = Mock()
        conn._sleep = Mock()
        result = SimpleNamespace(stdout=b"connected", stderr=b"", returncode=0)
        with patch.object(server.JOURNAL, "add"), patch.object(server.time, "sleep"), \
                patch.object(server.subprocess, "run", return_value=result) as run:
            with self.assertRaises(StopMonitor):
                conn.run()
        return conn, run

    def test_transient_failures_do_not_disconnect_and_success_resets_count(self):
        conn, run = self.monitor([False, False, True, False, False, True])
        conn._drop.assert_not_called()
        run.assert_not_called()
        self.assertTrue(conn.online)
        self.assertEqual(conn._probe_failures, 0)
        self.assertTrue(all(c.args == (15,) for c in conn._sleep.call_args_list))
        messages = [entry["msg"] for entry in conn.log]
        self.assertEqual(sum("проверка adb 1/3: таймаут 10 с" in m for m in messages), 2)

    def test_three_consecutive_failures_reconnect(self):
        conn, run = self.monitor([False, False, False])
        conn._drop.assert_called_once()
        self.assertIn(["adb", "connect", "192.0.2.1:5555"], [c.args[0] for c in run.call_args_list])
        self.assertEqual(conn._probe_failures, 0)
        self.assertIn("проверка adb 3/3: таймаут 10 с", [e["msg"] for e in conn.log])

    def test_agent_recovery_resets_adb_failure_count(self):
        conn, run = self.monitor([False, False, False, False, True],
                                 [False, False, True, False, False, False, False])
        conn._drop.assert_not_called()
        run.assert_not_called()

    def test_probe_timeout_and_unexpected_response_have_diagnostics(self):
        conn = server.Connection(server.Adb("adb", "192.0.2.1:5555"))
        with patch.object(server.subprocess, "run", side_effect=server.subprocess.TimeoutExpired("adb", 10)) as run:
            self.assertFalse(conn._alive())
            self.assertEqual(run.call_args.kwargs["timeout"], 10)
            self.assertEqual(conn._probe_error, "таймаут 10 с")
        for rc, out, err in [(1, b"", b"error: closed"), (0, b"unexpected", b""), (1, b"ok", b"failed")]:
            with self.subTest(rc=rc, out=out), patch.object(server.subprocess, "run",
                    return_value=SimpleNamespace(returncode=rc, stdout=out, stderr=err)):
                self.assertFalse(conn._alive())
                self.assertIn(f"код {rc}", conn._probe_error)
                self.assertIn(repr(out.decode()), conn._probe_error)
                self.assertIn(repr(err.decode()), conn._probe_error)


class AdbVersionTests(unittest.TestCase):
    def test_old_adb_is_skipped_and_reported(self):
        import subprocess as sp
        versions = {"/old/adb": "Android Debug Bridge version 1.0.31\n", "/new/adb": "Android Debug Bridge version 1.0.41\n"}
        run = lambda args, **kw: SimpleNamespace(stdout=versions.get(args[0], ""), returncode=0)
        with patch.object(server.subprocess, "run", side_effect=run), patch.object(server.Path, "is_file", return_value=True), \
             patch.dict(os.environ, {"ADB": "/old/adb"}), patch.object(server.shutil, "which", return_value="/new/adb"):
            self.assertEqual(server.find_adb(), "/new/adb")
        with patch.object(server.subprocess, "run", side_effect=run), patch.object(server.Path, "is_file", side_effect=[True] + [False] * 20), \
             patch.dict(os.environ, {"ADB": "/old/adb"}), patch.object(server.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit) as e:
                server.find_adb()
            self.assertIn("1.0.31", str(e.exception))

    def test_usage_dump_becomes_a_short_message(self):
        dump = "Android Debug Bridge version 1.0.31\n\n" + " -a - directs adb to listen on all interfaces\n" * 20
        self.assertIn("слишком старый", server.adb_usage_error(dump, "pull -a"))
        self.assertEqual(server.adb_usage_error("error: device offline", "pull -a"), "error: device offline")


class ShellExitCode(unittest.TestCase):
    """Старый adbd Лайт 2 без shell_v2: код команды приходит меткой в конце вывода."""

    def run_shell(self, stdout, rc=0):
        import server
        from unittest.mock import Mock, patch
        a = server.Adb('adb', 'dev')
        with patch.object(server.subprocess, 'run', return_value=Mock(returncode=rc, stdout=stdout.encode(), stderr=b'')) as r:
            res = a.shell('exit 3', check=False)
        return res, r.call_args.args[0][-1]

    def test_marker_gives_code_and_is_stripped(self):
        import server
        (out, _, code), cmd = self.run_shell(f'hello\r\n\r\n{server.Adb.RC_MARK}3\r\n')
        self.assertEqual((out, code), ('hello\n', 3))
        self.assertTrue(cmd.startswith('(\nexit 3\n); printf '))
        (out, _, code), _ = self.run_shell(f'noeol\r\n{server.Adb.RC_MARK}0\r\n')
        self.assertEqual((out, code), ('noeol', 0))

    def test_no_marker_keeps_adb_code(self):
        (out, _, code), _ = self.run_shell('partial', rc=1)
        self.assertEqual((out, code), ('partial', 1))
