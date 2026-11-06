"""Вход в аккаунт Яндекса и локальное управление колонкой по протоколу Glagol.

Протоколы — по открытой интеграции YandexStation для Home Assistant (github.com/AlexxIT/YandexStation):
вход по QR через passport.yandex.ru (неофициальный API), запасной вход — официальный OAuth-клиент
Яндекс Музыки; с токеном Музыки берём «токен разговора» на quasar.yandex.net и подключаемся к колонке
по WebSocket (wss://<ip>:1961). Только стандартная библиотека.
"""
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import shutil
import socket
import ssl
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
# OAuth-клиенты: мобильный Паспорт (сессия → x-token) и Яндекс Музыка (x-token → токен для Glagol).
try:
    from config import get_config
    _cfg_glagol = get_config().glagol
except ImportError:
    _cfg_glagol = None

if _cfg_glagol:
    PASSPORT_CLIENT = (_cfg_glagol.passport_client_id, _cfg_glagol.passport_client_secret)
    MUSIC_CLIENT = (_cfg_glagol.music_client_id, _cfg_glagol.music_client_secret)
    YASTATION_GLAGOL_PORT = _cfg_glagol.port
else:
    PASSPORT_CLIENT = ("c0ebe342af7d48fbbbfcf2d2eedb8f9e", "ad0a908f0aa341a182a37ecd75bc319e")
    MUSIC_CLIENT = ("23cabbbdc6cd418abb4b39c32c41195d", "53bc75238f0c4d08a118e51fe9203300")
    YASTATION_GLAGOL_PORT = 1961

OAUTH_URL = f"https://oauth.yandex.ru/authorize?response_type=token&client_id={MUSIC_CLIENT[0]}"

# Проверка сертификата колонки включается только явно: у локальных колонок сертификат самоподписанный,
# и обязательная проверка без CA сломала бы им подключение.
GLAGOL_CA_ENV = "PANEL_GLAGOL_CA"
TLS_INSECURE_WARNING = ("Glagol: TLS-сертификат колонки не проверяется; "
                        f"задайте {GLAGOL_CA_ENV} (файл или каталог с CA), чтобы включить проверку")


def _glagol_ca(ca=None):
    """Находит CA для проверки колонки. Возвращает (путь, причина ошибки)."""
    raw = str(ca).strip() if ca is not None else ""
    if not raw:   # явный аргумент важнее окружения, пустой — читаем PANEL_GLAGOL_CA
        raw = str(os.environ.get(GLAGOL_CA_ENV) or "").strip()
    if not raw:
        return None, None
    try:
        path = Path(raw).expanduser()
        if path.is_dir() or path.is_file():
            return path, None
    except Exception as e:
        return None, f"{GLAGOL_CA_ENV}={raw}: {e}"
    return None, f"{GLAGOL_CA_ENV}={raw}: файл или каталог не найден"


def glagol_ssl_context(ca=None):
    """TLS-контекст для WebSocket колонки: (context, verified, warning).

    Если CA задан и доступен — CERT_REQUIRED и загрузка этого CA. Если CA нет,
    остаётся CERT_NONE (локальная колонка с самоподписанным сертификатом работает),
    но в warning/info явно видно, что проверка отключена.
    """
    ca_path, problem = _glagol_ca(ca)
    try:
        ctx = ssl.create_default_context()
    except (OSError, ssl.SSLError) as e:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        problem = problem or f"не удалось создать TLS-контекст: {e}"
    if ca_path is not None:
        try:
            ctx.check_hostname = False   # в сертификате локальной колонки обычно IP, а не имя
            ctx.verify_mode = ssl.CERT_REQUIRED
            if ca_path.is_dir():
                ctx.load_verify_locations(capath=str(ca_path))
            else:
                ctx.load_verify_locations(cafile=str(ca_path))
            return ctx, True, None
        except (OSError, ValueError, ssl.SSLError) as e:
            problem = f"{GLAGOL_CA_ENV}={ca_path}: {e}"
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    warning = TLS_INSECURE_WARNING if not problem else f"{TLS_INSECURE_WARNING} ({problem})"
    return ctx, False, warning


class AuthError(Exception):
    pass


class YandexAuth:
    """Токены хранятся только на компьютере, в файле рядом с сервером (права 600)."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.load_error = None       # почему не удалось прочитать файл (панель при этом работает)
        self.backup_path = None      # копия битого файла, если её удалось сделать
        self.qr = None   # незавершённый вход по QR: opener с куками, заголовки, ответ submit
        self.lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._load()

    # ---- хранение ----
    def _load(self):
        """Читает токены, но никогда не роняет панель из-за битого/нечитаемого файла."""
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("ожидался объект JSON")
        except Exception as e:   # fail-safe: любая ошибка доступа/разбора — просто пустой вход
            self.data = {}
            self.load_error = f"не удалось прочитать {self.path.name}: {e}"
            self.backup_path = self._backup_corrupt()
            if self.backup_path:
                self.load_error += f"; копия: {self.backup_path.name}"
            return
        self.data = data

    def _backup_corrupt(self):
        """Сохраняет копию битого файла рядом (права 600), не мешая следующему входу."""
        backup = self.path.with_name(self.path.name + ".corrupt")
        try:
            shutil.copy2(self.path, backup)
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass
            return backup
        except Exception:
            return None

    @property
    def music_token(self):
        return self.data.get("music_token")

    def info(self):
        return {"logged_in": bool(self.music_token), "login": self.data.get("login"),
                "method": self.data.get("method"), "load_error": self.load_error}

    def _save(self, **kw):
        """Атомарная запись: временный файл в том же каталоге, fsync и os.replace."""
        data = {k: v for k, v in kw.items() if v}
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        with self._save_lock:
            fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(parent))
            try:
                try:
                    os.chmod(tmp_name, 0o600)   # mkstemp уже 600 на POSIX; на экзотической ФС не мешаем записи
                except OSError:
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fd = -1
                    json.dump(data, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, self.path)
                tmp_name = None
                if hasattr(os, "O_DIRECTORY"):   # fsync каталога — чтобы замена пережила сбой питания
                    try:
                        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(dfd)
                        finally:
                            os.close(dfd)
                    except OSError:
                        pass
            finally:
                if fd != -1:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if tmp_name:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
            self.data = data
            self.load_error = None

    def logout(self):
        with self._save_lock:
            self.data = {}
            self.path.unlink(missing_ok=True)
            self.load_error = None
        return self.info()

    # ---- HTTP ----
    @staticmethod
    def _req(opener, url, data=None, headers=None, json_body=None, timeout=15):
        h = {"User-Agent": UA, **(headers or {})}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
        try:
            with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise AuthError(f"{urllib.parse.urlsplit(url).netloc}: HTTP {e.code}")
        except OSError as e:
            raise AuthError(f"{urllib.parse.urlsplit(url).netloc}: {e}")

    def _finish(self, x_token=None, music_token=None, method=""):
        """Из x-token получаем токен Музыки, узнаём логин и сохраняем."""
        if x_token and not music_token:
            r = json.loads(self._req(None, "https://oauth.mobile.yandex.net/1/token", data={
                "client_id": MUSIC_CLIENT[0], "client_secret": MUSIC_CLIENT[1],
                "grant_type": "x-token", "access_token": x_token}))
            music_token = r.get("access_token")
            if not music_token:
                raise AuthError("Яндекс не выдал токен Музыки")
        try:
            login = json.loads(self._req(None, "https://login.yandex.ru/info?format=json",
                                         headers={"Authorization": f"OAuth {music_token}"})).get("login")
        except AuthError:
            raise AuthError("токен не подошёл — войдите ещё раз")
        self._save(music_token=music_token, x_token=x_token, login=login, method=method)
        return self.info()

    # ---- вход по QR ----
    def qr_start(self):
        with self.lock:
            jar = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            page = self._req(op, "https://passport.yandex.ru/pwl-yandex")
            m = re.search(r'__CSRF__ = "([^"]+)', page)
            if not m:
                raise AuthError("Страница входа Яндекса изменилась — используйте «Войти через Яндекс»")
            headers = {"X-CSRF-Token": m[1]}
            submit = json.loads(self._req(op, "https://passport.yandex.ru/pwl-yandex/api/passport/auth/password/submit",
                                          json_body={"retpath": "https://passport.yandex.ru/"}, headers=headers))
            magic = json.loads(self._req(op, "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code",
                                         data={"location_id": "0", "magic_track_id": submit["track_id"], "track_id": ""},
                                         headers=headers))
            if "link" not in magic:
                raise AuthError("Яндекс не выдал QR-код")
            self.qr = {"jar": jar, "op": op, "headers": headers, "submit": submit, "started": time.time()}
            return {"link": magic["link"]}

    def qr_status(self):
        with self.lock:
            q = self.qr
            if not q:
                return {"state": "none"}
            if time.time() - q["started"] > 300:
                self.qr = None
                return {"state": "expired"}
            r = json.loads(self._req(q["op"], "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code/status",
                                     json_body=q["submit"], headers=q["headers"]))
            if r.get("state") != "otp_auth_finished":
                return {"state": "waiting"}
            self._req(q["op"], "https://passport.yandex.ru/pwl-yandex/api/passport/sessions/get_session",
                      data={"track_id": r["trackId"]}, headers=q["headers"])
            cookies = "; ".join(f"{c.name}={c.value}" for c in q["jar"] if c.domain.endswith("yandex.ru"))
            t = json.loads(self._req(None, "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid",
                                     data={"client_id": PASSPORT_CLIENT[0], "client_secret": PASSPORT_CLIENT[1]},
                                     headers={"Ya-Client-Host": "passport.yandex.ru", "Ya-Client-Cookie": cookies}))
            self.qr = None
            if "access_token" not in t:
                raise AuthError("не удалось получить токен по сессии")
            return {"state": "done", **self._finish(x_token=t["access_token"], method="qr")}

    # ---- запасной вход: официальный OAuth, токен приходит в адресе страницы ----
    def oauth_finish(self, url_or_token):
        s = url_or_token.strip()
        m = re.search(r"access_token=([^&#\s]+)", s)
        token = m[1] if m else s
        if not re.fullmatch(r"[A-Za-z0-9_\-.]{20,}", token):
            raise AuthError("в адресе нет access_token — скопируйте адрес страницы целиком после входа")
        return self._finish(music_token=token, method="oauth")


class WebSocket:
    """Минимальный клиент RFC 6455 поверх TLS: текстовые кадры, ping/pong, close.

    Входящие кадры и буфер ограничены: сломанная или взломанная колонка не должна
    заставить панель выделить произвольный объём памяти.
    """

    MAX_FRAME_BYTES = 4 * 1024 * 1024
    MAX_MESSAGE_BYTES = 8 * 1024 * 1024
    MAX_BUFFER_BYTES = MAX_FRAME_BYTES + 14
    MAX_HANDSHAKE_BYTES = 16 * 1024

    def __init__(self, host, port, timeout=10, ssl_context=None):
        ctx = ssl_context if ssl_context is not None else glagol_ssl_context()[0]
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            raw.settimeout(timeout)
            self.sock = ctx.wrap_socket(raw, server_hostname=host)
        except BaseException:
            try:
                raw.close()
            except OSError:
                pass
            raise
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: https://{host}\r\n\r\n").encode())
            head = b""
            deadline = time.monotonic() + timeout
            while b"\r\n\r\n" not in head:
                if len(head) > self.MAX_HANDSHAKE_BYTES:
                    raise ConnectionError("Glagol: слишком большой ответ при рукопожатии")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectionError("Glagol: таймаут рукопожатия")
                self.sock.settimeout(remaining)
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    raise ConnectionError("Glagol: таймаут рукопожатия") from None
                if not chunk:
                    raise ConnectionError("колонка закрыла соединение при рукопожатии")
                head += chunk
            if len(head) > self.MAX_HANDSHAKE_BYTES:
                raise ConnectionError("Glagol: слишком большой ответ при рукопожатии")
            status, _, self.buf = head.partition(b"\r\n\r\n")
            if b" 101 " not in status.split(b"\r\n")[0]:
                raise ConnectionError("Glagol: " + status.split(b"\r\n")[0].decode(errors="replace"))
            self.sock.settimeout(timeout)
        except BaseException:
            try:
                self.sock.close()
            except OSError:
                pass
            raise
        self.wlock = threading.Lock()

    def _fill(self, n):
        # Только дочитываем в буфер, ничего не забирая: если recv упадёт по таймауту посреди кадра,
        # следующий вызов разберёт кадр с начала. Читаем не больше недостающего, поэтому буфер
        # ограничен размером кадра, а не скоростью/аппетитом отправителя.
        if n > self.MAX_BUFFER_BYTES:
            raise ConnectionError(f"Glagol: кадр больше {self.MAX_FRAME_BYTES} байт")
        while len(self.buf) < n:
            chunk = self.sock.recv(min(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("соединение закрыто")
            self.buf += chunk

    def send(self, data, opcode=1):
        if isinstance(data, str):
            data = data.encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.wlock:
            self.sock.sendall(head + mask + body)

    def recv(self):
        """Следующее текстовое сообщение (склеивает фрагменты, сам отвечает на ping)."""
        parts = []
        total = 0
        while True:
            self._fill(2)
            b1, b2 = self.buf[0], self.buf[1]
            opcode, n, pos = b1 & 0x0F, b2 & 0x7F, 2
            if n == 126:
                self._fill(4)
                n, pos = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                self._fill(10)
                n, pos = struct.unpack(">Q", self.buf[2:10])[0], 10
            if n > self.MAX_FRAME_BYTES:
                raise ConnectionError(f"Glagol: кадр {n} байт превышает предел {self.MAX_FRAME_BYTES}")
            if opcode >= 8 and (not (b1 & 0x80) or n > 125):
                raise ConnectionError("Glagol: некорректный управляющий кадр")
            mask = None
            if b2 & 0x80:
                self._fill(pos + 4)
                mask, pos = self.buf[pos:pos + 4], pos + 4
            self._fill(pos + n)
            data, self.buf = self.buf[pos:pos + n], self.buf[pos + n:]
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                raise ConnectionError("колонка закрыла соединение")
            if opcode == 9:
                self.send(data, opcode=10)
                continue
            if opcode == 10:
                continue
            total += n
            if total > self.MAX_MESSAGE_BYTES:
                raise ConnectionError(f"Glagol: сообщение превышает предел {self.MAX_MESSAGE_BYTES} байт")
            parts.append(data)
            if b1 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Glagol:
    """Держит соединение с колонкой, хранит последнее состояние и отправляет команды."""

    def __init__(self, auth, host, device_id, platform, port=None, config=None, ca=None):
        self.config = config or (_cfg_glagol if _cfg_glagol else None)
        effective_port = port if port is not None else (self.config.port if self.config else YASTATION_GLAGOL_PORT)
        self.auth, self.host, self.device_id, self.platform, self.port = auth, host, device_id, platform, effective_port
        self.ws = None
        self.token = None
        self.state = None          # последний «state» от колонки
        self.updated = 0.0
        self.error = None
        self.waiters = {}
        self.waiters_lock = threading.Lock()   # и словарь ожиданий, и self.ws меняются под ним
        self.wake = threading.Event()
        cfg_ca = ca if ca is not None else (getattr(self.config, "ca", None) if self.config else None)
        try:
            self._tls_context, self.tls_verified, self.tls_warning = glagol_ssl_context(cfg_ca)
        except Exception as e:   # панель должна подняться даже при сломанном TLS-окружении
            self._tls_context, self.tls_verified = None, False
            self.tls_warning = f"Glagol: не удалось подготовить TLS: {e}"
        threading.Thread(target=self._run, daemon=True, name="glagol").start()

    def info(self):
        return {"connected": self.ws is not None, "error": self.error, "state": self.state, "updated": self.updated,
                "tls_verified": getattr(self, "tls_verified", False),
                "tls_warning": getattr(self, "tls_warning", None)}

    def _conversation_token(self):
        q = urllib.parse.urlencode({"device_id": self.device_id, "platform": self.platform})
        cfg = getattr(self, "config", None)
        token_url = cfg.quasar_token_url if cfg else "https://quasar.yandex.net/glagol/token"
        req = urllib.request.Request(f"{token_url}?{q}",
                                     headers={"Authorization": f"OAuth {self.auth.music_token}", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError("Яндекс не принял токен — войдите заново")
            raise AuthError(f"quasar.yandex.net: HTTP {e.code}")
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLError):
                raise AuthError("контейнер не доверяет сертификату quasar.yandex.net — проверьте сертификаты "
                                "и прокси (SSL_CERT_FILE, HTTPS_PROXY)") from None
            raise AuthError(f"quasar.yandex.net: {reason}") from None
        if resp.get("status") != "ok":
            raise AuthError(f"quasar.yandex.net: {resp}")
        return resp["token"]

    def _envelope(self, payload, rid=None):
        return json.dumps({"conversationToken": self.token, "id": rid or str(uuid.uuid4()),
                           "payload": payload, "sentTime": int(time.time() * 1000)})

    def _resolve_waiter(self, rid, msg):
        """Будит ожидающий send; словарь и запись ответа защищены waiters_lock."""
        if not rid:
            return
        with self.waiters_lock:
            waiter = self.waiters.get(rid)
            if waiter is None:
                return
            waiter["resp"] = msg
            waiter["event"].set()

    def _fail_waiters(self, status="disconnected"):
        """Соединение закрылось: не оставляем send ждать полный таймаут."""
        with self.waiters_lock:
            pending = list(self.waiters.values())
            self.waiters.clear()
            for waiter in pending:
                if waiter.get("resp") is None:
                    waiter["resp"] = {"status": status}
                waiter["event"].set()

    def _run(self):
        fails = 0
        cfg = getattr(self, "config", None)
        sock_to = getattr(cfg, "sock_timeout", 30) if cfg else 30
        ping_int = getattr(cfg, "ping_interval", 20.0) if cfg else 20.0
        ws_to = getattr(cfg, "ws_timeout", 10) if cfg else 10
        while True:
            if not self.auth.music_token or not self.host or not self.device_id:
                self.error = None
                self.wake.wait(2)
                self.wake.clear()
                continue
            ws = None
            try:
                self.token = self._conversation_token()
                ws = WebSocket(self.host, self.port, timeout=ws_to,
                               ssl_context=getattr(self, "_tls_context", None))
                ws.sock.settimeout(sock_to)
                with self.waiters_lock:
                    self.ws = ws
                self.error, fails = None, 0
                ws.send(self._envelope({"command": "softwareVersion"}))
                last_ping = time.time()
                while True:
                    try:
                        msg = json.loads(ws.recv())
                    except socket.timeout:
                        msg = None
                    if msg:
                        if "state" in msg:
                            self.state, self.updated = msg["state"], time.time()
                        self._resolve_waiter(msg.get("requestId"), msg)
                    if time.time() - last_ping > ping_int:   # не даём соединению заснуть
                        ws.send(self._envelope({"command": "ping"}))
                        last_ping = time.time()
                    if not self.auth.music_token:
                        raise ConnectionError("вышли из аккаунта")
            except AuthError as e:
                self.error = str(e)
                fails += 1
            except (OSError, ConnectionError, ValueError) as e:
                self.error = f"нет связи с колонкой по Glagol: {e}"
                fails += 1
            finally:
                with self.waiters_lock:
                    if self.ws is ws:
                        self.ws = None
                if ws:
                    ws.close()
                self._fail_waiters("disconnected")
            self.wake.wait(min(60, 3 * fails))
            self.wake.clear()

    def send(self, payload, timeout=None):
        cfg = getattr(self, "config", None)
        effective_timeout = timeout if timeout is not None else (getattr(cfg, "cmd_timeout", 6.0) if cfg else 6.0)
        with self.waiters_lock:
            ws = self.ws
            if not ws:
                raise AuthError(self.error or "нет соединения с колонкой по Glagol")
            rid = str(uuid.uuid4())
            w = {"event": threading.Event(), "resp": None}
            self.waiters[rid] = w
        try:
            ws.send(self._envelope(payload, rid))
            w["event"].wait(effective_timeout)
            resp = w["resp"] or {}
            return {"status": resp.get("status", "timeout")}
        finally:
            with self.waiters_lock:
                self.waiters.pop(rid, None)

    def reconnect(self):
        with self.waiters_lock:
            ws, self.ws = self.ws, None
        if ws:
            ws.close()
        self._fail_waiters("reconnect")
        self.wake.set()


class YandexMusic:
    """API Яндекс Музыки (api.music.yandex.net) с тем же токеном, что и Glagol: подробности о треке,
    текст песни, очередь, похожие треки, исполнитель, лайки. Ответы приводим к компактному виду."""
    API = _cfg_glagol.music_api_base if _cfg_glagol else "https://api.music.yandex.net"
    # Ключ подписи запросов к текстам (как в библиотеке yandex-music-api).
    LYRICS_KEY = _cfg_glagol.lyrics_key if _cfg_glagol else b"p93jhgh689SBReK6ghtw62"

    def __init__(self, auth):
        self.auth = auth
        self.cache = {}
        self._uid = None
        self._likes = (0.0, set())

    def _req(self, path, query=None, form=None, timeout=15):
        url = self.API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": f"OAuth {self.auth.music_token}", "User-Agent": "Yandex-Music-API",
                   "X-Yandex-Music-Client": "YandexMusicAndroid/24023621"}
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("result")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise AuthError(f"Яндекс Музыка: HTTP {e.code}")
        except OSError as e:
            raise AuthError(f"Яндекс Музыка: {e}")

    @staticmethod
    def cover(uri, size=400):
        return f"https://{uri.replace('%%', f'{size}x{size}')}" if uri else None

    def _short(self, t):
        return {"id": str(t.get("id")), "title": (t.get("title") or "") + (f" ({t['version']})" if t.get("version") else ""),
                "artists": ", ".join(a.get("name", "") for a in t.get("artists", [])),
                "cover": self.cover(t.get("coverUri") or (t.get("albums") or [{}])[0].get("coverUri"), 100),
                "durationMs": t.get("durationMs"), "available": t.get("available", True)}

    def uid(self):
        if not self._uid:
            self._uid = ((self._req("/account/status") or {}).get("account") or {}).get("uid")
        return self._uid

    def liked(self, refresh=False):
        ts, ids = self._likes
        if refresh or time.time() - ts > 60:
            lib = (self._req(f"/users/{self.uid()}/likes/tracks") or {}).get("library") or {}
            ids = {str(x.get("id")) for x in lib.get("tracks", [])}
            self._likes = (time.time(), ids)
        return ids

    def track(self, tid):
        tid = str(tid)
        if tid not in self.cache:
            res = self._req(f"/tracks/{tid}") or []
            if not res:
                raise AuthError("трек не найден в Яндекс Музыке")
            t = res[0]
            al = (t.get("albums") or [{}])[0]
            self.cache[tid] = {
                **self._short(t),
                "artistList": [{"id": str(a.get("id")), "name": a.get("name"), "cover": self.cover((a.get("cover") or {}).get("uri"), 200)}
                               for a in t.get("artists", [])],
                "coverBig": self.cover(t.get("coverUri") or al.get("coverUri"), 800),
                "explicit": t.get("contentWarning") == "explicit",
                "lyrics": bool((t.get("lyricsInfo") or {}).get("hasAvailableTextLyrics") or t.get("lyricsAvailable")),
                "album": {"id": str(al.get("id", "")), "title": al.get("title"), "year": al.get("year"), "genre": al.get("genre"),
                          "label": ", ".join(l.get("name", "") if isinstance(l, dict) else str(l) for l in al.get("labels", [])),
                          "trackCount": al.get("trackCount"), "releaseDate": (al.get("releaseDate") or "")[:10]},
                "colors": t.get("derivedColors"),
            }
        return {**self.cache[tid], "liked": tid in self.liked()}

    @staticmethod
    def _parse_lrc(lrc):
        lines = []
        for ln in (lrc or "").splitlines():
            m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", ln)
            if m:
                lines.append([int(m[1]) * 60 + float(m[2]), m[3]])
        return lines

    @staticmethod
    def _lrclib(title, artist, album, duration):
        """LRCLIB (lrclib.net) — открытая база текстов, часто с привязкой ко времени. Без ключей."""
        def get(path, **q):
            lrclib_base = _cfg_glagol.lrclib_api_base if _cfg_glagol else "https://lrclib.net/api"
            url = lrclib_base + path + "?" + urllib.parse.urlencode(q)
            req = urllib.request.Request(url, headers={"User-Agent": "yapanel-web (local panel)"})
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    return json.load(r)
            except (urllib.error.HTTPError, OSError, ValueError):
                return None
        r = get("/get", artist_name=artist, track_name=title, album_name=album or "", duration=round(duration or 0))
        if not r:   # точного совпадения нет — ищем и берём ближайшее по длительности
            found = get("/search", track_name=title, artist_name=artist) or []
            found = [x for x in found if x.get("syncedLyrics") or x.get("plainLyrics") or x.get("instrumental")]
            if found:
                r = min(found, key=lambda x: abs((x.get("duration") or 0) - (duration or 0)))
        return r

    def lyrics(self, tid):
        tid = str(tid)
        res = self._lyrics_yandex(tid)
        if res.get("sync"):
            return res
        # У Яндекса нет синхронизированного текста — пробуем LRCLIB; обычный текст Яндекса держим про запас.
        try:
            t = self.track(tid)
            lr = self._lrclib(t["title"], (t.get("artistList") or [{}])[0].get("name") or t["artists"],
                              (t.get("album") or {}).get("title"), (t.get("durationMs") or 0) / 1000)
        except AuthError:
            lr = None
        if lr:
            src = {"source": "LRCLIB", "sourceUrl": f"https://lrclib.net/api/get/{lr.get('id')}" if lr.get("id") else "https://lrclib.net"}
            sync = self._parse_lrc(lr.get("syncedLyrics"))
            if sync:
                return {"sync": sync, "text": "\n".join(l[1] for l in sync), **src}
            if not res.get("text"):
                if lr.get("plainLyrics"):
                    return {"sync": None, "text": lr["plainLyrics"], **src}
                if lr.get("instrumental"):
                    return {"sync": None, "text": None, "instrumental": True, **src}
        return res

    def lyrics_by_name(self, title, artist, album=None, duration=0):
        """Текст для трека не из Яндекса (например, с телефона по Bluetooth): только LRCLIB, вход не нужен."""
        lr = self._lrclib(title, artist, album, duration) if title and artist else None
        if not lr:
            return {"sync": None, "text": None}
        src = {"source": "LRCLIB", "sourceUrl": f"https://lrclib.net/api/get/{lr.get('id')}" if lr.get("id") else "https://lrclib.net"}
        sync = self._parse_lrc(lr.get("syncedLyrics"))
        if sync:
            return {"sync": sync, "text": "\n".join(l[1] for l in sync), **src}
        if lr.get("plainLyrics"):
            return {"sync": None, "text": lr["plainLyrics"], **src}
        return {"sync": None, "text": None, "instrumental": bool(lr.get("instrumental")), **src}

    def _lyrics_yandex(self, tid):
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(self.LYRICS_KEY, f"{tid}{ts}".encode(), hashlib.sha256).digest()).decode()
        # Сначала синхронизированный текст (LRC); его у многих аккаунтов нет — тогда обычный через supplement.
        r = self._req(f"/tracks/{tid}/lyrics", {"format": "LRC", "timeStamp": ts, "sign": sign})
        if r and r.get("downloadUrl"):
            with urllib.request.urlopen(r["downloadUrl"], timeout=15) as f:
                lrc = f.read().decode("utf-8", "replace")
            lines = self._parse_lrc(lrc)
            if lines:
                return {"sync": lines, "text": "\n".join(l[1] for l in lines), "writers": r.get("writers"),
                        "source": "Яндекс Музыка"}
        s = self._req(f"/tracks/{tid}/supplement") or {}
        ly = s.get("lyrics") or {}
        text = ly.get("fullLyrics") or ly.get("lyrics")
        return {"sync": None, "text": text, "lang": ly.get("textLanguage"), "source": "Яндекс Музыка" if text else None}

    def similar(self, tid):
        r = self._req(f"/tracks/{tid}/similar") or {}
        return [self._short(t) for t in r.get("similarTracks", [])[:30]]

    def artist(self, aid):
        b = self._req(f"/artists/{aid}/brief-info") or {}
        a = b.get("artist") or {}
        counts = a.get("counts") or {}
        return {"id": str(a.get("id", aid)), "name": a.get("name"), "genres": a.get("genres", []),
                "cover": self.cover((a.get("cover") or {}).get("uri"), 300), "likes": a.get("likesCount"),
                # крупное фото — для телефона, где оно во всю ширину (сервер отдаёт до 1000 точек, в пропорциях оригинала)
                "cover_big": self.cover((a.get("cover") or {}).get("uri"), 1000),
                "tracks": counts.get("tracks"), "albums": counts.get("directAlbums"),
                "popular": [self._short(t) for t in b.get("popularTracks", [])[:10]],
                "similar": [{"id": str(x.get("id")), "name": x.get("name")} for x in b.get("similarArtists", [])[:12]],
                "description": ((a.get("description") or {}).get("text") or "")[:1500]}

    def queue(self, player):
        """Очередь по тому, откуда играет колонка: альбом, плейлист, исполнитель; у радио — соседние треки."""
        ptype, pid, info = player.get("playlistType"), str(player.get("playlistId") or ""), player.get("entityInfo") or {}
        cur = str(player.get("id") or "")
        title, tracks = None, []
        if ptype == "Album" and pid:
            a = self._req(f"/albums/{pid}/with-tracks") or {}
            title = a.get("title")
            tracks = [self._short(t) for vol in a.get("volumes", []) for t in vol]
        elif ptype == "Playlist" and pid:
            owner, kind = pid.split(":", 1) if ":" in pid else (player.get("playlistPuid"), pid)
            p = self._req(f"/users/{owner}/playlists/{kind}") or {}
            title = p.get("title")
            tracks = [self._short(x["track"]) for x in p.get("tracks", []) if x.get("track")]
        elif ptype == "Artist" and pid:
            a = self._req(f"/artists/{pid}/tracks", {"page-size": 50}) or {}
            tracks = [self._short(t) for t in a.get("tracks", [])]
            title = "Треки исполнителя"
        if not tracks:   # радио, «Моя волна», одиночный трек — то, что знаем: предыдущий, текущий, следующий
            title = title or ("Моя волна" if pid == "user:onyourwave" else player.get("playlistDescription") or "Поток")
            ids = [x for x in ((info.get("prev") or {}).get("id"), cur, (info.get("next") or {}).get("id")) if x]
            tracks = []
            for x in ids:
                try:
                    raw = self._raw(x)
                except AuthError:
                    continue
                if raw.get("title"):   # в волне по треку соседи бывают без данных — такие не показываем
                    tracks.append(self._short(raw))
        return {"title": title, "type": ptype, "tracks": tracks[:300],
                "current": next((i for i, t in enumerate(tracks) if t["id"] == cur), None)}

    def _raw(self, tid):
        res = self._req(f"/tracks/{tid}") or []
        if not res:
            raise AuthError("трек не найден")
        return res[0]

    def like(self, tid, on):
        uid = self.uid()
        self._req(f"/users/{uid}/likes/tracks/{'add-multiple' if on else 'remove'}", form={"track-ids": str(tid)})
        self.liked(refresh=True)
        return {"liked": str(tid) in self.liked()}

    def dislike(self, tid):
        self._req(f"/users/{self.uid()}/dislikes/tracks/add-multiple", form={"track-ids": str(tid)})
        return {"ok": True}
