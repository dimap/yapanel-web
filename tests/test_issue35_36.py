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


class Mini3Boundaries(unittest.TestCase):
    def test_unverified_animations_stay_blocked(self):
        s = fresh_server()
        profiles.install(s, 'mini3')
        p = s.Panel(Mock())
        for method, args in [('anim_install', ('ring', 'demo.led')), ('ring_try', ('loop\n', 5))]:
            with self.subTest(method=method), self.assertRaisesRegex(s.AdbError, 'mini3'):
                getattr(p, method)(*args)
        p.adb.shell.assert_not_called()
        p.adb.push.assert_not_called()
        p.adb.shell.return_value = ('', '', 0)
        self.assertTrue(p.hook_status()['supported'])

    def test_alarms_and_scenes_keep_native_methods(self):
        s = fresh_server()
        methods = ('alarm_test', 'sound_replace')
        original = {name: getattr(s.Panel, name) for name in methods}
        profiles.install(s, 'mini3')
        for name in methods:
            self.assertIs(getattr(s.Panel, name), original[name])

    def test_missing_boot_route_rejects_schedules_before_any_write(self):
        for method, args in [('alarms_save', ({'alarms': [{'time': '07:30'}]},)),
                             ('scenes_save', ({'scenes': []},)),
                             ('alarm_sound_upload', ('test.wav', b'audio')),
                             ('_alarm_write', ('/data/test', [])),
                             ('alarm_install', ())]:
            with self.subTest(method=method):
                s = fresh_server()
                profiles.install(s, 'mini3')
                adb = Mock()
                adb.shell.return_value = ('', '', 3)
                p = s.Panel(adb)
                p.alarms = Mock(return_value={'alarms': []})
                p._alarm_wav = Mock()
                with self.assertRaisesRegex(s.AdbError, 'Автозапуск'):
                    getattr(p, method)(*args)
                adb.push.assert_not_called()
                p._alarm_wav.assert_not_called()
                self.assertEqual(adb.shell.call_count, 1)

    def test_clean_mini3_offers_hook_setup_without_saved_features(self):
        s = fresh_server()
        profiles.install(s, 'mini3')
        adb = Mock()
        adb.shell.return_value = ('', '', 0)
        state = s.Panel(adb).hook_status()
        self.assertEqual(state['users'], [])
        self.assertTrue(state['setup_required'])
        adb.shell.return_value = ('hook\n', '', 0)
        self.assertFalse(s.Panel(adb).hook_status()['setup_required'])


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
            subprocess.run(['sh', '-c', script], env={**os.environ, 'PATH': str(bin)+':'+os.environ['PATH']}, check=True, timeout=5)
            self.assertEqual(log.read_text().splitlines(), ['off'])


class BluetoothPower(unittest.TestCase):
    @unittest.skipUnless(shutil.which('sh'), 'requires sh')
    def test_power_off_propagates_dbus_failure_and_saves_only_success(self):
        for status in (1, 0):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                flag, pair = root / 'off', root / 'pair'
                pair.touch()
                binary = root / 'dbus-send'
                binary.write_text(f'#!/bin/sh\necho bluez-error >&2\nexit {status}\n')
                binary.chmod(0o755)
                p = server.Panel(Mock())
                p.BT_PAIR_FLAG = str(pair)
                p._ensure_hook = Mock()
                p.bt_state = Mock(return_value={'powered': False})
                def shell(command, **kwargs):
                    if command.startswith('[ -f '):
                        return '', '', 0  # no active Bluetooth output
                    result = subprocess.run(['sh', '-c', command], capture_output=True, text=True,
                                            env={**os.environ, 'PATH': td + ':' + os.environ['PATH']}, timeout=5)
                    if result.returncode:
                        raise server.AdbError(result.stderr)
                    return result.stdout, result.stderr, result.returncode
                p.adb.shell.side_effect = shell
                with patch('server.BT_OFF', str(flag)), patch('server.WEB_DIR', td):
                    if status:
                        with self.assertRaisesRegex(server.AdbError, 'bluez-error'):
                            p.bt_action({'action': 'power_off'})
                        p.bt_state.assert_not_called()
                        p._ensure_hook.assert_not_called()
                    else:
                        self.assertEqual(p.bt_action({'action': 'power_off'}), {'powered': False})
                        p._ensure_hook.assert_called_once()
                self.assertEqual(flag.exists(), status == 0)
                self.assertEqual(pair.exists(), status != 0)


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
