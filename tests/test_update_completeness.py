import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import server


class UpdateCompleteness(unittest.TestCase):
    def test_update_installs_new_module_and_preserves_user_data(self):
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, 'w') as z:
            z.writestr('yapanel-web-0.3.0/server.py', '# server')
            z.writestr('yapanel-web-0.3.0/new_feature.py', 'value = 1')
            z.writestr('yapanel-web-0.3.0/.yandex_auth.json', 'untrusted')
            z.writestr('yapanel-web-0.3.0/../outside.py', 'bad')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'panel'; root.mkdir()
            (root / '.yandex_auth.json').write_text('keep')
            with patch.object(server, 'ROOT', root), patch.object(server.urllib.request, 'urlopen', return_value=io.BytesIO(blob.getvalue())):
                server.PanelUpdater()._from_zip('0.3.0')
            self.assertEqual((root / 'new_feature.py').read_text(), 'value = 1')
            self.assertEqual((root / '.yandex_auth.json').read_text(), 'keep')
            self.assertFalse((root.parent / 'outside.py').exists())

    def test_legacy_update_recovers_modules_before_import(self):
        source = Path(server.__file__).parent
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for f in source.glob('*.py'):
                if f.name not in ('zvuk.py', 'zvuk_player.py', 'zvuk_queries.py'):
                    shutil.copy2(f, root / f.name)
            (root / 'VERSION').write_text('0.3.0')
            (root / 'static').mkdir()
            shutil.copy2(source / 'static/model-colors.json', root / 'static/model-colors.json')
            archive = root / 'release.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                for name in ('zvuk.py', 'zvuk_player.py', 'zvuk_queries.py', 'model_colors.py'):
                    z.write(source / name, 'yapanel-web-0.3.0/' + name)
            code = ('import urllib.request; '
                    'urllib.request.urlopen=lambda *a,**kw: open("release.zip","rb"); '
                    'import server; print("started")')
            env = os.environ.copy(); env['PYTHONPATH'] = str(root)
            p = subprocess.run([sys.executable, '-c', code], cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('started', p.stdout)
            self.assertTrue((root / 'zvuk_queries.py').exists())
