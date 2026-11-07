"""Run the real preload library against a fake ALSA reader, with real Unix IPC."""
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('gcc'), 'Linux and a native C compiler required')
class MicrophoneTap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mic-tap-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.sockpath = self.dir / 'audio.sock'
        self.lease = self.dir / 'exclusive'
        (self.dir / 'fake.c').write_text('''
long snd_pcm_readi(void *p, void *buffer, unsigned long frames) {
 (void)p; short *b=buffer;
 for(unsigned long i=0;i<frames;i++){b[i*3]=100;b[i*3+1]=200;b[i*3+2]=300;}
 return frames;
}
long snd_pcm_mmap_readi(void *p,void *buffer,unsigned long frames) {
 return snd_pcm_readi(p,buffer,frames);
}
int snd_pcm_stream(void *p){(void)p;return 1;}
long snd_pcm_frames_to_bytes(void *p,long frames){(void)p;return frames*6;}
''')
        (self.dir / 'main.c').write_text('''
#include <stdio.h>
long snd_pcm_readi(void*,void*,unsigned long);
int main(void){short samples[6];snd_pcm_readi((void*)1,samples,2);
 for(int i=0;i<6;i++)printf("%d ",samples[i]);return 0;}
''')
        self.build('-shared', '-fPIC', 'fake.c', '-o', 'libfake.so')
        self.build('-shared', '-fPIC', str(ROOT / 'device/mic-tap/tap.c'), '-ldl',
                   f'-DMIC_SOCKET="{self.sockpath}"', f'-DMIC_LEASE="{self.lease}"', '-o', 'tap.so')
        self.build('main.c', '-L.', '-lfake', '-Wl,-rpath,$ORIGIN', '-o', 'reader')

    def build(self, *args):
        subprocess.run(['gcc', '-Wall', '-Wextra', *args], cwd=self.dir, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read(self):
        env = {**os.environ, 'LD_PRELOAD': str(self.dir / 'tap.so')}
        r = subprocess.run([str(self.dir / 'reader')], env=env, capture_output=True, check=True, timeout=3)
        return [int(n) for n in r.stdout.split()]

    def receiver(self):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(conn.close)
        conn.bind(str(self.sockpath))
        conn.settimeout(1)
        return conn

    def test_without_consumer_alice_keeps_original_audio(self):
        self.lease.touch()
        self.assertEqual(self.read(), [100, 200, 300] * 2)

    def test_copy_before_suppression_preserves_feedback(self):
        conn = self.receiver()
        self.lease.touch()
        self.assertEqual(self.read(), [0, 0, 300] * 2)
        packet = conn.recv(16384)
        self.assertEqual(struct.unpack('<5I', packet[:20]), (0x314d5057, 48000, 16 << 16 | 3, 2, 0))
        self.assertEqual(struct.unpack('<6h', packet[20:]), (100, 200, 300) * 2)

    def test_stale_or_absent_lease_restores_alice(self):
        conn = self.receiver()
        self.assertEqual(self.read(), [100, 200, 300] * 2)
        conn.recv(16384)
        self.lease.touch()
        os.utime(self.lease, (time.time() - 10,) * 2)
        self.assertEqual(self.read(), [100, 200, 300] * 2)
        conn.recv(16384)
