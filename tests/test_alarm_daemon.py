"""Execute daemon launch commands with a local stub instead of a speaker."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import server


@unittest.skipUnless(shutil.which('sh'), 'requires sh')
class AlarmDaemon(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pidfile = self.root / 'daemon.pid'
        self.state = self.root / 'state'
        self.started = self.root / 'started'
        self.fire = self.root / 'fire'
        self.daemon = self.root / 'alarmd.sh'
        self.daemon.touch()
        stub = self.root / 'start-stop-daemon'
        stub.write_text('#!/bin/sh\necho started >> "' + str(self.started) + '"\n')
        stub.chmod(0o755)
        for key, value in {'ALARM_SD': self.pidfile, 'ALARM_STATE': self.state,
                           'ALARM_FIRE': self.fire, 'ALARMD': self.daemon}.items():
            p = patch.object(server, key, str(value))
            p.start()
            self.addCleanup(p.stop)
        self.panel = server.Panel(Mock())
        self.panel.adb.shell.side_effect = self.shell

    def shell(self, command):
        result = subprocess.run(['sh', '-c', command], capture_output=True, text=True,
                                env={**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH']},
                                timeout=5)
        if result.returncode:
            raise server.AdbError(result.stderr)
        return result.stdout, result.stderr, result.returncode

    def worker(self):
        proc = subprocess.Popen(['sleep', '30'])
        def cleanup():
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        self.addCleanup(cleanup)
        self.pidfile.write_text(str(proc.pid))
        return proc

    def test_test_button_starts_missing_daemon_and_writes_trigger(self):
        self.assertEqual(self.panel.alarm_test('abc123'), {'ok': True})
        self.assertEqual(self.started.read_text(), 'started\n')
        self.assertEqual(self.fire.read_text(), 'abc123\n')

    def test_test_button_keeps_running_daemon(self):
        proc = self.worker()
        self.panel.alarm_test('abc123')
        self.assertIsNone(proc.poll())
        self.assertFalse(self.started.exists())
        self.assertEqual(self.fire.read_text(), 'abc123\n')

    def test_save_restarts_idle_daemon(self):
        proc = self.worker()
        self.state.write_text('idle\n')
        self.panel._alarm_daemon(restart=True)
        self.assertEqual(proc.wait(timeout=5), -15)
        self.assertEqual(self.started.read_text(), 'started\n')

    def test_save_preserves_ringing_daemon(self):
        proc = self.worker()
        self.state.write_text('ringing|abc123|123|sound\n')
        self.panel._alarm_daemon(restart=True)
        self.assertIsNone(proc.poll())
        self.assertFalse(self.started.exists())
