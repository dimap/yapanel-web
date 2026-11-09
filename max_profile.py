import subprocess
import tempfile
"""Station Max (yandexstation_2) Hardware Profile Adapter.

Platform: Amlogic S905X2 (g12a), AArch64, Android TV 9.
Hardware:
  - Ring: 24 RGB LEDs on /dev/graphics/fb5 (24x1, 24bpp, 72 bytes)
  - Display: 25x16 white LED dot matrix on /dev/graphics/fb4 (25x16, 24bpp, 1200 bytes)
  - Audio: Android AudioFlinger (stream 3 = STREAM_MUSIC), ALSA Loopback on hw:1,1
  - Bluetooth: Android Fluoride stack (com.android.bluetooth, A2dpMediaBrowserService)
"""

import base64
import hashlib
import logging
import os
import posixpath
import re
import shlex
import shutil
import struct
import time
import threading
import max_privacy
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from config import get_config
    _cfg_remote = get_config().remote
    _cfg_dev = get_config().device
except ImportError:
    _cfg_remote = None
    _cfg_dev = None

MAX_RING_DEV = _cfg_remote.max_ring_dev if _cfg_remote else "/dev/graphics/fb5"
MAX_SCREEN_DEV = _cfg_remote.max_screen_dev if _cfg_remote else "/dev/graphics/fb4"
MAX_RING_LEDS = 24
MAX_SCREEN_COLS = 25
MAX_SCREEN_ROWS = 16
MAX_SCREEN_PIXELS = 400
MAX_SCREEN_BYTES = 1200

BT_CONF_PATH = "/data/misc/bluedroid/bt_config.conf"
BT_CONF_BACKUP = BT_CONF_PATH + ".bak-webpanel"

# /proc is replaced in tests to validate pid/address checks without touching the host.
_PROC_ROOT = "/proc"
_QUASAR_CMDLINE_MARK = b"com.yandex.io.sdk"
_MIN_USER_ADDR = 0x1000
_MAX_USER_ADDR = 0x0000FFFFFFFFFFFF  # 48-bit user-space address on AArch64

# preexec_fn is only safe when the process is single-threaded and on POSIX.
_POSIX = os.name == "posix"

_log = logging.getLogger(__name__)

# Встроенный DEX собирается из device/android_audio/StationAudio.java.
# Для локальной Android-установки он распаковывается в каталог данных панели.
_MAX_AUDIO_DEX_PATH = Path("/data/yapanel-state/bin/StationAudio.dex")
_MAX_AUDIO_TMP_DIR = Path("/data/yapanel-state/tmp")
_MAX_AUDIO_DEX_SHA256 = "9b8bd5f89cc99b0ad9f8cf4a83d325c0f5e4e4d529d0ba3ab10ac5ec321fc8ea"
_MAX_AUDIO_DEX_BASE64 = (
    "ZGV4CjAzOQCXuKuA2OU3syyIt2N4tdT7wun0D0e9hrBAEAAAcAAAAHhWNBIAAAAAAAAAAHAPAABT"
    "AAAAcAAAABwAAAC8AQAAFQAAACwCAAAIAAAAKAMAACoAAABoAwAABAAAALgEAAAICwAAOAUAABAJ"
    "AAAVCQAAHwkAACcJAABVCQAAbwkAAIcJAACKCQAAjgkAAJEJAACUCQAAmAkAAJwJAACuCQAAwAkA"
    "ANIJAADiCQAACwoAACwKAABeCgAAiwoAAKgKAAC+CgAA0woAAPgKAAAYCwAANwsAAEcLAABeCwAA"
    "dQsAAIkLAACfCwAAswsAAM4LAADiCwAADwwAACAMAAA6DAAATQwAAG8MAAByDAAAdgwAAHoMAAB/"
    "DAAAggwAAIgMAACNDAAAogwAAK8MAAC3DAAAvgwAAMcMAADMDAAA0gwAANcMAADoDAAA9AwAAPwM"
    "AAACDQAACA0AABINAAAYDQAAJg0AAC8NAAA1DQAAQg0AAEsNAABUDQAAWg0AAGMNAABoDQAAbQ0A"
    "AIENAACRDQAAoA0AALkNAADNDQAA1w0AAN4NAADoDQAA9g0AAAIOAAAJDgAABgAAAAgAAAAMAAAA"
    "DQAAAA4AAAAPAAAAEAAAABEAAAASAAAAEwAAABQAAAAVAAAAFgAAABcAAAAYAAAAGQAAABoAAAAb"
    "AAAAHAAAAB0AAAAeAAAAHwAAACAAAAAhAAAAIgAAACcAAAArAAAALgAAAAYAAAAAAAAAAAAAAAcA"
    "AAAAAAAAxAgAAAoAAAAGAAAAzAgAAAkAAAAHAAAAAAAAAAkAAAAMAAAAAAAAAAkAAAAVAAAAAAAA"
    "AAoAAAAWAAAAzAgAAAsAAAAWAAAA1AgAACcAAAAZAAAAAAAAACgAAAAZAAAAzAgAACkAAAAZAAAA"
    "3AgAACkAAAAZAAAArAgAACkAAAAZAAAAtAgAACkAAAAZAAAA5AgAACkAAAAZAAAA7AgAACkAAAAZ"
    "AAAA1AgAACoAAAAZAAAA9AgAACkAAAAZAAAAxAgAACsAAAAaAAAAAAAAACwAAAAaAAAA/AgAAC0A"
    "AAAaAAAACAkAAAIAGABPAAAAAgAMAFAAAAADABgATwAAAAMADABQAAAABAAYAE8AAAAEAAwAUAAA"
    "AAUAAAAjAAAAFwARADMAAAACABAAAgAAAAIADQA9AAAAAwAQAAIAAAADABMAPgAAAAQAEAACAAAA"
    "BAAIAEUAAAAFAAgAAgAAAAUAEQA6AAAABQABAD8AAAAGAAgAAgAAAAYAAwAxAAAABgACAEgAAAAG"
    "AAIATAAAAAoACAACAAAACgAIAEEAAAAKAAgARAAAAAoACgBHAAAACgAPAEkAAAAKAAsASgAAAAoA"
    "DABLAAAACgAIAE0AAAALAA4AAgAAAAsAFABAAAAADAAIADkAAAAMAAQAOwAAAAwACABBAAAADAAI"
    "AEMAAAAQAA8AAgAAABAAEgAyAAAAEAAFADYAAAAQABIAOAAAABEADwBCAAAAEgAFADcAAAATAAgA"
    "AgAAABYACAACAAAAFgAGADAAAAAWAAcAMAAAABYABQBOAAAAFwAJADQAAAAYAAkAAgAAABgAAAA1"
    "AAAAGAAJAEYAAAACAAAAAAAAABMAAACsCAAAJQAAACgPAAC+DgAAAAAAAAMAAAAAAAAAEwAAALQI"
    "AAAlAAAAQA8AANIOAAAAAAAABAAAAAAAAAATAAAAvAgAACUAAABYDwAA5g4AAAAAAAAFAAAAEQAA"
    "ABMAAAAAAAAAJQAAAAAAAAD6DgAADg8AAAMAAwABAAAARAgAAAgAAABbAQAAWwIBAHAQIQAAAA4A"
    "AwACAAIAAABKCAAADAAAAFQSAAASAG4gKQACAFQSAQBuEBoAAgAOAAYABAACAAAAUQgAAC4AAABi"
    "AwcAIgAWAHAQIgAAABoBJABuICQAEAAMAG4gIwBAAAwEGgABAG4gJAAEAAwEbiAjAFQADARuECUA"
    "BAAMBG4gHwBDAFQjAgASFG4gKQBDAFQjAwBuEBoAAwAPBAMAAwABAAAAXQgAAAgAAABbAQIAWwID"
    "AHAQIQAAAA4AAwADAAEAAABjCAAACAAAAFsBBABbAgUAcBAhAAAADgADAAEAAgAAAGkIAAAXAAAA"
    "VCAEAG4QKAAAAAoAEwF8ADMQDgBiAAcAGgEEAG4gHwAQAFQgBQBuEBoAAAAOAAAACAABAAQAAgBw"
    "CAAAuAAAACFwEiESEjIgCgBiBwcAGgAmAG4gHwAHAA8BIgAQABIDRgcHA3AgGwBwAG4QHgAAAAoH"
    "OAeXAG4QHAAAAAoHOQcEACkAjwBxABgAAAAMBzkHBQBxABkAAABxABgAAAAMByIBGAATA3wAcCAn"
    "ADEAIgMKAHAQDQADACIEBgBwEAkABABuIAwAJAAMBG4gCwAkAAwEbhAKAAQADARuIBAAQwAiBAIA"
    "cDAAABQHbiASAEMAIgQDAHAwAgAUB24gEwBDAG4QHQAAAAwAbiARAAMAbhAOAAMAbhAUAAMAIgAL"
    "AHAgFQBwACIEBABwMAQAFAcXBWDqAABuQBYAQGVxABcAAABuECgAAQAKB24QDwADAA8HDQcoIw0H"
    "YgAHACIBFgBwECIAAQAaBAUAbiAkAEEADAFuECAABwAMB24gJABxAAwHbhAlAAcADAduIB8AcAAA"
    "AG4QDwADAA8CbhAPAAMAJwdiBwcAGgADAG4gHwAHAA8BPAAAAEgAAQCLAAAAHAAHAAJ/EooBiAEA"
    "iAEAAAEAAQABAAAAnwgAAAQAAABwECEAAAAOAAEAAQABAAAApAgAAAoAAABxEAgAAAAKADgABQBx"
    "ECYAAAAOADYCAAAOADkBAA5pWgBBAwAAAA4BIg9pWgA+AgAADgBMAgAADgBPAA6leFsAHQEADlp4"
    "IIflaT5LeFxaS0tHQI6QeDw9ARAYPE8CezsiKB4BHA8fOR88AkcdeAASAA48ABYBAA5LLT0AAQAA"
    "AAgAAAABAAAACQAAAAEAAAAUAAAAAQAAABsAAAABAAAAAAAAAAEAAAAVAAAAAQAAAAcAAAABAAAA"
    "CgAAAAEAAAAMAAAAAgAAABgADAADAAAACgAAAAAAAAACAAAAFAABAAMoKVYACCwgZXh0cmE9AAY8"
    "aW5pdD4ALEF1ZGlvIGZpbGUgZG9lcyBub3QgZXhpc3Qgb3IgaXMgbm90IHJlYWRhYmxlABhBdWRp"
    "byBwbGF5YmFjayB0aW1lZCBvdXQAFkNvdWxkIG5vdCBwbGF5IGF1ZGlvOiAAAUkAAklMAAFKAAFM"
    "AAJMSQACTEwAEExTdGF0aW9uQXVkaW8kMTsAEExTdGF0aW9uQXVkaW8kMjsAEExTdGF0aW9uQXVk"
    "aW8kMzsADkxTdGF0aW9uQXVkaW87ACdMYW5kcm9pZC9tZWRpYS9BdWRpb0F0dHJpYnV0ZXMkQnVp"
    "bGRlcjsAH0xhbmRyb2lkL21lZGlhL0F1ZGlvQXR0cmlidXRlczsAMExhbmRyb2lkL21lZGlhL01l"
    "ZGlhUGxheWVyJE9uQ29tcGxldGlvbkxpc3RlbmVyOwArTGFuZHJvaWQvbWVkaWEvTWVkaWFQbGF5"
    "ZXIkT25FcnJvckxpc3RlbmVyOwAbTGFuZHJvaWQvbWVkaWEvTWVkaWFQbGF5ZXI7ABRMYW5kcm9p"
    "ZC9vcy9IYW5kbGVyOwATTGFuZHJvaWQvb3MvTG9vcGVyOwAjTGRhbHZpay9hbm5vdGF0aW9uL0Vu"
    "Y2xvc2luZ01ldGhvZDsAHkxkYWx2aWsvYW5ub3RhdGlvbi9Jbm5lckNsYXNzOwAdTGRhbHZpay9h"
    "bm5vdGF0aW9uL1NpZ25hdHVyZTsADkxqYXZhL2lvL0ZpbGU7ABVMamF2YS9pby9QcmludFN0cmVh"
    "bTsAFUxqYXZhL2xhbmcvRXhjZXB0aW9uOwASTGphdmEvbGFuZy9PYmplY3Q7ABRMamF2YS9sYW5n"
    "L1J1bm5hYmxlOwASTGphdmEvbGFuZy9TdHJpbmc7ABlMamF2YS9sYW5nL1N0cmluZ0J1aWxkZXI7"
    "ABJMamF2YS9sYW5nL1N5c3RlbTsAK0xqYXZhL3V0aWwvY29uY3VycmVudC9hdG9taWMvQXRvbWlj"
    "SW50ZWdlcjsAD01BWF9QTEFZQkFDS19NUwAYTWVkaWFQbGF5ZXIgZXJyb3I6IHdoYXQ9ABFTdGF0"
    "aW9uQXVkaW8uamF2YQAgVXNhZ2U6IFN0YXRpb25BdWRpbyA8YXVkaW8tZmlsZT4AAVYAAlZJAAJW"
    "TAADVkxMAAFaAARaTElJAANaTEoAE1tMamF2YS9sYW5nL1N0cmluZzsAC2FjY2Vzc0ZsYWdzAAZh"
    "cHBlbmQABWJ1aWxkAAdjYW5SZWFkAANlcnIABGV4aXQAA2dldAAPZ2V0QWJzb2x1dGVQYXRoAApn"
    "ZXRNZXNzYWdlAAZpc0ZpbGUABGxvb3AABG1haW4ACG15TG9vcGVyAARuYW1lAAxvbkNvbXBsZXRp"
    "b24AB29uRXJyb3IABHBsYXkAC3Bvc3REZWxheWVkAAdwcmVwYXJlAAdwcmludGxuAARxdWl0AAdy"
    "ZWxlYXNlAANydW4AA3NldAASc2V0QXVkaW9BdHRyaWJ1dGVzAA5zZXRDb250ZW50VHlwZQANc2V0"
    "RGF0YVNvdXJjZQAXc2V0T25Db21wbGV0aW9uTGlzdGVuZXIAEnNldE9uRXJyb3JMaXN0ZW5lcgAI"
    "c2V0VXNhZ2UABXN0YXJ0AAh0b1N0cmluZwAMdmFsJGV4aXRDb2RlAAp2YWwkbG9vcGVyAAV2YWx1"
    "ZQCcAX5+RDh7ImJhY2tlbmQiOiJkZXgiLCJjb21waWxhdGlvbi1tb2RlIjoiZGVidWciLCJoYXMt"
    "Y2hlY2tzdW1zIjpmYWxzZSwibWluLWFwaSI6MjgsInNoYS0xIjoiMDg0YTg5MTI2YWUwNTk2YzU5"
    "OTE0M2RmNTdhNjU0NjQ2YWYyODMxMyIsInZlcnNpb24iOiI5LjIuNC1kZXYifQACDwFRHAEXAAIN"
    "AVEaCAIOAi8EADweAAIBAQCQIAGQIACAgAS4CgEB2AoAAgEBApAgAZAgAoCABOwLAwGACwACAQEE"
    "kCABkCAEgIAEjAwFAawMAQADAAYaBoKABIgQAQmgEAEK7AwBRGDqAAABAAAAqA4AAAIAAACwDgAA"
    "tg4AABwPAAAAAAAAAQAAAAAAAAAAAAAAFA8AABwPAAAAAAAAAQAAAAAAAAACAAAAFA8AABwPAAAA"
    "AAAAAQAAAAAAAAAEAAAAFA8AABEAAAAAAAAAAQAAAAAAAAABAAAAUwAAAHAAAAACAAAAHAAAALwB"
    "AAADAAAAFQAAACwCAAAEAAAACAAAACgDAAAFAAAAKgAAAGgDAAAGAAAABAAAALgEAAABIAAACQAA"
    "ADgFAAADIAAACQAAAEQIAAABEAAADAAAAKwIAAACIAAAUwAAABAJAAAEIAAAAwAAAKgOAAAAIAAA"
    "BAAAAL4OAAAFIAAAAQAAAA4PAAADEAAAAgAAABQPAAAGIAAAAwAAACgPAAAAEAAAAQAAAHAPAAA="
)
_MAX_SAY_LOCK = threading.Lock()


def _max_audio_player():
    """Установить и проверить наш Android-проигрыватель без доступа к /odm."""
    target = _MAX_AUDIO_DEX_PATH
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == _MAX_AUDIO_DEX_SHA256:
        return target

    data = base64.b64decode(_MAX_AUDIO_DEX_BASE64)
    if hashlib.sha256(data).hexdigest() != _MAX_AUDIO_DEX_SHA256:
        raise RuntimeError("Контрольная сумма встроенного Android-проигрывателя не совпадает")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".new")
    try:
        temporary.write_bytes(data)
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _max_android_say(text):
    """Синтезировать речь и проиграть через стандартный Android MediaPlayer."""
    if not _MAX_SAY_LOCK.acquire(blocking=False):
        raise RuntimeError("Другое озвучивание уже выполняется")
    try:
        dex = _max_audio_player()
        url = (
            "https://translate.google.com/translate_tts"
            "?ie=UTF-8&tl=ru&client=tw-ob&q=" + urllib.parse.quote(text)
        )
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            content_type = response.headers.get("Content-Type", "")
            audio = response.read(1_000_001)

        if not content_type.lower().startswith("audio/") or not 256 < len(audio) <= 1_000_000:
            raise RuntimeError("Сервис синтеза речи вернул некорректные аудиоданные")

        temp_dir = _MAX_AUDIO_TMP_DIR
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="say-", suffix=".mp3", dir=str(temp_dir),
                                         delete=False) as output:
            output.write(audio)
            audio_path = output.name

        try:
            env = os.environ.copy()
            env["CLASSPATH"] = str(dex)
            result = subprocess.run(
                ["/system/bin/app_process", "/system/bin", "StationAudio", audio_path],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=65,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or "").strip()[-500:]
                raise RuntimeError(f"Android-проигрыватель завершился с кодом {result.returncode}: {detail}")
        finally:
            os.unlink(audio_path)
    finally:
        _MAX_SAY_LOCK.release()



def _shell_result(res):
    """Normalize adb.shell()/test doubles to (stdout, stderr, rc)."""
    if isinstance(res, (tuple, list)):
        out = res[0] if len(res) > 0 and res[0] is not None else ""
        err = res[1] if len(res) > 1 and res[1] is not None else ""
        try:
            rc = int(res[2]) if len(res) > 2 else 0
        except (TypeError, ValueError):
            rc = 0
        return str(out), str(err), rc
    return ("" if res is None else str(res)), "", 0


def _preexec_allowed():
    if not _POSIX:
        return False
    try:
        return threading.active_count() <= 1
    except Exception:
        return False


def _preexec_unsupported(exc):
    msg = str(exc).lower()
    return "preexec" in msg and ("not supported" in msg or "support" in msg or "implemented" in msg)

# 4x7 font for digits, symbols, Latin, and Cyrillic
FONT_4X7 = {
    # Digits
    '0': ["0110", "1001", "1001", "1001", "1001", "1001", "0110"],
    '1': ["0010", "0110", "0010", "0010", "0010", "0010", "0111"],
    '2': ["0110", "1001", "0001", "0010", "0100", "1000", "1111"],
    '3': ["1110", "0001", "0001", "0110", "0001", "0001", "1110"],
    '4': ["1001", "1001", "1001", "1111", "0001", "0001", "0001"],
    '5': ["1111", "1000", "1000", "1110", "0001", "0001", "1110"],
    '6': ["0110", "1000", "1000", "1110", "1001", "1001", "0110"],
    '7': ["1111", "0001", "0010", "0010", "0100", "0100", "0100"],
    '8': ["0110", "1001", "1001", "0110", "1001", "1001", "0110"],
    '9': ["0110", "1001", "1001", "0111", "0001", "0001", "0110"],

    # Symbols & Punctuation
    ' ': ["00", "00", "00", "00", "00", "00", "00"],
    ':': ["0", "0", "1", "0", "1", "0", "0"],
    '.': ["0", "0", "0", "0", "0", "0", "1"],
    ',': ["0", "0", "0", "0", "0", "1", "1"],
    '-': ["000", "000", "000", "111", "000", "000", "000"],
    '+': ["000", "010", "010", "111", "010", "010", "000"],
    '*': ["0000", "1001", "0110", "1111", "0110", "1001", "0000"],
    '°': ["110", "110", "000", "000", "000", "000", "000"],
    '/': ["0001", "0001", "0010", "0100", "0100", "1000", "1000"],
    '\\': ["1000", "1000", "0100", "0010", "0010", "0001", "0001"],
    '!': ["1", "1", "1", "1", "0", "0", "1"],
    '?': ["0110", "1001", "0001", "0010", "0010", "0000", "0010"],
    '=': ["000", "000", "111", "000", "111", "000", "000"],
    '_': ["0000", "0000", "0000", "0000", "0000", "0000", "1111"],
    '%': ["1001", "1001", "0010", "0100", "1001", "1001", "0000"],
    '(': ["010", "100", "100", "100", "100", "100", "010"],
    ')': ["010", "001", "001", "001", "001", "001", "010"],

    # Latin A-Z
    'A': ["0110", "1001", "1001", "1111", "1001", "1001", "1001"],
    'B': ["1110", "1001", "1001", "1110", "1001", "1001", "1110"],
    'C': ["0111", "1000", "1000", "1000", "1000", "1000", "0111"],
    'D': ["1110", "1001", "1001", "1001", "1001", "1001", "1110"],
    'E': ["1111", "1000", "1000", "1110", "1000", "1000", "1111"],
    'F': ["1111", "1000", "1000", "1110", "1000", "1000", "1000"],
    'G': ["0111", "1000", "1000", "1011", "1001", "1001", "0111"],
    'H': ["1001", "1001", "1001", "1111", "1001", "1001", "1001"],
    'I': ["111", "010", "010", "010", "010", "010", "111"],
    'J': ["0001", "0001", "0001", "0001", "1001", "1001", "0110"],
    'K': ["1001", "1010", "1100", "1100", "1010", "1001", "1001"],
    'L': ["1000", "1000", "1000", "1000", "1000", "1000", "1111"],
    'M': ["10001", "11011", "10101", "10001", "10001", "10001", "10001"],
    'N': ["1001", "1101", "1101", "1011", "1011", "1001", "1001"],
    'O': ["0110", "1001", "1001", "1001", "1001", "1001", "0110"],
    'P': ["1110", "1001", "1001", "1110", "1000", "1000", "1000"],
    'Q': ["0110", "1001", "1001", "1001", "1011", "1001", "0111"],
    'R': ["1110", "1001", "1001", "1110", "1010", "1001", "1001"],
    'S': ["0111", "1000", "1000", "0110", "0001", "0001", "1110"],
    'T': ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    'U': ["1001", "1001", "1001", "1001", "1001", "1001", "0110"],
    'V': ["1001", "1001", "1001", "1001", "1001", "0110", "0100"],
    'W': ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    'X': ["1001", "1001", "0110", "0110", "0110", "1001", "1001"],
    'Y': ["1001", "1001", "1001", "0110", "0010", "0010", "0010"],
    'Z': ["1111", "0001", "0010", "0100", "0100", "1000", "1111"],

    # Cyrillic А-Я, Ё
    'А': ["0110", "1001", "1001", "1111", "1001", "1001", "1001"],
    'Б': ["1111", "1000", "1000", "1110", "1001", "1001", "1110"],
    'В': ["1110", "1001", "1001", "1110", "1001", "1001", "1110"],
    'Г': ["1111", "1000", "1000", "1000", "1000", "1000", "1000"],
    'Д': ["01100", "01100", "01100", "01100", "01100", "11111", "10001"],
    'Е': ["1111", "1000", "1000", "1110", "1000", "1000", "1111"],
    'Ё': ["0101", "0000", "1111", "1000", "1110", "1000", "1111"],
    'Ж': ["10101", "10101", "10101", "01110", "10101", "10101", "10101"],
    'З': ["1110", "0001", "0001", "0110", "0001", "0001", "1110"],
    'И': ["1001", "1001", "1011", "1101", "1101", "1001", "1001"],
    'Й': ["0110", "0000", "1001", "1011", "1101", "1001", "1001"],
    'К': ["1001", "1010", "1100", "1100", "1010", "1001", "1001"],
    'Л': ["0111", "1001", "1001", "1001", "1001", "1001", "1001"],
    'М': ["10001", "11011", "10101", "10001", "10001", "10001", "10001"],
    'Н': ["1001", "1001", "1001", "1111", "1001", "1001", "1001"],
    'О': ["0110", "1001", "1001", "1001", "1001", "1001", "0110"],
    'П': ["1111", "1001", "1001", "1001", "1001", "1001", "1001"],
    'Р': ["1110", "1001", "1001", "1110", "1000", "1000", "1000"],
    'С': ["0111", "1000", "1000", "1000", "1000", "1000", "0111"],
    'Т': ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    'У': ["1001", "1001", "1001", "0111", "0001", "0001", "1110"],
    'Ф': ["0110", "1001", "1111", "1001", "0110", "0010", "0010"],
    'Х': ["1001", "1001", "0110", "0110", "0110", "1001", "1001"],
    'Ц': ["10010", "10010", "10010", "10010", "10010", "11111", "00001"],
    'Ч': ["1001", "1001", "1001", "0111", "0001", "0001", "0001"],
    'Ш': ["10101", "10101", "10101", "10101", "10101", "10101", "11111"],
    'Щ': ["101010", "101010", "101010", "101010", "101010", "111111", "000001"],
    'Ъ': ["1100", "0100", "0100", "0110", "0101", "0101", "0110"],
    'Ы': ["10001", "10001", "10001", "11101", "10011", "10011", "11101"],
    'Ь': ["1000", "1000", "1000", "1110", "1001", "1001", "1110"],
    'Э': ["1110", "0001", "0001", "0111", "0001", "0001", "1110"],
    'Ю': ["10110", "10101", "10101", "11101", "10101", "10101", "10110"],
    'Я': ["0111", "1001", "1001", "0111", "0101", "1001", "1001"],
}


def render_matrix_text(text="12:34", brightness=180):
    """Render string into 1200-byte RGB888 buffer for 25x16 screen."""
    grid = [[0] * MAX_SCREEN_COLS for _ in range(MAX_SCREEN_ROWS)]
    total_w = 0
    glyphs = []
    for ch in str(text):
        upper = ch.upper()
        g = FONT_4X7.get(upper, FONT_4X7[' '])
        gw = len(g[0])
        glyphs.append((g, gw))
        total_w += gw + 1
    if total_w > 0:
        total_w -= 1

    start_x = max(0, (MAX_SCREEN_COLS - total_w) // 2)
    start_y = max(0, (MAX_SCREEN_ROWS - 7) // 2)

    cur_x = start_x
    for g, gw in glyphs:
        for r in range(7):
            for c in range(gw):
                if g[r][c] == '1':
                    y = start_y + r
                    x = cur_x + c
                    if 0 <= y < MAX_SCREEN_ROWS and 0 <= x < MAX_SCREEN_COLS:
                        grid[y][x] = max(0, min(255, int(brightness)))
        cur_x += gw + 1

    return matrix_grid_to_bytes(grid)


def v36_to_matrix(v):
    """Render 36 7-segment values directly onto 25x16 dot matrix grid."""
    grid = [[0] * MAX_SCREEN_COLS for _ in range(MAX_SCREEN_ROWS)]
    digit_x = [1, 7, 14, 20]
    for d, x0 in enumerate(digit_x):
        b = max(0, min(255, int(v[d * 8 + 0])))
        if b > 0:
            for x in range(x0 + 1, x0 + 4):
                grid[3][x] = b
        b = max(0, min(255, int(v[d * 8 + 1])))
        if b > 0:
            for y in range(4, 8):
                grid[y][x0 + 4] = b
        b = max(0, min(255, int(v[d * 8 + 2])))
        if b > 0:
            for y in range(9, 13):
                grid[y][x0 + 4] = b
        b = max(0, min(255, int(v[d * 8 + 3])))
        if b > 0:
            for x in range(x0 + 1, x0 + 4):
                grid[13][x] = b
        b = max(0, min(255, int(v[d * 8 + 4])))
        if b > 0:
            for y in range(9, 13):
                grid[y][x0] = b
        b = max(0, min(255, int(v[d * 8 + 5])))
        if b > 0:
            for y in range(4, 8):
                grid[y][x0] = b
        b = max(0, min(255, int(v[d * 8 + 6])))
        if b > 0:
            for x in range(x0 + 1, x0 + 4):
                grid[8][x] = b
        b = max(0, min(255, int(v[d * 8 + 7])))
        if b > 0 and x0 + 5 < MAX_SCREEN_COLS:
            grid[13][x0 + 5] = b

    b_deg = max(0, min(255, int(v[32])))
    b_dh = max(0, min(255, int(v[33])))
    b_dl = max(0, min(255, int(v[34])))
    b_dm = max(0, min(255, int(v[35])))
    if b_dh > 0:
        grid[5][12] = grid[6][12] = b_dh
    if b_dl > 0:
        grid[10][12] = grid[11][12] = b_dl
    if b_dm > 0:
        for x in range(11, 14):
            grid[8][x] = b_dm
    if b_deg > 0:
        grid[3][22] = grid[3][23] = b_deg
        grid[4][21] = grid[4][24] = b_deg
        grid[5][22] = grid[5][23] = b_deg

    return matrix_grid_to_bytes(grid)


def matrix_grid_to_bytes(grid):
    """Convert 25x16 2D grid of 0..255 brightness to 1200-byte RGB888 buffer."""
    buf = bytearray(MAX_SCREEN_BYTES)
    for r in range(MAX_SCREEN_ROWS):
        for c in range(MAX_SCREEN_COLS):
            val = max(0, min(255, int(grid[r][c])))
            idx = (r * MAX_SCREEN_COLS + c) * 3
            buf[idx] = val
            buf[idx + 1] = val
            buf[idx + 2] = val
    return bytes(buf)


_cached_quasar_ctrl = None  # (pid, is_init_addr)
_quasar_ctrl_lock = threading.Lock()


def find_quasar_controller_addr(target_pid=None):
    """Find Quasar's YandexStationFrontalController::isInitialized_ memory address.
    Matches the native max-platform implementation in quasar_pause.rs.
    """
    global _cached_quasar_ctrl

    pid = target_pid
    if not pid:
        try:
            for p in os.listdir("/proc"):
                if p.isdigit():
                    try:
                        with open(f"/proc/{p}/cmdline", "rb") as f:
                            cmd = f.read()
                        if cmd.startswith(b"com.yandex.io.sdk"):
                            pid = int(p)
                            break
                    except Exception:
                        pass
        except Exception:
            pass
    if not pid:
        return None

    if _cached_quasar_ctrl and _cached_quasar_ctrl[0] == pid:
        return _cached_quasar_ctrl

    maps_path = f"/proc/{pid}/maps"
    if not os.path.exists(maps_path):
        return None

    fb4_mmap_addr = None
    rw_ranges = []
    try:
        with open(maps_path, "r") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                r = parts[0]
                perms = parts[1]
                path = parts[-1] if len(parts) >= 6 else ""
                if "fb4" in path:
                    fb4_mmap_addr = int(r.split("-")[0], 16)
                if perms.startswith("rw") and perms.endswith("p") and "/dev/ashmem/" not in path:
                    b_start, b_end = [int(x, 16) for x in r.split("-")]
                    if b_start >= 0x5000000000 and (4096 <= (b_end - b_start) <= 64 * 1024 * 1024):
                        rw_ranges.append((b_start, b_end))
    except Exception:
        return None

    if not fb4_mmap_addr:
        return None

    chunk_size = 256 * 1024
    target_pattern = struct.pack("<Q", fb4_mmap_addr)

    try:
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as mem:
            for start, end in rw_ranges:
                cur = start
                while cur < end:
                    to_read = min(chunk_size, end - cur)
                    try:
                        mem.seek(cur)
                        chunk = mem.read(to_read)
                    except Exception:
                        break
                    if len(chunk) < 24:
                        break

                    idx = 0
                    while True:
                        idx = chunk.find(target_pattern, idx)
                        if idx == -1 or idx + 24 > len(chunk):
                            break
                        h, w = struct.unpack_from("<ii", chunk, idx + 16)
                        if h == 16 and w == 25:
                            fb_ptr_va = cur + idx
                            try:
                                mem.seek(fb_ptr_va - 256)
                                hdr = mem.read(288)
                                for rel in range(-232, -184, 8):
                                    i1 = 256 + rel - 24
                                    i2 = 256 + rel - 8
                                    if i1 >= 0 and i2 + 8 <= len(hdr):
                                        vp1, = struct.unpack_from("<Q", hdr, i1)
                                        vp2, = struct.unpack_from("<Q", hdr, i2)
                                        if vp2 > vp1 and 0x30 <= (vp2 - vp1) <= 0x100:
                                            is_init_addr = fb_ptr_va + rel
                                            _cached_quasar_ctrl = (pid, is_init_addr)
                                            return _cached_quasar_ctrl
                            except Exception:
                                pass
                        idx += 8

                    if to_read < chunk_size:
                        break
                    cur += chunk_size - 32
    except Exception:
        pass
    return None


def _quasar_pid_valid(pid):
    """True only when pid exists and its cmdline belongs to Quasar (com.yandex.io.sdk)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not os.path.isdir(f"{_PROC_ROOT}/{pid}"):
        return False
    try:
        with open(f"{_PROC_ROOT}/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except Exception:
        return False
    return _QUASAR_CMDLINE_MARK in cmdline


def _quasar_addr_valid(pid, addr):
    """True when addr is non-zero, in user space, and mapped in the pid's address space."""
    try:
        addr = int(addr)
    except (TypeError, ValueError):
        return False
    if not (_MIN_USER_ADDR <= addr < _MAX_USER_ADDR):
        return False
    try:
        with open(f"{_PROC_ROOT}/{pid}/maps", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                rng = line.split(None, 1)[0] if line.split(None, 1) else ""
                start_s, sep, end_s = rng.partition("-")
                if not sep:
                    continue
                try:
                    start, end = int(start_s, 16), int(end_s, 16)
                except ValueError:
                    continue
                if start <= addr < end:
                    return True
    except Exception:
        return False
    return False


def max_set_quasar_clock_paused(adb, pause=True):
    """Clean cooperative memory pause/resume matching max-platform / hal_bridge.
    Sets YandexStationFrontalController::isInitialized_ to 0 or 1 in Quasar memory.
    This lets Quasar continue running audio, Alice, and services normally,
    while cleanly stopping it from writing to /dev/graphics/fb4 without any process freezes.
    """
    global _cached_quasar_ctrl
    is_local = getattr(adb, "is_local", False) or os.path.exists(MAX_SCREEN_DEV)
    val = 0 if pause else 1

    if is_local:
        with _quasar_ctrl_lock:
            info = find_quasar_controller_addr()
            if not info:
                _cached_quasar_ctrl = None
                return False
            pid, addr = info
            # Never write into an unknown process or an implausible address.
            if not _quasar_pid_valid(pid) or not _quasar_addr_valid(pid, addr):
                _cached_quasar_ctrl = None
                return False
            try:
                with open(f"{_PROC_ROOT}/{pid}/mem", "r+b", buffering=0) as f:
                    f.seek(addr)
                    written = f.write(bytes([val]))
                if written == 0:
                    raise OSError("short write to /proc/%s/mem" % pid)
                return True
            except Exception:
                _cached_quasar_ctrl = None
    else:
        cmd = (
            f"/data/python/bin/python3.11 -c \"import sys; sys.path.insert(0, '/data/yapanel'); "
            f"from max_profile import max_set_quasar_clock_paused; "
            f"class FakeAdb: is_local = True\nmax_set_quasar_clock_paused(FakeAdb(), {pause})\" 2>/dev/null; true"
        )
        adb.shell(cmd, check=False)
    return False


def max_pause_alice_leds(adb):
    max_set_quasar_clock_paused(adb, True)


def max_resume_alice_leds(adb):
    max_set_quasar_clock_paused(adb, False)


def max_android_cmd(adb, args, timeout=None):
    """Run Android command with proper SELinux context for system services (dumpsys, svc)."""
    effective_timeout = timeout if timeout is not None else (_cfg_dev.shell_timeout if _cfg_dev else 10)
    if getattr(adb, "is_local", False):
        def _preexec():
            try:
                with open("/proc/self/attr/current", "w") as f:
                    f.write("u:r:system_server:s0")
            except Exception:
                pass

        def _run(use_preexec):
            kwargs = dict(capture_output=True, text=True, errors="replace", timeout=effective_timeout)
            if use_preexec:
                kwargs["preexec_fn"] = _preexec
            return subprocess.run(args, **kwargs)

        # preexec_fn is unsafe with threads and unsupported on non-POSIX platforms.
        if _preexec_allowed():
            try:
                return _run(True).stdout
            except Exception as exc:
                if not _preexec_unsupported(exc):
                    return ""
        try:
            return _run(False).stdout
        except Exception:
            return ""
    else:
        cmd = " ".join(shlex.quote(a) for a in args)
        out, _, _ = adb.shell(cmd, check=False, timeout=effective_timeout)
        return out


def max_remove_device_from_conf(conf_text, target_mac):
    """Remove target MAC section from bt_config.conf text."""
    target_mac = target_mac.lower()
    lines = conf_text.splitlines()
    out = []
    skip = False
    for line in lines:
        m = re.match(r"^\[([0-9a-f:]{17})\]", line.strip(), re.I)
        if m:
            if m.group(1).lower() == target_mac:
                skip = True
                continue
            else:
                skip = False
        elif line.startswith("["):
            skip = False

        if not skip:
            out.append(line)
    return "\n".join(out) + "\n"


def max_read_bt_conf(adb):
    """Read /data/misc/bluedroid/bt_config.conf locally or via ADB."""
    if getattr(adb, "is_local", False):
        try:
            with open(BT_CONF_PATH, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""
    else:
        out, _, _ = adb.shell(f"su -c 'cat {BT_CONF_PATH}' 2>/dev/null; true", check=False)
        return out


def max_write_bt_conf(adb, text):
    """Atomically write bt_config.conf, keeping a webpanel backup for rollback."""
    if text is None or not str(text).strip():
        raise ValueError("bt_config.conf: пустое содержимое — запись отменена")
    text = str(text)

    if getattr(adb, "is_local", False):
        target = Path(BT_CONF_PATH)
        backup = Path(BT_CONF_BACKUP)
        existed = target.exists()
        if existed:
            # No backup means no safe write: abort before touching the target.
            shutil.copy2(target, backup)
        tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            if existed:
                # os.replace() swaps the inode: carry over the original permissions/owner.
                try:
                    st = target.stat()
                except OSError:
                    st = None
                if st is not None:
                    try:
                        os.chmod(tmp, st.st_mode & 0o7777)
                    except OSError:
                        pass
                    if hasattr(os, "chown"):
                        try:
                            os.chown(tmp, st.st_uid, st.st_gid)
                        except OSError:
                            pass
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if existed and backup.exists():
                try:
                    shutil.copy2(backup, target)
                except Exception:
                    try:
                        os.replace(backup, target)
                    except Exception:
                        pass
            raise
        return

    suffix = f"{os.getpid()}-{os.urandom(4).hex()}"
    stage = f"/data/local/tmp/bt_config-{suffix}.tmp"
    target_tmp = f"{BT_CONF_PATH}.webpanel.tmp-{suffix}"
    q_stage = shlex.quote(stage)
    q_tmp = shlex.quote(target_tmp)
    q_target = shlex.quote(BT_CONF_PATH)
    q_backup = shlex.quote(BT_CONF_BACKUP)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, newline="") as tf:
        tf.write(text)
        tf.flush()
        try:
            os.fsync(tf.fileno())
        except OSError:
            pass
        tmp_path = tf.name
    write_attempted = False
    try:
        out, err, rc = _shell_result(adb.shell(
            f"su -c 'if [ -f {q_target} ]; then cp -f {q_target} {q_backup} || exit 3; "
            f"[ -f {q_backup} ] || exit 4; fi'",
            check=False,
        ))
        if rc != 0:
            # The target is still intact; do not restore a possibly partial backup.
            raise OSError(f"не удалось сохранить копию bt_config.conf: {(err or out).strip()}")
        write_attempted = True

        adb.push(tmp_path, stage)
        out, err, rc = _shell_result(adb.shell(
            f"su -c 'u=$(stat -c %u {q_target} 2>/dev/null); g=$(stat -c %g {q_target} 2>/dev/null); "
            f"m=$(stat -c %a {q_target} 2>/dev/null); "
            f"cp -f {q_stage} {q_tmp} && mv -f {q_tmp} {q_target} && [ -s {q_target} ] && "
            f"{{ if [ -n \"$u\" ] && [ -n \"$g\" ]; then chown \"$u:$g\" {q_target} 2>/dev/null; "
            f"else chown bluetooth:bluetooth {q_target} 2>/dev/null; fi; "
            f"[ -n \"$m\" ] && chmod \"$m\" {q_target} 2>/dev/null; true; }}'",
            check=False,
        ))
        if rc != 0:
            raise OSError(f"не удалось записать bt_config.conf: {(err or out).strip()}")
    except Exception:
        # The target is never truncated in place; restore the pre-write copy if writing began.
        if write_attempted:
            try:
                adb.shell(
                    f"su -c 'if [ -f {q_backup} ]; then cp -f {q_backup} {q_target}; fi; "
                    f"chown bluetooth:bluetooth {q_target} 2>/dev/null; "
                    f"chmod 660 {q_target} 2>/dev/null; true'",
                    check=False,
                )
            except Exception:
                pass
        raise
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        try:
            adb.shell(f"su -c 'rm -f {q_stage} {q_tmp}' 2>/dev/null; true", check=False)
        except Exception:
            pass


def max_is_bt_off(adb, bt_off_path):
    """Check if Bluetooth off flag exists."""
    if getattr(adb, "is_local", False):
        return os.path.exists(bt_off_path)
    else:
        out, _, _ = adb.shell(f"[ -f {shlex.quote(bt_off_path)} ] && echo 1 || echo 0", check=False)
        return out.strip() == "1"


def max_set_bt_off(adb, bt_off_path, off):
    """Set or remove Bluetooth off flag."""
    if getattr(adb, "is_local", False):
        try:
            if off:
                Path(bt_off_path).parent.mkdir(parents=True, exist_ok=True)
                Path(bt_off_path).touch()
            else:
                if os.path.exists(bt_off_path):
                    os.unlink(bt_off_path)
        except Exception:
            pass
    else:
        if off:
            adb.shell(f"mkdir -p $(dirname {shlex.quote(bt_off_path)}) && touch {shlex.quote(bt_off_path)}", check=False)
        else:
            adb.shell(f"rm -f {shlex.quote(bt_off_path)}", check=False)


def max_bt_info(adb):
    """Parse Android media_session dumpsys for active Bluetooth AVRCP playback."""
    try:
        if getattr(adb, "is_local", False):
            out = max_android_cmd(adb, ["dumpsys", "media_session"], timeout=5)
        else:
            out, _, _ = adb.shell("dumpsys media_session 2>/dev/null", check=False, timeout=5)
    except Exception:
        return None

    idx = out.find("A2dpMediaBrowserService")
    if idx == -1:
        return None
    block = out[idx:idx + 2000]
    end_idx = block.find("Session", 30)
    if end_idx != -1:
        block = block[:end_idx]

    m_state = re.search(r"state=PlaybackState\s*\{\s*state=(\d+),\s*position=(\d+)", block)
    if not m_state or int(m_state.group(1)) not in (2, 3):
        return None

    m_desc = re.search(r"description=([^\r\n]+)", block)
    if not m_desc:
        return None

    desc = m_desc.group(1).strip()
    if not desc or desc.lower() in ("null", "none", "unknown"):
        return None

    parts = [p.strip() for p in desc.split(",")]
    title = parts[0] if parts and parts[0] and parts[0].lower() not in ("null", "none") else None
    if not title:
        return None

    artist = parts[1] if len(parts) >= 2 and parts[1].lower() not in ("null", "none") else None
    album = parts[2] if len(parts) >= 3 and parts[2].lower() not in ("null", "none") else None

    return {
        "connected": True,
        "title": title,
        "artist": artist,
        "album": album,
        "position": int(m_state.group(2)) / 1000.0,
        "duration": 0.0,
        "playing": (int(m_state.group(1)) == 3),
    }


def install(s, profile):
    """Install Station Max adapters onto server module s."""
    P = s.Panel
    s.GLAGOL_PLATFORM = "yandexstation_2"
    s.LED_PREFIX = "max"

    # Alarms and scenes are Buildroot/Linux features; the voice assistant has a
    # native Max implementation (assistant_max.py) and works on Android TV 9.
    profile["alarms"] = False
    profile["scenes"] = False
    profile["assistant"] = True
    profile["apps"] = True
    if "tested" in profile and isinstance(profile["tested"], (list, tuple)):
        profile["tested"] = [x for x in profile["tested"] if x not in ("scenes", "alarm", "btout", "btpower")]
        if "assistant" not in profile["tested"]:
            profile["tested"].append("assistant")
        if "dsp" not in profile["tested"]:
            profile["tested"].append("dsp")

    P.alarms = lambda self: {"alarms": [], "supported": False, "installed": False, "running": False, "reason": "Будильники не поддерживаются на Станции Макс"}
    P.scenes = lambda self: {"scenes": [], "supported": False, "running": [], "log": [], "daemon": False, "reason": "Сценарии не поддерживаются на Станции Макс"}

    # Set root directory for Quasar system assets on Station Max Android
    s.QUASAR = "/data/data/com.yandex.io.sdk/files/system"
    # Wakeword daemon lives in /data/wakeword and is managed by the panel directly
    # (no YAM package, no maxpkg wrapper, no LAN UI).
    s.WAKE_BIN = s.WAKE_BIN_LOCAL
    s.WAKE_NEGATIVES = s.WAKE_NEGATIVES_LOCAL
    s.WAKE_BIN_SHA256 = s.WAKE_BIN_SHA256 or "643c279b3327f1a110a8c65bc9e48bc70d305b4bee1efba1e491d9f94f61849f"
    s.SOUNDS_DIR = f"{s.QUASAR}/sounds"
    s.SOUND_TARGET = f"{s.SOUNDS_DIR}/common/boot.wav"
    if not isinstance(getattr(s, "PATTERNS", None), dict):
        s.PATTERNS = {}
    s.PATTERNS["ring"] = f"{s.QUASAR}/ledpatterns"
    s.PATTERNS["clock"] = f"{s.WEB_DIR}/patterns/clock"
    s.BOOT_TARGET = f"{s.PATTERNS['clock']}/boot.binscled.zst"
    s.BOOT_CUSTOM = f"{s.WEB_DIR}/boot.binscled.zst"
    s.BOOT_ORIG = f"{s.WEB_DIR}/boot.orig.binscled.zst"
    s.ROOTKEEPER = s.BOOT_HOOK
    s.ROOTKEEPER_CALLER = "/odm/bin/adbd_watchdog.sh"
    s.ROOTKEEPER_CREATED = f"{s.BOOT_HOOK}.webpanel.created"
    # Station Max Android TV specific paths & privacy purge (no Buildroot /system/vendor/quasar or maind)
    s.HOSTS_TARGET = "/system/etc/hosts"
    # AppMetrica на этой прошивке живёт в com.yandex.tv.services, а не в com.yandex.io.sdk.
    # Старый путь оставляем как fallback для других сборок Макса.
    s.METRICA_BATCH = ("/data/data/com.yandex.tv.services/no_backup/appmetrica/analytics/appmetrica_vital*.dat "
                       "/data/data/com.yandex.tv.services/no_backup/appmetrica/analytics/appmetrica*.db "
                       "/data/data/com.yandex.io.sdk/databases/appmetrica*")
    s.ANALYTICS_PURGE = ("rm -f /data/data/com.yandex.tv.services/no_backup/appmetrica/analytics/appmetrica_vital*.dat "
                         "/data/data/com.yandex.tv.services/no_backup/appmetrica/analytics/appmetrica*.db "
                         "/data/data/com.yandex.io.sdk/databases/appmetrica* 2>/dev/null; true")

    # Ensure clock pattern and anim directories exist
    try:
        clock_dir = s.PATTERNS["clock"]
        os.makedirs(clock_dir, exist_ok=True)
        os.makedirs(f"{s.MY_ANIM_DIR}/clock", exist_ok=True)
        os.makedirs(f"{s.MY_ANIM_DIR}/ring", exist_ok=True)
        os.makedirs(f"{s.OVR_DIR}/ledpatterns/clock", exist_ok=True)
        if not os.path.exists(s.BOOT_TARGET):
            for fname in ("boot.binscled.zst", "alice_listening.binscled.zst", "alice_thinking.binscled.zst", "error.binscled.zst", "timer.binscled.zst"):
                src = f"/data/local/tmp/{fname}"
                dst = f"{clock_dir}/{fname}"
                if os.path.exists(src) and not os.path.exists(dst):
                    try:
                        import shutil
                        shutil.copyfile(src, dst)
                    except Exception:
                        pass
    except Exception:
        pass

    original_set_volume = P.set_volume

    AdbError = getattr(s, "AdbError", Exception)
    strip_ansi = getattr(s, "strip_ansi", lambda x: x)

    def max_sounds(self):
        """List sound files from /data/data/com.yandex.io.sdk/files/system/sounds."""
        out, _, _ = self.adb.shell(f"cd {s.SOUNDS_DIR} 2>/dev/null && find . -type f | sort", check=False)
        return [l[2:] for l in out.splitlines() if l.startswith("./")]

    P.sounds = max_sounds

    def max_stop_sound(self):
        """Stop sound playback immediately on Android."""
        self.adb.shell("pkill -9 -f PlayAudio 2>/dev/null; /odm/bin/playaudio --stop 2>/dev/null; killall aplay 2>/dev/null; true", check=False)
        return {"ok": True}

    P.stop_sound = max_stop_sound

    def max_play_remote_wav(self, path, cleanup=False):
        """Play remote audio file using /odm/bin/playaudio wrapper (Android MediaPlayer)."""
        self.stop_sound()
        q = shlex.quote(path)
        rm = f"; rm -f {q}" if cleanup else ""
        play_log = getattr(s, "PLAY_LOG", "/tmp/webpanel_play.log")
        q_log = shlex.quote(play_log)
        self.adb.shell(f": > {q_log} 2>/dev/null; chmod 666 {q_log} 2>/dev/null; true", check=False)
        self.detach("play", f"if [ -x /odm/bin/playaudio ]; then /odm/bin/playaudio {q} >{q_log} 2>&1{rm}; else aplay -q {q} >{q_log} 2>&1{rm}; fi")
        time.sleep(0.2)
        # playaudio may not have written anything yet: re-read the log briefly instead of
        # treating the first empty/error-looking chunk as a final failure.
        log = ""
        for attempt in range(3):
            try:
                res = self.adb.shell(f"tail -c 400 {q_log} 2>/dev/null; true", check=False)
                log = strip_ansi(_shell_result(res)[0]).strip()
            except Exception:
                log = ""
            if log and not re.search(r"(?i)(error|cannot|failed|unable|busy|exception|fatal)", log):
                break
            if attempt < 2:
                time.sleep(0.2 if log else 0.15)
        if re.search(r"(?i)(error|cannot|failed|unable|busy|exception|fatal)", log):
            raise AdbError("Не удалось проиграть звук: " + log[-300:])

    P._play_remote_wav = max_play_remote_wav

    def max_play_path(self, path):
        """Play audio file on Station Max. Native Android MediaPlayer plays WAV, MP3, OPUS, OGG directly."""
        self._play_remote_wav(path)
        return {"ok": True}

    P._play_path = max_play_path

    def max_play_system(self, name):
        all_sounds = self.sounds()
        if name not in all_sounds:
            matched = [snd for snd in all_sounds if snd.endswith("/" + name) or snd == name]
            if matched:
                name = matched[0]
            else:
                raise AdbError("Звук не найден")
        return self._play_path(f"{s.SOUNDS_DIR}/{name}")

    P.play_system = max_play_system

    def max_sound_paths(self, name):
        all_sounds = self.sounds()
        if name not in all_sounds:
            matched = [snd for snd in all_sounds if snd.endswith("/" + name) or snd == name]
            if matched:
                name = matched[0]
            else:
                raise AdbError("Звук не найден")
        return f"{s.SOUNDS_DIR}/{name}", f"{s.OVR_DIR}/sounds/{name}"

    P._sound_paths = max_sound_paths

    def max_sound_play_original(self, name):
        target, _ = self._sound_paths(name)
        data = self._original_bytes(target)
        ext = os.path.splitext(target)[1] or ".wav"
        remote = f"{s.WEB_DIR}/play-orig-{os.getpid()}-{os.urandom(4).hex()}{ext}"
        self.adb.shell(f"mkdir -p {s.WEB_DIR} && chmod 777 {s.WEB_DIR}", check=False)
        with tempfile.TemporaryDirectory() as td:
            tmp_local = Path(td) / f"orig{ext}"
            tmp_local.write_bytes(data)
            self.adb.push(tmp_local, remote)
        self._play_remote_wav(remote, cleanup=True)
        return {"ok": True}

    P.sound_play_original = max_sound_play_original

    def max_play_upload(self, filename, body):
        ext = Path(filename).suffix or ".wav"
        remote = f"{s.WEB_DIR}/play-upload-{os.getpid()}-{os.urandom(4).hex()}{ext}"
        self.adb.shell(f"mkdir -p {s.WEB_DIR} && chmod 777 {s.WEB_DIR}", check=False)
        with tempfile.TemporaryDirectory() as td:
            tmp_local = Path(td) / f"upload{ext}"
            tmp_local.write_bytes(body)
            self.adb.push(tmp_local, remote)
        self._play_remote_wav(remote, cleanup=True)
        return {"ok": True}

    P.play_upload = max_play_upload

    def max_sound_replace(self, name, body):
        if len(body) > getattr(s, "SOUND_UPLOAD_LIMIT", 50 * 1024 * 1024):
            raise AdbError("Файл слишком большой")
        target, custom = self._sound_paths(name)
        if target == s.SOUND_TARGET:
            return self.boot_sound_install(name, body)
        if getattr(self, "ffmpeg", None):
            processed = self._audio_like(self._original_bytes(target), body)
        else:
            processed = body
        self._install_override(processed, custom, target)
        return self.sounds_custom()

    P.sound_replace = max_sound_replace

    def max_sounds_custom(self):
        out, _, _ = self.adb.shell(
            f"cd {s.OVR_DIR}/sounds 2>/dev/null && find . -type f; [ -f {s.SOUND_CUSTOM} ] && echo @boot; "
            f"grep ' {s.SOUNDS_DIR}/' /proc/mounts | cut -d' ' -f2; true", check=False)
        lines = strip_ansi(out).split()
        mounted = {l[len(s.SOUNDS_DIR) + 1:] for l in lines if l.startswith(s.SOUNDS_DIR + "/")}
        custom = {l[2:] for l in lines if l.startswith("./")}
        if "@boot" in lines:
            custom.add(posixpath.relpath(s.SOUND_TARGET, s.SOUNDS_DIR))
        return {"custom": sorted(custom), "pending": sorted(custom - mounted), "ffmpeg": True}

    P.sounds_custom = max_sounds_custom

    def max_version(self):
        if getattr(self, "_version", None) is None:
            dumpsys_pkg = max_android_cmd(self.adb, ["dumpsys", "package", "com.yandex.io.sdk"])
            m_vn = re.search(r"versionName=([^\s]+)", dumpsys_pkg)
            vn = m_vn.group(1).strip() if m_vn else None

            out_b, _, _ = self.adb.shell("getprop ro.yndx.build.version 2>/dev/null", check=False)
            b_str = out_b.strip()
            m_build = re.search(r"/(\d+)", b_str)
            if vn and m_build:
                ver = f"{vn} ({m_build.group(1)})"
            elif vn:
                ver = vn
            elif b_str:
                ver = b_str
            else:
                ver = "3.367.25"

            did_out, _, _ = self.adb.shell("getprop ro.serialno 2>/dev/null; getprop ro.boot.serialno 2>/dev/null", check=False)
            dids = [l.strip() for l in did_out.splitlines() if l.strip()]
            did = dids[0] if dids else "X11RW6400260XK"

            self._version = {"quasar_version": ver, "device_id": did, "model": "max"}
            if getattr(self, "glagol", None):
                self._configure_glagol(did)
        return self._version

    P.version = max_version

    def max_volume_map(self):
        # 0..20 volume mapping for Max (scaled 0..100 in Android)
        return [i * 5 for i in range(21)]

    P._device_volume_map = max_volume_map

    def max_read_volume(self):
        # Read volume from Glagol if connected, otherwise from Android media volume
        glagol_info = self.glagol.info() if getattr(self, "glagol", None) else {}
        val = (glagol_info.get("state") or {}).get("volume") if glagol_info.get("connected") else None
        if isinstance(val, (int, float)) and 0 <= val <= 1:
            return round(val * 100), "glagol"
        try:
            out, _, _ = self.adb.shell("media volume --stream 3 --get 2>/dev/null", check=False)
            m = re.search(r"volume is (\d+)", out)
            if m:
                return int(m.group(1)), "android"
        except Exception:
            pass
        return 50, "default"

    def max_status(self):
        """Native status collector for Android TV 9 on Station Max."""
        status_script = r"""
echo "hostname=$(getprop net.hostname || hostname)"
echo "hardware_model=$(getprop ro.product.model || cat /proc/device-tree/model 2>/dev/null | tr '\000' ' ')"
echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
awk '/MemTotal/{print "mem_total="$2} /MemAvailable/{print "mem_avail="$2}' /proc/meminfo
echo "temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
echo "kernel=$(uname -r)"
echo "color=$(getprop ro.boot.color || echo graphite)"
echo "date=$(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "ip_address=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"
iw dev wlan0 link 2>/dev/null | grep -q 'signal:' && iw dev wlan0 link 2>/dev/null | awk '/SSID:/{print "ssid="$2} /signal:/{print "rssi="$2} /freq:/{print "freq="$2} /tx bitrate:/{print "linkspeed="$3}' || dumpsys wifi 2>/dev/null | grep -m 1 'mWifiInfo'
pidof com.yandex.io.sdk >/dev/null && echo "quasar=running" || echo "quasar=stopped"
"""
        out, _, _ = self.adb.shell(status_script, check=False)
        st = {
            "bt_connected": [],
            "matrix": {"cols": MAX_SCREEN_COLS, "rows": MAX_SCREEN_ROWS, "pixels": MAX_SCREEN_PIXELS},
            "ring_leds": MAX_RING_LEDS,
        }
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" in line and not line.startswith("mWifiInfo"):
                k, v = line.split("=", 1)
                st[k.strip()] = v.strip()
            elif "mWifiInfo" in line:
                m_ssid = re.search(r"SSID:\s*([^,\r\n]+)", line)
                if m_ssid:
                    st["ssid"] = m_ssid.group(1).strip()
                m_rssi = re.search(r"RSSI:\s*([-\d]+)", line)
                if m_rssi:
                    st["rssi"] = m_rssi.group(1).strip()
                m_speed = re.search(r"Link speed:\s*(\d+)Mbps", line)
                if m_speed:
                    st["linkspeed"] = m_speed.group(1).strip()
                m_freq = re.search(r"Frequency:\s*(\d+)MHz", line)
                if m_freq:
                    st["freq"] = m_freq.group(1).strip()

        if "rssi" in st and "RSSI" not in st:
            st["RSSI"] = st["rssi"]
        if "linkspeed" in st and "LINKSPEED" not in st:
            st["LINKSPEED"] = st["linkspeed"]

        if "color" not in st or not st["color"]:
            st["color"] = "graphite"

        # Volume
        level, source = max_read_volume(self)
        st.update(vol_level=level, vol_master=level, volume_source=source)
        st["muted"] = (level == 0)

        # Bluetooth metadata
        bt = max_bt_info(self.adb)
        if bt:
            st["bt_media"] = bt
            if bt.get("title"):
                st["bt_connected"] = [{"name": bt["title"], "type": "audio"}]

        # Device identity and version
        st.update(self.version())

        # MAC Address
        addr = ""
        for iface in ("wlan0", "eth0"):
            p = Path(f"/sys/class/net/{iface}/address")
            if p.exists():
                try:
                    addr = p.read_text().strip()
                    if addr:
                        break
                except Exception:
                    pass
        if not addr:
            out, _, _ = self.adb.shell("cat /sys/class/net/wlan0/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null", check=False)
            addr = out.strip()
        st["address"] = addr
        st["ffmpeg"] = True

        return st

    P.status = max_status

    def max_set_volume(self, data):
        level = None
        if "level" in data:
            level = max(0, min(100, int(round(float(data["level"])))))
        elif "master" in data:
            level = max(0, min(100, int(round(float(data["master"])))))

        if level is not None:
            # Sync to Glagol
            if getattr(self, "glagol", None) and getattr(self.glagol, "ws", None):
                try:
                    self.glagol.send({"command": "setVolume", "volume": level / 100.0})
                except Exception:
                    pass
            # Sync to Android AudioFlinger stream 3 (STREAM_MUSIC)
            self.adb.shell(f"media volume --stream 3 --set {level} 2>/dev/null; true", check=False)
            return {"ok": True, "level": level}
        return original_set_volume(self, data)

    P.set_volume = max_set_volume

    class MaxAsyncCapture:
        """High-speed in-memory capture thread directly reading framebuffers."""
        def __init__(self, adb):
            self.adb = adb
            self.pixels = [0] * MAX_SCREEN_PIXELS
            self.ring_all = [[0, 0, 0]] * MAX_RING_LEDS
            self.lock = threading.Lock()
            self.running = True
            self.at = time.time()
            self.is_local = getattr(adb, "is_local", False) or os.path.exists(MAX_SCREEN_DEV)
            if self.is_local:
                threading.Thread(target=self._screen_loop, daemon=True, name="max-screen-fb4").start()
                threading.Thread(target=self._ring_loop, daemon=True, name="max-ring-fb5").start()
            else:
                threading.Thread(target=self._remote_loop, daemon=True, name="max-remote-fb").start()

        def _screen_loop(self):
            fd = None
            try:
                fd = open(MAX_SCREEN_DEV, "rb", buffering=0)
            except Exception:
                pass
            while self.running:
                try:
                    if fd is None:
                        fd = open(MAX_SCREEN_DEV, "rb", buffering=0)
                    fd.seek(0)
                    raw = fd.read(MAX_SCREEN_BYTES)
                    if len(raw) >= MAX_SCREEN_BYTES:
                        px = [raw[i * 3] for i in range(MAX_SCREEN_PIXELS)]
                        with self.lock:
                            self.pixels = px
                            self.at = time.time()
                except Exception:
                    if fd:
                        try: fd.close()
                        except Exception: pass
                    fd = None
                time.sleep(0.035)  # ~28 FPS
            if fd:
                try: fd.close()
                except Exception: pass

        def _ring_loop(self):
            fd = None
            try:
                fd = open(MAX_RING_DEV, "rb", buffering=0)
            except Exception:
                pass
            while self.running:
                try:
                    if fd is None:
                        fd = open(MAX_RING_DEV, "rb", buffering=0)
                    fd.seek(0)
                    raw = fd.read(MAX_RING_LEDS * 3)
                    if len(raw) >= 72:
                        rg = [[raw[i * 3 + 2], raw[i * 3 + 1], raw[i * 3]] for i in range(MAX_RING_LEDS)]
                        with self.lock:
                            self.ring_all = rg
                            self.at = time.time()
                except Exception:
                    if fd:
                        try: fd.close()
                        except Exception: pass
                    fd = None
                time.sleep(0.04) # ~25 FPS
            if fd:
                try: fd.close()
                except Exception: pass

        def _remote_loop(self):
            while self.running:
                try:
                    cmd = (
                        f"toybox head -c {MAX_RING_LEDS * 3} {MAX_RING_DEV} 2>/dev/null | toybox base64; "
                        f"echo '---'; "
                        f"toybox head -c {MAX_SCREEN_BYTES} {MAX_SCREEN_DEV} 2>/dev/null | toybox base64"
                    )
                    out, _, _ = self.adb.shell(cmd, check=False, timeout=3)
                    if "---" in out:
                        parts = out.split("---")
                        ring_raw = base64.b64decode(parts[0].strip())
                        screen_raw = base64.b64decode(parts[1].strip() if len(parts) > 1 else "")
                        with self.lock:
                            if len(ring_raw) >= 72:
                                self.ring_all = [[ring_raw[i * 3 + 2], ring_raw[i * 3 + 1], ring_raw[i * 3]] for i in range(MAX_RING_LEDS)]
                            if len(screen_raw) >= MAX_SCREEN_BYTES:
                                self.pixels = [screen_raw[i * 3] for i in range(MAX_SCREEN_PIXELS)]
                            self.at = time.time()
                except Exception:
                    pass
                time.sleep(0.045)

        def get(self):
            with self.lock:
                pixels = list(self.pixels)
                ring_all = list(self.ring_all)
                t = self.at
            rep_ring = [ring_all[0], ring_all[8], ring_all[16]]
            return {
                "v": pixels[:36],
                "pixels": pixels,
                "matrix": {"cols": MAX_SCREEN_COLS, "rows": MAX_SCREEN_ROWS, "pixels": pixels},
                "ring": rep_ring,
                "ring_all": ring_all,
                "fps": 22,
                "t": t,
            }

    _fast_capture = None

    def get_capture(panel):
        nonlocal _fast_capture
        if _fast_capture is None:
            _fast_capture = MaxAsyncCapture(panel.adb)
        return _fast_capture

    def max_leds(self):
        return get_capture(self).get()

    P.clock_state = max_leds

    class MaxLedStream:
        def __init__(self, adb):
            self.adb = adb
        def get(self):
            return max_leds(self)

    s.LedStream = MaxLedStream

    _ring_pattern_thread = None
    _ring_pattern_stop = False

    def max_write_ring_raw(panel, raw_bytes):
        """Write 72-byte buffer to fb5 with FBIOPAN_DISPLAY ioctl."""
        if getattr(panel.adb, "is_local", False) or os.path.exists(MAX_RING_DEV):
            try:
                import fcntl
                with open(MAX_RING_DEV, "r+b", buffering=0) as f:
                    f.seek(0)
                    f.write(raw_bytes[:72])
                    vinfo = bytearray(160)
                    try:
                        fcntl.ioctl(f.fileno(), 0x4600, vinfo) # FBIOGET_VSCREENINFO
                        fcntl.ioctl(f.fileno(), 0x4606, vinfo) # FBIOPAN_DISPLAY
                    except Exception:
                        pass
                return
            except Exception:
                pass
        b64 = base64.b64encode(raw_bytes[:72]).decode("ascii")
        cmd = f"echo '{b64}' | toybox base64 -d > {MAX_RING_DEV} 2>/dev/null; echo 0,0 > /sys/class/graphics/fb5/pan 2>/dev/null; true"
        panel.adb.shell(cmd, check=False)

    def max_stop_ring_pattern(panel=None):
        nonlocal _ring_pattern_stop, _ring_pattern_thread
        _ring_pattern_stop = True
        if _ring_pattern_thread and _ring_pattern_thread.is_alive():
            _ring_pattern_thread.join(timeout=0.3)
        _ring_pattern_thread = None

    def max_ring(self, data):
        """Set ring RGB LEDs via /dev/graphics/fb5 (BGR24 layout) with ioctl FBIOPAN_DISPLAY."""
        max_stop_ring_pattern(self)
        if isinstance(data, dict):
            r = max(0, min(255, int(data.get("red", 0))))
            g = max(0, min(255, int(data.get("green", 0))))
            b = max(0, min(255, int(data.get("blue", 0))))
            colors = [(r, g, b)] * MAX_RING_LEDS
        elif isinstance(data, list):
            colors = data[:MAX_RING_LEDS]
            while len(colors) < MAX_RING_LEDS:
                colors.append((0, 0, 0))
        else:
            return {"ok": False, "error": "invalid ring data"}

        buf = bytearray(MAX_RING_LEDS * 3)
        for i, c in enumerate(colors):
            # Hardware expects BGR24: Byte 0 = Blue, Byte 1 = Green, Byte 2 = Red
            buf[i * 3] = max(0, min(255, int(c[2])))     # Blue
            buf[i * 3 + 1] = max(0, min(255, int(c[1]))) # Green
            buf[i * 3 + 2] = max(0, min(255, int(c[0]))) # Red

        max_write_ring_raw(self, buf)
        return {"ok": True}

    P.ring = max_ring

    def max_ring_reset(self):
        """Clear ring LEDs."""
        max_stop_ring_pattern(self)
        max_write_ring_raw(self, bytes(72))
        return {"ok": True, "leds": [[0, 0, 0]] * 3}

    P.ring_reset = max_ring_reset

    def max_patterns(self):
        """Return stock and custom animations for Station Max."""
        res = {}
        for kind in ("ring", "clock"):
            d = s.PATTERNS.get(kind, "")
            my_d = f"{s.MY_ANIM_DIR}/{kind}"
            std = []
            if d and os.path.exists(d):
                ext = ".led" if kind == "ring" else ".binscled"
                std = [f for f in os.listdir(d) if ext in f and not f.startswith(".")]
            elif d:
                out, _, _ = self.adb.shell(f"ls {shlex.quote(d)} 2>/dev/null; true", check=False)
                ext = ".led" if kind == "ring" else ".binscled"
                std = [l.strip() for l in out.splitlines() if ext in l and not l.strip().startswith(".")]

            mine = []
            if os.path.exists(my_d):
                mine = [f"my/{f}" for f in os.listdir(my_d) if not f.startswith(".")]
            else:
                out, _, _ = self.adb.shell(f"for f in {shlex.quote(my_d)}/*; do [ -f \"$f\" ] && echo \"${{f##*/}}\"; done; true", check=False)
                mine = [f"my/{l.strip()}" for l in out.splitlines() if l.strip()]

            res[kind] = sorted(std) + sorted(mine)
        return res

    P.patterns = max_patterns

    def max_play_pattern(self, kind, name, seconds=5):
        if kind == "ring":
            return max_play_ring_pattern(self, name, seconds)
        elif kind == "clock":
            frames = self.clock_frames(name)
            return self.play_clock(frames, seconds)
        return {"ok": False, "error": "unsupported"}

    def max_play_clock(self, frames, seconds=10):
        """Play clock animation frames on Station Max 25x16 dot matrix screen."""
        if not frames:
            raise AdbError("Нет кадров для анимации")

        self.display_stop()

        play_seq = []
        for f in frames:
            v = f.get("v")
            if not v:
                continue
            if len(v) == 36:
                raw_buf = v36_to_matrix(v)
            elif len(v) == 400:
                grid = [[v[r * 25 + c] for c in range(25)] for r in range(16)]
                raw_buf = matrix_grid_to_bytes(grid)
            elif len(v) == 1200:
                raw_buf = bytes(v)
            else:
                continue
            ms = max(20, min(60000, int(f.get("ms", 100))))
            play_seq.append((raw_buf, ms / 1000.0))

        if not play_seq:
            raise AdbError("Не удалось разобрать кадры анимации")

        disp_mgr = get_disp_mgr(self)

        def _play_worker():
            max_pause_alice_leds(self.adb)
            start_time = time.time()
            idx = 0
            while time.time() - start_time < seconds:
                raw_buf, dur = play_seq[idx]
                disp_mgr._write_frame(raw_buf)
                time.sleep(dur)
                idx += 1
                if idx >= len(play_seq):
                    if time.time() - start_time >= seconds * 0.8:
                        break
                    idx = 0
            disp_mgr.stop()
            max_resume_alice_leds(self.adb)

        t = threading.Thread(target=_play_worker, daemon=True)
        t.start()
        return {"ok": True, "frames": len(play_seq)}

    P.play_clock = max_play_clock

    def max_ring_try(self, text, seconds=5):
        """Play custom ring animation text directly on Station Max fb5."""
        max_stop_ring_pattern(self)
        parsed = s.parse_led(text)
        frames = parsed.get("frames", [])
        if not frames:
            return {"ok": False, "error": "Empty pattern"}

        nonlocal _ring_pattern_thread, _ring_pattern_stop
        _ring_pattern_stop = False

        def _worker():
            end_time = (time.time() + seconds) if seconds > 0 else float("inf")
            idx = 0
            while not _ring_pattern_stop and time.time() < end_time:
                if idx >= len(frames):
                    if not parsed.get("loop", False):
                        break
                    idx = 0
                frame = frames[idx]
                c_list = frame.get("c", [])
                max_write_ring_raw(self, c_list)
                ms = max(20, min(10000, frame.get("ms", 50)))
                time.sleep(ms / 1000.0)
                idx += 1
            max_write_ring_raw(self, [[0, 0, 0]] * MAX_RING_LEDS)

        _ring_pattern_thread = threading.Thread(target=_worker, daemon=True)
        _ring_pattern_thread.start()
        return {"ok": True}

    P.ring_try = max_ring_try

    def max_play_ring_pattern(self, name, seconds=5):
        max_stop_ring_pattern(self)
        led_path = f"{s.QUASAR}/ledpatterns/{name}"
        try:
            if os.path.exists(led_path):
                with open(led_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            else:
                text = self.adb.read_bytes(led_path).decode("utf-8", "replace")
        except Exception as e:
            return {"ok": False, "error": f"Pattern not found: {e}"}

        parsed = s.parse_led(text)
        frames = parsed.get("frames", [])
        if not frames:
            return {"ok": False, "error": "Empty pattern"}

        nonlocal _ring_pattern_thread, _ring_pattern_stop
        _ring_pattern_stop = False

        def _worker():
            end_time = (time.time() + seconds) if seconds > 0 else float("inf")
            idx = 0
            while not _ring_pattern_stop and time.time() < end_time:
                if idx >= len(frames):
                    if not parsed.get("loop", False):
                        break
                    idx = 0
                frame = frames[idx]
                c_list = frame.get("c", [])
                buf = bytearray(MAX_RING_LEDS * 3)
                for i in range(min(MAX_RING_LEDS, len(c_list))):
                    rgb = c_list[i]
                    buf[i * 3] = max(0, min(255, int(rgb[2])))     # Blue
                    buf[i * 3 + 1] = max(0, min(255, int(rgb[1]))) # Green
                    buf[i * 3 + 2] = max(0, min(255, int(rgb[0]))) # Red
                max_write_ring_raw(self, buf)
                idx += 1
                ms = max(16, frame.get("ms", 50))
                time.sleep(ms / 1000.0)
            if not _ring_pattern_stop:
                max_write_ring_raw(self, bytes(72))

        _ring_pattern_thread = threading.Thread(target=_worker, daemon=True, name="max-ring-pat")
        _ring_pattern_thread.start()
        return {"ok": True}

    P.play_pattern = max_play_pattern
    P.play_ring_pattern = max_play_ring_pattern
    P.stop_ring_pattern = max_stop_ring_pattern

    def max_write_matrix_raw(self, raw_bytes):
        """Write 1200-byte buffer to fb4 with paused Alice screen thread."""
        max_pause_alice_leds(self.adb)
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        cmd = f"echo '{b64}' | toybox base64 -d > {MAX_SCREEN_DEV} 2>/dev/null; true"
        self.adb.shell(cmd, check=False)

    class MaxDisplayManager:
        """Thread-safe persistent display manager for Station Max 25x16 screen."""
        def __init__(self, adb):
            self.adb = adb
            self.is_local = getattr(adb, "is_local", False) or os.path.exists(MAX_SCREEN_DEV)
            self.lock = threading.Lock()
            self.running = False
            self.mode = None
            self.raw_frame = None
            self.text = None
            self.end_time = 0.0
            self.thread = None
            self.brightness = 180

        def pin_text(self, text, brightness=180, seconds=0, mode="text"):
            raw = render_matrix_text(text, brightness)
            self._start(mode, raw, seconds, text=text, brightness=brightness)
            return {"mode": mode, "text": text}

        def pin_frame(self, frame, brightness=180, seconds=0):
            if len(frame) >= MAX_SCREEN_PIXELS:
                raw = bytearray(MAX_SCREEN_BYTES)
                for i, val in enumerate(frame[:MAX_SCREEN_PIXELS]):
                    b = max(0, min(255, int(val)))
                    raw[i * 3] = b
                    raw[i * 3 + 1] = b
                    raw[i * 3 + 2] = b
                raw = bytes(raw)
            elif len(frame) == 36:
                raw = v36_to_matrix(frame)
            else:
                raw = render_matrix_text(str(frame), brightness)
            self._start("frame", raw, seconds, brightness=brightness)
            return {"mode": "frame"}

        def pin_timer(self, seconds, brightness=180):
            mins, secs = divmod(seconds, 60)
            text = f"{mins:02d}:{secs:02d}"
            raw = render_matrix_text(text, brightness)
            self._start("timer", raw, seconds, text=text, brightness=brightness)
            return {"mode": "timer", "left": seconds}

        def pin_date(self, brightness=180):
            now = time.localtime()
            text = f"{now.tm_mday:02d}.{now.tm_mon:02d}"
            raw = render_matrix_text(text, brightness)
            self._start("date", raw, 0, text=text, brightness=brightness)
            return {"mode": "date", "text": text}

        def pin_temp(self, brightness=180):
            temp_val = 24
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    t = float(f.read().strip())
                    temp_val = round(t / 1000)
            except Exception:
                pass
            text = f"{temp_val}°"
            raw = render_matrix_text(text, brightness)
            self._start("temp", raw, 0, text=text, brightness=brightness)
            return {"mode": "temp", "text": text}

        def stop(self):
            with self.lock:
                self.running = False
                self.mode = None
                self.raw_frame = None
                self.text = None
                self.end_time = 0.0
                try:
                    now_str = time.strftime("%H:%M")
                    cur_clock = render_matrix_text(now_str, 180)
                    if self.is_local:
                        with open(MAX_SCREEN_DEV, "wb") as f:
                            f.write(cur_clock)
                except Exception:
                    pass
                max_resume_alice_leds(self.adb)
            return {"mode": None}

        def status(self):
            with self.lock:
                if not self.running:
                    return {"mode": None}
                res = {"mode": self.mode}
                if self.mode == "timer":
                    res["left"] = max(0, int(self.end_time - time.time()))
                elif self.text:
                    res["text"] = self.text
                return res

        def _start(self, mode, raw, seconds, text=None, brightness=180):
            with self.lock:
                max_pause_alice_leds(self.adb)
                self.running = True
                self.mode = mode
                self.raw_frame = raw
                self.text = text
                self.brightness = brightness
                self.end_time = (time.time() + seconds) if seconds > 0 else 0.0
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._worker_loop, daemon=True, name="max-disp-hold")
                    self.thread.start()

        def _worker_loop(self):
            last_written_raw = None
            last_refresh_time = 0.0
            last_timer_sec = -1
            last_timer_raw = None
            last_temp_poll = 0.0
            last_temp_raw = None
            last_date_str = ""
            last_date_raw = None
            while True:
                with self.lock:
                    if not self.running:
                        break
                    now = time.time()
                    if self.end_time > 0 and now >= self.end_time:
                        self.running = False
                        self.mode = None
                        self.raw_frame = None
                        try:
                            now_str = time.strftime("%H:%M")
                            cur_clock = render_matrix_text(now_str, 180)
                            if self.is_local:
                                with open(MAX_SCREEN_DEV, "wb") as f:
                                    f.write(cur_clock)
                        except Exception:
                            pass
                        max_resume_alice_leds(self.adb)
                        break
                    mode = self.mode
                    raw = self.raw_frame
                    end = self.end_time
                    br = self.brightness

                if mode == "timer":
                    left = max(0, int(end - time.time()))
                    if left != last_timer_sec:
                        last_timer_sec = left
                        mins, secs = divmod(left, 60)
                        t_str = f"{mins:02d}:{secs:02d}"
                        last_timer_raw = render_matrix_text(t_str, br)
                    current_raw = last_timer_raw
                elif mode == "temp":
                    if (now - last_temp_poll) >= 2.0:
                        last_temp_poll = now
                        temp_val = 24
                        try:
                            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                                temp_val = round(float(f.read().strip()) / 1000)
                        except Exception:
                            pass
                        t_str = f"{temp_val}°"
                        with self.lock:
                            self.text = t_str
                        last_temp_raw = render_matrix_text(t_str, br)
                    current_raw = last_temp_raw or raw
                elif mode == "date":
                    now_tm = time.localtime()
                    d_str = f"{now_tm.tm_mday:02d}.{now_tm.tm_mon:02d}"
                    if d_str != last_date_str:
                        last_date_str = d_str
                        with self.lock:
                            self.text = d_str
                        last_date_raw = render_matrix_text(d_str, br)
                    current_raw = last_date_raw or raw
                else:
                    current_raw = raw

                now = time.time()
                # Write to fb4 only if frame changed or every 500ms
                if current_raw and (current_raw != last_written_raw or (now - last_refresh_time) >= 2.0):
                    if len(current_raw) >= MAX_SCREEN_BYTES:
                        try:
                            if self.is_local:
                                with open(MAX_SCREEN_DEV, "wb") as f:
                                    f.write(current_raw)
                            else:
                                b64 = base64.b64encode(current_raw).decode("ascii")
                                cmd = f"echo '{b64}' | toybox base64 -d > {MAX_SCREEN_DEV} 2>/dev/null; true"
                                self.adb.shell(cmd, check=False)
                            last_written_raw = current_raw
                            last_refresh_time = now
                        except Exception:
                            pass

                time.sleep(0.04)

        def _write_frame(self, raw):
            if len(raw) >= MAX_SCREEN_BYTES:
                try:
                    if self.is_local:
                        with open(MAX_SCREEN_DEV, "wb") as f:
                            f.write(raw[:MAX_SCREEN_BYTES])
                    else:
                        b64 = base64.b64encode(raw[:MAX_SCREEN_BYTES]).decode("ascii")
                        cmd = f"echo '{b64}' | toybox base64 -d > {MAX_SCREEN_DEV} 2>/dev/null; true"
                        self.adb.shell(cmd, check=False)
                except Exception:
                    pass

    _disp_mgr = None

    def get_disp_mgr(panel):
        nonlocal _disp_mgr
        if _disp_mgr is None:
            adb_obj = getattr(panel, "adb", panel)
            _disp_mgr = MaxDisplayManager(adb_obj)
        return _disp_mgr

    def max_clock(self, data):
        """Display text, 36-segment frame, or 400 pixels on 25x16 screen. Holds for 5s."""
        mgr = get_disp_mgr(self)
        bright = int(data.get("brightness", 180))
        text = data.get("text")
        if text is not None and str(text).strip():
            return mgr.pin_text(str(text), bright, seconds=5, mode="once")
        if "pixels" in data:
            return mgr.pin_frame(data["pixels"], bright, seconds=5)
        if "v" in data:
            return mgr.pin_frame(data["v"], bright, seconds=5)
        return {"ok": True}

    P.clock = max_clock

    def max_display_set(self, data):
        """Set screen mode (timer, temp, date, frame, text) with persistent hold."""
        mgr = get_disp_mgr(self)
        mode = data.get("mode")
        bright = int(data.get("brightness", 180))

        if mode == "timer":
            seconds = int(data.get("seconds", 60))
            return mgr.pin_timer(seconds, bright)
        elif mode == "date":
            return mgr.pin_date(bright)
        elif mode == "temp":
            return mgr.pin_temp(bright)
        elif mode == "frame" and "pixels" in data:
            return mgr.pin_frame(data["pixels"], bright)
        elif mode == "frame" and "v" in data:
            return mgr.pin_frame(data["v"], bright)
        elif "text" in data and str(data["text"]).strip():
            return mgr.pin_text(str(data["text"]), bright)

        return {"ok": True}

    P.display_set = max_display_set

    def max_display_stop(self):
        return get_disp_mgr(self).stop()

    P.display_stop = max_display_stop

    def max_display_status(self):
        return get_disp_mgr(self).status()

    P.display_status = max_display_status

    def max_stop_pattern(self, kind=None):
        """Stop custom graphics and resume Alice's clock."""
        if kind in (None, "clock", "screen"):
            get_disp_mgr(self).stop()
        if kind in (None, "ring"):
            max_stop_ring_pattern(self)
            max_ring_reset(self)
        return {"ok": True}

    P.stop_pattern = max_stop_pattern

    def max_say(self, text, voice="alice"):
        """Произнести текст на Станции Макс: через Glagol или TTS fallback."""
        text = re.sub(r"[\x00-\x1f]", " ", str(text or "")).strip()
        if not 1 <= len(text) <= 300:
            raise AdbError("текст — от 1 до 300 символов")
        if voice == "assistant":
            return self.assistant_command({"say": text})

        # 1. Если Glagol онлайн — говорим голосом Алисы
        if getattr(self, "glagol", None) and self.glagol.ws:
            try:
                self.glagol.send({"command": "sendText", "text": f"Повтори за мной {text}"})
                return {"ok": True, "method": "glagol"}
            except Exception:
                pass

        # Локальный Android: штатные start-stop-daemon и playaudio могут отсутствовать.
        if getattr(self.adb, "is_local", False):
            try:
                _max_android_say(text)
            except Exception as exc:
                # Не записываем текст в журнал: HTTP-ошибка TTS может содержать исходный URL.
                _log.warning("Не удалось воспроизвести текст на Станции Макс: %s", type(exc).__name__)
                raise AdbError(f"Озвучивание не удалось: {type(exc).__name__}") from exc
            return {"ok": True, "method": "tts_android"}

        # 2. Native TTS fallback
        tts_url = "https://translate.google.com/translate_tts?ie=UTF-8&tl=ru&client=tw-ob&q=" + urllib.parse.quote(text)
        script = (
            f"python3 -c \"import urllib.request, os; "
            f"req = urllib.request.Request('{tts_url}', headers={{'User-Agent': 'Mozilla/5.0'}}); "
            f"data = urllib.request.urlopen(req, timeout=10).read(); "
            f"open('/data/local/tmp/say.mp3', 'wb').write(data); "
            f"os.chmod('/data/local/tmp/say.mp3', 0o644)\" && "
            f"/odm/bin/playaudio /data/local/tmp/say.mp3 >/dev/null 2>&1"
        )
        self.detach("say", script)
        return {"ok": True, "method": "tts"}

    P.say = max_say

    def max_bt_position(self, title):
        bt = max_bt_info(self.adb)
        if bt and bt.get("position") is not None:
            return (bt["position"], time.time(), bt.get("title") or title)
        return None

    P._bt_position = max_bt_position

    def max_bt_state(self):
        bt_off_path = getattr(s, "BT_OFF", "/data/quasar/webpanel/bt_off")
        keep_off = max_is_bt_off(self.adb, bt_off_path)

        dumpsys_out = max_android_cmd(self.adb, ["dumpsys", "bluetooth_manager"])

        m_enabled = re.search(r"enabled:\s*(true|false)", dumpsys_out, re.I)
        if m_enabled:
            powered = (m_enabled.group(1).lower() == "true")
        else:
            powered = bool(re.search(r"State:\s*ON", dumpsys_out, re.I))

        if keep_off:
            powered = False

        m_name = re.search(r"^\s*name:\s*(.+)$", dumpsys_out, re.M | re.I)
        alias = m_name.group(1).strip() if m_name else "Яндекс Станция Макс"

        m_scan = re.search(r"ScanMode:\s*(\w+)", dumpsys_out, re.I)
        scan_mode = m_scan.group(1).upper() if m_scan else ""
        is_disc = ("DISCOVERABLE" in scan_mode)

        pair_until = int(self._pair_until) if getattr(self, "_pair_until", 0) > time.time() else None
        discoverable = is_disc or (pair_until is not None)

        devices = []
        devices_map = {}

        conf_text = max_read_bt_conf(self.adb)
        if conf_text:
            cur_sec = None
            for line in conf_text.splitlines():
                line = line.strip()
                m_sec = re.match(r"^\[([0-9a-f:]{17})\]", line, re.I)
                if m_sec:
                    cur_sec = m_sec.group(1).upper()
                    devices_map[cur_sec] = {"mac": cur_sec, "name": cur_sec, "dev_class": 0}
                elif cur_sec and "=" in line:
                    k, _, v = line.partition("=")
                    k, v = k.strip().lower(), v.strip()
                    if k == "name" and v:
                        devices_map[cur_sec]["name"] = v
                    elif k == "devclass":
                        try:
                            devices_map[cur_sec]["dev_class"] = int(v)
                        except ValueError:
                            pass
                elif line.startswith("["):
                    cur_sec = None

        m_bonded = re.search(r"Bonded devices:\s*[\r\n]+((?:[ \t]+[0-9A-Fa-f:]{17}[^\r\n]*[\r\n]*)+)", dumpsys_out)
        if m_bonded:
            for bline in m_bonded.group(1).splitlines():
                bline = bline.strip()
                m_d = re.match(r"([0-9A-Fa-f:]{17})\s*(?:\[[^\]]*\])?\s*(.*)", bline)
                if m_d:
                    bmac = m_d.group(1).upper()
                    bname = m_d.group(2).strip()
                    if bmac not in devices_map:
                        devices_map[bmac] = {"mac": bmac, "name": bname or bmac, "dev_class": 0}
                    elif bname and devices_map[bmac]["name"] == bmac:
                        devices_map[bmac]["name"] = bname

        bt_media = max_bt_info(self.adb)
        for mac, info in devices_map.items():
            dev_class = info.get("dev_class", 0)
            major = (dev_class & 0x1F00) >> 8
            if major == 0x04:
                icon = "audio-headphones"
            elif major == 0x02:
                icon = "phone"
            elif major == 0x01:
                icon = "computer"
            else:
                icon = "phone"

            conn = bool(re.search(rf"mCurrentDevice:\s*{mac}", dumpsys_out, re.I)) or                    bool(re.search(rf"A2dpSinkStateMachine.*Connected.*{mac}", dumpsys_out, re.I)) or                    bool(re.search(rf"Connected.*{mac}", dumpsys_out, re.I))
            if not conn and bt_media and bt_media.get("title") and (info["name"].lower() in bt_media["title"].lower() or bt_media["title"].lower() in info["name"].lower()):
                conn = True

            devices.append({
                "mac": mac,
                "name": info["name"],
                "icon": icon,
                "paired": True,
                "trusted": True,
                "connected": conn,
                "battery": None,
            })
        devices.sort(key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))

        return {
            "powered": powered,
            "keep_off": keep_off,
            "discoverable": discoverable,
            "pairable": powered,
            "alias": alias,
            "pair_until": pair_until,
            "devices": devices,
            "out": {"on": False, "supported": False},
        }

    P.bt_state = max_bt_state

    def max_bt_action(self, data):
        AdbError = getattr(s, "AdbError", Exception)
        bt_off_path = getattr(s, "BT_OFF", "/data/quasar/webpanel/bt_off")
        act = data.get("action")
        mac = str(data.get("mac") or "").upper()

        if act == "power_off":
            max_set_bt_off(self.adb, bt_off_path, True)
            max_android_cmd(self.adb, ["svc", "bluetooth", "disable"])
            self._pair_until = 0
            if getattr(self, "glagol", None) and getattr(self.glagol, "ws", None):
                try:
                    self.glagol.send({"command": "sendText", "text": "выключи блютуз"})
                except Exception:
                    pass
        elif act == "power_on":
            max_set_bt_off(self.adb, bt_off_path, False)
            max_android_cmd(self.adb, ["svc", "bluetooth", "enable"])
            time.sleep(0.5)
        elif act == "pair_on":
            max_set_bt_off(self.adb, bt_off_path, False)
            max_android_cmd(self.adb, ["svc", "bluetooth", "enable"])
            time.sleep(0.5)
            self._pair_until = time.time() + 180
            if getattr(self, "glagol", None) and getattr(self.glagol, "ws", None):
                try:
                    self.glagol.send({"command": "sendText", "text": "включи блютуз"})
                except Exception:
                    pass
        elif act == "pair_off":
            self._pair_until = 0
            if getattr(self, "glagol", None) and getattr(self.glagol, "ws", None):
                try:
                    self.glagol.send({"command": "sendText", "text": "выключи блютуз"})
                except Exception:
                    pass
        elif act == "disconnect":
            if getattr(self, "glagol", None) and getattr(self.glagol, "ws", None):
                try:
                    self.glagol.send({"command": "sendText", "text": "отключи блютуз"})
                except Exception:
                    pass
        elif act == "remove":
            if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                raise AdbError("неверный адрес устройства")
            conf_text = max_read_bt_conf(self.adb)
            if conf_text:
                try:
                    new_conf = max_remove_device_from_conf(conf_text, mac)
                    max_write_bt_conf(self.adb, new_conf)
                    max_android_cmd(self.adb, ["svc", "bluetooth", "disable"])
                    time.sleep(0.5)
                    max_android_cmd(self.adb, ["svc", "bluetooth", "enable"])
                except Exception as e:
                    raise AdbError(f"Не удалось забыть устройство: {e}")
        elif act == "connect":
            if getattr(self, "glagol", None) and getattr(self.glagol, "ws", None):
                try:
                    self.glagol.send({"command": "sendText", "text": "включи блютуз"})
                except Exception:
                    pass
        else:
            raise AdbError("неизвестное действие")
        return self.bt_state()

    P.bt_action = max_bt_action

    def max_boot_hook_script():
        """Единственный генератор boot_hook.sh для Станции Макс.

        Никаких Buildroot-механизмов и maxctl/YAM: только bind-ы панели,
        демон слов активации и Frida-супервизор.
        """
        return (
            "#!/bin/sh\n"
            "# Webpanel boot hook for Station Max Android TV 9\n"
            f"[ -f {s.BOOT_ALWAYS} ] && rm -f {s.NO_BOOT_SOUND}\n"
            'bind() { [ -f "$1" ] && [ -f "$2" ] && ! grep -q " $2 " /proc/mounts && mount --bind "$1" "$2"; }\n'
            f"bind {s.BOOT_CUSTOM} {s.BOOT_TARGET}\n"
            f"bind {s.SOUND_CUSTOM} {s.SOUND_TARGET}\n"
            # Не подключаем hosts панели поверх внешнего менеджера OTA или
            # готового внешнего hosts, который уже блокирует аналитику (/odm).
            f"[ ! -f /odm/bin/adbguard.sh ] && [ ! -f /odm/etc/hosts-ota ] && "
            f"! grep -q appmetrica.yandex.net /odm/etc/hosts 2>/dev/null && "
            f"bind {s.WEB_DIR}/hosts {s.HOSTS_TARGET}\n"
            f"[ -d {s.OVR_DIR} ] && (cd {s.OVR_DIR} && find . -type f) | while read -r f; do "
            f"bind \"{s.OVR_DIR}/${{f#./}}\" \"{s.PATTERNS['clock']}/${{f#./}}\"; done\n"
            f"[ -d {s.OVR_DIR}/sounds ] && (cd {s.OVR_DIR}/sounds && find . -type f) | while read -r f; do "
            f"bind \"{s.OVR_DIR}/sounds/${{f#./}}\" \"{s.SOUNDS_DIR}/${{f#./}}\"; done\n"
            f"[ -f {s.WEB_DIR}/bt_off ] && ( svc bluetooth disable ) >/dev/null 2>&1 &\n"
            f"[ -f {s.ASSIST_CONF} ] && [ -x {s.ASSISTD} ] && [ ! -f {s.ASSIST_NO_AUTOSTART} ] && {{ "
            f"if [ ! -f {s.ASSIST_SD} ] || ! kill -0 \"$(cat {s.ASSIST_SD} 2>/dev/null)\" 2>/dev/null; then "
            # PID публикует только Python-демон после получения flock.
            # start-stop-daemon с -m перезаписывал бы PID при гонке с автозапуском Yapanel.
            f"if [ -x /odm/bin/start-stop-daemon ]; then "
            f"/odm/bin/start-stop-daemon -S -b -x /system/bin/sh -- -c {shlex.quote(s.ASSIST_RUN)}; "
            f"else ( /system/bin/sh -c {shlex.quote(s.ASSIST_RUN)} </dev/null >/dev/null 2>&1 & ); "
            f"fi; "
            f"fi; }}\n"
            + s.wakeword_boot_lines(android=True)
            + s.frida_boot_lines(android=True)
            + "exit 0\n"
        )

    def max_hook_status(self):
        """Native hook status for Android TV 9 / Station Max.

        Persistence is handled natively by /odm/bin/adbd_watchdog.sh which runs
        /data/quasar/webpanel/boot_hook.sh. Freshness is a real md5 check, not a
        constant: otherwise the panel cannot notice that the generated hook was
        replaced by an older version.
        """
        expected = hashlib.md5(max_boot_hook_script().encode("utf-8")).hexdigest()
        out, _, _ = self.adb.shell(
            f"{self._hook_users_script()}; [ -f {s.BOOT_HOOK} ] && echo hook; "
            f"md5sum {s.BOOT_HOOK} 2>/dev/null; true", check=False)
        out = s.strip_ansi(out)
        flags = set(out.split())
        users = [l.strip()[1:] for l in out.splitlines() if l.strip().startswith("@")]
        hook = "hook" in flags
        fresh = expected in out
        return {
            "hook": hook,
            "fresh": hook and fresh,
            "users": users,
            "possible": True,
            "supported": True,
            "reason": "",
            "broken": bool(users) and not (hook and fresh),
            "rootkeeper": "/odm/bin/adbd_watchdog.sh",
            "file": "/data/quasar/webpanel/boot_hook.sh",
        }

    def max_ensure_hook(self, remount=False):
        """Write the Station Max boot_hook.sh (Android TV 9, no Buildroot parts)."""
        hook_script = max_boot_hook_script()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "hook"
            with open(f, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(hook_script)
            self.adb.shell(f"mkdir -p {s.WEB_DIR}")
            self.adb.push(f, s.BOOT_HOOK)
        self.adb.shell(f"chmod 755 {s.BOOT_HOOK}; true")

    def max_hook_repair(self, remount=False):
        self._ensure_hook(remount=False)
        return self.hook_status()

    def max_boot_route(self):
        """Station Max uses Android boot watchdog, strictly no Buildroot rootkeeper/init.d."""
        return "watchdog"

    def max_hook_users_script(self):
        """Android TV 9 hook users (strictly no Buildroot daemons)."""
        users = [
            ("заставка", s.BOOT_CUSTOM),
            ("звук запуска", s.SOUND_CUSTOM),
            ("блокировка аналитики", s.HOSTS_CUSTOM),
            ("выключенный bluetooth", s.BT_OFF),
            ("агент", s.AGENT_TOKENS),
            ("ассистент", s.ASSIST_CONF),
            ("слова активации", s.WAKE_AUTOSTART),
            ("активация Алисы (Frida)", s.WAKE_FRIDA_AUTOSTART),
        ]
        cmd = "; ".join(f"[ -f {path} ] && echo {shlex.quote('@' + name)}" for name, path in users)
        cmd += f"; find {s.OVR_DIR}/sounds -type f 2>/dev/null | grep -q . && echo '@свои звуки'"
        cmd += f"; find {s.OVR_DIR}/ledpatterns -type f 2>/dev/null | grep -q . && echo '@свои анимации'"
        return cmd

    def max_drop_hook_if_unused(self):
        """Android TV 9 hook cleanup without Buildroot remount / rootkeeper manipulation."""
        self.adb.shell(
            f"if [ -z \"$({self._hook_users_script()})\" ]; then "
            f"rm -f {s.BOOT_HOOK} /data/quasar/rootkeeper.sh*; fi; true"
        )

    # ---------- слова активации на Максе: /data/wakeword + Frida ----------
    def max_wakeword_installed(self, force=False):
        """Файлы демона в /data/wakeword, пин sha256 и признаки процесса."""
        now = time.time()
        if not force and getattr(self, "_ww_info", None) and now - getattr(self, "_ww_info_at", 0.0) < 15:
            return dict(self._ww_info)
        out, _, _ = self.adb.shell(
            f"[ -f {s.WAKE_BIN} ] && echo @bin; "
            f"[ -x {s.WAKE_BIN} ] && echo @sha:$(sha256sum {s.WAKE_BIN} 2>/dev/null | cut -d' ' -f1); "
            f"find {s.WAKE_DATA}/words -name model.json -type f 2>/dev/null | grep -q . && echo @model; "
            f"[ -f {s.WAKE_NEGATIVES} ] && echo @neg; "
            f"[ -f {s.WAKE_JAR} ] && echo @jar; "
            f"[ -x {s.WAKE_FRIDA_INJECT} ] && echo @inject; "
            f"[ -f {s.WAKE_FRIDA_JS} ] && echo @fjs; "
            f"[ -f {s.WAKE_FRIDA_SH} ] && echo @fsh; "
            f"[ -f {s.WAKE_PID} ] && kill -0 $(cat {s.WAKE_PID}) 2>/dev/null && echo @pid; "
            f"pidof wakeword >/dev/null 2>&1 && echo @proc; "
            f"[ -f {s.WAKE_AUTOSTART} ] && echo @auto; "
            f"cat {s.WAKE_DATA}/VERSION 2>/dev/null; true", check=False)
        lines = [l.strip() for l in s.strip_ansi(out).splitlines() if l.strip()]
        version = next((l for l in lines if not l.startswith("@")), None)
        sha = next((l[5:] for l in lines if l.startswith("@sha:")), "")
        pin = str(getattr(s, "WAKE_BIN_SHA256", "") or "")
        bin_ok = bool(sha) and (sha == pin if pin else True)
        files_ok = bool(bin_ok and "@neg" in lines and "@jar" in lines and "@inject" in lines
                        and "@fjs" in lines and "@fsh" in lines)
        info = {"installed": bool("@bin" in lines or "@model" in lines),
                "version": version, "bin": "@bin" in lines, "bin_ok": bin_ok,
                "sha256": sha, "model": "@model" in lines,
                "negatives": "@neg" in lines, "jar": "@jar" in lines,
                "inject": "@inject" in lines, "frida_js": "@fjs" in lines,
                "frida_sh": "@fsh" in lines, "files_ok": files_ok,
                "pid": "@pid" in lines, "process": "@proc" in lines, "auto": "@auto" in lines}
        self._ww_info, self._ww_info_at = dict(info), now
        return info

    def max__wakeword_frida_probe(self, force=False):
        """Состояние Frida-супервизора и heartbeat агента (кэш 5 с)."""
        now = time.time()
        if not force and getattr(self, "_ww_frida", None) and now - getattr(self, "_ww_frida_at", 0.0) < 5:
            return dict(self._ww_frida)
        out, _, _ = self.adb.shell(
            f"[ -f {s.WAKE_FRIDA_PID} ] && kill -0 $(cat {s.WAKE_FRIDA_PID} 2>/dev/null) 2>/dev/null && echo @run; "
            f"[ -f {s.WAKE_FRIDA_JS} ] && echo @js; "
            f"[ -f {s.WAKE_FRIDA_SH} ] && echo @sh; "
            f"[ -x {s.WAKE_FRIDA_INJECT} ] && echo @inject; "
            f"pidof frida-inject >/dev/null 2>&1 && echo @injector; "
            f"a=$(stat -c %Y {s.WAKE_HEARTBEAT} 2>/dev/null || echo 0); n=$(date +%s); echo @age:$((n-a)); true",
            check=False)
        lines = [l.strip() for l in s.strip_ansi(out).splitlines() if l.strip()]
        age = next((int(l[5:]) for l in lines if l.startswith("@age:") and l[5:].isdigit()), None)
        info = {"supported": True, "running": "@run" in lines,
                "heartbeat_age": age, "heartbeat": age is not None and age < 20,
                "js": "@js" in lines, "sh": "@sh" in lines,
                "inject": "@inject" in lines, "injector": "@injector" in lines,
                "pid": "@run" in lines}
        self._ww_frida, self._ww_frida_at = dict(info), now
        return info

    def max_wakeword_frida_status(self):
        return self._wakeword_frida_probe()

    def max_wakeword_push_assets(self):
        """Кладёт Frida-скрипты из device/wakeword/frida в /data/wakeword/frida."""
        src = Path(__file__).resolve().parent / "device" / "wakeword" / "frida"
        self.adb.shell(f"mkdir -p {s.WAKE_FRIDA_DIR}", check=False)
        pushed = False
        if src.is_dir():
            for name in ("alice_trigger.js", "alice-frida-supervisor.sh"):
                f = src / name
                if f.is_file():
                    self.adb.push(f, f"{s.WAKE_FRIDA_DIR}/{name}")
                    pushed = True
        self.adb.shell(
            f"chmod 755 {s.WAKE_FRIDA_SH} 2>/dev/null; chmod 644 {s.WAKE_FRIDA_JS} 2>/dev/null; true",
            check=False)
        return pushed

    def max_wakeword_push_bulk_assets(self):
        """Кладёт крупные файлы (бинарник, negatives, jar, frida-inject), если их нет."""
        base = Path(__file__).resolve().parent / "device" / "wakeword"
        self.adb.shell(
            f"mkdir -p {s.WAKE_DATA} {posixpath.dirname(s.WAKE_NEGATIVES)} "
            f"{posixpath.dirname(s.WAKE_JAR)} {posixpath.dirname(s.WAKE_FRIDA_INJECT)}",
            check=False)
        info = self._wakeword_installed(force=True)
        pushed = False
        if not info.get("bin_ok"):
            local = base / "build" / "wakeword"
            if local.is_file():
                self.adb.shell(f"rm -f {s.WAKE_BIN}.new", check=False)
                self.adb.push(local, f"{s.WAKE_BIN}.new", timeout=300)
                self.adb.shell(f"chmod 755 {s.WAKE_BIN}.new; mv -f {s.WAKE_BIN}.new {s.WAKE_BIN}",
                               check=False)
                pushed = True
            elif not info.get("bin"):
                raise AdbError(f"нет {s.WAKE_BIN} и локальной копии device/wakeword/build/wakeword")
            else:
                raise AdbError("бинарник не совпадает с проверенным, а локальной копии нет")
        if not info.get("negatives"):
            local = base / "negatives.bin"
            if local.is_file():
                self.adb.shell(f"rm -f {s.WAKE_NEGATIVES}.new", check=False)
                self.adb.push(local, f"{s.WAKE_NEGATIVES}.new", timeout=300)
                self.adb.shell(f"mv -f {s.WAKE_NEGATIVES}.new {s.WAKE_NEGATIVES}", check=False)
                pushed = True
        if not info.get("jar"):
            local = base / "lib" / "audiocapture.jar"
            if local.is_file():
                self.adb.shell(f"rm -f {s.WAKE_JAR}.new", check=False)
                self.adb.push(local, f"{s.WAKE_JAR}.new", timeout=120)
                self.adb.shell(f"mv -f {s.WAKE_JAR}.new {s.WAKE_JAR}", check=False)
                pushed = True
        if not info.get("inject"):
            local = base / "bin" / "frida-inject"
            if local.is_file():
                self.adb.shell(f"rm -f {s.WAKE_FRIDA_INJECT}.new", check=False)
                self.adb.push(local, f"{s.WAKE_FRIDA_INJECT}.new", timeout=300)
                self.adb.shell(
                    f"chmod 755 {s.WAKE_FRIDA_INJECT}.new; mv -f {s.WAKE_FRIDA_INJECT}.new {s.WAKE_FRIDA_INJECT}",
                    check=False)
                pushed = True
        self._ww_info = None
        return pushed

    def max_wakeword_frida_start(self):
        self.max_wakeword_push_assets()
        if self._wakeword_frida_probe(force=True)["running"]:
            return self._wakeword_frida_probe(force=True)
        self.adb.shell(
            f"rm -f {s.WAKE_FRIDA_PID}; "
            f"if [ -x /odm/bin/start-stop-daemon ]; then "
            f"/odm/bin/start-stop-daemon -S -b -m -p {s.WAKE_FRIDA_PID} -x /bin/sh -- {s.WAKE_FRIDA_SH}; "
            f"else nohup /bin/sh {s.WAKE_FRIDA_SH} >/dev/null 2>&1 & echo $! > {s.WAKE_FRIDA_PID}; fi; true",
            check=False, timeout=20)
        for _ in range(10):
            time.sleep(0.3)
            st = self._wakeword_frida_probe(force=True)
            if st["running"]:
                return st
        return self._wakeword_frida_probe(force=True)

    def max_wakeword_frida_stop(self):
        self.adb.shell(
            f"p=$(cat {s.WAKE_FRIDA_PID} 2>/dev/null); [ -n \"$p\" ] && kill $p 2>/dev/null; "
            f"rm -f {s.WAKE_FRIDA_PID}; p=$(pidof frida-inject); [ -n \"$p\" ] && kill $p 2>/dev/null; true",
            check=False, timeout=20)
        self._ww_frida = None
        return self._wakeword_frida_probe(force=True)

    def max_wakeword_start(self):
        info = self._wakeword_installed(force=True)
        if not info["installed"]:
            raise AdbError(f"нет демона {s.WAKE_BIN} и обученной модели в {s.WAKE_DATA}")
        if not info["bin"]:
            raise AdbError(f"нет бинарника {s.WAKE_BIN}: нажмите «Установить/починить файлы»")
        pin = str(getattr(s, "WAKE_BIN_SHA256", "") or "")
        if pin and not info.get("bin_ok"):
            raise AdbError("бинарник слов активации не совпадает с проверенным build: "
                           f"{info.get('sha256') or '?'} != {pin}")
        try:
            self.wakeword_frida_start()
        except (AdbError, OSError, subprocess.TimeoutExpired) as e:
            s.JOURNAL.add(f"слова активации: Frida-агент не запустился: {e}", "warn")
        run = s.wakeword_run_cmd(android=True)
        q = shlex.quote(run)
        self.adb.shell(
            f"p=$(cat {s.WAKE_PID} 2>/dev/null); [ -n \"$p\" ] && kill $p 2>/dev/null; rm -f {s.WAKE_PID}; "
            f"p=$(pidof wakeword); if [ -n \"$p\" ]; then kill $p 2>/dev/null; i=0; "
            f"while kill -0 $p 2>/dev/null && [ $i -lt 20 ]; do sleep 0.25; i=$((i+1)); done; "
            f"kill -9 $p 2>/dev/null; fi; true", check=False, timeout=20)
        out, err, rc = self.adb.shell(
            f"if [ -x /odm/bin/start-stop-daemon ]; then "
            f"/odm/bin/start-stop-daemon -S -b -m -N 10 -p {s.WAKE_PID} -x /bin/sh -- -c {q}; "
            f"else nohup /bin/sh -c {q} >/dev/null 2>&1 & echo $! > {s.WAKE_PID}; fi; true",
            check=False, timeout=30)
        if rc != 0:
            raise AdbError(s.strip_ansi(out or err).strip()[-400:] or "не удалось запустить демон слов активации")
        self._ww_info = None
        self._ww_port = None
        for _ in range(25):
            time.sleep(0.3)
            st = self.wakeword_status()
            if st["running"] and not st.get("error"):
                return st
        st = self.wakeword_status()
        if st["running"]:
            return st
        raise AdbError("демон слов активации не запустился")

    def max_wakeword_stop(self):
        self.adb.shell(
            f"p=$(cat {s.WAKE_PID} 2>/dev/null); [ -n \"$p\" ] && kill $p 2>/dev/null; rm -f {s.WAKE_PID}; "
            f"p=$(pidof wakeword); [ -n \"$p\" ] && kill $p 2>/dev/null; "
            f"p=$(cat {s.WAKE_FRIDA_PID} 2>/dev/null); [ -n \"$p\" ] && kill $p 2>/dev/null; rm -f {s.WAKE_FRIDA_PID}; "
            f"p=$(pidof frida-inject); [ -n \"$p\" ] && kill $p 2>/dev/null; true",
            check=False, timeout=20)
        self._ww_port = None
        self._ww_info = None
        self._ww_frida = None
        return self.wakeword_status()

    def max_wakeword_autostart(self, on):
        if on and not self._wakeword_installed(force=True)["installed"]:
            raise AdbError("демон слов активации не установлен")
        if on:
            self.adb.shell(
                f"mkdir -p {s.WEB_DIR} && touch {s.WAKE_AUTOSTART} {s.WAKE_FRIDA_AUTOSTART}",
                check=False)
            self._ensure_hook()
            try:
                self.wakeword_frida_start()
            except (AdbError, OSError, subprocess.TimeoutExpired) as e:
                s.JOURNAL.add(f"слова активации: Frida-агент не запустился: {e}", "warn")
        else:
            self.adb.shell(f"rm -f {s.WAKE_AUTOSTART} {s.WAKE_FRIDA_AUTOSTART}", check=False)
            self._drop_hook_if_unused()
        self._ww_info = None
        return self.wakeword_status()

    def max_wakeword_install(self):
        """Проверить/починить файлы в /data/wakeword без YAM-пакета."""
        self.max_wakeword_push_assets()
        self.max_wakeword_push_bulk_assets()
        self.adb.shell(f"[ -f {s.WAKE_DATA}/VERSION ] || echo 0.3.0-maxpkg > {s.WAKE_DATA}/VERSION; true",
                       check=False)
        self._ww_info = None
        self._ww_port = None
        return self.wakeword_status()

    def max__wakeword_restart(self):
        self.wakeword_stop()
        return self.wakeword_start()

    def max__wakeword_apply_dry_run(self):
        """На Максе нативный триггер Алисы всегда включён: dry_run недопустим."""
        st = self._wakeword_settings()
        if bool(st.get("dry_run")):
            st["dry_run"] = False
            self._wakeword_settings_write(st)
            self.wakeword_stop()
            self.wakeword_start()
        return False

    def max_wakeword_actions_set(self, word, action, scene=None):
        word = str(word or "").strip()
        if not word:
            raise AdbError("пустое слово")
        if str(action or "alice") != "alice":
            raise AdbError("На Станции Макс слово активации всегда будит Алису")

        def mutate(data):
            data.setdefault(s.WAKE_ACTIONS_KEY, {}).pop(word, None)
            return data

        s.update_settings(mutate)
        self.max__wakeword_apply_dry_run()
        return self.wakeword_actions()

    original_wakeword_status = P.wakeword_status

    def max_wakeword_status(self):
        st = original_wakeword_status(self)
        info = self._wakeword_installed()
        st["dry_run"] = False
        st["bin_ok"] = bool(info.get("bin_ok"))
        st["bin_sha256"] = info.get("sha256") or ""
        st["files_ok"] = bool(info.get("files_ok"))
        st["negatives_ok"] = bool(info.get("negatives"))
        st["jar_ok"] = bool(info.get("jar"))
        st["inject_ok"] = bool(info.get("inject"))
        st["frida"] = self._wakeword_frida_probe()
        return st

    P.hook_status = max_hook_status
    P._ensure_hook = max_ensure_hook
    P.hook_repair = max_hook_repair
    P._boot_route = max_boot_route
    P._hook_users_script = max_hook_users_script
    P._drop_hook_if_unused = max_drop_hook_if_unused
    P._wakeword_installed = max_wakeword_installed
    P.max_wakeword_push_assets = max_wakeword_push_assets
    P.max_wakeword_push_bulk_assets = max_wakeword_push_bulk_assets
    P._wakeword_frida_probe = max__wakeword_frida_probe
    P.wakeword_frida_status = max_wakeword_frida_status
    P.wakeword_frida_start = max_wakeword_frida_start
    P.wakeword_frida_stop = max_wakeword_frida_stop
    P.wakeword_start = max_wakeword_start
    P.wakeword_stop = max_wakeword_stop
    P.wakeword_autostart = max_wakeword_autostart
    P.wakeword_install = max_wakeword_install
    P._wakeword_restart = max__wakeword_restart
    P._wakeword_apply_dry_run = max__wakeword_apply_dry_run
    P.wakeword_actions_set = max_wakeword_actions_set
    P.wakeword_status = max_wakeword_status

    def max_anim_paths(self, kind, name):
        if kind not in s.PATTERNS or name not in self.patterns().get(kind, []) or name.startswith("my/"):
            raise AdbError("нет такой анимации")
        target = f"{s.PATTERNS[kind]}/{name}"
        custom = f"{s.OVR_DIR}/ledpatterns/{kind}/{name}"
        return target, custom

    P._anim_paths = max_anim_paths

    def max_boot_status(self):
        hook_info = self.hook_status()
        hook_ok = bool(hook_info.get("hook"))

        target = s.BOOT_TARGET
        custom = s.BOOT_CUSTOM
        orig = s.BOOT_ORIG
        s_target = s.SOUND_TARGET
        s_custom = s.SOUND_CUSTOM

        out, _, _ = self.adb.shell(
            f"[ -f {target} ] && echo target; [ -f {custom} ] && echo custom; [ -f {orig} ] && echo orig; "
            f"grep -q ' {target} ' /proc/mounts && echo mounted; "
            f"[ -f {s_target} ] && echo s_target; [ -f {s_custom} ] && echo s_custom; "
            f"grep -q ' {s_target} ' /proc/mounts && echo s_mounted; "
            f"[ -f {s.NO_BOOT_SOUND} ] && echo nosound; [ -f {s.BOOT_ALWAYS} ] && echo always; true",
            check=False
        )
        flags = set(out.split())
        res = {
            "supported": "target" in flags or os.path.exists(target),
            "custom": "custom" in flags or os.path.exists(custom),
            "mounted": "mounted" in flags,
            "sound_supported": "s_target" in flags or os.path.exists(s_target),
            "sound_custom": "s_custom" in flags or os.path.exists(s_custom),
            "sound_mounted": "s_mounted" in flags,
            "hook": hook_ok,
            "no_boot_sound": "nosound" in flags or os.path.exists(s.NO_BOOT_SOUND),
            "always": "always" in flags or os.path.exists(s.BOOT_ALWAYS)
        }
        if res["supported"]:
            try:
                if not res["mounted"]:
                    target_bytes = self.adb.read_bytes(target) if not os.path.exists(target) else open(target, "rb").read()
                    res["default"] = s.decode_clock_pattern(".binscled.zst", target_bytes)
                elif "orig" in flags or os.path.exists(orig):
                    orig_bytes = self.adb.read_bytes(orig) if not os.path.exists(orig) else open(orig, "rb").read()
                    res["default"] = s.decode_clock_pattern(".binscled.zst", orig_bytes)
                else:
                    res["default"] = []
            except Exception:
                res["default"] = []

            if res["custom"]:
                try:
                    custom_bytes = self.adb.read_bytes(custom) if not os.path.exists(custom) else open(custom, "rb").read()
                    res["frames"] = s.decode_clock_pattern(".binscled.zst", custom_bytes)
                except Exception:
                    res["frames"] = []
        return res

    P.boot_status = max_boot_status

    def max_footprint(self):
        """Footprint on Station Max (Android TV 9 / AOSP)."""
        target = s.BOOT_TARGET
        custom = s.BOOT_CUSTOM
        orig = s.BOOT_ORIG
        s_target = s.SOUND_TARGET
        s_custom = s.SOUND_CUSTOM
        s_orig = s.SOUND_ORIG

        cmd = "; ".join([
            f"grep -q ' {target} ' /proc/mounts && echo 1 || echo 0",
            f"[ -e {custom} ] && echo 1 || echo 0",
            f"grep -q ' {s_target} ' /proc/mounts && echo 1 || echo 0",
            f"[ -e {s_custom} ] && echo 1 || echo 0",
            f"[ -e {s.BOOT_ALWAYS} ] && echo 1 || echo 0",
            f"[ -e {orig} ] && echo 1 || echo 0",
            f"[ -e {s_orig} ] && echo 1 || echo 0",
            f"[ -d {s.WEB_DIR} ] && du -sk {s.WEB_DIR} | cut -f1 || echo 0",
            f"grep -q ' /system/etc/hosts ' /proc/mounts || [ -f {s.WEB_DIR}/hosts ] && echo 1 || echo 0",
            f"find {s.OVR_DIR}/sounds -type f 2>/dev/null | wc -l",
            f"[ -d {s.OVR_DIR}/sounds ] && du -sk {s.OVR_DIR}/sounds | cut -f1 || echo 0",
            f"find {s.OVR_DIR}/ledpatterns -type f 2>/dev/null | wc -l",
            f"find {s.MY_ANIM_DIR} -type f 2>/dev/null | wc -l",
            f"[ -e {s.BT_OFF} ] && echo 1 || echo 0",
            f"[ -d {s.VIZ_DIR} ] || pgrep -x viz >/dev/null && echo 1 || echo 0",
            f"[ -d {s.VIZ_DIR} ] && du -sk {s.VIZ_DIR} | cut -f1 || echo 0",
            f"[ -x {s.BOOT_HOOK} ] && grep -q 'boot_hook.sh' {s.ROOTKEEPER_CALLER} 2>/dev/null && echo 1 || echo 0",
            f"[ -e {s.AGENT_BIN} ] && echo 1 || echo 0",
            f"{{ [ -f {s.WAKE_AUTOSTART} ] || pidof wakeword >/dev/null 2>&1; }} && echo 1 || echo 0",
            f"{{ [ -f {s.WAKE_FRIDA_AUTOSTART} ] || {{ [ -f {s.WAKE_FRIDA_PID} ] && kill -0 $(cat {s.WAKE_FRIDA_PID} 2>/dev/null) 2>/dev/null; }}; }} && echo 1 || echo 0",
            f"[ -d {s.WAKE_DATA} ] && du -sk {s.WAKE_DATA} | cut -f1 || echo 0",
        ])
        v = self.adb.shell(cmd, check=False)[0].split()
        v = (v + ["0"] * 21)[:21]
        b = [x == "1" for x in v]
        n = lambda x: int(x) if x.isdigit() else 0

        boot_on = b[0] or b[1]
        sound_on = b[2] or b[3]
        backup_on = b[5] or b[6]
        hook_on = b[16]
        privacy_state = self.privacy_status(migrate=False)
        privacy_detail = {
            "panel": "подмена /system/etc/hosts",
            "adbguard-v2": "adbguard v2, OTA отдельно",
            "conflict": "внешний hosts, панель не меняет",
        }.get(privacy_state.get("backend"), "управление hosts")

        items = [
            {"key": "boot", "name": "Своя заставка часов", "on": boot_on},
            {"key": "sound", "name": "Свой звук запуска", "on": sound_on},
            {"key": "sounds", "name": "Свои системные звуки", "on": n(v[9]) > 0, "detail": f"заменено: {n(v[9])}", "size_kb": n(v[10])},
            {"key": "anims", "name": "Свои анимации кольца и матрицы", "on": n(v[11]) + n(v[12]) > 0,
             "detail": f"заменено встроенных: {n(v[11])}, своих: {n(v[12])}"},
            {"key": "always", "name": "Заставка при каждой загрузке", "on": b[4]},
            {"key": "btoff", "name": "Bluetooth выключен", "on": b[13], "detail": "и после перезагрузки"},
            {"key": "privacy", "name": "Блокировка аналитики",
             "on": privacy_state.get("enabled", False),
             "detail": privacy_detail},
            {"key": "viz", "name": "Живой спектр с колонки", "on": b[14], "size_kb": n(v[15]),
             "detail": "захват звука усилителя (ALSA/Android)"},
            {"key": "agent", "name": "Агент панели (защищённое соединение)", "on": b[17],
             "detail": f"порт {s.agentlink.PORT}, вход по токену"},
            {"key": "wakeword", "name": "Свои слова активации", "on": b[18], "size_kb": n(v[20]),
             "detail": "демон и обученная модель в /data/wakeword"},
            {"key": "ww_frida", "name": "Активация Алисы (Frida)", "on": b[19],
             "detail": "startConversation в com.yandex.io.sdk"},
            {"key": "backup", "name": "Копии оригиналов", "on": backup_on,
             "detail": "заставка, звук запуска — на случай отката"},
            {"key": "hook", "name": "Автозапуск: boot_hook.sh (adbd_watchdog)", "on": hook_on,
             "detail": "запуск хука при загрузке системы"},
        ]
        return {
            "items": items,
            "dir": s.WEB_DIR,
            "size_kb": n(v[7]),
            "any": any(i["on"] for i in items),
            "yam_installed": False
        }

    def max_footprint_reset(self, dry_run=False):
        """Reset Station Max changes safely without touching non-existent Buildroot services.

        Only explicitly known panel-owned paths are removed.  With dry_run=True the
        device is not modified at all: the list is only logged and returned footprint
        is read normally.
        """
        web = str(getattr(s, "WEB_DIR", "/data/quasar/webpanel")).rstrip("/")
        owned = []

        def _add_owned(path):
            if not path:
                return
            path = str(path)
            if path not in owned:
                owned.append(path)

        for attr in ("BOOT_CUSTOM", "SOUND_CUSTOM", "BOOT_ALWAYS", "BOOT_HOOK", "BT_OFF", "HOSTS_CUSTOM",
                     "ASSIST_NO_AUTOSTART", "WAKE_AUTOSTART", "WAKE_FRIDA_AUTOSTART"):
            _add_owned(getattr(s, attr, None))
        created = getattr(s, "ROOTKEEPER_CREATED", "")
        if created and posixpath.normpath(str(created)).startswith(web + "/"):
            _add_owned(created)
        for name in ("override", "patterns", "viz", "bt_off", "hosts"):
            _add_owned(posixpath.join(web, name))

        _log.info("Max footprint reset%s: panel-owned paths: %s",
                  " (dry-run)" if dry_run else "", ", ".join(owned))
        journal = getattr(s, "JOURNAL", None)
        if journal is not None and hasattr(journal, "add"):
            try:
                journal.add(f"Сброс Max{'(dry-run)' if dry_run else ''}: пути панели: {', '.join(owned)}")
            except Exception:
                pass
        if dry_run:
            return self.footprint()

        self._agent_forget()
        q = shlex.quote
        bt_off = str(getattr(s, "BT_OFF", posixpath.join(web, "bt_off")))
        ovr = str(getattr(s, "OVR_DIR", posixpath.join(web, "override")))
        boot_target = str(getattr(s, "BOOT_TARGET", ""))
        sound_target = str(getattr(s, "SOUND_TARGET", ""))
        boot_orig = str(getattr(s, "BOOT_ORIG", ""))
        sound_orig = str(getattr(s, "SOUND_ORIG", ""))
        patterns = getattr(s, "PATTERNS", {})
        clock_dir = str(patterns.get("clock", "")) if isinstance(patterns, dict) else ""
        quasar = str(getattr(s, "QUASAR", ""))

        rm_owned = " ".join(
            f"if [ -e {q(path)} ]; then rm -rf {q(path)} && printf 'removed %s\\n' {q(path)}; fi;"
            for path in owned
        )
        restore_boot = ""
        if boot_orig and boot_target:
            restore_boot = (f"if [ -f {q(boot_orig)} ]; then mkdir -p {q(posixpath.dirname(boot_target))} && "
                            f"mv -f {q(boot_orig)} {q(boot_target)} 2>/dev/null; fi; ")
        restore_sound = ""
        if sound_orig and sound_target:
            restore_sound = (f"if [ -f {q(sound_orig)} ]; then mkdir -p {q(posixpath.dirname(sound_target))} && "
                             f"mv -f {q(sound_orig)} {q(sound_target)} 2>/dev/null; fi; ")
        unmount_overrides = ""
        if ovr and clock_dir and quasar:
            unmount_overrides = (
                f"[ -d {q(ovr)} ] && (cd {q(ovr)} && find . -type f) | while read -r f; do "
                f"  t={q(clock_dir)}/${{f#./}}; grep -q \" $t \" /proc/mounts && umount \"$t\" 2>/dev/null; "
                f"  t2={q(quasar)}/${{f#./}}; grep -q \" $t2 \" /proc/mounts && umount \"$t2\" 2>/dev/null; "
                f"done; "
            )

        with self.adb.only_adb():
            out, _, _ = _shell_result(self.adb.shell(
                f"[ -f {q(bt_off)} ] && svc bluetooth enable 2>/dev/null; "
                f"p=$(cat {q(s.WAKE_PID)} 2>/dev/null); [ -n \"$p\" ] && kill $p 2>/dev/null; "
                f"p=$(pidof wakeword); [ -n \"$p\" ] && kill $p 2>/dev/null; "
                f"p=$(cat {q(s.WAKE_FRIDA_PID)} 2>/dev/null); [ -n \"$p\" ] && kill $p 2>/dev/null; "
                f"p=$(pidof frida-inject); [ -n \"$p\" ] && kill $p 2>/dev/null; "
                f"rm -f {q(s.WAKE_PID)} {q(s.WAKE_FRIDA_PID)}; "
                f"killall viz 2>/dev/null; killall yapanel-agent 2>/dev/null; "
                f"grep -q ' {boot_target} ' /proc/mounts && umount {q(boot_target)} 2>/dev/null; "
                f"grep -q ' {sound_target} ' /proc/mounts && umount {q(sound_target)} 2>/dev/null; "
                # Снимаем только подтверждённый одиночный mount панели.
                f'if [ -f {q(s.HOSTS_CUSTOM)} ] && '
                f'[ ! -f /odm/bin/adbguard.sh ] && [ ! -f /odm/etc/hosts-ota ] && '
                f'[ "$(stat -c %d:%i {q(s.HOSTS_CUSTOM)} 2>/dev/null)" = '
                f'"$(stat -c %d:%i /system/etc/hosts 2>/dev/null)" ] && '
                f'[ "$(grep -c " /system/etc/hosts " /proc/mounts)" = "1" ]; '
                f'then umount /system/etc/hosts 2>/dev/null; fi; '
                f"{unmount_overrides}"
                f"{rm_owned}"
                f"{restore_boot}{restore_sound}"
                f"true", check=False
            ))
            for line in out.splitlines():
                if line.startswith("removed "):
                    _log.info("Max footprint reset: %s", line.strip())
            self.ring_reset()
            return self.footprint()

    P.footprint = max_footprint
    P._footprint_reset = max_footprint_reset
    max_privacy.install(s, P)

    def _dsp_monitor(self):
        """Следит за сменой трека/потока и возвращает DSP после сброса.

        Регистры SY6045S может переинициализировать аудиотракт при смене
        трека, рестарте Quasar или переподключении BT. Проверка каждую секунду
        дешёвая: тяжёлая работа выполняется только при реальном событии или
        расхождении read-back.
        """
        if getattr(self, "_dsp_monitor_started", False):
            return
        if not hasattr(self, "_max_dsp") or not getattr(self, "glagol", None):
            return
        self._dsp_monitor_started = True

        def loop():
            st = getattr(self.glagol, "state", None) or {}
            p = st.get("playerState") if isinstance(st, dict) else None
            p = p if isinstance(p, dict) else {}
            last_track = str(p.get("id") or p.get("trackId") or "")
            last_playing = bool(st.get("playing") or p.get("playing")) if isinstance(st, dict) else False
            last_ws = bool(getattr(self.glagol, "ws", None))
            last_event = last_verify = 0.0
            while True:
                time.sleep(1)
                dsp = getattr(self, "_max_dsp", None)
                if dsp is None:
                    continue
                try:
                    st = getattr(self.glagol, "state", None) or {}
                    p = st.get("playerState") if isinstance(st, dict) else None
                    p = p if isinstance(p, dict) else {}
                    track = str(p.get("id") or p.get("trackId") or "")
                    playing = bool(st.get("playing") or p.get("playing")) if isinstance(st, dict) else False
                    ws = bool(getattr(self.glagol, "ws", None))
                    now = time.time()
                    event = track != last_track or (playing and not last_playing) or (ws and not last_ws)
                    if event and now - last_event >= 2.0:
                        last_event = now
                        last_verify = now
                        dsp.ensure_applied(force=True)
                    elif now - last_verify >= 30.0:
                        last_verify = now
                        if not getattr(dsp, "_restored", False):
                            dsp.maybe_restore()
                        else:
                            dsp.ensure_applied()
                    last_track, last_playing, last_ws = track, playing, ws
                except Exception as e:  # фоновая проверка не должна ронять панель
                    _log.debug("DSP monitor: %s", e)

        threading.Thread(target=loop, daemon=True, name="dsp-monitor").start()

    P._dsp_monitor = _dsp_monitor

    def max_dsp_status(self):
        if not hasattr(self, "_max_dsp"):
            from dsp_max import MaxDSP
            self._max_dsp = MaxDSP(self)
        self._max_dsp.maybe_restore()
        self._dsp_monitor()
        return self._max_dsp.status()

    def max_dsp_action(self, data):
        if not hasattr(self, "_max_dsp"):
            from dsp_max import MaxDSP
            self._max_dsp = MaxDSP(self)
        self._dsp_monitor()
        return self._max_dsp.action(data)

    P.dsp_status = max_dsp_status
    P.dsp_action = max_dsp_action

    # On Station Max boot/startup, ensure hardware DSP matches the configured profile
    try:
        from dsp_max import MaxDSP
        _init_dsp = MaxDSP(s)
        if _init_dsp.is_supported():
            _init_dsp.restore_hardware_state()
    except Exception:
        pass
