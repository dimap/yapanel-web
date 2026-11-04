"""Mini 3 write boundaries and Bluetooth boot/DSP regressions; no speaker required."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import server
import profiles
from test_profiles import fresh_server


class BluetoothBoot(unittest.TestCase):
    @unittest.skipUnless(shutil.which('sh'), 'requires sh')
    def test_removed_off_flag_stops_existing_boot_loop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            flag, log = root / 'off', root / 'calls'
            flag.touch()
            bin = root / 'bin'
            bin.mkdir()
            (bin / 'dbus-send').write_text('#!/bin/sh\ncase "$*" in *Properties.Get*) echo true;; *) echo off >> "'+str(log)+'";; esac\n')
            (bin / 'sleep').write_text('#!/bin/sh\nrm -f "'+str(flag)+'"\n')
            for f in bin.iterdir():
                f.chmod(0o755)
            script = server.BT_OFF_BOOT.replace(server.BT_OFF, str(flag)) + '\nwait\n'
            subprocess.run(['sh', '-c', script], env={**os.environ, 'PATH': str(bin) + os.pathsep + os.environ['PATH']}, check=True, timeout=5)
            self.assertEqual(log.read_text().splitlines(), ['off'])


class BluetoothDSP(unittest.TestCase):
    def test_missing_tap_installs_binary_and_rebuilds_forwarder_without_capture(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / 'vizd'
            binary.write_bytes(b'fake arm64 binary')
            p = server.Panel(Mock())
            p._viz_binary = Mock(return_value=binary)
            p.viz_start = Mock()
            p.adb.shell.side_effect = [('aarch64\n', '', 0), ('', '', 0)] + [('', '', 0)] * 3
            p._ensure_btout_tap()
            p.adb.push.assert_called_once_with(binary, server.VIZD+'.new', timeout=300)
            commands = '\n'.join(c.args[0] for c in p.adb.shell.call_args_list)
            self.assertIn(server.BTOUT_FWD_SD, commands)
            self.assertNotIn('arecord', commands)
            p.viz_start.assert_not_called()

    def test_existing_matching_binary_does_not_interrupt_audio(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / 'vizd'
            binary.write_bytes(b'current binary')
            p = server.Panel(Mock())
            p._viz_binary = Mock(return_value=binary)
            p.adb.shell.side_effect = [('aarch64\n', '', 0), (hashlib.md5(binary.read_bytes()).hexdigest()+'  '+server.VIZD, '', 0)]
            p._ensure_btout_tap()
            p.adb.push.assert_not_called()
            self.assertEqual(p.adb.shell.call_count, 2)

    def test_dsp_failure_does_not_save_apparently_applied_preset(self):
        p = server.Panel(Mock())
        p._ensure_btout_tap = Mock(side_effect=server.AdbError('no DSP'))
        with patch('server.load_settings', return_value={}), patch('server.save_settings') as save:
            with self.assertRaisesRegex(server.AdbError, 'no DSP'):
                p.btout_eq('rock')
            save.assert_not_called()
        p.adb.push.assert_not_called()


class AnimationErrors(unittest.TestCase):
    def test_failed_preview_reports_utility_output(self):
        p = server.Panel(Mock())
        p.stop_pattern = Mock()
        p.detach = Mock()
        p.adb.shell.side_effect = [('', '', 0), ('led_util: invalid frame layout', '', 0)]
        with self.assertRaisesRegex(server.AdbError, 'invalid frame layout'):
            p._run_timed('ring', 'led_util demo', 5, verify=True)
        self.assertIn('/tmp/webpanel_ring.log', p.detach.call_args.args[1])

    def test_running_preview_is_accepted(self):
        p = server.Panel(Mock())
        p.stop_pattern = Mock()
        p.detach = Mock()
        p.adb.shell.side_effect = [('', '', 0), ('@running\n', '', 0)]
        p._run_timed('ring', 'led_util demo', 5, verify=True)

    @unittest.skipUnless(shutil.which('sh'), 'requires sh')
    def test_real_shell_start_check_detects_success_and_utility_failure(self):
        for command, succeeds in [('sleep 2', True), ("sh -c 'echo bad-layout >&2; exit 9'", False)]:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as td:
                p = server.Panel(Mock())
                p.stop_pattern = Mock()
                processes = []
                def local(script):
                    return script.replace('/tmp/webpanel_ring', str(Path(td) / 'ring'))
                def shell(script, **kwargs):
                    result = subprocess.run(['sh', '-c', local(script)], capture_output=True, text=True, timeout=5)
                    return result.stdout, result.stderr, result.returncode
                p.adb.shell = shell
                p.detach = lambda kind, script: processes.append(subprocess.Popen(['sh', '-c', local(script)]))
                try:
                    if succeeds:
                        p._run_timed('ring', command, 1, verify=True)
                    else:
                        with self.assertRaisesRegex(server.AdbError, 'bad-layout'):
                            p._run_timed('ring', command, 1, verify=True)
                finally:
                    for proc in processes:
                        proc.wait(timeout=5)
