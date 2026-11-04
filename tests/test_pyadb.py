import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pyadb


def pkt(cmd, a0, a1, data=b""):
    c = struct.unpack("<I", cmd)[0]
    return struct.pack("<6I", c, a0, a1, len(data), sum(data) & 0xFFFFFFFF, c ^ 0xFFFFFFFF) + data


class FakeAdbd:
    """Маленький adbd: CNXN без ключа, shell:echo …, sync (SEND/RECV/LIST) с файлами в памяти. maxdata 4096 — как у колонки."""

    def __init__(self):
        self.files = {"/data/a.txt": b"hello"}
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def recv(self, c, n):
        out = b""
        while len(out) < n:
            part = c.recv(n - len(out))
            if not part:
                raise ConnectionError
            out += part
        return out

    def read(self, c):
        cmd, a0, a1, n, _, _ = struct.unpack("<6I", self.recv(c, 24))
        return struct.pack("<I", cmd), a0, a1, self.recv(c, n) if n else b""

    def serve(self):
        while True:
            c, _ = self.srv.accept()
            threading.Thread(target=self.session, args=(c,), daemon=True).start()

    def session(self, c):
        try:
            self.read(c)
            c.sendall(pkt(b"CNXN", 0x01000000, 4096, b"device::ro.product.name=fake;"))
            streams = {}
            while True:
                cmd, a0, a1, data = self.read(c)
                if cmd == b"OPEN":
                    rid = 100 + a0
                    c.sendall(pkt(b"OKAY", rid, a0))
                    svc = data.rstrip(b"\0").decode()
                    if svc.startswith("shell:"):
                        out = svc[6:].replace("echo ", "", 1).encode() + b"\r\n"
                        c.sendall(pkt(b"WRTE", rid, a0, out))
                        self.read(c)          # OKAY на наш WRTE
                        c.sendall(pkt(b"CLSE", rid, a0))
                    else:
                        streams[a0] = (rid, bytearray())
                elif cmd == b"WRTE":
                    rid, buf = streams[a0]
                    c.sendall(pkt(b"OKAY", rid, a0))
                    buf += data
                    self.sync(c, a0, rid, buf)
                elif cmd == b"CLSE":
                    streams.pop(a0, None)
        except (ConnectionError, OSError, struct.error):
            pass

    def reply(self, c, a0, rid, data):
        c.sendall(pkt(b"WRTE", rid, a0, data))

    def sync(self, c, a0, rid, buf):
        while len(buf) >= 8:
            req, n = bytes(buf[:4]), struct.unpack("<I", buf[4:8])[0]
            if req in (b"DATA", b"SEND", b"RECV", b"LIST") and len(buf) < 8 + n:
                return
            if req == b"SEND":
                self.cur = (bytes(buf[8:8 + n]).decode().split(",")[0], bytearray())
                del buf[:8 + n]
            elif req == b"DATA":
                self.cur[1].extend(buf[8:8 + n])
                del buf[:8 + n]
            elif req == b"DONE":
                self.files[self.cur[0]] = bytes(self.cur[1])
                del buf[:8]
                self.reply(c, a0, rid, b"OKAY" + struct.pack("<I", 0))
            elif req == b"RECV":
                path = bytes(buf[8:8 + n]).decode()
                del buf[:8 + n]
                if path in self.files:
                    d = self.files[path]
                    self.reply(c, a0, rid, b"DATA" + struct.pack("<I", len(d)) + d + b"DONE" + struct.pack("<I", 0))
                else:
                    msg = b"No such file or directory"
                    self.reply(c, a0, rid, b"FAIL" + struct.pack("<I", len(msg)) + msg)
            elif req == b"LIST":
                del buf[:8 + n]
                out = b""
                for p, d in self.files.items():
                    name = p.rsplit("/", 1)[1].encode()
                    out += b"DENT" + struct.pack("<4I", 0o100644, len(d), 1700000000, len(name)) + name
                self.reply(c, a0, rid, out + b"DONE" + struct.pack("<4I", 0, 0, 0, 0))
            elif req == b"QUIT":
                del buf[:8]
            else:
                return


class PyAdbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeAdbd()
        cls.serial = f"127.0.0.1:{cls.fake.port}"
        cls.adb = pyadb.install()

    def run_adb(self, *args):
        return subprocess.run([self.adb, *args], capture_output=True, timeout=10)

    def test_connect_shell_and_others_untouched(self):
        self.assertIn(b"connected to", self.run_adb("connect", self.serial).stdout)
        r = self.run_adb("-s", self.serial, "shell", "echo привет")
        self.assertEqual(r.stdout.decode(), "привет\r\n")
        self.assertIn(self.serial, self.run_adb("devices", "-l").stdout.decode())
        self.assertEqual(subprocess.run([sys.executable, "-c", "exit(0)"]).returncode, 0)     # чужие программы — как обычно

    def test_push_pull_ls_big_file(self):
        self.run_adb("connect", self.serial)
        data = bytes(range(256)) * 400                                # 100 КБ — больше одного пакета 4096
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / "src", Path(td) / "dst"
            src.write_bytes(data)
            self.assertEqual(self.run_adb("-s", self.serial, "push", str(src), "/data/big.bin").returncode, 0)
            self.assertEqual(self.fake.files["/data/big.bin"], data)
            self.assertEqual(self.run_adb("-s", self.serial, "pull", "-a", "/data/big.bin", str(dst)).returncode, 0)
            self.assertEqual(dst.read_bytes(), data)
            bad = self.run_adb("-s", self.serial, "pull", "-a", "/nope", str(dst))
            self.assertEqual(bad.returncode, 1)
            self.assertIn(b"No such file", bad.stderr)
        rows = self.run_adb("-s", self.serial, "ls", "/data").stdout.decode().splitlines()
        self.assertIn(f"{0o100644:08x} {5:08x} {1700000000:08x} a.txt", rows)

    def test_unknown_device_is_lost_connection(self):
        r = self.run_adb("-s", "127.0.0.1:1", "shell", "true")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"not found", r.stderr)     # панель считает это потерей связи (ADB_LOST)


if __name__ == "__main__":
    unittest.main()
