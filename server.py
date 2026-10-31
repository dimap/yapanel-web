#!/usr/bin/env python3
"""Веб-панель для Яндекс Станций (Лайт 2, Мини 3, Мини 3 Про, Станция 2) с root-доступом по adb.

Запуск:  python3 server.py [--device 192.168.1.50] [--host 127.0.0.1] [--port 8765] [--open]
Зависимости: Python 3.9+ (только стандартная библиотека) и adb; ffmpeg и openssl — по желанию.
Все параметры можно задать и переменными окружения (см. --help), так панель запускается в Docker.
"""
import ipaddress
import argparse
import base64
import collections
import contextlib
import hashlib
import io
import json
import math
import os
import platform
import random
import re
import shlex
import posixpath
import shutil
import signal
import socket
import secrets
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import access
import agent as agentlink
import glagol

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MIME = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
        ".stl": "model/stl", ".png": "image/png", ".svg": "image/svg+xml",
        ".webmanifest": "application/manifest+json"}

QUASAR = "/system/vendor/quasar"
QRUN = f"LD_LIBRARY_PATH={QUASAR}/libs {QUASAR}"
SOUNDS_DIR = f"{QUASAR}/sounds"
PATTERNS = {"ring": f"{QUASAR}/ledpatterns/oknix", "clock": f"{QUASAR}/ledpatterns/clock"}
LOGS_DIR = "/data/quasar/daemons_logs"
LEDS = "/sys/class/leds"

# Дисплей часов: 4 позиции × сегменты A–H, затем дополнительные символы.
# Порядок дополнительных байтов в кадре — по проверкам заставки на колонке: значок градуса, верхняя точка
# двоеточия, нижняя, дефис по центру (дефис — «рот» в анимациях Алисы, в их файлах пульсирует только он).
CLOCK_EXTRAS = ["DEGREE", "DH", "DL", "DM"]
CLOCK_LEDS = [f"{s}{p}" for p in range(4) for s in "ABCDEFGH"] + CLOCK_EXTRAS
CLOCK_FRAME = 1 + len(CLOCK_LEDS) + 2  # флаг, яркости, длительность (мс, big-endian)
WEB_DIR = "/data/quasar/webpanel"
PLAY_LOG = "/tmp/webpanel_play.log"   # stderr aplay: по нему видно, почему звук не заиграл
BOOT_TARGET = f"{QUASAR}/ledpatterns/clock/boot.binscled.zst"
BOOT_CUSTOM = f"{WEB_DIR}/boot.binscled.zst"
BOOT_ORIG = f"{WEB_DIR}/boot.orig.binscled.zst"
BOOT_HOOK = f"{WEB_DIR}/boot_hook.sh"
# Пустой файл-флаг Quasar «заставка уже показана»: без него maind проигрывает заставку со звуком
# и сразу создаёт файл заново, поэтому сам по себе флаг срабатывает один раз.
NO_BOOT_SOUND = "/data/quasar/data/no_boot_sound"
BOOT_ALWAYS = f"{WEB_DIR}/boot_every_time"  # наш флаг: хук удаляет no_boot_sound при каждой загрузке
# Звук запуска (BootSound): несмотря на расширение, внутри MP3 44,1 кГц стерео.
SOUND_TARGET = f"{QUASAR}/sounds/common/boot.wav"
SOUND_CUSTOM = f"{WEB_DIR}/boot_sound.wav"
SOUND_ORIG = f"{WEB_DIR}/boot_sound.orig.wav"
# Свои системные файлы Quasar: override/<путь от QUASAR> накрывает QUASAR/<путь> через bind mount. Оригинал в
# /system только для чтения и остаётся под подменой — копии не нужны, «вернуть» = снять подмену и удалить свой.
OVR_DIR = f"{WEB_DIR}/override"
# Свои анимации — отдельно от подмен, встроенные не трогают: anims/ring/<имя>.led, anims/clock/<имя>.binscled.
# В списках анимаций они — «my/<имя>…» рядом со встроенными.
MY_ANIM_DIR = f"{WEB_DIR}/anims"
MY_ANIM_NAME = re.compile(r"[\w-]{1,40}")
# YAM (github.com/nyakokitsu/yam): менеджер пакетов для Станций. На колонке — демон yamd с HTTP API на :8099.
YAM_DIR = "/data/quazar/yam"
YAMD = f"{YAM_DIR}/bin/yamd"
YAM_PLUGINS = f"{YAM_DIR}/data/plugins"
# Бинарникам пакетов — право на запуск: некоторые .yap (например, fastfetch 2.70.0) собраны с файлом 0666,
# yamd распаковывает как есть и потом получает «Permission denied». Путь берём из [launch] path в plugin.toml.
YAM_FIX_EXEC = (f"for t in {YAM_PLUGINS}/*/plugin.toml; do [ -f \"$t\" ] || continue; d=${{t%/plugin.toml}}; "
                "grep -q '^kind *= *\"binary\"' \"$t\" || continue; "
                "p=$(sed -n 's/^path *= *\"\\(.*\\)\"/\\1/p' \"$t\" | head -n 1); "
                "case \"$p\" in ''|/*|*..*) continue ;; esac; [ -f \"$d/$p\" ] && chmod 755 \"$d/$p\"; done; true")
# В консоли панели бинарники пакетов доступны по имени (fastfetch, …).
YAM_PATH = (f"for d in {YAM_PLUGINS}/*/*/; do [ -d \"$d\" ] && PATH=\"$PATH:${{d%/}}\"; done; export PATH"
            f"; [ -d /data/yam/bin ] && PATH=\"$PATH:/data/yam/bin\"; export PATH; ")
YAM_PORT = 8099
YAM_REGISTRY = "https://raw.githubusercontent.com/nyakokitsu/yapkg/registry/index.json"
# yam 2 (релизы yam-v0.2+): уже не демон, а пакетный менеджер — команда yam на самой колонке. Ставим как их
# install.sh: бинарник, профиль платформы и обёртка yam-run (она задаёт платформу и корень) в /data/yam.
YAM2_ROOT = "/data/yam"
YAM2 = f"{YAM2_ROOT}/bin/yam"
YAM2_RUN = f"{YAM2_ROOT}/bin/yam-run"
# Официальный каталог и его ключ — из docs/QUICKSTART.md yam. Ключ проверяет подписи индексов на колонке.
YAM2_REGISTRY = ("official", "https://repo.yse.wtf/v2", "ed25519:5Rz8F47uftWVU5kLgWYhprkEZ4rpP2kecUge/a7KQas=")
# Службы пакетов yam только описывает, а запускать их — дело хоста; хост тут панель. На каждую службу — свой
# скрипт запуска yam2/<пакет~служба>.sh, файл yam2/boot/<то же> — «запускать при загрузке» (его читает хук).
YAM2_SVC_DIR = f"{WEB_DIR}/yam2"
YAM2_SVC_PID = "/tmp/webpanel_yam2_"     # + id службы + .pid
YAM2_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# Гасит службу по pid из pidfile ($p) вместе с её программой. start-stop-daemon -b пишет pid скрипта, а группа
# процессов — у его родителя после setsid, поэтому номер группы берём из /proc/$p/stat (пятое поле).
YAM2_KILL = ("g=$(awk '{print $5}' /proc/$p/stat 2>/dev/null); "
             "if [ \"${g:-0}\" -gt 1 ] && [ \"$g\" != \"$(awk '{print $5}' /proc/$$/stat)\" ]; "
             "then kill -TERM -- -$g 2>/dev/null; else kill $p; fi")
# Версия панели — в файле VERSION; сборка релиза проверяет, что метка vX.Y.Z с ним совпадает.
try:
    VERSION = (Path(__file__).resolve().parent / "VERSION").read_text().strip()
except OSError:
    VERSION = "0.0.0"
REPO = "dimap/yapanel-web"
LED_PREFIX = "plum"
# Каталог сегментов часов, если он не {LEDS}/{LED_PREFIX}:white:clock: у Мини 2 сегменты называются иначе,
# и профиль раскладывает их по именам Лайт 2 ссылками в /tmp (см. profiles.py).
CLOCK_PATH = None


def clock_path():
    return CLOCK_PATH or f"{LEDS}/{LED_PREFIX}:white:clock"


GLAGOL_PLATFORM = "plum"   # так Лайт 2 называет себя в запросах check_updates
# Где панель хранит своё (токен Яндекса, пароль, сертификаты, история графиков). По умолчанию — рядом с server.py;
# --data-dir / PANEL_DATA_DIR меняет это (в Docker — том /data). Пути пересчитываются в set_data_dir().
DATA_DIR = ROOT


def set_data_dir(path):
    global DATA_DIR, AUTH_FILE, PANEL_AUTH_FILE, METRICS_FILE, TLS_DIR, SETTINGS_FILE, AGENT_FILE
    DATA_DIR = Path(path).resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE = DATA_DIR / ".yandex_auth.json"
    PANEL_AUTH_FILE = DATA_DIR / ".panel_auth.json"
    METRICS_FILE = DATA_DIR / ".metrics.json"
    TLS_DIR = DATA_DIR / ".tls"
    SETTINGS_FILE = DATA_DIR / ".panel_settings.json"
    AGENT_FILE = DATA_DIR / ".agent.json"
    JOURNAL.open(DATA_DIR / ".panel_journal.jsonl")


def redact(text):
    """Убирает из текста то, чему не место в отчёте: токены и ключи, адреса, MAC, e-mail, имя пользователя в путях."""
    text = re.sub(r"\b(?:y0_|AQAAAA)[\w-]{10,}", "••••", text)
    text = re.sub(r"\b(sk|rk|pk|gsk|xai)-[\w-]{8,}", r"\1-••••", text)
    text = re.sub(r"(?i)(bearer\s+|token[\"']?\s*[:=]\s*[\"']?|api[_-]?key[\"']?\s*[:=]\s*[\"']?)[\w.~+/-]{12,}", r"\1••••", text)
    # IPv4 — но не версия прошивки вида 0.369.1.50.4794…: соседних цифр и точек быть не должно
    text = re.sub(r"(?<![\d.])(?!127\.0\.0\.1(?![\d.]))(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)", "x.x.x.x", text)
    text = re.sub(r"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", "••:••:••:••:••:••", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "•••@•••", text)
    return re.sub(r"(?i)([/\\](?:home|users)[/\\])[^/\\\s]+", r"\1<user>", text)


class Journal:
    """Журнал панели: что нажимали, чем ответила колонка, что со связью. Тела запросов не пишем — в них бывают
    ключи и пароли; сообщения проходят redact. Последние LIMIT записей лежат в .panel_journal.jsonl рядом
    с данными панели — их показывает вкладка «Логи» и берёт «Отчёт для Issue»."""
    LIMIT = 1000

    def __init__(self):
        self.path, self.lock = None, threading.Lock()
        self.items = collections.deque(maxlen=self.LIMIT)
        self.written = 0

    def open(self, path):
        with self.lock:
            self.path, self.items = Path(path), collections.deque(maxlen=self.LIMIT)
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines()[-self.LIMIT:]:
                    with contextlib.suppress(ValueError):
                        self.items.append(json.loads(line))
            except OSError:
                pass

    def add(self, msg, level="info"):
        msg = redact(str(msg))[:600]
        with self.lock:
            last = self.items[-1] if self.items else None
            if last and last["msg"] == msg and last["level"] == level:
                last["n"] = last.get("n", 1) + 1   # повторы (например, опрос при обрыве связи) — одной строкой
                last["t"] = time.time()
                return
            item = {"t": time.time(), "level": level, "msg": msg}
            self.items.append(item)
            if not self.path:
                return
            self.written += 1
            try:
                if self.written % 200 == 0:   # время от времени ужимаем файл до последних LIMIT записей
                    tmp = self.path.with_suffix(".tmp")
                    tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in self.items), encoding="utf-8")
                    os.replace(tmp, self.path)
                else:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def text(self, lines=300):
        mark = {"info": " ", "warn": "⚠", "error": "✖"}
        with self.lock:
            items = list(self.items)[-lines:]
        return "\n".join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(i['t']))} {mark.get(i['level'], ' ')} {i['msg']}"
                         + (f"  (×{i['n']})" if i.get("n", 1) > 1 else "") for i in items)


JOURNAL = Journal()
PANEL_PROFILE = ("lite2", False)   # (модель, определена ли сама) — ставит main()
set_data_dir(ROOT)
# Без пароля доступны только оболочка страницы и вход; всё остальное — после входа.
PUBLIC_PATHS = {"/", "/index.html", "/manifest.webmanifest", "/sw.js", "/ca.crt", "/api/panel-auth",
                "/api/panel-auth/login"}
# Команды Glagol, которые можно отправить из панели, и разрешённые для них поля.
# Громкость: у Quasar своя шкала 0–20; колонка показывает её ×5 (0–100 с шагом 5), Glagol — /20 (0–1).
# В ALSA она переводится по volume_manager_settings.platformToAlsaMap из quasar.cfg: 0 → 0, 1 → 49, 20 → 255.
def level_to_alsa(level):          # громкость как на колонке, 0–100 → Master 0–255
    p = round(max(0.0, min(100.0, float(level))) / 5)
    return 0 if p == 0 else round(49 + (p - 1) * 206 / 19)


def alsa_to_level(v):
    v = int(v or 0)
    return 0 if v <= 0 else max(1, round((v - 49) * 19 / 206) + 1) * 5


ALICE_COMMAND_LIMIT = 1024  # Conservative panel limit, matching Dialogs original_utterance.
MUSIC_COMMANDS = {"play": (), "stop": (), "next": (), "prev": (), "rewind": ("position",),
                  "setVolume": ("volume",), "repeat": ("mode",), "shuffle": ("enable",), "sendText": ("text",)}
YAMD_CMD = f"exec {YAMD} --data-dir {YAM_DIR}/data >{YAM_DIR}/yamd.log 2>&1"
YAM_AUTOSTART = f"{WEB_DIR}/yamd_autostart"  # наш флаг: хук запускает yamd при загрузке
# Будильник: sh-демон на колонке (device/alarmd.sh), конфиг и звуки — рядом.
ALARM_DIR = f"{WEB_DIR}/alarm"
ALARM_CONF = f"{ALARM_DIR}/alarms.conf"
ALARMD = f"{ALARM_DIR}/alarmd.sh"
ALARM_SD = "/tmp/webpanel_alarmd.sd"
ALARM_STATE = "/tmp/webpanel_alarm.state"
ALARM_CMD = "/tmp/webpanel_alarm.cmd"
ALARM_FIRE = "/tmp/webpanel_alarm.fire"
ALARM_SRC = Path(__file__).resolve().parent / "device" / "alarmd.sh"
SCENES_CONF = f"{ALARM_DIR}/scenes.conf"
SCENE_FIRE = "/tmp/webpanel_scene.fire"
SCENE_RUN = "/tmp/webpanel_scene."      # + id: pid идущего сценария
SCENES_LOG = f"{ALARM_DIR}/scenes.log"


def alarm_script():
    """Скрипт будильника для колонки — с \\n, даже если git на Windows переделал переводы строк."""
    return ALARM_SRC.read_bytes().replace(b"\r\n", b"\n")
ALARM_MODES = ("sound", "wave", "show", "music", "text")
ALARM_DEFAULT_SOUND = "common/timer.wav"
# Блокировка аналитики: свой /etc/hosts (копия системного + адреса аналитики на 127.0.0.1) подключается
# поверх системного через bind mount. Системный раздел не меняется; выключить — просто отмонтировать.
HOSTS_TARGET = "/etc/hosts"
HOSTS_CUSTOM = f"{WEB_DIR}/hosts"
HOSTS_MARK = "# webpanel: блокировка аналитики Яндекса"
ANALYTICS_HOSTS = ("clck.yandex.net", "report.appmetrica.yandex.net", "appmetrica.yandex.net", "metrica.yandex.net")
# Неотправленные события телеметрия копит здесь (до 1 МБ) и отправит, как только адреса откроются. Телеметрия
# читает файл при старте и дописывает его при выходе, поэтому сначала kill -9, потом удаление; launcher
# перезапускает её через секунду уже с пустой очередью. Остальное (fluent-bit, teleme3d) лежит в /tmp.
# Чистим только по действию пользователя: при включении и выключении блокировки и кнопкой.
METRICA_BATCH = "/data/quasar/appmetrica/persistedBatch"
ANALYTICS_PURGE = ("p=$(ps | grep '[m]aind --telemetry' | awk '{print $1}'); "
                   f"[ -n \"$p\" ] && kill -9 $p; rm -f {METRICA_BATCH}")
# Свой голосовой помощник (device/assistant): «Алиса, спроси GPT …» уходит выбранной модели. Программа на Go
# работает на самой колонке; панель кладёт её и config.json, проверяет текстом и читает состояние из /tmp.
ASSIST_DIR = f"{WEB_DIR}/assistant"
ASSISTD = f"{ASSIST_DIR}/assistantd"
ASSIST_CONF = f"{ASSIST_DIR}/config.json"
ASSIST_STATUS = "/tmp/webpanel_assistant.json"
ASSIST_CMD = "/tmp/webpanel_assistant.cmd"
ASSIST_SD = "/tmp/webpanel_assistant.sd"
ASSIST_RUN = f"exec {ASSISTD} -dir {ASSIST_DIR} >{ASSIST_DIR}/assistant.log 2>&1"
ASSIST_SRC = Path(__file__).resolve().parent / "device" / "assistant"
# Живой спектр с колонки: маленькая программа читает захват кодека (то, что реально играет усилитель,
# а не микрофон — микрофон остаётся Алисе) и раз в 50 мс пишет 4 полосы в /tmp. Панель читает их тем же
# потоком, что и светодиоды. Пока экспериментально: после перезагрузки колонки не поднимается сам.
VIZ_DIR = f"{WEB_DIR}/viz"
VIZD = f"{VIZ_DIR}/vizd"
VIZ_STATUS = "/tmp/webpanel_viz.json"
VIZ_SD = "/tmp/webpanel_viz.sd"
VIZ_RUN = f"exec {VIZD} >{VIZ_DIR}/viz.log 2>&1"
VIZ_SRC = Path(__file__).resolve().parent / "device" / "viz"
VIZ_ON = "/tmp/webpanel_viz.on"   # визуализатор включён в панели
VIZ_BT = "/tmp/webpanel_viz.bt"   # идёт Bluetooth-переливание: полосы считает tap из того же потока

# ---------- свои слова активации (yam-пакет tangenx/wakeword) ----------
# Демон живёт на колонке и отдаёт маленький JSON API на 8780. Панель не копирует его код:
# только читает состояние и шлёт команды (запись идёт микрофонами колонки), а API проксирует.
WAKE_PKG = "/data/yam/pkgs/tangenx/wakeword"
WAKE_SHARE = "/data/yam/share/tangenx.wakeword"
WAKE_BIN = "/data/yam/bin/wakeword"
WAKE_DATA = "/data/wakeword"
WAKE_PID = "/tmp/wakeword.pid"
WAKE_LOG = "/tmp/wakeword.log"
WAKE_AUTOSTART = f"{WEB_DIR}/wakeword.autostart"   # маркер: поднимать демон хуком панели после загрузки
WAKE_PORT = 8780
# targets из yam.yaml пакета: Макс (yandexstation_2), Стрит и Android-Станция не поддерживаются.
# что известно про пакет: README плагина + живая проверка на Лайт 2
WAKE_SUPPORT = {
    "tab": "Активация",
    "note": "Интеграция с панелью",
    "tested": ["plum", "pickle", "yandexmidi", "yandexmini_2", "bergamot"],
    "untested": ["yandexmicro", "yandexmini", "cucumber"],
    "unsupported": ["yandexstation", "orion"],
    "names": {"plum": "Лайт 2", "pickle": "Станция 2", "yandexmidi": "Станция 2",
              "yandexmini_2": "Мини 2", "bergamot": "Мини 3", "yandexmicro": "Микро",
              "yandexmini": "Мини", "cucumber": "Миди", "yandexstation": "Станция 1",
              "orion": "Макс"},
}
WAKE_TARGETS = ("yandexmicro", "plum", "yandexmini", "yandexmini_2", "bergamot", "pickle",
                "cucumber", "yandexmidi", "yandexstation", "orion")
WAKE_SETTINGS = f"{WAKE_DATA}/settings.json"
WAKE_ACTIONS_KEY = "wakeword_actions"

# ---------- эквалайзер для Bluetooth-выхода ----------
# Пресеты в виде полос (тип, частота, усиление дБ, добротность): tap на колонке получает готовые
# коэффициенты биквадов и применяет их к потоку, который идёт в Bluetooth-колонку.
EQ_FILE = f"{VIZ_DIR}/eq.json"
# Бас в слышимом небольшими колонками диапазоне; пресеты различаются и по середине.
EQ_PRESETS = {
    "flat": (),
    "rock": (("lowshelf", 150, 8.0), ("peaking", 750, -4.0, 0.8), ("highshelf", 4500, 5.0)),
    "electronic": (("lowshelf", 120, 9.0), ("peaking", 450, -3.0, 0.9),
                   ("peaking", 2800, 4.0, 0.9), ("highshelf", 6500, 5.0)),
    "vocal": (("lowshelf", 180, -6.0), ("peaking", 2200, 6.0, 0.8), ("highshelf", 5500, 3.0)),
    "night": (("lowshelf", 140, 6.0), ("highshelf", 3500, -7.0)),
}
EQ_CUSTOM_DEFAULT = ({"f": 60, "g": 0}, {"f": 230, "g": 0}, {"f": 910, "g": 0},
                     {"f": 3600, "g": 0}, {"f": 14000, "g": 0})
EQ_GAIN_MAX = 12.0


def eq_custom_bands(raw):
    """Пять полос своего эквалайзера: частота 20–20000 Гц и усиление −12…+12 дБ."""
    bands = []
    for b in list(raw or [])[:len(EQ_CUSTOM_DEFAULT)]:
        try:
            f, g = float(b.get("f")), float(b.get("g"))
        except (TypeError, ValueError, AttributeError):
            raise AdbError("плохие настройки эквалайзера")
        if not 20 <= f <= 20000:
            raise AdbError("частота вне 20–20000 Гц")
        if not -EQ_GAIN_MAX <= g <= EQ_GAIN_MAX:
            raise AdbError(f"усиление вне −{EQ_GAIN_MAX:.0f}…+{EQ_GAIN_MAX:.0f} дБ")
        bands.append({"f": round(f, 1), "g": round(g, 1)})
    bands += [dict(b) for b in EQ_CUSTOM_DEFAULT[len(bands):]]
    return bands


def eq_filters(preset, custom=None):
    """Полосы (тип, частота, усиление, добротность) для пресета или своего набора."""
    if preset != "custom":
        return list(EQ_PRESETS[preset])
    return [("peaking", b["f"], b["g"], 1.0) for b in (custom or EQ_CUSTOM_DEFAULT) if b["g"]]


def eq_coeffs(band, fs=48000):
    """Коэффициенты биквада (b0,b1,b2,a1,a2) по формулам RBJ; уже нормированы на a0."""
    kind, freq, gain = band[:3]
    q = band[3] if len(band) > 3 else 0.7
    a = 10 ** (gain / 40)
    w = 2 * math.pi * freq / fs
    cw, sw = math.cos(w), math.sin(w)
    if kind == "peaking":
        alpha = sw / (2 * q)
        b0, b1, b2 = 1 + alpha * a, -2 * cw, 1 - alpha * a
        a0, a1, a2 = 1 + alpha / a, -2 * cw, 1 - alpha / a
    else:   # полки: S = 1 (стандартный наклон)
        alpha = sw / 2 * math.sqrt(2.0)
        k = 2 * math.sqrt(a) * alpha
        if kind == "lowshelf":
            b0 = a * ((a + 1) - (a - 1) * cw + k)
            b1 = 2 * a * ((a - 1) - (a + 1) * cw)
            b2 = a * ((a + 1) - (a - 1) * cw - k)
            a0 = (a + 1) + (a - 1) * cw + k
            a1 = -2 * ((a - 1) + (a + 1) * cw)
            a2 = (a + 1) + (a - 1) * cw - k
        else:
            b0 = a * ((a + 1) + (a - 1) * cw + k)
            b1 = -2 * a * ((a - 1) + (a + 1) * cw)
            b2 = a * ((a + 1) + (a - 1) * cw - k)
            a0 = (a + 1) - (a - 1) * cw + k
            a1 = 2 * ((a - 1) - (a + 1) * cw)
            a2 = (a + 1) - (a - 1) * cw - k
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def eq_curve(preset, custom=None, points=96, fs=48000):
    """АЧХ для графика: логарифмическая сетка 20 Гц … 20 кГц и дБ."""
    coeffs = [eq_coeffs(b, fs) for b in eq_filters(preset, custom)]
    freqs = [20 * (20000 / 20) ** (i / (points - 1)) for i in range(points)]
    db = []
    for f in freqs:
        w = 2 * math.pi * f / fs
        z1 = complex(math.cos(w), -math.sin(w))
        z2 = z1 * z1
        h = 1 + 0j
        for b0, b1, b2, a1, a2 in coeffs:
            h *= (b0 + b1 * z1 + b2 * z2) / (1 + a1 * z1 + a2 * z2)
        db.append(round(20 * math.log10(abs(h)), 1))
    return freqs, db
# Агент панели (device/agent): команды, файлы и консоль по TLS с токеном — рядом с adb, а не вместо него.
# adb ставит, обновляет и чинит агента; агент молчит — панель работает через adb, как раньше.
AGENT_DIR = f"{WEB_DIR}/agent"
AGENT_BIN = f"{AGENT_DIR}/yapanel-agent"
AGENT_TOKENS = f"{AGENT_DIR}/tokens"            # sha256 токенов панелей — сами токены на колонке не хранятся
AGENT_CERT = f"{AGENT_DIR}/cert.pem"
AGENT_SD = "/tmp/webpanel_agent.sd"
AGENT_RUN = f"exec {AGENT_BIN} -dir {AGENT_DIR} -addr :{agentlink.PORT} >{AGENT_DIR}/agent.log 2>&1"
AGENT_SRC = Path(__file__).resolve().parent / "device" / "agent"
# Bluetooth выключен из панели: «Алиса, выключи блютуз» только закрывает сопряжение, а адаптер остаётся включённым.
# Флаг держит его выключенным и после перезагрузки (хук); «Алиса, включи блютуз» включает его обратно — это не мешаем.
BT_OFF = f"{WEB_DIR}/bt_off"


def bt_power_sh(on):
    return (f"dbus-send --system --print-reply --dest=org.bluez /org/bluez/hci0 org.freedesktop.DBus.Properties.Set "
            f"string:org.bluez.Adapter1 string:Powered variant:boolean:{'true' if on else 'false'} >/dev/null")


# при загрузке Bluetooth поднимается сам — первые две минуты гасим его, если панель велела держать выключенным
BT_OFF_BOOT = (f"[ -f {BT_OFF} ] && ( i=0; while [ $i -lt 120 ]; do dbus-send --system --print-reply --dest=org.bluez "
               f"/org/bluez/hci0 org.freedesktop.DBus.Properties.Get string:org.bluez.Adapter1 string:Powered 2>/dev/null "
               f"| grep -q true && {bt_power_sh(False)}; sleep 5; i=$((i + 5)); done ) >/dev/null 2>&1 &")
# Звук через Bluetooth-колонку: колонка остаётся «мозгом» (слушает, отвечает), а играет подключённая по Bluetooth.
# Весь звук Quasar держит один процесс (maind --mediad); конец его цепочки ALSA (dmix_spk_plug) подменяем на
# bluealsa своим asound.conf через bind mount. Регулятору громкости Алисы (softvol «Master») карту указываем явно:
# по цепочке, которая кончается Bluetooth, он её не находит, и плеер Алисы не открывает вывод.
# Сторож на колонке держит подмену, только пока Bluetooth-колонка подключена, иначе — встроенный динамик.
BTOUT_DIR = f"{WEB_DIR}/btout"
BTOUT_MAC = f"{BTOUT_DIR}/device"      # MAC выбранной колонки: есть файл — звук через неё
BTOUT_CONF = f"{BTOUT_DIR}/asound.conf"
BTOUT_LAST = f"{BTOUT_DIR}/last"        # последняя колонка — чтобы вернуться на неё одной кнопкой
BTOUT_NAME = f"{BTOUT_DIR}/name"        # её имя на момент подключения: без него в строке был бы MAC-адрес
BTOUT_SH = f"{BTOUT_DIR}/watch.sh"
BTOUT_SD = "/tmp/webpanel_btout.sd"
BTOUT_STATE = "/tmp/webpanel_btout.state"   # bt — играет через неё; waiting — ждём её, звук на встроенном
BTOUT_MARK = "# webpanel: btout"
BTOUT_FWD_SD = "/tmp/webpanel_btfwd.sd"
BTOUT_WATCH = r"""#!/bin/sh
# Веб-панель: звук через Bluetooth-колонку. Bluetooth-выход принимает один поток, а у Алисы голос и музыка —
# разные плееры, поэтому звук смешивается как обычно (dmix), только не в динамик, а в виртуальную петлю
# (snd-aloop), а переливалка arecord | aplay несёт смешанный поток в колонку. Пока колонка подключена —
# своя /etc/asound.conf, петля и переливалка; пропала (2 проверки подряд) — встроенный динамик.
# Подключение проверяем по D-Bus: каждый bluetoothctl оставляет в bluetoothd прошивки немного памяти навсегда.
D=__DIR__
M=$(cat $D/device 2>/dev/null) || exit 0
P=/org/bluez/hci0/dev_$(echo "$M" | tr : _)
S=__STATE__
F=__FWD__
conn() { dbus-send --system --print-reply --dest=org.bluez "$P" org.freedesktop.DBus.Properties.Get \
  string:org.bluez.Device1 string:Connected 2>/dev/null | grep -q 'boolean true'; }
ours() { grep -q ' /etc/asound.conf ' /proc/mounts && grep -q '__MARK__' /etc/asound.conf; }
quasar() { /system/vendor/quasar/q restart >/dev/null 2>&1; }
loop() { grep -q snd_aloop /proc/modules || insmod $(find /lib/modules -name snd-aloop.ko | head -n 1) pcm_substreams=1; }
fwd() {
  p=$(cat $F 2>/dev/null) && kill -0 "$p" 2>/dev/null && return
  # громкость колонки — силами bluealsa: многие колонки не слушают её по AVRCP; после переподключения сбрасывается
  dbus-send --system --print-reply --dest=org.bluealsa /org/bluealsa/hci0/dev_$(echo "$M" | tr : _)/a2dpsrc/sink \
    org.freedesktop.DBus.Properties.Set string:org.bluealsa.PCM1 string:SoftVolume variant:boolean:true >/dev/null 2>&1
  # громкость BT-колонки в bluealsa — всегда 100 %: остаются громкость Алисы и громкость на самой колонке
  ctl=$(amixer -D bluealsa scontrols 2>/dev/null | sed -n "s/^Simple mixer control '\(.*A2DP\)',0$/\1/p" | head -n 1)
  [ -n "$ctl" ] && amixer -D bluealsa sset "$ctl" 100% >/dev/null 2>&1
  # если живой спектр установлен — вставляем tap: он пересылает звук дальше и считает полосы
  tap=cat
  [ -x __VIZ__ ] && tap="__VIZ__ -tap"
  start-stop-daemon -S -b -m -p $F -x /bin/sh -- -c "arecord -q -D hw:Loopback,1,0 -f S16_LE -c 2 -r 48000 -t raw \
    --buffer-time=100000 | $tap | aplay -q -D bluealsa:DEV=$M,PROFILE=a2dp -f S16_LE -c 2 -r 48000 -t raw --buffer-time=200000"
}
fwd_stop() {   # переливалка — sh с двумя детьми: гасим всю группу процессов
  p=$(cat $F 2>/dev/null) && g=$(awk '{print $5}' /proc/$p/stat 2>/dev/null) && [ "${g:-0}" -gt 1 ] && kill -- -$g 2>/dev/null
  rm -f $F
}
miss=0; n=0
while [ -f $D/device ]; do
  if conn; then
    miss=0
    loop
    ours || { mount --bind $D/asound.conf /etc/asound.conf && quasar; }
    fwd
    echo bt > $S
  else
    miss=$((miss + 1))
    if ours && [ $miss -ge 2 ]; then fwd_stop; umount /etc/asound.conf || umount -l /etc/asound.conf; quasar; fi
    ours || echo waiting > $S
    # раз в 30 с просим колонку подключиться: включили её снова — звук вернётся на неё сам
    [ $((n % 6)) -eq 0 ] && dbus-send --system --dest=org.bluez "$P" org.bluez.Device1.Connect >/dev/null 2>&1 &
  fi
  n=$((n + 1)); sleep 5
done
fwd_stop
ours && { umount /etc/asound.conf || umount -l /etc/asound.conf; quasar; }
rm -f $S
"""


BOOT_HOOK_SCRIPT = (
    "#!/bin/sh\n"
    "# Веб-панель: своя заставка и звук запуска. Вызывается из rootkeeper.sh до того, как Quasar\n"
    "# их загрузит; подменяет стандартные файлы через bind mount (/system только для чтения).\n"
    f"[ -f {BOOT_ALWAYS} ] && rm -f {NO_BOOT_SOUND}\n"
    "bind() { [ -f \"$1\" ] && [ -f \"$2\" ] && ! grep -q \" $2 \" /proc/mounts && mount --bind \"$1\" \"$2\"; }\n"
    f"bind {BOOT_CUSTOM} {BOOT_TARGET}\n"
    f"bind {SOUND_CUSTOM} {SOUND_TARGET}\n"
    f"bind {HOSTS_CUSTOM} {HOSTS_TARGET}\n"
    f"[ -d {OVR_DIR} ] && (cd {OVR_DIR} && find . -type f) | while read -r f; do "
    f"bind \"{OVR_DIR}/${{f#./}}\" \"{QUASAR}/${{f#./}}\"; done\n"
    # Очередь аналитики при загрузке не чистим: для этого телеметрию приходится убивать kill -9, а в первую
    # минуту после старта это совпало с зависанием звука у Алисы. Адреса закрыты — очередь и так не уйдёт.
    # yamd сам после загрузки не стартует; start-stop-daemon -b — чтобы не держать rootkeeper.sh
    f"[ -f {YAM_AUTOSTART} ] && [ -x {YAMD} ] && ! pidof yamd >/dev/null && "
    f"start-stop-daemon -S -b -m -p /tmp/webpanel_yamd.sd -x /bin/sh -- -c {shlex.quote(YAMD_CMD)}\n"
    f"[ -f {ALARM_CONF} ] && [ -f {ALARMD} ] && start-stop-daemon -S -b -m -p {ALARM_SD} -x /bin/sh -- {ALARMD}\n"
    f"[ -f {ASSIST_CONF} ] && [ -x {ASSISTD} ] && start-stop-daemon -S -b -m -p {ASSIST_SD} -x /bin/sh -- -c {shlex.quote(ASSIST_RUN)}\n"
    f"[ -f {BTOUT_MAC} ] && [ -f {BTOUT_SH} ] && start-stop-daemon -S -b -m -p {BTOUT_SD} -x /bin/sh -- {BTOUT_SH}\n"
    # свои слова активации: демон из yam-пакета, стартуем из /data (rootfs-init после OTA пропадает)
    f"[ -f {WAKE_AUTOSTART} ] && [ -x {WAKE_BIN} ] && start-stop-daemon -S -b -m -N 10 -p {WAKE_PID} -x /bin/sh -- -c "
    f"{shlex.quote(f'exec {WAKE_BIN} run -data {WAKE_DATA} -negatives {WAKE_SHARE}/negatives.bin -listen 127.0.0.1:{WAKE_PORT} >> {WAKE_LOG} 2>&1')}\n"
    f"{BT_OFF_BOOT}\n"
    f"[ -f {AGENT_TOKENS} ] && [ -x {AGENT_BIN} ] && start-stop-daemon -S -b -m -p {AGENT_SD} -x /bin/sh -- -c {shlex.quote(AGENT_RUN)}\n"
    # службы пакетов yam 2, отмеченные «при загрузке»
    f"for f in {YAM2_SVC_DIR}/boot/*; do [ -f \"$f\" ] && [ -f \"{YAM2_SVC_DIR}/${{f##*/}}.sh\" ] && "
    f"start-stop-daemon -S -b -m -p \"{YAM2_SVC_PID}${{f##*/}}.pid\" -x /bin/sh -- \"{YAM2_SVC_DIR}/${{f##*/}}.sh\"; done\n"
    "exit 0\n"
)
ROOTKEEPER = "/data/quasar/rootkeeper.sh"
# rootkeeper.sh создаёт способ рутования, и он есть не у всех. Штатный onboot.sh запускает его, если файл есть, —
# тогда панель создаёт его сама и помечает этим файлом, чтобы при откате удалить целиком.
ROOTKEEPER_CREATED = f"{ROOTKEEPER}.webpanel.created"
ROOTKEEPER_CALLER = "/system/vendor/quasar/onboot.sh"
# откат созданного панелью rootkeeper.sh: после удаления нашей строки в нём остались только комментарии
ROOTKEEPER_DROP_CREATED = (f"if [ -f {ROOTKEEPER_CREATED} ] && [ -z \"$(grep -v '^#' {ROOTKEEPER} 2>/dev/null | grep -v '^[[:space:]]*$')\" ]; "
                           f"then rm -f {ROOTKEEPER} {ROOTKEEPER_CREATED} {ROOTKEEPER}.webpanel.bak; fi")
HOOK_MARK = "# webpanel-boot-hook"
# Если rootkeeper.sh нет и штатный onboot.sh его не вызывает (колонка рутована другим способом), но /etc/init.d
# доступна для записи (так её оставляют некоторые способы рутования: alicebridge, dropbear кладут туда S99z…),
# хук запускается своим скриптом оттуда. Корень сами не перемонтируем. Запускается после S99quasar: будильник,
# ассистент и службы успевают, а своя заставка и звук запуска могут не успеть.
INITD_DIR = "/etc/init.d"
INITD_HOOK = f"{INITD_DIR}/S99zwebpanel"
BOOT_MOUNTS = "/proc/mounts"


def initd_hook_script():
    return (f"#!/bin/sh\n{HOOK_MARK} — автозапуск веб-панели (rootkeeper.sh нет)\n"
            f"[ \"$1\" = start ] || exit 0\n[ -x {BOOT_HOOK} ] && {BOOT_HOOK}\nexit 0\n")


# /etc/init.d лежит на корне, который у колонки в рантайме только для чтения: свои S99z* туда в своё время
# положил способ рута, перемонтировав корень на время записи. Панель делает то же — но только по явному
# действию пользователя («Подключить хук») и всегда возвращает корень обратно в ro.
ROOT_RW = "mount -o remount,rw / 2>/dev/null || mount -o remount,rw ubi0:rootfs /"
ROOT_RO = "mount -o remount,ro / 2>/dev/null"


def with_root_rw(script):
    """sh: выполнить с корнем, перемонтированным в rw, и вернуть ro — даже если внутри ошибка."""
    return f"{{ {ROOT_RW}; }} >/dev/null 2>&1; {{ {script}; }}; rc=$?; {{ {ROOT_RO}; }} >/dev/null 2>&1; exit $rc"


def initd_write():
    """sh: положить свой сценарий автозапуска в init.d (чужие файлы и свой свежий не трогаем)."""
    return (f"grep -q '{HOOK_MARK}' {INITD_HOOK} 2>/dev/null || "
            f"{{ printf '%s' {shlex.quote(initd_hook_script())} > {INITD_HOOK} && chmod 755 {INITD_HOOK}; }} || exit 4")


def initd_writable():
    """Пробная запись только при установке, никогда при чтении статуса."""
    template = shlex.quote(f"{INITD_DIR}/.webpanel_w.XXXXXX")
    return f'(umask 077; probe=$(mktemp {template} 2>/dev/null) && rm -f "$probe")'


def initd_available():
    """Read-only prerequisite, not proof that an install or boot will succeed.

    BusyBox test -w alone can report a read-only mount writable for root. Inspect
    the most specific mount as well, including a bind mount covering init.d.
    Unknown/missing mount metadata must fail closed.
    """
    directory = shlex.quote(INITD_DIR)
    mounts = shlex.quote(BOOT_MOUNTS)
    awk = r'''{ m=$2; gsub(/\\040/, " ", m);
        if ((m=="/" || p==m || index(p,m "/")==1) && length(m)>=best) {
            best=length(m); options=$4
        }
    } END { exit !(best && index("," options ",", ",rw,")) }'''
    return (f"{{ [ -d {directory} ] && [ -w {directory} ] && "
            f"awk -v p={directory} {shlex.quote(awk)} {mounts} 2>/dev/null; }}")


def boot_route_script(require_caller=False):
    """Select a candidate using reads; installation still verifies writes."""
    keeper, caller = shlex.quote(ROOTKEEPER), shlex.quote(ROOTKEEPER_CALLER)
    called = f"sed '/^[[:space:]]*#/d' {caller} 2>/dev/null | grep -F {keeper} >/dev/null"
    rootkeeper = called if require_caller else f"[ -f {keeper} ] || {{ {called}; }}"
    return (f"if {rootkeeper}; then echo rootkeeper; "
            f"elif grep -q {shlex.quote(HOOK_MARK)} {shlex.quote(INITD_HOOK)} 2>/dev/null; then echo initd; "
            f"elif {initd_available()}; then echo initd; else exit 3; fi")


def hook_active():
    """sh: хук подключён — строка в rootkeeper.sh или свой скрипт в init.d."""
    return f"{{ grep -q '{HOOK_MARK}' {ROOTKEEPER} 2>/dev/null || grep -q '{HOOK_MARK}' {INITD_HOOK} 2>/dev/null; }}"


def hook_drop():
    """sh: снять хук — строку из rootkeeper.sh (и созданный нами файл), свой скрипт из init.d.
    init.d на корне только для чтения — файл убираем тем же коротким перемонтированием, как положили."""
    return (f"[ -f {ROOTKEEPER} ] && sed -i '/{HOOK_MARK}/d' {ROOTKEEPER}; {ROOTKEEPER_DROP_CREATED}; "
            f"grep -q '{HOOK_MARK}' {INITD_HOOK} 2>/dev/null && "
            f"{{ rm -f {INITD_HOOK} 2>/dev/null || {with_root_rw(f'rm -f {INITD_HOOK} 2>/dev/null')}; true; }}")


# Сегменты 7-сегментного индикатора: A — верх, B — верх-право, C — низ-право,
# D — низ, E — низ-лево, F — верх-лево, G — середина, H — точка.
SEGMENTS = {
    "0": "ABCDEF", "1": "BC", "2": "ABDEG", "3": "ABCDG", "4": "BCFG",
    "5": "ACDFG", "6": "ACDEFG", "7": "ABC", "8": "ABCDEFG", "9": "ABCDFG",
    "A": "ABCEFG", "B": "CDEFG", "C": "ADEF", "D": "BCDEG", "E": "ADEFG",
    "F": "AEFG", "G": "ACDEF", "H": "BCEFG", "I": "EF", "J": "BCDE",
    "L": "DEF", "N": "CEG", "O": "CDEG", "P": "ABEFG", "R": "EG",
    "S": "ACDFG", "T": "DEFG", "U": "BCDEF", "Y": "BCDFG", "-": "G",
    "_": "D", " ": "", "°": "ABFG",
}



RING_LEDS = [f"plum:{c}:ring{i}" for i in (1, 2, 3) for c in ("red", "green", "blue")]
# Возвращает светодиоды кольца под управление /dev/fb0 (если что-то сняло триггер записью в brightness).
RING_TRIGGER_CMD = ("for n in " + " ".join(RING_LEDS) + "; do grep -q '\\[led_ring_fb\\]' " + LEDS
                    + "/$n/trigger || echo led_ring_fb > " + LEDS + "/$n/trigger; done")


def ring_fb_cmd(pixels):
    """Кадр кольца во fb0: 3 пикселя по 3 байта в порядке B,G,R (rgba 8/16,8/8,8/0)."""
    data = "".join(f"\\x{b:02x}\\x{g:02x}\\x{r:02x}" for r, g, b in pixels)
    return f'echo -e -n "{data}" > /dev/fb0'


class AdbError(Exception):
    pass


# Состояние Bluetooth спрашиваем у bluetoothd по D-Bus одним запросом: каждый запуск bluetoothctl оставляет в
# bluetoothd прошивки ~4 КБ навсегда (он запущен с -d), и частый опрос раздувал его на десятки мегабайт.
BLUEZ_DUMP = "dbus-send --system --print-reply --dest=org.bluez / org.freedesktop.DBus.ObjectManager.GetManagedObjects 2>/dev/null"


def parse_bluez(text):
    """Вывод dbus-send GetManagedObjects → {путь объекта: {интерфейс: {свойство: значение}}} (только простые значения)."""
    objs, path, iface, prop = {}, None, None, None
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        m = re.match(r'object path "([^"]*)"$', line)
        if m and (i + 1 < len(lines) and lines[i + 1] == "array ["):
            path, iface = m.group(1), None
            objs.setdefault(path, {})
            continue
        m = re.match(r'string "(.*)"$', line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if m and path is not None:
            if nxt == "array [" and m.group(1).startswith("org."):
                iface = m.group(1)
                objs[path].setdefault(iface, {})
            elif nxt.startswith("variant") and iface:
                prop = m.group(1)
                v = re.match(r'variant\s+(string|object path|boolean|byte|u?int\d+|double)\s+(.*)$', nxt)
                if v:
                    kind, val = v.groups()
                    if kind in ("string", "object path"):
                        val = val[1:-1] if val.startswith('"') and val.endswith('"') else val
                    elif kind == "boolean":
                        val = val == "true"
                    elif kind == "double":
                        val = float(val)
                    else:
                        val = int(val)
                    objs[path][iface][prop] = val
    return objs


def write_unix(path, text):
    r"""Файл для колонки: всегда UTF-8 и \n. write_text на Windows пишет \r\n, и sh на колонке такие скрипты не выполняет."""
    Path(path).write_bytes(text.encode("utf-8"))


ZSTD_HELP = "для заставки и анимаций часов нужен Python 3.14+ (в нём есть zstd) или программа zstd"


def zstd_decompress(data):
    try:
        from compression import zstd
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        return subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise AdbError(ZSTD_HELP) from None


def zstd_compress(data):
    # Как в файлах Яндекса: потоковый кадр без размера содержимого и без контрольной суммы.
    try:
        from compression import zstd
        c = zstd.ZstdCompressor(level=19)
        return c.compress(data) + c.flush()
    except ImportError:
        pass
    try:
        return subprocess.run(["zstd", "-19", "-c", "--no-content-size", "--no-check"],
                              input=data, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise AdbError(ZSTD_HELP) from None



# Анимация кольца (.led) — текст: «RRGGBB RRGGBB RRGGBB мс» на кадр (3 точки у Лайт 2, 6 у Мини 2) и директивы
# loop, smooth N, gradient …, rotate …, gammacorrection, # комментарий. Простые кадры редактор показывает
# визуально, остальное — только текстом.
LED_LINE = re.compile(r"(loop|gammacorrection|smooth\s+\d{1,6}|gradient(\s+[0-9a-fA-F]{1,6})+|"
                      r"(rotate\s+)?([0-9a-fA-F]{6}\s+){1,32}\d{1,6}|#[^\x00-\x1f]*)?")


def check_led(text):
    if not isinstance(text, str) or len(text) > 64 * 1024:
        raise AdbError("анимация кольца: нужен текст до 64 КБ")
    text = text.replace("\r\n", "\n")
    for n, line in enumerate(text.split("\n"), 1):
        if not LED_LINE.fullmatch(line.strip()):
            raise AdbError(f"анимация кольца, строка {n}: непонятно «{line.strip()[:40]}»")
    if not any(re.fullmatch(r"(rotate\s+)?([0-9a-fA-F]{6}\s+)+\d+|gradient.*", l.strip()) for l in text.split("\n")):
        raise AdbError("в анимации кольца нет ни одного кадра")
    return text if text.endswith("\n") else text + "\n"


def parse_led(text):
    """Кадры для предпросмотра; simple — в файле только кадры и loop, его можно править визуально."""
    frames, loop, simple = [], False, True
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts == ["loop"]:
            loop = True
            continue
        try:
            if len(parts) >= 2 and all(len(h) == 6 for h in parts[:-1]):
                frames.append({"c": [[int(h[i:i + 2], 16) for i in (0, 2, 4)] for h in parts[:-1]], "ms": int(parts[-1])})
                continue
        except ValueError:
            pass
        simple = False
    return {"loop": loop, "frames": frames, "text": text, "simple": simple}


def decode_clock_pattern(name, data):
    """Кадры анимации часов: [{"flag", "v": [36 яркостей], "ms"}].

    Новые прошивки: бинарный .binscled(.zst) из кадров по 39 байт; старые — текстовый .scled
    («loop» в первой строке, далее «8 | 8 | 8 | 8 | 4 | мс»).
    """
    if name.endswith(".zst"):
        data = zstd_decompress(data)
    frames = []
    if ".binscled" in name:
        for i in range(0, len(data) - CLOCK_FRAME + 1, CLOCK_FRAME):
            b = data[i:i + CLOCK_FRAME]
            frames.append({"flag": b[0], "v": list(b[1:-2]), "ms": int.from_bytes(b[-2:], "big")})
        return frames
    loop = False
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line == "loop":
            loop = True
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        v = [int(x) for p in parts[:5] for x in p.split()]
        if len(v) == len(CLOCK_LEDS):
            frames.append({"flag": 0, "v": v, "ms": int(parts[5])})
    if loop and frames:
        frames[0]["flag"] = 1
    return frames


def encode_clock_pattern(frames):
    out = bytearray()
    for f in frames:
        v = [max(0, min(255, int(x))) for x in f["v"]]
        if len(v) != len(CLOCK_LEDS):
            raise AdbError(f"в кадре должно быть {len(CLOCK_LEDS)} значений")
        ms = max(10, min(65535, int(f.get("ms", 100))))
        out += bytes([1 if f.get("flag") else 0, *v]) + ms.to_bytes(2, "big")
    if not out:
        raise AdbError("нет ни одного кадра")
    return bytes(out)


def clock_player_script(frames):
    """sh-скрипт, рисующий кадры напрямую в sysfs: clock_led_util в новой прошивке падает.

    Флаг 1 у кадра — начало цикла (так помечены зацикленные анимации Яндекса). По завершении
    или по SIGTERM возвращает дисплею то, что было на нём до запуска.
    """
    if not frames:
        raise AdbError("в анимации нет кадров")
    snap = "/tmp/webpanel_clock.snap"
    names = " ".join(CLOCK_LEDS)
    lines = [
        "#!/bin/sh",
        f"L={clock_path()}",
        f"for n in {names}; do echo \"$n $(cat $L$n/brightness)\"; done > {snap}",
        f"restore() {{ while read n v; do echo $v > $L$n/brightness; done < {snap}; rm -f {snap}; exit 0; }}",
        "trap restore TERM INT",
    ]

    blank = [None] * len(CLOCK_LEDS)

    def frame_fn(fname, frame, prev):
        # Пишем только изменившиеся сегменты: 36 echo на кадр в busybox sh заметно тормозят.
        body = [f"echo {val} > ${{L}}{n}/brightness" for n, val, old in zip(CLOCK_LEDS, frame["v"], prev)
                if old is None or val != old]
        body.append(f"usleep {frame['ms'] * 1000}")
        return f"{fname}() {{\n" + "\n".join(body) + "\n}"

    prev = blank
    for i, f in enumerate(frames):
        lines.append(frame_fn(f"f{i}", f, prev))
        prev = f["v"]
    lines += [f"f{i}" for i in range(len(frames))]
    loop_start = next((i for i, f in enumerate(frames) if f["flag"]), None)
    if loop_start is not None:
        # На входе в цикл перед кадром стоит последний кадр, а не loop_start - 1, поэтому рисуем его целиком.
        lines.append(frame_fn("fl", frames[loop_start], blank))
        lines.append("while :; do fl; " + "".join(f"f{i}; " for i in range(loop_start + 1, len(frames))) + "done")
    else:
        lines.append("sleep 1")
    lines.append("restore")
    return "\n".join(lines) + "\n"


# Признаки того, что пропала сама связь с колонкой, а не упала команда.
ADB_LOST = ("error: closed", "device offline", "not found", "no devices", "unauthorized", "connection reset",
            "cannot connect", "failed to connect", "protocol fault", "connection refused")


class Adb:
    def __init__(self, adb_path, serial):
        self.transfers = 0      # идёт push/pull: старый adbd в это время медлит с ответами на shell
        self._transfers_lock = threading.Lock()
        self.adb = adb_path
        self.serial = serial
        self.lock = threading.Lock()
        self.conn = None  # Connection; сообщаем ему, когда связь пропала посреди команды
        # Защищённый канал (yapanel-agent): если настроен и отвечает — команды и файлы идут через него,
        # иначе как раньше через adb. Через adb всегда: установка и обновление самого агента.
        self.agent = None
        self.agent_ok = False         # последний вызов через агента удался
        self._agent_retry = 0.0       # агент не ответил — до этого времени идём через adb
        self._local = threading.local()

    @contextlib.contextmanager
    def only_adb(self):
        """Команды этого потока — только через adb (агент перезапускается или удаляется)."""
        prev = getattr(self._local, "adb", False)
        self._local.adb = True
        try:
            yield
        finally:
            self._local.adb = prev

    def _agent(self):
        if self.agent and not getattr(self._local, "adb", False) and time.time() >= self._agent_retry:
            return self.agent
        return None

    def _agent_down(self, err):
        if self.agent_ok:
            JOURNAL.add(f"агент: {err} — пока через adb", "warn")
        self.agent_ok = False
        self._agent_retry = time.time() + 30

    @contextlib.contextmanager
    def _transfer(self):
        with self._transfers_lock:
            self.transfers += 1
        try:
            yield
        finally:
            with self._transfers_lock:
                self.transfers -= 1

    def _base(self):
        return [self.adb] + (["-s", self.serial] if self.serial else [])

    # Старый adbd (Лайт 2) не знает shell_v2: adb shell всегда завершается с 0, а код команды теряется.
    # Поэтому команда идёт в подоболочке, а её код печатается меткой в конце вывода (exit внутри тоже ловится).
    RC_MARK = "@@webpanel-rc="

    def shell(self, cmd, timeout=15, check=True):
        a = self._agent()
        if a:
            try:
                out, rc = a.exec(cmd, timeout)
                self.agent_ok = True
                return out, "", rc        # stdout и stderr вместе — как у adb shell через pty
            except TimeoutError:
                raise AdbError(f"Колонка не ответила за {timeout} с") from None
            except agentlink.AgentError as e:
                self._agent_down(e)
        try:
            p = subprocess.run(self._base() + ["shell", f"(\n{cmd}\n); printf '\\n{self.RC_MARK}%d\\n' $?"],
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # Чаще всего это выключенная колонка при «зависшем» соединении adb — пусть Connection проверит.
            if self.conn:
                self.conn.wake.set()
            raise AdbError(f"Колонка не ответила за {timeout} с")
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        rc = p.returncode
        m = re.search(r"\r*\n" + re.escape(self.RC_MARK) + r"(\d+)\r*\n?\Z", out)
        if m:
            out, rc = out[:m.start()], rc or int(m.group(1))
        if p.returncode != 0 and not out and any(s in err.lower() for s in ADB_LOST):
            # Переподключением занимается Connection; пользователю — без сырого текста adb.
            if self.conn:
                self.conn.lost(err.strip())
            raise AdbError("Связь с колонкой потеряна")
        if check and rc != 0 and not out and err:
            raise AdbError(err.strip())
        # псевдотерминал колонки даёт \r\n, а adb.exe на Windows бывает дописывает ещё \r — \r\r\n
        crlf = lambda s: re.sub(r"\r+\n", "\n", s)
        return crlf(out), crlf(err), rc

    def pull(self, remote, local, timeout=300):
        a = self._agent()
        if a:
            try:
                data = a.read_file(remote, timeout)
            except (agentlink.AgentError, TimeoutError) as e:
                self._agent_down(e)
            except OSError as e:          # нет файла и т. п. — ответ агента, а не обрыв
                raise AdbError(str(e)) from None
            else:
                self.agent_ok = True
                Path(local).write_bytes(data)
                return
        # exec-out этот adbd не поддерживает ("error: closed"), а shell идёт через pty
        # и портит бинарные данные, поэтому файлы забираем только через pull.
        with self._transfer():
            p = subprocess.run(self._base() + ["pull", "-a", remote, str(local)], capture_output=True, timeout=timeout)
        if p.returncode != 0:
            raise AdbError(adb_usage_error((p.stderr or p.stdout).decode("utf-8", "replace").strip(), "pull -a"))

    def read_bytes(self, remote, timeout=120):
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "f"
            self.pull(remote, dst, timeout)
            return dst.read_bytes()

    def ls(self, remote):
        """Листинг через sync-протокол: [(mode, size, mtime, name)], имена с пробелами не ломаются."""
        a = self._agent()
        if a:
            try:
                res = a.ls(remote)
                self.agent_ok = True
                return res
            except (agentlink.AgentError, TimeoutError) as e:
                self._agent_down(e)
            except OSError as e:
                raise AdbError(str(e)) from None
        p = subprocess.run(self._base() + ["ls", remote], capture_output=True, timeout=20)
        out = p.stdout.decode("utf-8", "replace")
        if p.returncode != 0:
            raise AdbError((p.stderr.decode("utf-8", "replace") or out).strip())
        res = []
        # adb.exe на Windows заканчивает строки \r\n: без rstrip папка открывалась бы как «имя\r».
        # splitlines() не подходит — режет и по \x0c, \x1c и прочим, а они бывают в именах файлов.
        for line in out.split("\n"):
            parts = line.rstrip("\r").split(" ", 3)
            if len(parts) == 4:
                res.append((int(parts[0], 16), int(parts[1], 16), int(parts[2], 16), parts[3]))
        return res

    def push(self, local, remote, timeout=120):
        a = self._agent()
        if a:
            try:
                a.write_file(remote, Path(local).read_bytes(), os.stat(local).st_mode & 0o777, timeout)
                self.agent_ok = True
                return
            except (agentlink.AgentError, TimeoutError) as e:
                self._agent_down(e)
            except OSError as e:
                raise AdbError(str(e)) from None
        with self._transfer():
            p = subprocess.run(self._base() + ["push", str(local), remote], capture_output=True, timeout=timeout)
        if p.returncode != 0:
            raise AdbError(adb_usage_error(p.stderr.decode("utf-8", "replace").strip(), "push"))

    def popen(self, cmd, pty=False, text=False, cols=80, rows=24):
        """Долгая команда на колонке, похожая на subprocess.Popen (stdout, stdin.write, kill, wait).
        pty — в псевдотерминале (консоль). Через adb pty есть всегда: этот adbd иначе не умеет."""
        a = self._agent()
        if a:
            try:
                proc = a.popen(cmd, pty=pty, cols=cols, rows=rows, text=text)
                self.agent_ok = True
                return proc
            except agentlink.AgentError as e:
                self._agent_down(e)
        return subprocess.Popen(self._base() + ["shell", cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0 if not text else -1, text=text)

    def devices(self):
        p = subprocess.run([self.adb, "devices", "-l"], capture_output=True, timeout=10)
        return p.stdout.decode("utf-8", "replace")

    def forward(self, remote_port):
        """Проброс порта колонки на свободный локальный (adb forward tcp:0), возвращает локальный порт."""
        p = subprocess.run(self._base() + ["forward", "tcp:0", f"tcp:{remote_port}"], capture_output=True, timeout=10)
        out = p.stdout.decode().strip()
        if p.returncode != 0 or not out.isdigit():
            raise AdbError("adb forward: " + adb_usage_error(p.stderr.decode("utf-8", "replace").strip() or out, "forward tcp:0"))
        return int(out)


class Connection:
    """Следит за adb-подключением и сам переподключается. Состояние и лог — для экрана подключения.

    online — всё работает; waiting — колонка не отвечает по сети (выключена или вне сети);
    connecting — идёт adb connect; adb_error — колонка в сети, но adb не подключается (повтор с паузой).
    """
    RETRY = (3, 5, 10, 15, 30)

    def __init__(self, adb):
        self.adb = adb
        adb.conn = self
        self.state, self.since = "connecting", time.time()
        self.attempts = 0
        self.retry_at = 0.0
        self.log = collections.deque(maxlen=80)
        self.wake = threading.Event()

    @property
    def online(self):
        return self.state == "online"

    def note(self, msg, level="info"):
        self.log.append({"t": time.time(), "level": level, "msg": msg})
        JOURNAL.add("связь: " + msg, level)

    def _set(self, state):
        if state != self.state:
            self.state, self.since = state, time.time()

    def lost(self, why):
        if self.online:
            self.note(f"Связь потеряна: {why}", "warn")
            self._set("connecting")
            self.wake.set()

    def retry_now(self):
        self.retry_at = 0
        self.wake.set()
        return self.info()

    def info(self):
        return {"state": self.state, "serial": self.adb.serial, "since": self.since, "attempts": self.attempts,
                "retry_in": max(0, round(self.retry_at - time.time())) if self.state == "adb_error" else None,
                "log": list(self.log)}

    def _adb_state(self):
        try:
            p = subprocess.run(self.adb._base() + ["get-state"], capture_output=True, timeout=8)
        except subprocess.TimeoutExpired:
            return "", "adb get-state: таймаут"
        return p.stdout.decode().strip(), p.stderr.decode().strip()

    def _alive(self):
        """adb get-state верит в старое TCP-соединение: после резкого выключения колонки он ещё долго
        отвечает «device». Поэтому проверяем по-настоящему — короткой командой на колонке."""
        if self.adb.transfers:      # пока идёт передача файла, медленный ответ — не обрыв
            return True
        try:
            p = subprocess.run(self.adb._base() + ["shell", "echo ok"], capture_output=True, timeout=4)
            return p.stdout.strip() == b"ok"
        except subprocess.TimeoutExpired:
            return False

    def _drop(self):
        if self.adb.serial and ":" in self.adb.serial:
            subprocess.run([self.adb.adb, "disconnect", self.adb.serial], capture_output=True, timeout=10)

    def _sleep(self, seconds):
        self.wake.wait(seconds)
        self.wake.clear()

    def _agent_alive(self):
        a = self.adb._agent()
        if not a:
            return False
        try:
            a.ping()
            self.adb.agent_ok = True
            return True
        except (agentlink.AgentError, TimeoutError) as e:
            self.adb._agent_down(e)
            return False

    def run(self):
        network = bool(self.adb.serial and ":" in self.adb.serial)
        while True:
            if self._agent_alive():
                if not self.online:
                    self.note(f"Подключено через агента: {self.adb.serial}")
                self.attempts = 0
                self._set("online")
                self._sleep(3)
                continue
            state, err = self._adb_state()
            # голый TCP-коннект к 5555 оставлял на adbd сокеты в CLOSE_WAIT и забивал его;
            # живость проверяем через сам adb: get-state и короткую shell-команду
            if state == "device" and not self._alive():
                self.note("adb числит колонку подключённой, но она не отвечает — сбрасываем соединение", "warn")
                self._drop()
                state = ""
            if state == "device":
                if not self.online:
                    self.note(f"Подключено: {self.adb.serial or 'устройство по умолчанию'}")
                self.attempts = 0
                self._set("online")
                self._sleep(3)
                continue
            if not network:
                if self.state != "waiting":
                    self.note(f"Устройство не найдено: {err or state}", "warn")
                self.attempts = 0
                self._set("waiting")
                self._sleep(3)
                continue
            self._set("connecting")
            self.note(f"adb connect {self.adb.serial}")
            subprocess.run([self.adb.adb, "disconnect", self.adb.serial], capture_output=True, timeout=10)
            try:
                p = subprocess.run([self.adb.adb, "connect", self.adb.serial], capture_output=True, timeout=15)
                out = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
            except subprocess.TimeoutExpired:
                out = "таймаут adb connect"
            time.sleep(1)
            state, err = self._adb_state()
            if state == "device":
                continue  # отметится «Подключено» на следующем круге
            delay = self.RETRY[min(self.attempts, len(self.RETRY) - 1)]
            self.attempts += 1
            self.note(f"Не подключилось: {out or err or state}", "warn")
            self.retry_at = time.time() + delay
            self._set("adb_error")
            self._sleep(delay)


DISPLAY_SCRIPT = "/tmp/webpanel_display.sh"
DISPLAY_MODE = "/tmp/webpanel_display.mode"
DISPLAY_SNAP = "/tmp/webpanel_display.snap"        # что было на дисплее до закреплённого режима
DISPLAY_ONCE = "/tmp/webpanel_display.once"        # … и до «показать один раз»


def clock_restore_sh():
    """sh-функции для колонки: snap_save ФАЙЛ — запомнить дисплей и время; clock_restore ФАЙЛ — вернуть.

    Просто записать снимок мало: если прошла минута, на нём уже неверное время, а Quasar перерисует часы
    только при следующей смене минуты. Поэтому распознаём в снимке ЧЧ:ММ по сегментам, прибавляем
    прошедшие минуты и рисуем текущее время с той же яркостью. Часовой пояс для этого не нужен.
    Если в снимке не часы (свой текст, анимация), возвращаем его как есть.
    """
    digits = " ".join(f"{segs}) d={ch};;" for ch, segs in SEGMENTS.items() if ch.isdigit())
    draw = " ".join(f"{ch}) S={segs};;" for ch, segs in SEGMENTS.items() if ch.isdigit())
    return "\n".join([
        f"snap_save() {{ {{ echo \"t $(date +%s)\"; for n in {' '.join(CLOCK_LEDS)}; do "
        f"echo \"$n $(cat {clock_path()}$n/brightness)\"; done; }} > \"$1\"; }}",
        "clock_restore() {",
        "  [ -f \"$1\" ] || return 0",
        f"  while read n v; do [ \"$n\" = t ] || echo $v > {clock_path()}$n/brightness; done < \"$1\"",
        "  t0=$(sed -n 's/^t //p' \"$1\"); [ -n \"$t0\" ] || return 0",
        "  el=$(( $(date +%s) / 60 - t0 / 60 )); [ $el -gt 0 ] || return 0",
        "  hm=''; B=0",
        "  for p in 0 1 2 3; do",
        "    s=''",
        "    for g in A B C D E F G; do",
        "      v=$(sed -n \"s/^$g$p //p\" \"$1\")",
        "      [ \"${v:-0}\" -gt 0 ] && { s=\"$s$g\"; [ $v -gt $B ] && B=$v; }",
        "    done",
        f"    case \"$s\" in {digits} '') d=x;; *) return 0;; esac",
        "    hm=\"$hm$d\"",
        "  done",
        "  h=${hm%??}; m=${hm#??}; lead=0",
        "  case \"$h\" in x?) lead=1; h=${h#x};; *x*) return 0;; esac",
        "  case \"$m\" in *x*) return 0;; esac",
        "  h=${h#0}; m=${m#0}",
        "  tot=$(( (${h:-0} * 60 + ${m:-0} + el) % 1440 ))",
        "  new=$(printf '%02d%02d' $((tot / 60)) $((tot % 60)))",
        "  [ $lead = 1 ] && [ $((tot / 60)) -lt 10 ] && new=\"x${new#0}\"",
        "  p=0",
        "  for c in $(echo \"$new\" | sed 's/./& /g'); do",
        f"    case \"$c\" in {draw} *) S='';; esac",
        f"    for g in A B C D E F G; do case \"$S\" in *$g*) v=$B;; *) v=0;; esac; echo $v > {clock_path()}$g$p/brightness; done",
        "    p=$((p + 1))",
        "  done",
        "}",
    ])
TIMER_SOUND = "/tmp/webpanel_timer.wav"   # штатный звук таймера Quasar, перекодированный для aplay


def display_script(mode, frame=None, seconds=0, bright=160):
    """Скрипт «закреплённого» дисплея: раз в полсекунды перерисовывает своё содержимое, поэтому Quasar,
    который обновляет часы раз в минуту, его не затирает. По SIGTERM (и в конце таймера) возвращает
    то, что было на дисплее до запуска.

    frame — свой кадр (36 значений); date — ДД.ММ; temp — температура процессора °C; timer — отсчёт мм:сс.
    """
    lines = [
        "#!/bin/sh",
        f"L={clock_path()}",
        f"B={bright}",
        clock_restore_sh(),
        # если перед этим был «один раз», в его снимке — настоящие часы; иначе снимаем сейчас
        f"if [ -f {DISPLAY_ONCE} ]; then mv {DISPLAY_ONCE} {DISPLAY_SNAP}; else snap_save {DISPLAY_SNAP}; fi",
        f"restore() {{ clock_restore {DISPLAY_SNAP}; rm -f {DISPLAY_SNAP} {DISPLAY_MODE}; exit 0; }}",
        "trap restore TERM INT",
    ]
    if mode == "frame":
        lines.append(f"echo frame > {DISPLAY_MODE}")
        lines.append("while :; do")
        lines += [f"  echo {v} > ${{L}}{n}/brightness" for n, v in zip(CLOCK_LEDS, frame)]
        lines += ["  usleep 500000", "done"]
        return "\n".join(lines) + "\n"
    cases = " ".join(f"{ch}) S={segs};;" for ch, segs in SEGMENTS.items() if ch.isdigit() or ch == "-")
    lines += [
        f"segs() {{ case \"$1\" in {cases} *) S=;; esac; }}",
        # draw ТЕКСТ ВЕРХ НИЗ ТОЧКА ГРАДУС: 4 знака (x — пусто), яркость верхней и нижней точек двоеточия,
        # позиция цифры с точкой H (она чуть выше центра цифры) и яркость градуса
        "draw() {",
        "  i=0",
        "  for c in $(echo \"$1\" | sed 's/./& /g'); do",
        "    segs \"$c\"",
        "    for s in A B C D E F G; do case \"$S\" in *$s*) v=$B;; *) v=0;; esac; echo $v > $L$s$i/brightness; done",
        "    [ \"$4\" = \"$i\" ] && echo $B > ${L}H$i/brightness || echo 0 > ${L}H$i/brightness",
        "    i=$((i + 1))",
        "  done",
        "  echo $2 > ${L}DH/brightness; echo $3 > ${L}DL/brightness; echo 0 > ${L}DM/brightness; echo $5 > ${L}DEGREE/brightness",
        "}",
    ]
    if mode == "date":
        lines += [f"echo date > {DISPLAY_MODE}",
                  "while :; do draw \"$(date +%d%m)\" 0 $B '' 0; usleep 500000; done"]   # 24.10: нижняя точка двоеточия
    elif mode == "temp":
        lines += [f"echo temp > {DISPLAY_MODE}",
                  "while :; do",
                  "  t=$(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))",
                  "  draw \"$(printf '%4d' $t | tr ' ' x)\" 0 0 '' $B",
                  "  usleep 500000",
                  "done"]
    elif mode == "timer":
        lines += [f"END=$(( $(date +%s) + {int(seconds)} ))",
                  f"echo \"timer $END\" > {DISPLAY_MODE}",
                  "while :; do",
                  "  r=$(( END - $(date +%s) ))",
                  "  [ $r -le 0 ] && break",
                  "  m=$(( r / 60 )); [ $m -gt 99 ] && m=99",
                  "  draw \"$(printf '%02d%02d' $m $(( r % 60 )))\" $B $B '' 0",
                  "  usleep 500000",
                  "done",
                  # время вышло — звонок таймера (заранее перекодирован в WAV), мигаем нулями ~10 с и возвращаем часы
                  f"[ -f {TIMER_SOUND} ] && aplay -q {TIMER_SOUND} >/dev/null 2>&1 &",
                  "for k in 1 2 3 4 5 6 7 8 9 10; do draw 0000 $B $B '' 0; usleep 500000; draw xxxx 0 0 '' 0; usleep 500000; done",
                  "restore"]
    return "\n".join(lines) + "\n"


class Metrics:
    """История состояния колонки для вкладки «Графики»: раз в 10 с, хранится сутки в .metrics.json на компьютере.

    Колонка ничего не хранит — опрашиваем её одним коротким adb shell, как и «Обзор».
    События: перезагрузка колонки (аптайм уменьшился), пропала/вернулась связь, сработал будильник.
    """
    INTERVAL = 10
    KEEP = 24 * 3600
    FIELDS = ("cpu", "temp", "mem", "rssi", "disk", "load", "play")
    SCRIPT = (
        "head -n 1 /proc/stat; cut -d' ' -f1 /proc/uptime; cut -d' ' -f1 /proc/loadavg; "
        "awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t-a, t}' /proc/meminfo; "
        "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo -; "
        "wpa_cli -i wlan0 signal_poll 2>/dev/null | sed -n 's/^RSSI=//p' | grep . || echo -; "
        "df -k /data | awk 'NR==2{print $3, $2}'; "
        "grep -l 'state: RUNNING' /proc/asound/card*/pcm*p/sub*/status 2>/dev/null | wc -l; "
        f"cat {ALARM_STATE} 2>/dev/null | cut -d'|' -f1-2 | grep . || echo -")

    def __init__(self, adb, conn):
        self.adb, self.conn = adb, conn
        self.lock = threading.Lock()
        self.t, self.v, self.events = [], {f: [] for f in self.FIELDS}, []
        self.prev_cpu = self.prev_up = None
        self.was_online = None
        self.ringing = None
        self.saved = 0
        self.totals = {}           # объём памяти и /data, МБ — верх шкалы на графиках
        try:
            d = json.loads(METRICS_FILE.read_text())
            if d.get("interval") == self.INTERVAL:
                self.t, self.events = d["t"], d.get("events", [])
                # новая метрика в старом файле — пустые значения, а не потеря истории
                self.v = {f: d.get(f) or [None] * len(self.t) for f in self.FIELDS}
                self.totals = d.get("totals", {})
                self._trim()
        except (OSError, ValueError):
            pass

    def _trim(self):
        cut = time.time() - self.KEEP
        i = next((k for k, x in enumerate(self.t) if x >= cut), len(self.t))
        if i:
            self.t = self.t[i:]
            for f in self.FIELDS:
                self.v[f] = self.v[f][i:]
        self.events = [e for e in self.events if e["t"] >= cut][-300:]

    def event(self, kind, text):
        with self.lock:
            self.events.append({"t": round(time.time()), "type": kind, "text": text})

    def run(self):
        while True:
            start = time.time()
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — сборщик не должен падать из-за одного неудачного опроса
                pass
            if time.time() - self.saved > 300:
                self._save()
            time.sleep(max(1.0, self.INTERVAL - (time.time() - start)))

    def _tick(self):
        online = self.conn.online
        if online != self.was_online and self.was_online is not None:
            self.event("online" if online else "offline", "Связь с колонкой восстановлена" if online else "Связь с колонкой пропала")
        self.was_online = online
        if not online:
            self.prev_cpu = None
            return
        out, _, rc = self.adb.shell(self.SCRIPT, check=False, timeout=8)
        lines = strip_ansi(out).splitlines()
        if rc or len(lines) < 9:
            return
        num = lambda x: float(x) if re.fullmatch(r"-?\d+(\.\d+)?", x.strip()) else None
        cpu = None
        f = [int(x) for x in lines[0].split()[1:8]]
        idle, total = f[3] + f[4], sum(f)
        if self.prev_cpu and total > self.prev_cpu[1]:
            cpu = round(100 * (1 - (idle - self.prev_cpu[0]) / (total - self.prev_cpu[1])), 1)
        self.prev_cpu = (idle, total)
        up = num(lines[1])
        if up is not None and self.prev_up is not None and up + 30 < self.prev_up:
            self.event("reboot", "Колонка перезагрузилась")
        self.prev_up = up
        temp = num(lines[4])
        mem, mem_total = (lines[3].split() + [""])[:2]
        disk, disk_total = (lines[6].split() + [""])[:2]
        mem, disk = num(mem), num(disk)
        if num(mem_total):
            self.totals["mem"] = round(num(mem_total) / 1024)
        if num(disk_total):
            self.totals["disk"] = round(num(disk_total) / 1024)
        al = lines[8].strip()
        ring = al.split("|")[1] if al.startswith("ringing") and "|" in al else None
        if ring and ring != self.ringing:
            self.event("alarm", "Сработал будильник")
        self.ringing = ring
        row = {"cpu": cpu, "temp": round(temp / 1000, 1) if temp is not None else None,
               "mem": round(mem / 1024) if mem is not None else None, "rssi": num(lines[5]),
               "disk": round(disk / 1024, 1) if disk is not None else None, "load": num(lines[2]),
               "play": 1 if (num(lines[7]) or 0) > 0 else 0}
        with self.lock:
            self.t.append(round(time.time()))
            for k in self.FIELDS:
                self.v[k].append(row[k])
            if len(self.t) % 60 == 0:
                self._trim()

    def _save(self):
        with self.lock:
            self._trim()
            d = {"interval": self.INTERVAL, "t": self.t, "events": self.events, "totals": self.totals, **self.v}
            data = json.dumps(d, separators=(",", ":"))
        tmp = METRICS_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(data)
            os.replace(tmp, METRICS_FILE)
            self.saved = time.time()
        except OSError:
            pass

    def get(self, seconds):
        """Ряды за последние seconds секунд, не больше ~720 точек (усреднение по корзинам; play — максимум).
        Разрыв больше трёх интервалов — None, чтобы линия прерывалась там, где колонки не было."""
        seconds = max(60, min(self.KEEP, int(seconds)))
        now = time.time()
        with self.lock:
            cut = now - seconds
            i = next((k for k, x in enumerate(self.t) if x >= cut), len(self.t))
            t = self.t[i:]
            v = {f: self.v[f][i:] for f in self.FIELDS}
            events = [e for e in self.events if e["t"] >= cut]
        step = max(self.INTERVAL, seconds // 720)
        out_t, out = [], {f: [] for f in self.FIELDS}
        b = 0
        last = None
        while b < len(t):
            key = t[b] // step
            e = b
            while e < len(t) and t[e] // step == key:
                e += 1
            if last is not None and t[b] - last > max(3 * self.INTERVAL, 2 * step):
                out_t.append(last + self.INTERVAL)
                for f in self.FIELDS:
                    out[f].append(None)
            out_t.append(key * step + step // 2 if step > self.INTERVAL else t[b])
            for f in self.FIELDS:
                xs = [x for x in v[f][b:e] if x is not None]
                if not xs:
                    out[f].append(None)
                elif f == "play":
                    out[f].append(max(xs))
                else:
                    out[f].append(round(sum(xs) / len(xs), 1))
            last = t[e - 1]
            b = e
        return {"t": out_t, **out, "events": events, "totals": self.totals, "step": step, "from": round(cut), "to": round(now),
                "interval": self.INTERVAL, "keep": self.KEEP}

    def clear(self):
        with self.lock:
            self.t, self.v, self.events = [], {f: [] for f in self.FIELDS}, []
        self._save()
        return {"ok": True}


class LedStream:
    """Живое состояние светодиодов 10 раз в секунду — для визуализатора и 3D-модели.

    На колонке крутится один цикл, который читает сегменты часов и кольцо встроенным read (без запуска
    процессов на каждом шаге) и печатает строку; мы держим adb shell открытым и читаем строки. Замер:
    около +3,5 % загрузки колонки. Поток работает, только пока его спрашивают: через 3 с тишины — гасим.
    """
    RING = [f"{c}:ring{i}" for i in (1, 2, 3) for c in ("red", "green", "blue")]

    def __init__(self, adb):
        self.adb = adb
        self.values, self.at, self.wanted = None, 0.0, 0.0
        self.viz, self.viz_at = None, 0.0
        self.lock = threading.Lock()
        self.running = False

    def get(self):
        self.wanted = time.time()
        with self.lock:
            if not self.running:
                self.running = True
                threading.Thread(target=self._run, daemon=True, name="led-stream").start()
        v = self.values
        res = {"v": None}
        if v and time.time() - self.at <= 1.5:
            r = v[len(CLOCK_LEDS):]
            res = {"v": v[:len(CLOCK_LEDS)], "ring": [r[0:3], r[3:6], r[6:9]], "age": round(time.time() - self.at, 2)}
        # Живой спектр от vizd — тем же потоком, без отдельных запросов на каждые 100 мс.
        # Отдаём его и без свежих светодиодов: после перезапуска Quasar (например, при переходе
        # на Bluetooth-колонку) они на несколько секунд пропадают, а спектр уже есть.
        if self.viz and time.time() - self.viz_at < 1.0:
            res["spectrum"] = {"bands": self.viz.get("bands") or [], "rms": self.viz.get("rms", 0), "t": self.viz.get("t", 0)}
        return res

    PIDFILE = "/tmp/webpanel_leds.pid"

    def _kill_device_loop(self):
        # Цикл на колонке НЕ умирает вместе с adb shell — гасим его явно. PID-файл у циклов один,
        # и после перезапуска панели в нём уже новый PID, поэтому старые ищем по командной строке.
        self.adb.shell(f"for p in $(ps | awk '/webpanel_leds\\.pid/ && !/awk/ {{print $1}}'); do kill $p 2>/dev/null; done; "
                       f"rm -f {self.PIDFILE}; true", check=False)

    def _run(self):
        clock = " ".join(f"{LED_PREFIX}:white:clock{n}" for n in CLOCK_LEDS)
        ring = " ".join(f"plum:{x}" for x in self.RING)
        # Не дольше 60 с (600 × 0,1 с): даже если сервер упадёт, цикл на колонке закончится сам.
        # Спектр читаем чаще светодиодов (25 Гц против 12,5): столбики должны успевать за музыкой,
        # а лишние чтения sysfs не нужны.
        loop = (f"echo $$ > {self.PIDFILE}; cd {LEDS}; k=0; while [ $k -lt 1200 ]; do k=$((k + 1)); o=; "
                f"if [ $((k % 2)) -eq 1 ]; then for n in {clock} {ring}; do read v < $n/brightness; o=\"$o $v\"; done; echo $o; fi; "
                f"[ -s {VIZ_STATUS} ] && {{ read line < {VIZ_STATUS}; echo \"@viz $line\"; }}; usleep 40000; done; "
                f"rm -f {self.PIDFILE}")
        want = len(CLOCK_LEDS) + len(self.RING)
        try:
            self._kill_device_loop()   # хвосты прошлого запуска сервера
            while time.time() - self.wanted <= 3:
                proc = self.adb.popen(loop, text=True)
                stop = threading.Event()

                def guard(proc=proc, stop=stop):
                    # Если процесс на колонке убили снаружи (другая панель, перезагрузка), чтение
                    # из потока агента может не вернуться. Не ждём вечно: гасим proc.
                    while not stop.wait(0.5):
                        if time.time() - self.wanted > 4:
                            proc.kill()
                            return

                threading.Thread(target=guard, daemon=True, name="led-guard").start()
                try:
                    for line in proc.stdout:
                        line = line.strip()
                        if line.startswith("@viz "):
                            try:
                                self.viz, self.viz_at = json.loads(line[5:]), time.time()
                            except ValueError:
                                pass
                            if time.time() - self.wanted > 3:
                                break
                            continue
                        parts = line.split()
                        if len(parts) == want and all(p.isdigit() for p in parts):
                            self.values, self.at = [int(p) for p in parts], time.time()
                        if time.time() - self.wanted > 3:
                            break
                finally:
                    stop.set()
                    proc.kill()
                    proc.wait()
        except OSError:
            pass
        finally:
            self._kill_device_loop()
            self.values = None
            with self.lock:
                self.running = False


# pull -a и forward tcp:0 появились в новых adb; 1.0.31 вместо ответа печатает справку. 1.0.39 — platform-tools 28 (2018).
ADB_MIN = (1, 0, 39)
ADB_OLD = ("adb {got} слишком старый ({path}): панели нужен adb {need} или новее (Android platform-tools 28+).\n"
           "Скачайте свежий: https://developer.android.com/tools/releases/platform-tools — и положите папку platform-tools\n"
           "рядом с server.py, укажите путь в переменной ADB или уберите старый adb из PATH.")


def adb_version(path):
    """(1, 0, 41) из «Android Debug Bridge version 1.0.41»; None — не удалось узнать."""
    try:
        out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"version (\d+)\.(\d+)\.(\d+)", out)
    return tuple(map(int, m.groups())) if m else None


def adb_usage_error(text, what):
    """Старый adb на незнакомую команду отвечает простынёй справки — показываем вместо неё понятную причину."""
    if "Android Debug Bridge version" in text and len(text) > 300:
        return f"adb не понял команду «{what}» — похоже, он слишком старый, нужен platform-tools 28+"
    return text


def find_adb():
    # Телефон (APK) или компьютер без adb: протокол adb на чистом Python (pyadb.py), только по сети
    if os.environ.get("PANEL_PYADB"):
        import pyadb
        return pyadb.install()
    exe = "adb.exe" if os.name == "nt" else "adb"
    cands = [os.environ.get("ADB"), shutil.which("adb"), str(ROOT / "platform-tools" / exe),
             str(Path.home() / "Android/Sdk/platform-tools" / exe)]
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(name):
            cands.append(str(Path(os.environ[name]) / "platform-tools" / exe))
    if sys.platform == "darwin":
        cands.append(str(Path.home() / "Library/Android/sdk/platform-tools/adb"))
    if os.name == "nt":
        cands.append(str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/platform-tools/adb.exe"))
    old = None
    for cand in cands:
        if cand and Path(cand).is_file():
            version = adb_version(cand)
            if version is None or version >= ADB_MIN:
                return cand
            old = old or (cand, version)
    if old:
        raise SystemExit(ADB_OLD.format(got=".".join(map(str, old[1])), path=old[0], need=".".join(map(str, ADB_MIN))))
    raise SystemExit("adb не найден. Установите Android platform-tools (https://developer.android.com/tools/releases/platform-tools)\n"
                     "и добавьте в PATH, положите папку platform-tools рядом с server.py или укажите путь в переменной ADB.")


def normalize_device(dev):
    """«192.168.1.50» → «192.168.1.50:5555»; серийники USB и host:port — как есть."""
    dev = (dev or "").strip()
    if dev and ":" not in dev and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}|[\w-]+\.(local|lan|home)", dev):
        dev += ":5555"
    return dev or None


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# --- LED-панель Станции 2 (88 диодов) через ШТАТНУЮ утилиту led-play ---
# led-play сама знает формат пикселя (BGR+белый канал) и раскладку диодов, поэтому сырой записью
# в /dev/fb0 мы не занимаемся — лишь генерируем .led-кадр/анимацию и отдаём ей. Формат .led: строка =
# 84 токена RRGGBBWW (диоды 0..83) + длительность кадра (hex, мс). Флаг -r зацикливает → кадр
# перерисовывается ~30 раз/с и перебивает штатную анимацию прошивки.
LP = "/usr/bin/led-play"
LED_DEV = "/dev/fb0"
LED_SD = "/tmp/webpanel_led.sd"       # pidfile фоновой отрисовки
LED_FRAME = "/tmp/webpanel.led"       # сгенерированный кадр/анимация на устройстве
LED_GIF = "/tmp/webpanel.gif"         # загруженный пользователем GIF
LED_PANEL_DIR = f"{QUASAR}/ledpatterns/panel"
LED_HOLD_MS = 0x20                    # длительность кадра при удержании (~30 к/с перерисовки)
_LED_LOCK = threading.Lock()          # операции LED сериализуем: панель многопоточная, иначе гонки

# стоп: гасим только свой led-play по pid-файлу start-stop-daemon — чужие (прошивки, других программ) не трогаем
LED_STOP_SH = f"start-stop-daemon -K -q -p {LED_SD} 2>/dev/null; rm -f {LED_SD}; true"

# карта диодов из визуализатора YSgen2: idx -> (ряд, колонка) в сетке 15x13 (колонка = гориз. позиция)
def _led_build_pos():
    pos = {}
    def addrow(row, cols, start):
        for i, c in enumerate(cols):
            pos[start + i] = (row, c)
    odd, even = [1,3,5,7,9,11,13], [0,2,4,6,8,10,12,14]
    addrow(0,odd,0); addrow(1,even,7); addrow(2,odd,15); addrow(3,even,22); addrow(4,odd,30)
    pos[37]=(5,0); pos[38]=(5,2); pos[39]=(5,12); pos[40]=(5,14)
    pos[41]=(6,1); pos[42]=(6,13)
    pos[43]=(7,0); pos[44]=(7,2); pos[45]=(7,12); pos[46]=(7,14)
    addrow(8,odd,47); addrow(9,even,54); addrow(10,odd,62); addrow(11,even,69); addrow(12,odd,77)
    return pos
LED_POS = _led_build_pos()                       # 0..83 -> (row, col)
LED_COL = {i: LED_POS[i][1] for i in LED_POS}     # 0..83 -> колонка (0..14, гориз.)
LED_ROW = {i: LED_POS[i][0] for i in LED_POS}     # 0..83 -> ряд (0..12, сверху вниз)
LED_CR, LED_CC = 6.0, 7.0                         # центр сетки (для радиальных эффектов)

def _led_tok(r, g, b, w=0):
    return "%02x%02x%02x%02x" % (r & 255, g & 255, b & 255, w & 255)

def _led_line(pixels, ms=LED_HOLD_MS):
    # кадр led-play = 88 токенов (0..83 основная панель, 84..87 центр логотипа); недостающие — чёрные
    px = list(pixels)
    px += [(0, 0, 0, 0)] * (88 - len(px))
    return " ".join(_led_tok(*p) for p in px[:88]) + " %x\n" % ms

def _led_validate_tokens(tokens):
    if not isinstance(tokens, list) or not tokens:
        raise AdbError("нужны tokens (список RRGGBBWW)")
    out = []
    for t in tokens[:88]:
        t = str(t)
        if not re.match(r"^[0-9A-Fa-f]{8}$", t):
            raise AdbError("плохой токен (нужно 8 hex-символов RRGGBBWW)")
        out.append(t.lower())
    while len(out) < 88:
        out.append("00000000")
    return out

def _led_hsv(h):
    """h in [0,1) -> (r,g,b), насыщенность и яркость = макс."""
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    q, t = int(255 * (1 - f)), int(255 * f)
    return [(255,t,0),(q,255,0),(0,255,t),(0,q,255),(t,0,255),(255,0,q)][i]

def _led_anim_text(kind, color):
    col = color or [204, 51, 204]
    r, g, b = int(col[0]) & 255, int(col[1]) & 255, int(col[2]) & 255
    lines = []
    if kind == "rainbow":
        n = 60
        for fr in range(n):
            ph = fr / float(n)
            lines.append(_led_line([_led_hsv((ph + LED_COL[i] / 15.0) % 1.0) + (0,) for i in range(84)], 0x20))
    elif kind == "pulse":
        n = 40
        for fr in range(n):
            s = fr / float(n - 1)
            s = 1 - abs(1 - 2 * s)            # 0..1..0
            sm = s * s * (3 - 2 * s)          # smoothstep
            lines.append(_led_line([(int(r*sm), int(g*sm), int(b*sm), 0)] * 84, 0x28))
    elif kind == "chase":
        order = sorted(range(84), key=lambda i: (LED_COL[i], LED_ROW[i]))
        for step in range(len(order)):
            px = [(0, 0, 0, 0)] * 84
            px[order[step]] = (r, g, b, 0)
            lines.append(_led_line(px, 0x24))
    elif kind == "wave":                         # цветная линия бежит по колонкам (с заворотом)
        n = 45
        for fr in range(n):
            pos = fr / float(n) * 15
            px = []
            for i in range(84):
                d = abs(LED_COL[i] - pos); d = min(d, 15 - d)
                k = max(0.0, 1 - d / 2.5)
                px.append((int(r*k), int(g*k), int(b*k), 0))
            lines.append(_led_line(px, 0x20))
    elif kind == "ripple":                       # круги от центра логотипа
        maxd = (LED_CR**2 + LED_CC**2) ** 0.5
        n = 42
        for fr in range(n):
            rad = fr / float(n) * (maxd + 2)
            px = []
            for i in range(84):
                dist = ((LED_ROW[i]-LED_CR)**2 + (LED_COL[i]-LED_CC)**2) ** 0.5
                k = max(0.0, 1 - abs(dist - rad) / 1.8)
                px.append((int(r*k), int(g*k), int(b*k), 0))
            lines.append(_led_line(px, 0x20))
    elif kind == "comet":                        # точка с затухающим хвостом
        order = sorted(range(84), key=lambda i: (LED_COL[i], LED_ROW[i]))
        length, tail = len(order), 7
        for step in range(length):
            px = [(0, 0, 0, 0)] * 84
            for t in range(tail):
                k = max(0.0, 1 - t / float(tail))
                idx = order[(step - t) % length]
                px[idx] = (int(r*k), int(g*k), int(b*k), 0)
            lines.append(_led_line(px, 0x20))
    elif kind == "fire":                         # тёплое пламя снизу вверх с мерцанием
        random.seed(1)
        n = 50
        for fr in range(n):
            px = []
            for i in range(84):
                heat = LED_ROW[i] / 12.0                 # 0 сверху .. 1 снизу
                v = max(0.0, min(1.0, heat * (0.55 + 0.45*random.random()) + 0.08))
                px.append((int(255*v), int(120*v*v), int(8*v*v*v), 0))
            lines.append(_led_line(px, 0x2a))
    elif kind == "stars":                        # случайные мерцания на белом канале
        random.seed(2)
        n = 60
        for fr in range(n):
            px = [(0, 0, 0, 0)] * 84
            for _ in range(7):
                px[random.randint(0, 83)] = (0, 0, 0, random.randint(90, 255))
            lines.append(_led_line(px, 0x30))
    elif kind == "matrix":                        # зелёные капли стекают по колонкам
        random.seed(3)
        cols = sorted(set(LED_COL.values()))
        phase = {c: random.random()*13 for c in cols}
        speed = {c: 0.3 + random.random()*0.6 for c in cols}
        n = 48
        for fr in range(n):
            px = [(0, 0, 0, 0)] * 84
            for i in range(84):
                head = (phase[LED_COL[i]] + fr*speed[LED_COL[i]]) % 16
                d = head - LED_ROW[i]
                if 0 <= d < 4:
                    k = 1 - d/4.0
                    px[i] = (0, int(255*k), int(40*k), 0)
            lines.append(_led_line(px, 0x24))
    elif kind == "gradient":                      # перелив двух цветов по колонкам
        r2, g2, b2 = b, r, g                                  # второй цвет — поворот каналов
        n = 50
        for fr in range(n):
            off = fr / float(n)
            px = []
            for i in range(84):
                t = ((LED_COL[i] / 15.0) + off) % 1.0
                tb = 1 - abs(1 - 2*t)
                px.append((int(r+(r2-r)*tb), int(g+(g2-g)*tb), int(b+(b2-b)*tb), 0))
            lines.append(_led_line(px, 0x24))
    elif kind == "strobe":                         # красно-синие мигалки по половинам
        n = 8
        for fr in range(n):
            left_on = fr % 2 == 0
            px = []
            for i in range(84):
                left = LED_COL[i] < 7
                if left and left_on:
                    px.append((255, 0, 0, 0))
                elif (not left) and (not left_on):
                    px.append((0, 0, 255, 0))
                else:
                    px.append((0, 0, 0, 0))
            lines.append(_led_line(px, 0x60))
    else:
        raise AdbError("неизвестный эффект")
    return "".join(lines)


class Panel:
    def __init__(self, adb):
        self.adb = adb
        self._version = None
        self.ffmpeg = shutil.which("ffmpeg")
        self._ww_info = None       # кэш «пакет установлен»: вкладка опрашивает статус часто
        self._ww_info_at = 0.0
        self._ww_router = None
        self._ww_router_stop = threading.Event()
        self._ww_last_hit = None

    def _configure_glagol(self, device_id):
        # Glagol использует тип устройства из прошивки, а не профиль LED/часов панели.
        # Например, Лайт первого поколения — yandexmicro, даже при резервном профиле lite2.
        try:
            config, _, _ = self.adb.shell(f"cat {QUASAR}/quasar.cfg 2>/dev/null", check=False)
            config = json.loads(config)
            common = config.get("common") if isinstance(config, dict) else None
            platform = common.get("deviceType") if isinstance(common, dict) else None
            if isinstance(platform, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", platform):
                self.glagol.platform = platform
        except (AdbError, OSError, subprocess.TimeoutExpired, ValueError):
            pass  # На старых прошивках без этого поля остаётся платформа выбранного профиля.
        # Публикуем device_id после platform: фоновый поток уже может ожидать подключения.
        self.glagol.device_id = device_id or None
        if not self.glagol.host:     # колонка по USB — для Glagol нужен её адрес в Wi-Fi
            ip, _, _ = self.adb.shell("wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^ip_address=//p'", check=False)
            self.glagol.host = strip_ansi(ip).strip() or None
        self.glagol.wake.set()

    # ---------- статус ----------
    def version(self):
        if self._version is None:
            out, _, _ = self.adb.shell(f"{QRUN}/maind --version 2>/dev/null", timeout=20)
            m = re.search(r"Quasar Software Version:\s*(\S+)", out)
            d, _, _ = self.adb.shell(f"{QRUN}/maind --device-id 2>/dev/null | tail -1", timeout=20)
            self._version = {"quasar_version": m.group(1) if m else "?", "device_id": d.replace("Device ID:", "").strip()}
            self._configure_glagol(self._version["device_id"])
        return self._version

    def status(self):
        script = r"""
echo "hostname=$(hostname)"
echo "hardware_model=$(cat /proc/device-tree/model 2>/dev/null | tr '\000' ' ')"
echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
awk '/MemTotal/{print "mem_total="$2} /MemAvailable/{print "mem_avail="$2}' /proc/meminfo
echo "temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
echo "kernel=$(uname -r)"
echo "color=$(yandex-getcolor 2>/dev/null)"
echo "date=$(date '+%Y-%m-%d %H:%M:%S %Z')"
wpa_cli -i wlan0 status 2>/dev/null | grep -E '^(ssid|ip_address|freq|address|wpa_state)='
wpa_cli -i wlan0 signal_poll 2>/dev/null | grep -E '^(RSSI|LINKSPEED)='
echo "vol_master=$(amixer cget name='Master Playback Volume' | awk -F'[=,]' '/: values/{print $2}')"
echo "vol_bt=$(amixer cget name='Bluetooth Master Playback Volume' | awk -F'[=,]' '/: values/{print $2}')"
pidof quasar_launcher2 >/dev/null && echo quasar=running || echo quasar=stopped
pidof aplay >/dev/null && echo playing=1 || echo playing=0
echo @@BLUEZ
""" + BLUEZ_DUMP + "\n"
        out, _, _ = self.adb.shell(script)
        out, _, bluez = strip_ansi(out).partition("@@BLUEZ")
        st = {"bt_connected": []}
        for path, ifaces in parse_bluez(bluez).items():
            if "org.bluez.Adapter1" in ifaces:
                a = ifaces["org.bluez.Adapter1"]
                for k in ("Powered", "Discoverable", "Pairable"):
                    if k in a:
                        st[k.lower()] = "yes" if a[k] else "no"
            dev = ifaces.get("org.bluez.Device1")
            if dev and dev.get("Connected"):
                st["bt_connected"].append(f"{dev.get('Address', '')} {dev.get('Alias') or dev.get('Name') or ''}".strip())
        for line in out.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k == "bt_conn":
                st["bt_connected"].append(v.strip())
            else:
                st[k] = v.strip()
        st.update(self.version())
        st["ffmpeg"] = bool(self.ffmpeg)
        st["vol_level"] = alsa_to_level(st.get("vol_master"))
        st["muted"] = bool(load_settings().get("mute_before")) and st["vol_level"] == 0
        st["via"] = "agent" if self.adb.agent and self.adb.agent_ok else "adb"
        return st

    # ---------- громкость ----------
    def _volume_level(self):
        """Громкость колонки в уровнях 0–100: Glagol главнее, иначе ALSA Master."""
        v = (self.glagol.state or {}).get("volume")
        if isinstance(v, (int, float)):
            return max(0, min(100, round(float(v) * 20) * 5))
        out, _, _ = self.adb.shell("amixer cget name='Master Playback Volume' 2>/dev/null; true", check=False)
        m = re.search(r": values=(\d+)", strip_ansi(out))
        return alsa_to_level(m.group(1)) if m else None

    def set_mute(self, on):
        """Выключить/вернуть звук: запоминаем громкость и ставим 0 через тот же путь, что и ползунок."""
        s = load_settings()
        if on:
            if not s.get("mute_before"):
                vol = self._volume_level()
                if vol is None:
                    raise AdbError("не удалось узнать громкость колонки")
                if vol > 0:
                    s["mute_before"] = vol / 100
                    save_settings(s)
            self.set_volume({"level": 0})
            return {"muted": True, "volume": 0}
        before = round(float(s.pop("mute_before", 0.5)) * 100)
        save_settings(s)
        self.set_volume({"level": before})
        return {"muted": False, "volume": before}

    def set_volume(self, data):
        if "level" in data:
            level = max(0, min(100, round(float(data["level"]) / 5) * 5))
            if level > 0:   # пользователь выставил громкость — режим mute снимаем
                s = load_settings()
                if s.pop("mute_before", None) is not None:
                    save_settings(s)
            # Через Glagol громкость ставит сам Quasar — тогда на колонке те же цифры и та же логика.
            if self.glagol.ws:
                self.glagol.send({"command": "setVolume", "volume": level / 100})
                return {"ok": True, "via": "glagol"}
            data = {**data, "master": level_to_alsa(level)}
        cmds = []
        for key, ctl in (("master", "Master Playback Volume"), ("bt", "Bluetooth Master Playback Volume")):
            if key in data:
                v = max(0, min(255, int(data[key])))
                cmds.append(f"amixer cset name='{ctl}' {v},{v} >/dev/null")
        if cmds:
            self.adb.shell("; ".join(cmds))
        return {"ok": True}

    # ---------- звуки ----------
    def sounds(self):
        out, _, _ = self.adb.shell(f"cd {SOUNDS_DIR} && find . -type f | sort")
        return [l[2:] for l in out.splitlines() if l.startswith("./")]

    def detach(self, tag, script):
        """Запускает скрипт на колонке так, чтобы он пережил закрытие adb-сессии.

        nohup/setsid adbd всё равно прибивает, поэтому используем start-stop-daemon -b,
        как это делает rootkeeper.sh.
        """
        sd = f"/tmp/webpanel_{tag}.sd"
        self.adb.shell(f"rm -f {sd}; start-stop-daemon -S -b -m -p {sd} -x /bin/sh -- -c {shlex.quote(script)}")

    def _push_play_file(self, local):
        # Уникальное имя: пока играет старый файл, новый ему не мешает; после проигрывания убираем.
        remote = f"{WEB_DIR}/play-{os.getpid()}-{os.urandom(4).hex()}.wav"
        self.adb.shell(f"mkdir -p {WEB_DIR}", check=False)
        self.adb.push(local, remote)
        return remote

    def _play_remote_wav(self, path, cleanup=False):
        self.adb.shell("killall aplay 2>/dev/null; true", check=False)
        q = shlex.quote(path)
        rm = f"; rm -f {q}" if cleanup else ""
        self.detach("play", f"rm -f {PLAY_LOG}; aplay -q {q} >{PLAY_LOG} 2>&1{rm}")
        # aplay в фоне: через секунду смотрим его stderr. Успешный -q ничего не пишет, ошибка видна.
        time.sleep(1.0)
        log = strip_ansi(self.adb.shell(f"tail -c 400 {PLAY_LOG} 2>/dev/null; true", check=False)[0]).strip()
        if re.search(r"(?i)(error|cannot|failed|no such|unable|busy)", log):
            raise AdbError("не удалось проиграть звук на колонке: " + log[-300:])

    def _transcode_and_play(self, src_bytes):
        if not self.ffmpeg:
            raise AdbError("Для этого формата нужен ffmpeg на компьютере (поддерживается только WAV)")
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "out.wav"
            p = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                                "-ac", "2", "-ar", "48000", "-sample_fmt", "s16", "-y", str(dst)],
                               input=src_bytes, capture_output=True, timeout=300)
            if p.returncode != 0:
                raise AdbError("ffmpeg: " + p.stderr.decode("utf-8", "replace").strip()[-400:])
            remote = self._push_play_file(dst)
        self._play_remote_wav(remote, cleanup=True)

    def _play_path(self, path):
        # Расширению верить нельзя: например, sounds/common/boot.wav — это MP3. aplay берём только для RIFF.
        out, _, _ = self.adb.shell(f"head -c 4 {shlex.quote(path)}", check=False)
        if out.startswith("RIFF"):
            self._play_remote_wav(path)
        else:
            self._transcode_and_play(self.adb.read_bytes(path))
        return {"ok": True}

    def play_system(self, name):
        if name not in self.sounds():
            raise AdbError("нет такого звука")
        return self._play_path(f"{SOUNDS_DIR}/{name}")

    # ---------- свои системные звуки ----------
    SOUND_UPLOAD_LIMIT = 50 * 1024 * 1024

    def _sound_paths(self, name):
        if name not in self.sounds():
            raise AdbError("нет такого звука")
        return f"{SOUNDS_DIR}/{name}", f"{OVR_DIR}/sounds/{name}"

    def sounds_custom(self):
        """Какие системные звуки заменены. Звук запуска — свой файл со времён вкладки «Запуск»."""
        out, _, _ = self.adb.shell(
            f"cd {OVR_DIR}/sounds 2>/dev/null && find . -type f; [ -f {SOUND_CUSTOM} ] && echo @boot; "
            f"grep ' {SOUNDS_DIR}/' /proc/mounts | cut -d' ' -f2; true", check=False)
        lines = strip_ansi(out).split()
        mounted = {l[len(SOUNDS_DIR) + 1:] for l in lines if l.startswith(SOUNDS_DIR + "/")}
        custom = {l[2:] for l in lines if l.startswith("./")}
        if "@boot" in lines:
            custom.add(posixpath.relpath(SOUND_TARGET, SOUNDS_DIR))
        return {"custom": sorted(custom), "pending": sorted(custom - mounted), "ffmpeg": bool(self.ffmpeg)}

    def _audio_like(self, original, body):
        """Перекодирует body в тот же кодек, частоту и число каналов, что у стандартного звука: расширению не
        верим (в .wav бывают Opus и MP3), поэтому смотрим, что о нём скажет ffmpeg."""
        if not self.ffmpeg:
            raise AdbError("для замены звука нужен ffmpeg на компьютере")
        p = subprocess.run([self.ffmpeg, "-hide_banner", "-i", "pipe:0", "-f", "null", "-"],
                           input=original, capture_output=True, timeout=60)
        info = p.stderr.decode("utf-8", "replace")
        m = re.search(r"Audio: (\w+).*?, (\d+) Hz, (mono|stereo)", info)
        if not m:
            raise AdbError("не удалось понять формат стандартного звука")
        codec, rate, ch = m.group(1), m.group(2), "1" if m.group(3) == "mono" else "2"
        enc = {"opus": ["-c:a", "libopus", "-b:a", "96k", "-ar", "48000", "-f", "ogg"],
               "vorbis": ["-c:a", "libvorbis", "-q:a", "5", "-ar", rate, "-f", "ogg"],
               "mp3": ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", rate, "-id3v2_version", "0", "-write_xing", "0",
                       "-f", "mp3"]}.get(codec)
        if enc is None and codec.startswith("pcm_"):
            enc = ["-c:a", "pcm_s16le", "-ar", rate, "-f", "wav"]
        if enc is None:
            raise AdbError(f"формат стандартного звука ({codec}) панель пока не умеет")
        # системный звук не должен играть бесконечно: не длиннее 5 минут
        p = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn", "-t", "300",
                            "-ac", ch, "-map_metadata", "-1", *enc, "pipe:1"], input=body, capture_output=True, timeout=300)
        if p.returncode != 0 or not p.stdout:
            raise AdbError("ffmpeg: " + p.stderr.decode("utf-8", "replace").strip()[-400:])
        return p.stdout

    def sound_replace(self, name, body):
        if len(body) > self.SOUND_UPLOAD_LIMIT:
            raise AdbError("файл слишком большой")
        target, custom = self._sound_paths(name)
        if target == SOUND_TARGET:
            self.boot_sound_install(name, body)
        else:
            self._install_override(self._audio_like(self._original_bytes(target), body), custom, target)
        return self.sounds_custom()

    def sound_reset(self, name):
        target, custom = self._sound_paths(name)
        if target == SOUND_TARGET:
            self.boot_sound_reset()
        else:
            self._remove_override(custom, target)
        return self.sounds_custom()

    def sound_play_original(self, name):
        target, _ = self._sound_paths(name)
        data = self._original_bytes(target)
        if data[:4] == b"RIFF":
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "f.wav").write_bytes(data)
                remote = self._push_play_file(Path(td) / "f.wav")
            self._play_remote_wav(remote, cleanup=True)
        else:
            self._transcode_and_play(data)
        return {"ok": True}

    def play_upload(self, filename, body):
        if filename.lower().endswith(".wav") and not self.ffmpeg:
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                f.write(body)
                f.flush()
                remote = self._push_play_file(f.name)
            self._play_remote_wav(remote, cleanup=True)
        else:
            self._transcode_and_play(body)
        return {"ok": True}

    def stop_sound(self):
        self.adb.shell("killall aplay 2>/dev/null; true")
        return {"ok": True}

    # ---------- светодиоды ----------
    def patterns(self):
        res = {}
        for kind, d in PATTERNS.items():
            # свои — не через ls: busybox показывает не-ASCII имена знаками «?»
            out, _, _ = self.adb.shell(f"ls {d}; echo '<<<'; for f in {MY_ANIM_DIR}/{kind}/*; do [ -f \"$f\" ] && echo \"${{f##*/}}\"; done; true")
            std, _, mine = strip_ansi(out).partition("<<<")
            res[kind] = sorted(std.split()) + ["my/" + n for n in sorted(mine.split())]
        return res

    @staticmethod
    def _pattern_path(kind, name):
        """Файл анимации на колонке: встроенная — в Quasar, своя (my/…) — в MY_ANIM_DIR."""
        if name.startswith("my/"):
            return f"{MY_ANIM_DIR}/{kind}/{name[3:]}"
        return f"{PATTERNS[kind]}/{name}"

    # ---------- свои анимации ----------
    def my_anim_save(self, kind, name, frames=None, text=None):
        """Сохранить свою анимацию (новую или поверх своей же). Имя — буквы, цифры, «_» и «-»."""
        name = re.sub(r"\s+", "_", str(name or "").strip())
        if kind not in PATTERNS or not MY_ANIM_NAME.fullmatch(name):
            raise AdbError("имя своей анимации — до 40 букв, цифр, «_» или «-»")
        if kind == "clock":
            if not isinstance(frames, list) or not frames:
                raise AdbError("нет кадров")
            data, fname = encode_clock_pattern(frames), name + ".binscled"
        else:
            data, fname = check_led(text).encode(), name + ".led"
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a"
            f.write_bytes(data)
            self.adb.shell(f"mkdir -p {MY_ANIM_DIR}/{kind}")
            self.adb.push(f, f"{MY_ANIM_DIR}/{kind}/{fname}")
        return {"name": "my/" + fname, "patterns": self.patterns()}

    def my_anim_delete(self, kind, name):
        if kind not in PATTERNS or not name.startswith("my/") or name not in self.patterns()[kind]:
            raise AdbError("нет такой своей анимации")
        self.adb.shell(f"rm -f {shlex.quote(self._pattern_path(kind, name))}; "
                       f"rmdir {MY_ANIM_DIR}/{kind} {MY_ANIM_DIR} 2>/dev/null; true")
        return {"patterns": self.patterns()}

    def play_pattern(self, kind, name, seconds):
        if kind not in PATTERNS or name not in self.patterns()[kind]:
            raise AdbError("нет такого паттерна")
        path = self._pattern_path(kind, name)
        if kind == "clock":
            return self.play_clock(decode_clock_pattern(name, self.adb.read_bytes(path)), seconds)
        self._run_timed(kind, f"{QRUN}/led_util {shlex.quote(path)}", seconds)
        return {"ok": True}

    def _run_timed(self, kind, cmd, seconds):
        # Ни led_util, ни зацикленные анимации не завершаются сами — запускаем в фоне и убиваем по таймеру.
        seconds = max(1, min(600, int(seconds)))
        pidf = f"/tmp/webpanel_{kind}.pid"
        self.stop_pattern(kind)
        self.detach(kind, f"{cmd} >/dev/null 2>&1 & p=$!; echo $p > {pidf}; "
                          f"sleep {seconds}; kill $p 2>/dev/null; rm -f {pidf}")

    def play_clock(self, frames, seconds):
        frames = decode_clock_pattern(".binscled", encode_clock_pattern(frames))  # нормализуем ввод клиента
        self.display_stop()
        script = "/tmp/webpanel_clock_anim.sh"
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "anim.sh"
            write_unix(f, clock_player_script(frames))
            self.adb.push(f, script)
        self._run_timed("clock", f"sh {script}", seconds)
        return {"ok": True, "frames": len(frames)}

    # ---------- «закреплённый» дисплей часов ----------
    def display_status(self):
        out, _, _ = self.adb.shell(f"[ -f /tmp/webpanel_display.sd ] && kill -0 $(cat /tmp/webpanel_display.sd) 2>/dev/null "
                                   f"&& cat {DISPLAY_MODE} 2>/dev/null; true")
        parts = out.split()
        if not parts:
            return {"mode": None}
        res = {"mode": parts[0]}
        if parts[0] == "timer" and len(parts) > 1:
            dev_now, _, _ = self.adb.shell("date +%s")
            res["left"] = max(0, int(parts[1]) - int(dev_now.strip() or 0))
        return res

    def display_set(self, d):
        mode = d.get("mode")
        bright = max(1, min(255, int(d.get("brightness", 160))))
        frame, seconds = None, 0
        if mode == "frame":
            frame = [max(0, min(255, int(x))) for x in d["v"]]
            if len(frame) != len(CLOCK_LEDS):
                raise AdbError(f"нужно {len(CLOCK_LEDS)} значений")
        elif mode == "timer":
            seconds = max(1, min(99 * 60 + 59, int(d["seconds"])))
        elif mode not in ("date", "temp"):
            raise AdbError("неизвестный режим дисплея")
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "display.sh"
            write_unix(f, display_script(mode, frame, seconds, bright))
            self.adb.push(f, DISPLAY_SCRIPT)
            if mode == "timer":
                self._prepare_timer_sound(Path(td))
        self.display_stop()
        self.stop_pattern("clock")
        self.detach("display", f"exec sh {DISPLAY_SCRIPT}")
        # Скрипт пишет свой mode-файл не мгновенно — отвечаем сразу тем, что запустили.
        return {"mode": mode, **({"left": seconds} if mode == "timer" else {})}

    def _prepare_timer_sound(self, td):
        """sounds/*/timer.wav у Quasar на деле Ogg/Opus — aplay его не играет. Один раз перекодируем в WAV."""
        out, _, _ = self.adb.shell(f"[ -f {TIMER_SOUND} ] && echo have; true")
        if "have" in out or not self.ffmpeg:
            return
        src = next((f"{SOUNDS_DIR}/{n}" for n in self.sounds() if n.endswith("/timer.wav") or n == "timer.wav"), None)
        if not src:
            return
        dst = td / "timer.wav"
        r = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "2", "-ar", "48000",
                            "-sample_fmt", "s16", "-y", str(dst)], input=self.adb.read_bytes(src), capture_output=True)
        if r.returncode == 0:
            self.adb.push(dst, TIMER_SOUND)

    @staticmethod
    def _once_snapshot():
        """Перед «показать один раз» запоминаем дисплей (если снимка ещё нет — повторные «один раз» не затирают часы)."""
        return f"{clock_restore_sh()}\n[ -f {DISPLAY_ONCE} ] || snap_save {DISPLAY_ONCE}\n"

    def display_stop(self):
        # SIGTERM — скрипт сам вернёт прежнее содержимое дисплея. Ждём, пока он завершится: иначе следующий
        # режим успеет запомнить себе «прежний» дисплей раньше, чем тот восстановлен.
        self.adb.shell("f=/tmp/webpanel_display.sd; if [ -f $f ]; then p=$(cat $f); kill $p 2>/dev/null; "
                       "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do kill -0 $p 2>/dev/null || break; "
                       "usleep 100000; done; rm -f $f; fi; true")
        # «показать один раз» скрипта не запускает — возвращаем по его снимку
        self.adb.shell(f"{clock_restore_sh()}\nclock_restore {DISPLAY_ONCE}; rm -f {DISPLAY_ONCE}; true")
        return {"mode": None}

    def clock_frames(self, name):
        if name not in self.patterns()["clock"]:
            raise AdbError("нет такого паттерна")
        return decode_clock_pattern(name, self.adb.read_bytes(self._pattern_path("clock", name)))

    def stop_pattern(self, kind=None):
        kinds = [kind] if kind else list(PATTERNS)
        cmd = "; ".join(f"for f in /tmp/webpanel_{k}.pid /tmp/webpanel_{k}.sd; do "
                        f"[ -f $f ] && kill $(cat $f) 2>/dev/null; rm -f $f; done" for k in kinds)
        if "ring" in kinds:
            cmd += "; " + RING_TRIGGER_CMD
        self.adb.shell(cmd + "; true")
        return {"ok": True}

    def midi_hardware(self):
        raise AdbError("Диагностика Cucumber доступна только в профиле Миди")

    def midi_tests(self):
        raise AdbError("Цветовые пробы доступны только в профиле Миди")

    def midi_clock_tests(self):
        return self.midi_tests()

    def midi_test_run(self, data):
        return self.midi_tests()

    def midi_test_observe(self, data):
        return self.midi_tests()

    def midi_test_restore(self):
        return self.midi_tests()

    def logo(self, data):
        raise AdbError("Управление отдельным логотипом не поддерживается этим профилем")

    def logo_reset(self):
        raise AdbError("Управление отдельным логотипом не поддерживается этим профилем")

    def ring_reset(self):
        """Отдать кольцо Алисе. Quasar рисует его через /dev/fb0 («YNDX LED ring», 3×1 BGR), а до светодиодов
        картинка доходит через LED-триггер led_ring_fb. Запись 0 в sysfs brightness снимает триггер (так устроено
        ядро) — после этого кольцо «мёртвое». Возвращаем триггер и гасим наш кадр."""
        self.stop_pattern("ring")
        self.adb.shell(RING_TRIGGER_CMD + "; " + ring_fb_cmd([(0, 0, 0)] * 3))
        return {"ok": True, "leds": [[0, 0, 0]] * 3}

    def ring(self, data):
        """Цвет пишем во framebuffer, как это делает сам Quasar: он остаётся до следующего рисунка Алисы
        (громкость, ответ, слушание) и не перебивает его."""
        rgb = tuple(max(0, min(255, int(data.get(c, 0)))) for c in ("red", "green", "blue"))
        self.stop_pattern("ring")
        self.adb.shell(RING_TRIGGER_CMD + "; " + ring_fb_cmd([rgb] * 3))
        return {"ok": True}

    def led(self, d):
        """LED-панель Станции 2 через штатную led-play. Генерируем .led-кадр/анимацию, кладём на
        колонку и крутим `led-play -r` в фоне (переживает закрытие вкладки и перебивает прошивку).
          frame {tokens:[RRGGBBWW×84]} — удерживать нарисованный кадр
          off                          — удерживать чёрный (гаснет)
          anim {kind, color:[r,g,b]}   — rainbow/pulse/chase
          play {name|args:[name]}      — штатный .led из ledpatterns/panel
          stop / release               — отдать панель Алисе"""
        cmd = str(d.get("cmd", "")).strip()
        with _LED_LOCK:
            if cmd in ("stop", "release"):
                self.adb.shell(LED_STOP_SH)
                return {"ok": True, "running": False}
            if cmd == "play":
                name = str(d.get("name") or (d.get("args") or [""])[0])
                if not re.match(r"^[A-Za-z0-9_]+\.led$", name):
                    raise AdbError("плохое имя анимации")
                self._led_launch(f"{LED_PANEL_DIR}/{name}")
                return {"ok": True, "cmd": "play", "name": name, "running": True}
            if cmd == "off":
                text = _led_line([(0, 0, 0, 0)] * 88)
            elif cmd == "frame":
                text = " ".join(_led_validate_tokens(d.get("tokens"))) + " %x\n" % LED_HOLD_MS
            elif cmd == "anim":
                text = _led_anim_text(str(d.get("kind", "")), d.get("color"))
            else:
                raise AdbError("недопустимая команда LED")
            self._led_push(text)
            self._led_launch(LED_FRAME)
            return {"ok": True, "cmd": cmd, "running": True}

    def led_gif(self, body):
        """Проиграть пользовательский GIF на панели штатной led-play -g (зациклено)."""
        if not body or body[:4] != b"GIF8":
            raise AdbError("это не GIF")
        if len(body) > 4 * 1024 * 1024:
            raise AdbError("слишком большой GIF (>4 МБ)")
        with _LED_LOCK:
            with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
                f.write(body)
                local = f.name
            try:
                self.adb.push(local, LED_GIF)
            finally:
                os.remove(local)
            self.adb.shell(LED_STOP_SH)
            self.adb.shell(f"rm -f {LED_SD}; start-stop-daemon -S -b -m -p {LED_SD} "
                           f"-x {LP} -- -g {shlex.quote(LED_GIF)} -r -d {LED_DEV}")
        return {"ok": True, "running": True}

    def _led_push(self, text):
        """Положить .led-текст на колонку в LED_FRAME (мелкое — через printf, крупное — push файла)."""
        if len(text) < 3000:
            self.adb.shell(f"printf '%s' {shlex.quote(text)} > {LED_FRAME}")
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".led", delete=False) as f:
                f.write(text)
                local = f.name
            try:
                self.adb.push(local, LED_FRAME)
            finally:
                os.remove(local)

    def _led_launch(self, ledfile):
        """Снять прежнюю отрисовку и зациклить led-play по ledfile в фоне."""
        self.adb.shell(LED_STOP_SH)
        self.adb.shell(f"rm -f {LED_SD}; start-stop-daemon -S -b -m -p {LED_SD} "
                       f"-x {LP} -- -l {shlex.quote(ledfile)} -r -d {LED_DEV}")

    def led_patterns(self):
        # через find, а не ls: adb shell идёт по pty и ls красит вывод ANSI + колонками
        out, _, _ = self.adb.shell(f"find {QUASAR}/ledpatterns/panel -maxdepth 1 -type f -name '*.led' 2>/dev/null",
                                   check=False)
        return {"patterns": sorted(posixpath.basename(l.strip()) for l in out.split("\n") if l.strip().endswith(".led"))}

    def clock(self, data):
        if "v" in data:
            v = [max(0, min(255, int(x))) for x in data["v"]]
            if len(v) != len(CLOCK_LEDS):
                raise AdbError(f"нужно {len(CLOCK_LEDS)} значений")
            self.stop_pattern("clock")
            self.display_stop()
            self.adb.shell(self._once_snapshot() + "; ".join(f"echo {val} > {clock_path()}{n}/brightness"
                                                             for n, val in zip(CLOCK_LEDS, v)))
            return {"ok": True}
        text = str(data.get("text", "")).upper()
        bright = max(0, min(255, int(data.get("brightness", 160))))
        # Точку прикрепляем к предыдущему символу.
        cells = []
        for ch in text:
            if ch == "." and cells:
                cells[-1] = (cells[-1][0], True)
            else:
                cells.append((ch, False))
        cells = (cells + [(" ", False)] * 4)[:4]
        cmds = []
        for pos, (ch, dot) in enumerate(cells):
            on = set(SEGMENTS.get(ch, "")) | ({"H"} if dot else set())
            for seg in "ABCDEFGH":
                cmds.append(f"echo {bright if seg in on else 0} > {clock_path()}{seg}{pos}/brightness")
        for extra in ("DEGREE", "DH", "DL", "DM"):
            v = bright if data.get(extra.lower()) else 0
            cmds.append(f"echo {v} > {clock_path()}{extra}/brightness")
        self.adb.shell(self._once_snapshot() + "; ".join(cmds))
        return {"ok": True}

    def clock_state(self):
        # Что сейчас горит на дисплее и кольце — для 3D-модели в «Обзоре».
        ring = [f"plum:{c}:ring{i}/brightness" for i in (1, 2, 3) for c in ("red", "green", "blue")]
        out, _, _ = self.adb.shell("cd /sys/class/leds && cat " + " ".join(
            [f"{LED_PREFIX}:white:clock{n}/brightness" for n in CLOCK_LEDS] + ring))
        v = [int(x) for x in out.split()]
        if len(v) != len(CLOCK_LEDS) + len(ring):
            raise AdbError("не удалось прочитать светодиоды")
        r = v[len(CLOCK_LEDS):]
        return {"v": v[:len(CLOCK_LEDS)], "ring": [r[0:3], r[3:6], r[6:9]]}

    def ring_frames(self, name):
        if name not in self.patterns()["ring"]:
            raise AdbError("нет такого паттерна")
        return parse_led(self.adb.read_bytes(self._pattern_path("ring", name)).decode("utf-8", "replace"))

    # ---------- свои анимации: подмена файлов ledpatterns, как у звуков ----------
    def _anim_paths(self, kind, name):
        if kind not in PATTERNS or name not in self.patterns()[kind] or name.startswith("my/"):
            raise AdbError("нет такой анимации")
        target = f"{PATTERNS[kind]}/{name}"
        return target, f"{OVR_DIR}/{posixpath.relpath(target, QUASAR)}"

    def anim_custom(self):
        out, _, _ = self.adb.shell(
            f"cd {OVR_DIR}/ledpatterns 2>/dev/null && find . -type f; [ -f {BOOT_CUSTOM} ] && echo @boot; true", check=False)
        res = {kind: [] for kind in PATTERNS}
        dirs = {posixpath.basename(d): kind for kind, d in PATTERNS.items()}
        for line in strip_ansi(out).split():
            if line == "@boot":
                res["clock"].append(posixpath.basename(BOOT_TARGET))
            elif line.startswith("./") and line.count("/") == 2 and line.split("/")[1] in dirs:
                res[dirs[line.split("/")[1]]].append(line.split("/")[2])
        return {k: sorted(v) for k, v in res.items()}

    def anim_original(self, kind, name):
        target, _ = self._anim_paths(kind, name)
        if target == BOOT_TARGET:   # у заставки копия оригинала своя, со времён вкладки «Запуск»
            return {"frames": self.boot_status().get("default") or []}
        data = self._original_bytes(target)
        if kind == "clock":
            return {"frames": decode_clock_pattern(name, data)}
        return parse_led(data.decode("utf-8", "replace"))

    def anim_install(self, kind, name, frames=None, text=None):
        target, custom = self._anim_paths(kind, name)
        if kind == "clock":
            if not isinstance(frames, list) or not frames:
                raise AdbError("нет кадров")
            if target == BOOT_TARGET:
                self.boot_install(frames)
            else:
                data = encode_clock_pattern(frames)
                self._install_override(zstd_compress(data) if name.endswith(".zst") else data, custom, target)
        else:
            self._install_override(check_led(text).encode(), custom, target)
        return self.anim_custom()

    def anim_reset(self, kind, name):
        target, custom = self._anim_paths(kind, name)
        if target == BOOT_TARGET:
            self.boot_reset()
        else:
            self._remove_override(custom, target)
        return self.anim_custom()

    def ring_try(self, text, seconds):
        """Проиграть анимацию кольца из редактора, ничего не устанавливая."""
        path = "/tmp/webpanel_ring_try.led"
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.led"
            f.write_bytes(check_led(text).encode())
            self.adb.push(f, path)
        self._run_timed("ring", f"{QRUN}/led_util {path}", seconds)
        return {"ok": True}

    # ---------- запуск: заставка часов и звук ----------
    def boot_status(self):
        q = shlex.quote
        out, _, _ = self.adb.shell(
            f"[ -f {BOOT_TARGET} ] && echo target; [ -f {BOOT_CUSTOM} ] && echo custom; [ -f {BOOT_ORIG} ] && echo orig; "
            f"grep -q ' {BOOT_TARGET} ' /proc/mounts && echo mounted; "
            f"[ -f {SOUND_TARGET} ] && echo s_target; [ -f {SOUND_CUSTOM} ] && echo s_custom; "
            f"grep -q ' {SOUND_TARGET} ' /proc/mounts && echo s_mounted; "
            f"{hook_active()} && echo hook; "
            f"[ -f {NO_BOOT_SOUND} ] && echo nosound; [ -f {BOOT_ALWAYS} ] && echo always; true")
        flags = set(out.split())
        res = {"supported": "target" in flags, "custom": "custom" in flags, "mounted": "mounted" in flags,
               "sound_supported": "s_target" in flags, "sound_custom": "s_custom" in flags,
               "sound_mounted": "s_mounted" in flags,
               "hook": "hook" in flags, "no_boot_sound": "nosound" in flags, "always": "always" in flags}
        if res["supported"]:
            # Пока поверх стандартного файла смонтирован свой, стандартный берём из сохранённой копии.
            if not res["mounted"]:
                res["default"] = decode_clock_pattern(".binscled.zst", self.adb.read_bytes(BOOT_TARGET))
            elif "orig" in flags:
                res["default"] = decode_clock_pattern(".binscled.zst", self.adb.read_bytes(BOOT_ORIG))
            else:
                res["default"] = []
            if res["custom"]:
                res["frames"] = decode_clock_pattern(".binscled.zst", self.adb.read_bytes(BOOT_CUSTOM))
        return res

    def _boot_route(self):
        out, _, rc = self.adb.shell(
            boot_route_script(getattr(self, "_boot_require_caller", False)), timeout=10, check=False)
        route = out.strip()
        if rc or route not in ("rootkeeper", "initd"):
            raise AdbError("Автозапуск не подтверждён: нет подходящего rootkeeper.sh или доступной для записи /etc/init.d")
        return route

    def _ensure_hook(self, remount=False):
        """Подключает свежий хук к поддерживаемой точке загрузки.

        Профили с _boot_require_caller проверяют путь до автоматической записи.
        remount требует явного действия владельца, как в upstream 0.2.2.
        init.d запускается после Quasar и не доказывает работу заставки/звука.
        """
        guarded = getattr(self, "_boot_require_caller", False)
        route = None
        if guarded:
            try:
                route = self._boot_route()
            except AdbError:
                if not remount:
                    raise
                # The explicit repair action may install init.d on a read-only
                # root. An uncalled rootkeeper file is still not a valid route.
                route = "initd"
        with tempfile.TemporaryDirectory() as td:
            write_unix(Path(td) / "hook", BOOT_HOOK_SCRIPT)
            self.adb.shell(f"mkdir -p {WEB_DIR}")
            self.adb.push(Path(td) / "hook", BOOT_HOOK)
        remount_branch = f"elif {with_root_rw(initd_write())}; then :; " if remount else ""
        keeper_test = (f"[ {shlex.quote(route)} = rootkeeper ]" if route else
                       f"[ -f {ROOTKEEPER} ] || grep -q {ROOTKEEPER} {ROOTKEEPER_CALLER} 2>/dev/null")
        _, err, rc = self.adb.shell(
            f"chmod 755 {BOOT_HOOK}; "
            f"if {keeper_test}; then "
            f"[ -f {ROOTKEEPER} ] || {{ "
            f"printf '#!/bin/sh\\n# создан веб-панелью: штатный onboot.sh запускает этот файл, если он есть\\n' > {ROOTKEEPER} "
            f"&& chmod 755 {ROOTKEEPER} && touch {ROOTKEEPER_CREATED} || exit 4; }}; "
            f"[ -f {ROOTKEEPER}.webpanel.bak ] || cp {ROOTKEEPER} {ROOTKEEPER}.webpanel.bak; "
            f"grep -q '{HOOK_MARK}' {ROOTKEEPER} || "
            f"sed -i '1a [ -x {BOOT_HOOK} ] && {BOOT_HOOK} {HOOK_MARK}' {ROOTKEEPER}; "
            f"elif grep -q {shlex.quote(HOOK_MARK)} {shlex.quote(INITD_HOOK)} 2>/dev/null; then :; "
            f"elif {initd_writable()}; then {initd_write()}; "
            + remount_branch +
            f"else exit 3; fi", check=False)
        if rc == 3:
            raise AdbError(f"нет {ROOTKEEPER}, штатная загрузка ({ROOTKEEPER_CALLER}) его не вызывает, а /etc/init.d "
                            "только для чтения: подключить к загрузке не к чему")
        if rc != 0:
            raise AdbError(err.strip() or "не удалось подключить хук к загрузке")

    # Что держится на хуке: без него после перезагрузки колонки пропадает
    HOOK_USERS = (("заставка", BOOT_CUSTOM), ("звук запуска", SOUND_CUSTOM), ("заставка при каждой загрузке", BOOT_ALWAYS),
                  ("автозапуск yamd", YAM_AUTOSTART), ("будильник", ALARM_CONF), ("блокировка аналитики", HOSTS_CUSTOM),
                  ("ассистент", ASSIST_CONF), ("звук через Bluetooth-колонку", BTOUT_MAC),
                  ("защищённое соединение (агент)", AGENT_TOKENS), ("Bluetooth выключен", BT_OFF),
                  ("слова активации", WAKE_AUTOSTART))

    def _hook_users_script(self):
        """Печатает по строке на каждую функцию, которой нужен хук."""
        return ("; ".join(f"[ -f {path} ] && echo {shlex.quote('@' + name)}" for name, path in self.HOOK_USERS)
                + f"; find {OVR_DIR}/sounds -type f 2>/dev/null | grep -q . && echo '@свои звуки'"
                + f"; find {OVR_DIR}/ledpatterns -type f 2>/dev/null | grep -q . && echo '@свои анимации'"
                + f"; find {YAM2_SVC_DIR}/boot -type f 2>/dev/null | grep -q . && echo '@автозапуск служб yam'")

    def hook_status(self):
        """Подключён ли хук, свежий ли он и нужен ли — для общей плашки «не переживёт перезагрузку»."""
        out, _, _ = self.adb.shell(
            f"{self._hook_users_script()}; {hook_active()} && echo hook; grep -q '{HOOK_MARK}' {INITD_HOOK} 2>/dev/null && echo initd; "
            f"( {boot_route_script(getattr(self, '_boot_require_caller', False))} ) >/dev/null && echo possible; "
            f"md5sum {BOOT_HOOK} 2>/dev/null; true", check=False)
        out = strip_ansi(out)
        flags = set(out.split())
        users = [l.strip()[1:] for l in out.splitlines() if l.strip().startswith("@")]
        hook = "hook" in flags
        fresh = hashlib.md5(BOOT_HOOK_SCRIPT.encode()).hexdigest() in out
        return {"hook": hook, "fresh": hook and fresh, "users": users, "possible": "possible" in flags,
                "broken": bool(users) and not (hook and fresh), "rootkeeper": ROOTKEEPER,
                "file": INITD_HOOK if "initd" in flags else ROOTKEEPER}

    def hook_repair(self, remount=False):
        """Подключить/обновить хук. remount — корень только для чтения: положить свой скрипт в init.d,
        коротко перемонтировав корень (кнопка в панели спрашивает подтверждение)."""
        self._ensure_hook(remount=bool(remount))
        return self.hook_status()

    def _drop_hook_if_unused(self):
        # Хук нужен, пока есть хоть что-то своё; иначе возвращаем rootkeeper.sh к исходному виду.
        self.adb.shell(
            f"if [ -z \"$({self._hook_users_script()})\" ]; then "
            f"{hook_drop()}; rm -f {BOOT_HOOK}; fi; true")

    def _install_override(self, data, custom, target, orig=None):
        """Кладёт свой файл и сразу монтирует его поверх стандартного. orig — копия оригинала (для заставки и звука
        запуска, по старинке); остальным она не нужна: оригинал под подменой цел, его читает _original_bytes."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "f").write_bytes(data)
            q = shlex.quote
            self.adb.shell(f"mkdir -p {q(posixpath.dirname(custom))}" + (
                f"; grep -q ' {target} ' /proc/mounts || [ -f {orig} ] || cp {target} {orig}" if orig else ""))
            self.adb.push(Path(td) / "f", custom)
        self._ensure_hook()
        q = shlex.quote
        _, err, rc = self.adb.shell(f"grep -q ' {target} ' /proc/mounts || mount --bind {q(custom)} {q(target)}", check=False)
        if rc != 0:
            raise AdbError(err.strip() or "не удалось подменить файл")

    def _remove_override(self, custom, target):
        q = shlex.quote
        self.adb.shell(f"grep -q ' {target} ' /proc/mounts && {{ umount {q(target)} || umount -l {q(target)}; }}; "
                       f"rm -f {q(custom)}; [ -d {OVR_DIR} ] && find {OVR_DIR} -depth -type d -exec rmdir {{}} \\; 2>/dev/null; true")
        self._drop_hook_if_unused()

    def _original_bytes(self, target):
        """Стандартный файл, даже когда поверх смонтирован свой: bind его папки без вложенных монтирований
        (mount --bind не рекурсивный) показывает то, что лежит под подменой."""
        q = shlex.quote
        out, _, _ = self.adb.shell(f"grep -q ' {target} ' /proc/mounts && echo mounted; true", check=False)
        if "mounted" not in out:
            return self.adb.read_bytes(target)
        tmp = "/tmp/webpanel_orig.bin"
        _, err, rc = self.adb.shell(
            f"d=/tmp/webpanel_orig.d; mkdir -p $d && mount --bind {q(posixpath.dirname(target))} $d && "
            f"cp $d/{q(posixpath.basename(target))} {tmp}; rc=$?; umount $d 2>/dev/null; rmdir $d; exit $rc", check=False)
        if rc:
            raise AdbError(err.strip() or "не удалось прочитать стандартный файл")
        try:
            return self.adb.read_bytes(tmp)
        finally:
            self.adb.shell(f"rm -f {tmp}", check=False)

    def boot_install(self, frames):
        if not self.boot_status()["supported"]:
            raise AdbError(f"на этой прошивке нет {BOOT_TARGET}")
        self._install_override(zstd_compress(encode_clock_pattern(frames)), BOOT_CUSTOM, BOOT_TARGET, BOOT_ORIG)
        return self.boot_status()

    def boot_reset(self):
        self._remove_override(BOOT_CUSTOM, BOOT_TARGET)
        return self.boot_status()

    def boot_sound_install(self, filename, body):
        if not self.ffmpeg:
            raise AdbError("для смены звука нужен ffmpeg на компьютере")
        if not self.boot_status()["sound_supported"]:
            raise AdbError(f"на этой прошивке нет {SOUND_TARGET}")
        # Тот же формат, что у оригинала: MP3 44,1 кГц стерео.
        p = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn",
                            "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k",
                            "-map_metadata", "-1", "-id3v2_version", "0", "-write_xing", "0", "-f", "mp3", "pipe:1"],
                           input=body, capture_output=True, timeout=300)
        if p.returncode != 0 or not p.stdout:
            raise AdbError("ffmpeg: " + p.stderr.decode("utf-8", "replace").strip()[-400:])
        self._install_override(p.stdout, SOUND_CUSTOM, SOUND_TARGET, SOUND_ORIG)
        return self.boot_status()

    def boot_sound_reset(self):
        self._remove_override(SOUND_CUSTOM, SOUND_TARGET)
        return self.boot_status()

    def boot_sound_play(self, which):
        st = self.boot_status()
        if which == "custom":
            if not st["sound_custom"]:
                raise AdbError("свой звук не установлен")
            path = SOUND_CUSTOM
        else:
            path = SOUND_ORIG if st["sound_mounted"] else SOUND_TARGET
        return self._play_path(path)

    def boot_always(self, enabled):
        if enabled:
            self._ensure_hook()
            self.adb.shell(f"touch {BOOT_ALWAYS}")
        else:
            self.adb.shell(f"rm -f {BOOT_ALWAYS}")
            self._drop_hook_if_unused()
        return self.boot_status()

    # ---------- YAM: менеджер пакетов ----------
    _yam_port = None
    _registry = (0, None)

    # ---------- что панель оставила на колонке ----------
    # ---------- блокировка аналитики ----------
    def privacy_status(self):
        out, _, _ = self.adb.shell(
            f"grep -q ' {HOSTS_TARGET} ' /proc/mounts && echo mounted; [ -f {HOSTS_CUSTOM} ] && echo custom; "
            f"echo queued=$(cat {METRICA_BATCH} 2>/dev/null | wc -c); md5sum {BOOT_HOOK} 2>/dev/null; "
            # проверка на деле: куда сейчас ведёт адрес аналитики
            f"ping -c 1 -W 1 {ANALYTICS_HOSTS[0]} 2>&1 | head -n 1; true", check=False, timeout=15)
        out = strip_ansi(out)
        enabled = "mounted" in out and "custom" in out
        # хук от прошлой версии панели не умеет чистить очередь — обновляем
        if enabled and hashlib.md5(BOOT_HOOK_SCRIPT.encode()).hexdigest() not in out:
            self._ensure_hook()
        m = re.search(r"queued=(\d+)", out)
        return {"enabled": enabled, "hosts": list(ANALYTICS_HOSTS), "queued": int(m.group(1)) if m else 0,
                "blocked": f"({ANALYTICS_HOSTS[0]}" not in out and "127.0.0.1" in out}

    def privacy_set(self, enabled):
        if not enabled:
            # сначала очередь: как только адреса откроются, телеметрия отправила бы всё, что накопилось
            self.adb.shell(f"{ANALYTICS_PURGE}; true", check=False)
            self.adb.shell(f"grep -q ' {HOSTS_TARGET} ' /proc/mounts && {{ umount {HOSTS_TARGET} || umount -l {HOSTS_TARGET}; }}; "
                           f"rm -f {HOSTS_CUSTOM}; true")
            self._drop_hook_if_unused()
            return self.privacy_status()
        # основа — настоящий системный /etc/hosts (если наш уже подключён, сначала снимаем его)
        self.adb.shell(f"grep -q ' {HOSTS_TARGET} ' /proc/mounts && umount {HOSTS_TARGET}; true")
        base = strip_ansi(self.adb.read_bytes(HOSTS_TARGET).decode("utf-8", "replace")).replace("\r\n", "\n")
        lines = [ln for ln in base.splitlines() if ln.strip()]
        text = "\n".join(lines + ["", HOSTS_MARK] + [f"127.0.0.1\t{h}" for h in ANALYTICS_HOSTS]) + "\n"
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "hosts"
            write_unix(f, text)
            self.adb.shell(f"mkdir -p {WEB_DIR}")
            self.adb.push(f, HOSTS_CUSTOM)
        self._ensure_hook()
        _, err, rc = self.adb.shell(f"chmod 644 {HOSTS_CUSTOM}; mount --bind {HOSTS_CUSTOM} {HOSTS_TARGET}", check=False)
        if rc:
            raise AdbError("не удалось подключить свой /etc/hosts: " + strip_ansi(err).strip()[-200:])
        self.privacy_purge()
        return self.privacy_status()

    def privacy_purge(self):
        """Удалить накопленную неотправленную аналитику (только при включённой блокировке — иначе незачем)."""
        self.adb.shell(f"[ -f {HOSTS_CUSTOM} ] && {{ {ANALYTICS_PURGE}; }}; true", check=False)
        time.sleep(2)   # launcher перезапускает телеметрию
        return self.privacy_status()

    # ---------- свой голосовой помощник ----------
    ASSIST_FIELDS = {"llm": ("base_url", "model", "system_prompt", "max_tokens", "history", "proxy"),
                     "tts": ("provider", "base_url", "model", "voice", "speed"),
                     "led": ("enabled", "color"),
                     "mode": ("timeout_min", "follow_up")}

    ASSIST_ASSET = "assistantd-linux-arm64"

    def _assist_binary(self, download=True):
        return self._device_binary(ASSIST_SRC, "assistantd", self.ASSIST_ASSET, "помощника", download)

    def _device_binary(self, src, name, asset, what, download=True):
        """Программа для колонки (linux/arm64) из device/<…>, по порядку:
        1) есть Go — собираем из исходников (для тех, кто меняет её код);
        2) готовая сборка своей версии рядом (Docker, архив для Windows);
        3) скачанная с релиза GitHub для этой версии панели — с проверкой SHA256SUMS, как yamd;
        4) готовая сборка другой версии — лучше, чем ничего."""
        build = src / "build" / name
        go = shutil.which("go")
        if go:
            sources = list(src.glob("*.go")) + [src / "go.mod"]
            fresh = build.exists() and all(f.stat().st_mtime <= build.stat().st_mtime for f in sources if f.exists())
            if not fresh:
                env = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64"}
                r = subprocess.run([go, "build", "-trimpath", "-ldflags", f"-s -w -X main.version={VERSION}",
                                    "-o", str(build), "."], cwd=src, env=env, capture_output=True, text=True, timeout=300)
                if r.returncode:
                    raise AdbError(f"не удалось собрать программу {what}: " + (r.stderr or r.stdout).strip()[-400:])
            return build
        try:
            bundled = (src / "build" / "VERSION").read_text().strip()
        except OSError:
            bundled = ""
        if build.exists() and bundled == VERSION:
            return build
        cached = DATA_DIR / f".{name}-{VERSION}"
        if cached.exists():
            return cached
        if download:
            try:
                return self._release_download(asset, cached, name)
            except (OSError, ValueError, AdbError) as e:
                if not build.exists():
                    raise AdbError(f"не удалось скачать программу {what} для версии {VERSION}: {e}") from None
        if build.exists():
            return build
        raise AdbError(f"нет программы {what} для колонки: она скачивается с релиза GitHub — проверьте интернет")

    def _release_download(self, asset, dest, name):
        base = f"https://github.com/{REPO}/releases/download/v{VERSION}/"
        get = lambda n, limit: urllib.request.urlopen(urllib.request.Request(
            base + n, headers={"User-Agent": f"yapanel-web/{VERSION}"}), timeout=60).read(limit)
        sums = get("SHA256SUMS", 64 * 1024).decode("utf-8", "replace")
        want = next((ln.split()[0] for ln in sums.splitlines() if ln.split()[-1:] == [asset]), None)
        if not want:
            raise AdbError(f"в SHA256SUMS релиза нет {asset}")
        blob = get(asset, 64 * 1024 * 1024)
        if hashlib.sha256(blob).hexdigest() != want.lower():
            raise AdbError(f"контрольная сумма {asset} не совпала")
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, dest)
        for old in DATA_DIR.glob(f".{name}-*"):   # прежние версии больше не нужны
            if old != dest:
                old.unlink(missing_ok=True)
        return dest

    def _assist_config(self):
        out, _, _ = self.adb.shell(f"cat {ASSIST_CONF} 2>/dev/null; true", check=False)
        try:
            return json.loads(strip_ansi(out).strip() or "{}")
        except ValueError:
            return {}

    _assist_md5 = (0, None)

    def _assist_local_md5(self):
        """md5 сборки на компьютере — понять, что на колонке старая программа. Без Go и сборки — None."""
        try:
            out = self._assist_binary(download=False)   # статус опрашивается часто — ничего не качаем
        except AdbError:
            return None
        mtime = out.stat().st_mtime
        if self._assist_md5[0] != mtime:
            self._assist_md5 = (mtime, hashlib.md5(out.read_bytes()).hexdigest())
        return self._assist_md5[1]

    def assistant_status(self):
        out, _, _ = self.adb.shell(
            f"[ -x {ASSISTD} ] && echo @installed && md5sum {ASSISTD}; "
            f"[ -f {ASSIST_SD} ] && kill -0 $(cat {ASSIST_SD}) 2>/dev/null "
            f"&& echo @running; echo @status; cat {ASSIST_STATUS} 2>/dev/null; echo; true", check=False)
        out = strip_ansi(out)
        flags = set(re.findall(r"^@(\w+)$", out, re.M))
        try:
            state = json.loads(out.split("@status", 1)[1].strip() or "{}")
        except (ValueError, IndexError):
            state = {}
        cfg = self._assist_keys_migrate(self._assist_config())
        # ключи наружу не отдаём: только для каких адресов они есть и последние символы
        mask = lambda k: ("…" + k[-4:]) if len(k) > 8 else "задан"
        cfg["keys"] = {url: mask(k) for url, k in (cfg.get("keys") or {}).items() if k}
        for part in ("llm", "tts"):
            sec = cfg.setdefault(part, {})
            sec["key"] = cfg["keys"].get(self._key_id(sec.get("base_url", "")), "")
        running = "running" in flags
        local = self._assist_local_md5() if "installed" in flags else None
        return {"supported": True, "installed": "installed" in flags, "running": running, "config": cfg,
                "outdated": bool(local) and local not in out,
                "state": state if running else {}}

    def assistant_install(self):
        arch, _, _ = self.adb.shell("uname -m", check=False)
        if strip_ansi(arch).strip() not in ("aarch64", "arm64"):
            raise AdbError(f"ядро колонки {strip_ansi(arch).strip() or '?'}: программа помощника пока собрана "
                           "только под 64-битное (aarch64)")
        binary = self._assist_binary()
        self.adb.shell(f"mkdir -p {ASSIST_DIR}; start-stop-daemon -K -p {ASSIST_SD} 2>/dev/null; true", check=False)
        self.adb.push(binary, ASSISTD + ".new", timeout=300)
        self.adb.shell(f"chmod 755 {ASSISTD}.new; mv {ASSISTD}.new {ASSISTD}")
        if not self._assist_config():
            self._assist_write({"enabled": False})
        try:
            self._ensure_hook()
        except AdbError:
            pass   # хук поставить нельзя (нет rootkeeper.sh): помощник работает до перезагрузки колонки
        return self.assistant_start()

    def _assist_write(self, cfg):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            write_unix(f, json.dumps(cfg, ensure_ascii=False, indent=1))
            try:
                self.adb.push(f, ASSIST_CONF + ".new")
            except Exception:
                self.adb.shell(f"rm -f {ASSIST_CONF}.new", check=False)
                raise
        # push мог оборваться и оставить пустой .new: не ставим его вместо рабочего конфига
        out, _, _ = self.adb.shell(f"cat {ASSIST_CONF}.new 2>/dev/null; true", check=False)
        try:
            saved = json.loads(strip_ansi(out).strip() or "{}")
        except ValueError:
            saved = None
        if not isinstance(saved, dict) or not saved:
            self.adb.shell(f"rm -f {ASSIST_CONF}.new", check=False)
            raise AdbError("конфиг помощника не записался на колонку — оставил прежний")
        self.adb.shell(f"chmod 600 {ASSIST_CONF}.new; mv {ASSIST_CONF}.new {ASSIST_CONF}")

    @staticmethod
    def _key_id(base):
        # как keyID в device/assistant: один провайдер при любом написании адреса
        return str(base or "").strip().rstrip("/").lower()

    def _assist_keys_migrate(self, cfg):
        """Ключи хранятся по адресу API (cfg["keys"]); раньше лежали прямо в llm/tts — переносим."""
        keys = cfg.setdefault("keys", {})
        for part in ("llm", "tts"):
            sec = cfg.get(part) or {}
            key = sec.pop("api_key", None)
            if key and sec.get("base_url") and not keys.get(self._key_id(sec["base_url"])):
                keys[self._key_id(sec["base_url"])] = key
        return cfg

    def assistant_set(self, d):
        """Меняет только переданные поля. Ключ пишется для адреса API своего раздела: новый — заменить,
        пусто или нет поля — оставить. delete_key — удалить ключ указанного адреса."""
        cfg = self._assist_keys_migrate(self._assist_config())
        if "enabled" in d:
            cfg["enabled"] = bool(d["enabled"])
        phrases = lambda xs: [str(x).strip()[:60] for x in xs if str(x).strip()][:10]
        if isinstance(d.get("triggers"), list):
            cfg["triggers"] = phrases(d["triggers"])
        for k in ("enter", "exit"):
            if isinstance((d.get("mode") or {}).get(k), list):
                cfg.setdefault("mode", {})[k] = phrases(d["mode"][k])
        for part, fields in self.ASSIST_FIELDS.items():
            src = d.get(part)
            if not isinstance(src, dict):
                continue
            sec = cfg.setdefault(part, {})
            for k in fields:
                if k in src:
                    sec[k] = src[k]
            key = str(src.get("api_key") or "").strip()
            if key:
                if not sec.get("base_url"):
                    raise AdbError("ключ сохраняется для адреса API — сначала укажите адрес")
                cfg["keys"][self._key_id(sec["base_url"])] = key
        if d.get("delete_key"):
            cfg["keys"].pop(self._key_id(d["delete_key"]), None)
        mode = cfg.get("mode", {})
        if "timeout_min" in mode:
            mode["timeout_min"] = max(1, min(240, int(mode["timeout_min"])))
        led = cfg.get("led", {})
        if "color" in led and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(led["color"])):
            raise AdbError("цвет подсветки — в виде #rrggbb")
        tts = cfg.get("tts", {})
        if tts.get("provider") not in (None, "alice", "openai"):
            raise AdbError("неизвестный способ озвучки")
        if not self.assistant_status()["installed"]:
            raise AdbError("помощник не установлен на колонке")
        self._assist_write(cfg)
        return self.assistant_status()

    def assistant_models(self, d):
        """Список моделей API — запрашивает сама колонка: её сеть и прокси, ключ из настроек или ещё не сохранённый."""
        over = {k: str(d.get(k) or "").strip() for k in ("base_url", "api_key", "proxy")}
        tmp = "/tmp/webpanel_assistant.models"
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "o.json"
            write_unix(f, json.dumps(over, ensure_ascii=False))
            self.adb.push(f, tmp)
        try:
            out, _, _ = self.adb.shell(f"chmod 600 {tmp}; {ASSISTD} -dir {ASSIST_DIR} -models {tmp}; true", timeout=60, check=False)
        finally:
            self.adb.shell(f"rm -f {tmp}", check=False)
        line = next((l for l in strip_ansi(out).splitlines() if l.startswith("{")), "")
        try:
            res = json.loads(line)
        except ValueError:
            raise AdbError("программа на колонке старая — нажмите «Обновить программу»") from None
        if res.get("error"):
            raise AdbError(res["error"])
        return {"models": res.get("models") or []}

    def assistant_start(self):
        self.adb.shell(f"start-stop-daemon -K -p {ASSIST_SD} 2>/dev/null; sleep 0.5; true", check=False)
        self.detach("assistant", ASSIST_RUN)
        for _ in range(10):
            time.sleep(0.5)
            st = self.assistant_status()
            if st["running"]:
                return st
        out, _, _ = self.adb.shell(f"tail -5 {ASSIST_DIR}/assistant.log", check=False)
        raise AdbError("помощник не запустился: " + strip_ansi(out).strip()[-400:])

    def assistant_command(self, cmd):
        if not self.assistant_status()["running"]:
            raise AdbError("помощник не запущен")
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "cmd"
            write_unix(f, json.dumps(cmd, ensure_ascii=False))
            self.adb.push(f, ASSIST_CMD + ".new")
        self.adb.shell(f"mv {ASSIST_CMD}.new {ASSIST_CMD}")
        return {"ok": True}

    def say(self, text, voice="alice"):
        """Произнести текст на колонке: голосом Алисы («Повтори за мной» через её отладочный CLI — без входа
        в Яндекс, но через её облако) или синтезатором ассистента."""
        text = re.sub(r"[\x00-\x1f]", " ", str(text or "")).strip()
        if not 1 <= len(text) <= 300:
            raise AdbError("текст — от 1 до 300 символов")
        if voice == "assistant":
            return self.assistant_command({"say": text})
        # текст — файлом: в команду оболочки он не попадает
        path = "/tmp/webpanel_say.txt"
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "say"
            f.write_text("Повтори за мной: " + text + "\n", encoding="utf-8")
            self.adb.push(f, path)
        self.detach("say", f"t=$(cat {path}); rm -f {path}; "
                           f"{{ sleep 2; printf 'q\\n'; sleep 0.5; printf '%s\\n' \"$t\"; sleep 3; printf 'x\\n'; sleep 1; }} "
                           f"| {QRUN}/maind --cli >/dev/null 2>&1")
        return {"ok": True}

    def assistant_ask(self, text):
        text = str(text or "").strip()
        if not 1 <= len(text) <= 1000:
            raise AdbError("вопрос — от 1 до 1000 символов")
        return self.assistant_command({"ask": text})

    # ---------- радио ----------
    # Свои станции играет та же программа на колонке, что и ассистент (device/assistant/radio.go): встроенный
    # в прошивку GStreamer, без компьютера. Станции Алисы — обычный текстовый запрос «включи радио …».
    RADIO_URL = re.compile(r"https?://[^\s\"'\\!]{3,490}")
    RADIO_SEARCH = "https://all.api.radio-browser.info/json/stations/search"

    RADIO_PROXY = re.compile(r"http://(?:[^\s\"'\\!@/]+@)?[^\s\"'\\!@/]+")

    @staticmethod
    def _mask_proxy(proxy):
        return re.sub(r"(//[^:@/]+:)[^@/]+@", r"\1•••@", proxy or "")

    def radio_status(self):
        st = self.assistant_status()
        radio = st["config"].get("radio") or {}
        return {"supported": st.get("supported", True), "installed": st["installed"], "running": st["running"],
                "outdated": st.get("outdated", False),
                "stations": radio.get("stations") or [], "voice": radio.get("voice", True),
                "own_disabled": self.RADIO_OWN_DISABLED,
                "play": radio.get("play") or ["включи станцию", "включи радиостанцию", "поставь станцию"],
                "stop": radio.get("stop") or ["выключи станцию", "выключи радиостанцию", "останови станцию"],
                "now": (st.get("state") or {}).get("radio") or {},
                "proxy": self._mask_proxy(radio.get("proxy")), "checks": self._radio_checks(),
                "miss": (st.get("state") or {}).get("radio_miss"),
                "alice": [{"id": k, **v} for k, v in self.fm_stations().items()]}

    def _radio_station(self, s):
        name = str((s or {}).get("name") or "").strip()[:80]
        url = str((s or {}).get("url") or "").strip()
        if not name or not self.RADIO_URL.fullmatch(url):
            raise AdbError("у станции нужны название и адрес потока http(s)://… без кавычек и пробелов")
        station = {"name": name, "url": url}
        aliases = [str(a).strip()[:40] for a in ((s or {}).get("aliases") or []) if str(a).strip()][:8]
        if aliases:
            station["aliases"] = aliases
        return station

    def radio_set(self, d):
        cfg = self._assist_keys_migrate(self._assist_config())
        radio = cfg.setdefault("radio", {})
        if isinstance(d.get("stations"), list):
            radio["stations"] = [self._radio_station(s) for s in d["stations"]][:200]
        if "voice" in d:
            radio["voice"] = bool(d["voice"])
        if "proxy" in d:
            proxy = str(d["proxy"] or "").strip()
            if "•••" in proxy:
                proxy = radio.get("proxy", "")     # пароль не показывали — значит, его не меняли
            elif proxy and (not self.RADIO_PROXY.fullmatch(proxy) or re.search(r"[\"'\\!\s]", unquote(proxy))):
                raise AdbError("прокси для радио — http://адрес:порт или http://логин:пароль@адрес:порт")
            radio["proxy"] = proxy
        for k in ("play", "stop"):
            if isinstance(d.get(k), list):
                radio[k] = [str(x).strip()[:60] for x in d[k] if str(x).strip()][:10]
        if not self.assistant_status()["installed"]:
            raise AdbError("программа панели не установлена на колонке")
        self._assist_write(cfg)
        return self.radio_status()

    # Свои станции временно выключены: их проигрыватель ломал общий микшер звука колонки (см. radio.go).
    RADIO_OWN_DISABLED = True

    def radio_play(self, d):
        if self.RADIO_OWN_DISABLED:
            raise AdbError("свои станции временно отключены: их проигрыватель ломал звук колонки. Станции Алисы работают")
        station = self._radio_station(d)
        try:   # музыка Алисы и радио иначе заиграют одновременно
            if self.glagol.ws:
                self.glagol.send({"command": "stop"})
        except Exception:  # noqa: BLE001 — без Glagol радио всё равно включится
            pass
        self.assistant_command({"radio": station})
        return {"ok": True}

    # ---------- проверка станции: идёт ли поток и с какой скоростью ----------
    # Частая беда — блокировка провайдера: первые 16–32 КБ приходят сразу, потом соединение замирает намертво.
    # Проверяем с компьютера с панелью: обычно он ходит в интернет через тот же роутер, что и колонка.
    def _radio_checks(self):
        try:
            return json.loads((DATA_DIR / ".radio_checks.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def radio_check(self, url, seconds=6):
        url = str(url or "").strip()
        if not self.RADIO_URL.fullmatch(url):
            raise AdbError("адрес потока — http(s)://…")
        host = urlparse(url).hostname or ""
        try:
            ip = socket.getaddrinfo(host, None)[0][4][0]
        except OSError:
            ip = ""
        proxy = ((self._assist_config().get("radio") or {}).get("proxy") or "") if self.assistant_status()["installed"] else ""
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        opener = urllib.request.build_opener(*handlers)
        result = {"at": int(time.time()), "host": host, "ip": ip, "proxy": bool(proxy)}
        try:
            r = opener.open(urllib.request.Request(url, headers={"User-Agent": f"yapanel-web/{VERSION}"}), timeout=5)
        except urllib.error.HTTPError as e:
            result.update(state="down", detail=f"сервер ответил {e.code}")
        except (OSError, ValueError) as e:
            result.update(state="down", detail=f"не отвечает: {e}")
        else:
            need = (int(r.headers.get("icy-br") or 128) or 128) * 125   # байт в секунду
            marks, total, t0 = [], 0, time.time()
            with r:
                sock = getattr(getattr(r.fp, "raw", None), "_sock", None)
                while (now := time.time() - t0) < seconds:
                    if sock:
                        sock.settimeout(max(0.2, seconds - now))
                    try:
                        chunk = r.read1(65536)
                    except (OSError, ValueError):
                        break
                    if not chunk:
                        break
                    total += len(chunk)
                    marks.append((time.time() - t0, total))
            kb = total // 1024
            early = next((b for s, b in marks if s >= 2), total)     # что пришло к концу 2-й секунды
            if total == 0:
                result.update(state="down", detail="не прислал ни байта")
            elif total <= 64 * 1024 and total - early < 2048:
                result.update(state="blocked", detail=f"замирает после {kb} КБ")
            elif (total - early) / max(1, seconds - 2) < need * 0.8:
                result.update(state="slow", detail=f"{kb} КБ за {seconds} с — медленнее, чем играет")
            else:
                result.update(state="ok", detail=f"{kb} КБ за {seconds} с")
            result["kb"] = kb
        checks = self._radio_checks()
        checks[url] = result
        try:
            write_unix(DATA_DIR / ".radio_checks.json", json.dumps(checks, ensure_ascii=False))
        except OSError:
            pass
        return result

    def radio_stop(self):
        return self.assistant_command({"radio_stop": True})

    def radio_alice(self, name):
        name = str(name or "").strip()
        if not 1 <= len(name) <= 80:
            raise AdbError("название станции — от 1 до 80 символов")
        self.radio_stop_quiet()
        return self.music_cmd({"command": "sendText", "text": f"включи радио {name}"})

    def radio_stop_quiet(self):
        try:
            if self.assistant_status()["running"]:
                self.assistant_command({"radio_stop": True})
        except AdbError:
            pass

    def radio_search(self, q, country=""):
        """Каталог radio-browser.info: открытый, бесплатный, без ключа. Берём только потоки, которые сможем играть."""
        q = str(q or "").strip()[:80]
        if len(q) < 2:
            raise AdbError("введите хотя бы два символа")
        params = {"name": q, "limit": "40", "hidebroken": "true", "order": "clickcount", "reverse": "true"}
        if re.fullmatch(r"[A-Za-z]{2}", country or ""):
            params["countrycode"] = country.upper()
        req = urllib.request.Request(self.RADIO_SEARCH + "?" + urlencode(params),
                                     headers={"User-Agent": f"yapanel-web/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                items = json.load(r)
        except (OSError, ValueError) as e:
            raise AdbError(f"каталог радиостанций недоступен: {e}") from None
        out = []
        for it in items if isinstance(items, list) else []:
            url = str(it.get("url_resolved") or it.get("url") or "").strip()
            codec = str(it.get("codec") or "").upper()
            if it.get("hls") or not self.RADIO_URL.fullmatch(url):
                continue   # HLS (.m3u8) прошивка не проверена, странные адреса не пройдут в конвейер
            if codec in ("OGG", "VORBIS") or url.lower().split("?")[0].endswith(".ogg"):
                continue   # декодера Vorbis в прошивке нет
            out.append({"name": str(it.get("name") or "").strip()[:80] or url, "url": url,
                        "codec": str(it.get("codec") or "")[:12], "bitrate": int(it.get("bitrate") or 0),
                        "country": str(it.get("countrycode") or "")[:2], "tags": str(it.get("tags") or "")[:80]})
        return {"stations": out}

    # Логотипы станций Алисы — из каталога radio-browser.info. Панель скачивает их сама и хранит у себя
    # (.radio_logos), браузер к сайтам станций не ходит. Берём только точное совпадение названия.
    RADIO_LOGO_QUERY = {"маяк": "Радио Маяк", "энерджи": "Energy", "монтекарло": "Радио Монте-Карло",
                        "детское": "Детское радио", "шансон": "Радио Шансон", "loveradio": "Love Radio"}
    # официальные сайты: у части станций в каталоге нет иконки или она чужая (значок холдинга, белая на прозрачном)
    RADIO_LOGO_SITE = {"авторадио": "https://www.avtoradio.ru", "рекорд": "https://www.radiorecord.ru",
                       "energy": "https://www.energyfm.ru", "маяк": "https://radiomayak.ru",
                       "дорожноерадио": "https://dorognoe.ru", "шансон": "https://radioshanson.fm",
                       "loveradio": "https://www.loveradio.ru", "businessfm": "https://www.bfm.ru"}
    IMAGE_TYPES = ((b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"),
                   (b"\x00\x00\x01\x00", "image/x-icon"))

    @staticmethod
    def _radio_key(name):
        name = str(name or "").lower().replace("ё", "е")
        name = re.sub(r"^радио\s+", "", name)
        return re.sub(r"[^a-zа-я0-9]+", "", name)

    @staticmethod
    def _public_url(url):
        """Только http(s) на публичный адрес: каталог открытый, ссылка не должна вести в домашнюю сеть."""
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        except OSError:
            return False
        return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)

    def radio_logo(self, name):
        name = str(name or "").strip()[:80]
        key = self._radio_key(name)
        if not key:
            raise AdbError("нет названия")
        folder = DATA_DIR / ".radio_logos"
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        image, miss = folder / digest, folder / (digest + ".none")
        if image.exists():
            data = image.read_bytes()
            return data, next((m for sig, m in self.IMAGE_TYPES if data.startswith(sig)), "image/webp")
        if miss.exists() and time.time() - miss.stat().st_mtime < 7 * 86400:
            return None
        data = self._fetch_logo(name, key)
        folder.mkdir(exist_ok=True)
        if not data:
            miss.touch()
            return None
        image.write_bytes(data[0])
        return data

    def _fetch_logo(self, name, key):
        query = self.RADIO_LOGO_QUERY.get(key, name)
        params = {"name": query, "limit": "20", "hidebroken": "true", "order": "clickcount", "reverse": "true"}
        req = urllib.request.Request(self.RADIO_SEARCH + "?" + urlencode(params),
                                     headers={"User-Agent": f"yapanel-web/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                items = json.load(r)
        except (OSError, ValueError):
            return None
        want = {key, self._radio_key(query)}
        # «Радио Маяк (Radio Mayak)», «Радио Шансон - Radio Shanson»: название до скобок и тире — тоже совпадение,
        # но после точного; региональные «Радио Маяк Вологда» не подходят
        main = lambda n: self._radio_key(re.split(r"\s*[(\[|]|\s+[-–—]\s+", str(n or ""), maxsplit=1)[0])
        items = items if isinstance(items, list) else []
        ranked = [it for it in items if self._radio_key(it.get("name")) in want] + \
                 [it for it in items if self._radio_key(it.get("name")) not in want and main(it.get("name")) in want]
        tried = set()
        site = self.RADIO_LOGO_SITE.get(key)
        if site:
            ranked = [{"homepage": site}] + ranked
        for it in ranked:
            # иконка из каталога, иначе — значок официального сайта станции (у многих станций в каталоге её нет)
            urls = [str(it.get("favicon") or "")]
            home = urlparse(str(it.get("homepage") or ""))
            if home.scheme in ("http", "https") and home.netloc:
                urls += [f"{home.scheme}://{home.netloc}/apple-touch-icon.png", f"{home.scheme}://{home.netloc}/favicon.ico"]
            for url in urls:
                if url in tried or not url.startswith(("http://", "https://")):
                    continue
                tried.add(url)
                found = self._download_image(url)
                if found:
                    return found
        return None

    class _PublicRedirects(urllib.request.HTTPRedirectHandler):
        """Перенаправление проверяем так же, как исходный адрес: иконка из каталога не должна увести в домашнюю сеть."""
        max_redirections = 3

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not Panel._public_url(newurl):
                raise urllib.error.HTTPError(newurl, code, "перенаправление на непубличный адрес", headers, fp)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def _download_image(self, url):
        if not self._public_url(url):
            return None
        opener = urllib.request.build_opener(self._PublicRedirects)
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": f"yapanel-web/{VERSION}"}),
                             timeout=8) as r:
                data = r.read(300 * 1024 + 1)
        except (OSError, ValueError):
            return None
        if len(data) > 300 * 1024:
            return None
        kind = next((m for sig, m in self.IMAGE_TYPES if data.startswith(sig)), None)
        if not kind and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = "image/webp"
        return (data, kind) if kind else None   # SVG и прочее не берём: картинка отдаётся с адреса панели

    def assistant_remove(self):
        self.adb.shell(f"start-stop-daemon -K -p {ASSIST_SD} 2>/dev/null; sleep 0.5; "
                       f"rm -rf {ASSIST_DIR}; rm -f {ASSIST_SD} {ASSIST_STATUS} {ASSIST_CMD}; true", check=False)
        self._drop_hook_if_unused()
        return self.assistant_status()

    # ---------- живой спектр с колонки ----------
    VIZ_ASSET = "vizd-linux-arm64"

    def _viz_binary(self, download=True):
        return self._device_binary(VIZ_SRC, "vizd", self.VIZ_ASSET, "визуализатора", download)

    _viz_md5 = (0, None)

    def _viz_local_md5(self):
        """md5 сборки на компьютере — понять, что на колонке старая программа. Без Go и сборки — None."""
        try:
            out = self._viz_binary(download=False)
        except AdbError:
            return None
        mtime = out.stat().st_mtime
        if self._viz_md5[0] != mtime:
            self._viz_md5 = (mtime, hashlib.md5(out.read_bytes()).hexdigest())
        return self._viz_md5[1]

    def viz_status(self):
        """Состояние: установлен, запущен, последние полосы. Полосы нужны в основном для проверки из панели;
        визуализатор получает их потоком светодиодов (LedStream)."""
        out, _, _ = self.adb.shell(
            f"[ -x {VIZD} ] && echo @installed && md5sum {VIZD}; "
            f"[ -f {VIZ_SD} ] && kill -0 $(cat {VIZ_SD}) 2>/dev/null && echo @running; "
            f"echo @state; cat {VIZ_STATUS} 2>/dev/null; echo; true", check=False)
        out = strip_ansi(out)
        flags = set(re.findall(r"^@(\w+)$", out, re.M))
        try:
            state = json.loads(out.split("@state", 1)[1].strip() or "{}")
        except (ValueError, IndexError):
            state = {}
        running = "running" in flags
        age = round(time.time() - state["t"] / 1000, 2) if running and state.get("t") else None
        local = self._viz_local_md5() if "installed" in flags else None
        return {"supported": True, "installed": "installed" in flags, "running": running,
                "outdated": bool(local) and local not in out,
                "bands": (state.get("bands") or []) if running else [],
                "rms": state.get("rms", 0) if running else 0,
                "error": (state.get("error") or "") if running else "",
                "age": age, "device": "hw:0,0"}

    def viz_set(self, on):
        """Включить/выключить живой спектр. Включение при первом разе ставит программу на колонку.

        Открытие захвата кодека слышно коротким щелчком, поэтому на время переключения ставим
        играющую музыку на паузу и сразу продолжаем — щелчок не пугает.
        """
        resume = False
        try:
            if (self.glagol.state or {}).get("playing") and self.glagol.ws:
                self.glagol.send({"command": "stop"}, timeout=4)
                resume = True
                time.sleep(0.4)
        except Exception:  # noqa: BLE001
            resume = False
        try:
            return self._viz_set(on)
        finally:
            if resume:
                try:
                    self.glagol.send({"command": "play"}, timeout=4)
                except Exception:  # noqa: BLE001
                    pass

    def _viz_set(self, on):
        if not on:
            self.adb.shell(f"start-stop-daemon -K -p {VIZ_SD} 2>/dev/null; "
                           f"rm -f {VIZ_SD} {VIZ_ON} {VIZ_STATUS} {VIZ_BT}; true", check=False)
            return self.viz_status()
        st = self.viz_status()
        if not st["installed"] or st.get("outdated"):
            st = self.viz_install()
        else:
            st = self.viz_start()
        # Флаг для tap: при выводе на Bluetooth-колонку полосы считает он, из того же потока.
        # Если переливание уже идёт, перезапускаем его — иначе в конвейере не окажется tap.
        self.adb.shell(
            f"touch {VIZ_ON}; "
            f"[ -f {BTOUT_MAC} ] && {{ p=$(cat {BTOUT_FWD_SD} 2>/dev/null) && {{ {YAM2_KILL}; }}; "
            f"rm -f {BTOUT_FWD_SD}; }}; true", check=False)
        return st

    def viz_install(self):
        arch, _, _ = self.adb.shell("uname -m", check=False)
        if strip_ansi(arch).strip() not in ("aarch64", "arm64"):
            raise AdbError(f"ядро колонки {strip_ansi(arch).strip() or '?'}: живой спектр пока собран "
                           "только под 64-битное (aarch64)")
        binary = self._viz_binary()
        self.adb.shell(f"mkdir -p {VIZ_DIR}; start-stop-daemon -K -p {VIZ_SD} 2>/dev/null; true", check=False)
        self.adb.push(binary, VIZD + ".new", timeout=300)
        self.adb.shell(f"chmod 755 {VIZD}.new; mv {VIZD}.new {VIZD}")
        return self.viz_start()

    def viz_start(self):
        self.adb.shell(f"start-stop-daemon -K -p {VIZ_SD} 2>/dev/null; sleep 0.3; true", check=False)
        self.detach("viz", VIZ_RUN)
        for _ in range(12):
            time.sleep(0.3)
            st = self.viz_status()
            if st["running"]:   # в Bluetooth-режиме полосы пишет tap, статус появится чуть позже
                return st
        out, _, _ = self.adb.shell(f"tail -5 {VIZ_DIR}/viz.log", check=False)
        raise AdbError("визуализатор не запустился: " + strip_ansi(out).strip()[-300:])

    # ---------- свои слова активации ----------
    def _wakeword_installed(self, force=False):
        """Версия установленного yam-пакета и признаки файлов; без API. Кэш — для частого опроса вкладки."""
        now = time.time()
        if not force and getattr(self, "_ww_info", None) and now - getattr(self, "_ww_info_at", 0.0) < 15:
            return dict(self._ww_info)
        out, _, _ = self.adb.shell(
            f"v=$(ls -d {WAKE_PKG}/* 2>/dev/null | sort -V | tail -n 1); "
            f"[ -n \"$v\" ] && {{ [ -L \"$v\" ] && basename \"$(readlink -f \"$v\")\" || basename \"$v\"; }}; "
            f"[ -x {WAKE_BIN} ] && echo @bin; [ -f {WAKE_SHARE}/S97wakeword ] && echo @script; "
            f"[ -f {WAKE_PID} ] && kill -0 $(cat {WAKE_PID}) 2>/dev/null && echo @pid; "
            f"[ -f {WAKE_AUTOSTART} ] && echo @auto; true", check=False)
        lines = [l.strip() for l in strip_ansi(out).splitlines() if l.strip()]
        version = next((l for l in lines if not l.startswith("@")), None)
        info = {"installed": bool(version or "@bin" in lines or "@script" in lines),
                "version": version, "bin": "@bin" in lines, "script": "@script" in lines,
                "pid": "@pid" in lines, "auto": "@auto" in lines}
        self._ww_info, self._ww_info_at = dict(info), now
        return info

    def _wakeword_supported(self):
        gl = getattr(self, "glagol", None)
        platform = (gl.platform if gl else None) or GLAGOL_PLATFORM
        return platform in WAKE_TARGETS, platform

    def _wakeword_base(self):
        """База HTTP API демона: adb forward на свободный порт, иначе прямой IP колонки."""
        if not getattr(self, "_ww_port", None):
            try:
                self._ww_port = self.adb.forward(WAKE_PORT)
            except (AdbError, OSError, subprocess.TimeoutExpired):
                self._ww_port = None
        if getattr(self, "_ww_port", None):
            return f"http://127.0.0.1:{self._ww_port}"
        gl = getattr(self, "glagol", None)
        ip = (gl.host if gl else None) or ""
        if not ip and self.adb.serial and ":" in self.adb.serial:
            ip = self.adb.serial.split(":", 1)[0]
        if not ip:
            raise AdbError("нет связи с колонкой для API слов активации")
        return f"http://{ip}:{WAKE_PORT}"

    def _wakeword_api(self, path, method="GET", body=None, timeout=60):
        """Запрос к API демона; возвращает (content-type, dict/bytes)."""
        if not str(path).startswith("/api/") or ".." in path:
            raise AdbError("плохой путь API слов активации")
        base = self._wakeword_base()
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, ctype = r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                msg = (json.loads(raw) or {}).get("error") or ""
            except ValueError:
                msg = raw.decode("utf-8", "replace")[:300]
            raise AdbError(msg.strip() or f"API слов активации: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            self._ww_port = None      # возможно, adb forward отвалился — в следующий раз создадим заново
            raise AdbError(f"API слов активации недоступен: {e}") from None
        if ctype.startswith("application/json"):
            try:
                return ctype, json.loads(raw or b"{}")
            except ValueError:
                raise AdbError("API слов активации вернул не JSON") from None
        return ctype, raw

    def wakeword_status(self):
        info = self._wakeword_installed()
        supported, platform = self._wakeword_supported()
        state, error = None, ""
        if info["installed"]:
            try:
                _, state = self._wakeword_api("/api/state", timeout=5)
            except AdbError as e:
                error = str(e)
        running = bool(state) or info["pid"]
        if not running:
            error = ""       # демон штатно остановлен — это не ошибка API
        audio = (state or {}).get("audio") or {}
        return {"installed": info["installed"], "version": info["version"], "running": running,
                "supported": supported, "platform": platform, "audio": audio, "autostart": info["auto"],
                "dry_run": bool(load_settings().get(WAKE_ACTIONS_KEY) or {}),
                "words": (state or {}).get("words") or [], "hits": (state or {}).get("hits") or [],
                "enroll": (state or {}).get("enroll"), "train": (state or {}).get("train") or {"active": False},
                "station": (state or {}).get("station", ""), "error": error, "port": WAKE_PORT,
                "support": WAKE_SUPPORT}

    def wakeword_start(self):
        info = self._wakeword_installed(force=True)
        if not info["installed"]:
            raise AdbError("пакет tangenx/wakeword не установлен")
        run = (f"exec {WAKE_BIN} run -data {WAKE_DATA} -negatives {WAKE_SHARE}/negatives.bin "
               f"-listen 127.0.0.1:{WAKE_PORT} >> {WAKE_LOG} 2>&1")
        out, err, rc = self.adb.shell(
            f"start-stop-daemon -K -p {WAKE_PID} 2>/dev/null; rm -f {WAKE_PID}; "
            f"start-stop-daemon -S -b -m -N 10 -p {WAKE_PID} -x /bin/sh -- -c {shlex.quote(run)} 2>&1",
            check=False, timeout=30)
        if rc != 0:
            raise AdbError(strip_ansi(out or err).strip()[-400:] or "не удалось запустить демон слов активации")
        self._ww_info = None
        for _ in range(20):
            time.sleep(0.3)
            st = self.wakeword_status()
            if st["running"]:
                return st
        raise AdbError("демон слов активации не запустился")

    def wakeword_stop(self):
        out, err, rc = self.adb.shell(f"sh {WAKE_SHARE}/S97wakeword stop 2>&1", check=False, timeout=20)
        if rc != 0:
            raise AdbError(strip_ansi(out or err).strip()[-400:] or "не удалось остановить демон слов активации")
        self._ww_port = None
        self._ww_info = None
        return self.wakeword_status()

    def wakeword_autostart(self, on):
        """Включить/выключить запуск демона хуком панели после перезагрузки."""
        if on and not self._wakeword_installed(force=True)["installed"]:
            raise AdbError("пакет tangenx/wakeword не установлен")
        if on:
            self.adb.shell(f"mkdir -p {WEB_DIR} && touch {WAKE_AUTOSTART}", check=False)
            self._ensure_hook()
        else:
            self.adb.shell(f"rm -f {WAKE_AUTOSTART}", check=False)
            self._drop_hook_if_unused()
        self._ww_info = None
        return self.wakeword_status()

    def wakeword_install(self):
        """Поставить/обновить yam-пакет tangenx/wakeword из реестра."""
        self.yam2_install_pkg("tangenx/wakeword", yes=True)
        self._ww_info = None
        self._ww_port = None
        self._wakeword_apply_dry_run()
        return self.wakeword_status()

    def wakeword_proxy(self, method, path, body=None, timeout=90):
        """Прокси к API демона для вкладки: JSON и WAV отдаются как есть."""
        method = str(method or "GET").upper()
        if method not in ("GET", "POST", "DELETE"):
            raise AdbError("метод не поддерживается")
        ctype, data = self._wakeword_api(path, method, body, timeout=timeout)
        return data, ctype

    # ---------- маршрутизация слов по действиям ----------
    def _wakeword_settings(self):
        try:
            raw = self.adb.read_bytes(WAKE_SETTINGS) or b"{}"
            if not isinstance(raw, (str, bytes, bytearray)):
                return {}
            return json.loads(raw)
        except (AdbError, OSError, ValueError):
            return {}

    def _wakeword_settings_write(self, settings):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "settings.json"
            write_unix(f, json.dumps(settings, ensure_ascii=False, indent=1))
            self.adb.shell(f"mkdir -p {WAKE_DATA}", check=False)
            self.adb.push(f, WAKE_SETTINGS + ".new")
        self.adb.shell(f"mv {WAKE_SETTINGS}.new {WAKE_SETTINGS}", check=False)

    def _wakeword_restart(self):
        self.adb.shell(f"sh {WAKE_SHARE}/S97wakeword restart 2>&1", check=False, timeout=30)
        self._ww_port = None
        self._ww_info = None
        self._ww_last_hit = None
        time.sleep(1)

    def _wakeword_apply_dry_run(self):
        """В режиме маршрутизации демон не должен сам будить Алису — это делает роутер."""
        acts = load_settings().get(WAKE_ACTIONS_KEY) or {}
        want = bool(acts)
        st = self._wakeword_settings()
        if bool(st.get("dry_run")) != want:
            st["dry_run"] = want
            self._wakeword_settings_write(st)
            self._wakeword_restart()
        return want

    def wakeword_actions(self):
        s = load_settings()
        return {"actions": s.get(WAKE_ACTIONS_KEY) or {},
                "scenes": self.scenes()["scenes"],
                "dry_run": bool(self._wakeword_settings().get("dry_run"))}

    def wakeword_actions_set(self, word, action, scene=None):
        word = str(word or "").strip()
        if not word:
            raise AdbError("пустое слово")
        action = str(action or "alice")
        if action not in ("alice", "assistant", "scene", "none"):
            raise AdbError("неизвестное действие")
        scene = str(scene or "").strip() if action == "scene" else ""
        if action == "scene" and scene not in {x["id"] for x in self.scenes()["scenes"]}:
            raise AdbError("нет такого сценария")
        s = load_settings()
        acts = s.setdefault(WAKE_ACTIONS_KEY, {})
        if action == "alice":
            acts.pop(word, None)
        else:
            acts[word] = {"type": action, "scene": scene}
        save_settings(s)
        self._wakeword_apply_dry_run()
        return self.wakeword_actions()

    def wakeword_router_start(self):
        if getattr(self, "_ww_router", None) and self._ww_router.is_alive():
            return
        self._ww_router_stop = threading.Event()
        self._ww_router = threading.Thread(target=self._wakeword_router_loop, daemon=True, name="wakeword-router")
        self._ww_router.start()

    def _wakeword_fire(self, hit, acts):
        word = str(hit.get("word") or "")
        act = acts.get(word) or {"type": "alice"}
        kind = act.get("type") or "alice"
        if kind == "none":
            return
        if kind == "scene":
            sid = act.get("scene")
            if sid:
                try:
                    self.scene_run(sid)
                    JOURNAL.add(f"слова активации: «{hit.get('name') or word}» → сценарий {sid}", "info")
                except (AdbError, subprocess.TimeoutExpired) as e:
                    JOURNAL.add(f"слова активации: сценарий {sid} не запустился: {e}", "warn")
            return
        if kind == "assistant":
            try:
                self.assistant_command({"route": True})
                JOURNAL.add(f"слова активации: «{hit.get('name') or word}» → ассистент", "info")
            except (AdbError, subprocess.TimeoutExpired) as e:
                JOURNAL.add(f"слова активации: ассистент не принял слово: {e}", "warn")
        try:
            self._wakeword_api("/api/listen", "POST", timeout=3)
            JOURNAL.add(f"слова активации: «{hit.get('name') or word}» → Алиса слушает", "info")
        except AdbError as e:
            JOURNAL.add(f"слова активации: не удалось начать слушать: {e}", "warn")

    def _wakeword_router_loop(self):
        while not self._ww_router_stop.is_set():
            try:
                acts = load_settings().get(WAKE_ACTIONS_KEY) or {}
                if not acts:
                    self._ww_last_hit = None
                    self._ww_router_stop.wait(2)
                    continue
                st = self.wakeword_status()
                if not st["running"]:
                    self._ww_router_stop.wait(2)
                    continue
                _, state = self._wakeword_api("/api/state", timeout=3)
                hits = (state or {}).get("hits") or []
                ids = [int(h.get("id") or 0) for h in hits]
                if self._ww_last_hit is None:
                    self._ww_last_hit = max(ids or [0])
                else:
                    if ids and max(ids) < self._ww_last_hit:
                        self._ww_last_hit = 0   # демон перезапускался: новые события ещё не обработаны
                    for h in sorted(hits, key=lambda x: int(x.get("id") or 0)):
                        hid = int(h.get("id") or 0)
                        if hid <= self._ww_last_hit:
                            continue
                        self._ww_last_hit = hid
                        try:
                            self._wakeword_fire(h, acts)
                        except Exception as e:  # noqa: BLE001
                            JOURNAL.add(f"слова активации: действие не выполнено: {e}", "warn")
                self._ww_router_stop.wait(0.3)
            except Exception as e:  # noqa: BLE001
                JOURNAL.add(f"слова активации: роутер: {e}", "warn")
                self._ww_router_stop.wait(2)

    def footprint(self):
        """Всё постоянное, что панель ставит на колонку. Временное (/tmp) исчезает само после перезагрузки."""
        q = lambda path: f"[ -e {path} ] && echo 1 || echo 0"
        cmd = "; ".join([
            f"grep -q ' {BOOT_TARGET} ' /proc/mounts && echo 1 || echo 0", q(BOOT_CUSTOM),
            f"grep -q ' {SOUND_TARGET} ' /proc/mounts && echo 1 || echo 0", q(SOUND_CUSTOM),
            q(BOOT_ALWAYS), q(YAM_AUTOSTART), q(ALARMD),
            f"echo $(cat {ALARM_CONF} 2>/dev/null | grep -c .)/$(grep -c '^S|' {SCENES_CONF} 2>/dev/null)",
            q(BOOT_ORIG), q(SOUND_ORIG), q(f"{ROOTKEEPER}.webpanel.bak"),
            f"{hook_active()} && echo 1 || echo 0",
            f"[ -d {WEB_DIR} ] && du -sk {WEB_DIR} | cut -f1 || echo 0",
            f"[ -d {ALARM_DIR} ] && du -sk {ALARM_DIR} | cut -f1 || echo 0",
            q(YAMD),
            f"grep -q ' {HOSTS_TARGET} ' /proc/mounts && echo 1 || echo 0", q(HOSTS_CUSTOM),
            q(ASSISTD), f"[ -d {ASSIST_DIR} ] && du -sk {ASSIST_DIR} | cut -f1 || echo 0",
            f"find {OVR_DIR}/sounds -type f 2>/dev/null | wc -l",
            f"[ -d {OVR_DIR}/sounds ] && du -sk {OVR_DIR}/sounds | cut -f1 || echo 0",
            f"find {OVR_DIR}/ledpatterns -type f 2>/dev/null | wc -l",
            f"find {MY_ANIM_DIR} -type f 2>/dev/null | wc -l",
            f"find {YAM2_SVC_DIR}/boot -type f 2>/dev/null | wc -l", q(YAM2), q(BTOUT_MAC), q(AGENT_BIN), q(BT_OFF),
            q(VIZD), f"[ -d {VIZ_DIR} ] && du -sk {VIZ_DIR} | cut -f1 || echo 0", q(WAKE_AUTOSTART)])
        v = self.adb.shell(cmd, check=False)[0].split()
        v = (v + ["0"] * 31)[:31]
        b = [x == "1" for x in v]
        n = lambda x: int(x) if x.isdigit() else 0
        items = [
            {"key": "boot", "name": "Своя заставка часов", "on": b[0] or b[1]},
            {"key": "sound", "name": "Свой звук запуска", "on": b[2] or b[3]},
            {"key": "sounds", "name": "Свои системные звуки", "on": n(v[19]) > 0, "detail": f"заменено: {n(v[19])}",
             "size_kb": n(v[20])},
            {"key": "anims", "name": "Свои анимации кольца и часов", "on": n(v[21]) + n(v[22]) > 0,
             "detail": f"заменено встроенных: {n(v[21])}, своих: {n(v[22])}"},
            {"key": "always", "name": "Заставка при каждой загрузке", "on": b[4]},
            {"key": "yamd", "name": "Автозапуск yamd", "on": b[5]},
            {"key": "yam2", "name": "Автозапуск служб yam 2", "on": n(v[23]) > 0, "detail": f"служб: {n(v[23])}"},
            {"key": "btout", "name": "Звук через Bluetooth-колонку", "on": b[25], "detail": "сторож и своя /etc/asound.conf, пока колонка подключена"},
            {"key": "agent", "name": "Агент панели (защищённое соединение)", "on": b[26],
             "detail": f"порт {agentlink.PORT}, вход по токену"},
            {"key": "btoff", "name": "Bluetooth выключен", "on": b[27], "detail": "и после перезагрузки"},
            {"key": "privacy", "name": "Блокировка аналитики", "on": b[15] or b[16], "detail": "свой /etc/hosts"},
            {"key": "assistant", "name": "Голосовой помощник", "on": b[17], "size_kb": n(v[18])},
            {"key": "viz", "name": "Живой спектр с колонки", "on": b[28], "size_kb": n(v[29]),
             "detail": "экспериментально: захват звука усилителя, не микрофона"},
            {"key": "wakeword", "name": "Слова активации (автозапуск)", "on": b[30],
             "detail": "демон tangenx/wakeword, стартует хуком панели"},
            {"key": "alarm", "name": "Будильник и сценарии", "on": b[6],
             "detail": f"будильников: {n(v[7].split('/')[0])}, сценариев: {n((v[7].split('/') + ['0'])[1])}", "size_kb": n(v[13])},
            {"key": "backup", "name": "Копии оригиналов", "on": b[8] or b[9] or b[10],
             "detail": "заставка, звук запуска, rootkeeper.sh — на случай отката"},
            {"key": "hook", "name": "Автозапуск: строка в rootkeeper.sh или скрипт в /etc/init.d", "on": b[11]},
        ]
        return {"items": items, "dir": WEB_DIR, "size_kb": n(v[12]), "any": any(i["on"] for i in items),
                "yam_installed": b[14] or b[24]}

    def footprint_reset(self):
        """Возвращает колонку к исходному: снимает подмены, гасит наши демоны, убирает строку из rootkeeper.sh
        и удаляет /data/quasar/webpanel. yam не трогаем — это отдельный проект со своим удалением.
        Всё — через adb: агента это тоже удаляет."""
        self._agent_forget()
        with self.adb.only_adb():
            return self._footprint_reset()

    def _footprint_reset(self):
        self.adb.shell(
            f"[ -f {BT_OFF} ] && {bt_power_sh(True)}; "            f"start-stop-daemon -K -q -p {AGENT_SD} 2>/dev/null; rm -f {AGENT_SD}; "
            f"grep -q ringing {ALARM_STATE} 2>/dev/null && echo stop > {ALARM_CMD} && sleep 3; "
            f"start-stop-daemon -K -p {ALARM_SD} 2>/dev/null; start-stop-daemon -K -p {ASSIST_SD} 2>/dev/null; "
            f"start-stop-daemon -K -p {VIZ_SD} 2>/dev/null; start-stop-daemon -K -p {BTOUT_SD} 2>/dev/null; "
            f"[ -x {WAKE_SHARE}/S97wakeword ] && sh {WAKE_SHARE}/S97wakeword stop >/dev/null 2>&1; "
            f"p=$(cat {BTOUT_FWD_SD} 2>/dev/null) && {{ {YAM2_KILL}; }}; "
            f"{LED_STOP_SH}; "   # LED-панель Станции 2: фоновый led-play
            f"grep -q ' /etc/asound.conf ' /proc/mounts && grep -q '{BTOUT_MARK}' /etc/asound.conf && "
            f"{{ umount /etc/asound.conf || umount -l /etc/asound.conf; ( {QUASAR}/q restart >/dev/null 2>&1 & ); }}; "
            # службы yam 2 запускала панель — гасим их группы процессов (сами пакеты остаются в yam)
            f"for f in {YAM2_SVC_PID}*.pid; do [ -f \"$f\" ] && p=$(cat \"$f\") && kill -0 \"$p\" 2>/dev/null && {{ {YAM2_KILL}; }}; "
            f"rm -f \"$f\"; done 2>/dev/null; "
            f"killall aplay 2>/dev/null; "
            f"grep -q ' {BOOT_TARGET} ' /proc/mounts && {{ umount {BOOT_TARGET} || umount -l {BOOT_TARGET}; }}; "
            f"grep -q ' {SOUND_TARGET} ' /proc/mounts && {{ umount {SOUND_TARGET} || umount -l {SOUND_TARGET}; }}; "
            f"grep -q ' {HOSTS_TARGET} ' /proc/mounts && {{ umount {HOSTS_TARGET} || umount -l {HOSTS_TARGET}; }}; "
            f"[ -d {OVR_DIR} ] && (cd {OVR_DIR} && find . -type f) | while read -r f; do t=\"{QUASAR}/${{f#./}}\"; "
            f"grep -q \" $t \" /proc/mounts && {{ umount \"$t\" || umount -l \"$t\"; }}; done; "
            f"{hook_drop()}; true", check=False)
        # Копию rootkeeper.sh удаляем, только если без нашей строки файл совпал с ней: значит, других правок не было.
        out, _, _ = self.adb.shell(
            f"b={ROOTKEEPER}.webpanel.bak; if [ -f $b ]; then "
            f"if [ \"$(md5sum < {ROOTKEEPER})\" = \"$(md5sum < $b)\" ]; then rm -f $b; echo same; else echo differs; fi; fi; "
            f"rm -rf {WEB_DIR}; rm -f /tmp/webpanel_alarm* {ALARM_SD} /tmp/webpanel_assistant* /tmp/webpanel_viz*; true", check=False)
        self.ring_reset()
        res = self.footprint()
        if "differs" in out:
            res["note"] = (f"rootkeeper.sh после нашей строки менялся кем-то ещё, поэтому копия оригинала "
                           f"оставлена: {ROOTKEEPER}.webpanel.bak")
        return res

    # ---------- агент: защищённый канал ----------
    AGENT_ASSET = "yapanel-agent-linux-arm64"

    def _agent_host(self):
        serial = self.adb.serial or ""
        return serial.rpartition(":")[0] if ":" in serial else None

    @staticmethod
    def _agent_store():
        try:
            return json.loads(AGENT_FILE.read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _agent_save(store):
        tmp = AGENT_FILE.with_name(AGENT_FILE.name + ".tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1))
        try:
            os.chmod(tmp, 0o600)        # токен агента — как токен Яндекса: только владельцу
        except OSError:
            pass
        os.replace(tmp, AGENT_FILE)

    def agent_attach(self):
        """При запуске: для этой колонки есть свой токен — работаем через агента."""
        host, entry = self._agent_host(), self._agent_store().get(self.adb.serial or "")
        if host and entry:
            try:
                self.adb.agent = agentlink.AgentLink(host, entry["token"], entry["cert"])
            except (KeyError, ssl.SSLError, ValueError) as e:
                JOURNAL.add(f"агент: сохранённые ключи не подходят ({e})", "warn")

    def _agent_forget(self):
        self.adb.agent, self.adb.agent_ok = None, False
        store = self._agent_store()
        if store.pop(self.adb.serial or "", None) is not None:
            self._agent_save(store)

    def agent_status(self):
        out, _, _ = self.adb.shell(
            f"[ -x {AGENT_BIN} ] && echo @bin; [ -s {AGENT_TOKENS} ] && echo @tokens; "
            f"p=$(cat {AGENT_SD} 2>/dev/null) && kill -0 $p 2>/dev/null && echo @run; {hook_active()} && echo @hook; true",
            check=False)
        flags = set(strip_ansi(out).split())
        res = {"installed": "@bin" in flags, "running": "@run" in flags, "boot": "@hook" in flags,
               "paired": bool(self.adb.agent), "active": bool(self.adb.agent and self.adb.agent_ok),
               "network": bool(self._agent_host()), "port": agentlink.PORT, "panel_version": VERSION, "version": None}
        if self.adb.agent:
            try:
                ping = self.adb.agent.ping()
                res["version"], res["active"] = ping.get("version"), True
                res["outdated"] = self._agent_outdated(ping)
            except (agentlink.AgentError, TimeoutError) as e:
                res["error"] = str(e)
        res.setdefault("outdated", False)
        return res

    def _agent_outdated(self, ping):
        """Другая версия — или та же, но своя сборка отличается (меняли исходники агента)."""
        if ping.get("version") != VERSION:
            return True
        try:
            local = self._device_binary(AGENT_SRC, "yapanel-agent", self.AGENT_ASSET, "агента", download=False)
        except AdbError:
            return False
        return bool(ping.get("build")) and hashlib.sha256(local.read_bytes()).hexdigest()[:16] != ping["build"]

    def agent_install(self):
        """Ставит или обновляет агента — только через adb — и подключает эту панель своим токеном."""
        host = self._agent_host()
        if not host:
            raise AdbError("агент работает по сети: подключите колонку по адресу, а не по USB")
        store = self._agent_store()
        entry = store.get(self.adb.serial) or {}
        token = entry.get("token") or secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        name = re.sub(r"[^\w.-]", "_", f"{platform.node() or 'panel'}-{hashlib.sha256(str(DATA_DIR).encode()).hexdigest()[:6]}")
        with self.adb.only_adb():
            arch = strip_ansi(self.adb.shell("uname -m", check=False)[0]).strip()
            if arch not in ("aarch64", "arm64"):
                raise AdbError(f"агент пока собирается только для arm64, а у этой колонки {arch or 'неизвестно что'}")
            binary = self._device_binary(AGENT_SRC, "yapanel-agent", self.AGENT_ASSET, "агента")
            self.adb.shell(f"mkdir -p {AGENT_DIR} && chmod 700 {AGENT_DIR}; "
                           f"start-stop-daemon -K -q -p {AGENT_SD} 2>/dev/null; rm -f {AGENT_SD}; true", check=False)
            self.adb.push(binary, AGENT_BIN + ".new", timeout=300)
            self.adb.shell(f"chmod 755 {AGENT_BIN}.new && mv {AGENT_BIN}.new {AGENT_BIN}; touch {AGENT_TOKENS}; "
                           f"chmod 600 {AGENT_TOKENS}; grep -q '^{digest} ' {AGENT_TOKENS} || echo '{digest} {name}' >> {AGENT_TOKENS}; "
                           f"start-stop-daemon -S -b -m -p {AGENT_SD} -x /bin/sh -- -c {shlex.quote(AGENT_RUN)}")
            for _ in range(40):          # сертификат агент создаёт при первом запуске
                if "@cert" in self.adb.shell(f"[ -s {AGENT_CERT} ] && echo @cert; true", check=False)[0]:
                    break
                time.sleep(0.25)
            cert = self.adb.read_bytes(AGENT_CERT).decode("ascii", "replace")
        link = agentlink.AgentLink(host, token, cert)
        err = None
        for _ in range(20):
            try:
                link.ping()
                break
            except (agentlink.AgentError, TimeoutError) as e:
                err = e
                time.sleep(0.25)
        else:
            raise AdbError(f"агент запущен, но не отвечает по сети: {err}. Работаем через adb")
        store[self.adb.serial] = {"token": token, "cert": cert}
        self._agent_save(store)
        self.adb.agent, self.adb.agent_ok, self.adb._agent_retry = link, True, 0.0
        JOURNAL.add(f"агент {VERSION} установлен — команды идут по защищённому каналу")
        try:
            self._ensure_hook()
        except AdbError as e:
            JOURNAL.add(f"агент: автозапуск не подключён ({e}) — после перезагрузки колонки панель запустит его сама", "warn")
        return self.agent_status()

    def agent_remove(self):
        self._agent_forget()
        with self.adb.only_adb():
            self.adb.shell(f"start-stop-daemon -K -q -p {AGENT_SD} 2>/dev/null; rm -f {AGENT_SD}; rm -rf {AGENT_DIR}; true",
                           check=False)
            self._drop_hook_if_unused()
        JOURNAL.add("агент удалён — команды снова идут через adb")
        return self.agent_status()

    def agent_keeper(self, conn):
        """Раз в минуту: агент подключён, но молчит, а adb работает — запускаем его (колонка перезагрузилась
        без хука); версия не совпала с панелью — обновляем (один раз за запуск)."""
        updated = False
        while True:
            time.sleep(60 if updated else 20)
            if not self.adb.agent or not conn.online:
                continue
            try:
                if not self.adb.agent_ok:
                    with self.adb.only_adb():
                        self.adb.shell(f"[ -x {AGENT_BIN} ] && [ -s {AGENT_TOKENS} ] && ! kill -0 $(cat {AGENT_SD} 2>/dev/null) "
                                       f"2>/dev/null && start-stop-daemon -S -b -m -p {AGENT_SD} -x /bin/sh -- -c "
                                       f"{shlex.quote(AGENT_RUN)}; true", check=False)
                    self.adb._agent_retry = 0.0
                    continue
                if not updated and self._agent_outdated(self.adb.agent.ping()):
                    updated = True
                    self.agent_install()
                updated = True
            except (AdbError, agentlink.AgentError, OSError) as e:
                updated = True
                JOURNAL.add(f"агент: {e}", "warn")

    # ---------- резервная копия ----------
    # Что берём из /data/quasar/webpanel: только своё. Копии оригиналов не берём — на другой прошивке они
    # были бы чужими; при восстановлении они снимаются заново с этой колонки. Хук и скрипт будильника — тоже
    # не берём: ставим текущую версию из панели.
    BACKUP_FILES = re.compile(r"(boot\.binscled\.zst|boot_sound\.wav|boot_every_time|yamd_autostart|hosts|"
                              r"alarm/alarms\.conf|alarm/scenes\.conf|alarm/default\.wav|alarm/[su]_[0-9a-f]{10}\.wav|"
                              r"override/sounds/(?:[\w-][\w.-]*/)*[\w-][\w.-]*|override/ledpatterns/(?:clock|oknix)/[\w-][\w.-]*|"
                              r"anims/(?:ring/[\w-]{1,40}\.led|clock/[\w-]{1,40}\.binscled))")
    BACKUP_LIMIT = 200 * 1024 * 1024

    def backup(self, ui):
        with tempfile.TemporaryDirectory() as td:
            out, _, _ = self.adb.shell(f"cd {WEB_DIR} 2>/dev/null && find . -type f; true", check=False)
            names = [l.strip()[2:] for l in strip_ansi(out).splitlines() if l.strip().startswith("./")]
            names = [n for n in names if self.BACKUP_FILES.fullmatch(n)]
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for n in names:
                    local = Path(td) / n.replace("/", "_")
                    self.adb.pull(f"{WEB_DIR}/{n}", local)
                    tar.add(local, arcname="webpanel/" + n)
                manifest = {
                    "format": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "firmware": self.version().get("quasar_version"),
                    "items": [i["key"] for i in self.footprint()["items"] if i["on"] and i["key"] not in ("hook", "backup")],
                    "ui": {k: str(v) for k, v in (ui or {}).items() if re.fullmatch(r"(ui|ov|ch|snd)\.[a-z.]{1,20}", k)},
                }
                data = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mtime = len(data), int(time.time())
                tar.addfile(info, io.BytesIO(data))
        name = "stantsiya-backup-" + time.strftime("%Y%m%d-%H%M") + ".tar.gz"
        return name, buf.getvalue()

    def backup_restore(self, body):
        """Делает колонку такой, как в копии: сначала полный сброс того, что ставила панель, потом установка."""
        if len(body) > self.BACKUP_LIMIT:
            raise AdbError("файл слишком большой")
        files, manifest = {}, None
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
                for m in tar.getmembers():
                    if not m.isfile():
                        continue
                    if m.name == "manifest.json":
                        manifest = json.loads(tar.extractfile(m).read())
                    elif m.name.startswith("webpanel/") and self.BACKUP_FILES.fullmatch(m.name[9:]):
                        files[m.name[9:]] = tar.extractfile(m).read()
        except (tarfile.TarError, OSError, ValueError, EOFError) as e:
            raise AdbError(f"это не резервная копия панели: {e}")
        if not manifest or manifest.get("format") != 1:
            raise AdbError("это не резервная копия панели (нет manifest.json)")
        report = []
        self.footprint_reset()
        boot = self.boot_status()
        if "boot.binscled.zst" in files:
            if boot["supported"]:
                self._install_override(files["boot.binscled.zst"], BOOT_CUSTOM, BOOT_TARGET, BOOT_ORIG)
                report.append("заставка часов")
            else:
                report.append("заставка пропущена: на этой прошивке её не подменить")
        if "boot_sound.wav" in files:
            if boot.get("sound_supported"):
                self._install_override(files["boot_sound.wav"], SOUND_CUSTOM, SOUND_TARGET, SOUND_ORIG)
                report.append("звук запуска")
            else:
                report.append("звук запуска пропущен: на этой прошивке его не подменить")
        sounds = {n[len("override/sounds/"):]: data for n, data in files.items() if n.startswith("override/sounds/")}
        if sounds:
            have, skipped = set(self.sounds()), 0
            for name, data in sounds.items():
                if name in have:   # на другой прошивке такого звука может не быть
                    self._install_override(data, f"{OVR_DIR}/sounds/{name}", f"{SOUNDS_DIR}/{name}")
                else:
                    skipped += 1
            report.append(f"свои системные звуки ({len(sounds) - skipped})" + (f", пропущено {skipped}: на этой прошивке их нет" if skipped else ""))
        mine = {n: data for n, data in files.items() if n.startswith("anims/")}
        if mine:
            with tempfile.TemporaryDirectory() as td:
                self.adb.shell(f"mkdir -p {MY_ANIM_DIR}/ring {MY_ANIM_DIR}/clock")
                for n, data in mine.items():
                    f = Path(td) / "a"
                    f.write_bytes(data)
                    self.adb.push(f, f"{WEB_DIR}/{n}")
            report.append(f"свои анимации ({len(mine)})")
        anims = {n[len("override/ledpatterns/"):]: data for n, data in files.items() if n.startswith("override/ledpatterns/")}
        if anims:
            have, done = self.patterns(), 0   # my/… сюда не попадут: в override только встроенные
            for rel, data in anims.items():
                folder, name = rel.split("/")
                kind = next(k for k, d in PATTERNS.items() if posixpath.basename(d) == folder)
                if name in have[kind]:
                    self._install_override(data, f"{OVR_DIR}/ledpatterns/{rel}", f"{PATTERNS[kind]}/{name}")
                    done += 1
            report.append(f"свои анимации ({done})" + (f", пропущено {len(anims) - done}: на этой прошивке их нет" if done < len(anims) else ""))
        if "boot_every_time" in files:
            self.boot_always(True)
            report.append("заставка при каждой загрузке")
        if "hosts" in files:
            # файл из копии не ставим: в нём имя хоста той колонки — собираем заново из /etc/hosts этой
            self.privacy_set(True)
            report.append("блокировка аналитики")
        if "yamd_autostart" in files:
            if self.yam_status().get("installed"):
                self.yam_autostart(True)
                report.append("автозапуск yamd")
            else:
                report.append("автозапуск yamd пропущен: yam не установлен")
        if "alarm/alarms.conf" in files:
            with tempfile.TemporaryDirectory() as td:
                self.adb.shell(f"mkdir -p {ALARM_DIR}")
                for n, data in files.items():
                    if n.startswith("alarm/"):
                        f = Path(td) / n[6:]
                        f.write_bytes(data)
                        self.adb.push(f, f"{WEB_DIR}/{n}")
            # скрипт — текущей версии, часовой пояс — этого компьютера (их ставит alarm_install)
            n = len(self.alarm_install()["alarms"])
            report.append(f"будильник ({n})")
        return {"restored": report, "ui": manifest.get("ui") or {}, "created": manifest.get("created"),
                "firmware": manifest.get("firmware"), "footprint": self.footprint()}

    # ---------- будильник ----------
    ALARM_FIELDS = ("id", "on", "time", "days", "mode", "query", "sound", "sound_name", "volume", "ramp",
                    "duration", "snooze", "label")

    def alarms(self):
        parts = self.adb.shell(
            f"cat {ALARM_CONF} 2>/dev/null; echo '<<<'; cat {ALARM_STATE} 2>/dev/null; echo '<<<'; "
            f"tail -n 30 {ALARM_DIR}/alarm.log 2>/dev/null; echo '<<<'; "
            f"[ -f {ALARM_DIR}/localtime ] && export TZ=:{ALARM_DIR}/localtime; date '+%s %H:%M %Z'; echo '<<<'; "
            f"cat {ALARM_DIR}/tzname 2>/dev/null; echo '<<<'; cat {ALARM_DIR}/snooze 2>/dev/null; echo '<<<'; "
            f"p=$(cat {ALARM_SD} 2>/dev/null) && kill -0 $p 2>/dev/null && echo run; echo '<<<'; "
            f"[ -f {ALARMD} ] && md5sum {ALARMD} && du -sk {ALARM_DIR}; true", check=False)[0].split("<<<")
        parts = [x.strip("\r\n") for x in parts] + [""] * 8
        inst = parts[7].split()
        items = []
        for line in parts[0].splitlines():
            f = line.rstrip("\r").split("|", len(self.ALARM_FIELDS) - 1)
            if len(f) != len(self.ALARM_FIELDS):
                continue
            a = dict(zip(self.ALARM_FIELDS, f))
            a["on"] = a["on"] == "1"
            for k in ("volume", "ramp", "duration", "snooze"):
                a[k] = int(a[k]) if a[k].isdigit() else 0
            items.append(a)
        st = parts[1].strip().split("|")
        state = {"ringing": st[0] == "ringing", "id": st[1] if len(st) > 1 else None,
                 "since": int(st[2]) if len(st) > 2 and st[2].isdigit() else None, "via": st[3] if len(st) > 3 else None}
        t = parts[3].strip().split(" ", 2)
        sn = parts[5].strip().split("|")
        return {"alarms": items, "state": state, "log": [l for l in parts[2].splitlines() if l.strip()],
                "device_time": {"epoch": int(t[0]) if t and t[0].isdigit() else None,
                                "hm": t[1] if len(t) > 1 else "", "zone": t[2] if len(t) > 2 else ""},
                "tz": parts[4].strip() or None, "pc_tz": self._pc_tz()[0],
                "snooze": {"at": int(sn[0]), "id": sn[1]} if len(sn) == 2 and sn[0].isdigit() else None,
                "running": parts[6].strip() == "run", "ffmpeg": bool(self.ffmpeg),
                "installed": bool(inst), "dir": ALARM_DIR,
                "outdated": bool(inst) and inst[0] != hashlib.md5(alarm_script()).hexdigest(),
                "size_kb": int(inst[2]) if len(inst) > 2 and inst[2].isdigit() else None}

    @staticmethod
    def _pc_tz():
        """Часовой пояс этого компьютера: колонка живёт в UTC, а время будильника — местное."""
        lt = Path("/etc/localtime")
        try:
            real = lt.resolve()
            name = str(real).split("zoneinfo/", 1)[1] if "zoneinfo/" in str(real) else None
            return name or time.strftime("%Z"), real.read_bytes()
        except OSError:
            return None, None

    def _alarm_wav(self, data, dev_name):
        if not self.ffmpeg:
            raise AdbError("для звуков будильника нужен ffmpeg на компьютере")
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "a.wav"
            # не длиннее 5 минут: звук всё равно играет по кругу, а место в /data не бесконечное
            r = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn", "-t", "300",
                                "-ac", "2", "-ar", "48000", "-sample_fmt", "s16", "-y", str(dst)],
                               input=data, capture_output=True, timeout=300)
            if r.returncode != 0:
                raise AdbError("ffmpeg: " + r.stderr.decode("utf-8", "replace").strip()[-300:])
            self.adb.shell(f"mkdir -p {ALARM_DIR}")
            self.adb.push(dst, f"{ALARM_DIR}/{dev_name}")

    def alarm_sound_upload(self, filename, body):
        if not body:
            raise AdbError("пустой файл")
        name = "u_" + hashlib.sha1(body).hexdigest()[:10] + ".wav"
        self._alarm_wav(body, name)
        return {"sound": name, "sound_name": Path(filename).stem[:60] or "свой звук"}

    def alarms_save(self, data):
        items = data.get("alarms")
        if not isinstance(items, list) or len(items) > 30:
            raise AdbError("неверный список будильников")
        clean = lambda v, n: re.sub(r"[|\r\n]", " ", str(v or "")).strip()[:n]
        lines, used, sys_needed = [], set(), {}
        for a in items:
            aid = re.sub(r"[^a-z0-9]", "", str(a.get("id") or ""))[:12] or os.urandom(4).hex()
            tm = str(a.get("time", ""))
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tm):
                raise AdbError(f"неверное время: {tm}")
            days = "".join(sorted(set(re.sub(r"[^1-7]", "", str(a.get("days", ""))))))
            mode = a.get("mode") if a.get("mode") in ALARM_MODES else "sound"
            query = clean(a.get("query"), 200)
            if mode in ("music", "text") and not query:
                raise AdbError("укажите, что попросить у Алисы")
            dev = self._alarm_sound(a.get("sound"), sys_needed)
            used.add(dev)
            vol = max(1, min(10, int(a.get("volume") or 5)))
            ramp = max(0, min(1800, int(a.get("ramp") or 0)))
            dur = max(0, min(180, int(a.get("duration") or 0)))
            if mode == "sound" and not dur:
                dur = 30          # свой звук сам не замолчит
            snz = max(0, min(60, int(a.get("snooze") or 0)))
            lines.append("|".join([aid, "1" if a.get("on", True) else "0", tm, days, mode, query, dev,
                                   clean(a.get("sound_name"), 60), str(vol), str(ramp), str(dur), str(snz),
                                   clean(a.get("label"), 40)]))
        self._alarm_sounds_ready(sys_needed)
        self._alarm_write(ALARM_CONF, lines, f"rm -f {ALARM_DIR}/last_fired.t {ALARM_DIR}/snooze")
        return self.alarms()

    # ---------- общее для будильников и сценариев ----------
    @staticmethod
    def _alarm_sound(snd, sys_needed):
        """Звук будильника или сценария → имя файла на колонке. Системный (sys:путь) перекодируется в WAV один раз;
        свой уже загружен (u_*.wav)."""
        snd = str(snd or "")
        if snd.startswith("sys:"):
            path = snd[4:]
            if path == ALARM_DEFAULT_SOUND:
                return "default.wav"
            dev = "s_" + hashlib.sha1(path.encode()).hexdigest()[:10] + ".wav"
            sys_needed[dev] = path
            return dev
        return snd if re.fullmatch(r"u_[0-9a-f]{10}\.wav", snd) else "default.wav"

    def _alarm_default_path(self):
        """Штатный звук для запасного default.wav. common/timer.wav есть не во всех прошивках (на Мини 2 его нет) —
        тогда другой похожий из звуков колонки."""
        names = self.sounds()
        if ALARM_DEFAULT_SOUND in names:
            return ALARM_DEFAULT_SOUND
        audio = [n for n in names if n.endswith((".wav", ".mp3", ".opus", ".ogg"))]
        for key in ("timer", "alarm", "ring", "notification", "common/"):
            hit = next((n for n in sorted(audio) if key in n), None)
            if hit:
                return hit
        if not audio:
            raise AdbError("на колонке нет системных звуков для будильника — загрузите свой")
        return sorted(audio)[0]

    def _alarm_sounds_ready(self, sys_needed):
        # не ls: у busybox он раскрашивает имена даже в не-терминал
        have = set(self.adb.shell(f"mkdir -p {ALARM_DIR}; for f in {ALARM_DIR}/*; do echo \"${{f##*/}}\"; done",
                                  check=False)[0].split())
        # запасной звук — штатный звук таймера: на него переключаемся, если чего-то не хватает
        if "default.wav" not in have:
            self._alarm_wav(self.adb.read_bytes(f"{SOUNDS_DIR}/{self._alarm_default_path()}"), "default.wav")
        for dev, path in sys_needed.items():
            if dev not in have:
                if path not in self.sounds():
                    raise AdbError(f"нет такого звука: {path}")
                self._alarm_wav(self.adb.read_bytes(f"{SOUNDS_DIR}/{path}"), dev)

    def _alarm_script(self):
        return alarm_script()

    def _alarm_write(self, conf, lines, extra=""):
        """Пишет конфиг (будильников или сценариев), свежий скрипт демона и часовой пояс, чистит звуки, которые
        больше никому не нужны, и перезапускает демон. alarms.conf создаётся всегда: по нему хук запускает демон."""
        daemon = self._alarm_script()
        tz_name, tz_data = self._pc_tz()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            write_unix(td / "conf", "".join(l + "\n" for l in lines))
            self.adb.push(td / "conf", conf + ".new")
            (td / "alarmd").write_bytes(daemon)
            self.adb.push(td / "alarmd", ALARMD)
            if tz_data:
                (td / "tz").write_bytes(tz_data)
                self.adb.push(td / "tz", f"{ALARM_DIR}/localtime")
                write_unix(td / "tzn", tz_name or "")
                self.adb.push(td / "tzn", f"{ALARM_DIR}/tzname")
        self.adb.shell(f"mv {conf}.new {conf}; touch {ALARM_CONF}; chmod 644 {ALARM_DIR}/*; chmod 755 {ALARMD}; "
                       f"cd {ALARM_DIR} && for f in [su]_*.wav; do [ -f \"$f\" ] || continue; "
                       f"cat {ALARM_CONF} {SCENES_CONF} 2>/dev/null | grep -qF \"$f\" || rm -f \"$f\"; done; "
                       f"for f in anim_*.sh; do [ -f \"$f\" ] || continue; n=${{f#anim_}}; n=$(echo \"${{n%.sh}}\" | tr '~' /); "
                       f"grep -qF \"clock:$n|\" {SCENES_CONF} 2>/dev/null || rm -f \"$f\"; done"
                       + (f"; {extra}" if extra else "") + "; true")
        self._ensure_hook()
        self._alarm_daemon(restart=True)

    # ---------- сценарии ----------
    # Сценарий — граф нод (scenes.conf, разбирает device/alarmd.sh). У if и repeat два выхода: next и else.
    SCENE_KINDS = ("query", "say", "ask", "assistant", "vol", "sound", "ring", "wait", "wait_until", "wait_quiet", "stop",
                   "scene", "anim", "if", "repeat", "note")
    SCENE_CHECKS = ("time", "days", "playing", "online", "vol_above", "chance", "assistant_mode")

    def scenes(self):
        out, _, _ = self.adb.shell(
            f"cat {SCENES_CONF} 2>/dev/null; echo '<<<'; for f in {SCENE_RUN}*; do case $f in *.nodes|*.c*|*\\*) continue;; esac; "
            f"kill -0 $(cat $f) 2>/dev/null && echo \"${{f#{SCENE_RUN}}}\"; done; echo '<<<'; tail -n 40 {SCENES_LOG} 2>/dev/null; "
            f"echo '<<<'; p=$(cat {ALARM_SD} 2>/dev/null) && kill -0 $p 2>/dev/null && echo run; true", check=False)
        conf, running, log, daemon = (strip_ansi(out).split("<<<") + ["", "", "", ""])[:4]
        items, by_id = [], {}
        num = lambda v: int(v) if v.lstrip("-").isdigit() else 0
        for line in conf.splitlines():
            f = line.rstrip("\r").split("|")
            if f[0] == "S" and len(f) == 8:
                voice = f[3] == "voice"
                s = {"id": f[1], "on": f[2] == "1", "trigger": f[3], "time": "" if voice else f[4], "days": f[5], "start": f[6],
                     "name": f[7], "phrases": [x for x in f[4].split(",") if x] if voice else [], "nodes": []}
                items.append(s); by_id[s["id"]] = s
            elif f[0] == "N" and len(f) == 10 and f[1] in by_id:
                _, sid, nid, kind, a, b, nx, el, x, y = f
                n = {"id": nid, "kind": kind, "next": nx, "else": el, "x": num(x), "y": num(y)}
                if kind in ("query", "say", "ask", "note"):
                    n["text"] = a
                elif kind == "vol":
                    n.update(level=num(a), ramp=num(b))
                elif kind == "sound":
                    n["sound"] = b or "sys:" + ALARM_DEFAULT_SOUND
                elif kind == "ring":
                    n.update(color="#" + a, secs=num(b))
                elif kind in ("wait", "wait_quiet"):
                    n["secs"] = num(a)
                elif kind == "wait_until":
                    n["time"] = a
                elif kind == "scene":
                    n["target"] = a
                elif kind == "anim":
                    n.update(anim=a, secs=num(b))
                elif kind == "assistant":
                    n["mode"] = a == "on"
                elif kind == "if":
                    n.update(check=a, arg=b)
                elif kind == "repeat":
                    n["count"] = num(a)
                by_id[sid]["nodes"].append(n)
        return {"scenes": items, "running": running.split(), "log": [l for l in log.splitlines() if l.strip()],
                "daemon": daemon.strip() == "run"}

    def scenes_save(self, data):
        items = data.get("scenes")
        if not isinstance(items, list) or len(items) > 20:
            raise AdbError("неверный список сценариев")
        # тексты идут в конфиг через «|» и в JSON для ассистента — без разделителей, кавычек и обратной косой
        text = lambda v, n: re.sub(r'[|"\\\x00-\x1f]', " ", str(v or "")).strip()[:n]
        num = lambda v, lo, hi: max(lo, min(hi, int(float(v or 0))))
        hm = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
        ids = {re.sub(r"[^a-z0-9]", "", str(s.get("id") or ""))[:12] for s in items}
        lines, sys_needed, clock_anims = [], {}, set()
        patterns = None
        for s in items:
            sid = re.sub(r"[^a-z0-9]", "", str(s.get("id") or ""))[:12] or os.urandom(4).hex()
            name = text(s.get("name"), 40) or "Сценарий"
            # manual — сам не запускается: кнопкой в панели или нодой «Другой сценарий»
            trig = s.get("trigger") if s.get("trigger") in ("boot", "voice", "manual") else "time"
            tm = str(s.get("time") or "")
            if trig == "time" and not hm.fullmatch(tm):
                raise AdbError(f"«{name}»: неверное время {tm}")
            if trig == "voice":   # фразы — на место времени; запятая разделяет их и в конфиге
                phrases = [p for p in (text(x, 60).replace(",", " ").strip() for x in (s.get("phrases") or [])[:8]) if p]
                if not phrases:
                    raise AdbError(f"«{name}»: укажите фразу для ассистента")
                tm = ",".join(phrases)
            days = "".join(sorted(set(re.sub(r"[^1-7]", "", str(s.get("days", ""))))))
            nodes = s.get("nodes")
            if not isinstance(nodes, list) or len(nodes) > 60:
                raise AdbError(f"«{name}»: нод должно быть не больше 60")
            nids = [re.sub(r"[^0-9]", "", str(n.get("id") or ""))[:4] for n in nodes]
            if "" in nids or len(set(nids)) != len(nids):
                raise AdbError(f"«{name}»: у нод должны быть разные номера")
            link = lambda v: (str(v) if str(v or "") in nids else "")
            start = link(s.get("start"))
            if not start and any(n.get("kind") != "note" for n in nodes):
                raise AdbError(f"«{name}»: соедините «Запуск» с первой нодой")
            lines.append("|".join(["S", sid, "1" if s.get("on", True) else "0", trig, tm if trig in ("time", "voice") else "",
                                   days if trig != "manual" else "", start, name]))
            for nid, n in zip(nids, nodes):
                kind = n.get("kind")
                if kind not in self.SCENE_KINDS:
                    raise AdbError(f"«{name}»: неизвестная нода {kind}")
                a = b = ""
                if kind in ("query", "say", "ask"):
                    a = text(n.get("text"), 200)
                    if not a:
                        raise AdbError(f"«{name}»: в ноде не задан текст")
                elif kind == "note":
                    a = text(n.get("text"), 200)
                elif kind == "assistant":
                    a = "on" if n.get("mode") else "off"
                elif kind == "vol":
                    a, b = num(n.get("level"), 0, 10), num(n.get("ramp"), 0, 1800)
                elif kind == "sound":
                    b = str(n.get("sound") or "")
                    a = self._alarm_sound(b, sys_needed)
                    b = b if b.startswith("sys:") or a == b else ""
                elif kind == "ring":
                    c = str(n.get("color") or "").lstrip("#").lower()
                    if not re.fullmatch(r"[0-9a-f]{6}", c):
                        raise AdbError(f"«{name}»: неверный цвет кольца")
                    a, b = c, num(n.get("secs"), 1, 600)
                elif kind == "wait":
                    a = num(n.get("secs"), 1, 86400)
                elif kind == "wait_quiet":
                    a = num(n.get("secs"), 10, 7200)
                elif kind == "wait_until":
                    a = str(n.get("time") or "")
                    if not hm.fullmatch(a):
                        raise AdbError(f"«{name}»: неверное время ожидания")
                elif kind == "scene":
                    a = re.sub(r"[^a-z0-9]", "", str(n.get("target") or ""))[:12]
                    if a not in ids or a == sid:
                        raise AdbError(f"«{name}»: выберите, какой другой сценарий запустить")
                elif kind == "anim":
                    patterns = patterns or self.patterns()
                    pkind, _, pname = str(n.get("anim") or "").partition(":")
                    if pkind not in ("ring", "clock") or pname not in patterns.get(pkind, []) or not re.fullmatch(r"(my/)?[\w.-]+", pname):
                        raise AdbError(f"«{name}»: выберите анимацию из списка")
                    if pkind == "clock":
                        clock_anims.add(pname)
                    a, b = f"{pkind}:{pname}", num(n.get("secs"), 1, 600)
                elif kind == "if":
                    a = n.get("check")
                    if a not in self.SCENE_CHECKS:
                        raise AdbError(f"«{name}»: неизвестное условие")
                    arg = str(n.get("arg") or "")
                    if a == "time":
                        f, _, to = arg.partition("-")
                        if not (hm.fullmatch(f) and hm.fullmatch(to)):
                            raise AdbError(f"«{name}»: время в условии — как 22:00-07:00")
                        b = arg
                    elif a == "days":
                        b = "".join(sorted(set(re.sub(r"[^1-7]", "", arg)))) or "1234567"
                    elif a == "vol_above":
                        b = num(arg, 0, 10)
                    elif a == "chance":
                        b = num(arg, 0, 100)
                elif kind == "repeat":
                    a = num(n.get("count"), 1, 20)
                two = kind in ("if", "repeat")
                lines.append("|".join(["N", sid, nid, kind, str(a), str(b), "" if kind == "note" else link(n.get("next")),
                                       link(n.get("else")) if two else "", str(num(n.get("x"), -20000, 20000)), str(num(n.get("y"), -20000, 20000))]))
        self._alarm_sounds_ready(sys_needed)
        # анимацию часов колонка сама не разберёт (zstd, свой формат кадров) — кладём готовый скрипт, как для «На колонке»
        if clock_anims:
            with tempfile.TemporaryDirectory() as td:
                for pname in clock_anims:
                    script = "anim_" + pname.replace("/", "~") + ".sh"   # my/… — «~» вместо «/» в имени файла
                    f = Path(td) / "anim.sh"
                    write_unix(f, clock_player_script(self.clock_frames(pname)))
                    self.adb.push(f, f"{ALARM_DIR}/{script}")
        self._alarm_write(SCENES_CONF, lines)
        return self.scenes()

    def scene_run(self, sid):
        if sid not in {s["id"] for s in self.scenes()["scenes"]}:
            raise AdbError("нет такого сценария")
        if not self.scenes()["daemon"]:
            raise AdbError("сценарии на колонке не запущены — сохраните сценарий ещё раз")
        self.adb.shell(f"echo {shlex.quote(sid)} > {SCENE_FIRE}")
        return {"ok": True}

    def scene_stop(self, sid):
        sid = re.sub(r"[^a-z0-9]", "", str(sid or ""))
        self.adb.shell(f"p=$(cat {SCENE_RUN}{sid} 2>/dev/null) && kill $p 2>/dev/null; rm -f {SCENE_RUN}{sid} {SCENE_RUN}{sid}.*; "
                       f"killall aplay 2>/dev/null; true", check=False)
        self.ring_reset()
        return self.scenes()

    def alarm_install(self):
        """Ставит поддержку будильника на колонку (или обновляет скрипт): всё в ALARM_DIR + строка в хуке загрузки.
        Существующие будильники сохраняются."""
        cur = self.alarms()["alarms"]
        return self.alarms_save({"alarms": [{**a, "sound": "sys:" + a["sound_name"] if a["sound"].startswith("s_")
                                             else a["sound"]} for a in cur]})

    def alarm_uninstall(self):
        """Полностью убирает будильник с колонки: демон, будильники, звуки, журнал; хук — если больше не нужен."""
        self.adb.shell(f"grep -q ringing {ALARM_STATE} 2>/dev/null && echo stop > {ALARM_CMD} && sleep 3; "
                       f"start-stop-daemon -K -p {ALARM_SD} 2>/dev/null; killall aplay 2>/dev/null; "
                       f"rm -rf {ALARM_DIR}; rm -f {ALARM_SD} {ALARM_STATE} {ALARM_CMD} {ALARM_FIRE}; true")
        self._drop_hook_if_unused()
        return self.alarms()

    def _alarm_daemon(self, restart=False):
        """(Пере)запуск демона. Пока будильник звенит, не трогаем — иначе он оборвётся."""
        self.adb.shell(
            f"p=$(cat {ALARM_SD} 2>/dev/null); if kill -0 $p 2>/dev/null; then "
            + (f"grep -q ringing {ALARM_STATE} 2>/dev/null || {{ kill $p; rm -f {ALARM_SD}; }}; " if restart else "")
            + f"fi; [ -f {ALARM_SD} ] && kill -0 $(cat {ALARM_SD}) 2>/dev/null || "
            f"start-stop-daemon -S -b -m -p {ALARM_SD} -x /bin/sh -- {ALARMD}")

    def alarm_test(self, aid):
        if not re.fullmatch(r"[a-z0-9]{1,12}", aid or ""):
            raise AdbError("нет такого будильника")
        if self.adb.shell(f"[ -f {ALARMD} ] && echo y; true")[0].strip() != "y":
            raise AdbError("будильник не установлен на колонку")
        self._alarm_daemon()
        self.adb.shell(f"echo {aid} > {ALARM_FIRE}")
        return {"ok": True}

    def alarm_cmd(self, action):
        if action == "cancel_snooze":
            self.adb.shell(f"rm -f {ALARM_DIR}/snooze")
        elif action in ("stop", "snooze"):
            self.adb.shell(f"grep -q ringing {ALARM_STATE} 2>/dev/null && echo {action} > {ALARM_CMD}; true")
        else:
            raise AdbError("неизвестное действие")
        return {"ok": True}

    def yam_status(self):
        out, _, _ = self.adb.shell(
            f"[ -x {YAMD} ] && echo installed; pidof yamd >/dev/null && echo running; "
            f"awk '$4==\"0A\" && $2 ~ /:{YAM_PORT:04X}$/ {{f=1}} END{{if(f)print \"listening\"}}' "
            f"/proc/net/tcp /proc/net/tcp6; [ -f {YAM_AUTOSTART} ] && {hook_active()} && echo autostart; true")
        flags = set(out.split())
        return {"installed": "installed" in flags, "running": "running" in flags and "listening" in flags,
                "autostart": "autostart" in flags,
                "web_url": f"http://{self.adb.serial.split(':')[0]}:{YAM_PORT}/" if self.adb.serial and ":" in self.adb.serial else None}

    def yam_start(self):
        if not self.yam_status()["installed"]:
            raise AdbError("yam не установлен на колонке")
        # Как в docs/DEPLOYMENT.md, только через start-stop-daemon: nohup adbd всё равно прибивает.
        self.detach("yamd", YAMD_CMD)
        for _ in range(20):
            time.sleep(0.5)
            if self.yam_status()["running"]:
                return self.yam_status()
        out, _, _ = self.adb.shell(f"tail -5 {YAM_DIR}/yamd.log", check=False)
        raise AdbError("yamd не поднял порт 8099. Лог: " + out.strip()[-400:])

    # ---------- версия yamd ----------
    # Своей версии yamd не сообщает (нет ни --version, ни API), поэтому узнаём её по sha256 бинарника:
    # ищем эту сумму в SHA256SUMS релизов yam на GitHub. Список релизов кэшируется на час, суммы — навсегда.
    _yam_releases = (0, [])
    _yam_sums = {}
    YAM_ASSET = "yamd-aarch64-linux-musl"

    def _yam_release_list(self, refresh=False):
        at, rels = self._yam_releases
        if refresh or not rels or time.time() - at > 3600:
            req = urllib.request.Request("https://api.github.com/repos/nyakokitsu/yam/releases?per_page=30",
                                         headers={"User-Agent": "webpanel", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                rels = [x for x in json.load(r) if not x.get("draft") and not x.get("prerelease")]
            # С yam 0.2 (октябрь 2026) это новый пакетный менеджер без yamd; старый yam с демоном и веб-страницей,
            # на которых держится вкладка «Приложения», выходит дальше (v0.1.x). Берём только релизы с yamd.
            rels = [x for x in rels if any(a.get("name") == self.YAM_ASSET for a in x.get("assets", []))]
            rels.sort(key=lambda x: x.get("published_at") or "", reverse=True)
            self._yam_releases = (time.time(), rels)
        return rels

    def _yam_release_sha(self, rel):
        tag = rel.get("tag_name")
        if tag not in self._yam_sums:
            url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == "SHA256SUMS"), None)
            sha = None
            if url:
                with urllib.request.urlopen(url, timeout=20) as r:
                    sha = next((ln.split()[0] for ln in r.read().decode().splitlines()
                                if ln.split()[-1:] == [self.YAM_ASSET]), None)
            self._yam_sums[tag] = sha
        return self._yam_sums[tag]

    def yam_version(self, refresh=False):
        out, _, _ = self.adb.shell(f"[ -f {YAMD} ] && sha256sum {YAMD}; true", check=False, timeout=30)
        sha = (strip_ansi(out).split() or [None])[0]
        if not sha:
            return {"installed": None, "sha": None}
        try:
            rels = self._yam_release_list(refresh)
        except (OSError, ValueError) as e:
            return {"sha": sha, "installed": None, "latest": None, "error": f"GitHub недоступен: {e}"}
        if not rels:
            return {"sha": sha, "installed": None, "latest": None}
        latest = rels[0]
        installed = None
        for rel in rels[:15]:                  # дальше в историю не ищем — это уже очень старая сборка
            try:
                if self._yam_release_sha(rel) == sha:
                    installed = rel
                    break
            except OSError:
                break
        return {"sha": sha[:12], "installed": installed and installed["tag_name"],
                "installed_date": installed and (installed.get("published_at") or "")[:10],
                "latest": latest["tag_name"], "latest_date": (latest.get("published_at") or "")[:10],
                "latest_url": latest.get("html_url"),
                # сборка не нашлась среди релизов — не предлагаем «обновить», вдруг это своя сборка
                "update": bool(installed) and installed["tag_name"] != latest["tag_name"]}

    def yam_install_daemon(self):
        """Как install.sh из репозитория yam: последний релиз с yamd, проверка SHA256SUMS, yamd на колонку, запуск."""
        rels = self._yam_release_list(refresh=True)
        if not rels:
            raise AdbError("на GitHub нет релиза yam с yamd-aarch64-linux-musl")
        rel = rels[0]
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        if "yamd-aarch64-linux-musl" not in assets or "SHA256SUMS" not in assets:
            raise AdbError(f"в релизе yam {rel.get('tag_name')} нет SHA256SUMS")
        with urllib.request.urlopen(assets["SHA256SUMS"], timeout=30) as r:
            sums = r.read().decode()
        expected = next((ln.split()[0] for ln in sums.splitlines()
                         if ln.split()[-1:] == ["yamd-aarch64-linux-musl"]), None)
        with urllib.request.urlopen(assets["yamd-aarch64-linux-musl"], timeout=300) as r:
            blob = r.read()
        if not expected or hashlib.sha256(blob).hexdigest() != expected:
            raise AdbError("контрольная сумма yamd не совпала с SHA256SUMS")
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "yamd"
            f.write_bytes(blob)
            self.adb.shell(f"mkdir -p {YAM_DIR}/bin {YAM_DIR}/data")
            self.adb.push(f, f"{YAMD}.new", timeout=300)
        self.adb.shell(f"chmod 755 {YAMD}.new; pidof yamd >/dev/null && kill $(pidof yamd); sleep 1; mv {YAMD}.new {YAMD}")
        st = self.yam_start()
        return {**st, "version": rel.get("tag_name")}

    def yam_autostart(self, enabled):
        if enabled:
            if not self.yam_status()["installed"]:
                raise AdbError("yam не установлен на колонке")
            self._ensure_hook()
            self.adb.shell(f"mkdir -p {WEB_DIR}; touch {YAM_AUTOSTART}")
        else:
            self.adb.shell(f"rm -f {YAM_AUTOSTART}")
            self._drop_hook_if_unused()
        return self.yam_status()

    def _yam(self, method, path, body=None, headers=None, timeout=60):
        """Запрос к API yamd через adb forward (переподнимаем проброс, если adb переподключился)."""
        for attempt in (1, 2):
            if self._yam_port is None:
                self._yam_port = self.adb.forward(YAM_PORT)
            req = urllib.request.Request(f"http://127.0.0.1:{self._yam_port}{path}", data=body,
                                         method=method, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = r.read()
                    ctype = r.headers.get("Content-Type", "")
                    return json.loads(data) if "json" in ctype and data else data.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                raise AdbError(f"yamd: {e.read().decode('utf-8', 'replace').strip() or e.reason}")
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                self._yam_port = None
                if attempt == 2:
                    raise AdbError(f"yamd недоступен ({e}). Запущен ли он?")

    def yam_registry(self, refresh=False):
        ts, data = self._registry
        if refresh or data is None or time.time() - ts > 600:
            with urllib.request.urlopen(YAM_REGISTRY, timeout=20) as r:
                data = json.load(r)
            self._registry = (time.time(), data)
        return data

    def yam_plugins(self):
        return self._yam("GET", "/api/plugins")

    def yam_install(self, pkg_id, version):
        entry = next((p for p in self.yam_registry()["packages"]
                      if p["id"] == pkg_id and p["version"] == version), None)
        if not entry:
            raise AdbError("нет такого пакета в реестре")
        # Качаем на компьютере и сверяем sha256 сами: колонке не нужен доступ к GitHub.
        with urllib.request.urlopen(entry["url"], timeout=120) as r:
            blob = r.read()
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            raise AdbError("контрольная сумма пакета не совпала с реестром")
        boundary = "webpanel" + os.urandom(8).hex()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"package\"; filename=\"{pkg_id}.yap\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + blob + f"\r\n--{boundary}--\r\n".encode()
        res = self._yam("POST", "/api/plugins", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                        timeout=300)
        self.adb.shell(YAM_FIX_EXEC, check=False)
        return res

    def yam_action(self, action, pkg_id):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", pkg_id or ""):
            raise AdbError("плохой id пакета")
        method, path = {"remove": ("DELETE", ""), "toggle": ("POST", "/toggle"),
                        "run": ("POST", "/invoke"), "stop": ("POST", "/stop")}[action]
        body, headers = (b"{}", {"Content-Type": "application/json"}) if action == "run" else (None, {})
        if action == "run":
            self.adb.shell(YAM_FIX_EXEC, check=False)
        try:
            self._yam(method, f"/api/plugins/{pkg_id}{path}", body, headers)
        except AdbError as e:
            if action != "run" or "package launch mode does not support this command" not in str(e):
                raise
            self._yam("POST", f"/api/plugins/{pkg_id}/service/start")
        return {"ok": True}

    def yam_logs(self, pkg_id):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", pkg_id or ""):
            raise AdbError("плохой id пакета")
        return {"text": strip_ansi(self._yam("GET", f"/api/plugins/{pkg_id}/logs") or "")}

    def yam_settings(self, data=None):
        s = self._yam("GET", "/api/settings")
        if data and "allow_plugin_binaries" in data:
            s["allow_plugin_binaries"] = bool(data["allow_plugin_binaries"])
            self._yam("PUT", "/api/settings", json.dumps(s).encode(), {"Content-Type": "application/json"})
        return {"allow_plugin_binaries": s.get("allow_plugin_binaries", False)}

    # ---------- Плагины Luna: тот же HTTP API на :8099, что у старого yamd (клиент _yam выше) ----------

    LUNA_ACTIONS = {"remove": ("DELETE", ""), "toggle": ("POST", "/toggle"), "run": ("POST", "/invoke"),
                    "stop": ("POST", "/stop"), "service_start": ("POST", "/service/start"),
                    "service_stop": ("POST", "/service/stop"), "service_enable": ("POST", "/service/enable"),
                    "service_disable": ("POST", "/service/disable")}

    def luna_plugins(self):
        return {"plugins": self._yam("GET", "/api/plugins")}

    def luna_action(self, action, pkg_id):
        if action == "rescan":
            self._yam("POST", "/api/plugins/rescan")
            return {"ok": True}
        if action not in self.LUNA_ACTIONS:
            raise AdbError(f"неизвестное действие {action}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", pkg_id or ""):
            raise AdbError("плохой id плагина")
        method, path = self.LUNA_ACTIONS[action]
        body, headers = (b"{}", {"Content-Type": "application/json"}) if action == "run" else (None, {})
        try:
            self._yam(method, f"/api/plugins/{pkg_id}{path}", body, headers)
        except AdbError as e:
            if action != "run" or "package launch mode does not support this command" not in str(e):
                raise
            self._yam("POST", f"/api/plugins/{pkg_id}/service/start")
        return {"ok": True}

    def luna_logs(self, pkg_id):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", pkg_id or ""):
            raise AdbError("плохой id плагина")
        return {"text": strip_ansi(self._yam("GET", f"/api/plugins/{pkg_id}/logs") or "")}

    def luna_install(self, blob, filename):
        name = (filename or "").strip()
        if not name.lower().endswith(".yap") or len(name) <= 4 or "/" in name or "\\" in name:
            raise AdbError("ожидался файл пакета .yap")
        boundary = "webpanel" + os.urandom(8).hex()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"package\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + blob + f"\r\n--{boundary}--\r\n".encode()
        return self._yam("POST", "/api/plugins", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                         timeout=300)

    # ---------- yam 2: пакетный менеджер на колонке ----------
    # Платформа yam по device-tree — те же правила, что в install.sh yam (getprop есть не на всех колонках).
    # Порядок важен: у Лайт и Мини в compatible есть amlogic,axg, у Лайт 2 — тоже, поэтому axg — последним.
    # yandex,station-midi — это Станция 2 (yandexmidi); cucumber — Станция Миди (cucumber), не наоборот.
    # yandexstation_2 — Станция Макс: yam её платформу знает, но профиля панели пока нет.
    YAM2_PLATFORMS = ((r"station-mini2", "yandexmini_2"), (r"cucumber", "cucumber"), (r"yandex,station-midi", "yandexmidi"),
                      (r"prune", "plum"),
                      (r"orion", "orion"), (r"pickle", "pickle"), (r"station-mini3|bergamot", "bergamot"),
                      (r"saturn", "saturn"), (r"tulip", "yandexstation"), (r"yandexstation_2|station-max", "yandexstation_2"),
                      (r"mediatek,mt8167|jbl.*link.*music", "jbl_link_music"), (r"mango", "mango"), (r"plum", "plum"),
                      (r"yandex.*station.*micro|yandexmicro", "yandexmicro"), (r"amlogic,axg", "yandexmini"))
    YAM2_ARMV7 = ("mango", "jbl_link_music")
    _yam2_rels = (0, [])
    _yam2_sums = {}
    _yam2_lock = threading.Lock()

    @classmethod
    def yam2_detect(cls, identity):
        identity = identity.lower()
        return next((p for rx, p in cls.YAM2_PLATFORMS if re.search(rx, identity)), None)

    def yam2_platform(self):
        # Платформа, с которой yam уже установлен, — главнее: её выбрал установщик (или человек через YAM_PLATFORM).
        out, _, _ = self.adb.shell(f"sed -n \"s/^export YAM_PLATFORM='\\(.*\\)'$/\\1/p\" {YAM2_RUN} 2>/dev/null; echo @; "
                                   "getprop ro.product.model 2>/dev/null; getprop ro.product.device 2>/dev/null; "
                                   "cat /proc/device-tree/compatible 2>/dev/null | tr '\\000' '\\n'; true", check=False)
        installed, _, identity = strip_ansi(out).partition("@")
        installed = installed.strip()
        return installed if YAM2_NAME.fullmatch(installed or "") else self.yam2_detect(identity)

    def _yam2(self, args, timeout=120):
        """yam-run с аргументами; вывод без ANSI. args — список, каждый аргумент экранируется."""
        out, err, rc = self.adb.shell(f"{YAM2_RUN} {' '.join(shlex.quote(a) for a in args)} 2>&1", check=False,
                                      timeout=timeout)
        return strip_ansi(out + err).strip(), rc

    def _yam2_check(self, args, timeout=120):
        out, rc = self._yam2(args, timeout)
        if rc != 0:
            raise AdbError("yam: " + (out[-600:] or f"код {rc}"))
        return out

    @staticmethod
    def _yam2_pkg(name, version=False):
        m = re.fullmatch(r"([a-z0-9][a-z0-9._-]{0,63})/([a-z0-9][a-z0-9._-]{0,63})(@[0-9A-Za-z.+~-]{1,40})?", name or "")
        if not m or (m.group(3) and not version):
            raise AdbError("плохое имя пакета: нужно пространство/имя")
        return name

    def _yam2_releases(self, refresh=False):
        at, rels = self._yam2_rels
        if refresh or not rels or time.time() - at > 3600:
            req = urllib.request.Request("https://api.github.com/repos/nyakokitsu/yam/releases?per_page=30",
                                         headers={"User-Agent": "webpanel", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                rels = [x for x in json.load(r) if not x.get("draft") and not x.get("prerelease")
                        and re.fullmatch(r"yam-v\d+\.\d+\.\d+", x.get("tag_name") or "")]
            rels.sort(key=lambda x: x.get("published_at") or "", reverse=True)
            self._yam2_rels = (time.time(), rels)
        return rels

    def _yam2_sums_of(self, rel):
        tag = rel["tag_name"]
        if tag not in self._yam2_sums:
            url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == "SHA256SUMS"), None)
            sums = {}
            if url:
                with urllib.request.urlopen(url, timeout=30) as r:
                    for ln in r.read().decode().splitlines():
                        p = ln.split()
                        if len(p) == 2 and re.fullmatch(r"[0-9a-f]{64}", p[0]):
                            sums[p[1].lstrip("*")] = p[0]
            self._yam2_sums[tag] = sums
        return self._yam2_sums[tag]

    def yam2_status(self, refresh=False):
        plat = self.yam2_platform()
        out, _, _ = self.adb.shell(f"[ -x {YAM2} ] && [ -x {YAM2_RUN} ] && sha256sum {YAM2}; true", check=False, timeout=30)
        sha = (strip_ansi(out).split() or [None])[0]
        legacy, _, _ = self.adb.shell(f"[ -x {YAMD} ] && echo installed; pidof yamd >/dev/null && echo running; true",
                                      check=False)
        host = self.adb.serial.split(":")[0] if self.adb.serial and ":" in self.adb.serial else None
        res = {"installed": bool(sha), "platform": plat, "root": YAM2_ROOT, "host": host,
               "legacy": {"installed": "installed" in legacy, "running": "running" in legacy}}
        try:
            rels = self._yam2_releases(refresh)
            latest = rels[0] if rels else None
            res["latest"] = latest and latest["tag_name"]
            res["latest_url"] = latest and latest.get("html_url")
            if sha:
                asset = self._yam2_asset(plat)
                res["version"] = next((r["tag_name"] for r in rels[:10]
                                       if self._yam2_sums_of(r).get(asset) == sha), None)
                res["update"] = bool(res["version"]) and res["version"] != res["latest"]
        except (OSError, ValueError) as e:
            res["error"] = f"GitHub недоступен: {e}"
        if sha:
            out, rc = self._yam2(["registry", "list"], timeout=30)
            res["registries"] = [dict(zip(("name", "url"), ln.split("\t", 1))) for ln in out.splitlines()
                                 if "\t" in ln] if rc == 0 else []
            if rc != 0:
                res["error"] = "yam: " + out[-300:]
        return res

    def _yam2_asset(self, plat):
        return "yam-armv7-unknown-linux-musleabihf" if plat in self.YAM2_ARMV7 else "yam-aarch64-unknown-linux-musl"

    def yam2_install(self):
        """Как install.sh yam: последний релиз yam-v…, сверка с SHA256SUMS, бинарник + профиль + обёртка в /data/yam.
        Корень системы не трогаем (на Лайт install.sh ещё делает /usr/bin/yam и S99yam), старый yamd — тоже."""
        plat = self.yam2_platform()
        if not plat:
            raise AdbError("не удалось определить платформу колонки для yam")
        if not self._yam2_lock.acquire(blocking=False):
            raise AdbError("yam уже ставится")
        try:
            rels = self._yam2_releases(refresh=True)
            asset = self._yam2_asset(plat)
            rel = next((r for r in rels if any(a["name"] == asset for a in r.get("assets", []))), None)
            if not rel:
                raise AdbError(f"на GitHub нет релиза yam с {asset}")
            tag = rel["tag_name"]
            expected = self._yam2_sums_of(rel).get(asset)
            if not expected:
                raise AdbError(f"в релизе {tag} нет контрольной суммы {asset}")
            a = next(x for x in rel["assets"] if x["name"] == asset)
            with urllib.request.urlopen(a["browser_download_url"], timeout=300) as r:
                blob = r.read()
            if hashlib.sha256(blob).hexdigest() != expected or (a.get("digest") and a["digest"] != "sha256:" + expected):
                raise AdbError("контрольная сумма yam не совпала с SHA256SUMS")
            with urllib.request.urlopen(f"https://raw.githubusercontent.com/nyakokitsu/yam/{tag}/platforms/{plat}.yaml",
                                        timeout=30) as r:
                profile = r.read()
            if len(profile) > 4096 or not re.search(rb"(?m)^id: " + re.escape(plat.encode()) + rb"\s*$", profile):
                raise AdbError(f"профиль платформы {plat} в релизе {tag} не похож на профиль yam")
            free, _, _ = self.adb.shell(f"mkdir -p {YAM2_ROOT}/bin {YAM2_ROOT}/platforms && "
                                        f"df -k {YAM2_ROOT} | tail -1 | awk '{{print $4}}'", check=False)
            if free.strip().isdigit() and int(free) < len(blob) // 1024 + 4096:
                raise AdbError(f"мало места в /data: свободно {free.strip()} КБ")
            wrapper = (f"#!/bin/sh\nexport YAM_PLATFORM='{plat}'\nexport YAM_PROFILES='{YAM2_ROOT}/platforms'\n"
                       f"export YAM_ROOT='{YAM2_ROOT}'\nexec '{YAM2}' \"$@\"\n")   # как у install.sh: его --uninstall узнаёт её
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "yam").write_bytes(blob)
                (Path(td) / "p.yaml").write_bytes(profile)
                write_unix(Path(td) / "yam-run", wrapper)
                self.adb.push(Path(td) / "yam", f"{YAM2}.new", timeout=300)
                self.adb.push(Path(td) / "p.yaml", f"{YAM2_ROOT}/platforms/{plat}.yaml")
                self.adb.push(Path(td) / "yam-run", f"{YAM2_RUN}.new")
            out, _, _ = self.adb.shell(f"sha256sum {YAM2}.new", check=False)
            if expected not in out:
                self.adb.shell(f"rm -f {YAM2}.new {YAM2_RUN}.new", check=False)
                raise AdbError("файл на колонке не совпал со скачанным")
            self.adb.shell(f"chmod 755 {YAM2}.new {YAM2_RUN}.new && mv {YAM2}.new {YAM2} && mv {YAM2_RUN}.new {YAM2_RUN}")
            self._yam2_check(["list"], timeout=30)       # создаёт базу и заодно проверяет, что бинарник запускается
            return {**self.yam2_status(), "installed_tag": tag}
        finally:
            self._yam2_lock.release()

    def yam2_update(self):
        # Обновление сам yam проверяет подписью Ed25519 своего релиза
        return {"text": self._yam2_check(["update"], timeout=300), **self.yam2_status(refresh=True)}

    def yam2_uninstall(self):
        """Удаляет /data/yam целиком — как install.sh --uninstall, но без /usr/bin/yam и S99yam: их ставит не панель."""
        for svc in self._yam2_services_raw():
            self._yam2_svc_stop(svc["id"])
        out, _, rc = self.adb.shell(
            f"grep -Fqx \"export YAM_PROFILES='{YAM2_ROOT}/platforms'\" {YAM2_RUN} || exit 3; rm -rf {YAM2_ROOT}; "
            f"rm -rf {YAM2_SVC_DIR}", check=False, timeout=60)
        if rc == 3:
            raise AdbError(f"в {YAM2_ROOT} нет обёртки yam-run — не похоже на yam, удалять не стали")
        self._drop_hook_if_unused()
        return self.yam2_status()

    def yam2_registry_add(self):
        name, url, key = YAM2_REGISTRY
        return {"text": self._yam2_check(["registry", "add", name, url, "--key", key], timeout=30)}

    def yam2_packages(self):
        out = self._yam2_check(["list"], timeout=30)
        pkgs = [dict(zip(("name", "version", "target"), ln.split("\t"))) for ln in out.splitlines() if ln.count("\t") == 2]
        services = self._yam2_services_raw()
        for p in pkgs:
            p["services"] = [s for s in services if s["package"] == p["name"]]
        return {"packages": pkgs}

    def yam2_catalog(self, refresh=False):
        """Каталог настроенного реестра — для показа (описания, платформы). Подписи тут не проверяем: ставит пакет
        сам yam на колонке и проверяет подпись индекса и SHA-256 архива."""
        st = self.yam2_status()
        reg = next((r for r in st.get("registries", []) if r["name"] == YAM2_REGISTRY[0]), None)
        if not reg:
            return {"configured": False, "packages": [], "platform": st["platform"]}
        url = reg["url"].rstrip("/")
        if not url.startswith("https://"):
            raise AdbError("адрес реестра не https")
        ts, data, src = getattr(self, "_yam2_cat", (0, None, None))
        if refresh or data is None or src != url or time.time() - ts > 600:
            last = None
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url + "/index.json", headers={"User-Agent": "webpanel"}),
                                                timeout=20) as r:
                        data = json.load(r)
                    break
                except (urllib.error.URLError, OSError, ValueError) as e:
                    last = getattr(e, "reason", e)
                    if attempt == 0:
                        time.sleep(0.5)
            else:
                raise AdbError("каталог YAM 2 недоступен из контейнера: " + str(last)
                               + ". Проверьте доступ к " + url + " — в Docker/WSL2 часто мешает прокси или свои сертификаты")
            self._yam2_cat = (time.time(), data, url)
        plat = st["platform"]
        pkgs = [{k: p.get(k) for k in ("name", "summary", "latest", "homepage", "targets")}
                for p in data.get("packages", []) if isinstance(p, dict) and isinstance(p.get("name"), str)
                and p["name"] not in (data.get("revoked") or {})]
        for p in pkgs:
            t = p.get("targets") or []
            p["fits"] = not t or "any" in t or plat in t
            if p["name"] == "tangenx/wakeword":
                p["panel"] = WAKE_SUPPORT
        return {"configured": True, "packages": pkgs, "platform": plat, "url": url}

    def yam2_install_pkg(self, name, yes=False):
        name = self._yam2_pkg(name, version=True)
        bare = name.split("@")[0]
        running = [s["id"] for s in self._yam2_services_raw() if s["package"] == bare and s["running"]]
        out, rc = self._yam2(["install"] + (["--yes"] if yes else []) + [name], timeout=900)
        if rc != 0:
            # Пакет что-то заменяет или просит Entware — yam ждёт --yes; показываем человеку и спрашиваем.
            if not yes and ("rerun with --yes" in out or "requires --yes" in out):
                return {"need_yes": True, "text": out[-1200:]}
            raise AdbError("yam: " + (out[-800:] or f"код {rc}"))
        if bare == "tangenx/wakeword":
            self._ww_info = None   # вкладка должна увидеть установку сразу, без 15-секундного кэша
        for sid in running:          # обновлённый пакет: службы перезапускаем уже с новой версией
            self._yam2_svc_stop(sid)
            self._yam2_svc_start(sid)
        return {"text": out}

    def yam2_pkg(self, action, name, yes=False):
        if action == "install":
            return self.yam2_install_pkg(name, yes)
        if action == "remove":
            return self.yam2_remove(name)
        raise AdbError("неизвестное действие")

    def yam2_remove(self, name):
        name = self._yam2_pkg(name)
        for s in self._yam2_services_raw():
            if s["package"] == name:
                self._yam2_svc_stop(s["id"])
                self.adb.shell(f"rm -f {YAM2_SVC_DIR}/{s['id']}.sh {YAM2_SVC_DIR}/boot/{s['id']}", check=False)
        out = self._yam2_check(["remove", name], timeout=120)
        if name == "tangenx/wakeword":
            self._ww_info = None
        # каталоги etc/var/run пакета создавала панель — пустые убираем, с данными оставляем (вдруг поставят снова)
        ns, pkg = name.split("/")
        _, paths = self._yam2_paths()
        self.adb.shell("; ".join(f"rmdir {shlex.quote(paths[k] + '/' + ns + '/' + pkg)} {shlex.quote(paths[k] + '/' + ns)}"
                                 for k in ("etc", "var", "run")) + " 2>/dev/null; true", check=False)
        self._drop_hook_if_unused()
        return {"text": out}

    def yam2_doctor(self, repair=False):
        out, rc = self._yam2(["repair" if repair else "doctor"], timeout=120)
        return {"ok": rc == 0, "text": out}

    # Службы. yam service list печатает «пакет/служба<TAB>enabled|disabled<TAB>exec через пробел» — аргументы с
    # пробелами так не восстановить, но у пакетов их и нет; переменные ${YAM_*} подставляем сами, как задумано в yam.
    def _yam2_services_raw(self):
        out, rc = self._yam2(["service", "list"], timeout=30)
        if rc != 0:
            return []
        ids = []
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) != 3 or parts[0].count("/") != 2:
                continue
            ns, pkg, name = parts[0].split("/")
            if not all(YAM2_NAME.fullmatch(x) for x in (ns, pkg, name)):
                continue
            ids.append({"id": f"{ns}~{pkg}~{name}", "package": f"{ns}/{pkg}", "name": name,
                        "declared": parts[1] == "enabled", "exec": parts[2].split()})
        if ids:
            flags, _, _ = self.adb.shell("; ".join(
                f"p=$(cat {YAM2_SVC_PID}{s['id']}.pid 2>/dev/null) && kill -0 \"$p\" 2>/dev/null && echo run:{s['id']}; "
                f"[ -f {YAM2_SVC_DIR}/boot/{s['id']} ] && echo boot:{s['id']}" for s in ids) + "; true", check=False)
            flags = set(strip_ansi(flags).split())
            for s in ids:
                s["running"] = f"run:{s['id']}" in flags
                s["boot"] = f"boot:{s['id']}" in flags
        return ids

    def _yam2_paths(self):
        """paths.etc/var/run из профиля платформы на колонке."""
        plat = self.yam2_platform()
        out, _, _ = self.adb.shell(f"cat {YAM2_ROOT}/platforms/{plat}.yaml 2>/dev/null; true", check=False)
        paths = {"etc": f"{YAM2_ROOT}/etc", "var": f"{YAM2_ROOT}/var", "run": "/tmp/yam"}
        for k in paths:
            m = re.search(rf"(?m)^\s+{k}:\s*(/[A-Za-z0-9/._-]+)\s*$", out)
            if m and ".." not in m.group(1):
                paths[k] = m.group(1)
        return plat, paths

    def _yam2_launcher(self, svc):
        """sh-скрипт службы: окружение yam, перезапуск при падении (не чаще 5 раз подряд), лог в каталоге run."""
        ns, pkg, name = svc["id"].split("~")
        plat, paths = self._yam2_paths()
        pkg_dir = f"{YAM2_ROOT}/pkgs/{ns}/{pkg}/current"
        env = {"YAM_ROOT": YAM2_ROOT, "YAM_PKG": pkg_dir, "YAM_PLATFORM": plat,
               "YAM_ETC": f"{paths['etc']}/{ns}/{pkg}", "YAM_VAR": f"{paths['var']}/{ns}/{pkg}",
               "YAM_RUN": f"{paths['run']}/{ns}/{pkg}"}
        argv = [re.sub(r"\$\{(YAM_[A-Z]+)\}", lambda m: env.get(m.group(1), m.group(0)), a) for a in svc["exec"]]
        exe = argv[0]
        if exe.startswith("/") or ".." in exe.split("/"):
            raise AdbError(f"служба {svc['package']}/{name}: странный путь запуска {exe}")
        log = f"{env['YAM_RUN']}/{name}.log"
        q = shlex.quote
        return ("#!/bin/sh\n# служба пакета yam 2, создана веб-панелью\n"
                + "".join(f"export {k}={q(v)}\n" for k, v in env.items())
                + f"mkdir -p \"$YAM_ETC\" \"$YAM_VAR\" \"$YAM_RUN\"\ncd \"$YAM_PKG\" || exit 1\n"
                + f": > {q(log)}\nfails=0\n"
                + "while :; do\n  t=$(date +%s)\n"
                + f"  {' '.join(q(a) for a in [pkg_dir + '/' + exe] + argv[1:])} >> {q(log)} 2>&1\n"
                + "  rc=$?\n  [ \"$rc\" = 0 ] && exit 0\n"
                + "  [ $(( $(date +%s) - t )) -gt 60 ] && fails=0\n  fails=$((fails + 1))\n"
                + f"  echo \"[webpanel] выход с кодом $rc, перезапуск $fails/5\" >> {q(log)}\n"
                + "  [ \"$fails\" -ge 5 ] && exit 1\n  sleep 3\ndone\n")

    def _yam2_svc(self, sid):
        svc = next((s for s in self._yam2_services_raw() if s["id"] == sid), None)
        if not svc:
            raise AdbError("нет такой службы")
        return svc

    def _yam2_svc_start(self, sid):
        svc = self._yam2_svc(sid)
        with tempfile.TemporaryDirectory() as td:
            write_unix(Path(td) / "s", self._yam2_launcher(svc))
            self.adb.shell(f"mkdir -p {YAM2_SVC_DIR}/boot")
            self.adb.push(Path(td) / "s", f"{YAM2_SVC_DIR}/{sid}.sh")
        pid = f"{YAM2_SVC_PID}{sid}.pid"
        self.adb.shell(f"p=$(cat {pid} 2>/dev/null) && kill -0 \"$p\" 2>/dev/null || "
                       f"start-stop-daemon -S -b -m -p {pid} -x /bin/sh -- {YAM2_SVC_DIR}/{sid}.sh")

    def _yam2_svc_stop(self, sid):
        # start-stop-daemon -b делает setsid: у скрипта своя группа процессов, гасим её целиком вместе с программой
        pid = f"{YAM2_SVC_PID}{sid}.pid"
        self.adb.shell(f"p=$(cat {pid} 2>/dev/null) && kill -0 \"$p\" 2>/dev/null && {{ {YAM2_KILL}; "
                       f"for i in 1 2 3 4 5 6 7 8 9 10; do [ -n \"$g\" ] && kill -0 -- -$g 2>/dev/null || break; sleep 0.5; done; "
                       f"[ \"${{g:-0}}\" -gt 1 ] && kill -KILL -- -$g 2>/dev/null; }}; rm -f {pid}; true", check=False)

    def yam2_service(self, sid, action, stop_legacy=False):
        if not re.fullmatch(r"[a-z0-9._-]+~[a-z0-9._-]+~[a-z0-9._-]+", sid or ""):
            raise AdbError("плохой id службы")
        if action == "start":
            # Luna — преемница yamd и слушает тот же порт 8099: вместе им не ужиться
            if sid.startswith("yam~luna~") and self.adb.shell("pidof yamd >/dev/null && echo y; true")[0].strip():
                if not stop_legacy:
                    return {"need_stop_legacy": True}
                self.adb.shell("kill $(pidof yamd) 2>/dev/null; true", check=False)
                self.adb.shell(f"rm -f {YAM_AUTOSTART}", check=False)
            self._yam2_svc_start(sid)
        elif action == "stop":
            self._yam2_svc_stop(sid)
        elif action in ("boot", "noboot"):
            if action == "boot":
                self._yam2_svc(sid)
                self._ensure_hook()
                if not self.adb.shell(f"[ -f {YAM2_SVC_DIR}/{sid}.sh ] && echo y; true")[0].strip():
                    with tempfile.TemporaryDirectory() as td:
                        write_unix(Path(td) / "s", self._yam2_launcher(self._yam2_svc(sid)))
                        self.adb.shell(f"mkdir -p {YAM2_SVC_DIR}/boot")
                        self.adb.push(Path(td) / "s", f"{YAM2_SVC_DIR}/{sid}.sh")
                self.adb.shell(f"mkdir -p {YAM2_SVC_DIR}/boot && touch {YAM2_SVC_DIR}/boot/{sid}")
            else:
                self.adb.shell(f"rm -f {YAM2_SVC_DIR}/boot/{sid}")
                self._drop_hook_if_unused()
        else:
            raise AdbError("неизвестное действие")
        time.sleep(1 if action == "start" else 0)
        return self._yam2_svc(sid)

    def yam2_service_log(self, sid):
        if not re.fullmatch(r"[a-z0-9._-]+~[a-z0-9._-]+~[a-z0-9._-]+", sid or ""):
            raise AdbError("плохой id службы")
        ns, pkg, name = sid.split("~")
        _, paths = self._yam2_paths()
        out, _, _ = self.adb.shell(f"tail -c 16000 {shlex.quote(paths['run'] + f'/{ns}/{pkg}/{name}.log')} 2>/dev/null; true",
                                   check=False)
        return {"text": strip_ansi(out)}

    # ---------- музыка: аккаунт Яндекса и Glagol ----------
    _bt_pos = None   # (позиция, с; когда измерена; название трека)

    def music(self):
        g = self.glagol.info()
        p = (g.get("state") or {}).get("playerState")
        if p and p.get("playerType") == "bluetooth":
            # По Bluetooth Quasar шлёт в Glagol застывший progress (~0) — берём позицию у телефона по AVRCP.
            pos = self._bt_position(p.get("title"))
            if pos:
                g = {**g, "state": {**g["state"], "playerState": {**p, "progress": pos[0]}}, "progress_at": pos[1]}
        if p and p.get("type") == "FmRadio":
            self._remember_fm(p)
        return {"auth": self.auth.info(), "glagol": g}

    # Логотипы станций Алисы: их отдаёт только плеер, пока станция играет, — запоминаем на будущее.
    _fm_cache = None

    def _fm_file(self):
        return DATA_DIR / ".radio_alice.json"

    def fm_stations(self):
        if self._fm_cache is None:
            try:
                self._fm_cache = json.loads(self._fm_file().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._fm_cache = {}
        return self._fm_cache

    def _remember_fm(self, p):
        sid, title = str(p.get("id") or "")[:60], str(p.get("title") or "").strip()[:80]
        cover = str((p.get("extra") or {}).get("coverURI") or "")[:300]
        if not re.fullmatch(r"[\w.-]+", sid) or not title:
            return
        known = self.fm_stations()
        entry = {"title": title, "cover": cover}
        if known.get(sid) != entry:
            known[sid] = entry
            try:
                write_unix(self._fm_file(), json.dumps(known, ensure_ascii=False))
            except OSError:
                pass

    def _bt_position(self, title):
        """Позиция трека с телефона; спрашиваем не чаще раза в 3 с — между опросами её досчитывает страница."""
        c, now = self._bt_pos, time.time()
        if c and now - c[1] < 3 and c[2] == title:
            return c
        out, _, _ = self.adb.shell(BLUEZ_DUMP + "; true", check=False, timeout=8)
        pos = next((i["org.bluez.MediaPlayer1"]["Position"] for i in parse_bluez(strip_ansi(out)).values()
                    if isinstance(i.get("org.bluez.MediaPlayer1", {}).get("Position"), int)), None)
        self._bt_pos = (pos / 1000, now, title) if pos is not None else None
        return self._bt_pos

    def music_cmd(self, d):
        cmd = d.get("command")
        if cmd not in MUSIC_COMMANDS:
            raise AdbError("неизвестная команда")
        if cmd == "sendText":
            text = d.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= ALICE_COMMAND_LIMIT:
                raise AdbError("Команда Алисе должна содержать от 1 до 1024 символов")
            d = {**d, "text": text.strip()}
        payload = {"command": cmd, **{k: d[k] for k in MUSIC_COMMANDS[cmd] if k in d}}
        res = self.glagol.send(payload)
        if cmd == "play":
            # проверка «заиграло ли» — в фоне: ответ кнопке нужен сразу, а не через 4 с
            threading.Thread(target=self._play_fallback, args=(res,), daemon=True).start()
        return res

    def _play_fallback(self, res):
        """После музыки по Bluetooth облако Алисы помнит Bluetooth последним плеером: «play» (продолжить)
        превращается в bluetooth_player_resume, а телефон уже отключён — в логе Quasar «Media player is not
        initialized», и ничего не играет. Если за 4 с не заиграло, запускаем то же самое заново через playMusic:
        станцию — как станцию, иначе — последний трек."""
        for _ in range(8):
            time.sleep(0.5)
            if (self.glagol.state or {}).get("playing"):
                return res
        p = (self.glagol.state or {}).get("playerState") or {}
        if p.get("playerType") == "bluetooth":
            return res
        pid, ptype = p.get("playlistId") or "", p.get("playlistType")
        if ptype == "Radio" and pid:
            payload = {"command": "playMusic", "id": pid, "type": "radio"}
        elif p.get("id"):
            payload = {"command": "playMusic", "id": str(p["id"]), "type": "track"}
        else:
            return res
        return {**self.glagol.send(payload), "fallback": payload["type"]}

    # ---------- Яндекс Музыка: подробности о треке ----------
    def _need_login(self):
        if not self.auth.music_token:
            raise AdbError("войдите в Яндекс")

    def music_info(self, tid):
        self._need_login()
        return self.ymusic.track(tid)

    def music_lyrics(self, tid):
        self._need_login()
        return self.ymusic.lyrics(tid)

    def music_queue(self):
        self._need_login()
        player = (self.glagol.state or {}).get("playerState")
        if not player:
            return {"title": None, "tracks": [], "current": None}
        return self.ymusic.queue(player)

    def music_like(self, tid, on):
        self._need_login()
        return self.ymusic.like(tid, on)

    def music_dislike(self, tid):
        self._need_login()
        r = self.ymusic.dislike(tid)
        if self.glagol.ws:   # как дизлайк в приложении: убрать и перейти к следующему
            self.glagol.send({"command": "next"})
        return r

    def music_play(self, item_id, kind):
        if kind not in ("track", "album", "playlist", "artist"):
            raise AdbError("неизвестный тип")
        return self.glagol.send({"command": "playMusic", "id": str(item_id), "type": kind})

    def auth_qr_start(self):
        return self.auth.qr_start()

    def auth_qr_status(self):
        r = self.auth.qr_status()
        if r.get("state") == "done":
            self.glagol.reconnect()
        return r

    def auth_oauth(self, url):
        r = self.auth.oauth_finish(url)
        self.glagol.reconnect()
        return r

    def auth_logout(self):
        r = self.auth.logout()
        self.glagol.reconnect()
        return r

    # ---------- bluetooth ----------
    # Сопряжение подтверждает сам just-works-bluez-agent из прошивки, поэтому достаточно сделать колонку видимой.
    BT_PAIR_SECONDS = 180
    BT_PAIR_FLAG = "/tmp/webpanel_bt_pair"

    def bt_state(self):
        out = strip_ansi(self.adb.shell(f"{BLUEZ_DUMP}; echo @@PAIR; cat {self.BT_PAIR_FLAG} 2>/dev/null; "
                                        f"echo @@OUT; cat {BTOUT_MAC} 2>/dev/null; echo; cat {BTOUT_STATE} 2>/dev/null; echo; "
                                        f"p=$(cat {BTOUT_SD} 2>/dev/null) && kill -0 $p 2>/dev/null && echo watch; "
                                        # имя — последним: всё, что после @@NAME, считается именем
                                        f"echo @@LAST; cat {BTOUT_LAST} 2>/dev/null; [ -f {BT_OFF} ] && echo @@KEEPOFF; echo @@NAME; cat {BTOUT_NAME} 2>/dev/null; true",
                                        check=False, timeout=20)[0])
        out, _, outinfo = out.partition("@@OUT")
        outinfo, _, last = outinfo.partition("@@LAST")
        last, _, saved = last.partition("@@NAME")
        keep_off = "@@KEEPOFF" in last
        last = last.replace("@@KEEPOFF", "")
        self._btout_saved_name = saved.strip()[:60]
        bluez, _, pair = out.partition("@@PAIR")
        self._btout_last_mac = next((w for w in last.split() if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", w)), None)
        adapter, devices = {}, []
        for path, ifaces in parse_bluez(bluez).items():
            if "org.bluez.Adapter1" in ifaces:
                adapter = ifaces["org.bluez.Adapter1"]
            d = ifaces.get("org.bluez.Device1")
            # только сопряжённые и подключённые: найденные поиском рядом bluez тоже помнит, но это не «свои» устройства
            if d and d.get("Address") and (d.get("Paired") or d.get("Connected")):
                battery = ifaces.get("org.bluez.Battery1", {}).get("Percentage")
                devices.append({"mac": d["Address"], "name": d.get("Alias") or d.get("Name") or "", "icon": d.get("Icon", ""),
                                "paired": bool(d.get("Paired")), "trusted": bool(d.get("Trusted")),
                                "connected": bool(d.get("Connected")), "battery": battery})
        pair_until = next((int(l) for l in pair.split() if l.isdigit()), None)
        devices.sort(key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))
        return {
            "powered": bool(adapter.get("Powered")), "keep_off": keep_off, "discoverable": bool(adapter.get("Discoverable")),
            "pairable": bool(adapter.get("Pairable")), "alias": adapter.get("Alias") or adapter.get("Name"),
            "pair_until": pair_until if pair_until and pair_until > time.time() else None,
            "devices": devices, "out": {**self._btout_info(outinfo, devices, self._btout_saved_name), **self._btout_last(devices)},
        }

    def _btout_volume_max(self):
        """Громкость Bluetooth-колонки в bluealsa всегда 100 % (0 дБ): иначе получается три регулятора.
        Остаются громкость Алисы и громкость на самой колонке."""
        out, _, _ = self.adb.shell("amixer -D bluealsa scontrols 2>/dev/null; true", check=False)
        m = re.search(r"Simple mixer control '([^']*A2DP)',0", strip_ansi(out))
        if not m:
            return
        mac = strip_ansi(self.adb.shell(f"cat {BTOUT_MAC} 2>/dev/null; true", check=False)[0]).strip()
        soft = (f"dbus-send --system --print-reply --dest=org.bluealsa /org/bluealsa/hci0/dev_{mac.replace(':', '_')}/a2dpsrc/sink "
                f"org.freedesktop.DBus.Properties.Set string:org.bluealsa.PCM1 string:SoftVolume variant:boolean:true >/dev/null 2>&1; "
                if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac) else "")
        self.adb.shell(f"{soft}amixer -D bluealsa sset {shlex.quote(m.group(1))} 100% >/dev/null", check=False)

    def btout_eq_status(self):
        """Текущий пресет эквалайзера и его АЧХ для графика (без записи на колонку)."""
        s = load_settings()
        preset = s.get("eq_preset") or "flat"
        if preset not in EQ_PRESETS and preset != "custom":
            preset = "flat"
        custom = s.get("eq_custom") or [dict(b) for b in EQ_CUSTOM_DEFAULT]
        freqs, db = eq_curve(preset, custom)
        return {"preset": preset, "presets": [*EQ_PRESETS, "custom"], "custom": custom,
                "freqs": [round(f, 1) for f in freqs], "db": db}

    def btout_eq(self, preset=None, custom=None):
        """Выбрать пресет (None — применить сохранённый) и положить коэффициенты для tap на колонке."""
        s = load_settings()
        if preset is None:
            preset = s.get("eq_preset") or "flat"
        preset = str(preset)
        if preset not in EQ_PRESETS and preset != "custom":
            raise AdbError("неизвестный пресет эквалайзера")
        if preset == "custom":
            custom = eq_custom_bands(custom if custom is not None else s.get("eq_custom"))
            s["eq_custom"] = custom
        else:
            custom = s.get("eq_custom") or [dict(b) for b in EQ_CUSTOM_DEFAULT]
        s["eq_preset"] = preset
        save_settings(s)
        filters = [eq_coeffs(b) for b in eq_filters(preset, custom)]
        # Запас до фильтров: учитываем суммарную АЧХ, в том числе перекрывающиеся
        # свои полосы. Масштабируем числитель первого биквада — работает и со старым vizd.
        _, response = eq_curve(preset, custom, points=1024)
        peak_db = max(response)
        preamp_db = -(peak_db + 0.5) if peak_db > 0 else 0.0
        if filters and preamp_db:
            gain = 10 ** (preamp_db / 20)
            b0, b1, b2, a1, a2 = filters[0]
            filters[0] = (b0 * gain, b1 * gain, b2 * gain, a1, a2)
        payload = {"enabled": bool(filters),
                   "filters": [dict(zip(("b0", "b1", "b2", "a1", "a2"), c)) for c in filters]}
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "eq.json"
            write_unix(f, json.dumps(payload, ensure_ascii=False))
            self.adb.shell(f"mkdir -p {VIZ_DIR}", check=False)
            self.adb.push(f, EQ_FILE + ".new")
        self.adb.shell(f"chmod 644 {EQ_FILE}.new; mv {EQ_FILE}.new {EQ_FILE}", check=False)
        st = self.btout_eq_status()
        st["preset"] = preset
        return st

    def _btout_last(self, devices):
        mac = getattr(self, "_btout_last_mac", None)
        if not mac:
            return {}
        dev = next((d for d in devices if d["mac"] == mac), {})
        return {"last": {"mac": mac, "name": dev.get("name") or getattr(self, "_btout_saved_name", "") or "Bluetooth-колонка"}} if dev else {}

    def _btout_switch(self, mount):
        """Переключаем выход сразу (а не ждём сторожа) и перезапускаем Алису. Играющая музыка при этом
        останавливается: продолжать её сами больше не пытаемся — после перезапуска это работало ненадёжно."""
        cmd = (f"grep -q ' /etc/asound.conf ' /proc/mounts || {{ mount --bind {BTOUT_CONF} /etc/asound.conf && echo changed; }}" if mount else
               f"grep -q ' /etc/asound.conf ' /proc/mounts && grep -q '{BTOUT_MARK}' /etc/asound.conf && "
               f"{{ umount /etc/asound.conf || umount -l /etc/asound.conf; echo changed; }}")
        out, _, _ = self.adb.shell(cmd + "; true", check=False)
        if "changed" not in out:     # выход и так тот — Алису зря не перезапускаем
            return False
        self.detach("quasar", f"{QUASAR}/q restart >/dev/null 2>&1")
        return True

    @staticmethod
    def _btout_info(text, devices, saved=""):
        words = text.split()
        mac = next((w for w in words if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", w)), None)
        if not mac:
            return {"on": False}
        dev = next((d for d in devices if d["mac"] == mac), {})
        state = "bt" if "bt" in words else "waiting"
        # имя — из списка устройств или запомненное при подключении; MAC-адрес людям не показываем
        return {"on": True, "mac": mac, "name": dev.get("name") or saved or "Bluetooth-колонка", "connected": bool(dev.get("connected")),
                "state": state, "watch": "watch" in words}

    # ---------- звук через Bluetooth-колонку ----------
    @staticmethod
    def _bt_mac(mac):
        mac = str(mac or "").upper()
        if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
            raise AdbError("плохой адрес устройства")
        return mac

    def btout_scan(self, seconds=12):
        """Ищем устройства рядом (колонку надо заранее перевести в режим сопряжения). Колонки и наушники — первыми."""
        seconds = max(5, min(20, int(seconds)))
        self.adb.shell(f"[ -f {BT_OFF} ] && {{ rm -f {BT_OFF}; {bt_power_sh(True)}; sleep 1; }}; "
                       f"bluetoothctl --timeout {seconds} scan on >/dev/null 2>&1; true", check=False, timeout=seconds + 17)
        found = []
        for path, ifaces in parse_bluez(strip_ansi(self.adb.shell(BLUEZ_DUMP + "; true", check=False, timeout=20)[0])).items():
            d = ifaces.get("org.bluez.Device1")
            if not d or not d.get("Address"):
                continue
            cls = d.get("Class") or 0
            audio = str(d.get("Icon") or "").startswith("audio") or (cls >> 8) & 0x1F == 4
            found.append({"mac": d["Address"], "name": d.get("Alias") or d.get("Name") or "", "audio": audio,
                          "paired": bool(d.get("Paired")), "connected": bool(d.get("Connected")), "rssi": d.get("RSSI"),
                          "icon": d.get("Icon") or ""})
        found.sort(key=lambda x: (not x["audio"], x["rssi"] is None, -(x["rssi"] or -999)))
        return {"devices": found}

    def btout_use(self, mac):
        """Сопрягаем (если ещё нет), доверяем, подключаем — и включаем сторож: он переводит звук на колонку."""
        mac = self._bt_mac(mac)
        has_loop = self.adb.shell("grep -q snd_aloop /proc/modules || find /lib/modules -name snd-aloop.ko | grep -q . && echo yes; true",
                                  check=False)[0]
        self.adb.shell(f"[ -f {BT_OFF} ] && {{ rm -f {BT_OFF}; {bt_power_sh(True)}; sleep 1; }}; true", check=False)  # был выключен
        if "yes" not in has_loop:
            raise AdbError("в прошивке этой колонки нет модуля snd-aloop — без него голос Алисы и музыка не смогут "
                           "звучать в Bluetooth-колонке одновременно")
        out, _, _ = self.adb.shell(
            # Quasar держит адаптер «Pairable: no» — сопряжение тогда без сохранения ключа (без bonding), и после
            # отключения колонка «забывается». На время сопряжения включаем pairable и возвращаем как было.
            f"bluetoothctl info {mac} 2>/dev/null | grep -q 'Bonded: yes' || {{ pb=$(bluetoothctl show | grep -c 'Pairable: yes'); "
            f"bluetoothctl pairable on >/dev/null; bluetoothctl info {mac} 2>/dev/null | grep -q 'Paired: yes' && "
            f"bluetoothctl remove {mac} >/dev/null 2>&1 && bluetoothctl --timeout 10 scan on >/dev/null 2>&1; "
            f"bluetoothctl --timeout 25 pair {mac} 2>&1 | tail -2; [ \"$pb\" = 1 ] || bluetoothctl pairable off >/dev/null; }}; "
            f"bluetoothctl trust {mac} >/dev/null 2>&1; bluetoothctl --timeout 15 connect {mac} 2>&1 | tail -1; sleep 2; "
            f"bluetoothctl info {mac} 2>/dev/null | grep -E 'Connected|Bonded|UUID: Audio Sink'; true", check=False, timeout=90)
        out = strip_ansi(out)
        if "Connected: yes" not in out:
            raise AdbError("колонка не подключилась: включите на ней режим сопряжения и попробуйте ещё раз")
        if "Audio Sink" not in out:
            raise AdbError("это устройство не принимает звук по Bluetooth (нет профиля A2DP)")
        name = next((i["org.bluez.Device1"].get("Alias") or i["org.bluez.Device1"].get("Name") or ""
                     for i in parse_bluez(strip_ansi(self.adb.shell(BLUEZ_DUMP + "; true", check=False, timeout=20)[0])).values()
                     if i.get("org.bluez.Device1", {}).get("Address") == mac), "")
        conf = self._original_bytes("/etc/asound.conf").decode("utf-8", "replace").rstrip("\n") + (
            f"\n\n{BTOUT_MARK} — смешивание в петлю snd-aloop, в Bluetooth-колонку переливает сторож\n"
            "pcm.!speakers {\n  type softvol\n  min_dB -60.0\n  slave.pcm \"playback_metrics_tap\"\n"
            "  control {\n    name \"Master Playback Volume\"\n    card 0\n  }\n}\n"
            "pcm.!dmix_spk_plug {\n  type plug\n  slave.pcm \"dmix_loop\"\n}\n"
            "pcm.dmix_loop {\n  type dmix\n  ipc_key 1031\n  ipc_key_add_uid false\n  ipc_perm 0666\n"
            "  slave {\n    pcm \"hw:Loopback,0,0\"\n    channels 2\n    rate 48000\n    buffer_size 7680\n    period_size 960\n  }\n}\n")
        script = (BTOUT_WATCH.replace("__DIR__", BTOUT_DIR).replace("__STATE__", BTOUT_STATE).replace("__MARK__", BTOUT_MARK)
                  .replace("__FWD__", BTOUT_FWD_SD).replace("__VIZ__", VIZD))
        with tempfile.TemporaryDirectory() as td:
            write_unix(Path(td) / "conf", conf)
            write_unix(Path(td) / "sh", script)
            write_unix(Path(td) / "mac", mac + "\n")
            write_unix(Path(td) / "name", re.sub(r"[\r\n]+", " ", name)[:60] + "\n")
            self.adb.shell(f"mkdir -p {BTOUT_DIR}")
            self.adb.push(Path(td) / "conf", BTOUT_CONF)
            self.adb.push(Path(td) / "sh", BTOUT_SH)
            self.adb.push(Path(td) / "mac", BTOUT_MAC)
            self.adb.push(Path(td) / "mac", BTOUT_LAST)
            self.adb.push(Path(td) / "name", BTOUT_NAME)
        self._ensure_hook()
        self.adb.shell(f"chmod 755 {BTOUT_SH}; start-stop-daemon -K -p {BTOUT_SD} 2>/dev/null; true", check=False)
        restarted = self._btout_switch(mount=True)
        # сторож дальше следит за подключением (подмена уже стоит — он её не трогает, пока колонка на связи)
        self.adb.shell(f"start-stop-daemon -S -b -m -p {BTOUT_SD} -x /bin/sh -- {BTOUT_SH}")
        self._btout_volume_max()   # громкость BT-колонки в bluealsa — всегда 100 %
        try:
            self.btout_eq()        # сохранённый пресет эквалайзера — в tap
        except Exception as e:     # noqa: BLE001 — без эквалайзера звук всё равно должен включиться
            JOURNAL.add(f"Эквалайзер не применён: {e}", "warn")
        return {**self.bt_state(), "restarted": restarted}

    def btout_off(self):
        """Встроенный динамик: убираем выбор — сторож снимет подмену, перезапустит Алису и выйдет."""
        # сторожа гасим сами и сразу возвращаем встроенный динамик (с продолжением музыки)
        self.adb.shell(f"rm -f {BTOUT_MAC} {BTOUT_STATE}; start-stop-daemon -K -p {BTOUT_SD} 2>/dev/null; "
                       f"p=$(cat {BTOUT_FWD_SD} 2>/dev/null) && {YAM2_KILL.replace('kill $p;', 'kill $p;')}; rm -f {BTOUT_FWD_SD}; true", check=False)
        restarted = self._btout_switch(mount=False)
        self._drop_hook_if_unused()
        return {**self.bt_state(), "restarted": restarted}

    def bt_action(self, data):
        act, mac = data.get("action"), str(data.get("mac") or "")
        if act in ("connect", "disconnect", "remove", "trust", "untrust"):
            if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                raise AdbError("неверный адрес устройства")
            # отключили или забыли колонку, через которую идёт звук, — значит, через неё больше не играем: звук сразу
            # в саму колонку, а сторож перестаёт её ждать (иначе раз в 30 с подключал бы её обратно)
            if act in ("disconnect", "remove") and self.adb.shell(f"cat {BTOUT_MAC} 2>/dev/null; true", check=False)[0].strip() == mac:
                self.btout_off()
            out, _, _ = self.adb.shell(f"bluetoothctl {act} {mac} 2>&1 | tail -n 2; true", timeout=30, check=False)
            out = strip_ansi(out)
            if re.search(r"Failed|not available|Error", out):
                msg = re.sub(r".*(Failed[^\n]*|Error[^\n]*).*", r"\1", out.strip().replace("\n", " "))
                raise AdbError({"connect": "Не удалось подключить", "disconnect": "Не удалось отключить",
                                "remove": "Не удалось забыть"}.get(act, "Ошибка") + f": {msg[:120]}")
        elif act == "power_off":
            if self.adb.shell(f"[ -f {BTOUT_MAC} ] && echo on; true", check=False)[0].strip() == "on":
                raise AdbError("Звук идёт через Bluetooth-колонку — сначала верните его в колонку")
            self.adb.shell(f"{bt_power_sh(False)} && mkdir -p {WEB_DIR} && touch {BT_OFF}; rm -f {self.BT_PAIR_FLAG}")
            try:
                self._ensure_hook()
            except AdbError as e:
                JOURNAL.add(f"Bluetooth выключен до перезагрузки: автозапуск не подключён ({e})", "warn")
        elif act == "power_on":
            self.adb.shell(f"rm -f {BT_OFF}; {bt_power_sh(True)}")
            self._drop_hook_if_unused()
        elif act == "pair_on":
            until = int(time.time()) + self.BT_PAIR_SECONDS
            self.adb.shell(f"rm -f {BT_OFF}; {bt_power_sh(True)}; bluetoothctl pairable on >/dev/null; bluetoothctl discoverable on >/dev/null; "
                           f"echo {until} > {self.BT_PAIR_FLAG}")
            # через 3 минуты сами скрываемся, если флаг не сменился (повторное включение продлевает)
            self.detach("bt_pair", f"sleep {self.BT_PAIR_SECONDS}; [ \"$(cat {self.BT_PAIR_FLAG} 2>/dev/null)\" = {until} ] && "
                                   f"{{ bluetoothctl discoverable off; bluetoothctl pairable off; rm -f {self.BT_PAIR_FLAG}; }}")
        elif act == "pair_off":
            self.adb.shell(f"bluetoothctl discoverable off >/dev/null; bluetoothctl pairable off >/dev/null; "
                           f"rm -f {self.BT_PAIR_FLAG}; true")
        else:
            raise AdbError("неизвестное действие")
        return self.bt_state()

    # ---------- файлы ----------
    @staticmethod
    def norm(path):
        return posixpath.normpath("/" + str(path or "").strip().lstrip("/"))

    def fs_list(self, path):
        path = self.norm(path)
        entries = []
        has_links = False
        for mode, size, mtime, name in self.adb.ls(path):
            if name in (".", ".."):
                continue
            kind = {0o040000: "dir", 0o120000: "link", 0o100000: "file"}.get(mode & 0o170000, "other")
            has_links |= kind == "link"
            entries.append({"name": name, "type": kind, "size": size, "mtime": mtime,
                            "mode": stat.filemode(mode)})
        if not entries and path != "/":
            # adb ls молча возвращает пусто и для несуществующих путей — уточняем.
            out, _, _ = self.adb.shell(f"[ -d {shlex.quote(path)} ] && echo dir || echo missing")
            if "missing" in out:
                raise AdbError(f"нет такой папки: {path}")
        if has_links:
            out, _, _ = self.adb.shell(
                f"cd {shlex.quote(path)} && for f in * .[!.]*; do [ -L \"$f\" ] && printf '%s\\t%s\\t%s\\n' "
                f"\"$([ -d \"$f\" ] && echo d || echo f)\" \"$f\" \"$(readlink \"$f\")\"; done; true")
            links = {}
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    links[parts[1]] = (parts[0] == "d", parts[2])
            for e in entries:
                if e["name"] in links:
                    e["is_dir"], e["target"] = links[e["name"]]
        for e in entries:
            e.setdefault("is_dir", e["type"] == "dir")
        out, _, _ = self.adb.shell(f"touch {shlex.quote(path)}/.webpanel_w 2>/dev/null && "
                                   f"rm -f {shlex.quote(path)}/.webpanel_w && echo rw || echo ro")
        return {"path": path, "writable": out.strip() == "rw", "entries": entries}

    def fs_read(self, path, limit=512 * 1024):
        path = self.norm(path)
        # head в /tmp, а потом pull — так работает и для огромных файлов, и для /proc, /sys.
        tmp = "/tmp/webpanel_preview"
        _, err, rc = self.adb.shell(f"head -c {limit + 1} {shlex.quote(path)} > {tmp}", check=False)
        if rc != 0:
            raise AdbError(err.strip() or "не удалось прочитать файл")
        data = self.adb.read_bytes(tmp)
        self.adb.shell(f"rm -f {tmp}")
        truncated = len(data) > limit
        data = data[:limit]
        try:
            if b"\0" in data:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "nul")
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # Бинарник: отдаём hex-дамп первых 4 КБ.
            head = data[:4096]
            lines = []
            for i in range(0, len(head), 16):
                chunk = head[i:i + 16]
                hx = " ".join(f"{b:02x}" for b in chunk)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                lines.append(f"{i:08x}  {hx:<47}  {asc}")
            return {"binary": True, "text": "\n".join(lines), "truncated": len(data) > 4096}
        return {"binary": False, "text": text, "truncated": truncated}

    def fs_write(self, path, text):
        path = self.norm(path)
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f"
            f.write_bytes(text.encode("utf-8"))
            self.adb.push(f, path)
        return {"ok": True}

    def fs_upload(self, directory, name, body):
        name = posixpath.basename(name)
        if not name or name in (".", ".."):
            raise AdbError("плохое имя файла")
        dest = posixpath.join(self.norm(directory), name)
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f"
            f.write_bytes(body)
            self.adb.push(f, dest, timeout=600)
        return {"ok": True, "path": dest}

    def _guard(self, path):
        path = self.norm(path)
        if path.count("/") < 2 or path in ("/bkupgrade/data", "/data/quasar"):
            raise AdbError(f"{path}: слишком важный путь, используйте консоль")
        return path

    def fs_delete(self, path):
        path = self._guard(path)
        _, err, rc = self.adb.shell(f"rm -rf {shlex.quote(path)}", check=False)
        if rc != 0:
            raise AdbError(err.strip() or "не удалось удалить")
        return {"ok": True}

    def fs_rename(self, path, new_name):
        path = self._guard(path)
        new_name = str(new_name).strip()
        if not new_name or "/" in new_name or new_name in (".", ".."):
            raise AdbError("плохое имя")
        dest = posixpath.join(posixpath.dirname(path), new_name)
        _, err, rc = self.adb.shell(f"mv -n {shlex.quote(path)} {shlex.quote(dest)}", check=False)
        if rc != 0:
            raise AdbError(err.strip() or "не удалось переименовать")
        return {"ok": True}

    def fs_mkdir(self, path):
        path = self.norm(path)
        _, err, rc = self.adb.shell(f"mkdir -p {shlex.quote(path)}", check=False)
        if rc != 0:
            raise AdbError(err.strip() or "не удалось создать папку")
        return {"ok": True}

    def fs_play(self, path):
        return self._play_path(self.norm(path))

    def fs_download(self, path):
        """Возвращает (имя, локальный файл, tmpdir). Папки упаковываются в tar.gz на компьютере."""
        path = self.norm(path)
        name = posixpath.basename(path) or "root"
        td = tempfile.mkdtemp(prefix="webpanel_")
        local = Path(td) / name
        self.adb.pull(path, local, timeout=900)
        if local.is_dir():
            arc = Path(td) / f"{name}.tar.gz"
            with tarfile.open(arc, "w:gz") as tf:
                tf.add(local, arcname=name)
            return arc.name, arc, td
        return name, local, td

    # ---------- логи / консоль / система ----------
    def logs_list(self):
        out, _, _ = self.adb.shell(f"ls {LOGS_DIR}")
        return ["dmesg"] + sorted(strip_ansi(out).split())

    def log(self, name, lines):
        lines = max(10, min(5000, int(lines)))
        if name == "panel":   # журнал самой панели — на компьютере, не на колонке
            return {"text": JOURNAL.text(lines) or "Журнал пуст."}
        if name == "dmesg":
            cmd = f"dmesg | tail -n {lines}"
        elif name in self.logs_list():
            cmd = f"tail -n {lines} {LOGS_DIR}/{shlex.quote(name)}"
        else:
            raise AdbError("нет такого лога")
        out, _, _ = self.adb.shell(cmd, timeout=20)
        return {"text": strip_ansi(out)}

    def system(self, action):
        if action == "quasar_restart":
            self.detach("quasar", f"{QUASAR}/q restart >/dev/null 2>&1")
        elif action == "reboot":
            self.detach("reboot", "sleep 1; reboot")
        else:
            raise AdbError("неизвестное действие")
        return {"ok": True}


class WsServerConn:
    """Серверная сторона WebSocket (RFC 6455) поверх соединения http.server — только то, что нужно терминалу."""
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, handler):
        self.rf, self.wf = handler.rfile, handler.wfile
        self.lock = threading.Lock()

    @classmethod
    def accept_key(cls, key):
        return base64.b64encode(hashlib.sha1((key + cls.GUID).encode()).digest()).decode()

    def _read(self, n):
        b = self.rf.read(n)
        if len(b) < n:
            raise ConnectionError("closed")
        return b

    def recv(self):
        """Следующее сообщение (opcode, bytes) или None, если соединение закрыто."""
        buf, op = b"", None
        try:
            while True:
                h = self._read(2)
                fin, opcode, masked, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x80, h[1] & 0x7F
                if n == 126:
                    n = int.from_bytes(self._read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self._read(8), "big")
                if n > 1 << 20:
                    return None
                mask = self._read(4) if masked else b"\0\0\0\0"
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(self._read(n))) if n else b""
                if opcode == 8:
                    return None
                if opcode == 9:
                    self.send(data, 10)
                    continue
                if opcode == 10:
                    continue
                if opcode:
                    op = opcode
                buf += data
                if fin:
                    return op, buf
        except (ConnectionError, OSError, ValueError):
            return None

    def send(self, data, opcode=2):
        n = len(data)
        head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big")
                                          if n < 1 << 16 else bytes([127]) + n.to_bytes(8, "big"))
        with self.lock:
            self.wf.write(head + data)

    def close(self):
        try:
            self.send(b"", 8)
        except OSError:
            pass


class Terminal:
    """Интерактивная оболочка колонки для вкладки «Консоль»: adb shell с обычными каналами.

    Псевдотерминал adbd этой прошивки выделяет на колонке сам (поэтому работают Tab, Ctrl+C, top), а локальный
    не нужен — так консоль работает и на Windows. Shell v2 adbd не знает, поэтому размер окна задаём сами:
    при старте — stty в той же команде, при изменении — отдельным adb shell (stty -F на pts сеанса и SIGWINCH).
    """
    MAX = 4
    active = 0
    lock = threading.Lock()
    PS1 = r"\[\e[1;32m\]root@lite2\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]# "

    def __init__(self, adb, ws, cols, rows):
        self.adb, self.ws = adb, ws
        self.sid = os.urandom(4).hex()
        self.info = f"/tmp/webpanel_term_{self.sid}"
        cols, rows = max(20, min(400, cols)), max(5, min(200, rows))
        cmd = (f"echo $$ $(tty) > {self.info}; stty rows {rows} cols {cols} 2>/dev/null; "
               f"export TERM=xterm-256color PS1={shlex.quote(self.PS1)}; {YAM_PATH}cd /data 2>/dev/null; exec sh")
        self.proc = adb.popen(cmd, pty=True, cols=cols, rows=rows)

    def _pump(self):
        try:
            while True:
                data = self.proc.stdout.read(65536)    # небуферизованный канал: отдаёт то, что уже пришло
                if not data:
                    break
                self.ws.send(data)
        except (OSError, ValueError):
            pass
        self.ws.close()

    def resize(self, cols, rows):
        cols, rows = max(20, min(400, int(cols))), max(5, min(200, int(rows)))
        self.adb.shell(f"read p t < {self.info} && stty -F $t rows {rows} cols {cols} && kill -WINCH $p; true",
                       check=False, timeout=10)

    def run(self):
        threading.Thread(target=self._pump, daemon=True, name="term-out").start()
        try:
            while True:
                msg = self.ws.recv()
                if msg is None:
                    break
                try:
                    m = json.loads(msg[1])
                except ValueError:
                    continue
                if m.get("t") == "i" and isinstance(m.get("d"), str):
                    self.proc.stdin.write(m["d"].encode())
                    self.proc.stdin.flush()
                elif m.get("t") == "r":
                    threading.Thread(target=self.resize, args=(m.get("c", 80), m.get("r", 24)), daemon=True).start()
        except OSError:
            pass
        finally:
            self.proc.terminate()       # adb закрывает соединение — оболочка на колонке получает SIGHUP
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.adb.shell(f"rm -f {self.info}", check=False, timeout=10)


class Handler(BaseHTTPRequestHandler):
    panel: Panel = None
    access = None            # access.PanelAccess
    updater = None           # PanelUpdater
    ports = {}          # {"http": 8765, "https": 8766} — для подсказок в интерфейсе

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # вкладку закрыли или перезагрузили, пока колонка отвечала: ответ некому отдать, это не ошибка
            self.close_connection = True

    # ---------- доступ ----------
    @property
    def https(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def _token(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001
            return None
        m = c.get(access.COOKIE)
        return m.value if m else None

    def _cookie(self, token):
        attrs = "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.https else "")
        if token:
            return ("Set-Cookie", f"{access.COOKIE}={token}; Max-Age={access.SESSION_TTL}; {attrs}")
        return ("Set-Cookie", f"{access.COOKIE}=; Max-Age=0; {attrs}")

    def _guard(self, path):
        """True — запрос уже отклонён. POST с чужого сайта (CSRF) режем всегда, даже без пароля:
        иначе любая открытая вкладка могла бы дёрнуть /api/shell на localhost."""
        # Optional private worker: local listener + per-run key, never a LAN bypass.
        gateway_key = os.environ.get("PANEL_GATEWAY_SECRET")
        if gateway_key:
            import hmac
            local = ipaddress.ip_address(self.connection.getsockname()[0])
            if local.is_loopback and hmac.compare_digest(self.headers.get("X-Panel-Gateway", ""), gateway_key):
                return False
            self._send(403, {"error": "private worker"})
            return True
        if self.command == "POST":
            origin = self.headers.get("Origin")
            # «null» шлют изолированные iframe и т. п. — с панели такого не бывает, а чужая страница так обошла бы проверку
            if origin and (origin == "null" or urlparse(origin).netloc != self.headers.get("Host")):
                self._send(403, {"error": "запрос с чужого сайта"})
                return True
            # Панель шлёт тело только как JSON или файл. «Простые» типы (форма, text/plain) браузер отправит
            # с чужого сайта без предварительного запроса — их не принимаем, даже если Origin не пришёл.
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if int(self.headers.get("Content-Length") or 0) and ctype not in ("application/json", "application/octet-stream"):
                self._send(415, {"error": "тело запроса — только JSON или файл"})
                return True
        if path in PUBLIC_PATHS or path.startswith("/static/") or self.access.session_ok(self._token()):
            return False
        self._send(401, {"error": "Нужен вход", "auth": True})
        return True

    def _panel_auth_get(self):
        return {**self.access.info(self._token()), "secure": self.https, "ports": self.ports,
                "ca": (TLS_DIR / "ca.crt").exists()}

    def _panel_auth_post(self, path, d):
        a, tok, ua = self.access, self._token(), self.headers.get("User-Agent")
        try:
            if path == "/api/panel-auth/login":
                new = a.login(self.client_address[0], d.get("password"), ua)
                return self._send(200, {"ok": True}, headers=[self._cookie(new)])
            if path == "/api/panel-auth/logout":
                a.logout(tok)
                return self._send(200, {"ok": True}, headers=[self._cookie(None)])
            if path == "/api/panel-auth/password":
                new = a.set_password(d.get("current"), d.get("password"), ua)
                return self._send(200, {"ok": True}, headers=[self._cookie(new)])
            if path == "/api/panel-auth/disable":
                a.disable(d.get("current"))
                return self._send(200, {"ok": True}, headers=[self._cookie(None)])
            if path == "/api/panel-auth/logout-others":
                return self._send(200, {"ok": True, "closed": a.logout_others(tok)})
        except access.AccessError as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self):
        b = self._body()
        return json.loads(b) if b else {}

    # Поиск колонок идёт в фоне (секунды), экран подключения опрашивает ход: GET /api/conn/find
    finder = {"running": False, "stage": "", "done": 0, "total": 0, "nets": [], "found": [], "error": None, "auto": None, "t": 0}
    finder_lock = threading.Lock()

    def _conn_find(self, start=False):
        """Колонки в локальных сетях этого компьютера — то же, что --find, но из панели."""
        f = Handler.finder
        if start:
            if os.environ.get("PANEL_GATEWAY_SECRET"):
                raise AdbError("в режиме нескольких колонок адреса задаются в speakers.json")
            with Handler.finder_lock:
                if not f["running"]:
                    f.update(running=True, stage="nets", done=0, total=0, nets=[], found=[], error=None, auto=None, t=time.time())
                    threading.Thread(target=self._conn_find_run, daemon=True, name="find").start()
        current = self.panel.adb.serial
        return {**f, "current": current, "found": [{**x, "current": x["serial"] == current} for x in f["found"]]}

    def _conn_find_run(self):
        import profiles
        f = Handler.finder
        try:
            nets = local_networks()
            if not nets:
                raise AdbError("не нашлось домашней сети: компьютер не в локальной сети, или панель в Docker без сети хоста")
            f["nets"] = [str(n) for n in nets]
            found = find_devices(self.panel.adb.adb, nets, lambda stage, done, total: f.update(stage=stage, done=done, total=total))
            f["found"] = [{"serial": s, "ip": s.rpartition(":")[0], "model": m, "name": profiles.PROFILES[m]["name"]}
                          for s, m, _ in found]
            JOURNAL.add(f"поиск колонок в {', '.join(f['nets'])}: найдено {len(found)}")
            # первый запуск без адреса и колонка в сети одна — подключаемся сами, даже если страницу закрыли
            if not self.panel.adb.serial and len(found) == 1:
                f["auto"] = found[0][0]
                self._conn_use(found[0][0])
        except Exception as e:  # noqa: BLE001
            f["error"] = str(e)
            JOURNAL.add(f"поиск колонок: {e}", "warn")
        finally:
            f.update(running=False, stage="done")

    def _conn_use(self, serial):
        """Подключиться к другой колонке: запоминаем адрес и перезапускаем панель с ним."""
        if os.environ.get("PANEL_GATEWAY_SECRET"):
            raise AdbError("в режиме нескольких колонок адреса задаются в speakers.json")
        serial = normalize_device(serial)
        if not serial or not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}:\d{1,5}", serial) \
                or not ipaddress.ip_address(serial.rpartition(":")[0]).is_private:
            raise AdbError("нужен IP колонки в домашней сети, например 192.168.1.50")
        save_settings({**load_settings(), "device": serial})
        JOURNAL.add(f"выбрана колонка {serial} — перезапуск панели")
        threading.Timer(0.5, restart_self, kwargs={"device": serial}).start()
        return {"ok": True, "serial": serial}

    def _report(self):
        """Отчёт для Issue: окружение, колонка, хук, своё на колонке и хвост журнала — без личного (redact)."""
        import profiles
        p, out = self.panel, []
        def row(name, fn):
            try:
                out.append(f"- {name}: {fn()}")
            except Exception as e:  # noqa: BLE001 — отчёт собираем даже наполовину
                out.append(f"- {name}: не удалось узнать ({type(e).__name__}: {e})")
        model, auto = PANEL_PROFILE
        out.append("## Панель")
        row("Версия", lambda: f"{VERSION}, установка: {self.updater.kind() if self.updater else '?'}")
        row("Профиль", lambda: f"{profiles.PROFILES[model]['name']} ({model}{', определён сам' if auto else ', задан вручную'})")
        row("Система", lambda: f"{platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}")
        row("adb", lambda: ".".join(map(str, adb_version(p.adb.adb) or ())) or "?")
        row("ffmpeg", lambda: "есть" if p.ffmpeg else "нет")
        row("Вход в Яндекс", lambda: "есть" if AUTH_FILE.exists() else "нет")
        out.append("\n## Колонка")
        row("Связь", lambda: f"{p.conn.state}, попыток подключения: {p.conn.attempts}" if p.conn else "?")
        if p.conn and p.conn.online:
            row("Прошивка", lambda: p.version().get("quasar_version"))
            row("Плата и ядро", lambda: " · ".join(x for x in strip_ansi(p.adb.shell(
                "cat /proc/device-tree/model 2>/dev/null | tr -d '\\000'; echo; uname -rm; "
                "df -h /data 2>/dev/null | tail -1 | awk '{print \"свободно в /data: \" $4}'", check=False)[0]).splitlines() if x.strip()))
            def hook():
                h = p.hook_status()
                state = "подключён" + ("" if h["fresh"] else ", устарел") if h["hook"] else "не подключён"
                return (f"{state}; держится на нём: {', '.join(h['users']) or 'ничего'}"
                        + ("" if h["possible"] else "; rootkeeper.sh нет, /etc/init.d только для чтения")
                        + (f"; через {h['file']}" if h["hook"] else ""))
            row("Хук загрузки", hook)
            row("Своё на колонке", lambda: ", ".join(f"{i['name']}" + (f" ({i['detail']})" if i.get("detail") else "")
                                                     for i in p.footprint()["items"] if i["on"]) or "ничего")
        out.append("\n## Журнал панели (последние 150 записей)\n```\n" + (JOURNAL.text(150) or "пусто") + "\n```")
        return {"text": redact("\n".join(out)) + "\n"}

    # Действия, которые шлются потоком (ползунки, живой предпросмотр): в журнал — только ошибки
    JOURNAL_QUIET = {"/api/volume", "/api/ring", "/api/clock", "/api/radio/miss-seen", "/api/music/cmd"}
    # Что из тела запроса можно показать в журнале: имена и режимы, но не тексты, ключи и пароли
    JOURNAL_KEYS = ("name", "kind", "action", "which", "mode", "enabled", "id", "type")

    def _journal(self, error=None, started=None):
        path = urlparse(self.path).path
        if self.command == "GET" and not error:
            return
        if not error and path in self.JOURNAL_QUIET:
            return
        d = getattr(self, "_jdata", None) or {}
        detail = ", ".join(f"{k}={str(d[k])[:60]}" for k in self.JOURNAL_KEYS
                           if isinstance(d.get(k), (str, int, bool)) and not isinstance(d.get(k), float))
        name = unquote(self.headers.get("X-Sound") or self.headers.get("X-Filename") or "")
        detail = ", ".join(x for x in (detail, name and f"файл={name[:60]}") if x)
        took = f" за {time.time() - started:.1f} с" if started and time.time() - started >= 1 else ""
        head = f"{self.command} {path}" + (f" ({detail})" if detail else "")
        JOURNAL.add(f"{head}: {error}" if error else f"{head} — ок{took}", "error" if error else "info")

    def _handle(self, fn):
        started, error = time.time(), None
        try:
            self._send(200, fn())
        except (AdbError, glagol.AuthError, ValueError, KeyError) as e:
            error = f"{e}" if not isinstance(e, KeyError) else f"нет поля {e}"
            self._send(400, {"error": str(e)})
        except subprocess.TimeoutExpired as e:
            error = f"таймаут: {e}"
            self._send(504, {"error": error})
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            self._send(500, {"error": error})
        try:   # журнал — никогда не причина для ошибки ответа
            self._journal(error, started)
        except Exception:  # noqa: BLE001
            pass

    def _wakeword_proxy(self, method, path, body=None):
        """Прокси API слов активации: JSON и WAV отдаются как есть (WAV — с его Content-Type)."""
        try:
            data, ctype = self.panel.wakeword_proxy(method, path, body)
        except (AdbError, subprocess.TimeoutExpired) as e:
            return self._send(400, {"error": str(e)})
        if isinstance(data, (dict, list)):
            return self._send(200, data)
        return self._send(200, data, ctype or "application/octet-stream")

    def _download(self, path):
        try:
            name, local, td = self.panel.fs_download(path)
        except (AdbError, subprocess.TimeoutExpired) as e:
            return self._send(400, {"error": str(e)})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(local.stat().st_size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
            self.end_headers()
            with open(local, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 16)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def _terminal(self, q):
        """GET /api/term — апгрейд до WebSocket и интерактивная оболочка на колонке (root)."""
        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            return self._send(400, {"error": "нужен WebSocket"})
        # Браузер шлёт cookie и на WebSocket с чужого сайта — проверяем Origin, иначе любая страница получила бы root.
        origin = self.headers.get("Origin")
        if not origin or urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, {"error": "запрос с чужого сайта"})
        with Terminal.lock:
            if Terminal.active >= Terminal.MAX:
                return self._send(429, {"error": "слишком много открытых консолей"})
            Terminal.active += 1
        try:
            self.protocol_version = "HTTP/1.1"   # браузеры принимают апгрейд только в ответе HTTP/1.1
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", WsServerConn.accept_key(key))
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            ws = WsServerConn(self)
            Terminal(self.panel.adb, ws, int(q.get("cols") or 100), int(q.get("rows") or 30)).run()
        finally:
            with Terminal.lock:
                Terminal.active -= 1

    def _offline(self, path):
        """Пока колонки нет, API отвечает одним понятным статусом вместо ошибок adb."""
        # отчёт для Issue нужнее всего как раз тогда, когда колонка не подключается
        if path.startswith("/api/") and not path.startswith("/api/conn") and path != "/api/report" and not self.panel.conn.online:
            self._send(503, {"error": "Колонка не подключена", "offline": True})
            return True
        return False

    def do_GET(self):
        u = urlparse(self.path)
        if self._guard(u.path):
            return
        if u.path == "/api/panel-auth":
            return self._send(200, self._panel_auth_get())
        if u.path == "/manifest.webmanifest":
            return self._send(200, (STATIC / "manifest.webmanifest").read_bytes(), MIME[".webmanifest"])
        if u.path == "/sw.js":
            # Service worker отдаём из корня, чтобы его область была вся панель.
            return self._send(200, (STATIC / "sw.js").read_bytes(), MIME[".js"])
        if u.path == "/ca.crt":
            f = TLS_DIR / "ca.crt"
            if not f.exists():
                return self._send(404, {"error": "HTTPS не включён"})
            return self._send(200, f.read_bytes(), "application/x-x509-ca-cert",
                              headers=[("Content-Disposition", 'attachment; filename="stantsiya-panel-ca.crt"')])
        if u.path == "/api/conn":
            return self._send(200, self.panel.conn.info())
        if u.path == "/api/conn/find":
            return self._handle(self._conn_find)
        if u.path == "/api/midi/tests":
            # The saved owner observations remain readable without the speaker.
            return self._handle(self.panel.midi_tests)
        if self._offline(u.path):
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/term":
            return self._terminal(q)
        if u.path == "/api/wakeword/proxy":
            return self._wakeword_proxy("GET", q.get("path", ""))
        p = self.panel
        routes = {
            "/api/status": p.status,
            "/api/midi/hardware": p.midi_hardware,
            "/api/midi/tests": p.midi_tests,
            "/api/midi/tests/clock": p.midi_clock_tests,
            "/api/wakeword/status": p.wakeword_status,
            "/api/wakeword/actions": p.wakeword_actions,
            "/api/mute": lambda: {"muted": bool(load_settings().get("mute_before")), "volume": p._volume_level()},
            "/api/sounds": p.sounds,
            "/api/sounds/custom": p.sounds_custom,
            "/api/patterns": p.patterns,
            "/api/logs": p.logs_list,
            "/api/log": lambda: p.log(q.get("name", ""), q.get("lines", 300)),
            "/api/devices": lambda: {"text": p.adb.devices()},
            "/api/fs/list": lambda: p.fs_list(q.get("path", "/")),
            "/api/fs/read": lambda: p.fs_read(q.get("path", "")),
            "/api/clock/frames": lambda: p.clock_frames(q.get("name", "")),
            "/api/clock/boot": p.boot_status,
            "/api/clock/state": p.clock_state,
            "/api/leds/live": p.leds.get,
            "/api/display": p.display_status,
            "/api/ring/frames": lambda: p.ring_frames(q.get("name", "")),
            "/api/anim/custom": p.anim_custom,
            "/api/hook": p.hook_status,
            "/api/agent": p.agent_status,
            "/api/report": self._report,
            "/api/anim/original": lambda: p.anim_original(q.get("kind", ""), q.get("name", "")),
            "/api/led/patterns": p.led_patterns,
            "/api/yam/status": p.yam_status,
            "/api/music": p.music,
            "/api/music/info": lambda: p.music_info(q.get("id", "")),
            "/api/music/lyrics": lambda: p.music_lyrics(q.get("id", "")) if q.get("id") else
                p.ymusic.lyrics_by_name(q.get("title", ""), q.get("artist", ""), q.get("album"), float(q.get("duration") or 0)),
            "/api/music/queue": p.music_queue,
            "/api/music/similar": lambda: (p._need_login(), p.ymusic.similar(q.get("id", "")))[1],
            "/api/music/artist": lambda: (p._need_login(), p.ymusic.artist(q.get("id", "")))[1],
            "/api/auth": lambda: {**p.auth.info(), "oauth_url": glagol.OAUTH_URL},
            "/api/auth/qr": p.auth_qr_status,
            "/api/yam/registry": lambda: p.yam_registry(q.get("refresh") == "1"),
            "/api/yam/plugins": p.yam_plugins,
            "/api/yam/logs": lambda: p.yam_logs(q.get("id", "")),
            "/api/yam/settings": p.yam_settings,
            "/api/yam/version": lambda: p.yam_version(q.get("refresh") == "1"),
            "/api/yam2/status": lambda: p.yam2_status(q.get("refresh") == "1"),
            "/api/yam2/packages": p.yam2_packages,
            "/api/yam2/catalog": lambda: p.yam2_catalog(q.get("refresh") == "1"),
            "/api/yam2/log": lambda: p.yam2_service_log(q.get("id", "")),
            "/api/luna/plugins": p.luna_plugins,
            "/api/luna/log": lambda: p.luna_logs(q.get("id", "")),
            "/api/alarms": p.alarms,
            "/api/scenes": p.scenes,
            "/api/footprint": p.footprint,
            "/api/privacy": p.privacy_status,
            "/api/assistant": p.assistant_status,
            "/api/viz": p.viz_status,
            "/api/radio": p.radio_status,
            "/api/radio/search": lambda: p.radio_search(q.get("q", ""), q.get("country", "")),
            "/api/panel/version": lambda: self.updater.check(q.get("refresh") == "1"),
            "/api/bt": p.bt_state,
            "/api/btout/eq": p.btout_eq_status,
            "/api/metrics": lambda: p.metrics.get(q.get("range", 3600)),
        }
        if u.path in routes:
            return self._handle(routes[u.path])
        if u.path == "/api/fs/download":
            return self._download(q.get("path", ""))
        if u.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/radio/logo":
            try:
                logo = self.panel.radio_logo(q.get("name", ""))
            except AdbError as e:
                return self._send(400, {"error": str(e)})
            if not logo:
                return self._send(404, {"error": "логотип не найден"})
            return self._send(200, logo[0], logo[1], headers=[("Content-Security-Policy", "default-src 'none'")])
        if u.path.startswith("/static/"):
            f = (STATIC / unquote(u.path[len("/static/"):])).resolve()
            if f.is_file() and STATIC in f.parents:
                return self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if self._guard(u.path):
            return
        if u.path.startswith("/api/panel-auth/"):
            try:
                d = self._json()
            except ValueError:
                d = {}
            return self._panel_auth_post(u.path, d)
        if u.path == "/api/conn/retry":
            return self._send(200, self.panel.conn.retry_now())
        if u.path == "/api/conn/find":
            return self._handle(lambda: self._conn_find(start=True))
        if u.path == "/api/conn/use":
            try:
                d = self._json()
            except ValueError:
                d = {}
            self._jdata = d if isinstance(d, dict) else {}
            return self._handle(lambda: self._conn_use(self._jdata.get("serial")))
        if self._offline(u.path):
            return
        p = self.panel
        if u.path == "/api/upload":
            name = unquote(self.headers.get("X-Filename", "upload.bin"))
            body = self._body()
            return self._handle(lambda: p.play_upload(name, body))
        if u.path == "/api/sounds/replace":
            name = unquote(self.headers.get("X-Sound", ""))
            body = self._body()
            return self._handle(lambda: p.sound_replace(name, body))
        if u.path == "/api/led/gif":
            return self._handle(lambda: p.led_gif(self._body()))
        if u.path == "/api/boot/sound":
            name = unquote(self.headers.get("X-Filename", "sound"))
            body = self._body()
            return self._handle(lambda: p.boot_sound_install(name, body))
        if u.path == "/api/alarms/sound":
            name = unquote(self.headers.get("X-Filename", "sound"))
            body = self._body()
            return self._handle(lambda: p.alarm_sound_upload(name, body))
        if u.path == "/api/backup/restore":
            body = self._body()
            return self._handle(lambda: p.backup_restore(body))
        if u.path == "/api/backup":
            try:
                name, data = p.backup(self._json().get("ui"))
            except (AdbError, subprocess.TimeoutExpired, ValueError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, data, "application/gzip",
                              headers=[("Content-Disposition", f'attachment; filename="{name}"')])
        if u.path == "/api/fs/upload":
            name = unquote(self.headers.get("X-Filename", ""))
            directory = unquote(self.headers.get("X-Dir", "/tmp"))
            body = self._body()
            return self._handle(lambda: p.fs_upload(directory, name, body))
        if u.path == "/api/luna/install":
            name = unquote(self.headers.get("X-Filename", ""))
            body = self._body()
            return self._handle(lambda: p.luna_install(body, name))
        d = self._json()
        self._jdata = d if isinstance(d, dict) else {}
        if u.path == "/api/wakeword/proxy":
            return self._wakeword_proxy(str(self._jdata.get("method", "POST")).upper(),
                                        str(self._jdata.get("path", "")), self._jdata.get("body"))
        routes = {
            "/api/volume": lambda: p.set_volume(d),
            "/api/midi/tests/run": lambda: p.midi_test_run(d),
            "/api/midi/tests/observe": lambda: p.midi_test_observe(d),
            "/api/midi/tests/restore": p.midi_test_restore,
            "/api/mute": lambda: p.set_mute(bool(d.get("on"))),
            "/api/wakeword/install": p.wakeword_install,
            "/api/wakeword/start": p.wakeword_start,
            "/api/wakeword/stop": p.wakeword_stop,
            "/api/wakeword/actions": lambda: p.wakeword_actions_set(d.get("word"), d.get("action"), d.get("scene")),
            "/api/wakeword/autostart": lambda: p.wakeword_autostart(bool(d.get("on"))),
            "/api/play": lambda: p.play_system(d["name"]),
            "/api/sounds/reset": lambda: p.sound_reset(d["name"]),
            "/api/sounds/play-original": lambda: p.sound_play_original(d["name"]),
            "/api/stop": p.stop_sound,
            "/api/pattern": lambda: p.play_pattern(d["kind"], d["name"], d.get("seconds", 5)),
            "/api/pattern/stop": lambda: p.stop_pattern(d.get("kind")),
            "/api/ring": lambda: p.ring(d),
            "/api/logo": lambda: p.logo(d),
            "/api/logo/reset": p.logo_reset,
            "/api/led": lambda: p.led(d),
            "/api/agent/install": p.agent_install,
            "/api/agent/remove": p.agent_remove,
            "/api/clock": lambda: p.clock(d),
            "/api/clock/play": lambda: p.play_clock(d["frames"], d.get("seconds", 10)),
            "/api/anim/install": lambda: p.anim_install(d.get("kind"), d.get("name"), d.get("frames"), d.get("text")),
            "/api/anim/reset": lambda: p.anim_reset(d.get("kind"), d.get("name")),
            "/api/anim/my": lambda: p.my_anim_save(d.get("kind"), d.get("name"), d.get("frames"), d.get("text")),
            "/api/anim/my/delete": lambda: p.my_anim_delete(d.get("kind"), d.get("name")),
            "/api/hook/repair": lambda: p.hook_repair(bool(d.get("remount"))),
            "/api/ring/try": lambda: p.ring_try(d.get("text"), d.get("seconds", 5)),
            "/api/clock/boot": lambda: p.boot_install(d["frames"]),
            "/api/clock/boot/reset": p.boot_reset,
            "/api/boot/sound/reset": p.boot_sound_reset,
            "/api/boot/sound/play": lambda: p.boot_sound_play(d.get("which", "default")),
            "/api/clock/boot/always": lambda: p.boot_always(bool(d["enabled"])),
            "/api/yam/start": p.yam_start,
            "/api/yam/install-daemon": lambda: p.yam_install_daemon(**({"drop_backups": True} if d.get("drop_backups") else {})),
            "/api/display": lambda: p.display_set(d),
            "/api/display/stop": p.display_stop,
            "/api/ring/reset": p.ring_reset,
            "/api/music/cmd": lambda: p.music_cmd(d),
            "/api/music/like": lambda: p.music_like(d["id"], bool(d.get("like", True))),
            "/api/music/dislike": lambda: p.music_dislike(d["id"]),
            "/api/music/play": lambda: p.music_play(d["id"], d.get("type", "track")),
            "/api/auth/qr": p.auth_qr_start,
            "/api/auth/oauth": lambda: p.auth_oauth(d["url"]),
            "/api/auth/logout": p.auth_logout,
            "/api/yam/autostart": lambda: p.yam_autostart(bool(d["enabled"])),
            "/api/yam/install": lambda: p.yam_install(d["id"], d["version"]),
            "/api/yam/action": lambda: p.yam_action(d["action"], d["id"]),
            "/api/yam/settings": lambda: p.yam_settings(d),
            "/api/yam2/install": p.yam2_install,
            "/api/yam2/update": p.yam2_update,
            "/api/yam2/uninstall": p.yam2_uninstall,
            "/api/yam2/registry": p.yam2_registry_add,
            "/api/yam2/doctor": lambda: p.yam2_doctor(bool(d.get("repair"))),
            "/api/yam2/pkg": lambda: p.yam2_pkg(d.get("action"), d["name"], bool(d.get("yes"))),
            "/api/yam2/service": lambda: p.yam2_service(d["id"], d["action"], bool(d.get("stop_legacy"))),
            "/api/luna/action": lambda: p.luna_action(d.get("action"), d.get("id")),
            "/api/bt": lambda: p.bt_action(d),
            "/api/btout/scan": lambda: p.btout_scan(d.get("seconds", 12)),
            "/api/btout/use": lambda: p.btout_use(d.get("mac")),
            "/api/btout/off": p.btout_off,
            "/api/btout/eq": lambda: p.btout_eq(d.get("preset"), d.get("bands")),
            "/api/system": lambda: p.system(d["action"]),
            "/api/fs/write": lambda: p.fs_write(d["path"], d["text"]),
            "/api/fs/delete": lambda: p.fs_delete(d["path"]),
            "/api/fs/rename": lambda: p.fs_rename(d["path"], d["name"]),
            "/api/fs/mkdir": lambda: p.fs_mkdir(d["path"]),
            "/api/fs/play": lambda: p.fs_play(d["path"]),
            "/api/alarms": lambda: p.alarms_save(d),
            "/api/alarms/test": lambda: p.alarm_test(d.get("id")),
            "/api/alarms/cmd": lambda: p.alarm_cmd(d.get("action")),
            "/api/alarms/install": p.alarm_install,
            "/api/footprint/reset": p.footprint_reset,
            "/api/privacy": lambda: p.privacy_set(bool(d.get("enabled"))),
            "/api/privacy/purge": p.privacy_purge,
            "/api/assistant": lambda: p.assistant_set(d),
            "/api/assistant/install": p.assistant_install,
            "/api/assistant/start": p.assistant_start,
            "/api/assistant/ask": lambda: p.assistant_ask(d.get("text")),
            "/api/assistant/stop": lambda: p.assistant_command({"stop": True}),
            "/api/assistant/mode": lambda: p.assistant_command({"mode": bool(d.get("on"))}),
            "/api/radio": lambda: p.radio_set(d),
            "/api/radio/play": lambda: p.radio_play(d),
            "/api/radio/stop": p.radio_stop,
            "/api/radio/miss-seen": lambda: p.assistant_command({"radio_miss_seen": True}),
            "/api/radio/check": lambda: p.radio_check(d.get("url")),
            "/api/radio/alice": lambda: p.radio_alice(d.get("name")),
            "/api/assistant/remove": p.assistant_remove,
            "/api/viz": lambda: p.viz_set(bool(d.get("on"))),
            "/api/assistant/models": lambda: p.assistant_models(d),
            "/api/panel/update": lambda: self.updater.update(),
            "/api/metrics/clear": lambda: p.metrics.clear(),
            "/api/alarms/uninstall": p.alarm_uninstall,
            "/api/scenes": lambda: p.scenes_save(d),
            "/api/scenes/run": lambda: p.scene_run(d.get("id")),
            "/api/say": lambda: p.say(d.get("text"), d.get("voice", "alice")),
            "/api/scenes/stop": lambda: p.scene_stop(d.get("id")),
        }
        if u.path in routes:
            return self._handle(routes[u.path])
        self._send(404, {"error": "not found"})


class PanelUpdater:
    """Проверка новых релизов панели на GitHub и обновление — в зависимости от того, как панель установлена:
    zip (Windows-сборка, Download ZIP) — скачать релиз и заменить файлы панели; git — git pull --ff-only;
    docker — только подсказать команду (образ изнутри не обновить)."""
    FILES = ("server.py", "access.py", "agent.py", "glagol.py", "profiles.py", "midi.py", "managed_yam.py", "multi.py", "pyadb.py", "speakers.example.json", "VERSION", "LICENSE", "README.md", "CONTRIBUTION.md", "start.bat", "start.sh", "start.command",
             "Dockerfile", "docker-compose.yml")
    DIRS = ("static", "device")
    OBSOLETE = ("3d", "tools", "docs")  # папки прежних версий: модель переехала в static/models, остальное не нужно

    def __init__(self, restart=None):
        self.restart = restart or restart_self
        self.cache = (0, None)
        self.lock = threading.Lock()

    @staticmethod
    def kind():
        if os.environ.get("PANEL_INSTALL") in ("docker", "android"):
            return os.environ["PANEL_INSTALL"]
        if Path("/.dockerenv").exists():
            return "docker"
        return "git" if (ROOT / ".git").exists() else "zip"

    @staticmethod
    def _newer(a, b):
        """a новее b? «0.1.10» > «0.1.9»; «1.0.0-rc1» < «1.0.0»."""
        def key(v):
            main, _, pre = v.lstrip("v").partition("-")
            return [int(x) if x.isdigit() else 0 for x in main.split(".")] + [0] * 3, (pre == "", pre)
        ka, kb = key(a), key(b)
        return (ka[0][:4], ka[1]) > (kb[0][:4], kb[1])

    def check(self, refresh=False):
        at, rel = self.cache
        err = None
        if refresh or rel is None or time.time() - at > 6 * 3600:
            try:
                req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                             headers={"User-Agent": f"yapanel-web/{VERSION}",
                                                      "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    rel = json.load(r)
                self.cache = (time.time(), rel)
            except urllib.error.HTTPError as e:
                err = "релизов пока нет или репозиторий закрыт" if e.code == 404 else f"GitHub ответил {e.code}"
            except (OSError, ValueError) as e:
                err = f"GitHub недоступен: {e}"
        res = {"version": VERSION, "install": self.kind(), "repo": f"https://github.com/{REPO}", "checked": self.cache[0] or None}
        if err and rel is None:
            return {**res, "error": err}
        latest = rel.get("tag_name", "").lstrip("v")
        return {**res, "latest": latest, "latest_date": (rel.get("published_at") or "")[:10],
                "url": rel.get("html_url"), "update": bool(latest) and self._newer(latest, VERSION),
                **({"error": err} if err else {})}

    def update(self):
        if not self.lock.acquire(blocking=False):
            raise AdbError("обновление уже идёт")
        try:
            info = self.check(refresh=True)
            if info.get("error") and not info.get("latest"):
                raise AdbError(info["error"])
            if not info.get("update"):
                raise AdbError("уже установлена последняя версия")
            kind = info["install"]
            if kind == "docker":
                raise AdbError("в Docker обновите образ: docker compose pull && docker compose up -d")
            if kind == "android":
                raise AdbError("в приложении для Android установите новый APK со страницы релиза")
            if kind == "git":
                r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"], capture_output=True, text=True, timeout=120)
                if r.returncode:
                    raise AdbError("git pull не удался: " + (r.stderr or r.stdout).strip()[-300:])
            else:
                self._from_zip(info["latest"])
            threading.Timer(1.0, self.restart).start()
            return {"ok": True, "version": info["latest"], "install": kind}
        finally:
            self.lock.release()

    def repair_missing(self):
        """Обновление из архива копирует только файлы из списка старой версии: новых модулей (managed_yam.py
        после 0.1.3) после него нет. Докачиваем исходники своей же версии, пока профили их не импортировали."""
        missing = [f for f in self.FILES if f.endswith(".py") and not (ROOT / f).exists()]
        if not missing or self.kind() != "zip":
            return
        print(f"Нет {', '.join(missing)} — докачиваю файлы версии {VERSION} с GitHub…")
        try:
            self._from_zip(VERSION)
        except (OSError, AdbError, zipfile.BadZipFile) as e:
            print(f"Не удалось: {e}. Часть функций будет недоступна до следующего обновления.")

    def _from_zip(self, version):
        """Исходники релиза с GitHub → поверх файлов панели. Данные (.yandex_auth.json и пр.), runtime и
        platform-tools не трогаем — их в архиве нет, а заменяем только файлы самой панели."""
        url = f"https://github.com/{REPO}/archive/refs/tags/v{version}.zip"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "webpanel"}), timeout=120) as r:
            blob = r.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            prefix = names[0].split("/", 1)[0] + "/" if names else ""
            if not any(n == prefix + "server.py" for n in names):
                raise AdbError("в архиве релиза нет server.py")
            for n in names:
                rel = n[len(prefix):]
                top = rel.split("/", 1)[0]
                if rel not in self.FILES and top not in self.DIRS:
                    continue
                if ".." in Path(rel).parts:
                    continue
                dst = ROOT / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".upd")
                tmp.write_bytes(z.read(n))
                os.replace(tmp, dst)
                if rel in ("start.sh", "start.command"):
                    os.chmod(dst, 0o755)
        for name in self.OBSOLETE:
            shutil.rmtree(ROOT / name, ignore_errors=True)


# Платформа в device-tree (compatible «yandex,plum», модель «Yandex Plum EVT2») → профиль колонки.
# Имя хоста тут не годится: владелец мог его переименовать.
# Станция 2 — station-midi/yandexmidi; Миди — cucumber/cucumber-dvt2. Макс пока не поддерживается.
PLATFORM_MODELS = {"cucumber": "midi", "plum": "lite2", "prune": "lite2nc", "pickle": "mini3pro", "bergamot": "mini3",
                   "yandexmidi": "station2", "station-midi": "station2", "station-mini2": "mini2",
                   "station-micro": "lite", "yandexmicro": "lite"}


def detect_model(adb):
    """(профиль, платформа) подключённой колонки или (None, None), если платформа незнакомая."""
    out, _, _ = adb.shell("cat /proc/device-tree/compatible /proc/device-tree/model 2>/dev/null | tr '\\000' ' '; true",
                          check=False, timeout=15)
    text = strip_ansi(out).lower()
    for platform, model in PLATFORM_MODELS.items():
        if re.search(rf"(^|[^a-z0-9]){platform}([^a-z0-9]|$)", text):
            if model == 'mini2':
                # Есть ли у этой Мини 2 сегментный дисплей: без него профиль mini2 зовёт несуществующие
                # светодиоды часов. Не смогли посмотреть (adb моргнул) — не угадываем и остаёмся на mini2.
                leds, _, rc = adb.shell('ls /sys/class/leds 2>/dev/null', check=False, timeout=15)
                listing = strip_ansi(leds)
                if rc == 0 and listing.strip() and 'white:clock' not in listing:
                    return 'mini2nc', platform
            return model, platform
    return None, None


def local_networks():
    """Подсети /24 этого компьютера, где может быть колонка: частные адреса, кроме VPN-туннелей (/32)."""
    nets = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=5).stdout
        addrs = [m.group(1, 2) for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out)]
    except (OSError, subprocess.SubprocessError):
        addrs = []
    if not addrs:   # Windows, macOS: адрес, через который идёт трафик наружу (пакет не отправляется)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.connect(("10.255.255.255", 1))
                addrs = [(u.getsockname()[0], "24")]
        except OSError:
            pass
    for ip, prefix in addrs:
        a = ipaddress.ip_address(ip)
        if a.is_private and not a.is_loopback and not a.is_link_local and int(prefix) < 31:
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
            if net not in nets:
                nets.append(net)
    return nets


def find_devices(adb_path, nets, progress=None):
    """Ищет колонки в подсетях: открытый порт adb 5555, затем проверка платы по device-tree.
    progress(этап, сделано, всего) — для экрана подключения. → [(«ip:5555», профиль, имя хоста)]."""
    from concurrent.futures import ThreadPoolExecutor
    progress = progress or (lambda *a: None)
    lock, done = threading.Lock(), [0]

    def tick(stage, total):
        with lock:
            done[0] += 1
            progress(stage, done[0], total)

    def port_open(ip):
        try:
            socket.create_connection((ip, 5555), timeout=0.6).close()
            return ip
        except OSError:
            return None
        finally:
            tick("ports", len(hosts))

    hosts = [str(h) for net in nets for h in net.hosts()]
    progress("ports", 0, len(hosts))
    with ThreadPoolExecutor(64) as pool:
        candidates = [ip for ip in pool.map(port_open, hosts) if ip]
    done[0] = 0
    progress("check", 0, len(candidates))

    def check(ip):
        try:
            return _check(ip)
        finally:
            tick("check", len(candidates))

    def _check(ip):
        serial = f"{ip}:5555"
        try:
            subprocess.run([adb_path, "connect", serial], capture_output=True, timeout=10)
            out = subprocess.run([adb_path, "-s", serial, "shell",
                                  "cat /proc/device-tree/compatible 2>/dev/null | tr '\\000' ' '; echo; hostname"],
                                 capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            out = ""
        text = strip_ansi(out).strip().lower()
        for platform, model in PLATFORM_MODELS.items():
            if "yandex" in text and re.search(rf"(^|[^a-z0-9]){platform}([^a-z0-9]|$)", text):
                return serial, model, strip_ansi(out).strip().splitlines()[-1].strip()
        # не колонка (телевизор, приставка) — не оставляем ей висящее подключение
        subprocess.run([adb_path, "disconnect", serial], capture_output=True, timeout=10)
        return None

    with ThreadPoolExecutor(8) as pool:
        return [r for r in pool.map(check, candidates) if r]


def pick_device(adb_path, where):
    """--find: найти колонку в сети и выбрать (если их несколько — спросить)."""
    import profiles
    try:
        nets = [ipaddress.ip_network(where, strict=False)] if where != "auto" else local_networks()
    except ValueError:
        raise SystemExit(f"--find: «{where}» — не подсеть. Пример: --find 192.168.1.0/24")
    if not nets:
        raise SystemExit("--find: не нашлось локальной сети. Укажите подсеть явно: --find 192.168.1.0/24")
    print(f"Ищем колонку в {', '.join(map(str, nets))}…")
    found = find_devices(adb_path, nets)
    if not found:
        print("Колонка не найдена: проверьте, что она включена и в той же сети, а adb по сети на ней работает.")
        return None
    for i, (serial, model, name) in enumerate(found, 1):
        print(f"  {i}. {serial.rpartition(':')[0]}  {profiles.PROFILES[model]['name']}  ({name})")
    if len(found) == 1 or not (sys.stdin and sys.stdin.isatty()):
        return found[0][0]
    while True:
        try:
            choice = input(f"Какую подключить (1–{len(found)}): ").strip()
        except EOFError:
            return found[0][0]
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            return found[int(choice) - 1][0]


def watch_model(adb, conn, active):
    """--model auto: как только колонка на связи, узнаём её модель и запоминаем. Если запущены не с тем
    профилем (первый запуск, другая колонка), перезапускаемся с правильным — один раз, дальше он запомнен."""
    import profiles
    while not conn.online:
        time.sleep(1)
    try:
        model, platform = detect_model(adb)
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось определить модель колонки: {e}")
        return
    if not model:
        print(f"Модель колонки не распознана — остаётся профиль {active}. Его можно задать явно: --model")
        return
    settings = load_settings()
    if settings.get("model") != model:
        save_settings({**settings, "model": model})
    if model != active:
        print(f"Подключена {profiles.PROFILES[model]['name']} (платформа {platform}) — перезапуск с её профилем")
        restart_self()


def restart_self(device=None):
    """Перезапуск сервера тем же интерпретатором и с теми же аргументами (без --open: браузер уже открыт).
    device — с другой колонкой: старые --device/--find заменяем (иначе аргумент перебил бы запомненный адрес)."""
    args = [a for a in sys.argv if a != "--open"]
    if device:
        out, i = [], 0
        while i < len(args):
            a = args[i]
            if a == "--device" or (a == "--find" and i + 1 < len(args) and not args[i + 1].startswith("-")):
                i += 2   # у --find подсеть необязательна
                continue
            if a != "--find" and not a.startswith(("--device=", "--find=")):
                out.append(a)
            i += 1
        args = out + ["--device", device]
    try:
        Handler.panel.metrics._save()
    except Exception:  # noqa: BLE001
        pass
    sys.stdout.flush()
    if RESTART_HOOK:     # Android: Python внутри приложения, exec некуда — перезапуск делает служба
        return RESTART_HOOK(args[1:])
    os.execv(sys.executable, [sys.executable] + args)


RESTART_HOOK = None   # (аргументы без имени скрипта) → перезапуск снаружи, см. android/


class TLSServer(ThreadingHTTPServer):
    """HTTPS-сервер. TLS-рукопожатие — в потоке запроса, чтобы медленный клиент не держал accept()."""

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def get_request(self):
        sock, addr = super().get_request()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def finish_request(self, request, client_address):
        request.settimeout(15)
        try:
            request.do_handshake()
        except (OSError, ssl.SSLError):
            return              # например, открыли https-порт по http
        request.settimeout(None)
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        pass


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    except OSError:
        pass


def main():
    env = os.environ.get
    import profiles
    ap = argparse.ArgumentParser(description="Веб-панель для Яндекс Станций")
    ap.add_argument("--device", default=env("ADB_SERIAL"),
                    help="IP колонки (порт 5555 по умолчанию), host:port или серийник USB; запоминается [ADB_SERIAL]")
    ap.add_argument("--find", nargs="?", const="auto", metavar="ПОДСЕТЬ",
                    help="найти колонку в локальной сети (или в заданной подсети, например 192.168.1.0/24) и запомнить")
    ap.add_argument("--host", default=env("PANEL_HOST", "127.0.0.1"),
                    help="адрес веб-сервера; 0.0.0.0 — доступ из локальной сети [PANEL_HOST]")
    ap.add_argument("--port", type=int, default=int(env("PANEL_PORT", "8765")), help="порт HTTP [PANEL_PORT]")
    ap.add_argument("--https-port", type=int, default=int(env("PANEL_HTTPS_PORT", "8766")),
                    help="порт HTTPS со своим сертификатом (для установки как приложение с телефона); 0 — выключить [PANEL_HTTPS_PORT]")
    ap.add_argument("--data-dir", default=env("PANEL_DATA_DIR"),
                    help="где хранить токен, пароль, сертификаты и историю (по умолчанию — рядом с server.py) [PANEL_DATA_DIR]")
    ap.add_argument("--open", action="store_true", help="открыть панель в браузере после запуска")
    ap.add_argument("--model", choices=[*profiles.PROFILES, "auto"], default=env("PANEL_MODEL", "auto"),
                    help="профиль колонки: lite2, lite, mini3pro, mini3, mini2, station2, midi или auto — определить по плате [PANEL_MODEL]")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):   # консоль Windows: русский текст не должен ронять сервер
        try:
            stream.reconfigure(errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass
    if args.data_dir:
        set_data_dir(args.data_dir)

    # Адрес колонки: из аргумента/окружения, иначе — запомненный, иначе спросим (если есть кому ответить).
    settings = load_settings()
    device = normalize_device(args.device) or settings.get("device")
    if args.find:
        device = pick_device(find_adb(), args.find) or device
    # С --open (start.bat/start.sh) не спрашиваем в консоли: страница сама найдёт колонку в сети
    if not device and not args.open and sys.stdin and sys.stdin.isatty():
        try:
            device = normalize_device(input("IP-адрес колонки (Enter — колонка по USB): "))
        except EOFError:
            device = None
    if device and device != settings.get("device"):
        save_settings({**settings, "device": device})
        settings = load_settings()

    # Профиль: явный --model или auto — запомненный (до первого подключения — Лайт 2), сверка после подключения.
    auto = args.model == "auto"
    model = (settings.get("model") if settings.get("model") in profiles.PROFILES else "lite2") if auto else args.model
    PanelUpdater().repair_missing()
    profiles.install(sys.modules[__name__], model, auto=auto)
    global PANEL_PROFILE
    PANEL_PROFILE = (model, auto)

    adb = Adb(find_adb(), device)
    panel = Panel(adb)
    panel.auth = glagol.YandexAuth(AUTH_FILE)
    host = device.rpartition(":")[0] if device and ":" in device else None
    panel.glagol = glagol.Glagol(panel.auth, host, None, GLAGOL_PLATFORM)
    panel.wakeword_router_start()
    panel.ymusic = glagol.YandexMusic(panel.auth)
    panel.agent_attach()
    panel.conn = Connection(adb)
    panel.leds = LedStream(adb)
    panel.metrics = Metrics(adb, panel.conn)
    threading.Thread(target=panel.metrics.run, daemon=True, name="metrics").start()
    threading.Thread(target=panel.conn.run, daemon=True, name="adb-connection").start()
    threading.Thread(target=panel.agent_keeper, args=(panel.conn,), daemon=True, name="agent").start()
    # в режиме нескольких колонок (multi.py) профиль задан явно: перезапуск сменил бы порт процесса
    if auto and not os.environ.get("PANEL_GATEWAY_SECRET"):
        threading.Thread(target=watch_model, args=(adb, panel.conn, model), daemon=True, name="model").start()
    Handler.panel = panel
    Handler.access = access.PanelAccess(PANEL_AUTH_FILE)
    Handler.updater = PanelUpdater()
    Handler.ports = {"http": args.port}
    # после перезапуска (обновление) старый процесс может ещё держать порт — подождём
    for attempt in range(20):
        try:
            srv = ThreadingHTTPServer((args.host, args.port), Handler)
            break
        except OSError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    args.port = srv.server_address[1]
    Handler.ports["http"] = args.port
    ready = os.environ.get("PANEL_READY_FILE")
    if ready:
        Path(ready).write_text(json.dumps({"port": args.port}), encoding="utf8")
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"Панель {VERSION} · профиль {model}{' (авто)' if auto else ''}")
    JOURNAL.add(f"запуск панели {VERSION}: профиль {model}{' (авто)' if auto else ''}, {platform.system()} {platform.release()}, "
                f"Python {platform.python_version()}, adb {'.'.join(map(str, adb_version(adb.adb) or ())) or '?'}")
    print(f"adb: {adb.adb}  колонка: {device or 'по USB / единственное устройство adb'}  данные: {DATA_DIR}")
    print(f"Панель: http://{shown}:{args.port}/")
    if args.https_port:
        try:
            crt, key = access.ensure_certs(TLS_DIR)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(crt, key)
            for attempt in range(20):        # после перезапуска порт может быть ещё занят
                try:
                    tls = TLSServer((args.host, args.https_port), Handler, ctx)
                    break
                except OSError:
                    if attempt == 19:
                        raise
                    time.sleep(0.5)
            threading.Thread(target=tls.serve_forever, daemon=True, name="https").start()
            Handler.ports["https"] = args.https_port
            print(f"        https://{shown}:{args.https_port}/  (сертификат CA: /ca.crt)")
        except (OSError, access.AccessError, ssl.SSLError) as e:
            print(f"HTTPS не запущен ({e}) — панель работает по HTTP")
    if not Handler.access.enabled:
        print("Пароль панели не задан — его можно задать во вкладке «Настройки» → «Доступ».")
    if args.open:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, (f"http://{shown}:{args.port}/",)).start()
    # kill (SIGTERM) — как Ctrl+C: выходим через finally и успеваем сохранить историю графиков
    if threading.current_thread() is threading.main_thread():   # в приложении для Android сервер — не в главном потоке
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        panel.metrics._save()


if __name__ == "__main__":
    main()
