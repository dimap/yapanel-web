"""adb на чистом Python — для телефона (APK), где программы adb нет.

Говорит с adbd колонки по протоколу adb поверх TCP (порт 5555): shell, sync (push/pull/ls), проброс порта.
Панель зовёт adb через subprocess в двух десятках мест, поэтому здесь не новый API, а подмена самой программы:
install() перехватывает subprocess.run/Popen для argv[0] == ADB и выполняет те же команды в этом процессе
(connect, disconnect, devices, get-state, version, shell, push, pull, ls, forward). Остальные вызовы
subprocess идут как обычно.

Ограничения: только сеть (не USB); ключ adb (AUTH) не поддерживается — у рутованных колонок adbd ключа не
спрашивает; код выхода shell колонки не передаёт (старый adbd без shell_v2) — его метку ставит сама панель.
"""
import io
import os
import shlex
import socket
import struct
import subprocess
import threading
import time

ADB = "pyadb"               # «путь к adb», по которому панель нас зовёт
VERSION_TEXT = "Android Debug Bridge version 1.0.41\nVersion 34.0.5-pyadb\n"

A_CNXN, A_AUTH, A_OPEN, A_OKAY, A_CLSE, A_WRTE = (struct.unpack("<I", c)[0] for c in
                                                   (b"CNXN", b"AUTH", b"OPEN", b"OKAY", b"CLSE", b"WRTE"))
A_VERSION = 0x01000000
MAX_PAYLOAD = 256 * 1024
# Сколько несчитанных данных поток может держать в памяти. Потребитель, который не успевает
# читать (или вообще не читает), не должен съедать RAM бесконечным выводом команды.
MAX_STREAM_BUFFER = 64 * 1024 * 1024


class AdbError(Exception):
    pass


def _packet(cmd, a0, a1, data=b""):
    return struct.pack("<6I", cmd, a0, a1, len(data), sum(data) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF) + data


class Stream:
    """Один поток adb (shell:…, sync:, tcp:…): приём в очередь, отправка с ожиданием OKAY (управление потоком)."""

    def __init__(self, dev, local_id):
        self.dev, self.local, self.remote = dev, local_id, None
        self.opened = threading.Event()
        self.okay = threading.Event()
        self.closed = threading.Event()
        self.cond = threading.Condition()
        self.buf = bytearray()
        self.overflow = False

    def _on_data(self, data):
        """Принять данные; False — пакет не принят (поток закрыт или переполнен буфер)."""
        overflow = False
        with self.cond:
            if self.closed.is_set():
                return False
            if len(self.buf) + len(data) > MAX_STREAM_BUFFER:
                self.overflow = True
                overflow = True
            else:
                self.buf += data
            self.cond.notify_all()
        if overflow:
            # Закрываем поток, не держа cond: close() снова берёт его внутри _on_close.
            self.close()
        return not overflow

    def _on_close(self):
        self.closed.set()
        self.opened.set()
        self.okay.set()
        with self.cond:
            self.cond.notify_all()

    def write(self, data):
        mv = memoryview(bytes(data))
        while mv:
            if self.closed.is_set():
                raise AdbError("error: closed")
            chunk, mv = mv[:self.dev.max_data], mv[self.dev.max_data:]
            self.okay.clear()
            self.dev._send(_packet(A_WRTE, self.local, self.remote, bytes(chunk)))
            if not self.okay.wait(30) or self.closed.is_set() and not self.okay.is_set():
                raise AdbError("error: closed")

    def read(self, n=-1, timeout=None):
        """До n байт (−1 — всё, что есть); b"" — поток закрыт и данных больше нет."""
        end = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while not self.buf and not self.closed.is_set():
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    raise socket.timeout("read timeout")
                self.cond.wait(left)
            if not self.buf and self.overflow:
                raise AdbError("error: stream buffer overflow")
            k = len(self.buf) if n < 0 else min(n, len(self.buf))
            out = bytes(self.buf[:k])
            del self.buf[:k]
            return out

    def read_exact(self, n, timeout=None):
        out = bytearray()
        while len(out) < n:
            part = self.read(n - len(out), timeout)
            if not part:
                raise AdbError("error: closed")
            out += part
        return bytes(out)

    def close(self):
        if not self.closed.is_set() and self.remote is not None:
            try:
                self.dev._send(_packet(A_CLSE, self.local, self.remote))
            except OSError:
                pass
        self.dev._forget(self)
        self._on_close()


class Device:
    """Соединение с одним adbd: CNXN, чтение пакетов в отдельном потоке, раздача по потокам."""

    def __init__(self, serial, timeout=10):
        host, _, port = serial.rpartition(":")
        self.serial = serial
        self.sock = socket.create_connection((host, int(port or 5555)), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.wlock = threading.Lock()
        self.streams, self.next_id, self.lock = {}, 1, threading.Lock()
        self.max_data, self.banner, self.alive = 4096, "", True
        self._send(_packet(A_CNXN, A_VERSION, MAX_PAYLOAD, b"host::\0"))
        cmd, a0, a1, data = self._read_packet()
        if cmd == A_AUTH:
            self.sock.close()
            raise AdbError("unauthorized: колонка требует ключ adb — pyadb его не поддерживает")
        if cmd != A_CNXN:
            self.sock.close()
            raise AdbError("protocol fault (нет CNXN)")
        self.max_data = min(a1 or 4096, MAX_PAYLOAD)
        self.banner = data.rstrip(b"\0").decode("utf-8", "replace")
        self.sock.settimeout(None)
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, pkt):
        with self.wlock:
            self.sock.sendall(pkt)

    def _recv_exact(self, n):
        out = bytearray()
        while len(out) < n:
            part = self.sock.recv(n - len(out))
            if not part:
                raise ConnectionError("connection reset")
            out += part
        return bytes(out)

    def _protocol_fault(self, message):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass
        raise AdbError(f"protocol fault ({message})")

    def _read_packet(self):
        cmd, a0, a1, n, _crc, _magic = struct.unpack("<6I", self._recv_exact(24))
        # Доверять n из сети нельзя: сначала проверяем размер, только потом читаем payload.
        # Больше MAX_PAYLOAD по протоколу adb пакет быть не может (иначе n = 0xFFFFFFFF
        # заставил бы _recv_exact выделить 4 ГБ).
        if n > MAX_PAYLOAD:
            self._protocol_fault(f"payload too large: {n} > {MAX_PAYLOAD}")
        return cmd, a0, a1, self._recv_exact(n) if n else b""

    def _reader(self):
        try:
            while True:
                cmd, a0, a1, data = self._read_packet()
                st = self.streams.get(a1)
                if cmd == A_OKAY and st:
                    st.remote = a0
                    st.opened.set()
                    st.okay.set()
                elif cmd == A_WRTE and st:
                    if st._on_data(data):
                        self._send(_packet(A_OKAY, a1, a0))
                elif cmd == A_CLSE and st:
                    self._forget(st)
                    st._on_close()
        except (OSError, ConnectionError, struct.error, AdbError):
            pass
        finally:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass
            with self.lock:
                streams = list(self.streams.values())
                self.streams.clear()
            for st in streams:
                st._on_close()

    def _forget(self, st):
        with self.lock:
            self.streams.pop(st.local, None)

    def open(self, service, timeout=10):
        if not self.alive:
            raise AdbError("error: closed")
        with self.lock:
            local = self.next_id
            self.next_id += 1
            st = self.streams[local] = Stream(self, local)
        try:
            self._send(_packet(A_OPEN, local, 0, service.encode() + b"\0"))
        except OSError:
            self._forget(st)
            st._on_close()
            raise
        if not st.opened.wait(timeout) or st.remote is None:
            st.close()
            raise AdbError("error: closed")
        return st

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass
        with self.lock:
            streams = list(self.streams.values())
            self.streams.clear()
        for st in streams:
            st._on_close()


# ---------- реестр подключённых устройств (как у сервера adb) ----------
_devices, _dev_lock = {}, threading.Lock()


def _norm(serial):
    return serial if ":" in serial else serial + ":5555"


def connect(serial, timeout=10):
    serial = _norm(serial)
    with _dev_lock:
        d = _devices.get(serial)
        if d and d.alive:
            return d, True
    d = Device(serial, timeout)
    with _dev_lock:
        _devices[serial] = d
    return d, False


def device(serial=None):
    with _dev_lock:
        if serial:
            d = _devices.get(_norm(serial))
        else:
            alive = [x for x in _devices.values() if x.alive]
            d = alive[0] if len(alive) == 1 else None
            if not d:
                raise AdbError("error: no devices/emulators found" if not alive else "error: more than one device")
    if not d or not d.alive:
        # adb сам переподключается к колонке, которую раньше подключили, — делаем так же
        try:
            d, _ = connect(serial)
        except OSError:
            raise AdbError(f"error: device '{serial}' not found")
    return d


def disconnect(serial):
    with _dev_lock:
        d = _devices.pop(_norm(serial), None)
    if d:
        d.close()
    return d is not None


# ---------- команды ----------
def shell(dev, cmd, timeout=None):
    st = dev.open("shell:" + cmd)
    out = bytearray()
    try:
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            part = st.read(-1, None if end is None else max(0.01, end - time.monotonic()))
            if not part:
                break
            out += part
    finally:
        st.close()
    return bytes(out)


def _sync_req(st, cmd, arg=b""):
    st.write(cmd + struct.pack("<I", len(arg)) + arg)


def _sync_hdr(st, timeout=120):
    h = st.read_exact(8, timeout)
    return h[:4], struct.unpack("<I", h[4:])[0]


def ls(dev, path):
    st = dev.open("sync:")
    _sync_req(st, b"LIST", path.encode())
    rows = []
    while True:
        h = st.read_exact(4, 30)
        if h == b"DONE":
            st.read_exact(16, 30)
            break
        if h == b"FAIL":
            n = struct.unpack("<I", st.read_exact(4, 30))[0]
            raise AdbError(st.read_exact(n, 30).decode("utf-8", "replace"))
        mode, size, mtime, n = struct.unpack("<4I", st.read_exact(16, 30))
        rows.append((mode, size, mtime, st.read_exact(n, 30).decode("utf-8", "replace")))
    _sync_req(st, b"QUIT")
    st.close()
    return rows


def push(dev, local, remote, mode=0o644):
    with open(local, "rb") as f:
        data = f.read()
    st = dev.open("sync:")
    _sync_req(st, b"SEND", f"{remote},{mode}".encode())
    for i in range(0, len(data), 64 * 1024):
        _sync_req(st, b"DATA", data[i:i + 64 * 1024])
    st.write(b"DONE" + struct.pack("<I", int(time.time())))
    code, n = _sync_hdr(st)
    msg = st.read_exact(n, 30).decode("utf-8", "replace") if code == b"FAIL" and n else ""
    _sync_req(st, b"QUIT")
    st.close()
    if code != b"OKAY":
        raise AdbError(f"adb: error: failed to copy '{local}' to '{remote}': remote {msg or code.decode()}")
    return len(data)


def pull(dev, remote, local):
    st = dev.open("sync:")
    _sync_req(st, b"RECV", remote.encode())
    out = bytearray()
    while True:
        code, n = _sync_hdr(st)
        if code == b"DATA":
            out += st.read_exact(n, 120)
        elif code == b"DONE":
            break
        elif code == b"FAIL":
            msg = st.read_exact(n, 30).decode("utf-8", "replace")
            st.close()
            raise AdbError(f"adb: error: failed to stat remote object '{remote}': {msg}")
        else:
            st.close()
            raise AdbError("protocol fault (sync)")
    _sync_req(st, b"QUIT")
    st.close()
    with open(local, "wb") as f:
        f.write(out)
    return len(out)


_forwards, _forwards_lock = [], threading.Lock()


def _close_socket(sock):
    """Разбудить чужие recv/accept и закрыть сокет, не падая на уже закрытом."""
    shutdown = getattr(sock, "shutdown", None)
    if shutdown is not None:
        try:
            shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        sock.close()
    except OSError:
        pass


def _unregister_forward(srv):
    with _forwards_lock:
        try:
            _forwards.remove(srv)
        except ValueError:
            pass
    _close_socket(srv)


def close_forward(port):
    """Остановить проброс, созданный forward(); True — если такой порт был найден."""
    with _forwards_lock:
        srv = None
        for item in _forwards:
            try:
                if item.getsockname()[1] == port:
                    srv = item
                    break
            except OSError:
                pass
    if srv is None:
        return False
    _unregister_forward(srv)
    return True


def forward(dev, remote_port):
    """tcp:0 → свободный локальный порт; каждое подключение к нему — поток tcp:<порт> на колонке."""
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
    except OSError:
        _close_socket(srv)
        raise
    with _forwards_lock:
        _forwards.append(srv)

    def pump(conn):
        st = None
        try:
            try:
                st = dev.open(f"tcp:{remote_port}")
            except (AdbError, OSError):
                return

            def up():
                try:
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            break
                        st.write(data)
                except (OSError, AdbError):
                    pass
                finally:
                    st.close()
                    shutdown = getattr(conn, "shutdown", None)
                    if shutdown is not None:
                        try:
                            shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            threading.Thread(target=up, daemon=True).start()
            try:
                while True:
                    data = st.read(-1)
                    if not data:
                        break
                    conn.sendall(data)
            except (OSError, AdbError):
                pass
        finally:
            # Любая ошибка (в т.ч. в dev.open) не должна оставлять клиента и поток adb висеть.
            if st is not None:
                st.close()
            _close_socket(conn)

    def accept():
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                try:
                    threading.Thread(target=pump, args=(conn,), daemon=True).start()
                except RuntimeError:
                    _close_socket(conn)
        finally:
            _unregister_forward(srv)

    threading.Thread(target=accept, daemon=True).start()
    return srv.getsockname()[1]


# ---------- подмена subprocess ----------
def _parse(argv):
    args, serial = list(argv[1:]), None
    if args[:1] == ["-s"]:
        serial, args = args[1], args[2:]
    return serial, args


def _shell_command(args):
    """Собрать команду для `adb shell`.

    Один аргумент — это уже готовая команда (панель часто передаёт весь pipeline строкой),
    её нельзя лишний раз квотировать. Несколько аргументов — обычный argv: shlex.join
    сохраняет пробелы, кавычки и спецсимволы и не даёт shell истолковать их случайно.
    """
    if len(args) == 1:
        return args[0]
    return shlex.join(args)


def _run(argv, timeout=None):
    """Одна команда adb → (код, stdout, stderr) байтами, как у настоящего adb."""
    serial, args = _parse(argv)
    cmd = args[0] if args else ""
    try:
        if cmd == "version":
            return 0, VERSION_TEXT.encode(), b""
        if cmd == "connect":
            target = _norm(args[1])
            try:
                _, already = connect(target, timeout=min(timeout or 10, 10))
            except (OSError, AdbError) as e:
                return 1, f"failed to connect to '{target}': {e}\n".encode(), b""
            return 0, (f"already connected to {target}\n" if already else f"connected to {target}\n").encode(), b""
        if cmd == "disconnect":
            if len(args) > 1:
                ok = disconnect(args[1])
                return (0, f"disconnected {_norm(args[1])}\n".encode(), b"") if ok else \
                    (1, b"", f"error: no such device '{_norm(args[1])}'\n".encode())
            for s in list(_devices):
                disconnect(s)
            return 0, b"disconnected everything\n", b""
        if cmd == "devices":
            rows = "".join(f"{d.serial}\tdevice {d.banner.replace('device::', '').replace(';', ' ').strip()}\n"
                           for d in list(_devices.values()) if d.alive)
            return 0, ("List of devices attached\n" + rows + "\n").encode(), b""
        dev = device(serial)
        if cmd == "get-state":
            return 0, b"device\n", b""
        if cmd == "shell":
            return 0, shell(dev, _shell_command(args[1:]), timeout), b""
        if cmd == "push":
            n = push(dev, args[1], args[2])
            return 0, f"{args[1]}: 1 file pushed. {n} bytes\n".encode(), b""
        if cmd == "pull":
            rest = [a for a in args[1:] if a != "-a"]
            n = pull(dev, rest[0], rest[1])
            return 0, f"{rest[0]}: 1 file pulled. {n} bytes\n".encode(), b""
        if cmd == "ls":
            rows = ls(dev, args[1])
            return 0, "".join(f"{m:08x} {s:08x} {t:08x} {n}\n" for m, s, t, n in rows).encode(), b""
        if cmd == "forward" and args[1:2] == ["tcp:0"] and args[2:3] and args[2].startswith("tcp:"):
            return 0, f"{forward(dev, int(args[2][4:]))}\n".encode(), b""
        return 1, b"", f"pyadb: команда не поддерживается: {' '.join(args)}\n".encode()
    except AdbError as e:
        return 1, b"", (str(e) + "\n").encode()
    except (OSError, ConnectionError) as e:
        return 1, b"", f"error: closed ({e})\n".encode()


class _Popen:
    """Потоковый `adb shell` (консоль, светодиоды): stdout — канал, stdin — в поток колонки."""

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, text=False, bufsize=-1, **_):
        serial, args = _parse(argv)
        if not args or args[0] != "shell":
            raise OSError("pyadb: Popen поддерживает только shell")
        self.args, self.returncode = argv, None
        self.st = device(serial).open("shell:" + _shell_command(args[1:]))
        r, w = os.pipe()
        raw = io.FileIO(r, "rb")
        self.stdout = io.TextIOWrapper(io.BufferedReader(raw), encoding="utf-8", errors="replace") if text else raw
        self._w = w
        self.stdin = _StdinWriter(self.st) if stdin == subprocess.PIPE else None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while True:
                data = self.st.read(-1)
                if not data:
                    break
                os.write(self._w, data)
        except (OSError, AdbError):
            pass
        finally:
            self.st.close()
            try:
                os.close(self._w)
            except OSError:
                pass
            self.returncode = 0 if self.returncode is None else self.returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.st.closed.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.st.close()

    terminate = kill


class _StdinWriter:
    def __init__(self, st):
        self.st = st

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        try:
            self.st.write(data)
        except AdbError as e:
            raise BrokenPipeError(str(e))
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


_orig_run, _orig_popen = subprocess.run, subprocess.Popen


def _is_ours(argv):
    return isinstance(argv, (list, tuple)) and argv and argv[0] == ADB


def _patched_run(argv, *a, **kw):
    if not _is_ours(argv):
        return _orig_run(argv, *a, **kw)
    code, out, err = _run(argv, kw.get("timeout"))
    if kw.get("text") or kw.get("universal_newlines"):
        out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if kw.get("check") and code:
        raise subprocess.CalledProcessError(code, argv, out, err)
    return subprocess.CompletedProcess(argv, code, out, err)


def _patched_popen(argv, *a, **kw):
    return _Popen(argv, **kw) if _is_ours(argv) else _orig_popen(argv, *a, **kw)


def install():
    """Подменить adb для всего процесса; вернуть «путь к adb», который отдать панели."""
    # Повторный install() не должен заворачивать наши же обёртки ещё раз.
    if subprocess.run is not _patched_run:
        subprocess.run = _patched_run
    if subprocess.Popen is not _patched_popen:
        subprocess.Popen = _patched_popen
    return ADB
