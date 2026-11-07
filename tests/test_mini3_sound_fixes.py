"""Регрессии подмены звуков и аварийного возврата корня в ro."""
import shutil
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from test_profiles import fresh_server

class Mini3Sounds(unittest.TestCase):
    def test_override_uses_busybox_and_checks_hook_before_upload(self):
        s = fresh_server()
        a = Mock()
        a.shell.return_value = ('', '', 0)
        p = s.Panel(a)
        p._ensure_hook = Mock()
        a.push.side_effect = lambda *_: p._ensure_hook.assert_called_once()
        p._install_override(b'test', '/data/test', '/system/test')
        command = next(c.args[0] for c in a.shell.call_args_list if 'mount -t none' in c.args[0])
        self.assertIn('mount -t none -o bind /data/test /system/test', command)
        self.assertNotIn('mount --bind', command)
        self.assertIn('umount /system/test || exit 1', command)
        self.assertLess(command.index('umount /system/test'), command.index('mount -t none'))

    def test_original_read_uses_busybox(self):
        s = fresh_server()
        a = Mock()
        a.shell.return_value = ('mounted', '', 0)
        a.read_bytes.return_value = b'original'
        p = s.Panel(a)
        self.assertEqual(p._original_bytes('/system/test'), b'original')
        self.assertTrue(any('mount -t none -o bind' in c.args[0] for c in a.shell.call_args_list))

    def test_root_restore_runs_on_success_and_early_exit(self):
        shell = shutil.which('sh')
        if not shell:
            self.skipTest('Нужен sh')
        s = fresh_server()
        s.ROOT_RW = ':'
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / 'restored'
            s.ROOT_RO = 'echo restored > ' + shlex.quote(marker.as_posix())
            for action, code in [(':', 0), ('exit 4', 4)]:
                marker.unlink(missing_ok=True)
                r = subprocess.run([shell, '-c', s.with_root_rw(action)], capture_output=True)
                self.assertEqual(r.returncode, code)
                self.assertTrue(marker.exists())

    def test_reset_unmount_outcomes(self):
        shell = shutil.which('sh')
        if not shell:
            self.skipTest('Нужен sh')
        s = fresh_server()
        # Только файлы во временной папке; настоящие mount/umount не вызываются.
        for mode in ('fail', 'normal', 'lazy', 'unmounted'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                d = Path(td)
                custom, log = d / 'custom', d / 'calls'
                custom.write_bytes(b'previous sound')
                s.OVR_DIR = (d / 'missing-overrides').as_posix()
                stub = (f'mode={mode}; calls={shlex.quote(log.as_posix())}; '
                        'grep() { [ "$mode" != unmounted ]; }; '
                        'umount() { echo "$*" >> "$calls"; '
                        '[ "$mode" = normal ] || { [ "$mode" = lazy ] && [ "$1" = -l ]; }; }; ')
                def run(command, check=True, **kwargs):
                    result = subprocess.run([shell, '-c', stub + command], capture_output=True, timeout=5)
                    out, err = result.stdout.decode(), result.stderr.decode('utf-8', 'replace')
                    # Такой же контракт, как у Adb.shell: ненулевой код при check=True — ошибка API.
                    if check and result.returncode:
                        raise s.AdbError(err)
                    return out, err, result.returncode
                adb = Mock(); adb.shell.side_effect = run
                p = s.Panel(adb)
                p._sound_paths = Mock(return_value=('/system/review-target', custom.as_posix()))
                p._drop_hook_if_unused = Mock()
                p.sounds_custom = Mock(return_value={'custom': []})
                if mode == 'fail':
                    with self.assertRaisesRegex(s.AdbError, 'пользовательский файл сохранён'):
                        p.sound_reset('common/test.mp3')
                    self.assertEqual(custom.read_bytes(), b'previous sound')
                    p._drop_hook_if_unused.assert_not_called()
                    p.sounds_custom.assert_not_called()
                else:
                    self.assertEqual(p.sound_reset('common/test.mp3'), {'custom': []})
                    self.assertFalse(custom.exists())
                    p._drop_hook_if_unused.assert_called_once()
                calls = log.read_text().splitlines() if log.exists() else []
                self.assertEqual(calls, [] if mode == 'unmounted' else
                                 ['/system/review-target'] if mode == 'normal' else
                                 ['/system/review-target', '-l /system/review-target'])

class Mini3AnimationReset(unittest.TestCase):
    def test_saved_override_can_be_reset_with_empty_catalog(self):
        import profiles
        s = fresh_server()
        profiles.install(s, 'mini3')
        p = s.Panel(Mock())
        p.adb.shell.return_value = ('./oknix/alice_listening.led\n', '', 0)
        p._remove_override = Mock()
        self.assertEqual(p.patterns(), {'ring': [], 'clock': []})
        p.anim_reset('ring', 'alice_listening.led')
        p._remove_override.assert_called_once_with(
            s.OVR_DIR + '/ledpatterns/oknix/alice_listening.led',
            s.PATTERNS['ring'] + '/alice_listening.led')
        for name in ('../alice_listening.led', 'missing.led', 'my/test.led', "bad'file.led", None):
            with self.assertRaises(s.AdbError):
                p.anim_reset('ring', name)
        p.adb.shell.return_value = ('', '', 0)
        with self.assertRaises(s.AdbError):
            p.anim_reset('ring', 'alice_listening.led')

    def test_reset_failure_propagates_without_refresh(self):
        s = fresh_server()
        p = s.Panel(Mock())
        p.anim_custom = Mock(return_value={'ring': ['test.led'], 'clock': []})
        p._remove_override = Mock(side_effect=s.AdbError('пользовательский файл сохранён'))
        with self.assertRaisesRegex(s.AdbError, 'файл сохранён'):
            p.anim_reset('ring', 'test.led')
        p.anim_custom.assert_called_once()

    def test_mini3_confirmed_scope_keeps_animation_restrictions(self):
        import profiles
        s = fresh_server()
        profiles.install(s, 'mini3')
        handler = Mock()
        handler.path = '/api/profile'
        handler._guard.return_value = False
        s.Handler.do_GET(handler)
        profile = handler._send.call_args.args[1]
        self.assertEqual(set(profile['tested']), {'sounds', 'alarm', 'scenes'})
        self.assertIn('retail CA', profile['tested_note'])
        self.assertFalse(profile['ring_preview_supported'])
