"""Клиент yapanel-agent (device/agent): команды, файлы и долгие процессы на колонке по TLS с токеном.

Агенту доверяем не по имени, а по самому сертификату: его панель читает с колонки по adb при установке,
и другой сертификат на том же адресе соединение не пройдёт. Токен — в заголовке Authorization;
на колонке хранится только его sha256.
"""
import base64
import http.client
import json
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlencode

PORT = 7861


class AgentError(Exception):
    """Агент не ответил или отказал — команду можно повторить через adb."""


class AgentLink:
    POOL = 4

    def __init__(self, host, token, cert_pem, port=PORT):
        self.host, self.port, self.token = host, port, token
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False          # имя не важно: доверяем ровно этому сертификату
        ctx.load_verify_locations(cadata=cert_pem)
        self.ctx = ctx
        self._idle = []
        self._lock = threading.Lock()

    # ---------- HTTP ----------
    def _take(self, timeout, fresh=False):
        with self._lock:
            conn = self._idle.pop() if self._idle and not fresh else None
        reused = conn is not None
        if conn is None:
            conn = http.client.HTTPSConnection(self.host, self.port, context=self.ctx, timeout=timeout)
        conn.timeout = timeout
        if conn.sock:
            conn.sock.settimeout(timeout)
        return conn, reused

    def _give(self, conn):
        with self._lock:
            if len(self._idle) < self.POOL:
                self._idle.append(conn)
                return
        conn.close()

    def close(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for c in idle:
            c.close()

    def request(self, method, path, params=None, body=None, timeout=30):
        """(код, тело). TimeoutError — агент не ответил вовремя; AgentError — его нет или отказал."""
        url = path + ("?" + urlencode(params) if params else "")
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        for attempt in (0, 1):
            conn, reused = self._take(timeout, fresh=attempt > 0)
            try:
                conn.request(method, url, body=body, headers=headers)
                r = conn.getresponse()
                data = r.read()
            except socket.timeout:
                conn.close()
                raise TimeoutError(f"агент не ответил за {timeout:g} с") from None
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                # простоявшее соединение могло закрыться — один раз пробуем новое
                if reused and attempt == 0 and isinstance(e, (http.client.RemoteDisconnected, BrokenPipeError,
                                                              ConnectionResetError)):
                    continue
                raise AgentError(f"{self.host}:{self.port}: {e}") from None
            if r.will_close:
                conn.close()
            else:
                self._give(conn)
            if r.status == 401:
                raise AgentError("агент не принял токен панели")
            return r.status, data
        raise AgentError("агент не отвечает")

    def _json(self, status, data):
        try:
            return json.loads(data or b"null")
        except ValueError:
            raise AgentError(f"агент ответил не JSON ({status})") from None

    def _error(self, status, data):
        m = self._json(status, data)
        return (m or {}).get("error") if isinstance(m, dict) else None

    def ping(self, timeout=4):
        status, data = self.request("GET", "/v1/ping", timeout=timeout)
        if status != 200:
            raise AgentError(f"ping: {status}")
        return self._json(status, data)

    def exec(self, cmd, timeout=15):
        """(вывод, код). stdout и stderr вместе, как у adb shell. По тайм-ауту агент гасит команду."""
        status, data = self.request("POST", "/v1/exec", body=json.dumps({"cmd": cmd, "timeout": timeout}).encode(),
                                    timeout=timeout + 10)
        if status != 200:
            raise AgentError(self._error(status, data) or f"exec: {status}")
        m = self._json(status, data)
        if m.get("timeout"):
            raise TimeoutError(f"команда не закончилась за {timeout:g} с")
        return m.get("out", ""), int(m.get("rc", -1))

    def read_file(self, path, timeout=120):
        status, data = self.request("GET", "/v1/file", {"path": path}, timeout=timeout)
        if status == 404:
            raise FileNotFoundError(f"{path}: No such file or directory")
        if status != 200:
            raise OSError(self._error(status, data) or f"{path}: {status}")
        return data

    def write_file(self, path, data, mode=0o644, timeout=120):
        status, body = self.request("PUT", "/v1/file", {"path": path, "mode": format(mode & 0o7777, "o")},
                                    body=bytes(data), timeout=timeout)
        if status != 200:
            raise OSError(self._error(status, body) or f"{path}: {status}")

    def ls(self, path):
        status, data = self.request("GET", "/v1/ls", {"path": path}, timeout=20)
        if status != 200:
            raise OSError(self._error(status, data) or f"{path}: {status}")
        return [(int(m), int(s), int(t), str(n)) for m, s, t, n in self._json(status, data)]

    def popen(self, cmd, pty=False, cols=80, rows=24, text=False):
        return AgentProc(self, cmd, pty, cols, rows, text)


class AgentProc:
    """Долгий процесс на колонке, как subprocess.Popen: stdout (поток), stdin.write, kill, wait, returncode.
    Вывод приходит по WebSocket и перекладывается в локальный канал — его можно читать построчно."""

    def __init__(self, link, cmd, pty, cols, rows, text):
        raw = socket.create_connection((link.host, link.port), timeout=10)
        try:
            self.sock = link.ctx.wrap_socket(raw)
            key = base64.b64encode(os.urandom(16)).decode()
            q = urlencode({"cmd": cmd, "pty": "1" if pty else "0", "cols": cols, "rows": rows})
            self.sock.sendall((f"GET /v1/stream?{q} HTTP/1.1\r\nHost: {link.host}:{link.port}\r\n"
                               f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                               f"Sec-WebSocket-Version: 13\r\nAuthorization: Bearer {link.token}\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise AgentError("агент закрыл соединение")
                head += chunk
        except (OSError, ssl.SSLError) as e:
            raw.close()
            raise AgentError(f"поток: {e}") from None
        status, _, self.buf = head.partition(b"\r\n\r\n")
        line = status.split(b"\r\n")[0].decode(errors="replace")
        if " 101 " not in line:
            self.sock.close()
            raise AgentError("агент не принял токен панели" if " 401 " in line else f"поток: {line}")
        self.sock.settimeout(None)
        self.returncode = None
        self._wlock = threading.Lock()
        r, w = os.pipe()
        self._w = w
        self.stdout = os.fdopen(r, "r" if text else "rb", buffering=1 if text else 0,
                                **({"encoding": "utf-8", "errors": "replace"} if text else {}))
        self.stdin = _Stdin(self)
        self._reader = threading.Thread(target=self._read, daemon=True, name="agent-stream")
        self._reader.start()

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("соединение закрыто")
            self.buf += chunk

    def _frame(self):
        self._fill(2)
        b1, b2 = self.buf[0], self.buf[1]
        n, pos = b2 & 0x7F, 2
        if n == 126:
            self._fill(4)
            n, pos = struct.unpack(">H", self.buf[2:4])[0], 4
        elif n == 127:
            self._fill(10)
            n, pos = struct.unpack(">Q", self.buf[2:10])[0], 10
        self._fill(pos + n)
        data, self.buf = self.buf[pos:pos + n], self.buf[pos + n:]
        return b1 & 0x0F, data

    def _read(self):
        try:
            while True:
                op, data = self._frame()
                if op == 2:
                    os.write(self._w, data)
                elif op == 1:
                    try:
                        m = json.loads(data)
                    except ValueError:
                        continue
                    if "exit" in m:
                        self.returncode = int(m["exit"])
                    elif "error" in m:
                        os.write(self._w, ("агент: " + str(m["error"]) + "\n").encode())
                        self.returncode = 127
                elif op == 9:
                    self._send(data, 10)
                elif op == 8:
                    break
        except (OSError, ValueError):
            pass
        finally:
            if self.returncode is None:
                self.returncode = -9
            try:
                os.close(self._w)
            except OSError:
                pass
            self._close_sock()

    def _send(self, data, opcode=2):
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
        with self._wlock:
            self.sock.sendall(head + mask + body)

    def resize(self, cols, rows):
        self._send(json.dumps({"resize": [int(cols), int(rows)]}).encode(), 1)

    def _close_sock(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def kill(self):
        """Закрываем соединение — агент гасит всю группу процессов на колонке."""
        try:
            self._send(b"", 8)
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._close_sock()

    terminate = kill

    def poll(self):
        return self.returncode if not self._reader.is_alive() else None

    def wait(self, timeout=None):
        self._reader.join(timeout)
        if self._reader.is_alive():
            import subprocess
            raise subprocess.TimeoutExpired("agent", timeout)
        return self.returncode


class _Stdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.proc._send(bytes(data))
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass
