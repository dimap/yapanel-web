#!/usr/bin/env python3
"""assistant_max.py — Голосовой помощник для Яндекс Станции Макс (Android TV 9 AOSP).

«Алиса, спроси GPT …» уходит выбранной нейросетевой модели (OpenAI, Groq, Ollama, LM Studio),
штатный ответ Алисы мгновенно глушится, а ответ модели озвучивает Алиса («Повтори за мной»)
или локальный синтезатор речи через /odm/bin/playaudio.

События речи перехватываются из нативного Android logcat (тег quark-native:I).
Управление воспроизведением и речью Алисы идёт через локальный сокет Glagol (порт 1961).
"""

import argparse
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "max-1.0.0"

DEFAULT_PROMPT = (
    "Ты эрудированный собеседник и отвечаешь на любые вопросы. Отвечай по-русски и коротко: "
    "одно-три предложения разговорной речью. Ответ прочитают вслух, поэтому без списков, разметки, эмодзи, ссылок и формул."
)

DEFAULT_CONFIG = {
    "enabled": True,
    "triggers": ["спроси gpt", "спроси джипити", "спроси чат gpt", "спроси нейросеть", "спроси модель"],
    "llm": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "system_prompt": DEFAULT_PROMPT,
        "max_tokens": 500,
        "history": 6,
        "proxy": ""
    },
    "tts": {
        "provider": "alice",
        "base_url": "",
        "api_key": "",
        "model": "tts-1",
        "voice": "alloy",
        "speed": 1.0
    },
    "led": {
        "enabled": True,
        "color": "#7b2cbf"
    },
    "mode": {
        "enter": ["переключись на gpt", "переключись на ассистента", "включи ассистента"],
        "exit": ["отключись от ассистента", "выключи ассистента", "верни алису", "вернись к алисе"],
        "timeout_min": 10,
        "follow_up": True
    },
    "keys": {}
}

GENERIC_ERROR = "Ошибка ассистента. Пожалуйста, проверьте настройки в веб-панели."


def normalize(text: str) -> str:
    """Нормализация текста для сопоставления триггеров: нижний регистр, ё->е, удаление пунктуации."""
    s = (text or "").lower().replace("ё", "е")
    words = re.findall(r"[\w]+", s)
    return " ".join(words)


def match_trigger(phrase: str, triggers: list) -> tuple:
    """Сопоставление фразы со списком триггеров. Возвращает (остаток_вопроса, True) или ('', False)."""
    norm_phrase = normalize(phrase)
    for trig in triggers or []:
        norm_trig = normalize(trig)
        if not norm_trig:
            continue
        if norm_phrase == norm_trig:
            return "", True
        if norm_phrase.startswith(norm_trig + " "):
            # Извлекаем вопрос из оригинальной фразы, чтобы сохранить пунктуацию и регистр
            trig_words_count = len(norm_trig.split())
            words = phrase.split()
            seen = 0
            idx = 0
            for i, w in enumerate(words):
                seen += len(normalize(w).split())
                if seen >= trig_words_count:
                    idx = i + 1
                    break
            question = " ".join(words[idx:]).lstrip(" ,.:;!?—-\"'").rstrip()
            return question, True
    return "", False


def for_speech(text: str, limit: int = 900) -> str:
    """Подготовка ответа модели для озвучки 'Повтори за мной': удаление разметки и сокращение."""
    s = re.sub(r"[*_#`«»\"'>\[\]\(\)]", " ", text or "")
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in (".", "!", "?"):
        last_sep = cut.rfind(sep)
        if last_sep > limit // 2:
            return cut[:last_sep + 1]
    last_space = cut.rfind(" ")
    if last_space > 0:
        return cut[:last_space] + "…"
    return cut


def voice_error(err_text: str) -> str:
    """Преобразование технической ошибки в понятный русский голос."""
    msg = str(err_text or "").lower()
    if "401" in msg or "403" in msg or "ключ" in msg or "unauthorized" in msg:
        return "Ключ АПИ не подошёл. Проверьте ключ в веб-панели."
    if "404" in msg or "not found" in msg or "модель" in msg:
        return "Модель или адрес АПИ не найдены. Проверьте настройки в веб-панели."
    if "429" in msg or "quota" in msg or "rate limit" in msg:
        return "Сервер ассистента просит подождать: превышен лимит запросов."
    if "timed out" in msg or "deadline" in msg or "таймаут" in msg:
        return "Ассистент не ответил вовремя. Попробуйте спросить ещё раз."
    if "connection" in msg or "подключен" in msg or "network" in msg:
        return "Нет связи с сервером ассистента. Проверьте подключение к интернету."
    if "пустой ответ" in msg or "empty" in msg:
        return "Модель вернула пустой ответ. Попробуйте переформулировать вопрос."
    return GENERIC_ERROR


def key_id(base_url: str) -> str:
    """Нормализованный идентификатор адреса провайдера для хранения ключей."""
    return str(base_url or "").strip().rstrip("/").lower()


class GlagolClient:
    """Минимальный клиент локального WebSocket-протокола Glagol (порт 1961 на Станции Макс)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1961):
        self.host = host
        self.port = port
        self.sock = None
        self.token = ""
        self._load_token()

    def _load_token(self):
        auth_paths = [
            Path("/data/yapanel/.yandex_auth.json"),
            Path("/data/quasar/webpanel/.yandex_auth.json"),
        ]
        try:
            auth_paths.append(Path.home() / ".yandex_auth.json")
        except (RuntimeError, KeyError):
            pass
        for p in auth_paths:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self.token = data.get("music_token") or data.get("token") or ""
                if self.token:
                    break
            except Exception:
                continue

    def connect(self, timeout: float = 2.0) -> bool:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
            # HTTP WebSocket upgrade
            req = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            self.sock.sendall(req.encode("ascii"))
            resp = self.sock.recv(1024)
            if b" 101 " not in resp:
                self.sock.close()
                self.sock = None
                return False
            return True
        except Exception:
            self.sock = None
            return False

    def send_frame(self, data: bytes):
        if not self.sock and not self.connect():
            raise ConnectionError("Glagol socket disconnected")
        length = len(data)
        frame = bytearray([0x81])  # text opcode, FIN
        # Client frames to server must be masked
        mask_key = b"\x12\x34\x56\x78"
        if length <= 125:
            frame.append(0x80 | length)
        elif length <= 65535:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, "big"))
        frame.extend(mask_key)
        masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(data))
        frame.extend(masked)
        self.sock.sendall(frame)

    def send_command(self, payload: dict) -> bool:
        try:
            envelope = {
                "conversationToken": self.token,
                "payload": payload,
                "id": str(int(time.time() * 1000))
            }
            data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            self.send_frame(data)
            return True
        except Exception:
            try:
                if self.connect():
                    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
                    self.send_frame(data)
                    return True
            except Exception:
                pass
            return False

    def stop(self) -> bool:
        """Остановить речь или воспроизведение Алисы."""
        return self.send_command({"command": "stop"})

    def say(self, text: str) -> bool:
        """Озвучить текст голосом Алисы."""
        speech = for_speech(text)
        return self.send_command({"command": "sendText", "text": f"Повтори за мной {speech}"})


class AssistantDaemon:
    def __init__(self, work_dir: str = "/data/quasar/webpanel/assistant"):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.work_dir / "config.json"
        self.status_path = Path("/tmp/webpanel_assistant.json")
        self.cmd_path = Path("/tmp/webpanel_assistant.cmd")
        self.sd_path = Path("/tmp/webpanel_assistant.sd")

        self.cfg = self.load_config()
        self.cfg_mtime = 0
        self.state = "idle"
        self.error = ""
        self.cfg_error = ""
        self.history = []
        self.convo = []
        self.last_turn = 0
        self.mode = False
        self.mode_at = 0
        self.route = False
        self.ours = False
        self.say_at = 0
        self.lock = threading.Lock()
        self.running = True

        self.glagol = GlagolClient()

    def load_config(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if self.cfg_path.is_file():
            try:
                raw = json.loads(self.cfg_path.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
            except Exception as e:
                self.cfg_error = f"Ошибка чтения конфига: {e}"
        # Разрешение API ключа для текущего base_url
        base = key_id(cfg.get("llm", {}).get("base_url", ""))
        keys = cfg.get("keys", {})
        if base in keys:
            cfg["llm"]["api_key"] = keys[base]
        return cfg

    def reload_config_if_changed(self):
        try:
            mtime = self.cfg_path.stat().st_mtime
            if mtime != self.cfg_mtime:
                self.cfg_mtime = mtime
                with self.lock:
                    self.cfg = self.load_config()
                    self.cfg_error = ""
        except Exception:
            pass

    def write_status(self):
        with self.lock:
            st = {
                "pid": os.getpid(),
                "version": VERSION,
                "state": self.state,
                "error": self.error,
                "config_error": self.cfg_error,
                "enabled": self.cfg.get("enabled", True),
                "history": self.history[-20:],
                "at": int(time.time()),
                "mode": self.mode,
                "route": self.route
            }
            if self.mode:
                timeout_sec = self.cfg.get("mode", {}).get("timeout_min", 10) * 60
                st["mode_until"] = int(self.mode_at + timeout_sec)

        tmp = self.status_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.status_path)
        except Exception:
            pass

    def check_commands(self):
        if not self.cmd_path.is_file():
            return
        try:
            raw = self.cmd_path.read_text(encoding="utf-8")
            self.cmd_path.unlink(missing_ok=True)
            cmd = json.loads(raw)
        except Exception:
            return

        if cmd.get("stop"):
            self.stop_current()
        if "mode" in cmd:
            with self.lock:
                self.mode = bool(cmd["mode"])
                self.mode_at = time.time()
            self.write_status()
        if "route" in cmd:
            with self.lock:
                self.route = bool(cmd["route"])
            self.write_status()
        if cmd.get("ask"):
            threading.Thread(target=self.ask, args=(cmd["ask"], "panel"), daemon=True).start()
        if cmd.get("say"):
            threading.Thread(target=self.speak, args=(cmd["say"],), daemon=True).start()

    def stop_current(self):
        with self.lock:
            self.state = "idle"
            self.error = ""
        self.glagol.stop()
        # Android media key stop fallback
        try:
            subprocess.run(["input", "keyevent", "86"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        self.write_status()

    def call_llm(self, question: str) -> str:
        with self.lock:
            cfg = self.cfg.get("llm", {})
            base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
            api_key = cfg.get("api_key", "")
            model = cfg.get("model", "gpt-4o-mini")
            system_prompt = cfg.get("system_prompt", DEFAULT_PROMPT)
            max_tokens = int(cfg.get("max_tokens", 500))
            proxy = cfg.get("proxy", "")
            history_len = int(cfg.get("history", 6))

            # Сброс контекста если пауза больше 10 минут
            if time.time() - self.last_turn > 600:
                self.convo = []

            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(self.convo[-history_len * 2:])
            messages.append({"role": "user", "content": question})

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False
        }
        data = json.dumps(payload).encode("utf-8")
        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        handler_args = {}
        if proxy:
            handler_args["http"] = proxy
            handler_args["https"] = proxy
        proxy_handler = urllib.request.ProxyHandler(handler_args) if proxy else urllib.request.ProxyHandler()
        opener = urllib.request.build_opener(proxy_handler)

        req = urllib.request.Request(url, data=data, headers=headers)
        with opener.open(req, timeout=45) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            choices = resp_data.get("choices") or []
            if not choices:
                raise ValueError("Модель вернула пустой ответ")
            answer = choices[0].get("message", {}).get("content", "").strip()
            if not answer:
                raise ValueError("Модель вернула пустой ответ")
            return answer

    def speak(self, text: str):
        with self.lock:
            self.state = "speaking"
            self.say_at = time.time()
            self.ours = True
            provider = self.cfg.get("tts", {}).get("provider", "alice")
        self.write_status()

        success = False
        if provider == "alice":
            success = self.glagol.say(text)

        if not success:
            # Fallback к Google TTS / playaudio на Станции Макс
            try:
                enc = urllib.parse.quote(for_speech(text, limit=300))
                tts_url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl=ru&client=tw-ob&q={enc}"
                req = urllib.request.Request(tts_url, headers={"User-Agent": "Mozilla/5.0"})
                audio_path = "/data/local/tmp/assistant_say.mp3"
                with urllib.request.urlopen(req, timeout=10) as resp:
                    Path(audio_path).write_bytes(resp.read())
                subprocess.run(["/odm/bin/playaudio", audio_path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                success = True
            except Exception:
                pass

        with self.lock:
            self.state = "idle"
        self.write_status()

    def ask(self, question: str, source: str = "voice"):
        with self.lock:
            self.state = "thinking"
            self.error = ""
        self.write_status()

        t0 = time.time()
        answer = ""
        err_msg = ""
        try:
            answer = self.call_llm(question)
            with self.lock:
                self.convo.append({"role": "user", "content": question})
                self.convo.append({"role": "assistant", "content": answer})
                self.last_turn = time.time()
        except Exception as e:
            err_msg = str(e)
            with self.lock:
                self.error = err_msg

        duration_ms = int((time.time() - t0) * 1000)
        entry = {
            "at": int(time.time()),
            "source": source,
            "question": question,
            "answer": answer,
            "error": err_msg,
            "ms": duration_ms
        }
        with self.lock:
            self.history.append(entry)
        self.write_status()

        if answer:
            self.speak(answer)
        elif err_msg:
            spoken_err = voice_error(err_msg)
            self.speak(spoken_err)

    def handle_phrase(self, phrase: str):
        self.reload_config_if_changed()
        with self.lock:
            if not self.cfg.get("enabled", True):
                return
            mode = self.mode
            triggers = self.cfg.get("triggers", [])
            enter_phrases = self.cfg.get("mode", {}).get("enter", [])
            exit_phrases = self.cfg.get("mode", {}).get("exit", [])

        # Проверка включения/выключения постоянного режима
        _, enter_matched = match_trigger(phrase, enter_phrases)
        if enter_matched:
            self.glagol.stop()
            with self.lock:
                self.mode = True
                self.mode_at = time.time()
            self.write_status()
            self.speak("Режим ассистента включён.")
            return

        _, exit_matched = match_trigger(phrase, exit_phrases)
        if exit_matched:
            self.glagol.stop()
            with self.lock:
                self.mode = False
            self.write_status()
            self.speak("Возвращаюсь к Алисе.")
            return

        question, matched = match_trigger(phrase, triggers)
        if not matched and not mode:
            return

        if not matched and mode:
            question = phrase

        # Пользователь задал вопрос — мгновенно глушим стандартную Алису!
        self.glagol.stop()
        try:
            subprocess.run(["input", "keyevent", "86"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        norm_q = normalize(question)
        if norm_q in ("стоп", "хватит", "замолчи", "тихо", "отмена", "помолчи"):
            self.stop_current()
            return
        if norm_q in ("новый разговор", "забудь", "забудь все"):
            with self.lock:
                self.convo = []
            self.speak("Хорошо, начинаем новый разговор.")
            return

        threading.Thread(target=self.ask, args=(question, "voice"), daemon=True).start()

    def run_logcat_listener(self):
        """Чтение событий распознавания речи из Android logcat в реальном времени."""
        # -T 1: только новые события, -s quark-native:I: фильтр по C++ ядру Яндекса
        cmd = ["logcat", "-v", "brief", "-T", "1", "-s", "quark-native:I"]
        while self.running:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
                for line in proc.stdout:
                    if not self.running:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    if "onRequestStateChanged: LISTENING, recognizedPhrase='" in line:
                        m = re.search(r"recognizedPhrase='([^']*)'", line)
                        if m:
                            phrase = m.group(1).strip()
                            if phrase:
                                self.handle_phrase(phrase)
                    elif "onAliceStateChanged: IDLE" in line:
                        with self.lock:
                            self.ours = False
            except Exception:
                time.sleep(2)

    def run(self):
        self.sd_path.write_text(str(os.getpid()), encoding="utf-8")
        self.write_status()

        t = threading.Thread(target=self.run_logcat_listener, daemon=True)
        t.start()

        try:
            while self.running:
                self.reload_config_if_changed()
                self.check_commands()
                # Проверка таймаута режима ассистента
                with self.lock:
                    if self.mode:
                        timeout_sec = self.cfg.get("mode", {}).get("timeout_min", 10) * 60
                        if time.time() - self.mode_at > timeout_sec:
                            self.mode = False
                            self.write_status()
                time.sleep(0.3)
        finally:
            self.running = False
            self.sd_path.unlink(missing_ok=True)
            self.status_path.unlink(missing_ok=True)


def print_models(config_path: str, override_path: str):
    """Вывод списка доступных моделей API в JSON для веб-панели."""
    cfg = {}
    if os.path.isfile(config_path):
        try:
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except Exception:
            pass
    over = {}
    if override_path and override_path != "-":
        try:
            over = json.loads(Path(override_path).read_text(encoding="utf-8"))
        except Exception:
            pass

    base_url = (over.get("base_url") or cfg.get("llm", {}).get("base_url") or "https://api.openai.com/v1").rstrip("/")
    api_key = over.get("api_key") or cfg.get("llm", {}).get("api_key") or cfg.get("keys", {}).get(key_id(base_url), "")
    proxy = over.get("proxy") or cfg.get("llm", {}).get("proxy", "")

    url = f"{base_url}/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    handler_args = {}
    if proxy:
        handler_args["http"] = proxy
        handler_args["https"] = proxy
    proxy_handler = urllib.request.ProxyHandler(handler_args) if proxy else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(proxy_handler)

    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = []
            for item in data.get("data") or []:
                mid = item.get("id")
                if mid:
                    models.append(mid)
            models.sort()
            print(json.dumps({"models": models}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": f"Ошибка запроса моделей: {e}"}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Голосовой помощник для Станции Макс")
    parser.add_argument("-dir", default="/data/quasar/webpanel/assistant", help="Папка с config.json")
    parser.add_argument("-models", default="", help="Получить список моделей API")
    parser.add_argument("-version", action="store_true", help="Показать версию")
    args = parser.parse_args()

    if args.version:
        print(VERSION)
        return

    if args.models:
        print_models(os.path.join(args.dir, "config.json"), args.models)
        return

    daemon = AssistantDaemon(work_dir=args.dir)

    def handle_sig(sig, frame):
        daemon.running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    daemon.run()


if __name__ == "__main__":
    main()
