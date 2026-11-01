import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import access
import glagol
import server


class GlagolOnlyAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.panel = server.GlagolPanel()
        self.panel.auth = glagol.YandexAuth(Path(self.tmp.name) / 'auth.json')
        self.panel.glagol = Mock(host='192.0.2.10', state=None)
        self.panel.glagol.info.return_value = dict(connected=False, error=None, state=None, updated=0)
        self.panel.glagol.send.return_value = {'ok': True}
        self.panel.conn = server.GlagolConnection(self.panel)
        self.handler = type('GlagolHandler', (server.Handler,), {})
        self.handler.panel = self.panel
        self.handler.access = access.PanelAccess(Path(self.tmp.name) / 'panel.json')
        self.handler.updater = Mock()
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), self.handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, data=None, **headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=5)
        try:
            conn.request(method, path, None if data is None else json.dumps(data),
                         {'Content-Type': 'application/json', **headers})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_login_and_player_available_before_websocket_connects(self):
        self.assertIsNone(self.panel.adb)
        for path in ['/api/auth', '/api/music', '/api/status']:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], 200)
        code, info = self.request('GET', '/api/conn')
        self.assertEqual(code, 200)
        self.assertEqual(info['state'], 'auth_required')
        self.assertFalse(info['connected'])

    def test_system_routes_rejected_even_with_live_glagol(self):
        self.panel.glagol.info.return_value['connected'] = True
        for method, path in [('GET', '/api/fs/list'), ('GET', '/api/term'),
                             ('GET', '/api/metrics'), ('GET', '/api/conn/find'),
                             ('POST', '/api/shell'), ('POST', '/api/agent/install'),
                             ('POST', '/api/conn/use'), ('POST', '/api/clock/boot')]:
            with self.subTest(method=method, path=path):
                code, body = self.request(method, path, {} if method == 'POST' else None)
                self.assertEqual(code, 409)
                self.assertTrue(body['requires_adb'])
        self.panel.glagol.send.assert_not_called()

    def test_csrf_is_checked_before_player_commands(self):
        code, _ = self.request('POST', '/api/music/cmd', {'command': 'stop'}, Origin='https://example.org')
        self.assertEqual(code, 403)
        self.panel.glagol.send.assert_not_called()

    def test_panel_password_still_protects_player(self):
        self.handler.access.set_password(None, 'test-password-only', 'unittest')
        self.assertEqual(self.request('GET', '/api/music')[0], 401)
        self.assertEqual(self.request('GET', '/api/panel-auth')[0], 200)

    def test_volume_has_no_alsa_fallback(self):
        self.assertEqual(self.request('POST', '/api/volume', {'level': 43})[0], 200)
        self.panel.glagol.send.assert_called_once_with({'command': 'setVolume', 'volume': 0.45})
        self.assertEqual(self.request('POST', '/api/volume', {'master': 128})[0], 400)
        self.panel.glagol.send.reset_mock()
        self.panel.glagol.send.side_effect = glagol.AuthError('not connected')
        self.assertEqual(self.request('POST', '/api/volume', {'level': 50})[0], 400)

    def test_bluetooth_music_does_not_read_avrcp(self):
        state = {'playerState': {'playerType': 'bluetooth', 'title': 'test track'}}
        self.panel.glagol.info.return_value['state'] = state
        self.assertEqual(self.request('GET', '/api/music')[1]['glagol']['state'], state)
        self.assertIsNone(self.panel.adb)

    def test_retry_reconnects_glagol(self):
        self.assertEqual(self.request('POST', '/api/conn/retry', {})[0], 200)
        self.panel.glagol.reconnect.assert_called_once_with()

    def test_connection_state_does_not_claim_websocket_before_login(self):
        self.panel.auth.data = {'music_token': 'test-token'}
        # Stub account metadata, rather than use a real account/token.
        self.panel.auth.info = lambda: {'logged_in': True}
        g = self.panel.glagol.info.return_value
        self.assertEqual(self.panel.conn.info()['state'], 'connecting')
        g['error'] = 'test error'
        self.assertEqual(self.panel.conn.info()['state'], 'glagol_error')
        g.update(error=None, connected=True)
        self.assertTrue(self.panel.conn.online)
        self.assertEqual(self.panel.conn.info()['state'], 'online')


class GlagolOnlyStartup(unittest.TestCase):
    def test_starts_and_shuts_down_without_adb_on_path(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            ready = Path(temp) / 'ready.json'
            env = {**os.environ, 'PATH': temp, 'PANEL_READY_FILE': str(ready)}
            env.pop('PANEL_GATEWAY_SECRET', None)
            env.pop('PANEL_INSTALL', None)
            log = Path(temp) / 'stdout.txt'
            with log.open('w') as output:
                proc = subprocess.Popen([sys.executable, str(root / 'server.py'),
                    '--transport', 'glagol', '--speaker-host', '192.0.2.10',
                    '--device-id', 'test-device', '--model', 'lite2', '--port', '0',
                    '--https-port', '0', '--data-dir', temp], env=env,
                    stdout=output, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(ready.exists(), log.read_text())
                    port = json.loads(ready.read_text())['port']
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    try:
                        conn.request('GET', '/api/music')
                        response = conn.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertFalse(json.loads(response.read())['auth']['logged_in'])
                    finally:
                        conn.close()
                finally:
                    proc.terminate()
                    proc.wait(timeout=5)
                self.assertEqual(proc.returncode, 0, log.read_text())

    def test_cli_rejects_incomplete_or_unsafe_settings(self):
        root = Path(__file__).resolve().parents[1]
        base = [sys.executable, str(root / 'server.py'), '--transport', 'glagol']
        valid = ['--model', 'lite2', '--speaker-host', '192.0.2.10', '--device-id', 'test-device']
        for args in [[], valid + ['--find'], valid + ['--glagol-port', '0'],
                     valid + ['--speaker-host', 'https://example.org'],
                     valid + ['--device-id', '../secret']]:
            with self.subTest(args=args):
                p = subprocess.run(base + args, capture_output=True, timeout=5)
                self.assertEqual(p.returncode, 2, p.stderr.decode())


if __name__ == '__main__':
    unittest.main()
