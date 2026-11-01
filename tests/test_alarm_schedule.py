"""Exercise the real scheduler in a temporary directory; no daemon, ADB or audio."""
import datetime as dt
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AlarmScheduler(unittest.TestCase):
    def test_alarm_and_scene_use_copied_timezone_across_midnight(self):
        # Issue #37: 22:17 UTC Saturday is 01:17 Sunday in Moscow.
        source = (ROOT / 'device/alarmd.sh').read_text()
        scenes = 'check_scenes() {' + source.split('check_scenes() {', 1)[1].split('\n# После включения', 1)[0]
        minute = 'check_minute() {' + source.split('check_minute() {', 1)[1].split('\n}', 1)[0] + '\n}'
        epoch = int(dt.datetime(2026, 10, 31, 22, 17, tzinfo=dt.timezone.utc).timestamp())
        zone = Path('/usr/share/zoneinfo/Europe/Moscow')
        if not zone.exists():
            self.skipTest('requires Moscow TZif')
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'localtime').write_bytes(zone.read_bytes())
            for timezone, expected in [('UTC0', []), (':' + str(d / 'localtime'), ['scene:s1', 'alarm:a1'])]:
                with self.subTest(timezone=timezone):
                    (d / 'conf').write_text('a1|1|01:17|7|sound|\n')
                    (d / 'scenes').write_text('S|s1|1|time|01:17|7|n1|test\n')
                    (d / 'fired').write_text('')
                    (d / 'last').write_text('')
                    harness = r'''
CONF="$TEST_DIR/conf"; SCENES="$TEST_DIR/scenes"; LAST="$TEST_DIR/last"
SNOOZE="$TEST_DIR/snooze"
date() {
    if command date -d "@0" +%s >/dev/null 2>&1; then
        command date -d "@$TEST_EPOCH" "$@"
    else
        command date -r "$TEST_EPOCH" "$@"
    fi
}
run_id() { echo "alarm:$1" >> "$TEST_DIR/fired"; }
run_scene() { echo "scene:$1" >> "$TEST_DIR/fired"; }
''' + scenes + minute + '\ncheck_minute\ncheck_minute\n'
                    subprocess.run(['sh', '-c', harness], check=True, timeout=5,
                                   capture_output=True, text=True,
                                   env={**os.environ, 'TEST_DIR': td, 'TEST_EPOCH': str(epoch), 'TZ': timezone})
                    self.assertEqual((d / 'fired').read_text().splitlines(), expected)

    def test_due_alarm_only_once_and_snooze(self):
        script = (ROOT / 'device/alarmd.sh').read_text()
        function = script.split('check_minute() {', 1)[1].split('\necho idle > $STATE', 1)[0]
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'conf').write_text('a1|1|07:30|3|sound|\n')
            (d / 'snooze').write_text('99|b2\n')
            harness = r'''
CONF="$TEST_DIR/conf"; SNOOZE="$TEST_DIR/snooze"; LAST="$TEST_DIR/last"
date() { case "$1" in +%H:%M) echo 07:30;; +%u) echo 3;; +%Y%m%d%H%M) echo 202610280730;; +%s) echo 100;; esac; }
run_id() { echo "$1" >> "$TEST_DIR/fired"; }
check_scenes() { :; }
check_minute() {
''' + function + '\ncheck_minute\ncheck_minute\n'
            subprocess.run(['sh', '-c', harness], env={**os.environ, 'TEST_DIR': td},
                           check=True, timeout=5, capture_output=True, text=True)
            self.assertEqual((d / 'fired').read_text().splitlines(), ['b2', 'a1'])
            self.assertFalse((d / 'snooze').exists())


if __name__ == '__main__':
    unittest.main()
