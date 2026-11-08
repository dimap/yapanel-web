"""Классические скрипты панели делят один глобальный scope: дубли const/let ломают всю вкладку."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def classic_scripts():
    """Тела классических скриптов в порядке документа, включая внешние файлы."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    scripts = []
    for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I):
        if "importmap" in attrs or "module" in attrs:
            continue
        src = re.search(r"""src\s*=\s*["']([^"']+)["']""", attrs, re.I)
        if src:
            path = (ROOT / src.group(1).split("?", 1)[0]).resolve()
            if path.is_file():
                scripts.append(path.read_text(encoding="utf-8"))
        elif body.strip():
            scripts.append(body)
    return scripts


class PanelScripts(unittest.TestCase):
    @unittest.skipIf(not shutil.which("node"), "нужен node")
    def test_classic_scripts_parse_together(self):
        scripts = classic_scripts()
        self.assertTrue(scripts)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write("\n;\n".join(scripts))
            path = f.name
        try:
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(r.returncode, 0, (r.stderr or r.stdout)[-1500:])

    def test_main_script_lives_in_app_js(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app = ROOT / "static" / "app.js"
        self.assertRegex(html, r'<script src="static/app\.js(?:\?[^"]*)?"></script>')
        self.assertTrue(app.is_file())
        self.assertIn("const $ = s => document.querySelector(s);", app.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
