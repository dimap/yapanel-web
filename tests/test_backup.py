"""Archive boundaries on a local fixture; no speaker, service or system reset."""
import io
import json
import tarfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from test_profiles import fresh_server


def archive(files):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return out.getvalue()


class BackupBoundaries(unittest.TestCase):
    def test_backup_contains_only_panel_payload_and_allowed_browser_settings(self):
        s = fresh_server()
        payload = {
            'alarm/alarms.conf': b'test schedule',
            'override/sounds/common/timer.opus': b'test audio',
            'anims/ring/demo.led': b'test animation',
            'assistant/config.json': b'private API key',
            'boot_hook.sh': b'executable script',
            '../../etc/init.d/S99zdropbear': b'existing SSH',
            '.yandex_auth.json': b'private token',
        }
        adb = Mock()
        adb.shell.return_value = ('\n'.join('./' + n for n in payload), '', 0)
        adb.pull.side_effect = lambda remote, local: Path(local).write_bytes(payload[remote[len(s.WEB_DIR) + 1:]])
        panel = s.Panel(adb)
        panel.version = Mock(return_value={'quasar_version': 'test-firmware'})
        panel.footprint = Mock(return_value={'items': [
            {'key': 'alarm', 'on': True}, {'key': 'hook', 'on': True},
            {'key': 'backup', 'on': True}, {'key': 'assistant', 'on': False},
        ]})
        _, data = panel.backup({'ui.theme': 'dark', 'ch.range': 900, 'api_key': 'private'})
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            self.assertEqual(set(tar.getnames()), {'manifest.json',
                'webpanel/alarm/alarms.conf', 'webpanel/override/sounds/common/timer.opus',
                'webpanel/anims/ring/demo.led'})
            manifest = json.load(tar.extractfile('manifest.json'))
            self.assertEqual(manifest['items'], ['alarm'])
            self.assertEqual(manifest['ui'], {'ui.theme': 'dark', 'ch.range': '900'})
            for name in ('alarm/alarms.conf', 'override/sounds/common/timer.opus', 'anims/ring/demo.led'):
                self.assertEqual(tar.extractfile('webpanel/' + name).read(), payload[name])
        adb.push.assert_not_called()

    def test_invalid_archive_refuses_before_reset_or_device_operations(self):
        s = fresh_server()
        for data in (b'not an archive', archive({}),
                     archive({'manifest.json': b'{"format":2}'})):
            with self.subTest(data_size=len(data)):
                panel = s.Panel(Mock())
                panel.footprint_reset = Mock()
                with self.assertRaises(s.AdbError):
                    panel.backup_restore(data)
                panel.footprint_reset.assert_not_called()
                panel.adb.shell.assert_not_called()
                panel.adb.push.assert_not_called()

    def test_restore_pipeline_filters_foreign_files_and_preserves_local_sentinels(self):
        s = fresh_server()
        state = {
            '/etc/init.d/S99zdropbear': b'existing SSH',
            '/data/yam/bin/yam': b'existing package manager',
            '/private/.yandex_auth.json': b'owner token',
            s.WEB_DIR + '/anims/ring/old.led': b'old panel animation',
        }
        sentinels = {k: v for k, v in state.items() if not k.startswith(s.WEB_DIR + '/')}
        adb = Mock()
        adb.push.side_effect = lambda local, remote: state.__setitem__(remote, Path(local).read_bytes())
        panel = s.Panel(adb)
        # The reset contract is simulated here; the real reset remains forbidden
        # on Mini 2. This tests archive routing, not real uninstall/rollback.
        def reset_fixture():
            for path in list(state):
                if path.startswith(s.WEB_DIR + '/'):
                    del state[path]
        panel.footprint_reset = Mock(side_effect=reset_fixture)
        panel.footprint = Mock(return_value={'items': []})
        panel.boot_status = Mock(return_value={'supported': False, 'sound_supported': False})
        panel.sounds = Mock(return_value=['common/timer.opus'])
        panel._install_override = Mock(side_effect=lambda data, custom, target: state.__setitem__(custom, data))
        data = archive({
            'manifest.json': b'{"format":1,"firmware":"test-firmware","ui":{"ui.theme":"dark"}}',
            'webpanel/anims/ring/demo.led': b'restored animation',
            'webpanel/override/sounds/common/timer.opus': b'restored audio',
            'webpanel/override/sounds/absent.wav': b'absent from this firmware',
            'webpanel/boot.binscled.zst': b'unsupported boot animation',
            'webpanel/assistant/config.json': b'foreign API key',
            'webpanel/../../etc/init.d/S99zdropbear': b'overwrite SSH',
            '/data/yam/bin/yam': b'overwrite manager',
        })
        result = panel.backup_restore(data)
        self.assertEqual(state, {**sentinels,
            s.WEB_DIR + '/anims/ring/demo.led': b'restored animation',
            s.OVR_DIR + '/sounds/common/timer.opus': b'restored audio'})
        self.assertEqual(result['ui'], {'ui.theme': 'dark'})
        panel._install_override.assert_called_once_with(b'restored audio',
            s.OVR_DIR + '/sounds/common/timer.opus', s.SOUNDS_DIR + '/common/timer.opus')
        self.assertEqual(adb.push.call_count, 1)


if __name__ == '__main__':
    unittest.main()
