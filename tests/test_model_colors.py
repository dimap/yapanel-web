"""Body colour detection, durable identity-bound choices, and the real guarded HTTP routes."""
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import access
import model_colors as colors
import server

A, B = 'TEST0000000001', 'TEST0000000002'


class ProbeTests(unittest.TestCase):
    def test_shell_probe_distinguishes_missing_empty_failed_and_reported(self):
        for script, raw, read in [(None, '', 'unavailable'), ('exit 0', '', 'empty'),
                                  ('echo blue; exit 1', '', 'error'), ('echo blue', 'blue', 'available')]:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                if script:
                    tool = Path(directory) / 'yandex-getcolor'
                    tool.write_text('#!/bin/sh\n' + script + '\n')
                    tool.chmod(0o700)
                result = subprocess.run(['/bin/sh', '-c', colors.COLOR_PROBE],
                                        env={'PATH': directory}, capture_output=True, text=True, check=True)
                fields = dict(line.split('=', 1) for line in result.stdout.splitlines())
                self.assertEqual(fields, {'color': raw, 'color_read': read})

    def test_native_identity_formats_and_invalid_output(self):
        for value in (A, 'Device ID: ' + A, 'a' * 32):
            self.assertIsNotNone(colors.identity(value))
        for value in ('', None, 'hostname-mini', '192.0.2.1', 'Error: read failed', 'x' * 32):
            self.assertIsNone(colors.identity(value))

    def test_command_failure_never_counts_as_detected_even_with_output(self):
        self.assertEqual(colors.diagnostic('blue', 'error')['state'], 'error')
        self.assertEqual(colors.diagnostic('', 'unavailable')['reason'], 'command_missing')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.model_colors.json'
        self.store = colors.ColorStore(self.path)

    def test_all_supported_palettes_and_aliases_have_explicit_unknown(self):
        for model in [*colors.CATALOG['palettes'], *colors.CATALOG['aliases']]:
            with self.subTest(model=model):
                for key in colors.palette(model):
                    result = self.store.result(A, model, key.upper() + ' ', colors.diagnostic(key, 'available'))
                    self.assertEqual((result['effective'], result['source']), (key, 'device'))
                unknown = self.store.result(A, model, 'new-colour', colors.diagnostic('new-colour', 'available'))
                self.assertEqual((unknown['effective'], unknown['source'], unknown['reason']), (None, 'unknown', 'unsupported'))
        self.assertEqual(colors.normalize('mini3pro', 'gray'), 'grey')
        self.assertIn('green', colors.palette('mini3pro'))

    def test_manual_survives_restart_overrides_detection_and_isolated_by_device_and_model(self):
        self.store.save(A, 'mini3pro', 'green')
        again = colors.ColorStore(self.path)
        result = again.result(A, 'mini3pro', 'blue', colors.diagnostic('blue', 'available'))
        self.assertEqual((result['selected'], result['effective'], result['source']), ('green', 'green', 'user'))
        self.assertEqual(again.selection(B, 'mini3pro'), (False, None))
        self.assertEqual(again.selection(A, 'mini3'), (False, None))
        # No IP address or browser ID is involved in storage.
        self.assertEqual(again.selection(A, 'mini3pro'), (True, 'green'))

    def test_explicit_auto_is_persistent_and_does_not_revive_browser_legacy(self):
        self.store.save(A, 'mini3pro', 'green')
        self.store.save(A, 'mini3pro', None)
        self.assertEqual(colors.ColorStore(self.path).selection(A, 'mini3pro'), (True, None))

    def test_invalid_values_and_unknown_id_never_create_a_file(self):
        for device, model, color in [(A, 'mini3pro', 'purple'), (A, '../mini3pro', 'green'),
                                     (A, 'mini3pro', {}), (None, 'mini3pro', 'green')]:
            with self.subTest(color=color), self.assertRaises(ValueError):
                self.store.save(device, model, color)
        self.assertFalse(self.path.exists())

    def test_atomic_failure_preserves_previous_file_and_other_settings(self):
        settings = self.path.with_name('.panel_settings.json'); settings.write_text('{"keep":true}')
        self.store.save(A, 'mini3pro', 'green'); before = self.path.read_bytes()
        with patch.object(colors.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ValueError, 'сохранить'):
                self.store.save(A, 'mini3pro', 'blue')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(settings.read_text(), '{"keep":true}')
        self.assertEqual(list(self.path.parent.glob('.model-colors-*')), [])

    def test_corruption_is_reported_without_overwriting(self):
        self.path.write_text('{bad'); before = self.path.read_bytes()
        with self.assertRaises(ValueError): self.store.save(A, 'mini3pro', 'green')
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_updates_do_not_drop_other_speakers(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda device: self.store.save(device, 'mini3pro', 'green'), [A, B]))
        self.assertEqual(self.store.selection(A, 'mini3pro'), (True, 'green'))
        self.assertEqual(self.store.selection(B, 'mini3pro'), (True, 'green'))


class FakeAdb:
    def __init__(self):
        self.device, self.color, self.read, self.calls = A, '', 'unavailable', []
        self.serial = '192.0.2.1:5555'
        self.agent = False

    def shell(self, command, **kwargs):
        self.calls.append(command)
        return (f'model_color_device_id={self.device}\ncolor={self.color}\ncolor_read={self.read}\n', '', 0)


class ColorHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = patch.object(server, 'DATA_DIR', self.root); self.data.start(); self.addCleanup(self.data.stop)
        self.adb = FakeAdb(); self.panel = server.Panel(self.adb)
        self.panel.conn = SimpleNamespace(online=True, info=lambda: {'state': 'waiting'})
        self.panel.leds = Mock()
        # These routes are installed by profiles/midi at normal startup; they are unrelated to this fixture.
        for name in ('midi_hardware', 'midi_tests', 'midi_clock_tests', 'midi_test_run', 'midi_test_observe', 'midi_test_restore'):
            setattr(self.panel, name, Mock())
        handler = type('ColorHandler', (server.Handler,), {'panel': self.panel})
        handler.access = access.PanelAccess(self.root / 'auth.json')
        token = handler.access.set_password('', 'test-only-password', 'unittest')
        self.cookie = access.COOKIE + '=' + token
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.http.server_close); self.addCleanup(self.http.shutdown)

    def request(self, body=None, cookie=True, origin=None, path='/api/model-color?model=mini3pro'):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        headers = {'Cookie': self.cookie} if cookie else {}
        if origin: headers['Origin'] = origin
        if body is not None: headers['Content-Type'] = 'application/json'
        conn.request('POST' if body is not None else 'GET', path,
                     body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse(); data = json.loads(response.read()); code = response.status; conn.close()
        return code, data

    def test_read_save_second_client_reset_and_replacement(self):
        code, state = self.request(); self.assertEqual(code, 200)
        self.assertEqual((state['source'], state['reason']), ('unknown', 'command_missing'))
        change = {'model': 'mini3pro', 'color': 'green', 'device_id': state['device_id']}
        self.assertEqual(self.request(change)[1]['effective'], 'green')
        self.assertEqual(self.request()[1]['source'], 'user')
        self.adb.serial = '192.0.2.2:5555'
        self.assertEqual(self.request()[1]['effective'], 'green')
        self.adb.device = B
        self.assertIsNone(self.request()[1]['selected'])
        self.assertEqual(self.request(change)[0], 400, 'stale form must not write to replacement speaker')
        self.adb.device = A
        self.assertIsNone(self.request({**change, 'color': None})[1]['effective'])
        self.assertTrue(self.request()[1]['configured'])

    def test_auth_origin_and_offline_guards_run_before_speaker_reads(self):
        change = {'model': 'mini3pro', 'color': 'green', 'device_id': A}
        for body in (None, change): self.assertEqual(self.request(body, cookie=False)[0], 401)
        self.assertEqual(self.request(change, origin='https://untrusted.example')[0], 403)
        self.assertEqual(self.adb.calls, [])
        self.panel.conn.online = False
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.adb.calls, [])

    def test_bad_model_color_identity_and_failed_write_are_errors(self):
        for body in ({'model': 'mini3pro'}, {'model': 'mini3pro', 'color': []},
                     {'model': 'mini3pro', 'color': 'purple', 'device_id': A},
                     {'model': 'unknown', 'color': None, 'device_id': A}):
            self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(self.adb.calls, [])
        with patch.object(colors.os, 'replace', side_effect=OSError('full')):
            self.assertEqual(self.request({'model': 'mini3pro', 'color': 'green', 'device_id': A})[0], 400)
        self.assertFalse((self.root / '.model_colors.json').exists())

    def test_status_retains_raw_color_and_adds_diagnostics(self):
        self.panel.version = lambda: {'device_id': A}
        self.adb.color, self.adb.read = 'gray', 'available'
        value = self.panel.status()
        self.assertEqual(value['color'], 'gray')
        self.assertEqual(value['color_detection']['state'], 'available')
        self.assertEqual(value['model_color_device_id'], A)


if __name__ == '__main__':
    unittest.main()
