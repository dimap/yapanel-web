"""Exercise the real scheduler in a temporary directory; no daemon, ADB or audio."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AlarmScheduler(unittest.TestCase):
    @unittest.skipUnless(shutil.which('sh'), 'POSIX shell required; runs in Linux CI')
    def test_due_alarm_only_once_and_snooze(self):
        script = (ROOT / 'device/alarmd.sh').read_text(encoding="utf-8")
        function = script.split('check_minute() {', 1)[1].split('\necho idle > $STATE', 1)[0]
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'conf').write_text('a1|1|07:30|3|sound|\n', encoding="utf-8")
            (d / 'snooze').write_text('99|b2\n', encoding="utf-8")
            harness = r'''
CONF="$TEST_DIR/conf"; SNOOZE="$TEST_DIR/snooze"; LAST="$TEST_DIR/last"
date() { case "$1" in +%H:%M) echo 07:30;; +%u) echo 3;; +%Y%m%d%H%M) echo 202610280730;; +%s) echo 100;; esac; }
run_id() { echo "$1" >> "$TEST_DIR/fired"; }
check_scenes() { :; }
check_minute() {
''' + function + '\ncheck_minute\ncheck_minute\n'
            subprocess.run(['sh', '-c', harness], env={**os.environ, 'TEST_DIR': td},
                           check=True, timeout=5, capture_output=True, text=True)
            self.assertEqual((d / 'fired').read_text(encoding="utf-8").splitlines(), ['b2', 'a1'])
            self.assertFalse((d / 'snooze').exists())


if __name__ == '__main__':
    unittest.main()
