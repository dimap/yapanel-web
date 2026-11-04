"""Missing host audio tools must fail before remote file/mount changes."""
import unittest
from unittest.mock import Mock

import server


class FfmpegPreflight(unittest.TestCase):
    def panel(self):
        panel=server.Panel(Mock());panel.ffmpeg=None
        panel.adb.shell.return_value=('', '', 0)
        return panel

    def test_sound_replacement_refuses_before_reading_a_mounted_original(self):
        panel=self.panel();panel._original_bytes=Mock();panel._install_override=Mock()
        with self.assertRaisesRegex(server.AdbError,'ffmpeg'):
            panel.sound_replace('timer.wav',b'synthetic replacement')
        panel._original_bytes.assert_not_called();panel._install_override.assert_not_called()
        panel.adb.shell.assert_not_called();panel.adb.push.assert_not_called()

    def test_upload_playback_boot_and_alarm_sound_fail_before_remote_changes(self):
        for method,args in [('play_upload',('demo.mp3',b'synthetic')),('boot_sound_install',('demo.mp3',b'synthetic')),
                            ('alarm_sound_upload',('demo.mp3',b'synthetic'))]:
            panel=self.panel()
            with self.subTest(method=method),self.assertRaisesRegex(server.AdbError,'ffmpeg'):
                getattr(panel,method)(*args)
            panel.adb.shell.assert_not_called();panel.adb.push.assert_not_called()

    def test_missing_alarm_audio_uses_only_a_read_before_refusing(self):
        panel=self.panel()
        with self.assertRaisesRegex(server.AdbError,'ffmpeg'):
            panel._alarm_sounds_ready({'s_fake.wav':'timer.wav'})
        self.assertEqual(panel.adb.shell.call_count,1)
        command=panel.adb.shell.call_args.args[0]
        for write in ('mkdir','mount',' > ','cp ','rm '):self.assertNotIn(write,command)
        panel.adb.push.assert_not_called();panel.adb.read_bytes.assert_not_called()

    def test_existing_prepared_alarm_audio_does_not_require_host_transcoding(self):
        panel=self.panel();panel.adb.shell.return_value=('default.wav\ns_fake.wav\n','',0)
        panel._alarm_sounds_ready({'s_fake.wav':'timer.wav'})
        self.assertEqual(panel.adb.shell.call_count,1);panel.adb.push.assert_not_called()


if __name__=='__main__':unittest.main()
