import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def encoders():
    ff = shutil.which("ffmpeg")
    return subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout if ff else ""


def panel(sounds=("common/timer.wav", "common/boot.wav")):
    p = server.Panel(Mock())
    p.sounds = lambda: list(sounds)
    p.ffmpeg = shutil.which("ffmpeg")
    return p


class SoundOverrides(unittest.TestCase):
    """Свои системные звуки: bind mount поверх стандартного, без копий оригиналов."""

    def test_only_listed_sounds(self):
        p = panel()
        for name in ("../../etc/passwd", "common/../../x", "nope.wav"):
            with self.assertRaisesRegex(server.AdbError, "нет такого звука"):
                p.sound_reset(name)
        p.adb.shell.assert_not_called()

    def test_boot_sound_goes_through_boot_tab(self):
        p = panel()
        p.boot_sound_reset, p._remove_override = Mock(), Mock()
        p.sounds_custom = lambda: {"custom": []}
        p.sound_reset("common/boot.wav")
        p.boot_sound_reset.assert_called_once()
        p.sound_reset("common/timer.wav")
        p._remove_override.assert_called_once_with(f"{server.OVR_DIR}/sounds/common/timer.wav",
                                                   f"{server.SOUNDS_DIR}/common/timer.wav")

    def test_custom_list(self):
        p = panel()
        p.adb.shell.return_value = (f"./common/timer.wav\n./ru-RU/x.opus\n@boot\n{server.SOUNDS_DIR}/common/timer.wav\n"
                                    f"{server.SOUNDS_DIR}/common/boot.wav\n", "", 0)
        r = p.sounds_custom()
        self.assertEqual(r["custom"], ["common/boot.wav", "common/timer.wav", "ru-RU/x.opus"])
        self.assertEqual(r["pending"], ["ru-RU/x.opus"])   # лежит, но не смонтирован

    def test_hook_binds_override_tree(self):
        self.assertIn(f'bind "{server.OVR_DIR}/${{f#./}}" "{server.QUASAR}/${{f#./}}"', server.BOOT_HOOK_SCRIPT)

    def test_backup_names_cannot_escape(self):
        ok = server.Panel.BACKUP_FILES.fullmatch
        self.assertTrue(ok("override/sounds/common/timer.wav"))
        for bad in ("override/sounds/../x", "override/sounds/a/../b", "override/sounds/.x", "override/ledpatterns/x"):
            self.assertFalse(ok(bad), bad)

    @unittest.skipUnless("libopus" in encoders() and "libmp3lame" in encoders(), "нужен ffmpeg с libopus и libmp3lame")
    def test_transcodes_to_original_codec(self):
        ff = shutil.which("ffmpeg")
        tone = lambda *args: subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                             "sine=frequency=440:duration=0.3", *args, "pipe:1"],
                                            capture_output=True, check=True).stdout
        opus_mono = tone("-ac", "1", "-c:a", "libopus", "-f", "ogg")
        mp3_stereo = tone("-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-f", "mp3")
        wav = tone("-ac", "1", "-ar", "22050", "-f", "wav")
        p = panel()
        self.assertEqual(p._audio_like(opus_mono, wav)[:4], b"OggS")
        self.assertEqual(p._audio_like(wav, mp3_stereo)[:4], b"RIFF")
        out = p._audio_like(mp3_stereo, opus_mono)
        self.assertTrue(out[:2] == b"\xff\xfb" or out[:3] == b"ID3", out[:4])


if __name__ == "__main__":
    unittest.main()


class AnimOverrides(unittest.TestCase):
    """Свои анимации кольца и часов — те же подмены, что у звуков."""

    def test_led_validation(self):
        good = "loop\nff0000 00ff00 0000ff 200\nsmooth 100\ngradient 0 0000ff 1 0000ff 60000\nrotate ff0000 000000 000000 50\n# —\n"
        self.assertEqual(server.check_led(good), good)
        for bad in ("rm -rf /", "ff0000 00ff00 0000ff 200; reboot", "loop\n$(id)", "loop", "ff0000 1\n" * 10000):
            with self.assertRaises(server.AdbError):
                server.check_led(bad)

    def test_led_parse(self):
        r = server.parse_led("loop\nff0000 00ff00 0000ff 200\n")
        self.assertEqual((r["loop"], r["simple"], r["frames"][0]["c"][1], r["frames"][0]["ms"]), (True, True, [0, 255, 0], 200))
        self.assertFalse(server.parse_led("smooth 10\nff0000 00ff00 0000ff 200\n")["simple"])

    def test_only_listed_animations(self):
        p = server.Panel(Mock())
        p.patterns = lambda: {"ring": ["qc.led"], "clock": ["boot.binscled.zst", "blink.binscled.zst"]}
        for kind, name in (("ring", "../../etc/passwd"), ("clock", "qc.led"), ("evil", "qc.led")):
            with self.assertRaisesRegex(server.AdbError, "нет такой анимации"):
                p.anim_reset(kind, name)
        self.assertEqual(p._anim_paths("ring", "qc.led")[1], f"{server.OVR_DIR}/ledpatterns/oknix/qc.led")
        p.boot_reset, p._remove_override, p.anim_custom = Mock(), Mock(), Mock()
        p.anim_reset("clock", "boot.binscled.zst")
        p.boot_reset.assert_called_once()   # заставка — по-старому, через вкладку «Запуск»
        p._remove_override.assert_not_called()

    def test_custom_list(self):
        p = server.Panel(Mock())
        p.adb.shell.return_value = ("./oknix/qc.led\n./clock/blink.binscled.zst\n./other/x\n@boot\n", "", 0)
        self.assertEqual(p.anim_custom(), {"ring": ["qc.led"], "clock": ["blink.binscled.zst", "boot.binscled.zst"]})

    def test_custom_list_accepts_max_ring_folder(self):
        p = server.Panel(Mock())
        p.adb.shell.return_value = ("./ring/alice_speaking.led\n./clock/blink.binscled.zst\n@boot\n", "", 0)
        self.assertEqual(p.anim_custom(),
                         {"ring": ["alice_speaking.led"], "clock": ["blink.binscled.zst", "boot.binscled.zst"]})

    def test_backup_names(self):
        ok = server.Panel.BACKUP_FILES.fullmatch
        self.assertTrue(ok("override/ledpatterns/oknix/timer.led"))
        for bad in ("override/ledpatterns/x/a", "override/ledpatterns/oknix/../a", "override/ledpatterns/oknix/a/b"):
            self.assertFalse(ok(bad), bad)


class HookStatus(unittest.TestCase):
    """Общая плашка: своё есть, а хука нет или он от старой версии панели."""

    def status(self, out):
        p = server.Panel(Mock())
        p.adb.shell.return_value = (out, "", 0)
        return p.hook_status()

    def test_states(self):
        import hashlib
        md5 = hashlib.md5(server.BOOT_HOOK_SCRIPT.encode()).hexdigest()
        s = self.status(f"@будильник\n@свои звуки\nhook\npossible\n{md5}  {server.BOOT_HOOK}\n")
        self.assertEqual((s["broken"], s["users"]), (False, ["будильник", "свои звуки"]))
        self.assertTrue(self.status("@будильник\nhook\npossible\n0123  x\n")["broken"])   # старый хук
        s = self.status("@будильник\n")
        self.assertEqual((s["broken"], s["possible"]), (True, False))   # подключать не к чему
        self.assertFalse(self.status("possible\n")["broken"])   # своего нет — хук не нужен


class MyAnims(unittest.TestCase):
    """Свои анимации — отдельные файлы в webpanel/anims; встроенные не трогают."""

    def panel(self):
        p = server.Panel(Mock())
        p.patterns = lambda: {"ring": ["timer.led", "my/Закат.led"], "clock": ["boot.binscled.zst", "my/Часы.binscled"]}
        return p

    def test_save(self):
        p = self.panel()
        pushed = []
        p.adb.push = lambda local, remote, *a, **k: pushed.append((Path(local).read_bytes(), remote))
        r = p.my_anim_save("ring", "Мой закат", text="loop\nff0000 00ff00 0000ff 200\n")
        self.assertEqual(r["name"], "my/Мой_закат.led")
        self.assertEqual(pushed[0][1], f"{server.MY_ANIM_DIR}/ring/Мой_закат.led")
        p.my_anim_save("clock", "c1", frames=[{"flag": 0, "v": [255] * len(server.CLOCK_LEDS), "ms": 100}])
        self.assertEqual(pushed[1][1], f"{server.MY_ANIM_DIR}/clock/c1.binscled")
        for bad in ("../x", "a/b", "", "x" * 41, "a;b"):
            with self.assertRaises(server.AdbError, msg=bad):
                p.my_anim_save("ring", bad, text="ff0000 00ff00 0000ff 200")

    def test_paths_and_delete(self):
        p = self.panel()
        self.assertEqual(p._pattern_path("ring", "my/Закат.led"), f"{server.MY_ANIM_DIR}/ring/Закат.led")
        self.assertEqual(p._pattern_path("ring", "timer.led"), f"{server.PATTERNS['ring']}/timer.led")
        with self.assertRaises(server.AdbError):
            p.my_anim_delete("ring", "timer.led")   # встроенную так не удалить
        with self.assertRaises(server.AdbError):
            p.anim_reset("ring", "my/Закат.led")    # и подменой своя не считается


class DefaultAlarmSound(unittest.TestCase):
    """common/timer.wav есть не во всех прошивках (Мини 2, issue #17) — берём похожий штатный."""

    def test_default_alarm_sound_without_timer(self):
        self.assertEqual(panel()._alarm_default_path(), "common/timer.wav")
        p = panel(("common/boot.wav", "alice/alarm_morning.mp3", "common/ping.wav"))
        self.assertEqual(p._alarm_default_path(), "alice/alarm_morning.mp3")
        self.assertEqual(panel(("x/b.wav", "x/a.wav"))._alarm_default_path(), "x/a.wav")
        with self.assertRaises(server.AdbError):
            panel(())._alarm_default_path()

    def test_mini2_opus_and_midi_flat_sound_layouts(self):
        # Names observed by read-only SSH on the two devices, 29 Oct 2026.
        mini2 = panel(('common/notification_sound.opus', 'common/calls_ringing_incoming.mp3',
                       'common/timer.opus'))
        midi = panel(('notification_sound.mp3', 'calls_ringing_incoming.mp3', 'timer.wav'))
        self.assertEqual(mini2._alarm_default_path(), 'common/timer.opus')
        self.assertEqual(midi._alarm_default_path(), 'timer.wav')
