"""Защитные тесты pyadb: размер пакета, предел буфера потока, кавычки shell, уборка forward."""
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pyadb


def _header(cmd, a0=0, a1=0, n=0, data=b""):
    return struct.pack("<6I", cmd, a0, a1, n, sum(data) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF) + data


class _FakeSock:
    """Сокет, который отдаёт ровно подготовленные байты и падает при лишнем recv."""

    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.closed = False
        self.recv_sizes = []

    def recv(self, n):
        self.recv_sizes.append(n)
        if not self.data:
            raise AssertionError("pyadb прочитал payload, который должен был отклонить")
        chunk = bytes(self.data[:n])
        del self.data[:n]
        return chunk

    def sendall(self, data):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def _device_with_sock(sock):
    dev = pyadb.Device.__new__(pyadb.Device)
    dev.sock, dev.alive, dev.max_data = sock, True, 4096
    dev.wlock = threading.Lock()
    return dev


# ---------- 1. размер пакета ----------

@pytest.mark.parametrize("n", [pyadb.MAX_PAYLOAD + 1, 0xFFFFFFFF])
def test_read_packet_rejects_oversized_payload_without_reading_it(n):
    sock = _FakeSock(_header(pyadb.A_WRTE, 1, 2, n))
    dev = _device_with_sock(sock)

    with pytest.raises(pyadb.AdbError, match="protocol fault"):
        dev._read_packet()

    assert sock.closed
    assert not dev.alive
    assert sock.recv_sizes == [24]          # заголовок прочитан, payload — нет


def test_read_packet_accepts_normal_payload():
    data = b"hello"
    dev = _device_with_sock(_FakeSock(_header(pyadb.A_WRTE, 7, 8, len(data), data)))

    assert dev._read_packet() == (pyadb.A_WRTE, 7, 8, data)


# ---------- 2. предел буфера Stream ----------

class _FakeDev:
    def __init__(self):
        self.sent = []
        self.forgotten = []

    def _send(self, pkt):
        self.sent.append(pkt)

    def _forget(self, st):
        self.forgotten.append(st)


def test_stream_buffer_limit_closes_overflowing_stream(monkeypatch):
    monkeypatch.setattr(pyadb, "MAX_STREAM_BUFFER", 32)
    dev = _FakeDev()
    st = pyadb.Stream(dev, 1)
    st.remote = 77

    assert st._on_data(b"a" * 20) is True
    assert st._on_data(b"b" * 20) is False       # 20 + 20 > 32: не принимаем
    assert st.overflow is True
    assert st.closed.is_set()
    assert len(st.buf) == 20                     # буфер не вырос выше лимита
    assert dev.forgotten == [st]
    assert dev.sent                             # отправлен CLSE

    assert st.read(-1) == b"a" * 20              # уже принятые данные ещё можно забрать
    with pytest.raises(pyadb.AdbError, match="overflow"):
        st.read(-1)


def test_stream_single_chunk_above_limit_is_not_buffered(monkeypatch):
    monkeypatch.setattr(pyadb, "MAX_STREAM_BUFFER", 16)
    dev = _FakeDev()
    st = pyadb.Stream(dev, 1)
    st.remote = 1

    assert st._on_data(b"x" * 17) is False
    assert len(st.buf) == 0
    assert st.overflow is True
    assert st.closed.is_set()

    with pytest.raises(pyadb.AdbError, match="overflow"):
        st.read(-1)


def test_stream_ignores_data_after_close(monkeypatch):
    monkeypatch.setattr(pyadb, "MAX_STREAM_BUFFER", 16)
    dev = _FakeDev()
    st = pyadb.Stream(dev, 1)
    st.remote = 1
    st._on_data(b"a" * 16)
    assert st._on_data(b"b") is False            # ровно лимит, дальше нельзя
    assert st._on_data(b"c" * 1000) is False     # закрытый поток молча игнорирует данные
    assert len(st.buf) == 16


# ---------- 3. кавычки в adb shell ----------

class _EmptyStream:
    def __init__(self):
        self.closed = threading.Event()
        self.closed.set()
        self.was_closed = False
        self.writes = []

    def read(self, n=-1, timeout=None):
        return b""

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.was_closed = True
        self.closed.set()


class _CaptureDev:
    def __init__(self):
        self.services = []
        self.streams = []

    def open(self, service, timeout=10):
        self.services.append(service)
        st = _EmptyStream()
        self.streams.append(st)
        return st


@pytest.mark.parametrize("args, expected", [
    (["echo привет"], "echo привет"),
    (["echo", "hello world"], "echo 'hello world'"),
    (["printf", r"%s\n", "$HOME; rm -rf /"], r"printf '%s\n' '$HOME; rm -rf /'"),
    (["su", "-c", "pm list packages | grep yandex"], "su -c 'pm list packages | grep yandex'"),
])
def test_shell_command_quoting(args, expected):
    assert pyadb._shell_command(args) == expected


def test_run_shell_quotes_argv(monkeypatch):
    dev = _CaptureDev()
    monkeypatch.setattr(pyadb, "device", lambda serial=None: dev)

    code, out, err = pyadb._run([pyadb.ADB, "-s", "fake:5555", "shell", "echo", "hello world"])

    assert (code, out, err) == (0, b"", b"")
    assert dev.services == ["shell:echo 'hello world'"]
    assert dev.streams[0].was_closed


def test_run_shell_keeps_single_string_as_is(monkeypatch):
    dev = _CaptureDev()
    monkeypatch.setattr(pyadb, "device", lambda serial=None: dev)
    command = "echo hello > /data/local/tmp/x && cat /data/local/tmp/x"

    assert pyadb._run([pyadb.ADB, "-s", "fake:5555", "shell", command])[:2] == (0, b"")
    assert dev.services == ["shell:" + command]


def test_popen_shell_quotes_argv(monkeypatch):
    dev = _CaptureDev()
    monkeypatch.setattr(pyadb, "device", lambda serial=None: dev)

    proc = pyadb._Popen([pyadb.ADB, "-s", "fake:5555", "shell", "echo", "a b"])
    try:
        assert proc.wait(5) == 0
        assert dev.services == ["shell:echo 'a b'"]
    finally:
        proc.stdout.close()


def test_popen_shell_keeps_single_string_as_is(monkeypatch):
    dev = _CaptureDev()
    monkeypatch.setattr(pyadb, "device", lambda serial=None: dev)
    command = "echo a b | cat"

    proc = pyadb._Popen([pyadb.ADB, "-s", "fake:5555", "shell", command])
    try:
        assert proc.wait(5) == 0
        assert dev.services == ["shell:" + command]
    finally:
        proc.stdout.close()


# ---------- 4. уборка forward ----------

class _ForwardStream:
    def __init__(self):
        self.closed = False
        self.writes = []

    def read(self, n=-1, timeout=None):
        raise pyadb.AdbError("stream failed")

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True


class _ForwardDev:
    def __init__(self, fail_open=False):
        self.fail_open = fail_open
        self.streams = []

    def open(self, service, timeout=10):
        if self.fail_open:
            raise pyadb.AdbError("cannot open")
        st = _ForwardStream()
        self.streams.append((service, st))
        return st


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _assert_client_closed(client):
    try:
        assert client.recv(64) == b""
    except ConnectionError:          # reset/abort — тоже признак закрытия
        pass


def test_forward_closes_client_and_stream_on_adb_error():
    dev = _ForwardDev()
    port = pyadb.forward(dev, 1234)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.settimeout(5)
            _assert_client_closed(client)
        assert _wait_until(lambda: bool(dev.streams))
        assert dev.streams[0][0] == "tcp:1234"
        assert _wait_until(lambda: dev.streams[0][1].closed)
    finally:
        pyadb.close_forward(port)


def test_forward_closes_client_when_adb_stream_cannot_open():
    dev = _ForwardDev(fail_open=True)
    port = pyadb.forward(dev, 4321)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.settimeout(5)
            _assert_client_closed(client)
    finally:
        pyadb.close_forward(port)


def test_forward_closes_listener_when_bind_fails(monkeypatch):
    class _BrokenSrv:
        def __init__(self):
            self.closed = False

        def bind(self, address):
            raise OSError("address already in use")

        def shutdown(self, how):
            pass

        def close(self):
            self.closed = True

    srv = _BrokenSrv()
    monkeypatch.setattr(pyadb.socket, "socket", lambda *a, **kw: srv)

    with pytest.raises(OSError, match="address already in use"):
        pyadb.forward(None, 1234)
    assert srv.closed


def test_close_forward_is_idempotent():
    dev = _ForwardDev()
    port = pyadb.forward(dev, 1234)
    assert pyadb.close_forward(port) is True
    assert pyadb.close_forward(port) is False


# ---------- 5. install() не ломает обычный subprocess ----------

def test_install_is_idempotent_and_foreign_subprocess_still_works(monkeypatch):
    monkeypatch.setattr(subprocess, "run", pyadb._orig_run)
    monkeypatch.setattr(subprocess, "Popen", pyadb._orig_popen)

    assert pyadb.install() == pyadb.ADB
    assert subprocess.run is pyadb._patched_run
    assert subprocess.Popen is pyadb._patched_popen
    assert pyadb.install() == pyadb.ADB
    assert subprocess.run is pyadb._patched_run       # не завернули сами себя
    assert subprocess.Popen is pyadb._patched_popen

    r = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.strip() == "ok"

    p = subprocess.Popen([sys.executable, "-c", "print('ok')"], stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate(timeout=10)
    assert out.strip() == "ok"

    # pyadb-ветка тоже работает и не трогает сеть
    version = subprocess.run([pyadb.ADB, "version"], capture_output=True, text=True)
    assert version.returncode == 0
    assert "Android Debug Bridge version" in version.stdout
