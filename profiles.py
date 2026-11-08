"""Hardware profile adapter for Yandex Station Max (yandexstation_2)."""
import json
import max_profile

PROFILES = {
    'max': dict(
        name='Станция Макс',
        platform='yandexstation_2',
        led_prefix='max',
        clock=True,
        boot=True,
        alarms=False,
        scenes=False,
        assistant=True,
        apps=True,
        light=True,
        privacy=True,
        matrix=True,
        matrix_cols=25,
        matrix_rows=16,
        ring_leds=24,
        tested=('anims', 'sounds', 'say', 'viz', 'privacy', 'yam2', 'assistant'),
    ),
}

TRIAL_FEATURES = ("assistant", "scenes", "alarm", "anims", "sounds", "say", "yam2", "btout", "btpower", "privacy", "viz")


def install(s, model='max', auto=False):
    """Install hardware profile adapter for Station Max."""
    model = 'max'
    profile = dict(PROFILES['max'], id='max', auto=auto)
    profile['tested'] = list(profile.get('tested', ()))
    profile['ledpanel'] = False
    profile['hook_supported'] = True
    profile['ring_preview_supported'] = True
    profile['light_note'] = 'Анимация Алисы может заменить выбранный цвет.'

    s.GLAGOL_PLATFORM = profile['platform']
    s.LED_PREFIX = profile.get('led_prefix', profile['platform'])

    original_get = s.Handler.do_GET
    original_post = s.Handler.do_POST

    def do_get(self):
        path = self.path.split('?')[0]
        if path == '/api/profile':
            if not self._guard('/api/profile'):
                self._send(200, profile)
            return
        if path.startswith('/api/led/'):
            if not self._guard(path):
                self._send(400, {'error': 'LED-панель не поддерживается профилем max'})
            return
        original_get(self)

    def do_post(self):
        path = self.path.split('?')[0]
        if path == '/api/led' or path.startswith('/api/led/'):
            if not self._guard(path):
                self._send(400, {'error': 'LED-панель не поддерживается профилем max'})
            return
        original_post(self)

    s.Handler.do_GET = do_get
    s.Handler.do_POST = do_post

    # Install Station Max hardware adapter
    max_profile.install(s, profile)
