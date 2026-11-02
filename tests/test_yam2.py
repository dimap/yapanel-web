import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def panel(shell):
    p = server.Panel.__new__(server.Panel)
    p.adb = Mock()
    p.adb.shell.side_effect = shell
    return p


class Detect(unittest.TestCase):
    def test_platforms_like_install_sh(self):
        d = server.Panel.yam2_detect
        self.assertEqual(d("yandex,plum-evt2\nyandex,plum\namlogic, axg"), "plum")
        self.assertEqual(d("Yandex,Station-Mini2\namlogic,axg"), "yandexmini_2")
        self.assertEqual(d("yandex,bergamot"), "bergamot")
        self.assertEqual(d("yandex,station-micro\namlogic,axg"), "yandexmicro")
        self.assertEqual(d("amlogic,axg"), "yandexmini")
        self.assertIsNone(d("something,else"))

    def test_installed_platform_wins(self):
        p = panel(lambda cmd, **kw: ("bergamot\n@\nyandex,plum\n", "", 0))
        self.assertEqual(p.yam2_platform(), "bergamot")
        p = panel(lambda cmd, **kw: ("@\nyandex,plum\n", "", 0))
        self.assertEqual(p.yam2_platform(), "plum")


class Names(unittest.TestCase):
    def test_package_names(self):
        ok = server.Panel._yam2_pkg
        self.assertEqual(ok("yam/luna"), "yam/luna")
        self.assertEqual(ok("yam/luna@0.2.2", version=True), "yam/luna@0.2.2")
        for bad in ("luna", "yam/luna;reboot", "../x", "yam/luna@0.2", "Yam/Luna", "yam/luna/x", ""):
            with self.assertRaises(server.AdbError, msg=bad):
                ok(bad)


class Catalog(unittest.TestCase):
    def test_homepages_and_missing_airplay_metadata(self):
        p = server.Panel(Mock())
        p.yam2_status = lambda: {"registries": [{"name": "official", "url": "https://repo.yse.wtf/v2"}],
                                 "platform": "yandexmini_2"}
        site = "https://github.com/Yandex-Station-Universal-Repo/Station-airplay"
        for homepage in (None, "", "https://example.org/new-airplay-home"):
            with self.subTest(homepage=homepage):
                data = {"packages": [
                    {"name": "alice/airplay", "homepage": homepage, "targets": ["yandexmini_2"]},
                    {"name": "example/player", "homepage": "https://example.org/player"},
                    {"name": "example/no-site"},
                ]}
                with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
                    packages = p.yam2_catalog(refresh=True)["packages"]
                self.assertEqual(packages[0]["homepage"], homepage or site)
                self.assertEqual(packages[1]["homepage"], "https://example.org/player")
                self.assertIsNone(packages[2]["homepage"])
                self.assertTrue(packages[0]["fits"])

    def test_tls_error_is_explained(self):
        p = server.Panel(Mock())
        p.yam2_status = lambda: {"registries": [{"name": "official", "url": "https://repo.yse.wtf/v2"}],
                                 "platform": "cucumber"}
        err = urllib.error.URLError(ssl.SSLError("EOF"))
        with patch("server.urllib.request.urlopen", side_effect=err), \
             patch("server.time.sleep", return_value=None):
            with self.assertRaises(server.AdbError) as cm:
                p.yam2_catalog()
        msg = str(cm.exception)
        self.assertIn("каталог YAM 2", msg)
        self.assertIn("repo.yse.wtf", msg)


class Services(unittest.TestCase):
    LIST = "yam/luna/luna\tenabled\tbin/luna --data-dir ${YAM_VAR}\nbad line\nx/y;z/s\tenabled\tbin/s\n"

    def shell(self, cmd, **kw):
        if "service" in cmd and "list" in cmd:
            return (self.LIST, "", 0)
        if "webpanel_yam2_yam~luna~luna.pid" in cmd and "boot" in cmd:
            return ("run:yam~luna~luna\n", "", 0)
        if "YAM_PLATFORM=" in cmd:
            return ("plum\n@\n", "", 0)
        if "platforms/plum.yaml" in cmd:
            return ("id: plum\npaths:\n  root: /data/yam\n  etc: /data/yam/etc\n  var: /data/yam/var\n  run: /tmp/yam\n", "", 0)
        return ("", "", 0)

    def test_parse_skips_bad_names(self):
        svcs = panel(self.shell)._yam2_services_raw()
        self.assertEqual([s["id"] for s in svcs], ["yam~luna~luna"])
        self.assertTrue(svcs[0]["running"])
        self.assertFalse(svcs[0]["boot"])
        self.assertEqual(svcs[0]["exec"], ["bin/luna", "--data-dir", "${YAM_VAR}"])

    def test_launcher_runs_with_yam_env_and_gives_up(self):
        p = panel(self.shell)
        svc = p._yam2_services_raw()[0]
        with tempfile.TemporaryDirectory() as td:
            text = p._yam2_launcher(svc).replace("/data/yam", td + "/root").replace("/tmp/yam", td + "/run")
            self.assertIn("export YAM_VAR=", text)
            bin_dir = Path(td, "root/pkgs/yam/luna/current/bin")
            bin_dir.mkdir(parents=True)
            prog = bin_dir / "luna"
            prog.write_text('#!/bin/sh\necho "args=$* var=$YAM_VAR pwd=$(pwd)"\nexit 3\n')
            prog.chmod(0o755)
            script = Path(td, "s.sh")
            script.write_text(text.replace("sleep 3", "true"))
            r = subprocess.run(["sh", str(script)], capture_output=True, timeout=30)
            self.assertEqual(r.returncode, 1)          # 5 падений подряд — сдаётся
            log = Path(td, "run/yam/luna/luna.log").read_text()
            self.assertEqual(log.count("args=--data-dir " + td + "/root/var/yam/luna"), 5)
            self.assertIn("pwd=" + td + "/root/pkgs/yam/luna/current", log)
            self.assertIn("перезапуск 5/5", log)
            self.assertTrue(Path(td, "root/var/yam/luna").is_dir())

    def test_absolute_exec_rejected(self):
        p = panel(self.shell)
        with self.assertRaises(server.AdbError):
            p._yam2_launcher({"id": "yam~luna~luna", "package": "yam/luna", "exec": ["/bin/sh", "-c", "x"]})

    def test_bad_service_id(self):
        p = panel(self.shell)
        for bad in ("yam~luna", "yam~luna~l;reboot", "../x~y~z"):
            with self.assertRaises(server.AdbError):
                p.yam2_service(bad, "start")


class Install(unittest.TestCase):
    def release(self, digest):
        return {"tag_name": "yam-v0.2.2", "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "https://example/sums"},
            {"name": "yam-aarch64-unknown-linux-musl", "browser_download_url": "https://example/yam",
             "digest": "sha256:" + digest}]}

    def test_bad_checksum_never_pushes(self):
        p = panel(lambda cmd, **kw: ("@\nyandex,plum\n", "", 0))
        p._yam2_releases = lambda refresh=False: [self.release("0" * 64)]
        p._yam2_sums = {}
        bodies = [io.BytesIO(("0" * 64 + "  yam-aarch64-unknown-linux-musl\n").encode()), io.BytesIO(b"evil")]
        with patch.object(server.urllib.request, "urlopen", side_effect=bodies):
            with self.assertRaisesRegex(server.AdbError, "контрольная сумма"):
                p.yam2_install()
        p.adb.push.assert_not_called()

    def test_install_pushes_wrapper_like_install_sh(self):
        blob = b"yam-binary"
        digest = hashlib.sha256(blob).hexdigest()
        pushed = {}

        def shell(cmd, **kw):
            if "YAM_PLATFORM=" in cmd:
                return ("@\nyandex,plum\n", "", 0)
            if "sha256sum /data/yam/bin/yam.new" in cmd:
                return (digest + "  /data/yam/bin/yam.new", "", 0)
            if "df -k" in cmd:
                return ("60000\n", "", 0)
            return ("", "", 0)
        p = panel(shell)
        p.adb.push.side_effect = lambda local, remote, **kw: pushed.__setitem__(remote, Path(local).read_bytes())
        p._yam2_releases = lambda refresh=False: [self.release(digest)]
        p._yam2_sums = {}
        p.yam2_status = lambda refresh=False: {"installed": True}
        bodies = [io.BytesIO((digest + "  yam-aarch64-unknown-linux-musl\n").encode()), io.BytesIO(blob),
                  io.BytesIO(b"id: plum\narch: aarch64\n")]
        with patch.object(server.urllib.request, "urlopen", side_effect=bodies):
            res = p.yam2_install()
        self.assertEqual(res["installed_tag"], "yam-v0.2.2")
        self.assertEqual(pushed["/data/yam/bin/yam.new"], blob)
        wrapper = pushed["/data/yam/bin/yam-run.new"].decode()
        self.assertIn("export YAM_PLATFORM='plum'\n", wrapper)
        self.assertIn("export YAM_PROFILES='/data/yam/platforms'\n", wrapper)   # по ней узнаёт себя install.sh --uninstall
        self.assertNotIn("\r", wrapper)
        cmds = " ".join(c.args[0] for c in p.adb.shell.call_args_list)
        self.assertNotIn("/usr/bin", cmds)
        self.assertNotIn("remount", cmds)
        self.assertNotIn("/data/quazar/yam", cmds.replace("pidof yamd", ""))


class Hook(unittest.TestCase):
    def test_hook_starts_marked_services(self):
        self.assertIn(f"{server.YAM2_SVC_DIR}/boot/*", server.BOOT_HOOK_SCRIPT)
        self.assertIn("автозапуск служб yam", server.Panel._hook_users_script(server.Panel.__new__(server.Panel)))


if __name__ == "__main__":
    unittest.main()
