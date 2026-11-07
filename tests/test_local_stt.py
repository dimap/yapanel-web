import importlib.util
import io
import json
import threading
import types
import unittest
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location('local_stt', Path(__file__).resolve().parents[1] / 'tools/local-stt/server.py')
stt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stt)


def upload(model='base'):
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as f:
        f.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\x00\x00' * 4800)
    boundary = 'test-boundary'
    parts = []
    for name, value in [('model', model.encode()), ('language', b'ru'), ('file', audio.getvalue())]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value + b'\r\n')
    return f'multipart/form-data; boundary={boundary}', b''.join(parts) + f'--{boundary}--\r\n'.encode()


class LocalSTT(unittest.TestCase):
    def setUp(self):
        self.recognizer = Mock()
        self.recognizer.transcribe.return_value = ([types.SimpleNamespace(text=' Привет '), types.SimpleNamespace(text='мир')], None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), stt.make_handler(self.recognizer, 'base', 'test-key'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, token='test-key', model='base'):
        content_type, body = upload(model)
        req = urllib.request.Request(self.base + '/v1/audio/transcriptions', data=body,
                                     headers={'Content-Type': content_type, 'Authorization': 'Bearer ' + token})
        return urllib.request.urlopen(req, timeout=3)

    def test_transcribes_in_memory_with_vad(self):
        with self.request() as response:
            self.assertEqual(json.load(response), {'text': 'Привет мир'})
        _, kwargs = self.recognizer.transcribe.call_args
        self.assertTrue(kwargs['vad_filter'])
        self.assertFalse(kwargs['condition_on_previous_text'])
        self.assertEqual(kwargs['language'], 'ru')

    def test_rejects_auth_and_unserved_model_without_inference(self):
        for token, model, expected in [('wrong', 'base', 401), ('test-key', 'download-other-model', 400)]:
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request(token, model)
            self.assertEqual(err.exception.code, expected)
            err.exception.close()
        self.recognizer.transcribe.assert_not_called()
