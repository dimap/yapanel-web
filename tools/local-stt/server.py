"""Optional local STT sidecar; OpenAI-compatible WAV transcription on CPU.

Only start this service after explicit user opt-in; Docker setup is documented
in README.md. It is never installed or started by the panel.
Audio stays in memory; the loaded model is reused between requests.
"""
import argparse
import hmac
import io
import json
import logging
import os
import secrets
import threading
import wave
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_UPLOAD = 4 * 1024 * 1024


def parse_upload(content_type, body, model_name):
    if not content_type.startswith('multipart/form-data;'):
        raise ValueError('multipart/form-data required')
    message = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    if not message.is_multipart():
        raise ValueError('invalid multipart body')
    fields = {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if name in fields:
            raise ValueError('duplicate form field')
        fields[name] = part.get_payload(decode=True) or b''
    if fields.get('model', b'').decode() != model_name:
        raise ValueError('this server only serves model ' + model_name)
    if fields.get('response_format', b'json') != b'json':
        raise ValueError('only JSON response format is supported')
    language = fields.get('language', b'ru').decode().strip() or None
    if language and (len(language) > 8 or not language.replace('-', '').isalpha()):
        raise ValueError('invalid language')
    audio = fields.get('file', b'')
    with wave.open(io.BytesIO(audio), 'rb') as wav:
        rate, channels, count = wav.getframerate(), wav.getnchannels(), wav.getnframes()
        if channels != 1 or wav.getsampwidth() != 2 or rate not in (16000, 24000, 48000):
            raise ValueError('mono PCM16 WAV at 16, 24 or 48 kHz required')
        if not 0.1 <= count / rate <= 30:
            raise ValueError('audio duration must be between 0.1 and 30 seconds')
        if len(wav.readframes(count)) != count * 2:
            raise ValueError('truncated WAV')
    return audio, language


def make_handler(recognizer, model_name, api_key):
    busy = threading.BoundedSemaphore(1)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authorized(self):
            return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + api_key)

        def do_GET(self):
            if self.path == '/health':
                return self.reply(200, {'ready': True, 'model': model_name})
            if not self.authorized():
                return self.reply(401, {'error': {'message': 'invalid API key'}})
            if self.path == '/v1/models':
                return self.reply(200, {'data': [{'id': model_name, 'object': 'model', 'owned_by': 'local'}]})
            self.reply(404, {'error': {'message': 'not found'}})

        def do_POST(self):
            self.connection.settimeout(15)
            if not self.authorized():
                return self.reply(401, {'error': {'message': 'invalid API key'}})
            if self.path != '/v1/audio/transcriptions':
                return self.reply(404, {'error': {'message': 'not found'}})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_UPLOAD:
                    return self.reply(413, {'error': {'message': 'invalid upload size'}})
                if not busy.acquire(blocking=False):
                    return self.reply(429, {'error': {'message': 'recognizer busy'}})
                try:
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError('truncated request')
                    audio, language = parse_upload(self.headers.get('Content-Type', ''), body, model_name)
                    segments, _ = recognizer.transcribe(io.BytesIO(audio), language=language,
                                                       vad_filter=True, condition_on_previous_text=False,
                                                       beam_size=5)
                    text = ' '.join(s.text.strip() for s in segments).strip()
                finally:
                    busy.release()
            except (ValueError, UnicodeError, wave.Error, EOFError) as exc:
                return self.reply(400, {'error': {'message': str(exc)}})
            except Exception:
                logging.exception("Local STT inference failed")
                return self.reply(500, {'error': {'message': 'local speech recognition failed'}})
            self.reply(200, {'text': text})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='base')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18445)
    parser.add_argument('--key-file', type=Path, default=Path('/data/stt.key'))
    parser.add_argument('--download-root', type=Path)
    parser.add_argument('--cpu-threads', type=int, default=4)
    args = parser.parse_args()
    args.key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(args.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as key:
            key.write(secrets.token_urlsafe(32))
    api_key = args.key_file.read_text().strip()
    if not api_key:
        parser.error('key file is empty')
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device='cpu', compute_type='int8', cpu_threads=args.cpu_threads,
                         download_root=str(args.download_root) if args.download_root else None)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(model, args.model, api_key))
    print(f'Local STT ready at {args.host}:{args.port}, model={args.model}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
