import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import profiles
from test_profiles import fresh_server


@unittest.skip('Legacy Mini 3 Pro profile test (Station Max specialized)')
class ShellRestore(unittest.TestCase):
    def test_clock_restore_advances_elapsed_minute(self):
        s=fresh_server();profiles.install(s,'mini3pro')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);leds=root/'leds';leds.mkdir();s.LEDS=str(leds)
            for name in s.CLOCK_LEDS:
                node=leds/('pickle:white:clock'+name);node.mkdir();(node/'brightness').write_text('0')
            for index,digit in enumerate('1000'):
                for segment in s.SEGMENTS[digit]:(leds/f'pickle:white:clock{segment}{index}'/'brightness').write_text('180')
            (root/'date').write_text('#!/bin/sh\ncat "'+str(root/'now')+'"\n');(root/'date').chmod(0o755)
            env={**os.environ,'PATH':str(root)+os.pathsep+os.environ['PATH']}
            (root/'now').write_text('36000')
            subprocess.run(['sh','-c',s.clock_restore_sh()+'\nsnap_save '+str(root/'snapshot')],env=env,check=True)
            for node in leds.glob('*/brightness'):node.write_text('0')
            (root/'now').write_text('36060')
            subprocess.run(['sh','-c',s.clock_restore_sh()+'\nclock_restore '+str(root/'snapshot')],env=env,check=True)
            for index,digit in enumerate('1001'):
                actual=''.join(seg for seg in 'ABCDEFG' if int((leds/f'pickle:white:clock{seg}{index}'/'brightness').read_text())>0)
                self.assertEqual(actual,s.SEGMENTS[digit])

    def test_ring_restores_brightness_and_reattaches_framebuffer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'state';names=[]
            for index in range(16):
                for color in ('red','green','blue'):
                    node=root/f'pickle:{color}:ring{index}';node.mkdir();names.append(node)
                    (node/'brightness').write_text('7');(node/'trigger').write_text('[none] ledfb-ring ledfb-ring-sync')
            subprocess.run(['sh','-c',profiles.mini3pro_ring_script((1,2,3),str(root),str(state))],check=True)
            self.assertEqual(len(state.read_text().splitlines()),48)
            self.assertIn('none',state.read_text())
            subprocess.run(['sh','-c',profiles.mini3pro_ring_script(None,str(root),str(state))],check=True)
            for node in names:
                self.assertEqual((node/'brightness').read_text().strip(),'7')
                self.assertEqual((node/'trigger').read_text().strip(),'ledfb-ring')
            self.assertFalse(state.exists())

    def test_ring_preflight_failure_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);node=root/'pickle:red:ring0';node.mkdir()
            (node/'brightness').write_text('77');(node/'trigger').write_text('[none] timer')
            result=subprocess.run(['sh','-c',profiles.mini3pro_ring_script((1,2,3),str(root),str(root/'state'))],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual((node/'brightness').read_text(),'77')
