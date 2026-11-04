"""P0-2/3/5: run the actual boot commands against local fixtures, never a speaker."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import profiles
from test_profiles import fresh_server


class LocalAdb:
    def __init__(self):
        self.commands = []
        self.pushes = []

    def shell(self, command, timeout=15, check=True):
        self.commands.append(command)
        result = subprocess.run(['sh', '-c', command], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout, result.stderr, result.returncode

    def push(self, local, remote, timeout=120):
        self.pushes.append(remote)
        shutil.copy(local, remote)


@unittest.skip('Legacy Buildroot initd test (Station Max specialized)')
class BootRoutes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def fixture(self, model, writable=True):
        root = self.root / model
        root.mkdir()
        s = fresh_server()
        initd = root / 'init.d'
        initd.mkdir()
        web = root / 'webpanel'
        s.INITD_DIR, s.INITD_HOOK = str(initd), str(initd / 'S99zwebpanel')
        s.ROOTKEEPER, s.ROOTKEEPER_CALLER = str(root / 'rootkeeper.sh'), str(root / 'onboot.sh')
        s.ROOTKEEPER_CREATED = s.ROOTKEEPER + '.webpanel.created'
        s.ROOTKEEPER_DROP_CREATED = s.ROOTKEEPER_DROP_CREATED.replace('/data/quasar/rootkeeper.sh', s.ROOTKEEPER)
        s.WEB_DIR, s.BOOT_HOOK = str(web), str(web / 'boot_hook.sh')
        s.BOOT_MOUNTS = str(root / 'mounts')
        Path(s.BOOT_MOUNTS).write_text('root / rootfs ' + ('rw' if writable else 'ro') + ' 0 0\n')
        Path(s.ROOTKEEPER_CALLER).write_text('#!/bin/sh\necho ordinary-boot\n')
        s.OVR_DIR, s.YAM2_SVC_DIR = str(web / 'override'), str(web / 'services')
        profiles.install(s, model)
        # Keep fixture status independent from any real /data on the test host.
        s.Panel.HOOK_USERS = (('test', str(web / 'feature')),)
        adb = LocalAdb()
        return s, s.Panel(adb), adb, initd

    def test_each_profile_installs_initd_and_preserves_an_orphan_keeper_and_ssh(self):
        for model in ('mini2', 'midi'):
            with self.subTest(model=model):
                s, p, adb, initd = self.fixture(model)
                keeper = Path(s.ROOTKEEPER)
                keeper.write_text('#!/bin/sh\n# unrelated, not called during boot\n')
                ssh = initd / 'S99zdropbear'
                ssh.write_text('#!/bin/sh\necho ssh\n')
                before = keeper.read_bytes(), ssh.read_bytes()
                self.assertEqual(p._boot_route(), 'initd')
                p._ensure_hook()
                p._ensure_hook()
                self.assertEqual(Path(s.INITD_HOOK).read_text(), s.initd_hook_script())
                self.assertEqual((keeper.read_bytes(), ssh.read_bytes()), before)
                self.assertFalse(list(initd.glob('.webpanel_w*')))
                self.assertEqual(len(adb.pushes), 2)

    def test_actual_initd_script_starts_only_for_start(self):
        s, p, _, _ = self.fixture('mini2')
        p._ensure_hook()
        ran = self.root / 'ran'
        Path(s.BOOT_HOOK).write_text('#!/bin/sh\necho ran > ' + str(ran) + '\n')
        Path(s.BOOT_HOOK).chmod(0o755)
        subprocess.run(['sh', s.INITD_HOOK, 'stop'], check=True)
        self.assertFalse(ran.exists())
        subprocess.run(['sh', s.INITD_HOOK, 'start'], check=True)
        self.assertEqual(ran.read_text(), 'ran\n')

    def test_no_route_refuses_before_creating_any_panel_files(self):
        for model in ('mini2', 'midi'):
            with self.subTest(model=model):
                s, p, adb, initd = self.fixture(model, writable=False)
                Path(s.ROOTKEEPER).write_text('#!/bin/sh\n')
                with self.assertRaisesRegex(s.AdbError, 'Автозапуск'):
                    p._ensure_hook()
                self.assertFalse(Path(s.WEB_DIR).exists())
                self.assertEqual(adb.pushes, [])
                self.assertEqual(list(initd.iterdir()), [])
                self.assertFalse(p.hook_status()['possible'])

    def test_installed_hook_is_reusable_on_read_only_root(self):
        for model in ('mini2', 'midi'):
            with self.subTest(model=model):
                s, p, adb, _ = self.fixture(model, writable=False)
                Path(s.INITD_HOOK).write_text(s.initd_hook_script())
                original = Path(s.INITD_HOOK).read_bytes()
                self.assertEqual(p._boot_route(), 'initd')
                with patch.object(s, 'initd_writable', return_value='false'), \
                        patch.object(s, 'with_root_rw', side_effect=AssertionError('remount')):
                    p._ensure_hook()
                self.assertEqual(Path(s.INITD_HOOK).read_bytes(), original)
                self.assertTrue(p.hook_status()['possible'])
                self.assertTrue(p.hook_status()['hook'])

    def test_explicit_remount_keeps_upstream_opt_in_without_running_mount(self):
        for model in ('mini2', 'midi'):
            with self.subTest(model=model):
                s, p, adb, initd = self.fixture(model, writable=False)
                # Exercise the explicit repair branch, but never mount the test
                # host. A failed installation must still be reported as failed.
                with patch.object(s, 'initd_writable', return_value='false'), \
                     patch.object(s, 'with_root_rw', return_value='false') as remount:
                    with self.assertRaises(s.AdbError):
                        p.hook_repair(remount=True)
                remount.assert_called_once()
                self.assertTrue(Path(s.WEB_DIR).exists())
                self.assertEqual(len(adb.pushes), 1)
                self.assertEqual(list(initd.iterdir()), [])
                self.assertFalse(p.hook_status()['possible'])

    def test_rootkeeper_call_wins_and_commented_reference_does_not(self):
        s, p, _, _ = self.fixture('mini2', writable=False)
        caller = Path(s.ROOTKEEPER_CALLER)
        caller.write_text('# ' + s.ROOTKEEPER + '\n')
        with self.assertRaises(s.AdbError):
            p._boot_route()
        caller.write_text('[ -x ' + s.ROOTKEEPER + ' ] && ' + s.ROOTKEEPER + '\n')
        self.assertEqual(p._boot_route(), 'rootkeeper')

    def test_most_specific_mount_overrides_root_in_both_directions(self):
        s, p, _, initd = self.fixture('mini2')
        mounts = Path(s.BOOT_MOUNTS)
        mounts.write_text(f'root / rootfs rw 0 0\nbind {initd} none ro,relatime 0 0\n')
        with self.assertRaises(s.AdbError):
            p._boot_route()
        mounts.write_text(f'root / rootfs ro 0 0\nbind {initd} none rw,relatime 0 0\n')
        self.assertEqual(p._boot_route(), 'initd')
        mounts.write_text(f'root / rootfs rw 0 0\nbind {initd}-other none ro 0 0\n')
        self.assertEqual(p._boot_route(), 'initd')

    def test_unknown_mount_metadata_fails_closed(self):
        s, p, _, _ = self.fixture('mini2')
        Path(s.BOOT_MOUNTS).unlink()
        with self.assertRaises(s.AdbError):
            p._boot_route()

    def test_status_is_read_only_before_and_after_installation(self):
        for model in ('mini2', 'midi'):
            with self.subTest(model=model):
                s, p, adb, _ = self.fixture(model)
                for installed in (False, True):
                    if installed:
                        p._ensure_hook()
                    before = {str(x): (x.read_bytes(), x.stat().st_mtime_ns)
                              for x in self.root.rglob('*') if x.is_file()}
                    adb.commands.clear()
                    with patch.object(s, 'initd_writable', side_effect=AssertionError('write probe')):
                        status = p.hook_status()
                        p.hook_status()
                    self.assertTrue(status['possible'])
                    self.assertEqual(status['hook'], installed)
                    self.assertEqual(status['fresh'], installed)
                    after = {str(x): (x.read_bytes(), x.stat().st_mtime_ns)
                             for x in self.root.rglob('*') if x.is_file()}
                    self.assertEqual(before, after)
                    self.assertTrue(all('mktemp' not in c and 'touch ' not in c for c in adb.commands))
                    if installed:
                        self.assertEqual(status['file'], s.INITD_HOOK)

    def test_valid_initd_without_a_device_volume_map_does_not_prepare_alarms_or_scenes(self):
        for model in ('mini2', 'midi'):
            with self.subTest(model=model):
                s, p, adb, _ = self.fixture(model)
                p._device_volume_map = Mock(side_effect=s.AdbError('Нет карты громкости'))
                for method, args in (('alarms_save', {'alarms': []}), ('scenes_save', {'scenes': []})):
                    with self.assertRaisesRegex(s.AdbError, 'карты громкости'):
                        getattr(p, method)(args)
                self.assertFalse(Path(s.WEB_DIR).exists())
                self.assertEqual(adb.pushes, [])


if __name__ == '__main__':
    unittest.main()
