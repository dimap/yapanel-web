#!/usr/bin/env python3
"""assistant_max.py — Голосовой помощник для Яндекс Станции Макс (Android TV 9 AOSP).

«Алиса, спроси GPT …» уходит выбранной нейросетевой модели (OpenAI, Groq, Ollama, LM Studio),
штатный ответ Алисы мгновенно глушится, а ответ модели озвучивает Алиса («Повтори за мной»)
или локальный синтезатор речи через /odm/bin/playaudio.

События речи перехватываются из нативного Android logcat (тег quark-native:I).
Управление воспроизведением и речью Алисы идёт через локальный сокет Glagol (порт 1961).
"""

import argparse
import copy
import ipaddress
import json
import logging
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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

# Разумные пределы настроек: мусор из config.json не должен ронять демон.
MAX_TOKENS_LIMITS = (16, 8192)
HISTORY_LIMITS = (0, 50)
TIMEOUT_MIN_LIMITS = (1, 240)
TTS_SPEED_LIMITS = (0.25, 4.0)
TTS_TIMEOUT = 30
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 32 * 1024 * 1024
TTS_FILE = "/data/local/tmp/assistant_tts.wav"
GOOGLE_TTS_FILE = "/data/local/tmp/assistant_say.mp3"

# Служебные адреса облачных метаданных и link-local: по ним нельзя ходить из настроек.
_BLOCKED_METADATA_HOSTS = {
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}


def _ip_literal(host: str):
    """IP из строки, включая десятичную/восьмеричную запись IPv4 (inet_aton)."""
    h = str(host or "").strip().strip("[]")
    if "%" in h:                 # IPv6 scope id: fe80::1%eth0
        h = h.split("%", 1)[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(h))
    except (OSError, ValueError):
        return None


def _blocked_address(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_link_local or str(ip).lower() in _BLOCKED_METADATA_HOSTS)


def validate_base_url(url: str) -> str:
    """Проверяет адрес API. Разрешены только http/https и неслужебные хосты.

    localhost/127.0.0.1/частные адреса разрешены — на них живут Ollama и LM Studio.
    Возвращает адрес без хвостового «/», иначе ValueError.
    """
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("не задан адрес API")
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise ValueError("адрес API содержит недопустимые символы")
    try:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("неверный адрес API") from None
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("адрес API должен начинаться с http:// или https://")
    if not parsed.netloc or not host:
        raise ValueError("в адресе API нет имени сервера")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("неверный порт в адресе API")
    if parsed.query or parsed.fragment:
        raise ValueError("адрес API не должен содержать «?» или «#»")
    host = host.rstrip(".").lower()
    if host in _BLOCKED_METADATA_HOSTS:
        raise ValueError("адрес облачных метаданных запрещён")
    ip = _ip_literal(host)
    if ip is not None and _blocked_address(ip):
        raise ValueError("запросы к служебным и link-local адресам запрещены")
    return raw.rstrip("/")


def is_url_allowed(url: str) -> bool:
    """True, если адрес проходит validate_base_url (удобно для тестов и панели)."""
    try:
        validate_base_url(url)
        return True
    except (TypeError, ValueError):
        return False


def safe_base_url(url: str) -> str:
    """Псевдоним validate_base_url для читаемости в вызывающем коде."""
    return validate_base_url(url)


def is_safe_url(url: str) -> bool:
    """Псевдоним is_url_allowed: удобно для панели и внешних проверок."""
    return is_url_allowed(url)


def api_url(base_url: str, path: str) -> str:
    """Склеивает проверенный base_url и путь API, не теряя путь провайдера."""
    return validate_base_url(base_url) + "/" + str(path or "").lstrip("/")


def _redact_secrets(text, secrets) -> str:
    """Убирает значения ключей из строк, которые попадут в status/журнал."""
    s = str(text or "")
    for secret in sorted({str(x) for x in (secrets or ()) if x and len(str(x)) >= 4}, key=len, reverse=True):
        if secret in s:
            s = s.replace(secret, "***")
    return s


def _redact_value(value, secrets):
    """Рекурсивно вычищает ключи из структур, которые попадут в status."""
    if isinstance(value, str):
        return _redact_secrets(value, secrets)
    if isinstance(value, dict):
        return {k: _redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, secrets) for v in value]
    return value


def _proxy_secrets(proxy) -> set:
    """Пароль и полный URL прокси, которые нельзя показывать в status/логах."""
    secrets = set()
    proxy = str(proxy or "")
    if proxy:
        secrets.add(proxy)
        try:
            password = urllib.parse.urlsplit(proxy).password
        except (ValueError, TypeError):
            password = None
        if password:
            secrets.add(urllib.parse.unquote(password))
    return secrets


def _config_secrets(cfg: dict) -> set:
    secrets = set()
    if not isinstance(cfg, dict):
        return secrets
    for part in ("llm", "tts"):
        sec = cfg.get(part)
        if isinstance(sec, dict) and isinstance(sec.get("api_key"), str):
            secrets.add(sec["api_key"])
    llm = cfg.get("llm")
    if isinstance(llm, dict):
        secrets |= _proxy_secrets(llm.get("proxy"))
    keys = cfg.get("keys")
    if isinstance(keys, dict):
        for value in keys.values():
            if isinstance(value, str):
                secrets.add(value)
    return secrets


def _restrict_config_permissions(path: Path):
    """config.json содержит API-ключи: на POSIX держим права 600."""
    if os.name != "posix":
        return
    try:
        if path.is_file():
            os.chmod(path, 0o600)
    except OSError:
        pass                 # файловая система без chmod (FAT и т.п.) — не повод падать


def _audio_path(name: str) -> Path:
    """Локальный файл для озвучки: на колонке /data/local/tmp, иначе системный tmp."""
    candidate = Path("/data/local/tmp") / name
    if os.name != "nt":
        try:
            if candidate.parent.is_dir() and os.access(candidate.parent, os.W_OK):
                return candidate
        except OSError:
            pass
    return Path(tempfile.gettempdir()) / name


def _load_json_dict(path) -> tuple:
    """(dict, ошибка). Ошибку возвращаем отдельно, чтобы print_models не ходил
    в сеть по дефолтному адресу, если config.json на колонке битый."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return None, str(e)
    if not isinstance(data, dict):
        return None, "корень config.json должен быть объектом"
    return data, ""


def _as_str(value, default: str = "", field: str = "", errors=None) -> str:
    if isinstance(value, str):
        return value.strip()
    if errors is not None:
        errors.append(f"{field}: ожидается строка")
    return default


def _as_bool(value, default: bool, field: str = "", errors=None) -> bool:
    if isinstance(value, bool):
        return value
    if errors is not None:
        errors.append(f"{field}: ожидается true или false")
    return default


def _clamp_int(value, limits, default: int, field: str, errors) -> int:
    lo, hi = limits
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field}: ожидается целое число")
        return default
    if value < lo or value > hi:
        errors.append(f"{field}: допустимо от {lo} до {hi}")
    return max(lo, min(hi, value))


def _clamp_float(value, limits, default: float, field: str, errors) -> float:
    lo, hi = limits
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field}: ожидается число")
        return default
    try:
        value = float(value)
    except OverflowError:
        errors.append(f"{field}: число слишком большое")
        return default
    if not math.isfinite(value):
        errors.append(f"{field}: ожидается конечное число")
        return default
    if value < lo or value > hi:
        errors.append(f"{field}: допустимо от {lo} до {hi}")
    return max(lo, min(hi, value))


def _clean_phrases(value, field: str, errors, limit: int, max_len: int) -> list:
    out = []
    for item in value:
        if not isinstance(item, str):
            errors.append(f"{field}: фразы должны быть строками")
            continue
        text = item.strip()
        if text:
            out.append(text[:max_len])
        if len(out) >= limit:
            break
    return out


def _validate_config(raw) -> tuple:
    """Приводит прочитанный JSON к схеме. Возвращает (cfg, список ошибок).

    Ошибка в одном поле не отменяет остальные: неверное значение заменяется
    дефолтом, а причина попадает в cfg_error.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    errors = []
    if not isinstance(raw, dict):
        return cfg, ["Ошибка настроек: корень config.json должен быть объектом"]

    if "enabled" in raw:
        cfg["enabled"] = _as_bool(raw["enabled"], cfg["enabled"], "enabled", errors)

    if "triggers" in raw:
        if isinstance(raw["triggers"], list):
            cfg["triggers"] = _clean_phrases(raw["triggers"], "triggers", errors, 20, 80)
        else:
            errors.append("triggers: ожидается список фраз")

    # llm
    llm_raw = raw.get("llm", {})
    if not isinstance(llm_raw, dict):
        errors.append("llm: ожидается объект")
        llm_raw = {}
    llm = cfg["llm"]
    if "base_url" in llm_raw:
        value = llm_raw["base_url"]
        if not isinstance(value, str):
            errors.append("llm.base_url: ожидается строка")
        elif value.strip():
            try:
                llm["base_url"] = validate_base_url(value)
            except ValueError as e:
                errors.append(f"llm.base_url: {e}")
                llm["base_url"] = ""      # не подменяем опасный адрес адресом OpenAI
    if "api_key" in llm_raw:
        llm["api_key"] = _as_str(llm_raw["api_key"], "", "llm.api_key", errors)
    if "model" in llm_raw:
        llm["model"] = _as_str(llm_raw["model"], llm["model"], "llm.model", errors) or llm["model"]
    if "system_prompt" in llm_raw:
        llm["system_prompt"] = (_as_str(llm_raw["system_prompt"], "", "llm.system_prompt", errors)
                                or DEFAULT_PROMPT)
    if "proxy" in llm_raw:
        llm["proxy"] = _as_str(llm_raw["proxy"], "", "llm.proxy", errors)
    if "max_tokens" in llm_raw:
        llm["max_tokens"] = _clamp_int(llm_raw["max_tokens"], MAX_TOKENS_LIMITS,
                                       DEFAULT_CONFIG["llm"]["max_tokens"], "llm.max_tokens", errors)
    if "history" in llm_raw:
        llm["history"] = _clamp_int(llm_raw["history"], HISTORY_LIMITS,
                                    DEFAULT_CONFIG["llm"]["history"], "llm.history", errors)

    # tts
    tts_raw = raw.get("tts", {})
    if not isinstance(tts_raw, dict):
        errors.append("tts: ожидается объект")
        tts_raw = {}
    tts = cfg["tts"]
    if "provider" in tts_raw:
        if isinstance(tts_raw["provider"], str):
            provider = tts_raw["provider"].strip().lower()
            tts["provider"] = provider[:32] or "alice"
        else:
            errors.append("tts.provider: ожидается строка")
    if "base_url" in tts_raw:
        value = tts_raw["base_url"]
        if not isinstance(value, str):
            errors.append("tts.base_url: ожидается строка")
        elif value.strip():
            try:
                tts["base_url"] = validate_base_url(value)
            except ValueError as e:
                errors.append(f"tts.base_url: {e}")
                tts["base_url"] = ""
    if "api_key" in tts_raw:
        tts["api_key"] = _as_str(tts_raw["api_key"], "", "tts.api_key", errors)
    if "model" in tts_raw:
        tts["model"] = _as_str(tts_raw["model"], tts["model"], "tts.model", errors) or tts["model"]
    if "voice" in tts_raw:
        tts["voice"] = _as_str(tts_raw["voice"], tts["voice"], "tts.voice", errors) or tts["voice"]
    if "speed" in tts_raw:
        tts["speed"] = _clamp_float(tts_raw["speed"], TTS_SPEED_LIMITS,
                                    DEFAULT_CONFIG["tts"]["speed"], "tts.speed", errors)

    # led
    led_raw = raw.get("led", {})
    if not isinstance(led_raw, dict):
        errors.append("led: ожидается объект")
        led_raw = {}
    led = cfg["led"]
    if "enabled" in led_raw:
        led["enabled"] = _as_bool(led_raw["enabled"], led["enabled"], "led.enabled", errors)
    if "color" in led_raw:
        color = _as_str(led_raw["color"], "", "led.color", errors)
        if color:
            if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                led["color"] = color
            else:
                errors.append("led.color: ожидается цвет вида #rrggbb")

    # mode
    mode_raw = raw.get("mode", {})
    if not isinstance(mode_raw, dict):
        errors.append("mode: ожидается объект")
        mode_raw = {}
    mode = cfg["mode"]
    for key, limit in (("enter", 10), ("exit", 10)):
        if key in mode_raw:
            if isinstance(mode_raw[key], list):
                mode[key] = _clean_phrases(mode_raw[key], f"mode.{key}", errors, limit, 60)
            else:
                errors.append(f"mode.{key}: ожидается список фраз")
    if "timeout_min" in mode_raw:
        mode["timeout_min"] = _clamp_int(mode_raw["timeout_min"], TIMEOUT_MIN_LIMITS,
                                         DEFAULT_CONFIG["mode"]["timeout_min"], "mode.timeout_min", errors)
    if "follow_up" in mode_raw:
        mode["follow_up"] = _as_bool(mode_raw["follow_up"], mode["follow_up"], "mode.follow_up", errors)

    # keys: {base_url: api_key}
    keys_raw = raw.get("keys", {})
    keys = {}
    if not isinstance(keys_raw, dict):
        errors.append("keys: ожидается объект с ключами по адресу API")
    else:
        for base, key in keys_raw.items():
            if not isinstance(base, str) or not isinstance(key, str):
                errors.append("keys: адреса и ключи должны быть строками")
                continue
            base_id = key_id(base)
            key = key.strip()
            if base_id and key:
                keys[base_id] = key
    cfg["keys"] = keys

    # Ключ берём по адресу API, иначе остаётся legacy api_key из раздела.
    for part in ("llm", "tts"):
        base = key_id(cfg[part].get("base_url", ""))
        if base and keys.get(base):
            cfg[part]["api_key"] = keys[base]

    return cfg, errors


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


def phrase_from_log_line(line: str) -> str:
    """Распознанная фраза из строки logcat, если это событие речи Алисы на Максе.

    Android SDK на Станции Макс пишет распознанный текст в
    `onAliceStateChanged: BUSY, recognizedPhrase='…'`; в некоторых сборках
    встречается и `onRequestStateChanged: LISTENING, recognizedPhrase='…'`.
    """
    if "recognizedPhrase='" not in line:
        return ""
    if "onAliceStateChanged: BUSY" not in line and "onRequestStateChanged: LISTENING" not in line:
        return ""
    m = re.search(r"recognizedPhrase='([^']*)'", line)
    return m.group(1).strip() if m else ""


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


def _acquire_assistant_lock(path: Path):
    """Эксклюзивный lock: второй запуск из boot_hook или Yapanel не станет демоном."""
    if os.name != "posix":
        return True, None
    import fcntl

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False, None
    except BaseException:
        os.close(fd)
        raise
    return True, fd


class AssistantDaemon:
    def __init__(self, work_dir: str = "/data/quasar/webpanel/assistant"):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.work_dir / "config.json"
        self.status_path = Path("/tmp/webpanel_assistant.json")
        self.cmd_path = Path("/tmp/webpanel_assistant.cmd")
        self.sd_path = Path("/tmp/webpanel_assistant.sd")
        self.tts_path = _audio_path(Path(TTS_FILE).name)
        self.google_tts_path = _audio_path(Path(GOOGLE_TTS_FILE).name)

        self.cfg_error = ""
        self._secrets_seen = set()
        self.cfg = self.load_config()
        try:
            self.cfg_mtime = self.cfg_path.stat().st_mtime
        except OSError:
            self.cfg_mtime = 0
        self.state = "idle"
        self.error = ""
        self.history = []
        self.convo = []
        self.last_turn = 0
        self.mode = False
        self.mode_at = 0
        self.route = False
        self.ours = False
        self.say_at = 0
        self.last_phrase = ""
        self.last_phrase_at = 0.0
        self.lock = threading.Lock()
        self.running = True

        self.glagol = GlagolClient()

    def load_config(self) -> dict:
        """Читает config.json по схеме. Битый файл/мусор в полях не роняет демон:
        проблемные значения заменяются дефолтами, причина остаётся в cfg_error."""
        self.cfg_error = ""
        if not self.cfg_path.is_file():
            return copy.deepcopy(DEFAULT_CONFIG)
        _restrict_config_permissions(self.cfg_path)
        try:
            raw = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.cfg_error = f"Ошибка чтения конфига: {e}"
            return copy.deepcopy(DEFAULT_CONFIG)
        cfg, errors = _validate_config(raw)
        if not hasattr(self, "_secrets_seen"):
            self._secrets_seen = set()
        self._secrets_seen.update(_config_secrets(cfg))
        if errors:
            self.cfg_error = "; ".join(errors[:8])
        return cfg

    def reload_config_if_changed(self):
        try:
            mtime = self.cfg_path.stat().st_mtime
            if mtime != self.cfg_mtime:
                self.cfg_mtime = mtime
                with self.lock:
                    self.cfg = self.load_config()   # load_config сам выставляет cfg_error
        except Exception:
            pass

    def write_status(self):
        with self.lock:
            secrets = set(getattr(self, "_secrets_seen", set())) | _config_secrets(self.cfg)
            st = {
                "pid": os.getpid(),
                "version": VERSION,
                "state": self.state,
                "error": _redact_secrets(self.error, secrets),
                "config_error": _redact_secrets(self.cfg_error, secrets),
                "enabled": bool(self.cfg.get("enabled", True)),
                "history": [
                    _redact_value(entry, secrets)
                    for entry in self.history[-20:] if isinstance(entry, dict)
                ],
                "at": int(time.time()),
                "mode": self.mode,
                "route": self.route
            }
            if self.mode:
                mode = self.cfg.get("mode") if isinstance(self.cfg.get("mode"), dict) else {}
                try:
                    timeout_min = max(1, min(240, int(mode.get("timeout_min", 10))))
                except (TypeError, ValueError):
                    timeout_min = 10
                st["mode_until"] = int(self.mode_at + timeout_min * 60)

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
        if not isinstance(cmd, dict):
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
            cfg = self.cfg.get("llm") if isinstance(self.cfg.get("llm"), dict) else {}
            base_url = cfg.get("base_url", DEFAULT_CONFIG["llm"]["base_url"])
            api_key = _as_str(cfg.get("api_key"), "", "llm.api_key")
            model = _as_str(cfg.get("model"), DEFAULT_CONFIG["llm"]["model"], "llm.model")
            system_prompt = (_as_str(cfg.get("system_prompt"), "", "llm.system_prompt")
                             or DEFAULT_PROMPT)
            proxy = _as_str(cfg.get("proxy"), "", "llm.proxy")
            max_tokens = _clamp_int(cfg.get("max_tokens", DEFAULT_CONFIG["llm"]["max_tokens"]),
                                    MAX_TOKENS_LIMITS, DEFAULT_CONFIG["llm"]["max_tokens"],
                                    "llm.max_tokens", [])
            history_len = _clamp_int(cfg.get("history", DEFAULT_CONFIG["llm"]["history"]),
                                     HISTORY_LIMITS, DEFAULT_CONFIG["llm"]["history"],
                                     "llm.history", [])

            # Сброс контекста если пауза больше 10 минут
            if time.time() - self.last_turn > 600:
                self.convo = []

            messages = [{"role": "system", "content": system_prompt}]
            if history_len > 0:
                messages.extend(self.convo[-history_len * 2:])
            messages.append({"role": "user", "content": question})

        if not model:
            raise ValueError("не задана модель")
        url = api_url(base_url, "/chat/completions")   # проверяет http/https и служебные адреса
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        opener = self._opener(proxy)
        req = urllib.request.Request(url, data=data, headers=headers)
        with opener.open(req, timeout=45) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        resp_data = json.loads(raw)
        if not isinstance(resp_data, dict):
            raise ValueError("Непонятный ответ API")
        choices = resp_data.get("choices") or []
        if not choices:
            raise ValueError("Модель вернула пустой ответ")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        answer = str((message or {}).get("content") or "").strip()
        if not answer:
            raise ValueError("Модель вернула пустой ответ")
        return answer

    @staticmethod
    def _opener(proxy: str):
        if proxy and urllib.parse.urlsplit(proxy).scheme.lower() in ("socks5", "socks5h"):
            from assistant_socks import build_socks_opener
            return build_socks_opener(proxy)
        handler_args = {}
        if proxy:
            handler_args["http"] = proxy
            handler_args["https"] = proxy
        proxy_handler = urllib.request.ProxyHandler(handler_args) if proxy else urllib.request.ProxyHandler()
        return urllib.request.build_opener(proxy_handler)

    def _play_audio(self, path: Path) -> bool:
        """Проигрывает готовый файл: сначала playaudio (Android), затем aplay."""
        for cmd in (["/odm/bin/playaudio", str(path)], ["aplay", "-q", str(path)]):
            try:
                result = subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=120)
            except FileNotFoundError:
                continue
            except Exception:
                continue
            rc = getattr(result, "returncode", 0)
            if not isinstance(rc, int) or rc == 0:   # Mock в тестах не сообщает код
                return True
        return False

    def speak_custom(self, text: str, tts: dict = None) -> bool:
        """OpenAI-совместимый `/audio/speech`: свой голос вместо Алисы.

        Возвращает False при любой ошибке, чтобы вызывающий код перешёл к прежнему fallback.
        """
        if not isinstance(tts, dict):
            with self.lock:
                tts = dict(self.cfg.get("tts") or {}) if isinstance(self.cfg.get("tts"), dict) else {}
                llm = self.cfg.get("llm") if isinstance(self.cfg.get("llm"), dict) else {}
        else:
            with self.lock:
                llm = self.cfg.get("llm") if isinstance(self.cfg.get("llm"), dict) else {}
        provider = _as_str(tts.get("provider"), "alice", "tts.provider").lower() or "alice"
        if provider == "alice":
            return False
        try:
            url = api_url(tts.get("base_url"), "/audio/speech")
            model = _as_str(tts.get("model"), "tts-1", "tts.model") or "tts-1"
            voice = _as_str(tts.get("voice"), "alloy", "tts.voice") or "alloy"
            speed = _clamp_float(tts.get("speed", 1.0), TTS_SPEED_LIMITS, 1.0, "tts.speed", [])
            api_key = _as_str(tts.get("api_key"), "", "tts.api_key")
            proxy = _as_str(llm.get("proxy"), "", "llm.proxy")
        except (TypeError, ValueError):
            return False

        payload = {
            "model": model,
            "input": for_speech(text),
            "voice": voice,
            "response_format": "wav",
            "speed": speed
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "audio/wav"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            if proxy:
                with self._opener(proxy).open(req, timeout=TTS_TIMEOUT) as resp:
                    audio = resp.read(MAX_AUDIO_BYTES + 1)
            else:
                with urllib.request.urlopen(req, timeout=TTS_TIMEOUT) as resp:
                    audio = resp.read(MAX_AUDIO_BYTES + 1)
        except Exception:
            return False
        if not audio or len(audio) > MAX_AUDIO_BYTES or audio.lstrip()[:1] == b"{":
            return False

        try:
            tts_path = Path(self.tts_path)
            tts_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = tts_path.with_name(tts_path.name + ".part")
            tmp.write_bytes(audio)
            os.replace(tmp, tts_path)
        except (OSError, TypeError):
            return False
        return self._play_audio(tts_path)

    def speak_google(self, text: str) -> bool:
        """Прежний запасной путь: Google Translate TTS + playaudio."""
        try:
            enc = urllib.parse.quote(for_speech(text, limit=300))
            tts_url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl=ru&client=tw-ob&q={enc}"
            req = urllib.request.Request(tts_url, headers={"User-Agent": "Mozilla/5.0"})
            audio_path = Path(self.google_tts_path)
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(req, timeout=10) as resp:
                audio_path.write_bytes(resp.read())
            return self._play_audio(audio_path)
        except Exception:
            return False

    def speak(self, text: str):
        with self.lock:
            self.state = "speaking"
            self.say_at = time.time()
            self.ours = True
            tts = dict(self.cfg.get("tts") or {}) if isinstance(self.cfg.get("tts"), dict) else {}
            provider = _as_str(tts.get("provider"), "alice", "tts.provider").lower() or "alice"
        self.write_status()

        success = False
        if provider == "alice":
            success = self.glagol.say(text)
        elif tts.get("base_url"):
            try:
                success = self.speak_custom(text, tts)
            except Exception:
                success = False

        if not success:
            success = self.speak_google(text)

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
            with self.lock:
                secrets = set(getattr(self, "_secrets_seen", set())) | _config_secrets(self.cfg)
                err_msg = _redact_secrets(str(e), secrets)
                self.error = err_msg

        duration_ms = int((time.time() - t0) * 1000)
        with self.lock:
            secrets = set(getattr(self, "_secrets_seen", set())) | _config_secrets(self.cfg)
            entry = {
                "at": int(time.time()),
                "source": source,
                "question": _redact_secrets(question, secrets),
                "answer": _redact_secrets(answer, secrets),
                "error": _redact_secrets(err_msg, secrets),
                "ms": duration_ms
            }
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
            triggers = self.cfg.get("triggers")
            mode_cfg = self.cfg.get("mode") if isinstance(self.cfg.get("mode"), dict) else {}
            enter_phrases = mode_cfg.get("enter") if isinstance(mode_cfg.get("enter"), list) else []
            exit_phrases = mode_cfg.get("exit") if isinstance(mode_cfg.get("exit"), list) else []
            if not isinstance(triggers, list):
                triggers = []

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
                    phrase = phrase_from_log_line(line)
                    if phrase:
                        with self.lock:
                            ours = self.ours
                        if ours:
                            if time.time() - self.say_at < 20:
                                continue
                            with self.lock:
                                self.ours = False
                        now = time.time()
                        # одно и то же событие может прийти из двух состояний SDK
                        if phrase == self.last_phrase and now - self.last_phrase_at < 2.0:
                            continue
                        self.last_phrase, self.last_phrase_at = phrase, now
                        self.handle_phrase(phrase)
                    elif "onAliceStateChanged: IDLE" in line:
                        with self.lock:
                            self.ours = False
            except Exception:
                time.sleep(2)

    def run(self):
        claimed, lock_fd = _acquire_assistant_lock(
            self.sd_path.with_name(self.sd_path.name + ".lock")
        )
        if not claimed:
            return
        try:
            self.sd_path.write_text(str(os.getpid()), encoding="utf-8")
            self.write_status()

            t = threading.Thread(target=self.run_logcat_listener, daemon=True)
            t.start()

            try:
                while self.running:
                    self.reload_config_if_changed()
                    self.check_commands()
                    # Проверка таймаута режима ассистента
                    timed_out = False
                    with self.lock:
                        if self.mode:
                            mode_cfg = self.cfg.get("mode") if isinstance(self.cfg.get("mode"), dict) else {}
                            try:
                                timeout_min = max(1, min(240, int(mode_cfg.get("timeout_min", 10))))
                            except (TypeError, ValueError):
                                timeout_min = 10
                            if time.time() - self.mode_at > timeout_min * 60:
                                self.mode = False
                                timed_out = True
                    if timed_out:
                        self.write_status()
                    time.sleep(0.3)
            finally:
                self.running = False
                self.sd_path.unlink(missing_ok=True)
                self.status_path.unlink(missing_ok=True)

        finally:
            if lock_fd is not None:
                os.close(lock_fd)


def print_models(config_path: str, override_path: str):
    """Вывод списка доступных моделей API в JSON для веб-панели."""
    cfg, cfg_error = ({}, "")
    if os.path.isfile(config_path):
        _restrict_config_permissions(Path(config_path))
        cfg, cfg_error = _load_json_dict(config_path)
        cfg = cfg or {}
    over, _ = _load_json_dict(override_path) if override_path and override_path != "-" else ({}, "")
    over = over or {}

    llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    keys = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
    over_base = over.get("base_url") if isinstance(over.get("base_url"), str) else ""
    if not over_base and cfg_error:
        print(json.dumps({"error": f"Ошибка конфига: {cfg_error}"}, ensure_ascii=False))
        return
    cfg_base = llm.get("base_url") if isinstance(llm.get("base_url"), str) else ""
    base_url = (over_base or cfg_base or DEFAULT_CONFIG["llm"]["base_url"]).strip()

    try:
        base_url = validate_base_url(base_url)
    except ValueError as e:
        print(json.dumps({"error": f"Ошибка адреса API: {e}"}, ensure_ascii=False))
        return

    # Если адрес сменили в форме — не отправляем туда ключ прежнего провайдера.
    same_provider = not over_base or key_id(over_base) == key_id(cfg_base)
    if same_provider:
        api_key = (over.get("api_key") or llm.get("api_key")
                   or keys.get(key_id(base_url), ""))
    else:
        api_key = over.get("api_key") or keys.get(key_id(base_url), "")
    api_key = _as_str(api_key, "", "api_key")
    proxy = _as_str(over.get("proxy") or llm.get("proxy"), "", "proxy")

    url = api_url(base_url, "/models")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        opener = AssistantDaemon._opener(proxy)
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=15) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("непонятный ответ API")
        models = []
        for item in data.get("data") or []:
            mid = item.get("id") if isinstance(item, dict) else None
            if mid:
                models.append(str(mid))
        models.sort()
        print(json.dumps({"models": models}, ensure_ascii=False))
    except Exception as e:
        safe_error = _redact_secrets(str(e), {api_key} | _proxy_secrets(proxy))
        print(json.dumps({"error": f"Ошибка запроса моделей: {safe_error}"}, ensure_ascii=False))


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
