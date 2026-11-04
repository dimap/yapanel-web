"""Loopback-only browser fixture. It has no ADB connection and never contacts a speaker."""
import json
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import access
import server
from test_model_colors import FakeAdb


class FixturePanel(server.Panel):
    model = 'mini3pro'
    fail_save = False

    def status(self):
        return {'hostname': 'Test station', 'hardware_model': '', 'color': self.adb.color,
                'model_color_device_id': self.adb.device,
                'color_detection': server.model_colors.diagnostic(self.adb.color, self.adb.read)}

    def model_color(self, model, change=None):
        if change is not None and self.fail_save:
            raise ValueError('Не удалось сохранить цвет на сервере панели')
        return super().model_color(model, change)


# The actual model bootstrap, settings, catalogue, renderer and styles run in the browser.
HTML = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="static/model.css">
<style>body{font:16px system-ui;background:#f6f6f8;color:#17171e;margin:0;padding:16px;--danger:#b42318;--text:#17171e;--muted:#666;--line:#ddd;--field:#fff}
main{max-width:640px;margin:auto}.model-view{position:relative;width:100%;height:340px}.model-view canvas{position:absolute;inset:0;width:100%;height:100%}
.field,.btn{font:inherit;border:1px solid #ccd;border-radius:8px;padding:8px;background:white}.btn{cursor:pointer}.hint{font-size:14px;color:#666}.set-item{display:flex;gap:12px;justify-content:space-between;align-items:center;margin:12px 0}.set-label small{display:block;font-size:12px}#model-settings{margin-top:20px}.set-ctl{max-width:55%}select{max-width:100%}h1{font-size:24px}
</style><script type="importmap">{"imports":{"three":"./static/vendor/three/three.module.js"}}</script>
<script src="static/panel-context.js"></script></head><body><main>
<h1 id="model-name">Станция</h1><section class="model-widget"><div class="model-view" id="model-view"><canvas id="model-viz"></canvas></div><div id="model-color-panel" class="model-color-panel" hidden></div></section>
<div id="model-settings" hidden></div><div data-widget="light" hidden><p class="hint"></p></div>
<script type="module">import * as G from './static/clock-geometry.js';window.clockGeom=G;
await import('./static/model.js');window.__fixtureReady=true;
window.refreshFixture=async()=>{const r=await PanelContext.fetch('/api/status');await window.__modelColorController?.updateStatus(r.ok?await r.json():null);};
</script></main></body></html>'''


class Handler(server.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/': return self._send(200, HTML.encode(), 'text/html; charset=utf-8')
        if path == '/api/profile':
            return self._send(200, {'id': self.panel.model, 'name': 'Тестовая станция', 'clock': True,
                                    'boot': True, 'light': True, 'tested': ['anims', 'scenes', 'alarm', 'say', 'sounds']})
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == '/__test__/configure':
            d = self._json()
            for name in ('device', 'color', 'read'):
                if name in d: setattr(self.panel.adb, name, d[name])
            for name in ('model', 'fail_save'):
                if name in d: setattr(self.panel, name, d[name])
            self.panel.conn.online = d.get('online', True)
            if d.get('clear'):
                (server.DATA_DIR / '.model_colors.json').unlink(missing_ok=True)
            return self._send(200, {'ok': True})
        return super().do_POST()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='panel-color-test-') as data:
        server.set_data_dir(data)
        panel = FixturePanel(FakeAdb())
        panel.conn = SimpleNamespace(online=True, info=lambda: {'state': 'waiting'})
        panel.leds = Mock()
        for name in ('midi_hardware', 'midi_tests', 'midi_clock_tests', 'midi_test_run', 'midi_test_observe', 'midi_test_restore'):
            setattr(panel, name, Mock())
        Handler.panel = panel
        Handler.access = access.PanelAccess(Path(data) / 'auth.json')
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as http:
            print(json.dumps({'url': 'http://127.0.0.1:' + str(http.server_port)}), flush=True)
            http.serve_forever()
