"""Доступ к панели: пароль, сессии, HTTPS с локальным сертификатом.

Пароль хранится только как PBKDF2-хэш в .panel_auth.json (права 600) рядом с server.py. Сессия — случайный
токен в cookie (HttpOnly, SameSite=Strict); в файле лежат только хэши токенов, поэтому утечка файла не даёт войти.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

try:
    from config import get_config
    _cfg_sec = get_config().security
except ImportError:
    _cfg_sec = None

COOKIE = _cfg_sec.cookie_name if _cfg_sec else "yl2_session"
SESSION_TTL = _cfg_sec.session_ttl_seconds if _cfg_sec else 90 * 24 * 3600       # продлевается при каждом заходе
PBKDF2_ITER = _cfg_sec.pbkdf2_iter if _cfg_sec else 240_000
MIN_PASSWORD = _cfg_sec.min_password_len if _cfg_sec else 4


class AccessError(Exception):
    pass


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class PanelAccess:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.fails = {}            # ip → (число неудач подряд, время последней)
        self.data = {}
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    # ---------- состояние ----------
    @property
    def enabled(self):
        return bool(self.data.get("hash"))

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def _check_password(self, password):
        if not self.enabled:
            return True
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(self.data["salt"]),
                                 self.data.get("iter", PBKDF2_ITER))
        return hmac.compare_digest(dk.hex(), self.data["hash"])

    # ---------- сессии ----------
    def session_ok(self, token):
        if not self.enabled:
            return True
        if not token:
            return False
        h = _hash_token(token)
        with self.lock:
            s = self.data.get("sessions", {}).get(h)
            now = time.time()
            if not s or now - s["last"] > SESSION_TTL:
                return False
            if now - s["last"] > 3600:          # не пишем файл на каждый запрос
                s["last"] = now
                self._save()
        return True

    def _new_session(self, ua):
        token = secrets.token_urlsafe(32)
        now = time.time()
        sessions = self.data.setdefault("sessions", {})
        # чистим просроченные
        for h in [h for h, s in sessions.items() if now - s["last"] > SESSION_TTL]:
            del sessions[h]
        sessions[_hash_token(token)] = {"created": now, "last": now, "ua": (ua or "")[:160]}
        return token

    # ---------- вход / выход ----------
    def login(self, ip, password, ua):
        with self.lock:
            n, t = self.fails.get(ip, (0, 0))
            wait = self._lock_left(n, t)
            if wait:
                raise AccessError(f"Слишком много попыток. Подождите {wait} с")
            if not self.enabled:
                raise AccessError("Пароль не задан")
            if not self._check_password(password or ""):
                self.fails[ip] = (n + 1, time.time())
                time.sleep(_cfg_sec.login_throttle_delay if _cfg_sec else 0.7)                  # медленнее перебор
                left = self._lock_left(n + 1, time.time())
                raise AccessError("Неверный пароль" + (f". Следующая попытка через {left} с" if left else ""))
            self.fails.pop(ip, None)
            token = self._new_session(ua)
            self._save()
            return token

    @staticmethod
    def _lock_left(n, t):
        """После 5 неудач подряд — пауза 30 с, дальше удваивается (до 15 мин)."""
        if n < 5:
            return 0
        max_pause = _cfg_sec.rate_limit_max_pause if _cfg_sec else 900
        pause = min(max_pause, 30 * 2 ** (n - 5))
        return max(0, int(t + pause - time.time()))

    def logout(self, token):
        if not token:
            return
        with self.lock:
            if self.data.get("sessions", {}).pop(_hash_token(token), None):
                self._save()

    def set_password(self, current, new, ua):
        """Задать/сменить пароль. Все остальные сессии сбрасываются; возвращает токен новой сессии."""
        with self.lock:
            if self.enabled and not self._check_password(current or ""):
                time.sleep(_cfg_sec.login_throttle_delay if _cfg_sec else 0.7)
                raise AccessError("Текущий пароль неверный")
            if len(new or "") < MIN_PASSWORD:
                raise AccessError(f"Пароль — не короче {MIN_PASSWORD} символов")
            salt = secrets.token_bytes(16)
            dk = hashlib.pbkdf2_hmac("sha256", new.encode(), salt, PBKDF2_ITER)
            self.data = {"hash": dk.hex(), "salt": salt.hex(), "iter": PBKDF2_ITER, "sessions": {}}
            token = self._new_session(ua)
            self._save()
            return token

    def disable(self, current):
        with self.lock:
            if not self._check_password(current or ""):
                time.sleep(_cfg_sec.login_throttle_delay if _cfg_sec else 0.7)
                raise AccessError("Текущий пароль неверный")
            self.data = {}
            self._save()

    def logout_others(self, token):
        with self.lock:
            keep = _hash_token(token) if token else None
            sessions = self.data.get("sessions", {})
            n = len(sessions)
            self.data["sessions"] = {h: s for h, s in sessions.items() if h == keep}
            self._save()
            return n - len(self.data["sessions"])

    def info(self, token):
        return {"enabled": self.enabled, "authed": self.session_ok(token),
                "sessions": len(self.data.get("sessions", {})) if self.enabled else 0}


# ---------- HTTPS ----------
def get_host_ips():
    """Retrieve host IP addresses using hostname -I with robust socket fallback on Windows."""
    ips = {"127.0.0.1"}
    try:
        res = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0 and res.stdout:
            ips.update(a for a in res.stdout.split() if ":" not in a)
    except (OSError, subprocess.SubprocessError):
        pass
    try:  # адрес маршрута по умолчанию
        probe_target = _cfg_sec.route_probe_ip if _cfg_sec else "192.0.2.1"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((probe_target, 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ips, key=lambda a: (ipaddress.ip_address(a).version, ipaddress.ip_address(a)))


def local_names():
    """Имена и адреса, по которым к панели могут прийти из сети: идут в SAN сертификата."""
    host = socket.gethostname()
    names = {"localhost", host, f"{host.split('.')[0]}.local"}
    ips = get_host_ips()
    return sorted(names), ips


def find_openssl():
    try:
        from config import get_config
        cfg_openssl = get_config().server.openssl_bin
    except ImportError:
        cfg_openssl = None
    explicit = cfg_openssl or os.environ.get("PANEL_OPENSSL")
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise AccessError("PANEL_OPENSSL указывает на отсутствующий файл: " + explicit)
    found = shutil.which("openssl")
    if found:
        return found
    if os.name == "nt":
        # Git for Windows includes OpenSSL but normally does not put it in PATH.
        roots = [Path(os.environ.get(name, fallback)) for name, fallback in
                 (("ProgramFiles", "C:/Program Files"), ("ProgramFiles(x86)", "C:/Program Files (x86)"))]
        if os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"])/"Programs")
        for root in roots:
            for suffix in ("Git/usr/bin/openssl.exe", "Git/mingw64/bin/openssl.exe"):
                candidate = root/suffix
                if candidate.is_file():
                    return str(candidate)
    raise AccessError("OpenSSL не найден: он нужен для создания HTTPS-сертификата. "
                      "Укажите путь к openssl в PANEL_OPENSSL или добавьте его в PATH. "
                      "Для работы только по HTTP используйте --https-port 0")


def _openssl(*args, cwd):
    try:
        r = subprocess.run([find_openssl(), *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as error:
        raise AccessError("Не удалось запустить OpenSSL; проверьте PANEL_OPENSSL и доступность его файлов") from error
    if r.returncode:
        raise AccessError("openssl: " + (r.stderr.strip().splitlines() or ["ошибка"])[-1])


def ensure_certs(tls_dir: Path):
    """Свой маленький CA + сертификат сервера, подписанный им.

    CA ограничен name constraints: он годится только для локальных адресов и имён этой машины, так что
    установленный на телефон, он не сможет подтвердить чужой сайт, даже если ключ утечёт.
    Сертификат сервера перевыпускается, когда у машины меняется набор адресов.
    """
    tls_dir.mkdir(mode=0o700, exist_ok=True)
    ca_key, ca_crt = tls_dir / "ca.key", tls_dir / "ca.crt"
    key, crt, sans_file = tls_dir / "server.key", tls_dir / "server.crt", tls_dir / "server.sans"
    names, ips = local_names()
    # Every SAN must fit the CA constraints, including on hosts with VPN/public interfaces.
    allowed = _cfg_sec.allowed_subnets if _cfg_sec else ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
    networks = [ipaddress.ip_network(n) for n in allowed]
    ips = [a for a in ips if any(ipaddress.ip_address(a) in n for n in networks)]
    sans = ",".join([f"DNS:{n}" for n in names] + [f"IP:{a}" for a in ips])
    ca_days = str(_cfg_sec.ca_validity_days if _cfg_sec else 3650)
    cert_days = str(_cfg_sec.cert_validity_days if _cfg_sec else 397)
    if not ca_crt.exists() or not ca_key.exists():
        permitted = ["IP:127.0.0.0/255.0.0.0", "IP:10.0.0.0/255.0.0.0", "IP:172.16.0.0/255.240.0.0",
                     "IP:192.168.0.0/255.255.0.0", "DNS:localhost", "DNS:local"] + [f"DNS:{n}" for n in names]
        with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as f:
            f.write("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                    "[dn]\nCN=Yandex Station panel CA\n"
                    "[ext]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n"
                    "subjectKeyIdentifier=hash\n"
                    f"nameConstraints=critical,{','.join('permitted;' + p for p in permitted)}\n")
            cnf = f.name
        try:
            _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                     "-keyout", str(ca_key), "-out", str(ca_crt), "-days", ca_days, "-config", cnf, cwd=tls_dir)
        finally:
            os.unlink(cnf)
        try:
            os.chmod(ca_key, 0o600)
        except OSError:
            pass
        crt.unlink(missing_ok=True)
    if crt.exists() and key.exists() and sans_file.exists() and sans_file.read_text(encoding="utf-8") == sans:
        return crt, key
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as f:
        f.write("basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                f"subjectAltName={sans}\n")
        ext = f.name
    csr = tls_dir / "server.csr"
    try:
        _openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key),
                 "-out", str(csr), "-subj", "/CN=" + socket.gethostname()[:64], cwd=tls_dir)
        # 397 дней — дольше браузеры (Safari, Chrome) не принимают
        _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                 "-out", str(crt), "-days", cert_days, "-extfile", ext, cwd=tls_dir)
    finally:
        os.unlink(ext)
        csr.unlink(missing_ok=True)
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    sans_file.write_text(sans, encoding="utf-8")
    return crt, key
