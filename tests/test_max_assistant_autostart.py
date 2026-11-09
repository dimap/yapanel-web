"""Offline tests for local Station Max assistant autostart."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class TestMaxAssistantAutostart(unittest.TestCase):
    def test_delayed_files_trigger_retry_then_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            events = root / "startup.log"
            ticks = []

            def sleep(_delay):
                ticks.append(1)
                if len(ticks) == 3:
                    (directory / "config.json").write_text("{}", encoding="utf-8")
                    launcher = directory / "assistantd"
                    launcher.write_text("#!/bin/sh\n", encoding="ascii")
                    launcher.chmod(0o755)

            start = Mock(return_value=123)
            with patch.object(server, "_max_assistant_running", side_effect=[False, True]):
                ok = server._max_assistant_autostart_worker(
                    assistant_dir=directory,
                    log_path=events,
                    attempts=4,
                    delay=0,
                    sleep=sleep,
                    launch=start,
                )
            self.assertTrue(ok)
            self.assertEqual(start.call_count, 1)
            log = events.read_text(encoding="utf-8")
            self.assertIn("WAIT_FILES attempt=1", log)
            self.assertIn("START_ATTEMPT attempt=3", log)
            self.assertIn("START_OK", log)
            self.assertNotIn("RETRIES_EXHAUSTED", log)

    def test_autostart_flag_disables_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            (directory / "config.json").write_text("{}", encoding="utf-8")
            launcher = directory / "assistantd"
            launcher.write_text("#!/bin/sh\n", encoding="ascii")
            launcher.chmod(0o755)
            flag = root / "no-autostart"
            flag.write_text("", encoding="ascii")
            start = Mock()
            ok = server._max_assistant_autostart_worker(
                assistant_dir=directory, log_path=root / "startup.log",
                attempts=2, delay=0, sleep=lambda _: None, launch=start,
                autostart_path=flag,
            )
            self.assertFalse(ok)
            start.assert_not_called()
            log = (root / "startup.log").read_text(encoding="utf-8")
            self.assertIn("AUTOSTART_DISABLED", log)
            self.assertNotIn("START_ATTEMPT", log)

    def test_does_not_restart_existing_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            (directory / "config.json").write_text("{}", encoding="utf-8")
            launcher = directory / "assistantd"
            launcher.write_text("#!/bin/sh\n", encoding="ascii")
            launcher.chmod(0o755)
            start = Mock()
            with patch.object(server, "_max_assistant_running", return_value=True):
                ok = server._max_assistant_autostart_worker(
                    assistant_dir=directory, log_path=root / "startup.log",
                    attempts=1, delay=0, sleep=lambda _: None, launch=start,
                )
            self.assertTrue(ok)
            start.assert_not_called()
            self.assertIn("ALREADY_RUNNING",
                          (root / "startup.log").read_text(encoding="utf-8"))

    def test_no_files_leaves_clear_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            start = Mock()
            ok = server._max_assistant_autostart_worker(
                assistant_dir=directory, log_path=root / "startup.log",
                attempts=3, delay=0, sleep=lambda _: None, launch=start,
            )
            self.assertFalse(ok)
            start.assert_not_called()
            log = (root / "startup.log").read_text(encoding="utf-8")
            self.assertIn("WAIT_FILES attempt=1", log)
            self.assertIn("RETRIES_EXHAUSTED", log)

    def test_direct_launcher_waits_for_daemon_pid_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            (directory / "config.json").write_text("{}", encoding="utf-8")
            launcher = directory / "assistantd"
            launcher.write_text("#!/bin/sh\n", encoding="ascii")
            launcher.chmod(0o755)
            pid_path = root / "assistant.pid"
            child = Mock()
            child.pid = 23456
            child.poll.return_value = None
            pid_path.write_text("11111\n", encoding="ascii")  # Старый PID от предыдущего запуска.
            def daemon_writes_pid(_seconds):
                pid_path.write_text("23456\n", encoding="ascii")

            with patch.object(server, "_max_assistant_running", side_effect=[False, False, True]):
                with patch.object(server.subprocess, "Popen", return_value=child) as popen:
                    with patch.object(server.time, "sleep", side_effect=daemon_writes_pid):
                        pid = server._max_assistant_launch_local(directory, pid_path)
            self.assertEqual(pid, 23456)
            self.assertEqual(pid_path.read_text(encoding="ascii").strip(), "23456")
            args, kwargs = popen.call_args
            self.assertEqual(args[0][:1], [str(launcher)])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stderr"], server.subprocess.STDOUT)

    def test_remote_adb_does_not_launch_process_on_panel_host(self):
        panel = server.Panel(Mock())
        panel.adb.is_local = False
        with patch.object(server.Path, "is_file", return_value=True):
            with patch.object(panel, "assistant_status", return_value={"running": True}):
                with patch.object(panel, "detach") as detach:
                    with patch.object(server, "_max_assistant_launch_local") as launch:
                        panel.assistant_start()
        detach.assert_called_once()
        launch.assert_not_called()

    def test_local_adb_uses_direct_launcher(self):
        panel = server.Panel(Mock())
        panel.adb.is_local = True
        with patch.object(server.Path, "is_file", return_value=True):
            with patch.object(panel, "assistant_daemon_stop") as stop:
                with patch.object(panel, "assistant_status", return_value={"running": True}):
                    with patch.object(server, "_max_assistant_launch_local") as launch:
                        with patch.object(panel, "detach") as detach:
                            panel.assistant_start()
        stop.assert_called_once()
        launch.assert_called_once()
        detach.assert_not_called()


class TestMaxAssistantLocalStop(unittest.TestCase):
    def test_stale_pid_is_not_killed(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "assistant.sd"
            pidfile.write_text("12345\n", encoding="ascii")
            panel = server.Panel(Mock())
            panel.adb.is_local = True
            panel.adb.shell.return_value = ("", "", 0)
            with patch.object(server, "ASSIST_SD", str(pidfile)):
                with patch.object(server.Path, "is_file", return_value=True):
                    with patch.object(server, "_max_assistant_running", return_value=False):
                        with patch.object(server.os, "kill") as kill:
                            panel.assistant_daemon_stop()
            kill.assert_not_called()
            self.assertFalse(pidfile.exists())

    def test_running_pid_is_terminated(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "assistant.sd"
            pidfile.write_text("12345\n", encoding="ascii")
            panel = server.Panel(Mock())
            panel.adb.is_local = True
            panel.adb.shell.return_value = ("", "", 0)
            with patch.object(server, "ASSIST_SD", str(pidfile)):
                with patch.object(server.Path, "is_file", return_value=True):
                    with patch.object(server, "_max_assistant_running", side_effect=[True, False, False]):
                        with patch.object(server.os, "kill") as kill:
                            with patch.object(server.time, "sleep"):
                                panel.assistant_daemon_stop()
            kill.assert_called_once_with(12345, server.signal.SIGTERM)
            self.assertFalse(pidfile.exists())

    def test_assistantd_wrapper_uses_lf(self):
        wrapper = Path(server.__file__).resolve().parent / "device" / "assistant" / "assistantd"
        data = wrapper.read_bytes()
        self.assertNotIn(b"\r\n", data)
        self.assertTrue(data.startswith(b"#!/system/bin/sh\n"))


if __name__ == "__main__":
    unittest.main()
