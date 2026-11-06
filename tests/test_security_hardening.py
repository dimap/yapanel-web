"""Регрессии безопасности HTTP-панели: доступ, CSRF, лимит тела, файловый менеджер и логи.

Тесты поднимают настоящий ``server.Handler`` на localhost. Колонка заменена
записывающим фейковым adb, поэтому реальный ADB и устройство не нужны.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import access
import server


PASSWORD = "security-unit-password"


class FakeConn:
    online = True
    state = "online"
    attempts = 1

    def info(self):
        return {"state": self.state, "online": True}


class RecordingAdb:
    """Минимальный adb: запоминает команды/push, но ничего не делает с устройством."""

    is_local = False
    serial = "127.0.0.1:5555"
    adb = "adb"

    def __init__(self):
        self.commands = []
        self.pushes = []

    def shell(self, cmd, timeout=15, check=True, **_):
        self.commands.append(cmd)
        return "", "", 0

    def push(self, local, remote, timeout=120):
        data = Path(local).read_bytes()
        self.pushes.append((str(remote), data))
        return len(data)

    def pull(self, remote, local, timeout=120):
        Path(local).write_bytes(b"old")
        return 3

    def ls(self, path):
        return []


class LivePanel(server.Panel):
    """Panel без конструктора: все нужные атрибуты задаются вручную."""

    def __init__(self, adb):
        self.adb = adb
        self.conn = FakeConn()
        self.ffmpeg = None
        self._version = None
        self._ww_info = None
        self._ww_router = None
        self._ww_router_stop = threading.Event()
        self._ww_forward_lock = threading.Lock()
        self._ww_connect_at = None
        self.party = None
        self._party_lock = threading.Lock()
        # do_GET заранее собирает таблицу маршрутов и обращается к этим объектам.
        self.leds = SimpleNamespace(get=lambda: {})
        self.metrics = SimpleNamespace(get=lambda _range=3600: {})


class SecurityHandler(server.Handler):
    __test__ = False


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("security")
    auth = access.PanelAccess(data_dir / ".panel_auth.json")
    auth.set_password("", PASSWORD, "security-tests")
    adb = RecordingAdb()
    panel = LivePanel(adb)

    SecurityHandler.panel = panel
    SecurityHandler.access = auth
    SecurityHandler.updater = None
    SecurityHandler.ports = {}

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SecurityHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(httpd=httpd, auth=auth, panel=panel, adb=adb,
                              port=httpd.server_port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clear_adb_records(live_server):
    live_server.adb.commands.clear()
    live_server.adb.pushes.clear()
    yield


def cookie_header(auth):
    return f"{access.COOKIE}={auth._new_session('security-tests')}"


def request(live_server, method, path, data=None, *, raw=None, headers=None, cookie=True):
    hdrs = dict(headers or {})
    if cookie:
        hdrs.setdefault("Cookie", cookie_header(live_server.auth))
    body = raw
    if data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    conn = http.client.HTTPConnection("127.0.0.1", live_server.port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=hdrs)
        response = conn.getresponse()
        return response.status, response.read(), {k.lower(): v for k, v in response.getheaders()}
    finally:
        conn.close()


# ---------- PanelAccess ----------
@pytest.mark.parametrize("payload", [b"{broken json", b"[]", b"null", b'"token"'])
def test_corrupt_panel_auth_is_fail_closed(tmp_path, payload):
    path = tmp_path / ".panel_auth.json"
    path.write_bytes(payload)
    manager = access.PanelAccess(path)

    assert manager.session_ok("any-token") is False
    assert manager.session_ok("") is False
    assert manager.info("any-token")["authed"] is False
    # Повреждённый файл нельзя молча превращать в «пароль не задан».
    assert path.read_bytes() == payload


def test_missing_panel_auth_keeps_password_not_set(tmp_path):
    manager = access.PanelAccess(tmp_path / ".panel_auth.json")
    assert manager.session_ok("any-token") is True


# ---------- CSRF / Origin / Sec-Fetch-Site ----------
def test_post_with_foreign_origin_is_rejected(live_server):
    status, body, _ = request(
        live_server, "POST", "/api/fs/delete", {"path": "/data/local/tmp/x"},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    assert "чужого сайта" in json.loads(body)["error"]
    assert live_server.adb.commands == []


def test_post_with_empty_body_and_cross_site_fetch_metadata_is_rejected(live_server):
    # Пустое тело не должно обходить CSRF-проверку: браузер с чужой страницы
    # пришлёт Sec-Fetch-Site: cross-site (Origin может отсутствовать), и такой
    # POST не должен доходить до обработчика.
    status, body, _ = request(
        live_server, "POST", "/api/panel-auth/logout", raw=b"",
        headers={"Content-Type": "application/json", "Content-Length": "0",
                 "Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403, f"POST с cross-site без Origin прошёл в обработчик: {status} {body!r}"


def test_post_without_origin_or_fetch_metadata_requires_auth(live_server):
    # Без Origin и Sec-Fetch-Site (curl, старые клиенты) запрос не считается
    # доверенным автоматически: без сессии он должен получить 401, а не 200.
    status, body, _ = request(
        live_server, "POST", "/api/panel-auth/logout", raw=b"",
        headers={"Content-Type": "application/json", "Content-Length": "0"},
        cookie=False,
    )
    assert status == 401, f"POST без Origin/Sec-Fetch-Site и без сессии не требует вход: {status} {body!r}"


def test_post_with_cross_site_fetch_metadata_is_rejected(live_server):
    status, body, _ = request(
        live_server, "POST", "/api/fs/delete", {"path": "/data/local/tmp/x"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert live_server.adb.commands == []


def test_post_with_same_origin_fetch_metadata_is_allowed(live_server):
    # Современные браузеры всегда шлют Sec-Fetch-Site; реализация вправе
    # доверять same-origin и без Origin.
    status, body, _ = request(
        live_server, "POST", "/api/fs/write",
        {"path": "/data/local/tmp/security.txt", "text": "ok"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert status == 200, body
    assert live_server.adb.pushes[-1] == ("/data/local/tmp/security.txt", b"ok")


# ---------- лимит тела ----------
def _raw_status(port, request_bytes, timeout=3):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request_bytes)
        sock.settimeout(timeout)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pytest.fail("сервер пытался читать тело больше лимита вместо ответа 413 по Content-Length")
    head = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    match = re.match(r"HTTP/\d\.\d\s+(\d{3})", head)
    assert match, f"не удалось разобрать ответ: {data[:200]!r}"
    return int(match.group(1))


def test_json_content_length_above_limit_returns_413(live_server):
    limit = int(getattr(server, "JSON_BODY_LIMIT", 8 * 1024 * 1024))
    oversized = limit + 1
    headers = [
        "POST /api/fs/write HTTP/1.1",
        f"Host: 127.0.0.1:{live_server.port}",
        f"Origin: http://127.0.0.1:{live_server.port}",
        "Sec-Fetch-Site: same-origin",
        f"Cookie: {cookie_header(live_server.auth)}",
        "Content-Type: application/json",
        f"Content-Length: {oversized}",
        "Connection: close",
        "",
        "",
    ]
    status = _raw_status(live_server.port, "\r\n".join(headers).encode("ascii"))
    assert status == 413
    assert live_server.adb.pushes == []


# ---------- файловый менеджер ----------
@pytest.mark.parametrize("path", [
    "/system/etc/hosts",
    "/system/bin/sh",
    "/etc/passwd",
    "/proc/self/mem",
    "/data/quasar/evil",
    "/data/data/com.yandex/evil",
    "/data/local/evil",
])
def test_fs_write_and_delete_reject_protected_trees(live_server, path):
    status, body, _ = request(live_server, "POST", "/api/fs/write",
                              {"path": path, "text": "x"})
    assert status in (400, 403), f"запись в {path} не запрещена: {status} {body!r}"
    assert live_server.adb.pushes == []

    status, body, _ = request(live_server, "POST", "/api/fs/delete", {"path": path})
    assert status in (400, 403), f"удаление {path} не запрещено: {status} {body!r}"
    assert not any("rm -rf" in command for command in live_server.adb.commands)


def test_fs_write_and_delete_allow_data_local_tmp(live_server):
    path = "/data/local/tmp/security-allowed.txt"
    status, body, _ = request(live_server, "POST", "/api/fs/write",
                              {"path": path, "text": "allowed"})
    assert status == 200, body
    assert live_server.adb.pushes[-1] == (path, b"allowed")

    status, body, _ = request(live_server, "POST", "/api/fs/delete", {"path": path})
    assert status == 200, body
    assert any(f"rm -rf {path}" in command for command in live_server.adb.commands)


def test_fs_write_allows_panel_web_dir(live_server):
    path = f"{server.WEB_DIR}/edited-by-hand.txt"
    status, body, _ = request(live_server, "POST", "/api/fs/write",
                              {"path": path, "text": "panel-owned"})
    assert status == 200, body
    assert live_server.adb.pushes[-1] == (path, b"panel-owned")


def test_fs_list_hides_tls_directory(live_server):
    from urllib.parse import quote
    status, body, _ = request(live_server, "GET",
                              f"/api/fs/list?path={quote(server.WEB_DIR + '/.tls', safe='')}")
    assert status in (400, 403), f"служебный .tls виден в файловом менеджере: {status} {body!r}"


# ---------- логи ----------
@pytest.mark.parametrize("name", [
    "tmp:../../etc/passwd",
    "tmp:/etc/passwd",
    "tmp:..",
    "tmp:.",
    "tmp:sub/../../etc/passwd",
])
def test_log_rejects_tmp_path_traversal(live_server, name):
    from urllib.parse import quote
    status, body, _ = request(live_server, "GET", f"/api/log?name={quote(name, safe='')}&lines=50")
    assert status in (400, 404), f"лог {name!r} не отклонён: {status} {body!r}"
    assert not any("etc/passwd" in command for command in live_server.adb.commands)
    assert not any("cd /data/local/tmp" in command or ".." in command
                   for command in live_server.adb.commands)


def test_log_allows_plain_tmp_name(live_server):
    status, body, _ = request(live_server, "GET", "/api/log?name=tmp:yamd.log&lines=50")
    assert status == 200, body
    assert any("tail -n 50 /data/local/tmp/yamd.log" in command
               for command in live_server.adb.commands)


# ---------- кликджекинг ----------
def test_security_headers_deny_framing(live_server):
    status, body, headers = request(live_server, "GET", "/api/panel-auth", cookie=False)
    assert status == 200
    assert headers.get("x-frame-options", "").upper() == "DENY"
    csp = headers.get("content-security-policy", "")
    assert re.search(r"frame-ancestors\s+('none'|'self')", csp, re.IGNORECASE), \
        f"в CSP нет ограничения frame-ancestors: {csp!r}"
