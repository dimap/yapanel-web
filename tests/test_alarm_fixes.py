"""Четыре регрессии будильника: shell, Mono, Windows TZ и срок повтора."""
import datetime as dt
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class AlarmFixes(unittest.TestCase):
    def test_windows_fixed_offset_is_disclosed_for_both_schedules(self):
        with patch.object(server.os, 'name', 'nt'):
            p = server.Panel(Mock())
            p.adb.shell.return_value = ('', '', 0)
            with patch.object(p, '_pc_tz', return_value=('UTC+03:00', b'')):
                for state in (p.alarms(), p.scenes()):
                    self.assertIn('После перевода часов', state['timezone_note'])
        with patch.object(server.os, 'name', 'posix'):
            self.assertEqual(server.Panel._schedule_timezone_note(), '')

    def shell(self, script, **kwargs):
        shell = shutil.which('sh')
        if not shell:
            self.skipTest('Нужен sh')
        return subprocess.run([shell, *kwargs.pop('args', []), '-c', script],
                              capture_output=True, text=True, timeout=5, **kwargs)

    def test_test_button_and_restart_generate_valid_shell(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                adb = Mock()
                server.Panel(adb)._alarm_daemon(restart=restart)
                result = self.shell(adb.shell.call_args.args[0], args=['-n'])
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_volume_reader_accepts_mono_and_front_left(self):
        reader = next(s for s in server.alarm_script().decode().splitlines() if s.startswith('get_alsa()'))
        for line, expected in [('Mono: Playback 162 [64%]', '162'),
                               ('Front Left: Playback 87 [34%]\nFront Right: Playback 87 [34%]', '87')]:
            r = self.shell('amixer() { printf "%s\\n" "$MIXER"; }; ' + reader + '\nget_alsa',
                           env={**os.environ, 'MIXER':line})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), expected)

    def test_windows_offset_round_trip(self):
        for offset in (0, 18000, -12600, 20700):
            with patch.object(server.os, 'name', 'nt'), patch('datetime.datetime') as clock:
                clock.now.return_value.astimezone.return_value.utcoffset.return_value = dt.timedelta(seconds=offset)
                name, data = server.Panel._pc_tz()
            zone = ZoneInfo.from_file(io.BytesIO(data))
            self.assertTrue(name.startswith('UTC'))
            for year in (2026, 2040):
                self.assertEqual(dt.datetime(year, 1, 1, tzinfo=zone).utcoffset(), dt.timedelta(seconds=offset))

    @unittest.skipUnless(os.name == 'nt', 'Настройки Windows')
    def test_real_windows_offset(self):
        _, data = server.Panel._pc_tz()
        zone = ZoneInfo.from_file(io.BytesIO(data))
        now = dt.datetime.now(dt.timezone.utc)
        self.assertEqual(now.astimezone(zone).utcoffset(), now.astimezone().utcoffset())

    def test_snooze_fires_between_minute_boundaries(self):
        source = server.alarm_script().decode()
        loop = source[source.index('check_minute() {'):]
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d/'snooze').write_bytes(b'103|a1\n')
            stubs = '''
SNOOZE="$TEST_DIR/snooze"; CONF="$TEST_DIR/missing"; STATE="$TEST_DIR/state"
FIRE="$TEST_DIR/fire"; SFIRE="$TEST_DIR/scene"; LAST="$TEST_DIR/last"
epoch=100
date() { case "$1" in +%s) echo "$epoch";; +%S) echo 10;; +%H:%M) echo 07:30;; +%u) echo 1;; +%Y%m%d%H%M) echo 202611020730;; esac; }
sleep() { epoch=$((epoch+1)); [ "$epoch" -le 105 ] || exit 90; }
log() { :; }; boot_scenes() { :; }; check_scenes() { :; }
run_id() { echo "$1:$epoch" > "$TEST_DIR/fired"; exit 0; }
'''
            result = self.shell(stubs+loop, env={**os.environ, 'TEST_DIR':d.as_posix()})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((d/'fired').read_text().strip(), 'a1:103')
            self.assertFalse((d/'snooze').exists())
