import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import multi
import profiles

ROOT = Path(__file__).resolve().parents[1]


def fresh_server():
    spec = importlib.util.spec_from_file_location('isolated_panel', ROOT/'server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProfileTests(unittest.TestCase):
    def test_default_preserves_lite2_hardware_methods(self):
        s = fresh_server()
        previous = [s.Panel.clock, s.Panel.ring, s.Panel._ensure_hook, s.Adb.shell, s.LedStream]
        profiles.install(s)
        self.assertEqual(previous, [s.Panel.clock, s.Panel.ring, s.Panel._ensure_hook, s.Adb.shell, s.LedStream])
        self.assertEqual(s.GLAGOL_PLATFORM, 'plum')

    def test_assistant_is_experimental_on_other_models(self):
        s = fresh_server()
        profiles.install(s, 'mini2')
        p = s.Panel(Mock())
        calls = []
        p.adb.shell = lambda cmd, *a, **k: (calls.append(cmd), ('armv7l\n' if cmd == 'uname -m' else '@status\n{}\n', '', 0))[1]
        p._assist_config = lambda: {}
        st = p.assistant_status()
        self.assertTrue(st['experimental'])
        with self.assertRaisesRegex(s.AdbError, '64-битное'):
            p.assistant_install()   # 32-битное ядро: программу не кладём
        self.assertEqual(calls[-1], 'uname -m')
        p.assistant_start = lambda: 'started'
        p.adb.shell = lambda cmd, *a, **k: ('aarch64\n' if cmd == 'uname -m' else '', '', 0)
        p._assist_binary = lambda: Path('assistantd')
        p._assist_write = lambda cfg: None
        self.assertEqual(p.assistant_install(), 'started')   # хук Лайт 2 не ставится, но установка проходит

    def test_boot_status_hides_screen_keeps_sound_on_other_models(self):
        s = fresh_server()
        profiles.install(s, 'mini2')
        p = s.Panel(Mock())
        p.adb.shell = lambda cmd, *a, **k: ('s_target\nhook\n', '', 0)
        st = p.boot_status()
        self.assertFalse(st['supported'])
        self.assertTrue(st['sound_supported'])
        self.assertTrue(st['hook'])

    def test_pro_clock_scripts_use_pickle_without_altering_ring_count(self):
        s = fresh_server()
        profiles.install(s, 'mini3pro')
        self.assertIn('pickle:white:clock', s.display_script('date'))
        self.assertNotIn('plum:white:clock', s.display_script('date'))
        self.assertEqual(len(s.CLOCK_LEDS), 36)

    def test_experimental_model_rejects_led_writes_before_adb(self):
        s = fresh_server()
        profiles.install(s, 'mini3')
        p = s.Panel(Mock())
        for action in (lambda: p.clock({'text':'12:34'}), lambda: p.ring({'red':128})):
            with self.assertRaises(s.AdbError): action()
        p.adb.assert_not_called()
        self.assertEqual(p.adb.mock_calls, [])

    def test_mini3_hook_requires_a_boot_route_before_writing(self):
        s = fresh_server()
        profiles.install(s, 'mini3')
        adb = Mock()
        adb.shell.return_value = ('', '', 3)
        p = s.Panel(adb)
        with self.assertRaisesRegex(s.AdbError, 'Автозапуск'):
            p._ensure_hook()
        adb.push.assert_not_called()
        self.assertEqual(adb.shell.call_count, 1)

    def test_mini3_explicit_hook_repair_uses_initd_and_busybox_mount(self):
        s = fresh_server()
        profiles.install(s, 'mini3')
        adb = Mock()
        adb.shell.side_effect = [('', '', 3)] + [('', '', 0)] * 4
        p = s.Panel(adb)
        p._ensure_hook(remount=True)
        self.assertEqual(adb.push.call_count, 1)
        command = adb.shell.call_args.args[0]
        self.assertIn(s.INITD_HOOK, command)
        self.assertIn(s.ROOT_RO, command)
        self.assertNotIn('mount --bind ', s.BOOT_HOOK_SCRIPT)
        self.assertIn('mount -t none -o bind ', s.BOOT_HOOK_SCRIPT)

    def test_lite1_ring_uses_micro_leds_and_keeps_lite2_yam(self):
        s = fresh_server()
        profiles.install(s, 'lite')
        p = s.Panel(Mock())
        calls = []
        p.adb.shell = lambda cmd, *a, **k: (calls.append(cmd), ('10 20 30 0 0 0 1 2 3\n', '', 0))[1]
        p.ring({'red': 255, 'green': 16, 'blue': 1})
        self.assertIn('micro:red:ring1', calls[-1])
        self.assertNotIn('plum:', calls[-1])
        self.assertIn('\\x01\\x10\\xff', calls[-1])           # в fb0 — B, G, R
        self.assertEqual(p.clock_state()['ring'], [[10, 20, 30], [0, 0, 0], [1, 2, 3]])
        self.assertEqual(s.YAM_DIR, '/data/quazar/yam')
        self.assertEqual(s.LED_PREFIX, 'micro')
        self.assertEqual(s.detect_model(SimpleNamespace(shell=lambda *a, **k: ('yandex,station-micro amlogic,axg', '', 0))),
                         ('lite', 'station-micro'))
        self.assertEqual(set(profiles.PROFILES['lite']['tested']), {'alarm', 'sounds', 'say'})   # остальное — на пробу

    def test_model_detection_covers_prune_station_midi_and_no_clock_mini2(self):
        s = fresh_server()
        mini2 = 'yandex,station-mini2 amlogic, axg Yandex Station Mini 2'
        cases = (
            ('yandex,prune-evt2 yandex,prune amlogic, axg Yandex Prune EVT2', ('', 0), ('lite2nc', 'prune')),
            ('yandex,station-midi amlogic, axg Yandex Station Midi', ('', 0), ('station2', 'station-midi')),
            (mini2, ('mini2:red:ring1\nmini2:green:ring1\n', 0), ('mini2nc', 'station-mini2')),
            (mini2, ('mini2:white:clockA1\nmini2:red:ring1\n', 0), ('mini2', 'station-mini2')),
            (mini2, ('', 1), ('mini2', 'station-mini2')),   # ls не удался — не угадываем «без часов»
        )
        for identity, (leds, rc), expected in cases:
            def shell(command, _text=identity, _leds=leds, _rc=rc, **kwargs):
                if 'ls /sys/class/leds' in command:
                    return _leds, '', _rc
                return _text, '', 0
            self.assertEqual(s.detect_model(SimpleNamespace(shell=shell)), expected, identity)

    def test_no_clock_profiles_keep_ring_and_reject_clock_writes(self):
        for model, values, ring_count in (
            ('lite2nc', '10 20 30 0 0 0 1 2 3\n', 3),
            ('mini2nc', ' '.join(str(i) for i in range(18)) + '\n', 6),
        ):
            s = fresh_server()
            profiles.install(s, model)
            p = s.Panel(Mock())
            p.adb.shell = lambda command, *a, **k: (values, '', 0)
            with self.assertRaises(s.AdbError):
                p.clock({'text': '12:34'})
            state = p.clock_state()
            self.assertEqual(state['v'], [0] * 36)
            self.assertEqual(len(state['ring']), ring_count)
            # Профиль «без часов» — новый и непроверенный: помощник и заставка должны это признавать,
            # даже если профиль возвращается из своей ветки раньше общих правок.
            p._assist_config = lambda: {}
            p.adb.shell = lambda command, *a, **k: ('@status\n{}\n', '', 0)
            self.assertTrue(p.assistant_status()['experimental'])
            p.adb.shell = lambda command, *a, **k: ('target\n', '', 0)
            p.adb.read_bytes = lambda path: s.zstd_compress(b'')
            self.assertFalse(p.boot_status()['supported'])

    def test_privacy_toggle_is_offered_where_the_boot_hook_works(self):
        self.assertTrue(profiles.PROFILES['lite2'].get('privacy'))
        self.assertTrue(profiles.PROFILES['lite2nc'].get('privacy'))
        self.assertFalse(profiles.PROFILES['mini2'].get('privacy'))

    def test_mini2_maps_lite2_clock_names_to_its_segments(self):
        s = fresh_server()
        profiles.install(s, 'mini2')
        self.assertEqual((s.LED_PREFIX, s.GLAGOL_PLATFORM, s.YAM_DIR), ('mini2', 'yandexmini_2', '/data/quazar/yam'))
        self.assertIn('L=/tmp/webpanel_clock/', s.display_script('date'))
        self.assertEqual((profiles.MINI2_CLOCK['DH'], profiles.MINI2_CLOCK['DL'], profiles.MINI2_CLOCK['DM']),
                         ('PLUS2', 'PLUS1', 'MINUS'))
        # кадр Мини 2: 28 сегментов цифр, PLUS1, PLUS2, MINUS, DEGREE
        frame = bytes([1, *range(1, 33), 0, 20])
        decoded = s.decode_clock_pattern('x.binscled', frame)
        v = decoded[0]['v']
        self.assertEqual((len(v), v[0], v[6], v[7], v[8], v[32], v[33], v[34], v[35]),
                         (36, 1, 7, 0, 8, 32, 30, 29, 31))
        self.assertEqual(s.encode_clock_pattern(decoded), frame)

    def test_mini2_clamps_clock_brightness_at_hardware_maximum(self):
        s = fresh_server()
        profiles.install(s, 'mini2')
        p = s.Panel(Mock())
        commands = []
        p.adb.shell.side_effect = lambda command, *args, **kwargs: (commands.append(command), ('', '', 0))[1]
        p.clock({'v': [255] * len(s.CLOCK_LEDS)})
        self.assertIn('echo 170 > /tmp/webpanel_clock/DH/brightness', commands[-1])
        p.adb.push.side_effect = lambda path, destination: commands.append(Path(path).read_text(encoding='utf8'))
        p.display_set({'mode': 'date', 'brightness': 255})
        self.assertTrue(any('B=170' in command for command in commands))

    def test_mini2_profile_reports_clock_maximum(self):
        self.assertEqual(profiles.PROFILES['mini2']['clock_max'], 170)

    def test_mini2_say_is_confirmed(self):
        self.assertIn('say', profiles.PROFILES['mini2']['tested'])

    def test_mini2_uses_common_volume_instead_of_unsupported_bt_mixer(self):
        s = fresh_server()
        profiles.install(s, 'mini2')
        p = s.Panel(Mock())
        p.glagol = Mock(ws=None)
        with self.assertRaisesRegex(s.AdbError, 'общую громкость'):
            p.set_volume({'bt': 128})
        p.adb.assert_not_called()
        self.assertFalse(profiles.PROFILES['mini2']['bt_volume'])

    def test_mini2_creates_clock_links_only_for_clock_commands(self):
        s = fresh_server()
        sent = []
        def device(adb, command, timeout=15, check=True):
            sent.append(command)
            marker = command.split("'")[1].split('\\n')[0]
            return f'{marker}\n\n{marker}RC=0\n', '', 0
        s.Adb.shell = device
        profiles.install(s, 'mini2')
        s.Adb.shell(Mock(), 'echo 1 > /tmp/webpanel_clock/A0/brightness')
        s.Adb.shell(Mock(), 'echo ok')
        self.assertIn('ln -s /sys/class/leds/mini2:white:clock', sent[0])
        self.assertNotIn('ln -s', sent[1])

    def test_windows_adb_double_carriage_returns_on_every_model(self):
        # adb.exe на Windows бывает отдаёт \r\r\n — исправлено в Adb.shell, значит и для Лайт 2
        for model in ('lite2', 'mini2'):
            s = fresh_server()
            profiles.install(s, model)
            def run(cmd, **kwargs):
                text = 'ok\r\r\n'
                found = re.search(r'__PANEL_[0-9a-f]+__', cmd[-1])
                if found:   # обёртка других моделей — с маркерами
                    marker = found.group(0)
                    text = f'{marker}\r\r\nok\r\r\n{marker}RC=0\r\r\n'
                text += f'\r\r\n{s.Adb.RC_MARK}0\r\r\n'   # код выхода из Adb.shell
                return Mock(returncode=0, stdout=text.encode(), stderr=b'')
            with patch.object(s.subprocess, 'run', run):
                out, _, code = s.Adb.shell(s.Adb('adb', 'device'), 'echo ok')
            self.assertEqual((out.strip(), code), ('ok', 0), model)
            self.assertNotIn('\r', out)

    def test_other_profiles_block_boot_screen_and_restore_but_try_alarms(self):
        for model in ('station2', 'mini3pro', 'mini3', 'mini2'):
            s = fresh_server()
            s.Handler.do_POST = Mock()
            original = s.Handler.do_POST
            profiles.install(s, model)
            for path in ('/api/clock/boot', '/api/clock/boot/reset', '/api/backup/restore', '/api/footprint/reset'):
                request = SimpleNamespace(path=path, _guard=lambda _:False, _send=Mock())
                s.Handler.do_POST(request)
                self.assertEqual(request._send.call_args.args[0], 400)
            original.assert_not_called()
            # звук запуска, «когда показывать», будильник и ассистент — на пробу
            for path in ('/api/boot/sound/reset', '/api/clock/boot/always', '/api/alarms', '/api/assistant/install'):
                s.Handler.do_POST(SimpleNamespace(path=path, _guard=lambda _:False, _send=Mock()))
            self.assertEqual(original.call_count, 4)

    def test_led_panel_only_on_station2(self):
        # LED-панель (/dev/fb0 через led-play) есть только у Станции 2: другим моделям — 400 до обращения к adb
        for model, allowed in (('station2', True), ('lite2', False), ('mini3pro', False), ('mini3', False), ('mini2', False)):
            s = fresh_server()
            s.Handler.do_POST, s.Handler.do_GET = Mock(), Mock()
            post, get = s.Handler.do_POST, s.Handler.do_GET
            profiles.install(s, model)
            for path, handler in (('/api/led', s.Handler.do_POST), ('/api/led/gif', s.Handler.do_POST),
                                  ('/api/led/patterns', s.Handler.do_GET)):
                request = SimpleNamespace(path=path, _guard=lambda _: False, _send=Mock())
                handler(request)
                if allowed:
                    request._send.assert_not_called()
                else:
                    self.assertEqual(request._send.call_args.args[0], 400, (model, path))
            self.assertEqual((post.call_count, get.call_count), (2, 1) if allowed else (0, 0), model)
            # /api/leds/live — не LED-панель, проходит на всех моделях
            s.Handler.do_GET(SimpleNamespace(path='/api/leds/live', _guard=lambda _: False, _send=Mock()))
            self.assertEqual(get.call_count, 2 if allowed else 1, model)
            # фронт прячет пункт меню по флагу из /api/profile
            request = SimpleNamespace(path='/api/profile', _guard=lambda _: False, _send=Mock())
            s.Handler.do_GET(request)
            self.assertEqual(request._send.call_args.args[1]['ledpanel'], allowed, model)


class ConfigTests(unittest.TestCase):
    def load(self, speakers):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'speakers.json'
            path.write_text(json.dumps({'speakers':speakers}), encoding='utf8')
            return multi.load_config(path)

    def test_multiple_instances_of_one_model_have_separate_ids(self):
        value = self.load([dict(id=name, model='lite2', device='example.local') for name in ('room-a','room-b')])
        self.assertEqual(list(value), ['room-a','room-b'])

    def test_invalid_config_is_rejected_before_launch(self):
        valid = dict(id='room', model='lite2', device='example.local')
        for speakers in ([], [valid,valid], [dict(valid,id='../room')], [dict(valid,model='unknown')],
                         [dict(valid,device='two addresses')], [dict(valid,name='')]):
            with self.subTest(speakers=speakers), self.assertRaises(ValueError): self.load(speakers)


if __name__ == '__main__': unittest.main()


class TrialFeatures(unittest.TestCase):
    """Новые функции — на всех моделях на пробу; подтверждённые людьми (tested) — без предупреждения."""

    def test_lite2_tested_everything_others_trial(self):
        self.assertEqual(set(profiles.PROFILES['lite2']['tested']), set(profiles.TRIAL_FEATURES))
        for model, p in profiles.PROFILES.items():
            self.assertTrue(set(p.get('tested', ())) <= set(profiles.TRIAL_FEATURES), model)

    def test_assistant_warning_follows_tested(self):
        for tested, expect in (((), True), (('assistant',), False)):
            s = fresh_server()
            saved = profiles.PROFILES['mini2'].get('tested')
            profiles.PROFILES['mini2']['tested'] = tested
            try:
                profiles.install(s, 'mini2')
            finally:
                if saved is None:
                    profiles.PROFILES['mini2'].pop('tested')
                else:
                    profiles.PROFILES['mini2']['tested'] = saved
            p = s.Panel(Mock())
            p.adb.shell = lambda cmd, *a, **k: ('@status\n{}\n', '', 0)
            p._assist_config = lambda: {}
            self.assertEqual(p.assistant_status()['experimental'], expect)
