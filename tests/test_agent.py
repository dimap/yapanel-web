"""Агент панели (device/agent) вместе с клиентом agent.py и классом Adb: настоящий агент, собранный из исходников
и запущенный локально во временной папке. Нужны Go и Linux (агент — для колонки)."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent
import server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("go"), "нужны Linux и Go")
class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        binary = cls.tmp / "agent"
        subprocess.run(["go", "build", "-o", str(binary), "."], cwd=ROOT / "device" / "agent", check=True,
                       env={**os.environ, "CGO_ENABLED": "0"}, capture_output=True, timeout=300)
        (cls.tmp / "a").mkdir()
        (cls.tmp / "a" / "tokens").write_text(hashlib.sha256(TOKEN.encode()).hexdigest() + " test\n")
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.proc = subprocess.Popen([str(binary), "-dir", str(cls.tmp / "a"), "-addr", f"127.0.0.1:{cls.port}"],
                                    stderr=subprocess.DEVNULL)
        cert = cls.tmp / "a" / "cert.pem"
        for _ in range(100):
            if cert.exists():
                break
            time.sleep(0.05)
        cls.link = agent.AgentLink("127.0.0.1", TOKEN, cert.read_text(), cls.port)
        for _ in range(50):
            try:
                cls.link.ping()
                break
            except agent.AgentError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.link.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def adb(self):
        """Adb, у которого настоящего adb нет: всё, что не ушло через агента, упадёт."""
        a = server.Adb("/nonexistent/adb", "127.0.0.1:5555")
        a.agent = self.link
        return a

    def test_shell_files_ls_through_agent(self):
        a = self.adb()
        self.assertEqual(a.shell("echo привет; echo ошибка >&2; exit 2"), ("привет\nошибка\n", "", 2))
        src, dst = self.tmp / "src.bin", self.tmp / "dst.bin"
        data = os.urandom(150000)
        src.write_bytes(data)
        os.chmod(src, 0o640)
        remote = str(self.tmp / "remote" / "f.bin")
        a.push(src, remote)
        self.assertEqual(os.stat(remote).st_mode & 0o777, 0o640)     # режим — как у adb push
        a.pull(remote, dst)
        self.assertEqual(dst.read_bytes(), data)
        self.assertEqual(a.read_bytes(remote), data)
        names = [n for _, _, _, n in a.ls(str(self.tmp / "remote"))]
        self.assertEqual(names, [".", "..", "f.bin"])
        with self.assertRaisesRegex(server.AdbError, "No such file"):
            a.read_bytes("/nonexistent/file")
        with self.assertRaisesRegex(server.AdbError, "не ответила за 1 с"):
            a.shell("sleep 10", timeout=1)
        self.assertTrue(a.agent_ok)

    def test_popen_lines_and_pty(self):
        a = self.adb()
        proc = a.popen("for i in 1 2 3; do echo v$i; done", text=True)
        self.assertEqual([l.strip() for l in proc.stdout], ["v1", "v2", "v3"])
        self.assertEqual(proc.wait(5), 0)
        proc = a.popen("[ -t 0 ] && echo tty; stty size", pty=True, cols=100, rows=30)
        out = b""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            out += chunk
        self.assertIn(b"tty", out)
        self.assertIn(b"30 100", out)

    def test_only_adb_and_fallback(self):
        a = self.adb()
        calls = []
        fake = lambda *args, **kw: calls.append(args[0]) or subprocess.CompletedProcess(args[0], 0, b"via adb\n@@webpanel-rc=0\n", b"")
        with patch.object(server.subprocess, "run", fake):
            with a.only_adb():                      # агента обновляют — команды только через adb
                self.assertEqual(a.shell("echo x")[0], "via adb")
            self.assertEqual(len(calls), 1)
            a.agent = agent.AgentLink("127.0.0.1", "w" * 43, (self.tmp / "a" / "cert.pem").read_text(),
                                      self.port)
            with patch.object(server.JOURNAL, "add"):
                self.assertEqual(a.shell("echo x")[0], "via adb")     # чужой токен — агент отказал, работаем через adb
            self.assertFalse(a.agent_ok)
            self.assertEqual(len(calls), 2)
            a.shell("echo y")                        # пока агент «отдыхает», к нему не стучимся
            self.assertEqual(len(calls), 3)


class AgentStore(unittest.TestCase):
    def test_token_file_is_private_and_forget_removes_entry(self):
        with tempfile.TemporaryDirectory() as td, patch.object(server, "AGENT_FILE", Path(td) / ".agent.json"):
            p = server.Panel(server.Adb("adb", "10.0.0.5:5555"))
            p._agent_save({"10.0.0.5:5555": {"token": "x", "cert": "c"}, "10.0.0.6:5555": {"token": "y", "cert": "c"}})
            mode = os.stat(server.AGENT_FILE).st_mode & 0o777
            if sys.platform.startswith("win"):
                self.assertIn(mode, (0o600, 0o666))
            else:
                self.assertEqual(mode, 0o600)
            p._agent_forget()
            self.assertEqual(list(p._agent_store()), ["10.0.0.6:5555"])
            self.assertIsNone(p.adb.agent)

    def test_usb_serial_has_no_agent(self):
        p = server.Panel(server.Adb("adb", "USBSERIAL123"))
        with self.assertRaisesRegex(server.AdbError, "по сети"):
            p.agent_install()

    def test_hook_starts_agent(self):
        self.assertIn(server.AGENT_RUN.split(" >")[0].replace("exec ", ""), server.BOOT_HOOK_SCRIPT)
        self.assertIn(server.AGENT_TOKENS, dict(server.Panel.HOOK_USERS).values())


if __name__ == "__main__":
    unittest.main()
